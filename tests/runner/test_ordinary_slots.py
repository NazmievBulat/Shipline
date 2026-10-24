"""A machine that gave its pull request slots to another machine keeps none of its own, until no
other machine has an online ordinary runner: its spare then takes one slot and gives it back once
another machine's ordinary runner is online again.

Every machine's runners carry the same labels, so another machine's ordinary runner is one that
carries the base label and whose name is another machine's."""

from pathlib import Path

import pytest
from slot_stand_ins import (
    DOCKER,
    LABEL,
    MAIN,
    OTHER,
    STANDBY,
    Machine,
    make_machine,
    runner_name,
)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    machine = make_machine(tmp_path)
    machine.settle(slots=0)
    return machine


def _other(status: str, number: int = 1) -> tuple[str, str, list[str]]:
    """One of the other machine's ordinary runners."""
    return (runner_name(OTHER, number=number), status, [LABEL])


def _spare(machine: Machine, *labels: str) -> tuple[str, str, list[str]]:
    return (runner_name(machine.name, "spare"), "online", list(labels or [STANDBY]))


def test_no_slot_while_another_machines_ordinary_runner_is_online(machine: Machine) -> None:
    machine.runners(_other("online"), _spare(machine))

    assert machine.ordinary_slots() == 0


def test_one_slot_while_every_other_machines_ordinary_runner_is_offline(machine: Machine) -> None:
    machine.runners(_other("offline", 1), _other("offline", 2), _spare(machine))

    assert machine.ordinary_slots() == 1


def test_main_docker_and_standby_runners_of_another_machine_take_no_pull_request_job(
    machine: Machine,
) -> None:
    machine.runners(
        (runner_name(OTHER, "main"), "online", [MAIN]),
        (runner_name(OTHER, "docker"), "online", [DOCKER]),
        (runner_name(OTHER, "spare"), "online", [STANDBY]),
        _spare(machine),
    )

    assert machine.ordinary_slots() == 1


def test_the_spare_holding_the_slot_is_not_another_machines_runner(machine: Machine) -> None:
    """Else the spare would see itself online, give the slot back, and take it again."""
    machine.runners(_other("offline"), _spare(machine, LABEL))

    assert machine.ordinary_slots() == 1


def test_a_machine_with_slots_of_its_own_keeps_them_beside_another_machine(
    machine: Machine,
) -> None:
    machine.settle(slots=3)
    machine.runners(_other("online"), _spare(machine))

    assert machine.ordinary_slots() == 3


def test_the_spare_takes_the_slot_while_no_other_machine_has_an_online_ordinary_runner(
    machine: Machine,
) -> None:
    machine.runners(_other("offline"), _spare(machine))

    done = machine.keep_spare()

    assert done.returncode == 0, done.stderr
    assert LABEL in machine.labels_of(runner_name(machine.name, "spare"))


def test_the_spare_gives_the_slot_back_once_another_machines_ordinary_runner_is_online(
    machine: Machine,
) -> None:
    machine.runners(_other("online"), _spare(machine, LABEL))

    done = machine.keep_spare()

    assert done.returncode == 0, done.stderr
    labels = machine.labels_of(runner_name(machine.name, "spare"))
    assert LABEL not in labels
    assert STANDBY in labels


def test_the_spare_keeps_what_it_holds_when_the_runner_list_cannot_be_read(
    machine: Machine,
) -> None:
    """No list is no proof that the other machine is gone, nor that it is back."""
    machine.lose_github()

    machine.keep_spare()

    assert [call for call in machine.calls("gh") if "/labels" in call] == []


def test_the_spare_takes_no_slot_while_another_machines_ordinary_runner_is_online(
    machine: Machine,
) -> None:
    machine.runners(_other("online"), _spare(machine))

    done = machine.keep_spare()

    assert done.returncode == 0, done.stderr
    assert machine.labels_of(runner_name(machine.name, "spare")) == ["self-hosted", STANDBY]
