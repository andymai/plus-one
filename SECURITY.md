# Security policy

## Reporting a vulnerability

Report vulnerabilities through [GitHub's private vulnerability reporting](https://github.com/andymai/plus-one/security/advisories/new). Do not open a public issue.

Include:

- A description of the vulnerability.
- Steps to reproduce it.
- The impact.
- A suggested fix, if there is one.

Expect an initial response within 48 hours.

## Supported versions

The supported version is the latest release on `main`. Engineers run Plus One from a clone, and `bin/plus1 update` pulls `main`.

## How Plus One limits what it can do

### The reviewer

The reviewer runs Claude Code headless with these protections:

- A read-only tool allowlist plus a denylist.
- `--restricted`, so no settings file can add permissions.
- Hooks and MCP turned off.
- No GitHub credentials in its environment.
- Access to read the pull request's code, but no ability to push, comment, approve, or write files.

`tests/selftest_sandbox.py` checks these restrictions against the live Claude CLI.

Pull request titles, comments, and code reach the model only as fenced data, never as instructions. The reviewer does not choose commands to run. The only commands that run are checks the engineer listed in their own configuration, with fixed arguments and no shell.

### Posting to GitHub

Every review is pinned to the commit that was reviewed. Plus One refuses to post a review after the pull request receives new commits.

Approving clean pull requests without a click is off by default and can be turned on per repository. Even when enabled, Plus One never approves these without the engineer:

- Files that instruct the reviewer.
- Paths matching authorization or payment patterns.
- Code owned by teams the engineer chose.

### The web app

The web app uses these protections:

- By default, it listens only on `127.0.0.1`.
- Every request must name a loopback host, which protects against DNS rebinding.
- Sign-in uses a one-time link.
- Sessions use an HMAC-signed cookie. The secret is stored in the state directory with mode `0600`.
- Every `POST` request requires a CSRF token.
- It can optionally serve over Tailscale. Only the configured tailnet login gets access.

## Accepted risk

Browsers send localhost cookies to every port on the machine. Another local server that the browser visits therefore receives the Plus One session cookie.

Plus One trusts the engineer's other local servers the same way it trusts their files.

## Supply chain

- Dependencies install from lockfiles with hashes: `uv.lock`, plus an exported `requirements.lock` for pip.
- Dependency resolution stays one week behind PyPI with `exclude-newer = "7 days"`.
- Dependabot waits 7 days before proposing a release.
- Every GitHub Action is pinned to a commit SHA.
- OSV scans the lock on every pull request and weekly.