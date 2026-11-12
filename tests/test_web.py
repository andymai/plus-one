import contextlib
import gzip
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PLUS1_CONFIG"] = str(ROOT / "tests" / "config.toml")
from plus1 import (  # noqa: E402
    actions,
    calibrate,
    config,
    gh,
    prs,
    signin,
)
from plus1.state import State  # noqa: E402
from plus1.store import Store, now  # noqa: E402
from plus1.web import app as webapp  # noqa: E402
from plus1.web import cards, executor, queries  # noqa: E402

OWNER = "owner@example.com"
SHA = "a" * 40
DIFF = """diff --git a/app/x.rb b/app/x.rb
index 1..2 100644
--- a/app/x.rb
+++ b/app/x.rb
@@ -1,3 +1,4 @@
 one
-two
+TWO
+three
 four
"""


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.load()
        cfg.state_dir = Path(self.tmp.name)
        cfg.notify = False
        self.cfg = cfg
        self.store = Store(cfg)
        self.run_id = self.store.start_run("manual", "shadow")
        self.ids = {}
        self.add(42, "DRAFT", "Worth <script>alert(1)</script> fixing", drafts=[("P1", 0)])
        self.add(43, "DRAFT", "Blocking one", drafts=[("P0", 1)])
        self.add(
            44,
            "APPROVE",
            "Clean change",
            reason="Only removes a stale entry. Nothing else references it.",
        )
        self.add(
            45,
            "DEFER",
            "Migration",
            reason="clean, but db/migrate/1_x.rb is owned by db-reviewers",
            raw={"verdict": "APPROVE", "coverage": "thorough", "findings": []},
        )
        self.add(46, "APPROVE", "Moved on", reason="fine")
        self.store.observe(
            self.run_id, 46, "eligible", "x", pr={"head": "b" * 40, "title": "Moved on"}
        )
        self.server = webapp.TcpServer(("127.0.0.1", 0), webapp.make_handler(cfg))
        self.port = self.server.server_address[1]
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.posted = []
        self._orig = (gh.post_review, gh.head_sha, actions.review_now, actions.notify)
        self.told = []
        actions.notify = lambda cfg, title, body, path="/", store=None: self.told.append(
            (title, body, path)
        )
        gh.post_review = lambda cfg, n, sha, event, body="", comments=(): (
            self.posted.append(
                {"n": n, "sha": sha, "event": event, "body": body, "comments": list(comments)}
            )
            or {"id": 900 + n}
        )
        gh.head_sha = lambda cfg, n: SHA
        actions.review_now = lambda cfg, n: self.posted.append({"review_now": n}) or "unit"

    def tearDown(self):
        gh.post_review, gh.head_sha, actions.review_now, actions.notify = self._orig
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def test_a_pr_in_your_code_leads_its_group_with_a_chip_and_says_why(self):
        self.add(47, "APPROVE", "In your code", reason="fine")
        self.store.db.execute(
            "UPDATE reviews SET yours = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "asked": "ann",
                        "lines": 30,
                        "changed": 40,
                        "top": [["app/models/cart.rb", 30]],
                        "top_count": 1,
                        "files": [],
                    }
                ),
                self.ids[47],
            ),
        )
        ready = [c["pr"] for c in cards.todo(self.store, self.cfg) if c["group"] == "looks_good"]
        self.assertEqual(ready[:2], [47, 44])
        _, page = self.request("GET", "/")
        row = re.search(r'id="card-47".*?</summary>', page, re.S).group(0)
        self.assertIn('<span class="yours-chip">asked you</span>', row)
        self.assertIn('<span class="yours-chip">your code</span>', row)
        _, pane = self.request("GET", f"/review/{self.ids[47]}")
        self.assertIn("ann asked you by name", pane)
        self.assertIn("Rewrites 30 lines you wrote in <code>cart.rb</code>", pane)

    def add(self, number, verdict, title, reason="r", drafts=(), raw=None):
        pr = {
            "number": number,
            "head": SHA,
            "base": "main",
            "title": title,
            "author": "teammate",
            "url": f"https://github.com/globex/web/pull/{number}",
            "labels": ["agent/claude-code"],
            "files": ["app/x.rb"],
            "additions": 2,
            "deletions": 1,
        }
        rid = self.store.start_review(self.run_id, pr, "requested", "substantive", "")
        self.store.update(
            "reviews",
            rid,
            verdict=verdict,
            reason=reason,
            diff=DIFF,
            finished_at=now(),
            raw_review=raw,
        )
        for severity, needs_edit in drafts:
            fid = self.store.insert(
                "findings",
                review_id=rid,
                idx=0,
                severity=severity,
                label="issue",
                title=f"{severity} finding title",
                path="app/x.rb",
                line=2,
                evidence="saw it",
                impact="it hurts",
                suggestion="fix it",
                confidence="high",
                verified=1,
                verify_reason="confirmed",
                final_path="app/x.rb",
                final_line=2,
            )
            self.store.insert(
                "drafts",
                review_id=rid,
                finding_id=fid,
                pr=number,
                sha=SHA,
                severity=severity,
                path="app/x.rb",
                line=2,
                inline=1,
                body=f"issue: <b>{severity}</b> body",
                original_body="x",
                needs_edit=needs_edit,
                unsupported=["a claim"] if needs_edit else [],
                status="queued",
                updated_at=now(),
            )
        self.ids[number] = rid
        return rid

    def request(
        self,
        method,
        path,
        login: str | None = OWNER,
        body=None,
        cookie=None,
        htmx=False,
        headers=None,
    ):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = dict(headers or {})
        if login:
            headers["Tailscale-User-Login"] = login
        if cookie:
            headers["Cookie"] = cookie
        if htmx:
            headers["HX-Request"] = "true"
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp, text

    def session(self):
        resp, text = self.request("GET", "/")
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        return cookie, re.search(r'name="csrf-token" content="([^"]+)"', text).group(1)

    def act(self, number, action, htmx=True, **fields):
        cookie, token = self.session()
        body = {
            "csrf": token,
            "next": "/",
            "variant": "card",
            "review_id": self.ids.get(number, ""),
            **fields,
        }
        return self.request("POST", f"/card/{number}/{action}", cookie=cookie, htmx=htmx, body=body)

    def run_due(self):
        return executor.run_due(
            self.cfg, self.store, now_ts=time.time() + executor.UNDO_SECONDS + 1
        )

    FIXED = {
        "thread_id": "T1",
        "url": "https://github.com/globex/web/pull/50#discussion_r1",
        "path": "app/x.rb",
        "line": 2,
        "title": "Restore the timeout",
        "open": False,
        "why": "Line 2 sets it back to 30.",
    }
    OPEN = {
        "thread_id": "T2",
        "url": "https://github.com/globex/web/pull/50#discussion_r2",
        "path": "app/x.rb",
        "line": 3,
        "title": "Stop the retry loop",
        "open": True,
        "why": "It still loops forever.",
    }

    def add_followup(self, number, items, verdict="APPROVE", reason="Safe now.", drafts=()):
        rid = self.add(number, verdict, f"Follow-up {number}", reason=reason, drafts=drafts)
        self.store.update("reviews", rid, followup={"sha": "c" * 40, "items": items})
        return rid

    def resolving(self):
        self.resolved, saved = [], gh.resolve_thread
        gh.resolve_thread = lambda thread_id: self.resolved.append(thread_id) or True
        self.addCleanup(setattr, gh, "resolve_thread", saved)

    def test_a_fixed_follow_up_approves_and_resolves_only_the_fixed_threads(self):
        self.resolving()
        by_author = {
            **self.FIXED,
            "thread_id": "T9",
            "resolved": True,
            "resolved_by": "hannah",
            "title": "Rename the job",
        }
        self.add_followup(
            50, [self.FIXED, {**self.OPEN, "open": False, "thread_id": None}, by_author]
        )
        _, page = self.request("GET", "/")
        self.assertIn("Fixed since you asked", page)
        self.assertIn('href="https://github.com/globex/web/pull/50#discussion_r1"', page)
        self.assertIn("All 3 of your comments are fixed.", page)
        self.assertIn("though hannah marked it resolved", page)
        self.act(50, "approve_followup")
        self.run_due()
        self.assertEqual([(p["n"], p["event"]) for p in self.posted], [(50, "APPROVE")])
        self.assertEqual(self.resolved, ["T1"])
        self.assertNotIn("card-50", self.request("GET", "/")[1])

    def test_a_follow_up_with_asks_still_open_waits_by_default(self):
        self.resolving()
        self.add_followup(
            51,
            [self.FIXED, self.OPEN],
            verdict="DEFER",
            reason="still open since you asked for changes: Stop the retry loop",
        )
        _, page = self.request("GET", "/")
        self.assertIn("Approve anyway, or keep waiting?", page)
        self.assertIn("1 of 2 still open: Stop the retry loop", page)
        self.act(51, "keep_waiting")
        self.run_due()
        self.assertEqual(self.posted, [])
        self.assertEqual(
            self.store.query("SELECT how FROM handled WHERE pr = 51")[0]["how"], "keep waiting"
        )
        self.assertNotIn("card-51", self.request("GET", "/")[1])

    def test_approving_anyway_resolves_what_was_fixed_and_leaves_the_rest(self):
        self.resolving()
        self.add_followup(
            51,
            [self.FIXED, self.OPEN],
            verdict="DEFER",
            reason="still open since you asked for changes: Stop",
        )
        self.act(51, "approve_followup")
        self.run_due()
        self.assertEqual(([p["event"] for p in self.posted], self.resolved), (["APPROVE"], ["T1"]))

    def test_new_comments_on_a_follow_up_keep_your_block_while_asks_are_open(self):
        self.resolving()
        self.add_followup(
            52, [self.FIXED, self.OPEN], verdict="DRAFT", reason="new", drafts=[("P1", 0)]
        )
        _, page = self.request("GET", "/")
        self.assertIn("Post 1 comment, and keep asking for changes?", page)
        self.act(52, "approve_with_comment")
        self.run_due()
        self.assertEqual((self.posted[0]["event"], self.resolved), ("COMMENT", []))

    def test_new_comments_once_every_ask_is_fixed_approve_and_resolve(self):
        self.resolving()
        self.add_followup(53, [self.FIXED], verdict="DRAFT", reason="new", drafts=[("P1", 0)])
        self.act(53, "approve_with_comment")
        self.run_due()
        self.assertEqual((self.posted[0]["event"], self.resolved), ("APPROVE", ["T1"]))

    def test_a_follow_up_with_nothing_new_since_shows_no_card(self):
        from plus1.review import FOLLOWUP_UNCHANGED

        self.add_followup(54, [self.OPEN], verdict="DEFER", reason=FOLLOWUP_UNCHANGED)
        self.assertNotIn("card-54", self.request("GET", "/")[1])

    def test_auth(self):
        self.assertEqual(self.request("GET", "/", login=None)[0].status, 403)
        self.assertEqual(self.request("GET", "/", login="stranger@example.com")[0].status, 403)
        cookie, _ = self.session()
        self.assertEqual(
            self.request("POST", "/card/44/approve", cookie=cookie, body={"next": "/"})[0].status,
            403,
        )

    def test_todo_orders_cards_in_plain_words_and_escapes(self):
        resp, text = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        order = [
            text.index(h)
            for h in (
                "Looks good, would approve",
                "Found a blocking problem",
                "Worth fixing before it ships",
                "Needs your eyes",
            )
        ]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;b&gt;P1&lt;/b&gt;", text)
        self.assertIn("approving is up to you", text)
        self.assertIn("(5) To do", text)
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy") or "")

    def test_post_waits_for_undo_window_then_posts(self):
        _resp, text = self.act(42, "approve_with_comment")
        self.assertIn("Undo", text)
        self.assertEqual(self.posted, [])
        self.assertEqual(executor.run_due(self.cfg, self.store), 0)
        self.assertEqual(self.run_due(), 1)
        post = self.posted[-1]
        self.assertEqual((post["n"], post["event"]), (42, "APPROVE"))
        self.assertEqual(
            post["comments"], [{"path": "app/x.rb", "line": 2, "body": "issue: <b>P1</b> body"}]
        )
        _, card = self.request("GET", "/partials/card/42", htmx=True)
        self.assertIn("Posted the comment", card)
        calibrate.infer_from_actions(self.store)
        self.assertEqual(
            self.store.query("SELECT grade FROM grades WHERE review_id = ?", self.ids[42])[0][0],
            "agree",
        )

    def test_undo_cancels(self):
        _, text = self.act(44, "approve")
        pending_id = re.search(r"/pending/(\d+)/undo", text).group(1)
        cookie, token = self.session()
        _resp, text = self.request(
            "POST",
            f"/pending/{pending_id}/undo",
            cookie=cookie,
            htmx=True,
            body={"csrf": token, "variant": "card"},
        )
        self.assertIn("Looks good, would approve", text)
        self.assertEqual(self.run_due(), 0)
        self.assertEqual(self.posted, [])

    def test_unchecked_comment_and_stale_pr_cannot_post(self):
        _, page = self.request("GET", "/")
        self.assertIn("<li>a claim</li>", page)
        _, text = self.act(43, "post")
        self.assertIn("Check the comment first", text)
        _, text = self.act(46, "approve")
        self.assertIn("New commits since this review", text)
        self.assertEqual(self.run_due(), 0)
        _, text = self.act(
            43,
            "save_comment",
            draft_id=self.store.query("SELECT id FROM drafts WHERE pr = 43")[0][0],
            body="issue (blocking): fixed wording",
        )
        self.assertIn("Comment saved.", text)
        self.act(43, "post")
        self.run_due()
        self.assertEqual((self.posted[-1]["n"], self.posted[-1]["event"]), (43, "REQUEST_CHANGES"))

    def rejected_review(self, number=49):
        rid = self.add(
            number,
            "DEFER",
            "Kill switch PR",
            reason="findings did not survive verification: switch is live",
            raw={
                "verdict": "DEFER",
                "coverage": "thorough",
                "findings": [{}],
                "summary": "The switch is live.",
            },
        )
        common = {
            "review_id": rid,
            "label": "question",
            "path": "app/x.rb",
            "line": 2,
            "evidence": "DEFAULT_VALUE = false",
            "impact": "Addie reads productless rows",
            "suggestion": "default it on",
            "confidence": "high",
            "verified": 0,
            "final_path": "app/x.rb",
            "final_line": 2,
        }
        fid = self.store.insert(
            "findings",
            idx=0,
            severity="P1",
            title="Kill switch is live on merge",
            verify_reason="The migration runs before pods roll out.",
            **common,
        )
        self.store.insert(
            "findings",
            idx=1,
            severity="P1",
            title="Old problem now fixed",
            verify_reason="fixed",
            carried="1",
            **common,
        )
        return rid, fid

    def writing(self, rid, fid, at=None):
        return self.store.insert(
            "drafts",
            review_id=rid,
            finding_id=fid,
            pr=49,
            sha=SHA,
            severity="P1",
            status="writing",
            updated_at=at or now(),
        )

    def write_with(self, draft_id, write):
        from plus1 import cli, drafts

        saved, drafts.write_comment = drafts.write_comment, write
        try:
            return cli.write_up(self.cfg, self.store, draft_id)
        finally:
            drafts.write_comment = saved

    @staticmethod
    def written(cfg, pr, finding, rec=None, idx=0):
        return {
            "body": f"question (non-blocking): {finding['title']}",
            "needs_edit": False,
            "unsupported": [],
            "cost": 0.0,
        }

    def test_a_needs_a_look_card_shows_what_the_double_check_dropped(self):
        self.rejected_review()
        _, page = self.request("GET", "/")
        for text in (
            "What it couldn",
            "Kill switch is live on merge",
            "The migration runs before pods roll out.",
            "Write it up",
        ):
            self.assertIn(text, page)
        self.assertNotIn("Old problem now fixed", page)

    def test_write_up_drafts_one_comment_you_can_post(self):
        rid, fid = self.rejected_review()
        spawned = []
        saved, actions.spawn = (
            actions.spawn,
            lambda cfg, name, *args: spawned.append((name, *args)) or "unit",
        )
        try:
            _, text = self.act(49, "write_up", finding_id=fid)
            self.assertIn("Writing the comment", text)
            self.assertIn('hx-get="/partials/card/49?variant=card"', text)
            _, again = self.act(49, "write_up", finding_id=fid)
            self.assertIn("already being written", again)
        finally:
            actions.spawn = saved
        draft_id = spawned[0][2]
        self.assertEqual(spawned, [("write-up-49", "write-up", draft_id)])
        self.assertTrue(self.write_with(draft_id, self.written))
        card = cards.card_for_pr(self.store, self.cfg, 49)
        self.assertEqual(
            (card["state"], card["summary"]), ("worth_fixing", "Kill switch is live on merge")
        )
        self.assertEqual(
            card["drafts"][0]["body"], "question (non-blocking): Kill switch is live on merge"
        )
        _, page = self.request("GET", f"/review/{rid}")
        self.assertIn("you overruled the double-check", page)
        self.assertIn("Why the double-check dropped it", page)

    def test_a_failed_or_stalled_write_up_offers_the_button_again(self):
        rid, fid = self.rejected_review()

        def down(*args, **kwargs):
            raise RuntimeError("the writer is down")

        self.assertFalse(self.write_with(self.writing(rid, fid), down))
        f = cards.card_for_pr(self.store, self.cfg, 49)["rejected"][0]
        self.assertEqual((f["writing"], f["write_error"]), (False, "the writer is down"))
        self.assertEqual(
            self.told[-1][:2], ("Couldn't write the comment", "#49: the writer is down")
        )
        self.writing(
            rid,
            fid,
            at=(datetime.now().astimezone() - timedelta(minutes=20)).isoformat(timespec="seconds"),
        )
        f = cards.card_for_pr(self.store, self.cfg, 49)["rejected"][0]
        self.assertEqual(
            (f["writing"], f["write_error"]), (False, "The writer stopped before it finished.")
        )
        _, page = self.request("GET", "/")
        self.assertIn("Write it up", page)

    def test_a_write_up_that_finishes_after_a_newer_review_stays_retired(self):
        rid, fid = self.rejected_review()
        draft_id = self.writing(rid, fid)
        self.add(49, "APPROVE", "Kill switch PR", reason="fine")
        self.assertTrue(self.write_with(draft_id, self.written))
        self.assertEqual(
            self.store.query("SELECT status FROM drafts WHERE id = ?", draft_id)[0][0], "superseded"
        )

    def test_write_up_takes_only_a_dropped_finding_on_a_needs_a_look_card(self):
        self.rejected_review()
        verified = self.store.query("SELECT id FROM findings WHERE review_id = ?", self.ids[42])[0][
            0
        ]
        self.assertIn("Pick a finding", self.act(49, "write_up", finding_id=verified)[1])
        self.assertIn("Pick a finding", self.act(49, "write_up", finding_id="x")[1])
        self.assertIn("anymore", self.act(44, "write_up", finding_id=verified)[1])
        carried = self.store.query(
            "SELECT id FROM findings WHERE review_id = ? AND carried = '1'", self.ids[49]
        )[0][0]
        self.assertIn("Pick a finding", self.act(49, "write_up", finding_id=carried)[1])
        with self.assertRaises(actions.ActionError):
            actions.write_up(self.cfg, self.store, self.ids[49], carried)

    def test_a_writer_that_cannot_start_leaves_the_button_not_a_500(self):
        _rid, fid = self.rejected_review()

        def no_systemd(cfg, name, *args):
            raise FileNotFoundError("systemd-run")

        saved, actions.spawn = actions.spawn, no_systemd
        try:
            resp, text = self.act(49, "write_up", finding_id=fid)
        finally:
            actions.spawn = saved
        self.assertEqual(resp.status, 200)
        self.assertIn("Could not start the writer", text)
        self.assertEqual(
            [r[0] for r in self.store.query("SELECT status FROM drafts WHERE finding_id = ?", fid)],
            ["failed"],
        )
        self.assertIn("Write it up", text)

    def test_finishing_the_card_retires_a_comment_still_being_written(self):
        rid, fid = self.rejected_review()
        draft_id = self.writing(rid, fid)
        self.act(49, "handled")
        self.run_due()
        self.assertFalse(self.write_with(draft_id, self.written))
        self.assertEqual(
            self.store.query("SELECT status FROM drafts WHERE id = ?", draft_id)[0][0], "superseded"
        )

    def test_a_write_up_counts_on_the_day_it_ran_not_the_reviews_day(self):
        rid, _ = self.rejected_review()
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        self.store.update("reviews", rid, started_at=yesterday, finished_at=yesterday, cost_usd=1.0)
        self.store.insert(
            "stages", review_id=rid, stage="factcheck", idx=0, started_at=now(), cost_usd=0.25, ok=1
        )
        spend = {d["day"]: d["cost"] for d in queries.daily_spend(self.store)}
        self.assertAlmostEqual(spend[yesterday[:10]], 1.0)
        self.assertAlmostEqual(spend[now()[:10]], 0.25)
        self.assertEqual(queries.late_spend(self.store, yesterday[:10]), {now()[:10]: 0.25})
        self.assertAlmostEqual(queries.weekly(self.store)["spend"], 1.25)

    def test_opening_a_store_with_duplicate_writers_keeps_the_newest(self):
        rid, fid = self.rejected_review()
        self.store.db.execute("DROP INDEX drafts_one_writer")
        old, new = self.writing(rid, fid), self.writing(rid, fid)
        Store(self.cfg).db.close()
        self.assertEqual(
            dict(self.store.query("SELECT id, status FROM drafts WHERE finding_id = ?", fid)),
            {old: "failed", new: "writing"},
        )

    def test_only_one_writer_per_finding_and_a_given_up_one_cannot_queue(self):
        rid, fid = self.rejected_review()
        self.writing(rid, fid)
        with self.assertRaises(actions.ActionError):
            actions.write_up(self.cfg, self.store, rid, fid)
        self.store.db.execute("DELETE FROM drafts WHERE finding_id = ?", (fid,))
        stalled = self.writing(
            rid,
            fid,
            at=(datetime.now().astimezone() - timedelta(minutes=20)).isoformat(timespec="seconds"),
        )
        saved, actions.spawn = actions.spawn, lambda cfg, name, *args: "unit"
        try:
            retry = actions.write_up(self.cfg, self.store, rid, fid)
        finally:
            actions.spawn = saved
        self.assertFalse(self.write_with(stalled, self.written))
        statuses = dict(self.store.query("SELECT id, status FROM drafts WHERE finding_id = ?", fid))
        self.assertEqual(statuses, {stalled: "failed", retry: "writing"})

    def draft_of(self, number):
        return self.store.query("SELECT id FROM drafts WHERE pr = ?", number)[0][0]

    def ask_rewrite(self, number, suggestion, **fields):
        spawned = []
        saved, actions.spawn = (
            actions.spawn,
            lambda cfg, name, *args: spawned.append((name, *args)) or "unit",
        )
        try:
            _, text = self.act(
                number,
                "rewrite",
                draft_id=fields.pop("draft_id", self.draft_of(number)),
                suggestion=suggestion,
                **fields,
            )
        finally:
            actions.spawn = saved
        return text, spawned[0][2] if spawned else None

    def rewrite_with(self, rewrite_id, body="issue (non-blocking): Shorter.", flags=(), fail=None):
        from plus1 import cli, drafts

        def fake(cfg, pr, finding, base, notes, code="", rec=None, idx=0):
            if fail:
                raise RuntimeError(fail)
            self.rewritten.append((base, notes, code))
            return {
                "body": body,
                "needs_edit": bool(flags),
                "unsupported": list(flags),
                "cost": 0.05,
            }

        saved, drafts.rewrite_comment = drafts.rewrite_comment, fake
        try:
            return cli.rewrite(self.cfg, self.store, rewrite_id)
        finally:
            drafts.rewrite_comment = saved

    def draft_row(self, number):
        return dict(self.store.query("SELECT * FROM drafts WHERE pr = ?", number)[0])

    def test_a_rewrite_waits_beside_the_comment_until_you_use_it(self):
        self.rewritten = []
        self.store.db.execute("UPDATE drafts SET original_body = body")
        draft_id = self.draft_of(42)
        _, page = self.request("GET", "/")
        self.assertIn("/card/42/rewrite", page)
        self.assertIn("Ask for a rewrite", page)
        self.assertNotIn("Restore original", page)
        text, rewrite_id = self.ask_rewrite(42, "", chip="Make it shorter.")
        self.assertIn("Rewriting: “Make it shorter.”", text)
        self.assertIn(f"rewrite={rewrite_id}", text)
        self.assertNotIn("/card/42/rewrite", text)
        resp, _ = self.request(
            "GET", f"/partials/card/42?variant=card&rewrite={rewrite_id}", htmx=True
        )
        self.assertEqual(resp.status, 204)
        self.assertIn("already being rewritten", self.ask_rewrite(42, "Make it softer.")[0])
        self.assertTrue(self.rewrite_with(rewrite_id))
        base, notes, code = self.rewritten[0]
        self.assertEqual((base, notes), ("issue: <b>P1</b> body", ["Make it shorter."]))
        self.assertIn(">>     2 +TWO", code)
        resp, card = self.request(
            "GET", f"/partials/card/42?variant=card&rewrite={rewrite_id}", htmx=True
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("Use this", card)
        self.assertIn("issue (non-blocking): Shorter.", card)
        self.assertEqual(self.draft_row(42)["body"], "issue: <b>P1</b> body")
        _, used = self.act(42, "use_rewrite", draft_id=draft_id, rewrite_id=rewrite_id)
        self.assertIn("Using the rewrite", used)
        self.assertNotIn("Use this", used)
        self.assertIn("Make this a rule", used)
        self.assertIn("Restore original", used)
        row = self.draft_row(42)
        self.assertEqual(
            (row["body"], json.loads(row["notes"])),
            ("issue (non-blocking): Shorter.", ["Make it shorter."]),
        )
        self.assertEqual(
            [
                r[0]
                for r in self.store.query(
                    "SELECT action FROM actions WHERE action LIKE '%rewrite%' ORDER BY id"
                )
            ],
            ["rewrite_draft", "use_rewrite"],
        )
        self.act(42, "approve_with_comment")
        self.run_due()
        self.assertEqual(self.posted[-1]["comments"][0]["body"], "issue (non-blocking): Shorter.")

    def test_rewrite_again_revises_the_proposal_and_keep_mine_drops_the_chain(self):
        self.rewritten = []
        self.store.db.execute("UPDATE drafts SET original_body = body")
        draft_id = self.draft_of(42)
        first = self.ask_rewrite(42, "Make it shorter.")[1]
        self.rewrite_with(first, body="issue (non-blocking): Short.")
        second = self.ask_rewrite(42, "Ask it as a question.", parent_id=first)[1]
        waiting = cards.card_for_pr(self.store, self.cfg, 42)["drafts"][0]["rewrite"]
        self.assertEqual((waiting["ready"]["id"], waiting["writing"]["id"]), (first, second))
        self.rewrite_with(second, body="issue (non-blocking): Short?")
        self.assertEqual(
            self.rewritten[-1][:2],
            ("issue (non-blocking): Short.", ["Make it shorter.", "Ask it as a question."]),
        )
        self.assertEqual(
            dict(self.store.query("SELECT id, status FROM rewrites")),
            {first: "revised", second: "ready"},
        )
        self.assertIn(
            "isn&#39;t waiting anymore",
            self.act(42, "use_rewrite", draft_id=draft_id, rewrite_id=first)[1],
        )
        _, kept = self.act(42, "keep_mine", draft_id=draft_id, rewrite_id=second)
        self.assertNotIn("Use this", kept)
        self.assertNotIn("Restore original", kept)
        self.assertIn("/card/42/rewrite", kept)
        self.assertEqual(self.draft_row(42)["body"], "issue: <b>P1</b> body")
        self.assertIn(
            "isn&#39;t waiting anymore",
            self.act(42, "use_rewrite", draft_id=draft_id, rewrite_id=second)[1],
        )

    def test_a_flagged_rewrite_keeps_its_flag_and_restore_brings_back_the_first_drafts(self):
        self.rewritten = []
        self.store.db.execute("UPDATE drafts SET original_body = body")
        clean = self.draft_of(42)
        rewrite_id = self.ask_rewrite(42, "Say the iOS client reads it.")[1]
        self.rewrite_with(
            rewrite_id,
            body="issue (non-blocking): The iOS client reads it.",
            flags=["The iOS client reads it"],
        )
        _, proposed = self.request("GET", "/")
        self.assertIn("Using this keeps the flag", proposed)
        _, used = self.act(42, "use_rewrite", draft_id=clean, rewrite_id=rewrite_id)
        self.assertIn("Check this before posting", used)
        self.assertIn("Check the comment first", self.act(42, "approve_with_comment")[1])
        _, back = self.act(42, "restore_original", draft_id=clean)
        self.assertIn("Back to the first draft", back)
        row = self.draft_row(42)
        self.assertEqual(
            (row["body"], row["needs_edit"], json.loads(row["unsupported"]), row["notes"]),
            ("issue: <b>P1</b> body", 0, [], None),
        )
        self.assertIn(
            "already the first draft", self.act(42, "restore_original", draft_id=clean)[1]
        )
        flagged = self.draft_of(43)
        rewrite_id = self.ask_rewrite(43, "Make it shorter.")[1]
        self.rewrite_with(rewrite_id, body="issue (blocking): Short.")
        self.act(43, "use_rewrite", draft_id=flagged, rewrite_id=rewrite_id)
        self.assertEqual(self.draft_row(43)["needs_edit"], 0)
        self.act(43, "restore_original", draft_id=flagged)
        row = self.draft_row(43)
        self.assertEqual((row["needs_edit"], json.loads(row["unsupported"])), (1, ["a claim"]))
        self.store.db.execute(
            "UPDATE drafts SET body = 'edited', original_needs_edit = 1, original_unsupported = '[]' WHERE id = ?",
            (clean,),
        )
        self.act(42, "restore_original", draft_id=clean)
        self.assertEqual(self.draft_row(42)["needs_edit"], 1)

    def test_posting_acts_on_the_current_text_and_retires_the_rewrite(self):
        self.rewritten = []
        self.add(47, "DRAFT", "Second", drafts=[("P1", 0)])
        ready = self.ask_rewrite(42, "Make it shorter.")[1]
        self.rewrite_with(ready)
        writing = self.ask_rewrite(47, "Make it shorter.")[1]
        for number in (42, 47):
            self.act(number, "approve_with_comment")
        self.run_due()
        self.assertEqual(
            [p["comments"][0]["body"] for p in self.posted if p.get("n") in (42, 47)],
            ["issue: <b>P1</b> body", "issue: <b>P1</b> body"],
        )
        self.assertFalse(self.rewrite_with(writing))
        self.assertEqual(
            dict(self.store.query("SELECT id, status FROM rewrites")),
            {ready: "superseded", writing: "superseded"},
        )

    def test_a_rewrite_needs_words_and_a_failed_one_offers_the_button_again(self):
        self.rewritten = []
        self.assertIn("Say how the comment should change", self.ask_rewrite(42, "  ")[0])
        self.assertIn("under 500 characters", self.ask_rewrite(42, "x" * 601)[0])
        self.assertIsNotNone(self.ask_rewrite(42, "x" * 500, chip="Ask it as a question.")[1])
        self.store.db.execute("DELETE FROM rewrites")
        other = self.draft_of(43)
        self.assertIn(
            "belongs to an older review", self.ask_rewrite(42, "Shorter", draft_id=other)[0]
        )
        self.assertIn("isn&#39;t on this card", self.ask_rewrite(42, "Shorter", draft_id="x")[0])
        rewrite_id = self.ask_rewrite(42, "Make it shorter.", chip="Ask it as a question.")[1]
        self.assertEqual(
            self.store.query("SELECT suggestion FROM rewrites WHERE id = ?", rewrite_id)[0][0],
            "Make it shorter. Ask it as a question.",
        )
        self.assertFalse(self.rewrite_with(rewrite_id, fail="the writer is down"))
        self.assertEqual(
            self.told[-1][:2], ("Couldn't rewrite the comment", "#42: the writer is down")
        )
        _, page = self.request("GET", "/")
        self.assertIn("Couldn't rewrite it: the writer is down", page)
        self.assertIn("/card/42/rewrite", page)
        self.store.db.execute("DELETE FROM rewrites")
        old = (datetime.now().astimezone() - timedelta(minutes=20)).isoformat(timespec="seconds")
        stalled = self.store.insert(
            "rewrites",
            draft_id=self.draft_of(42),
            suggestion="s",
            notes=["s"],
            base="b",
            status="writing",
            created_at=old,
            updated_at=old,
        )
        _, page = self.request("GET", "/")
        self.assertIn("The writer stopped before it finished.", page)
        retry = self.ask_rewrite(42, "Make it shorter.")[1]
        self.assertEqual(
            self.store.query("SELECT status FROM rewrites WHERE id = ?", stalled)[0][0], "failed"
        )
        self.assertFalse(self.rewrite_with(stalled))
        self.assertTrue(self.rewrite_with(retry))

    def test_restore_original_and_keep_mine_retire_every_open_rewrite(self):
        self.rewritten = []
        draft_id = self.draft_of(42)

        def status(rewrite_id):
            return self.store.query("SELECT status FROM rewrites WHERE id = ?", rewrite_id)[0][0]

        waiting = self.ask_rewrite(42, "Make it shorter.")[1]
        self.rewrite_with(waiting)
        _, page = self.act(42, "restore_original", draft_id=draft_id)
        self.assertNotIn("Use this", page)
        self.assertEqual(status(waiting), "superseded")
        parent = self.ask_rewrite(42, "Make it shorter.")[1]
        self.rewrite_with(parent)
        alongside = self.store.insert(
            "rewrites",
            draft_id=draft_id,
            parent_id=parent,
            suggestion="s",
            notes=["s"],
            base="b",
            body="issue: s",
            status="ready",
            created_at=now(),
            updated_at=now(),
        )
        self.act(42, "keep_mine", draft_id=draft_id, rewrite_id=parent)
        self.assertEqual((status(parent), status(alongside)), ("discarded", "superseded"))

    def test_a_draft_without_a_finding_offers_no_rewrite(self):
        self.store.db.execute("UPDATE drafts SET finding_id = NULL WHERE pr = 43")
        _, page = self.request("GET", "/")
        self.assertIn("/card/43/save_comment", page)
        self.assertNotIn("/card/43/rewrite", page)
        self.assertIn("no finding to check a rewrite against", self.ask_rewrite(43, "Shorter")[0])

    def test_a_rewriter_that_cannot_start_leaves_the_button_not_a_500(self):
        def no_systemd(cfg, name, *args):
            raise FileNotFoundError("systemd-run")

        saved, actions.spawn = actions.spawn, no_systemd
        try:
            resp, text = self.act(
                42, "rewrite", draft_id=self.draft_of(42), suggestion="Make it shorter."
            )
        finally:
            actions.spawn = saved
        self.assertEqual(resp.status, 200)
        self.assertIn("Could not start the rewrite", text)
        self.assertIn("/card/42/rewrite", text)
        self.assertEqual(
            self.store.query("SELECT COUNT(*) FROM actions WHERE action = 'rewrite_draft'")[0][0], 0
        )

    def test_make_this_a_rule_drafts_one_rubric_change_from_a_used_rewrite(self):
        self.rewritten = []
        draft_id = self.draft_of(42)
        self.assertIn(
            "Only a comment you had rewritten",
            self.act(42, "make_rule", draft_id=draft_id, note="x")[1],
        )
        first = self.ask_rewrite(42, "Make it shorter.")[1]
        self.rewrite_with(first)
        self.act(42, "use_rewrite", draft_id=draft_id, rewrite_id=first)
        _, page = self.request("GET", "/")
        self.assertIn('maxlength="1000" required>Make it shorter.</textarea>', page)
        spawned = []
        saved, actions.spawn = (
            actions.spawn,
            lambda cfg, name, *args: spawned.append((name, *args)) or "unit",
        )
        try:
            self.assertIn(
                "Say what future comments should do",
                self.act(42, "make_rule", draft_id=draft_id, note=" ")[1],
            )
            _, text = self.act(
                42, "make_rule", draft_id=draft_id, note="Keep comments to two sentences."
            )
            _, again = self.act(42, "make_rule", draft_id=draft_id, note="Again")
        finally:
            actions.spawn = saved
        self.assertIn("Drafting the rule", text)
        self.assertIn("already being drafted", again)
        row = dict(self.store.query("SELECT * FROM proposals")[0])
        self.assertEqual(
            (row["kind"], row["draft_id"], row["status"], row["note"]),
            ("rewrite", draft_id, "pending", "Keep comments to two sentences."),
        )
        self.assertEqual(spawned, [(f"propose-{row['id']}", "propose", row["id"])])
        self.assertEqual(
            json.loads(row["comments"]),
            {"drafted": "x", "kept": "issue (non-blocking): Shorter."},
        )
        self.assertIn(f'href="/proposals/{row["id"]}">Rubric changes', text)
        _, detail = self.request("GET", f"/proposals/{row['id']}")
        self.assertIn("Comment rule from", detail)

    def test_would_comment_grades_and_clears_card(self):
        _, text = self.act(
            44, "would_comment", note="It should flag the missing spec", tag="missed issue"
        )
        self.assertIn("Teach it", text)
        grade = self.store.query("SELECT * FROM grades WHERE review_id = ?", self.ids[44])[0]
        self.assertEqual(
            (grade["grade"], grade["tag"], grade["source"]), ("disagree", "missed issue", "manual")
        )
        _, page = self.request("GET", "/")
        self.assertNotIn("Clean change", page)

    def test_handled_and_dont_post(self):
        self.act(45, "handled")
        self.act(42, "dont_post")
        self.run_due()
        _, page = self.request("GET", "/")
        self.assertNotIn("Migration", page)
        self.assertNotIn("Worth &lt;script&gt;", page)
        self.assertEqual(self.posted, [])

    def test_non_htmx_actions_redirect(self):
        resp, _ = self.act(44, "approve", htmx=False)
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/"))

    def test_pages_render_and_old_urls_redirect(self):
        for path in (
            "/activity",
            "/activity?show=waiting",
            "/activity?show=skipped",
            "/activity?q=Clean",
            "/insights",
            f"/review/{self.ids[42]}",
            f"/review/{self.ids[45]}",
            "/pr/42",
            "/proposals",
            "/partials/live",
            "/partials/status",
            "/static/app.css",
            "/palette.json",
        ):
            self.assertEqual(self.request("GET", path)[0].status, 200, path)
        resp, _ = self.request("GET", "/devices")
        self.assertEqual(resp.getheader("Location"), "/settings#notifications")
        for old in ("/history", "/live", "/calibration", "/weekly", "/ops"):
            resp, _ = self.request("GET", old)
            self.assertEqual(resp.status, 301, old)
        _, review = self.request("GET", f"/review/{self.ids[42]}")
        self.assertIn("Why it thinks so", review)
        self.assertIn("What it saw", review)
        self.assertIn('class="add mark"', review)
        self.assertEqual(self.request("GET", "/review/99999")[0].status, 404)

    def test_favicon_follows_todo_count(self):
        def favicon():
            count = json.loads(self.request("GET", "/todo-state.json")[1])["count"]
            _, page = self.request("GET", "/")
            href = re.search(r'<link rel="icon" [^>]*href="([^"]+)"', page).group(1)
            self.assertEqual(self.request("GET", href)[0].status, 200)
            return count, href

        count, href = favicon()
        self.assertTrue(0 < count < 9)
        self.assertEqual(href, f"/static/fav-{count}.svg")
        for n in range(100, 110):
            self.add(n, "DRAFT", f"Extra {n}", drafts=[("P1", 0)])
        count, href = favicon()
        self.assertGreater(count, 9)
        self.assertEqual(href, "/static/fav-9.svg")
        for n in self.ids:
            self.store.set_pr_status(n, "MERGED", "APPROVED", False)
        self.store.db.execute("UPDATE drafts SET status = 'released'")
        self.assertEqual(favicon(), (0, "/static/icon.svg"))

        static = webapp.HERE / "static"
        manifest = json.loads((static / "manifest.webmanifest").read_text())
        names = [icon["src"].removeprefix("/static/") for icon in manifest["icons"]]
        names += re.findall(r'"/static/([\w.-]+)"', (static / "sw.js").read_text())
        names += [f"fav-{n}.svg" for n in range(1, 10)] + ["icon.svg", "apple-touch-icon.png"]
        for name in names:
            self.assertTrue((static / name).is_file(), name)

    def test_review_page_puts_the_code_by_the_comment_and_links_to_the_next_card(self):
        _, page = self.request("GET", f"/review/{self.ids[42]}")
        self.assertIn('class="excerpt"', page)
        self.assertNotIn("What to do", page)
        self.assertIn('id="D-app-x-rb-2"', page)
        order = [c["pr"] for c in cards.todo(self.store, self.cfg)]
        nxt = order[order.index(42) + 1]
        self.assertIn(f'href="/review/{self.ids[nxt]}" data-next', page)

    def test_todo_rows_open_the_first_and_group_stacks_in_order(self):
        self.add(50, "APPROVE", "Second part [2/2] (ABC-12)")
        self.add(51, "DEFER", "First part [1/2] (ABC-12)", reason="thin coverage: unsure")
        _, page = self.request("GET", "/")
        first = re.search(r'<details class="titem[^>]*>', page).group(0)
        self.assertTrue(first.endswith(" open>"), first)
        self.assertEqual(len(re.findall(r'<details class="titem(?! tstack)[^>]*\bopen>', page)), 1)
        stack = page[page.index('id="stack-ABC-12"') :]
        self.assertLess(stack.index('id="card-51"'), stack.index('id="card-50"'))
        self.assertIn('<span class="trow-part">1/2</span>', stack)
        order = [c["pr"] for c in cards.ordered(cards.todo(self.store, self.cfg))]
        self.assertEqual(order[order.index(51) + 1], 50)

    def test_rows_name_slack_channels_but_not_plain_requests(self):
        self.store.update("reviews", self.ids[44], source="both")
        self.store.observe(
            self.run_id, 44, "eligible", "x", pr={"head": SHA}, source="both", channels=["commerce"]
        )
        _, page = self.request("GET", "/")
        page = page[: page.index('id="pane"')]
        self.assertIn('<span class="source-chip">requested + #commerce</span>', page)
        self.assertEqual(page.count('class="source-chip"'), 1)

    def test_todo_state_lists_current_cards(self):
        _resp, text = self.request("GET", "/todo-state.json")
        state = json.loads(text)
        self.assertEqual(state["count"], 5)
        self.assertIn(f"42:{self.ids[42]}", state["keys"])

    def test_dont_post_asks_why_and_records_the_answer(self):
        _, text = self.act(42, "dont_post")
        self.assertIn("Why not post it?", text)
        cookie, token = self.session()
        _resp, text = self.request(
            "POST",
            f"/review/{self.ids[42]}/reason",
            cookie=cookie,
            htmx=True,
            body={"csrf": token, "reason": "not_worth_it", "variant": "card"},
        )
        self.assertIn("Noted: right, but not worth a comment", text)
        grade = self.store.query("SELECT * FROM grades WHERE review_id = ?", self.ids[42])[0]
        self.assertEqual(
            (grade["grade"], grade["tag"], grade["source"]),
            ("disagree", "wrong severity", "manual"),
        )
        self.run_due()
        calibrate.infer_from_actions(self.store)
        self.assertEqual(
            self.store.query("SELECT source FROM grades WHERE review_id = ?", self.ids[42])[0][0],
            "manual",
        )

    def test_activity_groups_by_day_and_marks_the_last_visit(self):
        old = "2025-12-29T09:00:00-08:00"
        self.store.update("reviews", self.ids[42], started_at=old)
        self.store.set_meta("activity_seen_id", str(self.ids[42]))
        _, page = self.request("GET", "/activity")
        self.assertIn("New since your last visit", page)
        self.assertLess(
            page.index("New since your last visit"),
            page.index(f'data-href="/review/{self.ids[42]}"'),
        )
        self.assertIn('<li class="list-day">Today</li>', page)
        self.assertIn('<li class="list-day">Dec 29, 2025</li>', page)
        self.assertNotIn("Waiting on you</span>", page)
        _, again = self.request("GET", "/activity")
        self.assertNotIn("New since your last visit", again)

    def test_activity_live_refresh_keeps_the_first_loads_divider(self):
        self.store.set_meta("activity_seen_id", str(self.ids[42]))
        _, page = self.request("GET", "/activity?q=Clean")
        self.assertIn(f'hx-get="/activity?show=all&q=Clean&page=1&seen={self.ids[42]}"', page)
        _, page = self.request("GET", "/activity")
        self.assertIn(f'&seen={self.ids[42]}"', page)
        self.assertIn('hx-select="#feed"', page)
        _, refreshed = self.request("GET", f"/activity?show=all&q=&page=1&seen={self.ids[42]}")
        self.assertIn("New since your last visit", refreshed)
        _, plain = self.request("GET", "/activity")
        self.assertNotIn("New since your last visit", plain)

    def test_reasoning_never_repeats_what_the_card_already_says(self):
        said = "Only renames a helper nothing else calls."
        self.add(70, "APPROVE", "Rename", reason=said, raw={"summary": said})
        self.add(
            71,
            "DEFER",
            "New table",
            reason="clean, but db/schema.rb is owned by finance",
            raw={"summary": "Only adds a table that nothing reads yet."},
        )
        for number, text in ((70, said), (71, "Only adds a table that nothing reads yet.")):
            _, page = self.request("GET", f"/review/{self.ids[number]}")
            start = page.index("<h2>Reasoning</h2>")
            reasoning = page[start : page.index("</details>", start)]
            self.assertIn(text, page[:start])
            self.assertNotIn(text, reasoning)
            self.assertIn("It didn't find anything worth a comment.", reasoning)

    def test_review_page_lists_what_it_checked(self):
        self.store.update(
            "reviews",
            self.ids[44],
            raw_review={"summary": "s", "checked": ["traced `foo` callers"]},
        )
        _, page = self.request("GET", f"/review/{self.ids[44]}")
        self.assertIn("What it checked", page)
        self.assertIn("traced <code>foo</code> callers", page)

    def test_allowing_a_flagged_comment_records_a_false_alarm_and_unblocks_posting(self):
        _, page = self.request("GET", "/")
        self.assertIn("It&#39;s right, allow posting", page)
        draft_id = self.store.query("SELECT id FROM drafts WHERE pr = 43")[0][0]
        _, text = self.act(43, "allow", draft_id=draft_id)
        self.assertIn("It can be posted as written", text)
        self.assertNotIn("Check this before posting", text)
        row = self.store.query("SELECT * FROM actions WHERE action = 'factcheck_false_alarm'")[0]
        self.assertEqual((row["pr"], json.loads(row["payload"])["unsupported"]), (43, ["a claim"]))
        self.act(43, "post")
        self.run_due()
        self.assertEqual((self.posted[-1]["n"], self.posted[-1]["event"]), (43, "REQUEST_CHANGES"))

    def test_ready_cards_show_files_and_an_excerpt(self):
        _, page = self.request("GET", "/")
        card = page[
            page.index('id="card-44"') : page.index('id="card-46"')
            if 'id="card-46"' in page
            else None
        ]
        self.assertIn('<span class="fp-base">x.rb</span>', card)
        self.assertIn('<span class="add-text">+2</span>', card)
        self.assertIn('class="excerpt"', card)

    def test_pages_are_gzipped_when_the_client_accepts_it(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/", headers={"Tailscale-User-Login": OWNER, "Accept-Encoding": "gzip"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        self.assertEqual(
            (resp.getheader("Content-Encoding"), resp.getheader("Vary")),
            ("gzip", "Accept-Encoding"),
        )
        self.assertIn(b"(5) To do", gzip.decompress(raw))
        self.assertIsNone(self.request("GET", "/")[0].getheader("Content-Encoding"))

    def test_undo_clears_a_reason_given_during_the_undo_window(self):
        _, text = self.act(42, "dont_post")
        pending_id = re.search(r"/pending/(\d+)/undo", text).group(1)
        cookie, token = self.session()
        self.request(
            "POST",
            f"/review/{self.ids[42]}/reason",
            cookie=cookie,
            htmx=True,
            body={"csrf": token, "reason": "wrong", "variant": "card"},
        )
        self.assertEqual(
            len(self.store.query("SELECT * FROM grades WHERE review_id = ?", self.ids[42])), 1
        )
        self.request(
            "POST",
            f"/pending/{pending_id}/undo",
            cookie=cookie,
            htmx=True,
            body={"csrf": token, "variant": "card"},
        )
        self.assertEqual(
            self.store.query("SELECT * FROM grades WHERE review_id = ?", self.ids[42]), []
        )

    def test_unknown_reason_is_rejected(self):
        cookie, token = self.session()
        resp, _ = self.request(
            "POST",
            f"/review/{self.ids[42]}/reason",
            cookie=cookie,
            htmx=True,
            body={"csrf": token, "reason": "because", "variant": "card"},
        )
        self.assertEqual(resp.status, 400)

    def test_actions_from_a_stale_page_or_the_wrong_state_are_refused(self):
        _, text = self.act(44, "approve", review_id=999)
        self.assertIn("reviewed again since you loaded the page", text)
        _, text = self.act(44, "post")
        self.assertIn("doesn&#39;t fit this PR anymore", text)
        self.assertEqual(self.store.query("SELECT COUNT(*) FROM pending_actions")[0][0], 0)

    def test_allow_refuses_a_draft_from_another_review(self):
        other = self.store.query("SELECT id FROM drafts WHERE pr = 43")[0][0]
        _, text = self.act(42, "allow", draft_id=other)
        self.assertIn("belongs to an older review", text)
        self.assertEqual(
            self.store.query("SELECT needs_edit FROM drafts WHERE id = ?", other)[0][0], 1
        )

    def test_drafting_failures_ask_for_an_edit_and_are_not_fact_check_flags(self):
        from plus1.drafts import DRAFT_FAILED

        self.add(47, "DRAFT", "Writer failed", drafts=[("P1", 1)])
        self.store.db.execute(
            "UPDATE drafts SET unsupported = ? WHERE pr = 47", (json.dumps([DRAFT_FAILED]),)
        )
        _, page = self.request("GET", "/")
        card = page[page.index('id="card-47"') :]
        self.assertIn("The comment writer failed", card[: card.index('class="buttons"')])
        self.assertNotIn("/card/47/allow", page)
        _, text = self.act(
            47, "allow", draft_id=self.store.query("SELECT id FROM drafts WHERE pr = 47")[0][0]
        )
        self.assertIn("comment writer failed", text)
        self.assertEqual(queries.factcheck_stats(self.store)["flagged"], 1)

    def test_stack_parts_keep_their_label_after_a_swap(self):
        self.add(51, "DEFER", "First part [1/2] (ABC-12)", reason="thin coverage: unsure")
        self.add(50, "APPROVE", "Second part [2/2] (ABC-12)")
        _, page = self.request("GET", "/")
        self.assertIn('<input type="hidden" name="variant" value="part">', page)
        _, part = self.request("GET", "/partials/card/51?variant=part", htmx=True)
        self.assertIn('<span class="trow-part">1/2</span>', part)

    def test_cards_lead_with_the_decision_and_say_what_the_pr_does(self):
        self.store.update(
            "reviews",
            self.ids[42],
            raw_review={"about": "Moves listings into their own collection."},
            ticket="SHOP-1079",
        )
        _, page = self.request("GET", "/")
        card = page[
            page.index('id="card-42"') : page.index('class="buttons"', page.index('id="card-42"'))
        ]
        self.assertLess(
            card.index("Approve with 1 comment?"),
            card.index("Moves listings into their own collection."),
        )
        self.assertIn('href="https://linear.app/globex/issue/SHOP-1079"', card)
        self.assertNotIn("1 lines", card)

    def test_the_decision_button_waits_for_a_flagged_comment_too(self):
        _, page = self.request("GET", "/")
        card = page[page.index('id="card-43"') :]
        top = card[
            card.index('class="decision"') : card.index("</div>", card.index('class="decision"'))
        ]
        self.assertIn('action="/card/43/post"', top)
        self.assertIn("disabled", top)

    def test_each_card_offers_its_main_action_once_beside_the_decision(self):
        _, page = self.request("GET", "/")
        page = page[: page.index('id="pane"')]
        for number, action in (
            (42, "approve_with_comment"),
            (43, "post"),
            (44, "approve"),
            (45, "approve"),
        ):
            self.assertEqual(page.count(f'action="/card/{number}/{action}"'), 1, number)
            card = page[page.index(f'id="card-{number}"') :]
            decision = card[
                card.index('class="decision"') : card.index(
                    "</div>", card.index('class="decision"')
                )
            ]
            self.assertIn(f'action="/card/{number}/{action}"', decision)
        card = page[page.index('id="card-45"') :]
        end = card.find('<details class="titem', 1)
        self.assertEqual(card[: end if end != -1 else None].count("Open on GitHub"), 1)

    def test_rows_lead_their_second_line_with_the_creator_and_say_why(self):
        self.store.update(
            "reviews",
            self.ids[45],
            author="devin-ai-integration",
            requested_by="nick",
            labels=["agent/devin"],
        )
        self.store.update(
            "reviews",
            self.ids[44],
            raw_review={"about": "Part 1 of the ABC-1 stack. The PR drops a stale entry."},
        )
        self.add(47, "DRAFT", "Two comments", drafts=[("P1", 0), ("P0", 0)])
        _, page = self.request("GET", "/")

        def row(n):
            at = page.index(f'id="card-{n}"')
            return page[at : page.index("</summary>", at)]

        self.assertIn('<span class="creator-name">nick</span><span class="creator-via">', row(45))
        self.assertIn("via Devin", row(45))
        self.assertNotIn("devin-ai-integration", row(45))
        self.assertIn("Clean, but db-reviewers owns 1_x.rb", row(45))
        self.assertIn('<span class="creator-name">teammate</span>', row(42))
        self.assertIn("P1 finding title", row(42))
        self.assertIn("Drops a stale entry.", row(44))
        self.assertIn("P0 finding title", row(47))
        self.assertIn('P0 finding title</span><span class="trow-more">+1 more</span>', row(47))

    def test_a_stack_row_names_two_creators_and_counts_the_rest(self):
        for n, author in ((60, "ann"), (61, "bob"), (62, "cy")):
            self.add(n, "APPROVE", f"Part [{n - 59}/3] (ABC-7)")
            self.store.update("reviews", self.ids[n], author=author)
        _, page = self.request("GET", "/")
        stack = page[page.index('id="stack-ABC-7"') :]
        stack = stack[: stack.index("</summary>")]
        self.assertEqual(stack.count('class="creator-name"'), 2)
        self.assertIn('<span class="trow-more" title="cy">+1 more</span>', stack)

    def test_todo_shows_the_first_waiting_card_beside_the_list(self):
        self.act(44, "approve")
        _, page = self.request("GET", "/")
        self.assertIn('class="content has-inbox is-home"', page)
        pane = page[page.index('id="pane"') :]
        self.assertIn(f'data-review="{self.ids[43]}"', pane)
        self.assertIn('id="pane-43"', pane)
        self.assertIn('hx-target="#pane-43"', pane)
        self.assertNotIn('id="card-43"', pane)
        self.assertEqual(page.count('id="pane-43"'), 1)

    def test_todo_keeps_both_panes_while_every_card_waits_on_undo(self):
        for card in cards.todo(self.store, self.cfg):
            executor.schedule(self.store, "approve", card)
        _, page = self.request("GET", "/")
        self.assertIn('class="content has-inbox is-home"', page)
        self.assertIn('class="inbox-pane"', page)
        self.assertIn("Pick a PR to see its review.", page[page.index('id="pane"') :])
        self.assertNotIn("You're all caught up", page)

    def test_ready_to_approve_leads_but_a_stack_files_under_its_most_urgent_part(self):
        self.add(50, "APPROVE", "Second part [2/2] (ABC-12)")
        self.add(51, "DRAFT", "First part [1/2] (ABC-12)", drafts=[("P0", 0)])
        entries = cards.entries(cards.todo(self.store, self.cfg))
        self.assertEqual(entries[0]["group"], "looks_good")
        stack = next(e for e in entries if e["kind"] == "stack")
        self.assertEqual(stack["group"], "blocking")

    def test_a_teammates_pr_leads_its_group_with_a_chip(self):
        self.add(60, "APPROVE", "From my team")
        self.store.update("reviews", self.ids[60], author="Bo-on-my-team")
        self.add(61, "APPROVE", "From a team I watch")
        self.store.update("reviews", self.ids[61], author="cy")
        self.cfg.team.people, self.cfg.team.others = ["bo-on-my-team"], ["acme/data"]
        self.store.set_meta("team_members:acme/data", json.dumps({"members": ["cy"], "at": now()}))
        ready = [
            e for e in cards.entries(cards.todo(self.store, self.cfg)) if e["group"] == "looks_good"
        ]
        self.assertEqual([e["card"]["pr"] for e in ready[:2]], [60, 61])
        _, page = self.request("GET", "/")
        row = page[page.index('id="card-60"') : page.index('id="card-61"')]
        self.assertIn('<span class="yours-chip">team</span>', row)
        self.assertIn('<span class="yours-chip">watching</span>', page)

    def test_a_ready_pr_with_new_commits_files_under_new_commits_without_approve(self):
        _, page = self.request("GET", "/")
        page = page[: page.index('id="pane"')]
        ready = page[
            page.index('aria-label="Ready to approve"') : page.index('aria-label="New commits"')
        ]
        self.assertIn('id="card-44"', ready)
        self.assertNotIn('id="card-46"', ready)
        moved = page[page.index('aria-label="New commits"') :]
        moved = moved[moved.index('id="card-46"') :]
        self.assertIn("New commits since this review", moved)
        self.assertIn("/card/46/review_now", moved)
        self.assertNotIn("/card/46/approve", moved)
        self.assertNotIn('title="Looks good, would approve"', moved)
        self.assertIn('title="New commits"', moved)
        entries = cards.entries(cards.todo(self.store, self.cfg))
        self.assertEqual(entries[-1]["group"], "stale")
        self.assertEqual(entries[-1]["tone"], "muted")

    def test_a_stale_stack_part_says_new_commits_in_its_dot(self):
        self.add(50, "APPROVE", "Second part [2/2] (ABC-12)")
        self.add(51, "APPROVE", "First part [1/2] (ABC-12)")
        self.store.observe(
            self.run_id,
            50,
            "eligible",
            "x",
            pr={"head": "c" * 40, "title": "Second part [2/2] (ABC-12)"},
        )
        _, page = self.request("GET", "/")
        stack = page[page.index('id="stack-ABC-12"') :]
        stack = stack[: stack.index("</summary>")]
        self.assertIn('title="2/2: New commits"', stack)
        self.assertIn('title="1/2: Looks good, would approve"', stack)

    def test_a_review_on_the_list_opens_in_the_inbox_and_others_stand_alone(self):
        _, page = self.request("GET", f"/review/{self.ids[45]}")
        self.assertIn('class="content has-inbox is-review"', page)
        self.assertIn('id="card-45"', page[: page.index('id="pane"')])
        self.assertIn(f'data-review="{self.ids[45]}"', page[page.index('id="pane"') :])
        older = self.ids[45]
        self.add(
            45,
            "DEFER",
            "Migration",
            reason="clean, but db/migrate/1_x.rb is owned by db-reviewers",
            raw={"verdict": "APPROVE", "coverage": "thorough", "findings": []},
        )
        _, page = self.request("GET", f"/review/{older}")
        self.assertNotIn('class="inbox"', page)
        self.assertIn('class="ph-title crumbs"', page)
        self.assertIn("This is an older review", page)

    def test_the_pane_partial_renders_one_review(self):
        resp, pane = self.request("GET", f"/partials/pane/{self.ids[44]}", htmx=True)
        self.assertEqual(resp.status, 200)
        self.assertTrue(pane.lstrip().startswith('<article class="pane"'), pane[:80])
        self.assertIn('id="pane-44"', pane)
        self.assertIn('data-card="pane-44"', pane)
        self.assertIn("About this review", pane)
        self.assertEqual(self.request("GET", "/partials/pane/99999", htmx=True)[0].status, 404)

    def test_acting_from_the_pane_swaps_the_pane_card(self):
        _, text = self.act(44, "approve", variant="page")
        self.assertIn('id="pane-44"', text)
        self.assertIn('hx-target="#pane-44"', text)
        self.assertNotIn('id="card-44"', text)

    def test_each_comment_carries_the_evidence_behind_it(self):
        _, page = self.request("GET", "/")
        draft = page[page.index('id="card-42"') :]
        draft = draft[draft.index('class="draft') : draft.index('class="buttons"')]
        why = draft[draft.index('<details class="why">') :]
        for text in ("double-checked", "saw it", "it hurts", "confirmed"):
            self.assertIn(text, why)
        _, review = self.request("GET", f"/review/{self.ids[42]}")
        self.assertIn("Each comment above carries the evidence behind it.", review)
        self.assertNotIn('<article class="finding">', review)

    def test_a_pr_that_needs_a_look_leads_with_its_sensitive_files(self):
        diff = (
            DIFF
            + DIFF.replace("app/x.rb", "db/migrate/1_x.rb")
            + DIFF.replace("app/x.rb", "lib/other.rb")
        )
        self.store.update(
            "reviews",
            self.ids[45],
            lane="never_approve",
            diff=diff,
            lane_reason="db/migrate/1_x.rb is owned by db-reviewers",
            files=["app/x.rb", "db/migrate/1_x.rb", "lib/other.rb"],
            p2_notes=["first note", "second note", "third note", "fourth note"],
        )
        (Path(self.tmp.name) / ".github").mkdir()
        (Path(self.tmp.name) / ".github" / "CODEOWNERS").write_text("app/x.rb @globex/finance\n")
        self.cfg.checkout = self.tmp.name
        _, review = self.request("GET", f"/review/{self.ids[45]}")
        pane = review[review.index('id="pane"') :]
        self.assertIn("Look it over, then approve?", pane)
        self.assertIn(
            'action="/card/45/approve"',
            pane[
                pane.index('class="decision"') : pane.index(
                    "</div>", pane.index('class="decision"')
                )
            ],
        )
        notes = pane[pane.index('class="quiet-notes"') :]
        self.assertIn("fourth note", notes[: notes.index("</section>")])
        code = pane[pane.index("<h2>The code</h2>") - 200 :]
        self.assertIn("open>", code[: code.index("<h2>The code</h2>") + 20])
        lead = code[: code.index('class="full-diff"')]
        self.assertIn("owned by db-reviewers", lead)
        self.assertIn("owned by finance", lead)
        self.assertNotIn("other.rb", lead)

    def test_a_pr_it_could_not_fully_review_keeps_github_first(self):
        self.store.update(
            "reviews",
            self.ids[45],
            reason="too large to trace (3000 changed lines)",
            lane="never_approve",
            lane_reason="db/migrate/1_x.rb is owned by db-reviewers",
        )
        _, page = self.request("GET", f"/review/{self.ids[45]}")
        pane = page[page.index('id="pane"') :]
        decision = pane[
            pane.index('class="decision"') : pane.index("</div>", pane.index('class="decision"'))
        ]
        self.assertIn("Check it on GitHub, then approve?", decision)
        self.assertIn(
            'class="button primary" href="https://github.com/globex/web/pull/45"', decision
        )
        self.assertNotIn('action="/card/45/approve"', decision)
        self.assertIn('action="/card/45/approve"', pane[pane.index('class="buttons"') :])
        self.assertNotIn('class="quiet-notes"', pane)

    def test_a_mapped_large_pr_leads_with_where_to_look(self):
        diff = DIFF + DIFF.replace("app/x.rb", "lib/other.rb")
        focus = [{"path": "lib/other.rb", "line": 2, "why": "Renames the value `four` reads."}]
        self.store.update(
            "reviews",
            self.ids[45],
            reason="too large to trace (3000 changed lines)",
            lane="substantive",
            lane_reason="",
            diff=diff,
            files=["app/x.rb", "lib/other.rb"],
            raw_review={
                "coverage": "thin",
                "findings": [],
                "summary": "The rename is the risk.",
                "focus": focus,
            },
        )
        _, page = self.request("GET", f"/review/{self.ids[45]}")
        pane = page[page.index('id="pane"') :]
        self.assertIn(
            "Too big to trace in full (3,000 changed lines), so it marked where to look first.",
            pane,
        )
        look = pane[pane.index("Where to look first") :]
        look = look[: look.index("</section>")]
        self.assertIn(f'href="/review/{self.ids[45]}#L-lib-other-rb-2"', look)
        self.assertIn("Renames the value <code>four</code> reads.", look)
        self.assertIn('class="button primary" href="https://github.com/globex/web/pull/45"', pane)
        code = pane[pane.index("<h2>The code</h2>") - 200 :]
        self.assertIn("open>", code[: code.index("<h2>The code</h2>") + 20])
        lead = code[: code.index('class="full-diff"')]
        self.assertIn("<strong>other.rb</strong>", lead)
        self.assertNotIn("<strong>x.rb</strong>", lead)
        self.assertIn('id="L-lib-other-rb-2"', lead)

    def test_files_the_tick_stored_as_sensitive_lead_the_code(self):
        diff = DIFF + DIFF.replace("app/x.rb", "lib/other.rb")
        self.store.update(
            "reviews",
            self.ids[45],
            lane="never_approve",
            diff=diff,
            sensitive={"lib/other.rb": "money"},
            lane_reason="lib/other.rb is owned by money",
            files=["app/x.rb", "lib/other.rb"],
        )
        self.cfg.checkout = self.tmp.name
        _, page = self.request("GET", f"/review/{self.ids[45]}")
        lead = page[page.index("<h2>The code</h2>") : page.index('class="full-diff"')]
        self.assertIn("owned by money", lead)
        self.assertIn("other.rb", lead)
        self.assertNotIn("<strong>x.rb</strong>", lead)

    def test_a_grading_queue_walks_past_decisions_approvals_then_drafts_then_adds_them_up(self):
        for n in (42, 43, 44, 45, 46):
            self.store.set_pr_status(n, "MERGED", "APPROVED", False)
        self.store.db.execute("UPDATE drafts SET status = 'released'")
        # The checklist is short of graded approvals, so they lead; then comment drafts; then what was left to you.
        order = [self.ids[n] for n in (46, 44, 43, 42, 45)]
        resp, _ = self.request("GET", "/grade")
        self.assertEqual(resp.status, 303)
        start = resp.getheader("Location")
        self.assertEqual(start, "/grade?q=" + urllib.parse.quote(",".join(map(str, order))))
        self.store.action("approve", "ui", pr=46, sha=SHA, ok=False, error="boom")
        _, page = self.request("GET", start)
        self.assertIn('<span class="grade-num">#46</span>', page)
        self.assertIn("<strong>1 of 5</strong> in this queue", page)
        self.assertEqual(page.count('class="gq-cell '), 5)
        self.assertIn("30 graded approvals to go", page)
        self.assertIn("Approved on GitHub (failed)", page)
        cookie, token = self.session()
        queue = ",".join(map(str, order))

        def answer(number, **fields):
            body = {"csrf": token, "q": queue, "skip": fields.pop("skip", ""), **fields}
            return self.request(
                "POST", f"/grade/{self.ids[number]}", cookie=cookie, htmx=True, body=body
            )

        def grade_of(number):
            rows = self.store.query(
                "SELECT grade, tag, source FROM grades WHERE review_id = ?", self.ids[number]
            )
            return tuple(rows[0]) if rows else None

        resp, card = answer(46, grade="agree")
        self.assertEqual(grade_of(46), ("agree", None, "manual"))
        self.assertIn('<span class="grade-num">#44</span>', card)
        self.assertIn("<strong>2 of 5</strong>", card)
        self.assertIn('class="gq-cell is-agree"', card)
        self.assertEqual(resp.getheader("HX-Replace-Url"), start)
        _, card = answer(44, grade="skip")
        self.assertIsNone(grade_of(44))
        self.assertIn('<span class="grade-num">#43</span>', card)
        self.assertIn(f'name="skip" value="{self.ids[44]}"', card)
        self.assertIn('class="gq-cell is-skipped"', card)
        skip = str(self.ids[44])
        answer(43, grade="agree", skip=skip)
        answer(42, grade="agree", skip=skip)
        _, done = answer(
            45, grade="disagree", tag="too cautious", note="fine to approve", skip=skip
        )
        self.assertEqual(grade_of(45), ("disagree", "too cautious", "manual"))
        self.assertEqual(
            self.store.query("SELECT note FROM grades WHERE review_id = ?", self.ids[45])[0][0],
            "fine to approve",
        )
        self.assertIn("<strong>Queue finished</strong>", done)
        tiles = re.findall(r'class="tile-num">(\d+)<', done)
        self.assertEqual(tiles, ["3", "1", "1"])
        self.assertIn("too cautious", done[done.index(">Wrong calls</h3>") :])
        self.assertRegex(
            done,
            r"Graded approvals</span><span class=\"checklist-value\">1 <span class=\"add-text\">\+1<",
        )
        self.assertIn("That was the last of them.", done)
        _, again = self.request("GET", f"/grade?skip={skip}")
        self.assertIn("Nothing left to grade", again)
        resp, _ = self.request("GET", "/grade")
        self.assertEqual(resp.getheader("Location"), f"/grade?q={skip}")

    def test_the_sidebar_counts_decisions_waiting_for_a_grade(self):
        _, page = self.request("GET", "/insights")
        self.assertIn('<a href="/grade" >', page)
        self.assertNotIn("sb-count is-quiet", page)
        for n in (42, 43, 44, 45, 46):
            self.store.set_pr_status(n, "MERGED", "APPROVED", False)
        self.store.db.execute("UPDATE drafts SET status = 'released'")
        for path in ("/insights", "/settings", f"/review/{self.ids[44]}"):
            _, page = self.request("GET", path)
            self.assertRegex(
                page, r'class="sb-count is-quiet" title="5 past decisions waiting for a grade">5<'
            )

    def test_grading_leaves_out_prs_still_on_todo_and_refuses_unknown_answers(self):
        older = self.ids[44]
        self.add(44, "APPROVE", "Clean change", reason="Still clean.")
        _, page = self.request("GET", "/grade")
        self.assertIn("Nothing left to grade", page)
        cookie, token = self.session()
        resp, _ = self.request(
            "POST",
            f"/grade/{self.ids[44]}",
            cookie=cookie,
            htmx=True,
            body={"csrf": token, "grade": "maybe"},
        )
        self.assertEqual(resp.status, 400)
        for rid in (older, self.ids[44]):
            _, card = self.request(
                "POST",
                f"/grade/{rid}",
                cookie=cookie,
                htmx=True,
                body={"csrf": token, "grade": "agree"},
            )
            self.assertIn("so it wasn&#39;t graded", card)
        self.assertEqual(self.store.query("SELECT COUNT(*) FROM grades")[0][0], 0)

    def test_an_empty_todo_nudges_toward_grading_until_the_gate_is_met(self):
        for n in (42, 43, 44, 45, 46):
            self.store.set_pr_status(n, "MERGED", "APPROVED", False)
        self.store.db.execute("UPDATE drafts SET status = 'released'")
        _, page = self.request("GET", "/")
        self.assertIn('class="content has-inbox is-home"', page)
        self.assertIn('class="inbox-pane is-empty"', page)
        self.assertIn("You're all caught up", page[page.index('id="pane"') :])
        self.assertIn(
            "Grade 30 more approvals to finish its checklist for approving on its own. 5 past decisions are waiting for one.",
            page,
        )
        self.assertIn('<a class="button primary" href="/grade">', page)
        self.assertIn("Start a grading queue", page)

    def test_activity_folds_earlier_reviews_under_the_latest_and_names_requesters(self):
        older = self.ids[44]
        self.add(44, "APPROVE", "Clean change", reason="Still clean.")
        self.store.update(
            "reviews",
            self.ids[45],
            author="devin-ai-integration",
            requested_by="nick",
            labels=["agent/devin"],
        )
        _, page = self.request("GET", "/activity")
        self.assertEqual(page.count('<span class="row-num">#44</span>'), 1)
        self.assertIn("1 earlier review of #44", page)
        self.assertIn(f'href="/review/{older}"', page[page.index("1 earlier review of #44") :])
        self.assertIn('<span class="creator-name">nick</span><span class="creator-via">', page)
        _, found = self.request("GET", "/activity?q=nick")
        self.assertIn('<span class="row-num">#45</span>', found)
        self.assertNotIn('<span class="row-num">#44</span>', found)
        self.store.update("reviews", older, reason="Only the older review says zebra.")
        _, found = self.request("GET", "/activity?q=zebra")
        self.assertEqual(found.count('<span class="row-num">#44</span>'), 1)
        self.assertIn("1 earlier review of #44", found)

    def test_insights_sends_ungraded_decisions_to_the_queue(self):
        _, page = self.request("GET", "/insights")
        self.assertNotIn('class="cta-row', page)
        for n in (42, 43, 44, 45, 46):
            self.store.set_pr_status(n, "MERGED", "APPROVED", False)
        _, page = self.request("GET", "/insights")
        self.assertIn('<a href="/grade">30+ graded approvals</a>', page)
        self.assertIn("30 to go", page)
        self.assertIn('<a class="cta-row block" href="/grade">', page)

    def test_an_oversized_stack_lists_no_parts(self):
        self.add(52, "APPROVE", "Huge [1/100000] (ABC-9)")
        self.assertEqual(cards.stack_parts(self.store, self.cfg, "ABC-9", 100000), [])

    def test_review_page_lists_the_stack_with_each_part_status(self):
        self.add(51, "DEFER", "First part [1/3] (ABC-12)", reason="thin coverage: unsure")
        self.add(50, "APPROVE", "Second part [2/3] (ABC-12)")
        self.store.set_pr_status(50, "MERGED", "APPROVED", False)
        _, page = self.request("GET", f"/review/{self.ids[51]}")
        side = page[page.index('class="stack-list"') :]
        self.assertIn("needs a look", side[: side.index("</li>")])
        self.assertIn("merged", side)
        self.assertIn("not seen yet", side)
        self.assertIn('href="https://linear.app/globex/issue/ABC-12"', page)

    def test_review_page_shows_what_to_do_once_the_comment_is_gone(self):
        self.store.db.execute("UPDATE drafts SET status = 'dismissed' WHERE pr = 42")
        _, page = self.request("GET", f"/review/{self.ids[42]}")
        self.assertIn("What to do", page)

    def test_a_pending_first_row_does_not_use_up_the_open_row(self):
        self.act(43, "dont_post")
        _, page = self.request("GET", "/")
        self.assertEqual(len(re.findall(r'<details class="titem(?! tstack)[^>]*\bopen>', page)), 1)

    def test_neighbors_at_the_edges(self):
        order = [c["pr"] for c in cards.ordered(cards.todo(self.store, self.cfg))]
        last = cards.neighbors(self.store, self.cfg, order[-1])
        self.assertEqual(
            (last["next"], last["position"], last["total"]), (None, len(order), len(order))
        )
        gone = cards.neighbors(self.store, self.cfg, 9999)
        self.assertEqual((gone["position"], gone["next"]["pr"]), (None, order[0]))

    def test_a_running_action_keeps_polling_until_it_lands(self):
        self.act(44, "approve")
        self.store.db.execute("UPDATE pending_actions SET status = 'running'")
        _, part = self.request("GET", "/partials/card/44?variant=card", htmx=True)
        self.assertIn('hx-trigger="every 1s"', part)
        self.assertNotIn("<details", part)
        self.assertNotIn("data-undo", part)
        self.store.db.execute("UPDATE pending_actions SET status = 'pending'")
        self.run_due()
        _, part = self.request("GET", "/partials/card/44?variant=card", htmx=True)
        self.assertIn("Approved on #44", part)

    def test_a_restart_fails_actions_left_running(self):
        self.act(44, "approve")
        self.store.db.execute("UPDATE pending_actions SET status = 'running'")
        executor.recover(self.store, self.cfg)
        row = self.store.query("SELECT status, error FROM pending_actions")[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("Check GitHub", row["error"])
        self.assertIsNone(cards.pending_for(self.store, 44))
        self.assertEqual(
            self.told,
            [
                (
                    "Approval may not have gone through on #44",
                    "The app restarted while this was running. Check GitHub before trying again.",
                    f"/review/{self.ids[44]}",
                )
            ],
        )

    def test_approving_a_pr_that_needs_a_look_takes_it_off_the_list(self):
        self.act(45, "approve")
        self.run_due()
        _, page = self.request("GET", "/")
        self.assertNotIn('id="card-45"', page)
        _, part = self.request("GET", "/partials/card/45?variant=card", htmx=True)
        self.assertIn("Approved on #45", part)

    def test_handing_off_hides_the_pr_until_brought_back(self):
        _, text = self.act(44, "hand_off")
        self.assertIn("Handing off", text)
        self.run_due()
        _, page = self.request("GET", "/")
        self.assertNotIn('id="card-44"', page)
        self.assertEqual(self.store.ignored()[44]["title"], "Clean change")
        _, listing = self.request("GET", "/activity?show=handed_off")
        self.assertIn('action="/pr/44/unignore"', listing)
        _, review = self.request("GET", f"/review/{self.ids[44]}")
        self.assertIn("You handed this PR off", review)
        _, waiting = self.request("GET", "/activity?show=waiting")
        self.assertNotIn(f'data-href="/review/{self.ids[44]}"', waiting)
        cookie, token = self.session()
        resp, _ = self.request(
            "POST", "/pr/44/unignore", cookie=cookie, body={"csrf": token, "next": "/"}
        )
        self.assertEqual(resp.status, 303)
        _, review = self.request("GET", f"/review/{self.ids[44]}")
        self.assertIn("You brought it back", review)
        _, page = self.request("GET", "/")
        self.assertIn('id="card-44"', page)

    def test_a_stale_pr_can_be_handed_off(self):
        _, text = self.act(46, "hand_off")
        self.assertIn("Handing off", text)

    def test_hand_off_sits_on_the_button_row_outside_the_menu(self):
        _, part = self.request("GET", "/partials/card/44?variant=card", htmx=True)
        row = part.split('<div class="buttons">', 1)[1]
        self.assertLess(
            row.index('action="/card/44/hand_off"'), row.index('<details class="more">')
        )
        self.assertEqual(part.count('action="/card/44/hand_off"'), 1)

    def test_a_stale_review_page_drops_the_menu_it_has_nothing_for(self):
        _, page = self.request("GET", "/partials/card/46?variant=page", htmx=True)
        self.assertIn('action="/card/46/hand_off"', page)
        self.assertNotIn('<details class="more">', page)
        _, row = self.request("GET", "/partials/card/46?variant=card", htmx=True)
        self.assertIn('<details class="more">', row)

    def test_checks_skip_handed_off_prs_before_fetching_them(self):
        from types import SimpleNamespace

        from plus1 import cli

        self.store.ignore(44, "Clean change")
        fetched, saved = [], (gh.run, gh.candidates, gh.fetch_pr)
        gh.run = lambda args, input_text=None, timeout=90: ""
        gh.candidates = lambda cfg: [44, 77]
        gh.fetch_pr = lambda cfg, n: fetched.append(n) or {"state": "CLOSED", "number": n}
        self.cfg.slack.enabled, self.cfg.checkout = False, self.tmp.name
        try:
            args = SimpleNamespace(
                pr=None, any_time=True, force=False, plan=True, trigger="manual", wait_lock=False
            )
            self.assertEqual(cli.tick(self.cfg, args), 0)
        finally:
            gh.run, gh.candidates, gh.fetch_pr = saved
        self.assertEqual(fetched, [77])

    def test_every_card_form_names_the_review_it_was_rendered_for(self):
        pages = [
            self.request("GET", "/")[1],
            self.request("GET", f"/review/{self.ids[42]}")[1],
            self.request("GET", "/partials/card/43?variant=card", htmx=True)[1],
        ]
        forms = [
            f
            for page in pages
            for f in re.findall(
                r'<form[^>]*action="/card/(\d+)/(\w+)"[^>]*>(.*?)</form>', page, re.S
            )
        ]
        self.assertGreater(len(forms), 10)
        for number, action, body in forms:
            self.assertIn(
                f'name="review_id" value="{self.ids[int(number)]}"', body, (number, action)
            )

    def test_a_review_still_writing_comments_is_not_actionable(self):
        rid = self.add(48, "DRAFT", "Still drafting", drafts=[("P1", 0)])
        self.store.update("reviews", rid, finished_at=None)
        self.assertNotIn(48, [c["pr"] for c in cards.todo(self.store, self.cfg)])

    def test_merged_closed_approved_and_queued_prs_leave_todo(self):
        self.store.set_pr_status(42, "MERGED", "APPROVED", False)
        self.store.set_pr_status(43, "OPEN", "REVIEW_REQUIRED", False)
        self.store.set_pr_status(44, "OPEN", "REVIEW_REQUIRED", True)
        self.store.set_pr_status(45, "CLOSED", "REVIEW_REQUIRED", False)
        self.store.set_pr_status(46, "OPEN", "APPROVED", False)
        self.assertEqual([c["pr"] for c in cards.todo(self.store, self.cfg)], [43])
        self.assertIsNone(cards.card_for_pr(self.store, self.cfg, 42))
        self.store.set_pr_status(46, "OPEN", "REVIEW_REQUIRED", False)
        self.assertIn(46, [c["pr"] for c in cards.todo(self.store, self.cfg)])

    def test_a_clean_review_keeps_its_card_until_an_approval_goes_out(self):
        self.cfg.repos[0].auto_approve = True
        self.assertEqual(cards.card_for_pr(self.store, self.cfg, 44)["state"], "looks_good")
        self.store.update("reviews", self.ids[44], held="daily cap")
        self.assertIn("held: daily cap", cards.card_for_pr(self.store, self.cfg, 44)["flags"])
        self.store.action("approve", "timer", pr=44, sha=SHA)
        self.assertIsNone(cards.card_for_pr(self.store, self.cfg, 44))

    def test_a_blocking_comment_overtaken_by_someone_elses_approval_leads_todo(self):
        self.store.set_pr_status(43, "OPEN", "APPROVED", False)
        card = cards.card_for_pr(self.store, self.cfg, 43)
        self.assertEqual(
            (card["group"], card["state"], card["headline"]),
            ("overtaken", "blocking", "Approved without your blocking comment"),
        )
        self.assertNotIn("approved by someone else", card["flags"])
        self.assertEqual(cards.todo(self.store, self.cfg)[0]["pr"], 43)
        self.store.set_pr_status(43, "OPEN", "APPROVED", True)
        self.assertEqual(
            cards.card_for_pr(self.store, self.cfg, 43)["headline"],
            "In the merge queue without your blocking comment",
        )
        self.store.set_pr_status(42, "OPEN", "APPROVED", False)
        self.assertIn(
            "approved by someone else", cards.card_for_pr(self.store, self.cfg, 42)["flags"]
        )

    def test_a_retired_action_saved_earlier_is_cancelled_not_done(self):
        self.store.insert(
            "pending_actions",
            kind="post_late",
            pr=42,
            review_id=self.ids[42],
            sha=SHA,
            payload={"title": "t"},
            created_at=now(),
            execute_at=time.time(),
            status="pending",
        )
        self.run_due()
        row = self.store.query(
            "SELECT status, error FROM pending_actions WHERE kind = 'post_late'"
        )[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(
            executor.describe({"kind": "post_late", "payload": "{}"})[0], ("Working on it", "Done")
        )

    def test_a_merged_pr_expires_its_comments_even_with_nothing_to_poll(self):
        for n in self.ids:
            self.store.set_pr_status(n, "MERGED", "APPROVED", False)
        saved = gh.pr_states
        gh.pr_states = lambda cfg, numbers: self.fail("nothing should need polling")
        try:
            self.assertEqual(cards.refresh_status(self.store, self.cfg), 0)
        finally:
            gh.pr_states = saved
        self.assertEqual({r[0] for r in self.store.query("SELECT status FROM drafts")}, {"expired"})

    def test_a_reopened_pr_keeps_comments_queued_after_it_reopened(self):
        self.store.set_pr_status(42, "CLOSED", "REVIEW_REQUIRED", False)
        saved = gh.pr_states
        gh.pr_states = lambda cfg, numbers: {
            n: {"state": "OPEN", "decision": "REVIEW_REQUIRED", "merge_queue": False}
            for n in numbers
        }
        try:
            cards.refresh_status(self.store, self.cfg)
        finally:
            gh.pr_states = saved
        self.assertEqual(
            self.store.query("SELECT status FROM drafts WHERE pr = 42")[0][0], "queued"
        )

    def test_comments_a_pr_merged_without_leave_todo_and_show_in_activity(self):
        self.store.set_pr_status(43, "MERGED", "APPROVED", False)
        self.assertIsNone(cards.card_for_pr(self.store, self.cfg, 43))
        self.store.expire_unposted()
        self.assertEqual(
            self.store.query("SELECT status FROM drafts WHERE pr = 43")[0][0], "expired"
        )
        self.assertEqual(
            self.store.query("SELECT status FROM drafts WHERE pr = 42")[0][0], "queued"
        )
        _, page = self.request("GET", "/activity")
        self.assertIn("Merged before you posted", page)
        calibrate.infer_from_actions(self.store)
        self.assertEqual(
            self.store.query("SELECT COUNT(*) FROM grades WHERE review_id = ?", self.ids[43])[0][0],
            0,
        )

    def test_status_refresh_warns_once_when_a_pr_moves_on_without_your_comments(self):
        self.add(47, "DRAFT", "Queued one", drafts=[("P1", 0), ("P1", 0)])
        self.add(48, "DRAFT", "Closed one", drafts=[("P0", 0)])
        now_state: dict[int, tuple[str, str, bool]] = dict.fromkeys(
            range(42, 49), ("OPEN", "REVIEW_REQUIRED", False)
        )

        def states(cfg, numbers):
            return {
                n: dict(zip(("state", "decision", "merge_queue"), now_state[n], strict=True))
                for n in numbers
            }

        saved, gh.pr_states = gh.pr_states, states
        try:
            cards.refresh_status(self.store, self.cfg)
            self.assertEqual(self.told, [])
            now_state.update(
                {
                    42: ("MERGED", "APPROVED", False),
                    43: ("OPEN", "APPROVED", False),
                    44: ("OPEN", "APPROVED", False),
                    47: ("OPEN", "APPROVED", True),
                    48: ("CLOSED", "REVIEW_REQUIRED", False),
                }
            )
            cards.refresh_status(self.store, self.cfg)
            cards.refresh_status(self.store, self.cfg)
        finally:
            gh.pr_states = saved
        self.assertEqual(
            self.told,
            [
                (
                    "Comments not posted",
                    "#42 merged before your comment went out.",
                    f"/review/{self.ids[42]}",
                ),
                (
                    "Comments not posted",
                    "#43 was approved by someone else before your blocking comment went out.",
                    f"/review/{self.ids[43]}",
                ),
                (
                    "Comments not posted",
                    "#47 entered the merge queue before your 2 comments went out.",
                    f"/review/{self.ids[47]}",
                ),
                (
                    "Comments not posted",
                    "#48 was closed before your blocking comment went out.",
                    f"/review/{self.ids[48]}",
                ),
            ],
        )

    def test_an_old_review_with_unposted_comments_stays_tracked(self):
        month_ago = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
        for number in (43, 44):
            self.store.update("reviews", self.ids[number], started_at=month_ago)
        asked = []

        def states(cfg, numbers):
            asked.extend(numbers)
            return {}

        saved, gh.pr_states = gh.pr_states, states
        try:
            cards.refresh_status(self.store, self.cfg)
        finally:
            gh.pr_states = saved
        self.assertIn(43, asked)
        self.assertNotIn(44, asked)
        todo = [c["pr"] for c in cards.todo(self.store, self.cfg)]
        self.assertIn(43, todo)
        self.assertNotIn(44, todo)
        self.store.set_pr_status(43, "CLOSED", "REVIEW_REQUIRED", False)
        asked.clear()
        saved, gh.pr_states = gh.pr_states, states
        try:
            cards.refresh_status(self.store, self.cfg)
        finally:
            gh.pr_states = saved
        self.assertNotIn(43, asked)
        # Once the PR has closed, its comments expire rather than wait on To do.
        self.assertEqual(
            self.store.query("SELECT status FROM drafts WHERE pr = 43")[0][0], "expired"
        )
        self.assertNotIn(43, [c["pr"] for c in cards.todo(self.store, self.cfg)])

    def test_status_refresh_stops_asking_about_merged_prs(self):
        asked = []

        def states(cfg, numbers):
            asked.append(sorted(numbers))
            return {
                n: {
                    "state": "MERGED" if n == 42 else "OPEN",
                    "decision": "REVIEW_REQUIRED",
                    "merge_queue": False,
                }
                for n in numbers
            }

        saved, gh.pr_states = gh.pr_states, states
        try:
            self.assertEqual(cards.refresh_status(self.store, self.cfg), 5)
            cards.refresh_status(self.store, self.cfg)
        finally:
            gh.pr_states = saved
        self.assertEqual(asked, [[42, 43, 44, 45, 46], [43, 44, 45, 46]])
        self.assertNotIn(42, [c["pr"] for c in cards.todo(self.store, self.cfg)])

    def test_stale_drafts_offer_no_edits_and_can_be_marked_handled(self):
        self.add(48, "DRAFT", "Stale draft", drafts=[("P1", 1)])
        self.store.observe(
            self.run_id, 48, "eligible", "x", pr={"head": "b" * 40, "title": "Stale draft"}
        )
        _, page = self.request("GET", "/")
        self.assertNotIn("/card/48/save_comment", page)
        self.assertNotIn("/card/48/allow", page)
        self.assertNotIn("/card/48/rewrite", page)
        draft_id = self.store.query("SELECT id FROM drafts WHERE pr = 48")[0][0]
        _, text = self.act(48, "rewrite", draft_id=draft_id, suggestion="Make it shorter.")
        self.assertIn("doesn&#39;t fit this PR anymore", text)
        self.act(48, "handled")
        self.run_due()
        self.assertNotIn(48, [c["pr"] for c in cards.todo(self.store, self.cfg)])

    def test_the_writer_failed_warning_clears_once_edited(self):
        from plus1.drafts import DRAFT_FAILED

        self.add(47, "DRAFT", "Writer failed", drafts=[("P1", 1)])
        self.store.db.execute(
            "UPDATE drafts SET unsupported = ? WHERE pr = 47", (json.dumps([DRAFT_FAILED]),)
        )
        draft_id = self.store.query("SELECT id FROM drafts WHERE pr = 47")[0][0]
        _, text = self.act(47, "save_comment", draft_id=draft_id, body="issue: rewritten by hand")
        self.assertIn("Comment saved.", text)
        self.assertNotIn("comment writer failed", text)

    def handoff_tick(self, days_ago, reviews=()):
        from types import SimpleNamespace

        from plus1 import cli

        self.store.ignore(44, "Clean change")
        at = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")
        self.store.db.execute("UPDATE ignored SET at = ? WHERE pr = 44", (at,))
        pr = {
            "number": 44,
            "state": "OPEN",
            "draft": True,
            "decision": "REVIEW_REQUIRED",
            "reviews": list(reviews),
            "head": SHA,
            "base": "main",
            "title": "Clean change",
            "author": "teammate",
            "merge_queue": False,
            "labels": [],
            "mergeable": "MERGEABLE",
            "threads": [],
            "conversation": [],
            "checks": [],
            "files": ["app/x.rb"],
            "additions": 2,
            "deletions": 1,
            "files_truncated": False,
            "ready_at": datetime.now().astimezone(),
            "url": "https://github.com/globex/web/pull/44",
        }
        fetched, saved = [], (gh.run, gh.candidates, gh.fetch_pr, gh.followups)
        gh.run = lambda args, input_text=None, timeout=90: ""
        gh.candidates = lambda cfg: [44]
        gh.followups = lambda cfg: []
        gh.fetch_pr = lambda cfg, n: fetched.append(n) or pr
        self.cfg.slack.enabled, self.cfg.checkout = False, self.tmp.name
        try:
            args = SimpleNamespace(
                pr=None, any_time=True, force=False, plan=False, trigger="timer", wait_lock=False
            )
            cli.tick(self.cfg, args)
        finally:
            gh.run, gh.candidates, gh.fetch_pr, gh.followups = saved
        return fetched

    def test_a_hand_off_nobody_picked_up_comes_back_after_three_working_days(self):
        self.assertEqual(self.handoff_tick(days_ago=8), [44])
        self.assertFalse(self.store.is_ignored(44))
        self.assertEqual(
            self.store.query("SELECT actor FROM actions WHERE action = 'resurface'")[0][0], "timer"
        )
        self.assertIn("nobody picked this up", cards.card_for_pr(self.store, self.cfg, 44)["flags"])

    def test_a_recent_or_picked_up_hand_off_stays_handed_off(self):
        self.assertEqual(self.handoff_tick(days_ago=0), [])
        self.assertTrue(self.store.is_ignored(44))
        kayla = {
            "author": "kayla",
            "bot": False,
            "state": "COMMENTED",
            "body": "",
            "oid": SHA,
            "at": datetime.now().astimezone(),
        }
        self.assertEqual(self.handoff_tick(days_ago=8, reviews=[kayla]), [44])
        self.assertTrue(self.store.is_ignored(44))

    def eligible_tick(self, states=None, spend=0.0):
        from types import SimpleNamespace

        from plus1 import cli, review

        pr = {
            "number": 77,
            "state": "OPEN",
            "draft": False,
            "decision": "REVIEW_REQUIRED",
            "reviews": [],
            "head": SHA,
            "base": "main",
            "title": "Eligible",
            "author": "teammate",
            "merge_queue": False,
            "labels": [],
            "mergeable": "MERGEABLE",
            "threads": [],
            "conversation": [],
            "checks": [],
            "files": ["app/x.rb"],
            "additions": 2,
            "deletions": 1,
            "files_truncated": False,
            "url": "https://github.com/globex/web/pull/77",
            "ready_at": datetime.now().astimezone() - timedelta(days=1),
        }
        if spend:
            self.store.insert(
                "stages",
                review_id=self.ids[42],
                stage="deep",
                idx=0,
                started_at=now(),
                cost_usd=spend,
                ok=1,
            )
        reviewed = []
        saved = (gh.run, gh.candidates, gh.fetch_pr, gh.followups, gh.pr_states, review.run)
        gh.run = lambda args, input_text=None, timeout=90: ""
        gh.candidates = lambda cfg: [77]
        gh.followups = lambda cfg: []
        gh.fetch_pr = lambda cfg, n: pr
        gh.pr_states = states or (
            lambda cfg, numbers: {
                n: {"state": "OPEN", "decision": "REVIEW_REQUIRED", "merge_queue": False}
                for n in numbers
            }
        )
        review.run = lambda *a, **k: (
            reviewed.append(a) or (_ for _ in ()).throw(RuntimeError("stop after starting"))
        )
        self.cfg.slack.enabled, self.cfg.checkout = False, self.tmp.name
        try:
            cli.tick(
                self.cfg,
                SimpleNamespace(
                    pr=None,
                    any_time=True,
                    force=False,
                    plan=False,
                    trigger="timer",
                    wait_lock=False,
                ),
            )
        finally:
            gh.run, gh.candidates, gh.fetch_pr, gh.followups, gh.pr_states, review.run = saved
        last = self.store.query(
            "SELECT outcome, reason FROM observations WHERE pr = 77 ORDER BY id DESC LIMIT 1"
        )[0]
        return len(reviewed), (last["outcome"], last["reason"])

    def test_a_spent_day_stops_new_reviews(self):
        self.assertEqual(
            self.eligible_tick(spend=40.0), (0, ("waiting", "daily spend cap reached"))
        )
        _, status = self.request("GET", "/partials/status")
        self.assertIn("cap of $40.00 reached", status)

    def test_a_pr_approved_while_the_check_ran_isnt_reviewed(self):
        def approved(cfg, numbers):
            return {77: {"state": "OPEN", "decision": "APPROVED", "merge_queue": False}}

        self.assertEqual(
            self.eligible_tick(states=approved),
            (0, ("skip", "approved or merged since this check started")),
        )
        self.assertEqual(self.eligible_tick()[0], 1)

    def test_the_sidebar_says_when_slack_went_quiet(self):
        _, status = self.request("GET", "/partials/status")
        self.assertNotIn("Slack off", status)
        ok = self.store.start_run("timer", "shadow")
        self.store.update("runs", ok, slack_ok=1)
        down = self.store.start_run("timer", "shadow")
        self.store.update("runs", down, slack_ok=0, started_at="2026-10-15T09:30:00-07:00")
        later = self.store.start_run("timer", "shadow")
        self.store.update("runs", later, slack_ok=0)
        _, status = self.request("GET", "/partials/status")
        self.assertIn("Slack off since", status)
        self.store.update("runs", later, slack_ok=1)
        self.assertNotIn("Slack off", self.request("GET", "/partials/status")[1])

    def test_a_pr_passed_over_for_budget_shows_as_waiting(self):
        self.store.observe(
            self.run_id,
            77,
            "waiting",
            "waiting for a review slot",
            pr={"head": SHA, "title": "Slack only"},
        )
        self.assertEqual(self.store.waits(77, SHA), 1)
        _, page = self.request("GET", "/activity?show=skipped")
        self.assertIn("Waiting for a review slot", page)

    def test_an_open_pr_keeps_its_card_past_the_window(self):
        month_ago = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
        self.store.update("reviews", self.ids[45], started_at=month_ago)
        self.assertNotIn(45, [c["pr"] for c in cards.todo(self.store, self.cfg)])
        self.store.set_pr_status(45, "OPEN", "REVIEW_REQUIRED", False)
        self.assertIn(45, [c["pr"] for c in cards.todo(self.store, self.cfg)])
        asked, saved = [], gh.pr_states
        gh.pr_states = lambda cfg, numbers: (
            asked.extend(numbers)
            or {45: {"state": "MERGED", "decision": "APPROVED", "merge_queue": False}}
        )
        try:
            cards.refresh_status(self.store, self.cfg)
        finally:
            gh.pr_states = saved
        self.assertIn(45, asked)
        self.assertNotIn(45, [c["pr"] for c in cards.todo(self.store, self.cfg)])

    def test_reviewing_a_handed_off_pr_brings_it_back_and_says_so(self):
        from types import SimpleNamespace

        from plus1 import cli

        self.store.ignore(44, "Clean change")
        saved = (gh.run, gh.fetch_pr)
        gh.run = lambda args, input_text=None, timeout=90: ""
        gh.fetch_pr = lambda cfg, n: {"state": "CLOSED", "number": n}
        self.cfg.slack.enabled, self.cfg.checkout = False, self.tmp.name
        try:
            dry = SimpleNamespace(
                pr=44, any_time=True, force=False, plan=True, trigger="ui", wait_lock=False
            )
            cli.tick(self.cfg, dry)
            self.assertTrue(self.store.is_ignored(44))
            args = SimpleNamespace(
                pr=44, any_time=True, force=False, plan=False, trigger="ui", wait_lock=False
            )
            cli.tick(self.cfg, args)
        finally:
            gh.run, gh.fetch_pr = saved
        self.assertFalse(self.store.is_ignored(44))
        self.assertEqual(
            self.store.query("SELECT actor FROM actions WHERE action = 'unignore'")[0][0], "ui"
        )

    def test_a_handled_mark_does_not_hide_a_newer_review_at_the_same_commit(self):
        self.act(44, "would_comment", note="missing spec", tag="missed issue")
        self.assertNotIn(44, [c["pr"] for c in cards.todo(self.store, self.cfg)])
        self.add(44, "DRAFT", "Clean change", drafts=[("P0", 0)])
        card = cards.card_for_pr(self.store, self.cfg, 44)
        self.assertEqual(card["state"], "blocking")
        _, activity = self.request("GET", "/activity")
        row = activity[activity.index(f'data-href="/review/{self.ids[44]}"') :]
        self.assertNotIn("You handled it", row[: row.index("</li>")])

    def test_handed_off_list_honors_search(self):
        self.store.ignore(44, "Clean change")
        self.store.ignore(45, "Migration")
        _, page = self.request("GET", "/activity?show=handed_off&q=clean")
        self.assertIn('action="/pr/44/unignore"', page)
        self.assertNotIn('action="/pr/45/unignore"', page)

    def test_palette_lists_latest_review_per_pr(self):
        resp, text = self.request("GET", "/palette.json")
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        data = json.loads(text)
        self.assertFalse(data["paused"])
        by_pr = {p["pr"]: p for p in data["prs"]}
        self.assertEqual(set(by_pr), {42, 43, 44, 45, 46})
        self.assertEqual(by_pr[44]["review"], self.ids[44])
        self.assertEqual(by_pr[44]["outcome"], "Looks good")

    def test_a_rubric_learned_from_github_shows_without_a_review(self):
        pid = self.store.insert(
            "proposals",
            kind="learned",
            created_at=now(),
            status="ready",
            note="Learned from 3 reviews",
            rationale="You block on locks.",
            edits=[{"find": "a\n", "replace": "b\n"}],
            before="a\n",
            after="b\n",
        )
        for path in (f"/proposals/{pid}", "/proposals", "/insights"):
            resp, page = self.request("GET", path)
            self.assertEqual(resp.status, 200, path)
            self.assertIn(
                "Learned from your reviews on GitHub"
                if path != f"/proposals/{pid}"
                else "Rubric learned from your reviews on GitHub",
                page,
            )

    def test_review_now_validates_target(self):
        cookie, token = self.session()
        resp, _ = self.request(
            "POST",
            "/review-now",
            cookie=cookie,
            body={"csrf": token, "target": "https://github.com/x/y/pull/5"},
        )
        self.assertIn("error=", resp.getheader("Location") or "")
        resp, _ = self.request(
            "POST", "/review-now", cookie=cookie, body={"csrf": token, "target": "#77"}
        )
        self.assertTrue(resp.getheader("Location").startswith("/activity"))
        self.assertIn({"review_now": 77}, self.posted)

    def second_repo(self):
        api = SimpleNamespace(**{**vars(self.cfg.repos[0]), "name": "acme/api", "checkout": ""})
        self.cfg.repos.append(api)
        Store(self.cfg).db.close()
        pid = prs.pid("acme/api", 7)
        pr = {
            "id": pid,
            "number": 7,
            "head": SHA,
            "base": "main",
            "title": "Api change",
            "author": "teammate",
            "url": "https://github.com/acme/api/pull/7",
            "labels": [],
            "files": ["app/y.rb"],
            "additions": 1,
            "deletions": 1,
        }
        rid = self.store.start_review(self.run_id, pr, "requested", "substantive", "")
        self.store.update("reviews", rid, verdict="DEFER", reason="r", diff=DIFF, finished_at=now())
        return pid

    def test_a_second_repo_names_every_pr_and_links_to_its_own_github(self):
        self.second_repo()
        _, page = self.request("GET", "/")
        self.assertIn("api#7", page)
        self.assertIn("web#42", page)
        self.assertIn("https://github.com/acme/api/pull/7", page)
        _, palette = self.request("GET", "/palette.json")
        item = next(p for p in json.loads(palette)["prs"] if p["repo"] == "acme/api")
        self.assertEqual((item["pr"], item["label"]), (7, "api#7"))

    def test_each_repo_has_its_own_checklist_and_setting(self):
        self.second_repo()
        _, page = self.request("GET", "/insights")
        self.assertIn("Ready to approve on its own in globex/web?", page)
        self.assertIn("Ready to approve on its own in acme/api?", page)
        self.assertIn('href="/settings#repo-1"', page)
        self.assertNotIn("It approves clean PRs in", page)
        self.cfg.repos[1].auto_approve = True
        _, page = self.request("GET", "/insights")
        self.assertIn("It approves clean PRs in acme/api without you.", page)
        self.assertIn("Turn it off in Settings", page)
        self.assertIn("Auto-approve", self.request("GET", "/")[1])

    def test_a_second_repos_pr_is_found_by_its_github_number(self):
        self.second_repo()
        for q in ("7", "#7", "api#7"):
            _, page = self.request("GET", f"/activity?q={urllib.parse.quote(q)}")
            self.assertIn("Api change", page, q)

    def test_a_repo_taken_out_of_the_config_keeps_its_reviews_readable(self):
        pid = self.second_repo()
        rid = self.store.query("SELECT id FROM reviews WHERE pr = ?", pid)[0][0]
        self.cfg.repos = self.cfg.repos[:1]
        polled = []
        saved = gh.pr_states
        gh.pr_states = lambda cfg, numbers: polled.append((cfg.repo, list(numbers))) or {}
        try:
            cards.refresh_status(self.store, self.cfg)
        finally:
            gh.pr_states = saved
        self.assertEqual([repo for repo, _ in polled], ["globex/web"])
        resp, _ = self.request("GET", f"/review/{rid}")
        self.assertEqual(resp.status, 200)

    def test_review_now_takes_a_link_from_any_configured_repo(self):
        pid = self.second_repo()
        cookie, token = self.session()
        resp, _ = self.request(
            "POST",
            "/review-now",
            cookie=cookie,
            body={"csrf": token, "target": "https://github.com/acme/api/pull/7"},
        )
        self.assertTrue(resp.getheader("Location").startswith("/activity"))
        self.assertIn({"review_now": pid}, self.posted)

    def test_expired_pending_is_not_executed(self):
        self.act(44, "approve")
        executor.run_due(
            self.cfg,
            self.store,
            now_ts=time.time() + executor.UNDO_SECONDS + executor.STALE_AFTER_SECONDS + 5,
        )
        self.assertEqual(self.posted, [])
        self.assertEqual(self.store.query("SELECT status FROM pending_actions")[0][0], "expired")
        self.assertEqual(
            self.told,
            [
                (
                    "Not approved on #44",
                    "Plus One was offline when it was due, so nothing happened.",
                    f"/review/{self.ids[44]}",
                )
            ],
        )

    def test_a_post_refused_for_new_commits_reviews_them_on_its_own(self):
        old = self.ids[44]
        self.act(44, "approve")
        gh.head_sha = lambda cfg, n: "c" * 40
        self.run_due()
        self.assertEqual(self.posted, [{"review_now": 44}])
        self.assertEqual(
            self.told,
            [
                (
                    "Not approved on #44",
                    "The PR has new commits since the review, so nothing was posted. It's reviewing them now.",
                    f"/review/{old}",
                )
            ],
        )
        self.assertIsNone(cards.card_for_pr(self.store, self.cfg, 44))
        self.assertNotIn(44, [c["pr"] for c in cards.todo(self.store, self.cfg)])
        _, row = self.request("GET", "/partials/card/44?variant=card")
        self.assertIn("so nothing was posted. It&#39;s reviewing them now.", row)
        self.assertIn('hx-get="/partials/card/44?variant=card" hx-trigger="every 5s"', row)
        self.assertNotIn("/card/44/review_now", row)
        _, review = self.request("GET", f"/review/{old}")
        self.assertIn("so nothing was posted. It&#39;s reviewing them now.", review)
        self.assertIn(
            'hx-get="/partials/card/44?variant=page&amp;open=1" hx-trigger="every 5s"', review
        )

        # A review that never got going hands the card back after an hour.
        hours_ago = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        self.store.db.execute("UPDATE pending_actions SET finished_at = ?", (hours_ago,))
        card = cards.card_for_pr(self.store, self.cfg, 44)
        self.assertTrue(card["stale"])
        self.assertEqual(card["failure"][1].split(". ")[-1], "Review it again first.")
        self.store.db.execute("UPDATE pending_actions SET finished_at = ?", (now(),))

        newer = self.add(44, "APPROVE", "Looks good")
        self.store.update("reviews", newer, sha="c" * 40, finished_at=None)
        self.assertIsNone(cards.card_for_pr(self.store, self.cfg, 44))
        _, row = self.request("GET", "/partials/card/44?variant=card")
        self.assertIn("It&#39;s reviewing them now.", row)
        waiting, body = self.request("GET", "/partials/card/44?variant=page&open=1", htmx=True)
        self.assertEqual((waiting.status, waiting.getheader("HX-Redirect")), (200, None))
        self.assertIn("It&#39;s reviewing them now.", body)
        self.store.update("reviews", newer, finished_at=now())
        card = cards.card_for_pr(self.store, self.cfg, 44)
        self.assertEqual((card["review_id"], card["failure"]), (newer, None))
        # The old review page opens the new review's page, not just its card amid the old evidence.
        landed, _ = self.request("GET", "/partials/card/44?variant=page&open=1", htmx=True)
        self.assertEqual(
            (landed.status, landed.getheader("HX-Redirect")), (204, f"/review/{newer}")
        )
        swap, body = self.request("GET", "/partials/card/44?variant=card", htmx=True)
        self.assertEqual((swap.status, swap.getheader("HX-Redirect")), (200, None))
        self.assertIn('id="card-44"', body)

    def test_a_review_page_opens_a_newer_review_instead_of_taking_its_card(self):
        old = self.ids[44]
        newer = self.add(44, "APPROVE", "Looks good")

        def poll(shown):
            url = f"http://127.0.0.1:{self.port}/review/{shown}"
            return self.request(
                "GET", "/partials/card/44?variant=page", htmx=True, headers={"HX-Current-URL": url}
            )

        moved, _ = poll(old)
        self.assertEqual((moved.status, moved.getheader("HX-Redirect")), (204, f"/review/{newer}"))
        same, body = poll(newer)
        self.assertEqual((same.status, same.getheader("HX-Redirect")), (200, None))
        self.assertIn('id="pane-44"', body)

    def test_a_refused_post_while_paused_starts_no_review(self):
        State(self.cfg).pause()
        self.act(44, "approve")
        gh.head_sha = lambda cfg, n: "c" * 40
        self.run_due()
        self.assertEqual(self.posted, [])
        self.assertTrue(self.told[0][1].endswith("Review it again first."))
        self.assertTrue(cards.card_for_pr(self.store, self.cfg, 44)["stale"])

    def test_a_refused_post_leaves_a_review_already_running_alone(self):
        self.act(44, "approve")
        gh.head_sha = lambda cfg, n: "c" * 40
        newer = self.add(44, "APPROVE", "Looks good")
        self.store.update("reviews", newer, finished_at=None)
        self.run_due()
        self.assertEqual(self.posted, [])
        self.assertTrue(self.told[0][1].endswith("It's reviewing them now."))

    def test_a_refused_approval_reaches_andy_and_stays_on_the_card_when_no_review_starts(self):
        def down(cfg, n):
            raise actions.ActionError("Could not start review: systemd is down")

        actions.review_now = down
        self.act(44, "approve")
        self.assertIn(
            "This happens on the server, so you can close the app.", self.request("GET", "/")[1]
        )
        gh.head_sha = lambda cfg, n: "c" * 40
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_due()
        self.assertIn("systemd is down", err.getvalue())
        self.assertEqual(self.posted, [])
        self.assertEqual(
            self.told,
            [
                (
                    "Not approved on #44",
                    "The PR has new commits since the review, so nothing was posted. Review it again first.",
                    f"/review/{self.ids[44]}",
                )
            ],
        )
        self.store.db.execute("UPDATE pending_actions SET execute_at = execute_at - 86400")
        self.store.insert(
            "pending_actions",
            kind="approve",
            pr=44,
            review_id=self.ids[44],
            sha=SHA,
            payload={},
            created_at=now(),
            execute_at=time.time() - 60,
            status="cancelled",
        )
        _, page = self.request("GET", "/")
        row = page[page.index('id="card-44"') :]
        row = (
            row[: row.index("</summary>")]
            if "<summary" in row[:200]
            else row[: row.index("</article>")]
        )
        self.assertIn("Not approved. The PR has new commits since the review", row)
        card = cards.card_for_pr(self.store, self.cfg, 44)
        self.assertTrue(card["stale"])
        self.assertEqual(card["decision"], "New commits since this review")
        _, review = self.request("GET", f"/review/{self.ids[44]}")
        self.assertIn(
            "<strong>Not approved.</strong> The PR has new commits since the review", review
        )

    def test_a_refused_post_points_at_the_next_review(self):
        self.act(42, "approve_with_comment")
        gh.head_sha = lambda cfg, n: "c" * 40
        self.run_due()
        self.assertEqual(self.posted, [{"review_now": 42}])
        newer = self.add(42, "DRAFT", "Worth fixing")
        self.store.update("reviews", newer, verdict=None, finished_at=None)
        self.assertIsNone(cards.card_for_pr(self.store, self.cfg, 42))

        _, row = self.request("GET", "/partials/card/42?variant=card")
        self.assertIn(
            "<strong>Comment not posted on #42.</strong> The PR has new commits since the review, "
            "so nothing was posted. It&#39;s reviewing them now.",
            row,
        )
        self.assertIn('class="dcard slim is-row is-reviewing"', row)
        self.assertIn('hx-get="/partials/card/42?variant=card" hx-trigger="every 5s"', row)
        self.assertNotIn("/card/42/review_now", row)

        hours_ago = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        self.store.update("reviews", newer, started_at=hours_ago)
        _, row = self.request("GET", "/partials/card/42?variant=card")
        self.assertIn("Review it again first.", row)
        self.assertNotIn("every 5s", row)
        self.assertNotIn("is-reviewing", row)
        self.assertIn(
            'action="/card/42/review_now" hx-post="/card/42/review_now" hx-target="#card-42"', row
        )
        _, pane = self.request("GET", "/partials/card/42?variant=page")
        self.assertIn('hx-target="#pane-42"', pane)
        self.act(42, "review_now")
        self.assertEqual(self.posted.count({"review_now": 42}), 2)

        self.store.update("reviews", newer, verdict="DEFER", reason="r", finished_at=now())
        _, card = self.request("GET", "/partials/card/42?variant=card")
        self.assertIn('id="card-42"', card)
        self.assertNotIn("Comment not posted", card)


class LocalWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = config.load()
        cfg.state_dir = Path(self.tmp.name)
        cfg.notify = False
        cfg.web.mode, cfg.web.allowed_login = "local", ""
        self.cfg = cfg
        self.store = Store(cfg)
        self.server = webapp.TcpServer(("127.0.0.1", 0), webapp.make_handler(cfg))
        self.port = self.server.server_address[1]
        cfg.web.port = self.port
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def get(self, path, host=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if cookie:
            sent["Cookie"] = cookie
        conn.request("GET", path, headers=sent)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp, body

    def sign_in(self, link=None):
        link = link or signin.link(self.cfg, self.store)
        resp, _ = self.get(link.split(str(self.port), 1)[1])
        return resp

    def test_the_sign_in_link_sets_a_session_and_nothing_else_gets_in(self):
        self.assertEqual(self.get("/")[0].status, 401)
        self.assertEqual(self.get("/", headers={"Tailscale-User-Login": OWNER})[0].status, 401)
        self.assertEqual(self.get("/login?code=wrong")[0].status, 401)
        link = signin.link(self.cfg, self.store)
        resp = self.sign_in(link)
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/"))
        cookie = resp.getheader("Set-Cookie") or ""
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("Secure", cookie)
        session = cookie.split(";")[0]
        resp, page = self.get("/", cookie=session)
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Set-Cookie"))
        self.assertNotIn('href="/devices"', page)
        self.assertEqual(oct((self.cfg.state_dir / "web-token").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.sign_in(link).status, 401)
        self.assertNotIn(signin.token(self.cfg), link)

    def post(self, path, session, csrf, cookies=""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            path,
            body=urllib.parse.urlencode({"csrf": csrf}),
            headers={
                "Host": f"127.0.0.1:{self.port}",
                "Cookie": "; ".join(c for c in (session, cookies) if c),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp

    def test_a_csrf_cookie_planted_from_another_port_doesnt_work(self):
        session = self.sign_in().getheader("Set-Cookie").split(";")[0]
        _, page = self.get("/", cookie=session)
        token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        planted = "x" * 40
        self.assertEqual(
            self.post("/pause", session, planted, cookies=f"plus1_csrf={planted}").status, 403
        )
        self.assertEqual(self.post("/pause", session, token).status, 303)

    def test_opening_the_app_keeps_the_code_off_the_command_line(self):
        opened = []
        saved, signin.host.open_url = signin.host.open_url, opened.append
        try:
            url = signin.open_in_browser(self.cfg, self.store)
        finally:
            signin.host.open_url = saved
        page = self.cfg.state_dir / "sign-in.html"
        self.assertEqual(opened, [page.resolve().as_uri()])
        self.assertIn(url, page.read_text())
        self.assertEqual(oct(page.stat().st_mode & 0o777), "0o600")

    def test_a_session_ends_after_its_days(self):
        old = signin.session(self.cfg, issued=time.time() - signin.SESSION_DAYS * 86400 - 60)
        self.assertEqual(self.get("/", cookie=f"{signin.COOKIE}={old}")[0].status, 401)
        recent = signin.session(self.cfg, issued=time.time() - 86400)
        self.assertEqual(self.get("/", cookie=f"{signin.COOKIE}={recent}")[0].status, 200)
        forged = recent.split(".")[0] + "." + "0" * 64
        self.assertEqual(self.get("/", cookie=f"{signin.COOKIE}={forged}")[0].status, 401)

    def test_a_link_left_unused_expires(self):
        link = signin.link(self.cfg, self.store)
        pending = json.loads(self.store.get_meta("web_login", ""))
        self.store.set_meta("web_login", json.dumps({**pending, "expires": time.time() - 1}))
        self.assertEqual(self.sign_in(link).status, 401)

    def test_a_secret_another_process_just_made_is_kept(self):
        made = signin.token(self.cfg)
        reads, real = [], signin._read
        # The first read misses, as if this process looked before the other one wrote.
        signin._read = lambda path: reads.append(path) or (None if len(reads) == 1 else real(path))
        try:
            self.assertEqual(signin.token(self.cfg), made)
        finally:
            signin._read = real
        self.assertEqual(signin.token(self.cfg), made)
        self.assertEqual(list(self.cfg.state_dir.glob(".web-token-*")), [])

    def test_a_relative_state_dir_still_opens_the_sign_in_page(self):
        opened = []
        saved_open, signin.host.open_url = signin.host.open_url, opened.append
        cwd = os.getcwd()
        os.chdir(self.cfg.state_dir)
        saved_dir, self.cfg.state_dir = self.cfg.state_dir, Path("relative-state")
        try:
            signin.open_in_browser(self.cfg, self.store)
        finally:
            signin.host.open_url = saved_open
            self.cfg.state_dir = saved_dir
            os.chdir(cwd)
        self.assertTrue(opened[0].startswith("file:///"))

    def test_an_empty_secret_never_signs_anyone_in(self):
        (self.cfg.state_dir / "web-token").write_text("")
        issued = int(time.time())
        guessed = signin.hmac.new(
            b"", f"plus1 session {issued}".encode(), signin.hashlib.sha256
        ).hexdigest()
        self.assertEqual(self.get("/", cookie=f"{signin.COOKIE}={issued}.{guessed}")[0].status, 401)
        self.assertGreaterEqual(len(signin.token(self.cfg)), signin.MIN_TOKEN)

    def test_the_access_log_leaves_the_token_out(self):
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err):
            self.sign_in()
        self.assertIn("/login?code=<redacted> ", err.getvalue())

    def test_a_page_reached_through_another_host_name_is_refused(self):
        session = self.sign_in().getheader("Set-Cookie").split(";")[0]
        for host in (
            "evil.example",
            f"evil.example:{self.port}",
            f"127.0.0.1.evil.example:{self.port}",
        ):
            self.assertEqual(self.get("/", host=host, cookie=session)[0].status, 403, host)
        self.assertEqual(
            self.get("/", host=f"localhost:{self.port}", cookie=session)[0].status, 200
        )

    def test_a_new_link_signs_every_browser_out(self):
        old = self.sign_in().getheader("Set-Cookie").split(";")[0]
        link = signin.link(self.cfg, self.store, new=True)
        self.assertEqual(self.get("/", cookie=old)[0].status, 401)
        fresh = self.sign_in(link).getheader("Set-Cookie").split(";")[0]
        self.assertEqual(self.get("/", cookie=fresh)[0].status, 200)

    def test_phone_notifications_wait_for_tailscale(self):
        session = self.sign_in().getheader("Set-Cookie").split(";")[0]
        resp, page = self.get("/settings", cookie=session)
        self.assertEqual(resp.status, 200)
        self.assertIn("Notifications on your phone need the web app served over Tailscale", page)
        self.assertNotIn("enable-push", page)


class LauncherTest(unittest.TestCase):
    def test_launcher_turns_route_errors_into_their_status(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.toml"
            (Path(d) / "rubric.md").write_text((ROOT / "tests" / "rubric.md").read_text())
            cfg_path.write_text(
                re.sub(
                    r"(?m)^state_dir = .*$",
                    f'state_dir = "{d}"',
                    (ROOT / "tests" / "config.toml").read_text(),
                )
            )
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            proc = subprocess.Popen(
                [str(ROOT / "bin" / "plus1-web"), "--tcp", f"127.0.0.1:{port}", "--dev-login"],
                env={**os.environ, "PLUS1_CONFIG": str(cfg_path)},
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                self.assertIn("listening", proc.stdout.readline())
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/review/99999")
                self.assertEqual(conn.getresponse().status, 404)
                conn.close()
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                proc.stdout.close()


class DiffParseTest(unittest.TestCase):
    def test_marks_and_line_numbers(self):
        files = queries.parse_diff(DIFF, [("app/x.rb", 3)])
        rows = [r for r in files[0]["rows"] if r["kind"] != "hunk"]
        self.assertEqual(
            [(r["kind"], r["old"], r["new"]) for r in rows],
            [("ctx", 1, 1), ("del", 2, None), ("add", None, 2), ("add", None, 3), ("ctx", 3, 4)],
        )
        self.assertEqual([r["new"] for r in rows if r["mark"]], [3])

    def test_ticks_escapes_before_formatting(self):
        out = str(webapp.ticks("use `x<y>` not <script>"))
        self.assertEqual(out, "use <code>x&lt;y&gt;</code> not &lt;script&gt;")

    def test_focus_rows_keeps_window_around_marks(self):
        rows = [{"kind": "ctx", "mark": i == 20} for i in range(40)]
        out = webapp.focus_rows(rows, radius=3)
        self.assertEqual(len(out), 7)


if __name__ == "__main__":
    unittest.main()
