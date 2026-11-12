# Getting started with Plus One

Plus One starts as a private reviewer that drafts decisions and comments without posting them. Clean PRs wait for your click by default. You can let each repository approve clean pull requests on its own during scheduled checks by turning on its setting.

## Set it up

You need:

- macOS or Linux
- `git`
- The GitHub CLI, signed in with `gh auth login`
- Claude Code, signed in
- `uv`, or Python 3.11 or newer

On a Mac without `uv` or a recent Python:

```sh
brew install uv
```

Clone and run setup:

```sh
git clone https://github.com/andymai/plus-one.git
cd plus-one
bin/plus1 setup
```

The first run builds a Python environment in `.venv`.

Setup asks for:

1. Your GitHub login and what drafts should call you. Both are prefilled from GitHub.
2. Repositories to review. It suggests repositories with the most review requests and recent reviews from you.
3. The local clone of each repository. It searches common folders such as `~/code`, `~/src`, and `~/Developer`. Plus One only adds temporary worktrees to these clones. It never touches your branches.
4. Teams whose pull requests it should never approve on its own. It suggests teams named by approval-gate checks and CODEOWNERS teams with names that read like security, database, payments, or infrastructure.
5. An optional command, such as a linter, to run before each review.
6. Your time zone, working hours, and working days.
7. Desktop notifications.
8. Slack.
9. Tailscale.

Setup writes `~/.config/plus1/config.toml`. If a config already exists, it keeps a `.bak` copy beside it. It creates `~/.config/plus1/rubric.md` from a generic rubric, offers to learn from your GitHub reviews, installs the services, and opens the web app.

Run this after setup:

```sh
bin/plus1 doctor
```

It checks tools, logins, repository access, local clones, the rubric, the timer, the web app, and the last check. Each failure includes a suggested fix.

## The first week

Approving clean PRs on its own begins off in every repository. Plus One reviews pull requests, drafts comments, and fills your To do list, but nothing reaches GitHub unless you click.

Each pull request starts with the decision and one action, such as “Approve with 2 comments?” After you act, you have 8 seconds to undo it. The action is refused if the pull request received new commits after the review. Plus One then reviews the new commits on its own, and the new result replaces the card.

PRs count as your code after a personal review request, enough changed or removed lines you wrote, or 2 commits in the last 6 months to a changed file. They move up one review tier, never ahead of follow-ups, and sort first in their To do group. Rows show an "asked you", "your code", or "your files" chip for each signal, while cards explain each in a sentence. Check Settings, You, Commit emails for the addresses Plus One found.

To see your team's PRs first, add them under Settings, You: Your teams takes GitHub teams as `org/team`, People takes individual GitHub logins, and Other teams takes teams you're not on but still want to review. PRs from your team lead each To do group with a "team" chip, then PRs from other teams with a "watching" chip, then PRs in your own code. Both move up one review tier, and the check's notification counts your team's PRs that still need you. These lists only reorder PRs that already reach you. They don't add reviews or change what can be approved. Setup offers your GitHub teams for Your teams.

Use `j` and `k` to move between pull requests. Press `⌘K` to open the command menu.

Grade its calls as you work. Your actions in the app and on GitHub grade most decisions. While the readiness checklist is unmet, an empty To do offers "Start a grading queue." You can also start one from Grade in the sidebar, which shows how many are waiting, from the ungraded decisions row in Insights, or from the command menu. Each queue contains 12 past decisions and leaves out PRs still on To do. Your grading queue puts approvals first for repositories whose readiness checklist still needs graded approvals until it has 30 or more, followed by comment drafts that would put words in your name, other approvals, and decisions left to you.

A repository's checklist has:

- 5 working days of decisions
- 30 or more graded approvals
- Zero false approvals
- At least 90% agreement

Insights shows this advisory checklist for each repository and estimates the working days left at the recent pace. It does not block the setting. Follow the repository's Settings link and type `loosen` to turn it on. Each repository decides independently.

## Teach it how you review

Your rubric is a plain Markdown file at `~/.config/plus1/rubric.md`. It records:

- What blocks a merge for you (P0)
- What deserves a comment (P1)
- Nits that remain private (P2)
- What it never approves on its own
- How it reviews
- How drafted comments should read

You can edit it on the Settings page, which shows your change as a diff and records it under Rubric changes so you can revert it, or edit the file directly. The `## Comment format (drafts)` section controls the voice of every drafted comment. When you rewrite a drafted comment from its card and use the result, Make this a rule turns your note into a proposed change to that section, which waits in Rubric changes for you to accept.

To learn from your review history, run:

```sh
bin/plus1 learn
```

It reads up to 300 reviews you wrote on GitHub during the last 183 days. Use `--limit` and `--days` to change those values. It excludes your own pull requests and anything Plus One posted for you.

Learning uses only your words: decisions, review summaries, and inline comments, along with the code under each comment. It cannot change the rubric's verdict rules or never-approve section, or add a rule that names anyone.

The command drafts a new rubric and shows you the diff. You choose whether to accept it. Accepted changes can be reverted under Rubric changes in the web app. A learning run costs about $1.

To count what it would read without making model calls:

```sh
bin/plus1 learn --plan
```

Settings also has a Learn again button that runs the same learning in the background; the result waits under Rubric changes.

Learning continues as you use it. When you mark a call wrong, select “Teach it” to draft a rubric change in about a minute. When your own GitHub review differs from its call, it drafts a proposal automatically, up to 3 per day. Every proposal waits under Rubric changes for you to accept, reject, or later revert.

## Change settings later

Open Settings, in the sidebar and command menu, to change how Plus One works. The page groups settings by the question you have:

- **Checking:** Set the schedule, Slack sources, waits and skips, approval-gate checks, hand-off return, size limit, daily spend cap, and reviews per check.
- **Approving on its own:** Turn each repo’s switch on or off beside its readiness checklist, set never-approve teams and paths for each repo and for every repo, approvals per check and per day, and the largest PR Plus One can approve. Built-in never-approve paths, skip labels, and blocking markers are locked.
- **Reviewing:** Edit or relearn the rubric, set each model role’s model, effort, budget, and time limit, choose who drafts comments, set each repo’s clone, check commands, peers, and issue tracker, and add or remove repos.
- **Notifications:** Control desktop notifications and manage your phones and other devices.
- **System:** Set your name and GitHub login, how the web app is served and who can sign in, and the history folder, and see recent changes.

The strip at the top shows whether Plus One is checking, the next check, today’s spend against the cap, and who approves in each repo. Use its switch to pause or resume checks. On wide screens, the sticky section rail includes a filter that `/` focuses. On narrower screens, use the grouped index and sticky crumb.

Each section saves independently. Save and Discard appear after you change it. Nondefault values show the default and a Reset. Saving updates `~/.config/plus1/config.toml` while keeping comments and writing only changed values. Plus One keeps `config.toml.bak` and restores it if the result does not load. The next check uses saved changes, and the app also picks up hand edits. A change that lets Plus One do more on its own lists each change and asks you to type `loosen`. A change to what runs on your machine shows the exact command and asks you to type `run`. A change that could lock you out of the web app asks for the new value a second time. Web app settings and the history folder apply after a restart, which the page offers when needed.

## What it costs

Model calls are billed to your Claude Code login.

One engineer's instance reviewing one busy repository measured these daily costs over 11 days:

- Median: $15.48
- 90th percentile: $17.69
- Maximum: $32.31

Checks stop starting reviews when daily spending reaches `limits.max_spend_per_day_usd`, which defaults to $40. The sidebar shows when the cap is reached, and checks start reviews again the next day. Reviews, write-ups, and rubric changes that you request still run.

## What keeps it safe

The reviewer runs Claude Code headless with read-only tools and no GitHub credentials. It cannot push, comment, or approve. Plus One posts only actions you click, plus silent `APPROVED` reviews from scheduled checks for repositories where you turned on approving clean PRs on its own. A review you start leaves a Ready to approve card. Every post is pinned to the reviewed commit.

It never approves a pull request on its own when it touches:

- Files that instruct the reviewer, including `CLAUDE.md`, `AGENTS.md`, or `.claude/`
- Authorization or payment paths
- Files owned by teams you selected during setup

It still reviews these pull requests, but leaves approval to you.

In a repo that approves on its own, a clean PR waits for you when its author asks you by name, it changes or removes 20 or more lines you wrote, or your lines are at least a quarter of those lines, counting at least 5. Recent commits alone never hold approval. Approval also waits if git cannot be read. Each repo’s Hold PRs in your code switch, under Settings, Approving on its own, is on by default. Turning it off requires typing `loosen`.

A configured check command runs code from the pull request branch on your machine. Add only commands you would run on any teammate's branch.

When multiple people run their own instances, a team review request reaches every instance. Each timer check waits up to 10 random minutes. Each instance checks GitHub again before reviewing and before approving, so it skips a pull request that someone already approved.

To stop new checks and hold automatic approvals:

```sh
bin/plus1 pause
```

A check already running finishes its started reviews. Continue with:

```sh
bin/plus1 resume
```

## On a Mac

The timer and web app run as launchd agents:

- `~/Library/LaunchAgents/local.plus1.tick.plist`
- `~/Library/LaunchAgents/local.plus1.web.plist`

Logs are stored in `~/.local/state/plus1/logs/`. Checks run while the Mac is awake. When it wakes, launchd runs a check missed during sleep.

For clickable notifications:

```sh
brew install terminal-notifier
```

Without it, Plus One uses plain macOS notifications.

For Slack, leave `slack.token_path` empty. Plus One reads the token from the Keychain item used by Claude Code.

## On your phone (optional)

The web app normally stays at `http://127.0.0.1:8470`. To reach it through Tailscale, set these values under `[web]`:

- `allowed_login` to your tailnet login
- `public_url` to the HTTPS address provided by `tailscale serve`

The public URL is used for notification links and Web Push.

Restart the installed web app so it listens on its Unix socket:

```sh
bin/plus1 install
```

The default socket is `~/.local/state/plus1/web.sock`. Publish it with:

```sh
sudo tailscale serve --bg unix:$HOME/.local/state/plus1/web.sock
```

Only your configured tailnet login can enter. Web Push notifications can then reach your phone.

## When something is off

Start with:

```sh
bin/plus1 doctor
```

To see what a check would do now without model calls:

```sh
bin/plus1 tick --plan --any-time
```

On Linux, inspect logs with:

```sh
journalctl --user -u plus1
```

On a Mac, read `~/.local/state/plus1/logs/tick.log`.

Plus One keeps itself up to date. By default it checks hourly and installs new releases when nothing is running, and open tabs reload themselves, or show a button if you have something half-typed. If a new version doesn't come up properly, it goes back to the one you had and tells you. See what changed under What's new, from the version in the sidebar, and change how this works under Settings, Updates.

To install the latest version right now, or to go back to the one before the last update:

```sh
bin/plus1 update
bin/plus1 update --rollback
```

To remove the services:

```sh
bin/plus1 uninstall
```

Your configuration and history remain in `~/.config/plus1` and `~/.local/state/plus1` until you delete them.