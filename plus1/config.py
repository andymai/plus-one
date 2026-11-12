import os
import re
import shlex
import tomllib
from pathlib import Path
from types import SimpleNamespace

from . import prs

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "plus1" / "defaults.toml"
STARTER_RUBRIC = ROOT / "plus1" / "starter_rubric.md"
LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
TEAM_NAME = LOGIN + r"/[A-Za-z0-9._-]+"
UPDATE_MODES = ("automatic", "ask", "off")
UPDATE_CHANNELS = ("releases", "main")
# Lists that personal config adds to and can't shorten, so no config can drop a default safety lane.
ADDITIVE = {
    "lanes": ("never_approve_owners", "never_approve_paths"),
    "filters": ("hold_labels", "blocking_markers"),
}


class ConfigError(RuntimeError):
    pass


def home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "plus1"


def default_path():
    return Path(os.environ.get("PLUS1_CONFIG") or home() / "config.toml")


def _ns(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in value.items()})
    return value


def _read(path):
    with open(path, "rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} isn't valid TOML: {exc}") from None


def _merge(base, over):
    out = dict(base)
    for key, value in over.items():
        out[key] = (
            _merge(out[key], value)
            if isinstance(value, dict) and isinstance(out.get(key), dict)
            else value
        )
    return out


def _union(*lists):
    return list(dict.fromkeys(item for items in lists for item in items or ()))


def system_zone():
    # /etc/localtime links into the zoneinfo tree on Linux and macOS alike.
    target = os.path.realpath("/etc/localtime")
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"


def _repo(entry, defaults, personal):
    name = entry.get("name", "")
    if name.count("/") != 1:
        raise ConfigError(f"[[repos]] name must look like owner/repo, not {name!r}")
    sections = {}
    for section, keys in ADDITIVE.items():
        # Defaults, then the top of personal config, then the repo's own entry. The lists union; anything else overrides.
        layers = (defaults.get(section, {}), personal.get(section, {}), entry.get(section, {}))
        merged = {k: v for layer in layers for k, v in layer.items() if k not in keys}
        sections[section] = SimpleNamespace(
            **merged, **{k: _union(*(layer.get(k) for layer in layers)) for k in keys}
        )
    checks = entry.get("checks", [])
    if not isinstance(checks, list):
        raise ConfigError(
            f'{name}: checks is a list of command lines, like ["bin/packwerk check {{files}}"]'
        )
    for c in checks:
        try:
            ok = isinstance(c, str) and bool(shlex.split(c))
        except ValueError:
            ok = False
        if not ok:
            raise ConfigError(
                f"{name}: each of checks is one command line, like 'bin/packwerk check {{files}}', not {c!r}"
            )
    auto_approve = entry.get("auto_approve", False)
    if not isinstance(auto_approve, bool):
        raise ConfigError(f"{name}: auto_approve is true or false, not {auto_approve!r}")
    hold_yours = entry.get("hold_yours", True)
    if not isinstance(hold_yours, bool):
        raise ConfigError(f"{name}: hold_yours is true or false, not {hold_yours!r}")
    return SimpleNamespace(
        name=name,
        checkout=os.path.expanduser(entry.get("checkout", "")),
        peers=[os.path.expanduser(p) for p in entry.get("peers", [])],
        issues=entry.get("issues") or personal.get("issues", ""),
        checks=list(checks),
        auto_approve=auto_approve,
        hold_yours=hold_yours,
        lanes=sections["lanes"],
        filters=sections["filters"],
    )


def auto_repos(cfg):
    """The repos whose [[repos]] entry lets a timer tick approve a clean PR on its own. Each repo turns it on by itself,
    since trust earned on one repo doesn't carry to another."""
    return {r.name for r in cfg.repos if r.auto_approve}


def holds_yours(cfg, name):
    """Whether the repo's automatic approvals wait for the owner on a PR in their own code (plus1.yours.hold)."""
    return next((r.hold_yours for r in cfg.repos if r.name == name), True)


def repo(cfg, name=None):
    return next((r for r in cfg.repos if r.name == name), cfg.repos[0])


def at(cfg, pr_id):
    """The repo view and GitHub number for a stored PR id."""
    name, number = prs.split(pr_id)
    return view(cfg, name), number


def view(cfg, name):
    """cfg as seen from one repo: cfg.repo, checkout, peers, issues, lanes, and filters are that repo's."""
    # The top-level fields already mirror the first repo.
    if name == getattr(cfg, "repo", None):
        return cfg
    r = repo(cfg, name)
    if r.name != name:
        raise ConfigError(f"{name} isn't one of the repos in {cfg.config_path}")
    out = SimpleNamespace(**vars(cfg))
    out.repo, out.checkout, out.peers, out.issues, out.lanes, out.filters = (
        r.name,
        r.checkout,
        r.peers,
        r.issues,
        r.lanes,
        r.filters,
    )
    return out


def rubric_text(cfg):
    # No fallback: a rubric that went missing must stop the reviewer, not quietly swap in someone else's rules.
    try:
        return cfg.rubric_path.read_text()
    except FileNotFoundError:
        raise ConfigError(
            f"No rubric at {cfg.rubric_path}. Copy {STARTER_RUBRIC} there to start from the generic one."
        ) from None


def load(path=None):
    path = Path(path or default_path())
    if not path.exists():
        raise ConfigError(
            f"No config at {path}. Copy {ROOT / 'config.example.toml'} there and fill it in."
        )
    defaults, personal = _read(DEFAULTS), _read(path)
    if not personal.get("me"):
        raise ConfigError(f'{path} needs me = "<your GitHub login>"')
    if not personal.get("repos"):
        raise ConfigError(f"{path} needs at least one [[repos]] entry")
    repos = [_repo(entry, defaults, personal) for entry in personal["repos"]]
    names = [r.name for r in repos]
    if len(set(names)) != len(names):
        raise ConfigError(f"{path} lists the same repo twice in [[repos]]")
    merged = _merge(
        defaults, {k: v for k, v in personal.items() if k not in ADDITIVE and k != "repos"}
    )
    cfg = _ns(merged)
    cfg.repos = repos
    cfg.name = cfg.name if getattr(cfg, "name", "") else cfg.me
    cfg.timezone = getattr(cfg, "timezone", "") or system_zone()
    if not isinstance(cfg.emails, list) or not all(
        isinstance(e, str) and "@" in e for e in cfg.emails
    ):
        raise ConfigError(
            f'emails is a list of addresses, like ["you@example.com"], not {cfg.emails!r}'
        )
    for key, pattern, like in (
        ("teams", TEAM_NAME, "your-org/your-team"),
        ("people", LOGIN, "a-login"),
        ("others", TEAM_NAME, "your-org/other-team"),
    ):
        entries = getattr(getattr(cfg, "team", None), key, None)
        if not isinstance(entries, list) or not all(
            isinstance(e, str) and re.fullmatch(pattern, e) for e in entries
        ):
            raise ConfigError(f'team.{key} is a list like ["{like}"], not {entries!r}')
    if cfg.updates.mode not in UPDATE_MODES:
        raise ConfigError(f"updates.mode is one of {UPDATE_MODES}, not {cfg.updates.mode!r}")
    if cfg.updates.channel not in UPDATE_CHANNELS:
        raise ConfigError(
            f"updates.channel is one of {UPDATE_CHANNELS}, not {cfg.updates.channel!r}"
        )
    cfg.state_dir = Path(os.path.expanduser(cfg.state_dir))
    cfg.web.socket = (
        os.path.expanduser(cfg.web.socket) if cfg.web.socket else str(cfg.state_dir / "web.sock")
    )
    # Tailscale serves it to the owner's other devices; otherwise it stays on this machine's loopback.
    cfg.web.mode = cfg.web.mode or ("tailscale" if cfg.web.allowed_login else "local")
    if cfg.web.mode not in ("local", "tailscale"):
        raise ConfigError(f"web.mode is local or tailscale, not {cfg.web.mode!r}")
    if cfg.web.mode == "tailscale" and not cfg.web.allowed_login:
        raise ConfigError(
            'web.mode = "tailscale" needs web.allowed_login, the tailnet login allowed in'
        )
    cfg.web.public_url = cfg.web.public_url or (
        f"http://127.0.0.1:{cfg.web.port}" if cfg.web.mode == "local" else ""
    )
    cfg.config_path = path
    # Beside the config, so it's the owner's alone and a git pull never touches it.
    cfg.rubric_path = path.parent / "rubric.md"
    if not cfg.rubric_path.exists():
        rubric_text(cfg)
    first = repos[0]
    cfg.repo, cfg.checkout, cfg.peers, cfg.issues = (
        first.name,
        first.checkout,
        first.peers,
        first.issues,
    )
    cfg.lanes, cfg.filters = first.lanes, first.filters
    return cfg
