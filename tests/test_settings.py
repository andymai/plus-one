import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PLUS1_CONFIG"] = str(ROOT / "tests" / "config.toml")
from plus1 import __version__, actions, config, gh, settings, team, updates  # noqa: E402
from plus1.store import Store, now  # noqa: E402
from plus1.web import app as webapp  # noqa: E402
from plus1.web import routes  # noqa: E402

OWNER = "owner@example.com"
EXTRA = """
# My own note about limits
[limits]
max_spend_per_day_usd = 30.0  # keep it modest
"""


class Copied(unittest.TestCase):
    """A writable copy of the fixture config and rubric, so saving never touches the fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        text = (ROOT / "tests" / "config.toml").read_text()
        repo_lanes = '[repos.lanes]\nnever_approve_owners = ["db-reviewers", "health-eng", "money", "finance"]\n'
        assert repo_lanes in text
        unused = 'state_dir = "~/.local/state/plus1-tests-unused"'
        assert unused in text
        # The file names the same history folder the running copy uses, so nothing reads as waiting for a restart.
        text = text.replace(unused, f'state_dir = "{self.dir / "state"}"')
        (self.dir / "config.toml").write_text(text + EXTRA)
        shutil.copyfile(ROOT / "tests" / "rubric.md", self.dir / "rubric.md")
        self.cfg = config.load(self.dir / "config.toml")
        self.cfg.notify = False

    def tearDown(self):
        self.tmp.cleanup()

    def written(self):
        return (self.dir / "config.toml").read_text()


BUDGET = {"max_spend_per_day_usd": "30", "max_reviews_per_tick": "4"}
SKIPS = {"min_ready_minutes": "30", "handoff_working_days": "3", "max_map_lines": "10000"}
LIMITS = {"max_approvals_per_tick": "5", "max_approvals_per_day": "20", "max_diff_lines": "2500"}
REPO_FORM = {"owners": "db-reviewers\nhealth-eng\nmoney\nfinance", "paths": ""}


def clone(where, repo):
    """A git repo whose origin is repo on GitHub, like a real clone."""
    where.mkdir(parents=True)
    for args in (["init", "-q"], ["remote", "add", "origin", f"git@github.com:{repo}.git"]):
        subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True)
    return where


class SettingsTest(Copied):
    def parse(self, section, form, repo=None):
        return settings.parse(section, form, self.cfg, repo)

    def test_a_save_keeps_comments_and_layout_and_a_backup(self):
        before = self.written()
        fresh = settings.save(
            self.cfg, self.parse("budget", {**BUDGET, "max_spend_per_day_usd": "25"})
        )
        text = self.written()
        self.assertIn("# My own note about limits", text)
        self.assertIn("max_spend_per_day_usd = 25.0  # keep it modest", text)
        self.assertEqual(fresh.limits.max_spend_per_day_usd, 25.0)
        self.assertEqual((self.dir / "config.toml.bak").read_text(), before)

    def test_raising_a_cap_needs_the_typed_word_and_says_what_it_was(self):
        changes = self.parse("budget", {**BUDGET, "max_spend_per_day_usd": "60"})
        with self.assertRaisesRegex(
            settings.NeedsConfirm, "raises the daily spend cap to 60.0"
        ) as caught:
            settings.save(self.cfg, changes)
        item = caught.exception.items[0]
        self.assertEqual((item.label, item.was, item.now), ("Daily spend cap", "$30", "$60"))
        self.assertIn("max_spend_per_day_usd = 30.0", self.written())
        self.assertEqual(
            settings.save(self.cfg, changes, confirm="Loosen").limits.max_spend_per_day_usd, 60.0
        )

    def test_raising_an_approval_cap_needs_the_word_and_lowering_one_doesnt(self):
        more = self.parse("limits", {**LIMITS, "max_approvals_per_tick": "9"})
        with self.assertRaisesRegex(settings.NeedsConfirm, "raises approvals per check to 9"):
            settings.save(self.cfg, more)
        fewer = self.parse(
            "limits", {**LIMITS, "max_approvals_per_tick": "2", "max_approvals_per_day": "10"}
        )
        self.assertEqual(settings.save(self.cfg, fewer).limits.max_approvals_per_day, 10)

    def test_taking_out_a_never_approve_team_needs_the_typed_word_and_adding_one_doesnt(self):
        repo = self.cfg.repos[0].name
        keep = {**REPO_FORM, "owners": REPO_FORM["owners"] + "\n@acme/security"}
        fresh = settings.save(self.cfg, self.parse("repo", keep, repo))
        self.assertIn("security", fresh.repos[0].lanes.never_approve_owners)
        dropped = {**keep, "owners": "db-reviewers\nhealth-eng\nmoney\nsecurity"}
        with self.assertRaisesRegex(
            settings.NeedsConfirm, f"takes finance out of never-approve teams in {repo}"
        ) as caught:
            settings.save(fresh, settings.parse("repo", dropped, fresh, repo))
        self.assertEqual(caught.exception.items[0].removed, ["finance"])
        fresh = settings.save(fresh, settings.parse("repo", dropped, fresh, repo), confirm="loosen")
        self.assertNotIn("finance", fresh.repos[0].lanes.never_approve_owners)

    def test_approving_on_its_own_needs_the_typed_word_to_turn_on_and_nothing_to_turn_off(self):
        repo = self.cfg.repos[0].name
        settings.save(self.cfg, self.parse("repo", REPO_FORM, repo))
        self.assertNotIn("auto_approve", self.written())
        on = self.parse("repo", {**REPO_FORM, "auto_approve": "1"}, repo)
        with self.assertRaisesRegex(
            settings.NeedsConfirm, f"turns on approving clean PRs in {repo}"
        ):
            settings.save(self.cfg, on)
        fresh = settings.save(self.cfg, on, confirm="loosen")
        self.assertEqual(config.auto_repos(fresh), {repo})
        self.assertTrue(tomllib.loads(self.written())["repos"][0]["auto_approve"])
        fresh = settings.save(fresh, settings.parse("repo", REPO_FORM, fresh, repo))
        self.assertEqual(config.auto_repos(fresh), set())

    def test_holding_prs_in_your_code_needs_the_word_to_turn_off_and_nothing_to_turn_on(self):
        repo = self.cfg.repos[0].name
        self.assertTrue(config.holds_yours(self.cfg, repo))
        # A form from before the switch existed says nothing about it, so nothing changes.
        self.assertNotIn((repo, *settings.HOLD), self.parse("repo", REPO_FORM, repo))
        off = self.parse("repo", {**REPO_FORM, "hold_shown": "1"}, repo)
        with self.assertRaisesRegex(
            settings.NeedsConfirm, f"lets {repo} approve PRs in your code on its own"
        ):
            settings.save(self.cfg, off)
        fresh = settings.save(self.cfg, off, confirm="loosen")
        self.assertFalse(config.holds_yours(fresh, repo))
        self.assertFalse(tomllib.loads(self.written())["repos"][0]["hold_yours"])
        on = settings.parse(
            "repo", {**REPO_FORM, "hold_shown": "1", "hold_yours": "1"}, fresh, repo
        )
        self.assertTrue(config.holds_yours(settings.save(fresh, on), repo))

    def test_commit_emails_are_checked_and_saved_lowercase(self):
        you = {"name": self.cfg.name, "me": self.cfg.me}
        with self.assertRaisesRegex(settings.SettingsError, "That email") as caught:
            self.parse("you", {**you, "emails": "not-an-email"})
        self.assertEqual(caught.exception.field, "emails")
        fresh = settings.save(self.cfg, self.parse("you", {**you, "emails": "Me@Work.example"}))
        self.assertEqual(fresh.emails, ["me@work.example"])
        self.assertNotIn((None, "", "emails"), self.parse("you", you))
        fresh = settings.save(fresh, settings.parse("you", {**you, "emails": ""}, fresh))
        self.assertNotIn("emails", self.written())

    def test_teams_and_people_are_checked_lowercased_and_taken_out_when_emptied(self):
        you = {"name": self.cfg.name, "me": self.cfg.me}
        for field, bad in (("teams", "no-slash"), ("people", "acme/web"), ("others", "a b/c")):
            with self.assertRaises(settings.SettingsError) as caught:
                self.parse("you", {**you, field: bad})
            self.assertEqual(caught.exception.field, field)
        fresh = settings.save(
            self.cfg,
            self.parse(
                "you",
                {**you, "teams": "Acme/Web\nacme/web", "people": "Ann", "others": "acme/data"},
            ),
        )
        self.assertEqual(
            (fresh.team.teams, fresh.team.people, fresh.team.others),
            (["acme/web"], ["ann"], ["acme/data"]),
        )
        self.assertEqual(
            tomllib.loads(self.written())["team"],
            {"teams": ["acme/web"], "people": ["ann"], "others": ["acme/data"]},
        )
        self.assertNotIn((None, "team", "teams"), settings.parse("you", you, fresh))
        fresh = settings.save(
            fresh,
            settings.parse("you", {**you, "teams": "", "people": "", "others": ""}, fresh),
        )
        self.assertNotIn("team", tomllib.loads(self.written()))
        self.assertEqual(fresh.team.teams, [])

    def test_changing_the_team_needs_no_confirmation(self):
        you = {"name": self.cfg.name, "me": self.cfg.me, "people": "ann"}
        changes = self.parse("you", you)
        self.assertEqual(settings.loosens(self.cfg, changes), [])
        self.assertEqual(settings.key_name(None, "team", "people"), "team.people")

    def test_update_settings_are_checked_and_going_back_to_defaults_clears_them(self):
        form = {"mode": "ask", "channel": "releases"}
        for key in ("mode", "channel"):
            with self.assertRaises(settings.SettingsError) as caught:
                self.parse("updates", {**form, key: "sometimes"})
            self.assertEqual(caught.exception.field, key)
        self.assertEqual(self.cfg.updates.mode, "off")
        fresh = settings.save(self.cfg, self.parse("updates", form))
        self.assertEqual(fresh.updates.mode, "ask")
        self.assertEqual(tomllib.loads(self.written())["updates"]["mode"], "ask")
        changes = settings.changed(
            fresh, settings.parse("updates", {"mode": "automatic", "channel": "releases"}, fresh)
        )
        fresh = settings.save(fresh, changes, confirm="loosen")
        self.assertEqual(fresh.updates.mode, "automatic")
        self.assertNotIn("mode", tomllib.loads(self.written()).get("updates", {}))

    def test_updating_on_its_own_or_following_main_needs_the_word(self):
        changes = self.parse("updates", {"mode": "automatic", "channel": "main"})
        items = settings.loosens(self.cfg, changes)
        self.assertEqual(
            [(i.was, i.now) for i in items], [("Off", "Automatic"), ("Releases", "Main")]
        )
        with self.assertRaises(settings.NeedsConfirm):
            settings.save(self.cfg, changes)
        fresh = settings.save(self.cfg, changes, confirm="loosen")
        self.assertEqual((fresh.updates.mode, fresh.updates.channel), ("automatic", "main"))
        self.assertEqual(
            settings.loosens(
                fresh, settings.parse("updates", {"mode": "ask", "channel": "releases"}, fresh)
            ),
            [],
        )

    def test_default_lanes_stay_and_arent_copied_into_the_repo(self):
        repo = self.cfg.repos[0].name
        always = settings.inherited(self.cfg, repo)[("lanes", "never_approve_paths")]
        self.assertTrue(any("CLAUDE" in p for p in always))
        form = {**REPO_FORM, "paths": "\n".join([*always, r"^db/"])}
        fresh = settings.save(self.cfg, self.parse("repo", form, repo))
        self.assertEqual(settings.owned(fresh, repo)[("lanes", "never_approve_paths")], [r"^db/"])
        self.assertTrue(set(always) <= set(fresh.repos[0].lanes.never_approve_paths))

    def test_skip_labels_save_for_each_repo_the_form_names(self):
        (self.dir / "config.toml").write_text(
            self.written()
            + '\n[[repos]]\nname = "acme/api"\ncheckout = ""\n[repos.filters]\nhold_labels = ["frozen"]\n'
        )
        cfg = config.load(self.dir / "config.toml")
        web = settings.scoped("hold_labels", "globex/web")
        changes = settings.changed(
            cfg, settings.parse("skips", {**SKIPS, web: "needs-design\nhold"}, cfg)
        )
        self.assertEqual(
            changes, {("globex/web", "filters", "hold_labels"): ["needs-design", "hold"]}
        )
        fresh = settings.save(cfg, changes)
        self.assertEqual(
            settings.owned(fresh, "globex/web")[("filters", "hold_labels")], ["needs-design"]
        )
        self.assertEqual(settings.owned(fresh, "acme/api")[("filters", "hold_labels")], ["frozen"])
        api = settings.scoped("hold_labels", "acme/api")
        with self.assertRaisesRegex(
            settings.NeedsConfirm, "takes frozen out of skip labels in acme/api"
        ):
            settings.save(
                fresh, settings.parse("skips", {**SKIPS, web: "needs-design", api: ""}, fresh)
            )

    def test_the_page_asks_when_checks_stop(self):
        form = {
            "day_Mon": "1",
            "start_hour": "7",
            "end": "18",
            "jitter_minutes": "10",
            "timezone": "UTC",
        }
        self.assertEqual(self.parse("schedule", form)[(None, "window", "end_hour")], 17)
        late = self.parse("schedule", {**form, "end": "24"})
        self.assertEqual(late[(None, "window", "end_hour")], 23)

    def test_bad_answers_say_whats_wrong_and_where(self):
        base = {
            "day_Mon": "1",
            "start_hour": "7",
            "end": "17",
            "jitter_minutes": "10",
            "timezone": "UTC",
        }
        for change, problem, field in (
            ({"start_hour": "24"}, "start", "start_hour"),
            ({"end": "7"}, "has to come after the start", "end"),
            ({"end": "25"}, "end", "end"),
            ({"timezone": "Mars/Base"}, "time zone", "timezone"),
            ({"day_Mon": ""}, "at least one day", "days"),
        ):
            with self.assertRaisesRegex(settings.SettingsError, problem) as caught:
                self.parse("schedule", {k: v for k, v in {**base, **change}.items() if v})
            self.assertEqual(caught.exception.field, field)
        with self.assertRaisesRegex(settings.SettingsError, "valid pattern"):
            self.parse("repo", {"owners": "", "paths": "(unclosed"})
        with self.assertRaisesRegex(settings.SettingsError, "an ID like C0123456789"):
            self.parse("sources", {"enabled": "1", "channels": "eng=not-an-id"})
        with self.assertRaisesRegex(settings.SettingsError, "a name"):
            self.parse("sources", {"enabled": "1", "channels": "=C0123456789"})
        with self.assertRaisesRegex(settings.SettingsError, "Unknown section"):
            self.parse("nope", {})

    def test_amounts_must_be_real_numbers(self):
        for amount in ("nan", "inf", "-1", "forty"):
            with self.assertRaisesRegex(settings.SettingsError, "amount in dollars"):
                self.parse("budget", {**BUDGET, "max_spend_per_day_usd": amount})

    def test_two_saves_at_once_both_land(self):
        budget = self.parse("budget", {"max_spend_per_day_usd": "20", "max_reviews_per_tick": "3"})
        threads = [
            threading.Thread(target=settings.save, args=(self.cfg, budget)),
            threading.Thread(target=settings.save, args=(self.cfg, {(None, "", "notify"): True})),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        written = tomllib.loads(self.written())
        self.assertEqual((written["limits"]["max_reviews_per_tick"], written["notify"]), (3, True))
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_a_write_that_wouldnt_load_is_put_back(self):
        before = self.written()
        saved = config.load

        def broken(path=None):
            raise config.ConfigError("simulated")

        config.load = broken
        try:
            with self.assertRaisesRegex(settings.SettingsError, "nothing changed"):
                settings.save(self.cfg, {(None, "", "notify"): True})
        finally:
            config.load = saved
        self.assertEqual(self.written(), before)

    def models_form(self, **change):
        form = {"incremental_max_lines": "100"}
        for role in vars(self.cfg.models):
            m = getattr(self.cfg.models, role)
            form |= {
                f"{role}.model": m.model,
                f"{role}.effort": m.effort,
                f"{role}.budget_usd": f"{m.budget_usd:g}",
                f"{role}.timeout_s": str(m.timeout_s // 60),
            }
        return {**form, **change}

    def test_a_model_role_saves_as_its_own_table_and_a_bigger_budget_needs_the_word(self):
        form = self.models_form(**{"deep.budget_usd": "12", "deep.timeout_s": "30"})
        changes = settings.changed(self.cfg, self.parse("models", form))
        self.assertEqual(
            changes,
            {(None, "models.deep", "budget_usd"): 12.0, (None, "models.deep", "timeout_s"): 1800},
        )
        with self.assertRaisesRegex(
            settings.NeedsConfirm, "raises deep review budget to 12.0"
        ) as caught:
            settings.save(self.cfg, changes)
        item = caught.exception.items[0]
        self.assertEqual((item.was, item.now), ("$8", "$12"))
        fresh = settings.save(self.cfg, changes, confirm="loosen")
        self.assertEqual((fresh.models.deep.budget_usd, fresh.models.deep.timeout_s), (12.0, 1800))
        self.assertIn("[models.deep]", self.written())
        self.assertNotIn("[models]\n", self.written())
        back = settings.changed(fresh, settings.parse("models", self.models_form(), fresh))
        fresh = settings.save(fresh, back)
        self.assertNotIn("models", self.written())
        self.assertEqual(fresh.models.deep.budget_usd, 8.0)

    def test_model_fields_are_checked(self):
        for change, field in (
            ({"deep.effort": "extreme"}, "deep.effort"),
            ({"triage.model": "has space"}, "triage.model"),
            ({"verify.budget_usd": "0"}, "verify.budget_usd"),
            ({"learn.timeout_s": "500"}, "learn.timeout_s"),
        ):
            with self.assertRaises(settings.SettingsError) as caught:
                self.parse("models", self.models_form(**change))
            self.assertEqual(caught.exception.field, field)
        custom = self.parse("models", self.models_form(**{"deep.model": "claude-opus-5-5[1m]"}))
        self.assertEqual(custom[(None, "models.deep", "model")], "claude-opus-5-5[1m]")

    def test_a_value_set_back_to_its_default_leaves_the_file(self):
        settings.save(self.cfg, self.parse("budget", {**BUDGET, "max_reviews_per_tick": "6"}))
        self.assertIn("max_reviews_per_tick = 6", self.written())
        fresh = config.load(self.dir / "config.toml")
        settings.save(fresh, settings.changed(fresh, settings.parse("budget", BUDGET, fresh)))
        self.assertNotIn("max_reviews_per_tick", self.written())
        self.assertIn("max_spend_per_day_usd = 30.0  # keep it modest", self.written())

    def test_an_unchanged_form_changes_nothing(self):
        form = {f"day_{d}": "1" for d in ("Mon", "Tue", "Wed", "Thu", "Fri")}
        form |= {"start_hour": "7", "end": "18", "jitter_minutes": "0"}
        form |= {"timezone": "America/Los_Angeles"}
        self.assertEqual(settings.changed(self.cfg, self.parse("schedule", form)), {})
        self.assertEqual(settings.changed(self.cfg, self.parse("skips", SKIPS)), {})

    def test_every_repo_lists_and_blocking_markers(self):
        every = self.parse("every", {"owners": "@acme/security", "paths": r"^vendor/"})
        fresh = settings.save(self.cfg, settings.changed(self.cfg, every))
        self.assertIn("security", fresh.repos[0].lanes.never_approve_owners)
        self.assertIn(r"^vendor/", fresh.repos[0].lanes.never_approve_paths)
        fewer = settings.parse("every", {"owners": "", "paths": r"^vendor/"}, fresh)
        with self.assertRaisesRegex(
            settings.NeedsConfirm, "takes security out of never-approve teams for every repo"
        ):
            settings.save(fresh, settings.changed(fresh, fewer))
        with self.assertRaisesRegex(settings.SettingsError, "valid pattern"):
            self.parse("every", {"owners": "", "paths": "(open"})
        mine = settings.scoped("blocking_markers", "globex/web")
        form = {**SKIPS, "blocking_markers": "nack", mine: "hold up"}
        fresh = settings.save(fresh, settings.changed(fresh, settings.parse("skips", form, fresh)))
        markers = set(fresh.repos[0].filters.blocking_markers)
        self.assertTrue({"nack", "hold up", "(blocking)"} <= markers)

    def test_adding_an_approval_gate_needs_the_word(self):
        gates = settings.current(self.cfg, None, *settings.GATES)
        self.assertEqual(gates, [r"(?i)^require .+ approval$"])
        form = {**SKIPS, "approval_gates": "\n".join([*gates, "^Security sign-off$"])}
        more = settings.changed(self.cfg, self.parse("skips", form))
        with self.assertRaisesRegex(
            settings.NeedsConfirm, "stops failing checks named like"
        ) as caught:
            settings.save(self.cfg, more)
        self.assertEqual(caught.exception.items[0].added, ["^Security sign-off$"])
        fresh = settings.save(self.cfg, more, confirm="loosen")
        self.assertIn("^Security sign-off$", fresh.repos[0].filters.approval_gates)
        self.assertEqual(settings.gate_overrides(fresh), [])

    def test_who_you_are_and_slack_details(self):
        for form, field in (
            ({"name": "Andy", "me": "not a login"}, "me"),
            ({"name": " ", "me": "andymai"}, "name"),
        ):
            with self.assertRaises(settings.SettingsError) as caught:
                self.parse("you", form)
            self.assertEqual(caught.exception.field, field)
        you = self.parse("you", {"name": "A. Mai", "me": "andymai"})
        fresh = settings.save(self.cfg, settings.changed(self.cfg, you))
        self.assertEqual(fresh.name, "A. Mai")
        slack = {"enabled": "1", "channels": "storefront=C0123456789", "lookback_hours": "24"}
        slack |= {
            "token_path": "/nonexistent/credentials.json",
            "token_key": "plugin:slack:slack|test",
            "keychain_service": "Claude Code-credentials",
        }
        changes = settings.changed(fresh, settings.parse("sources", slack, fresh))
        self.assertEqual(changes, {(None, "slack", "lookback_hours"): 24})
        both = "/nonexistent/credentials.json\n/nonexistent/work/credentials.json"
        changes = settings.changed(
            fresh, settings.parse("sources", {**slack, "token_path": both}, fresh)
        )
        self.assertEqual(
            changes[(None, "slack", "token_path")],
            ["/nonexistent/credentials.json", "/nonexistent/work/credentials.json"],
        )

    def test_a_check_command_needs_run_and_shows_how_it_will_run(self):
        field = settings.scoped("checks", "globex/web")
        form = {"check_timeout_s": "300", field: "bin/packwerk check {files}\n'bin/lint' --fix"}
        changes = settings.changed(self.cfg, self.parse("repos", form))
        with self.assertRaisesRegex(settings.NeedsConfirm, "type run") as caught:
            settings.save(self.cfg, changes)
        argvs = [r.argv for r in caught.exception.runs]
        self.assertEqual(argvs, [["bin/packwerk", "check", "{files}"], ["bin/lint", "--fix"]])
        fresh = settings.save(self.cfg, changes, run="run")
        self.assertEqual(fresh.repos[0].checks, ["bin/packwerk check {files}", "'bin/lint' --fix"])
        fewer = settings.changed(
            fresh, settings.parse("repos", {"check_timeout_s": "300", field: ""}, fresh)
        )
        self.assertEqual(settings.save(fresh, fewer).repos[0].checks, [])
        with self.assertRaisesRegex(settings.SettingsError, "one command line"):
            self.parse("repos", {"check_timeout_s": "300", field: "bin/x 'unclosed"})

    def test_a_drafting_script_or_login_folder_needs_run(self):
        script = self.dir / "draft.sh"
        script.write_text("#!/bin/sh\n")
        form = self.models_form(
            draft_mode="script", draft_command=str(script), claude_config_dir=""
        )
        changes = settings.changed(self.cfg, self.parse("models", form))
        self.assertEqual(changes, {(None, "", "draft_command"): str(script)})
        with self.assertRaises(settings.NeedsConfirm) as caught:
            settings.save(self.cfg, changes)
        self.assertEqual(caught.exception.runs[0].argv[:2], ["bash", str(script)])
        fresh = settings.save(self.cfg, changes, run="RUN")
        self.assertEqual(fresh.draft_command, str(script))
        back = settings.changed(
            fresh, settings.parse("models", {**form, "draft_mode": "claude"}, fresh)
        )
        self.assertEqual(settings.save(fresh, back).draft_command, "")
        for change, field in (
            ({"draft_command": str(self.dir / "missing.sh")}, "draft_command"),
            ({"claude_config_dir": str(self.dir / "nowhere")}, "claude_config_dir"),
        ):
            with self.assertRaises(settings.SettingsError) as caught:
                self.parse("models", {**form, **change})
            self.assertEqual(caught.exception.field, field)
        folder = settings.changed(
            self.cfg,
            self.parse(
                "models", {**form, "draft_mode": "claude", "claude_config_dir": str(self.dir)}
            ),
        )
        with self.assertRaises(settings.NeedsConfirm) as caught:
            settings.save(self.cfg, folder)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.dir}", caught.exception.runs[0].argv)

    def web_form(self, **change):
        # The page fills these from config.toml, which can be ahead of the running app.
        web = settings.ondisk(self.cfg).web
        form = {"mode": web.mode, "allowed_login": web.allowed_login, "socket": web.socket}
        return {**form, "port": str(web.port), "public_url": web.public_url, **change}

    def test_web_settings_wait_for_a_restart_and_risky_ones_are_typed_twice(self):
        self.assertEqual(settings.changed(self.cfg, self.parse("web", self.web_form())), {})
        home = str(Path.home())
        socket = self.cfg.web.socket
        if socket.startswith(home):
            tilde = self.web_form(socket="~" + socket[len(home) :])
            self.assertEqual(settings.changed(self.cfg, self.parse("web", tilde)), {})
        login = settings.changed(
            self.cfg, self.parse("web", self.web_form(allowed_login="me@github"))
        )
        with self.assertRaisesRegex(
            settings.NeedsConfirm, "type the new allowed login again"
        ) as caught:
            settings.save(self.cfg, login, again={"allowed_login": "me@githu"})
        self.assertEqual(caught.exception.retypes[0].value, "me@github")
        fresh = settings.save(self.cfg, login, again={"allowed_login": "me@github"})
        self.assertEqual(
            settings.restart_pending(self.cfg),
            {"Allowed login": ("owner@example.com", "me@github")},
        )
        self.assertEqual(settings.current(fresh, None, "web", "allowed_login"), "me@github")
        port = settings.changed(self.cfg, self.parse("web", self.web_form(port="9000")))
        self.assertEqual(list(port), [(None, "web", "port")])
        with self.assertRaises(settings.SettingsError) as caught:
            self.parse("web", self.web_form(allowed_login=""))
        self.assertEqual(caught.exception.field, "allowed_login")
        with self.assertRaises(settings.SettingsError) as caught:
            self.parse("web", self.web_form(mode="carrier-pigeon"))
        self.assertEqual(caught.exception.field, "mode")
        moved = settings.changed(
            self.cfg, self.parse("files", {"state_dir": str(self.dir / "elsewhere")})
        )
        settings.save(self.cfg, moved)
        self.assertIn("History folder", settings.restart_pending(self.cfg))

    def test_a_repo_is_added_with_its_clone_and_removed_with_its_entry(self):
        api = clone(self.dir / "api", "acme/api")
        fresh = settings.add_repo(self.cfg, "acme/api", str(api))
        self.assertEqual([r.name for r in fresh.repos], ["globex/web", "acme/api"])
        self.assertFalse(fresh.repos[1].auto_approve)
        for name, path, problem in (
            ("acme/api", str(api), "already set up"),
            ("not a repo", str(api), "owner/repo"),
            ("acme/other", str(api), "origin is git@github.com:acme/api.git, not acme/other"),
            ("acme/other", str(self.dir), "no git clone"),
        ):
            with self.assertRaisesRegex(settings.SettingsError, problem):
                settings.add_repo(fresh, name, path)
        fresh = settings.remove_repo(fresh, "acme/api")
        self.assertEqual([r.name for r in fresh.repos], ["globex/web"])
        with self.assertRaisesRegex(settings.SettingsError, "at least one repo"):
            settings.remove_repo(fresh, "globex/web")
        with self.assertRaisesRegex(settings.SettingsError, "isn't in your config"):
            settings.remove_repo(fresh, "acme/gone")

    def test_a_repos_clone_peers_and_tracker_are_checked_and_saved(self):
        web = clone(self.dir / "web", "globex/web")
        peer = clone(self.dir / "ios", "globex/ios")
        base = {"check_timeout_s": "300"}
        form = {**base, "checkout@globex/web": str(web), "peers@globex/web": str(peer)}
        form["issues@globex/web"] = "https://linear.app/acme/issue"
        changes = settings.changed(self.cfg, self.parse("repos", form))
        fresh = settings.save(self.cfg, changes)
        self.assertEqual((fresh.repos[0].checkout, fresh.repos[0].peers), (str(web), [str(peer)]))
        self.assertEqual(fresh.repos[0].issues, "https://linear.app/acme/issue")
        self.assertEqual(settings.changed(fresh, settings.parse("repos", form, fresh)), {})
        for change, problem in (
            ({"checkout@globex/web": str(peer)}, "not globex/web"),
            ({"peers@globex/web": str(self.dir / "nowhere")}, "no folder"),
            ({"issues@globex/web": "linear"}, "doesn't look right"),
        ):
            with self.assertRaisesRegex(settings.SettingsError, problem):
                settings.parse("repos", {**form, **change}, fresh)

    def test_the_rubric_outline_marks_what_learn_keeps_and_what_drafts_read(self):
        sections = settings.outline(self.cfg.rubric_path.read_text())
        by_name = {s["heading"].split(" (")[0]: s for s in sections}
        self.assertTrue(by_name["Verdicts"]["locked"])
        self.assertTrue(by_name["Never auto-approve lanes"]["locked"])
        self.assertFalse(by_name["Review procedure"]["locked"])
        self.assertTrue(by_name["Comment format"]["voice"])
        self.assertTrue(all(s["words"] > 0 for s in sections))

    def test_a_logged_change_reads_as_before_and_after(self):
        from plus1.web import words

        self.assertEqual(
            words.setting_change("limits.max_spend_per_day_usd", 30.0, 25.0),
            {"label": "Daily spend cap", "was": "$30", "now": "$25"},
        )
        self.assertEqual(
            words.setting_change("window.end_hour", 17, 20),
            {"label": "End", "was": "6 PM", "now": "9 PM"},
        )
        self.assertEqual(
            words.setting_change(
                "globex/web:lanes.never_approve_owners", ["money", "finance"], ["money", "health"]
            ),
            {
                "label": "Never-approve teams in globex/web",
                "added": ["health"],
                "removed": ["finance"],
            },
        )
        self.assertEqual(words.setting_change("notify", words.UNKNOWN, True)["was"], None)
        self.assertEqual(
            words.setting_change("models.deep.budget_usd", 8.0, 12.0),
            {"label": "Deep review budget", "was": "$8", "now": "$12"},
        )
        self.assertEqual(words.setting_change("models.verify.timeout_s", 600, 900)["now"], "15 min")
        self.assertEqual(words.days_name([0, 2, 4]), "Mon, Wed, Fri")
        self.assertEqual(words.days_name([5, 6]), "Weekends")


class SettingsPageTest(Copied):
    def setUp(self):
        super().setUp()
        self.store = Store(self.cfg)
        handler = webapp.make_handler(self.cfg)
        self.server = webapp.TcpServer(("127.0.0.1", 0), handler)
        self.app = handler.app
        self.port = self.server.server_address[1]
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self._spawn = actions.spawn

    def tearDown(self):
        actions.spawn = self._spawn
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        super().tearDown()

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Tailscale-User-Login": OWNER}
        if cookie:
            headers["Cookie"] = cookie
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp, text

    def post(self, path, **fields):
        resp, page = self.request("GET", "/settings")
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        return self.request("POST", path, body={"csrf": token, **fields}, cookie=cookie)

    def test_the_page_shows_every_section(self):
        resp, page = self.request("GET", "/settings")
        self.assertEqual(resp.status, 200)
        for heading in (
            "Checking",
            "Schedule",
            "Sources",
            "Waits and skips",
            "Budget",
            "Approving on its own",
            self.cfg.repos[0].name,
            "Every repo",
            "Limits",
            "Rubric",
            "Models",
            "Repos",
            "Notifications",
            "Web app",
            "Recent changes",
        ):
            self.assertIn(f">{heading}<", page)
        self.assertIn('src="/static/settings.js"', page)
        self.assertIn("Readiness", page)
        self.assertIn(">Hold PRs in your code<", page)
        self.assertIn(">Commit emails<", page)
        for label in ("Your teams", "People", "Other teams"):
            self.assertIn(f">{label}<", page)
        self.assertIn('href="/settings"', self.request("GET", "/insights")[1])

    def set_update_state(self, **state):
        self.store.set_meta(updates.STATE, json.dumps({"checked_at": now(), **state}))

    def test_the_updates_section_shows_where_things_stand(self):
        _, page = self.request("GET", "/settings")
        self.assertIn('id="updates"', page)
        self.assertIn("Not checked yet", page)
        self.assertIn('action="/settings/updates/check"', page)
        self.assertIn(f"Plus One {__version__}", page)
        self.assertNotIn('action="/settings/updates/install"', page)
        self.assertNotIn('action="/settings/updates/rollback"', page)
        self.assertIn('href="/whats-new"', page)

    def test_a_waiting_version_can_be_installed_and_an_earlier_one_returned_to(self):
        self.set_update_state(status="available", latest="9.9.9", latest_sha="abc", reason="")
        self.store.set_meta(updates.PREVIOUS, json.dumps({"sha": "def", "version": "1.2.3"}))
        _, page = self.request("GET", "/settings")
        self.assertIn("9.9.9 is ready.", page)
        self.assertIn("Install 9.9.9", page)
        self.assertIn("Roll back to 1.2.3", page)

    def test_a_blocked_update_says_why_and_a_skipped_one_says_so(self):
        self.set_update_state(
            status="blocked", latest="9.9.9", reason="this clone is on my-work, not main"
        )
        _, page = self.request("GET", "/settings")
        self.assertIn("9.9.9 is waiting, but this clone is on my-work, not main.", page)
        self.assertNotIn("Install 9.9.9", page)
        self.set_update_state(status="skipped", latest="9.9.9", reason="")
        _, page = self.request("GET", "/settings")
        self.assertIn("Skipping 9.9.9", page)
        self.assertIn("Install 9.9.9", page)

    def test_history_shows_why_a_version_went_back(self):
        updates._record(self.store, "9.9.9", "rolled back", "it failed its smoke test")
        _, page = self.request("GET", "/settings")
        self.assertIn("9.9.9 rolled back", page)
        self.assertIn("it failed its smoke test", page)

    def test_install_and_roll_back_run_in_the_background(self):
        spawned = []
        actions.spawn = lambda cfg, name, *args: spawned.append((name, args))
        resp, _ = self.post("/settings/updates/install")
        self.assertEqual(resp.status, 303)
        self.assertIn("settings", str(resp.getheader("Location")))
        self.post("/settings/updates/rollback")
        self.assertEqual(spawned, [("update", ("update",)), ("update", ("update", "--rollback"))])

    def test_a_job_that_wont_start_says_so(self):
        def broken(cfg, name, *args):
            raise actions.ActionError("no systemd")

        actions.spawn = broken
        resp, _ = self.post("/settings/updates/install")
        self.assertIn("no+systemd", str(resp.getheader("Location")))

    def test_check_now_saves_what_it_finds_without_holding_the_page(self):
        checked, release = [], threading.Event()

        def check(cfg, store):
            checked.append(cfg.me)
            release.wait(10)

        saved = updates.check, routes.UPDATE_CHECK_WAIT
        updates.check, routes.UPDATE_CHECK_WAIT = check, 0.01
        try:
            resp, _ = self.post("/settings/updates/check")
            self.assertEqual(resp.status, 303)
            self.assertIn("Still+asking", str(resp.getheader("Location")))
        finally:
            release.set()
            for thread in threading.enumerate():
                if thread.name == "plus1-update-check":
                    thread.join(5)
            updates.check, routes.UPDATE_CHECK_WAIT = saved
        self.assertEqual(checked, [self.cfg.me])

    def test_the_saved_mode_applies_from_the_form(self):
        resp, _ = self.post("/settings/updates", mode="ask", channel="releases")
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.app.cfg.updates.mode, "ask")
        resp, page = self.post("/settings/updates", mode="automatic", channel="releases")
        self.assertEqual(resp.status, 422)
        self.assertIn("loosen", page)
        self.assertEqual(self.app.cfg.updates.mode, "ask")
        resp, _ = self.post(
            "/settings/updates", mode="automatic", channel="releases", confirm="loosen"
        )
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.app.cfg.updates.mode, "automatic")

    def test_whats_new_and_the_sidebar_read_from_this_copy(self):
        _, page = self.request("GET", "/settings")
        self.assertIn('class="sb-version" href="/whats-new"', page)
        self.assertNotIn("github.com/andymai/plus-one/releases", page)
        resp, page = self.request("GET", "/whats-new")
        self.assertEqual(resp.status, 200)
        self.assertIn("<h1>What&#39;s new</h1>".replace("&#39;", "'"), page)
        self.assertIn('<meta name="plus1-build"', page)

    def test_the_sidebar_says_when_an_update_needs_a_click(self):
        self.app.cfg.updates.mode = "ask"
        self.set_update_state(status="available", latest="9.9.9", latest_sha="abc", reason="")
        _, page = self.request("GET", "/insights")
        self.assertIn('class="sb-update"', page)
        self.assertIn("Update available", page)

    def test_the_notice_that_it_updates_itself_shows_once(self):
        self.assertNotIn("is-sticky", self.request("GET", "/insights")[1])
        self.app.cfg.updates.mode = "automatic"
        self.assertIn("is-sticky", self.request("GET", "/insights")[1])
        self.assertNotIn("is-sticky", self.request("GET", "/insights")[1])

    def test_a_sidebar_poll_cant_use_up_the_notice(self):
        self.app.cfg.updates.mode = "automatic"
        resp, _ = self.request("GET", "/partials/status")
        self.assertEqual(resp.status, 200)
        self.assertIn("is-sticky", self.request("GET", "/insights")[1])

    def test_health_says_which_build_is_answering(self):
        _, body = self.request("GET", "/healthz")
        self.assertEqual(body, f"ok {updates.build()}")

    def test_saving_a_team_reads_it_and_the_row_says_what_it_found(self):
        read, saved = [], gh.team_members
        gh.team_members = lambda name: read.append(name) or ["ann", "bo"]
        try:
            resp, _ = self.post(
                "/settings/you",
                name=self.cfg.name,
                me=self.cfg.me,
                teams="Acme/Web",
                people="dee",
                others="acme/data",
            )
        finally:
            gh.team_members = saved
        self.assertEqual(resp.status, 303)
        self.assertEqual(read, ["acme/web", "acme/data"])
        _, page = self.request("GET", "/settings")
        self.assertIn("acme/web: 2 people", page)
        self.assertIn("acme/data: 2 people", page)
        self.assertEqual(team.known(self.app.cfg, self.store), ({"ann", "bo", "dee"}, set()))
        log = self.store.query("SELECT payload FROM actions WHERE action = 'settings'")
        self.assertIn("team.people", log[-1]["payload"])

    def test_a_slow_github_only_holds_a_save_briefly_and_the_read_finishes_after(self):
        release = threading.Event()

        def slow(name):
            release.wait(10)
            return ["ann"]

        saved = gh.team_members, routes.TEAM_READ_WAIT
        gh.team_members, routes.TEAM_READ_WAIT = slow, 0.01
        try:
            resp, _ = self.post(
                "/settings/you", name=self.cfg.name, me=self.cfg.me, teams="acme/web"
            )
            self.assertEqual(resp.status, 303)
            self.assertIn("acme/web: not read yet", self.request("GET", "/settings")[1])
            release.set()
            for thread in threading.enumerate():
                if thread.name == "plus1-team-read":
                    thread.join(5)
        finally:
            release.set()
            gh.team_members, routes.TEAM_READ_WAIT = saved
        self.assertIn("acme/web: 1 person", self.request("GET", "/settings")[1])

    def test_a_team_it_cant_read_says_why_on_its_row(self):
        saved = gh.team_members

        def broken(name):
            raise gh.GhError("gh api failed: 404")

        gh.team_members = broken
        try:
            self.post("/settings/you", name=self.cfg.name, me=self.cfg.me, teams="acme/gone")
        finally:
            gh.team_members = saved
        _, page = self.request("GET", "/settings")
        self.assertIn("Couldn't read acme/gone", page)

    def test_a_refused_team_comes_back_with_what_was_typed(self):
        resp, page = self.post(
            "/settings/you", name=self.cfg.name, me=self.cfg.me, teams="no-slash"
        )
        self.assertEqual(resp.status, 422)
        self.assertIn("That team doesn", page)
        self.assertIn("no-slash", page)

    def test_suggestions_leave_out_what_the_lists_already_have(self):
        cfg = self.app.cfg
        cfg.team.teams, cfg.team.others = ["globex/web"], ["globex/taken"]
        saved = gh.my_teams, gh.org_teams
        gh.my_teams = lambda: [("globex/web", 8), ("globex/core", 1), ("other/x", 3)]
        gh.org_teams = lambda org: [
            "globex/web",
            "globex/core",
            "globex/data",
            "globex/taken",
        ]
        try:
            _, mine = self.request("GET", "/settings/team/suggest?list=teams")
            _, others = self.request("GET", "/settings/team/suggest?list=others")
        finally:
            gh.my_teams, gh.org_teams = saved
        self.assertIn('data-chip-add="globex/core" data-chip-list="teams"', mine)
        self.assertIn("1 person", mine)
        self.assertNotIn('data-chip-add="globex/web"', mine)
        self.assertNotIn("other/x", mine)
        self.assertIn('data-chip-add="globex/data" data-chip-list="others"', others)
        for gone in ("globex/web", "globex/core", "globex/taken"):
            self.assertNotIn(f'data-chip-add="{gone}"', others)

    def test_suggestions_say_so_when_github_cant_answer(self):
        saved = gh.my_teams

        def broken():
            raise gh.GhError("gh api failed")

        gh.my_teams = broken
        try:
            resp, page = self.request("GET", "/settings/team/suggest")
        finally:
            gh.my_teams = saved
        self.assertEqual(resp.status, 200)
        self.assertIn("ask GitHub for teams", page)

    def test_the_phone_tab_bar_has_settings_and_marks_its_pages(self):
        def lit(path):
            tabbar = re.search(
                r'<nav class="tabbar".*?</nav>', self.request("GET", path)[1], re.S
            ).group(0)
            return re.findall(r'<a href="([^"]+)" class="on"', tabbar)

        self.assertEqual(lit("/settings"), ["/settings"])
        self.assertEqual(lit("/settings/rubric"), ["/settings"])
        self.assertEqual(lit("/proposals"), ["/settings"])
        self.assertEqual(lit("/insights"), ["/insights"])

    def test_saving_a_section_writes_the_file_and_the_running_app_uses_it(self):
        resp, _ = self.post(
            "/settings/schedule",
            day_Mon="1",
            day_Tue="1",
            start_hour="8",
            end="17",
            jitter_minutes="5",
            timezone="Europe/Berlin",
        )
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.app.cfg.window.end_hour, 16)
        written = tomllib.loads(self.written())
        self.assertEqual(
            (written["window"]["start_hour"], written["window"]["days"], written["timezone"]),
            (8, [0, 1], "Europe/Berlin"),
        )
        self.assertEqual(
            (self.app.cfg.window.start_hour, self.app.cfg.timezone), (8, "Europe/Berlin")
        )
        self.assertEqual(self.app.cfg.state_dir, self.dir / "state")

    def test_loosening_asks_for_the_word_shows_before_and_after_and_keeps_what_you_typed(self):
        fields = {"max_spend_per_day_usd": "80", "max_reviews_per_tick": "4"}
        resp, page = self.post("/settings/budget", **fields)
        self.assertEqual(resp.status, 422)
        self.assertIn('name="confirm"', page)
        self.assertIn('value="80"', page)
        self.assertRegex(page, r'class="was">\$30<.*class="now">\$80<')
        resp, _ = self.post("/settings/budget", confirm="loosen", **fields)
        self.assertEqual(resp.status, 303)
        self.assertEqual(self.app.cfg.limits.max_spend_per_day_usd, 80.0)

    def test_the_new_sections_save_from_the_page(self):
        resp, _ = self.post("/settings/rubric-limits", auto_proposals_per_day="5")
        self.assertEqual((resp.status, self.app.cfg.limits.auto_proposals_per_day), (303, 5))
        resp, _ = self.post("/settings/you", name="Andy", me="andymai")
        self.assertIn("Nothing+changed", resp.getheader("Location") or "")
        _, page = self.request("GET", "/settings")
        self.assertIn('name="deep.effort"', page)
        self.assertIn("<option selected>high</option>", page)
        self.assertIn('action="/settings/every#every"', page)
        self.assertIn('name="blocking_markers@globex/web"', page)
        gates = r"(?i)^require .+ approval$" + "\n^Security$"
        resp, page = self.post("/settings/skips", **SKIPS, approval_gates=gates)
        self.assertEqual(resp.status, 422)
        self.assertIn('<span class="add-text">+ ^Security$</span>', page)

    def test_the_page_shows_what_would_run_and_offers_a_restart(self):
        field = settings.scoped("checks", "globex/web")
        resp, page = self.post(
            "/settings/repos", check_timeout_s="300", **{field: "bin/lint {files}"}
        )
        self.assertEqual(resp.status, 422)
        self.assertIn("This runs code on this machine", page)
        self.assertIn(
            '<code class="is-files" title="Becomes the changed files that exist">{files}</code>',
            page,
        )
        resp, _ = self.post(
            "/settings/repos",
            check_timeout_s="300",
            confirm_run="run",
            **{field: "bin/lint {files}"},
        )
        self.assertEqual((resp.status, self.app.cfg.repos[0].checks), (303, ["bin/lint {files}"]))
        web = {
            "mode": "tailscale",
            "allowed_login": OWNER,
            "socket": self.cfg.web.socket,
            "port": "9000",
        }
        web["public_url"] = self.cfg.web.public_url
        resp, _ = self.post("/settings/web", **web)
        self.assertEqual(resp.status, 303)
        _, page = self.request("GET", "/settings")
        self.assertIn("Saved, and waiting for the web app to restart: port.", page)
        started = []
        actions.spawn = lambda cfg, name, *args: started.append(args) or "unit"
        resp, _ = self.post("/settings/restart")
        self.assertEqual((resp.status, started), (303, [("restart-web",)]))
        resp, page = self.post("/settings/web", **{**web, "mode": "local"})
        self.assertEqual(resp.status, 422)
        self.assertIn('name="again_mode" data-word="local" data-exact', page)

    def test_repos_are_added_suggested_and_removed_from_the_page(self):
        from plus1 import setup

        api = clone(self.dir / "api", "acme/api")
        saved = setup.suggest_repos, setup.find_checkout
        setup.suggest_repos = lambda limit=8: ["globex/web", "acme/api", "acme/docs"]
        setup.find_checkout = lambda name: api if name == "acme/api" else None
        try:
            _, found = self.request("GET", "/settings/repos/suggest")
        finally:
            setup.suggest_repos, setup.find_checkout = saved
        self.assertIn('<input type="hidden" name="checkout" value="' + str(api) + '">', found)
        self.assertIn("No clone found in your usual code folders", found)
        self.assertNotIn(">globex/web<", found)
        setup.suggest_repos = lambda limit=8: (_ for _ in ()).throw(RuntimeError("gh failed"))
        try:
            _, failed = self.request("GET", "/settings/repos/suggest")
        finally:
            setup.suggest_repos = saved[0]
        self.assertIn("Couldn't ask GitHub for repos you review", failed)
        resp, _ = self.post("/settings/repos/add", name="acme/api", checkout=str(api))
        self.assertIn("Added+acme%2Fapi", resp.getheader("Location") or "")
        self.assertEqual([r.name for r in self.app.cfg.repos], ["globex/web", "acme/api"])
        resp, _ = self.post("/settings/repo/1/remove", repo="acme/other")
        self.assertIn("error=", resp.getheader("Location") or "")
        resp, _ = self.post("/settings/repo/1/remove", repo="acme/api")
        self.assertIn("Removed+acme%2Fapi", resp.getheader("Location") or "")
        self.assertEqual([r.name for r in self.app.cfg.repos], ["globex/web"])
        _, page = self.request("GET", "/settings")
        self.assertIn("<b>Repos</b>", page)
        resp, _ = self.post("/settings/repos/add", name="acme/api", checkout=str(self.dir))
        self.assertIn("no+git+clone", resp.getheader("Location") or "")

    def test_devices_live_under_notifications(self):
        resp, page = self.request("GET", "/settings")
        self.assertIn('id="enable-push"', page)
        self.assertIn("No devices yet.", page)
        resp, _ = self.request("GET", "/devices")
        self.assertEqual(resp.getheader("Location"), "/settings#notifications")

    def test_a_save_lands_back_on_its_section_with_a_note(self):
        resp, _ = self.post("/settings/notifications", notify="1")
        self.assertEqual(resp.status, 303)
        location = resp.getheader("Location")
        self.assertTrue(location.startswith("/settings?msg="), location)
        self.assertTrue(location.endswith("#notifications"), location)
        self.assertTrue(self.app.cfg.notify)

    def test_a_mistake_shows_under_its_field(self):
        resp, page = self.post(
            "/settings/schedule",
            day_Mon="1",
            start_hour="9",
            end="8",
            jitter_minutes="0",
            timezone="UTC",
        )
        self.assertEqual(resp.status, 422)
        self.assertRegex(
            page, r'(?s)has-error[^>]*data-keys="hours[^"]*">.*?has to come after the start'
        )

    def test_skip_labels_save_from_their_own_section(self):
        resp, _ = self.post(
            "/settings/skips",
            **SKIPS,
            **{settings.scoped("hold_labels", "globex/web"): "needs-design"},
        )
        self.assertEqual(resp.status, 303)
        self.assertIn("needs-design", self.app.cfg.repos[0].filters.hold_labels)

    def test_a_repo_form_from_before_the_repos_changed_is_refused(self):
        resp, page = self.post("/settings/repo/0", repo="acme/other", owners="", paths="")
        self.assertEqual(resp.status, 422)
        self.assertIn("Your repos changed since this page loaded", page)
        self.assertEqual(len(self.app.cfg.repos[0].lanes.never_approve_owners), 4)

    def test_recent_changes_show_what_each_save_replaced(self):
        self.post("/settings/budget", max_spend_per_day_usd="25", max_reviews_per_tick="4")
        _, page = self.request("GET", "/settings")
        self.assertRegex(page, r"<b>Daily spend cap</b> <s>\$30</s> → \$25")
        self.assertNotIn("Reviews per check</b>", page)

    def test_each_slack_channel_shows_how_many_prs_it_surfaced(self):
        from plus1.web import queries

        run = self.store.start_run("timer", "shadow")
        self.store.observe(run, 101, "reviewed", "", source="slack", channels=["storefront"])
        self.store.observe(run, 101, "skipped", "", source="slack", channels=["storefront"])
        self.store.observe(run, 102, "reviewed", "", source="slack", channels=["storefront", "eng"])
        self.assertEqual(queries.channel_counts(self.store), {"storefront": 2, "eng": 1})
        _, page = self.request("GET", "/settings")
        self.assertIn('data-count="2">2 PRs<', page)

    def test_an_edit_made_by_hand_shows_up_without_a_restart(self):
        time.sleep(0.01)
        (self.dir / "config.toml").write_text(
            self.written().replace("max_spend_per_day_usd = 30.0", "max_spend_per_day_usd = 12.0")
        )
        self.request("GET", "/settings")
        self.assertEqual(self.app.cfg.limits.max_spend_per_day_usd, 12.0)

    def test_the_rubric_is_previewed_then_saved_as_a_change_you_can_revert(self):
        before = self.cfg.rubric_path.read_text()
        edited = before + "\n- Ask for a test when a bug fix has none.\n"
        resp, page = self.post("/settings/rubric", text=edited, preview="1")
        self.assertIn("+- Ask for a test when a bug fix has none.", page)
        self.assertEqual(self.cfg.rubric_path.read_text(), before)
        resp, _ = self.post("/settings/rubric", text=edited, save="1")
        self.assertEqual(resp.status, 303)
        proposal = self.store.query("SELECT * FROM proposals ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((proposal["kind"], proposal["status"]), ("manual", "accepted"))
        self.assertIn("Ask for a test", self.cfg.rubric_path.read_text())
        resp, page = self.post(
            "/settings/rubric",
            text=edited.replace("## Comment format (drafts)", "## Voice"),
            save="1",
        )
        self.assertIn("Keep the", page)

    def test_a_hand_edit_that_doesnt_parse_keeps_the_last_good_settings(self):
        time.sleep(0.01)
        (self.dir / "config.toml").write_text(self.written() + "\n[broken\n")
        resp, page = self.request("GET", "/settings")
        self.assertEqual(resp.status, 200)
        self.assertIn("doesn't parse right now", page)
        self.assertEqual(self.app.cfg.limits.max_spend_per_day_usd, 30.0)
        resp, page = self.post("/settings/notifications", notify="1")
        self.assertEqual(resp.status, 422)
        self.assertIn("nothing was saved", page)

    def test_a_repo_added_by_hand_gets_an_id_right_away(self):
        from plus1 import prs

        time.sleep(0.01)
        (self.dir / "config.toml").write_text(
            self.written() + '\n[[repos]]\nname = "acme/api"\ncheckout = "/tmp"\n'
        )
        self.request("GET", "/settings")
        self.assertEqual(prs.pid("acme/api", 7), prs.BASE + 7)

    def test_an_old_rubric_editor_cant_undo_a_newer_change(self):
        _, page = self.request("GET", "/settings/rubric")
        base = re.search(r'name="base" value="([0-9a-f]+)"', page).group(1)
        self.cfg.rubric_path.write_text(
            self.cfg.rubric_path.read_text() + "\n- A rule added elsewhere.\n"
        )
        _resp, page = self.post(
            "/settings/rubric",
            text="# Rubric\n\n## Comment format (drafts)\n- short\n",
            base=base,
            save="1",
        )
        self.assertIn("changed since you opened it", page)
        self.assertIn("A rule added elsewhere", self.cfg.rubric_path.read_text())

    def test_a_rubric_edit_that_cant_be_recorded_is_undone(self):
        from plus1 import propose

        before = self.cfg.rubric_path.read_text()

        class Full:
            def insert(self, *args, **kwargs):
                raise RuntimeError("database is locked")

        with self.assertRaisesRegex(RuntimeError, "locked"):
            propose.save_edit(self.cfg, Full(), before + "\n- New rule.\n")
        self.assertEqual(self.cfg.rubric_path.read_text(), before)

        rubric = self.cfg.rubric_path

        class Slow:
            def insert(self, *args, **kwargs):
                rubric.write_text("written by hand meanwhile\n")
                raise RuntimeError("database is locked")

        with self.assertRaises(RuntimeError):
            propose.save_edit(self.cfg, Slow(), before + "\n- Another rule.\n")
        self.assertEqual(self.cfg.rubric_path.read_text(), "written by hand meanwhile\n")

    def test_learning_again_runs_in_the_background(self):
        started = []
        actions.spawn = lambda cfg, name, *args: started.append(args) or "unit"
        resp, _ = self.post("/settings/learn")
        self.assertEqual((resp.status, started), (303, [("learn",)]))


if __name__ == "__main__":
    unittest.main()
