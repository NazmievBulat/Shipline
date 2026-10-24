"""`runner/build-images.sh` builds the runner images: the install runs it, and so does a weekly
timer, because GitHub refuses runners that fall far behind. The repository's own command
(`runner.image_build`) builds them when it names one; else the pool builds its own Dockerfile,
and its virtual machine's only on a machine with /dev/kvm."""

import os
import subprocess
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[2] / "runner"
_BUILD = _RUNNER / "build-images.sh"


@pytest.fixture
def machine(tmp_path: Path) -> Path:
    """A folder with a stand-in `docker` that writes down each call, and a checkout to run in."""
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    docker = bin_folder / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f"echo \"docker $*\" >> '{tmp_path}/calls.log'\n"
        f"[[ ! -e '{tmp_path}/fail' ]]\n"
    )
    docker.chmod(0o755)
    (tmp_path / "checkout").mkdir()
    return tmp_path


def _build(
    machine: Path, *, kvm: bool, prefix: str = "example-app", command: str | None = None
) -> subprocess.CompletedProcess[str]:
    if kvm:
        (machine / "kvm").touch()
    env = {
        "PATH": f"{machine / 'bin'}:{os.environ['PATH']}",
        "HOME": "/nonexistent",
        "RUNNER_PREFIX": prefix,
        "RUNNER_KVM": str(machine / "kvm"),
    }
    if command is not None:
        env["SHIPLINE_RUNNER_IMAGE_BUILD"] = command
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


def test_with_kvm_the_pool_builds_its_ordinary_image_then_its_docker_image(machine: Path) -> None:
    done = _build(machine, kvm=True)

    assert done.returncode == 0, done.stderr
    assert _calls(machine) == [
        f"docker build --pull -t example-app-runner {_RUNNER}",
        f"docker build --pull -t example-app-runner-docker {_RUNNER / 'vm'}",
    ]


def test_without_kvm_only_the_ordinary_image_is_built(machine: Path) -> None:
    done = _build(machine, kvm=False)

    assert done.returncode == 0, done.stderr
    assert _calls(machine) == [f"docker build --pull -t example-app-runner {_RUNNER}"]


def test_a_failed_build_stops_and_says_so(machine: Path) -> None:
    (machine / "fail").touch()

    done = _build(machine, kvm=True)

    assert done.returncode != 0
    assert _calls(machine) == [f"docker build --pull -t example-app-runner {_RUNNER}"]


def test_the_repository_s_own_command_builds_them_from_its_checkout(machine: Path) -> None:
    command = "printf '%s %s\\n' \"$RUNNER_PREFIX\" \"$PWD\" > built; echo 'one word' >> built"

    done = _build(machine, kvm=True, command=command)

    assert done.returncode == 0, done.stderr
    built = (machine / "checkout/built").read_text().splitlines()
    assert built == [f"example-app {machine / 'checkout'}", "one word"]
    assert _calls(machine) == []


def test_the_repository_s_failing_command_fails_the_build(machine: Path) -> None:
    done = _build(machine, kvm=False, command="exit 3")

    assert done.returncode == 3


@pytest.mark.parametrize("prefix", ["", "Example", "a b", "a;b"])
def test_a_prefix_that_cannot_name_an_image_is_refused(machine: Path, prefix: str) -> None:
    done = _build(machine, kvm=True, prefix=prefix)

    assert done.returncode != 0
    assert "RUNNER_PREFIX" in done.stderr
    assert _calls(machine) == []
