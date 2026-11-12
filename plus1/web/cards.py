import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import actions, codeowners, config, diffs, gh, prs, team
from ..drafts import DRAFT_FAILED
from ..review import FOLLOWUP_OPEN, FOLLOWUP_UNCHANGED
from ..store import Store, decode_json
from . import queries, refs, words

ORDER = {
    "overtaken": 0,
    "fixed": 1,
    "looks_good": 2,
    "blocking": 3,
    "worth_fixing": 4,
    "needs_eyes": 5,
    "still_open": 6,
    "error": 7,
    "stale": 8,
}
URGENCY = {
    "overtaken": 0,
    "blocking": 1,
    "fixed": 2,
    "worth_fixing": 3,
    "needs_eyes": 4,
    "still_open": 5,
    "error": 6,
    "stale": 7,
    "looks_good": 8,
}
FOLLOWUP_STATES = ("fixed", "still_open")
STACK_PART = re.compile(r"\s*\[(\d+)/(\d+)\]")
STACK_TICKET = re.compile(r"\s*\(([A-Z][A-Z0-9]*-\d+)\)")
GIST_LEAD = re.compile(r"^(?:part \d+ of [^.]*\.\s+)?(?:(?:the|this) PR\s+|it\s+)?", re.I)
MAX_STACK = 20


def stack_key(title):
    part, ticket = STACK_PART.search(title or ""), STACK_TICKET.search(title or "")
    if not (part and ticket):
        return None
    return ticket.group(1), int(part.group(1)), int(part.group(2))


def _short_title(title):
    return STACK_TICKET.sub("", STACK_PART.sub("", title or "")).strip()


def _source(store, review, number):
    source = review.get("source")
    if source not in ("slack", "both", "manual"):
        return None
    if source == "manual":
        return "manual run"
    channels = queries.story_channels(store, number)
    where = ", ".join(f"#{c.replace('_', '-')}" for c in channels[:2]) or "Slack"
    return where if source == "slack" else f"requested + {where}"


def _first_sentence(text, limit=220):
    text = (text or "").strip()
    for stop in (". ", "\n"):
        idx = text.find(stop)
        if 0 < idx < limit:
            return text[: idx + 1].strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _gist(about):
    text = GIST_LEAD.sub("", (about or "").strip(), count=1)
    return _first_sentence(text[:1].upper() + text[1:], 160) or None


_owner_rules = {"key": None, "rules": []}


def _codeowners(cfg):
    path = Path(cfg.checkout or "") / ".github" / "CODEOWNERS"
    try:
        key = (str(path), path.stat().st_mtime)
        if _owner_rules["key"] != key:
            _owner_rules.update(key=key, rules=codeowners.parse(path.read_text(errors="replace")))
    except OSError:
        return []
    return _owner_rules["rules"]


def sensitive_files(cfg, review):
    if review.get("pr") is not None:
        cfg, _ = config.at(cfg, review["pr"])
    stored = decode_json(review.get("sensitive"), None)
    if isinstance(stored, dict):
        return {
            path: f"owned by {owner}" if owner else "sensitive path"
            for path, owner in stored.items()
        }
    # Reviews from before the tick stored every hit: fall back to the local checkout's CODEOWNERS, which may lag GitHub.
    lane_reason = review.get("lane_reason") or ""
    out = {}
    owned, restricted_path = words.OWNED.match(lane_reason), words.RESTRICTED.match(lane_reason)
    if owned:
        out[owned.group(1)] = f"owned by {owned.group(2)}"
    elif restricted_path:
        out[restricted_path.group(1)] = "sensitive path"
    restricted = set(cfg.lanes.never_approve_owners)
    patterns = [re.compile(p, re.I) for p in cfg.lanes.never_approve_paths]
    rules = _codeowners(cfg)
    for path in decode_json(review.get("files"), []) or []:
        if path in out:
            continue
        hit = restricted & codeowners.owners_for(rules, path)
        if hit:
            out[path] = f"owned by {sorted(hit)[0]}"
        elif any(p.search(path) for p in patterns):
            out[path] = "sensitive path"
    return out


def _ignored(store, number):
    return bool(store.query("SELECT 1 FROM ignored WHERE pr = ?", number))


def _handled(store, number, sha, review_id):
    return bool(
        store.query(
            "SELECT 1 FROM handled WHERE pr = ? AND sha = ? AND (review_id IS NULL OR review_id >= ?) LIMIT 1",
            number,
            sha,
            review_id,
        )
    )


def _approved(store, number, sha):
    return bool(
        store.query(
            "SELECT 1 FROM actions WHERE pr = ? AND sha = ? AND action IN ('approve', 'release') AND ok = 1 LIMIT 1",
            number,
            sha,
        )
    )


def _settled(status, drafts=()):
    if not status:
        return False
    # Someone else's approval doesn't make the owner's queued comments moot, so those cards wait for the merge. Once it
    # merges or closes, the watcher retires them (Store.expire_unposted): nothing is left to act on.
    if drafts:
        return status["state"] != "OPEN"
    return (
        status["state"] != "OPEN" or bool(status["merge_queue"]) or status["decision"] == "APPROVED"
    )


def pending_for(store, number):
    rows = store.query(
        "SELECT * FROM pending_actions WHERE pr = ? AND status IN ('pending', 'running') ORDER BY id DESC LIMIT 1",
        number,
    )
    return dict(rows[0]) if rows else None


def _running_since():
    return (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")


def reviewing(store, number):
    # A tick killed mid-review never finishes its row, so only a recent one counts as running.
    return bool(
        store.query(
            """SELECT 1 FROM reviews WHERE pr = ? AND finished_at IS NULL AND started_at >= ?
                               AND id = (SELECT MAX(id) FROM reviews WHERE pr = ?)""",
            number,
            _running_since(),
            number,
        )
    ) or _review_coming(store, number)


def _review_coming(store, number):
    # A post refused for new commits starts a review of them, which can wait out a running tick before its row exists.
    rows = store.query(
        """SELECT payload FROM pending_actions p WHERE pr = ? AND status = 'failed' AND finished_at >= ?
             AND id = (SELECT MAX(id) FROM pending_actions WHERE pr = ?)
             AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.pr = p.pr AND r.id > p.review_id)""",
        number,
        _running_since(),
        number,
    )
    return bool(rows) and bool(decode_json(rows[0]["payload"], {}).get("rereview"))


def _facts(review, raw, state, parsed):
    stats = diffs.file_stats(review.get("diff"))
    stats.sort(key=lambda s: (bool(diffs.TESTISH.search(s["path"])), -(s["add"] + s["del"])))
    notes = words.quiet_notes(decode_json(review.get("p2_notes"), []))
    return {
        "files": stats[:4],
        "more_files": max(0, len(stats) - 4),
        "notes": notes[:3],
        "more_notes": max(0, len(notes) - 3),
        "all_notes": notes,
        "checked": raw.get("checked") or [],
        "focus": raw.get("focus") or [],
        "excerpt": queries.lead_excerpt(parsed()) if state == "looks_good" else None,
    }


def _rewriting(store, drafts):
    ids = [d["id"] for d in drafts]
    states, rules = actions.rewrites(store, ids), store.rules_of(ids)
    for d in drafts:
        d["rewrite"] = states.get(d["id"], {})
        d["notes"] = decode_json(d.get("notes"), []) or []
        d["restorable"] = d.get("original_body") is not None and (
            d["body"] != d["original_body"] or bool(d["notes"])
        )
        d["rule"] = rules.get(d["id"])


def build(store, review, cfg, light=False):
    """Turn a review row into a card, or None when it does not need the owner."""
    number, sha = review["pr"], review["sha"]
    drafts = [
        dict(d)
        for d in store.query(
            "SELECT * FROM drafts WHERE review_id = ? AND status = 'queued' ORDER BY severity, id",
            review["id"],
        )
    ]
    for d in drafts:
        if isinstance(d.get("unsupported"), str):
            try:
                d["unsupported"] = json.loads(d["unsupported"])
            except json.JSONDecodeError:
                d["unsupported"] = [d["unsupported"]]
    if not review.get("finished_at") or _review_coming(store, number):
        return None
    followup = decode_json(review.get("followup"), None)
    status = store.pr_status(number)
    if review.get("error"):
        state = "error"
    elif drafts:
        state = "blocking" if any(d["severity"] == "P0" for d in drafts) else "worth_fixing"
    elif followup:
        if review.get("reason") == FOLLOWUP_UNCHANGED:
            return None
        state = "still_open" if any(i["open"] for i in followup["items"]) else "fixed"
    elif review.get("verdict") == "DEFER":
        state = "needs_eyes"
    elif review.get("verdict") == "APPROVE":
        # One that went out on its own leaves through _approved below.
        state = "looks_good"
    else:
        return None
    if _handled(store, number, sha, review["id"]):
        return None
    if state in ("needs_eyes", "looks_good", *FOLLOWUP_STATES) and _approved(store, number, sha):
        return None
    if _ignored(store, number) or _settled(status, drafts):
        return None

    findings = [
        dict(f)
        for f in store.query(
            """
        SELECT * FROM findings WHERE review_id = ?
          AND (verified = 1 OR id IN (SELECT finding_id FROM drafts WHERE review_id = ? AND status = 'queued'))
        ORDER BY idx""",
            review["id"],
            review["id"],
        )
    ]
    rejected = []
    if state == "needs_eyes" and not light:
        ups = actions.write_ups(store, review["id"])
        for row in store.query(
            """SELECT * FROM findings WHERE review_id = ? AND verified = 0 AND carried IS NULL
                                  ORDER BY idx""",
            review["id"],
        ):
            f, up = dict(row), ups.get(row["id"]) or {}
            f["writing"] = up.get("status") == "writing"
            f["write_error"] = up.get("body") if up.get("status") == "failed" else None
            rejected.append(f)
    raw = decode_json(review.get("raw_review"), {}) or {}
    files = []

    def parsed():
        if not files:
            files.extend(queries.parse_diff(review.get("diff")))
        return files

    for d in drafts:
        d["excerpt"] = (
            queries.excerpt(parsed(), d["path"], d["line"])
            if d.get("inline") and not light
            else None
        )
        d["draft_failed"] = bool(d["needs_edit"]) and d["unsupported"] == [DRAFT_FAILED]
        d["finding"] = next((f for f in findings if f["id"] == d["finding_id"]), None)
    if drafts and not light:
        _rewriting(store, drafts)
    headline, tone = words.STATE[state]
    detail, reason = "", None
    if state in ("blocking", "worth_fixing") and findings:
        top = drafts[0]["finding_id"]
        summary = reason = next(
            (f["title"] for f in findings if f["id"] == top), findings[0]["title"]
        )
    elif state == "needs_eyes":
        summary, detail = words.waiting(review)
        reason = words.waiting_line(review)
    elif state == "error":
        summary = review.get("error")
        reason = _first_sentence(summary, 160)
    elif state in FOLLOWUP_STATES and followup:
        summary = reason = words.followup_line(followup["items"])
        if review.get("verdict") == "DEFER" and not (review.get("reason") or "").startswith(
            FOLLOWUP_OPEN
        ):
            detail = words.defer_reason(review.get("reason"))
    else:
        summary = _first_sentence(review.get("reason"))
        reason = _gist(raw.get("about")) if state == "looks_good" else None
    latest = queries.latest_observed_sha(store, number)
    last = store.query(
        """SELECT kind, status, error FROM pending_actions
                          WHERE review_id = ? AND status IN ('failed', 'expired') ORDER BY id DESC LIMIT 1""",
        review["id"],
    )
    failure = (
        words.action_failure(last[0]["kind"], last[0]["status"], last[0]["error"]) if last else None
    )
    moved = bool(failure and words.HEAD_MOVED.search(last[0]["error"] or ""))
    stale = bool(latest and latest != sha) or moved
    taken = words.overtaken(status) if drafts else None
    # A stale card offers only Review again, so it can't file under its state's group (say, Ready to approve).
    group = "stale" if stale else "overtaken" if taken and state == "blocking" else state
    if group == "overtaken":
        headline, tone = words.STATE["overtaken"]
        if status["merge_queue"]:
            headline = "In the merge queue without your blocking comment"
    if stale:
        tone = "muted"
    clean = state == "needs_eyes" and words.clean_defer(review)
    waiting = bool(followup) and any(i["open"] for i in followup["items"])
    labels = decode_json(review.get("labels"), [])
    author = review.get("author") or ""
    agent = (
        not author.startswith("dependabot")
        and author != "app/dependabot"
        and any(
            str(label).startswith("agent/") and label != "agent/dependabot"
            for label in labels or []
        )
    )
    size = (review.get("additions") or 0) + (review.get("deletions") or 0)
    stack = stack_key(review.get("title"))
    ticket = review.get("ticket") or gh.ticket(review.get("title"))
    flags = words.flags(state, review.get("lane"), size, agent)
    chips, mine = words.yours(review.get("yours"))
    from_team = team.kind(review, team.known(cfg, store))
    if taken and group != "overtaken":
        flags.append(taken)
    if state == "looks_good" and review.get("held"):
        flags.append(f"held: {review['held']}")
    last_act = store.query(
        "SELECT action FROM actions WHERE pr = ? ORDER BY id DESC LIMIT 1", number
    )
    if last_act and last_act[0]["action"] == "resurface":
        flags.append("nobody picked this up")
    return {
        "pr": number,
        "sha": sha,
        "stack": stack,
        "short_title": _short_title(review.get("title")) if stack else None,
        "source": _source(store, review, number),
        "review_id": review["id"],
        "title": review.get("title") or f"PR #{number}",
        "author": author,
        "creator": words.creator(review),
        "reason": reason,
        "more": max(0, len(drafts) - 1),
        "url": review.get("url") or prs.url(number),
        "when": review.get("finished_at") or review.get("started_at"),
        "state": state,
        "group": group,
        "headline": headline,
        "icon_label": words.GROUP[group] if stale else headline,
        "tone": tone,
        "decision": words.decision(state, len(drafts), stale, clean, waiting=waiting),
        "followup": followup,
        "waiting": waiting,
        "clean": clean,
        "about": raw.get("about"),
        "ticket": ticket,
        "ticket_url": f"{cfg.issues}/{ticket}" if ticket else None,
        "parts": stack_parts(store, cfg, stack[0], stack[2]) if stack and not light else [],
        "flags": flags,
        "team": from_team,
        "team_chip": words.TEAM_CHIP.get(from_team),
        "yours": chips,
        "yours_lines": mine,
        "summary": summary,
        "detail": detail,
        "facts": _facts(review, raw, state, parsed)
        if state in ("looks_good", "needs_eyes") and not light
        else None,
        "drafts": drafts,
        "rejected": rejected,
        "refs": None if light else refs.for_review(cfg, review),
        "agent": agent,
        "stale": stale,
        "failure": failure,
        "pending": pending_for(store, number),
        "order": ORDER[group],
    }


def stack_parts(store, cfg, ticket, total):
    if total > MAX_STACK:
        return []
    latest = {}
    for r in store.query(
        """
        SELECT r.* FROM reviews r
        JOIN (SELECT pr, MAX(id) AS id FROM reviews WHERE title LIKE ? GROUP BY pr) last ON last.id = r.id
    """,
        f"%({ticket})%",
    ):
        key = stack_key(r["title"])
        if key and key[0] == ticket and key[2] == total:
            latest[key[1]] = dict(r)
    parts = []
    for n in range(1, total + 1):
        review = latest.get(n)
        if review is None:
            parts.append({"part": n, "pr": None, "label": "not seen yet", "tone": "muted"})
            continue
        rows = store.query("SELECT * FROM pr_status WHERE pr = ?", review["pr"])
        status = dict(rows[0]) if rows else {}
        card = build(store, review, cfg, light=True)
        if status.get("state") == "MERGED":
            label, tone = "merged", "ok"
        elif status.get("state") == "CLOSED":
            label, tone = "closed", "muted"
        elif card:
            label, tone = words.GROUP[card["group"]].lower(), card["tone"]
        elif status.get("merge_queue"):
            label, tone = "in the merge queue", "ok"
        elif status.get("decision") == "APPROVED":
            label, tone = "approved", "ok"
        else:
            label, tone = "nothing needed from you", "muted"
        parts.append(
            {
                "part": n,
                "pr": review["pr"],
                "review_id": review["id"],
                "title": _short_title(review["title"]),
                "label": label,
                "tone": tone,
            }
        )
    return parts


def _since():
    return (datetime.now().astimezone() - timedelta(days=14)).isoformat(timespec="seconds")


def todo(store, cfg, light=False):
    reviews = [
        dict(r)
        for r in store.query(
            """
        SELECT r.* FROM reviews r
        JOIN (SELECT pr, MAX(id) AS id FROM reviews GROUP BY pr) last ON last.id = r.id
        WHERE r.started_at >= ? OR r.pr IN (SELECT pr FROM drafts WHERE status = 'queued')
           OR r.pr IN (SELECT pr FROM pr_status WHERE state = 'OPEN')
    """,
            _since(),
        )
    ]
    cards = [c for c in (build(store, r, cfg, light) for r in reviews) if c]
    cards.sort(key=lambda c: (c["order"], *_rank([c]), c["when"] or ""))
    return cards


def refresh_status(store, cfg):
    store.expire_unposted(merged_only=True)
    numbers = [
        r["pr"]
        for r in store.query(
            """
        SELECT DISTINCT r.pr FROM reviews r LEFT JOIN pr_status s ON s.pr = r.pr
        WHERE (r.started_at >= ? OR s.state = 'OPEN' OR (r.pr IN (SELECT pr FROM drafts WHERE status = 'queued')
                                                         AND s.state IS NULL))
          AND (s.state IS NULL OR s.state != 'MERGED')
    """,
            _since(),
        )
    ]
    if not numbers:
        return 0
    states = {}
    configured = {r.name for r in cfg.repos}
    # A repo taken out of the config keeps its saved reviews, but nothing polls it any more.
    for name in dict.fromkeys(prs.repo(n) for n in numbers if prs.repo(n) in configured):
        rcfg = config.view(cfg, name)
        found = gh.pr_states(rcfg, [prs.number(n) for n in numbers if prs.repo(n) == name])
        states.update({prs.pid(name, n): s for n, s in found.items()})
    for number, s in states.items():
        drafts = actions.queued(store, number)
        if drafts and not _settled(store.pr_status(number)) and _settled(s):
            # Before saving the new status, so a send that raises is retried on the next poll.
            actions.notify(
                cfg,
                "Comments not posted",
                words.unposted(prs.label(number), s, drafts),
                path=f"/review/{drafts[0]['review_id']}",
                store=store,
            )
        store.set_pr_status(number, s["state"], s["decision"], s["merge_queue"])
    # Only after this poll: a stale CLOSED for a reopened PR would expire the comments queued since it reopened.
    store.expire_unposted()
    return len(states)


def watch_github(cfg, every_s=120):
    def loop():
        store = Store(cfg)
        while True:
            try:
                refresh_status(store, cfg)
            except Exception as exc:
                print(f"GitHub status refresh failed: {exc}", flush=True)
            time.sleep(every_s)

    thread = threading.Thread(target=loop, name="plus1-github-status", daemon=True)
    thread.start()
    return thread


TEAM_RANK = {team.TEAM: 0, team.OTHER: 1, "": 2}


def _team(parts):
    found = {p.get("team") for p in parts}
    return team.TEAM if team.TEAM in found else team.OTHER if team.OTHER in found else ""


def _rank(parts):
    # Within a group: the owner's team, then other teams they watch, then their own code.
    return TEAM_RANK[_team(parts)], not any(p.get("yours") for p in parts)


def entries(cards):
    stacks = {}
    for c in cards:
        if c["stack"]:
            stacks.setdefault(c["stack"][0], []).append(c)
    rows, seen = [], set()
    for c in cards:
        key = c["stack"][0] if c["stack"] and len(stacks[c["stack"][0]]) > 1 else None
        if key is None:
            rows.append(
                {
                    "kind": "card",
                    "card": c,
                    "group": c["group"],
                    "tone": c["tone"],
                    "key": (c["order"], *_rank([c]), c["when"] or ""),
                }
            )
        elif key not in seen:
            seen.add(key)
            parts = sorted(stacks[key], key=lambda p: p["stack"][1])
            # A stack files under its most urgent part, so one ready part can't hide a blocking one.
            lead = min(parts, key=lambda p: (URGENCY[p["group"]], p["when"] or ""))
            when = min(p["when"] or "" for p in parts)
            rows.append(
                {
                    "kind": "stack",
                    "ticket": key,
                    "parts": parts,
                    "total": parts[0]["stack"][2],
                    "group": lead["group"],
                    "tone": lead["tone"],
                    "when": when,
                    "creators": list(dict.fromkeys(p["creator"] for p in parts)),
                    "team_chip": words.TEAM_CHIP.get(_team(parts)),
                    "key": (lead["order"], *_rank(parts), when),
                }
            )
    rows.sort(key=lambda r: r["key"])
    return rows


def ordered(cards):
    return [c for r in entries(cards) for c in ([r["card"]] if r["kind"] == "card" else r["parts"])]


def neighbors(store, cfg, number, current=None):
    queue = ordered(todo(store, cfg, light=True) if current is None else current)
    prs = [c["pr"] for c in queue]
    if number not in prs:
        return {
            "prev": None,
            "next": queue[0] if queue else None,
            "position": None,
            "total": len(queue),
        }
    i = prs.index(number)
    return {
        "prev": queue[i - 1] if i else None,
        "next": queue[i + 1] if i + 1 < len(queue) else None,
        "position": i + 1,
        "total": len(queue),
    }


def card_for_pr(store, cfg, number):
    rows = store.query("SELECT * FROM reviews WHERE pr = ? ORDER BY id DESC LIMIT 1", number)
    return build(store, dict(rows[0]), cfg) if rows else None
