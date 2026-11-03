"""Suggestions are decoration; only the owner's visible input blocks a wake."""

import json
import os
import subprocess

import pytest
from workflow_harness import SCRIPTS, Workflow, instruction

_STATUS = (
    "Status check: if what you were waiting for is done, carry on; if not, run "
    "`scripts/workflow/notify waiting` and end your turn."
)


def _bordered_input(
    workflow: Workflow, *, width: int = 72, footer: str = "rules", text: str = ""
) -> None:
    """Draw a padded right border and a status row after the ordinary input box."""
    workflow.add_worktree(7)
    workflow.running("review-7")
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "review-7.idle").touch()
    workflow.screen("Waiting.\n" + "─" * width + "\n> \n" + "─" * width + "\n", "review-7")
    (workflow.state / "input-review-7").write_text(text)
    tmux = workflow.bin / "tmux"
    script = tmux.read_text().replace(
        'lines[rules[-2] + 1:rules[-1]] = ["> " + text]',
        "import textwrap\n    lines[rules[-2] + 1:rules[-1]] = "
        f'[row.ljust({width}) for row in textwrap.wrap("> " + text, width={width}, '
        'subsequent_indent="  ", replace_whitespace=False) or ["> "]]',
    )
    status = (
        ["─" * width, "Status: waiting", "─" * width]
        if footer == "rules"
        else ["─" * 20 + " Status: waiting " + "─" * 20]
    )
    script = script.replace(
        'print("\\n".join(lines))',
        f"lines += {status!r}\n"
        f'print("\\n".join(row.ljust({width}) + "\\x1b[90m│\\x1b[0m" for row in lines))',
    )
    tmux.write_text(script)


@pytest.mark.parametrize("width", [72, 240])
@pytest.mark.parametrize("footer", ["rules", "inline"])
def test_bordered_input_with_later_status_rules_is_matched_and_submitted(
    workflow: Workflow, width: int, footer: str
) -> None:
    _bordered_input(workflow, width=width, footer=footer)

    done = workflow.run("status-check", "review-7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("tmux review-7 send-keys") == [
        f"tmux review-7 send-keys -t =review-7: -l {_STATUS}",
        "tmux review-7 send-keys -t =review-7: Enter",
    ]
    assert not (workflow.notes(7) / "review-7.typed").exists()
    assert (workflow.state / "input-review-7").read_text() == ""


@pytest.mark.parametrize("width", [72, 240])
def test_a_bordered_input_retry_submits_only_the_ticks_own_text(
    workflow: Workflow, width: int
) -> None:
    _bordered_input(workflow, width=width)
    now = 1_790_000_000
    waiting = workflow.notes(7) / "review-7.job-wait"
    waiting.touch()
    os.utime(waiting, (now - 3000, now - 3000))
    (workflow.state / "swallow-enter-review-7").write_text("2")

    first = workflow.run_script("tick", env={"WORKFLOW_NOW": str(now)})

    assert first.returncode != 0
    assert (workflow.notes(7) / "review-7.typed").read_text() == _STATUS
    second = workflow.run_script("tick", env={"WORKFLOW_NOW": str(now + 180)})

    assert second.returncode == 0, second.stderr
    assert workflow.calls("tmux review-7 send-keys") == [
        f"tmux review-7 send-keys -t =review-7: -l {_STATUS}",
        "tmux review-7 send-keys -t =review-7: Enter",
        "tmux review-7 send-keys -t =review-7: Enter",
        "tmux review-7 send-keys -t =review-7: Enter",
    ]
    assert not (workflow.notes(7) / "review-7.typed").exists()
    assert (workflow.state / "input-review-7").read_text() == ""


@pytest.mark.parametrize(
    "text", ["Owner draft", _STATUS + " │", _STATUS + " ││", _STATUS + " more"]
)
def test_bordered_owner_text_is_never_submitted_even_with_an_automation_note(
    workflow: Workflow, text: str
) -> None:
    _bordered_input(workflow, text=text)
    (workflow.notes(7) / "review-7.typed").write_text(_STATUS)

    done = workflow.run("status-check", "review-7")

    assert done.returncode == 3
    assert workflow.calls("tmux review-7 send-keys") == []
    assert (workflow.state / "input-review-7").read_text() == text


def test_owner_edits_in_a_bordered_box_during_paste_settling_are_not_submitted(
    workflow: Workflow,
) -> None:
    _bordered_input(workflow)
    visible = workflow.state / "input-review-7"
    (workflow.state / "on-sleep").write_text(f'echo "Owner edited the input" > "{visible}"')

    done = workflow.run("status-check", "review-7")

    assert done.returncode != 0
    assert "input changed before Enter" in done.stderr
    assert workflow.calls("tmux review-7 send-keys -t =review-7: Enter") == []


def test_a_newer_incomplete_box_does_not_authorize_older_automation() -> None:
    screen = "─" * 40 + "\n> previous automation\n" + "─" * 40
    screen += "\nA newer turn.\n" + "─" * 40 + "\n> Owner draft\n"

    done = subprocess.run(
        [
            "bash", "-c", 'source "$1"; screen=$(cat); claude_input "$screen"',
            "bash", str(SCRIPTS / "input-box"),
        ],
        input=screen,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode != 0
    assert done.stdout == ""


@pytest.mark.parametrize("footer", ["rules", "inline"])
def test_wake_last_line_stops_at_the_input_box_before_status_rules(
    workflow: Workflow, footer: str
) -> None:
    _bordered_input(workflow, footer=footer)
    now = 1_790_000_000
    idle = workflow.notes(7) / "review-7.idle"
    os.utime(idle, (now - 3000, now - 3000))

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(now)})

    assert done.returncode == 0, done.stderr
    pending = workflow.repo / ".local/workflow/stuck.pending"
    assert [json.loads(path.read_text())["line"] for path in pending.glob("*.json")] == [
        "idle, no rule: review-7 (50 min, last line: Waiting.)"
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\x1b[39m\u276f\u00a0\x1b[2mgo on\x1b[0m", ""),
        ("\u276f\u00a0wait", "\u00a0wait\n"),
        ("\u276f\u00a0wait\x1b[2m for checks\x1b[0m", "\u00a0wait\n"),
        ("\u276f\u00a0\x1b[2mgo\x1b[22mwait", "\u00a0wait\n"),
        ("\u276f\u00a0\x1b[2mgo\x1b[mwait", "\u00a0wait\n"),
        ("\u276f\u00a0\x1b[2mgo\x1b[0;39mwait", "\u00a0wait\n"),
        ("\u276f\u00a0\x1b[2;39mgo\x1b[39m on\x1b[0m", ""),
        ("\u276f\u00a0\x1b[38;2;12;34;56mwait\x1b[0m", "\u00a0wait\n"),
        ("\u276f\u00a0\x1b[2mgo\n on\x1b[0m", ""),
        ("\u276f\u00a0", ""),
        ("\u276f\u00a0\x1b]8;id=1;https://x.y\x1b\\see\x1b]8;;\x07 it", "\u00a0see it\n"),
        ("\u276f\u00a0\x1b[2mgo on\x1b[0m\x1b]8;;\x1b\\", ""),
    ],
)
def test_only_non_dim_text_counts_as_input(text: str, expected: str) -> None:
    done = subprocess.run(
        ["bash", "-c", 'source "$1"; nonblank_input', "bash", str(SCRIPTS / "input-box")],
        input=text + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == expected


def test_stripping_escapes_leaves_what_a_plain_capture_shows() -> None:
    done = subprocess.run(
        ["bash", "-c", 'source "$1"; strip_ansi', "bash", str(SCRIPTS / "input-box")],
        input="\x1b[38;5;244m\x1b]8;id=a;https://x.y\x1b\\\u2500\u2500\x1b]8;;\x07\x1b[39m ok\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == "\u2500\u2500 ok\n"


@pytest.mark.parametrize("action", ["checks-finished", "resend", "bridge-empty"])
@pytest.mark.parametrize("typed", [False, True])
def test_suggestions_allow_wakes_but_typed_text_still_blocks_them(
    workflow: Workflow, action: str, typed: bool
) -> None:
    workflow.add_worktree(7)
    name = "review-7" if action == "checks-finished" else "ticket-7"
    workflow.running(name)
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / f"{name}.checks-wait").touch()
    rule = "\x1b[90m" + "─" * 40 + "\x1b[0m"
    prompt = "\u276f\u00a0" + ("wait" if typed else "") + "\x1b[2mgo on\x1b[0m"
    output = (
        "\x1b[31m⎿ \x1b]8;id=b;https://claude.ai\x1b\\You've hit your session limit"
        "\x1b]8;;\x1b\\ · resets 4pm\x1b[39m"
        if action == "resend"
        else "Done."
    )
    workflow.screen(f"{output}\n{rule}\n{prompt}\n{rule}\n", name)
    # Behave like capture-pane: escapes are only returned when -e was requested.
    tmux = workflow.bin / "tmux"
    tmux.write_text(
        tmux.read_text().replace(
            '    cat "$screen"',
            '    case " $* " in\n'
            '      *" -e "*) cat "$screen" ;;\n'
            "      *) sed -E 's/\\x1b\\[[0-?]*[ -/]*[@-~]//g' \"$screen\" ;;\n"
            "    esac",
        )
    )

    if action == "bridge-empty":
        done = workflow.run_script(action, name)
    else:
        args = (action, name, "a" * 40) if action == "checks-finished" else (action, name)
        done = workflow.run(*args)

    assert (done.returncode == 0) is not typed, done.stderr
    calls = workflow.calls(f"tmux {name} send-keys")
    if typed or action == "bridge-empty":
        assert calls == []
    else:
        expected = (
            f"The checks on {'a' * 40} have finished. Read them once and go on by the rules: "
            "merge when they all passed, fix or send back when one failed."
            if action == "checks-finished"
            else instruction("Implement pull request 7")
        )
        assert calls == [
            f"tmux {name} send-keys -t ={name}: -l {expected}",
            f"tmux {name} send-keys -t ={name}: Enter",
        ]
