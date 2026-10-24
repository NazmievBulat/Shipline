"""The pool's programs take every name from the prefix, the label and the machine: none of them
names a runner, a unit or a label of its own."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[2] / "runner"
sys.path.insert(0, str(_RUNNER))
import ordinary_slots  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("planner_state", _RUNNER / "planner-state.py")
assert _SPEC and _SPEC.loader
planner_state = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(planner_state)


def _installed(tmp_path: Path) -> Path:
    """A copy of the pool's scripts with the pool settings an install writes."""
    folder = tmp_path / "installed"
    folder.mkdir()
    for path in _RUNNER.glob("*.sh"):
        shutil.copy(path, folder)
    (folder / "pool-settings.sh").write_text(
        "repository='example-owner/example-app'\nprefix='example-app'\nlabel='example-pool'\n"
        "machine='desk'\nroles='main'\nother_machines=''\n"
    )
    return folder


def test_the_lender_lends_this_machine_s_main_runners_by_the_pool_s_labels(
    tmp_path: Path,
) -> None:
    folder = _installed(tmp_path)
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    (bin_folder / "python3").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    (bin_folder / "python3").chmod(0o755)

    done = subprocess.run(
        ["sh", str(folder / "keep-lending.sh")],
        env={"PATH": f"{bin_folder}:{os.environ['PATH']}", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        str(folder / "lend_runners.py"),
        "example-owner/example-app",
        "example-pool",
        "example-pool-first",
        "example-pool-main",
        "1",
        "20",
        "desk-main",
    ]


def _ledger(root: Path, large: str) -> None:
    month = datetime.now(UTC).strftime("%Y-%m")
    run = {
        "target": "github",
        "run_id": "7",
        "attempt": 1,
        "small": "ubuntu-latest",
        "large": large,
        "cost": None,
        "reserved": 90,
    }
    (root / "ledger").mkdir(parents=True)
    (root / "ledger" / f"{month}.json").write_text(
        json.dumps({"month": month, "runs": {"7:1": run}})
    )


def test_the_slot_budget_takes_the_pool_s_label_for_runs_sent_to_it(tmp_path: Path) -> None:
    _ledger(tmp_path, "example-pool")

    assert ordinary_slots._used(tmp_path, datetime.now(UTC), "example-pool") == 0
    with pytest.raises(ValueError, match="invalid budget"):
        ordinary_slots._used(tmp_path, datetime.now(UTC), "other-pool")


def test_the_planner_counts_the_ordinary_runners_by_their_container_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, ...]] = []

    def read(*command: str) -> str:
        asked.append(command)
        if command[0] == "systemctl":
            return (
                "example-app-runner@1.service loaded active running\n"
                "example-app-runner@2.service loaded active running\n"
                "other-app-runner@3.service loaded active running\n"
            )
        if command[:2] == ("docker", "ps"):
            return "example-app-runner-1\nexample-app-runner-2\nother-app-runner-3\n"
        if command[:2] == ("docker", "top"):
            busy = command[2] == "example-app-runner-2"
            return "PID COMMAND\n1 Runner.Listener\n" + ("2 Runner.Worker\n" if busy else "")
        raise AssertionError(command)

    monkeypatch.setattr(planner_state, "_read", read)
    monkeypatch.setattr(planner_state, "others", lambda machines, label: (0, 0))

    state = planner_state.snapshot(2, [], "example-app-home", "example-app-runner")

    assert (state["free"], state["busy"]) == (1, 1)
    assert "example-app-runner@*.service" in asked[0]
