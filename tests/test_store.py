import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plus1.store import Recorder, Store, import_legacy


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(
            state_dir=Path(self.tmp.name), checkout=self.tmp.name, repo="globex/web"
        )
        self.store = Store(self.cfg)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def pr(self):
        return {
            "number": 7,
            "head": "abc123",
            "base": "main",
            "title": "t",
            "author": "a",
            "url": "u",
            "labels": ["agent/x"],
            "files": ["a.rb"],
            "additions": 1,
            "deletions": 2,
        }

    def test_review_round_trip_encodes_json(self):
        run_id = self.store.start_run("manual", "shadow")
        review_id = self.store.start_review(run_id, self.pr(), "requested", None, "")
        self.store.update("reviews", review_id, raw_review={"verdict": "APPROVE"}, p2_notes=["n1"])
        row = self.store.query("SELECT * FROM reviews WHERE id = ?", review_id)[0]
        self.assertEqual(json.loads(row["labels"]), ["agent/x"])
        self.assertEqual(json.loads(row["raw_review"])["verdict"], "APPROVE")
        self.assertEqual(json.loads(row["p2_notes"]), ["n1"])
        self.assertIsNone(row["ticket"])

    def test_the_prior_review_is_the_last_one_that_ran_the_model(self):
        run_id = self.store.start_run("manual", "shadow")

        def review(sha, raw=None, error=None, found=()):
            rid = self.store.start_review(run_id, {**self.pr(), "head": sha}, "requested", None, "")
            self.store.update("reviews", rid, raw_review=raw, error=error)
            for idx, (title, verified) in enumerate(found):
                self.store.insert(
                    "findings",
                    review_id=rid,
                    idx=idx,
                    severity="P1",
                    title=title,
                    verified=verified,
                )
            return rid

        review("s1", raw={"verdict": "DRAFT"}, found=[("kill switch", 1), ("guess", 0)])
        review("s2", raw=None)
        review("s3", raw={"verdict": "DRAFT"}, error="boom", found=[("from a failed run", 1)])
        current = review("s4")
        last = self.store.prior_review(7, current)
        self.assertEqual(
            (last["sha"], [(f["title"], f["verified"]) for f in last["findings"]]),
            ("s1", [("kill switch", 1), ("guess", 0)]),
        )
        fixed = review("s5", raw={"verdict": "APPROVE"})
        self.assertEqual(
            (
                self.store.prior_review(7, fixed + 1)["sha"],
                self.store.prior_review(7, fixed + 1)["findings"],
            ),
            ("s5", []),
        )
        self.assertIsNone(self.store.prior_review(8, current))

    def test_carried_is_backfilled_from_notes_when_the_column_arrives(self):
        run_id = self.store.start_run("manual", "shadow")
        rid = self.store.start_review(run_id, self.pr(), "requested", None, "")
        self.store.update(
            "reviews",
            rid,
            p2_notes=[
                "fixed since the last review: Kill switch (now gated)",
                "dropped unverified: Guess (no)",
            ],
        )
        fixed = self.store.insert("findings", review_id=rid, idx=0, title="Kill switch", verified=0)
        guess = self.store.insert("findings", review_id=rid, idx=1, title="Guess", verified=0)
        self.store.db.execute("ALTER TABLE findings DROP COLUMN carried")
        Store(self.cfg).db.close()
        carried = dict(
            self.store.query("SELECT id, carried FROM findings WHERE review_id = ?", rid)
        )
        self.assertEqual(carried, {fixed: "1", guess: None})

    def test_an_older_database_gains_the_rewrite_columns_and_one_rule_per_comment(self):
        self.store.db.execute("DROP INDEX proposals_one_rewrite")
        for table, column in (
            ("proposals", "draft_id"),
            ("drafts", "notes"),
            ("drafts", "original_needs_edit"),
            ("drafts", "original_unsupported"),
        ):
            self.store.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        Store(self.cfg).db.close()
        draft = self.store.insert(
            "drafts", pr=1, sha="a", body="b", notes=["s"], status="queued", updated_at="t"
        )
        rule = {"draft_id": draft, "kind": "rewrite", "created_at": "t", "status": "pending"}
        self.store.insert("proposals", **rule)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.insert("proposals", **rule)
        self.store.insert("proposals", **{**rule, "status": "rejected"})
        self.assertEqual(self.store.rules_of([draft])[draft]["status"], "rejected")

    def test_adding_a_column_another_store_already_added_is_a_no_op(self):
        Store(self.cfg).db.close()
        self.store.add_column("reviews", "ticket")
        with self.assertRaises(sqlite3.OperationalError):
            self.store.add_column("no_such_table", "ticket")

    def test_review_keeps_its_ticket(self):
        run_id = self.store.start_run("manual", "shadow")
        review_id = self.store.start_review(
            run_id, {**self.pr(), "ticket": "ORD-784"}, "requested", None, ""
        )
        self.assertEqual(
            self.store.query("SELECT ticket FROM reviews WHERE id = ?", review_id)[0]["ticket"],
            "ORD-784",
        )

    def test_a_new_review_retires_drafts_left_from_earlier_ones(self):
        from plus1 import cli
        from plus1.review import Reviewed
        from plus1.state import State

        run_id = self.store.start_run("manual", "shadow")
        old = self.store.start_review(run_id, self.pr(), "requested", None, "")
        self.store.insert(
            "drafts",
            review_id=old,
            pr=7,
            sha="abc123",
            severity="P1",
            path="a.rb",
            line=1,
            inline=1,
            body="first review comment",
            original_body="x",
            needs_edit=0,
            status="queued",
            updated_at="t",
        )
        new = self.store.start_review(run_id, self.pr(), "manual", None, "")
        cfg = SimpleNamespace(state_dir=Path(self.tmp.name))
        res = Reviewed()
        res.reason = "thin coverage: unsure"
        tally = {"drafts": 0, "defers": 0, "approved": 0}
        cli.handle(
            cfg,
            State(cfg),
            self.store,
            Recorder(self.store, run_id, new, 7),
            new,
            self.pr(),
            res,
            tally,
        )
        self.assertEqual(
            self.store.query("SELECT status FROM drafts WHERE review_id = ?", old)[0][0],
            "superseded",
        )

    def test_a_check_never_approves_a_pr_handed_off_while_it_ran(self):
        from plus1 import actions, cli
        from plus1.review import Reviewed
        from plus1.state import State

        run_id = self.store.start_run("timer", "live")
        review_id = self.store.start_review(run_id, self.pr(), "requested", None, "")
        self.store.ignore(7, "t")
        cfg = self.live()
        res = Reviewed()
        res.verdict, res.reason = "APPROVE", "clean"
        approved, saved = [], actions.approve
        actions.approve = lambda *a, **k: approved.append(a)
        try:
            cli.handle(
                cfg,
                State(cfg),
                self.store,
                Recorder(self.store, run_id, review_id, 7),
                review_id,
                self.pr(),
                res,
                {"drafts": 0, "defers": 0, "approved": 0},
                auto=True,
            )
        finally:
            actions.approve = saved
        self.assertEqual(approved, [])

    def test_a_follow_up_never_approves_on_its_own_even_where_that_is_on(self):
        from plus1 import actions, cli
        from plus1.review import Reviewed
        from plus1.state import State

        run_id = self.store.start_run("timer", "live")
        review_id = self.store.start_review(run_id, self.pr(), "followup", None, "")
        cfg = self.live()
        res = Reviewed()
        res.verdict, res.reason = "APPROVE", "clean"
        res.followup = {
            "sha": "c0ffee",
            "items": [
                {
                    "thread_id": "T1",
                    "open": False,
                    "title": "t",
                    "why": "w",
                    "url": None,
                    "path": "a.rb",
                    "line": 1,
                }
            ],
        }
        approved, saved = [], actions.approve
        actions.approve = lambda *a, **k: approved.append(a)
        try:
            cli.handle(
                cfg,
                State(cfg),
                self.store,
                Recorder(self.store, run_id, review_id, 7),
                review_id,
                self.pr(),
                res,
                {"drafts": 0, "defers": 0, "approved": 0},
                auto=True,
            )
        finally:
            actions.approve = saved
        self.assertEqual(approved, [])
        stored = self.store.query("SELECT followup FROM reviews WHERE id = ?", review_id)[0][
            "followup"
        ]
        self.assertEqual(json.loads(stored)["items"][0]["thread_id"], "T1")

    def live(self, auto_approve=True, **limits):
        return SimpleNamespace(
            state_dir=Path(self.tmp.name),
            me="andymai",
            repo="globex/web",
            repos=[SimpleNamespace(name="globex/web", auto_approve=auto_approve, hold_yours=False)],
            limits=SimpleNamespace(
                **{
                    "max_approvals_per_tick": 5,
                    "max_approvals_per_day": 20,
                    "min_ready_minutes": 30,
                    "max_map_lines": 10000,
                    **limits,
                }
            ),
            filters=SimpleNamespace(hold_labels=["hold"], blocking_markers=["(blocking)"]),
            lanes=SimpleNamespace(never_approve_owners=[], never_approve_paths=[]),
        )

    def held_review(self, held="daily cap"):
        run_id = self.store.start_run("timer", "live")
        review_id = self.store.start_review(run_id, self.pr(), "requested", None, "")
        self.store.update(
            "reviews", review_id, verdict="APPROVE", reason="clean", held=held, finished_at="t"
        )
        return review_id

    def github_pr(self, **over):
        from datetime import datetime, timedelta

        return {
            "number": 7,
            "state": "OPEN",
            "draft": False,
            "author": "ann",
            "decision": "REVIEW_REQUIRED",
            "merge_queue": False,
            "labels": [],
            "mergeable": "MERGEABLE",
            "head": "abc123",
            "reviews": [],
            "threads": [],
            "conversation": [],
            "checks": [],
            "files": ["app/x.rb"],
            "additions": 1,
            "deletions": 1,
            "ready_at": datetime.now().astimezone() - timedelta(days=1),
            **over,
        }

    def retry(self, cfg, pr):
        from datetime import datetime

        from plus1 import actions, cli, gh
        from plus1.state import State

        approved = []
        saved = actions.approve, gh.fetch_pr
        actions.approve = lambda cfg, st, store, n, sha, reason, actor="cli": approved.append(
            (n, sha, actor)
        )
        gh.fetch_pr = lambda cfg, n: pr
        tally = {"approved": 0}
        try:
            cli.retry_held(cfg, State(cfg), self.store, tally, {}, datetime.now().astimezone())
        finally:
            actions.approve, gh.fetch_pr = saved
        return approved, tally

    def test_a_capped_approval_is_held_on_its_card_not_lost(self):
        from plus1 import actions, cli
        from plus1.review import Reviewed
        from plus1.state import State

        run_id = self.store.start_run("timer", "live")
        review_id = self.store.start_review(run_id, self.pr(), "requested", None, "")
        res = Reviewed()
        res.verdict, res.reason = "APPROVE", "clean"
        approved, saved = [], actions.approve
        actions.approve = lambda *a, **k: approved.append(a)
        try:
            cli.handle(
                self.live(max_approvals_per_day=0),
                State(self.live()),
                self.store,
                Recorder(self.store, run_id, review_id, 7),
                review_id,
                self.pr(),
                res,
                {"drafts": 0, "defers": 0, "approved": 0},
                auto=True,
            )
        finally:
            actions.approve = saved
        self.assertEqual(approved, [])
        self.assertEqual(
            self.store.query("SELECT held FROM reviews WHERE id = ?", review_id)[0][0], "daily cap"
        )

    def test_a_standing_request_for_changes_holds_an_approval(self):
        from plus1 import actions, cli
        from plus1.review import Reviewed
        from plus1.state import State

        run_id = self.store.start_run("timer", "live")
        review_id = self.store.start_review(run_id, self.pr(), "requested", None, "")
        res = Reviewed()
        res.verdict, res.reason = "APPROVE", "clean"
        approved, saved = [], actions.approve
        actions.approve = lambda *a, **k: approved.append(a)
        try:
            cli.handle(
                self.live(),
                State(self.live()),
                self.store,
                Recorder(self.store, run_id, review_id, 7),
                review_id,
                {**self.pr(), "decision": "CHANGES_REQUESTED"},
                res,
                {"drafts": 0, "defers": 0, "approved": 0},
                auto=True,
            )
        finally:
            actions.approve = saved
        self.assertEqual(approved, [])
        self.assertEqual(
            self.store.query("SELECT held FROM reviews WHERE id = ?", review_id)[0][0],
            "changes requested on GitHub",
        )
        self.assertEqual(
            self.retry(self.live(), self.github_pr(decision="CHANGES_REQUESTED"))[0], []
        )

    def approve_via_handle(self, states, cfg=None, auto=True):
        from plus1 import actions, cli, gh
        from plus1.review import Reviewed
        from plus1.state import State

        run_id = self.store.start_run("timer", "live")
        review_id = self.store.start_review(run_id, self.pr(), "requested", None, "")
        res = Reviewed()
        res.verdict, res.reason = "APPROVE", "clean"
        approved, saved = [], (actions.approve, gh.pr_states)
        actions.approve = lambda *a, **k: approved.append(a)
        gh.pr_states = states
        try:
            cli.handle(
                cfg or self.live(),
                State(self.live()),
                self.store,
                Recorder(self.store, run_id, review_id, 7),
                review_id,
                {**self.pr(), "decision": "REVIEW_REQUIRED"},
                res,
                {"drafts": 0, "defers": 0, "approved": 0},
                auto=auto,
            )
        finally:
            actions.approve, gh.pr_states = saved
        return approved, self.store.query("SELECT held FROM reviews WHERE id = ?", review_id)[0][0]

    def test_an_approval_looks_at_github_once_more_before_it_goes_out(self):
        def state(decision):
            return lambda cfg, numbers: {
                7: {"state": "OPEN", "decision": decision, "merge_queue": False}
            }

        self.assertEqual(
            self.approve_via_handle(state("APPROVED")), ([], "already approved or merged")
        )
        self.assertEqual(
            self.approve_via_handle(state("CHANGES_REQUESTED")), ([], "changes requested on GitHub")
        )
        approved, held = self.approve_via_handle(state("REVIEW_REQUIRED"))
        self.assertEqual((len(approved), held), (1, None))

        def down(cfg, numbers):
            raise RuntimeError("gh is down")

        self.assertEqual(self.approve_via_handle(down), ([], "couldn't recheck the PR on GitHub"))

    def test_only_a_scheduled_check_approves_on_its_own_and_only_where_that_is_on(self):
        def clear(cfg, numbers):
            return {7: {"state": "OPEN", "decision": "REVIEW_REQUIRED", "merge_queue": False}}

        self.assertEqual(self.approve_via_handle(clear, auto=False), ([], None))
        self.assertEqual(
            self.approve_via_handle(clear, cfg=self.live(auto_approve=False)), ([], None)
        )

    def test_a_held_approval_goes_out_later_only_through_todays_gates(self):
        review_id = self.held_review()
        approved, tally = self.retry(self.live(), self.github_pr())
        self.assertEqual((approved, tally["approved"]), ([(7, "abc123", "timer")], 1))
        self.assertIsNone(
            self.store.query("SELECT held FROM reviews WHERE id = ?", review_id)[0][0]
        )

    def test_a_held_approval_waits_when_the_pr_changed_since(self):
        review_id = self.held_review()
        blocked = self.github_pr(
            reviews=[
                {
                    "author": "kayla",
                    "state": "CHANGES_REQUESTED",
                    "oid": "abc123",
                    "body": "no",
                    "bot": False,
                }
            ]
        )
        for pr, why in (
            (blocked, "changes requested by kayla"),
            (self.github_pr(head="def456"), "new commits"),
            (self.github_pr(labels=["hold"]), "hold label: hold"),
        ):
            approved, _ = self.retry(self.live(), pr)
            self.assertEqual(approved, [])
            self.assertEqual(
                self.store.query("SELECT held FROM reviews WHERE id = ?", review_id)[0][0], why
            )

    def test_a_held_approval_the_owner_settled_is_not_retried(self):
        review_id = self.held_review()
        self.store.insert(
            "handled", pr=7, sha="abc123", review_id=review_id, how="would comment", at="t"
        )
        self.assertEqual(self.retry(self.live(), self.github_pr())[0], [])

    def test_held_approvals_wait_where_approving_on_its_own_is_off_and_at_the_cap(self):
        self.held_review()
        self.assertEqual(self.retry(self.live(auto_approve=False), self.github_pr())[0], [])
        self.assertEqual(self.retry(self.live(max_approvals_per_day=0), self.github_pr())[0], [])

    def test_the_daily_cap_counts_only_automatic_approvals(self):
        from plus1.state import State

        st = State(self.live())
        st.audit("approve_posted", pr=1, actor="timer")
        st.audit("approve_posted", pr=2, actor="ui")
        st.audit("approve_posted", pr=3)
        self.assertEqual(st.approvals_today(), 2)

    def test_recorder_stage_captures_usage(self):
        run_id = self.store.start_run("manual", "shadow")
        review_id = self.store.start_review(run_id, self.pr(), "requested", None, "")
        rec = Recorder(self.store, run_id, review_id, 7)
        result = SimpleNamespace(
            cost=0.12,
            turns=3,
            duration_s=4.5,
            model="claude-opus-5-5",
            usage={"input": 10, "output": 20, "cache_read": 30, "cache_write": 40},
        )
        rec.stage("deep", 0, "claude-opus-5-5", "high", "2026-10-01T12:00:00-07:00", result=result)
        rec.stage("verify", 1, "opus", "medium", "2026-10-01T12:00:00-07:00", error="boom")
        rec.event("deep", "deep review")
        rows = self.store.query("SELECT * FROM stages ORDER BY id")
        self.assertEqual(
            (rows[0]["cost_usd"], rows[0]["output_tokens"], rows[0]["ok"]), (0.12, 20, 1)
        )
        self.assertEqual((rows[1]["ok"], rows[1]["error"]), (0, "boom"))
        self.assertEqual(
            self.store.query("SELECT message FROM events")[0]["message"], "deep review"
        )

    def test_draft_status_only_moves_queued(self):
        self.store.insert("drafts", pr=7, sha="abc", status="queued", updated_at="x")
        self.store.set_draft_status(7, "abc", "released")
        self.store.set_draft_status(7, "abc", "dismissed")
        self.assertEqual(self.store.query("SELECT status FROM drafts")[0]["status"], "released")

    def test_import_legacy_once(self):
        audit = self.cfg.state_dir / "audit.jsonl"
        audit.write_text(
            json.dumps(
                {
                    "at": "2026-10-01T11:13:31-07:00",
                    "mode": "shadow",
                    "action": "decision",
                    "pr": 9,
                    "sha": "def",
                    "verdict": "DRAFT",
                    "reason": "r",
                    "lane": "substantive",
                    "cost_usd": 0.57,
                }
            )
            + "\n"
        )
        (self.cfg.state_dir / "reports").mkdir()
        (self.cfg.state_dir / "reports" / "2026-10-01.md").write_text(
            "- 11:13 #9 title (a) -> DRAFT: r\n- 11:13     note: first\n- 11:14 #10 other -> APPROVE: x\n- 11:14     note: not mine\n"
        )
        (self.cfg.state_dir / "queue").mkdir()
        (self.cfg.state_dir / "queue" / "9.json").write_text(
            json.dumps(
                {
                    "sha": "def",
                    "title": "title",
                    "author": "a",
                    "url": "u",
                    "comments": [
                        {
                            "severity": "P1",
                            "path": "a.rb",
                            "line": 3,
                            "inline": True,
                            "body": "b",
                            "needs_edit": False,
                            "unsupported": [],
                        }
                    ],
                }
            )
        )
        self.assertEqual(import_legacy(self.cfg, self.store), 1)
        self.assertEqual(import_legacy(self.cfg, self.store), 0)
        review = self.store.query("SELECT * FROM reviews")[0]
        self.assertEqual(json.loads(review["p2_notes"]), ["first"])
        self.assertEqual(review["title"], "title")
        self.assertEqual(self.store.query("SELECT status FROM drafts")[0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
