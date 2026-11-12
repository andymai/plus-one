import re
from collections import namedtuple
from datetime import timedelta

from . import codeowners

Skip = namedtuple("Skip", "kind reason")


def _latest_by_author(reviews):
    latest = {}
    for review in reviews:
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review["author"]] = review
    return latest


def evaluate(pr, cfg, now, decided_sha=None):
    if pr["state"] != "OPEN":
        return Skip("skip", "not open")
    if pr["draft"]:
        return Skip("skip", "draft")
    if pr["author"] == cfg.me:
        return Skip("skip", "authored by me")
    if pr["merge_queue"]:
        return Skip("skip", "in the merge queue")
    if pr["decision"] == "APPROVED":
        return Skip("skip", "already approved overall")
    held = set(pr["labels"]) & set(cfg.filters.hold_labels)
    if held:
        return Skip("skip", f"hold label: {sorted(held)[0]}")
    if pr["mergeable"] == "CONFLICTING":
        return Skip("skip", "merge conflict")
    if decided_sha == pr["head"]:
        return Skip("skip", "already decided at this commit")
    if any(r["author"] == cfg.me and r["oid"] == pr["head"] for r in pr["reviews"]):
        return Skip("skip", "I already reviewed this commit")

    for author, review in _latest_by_author(pr["reviews"]).items():
        if review["state"] == "CHANGES_REQUESTED" and author != cfg.me:
            return Skip("skip", f"changes requested by {author}")
    markers = cfg.filters.blocking_markers
    # On a follow-up the owner's own blocking threads are the asks it checks, so they can't hold it back.
    mine = cfg.me if followup(pr, cfg.me) else None
    for thread in pr["threads"]:
        body = thread["body"].lower()
        if (
            not thread["resolved"]
            and not thread["outdated"]
            and thread["author"] != mine
            and any(m in body for m in markers)
        ):
            return Skip("skip", f"open blocking thread from {thread['author']}")

    gates = [re.compile(p) for p in getattr(cfg.filters, "approval_gates", [])]
    ci = [c for c in pr["checks"] if not any(g.search(c["name"]) for g in gates)]
    checks = [c for c in ci if c["required"]] or ci
    failing = [c["name"] for c in checks if c["state"] == "FAIL"]
    if failing:
        return Skip("skip", f"required check failing: {failing[0]}")
    if checks and all(c["state"] == "PENDING" for c in checks):
        return Skip("skip", "CI still pending")

    if now - pr["ready_at"] < timedelta(minutes=cfg.limits.min_ready_minutes):
        return Skip("skip", "ready for under the minimum wait")

    size = pr["additions"] + pr["deletions"]
    if size > cfg.limits.max_map_lines:
        return Skip("defer", f"too large to trace ({size} changed lines)")
    return None


def followup(pr, me):
    """What `me` is still asking for, when their latest decisive review on the PR requested changes."""
    latest = _latest_by_author(pr["reviews"]).get(me)
    if not latest or latest["state"] != "CHANGES_REQUESTED":
        return None
    # Outdated threads count: the code under them moving is usually how the fix shows up. So do threads someone
    # else resolved, since an author marking one resolved doesn't show it's fixed. Only the owner's own resolve settles it.
    threads = [
        t
        for t in pr["threads"]
        if t["author"] == me and not (t["resolved"] and t.get("resolved_by") == me)
    ]
    # A force-push can leave the review with no commit, and it's still the owner's block.
    return {
        "sha": latest.get("oid") or "",
        "body": (latest.get("body") or "").strip(),
        "threads": threads,
        "incomplete": bool(pr.get("threads_truncated")),
    }


def picked_up(pr, since):
    """Whether anyone besides the author reviewed the PR after `since`. On an agent's PR the person who asked for it
    is its author in all but name, so their replies don't count either."""
    authors = {pr["author"], pr.get("requested_by") or "", ""}
    return any(
        r["at"] and r["at"] >= since and not r["bot"] and r["author"] not in authors
        for r in pr["reviews"]
    )


def thread_authors(pr):
    return {
        c["author"]
        for t in pr["threads"]
        if not t["resolved"] and not t["outdated"]
        for c in t["comments"]
    } - {""}


def could_hold(pr):
    # Any state counts as latest here: a COMMENTED review after an approval can carry a new question.
    latest = {r["author"]: r["state"] for r in pr["reviews"]}
    approved = {a for a, state in latest.items() if state == "APPROVED"}
    spoke = {
        c["author"]
        for t in pr["threads"]
        if not t["resolved"] and not t["outdated"]
        for c in t["comments"]
        if not c["bot"]
    }
    spoke |= {r["author"] for r in pr["reviews"] if not r["bot"] and r["body"].strip()}
    spoke |= {c["author"] for c in pr["conversation"] if not c["bot"]}
    return spoke - approved - {pr["author"], ""}


def waiting_on(pr, names):
    eligible = could_hold(pr)
    return [n for n in dict.fromkeys(names) if n in eligible]


def too_large(pr, cfg):
    size = pr["additions"] + pr["deletions"]
    if pr["files_truncated"] or size > cfg.limits.max_diff_lines:
        return f"too large to trace ({size} changed lines)"
    return None


def sensitive(pr, rules, cfg):
    restricted = set(cfg.lanes.never_approve_owners)
    patterns = [re.compile(p, re.I) for p in cfg.lanes.never_approve_paths]
    out = {}
    for path in pr["files"]:
        hit = restricted & codeowners.owners_for(rules, path)
        if hit:
            out[path] = sorted(hit)[0]
        elif any(p.search(path) for p in patterns):
            out[path] = ""
    return out


def lane(pr, rules, cfg):
    if pr["author"].startswith("dependabot") or pr["author"] == "app/dependabot":
        return "dependabot", "dependency bump"
    for path, owner in sensitive(pr, rules, cfg).items():
        return (
            "never_approve",
            f"{path} is owned by {owner}" if owner else f"{path} matches a restricted path",
        )
    return None, ""
