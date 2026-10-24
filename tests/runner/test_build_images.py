"""The repository builds the runner images and runs the Docker Hub cache with commands of its own,
`runner.image_build` and `runner.hub_cache` of its shipline.json, and Shipline's
`runner/build-images.sh` and `runner/hub-cache.sh` run them: the install and a weekly timer the
first, the cache's units the second. Both the command and its files are main's as last fetched,
never what the checkout holds."""

import json
import os
import subprocess
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[2] / "runner"

_GIT_ENV = {
    "PATH": os.environ["PATH"],
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
    """A home folder and the repository's main checkout, with no main yet."""
    (tmp_path / "home").mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "--initial-branch=main")
    return tmp_path


def _main(machine: Path, runner: dict[str, str], files: dict[str, str] | None = None) -> None:
    """Commit a shipline.json naming `runner`, and `files`, as main, as last fetched from origin."""
    checkout = machine / "checkout"
    config = {"runner": {"prefix": "example-app", **runner}}
    for name, text in {"shipline.json": json.dumps(config), **(files or {})}.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "-m", "main")
    _git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")


def _run(
    machine: Path, script: str, *args: str, prefix: str = "example-app"
) -> subprocess.CompletedProcess[str]:
    env = {
        **_GIT_ENV,
        "HOME": str(machine / "home"),
        "RUNNER_PREFIX": prefix,
        "BUILT": str(machine / "built"),
    }
    return subprocess.run(
        ["sh", str(_RUNNER / script), *args],
        cwd=machine / "checkout",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


_SHOW = 'printf "%s|%s|%s|%s\\n" "$RUNNER_PREFIX" "$RUNNER_FILES" "$(cat marker)" "$*" >> "$BUILT"'


def test_the_repository_s_image_command_builds_them_on_main_s_files(machine: Path) -> None:
    _main(machine, {"image_build": _SHOW}, {"marker": "main's"})

    done = _run(machine, "build-images.sh")

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text() == f"example-app|{_RUNNER}|main's|\n"


def test_what_the_checkout_holds_beside_main_never_runs(machine: Path) -> None:
    """The checkout may hold another branch, a fork's pull request among them: the build still
    takes main's command and main's files, and leaves the checkout as it was."""
    _main(machine, {"image_build": 'cat marker > "$BUILT"'}, {"marker": "main's\n"})
    checkout = machine / "checkout"
    fork = {"runner": {"prefix": "example-app", "image_build": 'echo fork > "$BUILT"; touch ran'}}
    (checkout / "shipline.json").write_text(json.dumps(fork))
    (checkout / "marker").write_text("the fork's\n")

    done = _run(machine, "build-images.sh")

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text() == "main's\n"
    assert not (checkout / "ran").exists()
    assert (checkout / "marker").read_text() == "the fork's\n"


def test_a_failing_image_command_fails_the_build(machine: Path) -> None:
    _main(machine, {"image_build": "exit 3"})

    assert _run(machine, "build-images.sh").returncode == 3


def test_without_an_image_command_nothing_is_built_and_it_says_so(machine: Path) -> None:
    _main(machine, {})

    done = _run(machine, "build-images.sh")

    assert done.returncode != 0
    assert "runner.image_build" in done.stderr


def test_without_main_nothing_runs(machine: Path) -> None:
    done = _run(machine, "build-images.sh")

    assert done.returncode != 0
    assert "origin/main" in done.stderr


def test_the_hub_cache_command_runs_on_main_s_files_with_its_arguments(machine: Path) -> None:
    show = (
        'printf "%s %s %s %s:%s %s\\n" "$*" "$HUB_CACHE_CONTAINER" "$HUB_CACHE_NETWORK" '
        '"$HUB_CACHE_ADDRESS" "$HUB_CACHE_PORT" "$(cat marker)" >> "$BUILT"\n'
    )
    # The arguments go after the command line, as `commands.test`'s do.
    _main(machine, {"hub_cache": "sh tools/cache"}, {"marker": "main's", "tools/cache": show})

    assert _run(machine, "hub-cache.sh").returncode == 0
    done = _run(machine, "hub-cache.sh", "watch")

    assert done.returncode == 0, done.stderr
    assert (machine / "built").read_text().splitlines() == [
        " example-app-hub-cache example-app-hub-cache 172.31.255.2:5000 main's",
        "watch example-app-hub-cache example-app-hub-cache 172.31.255.2:5000 main's",
    ]


def test_the_watch_leaves_the_running_cache_s_files_alone(machine: Path) -> None:
    """The cache runs for days from its folder; the watch every 10 minutes takes one of its own,
    and a folder whose main has not moved is not taken again."""
    _main(machine, {"hub_cache": "true"})
    folders = machine / "home/.local/state/example-app-runner/main"
    assert _run(machine, "hub-cache.sh").returncode == 0
    (folders / "hub_cache/kept").touch()

    assert _run(machine, "hub-cache.sh", "watch").returncode == 0
    assert _run(machine, "hub-cache.sh").returncode == 0

    assert (folders / "hub_cache/kept").exists()
    assert (folders / "hub_cache-watch/shipline.json").exists()


def test_without_a_hub_cache_command_there_is_no_cache(machine: Path) -> None:
    _main(machine, {})

    done = _run(machine, "hub-cache.sh")

    assert done.returncode != 0
    assert "runner.hub_cache" in done.stderr


@pytest.mark.parametrize("prefix", ["", "Example", "a b", "a;b"])
def test_a_prefix_that_cannot_name_an_image_is_refused(machine: Path, prefix: str) -> None:
    _main(machine, {"image_build": 'touch "$BUILT"'})

    done = _run(machine, "build-images.sh", prefix=prefix)

    assert done.returncode != 0
    assert "prefix" in done.stderr
    assert not (machine / "built").exists()
