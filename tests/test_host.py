import json
import os
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PLUS1_CONFIG"] = str(ROOT / "tests" / "config.toml")
from plus1 import cli, config, host, slack, units  # noqa: E402


class Stubbed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ran = []
        self._orig = (
            host.MAC,
            host.systemd,
            host.subprocess.run,
            host.subprocess.Popen,
            host.shutil.which,
            slack.subprocess.run,
        )

    def tearDown(self):
        (
            host.MAC,
            host.systemd,
            host.subprocess.run,
            host.subprocess.Popen,
            host.shutil.which,
            slack.subprocess.run,
        ) = self._orig
        self.tmp.cleanup()

    def capture(self, returncode=0, stdout=""):
        def run(cmd, **kw):
            self.ran.append((cmd, kw))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="nope")

        return run


class HostTest(Stubbed):
    def test_background_jobs_carry_the_config_they_were_started_with(self):
        host.systemd = lambda: True
        host.subprocess.run = self.capture()
        job = host.spawn(self.dir, "propose-3", ["propose", 3])
        [(cmd, _)] = self.ran
        self.assertEqual(cmd[:4], ["systemd-run", "--user", "--collect", f"--unit={job}"])
        self.assertIn(f"--setenv=PLUS1_CONFIG={ROOT / 'tests' / 'config.toml'}", cmd)
        self.assertTrue(any(c.startswith("--setenv=PATH=") for c in cmd))
        self.assertEqual(cmd[-3:], [str(ROOT / "bin" / "plus1"), "propose", "3"])

    def test_without_systemd_a_job_runs_detached_and_logs_under_the_state_dir(self):
        host.systemd = lambda: False
        job = host.spawn(self.dir, "help", ["--help"])
        log = self.dir / "logs" / f"{job}.log"
        for _ in range(100):
            if "usage" in (log.read_text() if log.exists() else ""):
                break
            time.sleep(0.05)
        self.assertIn("usage: plus1", log.read_text())

    def test_without_a_user_session_a_job_runs_detached(self):
        host.systemd = lambda: True
        host.subprocess.run = self.capture(returncode=1)
        started = []
        host.subprocess.Popen = lambda argv, **kw: started.append((argv, kw))
        job = host.spawn(self.dir, "x", ["tick"])
        [(argv, kw)] = started
        self.assertEqual(argv, [str(ROOT / "bin" / "plus1"), "tick"])
        self.assertTrue(kw["start_new_session"])
        self.assertTrue((self.dir / "logs" / f"{job}.log").exists())

        def cant(argv, **kw):
            raise FileNotFoundError(argv[0])

        host.subprocess.Popen = cant
        with self.assertRaises(OSError):
            host.spawn(self.dir, "x", ["tick"])

    def test_macos_notifications_pass_text_as_data(self):
        host.MAC = True
        host.shutil.which = lambda name: None
        host.subprocess.Popen = lambda cmd, **kw: self.ran.append((cmd, kw))
        host.notify('Bad" & do shell script "x', "body", "https://x/")
        [(cmd, _)] = self.ran
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-2:], ['Bad" & do shell script "x', "body"])
        self.assertNotIn("do shell script", " ".join(cmd[:-2]))
        self.ran.clear()
        host.shutil.which = lambda name: (
            "/opt/homebrew/bin/terminal-notifier" if name == "terminal-notifier" else None
        )
        host.notify("t", "b", "https://x/review/1")
        self.assertEqual(self.ran[0][0][-2:], ["-open", "https://x/review/1"])

    def test_the_slack_token_is_the_newest_any_claude_login_holds(self):
        key = "plugin:slack:slack|x"
        with tempfile.TemporaryDirectory() as tmp:

            def login(name, token, hours):
                path = Path(tmp) / name / ".credentials.json"
                path.parent.mkdir()
                entry = {"accessToken": token, "expiresAt": (time.time() + hours * 3600) * 1000}
                path.write_text(json.dumps({"mcpOAuth": {key: entry}}))
                return str(path)

            home, work = login("home", "xoxp-home", 11), login("work", "xoxp-work", 12)
            cfg = SimpleNamespace(slack=SimpleNamespace(token_path=[home, work], token_key=key))
            self.assertEqual(slack._token(cfg), "xoxp-work")
            cfg.slack.token_path = [str(Path(tmp) / "gone.json"), home]
            self.assertEqual(slack._token(cfg), "xoxp-home")
            cfg.slack.token_path = home
            self.assertEqual(slack._token(cfg), "xoxp-home")
            cfg.slack.token_path = [str(Path(tmp) / "gone.json")]
            with self.assertRaisesRegex(slack.SlackUnavailable, "no plugin:slack:slack"):
                slack._token(cfg)

    def test_the_slack_token_comes_from_the_keychain_on_macos(self):
        host.MAC = True
        cfg = SimpleNamespace(
            slack=SimpleNamespace(
                token_path="",
                token_key="plugin:slack|x",
                keychain_service="Claude Code-credentials",
            )
        )
        stored = {
            "mcpOAuth": {
                "plugin:slack|x": {
                    "accessToken": "xoxp-1",
                    "expiresAt": (time.time() + 3600) * 1000,
                }
            }
        }
        slack.subprocess.run = self.capture(stdout=json.dumps(stored))
        self.assertEqual(slack._token(cfg), "xoxp-1")
        self.assertEqual(
            self.ran[0][0][:4],
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
        )
        host.MAC = False
        with self.assertRaises(slack.SlackUnavailable):
            slack._token(cfg)


class UnitsTest(Stubbed):
    def test_systemd_units_run_this_checkout_with_its_environment(self):
        found = units.systemd()
        self.assertEqual(sorted(found), ["plus1-web.service", "plus1.service", "plus1.timer"])
        self.assertIn(
            f'ExecStart="{ROOT / "bin" / "plus1"}" tick --trigger timer', found["plus1.service"]
        )
        self.assertIn('Environment="PATH=', found["plus1.service"])
        self.assertIn("OnCalendar=*-*-* *:00,30:00", found["plus1.timer"])
        self.assertNotIn("plus1-web.service", units.systemd(web=False))
        self.assertEqual(units._quote('a "b" 100%'), '"a \\"b\\" 100%%"')

    def test_launchd_agents_are_valid_plists_that_log_under_the_state_dir(self):
        agents = {
            name: plistlib.loads(text.encode()) for name, text in units.launchd(self.dir).items()
        }
        tick, web = agents["local.plus1.tick.plist"], agents["local.plus1.web.plist"]
        self.assertEqual(
            tick["ProgramArguments"], [str(ROOT / "bin" / "plus1"), "tick", "--trigger", "timer"]
        )
        self.assertEqual(tick["StartCalendarInterval"], [{"Minute": 0}, {"Minute": 30}])
        self.assertEqual(tick["StandardOutPath"], str(self.dir / "logs" / "tick.log"))
        self.assertTrue(web["KeepAlive"])
        self.assertIn("PATH", web["EnvironmentVariables"])

    def test_writing_over_an_old_link_leaves_the_checkout_alone(self):
        original = self.dir / "checkout-unit"
        original.write_text("old")
        target = self.dir / "units"
        target.mkdir()
        (target / "plus1.service").symlink_to(original)
        units.write(target, {"plus1.service": "new"})
        self.assertFalse((target / "plus1.service").is_symlink())
        self.assertEqual(
            ((target / "plus1.service").read_text(), original.read_text()), ("new", "old")
        )

    def test_install_out_writes_files_without_loading_them(self):
        out = self.dir / "out"
        self.assertEqual(cli.main(["install", "--out", str(out), "--no-web"]), 0)
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(
            names, ["local.plus1.tick.plist"] if host.MAC else ["plus1.service", "plus1.timer"]
        )

    def test_the_next_check_comes_from_the_working_window(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from plus1.web import queries

        cfg = config.load()
        zone = ZoneInfo(cfg.timezone)
        self.assertEqual(
            queries.next_check(cfg, datetime(2026, 10, 15, 9, 10, tzinfo=zone)), "09:30"
        )
        self.assertEqual(
            queries.next_check(cfg, datetime(2026, 10, 16, 18, 5, tzinfo=zone)), "Mon 07:00"
        )
        # The timer keeps this machine's clock; a zone 45 minutes off shows the slot in the configured zone.
        cfg.timezone = "Asia/Kathmandu"
        self.assertEqual(
            queries.next_check(cfg, datetime(2026, 10, 15, 9, 10, tzinfo=ZoneInfo("UTC"))), "15:15"
        )

    def test_a_timer_only_install_takes_down_an_earlier_web_app(self):
        host.MAC = False
        host.systemd = lambda: True
        target = self.dir / "units"
        target.mkdir()
        (target / "plus1-web.service").write_text("old")
        units.subprocess.run, saved = self.capture(), units.subprocess.run
        saved_target, units.target = units.target, lambda: target
        try:
            units.install(config.load(), web=False)
        finally:
            units.subprocess.run, units.target = saved, saved_target
        self.assertFalse((target / "plus1-web.service").exists())
        self.assertIn(
            ["systemctl", "--user", "disable", "--now", "plus1-web.service"],
            [list(c) for c, _ in self.ran],
        )
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["plus1.service", "plus1.timer"])

    def test_macos_install_replaces_a_running_agent(self):
        host.MAC = True
        units.subprocess.run, saved = self.capture(), units.subprocess.run
        saved_target, units.target = units.target, lambda: self.dir / "agents"
        try:
            cfg = config.load()
            cfg.state_dir = self.dir / "state"
            paths = units.install(cfg, web=False)
        finally:
            units.subprocess.run, units.target = saved, saved_target
        domain = f"gui/{os.getuid()}"
        self.assertEqual(
            [list(c) for c, _ in self.ran],
            [
                ["launchctl", "bootout", f"{domain}/local.plus1.web"],
                ["launchctl", "bootout", f"{domain}/local.plus1.tick"],
                ["launchctl", "bootstrap", domain, str(paths[0])],
            ],
        )
        self.assertTrue((self.dir / "state" / "logs").is_dir())


if __name__ == "__main__":
    unittest.main()
