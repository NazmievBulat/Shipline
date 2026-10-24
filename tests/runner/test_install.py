"""`install.sh` installs a machine with no ordinary slot of its own while the pool has another
machine to take its pull request jobs, keeping its spare, and writes the machine's own memory cap
of one ordinary job, which its job containers get."""

from pathlib import Path

import pytest
from slot_stand_ins import (
    SHARED_JOB_MEMORY,
    SPARE_UNIT,
    Machine,
    make_machine,
    ordinary_unit,
)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return make_machine(tmp_path)


def _enabled(machine: Machine) -> list[str]:
    return [call.split()[-1] for call in machine.calls("systemctl") if " enable --now " in call]


def test_a_machine_of_a_pool_of_several_installs_with_no_ordinary_slot(machine: Machine) -> None:
    machine.settle(slots=0)

    done = machine.install()

    assert done.returncode == 0, done.stderr
    assert "0 PR job slots, 1 spare" in done.stdout
    assert "ordinary_jobs=0" in machine.pool_settings.read_text().splitlines()


def test_a_machine_with_no_ordinary_slot_keeps_its_spare_runner_and_supervisor(
    machine: Machine,
) -> None:
    machine.settle(slots=0)

    machine.install()

    enabled = _enabled(machine)
    assert ordinary_unit(1) in enabled
    assert ordinary_unit(2) not in enabled
    assert SPARE_UNIT in enabled


def test_the_only_machine_of_the_pool_is_refused_with_no_ordinary_slot(machine: Machine) -> None:
    machine.settle(slots=0, alone=True)

    done = machine.install()

    assert done.returncode != 0
    assert "There must be at least 1 ordinary runner" in done.stderr
    assert machine.calls("systemctl") == []


def test_a_machine_with_no_ordinary_slot_and_no_spare_is_refused(machine: Machine) -> None:
    """Its spare is what keeps pull request jobs running while no other machine is up."""
    machine.settle(slots=0, spares=0)

    done = machine.install()

    assert done.returncode != 0
    assert "There must be at least 1 ordinary runner" in done.stderr
    assert machine.calls("systemctl") == []


def test_the_machines_own_job_memory_is_written_into_its_pool_settings(machine: Machine) -> None:
    machine.settle(slots=3, job_memory="6g")

    done = machine.install()

    assert done.returncode == 0, done.stderr
    assert "job_memory=6g" in machine.pool_settings.read_text().splitlines()


def test_a_machine_without_a_job_memory_of_its_own_writes_none(machine: Machine) -> None:
    machine.settle(slots=3)

    done = machine.install()

    assert done.returncode == 0, done.stderr
    assert "job_memory" not in machine.pool_settings.read_text()


@pytest.mark.parametrize("given", ["6", "6 g", "6g; reboot", "$(id)", "0g", "6G", "6g\n7g"])
def test_a_job_memory_docker_cannot_read_as_a_size_is_refused(machine: Machine, given: str) -> None:
    machine.settle(slots=3, job_memory=f"'{given}'")

    done = machine.install()

    assert done.returncode != 0
    assert "job_memory" in done.stderr
    assert machine.calls("systemctl") == []


def _memory_of_the_job_container(machine: Machine) -> str:
    done = machine.start_job_container()
    assert done.returncode == 0, done.stderr
    (run,) = [call.split() for call in machine.calls("docker") if call.startswith("docker run ")]
    return run[run.index("--memory") + 1]


def test_a_job_container_is_capped_at_the_machines_own_job_memory(machine: Machine) -> None:
    machine.installed_as(slots=3, job_memory="6g")

    assert _memory_of_the_job_container(machine) == "6g"


def test_a_job_container_is_capped_at_the_shared_value_when_the_machine_names_none(
    machine: Machine,
) -> None:
    machine.installed_as(slots=3)

    assert _memory_of_the_job_container(machine) == SHARED_JOB_MEMORY
