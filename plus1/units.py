"""Background services for this checkout: systemd user units on Linux, launchd agents on macOS."""

import os
import plistlib
import subprocess
from pathlib import Path

from . import host

TICK, WEB = "local.plus1.tick", "local.plus1.web"


def _quote(value):
    # systemd expands % specifiers even inside quotes.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd(web=True):
    """File name to contents for the units. The timer fires every half hour; the tick itself keeps to the window."""
    env = "".join(f"Environment={_quote(f'{k}={v}')}\n" for k, v in host.service_env().items())
    plus1, plus1_web = host.ROOT / "bin" / "plus1", host.ROOT / "bin" / "plus1-web"
    units = {
        "plus1.service": f"""[Unit]
Description=Plus One: review the PRs waiting on you

[Service]
Type=oneshot
ExecStart={_quote(plus1)} tick --trigger timer
TimeoutStartSec=50min
Nice=10
{env}""",
        "plus1.timer": """[Unit]
Description=Plus One: check for PRs every 30 minutes (each check keeps to your working hours)

[Timer]
OnCalendar=*-*-* *:00,30:00
Persistent=false

[Install]
WantedBy=timers.target
""",
    }
    if web:
        units["plus1-web.service"] = f"""[Unit]
Description=Plus One web app

[Service]
Type=simple
ExecStart={_quote(plus1_web)}
Restart=on-failure
RestartSec=5
{env}
[Install]
WantedBy=default.target
"""
    return units


def launchd(state_dir, web=True):
    """File name to contents for the agents, which log under the state dir since launchd has no journal."""
    logs = Path(state_dir) / "logs"
    common = {"EnvironmentVariables": host.service_env(), "WorkingDirectory": str(host.ROOT)}
    agents = {
        TICK: {
            "Label": TICK,
            "ProgramArguments": [str(host.ROOT / "bin" / "plus1"), "tick", "--trigger", "timer"],
            "StartCalendarInterval": [{"Minute": 0}, {"Minute": 30}],
            "ProcessType": "Background",
            "Nice": 10,
            "StandardOutPath": str(logs / "tick.log"),
            "StandardErrorPath": str(logs / "tick.log"),
            **common,
        }
    }
    if web:
        agents[WEB] = {
            "Label": WEB,
            "ProgramArguments": [str(host.ROOT / "bin" / "plus1-web")],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "web.log"),
            "StandardErrorPath": str(logs / "web.log"),
            **common,
        }
    return {f"{label}.plist": plistlib.dumps(agent).decode() for label, agent in agents.items()}


def target():
    if host.MAC:
        return Path.home() / "Library" / "LaunchAgents"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def files(cfg, web=True):
    return launchd(cfg.state_dir, web) if host.MAC else systemd(web)


def _run(*cmd, check=True):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {proc.stderr.strip()[:300]}")
    return proc


def write(directory, contents):
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in contents.items():
        path = directory / name
        # Earlier installs linked these into the checkout; write a real file in the link's place.
        if path.is_symlink():
            path.unlink()
        path.write_text(text)
        paths.append(path)
    return paths


def install(cfg, web=True):
    """Writes the units for this checkout and starts them. Returns the paths written."""
    if not web:
        # A timer-only install takes down the web app an earlier install left running.
        _remove(WEB if host.MAC else "plus1-web.service")
    if host.MAC:
        (Path(cfg.state_dir) / "logs").mkdir(parents=True, exist_ok=True)
        paths = write(target(), files(cfg, web))
        domain = f"gui/{os.getuid()}"
        for path in paths:
            # Loading replaces nothing, so take down whatever an earlier install left running first.
            _run("launchctl", "bootout", f"{domain}/{path.stem}", check=False)
            _run("launchctl", "bootstrap", domain, str(path))
        return paths
    if not host.systemd():
        raise RuntimeError(
            "No systemd user session here, so run bin/plus1 tick from cron or another scheduler instead."
        )
    paths = write(target(), files(cfg, web))
    _run("systemctl", "--user", "daemon-reload")
    _run("systemctl", "--user", "enable", "--now", "plus1.timer")
    if web:
        _run("systemctl", "--user", "enable", "plus1-web.service")
        # Restart, not start, so a running web app picks up the code it was installed from.
        _run("systemctl", "--user", "restart", "plus1-web.service")
    return paths


def _remove(unit):
    if host.MAC:
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{unit}", check=False)
        path = target() / f"{unit}.plist"
    else:
        if host.systemd():
            _run("systemctl", "--user", "disable", "--now", unit, check=False)
        path = target() / unit
    if path.exists() or path.is_symlink():
        path.unlink()
        return path
    return None


def active():
    """Whether the timer is loaded and will fire, not just installed."""
    if host.MAC:
        return _run("launchctl", "print", f"gui/{os.getuid()}/{TICK}", check=False).returncode == 0
    return (
        host.systemd()
        and _run("systemctl", "--user", "is-active", "plus1.timer", check=False).returncode == 0
    )


def installed(web=False):
    """Whether install set up the timer here (or, with web, the web app)."""
    if host.MAC:
        return (target() / f"{WEB if web else TICK}.plist").exists()
    unit = "plus1-web.service" if web else "plus1.timer"
    if not (target() / unit).exists():
        return False
    return _run("systemctl", "--user", "is-enabled", unit, check=False).returncode == 0


def restart_web():
    if host.MAC:
        _run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{WEB}")
    else:
        _run("systemctl", "--user", "restart", "plus1-web.service")


def uninstall():
    units = (TICK, WEB) if host.MAC else ("plus1.timer", "plus1.service", "plus1-web.service")
    removed = [path for path in map(_remove, units) if path]
    if not host.MAC and host.systemd():
        _run("systemctl", "--user", "daemon-reload", check=False)
    return removed
