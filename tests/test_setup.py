import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PLUS1_CONFIG"] = str(ROOT / "tests" / "config.toml")
from plus1 import __version__, config, doctor, gh, host, setup, units  # noqa: E402

CODEOWNERS = """
/app/ @acme/web-team
/db/ @acme/database-reviewers
/billing/ @acme/billing-eng
/lib/money/ @acme/billing-eng
"""


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


class Scripted(setup.Prompt):
    """Answers in order; an empty answer takes the default."""

    def __init__(self, answers):
        self.answers, self.asked, self.said = list(answers), [], []
        super().__init__(ask=self._ask, say=self.said.append)

    def _ask(self, question):
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else ""


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._orig = (gh.run, units.install, host.open_url, os.environ.get("PLUS1_CONFIG"))

    def tearDown(self):
        gh.run, units.install, host.open_url, saved = self._orig
        if saved is None:
            os.environ.pop("PLUS1_CONFIG", None)
        else:
            os.environ["PLUS1_CONFIG"] = saved
        self.tmp.cleanup()

    def clone(self, name, origin):
        repo = self.dir / "code" / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "remote", "add", "origin", origin)
        return repo

    def github(self, checks=(), codeowners=CODEOWNERS, requested=("acme/api",)):
        def run(args, input_text=None, timeout=90):
            if args[:2] == ["api", "user"]:
                return json.dumps({"login": "ann", "name": "Ann Example"})
            if args[:2] == ["search", "prs"]:
                return json.dumps([{"repository": {"nameWithOwner": r}} for r in requested])
            if args[:2] == ["api", "graphql"]:
                nodes = [
                    {
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "nodes": [
                                                    {"__typename": "CheckRun", "name": c}
                                                    for c in checks
                                                ]
                                            }
                                        }
                                    }
                                }
                            ]
                        }
                    }
                ]
                return json.dumps({"data": {"repository": {"pullRequests": {"nodes": nodes}}}})
            if "CODEOWNERS" in " ".join(args):
                return codeowners
            raise AssertionError(args)

        gh.run = run

    def test_a_clone_is_found_by_its_origin_not_its_folder(self):
        self.clone("api", "git@github.com:acme/api.git")
        self.clone("web", "https://github.com/acme/web-old")
        self.assertEqual(
            setup.find_checkout("acme/api", roots=[self.dir / "code"]), self.dir / "code" / "api"
        )
        self.assertIsNone(setup.find_checkout("acme/web", roots=[self.dir / "code"]))
        self.assertTrue(setup.matches("https://github.com/acme/api", "acme/api"))
        self.assertFalse(setup.matches("https://github.com/acme/api-v2.git", "acme/api"))

    def test_gate_teams_come_from_approval_checks_and_gate_like_owners(self):
        self.github(checks=["RSpec", "Require security-reviewers Approval"])
        teams = dict(setup.gate_teams("acme/api"))
        self.assertEqual(sorted(teams), ["billing-eng", "database-reviewers", "security-reviewers"])
        self.assertEqual(
            teams["security-reviewers"], "a check is named 'Require security-reviewers Approval'"
        )
        self.assertEqual(teams["billing-eng"], "owns 2 CODEOWNERS patterns")

    def test_the_config_it_writes_loads(self):
        answers = {
            "me": "ann",
            "name": "Ann",
            "timezone": "Europe/Berlin",
            "notify": True,
            "days": [0, 1, 2, 3, 4],
            "start_hour": 8,
            "end_hour": 18,
            "repos": [
                {
                    "name": "acme/api",
                    "checkout": "~/code/api",
                    "never_approve_owners": ["billing-eng"],
                    "checks": ['bin/lint "{files}"'],
                }
            ],
            "slack": {
                "token_path": "~/.claude/.credentials.json",
                "token_key": "plugin:slack:slack|x",
                "channels": {'eng "core"': "C1"},
            },
            "web": {"allowed_login": "", "public_url": ""},
        }
        text = setup.render(answers)
        self.assertIn("# These run code from the PR's branch", text)
        path = self.dir / "config.toml"
        path.write_text(text)
        (self.dir / "rubric.md").write_text("# r\n")
        cfg = config.load(path)
        self.assertEqual(
            (cfg.me, cfg.timezone, cfg.window.start_hour, cfg.web.mode),
            ("ann", "Europe/Berlin", 8, "local"),
        )
        self.assertEqual(cfg.repos[0].checks, ['bin/lint "{files}"'])
        self.assertIn("billing-eng", cfg.repos[0].lanes.never_approve_owners)
        self.assertEqual(tomllib.loads(text)["slack"]["channels"], {'eng "core"': "C1"})

    def test_setup_writes_config_and_rubric_and_signs_you_in(self):
        self.clone("api", "git@github.com:acme/api.git")
        self.github(checks=["Require security-reviewers Approval"], codeowners="")
        os.environ["PLUS1_CONFIG"] = str(self.dir / "home" / "config.toml")
        installed, opened = [], []
        units.install = lambda cfg, web=True: installed.append(cfg.me) or []
        host.open_url = opened.append
        saved_roots = setup.ROOTS
        setup.ROOTS = (str(self.dir / "code"),)
        state = self.dir / "state"
        try:
            prompt = Scripted(
                [
                    "",
                    "",  # login and name from GitHub
                    "",  # keep the suggested repo
                    "",  # no other repos
                    "",  # the clone it found
                    "",  # keep the suggested gate team
                    "n",  # no checks
                    "",
                    "",
                    "",
                    "",  # zone, hours, weekdays
                    "n",  # no desktop notifications
                    "n",  # no Slack
                    "",  # no Tailscale
                    "y",  # install
                ]
            )
            saved_state = config.load
            config.load = lambda path=None: _with_state(saved_state(path), state)
            try:
                self.assertEqual(setup.run(prompt, learn_first=False), 0)
            finally:
                config.load = saved_state
        finally:
            setup.ROOTS = saved_roots
        written = tomllib.loads((self.dir / "home" / "config.toml").read_text())
        self.assertEqual((written["me"], written["name"], written["notify"]), ("ann", "Ann", False))
        self.assertEqual(
            written["repos"][0]["lanes"]["never_approve_owners"], ["security-reviewers"]
        )
        self.assertEqual(written["repos"][0]["checkout"], str(self.dir / "code" / "api"))
        self.assertEqual(
            (self.dir / "home" / "rubric.md").read_text(), config.STARTER_RUBRIC.read_text()
        )
        self.assertEqual(installed, ["ann"])
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("file://"))

    def test_setup_offers_your_github_teams_and_writes_the_ones_you_keep(self):
        self.github()

        def teams():
            return [("acme/all-eng", 104), ("acme/web", 8), ("other/x", 1)]

        saved = gh.my_teams
        gh.my_teams = teams
        try:
            prompt = Scripted(["2"])
            picked = setup.ask_team(prompt, [{"name": "acme/api"}])
        finally:
            gh.my_teams = saved
        self.assertEqual(picked, ["acme/all-eng"])
        self.assertIn("  1. acme/web  (8 people)", prompt.said)
        self.assertNotIn("  3. other/x  (1 person)", prompt.said)
        answers = {
            "me": "ann",
            "name": "Ann",
            "timezone": "UTC",
            "notify": False,
            "days": [0],
            "start_hour": 7,
            "end_hour": 17,
            "repos": [{"name": "acme/api", "checkout": "/tmp/api"}],
            "team": picked,
        }
        self.assertEqual(tomllib.loads(setup.render(answers))["team"]["teams"], ["acme/all-eng"])
        answers["team"] = []
        self.assertNotIn("team", tomllib.loads(setup.render(answers)))

    def test_setup_goes_on_without_teams_when_github_cant_say(self):
        self.github()
        prompt = Scripted([])
        self.assertEqual(setup.ask_team(prompt, [{"name": "acme/api"}]), [])
        self.assertEqual(prompt.asked, [])
        self.assertIn("Settings, You", prompt.said[0])

    def test_a_repo_without_a_clone_can_be_skipped(self):
        self.github()
        saved_roots = setup.ROOTS
        setup.ROOTS = (str(self.dir / "nowhere"),)
        try:
            prompt = Scripted(["", "", "", "", "/tmp", ""])
            self.assertEqual(setup.run(prompt, learn_first=False), 1)
        finally:
            setup.ROOTS = saved_roots
        self.assertIn(
            "/tmp isn't a clone of acme/api. Clone it with: gh repo clone acme/api <dir>",
            prompt.said,
        )
        self.assertIn("Skipping acme/api for now.", prompt.said)
        self.assertFalse(Path(os.environ["PLUS1_CONFIG"]).with_name("never").exists())

    def test_doctor_says_how_to_fix_what_fails(self):
        cfg = config.load()
        cfg.state_dir = self.dir / "state"
        cfg.draft_command = str(self.dir / "gone.sh")
        self.github()
        saved = (doctor._run, units.active, doctor._web_up)
        doctor._run = lambda *cmd, timeout=30: (
            (1, "not logged in") if cmd[:2] == ("gh", "auth") else (0, "")
        )
        units.active = lambda: False
        doctor._web_up = lambda cfg: False
        said = []
        try:
            self.assertEqual(doctor.run(cfg, say=said.append), 1)
        finally:
            doctor._run, units.active, doctor._web_up = saved
        text = "\n".join(said)
        self.assertIn("FAIL GitHub CLI signed in\n     Run gh auth login.", text)
        self.assertIn("FAIL checks run on a timer\n     Run bin/plus1 install.", text)
        self.assertIn(f"FAIL drafting script at {self.dir / 'gone.sh'}", text)
        self.assertIn("ok   no checks have run yet", text)

    def test_unicode_in_a_name_or_path_writes_valid_toml(self):
        answers = {
            "me": "ann",
            "name": "Ann \U0001f680",
            "timezone": "UTC",
            "notify": False,
            "days": [0],
            "start_hour": 7,
            "end_hour": 17,
            "slack": None,
            "web": {},
            "repos": [
                {
                    "name": "acme/api",
                    "checkout": "~/code/\u00e9t\u00e9-api",
                    "never_approve_owners": [],
                }
            ],
        }
        loaded = tomllib.loads(setup.render(answers))
        self.assertEqual(
            (loaded["name"], loaded["repos"][0]["checkout"]),
            ("Ann \U0001f680", "~/code/\u00e9t\u00e9-api"),
        )

    def test_a_found_clone_is_skipped_only_on_purpose(self):
        self.clone("api", "git@github.com:acme/api.git")
        self.github(requested=())
        saved_roots = setup.ROOTS
        setup.ROOTS = (str(self.dir / "code"),)
        try:
            prompt = Scripted(["acme/api", "skip"])
            self.assertEqual(setup.ask_repos(prompt, "ann"), [])
            self.assertIn("Skipping acme/api for now.", prompt.said)
            prompt = Scripted(["acme/api", "", "", "n"])
            self.assertEqual([r["name"] for r in setup.ask_repos(prompt, "ann")], ["acme/api"])
        finally:
            setup.ROOTS = saved_roots

    def test_bad_zones_and_hours_are_asked_again(self):
        prompt = Scripted(["Mars/Olympus", "Europe/Berlin"])
        self.assertEqual(
            setup.ask_until(prompt, "Time zone", "UTC", setup._zone, "bad zone"), "Europe/Berlin"
        )
        self.assertEqual(prompt.said, ["bad zone"])
        prompt = Scripted(["25", "seven", "9"])
        self.assertEqual(setup.ask_until(prompt, "Start", "7", setup._hour, "bad hour"), 9)

    def test_a_failed_search_still_lets_you_type_repos(self):
        def down(args, input_text=None, timeout=90):
            raise gh.GhError("gh search prs failed: rate limited")

        gh.run = down
        prompt = Scripted(["acme/api", "skip"])
        self.assertEqual(setup.ask_repos(prompt, "ann"), [])
        self.assertTrue(any("Couldn't search GitHub" in line for line in prompt.said))

    def test_update_restarts_a_web_app_installed_alone(self):
        # No config to load, so this is the plain pull that stays available when a bad config blocks everything else.
        os.environ["PLUS1_CONFIG"] = str(self.dir / "missing.toml")
        restarted, saved = [], (units.installed, units.restart_web, units.install, doctor._run)
        units.installed = lambda web=False: web
        units.restart_web = lambda: restarted.append(True)
        units.install = lambda cfg, web=True: self.fail("the timer wasn't installed")
        doctor._run = lambda *cmd, timeout=30: (0, "")
        said = []
        try:
            self.assertEqual(doctor.update(say=said.append), 0)
        finally:
            units.installed, units.restart_web, units.install, doctor._run = saved
        self.assertEqual(restarted, [True])
        self.assertEqual(said[-1], f"now on Plus One {__version__}")

    def test_the_version_is_one_flag_away(self):
        from plus1 import cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--version"])
        self.assertEqual(out.getvalue().strip(), f"Plus One {__version__}")

    def test_an_existing_config_is_kept_unless_you_say_so(self):
        path = self.dir / "config.toml"
        path.write_text("old")
        setup.write(path, "new")
        self.assertEqual(path.read_text(), "new")
        self.assertEqual([p.read_text() for p in self.dir.glob("config.toml.*.bak")], ["old"])

    def test_numbered_picks_keep_what_you_name(self):
        prompt = Scripted(["2, 3"])
        self.assertEqual(
            prompt.pick("?", [("a", ""), ("b", ""), ("c", "")], chosen=["a"]), ["b", "c"]
        )
        self.assertEqual(Scripted([""]).pick("?", [("a", ""), ("b", "")], chosen=["a"]), ["a"])
        self.assertEqual(Scripted(["none"]).pick("?", [("a", "")], chosen=["a"]), [])

    def test_the_slack_key_comes_from_the_credentials_file(self):
        creds = self.dir / "creds.json"
        creds.write_text(json.dumps({"mcpOAuth": {"other|1": {}, "plugin:slack:slack|abc": {}}}))
        self.assertEqual(setup.slack_key(creds), "plugin:slack:slack|abc")
        self.assertEqual(setup.slack_key(self.dir / "missing.json"), "")


def _with_state(cfg, state):
    cfg.state_dir = state
    return cfg


if __name__ == "__main__":
    unittest.main()
