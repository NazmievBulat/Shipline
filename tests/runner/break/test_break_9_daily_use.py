"""Breaking tests for pull request 9: an installation that already runs a pool of this shape
installs it again with Shipline's install.sh, and replays what it does every day.

`systemctl`, `docker`, `loginctl` and `gh` are stand-ins that write down what they were asked:
nothing is installed on the machine the tests run on."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_RUNNER = _ROOT / "runner"
_INSTALL = _RUNNER / "install.sh"
_EXPECTED = _ROOT / "tests/runner/expected/units"

# The stand-ins. `systemctl list-units` answers what $RUNNING holds; `gh` answers the runner
# listing with what $REGISTERED holds and a registration token with a word.
_SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$CALLS"
case "$*" in
  *list-units*) [ -f "$RUNNING" ] && cat "$RUNNING" ;;
esac
exit 0
"""
_DOCKER = """#!/bin/sh
echo "docker $*" >> "$CALLS"
case "$1" in
  image) echo sha256:example ;;
esac
exit 0
"""
_GH = """#!/bin/sh
echo "gh $*" >> "$CALLS"
case "$*" in
  *registration-token*) echo example-token ;;
  *"-X DELETE"*) ;;
  *actions/runners*) [ -f "$REGISTERED" ] && cat "$REGISTERED" ;;
esac
exit 0
"""

_PC_SETTINGS = (
    "machine=home\n"
    "other_machines=office\n"
    "roles='planner main ordinary docker'\n"
    "ordinary_jobs=4\n"
    "ordinary_blacksmith_jobs=1\n"
    "docker_runners=3\n"
)
_OFFICE_SETTINGS = (
    "machine=office\n"
    "other_machines=home\n"
    "roles='ordinary docker'\n"
    "ordinary_jobs=2\n"
    "ordinary_blacksmith_jobs=1\n"
    "docker_runners=1\n"
)
_RUNNER_CONFIG = {
    "prefix": "example-app",
    "label": "example-app-home",
    "planner_label": "home-planner",
    "image_build": 'echo "built $RUNNER_PREFIX" >> "$BUILT"',
    "hub_cache": "tools/hub-cache",
}


@pytest.fixture
def machine(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    for name, text in (("systemctl", _SYSTEMCTL), ("docker", _DOCKER), ("gh", _GH)):
        (bin_folder / name).write_text(text)
    (bin_folder / "loginctl").write_text("#!/bin/sh\necho yes\n")
    for program in bin_folder.iterdir():
        program.chmod(0o755)
    (tmp_path / "checkout").mkdir()
    (tmp_path / "kvm").touch()
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    return tmp_path


def _env(machine: Path) -> dict[str, str]:
    python = Path(sys.executable).parent
    return {
        "PATH": f"{machine / 'bin'}:{python}:{os.environ['PATH']}",
        "HOME": str(machine / "home"),
        "LANG": "C.UTF-8",
        "CALLS": str(machine / "calls.log"),
        "BUILT": str(machine / "built"),
        "RUNNING": str(machine / "running"),
        "REGISTERED": str(machine / "registered"),
        "WORKFLOW_GITHUB_RAW_GH": str(machine / "bin/gh"),
        "RUNNER_KVM_DEVICE": str(machine / "kvm"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "owner@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "owner@example.com",
    }


def _git(machine: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=machine / "checkout", env=_env(machine), capture_output=True, check=True
    )


def _main(machine: Path, runner: dict, files: dict[str, str] | None = None) -> None:
    """The app's main, as last fetched: its shipline.json and any files of its own."""
    checkout = machine / "checkout"
    config = {"repository": "example-owner/example-app", "runner": runner}
    (checkout / "shipline.json").write_text(json.dumps(config))
    for name, text in (files or {}).items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    _git(machine, "add", "--all")
    _git(machine, "commit", "--quiet", "--allow-empty", "-m", "main")
    _git(machine, "update-ref", "refs/remotes/origin/main", "HEAD")


def _settings(machine: Path, text: str) -> None:
    path = machine / "home/.config/shipline/runner.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _install(machine: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_INSTALL), *args],
        cwd=machine / "checkout",
        env=_env(machine),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _calls(machine: Path, program: str) -> list[str]:
    log = machine / "calls.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return [line.removeprefix(f"{program} ") for line in lines if line.startswith(f"{program} ")]


def _installed(machine: Path) -> Path:
    return machine / "home/.local/share/example-app-runner"


def _enabled(machine: Path) -> set[str]:
    return {
        call.removeprefix("--user enable --now ")
        for call in _calls(machine, "systemctl")
        if call.startswith("--user enable --now ")
    }


def _pc_units() -> set[str]:
    return (
        {f"example-app-runner@{n}.service" for n in range(1, 6)}
        | {f"example-app-runner-docker@{n}.service" for n in range(1, 4)}
        | {
            "example-app-runner-main.service",
            "example-app-runner-main-2.service",
            "example-app-runner-planner.service",
            "example-app-runner-spare.service",
            "example-app-runner-lend.service",
            "example-app-runner-image.timer",
            "example-app-runner-usage.timer",
            "example-app-hub-cache.service",
            "example-app-hub-cache-watch.timer",
        }
    )


def _registration(machine: Path, kind: str, number: str) -> str:
    """What one keeper registers: the `docker run` it starts, its first round only."""
    calls = machine / "calls.log"
    calls.unlink(missing_ok=True)
    keeper = subprocess.Popen(
        ["sh", str(_installed(machine) / "keep-runner.sh"), number, kind],
        env={**_env(machine), "RUNNER_WITHOUT_FIREWALL": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            runs = [call for call in _calls(machine, "docker") if call.startswith("run ")]
            if runs:
                return runs[0]
            time.sleep(0.1)
        raise AssertionError("the keeper started no runner")
    finally:
        os.killpg(keeper.pid, signal.SIGKILL)
        keeper.wait()


# 1. The main machine: every role, four PR slots and a spare, three Docker runners, the cache, and
#    the planner's label the bare word its workflows ask for.
def test_the_main_machine_installs_the_same_units_and_registers_the_same_runners(
    machine: Path,
) -> None:
    _main(machine, _RUNNER_CONFIG)
    _settings(machine, _PC_SETTINGS)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert "Installed on home: 4 PR job slots, 1 spare, 2 reserved for main, and 3 with Docker" in (
        done.stdout
    )
    # Every unit of the pool, byte for byte the ones rendered for the prefix.
    units = machine / "home/.config/systemd/user"
    for path in units.iterdir():
        if path.is_file():
            assert path.read_text() == (_EXPECTED / path.name).read_text(), path.name
    firewall = _installed(machine) / "systemd/example-app-runner-firewall.service"
    assert firewall.read_text() == (_EXPECTED / firewall.name).read_text()
    assert _enabled(machine) == _pc_units()
    settings = (_installed(machine) / "pool-settings.sh").read_text()
    assert "planner_label='home-planner'" in settings
    assert (machine / "built").read_text() == "built example-app\n"

    # The same runner names register, each with the label its jobs ask for.
    expected = {
        ("0", "planner"): ("home-planner", "home-planner", "example-app-runner-planner"),
        ("0", "main"): ("home-main", "example-app-home-main", "example-app-runner-main"),
        ("2", "main"): ("home-main-2", "example-app-home-main", "example-app-runner-main-2"),
        ("1", "ordinary"): ("home-1", "example-app-home-standby", "example-app-runner-1"),
        ("5", "ordinary"): ("home-5", "example-app-home-standby", "example-app-runner-5"),
        ("1", "docker"): ("home-docker-1", "example-app-home-docker", "example-app-runner-docker-1"),
    }
    for (number, kind), (name, label, container) in expected.items():
        run = _registration(machine, kind, number)
        assert f"--env RUNNER_NAME={name} " in run, run
        assert f"--env RUNNER_LABEL={label} " in run, run
        assert f"--env RUNNER_REPOSITORY=example-owner/example-app " in run, run
        assert f"--name {container} " in run, run


# 2. The lender lends this machine's main runner while main has no job waiting.
def test_the_lender_still_lends_home_main_while_main_has_none(machine: Path) -> None:
    _main(machine, _RUNNER_CONFIG)
    _settings(machine, _PC_SETTINGS)
    assert _install(machine).returncode == 0
    installed = _installed(machine)
    (installed / "lend_runners.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")

    done = subprocess.run(
        ["sh", str(installed / "keep-lending.sh")],
        env=_env(machine),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    repository, home, first, main, most, seconds, own = json.loads(done.stdout)
    assert (repository, home, first, main, own) == (
        "example-owner/example-app",
        "example-app-home",
        "example-app-home-first",
        "example-app-home-main",
        "home-main",
    )
    sys.path.insert(0, str(_RUNNER))
    try:
        import lend_runners
    finally:
        sys.path.remove(str(_RUNNER))
    labels = lend_runners.Labels(home=home, first=first, main=main)
    runners = [
        lend_runners.Registration(31, "home-main", True, False, frozenset({main})),
        lend_runners.Registration(32, "home-main-2", True, False, frozenset({main})),
    ]
    changes = lend_runners.lending(
        [r for r in runners if r.name.startswith(own)],
        labels,
        main_waiting=False,
        first_waiting=False,
        most=int(most),
    )
    assert {(change, label) for change, _, label in changes} == {("add", home), ("add", first)}


# 3. A reinstall over the running pool: nothing left stopped, nothing registered twice or retired,
#    the identity and the budget wrapper's state kept.
def test_a_reinstall_over_the_running_pool_starts_again_all_it_stopped(machine: Path) -> None:
    _main(machine, _RUNNER_CONFIG)
    _settings(machine, _PC_SETTINGS)
    identity = machine / "home/.config/shipline-runner"
    identity.mkdir(parents=True, mode=0o700)
    (identity / "token").write_text("example-identity\n")
    (identity / "token").chmod(0o600)
    assert _install(machine).returncode == 0
    wrapper = machine / "home/.local/share/example-app-github"
    upstream = (wrapper / "github-upstream").read_text()
    budget = machine / "home/.local/state/workflow/github-budget"
    budget.mkdir(parents=True, exist_ok=True)
    (budget / "runner-state.json").write_text('{"example": 1}')
    running = sorted(u for u in _pc_units() if u.endswith(".service") and "hub-cache" not in u)
    (machine / "running").write_text(
        "".join(f"{unit} loaded active running CI runner\n" for unit in running)
    )
    (machine / "registered").write_text(
        "".join(
            f"{n}\t{name}\n"
            for n, name in enumerate(
                [*(f"home-{i}" for i in range(1, 6)), "home-docker-1", "home-docker-3",
                 "home-main", "home-main-2", "home-planner", "office-1", "office-3"],
                start=100,
            )
        )
    )
    (machine / "calls.log").unlink()

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    calls = _calls(machine, "systemctl")
    stop = next(call for call in calls if call.startswith("--user stop "))
    starts = _enabled(machine)
    assert calls.index(stop) < min(
        calls.index(f"--user enable --now {unit}") for unit in starts
    )
    assert set(running) <= starts, set(running) - starts
    assert not [call for call in _calls(machine, "gh") if "DELETE" in call]
    assert (identity / "token").read_text() == "example-identity\n"
    assert "use their own GitHub identity" in done.stdout
    assert (wrapper / "github-upstream").read_text() == upstream
    assert (machine / "home/.local/bin/gh").resolve() == (wrapper / "github-gh.sh").resolve()
    assert (budget / "runner-state.json").read_text() == '{"example": 1}'


# 4. The installation's runner identity is where its own installer kept it,
#    `~/.config/<prefix>-runner`, and its shipline.json names that folder as
#    `runner.identity_folder`. After the install the runner programs must still use it.
def test_the_runners_keep_the_github_identity_the_installation_has_today(machine: Path) -> None:
    _main(machine, {**_RUNNER_CONFIG, "identity_folder": "~/.config/example-app-runner"})
    _settings(machine, _PC_SETTINGS)
    identity = machine / "home/.config/example-app-runner"
    identity.mkdir(parents=True, mode=0o700)
    (identity / "token").write_text("example-identity\n")
    (identity / "token").chmod(0o600)

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    # What the installed runner programs (keep-runner.sh, keep-spare.sh, the lender) read.
    seen = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import github_identity as g; "
            "print(g.folder()); print(g.configured())",
            str(_installed(machine)),
        ],
        env=_env(machine),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert seen == [str(identity), "True"], seen
    assert "use their own GitHub identity" in done.stdout, done.stdout


# 5. The second machine: ordinary and Docker only, fewer slots. It never touches the main
#    machine's registrations and starts none of main's units.
def test_the_second_machine_installs_its_own_runners_only(machine: Path) -> None:
    _main(machine, _RUNNER_CONFIG)
    _settings(machine, _OFFICE_SETTINGS)
    (machine / "registered").write_text(
        "".join(
            f"{n}\t{name}\n"
            for n, name in enumerate(
                [*(f"home-{i}" for i in range(1, 8)), "home-main", "home-planner",
                 "office-1", "office-2", "office-3", "office-7", "office-docker-1"],
                start=100,
            )
        )
    )

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert _enabled(machine) == {
        "example-app-runner@1.service",
        "example-app-runner@2.service",
        "example-app-runner@3.service",
        "example-app-runner-spare.service",
        "example-app-runner-docker@1.service",
        "example-app-runner-image.timer",
        "example-app-runner-usage.timer",
        "example-app-hub-cache.service",
        "example-app-hub-cache-watch.timer",
    }
    deleted = [call for call in _calls(machine, "gh") if "DELETE" in call]
    assert len(deleted) == 1 and deleted[0].endswith("actions/runners/112"), deleted
    assert _registration(machine, "ordinary", "3").count("RUNNER_NAME=office-3 ") == 1


# 6. The weekly image timer builds the app's own images, from its own Dockerfile and
#    vm.Dockerfile on origin/main, with no environment but the unit's.
def test_the_image_timer_builds_the_app_s_own_images_on_main(machine: Path) -> None:
    command = (
        'test -f .github/runner/Dockerfile && test -f .github/runner/vm.Dockerfile && '
        'test -f "$RUNNER_FILES/start-runner.sh" && test -d "$RUNNER_FILES/vm" && '
        'echo "built $RUNNER_PREFIX $(cat .github/runner/Dockerfile)" >> "$BUILT"'
    )
    _main(
        machine,
        {**_RUNNER_CONFIG, "image_build": command},
        {".github/runner/Dockerfile": "main", ".github/runner/vm.Dockerfile": "main"},
    )
    _settings(machine, _PC_SETTINGS)
    assert _install(machine).returncode == 0
    # A branch is checked out in the main checkout, with a Dockerfile of its own.
    _git(machine, "checkout", "--quiet", "-b", "example-branch")
    (machine / "checkout/.github/runner/Dockerfile").write_text("branch")
    _git(machine, "commit", "--quiet", "-am", "branch")
    (machine / "built").unlink()

    env = {
        key: value
        for key, value in _env(machine).items()
        if key in ("PATH", "HOME", "LANG", "BUILT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
    }
    done = subprocess.run(
        [str(_installed(machine) / "build-images.sh")],
        cwd=machine / "home",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text() == "built example-app main\n"


# 7. Two builds at once: the weekly timer's build is under way when main has moved and the owner
#    installs again (install.sh builds the images too). The first build must not lose its files.
def test_a_build_under_way_keeps_its_files_when_a_second_one_starts(machine: Path) -> None:
    command = 'cat step-1 >> "$BUILT"; sleep 3; cat step-2 >> "$BUILT"'
    _main(machine, {**_RUNNER_CONFIG, "image_build": command}, {"step-1": "one\n", "step-2": "two\n"})
    _settings(machine, _PC_SETTINGS)
    assert _install(machine).returncode == 0
    (machine / "built").unlink()
    build = str(_installed(machine) / "build-images.sh")

    timer = subprocess.Popen(
        [build], cwd=machine / "home", env=_env(machine), stderr=subprocess.PIPE, text=True
    )
    time.sleep(1.5)
    _main(machine, {**_RUNNER_CONFIG, "image_build": command}, {"step-2": "two, newer\n"})
    second = subprocess.run(
        [build], cwd=machine / "home", env=_env(machine), capture_output=True, text=True,
        timeout=60,
    )
    _, timer_errors = timer.communicate(timeout=60)

    assert second.returncode == 0, second.stderr
    assert timer.returncode == 0, timer_errors
    assert (machine / "built").read_text().count("two") == 2


# 8. A machine runs one pool (runner/README.md). An install under another prefix, over a pool
#    that runs, must not start a second pool beside it with the same runner names.
def test_an_install_under_another_prefix_does_not_start_a_second_pool(machine: Path) -> None:
    _main(machine, _RUNNER_CONFIG)
    _settings(machine, _PC_SETTINGS)
    assert _install(machine).returncode == 0
    _main(machine, {**_RUNNER_CONFIG, "prefix": "example-app-runner"})
    (machine / "calls.log").unlink()

    done = _install(machine)

    started = [unit for unit in _enabled(machine) if unit.startswith("example-app-runner-runner")]
    assert done.returncode != 0 and not started, (done.returncode, sorted(started)[:3])


# 9. A reinstall over today's pool. Its spare supervisor and planner keep running while install.sh
#    builds the images (minutes), after it has copied the new scripts over theirs: every five
#    seconds they call `ordinary_slots.py <jobs> <reduced> <repository>`, as they were started.
#    The image command here replays that call at that moment, from the installed folder.
def test_today_s_running_supervisors_keep_working_while_the_reinstall_builds(
    machine: Path,
) -> None:
    probe = (
        'python3 "$RUNNER_FILES/ordinary_slots.py" 4 1 example-owner/example-app '
        '>> "$BUILT" 2>/dev/null; echo "exit $?" >> "$BUILT"'
    )
    _main(machine, {**_RUNNER_CONFIG, "image_build": probe})
    _settings(machine, _PC_SETTINGS)
    # Today's install: its scripts and the pool settings it wrote, without the repository's names.
    installed = _installed(machine)
    installed.mkdir(parents=True)
    (installed / "pool-settings.sh").write_text(
        "machine=home\nroles='planner main ordinary docker'\nother_machines='office'\n"
        "ordinary_jobs=4\nordinary_blacksmith_jobs=1\n"
    )

    done = _install(machine)

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text().splitlines()[-1] == "exit 0", (
        machine / "built"
    ).read_text()


# 10. The merge gate re-runs failed jobs of every label the pool's runners carry, the bare
#     planner label among them, when shipline.json names the label and the planner's label.
def test_the_gate_reruns_jobs_of_the_bare_planner_label(machine: Path) -> None:
    _main(machine, _RUNNER_CONFIG)
    done = subprocess.run(
        [sys.executable, str(_ROOT / "scripts/workflow/workflow_config.py"), "sh"],
        cwd=machine / "checkout",
        env=_env(machine),
        capture_output=True,
        text=True,
        check=True,
    )
    line = next(x for x in done.stdout.splitlines() if x.startswith("SHIPLINE_RUNNER_LABELS="))
    words = subprocess.run(
        ["sh", "-c", f'{line}; eval "set -- $SHIPLINE_RUNNER_LABELS"; printf "%s\\n" "$@"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert words == [
        "example-app-home",
        "example-app-home-standby",
        "example-app-home-first",
        "example-app-home-main",
        "example-app-home-docker",
        "home-planner",
    ]
