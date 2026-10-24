"""Share the home slot limit between the spare supervisor and availability publisher.

Install after a green Blacksmith trial. Only the host reads the repository switches, as the
runner programs' own GitHub identity or its gh login: home gives up a slot only while Blacksmith
takes automatic overflow.
Budget reads include live reservations crossing a month boundary.

A machine that gave all its PR slots to the other machines (install.sh) takes one back while none
of them has an online ordinary runner, so PR jobs still run when the others are off.
"""

import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from github_budget import RUNNER, Limited, call

BLACKSMITH_LIMIT = 3000
BLACKSMITH_RESERVE = 140
_RESERVE = {"blacksmith": BLACKSMITH_RESERVE, "github": 90}
_LEGACY_RESERVE = {"blacksmith": 115, "github": 70}
_LABELS = {
    "blacksmith": [("blacksmith-2vcpu-ubuntu-2404", "blacksmith-4vcpu-ubuntu-2404")],
    "github": [("ubuntu-latest", "ubuntu-latest"), ("ubuntu-latest", "example-app-home")],
}


def _blacksmith_on(root: Path, repository: str, now: datetime) -> bool:
    cache = root / "home/blacksmith-switch.json"
    last = False
    try:
        saved = json.loads(cache.read_text())
        if type(saved["on"]) is bool:
            last = saved["on"]
            if 0 <= now.timestamp() - saved["updated_at"] < 60:
                return last
    except (OSError, ValueError, KeyError, TypeError):
        pass
    on = False
    try:
        # One request for both switches stays within the publisher's five-second read.
        answer = call(
            "ordinary-slots",
            "api",
            f"repos/{repository}/actions/variables?per_page=30",
            timeout=3,
            identity=RUNNER,
        )
        answer.check_returncode()
        values = {row["name"]: row["value"] for row in json.loads(answer.stdout)["variables"]}
        # With CI_OVERFLOW off no automatic run goes to Blacksmith, so home keeps every slot.
        on = values.get("CI_BLACKSMITH") == "on" and values.get("CI_OVERFLOW") == "on"
    except Limited:
        # The budget pause sent no request: keep the last answer, and ask again after it.
        return last
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
    ):
        pass
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Both host services may refresh at once. Separate temporary files and atomic
    # replacement keep readers from seeing partial cache contents.
    with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, delete=False) as output:
        temporary = Path(output.name)
        json.dump({"updated_at": now.timestamp(), "on": on}, output)
    temporary.replace(cache)
    return on


def _used(root: Path, now: datetime) -> int:
    month = now.strftime("%Y-%m")
    folder = root / "ledger"
    paths = sorted(folder.glob("*.json"))
    # No ledger at all means the planner never reserved a provider run. Keep all home
    # slots. A month whose ledger the planner has not written yet has spent nothing.
    if not paths:
        raise ValueError("budget unavailable")
    costs: dict[str, int] = {}
    for path in paths:
        datetime.strptime(path.stem, "%Y-%m")
        saved = json.loads(path.read_text())
        if saved["month"] != path.stem or path.stem > month:
            raise ValueError("invalid ledger month")
        for key, row in saved["runs"].items():
            target = row["target"]
            cost, reserved = row["cost"], row["reserved"]
            # Match the planner's ledger contract: a destination whose ledger the
            # planner rejects cannot justify reducing home capacity.
            if (
                target not in _RESERVE
                or not re.fullmatch(r"[1-9][0-9]*", row["run_id"])
                or type(row["attempt"]) is not int
                or row["attempt"] < 1
                or key != f"{row['run_id']}:{row['attempt']}"
                or (row["small"], row["large"]) not in _LABELS[target]
                or reserved not in (_RESERVE[target], _LEGACY_RESERVE[target])
                or (cost is not None and (type(cost) is not int or cost < 0))
            ):
                raise ValueError("invalid budget")
            if target == "blacksmith" and (path.stem == month or cost is None):
                # Current-month settlement supersedes a carried old reservation.
                costs[key] = reserved if cost is None else cost
    return sum(costs.values())


# The spare supervisor reads the listing every five seconds (planner-state.py); an older one is
# no proof of anything.
_LISTING_SECONDS = 20


def others_online(machines: list[str], label: str, listing: Path) -> bool | None:
    """Whether another machine has an online ordinary runner (`<machine>-<n>`, names.sh) with the
    workflow label, in the spare supervisor's runner listing (keep-spare.sh). Main's runners are
    not counted, even while one is lent to PR jobs: it is busy exactly when main has work. None
    when the listing is missing, stale or unreadable."""
    try:
        if not 0 <= time.time() - listing.stat().st_mtime < _LISTING_SECONDS:
            return None
        rows = listing.read_text().splitlines()
    except OSError:
        return None
    names = re.compile(f"({'|'.join(map(re.escape, machines))})-[1-9][0-9]*")
    for row in rows:
        fields = row.split("\t")
        if (
            len(fields) == 5
            and names.fullmatch(fields[1])
            and fields[2] == "online"
            and label in fields[4].split(",")
        ):
            return True
    return False


def slots(
    maximum: int,
    reduced: int,
    repository: str,
    *,
    machines: list[str] | None = None,
    label: str | None = None,
    listing: Path | None = None,
    root: Path | None = None,
    now: datetime | None = None,
) -> int:
    root = root or Path.home() / ".local/state/ci-overflow"
    now = now or datetime.now(UTC)
    # No slot of its own: one while the listing shows no other machine's ordinary runner online.
    # Without a listing to show it, none.
    if maximum == 0:
        if machines and label and listing and others_online(machines, label, listing) is False:
            return 1
        return 0
    try:
        if (
            reduced < maximum
            and _blacksmith_on(root, repository, now)
            and _used(root, now) + BLACKSMITH_RESERVE < BLACKSMITH_LIMIT
        ):
            return reduced
    except Exception as error:
        print(f"Home slot budget unavailable: {error}", file=sys.stderr)
    return maximum


if __name__ == "__main__":
    # ordinary_slots.py <slots> <slots while Blacksmith overflows> <repository>
    #   [<other machines> <workflow label> <runner listing>]
    given = sys.argv[4:7]
    print(
        slots(
            int(sys.argv[1]),
            int(sys.argv[2]),
            sys.argv[3],
            machines=given[0].split() if given else None,
            label=given[1] if len(given) > 1 else None,
            listing=Path(given[2]) if len(given) > 2 else None,
        )
    )
