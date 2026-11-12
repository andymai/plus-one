"""Sign-in for the web app on this machine's loopback.

A long-lived secret in the state dir never leaves it: the session cookie is derived from it, and the link that signs a
browser in carries a one-time code instead, since a link passes through a browser's command line and history.
"""

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse

from . import host

COOKIE = "plus1_session"
HOSTS = {"127.0.0.1", "localhost", "::1"}
CODE_SECONDS = 300
SESSION_DAYS = 30
MIN_TOKEN = 32


def _path(cfg):
    return cfg.state_dir / "web-token"


def _read(path):
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        return None
    # A short or empty secret would make a session anyone can compute, so it never counts.
    return value if len(value) >= MIN_TOKEN else None


def token(cfg, new=False):
    """The secret sessions derive from, made on first use. A new one signs every browser out."""
    path = _path(cfg)
    current = None if new else _read(path)
    if current:
        return current
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".web-token-{secrets.token_hex(8)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32))
    # Replace only on purpose or over a broken secret. Otherwise another process may have just made a good one, and
    # replacing it would end the session it signed in.
    if new or (path.exists() and not _read(path)):
        os.replace(tmp, path)
    else:
        # Two processes making the first secret at once both end up with whichever landed first.
        with contextlib.suppress(FileExistsError):
            os.link(tmp, path)
        os.unlink(tmp)
    value = _read(path)
    if not value:
        raise RuntimeError(f"{path} doesn't hold a usable secret")
    return value


def session(cfg, issued=None):
    """A session cookie: when it was issued, signed with the secret, so one that leaks stops working on its own."""
    issued = int(time.time() if issued is None else issued)
    mac = hmac.new(
        token(cfg).encode(), f"plus1 session {issued}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{issued}.{mac}"


def csrf(cfg):
    return hmac.new(token(cfg).encode(), b"plus1 csrf", hashlib.sha256).hexdigest()


def valid_session(cfg, sent):
    issued, _, _ = (sent or "").partition(".")
    if not issued.isdigit() or time.time() - int(issued) > SESSION_DAYS * 86400:
        return False
    return hmac.compare_digest(sent.encode(), session(cfg, int(issued)).encode())


def _digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def link(cfg, store, new=False):
    """A sign-in link good once, for CODE_SECONDS. With new, every browser signed in so far is signed out."""
    token(cfg, new=new)
    code = secrets.token_urlsafe(24)
    store.set_meta(
        "web_login", json.dumps({"digest": _digest(code), "expires": time.time() + CODE_SECONDS})
    )
    return f"http://127.0.0.1:{cfg.web.port}/login?code={code}"


REDIRECT = """<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<meta http-equiv="refresh" content="0;url={url}"><title>Plus One</title><a href="{url}">Open Plus One</a>
"""


def open_in_browser(cfg, store, new=False):
    """Opens a fresh sign-in link through a page in the state dir, so the code stays off the browser's command line,
    which other users on this machine can read. Returns the link."""
    url = link(cfg, store, new=new)
    page = cfg.state_dir / "sign-in.html"
    page.unlink(missing_ok=True)
    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(REDIRECT.format(url=url))
    host.open_url(page.resolve().as_uri())
    return url


def redeem(store, code):
    raw = store.get_meta("web_login")
    if not code or not raw:
        return False
    pending = json.loads(raw)
    if pending["expires"] < time.time() or not hmac.compare_digest(
        _digest(code), pending["digest"]
    ):
        return False
    # Only the request that clears the code signs in, so a link can't be used twice.
    cleared = store.db.execute(
        "DELETE FROM meta WHERE key = 'web_login' AND value = ?", (raw,)
    ).rowcount
    return cleared == 1


def loopback(host_header):
    """Whether a request names this machine, which a page using DNS rebinding to reach the port can't fake."""
    try:
        return urllib.parse.urlsplit("//" + (host_header or "")).hostname in HOSTS
    except ValueError:
        return False
