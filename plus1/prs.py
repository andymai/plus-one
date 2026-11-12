"""PR ids: one integer that names a PR across every repo an instance reviews.

An id is the repo's id times BASE plus the GitHub number. The first repo ever registered gets id 0, so an instance that
started with one repo keeps every stored number as it was. Store registers the configured repos when it opens, which
fills the lookups below for the rest of the process.
"""

import threading

BASE = 10_000_000

_names = {}
_ids = {}
_named = False
_lock = threading.Lock()

SCHEMA = "CREATE TABLE IF NOT EXISTS repos (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"


def register(db, names):
    global _named, _names, _ids
    with _lock:
        db.execute(SCHEMA)
        for name in names:
            if db.execute("SELECT 1 FROM repos WHERE name = ?", (name,)).fetchone() is None:
                # The first repo takes id 0, the one an instance's existing rows were stored under.
                db.execute(
                    "INSERT INTO repos (id, name) VALUES ((SELECT COALESCE(MAX(id) + 1, 0) FROM repos), ?)",
                    (name,),
                )
        rows = db.execute("SELECT id, name FROM repos").fetchall()
        # Built aside and swapped in whole, since other threads read these while a Store opens.
        _names = dict(rows)
        _ids = {name: repo_id for repo_id, name in rows}
        _named = len(names) > 1


def pid(repo, number):
    return _ids[repo] * BASE + int(number)


def split(pr_id):
    return _names[int(pr_id) // BASE], int(pr_id) % BASE


def number(pr_id):
    return int(pr_id) % BASE


def repo(pr_id):
    return _names[int(pr_id) // BASE]


def label(pr_id):
    """#123 while one repo is configured, short-name#123 once there are more."""
    name, n = split(pr_id)
    return f"{name.split('/')[1]}#{n}" if _named or int(pr_id) >= BASE else f"#{n}"


def bounds(repo):
    """The range of ids a repo's PRs take."""
    lo = _ids[repo] * BASE
    return lo, lo + BASE - 1


def url(pr_id):
    name, n = split(pr_id)
    return f"https://github.com/{name}/pull/{n}"
