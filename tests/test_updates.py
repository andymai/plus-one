import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PLUS1_CONFIG"] = str(ROOT / "tests" / "config.toml")

from plus1 import actions, cli, config, doctor, gh, host, review, units, updates  # noqa: E402
from plus1.state import State  # noqa: E402
from plus1.store import Store, now  # noqa: E402
from plus1.web import app as webapp  # noqa: E402
from plus1.web import executor  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(where, *args):
    return subprocess.run(
        ["git", "-C", str(where), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def ago(**delta):
    return (datetime.now().astimezone() - timedelta(**delta)).isoformat(timespec="seconds")


class Base(unittest.TestCase):
    """A real origin, this install's clone, and a second clone that plays whoever cuts releases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.env = {k: os.environ.get(k) for k in GIT_ENV}
        os.environ.update(GIT_ENV)
        self.origin, self.clone, self.upstream = base / "origin.git", base / "app", base / "up"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.upstream)],
            check=True,
            capture_output=True,
        )
        git(self.upstream, "checkout", "-q", "-B", "main")
        self.release("1.0.0")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.clone)],
            check=True,
            capture_output=True,
        )

        self.cfg = config.load()
        self.cfg.state_dir = base / "state"
        self.cfg.state_dir.mkdir()
        self.cfg.notify = False
        self.cfg.updates.mode, self.cfg.updates.channel = "automatic", "releases"
        self.store = Store(self.cfg)

        self.saved = (
            updates.ROOT,
            updates._deps,
            updates._smoke,
            updates._restart,
            updates._healthy,
            updates.serving,
            updates.HEALTH_WAIT_S,
            updates.SETTLE_S,
            updates.QUIET_WAIT_S,
            actions.notify,
            host.spawn,
            units.installed,
        )
        updates.ROOT = self.clone
        updates.SETTLE_S = 0
        self.steps, self.notified, self.spawned, self.said = [], [], [], []
        updates._deps = lambda: self.steps.append("deps")
        updates._smoke = lambda: self.steps.append("smoke")
        updates._restart = lambda: self.steps.append("restart")
        updates._healthy = lambda cfg, sha, exact=True: self.steps.append("healthy")
        actions.notify = lambda cfg, title, body, path="/", store=None: self.notified.append(
            (title, body)
        )
        host.spawn = lambda state_dir, name, args: self.spawned.append((name, args))
        units.installed = lambda web=False: False

    def tearDown(self):
        (
            updates.ROOT,
            updates._deps,
            updates._smoke,
            updates._restart,
            updates._healthy,
            updates.serving,
            updates.HEALTH_WAIT_S,
            updates.SETTLE_S,
            updates.QUIET_WAIT_S,
            actions.notify,
            host.spawn,
            units.installed,
        ) = self.saved
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.store.db.close()
        self.tmp.cleanup()

    def commit(self, version, tag=None):
        package = self.upstream / "plus1"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text(f'__version__ = "{version}"\n')
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "--allow-empty", "-m", f"release {version}")
        if tag:
            git(self.upstream, "tag", tag)
        git(self.upstream, "push", "-q", "origin", "main", "--tags")
        return git(self.upstream, "rev-parse", "HEAD")

    def release(self, version):
        return self.commit(version, f"v{version}")

    def head(self):
        return git(self.clone, "rev-parse", "HEAD")

    def state(self):
        return json.loads(self.store.get_meta(updates.STATE) or "{}")

    def say(self, text):
        self.said.append(text)

    def run_update(self, how="now"):
        return updates.run(self.cfg, how, self.say)


class CheckTest(Base):
    def test_the_newest_release_wins_by_version_not_by_name(self):
        self.release("1.9.0")
        self.release("1.10.0")
        state = updates.check(self.cfg, self.store)
        self.assertEqual((state["status"], state["latest"]), ("available", "1.10.0"))

    def test_a_tag_that_isnt_on_main_is_not_a_release(self):
        git(self.upstream, "checkout", "-q", "-b", "side")
        self.commit("9.9.9", "v9.9.9")
        git(self.upstream, "push", "-q", "origin", "side")
        git(self.upstream, "checkout", "-q", "main")
        state = updates.check(self.cfg, self.store)
        self.assertEqual((state["status"], state["latest"]), ("current", "1.0.0"))

    def test_main_follows_merges_that_have_no_release(self):
        sha = self.commit("1.0.0")
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "current")
        self.cfg.updates.channel = "main"
        state = updates.check(self.cfg, self.store)
        self.assertEqual(state["status"], "available")
        self.assertEqual(state["latest"], f"1.0.0 ({sha[:7]})")

    def test_a_tag_moved_on_origin_doesnt_stop_updates(self):
        sha = self.commit("1.0.1")
        git(self.upstream, "tag", "-f", "v1.0.0", sha)
        git(self.upstream, "push", "-q", "--force", "origin", "v1.0.0")
        state = updates.check(self.cfg, self.store)
        self.assertNotIn("error", state)
        self.assertEqual(state["status"], "available")

    def test_a_clone_ahead_of_the_newest_release_is_current(self):
        self.commit("1.0.0")
        git(self.clone, "pull", "-q", "--ff-only")
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "current")

    def test_a_repo_with_no_releases_has_nothing_to_do(self):
        git(self.upstream, "tag", "-d", "v1.0.0")
        git(self.upstream, "push", "-q", "origin", ":refs/tags/v1.0.0")
        git(self.clone, "tag", "-d", "v1.0.0")
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "current")

    def test_uncommitted_changes_block_it_and_it_says_so_once(self):
        self.release("1.1.0")
        (self.clone / "plus1" / "__init__.py").write_text("# mine\n")
        state = updates.check(self.cfg, self.store)
        self.assertEqual(state["status"], "blocked")
        self.assertIn("uncommitted changes", state["reason"])
        updates.check(self.cfg, self.store)
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.notified[0][0], "Plus One can't update")

    def test_a_file_git_doesnt_track_blocks_nothing(self):
        self.release("1.1.0")
        (self.clone / "scratch.txt").write_text("mine\n")
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "available")

    def test_being_on_another_branch_blocks_it(self):
        self.release("1.1.0")
        git(self.clone, "checkout", "-q", "-b", "my-work")
        state = updates.check(self.cfg, self.store)
        self.assertEqual(state["status"], "blocked")
        self.assertIn("my-work", state["reason"])

    def test_local_commits_block_it(self):
        self.release("1.1.0")
        (self.clone / "mine.txt").write_text("mine\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "mine")
        state = updates.check(self.cfg, self.store)
        self.assertEqual(state["status"], "blocked")
        self.assertIn("aren't on main", state["reason"])

    def test_asking_first_shows_a_block_without_a_notification(self):
        self.cfg.updates.mode = "ask"
        self.release("1.1.0")
        (self.clone / "plus1" / "__init__.py").write_text("# mine\n")
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "blocked")
        self.assertEqual(self.notified, [])

    def test_clearing_a_block_lets_the_next_one_notify(self):
        self.release("1.1.0")
        path = self.clone / "plus1" / "__init__.py"
        original = path.read_text()
        path.write_text("# mine\n")
        updates.check(self.cfg, self.store)
        path.write_text(original)
        updates.check(self.cfg, self.store)
        path.write_text("# mine again\n")
        updates.check(self.cfg, self.store)
        self.assertEqual(len(self.notified), 2)

    def test_a_failed_fetch_keeps_what_it_knew(self):
        self.release("1.1.0")
        updates.check(self.cfg, self.store)
        git(self.clone, "remote", "set-url", "origin", str(self.origin) + "-gone")
        state = updates.check(self.cfg, self.store)
        self.assertEqual(state["status"], "available")
        self.assertTrue(state["error"])
        self.assertEqual(self.notified, [])


class InstallTest(Base):
    def test_it_moves_forward_verifies_and_records(self):
        before = self.head()
        sha = self.release("1.1.0")
        self.assertEqual(self.run_update("auto"), 0)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.steps, ["deps", "smoke", "restart", "healthy"])
        self.assertEqual(self.state()["status"], "current")
        previous = json.loads(self.store.get_meta(updates.PREVIOUS) or "{}")
        self.assertEqual(previous, {"sha": before, "version": "1.0.0"})
        view = updates.view(self.cfg, self.store)
        self.assertEqual(view["history"][0]["result"], "installed")
        self.assertIsNotNone(view["installed_at"])
        self.assertIsNone(view["installing"])
        self.assertEqual(self.said[-1], "now on Plus One 1.1.0")
        self.assertEqual(self.notified, [])

    def test_it_stays_on_main_and_keeps_tracking_origin(self):
        self.release("1.1.0")
        self.run_update()
        self.assertEqual(git(self.clone, "symbolic-ref", "--short", "HEAD"), "main")

    def test_a_clean_clone_already_current_changes_nothing(self):
        before = self.head()
        self.assertEqual(self.run_update("auto"), 0)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.steps, [])
        self.assertEqual(self.store.get_meta(updates.PREVIOUS), None)

    def test_running_it_by_hand_when_current_still_restarts(self):
        self.assertEqual(self.run_update("now"), 0)
        self.assertEqual(self.steps, ["restart"])

    def fail_at(self, name):
        def broken(*args, **kwargs):
            raise updates.Failed(f"{name} broke")

        setattr(updates, name, broken)

    def assert_rolled_back(self, before, sha, why):
        self.assertEqual(self.head(), before)
        self.assertEqual(self.store.get_meta(updates.SKIP), sha)
        self.assertEqual(self.state()["status"], "skipped")
        last = updates.view(self.cfg, self.store)["history"][0]
        self.assertEqual((last["version"], last["result"]), ("1.1.0", "rolled back"))
        self.assertIn(why, last["why"])
        self.assertEqual(self.notified[-1][0], "Plus One update failed")
        self.assertIn("Back on 1.0.0.", self.notified[-1][1])
        self.assertIsNone(updates.installing(self.store))
        self.assertIsNone(self.store.get_meta(updates.PREVIOUS))

    def test_a_failed_smoke_test_goes_back(self):
        before, sha = self.head(), self.release("1.1.0")
        self.fail_at("_smoke")
        self.assertEqual(self.run_update("auto"), 1)
        self.assert_rolled_back(before, sha, "_smoke broke")
        self.assertNotIn("restart", self.steps)

    def test_dependencies_that_wont_install_go_back(self):
        before, sha = self.head(), self.release("1.1.0")
        calls = []

        def deps():
            calls.append(self.head())
            if len(calls) == 1:
                raise updates.Failed("no such package")

        updates._deps = deps
        self.assertEqual(self.run_update("auto"), 1)
        self.assert_rolled_back(before, sha, "no such package")
        self.assertEqual(calls, [sha, before])

    def test_a_web_app_that_doesnt_come_back_goes_back_and_restarts_the_old_code(self):
        before, sha = self.head(), self.release("1.1.0")
        heads = []

        def healthy(cfg, target, exact=True):
            heads.append((self.head(), exact))
            if exact:
                raise updates.Failed("the web app didn't come back")

        updates._healthy = healthy
        self.assertEqual(self.run_update("auto"), 1)
        self.assert_rolled_back(before, sha, "didn't come back")
        self.assertEqual(heads, [(sha, True), (before, False)])
        self.assertEqual(self.steps.count("restart"), 2)

    def test_if_going_back_fails_too_the_notification_says_so(self):
        self.release("1.1.0")
        self.fail_at("_smoke")
        self.fail_at("_deps")
        self.assertEqual(self.run_update("auto"), 1)
        self.assertIn("Going back failed too", self.notified[-1][1])

    def test_a_skipped_version_stays_skipped_until_a_newer_one(self):
        sha = self.release("1.1.0")
        self.fail_at("_smoke")
        self.run_update("auto")
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "skipped")
        self.assertEqual(self.run_update("auto"), 0)
        self.assertNotEqual(self.head(), sha)
        newer = self.release("1.2.0")
        updates._smoke = lambda: None
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "available")
        self.assertEqual(self.run_update("auto"), 0)
        self.assertEqual(self.head(), newer)
        self.assertIsNone(self.store.get_meta(updates.SKIP) or None)

    def test_installing_by_hand_overrides_a_skip(self):
        sha = self.release("1.1.0")
        self.fail_at("_smoke")
        self.run_update("auto")
        updates._smoke = lambda: None
        self.assertEqual(self.run_update("now"), 0)
        self.assertEqual(self.head(), sha)

    def test_a_blocked_clone_is_left_exactly_as_it_was(self):
        self.release("1.1.0")
        path = self.clone / "plus1" / "__init__.py"
        path.write_text("# mine\n")
        before = self.head()
        self.assertEqual(self.run_update("now"), 1)
        self.assertEqual(self.head(), before)
        self.assertEqual(path.read_text(), "# mine\n")
        self.assertIn("uncommitted changes", self.said[-1])
        self.assertEqual(self.steps, [])

    def test_nothing_starts_while_a_check_holds_the_lock(self):
        self.release("1.1.0")
        lock = State(self.cfg).lock()
        saved = updates.AUTO_WAIT_S
        updates.AUTO_WAIT_S = 0
        before = self.head()
        try:
            self.assertEqual(self.run_update("auto"), 0)
        finally:
            updates.AUTO_WAIT_S = saved
            lock.close()
        self.assertEqual(self.head(), before)
        self.assertEqual(self.steps, [])

    def test_a_check_cant_start_during_an_install(self):
        self.release("1.1.0")
        seen = []
        updates._smoke = lambda: seen.append(State(self.cfg).lock())
        self.run_update("auto")
        self.assertEqual(seen, [None])

    def test_a_failed_fetch_says_so_and_installs_nothing(self):
        git(self.clone, "remote", "set-url", "origin", str(self.origin) + "-gone")
        self.assertEqual(self.run_update("now"), 1)
        self.assertIn("Couldn't ask GitHub", self.said[-1])
        self.assertEqual(self.steps, [])


class RollbackTest(Base):
    def test_it_goes_back_once_and_skips_what_it_left(self):
        before = self.head()
        sha = self.release("1.1.0")
        self.run_update()
        self.steps.clear()
        self.assertEqual(self.run_update("rollback"), 0)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.steps, ["deps", "restart", "healthy"])
        self.assertEqual(self.store.get_meta(updates.SKIP), sha)
        self.assertEqual(updates.view(self.cfg, self.store)["previous"], {})
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "skipped")
        last = updates.view(self.cfg, self.store)["history"][0]
        self.assertEqual((last["version"], last["why"]), ("1.1.0", "you asked"))
        self.assertEqual(self.said[-1], "back on Plus One 1.0.0")

    def test_with_nothing_to_go_back_to_it_says_so(self):
        self.assertEqual(self.run_update("rollback"), 1)
        self.assertIn("no earlier version", self.said[-1])

    def test_it_wont_touch_a_clone_with_changes(self):
        sha = self.release("1.1.0")
        self.run_update()
        (self.clone / "plus1" / "__init__.py").write_text("# mine\n")
        self.assertEqual(self.run_update("rollback"), 1)
        self.assertEqual(self.head(), sha)
        self.assertIn("uncommitted changes", self.said[-1])

    def test_it_wont_drop_commits_made_since_the_update(self):
        self.release("1.1.0")
        self.run_update()
        (self.clone / "mine.txt").write_text("mine\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "my own work")
        mine = self.head()
        self.assertEqual(self.run_update("rollback"), 1)
        self.assertEqual(self.head(), mine)
        self.assertIn("aren't on main", self.said[-1])
        self.assertEqual(self.steps.count("deps"), 1)

    def test_a_failed_rollback_returns_to_where_it_was(self):
        sha = self.release("1.1.0")
        self.run_update()
        calls = []

        def healthy(cfg, target, exact=True):
            calls.append(target)
            if len(calls) == 1:
                raise updates.Failed("no answer")

        updates._healthy = healthy
        self.assertEqual(self.run_update("rollback"), 1)
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.notified[-1][0], "Plus One rollback failed")
        self.assertNotEqual(self.store.get_meta(updates.SKIP), sha)


class BusyTest(Base):
    def add_draft(self, updated_at):
        return self.store.insert(
            "drafts",
            pr=1,
            sha="s",
            body="b",
            status="writing",
            updated_at=updated_at,
        )

    def test_a_check_in_progress_is_busy(self):
        self.assertIsNone(updates.busy(self.cfg, self.store))
        lock = State(self.cfg).lock()
        try:
            self.assertEqual(updates.busy(self.cfg, self.store), "a check is running")
        finally:
            lock.close()
        self.assertIsNone(updates.busy(self.cfg, self.store))

    def pending(self, execute_at, status="pending"):
        return self.store.insert(
            "pending_actions",
            kind="approve",
            pr=1,
            created_at=now(),
            execute_at=execute_at,
            status=status,
        )

    def test_a_card_action_in_its_undo_window_is_busy(self):
        import time

        self.pending(time.time() + 8)
        self.assertIn("card action", str(updates.busy(self.cfg, self.store)))

    def test_a_slow_card_action_still_counts_after_several_minutes(self):
        import time

        self.pending(time.time() - 600, "running")
        self.assertIn("card action", str(updates.busy(self.cfg, self.store)))

    def test_a_pending_action_too_old_to_run_doesnt_count(self):
        import time

        self.pending(time.time() - 600)
        self.assertIsNone(updates.busy(self.cfg, self.store))

    def test_a_card_action_a_crash_left_behind_doesnt_block_forever(self):
        import time

        self.pending(time.time() - 3600, "running")
        self.assertIsNone(updates.busy(self.cfg, self.store))

    def test_a_comment_being_written_is_busy_until_it_counts_as_failed(self):
        self.add_draft(now())
        self.assertIn("comment", str(updates.busy(self.cfg, self.store)))
        self.store.db.execute("UPDATE drafts SET updated_at = ?", (ago(minutes=60),))
        self.assertIsNone(updates.busy(self.cfg, self.store))

    def test_a_rubric_change_being_drafted_is_busy_for_a_while(self):
        rid = self.store.insert("proposals", created_at=now(), status="pending")
        self.assertIn("rubric", str(updates.busy(self.cfg, self.store)))
        self.store.update("proposals", rid, created_at=ago(hours=2))
        self.assertIsNone(updates.busy(self.cfg, self.store))

    def test_an_install_that_died_stops_counting_as_running(self):
        self.store.set_meta(updates.INSTALLING, json.dumps({"at": now(), "to": "1.1.0"}))
        self.assertTrue(updates.installing(self.store))
        self.store.set_meta(updates.INSTALLING, json.dumps({"at": ago(hours=1), "to": "1.1.0"}))
        self.assertIsNone(updates.installing(self.store))


class QuietRestartTest(Base):
    def add_action(self, status, kind="approve"):
        import time

        return self.store.insert(
            "pending_actions",
            kind=kind,
            pr=1,
            created_at=now(),
            execute_at=time.time() - 1,
            status=status,
        )

    def test_it_holds_new_actions_back_during_the_restart_only(self):
        seen = []
        updates._restart = lambda: seen.append(updates.restarting(self.store))
        self.assertFalse(updates.restarting(self.store))
        updates._restart_quietly(self.store)
        self.assertEqual(seen, [True])
        self.assertFalse(updates.restarting(self.store))

    def test_the_hold_lifts_even_when_the_restart_fails(self):
        def broken():
            raise updates.Failed("no")

        updates._restart = broken
        with self.assertRaises(updates.Failed):
            updates._restart_quietly(self.store)
        self.assertFalse(updates.restarting(self.store))

    def test_a_hold_a_dead_job_left_behind_lifts_on_its_own(self):
        self.store.set_meta(updates.RESTARTING, ago(minutes=3))
        self.assertFalse(updates.restarting(self.store))

    def test_it_waits_for_a_running_action_to_finish(self):
        row = self.add_action("running")
        order = []
        updates._restart = lambda: order.append("restart")

        def finish():
            store = Store(self.cfg)
            order.append("finished")
            store.update("pending_actions", row, status="done")
            store.db.close()

        timer = threading.Timer(0.2, finish)
        timer.start()
        updates._restart_quietly(self.store)
        timer.join()
        self.assertEqual(order, ["finished", "restart"])

    def test_it_wont_restart_over_an_action_that_wont_finish(self):
        self.add_action("running")
        updates.QUIET_WAIT_S = 0
        updates._restart = lambda: self.fail("that would cut it off")
        with self.assertRaises(updates.StillRunning):
            updates._restart_quietly(self.store)
        self.assertFalse(updates.restarting(self.store))

    def test_recovering_from_a_failure_restarts_regardless(self):
        self.add_action("running")
        updates.QUIET_WAIT_S = 0
        seen = []
        updates._restart = lambda: seen.append(True)
        updates._restart_quietly(self.store, insist=True)
        self.assertEqual(seen, [True])

    def test_a_row_a_crash_left_behind_doesnt_stop_a_restart(self):
        row = self.add_action("running")
        self.store.update("pending_actions", row, execute_at=1.0)
        updates.QUIET_WAIT_S = 0
        seen = []
        updates._restart = lambda: seen.append(True)
        updates._restart_quietly(self.store)
        self.assertEqual(seen, [True])

    def click_during_the_install(self):
        def smoke():
            self.steps.append("smoke")
            self.add_action("running")

        updates._smoke = smoke
        updates.QUIET_WAIT_S = 0

    def test_a_click_mid_install_puts_it_off_without_blaming_the_version(self):
        before, sha = self.head(), self.release("1.1.0")
        self.click_during_the_install()
        self.assertEqual(self.run_update("auto"), 0)
        self.assertEqual(self.head(), before)
        self.assertNotIn("restart", self.steps)
        self.assertFalse(self.store.get_meta(updates.SKIP))
        self.assertEqual(updates.view(self.cfg, self.store)["history"], [])
        self.assertEqual(self.notified, [])
        self.assertIn("It'll try again", self.said[-1])
        self.assertEqual(updates.check(self.cfg, self.store)["status"], "available")
        self.store.db.execute("UPDATE pending_actions SET status = 'done'")
        updates._smoke = lambda: None
        self.assertEqual(self.run_update("auto"), 0)
        self.assertEqual(self.head(), sha)

    def test_a_click_during_a_rollback_changes_nothing(self):
        sha = self.release("1.1.0")
        self.run_update()
        self.steps.clear()
        self.add_action("running")
        saved = updates._working
        updates._working = lambda store: None
        updates.QUIET_WAIT_S = 0
        try:
            self.assertEqual(self.run_update("rollback"), 1)
        finally:
            updates._working = saved
        self.assertEqual(self.head(), sha)
        self.assertNotIn("restart", self.steps)
        self.assertTrue(updates.view(self.cfg, self.store)["previous"])
        self.assertFalse(self.store.get_meta(updates.SKIP))

    def test_the_executor_leaves_actions_pending_while_held_and_runs_them_after(self):
        row = self.add_action("pending", kind="gone-in-this-version")
        self.store.set_meta(updates.RESTARTING, now())
        self.assertEqual(executor.run_due(self.cfg, self.store), 0)
        status = self.store.query("SELECT status FROM pending_actions WHERE id = ?", row)[0][0]
        self.assertEqual(status, "pending")
        self.store.set_meta(updates.RESTARTING, "")
        executor.run_due(self.cfg, self.store)
        status = self.store.query("SELECT status FROM pending_actions WHERE id = ?", row)[0][0]
        self.assertEqual(status, "cancelled")

    def test_an_install_goes_through_the_quiet_restart(self):
        self.release("1.1.0")
        seen = []
        updates._restart = lambda: seen.append(updates.restarting(self.store))
        self.assertEqual(self.run_update("auto"), 0)
        self.assertEqual(seen, [True])

    def test_a_delayed_rollback_that_cant_put_the_files_back_says_so(self):
        self.release("1.1.0")
        self.run_update()
        self.add_action("running")
        saved = updates._working
        updates._working = lambda store: None
        updates.QUIET_WAIT_S = 0
        calls = []

        def deps():
            calls.append(1)
            if len(calls) == 2:
                raise updates.Failed("no such package")

        updates._deps = deps
        try:
            self.assertEqual(self.run_update("rollback"), 1)
        finally:
            updates._working = saved
        self.assertEqual(self.notified[-1][0], "Plus One rollback failed")
        self.assertIn("no such package", self.notified[-1][1])
        self.assertNotIn("Nothing changed", self.said[-1])

    def test_a_put_off_install_that_cant_put_the_files_back_says_so(self):
        self.release("1.1.0")
        self.click_during_the_install()
        calls = []

        def deps():
            calls.append(1)
            if len(calls) == 2:
                raise updates.Failed("no such package")

        updates._deps = deps
        self.assertEqual(self.run_update("auto"), 1)
        self.assertEqual(self.notified[-1][0], "Plus One update failed")
        self.assertIn("no such package", self.said[-1])


class MaybeTest(Base):
    def test_off_never_touches_the_network(self):
        self.cfg.updates.mode = "off"
        self.release("1.1.0")
        saved = updates.check
        updates.check = lambda cfg, store: self.fail("off shouldn't check")
        try:
            updates.maybe(self.cfg, self.store)
        finally:
            updates.check = saved
        self.assertEqual(self.spawned, [])

    def test_automatic_starts_an_install_when_one_is_waiting(self):
        self.release("1.1.0")
        updates.maybe(self.cfg, self.store, self.say)
        self.assertEqual(self.spawned, [("update", ["update", "--auto"])])
        self.assertIn("1.1.0", self.said[-1])

    def test_asking_first_only_checks(self):
        self.cfg.updates.mode = "ask"
        self.release("1.1.0")
        updates.maybe(self.cfg, self.store)
        self.assertEqual(self.state()["status"], "available")
        self.assertEqual(self.spawned, [])

    def test_nothing_starts_when_current_blocked_or_skipped(self):
        updates.maybe(self.cfg, self.store)
        self.assertEqual(self.spawned, [])
        sha = self.release("1.1.0")
        self.store.set_meta(updates.SKIP, sha)
        self.store.set_meta(updates.STATE, "")
        updates.maybe(self.cfg, self.store)
        self.assertEqual(self.state()["status"], "skipped")
        self.assertEqual(self.spawned, [])
        self.store.set_meta(updates.SKIP, "")
        git(self.clone, "checkout", "-q", "-b", "my-work")
        self.store.set_meta(updates.STATE, "")
        updates.maybe(self.cfg, self.store)
        self.assertEqual(self.state()["status"], "blocked")
        self.assertEqual(self.spawned, [])

    def test_it_checks_at_most_hourly_but_keeps_retrying_a_waiting_install(self):
        self.release("1.1.0")
        lock = State(self.cfg).lock()
        try:
            updates.maybe(self.cfg, self.store)
        finally:
            lock.close()
        self.assertEqual(self.spawned, [])
        saved, calls = updates.check, []
        updates.check = lambda cfg, store: calls.append(1) or saved(cfg, store)
        try:
            updates.maybe(self.cfg, self.store)
            self.assertEqual(calls, [])
            self.assertEqual(len(self.spawned), 1)
            state = {**self.state(), "checked_at": ago(minutes=61)}
            self.store.set_meta(updates.STATE, json.dumps(state))
            updates.maybe(self.cfg, self.store)
            self.assertEqual(calls, [1])
        finally:
            updates.check = saved

    def test_nothing_starts_while_one_is_running_or_github_is_unreachable(self):
        self.release("1.1.0")
        self.store.set_meta(updates.INSTALLING, json.dumps({"at": now(), "to": "1.1.0"}))
        updates.maybe(self.cfg, self.store)
        self.assertEqual(self.spawned, [])
        self.store.set_meta(updates.INSTALLING, "")
        git(self.clone, "remote", "set-url", "origin", str(self.origin) + "-gone")
        self.store.set_meta(
            updates.STATE, json.dumps({"status": "available", "checked_at": ago(hours=2)})
        )
        updates.maybe(self.cfg, self.store)
        self.assertEqual(self.spawned, [])

    def catch_up(self, running):
        saved = updates.build
        updates.build = lambda: running
        try:
            updates._catch_up(self.cfg, self.store)
        finally:
            updates.build = saved

    def test_a_clone_pulled_by_hand_gets_its_web_app_restarted(self):
        units.installed = lambda web=False: web
        self.catch_up(self.head()[: updates.BUILD_LENGTH])
        self.assertEqual(self.spawned, [])
        self.catch_up("something-else")
        self.assertEqual(self.spawned, [("restart-web", ["restart-web"])])

    def test_a_restart_only_happens_when_it_should(self):
        units.installed = lambda web=False: web
        self.cfg.updates.mode = "ask"
        self.catch_up("something-else")
        self.cfg.updates.mode = "automatic"
        units.installed = lambda web=False: False
        self.catch_up("something-else")
        units.installed = lambda web=False: web
        git(self.clone, "checkout", "-q", "-b", "my-work")
        self.catch_up("something-else")
        self.assertEqual(self.spawned, [])


class HealthTest(Base):
    def setUp(self):
        super().setUp()
        updates._healthy = self.saved[4]
        updates.HEALTH_WAIT_S = 0.05
        units.installed = lambda web=False: web

    def test_the_new_build_has_to_be_the_one_answering(self):
        updates.serving = lambda cfg: "a" * updates.BUILD_LENGTH
        updates._healthy(self.cfg, "a" * 40)
        with self.assertRaisesRegex(updates.Failed, "still on"):
            updates._healthy(self.cfg, "b" * 40)
        updates._healthy(self.cfg, "b" * 40, exact=False)

    def test_going_back_to_a_version_that_doesnt_say_only_needs_an_answer(self):
        updates.serving = lambda cfg: ""
        updates._healthy(self.cfg, "b" * 40, exact=False)

    def test_no_answer_fails(self):
        updates.serving = lambda cfg: None
        with self.assertRaisesRegex(updates.Failed, "come back"):
            updates._healthy(self.cfg, "a" * 40)

    def test_without_a_web_app_there_is_nothing_to_ask(self):
        units.installed = lambda web=False: False
        updates.serving = lambda cfg: self.fail("nothing to ask")
        updates._healthy(self.cfg, "a" * 40)


class ServingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load()
        self.cfg.state_dir = Path(self.tmp.name)
        self.cfg.notify = False
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def serve(self, server):
        self.servers.append(server)
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        return server

    def test_it_signs_in_the_way_local_mode_expects(self):
        self.cfg.web.mode = "local"
        server = self.serve(webapp.TcpServer(("127.0.0.1", 0), webapp.make_handler(self.cfg)))
        self.cfg.web.port = server.server_address[1]
        self.assertEqual(updates.serving(self.cfg), updates.build())

    def test_it_asks_over_the_socket_in_tailscale_mode(self):
        self.cfg.web.mode = "tailscale"
        self.cfg.web.socket = str(Path(self.tmp.name) / "web.sock")
        self.serve(webapp.UnixServer(self.cfg.web.socket, webapp.make_handler(self.cfg)))
        self.assertEqual(updates.serving(self.cfg), updates.build())

    def test_nothing_listening_is_no_answer(self):
        self.cfg.web.mode = "tailscale"
        self.cfg.web.socket = str(Path(self.tmp.name) / "gone.sock")
        self.assertIsNone(updates.serving(self.cfg))
        self.cfg.web.mode = "local"
        self.cfg.web.port = 1
        self.assertIsNone(updates.serving(self.cfg))


class ChangelogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = updates.ROOT
        updates.ROOT = Path(self.tmp.name)

    def tearDown(self):
        updates.ROOT = self.saved
        self.tmp.cleanup()

    def test_it_keeps_the_words_and_drops_the_links(self):
        (updates.ROOT / "CHANGELOG.md").write_text(
            """# Changelog

## [1.10.0](https://x/compare/v1.9.0...v1.10.0) (2026-11-04)


### Features

* **web:** put your team's PRs first in To do ([#76](https://x/issues/76)) ([34c6547](https://x/commit/34c6547))


### Bug Fixes

* stop reading `tick.lock` twice ([#70](https://x/issues/70)) ([abc](https://x/commit/abc)), closes [#9](https://x/issues/9)


### CI

* run it on Tuesdays ([#1](https://x/issues/1))

## [1.9.1](https://x/compare) (2026-10-29)

### CI

* nothing anyone using it would see
"""
        )
        releases = updates.changelog()
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["version"], "1.10.0")
        self.assertEqual(releases[0]["date"], "Nov 4, 2026")
        self.assertEqual(
            releases[0]["groups"],
            {
                "New": ["Put your team's PRs first in To do"],
                "Fixed": ["Stop reading `tick.lock` twice"],
            },
        )

    def test_no_changelog_is_an_empty_list(self):
        self.assertEqual(updates.changelog(), [])

    def test_the_real_one_parses(self):
        updates.ROOT = ROOT
        releases = updates.changelog()
        self.assertTrue(releases)
        for release in releases:
            self.assertRegex(release["version"], r"^\d+\.\d+\.\d+$")
            for items in release["groups"].values():
                for item in items:
                    self.assertNotIn("](", item)


class IntroAndNoteTest(Base):
    def test_the_notice_shows_once_per_install(self):
        self.assertEqual(updates.intro(self.cfg, self.store), updates.INTRO_TEXT)
        self.assertIsNone(updates.intro(self.cfg, self.store))

    def test_off_has_no_notice(self):
        self.cfg.updates.mode = "off"
        self.assertIsNone(updates.intro(self.cfg, self.store))

    def test_the_sidebar_only_speaks_when_theres_something_to_do(self):
        self.release("1.1.0")
        updates.check(self.cfg, self.store)
        self.assertIsNone(updates.note(self.cfg, self.store))
        self.cfg.updates.mode = "ask"
        self.assertEqual(updates.note(self.cfg, self.store), "Update available")
        self.cfg.updates.mode = "off"
        self.assertIsNone(updates.note(self.cfg, self.store))
        self.cfg.updates.mode = "automatic"
        git(self.clone, "checkout", "-q", "-b", "my-work")
        updates.check(self.cfg, self.store)
        self.assertEqual(updates.note(self.cfg, self.store), "Updates paused")


class ConfigTest(unittest.TestCase):
    def load(self, extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            text = (ROOT / "tests" / "config.toml").read_text()
            path.write_text(text.replace('[updates]\nmode = "off"\n', extra))
            (Path(tmp) / "rubric.md").write_text("rules")
            return config.load(path)

    def test_it_defaults_to_automatic_releases(self):
        cfg = self.load("")
        self.assertEqual((cfg.updates.mode, cfg.updates.channel), ("automatic", "releases"))

    def test_a_bad_value_is_refused_with_its_key(self):
        for extra, key in (
            ('[updates]\nmode = "sometimes"\n', "updates.mode"),
            ('[updates]\nchannel = "nightly"\n', "updates.channel"),
        ):
            with self.assertRaisesRegex(config.ConfigError, key):
                self.load(extra)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.saved = doctor.update, updates.run

    def tearDown(self):
        doctor.update, updates.run = self.saved

    def test_the_flags_pick_how_it_runs(self):
        seen = []
        doctor.update = lambda how="now": seen.append(how) or 0
        for flags in ([], ["--auto"], ["--rollback"]):
            self.assertEqual(cli.main(["update", *flags]), 0)
        self.assertEqual(seen, ["now", "auto", "rollback"])

    def test_a_broken_config_cant_turn_a_rollback_into_a_pull(self):
        saved_env, saved_run = os.environ["PLUS1_CONFIG"], doctor._run
        os.environ["PLUS1_CONFIG"] = "/nonexistent/config.toml"
        doctor._run = lambda *cmd, timeout=30: self.fail(f"nothing should run: {cmd}")
        said = []
        try:
            self.assertEqual(doctor.update(say=said.append, how="rollback"), 1)
            self.assertEqual(doctor.update(say=said.append, how="auto"), 1)
        finally:
            os.environ["PLUS1_CONFIG"], doctor._run = saved_env, saved_run
        self.assertIn("nothing changed", said[0])

    def test_update_goes_through_the_checked_path_whatever_the_mode(self):
        seen = []
        updates.run = lambda cfg, how, say: seen.append((cfg.updates.mode, how)) or 0
        self.assertEqual(doctor.update(how="now"), 0)
        self.assertEqual(seen, [("off", "now")])


class DoctorTest(Base):
    def test_a_paused_update_is_something_to_fix(self):
        self.release("1.1.0")
        self.assertEqual(doctor._updates(self.cfg)[0], True)
        git(self.clone, "checkout", "-q", "-b", "my-work")
        updates.check(self.cfg, self.store)
        ok, what, fix = doctor._updates(self.cfg)
        self.assertFalse(ok)
        self.assertIn("my-work", what)
        self.assertIn("Settings, Updates", fix)
        self.cfg.updates.mode = "off"
        self.assertTrue(doctor._updates(self.cfg)[0])


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load()
        self.cfg.state_dir = Path(self.tmp.name)
        self.cfg.notify = False
        self.cfg.slack.enabled, self.cfg.checkout = False, self.tmp.name
        self.saved = gh.run, gh.candidates, gh.followups, review.run, updates.maybe
        gh.run = lambda args, input_text=None, timeout=90: ""
        gh.candidates = lambda cfg: []
        gh.followups = lambda cfg: []
        self.calls = []

        def maybe(cfg, store, log=print):
            # The lock has to be free by now, or the update it starts would find a check still running.
            lock = State(cfg).lock()
            self.calls.append(lock is not None)
            if lock:
                lock.close()

        updates.maybe = maybe

    def tearDown(self):
        gh.run, gh.candidates, gh.followups, review.run, updates.maybe = self.saved
        self.tmp.cleanup()

    def tick(self, **overrides):
        args = {
            "pr": None,
            "any_time": True,
            "force": False,
            "plan": False,
            "trigger": "timer",
            "wait_lock": False,
            **overrides,
        }
        self.assertEqual(cli.tick(self.cfg, SimpleNamespace(**args)), 0)

    def test_a_timer_tick_looks_for_an_update_after_letting_go_of_the_lock(self):
        self.tick()
        self.assertEqual(self.calls, [True])

    def test_other_runs_dont(self):
        self.tick(trigger="manual")
        self.tick(plan=True)
        self.assertEqual(self.calls, [])

    def test_a_failed_look_doesnt_fail_the_tick(self):
        def broken(cfg, store, log=print):
            raise RuntimeError("no")

        updates.maybe = broken
        self.tick()


class RealScriptsTest(unittest.TestCase):
    """The stubbed steps above have to work for real at least once."""

    def test_the_smoke_test_passes_on_this_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            text = (ROOT / "tests" / "config.toml").read_text()
            text = text.replace(
                'state_dir = "~/.local/state/plus1-tests-unused"', f'state_dir = "{tmp}/state"'
            )
            config_path.write_text(text)
            shutil.copyfile(ROOT / "tests" / "rubric.md", Path(tmp) / "rubric.md")
            proc = subprocess.run(
                [sys.executable, "-c", "from plus1.web.app import main; main(['--smoke'])"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                env={**os.environ, "PLUS1_CONFIG": str(config_path)},
                timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")

    def test_the_smoke_test_catches_a_broken_template(self):
        cfg = config.load()
        with tempfile.TemporaryDirectory() as tmp:
            cfg.state_dir = Path(tmp)
            saved = webapp.App.__init__

            def broken(self, *args, **kwargs):
                saved(self, *args, **kwargs)
                from jinja2 import DictLoader

                self.env.loader = DictLoader({"bad.html": "{% if %}"})

            webapp.App.__init__ = broken
            try:
                with self.assertRaises(Exception) as caught:
                    webapp.smoke(cfg)
                self.assertEqual(type(caught.exception).__name__, "TemplateSyntaxError")
            finally:
                webapp.App.__init__ = saved


if __name__ == "__main__":
    unittest.main()
