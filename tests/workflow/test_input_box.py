"""Suggestions are decoration; only the owner's visible input blocks a wake."""

import subprocess

import pytest
from workflow_harness import SCRIPTS, Workflow, instruction


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
