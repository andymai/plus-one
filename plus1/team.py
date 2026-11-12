"""Whose PRs the owner wants first: their GitHub teams and named people, then other teams they watch. This only orders
PRs that already reached the owner. It never picks up a new PR and never changes what gets approved or posted."""

import json
import subprocess
from datetime import datetime, timedelta

from . import gh

TEAM, OTHER = "team", "other"
# One row per team, so a slow read of one team can't overwrite a newer read of another.
PREFIX = "team_members:"
FRESH = timedelta(days=1)
RETRY = timedelta(hours=1)


def _saved(store):
    rows = store.query("SELECT key, value FROM meta WHERE key LIKE ?", f"{PREFIX}%")
    return {row["key"][len(PREFIX) :]: json.loads(row["value"]) for row in rows}


def _due(entry, now):
    if not entry.get("at"):
        return True
    return now - datetime.fromisoformat(entry["at"]) >= (RETRY if entry.get("failed") else FRESH)


def refresh(cfg, store, log=print):
    """Reads each configured GitHub team once a day, and a newly added one at once. A failed read keeps the last members,
    so a GitHub outage can't take a teammate's PRs out of the team."""
    saved, now = _saved(store), datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")
    for name in dict.fromkeys(n.lower() for n in [*cfg.team.teams, *cfg.team.others]):
        entry = saved.get(name, {})
        if not _due(entry, now):
            continue
        try:
            fresh = {"members": gh.team_members(name), "at": stamp}
        except (gh.GhError, subprocess.SubprocessError, OSError) as exc:
            log(f"couldn't read the GitHub team {name}: {exc}")
            fresh = {"members": entry.get("members", []), "at": stamp, "failed": True}
        store.set_meta(f"{PREFIX}{name}", json.dumps(fresh))


def known(cfg, store):
    """Lowercase logins as (the owner's team, other teams they watch). Someone on both counts as the owner's team."""
    saved = _saved(store)

    def members(names):
        return {login for name in names for login in saved.get(name.lower(), {}).get("members", [])}

    me = {cfg.me.lower()}
    mine = (members(cfg.team.teams) | {p.lower() for p in cfg.team.people}) - me
    return mine, members(cfg.team.others) - mine - me


def kind(pr, people):
    """TEAM or OTHER when the PR's author, or the person who asked an agent to open it, is one of these people."""
    who = {(pr.get("author") or "").lower(), (pr.get("requested_by") or "").lower()}
    mine, others = people
    return TEAM if who & mine else OTHER if who & others else ""


def size(count):
    return f"{count} {'person' if count == 1 else 'people'}"


def status(names, store):
    saved = _saved(store)
    found = []
    for name in names:
        entry = saved.get(name.lower())
        count = len(entry.get("members", [])) if entry else 0
        found.append(
            {
                "name": name,
                "size": size(count) if entry else "not read yet",
                "count": count,
                "failed": bool(entry and entry.get("failed")),
            }
        )
    return found


def _orgs(repos):
    return sorted({repo.split("/")[0].lower() for repo in repos})


def suggest_mine(repos):
    """The owner's GitHub teams in the orgs of these repos, smallest first, since a small team is likelier to be the one
    they work on than an all-engineering list."""
    orgs = set(_orgs(repos))
    teams = [t for t in gh.my_teams() if t[0].split("/")[0] in orgs]
    return sorted(teams, key=lambda t: (t[1], t[0]))


def suggest_others(repos):
    mine = {name for name, _ in gh.my_teams()}
    return sorted({name for org in _orgs(repos) for name in gh.org_teams(org)} - mine)
