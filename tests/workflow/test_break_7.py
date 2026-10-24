"""Breaking tests for `shipline install` (pull request 7), as an owner runs it once at the
machine on a repository of an organisation: labels the account may or may not create, a GitHub
that refuses or stalls, labels that are there under another case, and two runs at once. What
must never be left is a half install: the timer running while a label the workflow reads is
missing."""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline

_REPOSITORY = "example-org/example-app"
_PRIORITIES = ["priority: blocker", "priority: high", "priority: medium", "priority: low"]
_LABELS = ["workflow", "audited", *_PRIORITIES]

# Wraps the harness's `gh`. Behaviour a test asks for, by files in the state folder:
#   refuse-create-<label>   `gh label create <label>` is refused, as an organisation does for an
#                           account whose token may not write (HTTP 403)
#   hang-list-after-<n>     every `gh label list` after the n-th one stalls, as a dead connection
#   slow-create             `gh label create` waits a moment between GitHub's check and its write
# GitHub's own rule holds always: a label is unique by its name in any case.
_GH = """#!/bin/bash
state="@STATE@"
if [ "$1 $2" = "label list" ]; then
  n=$(( $(cat "$state/list-count" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$state/list-count"
  for f in "$state"/hang-list-after-*; do
    [ -e "$f" ] || continue
    [ "$n" -le "${f##*-}" ] || { echo "gh label list hung" >> "$state/calls.log"; exec /usr/bin/sleep 120; }
  done
fi
if [ "$1 $2" = "label create" ]; then
  echo "gh $*" >> "$state/calls.log"
  if [ -e "$state/refuse-create-$3" ]; then
    echo "HTTP 403: Resource not accessible by integration" >&2
    exit 1
  fi
  [ ! -e "$state/slow-create" ] || /usr/bin/sleep 0.5
  if grep -qixF -- "$3" "$state/labels" 2>/dev/null; then
    echo "label with name \\"$3\\" already exists; use \\`--force\\` to update its color and description" >&2
    exit 1
  fi
  echo "$3" >> "$state/labels"
  exit 0
fi
exec "$state/gh-real" "$@"
"""


def _node(workflow: Workflow) -> None:
    workflow.fake("node", '#!/bin/bash\n[ "$1" = --version ] && echo v22.12.0\n')


@pytest.fixture
def workflow(workflow: Workflow) -> Workflow:
    install_shipline(workflow)
    _node(workflow)
    shutil.move(workflow.bin / "gh", workflow.state / "gh-real")
    workflow.fake("gh", _GH)
    (workflow.state / "labels").write_text("")
    return workflow


def _timer_started(workflow: Workflow) -> bool:
    return "systemctl --user enable --now workflow-tick.timer" in workflow.calls("systemctl")


def _units(workflow: Workflow) -> Path:
    return Path(workflow.env["HOME"]) / ".config" / "systemd" / "user"


def _labels(workflow: Workflow) -> list[str]:
    return (workflow.state / "labels").read_text().splitlines()


def test_an_organisation_that_refuses_a_priority_label_gets_no_timer(workflow: Workflow) -> None:
    """The account may push (permissions.push) but the organisation refuses its writes."""
    (workflow.state / "refuse-create-priority: high").touch()

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode != 0
    assert "HTTP 403" in done.stderr
    assert not _timer_started(workflow)
    assert not (_units(workflow) / "workflow-tick.timer").exists()


def test_an_organisation_that_refuses_the_workflow_label_gets_no_timer(workflow: Workflow) -> None:
    """The priority labels are there; `workflow` is created by install-timer itself."""
    (workflow.state / "labels").write_text("\n".join(_PRIORITIES) + "\n")
    (workflow.state / "refuse-create-workflow").touch()

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode != 0
    assert not _timer_started(workflow)
    assert not (_units(workflow) / "workflow-tick.timer").exists()


def test_a_second_run_once_the_account_may_write_finishes_the_install(workflow: Workflow) -> None:
    (workflow.state / "refuse-create-audited").touch()
    first = workflow.shipline("install", str(workflow.repo))
    assert first.returncode != 0
    assert not _timer_started(workflow)

    (workflow.state / "refuse-create-audited").unlink()
    again = workflow.shipline("install", str(workflow.repo))

    assert again.returncode == 0, again.stderr
    assert sorted(_labels(workflow)) == sorted(_LABELS)
    assert _timer_started(workflow)


def test_no_right_to_push_refuses_before_anything_with_every_label_named(
    workflow: Workflow,
) -> None:
    (workflow.state / "no-push").touch()

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == "nothing was installed"
    named = [line for line in done.stderr.splitlines() if line.startswith("  gh label create")]
    assert len(named) == len(_LABELS)
    assert workflow.calls("gh label create") == []
    assert not (Path(workflow.env["HOME"]) / ".claude").exists()
    assert not _timer_started(workflow)


def test_a_github_that_stalls_inside_the_timer_step_does_not_hang_the_install(
    workflow: Workflow,
) -> None:
    """The first label list answers; the second, install-timer's own, stalls. Every call of the
    install is meant to give up after 30 seconds."""
    (workflow.state / "hang-list-after-1").touch()
    started = time.monotonic()
    try:
        done = subprocess.run(
            ["bash", str(workflow.home / "bin" / "shipline"), "install", str(workflow.repo)],
            cwd=workflow.repo,
            env=workflow.env,
            capture_output=True,
            text=True,
            timeout=75,
            check=False,
        )
        returncode = done.returncode
    except subprocess.TimeoutExpired:
        returncode = None
    took = time.monotonic() - started

    assert returncode is not None and took < 45, f"install still running after {took:.0f}s"
    assert not _timer_started(workflow)


def test_labels_that_are_there_in_another_case_do_not_stop_the_install(
    workflow: Workflow,
) -> None:
    """An organisation's repository that already has `Workflow` and `Priority: High`. GitHub
    holds one label per name in any case, so the workflow's labels are there."""
    (workflow.state / "labels").write_text("Workflow\nPriority: High\nbug\n")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 0, done.stderr
    assert _timer_started(workflow)


def test_two_installs_at_once_end_installed(workflow: Workflow) -> None:
    (workflow.state / "slow-create").touch()
    results: list[subprocess.CompletedProcess[str]] = []

    def run() -> None:
        results.append(workflow.shipline("install", str(workflow.repo)))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert any(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert sorted(_labels(workflow)) == sorted(_LABELS)
    assert _timer_started(workflow)


def test_a_label_create_that_stalls_is_given_up_and_leaves_no_timer(
    workflow: Workflow,
) -> None:
    workflow.fake(
        "gh",
        _GH.replace(
            'if [ "$1 $2" = "label create" ]; then\n',
            'if [ "$1 $2" = "label create" ]; then\n  exec /usr/bin/sleep 120\n',
            1,
        ),
    )
    started = time.monotonic()
    done = subprocess.run(
        ["bash", str(workflow.home / "bin" / "shipline"), "install", str(workflow.repo)],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=75,
        check=False,
    )

    assert done.returncode != 0
    assert time.monotonic() - started < 45
    assert not _timer_started(workflow)
