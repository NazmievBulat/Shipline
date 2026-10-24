"""A machine that gave its pull request slots to another machine keeps none of its own, until no
other machine has an online ordinary runner: its spare then takes one slot and gives it back once
another machine's ordinary runner is online again.

Every machine's runners carry the same labels, so another machine's ordinary runner is one whose
name is another machine's ordinary runner's (`<machine>-<n>`), online with the base label."""

from pathlib import Path

import pytest
from slot_stand_ins import (
    DOCKER,
    FIRST,
    LABEL,
    MACHINE,
    MAIN,
    OTHER,
    ROUND,
    STANDBY,
    Machine,
    make_machine,
    runner_name,
)

# The spare of the machine with no slot of its own: its one ordinary runner, beyond its 0 slots.
SPARE = runner_name(MACHINE, number=1)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    machine = make_machine(tmp_path)
    machine.installed_as(slots=0)
    return machine


def _other(status: str, number: int = 1) -> tuple[str, str, list[str]]:
    """One of the other machine's ordinary runners, holding a slot there."""
    return (runner_name(OTHER, number=number), status, [STANDBY, LABEL, FIRST])


def _spare(*labels: str) -> tuple[str, str, list[str]]:
    return (SPARE, "online", [STANDBY, *labels])


def test_no_slot_while_another_machines_ordinary_runner_is_online(machine: Machine) -> None:
    machine.runners(_other("online"), _spare())

    assert machine.ordinary_slots() == 0


def test_one_slot_while_every_other_machines_ordinary_runner_is_offline(machine: Machine) -> None:
    machine.runners(_other("offline", 1), _other("offline", 2), _spare())

    assert machine.ordinary_slots() == 1


def test_no_slot_while_another_machines_ordinary_runner_runs_a_job(machine: Machine) -> None:
    machine.runners(_other("busy"), _spare())

    assert machine.ordinary_slots() == 0


def test_no_slot_from_a_runner_list_older_than_the_supervisors_rounds(machine: Machine) -> None:
    """An old list is no proof that the other machine is off now."""
    machine.runners(_other("offline"), _spare())

    assert machine.ordinary_slots(age=60) == 0


def test_the_planner_counts_the_slot_the_spare_takes(machine: Machine) -> None:
    """Else the planner would see no free slot here while the spare waits for a job."""
    machine.runners(_other("offline"), _spare(LABEL, FIRST))

    assert machine.planner_slots() == 1


def test_the_planner_counts_no_slot_while_another_machines_ordinary_runner_is_online(
    machine: Machine,
) -> None:
    machine.runners(_other("online"), _spare())

    assert machine.planner_slots() == 0


def test_one_slot_when_the_runner_list_names_no_other_machines_runner(machine: Machine) -> None:
    machine.runners(_spare())

    assert machine.ordinary_slots() == 1


def test_main_docker_and_standby_runners_of_another_machine_take_no_pull_request_job(
    machine: Machine,
) -> None:
    """Main's runner, even while it is lent to pull request jobs, is busy when main has work."""
    machine.runners(
        (runner_name(OTHER, "main"), "online", [MAIN, LABEL, FIRST]),
        (runner_name(OTHER, "docker"), "online", [DOCKER]),
        (runner_name(OTHER, number=3), "online", [STANDBY]),
        _spare(),
    )

    assert machine.ordinary_slots() == 1


def test_the_spare_holding_the_slot_is_not_another_machines_runner(machine: Machine) -> None:
    """Else the spare would see itself online, give the slot back, and take it again."""
    machine.runners(_other("offline"), _spare(LABEL, FIRST))

    assert machine.ordinary_slots() == 1


def test_a_machine_with_slots_of_its_own_keeps_them_beside_another_machine(
    tmp_path: Path,
) -> None:
    machine = make_machine(tmp_path)
    machine.installed_as(slots=3)
    machine.runners(_other("online"), _spare())

    assert machine.ordinary_slots() == 3


def test_the_spare_takes_the_slot_while_no_other_machine_has_an_online_ordinary_runner(
    machine: Machine,
) -> None:
    machine.runners(_other("offline"), _spare())

    done = machine.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert machine.labels_of(SPARE) == ["self-hosted", STANDBY, LABEL, FIRST]


def test_the_spare_gives_the_slot_back_once_another_machines_ordinary_runner_is_online(
    machine: Machine,
) -> None:
    machine.runners(_other("online"), _spare(LABEL, FIRST))

    done = machine.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert machine.labels_of(SPARE) == ["self-hosted", STANDBY]


def test_a_spare_running_a_job_keeps_its_slot_until_the_job_ends(machine: Machine) -> None:
    """The other machine is back, but a job that started on the spare is not cut off."""
    machine.runners(_other("online"), (SPARE, "busy", [STANDBY, LABEL, FIRST]))

    done = machine.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert machine.labels_of(SPARE) == ["self-hosted", STANDBY, LABEL, FIRST]


def test_the_spare_takes_no_slot_while_another_machines_ordinary_runner_is_online(
    machine: Machine,
) -> None:
    machine.runners(_other("online"), _spare())

    done = machine.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert machine.labels_of(SPARE) == ["self-hosted", STANDBY]


def test_the_spare_keeps_what_it_holds_when_the_runner_list_cannot_be_read(
    machine: Machine,
) -> None:
    """No list is no proof that the other machine is gone, nor that it is back."""
    machine.lose_github()

    done = machine.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert [call for call in machine.calls("gh") if "/labels" in call] == []
