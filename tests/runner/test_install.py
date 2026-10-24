"""`runner/install.sh` installs a machine's runners: the repository's names from its shipline.json,
the machine's own name, roles and counts from `~/.config/shipline/runner.sh`, the units rendered
for the prefix. `systemctl`, `docker`, `loginctl` and `gh` are stand-ins that write down what
they were asked: nothing is installed on the machine the tests run on."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from slot_stand_ins import (
    FIRST,
    LABEL,
    MACHINE,
    OTHER,
    SHARED_JOB_MEMORY,
    SPARE_UNIT,
    STANDBY,
    Machine,
    make_machine,
    ordinary_unit,
    runner_name,
)

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
    ]
    # The repository's own command built the images, on main's files.
    assert (machine / "built").read_text() == "built\n"
    # The units are the ones rendered for the prefix; without a cache command, no cache units.
    units = machine / "home/.config/systemd/user"
    installed = sorted(path.name for path in units.iterdir() if path.is_file())
    assert installed == sorted(
        name for name in os.listdir(_EXPECTED) if "hub-cache" not in name and "firewall" not in name
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


def test_a_command_of_many_lines_is_read_whole(machine: Path) -> None:
    _repository(machine, {"prefix": "example-app", "image_build": 'set -e\necho built >> "$BUILT"'})
    _settings(machine)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text() == "built\n"


def test_no_value_of_the_file_runs_as_code(machine: Path) -> None:
    config = {
        "repository": "example-owner/example-app",
        "annotations": {"web_suite": "x\nSHIPLINE_REPOSITORY=$(touch injected) #\ny"},
        "runner": _RUNNER,
    }
    (machine / "checkout/shipline.json").write_text(json.dumps(config))
    _git(machine, "add", "--all")
    _git(machine, "commit", "--quiet", "-m", "main")
    _git(machine, "update-ref", "refs/remotes/origin/main", "HEAD")
    _settings(machine)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert not (machine / "checkout/injected").exists()
    assert (
        "repository='example-owner/example-app'"
        in (_installed(machine) / "pool-settings.sh").read_text()
    )


def test_a_checkout_whose_file_is_not_main_s_is_refused(machine: Path) -> None:
    """The pool runs main's commands: the names it installs by are main's too."""
    _repository(machine, _RUNNER)
    _settings(machine)
    config = {"repository": "example-owner/example-app", "runner": {**_RUNNER, "hub_cache": "x"}}
    (machine / "checkout/shipline.json").write_text(json.dumps(config))

    done = _install(machine)

    assert done.returncode != 0
    assert "origin/main" in done.stderr
    assert not _calls(machine, "systemctl")


def test_a_cache_the_repository_no_longer_runs_is_switched_off(machine: Path) -> None:
    (machine / "kvm").touch()
    _settings(machine, "machine=office\nroles='docker'\ndocker_runners=1\n")
    _repository(machine, {**_RUNNER, "hub_cache": "tools/hub-cache"})
    assert _install(machine).returncode == 0
    (machine / "calls.log").unlink()
    _repository(machine, _RUNNER)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    units = machine / "home/.config/systemd/user"
    assert not list(units.glob("example-app-hub-cache*"))
    assert (
        "--user disable --now example-app-hub-cache.service example-app-hub-cache-watch.timer"
        in _calls(machine, "systemctl")
    )


def test_the_github_budget_wrapper_is_installed_by_the_prefix(machine: Path) -> None:
    _repository(machine, _RUNNER)
    _settings(machine)

    assert _install(machine).returncode == 0

    wrapper = machine / "home/.local/share/example-app-github/github-gh.sh"
    assert wrapper.is_file()
    assert (machine / "home/.local/bin/gh").resolve() == wrapper.resolve()


def test_a_count_that_is_not_a_number_is_refused(machine: Path) -> None:
    _repository(machine, _RUNNER)
    _settings(machine, _SETTINGS + "ordinary_blacksmith_jobs=' 1; touch x'\n")

    done = _install(machine)

    assert done.returncode != 0
    assert not _installed(machine).exists()


@pytest.mark.parametrize(
    ("named", "folder"),
    [(None, ".config/shipline-runner"), ("~/.config/example-app-runner", ".config/example-app-runner")],
)
def test_the_runners_read_their_identity_where_the_repository_names_it(
    machine: Path, named: str | None, folder: str
) -> None:
    """An installation keeps the runners' GitHub identity it has: the installed runner programs
    read it in the folder `runner.identity_folder` names, `~/.config/shipline-runner` by default."""
    runner = _RUNNER if named is None else {**_RUNNER, "identity_folder": named}
    _repository(machine, runner)
    _settings(machine)
    identity = machine / "home" / folder
    identity.mkdir(parents=True, mode=0o700)
    (identity / "token").write_text("example-identity\n")

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    seen = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import github_identity; "
            "print(github_identity.folder()); print(github_identity.configured())",
            str(_installed(machine)),
        ],
        env=_env(machine),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert seen == [str(identity), "True"]
    assert f"their own GitHub identity, from {identity}." in done.stdout


def test_a_service_that_restarts_while_the_images_build_finds_its_settings(machine: Path) -> None:
    """install.sh copies the new scripts before the images build, which takes minutes, and stops
    the running services after it: one that restarts meanwhile runs the new scripts, and finds the
    names they need beside them."""
    restart = (
        'cd "$RUNNER_FILES" && '
        'sh -c ". ./settings.sh; . ./pool-settings.sh; . ./names.sh; echo \\$container" >> "$BUILT"'
    )
    _repository(machine, {**_RUNNER, "image_build": restart})
    _settings(machine)
    # What an install before the prefix wrote.
    _installed(machine).mkdir(parents=True)
    (_installed(machine) / "pool-settings.sh").write_text(
        "machine=home\nroles='main ordinary planner'\nother_machines='office'\nordinary_jobs=2\n"
    )

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text() == "example-app-runner\n"


# A machine that gives all its PR slots to the other machines of its pool, and a machine's own
# memory cap of one ordinary job (README.md, "Moving a machine's PR slots").


@pytest.fixture
def pool_machine(tmp_path: Path) -> Machine:
    return make_machine(tmp_path)


def _enabled(machine: Machine) -> list[str]:
    return [call.split()[-1] for call in machine.calls("systemctl") if " enable --now " in call]


def test_a_machine_of_a_pool_of_several_installs_with_no_ordinary_slot(
    pool_machine: Machine,
) -> None:
    pool_machine.settle(slots=0)

    done = pool_machine.install()

    assert done.returncode == 0, done.stderr
    assert "0 PR job slots, 1 spare" in done.stdout
    assert "ordinary_jobs=0" in pool_machine.pool_settings.read_text().splitlines()


def test_a_machine_with_no_ordinary_slot_keeps_its_spare_runner_and_supervisor(
    pool_machine: Machine,
) -> None:
    pool_machine.settle(slots=0)

    pool_machine.install()

    enabled = _enabled(pool_machine)
    assert ordinary_unit(1) in enabled
    assert ordinary_unit(2) not in enabled
    assert SPARE_UNIT in enabled


def test_the_only_machine_of_the_pool_is_refused_with_no_ordinary_slot(
    pool_machine: Machine,
) -> None:
    pool_machine.settle(slots=0, alone=True)

    done = pool_machine.install()

    assert done.returncode != 0
    assert "There must be at least 1 ordinary runner" in done.stderr
    assert "other_machines" in done.stderr
    assert pool_machine.calls("systemctl") == []


def test_a_machine_with_no_ordinary_slot_and_no_spare_is_refused(pool_machine: Machine) -> None:
    """Its spare is what keeps pull request jobs running while no other machine is up."""
    pool_machine.settle(slots=0, spares=0)

    done = pool_machine.install()

    assert done.returncode != 0
    assert "There must be at least 1 ordinary runner" in done.stderr
    assert "ordinary_spares" in done.stderr
    assert pool_machine.calls("systemctl") == []


def test_the_machines_own_job_memory_is_written_into_its_pool_settings(
    pool_machine: Machine,
) -> None:
    pool_machine.settle(slots=3, job_memory="6g")

    done = pool_machine.install()

    assert done.returncode == 0, done.stderr
    assert "job_memory=6g" in pool_machine.pool_settings.read_text().splitlines()


def test_a_machine_without_a_job_memory_of_its_own_writes_none(pool_machine: Machine) -> None:
    pool_machine.settle(slots=3)

    done = pool_machine.install()

    assert done.returncode == 0, done.stderr
    assert "job_memory" not in pool_machine.pool_settings.read_text()


@pytest.mark.parametrize(
    "given", ["6", "6 g", "6g; reboot", "$(id)", "0g", "6G", "6g\n7g", "512m", "1k"]
)
def test_a_job_memory_of_no_whole_gigabytes_is_refused(pool_machine: Machine, given: str) -> None:
    pool_machine.settle(slots=3, job_memory=f"'{given}'")

    done = pool_machine.install()

    assert done.returncode != 0
    assert "job_memory" in done.stderr
    assert pool_machine.calls("systemctl") == []


def _memory_of_the_job_container(machine: Machine, kind: str = "ordinary") -> str:
    done = machine.start_job_container(kind)
    assert done.returncode == 0, done.stderr
    (run,) = [call.split() for call in machine.calls("docker") if call.startswith("docker run ")]
    return run[run.index("--memory") + 1]


def test_a_job_container_is_capped_at_the_machines_own_job_memory(pool_machine: Machine) -> None:
    pool_machine.installed_as(slots=3, job_memory="6g")

    assert _memory_of_the_job_container(pool_machine) == "6g"


def test_a_job_container_is_capped_at_the_shared_value_when_the_machine_names_none(
    pool_machine: Machine,
) -> None:
    pool_machine.installed_as(slots=3)

    assert _memory_of_the_job_container(pool_machine) == SHARED_JOB_MEMORY


def test_mains_job_containers_keep_the_shared_value_beside_the_machines_own(
    pool_machine: Machine,
) -> None:
    """A machine's job_memory is the cap of its ordinary jobs; main's runners keep theirs."""
    pool_machine.installed_as(slots=3, job_memory="6g")

    assert _memory_of_the_job_container(pool_machine, "main") == SHARED_JOB_MEMORY


# A move is both machines agreeing: the receiving machine installs first, then the giving one.


def _holding(machine: str, number: int, status: str = "online") -> tuple[str, str, list[str]]:
    """A machine's ordinary runner holding a PR slot."""
    return (runner_name(machine, number=number), status, [STANDBY, LABEL, FIRST])


def _installed_with_four(pool_machine: Machine, *others: tuple[str, str, list[str]]) -> None:
    pool_machine.installed_as(slots=4)
    pool_machine.runners(*[_holding(MACHINE, n) for n in range(1, 6)], *others)


def test_lowering_the_slots_while_the_other_machine_is_off_is_refused(
    pool_machine: Machine,
) -> None:
    _installed_with_four(pool_machine, _holding(OTHER, 1, "offline"), _holding(OTHER, 2, "offline"))
    pool_machine.settle(slots=2)

    before = pool_machine.installation()

    done = pool_machine.install()

    assert done.returncode != 0
    assert "Install the receiving machine first" in done.stderr
    assert pool_machine.calls("systemctl") == []
    assert "ordinary_jobs=4" in pool_machine.pool_settings.read_text().splitlines()
    assert pool_machine.installation() == before



def test_lowering_the_slots_installs_once_the_other_machine_holds_them(
    pool_machine: Machine,
) -> None:
    _installed_with_four(pool_machine, _holding(OTHER, 1), _holding(OTHER, 2))
    pool_machine.settle(slots=2)

    done = pool_machine.install()

    assert done.returncode == 0, done.stderr
    assert "ordinary_jobs=2" in pool_machine.pool_settings.read_text().splitlines()


def test_main_docker_and_standby_runners_of_the_other_machine_hold_no_slot_given_up(
    pool_machine: Machine,
) -> None:
    _installed_with_four(
        pool_machine,
        (runner_name(OTHER, "main"), "online", [f"{LABEL}-main", LABEL, FIRST]),
        (runner_name(OTHER, "docker"), "online", [f"{LABEL}-docker"]),
        (runner_name(OTHER, number=1), "online", [STANDBY]),
    )
    pool_machine.settle(slots=3)

    before = pool_machine.installation()

    done = pool_machine.install()

    assert done.returncode != 0
    assert pool_machine.calls("systemctl") == []
    assert pool_machine.installation() == before



def test_lowering_the_slots_with_github_s_runners_unreadable_is_refused(
    pool_machine: Machine,
) -> None:
    pool_machine.installed_as(slots=4)
    pool_machine.lose_github()
    pool_machine.settle(slots=2)

    before = pool_machine.installation()

    done = pool_machine.install()

    assert done.returncode != 0
    assert "Could not read GitHub's runners" in done.stderr
    assert pool_machine.calls("systemctl") == []
    assert pool_machine.installation() == before



def test_the_report_says_how_many_more_pr_jobs_the_room_holds(pool_machine: Machine) -> None:
    pool_machine.installed_as(slots=2)
    pool_machine.measure(available_gib=30)

    done = pool_machine.usage("report", "--days", "7")

    assert done.returncode == 0, done.stdout
    lines = done.stdout.strip().splitlines()
    assert lines[-2].startswith("Room for 2 more PR jobs of 10g each")
    assert lines[-1].startswith("Room every day: yes")
    in_jobs_of_6g = pool_machine.usage("report", "--days", "7", "--job-memory", "6g").stdout
    assert "Room for 3 more PR jobs of 6g each" in in_jobs_of_6g


def test_without_a_record_the_machine_holds_its_runners_with_the_label(
    pool_machine: Machine,
) -> None:
    """A machine installed before installs were recorded still holds the slots GitHub lists."""
    pool_machine.runners(
        *[_holding(MACHINE, n) for n in range(1, 4)], _holding(OTHER, 1, "offline")
    )
    pool_machine.settle(slots=0)

    done = pool_machine.install()

    assert done.returncode != 0
    assert "would give up 3 PR slots" in done.stderr
    assert pool_machine.calls("systemctl") == []


def test_the_usage_report_refuses_a_cap_of_no_whole_gigabytes(
    pool_machine: Machine,
) -> None:
    pool_machine.installed_as(slots=2)

    done = pool_machine.usage("report", "--job-memory", "10240m")

    assert done.returncode == 2
    assert "whole gigabytes" in done.stderr


def test_slots_moved_away_come_back_without_measured_room(tmp_path: Path) -> None:
    """The busy machine takes its slots back first, though its week measured no room: that is
    why they left. The other machine then gives them up, once those runners are online."""
    home = make_machine(tmp_path / "home-machine")
    home.installed_as(slots=0)
    home.measure(available_gib=9, pressure=36)
    home.settle(slots=4)

    done = home.install()

    assert done.returncode == 0, done.stderr
    assert "ordinary_jobs=4" in home.pool_settings.read_text().splitlines()
    office = make_machine(tmp_path / "office-machine")
    office.name = OTHER
    office.installed_as(slots=4)
    office.runners(*[_holding(MACHINE, n) for n in range(1, 5)], _holding(OTHER, 1))
    office.settle(slots=0)

    done = office.install()

    assert done.returncode == 0, done.stderr
    assert "ordinary_jobs=0" in office.pool_settings.read_text().splitlines()


def test_a_pool_machine_whose_identity_gives_no_token_takes_a_new_one(
    pool_machine: Machine,
) -> None:
    """The runner list is read with the new identity, so a lapsed one can be replaced."""
    pool_machine.installed_as(slots=4)
    pool_machine.identity.mkdir(parents=True, exist_ok=True)
    (pool_machine.identity / "app-id").write_text("12345\n")
    token = pool_machine.root / "token"
    token.write_text("stand-in\n")

    done = pool_machine.install("--github-token", str(token))

    assert done.returncode == 0, done.stderr
    assert (pool_machine.identity / "token").read_text() == "stand-in\n"
    assert not (pool_machine.identity / "app-id").exists()


def test_a_refused_move_with_a_new_identity_leaves_the_counts_units_and_installed_copy(
    pool_machine: Machine,
) -> None:
    """The new identity, which GitHub has just accepted, is all a refused move changes."""
    _installed_with_four(pool_machine, _holding(OTHER, 1, "offline"))
    pool_machine.settle(slots=2)
    token = pool_machine.root / "token"
    token.write_text("stand-in\n")
    before = pool_machine.installation(identity=False)

    done = pool_machine.install("--github-token", str(token))

    assert done.returncode != 0
    assert "Install the receiving machine first" in done.stderr
    assert pool_machine.installation(identity=False) == before
    assert pool_machine.calls("systemctl") == []
    assert (pool_machine.identity / "token").read_text() == "stand-in\n"


def test_an_identity_github_refuses_stops_the_install_with_nothing_changed(
    pool_machine: Machine,
) -> None:
    pool_machine.installed_as(slots=4)
    pool_machine.refuse_new_identities()
    pool_machine.settle(slots=4)
    token = pool_machine.root / "token"
    token.write_text("stand-in\n")
    before = pool_machine.installation()

    done = pool_machine.install("--github-token", str(token))

    assert done.returncode != 0
    assert "Resource not accessible" in done.stderr
    assert pool_machine.installation() == before
    assert pool_machine.calls("systemctl") == []
