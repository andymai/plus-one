import re


def _compile(pattern):
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/").rstrip("/")
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    if anchored or "/" in pattern:
        return re.compile(rf"^{body}(?:/.*)?$")
    return re.compile(rf"^(?:.*/)?{body}(?:/.*)?$")


def _owner(token):
    return token.lstrip("@").split("/", 1)[-1]


def parse(text):
    rules = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        rules.append((_compile(pattern), {_owner(o) for o in owners}))
    return rules


def owners_for(rules, path):
    for regex, owners in reversed(rules):
        if regex.match(path):
            return owners
    return set()
