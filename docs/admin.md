# Running Plus One for a team

You manage repository access and keep `main` releasable. Each engineer runs and controls a separate instance on their own machine.

## Access

Give each engineer read access to the private repository by adding them as a collaborator. They can then clone it and run their own instance.

Plus One needs no GitHub organization settings, GitHub app, or separate token. It uses each engineer's existing `gh` login.

An instance has the same GitHub permissions as its engineer. It reviews as that engineer and posts as them, only in the repositories they list: actions the engineer clicks, plus approvals of clean PRs from scheduled checks where they turned on approving on its own.

Approvals count like the engineer's own approvals. Branch rules, required checks, and CODEOWNERS still apply.

## How instances relate

Every engineer has their own:

- Plus One process on their machine
- GitHub and Claude Code logins
- Configuration in `~/.config/plus1/`
- History in `~/.local/state/plus1/`
- Rubric in `~/.config/plus1/rubric.md`

Nothing is shared between instances, and there is no server to run.

A team review request reaches every member's instance. Each timer check starts up to 10 random minutes late, controlled by `window.jitter_minutes`. Every instance asks GitHub for fresh state before reviewing and again before approving. If one instance has already approved a pull request, the others skip it.

Rubrics are personal. Each engineer's rubric is learned from their own GitHub reviews and changes only when they accept a change. The repository supplies a generic starting rubric at `plus1/starter_rubric.md`.

Insights gives the engineer an advisory checklist for deciding whether to turn on approving clean PRs on its own for a repository:

- 5 working days of decisions
- 30 or more graded approvals
- Zero false approvals
- 90% or more agreement

The checklist does not block the setting.

Plus One never approves on its own when a pull request touches files that instruct the reviewer, authorization or payment paths, or files owned by teams the engineer chose as never-approve lanes.

## Updates

Plus One updates itself. By default, each install checks hourly for a new release tag on `main`, and installs it at the next moment nothing is running: no check, card action, or drafting in progress. An install from before this feature needs one `bin/plus1 update` to pick it up. It tests the new version, restarts, and confirms the new code is answering. If any of that fails, it goes back to the version it was on, skips the release until a newer one exists, and notifies the engineer. A clone with uncommitted changes, local commits, or another branch checked out is left alone, and the engineer is told once.

Engineers can change this under Settings, Updates: Ask first shows Update available instead of installing, Off stops checking, and following Main takes every merge instead of only releases. To install right away:

```sh
bin/plus1 update
```

`bin/plus1 update --rollback` goes back to the version before the last update.

A release reaches every install on automatic soon after it's tagged, and each one runs from that engineer's checkout. Keep `main` releasable. CI runs unit tests on Linux, and also on macOS when platform files change.

## Costs

Costs go to each engineer's Claude Code account.

For one engineer's instance reviewing one busy repository over 11 days, daily cost had a median of $15.48, a 90th percentile of $17.69, and a maximum of $32.31. Learning a rubric costs about $1.

Each instance stops starting reviews when daily spending reaches `limits.max_spend_per_day_usd`. The default is $40. Each engineer can change the limit in their own configuration.

## Safety

The reviewer runs Claude Code headless with read-only tools and without GitHub credentials in its environment. `tests/selftest_sandbox.py` checks this sandbox against the live Claude CLI.

Plus One posts only what the engineer clicks, plus silent approvals from scheduled checks in repositories where the engineer turned on approving clean PRs on its own. Before each approval, it asks GitHub again. Every approval is pinned to the reviewed commit and is refused if the pull request has moved.

Check commands run code from the pull request branch on the engineer's machine. They are off unless the engineer lists them.

## When someone is stuck

Run:

```sh
bin/plus1 doctor
```

It identifies what is missing and explains how to fix it.

To stop new checks and automatic approvals on one instance, run:

```sh
bin/plus1 pause
```

A check already running finishes the reviews it started.