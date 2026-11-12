"""Keeps an install current the way a hosted web app stays current: it notices a new version, installs it at an idle
moment, checks that it came up, and goes back if it didn't. It only ever moves this clone's main forward, so a clone
holding someone's own work is left alone."""

import functools
import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta

from . import __version__, actions, config, host, signin, units
from .state import State
from .store import Store, now

ROOT = host.ROOT
BRANCH = "main"
LOCAL_COMMITS = f"this clone has commits that aren't on {BRANCH}"
PAGE = "/settings#updates"
BUILD_LENGTH = 12
CHECK_EVERY = timedelta(hours=1)
WATCH_EVERY_S = 600
AUTO_WAIT_S = 120
NOW_WAIT_S = 1800
HEALTH_WAIT_S = 60
# Longer than any install takes, so a job that died mid-update can't hold off the next one forever.
INSTALLING_FOR = timedelta(minutes=20)
# Card actions wait while this is up. They expire 120 seconds overdue, so a job that died here can't cost them that.
RESTARTING_FOR = timedelta(seconds=120)
QUIET_WAIT_S = 90
SETTLE_S = 1
HISTORY = 20

STATE = "updates_state"
HISTORY_KEY = "updates_history"
SKIP = "updates_skip"
PREVIOUS = "updates_previous"
INSTALLING = "updates_installing"
RESTARTING = "updates_restarting"
TOLD = "updates_told"
INTRO = "updates_intro"
INTRO_TEXT = "Plus One now keeps itself up to date. You can change that in Settings, Updates."


class Failed(RuntimeError):
    pass


class StillRunning(Failed):
    """Nothing is wrong with the new version, it just isn't a good moment."""


# ConfigError and units.install's missing-systemd error are RuntimeErrors too.
FAILURES = (RuntimeError, OSError, subprocess.SubprocessError)


def _git(*args, timeout=60):
    # Nothing here can answer a prompt, so a missing credential has to fail rather than hang.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND")
        or "ssh -o BatchMode=yes -o ConnectTimeout=15",
    }
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout, env=env
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _ok(*args, timeout=60):
    code, out, err = _git(*args, timeout=timeout)
    if code != 0:
        raise Failed(err.splitlines()[-1] if err else f"git {args[0]} failed")
    return out


def _load(store, key, default):
    raw = store.get_meta(key)
    return json.loads(raw) if raw else default


def _short(exc):
    return str(exc).strip()[:300]


def _older_than(stamp, age):
    return not stamp or datetime.now().astimezone() - datetime.fromisoformat(stamp) >= age


def head():
    code, out, _ = _git("rev-parse", "HEAD")
    return out if code == 0 else ""


@functools.lru_cache(maxsize=1)
def build():
    """What this process is running, read once at startup: pulling new files doesn't change the code already loaded."""
    return head()[:BUILD_LENGTH] or __version__


def _ancestor(older, newer):
    return _git("merge-base", "--is-ancestor", older, newer)[0] == 0


def _version_at(sha):
    code, text, _ = _git("show", f"{sha}:plus1/__init__.py")
    found = re.search(r'__version__ = "([^"]+)"', text) if code == 0 else None
    return found.group(1) if found else sha[:7]


def blocked():
    """Why this clone can't take an update, or None. Moving it would overwrite someone's own work."""
    if _git("rev-parse", "--is-inside-work-tree")[0] != 0:
        return "this isn't a git clone"
    _, branch, _ = _git("symbolic-ref", "--short", "-q", "HEAD")
    if branch != BRANCH:
        return f"this clone is on {branch or 'a detached commit'}, not {BRANCH}"
    if _git("status", "--porcelain", "--untracked-files=no")[1]:
        return "this clone has uncommitted changes"
    return None


def latest(cfg):
    """(commit, name) of the newest version on the channel, or (None, "") when it has none yet."""
    origin = f"origin/{BRANCH}"
    if cfg.updates.channel == BRANCH:
        sha = _ok("rev-parse", origin)
        return sha, f"{_version_at(sha)} ({sha[:7]})"
    tags = _ok("tag", "--list", "v[0-9]*", "--merged", origin, "--sort=-v:refname").split()
    if not tags:
        return None, ""
    return _ok("rev-parse", f"{tags[0]}^{{commit}}"), tags[0].removeprefix("v")


def _where(store, sha):
    if not sha or _ancestor(sha, "HEAD"):
        return "current", ""
    why = blocked()
    if why is None and not _ancestor("HEAD", sha):
        why = LOCAL_COMMITS
    if why:
        return "blocked", why
    return ("skipped" if store.get_meta(SKIP) == sha else "available"), ""


def _tell(cfg, store, reason):
    """Says once that an update is stuck, since it'd otherwise be stuck silently."""
    told = store.get_meta(TOLD) or ""
    if reason == told:
        return
    store.set_meta(TOLD, reason)
    if reason and cfg.updates.mode == "automatic":
        actions.notify(
            cfg,
            "Plus One can't update",
            f"A new version is waiting, but {reason}.",
            path=PAGE,
            store=store,
        )


def check(cfg, store):
    """Asks origin what's newest and saves where this clone stands. A failed fetch keeps what's known."""
    saved = _load(store, STATE, {})
    try:
        _ok(
            "fetch",
            "--quiet",
            "--tags",
            "--force",
            "origin",
            f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}",
            timeout=120,
        )
        sha, name = latest(cfg)
    except FAILURES as exc:
        state = {**saved, "checked_at": now(), "error": _short(exc)}
        store.set_meta(STATE, json.dumps(state))
        return state
    status, reason = _where(store, sha)
    state = {
        "checked_at": now(),
        "status": status,
        "latest": name,
        "latest_sha": sha or "",
        "reason": reason,
    }
    store.set_meta(STATE, json.dumps(state))
    _tell(cfg, store, reason)
    return state


def _running(store):
    return store.query(
        "SELECT 1 FROM pending_actions WHERE status = 'running' AND execute_at > ?",
        time.time() - 1800,
    )


def _working(store):
    """What's mid-flight that an update would change the code under, or None. Each row counts only while it's recent,
    so one a crash left behind can't hold off updates forever."""
    moment = datetime.now().astimezone()
    if _running(store) or store.query(
        "SELECT 1 FROM pending_actions WHERE status = 'pending' AND execute_at > ?",
        time.time() - 300,
    ):
        return "a card action is running"
    writers = (moment - timedelta(minutes=actions.WRITING_MINUTES)).isoformat(timespec="seconds")
    for table, what in (("drafts", "a comment"), ("rewrites", "a rewrite")):
        if store.query(
            f"SELECT 1 FROM {table} WHERE status = 'writing' AND updated_at > ?",  # noqa: S608 (fixed names)
            writers,
        ):
            return f"{what} is being written"
    proposals = (moment - timedelta(minutes=30)).isoformat(timespec="seconds")
    if store.query(
        "SELECT 1 FROM proposals WHERE status = 'pending' AND created_at > ?", proposals
    ):
        return "a rubric change is being drafted"
    return None


def busy(cfg, store):
    lock = State(cfg).lock()
    if lock is None:
        return "a check is running"
    lock.close()
    return _working(store)


def installing(store):
    running = _load(store, INSTALLING, {})
    return running if running and not _older_than(running.get("at"), INSTALLING_FOR) else None


def maybe(cfg, store, log=print):
    """The automatic path: check when due, and start an install when one's waiting and nothing is in progress."""
    if cfg.updates.mode == "off":
        return
    state = _load(store, STATE, {})
    if _older_than(state.get("checked_at"), CHECK_EVERY):
        state = check(cfg, store)
    if (
        cfg.updates.mode != "automatic"
        or state.get("status") != "available"
        or state.get("error")
        or installing(store)
        or busy(cfg, store)
    ):
        return
    log(f"updating to Plus One {state['latest']}")
    host.spawn(cfg.state_dir, "update", ["update", "--auto"])


def _catch_up(cfg, store):
    """A clone pulled by hand leaves the web app on the old code until something restarts it."""
    disk = head()[:BUILD_LENGTH]
    if (
        cfg.updates.mode == "automatic"
        and disk
        and disk != build()
        and units.installed(web=True)
        and not blocked()
        and not installing(store)
        and not busy(cfg, store)
    ):
        host.spawn(cfg.state_dir, "restart-web", ["restart-web"])


def watch(cfg):
    def loop():
        store = Store(cfg)
        while True:
            time.sleep(60)
            try:
                maybe(cfg, store)
                _catch_up(cfg, store)
            except Exception as exc:
                print(f"update check failed: {exc}", flush=True)
            time.sleep(WATCH_EVERY_S)

    thread = threading.Thread(target=loop, name="plus1-updates", daemon=True)
    thread.start()
    return thread


def _record(store, version, result, why=""):
    history = _load(store, HISTORY_KEY, [])
    history.insert(0, {"version": version, "at": now(), "result": result, "why": why})
    store.set_meta(HISTORY_KEY, json.dumps(history[:HISTORY]))


def _deps():
    proc = subprocess.run(
        [str(ROOT / "bin" / "ensure-venv")], capture_output=True, text=True, timeout=600
    )
    if proc.returncode != 0:
        raise Failed(f"its dependencies wouldn't install: {_short(proc.stderr or proc.stdout)}")


def _smoke():
    """Runs the new code, not this process's, before anything restarts onto it."""
    for name, *args in (("plus1", "--version"), ("plus1-web", "--smoke")):
        proc = subprocess.run(
            [str(ROOT / "bin" / name), *args],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=ROOT,
        )
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            raise Failed(f"it failed its smoke test: {lines[-1] if lines else name}"[:300])


def restarting(store):
    return not _older_than(store.get_meta(RESTARTING), RESTARTING_FOR)


def _restart_quietly(store, insist=False):
    """A restart fails whatever the web app is doing, and being idle when an install began says nothing about now, so
    this holds new card actions back and lets running ones finish. Held ones run on the new process. One that won't
    finish stops the restart, unless the app has to come back whatever it costs."""
    store.set_meta(RESTARTING, now())
    try:
        # The executor reads the flag just before it marks a row running, so give one already past that a moment.
        time.sleep(SETTLE_S)
        deadline = time.monotonic() + QUIET_WAIT_S
        while _running(store) and time.monotonic() < deadline:
            time.sleep(1)
        if _running(store) and not insist:
            raise StillRunning("a card action is still running")
        _restart()
    finally:
        store.set_meta(RESTARTING, "")


def _restart():
    timer, web = units.installed(), units.installed(web=True)
    if timer:
        units.install(config.load(), web=web)
    elif web:
        units.restart_web()


class _Unix(http.client.HTTPConnection):
    def __init__(self, unix_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = unix_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def serving(cfg):
    """The build the web app answers with, "" from a version that doesn't say, or None when it isn't answering."""
    if cfg.web.mode == "tailscale":
        conn = _Unix(cfg.web.socket, 5)
        headers = {"Tailscale-User-Login": cfg.web.allowed_login}
    else:
        conn = http.client.HTTPConnection("127.0.0.1", cfg.web.port, timeout=5)
        headers = {"Cookie": f"{signin.COOKIE}={signin.session(cfg)}"}
    try:
        conn.request("GET", "/healthz", headers=headers)
        response = conn.getresponse()
        words = response.read().decode().split()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    if response.status != 200:
        return None
    return words[1] if len(words) > 1 else ""


def _healthy(cfg, sha, exact=True):
    if not units.installed(web=True):
        return
    deadline = time.monotonic() + HEALTH_WAIT_S
    seen = None
    while time.monotonic() < deadline:
        seen = serving(cfg)
        if seen is not None and (not exact or seen == sha[:BUILD_LENGTH]):
            return
        time.sleep(2)
    raise Failed(
        "the web app didn't come back" if seen is None else f"the web app is still on {seen[:7]}"
    )


def _restore(cfg, store, sha, restart=True):
    """Goes back to a commit. Returns what went wrong, or None. Without a restart the old code never stopped running,
    so only the files need to go back."""
    try:
        _ok("reset", "--keep", sha)
        _deps()
        if restart:
            _restart_quietly(store, insist=True)
            _healthy(cfg, sha, exact=False)
    except FAILURES as exc:
        return _short(exc)
    return None


def _settle(cfg, store, wait_s):
    """Takes the tick lock, so no check starts mid-update, then waits for anything else to finish."""
    deadline = time.monotonic() + wait_s
    lock = State(cfg).lock(wait_s=wait_s)
    if lock is None:
        return None
    while _working(store):
        if time.monotonic() >= deadline:
            lock.close()
            return None
        time.sleep(5)
    return lock


def _install(cfg, store, state, say):
    sha, name = state["latest_sha"], state["latest"]
    previous = head()
    store.set_meta(INSTALLING, json.dumps({"at": now(), "to": name}))
    say(f"installing Plus One {name}")
    restarting_now = False
    try:
        _ok("merge", "--ff-only", "--quiet", sha, timeout=120)
        _deps()
        _smoke()
        restarting_now = True
        _restart_quietly(store)
        _healthy(cfg, sha)
    except StillRunning as exc:
        stuck = _restore(cfg, store, previous, restart=False)
        if stuck:
            body = f"Going back failed: {stuck}."
            actions.notify(cfg, "Plus One update failed", body, path=PAGE, store=store)
            say(body)
            return 1
        say(f"Not now: {exc}. It'll try again.")
        return 0
    except FAILURES as exc:
        why = _short(exc)
        stuck = _restore(cfg, store, previous, restart=restarting_now)
        store.set_meta(SKIP, sha)
        _record(store, name, "rolled back", why)
        body = f"{name} didn't work: {why}. "
        body += f"Going back failed too: {stuck}." if stuck else f"Back on {_version_at(previous)}."
        actions.notify(cfg, "Plus One update failed", body, path=PAGE, store=store)
        store.set_meta(STATE, json.dumps({**state, "checked_at": now(), "status": "skipped"}))
        say(body)
        return 1
    finally:
        store.set_meta(INSTALLING, "")
    store.set_meta(PREVIOUS, json.dumps({"sha": previous, "version": _version_at(previous)}))
    store.set_meta(SKIP, "")
    _record(store, name, "installed")
    store.set_meta(STATE, json.dumps({**state, "checked_at": now(), "status": "current"}))
    say(f"now on Plus One {name}")
    return 0


def _update(cfg, store, how, say):
    lock = _settle(cfg, store, AUTO_WAIT_S if how == "auto" else NOW_WAIT_S)
    if lock is None:
        say("Something is still running, so nothing changed.")
        return 0 if how == "auto" else 1
    try:
        state = check(cfg, store)
        if state.get("error"):
            say(f"Couldn't ask GitHub for the latest version: {state['error']}")
            return 1
        if state["status"] == "blocked":
            say(f"Can't update: {state['reason']}.")
            return 1
        if state["status"] == "current":
            say(f"Already on the latest Plus One ({_version_at('HEAD')}).")
            if how != "auto":
                _restart_quietly(store)
            return 0
        if state["status"] == "skipped" and how == "auto":
            return 0
        return _install(cfg, store, state, say)
    finally:
        lock.close()


def _rollback(cfg, store, say):
    previous = _load(store, PREVIOUS, {})
    if not previous:
        say("There's no earlier version to go back to.")
        return 1
    lock = _settle(cfg, store, NOW_WAIT_S)
    if lock is None:
        say("Something is still running, so nothing changed.")
        return 1
    try:
        # Going back moves main, so it would take any commit made since with it.
        why = blocked() or (None if _ancestor("HEAD", f"origin/{BRANCH}") else LOCAL_COMMITS)
        if why:
            say(f"Can't roll back: {why}.")
            return 1
        leaving = head()
        version = _version_at(leaving)
        store.set_meta(INSTALLING, json.dumps({"at": now(), "to": previous["version"]}))
        try:
            _ok("reset", "--keep", previous["sha"])
            _deps()
            _restart_quietly(store)
            _healthy(cfg, previous["sha"], exact=False)
        except StillRunning as exc:
            stuck = _restore(cfg, store, leaving, restart=False)
            if stuck:
                body = f"Couldn't finish going back, and returning to {version} failed: {stuck}."
                actions.notify(cfg, "Plus One rollback failed", body, path=PAGE, store=store)
                say(body)
                return 1
            say(f"Not now: {exc}. Nothing changed, so try again in a moment.")
            return 1
        except FAILURES as exc:
            again = _restore(cfg, store, leaving)
            body = f"Couldn't go back to {previous['version']}: {_short(exc)}."
            if again:
                body += f" Returning to {version} failed too: {again}."
            actions.notify(cfg, "Plus One rollback failed", body, path=PAGE, store=store)
            say(body)
            return 1
        finally:
            store.set_meta(INSTALLING, "")
        store.set_meta(SKIP, leaving)
        store.set_meta(PREVIOUS, "")
        _record(store, version, "rolled back", "you asked")
        state = _load(store, STATE, {})
        store.set_meta(STATE, json.dumps({**state, "checked_at": now(), "status": "skipped"}))
        say(f"back on Plus One {previous['version']}")
        return 0
    finally:
        lock.close()


def run(cfg, how="now", say=print):
    """How is now (waits for its moment, and installs a version an earlier rollback skipped), auto (gives up quickly
    if things stay busy), or rollback."""
    store = Store(cfg)
    try:
        return _rollback(cfg, store, say) if how == "rollback" else _update(cfg, store, how, say)
    finally:
        store.db.close()


def intro(cfg, store):
    """The notice each install shows once, since it now changes its own code."""
    if cfg.updates.mode == "off" or store.get_meta(INTRO):
        return None
    store.set_meta(INTRO, now())
    return INTRO_TEXT


def note(cfg, store):
    """The sidebar's line, when there's something to do about updates."""
    status = _load(store, STATE, {}).get("status")
    if cfg.updates.mode == "off":
        return None
    if status == "available" and cfg.updates.mode == "ask":
        return "Update available"
    return "Updates paused" if status == "blocked" else None


def view(cfg, store):
    history = _load(store, HISTORY_KEY, [])
    return {
        "version": __version__,
        "commit": build()[:7],
        "state": _load(store, STATE, {}),
        "previous": _load(store, PREVIOUS, {}),
        "history": history,
        "installing": installing(store),
        "installed_at": next((h["at"] for h in history if h["result"] == "installed"), None),
    }


CHANGELOG_GROUPS = {
    "⚠ BREAKING CHANGES": "Changed",
    "Features": "New",
    "Bug Fixes": "Fixed",
    "Performance": "Faster",
}
RELEASE = re.compile(r"^##+ \[?(\d[^\]\s]*)\]?(?:\([^)]*\))?(?: \((\d{4}-\d{2}-\d{2})\))?")
LINKS = re.compile(r"\s*\(?\[[^\]]+\]\([^)]+\)\)?(?:, closes .*)?")
SCOPE = re.compile(r"^\*\*[^*]+:\*\*\s*")


def changelog():
    """The releases in CHANGELOG.md, newest first, keeping what someone using the app would care about."""
    try:
        lines = (ROOT / "CHANGELOG.md").read_text().splitlines()
    except OSError:
        return []
    releases, group = [], None
    for line in lines:
        release = RELEASE.match(line)
        if release:
            day = datetime.strptime(release[2], "%Y-%m-%d") if release[2] else None
            date = f"{day:%b} {day.day}, {day.year}" if day else ""
            releases.append({"version": release[1], "date": date, "groups": {}})
            group = None
        elif line.startswith("### ") and releases:
            group = CHANGELOG_GROUPS.get(line[4:].strip())
        elif line.startswith("* ") and releases and group:
            text = SCOPE.sub("", LINKS.sub("", line[2:])).strip()
            releases[-1]["groups"].setdefault(group, []).append(text[:1].upper() + text[1:])
    return [r for r in releases if r["groups"]]
