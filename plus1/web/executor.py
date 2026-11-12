import json
import threading
import time
import traceback

from .. import actions, updates
from ..state import State
from ..store import Store, decode_json, now
from . import cards, words

UNDO_SECONDS = 8
STALE_AFTER_SECONDS = 120
KINDS = {
    "release",
    "approve",
    "approve_dismiss",
    "approve_anyway",
    "approve_followup",
    "dismiss",
    "handled",
    "keep_waiting",
    "drop",
    "ignore",
}
POSTS = {"release", "approve", "approve_dismiss", "approve_anyway", "approve_followup"}
NOT_POSTING = {"dismiss", "approve_dismiss", "approve_anyway"}


class PendingError(RuntimeError):
    pass


def schedule(store, kind, card, delay=UNDO_SECONDS):
    if kind not in KINDS:
        raise PendingError("Unknown action.")
    if cards.pending_for(store, card["pr"]):
        raise PendingError("Something is already waiting to happen on this PR.")
    return store.insert(
        "pending_actions",
        kind=kind,
        pr=card["pr"],
        review_id=card["review_id"],
        sha=card["sha"],
        payload={"title": card["title"]},
        created_at=now(),
        execute_at=time.time() + delay,
        status="pending",
    )


def undo(store, pending_id):
    cur = store.db.execute(
        "UPDATE pending_actions SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'pending'",
        (now(), pending_id),
    )
    return cur.rowcount == 1


def _execute(cfg, store, row):
    st = State(cfg)
    number, sha, kind = row["pr"], row["sha"], row["kind"]
    if kind == "release":
        actions.release(cfg, st, store, number, actor="ui")
    elif kind == "approve":
        actions.approve(
            cfg,
            st,
            store,
            number,
            sha,
            f"approved from Plus One (review {row['review_id']})",
            actor="ui",
        )
    elif kind == "approve_followup":
        actions.approve_followup(cfg, st, store, number, sha, row["review_id"], actor="ui")
    elif kind == "approve_dismiss":
        actions.dismiss(cfg, st, store, number, approve_after=True, actor="ui")
    elif kind == "approve_anyway":
        actions.approve(
            cfg, st, store, number, sha, "approved without posting the blocking comment", actor="ui"
        )
        actions.resolve_fixed(store, number, sha, row["review_id"], "ui")
        store.set_draft_status(number, sha, "dismissed")
        store.action("dismiss", "ui", pr=number, sha=sha)
    elif kind == "dismiss":
        actions.dismiss(cfg, st, store, number, approve_after=False, actor="ui")
    elif kind in ("handled", "keep_waiting"):
        store.insert(
            "handled",
            pr=number,
            sha=sha,
            review_id=row["review_id"],
            how=kind.replace("_", " "),
            at=now(),
        )
    elif kind == "drop":
        actions.drop(st, store, number, actor="ui")
    elif kind == "ignore":
        store.ignore(number, json.loads(row["payload"] or "{}").get("title"), row["review_id"])
        store.action("ignore", "ui", pr=number, sha=sha)
    # A comment still being written for this card would otherwise land after the owner finished with it.
    store.retire_writers(number)


def _review_again(cfg, store, row, error):
    # A paused tick exits before it reviews anything, so the card would wait on a review that never runs.
    if not words.HEAD_MOVED.search(error) or State(cfg).paused:
        return False
    # Whatever stops the review from starting, the refusal still has to reach its card and the owner.
    try:
        if not cards.reviewing(store, row["pr"]):
            actions.review_now(cfg, row["pr"])
    except Exception:
        traceback.print_exc()
        return False
    return True


def _tell(cfg, store, row, status, error=None, reviewing=False):
    # The page that scheduled this may be long closed, so a failure has to reach the owner some other way.
    what, why = words.action_failure(row["kind"], status, error, reviewing)
    try:
        actions.notify(
            cfg, f"{what} on #{row['pr']}", why, path=f"/review/{row['review_id']}", store=store
        )
    except Exception:
        traceback.print_exc()


def run_due(cfg, store, now_ts=None):
    now_ts = now_ts or time.time()
    ran = 0
    for row in store.query(
        "SELECT * FROM pending_actions WHERE status = 'pending' AND execute_at <= ? ORDER BY id",
        now_ts,
    ):
        if updates.restarting(store):
            return ran
        row = dict(row)
        if row["kind"] not in KINDS:
            # Saved by an older version for an action that no longer exists, so doing nothing must not read as done.
            store.update(
                "pending_actions",
                row["id"],
                status="cancelled",
                finished_at=now(),
                error="This action is no longer available.",
            )
            continue
        if now_ts - row["execute_at"] > STALE_AFTER_SECONDS:
            store.update(
                "pending_actions",
                row["id"],
                status="expired",
                finished_at=now(),
                error="The app was offline when this was due, so it was not done.",
            )
            _tell(cfg, store, row, "expired")
            continue
        claimed = store.db.execute(
            "UPDATE pending_actions SET status = 'running' WHERE id = ? AND status = 'pending'",
            (row["id"],),
        ).rowcount
        if not claimed:
            continue
        try:
            _execute(cfg, store, row)
            store.update("pending_actions", row["id"], status="done", finished_at=now())
        except Exception as exc:
            error = str(exc)
            # Started before the row leaves running, so the card keeps polling until it can say a review is coming.
            again = _review_again(cfg, store, row, error)
            store.update(
                "pending_actions",
                row["id"],
                status="failed",
                finished_at=now(),
                error=error[:500],
                payload={**(decode_json(row["payload"], None) or {}), "rereview": again},
            )
            _tell(cfg, store, row, "failed", error, again)
        ran += 1
    return ran


def last_result(store, number, within_s=600):
    # Once a newer review has finished it speaks for the PR, so an older review's outcome would mislabel its card.
    rows = store.query(
        """SELECT * FROM pending_actions p WHERE pr = ? AND status IN ('done', 'failed', 'expired', 'cancelled')
                          AND execute_at >= ? AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.pr = p.pr AND r.id > p.review_id
                                                              AND r.finished_at IS NOT NULL)
                          ORDER BY id DESC LIMIT 1""",
        number,
        time.time() - within_s,
    )
    return dict(rows[0]) if rows else None


def recover(store, cfg=None):
    error = "The app restarted while this was running"
    rows = [dict(r) for r in store.query("SELECT * FROM pending_actions WHERE status = 'running'")]
    store.db.execute(
        "UPDATE pending_actions SET status = 'failed', finished_at = ?, error = ? WHERE status = 'running'",
        (now(), error + ". Check GitHub before trying again."),
    )
    for row in rows if cfg else []:
        _tell(cfg, store, row, "failed", error)


def start(cfg):
    def loop():
        store = Store(cfg)
        recover(store, cfg)
        while True:
            try:
                run_due(cfg, store)
            except Exception:
                traceback.print_exc()
            time.sleep(1)

    thread = threading.Thread(target=loop, name="plus1-executor", daemon=True)
    thread.start()
    return thread


def describe(row):
    title = (
        json.loads(row["payload"] or "{}")
        if isinstance(row.get("payload"), str)
        else row.get("payload") or {}
    ).get("title", "")
    return {
        "release": ("Posting the comment", "Posted the comment"),
        "approve": ("Approving", "Approved"),
        "approve_dismiss": ("Approving without the comment", "Approved without the comment"),
        "approve_anyway": ("Approving without the comment", "Approved without the comment"),
        "dismiss": ("Not posting the comment", "Comment not posted"),
        "handled": ("Marking as handled", "Marked as handled"),
        "keep_waiting": (
            "Keeping your request for changes",
            "Kept your request for changes. The next push brings it back",
        ),
        "approve_followup": (
            "Approving and resolving your fixed comments",
            "Approved, and resolved your fixed comments",
        ),
        "drop": ("Sending back for another review", "Sent back. The next check reviews it again"),
        "ignore": ("Handing off", "Handed off. Checks skip it until you bring it back"),
    }.get(row["kind"], ("Working on it", "Done")), title
