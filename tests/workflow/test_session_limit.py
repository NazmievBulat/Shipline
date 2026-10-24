"""A session the usage limit stopped is listed as `limit`, and can be sent its instruction again."""

import pytest
from workflow_harness import Workflow, instruction

_WORKER = instruction("Implement pull request 7")
_AUDIT = instruction("Audit pull request 7")

# The screen of a session whose first instruction met the limit: the instruction, the line
# Claude prints, and the box the owner types in.
_BOX = "\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n  ? for shortcuts\n"


def _stopped(resets: str = " · resets 4:10pm (America/New_York)") -> str:
    return f"> {_WORKER}\n  ⎿  You've hit your session limit{resets}\n{_BOX}"


def _typed(workflow: Workflow, name: str) -> list[str]:
    start = f"tmux {name} send-keys "
    return [call[len(start) :] for call in workflow.calls(start)]


def test_a_session_stopped_on_the_limit_is_not_listed_as_busy(workflow: Workflow) -> None:
    """The stop hook did not run, so there is no idle mark: only the screen says it stopped."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.running("audit-12")
    workflow.screen(_stopped(), "ticket-7")

    done = workflow.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "audit-12  busy  0 MB held  0 MB in swap",
        "ticket-7  limit  0 MB held  0 MB in swap  session limit  resets 4:10pm (America/New_York)",
    ]


@pytest.mark.parametrize(
    ("line", "listed"),
    [
        ("You've hit your session limit · resets 4pm", "  session limit  resets 4pm"),
        (
            "You've hit your weekly limit · resets Oct 15, 1am (America/New_York)",
            "  weekly limit  resets Oct 15, 1am (America/New_York)",
        ),
        # The apostrophe a typesetter would use.
        (
            "You\u2019ve hit your limit · resets 10am (Europe/Berlin)",
            "  limit  resets 10am (Europe/Berlin)",
        ),
        (
            "You've hit your session limit · resets 2:50pm (America/New_York) · progress saved",
            "  session limit  resets 2:50pm (America/New_York)",
        ),
        (
            "You've hit your session limit · resets 2:50pm (America/New_York) "
            "(error type rate_limit, HTTP 429, request id req_011)",
            "  session limit  resets 2:50pm (America/New_York)",
        ),
        ("You've hit your session limit", "  session limit"),
        ("You've hit your session limit · resets 4pm   ", "  session limit  resets 4pm"),
        ("You've hit your session limit · resets 4pm (UTC)", "  session limit  resets 4pm (UTC)"),
        ("● You've hit your Opus limit · resets 4pm", "  Opus limit  resets 4pm"),
    ],
)
def test_the_limit_line_is_read_however_it_names_the_limit_and_the_time(
    workflow: Workflow, line: str, listed: str
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(f"> {_WORKER}\n  {line}\n{_BOX}", "ticket-7")

    done = workflow.run("list")

    assert done.stdout.splitlines() == [f"ticket-7  limit  0 MB held  0 MB in swap{listed}"]


# Screens of review sessions stopped on the limit: Claude now
# prints a line of advice under the limit line, and its spinner after it.
_STOPPED_WITH_ADVICE = (
    f"> {_WORKER}\n"
    "  ⎿  You've hit your session limit · resets 10:30pm (America/New_York)\n"
    "     /upgrade or /usage-credits to finish what you're working on.\n"
    "✻ Baked for 0s · done 9:33 PM\n"
    f"{_BOX}"
)
# The older form of the same stop, without the advice.
_STOPPED_WITHOUT_ADVICE = (
    f"> {_WORKER}\n"
    "  ⎿  You've hit your session limit · resets 10:30pm (America/New_York)\n"
    "✻ Baked for 0s · done 9:33 PM\n"
    f"{_BOX}"
)


@pytest.mark.parametrize("screen", [_STOPPED_WITH_ADVICE, _STOPPED_WITHOUT_ADVICE])
def test_a_limit_line_with_advice_under_it_is_limit_whatever_the_spinner_says(
    workflow: Workflow, screen: str
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(screen, "ticket-7")

    listed = workflow.run("list")
    resent = workflow.run("resend", "ticket-7")

    assert listed.stdout.splitlines() == [
        "ticket-7  limit  0 MB held  0 MB in swap  session limit  resets 10:30pm (America/New_York)"
    ]
    assert resent.returncode == 0, resent.stderr
    assert _typed(workflow, "ticket-7") == [f"-t =ticket-7: -l {_WORKER}", "-t =ticket-7: Enter"]


@pytest.mark.parametrize(
    ("said", "listed"),
    [
        # A narrow pane wraps the advice onto a second line.
        (
            "  ⎿  You've hit your session limit · resets 4pm\n"
            "     /upgrade or /usage-credits to finish what\n"
            "     you're working on.\n",
            "  session limit  resets 4pm",
        ),
        # The spinner may name another state.
        (
            "  ⎿  You've hit your session limit · resets 4pm\n"
            "     /upgrade or /usage-credits to finish what you're working on.\n"
            "✢ Thinking… (esc to interrupt)\n",
            "  session limit  resets 4pm",
        ),
        # Another limit with the same advice.
        (
            "  ⎿  You've hit your weekly limit · resets Oct 20, 9am (America/New_York)\n"
            "     /upgrade or /usage-credits to finish what you're working on.\n",
            "  weekly limit  resets Oct 20, 9am (America/New_York)",
        ),
    ],
)
def test_the_advice_is_read_wrapped_under_any_spinner_and_any_limit(
    workflow: Workflow, said: str, listed: str
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(f"> {_WORKER}\n{said}{_BOX}", "ticket-7")

    done = workflow.run("list")

    assert done.stdout.splitlines() == [f"ticket-7  limit  0 MB held  0 MB in swap{listed}"]


@pytest.mark.parametrize(
    "screen",
    [
        # The advice is Claude's only right under the limit line; anywhere else it was printed.
        "  ⎿  You've hit your session limit · resets 4pm\n\n"
        "     /upgrade or /usage-credits to finish what you're working on.\n" + _BOX,
        # Indented words after the advice and an empty line are not the advice wrapped.
        "  ⎿  You've hit your session limit · resets 4pm\n"
        "     /upgrade or /usage-credits to finish what\n\n"
        "     Reading the description.\n" + _BOX,
        # The session went on after the advice.
        "  ⎿  You've hit your session limit · resets 4pm\n"
        "     /upgrade or /usage-credits to finish what you're working on.\n"
        "● Reading the description.\n" + _BOX,
        # A line that only starts like the advice.
        "  ⎿  You've hit your session limit · resets 4pm\n     /upgraded the lock file.\n" + _BOX,
    ],
)
def test_the_advice_counts_only_right_under_the_limit_line(workflow: Workflow, screen: str) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(screen, "ticket-7")

    done = workflow.run("list")

    assert done.stdout.splitlines() == ["ticket-7  busy  0 MB held  0 MB in swap"]


def test_a_session_stopped_on_the_limit_is_limit_even_with_an_idle_mark(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    (worktree / ".local" / "workflow").mkdir(parents=True)
    (worktree / ".local" / "workflow" / "ticket-7.idle").touch()
    workflow.screen(_stopped(), "ticket-7")

    done = workflow.run("list")

    assert done.stdout.startswith("ticket-7  limit  ")


def test_a_limit_line_is_printed_with_the_memory_unknown(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7", scope="user.slice/session-2.scope")
    workflow.screen(_stopped(), "ticket-7")

    done = workflow.run("list")

    assert done.stdout.splitlines() == [
        "ticket-7  limit  memory unknown  session limit  resets 4:10pm (America/New_York)"
    ]


@pytest.mark.parametrize(
    "screen",
    [
        # The session went on after the limit: something ran.
        f"  ⎿  You've hit your session limit · resets 4pm\n\n> {_WORKER}\n\n✻ Thinking…\n{_BOX}",
        "  ⎿  You've hit your session limit · resets 4pm\n\n● Reading the description.\n" + _BOX,
        # The session is talking about the limit, not stopped by it.
        "● The screen said You've hit your session limit · resets 4pm\n" + _BOX,
        '● It printed "You\'ve hit your session limit · resets 4pm"\n' + _BOX,
        "● `You've hit your session limit · resets 4pm`\n" + _BOX,
        # What the owner typed is not what Claude printed.
        "> You've hit your session limit · resets 4pm\n" + _BOX,
        # Under the box nothing is read.
        f"● Done.\n{_BOX}  You've hit your session limit · resets 4pm\n",
    ],
)
def test_a_session_is_not_limit_when_the_line_is_not_its_last_output(
    workflow: Workflow, screen: str
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(screen, "ticket-7")

    done = workflow.run("list")

    assert done.stdout.splitlines() == ["ticket-7  busy  0 MB held  0 MB in swap"]


def test_a_session_whose_screen_cannot_be_read_is_listed_as_before(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.fake(
        "tmux",
        (workflow.bin / "tmux")
        .read_text()
        .replace("  capture-pane)", "  capture-pane) exit 1 ;;\n  never)"),
    )

    done = workflow.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["ticket-7  busy  0 MB held  0 MB in swap"]


@pytest.mark.parametrize(("name", "sentence"), [("ticket-7", _WORKER), ("audit-7", _AUDIT)])
def test_resend_types_the_instruction_the_session_was_started_with(
    workflow: Workflow, name: str, sentence: str
) -> None:
    workflow.add_worktree(7)
    workflow.running(name)
    workflow.screen(_stopped(), name)

    done = workflow.run("resend", name)

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{name} was sent its instruction again\n"
    assert _typed(workflow, name) == [f"-t ={name}: -l {sentence}", f"-t ={name}: Enter"]


def test_resend_types_nothing_into_a_session_that_is_not_stopped_on_the_limit(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen("● Which limit do you want?\n" + _BOX, "ticket-7")

    done = workflow.run("resend", "ticket-7")

    assert done.returncode == 1
    assert done.stderr == "ticket-7 is not stopped on the usage limit\n"
    assert _typed(workflow, "ticket-7") == []


def test_resend_refuses_a_session_that_does_not_run_and_a_hunt(workflow: Workflow) -> None:
    workflow.add_hunt("phone-1")
    workflow.running("hunt-phone-1")
    workflow.screen(_stopped(), "hunt-phone-1")

    gone = workflow.run("resend", "ticket-7")
    hunt = workflow.run("resend", "hunt-phone-1")

    assert gone.returncode == 1
    assert gone.stderr == "ticket-7 is not running\n"
    assert hunt.returncode == 1
    assert "hunt" in hunt.stderr
    assert _typed(workflow, "hunt-phone-1") == []


def test_resend_that_cannot_type_fails(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(_stopped(), "ticket-7")
    (workflow.state / "send-keys-fails").touch()

    done = workflow.run("resend", "ticket-7")

    assert done.returncode == 1
    assert done.stderr == "the instruction could not be sent to ticket-7\n"


def test_a_stop_forgets_the_reset_the_instruction_was_sent_after(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.notes(7).mkdir(parents=True)
    mark = workflow.notes(7) / "ticket-7.resent"
    mark.write_text("1791922200\n")

    workflow.run("stop", "ticket-7")

    assert not mark.exists()


@pytest.mark.parametrize(
    "between",
    [
        "\n✻ Churned for 5m 12s\n\n> \n",
        "  ⎿  Tip: the limit is shared by every session\n\u276f \n",
        "\n\n\n",
    ],
)
def test_what_claude_draws_itself_between_the_line_and_the_box_does_not_count(
    workflow: Workflow, between: str
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(
        f"> {_WORKER}\n  ⎿  You've hit your session limit · resets 4pm\n{between}{_BOX}", "ticket-7"
    )

    done = workflow.run("list")

    assert done.stdout.startswith("ticket-7  limit  ")


def test_the_last_limit_line_of_a_screen_is_the_one_that_counts(workflow: Workflow) -> None:
    """The screen of a second stop: the first line, the sentence typed again, the new line."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(
        f"> {_WORKER}\n  ⎿  You've hit your session limit · resets 4pm\n\n"
        f"> {_WORKER}\n  ⎿  You've hit your session limit · resets 9pm\n{_BOX}",
        "ticket-7",
    )

    done = workflow.run("list")

    assert done.stdout.splitlines() == [
        "ticket-7  limit  0 MB held  0 MB in swap  session limit  resets 9pm"
    ]


def test_a_rule_in_what_the_session_printed_is_not_taken_for_the_box(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    rule = "─" * 40
    workflow.screen(
        f"● A table:\n{rule}\n  ⎿  You've hit your session limit · resets 4pm\n{_BOX}", "ticket-7"
    )
    workflow.running("audit-7")
    workflow.screen(
        f"● A table:\n{rule}\n● Which limit do you want?\n{_BOX}"
        "  You've hit your session limit · resets 4pm\n",
        "audit-7",
    )

    done = workflow.run("list")

    assert done.stdout.splitlines() == [
        "audit-7  busy  0 MB held  0 MB in swap",
        "ticket-7  limit  0 MB held  0 MB in swap  session limit  resets 4pm",
    ]


def test_a_screen_cut_off_inside_the_box_is_read_above_its_one_rule(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    rule = "─" * 40
    workflow.screen(
        f"  ⎿  You've hit your session limit · resets 4pm\n{rule}\n> half a sentence\n", "ticket-7"
    )

    done = workflow.run("list")

    assert done.stdout.startswith("ticket-7  limit  ")


def test_the_session_listed_after_a_stopped_one_does_not_inherit_its_limit(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7)
    workflow.running("audit-7")
    workflow.screen(_stopped(), "audit-7")
    workflow.running("ticket-9")

    done = workflow.run("list")

    assert done.stdout.splitlines() == [
        "audit-7  limit  0 MB held  0 MB in swap  session limit  resets 4:10pm (America/New_York)",
        "ticket-9  busy  0 MB held  0 MB in swap",
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "> wait, do 9 first",
        "\u276f wait\n  and then 9",
        "  wait",
        "\u276f\u00a0\u043e\u0442\u0432\u0435\u0442",
        "\u276f\u2003wait",
    ],
)
def test_resend_types_nothing_into_a_box_that_holds_an_unsent_text(
    workflow: Workflow, prompt: str
) -> None:
    """The instruction would be added to it, and both sent as one."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    rule = "─" * 40
    workflow.screen(
        f"> {_WORKER}\n  ⎿  You've hit your session limit · resets 4pm\n\n"
        f"{rule}\n{prompt}\n{rule}\n  ? for shortcuts\n",
        "ticket-7",
    )

    listed = workflow.run("list")
    done = workflow.run("resend", "ticket-7")

    assert listed.stdout.startswith("ticket-7  limit  ")
    assert done.returncode == 1
    assert done.stderr == (
        "ticket-7 has text in its box that was not sent; nothing was typed into it\n"
    )
    assert _typed(workflow, "ticket-7") == []


def test_resend_reads_a_box_that_the_screen_cuts_off(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    rule = "─" * 40
    workflow.screen(
        f"  ⎿  You've hit your session limit · resets 4pm\n{rule}\n> half a sentence\n", "ticket-7"
    )

    done = workflow.run("resend", "ticket-7")

    assert done.returncode == 1
    assert _typed(workflow, "ticket-7") == []


@pytest.mark.parametrize("space", ["\u00a0", "\u1680", "\u2003", "\u202f", "\u205f", "\u3000"])
def test_resend_accepts_a_unicode_space_only_prompt(workflow: Workflow, space: str) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(_stopped().replace("> \n", f"\u276f{space}\n \t{space}\n"), "ticket-7")

    done = workflow.run("resend", "ticket-7", env={"LC_ALL": "C"})

    assert done.returncode == 0, done.stderr
    assert _typed(workflow, "ticket-7") == [
        f"-t =ticket-7: -l {_WORKER}",
        "-t =ticket-7: Enter",
    ]
