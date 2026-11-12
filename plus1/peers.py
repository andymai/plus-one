import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _git(cwd, *args, timeout=600):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def root(cfg):
    return cfg.state_dir / "peers"


def _default_branch(repo, log):
    # Asks origin, since the checkout's origin/HEAD keeps whatever the default branch was when it was cloned.
    try:
        head = _git(repo, "ls-remote", "--symref", "origin", "HEAD", timeout=120).split("\t", 1)[0]
        if not head.startswith("ref: refs/heads/"):
            raise RuntimeError(f"origin's HEAD is {head[:60]!r}")
        branch = head.removeprefix("ref: refs/heads/")
        _git(
            repo,
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            timeout=120,
        )
        return branch
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD").removeprefix("origin/")
        log(f"peer {repo.name}: fetch failed, reading the last fetched {branch}: {exc}")
        return branch


def _snapshot(cfg, repo, log):
    tree = root(cfg) / repo.name
    try:
        branch = _default_branch(repo, log)
        # Its own repo and object store, holding only origin's branches, so nothing in it leads to the branches,
        # stashes, or edits in the owner's checkout.
        if not (tree / ".git").is_dir():
            shutil.rmtree(tree, ignore_errors=True)
            _git(repo, "init", "--quiet", str(tree))
        ref = f"refs/remotes/origin/{branch}"
        _git(tree, "fetch", "--no-tags", "--quiet", str(repo), f"+{ref}:{ref}")
        _git(tree, "checkout", "--quiet", "--detach", "--force", f"origin/{branch}")
        sha, date = _git(tree, "log", "-1", "--format=%H %cs").split()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        log(f"peer {repo.name}: left out: {exc}")
        return None
    return {"name": repo.name, "path": tree, "branch": branch, "sha": sha, "date": date}


def refresh(cfg, log):
    repos = {}
    for repo in (Path(r).expanduser() for r in cfg.peers):
        if repo.name in repos:
            log(f"peer {repo.name}: left out, {repos[repo.name]} already has that name")
        else:
            repos[repo.name] = repo
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [p for p in pool.map(lambda repo: _snapshot(cfg, repo, log), repos.values()) if p]
