import json
import math
from datetime import UTC, datetime, timedelta

from . import config, gh, prs
from .store import now

TAGS = [
    "false approval",
    "missed issue",
    "false alarm",
    "wrong severity",
    "wrong lane",
    "comment text",
    "too cautious",
    "other",
]
GATE = {"days": 5, "graded_approvals": 30, "false_approvals": 0, "agreement": 0.9}
REASONS = {
    "wrong": ("disagree", "false alarm", "it was wrong"),
    "not_worth_it": ("disagree", "wrong severity", "right, but not worth a comment"),
    "handled": ("agree", None, "already handled"),
    "other": ("disagree", "other", "other"),
}


def eta(days, approval_days, graded_approvals, false_approvals, agreement_ok=True, window=5):
    if false_approvals > GATE["false_approvals"]:
        return None, "Can't finish while a false approval is on record."
    if not agreement_ok:
        return None, f"Agreement has to reach {int(GATE['agreement'] * 100)}% first."
    need_days = max(0, GATE["days"] - len(days))
    need_approvals = max(0, GATE["graded_approvals"] - graded_approvals)
    if not need_days and not need_approvals:
        return 0, ""
    recent = days[-window:]
    if len(recent) < 2:
        return None, "Not enough history to estimate yet."
    pace = sum(d in recent for d in approval_days) / len(recent)
    if need_approvals and not pace:
        return None, "No graded approvals in the last few working days."
    estimate = max(need_days, math.ceil(need_approvals / pace) if need_approvals else 0)
    return (
        estimate,
        f"About {estimate} working day{'s' if estimate != 1 else ''} at the recent pace.",
    )


REASON_ACTION = "chose not to post"


def grade_reason(store, review_id, reason):
    value, tag, note = REASONS[reason]
    return grade(store, review_id, value, "manual", tag=tag, note=note, andy_action=REASON_ACTION)


def clear_reason(store, review_id):
    store.db.execute(
        "DELETE FROM grades WHERE review_id = ? AND source = 'manual' AND andy_action = ?",
        (review_id, REASON_ACTION),
    )


def grade(store, review_id, value, source, tag=None, note=None, andy_action=None):
    existing = store.query("SELECT source FROM grades WHERE review_id = ?", review_id)
    if existing and existing[0]["source"] == "manual" and source != "manual":
        return False
    store.db.execute(
        """
        INSERT INTO grades (review_id, source, grade, tag, note, andy_action, at) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_id) DO UPDATE SET source = excluded.source, grade = excluded.grade, tag = excluded.tag,
          note = excluded.note, andy_action = excluded.andy_action,
          -- Kept while the grade holds, so a sync that finds the same answer again doesn't make it look new.
          at = CASE WHEN grades.grade IS excluded.grade AND grades.tag IS excluded.tag THEN grades.at ELSE excluded.at END
    """,
        (review_id, source, value, tag, note, andy_action, now()),
    )
    return True


def clear_grade(store, review_id):
    store.db.execute("DELETE FROM grades WHERE review_id = ?", (review_id,))


def infer_from_actions(store):
    """Grade reviews from what the owner did with them in the UI or CLI."""
    rows = store.query("""
        SELECT r.id, r.pr, r.sha, r.verdict,
               (SELECT GROUP_CONCAT(a.action) FROM actions a
                 WHERE a.pr = r.pr AND a.sha = r.sha AND a.ok = 1 AND a.actor IN ('ui', 'cli')) AS acts,
               (SELECT COUNT(*) FROM drafts d WHERE d.review_id = r.id AND d.body != d.original_body) AS edited
        FROM reviews r LEFT JOIN grades g ON g.review_id = r.id
        WHERE r.verdict IN ('APPROVE', 'DRAFT') AND (g.review_id IS NULL OR g.source = 'actions')
    """)
    changed = 0
    for r in rows:
        acts = set((r["acts"] or "").split(",")) - {""}
        result = None
        if r["verdict"] == "DRAFT" and "release" in acts:
            result = (
                "agree",
                None,
                "edited before release" if r["edited"] else None,
                "released the draft",
            )
        elif r["verdict"] == "DRAFT" and "dismiss" in acts:
            result = ("disagree", "false alarm", None, "dismissed the draft")
        elif r["verdict"] == "APPROVE" and "approve" in acts:
            result = ("agree", None, None, "approved from Plus One")
        if result and grade(
            store,
            r["id"],
            result[0],
            "actions",
            tag=result[1],
            note=result[2],
            andy_action=result[3],
        ):
            changed += 1
    return changed


def _mine_on_github(cfg, store, number, after):
    rcfg, n = config.at(cfg, number)
    bot_ids = {
        r[0]
        for r in store.query(
            "SELECT github_review_id FROM actions WHERE pr = ? AND github_review_id IS NOT NULL",
            number,
        )
    }
    reviews = json.loads(
        gh.run(["api", f"repos/{rcfg.repo}/pulls/{n}/reviews", "--paginate", "--slurp"])
    )
    reviews = [r for page in reviews for r in page]
    mine = [
        r
        for r in reviews
        if (r.get("user") or {}).get("login") == cfg.me
        and r["id"] not in bot_ids
        and r.get("submitted_at")
        and r["submitted_at"] >= after
    ]
    if not mine:
        return None
    ids = {r["id"] for r in mine}
    comments = json.loads(
        gh.run(["api", f"repos/{rcfg.repo}/pulls/{n}/comments", "--paginate", "--slurp"])
    )
    inline = sum(
        1
        for page in comments
        for c in page
        if c.get("pull_request_review_id") in ids and c.get("in_reply_to_id") is None
    )
    talked = inline > 0 or any((r.get("body") or "").strip() for r in mine)
    if any(r["state"] == "APPROVED" for r in mine):
        return "approved with comments" if talked else "approved"
    if any(r["state"] == "CHANGES_REQUESTED" for r in mine):
        return "changes requested"
    return "commented" if talked else None


DID = {
    "approved": "approved",
    "approved with comments": "approved with comments",
    "changes requested": "requested changes",
    "commented": "commented",
}


def sync_github(cfg, store, limit=25):
    since = (datetime.now().astimezone() - timedelta(days=14)).isoformat(timespec="seconds")
    rows = store.query(
        """
        SELECT r.id, r.pr, r.verdict, r.started_at FROM reviews r
        JOIN (SELECT pr, MAX(id) AS id FROM reviews WHERE verdict IN ('APPROVE', 'DRAFT') GROUP BY pr) last ON last.id = r.id
        LEFT JOIN grades g ON g.review_id = r.id
        WHERE r.started_at >= ? AND (g.review_id IS NULL OR g.source = 'github')
        ORDER BY r.id DESC LIMIT ?
    """,
        since,
        limit,
    )
    changed = 0
    for r in rows:
        utc_after = (
            datetime.fromisoformat(r["started_at"]).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        action = _mine_on_github(cfg, store, r["pr"], utc_after)
        if action is None:
            continue
        if r["verdict"] == "APPROVE":
            value, tag = (
                ("agree", None) if action.startswith("approved") else ("disagree", "false approval")
            )
        else:
            value, tag = ("disagree", "false alarm") if action == "approved" else ("agree", None)
        if grade(
            store, r["id"], value, "github", tag=tag, andy_action=f"you {DID[action]} on GitHub"
        ):
            changed += 1
    store.set_meta("github_sync_at", now())
    return changed


def stats(store, cfg, repo=None):
    """The checklist for approving on its own, for one repo's reviews when repo is given, since trust earned on one repo
    doesn't carry."""
    lo, hi = prs.bounds(repo) if repo else (0, 2**62)
    rows = [
        dict(r)
        for r in store.query(
            """
        SELECT r.id, r.pr, r.title, r.verdict, r.started_at, g.grade, g.tag, g.source, g.note, g.andy_action
        FROM reviews r LEFT JOIN grades g ON g.review_id = r.id
        WHERE r.verdict IS NOT NULL AND r.source != 'legacy' AND r.pr BETWEEN ? AND ?
        ORDER BY r.id DESC
    """,
            lo,
            hi,
        )
    ]
    graded = [r for r in rows if r["grade"]]
    agree = sum(1 for r in graded if r["grade"] == "agree")
    graded_approvals = [r for r in graded if r["verdict"] == "APPROVE"]
    false_approvals = [r for r in graded_approvals if r["grade"] == "disagree"]
    days = sorted(
        {
            r["started_at"][:10]
            for r in rows
            if datetime.fromisoformat(r["started_at"]).weekday() < 5
        }
    )
    agreement = agree / len(graded) if graded else 0.0
    checks = [
        {
            "key": "days",
            "label": f"{GATE['days']}+ working days of decisions",
            "value": len(days),
            "ok": len(days) >= GATE["days"],
        },
        {
            "key": "graded_approvals",
            "label": f"{GATE['graded_approvals']}+ graded approvals",
            "value": len(graded_approvals),
            "ok": len(graded_approvals) >= GATE["graded_approvals"],
        },
        {
            "key": "false_approvals",
            "label": "zero false approvals",
            "value": len(false_approvals),
            "ok": len(false_approvals) == GATE["false_approvals"],
        },
        {
            "key": "agreement",
            "label": f"{int(GATE['agreement'] * 100)}%+ agreement",
            "value": f"{agreement:.0%}" if graded else "n/a",
            "ok": bool(graded) and agreement >= GATE["agreement"],
        },
    ]
    by_verdict = {}
    for r in rows:
        v = by_verdict.setdefault(r["verdict"], {"total": 0, "graded": 0, "agree": 0})
        v["total"] += 1
        if r["grade"]:
            v["graded"] += 1
            v["agree"] += r["grade"] == "agree"
    approval_days = [r["started_at"][:10] for r in graded_approvals]
    return {
        "eta": eta(
            days,
            approval_days,
            len(graded_approvals),
            len(false_approvals),
            agreement_ok=not graded or agreement >= GATE["agreement"],
        ),
        "checks": checks,
        "ready": all(c["ok"] for c in checks),
        "approvals_to_go": max(0, GATE["graded_approvals"] - len(graded_approvals)),
        "graded": len(graded),
        "total": len(rows),
        "agreement": agreement,
        "by_verdict": by_verdict,
        "disagreements": [r for r in graded if r["grade"] == "disagree"][:50],
        "days": days,
    }


def short_of_approvals(store, cfg):
    """The repos whose checklist still needs graded approvals."""
    return {r.name for r in cfg.repos if stats(store, cfg, r.name)["approvals_to_go"]}


def queue(store, leave_out=(), short=()):
    """Reviews waiting for a grade, as their id and PR. Approvals in a repo whose checklist is short of graded ones come
    first; then comment drafts, the calls that would put words in the owner's name; then other approvals; then
    decisions left to the owner."""
    rows = [
        dict(r)
        for r in store.query("""
        SELECT r.id, r.pr, r.verdict FROM reviews r LEFT JOIN grades g ON g.review_id = r.id
        WHERE g.review_id IS NULL AND r.verdict IN ('APPROVE', 'DRAFT', 'DEFER') AND r.source != 'legacy'
          AND r.finished_at IS NOT NULL AND r.error IS NULL
        ORDER BY r.id DESC
    """)
    ]
    leave_out = set(leave_out)

    def rank(r):
        if r["verdict"] == "APPROVE":
            return 0 if prs.repo(r["pr"]) in short else 2
        return 1 if r["verdict"] == "DRAFT" else 3

    return sorted((r for r in rows if r["id"] not in leave_out), key=rank)
