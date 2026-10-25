"""Breaking tests for pull request 65: the conflict reviewer's nudge, reviewer start lines and
the combined journal export. Each replays a daily flow with neutral names."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from workflow.test_checks_finished import _STARTED, _waiting  # pyright: ignore
from workflow.test_conflict_review import _conflict, _resolve  # pyright: ignore
from workflow.test_independent_review import _idle  # pyright: ignore
from workflow.test_merge_driver import _folder, _publish  # pyright: ignore
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow, session_arguments

handed = handed_fixture
github = github_fixture

_ROOT = Path(__file__).resolve().parents[2]
_COMPARE = _ROOT / "tools/compare-shadow"
_NOW = 1_790_010_000


def _nudges(workflow: Workflow) -> list[str]:
    return workflow.calls("tmux review-7 send-keys -t =review-7: -l GitHub reports")


def _wake_lines(workflow: Workflow) -> list[str]:
    log = Path(workflow.env["HOME"]) / ".local/state/workflow/wake.log"
    return log.read_text().splitlines() if log.exists() else []


def _hand_back_conflict(handed: GitHub) -> tuple[Workflow, str, subprocess.CompletedProcess[str]]:
    """A handed-off pull request meets a non-mechanical conflict; the tick starts the
    conflict reviewer for its passed head."""
    workflow = handed.workflow
    head, main = _conflict(workflow)
    handed.head, handed.base = head, main
    handed.pulls["7"].update(headRefOid=head, mergeable="CONFLICTING", mergeStateStatus="DIRTY")
    handed.main()
    handed.ci(main, head, tested_base=workflow.git("merge-base", main, head))
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    note = _folder(workflow) / "merge-wanted-7"
    data = json.loads(note.read_text())
    data["head"] = head
    note.write_text(json.dumps(data))
    _publish(handed, head=head)
    started = workflow.run_script("tick")
    assert started.returncode == 0, started.stdout + started.stderr
    assert "Review conflict resolution for pull request 7" in " ".join(
        session_arguments(workflow.state, "review-7")
    )
    return workflow, head, started


def _answering(workflow: Workflow) -> None:
    _idle(workflow, "review-7")
    workflow.screen(
        "● Checking the resolution.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "review-7"
    )


def test_a_hand_back_conflict_reviewer_start_is_logged_without_its_link(handed: GitHub) -> None:
    # The merge driver starts the conflict reviewer, not the tick's own start line.
    _, _, started = _hand_back_conflict(handed)
    lines = started.stdout.splitlines()
    assert lines.count("review-7 started") == 1, started.stdout
    assert not re.search(r"https?://", started.stdout + started.stderr)


def test_conflict_reviewer_is_not_nudged_over_several_ticks_while_github_lags(
    handed: GitHub,
) -> None:
    # Daily flow: the reviewer resolves at once, GitHub keeps CONFLICTING on the passed head
    # for several ticks. Not one nudge, and no wake-log line that would ring the phone.
    workflow, head, _ = _hand_back_conflict(handed)
    _resolve(workflow)
    _answering(workflow)
    for _ in range(3):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    assert _nudges(workflow) == []
    assert not any("merge conflict on" in line for line in _wake_lines(workflow))
    # The tick did see the conflict and noted it, owner-only.
    seen = _folder(workflow) / f"review-7.conflict-seen-{head}"
    assert seen.exists()
    assert stat.S_IMODE(seen.stat().st_mode) & 0o077 == 0


def test_conflict_reviewer_unresolved_yet_is_not_nudged_either(handed: GitHub) -> None:
    # The reviewer is still working on the merge (nothing committed) when the next tick runs.
    workflow, _, _ = _hand_back_conflict(handed)
    _answering(workflow)
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    assert _nudges(workflow) == []


def test_a_restarted_conflict_reviewer_on_the_same_head_is_not_nudged(handed: GitHub) -> None:
    # The conflict reviewer stops (memory, a crash) and a fresh one is started on the same
    # head while the worktree still holds the merge.
    workflow, _, _ = _hand_back_conflict(handed)
    _resolve(workflow)
    assert workflow.run("stop", "review-7").returncode == 0
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Review conflict resolution for pull request 7" in " ".join(
        session_arguments(workflow.state, "review-7")
    )
    _answering(workflow)
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    assert _nudges(workflow) == []


def test_an_older_reviewer_still_gets_one_nudge_for_a_new_conflict(workflow: Workflow) -> None:
    # A reviewer waiting on checks before main moved under it must still hear of the conflict.
    head = _waiting(workflow)
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY\n")
    for at in (_STARTED + 100, _STARTED + 280, _STARTED + 460):
        done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(at)})
        assert done.returncode == 0, done.stdout + done.stderr
    nudges = _nudges(workflow)
    assert len(nudges) == 1
    assert head in nudges[0]


def test_an_empty_or_garbled_seen_note_falls_back_to_the_nudge(workflow: Workflow) -> None:
    # A note cut short by a crash or a full disk: the tick must not die and must not go silent.
    head = _waiting(workflow)
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY\n")
    seen = workflow.repo / f".local/workflow/review-7.conflict-seen-{head}"
    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.write_text("")
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_STARTED + 100)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(_nudges(workflow)) == 1


def test_a_conflict_that_cleared_and_came_back_on_the_same_head_reaches_a_later_reviewer(
    workflow: Workflow,
) -> None:
    # Low: GitHub reports a conflict on H, then H merges cleanly again (main reverted), a fresh
    # reviewer starts, and only then does H conflict again. The note from the first sighting
    # still dates the conflict before this reviewer's start.
    head = _waiting(workflow)
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY\n")
    first = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_STARTED + 100)})
    assert first.returncode == 0, first.stdout + first.stderr
    (workflow.state / "mergeability-7").write_text("MERGEABLE CLEAN\n")
    later = _STARTED + 300
    os.utime(workflow.notes(7) / "review-7.started", (later, later))
    (workflow.notes(7) / "review-7.checks-wait").touch()
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY\n")
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_STARTED + 500)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(_nudges(workflow)) == 1, f"no nudge on {head}"


def test_a_shadow_tick_writes_no_seen_note(workflow: Workflow) -> None:
    head = _waiting(workflow)
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY\n")
    done = workflow.run_script("tick", "--shadow", env={"WORKFLOW_NOW": str(_STARTED + 100)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert not list((workflow.repo / ".local/workflow").glob(f"review-7.conflict-seen-{head}"))


# The journal export.


def _documented_export(text: str) -> str:
    """The shell block of the documented export, from its `{` to its `>tick.journal`."""
    block = re.search(r"^( *)\{\n(?:.*\n)*?\1\} \| .*>tick\.journal$", text, re.MULTILINE)
    assert block, "no combined export found"
    return "\n".join(line.strip() for line in block[0].splitlines())


_START = f"{_NOW + 75}.000001 example-host systemd[900]: Starting workflow-tick.service - Tick...\n"
_FINISH = (
    f"{_NOW + 140}.000001 example-host systemd[900]: Finished workflow-tick.service - Tick.\n"
)
_KEPT = f"{_NOW + 90}.123456 example-host tick[4242]: review-7 squeezed\n"
# A short-lived child's line stored without unit fields.
_LOST = f"{_NOW + 91}.654321 example-host tick[4250]: review-8 stopped\n"


def _fake_journalctl(folder: Path) -> Path:
    tools = folder / "bin"
    tools.mkdir()
    script = tools / "journalctl"
    (folder / "by-unit").write_text(_START + _KEPT + _FINISH)
    (folder / "by-identifier").write_text(_KEPT + _LOST)
    script.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in\n'
        f'  *" -t tick "*) cat "{folder}/by-identifier" ;;\n'
        f'  *" -u workflow-tick.service "*) cat "{folder}/by-unit" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    script.chmod(0o700)
    return tools


@pytest.mark.parametrize("source", ["readme", "tool help", "tool docstring"])
def test_the_documented_export_keeps_a_stop_line_without_unit_fields(
    tmp_path: Path, source: str
) -> None:
    if source == "readme":
        text = (_ROOT / "scripts/workflow/README.md").read_text()
    elif source == "tool help":
        text = subprocess.run(
            ["python3", str(_COMPARE), "--help"], capture_output=True, text=True, check=True
        ).stdout
    else:
        text = _COMPARE.read_text()
    command = _documented_export(text).replace("<start>", "-1h")
    tools = _fake_journalctl(tmp_path)
    exported = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert exported.returncode == 0, exported.stderr
    journal = (tmp_path / "tick.journal").read_text()
    # Each line once, in time order, the systemd anchors kept.
    assert journal == _START + _KEPT + _LOST + _FINISH

    (tmp_path / "shadow").write_text(
        f"{_NOW} shadow tick\n{_NOW} review-7 squeezed\n{_NOW} review-8 stopped\n"
        f"{_NOW + 180} shadow tick\n"
    )
    (tmp_path / "wake").write_text("")
    compared = subprocess.run(
        ["python3", str(_COMPARE), "--shadow", str(tmp_path / "shadow"), "--wake",
         str(tmp_path / "wake"), "--journal", str(tmp_path / "tick.journal")],
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compared.returncode == 0, compared.stdout + compared.stderr
    assert "2 decisions compared" in compared.stdout
    assert "none differs" in compared.stdout


def test_the_unit_export_alone_still_loses_the_stop(tmp_path: Path) -> None:
    # Control: the old export shows the stop as shadow-only.
    (tmp_path / "journal").write_text(_START + _KEPT + _FINISH)
    (tmp_path / "shadow").write_text(
        f"{_NOW} shadow tick\n{_NOW} review-8 stopped\n{_NOW + 180} shadow tick\n"
    )
    (tmp_path / "wake").write_text("")
    compared = subprocess.run(
        ["python3", str(_COMPARE), "--shadow", str(tmp_path / "shadow"), "--wake",
         str(tmp_path / "wake"), "--journal", str(tmp_path / "journal")],
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compared.returncode != 0
    assert "review-8 stopped" in compared.stdout


def test_a_real_conflict_reviewer_start_matches_the_shadow_s(tmp_path: Path) -> None:
    # Both sides now log a hand-back reviewer's start; the comparison must pair them.
    journal = (
        _START
        + f"{_NOW + 120}.000001 example-host tick[4242]: review-7 started\n"
        + _FINISH
    )
    (tmp_path / "journal").write_text(journal)
    (tmp_path / "shadow").write_text(
        f"{_NOW} shadow tick\n{_NOW} review-7 started\n{_NOW + 180} shadow tick\n"
    )
    (tmp_path / "wake").write_text("")
    compared = subprocess.run(
        ["python3", str(_COMPARE), "--shadow", str(tmp_path / "shadow"), "--wake",
         str(tmp_path / "wake"), "--journal", str(tmp_path / "journal")],
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compared.returncode == 0, compared.stdout
    assert "none differs" in compared.stdout


def test_the_identifier_export_matches_the_tick_unit_shipline_writes() -> None:
    # Low: systemd tags a service's output with the basename of its ExecStart program unless
    # SyslogIdentifier= says otherwise. A unit written by `install-timer` runs the `shipline`
    # launcher, so `-t tick` would find none of its lines and the export would silently fall
    # back to the unit filter alone. Not the case of an installation running its own `tick`.
    unit = (_ROOT / "scripts/workflow/install-timer").read_text()
    assert 'ExecStart="${escaped_launcher' in unit and "SyslogIdentifier" not in unit
    # The launcher it runs is the checkout's `bin/shipline`.
    assert "launcher=$home/bin/shipline" in (_ROOT / "scripts/workflow/launcher-link").read_text()
    assert "-t shipline " in (_ROOT / "scripts/workflow/README.md").read_text()
