"""Whether a PR is in the owner's own code: they wrote the lines it changes, they keep committing to its files, or its
author asked them by name. Such a PR is reviewed sooner, sorts first in its To do group, and in a repo that approves on
its own waits for the owner (config's hold_yours)."""

import contextlib
import json
import re
import subprocess
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import config, gh, prs

LINES = 20
SHARE = 0.25
# The share counts only from this many lines, so a three-line PR through one of your functions isn't yours.
SHARE_FLOOR = 5
COMMITS = 2
MONTHS = 6
TOP = 5
EMAILS_KEY = "yours_emails"
EMAILS_DAYS = 1
# Rewritten whole by a tool on every install or migration, so whoever ran it last owns nothing there.
GENERATED = re.compile(
    r"(?:^|/)(?:[^/]*\.lock|package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|go\.sum"
    r"|db/(?:\w+_)?schema\.rb|db/(?:\w+_)?structure\.sql|[^/]*\.min\.(?:js|css)|[^/]*\.snap)$"
)
HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+")


def asked(pr, me):
    """Who asked the owner by name to review this PR, or None. A team request, a bot's (auto-assignment), and one the
    owner made themselves don't count."""
    me = me.lower()
    for r in reversed(pr.get("requests") or []):
        if r["to"].lower() == me and r["by"] and r["by"].lower() != me and not r["bot"]:
            return r["by"]
    return None


def kinds(found):
    """Which signals hold: asked (by name), code (lines the owner wrote), files (the owner's recent commits)."""
    found = found or {}
    out = ["asked"] if found.get("asked") else []
    lines, changed = found.get("lines", 0), found.get("changed", 0)
    if lines >= LINES or (lines >= SHARE_FLOOR and lines >= SHARE * changed):
        out.append("code")
    if found.get("files"):
        out.append("files")
    return out


def hold(found):
    """Why an automatic approval waits for the owner, or None. Recent commits alone only move a PR up."""
    found = found or {}
    signals = kinds(found)
    if "asked" in signals:
        return "you were asked by name"
    if "code" in signals:
        return "rewrites your code"
    if found.get("error"):
        return "couldn't tell whose code it changes"
    return None


def detected(cfg, store=None):
    """The addresses found for the owner: git's user.email in each clone, and the authors GitHub matched to the
    owner's recent commits (refreshed once a day by a tick)."""
    found = []
    for r in cfg.repos:
        if r.checkout:
            with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
                found += _git(r.checkout, "config", "--get-all", "user.email").decode().split()
    if store is not None:
        found += json.loads(store.get_meta(EMAILS_KEY) or "{}").get("emails", [])
    return sorted({e.lower() for e in found if "@" in e and e.lower() != "noreply@github.com"})


def refresh_emails(cfg, store, log=print):
    stamp = json.loads(store.get_meta(EMAILS_KEY) or "{}").get("at", "")
    if stamp and datetime.fromisoformat(stamp) > datetime.now().astimezone() - timedelta(
        days=EMAILS_DAYS
    ):
        return
    emails = set()
    for r in cfg.repos:
        try:
            emails |= set(gh.commit_emails(config.view(cfg, r.name)))
        except gh.GhError as exc:
            log(f"couldn't ask GitHub which emails {r.name} knows you by: {exc}")
    store.set_meta(
        EMAILS_KEY,
        json.dumps(
            {
                "emails": sorted(emails),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        ),
    )


def matcher(cfg, store=None):
    emails = {e.lower() for e in getattr(cfg, "emails", [])} | set(detected(cfg, store))
    noreply = re.compile(rf"(?:\d+\+)?{re.escape(cfg.me)}@users\.noreply\.github\.com", re.I)
    return lambda email: email.lower() in emails or bool(noreply.fullmatch(email))


def gather(cfg, store, items, log=print):
    """Set pr["yours"] on each PR: what the owner has to do with it. Git's part is kept per commit, so a PR waiting
    for a review slot isn't measured again; who asked is read fresh from each fetch."""
    todo = {}
    for pr in items:
        found = store.yours(pr.get("id", pr["number"]), pr["head"])
        if found is None:
            todo.setdefault(prs.repo(pr.get("id", pr["number"])), []).append(pr)
        else:
            pr["yours"] = {**found, "asked": asked(pr, cfg.me)}
    mine = matcher(cfg, store) if todo else None
    for repo, prs_ in todo.items():
        checkout = config.view(cfg, repo).checkout
        try:
            refs = {pr["base"] for pr in prs_} | {f"pull/{pr['number']}/head" for pr in prs_}
            _git(checkout, "fetch", "--no-tags", "--quiet", "origin", *sorted(refs), timeout=300)
            with ThreadPoolExecutor(max_workers=4) as pool:
                measured = list(pool.map(_safe, [checkout] * len(prs_), prs_, [mine] * len(prs_)))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            log(f"{repo}: couldn't read whose code its PRs change: {exc}")
            measured = [{"error": str(exc)[:200]}] * len(prs_)
        for pr, found in zip(prs_, measured, strict=True):
            if "error" not in found:
                store.save_yours(pr.get("id", pr["number"]), pr["head"], found)
            pr["yours"] = {**found, "asked": asked(pr, cfg.me)}


def _safe(checkout, pr, mine):
    try:
        return measure(checkout, pr, mine)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)[:200]}


def measure(checkout, pr, mine):
    """The owner's lines among those the PR changes or removes, blamed where the branch started, and the changed files
    they made COMMITS or more commits to in the last MONTHS months on the base branch."""
    base = f"origin/{pr['base']}"
    fork = _git(checkout, "merge-base", base, pr["head"]).strip().decode()
    patch = _git(
        checkout,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "-U0",
        fork,
        pr["head"],
    )
    spans = {p: s for p, s in _old_spans(patch).items() if not GENERATED.search(p)}
    lines, changed = Counter(), 0
    with tempfile.NamedTemporaryFile(suffix=".revs") as ignore:
        revs = _ignore_revs(checkout, fork, ignore)
        for path, ranges in spans.items():
            for email in _blame(checkout, fork, path, ranges, revs):
                changed += 1
                if mine(email):
                    lines[path] += 1
    files = [f for f in pr.get("files") or [] if not GENERATED.search(f)]
    commits = _commits(checkout, base, files, mine) if files else Counter()
    return {
        "lines": sum(lines.values()),
        "changed": changed,
        "top": [list(x) for x in lines.most_common(TOP)],
        "top_count": len(lines),
        "files": [list(x) for x in commits.most_common(TOP) if x[1] >= COMMITS],
        "files_count": sum(1 for n in commits.values() if n >= COMMITS),
    }


def _old_spans(patch):
    """Each file's changed or removed line ranges on the old side of a -U0 patch. A new file has none, and a name git
    had to quote is left out."""
    spans, path = {}, None
    for line in patch.split(b"\n"):
        if line.startswith(b"--- "):
            name = line[4:]
            path = name[2:].decode(errors="replace") if name.startswith(b"a/") else None
        elif line.startswith(b"@@") and path:
            m = HUNK.match(line)
            if m:
                start, count = int(m[1]), int(m[2] if m[2] is not None else 1)
                if count:
                    spans.setdefault(path, []).append((start, count))
    return spans


def _ignore_revs(checkout, fork, handle):
    # Formatting sweeps the repo lists in .git-blame-ignore-revs shouldn't make whoever ran them own every line.
    try:
        handle.write(_git(checkout, "show", f"{fork}:.git-blame-ignore-revs"))
    except RuntimeError:
        return None
    handle.flush()
    return handle.name


def _blame(checkout, fork, path, ranges, revs):
    args = ["blame", "--line-porcelain", "-w"]
    for start, count in ranges:
        args += ["-L", f"{start},+{count}"]
    try:
        out = _git(
            checkout, *args, *(["--ignore-revs-file", revs] if revs else []), fork, "--", path
        )
    except RuntimeError:
        if not revs:
            raise
        out = _git(checkout, *args, fork, "--", path)
    return [
        line[len(b"author-mail <") : -1].decode(errors="replace")
        for line in out.split(b"\n")
        if line.startswith(b"author-mail <")
    ]


def _commits(checkout, base, files, mine):
    out = _git(
        checkout,
        "log",
        f"--since={MONTHS} months ago",
        "--no-merges",
        "--format=%x00%ae",
        "--name-only",
        base,
        "--",
        *files,
    )
    counts, who = Counter(), ""
    for line in out.decode(errors="replace").split("\n"):
        if line.startswith("\0"):
            who = line[1:]
        elif line and mine(who):
            counts[line] += 1
    return counts


def _git(checkout, *args, timeout=120):
    # Literal pathspecs, since PR file names reach git log and a name like :(top) would otherwise be read as magic.
    proc = subprocess.run(
        ["git", "--literal-pathspecs", "-c", "core.quotePath=false", "-C", checkout, *args],
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()[-200:]}"
        )
    return proc.stdout
