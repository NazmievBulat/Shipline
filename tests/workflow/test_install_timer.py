"""`install-timer` makes systemd run the tick every 3 minutes and creates the two labels."""

import shutil
import subprocess
from pathlib import Path

import pytest
from workflow_harness import Workflow, make_workflow


def _units(workflow: Workflow) -> Path:
    return Path(workflow.env["HOME"]) / ".config" / "systemd" / "user"


def test_the_service_runs_the_tick_of_the_main_checkout(workflow: Workflow) -> None:
    done = workflow.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    service = (_units(workflow) / "workflow-tick.service").read_text().splitlines()
    assert "Type=oneshot" in service
    assert f"WorkingDirectory={workflow.repo}" in service
    assert f'ExecStart="{workflow.repo}/scripts/workflow/tick"' in service


def test_the_service_finds_the_same_programs_as_the_one_who_installed_it(
    workflow: Workflow,
) -> None:
    """systemd gives a service a short `PATH` without `gh`, `claude` or the repository's tools."""
    workflow.run_script("install-timer")

    service = (_units(workflow) / "workflow-tick.service").read_text().splitlines()
    assert f'Environment="PATH={workflow.env["PATH"]}"' in service


def test_the_service_looks_for_sessions_where_the_one_who_installed_it_keeps_them(
    workflow: Workflow,
) -> None:
    """Without it the tick would see no session and start a second worker for each draft."""
    workflow.run_script("install-timer")

    service = (_units(workflow) / "workflow-tick.service").read_text().splitlines()
    assert f'Environment="TMUX_TMPDIR={workflow.env["TMUX_TMPDIR"]}"' in service


def test_the_service_names_no_folder_of_sockets_when_the_default_one_is_used(
    workflow: Workflow,
) -> None:
    env = {key: value for key, value in workflow.env.items() if key != "TMUX_TMPDIR"}
    workflow.env.clear()
    workflow.env.update(env)

    workflow.run_script("install-timer")

    assert "TMUX_TMPDIR" not in (_units(workflow) / "workflow-tick.service").read_text()


def test_the_service_leaves_the_sessions_it_started_running(workflow: Workflow) -> None:
    """By default systemd ends every process a service leaves behind, the workers included."""
    workflow.run_script("install-timer")

    service = (_units(workflow) / "workflow-tick.service").read_text().splitlines()
    assert "KillMode=process" in service


def test_the_timer_fires_every_3_minutes(workflow: Workflow) -> None:
    workflow.run_script("install-timer")

    timer = (_units(workflow) / "workflow-tick.timer").read_text().splitlines()
    assert "OnCalendar=*:0/3" in timer
    assert "AccuracySec=10s" in timer
    assert "WantedBy=timers.target" in timer


def test_the_timer_is_loaded_and_switched_on(workflow: Workflow) -> None:
    workflow.run_script("install-timer")

    assert workflow.calls("systemctl") == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now workflow-tick.timer",
    ]


def test_the_units_go_where_the_owner_keeps_his_configuration(workflow: Workflow) -> None:
    elsewhere = workflow.root / "config"

    workflow.run_script("install-timer", env={"XDG_CONFIG_HOME": str(elsewhere)})

    assert (elsewhere / "systemd" / "user" / "workflow-tick.timer").is_file()
    assert not _units(workflow).exists()


def test_installing_from_a_worktree_still_points_at_the_main_checkout(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)

    done = workflow.run_script("install-timer", cwd=worktree)

    assert done.returncode == 0, done.stderr
    service = (_units(workflow) / "workflow-tick.service").read_text()
    assert f'ExecStart="{workflow.repo}/scripts/workflow/tick"\n' in service


def test_both_labels_are_created_when_they_are_missing(workflow: Workflow) -> None:
    (workflow.state / "labels").write_text("bug\n")

    done = workflow.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    assert (workflow.state / "labels").read_text().splitlines() == ["bug", "workflow", "audited"]


def test_a_label_that_exists_is_not_created_again(workflow: Workflow) -> None:
    (workflow.state / "labels").write_text("workflow\n")

    workflow.run_script("install-timer")

    created = [call.split()[3] for call in workflow.calls("gh label create")]
    assert created == ["audited"]


def test_a_label_with_a_longer_name_does_not_count_as_the_label(workflow: Workflow) -> None:
    (workflow.state / "labels").write_text("workflow-old\nnot-audited\n")

    workflow.run_script("install-timer")

    assert (workflow.state / "labels").read_text().splitlines()[2:] == ["workflow", "audited"]


def test_the_labels_are_created_with_a_colour_and_what_they_mean(workflow: Workflow) -> None:
    workflow.run_script("install-timer")

    assert workflow.calls("gh label create") == [
        "gh label create workflow --color 1D76DB --description "
        "Picked up by the workflow: gets a worker, a review and a merge",
        "gh label create audited --color 0E8A16 --description Its audit left nothing open",
    ]


def test_nothing_is_installed_when_the_labels_cannot_be_read(workflow: Workflow) -> None:
    (workflow.state / "fail-gh-labels").touch()

    done = workflow.run_script("install-timer")

    assert done.returncode != 0
    assert "cannot read the labels" in done.stderr
    assert workflow.calls("gh label create") == []
    assert workflow.calls("systemctl") == []
    assert not _units(workflow).exists()


def test_nothing_is_installed_when_a_label_cannot_be_created(workflow: Workflow) -> None:
    (workflow.state / "fail-gh-label-create").touch()

    done = workflow.run_script("install-timer")

    assert done.returncode != 0
    assert workflow.calls("systemctl") == []
    assert not _units(workflow).exists()


def test_a_percent_sign_in_the_path_reaches_the_tick_as_it_is(workflow: Workflow) -> None:
    """systemd reads `%` as the start of a placeholder; doubled, it stands for itself."""
    path = workflow.env["PATH"] + ":/opt/100%/bin"

    workflow.run_script("install-timer", env={"PATH": path})

    service = (_units(workflow) / "workflow-tick.service").read_text()
    assert ":/opt/100%%/bin" in service


_ANALYZE = shutil.which("systemd-analyze")
# Environment prerequisite: systemd-analyze must be installed to verify systemd units.
_needs_systemd = pytest.mark.skipif(_ANALYZE is None, reason="systemd-analyze is not installed")  # noqa: TID251


@_needs_systemd
def test_systemd_reads_the_timer_as_every_3_minutes(workflow: Workflow) -> None:
    workflow.run_script("install-timer")
    timer = (_units(workflow) / "workflow-tick.timer").read_text().splitlines()
    calendar = next(line for line in timer if line.startswith("OnCalendar=")).split("=", 1)[1]

    read = subprocess.run(
        [str(_ANALYZE), "calendar", calendar], capture_output=True, text=True, check=False
    )

    assert read.returncode == 0, read.stderr
    assert "Normalized form: *-*-* *:00/3:00" in read.stdout


@_needs_systemd
def test_systemd_finds_nothing_wrong_in_the_units(workflow: Workflow) -> None:
    workflow.run_script("install-timer")
    units = _units(workflow)

    read = subprocess.run(
        [
            str(_ANALYZE),
            "--user",
            "verify",
            str(units / "workflow-tick.service"),
            str(units / "workflow-tick.timer"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    # Faults of other units on the machine are printed too. CI keeps the test's folder on a
    # drive that runs no programs, which is not a fault of the unit.
    about_ours = [
        line
        for line in (read.stdout + read.stderr).splitlines()
        if "workflow-tick" in line and "is not executable" not in line
    ]
    assert about_ours == []


def test_a_checkout_in_a_folder_with_a_space_and_a_percent_sign_still_gets_its_tick(
    workflow: Workflow, tmp_path: Path
) -> None:
    """systemd splits a command at a space and reads `%` as the start of a placeholder."""
    odd = make_workflow(tmp_path / "my code 100%", workflow.bin)

    done = odd.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    service = (_units(odd) / "workflow-tick.service").read_text().splitlines()
    escaped = str(odd.repo).replace("%", "%%")
    assert f"WorkingDirectory={escaped}" in service
    assert f'ExecStart="{escaped}/scripts/workflow/tick"' in service


@_needs_systemd
def test_systemd_finds_nothing_wrong_with_a_checkout_in_an_odd_folder(
    workflow: Workflow, tmp_path: Path
) -> None:
    odd = make_workflow(tmp_path / "my code 100%", workflow.bin)
    odd.run_script("install-timer")

    read = subprocess.run(
        [str(_ANALYZE), "--user", "verify", str(_units(odd) / "workflow-tick.service")],
        capture_output=True,
        text=True,
        check=False,
    )

    about_ours = [
        line
        for line in (read.stdout + read.stderr).splitlines()
        if "workflow-tick" in line and "is not executable" not in line
    ]
    assert about_ours == []
