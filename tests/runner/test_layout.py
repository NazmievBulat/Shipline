"""The runner pool lives in `runner/` of Shipline, as the files a repository installs on its own
machines, and Shipline itself registers no runner of its own."""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _ROOT / "runner"

# The programs of the pool: the lender, the ordinary slots, the planner's state, the usage
# sampler, the installers, the names and settings, and the runner's own start.
_FILES = [
    "lend_runners.py",
    "ordinary_slots.py",
    "planner-state.py",
    "usage.py",
    "install.sh",
    "install-firewall.sh",
    "names.sh",
    "settings.sh",
    "start-runner.sh",
    "runner-container.sh",
    "check-docker-runner.sh",
    "sample-usage.sh",
    "README.md",
]


@pytest.mark.parametrize("name", _FILES)
def test_the_pool_holds_each_of_its_programs(name: str) -> None:
    assert (_RUNNER / name).is_file()


def test_the_pool_holds_its_keepers_and_its_machine_files() -> None:
    assert sorted(_RUNNER.glob("keep-*.sh"))
    assert (_RUNNER / "vm").is_dir() and any((_RUNNER / "vm").iterdir())


def test_shipline_s_own_ci_registers_and_uses_no_runner_of_its_own() -> None:
    for workflow in sorted((_ROOT / ".github/workflows").glob("*.y*ml")):
        text = workflow.read_text()
        assert "self-hosted" not in text, workflow.name
        assert set(re.findall(r"^\s+runs-on: (.+)$", text, re.M)) <= {"ubuntu-latest"}
