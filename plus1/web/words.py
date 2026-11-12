import re

from .. import settings
from ..review import FOLLOWUP_OPEN, FOLLOWUP_UNCHANGED
from ..store import decode_json
from ..team import OTHER, TEAM
from ..yours import MONTHS, kinds

LANES = {
    "trivial": "small change",
    "substantive": "full review",
    "dependabot": "dependency bump",
    "never_approve": "sensitive area",
}

SEVERITY = {"P0": "Blocking", "P1": "Worth fixing"}

AGENTS = {"devin": "Devin", "claude-code": "Claude Code"}

YOURS = {"asked": "asked you", "code": "your code", "files": "your files"}
TEAM_CHIP = {TEAM: "team", OTHER: "watching"}


def update_line(update, mode):
    state, latest = update["state"], update["state"].get("latest")
    status = state.get("status")
    if update["installing"]:
        return f"Installing {update['installing']['to']} now."
    if state.get("error"):
        return f"Couldn't ask GitHub: {state['error']}"
    if status == "available":
        return f"{latest} is ready." + (
            " It installs at the next idle moment." if mode == "automatic" else ""
        )
    if status == "skipped":
        return f"Skipping {latest}, which was rolled back, until a newer version is out."
    if status == "blocked":
        return f"{latest} is waiting, but {state['reason']}."
    return "You're on the latest version." if status == "current" else "Not checked yet."


STATE = {
    "blocking": ("Found a blocking problem", "bad"),
    "worth_fixing": ("Worth fixing before it ships", "warn"),
    "needs_eyes": ("Needs your eyes", "info"),
    "looks_good": ("Looks good, would approve", "ok"),
    "error": ("Couldn't review this one", "err"),
    "fixed": ("Fixed since you asked for changes", "ok"),
    "overtaken": ("Approved without your blocking comment", "bad"),
    "still_open": ("Still open since you asked for changes", "warn"),
}

GROUP = {
    "blocking": "Blocking",
    "worth_fixing": "Worth fixing",
    "needs_eyes": "Needs a look",
    "looks_good": "Ready to approve",
    "error": "Couldn’t review",
    "stale": "New commits",
    "fixed": "Fixed since you asked",
    "overtaken": "Approved without your blocking comment",
    "still_open": "Waiting on fixes",
}

LARGE_DIFF = 500

VERDICT_TONE = {"APPROVE": "ok", "DRAFT": "warn", "DEFER": "info"}

PROPOSAL_TONE = {"accepted": "ok", "ready": "info", "pending": "warn", "failed": "bad"}

STAGE = {
    "triage": "Sort",
    "deep": "Review",
    "verify": "Double-check",
    "draft": "Write comment",
    "factcheck": "Fact-check comment",
}

OUTCOME = {
    "APPROVE": "Looks good",
    "DRAFT": "Found something",
    "DEFER": "Needed your eyes",
    None: "Didn't finish",
}

ACTION = {
    "release": "You posted the comment",
    "approve": "Approved on GitHub",
    "dismiss": "You chose not to post",
    "drop": "Sent back for another review",
    "edit_draft": "You edited the comment",
    "rewrite_draft": "You asked for a rewrite",
    "use_rewrite": "You used the rewrite",
    "keep_draft": "You kept your comment over the rewrite",
    "restore_draft": "You went back to the first draft",
    "factcheck_false_alarm": "You said the fact-check was wrong",
    "ignore": "You handed it off",
    "unignore": "You brought it back",
    "resolve_thread": "Resolved your fixed comment",
    "resurface": "Came back: nobody else reviewed it",
}

NOT_DONE = {
    "release": "Comment not posted",
    "approve": "Not approved",
    "approve_dismiss": "Not approved",
    "approve_anyway": "Not approved",
    "dismiss": "Comment not skipped",
    "handled": "Not marked as handled",
    "drop": "Not sent back",
    "ignore": "Not handed off",
    "approve_followup": "Not approved",
    "keep_waiting": "Not marked as waiting",
}

UNSURE = {
    "release": "Comment may not have posted",
    "approve": "Approval may not have gone through",
    "approve_dismiss": "Approval may not have gone through",
    "approve_anyway": "Approval may not have gone through",
    "approve_followup": "Approval may not have gone through",
}

# actions._head_check raises this text when the PR moved past the reviewed commit.
HEAD_MOVED = re.compile(r"has new commits \((\w+) is now (\w+)\)")

OWNED = re.compile(r"(\S+) is owned by (\S+)")
RESTRICTED = re.compile(r"(\S+) matches a restricted path")
LARGE = re.compile(r"too large to trace \((\d+) changed lines\)")
OPEN_COMMENTS = re.compile(r"clean, but open comments from (.+)")
PLAIN_PREFIXES = (
    "clean, but ",
    "too large to trace",
    "thin coverage: ",
    "findings did not survive verification: ",
    FOLLOWUP_OPEN,
    FOLLOWUP_UNCHANGED,
)


def decision(state, comments, stale, clean=False, waiting=False):
    if stale:
        return "New commits since this review"
    n = f"{comments} comment{'' if comments == 1 else 's'}"
    if state == "worth_fixing" and waiting:
        return f"Post {n}, and keep asking for changes?"
    return {
        "blocking": f"Request changes with {n}?",
        "worth_fixing": f"Approve with {n}?",
        "needs_eyes": "Look it over, then approve?"
        if clean
        else "Check it on GitHub, then approve?",
        "looks_good": "Approve it?",
        "error": "Try the review again?",
        "fixed": "Approve it?",
        "still_open": "Approve anyway, or keep waiting?",
    }[state]


def followup_line(items):
    still = [i for i in items if i["open"]]
    n = len(items)
    if not still:
        return (
            f"All {n} of your comments are fixed."
            if n > 1
            else "Your comment is fixed."
            if n
            else "Nothing you asked for is still open."
        )
    return (
        f"{len(still)} of {n} still open: {still[0]['title']}"
        if n > 1
        else f"Still open: {still[0]['title']}"
    )


def overtaken(status):
    if not status or status["state"] != "OPEN":
        return None
    if status["merge_queue"]:
        return "in the merge queue"
    return "approved by someone else" if status["decision"] == "APPROVED" else None


def unposted(label, status, drafts):
    event = {"MERGED": "merged", "CLOSED": "was closed"}.get(status["state"]) or (
        "entered the merge queue" if status["merge_queue"] else "was approved by someone else"
    )
    n = len(drafts)
    kind = "blocking comment" if all(d["severity"] == "P0" for d in drafts) else "comment"
    what = kind if n == 1 else f"{n} {kind}s"
    return f"{label} {event} before your {what} went out."


def _many(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def yours(found):
    """The row chips and card sentences for what the owner has to do with a PR (plus1.yours), each only when it holds."""
    found = decode_json(found, {}) or {}
    signals, lines = kinds(found), []
    if "asked" in signals:
        lines.append(f"{found['asked']} asked you by name")
    if "code" in signals:
        top = found.get("top") or [["", 0]]
        more = max(found.get("top_count", len(top)) - 1, 0)
        where = f"`{top[0][0].rsplit('/', 1)[-1]}`" + (
            f" and {_many(more, 'more file')}" if more else ""
        )
        lines.append(f"Rewrites {_many(found['lines'], 'line')} you wrote in {where}")
    if "files" in signals:
        path, times = found["files"][0]
        more = max(found.get("files_count", len(found["files"])) - 1, 0)
        tail = f", and to {_many(more, 'more file')} it changes" if more else ""
        lines.append(
            f"You committed to `{path.rsplit('/', 1)[-1]}` {times} times in {MONTHS} months{tail}"
        )
    return [YOURS[s] for s in signals], lines


def flags(state, lane, size, agent):
    out = []
    if lane == "never_approve" and state != "needs_eyes":
        out.append("sensitive area")
    if size >= LARGE_DIFF:
        out.append(f"{size:,} lines")
    if agent:
        out.append("agent-written")
    return out


def lane(value):
    return LANES.get(value or "", value or "")


def _names(joined):
    names = joined.split(", ")
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def defer_reason(reason):
    reason = reason or ""
    if reason == FOLLOWUP_UNCHANGED:
        return "Its files haven't changed since you asked for changes."
    if reason.startswith(FOLLOWUP_OPEN):
        return "Still open since you asked for changes: " + reason[len(FOLLOWUP_OPEN) :]
    m = OPEN_COMMENTS.match(reason)
    if m:
        return f"Looks clean, but {_names(m.group(1))} left comments the author hasn't addressed, so approving is up to you."
    m = re.match(r"clean, but (\S+) is owned by (\S+)", reason)
    if m:
        return f"Looks clean, but it touches {m.group(1)} (owned by {m.group(2)}), so approving is up to you."
    m = re.match(r"clean, but (\S+) matches a restricted path", reason)
    if m:
        return f"Looks clean, but {m.group(1)} is in a sensitive area, so approving is up to you."
    m = LARGE.match(reason)
    if m:
        return f"Too big to review on its own ({m.group(1)} changed lines)."
    if reason.startswith("thin coverage: "):
        return (
            "It couldn't trace enough of the code to be sure. " + reason[len("thin coverage: ") :]
        )
    if reason.startswith("findings did not survive verification: "):
        return (
            "It had concerns but couldn't confirm them. "
            + reason[len("findings did not survive verification: ") :]
        )
    return reason


def skip_reason(reason):
    reason = reason or ""
    plain = {
        "draft": "Still a draft",
        "authored by me": "Your own PR",
        "already approved overall": "Already approved by someone else",
        "in the merge queue": "In the merge queue",
        "merge conflict": "Has a merge conflict",
        "already decided at this commit": "Already reviewed this commit",
        "I already reviewed this commit": "You already reviewed this commit",
        "CI still pending": "CI still running",
        "ready for under the minimum wait": "Just opened, giving the author a few minutes",
        "not open": "Closed or merged",
        "handed off": "You handed it off",
        "waiting for a review slot": "Waiting for a review slot. It moves up each check it waits",
        "daily spend cap reached": "Today's spend cap is reached, so it waits for tomorrow",
        "approved or merged since this check started": "Approved, merged, or closed before its turn came",
    }
    if reason in plain:
        return plain[reason]
    if reason.startswith("required check failing: "):
        return "CI failing: " + reason.split(": ", 1)[1]
    if reason.startswith("changes requested by "):
        return f"{reason.split('by ', 1)[1]} requested changes"
    if reason.startswith("open blocking thread from "):
        return f"{reason.split('from ', 1)[1]} left a blocking comment"
    if reason.startswith("hold label: "):
        return "Labeled " + reason.split(": ", 1)[1]
    if reason.startswith("too large to trace"):
        return defer_reason(reason)
    return reason


def lane_plain(lane_reason):
    m = OWNED.match(lane_reason or "")
    if m:
        return f"it touches `{m.group(1)}`, owned by {m.group(2)}"
    m = RESTRICTED.match(lane_reason or "")
    if m:
        return f"it touches `{m.group(1)}`, a sensitive path"
    return ""


def waiting(review):
    reason = review.get("reason") or ""
    raw = decode_json(review.get("raw_review") or {}, {})
    large = LARGE.match(reason)
    if large and raw.get("focus"):
        return (
            f"Too big to trace in full ({int(large.group(1)):,} changed lines), so it marked where to look first.",
            raw.get("summary") or "",
        )
    if large:
        return defer_reason(reason), ""
    thin = raw.get("coverage") == "thin" or reason.startswith("thin coverage: ")
    unconfirmed = reason.startswith("findings did not survive verification: ")
    where = lane_plain(review.get("lane_reason"))
    if review.get("lane") == "never_approve" and where:
        clean = clean_defer(review)
        lead = f"{'Looks clean. ' if clean else ''}Waiting on you because {where}."
        if thin:
            lead += " It couldn't trace all of it."
        elif unconfirmed:
            lead += " It had concerns it couldn't confirm."
        elif not clean:
            lead += " It couldn't decide on its own."
    elif reason.startswith("clean, but "):
        lead = defer_reason(reason)
    elif review.get("lane") == "dependabot":
        lead = (
            "Dependency bump it couldn't fully check."
            if thin
            else "Dependency bump that needs your call."
        )
    elif unconfirmed:
        lead = "It had concerns but couldn't confirm them."
    elif thin:
        lead = "It couldn't trace enough of the code to be sure."
    else:
        lead = "It couldn't decide on this one."
    detail = raw.get("summary") or ("" if reason.startswith(PLAIN_PREFIXES) else reason)
    return lead, ("" if detail == lead else detail)


def clean_defer(review):
    raw = decode_json(review.get("raw_review") or {}, {})
    if raw.get("verdict") != "APPROVE" or raw.get("coverage") == "thin" or raw.get("findings"):
        return False
    # Only review.run's holds (the lane cap and open comments) pair the reviewer's APPROVE with this reason.
    # Checking both keeps a reviewer's own DEFER from reading as clean, even when its summary happens to start
    # the same way.
    return (review.get("reason") or "").startswith("clean, but ")


def waiting_line(review):
    reason = review.get("reason") or ""
    raw = decode_json(review.get("raw_review") or {}, {})
    m = LARGE.match(reason)
    if m:
        return f"Too big to {'trace in full' if raw.get('focus') else 'review on its own'} ({int(m.group(1)):,} lines)"
    m = OPEN_COMMENTS.match(reason)
    if m and clean_defer(review):
        return f"Clean, but open comments from {_names(m.group(1))}"
    thin = raw.get("coverage") == "thin" or reason.startswith("thin coverage: ")
    unconfirmed = reason.startswith("findings did not survive verification: ")
    lane_reason = review.get("lane_reason") or (
        reason[len("clean, but ") :] if reason.startswith("clean, but ") else ""
    )
    owned, restricted = OWNED.match(lane_reason), RESTRICTED.match(lane_reason)
    where = ""
    if owned:
        where = f"{owned.group(2)} owns {owned.group(1).rsplit('/', 1)[-1]}"
    elif restricted:
        where = f"{restricted.group(1).rsplit('/', 1)[-1]} is a sensitive path"
    if where:
        if thin:
            return f"Couldn't trace all of it, and {where}"
        if unconfirmed:
            return f"Concerns it couldn't confirm, and {where}"
        return f"Clean, but {where}" if clean_defer(review) else f"Couldn't decide, and {where}"
    if review.get("lane") == "dependabot":
        return "Dependency bump it couldn't fully check" if thin else "Dependency bump"
    if unconfirmed:
        return "Concerns it couldn't confirm"
    if thin:
        return "Couldn't trace enough of the code"
    return "Couldn't decide"


def creator(review):
    author = review.get("author") or ""
    requester = review.get("requested_by")
    if not requester or requester == author:
        return author, None
    labels = [str(label) for label in decode_json(review.get("labels"), []) or []]
    agents = [
        label[len("agent/") :]
        for label in labels
        if label.startswith("agent/") and label != "agent/dependabot"
    ]
    return requester, (
        AGENTS.get(agents[0], agents[0].replace("-", " ").title()) if agents else author
    )


def action_failure(kind, status, error, reviewing=False):
    if status == "expired":
        why = "Plus One was offline when it was due, so nothing happened."
    elif HEAD_MOVED.search(error or ""):
        then = "It's reviewing them now." if reviewing else "Review it again first."
        why = f"The PR has new commits since the review, so nothing was posted. {then}"
    elif kind in UNSURE:
        # Past the head check the request may have reached GitHub before the error, so a blind retry could post twice.
        return (
            UNSURE[kind],
            f"{(error or 'It stopped partway.').strip().rstrip('.')}. Check GitHub before trying again.",
        )
    else:
        why = (error or "It stopped partway.").strip()
    return NOT_DONE.get(kind, "Didn't finish"), why


def quiet_notes(notes):
    return [n for n in notes or [] if not n.startswith("dropped unverified")]


def factcheck_note(text):
    text = re.sub(r"\s*\((?:facts?\s*)?[\d,\s-]+\)", "", text or "", flags=re.I)
    text = re.sub(
        r"\bthe supporting facts\b|\bsupporting facts\b|\bthe facts\b", "the evidence", text
    )
    text = text.replace("the reviewer's suggested fix", "the suggested fix")
    return re.sub(r"\s+[\u2014\u2013]\s+", ": ", text).strip()


# A settings log row from before saves recorded what they replaced.
UNKNOWN = object()


def hour_name(hour):
    """An hour of the day as a clock reads it: 7 AM, 12 PM, midnight."""
    hour = int(hour)
    if hour in (0, 24):
        return "midnight"
    return f"{12 if hour % 12 == 0 else hour % 12} {'AM' if hour < 12 else 'PM'}"


MODEL_ROLES = settings.MODEL_ROLES


def clock_time(at):
    """A time of day as the settings page shows it: 11:30 AM."""
    return f"{12 if at.hour % 12 == 0 else at.hour % 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def clock_hour(hour):
    """An hour as the schedule menus show it: 7:00 AM, and Midnight for the end of the day."""
    hour = int(hour)
    if hour == 24:
        return "Midnight"
    return f"{12 if hour % 12 == 0 else hour % 12}:00 {'AM' if hour < 12 else 'PM'}"


def days_name(days):
    days = sorted(days)
    named = {(0, 1, 2, 3, 4): "Weekdays", (0, 1, 2, 3, 4, 5, 6): "Every day", (5, 6): "Weekends"}
    return named.get(tuple(days)) or ", ".join(settings.DAYS[d] for d in days) or "No days"


def _setting_value(key, value):
    if isinstance(value, bool):
        return "On" if value else "Off"
    if key == "days":
        return days_name(value)
    if key == "start_hour":
        return hour_name(value)
    if key == "end_hour":
        # Stored as the last hour checks run in; the page names the hour they stop.
        return hour_name(int(value) + 1)
    if key == "jitter_minutes":
        return f"{value} min"
    if key in ("max_spend_per_day_usd", "budget_usd"):
        return f"${float(value):,.2f}".replace(".00", "")
    if key == "timeout_s":
        return f"{int(value) // 60} min"
    if isinstance(value, int) and value >= 1000:
        return f"{value:,}"
    return str(value)


def setting_change(name, before, after):
    """One logged settings change as the page words it: a value that moved, or entries added and removed."""
    scope, _, rest = name.rpartition(":")
    table, _, key = rest.rpartition(".")
    label = settings.label(table, key)
    if scope:
        label = f"{label} in {scope}"
    if isinstance(after, (list, dict)):
        was = list(before) if isinstance(before, (list, dict)) else []
        now = list(after)
        return {
            "label": label,
            "added": [v for v in now if v not in was],
            "removed": [v for v in was if v not in now],
        }
    return {
        "label": label,
        "was": None if before is UNKNOWN else _setting_value(key, before),
        "now": _setting_value(key, after),
    }
