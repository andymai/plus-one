# Contributing to Plus One

Set up the locked development environment, run the CI checks, complete any relevant manual checks, then open a pull request.

## Set up

Clone the repository and install the locked dependencies:

```bash
git clone https://github.com/andymai/plus-one.git
cd plus-one
uv sync
```

`uv sync` builds `.venv` from `uv.lock` and includes the development tools.

Install the commit hooks once per clone:

```bash
uv run prek install
```

On every commit, the hooks run ruff linting with fixes and formatting on staged Python files, run basedpyright, and check that the commit message is a Conventional Commit. The hooks use the same locked tool versions as CI.

## Run the checks

Run every unit test:

```bash
.venv/bin/python -m unittest discover -s tests
```

The suite takes about 20 seconds and uses no network. Tests read `tests/config.toml`, never your personal config. They use unittest with no mock library. GitHub and process calls are stubbed by reassigning module attributes and restored in `tearDown`.

Run one test by its full name:

```bash
.venv/bin/python -m unittest tests.test_core.CodeownersTest.test_globstar
```

Run the coverage check:

```bash
uv run coverage run -m unittest discover -s tests && uv run coverage report
```

CI fails below the `fail_under` floor in `pyproject.toml`. Raise the floor when coverage rises. Never lower it.

Run the other CI checks:

```bash
uv run ruff format
uv run ruff check
uv run basedpyright
uv run lint-imports
uv run vulture
```

Basedpyright runs in standard mode with no errors allowed. The import rule allows only `plus1.web` to import `plus1.web`. This keeps the CLI, scheduled check, and setup independent of the web app.

Vulture checks for dead code. Put a name used only by templates or the standard library in `vulture_whitelist.py`.

To disable a lint rule for one line, include the reason:

```python
# noqa: CODE (why)
```

Write comments only where the reason is not obvious from the code.

CI also tests Python 3.11 and 3.14, renders every web page on macOS, verifies both lock files, and scans the lock for known vulnerabilities.

## Two checks CI can't run

If you change the Claude CLI flags in `plus1/claude.py`, or its `ALLOWED` or `DENIED` tool lists, run:

```bash
.venv/bin/python tests/selftest_sandbox.py
```

This runs the real Claude CLI and costs a few cents.

If you change the web app, click through it on a copy of your state:

```bash
bin/plus1-web --tcp 127.0.0.1:8000 --dev-login
```

Put stub `gh` and `systemd-run` scripts first on your `PATH`. A clicked Post or Approve otherwise reaches GitHub through the real `gh`.

## Dependencies

Declare dependencies in `pyproject.toml` and lock them in `uv.lock`. After changing either file, run:

```bash
uv lock
```

Regenerate `requirements.lock` with the `uv export` command written in its header.

## Pull requests

Use a Conventional Commit title with a lowercase subject. Valid types are `feat`, `fix`, `perf`, `refactor`, `chore`, `test`, `docs`, `style`, `ci`, `build`, and `revert`.

Pull requests are squash-merged, so the title becomes the commit. The pull request template asks about both manual checks. Merged `feat`, `fix`, and `perf` commits automatically produce releases.

Report security problems through `SECURITY.md`, not an issue.

## Where to read more

`CLAUDE.md` describes the architecture module by module. `DESIGN.md` explains the decisions and the pipeline each check runs. `UI-PLAN.md` covers the web app. `docs/onboarding.md` explains setup and use.

## License

Your contributions are licensed under the repository's MIT License.