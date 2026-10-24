"""Share the home slot limit between the spare supervisor and availability publisher.

Install after a green Blacksmith trial. Only the host reads the repository switches, as the
runner programs' own GitHub identity or its gh login: home gives up a slot only while Blacksmith
takes automatic overflow.
Budget reads include live reservations crossing a month boundary.
"""

import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from github_budget import RUNNER, Limited, call

BLACKSMITH_LIMIT = 3000
BLACKSMITH_RESERVE = 140
_RESERVE = {"blacksmith": BLACKSMITH_RESERVE, "github": 90}
_LEGACY_RESERVE = {"blacksmith": 115, "github": 70}


def _labels(label: str) -> dict[str, list[tuple[str, str]]]:
    """The (small, large) runner labels the planner may record a run of each destination with;
    the pool's own label is one of them."""
    return {
        "blacksmith": [("blacksmith-2vcpu-ubuntu-2404", "blacksmith-4vcpu-ubuntu-2404")],
        "github": [("ubuntu-latest", "ubuntu-latest"), ("ubuntu-latest", label)],
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


def _used(root: Path, now: datetime, label: str) -> int:
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
                or (row["small"], row["large"]) not in _labels(label)[target]
                or reserved not in (_RESERVE[target], _LEGACY_RESERVE[target])
                or (cost is not None and (type(cost) is not int or cost < 0))
            ):
                raise ValueError("invalid budget")
            if target == "blacksmith" and (path.stem == month or cost is None):
                # Current-month settlement supersedes a carried old reservation.
                costs[key] = reserved if cost is None else cost
    return sum(costs.values())


def slots(
    maximum: int,
    reduced: int,
    repository: str,
    label: str,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> int:
    root = root or Path.home() / ".local/state/ci-overflow"
    now = now or datetime.now(UTC)
    try:
        if (
            reduced < maximum
            and _blacksmith_on(root, repository, now)
            and _used(root, now, label) + BLACKSMITH_RESERVE < BLACKSMITH_LIMIT
        ):
            return reduced
    except Exception as error:
        print(f"Home slot budget unavailable: {error}", file=sys.stderr)
    return maximum


if __name__ == "__main__":
    if len(sys.argv) < 5:
        # A supervisor of an earlier install, still running while install.sh builds the images,
        # names no label: without it no run can be counted, so every slot is kept.
        print("Home slot budget unavailable: no pool label given", file=sys.stderr)
        print(int(sys.argv[1]))
    else:
        print(slots(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]))
