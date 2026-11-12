import contextlib
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import config, gh, host, prs
from .drafts import DRAFT_FAILED
from .store import decode_json, now

ROOT = Path(__file__).resolve().parent.parent


class ActionError(RuntimeError):
    pass


def notify(cfg, title, body, path="/", store=None):
    if not cfg.notify:
        return
    url = cfg.web.public_url.rstrip("/") + path
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        host.notify(title, body, url)
    # Web Push is best effort: a dead subscription or a push service outage must not fail the action it reports.
    with contextlib.suppress(Exception):
        from . import push
        from .store import Store

        push.send_all(cfg, store or Store(cfg), title, body, url)


def queued(store, number):
    return [
        dict(r)
        for r in store.query(
            "SELECT * FROM drafts WHERE pr = ? AND status = 'queued' ORDER BY id", number
        )
    ]


def release_payload(store, number):
    drafts = queued(store, number)
    if not drafts:
        raise ActionError(f"Nothing is queued for #{number}.")
    shas = {d["sha"] for d in drafts}
    if len(shas) > 1:
        raise ActionError(
            f"Drafts for #{number} span more than one commit. Drop them and re-review."
        )
    has_p0 = any(d["severity"] == "P0" for d in drafts)
    # While some of the owner's earlier asks are still open, new non-blocking comments mustn't lift their block.
    waiting = any(
        i["open"] for i in (followup_of(store, drafts[0]["review_id"]) or {}).get("items", [])
    )
    return {
        "number": number,
        "sha": shas.pop(),
        "event": "REQUEST_CHANGES" if has_p0 else "COMMENT" if waiting else "APPROVE",
        "has_p0": has_p0,
        "body": "\n\n".join(d["body"] for d in drafts if not d["inline"]),
        "comments": [
            {"path": d["path"], "line": d["line"], "body": d["body"]} for d in drafts if d["inline"]
        ],
        "drafts": drafts,
    }


def followup_of(store, review_id):
    rows = store.query("SELECT followup FROM reviews WHERE id = ?", review_id)
    return decode_json(rows[0]["followup"], None) if rows else None


def resolve_fixed(store, number, sha, review_id, actor):
    followup = followup_of(store, review_id) or {}
    for item in followup.get("items", []):
        if item["open"] or item.get("resolved") or not item.get("thread_id"):
            continue
        # The approval already stands, so a thread that won't resolve is recorded rather than raised.
        try:
            gh.resolve_thread(item["thread_id"])
        except Exception as exc:
            store.action(
                "resolve_thread",
                actor,
                pr=number,
                sha=sha,
                payload={"thread_id": item["thread_id"]},
                ok=False,
                error=str(exc)[:500],
            )
        else:
            store.action(
                "resolve_thread",
                actor,
                pr=number,
                sha=sha,
                payload={"thread_id": item["thread_id"]},
            )


def _head_check(cfg, number, sha):
    rcfg, n = config.at(cfg, number)
    current = gh.head_sha(rcfg, n)
    if current != sha:
        raise ActionError(
            f"#{n} has new commits ({sha[:8]} is now {current[:8]}). Drop the draft and re-review."
        )


def _post(cfg, st, store, action, actor, number, sha, event, body="", comments=(), reason=None):
    payload = {"body": body, "comments": list(comments), "reason": reason}
    try:
        rcfg, n = config.at(cfg, number)
        response = gh.post_review(rcfg, n, sha, event, body=body, comments=comments)
    except Exception as exc:
        store.action(
            action,
            actor,
            pr=number,
            sha=sha,
            event=event,
            payload=payload,
            ok=False,
            error=str(exc)[:500],
        )
        raise
    store.action(
        action,
        actor,
        pr=number,
        sha=sha,
        event=event,
        payload=payload,
        github_review_id=response.get("id"),
    )
    st.audit(
        "approve_posted" if event == "APPROVE" else "review_posted",
        pr=number,
        sha=sha,
        reason=reason,
        actor=actor,
    )
    return response


def approve(cfg, st, store, number, sha, reason, actor="cli"):
    _head_check(cfg, number, sha)
    return _post(cfg, st, store, "approve", actor, number, sha, "APPROVE", reason=reason)


def approve_followup(cfg, st, store, number, sha, review_id, actor="ui"):
    approve(
        cfg, st, store, number, sha, f"approved after a follow-up (review {review_id})", actor=actor
    )
    resolve_fixed(store, number, sha, review_id, actor)


def release(cfg, st, store, number, actor="cli", force=False):
    p = release_payload(store, number)
    unfit = [d for d in p["drafts"] if d["needs_edit"]]
    if unfit and not force:
        raise ActionError(f"{len(unfit)} comment(s) are flagged needs edit. Edit them first.")
    _head_check(cfg, number, p["sha"])
    _post(
        cfg,
        st,
        store,
        "release",
        actor,
        number,
        p["sha"],
        p["event"],
        body=p["body"],
        comments=p["comments"],
        reason="draft released",
    )
    store.set_draft_status(number, p["sha"], "released")
    if p["event"] == "APPROVE":
        resolve_fixed(store, number, p["sha"], p["drafts"][0]["review_id"], actor)
    return p["event"]


def dismiss(cfg, st, store, number, approve_after=True, actor="cli"):
    p = release_payload(store, number)
    if approve_after and not p["has_p0"]:
        approve(cfg, st, store, number, p["sha"], "P1 draft dismissed", actor=actor)
        resolve_fixed(store, number, p["sha"], p["drafts"][0]["review_id"], actor)
        result = "approved"
    else:
        result = "dismissed"
    store.set_draft_status(number, p["sha"], "dismissed")
    store.action("dismiss", actor, pr=number, sha=p["sha"])
    return result


def drop(st, store, number, actor="cli"):
    for d in queued(store, number):
        store.set_draft_status(number, d["sha"], "dropped")
    st.forget(number)
    store.action("drop", actor, pr=number)


def _queued_draft(store, draft_id, review_id):
    rows = store.query("SELECT * FROM drafts WHERE id = ?", draft_id)
    if not rows or rows[0]["status"] != "queued":
        raise ActionError("That draft is no longer queued.")
    if review_id is not None and rows[0]["review_id"] != review_id:
        raise ActionError("That comment belongs to an older review.")
    return rows[0]


def update_draft(store, draft_id, body, review_id=None, actor="ui"):
    draft = _queued_draft(store, draft_id, review_id)
    body = body.strip()
    if not body:
        raise ActionError("A comment cannot be empty.")
    store.update("drafts", draft_id, body=body, needs_edit=0, updated_at=now())
    store.action(
        "edit_draft", actor, pr=draft["pr"], sha=draft["sha"], payload={"draft_id": draft_id}
    )


def allow_draft(store, draft_id, review_id=None, actor="ui"):
    draft = _queued_draft(store, draft_id, review_id)
    try:
        unsupported = json.loads(draft["unsupported"] or "[]")
    except json.JSONDecodeError:
        unsupported = [draft["unsupported"]]
    if unsupported == [DRAFT_FAILED]:
        raise ActionError("The comment writer failed on this one. Edit it before posting.")
    store.update("drafts", draft_id, needs_edit=0, updated_at=now())
    store.action(
        "factcheck_false_alarm",
        actor,
        pr=draft["pr"],
        sha=draft["sha"],
        payload={"draft_id": draft_id, "unsupported": unsupported},
    )


def spawn(cfg, name, *args):
    try:
        return host.spawn(cfg.state_dir, name, args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActionError(f"Could not start {name}: {exc}") from exc


def review_now(cfg, number):
    repo, n = prs.split(number)
    return spawn(
        cfg,
        f"review-{number}",
        "tick",
        "--pr",
        f"{repo}#{n}",
        "--any-time",
        "--force",
        "--trigger",
        "ui",
        "--wait-lock",
    )


WRITING_MINUTES = 15


STALLED = "The writer stopped before it finished."


def stalled(row):
    age = datetime.now().astimezone() - datetime.fromisoformat(row["updated_at"])
    return row["status"] == "writing" and age > timedelta(minutes=WRITING_MINUTES)


def write_ups(store, review_id):
    # The spawned writer can die without touching its row, so one quiet past the limit counts as failed.
    out = {}
    for row in store.query(
        "SELECT * FROM drafts WHERE review_id = ? AND status IN ('writing', 'failed') ORDER BY id",
        review_id,
    ):
        d = dict(row)
        if stalled(d):
            d["status"], d["body"] = "failed", STALLED
        out[d["finding_id"]] = d
    return out


def write_up(cfg, store, review_id, finding_id):
    rows = store.query(
        """SELECT f.*, r.pr, r.sha FROM findings f JOIN reviews r ON r.id = f.review_id
                          WHERE f.id = ? AND f.review_id = ? AND f.verified = 0 AND f.carried IS NULL""",
        finding_id,
        review_id,
    )
    if not rows:
        raise ActionError("That finding isn't on this review anymore.")
    f = rows[0]
    # Fail a stalled attempt in the table, not just on the card, so its writer can't queue a second comment
    # if it finishes after this retry.
    for row in store.query(
        "SELECT * FROM drafts WHERE finding_id = ? AND status = 'writing'", finding_id
    ):
        if stalled(row):
            store.db.execute(
                "UPDATE drafts SET status = 'failed', body = ?, updated_at = ? WHERE id = ? AND status = 'writing'",
                (STALLED, now(), row["id"]),
            )
    try:
        draft_id = store.insert(
            "drafts",
            review_id=review_id,
            finding_id=finding_id,
            pr=f["pr"],
            sha=f["sha"],
            severity=f["severity"],
            path=f["final_path"] or f["path"],
            line=f["final_line"] or f["line"],
            status="writing",
            updated_at=now(),
        )
    except sqlite3.IntegrityError:
        raise ActionError("That comment is already being written.") from None
    try:
        spawn(cfg, f"write-up-{f['pr']}", "write-up", draft_id)
    except (ActionError, OSError, subprocess.SubprocessError) as exc:
        store.update("drafts", draft_id, status="failed", body=str(exc)[:300], updated_at=now())
        if isinstance(exc, ActionError):
            raise
        raise ActionError(f"Could not start the writer: {exc}") from exc
    return draft_id


SUGGESTION_CHARS = 500
# The box holds SUGGESTION_CHARS, and the chips need room on top of a full one.
CHIP_ROOM = 100


def rewrites(store, draft_ids):
    """Each draft's rewrite in progress, the proposal waiting on the owner, and the error when the latest failed."""
    out = {}
    for row in store.rewrites_of(draft_ids):
        r = dict(row)
        r["unsupported"] = decode_json(r["unsupported"], []) or []
        if stalled(r):
            r["status"], r["error"] = "failed", STALLED
        state = out.setdefault(r["draft_id"], {"writing": None, "ready": None, "error": None})
        if r["status"] in ("writing", "ready"):
            state[r["status"]] = r
        # Only the newest row speaks for the draft, so an old failure goes once anything happens after it.
        state["error"] = r["error"] if r["status"] == "failed" else None
    return out


def rewrite(cfg, store, draft_id, suggestion, review_id=None, parent_id=None, actor="ui"):
    draft = _queued_draft(store, draft_id, review_id)
    suggestion = " ".join(suggestion.split())
    if not suggestion:
        raise ActionError("Say how the comment should change.")
    if len(suggestion) > SUGGESTION_CHARS + CHIP_ROOM:
        raise ActionError(f"Keep it under {SUGGESTION_CHARS} characters.")
    if draft["finding_id"] is None:
        raise ActionError(
            "This comment has no finding to check a rewrite against. Edit it by hand."
        )
    for row in store.query(
        "SELECT * FROM rewrites WHERE draft_id = ? AND status = 'writing'", draft_id
    ):
        if stalled(row):
            store.db.execute(
                "UPDATE rewrites SET status = 'failed', error = ?, updated_at = ? WHERE id = ? AND status = 'writing'",
                (STALLED, now(), row["id"]),
            )
    if parent_id is None:
        base, notes = draft["body"], decode_json(draft["notes"], []) or []
    else:
        rows = store.query(
            "SELECT * FROM rewrites WHERE id = ? AND draft_id = ? AND status = 'ready'",
            parent_id,
            draft_id,
        )
        if not rows:
            raise ActionError("That rewrite isn't waiting anymore.")
        base, notes = rows[0]["body"], decode_json(rows[0]["notes"], []) or []
    try:
        rewrite_id = store.insert(
            "rewrites",
            draft_id=draft_id,
            parent_id=parent_id,
            suggestion=suggestion,
            notes=[*notes, suggestion],
            base=base,
            status="writing",
            created_at=now(),
            updated_at=now(),
        )
    except sqlite3.IntegrityError:
        raise ActionError("This comment is already being rewritten.") from None
    try:
        spawn(cfg, f"rewrite-{draft['pr']}", "rewrite", rewrite_id)
    except (ActionError, OSError, subprocess.SubprocessError) as exc:
        store.update(
            "rewrites", rewrite_id, status="failed", error=str(exc)[:300], updated_at=now()
        )
        if isinstance(exc, ActionError):
            raise
        raise ActionError(f"Could not start the rewrite: {exc}") from exc
    store.action(
        "rewrite_draft",
        actor,
        pr=draft["pr"],
        sha=draft["sha"],
        payload={"draft_id": draft_id, "rewrite_id": rewrite_id, "suggestion": suggestion},
    )
    return rewrite_id


def use_rewrite(store, draft_id, rewrite_id, review_id=None, actor="ui"):
    draft = _queued_draft(store, draft_id, review_id)
    if not store.use_rewrite(rewrite_id, draft_id):
        raise ActionError("That rewrite isn't waiting anymore.")
    store.action(
        "use_rewrite",
        actor,
        pr=draft["pr"],
        sha=draft["sha"],
        payload={"draft_id": draft_id, "rewrite_id": rewrite_id},
    )


def keep_draft(store, draft_id, rewrite_id, review_id=None, actor="ui"):
    draft = _queued_draft(store, draft_id, review_id)
    if not store.discard_rewrite(rewrite_id, draft_id):
        raise ActionError("That rewrite isn't waiting anymore.")
    store.action(
        "keep_draft",
        actor,
        pr=draft["pr"],
        sha=draft["sha"],
        payload={"draft_id": draft_id, "rewrite_id": rewrite_id},
    )


def restore_draft(store, draft_id, review_id=None, actor="ui"):
    draft = _queued_draft(store, draft_id, review_id)
    if draft["body"] == draft["original_body"] and not decode_json(draft["notes"], None):
        raise ActionError("This is already the first draft.")
    if not store.restore_original(draft_id):
        raise ActionError("That draft is no longer queued.")
    store.action(
        "restore_draft", actor, pr=draft["pr"], sha=draft["sha"], payload={"draft_id": draft_id}
    )


def make_rule(cfg, store, draft_id, note, review_id=None):
    draft = _queued_draft(store, draft_id, review_id)
    if not decode_json(draft["notes"], None):
        raise ActionError("Only a comment you had rewritten can become a rule.")
    note = note.strip()[:1000]
    if not note:
        raise ActionError("Say what future comments should do.")
    try:
        proposal_id = store.insert(
            "proposals",
            review_id=draft["review_id"],
            draft_id=draft_id,
            # The texts as the owner saw them, since the comment can change before the proposer runs.
            comments={"drafted": draft["original_body"], "kept": draft["body"]},
            kind="rewrite",
            created_at=now(),
            status="pending",
            note=note,
        )
    except sqlite3.IntegrityError:
        raise ActionError("A rule from this comment is already being drafted.") from None
    _run_proposal(cfg, store, proposal_id)
    return proposal_id


def start_proposal(cfg, store, review_id, note, kind="review"):
    proposal_id = store.insert(
        "proposals", review_id=review_id, kind=kind, created_at=now(), status="pending", note=note
    )
    _run_proposal(cfg, store, proposal_id)
    return proposal_id


def _run_proposal(cfg, store, proposal_id):
    try:
        spawn(cfg, f"propose-{proposal_id}", "propose", proposal_id)
    except (ActionError, OSError, subprocess.SubprocessError) as exc:
        store.update("proposals", proposal_id, status="failed", error=str(exc)[:500])
        if isinstance(exc, ActionError):
            raise
        raise ActionError(f"Could not start the rubric change: {exc}") from exc


OVERRULED = {
    "false approval": "Plus One would have approved this, and {did}.",
    "false alarm": "Plus One drafted comments on this, and {did} without any.",
}


def learn_from_github(cfg, store):
    """Drafts a rubric change for each recent review the owner overruled on GitHub, up to a few a day."""
    today = datetime.now().astimezone().date().isoformat()
    # Only recent grades, so turning this on doesn't draft a change for every old disagreement at once.
    since = (datetime.now().astimezone() - timedelta(days=3)).isoformat(timespec="seconds")
    # Claimed in one write transaction, so a timer tick and the Insights button can't both take the same slots.
    store.db.execute("BEGIN IMMEDIATE")
    try:
        made = store.query(
            "SELECT COUNT(*) FROM proposals WHERE kind IN ('github', 'github-extra') AND created_at >= ?",
            today,
        )[0][0]
        rows = store.query(
            """
            SELECT g.review_id, g.tag, g.andy_action FROM grades g
            WHERE g.source = 'github' AND g.grade = 'disagree' AND g.at >= ?
              AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.review_id = g.review_id)
            ORDER BY g.at DESC LIMIT ?
        """,
            since,
            max(0, cfg.limits.auto_proposals_per_day - made),
        )
        claimed = []
        for r in rows:
            note = OVERRULED.get(
                r["tag"], "Plus One's call and yours on GitHub differed: {did}."
            ).format(did=r["andy_action"])
            claimed.append(
                store.insert(
                    "proposals",
                    review_id=r["review_id"],
                    kind="github",
                    created_at=now(),
                    status="pending",
                    note=note,
                )
            )
        store.db.execute("COMMIT")
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
    started = []
    for proposal_id in claimed:
        try:
            _run_proposal(cfg, store, proposal_id)
            started.append(proposal_id)
        except ActionError:
            pass
    return started
