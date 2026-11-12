<div align="center">

# Plus One

Local pull request reviews, drafted by Claude Code in your voice.

[![CI](https://github.com/andymai/plus-one/actions/workflows/ci.yml/badge.svg)](https://github.com/andymai/plus-one/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/andymai/plus-one)](https://github.com/andymai/plus-one/releases)
[![License: MIT](https://img.shields.io/github/license/andymai/plus-one)](./LICENSE)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/to-do-dark.png">
  <img alt="Plus One's To do list for acme/storefront groups pull requests as Ready to approve, Blocking, Worth fixing, and Needs a look, beside an open blocking PR with a drafted comment about a duplicated idempotency key." src="docs/media/to-do-light.png">
</picture>

</div>

Plus One reviews the pull requests waiting on you. It finds review requests in the repositories you choose, including requests to your teams, revisits PRs where you requested changes after the author pushes again, and can include PR links from selected Slack channels.

It learns from reviews you have written on GitHub and drafts comments in your voice. When you mark a call wrong, or your GitHub review differs from its call, Plus One drafts a rubric change for you to accept. Approving clean PRs on its own is off by default for every repository, so nothing posts without you. You can turn it on independently for repositories you trust.

Plus One runs on your own Mac or Linux machine with your GitHub and Claude Code logins.

## Start

You need macOS or Linux, git, the GitHub CLI (`gh`) signed in, Claude Code signed in, and either uv or Python 3.11 or newer.

```sh
git clone https://github.com/andymai/plus-one.git
cd plus-one
bin/plus1 setup
```

The first run creates its own Python environment. Setup asks which repositories to use, where your clones are, which teams' PRs it should never approve automatically, and your working hours. It writes configuration to `~/.config/plus1/config.toml` and stores your rubric beside it.

Setup can learn your rubric from your GitHub reviews for about $1. It also installs a timer that checks for PRs every 30 minutes during your working hours, then opens the web app.

## Web app

Run `bin/plus1 open` to open the signed-in web app at http://127.0.0.1:8470. To reach it from your phone, you can serve it over Tailscale instead.

## Commands

Run commands as `bin/plus1 <command>`.

| Command | Description |
| --- | --- |
| `setup` | Set Plus One up, step by step. |
| `doctor` | Check its requirements and show how to fix anything missing. |
| `update` | Install the latest version now and restart it. It also does this on its own, and `--rollback` goes back one version. |
| `open [--new]` | Open the web app signed in. `--new` signs every browser out first. |
| `install [--no-web]` | Start the timer and, unless excluded, the web app. |
| `uninstall` | Remove the timer and web app. |
| `learn [--plan]` | Learn your rubric from GitHub reviews. `--plan` only counts what it would read. |
| `tick [--plan] [--pr N]` | Check for PRs now. `--plan` applies filters without model calls. |
| `status` | Show each repository's mode, pause state, and queued drafts. |
| `pause` / `resume` | Stop or restart checking. |
| `release N` | Post queued comments. If none blocks, also approve the PR. |
| `dismiss N [--no-approve]` | Discard queued comments. If none blocks, also approve unless `--no-approve` is set. |
| `ignore N` / `unignore N` | Hand a PR to other reviewers, or bring it back. |

## Read more

Start with `docs/onboarding.md` for setup, the first week, teaching Plus One, costs, safety, and Mac notes.

See `docs/admin.md` for repository maintenance, `DESIGN.md` for how a review works, `UI-PLAN.md` for the web app, and `CLAUDE.md` for working on the code.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for working on the code and the checks. See [SECURITY.md](SECURITY.md) for reporting a vulnerability.
