"""Read sharing for train planning. Merge evidence never uses this cache.

The tick owns a temporary directory. Only a reconciliation activates API memoization;
ordinary gate reads, including every check before a squash, always reach GitHub.
"""

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import check_repair
import workflow_config


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except OSError, ValueError:
        return {}


def save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def tick() -> Path | None:
    path = os.environ.get("WORKFLOW_TRAIN_TICK")
    if path:
        folder = Path(path)
        started = load(folder / "context.json").get("started", 0)
        # The directory is unique to this tick and removed by its EXIT trap. A slow
        # GitHub read must not expire it halfway through the tick and restart the scan.
        if isinstance(started, (int, float)) and 0 < started <= time.time():
            return folder
    return None


def planning_pulls(folder: Path) -> dict[str, Any]:
    # The bulk snapshot also feeds ticket selection, which requires number/body.
    # Gate views have fewer fields; keep their updates in a planning-only copy.
    path = folder / "planning-pulls.json"
    return load(path if path.exists() else folder / "pulls.json")


def pull(number: str, read: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """The tick's snapshot for planning; missing/just-merged pulls get one real read."""
    folder = tick()
    if folder is None:
        return read(number)
    pulls = planning_pulls(folder)
    if number not in pulls:
        pulls[number] = read(number)
        save(folder / "planning-pulls.json", pulls)
    return pulls[number]


def refresh_pull(number: str, read: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """A durable handoff may have advanced after the tick took its snapshot."""
    fresh = read(number)
    if folder := tick():
        pulls = planning_pulls(folder)
        pulls[number] = {**pulls.get(number, {}), **fresh}
        save(folder / "planning-pulls.json", pulls)
    return fresh


class Reads:
    def __init__(self, folder: Path, base: str) -> None:
        self.tick = tick()
        self.base = base
        self.path = folder / "train-read-cache.json"
        self.previous = load(self.path)
        self.responses = load(self.tick / "api.json") if self.tick else {}
        self.owners: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.steady: set[str] = set()
        self.polling: set[str] = set()
        for member in planning_pulls(self.tick).values() if self.tick else []:
            self.register(member)

    def register(self, member: dict[str, Any]) -> None:
        head = member["headRefOid"]
        # Labels and availability still get recomputed locally. They do not change
        # the runs or jobs on this head. Rollup IDs/status/conclusion change on reruns.
        self.tokens[head] = digest(
            {
                "main": self.base,
                "head": head,
                "checks": member.get("statusCheckRollup"),
                "prefix_only": workflow_config.current().checks.prefix_only,
            }
        )
        rollup = member.get("statusCheckRollup") or []
        if rollup and all(
            (check.get("status") == "COMPLETED" and check.get("conclusion") is not None)
            or (
                check.get("__typename") == "StatusContext"
                and check.get("state") in ("SUCCESS", "FAILURE", "ERROR")
            )
            for check in rollup
        ):
            self.steady.add(head)
        else:
            self.steady.discard(head)

    def _poll_retry(self, head: str) -> None:
        self.polling.add(head)
        # Jobs and annotations can have been cached before the retry was requested.
        # Remove them too, so they cannot outlive the run metadata catching up.
        self.previous = {
            path: entry for path, entry in self.previous.items()
            if entry.get("token") != self.tokens.get(head)
        }

    def api(self, path: str, read: Callable[[], Any], *, fresh: bool = False) -> Any:
        """GitHub's answer for `path`, shared within the tick; `fresh` asks GitHub again, and
        the later reads of the tick share that answer."""
        config = workflow_config.current()
        files = "|".join(re.escape(name) for name in (config.workflows.ci, config.workflows.images))
        head_match = re.search(
            rf"/workflows/(?:{files})/runs\?head_sha=([^&]+)&event=pull_request",
            path,
        )
        owner = head_match[1] if head_match else self.owners.get(path)
        token = self.tokens.get(owner or "")
        key = digest([path, token])

        def retrying(value: Any) -> bool:
            # A terminal rollup can precede its successful retry's run metadata. Do not
            # freeze the failed first attempt under that otherwise unchanged rollup.
            return bool(head_match) and any(
                check_repair.retry_requested(self.path.parent, found) for found in value
            )

        candidate = self.responses.get(key, self.previous.get(path, {}).get("value", []))
        if owner and retrying(candidate):
            self._poll_retry(owner)

        if fresh:
            value = read()
            self.responses[key] = value
        elif key in self.responses:
            value = self.responses[key]
        elif (
            owner in self.steady
            and owner not in self.polling
            and self.previous.get(path, {}).get("token") == token
            and not retrying(self.previous[path]["value"])
        ):
            value = self.previous[path]["value"]
            self.responses[key] = value
        else:
            value = read()
            self.responses[key] = value
        if owner and token:
            if retrying(value):
                self._poll_retry(owner)
            if head_match:
                for run in value:
                    self.owners[f"{config.api}/actions/runs/{run['id']}/jobs?per_page=100"] = owner
                    # A job carried into a re-run of the failed jobs has its notes in the
                    # attempt where it ran (merge_gate.job_notes).
                    attempt = run.get("run_attempt")
                    for earlier in range(1, attempt if isinstance(attempt, int) else 1):
                        self.owners[
                            f"{config.api}/actions/runs/{run['id']}/attempts/{earlier}/jobs"
                            "?per_page=100"
                        ] = owner
            elif "/jobs?" in path:
                for job in value:
                    self.owners[
                        f"{config.api}/check-runs/{job['id']}/annotations?per_page=100"
                    ] = owner
            # Pending members are always polled once per tick. A lost check must age
            # into recognition, and a scratch rerun must never use the member's cache.
            if (
                owner in self.steady
                and owner not in self.polling
                and not retrying(value)
                and (value or f"/{config.workflows.images}/runs?" in path)
                and (
                    not isinstance(value, list)
                    or all(item.get("status", "completed") == "completed" for item in value)
                )
            ):
                self.previous[path] = {"token": token, "value": value}
        return value

    def invalidate(self) -> None:
        """A cancel or retry may have changed GitHub even when its response was lost."""
        self.responses.clear()
        if self.tick:
            save(self.tick / "api.json", {})

    def finish(self) -> None:
        tokens = set(self.tokens.values())
        save(
            self.path,
            {key: value for key, value in self.previous.items() if value.get("token") in tokens},
        )
        if self.tick:
            save(self.tick / "api.json", self.responses)
