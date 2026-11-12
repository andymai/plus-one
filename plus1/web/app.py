import argparse
import gzip
import hmac
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from .. import __version__, config, prs, settings, signin, updates
from ..state import State
from ..store import Store
from . import cards, queries, refs

HERE = Path(__file__).resolve().parent
STATIC_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
CSRF_COOKIE = "plus1_csrf"
TOKEN_PARAM = re.compile(r"([?&]code=)[^&\s\"]+")
COMPRESSIBLE = (
    "text/",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
)


class Response:
    def __init__(
        self,
        body: str | bytes = "",
        status=200,
        content_type="text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ):
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.content_type = content_type
        self.headers = headers or {}


def redirect(location, status=303):
    return Response("", status=status, headers={"Location": location})


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def ago(value):
    ts = _parse_ts(value)
    if ts is None:
        return ""
    seconds = int((datetime.now().astimezone() - ts).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def clock(value):
    ts = _parse_ts(value)
    if ts is None:
        return ""
    local = ts.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%b %d %H:%M")


def money(value):
    return f"${(value or 0):.2f}"


def duration(value):
    if value is None:
        return ""
    value = float(value)
    return f"{value:.0f}s" if value < 90 else f"{value / 60:.1f}m"


def elapsed(start, end=None):
    a, b = _parse_ts(start), _parse_ts(end) or datetime.now().astimezone()
    if a is None:
        return ""
    return duration((b - a).total_seconds())


def ticks(value):
    """Escape text, then render `code` spans. Escaping first keeps it safe for untrusted PR content."""
    escaped = str(escape(value or ""))
    return Markup(re.sub(r"`([^`\n]{1,200})`", r"<code>\1</code>", escaped))  # noqa: S704 (escaped above)


TOKEN = re.compile(r"\w[\w.:#/@-]*\w[?!]?|\w")
SKIP_WORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "is",
    "it",
    "as",
    "for",
    "that",
    "this",
    "with",
    "but",
    "not",
}


def _code_like(token):
    return len(token) >= 3 and (re.search(r"[_.:#/\d]", token) or token != token.lower())


def _flag_spans(body, claim, limit=2):
    claim = re.split("\\s+[\u2014\u2013-]\\s+|\\s\\(|:\\s", claim or "", maxsplit=1)[0]
    target = [m.group().lower() for m in TOKEN.finditer(claim)]
    runs, offset = [], 0
    # Search plain and backticked segments separately so a highlight never straddles a code span.
    for segment in re.split(r"(`[^`\n]*`)", body):
        inner = 1 if len(segment) > 1 and segment[0] == segment[-1] == "`" else 0
        toks = [
            (m.start() + offset + inner, m.end() + offset + inner, m.group())
            for m in TOKEN.finditer(segment[inner : len(segment) - inner])
        ]
        for i in range(len(toks)):
            for j in range(len(target)):
                k = 0
                while (
                    i + k < len(toks)
                    and j + k < len(target)
                    and toks[i + k][2].lower() == target[j + k]
                ):
                    k += 1
                lo, hi = i, i + k
                while lo < hi and toks[lo][2].lower() in SKIP_WORDS:
                    lo += 1
                while hi > lo and toks[hi - 1][2].lower() in SKIP_WORDS:
                    hi -= 1
                n = hi - lo
                code = any(_code_like(tok[2]) for tok in toks[lo:hi])
                if n and (n >= 2 or code):
                    runs.append(
                        (n + 2 * code + 2 * (j == 0 and lo == i), toks[lo][0], toks[hi - 1][1])
                    )
        offset += len(segment)
    picked = []
    for _, start, end in sorted(runs, key=lambda r: (-r[0], r[1])):
        if len(picked) < limit and not any(s < end and start < e for s, e in picked):
            picked.append((start, end))
    return picked


def flagged(body, claims):
    text = (body or "").replace("\x01", "").replace("\x02", "")
    spans = []
    for claim in claims or []:
        for span in _flag_spans(text, claim):
            if not any(s < span[1] and span[0] < e for s, e in spans):
                spans.append(span)
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + "\x01" + text[start:end] + "\x02" + text[end:]
    # ticks escapes the text before the marks go in.
    return Markup(  # noqa: S704
        str(ticks(text)).replace("\x01", '<mark class="flag">').replace("\x02", "</mark>")
    )


def dayname(value):
    ts = _parse_ts(value)
    if ts is None:
        return ""
    day = ts.astimezone().date()
    today = datetime.now().astimezone().date()
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    return day.strftime("%a, %b %-d") if day.year == today.year else day.strftime("%b %-d, %Y")


def hue(value, buckets=8):
    return sum((value or "").encode()) % buckets


def focus_rows(rows, radius=6):
    marked = [i for i, row in enumerate(rows) if row.get("mark")]
    if not marked:
        return rows
    keep = sorted(
        {j for i in marked for j in range(max(0, i - radius), min(len(rows), i + radius + 1))}
    )
    out, last = [], None
    for j in keep:
        if last is not None and j != last + 1:
            out.append({"kind": "gap"})
        if rows[j]["kind"] != "hunk" or (last is not None and j == last + 1):
            out.append(rows[j])
        last = j
    return out


def tilde(path):
    text = str(path)
    # Home can be reached through a link (/home/you to /var/home/you), so either spelling shortens.
    for home in dict.fromkeys((str(Path.home()), os.path.realpath(Path.home()))):
        if text == home or text.startswith(home + os.sep):
            return "~" + text[len(home) :]
    return text


class App:
    def __init__(self, cfg: SimpleNamespace, dev_login="", run_executor=False):
        self.cfg = cfg
        self.dev_login = dev_login
        self.run_executor = run_executor
        self.local = threading.local()
        self.env = Environment(
            loader=FileSystemLoader(HERE / "templates"),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters.update(
            ago=ago,
            clock=clock,
            money=money,
            duration=duration,
            elapsed=elapsed,
            short=lambda s: (s or "")[:8],
            anchor=refs.anchor,
            keys=lambda ns: list(vars(ns)),
            focus_rows=focus_rows,
            ticks=ticks,
            hue=hue,
            flagged=flagged,
            dayname=dayname,
            refs=lambda html, r: r.linkify(html) if r else html,
            prnum=prs.number,
            prlabel=prs.label,
            prurl=prs.url,
            tilde=tilde,
        )
        self._label_repos()
        self.routes = []
        self.config_stamp = self._stamp()
        from . import routes

        routes.register(self)

    def _label_repos(self):
        names = [r.name for r in self.cfg.repos]
        self.env.globals.update(
            repo=self.cfg.repo,
            repos_label=names[0] if len(names) == 1 else f"{len(names)} repos",
            version=__version__,
            build=updates.build(),
        )

    def _stamp(self):
        try:
            return self.cfg.config_path.stat().st_mtime_ns
        except (AttributeError, OSError):
            return None

    def adopt(self, new):
        """Takes on a freshly loaded config in place, so every thread holding cfg sees it. Where state lives and how
        the app is served stay as they started, since changing those needs a restart."""
        new.state_dir, new.web = self.cfg.state_dir, self.cfg.web
        vars(self.cfg).update(vars(new))
        self.config_stamp = self._stamp()
        # A repo added by hand needs an id before any request looks one up.
        Store(self.cfg).db.close()
        self._label_repos()

    def refresh_config(self):
        """Picks up config.toml edits, from the settings page or by hand, without a restart."""
        stamp = self._stamp()
        if stamp is None or stamp == self.config_stamp:
            return
        try:
            with settings.LOCK:
                self.adopt(config.load(self.cfg.config_path))
        except (config.ConfigError, OSError) as exc:
            # A broken edit keeps the last good config running until it's fixed.
            self.config_stamp = stamp
            sys.stderr.write(f"config.toml didn't load, keeping the last good one: {exc}\n")

    def route(self, method, pattern):
        def wrap(fn):
            self.routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn

        return wrap

    @property
    def store(self):
        if getattr(self.local, "store", None) is None:
            self.local.store = Store(self.cfg)
        return self.local.store

    @property
    def state(self):
        return State(self.cfg)

    def render(self, name, request, **context):
        context.setdefault("status", queries.status(self.cfg, self.store, self.state))
        todo = context.get("cards")
        if "todo_count" not in context:
            todo = todo if todo is not None else cards.todo(self.store, self.cfg, light=True)
            context["todo_count"] = len(todo)
        # Partials pass todo_count=None and have no sidebar to count for.
        if context["todo_count"] is not None and "grade_count" not in context:
            if todo is None:
                todo = cards.todo(self.store, self.cfg, light=True)
            context["grade_count"] = queries.ungraded_count(self.store, {c["pr"] for c in todo})
        if context["todo_count"] is not None and not name.startswith("partials/"):
            context.update(
                update_notice=updates.intro(self.cfg, self.store),
                update_note=updates.note(self.cfg, self.store),
            )
        context.update(
            csrf=request.csrf,
            path=request.path,
            cfg=self.cfg,
            auto_repos=config.auto_repos(self.cfg),
            flash=request.arg("msg"),
            flash_error=request.arg("error"),
        )
        return Response(self.env.get_template(name).render(**context))


class Request:
    def __init__(self, handler, method, path, query, form, csrf):
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.form = form
        self.csrf = csrf
        self.htmx = handler.headers.get("HX-Request") == "true"

    def arg(self, name, default=""):
        return self.query.get(name, [default])[0]

    def field(self, name, default=""):
        return self.form.get(name, [default])[0]


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "plus1-web"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        line = TOKEN_PARAM.sub(r"\1<redacted>", format % args)
        sys.stderr.write(f"{datetime.now().strftime('%H:%M:%S')} {line}\n")

    def address_string(self):
        return "tailnet"

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def _cookie(self, name):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[name].value if name in cookie else None

    def authorize(self, url, query):
        """None once the request is the owner's, or the response that signs this browser in."""
        cfg = self.app.cfg
        if self.app.dev_login:
            return None
        if cfg.web.mode == "tailscale":
            login = self.headers.get("Tailscale-User-Login", "")
            if not login or not hmac.compare_digest(login.encode(), cfg.web.allowed_login.encode()):
                raise HttpError(403, "This app only serves its owner's tailnet login.")
            return None
        if not signin.loopback(self.headers.get("Host")):
            raise HttpError(403, f"Open Plus One at http://127.0.0.1:{cfg.web.port}.")
        if url.path == "/login":
            if not signin.redeem(self.app.store, query.get("code", [""])[0]):
                raise HttpError(
                    401,
                    "That sign-in link was used or is out of date. Run bin/plus1 open for a new one.",
                )
            cookie = (
                f"{signin.COOKIE}={signin.session(cfg)}; Path=/; SameSite=Lax; HttpOnly; "
                f"Max-Age={signin.SESSION_DAYS * 86400}"
            )
            return Response(b"", status=303, headers={"Location": "/", "Set-Cookie": cookie})
        if not signin.valid_session(cfg, self._cookie(signin.COOKIE)):
            raise HttpError(401, "Sign in with the link from bin/plus1 open.")
        return None

    def dispatch(self, method):
        try:
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            signed_in = self.authorize(url, query)
            self.app.refresh_config()
            if signed_in is not None:
                self.respond(signed_in)
                return
            new_cookie = None
            if self.app.cfg.web.mode == "local":
                # Every port on this host is one site to a browser, so a page served on another port could plant a
                # CSRF cookie. Derived from the secret instead, the token can't be planted.
                csrf = signin.csrf(self.app.cfg)
            else:
                csrf = self._cookie(CSRF_COOKIE)
                if not csrf or not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", csrf):
                    csrf = secrets.token_urlsafe(32)
                    new_cookie = csrf
            form = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    raise HttpError(413, "Request too large.")
                raw = self.rfile.read(length).decode() if length else ""
                form = urllib.parse.parse_qs(raw, keep_blank_values=True)
                sent = self.headers.get("X-CSRF-Token") or form.get("csrf", [""])[0]
                if new_cookie or not sent or not hmac.compare_digest(sent.encode(), csrf.encode()):
                    raise HttpError(
                        403, "Missing or stale CSRF token. Reload the page and try again."
                    )
            request = Request(self, method, url.path, query, form, csrf)
            for route_method, pattern, fn in self.app.routes:
                if route_method != method:
                    continue
                m = pattern.match(url.path)
                if m:
                    result = fn(request, *m.groups())
                    if result is None:
                        return
                    self.respond(result, new_cookie)
                    return
            raise HttpError(404, "Not found.")
        except HttpError as exc:
            self.respond(Response(self._error_page(exc.status, exc.message), status=exc.status))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.respond(
                Response(
                    self._error_page(500, "Something went wrong. See the server log."), status=500
                )
            )

    def _error_page(self, status, message):
        return self.app.env.get_template("error.html").render(status=status, message=message)

    def security_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def respond(self, response, new_cookie=None):
        body = response.body
        compress = (
            len(body) > 1024
            and response.content_type.startswith(COMPRESSIBLE)
            and "gzip" in self.headers.get("Accept-Encoding", "")
        )
        if compress:
            body = gzip.compress(body, compresslevel=6)
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(body)))
        if compress:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.security_headers()
        if "Cache-Control" not in response.headers:
            self.send_header("Cache-Control", "no-store")
        if new_cookie:
            self.send_header(
                "Set-Cookie",
                f"{CSRF_COOKIE}={new_cookie}; Path=/; Secure; SameSite=Strict; HttpOnly",
            )
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self, store, last_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.security_headers()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        deadline = time.monotonic() + 1800
        beat = time.monotonic()
        try:
            while time.monotonic() < deadline:
                rows = queries.events_after(store, last_id)
                for row in rows:
                    last_id = row["id"]
                    payload = json.dumps(
                        {
                            k: row[k]
                            for k in ("id", "pr", "stage", "message", "at", "title", "review_id")
                        }
                    )
                    self.wfile.write(
                        f"id: {last_id}\nevent: progress\ndata: {payload}\n\n".encode()
                    )
                if rows:
                    self.wfile.flush()
                if time.monotonic() - beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    beat = time.monotonic()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("tailnet", 0)


class TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def smoke(cfg):
    """An update runs this before restarting onto new code: every template compiles and To do builds from real history."""
    app = App(cfg)
    for name in app.env.list_templates():
        app.env.get_template(name)
    cards.todo(app.store, cfg, light=True)
    print("ok", flush=True)


def make_handler(cfg, dev_login="", run_executor=False):
    return type("BoundHandler", (Handler,), {"app": App(cfg, dev_login, run_executor)})


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plus1-web")
    parser.add_argument("--socket", help="unix socket path (default from config)")
    parser.add_argument("--tcp", help="host:port for local testing only")
    parser.add_argument(
        "--dev-login",
        action="store_true",
        help="with --tcp, treat requests without an identity header as the owner",
    )
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.dev_login and not args.tcp:
        raise SystemExit("--dev-login only works with --tcp")
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        raise SystemExit(f"error: {exc}") from None
    if args.smoke:
        smoke(cfg)
        return
    # Any non-empty name stands in for the owner; local mode has no tailnet login to borrow.
    handler = make_handler(
        cfg,
        (cfg.web.allowed_login or cfg.me) if args.dev_login else "",
        run_executor=not args.dev_login,
    )
    if not args.dev_login:
        from . import executor

        executor.start(cfg)
        cards.watch_github(cfg)
        updates.watch(cfg)
    if args.tcp:
        host, port = args.tcp.rsplit(":", 1)
        if host not in ("127.0.0.1", "localhost"):
            raise SystemExit("--tcp only binds to loopback")
        server = TcpServer((host, int(port)), handler)
        print(f"listening on http://{host}:{port}", flush=True)
    elif cfg.web.mode == "local":
        server = TcpServer(("127.0.0.1", cfg.web.port), handler)
        print(
            f"listening on http://127.0.0.1:{cfg.web.port} (bin/plus1 open signs you in)",
            flush=True,
        )
    else:
        path = Path(args.socket or os.path.expanduser(cfg.web.socket))
        path.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            server = UnixServer(str(path), handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        print(f"listening on unix:{path}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
