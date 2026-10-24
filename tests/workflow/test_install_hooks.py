"""`install-hooks` adds the hooks that call `notify` to the owner's Claude settings."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow, install_shipline



@pytest.fixture
def workflow(workflow: Workflow) -> Workflow:
    """Installed from a Shipline checkout of its own, as a repository installs it."""
    install_shipline(workflow)
    return workflow


def _settings_file(workflow: Workflow) -> Path:
    return workflow.root / "home" / ".claude" / "settings.json"


def _settings(workflow: Workflow) -> dict[str, Any]:
    read: dict[str, Any] = json.loads(_settings_file(workflow).read_text())
    return read


def _write_settings(workflow: Workflow, text: str) -> None:
    _settings_file(workflow).parent.mkdir(exist_ok=True)
    _settings_file(workflow).write_text(text)


def _run_hook(
    workflow: Workflow, command: str, session: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a hook's command the way Claude does: through a shell, the event on standard input."""
    named = {} if session is None else {"WORKFLOW_SESSION": session}
    return subprocess.run(
        ["bash", "-c", command],
        cwd=workflow.repo,
        env={**workflow.env, **named},
        input="{}\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _commands(settings: dict[str, Any], event: str) -> list[str]:
    return [hook["command"] for entry in settings["hooks"][event] for hook in entry["hooks"]]


def test_the_hooks_are_added_to_settings_that_do_not_exist_yet(workflow: Workflow) -> None:
    done = workflow.run_script("install-hooks")

    assert done.returncode == 0, done.stderr
    hooks = _settings(workflow)["hooks"]
    assert set(hooks) == {"Stop", "UserPromptSubmit", "Notification", "PreToolUse"}
    assert hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion"
    assert "matcher" not in hooks["Stop"][0]
    assert "matcher" not in hooks["UserPromptSubmit"][0]
    assert hooks["Notification"][0]["matcher"] == "permission_prompt"


def test_each_hook_calls_notify_on_the_main_checkout_with_ten_seconds(workflow: Workflow) -> None:
    workflow.run_script("install-hooks")

    assert workflow.home
    launcher = workflow.home / "bin" / "shipline"
    for entries in _settings(workflow)["hooks"].values():
        [hook] = entries[0]["hooks"]
        assert hook["type"] == "command"
        assert hook["timeout"] == 10
        command = f"bash {launcher} --repo {workflow.repo} notify 2>/dev/null || true"
        assert hook["command"] == command


def test_installed_from_a_worktree_the_hooks_still_name_the_main_checkout(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7)

    done = workflow.shipline("install-hooks", cwd=worktree)

    assert done.returncode == 0, done.stderr
    [command] = _commands(_settings(workflow), "Stop")
    assert f" --repo {workflow.repo} notify " in command
    assert str(worktree) not in command


def test_a_hook_written_when_the_scripts_were_in_the_repository_is_replaced_by_ours(
    workflow: Workflow,
) -> None:
    held = f"bash {workflow.repo}/scripts/workflow/notify 2>/dev/null || true"
    entry = {"hooks": [{"type": "command", "command": held, "timeout": 10}]}
    _write_settings(workflow, json.dumps({"hooks": {"Stop": [entry]}}))

    workflow.run_script("install-hooks")

    [command] = _commands(_settings(workflow), "Stop")
    assert command.endswith(f" --repo {workflow.repo} notify 2>/dev/null || true")
    assert "scripts/workflow" not in command


def test_a_hook_that_calls_a_shipline_checkout_that_moved_is_pointed_at_this_one(
    workflow: Workflow,
) -> None:
    assert workflow.home
    workflow.run_script("install-hooks")
    installed = _settings_file(workflow).read_text()
    _write_settings(workflow, installed.replace(str(workflow.home), "/where/shipline/was"))

    done = workflow.run_script("install-hooks")

    assert done.returncode == 0, done.stderr
    assert _settings_file(workflow).read_text() == installed


def test_the_installed_command_runs_notify_and_never_fails_the_session(workflow: Workflow) -> None:
    workflow.run_script("install-hooks")
    [command] = _commands(_settings(workflow), "Stop")
    notify = workflow.script.parent / "notify"
    notify.write_text(
        'cat > "$HOME/received"\necho "$WORKFLOW_SESSION" >> "$HOME/received"\nexit 3\n'
    )

    done = _run_hook(workflow, command, session="ticket-7")

    assert done.returncode == 0
    assert (workflow.root / "home" / "received").read_text() == "{}\nticket-7\n"


def test_the_installed_command_is_quiet_when_the_script_is_not_there(workflow: Workflow) -> None:
    workflow.run_script("install-hooks")
    [command] = _commands(_settings(workflow), "Stop")
    (workflow.script.parent / "notify").unlink()

    done = _run_hook(workflow, command)

    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr == ""


def test_every_other_setting_and_hook_is_left_as_it_is(workflow: Workflow) -> None:
    before: dict[str, Any] = {
        "model": "opus",
        "permissions": {"allow": ["Bash(git status)"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "validator"}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
            "Notification": [
                {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": "beep"}]}
            ],
        },
        "theme": "dark",
    }
    _write_settings(workflow, json.dumps(before, indent=4))

    done = workflow.run_script("install-hooks")

    assert done.returncode == 0, done.stderr
    after = _settings(workflow)
    assert list(after) == list(before)
    assert {key: after[key] for key in ("model", "permissions", "theme")} == {
        key: before[key] for key in ("model", "permissions", "theme")
    }
    assert after["hooks"]["PreToolUse"][0] == before["hooks"]["PreToolUse"][0]
    assert len(after["hooks"]["PreToolUse"]) == 2
    assert after["hooks"]["Stop"][0] == before["hooks"]["Stop"][0]
    assert after["hooks"]["Notification"][0] == before["hooks"]["Notification"][0]
    assert len(after["hooks"]["Stop"]) == 2
    assert len(after["hooks"]["Notification"]) == 2
    assert len(after["hooks"]["UserPromptSubmit"]) == 1


def test_a_second_run_adds_nothing(workflow: Workflow) -> None:
    workflow.run_script("install-hooks")
    first = _settings_file(workflow).read_text()

    done = workflow.run_script("install-hooks")

    assert done.returncode == 0
    assert _settings_file(workflow).read_text() == first
    assert "already" in done.stdout


def test_only_the_missing_hook_is_added(workflow: Workflow) -> None:
    workflow.run_script("install-hooks")
    settings = _settings(workflow)
    del settings["hooks"]["UserPromptSubmit"]
    _write_settings(workflow, json.dumps(settings))

    workflow.run_script("install-hooks")

    after = _settings(workflow)
    assert len(after["hooks"]["Stop"]) == 1
    assert len(after["hooks"]["Notification"]) == 1
    assert len(after["hooks"]["UserPromptSubmit"]) == 1


def test_a_notify_hook_for_another_notification_does_not_count_as_installed(
    workflow: Workflow,
) -> None:
    workflow.run_script("install-hooks")
    settings = _settings(workflow)
    settings["hooks"]["Notification"][0]["matcher"] = "idle_prompt"
    _write_settings(workflow, json.dumps(settings))

    workflow.run_script("install-hooks")

    matchers = [entry["matcher"] for entry in _settings(workflow)["hooks"]["Notification"]]
    assert matchers == ["idle_prompt", "permission_prompt"]


def test_the_settings_before_the_change_are_kept_beside_the_file(workflow: Workflow) -> None:
    _write_settings(workflow, '{"model": "opus"}\n')

    workflow.run_script("install-hooks")

    backup = _settings_file(workflow).with_name("settings.json.before-workflow-hooks")
    assert backup.read_text() == '{"model": "opus"}\n'


def test_settings_that_cannot_be_read_are_refused_and_left_alone(workflow: Workflow) -> None:
    _write_settings(workflow, '{"model": "opus",\n')

    done = workflow.run_script("install-hooks")

    assert done.returncode != 0
    assert "settings.json" in done.stderr
    assert _settings_file(workflow).read_text() == '{"model": "opus",\n'


def test_settings_that_are_not_an_object_are_refused(workflow: Workflow) -> None:
    _write_settings(workflow, "[]\n")

    done = workflow.run_script("install-hooks")

    assert done.returncode != 0
    assert _settings_file(workflow).read_text() == "[]\n"


def test_the_file_keeps_its_own_indentation(workflow: Workflow) -> None:
    _write_settings(workflow, '{\n    "model": "opus",\n    "env": {\n        "A": "1"\n    }\n}\n')

    workflow.run_script("install-hooks")

    text = _settings_file(workflow).read_text()
    assert text.startswith('{\n    "model": "opus",\n    "env": {\n        "A": "1"\n    },\n')


def test_a_checkout_in_a_folder_with_a_space_gets_a_command_that_runs(workflow: Workflow) -> None:
    assert workflow.home
    moved = workflow.root / "my code" / "repo"
    moved.parent.mkdir()
    workflow.repo.rename(moved)
    workflow.repo = moved
    shipline = workflow.root / "my code" / "shipline"
    workflow.home.rename(shipline)
    workflow.home = shipline
    workflow.run_script("install-hooks")
    [command] = _commands(_settings(workflow), "Stop")
    (workflow.script.parent / "notify").write_text('cat > "$HOME/received"\n')

    done = _run_hook(workflow, command, session="ticket-7")

    assert done.returncode == 0
    assert (workflow.root / "home" / "received").read_text() == "{}\n"


def test_a_hook_that_only_has_notify_in_its_name_is_not_taken_for_ours(workflow: Workflow) -> None:
    other = {"hooks": [{"type": "command", "command": "notify-send"}]}
    _write_settings(workflow, json.dumps({"hooks": {"Stop": [other]}}))

    workflow.run_script("install-hooks")

    commands = _commands(_settings(workflow), "Stop")
    assert commands[0] == "notify-send"
    assert len(commands) == 2


def test_a_hook_that_calls_notify_in_a_checkout_that_moved_is_pointed_at_this_one(
    workflow: Workflow,
) -> None:
    workflow.run_script("install-hooks")
    installed = _settings_file(workflow).read_text()
    _write_settings(workflow, installed.replace(str(workflow.repo), "/where/the/checkout/was"))

    done = workflow.run_script("install-hooks")

    assert done.returncode == 0, done.stderr
    assert _settings_file(workflow).read_text() == installed


def test_a_settings_file_that_is_a_link_stays_a_link(workflow: Workflow) -> None:
    kept = workflow.root / "dotfiles" / "settings.json"
    kept.parent.mkdir()
    kept.write_text('{\n  "model": "opus"\n}\n')
    _settings_file(workflow).parent.mkdir(exist_ok=True)
    _settings_file(workflow).symlink_to(kept)

    workflow.run_script("install-hooks")

    assert _settings_file(workflow).is_symlink()
    hooks = json.loads(kept.read_text())["hooks"]
    assert set(hooks) == {"Stop", "UserPromptSubmit", "Notification", "PreToolUse"}


def test_the_first_copy_of_the_settings_is_kept_when_it_runs_again(workflow: Workflow) -> None:
    original = '{\n  "model": "opus"\n}\n'
    _write_settings(workflow, original)
    workflow.run_script("install-hooks")
    changed = _settings(workflow)
    del changed["hooks"]["Stop"]
    _write_settings(workflow, json.dumps(changed, indent=2) + "\n")

    workflow.run_script("install-hooks")

    backup = _settings_file(workflow).with_name("settings.json.before-workflow-hooks")
    assert backup.read_text() == original
    assert "Stop" in _settings(workflow)["hooks"]


def test_a_command_that_only_resembles_ours_is_left_as_it_is(workflow: Workflow) -> None:
    others = [
        "/other/repo/scripts/workflow/notify-slack --channel x",
        "cd /old && scripts/workflow/notify; say done",
    ]
    entry = {"hooks": [{"type": "command", "command": command} for command in others]}
    _write_settings(workflow, json.dumps({"hooks": {"Stop": [entry]}}))

    workflow.run_script("install-hooks")

    commands = _commands(_settings(workflow), "Stop")
    assert commands[:2] == others
    assert len(commands) == 3


def test_another_hook_beside_ours_from_a_moved_checkout_is_left_as_it_is(
    workflow: Workflow,
) -> None:
    workflow.run_script("install-hooks")
    ours = _commands(_settings(workflow), "Stop")[0]
    moved = ours.replace(str(workflow.repo), "/where/the/checkout/was")
    entry = {"hooks": [{"type": "command", "command": c} for c in ("say done", moved)]}
    _write_settings(workflow, json.dumps({"hooks": {"Stop": [entry]}}))

    workflow.run_script("install-hooks")

    assert _commands(_settings(workflow), "Stop") == ["say done", ours]


def test_our_hook_is_known_again_when_the_checkout_path_has_a_shell_character(
    workflow: Workflow,
) -> None:
    workflow.run_script("install-hooks")
    installed = _settings(workflow)
    for entries in installed["hooks"].values():
        command = entries[0]["hooks"][0]["command"]
        entries[0]["hooks"][0]["command"] = command.replace(str(workflow.repo), r"/old/a\&b\;c\|d")
    _write_settings(workflow, json.dumps(installed, indent=2) + "\n")

    workflow.run_script("install-hooks")

    for event in ("Stop", "UserPromptSubmit", "Notification", "PreToolUse"):
        [command] = _commands(_settings(workflow), event)
        assert str(workflow.repo) in command


def test_a_hook_of_the_owner_before_another_tool_is_left_beside_ours(workflow: Workflow) -> None:
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "say bash"}]}
    _write_settings(workflow, json.dumps({"hooks": {"PreToolUse": [other]}}))

    workflow.run_script("install-hooks")

    entries = _settings(workflow)["hooks"]["PreToolUse"]
    assert entries[0] == other
    assert [entry["matcher"] for entry in entries] == ["Bash", "AskUserQuestion"]


def test_a_command_that_holds_ours_among_others_is_left_as_it_is(workflow: Workflow) -> None:
    workflow.run_script("install-hooks")
    ours = _commands(_settings(workflow), "Stop")[0]
    moved = ours.replace(str(workflow.repo), "/old")
    others = [
        f"{moved}; say done",
        f"cd /x && {moved}",
        f"bash /old/scripts/workflow/notify && say done; {moved}",
    ]
    entry = {"hooks": [{"type": "command", "command": command} for command in others]}
    _write_settings(workflow, json.dumps({"hooks": {"Stop": [entry]}}))

    workflow.run_script("install-hooks")

    assert _commands(_settings(workflow), "Stop") == [*others, ours]


def test_the_hooks_are_added_to_a_settings_file_of_a_thousand_lines(workflow: Workflow) -> None:
    """Reading the indentation must not depend on how much of the file follows its first line.

    Every allowed command is a line indented like a key, so the lines the installer looks at
    are more than a pipe takes in one piece.
    """
    allowed = [f"Bash(tool-{number} --with an argument:*)" for number in range(1100)]
    before = {"model": "opus", "permissions": {"allow": allowed}}
    _write_settings(workflow, json.dumps(before, indent=2) + "\n")

    done = workflow.run_script("install-hooks")

    assert done.returncode == 0, done.stderr
    assert "added the workflow hooks" in done.stdout
    after = _settings(workflow)
    assert set(after["hooks"]) == {"Stop", "UserPromptSubmit", "Notification", "PreToolUse"}
    assert after["permissions"] == before["permissions"]
    assert _settings_file(workflow).read_text().startswith('{\n  "model": "opus",\n')
