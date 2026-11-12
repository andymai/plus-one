import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plus1 import calibrate, gh, propose
from plus1.store import Store, now


class CalibrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(
            state_dir=Path(self.tmp.name),
            repo="globex/web",
            repos=[SimpleNamespace(name="globex/web")],
            me="andymai",
        )
        self.store = Store(self.cfg)
        self._run = gh.run

    def tearDown(self):
        gh.run = self._run
        self.store.db.close()
        self.tmp.cleanup()

    def review(self, pr, verdict, sha="s" * 40, started="2026-10-01T10:00:00-07:00"):
        rid = self.store.insert(
            "reviews",
            pr=pr,
            sha=sha,
            verdict=verdict,
            source="requested",
            title=f"PR {pr}",
            started_at=started,
            finished_at=started,
        )
        return rid

    def grade_of(self, rid):
        rows = self.store.query("SELECT * FROM grades WHERE review_id = ?", rid)
        return dict(rows[0]) if rows else None

    def test_infer_from_actions(self):
        released = self.review(1, "DRAFT")
        dismissed = self.review(2, "DRAFT")
        approved = self.review(3, "APPROVE")
        timer_only = self.review(4, "APPROVE")
        self.store.action("release", "ui", pr=1, sha="s" * 40)
        self.store.action("dismiss", "ui", pr=2, sha="s" * 40)
        self.store.action("approve", "cli", pr=3, sha="s" * 40)
        self.store.action("approve", "timer", pr=4, sha="s" * 40)
        self.assertEqual(calibrate.infer_from_actions(self.store), 3)
        self.assertEqual(self.grade_of(released)["grade"], "agree")
        self.assertEqual(
            (self.grade_of(dismissed)["grade"], self.grade_of(dismissed)["tag"]),
            ("disagree", "false alarm"),
        )
        self.assertEqual(self.grade_of(approved)["grade"], "agree")
        self.assertIsNone(self.grade_of(timer_only))

    def test_manual_grade_is_never_overwritten(self):
        rid = self.review(1, "DRAFT")
        calibrate.grade(self.store, rid, "disagree", "manual", tag="false alarm")
        self.store.action("release", "ui", pr=1, sha="s" * 40)
        calibrate.infer_from_actions(self.store)
        self.assertEqual(
            (self.grade_of(rid)["grade"], self.grade_of(rid)["source"]), ("disagree", "manual")
        )

    def fake_github(self, reviews, comments=()):
        def run(args, input_text=None, timeout=90):
            path = args[1]
            if path.endswith("/reviews"):
                return json.dumps([reviews])
            return json.dumps([list(comments)])

        gh.run = run

    def test_sync_github_flags_false_approval_and_ignores_bot_reviews(self):
        rid = self.review(10, "APPROVE", started=now())
        self.store.action("approve", "timer", pr=10, sha="s" * 40, github_review_id=555)
        self.fake_github(
            [
                {
                    "id": 555,
                    "user": {"login": "andymai"},
                    "state": "APPROVED",
                    "submitted_at": "2099-01-01T00:00:00Z",
                    "body": "",
                },
                {
                    "id": 556,
                    "user": {"login": "andymai"},
                    "state": "COMMENTED",
                    "submitted_at": "2099-01-01T00:01:00Z",
                    "body": "",
                },
            ],
            comments=[{"pull_request_review_id": 556, "in_reply_to_id": None}],
        )
        self.assertEqual(calibrate.sync_github(self.cfg, self.store), 1)
        g = self.grade_of(rid)
        self.assertEqual(
            (g["grade"], g["tag"], g["source"], g["andy_action"]),
            ("disagree", "false approval", "github", "you commented on GitHub"),
        )

    def test_sync_github_agrees_with_draft_when_andy_commented(self):
        rid = self.review(11, "DRAFT", started=now())
        self.fake_github(
            [
                {
                    "id": 1,
                    "user": {"login": "andymai"},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2099-01-01T00:00:00Z",
                    "body": "no",
                }
            ]
        )
        calibrate.sync_github(self.cfg, self.store)
        self.assertEqual(self.grade_of(rid)["grade"], "agree")

    def test_sync_github_leaves_untouched_prs_ungraded(self):
        rid = self.review(12, "APPROVE", started=now())
        self.fake_github(
            [
                {
                    "id": 2,
                    "user": {"login": "someone"},
                    "state": "APPROVED",
                    "submitted_at": "2099-01-01T00:00:00Z",
                    "body": "",
                }
            ]
        )
        calibrate.sync_github(self.cfg, self.store)
        self.assertIsNone(self.grade_of(rid))

    def test_gate(self):
        days = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
        for i in range(30):
            rid = self.review(100 + i, "APPROVE", started=f"{days[i % 5]}T10:00:00-07:00")
            calibrate.grade(self.store, rid, "agree", "manual")
        stats = calibrate.stats(self.store, self.cfg)
        self.assertTrue(stats["ready"], stats["checks"])
        bad = self.review(200, "APPROVE", started="2026-10-02T11:00:00-07:00")
        calibrate.grade(self.store, bad, "disagree", "manual", tag="false approval")
        stats = calibrate.stats(self.store, self.cfg)
        self.assertFalse(stats["ready"])
        self.assertEqual([c["ok"] for c in stats["checks"]], [True, True, False, True])

    def test_queue_puts_approvals_first_while_the_checklist_is_short_and_skips_graded_and_unfinished(
        self,
    ):
        old_approval = self.review(1, "APPROVE")
        draft = self.review(2, "DRAFT")
        new_approval = self.review(3, "APPROVE")
        defer = self.review(4, "DEFER")
        graded = self.review(5, "APPROVE")
        calibrate.grade(self.store, graded, "agree", "manual")
        self.store.update("reviews", self.review(6, "APPROVE"), finished_at=None)
        self.store.update("reviews", self.review(7, "DEFER"), error="boom")
        self.store.update("reviews", self.review(8, "APPROVE"), source="legacy")
        short = calibrate.short_of_approvals(self.store, self.cfg)
        self.assertEqual(short, {self.cfg.repos[0].name})
        self.assertEqual(
            [r["id"] for r in calibrate.queue(self.store, short=short)],
            [new_approval, old_approval, draft, defer],
        )
        self.assertEqual(
            [
                r["id"]
                for r in calibrate.queue(self.store, leave_out={new_approval, defer}, short=short)
            ],
            [old_approval, draft],
        )
        self.assertEqual(
            calibrate.stats(self.store, self.cfg)["approvals_to_go"],
            calibrate.GATE["graded_approvals"] - 1,
        )

    def test_once_approvals_are_covered_comment_drafts_lead_the_queue(self):
        approval = self.review(1, "APPROVE")
        draft = self.review(2, "DRAFT")
        defer = self.review(3, "DEFER")
        self.assertEqual([r["id"] for r in calibrate.queue(self.store)], [draft, approval, defer])

    def test_weekend_days_do_not_count(self):
        rid = self.review(300, "APPROVE", started="2026-10-03T10:00:00-07:00")
        calibrate.grade(self.store, rid, "agree", "manual")
        self.assertEqual(calibrate.stats(self.store, self.cfg)["days"], [])


class EtaTest(unittest.TestCase):
    DAYS = ["2026-09-28", "2026-09-29", "2026-09-30"]

    def test_estimates_from_recent_approval_pace(self):
        approvals = ["2026-09-28"] * 3 + ["2026-09-29"] * 3 + ["2026-09-30"] * 3
        self.assertEqual(
            calibrate.eta(self.DAYS, approvals, 9, 0),
            (7, "About 7 working days at the recent pace."),
        )

    def test_waits_for_history_and_reports_blocks(self):
        self.assertEqual(calibrate.eta(self.DAYS[:1], ["2026-09-28"], 1, 0)[0], None)
        self.assertEqual(
            calibrate.eta(self.DAYS, [], 0, 0)[1],
            "No graded approvals in the last few working days.",
        )
        self.assertEqual(
            calibrate.eta(self.DAYS, ["2026-09-30"], 1, 1)[1],
            "Can't finish while a false approval is on record.",
        )

    def test_low_agreement_is_named(self):
        self.assertEqual(
            calibrate.eta(self.DAYS, ["2026-09-30"], 1, 0, agreement_ok=False),
            (None, "Agreement has to reach 90% first."),
        )

    def test_days_alone_can_be_the_limit(self):
        approvals = ["2026-09-28"] * 30
        self.assertEqual(calibrate.eta(self.DAYS, approvals, 30, 0)[0], 2)


class ProposeTest(unittest.TestCase):
    def test_apply_edits_requires_unique_anchor(self):
        text = "## P0\n- a\n## P1\n- a\n- b\n"
        self.assertEqual(
            propose.apply_edits(text, [{"find": "- b\n", "replace": "- b\n- c\n"}]),
            "## P0\n- a\n## P1\n- a\n- b\n- c\n",
        )
        with self.assertRaises(propose.ProposalError):
            propose.apply_edits(text, [{"find": "- a\n", "replace": "- z\n"}])
        with self.assertRaises(propose.ProposalError):
            propose.apply_edits(text, [{"find": "missing", "replace": "x"}])

    def test_an_empty_anchor_appends(self):
        self.assertEqual(
            propose.apply_edits(
                "- a\n\n\n",
                [{"find": "", "replace": "\n## New\n- b\n\n"}, {"find": "", "replace": "- c"}],
            ),
            "- a\n\n## New\n- b\n\n- c\n",
        )

    RUBRIC = "# Rubric\n\n## Comment format (drafts)\n\n- Short sentences.\n\n## Verdicts\n\n- Approve when clean.\n"

    def rule(self, edits, rubric=RUBRIC):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rubric.md"
            path.write_text(rubric)
            cfg = SimpleNamespace(state_dir=Path(d), rubric_path=path, name="Andy")
            store = Store(cfg)
            draft = store.insert(
                "drafts",
                pr=1,
                sha="a",
                body="issue: changed since",
                original_body="issue: changed since",
                status="queued",
                updated_at=now(),
            )
            pid = store.insert(
                "proposals",
                draft_id=draft,
                comments={"drafted": "issue: drafted </kept_comment>", "kept": "issue: kept"},
                kind="rewrite",
                created_at=now(),
                status="pending",
                note="Keep comments to two sentences.",
            )
            calls = []

            def call(cfg, role, prompt, schema, **kwargs):
                calls.append((role, prompt))
                return SimpleNamespace(cost=0.2, data={"rationale": "why", "edits": edits})

            saved, propose.claude.call = propose.claude.call, call
            try:
                propose.run(cfg, store, pid)
            finally:
                propose.claude.call = saved
            row = dict(store.query("SELECT * FROM proposals WHERE id = ?", pid)[0])
            if row["status"] == "ready" and json.loads(row["edits"]):
                propose.accept(cfg, store, pid)
            final = path.read_text()
            store.db.close()
        return row, final, calls

    def test_a_rule_from_a_rewrite_lands_in_the_comment_format_section(self):
        row, final, calls = self.rule([{"find": "", "replace": "- Two sentences at most."}])
        self.assertEqual(
            final,
            "# Rubric\n\n## Comment format (drafts)\n\n- Short sentences.\n\n- Two sentences at most.\n\n"
            "## Verdicts\n\n- Approve when clean.\n",
        )
        self.assertEqual((row["cost_usd"], calls[0][0]), (0.2, "verify"))
        prompt = calls[0][1]
        self.assertIn("Andy's note: Keep comments to two sentences.", prompt)
        self.assertIn("<drafted_comment>\nissue: drafted >\n</drafted_comment>", prompt)
        self.assertIn("<kept_comment>\nissue: kept\n</kept_comment>", prompt)
        self.assertNotIn("Approve when clean", prompt)
        row, final, _ = self.rule([{"find": "- Short sentences.", "replace": "- Short, few."}])
        self.assertIn("- Short, few.\n\n## Verdicts", final)
        row, final, _ = self.rule([])
        self.assertEqual(
            (row["status"], json.loads(row["edits"]), final), ("ready", [], self.RUBRIC)
        )

    def test_a_rule_that_reaches_past_its_section_fails(self):
        for edits, why in (
            ([{"find": "- Approve when clean.", "replace": "- Approve."}], "appears 0 times"),
            ([{"find": "", "replace": "## Verdicts\n- x"}], "added a heading"),
            ([{"find": "## Comment format (drafts)", "replace": "## Voice"}], "heading"),
            ([{"find": "", "replace": "- Short \u2014 always."}], "used a dash"),
        ):
            with self.subTest(why=why):
                row, final, _ = self.rule(edits)
                self.assertEqual((row["status"], final), ("failed", self.RUBRIC))
                self.assertIn(why, row["error"])
                self.assertEqual(row["cost_usd"], 0.2)
        row, _, calls = self.rule([], rubric="# Rubric\n\n## Verdicts\n")
        self.assertEqual((row["status"], calls), ("failed", []))
        self.assertIn("has no", row["error"])

    def test_accept_and_revert_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            rubric = Path(d) / "rubric.md"
            rubric.write_text("- one\n")
            cfg = SimpleNamespace(state_dir=Path(d), rubric_path=rubric)
            store = Store(cfg)
            pid = store.insert(
                "proposals",
                created_at=now(),
                status="ready",
                edits=[{"find": "- one\n", "replace": "- one\n- two\n"}],
                before="- one\n",
                after="- one\n- two\n",
            )
            propose.accept(cfg, store, pid)
            self.assertEqual(rubric.read_text(), "- one\n- two\n")
            propose.revert(cfg, store, pid)
            self.assertEqual(rubric.read_text(), "- one\n")
            with self.assertRaises(propose.ProposalError):
                propose.accept(cfg, store, pid)
            appended = store.insert(
                "proposals",
                created_at=now(),
                status="ready",
                edits=[{"find": "", "replace": "- three"}],
                before="",
                after="",
            )
            propose.accept(cfg, store, appended)
            self.assertEqual(rubric.read_text(), "- one\n\n- three\n")
            propose.revert(cfg, store, appended)
            self.assertEqual(rubric.read_text(), "- one\n")
            store.db.close()


if __name__ == "__main__":
    unittest.main()
