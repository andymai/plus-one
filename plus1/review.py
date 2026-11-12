import contextlib
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import claude, config, diffs, filters, prompts
from .store import decode_json

SEVERITY = ("P0", "P1")
FOLLOWUP_UNCHANGED = "unchanged since you asked for changes"
FOLLOWUP_OPEN = "still open since you asked for changes: "
LABEL = re.compile(r"^(?:\*\*)?\w+(?: \([^)]*\))?:(?:\*\*)?\s*")


class Reviewed:
    def __init__(self):
        self.verdict = "DEFER"
        self.reason = ""
        self.findings = []
        self.checks = []
        self.p2_notes = []
        self.cost = 0.0
        self.diff = ""
        self.diff_files = {}
        self.lane = ""
        self.raw = None
        self.followup: dict | None = None


def _git(cfg, *args, cwd=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd or cfg.checkout, capture_output=True, text=True, timeout=600
    )
    if proc.returncode != 0:
        # The error is at the end: worktree add and fetch print progress first.
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def sweep_worktrees(cfg):
    work = cfg.state_dir / "work"
    for child in work.glob("pr-*"):
        shutil.rmtree(child, ignore_errors=True)
    checkouts = (
        [config.view(cfg, r.name).checkout for r in cfg.repos]
        if hasattr(cfg, "repos")
        else [cfg.checkout]
    )
    for checkout in dict.fromkeys(c for c in checkouts if c):
        subprocess.run(["git", "worktree", "prune"], cwd=checkout, capture_output=True)


def _setup(cfg, pr):
    work = cfg.state_dir / "work" / f"pr-{pr.get('id', pr['number'])}-{pr['head'][:8]}"
    tree = work / "tree"
    work.mkdir(parents=True, exist_ok=True)
    _git(cfg, "fetch", "--no-tags", "origin", pr["base"], f"pull/{pr['number']}/head")
    _git(cfg, "worktree", "add", "--quiet", "--detach", str(tree), pr["head"])
    diff = _git(cfg, "diff", "--no-color", "-U5", f"origin/{pr['base']}...{pr['head']}", cwd=tree)
    diff_path = work / "pr.diff"
    diff_path.write_text(diff)
    return work, tree, diff_path, diff


def leftovers(path):
    """Our processes still running from inside path."""
    root, me = os.path.realpath(path), os.getpid()

    def inside(cwd):
        return cwd == root or cwd.startswith(root + os.sep)

    if os.path.isdir("/proc"):
        found = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                if (
                    int(pid) != me
                    and os.stat(f"/proc/{pid}").st_uid == os.getuid()
                    and inside(os.readlink(f"/proc/{pid}/cwd"))
                ):
                    found.append(int(pid))
            except OSError:
                continue
        return found
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpn"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found, pid = [], None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid and pid != me and inside(line[1:]):
            found.append(pid)
    return found


def stop_leftovers(path, grace_s=3):
    # A check can leave a daemon behind (Spring, for one), which would outlive the worktree it serves.
    pids = leftovers(path)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        deadline = time.monotonic() + grace_s
        while pids and time.monotonic() < deadline:
            pids = [p for p in pids if _alive(p)]
            time.sleep(0.1)
        if not pids:
            return


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A killed child of ours stays a zombie until reaped, which still answers signal 0.
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def _teardown(cfg, work, tree):
    stop_leftovers(tree)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(tree)], cwd=cfg.checkout, capture_output=True
    )
    shutil.rmtree(work, ignore_errors=True)


def _event(rec, stage, message):
    if rec:
        rec.event(stage, message)


def _carried(row):
    return {
        "severity": row["severity"],
        "label": row["label"],
        "title": row["title"],
        "path": row["final_path"] or row["path"],
        "line": row["final_line"] or row["line"],
        "evidence": row["evidence"],
        "impact": row["impact"],
        "suggestion": row["suggestion"],
        "confidence": row["confidence"],
        "raised_by": row.get("raised_by") or "",
        "carried": True,
    }


def _headline(body):
    for line in prompts._strip(body).splitlines():
        line = LABEL.sub("", line.strip(), count=1).strip()
        if line:
            return line if len(line) <= 140 else line[:140].rsplit(" ", 1)[0] + "…"
    return "your comment"


def _asked_item(body, path, line, thread_id, url, resolved=False, resolved_by=None):
    return {
        "severity": "P0",
        "label": "issue",
        "title": _headline(body),
        "path": path,
        "line": line,
        "evidence": prompts._strip(body).strip()[:1500],
        "impact": "",
        "suggestion": "",
        "confidence": "high",
        "raised_by": "",
        "carried": True,
        "origin": "owner",
        "thread_id": thread_id,
        "url": url,
        "resolved": resolved,
        "resolved_by": resolved_by,
    }


def _asked(followup):
    items = [
        _asked_item(
            t["body"],
            t["path"],
            t["line"] or t["original_line"] or 0,
            t["id"],
            t["url"],
            t.get("resolved", False),
            t.get("resolved_by"),
        )
        for t in followup["threads"]
    ]
    if followup["body"]:
        items.append(_asked_item(followup["body"], "", 0, None, None))
    return items


UNCHECKED = "More than 100 review threads, so some of your comments weren't checked"


def _followup_record(followup, items, outcome):
    asks = [
        {
            "thread_id": f["thread_id"],
            "url": f["url"],
            "path": f["path"],
            "line": f["line"],
            "title": f["title"],
            "resolved": f.get("resolved", False),
            "resolved_by": f.get("resolved_by"),
            "open": outcome.get(n, (True, ""))[0],
            "why": outcome.get(n, (True, ""))[1],
        }
        for n, f in enumerate(items, 1)
        if f.get("origin") == "owner"
    ]
    if followup.get("incomplete"):
        # Asks past GitHub's first 100 threads were never seen, so the card can't call everything fixed.
        asks.append(
            {
                "thread_id": None,
                "url": None,
                "path": "",
                "line": 0,
                "title": UNCHECKED,
                "open": True,
                "why": "Check the rest on GitHub.",
            }
        )
    return {"sha": followup["sha"], "items": asks}


CHECK_OUTPUT_CHARS = 4000
EXIT_GRACE_S = 0.5


def changed_files(pr, tree):
    # GitHub lists a PR's first 100 files, and git has them all.
    if not pr["files_truncated"]:
        return pr["files"]
    proc = subprocess.run(
        ["git", "diff", "--name-only", "-z", "--no-renames", f"origin/{pr['base']}...{pr['head']}"],
        cwd=tree,
        capture_output=True,
        timeout=120,
    )
    return (
        [os.fsdecode(n) for n in proc.stdout.split(b"\0") if n]
        if proc.returncode == 0
        else pr["files"]
    )


def checked_files(pr, tree):
    # File names come from the PR, so one that starts with a dash could pass itself as a flag, and a deleted file
    # isn't there to check.
    return [
        f for f in changed_files(pr, tree) if not f.startswith("-") and (Path(tree) / f).exists()
    ]


def _run_check(argv, tree, env, timeout):
    # Only the tail of the output is kept as it arrives, so a check that prints without end fills neither memory nor
    # disk.
    try:
        proc = subprocess.Popen(
            argv,
            cwd=tree,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        return None, f"couldn't start: {exc}"
    out = proc.stdout
    if out is None:
        raise RuntimeError("Popen with stdout=PIPE gave no pipe")
    tail, keep = bytearray(), CHECK_OUTPUT_CHARS * 4
    deadline = time.monotonic() + timeout
    exited = None
    with out:
        fd = out.fileno()
        while (at := time.monotonic()) < deadline:
            if exited is None and proc.poll() is not None:
                exited = at
            # Once it exits, a moment more for what it printed last. Anything it left running that still holds the
            # pipe, printing or not, can't keep the review waiting.
            if exited is not None and at - exited > EXIT_GRACE_S:
                break
            ready, _, _ = select.select(
                [fd], [], [], 0.1 if exited is not None else min(0.5, deadline - at)
            )
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                tail += chunk
                del tail[:-keep]
            elif exited is not None:
                break
    try:
        code, note = proc.wait(timeout=max(0, deadline - time.monotonic())), ""
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        code, note = None, f"timed out after {timeout}s"
    text = tail.decode(errors="replace").strip()[-CHECK_OUTPUT_CHARS:]
    return code, "\n".join(part for part in (text, note) if part)


def run_checks(cfg, repo, pr, tree, rec=None):
    """The repo's opted-in checks, run by Plus One with fixed arguments so the reviewer never chooses what runs."""
    results, files = [], checked_files(pr, tree)
    for command in repo.checks:
        words = shlex.split(command)
        if "{files}" in words and not files:
            continue
        argv = [part for w in words for part in (files if w == "{files}" else [w])]
        _event(rec, "checks", f"running {command}")
        code, output = _run_check(argv, tree, claude._env(cfg), cfg.limits.check_timeout_s)
        results.append({"command": command, "exit": code, "output": output})
    return results


CARRIED_NOTE = "patch unchanged since "


def _carries(last, pr, lane):
    if last["verdict"] == "DEFER":
        # A hold for open comments depends on a discussion that may have moved, so it gets a fresh look.
        return not (last["reason"] or "").startswith("clean, but open comments from ")
    # An approval skips the model, so it has to clear the gates a fresh one would: the lane still allows it, and nobody
    # who commented could be owed an answer, which only the model can judge.
    return last["verdict"] == "APPROVE" and lane != "never_approve" and not filters.could_hold(pr)


def _carry(result, last, lane):
    result.verdict, result.reason = last["verdict"], last["reason"]
    result.raw = decode_json(last["raw_review"], {}) or {}
    result.lane = lane or last["lane"]
    notes = [n for n in decode_json(last["p2_notes"], []) or [] if not n.startswith(CARRIED_NOTE)]
    result.p2_notes = [*notes, f"{CARRIED_NOTE}{last['sha'][:10]}, so that review carries over"]
    for f in last["findings"]:
        result.checks.append(
            {
                "finding": {**f, "carried": f.get("carried")},
                "verified": False,
                "reason": f["verify_reason"],
                "path": f["final_path"] or f["path"],
                "line": f["final_line"] or f["line"],
            }
        )
    return result


def _changes(base, sha, cwd):
    # Blob hashes and modes, read as bytes, so no way of writing a line can make two different patches compare equal.
    def git(*args):
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()[:300]}"
            )
        return proc.stdout

    fork = git("merge-base", f"origin/{base}", sha).strip().decode()
    parts = git("diff", "--raw", "-z", "--no-abbrev", "--no-renames", fork, sha).split(b"\0")
    return dict(zip(parts[1::2], parts[0::2], strict=False))


def _moved(cfg, pr, last_sha, tree):
    try:
        before, after = _changes(pr["base"], last_sha, tree), _changes(pr["base"], pr["head"], tree)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    return sorted(
        os.fsdecode(p) for p in before.keys() | after.keys() if before.get(p) != after.get(p)
    )


def _since(cfg, pr, last, changed, tree, work):
    raw = decode_json(last["raw_review"], {}) or {}
    if last["verdict"] not in ("APPROVE", "DRAFT") or raw.get("coverage") != "thorough":
        return None
    try:
        delta = _git(
            cfg, "diff", "--no-color", "-U5", last["sha"], pr["head"], "--", *changed, cwd=tree
        )
    except RuntimeError:
        return None
    path = work / "since.diff"
    path.write_text(delta)
    return {
        "sha": last["sha"],
        "verdict": last["verdict"],
        "summary": raw.get("summary") or last["reason"] or "",
        "files": changed,
        "path": path,
    }


def run(cfg, pr, lane, lane_reason, rec=None, last=None, peers=()):
    result = Reviewed()
    followup = pr.get("followup")
    prior_sha = last["sha"] if last else ""
    # The owner's own comments are tracked through the follow-up, never redrafted as Plus One's findings.
    prior = [
        r for r in (last or {}).get("findings", []) if r["verified"] and r.get("origin") != "owner"
    ]
    # The owner declined to post a waived one, so it never comes back as a claim of Plus One's own, even as a later
    # review's copy of it.
    waived = (last or {}).get("waived") or []
    declined = {w["title"] for w in waived}
    prior = [
        r for r in prior if r.get("draft_status") != "dismissed" and r["title"] not in declined
    ]
    if followup:
        # A posted finding is one of the owner's threads now, so only the ones still unposted carry on their own.
        prior = [r for r in prior if r.get("draft_status") != "released"]
    earlier = (_asked(followup) if followup else []) + [_carried(r) for r in prior]
    large = filters.too_large(pr, cfg)
    system = prompts.SYSTEM.format(
        rubric=config.rubric_text(cfg), name=cfg.name, me=cfg.me, repo=pr["repo"]
    )
    _event(rec, "setup", "checking out the PR head")
    work, tree, diff_path, diff = _setup(cfg, pr)
    readable = [work, *(p["path"] for p in peers)]
    try:
        result.diff = diff
        result.diff_files = diffs.right_lines(diff)
        since = None
        if followup and followup["sha"] and _moved(cfg, pr, followup["sha"], tree) == []:
            # A rebase that leaves the PR's own files alone can't have fixed anything, so there's nothing to show yet.
            _event(
                rec,
                "carry",
                f"nothing changed since changes were requested at {followup['sha'][:8]}",
            )
            result.verdict, result.reason = "DEFER", FOLLOWUP_UNCHANGED
            result.lane = lane or (last or {}).get("lane") or ""
            result.followup = _followup_record(followup, earlier, {})
            return result
        # An explicit review at the same commit always runs in full, and so does a PR retargeted to another base,
        # whose diff the earlier review never saw.
        if (
            last
            and last["sha"] != pr["head"]
            and last.get("diff")
            and last.get("base") == pr["base"]
        ):
            moved = _moved(cfg, pr, last["sha"], tree)
            if moved == [] and not followup and _carries(last, pr, lane):
                _event(
                    rec, "carry", f"patch unchanged since {last['sha'][:8]}, keeping that review"
                )
                return _carry(result, last, lane)
            # moved names exactly which files changed; the text interdiff only sizes the change.
            if (
                moved
                and not large
                and diffs.interdiff(last["diff"], diff)[1] < cfg.limits.incremental_max_lines
            ):
                since = _since(cfg, pr, last, moved, tree, work)
        if lane is None:
            _event(rec, "triage", "classifying")
            triage = claude.timed(
                rec,
                cfg,
                "triage",
                "triage",
                0,
                prompts.triage_prompt(pr),
                prompts.TRIAGE_SCHEMA,
                tools=False,
            )
            result.cost += triage.cost
            lane = triage.data["lane"]
        result.lane = lane

        checks = run_checks(cfg, config.repo(cfg, pr["repo"]), pr, tree, rec)
        kind = (
            "mapping a large PR"
            if large
            else "reviewing what changed since the last review"
            if since
            else "deep review"
        )
        _event(rec, "deep", f"{kind} ({lane})")
        review = claude.timed(
            rec,
            cfg,
            "deep",
            "deep",
            0,
            prompts.review_prompt(
                pr,
                lane,
                lane_reason,
                diff_path,
                cfg.me,
                cfg.name,
                large=bool(large),
                earlier=earlier,
                earlier_sha=prior_sha,
                since=since,
                peers=peers,
                tree=tree,
                followup_sha=followup["sha"] if followup else None,
                waived=waived,
                checks=checks,
            ),
            prompts.MAP_SCHEMA if large else prompts.REVIEW_SCHEMA,
            cwd=tree,
            add_dirs=readable,
            system=system,
        )
        result.cost += review.cost
        data = review.data
        if since:
            data["since"] = since["sha"]
        result.raw = data
        result.p2_notes = list(data["p2_notes"])

        raisers = filters.thread_authors(pr)
        for finding in [*data["findings"], *earlier]:
            if finding.get("raised_by") not in raisers:
                finding["raised_by"] = ""
        continued = set()
        for finding in data["findings"]:
            n = finding.get("continues") or 0
            if not 1 <= n <= len(earlier):
                finding["continues"] = 0
                continue
            continued.add(n)
            before = earlier[n - 1]
            if SEVERITY.index(before["severity"]) < SEVERITY.index(finding["severity"]):
                finding["severity"] = before["severity"]
            finding["raised_by"] = finding["raised_by"] or before["raised_by"]
            if before.get("origin") == "owner":
                finding.update(origin="owner", thread_id=before["thread_id"], url=before["url"])
        claims = data["findings"]
        repeats = [f for f in claims if f["title"] in declined and not f["continues"]]
        result.p2_notes += [f"you chose not to post this earlier: {f['title']}" for f in repeats]
        claims = [f for f in claims if f not in repeats]
        if followup:
            # The owner's open threads are checked one by one below, so a new finding restating one is a duplicate.
            repeats = [f for f in claims if f["raised_by"] == cfg.me and not f["continues"]]
            result.p2_notes += [f"already one of your open comments: {f['title']}" for f in repeats]
            claims = [f for f in claims if f not in repeats]

        def verify(finding):
            i = len(result.checks) + 1
            _event(rec, "verify", f"verifying finding {i}")
            check = claude.timed(
                rec,
                cfg,
                "verify",
                "verify",
                i,
                prompts.verify_prompt(pr, finding, peers, tree),
                prompts.VERIFY_SCHEMA,
                cwd=tree,
                add_dirs=readable,
                system=system,
            )
            result.cost += check.cost
            result.checks.append({"finding": dict(finding), **check.data})
            if check.data["verified"]:
                finding["path"], finding["line"] = check.data["path"], check.data["line"]
                finding["verified_reason"] = check.data["reason"]
                finding["check_index"] = i - 1
                result.findings.append(finding)
            else:
                note = (
                    "fixed since you asked for changes"
                    if finding.get("origin") == "owner"
                    else "fixed since the last review"
                    if finding.get("carried")
                    else "dropped unverified"
                )
                result.p2_notes.append(f"{note}: {finding['title']} ({check.data['reason']})")
            return check.data

        held, outcome = set(), {}
        for finding in claims:
            check = verify(finding)
            if check["verified"] and finding["continues"]:
                held.add(finding["continues"])
                outcome[finding["continues"]] = (True, check["reason"])
        # A continuation can restate the earlier claim more narrowly, so its failing says nothing about the
        # original. Every earlier finding no verified continuation holds is checked again as first written.
        for n, finding in enumerate(earlier, 1):
            if n not in held:
                check = verify(finding)
                outcome[n] = (check["verified"], check["reason"])

        still = [f["title"] for f in result.findings if f.get("origin") == "owner"]
        if followup and followup.get("incomplete"):
            still.append(UNCHECKED)
        result.findings = [f for f in result.findings if f.get("origin") != "owner"]
        if followup:
            result.followup = _followup_record(followup, earlier, outcome)

        if result.findings:
            result.verdict = "DRAFT"
            fresh = any(not (f.get("carried") or f.get("continues")) for f in result.findings)
            result.reason = (
                data["summary"]
                if fresh
                else f"still open from the last review: {result.findings[0]['title']}"
            )
        elif still:
            result.reason = f"{FOLLOWUP_OPEN}{still[0]}"
        elif large:
            result.reason = large
        elif data["verdict"] == "APPROVE" and data["coverage"] == "thorough":
            result.verdict = "APPROVE"
            result.reason = data["summary"]
        elif data["verdict"] == "APPROVE":
            result.reason = f"thin coverage: {data['summary']}"
        elif data["findings"]:
            result.reason = f"findings did not survive verification: {data['summary']}"
        else:
            result.reason = data["summary"]

        if lane == "never_approve" and result.verdict == "APPROVE":
            result.verdict = "DEFER"
            result.reason = f"clean, but {lane_reason}"
        waiting = filters.waiting_on(pr, data.get("waiting_on") or [])
        if result.verdict == "APPROVE" and waiting:
            result.verdict = "DEFER"
            result.reason = f"clean, but open comments from {', '.join(waiting)}"
    finally:
        _teardown(cfg, work, tree)
    return result
