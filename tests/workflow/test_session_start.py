"""`session start` puts one Claude session, in its own worktree, behind one pull request."""

import fcntl
import shutil
import subprocess
from pathlib import Path

import pytest
from workflow_harness import (
    SESSION_LINK,
    Workflow,
    in_scope,
    instruction,
    session_arguments,
)

_WORKER = instruction("Implement pull request 7")
_AUDIT = instruction("Audit pull request 7")

_ALLOWED_VARIABLES = {
    "HOME",
    "USER",
    "SHELL",
    "PATH",
    "LANG",
    "TERM",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
    "WORKFLOW_SESSION",
}
# What the shell that runs the stand-in `tmux` adds by itself.
_SHELL_VARIABLES = {"PWD", "SHLVL", "_", "OLDPWD"}

_HIGH_PRESSURE = "some avg10=30.00 avg60=10.01 avg300=1.00 total=1\n"
_PRESSURE_AT_LIMIT = "some avg10=30.00 avg60=10.00 avg300=1.00 total=1\n"
_LOW_PRESSURE = "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
_SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"


def _session_environment(workflow: Workflow, name: str) -> dict[str, str]:
    lines = (workflow.state / "env" / name).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def _session_arguments(workflow: Workflow, name: str) -> list[str]:
    return session_arguments(workflow.state, name)


def test_a_worker_gets_a_worktree_on_the_branch_of_its_pull_request(workflow: Workflow) -> None:
    head = workflow.add_pull_request(7)

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    worktree = workflow.worktree(7)
    assert workflow.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree) == workflow.branch(7)
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == head


def test_a_new_worktree_is_made_able_to_test_and_push(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    workflow.run("start", "worker", "7")

    worktree = workflow.worktree(7)
    assert workflow.calls("worktree-setup") == [f"worktree-setup  in {worktree}"]


def test_no_session_starts_when_the_worktree_cannot_be_prepared(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "fail-worktree-setup").touch()

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert not workflow.is_running("ticket-7")


def test_a_worker_is_a_claude_session_told_to_implement_the_pull_request(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)

    workflow.run("start", "worker", "7")

    assert _session_arguments(workflow, "ticket-7") == [
        "-d",
        "-x",
        "200",
        "-y",
        "50",
        "-s",
        "ticket-7",
        "-c",
        str(workflow.worktree(7)),
        f'claude --effort medium --remote-control ticket-7 "{_WORKER}"',
    ]


def test_a_audit_is_a_session_of_its_own_in_the_same_worktree(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    done = workflow.run("start", "audit", "7")

    assert done.returncode == 0, done.stderr
    assert _session_arguments(workflow, "audit-7") == [
        "-d",
        "-x",
        "200",
        "-y",
        "50",
        "-s",
        "audit-7",
        "-c",
        str(workflow.worktree(7)),
        f'claude --effort high --model opus --remote-control audit-7 "{_AUDIT}"',
    ]
    assert not workflow.is_running("ticket-7")


@pytest.mark.parametrize(
    ("kind", "name", "sentence"),
    [("worker", "ticket-7", _WORKER), ("audit", "audit-7", _AUDIT)],
    ids=["worker", "audit"],
)
def test_a_shell_hands_claude_the_whole_sentence_as_one_argument(
    workflow: Workflow, kind: str, name: str, sentence: str
) -> None:
    """`tmux` runs the command through a shell, which must read nothing in the sentence."""
    workflow.add_pull_request(7)
    workflow.run("start", kind, "7")
    command = _session_arguments(workflow, name)[-1]

    read = subprocess.run(
        ["bash", "-c", f'claude() {{ printf "%s\\n" "$@"; }}\n{command}'],
        capture_output=True,
        text=True,
        check=True,
    )

    assert read.stdout.splitlines()[-1] == sentence
    assert read.stderr == ""


def test_the_rules_the_sentence_names_are_the_workflow_rules() -> None:
    root = Path(__file__).resolve().parents[2]
    commands = "git fetch origin main && git show origin/main:AGENTS.md"

    assert _WORKER.endswith(f"read them with: {commands}")
    assert _AUDIT.endswith(f"read them with: {commands}")
    rules = (root / "scripts/workflow/RULES.md").read_text()
    assert "`git fetch origin main`, then `git show origin/main:AGENTS.md`" in rules


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_the_effort_level_named_at_the_start_is_handed_to_claude(
    workflow: Workflow, effort: str
) -> None:
    workflow.add_pull_request(7)

    done = workflow.run("start", "audit", "7", effort)

    assert done.returncode == 0, done.stderr
    assert _session_arguments(workflow, "audit-7")[-1] == (
        f'claude --effort {effort} --model opus --remote-control audit-7 "{_AUDIT}"'
    )


@pytest.mark.parametrize("model", ["opus", "sonnet"])
def test_the_model_named_at_the_start_is_handed_to_claude(workflow: Workflow, model: str) -> None:
    workflow.add_pull_request(7)

    done = workflow.run("start", "worker", "7", "low", model)

    assert done.returncode == 0, done.stderr
    assert _session_arguments(workflow, "ticket-7")[-1] == (
        f'claude --effort low --model {model} --remote-control ticket-7 "{_WORKER}"'
    )


def test_the_session_sees_only_the_listed_variables(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    workflow.run(
        "start",
        "worker",
        "7",
        env={"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDECODE": "1", "SECRET_TOKEN": "x"},
    )

    seen = _session_environment(workflow, "ticket-7")
    # TMUX_TMPDIR is there because this test's caller has it: see the test below.
    assert set(seen) - _SHELL_VARIABLES == _ALLOWED_VARIABLES | {"TMUX_TMPDIR"}
    assert seen["WORKFLOW_SESSION"] == "ticket-7"
    assert seen["HOME"] == workflow.env["HOME"]
    gh_bin = workflow.script.parent / "gh-bin"
    assert seen["PATH"] == f"{gh_bin}:{workflow.env['PATH']}"


def test_a_audit_session_is_told_it_is_a_audit(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    workflow.run("start", "audit", "7")

    assert _session_environment(workflow, "audit-7")["WORKFLOW_SESSION"] == "audit-7"


def test_the_session_link_is_kept_in_the_worktree_for_the_push(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    done = workflow.run("start", "worker", "7")

    link_file = workflow.worktree(7) / ".local" / "workflow" / "ticket-7.url"
    assert link_file.read_text() == SESSION_LINK + "\n"
    assert SESSION_LINK in done.stdout


def test_the_link_is_found_after_it_scrolled_off_the_screen(workflow: Workflow) -> None:
    """A tall status line pushes the line with the link out of the visible window."""
    workflow.add_pull_request(7)
    workflow.screen("status line\n" * 20)
    workflow.scrollback(f"Welcome\n  Remote Control: {SESSION_LINK}\n", "ticket-7")

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    link_file = workflow.worktree(7) / ".local" / "workflow" / "ticket-7.url"
    assert link_file.read_text() == SESSION_LINK + "\n"


def test_a_start_without_a_session_link_stops_the_session_and_shows_the_screen(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    workflow.screen("Do you trust the files in this folder?\n")

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert "Do you trust the files in this folder?" in done.stderr
    assert not workflow.is_running("ticket-7")
    assert not (workflow.worktree(7) / ".local" / "workflow" / "ticket-7.url").exists()
    assert workflow.calls("sleep") == ["sleep 1"] * 60


def test_a_session_that_ends_at_once_is_reported_without_waiting(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.screen("")
    (workflow.state / "dies-at-once").touch()

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert "printed no session link" in done.stderr
    assert workflow.calls("sleep") == []


def test_the_link_is_waited_for_until_it_appears(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.screen("starting\n")
    (workflow.state / "on-sleep").write_text(
        f'echo "{SESSION_LINK}" > "{workflow.state}/screen/default"\n'
    )

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("sleep") == ["sleep 1"]
    assert workflow.is_running("ticket-7")


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_closed_pull_request_gets_no_session(workflow: Workflow, state: str) -> None:
    workflow.add_pull_request(7, state=state)

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert "pull request 7 is closed" in done.stderr
    assert not workflow.is_running("ticket-7")
    assert not workflow.worktree(7).exists()


def test_a_pull_request_that_cannot_be_read_gets_no_session(workflow: Workflow) -> None:
    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert "cannot read pull request 7" in done.stderr
    assert not workflow.is_running("ticket-7")


@pytest.mark.parametrize(
    ("kind", "running"),
    [
        ("worker", "ticket-7"),
        ("worker", "audit-7"),
        ("audit", "ticket-7"),
        ("audit", "audit-7"),
    ],
)
def test_a_worker_and_a_audit_never_share_the_worktree(
    workflow: Workflow, kind: str, running: str
) -> None:
    workflow.add_pull_request(7)
    workflow.running(running)

    done = workflow.run("start", kind, "7")

    assert done.returncode != 0
    assert f"{running} is already running" in done.stderr
    assert workflow.calls("tmux ticket-7 new-session") == []
    assert workflow.calls("tmux audit-7 new-session") == []
    assert not workflow.worktree(7).exists()


def test_a_socket_left_by_a_dead_server_does_not_block_a_start(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.socket("ticket-7")

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")


def test_a_branch_checked_out_elsewhere_is_refused_and_left_alone(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    elsewhere = workflow.repo / ".claude" / "worktrees" / "pr-7"
    workflow.git("fetch", "--quiet", "origin", workflow.branch(7))
    workflow.git(
        "worktree", "add", "--quiet", "-b", workflow.branch(7), str(elsewhere), "FETCH_HEAD"
    )

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert f"already checked out in {elsewhere}" in done.stderr
    assert elsewhere.is_dir()
    assert not workflow.worktree(7).exists()
    assert not workflow.is_running("ticket-7")


def test_an_existing_worktree_is_used_as_it_is(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    (worktree / "notes.txt").write_text("work in progress\n")

    done = workflow.run("start", "audit", "7")

    assert done.returncode == 0, done.stderr
    assert (worktree / "notes.txt").read_text() == "work in progress\n"
    assert _session_arguments(workflow, "audit-7")[8] == str(worktree)
    # Prepared again: this repairs a worktree whose first setup failed half way.
    assert workflow.calls("worktree-setup") == [f"worktree-setup  in {worktree}"]


def test_a_worktree_folder_on_another_branch_is_refused(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.git("worktree", "add", "--quiet", "-b", "other", str(workflow.worktree(7)), "main")

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert "other" in done.stderr
    assert not workflow.is_running("ticket-7")


def test_a_start_waits_while_memory_pressure_is_above_ten(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.pressure_file.write_text(_HIGH_PRESSURE)
    (workflow.state / "on-sleep").write_text(
        f'[ "$1" = 60 ] && printf "{_LOW_PRESSURE.strip()}\\n" > "{workflow.pressure_file}"\n'
    )

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("sleep") == ["sleep 60"]
    calls = workflow.calls()
    assert calls.index("sleep 60") < calls.index(
        next(call for call in calls if call.startswith("tmux ticket-7 new-session"))
    )


def test_pressure_of_exactly_ten_does_not_delay_a_start(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.pressure_file.write_text(_PRESSURE_AT_LIMIT)

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("sleep") == []


def test_a_start_waits_while_the_swap_file_is_more_than_half_full(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.swaps_file.write_text(_SWAPS_HEADER + "/swapfile file 1000 501 10\n")
    (workflow.state / "on-sleep").write_text(
        f'printf "Filename\\n/swapfile file 1000 500 10\\n" > "{workflow.swaps_file}"\n'
    )

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("sleep") == ["sleep 60"]


def test_full_compressed_memory_does_not_delay_a_start(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.swaps_file.write_text(
        _SWAPS_HEADER + "/dev/zram0 partition 1000 1000 100\n/swapfile file 1000 0 10\n"
    )

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("sleep") == []


def test_the_wait_between_memory_checks_can_be_set(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.pressure_file.write_text(_HIGH_PRESSURE)
    (workflow.state / "on-sleep").write_text(
        f'printf "{_LOW_PRESSURE.strip()}\\n" > "{workflow.pressure_file}"\n'
    )

    workflow.run("start", "worker", "7", env={"WORKFLOW_WAIT_SECONDS": "7"})

    assert workflow.calls("sleep") == ["sleep 7"]


def test_a_load_at_the_ceiling_does_not_delay_a_start(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    # 1.5 times the 4 processors; the 1-minute and 15-minute figures do not count.
    workflow.loadavg_file.write_text("40.00 6.00 40.00 1/100 1234\n")

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("sleep") == []
    assert workflow.is_running("ticket-7")


def test_a_start_waits_while_the_load_is_above_the_ceiling(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.loadavg_file.write_text("1.00 6.01 1.00 1/100 1234\n")
    (workflow.state / "on-sleep").write_text(
        f'printf "1.00 5.00 1.00 1/100 1234\\n" > "{workflow.loadavg_file}"\n'
    )

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert (
        "the processors are overloaded: load 6.01 over 5 minutes, ceiling 6"
        " (1.5 times 4 processors); looking again in 60 seconds"
    ) in done.stderr
    assert workflow.calls("sleep") == ["sleep 60"]
    calls = workflow.calls()
    assert calls.index("sleep 60") < calls.index(
        next(call for call in calls if call.startswith("tmux ticket-7 new-session"))
    )


@pytest.mark.parametrize(
    ("kind", "name"), [("worker", "ticket-7"), ("review", "review-7"), ("audit", "audit-7")]
)
def test_a_start_the_tick_makes_while_the_load_is_high_is_tried_next_tick(
    workflow: Workflow, kind: str, name: str
) -> None:
    workflow.add_pull_request(7, draft=kind == "worker")
    workflow.loadavg_file.write_text("1.00 9.00 1.00 1/100 1234\n")

    done = workflow.run("start", kind, "7", env={"WORKFLOW_DEFER_START": "1"})

    assert done.returncode == 75
    assert "load 9.00 over 5 minutes, ceiling 6" in done.stderr
    assert "the next tick tries this start again" in done.stderr
    assert workflow.calls("sleep") == []
    assert not workflow.is_running(name)


def test_a_load_ceiling_that_is_not_a_number_is_refused(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    done = workflow.run("start", "worker", "7", env={"WORKFLOW_LOAD_PER_PROCESSOR": "abc"})

    assert done.returncode not in (0, 75)
    assert "WORKFLOW_LOAD_PER_PROCESSOR" in done.stderr
    assert workflow.calls("sleep") == []
    assert not workflow.is_running("ticket-7")


def test_a_bad_load_ceiling_does_not_stop_the_listing_the_tick_reads(
    workflow: Workflow,
) -> None:
    done = workflow.run("list", env={"WORKFLOW_LOAD_PER_PROCESSOR": "abc"})

    assert done.returncode == 0, done.stderr
    assert "WORKFLOW_LOAD_PER_PROCESSOR" not in done.stderr


@pytest.mark.parametrize(("processors", "waits"), [(4, True), (8, False)])
def test_the_load_ceiling_scales_with_the_processors(
    workflow: Workflow, processors: int, waits: bool
) -> None:
    workflow.add_pull_request(7)
    workflow.fake("nproc", f"#!/bin/bash\necho {processors}\n")
    workflow.loadavg_file.write_text("1.00 10.00 1.00 1/100 1234\n")

    done = workflow.run("start", "worker", "7", env={"WORKFLOW_DEFER_START": "1"})

    assert done.returncode == (75 if waits else 0), done.stderr
    assert workflow.is_running("ticket-7") is not waits


def test_an_unreadable_load_does_not_hold_a_start(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.loadavg_file.unlink()

    done = workflow.run("start", "worker", "7", env={"WORKFLOW_DEFER_START": "1"})

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")


def test_the_load_is_said_with_its_ceiling(workflow: Workflow) -> None:
    workflow.loadavg_file.write_text("1.00 7.25 1.00 1/100 1234\n")

    done = workflow.run("load")

    assert done.returncode == 0, done.stderr
    assert (
        done.stdout == "load 7.25 over 5 minutes, ceiling 6 on 4 processors; above it starts wait\n"
    )


def test_an_unreadable_load_is_said(workflow: Workflow) -> None:
    workflow.loadavg_file.unlink()

    assert workflow.run("load").stdout == "load could not be read\n"


def test_a_start_first_removes_what_is_finished(workflow: Workflow) -> None:
    finished = workflow.add_worktree(3, state="MERGED")
    workflow.add_pull_request(7)

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert not finished.exists()
    assert workflow.is_running("ticket-7")


@pytest.mark.parametrize(
    "arguments",
    [
        ("start", "worker", "7a"),
        ("start", "worker", ""),
        ("start", "worker", "-7"),
        ("start", "worker"),
        ("start", "builder", "7"),
        ("start",),
        (),
        ("launch", "worker", "7"),
    ],
)
def test_a_start_that_is_not_a_kind_and_a_number_is_rejected(
    workflow: Workflow, arguments: tuple[str, ...]
) -> None:
    workflow.add_pull_request(7)

    done = workflow.run(*arguments)

    assert done.returncode != 0
    assert "usage" in done.stderr.lower()
    assert workflow.calls() == []


def test_marks_left_by_an_earlier_session_of_the_same_name_are_removed(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7)
    notes = worktree / ".local" / "workflow"
    notes.mkdir(parents=True)
    for mark in ("idle", "waiting", "pushed"):
        (notes / f"ticket-7.{mark}").touch()
    (notes / "audit-7.pushed").touch()
    (notes / "ticket-7.url").write_text("https://claude.ai/code/session_old\n")
    (notes / "ticket-7.limits").write_text("memory.max max, memory.swap.max max\n")

    workflow.run("start", "worker", "7")

    assert sorted(path.name for path in notes.iterdir()) == [
        "audit-7.pushed",
        "ticket-7.limits",
        "ticket-7.url",
    ]
    assert (notes / "ticket-7.url").read_text() == SESSION_LINK + "\n"
    # Written again by this session's own scope as it started.
    assert (notes / "ticket-7.limits").read_text() == "in place\n"


def test_a_new_worktree_starts_at_the_head_of_the_pull_request_not_at_a_stale_local_branch(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    workflow.git("branch", workflow.branch(7), "main")
    head = workflow.push_from_elsewhere(7)

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == head


def test_a_local_branch_with_commits_of_its_own_is_not_moved(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.git("branch", workflow.branch(7), "main")
    workflow.git("worktree", "add", "--quiet", str(workflow.root / "scratch"), workflow.branch(7))
    workflow.git(
        "commit", "--quiet", "--allow-empty", "-m", "fix: local only", cwd=workflow.root / "scratch"
    )
    local = workflow.git("rev-parse", workflow.branch(7))
    workflow.git("worktree", "remove", str(workflow.root / "scratch"))
    workflow.push_from_elsewhere(7)

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == local
    assert "has commits that are not on GitHub" in done.stderr


def test_a_branch_that_is_not_on_the_remote_is_refused(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.git("push", "--quiet", "origin", "--delete", workflow.branch(7))

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert f"cannot fetch {workflow.branch(7)} from origin" in done.stderr
    assert not workflow.worktree(7).exists()
    assert not workflow.is_running("ticket-7")


def test_a_plain_folder_where_the_worktree_belongs_is_refused(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.worktree(7).mkdir(parents=True)

    done = workflow.run("start", "worker", "7")

    assert done.returncode != 0
    assert "exists and is not a worktree" in done.stderr
    assert not workflow.is_running("ticket-7")


def test_a_worktree_whose_folder_was_deleted_by_hand_is_made_again(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    shutil.rmtree(worktree)

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert (worktree / "scripts" / "workflow" / "session").is_file()
    assert workflow.is_running("ticket-7")


def test_a_start_is_not_delayed_where_memory_cannot_be_measured(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.pressure_file.unlink()
    workflow.swaps_file.unlink()

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("sleep") == []


@pytest.mark.parametrize(
    "arguments",
    [
        ("start", "worker", "7", "8"),
        ("start", "audit", "7", "high", "sonnet"),
        ("start", "worker", "7", "fast"),
        ("start", "worker", "7", "slow"),
        ("start", "worker", "7", "higher"),
        ("start", "worker", "7", "high", "xopus"),
        ("start", "worker", "7", "high", "sonnets"),
        ("start", "worker", "7", "sonnet"),
        ("start", "worker", "7", "high", "haiku"),
        ("start", "worker", "7", "high", "high"),
        ("start", "worker", "7", "high", "opus", "now"),
        ("start", "worker", "7", "", "opus"),
        ("list", "ticket-7"),
        ("cleanup", "ticket-7"),
        ("stop", "ticket-7", "audit-7"),
        ("squeeze",),
        ("protect",),
        ("unprotect", "ticket-7", "now"),
    ],
)
def test_too_many_or_too_few_words_are_rejected(
    workflow: Workflow, arguments: tuple[str, ...]
) -> None:
    workflow.running("ticket-7")

    done = workflow.run(*arguments)

    assert done.returncode != 0
    assert "usage" in done.stderr.lower()
    assert workflow.calls() == []
    assert workflow.is_running("ticket-7")


def test_the_folder_of_tmux_sockets_reaches_the_session_only_when_the_caller_has_one(
    workflow: Workflow,
) -> None:
    """Otherwise the new server would listen where `list` and `stop` do not look."""
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    elsewhere = str(workflow.root / "sockets")
    del workflow.env["TMUX_TMPDIR"]

    workflow.run("start", "worker", "7")
    workflow.run("start", "worker", "8", env={"TMUX_TMPDIR": elsewhere})

    assert "TMUX_TMPDIR" not in _session_environment(workflow, "ticket-7")
    assert _session_environment(workflow, "ticket-8")["TMUX_TMPDIR"] == elsewhere


@pytest.mark.parametrize("kind", ["worker", "audit"])
def test_a_second_start_for_the_same_pull_request_is_refused_while_the_first_prepares(
    workflow: Workflow, kind: str
) -> None:
    """Preparing a worktree takes minutes; both starts would pass the check for a session."""
    workflow.add_pull_request(7)
    lock = workflow.repo / ".local" / "workflow" / "start-7.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = workflow.run("start", kind, "7")

    assert done.returncode != 0
    assert "another start for pull request 7 is under way" in done.stderr
    assert workflow.calls("worktree-setup") == []
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("audit-7")


def test_a_start_for_another_pull_request_is_not_held_up(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    lock = workflow.repo / ".local" / "workflow" / "start-7.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = workflow.run("start", "worker", "8")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-8")


@pytest.mark.parametrize("kind", ["worker", "audit", "review"])
def test_every_session_runs_in_a_scope_of_its_own_with_memory_limits(
    workflow: Workflow, kind: str
) -> None:
    """A runaway test run is killed inside its own session; the others go on."""
    workflow.add_pull_request(7)

    done = workflow.run("start", kind, "7")

    assert done.returncode == 0, done.stderr
    name = {"worker": "ticket-7", "audit": "audit-7", "review": "review-7"}[kind]
    arguments = (workflow.state / "args" / name).read_text().splitlines()
    assert in_scope(arguments, name)[-1].startswith("claude --effort ")


def test_the_memory_limits_can_be_set_for_a_test(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    workflow.run(
        "start",
        "worker",
        "7",
        env={"WORKFLOW_SESSION_MEMORY_MAX": "64M", "WORKFLOW_SESSION_SWAP_MAX": "0"},
    )

    arguments = (workflow.state / "args" / "ticket-7").read_text().splitlines()
    assert in_scope(arguments, "ticket-7", "64M", "0")[-1].startswith("claude --effort ")


_ANALYST = "analyst-day-2026-10-24"
_ANALYSIS_RULES = "by the section Analyse the workflow of AGENTS.md in this worktree"


def _claude_words(workflow: Workflow, name: str) -> list[str]:
    """The words Claude is given, as the shell `tmux` runs the command through reads them."""
    command = _session_arguments(workflow, name)[-1]
    read = subprocess.run(
        ["bash", "-c", f'claude() {{ printf "%s\\n" "$@"; }}\n{command}'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert read.stderr == ""
    return read.stdout.splitlines()


@pytest.mark.parametrize(
    ("days", "span"),
    [((), "the last day"), (("1",), "the last day"), (("7",), "the last 7 days")],
    ids=["one day by default", "one day", "a week"],
)
def test_an_analyst_is_an_opus_session_at_medium_effort_told_to_analyse_the_workflow(
    workflow: Workflow, days: tuple[str, ...], span: str
) -> None:
    done = workflow.run("start", "analyst", "day-2026-10-24", *days)

    assert done.returncode == 0, done.stderr
    report = workflow.analysis_report("day-2026-10-24")
    worktree = workflow.analyst_worktree("day-2026-10-24")
    assert _session_arguments(workflow, _ANALYST)[:9] == [
        "-d",
        "-x",
        "200",
        "-y",
        "50",
        "-s",
        _ANALYST,
        "-c",
        str(worktree),
    ]
    assert _claude_words(workflow, _ANALYST) == [
        "--effort",
        "medium",
        "--model",
        "opus",
        "--add-dir",
        str(workflow.repo / ".local" / "analyses"),
        "--add-dir",
        str(workflow.repo / ".local" / "workflow"),
        "--add-dir",
        str(workflow.root / "home" / ".local" / "state" / "workflow"),
        "--remote-control",
        _ANALYST,
        f"Analyse the workflow over {span}, as {_ANALYST}, {_ANALYSIS_RULES}",
    ]
    assert _session_environment(workflow, _ANALYST)["WORKFLOW_ANALYSIS_REPORT"] == str(report)
    assert _session_environment(workflow, _ANALYST)["WORKFLOW_SESSION"] == _ANALYST
    assert report.is_dir()
    assert (worktree / ".local" / "workflow" / f"{_ANALYST}.url").read_text() == (
        SESSION_LINK + "\n"
    )
    assert (worktree / ".local" / "workflow" / f"{_ANALYST}.started").exists()
    assert f"{_ANALYST} started: {SESSION_LINK}" in done.stdout


def test_an_analyst_works_in_a_copy_of_main_on_no_branch_and_sets_nothing_up(
    workflow: Workflow,
) -> None:
    """It reads and writes its report: nothing in its worktree is tested or pushed."""
    done = workflow.run("start", "analyst", "day-2026-10-24")

    assert done.returncode == 0, done.stderr
    worktree = workflow.analyst_worktree("day-2026-10-24")
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == workflow.git("rev-parse", "main")
    assert workflow.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree) == "HEAD"
    assert workflow.calls("worktree-setup") == []


def test_an_analysis_whose_report_folder_exists_is_not_started_again(workflow: Workflow) -> None:
    workflow.analysis_report("day-2026-10-24").mkdir(parents=True)

    done = workflow.run("start", "analyst", "day-2026-10-24")

    assert done.returncode != 0
    assert "has a report already" in done.stderr
    assert not workflow.is_running(_ANALYST)


def test_an_analyst_that_prints_no_link_is_stopped_and_leaves_no_report_folder(
    workflow: Workflow,
) -> None:
    workflow.screen("Do you trust the files in this folder?\n")

    done = workflow.run("start", "analyst", "day-2026-10-24")

    assert done.returncode != 0
    assert not workflow.is_running(_ANALYST)
    assert not workflow.analysis_report("day-2026-10-24").exists()


@pytest.mark.parametrize(
    "words",
    [
        ("analyst",),
        ("analyst", "Day_1"),
        ("analyst", "day-1", "8"),
        ("analyst", "day-1", "0"),
        ("analyst", "day-1", "week"),
        ("analyst", "day-1", "1", "opus"),
    ],
)
def test_an_analyst_start_with_wrong_words_is_rejected(
    workflow: Workflow, words: tuple[str, ...]
) -> None:
    done = workflow.run("start", *words)

    assert done.returncode == 2
    assert "session start analyst <name> [1-7]" in done.stderr
    assert workflow.calls("tmux") == []


def test_an_analyst_is_listed_stopped_and_its_worktree_removed_once_it_ended(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    workflow.running(_ANALYST)
    report = workflow.analysis_report("day-2026-10-24")
    (report / "report.md").write_text("# Analysis\n")

    listed = workflow.run("list")
    stopped = workflow.run("stop", _ANALYST)
    cleaned = workflow.run("cleanup", "hunts")

    assert listed.stdout.startswith(f"{_ANALYST}  busy  0 MB held")
    assert stopped.returncode == 0, stopped.stderr
    assert not workflow.is_running(_ANALYST)
    assert cleaned.returncode == 0, cleaned.stderr
    assert f"{_ANALYST}: removed, the analysis has ended; its report is in {report}" in (
        cleaned.stdout
    )
    assert not worktree.exists()
    assert (report / "report.md").read_text() == "# Analysis\n"


def test_a_running_analyst_keeps_its_worktree(workflow: Workflow) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    workflow.running(_ANALYST)

    done = workflow.run("cleanup", "hunts")

    assert done.returncode == 0, done.stderr
    assert worktree.exists()


def test_an_analyst_is_never_sent_its_instruction_again(workflow: Workflow) -> None:
    """Its 40 minutes run on while the usage limit stops it."""
    workflow.add_analyst("day-2026-10-24")
    workflow.running(_ANALYST)

    done = workflow.run("resend", _ANALYST)

    assert done.returncode != 0
    assert "an analyst is not sent its instruction again" in done.stderr


def test_an_analyst_that_tmux_refuses_leaves_no_report_folder(workflow: Workflow) -> None:
    (workflow.state / f"refuse-launch-{_ANALYST}").touch()

    done = workflow.run("start", "analyst", "day-2026-10-24")

    assert done.returncode != 0
    assert f"{_ANALYST} could not be started" in done.stderr
    assert not workflow.analysis_report("day-2026-10-24").exists()


@pytest.mark.parametrize("reason", ["another start holds the lock", "the fetch fails"])
def test_an_analyst_start_refused_for_a_passing_reason_leaves_nothing_and_says_try_later(
    workflow: Workflow, reason: str
) -> None:
    """Exit 75, as a start deferred for memory: the tick tries the day's start again."""
    lock_path = workflow.repo / ".local" / "workflow" / "start-analyst.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        if reason == "another start holds the lock":
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            workflow.fake(
                "git", '#!/bin/bash\n[[ $1 == fetch ]] && exit 128\nexec /usr/bin/git "$@"\n'
            )
        done = workflow.run("start", "analyst", "day-2026-10-24")

    assert done.returncode == 75, done.stderr
    assert not [call for call in workflow.calls("tmux") if " new-session " in call]
    assert not workflow.analysis_report("day-2026-10-24").exists()
    assert not workflow.analyst_worktree("day-2026-10-24").exists()
