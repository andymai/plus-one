"""What differs between Linux and macOS: background jobs, desktop notifications, and the PATH services run with."""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MAC = sys.platform == "darwin"
ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("claude", "gh", "git", "uv", "terminal-notifier", "notify-send")
# Carried into every background job, so a job started from a sandbox or a second config reads the same one.
CARRIED = ("PLUS1_CONFIG", "XDG_CONFIG_HOME")

NOTIFY_SCRIPT = (
    'choice=$(notify-send -a "Plus One" -t 0 --action=open=Open -- "$1" "$2"); '
    'if [ "$choice" = open ]; then xdg-open "$3"; fi'
)
# Arguments reach AppleScript as data through argv, never spliced into the script.
OSASCRIPT = (
    "on run argv",
    'display notification (item 2 of argv) with title "Plus One" subtitle (item 1 of argv)',
    "end run",
)


def service_path():
    """PATH for work started outside a login shell (systemd, launchd, a background job), so it finds the same tools."""
    home = Path.home()
    found = [os.path.dirname(p) for p in map(shutil.which, TOOLS) if p]
    common = [
        "/opt/homebrew/bin",
        "/home/linuxbrew/.linuxbrew/bin",
        f"{home}/.local/bin",
        f"{home}/.local/share/npm/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    return ":".join(d for d in dict.fromkeys(found + common) if os.path.isdir(d))


def service_env():
    return {
        "PATH": service_path(),
        **{k: os.path.abspath(os.environ[k]) for k in CARRIED if os.environ.get(k)},
    }


def systemd():
    return not MAC and bool(shutil.which("systemd-run")) and bool(shutil.which("systemctl"))


def spawn(state_dir, name, args):
    """Runs bin/plus1 with args in the background, outliving whatever started it. Returns the job's name."""
    job = f"plus1-{name}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    argv = [str(ROOT / "bin" / "plus1"), *map(str, args)]
    if systemd():
        env = [f"--setenv={k}={v}" for k, v in service_env().items()]
        try:
            proc = subprocess.run(
                ["systemd-run", "--user", "--collect", f"--unit={job}", *env, *argv],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            return job
        # Installed but with no user session to talk to (a container, a login without lingering): run it directly.
    logs = Path(state_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / f"{job}.log", "ab") as log:
        subprocess.Popen(
            argv,
            cwd=ROOT,
            env={**os.environ, **service_env()},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return job


def open_url(url):
    opener = "open" if MAC else shutil.which("xdg-open")
    if opener:
        subprocess.Popen(
            [opener, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return bool(opener)


def notify(title, body, url):
    if MAC:
        if shutil.which("terminal-notifier"):
            cmd = [
                "terminal-notifier",
                "-title",
                "Plus One",
                "-subtitle",
                title,
                "-message",
                body,
                "-open",
                url,
            ]
        else:
            cmd = ["osascript", *(part for line in OSASCRIPT for part in ("-e", line)), title, body]
    elif shutil.which("notify-send"):
        cmd = ["bash", "-c", NOTIFY_SCRIPT, "notify", title, body, url]
        if systemd():
            # In its own unit, so waiting for a click on Open doesn't hold up the caller or die with it.
            subprocess.run(
                ["systemd-run", "--user", "--collect", "--quiet", "--", *cmd],
                capture_output=True,
                timeout=10,
            )
            return
    else:
        return
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
