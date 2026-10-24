"""The merge gate tells the runners' lender which pull request is first in the merge train
(scripts/workflow/merge_gate.py, .github/runner/lend_runners.py)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_GATE = Path(__file__).resolve().parents[2] / "scripts/workflow"


def _save(folder: Path, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
            "from pathlib import Path; g.Train(Path(sys.argv[2])).save()",
            str(_GATE),
            str(folder),
        ],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "train,expected",
    [
        # A blocked pull request is passed over: the first is the one the gate serves next.
        (
            {"waiting": ["431", "433", "450"], "blocked": {"431": "aaa"}},
            {"number": "433", "batch": None},
        ),
        # The branch of the first member's prefix, whose runs decide its turn.
        (
            {
                "waiting": ["433", "450"],
                "prefixes": {
                    "base": "abc",
                    "merged": 0,
                    "members": [
                        {"number": "433", "scratch": {"branch": "workflow/merge-batch-3"}},
                        {"number": "450", "scratch": {"branch": "workflow/merge-batch-4"}},
                    ],
                },
            },
            {"number": "433", "batch": "workflow/merge-batch-3"},
        ),
        # A first member that merges by its own checks has no prefix to publish.
        (
            {
                "waiting": ["433", "450"],
                "prefixes": {
                    "base": "abc",
                    "merged": 0,
                    "members": [
                        {"number": "433", "scratch": None},
                        {"number": "450", "scratch": {"branch": "workflow/merge-batch-4"}},
                    ],
                },
            },
            {"number": "433", "batch": None},
        ),
        ({"waiting": []}, {"number": None, "batch": None}),
    ],
)
def test_every_save_of_the_train_notes_its_first_member_for_the_runners(
    tmp_path: Path, train: dict[str, object], expected: dict[str, object]
) -> None:
    folder = tmp_path / "workflow"
    folder.mkdir()
    (folder / "merge-train.json").write_text(json.dumps(train))

    done = _save(folder, tmp_path / "home")

    assert done.returncode == 0, done.stderr
    note = tmp_path / "home/.local/state/workflow/train-first.json"
    assert json.loads(note.read_text()) == expected


def test_a_note_that_cannot_be_written_does_not_stop_the_train(tmp_path: Path) -> None:
    folder = tmp_path / "workflow"
    folder.mkdir()
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["433"]}))
    home = tmp_path / "home"
    (home / ".local/state").mkdir(parents=True)
    (home / ".local/state/workflow").write_text("a file where the folder should be")

    done = _save(folder, home)

    assert done.returncode == 0, done.stderr
    assert "could not note the train's first member" in done.stdout
    assert json.loads((folder / "merge-train.json").read_text())["waiting"] == ["433"]


def test_save_publishes_batch_before_dispatch_without_a_lender_round(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from runner.test_ci_overflow import plan_with_lender

    home = tmp_path / "home"
    planner = home / ".local/state/ci-overflow/home/first.json"
    planner.parent.mkdir(parents=True)
    heartbeat = datetime.now(UTC)
    planner.write_text(
        json.dumps(
            {
                "number": "431",
                "batch": None,
                "free": 0,
                "updated_at": heartbeat.isoformat(),
            }
        )
    )
    folder = tmp_path / "workflow"
    folder.mkdir()
    (folder / "merge-train.json").write_text(
        json.dumps(
            {
                "waiting": ["433"],
                "prefixes": {
                    "base": "abc",
                    "merged": 0,
                    "members": [{"number": "433", "scratch": {"branch": "workflow/merge-batch-3"}}],
                },
            }
        )
    )
    done = _save(folder, home)
    assert done.returncode == 0, done.stderr
    decision = plan_with_lender(tmp_path, planner, branch="workflow/merge-batch-3", now=heartbeat)
    assert decision["large"] == "example-app-home-first"
    assert json.loads(planner.read_text())["updated_at"] == heartbeat.isoformat()
    assert planner.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "attempt,conclusion,expected",
    [(1, "cancelled", "pending"), (2, "cancelled", "red"), (2, "failure", "red")],
)
def test_only_the_recorded_cancelled_attempt_is_pending(
    tmp_path: Path, attempt: int, conclusion: str, expected: str
) -> None:
    import time

    note = tmp_path / ".local/state/ci-overflow/replanned.json"
    note.parent.mkdir(parents=True)
    note.write_text(
        json.dumps(
            {
                "20": {
                    "state": "rerun",
                    "attempt": 1,
                    "head": "head",
                    "updated_at": time.time(),
                }
            }
        )
    )
    run = {
        "id": 20,
        "head_sha": "head",
        "run_attempt": attempt,
        "conclusion": conclusion,
        "status": "completed",
    }
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,json;sys.path.insert(0,sys.argv[1]);import merge_gate as g;"
            "print(g.verdict(json.loads(sys.argv[2]),[])[0])",
            str(_GATE),
            json.dumps(run),
        ],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == expected
