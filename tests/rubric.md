# Review rubric

A starting point. Onboarding rewrites it from your own past reviews, and accepted rubric changes keep it in step with how you review. Edit it freely: it's yours.

## Verdicts

Every PR ends in exactly one verdict.

- `APPROVE`: no P0 or P1 finding, and you can state in one sentence why the change is safe after reading beyond the diff. Where the owner lets Plus One approve on its own, it posts a silent APPROVED review pinned to the reviewed head SHA; otherwise the owner approves.
- `DRAFT`: at least one P0 or P1 finding survived verification. Queue inline comments, withhold approval.
- `DEFER`: not confident, or too large to trace. Post nothing, record why in the report.

A never-approve lane does not change the verdict you give. Give the one the PR earns, and Plus One holds a clean `APPROVE` for the owner as `DEFER` instead of posting it.

"I found nothing" is not enough for `APPROVE` when coverage was thin. Thin coverage is `DEFER`.

Plausible code is the main risk: it reads right at a glance and breaks under a trace, and agents write it with confidence. Don't assume the author understood every line. Each non-obvious change has to prove itself before you approve. That bar is for finding problems, not inventing them: when a suspicion traces clean, or every alternative is worse, drop it.

## P0 (blocking)

1. **Boundaries and existing patterns.** A change that works but sidesteps a module boundary or an established pattern.
   - Reaching into another module's internals where the codebase has a public interface or an event for it.
   - Reimplementing something that already exists in the repo. Grep for it before accepting the new one.
2. **Correctness and silent production degradation.**
   - The diff contradicts the PR description, or contains an accidental leftover (a constant changed 30 to 3 while the body defers it).
   - Undeclared config: a build, CI, lint, dependency, or flag-default change the body doesn't mention or defend, riding along with other work. Config that the body or a why-comment explains and scopes is fine.
   - Globs and regexes that don't match what they claim. Check a pattern against real paths from `git ls-files` instead of reading it.
   - Swallowed errors that turn a failure into silently wrong output: an empty `catch` or `rescue`, an error logged and dropped where callers expect it to propagate, an unawaited promise, optional chaining on a value that should never be missing.
   - A method, option, or keyword argument the library doesn't have at the locked version. A plausible name is the tell. Find a matching use elsewhere in the repo before trusting it.
   - Incomplete changes: a rename or new argument that reached every call site but one (grep for the siblings and find the holdout), a new state field set on entry but not cleared on the back, error, or reset paths, one arm of a switch, union, or enum left on the old path.
   - The same value encoded differently across a boundary: casing, units, money as a float against a decimal column, local time where another timezone was intended.
   - Schema and data migrations that lock, rewrite, or drop data without the repo's documented safe path, or a schema file that disagrees with its migration.
   - Crash paths: null, zero, NaN, empty, or garbage input reaching an unguarded call.
   - An unguarded external call that turns an outage into an error page, or a fallback that causes a stampede.
   - A URL-reachable internal path (an internal-only option or route that anyone can append as a param).
   - Cache correctness: per-user data cached for everyone, a cache key missing an input, a null result cached, invalidation that only covers some mutation paths.
   - Non-idempotent or order-dependent state: mutating a memoized value, unseeded randomness that breaks pagination stability.
   - Default settings changes that affect production.
   - Live on merge into a store another service reads: the PR starts writing new or differently shaped data (a shared collection, a queue, a cache, an API response) as soon as it merges or deploys, and nothing shows that reader handles it. A kill switch that defaults to running counts as live. When the reader lives in another repo, open it in the checkouts the prompt lists. A reader that handles the change clears it, but for a mobile app only when the handling is as old as the code that reads the data, since older app builds stay in use. A reader that doesn't handle it, or one whose repo isn't checked out, stays P0.
3. **Security, authz, PII.**
   - Identifiers or secrets reaching places they should not (logs, job arguments, cache keys, client bundles).
   - Missing access control, enumeration through status codes, PII in logs or model-readable stores.
   - Queries built by string interpolation, unfiltered mass assignment, raw HTML from user-controlled data, a skipped auth check.
   - A real person's name, email, or phone number in a test fixture.
   - Apply the repo's security and privacy docs when it has them, and cite the doc.

Between two severities, pick the higher when the problem ships silently, fails silently, or sits in code reviewers skim, and again when local runs and CI can't exercise it (production-only data, third-party callbacks), since green tests prove nothing there. Weigh a small diff by what it reaches: a changed default or a relaxed validation is three lines that touch every consumer, so trace who relied on the old behavior before approving.

## P1 (important, non-blocking, still withheld until the owner releases or dismisses)

- Must land before a flag is enabled or the feature goes live (`issue (non-blocking, fix before enabling X)`). This means a flag that starts off. A switch that defaults to running is live on merge, which is P0 above.
- Rollout coverage gaps: behavior works on one surface (API, server render, a client) but not another that shares it.
- Semantics that must be settled before something ships: null or default handling for a new field, a new column or public name that will be hard to change.
- Convention violations that a repo doc states explicitly and that carry runtime risk (not style).

## P2 and nits (private report only, never posted)

Style and naming, comment hygiene, DRY suggestions, micro-performance, missing tests, PR template gaps. They appear in the local report so the owner can see what was noticed.

- Test theater: a test that cannot fail because it mocks the thing it tests or asserts only on a stub it set up, or covers only the old branch of a new parallel path.
- Over-defensive code: guards on values whose type the caller controls, back-compat shims with no other caller, an abstraction nothing uses yet, type-checker escapes covering a real mismatch.
- Names lifted from the ticket or the plan instead of the domain.
- A comment that records a fact the code can't show (a third-party quirk, an API casing gotcha) is not a nit. Leave it.

## Never auto-approve lanes (review and report, the owner approves)

- Schema and data migrations.
- Authorization and permissions code, money and payments, health data.
- Files that instruct automated reviewers (`CLAUDE.md`, `AGENTS.md`, `.claude/`).
- Findings still follow the P0/P1/P2 rules and are queued as drafts. The verdict follows the same rules as any other PR (see Verdicts).

## Review procedure

1. Read the PR body and the diff. List the claims the body makes, then what the diff changes that the body doesn't mention.
2. Check each claim against the diff (leftovers, contradictions, deferred items that changed anyway). A config, build, CI, dependency, or default change the body leaves out needs a why-comment, or it is a finding.
3. The PR is checked out read-only. For each changed symbol, read callers, subscribers, and nearby similar code. Grep for an existing pattern before accepting a new one.
4. Spend the most time where reviewers skim: regexes and globs, long query chains, metaprogramming, and date, timezone, and money math. Lockfiles, snapshots, and planning docs need only a glance. Leave what the repo's linters and type checker already enforce to them.
5. Read the docs the repo's `CLAUDE.md` or `AGENTS.md` routes the changed paths to, and apply them. Cite the doc when it is the reason for a finding.
6. For anything outside the diff that matters, say so and anchor on the nearest diff line.
7. Re-derive every P0 and P1 finding from the code in a second pass. Drop what cannot be re-derived.

When the prompt says a PR is too large to trace in full, step 3 covers only the riskiest code it names, not every changed symbol, and the review ends with where the owner should look first.

## Extra checks for agent-authored PRs (labels `agent/*`)

- The body claims versus the diff: leftovers, partially applied refactors, changed constants and flags not in the body.
- Assertions that pass for any value (compare to an independent reference).
- Over-broad refactors or generated churn mixed with the real change.
- Added comments that narrate the change, or ticket references in code.

## Dependabot lane

Patch and minor bumps: verify only the manifest and lockfile changed, read the upstream changelog against repo usage, confirm required checks are not failing. Major bumps, or anything touching more than the manifest and lockfile, use the main rubric.

## Comment format (drafts)

Plain language, first person, the way you'd say it to a teammate at their desk. Light Conventional Comments label up front, then talk.

- Short sentences. Say what you saw, why it worries you, and what you'd do instead. Ask the question when it is a question.

- No formal report language: no "Finding", "Impact", "Recommendation" headers, no restating the PR title, no praise padding, no em dashes.
- Labels: `issue (blocking)`, `issue (non-blocking, fix before enabling <flag>)`, `refactor`, `question`. The label is the only structure.
- Length: 2 to 4 sentences. Include the evidence (`file:line`, or the existing pattern to copy) and the suggested fix in the same breath, not as a list. Cite the repo doc by path when it is the reason.
- If the subject is outside the diff, anchor on the nearest changed line and say so in one clause.
- Drafts are written from a brief that carries this format, then checked for dashes and fact-checked before being queued.
