"""`usage` prints the figures the status line shows, from its file or from Anthropic."""

import json
import os
import time

import pytest
from workflow_harness import Workflow

_FIVE_RESETS = "2026-10-14T01:10:00.318040+00:00"
_SEVEN_RESETS = "2026-10-16T07:00:00+00:00"
_ANSWER = {
    "five_hour": {"utilization": 76.0, "resets_at": _FIVE_RESETS},
    "seven_day": {"utilization": 41.0, "resets_at": _SEVEN_RESETS},
}
_LINES = "five_hour 76 1791940200\nseven_day 41 1792134000\n"


def _answering(workflow: Workflow, answer: str) -> None:
    """A stand-in for `curl` that answers as Anthropic does, and keeps what it was given."""
    (workflow.state / "answer").write_text(answer)
    workflow.fake(
        "curl",
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$@" > "@STATE@/curl-args"\n'
        'cat > "@STATE@/curl-input"\n'
        'cat "@STATE@/answer"\n',
    )


def _credentials(workflow: Workflow) -> None:
    path = workflow.root / "home" / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tokenOfTheOwner"}}))


def test_the_status_line_file_is_read_while_it_is_fresh(workflow: Workflow) -> None:
    workflow.usage(76, _FIVE_RESETS, seven=41, seven_resets=_SEVEN_RESETS)

    done = workflow.run_script("usage")

    assert done.returncode == 0, done.stderr
    assert done.stdout == _LINES
    assert workflow.calls("curl") == []


def test_an_old_file_is_replaced_by_the_answer_of_anthropic(workflow: Workflow) -> None:
    workflow.usage(10, "2026-10-13T20:00:00+00:00")
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    old = time.time() - 600
    os.utime(cache, (old, old))
    _credentials(workflow)
    _answering(workflow, json.dumps(_ANSWER))

    done = workflow.run_script("usage")

    assert done.returncode == 0, done.stderr
    assert done.stdout == _LINES
    # The token is given on standard input, never as an argument.
    assert "tokenOfTheOwner" not in (workflow.state / "curl-args").read_text()
    assert "Bearer tokenOfTheOwner" in (workflow.state / "curl-input").read_text()


def test_without_a_file_or_a_token_it_fails_and_prints_nothing(workflow: Workflow) -> None:
    done = workflow.run_script("usage")

    assert done.returncode == 1
    assert done.stdout == ""


def test_an_answer_that_is_not_the_figures_fails(workflow: Workflow) -> None:
    _credentials(workflow)
    _answering(workflow, '{"error": {"type": "authentication_error"}}')

    done = workflow.run_script("usage")

    assert done.returncode == 1
    assert done.stdout == ""


def test_a_window_without_its_reset_is_left_out(workflow: Workflow) -> None:
    _credentials(workflow)
    _answering(workflow, json.dumps({**_ANSWER, "seven_day": {"utilization": 3.0}}))

    done = workflow.run_script("usage")

    assert done.stdout == "five_hour 76 1791940200\n"


@pytest.mark.parametrize(("age", "fresh"), [(299, True), (301, False)])
def test_the_file_counts_as_fresh_for_5_minutes(workflow: Workflow, age: int, fresh: bool) -> None:
    workflow.usage(76, _FIVE_RESETS)
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    changed = int(time.time())
    os.utime(cache, (changed, changed))

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(changed + age)})

    assert (done.returncode == 0) == fresh
