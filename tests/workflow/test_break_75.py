"""Independent pane replays for completion and safe prompt delivery in PR 75.

Only this file is run. External commands are replaced with a pane stand-in;
the shell functions under test are read verbatim from the production scripts.
"""

import json
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"
PROMPT = "Continue checking example-app."
RULE = "─" * 48


def function(script, name):
    source = (SCRIPTS / script).read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", source, re.M | re.S)
    assert match, name
    return match.group()


def shell(code, *, data="", env=None):
    return subprocess.run(
        ["bash", "-c", code], input=data, text=True, capture_output=True,
        env=env, cwd=env.get("BREAK_STATE") if env else None, timeout=12, check=False,
    )


def source(name):
    return 'source "$BREAK_SCRIPTS/' + name + '"\n'


@pytest.fixture
def pane(tmp_path):
    state = tmp_path / "pane"
    state.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    notes = state / "notes"
    notes.mkdir()
    (state / "input").write_text("")
    (state / "config").write_text("{}")
    program = commands / "tmux"
    program.write_text('''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
state = Path(os.environ["BREAK_STATE"])
config = json.loads((state / "config").read_text())
args = sys.argv[1:]
with (state / "calls").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if "capture-pane" in args:
    if config.get("capture_failure"):
        sys.exit(1)
    if "raw" in config:
        print(config["raw"])
        sys.exit(0)
    text = (state / "input").read_text()
    if config.get("hide_input"):
        text = ""
    if not text:
        text = config.get("suggestion", "")
    if args[1].startswith("codex-"):
        print("Waiting for checks.\\n" + config.get("native_marker", "› ") + text
              + "\\n\\n  ? for shortcuts")
        sys.exit(0)
    rows = text.split("\\n")
    marker = config.get("marker", "> ")
    lines = ["Waiting for checks.", "─" * 48, marker + rows[0]]
    lines += ["  " + row for row in rows[1:]]
    if not config.get("incomplete"):
        lines += ["─" * 48]
    lines += ["─" * 16 + " Status: waiting " + "─" * 16]
    if config.get("border"):
        lines = [row.ljust(72) + "\\x1b[90m│\\x1b[0m" for row in lines]
    screen = "\\n".join(lines)
    if "-e" not in args:
        import re
        screen = re.sub(r"\\x1b\\[[0-?]*[ -/]*[@-~]", "", screen)
    print(screen)
elif "send-keys" in args:
    if "-l" in args:
        (state / "input").write_text(args[args.index("-l") + 1])
    elif args[-1] == "Enter":
        remaining = config.get("swallow", 0)
        if remaining:
            config["swallow"] = remaining - 1
            (state / "config").write_text(json.dumps(config))
        else:
            (state / "submitted").write_text((state / "input").read_text())
            (state / "input").write_text("")
else:
    sys.exit(2)
''')
    program.chmod(0o700)
    sleeper = commands / "sleep"
    sleeper.write_text('''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import time
state = Path(os.environ["BREAK_STATE"])
config = json.loads((state / "config").read_text())
if config.get("pause_on_sleep"):
    (state / "settling").touch()
    deadline = time.monotonic() + 5
    while not (state / "release").exists():
        if time.monotonic() > deadline:
            raise SystemExit(1)
        time.sleep(0.01)
if "edit_on_sleep" in config:
    (state / "input").write_text(config.pop("edit_on_sleep"))
if config.pop("incomplete_on_sleep", False):
    config["incomplete"] = True
if config.pop("capture_failure_on_sleep", False):
    config["capture_failure"] = True
(state / "config").write_text(json.dumps(config))
''')
    sleeper.chmod(0o700)
    return state, {
        **os.environ, "BREAK_STATE": str(state), "BREAK_SCRIPTS": str(SCRIPTS),
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
    }


def configure(pane, **config):
    (pane[0] / "config").write_text(json.dumps(config))


def keys(pane):
    calls = pane[0] / "calls"
    return [args for line in calls.read_text().splitlines()
            if "send-keys" in (args := json.loads(line))]


def send(pane, name="review-7"):
    code = "set -euo pipefail\n" + source("input-box")
    for function_name in ("read_prompt_box", "prompt_box_matches", "send_prompt"):
        code += function("session", function_name) + "\n"
    code += '''die() { printf '%s\\n' "$*" >&2; exit 3; }
worktree_of() { printf '%s\\n' "$BREAK_STATE"; }
SHIPLINE_STATE_FOLDER=notes
'''
    code += f"send_prompt {name} 'Continue checking example-app.'\n"
    return shell(code, env=pane[1])


@pytest.mark.parametrize("words", ["review finished", "review sent back", "worker finished",
                                  "audit finished", "hunt finished"])
@pytest.mark.parametrize("frame", ["│", "╎", "╬", "│ \u00a0 ┃"])
def test_completion_borders_and_last_line(pane, words, frame):
    name = "ticket-7" if words.startswith("worker") else "review-7"
    screen = f"│ ● **{words}!**      {frame}\n│           {frame}\n{RULE}│\n>      │\n{RULE}│\n"
    configure(pane, raw=screen)
    pattern = next(line for line in (SCRIPTS / "tick").read_text().splitlines()
                   if line.startswith("FINISHED_LINE="))
    code = "set -euo pipefail\n" + source("screen-borders") + source("input-box")
    code += pattern + "\n" + function("tick", "said_finished") + "\n"
    code += function("tick", "last_line") + "\n"
    code += 'main=$BREAK_STATE; SHIPLINE_STATE_FOLDER=notes\n'
    code += f'said_finished {name} "{words}"\nlast_line {name}\n'
    done = shell(code, env=pane[1])
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"**{words}!**"
    code = source("screen-borders") + pattern + "\n" + function("notify", "said_finished")
    code += f'\nsaid_finished "{words}" "$(cat)"\n'
    done = shell(code, data=f"│ {words}   {frame}\n│    {frame}\n", env=pane[1])
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("message", ["We will say review finished later.",
                                    "> review finished", '"review finished"',
                                    "review finished\nPlease check one more thing."])
def test_completion_does_not_end_a_sentence_quote_or_newer_reply(pane, message):
    configure(pane, raw="\n".join(row + "     │" for row in message.splitlines())
              + f"\n{RULE}│\n>      │\n{RULE}│\n")
    pattern = next(line for line in (SCRIPTS / "tick").read_text().splitlines()
                   if line.startswith("FINISHED_LINE="))
    code = source("screen-borders") + pattern + "\n" + function("tick", "said_finished")
    code += '\nmain=$BREAK_STATE; SHIPLINE_STATE_FOLDER=notes; said_finished review-7\n'
    assert shell(code, env=pane[1]).returncode != 0


@pytest.mark.parametrize("style", ["2", "90", "38;5;244", "38;2;153;153;153"])
@pytest.mark.parametrize("border", [False, True])
def test_suggestion_allows_delivery_but_identical_owner_words_block(pane, style, border):
    configure(pane, suggestion=f"\x1b[{style}mcontinue\x1b[0m", border=border)
    done = send(pane)
    assert done.returncode == 0, done.stderr
    assert (pane[0] / "submitted").read_text() == PROMPT
    assert not (pane[0] / "notes/review-7.typed").exists()
    (pane[0] / "calls").write_text("")
    (pane[0] / "submitted").unlink()
    (pane[0] / "input").write_text("continue")
    done = send(pane)
    assert done.returncode != 0
    assert keys(pane) == []
    assert not (pane[0] / "submitted").exists()


@pytest.mark.parametrize("border", [False, True])
@pytest.mark.parametrize("edit", ["Owner follow-up", PROMPT + "\nOwner addition",
                                 PROMPT + "\n" + RULE + "\nOwner addition"])
def test_owner_edit_during_settling_never_reaches_enter(pane, border, edit):
    configure(pane, border=border, edit_on_sleep=edit)
    done = send(pane)
    assert done.returncode != 0
    assert "input changed before Enter" in done.stderr
    assert not any(args[-1] == "Enter" for args in keys(pane))
    assert (pane[0] / "input").read_text() == edit


def test_incomplete_redraw_before_enter_stops_delivery(pane):
    configure(pane, incomplete_on_sleep=True)
    done = send(pane)
    assert done.returncode != 0
    assert not any(args[-1] == "Enter" for args in keys(pane))
    assert (pane[0] / "notes/review-7.typed").read_text() == PROMPT


def test_swallowed_enters_retry_only_the_recorded_text(pane):
    configure(pane, swallow=2, border=True)
    first = send(pane)
    assert first.returncode != 0
    assert "after two Enter attempts" in first.stderr
    assert (pane[0] / "notes/review-7.typed").read_text() == PROMPT
    second = send(pane)
    assert second.returncode == 0, second.stderr
    assert sum("-l" in args for args in keys(pane)) == 1
    assert (pane[0] / "submitted").read_text() == PROMPT


def test_missing_capture_never_types_or_submits(pane):
    configure(pane, capture_failure=True)
    done = send(pane)
    assert done.returncode != 0
    assert keys(pane) == []


@pytest.mark.parametrize("text", ["Owner addition", "? for shortcuts", PROMPT + " │"])
def test_bridge_refuses_partial_current_box_and_owner_continuations(pane, text):
    configure(pane, raw=f"{RULE}\n> \n{RULE}\n{RULE}\n> \n  {text}\n")
    done = subprocess.run(
        ["bash", str(SCRIPTS / "bridge-empty"), "review-7"],
        env=pane[1], capture_output=True, text=True, timeout=12, check=False,
    )
    assert done.returncode != 0
    assert keys(pane) == []


@pytest.mark.parametrize("style", ["2", "90", "38:5:244", "38:2::153:153:153"])
def test_codex_placeholder_and_normal_draft_are_distinguished(pane, style):
    code = source("input-box") + 'codex_input "$(cat)"\n'
    done = shell(code, data=f"Waiting.\n› \x1b[{style}mAsk for help\x1b[0m\n\n  ? for shortcuts\n",
                 env=pane[1])
    assert done.returncode == 0 and not done.stdout.strip(), done.stderr
    done = shell(code, data="Waiting.\n› Ask for help\n  Owner addition\n\n  ? for shortcuts\n",
                 env=pane[1])
    assert "Ask for help" in done.stdout and "Owner addition" in done.stdout


def test_recorded_native_turn_stays_authoritative(pane):
    notes = pane[0] / ".claude/worktrees/ticket-7/notes"
    notes.mkdir(parents=True)
    (notes / "review-7.codex").touch()
    (notes / "review-7.codex-turn.json").write_text(json.dumps({
        "last-assistant-message": "Still waiting for checks.", "turn-id": "one",
    }))
    configure(pane, raw=f"review finished      │\n{RULE}\n> \n{RULE}\n")
    pattern = next(line for line in (SCRIPTS / "tick").read_text().splitlines()
                   if line.startswith("FINISHED_LINE="))
    code = source("screen-borders") + pattern + "\n" + function("tick", "said_finished")
    code += '\nmain=$BREAK_STATE; SHIPLINE_STATE_FOLDER=notes; said_finished review-7\n'
    assert shell(code, env=pane[1]).returncode != 0


def native_code(pane):
    state = pane[0]
    notes = state / ".claude/worktrees/ticket-7/notes"
    notes.mkdir(parents=True)
    for suffix in ("checks-wait", "bridge-stopped"):
        (notes / f"review-7.{suffix}").touch()
    for suffix in ("codex-turn.json", "codex-delivered"):
        (notes / f"review-7.{suffix}").write_text('{"turn-id":"one"}')
    code = "set -euo pipefail\n" + source("input-box")
    for name in ("read_prompt_box", "prompt_box_matches", "send_prompt", "send_codex_prompt"):
        code += function("session", name) + "\n"
    code += '''die() { printf '%s\\n' "$*" >&2; exit 3; }
require_running() { :; }
worktree_of() { printf '%s\\n' "$BREAK_STATE"; }
main=$BREAK_STATE
script_dir=$BREAK_SCRIPTS
SHIPLINE_STATE_FOLDER=notes
send_codex_prompt review-7 'Continue checking example-app.' wake
'''
    return code


def test_a_lock_held_too_long_refuses_native_delivery_without_typing(pane):
    code = native_code(pane)
    lock = pane[0] / "notes/start-7.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = shell(code, env=pane[1])
    assert done.returncode != 0
    assert "another action" in done.stderr
    assert not (pane[0] / "calls").exists()


def test_two_native_calls_cannot_both_submit(pane):
    configure(pane, pause_on_sleep=True)
    code = native_code(pane)
    first = subprocess.Popen(["bash", "-c", code], env=pane[1],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 6
        while not (pane[0] / "settling").exists():
            assert first.poll() is None, first.communicate()
            assert time.monotonic() < deadline, "first sender did not reach settling"
            time.sleep(0.01)
        second = shell(code, env=pane[1])
        assert second.returncode != 0
        assert "another action" in second.stderr
        (pane[0] / "release").touch()
        out, err = first.communicate(timeout=8)
        assert first.returncode == 0, (out, err)
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate()
    assert sum("-l" in args for args in keys(pane)) == 1
    assert sum(args[-1] == "Enter" for args in keys(pane)) == 1


@pytest.mark.parametrize("reason", ["lost", "checks"])
def test_lost_or_late_check_wake_retries_with_a_grey_suggestion(pane, reason):
    configure(pane, suggestion="\x1b[90mcontinue\x1b[39m", border=True, swallow=2)
    (pane[0] / "notes/review-7.checks-wait").touch()
    code = "set -euo pipefail\n" + source("input-box")
    for name in ("read_prompt_box", "prompt_box_matches", "send_prompt",
                 "send_reviewer_update", "cmd_checks_finished"):
        code += function("session", name) + "\n"
    code += '''die() { printf '%s\\n' "$*" >&2; exit 3; }
require_running() { :; }
stopped_on_limit() { return 1; }
worktree_of() { printf '%s\\n' "$BREAK_STATE"; }
SHIPLINE_STATE_FOLDER=notes
'''
    code += f"cmd_checks_finished review-7 {'a' * 40} {reason}\n"
    first = shell(code, env=pane[1])
    assert first.returncode != 0
    second = shell(code, env=pane[1])
    assert second.returncode == 0, second.stderr
    submitted = (pane[0] / "submitted").read_text()
    assert ("GitHub lost a check" if reason == "lost" else "checks on") in submitted
    assert sum("-l" in args for args in keys(pane)) == 1


@pytest.mark.parametrize("style", ["normal", "grey", "dim"])
@pytest.mark.parametrize("name", ["review-7", "codex-7"])
def test_styled_prompt_marker_does_not_hide_an_owner_quote_edit(pane, style, name):
    start, end = {"normal": ("", ""), "grey": ("\x1b[90m", "\x1b[39m"),
                  "dim": ("\x1b[2m", "\x1b[22m")}[style]
    configure(pane, marker=start + "❯ " + end, native_marker=start + "› " + end)
    (pane[0] / "input").write_text("> " + PROMPT)
    (pane[0] / f"notes/{name}.typed").write_text(PROMPT)
    done = send(pane, name)
    assert done.returncode != 0, (
        "sender exited 0 and submitted owner edit: " + repr((pane[0] / "submitted").read_text())
    )
    assert keys(pane) == []


def test_grey_prompt_with_owner_quote_through_real_tmux_capture(tmp_path):
    tmux = shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux is not installed")
    config = tmp_path / "config"
    config.touch()
    display = tmp_path / "display"
    display.write_text(f"{RULE}\n\x1b[90m❯ \x1b[39m> {PROMPT}\n{RULE}\n")
    hold = tmp_path / "hold"
    hold.touch()
    socket = Path.cwd() / ".local" / f"b75-{os.getpid()}.sock"
    socket.parent.mkdir(exist_ok=True)
    prefix = [tmux, "-S", str(socket), "-f", str(config)]
    env = {**os.environ, "BREAK_DISPLAY": str(display), "BREAK_HOLD": str(hold),
           "BREAK_SCRIPTS": str(SCRIPTS)}
    env.pop("TMUX", None)
    try:
        started = subprocess.run(prefix + ["new-session", "-d", "-x", "120", "-y", "24",
                                "-s", "example-app", "bash", "-c",
                                'cat "$BREAK_DISPLAY"; exec tail -f "$BREAK_HOLD"'],
                       env=env, capture_output=True, text=True, check=False, timeout=5)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 5
        while True:
            shown = subprocess.run(prefix + ["capture-pane", "-p", "-e", "-t", "example-app:"],
                                   env=env, capture_output=True, text=True, check=True, timeout=5)
            if PROMPT in shown.stdout:
                break
            assert time.monotonic() < deadline, "terminal did not draw the test prompt"
            time.sleep(0.01)
        code = source("input-box") + 'claude_input "$(cat)"\n'
        done = shell(code, data=shown.stdout, env=env)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "> " + PROMPT, "the real capture lost the owner quote marker"
    finally:
        subprocess.run(prefix + ["kill-server"], env=env, capture_output=True, timeout=5)


def test_capture_failure_after_typing_restores_native_wait_for_retry(pane):
    configure(pane, capture_failure_on_sleep=True)
    code = native_code(pane)
    done = shell(code, env=pane[1])
    assert done.returncode != 0
    assert "cannot read the input box" in done.stderr
    assert (pane[0] / ".claude/worktrees/ticket-7/notes/review-7.checks-wait").exists()
    assert (pane[0] / "notes/codex-7.typed").read_text() == PROMPT
    assert not any(args[-1] == "Enter" for args in keys(pane))


def test_owner_shell_metacharacters_are_neither_evaluated_nor_sent(pane):
    text = "$(touch injected) `touch injected` ; example-app"
    (pane[0] / "input").write_text(text)
    done = send(pane)
    assert done.returncode != 0
    assert keys(pane) == []
    assert not (pane[0] / "injected").exists()
    assert (pane[0] / "input").read_text() == text


def test_draw_timeout_leaves_owned_text_for_the_next_retry(pane):
    configure(pane, hide_input=True)
    first = send(pane)
    assert first.returncode != 0
    assert "did not appear in its input box within 2 seconds" in first.stderr
    assert not any(args[-1] == "Enter" for args in keys(pane))
    assert (pane[0] / "notes/review-7.typed").read_text() == PROMPT
    configure(pane)
    second = send(pane)
    assert second.returncode == 0, second.stderr
    assert sum("-l" in args for args in keys(pane)) == 1
