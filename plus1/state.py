import fcntl
import json
import os
import time
from datetime import datetime


class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg.state_dir
        for sub in ("reports", "work"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.audit_file = self.dir / "audit.jsonl"
        self.data = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    @property
    def paused(self):
        return (self.dir / "PAUSE").exists()

    def pause(self):
        (self.dir / "PAUSE").touch()

    def resume(self):
        (self.dir / "PAUSE").unlink(missing_ok=True)

    def lock(self, wait_s=0):
        handle = open(self.dir / "tick.lock", "w")  # noqa: SIM115 (held open, it holds the lock)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return None
                time.sleep(5)

    def decided_sha(self, number):
        return self.data.get(str(number), {}).get("sha")

    def entry(self, number):
        return self.data.get(str(number))

    def _save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.state_file)

    def record(self, number, sha, verdict, reason, **extra):
        self.data[str(number)] = {"sha": sha, "verdict": verdict, "reason": reason, **extra}
        self._save()

    def forget(self, number):
        self.data.pop(str(number), None)
        self._save()

    def audit(self, action, **fields):
        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "action": action,
            **fields,
        }
        with open(self.audit_file, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def approvals_today(self):
        """Automatic approvals posted today. The cap limits the timer, so approvals the owner clicks don't count."""
        if not self.audit_file.exists():
            return 0
        today = datetime.now().astimezone().date().isoformat()
        count = 0
        for line in self.audit_file.read_text().splitlines():
            rec = json.loads(line)
            # Lines from before approvals named their actor all counted.
            if (
                rec["action"] == "approve_posted"
                and rec["at"].startswith(today)
                and rec.get("actor", "timer") == "timer"
            ):
                count += 1
        return count

    def report(self, line):
        day = datetime.now().astimezone().date().isoformat()
        stamp = datetime.now().astimezone().strftime("%H:%M")
        with open(self.dir / "reports" / f"{day}.md", "a") as f:
            f.write(f"- {stamp} {line}\n")
