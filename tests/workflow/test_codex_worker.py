"""Interactive Codex, its relay bridge and process-scoped notifications."""

import json
import selectors
import shlex
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import SESSION_LINK, Workflow, instruction, scope_words


def _codex(workflow: Workflow, draft: bool = True) -> None:
    workflow.add_worktree(7, draft=draft)
    workflow.notes(7).mkdir(parents=True, exist_ok=True)
    (workflow.notes(7) / "ticket-7.codex").touch()
    workflow.running("ticket-7")
    workflow.running("codex-7")


def _keys(workflow: Workflow) -> None:
    path = workflow.root / "home" / "agents" / ".env"
    path.parent.mkdir()
    path.write_text("PUSHOVER_USER=owner\nPUSHOVER_TOKEN=app\n")


def _notify(
    workflow: Workflow, message: str, bridge: bool = False
) -> subprocess.CompletedProcess[str]:
    payload = json.dumps(
        {"type": "agent-turn-complete", "last-assistant-message": message, "turn-id": message}
    )
    return workflow.run_script(
        "notify",
        "codex",
        payload,
        env={"WORKFLOW_SESSION": "ticket-7", "WORKFLOW_CODEX_BRIDGE": "1" if bridge else "0"},
    )


def _args(workflow: Workflow, name: str) -> list[str]:
    command = (workflow.state / "args" / name).read_text().splitlines()[-1]
    return shlex.split(command)


@pytest.mark.parametrize("custom_state", [False, True])
def test_codex_launch_is_interactive_with_restricted_haiku_bridge(
    workflow: Workflow, custom_state: bool
) -> None:
    workflow.add_pull_request(7)
    state = workflow.root / ("state with spaces" if custom_state else "home/.local/state")
    env = {"XDG_STATE_HOME": str(state)} if custom_state else {}
    done = workflow.run("start", "worker", "7", "high", "codex", env=env)
    assert done.returncode == 0, done.stderr
    assert SESSION_LINK in done.stdout
    codex = _args(workflow, "codex-7")
    assert codex[0] == "codex" and "exec" not in codex
    assert codex[-1] == instruction("Implement pull request 7")
    assert "model_reasoning_effort=high" in codex
    assert "workspace-write" in codex
    grants = [codex[index + 1] for index, arg in enumerate(codex) if arg == "--add-dir"]
    push_queue = state / "workflow/pushes"
    assert str(push_queue) in grants
    assert push_queue.is_dir()
    notify = next(arg for arg in codex if arg.startswith("notify="))
    assert json.loads(notify[7:]) == [
        "env",
        "WORKFLOW_CODEX_BRIDGE=1",
        "bash",
        str(workflow.script.with_name("notify")),
        "codex",
    ]
    bridge = _args(workflow, "ticket-7")
    assert bridge[:5] == ["claude", "--effort", "low", "--model", "claude-haiku-5-5"]
    assert bridge[bridge.index("--tools") + 1] == ""
    assert bridge[bridge.index("--remote-control") + 1] == "ticket-7"
    assert "--strict-mcp-config" in bridge
    assert (
        bridge[bridge.index("--allowedTools") + 1]
        == "mcp__codex_bridge__await_turn,mcp__codex_bridge__reply"
    )
    config = json.loads(Path(bridge[bridge.index("--mcp-config") + 1]).read_text())
    assert list(config["mcpServers"]) == ["codex_bridge"]
    assert "never" in bridge[bridge.index("--system-prompt") + 1].lower()
    env = (workflow.state / "env" / "codex-7").read_text()
    assert "WORKFLOW_SESSION=ticket-7\n" in env
    assert f"XDG_STATE_HOME={state}\n" in env
    assert "WORKFLOW_CODEX_BRIDGE=1\n" not in env
    bridge_env = (workflow.state / "env" / "ticket-7").read_text()
    assert "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0\n" in bridge_env


@pytest.mark.parametrize("setting", ["file", "environment"])
def test_tick_can_choose_codex(workflow: Workflow, setting: str) -> None:
    workflow.add_pull_request(7)
    env: dict[str, str] = {}
    if setting == "environment":
        env["WORKFLOW_WORKER_PROGRAM"] = "codex"
    else:
        path = workflow.repo / ".local" / "workflow"
        path.mkdir(parents=True)
        (path / "worker-program").write_text("codex\n")
    done = workflow.run_script("tick", env=env)
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("codex-7")
    assert workflow.is_running("ticket-7")


def test_bridge_start_failure_stops_codex(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.screen("No link\n", "ticket-7")
    done = workflow.run("start", "worker", "7", "medium", "codex")
    assert done.returncode != 0
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")


def test_finished_bridge_stops_both_and_starts_reviewer(workflow: Workflow) -> None:
    _codex(workflow, draft=False)
    workflow.screen("Worker finished.\n> \n", "ticket-7")
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Worker finished."})
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")
    assert workflow.is_running("review-7")
    assert (workflow.repo / ".local/workflow/codex-built-7").exists()


@pytest.mark.parametrize("lost_bridge", [False, True])
@pytest.mark.parametrize(
    "ending", ["worker finished", "Worker finished.", "**worker finished**", "worker finished  "]
)
def test_recorded_codex_completion_stops_worker_without_bridge_words(
    workflow: Workflow, lost_bridge: bool, ending: str
) -> None:
    _codex(workflow, draft=False)
    _notify(workflow, f"Pushed and ready.\n\n{ending}\n\n", bridge=True)
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Done."})
    if lost_bridge:
        (workflow.state / "tmux" / "ticket-7").unlink()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")
    assert workflow.is_running("review-7")


@pytest.mark.parametrize(
    "line",
    [
        '"worker finished"',
        'worker finished"',
        'Codex says: "worker finished"',
        "“worker finished”",
        "'worker finished'",
    ],
)
def test_quoted_bridge_completion_is_a_fallback(workflow: Workflow, line: str) -> None:
    _codex(workflow, draft=False)
    workflow.screen(f"● {line}\n> \n", "ticket-7")
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": line})

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("codex-7")
    assert workflow.is_running("review-7")


@pytest.mark.parametrize("reason", ["draft", "later-turn", "continued", "answered", "pending"])
def test_old_or_unfinished_codex_completion_does_not_stop_worker(
    workflow: Workflow, reason: str
) -> None:
    _codex(workflow, draft=reason == "draft")
    _notify(workflow, "worker finished", bridge=True)
    workflow.screen('● "worker finished"\n> \n', "ticket-7")
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Done."})
    if reason == "later-turn":
        _notify(workflow, "Which change do you want?", bridge=True)
    elif reason == "continued":
        _notify(workflow, "worker finished\nWhich change do you want?", bridge=True)
    elif reason == "answered":
        notes = workflow.notes(7)
        (notes / "ticket-7.codex-answered").write_bytes(
            (notes / "ticket-7.codex-turn.json").read_bytes()
        )
    elif reason == "pending":
        (workflow.notes(7) / "ticket-7.owner-reply").write_text("One more fix")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")


@pytest.mark.parametrize("answered", [False, True])
def test_lost_bridge_is_started_again_and_shows_the_last_turn(
    workflow: Workflow, answered: bool
) -> None:
    _codex(workflow, draft=False)
    _notify(workflow, "Which change do you want?", bridge=True)
    notes = workflow.notes(7)
    turn = (notes / "ticket-7.codex-turn.json").read_text()
    (notes / "ticket-7.codex-delivered").write_text(turn)
    if answered:
        (notes / "ticket-7.codex-answered").write_text(turn)
    (workflow.state / "tmux" / "ticket-7").unlink()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")
    assert _args(workflow, "ticket-7")[:5] == [
        "claude",
        "--effort",
        "low",
        "--model",
        "claude-haiku-5-5",
    ]
    # An answered turn is not relayed again; an unanswered one is shown once more.
    assert (notes / "ticket-7.codex-delivered").exists() is answered


def test_failed_bridge_restart_leaves_codex_running(workflow: Workflow) -> None:
    _codex(workflow)
    (workflow.state / "tmux" / "ticket-7").unlink()
    workflow.screen("No link\n", "ticket-7")
    done = workflow.run("bridge", "ticket-7")
    assert done.returncode != 0
    assert not workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7")


_PARSE_FAILURE = (
    f"Remote Control: {SESSION_LINK}\n"
    '● <invoke> <parameter name="name">await_turn</parameter>\n'
    "  The model's tool call could not be parsed (retry also failed).\n"
    "────────────────────\n\u276f \n────────────────────\n"
)


@pytest.mark.parametrize("state", ["busy", "idle", "limit"])
def test_codex_completion_closes_a_bridge_in_any_state(workflow: Workflow, state: str) -> None:
    _codex(workflow, draft=False)
    _notify(workflow, "Pushed and ready.\nworker finished", bridge=True)
    workflow.screen(_PARSE_FAILURE, "ticket-7")
    if state == "idle":
        (workflow.notes(7) / "ticket-7.idle").touch()
    elif state == "limit":
        workflow.screen("You've hit your session limit · resets 4pm (UTC)\n> \n", "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")
    assert workflow.is_running("review-7")


@pytest.mark.parametrize("draft", [False, True])
def test_parse_failed_bridge_is_restarted_while_codex_works(
    workflow: Workflow, draft: bool
) -> None:
    _codex(workflow, draft=draft)
    _notify(workflow, "Still testing.", bridge=True)
    workflow.screen(_PARSE_FAILURE, "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")
    assert workflow.calls("tmux ticket-7 kill-server")
    assert _args(workflow, "ticket-7")[:2] == ["claude", "--effort"]
    assert not workflow.calls("tmux codex-7 kill-server")
    assert not workflow.calls("tmux codex-7 send-keys")


@pytest.mark.parametrize(
    "reason", ["newer-reply", "unsent-reply", "submitted-reply", "active-turn", "draft-finished"]
)
def test_failed_bridge_recovery_keeps_current_work_and_owner_input(
    workflow: Workflow, reason: str
) -> None:
    _codex(workflow, draft=reason == "draft-finished")
    _notify(
        workflow, "worker finished" if reason == "draft-finished" else "Still testing.", bridge=True
    )
    screen = _PARSE_FAILURE
    if reason == "newer-reply":
        screen = screen.replace(
            "────────────────────", "● Waiting for Codex.\n────────────────────", 1
        )
    elif reason == "unsent-reply":
        screen = screen.replace("\u276f ", "\u276f My unsent answer")
    elif reason in {"submitted-reply", "active-turn"}:
        workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"})
        newer = "\u276f 1\n"
        if reason == "active-turn":
            newer = "✻ Thinking… (esc to interrupt)\n"
        screen = screen.replace("────────────────────", newer + "────────────────────", 1)
    workflow.screen(screen, "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")
    if reason != "draft-finished":
        assert not workflow.calls("tmux ticket-7 kill-server")
        assert not (workflow.state / "args" / "ticket-7").exists()
    if reason in {"submitted-reply", "active-turn"}:
        assert (workflow.notes(7) / "ticket-7.owner-reply").read_text() == "1"


def test_parse_failed_reply_is_forwarded_once_before_replacing_bridge(workflow: Workflow) -> None:
    _codex(workflow)
    _notify(workflow, "Which option? Reply 1 or 2.", bridge=True)
    answer = "  1; $(literal)\nsecond line\n\n"
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": answer})
    workflow.screen(_PARSE_FAILURE.replace("● <invoke>", "\u276f 1\n● <invoke>"), "ticket-7")
    workflow.screen(_CODEX_WAITING, "codex-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7")
    assert workflow.calls("tmux ticket-7 kill-server")
    assert not (workflow.notes(7) / "ticket-7.owner-reply").exists()
    assert "-l -- " + answer in (workflow.state / "calls.log").read_text()
    assert len(workflow.calls("tmux codex-7 send-keys")) == 2
    assert not workflow.calls("tmux codex-7 kill-server")
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux codex-7 send-keys")) == 2


def test_failed_pending_reply_delivery_is_relayed_without_resending(workflow: Workflow) -> None:
    _codex(workflow)
    _notify(workflow, "Which option? Reply 1 or 2.", bridge=True)
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"})
    workflow.screen(_PARSE_FAILURE, "ticket-7")
    workflow.screen(_CODEX_WAITING, "codex-7")
    (workflow.state / "swallow-enter-codex-7").write_text("2")

    done = workflow.run_script("tick")

    assert done.returncode == 1
    assert "The reply was not sent" in done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7")
    assert workflow.calls("tmux ticket-7 kill-server")
    assert not workflow.calls("tmux codex-7 kill-server")
    assert (workflow.state / "args" / "ticket-7").exists()
    assert not (workflow.notes(7) / "ticket-7.codex-answered").exists()
    failure = workflow.notes(7) / "ticket-7.codex-recovery-error"
    assert "The reply was not sent" in failure.read_text()
    calls = workflow.calls("tmux codex-7 send-keys")
    assert workflow.run_script("tick").returncode == 0
    assert workflow.calls("tmux codex-7 send-keys") == calls
    assert failure.exists()
    with _relay(workflow) as relay:
        turn = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert "The reply was not sent" in turn["result"]["content"][0]["text"]
    assert not failure.exists()


def test_restarted_bridge_shows_a_waiting_permission_panel_again(workflow: Workflow) -> None:
    _codex(workflow)
    notes = workflow.notes(7)
    (notes / "ticket-7.codex-approval-delivered").write_text(
        "Would you like to grant these permissions?"
    )
    (workflow.state / "tmux" / "ticket-7").unlink()
    done = workflow.run("bridge", "ticket-7")
    assert done.returncode == 0, done.stderr
    assert not (notes / "ticket-7.codex-approval-delivered").exists()


def test_failed_bridge_restart_on_codex_limit_resend_leaves_codex_running(
    workflow: Workflow,
) -> None:
    _codex(workflow)
    (workflow.state / "tmux" / "ticket-7").unlink()
    workflow.screen(
        "■ You've hit your usage limit. Try again at Oct 15, 2026 4:00 PM.\n\u203a \n", "codex-7"
    )
    workflow.screen("No link\n", "ticket-7")
    done = workflow.run("resend", "ticket-7")
    assert done.returncode != 0
    assert workflow.is_running("codex-7")
    assert (workflow.notes(7) / "ticket-7.codex").exists()


def test_tick_whose_bridge_restart_fails_keeps_codex_and_starts_no_reviewer(
    workflow: Workflow,
) -> None:
    _codex(workflow, draft=False)
    (workflow.state / "tmux" / "ticket-7").unlink()
    workflow.screen("No link\n", "ticket-7")
    done = workflow.run_script("tick")
    assert done.returncode == 1
    assert workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")


def test_bridge_is_refused_without_a_running_codex_worker(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    done = workflow.run("bridge", "ticket-7")
    assert done.returncode != 0
    assert "no running Codex worker" in done.stderr
    assert workflow.run("bridge", "review-7").returncode == 2


def test_bridged_turn_has_one_phone_push_with_link(workflow: Workflow) -> None:
    _codex(workflow)
    _keys(workflow)
    (workflow.notes(7) / "ticket-7.url").write_text(SESSION_LINK)
    message = 'Recommend option 1. Type "1".'
    _notify(workflow, message, bridge=True)
    assert workflow.pushes() == []
    event = json.loads((workflow.notes(7) / "ticket-7.codex-turn.json").read_text())
    assert event["last-assistant-message"] == message
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": message})
    [push] = workflow.pushes()
    assert push.message == message and push.url == SESSION_LINK


@pytest.mark.parametrize(
    "message",
    [
        "Worker finished.",
        "You've hit your usage limit. Try again at Oct 15, 2026 4:00 PM.",
        "Your usage limit was reached, so I am stopping here.",
    ],
)
def test_codex_completion_or_limit_does_not_ring(workflow: Workflow, message: str) -> None:
    _codex(workflow, draft=False)
    _keys(workflow)
    _notify(workflow, message, bridge=True)
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": message})
    assert workflow.pushes() == []


def test_unbridged_codex_question_does_not_ring(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    _keys(workflow)
    _notify(workflow, "Recommend 1. Type 1.")
    assert workflow.pushes() == []
    assert not (workflow.notes(7) / "ticket-7.idle").exists()


def test_codex_waiting_for_job_is_quiet(workflow: Workflow) -> None:
    _codex(workflow)
    _keys(workflow)
    workflow.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "ticket-7"})
    _notify(workflow, "Tests still run.", bridge=True)
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Tests still run."})
    assert workflow.pushes() == []
    assert not (workflow.notes(7) / "ticket-7.idle").exists()


@pytest.mark.parametrize(
    "payload", ["not JSON", '{"type":"other"}', '{"type":"agent-turn-complete"}']
)
def test_other_codex_payload_does_nothing(workflow: Workflow, payload: str) -> None:
    workflow.add_worktree(7)
    done = workflow.run_script("notify", "codex", payload, env={"WORKFLOW_SESSION": "ticket-7"})
    assert done.returncode == 0
    assert not list(workflow.notes(7).glob("*.idle"))


def test_codex_limit_is_listed_and_resend_goes_to_codex_once(workflow: Workflow) -> None:
    _codex(workflow)
    workflow.screen(
        "■ You've hit your usage limit. Visit settings or try again at "
        "Oct 15th, 2026 4:00 PM.\n\u203a \n",
        "codex-7",
    )
    env = {"WORKFLOW_NOW": "1792080000", "TZ": "UTC"}
    listed = workflow.run("list", env={"TZ": "UTC"})
    assert "limit" in listed.stdout and "Oct 15, 2026, 4:00pm (UTC)" in listed.stdout
    workflow.run_script("tick", env=env)
    workflow.run_script("tick", env=env)
    calls = workflow.calls("tmux codex-7 send-keys")
    assert len(calls) == 2
    assert instruction("Implement pull request 7") in calls[0]
    assert "__WORKFLOW_CODEX_TURN__" in workflow.calls("tmux ticket-7 send-keys")[0]


@pytest.mark.parametrize(
    "screen",
    [
        pytest.param(
            "■ You've hit your usage limit. Visit settings or try again at\n"
            "Oct 15th, 2026 4:00 PM.\n\u203a \n",
            id="wrapped",
        ),
        pytest.param(
            "■ You've hit your usage limit. Try again at 2026-10-15 16:00 UTC.\n\u203a \n",
            id="iso-date",
        ),
    ],
)
def test_codex_limit_reset_is_read_wrapped_or_as_an_iso_date(
    workflow: Workflow, screen: str
) -> None:
    _codex(workflow)
    workflow.screen(screen, "codex-7")
    listed = workflow.run("list", env={"TZ": "UTC"})
    assert "Codex limit  resets Oct 15, 2026, 4:00pm (UTC)" in listed.stdout


def test_codex_without_reset_waits_and_does_not_inherit_claude_usage(workflow: Workflow) -> None:
    _codex(workflow)
    workflow.screen("■ You've hit your usage limit.\n\u203a \n", "codex-7")
    workflow.usage(99, "2026-10-15T16:00:00+00:00")
    workflow.run_script("tick", env={"WORKFLOW_NOW": "1792080000", "TZ": "UTC"})
    assert not workflow.calls("tmux codex-7 send-keys")
    assert "Codex limit" in workflow.run("list").stdout


def test_memory_actions_include_codex_and_bridge(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "ticket-7.codex").touch()
    bridge = workflow.running("ticket-7", held=10 * 1024 * 1024)
    codex = workflow.running("codex-7", held=500 * 1024 * 1024)
    assert "510 MB held" in workflow.run("list").stdout
    assert workflow.run("protect", "ticket-7").returncode == 0
    for group in (bridge, codex):
        assert (group / "memory.low").read_text() == str(4 * 1024**3)
    assert workflow.run("squeeze", "ticket-7").returncode == 0
    assert (codex / "memory.reclaim").read_text() == str(500 * 1024 * 1024)
    workflow.run("unprotect", "ticket-7")
    assert (codex / "memory.low").read_text() == "0"


@contextmanager
def _relay(workflow: Workflow) -> Generator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [
            "node",
            str(workflow.script.with_name("codex-bridge.mjs")),
            str(workflow.worktree(7)),
            "ticket-7",
        ],
        env=workflow.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        process.terminate()
        process.communicate(timeout=5)


def _rpc(
    process: subprocess.Popen[str],
    method: str,
    params: dict[str, object] | None = None,
    timeout: float = 5,
) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}) + "\n"
    )
    process.stdin.flush()
    with selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ)
        assert ready.select(timeout=timeout), "relay did not answer"
    result: dict[str, Any] = json.loads(process.stdout.readline())
    return result


def test_bridge_relays_one_turn_each_way_and_exposes_no_other_actions(workflow: Workflow) -> None:
    _codex(workflow)
    message = "Recommend 1.\nType 1."
    _notify(workflow, message, bridge=True)
    with _relay(workflow) as relay:
        tools = _rpc(relay, "tools/list")["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["await_turn", "reply"]
        turn = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert turn["result"]["content"][0]["text"] == message
        answer = "  1; $(touch nope) `literal`\nsecond line\n\n"
        workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": answer})
        assert "result" in _rpc(relay, "tools/call", {"name": "reply"})
        assert "error" in _rpc(relay, "tools/call", {"name": "run_tests"})
    assert "-l -- " + answer in (workflow.state / "calls.log").read_text()
    assert not (workflow.repo / "nope").exists()


def test_bridge_reports_dead_codex(workflow: Workflow) -> None:
    _codex(workflow)
    (workflow.state / "tmux" / "codex-7").unlink()
    with _relay(workflow) as relay:
        result = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert "ended before another turn" in result["error"]["message"]


def test_installer_refuses_global_codex_option_and_preserves_settings(workflow: Workflow) -> None:
    config = workflow.root / "home" / ".codex" / "config.toml"
    config.parent.mkdir()
    before = 'notify = ["my-notifier"]\nmodel = "gpt-test"\n'
    config.write_text(before)
    done = workflow.run_script("install-hooks", "--codex")
    assert done.returncode == 2
    assert config.read_text() == before
    assert not config.with_name("config.toml.before-workflow-hooks").exists()


def test_default_installer_does_not_change_codex_settings(workflow: Workflow) -> None:
    assert workflow.run_script("install-hooks").returncode == 0
    assert not (workflow.root / "home" / ".codex" / "config.toml").exists()


def test_bridge_limit_turn_is_quiet(workflow: Workflow) -> None:
    _codex(workflow)
    _keys(workflow)
    workflow.notify(
        {
            "hook_event_name": "Stop",
            "last_assistant_message": (
                "You've hit your usage limit. Try again at Oct 15, 2026 4:00 PM."
            ),
        }
    )
    assert workflow.pushes() == []


def test_new_codex_run_clears_the_previous_turn(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "ticket-7.codex-turn.json").write_text(
        '{"last-assistant-message":"worker finished"}'
    )
    done = workflow.run("start", "worker", "7", "medium", "codex")
    assert done.returncode == 0, done.stderr
    assert not (workflow.notes(7) / "ticket-7.codex-turn.json").exists()


def test_stop_also_ends_codex_when_bridge_disappeared(workflow: Workflow) -> None:
    _codex(workflow)
    (workflow.state / "tmux" / "ticket-7").unlink()
    assert workflow.run("list").stdout.startswith("ticket-7  idle")
    done = workflow.run("stop", "ticket-7")
    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("codex-7")


def test_historical_codex_limit_does_not_block_finished_handoff(workflow: Workflow) -> None:
    _codex(workflow, draft=False)
    workflow.screen(
        "■ You've hit your usage limit. Try again at Oct 15th, 2026 4:00 PM.\n"
        "\u203a Continue\n• Finished the work.\n\u203a \n",
        "codex-7",
    )
    workflow.screen("Worker finished.\n> \n", "ticket-7")
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Worker finished."})
    assert "idle" in workflow.run("list").stdout
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
    assert not workflow.is_running("codex-7")


def test_second_codex_turn_wakes_bridge_and_preserves_repeated_words(workflow: Workflow) -> None:
    _codex(workflow)
    first = "Recommend 1. Type 1."
    _notify(workflow, first, bridge=True)
    with _relay(workflow) as relay:
        assert (
            _rpc(relay, "tools/call", {"name": "await_turn"})["result"]["content"][0]["text"]
            == first
        )
        workflow.notify({"hook_event_name": "Stop", "last_assistant_message": first})
        workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"})
        assert "result" in _rpc(relay, "tools/call", {"name": "reply"})
        # The bridge stopped while waiting for its background tool; the next turn wakes it.
        workflow.notify({"hook_event_name": "Stop", "last_assistant_message": first})
        # Same text on a distinct turn must still be delivered.
        payload = json.dumps(
            {"type": "agent-turn-complete", "last-assistant-message": first, "turn-id": "turn-2"}
        )
        workflow.run_script(
            "notify",
            "codex",
            payload,
            env={"WORKFLOW_SESSION": "ticket-7", "WORKFLOW_CODEX_BRIDGE": "1"},
        )
        calls = workflow.calls("tmux ticket-7 send-keys")
        assert len(calls) == 2 and "__WORKFLOW_CODEX_TURN__" in calls[0]
        assert (
            _rpc(relay, "tools/call", {"name": "await_turn"})["result"]["content"][0]["text"]
            == first
        )
        # A historical quota line cannot be returned instead of the next real turn.
        workflow.screen(
            "■ You've hit your usage limit.\n\u203a Continue\n• Done.\n\u203a \n", "codex-7"
        )
        (workflow.state / "tmux" / "codex-7").unlink()
        result = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert "error" in result  # no duplicate of turn-2


def test_quota_delivery_does_not_replay_the_last_completed_turn(workflow: Workflow) -> None:
    _codex(workflow)
    _notify(workflow, "The first question.\n", bridge=True)
    with _relay(workflow) as relay:
        first = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert first["result"]["content"][0]["text"] == "The first question.\n"
        workflow.screen("■ You've hit your usage limit.\n\u203a \n", "codex-7")
        quota = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert "usage limit" in quota["result"]["content"][0]["text"]
        (workflow.state / "tmux" / "codex-7").unlink()
        assert "error" in _rpc(relay, "tools/call", {"name": "await_turn"})


def test_quota_resend_preserves_unsent_bridge_reply(workflow: Workflow) -> None:
    _codex(workflow)
    workflow.screen(
        "■ You've hit your usage limit. Try again at Oct 15, 2026 4:00 PM.\n\u203a \n", "codex-7"
    )
    workflow.screen("──────\n> My unsent answer\n──────\n", "ticket-7")
    done = workflow.run("resend", "ticket-7")
    assert done.returncode != 0
    assert "text in its box" in done.stderr
    assert not workflow.calls("tmux ticket-7 send-keys")
    assert not workflow.calls("tmux codex-7 send-keys")


def test_notification_wake_preserves_unsent_bridge_reply(workflow: Workflow) -> None:
    _codex(workflow)
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Choose 1."})
    workflow.screen("──────\n> My unsent answer\n──────\n", "ticket-7")
    _notify(workflow, "The next turn.", bridge=True)
    assert not workflow.calls("tmux ticket-7 send-keys")
    assert (
        json.loads((workflow.notes(7) / "ticket-7.codex-turn.json").read_text())[
            "last-assistant-message"
        ]
        == "The next turn."
    )


def test_bridge_limit_resend_replays_question_without_sending_worker_instruction(
    workflow: Workflow,
) -> None:
    _codex(workflow)
    _notify(workflow, "Choose 1 or 2.", bridge=True)
    with _relay(workflow) as relay:
        _rpc(relay, "tools/call", {"name": "await_turn"})
        workflow.screen("You've hit your session limit · resets 4pm (UTC)\n> \n", "ticket-7")
        for _ in range(2):
            done = workflow.run_script("tick", env={"WORKFLOW_NOW": "1792080000"})
            assert done.returncode == 0, done.stderr
        calls = workflow.calls("tmux ticket-7 send-keys")
        assert len(calls) == 2
        assert calls[0].endswith("__WORKFLOW_CODEX_TURN__")
        assert not workflow.calls("tmux codex-7 send-keys")
        turn = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert turn["result"]["content"][0]["text"] == "Choose 1 or 2."


def test_bridge_refuses_reply_without_recorded_owner_prompt(workflow: Workflow) -> None:
    _codex(workflow)
    with _relay(workflow) as relay:
        result = _rpc(relay, "tools/call", {"name": "reply"})
    assert "error" in result
    assert not workflow.calls("tmux codex-7 send-keys")


@pytest.mark.parametrize(
    "provenance",
    [
        {},
        {"source": "user_input"},
        {"source": "background_task_output"},
        {"source": "auto_continue"},
        {"source": "permission_request"},
        {"source": "quota_auto_resume"},
        {"source": "model_switch"},
        {"system": True},
        {"is_user_input": False},
        {"user_initiated": False},
    ],
)
def test_background_completion_keeps_codex_finished(
    workflow: Workflow, provenance: dict[str, object]
) -> None:
    _codex(workflow, draft=False)
    _notify(workflow, "Ready.\nworker finished", bridge=True)
    event = json.loads((workflow.script.parent / "tests/await-turn-completed.json").read_text())
    assert workflow.notify({**event, **provenance}).returncode == 0
    assert not (workflow.notes(7) / "ticket-7.owner-reply").exists()
    assert not (workflow.notes(7) / "ticket-7.codex-followed-up").exists()
    with _relay(workflow) as relay:
        assert "error" in _rpc(relay, "tools/call", {"name": "reply"})
    assert not workflow.calls("tmux codex-7 send-keys")
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Done."})

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("codex-7")
    assert not workflow.is_running("ticket-7")
    assert workflow.is_running("review-7")


_NOTICE = "<task-notification>\n<status>completed</status>\n</task-notification>"


@pytest.mark.parametrize(
    "answer",
    [
        "  One more fix: explain <task-notification> here.\n\n",
        # Owner text between two notification blocks is still his answer.
        f"{_NOTICE}\n\nOne more fix.\n{_NOTICE}",
    ],
)
def test_owner_prompt_supersedes_finished_codex_turn(workflow: Workflow, answer: str) -> None:
    _codex(workflow, draft=False)
    _notify(workflow, "worker finished", bridge=True)
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": answer})
    assert (workflow.notes(7) / "ticket-7.owner-reply").read_text() == answer
    assert (workflow.notes(7) / "ticket-7.codex-followed-up").read_text() == (
        workflow.notes(7) / "ticket-7.codex-turn.json"
    ).read_text()
    with _relay(workflow) as relay:
        assert "result" in _rpc(relay, "tools/call", {"name": "reply"})
    assert "-l -- " + answer in (workflow.state / "calls.log").read_text()
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Reply sent."})

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")


def test_owner_prompt_is_recorded_exactly_and_consumed_once(workflow: Workflow) -> None:
    _codex(workflow)
    answer = "  1; $(literal)\nsecond line\n\n"
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": answer})
    recorded = workflow.notes(7) / "ticket-7.owner-reply"
    assert recorded.read_text() == answer
    with _relay(workflow) as relay:
        schema = _rpc(relay, "tools/list")["result"]["tools"][1]["inputSchema"]
        assert schema == {"type": "object", "properties": {}, "additionalProperties": False}
        sent = _rpc(relay, "tools/call", {"name": "reply"})
        assert "result" in sent
        again = _rpc(relay, "tools/call", {"name": "reply"})
        assert "error" in again
    assert not recorded.exists()
    assert len(workflow.calls("tmux codex-7 send-keys")) == 2
    assert "-l -- " + answer in (workflow.state / "calls.log").read_text()


_CODEX_WAITING = "• Recommend 1. Type 1.\n\n\u203a Ask Codex to do anything\n\n  ? for shortcuts\n"


def _after_typing(workflow: Workflow, screen: str) -> None:
    """What Codex draws once the reply is typed, where the stand-in's box cannot show it."""
    (workflow.state / "screen-after-typing-codex-7").write_text(screen)


def _reply(workflow: Workflow, answer: str) -> dict[str, Any]:
    workflow.screen(_CODEX_WAITING, "codex-7")
    (workflow.notes(7) / "ticket-7.codex-delivered").write_text("the delivered turn")
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": answer})
    with _relay(workflow) as relay:
        # Two Enter presses that each wait three seconds for the box to empty.
        return _rpc(relay, "tools/call", {"name": "reply"}, timeout=15)


@pytest.mark.parametrize("answer", ["1", "  1  ", "\n  1; $(literal)\nsecond line\n\n"])
@pytest.mark.parametrize("swallowed", [0, 1])
def test_reply_whose_enter_is_swallowed_is_sent_by_a_second_enter(
    workflow: Workflow, answer: str, swallowed: int
) -> None:
    _codex(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text(str(swallowed))
    sent = _reply(workflow, answer)
    assert sent["result"]["content"][0]["text"] == "Reply sent unchanged. Call await_turn."
    enter = "tmux codex-7 send-keys -t =codex-7: Enter"
    assert workflow.calls("tmux codex-7 send-keys")[1:] == [enter] * (1 + swallowed)
    assert "-l -- " + answer in (workflow.state / "calls.log").read_text()
    assert (workflow.notes(7) / "ticket-7.codex-answered").read_text() == "the delivered turn"


def test_reply_that_never_leaves_the_box_is_reported_as_not_sent(workflow: Workflow) -> None:
    _codex(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text("2")
    result = _reply(workflow, "1")
    assert "not sent" in result["error"]["message"]
    assert "still in Codex's input box" in result["error"]["message"]
    assert (
        workflow.calls("tmux codex-7 send-keys -t =codex-7: Enter")
        == ["tmux codex-7 send-keys -t =codex-7: Enter"] * 2
    )
    assert (workflow.state / "input-codex-7").read_text() == "1"
    assert not (workflow.notes(7) / "ticket-7.codex-answered").exists()
    assert not (workflow.notes(7) / "ticket-7.owner-reply").exists()


def test_failed_owner_followup_does_not_reactivate_recorded_completion(
    workflow: Workflow,
) -> None:
    _codex(workflow, draft=False)
    _notify(workflow, "worker finished", bridge=True)
    (workflow.state / "swallow-enter-codex-7").write_text("2")

    result = _reply(workflow, "One more fix")
    assert "not sent" in result["error"]["message"]
    assert not (workflow.notes(7) / "ticket-7.owner-reply").exists()
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Reply not sent."})

    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")

    # A later completed turn, distinct from the one followed up, can finish normally.
    _notify(workflow, "Fixed.\nworker finished", bridge=True)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("codex-7")
    assert workflow.is_running("review-7")


def test_reply_that_never_appears_gets_no_enter(workflow: Workflow) -> None:
    _codex(workflow)
    (workflow.state / "hide-input-codex-7").touch()
    result = _reply(workflow, "1")
    assert "did not appear" in result["error"]["message"]
    assert not workflow.calls("tmux codex-7 send-keys -t =codex-7: Enter")
    assert not (workflow.notes(7) / "ticket-7.codex-answered").exists()


@pytest.mark.parametrize("swallowed", [0, 1])
def test_reply_shown_as_a_pasted_content_stand_in_is_sent(
    workflow: Workflow, swallowed: int
) -> None:
    _codex(workflow)
    (workflow.state / "hide-input-codex-7").touch()
    (workflow.state / "swallow-enter-codex-7").write_text(str(swallowed))
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "a long answer"})
    workflow.screen(_CODEX_WAITING, "codex-7")
    _after_typing(workflow, "• Question\n\n\u203a [Pasted Content 1204 chars]\n")
    with _relay(workflow) as relay:
        sent = _rpc(relay, "tools/call", {"name": "reply"}, timeout=15)
    assert "result" in sent
    assert len(workflow.calls("tmux codex-7 send-keys -t =codex-7: Enter")) == 1 + swallowed


def test_tall_reply_whose_first_lines_scrolled_away_is_sent(workflow: Workflow) -> None:
    _codex(workflow)
    (workflow.state / "hide-input-codex-7").touch()
    answer = "".join(f"line {number}\n" for number in range(1, 42))
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": answer})
    workflow.screen(_CODEX_WAITING, "codex-7")
    _after_typing(workflow, "• Question\n\n\u203a line 40\n  line 41\n\n  ? for shortcuts\n")
    (workflow.state / "screen-after-enter-codex-7").write_text(_CODEX_WAITING)
    with _relay(workflow) as relay:
        sent = _rpc(relay, "tools/call", {"name": "reply"}, timeout=15)
    assert "result" in sent
    assert len(workflow.calls("tmux codex-7 send-keys -t =codex-7: Enter")) == 1


def test_reply_is_sent_when_codex_shows_it_is_working(workflow: Workflow) -> None:
    # The transcript's echo of the reply is the last prompt line while no box is drawn.
    _codex(workflow)
    (workflow.state / "hide-input-codex-7").touch()
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"})
    workflow.screen(_CODEX_WAITING, "codex-7")
    _after_typing(workflow, "• Recommend 1. Type 1.\n\n\u203a 1\n\n  ? for shortcuts\n")
    (workflow.state / "screen-after-enter-codex-7").write_text(
        "\u203a 1\n\n• Working (0s • esc to interrupt)\n"
    )
    with _relay(workflow) as relay:
        sent = _rpc(relay, "tools/call", {"name": "reply"}, timeout=15)
    assert sent["result"]["content"][0]["text"] == "Reply sent unchanged. Call await_turn."
    assert len(workflow.calls("tmux codex-7 send-keys -t =codex-7: Enter")) == 1


@pytest.mark.parametrize(
    ("screen", "answer"),
    [
        # Codex already works, so its status row was there before the reply.
        ("• Working (9s • esc to interrupt)\n\n", "also run the linter"),
        # A row of the footer in the reply is not the reply left in the box.
        ("• Working (9s • esc to interrupt)\n\n", "When done, explain\n? for shortcuts"),
        # The words of the status row in Codex's question or in the reply are not the row.
        ("• Press esc to interrupt (Codex) or type 1.\n\n", "1"),
        ("• Recommend 1. Type 1.\n\n", "• Working (explain esc to interrupt)"),
    ],
)
@pytest.mark.parametrize("swallowed", [1, 2])
def test_reply_is_sent_only_when_it_leaves_the_box_or_codex_starts_working(
    workflow: Workflow, screen: str, answer: str, swallowed: int
) -> None:
    _codex(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text(str(swallowed))
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": answer})
    workflow.screen(screen + "\u203a Ask Codex to do anything\n\n  ? for shortcuts\n", "codex-7")
    with _relay(workflow) as relay:
        result = _rpc(relay, "tools/call", {"name": "reply"}, timeout=15)
    assert len(workflow.calls("tmux codex-7 send-keys -t =codex-7: Enter")) == 2
    if swallowed == 1:
        assert result["result"]["content"][0]["text"] == "Reply sent unchanged. Call await_turn."
    else:
        assert "still in Codex's input box" in result["error"]["message"]
        assert not (workflow.notes(7) / "ticket-7.codex-answered").exists()


@pytest.mark.parametrize(
    "event",
    [
        {"prompt": "__WORKFLOW_CODEX_TURN__"},
        {"prompt": "another session's answer", "source": "background_task_output"},
        {"prompt": "another agent's answer", "source": "agent_message"},
        {"prompt": "another teammate's answer", "source": "teammate"},
        {"prompt": "a system answer", "system": True},
        {"prompt": "an automatic answer", "user_initiated": False},
        {},
    ],
)
def test_automatic_prompt_cannot_authorize_bridge_reply(
    workflow: Workflow, event: dict[str, object]
) -> None:
    _codex(workflow)
    workflow.notify({"hook_event_name": "UserPromptSubmit", **event})
    assert not (workflow.notes(7) / "ticket-7.owner-reply").exists()


@pytest.mark.parametrize(
    ("name", "bridge", "marker"),
    [
        ("review-7", False, False),
        ("ticket-7", True, False),
        ("ticket-7", False, True),
        ("hunt-test", False, False),
    ],
)
def test_unrelated_codex_notify_neither_rings_nor_changes_session_state(
    workflow: Workflow, name: str, bridge: bool, marker: bool
) -> None:
    workflow.add_worktree(7)
    _keys(workflow)
    worktree = workflow.add_hunt("test") if name.startswith("hunt-") else workflow.worktree(7)
    notes = worktree / ".local" / "workflow"
    notes.mkdir(parents=True, exist_ok=True)
    if marker:
        (notes / f"{name}.codex").touch()
    done = workflow.run_script(
        "notify",
        "codex",
        json.dumps({"type": "agent-turn-complete", "last-assistant-message": "Found an issue."}),
        env={"WORKFLOW_SESSION": name, "WORKFLOW_CODEX_BRIDGE": "1" if bridge else "0"},
    )
    assert done.returncode == 0
    assert workflow.pushes() == []
    assert not (notes / f"{name}.idle").exists()
    assert not (notes / f"{name}.codex-turn.json").exists()


def test_limit_words_in_later_lines_do_not_silence_bridge_question(workflow: Workflow) -> None:
    _codex(workflow)
    _keys(workflow)
    message = "The review found an issue.\nYou've hit your usage limit.\nChoose 1 or 2."
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": message})
    assert len(workflow.pushes()) == 1


def test_codex_shim_is_first_on_worker_path_and_nested_review_has_no_bridge_flag(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    assert workflow.run("start", "worker", "7", "medium", "codex").returncode == 0
    env = dict(
        line.split("=", 1) for line in (workflow.state / "env" / "codex-7").read_text().splitlines()
    )
    assert env.get("WORKFLOW_CODEX_BRIDGE") != "1"
    assert Path(env["PATH"].split(":")[0], "gh").is_file()
    # A nested Codex review only sees the global callback, without the worker's -c override.
    payload = json.dumps({"type": "agent-turn-complete", "last-assistant-message": "Review result"})
    done = workflow.run_script("notify", "codex", payload, env=env)
    assert done.returncode == 0
    assert not (workflow.notes(7) / "ticket-7.codex-turn.json").exists()
    assert not (workflow.notes(7) / "ticket-7.idle").exists()


def test_bridge_start_keeps_worker_marker_for_early_notify(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    original = workflow.bin / "tmux.real"
    (workflow.bin / "tmux").rename(original)
    marker = workflow.notes(7) / "ticket-7.codex"
    workflow.fake(
        "tmux",
        f"""#!/bin/bash
if [ "$3" = new-session ]; then
  [ -e {shlex.quote(str(marker))} ] || exit 1
fi
exec {shlex.quote(str(original))} "$@"
""",
    )
    done = workflow.run("start", "worker", "7", "medium", "codex")
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "merge", "7"],
        ["pr", "edit", "7", "--add-label", "workflow"],
        ["pr", "review", "7"],
        ["pr", "close", "7"],
        ["label", "create", "test"],
        ["pr", "ready", "8"],
        ["pr", "ready", "7", "--undo"],
        ["pr", "ready", "7", "--repo", "elsewhere/repo"],
        ["api", "-X", "POST", "repos/a/b/issues/7/labels"],
        ["api", "repos/a/b/issues/7", "-f", "title=changed"],
        ["api", "--method=PATCH", "repos/a/b/issues/7"],
        ["api", "-X", "PATCH", "repos/another/repository/pulls/7", "-F", "body=@/tmp/task.md"],
        ["api", "graphql", "-X", "GET", "-f", "query=mutation{}"],
        [
            "api",
            "-X",
            "PATCH",
            "repos/example-org/example-app/pulls/8",
            "-F",
            "body=@/tmp/task.md",
        ],
        [
            "api",
            "-X",
            "PATCH",
            "repos/example-org/example-app/pulls/8",
            "-F",
            "body=@/tmp/task.md",
            "--jq",
            ".number",
        ],
        [
            "api",
            "-X",
            "PATCH",
            "repos/example-org/example-app/pulls/7",
            "-F",
            "title=@/tmp/task.md",
        ],
        [
            "api",
            "-X",
            "PATCH",
            "repos/example-org/example-app/pulls/7",
            "-F",
            "body=@/tmp/task.md",
            "-F",
            "base=main",
        ],
        ["pr", "create", "--title", "follow-up"],
        ["pr", "create", "--draft", "--label", "workflow"],
        ["pr", "create", "--draft", "--label=workflow"],
        ["pr", "create", "--draft", "-lworkflow"],
        ["pr", "create", "--draft", "-l", "workflow"],
        # A ticket it opens may carry only the priority a session gives it.
        ["pr", "create", "--draft", "--label", "priority: blocker"],
        ["pr", "create", "--draft", "--label=priority: low"],
        ["pr", "create", "--draft", "--label", "priority: high,workflow"],
        ["pr", "create", "--draft", "--label", "priority: high", "--label", "workflow"],
        ["pr", "create", "--draft", "--label"],
        ["pr", "create", "--label", "priority: medium"],
    ],
)
def test_codex_gh_shim_refuses_mutations(workflow: Workflow, args: list[str]) -> None:
    workflow.add_pull_request(7)
    assert workflow.run("start", "worker", "7", "medium", "codex").returncode == 0
    env = dict(
        line.split("=", 1) for line in (workflow.state / "env" / "codex-7").read_text().splitlines()
    )
    workflow.fake("gh", '#!/bin/bash\necho "allowed $*"\n')
    shim = Path(env["PATH"].split(":")[0], "gh")
    done = subprocess.run(
        ["bash", str(shim), *args], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode != 0
    assert "Codex worker" in done.stderr
    assert "allowed" not in done.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "view", "7", "--json", "body"],
        ["pr", "list"],
        ["pr", "diff", "7"],
        ["pr", "checks", "7"],
        ["pr", "ready", "7"],
        ["api", "--paginate", "repos/a/b/pulls/7/reviews"],
        ["api", "-X", "GET", "repos/a/b/pulls/7"],
        ["api", "--method=GET", "repos/a/b/pulls/7"],
        [
            "api",
            "-X",
            "PATCH",
            "repos/example-org/example-app/pulls/7",
            "-F",
            "body=@/tmp/task.md",
        ],
        ["pr", "create", "--draft", "--title", "fix: follow-up", "--body-file", "/tmp/task.md"],
        ["pr", "create", "--draft", "--label", "priority: medium", "--title", "fix: follow-up"],
        ["pr", "create", "--draft", "--label=priority: high"],
        ["pr", "create", "-d", "-l", "priority: high"],
        ["pr", "create", "--draft", "-lpriority: medium"],
    ],
)
def test_codex_gh_shim_allows_reads_and_own_ready(workflow: Workflow, args: list[str]) -> None:
    workflow.add_pull_request(7)
    assert workflow.run("start", "worker", "7", "medium", "codex").returncode == 0
    env = dict(
        line.split("=", 1) for line in (workflow.state / "env" / "codex-7").read_text().splitlines()
    )
    workflow.fake("gh", '#!/bin/bash\necho "allowed $*"\n')
    shim = Path(env["PATH"].split(":")[0], "gh")
    done = subprocess.run(
        ["bash", str(shim), *args], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "allowed " + " ".join(args)


@pytest.mark.parametrize(
    ("suffix", "allowed"),
    [
        (["--jq", ".number"], True),
        (["-q", ".html_url"], True),
        (["--silent"], True),
        (["--template", "{{.number}}"], True),
        (["-t", "{{.number}}"], True),
        (["-i"], True),
        (["--include"], True),
        (["--verbose"], True),
        (["--jq", ".number", "--silent", "-i"], True),
        (["--template", "-F"], True),
        (["-f", "title=changed"], False),
        (["-F", "state=closed"], False),
        (["--silent", "-F", "body=@/tmp/other.md"], False),
        (["--silent", "repos/example-org/example-app/pulls/8"], False),
        (["--silent", "-X", "DELETE"], False),
        (["-H", "Accept: application/json"], False),
        (["--jq"], False),
        (["-q"], False),
        (["--template"], False),
        (["-t"], False),
        (["--unknown"], False),
    ],
)
def test_codex_gh_shim_body_output_options(
    workflow: Workflow, suffix: list[str], allowed: bool
) -> None:
    args = [
        "api",
        "-X",
        "PATCH",
        "repos/example-org/example-app/pulls/7",
        "-F",
        "body=@/tmp/task.md",
        *suffix,
    ]
    if allowed:
        test_codex_gh_shim_allows_reads_and_own_ready(workflow, args)
    else:
        test_codex_gh_shim_refuses_mutations(workflow, args)


def test_hour_only_codex_limit_has_no_invented_reset(workflow: Workflow) -> None:
    _codex(workflow)
    workflow.screen("■ You've hit your usage limit. Try again at 4:00 PM.\n\u203a \n", "codex-7")
    listed = workflow.run("list")
    assert "Codex limit" in listed.stdout
    assert "resets" not in listed.stdout
    workflow.run_script("tick", env={"WORKFLOW_NOW": "1792080000", "TZ": "UTC"})
    assert not workflow.calls("tmux codex-7 send-keys")


@pytest.mark.parametrize(
    "heading",
    [
        "Would you like to run the following command?",
        "Would you like to make the following edits?",
        "Would you like to grant these permissions?",
        "Would you like to send input to the existing terminal?",
    ],
)
def test_codex_permission_question_is_relayed_before_turn_complete(
    workflow: Workflow, heading: str
) -> None:
    _codex(workflow)
    _keys(workflow)
    panel = (
        f"{heading}\n\n"
        "$ git push\n\n\u203a 1. Yes, proceed (y)\n"
        "2. No (esc)\n\nPress enter to confirm or esc to cancel"
    )
    workflow.screen(panel, "codex-7")
    with _relay(workflow) as relay:
        result = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert result["result"]["content"][0]["text"] == panel
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": panel})
    assert len(workflow.pushes()) == 1


@pytest.mark.parametrize("taken", [True, False])
def test_reply_to_a_confirmation_panel_gets_one_enter_and_must_change_it(
    workflow: Workflow, taken: bool
) -> None:
    _codex(workflow)
    if not taken:
        (workflow.state / "hide-input-codex-7").touch()
        (workflow.state / "swallow-enter-codex-7").write_text("1")
    workflow.screen(
        "Would you like to run the following command?\n\n$ git push\n\n"
        "\u203a 1. Yes, proceed (y)\n2. No (esc)\n\nPress enter to confirm or esc to cancel",
        "codex-7",
    )
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"})
    with _relay(workflow) as relay:
        result = _rpc(relay, "tools/call", {"name": "reply"}, timeout=15)
    assert len(workflow.calls("tmux codex-7 send-keys -t =codex-7: Enter")) == 1
    assert "-l -- 1" in (workflow.state / "calls.log").read_text()
    if taken:
        assert result["result"]["content"][0]["text"] == "Reply sent unchanged. Call await_turn."
    else:
        assert "same confirmation panel" in result["error"]["message"]


def test_answered_question_is_not_replayed_after_bridge_limit(workflow: Workflow) -> None:
    _codex(workflow)
    _notify(workflow, "Choose 1 or 2.", bridge=True)
    with _relay(workflow) as relay:
        _rpc(relay, "tools/call", {"name": "await_turn"})
        delivered = (workflow.notes(7) / "ticket-7.codex-delivered").read_text()
        workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"})
        assert "result" in _rpc(relay, "tools/call", {"name": "reply"})
        workflow.screen("You've hit your session limit · resets 4pm (UTC)\n> \n", "ticket-7")
        done = workflow.run("resend", "ticket-7")
        assert done.returncode == 0, done.stderr
        assert (workflow.notes(7) / "ticket-7.codex-answered").read_text() == delivered
        assert relay.stdin and relay.stdout
        relay.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {"name": "await_turn"},
                }
            )
            + "\n"
        )
        relay.stdin.flush()
        # The server processes await_turn synchronously up to its first wait before
        # reading tools/list. Its reply is a barrier: a replay would arrive first.
        barrier = _rpc(relay, "tools/list")
        assert barrier["id"] == 1, "answered turn was replayed"
        relay.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 99},
                }
            )
            + "\n"
        )
        relay.stdin.flush()
        with selectors.DefaultSelector() as ready:
            ready.register(relay.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "relay did not cancel the pending wait"
        cancelled = json.loads(relay.stdout.readline())
        assert cancelled["id"] == 99
        assert cancelled["error"]["message"] == "The operation was aborted"


def test_codex_limit_restarts_missing_bridge_and_failed_send_is_retried(workflow: Workflow) -> None:
    _codex(workflow)
    (workflow.state / "tmux" / "ticket-7").unlink()
    workflow.screen(
        "■ You've hit your usage limit. Try again at Oct 15, 2026 4:00 PM.\n\u203a \n", "codex-7"
    )
    (workflow.state / "send-keys-fails").touch()
    env = {"WORKFLOW_NOW": "1792080000", "TZ": "UTC"}
    first = workflow.run_script("tick", env=env)
    assert first.returncode == 1
    assert workflow.is_running("ticket-7"), first.stderr
    assert not (workflow.notes(7) / "ticket-7.resent").exists()
    assert len(workflow.calls("tmux codex-7 send-keys")) == 1
    (workflow.state / "send-keys-fails").unlink()
    second = workflow.run_script("tick", env=env)
    assert second.returncode == 0, second.stderr
    calls = workflow.calls("tmux codex-7 send-keys")
    assert len(calls) == 3
    assert instruction("Implement pull request 7") in calls[-2]
    assert (workflow.notes(7) / "ticket-7.resent").exists()


@pytest.mark.parametrize(
    ("prompt", "woken"),
    [("\u276f\u00a0", True), ("\u276f\u00a0\n\u3000", True), ("\u276f\u00a0wait", False)],
)
def test_a_stopped_bridge_reads_its_empty_box_with_a_no_break_space(
    workflow: Workflow, prompt: str, woken: bool
) -> None:
    _codex(workflow)
    (workflow.notes(7) / "ticket-7.bridge-stopped").touch()
    rule = "─" * 40
    workflow.screen(f"Done.\n\n{rule}\n{prompt}\n{rule}\n", "ticket-7")
    assert _notify(workflow, "Recommend 1. Type 1.", bridge=True).returncode == 0
    calls = workflow.calls("tmux ticket-7 send-keys")
    assert (len(calls) == 2 and "__WORKFLOW_CODEX_TURN__" in calls[0]) is woken


@pytest.mark.parametrize("swallowed", [1, 2])
def test_codex_turn_marker_uses_checked_delivery(workflow: Workflow, swallowed: int) -> None:
    _codex(workflow)
    workflow.screen(
        "■ You've hit your usage limit. Visit settings or try again at "
        "Oct 15th, 2026 4:00 PM.\n\u203a \n",
        "codex-7",
    )
    (workflow.state / "swallow-enter-ticket-7").write_text(str(swallowed))
    done = workflow.run("resend", "ticket-7")
    assert len(workflow.calls("tmux ticket-7 send-keys -t =ticket-7: Enter")) == 2
    if swallowed == 1:
        assert done.returncode == 0, done.stderr
    else:
        assert done.returncode != 0
        assert "still has automation text" in done.stderr
        assert (workflow.notes(7) / "ticket-7.typed").read_text() == "__WORKFLOW_CODEX_TURN__"


def test_codex_box_holding_its_own_earlier_instruction_gets_only_enter(
    workflow: Workflow,
) -> None:
    _codex(workflow)
    workflow.screen(
        "■ You've hit your usage limit. Visit settings or try again at "
        "Oct 15th, 2026 4:00 PM.\n\u203a \n",
        "codex-7",
    )
    sentence = instruction("Implement pull request 7")
    (workflow.notes(7) / "codex-7.typed").write_text(sentence)
    (workflow.state / "input-codex-7").write_text(sentence)
    done = workflow.run("resend", "ticket-7")
    assert done.returncode == 0, done.stderr
    assert workflow.calls("tmux codex-7 send-keys") == ["tmux codex-7 send-keys -t =codex-7: Enter"]
    assert not (workflow.notes(7) / "codex-7.typed").exists()


@pytest.mark.parametrize("bridge_box", ["__WORKFLOW_CODEX_TURN__", "My unsent answer"])
def test_bridge_box_holding_its_own_earlier_marker_gets_only_enter(
    workflow: Workflow, bridge_box: str
) -> None:
    _codex(workflow)
    workflow.screen(
        "■ You've hit your usage limit. Visit settings or try again at "
        "Oct 15th, 2026 4:00 PM.\n\u203a \n",
        "codex-7",
    )
    workflow.screen("──────\n> \n──────\n", "ticket-7")
    (workflow.notes(7) / "ticket-7.typed").write_text("__WORKFLOW_CODEX_TURN__")
    (workflow.state / "input-ticket-7").write_text(bridge_box)
    done = workflow.run("resend", "ticket-7")
    if bridge_box == "__WORKFLOW_CODEX_TURN__":
        assert done.returncode == 0, done.stderr
        assert workflow.calls("tmux ticket-7 send-keys") == [
            "tmux ticket-7 send-keys -t =ticket-7: Enter"
        ]
        assert not (workflow.notes(7) / "ticket-7.typed").exists()
    else:
        assert done.returncode != 0
        assert "text in its box" in done.stderr
        assert not workflow.calls("tmux ticket-7 send-keys")
        assert not workflow.calls("tmux codex-7 send-keys")


def test_a_restarted_bridge_keeps_the_question_codex_asked(workflow: Workflow) -> None:
    """The check-in counts the wait from the first time Codex asked, not from the restart."""
    _codex(workflow)
    asking = workflow.notes(7) / "ticket-7.asking"
    asking.write_text("1792000000 123\n")
    (workflow.state / "tmux" / "ticket-7").unlink()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert asking.read_text() == "1792000000 123\n"


def test_codex_and_its_bridge_each_run_in_a_scope_with_the_memory_limits(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)

    done = workflow.run("start", "worker", "7", "high", "codex")

    assert done.returncode == 0, done.stderr
    for name in ("codex-7", "ticket-7"):
        arguments = (workflow.state / "args" / name).read_text().splitlines()
        start = arguments.index("systemd-run")
        assert arguments[start:-1] == scope_words(name, arguments[start + 5])


def test_list_shows_the_limit_of_codex_and_its_bridge_once(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "ticket-7.codex").touch()
    for name, held in (("ticket-7", 10), ("codex-7", 500)):
        scope = f"user.slice/app.slice/workflow-{name}-4242.scope"
        group = workflow.running(name, held=held * 1024 * 1024, scope=scope)
        (group / "memory.max").write_text(f"{8 * 1024**3}\n")

    assert "ticket-7  busy  510 MB held (limit 8 GB each)  0 MB in swap" in (
        workflow.run("list").stdout.splitlines()
    )
