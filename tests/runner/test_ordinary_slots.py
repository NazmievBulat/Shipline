"""A machine whose pull request jobs went to another machine keeps none of its own, until no other
machine has an online ordinary runner: its spare then takes one slot and gives it back once
another machine's ordinary runner is online again."""

from pathlib import Path

import pytest
from slot_stand_ins import ORDINARY, Machine, make_machine, runner_name

OTHER = "machine-one"


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    machine = make_machine(tmp_path)
    machine.settle(role="ordinary", ordinary_slots=0, ordinary_jobs_elsewhere="yes")
    return machine


def _spare(machine: Machine, *labels: str) -> tuple[str, str, list[str]]:
    return (runner_name(machine.name, "spare"), "online", list(labels))


def _label_calls(machine: Machine) -> list[str]:
    return [call for call in machine.calls("gh") if "/labels" in call]


def test_no_slot_while_another_machines_ordinary_runner_is_online(machine: Machine) -> None:
    machine.runners((runner_name(OTHER, "ordinary"), "online", [ORDINARY]), _spare(machine))

    assert machine.ordinary_slots() == 0


def test_one_slot_while_every_other_machines_ordinary_runner_is_offline(machine: Machine) -> None:
    machine.runners(
        (runner_name(OTHER, "ordinary", 1), "offline", [ORDINARY]),
        (runner_name(OTHER, "ordinary", 2), "offline", [ORDINARY]),
        _spare(machine),
    )

    assert machine.ordinary_slots() == 1


def test_one_slot_when_no_other_machine_has_an_ordinary_runner(machine: Machine) -> None:
    machine.runners(
        (runner_name(OTHER, "main"), "online", [f"{ORDINARY}-main"]),
        _spare(machine),
    )

    assert machine.ordinary_slots() == 1


def test_the_spare_holding_the_slot_is_not_another_machines_runner(machine: Machine) -> None:
    """Else the spare would see itself online, give the slot back, and take it again."""
    machine.runners(
        (runner_name(OTHER, "ordinary"), "offline", [ORDINARY]),
        _spare(machine, ORDINARY),
    )

    assert machine.ordinary_slots() == 1


def test_a_machine_with_slots_of_its_own_keeps_them_beside_another_machine(
    machine: Machine,
) -> None:
    machine.settle(role="ordinary", ordinary_slots=3)
    machine.runners((runner_name(OTHER, "ordinary"), "online", [ORDINARY]), _spare(machine))

    assert machine.ordinary_slots() == 3


def test_the_spare_takes_the_slot_while_no_other_machine_has_an_online_ordinary_runner(
    machine: Machine,
) -> None:
    machine.runners((runner_name(OTHER, "ordinary"), "offline", [ORDINARY]), _spare(machine))

    done = machine.keep_spare()

    assert done.returncode == 0, done.stderr
    (added,) = _label_calls(machine)
    assert "POST" in added
    assert ORDINARY in added


def test_the_spare_gives_the_slot_back_once_another_machines_ordinary_runner_is_online(
    machine: Machine,
) -> None:
    machine.runners(
        (runner_name(OTHER, "ordinary"), "online", [ORDINARY]),
        _spare(machine, ORDINARY),
    )

    done = machine.keep_spare()

    assert done.returncode == 0, done.stderr
    (removed,) = _label_calls(machine)
    assert "DELETE" in removed
    assert ORDINARY in removed


def test_the_spare_keeps_what_it_holds_when_the_runner_list_cannot_be_read(
    machine: Machine,
) -> None:
    """No list is no proof that the other machine is gone, nor that it is back."""
    machine.lose_github()

    machine.keep_spare()

    assert _label_calls(machine) == []


def test_the_spare_takes_no_slot_while_another_machines_ordinary_runner_is_online(
    machine: Machine,
) -> None:
    machine.runners((runner_name(OTHER, "ordinary"), "online", [ORDINARY]), _spare(machine))

    done = machine.keep_spare()

    assert done.returncode == 0, done.stderr
    assert _label_calls(machine) == []
