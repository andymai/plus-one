import json
import re
import subprocess
from datetime import datetime

QUERY = """
query($owner:String!,$name:String!,$n:Int!){
 repository(owner:$owner,name:$name){
  pullRequest(number:$n){
   number title body url state isDraft mergeable reviewDecision isInMergeQueue headRefOid headRefName baseRefName createdAt updatedAt additions deletions
   author{login __typename}
   assignees(first:5){nodes{login}}
   labels(first:30){nodes{name}}
   files(first:100){nodes{path} pageInfo{hasNextPage}}
   reviews(last:50){nodes{author{login __typename} state body submittedAt commit{oid}}}
   reviewThreads(first:100){pageInfo{hasNextPage} nodes{id isResolved resolvedBy{login} isOutdated path line originalLine comments(first:20){nodes{author{login __typename} body url}}}}
   comments(last:30){nodes{author{login __typename} body}}
   timelineItems(last:1,itemTypes:[READY_FOR_REVIEW_EVENT]){nodes{... on ReadyForReviewEvent{createdAt}}}
   requests:timelineItems(last:50,itemTypes:[REVIEW_REQUESTED_EVENT]){nodes{... on ReviewRequestedEvent{actor{login __typename} requestedReviewer{... on User{login}}}}}
   commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){nodes{__typename
     ... on CheckRun{name status conclusion startedAt completedAt isRequired(pullRequestNumber:$n)}
     ... on StatusContext{context state createdAt isRequired(pullRequestNumber:$n)}}}}}}}
  }
 }
}
"""

PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,10}-[1-9][0-9]{0,5})(?![A-Za-z0-9])")
LINEAR_URL = re.compile(r"linear\.app/[\w-]+/issue/([A-Za-z]{2,10}-[1-9][0-9]{0,5})(?![0-9])")
BRANCH_TICKET = re.compile(r"(?:^|/)([A-Za-z]{2,10}-[1-9][0-9]{0,5})(?=[-/]|$)")
REQUESTED_BY = re.compile(r"^\W*requested by\W*@([A-Za-z0-9-]+)", re.I | re.M)
NOT_TICKETS = {
    "AES",
    "CVE",
    "ECMA",
    "ES",
    "GA",
    "GPT",
    "HTTP",
    "IE",
    "ISO",
    "PEP",
    "PR",
    "RFC",
    "RSA",
    "SHA",
    "SSL",
    "TLS",
    "UTF",
}


class GhError(RuntimeError):
    pass


def run(args, input_text=None, timeout=90):
    proc = subprocess.run(
        ["gh", *args], input=input_text, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def candidates(cfg):
    out = run(
        [
            "search",
            "prs",
            "--repo",
            cfg.repo,
            "--review-requested",
            "@me",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number",
            "-q",
            ".[].number",
        ]
    )
    return [int(line) for line in out.split()]


def followups(cfg):
    # The overall decision, so this also finds PRs someone else blocked; filters.followup keeps the ones you did.
    out = run(
        [
            "search",
            "prs",
            "--repo",
            cfg.repo,
            "--reviewed-by",
            "@me",
            "--review",
            "changes_requested",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number",
            "-q",
            ".[].number",
        ]
    )
    return [int(line) for line in out.split()]


def commit_emails(cfg):
    """The author addresses on the owner's latest commits to the repo, as GitHub matched them to their login."""
    out = run(
        [
            "api",
            f"repos/{cfg.repo}/commits?author={cfg.me}&per_page=100",
            "--jq",
            ".[].commit.author.email",
        ]
    )
    return sorted({line.strip().lower() for line in out.split() if "@" in line})


def team_members(name):
    org, slug = name.split("/", 1)
    out = run(
        [
            "api",
            "--paginate",
            f"orgs/{org}/teams/{slug}/members?per_page=100",
            "--jq",
            ".[].login",
        ],
        timeout=30,
    )
    return sorted({login.lower() for login in out.split()})


def my_teams():
    """The logged-in user's teams as (org/slug, member count)."""
    out = run(
        [
            "api",
            "--paginate",
            "user/teams?per_page=100",
            "--jq",
            ".[] | [.organization.login, .slug, .members_count] | @tsv",
        ],
        timeout=30,
    )
    rows = (line.split("\t") for line in out.splitlines() if line.strip())
    return [(f"{org}/{slug}".lower(), int(count or 0)) for org, slug, count in rows]


def org_teams(org):
    out = run(
        ["api", "--paginate", f"orgs/{org}/teams?per_page=100", "--jq", ".[].slug"], timeout=30
    )
    return [f"{org}/{slug}".lower() for slug in out.split()]


def open_numbers(cfg):
    out = run(
        [
            "pr",
            "list",
            "--repo",
            cfg.repo,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number",
            "-q",
            ".[].number",
        ]
    )
    return {int(line) for line in out.split()}


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _check(node):
    if node["__typename"] == "CheckRun":
        if node["status"] != "COMPLETED":
            state = "PENDING"
        else:
            state = "PASS" if node["conclusion"] in PASS_CONCLUSIONS else "FAIL"
        return {
            "name": node["name"],
            "state": state,
            "required": bool(node["isRequired"]),
            "at": _ts(node["completedAt"] or node["startedAt"]),
        }
    state = {"SUCCESS": "PASS", "PENDING": "PENDING", "EXPECTED": "PENDING"}.get(
        node["state"], "FAIL"
    )
    return {
        "name": node["context"],
        "state": state,
        "required": bool(node["isRequired"]),
        "at": _ts(node["createdAt"]),
    }


def _latest_per_name(checks):
    latest = {}
    for check in checks:
        prev = latest.get(check["name"])
        if prev is None or (check["at"] and (prev["at"] is None or check["at"] > prev["at"])):
            latest[check["name"]] = check
    return list(latest.values())


def ticket(title, branch="", body=""):
    found = [
        *TICKET.findall(title or ""),
        *LINEAR_URL.findall(body or ""),
        *BRANCH_TICKET.findall(branch or ""),
        *TICKET.findall(body or ""),
    ]
    for key in (k.upper() for k in found):
        if key.split("-")[0] not in NOT_TICKETS:
            return key
    return None


def requester(raw):
    if (raw.get("author") or {}).get("__typename") != "Bot":
        return None
    m = REQUESTED_BY.search(raw.get("body") or "")
    if m:
        return m.group(1)
    assignees = [n["login"] for n in (raw.get("assignees") or {}).get("nodes", [])]
    return assignees[0] if assignees else None


def _comment(node):
    author = node["author"] or {}
    return {
        "author": author.get("login", ""),
        "bot": author.get("__typename") == "Bot",
        "body": node["body"] or "",
    }


def _thread(node):
    comments = [_comment(c) for c in node["comments"]["nodes"]]
    first = comments[0] if comments else {"author": "", "body": ""}
    nodes = node["comments"]["nodes"]
    return {
        "id": node.get("id"),
        "resolved": node["isResolved"],
        "resolved_by": (node.get("resolvedBy") or {}).get("login"),
        "outdated": node["isOutdated"],
        "path": node["path"],
        "line": node["line"],
        "original_line": node.get("originalLine"),
        "author": first["author"],
        "body": first["body"],
        "url": nodes[0].get("url") if nodes else None,
        "comments": comments,
    }


def normalize(raw):
    owner_commit = raw["commits"]["nodes"][0]["commit"]
    rollup = owner_commit["statusCheckRollup"]
    contexts = rollup["contexts"]["nodes"] if rollup else []
    ready = raw["timelineItems"]["nodes"]
    return {
        "number": raw["number"],
        "repo": "/".join(raw["url"].split("/")[3:5]),
        "title": raw["title"],
        "body": raw["body"] or "",
        "url": raw["url"],
        "state": raw["state"],
        "draft": raw["isDraft"],
        "mergeable": raw["mergeable"],
        "decision": raw["reviewDecision"],
        "merge_queue": raw["isInMergeQueue"],
        "head": raw["headRefOid"],
        "base": raw["baseRefName"],
        "author": (raw["author"] or {}).get("login", ""),
        "requested_by": requester(raw),
        "requests": [
            {
                "by": (e.get("actor") or {}).get("login", ""),
                "bot": (e.get("actor") or {}).get("__typename") == "Bot",
                "to": e["requestedReviewer"]["login"],
            }
            for e in (raw.get("requests") or {}).get("nodes", [])
            if (e.get("requestedReviewer") or {}).get("login")
        ],
        "labels": [n["name"].lower() for n in raw["labels"]["nodes"]],
        "files": [n["path"] for n in raw["files"]["nodes"]],
        "files_truncated": raw["files"]["pageInfo"]["hasNextPage"],
        "additions": raw["additions"],
        "deletions": raw["deletions"],
        "ticket": ticket(raw["title"], raw.get("headRefName"), raw["body"]),
        "ready_at": _ts(ready[0]["createdAt"]) if ready else _ts(raw["createdAt"]),
        "reviews": [
            {
                **_comment(r),
                "state": r["state"],
                "oid": (r["commit"] or {}).get("oid"),
                "at": _ts(r.get("submittedAt")),
            }
            for r in raw["reviews"]["nodes"]
        ],
        "threads": [_thread(t) for t in raw["reviewThreads"]["nodes"]],
        "threads_truncated": bool((raw["reviewThreads"].get("pageInfo") or {}).get("hasNextPage")),
        "conversation": [_comment(c) for c in raw["comments"]["nodes"]],
        "checks": _latest_per_name([_check(c) for c in contexts]),
    }


def fetch_pr(cfg, number):
    owner, name = cfg.repo.split("/")
    out = run(
        [
            "api",
            "graphql",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"n={number}",
            "-f",
            f"query={QUERY}",
        ]
    )
    return normalize(json.loads(out)["data"]["repository"]["pullRequest"])


def pr_states(cfg, numbers, batch=50):
    owner, name = cfg.repo.split("/")
    states = {}
    numbers = [int(n) for n in numbers]
    for start in range(0, len(numbers), batch):
        fields = " ".join(
            f"p{n}:pullRequest(number:{n}){{number state reviewDecision isInMergeQueue}}"
            for n in numbers[start : start + batch]
        )
        query = f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{fields}}}}}"
        out = run(
            ["api", "graphql", "-F", f"owner={owner}", "-F", f"name={name}", "-f", f"query={query}"]
        )
        for node in json.loads(out)["data"]["repository"].values():
            if node:
                states[node["number"]] = {
                    "state": node["state"],
                    "decision": node["reviewDecision"],
                    "merge_queue": node["isInMergeQueue"],
                }
    return states


def resolve_thread(thread_id):
    query = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
    out = run(["api", "graphql", "-F", f"id={thread_id}", "-f", f"query={query}"])
    return json.loads(out)["data"]["resolveReviewThread"]["thread"]["isResolved"]


def head_sha(cfg, number):
    return run(["api", f"repos/{cfg.repo}/pulls/{number}", "--jq", ".head.sha"]).strip()


def post_review(cfg, number, sha, event, body="", comments=()):
    payload = {"commit_id": sha, "event": event}
    if body:
        payload["body"] = body
    if comments:
        payload["comments"] = [
            {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]}
            for c in comments
        ]
    out = run(
        ["api", "-X", "POST", f"repos/{cfg.repo}/pulls/{number}/reviews", "--input", "-"],
        input_text=json.dumps(payload),
    )
    return json.loads(out)
