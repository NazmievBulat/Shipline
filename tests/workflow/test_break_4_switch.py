"""Breaking tests for pull request 4: a repository that carries the scripts in its own tree
switches to a separate Shipline checkout, and back.

The repository keeps its own copy of the scripts throughout, as it does until it is moved onto
`shipline <command>`: sessions started before the switch still call that copy by its path, and
going back is running that copy's installers again. `break_4_before` holds the installers of
`main` before this change."""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from workflow_harness import LAUNCHER, Workflow

BEFORE = Path(__file__).resolve().parent / "break_4_before"
EVENTS = ("Stop", "UserPromptSubmit", "Notification", "PreToolUse")


def _install_beside(workflow: Workflow) -> Path:
    """Shipline's checkout apart from the repository, the repository keeping its own copy of the
    scripts, whose installers are those of `main` before this change."""
    home = workflow.root / "shipline"
    held = workflow.repo / "scripts" / "workflow"
    shutil.copytree(held, home / "scripts" / "workflow", symlinks=True)
    (home / "bin").mkdir()
    shutil.copy2(LAUNCHER, home / "bin" / "shipline")
    env = {**workflow.env, "GIT_DIR": str(home / ".git"), "GIT_WORK_TREE": str(home)}
    for args in (
        ["init", "--quiet", "--initial-branch=main"],
        ["add", "-A"],
        ["commit", "--quiet", "-m", "chore: shipline"],
    ):
        subprocess.run(["git", *args], cwd=home, env=env, capture_output=True, check=True)
    for name in ("install-hooks", "install-timer"):
        shutil.copy2(BEFORE / name, held / name)
    (held / "launcher-link").unlink(missing_ok=True)
    workflow.home = home
    return home


def _old(workflow: Workflow, script: str) -> subprocess.CompletedProcess[str]:
    """One of the repository's own scripts, run by the owner in its main checkout."""
    return subprocess.run(
        ["bash", str(workflow.repo / "scripts" / "workflow" / script)],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _settings(workflow: Workflow) -> dict:
    return json.loads((Path(workflow.env["HOME"]) / ".claude" / "settings.json").read_text())


def _hooks(workflow: Workflow) -> dict[str, list[str]]:
    return {
        event: [hook["command"] for entry in entries for hook in entry["hooks"]]
        for event, entries in _settings(workflow)["hooks"].items()
    }


def _owner_settings(workflow: Workflow) -> None:
    """The owner's settings carry a hook and a setting of their own beside the workflow's."""
    path = Path(workflow.env["HOME"]) / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "model": "example-model",
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true # mine"}]}]},
            },
            indent=2,
        )
        + "\n"
    )


def _phone(workflow: Workflow, number: int = 7) -> Path:
    """Pull request `number` is a draft with its worktree, its session link is saved and the
    owner's phone keys are there, as `test_notify` sets them."""
    worktree = workflow.add_worktree(number)
    workflow.set_draft(number, draft=True)
    workflow.notes(number).mkdir(parents=True, exist_ok=True)
    (workflow.notes(number) / f"ticket-{number}.url").write_text(
        "https://example.com/session-link\n"
    )
    keys = Path(workflow.env["HOME"]) / "agents" / ".env"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("PUSHOVER_USER=exampleUser\nPUSHOVER_TOKEN=exampleToken\n")
    return worktree


@pytest.fixture
def switched(workflow: Workflow) -> Workflow:
    _install_beside(workflow)
    return workflow


def test_the_switch_replaces_each_old_hook_and_keeps_the_owner_s(switched: Workflow) -> None:
    _owner_settings(switched)
    assert _old(switched, "install-hooks").returncode == 0
    old = f"bash {switched.repo}/scripts/workflow/notify 2>/dev/null || true"
    assert _hooks(switched) == {
        "Stop": ["true # mine", old],
        **{event: [old] for event in EVENTS[1:]},
    }

    done = switched.shipline("install-hooks")

    assert done.returncode == 0, done.stderr
    new = (
        f"bash {switched.home}/bin/shipline --repo {switched.repo} notify 2>/dev/null || true"
    )
    assert _hooks(switched) == {
        "Stop": ["true # mine", new],
        **{event: [new] for event in EVENTS[1:]},
    }
    assert _settings(switched)["model"] == "example-model"


def test_going_back_with_the_old_installer_leaves_one_hook_per_event(switched: Workflow) -> None:
    """Going back is one step: the repository's own `install-hooks` points the hooks at its
    scripts again. Afterwards each event must still run `notify` once."""
    assert _old(switched, "install-hooks").returncode == 0
    assert switched.shipline("install-hooks").returncode == 0

    back = _old(switched, "install-hooks")

    assert back.returncode == 0, back.stderr
    old = f"bash {switched.repo}/scripts/workflow/notify 2>/dev/null || true"
    assert _hooks(switched) == {event: [old] for event in EVENTS}


def test_after_going_back_a_permission_prompt_rings_the_phone_once(switched: Workflow) -> None:
    """What the owner's phone gets from one permission prompt once the hooks were pointed back
    with the repository's own installer: every hook command of the event runs, as Claude runs
    them."""
    worktree = _phone(switched)
    assert _old(switched, "install-hooks").returncode == 0
    assert switched.shipline("install-hooks").returncode == 0
    assert _old(switched, "install-hooks").returncode == 0

    event = {
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": "Claude needs your permission to use Bash",
    }
    for command in _hooks(switched)["Notification"]:
        done = subprocess.run(
            ["bash", "-c", command],
            cwd=worktree,
            env={**switched.env, "WORKFLOW_SESSION": "ticket-7"},
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert done.returncode == 0

    assert len(switched.pushes()) == 1


def test_going_back_with_the_old_timer_installer_is_one_step(switched: Workflow) -> None:
    units = Path(switched.env["HOME"]) / ".config" / "systemd" / "user"
    assert _old(switched, "install-timer").returncode == 0
    before = (units / "workflow-tick.service").read_text()

    assert switched.shipline("install-timer").returncode == 0
    assert f'ExecStart="{switched.home}/bin/shipline" tick' in (
        units / "workflow-tick.service"
    ).read_text().splitlines()
    back = _old(switched, "install-timer")

    assert back.returncode == 0, back.stderr
    assert (units / "workflow-tick.service").read_text() == before


def _unit(workflow: Workflow) -> tuple[Path, dict[str, str], list[str]]:
    """The folder, environment and command systemd runs the tick's service with."""
    service = (
        Path(workflow.env["HOME"]) / ".config" / "systemd" / "user" / "workflow-tick.service"
    ).read_text()
    folder = Path(re.search(r"^WorkingDirectory=(.*)$", service, re.M)[1].replace("%%", "%"))
    env = {"HOME": workflow.env["HOME"]}
    for value in re.findall(r'^Environment="([^"]*)"$', service, re.M):
        key, _, setting = value.partition("=")
        env[key] = setting.replace("%%", "%")
    [start] = re.findall(r"^ExecStart=(.*)$", service, re.M)
    match = re.fullmatch(r'"([^"]*)" (\w+)', start)
    assert match, start
    return folder, env, [match[1].replace("$$", "$"), match[2]]


def _systemd_env(workflow: Workflow, env: dict[str, str]) -> dict[str, str]:
    """systemd's environment for a user service: the unit's lines, and nothing of the shell the
    owner installed it from but what the harness needs for its stand-ins."""
    keep = {
        key: workflow.env[key]
        for key in workflow.env
        if key.startswith(("WORKFLOW_", "GIT_", "XDG_")) or key in ("USER", "TMPDIR", "LANG")
    }
    return {**keep, **env}


def test_the_timer_ticks_as_systemd_starts_it_and_keeps_a_running_session(
    switched: Workflow,
) -> None:
    """A session started before the switch keeps running; the tick of Shipline's checkout starts
    the waiting ticket in the repository's worktree folder, with the repository's notes."""
    switched.add_worktree(7)
    switched.running("ticket-7")
    switched.add_pull_request(8)
    assert switched.shipline("install-timer").returncode == 0
    folder, env, command = _unit(switched)

    done = subprocess.run(
        command,
        cwd=folder,
        env=_systemd_env(switched, env),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert switched.is_running("ticket-7")
    assert switched.is_running("ticket-8")
    assert switched.calls("tmux ticket-7 kill") == []
    assert (switched.worktree(8)).is_dir()
    # The session runs in a scope of the same name as before, and reaches Shipline's commands.
    assert any("--unit=workflow-ticket-8-" in call for call in switched.calls())
    lines = (switched.state / "env" / "ticket-8").read_text().splitlines()
    environment = dict(line.split("=", 1) for line in lines if "=" in line)
    assert environment["SHIPLINE_REPO"] == str(switched.repo)
    assert environment["PATH"].startswith(f"{switched.home}/bin:")
    assert str(switched.worktree(8)).startswith(str(switched.repo / ".claude" / "worktrees"))


def test_a_tick_of_the_old_copy_still_running_keeps_the_new_one_out(switched: Workflow) -> None:
    """The switch happens while the repository's own tick is in the middle of a run: it holds the
    tick's lock. The first tick of Shipline's checkout must leave, not run beside it."""
    switched.add_pull_request(8)
    assert switched.shipline("install-timer").returncode == 0
    folder, env, command = _unit(switched)
    common = switched.git("rev-parse", "--path-format=absolute", "--git-common-dir")
    holder = subprocess.Popen(
        ["flock", str(Path(common) / "shipline-tick.lock"), shutil.which("sleep", path="/usr/bin:/bin"), "30"],
        env=switched.env,
    )
    try:
        time.sleep(0.5)
        done = subprocess.run(
            command,
            cwd=folder,
            env=_systemd_env(switched, env),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        holder.kill()
        holder.wait()

    assert done.returncode == 0, done.stdout + done.stderr
    assert not switched.is_running("ticket-8")


def _old_session_env(workflow: Workflow, path: str) -> dict[str, str]:
    """A session the repository's own tick started before the switch: its `gh` guard first on
    the PATH the tick had, and no `SHIPLINE_REPO`."""
    env = {**workflow.env, "WORKFLOW_SESSION": "ticket-7"}
    env.pop("SHIPLINE_REPO", None)
    env["PATH"] = f"{workflow.repo}/scripts/workflow/gh-bin:{path}"
    return env


def _status_check_command(workflow: Workflow) -> str:
    """The command the tick of Shipline's checkout tells a waiting session to run."""
    workflow.running("ticket-7")
    box = "─" * 40 + "\n> \n" + "─" * 40 + "\n"
    workflow.screen("● Waiting for the full Python run in the background.\n" + box, "ticket-7")
    workflow.notes(7).mkdir(parents=True, exist_ok=True)
    (workflow.notes(7) / "ticket-7.idle").write_text("\n")
    done = workflow.shipline("session", "status-check", "ticket-7")
    assert done.returncode == 0, done.stderr
    prefix = "tmux ticket-7 send-keys -t =ticket-7: -l "
    [typed] = [call.removeprefix(prefix) for call in workflow.calls(prefix)]
    [command] = re.findall(r"`([^`]*)`", typed)
    return command


@pytest.mark.parametrize("local_bin_on_path", [True, False])
def test_a_session_started_before_the_switch_can_run_what_the_status_check_tells_it(
    switched: Workflow, local_bin_on_path: bool
) -> None:
    """The tick of Shipline's checkout asks every waiting session, old ones too, to run
    `shipline notify waiting`. A session started by the repository's own tick has neither
    Shipline's `bin` nor `SHIPLINE_REPO`: it finds `shipline` only through the link
    `~/.local/bin/shipline`, when that folder was on the PATH the old timer was installed with."""
    worktree = switched.add_worktree(7)
    assert switched.shipline("install-hooks").returncode == 0
    command = _status_check_command(switched)
    assert command == "shipline notify waiting"
    local_bin = Path(switched.env["HOME"]) / ".local" / "bin"
    path = switched.env["PATH"]
    if local_bin_on_path:
        path = f"{local_bin}:{path}"

    done = subprocess.run(
        ["bash", "-c", command],
        cwd=worktree,
        env=_old_session_env(switched, path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert (switched.notes(7) / "ticket-7.job-wait").exists() or any(
        switched.notes(7).glob("ticket-7.*wait*")
    )


def test_the_new_hook_rings_the_phone_from_a_worktree_with_the_event_on_stdin(
    switched: Workflow,
) -> None:
    worktree = _phone(switched)
    assert switched.shipline("install-hooks").returncode == 0
    [command] = _hooks(switched)["Stop"]

    done = subprocess.run(
        ["bash", "-c", command],
        cwd=worktree,
        env={**switched.env, "WORKFLOW_SESSION": "ticket-7"},
        input=json.dumps({"hook_event_name": "Stop", "last_assistant_message": "Which one?"}),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0
    assert done.stdout == ""
    [push] = switched.pushes()
    assert push.title == "ticket-7 needs you"
    assert push.message == "Which one?"
    assert (switched.notes(7) / "ticket-7.idle").exists()


@pytest.mark.parametrize("missing", ["checkout", "repository"])
def test_a_hook_whose_checkout_or_repository_is_gone_stays_silent(
    switched: Workflow, missing: str
) -> None:
    worktree = switched.add_worktree(7)
    assert switched.shipline("install-hooks").returncode == 0
    [command] = _hooks(switched)["Stop"]
    if missing == "checkout":
        command = command.replace(str(switched.home), str(switched.root / "moved"))
    else:
        command = command.replace(f"--repo {switched.repo}", f"--repo {switched.root}/moved")

    done = subprocess.run(
        ["bash", "-c", command],
        cwd=worktree,
        env={**switched.env, "WORKFLOW_SESSION": "ticket-7"},
        input=json.dumps({"hook_event_name": "Stop", "last_assistant_message": "Which one?"}),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0
    assert done.stdout == ""


def test_many_hooks_at_once_each_reach_their_own_session(switched: Workflow) -> None:
    """Five sessions stop at the same moment; each is marked idle in its own notes."""
    numbers = range(7, 12)
    worktrees = {number: switched.add_worktree(number) for number in numbers}
    for number in numbers:
        switched.set_draft(number, draft=True)
    assert switched.shipline("install-hooks").returncode == 0
    [command] = _hooks(switched)["Stop"]

    running = [
        subprocess.Popen(
            ["bash", "-c", command],
            cwd=worktrees[number],
            env={**switched.env, "WORKFLOW_SESSION": f"ticket-{number}"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for number in numbers
    ]
    for process in running:
        process.communicate(json.dumps({"hook_event_name": "Stop"}), timeout=30)

    assert all(process.returncode == 0 for process in running)
    for number in numbers:
        assert (switched.notes(number) / f"ticket-{number}.idle").exists(), number


def test_the_files_the_installers_write_are_not_open_to_others(switched: Workflow) -> None:
    mask = os.umask(0o022)
    try:
        assert switched.shipline("install-hooks").returncode == 0
        assert switched.shipline("install-timer").returncode == 0
    finally:
        os.umask(mask)
    home = Path(switched.env["HOME"])
    for path in (
        home / ".claude" / "settings.json",
        home / ".config" / "systemd" / "user" / "workflow-tick.service",
    ):
        assert path.stat().st_mode & 0o022 == 0, path
    link = home / ".local" / "bin" / "shipline"
    assert link.is_symlink()
