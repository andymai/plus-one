import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import (
    __version__,
    actions,
    calibrate,
    codeowners,
    config,
    diffs,
    doctor,
    drafts,
    filters,
    gh,
    learn,
    peers,
    propose,
    prs,
    review,
    setup,
    signin,
    slack,
    team,
    units,
    updates,
    yours,
)
from .state import State
from .store import Recorder, Store, decode_json, import_legacy
from .store import now as stamp


def log(msg):
    print(msg, flush=True)


def in_window(cfg, now):
    w = cfg.window
    return now.weekday() in w.days and w.start_hour <= now.hour <= w.end_hour


def queue_draft(cfg, st, store, rec, review_id, pr, res, finding_ids):
    comments = []
    cost = 0.0
    total = len(res.findings)
    for i, finding in enumerate(res.findings, 1):
        if rec:
            rec.event("draft", f"drafting comment {i} of {total}")
        written = drafts.write_comment(cfg, pr, finding, rec=rec, idx=i)
        cost += written["cost"]
        placed = diffs.anchor(res.diff_files, finding["path"], finding["line"])
        comment = {
            "severity": finding["severity"],
            "path": placed[0] if placed else finding["path"],
            "line": placed[1] if placed else finding["line"],
            "inline": bool(placed),
            "body": written["body"],
            "needs_edit": written["needs_edit"],
            "unsupported": written["unsupported"],
        }
        comments.append(comment)
        store.insert(
            "drafts",
            review_id=review_id,
            finding_id=finding_ids.get(finding["check_index"]),
            pr=pr.get("id", pr["number"]),
            sha=pr["head"],
            original_body=comment["body"],
            original_needs_edit=comment["needs_edit"],
            original_unsupported=comment["unsupported"],
            status="queued",
            updated_at=stamp(),
            **comment,
        )
    return cost


def _drafting(store, draft):
    review = dict(store.query("SELECT * FROM reviews WHERE id = ?", draft["review_id"])[0])
    f = dict(store.query("SELECT * FROM findings WHERE id = ?", draft["finding_id"])[0])
    pr = {
        "id": review["pr"],
        "number": prs.number(review["pr"]),
        "repo": prs.repo(review["pr"]),
        "title": review["title"],
        "author": review["author"],
        "head": review["sha"],
    }
    finding = {
        "severity": f["severity"],
        "label": f["label"],
        "title": f["title"],
        "path": f["final_path"] or f["path"],
        "line": f["final_line"] or f["line"],
        "evidence": f["evidence"],
        "impact": f["impact"],
        "suggestion": f["suggestion"],
        "raised_by": f["raised_by"] or "",
    }
    return review, pr, finding


def write_up(cfg, store, draft_id):
    rows = store.query("SELECT * FROM drafts WHERE id = ? AND status = 'writing'", draft_id)
    if not rows:
        return False
    draft = dict(rows[0])
    review, pr, finding = _drafting(store, draft)
    rec = Recorder(store, review["run_id"], review["id"], review["pr"])
    rec.event("draft", "writing up a finding the double-check dropped")
    try:
        written = drafts.write_comment(cfg, pr, finding, rec=rec)
    except Exception as exc:
        store.update("drafts", draft_id, status="failed", body=str(exc)[:300], updated_at=stamp())
        rec.event("error", f"couldn't write the comment: {exc}"[:300])
        actions.notify(
            cfg,
            "Couldn't write the comment",
            f"{prs.label(review['pr'])}: {exc}"[:200],
            path=f"/review/{review['id']}",
            store=store,
        )
        return False
    placed = diffs.anchor(diffs.right_lines(review["diff"] or ""), finding["path"], finding["line"])
    finished = store.finish_write_up(
        draft_id,
        review["pr"],
        review["id"],
        path=placed[0] if placed else finding["path"],
        line=placed[1] if placed else finding["line"],
        inline=bool(placed),
        body=written["body"],
        original_body=written["body"],
        needs_edit=written["needs_edit"],
        unsupported=written["unsupported"],
        original_needs_edit=written["needs_edit"],
        original_unsupported=written["unsupported"],
        updated_at=stamp(),
    )
    rec.event(
        "done" if finished else "error",
        "comment ready" if finished else "comment discarded: the attempt was given up",
    )
    return finished


def rewrite(cfg, store, rewrite_id):
    rows = store.query("SELECT * FROM rewrites WHERE id = ? AND status = 'writing'", rewrite_id)
    if not rows:
        return False
    rw = dict(rows[0])
    draft = dict(store.query("SELECT * FROM drafts WHERE id = ?", rw["draft_id"])[0])
    review, pr, finding = _drafting(store, draft)
    rec = Recorder(store, review["run_id"], review["id"], review["pr"])
    rec.event("draft", "rewriting a comment the way you asked")
    code = (
        diffs.around(review["diff"] or "", draft["path"], draft["line"]) if draft["inline"] else ""
    )
    try:
        written = drafts.rewrite_comment(
            cfg, pr, finding, rw["base"], decode_json(rw["notes"], []), code, rec=rec
        )
        if not written["body"]:
            raise RuntimeError("the writer came back without a comment")
    except Exception as exc:
        store.db.execute(
            "UPDATE rewrites SET status = 'failed', error = ?, updated_at = ? WHERE id = ? AND status = 'writing'",
            (str(exc)[:300], stamp(), rewrite_id),
        )
        rec.event("error", f"couldn't rewrite the comment: {exc}"[:300])
        actions.notify(
            cfg,
            "Couldn't rewrite the comment",
            f"{prs.label(review['pr'])}: {exc}"[:200],
            path=f"/review/{review['id']}",
            store=store,
        )
        return False
    finished = store.finish_rewrite(
        rewrite_id,
        body=written["body"],
        needs_edit=written["needs_edit"],
        unsupported=written["unsupported"],
        cost_usd=written["cost"],
        updated_at=stamp(),
    )
    rec.event(
        "done" if finished else "error",
        "rewrite ready" if finished else "rewrite discarded: the comment moved on",
    )
    return finished


def save_findings(store, review_id, res):
    ids = {}
    for idx, check in enumerate(res.checks):
        f = check["finding"]
        ids[idx] = store.insert(
            "findings",
            review_id=review_id,
            idx=idx,
            severity=f["severity"],
            label=f["label"],
            title=f["title"],
            path=f["path"],
            line=f["line"],
            evidence=f["evidence"],
            impact=f["impact"],
            suggestion=f["suggestion"],
            confidence=f["confidence"],
            raised_by=f.get("raised_by") or None,
            carried="1" if f.get("carried") else None,
            origin=f.get("origin"),
            verified=check["verified"],
            verify_reason=check["reason"],
            final_path=check["path"],
            final_line=check["line"],
        )
    return ids


def handle(cfg, st, store, rec, review_id, pr, res, tally, auto=False):
    number, sha = pr.get("id", pr["number"]), pr["head"]
    label = prs.label(number)
    store.retire_drafts(number, review_id)
    finding_ids = save_findings(store, review_id, res)
    if res.verdict == "DRAFT":
        res.cost += queue_draft(cfg, st, store, rec, review_id, pr, res, finding_ids)
        tally["drafts"] += 1
    if res.verdict == "DEFER":
        tally["defers"] += 1
    extra = {"lane": res.lane, "cost_usd": round(res.cost, 2)}
    raw = res.raw or {}
    store.update(
        "reviews",
        review_id,
        lane=res.lane,
        verdict=res.verdict,
        reason=res.reason,
        coverage=raw.get("coverage"),
        summary=raw.get("summary"),
        raw_review=raw,
        p2_notes=res.p2_notes,
        diff=res.diff,
        finished_at=stamp(),
        cost_usd=round(res.cost, 4),
        followup=res.followup,
    )
    rec.event("done", res.verdict)
    notes = f" (private notes: {len(res.p2_notes)})" if res.p2_notes else ""
    st.report(f"{label} {pr['title'][:70]} ({pr['author']}) -> {res.verdict}: {res.reason}{notes}")
    for note in res.p2_notes:
        st.report(f"    note: {note}")
    st.audit(
        "decision",
        pr=number,
        sha=sha,
        verdict=res.verdict,
        reason=res.reason,
        findings=len(res.findings),
        **extra,
    )
    st.record(number, sha, res.verdict, res.reason, **extra)
    if res.verdict != "APPROVE":
        return
    if res.followup:
        # Lifting the owner's own Request changes is always their call, even when the author re-requested a review.
        log(f"{label} fixed since you asked for changes: left for you to approve")
        return
    # Only a scheduled check approves on its own, and only where the repo's entry turns it on. A review the owner
    # started (Review again, tick --pr) leaves the card for them, since they're right there.
    if not auto or prs.repo(number) not in config.auto_repos(cfg):
        log(f"{label} ready for you to approve: {res.reason}")
        return
    held = yours_hold(cfg, pr) or hold(cfg, st, store, number, tally, pr)
    if held:
        # The card shows a held approval and a later check retries it, so it isn't lost with the decision recorded.
        store.update("reviews", review_id, held=held)
        log(f"{label} approval held: {held}")
        st.report(f"{label} approval held back: {held}")
        return
    actions.approve(cfg, st, store, number, sha, res.reason, actor="timer")
    tally["approved"] += 1
    log(f"{label} approved")


def hold(cfg, st, store, number, tally, pr=None):
    # Someone's Changes requested stands (maybe the owner's, older than the reviews fetched), and only a person lifts it.
    if pr and pr.get("decision") == "CHANGES_REQUESTED":
        return "changes requested on GitHub"
    if st.paused:
        return "paused"
    if store.is_ignored(number):
        return "handed off"
    if st.approvals_today() >= cfg.limits.max_approvals_per_day:
        return "daily cap"
    if tally["approved"] >= cfg.limits.max_approvals_per_tick:
        return "per-check cap"
    if pr:
        # Teammates' instances review the same team requests, so look again just before adding another approval.
        try:
            state = fresh_state(cfg, number)
        except Exception:
            return "couldn't recheck the PR on GitHub"
        if state.get("decision") == "CHANGES_REQUESTED":
            return "changes requested on GitHub"
        if (
            state.get("decision") == "APPROVED"
            or state.get("merge_queue")
            or state.get("state", "OPEN") != "OPEN"
        ):
            return "already approved or merged"
    return None


def yours_hold(cfg, pr):
    """Why an automatic approval waits because the PR is in the owner's own code, where the repo holds those."""
    if not config.holds_yours(cfg, prs.repo(pr.get("id", pr["number"]))):
        return None
    return yours.hold(pr["yours"]) if "yours" in pr else "couldn't tell whose code it changes"


def regate(cfg, pr, sha, rules, now):
    """Why a held approval can't go out now, or None. It faces every gate a fresh approval would, as the PR stands."""
    if pr["head"] != sha:
        return "new commits"
    if pr["decision"] == "CHANGES_REQUESTED":
        return "changes requested on GitHub"
    skip = filters.evaluate(pr, cfg, now)
    if skip:
        return skip.reason
    if filters.lane(pr, rules, cfg)[0] == "never_approve" or filters.followup(pr, cfg.me):
        return "needs your approval"
    if filters.could_hold(pr):
        return "new comments since the review"
    return yours_hold(cfg, pr)


def retry_held(cfg, st, store, tally, rules, now):
    live = config.auto_repos(cfg)
    if not live:
        return
    rows = [
        dict(r)
        for r in store.query(
            """
        SELECT r.* FROM reviews r JOIN (SELECT pr, MAX(id) AS id FROM reviews GROUP BY pr) last ON last.id = r.id
        WHERE r.verdict = 'APPROVE' AND r.held IS NOT NULL AND r.started_at >= ?
          AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.pr = r.pr AND a.sha = r.sha AND a.ok = 1
                          AND a.action IN ('approve', 'release'))
          -- The owner already settled this card (I'd comment, handled), so the retry must not overrule them.
          AND NOT EXISTS (SELECT 1 FROM handled h WHERE h.pr = r.pr AND h.sha = r.sha
                          AND (h.review_id IS NULL OR h.review_id >= r.id))
        ORDER BY r.id""",
            (datetime.now().astimezone() - timedelta(days=14)).isoformat(timespec="seconds"),
        )
    ]
    for r in rows:
        if prs.repo(r["pr"]) not in live:
            continue
        held = hold(cfg, st, store, r["pr"], tally)
        if not held:
            try:
                rcfg, n = config.at(cfg, r["pr"])
                pr = {**gh.fetch_pr(rcfg, n), "id": r["pr"]}
                if pr["head"] == r["sha"] and config.holds_yours(cfg, rcfg.repo):
                    yours.gather(cfg, store, [pr], log)
                held = regate(rcfg, pr, r["sha"], rules.get(rcfg.repo, []), now)
            except Exception as exc:
                held = f"couldn't recheck: {exc}"[:200]
        if held:
            store.update("reviews", r["id"], held=held)
            continue
        try:
            actions.approve(cfg, st, store, r["pr"], r["sha"], r["reason"], actor="timer")
        except Exception as exc:
            store.update("reviews", r["id"], held=str(exc)[:200])
            continue
        store.update("reviews", r["id"], held=None)
        tally["approved"] += 1
        log(f"{prs.label(r['pr'])} approved (held earlier)")


def jitter(cfg, args):
    """Wait a random part of window.jitter_minutes before a timer tick, so teammates don't review in lockstep."""
    if args.trigger != "timer" or args.plan or not cfg.window.jitter_minutes:
        return 0
    delay = random.uniform(0, cfg.window.jitter_minutes * 60)  # noqa: S311 (a delay, not a secret)
    log(f"starting in {delay / 60:.1f} minutes")
    time.sleep(delay)
    return delay


def fresh_state(cfg, number):
    rcfg, n = config.at(cfg, number)
    return gh.pr_states(rcfg, [n]).get(n) or {}


def working_days(start, end):
    days, day = 0, start.date()
    while day < end.date():
        day += timedelta(days=1)
        days += day.weekday() < 5
    return days


def due_back(cfg, handed_off, numbers, now):
    """Handed-off PRs still requested that have sat long enough to check whether anyone picked them up."""
    return {
        n
        for n in numbers
        if n in handed_off
        and working_days(datetime.fromisoformat(handed_off[n]["at"]).astimezone(now.tzinfo), now)
        >= cfg.limits.handoff_working_days
    }


def pr_id(cfg, text):
    """A stored PR id from 123 (the first configured repo) or owner/repo#123."""
    repo, _, number = text.rpartition("#")
    repo = repo or cfg.repos[0].name
    if not number.isdigit() or repo not in [r.name for r in cfg.repos]:
        raise config.ConfigError(
            f"{text!r} isn't a PR number or owner/repo#number for a configured repo"
        )
    return prs.pid(repo, int(number))


def fetch(cfg, number):
    rcfg, n = config.at(cfg, number)
    return {**gh.fetch_pr(rcfg, n), "id": number}


def gather(cfg, st, args):
    if args.pr:
        return [args.pr], {args.pr}, {}, None, set()
    requested, followups = [], []
    for r in cfg.repos:
        rcfg = config.view(cfg, r.name)
        for kind, search, into in (
            ("review request", gh.candidates, requested),
            ("follow-up", gh.followups, followups),
        ):
            try:
                into += [prs.pid(r.name, n) for n in search(rcfg)]
            except Exception as exc:
                log(f"{kind} search failed for {r.name}: {exc}")
                st.report(f"{kind} search failed for {r.name}: {exc}")
    announced = {}
    slack_ok = None
    if cfg.slack.enabled:
        try:
            announced = slack.announced(cfg)
            open_now = {
                prs.pid(r.name, n)
                for r in cfg.repos
                for n in gh.open_numbers(config.view(cfg, r.name))
            }
            announced = {n: ch for n, ch in announced.items() if n in open_now}
            slack_ok = True
        except Exception as exc:
            slack_ok = False
            log(f"slack source unavailable: {exc}")
            st.report(f"slack source unavailable: {exc}")
    numbers = list(dict.fromkeys(requested + followups + sorted(announced)))
    return numbers, set(requested), announced, slack_ok, set(followups)


def source_of(number, requested, announced, args, followups=()):
    if args.pr:
        return "manual"
    if number in requested and number in announced:
        return "both"
    if number in requested:
        return "requested"
    return "slack" if number in announced else "followup" if number in followups else "requested"


VIA = ("follow-up", "requested+slack", "requested", "slack only")


def priority(number, requested, announced, pr, waits=0):
    # An author who pushed fixes is waiting on the owner specifically, so follow-ups go first.
    if pr.get("followup"):
        tier = 0
    else:
        tier = 1 if number in requested and number in announced else 2 if number in requested else 3
    # A PR in the owner's own code or from a team they named moves up a tier, though never ahead of follow-ups.
    rank = max(1, tier - 1) if tier and (yours.kinds(pr.get("yours")) or pr.get("team")) else tier
    # Each check that passed it over for budget moves it up a tier, so a Slack-only PR can't wait forever.
    return max(0, rank - waits), pr["ready_at"], tier


def tick(cfg, args):
    st = State(cfg)
    now = datetime.now(ZoneInfo(cfg.timezone))
    if st.paused:
        log("paused")
        return 0
    if not (args.any_time or in_window(cfg, now)):
        log("outside working window")
        return 0
    if jitter(cfg, args) and st.paused:
        log("paused")
        return 0
    lock = st.lock(wait_s=1800 if args.wait_lock else 0)
    if lock is None:
        log("another tick is running")
        return 0

    store = Store(cfg)
    import_legacy(cfg, store)
    auto = args.trigger == "timer" and not args.pr and not args.plan
    run_id = (
        None
        if args.plan
        else store.start_run(args.trigger, "live" if auto and config.auto_repos(cfg) else "shadow")
    )

    def observe(number, outcome, reason, pr=None):
        if run_id:
            store.observe(
                run_id,
                number,
                outcome,
                reason,
                pr=pr,
                source=source_of(number, requested, announced, args, followups),
                channels=announced.get(number),
            )

    rules = {
        r.name: codeowners.parse(_codeowners_text(config.view(cfg, r.name))) for r in cfg.repos
    }
    review.sweep_worktrees(cfg)
    numbers, requested, announced, slack_ok, followups = gather(cfg, st, args)
    tally = {
        "drafts": 0,
        "defers": 0,
        "approved": 0,
        "reviewed": 0,
        "skipped": 0,
        "errors": 0,
        "cost": 0.0,
        "asked": 0,
        "yours": 0,
        "team": 0,
    }
    if args.pr and not args.plan and store.is_ignored(args.pr):
        store.unignore(args.pr)
        store.action("unignore", args.trigger, pr=args.pr, sha=store.latest_sha(args.pr))
    handed_off = {} if args.pr else store.ignored()
    due = due_back(cfg, handed_off, numbers, now)

    fetched = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch, cfg, n): n for n in numbers if n not in handed_off or n in due
        }
        for future, number in futures.items():
            try:
                fetched[number] = future.result()
            except Exception as exc:
                fetched[number] = exc

    eligible = []
    for number in numbers:
        pr = fetched.get(number)
        label, rcfg = prs.label(number), config.view(cfg, prs.repo(number))
        since = datetime.fromisoformat(handed_off[number]["at"]) if number in handed_off else None
        if (
            number in due
            and isinstance(pr, dict)
            and pr["state"] == "OPEN"
            and pr["decision"] != "APPROVED"
            and not filters.picked_up(pr, since)
        ):
            log(
                f"{label} back from hand-off: nobody reviewed it in {cfg.limits.handoff_working_days} working days"
            )
            if not args.plan:
                store.unignore(number)
                store.action("resurface", "timer", pr=number, sha=pr["head"])
            del handed_off[number]
        if number in handed_off:
            log(f"{label} skip: handed off")
            observe(number, "skip", "handed off", {"title": handed_off[number]["title"]})
            tally["skipped"] += 1
            continue
        try:
            pr = fetched[number]
            if isinstance(pr, Exception):
                raise pr
            skip = filters.evaluate(pr, rcfg, now, st.decided_sha(number))
            if skip and args.force and skip.kind == "skip":
                skip = None
            if skip:
                log(f"{label} {skip.kind}: {skip.reason}")
                observe(number, skip.kind, skip.reason, pr)
                if skip.kind == "defer" and not args.plan:
                    st.report(
                        f"{label} {pr['title'][:70]} ({pr['author']}) -> DEFER: {skip.reason}"
                    )
                    st.record(number, pr["head"], "DEFER", skip.reason)
                    review_id = store.start_review(
                        run_id,
                        pr,
                        source_of(number, requested, announced, args, followups),
                        None,
                        "",
                    )
                    store.update(
                        "reviews",
                        review_id,
                        verdict="DEFER",
                        reason=skip.reason,
                        finished_at=stamp(),
                        cost_usd=0,
                    )
                    tally["defers"] += 1
                else:
                    tally["skipped"] += 1
                continue
            pr["followup"] = filters.followup(pr, cfg.me)
            lane, lane_reason = filters.lane(pr, rules[rcfg.repo], rcfg)
            if lane == "never_approve":
                pr["sensitive"] = filters.sensitive(pr, rules[rcfg.repo], rcfg)
            observe(number, "eligible", lane_reason or lane or "triage", pr)
            eligible.append((pr, lane, lane_reason))
        except Exception as exc:
            tally["errors"] += 1
            log(f"{label} error: {exc}")
            observe(number, "error", str(exc)[:500])
            st.audit("error", pr=number, error=str(exc)[:500])

    # Outside the check for eligible PRs: cards already on To do read these members too.
    team.refresh(cfg, store, log)
    if eligible:
        yours.refresh_emails(cfg, store, log)
        yours.gather(cfg, store, [pr for pr, _, _ in eligible], log)
        people = team.known(cfg, store)
        for pr, _, _ in eligible:
            pr["team"] = team.kind(pr, people)
    ranked = sorted(
        (
            (
                priority(pr["id"], requested, announced, pr, store.waits(pr["id"], pr["head"])),
                pr,
                lane,
                lane_reason,
            )
            for pr, lane, lane_reason in eligible
        ),
        key=lambda item: item[0],
    )
    if auto:
        try:
            retry_held(cfg, st, store, tally, rules, now)
        except Exception as exc:
            log(f"retrying held approvals failed: {exc}")
    snapshots = {}
    for (_, _, tier), pr, lane, lane_reason in ranked:
        number = pr["id"]
        label, rcfg = prs.label(number), config.view(cfg, prs.repo(number))
        via = ", ".join(filter(None, [VIA[tier], *yours.kinds(pr.get("yours")), pr.get("team")]))
        if args.plan:
            mapped = " map only," if filters.too_large(pr, rcfg) else ""
            log(
                f"{label} would review [{via}] ({lane or 'triage'}){mapped} {lane_reason} | {pr['title'][:60]} ({pr['author']})"
            )
            continue
        if tally["reviewed"] >= cfg.limits.max_reviews_per_tick:
            log(f"{label} waiting: review budget for this tick is used")
            observe(number, "waiting", "waiting for a review slot", pr)
            continue
        if not args.pr and store.is_ignored(number):
            log(f"{label} skip: handed off during this check")
            continue
        if not args.pr and store.spend_today() >= cfg.limits.max_spend_per_day_usd:
            log(f"{label} waiting: today's spend reached ${cfg.limits.max_spend_per_day_usd:.2f}")
            observe(number, "waiting", "daily spend cap reached", pr)
            continue
        if not args.pr and not pr.get("followup"):
            try:
                state = fresh_state(cfg, number)
            except Exception:
                state = {}
            if (
                state.get("decision") == "APPROVED"
                or state.get("merge_queue")
                or state.get("state", "OPEN") != "OPEN"
            ):
                log(f"{label} skip: approved or merged since this check started")
                observe(number, "skip", "approved or merged since this check started", pr)
                tally["skipped"] += 1
                continue
        if rcfg.repo not in snapshots:
            snapshots[rcfg.repo] = peers.refresh(rcfg, log)
        review_id = store.start_review(
            run_id, pr, source_of(number, requested, announced, args, followups), lane, lane_reason
        )
        rec = Recorder(store, run_id, review_id, number)
        try:
            log(f"{label} reviewing [{via}] ({lane or 'triage'})")
            res = review.run(
                rcfg,
                pr,
                lane,
                lane_reason,
                rec=rec,
                last=store.prior_review(number, review_id),
                peers=snapshots[rcfg.repo],
            )
            tally["reviewed"] += 1
            tally["cost"] += res.cost
            approved = tally["approved"]
            handle(rcfg, st, store, rec, review_id, pr, res, tally, auto)
            if tally["approved"] == approved:
                signals = yours.kinds(pr.get("yours"))
                tally["asked" if "asked" in signals else "yours"] += bool(signals)
                tally["team"] += pr.get("team") == team.TEAM
        except Exception as exc:
            tally["errors"] += 1
            log(f"{label} error: {exc}")
            store.update("reviews", review_id, error=str(exc)[:1000], finished_at=stamp())
            rec.event("error", str(exc)[:300])
            st.audit("error", pr=number, error=str(exc)[:500])
    summary = (
        f"reviewed={tally['reviewed']} skipped={tally['skipped']} drafts={tally['drafts']} "
        f"defers={tally['defers']} approved={tally['approved']} errors={tally['errors']} "
        f"cost=${tally['cost']:.2f}"
    )
    if not args.plan:
        try:
            calibrate.infer_from_actions(store)
            calibrate.sync_github(cfg, store)
            for proposal_id in actions.learn_from_github(cfg, store):
                log(f"drafting rubric change {proposal_id} from a review you overruled on GitHub")
        except Exception as exc:
            log(f"calibration sync failed: {exc}")
    log(f"tick done: {summary}")
    st.audit("tick", **tally)
    if run_id:
        store.finish_run(run_id, tally, len(numbers), slack_ok)
    needs_you = any(tally[k] for k in ("drafts", "defers", "asked", "yours", "team"))
    if needs_you or tally["errors"]:
        actions.notify(
            cfg,
            "Plus One needs you",
            f"{tally['drafts']} draft(s), {tally['defers']} deferred, {tally['errors']} error(s)."
            + yours_note(tally),
            path="/" if needs_you else "/ops",
        )
    if auto:
        # Let go of the lock first: this check finishing is the idle moment an update waits for.
        lock.close()
        try:
            updates.maybe(cfg, store, log)
        except Exception as exc:
            log(f"update check failed: {exc}")
    return 0


def yours_note(tally):
    parts = [
        f"{tally['team']} from your team" if tally["team"] else "",
        f"{tally['asked']} asked for you by name" if tally["asked"] else "",
        f"{tally['yours']} in your code" if tally["yours"] else "",
    ]
    said = ", ".join(p for p in parts if p)
    return f" {said[0].upper()}{said[1:]}." if said else ""


def _codeowners_text(cfg):
    return gh.run(
        [
            "api",
            f"repos/{cfg.repo}/contents/.github/CODEOWNERS",
            "-H",
            "Accept: application/vnd.github.raw",
        ]
    )


def cmd_status(cfg, args):
    st, store = State(cfg), Store(cfg)
    for r in cfg.repos:
        log(f"approves on its own: {'yes' if r.auto_approve else 'no'} ({r.name})")
    log(f"paused: {st.paused}")
    queued = [
        prs.label(r[0])
        for r in store.query("SELECT DISTINCT pr FROM drafts WHERE status = 'queued' ORDER BY pr")
    ]
    log(f"queued drafts: {queued}")
    log(f"approvals today: {st.approvals_today()}/{cfg.limits.max_approvals_per_day}")
    verdicts = dict(
        store.query(
            "SELECT verdict, COUNT(*) FROM reviews WHERE verdict IS NOT NULL GROUP BY verdict"
        )
    )
    log(f"reviews on record: {verdicts}")
    log(f"web: {cfg.web.public_url}")
    return 0


def cmd_queue(cfg, args):
    store = Store(cfg)
    rows = store.query("""
        SELECT d.pr, r.title, r.author, COUNT(*) AS n,
               SUM(CASE WHEN d.severity = 'P0' THEN 1 ELSE 0 END) AS p0,
               SUM(CASE WHEN d.needs_edit THEN 1 ELSE 0 END) AS edits
        FROM drafts d LEFT JOIN reviews r ON r.id = d.review_id
        WHERE d.status = 'queued' GROUP BY d.pr ORDER BY d.pr""")
    for r in rows:
        flags = "BLOCKING" if r["p0"] else "P1 only"
        log(
            f"{prs.label(r['pr'])} [{flags}] {(r['title'] or '')[:60]} ({r['author']}) comments={r['n']} needs_edit={r['edits']}"
        )
    return 0


def cmd_show(cfg, args):
    store = Store(cfg)
    drafts = actions.queued(store, args.pr)
    if not drafts:
        log("nothing queued")
        return 1
    review = store.query("SELECT * FROM reviews WHERE id = ?", drafts[0]["review_id"])
    if review:
        r = review[0]
        log(
            f"{prs.label(r['pr'])} {r['title']} ({r['author']})\n{r['url']}\nsha {r['sha'][:10]}\nsummary: {r['reason']}"
        )
        log(f"{cfg.web.public_url}/review/{r['id']}\n")
    for d in drafts:
        where = f"{d['path']}:{d['line']}" + (
            "" if d["inline"] else " (outside the diff, goes in review body)"
        )
        log(f"--- {d['severity']} {where}" + ("  NEEDS EDIT" if d["needs_edit"] else ""))
        log(d["body"] + "\n")
    return 0


def cmd_report(cfg, args):
    st = State(cfg)
    day = args.day or datetime.now().astimezone().date().isoformat()
    path = st.dir / "reports" / f"{day}.md"
    log(path.read_text() if path.exists() else "no report for that day")
    return 0


def cmd_release(cfg, args):
    log(f"posted: {actions.release(cfg, State(cfg), Store(cfg), args.pr, force=args.force)}")
    return 0


def cmd_dismiss(cfg, args):
    log(actions.dismiss(cfg, State(cfg), Store(cfg), args.pr, approve_after=not args.no_approve))
    return 0


def cmd_drop(cfg, args):
    actions.drop(State(cfg), Store(cfg), args.pr)
    log("dropped; the next tick will re-review")
    return 0


def cmd_approve(cfg, args):
    store = Store(cfg)
    rows = store.query("SELECT * FROM reviews WHERE pr = ? ORDER BY id DESC LIMIT 1", args.pr)
    if not rows or rows[0]["verdict"] != "APPROVE":
        log("the latest review of that PR is not an APPROVE")
        return 1
    actions.approve(cfg, State(cfg), store, args.pr, rows[0]["sha"], rows[0]["reason"])
    log("approved")
    return 0


def cmd_write_up(cfg, args):
    return 0 if write_up(cfg, Store(cfg), args.draft) else 1


def cmd_rewrite(cfg, args):
    return 0 if rewrite(cfg, Store(cfg), args.rewrite) else 1


def cmd_propose(cfg, args):
    propose.run(cfg, Store(cfg), args.proposal)
    row = Store(cfg).query("SELECT status, error FROM proposals WHERE id = ?", args.proposal)[0]
    log(f"proposal {args.proposal}: {row['status']} {row['error'] or ''}")
    return 0 if row["status"] == "ready" else 1


def cmd_learn(cfg, args):
    store = Store(cfg)
    if args.plan:
        found = learn.reviewed(cfg, args.days)
        read = learn.history(cfg, found, learn.posted_ids(store), args.limit, args.days, log)
        counts = learn.tally(read)
        log(
            f"would read {counts['reviews']} of your reviews on {counts['prs']} PRs in "
            f"{len(learn.batches([learn.record(pr) for pr in read]))} batches, plus one call to write the rubric"
        )
        return 0
    proposal_id = learn.run(cfg, store, args.days, args.limit, log)
    if proposal_id is None:
        log(
            f"no reviews of yours on GitHub in the last {args.days} days to learn from; your rubric is unchanged"
        )
        return 0
    row = store.query("SELECT rationale, note FROM proposals WHERE id = ?", proposal_id)[0]
    print(learn.diff(store, proposal_id))
    log(row["rationale"])
    log(row["note"])
    # Always the owner's call after seeing the change: the rubric was written from text that isn't all theirs.
    if (
        sys.stdin.isatty()
        and input(f"Replace {cfg.rubric_path} with this? [y/N] ").strip().lower() == "y"
    ):
        propose.accept(cfg, store, proposal_id)
        log(f"rubric updated; undo it from Rubric changes in the web app (proposal {proposal_id})")
    else:
        log(
            f"left as rubric change {proposal_id}; accept or reject it from Rubric changes in the web app"
        )
    return 0


def cmd_open(cfg, args):
    if cfg.web.mode != "local":
        print(cfg.web.public_url or "Set web.public_url to the address tailscale serve gives you.")
        return 0
    store = Store(cfg)
    url = (
        signin.link(cfg, store, new=args.new)
        if args.print_only
        else signin.open_in_browser(cfg, store, new=args.new)
    )
    print(url)
    if args.new:
        log("signed every browser out; this link signs you back in")
    log(f"the link works once, for {signin.CODE_SECONDS // 60} minutes")
    return 0


def cmd_doctor(cfg, args):
    return doctor.run(cfg)


def cmd_setup(args):
    return setup.run()


def cmd_update(args):
    return doctor.update(how="rollback" if args.rollback else "auto" if args.auto else "now")


def cmd_restart_web(args):
    units.restart_web()
    return 0


def cmd_install(cfg, args):
    if args.out:
        for path in units.write(Path(args.out), units.files(cfg, web=not args.no_web)):
            log(f"wrote {path}")
        return 0
    for path in units.install(cfg, web=not args.no_web):
        log(f"installed {path}")
    log(
        "checks run every 30 minutes inside your working hours"
        + ("" if args.no_web else ", and the web app is up")
    )
    return 0


def cmd_uninstall(cfg, args):
    for path in units.uninstall():
        log(f"removed {path}")
    return 0


def cmd_ignore(cfg, args):
    store = Store(cfg)
    latest = store.query(
        "SELECT id, title FROM reviews WHERE pr = ? ORDER BY id DESC LIMIT 1", args.pr
    )
    store.ignore(
        args.pr, latest[0]["title"] if latest else None, latest[0]["id"] if latest else None
    )
    store.action("ignore", "cli", pr=args.pr, sha=store.latest_sha(args.pr))
    log(
        f"{prs.label(args.pr)} handed off; checks skip it until you run: plus1 unignore {args.pr_text}"
    )
    return 0


def cmd_unignore(cfg, args):
    store = Store(cfg)
    store.unignore(args.pr)
    store.action("unignore", "cli", pr=args.pr, sha=store.latest_sha(args.pr))
    log(f"{prs.label(args.pr)} is back on your list")
    return 0


def cmd_pause(cfg, args):
    State(cfg).pause()
    log("paused")
    return 0


def cmd_resume(cfg, args):
    State(cfg).resume()
    log("resumed")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plus1")
    parser.add_argument("--version", action="version", version=f"Plus One {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick")
    p.add_argument("--pr", help="a PR number in your first repo, or owner/repo#number")
    p.add_argument("--any-time", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore skip rules for --pr")
    p.add_argument(
        "--plan", action="store_true", help="apply filters and lanes only, no model calls"
    )
    p.add_argument("--trigger", default="manual", choices=["manual", "timer", "ui"])
    p.add_argument(
        "--wait-lock", action="store_true", help="wait for a running tick instead of exiting"
    )
    p.set_defaults(fn=tick)
    for name, fn in (
        ("status", cmd_status),
        ("queue", cmd_queue),
        ("pause", cmd_pause),
        ("resume", cmd_resume),
    ):
        sub.add_parser(name).set_defaults(fn=fn)
    p = sub.add_parser("report")
    p.add_argument("day", nargs="?")
    p.set_defaults(fn=cmd_report)
    for name, fn in (
        ("show", cmd_show),
        ("drop", cmd_drop),
        ("approve", cmd_approve),
        ("ignore", cmd_ignore),
        ("unignore", cmd_unignore),
    ):
        p = sub.add_parser(name)
        p.add_argument("pr", help="a PR number in your first repo, or owner/repo#number")
        p.set_defaults(fn=fn)
    p = sub.add_parser("propose")
    p.add_argument("proposal", type=int)
    p.set_defaults(fn=cmd_propose)
    p = sub.add_parser("learn", help="learn your rubric from the reviews you wrote on GitHub")
    p.add_argument(
        "--days", type=int, default=learn.DAYS, help=f"how far back to read (default {learn.DAYS})"
    )
    p.add_argument(
        "--limit",
        type=int,
        default=learn.LIMIT,
        help=f"most of your reviews to read (default {learn.LIMIT})",
    )
    p.add_argument(
        "--plan", action="store_true", help="count what it would read, with no model calls"
    )
    p.set_defaults(fn=cmd_learn)
    sub.add_parser("setup", help="set Plus One up for you, step by step").set_defaults(
        fn=None, bare=cmd_setup
    )
    sub.add_parser("doctor", help="check that everything Plus One needs is in place").set_defaults(
        fn=cmd_doctor
    )
    p = sub.add_parser("update", help="install the latest Plus One now and restart it")
    p.add_argument(
        "--rollback", action="store_true", help="go back to the version before the last update"
    )
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=None, bare=cmd_update)
    sub.add_parser("restart-web").set_defaults(fn=None, bare=cmd_restart_web)
    p = sub.add_parser("open", help="open the web app, signed in")
    p.add_argument("--new", action="store_true", help="sign every browser out first")
    p.add_argument(
        "--print-only", action="store_true", help="print the link without opening a browser"
    )
    p.set_defaults(fn=cmd_open)
    p = sub.add_parser(
        "install", help="run checks on a timer and keep the web app up, from this checkout"
    )
    p.add_argument("--no-web", action="store_true", help="only the timer")
    p.add_argument("--out", help="write the unit files to this directory without installing them")
    p.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="stop and remove what install set up").set_defaults(
        fn=cmd_uninstall
    )
    p = sub.add_parser("write-up")
    p.add_argument("draft", type=int)
    p.set_defaults(fn=cmd_write_up)
    p = sub.add_parser("rewrite")
    p.add_argument("rewrite", type=int)
    p.set_defaults(fn=cmd_rewrite)
    p = sub.add_parser("release")
    p.add_argument("pr", help="a PR number in your first repo, or owner/repo#number")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_release)
    p = sub.add_parser("dismiss")
    p.add_argument("pr", help="a PR number in your first repo, or owner/repo#number")
    p.add_argument("--no-approve", action="store_true")
    p.set_defaults(fn=cmd_dismiss)
    args = parser.parse_args(argv)
    if getattr(args, "bare", None):
        # These run before there's a config to load, or replace the code that would load it.
        try:
            return args.bare(args)
        except (KeyboardInterrupt, EOFError):
            print("\nstopped; nothing after the last step was written", file=sys.stderr)
            return 1
    try:
        cfg = config.load()
        if getattr(args, "pr", None):
            Store(cfg).db.close()
            args.pr_text, args.pr = args.pr, pr_id(cfg, args.pr)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        return args.fn(cfg, args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.cmd == "tick":
            actions.notify(cfg, "Plus One tick failed", str(exc)[:200])
        return 1
