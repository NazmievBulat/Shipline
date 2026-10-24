"""`runner/build-images.sh` builds the runner images: the install runs it, and so does a weekly
timer, because GitHub refuses runners that fall far behind. The repository's own command
(`runner.image_build` of its shipline.json) builds them when it names one; else the pool builds
its own Dockerfile, and its virtual machine's only on a machine with /dev/kvm. Both the command
and its files are main's as last fetched, never what the checkout holds."""

import json
import os
import subprocess
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[2] / "runner"
_BUILD = _RUNNER / "build-images.sh"

_GIT_ENV = {
    "PATH": os.environ["PATH"],
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


def _git(checkout: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=checkout, env=_GIT_ENV, capture_output=True, check=True)


@pytest.fixture
def machine(tmp_path: Path) -> Path:
    """A folder with a stand-in `docker` that writes down each call, and the repository's main
    checkout, whose `origin/main` is its first commit."""
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    docker = bin_folder / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f"echo \"docker $*\" >> '{tmp_path}/calls.log'\n"
        f"[[ ! -e '{tmp_path}/fail' ]]\n"
    )
    docker.chmod(0o755)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "--initial-branch=main")
    return tmp_path


def _main(machine: Path, files: dict[str, str]) -> None:
    """Commit `files` as main, as last fetched from origin."""
    checkout = machine / "checkout"
    for name, text in files.items():
        (checkout / name).write_text(text)
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "--allow-empty", "-m", "main")
    _git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")


def _build(
    machine: Path, *, kvm: bool, prefix: str = "example-app"
) -> subprocess.CompletedProcess[str]:
    if kvm:
        (machine / "kvm").touch()
    env = {
        **_GIT_ENV,
        "PATH": f"{machine / 'bin'}:{os.environ['PATH']}",
        "RUNNER_PREFIX": prefix,
        "RUNNER_KVM": str(machine / "kvm"),
        "BUILT": str(machine / "built"),
    }
    return subprocess.run(
        ["bash", str(_BUILD)],
        cwd=machine / "checkout",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(machine: Path) -> list[str]:
    log = machine / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _config(command: str) -> str:
    return json.dumps({"runner": {"prefix": "example-app", "image_build": command}})


def test_with_kvm_the_pool_builds_its_ordinary_image_then_its_docker_image(machine: Path) -> None:
    _main(machine, {})

    done = _build(machine, kvm=True)

    assert done.returncode == 0, done.stderr
    assert _calls(machine) == [
        f"docker build --pull -t example-app-runner {_RUNNER}",
        f"docker build --pull -t example-app-runner-docker {_RUNNER / 'vm'}",
    ]


def test_without_kvm_only_the_ordinary_image_is_built(machine: Path) -> None:
    _main(machine, {"shipline.json": json.dumps({"runner": {"prefix": "example-app"}})})

    done = _build(machine, kvm=False)

    assert done.returncode == 0, done.stderr
    assert _calls(machine) == [f"docker build --pull -t example-app-runner {_RUNNER}"]


def test_a_failed_build_stops_and_says_so(machine: Path) -> None:
    _main(machine, {})
    (machine / "fail").touch()

    done = _build(machine, kvm=True)

    assert done.returncode != 0
    assert _calls(machine) == [f"docker build --pull -t example-app-runner {_RUNNER}"]


def test_the_repository_s_own_command_builds_them_from_main_s_files(machine: Path) -> None:
    command = (
        'printf "%s %s\\n" "$RUNNER_PREFIX" "$(cat marker)" > "$BUILT"; '
        'echo "one word" >> "$BUILT"'
    )
    _main(machine, {"shipline.json": _config(command), "marker": "main's\n"})

    done = _build(machine, kvm=True)

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text().splitlines() == ["example-app main's", "one word"]
    assert _calls(machine) == []


def test_what_the_checkout_holds_beside_main_never_runs(machine: Path) -> None:
    """The checkout may hold another branch, a fork's pull request among them: the build still
    takes main's command and main's files, and leaves the checkout as it was."""
    _main(machine, {"shipline.json": _config('cat marker > "$BUILT"'), "marker": "main's\n"})
    checkout = machine / "checkout"
    (checkout / "shipline.json").write_text(_config('echo fork > "$BUILT"; touch fork-ran'))
    (checkout / "marker").write_text("the fork's\n")

    done = _build(machine, kvm=False)

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text() == "main's\n"
    assert not (checkout / "fork-ran").exists()
    assert (checkout / "marker").read_text() == "the fork's\n"


def test_the_repository_s_failing_command_fails_the_build(machine: Path) -> None:
    _main(machine, {"shipline.json": _config("exit 3")})

    done = _build(machine, kvm=False)

    assert done.returncode == 3


def test_without_main_nothing_is_built(machine: Path) -> None:
    done = _build(machine, kvm=True)

    assert done.returncode != 0
    assert "origin/main" in done.stderr
    assert _calls(machine) == []


@pytest.mark.parametrize("prefix", ["", "Example", "a b", "a;b"])
def test_a_prefix_that_cannot_name_an_image_is_refused(machine: Path, prefix: str) -> None:
    _main(machine, {})

    done = _build(machine, kvm=True, prefix=prefix)

    assert done.returncode != 0
    assert "RUNNER_PREFIX" in done.stderr
    assert _calls(machine) == []
