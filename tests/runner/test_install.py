"""`runner/install.sh` installs a machine's runners: the repository's names from its shipline.json,
the machine's own name, roles and counts from `~/.config/shipline/runner.sh`, the units rendered
for the prefix. `systemctl`, `docker`, `loginctl` and `gh` are stand-ins that write down what
they were asked: nothing is installed on the machine the tests run on."""

import json
import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_INSTALL = _ROOT / "runner/install.sh"
_EXPECTED = Path(__file__).resolve().parent / "expected/units"

_LOGGED = '#!/bin/sh\necho "{name} $*" >> "$CALLS"\n'


@pytest.fixture
def machine(tmp_path: Path) -> Path:
    """A home folder, stand-ins first on the PATH, and the repository's main checkout."""
    home = tmp_path / "home"
    home.mkdir()
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    for name in ("systemctl", "docker", "gh"):
        (bin_folder / name).write_text(_LOGGED.format(name=name))
    (bin_folder / "loginctl").write_text("#!/bin/sh\necho yes\n")
    for program in bin_folder.iterdir():
        program.chmod(0o755)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    return tmp_path


def _env(machine: Path) -> dict[str, str]:
    return {
        "PATH": f"{machine / 'bin'}:{os.environ['PATH']}",
        "HOME": str(machine / "home"),
        "LANG": "C.UTF-8",
        "CALLS": str(machine / "calls.log"),
        "BUILT": str(machine / "built"),
        "WORKFLOW_GITHUB_RAW_GH": str(machine / "bin/gh"),
        "RUNNER_KVM_DEVICE": str(machine / "kvm"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }


def _git(machine: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=machine / "checkout", env=_env(machine), capture_output=True, check=True
    )


def _repository(machine: Path, runner: dict[str, str]) -> None:
    """The repository's shipline.json on main, as last fetched."""
    config = {"repository": "example-owner/example-app", "runner": runner}
    (machine / "checkout/shipline.json").write_text(json.dumps(config))
    _git(machine, "add", "--all")
    _git(machine, "commit", "--quiet", "-m", "main")
    _git(machine, "update-ref", "refs/remotes/origin/main", "HEAD")


_RUNNER = {"prefix": "example-app", "image_build": 'echo built >> "$BUILT"'}
_SETTINGS = "machine=home\nroles='main ordinary planner'\nother_machines=office\nordinary_jobs=2\n"


def _settings(machine: Path, text: str = _SETTINGS) -> Path:
    path = machine / "home/.config/shipline/runner.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _install(machine: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_INSTALL), *args],
        cwd=machine / "checkout",
        env=_env(machine),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _calls(machine: Path, program: str) -> list[str]:
    log = machine / "calls.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line.removeprefix(f"{program} ") for line in lines if line.startswith(f"{program} ")]


def _installed(machine: Path) -> Path:
    return machine / "home/.local/share/example-app-runner"


def test_a_machine_s_runners_are_installed_by_the_prefix_and_its_own_settings(
    machine: Path,
) -> None:
    _repository(machine, _RUNNER)
    _settings(machine)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert "Installed on home: 2 PR job slots, 1 spare, 2 reserved for main" in done.stdout
    settings = (_installed(machine) / "pool-settings.sh").read_text().splitlines()
    assert settings == [
        "repository='example-owner/example-app'",
        "prefix='example-app'",
        "label='example-app-home'",
        "planner_label='example-app-home-planner'",
        f"checkout='{machine / 'checkout'}'",
        "machine='home'",
        "roles='main ordinary planner'",
        "other_machines='office'",
        "ordinary_jobs=2",
        "ordinary_blacksmith_jobs=1",
        "ordinary_spares=1",
        "docker_runners=0",
    ]
    # The repository's own command built the images, on main's files.
    assert (machine / "built").read_text() == "built\n"
    # The units are the ones rendered for the prefix; without a cache command, no cache units.
    units = machine / "home/.config/systemd/user"
    installed = sorted(path.name for path in units.iterdir() if path.is_file())
    assert installed == sorted(
        name
        for name in os.listdir(_EXPECTED)
        if "hub-cache" not in name and "firewall" not in name
    )
    for name in installed:
        assert (units / name).read_text() == (_EXPECTED / name).read_text(), name
    firewall = _installed(machine) / "systemd/example-app-runner-firewall.service"
    assert firewall.read_text() == (_EXPECTED / firewall.name).read_text()
    started = [call for call in _calls(machine, "systemctl") if call.startswith("--user enable")]
    assert started == [
        "--user enable --now example-app-runner-planner.service",
        "--user enable --now example-app-runner-image.timer",
        "--user enable --now example-app-runner-usage.timer",
        "--user enable --now example-app-runner-main.service",
        "--user enable --now example-app-runner-main-2.service",
        "--user enable --now example-app-runner@1.service",
        "--user enable --now example-app-runner@2.service",
        "--user enable --now example-app-runner@3.service",
        "--user enable --now example-app-runner-spare.service",
        "--user enable --now example-app-runner-lend.service",
        "--user enable --now example-app-runner-balance.timer",
    ]
    # The programs that use GitHub are copies, not links into the checkout.
    assert not (_installed(machine) / "github_budget.py").is_symlink()
    assert (_installed(machine) / "github_budget.py").is_file()


def test_a_docker_machine_gets_the_cache_only_when_the_repository_runs_one(machine: Path) -> None:
    (machine / "kvm").touch()
    _settings(machine, "machine=office\nroles='docker'\ndocker_runners=1\n")
    _repository(machine, {**_RUNNER, "hub_cache": "tools/hub-cache"})

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    units = machine / "home/.config/systemd/user"
    assert (units / "example-app-hub-cache.service").is_file()
    assert (units / "example-app-hub-cache-watch.timer").is_file()
    started = [call for call in _calls(machine, "systemctl") if call.startswith("--user enable")]
    assert "--user enable --now example-app-hub-cache.service" in started
    assert "--user enable --now example-app-runner-docker@1.service" in started


def test_without_a_cache_command_a_docker_machine_gets_no_cache(machine: Path) -> None:
    (machine / "kvm").touch()
    _settings(machine, "machine=office\nroles='docker'\ndocker_runners=1\n")
    _repository(machine, _RUNNER)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert not (machine / "home/.config/systemd/user/example-app-hub-cache.service").exists()
    assert not [call for call in _calls(machine, "systemctl") if "hub-cache" in call]


def test_the_machine_settings_file_may_be_named(machine: Path) -> None:
    _repository(machine, _RUNNER)
    elsewhere = machine / "elsewhere.sh"
    elsewhere.write_text(_SETTINGS.replace("machine=home", "machine=desk"))

    done = subprocess.run(
        ["sh", str(_INSTALL)],
        cwd=machine / "checkout",
        env={**_env(machine), "SHIPLINE_RUNNER_SETTINGS": str(elsewhere)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "machine='desk'" in (_installed(machine) / "pool-settings.sh").read_text()


@pytest.mark.parametrize(
    "settings,said",
    [
        (None, "write"),
        ("roles='ordinary'\n", "no machine name"),
        ("machine=Home\nroles='ordinary'\n", "no machine name"),
        ("machine=home\nother_machines='office x|y'\nroles='ordinary'\n", "no machine name"),
        ("machine=home\n", "names no roles"),
    ],
    ids=["no file", "no machine", "capital", "a pattern", "no roles"],
)
def test_a_machine_without_usable_settings_is_refused_before_anything_is_installed(
    machine: Path, settings: str | None, said: str
) -> None:
    _repository(machine, _RUNNER)
    if settings is not None:
        _settings(machine, settings)

    done = _install(machine)

    assert done.returncode != 0
    assert said in done.stderr
    assert not _installed(machine).exists()
    assert not _calls(machine, "systemctl")


@pytest.mark.parametrize("runner", [{"image_build": "true"}, {"prefix": "example-app"}])
def test_a_repository_that_names_no_prefix_or_no_image_build_is_refused(
    machine: Path, runner: dict[str, str]
) -> None:
    _repository(machine, runner)
    _settings(machine)

    done = _install(machine)

    assert done.returncode != 0
    assert "runner.prefix and runner.image_build" in done.stderr
    assert not _calls(machine, "systemctl")


def test_a_machine_is_never_taken_to_be_one_it_was_not_named(machine: Path) -> None:
    _repository(machine, _RUNNER)
    _settings(machine)
    assert _install(machine).returncode == 0
    _settings(machine, _SETTINGS.replace("machine=home", "machine=desk"))

    done = _install(machine)

    assert done.returncode != 0
    assert "installed as home, not desk" in done.stderr


_BOUNDS = "balance_ordinary=2-5\nbalance_docker=0-1\nbalance off\n"


def test_the_balancers_bounds_go_from_the_machine_settings_into_the_installed_ones(
    machine: Path,
) -> None:
    _repository(machine, _RUNNER)
    _settings(machine, _SETTINGS + _BOUNDS)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert (_installed(machine) / "pool-settings.sh").read_text().endswith(_BOUNDS)


def test_a_reinstall_keeps_the_installed_bounds_when_the_machine_settings_name_none(
    machine: Path,
) -> None:
    _repository(machine, _RUNNER)
    _settings(machine)
    assert _install(machine).returncode == 0
    installed = _installed(machine) / "pool-settings.sh"
    # The balancer moved a slot meanwhile, and its owner set bounds there.
    installed.write_text(installed.read_text().replace("ordinary_jobs=2", "ordinary_jobs=3") + _BOUNDS)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    text = installed.read_text()
    # The counts are the machine settings' again; the bounds stay.
    assert "ordinary_jobs=2\n" in text
    assert text.endswith(_BOUNDS)


def test_the_balancer_leaves_the_runners_alone_while_they_are_installed(machine: Path) -> None:
    _repository(machine, _RUNNER)
    _settings(machine)
    lock = machine / "home/.local/state/workflow/runner-balance.lock"
    # Docker is asked while the runners are down: a balancer run then would find them gone.
    (machine / "bin/docker").write_text(
        f'#!/bin/sh\nif flock -n "{lock}" true; then echo free; else echo held; fi >> "$CALLS.lock"\n'
    )

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert set((machine / "calls.log.lock").read_text().split()) == {"held"}


def test_a_machine_with_neither_kind_of_runner_gets_no_balancer(machine: Path) -> None:
    _repository(machine, _RUNNER)
    _settings(machine, "machine=home\nroles='planner'\n")

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert not [call for call in _calls(machine, "systemctl") if "balance" in call]
