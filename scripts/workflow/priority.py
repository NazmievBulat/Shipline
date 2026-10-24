"""The priority of a ticket, read from its labels (AGENTS.md, the merge gate section).

The most urgent `priority:` label a pull request carries is its priority, and one without any
is medium. `priority.jq` says the same for the shell scripts; a test holds the two together.
"""

from collections.abc import Iterable
from typing import Any

# Most urgent first: the merge train serves, and the tick starts, in this order.
PRIORITIES = ("blocker", "high", "medium", "low")
UNLABELLED = "medium"
LABEL_PREFIX = "priority: "


def of(labels: Iterable[dict[str, Any]] | None) -> str:
    names = {label.get("name") for label in labels or []}
    return next(
        (priority for priority in PRIORITIES if LABEL_PREFIX + priority in names), UNLABELLED
    )


def rank(priority: str) -> int:
    return PRIORITIES.index(priority if priority in PRIORITIES else UNLABELLED)
