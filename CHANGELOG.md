# Changelog

## 1.12.0 (2026-11-12)


### Features

* **web:** when new commits stop a post, review them right away


### Bug Fixes

* **deps:** upgrade certifi to 2026.10.23
* **web:** keep both To do panes when nothing is waiting

## 1.11.0 (2026-11-04)


### Features

* **web:** keep Plus One up to date on its own

## 1.10.0 (2026-11-04)


### Features

* **web:** put your team's PRs first in To do

## 1.9.0 (2026-10-29)


### Features

* **web:** put Let someone else review on the card's button row

## 1.8.0 (2026-10-28)


### Features

* **web:** rewrite a drafted comment from your suggestion, and make a kept rewrite a rule

## 1.7.2 (2026-10-28)


### Bug Fixes

* **drafts:** stop the fact-check flagging the comment's own label, and have doctor flag a missing drafting script

## 1.7.1 (2026-10-17)


### Bug Fixes

* **web:** keep the mobile tab bar pinned when content runs wider than the screen

## 1.7.0 (2026-10-16)


### Features

* **web:** grade comment drafts first once a repo's checklist has its graded approvals


### Bug Fixes

* **slack:** read the newest token across every Claude login's credentials file

## 1.6.0 (2026-10-16)


### Features

* review PRs in your own code sooner, flag them on To do, and hold their automatic approval

## 1.5.0 (2026-10-16)


### Features

* **web:** add and remove repos, edit each repo's clone, peers, and tracker, and manage devices in settings

## 1.4.0 (2026-10-16)


### Features

* **web:** edit check commands, drafting, login folder, the web app, and history from settings

## 1.3.0 (2026-10-16)


### Features

* **web:** edit models, waits, limits, every-repo lists, and Slack details from settings

## 1.2.0 (2026-10-16)


### Features

* **web:** grade past decisions in queues of 12, with Grade in the sidebar

## 1.1.1 (2026-10-16)


### Bug Fixes

* **web:** reasoning on the review page says what the card hasn't

## 1.1.0 (2026-10-16)


### Features

* **web:** settings grouped by what they do, with a section rail and saves that show what changes

## 1.0.0 (2026-10-16)


### ⚠ BREAKING CHANGES

* approving clean PRs on its own is a per-repo setting, off by default

### Features

* a settings page that edits your config and rubric in place, asking before it loosens anything
* approving clean PRs on its own is a per-repo setting, off by default
* fold uncanny-pattern checks into the rubric
* follow up on PRs you requested changes on
* guided setup, doctor, and update, with a first run that sets itself up
* hand a PR off to other reviewers
* layered config with team profiles and a personal config dir
* learn your rubric from your own GitHub reviews, and keep learning when you overrule it
* let reviewer read peer repos and close sandbox holes
* map oversized PRs before deferring
* post blocking drafts as a request-changes review
* review several repos from one instance, each going live on its own
* **review:** record what the reviewer checked
* **review:** say what each PR does and keep its ticket key
* run a repo's opted-in checks before the review
* run on macOS too: launchd agents, background jobs, notifications, and the Keychain
* serve the web app on this machine with a sign-in link, Tailscale optional
* skip re-reviews of an unchanged patch and narrow small ones
* stagger ticks, recheck GitHub before acting, and cap daily spend
* **web:** animate page switches, card actions, folds, and live rows
* **web:** decide from the card
* **web:** fold earlier reviews in Activity and send the gate to grading
* **web:** give each comment its evidence and review Needs a look in the app
* **web:** grade past decisions one at a time from a queue
* **web:** keep Activity current and add pull-to-refresh
* **web:** keep up with new reviews and capture why
* **web:** lead each card with the decision
* **web:** Linear-style redesign
* **web:** plain reasons, fact-check flags you can overrule, richer ready cards
* **web:** replace checkmark branding with a +1 icon set
* **web:** show findings the double-check dropped and write them up on demand
* **web:** show ready to approve PRs first in To do
* **web:** show To do as a two-pane inbox on wide screens
* **web:** show who opened each To do PR and why it's waiting
* **web:** To do as collapsible rows with stacks
* weigh other reviewers' comments in the deep review


### Bug Fixes

* a handled mark only hides the review it was made on
* a Settings tab on phones, fields that don't zoom on iOS, and a Learn again button that fits
* carry verified findings into re-reviews and alert on unposted drafts
* clear ruff's lint and bandit findings, and gate CI on ruff check
* close gaps from the second review round
* distinguish clean never-approve deferrals
* keep open PRs, waivers, and hand-offs in view
* keep unposted comments and held approvals in view
* retire comments a PR merged without instead of listing them
* review PRs held by approval gates and show why others wait
* **store:** let a thread add a new column another thread just added
* type-check with basedpyright in CI, with every error fixed
* **web:** close gaps found in review
* **web:** fail actions a restart left running
* **web:** group stale cards under New commits
* **web:** hide PRs that are merged, closed, approved, or in the merge queue
* **web:** menus inside To do rows were clipped; record bring-backs
* **web:** point a refused post at the review that follows it
* **web:** show an action as in progress until it lands
* **web:** tell Andy when an action fails after he leaves


### Performance

* **web:** gzip text responses


### CI

* release with release-please from 1.0.0, and keep requirements.lock current on Dependabot PRs
