import contextlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import calibrate, config, diffs, host, units
from ..cli import in_window
from ..drafts import DRAFT_FAILED
from ..prs import BASE as PR_BASE
from ..store import decode_json
from . import words

JSON_COLUMNS = {"labels", "files", "raw_review", "p2_notes", "unsupported", "payload", "sensitive"}

_timer_cache = {"at": 0.0, "value": None}


def _decoded(row):
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        out[key] = decode_json(out[key], out[key])
    return out


def _row(row):
    return None if row is None else _decoded(row)


def _rows(rows):
    return [_decoded(r) for r in rows]


def today_prefix():
    return datetime.now().astimezone().date().isoformat()


def timer_status():
    """Whether install set up the timer, and whether systemd is running a check now. Cached for a few seconds."""
    if time.monotonic() - _timer_cache["at"] < 5 and _timer_cache["value"]:
        return _timer_cache["value"]
    running = False
    if host.systemd():
        proc = subprocess.run(
            ["systemctl", "--user", "show", "plus1.service", "-p", "ActiveState", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        running = proc.stdout.strip() in ("activating", "active")
    value = {"active": units.active(), "running": running}
    _timer_cache.update(at=time.monotonic(), value=value)
    return value


def next_check_at(cfg, now=None):
    """When the timer's next check falls inside the working window, in the configured zone. The timer fires every half
    hour on this machine's clock, and a check outside the window, which is in the configured zone, exits at once."""
    zone = ZoneInfo(cfg.timezone)
    now = now or datetime.now().astimezone()
    at = now.replace(second=0, microsecond=0) + timedelta(minutes=30 - now.minute % 30)
    for _ in range(48 * 8):
        there = at.astimezone(zone)
        if in_window(cfg, there):
            return there
        at += timedelta(minutes=30)
    return None


def next_check(cfg, now=None):
    now = now or datetime.now().astimezone()
    there = next_check_at(cfg, now)
    if there is None:
        return None
    same_day = there.date() == now.astimezone(there.tzinfo).date()
    return there.strftime("%H:%M") if same_day else there.strftime("%a %H:%M")


def status(cfg, store, st):
    timer = timer_status()
    last_run = _row(store.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone())
    open_run = _row(
        store.db.execute(
            "SELECT * FROM runs WHERE finished_at IS NULL AND started_at >= ? ORDER BY id DESC LIMIT 1",
            ((datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds"),),
        ).fetchone()
    )
    # Every model call today, the same figure the daily cap counts.
    spend = store.spend_today()
    approvals = store.db.execute(
        "SELECT COUNT(*) FROM actions WHERE action = 'approve' AND actor = 'timer' AND ok = 1 AND at LIKE ?",
        (today_prefix() + "%",),
    ).fetchone()[0]
    slack_row = store.db.execute(
        "SELECT slack_ok FROM runs WHERE slack_ok IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    slack_down = store.db.execute("""SELECT MIN(started_at) FROM runs WHERE slack_ok = 0
        AND id > COALESCE((SELECT MAX(id) FROM runs WHERE slack_ok = 1), 0)""").fetchone()[0]
    return {
        "auto_repos": sorted(config.auto_repos(cfg)),
        "paused": st.paused,
        "running": timer["running"] or open_run is not None,
        "timer_enabled": timer["active"],
        "next_run": next_check(cfg) if timer["active"] else None,
        "last_run": last_run,
        "spend_today": spend or 0.0,
        "spend_cap": cfg.limits.max_spend_per_day_usd,
        "spend_capped": spend >= cfg.limits.max_spend_per_day_usd,
        "approvals_today": approvals,
        "approvals_cap": cfg.limits.max_approvals_per_day,
        "slack_ok": None if slack_row is None else bool(slack_row[0]),
        "slack_enabled": cfg.slack.enabled,
        "slack_down_since": slack_down,
    }


def latest_observed_sha(store, number):
    row = store.db.execute(
        "SELECT sha FROM observations WHERE pr = ? AND sha IS NOT NULL ORDER BY id DESC LIMIT 1",
        (number,),
    ).fetchone()
    return row[0] if row else None


def _number(q):
    """The GitHub number in a search like 123, #123, or api#123, else -1, which matches nothing."""
    tail = q.strip().rpartition("#")[2]
    return int(tail) if tail.isdigit() else -1


def history(store, q="", verdict="", lane="", source="", page=1, per_page=50):
    where, params = ["1 = 1"], []
    if q:
        like = f"%{q}%"
        where.append(
            "(r.title LIKE ? OR r.reason LIKE ? OR r.author LIKE ? OR r.pr % {PR_BASE} = ?)"
        )
        params += [like, like, like, _number(q)]
    if verdict:
        where.append("r.verdict = ?")
        params.append(verdict)
    if lane:
        where.append("r.lane = ?")
        params.append(lane)
    if source:
        where.append("r.source = ?")
        params.append(source)
    total = store.db.execute(
        f"SELECT COUNT(*) FROM reviews r WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    rows = _rows(
        store.db.execute(
            f"""
        SELECT r.id, r.pr, r.sha, r.title, r.author, r.url, r.verdict, r.reason, r.lane, r.source,
               r.cost_usd, r.started_at, r.finished_at, r.error,
               (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.verified = 1) AS verified,
               (SELECT COUNT(*) FROM findings f WHERE f.review_id = r.id AND f.verified = 0) AS dropped,
               g.grade
        FROM reviews r LEFT JOIN grades g ON g.review_id = r.id
        WHERE {" AND ".join(where)}
        ORDER BY r.id DESC LIMIT ? OFFSET ?
    """,
            [*params, per_page, (page - 1) * per_page],
        )
    )
    return {"rows": rows, "total": total, "page": page, "pages": max(1, -(-total // per_page))}


def stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def skips(store, q="", limit=200):
    where, params = "o.outcome IN ('skip', 'error', 'waiting')", []
    if q:
        where += f" AND (o.title LIKE ? OR o.pr % {PR_BASE} = ?)"
        params += [f"%{q}%", _number(q)]
    rows = _rows(
        store.db.execute(
            f"""
        SELECT o.* FROM observations o
        JOIN (SELECT pr, MAX(id) AS id FROM observations GROUP BY pr) last ON last.id = o.id
        WHERE {where} ORDER BY o.id DESC LIMIT ?""",
            [*params, limit],
        )
    )
    for row in rows:
        row["plain"] = words.skip_reason(row["reason"])
    return rows


ACTIVITY_SQL = """
WITH base AS (
  SELECT r.id, r.pr, r.sha, r.title, r.author, r.requested_by, r.labels, r.verdict, r.reason, r.lane, r.cost_usd,
         r.started_at, r.finished_at, r.error, r.source,
         r.id = (SELECT MAX(id) FROM reviews x WHERE x.pr = r.pr) AS is_latest,
         (SELECT COUNT(*) FROM drafts d WHERE d.review_id = r.id AND d.status = 'queued') AS waiting,
         (SELECT COUNT(*) FROM drafts d WHERE d.review_id = r.id AND d.status = 'released') AS released,
         (SELECT COUNT(*) FROM drafts d WHERE d.review_id = r.id AND d.status IN ('dismissed', 'dropped')) AS not_posted,
         (SELECT COUNT(*) FROM drafts d WHERE d.review_id = r.id AND d.status = 'expired') AS expired,
         (SELECT s.state FROM pr_status s WHERE s.pr = r.pr) AS pr_state,
         (SELECT COUNT(*) FROM drafts d WHERE d.review_id = r.id AND d.severity = 'P0') AS p0,
         EXISTS (SELECT 1 FROM actions a WHERE a.pr = r.pr AND a.sha = r.sha AND a.action = 'approve' AND a.ok = 1) AS approved,
         (SELECT a.actor FROM actions a WHERE a.pr = r.pr AND a.sha = r.sha AND a.action = 'approve' AND a.ok = 1 LIMIT 1) AS approved_by,
         EXISTS (SELECT 1 FROM handled h WHERE h.pr = r.pr AND h.sha = r.sha AND (h.review_id IS NULL OR h.review_id >= r.id)) AS handled,
         EXISTS (SELECT 1 FROM ignored i WHERE i.pr = r.pr) AS handed_off,
         g.grade
  FROM reviews r LEFT JOIN grades g ON g.review_id = r.id
)
SELECT * FROM base WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?
"""


def _after(row):
    if row["released"]:
        return "You posted the comment", "ok"
    if row["approved"]:
        return (
            "Approved automatically" if row["approved_by"] == "timer" else "You approved it"
        ), "ok"
    if row["not_posted"]:
        return "Not posted", "muted"
    if row["expired"]:
        return (
            "Merged" if row["pr_state"] == "MERGED" else "Closed"
        ) + " before you posted", "muted"
    if row["handled"]:
        return "You handled it", "muted"
    if row["handed_off"]:
        return "You handed it off", "muted"
    if not row["is_latest"]:
        return "Replaced by a newer review", "muted"
    if row["error"]:
        return "Couldn't finish", "bad"
    if row["waiting"] or row["verdict"] in ("DEFER", "APPROVE"):
        return "Waiting on you", "warn"
    return "", "muted"


def _label(row):
    if not row["finished_at"] and not row["error"]:
        row["outcome"], row["tone"] = "Reviewing now", "info"
        row["after"], row["after_tone"] = "", "muted"
        return row
    if row["error"]:
        row["outcome"], row["tone"] = "Couldn't review", "bad"
    elif row["verdict"] == "DRAFT":
        row["outcome"], row["tone"] = (
            ("Found a blocking problem", "bad") if row["p0"] else ("Worth fixing", "warn")
        )
    else:
        row["outcome"] = words.OUTCOME.get(row["verdict"], "")
        row["tone"] = words.VERDICT_TONE.get(row["verdict"], "muted")
    row["after"], row["after_tone"] = _after(row)
    return row


def activity(store, q="", show="all", page=1, per_page=40):
    where, params = ["1 = 1"], []
    match = f"(title LIKE ? OR reason LIKE ? OR author LIKE ? OR requested_by LIKE ? OR pr % {PR_BASE} = ?)"
    if q:
        # Everything shows each PR once, so a match on any of its reviews brings up the latest.
        where.append(f"pr IN (SELECT pr FROM base WHERE {match})" if show == "all" else match)
        params += [f"%{q}%"] * 4 + [_number(q)]
    if show == "all":
        where.append("is_latest")
    elif show == "posted":
        where.append("(released > 0 OR approved)")
    elif show == "not_posted":
        where.append("not_posted > 0")
    elif show == "waiting":
        where.append(
            "is_latest AND error IS NULL AND NOT approved AND NOT handled AND NOT handed_off AND released = 0 AND not_posted = 0 "
            "AND (waiting > 0 OR verdict = 'DEFER' OR verdict = 'APPROVE')"
        )
    sql = ACTIVITY_SQL.format(where=" AND ".join(where))
    rows = [
        _label(row)
        for row in _rows(store.db.execute(sql, [*params, per_page + 1, (page - 1) * per_page]))
    ]
    more, rows = len(rows) > per_page, rows[:per_page]
    if show == "all" and rows:
        prs = sorted({r["pr"] for r in rows})
        older = {}
        sql = ACTIVITY_SQL.format(where=f"pr IN ({','.join('?' * len(prs))}) AND NOT is_latest")
        for row in _rows(store.db.execute(sql, [*prs, -1, 0])):
            older.setdefault(row["pr"], []).append(_label(row))
        for row in rows:
            row["older"] = older.get(row["pr"], [])
    return {"rows": rows, "page": page, "more": more}


def fold_quiet(runs):
    out = []
    for run in runs:
        quiet = run["finished_at"] and not any(
            run.get(k) for k in ("reviewed", "defers", "drafts", "approved", "errors", "cost_usd")
        )
        if quiet and out and out[-1].get("quiet"):
            out[-1]["count"] += 1
            out[-1]["first"] = run["started_at"]
        elif quiet:
            out.append(
                {
                    "quiet": True,
                    "count": 1,
                    "first": run["started_at"],
                    "last": run["started_at"],
                    "run": run,
                }
            )
        else:
            out.append(run)
    return [o["run"] if o.get("quiet") and o["count"] == 1 else o for o in out]


def review_detail(store, review_id):
    review = _row(store.db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone())
    if review is None:
        return None
    review["findings"] = _rows(
        store.db.execute("SELECT * FROM findings WHERE review_id = ? ORDER BY idx", (review_id,))
    )
    review["drafts"] = _rows(
        store.db.execute("SELECT * FROM drafts WHERE review_id = ? ORDER BY id", (review_id,))
    )
    review["stages"] = _rows(
        store.db.execute("SELECT * FROM stages WHERE review_id = ? ORDER BY id", (review_id,))
    )
    review["events"] = _rows(
        store.db.execute("SELECT * FROM events WHERE review_id = ? ORDER BY id", (review_id,))
    )
    review["grade"] = _row(
        store.db.execute("SELECT * FROM grades WHERE review_id = ?", (review_id,)).fetchone()
    )
    review["actions"] = _rows(
        store.db.execute(
            "SELECT * FROM actions WHERE pr = ? AND sha = ? ORDER BY id",
            (review["pr"], review["sha"]),
        )
    )
    review["latest_sha"] = latest_observed_sha(store, review["pr"])
    return review


def pr_story(store, number):
    return {
        "number": number,
        "reviews": _rows(
            store.db.execute("SELECT * FROM reviews WHERE pr = ? ORDER BY id DESC", (number,))
        ),
        "observations": _rows(
            store.db.execute(
                """
            SELECT o.*, r.trigger FROM observations o JOIN runs r ON r.id = o.run_id
            WHERE o.pr = ? ORDER BY o.id DESC LIMIT 200""",
                (number,),
            )
        ),
        "actions": _rows(
            store.db.execute("SELECT * FROM actions WHERE pr = ? ORDER BY id DESC", (number,))
        ),
        "drafts": _rows(
            store.db.execute("SELECT * FROM drafts WHERE pr = ? ORDER BY id DESC", (number,))
        ),
    }


def runs(store, limit=50):
    return _rows(store.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))


def daily_spend(store, days=14):
    today = datetime.now().astimezone().date()
    span = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    rows = {
        r["day"]: dict(r)
        for r in store.db.execute(
            """
        SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS reviews, COALESCE(SUM(cost_usd), 0) AS cost
        FROM reviews WHERE started_at >= ? GROUP BY day ORDER BY day
    """,
            (span[0],),
        ).fetchall()
    }
    late = late_spend(store, span[0])
    out = []
    for day in span:
        row = rows.get(day, {"day": day, "reviews": 0, "cost": 0.0})
        out.append({**row, "cost": row["cost"] + late.get(day, 0.0)})
    return out


def late_spend(store, since):
    # A review's total covers what ran during its tick. A model call after it finished (Write it up) counts on the
    # day it ran, so it lands on today's spend rather than the review's start day.
    return {
        r["day"]: r["cost"]
        for r in store.db.execute(
            """
        SELECT substr(s.started_at, 1, 10) AS day, COALESCE(SUM(s.cost_usd), 0) AS cost
        FROM stages s JOIN reviews r ON r.id = s.review_id
        WHERE r.finished_at IS NOT NULL AND s.started_at > r.finished_at AND s.started_at >= ?
        GROUP BY day""",
            (since,),
        ).fetchall()
    }


def palette(store, limit=80):
    return _rows(
        store.db.execute(
            """
        SELECT r.id, r.pr, r.title, r.author, r.verdict, r.error FROM reviews r
        JOIN (SELECT pr, MAX(id) AS id FROM reviews GROUP BY pr) last ON last.id = r.id
        ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        )
    )


def stage_costs(store, days=7):
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = store.db.execute(
        """
        SELECT stage, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost,
               COALESCE(AVG(duration_s), 0) AS avg_s, SUM(CASE WHEN ok THEN 0 ELSE 1 END) AS failures
        FROM stages WHERE started_at >= ? GROUP BY stage ORDER BY cost DESC
    """,
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def events_after(store, last_id, limit=200):
    return _rows(
        store.db.execute(
            """
        SELECT e.*, r.title FROM events e LEFT JOIN reviews r ON r.id = e.review_id
        WHERE e.id > ? ORDER BY e.id LIMIT ?""",
            (last_id, limit),
        )
    )


def live(store):
    run = _row(store.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone())
    if run is None:
        return {"run": None, "rows": []}
    items = _rows(
        store.db.execute(
            """
        SELECT r.id, r.pr, r.title, r.author, r.lane, r.verdict, r.error, r.started_at, r.finished_at,
               (SELECT message FROM events e WHERE e.review_id = r.id ORDER BY e.id DESC LIMIT 1) AS step,
               (SELECT COALESCE(SUM(cost_usd), 0) FROM stages s WHERE s.review_id = r.id) AS cost
        FROM reviews r WHERE r.run_id = ? ORDER BY r.id""",
            (run["id"],),
        )
    )
    checked = store.db.execute(
        "SELECT COUNT(*) FROM observations WHERE run_id = ?", (run["id"],)
    ).fetchone()[0]
    return {"run": run, "rows": items, "checked": checked}


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_diff(text, marks=()):
    """Split a unified diff into files and rows for rendering. marks are (path, line) pairs."""
    marked = set(marks)
    files, current = [], None
    old = new = 0
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            current = {"path": line.split(" b/", 1)[-1], "rows": [], "marked": False}
            files.append(current)
            continue
        if current is None:
            continue
        if (
            line.startswith(
                (
                    "index ",
                    "--- ",
                    "+++ ",
                    "new file",
                    "deleted file",
                    "similarity",
                    "rename ",
                    "old mode",
                    "new mode",
                )
            )
            and not current["rows"]
        ):
            if line.startswith("+++ ") and line != "+++ /dev/null":
                current["path"] = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        m = HUNK.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            current["rows"].append({"kind": "hunk", "text": line, "old": None, "new": None})
            continue
        if line.startswith("\\"):
            continue
        kind = {"+": "add", "-": "del"}.get(line[:1], "ctx")
        row = {
            "kind": kind,
            "text": line[1:],
            "old": None if kind == "add" else old,
            "new": None if kind == "del" else new,
        }
        row["mark"] = kind != "del" and (current["path"], new) in marked
        current["marked"] = current["marked"] or row["mark"]
        current["rows"].append(row)
        if kind != "add":
            old += 1
        if kind != "del":
            new += 1
    return files


def _hunks(rows):
    hunks, current = [], None
    for row in rows:
        if row["kind"] == "hunk":
            current = []
            hunks.append(current)
        elif current is not None:
            current.append(row)
    return hunks


def _window(path, hunk, idx, radius, cap=80):
    lo, hi = max(0, idx - radius), min(len(hunk), idx + radius + 1)
    start = max(0, min(idx - cap // 2, len(hunk) - cap))
    return {
        "path": path,
        "focus": hunk[idx],
        "rows": hunk[lo:hi],
        "hunk": hunk[start : start + cap],
        "trimmed": len(hunk) > cap,
        "more": len(hunk) > hi - lo,
    }


def excerpt(files, path, line, radius=2):
    for f in files:
        if f["path"] != path:
            continue
        for hunk in _hunks(f["rows"]):
            for i, row in enumerate(hunk):
                if row["new"] == line and row["kind"] != "del":
                    return _window(path, hunk, i, radius)
    return None


def lead_excerpt(files, radius=2):
    code = [f for f in files if not diffs.TESTISH.search(f["path"])] or files
    if not code:
        return None
    biggest = max(code, key=lambda f: sum(r["kind"] in ("add", "del") for r in f["rows"]))
    for hunk in _hunks(biggest["rows"]):
        for i, row in enumerate(hunk):
            if row["kind"] in ("add", "del"):
                return _window(biggest["path"], hunk, min(i + radius - 1, len(hunk) - 1), radius)
    return None


def factcheck_stats(store):
    flagged = store.db.execute(
        "SELECT COUNT(*) FROM drafts WHERE unsupported IS NOT NULL AND unsupported NOT IN ('', '[]', 'null', ?)",
        (json.dumps([DRAFT_FAILED]),),
    ).fetchone()[0]
    wrong = store.db.execute(
        "SELECT COUNT(*) FROM actions WHERE action = 'factcheck_false_alarm' AND ok = 1"
    ).fetchone()[0]
    return {"flagged": flagged, "wrong": wrong}


def weekly(store, days=7):
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    one = lambda sql, *p: store.db.execute(sql, p).fetchone()[0]  # noqa: E731
    verdicts = dict(
        store.db.execute(
            "SELECT verdict, COUNT(*) FROM reviews WHERE started_at >= ? AND verdict IS NOT NULL GROUP BY verdict",
            (since,),
        ).fetchall()
    )
    actions_by = dict(
        store.db.execute(
            "SELECT action, COUNT(*) FROM actions WHERE at >= ? AND ok = 1 GROUP BY action",
            (since,),
        ).fetchall()
    )
    grades = dict(
        store.db.execute(
            """
        SELECT g.grade, COUNT(*) FROM grades g JOIN reviews r ON r.id = g.review_id
        WHERE r.started_at >= ? GROUP BY g.grade""",
            (since,),
        ).fetchall()
    )
    graded = sum(grades.values())
    findings = [
        dict(r)
        for r in store.db.execute(
            """
        SELECT f.severity, f.label, COUNT(*) AS n, SUM(f.verified) AS kept
        FROM findings f JOIN reviews r ON r.id = f.review_id WHERE r.started_at >= ?
        GROUP BY f.severity, f.label ORDER BY n DESC""",
            (since,),
        ).fetchall()
    ]
    skip_reasons = [
        dict(r)
        for r in store.db.execute(
            """
        SELECT CASE WHEN instr(reason, ':') > 0 THEN substr(reason, 1, instr(reason, ':') - 1) ELSE reason END AS reason,
               COUNT(DISTINCT pr) AS prs
        FROM observations WHERE at >= ? AND outcome = 'skip' GROUP BY 1 ORDER BY prs DESC LIMIT 8""",
            (since,),
        ).fetchall()
    ]
    lanes = dict(
        store.db.execute(
            "SELECT COALESCE(lane, 'unknown'), COUNT(*) FROM reviews WHERE started_at >= ? GROUP BY 1",
            (since,),
        ).fetchall()
    )
    return {
        "since": since,
        "reviews": sum(verdicts.values()),
        "verdicts": verdicts,
        "actions": actions_by,
        "agreement": (grades.get("agree", 0) / graded) if graded else None,
        "graded": graded,
        "spend": one("SELECT COALESCE(SUM(cost_usd), 0) FROM reviews WHERE started_at >= ?", since)
        + sum(late_spend(store, since).values()),
        "avg_cost": one(
            "SELECT COALESCE(AVG(cost_usd), 0) FROM reviews WHERE started_at >= ? AND cost_usd > 0",
            since,
        ),
        "avg_minutes": one(
            """SELECT COALESCE(AVG((julianday(finished_at) - julianday(started_at)) * 1440), 0)
                              FROM reviews WHERE started_at >= ? AND finished_at IS NOT NULL AND source != 'legacy'""",
            since,
        ),
        "prs_seen": one("SELECT COUNT(DISTINCT pr) FROM observations WHERE at >= ?", since),
        "findings": findings,
        "skip_reasons": skip_reasons,
        "lanes": lanes,
    }


def proposals(store, limit=50):
    return _rows(
        store.db.execute(
            """
        SELECT p.id, p.review_id, p.status, p.created_at, p.decided_at, p.note, r.pr, r.title
        FROM proposals p LEFT JOIN reviews r ON r.id = p.review_id ORDER BY p.id DESC LIMIT ?""",
            (limit,),
        )
    )


def proposal(store, proposal_id):
    return _row(
        store.db.execute(
            """
        SELECT p.*, r.pr, r.title, r.verdict FROM proposals p LEFT JOIN reviews r ON r.id = p.review_id
        WHERE p.id = ?""",
            (proposal_id,),
        ).fetchone()
    )


def push_subs(store):
    return _rows(
        store.db.execute(
            "SELECT id, label, created_at, last_ok, failures FROM push_subs ORDER BY id"
        )
    )


def ungraded_count(store, waiting):
    """Decisions waiting for a grade, leaving out PRs still on To do, which the grade queue leaves out too."""
    return sum(1 for r in calibrate.queue(store) if r["pr"] not in waiting)


def grade_queue(store, ids):
    """For a grading queue's reviews: each one's PR, title, and verdict, and the grade any of them has now."""
    if not ids:
        return {}, {}
    marks = ",".join("?" * len(ids))
    rows = store.db.execute(
        f"SELECT id, pr, title, verdict FROM reviews WHERE id IN ({marks})", ids
    ).fetchall()
    grades = store.db.execute(
        f"SELECT review_id, grade, tag FROM grades WHERE review_id IN ({marks})", ids
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}, {g["review_id"]: dict(g) for g in grades}


def channel_counts(store, days=14):
    """How many PRs each Slack channel surfaced lately."""
    since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    found = {}
    for pr, value in store.db.execute(
        "SELECT DISTINCT pr, channels FROM observations WHERE at >= ? AND channels IS NOT NULL",
        (since,),
    ):
        with contextlib.suppress(TypeError, json.JSONDecodeError):
            for name in json.loads(value):
                found.setdefault(name, set()).add(pr)
    return {name: len(prs) for name, prs in found.items()}


def settings_log(store, limit=8):
    """Recent saves from the settings page, newest first."""
    out = []
    for row in _rows(
        store.db.execute(
            "SELECT at, payload FROM actions WHERE action = 'settings' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    ):
        payload = row["payload"] or {}
        before = payload.get("before", {})
        out.append(
            {
                "at": row["at"],
                "section": payload.get("section", ""),
                "changes": [
                    words.setting_change(key, before.get(key, words.UNKNOWN), value)
                    for key, value in payload.get("changes", {}).items()
                    if before.get(key, words.UNKNOWN) != value
                ],
            }
        )
    return out


def story_channels(store, number):
    channels = set()
    for (value,) in store.db.execute(
        "SELECT DISTINCT channels FROM observations WHERE pr = ? AND channels IS NOT NULL",
        (number,),
    ):
        with contextlib.suppress(TypeError, json.JSONDecodeError):
            channels.update(json.loads(value))
    return sorted(channels)


_live_pr_cache = {}


def live_pr(cfg, number):
    from .. import gh

    cached = _live_pr_cache.get(number)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    try:
        from .. import config

        rcfg, n = config.at(cfg, number)
        pr = gh.fetch_pr(rcfg, n)
    except Exception as exc:
        pr = {"error": str(exc)[:200]}
    else:
        latest = {}
        for review in pr["reviews"]:
            if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
                latest[review["author"]] = review
        checks = [c for c in pr["checks"] if c["required"]] or pr["checks"]
        pr["reviewers"] = sorted(latest.values(), key=lambda r: r["author"])
        pr["check_summary"] = {
            s: sum(1 for c in checks if c["state"] == s) for s in ("PASS", "FAIL", "PENDING")
        }
        pr["failing"] = [c["name"] for c in checks if c["state"] == "FAIL"]
        pr["open_threads"] = sum(1 for t in pr["threads"] if not t["resolved"])
    _live_pr_cache[number] = (time.monotonic(), pr)
    return pr
