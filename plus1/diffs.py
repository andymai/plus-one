import difflib
import re

TESTISH = re.compile(
    r"(^|/)(spec|specs|test|tests|__tests__|fixtures?)/|_spec\.rb$|_test\.rb$|\.(test|spec)\.[jt]sx?$"
)
HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def right_lines(diff):
    files = {}
    path = None
    new = old_left = new_left = 0
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            if line.startswith("\\"):
                continue
            if line.startswith("+"):
                files[path].add(new)
                new += 1
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            else:
                files[path].add(new)
                new += 1
                new_left -= 1
                old_left -= 1
            continue
        if line.startswith("+++ "):
            target = line[4:]
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else target
                files.setdefault(path, set())
            continue
        m = HUNK.match(line)
        if m and path:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new = int(m.group(2))
            new_left = int(m.group(3)) if m.group(3) is not None else 1
    return files


def around(diff, path, line, radius=6):
    """The hunk lines around path:line with that line marked >>, or "" when the diff doesn't show it."""
    hunks, current = [], None
    old_left = new_left = new = 0
    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            if raw.startswith("\\"):
                continue
            kind = raw[:1]
            hunks[-1][1].append((None if kind == "-" else new, raw))
            if kind == "-":
                old_left -= 1
            else:
                new += 1
                new_left -= 1
                if kind != "+":
                    old_left -= 1
            continue
        if raw.startswith("+++ "):
            target = raw[4:]
            current = target[2:] if target.startswith("b/") else target
            continue
        m = HUNK.match(raw)
        if m:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new = int(m.group(2))
            new_left = int(m.group(3)) if m.group(3) is not None else 1
            hunks.append((current, []))
    for name, rows in hunks:
        hit = next((i for i, (n, _) in enumerate(rows) if name == path and n == line), None)
        if hit is not None:
            lo = max(0, hit - radius)
            return "\n".join(
                f"{'>>' if i == hit else '  '} {n or '':>5} {text}"
                for i, (n, text) in enumerate(rows[lo : hit + radius + 1], lo)
            )
    return ""


def anchor(files, path, line):
    lines = files.get(path)
    if not lines:
        return None
    if line in lines:
        return path, line
    return path, min(lines, key=lambda candidate: abs(candidate - (line or 0)))


def file_stats(diff):
    stats, current, in_hunk = [], None, False
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            current = [line.split(" b/", 1)[-1], 0, 0]
            stats.append(current)
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif current and in_hunk:
            if line.startswith("+"):
                current[1] += 1
            elif line.startswith("-"):
                current[2] += 1
    return [{"path": path, "add": add, "del": rem} for path, add, rem in stats]


def patches(diff):
    files, path, in_hunk = {}, None, False
    # str.splitlines would also split on form feeds and Unicode line separators inside a line.
    for line in (diff or "").split("\n"):
        if line.startswith("diff --git "):
            path, in_hunk = line.split(" b/", 1)[-1], False
            files[path] = {"headers": [], "lines": []}
        elif path is None or not line:
            continue
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            # Inside a hunk, "+++ x" is an added line "++ x", not a file header.
            if line.startswith(("+", "-")):
                files[path]["lines"].append(line)
        elif not line.startswith(("--- ", "+++ ")):
            files[path]["headers"].append(line)
    out = {}
    for path, f in files.items():
        # Mode, rename, and binary changes have no +/- lines, so their headers count. A text file's index line changes
        # whenever the base moves, but for a binary file its blob hashes are the only sign the content changed.
        binary = any(h.startswith(("Binary files ", "GIT binary patch")) for h in f["headers"])
        out[path] = [h for h in f["headers"] if binary or not h.startswith("index ")] + f["lines"]
    return out


def interdiff(old, new):
    # Hunk headers and context lines move whenever the base does, so only the patch lines and change headers count.
    before, after = patches(old), patches(new)
    changed, size = [], 0
    for path in sorted(before.keys() | after.keys()):
        a, b = before.get(path, []), after.get(path, [])
        if a == b:
            continue
        ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
        size += sum((i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
        changed.append(path)
    return changed, size
