# Plus One design

Plus One reviews the PRs waiting on its owner: silent approval of clean PRs during scheduled checks for repositories where it is enabled, queued comment drafts for P0 and P1 findings, `DEFER` for everything else.

## Decisions

| Area | Decision |
|---|---|
| Runtime | `bin/plus1 install` creates a systemd user timer or launchd agent on the owner's machine. Reviews use headless `claude -p` with the owner's Claude Code login or `claude_config_dir`. GitHub calls use `gh` signed in as the owner. |
| Schedule | The timer fires every 30 minutes. Checks run only inside `[window]`, default weekdays from 07:00 to 17:00 in the configured zone. Timer checks add up to 10 random minutes of delay. |
| Scope | Review repos come from `[[repos]]`. Candidates are open PRs requested of the owner or their teams, follow-ups after the owner requested changes, and optionally PRs linked in selected Slack channels within 72 hours. |
| Priority | Follow-ups come first, then PRs both requested and linked, requested PRs, and Slack-only PRs. A PR in your code or from a team you named moves up one tier, never past follow-ups. A PR passed over for budget moves up one tier per check. |
| Updates | Each install keeps itself current from its channel (release tags by default, or every merge to main), at an idle moment, and goes back on its own if the new version fails its smoke test or doesn't come up. It never touches a clone with its own work in it. Settings can switch the channel, or switch it to ask first or off. |
| Approval setting | Off by default per repository. Insights advises 5 working days, 30 or more graded approvals, zero false approvals, and at least 90% agreement, but does not block turning it on. Settings shows the number met. Turning it on requires typed `loosen`; turning it off requires nothing. |
| Clean PR | A scheduled check for an enabled repository asks GitHub again, then posts a silent `APPROVED` review pinned to the reviewed head SHA. An owner-started review, or any review where the setting is off, leaves Ready to approve on To do. |
| Findings | P0 and P1 findings are fact-checked and drafted in the owner's voice. The owner posts or drops drafts in the web app or with `bin/plus1 release` and `dismiss`. A posted draft containing P0 uses `REQUEST_CHANGES`. |
| P1 | Approval stays withheld until the owner posts or dismisses the draft. |
| Unsure | `DEFER` posts no approval or comment and appears in the report. |
| Never approve | Reviewer instruction files, `.claude/`, authorization paths, payment paths, selected CODEOWNERS teams, and configured path patterns are reviewed but left for the owner to approve. |
| Skips | Skip drafts, approved PRs, conflicts, pending-only CI, hold labels, and handed-off PRs. Hand-offs return when brought back or after 3 working days without another review. Checks named like `Require ... Approval` never count as CI. |
| Preconditions | Required checks are not failing, no blocking thread from another reviewer is open, and the PR has been ready for at least 30 minutes. |
| Other reviewers | Open threads and human comments become claims for the deep review to check. Existing findings are drafted as agreement. A clean PR is held when a human reviewer still owes an answer. |
| Re-review | A new head SHA is evaluated. A SHA already reviewed by the owner is skipped. Verified findings carry forward, are verified again, and cannot drop in severity. |
| Rebase reuse | If the PR's files have identical before and after blobs, `DEFER` carries without a model call unless caused by open comments. `APPROVE` also carries outside the never-approve lane when no commenter could hold it. |
| Incremental review | A change below `incremental_max_lines` after a thorough `APPROVE` or `DRAFT` reviews only moved files when the PR is not too large and keeps its base branch. Other changes receive full review. |
| Follow-ups | After the author pushes, the owner's open asks are checked against the new head. The result is fixed, where Approve leads, or still waiting. Follow-ups are never approved automatically. |
| Depth | Review uses a read-only checkout in a throwaway worktree, caller and pattern searches, fixed check commands enabled by the owner, and read-only snapshots of configured `peers`. The reviewer never chooses command arguments. |
| Models | Haiku at low effort triages. Opus at high effort performs deep review. Opus at medium effort verifies findings. Sonnet drafts comments. Haiku fact-checks drafts. Sonnet and Opus learn the rubric. Each role is one `[models.*]` line. |
| Rubric | `rubric.md` sits beside the owner's config. `bin/plus1 learn` learns from GitHub reviews. Accepted rubric changes refine it. Changes are drafted after a graded wrong call or when the owner's GitHub review differs from the call. |
| Agent PRs | Use the same rubric plus agent-specific checks. |
| Reporting | Results appear in the web app described by `UI-PLAN.md` and a local report. With `notify`, desktop notifications and Web Push over Tailscale cover queued drafts, deferrals, errors, and a PR approved, queued, merged, or closed before its drafts were posted. Each links to its item. |
| Safety | A `PAUSE` file, approval caps per check and day, a daily spend cap, fail-closed errors, and an append-only audit log limit action. |
| Location | Code lives in this repo. Config and rubric live in `~/.config/plus1/`. State lives in `~/.local/state/plus1/` or `state_dir`. |

## Pipeline per check

1. Exit when `PAUSE` exists or time is outside `[window]`. A timer check waits its random delay. Acquire a `flock` so checks cannot overlap.
2. For each repo, search GitHub for review requests and follow-ups. Read Slack when enabled. Apply skips and preconditions without model calls.
3. Before each review, stop when daily spend reaches its cap. Ask GitHub again whether the PR remains open and unapproved.
4. Triage each survivor into `dependabot`, `trivial`, `substantive`, or `never-approve`.
5. Run configured check commands. Deep-review substantive and never-approve PRs in a worktree at the PR head. Produce a structured verdict with findings, severity, confidence, and evidence.
6. Re-derive every P0 and P1 finding in a verification pass. Convert unverified findings to private notes.
7. Act. On a scheduled check with the repository setting on, ask GitHub once more, then post `APPROVED` with `commit_id` set to the reviewed SHA, subject to approval caps and never-approve lanes. Never approve a PR the owner asked to change. Otherwise, leave a Ready to approve card. Put comment drafts in the release queue.
8. Append the decision to the audit log and daily report, then notify. Grade past calls against the owner's GitHub actions and draft rubric changes for calls overruled there.

## State

- `prr.db`: SQLite in WAL mode. Stores repos, runs, per-PR observations, reviewed diffs, stage model usage, tokens, cost, duration, findings and verifier verdicts, drafts, GitHub action IDs, grades, rubric changes, push subscriptions, hand-offs, and live progress events.
- `state.json`: Last decided head SHA for each PR, used by skip rules.
- `audit.jsonl` and `reports/YYYY-MM-DD.md`: Append-only text logs of the same decisions.
- `logs/`: Timer and web app output for launchd installations.

## Notes

- Slack reads `conversations.history` using the token stored by the Claude Code Slack plugin. The token lasts about a day and interactive Claude Code sessions refresh it. If expired, the check records the problem in the report and sidebar, then continues with GitHub requests.
- A path owned by any never-approve team puts the PR in that lane. Files with multiple owners defer when any owner is restricted.
- Dependabot approvals are silent.
- The reviewer uses `--restricted`, a read-only tool allowlist, and no GitHub credentials in its environment. No settings file can add permissions. `tests/selftest_sandbox.py` checks these properties against the live Claude Code command line.