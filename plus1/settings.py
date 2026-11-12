"""The settings page's edits to the owner's config.toml, made in place so their comments and layout survive."""

import math
import os
import re
import shlex
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, learn

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODELS = ("haiku", "sonnet", "opus")
# Each model role, as the page names it and what it does.
MODEL_ROLES = {
    "triage": ("Triage", "Decides whether a PR needs a deep look"),
    "deep": ("Deep review", "Reads the code and finds problems"),
    "verify": ("Double-check", "Tries to disprove each finding"),
    "draft": ("Drafting", "Writes the comments"),
    "factcheck": ("Fact-check", "Checks each comment against its finding"),
    "study": ("Study", "Reads your reviews for Learn again"),
    "learn": ("Learn", "Writes your rubric from those notes"),
}
MODEL_FIELDS = {
    "model": "model",
    "effort": "effort",
    "budget_usd": "budget",
    "timeout_s": "time limit",
}
# Raising one of these lets Plus One do more on its own, so the page asks for a typed confirmation first. A model
# role's budget counts too (see loosens).
CAPS = (
    ("limits", "max_spend_per_day_usd"),
    ("limits", "max_approvals_per_tick"),
    ("limits", "max_approvals_per_day"),
    ("limits", "max_diff_lines"),
)
# Taking an entry out of one of these lets Plus One approve or review something it held back before.
GUARDS = (
    ("lanes", "never_approve_owners"),
    ("lanes", "never_approve_paths"),
    ("filters", "hold_labels"),
    ("filters", "blocking_markers"),
)
# Adding a pattern here means a failing check with that name no longer holds a PR back.
GATES = ("filters", "approval_gates")
# Turning this on lets a scheduled check approve a clean PR in the repo without the owner.
AUTO = ("", "auto_approve")
# Turning this off lets a scheduled check approve a PR in the owner's own code (plus1.yours) without them.
HOLD = ("", "hold_yours")
UPDATE_MODE = ("updates", "mode")
UPDATE_CHANNEL = ("updates", "channel")
MODE_WORDS = {"automatic": "Automatic", "ask": "Ask first", "off": "Off"}
CONFIRM = "loosen"
# Changing one of these changes what runs on this machine: a repo's check commands (scope is the repo), the script
# that drafts comments, or the Claude Code login every model call uses.
RUNS = (("", "checks"), ("", "draft_command"), ("", "claude_config_dir"))
RUN = "run"
# These take effect when the web app restarts, and a wrong value in the first three can keep the owner out of it.
RESTART = (
    ("web", "mode"),
    ("web", "allowed_login"),
    ("web", "socket"),
    ("web", "port"),
    ("web", "public_url"),
    ("", "state_dir"),
)
RETYPE = (("web", "mode"), ("web", "allowed_login"), ("web", "socket"))
# Paths compare by where they point, so ~/x and /home/you/x are the same value.
PATHS = (
    ("web", "socket"),
    ("", "state_dir"),
    ("", "draft_command"),
    ("", "claude_config_dir"),
    ("slack", "token_path"),
    ("", "checkout"),
    ("", "peers"),
)
# Held from reading config.toml to taking the result on, so two saves can't interleave or land out of order.
LOCK = threading.RLock()

LABELS = {
    ("window", "days"): "Days",
    ("window", "start_hour"): "Start",
    ("window", "end_hour"): "End",
    ("window", "jitter_minutes"): "Random delay",
    ("", "timezone"): "Time zone",
    ("", "notify"): "Desktop notifications",
    ("", "name"): "Name",
    ("", "me"): "GitHub login",
    ("slack", "enabled"): "Slack",
    ("slack", "channels"): "Slack channels",
    ("slack", "lookback_hours"): "Slack look back",
    ("slack", "token_path"): "Slack token files",
    ("slack", "token_key"): "Slack token key",
    ("slack", "keychain_service"): "Keychain item",
    ("limits", "max_spend_per_day_usd"): "Daily spend cap",
    ("limits", "max_reviews_per_tick"): "Reviews per check",
    ("limits", "max_approvals_per_tick"): "Approvals per check",
    ("limits", "max_approvals_per_day"): "Approvals per day",
    ("limits", "max_diff_lines"): "Largest PR it can approve",
    ("limits", "max_map_lines"): "Skip PRs larger than",
    ("limits", "min_ready_minutes"): "Wait after a PR is ready",
    ("limits", "handoff_working_days"): "Hand-offs come back after",
    ("limits", "incremental_max_lines"): "Re-review only the new change under",
    ("limits", "auto_proposals_per_day"): "Proposals a day from GitHub",
    AUTO: "Approve on its own",
    HOLD: "Hold PRs in your code",
    ("", "emails"): "Commit emails",
    ("team", "teams"): "Your teams",
    ("team", "people"): "People",
    ("team", "others"): "Other teams",
    ("updates", "mode"): "When a new version is out",
    ("updates", "channel"): "Follow",
    ("lanes", "never_approve_owners"): "Never-approve teams",
    ("lanes", "never_approve_paths"): "Never-approve paths",
    ("filters", "hold_labels"): "Skip labels",
    ("filters", "blocking_markers"): "Blocking markers",
    GATES: "Approval-gate checks",
    ("", "checks"): "Checks before each review",
    ("", "checkout"): "Local clone",
    ("", "peers"): "Peers",
    ("", "issues"): "Issue tracker",
    ("", "repos"): "Repos",
    ("", "draft_command"): "Drafting script",
    ("", "claude_config_dir"): "Claude login folder",
    ("limits", "check_timeout_s"): "Check time limit",
    ("web", "mode"): "Served",
    ("web", "allowed_login"): "Allowed login",
    ("web", "socket"): "Socket",
    ("web", "port"): "Port",
    ("web", "public_url"): "Address",
    ("", "state_dir"): "History folder",
}


def label(table, key):
    """How the page names a setting."""
    if table.startswith("models."):
        role = table.removeprefix("models.")
        return f"{MODEL_ROLES.get(role, (role.capitalize(),))[0]} {MODEL_FIELDS.get(key, key)}"
    return LABELS.get((table, key)) or key.replace("_", " ").capitalize()


class SettingsError(ValueError):
    def __init__(self, message, field=""):
        super().__init__(message)
        self.field = field


@dataclass
class Loosening:
    """One change that lets Plus One do more on its own: a value raised or turned on, or entries taken out."""

    label: str
    text: str
    was: str = ""
    now: str = ""
    removed: list = field(default_factory=list)
    added: list = field(default_factory=list)


@dataclass
class Run:
    """A command line that would run on this machine, split the way it will run, with where and when."""

    label: str
    argv: list
    note: str


@dataclass
class Retype:
    """A value that can lock the owner out of the web app, which the form must carry twice."""

    label: str
    name: str
    value: str


class NeedsConfirm(SettingsError):
    def __init__(self, message, items=(), runs=(), retypes=()):
        super().__init__(message)
        self.items, self.runs, self.retypes = list(items), list(runs), list(retypes)


def _int(text, low, high, what, name):
    text = str(text).strip()
    if not re.fullmatch(r"\d+", text) or not low <= int(text) <= high:
        raise SettingsError(f"{what} is a whole number from {low} to {high}.", name)
    return int(text)


def _money(text, what, name):
    try:
        value = round(float(str(text).strip()), 2)
    except ValueError:
        raise SettingsError(f"{what} is an amount in dollars, like 40.", name) from None
    if not math.isfinite(value) or value < 0:
        raise SettingsError(f"{what} is an amount in dollars, like 40.", name)
    return value


def _lines(text):
    return list(dict.fromkeys(line.strip() for line in str(text).splitlines() if line.strip()))


def _zone(text):
    try:
        ZoneInfo(text.strip())
    except (ValueError, ZoneInfoNotFoundError):
        raise SettingsError(
            f"{text.strip()!r} isn't a time zone name like America/New_York.", "timezone"
        ) from None
    return text.strip()


def _patterns(lines, name):
    for line in lines:
        try:
            re.compile(line)
        except re.error as exc:
            raise SettingsError(f"{line!r} isn't a valid pattern: {exc}.", name) from None
    return lines


def _text(text, what, name, pattern=None, required=False):
    text = str(text).strip()
    if required and not text:
        raise SettingsError(f"{what} can't be empty.", name)
    if text and pattern and not re.fullmatch(pattern, text):
        raise SettingsError(f"{what} doesn't look right: {text!r}.", name)
    return text


def _token_paths(text):
    """One token file as a string, as config has always held it, or several as a list."""
    paths = _lines(text)
    return paths[0] if len(paths) == 1 else paths if paths else ""


def _commands(lines, name):
    for line in lines:
        try:
            ok = bool(shlex.split(line))
        except ValueError:
            ok = False
        if not ok:
            raise SettingsError(f"{line!r} isn't one command line.", name)
    return lines


def _clone(path, repo, name):
    """A clone of repo: a git checkout whose origin is that repo on GitHub, so the reviews it serves are of that repo."""
    from . import setup

    path = str(path).strip()
    where = os.path.expanduser(path)
    if not path or not os.path.exists(os.path.join(where, ".git")):
        raise SettingsError(f"There's no git clone at {path or 'that path'}.", name)
    origin = setup._origin(where)
    if not setup.matches(origin, repo):
        raise SettingsError(f"That clone's origin is {origin or 'missing'}, not {repo}.", name)
    return path


def scoped(key, repo):
    """A per-repo field's name, by the repo rather than its position, so a repo added meanwhile can't shift it."""
    return f"{key}@{repo}"


def _lists(form, cfg, key, table="filters", patterns=False):
    """A list that can be set for every repo (its plain field) and for each repo (its scoped field). Only the fields
    the form carries change: a repo added by hand after the page loaded keeps its own."""
    out = {}
    if key in form:
        out[(None, table, key)] = _lines(form[key])
    for r in cfg.repos:
        if scoped(key, r.name) in form:
            out[(r.name, table, key)] = _lines(form[scoped(key, r.name)])
    if patterns:
        for name, value in out.items():
            _patterns(value, key if name[0] is None else scoped(key, name[0]))
    return out


def parse(section, form, cfg, repo=None):
    """The changes a section's form asks for, as {(scope, table, key): value} where scope is None for the top of the
    config or a repo's name for its [[repos]] entry, checked but not yet saved."""
    if section == "schedule":
        days = [i for i, name in enumerate(DAYS) if form.get(f"day_{name}")]
        if not days:
            raise SettingsError("Pick at least one day.", "days")
        start = _int(form.get("start_hour", ""), 0, 23, "The start", "start_hour")
        # The page asks when checks stop, which is an hour after the last hour they run in.
        end = _int(form.get("end", ""), 1, 24, "The end", "end")
        if end <= start:
            raise SettingsError("The end has to come after the start.", "end")
        return {
            (None, "window", "days"): days,
            (None, "window", "start_hour"): start,
            (None, "window", "end_hour"): end - 1,
            (None, "window", "jitter_minutes"): _int(
                form.get("jitter_minutes", ""), 0, 29, "The random delay", "jitter_minutes"
            ),
            (None, "", "timezone"): _zone(form.get("timezone", "")),
        }
    if section == "budget":
        return {
            (None, "limits", "max_spend_per_day_usd"): _money(
                form.get("max_spend_per_day_usd", ""),
                "The daily spend cap",
                "max_spend_per_day_usd",
            ),
            (None, "limits", "max_reviews_per_tick"): _int(
                form.get("max_reviews_per_tick", ""),
                1,
                20,
                "Reviews per check",
                "max_reviews_per_tick",
            ),
        }
    if section == "limits":
        return {
            (None, "limits", "max_approvals_per_tick"): _int(
                form.get("max_approvals_per_tick", ""),
                0,
                50,
                "Approvals per check",
                "max_approvals_per_tick",
            ),
            (None, "limits", "max_approvals_per_day"): _int(
                form.get("max_approvals_per_day", ""),
                0,
                500,
                "Approvals per day",
                "max_approvals_per_day",
            ),
            (None, "limits", "max_diff_lines"): _int(
                form.get("max_diff_lines", ""),
                100,
                20000,
                "The largest PR it can approve",
                "max_diff_lines",
            ),
        }
    if section == "notifications":
        return {(None, "", "notify"): bool(form.get("notify"))}
    if section == "sources":
        channels = {}
        for line in _lines(form.get("channels", "")):
            name, _, channel = line.partition("=")
            if not name.strip():
                raise SettingsError(f"Give channel {channel.strip()} a name.", "channels")
            if not re.fullmatch(r"[A-Z0-9]{8,12}", channel.strip()):
                raise SettingsError(f"#{name.strip()} needs an ID like C0123456789.", "channels")
            channels[name.strip()] = channel.strip()
        return {
            (None, "slack", "enabled"): bool(form.get("enabled")),
            (None, "slack", "channels"): channels,
            (None, "slack", "lookback_hours"): _int(
                form.get("lookback_hours", ""), 1, 336, "How far back it reads", "lookback_hours"
            ),
            (None, "slack", "token_path"): _token_paths(form.get("token_path", "")),
            (None, "slack", "token_key"): _text(
                form.get("token_key", ""), "The token key", "token_key"
            ),
            (None, "slack", "keychain_service"): _text(
                form.get("keychain_service", ""), "The Keychain item", "keychain_service"
            ),
        }
    if section == "skips":
        found: dict[tuple, Any] = {
            (None, "limits", "min_ready_minutes"): _int(
                form.get("min_ready_minutes", ""), 0, 1440, "The wait", "min_ready_minutes"
            ),
            (None, "limits", "handoff_working_days"): _int(
                form.get("handoff_working_days", ""),
                1,
                30,
                "Hand-offs come back after",
                "handoff_working_days",
            ),
            (None, "limits", "max_map_lines"): _int(
                form.get("max_map_lines", ""), 500, 100000, "The skip size", "max_map_lines"
            ),
            **_lists(form, cfg, "hold_labels"),
            **_lists(form, cfg, "blocking_markers"),
        }
        if "approval_gates" in form:
            found[(None, *GATES)] = _patterns(_lines(form["approval_gates"]), "approval_gates")
        return found
    if section == "every":
        return {
            (None, "lanes", "never_approve_owners"): [
                o.lstrip("@").split("/")[-1] for o in _lines(form.get("owners", ""))
            ],
            (None, "lanes", "never_approve_paths"): _patterns(
                _lines(form.get("paths", "")), "paths"
            ),
        }
    if section == "repo":
        out = {
            (repo, *AUTO): bool(form.get("auto_approve")),
            (repo, "lanes", "never_approve_owners"): [
                o.lstrip("@").split("/")[-1] for o in _lines(form.get("owners", ""))
            ],
            (repo, "lanes", "never_approve_paths"): _patterns(
                _lines(form.get("paths", "")), "paths"
            ),
        }
        # An unchecked switch sends nothing, so the form says it carried this one.
        if form.get("hold_shown"):
            out[(repo, *HOLD)] = bool(form.get("hold_yours"))
        return out
    if section == "rubric":
        return {
            (None, "limits", "auto_proposals_per_day"): _int(
                form.get("auto_proposals_per_day", ""),
                0,
                20,
                "Proposals a day",
                "auto_proposals_per_day",
            )
        }
    if section == "models":
        out: dict[tuple, Any] = {
            (None, "limits", "incremental_max_lines"): _int(
                form.get("incremental_max_lines", ""),
                0,
                2000,
                "The re-review size",
                "incremental_max_lines",
            )
        }
        if "draft_mode" in form:
            script = ""
            if form["draft_mode"] == "script":
                script = _text(
                    form.get("draft_command", ""), "The drafting script", "draft_command"
                )
                if not os.path.isfile(os.path.expanduser(script)):
                    raise SettingsError(
                        f"There's no script at {script or 'that path'}.", "draft_command"
                    )
            out[(None, "", "draft_command")] = script
        if "claude_config_dir" in form:
            folder = _text(
                form["claude_config_dir"], "The Claude login folder", "claude_config_dir"
            )
            if folder and not os.path.isdir(os.path.expanduser(folder)):
                raise SettingsError(f"There's no folder at {folder}.", "claude_config_dir")
            out[(None, "", "claude_config_dir")] = folder
        for role in vars(cfg.models):
            name = MODEL_ROLES.get(role, (role.capitalize(),))[0]
            table = f"models.{role}"
            out[(None, table, "model")] = _text(
                form.get(f"{role}.model", ""),
                f"{name}'s model",
                f"{role}.model",
                r"[A-Za-z0-9][A-Za-z0-9._\[\]-]*",
                required=True,
            )
            effort = form.get(f"{role}.effort", "")
            if effort not in EFFORTS:
                raise SettingsError(
                    f"{name}'s effort is one of {', '.join(EFFORTS)}.", f"{role}.effort"
                )
            out[(None, table, "effort")] = effort
            budget = _money(
                form.get(f"{role}.budget_usd", ""), f"{name}'s budget", f"{role}.budget_usd"
            )
            if budget <= 0:
                raise SettingsError(
                    f"{name}'s budget has to be more than $0.", f"{role}.budget_usd"
                )
            out[(None, table, "budget_usd")] = budget
            out[(None, table, "timeout_s")] = 60 * _int(
                form.get(f"{role}.timeout_s", ""),
                1,
                120,
                f"{name}'s time limit in minutes",
                f"{role}.timeout_s",
            )
        return out
    if section == "repos":
        setup: dict[tuple, Any] = {
            (None, "limits", "check_timeout_s"): _int(
                form.get("check_timeout_s", ""), 10, 3600, "The check time limit", "check_timeout_s"
            )
        }
        for r in cfg.repos:
            field = scoped("checks", r.name)
            if field in form:
                setup[(r.name, "", "checks")] = _commands(_lines(form[field]), field)
            field = scoped("checkout", r.name)
            if field in form:
                setup[(r.name, "", "checkout")] = _clone(form[field], r.name, field)
            field = scoped("peers", r.name)
            if field in form:
                for peer in (peers := _lines(form[field])):
                    if not os.path.isdir(os.path.expanduser(peer)):
                        raise SettingsError(f"There's no folder at {peer}.", field)
                setup[(r.name, "", "peers")] = peers
            field = scoped("issues", r.name)
            if field in form:
                setup[(r.name, "", "issues")] = _text(
                    form[field], "The issue tracker", field, r"https?://\S+"
                )
        return setup
    if section == "web":
        mode = form.get("mode", "")
        if mode not in ("local", "tailscale"):
            raise SettingsError("Serve it on this computer or over Tailscale.", "mode")
        login = _text(
            form.get("allowed_login", ""), "The allowed login", "allowed_login", r"\S+@\S+"
        )
        if mode == "tailscale" and not login:
            raise SettingsError(
                "Over Tailscale needs the login allowed in, like you@github.", "allowed_login"
            )
        return {
            (None, "web", "mode"): mode,
            (None, "web", "allowed_login"): login,
            (None, "web", "socket"): _text(form.get("socket", ""), "The socket", "socket"),
            (None, "web", "port"): _int(form.get("port", ""), 1024, 65535, "The port", "port"),
            (None, "web", "public_url"): _text(
                form.get("public_url", ""), "The address", "public_url", r"https?://\S+"
            ),
        }
    if section == "files":
        return {
            (None, "", "state_dir"): _text(
                form.get("state_dir", ""), "The history folder", "state_dir", required=True
            )
        }
    if section == "you":
        out = {
            (None, "", "name"): _text(form.get("name", ""), "Your name", "name", required=True),
            (None, "", "me"): _text(
                form.get("me", ""),
                "Your GitHub login",
                "me",
                config.LOGIN,
                required=True,
            ),
        }
        if "emails" in form:
            emails = [e.lower() for e in _lines(form["emails"])]
            for e in emails:
                _text(e, "That email", "emails", r"[^@\s]+@[^@\s]+\.[^@\s]+")
            out[(None, "", "emails")] = emails
        for key, what, pattern in (
            ("teams", "That team", config.TEAM_NAME),
            ("people", "That login", config.LOGIN),
            ("others", "That team", config.TEAM_NAME),
        ):
            if key in form:
                entries = list(dict.fromkeys(e.lower() for e in _lines(form[key])))
                for e in entries:
                    _text(e, what, key, pattern)
                out[(None, "team", key)] = entries
        return out
    if section == "updates":
        out = {}
        for key, allowed in (("mode", config.UPDATE_MODES), ("channel", config.UPDATE_CHANNELS)):
            if form.get(key) not in allowed:
                raise SettingsError("Pick one of the choices.", key)
            out[(None, "updates", key)] = form[key]
        return out
    raise SettingsError("Unknown section.")


def _table(doc, name, repo=None, create=True) -> Any:
    """The TOML table a key lives in: the top level, a top-level table (models.deep is a table inside models), or a
    table inside one [[repos]] entry. None when it doesn't exist and create is False."""
    import tomlkit

    parent = doc
    if repo is not None:
        parent = next((r for r in doc.get("repos", []) if r.get("name") == repo), None)
        if parent is None:
            raise SettingsError(f"{repo} isn't in your config.")
    parts = [p for p in name.split(".") if p]
    for i, part in enumerate(parts):
        if part not in parent:
            if not create:
                return None
            # A table that only holds tables (models above models.deep) gets no header of its own.
            parent[part] = tomlkit.table(is_super_table=i < len(parts) - 1)
        parent = parent[part]
    return parent


def _value(value):
    import tomlkit

    if isinstance(value, dict):
        table = tomlkit.table()
        for k, v in value.items():
            table[k] = v
        return table
    if isinstance(value, list) and len(value) > 3:
        items = tomlkit.array()
        items.extend(value)
        return items.multiline(True)
    return value


def key_name(scope, table, key):
    """How a change is named in the settings log: window.days, notify, or owner/repo:lanes.never_approve_paths."""
    name = f"{table}.{key}" if table else key
    return f"{scope}:{name}" if scope else name


def show(key, value):
    """A setting's value as the page words it."""
    if isinstance(value, bool):
        return "On" if value else "Off"
    if key in ("max_spend_per_day_usd", "budget_usd"):
        return f"${value:,.2f}".replace(".00", "")
    if isinstance(value, int) and value >= 1000:
        return f"{value:,}"
    return str(value)


MISSING = object()


def _lookup(tree, table, key) -> Any:
    for part in [p for p in table.split(".") if p]:
        tree = tree.get(part, {}) if isinstance(tree, dict) else {}
    return tree.get(key, MISSING) if isinstance(tree, dict) else MISSING


def ondisk(cfg):
    """The config as config.toml says now. The running app keeps where state lives and how it's served until it
    restarts, so those read from here."""
    return config.load(cfg.config_path)


def current(cfg, scope, table, key) -> Any:
    """What a setting is now, for one scope: the loaded config's value, or for a list kept per scope, the entries that
    scope sets itself."""
    if scope is not None:
        if (table, key) == AUTO:
            return scope in config.auto_repos(cfg)
        if (table, key) == HOLD:
            return config.holds_yours(cfg, scope)
        if (table, key) in (("", "checks"), ("", "checkout"), ("", "peers"), ("", "issues")):
            entry = next((r for r in cfg.repos if r.name == scope), None)
            value = getattr(entry, key, None) if entry else None
            return (
                list(value) if isinstance(value, list) else value or ("" if key != "checks" else [])
            )
        return owned(cfg, scope).get((table, key), [])
    if (table, key) in RESTART:
        cfg = ondisk(cfg)
        if key == "state_dir":
            return str(cfg.state_dir)
    if (table, key) in GUARDS:
        return owned(cfg, None).get((table, key), [])
    if (table, key) == GATES:
        value = _lookup(_doc(cfg), table, key)
        return list(value) if value is not MISSING else default(table, key)
    holder = cfg
    for part in [p for p in table.split(".") if p]:
        holder = getattr(holder, part)
    value = getattr(holder, key)
    return dict(vars(value)) if table == "slack" and key == "channels" else value


def default(table, key) -> Any:
    """A setting's value in defaults.toml, or MISSING when it has none there."""
    return _lookup(config._read(config.DEFAULTS), table, key)


def _where(value):
    """Where a path, or each path in a list, points."""
    if isinstance(value, list):
        return [os.path.expanduser(str(v)) for v in value]
    return os.path.expanduser(str(value or ""))


def _same(table, key, a, b):
    if (table, key) in PATHS:
        return _where(a) == _where(b)
    return a == b


def changed(cfg, changes):
    """Only the changes that differ from what's set now, so a save touches nothing else in the file."""
    return {k: v for k, v in changes.items() if not _same(k[1], k[2], current(cfg, *k), v)}


def runs(cfg, changes):
    """The command lines these changes would have Plus One run, each as it will run."""
    found = []
    for (scope, table, key), value in changes.items():
        if (table, key) not in RUNS or not value:
            continue
        if key == "checks":
            for line in [v for v in value if v not in current(cfg, scope, table, key)]:
                found.append(
                    Run(
                        f"Check in {scope}",
                        shlex.split(line),
                        "In the PR's worktree before every review, with no shell, the reviewer's stripped "
                        "environment, and the check time limit. {files} becomes the changed files that exist.",
                    )
                )
        elif key == "draft_command":
            found.append(
                Run(
                    "Drafting script",
                    ["bash", value, "BRIEF_PATH", "OUT_PATH", "--max", "110"],
                    "For every comment it drafts, in place of the drafting model.",
                )
            )
        else:
            found.append(
                Run(
                    "Claude login folder",
                    [f"CLAUDE_CONFIG_DIR={value}", "claude", "-p", "…"],
                    "Every model call runs with the Claude Code login and settings in this folder.",
                )
            )
    return found


def retypes(changes):
    """The changes that can lock the owner out of the web app."""
    return [
        Retype(label(table, key), key, str(value))
        for (scope, table, key), value in changes.items()
        if (table, key) in RETYPE and value
    ]


def restart_pending(cfg):
    """The settings config.toml holds that the running web app hasn't taken on yet, as {label: (running, saved)}."""
    try:
        disk = ondisk(cfg)
    except config.ConfigError:
        return {}
    pending = {}
    for table, key in RESTART:
        running, saved = (
            (str(cfg.state_dir), str(disk.state_dir))
            if key == "state_dir"
            else (getattr(cfg.web, key), getattr(disk.web, key))
        )
        if running != saved:
            pending[label(table, key)] = (running, saved)
    return pending


def loosens(cfg, changes):
    """What in these changes lets Plus One do more on its own."""
    found = []
    for (scope, table, key), value in changes.items():
        name = label(table, key)
        raised = (scope is None and (table, key) in CAPS) or (
            table.startswith("models.") and key == "budget_usd"
        )
        if raised:
            was = current(cfg, scope, table, key)
            if value > was:
                what = "the daily spend cap" if key == "max_spend_per_day_usd" else name.lower()
                found.append(
                    Loosening(name, f"raises {what} to {value}", show(key, was), show(key, value))
                )
        if (table, key) == UPDATE_MODE and value == "automatic":
            was = current(cfg, scope, table, key)
            if was != value:
                found.append(
                    Loosening(
                        name, "installs new versions on its own", MODE_WORDS[was], "Automatic"
                    )
                )
        if (
            (table, key) == UPDATE_CHANNEL
            and value == "main"
            and current(cfg, scope, table, key) != value
        ):
            found.append(
                Loosening(
                    name, "installs every merge to main, not only releases", "Releases", "Main"
                )
            )
        if (table, key) == AUTO and value and scope not in config.auto_repos(cfg):
            found.append(
                Loosening(
                    f"Approve on its own in {scope}",
                    f"turns on approving clean PRs in {scope}",
                    "Off",
                    "On",
                )
            )
        if (table, key) == HOLD and not value and config.holds_yours(cfg, scope):
            found.append(
                Loosening(
                    f"Hold PRs in your code in {scope}",
                    f"lets {scope} approve PRs in your code on its own",
                    "On",
                    "Off",
                )
            )
        if (table, key) in GUARDS:
            removed = [v for v in current(cfg, scope, table, key) if v not in value]
            if removed:
                where = f" in {scope}" if scope else " for every repo"
                found.append(
                    Loosening(
                        f"{name}{where}",
                        f"takes {', '.join(removed)} out of {name.lower()}{where}",
                        removed=removed,
                    )
                )
        if (table, key) == GATES:
            added = [v for v in value if v not in current(cfg, scope, table, key)]
            if added:
                found.append(
                    Loosening(
                        name,
                        f"stops failing checks named like {', '.join(added)} from holding a PR",
                        added=added,
                    )
                )
    return found


def _doc(cfg):
    import tomlkit

    return tomlkit.parse(cfg.config_path.read_text())


def inherited(cfg, repo):
    """For each guarded list, the entries that apply to this scope from further out (the defaults, and for a repo the
    top of the config too), which the page shows but can't remove there."""
    defaults = config._read(config.DEFAULTS)
    top = _doc(cfg) if repo is not None else {}
    return {
        (table, key): list(
            dict.fromkeys([*defaults.get(table, {}).get(key, []), *top.get(table, {}).get(key, [])])
        )
        for table, key in GUARDS
    }


def gate_overrides(cfg):
    """The repos whose own entry sets approval_gates, which the every-repo list doesn't reach."""
    return [
        r.get("name")
        for r in _doc(cfg).get("repos", [])
        if "approval_gates" in r.get("filters", {})
    ]


def owned(cfg, repo):
    """For each guarded list, the entries this scope sets itself (a repo's entry, or the top of the config for None),
    which the page can change."""
    doc = _doc(cfg)
    entry = (
        doc
        if repo is None
        else next((r for r in doc.get("repos", []) if r.get("name") == repo), {})
    )
    return {(table, key): list(entry.get(table, {}).get(key, [])) for table, key in GUARDS}


def outline(text):
    """The rubric's sections in order, with their length and whether Learn again may change them."""
    out = []
    for part in re.split(r"(?m)^## ", text)[1:]:
        heading = part.splitlines()[0].strip()
        out.append(
            {
                "heading": heading,
                "words": len(part.split()) - len(heading.split()),
                "locked": any(f"## {heading}".startswith(prefix) for prefix in learn.LOCKED),
                "voice": heading.startswith("Comment format (drafts)"),
            }
        )
    return out


def _prune(doc, table):
    """Drops tables a deletion left empty, innermost first, so no bare [limits] header stays behind."""
    parts = [p for p in table.split(".") if p]
    while parts:
        parent = _table(doc, ".".join(parts[:-1]), create=False)
        if parent is None:
            return
        node = parent.get(parts[-1])
        if node is None or len(node):
            return
        del parent[parts[-1]]
        parts.pop()


def save(cfg, changes, confirm="", run="", again=None):
    """Writes the changes into config.toml in place, keeping the old file as config.toml.bak, and returns the new
    config. Loosening needs confirm to be CONFIRM, anything that changes what runs needs run to be RUN, and a value
    that can lock the owner out needs again[key] to repeat it."""
    import tomlkit

    with LOCK:
        return _save(cfg, changes, confirm, run, again or {}, tomlkit)


def _save(cfg, changes, confirm, run, again, tomlkit):
    doc = _open(cfg, tomlkit)
    loosened, ran, typed = loosens(cfg, changes), runs(cfg, changes), retypes(changes)
    owed = [
        *([f"type {CONFIRM}"] if loosened and confirm.strip().lower() != CONFIRM else []),
        *([f"type {RUN}"] if ran and run.strip().lower() != RUN else []),
        *[
            f"type the new {r.label.lower()} again"
            for r in typed
            if again.get(r.name, "").strip() != r.value
        ],
    ]
    if owed:
        what = [item.text for item in loosened] + [f"runs {r.label.lower()}" for r in ran]
        what += [f"changes the {r.label.lower()}" for r in typed]
        raise NeedsConfirm(
            f"This {', and '.join(what)}. To save it, {', and '.join(owed)}.",
            loosened,
            ran,
            typed,
        )
    path = cfg.config_path
    for (scope, table, key), value in changes.items():
        if (table, key) in GUARDS:
            # Entries that already apply from further out don't need repeating here.
            elsewhere = inherited(cfg, scope).get((table, key), [])
            value = [v for v in value if v not in elsewhere]
        elif scope is None and value == default(table, key):
            # Back to the default: take the key out so later changes to defaults.toml reach it.
            target = _table(doc, table, create=False)
            if target is not None and key in target:
                del target[key]
                _prune(doc, table)
            continue
        target = _table(doc, table, scope)
        if (table, key) == AUTO and not value and key not in target:
            continue
        target[key] = _value(value)
    return _write(path, doc, tomlkit)


def _write(path, doc, tomlkit):
    backup = path.with_name(path.name + ".bak")
    shutil.copyfile(path, backup)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(tomlkit.dumps(doc))
    os.replace(tmp, path)
    try:
        return config.load(path)
    except config.ConfigError as exc:
        # A file that doesn't load would stop every check, so it goes back the way it was.
        os.replace(backup, path)
        raise SettingsError(f"That would break the config, so nothing changed: {exc}") from None


def _open(cfg, tomlkit):
    try:
        return tomlkit.parse(cfg.config_path.read_text())
    except Exception as exc:
        raise SettingsError(
            f"config.toml doesn't parse right now, so nothing was saved: {exc}"
        ) from None


def add_repo(cfg, name, checkout):
    """Adds a [[repos]] entry for a repo and its clone. It approves nothing on its own until the owner turns that on."""
    import tomlkit

    name = str(name).strip()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
        raise SettingsError(f"{name or 'That'} doesn't look like owner/repo.", "name")
    if name in {r.name for r in cfg.repos}:
        raise SettingsError(f"{name} is already set up.", "name")
    entry = tomlkit.table()
    entry["name"] = name
    entry["checkout"] = _clone(checkout, name, "checkout")
    with LOCK:
        doc = _open(cfg, tomlkit)
        doc["repos"].append(entry)
        return _write(cfg.config_path, doc, tomlkit)


def remove_repo(cfg, name):
    """Takes a repo's [[repos]] entry out. Its reviews and history stay in the database."""
    import tomlkit

    with LOCK:
        doc = _open(cfg, tomlkit)
        repos = doc.get("repos", [])
        keep = [r for r in repos if r.get("name") != name]
        if len(keep) == len(repos):
            raise SettingsError(f"{name} isn't in your config.")
        if not keep:
            raise SettingsError("Plus One needs at least one repo, so the last one stays.")
        for i in reversed(range(len(repos))):
            if repos[i].get("name") == name:
                del repos[i]
        return _write(cfg.config_path, doc, tomlkit)
