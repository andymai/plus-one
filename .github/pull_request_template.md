## What does this PR do?

## How to test

## Checklist

- [ ] Tests pass (`.venv/bin/python -m unittest discover -s tests`)
- [ ] Sandbox self-test rerun if `claude.py` sandbox flags, `ALLOWED`, or `DENIED` changed (`tests/selftest_sandbox.py`)
- [ ] Web changes clicked through with stub `gh` and `systemd-run` first on `PATH`
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
