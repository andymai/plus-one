import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

from . import host, prs


class SlackUnavailable(RuntimeError):
    pass


def extract(blob, repo):
    pattern = rf"(?:github\.com/{re.escape(repo)}/pull|app\.graphite\.com/github/pr/{re.escape(repo)})/(\d+)"
    return {int(n) for n in re.findall(pattern, blob)}


def _paths(cfg):
    value = cfg.slack.token_path or []
    return [os.path.expanduser(p) for p in ([value] if isinstance(value, str) else value) if p]


def _stored(cfg):
    """Each copy of the Slack plugin's token that Claude Code holds: one per token file (one per Claude login that uses
    Slack), or on macOS with no file, the Keychain's."""
    paths = _paths(cfg)
    if not paths:
        return [_keychain(cfg)["mcpOAuth"][cfg.slack.token_key]]
    found = []
    for path in paths:
        try:
            with open(path) as f:
                entry = json.load(f).get("mcpOAuth", {}).get(cfg.slack.token_key)
        except (OSError, json.JSONDecodeError):
            continue
        if entry:
            found.append(entry)
    if not found:
        raise SlackUnavailable(f"no {cfg.slack.token_key} token in {', '.join(paths)}")
    return found


def _keychain(cfg):
    if not host.MAC:
        raise SlackUnavailable(
            "set slack.token_path to the Claude credentials file that holds the Slack token"
        )
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", cfg.slack.keychain_service, "-w"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise SlackUnavailable(
            f"no {cfg.slack.keychain_service!r} item in the Keychain: {proc.stderr.strip()[:200]}"
        )
    return json.loads(proc.stdout)


def _token(cfg):
    # Slack revokes a person's older tokens for the app when it issues a new one, so of the copies two Claude logins
    # hold, only the one issued last works, and it expires last.
    entry = max(_stored(cfg), key=lambda e: e.get("expiresAt", 0))
    if entry["expiresAt"] / 1000 < time.time() + 60:
        raise SlackUnavailable(
            "stored Slack token has expired; an interactive Claude session refreshes it"
        )
    return entry["accessToken"]


def _history(token, channel, oldest):
    messages, cursor = [], None
    for _ in range(3):
        params = {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        request = urllib.request.Request(
            "https://slack.com/api/conversations.history",
            data=urllib.parse.urlencode(params).encode(),
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed https URL)
            body = json.load(response)
        if not body.get("ok"):
            raise SlackUnavailable(f"slack {channel}: {body.get('error')}")
        messages += body["messages"]
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return messages


def announced(cfg):
    token = _token(cfg)
    oldest = time.time() - cfg.slack.lookback_hours * 3600
    found = {}
    for name, channel in vars(cfg.slack.channels).items():
        for message in _history(token, channel, oldest):
            blob = json.dumps(message)
            for repo in cfg.repos:
                for number in extract(blob, repo.name):
                    found.setdefault(prs.pid(repo.name, number), set()).add(name)
    return found
