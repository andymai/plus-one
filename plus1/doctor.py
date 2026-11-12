"""bin/plus1 doctor: checks each thing Plus One needs and says how to fix what's missing."""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from . import __version__, config, gh, host, units, updates
from .setup import matches


def _run(*cmd, timeout=30):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def checks(cfg):
    """(ok, what, how to fix) for each check, in the order they depend on each other."""
    out = [
        (
            sys.version_info >= (3, 11),
            f"Python {sys.version.split()[0]}",
            "Plus One needs Python 3.11 or newer.",
        )
    ]
    for tool, fix in (
        ("git", "Install git."),
        ("gh", "Install the GitHub CLI: https://cli.github.com"),
        ("claude", "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code"),
    ):
        out.append((bool(shutil.which(tool)), f"{tool} on PATH", fix))
    code, _ = _run("gh", "auth", "status")
    out.append((code == 0, "GitHub CLI signed in", "Run gh auth login."))
    if code == 0:
        try:
            login = json.loads(gh.run(["api", "user"]))["login"]
            out.append(
                (
                    login == cfg.me,
                    f"GitHub login is {cfg.me}",
                    f"gh is signed in as {login}; set me in "
                    f"{cfg.config_path} or switch with gh auth.",
                )
            )
        except Exception as exc:
            out.append((False, "GitHub answers", str(exc)[:200]))
    for r in cfg.repos:
        try:
            gh.run(["api", f"repos/{r.name}", "--jq", ".full_name"])
            out.append((True, f"{r.name} readable on GitHub", ""))
        except Exception:
            out.append(
                (
                    False,
                    f"{r.name} readable on GitHub",
                    f"Check the name and that {cfg.me} can see it.",
                )
            )
        code, origin = _run("git", "-C", r.checkout, "remote", "get-url", "origin")
        out.append(
            (
                code == 0 and matches(origin, r.name),
                f"{r.checkout} is a clone of {r.name}",
                f"Point checkout at a clone of {r.name} (gh repo clone {r.name}).",
            )
        )
    out.append(
        (
            cfg.rubric_path.exists(),
            f"rubric at {cfg.rubric_path}",
            f"Copy {config.STARTER_RUBRIC} there, or run bin/plus1 setup.",
        )
    )
    if cfg.draft_command:
        # A script removed after it was set fails every draft, which only shows on the cards.
        script = os.path.expanduser(cfg.draft_command)
        out.append(
            (
                os.path.isfile(script),
                f"drafting script at {script}",
                "Point draft_command at a script that exists, or pick Claude under "
                "Comments drafted by in Settings, Models.",
            )
        )
    out.append((units.active(), "checks run on a timer", "Run bin/plus1 install."))
    if cfg.web.mode == "local":
        out.append(
            (
                _web_up(cfg),
                f"web app answering at {cfg.web.public_url}",
                "Run bin/plus1 install, or bin/plus1-web to try it in the foreground.",
            )
        )
    out.append(_last_tick(cfg))
    out.append(_updates(cfg))
    return out


def _web_up(cfg):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{cfg.web.port}/", timeout=5)
    except urllib.error.HTTPError as exc:
        # Signed out is a 401, which still means it's up.
        return exc.code == 401
    except OSError:
        return False
    return True


def _updates(cfg):
    from .store import Store

    state = updates.view(cfg, Store(cfg))["state"]
    if cfg.updates.mode != "off" and state.get("status") == "blocked":
        return (
            False,
            f"updates are paused: {state['reason']}",
            "Clear that, or turn updates off in Settings, Updates.",
        )
    return (True, f"updates are {cfg.updates.mode} on {cfg.updates.channel}", "")


def _last_tick(cfg):
    from .store import Store

    store = Store(cfg)
    row = store.db.execute("SELECT MAX(started_at) FROM runs").fetchone()
    if not row or not row[0]:
        return (True, "no checks have run yet", "")
    last = datetime.fromisoformat(row[0])
    stale = datetime.now().astimezone() - last > timedelta(days=4)
    return (
        not stale,
        f"last check {last.strftime('%a %b %d %H:%M')}",
        "Nothing has run in four days: see the timer's log (journalctl --user -u plus1 on Linux, "
        f"{cfg.state_dir / 'logs' / 'tick.log'} on macOS).",
    )


def run(cfg, say=print):
    say(f"Plus One {__version__}")
    failed = 0
    for ok, what, fix in checks(cfg):
        say(f"{'ok  ' if ok else 'FAIL'} {what}" + ("" if ok or not fix else f"\n     {fix}"))
        failed += not ok
    say("All good." if not failed else f"{failed} thing{'s' if failed != 1 else ''} to fix.")
    return 1 if failed else 0


def update(say=print, how="now"):
    """Installs through plus1.updates. A config that doesn't load mustn't strand anyone on the version that broke it, so
    that case just pulls."""
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        if how != "now":
            say(f"Your config doesn't load, so nothing changed: {exc}")
            return 1
        say(
            f"Your config doesn't load, so this pulls {updates.BRANCH} without the usual checks: {exc}"
        )
        return _pull(say)
    return updates.run(cfg, how, say)


def _pull(say):
    code, text = _run("git", "-C", str(host.ROOT), "pull", "--ff-only", timeout=120)
    say(text)
    if code != 0:
        return 1
    code, text = _run(str(host.ROOT / "bin" / "ensure-venv"), timeout=600)
    if code != 0:
        say(text)
        return 1
    timer, web = units.installed(), units.installed(web=True)
    if timer:
        units.install(config.load(), web=web)
    elif web:
        units.restart_web()
    if timer or web:
        say("restarted the services on the new code")
    # This process still runs the code from before the pull, so the new version is read from disk.
    found = re.search(r'__version__ = "([^"]+)"', (host.ROOT / "plus1" / "__init__.py").read_text())
    say(f"now on Plus One {found.group(1) if found else 'an unknown version'}")
    return 0
