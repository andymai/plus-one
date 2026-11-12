import json
import re
import sqlite3
import subprocess
from datetime import datetime

from . import prs

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  trigger TEXT NOT NULL,
  mode TEXT NOT NULL,
  candidates INTEGER,
  reviewed INTEGER,
  skipped INTEGER,
  drafts INTEGER,
  defers INTEGER,
  approved INTEGER,
  errors INTEGER,
  cost_usd REAL,
  slack_ok INTEGER,
  note TEXT
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  at TEXT NOT NULL,
  pr INTEGER NOT NULL,
  sha TEXT,
  title TEXT,
  author TEXT,
  source TEXT,
  outcome TEXT NOT NULL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS observations_pr ON observations(pr);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id),
  pr INTEGER NOT NULL,
  sha TEXT NOT NULL,
  base TEXT,
  title TEXT,
  author TEXT,
  url TEXT,
  labels TEXT,
  files TEXT,
  additions INTEGER,
  deletions INTEGER,
  source TEXT,
  lane TEXT,
  lane_reason TEXT,
  verdict TEXT,
  reason TEXT,
  coverage TEXT,
  summary TEXT,
  raw_review TEXT,
  p2_notes TEXT,
  diff TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  cost_usd REAL,
  error TEXT
);
CREATE INDEX IF NOT EXISTS reviews_pr ON reviews(pr);
CREATE TABLE IF NOT EXISTS stages (
  id INTEGER PRIMARY KEY,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  stage TEXT NOT NULL,
  idx INTEGER NOT NULL DEFAULT 0,
  model TEXT,
  effort TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_s REAL,
  cost_usd REAL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  turns INTEGER,
  ok INTEGER NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  idx INTEGER NOT NULL,
  severity TEXT,
  label TEXT,
  title TEXT,
  path TEXT,
  line INTEGER,
  evidence TEXT,
  impact TEXT,
  suggestion TEXT,
  confidence TEXT,
  verified INTEGER,
  verify_reason TEXT,
  final_path TEXT,
  final_line INTEGER
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY,
  review_id INTEGER REFERENCES reviews(id),
  finding_id INTEGER REFERENCES findings(id),
  pr INTEGER NOT NULL,
  sha TEXT NOT NULL,
  severity TEXT,
  path TEXT,
  line INTEGER,
  inline INTEGER,
  body TEXT,
  original_body TEXT,
  needs_edit INTEGER,
  unsupported TEXT,
  original_needs_edit INTEGER,
  original_unsupported TEXT,
  notes TEXT,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_pr ON drafts(pr);
CREATE TABLE IF NOT EXISTS rewrites (
  id INTEGER PRIMARY KEY,
  draft_id INTEGER NOT NULL REFERENCES drafts(id),
  parent_id INTEGER REFERENCES rewrites(id),
  suggestion TEXT NOT NULL,
  notes TEXT NOT NULL,
  base TEXT NOT NULL,
  body TEXT,
  needs_edit INTEGER,
  unsupported TEXT,
  status TEXT NOT NULL,
  error TEXT,
  cost_usd REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rewrites_draft ON rewrites(draft_id);
CREATE UNIQUE INDEX IF NOT EXISTS rewrites_one_writer ON rewrites(draft_id) WHERE status = 'writing';
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  pr INTEGER,
  sha TEXT,
  action TEXT NOT NULL,
  event TEXT,
  actor TEXT,
  payload TEXT,
  github_review_id INTEGER,
  ok INTEGER NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS grades (
  review_id INTEGER PRIMARY KEY REFERENCES reviews(id),
  source TEXT NOT NULL,
  grade TEXT NOT NULL,
  tag TEXT,
  note TEXT,
  andy_action TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY,
  review_id INTEGER REFERENCES reviews(id),
  draft_id INTEGER REFERENCES drafts(id),
  comments TEXT,
  kind TEXT,
  cost_usd REAL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  note TEXT,
  rationale TEXT,
  edits TEXT,
  before TEXT,
  after TEXT,
  error TEXT,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS push_subs (
  id INTEGER PRIMARY KEY,
  endpoint TEXT NOT NULL UNIQUE,
  keys TEXT NOT NULL,
  label TEXT,
  created_at TEXT NOT NULL,
  last_ok TEXT,
  failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_actions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  pr INTEGER NOT NULL,
  review_id INTEGER,
  sha TEXT,
  payload TEXT,
  created_at TEXT NOT NULL,
  execute_at REAL NOT NULL,
  status TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS handled (
  id INTEGER PRIMARY KEY,
  pr INTEGER NOT NULL,
  sha TEXT,
  review_id INTEGER,
  how TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ignored (
  pr INTEGER PRIMARY KEY,
  title TEXT,
  review_id INTEGER,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS yours (
  pr INTEGER NOT NULL,
  sha TEXT NOT NULL,
  data TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (pr, sha)
);
CREATE TABLE IF NOT EXISTS pr_status (
  pr INTEGER PRIMARY KEY,
  state TEXT NOT NULL,
  decision TEXT,
  merge_queue INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  run_id INTEGER,
  review_id INTEGER,
  pr INTEGER,
  stage TEXT,
  message TEXT
);
"""


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _encode(value):
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(sorted(value) if isinstance(value, set) else value, default=str)
    if isinstance(value, bool):
        return int(value)
    return value


def decode_json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


class Store:
    def __init__(self, cfg):
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cfg.state_dir / "prr.db", timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        names = (
            [r.name for r in cfg.repos]
            if hasattr(cfg, "repos")
            else [cfg.repo]
            if hasattr(cfg, "repo")
            else []
        )
        prs.register(self.db, names)
        for table, column in (
            ("observations", "channels"),
            ("reviews", "ticket"),
            ("reviews", "requested_by"),
            ("reviews", "sensitive"),
            ("findings", "raised_by"),
            ("findings", "carried"),
            ("reviews", "followup"),
            ("findings", "origin"),
            ("reviews", "held"),
            ("reviews", "yours"),
            ("proposals", "kind"),
            ("proposals", "cost_usd"),
            ("drafts", "original_needs_edit"),
            ("drafts", "original_unsupported"),
            ("drafts", "notes"),
            ("proposals", "draft_id"),
            ("proposals", "comments"),
        ):
            columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.add_column(table, column)
                if (table, column) == ("findings", "carried"):
                    self._backfill_carried()
        # A database written before this index existed can hold two writing rows for one finding, and then the
        # CREATE fails and no Store opens. Keep the newest attempt.
        self.db.execute("""UPDATE drafts SET status = 'failed' WHERE status = 'writing' AND id NOT IN
                           (SELECT MAX(id) FROM drafts WHERE status = 'writing' GROUP BY finding_id)""")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS drafts_one_writer ON drafts(finding_id) WHERE status = 'writing'"
        )
        # Two syncs at once (the timer's and the Insights button) can't both draft a change for one overruled review.
        # Any extra a database already holds moves out of the index's way, still counted toward the day's limit.
        self.db.execute("""UPDATE proposals SET kind = 'github-extra' WHERE kind = 'github' AND id NOT IN
                           (SELECT MIN(id) FROM proposals WHERE kind = 'github' GROUP BY review_id)""")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS proposals_one_github ON proposals(review_id) WHERE kind = 'github'"
        )
        # A double click on Make this a rule can't draft two rules from one comment.
        self.db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS proposals_one_rewrite ON proposals(draft_id)
               WHERE kind = 'rewrite' AND status IN ('pending', 'ready')"""
        )

    def _backfill_carried(self):
        # Reviews saved before the column record a carried finding the verifier let go only in their notes.
        for review in self.db.execute(
            "SELECT id, p2_notes FROM reviews WHERE p2_notes LIKE '%fixed since the last review: %'"
        ).fetchall():
            notes = decode_json(review["p2_notes"], [])
            for f in self.db.execute(
                "SELECT id, title FROM findings WHERE review_id = ? AND verified = 0",
                (review["id"],),
            ).fetchall():
                if any(n.startswith(f"fixed since the last review: {f['title']} (") for n in notes):
                    self.db.execute("UPDATE findings SET carried = '1' WHERE id = ?", (f["id"],))

    def add_column(self, table, column):
        # The web app opens a Store in each of its threads at startup, so another one can add the column first.
        try:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise

    def __del__(self):
        # A finalizer can run after module globals are cleared at exit, so it can't lean on contextlib.
        try:  # noqa: SIM105
            self.db.close()
        except Exception:  # noqa: S110
            pass

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def insert(self, table, **cols):
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self.db.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks})", [_encode(v) for v in cols.values()]
        )
        return cur.lastrowid

    def update(self, table, row_id, **cols):
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.db.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?",
            [_encode(v) for v in cols.values()] + [row_id],
        )

    def query(self, sql, *params):
        return self.db.execute(sql, params).fetchall()

    def start_run(self, trigger, mode):
        return self.insert("runs", started_at=now(), trigger=trigger, mode=mode)

    def finish_run(self, run_id, tally, candidates, slack_ok):
        self.update(
            "runs",
            run_id,
            finished_at=now(),
            candidates=candidates,
            slack_ok=slack_ok,
            reviewed=tally["reviewed"],
            skipped=tally["skipped"],
            drafts=tally["drafts"],
            defers=tally["defers"],
            approved=tally["approved"],
            errors=tally["errors"],
            cost_usd=round(tally["cost"], 4),
        )

    def observe(self, run_id, number, outcome, reason, pr=None, source=None, channels=None):
        pr = pr or {}
        self.insert(
            "observations",
            run_id=run_id,
            at=now(),
            pr=number,
            sha=pr.get("head"),
            title=pr.get("title"),
            author=pr.get("author"),
            source=source,
            outcome=outcome,
            reason=reason,
            channels=sorted(channels) if channels else None,
        )

    def spend_today(self):
        """Every model call today: review stages, plus rubric work, which has no review to hang stages on."""
        day = datetime.now().astimezone().date().isoformat() + "%"
        return self.db.execute(
            """SELECT (SELECT COALESCE(SUM(cost_usd), 0) FROM stages WHERE started_at LIKE ?)
                                         + (SELECT COALESCE(SUM(cost_usd), 0) FROM proposals WHERE created_at LIKE ?)""",
            (day, day),
        ).fetchone()[0]

    def yours(self, number, sha):
        """What git showed about the owner and this commit (plus1.yours.measure), or None before it's been read."""
        row = self.db.execute(
            "SELECT data FROM yours WHERE pr = ? AND sha = ?", (number, sha)
        ).fetchone()
        return decode_json(row[0], None) if row else None

    def save_yours(self, number, sha, data):
        self.db.execute(
            "INSERT OR REPLACE INTO yours (pr, sha, data, at) VALUES (?, ?, ?, ?)",
            (number, sha, json.dumps(data), now()),
        )

    def waits(self, number, sha):
        """How many checks passed this commit over for lack of review budget."""
        return self.db.execute(
            "SELECT COUNT(*) FROM observations WHERE pr = ? AND sha = ? AND outcome = 'waiting'",
            (number, sha),
        ).fetchone()[0]

    def start_review(self, run_id, pr, source, lane, lane_reason):
        return self.insert(
            "reviews",
            run_id=run_id,
            pr=pr.get("id", pr["number"]),
            sha=pr["head"],
            base=pr["base"],
            title=pr["title"],
            author=pr["author"],
            url=pr["url"],
            labels=pr["labels"],
            files=pr["files"],
            additions=pr["additions"],
            deletions=pr["deletions"],
            source=source,
            lane=lane,
            lane_reason=lane_reason,
            ticket=pr.get("ticket"),
            requested_by=pr.get("requested_by"),
            sensitive=pr.get("sensitive"),
            yours=pr.get("yours"),
            started_at=now(),
        )

    def event(self, run_id, review_id, number, stage, message):
        self.insert(
            "events",
            at=now(),
            run_id=run_id,
            review_id=review_id,
            pr=number,
            stage=stage,
            message=message,
        )

    def action(
        self,
        action,
        actor,
        pr=None,
        sha=None,
        event=None,
        payload=None,
        github_review_id=None,
        ok=True,
        error=None,
    ):
        return self.insert(
            "actions",
            at=now(),
            pr=pr,
            sha=sha,
            action=action,
            event=event,
            actor=actor,
            payload=payload,
            github_review_id=github_review_id,
            ok=ok,
            error=error,
        )

    def ignore(self, number, title=None, review_id=None):
        self.db.execute(
            "INSERT OR REPLACE INTO ignored (pr, title, review_id, at) VALUES (?, ?, ?, ?)",
            (number, title, review_id, now()),
        )

    def unignore(self, number):
        self.db.execute("DELETE FROM ignored WHERE pr = ?", (number,))

    def latest_sha(self, number):
        row = self.db.execute(
            "SELECT sha FROM reviews WHERE pr = ? ORDER BY id DESC LIMIT 1", (number,)
        ).fetchone()
        return row[0] if row else None

    def is_ignored(self, number):
        return (
            self.db.execute("SELECT 1 FROM ignored WHERE pr = ?", (number,)).fetchone() is not None
        )

    def ignored(self):
        return {r["pr"]: dict(r) for r in self.db.execute("SELECT * FROM ignored ORDER BY at DESC")}

    def prior_review(self, number, before_id):
        # raw_review is set only when the model ran (or its result carried over), so filter defers and failed runs
        # can't break the chain.
        row = self.db.execute(
            """SELECT * FROM reviews WHERE pr = ? AND id < ? AND raw_review IS NOT NULL
                                 AND error IS NULL ORDER BY id DESC LIMIT 1""",
            (number, before_id),
        ).fetchone()
        if not row:
            return None
        last = dict(row)
        last["waived"] = self.waived(number)
        last["findings"] = [
            dict(f)
            for f in self.db.execute(
                """
            SELECT f.*, (SELECT d.status FROM drafts d WHERE d.finding_id = f.id ORDER BY d.id DESC LIMIT 1) AS draft_status
            FROM findings f WHERE f.review_id = ? ORDER BY f.idx""",
                (row["id"],),
            )
        ]
        return last

    def waived(self, number):
        """Findings the owner chose not to post on this PR, from any review of it."""
        rows = self.db.execute(
            """
            SELECT f.title, COALESCE(f.final_path, f.path) AS path, COALESCE(f.final_line, f.line) AS line
            FROM drafts d JOIN findings f ON f.id = d.finding_id
            WHERE d.pr = ? AND d.status = 'dismissed' ORDER BY d.id""",
            (number,),
        ).fetchall()
        return list({r["title"]: dict(r) for r in rows}.values())

    def pr_status(self, number):
        row = self.db.execute("SELECT * FROM pr_status WHERE pr = ?", (number,)).fetchone()
        return dict(row) if row else None

    def set_pr_status(self, number, state, decision, merge_queue):
        self.db.execute(
            "INSERT OR REPLACE INTO pr_status (pr, state, decision, merge_queue, checked_at) VALUES (?, ?, ?, ?, ?)",
            (number, state, decision, int(bool(merge_queue)), now()),
        )

    def retire_drafts(self, number, keep_review_id):
        self.db.execute(
            "UPDATE drafts SET status = 'superseded', updated_at = ? WHERE pr = ? AND status = 'queued' AND review_id != ?",
            (now(), number, keep_review_id),
        )

    def finish_write_up(self, draft_id, number, review_id, **cols):
        # One statement, so a review that starts mid-write can't slip between the check and the queueing, and a
        # writer whose attempt was already given up (no longer 'writing') changes nothing.
        sets = ", ".join(f"{k} = ?" for k in cols)
        cur = self.db.execute(
            f"""UPDATE drafts SET {sets}, status = CASE WHEN EXISTS
                  (SELECT 1 FROM reviews WHERE pr = ? AND id > ?) THEN 'superseded' ELSE 'queued' END
                WHERE id = ? AND status = 'writing'""",
            [_encode(v) for v in cols.values()] + [number, review_id, draft_id],
        )
        return cur.rowcount == 1

    def retire_writers(self, number):
        self.db.execute(
            "UPDATE drafts SET status = 'superseded', updated_at = ? WHERE pr = ? AND status = 'writing'",
            (now(), number),
        )
        self.db.execute(
            """UPDATE rewrites SET status = 'superseded', updated_at = ? WHERE status IN ('writing', 'ready')
                 AND draft_id IN (SELECT id FROM drafts WHERE pr = ?)""",
            (now(), number),
        )

    def rewrites_of(self, draft_ids):
        marks = ", ".join("?" for _ in draft_ids)
        return self.query(
            f"SELECT * FROM rewrites WHERE draft_id IN ({marks}) ORDER BY updated_at, id",
            *draft_ids,
        )

    def rules_of(self, draft_ids):
        marks = ", ".join("?" for _ in draft_ids)
        return {
            r["draft_id"]: dict(r)
            for r in self.query(
                f"""SELECT id, CAST(draft_id AS INTEGER) AS draft_id, status, error FROM proposals
                    WHERE kind = 'rewrite' AND draft_id IN ({marks}) ORDER BY id""",
                *draft_ids,
            )
        }

    def finish_rewrite(self, rewrite_id, **cols):
        # One statement, so a comment posted or retired mid-rewrite can't take a proposal, and a rewrite already
        # given up (no longer 'writing') changes nothing.
        sets = ", ".join(f"{k} = ?" for k in cols)
        cur = self.db.execute(
            f"""UPDATE rewrites SET {sets}, status = CASE WHEN EXISTS
                  (SELECT 1 FROM drafts WHERE id = rewrites.draft_id AND status = 'queued')
                  THEN 'ready' ELSE 'superseded' END
                WHERE id = ? AND status = 'writing'""",
            [_encode(v) for v in cols.values()] + [rewrite_id],
        )
        row = self.db.execute(
            "SELECT draft_id, status FROM rewrites WHERE id = ?", (rewrite_id,)
        ).fetchone()
        if cur.rowcount != 1 or row["status"] != "ready":
            return False
        # The proposal this one revised gives way to it.
        self.db.execute(
            """UPDATE rewrites SET status = 'revised', updated_at = ?
               WHERE draft_id = ? AND status = 'ready' AND id != ?""",
            (now(), row["draft_id"], rewrite_id),
        )
        return True

    def use_rewrite(self, rewrite_id, draft_id):
        # One transaction, so a release running meanwhile posts the old text or the new, and never the new text
        # under the old one's flags.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                """SELECT r.* FROM rewrites r JOIN drafts d ON d.id = r.draft_id
                   WHERE r.id = ? AND r.draft_id = ? AND r.status = 'ready' AND d.status = 'queued'""",
                (rewrite_id, draft_id),
            ).fetchone()
            if row is not None:
                # Drafts stored before original_unsupported keep theirs from the first rewrite they take.
                self.db.execute(
                    """UPDATE drafts SET original_unsupported = COALESCE(original_unsupported, unsupported),
                         body = ?, needs_edit = ?, unsupported = ?, notes = ?, updated_at = ? WHERE id = ?""",
                    (
                        row["body"],
                        row["needs_edit"],
                        row["unsupported"],
                        row["notes"],
                        now(),
                        draft_id,
                    ),
                )
                self.db.execute(
                    "UPDATE rewrites SET status = 'accepted', updated_at = ? WHERE id = ?",
                    (now(), rewrite_id),
                )
                self.db.execute(
                    """UPDATE rewrites SET status = 'superseded', updated_at = ?
                       WHERE draft_id = ? AND status IN ('writing', 'ready')""",
                    (now(), draft_id),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return row is not None

    def discard_rewrite(self, rewrite_id, draft_id):
        cur = self.db.execute(
            """UPDATE rewrites SET status = 'discarded', updated_at = ?
               WHERE id = ? AND draft_id = ? AND status = 'ready'""",
            (now(), rewrite_id, draft_id),
        )
        if cur.rowcount != 1:
            return False
        # Keep mine drops the whole chain, including a revision of it still being written, or one that finished
        # between these two statements.
        self.drop_rewrites(draft_id)
        return True

    def drop_rewrites(self, draft_id):
        self.db.execute(
            """UPDATE rewrites SET status = 'superseded', updated_at = ?
               WHERE draft_id = ? AND status IN ('writing', 'ready')""",
            (now(), draft_id),
        )

    def restore_original(self, draft_id):
        # The first draft comes back with its own fact-check flags. Drafts stored before original_needs_edit count
        # as flagged when their first fact-check listed anything.
        cur = self.db.execute(
            """UPDATE drafts SET body = original_body,
                 needs_edit = COALESCE(original_needs_edit, CASE WHEN
                   COALESCE(original_unsupported, unsupported, '[]') IN ('[]', '') THEN 0 ELSE 1 END),
                 unsupported = COALESCE(original_unsupported, unsupported), notes = NULL, updated_at = ?
               WHERE id = ? AND status = 'queued' AND original_body IS NOT NULL""",
            (now(), draft_id),
        )
        if cur.rowcount != 1:
            return False
        # A rewrite waiting or running revised the text being abandoned.
        self.drop_rewrites(draft_id)
        return True

    def expire_unposted(self, merged_only=False):
        # A comment can't change a PR that merged or closed, so it isn't anything left to do. A merge is final, but a
        # cached CLOSED can be stale (the PR reopened), so callers pass merged_only until they've polled GitHub.
        states = ("MERGED",) if merged_only else ("MERGED", "CLOSED")
        self.db.execute(
            f"""UPDATE drafts SET status = 'expired', updated_at = ? WHERE status = 'queued'
                           AND pr IN (SELECT pr FROM pr_status WHERE state IN ({",".join("?" * len(states))}))""",
            (now(), *states),
        )

    def set_draft_status(self, number, sha, status):
        self.db.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE pr = ? AND sha = ? AND status = 'queued'",
            (status, now(), number, sha),
        )


class Recorder:
    def __init__(self, store, run_id, review_id, number):
        self.store, self.run_id, self.review_id, self.number = store, run_id, review_id, number

    def event(self, stage, message):
        self.store.event(self.run_id, self.review_id, self.number, stage, message)

    def stage(
        self, stage, idx, model, effort, started_at, result=None, error=None, duration_s=None
    ):
        usage = getattr(result, "usage", None) or {}
        self.store.insert(
            "stages",
            review_id=self.review_id,
            stage=stage,
            idx=idx,
            model=model,
            effort=effort,
            started_at=started_at,
            finished_at=now(),
            duration_s=getattr(result, "duration_s", None) or duration_s,
            cost_usd=getattr(result, "cost", None),
            input_tokens=usage.get("input"),
            output_tokens=usage.get("output"),
            cache_read_tokens=usage.get("cache_read"),
            cache_write_tokens=usage.get("cache_write"),
            turns=getattr(result, "turns", None),
            ok=error is None,
            error=error,
        )


NOTE = re.compile(r"^- \d\d:\d\d\s+note: (.*)$")
DECISION = re.compile(r"^- \d\d:\d\d #(\d+) ")


def _legacy_notes(cfg, number):
    notes = []
    for report in sorted((cfg.state_dir / "reports").glob("*.md")):
        current = None
        for line in report.read_text().splitlines():
            m = DECISION.match(line)
            if m:
                current = int(m.group(1))
                continue
            n = NOTE.match(line)
            if n and current == number:
                notes.append(n.group(1))
    return notes


def _legacy_diff(cfg, sha):
    proc = subprocess.run(
        ["git", "diff", "--no-color", "-U5", f"origin/main...{sha}"],
        cwd=cfg.checkout,
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def import_legacy(cfg, store):
    if store.query("SELECT 1 FROM reviews LIMIT 1"):
        return 0
    audit = cfg.state_dir / "audit.jsonl"
    if not audit.exists():
        return 0
    imported = 0
    for line in audit.read_text().splitlines():
        rec = json.loads(line)
        if rec["action"] != "decision":
            continue
        review_id = store.insert(
            "reviews",
            pr=rec["pr"],
            sha=rec["sha"],
            source="legacy",
            lane=rec.get("lane"),
            verdict=rec["verdict"],
            reason=rec["reason"],
            cost_usd=rec.get("cost_usd"),
            p2_notes=_legacy_notes(cfg, rec["pr"]),
            diff=_legacy_diff(cfg, rec["sha"]),
            started_at=rec["at"],
            finished_at=rec["at"],
        )
        queued = cfg.state_dir / "queue" / f"{rec['pr']}.json"
        if queued.exists():
            draft = json.loads(queued.read_text())
            store.update(
                "reviews", review_id, title=draft["title"], author=draft["author"], url=draft["url"]
            )
            for c in draft["comments"]:
                store.insert(
                    "drafts",
                    review_id=review_id,
                    pr=rec["pr"],
                    sha=draft["sha"],
                    severity=c["severity"],
                    path=c["path"],
                    line=c["line"],
                    inline=c["inline"],
                    body=c["body"],
                    original_body=c["body"],
                    needs_edit=c["needs_edit"],
                    unsupported=c["unsupported"],
                    original_needs_edit=c["needs_edit"],
                    original_unsupported=c["unsupported"],
                    status="queued",
                    updated_at=now(),
                )
        imported += 1
    return imported
