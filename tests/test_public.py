import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Each name is split so that this file passes its own check.
PRIVATE = (
    "baby" + "list",
    "eng-" + "dme",
    "eng-db-" + "reviewers",
    "agent" + "core",
    "fight" + "-me",
    "REG" + "AI-",
    "C040" + "MAB0X1T",
)


class PublicTreeTest(unittest.TestCase):
    def test_no_tracked_file_names_a_private_org_team_or_channel(self):
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        if listed.returncode:
            self.skipTest("not a git checkout")
        found = []
        for name in filter(None, listed.stdout.split("\0")):
            path = ROOT / name
            if not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", "ignore").lower()
            found += [f"{name}: {word}" for word in PRIVATE if word.lower() in text]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
