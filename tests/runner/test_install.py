"""`install.sh` installs a machine with no ordinary slot of its own when another machine takes its
pull request jobs, and writes the machine's own memory cap of one ordinary job."""

from pathlib import Path

import pytest
from slot_stand_ins import SHARED_JOB_MEMORY, Machine, make_machine


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return make_machine(tmp_path)


def test_a_machine_whose_jobs_go_to_another_machine_installs_with_no_ordinary_slot(
    machine: Machine,
) -> None:
    machine.settle(role="ordinary", ordinary_slots=0, ordinary_jobs_elsewhere="yes")

    done = machine.install()

    assert done.returncode == 0, done.stderr
    enabled = " ".join(machine.calls("systemctl"))
    assert "spare" in enabled
    assert "ordinary@" not in enabled


def test_a_machine_with_no_ordinary_slot_and_no_other_machine_for_its_jobs_is_refused(
    machine: Machine,
) -> None:
    machine.settle(role="ordinary", ordinary_slots=0)

    done = machine.install()

    assert done.returncode != 0
    assert "There must be at least 1 ordinary runner" in done.stderr
    assert machine.calls("systemctl") == []


def test_the_machines_own_job_memory_is_written_into_its_pool_settings(machine: Machine) -> None:
    machine.settle(role="ordinary", ordinary_slots=3, job_memory="5g")

    done = machine.install()

    assert done.returncode == 0, done.stderr
    assert "job_memory=5g" in machine.pool_settings.read_text().splitlines()


def test_a_job_container_is_capped_at_the_machines_own_job_memory(machine: Machine) -> None:
    machine.settle(role="ordinary", ordinary_slots=3, job_memory="5g")
    assert machine.install().returncode == 0

    done = machine.start_job_container()

    assert done.returncode == 0, done.stderr
    (run,) = [call for call in machine.calls("docker") if call.startswith("docker run ")]
    assert "--memory 5g" in run


def test_a_job_container_is_capped_at_the_shared_value_when_the_machine_names_none(
    machine: Machine,
) -> None:
    machine.settle(role="ordinary", ordinary_slots=3)
    assert machine.install().returncode == 0

    done = machine.start_job_container()

    assert done.returncode == 0, done.stderr
    (run,) = [call for call in machine.calls("docker") if call.startswith("docker run ")]
    assert f"--memory {SHARED_JOB_MEMORY}" in run
