"""Publish ordinary runners' local service/process state for the isolated planner.

This installed host script exposes counts only. The job cannot run host commands or
read Docker's socket. A failed refresh leaves the old snapshot to expire after 20s.
The PR runners of the other machines are counted from the runner listing that this
machine's spare supervisor saves beside this script (keep-spare.sh).
"""

import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def _read(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=5).stdout


# The supervisor reads the listing every five seconds; an older one is not counted.
_LISTING_SECONDS = 20


def others(
    machines: list[str], label: str, listing: Path | None = None, now: float | None = None
) -> tuple[int, int]:
    """Free and busy PR runners of the other machines, from GitHub's list of registrations.

    Each machine's supervisor gives the workflow label only to as many of its runners as it
    has slots, so an idle online runner with the label is a free slot of that machine. A
    missing, stale or unreadable listing counts none, which keeps the planner's old view.
    """
    if not machines:
        return 0, 0
    listing = listing or Path(__file__).with_name("runners.tsv")
    now = time.time() if now is None else now
    try:
        if not 0 <= now - listing.stat().st_mtime < _LISTING_SECONDS:
            return 0, 0
        rows = listing.read_text().splitlines()
    except OSError:
        return 0, 0
    names = re.compile(f"({'|'.join(map(re.escape, machines))})-[1-9][0-9]*")
    free = busy = 0
    for row in rows:
        fields = row.split("\t")
        if len(fields) != 5 or not names.fullmatch(fields[1]):
            continue
        _, _, status, is_busy, labels = fields
        if is_busy == "true":
            busy += 1
        elif status == "online" and label in labels.split(","):
            free += 1
    return free, busy


def snapshot(
    ordinary_jobs: int, machines: list[str] | None = None, label: str = "example-app-home"
) -> dict[str, int | str]:
    """Free means a job slot the workflow label can still use, not merely an idle container.

    Every ordinary container registers with the standby label; keep-spare.sh labels at most
    ordinary_jobs of them. The idle spare beyond those slots cannot take a queued job.
    """
    services = _read(
        "systemctl",
        "--user",
        "list-units",
        "--state=active",
        "--plain",
        "--no-legend",
        "example-app-runner@*.service",
    )
    containers = set(_read("docker", "ps", "--format", "{{.Names}}").splitlines())
    idle = busy = 0
    for line in services.splitlines():
        match = re.fullmatch(r"example-app-runner@([1-9][0-9]*)\.service", line.split()[0])
        if not match:
            continue
        name = f"example-app-runner-{match[1]}"
        if name not in containers:
            continue
        processes = {
            line.split()[-1]
            for line in _read("docker", "top", name, "-eo", "pid,comm").splitlines()
            if line.split()
        }
        if "Runner.Listener" in processes:
            if "Runner.Worker" in processes:
                busy += 1
            else:
                idle += 1
    free = max(0, min(idle, ordinary_jobs - busy))
    elsewhere_free, elsewhere_busy = others(machines or [], label)
    return {
        "updated_at": datetime.now(UTC).isoformat(),
        "free": free + elsewhere_free,
        "busy": busy + elsewhere_busy,
    }


def main() -> None:
    maximum, reduced, repository = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    machines = sys.argv[4].split() if len(sys.argv) > 4 else []
    label = sys.argv[5] if len(sys.argv) > 5 else "example-app-home"
    folder = Path.home() / ".local/state/ci-overflow/home"
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            slots = int(
                _read(
                    sys.executable,
                    str(Path(__file__).with_name("ordinary_slots.py")),
                    str(maximum),
                    str(reduced),
                    repository,
                )
            )
            state = snapshot(slots, machines, label)
            temporary = folder / "state.tmp"
            temporary.write_text(json.dumps(state))
            temporary.replace(folder / "state.json")
        except Exception as error:
            print(f"Home runner snapshot unavailable: {error}", file=sys.stderr, flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
