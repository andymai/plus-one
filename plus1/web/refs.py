import os
import re
import subprocess
import time
from collections import defaultdict

from markupsafe import Markup, escape

from .. import diffs, prs
from ..store import decode_json

REF = re.compile(r"(?<![\w/.@-])((?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z]{1,6}):(\d+)(?:-(\d+))?")
_index = {"at": 0.0, "checkout": None, "paths": None, "by_name": None}


def anchor(path):
    return re.sub(r"[^A-Za-z0-9]+", "-", path or "")


def checkout_index(checkout, ttl=600):
    if _index["checkout"] == checkout and time.monotonic() - _index["at"] < ttl:
        return _index["paths"], _index["by_name"]
    paths = set()
    if checkout and os.path.isdir(checkout):
        try:
            proc = subprocess.run(
                ["git", "ls-files"], cwd=checkout, capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                paths = set(proc.stdout.splitlines())
        except (OSError, subprocess.SubprocessError):
            paths = set()
    by_name = defaultdict(list)
    for path in paths:
        by_name[path.rsplit("/", 1)[-1]].append(path)
    _index.update(at=time.monotonic(), checkout=checkout, paths=paths, by_name=by_name)
    return paths, by_name


def _common_prefix(a, b):
    n = 0
    for x, y in zip(a.split("/"), b.split("/"), strict=False):
        if x != y:
            break
        n += 1
    return n


class Refs:
    def __init__(self, repo, review_id, sha, files, diff, here=False, index=None):
        self.repo, self.review_id, self.sha, self.here = repo, review_id, sha, here
        self.pr_files = list(files or [])
        self._diff = diff
        self._lines = None
        self._index = index

    @property
    def lines(self):
        if self._lines is None:
            self._lines = diffs.right_lines(self._diff or "")
        return self._lines

    def resolve(self, ref):
        known = self.pr_files + list(self.lines)
        if ref in known:
            return ref
        tail = [p for p in dict.fromkeys(known) if p.endswith("/" + ref)]
        if len(tail) == 1:
            return tail[0]
        paths, by_name = self._index if self._index is not None else ((), {})
        if ref in paths:
            return ref
        if "/" in ref:
            suffix = [p for p in by_name.get(ref.rsplit("/", 1)[-1], []) if p.endswith("/" + ref)]
            return suffix[0] if len(suffix) == 1 else (ref if not paths else None)
        named = by_name.get(ref, [])
        if len(named) == 1:
            return named[0]
        if len(named) > 1 and self.pr_files:
            scored = sorted(
                ((max(_common_prefix(n, f) for f in self.pr_files), n) for n in named), reverse=True
            )
            if scored[0][0] >= 2 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                return scored[0][1]
        return None

    def url(self, ref, line, end=None):
        path = self.resolve(ref)
        if not path:
            return None
        if line in self.lines.get(path, ()):
            target = f"#L-{anchor(path)}-{line}"
            return target if self.here else f"/review/{self.review_id}{target}"
        if not self.sha:
            return None
        span = f"#L{line}" + (f"-L{end}" if end else "")
        return f"https://github.com/{self.repo}/blob/{self.sha}/{path}{span}"

    def linkify(self, html):
        def link(m):
            url = self.url(m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
            if not url:
                return m.group(0)
            external = (
                ' target="_blank" rel="noopener noreferrer"' if url.startswith("https://") else ""
            )
            return f'<a class="ref" href="{escape(url)}"{external}>{m.group(0)}</a>'

        return Markup(REF.sub(link, str(escape(html))))  # noqa: S704 (escaped first)


def for_review(cfg, review, here=False):
    repo, checkout = cfg.repo, getattr(cfg, "checkout", None)
    if review.get("pr") is not None and hasattr(cfg, "repos"):
        repo = prs.repo(review["pr"])
        # A repo taken out of the config still renders its saved reviews, just without a checkout to resolve names.
        checkout = next((r.checkout for r in cfg.repos if r.name == repo), None)
    files = decode_json(review.get("files"), [])
    return Refs(
        repo,
        review.get("id"),
        review.get("sha"),
        files,
        review.get("diff"),
        here=here,
        index=checkout_index(checkout),
    )
