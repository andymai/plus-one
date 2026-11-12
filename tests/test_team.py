import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PLUS1_CONFIG"] = str(ROOT / "tests" / "config.toml")

from plus1 import actions, cli, config, gh, review, team  # noqa: E402
from plus1.store import Store, now  # noqa: E402
from plus1.web import cards  # noqa: E402

SHA = "a" * 40


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load()
        self.cfg.state_dir = Path(self.tmp.name)
        self.cfg.notify = False
        self.store = Store(self.cfg)
        self.saved = (
            gh.run,
            gh.candidates,
            gh.followups,
            gh.fetch_pr,
            gh.pr_states,
            gh.team_members,
            review.run,
            cli.handle,
            actions.notify,
        )

    def tearDown(self):
        (
            gh.run,
            gh.candidates,
            gh.followups,
            gh.fetch_pr,
            gh.pr_states,
            gh.team_members,
            review.run,
            cli.handle,
            actions.notify,
        ) = self.saved
        self.store.db.close()
        self.tmp.cleanup()

    def members(self, **teams):
        for name, people in teams.items():
            self.store.set_meta(
                f"{team.PREFIX}{name.replace('__', '/')}",
                json.dumps({"members": people, "at": "2999-01-01T00:00:00+00:00"}),
            )


class RefreshTest(Base):
    def setUp(self):
        super().setUp()
        self.cfg.team.teams, self.cfg.team.others = ["Acme/Web"], ["acme/data"]
        self.read = []

        def members(name):
            self.read.append(name)
            return ["ann", "bo"]

        gh.team_members = members

    def age(self, name, **ago):
        entry = json.loads(self.store.get_meta(f"{team.PREFIX}{name}") or "{}")
        at = datetime.now().astimezone() - timedelta(**ago)
        entry["at"] = at.isoformat(timespec="seconds")
        self.store.set_meta(f"{team.PREFIX}{name}", json.dumps(entry))

    def test_reads_each_new_team_once_and_again_after_a_day(self):
        team.refresh(self.cfg, self.store)
        self.assertEqual(self.read, ["acme/web", "acme/data"])
        team.refresh(self.cfg, self.store)
        self.assertEqual(len(self.read), 2)
        self.age("acme/web", days=1, minutes=1)
        team.refresh(self.cfg, self.store)
        self.assertEqual(self.read[2:], ["acme/web"])

    def test_a_new_team_is_read_without_rereading_the_others(self):
        team.refresh(self.cfg, self.store)
        self.cfg.team.others.append("acme/ios")
        team.refresh(self.cfg, self.store)
        self.assertEqual(self.read[2:], ["acme/ios"])

    def test_a_failed_read_keeps_the_last_members_and_retries_within_the_hour(self):
        team.refresh(self.cfg, self.store)
        self.age("acme/web", days=2)

        def broken(name):
            raise gh.GhError("gh api failed: 404")

        gh.team_members = broken
        said = []
        team.refresh(self.cfg, self.store, said.append)
        self.assertIn("acme/web", said[0])
        self.assertEqual(team.known(self.cfg, self.store)[0], {"ann", "bo"})
        self.assertEqual(
            team.status(self.cfg.team.teams, self.store),
            [{"name": "Acme/Web", "size": "2 people", "count": 2, "failed": True}],
        )
        gh.team_members = lambda name: ["cy"]
        team.refresh(self.cfg, self.store)
        self.assertEqual(team.known(self.cfg, self.store)[0], {"ann", "bo"})
        self.age("acme/web", hours=1, minutes=1)
        team.refresh(self.cfg, self.store)
        self.assertEqual(team.known(self.cfg, self.store)[0], {"cy"})
        self.assertFalse(team.status(self.cfg.team.teams, self.store)[0]["failed"])

    def test_a_timeout_or_a_missing_gh_counts_as_a_failed_read(self):
        for problem in (subprocess.TimeoutExpired("gh", 30), FileNotFoundError("gh")):

            def broken(name, problem=problem):
                raise problem

            gh.team_members = broken
            self.store.db.execute("DELETE FROM meta WHERE key LIKE 'team_members:%'")
            team.refresh(self.cfg, self.store, lambda _: None)
            self.assertTrue(team.status(["acme/web"], self.store)[0]["failed"])

    def test_a_team_taken_off_the_lists_stops_counting(self):
        team.refresh(self.cfg, self.store)
        self.cfg.team.teams, self.cfg.team.others = [], []
        team.refresh(self.cfg, self.store)
        self.assertEqual(team.known(self.cfg, self.store), (set(), set()))

    def test_a_slow_read_cant_erase_a_team_read_meanwhile(self):
        def on(*names):
            return SimpleNamespace(
                me="me", team=SimpleNamespace(teams=list(names), people=[], others=[])
            )

        def members(name):
            if name == "acme/web":
                team.refresh(on("acme/ios"), self.store)
                return ["ann"]
            return ["zed"]

        gh.team_members = members
        team.refresh(on("acme/web"), self.store)
        self.assertEqual(
            team.known(on("acme/web", "acme/ios"), self.store), ({"ann", "zed"}, set())
        )

    def test_a_team_never_read_says_so(self):
        self.assertEqual(
            team.status(["acme/new"], self.store),
            [{"name": "acme/new", "size": "not read yet", "count": 0, "failed": False}],
        )


class MatchTest(Base):
    def test_teams_and_people_are_the_team_and_someone_on_both_lists_counts_as_the_team(self):
        self.cfg.team.teams = ["acme/web"]
        self.cfg.team.people = ["Dee"]
        self.cfg.team.others = ["acme/data"]
        self.members(acme__web=["ann", "bo"], acme__data=["bo", "cy"])
        people = team.known(self.cfg, self.store)
        self.assertEqual(people, ({"ann", "bo", "dee"}, {"cy"}))
        self.assertEqual(team.kind({"author": "ann"}, people), team.TEAM)
        self.assertEqual(team.kind({"author": "bo"}, people), team.TEAM)
        self.assertEqual(team.kind({"author": "DEE"}, people), team.TEAM)
        self.assertEqual(team.kind({"author": "cy"}, people), team.OTHER)
        self.assertEqual(team.kind({"author": "zed"}, people), "")

    def test_an_agent_pr_counts_for_the_person_who_asked_for_it(self):
        people = ({"ann"}, {"cy"})
        self.assertEqual(
            team.kind({"author": "app/devin", "requested_by": "Ann"}, people), team.TEAM
        )
        self.assertEqual(team.kind({"author": "app/devin", "requested_by": "cy"}, people), "other")
        self.assertEqual(team.kind({"author": "app/devin", "requested_by": None}, people), "")

    def test_the_owner_is_never_their_own_team(self):
        self.cfg.team.teams, self.cfg.team.people = ["acme/web"], [self.cfg.me.upper()]
        self.cfg.team.others = ["acme/data"]
        self.members(acme__web=[self.cfg.me.lower()], acme__data=[self.cfg.me.lower(), "cy"])
        self.assertEqual(team.known(self.cfg, self.store), (set(), {"cy"}))


class ConfigTest(unittest.TestCase):
    def load(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text((ROOT / "tests" / "config.toml").read_text() + extra)
            (Path(tmp) / "rubric.md").write_text("rules")
            return config.load(path)

    def test_the_lists_default_to_empty_and_a_config_can_set_them(self):
        self.assertEqual(config.load().team.teams, [])
        cfg = self.load(
            '\n[team]\nteams = ["acme/web"]\npeople = ["ann"]\nothers = ["acme/data.eng"]\n'
        )
        self.assertEqual(
            (cfg.team.teams, cfg.team.people, cfg.team.others),
            (["acme/web"], ["ann"], ["acme/data.eng"]),
        )

    def test_a_bad_entry_is_refused_with_its_key(self):
        for extra, key in (
            ('\n[team]\nteams = ["no-slash"]\n', "team.teams"),
            ('\n[team]\npeople = ["acme/web"]\n', "team.people"),
            ('\n[team]\nothers = "acme/web"\n', "team.others"),
        ):
            with self.assertRaisesRegex(config.ConfigError, key):
                self.load(extra)


class OrderTest(unittest.TestCase):
    def card(self, pr, team_kind="", yours=(), order=3, when="2026-01-01", title="Solo"):
        return {
            "pr": pr,
            "group": "blocking",
            "tone": "bad",
            "order": order,
            "when": when,
            "team": team_kind,
            "yours": list(yours),
            "stack": cards.stack_key(title),
            "creator": ("a", None),
        }

    def test_within_a_group_team_then_other_teams_then_your_code_then_the_rest(self):
        queue = [
            self.card(1, when="2026-01-01"),
            self.card(2, yours=["your code"]),
            self.card(3, team.OTHER),
            self.card(4, team.TEAM),
            self.card(5, team.TEAM, yours=["your code"], when="2026-01-09"),
            self.card(6, team.OTHER, yours=["your files"]),
        ]
        self.assertEqual([c["pr"] for c in cards.ordered(queue)], [5, 4, 6, 3, 2, 1])

    def test_the_team_never_crosses_a_group(self):
        queue = [self.card(1, team.TEAM, order=4), self.card(2, order=3)]
        self.assertEqual([c["pr"] for c in cards.ordered(queue)], [2, 1])

    def test_a_stack_takes_its_best_parts_rank_and_chip(self):
        queue = [
            self.card(1),
            self.card(2, team.OTHER, title="One [1/2] (X-1)"),
            self.card(3, team.TEAM, title="Two [2/2] (X-1)"),
            self.card(4, team.OTHER),
        ]
        rows = cards.entries(queue)
        self.assertEqual(rows[0]["kind"], "stack")
        self.assertEqual(rows[0]["team_chip"], "team")
        self.assertEqual([c["pr"] for c in cards.ordered(queue)], [2, 3, 4, 1])


class TickTest(Base):
    def pr(self, number, author, ready):
        return {
            "number": number,
            "state": "OPEN",
            "draft": False,
            "decision": "REVIEW_REQUIRED",
            "reviews": [],
            "head": SHA,
            "base": "main",
            "title": f"PR {number}",
            "author": author,
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
            "url": f"https://github.com/globex/web/pull/{number}",
            "ready_at": datetime.now().astimezone() - timedelta(days=ready),
        }

    def tick(self, numbers, draft_for=()):
        prs_by_number = {n: self.pr(n, author, ready) for n, author, ready in numbers}
        reviewed, notified = [], []
        gh.run = lambda args, input_text=None, timeout=90: ""
        gh.candidates = lambda cfg: list(prs_by_number)
        gh.followups = lambda cfg: []
        gh.fetch_pr = lambda cfg, n: prs_by_number[n]
        gh.pr_states = lambda cfg, ns: {
            n: {"state": "OPEN", "decision": "REVIEW_REQUIRED", "merge_queue": False} for n in ns
        }
        gh.team_members = lambda name: ["ann"]
        review.run = lambda cfg, pr, *a, **k: (
            reviewed.append(pr["number"]) or SimpleNamespace(cost=0.0)
        )

        def handle(rcfg, st, store, rec, review_id, pr, res, tally, auto):
            if pr["number"] in draft_for:
                tally["drafts"] += 1

        cli.handle = handle
        actions.notify = lambda cfg, title, body, path=None, store=None: notified.append(
            (body, path)
        )
        self.cfg.slack.enabled, self.cfg.checkout = False, self.tmp.name
        self.assertEqual(
            cli.tick(
                self.cfg,
                SimpleNamespace(
                    pr=None,
                    any_time=True,
                    force=False,
                    plan=False,
                    trigger="manual",
                    wait_lock=False,
                ),
            ),
            0,
        )
        return reviewed, notified

    def test_a_teammates_pr_is_reviewed_first_and_sends_the_notification_alone(self):
        self.cfg.team.teams = ["acme/web"]
        reviewed, notified = self.tick([(77, "zed", 3), (78, "ann", 1)])
        self.assertEqual(reviewed, [78, 77])
        self.assertEqual(len(notified), 1)
        self.assertIn("1 from your team.", notified[0][0])
        self.assertEqual(notified[0][1], "/")

    def test_another_teams_pr_is_reviewed_sooner_but_stays_out_of_the_notification(self):
        self.cfg.team.others = ["acme/data"]
        reviewed, notified = self.tick([(77, "zed", 3), (78, "ann", 1)])
        self.assertEqual(reviewed, [78, 77])
        self.assertEqual(notified, [])

    def test_a_check_with_nothing_to_review_still_reads_due_teams(self):
        self.cfg.team.teams = ["acme/web"]
        self.assertEqual(self.tick([]), ([], []))
        self.assertEqual(team.known(self.cfg, self.store), ({"ann"}, set()))

    def test_with_no_team_set_the_older_pr_goes_first_and_nothing_notifies(self):
        reviewed, notified = self.tick([(77, "zed", 3), (78, "ann", 1)])
        self.assertEqual(reviewed, [77, 78])
        self.assertEqual(notified, [])

    def test_a_team_only_reorders_what_already_arrived(self):
        self.cfg.team.people = ["ann"]
        reviewed, _ = self.tick([(77, "zed", 3)])
        self.assertEqual(reviewed, [77])
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM reviews")[0]["n"], 1)

    def test_a_teammate_can_rise_only_one_tier_and_never_past_a_follow_up(self):
        stamp = datetime.now().astimezone()
        plain = {"ready_at": stamp}
        mine = {"ready_at": stamp, "team": team.TEAM}
        other = {"ready_at": stamp, "team": team.OTHER}
        both = {**mine, "yours": {"lines": 30, "changed": 30}}
        self.assertEqual(cli.priority(1, {1}, {}, plain)[0], 2)
        self.assertEqual(cli.priority(1, {1}, {}, mine)[0], 1)
        self.assertEqual(cli.priority(1, {1}, {}, other)[0], 1)
        self.assertEqual(cli.priority(1, {1}, {}, both)[0], 1)
        self.assertEqual(cli.priority(1, set(), {1: {"c"}}, mine)[0], 2)
        self.assertEqual(cli.priority(1, {1}, {1: {"c"}}, mine)[0], 1)
        self.assertEqual(cli.priority(1, set(), {}, {**mine, "followup": {"x": 1}})[0], 0)

    def test_the_note_counts_the_team_first(self):
        self.assertEqual(
            cli.yours_note({"team": 2, "asked": 1, "yours": 1}),
            " 2 from your team, 1 asked for you by name, 1 in your code.",
        )
        self.assertEqual(cli.yours_note({"team": 1, "asked": 0, "yours": 0}), " 1 from your team.")


class GitHubTest(Base):
    def test_team_members_are_lowercase_and_paged(self):
        calls = []

        def run(args, input_text=None, timeout=90):
            calls.append(args)
            return "Ann\nbo\n"

        gh.run = run
        self.assertEqual(gh.team_members("acme/web"), ["ann", "bo"])
        self.assertIn("--paginate", calls[0])
        self.assertIn("orgs/acme/teams/web/members?per_page=100", calls[0])

    def test_suggestions_stay_in_the_repos_orgs_smallest_first(self):
        def run(args, input_text=None, timeout=90):
            if any("user/teams" in a for a in args):
                return "Acme\tall-eng\t104\nAcme\tweb\t8\nOther\tcore\t1\nAcme\tnew\t\n"
            if any("orgs/acme/teams" in a for a in args):
                return "all-eng\ndata\nweb\nnew\nios\n"
            raise AssertionError(args)

        gh.run = run
        self.assertEqual(
            team.suggest_mine(["Acme/api", "acme/web"]),
            [("acme/new", 0), ("acme/web", 8), ("acme/all-eng", 104)],
        )
        self.assertEqual(team.suggest_others(["acme/api"]), ["acme/data", "acme/ios"])


class SizeTest(unittest.TestCase):
    def test_one_person_and_many(self):
        self.assertEqual(
            (team.size(1), team.size(2), team.size(0)), ("1 person", "2 people", "0 people")
        )


class CardTest(Base):
    def test_a_card_shows_a_list_change_at_once(self):
        run_id = self.store.start_run("manual", "shadow")
        rid = self.store.start_review(
            run_id,
            {
                "number": 42,
                "head": SHA,
                "base": "main",
                "title": "Change",
                "author": "ann",
                "url": "https://github.com/globex/web/pull/42",
                "labels": [],
                "files": ["app/x.rb"],
                "additions": 2,
                "deletions": 1,
            },
            "requested",
            "substantive",
            "",
        )
        self.store.update("reviews", rid, verdict="APPROVE", reason="r", finished_at=now())
        card = cards.todo(self.store, self.cfg)[0]
        self.assertEqual((card["team"], card["team_chip"]), ("", None))
        self.cfg.team.people = ["ann"]
        card = cards.todo(self.store, self.cfg)[0]
        self.assertEqual((card["team"], card["team_chip"]), (team.TEAM, "team"))
        self.cfg.team.people, self.cfg.team.others = [], ["acme/data"]
        self.members(acme__data=["ann"])
        card = cards.todo(self.store, self.cfg)[0]
        self.assertEqual((card["team"], card["team_chip"]), (team.OTHER, "watching"))


if __name__ == "__main__":
    unittest.main()
