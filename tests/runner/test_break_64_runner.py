"""Breaker tests for #64: a runner pool installed from a config kept outside the checkout.

The installation keeps no `shipline.json` in its repository. Its config is the home file
`~/.config/shipline/<owner>-<name>/shipline.json`, the repository is named by both its GitHub
`origin` and `.github/runner/settings.sh`, and the commands it names are programs of the
repository, given by relative path. `systemctl`, `docker`, `loginctl` and `gh` are stand-ins."""

import json
import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_INSTALL = _ROOT / "runner/install.sh"
_LOGGED = '#!/bin/sh\necho "{name} $*" >> "$CALLS"\n'
_SETTINGS = "machine=home\nroles='main ordinary planner'\nother_machines=office\nordinary_jobs=2\n"
_BUILD = '#!/bin/sh\necho "{who} $*" >> "$BUILT"\n'


@pytest.fixture
def machine(tmp_path: Path) -> Path:
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
    (checkout / ".github/runner").mkdir(parents=True)
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/example-org/example-app.git")
    (checkout / ".github/runner/settings.sh").write_text("repository=example-org/example-app\n")
    build = checkout / "scripts/build-image"
    build.parent.mkdir()
    build.write_text(_BUILD.format(who="main"))
    build.chmod(0o755)
    _git(tmp_path, "add", "--all")
    _git(tmp_path, "commit", "--quiet", "-m", "main")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    # The working tree holds another branch's program: only main's may run with Docker's rights.
    build.write_text(_BUILD.format(who="worktree"))
    path = tmp_path / "home/.config/shipline/runner.sh"
    path.parent.mkdir(parents=True)
    path.write_text(_SETTINGS)
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


def _git(machine: Path, *args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd or machine / "checkout", env=_env(machine),
        capture_output=True, check=True,
    )


def _home_file(machine: Path, runner: dict[str, str] | None = None) -> Path:
    path = machine / "home/.config/shipline/example-org-example-app/shipline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    runner = runner or {"prefix": "example-app", "image_build": "scripts/build-image"}
    path.write_text(json.dumps({"runner": runner}))
    return path


def _installed(machine: Path) -> Path:
    return machine / "home/.local/share/example-app-runner"


def _run(machine: Path, script: Path, cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        ["sh", str(script)], cwd=cwd, env=env or _env(machine),
        capture_output=True, text=True, timeout=60, check=False,
    )


def _built(machine: Path) -> list[str]:
    path = machine / "built"
    return path.read_text().splitlines() if path.exists() else []


def test_the_home_file_installs_and_the_weekly_build_runs_main_s_program(machine: Path) -> None:
    path = _home_file(machine)

    done = _run(machine, _INSTALL, machine / "checkout")

    assert done.returncode == 0, done.stderr
    assert str(path) in done.stdout
    assert _built(machine) == ["main "]
    settings = (_installed(machine) / "pool-settings.sh").read_text()
    assert f"installation_config='{path}'" in settings
    # The weekly timer: a service with no config variables, from no particular folder.
    done = _run(machine, _installed(machine) / "build-images.sh", machine / "home")
    assert done.returncode == 0, done.stderr
    assert _built(machine) == ["main ", "main "]


def test_a_balancer_move_keeps_the_external_config(machine: Path) -> None:
    _home_file(machine)
    assert _run(machine, _INSTALL, machine / "checkout").returncode == 0
    installed = _installed(machine)
    moved = subprocess.run(
        ["python3", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import balance_runners as b; "
         "b.write_count('ordinary', 1)", str(installed)],
        env=_env(machine), capture_output=True, text=True, timeout=30, check=False,
    )
    assert moved.returncode == 0, moved.stderr
    assert "ordinary_jobs=1" in (installed / "pool-settings.sh").read_text()

    done = _run(machine, installed / "build-images.sh", machine / "home")

    assert done.returncode == 0, done.stderr
    assert _built(machine)[-1] == "main "


def test_an_install_from_a_session_worktree_is_the_install_from_main(machine: Path) -> None:
    path = _home_file(machine)
    worktree = machine / "checkout/.claude/worktrees/ticket-7"
    _git(machine, "worktree", "add", "--quiet", "-b", "ticket-7", str(worktree))

    done = _run(machine, _INSTALL, worktree)

    assert done.returncode == 0, done.stderr
    assert str(path) in done.stdout
    settings = (_installed(machine) / "pool-settings.sh").read_text()
    assert f"checkout='{machine / 'checkout'}'" in settings
    assert _built(machine) == ["main "]


def test_the_owner_s_edit_of_the_home_file_reaches_the_next_build(machine: Path) -> None:
    path = _home_file(machine)
    assert _run(machine, _INSTALL, machine / "checkout").returncode == 0
    _home_file(machine, {"prefix": "example-app", "image_build": "scripts/build-image again"})

    done = _run(machine, _installed(machine) / "build-images.sh", machine / "home")

    assert done.returncode == 0, done.stderr
    assert _built(machine)[-1] == "main again"
    assert path.exists()


def test_a_home_file_removed_after_install_is_named_when_the_build_fails(machine: Path) -> None:
    path = _home_file(machine)
    assert _run(machine, _INSTALL, machine / "checkout").returncode == 0
    path.unlink()

    done = _run(machine, _installed(machine) / "build-images.sh", machine / "home")

    assert done.returncode != 0
    assert str(path) in done.stderr


def test_moving_back_to_a_checkout_file_drops_the_recorded_external_one(machine: Path) -> None:
    path = _home_file(machine)
    assert _run(machine, _INSTALL, machine / "checkout").returncode == 0
    path.unlink()
    checkout_file = machine / "checkout/shipline.json"
    checkout_file.write_text(json.dumps({"runner": {"prefix": "example-app",
                                                    "image_build": "scripts/build-image checkout"}}))
    (machine / "checkout/scripts/build-image").write_text(_BUILD.format(who="main"))
    _git(machine, "add", "--all")
    _git(machine, "commit", "--quiet", "-m", "checkout config")
    _git(machine, "update-ref", "refs/remotes/origin/main", "HEAD")

    done = _run(machine, _INSTALL, machine / "checkout")

    assert done.returncode == 0, done.stderr
    assert "installation_config" not in (_installed(machine) / "pool-settings.sh").read_text()
    assert _built(machine)[-1] == "main checkout"


def test_an_explicit_name_of_the_checkout_file_through_a_link_is_still_checked(
    machine: Path,
) -> None:
    """Low, not reached in use: the installation keeps no checkout file. A checkout file named
    through a linked folder is the checkout's file, yet it is taken as external: no freshness
    check, and later builds read the working tree's file, not main's."""
    checkout_file = machine / "checkout/shipline.json"
    checkout_file.write_text(json.dumps({"runner": {"prefix": "example-app",
                                                    "image_build": "scripts/build-image"}}))
    _git(machine, "add", "--all")
    _git(machine, "commit", "--quiet", "-m", "checkout config")
    _git(machine, "update-ref", "refs/remotes/origin/main", "HEAD")
    # A branch's change to the checkout file, not on main.
    checkout_file.write_text(json.dumps({"runner": {"prefix": "example-app",
                                                    "image_build": "echo branch >> \"$BUILT\""}}))
    link = machine / "linked"
    link.symlink_to(machine / "checkout")
    env = {**_env(machine), "SHIPLINE_CONFIG": str(link / "shipline.json")}

    done = _run(machine, _INSTALL, machine / "checkout", env)

    # The working tree's command must not run as main's: the build sees main's or refuses.
    assert "branch" not in _built(machine), _built(machine)
    assert done.returncode != 0, done.stdout
    assert "is not the one of origin/main" in done.stderr
