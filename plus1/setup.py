"""bin/plus1 setup: writes your config and rubric, learns your rubric, installs the services, and signs you in."""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import codeowners, config, gh, host
from .team import size, suggest_mine

ROOTS = (
    "~/code",
    "~/src",
    "~/Git",
    "~/git",
    "~/projects",
    "~/dev",
    "~/work",
    "~/repos",
    "~/Developer",
    "~",
)
# Team names that usually guard something a reviewer shouldn't approve alone. Setup only suggests; you choose.
GATE_WORDS = re.compile(
    r"(?i)secur|appsec|\bdb\b|database|dba|payment|billing|financ|finops|money|infra|\bsre\b|legal|"
    r"privacy|complian|\bauth|identity"
)
GATE_CHECK = re.compile(r"(?i)^require (.+) approval$")
RECENT_CHECKS = """
query($owner:String!,$name:String!){repository(owner:$owner,name:$name){pullRequests(last:30,states:[OPEN,MERGED]){nodes{
 commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){nodes{__typename ... on CheckRun{name}
 ... on StatusContext{context}}}}}}}}}}}
"""


class Prompt:
    def __init__(self, ask=input, say=print):
        self.ask, self.say = ask, say

    def text(self, question, default=""):
        answer = self.ask(f"{question}{f' [{default}]' if default else ''}: ").strip()
        return answer or default

    def yes(self, question, default=True):
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer.startswith("y")

    def pick(self, question, options, chosen=()):
        """Numbered choices; the answer is the numbers to keep, comma separated."""
        if not options:
            return []
        self.say(question)
        for i, (value, note) in enumerate(options, 1):
            self.say(f"  {i}. {value}{f'  ({note})' if note else ''}")
        default = ",".join(str(i) for i, (value, _) in enumerate(options, 1) if value in chosen)
        answer = self.text("Numbers to keep, comma separated, or none", default or "none")
        if answer.strip().lower() == "none":
            return []
        keep = {int(n) for n in re.findall(r"\d+", answer)}
        return [value for i, (value, _) in enumerate(options, 1) if i in keep]


def who():
    user = json.loads(gh.run(["api", "user"]))
    return user["login"], (user.get("name") or user["login"]).split()[0]


def suggest_repos(limit=8):
    """Repos you're asked to review in, most requests first, then ones you reviewed in the last 90 days."""
    counts = {}
    since = (datetime.now() - timedelta(days=90)).date().isoformat()
    for args in (
        ["--review-requested=@me", "--state=open"],
        ["--reviewed-by=@me", f"--updated=>={since}"],
    ):
        out = gh.run(
            ["search", "prs", *args, "--limit", "200", "--json", "repository"], timeout=120
        )
        for item in json.loads(out):
            name = item["repository"]["nameWithOwner"]
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _origin(path):
    proc = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def matches(url, repo):
    return bool(re.search(rf"github\.com[:/]{re.escape(repo)}(\.git)?/?$", url, re.I))


def find_checkout(repo, roots=None):
    """A clone of repo under the usual code folders, at most two levels down."""
    name = repo.split("/")[1]
    seen = set()
    for root in (Path(r).expanduser() for r in roots or ROOTS):
        for candidate in (root / name, *(p / name for p in root.glob("*") if p.is_dir())):
            if candidate in seen or not (candidate / ".git").exists():
                continue
            seen.add(candidate)
            if matches(_origin(candidate), repo):
                return candidate
    return None


def gate_teams(repo):
    """Teams to suggest for the never-approve lane, with why: an approval-gate check names them, or a CODEOWNERS team
    whose name reads like a gate."""
    found = {}
    owner, name = repo.split("/")
    try:
        out = gh.run(
            [
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-f",
                f"query={RECENT_CHECKS}",
            ],
            timeout=120,
        )
        prs = json.loads(out)["data"]["repository"]["pullRequests"]["nodes"]
        for pr in prs:
            for commit in pr["commits"]["nodes"]:
                rollup = commit["commit"]["statusCheckRollup"] or {"contexts": {"nodes": []}}
                for check in rollup["contexts"]["nodes"]:
                    m = GATE_CHECK.match(check.get("name") or check.get("context") or "")
                    if m:
                        found[m.group(1).strip()] = f"a check is named {m.group(0)!r}"
    except Exception:  # noqa: S110 (a failed lookup only means fewer suggestions)
        pass
    try:
        text = gh.run(
            [
                "api",
                f"repos/{repo}/contents/.github/CODEOWNERS",
                "-H",
                "Accept: application/vnd.github.raw",
            ]
        )
    except Exception:
        text = ""
    paths = {}
    for _, owners in codeowners.parse(text):
        for team in owners:
            paths[team] = paths.get(team, 0) + 1
    for team, n in paths.items():
        if team not in found and GATE_WORDS.search(team):
            found[team] = f"owns {n} CODEOWNERS pattern{'s' if n != 1 else ''}"
    return sorted(found.items())


def slack_key(path):
    """The Claude Slack plugin's entry in a Claude credentials file, when there is one."""
    try:
        stored = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return ""
    return next((k for k in stored.get("mcpOAuth", {}) if k.startswith("plugin:slack")), "")


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    # A JSON string is a TOML basic string, as long as text outside ASCII stays literal: JSON's surrogate-pair escapes
    # aren't valid TOML.
    return json.dumps(str(value), ensure_ascii=False)


def render(answers):
    """The personal config.toml for a set of answers, commented the way config.example.toml is."""
    lines = [
        "# Written by bin/plus1 setup. Anything in plus1/defaults.toml can be overridden here.",
        "# Lanes and filters only add to the defaults. Your rubric is rubric.md beside this file.",
        "",
        f"me = {_toml(answers['me'])}",
        f"name = {_toml(answers['name'])}",
        f"timezone = {_toml(answers['timezone'])}",
        f"notify = {_toml(answers['notify'])}",
        "",
        "[window]",
        f"days = {_toml(answers['days'])}",
        f"start_hour = {answers['start_hour']}",
        f"end_hour = {answers['end_hour']}",
    ]
    if answers.get("team"):
        lines += ["", "[team]", f"teams = {_toml(answers['team'])}"]
    for repo in answers["repos"]:
        lines += [
            "",
            "[[repos]]",
            f"name = {_toml(repo['name'])}",
            f"checkout = {_toml(repo['checkout'])}",
        ]
        if repo.get("checks"):
            lines.append("# These run code from the PR's branch before each review.")
            lines.append(f"checks = {_toml(repo['checks'])}")
        if repo.get("never_approve_owners"):
            lines += [
                "",
                "[repos.lanes]",
                f"never_approve_owners = {_toml(repo['never_approve_owners'])}",
            ]
    slack = answers.get("slack")
    lines += ["", "[slack]", f"enabled = {_toml(bool(slack))}"]
    if slack:
        if slack.get("token_path"):
            lines.append(f"token_path = {_toml(slack['token_path'])}")
        lines.append(f"token_key = {_toml(slack['token_key'])}")
        lines += ["", "[slack.channels]"] + [
            f"{_toml(n)} = {_toml(c)}" for n, c in slack["channels"].items()
        ]
    web = answers.get("web") or {}
    if web.get("allowed_login"):
        lines += [
            "",
            "[web]",
            'mode = "tailscale"',
            f"allowed_login = {_toml(web['allowed_login'])}",
        ]
        if web.get("public_url"):
            lines.append(f"public_url = {_toml(web['public_url'])}")
    return "\n".join(lines) + "\n"


def _tilde(path):
    home = str(Path.home())
    path = str(path)
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def ask_until(prompt, question, default, parse, problem):
    while True:
        value = parse(prompt.text(question, default))
        if value is not None:
            return value
        prompt.say(problem)


def _zone(text):
    try:
        ZoneInfo(text)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return text


def _hour(text):
    return int(text) if text.strip().isdigit() and 0 <= int(text) <= 23 else None


def ask_repos(prompt, login):
    prompt.say("Looking for the repos you review...")
    try:
        suggested = suggest_repos()
    except Exception as exc:
        prompt.say(
            f"Couldn't search GitHub for suggestions ({str(exc)[:120]}), so type the repos below."
        )
        suggested = []
    picked = prompt.pick(
        "Which repos should Plus One review for you?",
        [(name, f"{n} recent PR{'s' if n != 1 else ''}") for name, n in suggested],
        chosen=[name for name, _ in suggested[:1]],
    )
    extra = prompt.text("Any other repos, as owner/repo separated by spaces", "")
    repos = list(dict.fromkeys(picked + extra.split()))
    out = []
    for name in repos:
        if name.count("/") != 1:
            prompt.say(f"Skipping {name!r}: it needs to look like owner/repo.")
            continue
        found = find_checkout(name)
        checkout = None
        while checkout is None:
            answer = prompt.text(
                f"Local clone of {name} (Plus One adds worktrees to it, never touches your branches; "
                "type skip to leave this repo out)",
                _tilde(found) if found else "",
            )
            if not answer or answer.strip().lower() == "skip":
                break
            if matches(_origin(Path(answer).expanduser()), name):
                checkout = Path(answer).expanduser()
            else:
                prompt.say(
                    f"{answer} isn't a clone of {name}. Clone it with: gh repo clone {name} <dir>"
                )
        if checkout is None:
            prompt.say(f"Skipping {name} for now.")
            continue
        teams = gate_teams(name)
        lanes = prompt.pick(
            f"PRs these teams own in {name} will never be approved on their own (you still get the "
            "review). Keep which?",
            teams,
            chosen=[t for t, _ in teams],
        )
        checks = []
        if prompt.yes(
            f"Run a command before each review of {name}, like a linter? It runs code from the PR's branch",
            default=False,
        ):
            checks = [c for c in [prompt.text("Command (use {files} for the changed files)")] if c]
        out.append(
            {
                "name": name,
                "checkout": _tilde(checkout),
                "never_approve_owners": lanes,
                "checks": checks,
            }
        )
    return out


def ask_team(prompt, repos):
    try:
        found = suggest_mine([r["name"] for r in repos])
    except Exception as exc:
        prompt.say(
            f"Couldn't read your GitHub teams ({str(exc)[:120]}). You can add them later in Settings, You."
        )
        return []
    return prompt.pick(
        "Which of your GitHub teams do you work on? Their PRs come first in To do and get reviewed sooner",
        [(name, size(count)) for name, count in found],
    )


def ask_slack(prompt):
    if not prompt.yes(
        "Also review PRs linked in Slack channels? Needs the Claude Code Slack plugin",
        default=False,
    ):
        return None
    token_path = ""
    if host.MAC:
        key = prompt.text(
            "Slack plugin key in your Claude credentials (from the Keychain item Claude Code-credentials)"
        )
    else:
        token_path = prompt.text(
            "Claude credentials file holding the Slack token", "~/.claude/.credentials.json"
        )
        key = prompt.text("Slack plugin key", slack_key(token_path))
    channels = {}
    while True:
        entry = prompt.text("A channel as name=ID (blank when done)", "")
        if not entry:
            break
        label, _, channel = entry.partition("=")
        if channel.strip():
            channels[label.strip()] = channel.strip()
    return (
        {"token_path": token_path, "token_key": key, "channels": channels}
        if key and channels
        else None
    )


def run(prompt=None, learn_first=True):
    prompt = prompt or Prompt()
    path = config.default_path()
    prompt.say(
        "Plus One reviews the PRs waiting on you, drafts comments for you to post, and can approve the clean "
        "ones once you let it.\n"
    )
    login, name = who()
    answers = {
        "me": prompt.text("Your GitHub login", login),
        "name": prompt.text("What drafts call you", name),
    }
    answers["repos"] = ask_repos(prompt, answers["me"])
    if not answers["repos"]:
        prompt.say("Nothing to review yet. Run bin/plus1 setup again once you know which repo.")
        return 1
    answers["team"] = ask_team(prompt, answers["repos"])
    answers["timezone"] = ask_until(
        prompt,
        "Time zone",
        config.system_zone(),
        _zone,
        "That isn't a time zone name like America/New_York.",
    )
    answers["start_hour"] = ask_until(
        prompt, "Start checking at (hour, 0 to 23)", "7", _hour, "Give an hour from 0 to 23."
    )
    answers["end_hour"] = ask_until(
        prompt,
        "Stop checking at (hour, 0 to 23)",
        "17",
        lambda text: _hour(text) if (_hour(text) or 0) >= answers["start_hour"] else None,
        f"Give an hour from {answers['start_hour']} to 23.",
    )
    answers["days"] = [0, 1, 2, 3, 4] if prompt.yes("Weekdays only?", True) else list(range(7))
    answers["notify"] = prompt.yes("Desktop notifications when something needs you?", True)
    answers["slack"] = ask_slack(prompt)
    login_ts = prompt.text(
        "To open the web app from your phone over Tailscale, your tailnet login (blank keeps it on "
        "this computer)",
        "",
    )
    answers["web"] = {
        "allowed_login": login_ts,
        "public_url": prompt.text("The https address tailscale serve gives it", "")
        if login_ts
        else "",
    }
    if path.exists() and not prompt.yes(
        f"{path} exists. Replace it (the old one is kept beside it)?", False
    ):
        return 1
    write(path, render(answers))
    rubric = path.parent / "rubric.md"
    if not rubric.exists():
        shutil.copyfile(config.STARTER_RUBRIC, rubric)
        prompt.say(f"Started your rubric at {rubric} from the generic one.")
    prompt.say("Plus One keeps itself up to date. You can change that in Settings, Updates.")
    cfg = config.load(path)
    if learn_first and prompt.yes(
        "Learn your rubric from the reviews you wrote on GitHub? Reads up to 300 of them "
        "with Claude, about $1",
        True,
    ):
        _learn(cfg, prompt)
    from . import units

    running = False
    if prompt.yes("Run checks every 30 minutes and keep the web app up?", True):
        for installed in units.install(cfg):
            prompt.say(f"installed {installed}")
        running = True
    if running:
        _sign_in(cfg, prompt)
    else:
        prompt.say("\nStart the web app with bin/plus1-web, then sign in with bin/plus1 open.")
    prompt.say(
        "\nNothing posts without you. Each repository's Settings can let it approve clean PRs on its own."
    )
    prompt.say("bin/plus1 doctor checks the setup any time.")
    return 0


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.rename(path.with_name(f"{path.name}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"))
    path.write_text(text)


def _learn(cfg, prompt):
    from . import learn, propose
    from .store import Store

    store = Store(cfg)
    try:
        proposal_id = learn.run(cfg, store, log=prompt.say)
    except Exception as exc:
        prompt.say(f"Learning didn't finish ({exc}). Run bin/plus1 learn to try again.")
        return
    if proposal_id is None:
        prompt.say("No reviews of yours to learn from yet, so the generic rubric stays.")
        return
    prompt.say(learn.diff(store, proposal_id))
    prompt.say(store.query("SELECT rationale FROM proposals WHERE id = ?", proposal_id)[0][0])
    if prompt.yes("Use this rubric?", True):
        propose.accept(cfg, store, proposal_id)
    else:
        prompt.say(
            "Kept the generic one. The learned one waits under Rubric changes in the web app."
        )


def _sign_in(cfg, prompt):
    if cfg.web.mode != "local":
        prompt.say(
            f"Open {cfg.web.public_url or 'the address tailscale serve gives you'} once it's published."
        )
        return
    from . import signin
    from .store import Store

    url = signin.open_in_browser(cfg, Store(cfg))
    prompt.say(
        f"\nOpening Plus One. If no browser opens, use this link once, within {signin.CODE_SECONDS // 60} "
        f"minutes: {url}\nbin/plus1 open makes a new one any time."
    )
