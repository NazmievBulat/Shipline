"""Shipline runs from a checkout of its own, kept apart from the repository it works on, which
calls it as `shipline <command>` and keeps none of its scripts. Every script finds its own files
beside itself; the repository is the main checkout `--repo` or `SHIPLINE_REPO` names, else the
one the current directory is in."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from test_merge_when_current import GitHub, github  # noqa: F401  (the gate's stand-in)
from workflow_harness import Workflow, install_shipline, instruction, session_arguments

_SHIPLINE = Path(__file__).resolve().parents[2]
_WORKFLOW = _SHIPLINE / "scripts" / "workflow"

_GIT_ENV = {
    "PATH": os.environ["PATH"],
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _repository(tmp_path: Path, name: str = "repo", state_folder: str = ".local/shipline") -> Path:
    """A repository with no `scripts/workflow` of its own, its notes in a folder of its own."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "remote", "add", "origin", f"https://github.com/example-owner/{name}.git")
    (repo / "shipline.json").write_text(f'{{"state_folder": "{state_folder}"}}\n')
    _git(repo, "add", "shipline.json")
    _git(repo, "commit", "--quiet", "-m", "chore: start")
    return repo


def _config(cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """What a Bash script of the Shipline checkout reads, called from `cwd`."""
    return subprocess.run(
        [
            "bash",
            "-c",
            'config=$(python3 "$1/workflow_config.py" shell) || exit 1; eval "$config"; '
            'printf "%s\\n" "$SHIPLINE_REPOSITORY" "$SHIPLINE_STATE_FOLDER" "$SHIPLINE_REPO"; '
            'bash -c \'printf "%s\\n" "$SHIPLINE_REPO"\'',
            "bash",
            str(_WORKFLOW),
        ],
        cwd=cwd,
        env={**_GIT_ENV, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_repository_is_the_main_checkout_shipline_repo_names(tmp_path: Path) -> None:
    repo = _repository(tmp_path, state_folder=".local/named")
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(worktree))
    elsewhere = _repository(tmp_path, "elsewhere")

    # Named by one of its worktrees, from inside another repository.
    done = _config(elsewhere, {"SHIPLINE_REPO": str(worktree)})

    assert done.returncode == 0, done.stderr
    # The scripts' own checkout is not the repository; programs started after it read the same.
    assert done.stdout.splitlines() == ["example-owner/repo", ".local/named", str(repo), str(repo)]


def test_without_a_name_the_repository_is_the_one_of_the_current_directory(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, state_folder=".local/here")
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(worktree))

    done = _config(worktree)

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["example-owner/repo", ".local/here", str(repo), str(repo)]


def test_outside_any_repository_with_none_named_it_says_how_to_name_one(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    done = _config(outside)

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (
        f"{outside} is in no git repository: run Shipline inside the checkout of the repository "
        "it works on, or name that checkout with SHIPLINE_REPO or `shipline --repo <path>`.\n"
    )


def test_a_named_folder_that_is_no_repository_is_refused_by_its_name(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    missing = tmp_path / "missing"

    done = _config(repo, {"SHIPLINE_REPO": str(missing)})

    assert done.returncode == 1
    assert done.stderr.startswith(f"SHIPLINE_REPO names {missing}, which is in no git repository")


@pytest.fixture
def workflow(workflow: Workflow) -> Workflow:
    """The harness's repository with Shipline kept apart, in a checkout of its own; the gate's
    stand-in for GitHub (`github`) is set up on it too."""
    install_shipline(workflow)
    return workflow


def _environment(workflow: Workflow, name: str) -> dict[str, str]:
    """The environment a session's `tmux` was started with."""
    lines = (workflow.state / "env" / name).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_a_repository_without_the_scripts_is_ticked_and_its_ticket_gets_a_session(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)

    # As the timer runs it: the repository named, from a folder that is in Shipline's checkout.
    done = workflow.shipline("tick", env={"SHIPLINE_REPO": str(workflow.repo)}, cwd=workflow.home)

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    arguments = session_arguments(workflow.state, "ticket-7")
    assert arguments[-1].endswith(f'"{instruction("Implement pull request 7")}"')
    assert arguments[arguments.index("-c") + 1] == str(workflow.worktree(7))
    assert not (workflow.worktree(7) / "scripts" / "workflow").exists()
    # The session's commands and hooks reach the same checkout and the same repository.
    environment = _environment(workflow, "ticket-7")
    assert environment["SHIPLINE_REPO"] == str(workflow.repo)
    assert environment["PATH"].split(":")[:2] == [
        str(workflow.home / "bin"),
        str(workflow.home / "scripts" / "workflow" / "gh-bin"),
    ]
    # The notes are the repository's; Shipline's checkout holds none.
    assert (workflow.repo / ".local" / "workflow").is_dir()
    assert _git(workflow.home, "status", "--porcelain", "--ignored") == ""


def test_a_repository_without_the_scripts_passes_the_gate(github: GitHub) -> None:  # noqa: F811
    workflow = github.workflow
    assert not (workflow.repo / "scripts" / "workflow").exists()
    github.save()

    # As a reviewer calls it from its worktree: the repository named in its session.
    done = workflow.shipline("merge-when-current", "7", env={"SHIPLINE_REPO": str(workflow.repo)})

    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert (workflow.repo / ".local" / "workflow").is_dir()
    assert _git(workflow.home, "status", "--porcelain", "--ignored") == ""


def _gate(workflow: Workflow, code: str) -> subprocess.CompletedProcess[str]:
    """Runs `code` with Shipline's gate imported as `g`, in the repository SHIPLINE_REPO names,
    as a session runs it in its worktree; it prints what it found."""
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g\n"
            + textwrap.dedent(code),
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env={**workflow.env, "SHIPLINE_REPO": str(workflow.repo)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_gate_reads_the_files_a_repository_keeps_for_it_from_that_repository(
    workflow: Workflow,
) -> None:
    calls = workflow.state / "calls.log"
    kept = workflow.repo / "scripts" / "workflow"
    kept.mkdir()
    (kept / "pre-train-checks").write_text(f'echo repository-checks >> "{calls}"\nexit 1\n')
    (kept / "mechanical_conflicts.py").write_text(
        "class MechanicalConflict(Exception):\n    pass\n"
        "def taken_migrations(head, base):\n    return ['migrations/0002_taken.py']\n"
    )
    # Beside the gate, in Shipline's checkout, is never the repository's.
    (workflow.script.parent / "pre-train-checks").write_text(
        f'echo shipline-checks >> "{calls}"\nexit 0\n'
    )
    (workflow.repo / "app.txt").write_text("changed\n")
    workflow.git("add", "app.txt")
    workflow.git("commit", "--quiet", "-m", "feat: an app change")

    done = _gate(
        workflow,
        """
        from pathlib import Path
        folder = g.main_checkout() / g.CONFIG.state_folder
        folder.mkdir(parents=True, exist_ok=True)
        print(folder)
        print(g._taken_migrations("HEAD", "HEAD"))
        print("needed" if g.browser_needed("HEAD~1", "HEAD") else "none")
        try:
            g.pre_train_checks("7", folder)
        except g.Refused as error:
            print("refused:", error)
        """,
    )

    assert done.stdout.splitlines() == [
        str(workflow.repo / ".local" / "workflow"),
        "['migrations/0002_taken.py']",
        # The repository's E2E planner, in its `scripts`, says an app change needs the browser.
        "needed",
        "refused: pre-train checks failed; fix the inventory before joining the merge train",
    ], done.stderr
    assert workflow.calls() == ["repository-checks"]


def _checkout(folder: Path) -> Path:
    """A Shipline checkout whose every command prints the folder it runs in, the repository it
    is given, and its own name and arguments."""
    scripts = folder / "scripts" / "workflow"
    scripts.mkdir(parents=True)
    for name in _launched_commands():
        (scripts / name).write_text(
            f'printf "%s\\n" "$PWD" "${{SHIPLINE_REPO-unset}}" "{name} $*"\n'
        )
    (folder / "bin").mkdir()
    launcher = folder / "bin" / "shipline"
    launcher.write_bytes((_SHIPLINE / "bin" / "shipline").read_bytes())
    launcher.chmod(0o755)
    return folder


def _launched_commands() -> list[str]:
    text = (_SHIPLINE / "bin" / "shipline").read_text()
    listed = text.split("COMMANDS=(\n", 1)[1].split(")", 1)[0]
    return listed.split()


def _launch(
    launcher: Path, *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(launcher), *args],
        cwd=cwd,
        env={**_GIT_ENV, "HOME": str(cwd), **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_launcher_runs_each_command_of_its_own_checkout(tmp_path: Path) -> None:
    home = _checkout(tmp_path / "shipline")
    repo = _repository(tmp_path)

    for name in _launched_commands():
        done = _launch(home / "bin" / "shipline", name, "7", "--x", cwd=repo)

        assert done.returncode == 0, done.stderr
        # Nothing names the repository: the scripts take the current folder's.
        assert done.stdout.splitlines() == [str(repo), "unset", f"{name} 7 --x"]


def test_the_launcher_refuses_what_is_not_a_command(tmp_path: Path) -> None:
    home = _checkout(tmp_path / "shipline")
    (home / "scripts" / "workflow" / "merge_gate.py").write_text("")
    repo = _repository(tmp_path)

    for args in ([], ["merge_gate.py"], ["limit-reset"], ["../bin/shipline"], ["--repo"]):
        done = _launch(home / "bin" / "shipline", *args, cwd=repo)

        assert done.returncode == 2, args
        assert done.stdout == ""
        assert "usage: shipline [--repo <path>] <command> [arguments]" in done.stderr


def test_the_repository_named_is_passed_on_and_the_command_runs_in_it(tmp_path: Path) -> None:
    home = _checkout(tmp_path / "shipline")
    repo = _repository(tmp_path)
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(worktree))
    launcher = home / "bin" / "shipline"

    # From a folder outside the repository, it runs in the repository's main checkout.
    named = _launch(launcher, "--repo", "repo", "tick", cwd=tmp_path)
    given = _launch(launcher, "tick", cwd=home, env={"SHIPLINE_REPO": str(worktree)})
    # In one of the repository's checkouts, it stays there.
    inside = _launch(launcher, f"--repo={repo}", "notify", "waiting", cwd=worktree)

    assert named.stdout.splitlines() == [str(repo), str(repo), "tick "]
    assert given.stdout.splitlines() == [str(repo), str(worktree), "tick "]
    assert inside.stdout.splitlines() == [str(worktree), str(repo), "notify waiting"]


def test_a_link_runs_the_checkout_it_points_into_and_a_copy_runs_shipline_home(
    tmp_path: Path,
) -> None:
    home = _checkout(tmp_path / "home" / ".local" / "share" / "shipline")
    other = _checkout(tmp_path / "other")
    repo = _repository(tmp_path)
    links = tmp_path / "links"
    links.mkdir()
    (links / "shipline").symlink_to(other / "bin" / "shipline")
    copy = tmp_path / "copy" / "shipline"
    copy.parent.mkdir()
    copy.write_bytes((home / "bin" / "shipline").read_bytes())
    copy.chmod(0o755)
    (other / "scripts" / "workflow" / "usage").write_text("echo other\n")
    (home / "scripts" / "workflow" / "usage").write_text("echo default\n")
    named = _checkout(tmp_path / "named")
    (named / "scripts" / "workflow" / "usage").write_text("echo named\n")
    env = {"HOME": str(tmp_path / "home")}

    assert _launch(links / "shipline", "usage", cwd=repo, env=env).stdout == "other\n"
    assert _launch(copy, "usage", cwd=repo, env=env).stdout == "default\n"
    named_home = {**env, "SHIPLINE_HOME": str(named)}
    assert _launch(copy, "usage", cwd=repo, env=named_home).stdout == "named\n"
    # SHIPLINE_HOME never moves a launcher that is in a checkout of its own.
    assert _launch(links / "shipline", "usage", cwd=repo, env=named_home).stdout == "other\n"
    missing = _launch(copy, "usage", cwd=repo, env={"HOME": str(tmp_path / "nobody")})
    assert missing.returncode == 1
    assert missing.stderr == (
        f"no Shipline checkout at {tmp_path}/nobody/.local/share/shipline: clone Shipline "
        "there, or name its checkout with SHIPLINE_HOME\n"
    )
