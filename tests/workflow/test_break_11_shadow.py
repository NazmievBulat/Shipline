"""Breaking tests for the shadow run (#11), written by a session that did not build it.

Each replays a day of an installation that runs its own tick every 3 minutes beside 5 to 15
sessions, with a shadow tick just before each real one, reading the same state, and checks that
the shadow changes nothing there and that its comparison tells the owner the truth.
"""

import hashlib
import os
import subprocess
from pathlib import Path

from workflow.test_lost_check import _lose, _run, _waiting_reviewer
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow

github = github_fixture

_ROOT = Path(__file__).resolve().parents[2]
_COMPARE = _ROOT / "tools" / "compare-shadow"
_NOW = 1_790_010_000  # 2026-09-21 17:00:00 UTC
_BOX = "\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n"


def _digest(root: Path, leave_out: tuple[Path, ...] = ()) -> dict[str, str]:
    found: dict[str, str] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if any(path == out or out in path.parents for out in leave_out):
            continue
        if path.is_file():
            found[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        elif path.is_dir():
            found[str(path.relative_to(root))] = "folder"
    return found


def _shadow_log(workflow: Workflow) -> Path:
    return workflow.repo / ".local" / "shipline" / "shadow.log"


# A GitHub check lost: a reviewer waits for its checks before it joined the line, and GitHub
# still lists one as running though its run ended an hour ago.


def test_a_shadow_tick_writes_no_lost_check_note_into_the_live_state(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)
    state = workflow.repo / ".local" / "workflow"
    before = _digest(state)

    done = workflow.run_script("tick", "--shadow")

    assert done.returncode == 0, done.stdout + done.stderr
    # The shadow saw the decision the real tick would take ...
    assert "review-7 lost check on " in _shadow_log(workflow).read_text()
    # ... and left the installation's notes as they were: the wake's pending lost checks are
    # the live installation's, which tells its orchestrator of them.
    assert _digest(state) == before, sorted(set(_digest(state)) - set(before))


# The shadow tick runs just before the real one, in the same start of its service: the real tick
# reads the clock as many seconds later as the shadow tick took. A wait that crosses its
# threshold in between is decided by the real tick alone, and the mark it keeps hides it from
# every later shadow tick.


def _waiting_worker(workflow: Workflow, since: int) -> None:
    workflow.add_worktree(13)
    workflow.running("ticket-13")
    workflow.screen(
        "● Waiting for the full Python run in the background." + _BOX, "ticket-13"
    )
    note = workflow.notes(13) / "ticket-13.job-wait"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("\n")
    os.utime(note, (since, since))


def test_a_status_check_due_between_the_shadow_and_the_real_tick_is_no_difference(
    workflow: Workflow,
) -> None:
    # The worker has waited 49 min 50 s when the shadow tick reads the clock; the real tick
    # reads it 20 s later, once the shadow tick is done.
    _waiting_worker(workflow, _NOW - 50 * 60 + 10)
    for at in (_NOW, _NOW + 180):
        shadow = workflow.run_script(
            "tick", "--shadow", env={"WORKFLOW_NOW": str(at), "TZ": "UTC"}
        )
        real = workflow.run_script(
            "tick", env={"WORKFLOW_NOW": str(at + 20), "TZ": "UTC"}
        )
        assert shadow.returncode == real.returncode == 0, shadow.stderr + real.stderr
    assert (
        "ticket-13 idle 50 min waiting for its own job or turn; status check sent"
        in (workflow.wake_log.read_text())
    )

    compared = subprocess.run(
        [
            "python3",
            str(_COMPARE),
            "--shadow",
            str(_shadow_log(workflow)),
            "--wake",
            str(workflow.wake_log),
        ],
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    # Both took the same decision, 20 s apart: the comparison the owner reads lists no
    # difference for that worker. (The wake's check-in differs too, by main's report, which
    # the real tick refreshed and `compare-shadow --help` names as explained.)
    assert compared.returncode in (0, 1), compared.stderr
    assert "ticket-13" not in compared.stdout, compared.stdout


def test_a_night_check_in_due_between_the_shadow_and_the_real_tick_is_no_difference(
    workflow: Workflow,
) -> None:
    night = _NOW + 6 * 3600  # 23:00 UTC: the wake looks every 20 minutes
    clock = {"TZ": "UTC"}
    workflow.add_worktree(11)
    workflow.running("ticket-11")
    workflow.screen("● Working on the change." + _BOX, "ticket-11")
    workflow.orchestrator()
    # A real tick a quarter of an hour before, as the installation's last ones: main's report
    # is as it left it.
    settled = workflow.run_script(
        "tick", env={**clock, "WORKFLOW_NOW": str(night - 900)}
    )
    assert settled.returncode == 0, settled.stderr
    # The wake last looked 19 min 25 s before the shadow tick reads the clock.
    looked = (
        Path(workflow.env["HOME"]) / ".local" / "state" / "workflow" / "wake.looked"
    )
    looked.write_text(f"{night - 1165}\n")
    for at in (night, night + 180):
        shadow = workflow.run_script(
            "tick", "--shadow", env={**clock, "WORKFLOW_NOW": str(at)}
        )
        real = workflow.run_script("tick", env={**clock, "WORKFLOW_NOW": str(at + 20)})
        assert shadow.returncode == real.returncode == 0, shadow.stderr + real.stderr

    compared = subprocess.run(
        [
            "python3",
            str(_COMPARE),
            "--shadow",
            str(_shadow_log(workflow)),
            "--wake",
            str(workflow.wake_log),
        ],
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    # Both wakes looked once, 20 s apart, and came to the same check-in.
    assert compared.returncode == 0, compared.stdout + compared.stderr


# A slow GitHub: the drop-in the README gives runs the shadow tick before the installation's own
# tick in the same start of its service, so the real tick starts only once the shadow tick has
# ended. Its `-` lets a failed shadow tick pass, not a slow one: every read of a shadow tick may
# take its 30 s, and nothing bounds the whole.


def test_the_readme_s_drop_in_bounds_how_long_the_shadow_tick_holds_the_real_one() -> (
    None
):
    readme = (_ROOT / "scripts" / "workflow" / "README.md").read_text()
    drop_in = [line for line in readme.splitlines() if "ExecStartPre=" in line]

    assert drop_in, "the README names no drop-in"
    assert all("timeout" in line for line in drop_in), drop_in


# The backstop of a shadow run: a GraphQL mutation read from a file. No caller of the tick sends
# one; this is the backstop alone.


def test_the_shadow_backstop_refuses_a_graphql_mutation_given_as_a_body_file() -> None:
    done = subprocess.run(
        [
            "python3",
            "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); import github_budget; "
                "print(github_budget.writes(('api', 'graphql', '--input', 'body.json')))"
            ),
            str(_ROOT / "scripts" / "workflow"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.stdout.strip() == "True", done.stdout + done.stderr


# A dozen workers whose screens have not changed for an hour: the shadow names them stuck in its
# own check-in, and the installation's stuck notes, wake files and marks stay as they were.


def test_stuck_workers_among_a_dozen_sessions_are_noted_by_the_shadow_alone(
    workflow: Workflow,
) -> None:
    for number in range(20, 32):
        workflow.add_worktree(number)
        workflow.running(f"ticket-{number}")
        workflow.screen(
            f"● Working on change {number}.\n  Running tests" + _BOX, f"ticket-{number}"
        )
    workflow.orchestrator()
    state = workflow.repo / ".local" / "workflow"
    state.mkdir(parents=True, exist_ok=True)
    # The real tick read every screen an hour ago; none has changed since.
    first = workflow.run_script(
        "tick", env={"WORKFLOW_NOW": str(_NOW - 3600), "TZ": "UTC"}
    )
    assert first.returncode == 0, first.stderr
    home_state = Path(workflow.env["HOME"]) / ".local" / "state" / "workflow"
    before = (
        _digest(state),
        _digest(home_state, leave_out=(home_state / "github-budget",)),
    )

    done = workflow.run_script(
        "tick", "--shadow", env={"WORKFLOW_NOW": str(_NOW), "TZ": "UTC"}
    )

    assert done.returncode == 0, done.stderr
    after = (
        _digest(state),
        _digest(home_state, leave_out=(home_state / "github-budget",)),
    )
    assert after == before
    log = _shadow_log(workflow).read_text()
    assert "stuck? ticket-20 (60 min, running: " in log, log
    assert "  stuck? ticket-20 (60 min" in log
