import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plus1 import actions, cli, gh, yours
from plus1.review import Reviewed
from plus1.state import State
from plus1.store import Recorder, Store
from plus1.web import words

ME, OTHER = "me@example.com", "other@example.com"
REPO = "acme/web"


def git(where, *args, email=OTHER):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": email.split("@")[0],
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": email.split("@")[0],
        "GIT_COMMITTER_EMAIL": email,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", "-C", str(where), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def commit(where, files, message, email):
    for name, text in files.items():
        path = Path(where) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(where, "add", name)
    git(where, "commit", "-q", "-m", message, email=email)
    return git(where, "rev-parse", "HEAD")


def lines(fmt, n):
    return "".join(fmt.format(i) + "\n" for i in range(1, n + 1))


class Repo(unittest.TestCase):
    """An origin with a PR whose head is at refs/pull/1/head, and a clone of it, as on the owner's machine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        src = root / "src"
        src.mkdir()
        git(src, "init", "-q", "-b", "main")
        commit(src, {"a.py": lines("x{} = 'a'", 30)}, "mine", ME)
        commit(src, {"b.py": lines("y{} = 1", 10)}, "theirs", OTHER)
        commit(src, {"db/schema.rb": lines("t{} = 1", 40)}, "a migration", ME)
        tidy = commit(src, {"a.py": lines('x{} = "a"', 30)}, "quote style", OTHER)
        commit(src, {".git-blame-ignore-revs": tidy + "\n"}, "ignore the sweep", OTHER)
        commit(src, {"a.py": lines('x{} = "a"', 29) + 'x30 = "z"\n'}, "touch a.py again", ME)
        git(src, "checkout", "-q", "-b", "feature")
        self.head = commit(
            src,
            {
                "a.py": lines('x{} = "b"', 25)
                + "".join(f'x{i} = "a"\n' for i in range(26, 30))
                + 'x30 = "z"\n',
                "b.py": "y1 = 2\ny2 = 2\n" + lines("y{} = 1", 10).split("\n", 2)[2],
                "db/schema.rb": lines("t{} = 2", 40),
                "c.py": "new = 1\n",
            },
            "the PR",
            OTHER,
        )
        git(src, "update-ref", "refs/pull/1/head", self.head)
        git(src, "checkout", "-q", "main")
        self.clone = root / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(src), str(self.clone)], check=True, capture_output=True
        )
        git(self.clone, "config", "user.email", ME)
        self.cfg = SimpleNamespace(
            me="andy",
            emails=[],
            repo=REPO,
            checkout=str(self.clone),
            repos=[SimpleNamespace(name=REPO, checkout=str(self.clone), hold_yours=True)],
            state_dir=root / "state",
        )
        self.store = Store(self.cfg)
        self.pr = {
            "number": 1,
            "head": self.head,
            "base": "main",
            "files": ["a.py", "b.py", "db/schema.rb", "c.py"],
            "requests": [],
        }

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_it_counts_the_lines_you_wrote_that_the_pr_changes_past_formatting_sweeps(self):
        yours.gather(self.cfg, self.store, [self.pr], log=lambda m: None)
        found = self.pr["yours"]
        # a.py's first 25 lines are yours under the quote-style sweep the repo ignores; b.py's two are theirs, and the
        # schema dump counts for nobody.
        self.assertEqual((found["lines"], found["changed"]), (25, 27))
        self.assertEqual(found["top"], [["a.py", 25]])
        self.assertEqual(found["files"], [["a.py", 2]])
        self.assertEqual(yours.kinds(found), ["code", "files"])
        self.assertEqual(yours.hold(found), "rewrites your code")
        self.assertEqual(self.store.yours(1, self.head)["lines"], 25)

    def test_a_measured_commit_isnt_read_again(self):
        yours.gather(self.cfg, self.store, [self.pr], log=lambda m: None)
        saved = yours._git
        yours._git = lambda *a, **k: (_ for _ in ()).throw(AssertionError("read git again"))
        try:
            again = {**self.pr, "requests": [{"by": "ann", "bot": False, "to": "andy"}]}
            yours.gather(self.cfg, self.store, [again], log=lambda m: None)
        finally:
            yours._git = saved
        self.assertEqual((again["yours"]["lines"], again["yours"]["asked"]), (25, "ann"))

    def test_git_failing_holds_the_approval_and_isnt_kept(self):
        said = []
        broken = {**self.pr, "base": "no-such-branch"}
        yours.gather(self.cfg, self.store, [broken], log=said.append)
        self.assertIn("error", broken["yours"])
        self.assertEqual(yours.hold(broken["yours"]), "couldn't tell whose code it changes")
        self.assertIsNone(self.store.yours(1, self.head))
        self.assertTrue(said)

    def test_git_config_email_is_found(self):
        self.assertIn(ME, yours.detected(self.cfg))
        mine = yours.matcher(self.cfg)
        self.assertTrue(mine("ME@example.com"))
        self.assertTrue(mine("123+andy@users.noreply.github.com"))
        self.assertFalse(mine(OTHER))


class SignalsTest(unittest.TestCase):
    def test_only_a_person_asking_you_by_name_counts(self):
        pr = {
            "requests": [
                {"by": "ann", "bot": False, "to": "andy"},
                {"by": "andy", "bot": False, "to": "andy"},
                {"by": "assigner", "bot": True, "to": "andy"},
                {"by": "bob", "bot": False, "to": "carol"},
            ]
        }
        self.assertEqual(yours.asked(pr, "Andy"), "ann")
        self.assertIsNone(yours.asked({"requests": pr["requests"][1:]}, "andy"))

    def test_your_lines_count_from_twenty_or_a_quarter_of_at_least_five(self):
        for lines, changed, mine in (
            (20, 500, True),
            (19, 500, False),
            (5, 20, True),
            (4, 8, False),
            (5, 21, False),
        ):
            self.assertEqual(
                "code" in yours.kinds({"lines": lines, "changed": changed}), mine, (lines, changed)
            )

    def test_by_name_holds_first_and_recent_commits_alone_never_hold(self):
        self.assertEqual(
            yours.hold({"asked": "ann", "lines": 40, "changed": 40}), "you were asked by name"
        )
        self.assertIsNone(yours.hold({"files": [["a.py", 3]]}))
        self.assertIsNone(yours.hold({}))

    def test_github_review_requests_become_who_asked_whom(self):
        raw = {
            "requests": {
                "nodes": [
                    {
                        "actor": {"login": "ann", "__typename": "User"},
                        "requestedReviewer": {"login": "andy"},
                    },
                    {"actor": {"login": "ann", "__typename": "User"}, "requestedReviewer": {}},
                    {
                        "actor": {"login": "auto", "__typename": "Bot"},
                        "requestedReviewer": {"login": "andy"},
                    },
                ]
            }
        }
        self.assertEqual(
            gh.normalize(_raw(raw))["requests"],
            [{"by": "ann", "bot": False, "to": "andy"}, {"by": "auto", "bot": True, "to": "andy"}],
        )

    def test_the_card_says_each_signal_in_words(self):
        chips, said = words.yours(
            {
                "asked": "ann",
                "lines": 42,
                "changed": 60,
                "top": [["app/models/cart.rb", 30], ["app/models/line.rb", 12]],
                "top_count": 2,
                "files": [["app/models/cart.rb", 4]],
                "files_count": 3,
            }
        )
        self.assertEqual(chips, ["asked you", "your code", "your files"])
        self.assertEqual(
            said,
            [
                "ann asked you by name",
                "Rewrites 42 lines you wrote in `cart.rb` and 1 more file",
                "You committed to `cart.rb` 4 times in 6 months, and to 2 more files it changes",
            ],
        )
        self.assertEqual(words.yours(None), ([], []))


def _raw(extra):
    return {
        "number": 1,
        "url": "https://github.com/acme/web/pull/1",
        "title": "t",
        "body": "",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "isInMergeQueue": False,
        "headRefOid": "a" * 40,
        "headRefName": "x",
        "baseRefName": "main",
        "createdAt": "2026-10-01T00:00:00Z",
        "author": {"login": "bob"},
        "labels": {"nodes": []},
        "files": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "additions": 1,
        "deletions": 1,
        "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "comments": {"nodes": []},
        "timelineItems": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
        **extra,
    }


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(
            state_dir=Path(self.tmp.name),
            me="andy",
            emails=[],
            repo=REPO,
            checkout=self.tmp.name,
            repos=[SimpleNamespace(name=REPO, auto_approve=True, hold_yours=True)],
            limits=SimpleNamespace(
                max_approvals_per_tick=5,
                max_approvals_per_day=20,
                min_ready_minutes=30,
                max_map_lines=10000,
            ),
            filters=SimpleNamespace(hold_labels=[], blocking_markers=[]),
            lanes=SimpleNamespace(never_approve_owners=[], never_approve_paths=[]),
        )
        self.store = Store(self.cfg)
        self.saved = actions.approve, cli.fresh_state, gh.fetch_pr
        self.approved = []
        actions.approve = lambda cfg, st, store, n, sha, reason, actor="cli": self.approved.append(
            n
        )
        cli.fresh_state = lambda cfg, n: {"state": "OPEN", "decision": "REVIEW_REQUIRED"}

    def tearDown(self):
        actions.approve, cli.fresh_state, gh.fetch_pr = self.saved
        self.store.db.close()
        self.tmp.cleanup()

    def pr(self, found):
        return {
            "number": 7,
            "head": "abc123",
            "base": "main",
            "title": "t",
            "author": "ann",
            "url": "u",
            "labels": [],
            "files": ["a.py"],
            "additions": 1,
            "deletions": 1,
            "yours": found,
        }

    def handle(self, found):
        run_id = self.store.start_run("timer", "live")
        pr = self.pr(found)
        review_id = self.store.start_review(run_id, pr, "requested", None, "")
        res = Reviewed()
        res.verdict, res.reason = "APPROVE", "clean"
        cli.handle(
            self.cfg,
            State(self.cfg),
            self.store,
            Recorder(self.store, run_id, review_id, 7),
            review_id,
            pr,
            res,
            {"drafts": 0, "defers": 0, "approved": 0},
            auto=True,
        )
        return self.store.query("SELECT held, yours FROM reviews WHERE id = ?", review_id)[0]

    def test_a_pr_in_your_code_waits_for_you_where_the_repo_holds_them(self):
        row = self.handle({"lines": 30, "changed": 30})
        self.assertEqual((self.approved, row["held"]), ([], "rewrites your code"))
        self.assertIn('"lines": 30', row["yours"])
        self.cfg.repos[0].hold_yours = False
        self.assertIsNone(self.handle({"lines": 30, "changed": 30})["held"])
        self.assertEqual(len(self.approved), 1)

    def test_your_recent_commits_alone_dont_hold_an_approval(self):
        self.assertIsNone(self.handle({"lines": 0, "changed": 9, "files": [["a.py", 3]]})["held"])
        self.assertEqual(len(self.approved), 1)

    def test_a_held_approval_in_your_code_stays_held_on_retry(self):
        held = self.handle({"lines": 30, "changed": 30})
        self.assertEqual(held["held"], "rewrites your code")
        self.store.save_yours(7, "abc123", {"lines": 30, "changed": 30})
        fresh = {
            **self.pr(None),
            "state": "OPEN",
            "draft": False,
            "decision": "REVIEW_REQUIRED",
            "merge_queue": False,
            "mergeable": "MERGEABLE",
            "reviews": [],
            "threads": [],
            "conversation": [],
            "checks": [],
            "requests": [],
            "ready_at": datetime.now().astimezone() - timedelta(days=1),
        }
        del fresh["yours"]
        gh.fetch_pr = lambda cfg, n: dict(fresh)
        tally = {"approved": 0}
        cli.retry_held(
            self.cfg, State(self.cfg), self.store, tally, {}, datetime.now().astimezone()
        )
        self.assertEqual(self.approved, [])
        self.assertEqual(
            self.store.query("SELECT held FROM reviews ORDER BY id DESC LIMIT 1")[0][0],
            "rewrites your code",
        )

    def test_a_pr_in_your_code_moves_up_a_tier_but_never_past_follow_ups(self):
        now = datetime.now().astimezone()
        mine = {"ready_at": now, "yours": {"lines": 30, "changed": 30}}
        plain = {"ready_at": now}
        self.assertEqual(cli.priority(1, {1}, {}, mine)[0], 1)
        self.assertEqual(cli.priority(1, {1}, {}, plain)[0], 2)
        self.assertEqual(cli.priority(1, set(), {1: {"c"}}, mine)[0], 2)
        self.assertEqual(cli.priority(1, {1}, {1: {"c"}}, mine)[0], 1)
        self.assertEqual(cli.priority(1, set(), {}, {**mine, "followup": {"x": 1}})[0], 0)

    def test_the_tick_notification_counts_prs_in_your_code(self):
        self.assertEqual(
            cli.yours_note({"team": 0, "asked": 1, "yours": 2}),
            " 1 asked for you by name, 2 in your code.",
        )
        self.assertEqual(cli.yours_note({"team": 0, "asked": 0, "yours": 0}), "")


if __name__ == "__main__":
    unittest.main()
