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
    "render-units.sh",
    "build-images.sh",
    "hub-cache.sh",
    "main-command.sh",
    "README.md",
]


@pytest.mark.parametrize("name", _FILES)
def test_the_pool_holds_each_of_its_programs(name: str) -> None:
    assert (_RUNNER / name).is_file()


def test_the_pool_holds_its_keepers_and_its_machine_files() -> None:
    assert sorted(_RUNNER.glob("keep-*.sh"))
    assert (_RUNNER / "vm").is_dir() and any((_RUNNER / "vm").iterdir())


def test_no_program_of_the_pool_names_a_repository_or_a_prefix_of_its_own() -> None:
    """Every name comes from the repository's shipline.json and the machine's own settings; only
    the usage of render-units.sh and the README show the example prefix."""
    for path in sorted(_RUNNER.rglob("*")):
        if not path.is_file() or path.name in ("README.md", "render-units.sh"):
            continue
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        for name in ("example-app", "example-owner", "home-planner", "home-main"):
            assert name not in text, f"{path.relative_to(_RUNNER)} names {name}"


def test_shipline_s_own_ci_registers_and_uses_no_runner_of_its_own() -> None:
    for workflow in sorted((_ROOT / ".github/workflows").glob("*.y*ml")):
        text = workflow.read_text()
        assert "self-hosted" not in text, workflow.name
        assert set(re.findall(r"^\s+runs-on: (.+)$", text, re.M)) <= {"ubuntu-latest"}


def _words(text: str) -> str:
    """The text with its line wrapping undone, so a phrase may break across lines."""
    return " ".join(text.split())


@pytest.mark.parametrize(
    "said",
    [
        # How a repository sets the prefix and its image build.
        '"prefix": "example-app"',
        "`runner.prefix`",
        "`runner.image_build`",
        "`runner.label`",
        "`runner.planner_label`",
        "`runner.hub_cache`",
        "`runner.labels`",
        "`<label>-standby`",
        "`<label>-first`",
        "`<label>-main`",
        "`<label>-docker`",
        # How a machine holds its own settings, outside any repository.
        "`~/.config/shipline/runner.sh`",
        "`SHIPLINE_RUNNER_SETTINGS`",
        "machine=home",
        "other_machines=office",
        "roles='main ordinary planner'",
        "ordinary_jobs=2",
        "docker_runners=",
        "`origin/main`",
        # What the repository's own commands are given and must do.
        "`RUNNER_FILES`",
        "`/usr/local/share/shipline-runner/work-as-shipped`",
        "`HUB_CACHE_CONTAINER`",
        # How a machine installs it.
        "sh <shipline>/runner/install.sh",
        "install-firewall.sh",
        # Never Shipline's own.
        "never registers a runner to Shipline itself",
    ],
)
def test_the_readme_says_how_a_repository_sets_the_prefix_and_a_machine_its_settings(
    said: str,
) -> None:
    assert said in _words((_RUNNER / "README.md").read_text())


def test_the_readme_names_every_unit_the_templates_hold() -> None:
    text = (_RUNNER / "README.md").read_text()

    for template in sorted((_RUNNER / "systemd").iterdir()):
        name = template.name.replace("@PREFIX@", "<prefix>")
        assert f"`{name}`" in text, name
