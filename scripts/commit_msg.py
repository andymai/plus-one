"""commit-msg hook: the first line is a Conventional Commit, with the types .github/workflows/pr-title.yml accepts."""

import re
import sys

TYPES = (
    "feat",
    "fix",
    "perf",
    "refactor",
    "chore",
    "test",
    "docs",
    "style",
    "ci",
    "build",
    "revert",
)
HEADER = re.compile(rf"({'|'.join(TYPES)})(\([\w./-]+\))?!?: (?![A-Z]).+")
# Messages git writes itself.
GENERATED = ("Merge ", "fixup! ", "squash! ", "amend! ", 'Revert "')


def main(path):
    with open(path, encoding="utf-8") as f:
        first = f.readline().strip()
    if first.startswith(GENERATED) or HEADER.fullmatch(first):
        return 0
    print(
        f"Commit message {first!r} isn't a Conventional Commit: type(scope): subject, where type is one of "
        f"{', '.join(TYPES)} and the subject starts lowercase.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
