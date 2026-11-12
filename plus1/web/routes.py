import json
import re
import threading
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import (
    actions,
    calibrate,
    config,
    gh,
    propose,
    prs,
    push,
    settings,
    team,
    units,
    updates,
    yours,
)
from ..store import Store
from . import cards, executor, refs, words
from .app import HERE, STATIC_TYPES, HttpError, Response, queries, redirect

# GitHub can hang for 30 seconds a team, so a save waits this long and the read finishes on its own.
TEAM_READ_WAIT = 5
UPDATE_CHECK_WAIT = 20

# How many past decisions one grading queue holds.
GRADE_QUEUE = 12
CARD_ACTIONS = {
    "post": "release",
    "approve_with_comment": "release",
    "approve": "approve",
    "approve_without_comment": "approve_dismiss",
    "approve_anyway": "approve_anyway",
    "dont_post": "dismiss",
    "handled": "handled",
    "review_later": "drop",
    "hand_off": "ignore",
    "approve_followup": "approve_followup",
    "keep_waiting": "keep_waiting",
}
REWRITING = ("rewrite", "use_rewrite", "keep_mine", "restore_original", "make_rule")
ALLOWED = {
    "blocking": {
        "post",
        "dont_post",
        "approve_anyway",
        "review_later",
        "save_comment",
        "allow",
        *REWRITING,
        "hand_off",
    },
    "worth_fixing": {
        "approve_with_comment",
        "approve_without_comment",
        "dont_post",
        "review_later",
        "save_comment",
        "allow",
        *REWRITING,
        "hand_off",
    },
    "needs_eyes": {"approve", "handled", "hand_off", "write_up"},
    "looks_good": {"approve", "would_comment", "hand_off"},
    "error": {"handled", "hand_off"},
    "fixed": {"approve_followup", "keep_waiting", "hand_off"},
    "still_open": {"approve_followup", "keep_waiting", "hand_off"},
}
SHOW = {
    "all": "Everything",
    "waiting": "Waiting on you",
    "posted": "Posted",
    "not_posted": "Not posted",
    "skipped": "Skipped PRs",
    "handed_off": "Handed off",
}


def register(app):
    def back(path, msg=None, error=None):
        params = {k: v for k, v in (("msg", msg), ("error", error)) if v}
        # The query goes before any #anchor, or the browser keeps it as part of the anchor.
        path, mark, anchor = path.partition("#")
        return redirect(
            path + ("?" + urllib.parse.urlencode(params) if params else "") + mark + anchor
        )

    def safe_next(request, default="/"):
        nxt = request.field("next", default)
        return nxt if nxt.startswith("/") and not nxt.startswith("//") else default

    def card_response(request, number, variant="card", note=None, error=None):
        card = cards.card_for_pr(app.store, app.cfg, number)
        pending = cards.pending_for(app.store, number)
        result = None if pending else executor.last_result(app.store, number)
        if result and card and result["status"] == "cancelled":
            result = None
        # A newer review that hasn't finished hides the card, so say so instead of offering nothing.
        reviewing = card is None and not pending and cards.reviewing(app.store, number)
        return app.render(
            "partials/card.html",
            request,
            card=card,
            pending=pending,
            result=result,
            reviewing=reviewing,
            number=number,
            variant=variant,
            note=note,
            error=error,
            now_ts=time.time(),
            expanded=True,
            describe=executor.describe,
            tags=calibrate.TAGS,
        )

    def card_or_back(request, number, msg=None, error=None):
        if request.htmx:
            return card_response(
                request, number, variant=request.field("variant", "card"), note=msg, error=error
            )
        return back(safe_next(request), msg=msg, error=error)

    def pane_context(detail, todo_cards=None):
        marks = [
            (f["final_path"] or f["path"], f["final_line"] or f["line"])
            for f in detail["findings"]
            if f["verified"]
        ]
        marks += [(d["path"], d["line"]) for d in detail["drafts"]]
        marks += [(x["path"], x["line"]) for x in (detail.get("raw_review") or {}).get("focus", [])]
        nav = cards.neighbors(app.store, app.cfg, detail["pr"], todo_cards)
        latest = app.store.query("SELECT MAX(id) FROM reviews WHERE pr = ?", detail["pr"])[0][0]
        is_latest = latest == detail["id"]
        card = cards.build(app.store, detail, app.cfg) if is_latest else None
        pending = cards.pending_for(app.store, detail["pr"])
        stack = cards.stack_key(detail.get("title"))
        files = queries.parse_diff(detail["diff"], marks)
        sensitive = (
            cards.sensitive_files(app.cfg, detail) if detail.get("lane") == "never_approve" else {}
        )
        for f in files:
            f["sensitive"] = sensitive.get(f["path"])
        return {
            "r": detail,
            "files": files,
            "card": card,
            "number": detail["pr"],
            "ticket": detail.get("ticket") or gh.ticket(detail.get("title")),
            "parts": card["parts"]
            if card
            else cards.stack_parts(app.store, app.cfg, stack[0], stack[2])
            if stack
            else [],
            "handed_off": app.store.ignored().get(detail["pr"]),
            "rr": refs.for_review(app.cfg, detail, here=True),
            "nav": nav,
            "pending": pending,
            "reviewing": not card and not pending and cards.reviewing(app.store, detail["pr"]),
            "result": None if pending else executor.last_result(app.store, detail["pr"]),
            "proposals": [
                p for p in queries.proposals(app.store) if p["review_id"] == detail["id"]
            ],
            "is_latest": is_latest,
            "latest_id": latest,
        }

    def inbox(request, todo_cards, detail, nudge=None):
        return app.render(
            "inbox.html",
            request,
            cards=todo_cards,
            entries=cards.entries(todo_cards),
            nudge=nudge,
            live=queries.live(app.store),
            pane=pane_context(detail, todo_cards) if detail else None,
            now_ts=time.time(),
            describe=executor.describe,
            tags=calibrate.TAGS,
        )

    # Pages

    @app.route("GET", "/")
    def todo(request):
        if not app.run_executor:
            executor.run_due(app.cfg, app.store)
        todo_cards = cards.todo(app.store, app.cfg)
        first = next((c for c in cards.ordered(todo_cards) if not c["pending"]), None)
        nudge = None
        if not todo_cards:
            left = len(gradable())
            stats = calibrate.stats(app.store, app.cfg)
            if not stats["ready"]:
                nudge = {"to_go": stats["approvals_to_go"], "left": left}
        return inbox(
            request,
            todo_cards,
            queries.review_detail(app.store, first["review_id"]) if first else None,
            nudge,
        )

    @app.route("GET", "/activity")
    def activity(request):
        show = request.arg("show", "all")
        if show not in SHOW:
            show = "all"
        try:
            page = max(1, int(request.arg("page", "1")))
        except ValueError:
            page = 1
        q = request.arg("q").strip()
        # Live refreshes pass the seen id the page first loaded with, so the "new since" divider stays put.
        pinned = request.arg("seen")
        seen = int(pinned) if pinned.isdigit() else int(app.store.get_meta("activity_seen_id") or 0)
        if page == 1 and show == "all" and not q:
            app.store.set_meta(
                "activity_seen_id",
                str(app.store.query("SELECT COALESCE(MAX(id), 0) FROM reviews")[0][0]),
            )
        context = {
            "show": show,
            "shows": SHOW,
            "q": q,
            "page": page,
            "live": queries.live(app.store),
            "seen": seen,
        }
        if show == "skipped":
            context["skips"] = queries.skips(app.store, q=q)
        elif show == "handed_off":
            context["handed_off"] = [
                h
                for h in app.store.ignored().values()
                if not q
                or queries._number(q) == prs.number(h["pr"])
                or q.lower() in (h["title"] or "").lower()
            ]
        else:
            context["data"] = queries.activity(app.store, q=q, show=show, page=page)
        return app.render("activity.html", request, **context)

    @app.route("GET", "/insights")
    def insights(request):
        calibrate.infer_from_actions(app.store)
        gates = [
            {
                "repo": r.name,
                "index": i,
                "auto": r.auto_approve,
                "stats": calibrate.stats(app.store, app.cfg, r.name),
            }
            for i, r in enumerate(app.cfg.repos)
        ]
        return app.render(
            "insights.html",
            request,
            stats=calibrate.stats(app.store, app.cfg),
            gates=gates,
            ungraded=len(gradable()),
            synced=app.store.get_meta("github_sync_at"),
            w=queries.weekly(app.store),
            runs=queries.fold_quiet(queries.runs(app.store, limit=30)),
            spend=queries.daily_spend(app.store),
            stages=queries.stage_costs(app.store),
            proposals=queries.proposals(app.store, limit=8),
            factcheck=queries.factcheck_stats(app.store),
            subs=queries.push_subs(app.store),
        )

    for old, new in (
        ("/history", "/activity"),
        ("/live", "/activity"),
        ("/calibration", "/insights"),
        ("/weekly", "/insights"),
        ("/ops", "/insights"),
    ):
        app.route("GET", re.escape(old))(lambda request, _new=new: redirect(_new, status=301))

    def review_detail_or_404(review_id):
        detail = queries.review_detail(app.store, int(review_id))
        if detail is None:
            raise HttpError(404, "No such review.")
        return detail

    @app.route("GET", r"/review/(\d+)")
    def review(request, review_id):
        detail = review_detail_or_404(review_id)
        todo_cards = cards.todo(app.store, app.cfg)
        latest = app.store.query("SELECT MAX(id) FROM reviews WHERE pr = ?", detail["pr"])[0][0]
        if latest == detail["id"] and any(c["pr"] == detail["pr"] for c in todo_cards):
            return inbox(request, todo_cards, detail)
        return app.render(
            "review.html",
            request,
            cards=todo_cards,
            now_ts=time.time(),
            describe=executor.describe,
            tags=calibrate.TAGS,
            **pane_context(detail, todo_cards),
        )

    @app.route("GET", r"/pr/(\d+)")
    def pr(request, number):
        number = int(number)
        return app.render(
            "pr.html",
            request,
            story=queries.pr_story(app.store, number),
            handed_off=app.store.ignored().get(number),
            live_pr=queries.live_pr(app.cfg, number),
            channels=queries.story_channels(app.store, number),
        )

    # Partials

    @app.route("GET", r"/partials/pane/(\d+)")
    def pane_partial(request, review_id):
        return app.render(
            "partials/pane.html",
            request,
            todo_count=None,
            now_ts=time.time(),
            describe=executor.describe,
            tags=calibrate.TAGS,
            **pane_context(review_detail_or_404(review_id)),
        )

    @app.route("GET", r"/partials/card/(\d+)")
    def card_partial(request, number):
        # A card waiting on a rewrite polls without swapping until it lands, so a comment being edited by hand
        # meanwhile isn't wiped every few seconds.
        waiting = request.arg("rewrite")
        if waiting.isdigit():
            rows = app.store.query("SELECT * FROM rewrites WHERE id = ?", int(waiting))
            if rows and rows[0]["status"] == "writing" and not actions.stalled(rows[0]):
                return Response("", status=204)
        # The page around a review card shows the evidence it was rendered with, so a card for another review, or
        # for the review the page waited on, opens that review's own page instead of landing in this one.
        if request.arg("variant") == "page":
            card = cards.card_for_pr(app.store, app.cfg, int(number))
            page = re.search(r"/review/(\d+)", request.handler.headers.get("HX-Current-URL", ""))
            if card and (request.arg("open") or (page and int(page.group(1)) != card["review_id"])):
                return Response(
                    "", status=204, headers={"HX-Redirect": f"/review/{card['review_id']}"}
                )
        return card_response(request, int(number), variant=request.arg("variant", "card"))

    @app.route("GET", "/partials/live")
    def live_partial(request):
        return app.render(
            "partials/live.html",
            request,
            live=queries.live(app.store),
            compact=request.arg("compact") == "1",
        )

    @app.route("GET", "/partials/status")
    def status_partial(request):
        return app.render("partials/status.html", request)

    # Card actions

    @app.route("POST", r"/card/(\d+)/(\w+)")
    def card_action(request, number, action):
        number = int(number)
        try:
            if action == "review_now":
                actions.review_now(app.cfg, number)
                if request.htmx:
                    return app.render(
                        "partials/noted.html",
                        request,
                        number=number,
                        variant=request.field("variant", "card"),
                        review_id=None,
                        note=None,
                        reviewing=True,
                    )
                return back("/activity", msg=f"Reviewing #{number} again now.")
            card = cards.card_for_pr(app.store, app.cfg, number)
            if card is None:
                return card_or_back(
                    request, number, error="This PR no longer needs anything from you."
                )
            if request.field("review_id") != str(card["review_id"]):
                return card_or_back(
                    request,
                    number,
                    error="This PR was reviewed again since you loaded the page. Check the new result first.",
                )
            if action not in ({"handled", "hand_off"} if card["stale"] else ALLOWED[card["state"]]):
                return card_or_back(
                    request, number, error="That action doesn't fit this PR anymore."
                )
            if action in CARD_ACTIONS:
                kind = CARD_ACTIONS[action]
                if card["stale"] and kind in executor.POSTS:
                    return card_or_back(
                        request,
                        number,
                        error="The PR has new commits since this review. Review it again first.",
                    )
                if kind == "release" and any(d["needs_edit"] for d in card["drafts"]):
                    return card_or_back(
                        request,
                        number,
                        error="Check the comment first: part of it isn't backed by the evidence.",
                    )
                executor.schedule(app.store, kind, card)
                return card_or_back(request, number)
            if action == "allow":
                actions.allow_draft(app.store, int(request.field("draft_id")), card["review_id"])
                return card_or_back(request, number, msg="Okay. It can be posted as written.")
            if action == "save_comment":
                actions.update_draft(
                    app.store,
                    int(request.field("draft_id")),
                    request.field("body"),
                    card["review_id"],
                )
                return card_or_back(request, number, msg="Comment saved.")
            if action == "would_comment":
                note = request.field("note").strip()[:1000] or None
                tag = request.field("tag") or "false approval"
                calibrate.grade(
                    app.store,
                    card["review_id"],
                    "disagree",
                    "manual",
                    tag=tag if tag in calibrate.TAGS else "other",
                    note=note,
                )
                app.store.insert(
                    "handled",
                    pr=number,
                    sha=card["sha"],
                    review_id=card["review_id"],
                    how="would comment",
                    at=queries.stamp(),
                )
                if request.htmx:
                    return app.render(
                        "partials/noted.html",
                        request,
                        number=number,
                        variant=request.field("variant", "card"),
                        review_id=card["review_id"],
                        note=note,
                        reviewing=False,
                    )
                return back(
                    f"/review/{card['review_id']}",
                    msg="Noted. You can turn this into a rubric change below.",
                )
            if action == "write_up":
                finding_id = request.field("finding_id", "")
                if not finding_id.isdigit() or int(finding_id) not in {
                    f["id"] for f in card["rejected"]
                }:
                    return card_or_back(request, number, error="Pick a finding to write up.")
                actions.write_up(app.cfg, app.store, card["review_id"], int(finding_id))
                return card_or_back(request, number)
            if action in REWRITING:
                return rewriting(request, number, action, card["review_id"])
        except (executor.PendingError, actions.ActionError) as exc:
            return card_or_back(request, number, error=str(exc))
        raise HttpError(404, "Unknown action.")

    def field_id(request, name):
        value = request.field(name, "")
        if not value.isdigit():
            raise actions.ActionError("That comment isn't on this card anymore.")
        return int(value)

    def rewriting(request, number, action, review_id):
        draft_id = field_id(request, "draft_id")
        if action == "rewrite":
            suggestion, chip = request.field("suggestion").strip(), request.field("chip").strip()
            parent = request.field("parent_id", "")
            actions.rewrite(
                app.cfg,
                app.store,
                draft_id,
                f"{suggestion} {chip}".strip(),
                review_id,
                parent_id=field_id(request, "parent_id") if parent else None,
            )
            return card_or_back(request, number)
        if action == "use_rewrite":
            actions.use_rewrite(app.store, draft_id, field_id(request, "rewrite_id"), review_id)
            return card_or_back(request, number, msg="Using the rewrite.")
        if action == "keep_mine":
            actions.keep_draft(app.store, draft_id, field_id(request, "rewrite_id"), review_id)
            return card_or_back(request, number)
        if action == "restore_original":
            actions.restore_draft(app.store, draft_id, review_id)
            return card_or_back(request, number, msg="Back to the first draft.")
        actions.make_rule(app.cfg, app.store, draft_id, request.field("note"), review_id)
        return card_or_back(
            request,
            number,
            msg="Drafting the rule. It shows up in Rubric changes for you to accept.",
        )

    @app.route("POST", r"/review/(\d+)/reason")
    def reason(request, review_id):
        choice = request.field("reason")
        rows = app.store.query("SELECT pr FROM reviews WHERE id = ?", int(review_id))
        if choice not in calibrate.REASONS or not rows:
            raise HttpError(400, "Unknown reason.")
        calibrate.grade_reason(app.store, int(review_id), choice)
        if request.htmx:
            return card_response(request, rows[0]["pr"], variant=request.field("variant", "card"))
        return back(safe_next(request), msg="Thanks, noted.")

    @app.route("POST", r"/pr/(\d+)/unignore")
    def unignore(request, number):
        app.store.unignore(int(number))
        app.store.action("unignore", "ui", pr=int(number), sha=app.store.latest_sha(int(number)))
        return back(
            safe_next(request, "/activity?show=handed_off"), msg=f"#{number} is back on your list."
        )

    @app.route("GET", "/todo-state.json")
    def todo_state(request):
        current = cards.todo(app.store, app.cfg, light=True)
        body = {"count": len(current), "keys": [f"{c['pr']}:{c['review_id']}" for c in current]}
        return Response(json.dumps(body), content_type="application/json")

    @app.route("POST", r"/pending/(\d+)/undo")
    def undo(request, pending_id):
        rows = app.store.query(
            "SELECT pr, kind, review_id FROM pending_actions WHERE id = ?", int(pending_id)
        )
        if not rows:
            raise HttpError(404, "Nothing to undo.")
        undone = executor.undo(app.store, int(pending_id))
        if undone and rows[0]["kind"] in executor.NOT_POSTING:
            calibrate.clear_reason(app.store, rows[0]["review_id"])
        return card_or_back(
            request,
            rows[0]["pr"],
            error=None if undone else "Too late to undo, it already happened.",
        )

    @app.route("POST", "/review-now")
    def review_now(request):
        target = request.field("target").strip()
        names = [r.name for r in app.cfg.repos]
        m = re.fullmatch(r"#?(\d+)", target)
        link = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", target)
        if m:
            number = prs.pid(names[0], int(m.group(1)))
        elif link and link.group(1) in names:
            number = prs.pid(link.group(1), int(link.group(2)))
        else:
            return back(
                "/",
                error=f"Give a PR number in {names[0]}, or a pull request link from {', '.join(names)}.",
            )
        try:
            actions.review_now(app.cfg, number)
        except actions.ActionError as exc:
            return back("/", error=str(exc))
        return back(
            "/activity", msg=f"Reviewing {prs.label(number)} now. It shows up here as it goes."
        )

    # Grading and rubric

    def _ids(text):
        return [int(x) for x in (text or "").split(",") if x.strip().isdigit()]

    def gradable(skipped=()):
        calibrate.infer_from_actions(app.store)
        waiting = {c["pr"] for c in cards.todo(app.store, app.cfg, light=True)}
        short = calibrate.short_of_approvals(app.store, app.cfg)
        return [
            r
            for r in calibrate.queue(app.store, leave_out=skipped, short=short)
            if r["pr"] not in waiting
        ]

    def queue_url(queue, skipped):
        params = {"q": ",".join(map(str, queue)), "skip": ",".join(map(str, skipped))}
        return "/grade?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})

    def grade_page(request, queue, skipped, partial=False, notice=None):
        """One decision of a grading queue at a time, or the summary once the queue is done. The queue and what was
        skipped ride in the URL; grades come from the table, so a refresh picks up where you were."""
        left = gradable(skipped)
        open_ids = {r["id"] for r in left}
        rows, grades = queries.grade_queue(app.store, queue)
        strip, current = [], None
        for rid in (rid for rid in queue if rid in rows):
            row = rows[rid]
            if rid in grades:
                state = grades[rid]["grade"]
            elif rid in skipped or rid not in open_ids:
                state = "skipped"
            elif current is None:
                state, current = "current", rid
            else:
                state = "next"
            strip.append({**row, "state": state, "tag": grades.get(rid, {}).get("tag")})
        item = None
        if current is not None:
            detail = review_detail_or_404(current)
            status = app.store.query("SELECT * FROM pr_status WHERE pr = ?", detail["pr"])
            item = {
                "r": detail,
                "status": dict(status[0]) if status else None,
                "kept": [f for f in detail["findings"] if f["verified"]],
            }
        stats = calibrate.stats(app.store, app.cfg)
        response = app.render(
            "partials/grade_card.html" if partial else "grade.html",
            request,
            item=item,
            strip=strip,
            position=next((i for i, s in enumerate(strip, 1) if s["state"] == "current"), 0),
            waiting=len([r for r in left if r["id"] not in set(queue)]),
            queue=",".join(map(str, queue)),
            skipped=",".join(map(str, skipped)),
            tags=calibrate.TAGS,
            notice=notice,
            stats=stats,
            checks={c["key"]: c for c in stats["checks"]},
        )
        if partial and queue:
            response.headers["HX-Replace-Url"] = queue_url(queue, skipped)
        return response

    @app.route("GET", "/grade")
    def grade_queue(request):
        queue, skipped = _ids(request.arg("q")), _ids(request.arg("skip"))
        if not queue:
            fresh = [r["id"] for r in gradable(skipped)[:GRADE_QUEUE]]
            if fresh:
                return redirect(queue_url(fresh, skipped))
        return grade_page(request, queue, skipped)

    @app.route("POST", r"/grade/(\d+)")
    def grade_next(request, review_id):
        review_id = int(review_id)
        if not app.store.query("SELECT 1 FROM reviews WHERE id = ?", review_id):
            raise HttpError(404, "No such review.")
        queue = _ids(request.field("q")) or [review_id]
        skipped = _ids(request.field("skip"))
        value = request.field("grade")
        notice = None
        if value == "skip":
            skipped.append(review_id)
        elif value in ("agree", "disagree") and review_id not in {r["id"] for r in gradable()}:
            notice = "That one was graded already or is waiting on To do, so it wasn't graded."
        elif value in ("agree", "disagree"):
            tag = request.field("tag") or None
            if tag and tag not in calibrate.TAGS:
                tag = "other"
            calibrate.grade(
                app.store,
                review_id,
                value,
                "manual",
                tag=tag if value == "disagree" else None,
                note=request.field("note").strip()[:1000] or None,
            )
        else:
            raise HttpError(400, "Pick right call, wrong call, or skip.")
        if request.htmx:
            return grade_page(request, queue, skipped, partial=True, notice=notice)
        return redirect(queue_url(queue, skipped))

    @app.route("POST", r"/review/(\d+)/grade")
    def grade(request, review_id):
        review_id = int(review_id)
        value = request.field("grade")
        nxt = safe_next(request, f"/review/{review_id}")
        if value == "clear":
            calibrate.clear_grade(app.store, review_id)
            return back(nxt, msg="Grade cleared.")
        if value not in ("agree", "disagree"):
            return back(nxt, error="Pick agree or disagree.")
        tag = request.field("tag") or None
        if tag and tag not in calibrate.TAGS:
            tag = "other"
        calibrate.grade(
            app.store,
            review_id,
            value,
            "manual",
            tag=tag if value == "disagree" else None,
            note=request.field("note").strip()[:1000] or None,
        )
        return back(nxt, msg="Thanks, noted.")

    @app.route("POST", r"/review/(\d+)/propose")
    def start_proposal(request, review_id):
        try:
            pid = actions.start_proposal(
                app.cfg, app.store, int(review_id), request.field("note").strip()[:2000]
            )
        except actions.ActionError as exc:
            return back(f"/review/{review_id}", error=str(exc))
        return back(f"/proposals/{pid}", msg="Drafting a rubric change. This takes about a minute.")

    @app.route("POST", "/insights/sync")
    def insights_sync(request):
        try:
            changed = calibrate.sync_github(app.cfg, app.store)
        except Exception as exc:
            return back("/insights", error=f"Couldn't reach GitHub: {str(exc)[:200]}")
        learning = len(actions.learn_from_github(app.cfg, app.store))
        more = (
            f" Drafting {learning} rubric change{'s' if learning != 1 else ''} from them."
            if learning
            else ""
        )
        return back(
            "/insights",
            msg=f"Checked your GitHub reviews. {changed} grade{'s' if changed != 1 else ''} updated.{more}",
        )

    @app.route("POST", "/pause")
    def pause(request):
        app.state.pause()
        app.store.action("pause", "ui")
        return back(
            safe_next(request, "/insights"), msg="Paused. It won't check PRs until you resume."
        )

    @app.route("POST", "/resume")
    def resume(request):
        app.state.resume()
        app.store.action("resume", "ui")
        return back(safe_next(request, "/insights"), msg="Resumed.")

    @app.route("GET", "/proposals")
    def proposal_list(request):
        return app.render("proposals.html", request, rows=queries.proposals(app.store))

    @app.route("GET", r"/proposals/(\d+)")
    def proposal_detail(request, proposal_id):
        p = queries.proposal(app.store, int(proposal_id))
        if p is None:
            raise HttpError(404, "No such proposal.")
        text = propose.diff(p["before"], p["after"]) if p.get("before") and p.get("after") else ""
        return app.render(
            "partials/proposal.html" if request.htmx else "proposal.html",
            request,
            p=p,
            diff_text=text,
        )

    @app.route("POST", r"/proposals/(\d+)/(accept|reject|revert)")
    def proposal_decide(request, proposal_id, decision):
        pid = int(proposal_id)
        try:
            if decision == "accept":
                propose.accept(app.cfg, app.store, pid)
                msg = "Rubric updated. The next review uses it."
            elif decision == "reject":
                propose.reject(app.store, pid)
                msg = "Proposal rejected."
            else:
                propose.revert(app.cfg, app.store, pid)
                msg = "Rubric change reverted."
        except propose.ProposalError as exc:
            return back(f"/proposals/{pid}", error=str(exc))
        app.store.action(f"proposal_{decision}", "ui", payload={"proposal": pid})
        return back(f"/proposals/{pid}", msg=msg)

    # Settings

    SECTIONS = (
        "schedule",
        "sources",
        "skips",
        "budget",
        "every",
        "limits",
        "models",
        "notifications",
        "you",
        "repos",
        "web",
        "files",
        "updates",
    )

    def settings_page(
        request,
        error=None,
        error_field="",
        confirm=None,
        items=(),
        runs=(),
        retypes=(),
        section=None,
        status=200,
    ):
        cfg = app.cfg
        broken = None
        # Read once: another request can take on a hand edit that changes the repos while this one renders.
        configured = cfg.repos
        try:
            lists = [
                (settings.owned(cfg, r.name), settings.inherited(cfg, r.name)) for r in configured
            ]
            top = settings.owned(cfg, None)
            gates = settings.current(cfg, None, *settings.GATES)
            gate_overrides = settings.gate_overrides(cfg)
            disk = settings.ondisk(cfg)
        except Exception as exc:
            # A hand edit that doesn't parse: the app keeps running on the last good config, and nothing saves here.
            broken, lists, top = str(exc), [({}, {}) for _ in configured], {}
            gates, gate_overrides, disk = list(cfg.filters.approval_gates), [], cfg
        defaults = config._read(config.DEFAULTS)
        builtin = {(t, k): defaults.get(t, {}).get(k, []) for t, k in settings.GUARDS}
        repos = [
            {
                "entry": r,
                "index": i,
                "owned": owned,
                "inherited": inherited,
                "stats": calibrate.stats(app.store, cfg, r.name),
            }
            for i, (r, (owned, inherited)) in enumerate(zip(configured, lists, strict=True))
        ]
        rubric = cfg.rubric_path.read_text() if cfg.rubric_path.exists() else ""
        last = app.store.query(
            "SELECT * FROM proposals WHERE status = 'accepted' ORDER BY decided_at DESC LIMIT 1"
        )
        waiting = app.store.query("SELECT COUNT(*) AS n FROM proposals WHERE status = 'ready'")
        window = cfg.window
        status_now = queries.status(cfg, app.store, app.state)
        next_at = queries.next_check_at(cfg)
        today = datetime.now(ZoneInfo(cfg.timezone)).date()
        next_label = next_at and (
            words.clock_time(next_at)
            if next_at.date() == today
            else f"{next_at.strftime('%a')} {words.clock_time(next_at)}"
        )
        pause_file = cfg.state_dir / "PAUSE"
        paused_since = (
            datetime.fromtimestamp(pause_file.stat().st_mtime).astimezone().isoformat()
            if status_now["paused"] and pause_file.exists()
            else None
        )
        log = queries.settings_log(app.store)
        update_view = updates.view(cfg, app.store)
        own_everywhere = sum(len(v) for k, v in top.items() if k[0] == "lanes")
        nav = [
            (
                "Checking",
                [
                    (
                        "schedule",
                        "Schedule",
                        f"{words.days_name(window.days)}, "
                        f"{words.hour_name(window.start_hour)} to {words.hour_name(window.end_hour + 1)}",
                        False,
                    ),
                    (
                        "sources",
                        "Sources",
                        f"Review requests, {len(vars(cfg.slack.channels))} channels"
                        if cfg.slack.enabled
                        else "Review requests",
                        False,
                    ),
                    ("skips", "Waits and skips", f"{cfg.limits.min_ready_minutes} min wait", False),
                    (
                        "budget",
                        "Budget",
                        f"{settings.show('max_spend_per_day_usd', cfg.limits.max_spend_per_day_usd)} a day",
                        False,
                    ),
                ],
            ),
            (
                "Approving on its own",
                [
                    (
                        f"repo-{r['index']}",
                        r["entry"].name,
                        "On" if r["entry"].auto_approve else "Off",
                        r["entry"].auto_approve,
                    )
                    for r in repos
                ]
                + [
                    (
                        "every",
                        "Every repo",
                        f"{own_everywhere} of your own" if own_everywhere else "Built-in only",
                        False,
                    ),
                    ("limits", "Limits", f"{cfg.limits.max_approvals_per_day} a day", False),
                ],
            ),
            (
                "Reviewing",
                [
                    ("rubric", "Rubric", f"{len(rubric.split()):,} words", False),
                    (
                        "models",
                        "Models",
                        f"{cfg.models.deep.model.capitalize()} for deep review",
                        False,
                    ),
                    ("repos", "Repos", f"{len(repos)} repo{'s' if len(repos) != 1 else ''}", False),
                ],
            ),
            (
                "Notifications",
                [
                    (
                        "notifications",
                        "Notifications",
                        f"Desktop {'on' if cfg.notify else 'off'}",
                        False,
                    )
                ],
            ),
            (
                "System",
                [
                    ("you", "You", cfg.me, False),
                    (
                        "web",
                        "Web app",
                        "Over Tailscale" if cfg.web.mode == "tailscale" else "On this computer",
                        False,
                    ),
                    ("files", "Files", "", False),
                    ("updates", "Updates", settings.MODE_WORDS[cfg.updates.mode], False),
                    ("changes", "Recent changes", "", False),
                ],
            ),
        ]
        response = app.render(
            "settings.html",
            request,
            status=status_now,
            repos=repos,
            top=top,
            builtin=builtin,
            gates=gates,
            gate_overrides=gate_overrides,
            found_emails=yours.detected(cfg, app.store),
            update=update_view,
            update_line=words.update_line(update_view, cfg.updates.mode),
            team_status={
                "teams": team.status(cfg.team.teams, app.store),
                "others": team.status(cfg.team.others, app.store),
            },
            disk=disk,
            pending=settings.restart_pending(cfg),
            web_installed=units.installed(web=True),
            efforts=settings.EFFORTS,
            model_names=settings.MODELS,
            defaults=defaults,
            system_zone=config.system_zone(),
            days=settings.DAYS,
            hours=[(h, words.clock_hour(h)) for h in range(25)],
            when=f"{words.days_name(window.days)}, {words.hour_name(window.start_hour)} to "
            f"{words.hour_name(window.end_hour + 1)}",
            zone_abbr=datetime.now(ZoneInfo(cfg.timezone)).strftime("%Z"),
            next_label=next_label,
            next_today=bool(next_at) and next_at.date() == today,
            paused_since=paused_since,
            nav=nav,
            spend_today=app.store.spend_today() or 0.0,
            spend_days=queries.daily_spend(app.store, 14),
            approvals_today=status_now["approvals_today"],
            stage_costs={s["stage"]: s for s in queries.stage_costs(app.store, days=14)},
            channel_counts=queries.channel_counts(app.store),
            outline=settings.outline(rubric),
            rubric_words=len(rubric.split()),
            rubric_change=dict(last[0]) if last else None,
            proposals_waiting=waiting[0]["n"] if waiting else 0,
            subs=queries.push_subs(app.store) if cfg.web.mode == "tailscale" else None,
            vapid=push.public_key(cfg) if cfg.web.mode == "tailscale" else "",
            log=log,
            channels="\n".join(f"{k}={c}" for k, c in vars(cfg.slack.channels).items()),
            sent={k: v[0] for k, v in request.form.items()} if section else {},
            section=section,
            error=error,
            error_field=error_field,
            confirm=confirm,
            items=items,
            runs=runs,
            retypes=retypes,
            confirm_word=settings.CONFIRM,
            run_word=settings.RUN,
            broken=broken,
        )
        response.status = status
        return response

    def save_settings(request, section, repo=None, anchor=None):
        form = {k: v[0] for k, v in request.form.items()}
        try:
            if repo is not None and form.get("repo") != repo:
                raise settings.SettingsError(
                    "Your repos changed since this page loaded, so nothing was saved. Reload it and try again."
                )
            with settings.LOCK:
                changes = settings.changed(app.cfg, settings.parse(section, form, app.cfg, repo))
                if not changes:
                    return back(f"/settings#{anchor}", msg="Nothing changed.")
                before = {settings.key_name(*k): settings.current(app.cfg, *k) for k in changes}
                again = {key: request.field(f"again_{key}") for _, key in settings.RETYPE}
                app.adopt(
                    settings.save(
                        app.cfg,
                        changes,
                        request.field("confirm"),
                        request.field("confirm_run"),
                        again,
                    )
                )
        except settings.NeedsConfirm as exc:
            return settings_page(
                request,
                section=anchor,
                status=422,
                confirm=anchor,
                items=exc.items,
                runs=exc.runs,
                retypes=exc.retypes,
            )
        except settings.SettingsError as exc:
            return settings_page(
                request, error=str(exc), error_field=exc.field, section=anchor, status=422
            )
        app.store.action(
            "settings",
            "ui",
            payload={
                "section": anchor,
                "changes": {settings.key_name(*k): v for k, v in changes.items()},
                "before": before,
            },
        )
        return back(f"/settings#{anchor}", msg="Saved. The next check uses it.")

    @app.route("GET", "/settings")
    def settings_view(request):
        return settings_page(request)

    @app.route("POST", rf"/settings/({'|'.join(SECTIONS)})")
    def settings_save(request, section):
        response = save_settings(request, section, anchor=section)
        if section == "you":
            read_teams()
        return response

    def on_a_thread(name, wait_s, work):
        """Runs work(store) without letting a slow GitHub hold up the page. Returns whether it finished in time."""
        cfg = app.cfg

        def run():
            store = Store(cfg)
            try:
                work(cfg, store)
            finally:
                store.db.close()

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        thread.join(wait_s)
        return not thread.is_alive()

    def read_teams():
        on_a_thread("plus1-team-read", TEAM_READ_WAIT, team.refresh)

    @app.route("POST", "/settings/updates/check")
    def settings_updates_check(request):
        done = on_a_thread("plus1-update-check", UPDATE_CHECK_WAIT, updates.check)
        msg = "Checked for updates." if done else "Still asking GitHub. Reload in a moment."
        return back(updates.PAGE, msg=msg)

    def start_update(*args, msg):
        try:
            actions.spawn(app.cfg, "update", "update", *args)
        except actions.ActionError as exc:
            return back(updates.PAGE, error=str(exc))
        return back(updates.PAGE, msg=msg)

    @app.route("POST", "/settings/updates/install")
    def settings_updates_install(request):
        return start_update(msg="Updating. This page reloads when it's done.")

    @app.route("POST", "/settings/updates/rollback")
    def settings_updates_rollback(request):
        return start_update("--rollback", msg="Rolling back. This page reloads when it's done.")

    @app.route("GET", "/whats-new")
    def whats_new(request):
        return app.render("whats_new.html", request, releases=updates.changelog())

    def repo_change(before, after):
        app.store.action(
            "settings",
            "ui",
            payload={"section": "repos", "changes": {"repos": after}, "before": {"repos": before}},
        )

    @app.route("GET", "/settings/repos/suggest")
    def settings_repo_suggestions(request):
        from .. import setup

        configured = {r.name for r in app.cfg.repos}
        try:
            names = [n for n in setup.suggest_repos(limit=8) if n not in configured]
        except Exception:
            return app.render(
                "partials/repo_suggestions.html", request, todo_count=None, failed=True
            )
        found = [(n, setup.find_checkout(n)) for n in names]
        return app.render(
            "partials/repo_suggestions.html", request, todo_count=None, found=found, failed=None
        )

    @app.route("GET", "/settings/team/suggest")
    def settings_team_suggestions(request):
        target = "others" if request.arg("list") == "others" else "teams"
        repos = [r.name for r in app.cfg.repos]
        taken = {*app.cfg.team.teams, *app.cfg.team.others}
        try:
            found = (
                [(n, team.size(c)) for n, c in team.suggest_mine(repos) if n not in taken]
                if target == "teams"
                else [(n, "") for n in team.suggest_others(repos) if n not in taken]
            )
        except Exception:
            found, failed = [], True
        else:
            failed = False
        return app.render(
            "partials/team_suggestions.html",
            request,
            todo_count=None,
            found=found,
            target=target,
            failed=failed,
        )

    @app.route("POST", "/settings/repos/add")
    def settings_repo_add(request):
        before = [r.name for r in app.cfg.repos]
        try:
            with settings.LOCK:
                app.adopt(
                    settings.add_repo(app.cfg, request.field("name"), request.field("checkout"))
                )
        except settings.SettingsError as exc:
            return back("/settings#repos", error=str(exc))
        repo_change(before, [r.name for r in app.cfg.repos])
        return back(
            "/settings#repos",
            msg=f"Added {request.field('name').strip()}. The next check includes it.",
        )

    @app.route("POST", r"/settings/repo/(\d+)/remove")
    def settings_repo_remove(request, index):
        before = [r.name for r in app.cfg.repos]
        name = request.field("repo")
        if int(index) >= len(before) or before[int(index)] != name:
            return back(
                "/settings#repos",
                error="Your repos changed since this page loaded, so nothing was removed. Reload it and try again.",
            )
        try:
            with settings.LOCK:
                app.adopt(settings.remove_repo(app.cfg, name))
        except settings.SettingsError as exc:
            return back("/settings#repos", error=str(exc))
        repo_change(before, [r.name for r in app.cfg.repos])
        return back("/settings#repos", msg=f"Removed {name}. Its reviews and history stay.")

    @app.route("POST", "/settings/restart")
    def settings_restart(request):
        try:
            actions.spawn(app.cfg, "restart-web", "restart-web")
        except actions.ActionError as exc:
            return back("/settings#web", error=str(exc))
        return back("/settings#web", msg="Restarting the web app. Reload in a few seconds.")

    @app.route("POST", "/settings/rubric-limits")
    def settings_rubric_limits(request):
        return save_settings(request, "rubric", anchor="rubric")

    @app.route("POST", r"/settings/repo/(\d+)")
    def settings_repo(request, index):
        if int(index) >= len(app.cfg.repos):
            raise HttpError(404, "No such repo.")
        return save_settings(
            request, "repo", app.cfg.repos[int(index)].name, anchor=f"repo-{index}"
        )

    @app.route("GET", "/settings/rubric")
    def rubric_edit(request):
        text = app.cfg.rubric_path.read_text()
        return app.render(
            "rubric_edit.html", request, text=text, diff_text="", base=propose.version(text)
        )

    @app.route("POST", "/settings/rubric")
    def rubric_save(request):
        text, base = request.field("text"), request.field("base")
        if request.field("save"):
            try:
                pid = propose.save_edit(app.cfg, app.store, text, base)
            except propose.ProposalError as exc:
                return app.render(
                    "rubric_edit.html",
                    request,
                    text=text,
                    error=str(exc),
                    base=base,
                    diff_text=propose.diff(app.cfg.rubric_path.read_text(), text),
                )
            return back(
                f"/proposals/{pid}", msg="Rubric saved. Revert it here if it doesn't work out."
            )
        current = app.cfg.rubric_path.read_text()
        cleaned = (
            "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines())
            + "\n"
        )
        return app.render(
            "rubric_edit.html",
            request,
            text=text,
            diff_text=propose.diff(current, cleaned),
            preview=True,
            base=base,
        )

    @app.route("POST", "/settings/learn")
    def learn_again(request):
        try:
            actions.spawn(app.cfg, "learn", "learn")
        except actions.ActionError as exc:
            return back("/settings#rubric", error=str(exc))
        return back(
            "/proposals",
            msg="Reading your GitHub reviews. The new rubric shows up here in a few minutes, "
            "to accept or not.",
        )

    # Devices and push

    @app.route("GET", "/devices")
    def devices(request):
        return redirect("/settings#notifications")

    @app.route("POST", "/push/subscribe")
    def push_subscribe(request):
        if app.cfg.web.mode != "tailscale":
            raise HttpError(400, 'Phone notifications need web.mode = "tailscale".')
        try:
            push.subscribe(
                app.store,
                request.field("endpoint"),
                request.field("p256dh"),
                request.field("auth"),
                request.field("label") or "device",
            )
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None
        return Response("ok", content_type="text/plain")

    @app.route("POST", r"/push/(\d+)/delete")
    def push_delete(request, sub_id):
        app.store.db.execute("DELETE FROM push_subs WHERE id = ?", (int(sub_id),))
        return back("/settings#notifications", msg="Device removed.")

    @app.route("POST", "/push/test")
    def push_test(request):
        sent = push.send_all(
            app.cfg,
            app.store,
            "Plus One test",
            "Notifications reach this device.",
            app.cfg.web.public_url.rstrip("/") + "/",
        )
        return back(
            "/settings#notifications",
            msg=f"Sent a test to {sent} device{'s' if sent != 1 else ''}.",
        )

    # Plumbing

    @app.route("GET", "/events")
    def events(request):
        last = request.handler.headers.get("Last-Event-ID")
        last_id = (
            int(last)
            if last and last.isdigit()
            else app.store.db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        )
        request.handler.stream_events(app.store, last_id)
        return None

    @app.route("GET", r"/static/([\w.-]+)")
    def static(request, name):
        path = HERE / "static" / name
        if path.suffix not in STATIC_TYPES or not path.is_file():
            raise HttpError(404, "Not found.")
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable"
            if path.suffix == ".woff2"
            else "no-cache"
        }
        if name == "sw.js":
            headers["Service-Worker-Allowed"] = "/"
        return Response(path.read_bytes(), content_type=STATIC_TYPES[path.suffix], headers=headers)

    @app.route("GET", "/palette.json")
    def palette(request):
        items = [
            {
                "pr": prs.number(r["pr"]),
                "label": prs.label(r["pr"]),
                "repo": prs.repo(r["pr"]),
                "title": r["title"] or "",
                "author": r["author"] or "",
                "review": r["id"],
                "outcome": "Couldn't review" if r["error"] else words.OUTCOME.get(r["verdict"], ""),
            }
            for r in queries.palette(app.store)
        ]
        body = {"paused": app.state.paused, "prs": items}
        return Response(json.dumps(body), content_type="application/json")

    @app.route("GET", "/healthz")
    def health(request):
        return Response(f"ok {updates.build()}", content_type="text/plain")

    def grade_for(review_id):
        rows = app.store.query(
            "SELECT grade, tag, note, source FROM grades WHERE review_id = ?", review_id
        )
        return dict(rows[0]) if rows else None

    app.env.globals.update(
        words=words,
        grade_for=grade_for,
        reasons=calibrate.REASONS,
        not_posting=executor.NOT_POSTING,
    )
