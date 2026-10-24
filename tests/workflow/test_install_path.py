"""Shipline runs from a checkout of its own, kept apart from the repository it works on, which
calls it as `shipline <command>` and keeps none of its scripts. Every script finds its own files
beside itself; the repository is the main checkout `--repo` or `SHIPLINE_REPO` names, else the
one the current directory is in."""

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from workflow.test_conflict_review import (  # pyright: ignore[reportPrivateUsage]
    _folder,
    _panel_conflict,
    _resolve,
)
from workflow.test_merge_when_current import (  # pyright: ignore[reportPrivateUsage]
    GitHub,
    _two_stale_pulls,
)
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow, install_shipline, instruction, session_arguments

# The gate's stand-in for GitHub, on the repository with Shipline kept apart (`workflow` below).
github = github_fixture

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


def test_a_repository_without_the_scripts_passes_the_gate(github: GitHub) -> None:
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


def test_the_gate_runs_the_commands_a_repository_names_from_that_repository(
    workflow: Workflow,
) -> None:
    calls = workflow.state / "calls.log"
    programs = {
        "pre-train-checks": f'echo repository-checks >> "{calls}"\nexit 1\n',
        "conflicts": 'echo migrations/0002_taken.py\n',
        "browser-plan": "echo needed\n",
    }
    kept = workflow.repo / "tools" / "shipline"
    kept.mkdir(parents=True)
    for name, body in programs.items():
        # The program runs from the test's bin folder, which allows running programs; the
        # repository holds a link to it where its `shipline.json` names it.
        workflow.fake(f"repository-{name}", "#!/bin/bash\n" + body)
        (kept / name).symlink_to(workflow.bin / f"repository-{name}")
    # The same path in Shipline's checkout is never the repository's.
    (workflow.home / "tools" / "shipline").mkdir(parents=True)
    workflow.fake("shipline-checks", f'#!/bin/bash\necho shipline-checks >> "{calls}"\nexit 0\n')
    (workflow.home / "tools" / "shipline" / "pre-train-checks").symlink_to(
        workflow.bin / "shipline-checks"
    )
    config = json.loads((workflow.repo / "shipline.json").read_text())
    config["commands"] = {
        "pre_train_checks": "tools/shipline/pre-train-checks",
        "mechanical_conflicts": "tools/shipline/conflicts",
        "browser_plan": "tools/shipline/browser-plan",
    }
    (workflow.repo / "shipline.json").write_text(json.dumps(config))
    (workflow.repo / "app.txt").write_text("changed\n")
    workflow.git("add", "-A")
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


def _configured_commands() -> list[str]:
    """The repository's own commands `shipline` runs by the names its `shipline.json` gives."""
    text = (_SHIPLINE / "bin" / "shipline").read_text()
    return text.split("CONFIGURED=(\n", 1)[1].split(")", 1)[0].split()


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


def test_install_hooks_and_install_timer_write_the_checkout_s_paths_and_the_repository_s(
    workflow: Workflow,
) -> None:
    fake_home = Path(workflow.env["HOME"])
    worktree = workflow.add_worktree(7)
    launcher = workflow.home / "bin" / "shipline"

    # From one of the repository's worktrees, and from outside it with the repository named.
    hooks = workflow.shipline("install-hooks", cwd=worktree)
    timer = workflow.shipline("--repo", str(workflow.repo), "install-timer", cwd=workflow.root)

    assert hooks.returncode == 0, hooks.stderr
    assert timer.returncode == 0, timer.stderr
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text())
    command = f"bash {launcher} --repo {workflow.repo} notify 2>/dev/null || true"
    assert {
        event: [hook["command"] for entry in entries for hook in entry["hooks"]]
        for event, entries in settings["hooks"].items()
    } == {event: [command] for event in ("Stop", "UserPromptSubmit", "Notification", "PreToolUse")}
    service = (fake_home / ".config" / "systemd" / "user" / "workflow-tick.service").read_text()
    lines = service.splitlines()
    assert f"WorkingDirectory={workflow.repo}" in lines
    assert f'Environment="SHIPLINE_REPO={workflow.repo}"' in lines
    assert f'ExecStart="{launcher}" tick' in lines
    assert str(worktree) not in service
    link = fake_home / ".local" / "bin" / "shipline"
    assert link.is_symlink()
    assert link.resolve() == launcher.resolve()

    # The hook a session runs reaches the checkout's `notify`, on the repository named.
    (workflow.script.parent / "notify").write_text(
        f'printf "%s\\n" "$PWD" "$SHIPLINE_REPO" "$*" > "{workflow.state}/notified"\n'
    )
    done = subprocess.run(
        ["bash", "-c", command],
        cwd=worktree,
        env=workflow.env,
        input="{}\n",
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert (workflow.state / "notified").read_text().splitlines() == [
        str(worktree),
        str(workflow.repo),
        "",
    ]


def test_the_link_is_pointed_at_this_checkout_and_a_file_of_the_owner_is_left_alone(
    workflow: Workflow,
) -> None:
    link = Path(workflow.env["HOME"]) / ".local" / "bin" / "shipline"
    link.parent.mkdir(parents=True)
    link.symlink_to("/where/shipline/was/bin/shipline")

    done = workflow.shipline("install-hooks")

    assert done.returncode == 0, done.stderr
    assert link.resolve() == (workflow.home / "bin" / "shipline").resolve()

    link.unlink()
    link.write_text("#!/bin/sh\necho mine\n")

    done = workflow.shipline("install-hooks")

    assert done.returncode == 0, done.stderr
    assert link.read_text() == "#!/bin/sh\necho mine\n"
    assert f"{link} is not a link to Shipline; it was left as it is" in done.stderr


# The SHA-256 of each sentence that names a command, as the scripts wrote it when the scripts
# were in the repository and the sentence named the command by its path there.
_SENTENCES_BEFORE = {
    "status check": "3e8d5f12c6bdb570b59ceddfe8f2125e3fdade3efa60145fdfab708ddd9aa3e7",
    "train wait": "f0a253050d62873459e22be5ac48a72322b68bf89e4b5a1d1463e5d0c480e8c9",
}
# What changed in them: the command's path became the launcher's absolute path, which a session
# started before the switch runs without Shipline's `bin` on its PATH.
_COMMANDS = {"notify waiting"}


def _as_before(sentence: str, launcher: Path) -> str:
    for command in _COMMANDS:
        sentence = sentence.replace(f"{launcher} {command}", f"scripts/workflow/{command}")
    return sentence


def _status_check(workflow: Workflow) -> str:
    """The sentence a session waiting for its own job is asked whether the wait is over with."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    box = "─" * 40 + "\n> \n" + "─" * 40 + "\n"
    workflow.screen("● Waiting for the full Python run in the background.\n" + box, "ticket-7")
    workflow.notes(7).mkdir(parents=True, exist_ok=True)
    (workflow.notes(7) / "ticket-7.idle").write_text("\n")
    done = workflow.shipline("session", "status-check", "ticket-7")
    assert done.returncode == 0, done.stderr
    prefix = "tmux ticket-7 send-keys -t =ticket-7: -l "
    [typed] = [call.removeprefix(prefix) for call in workflow.calls(prefix)]
    return typed


def _train_wait(github: GitHub) -> str:
    """The line the gate tells a pull request behind the train's first with."""
    second = _two_stale_pulls(github)
    assert github.run().returncode == 2
    waiting = github.workflow.shipline("merge-when-current", "8", cwd=second)
    assert waiting.returncode == 3, waiting.stdout + waiting.stderr
    [line] = [line for line in waiting.stdout.splitlines() if "waits behind" in line]
    return line


def test_each_sentence_is_as_before_with_only_the_command_paths_changed(
    github: GitHub,
) -> None:
    sentences = {"status check": _status_check(github.workflow), "train wait": _train_wait(github)}

    launcher = github.workflow.home / "bin" / "shipline"
    for name, sentence in sentences.items():
        assert f"{launcher} notify waiting" in sentence, name
        assert "scripts/workflow" not in sentence, name
    assert {
        name: hashlib.sha256(_as_before(sentence, launcher).encode()).hexdigest()
        for name, sentence in sentences.items()
    } == _SENTENCES_BEFORE


def test_the_rules_name_each_command_as_shipline_command() -> None:
    commands = _launched_commands()
    for document in ("RULES.md", "weaker-tests.md"):
        text = (_WORKFLOW / document).read_text()

        # A command by its path; `scripts/workflow/ticket_starts.py` names a file, not `tick`.
        by_path = [
            name for name in commands if re.search(rf"scripts/workflow/{name}(?![\w.-])", text)
        ]
        assert by_path == [], document
        assert "git-common-dir" not in text, document
        named = set(re.findall(r"shipline ([a-z-]+)", text))
        assert named <= set(commands) | set(_configured_commands()), document
    rules = (_WORKFLOW / "RULES.md").read_text()
    assert set(re.findall(r"`shipline ([a-z-]+)", rules)) == {
        "browser-tests-for-changes",
        "install-hooks",
        "install-timer",
        "merge-when-current",
        "notify",
        "pre-train-checks",
        "review-panel",
        "session",
        "tick",
        "ticket-files",
        "wake",
        "weaker-tests",
    }


def test_a_codex_worker_reaches_the_same_checkout_and_repository(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    done = workflow.shipline("session", "start", "worker", "7", "medium", "codex")

    assert done.returncode == 0, done.stderr
    environment = _environment(workflow, "codex-7")
    assert environment["SHIPLINE_REPO"] == str(workflow.repo)
    # Its guarded `gh` first, then `shipline`.
    assert environment["PATH"].split(":")[:2] == [
        str(workflow.home / "scripts" / "workflow" / "codex-bin"),
        str(workflow.home / "bin"),
    ]


def test_notify_and_ticket_files_work_on_the_repository_from_shipline_s_checkout(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7)
    (workflow.state / "body-7").write_text("Change it.\n\n## Files\n\n- `app.txt`\n")

    # As a session runs them in its worktree, the repository in its environment.
    named = {"SHIPLINE_REPO": str(workflow.repo), "WORKFLOW_SESSION": "ticket-7"}
    waiting = workflow.shipline("notify", "waiting", env=named, cwd=worktree)
    files = workflow.shipline("ticket-files", "7", cwd=worktree)

    assert waiting.returncode == 0, waiting.stderr
    assert (workflow.notes(7) / "ticket-7.waiting").exists()
    assert files.returncode == 0, files.stderr
    assert files.stdout == "app.txt\n"
    assert (workflow.repo / ".local" / "workflow" / "files-7").read_text() == "app.txt\n"
    assert _git(workflow.home, "status", "--porcelain", "--ignored") == ""


def test_a_conflict_review_in_a_repository_without_test_selectors_selects_none(
    workflow: Workflow,
) -> None:
    _panel_conflict(workflow)
    resolved = _resolve(workflow)
    workflow.set_pull_request(7, "OPEN", resolved)

    done = workflow.shipline("review-panel", "7")

    assert done.returncode == 0, done.stdout + done.stderr
    evidence = _folder(workflow) / "reviews" / "7" / resolved
    assert json.loads((evidence / "server-tests.json").read_text()) == {
        "whole": True,
        "tests": [],
        "total": 0,
        "reasons": ["no server test selection is configured"],
    }
    assert (evidence / "browser-tests.txt").read_text() == ""
    assert (evidence / "task.md").read_text().endswith(
        "\nNo browser test selection is configured: CI runs the whole browser suite.\n"
    )
