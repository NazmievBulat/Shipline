"""The same commands against the real `tmux`, which the stand-in can only imitate.

Only `claude` is replaced, by a script that prints a session link and stays open, and
`systemd-run`, by one that runs the session's command, except where the real scope is tested.
"""

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from workflow_harness import SESSION_LINK, Workflow, instruction

_WORKER = instruction("Implement pull request 7")

_TMUX = shutil.which("tmux")


def _has_user_scopes() -> bool:
    """Whether this machine can make the scopes sessions run in: a user's systemd with memory."""
    if shutil.which("systemd-run") is None:
        return False
    made = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=1G", "true"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return made.returncode == 0


# Environment prerequisite: requires an installed tmux binary.
pytestmark = pytest.mark.skipif(_TMUX is None, reason="tmux is not installed")  # noqa: TID251


@pytest.fixture
def real_tmux(workflow: Workflow) -> Iterator[Workflow]:
    # A socket path may be about 100 characters at most, so it cannot live in pytest's folder.
    sockets = Path(tempfile.mkdtemp(prefix="wf-"))
    (workflow.bin / "tmux").unlink()
    workflow.fake(
        "claude",
        "#!/bin/bash\n"
        # What the wake asks: no orchestrator is open.
        'if [ "$*" = "agents --json" ]; then echo "[]"; exit 0; fi\n'
        'echo "$WORKFLOW_SESSION: $*" > "@STATE@/claude-ran"\n'
        f'echo "Remote Control: {SESSION_LINK}"\n'
        "exec /bin/sleep 600\n",
    )
    (workflow.bin / "sleep").unlink()
    workflow.env["TMUX_TMPDIR"] = str(sockets)
    try:
        yield workflow
    finally:
        for name in ("ticket-7", "ticket-8", "audit-7", "review-7", "cloud-7"):
            subprocess.run(
                [str(_TMUX), "-L", name, "kill-server"],
                env=workflow.env,
                capture_output=True,
                check=False,
            )
        shutil.rmtree(sockets, ignore_errors=True)


def test_a_cloud_launch_has_a_terminal_and_survives_its_panes_immediate_exit(
    real_tmux: Workflow,
) -> None:
    real_tmux.add_pull_request(7)
    real_tmux.fake(
        "claude",
        "#!/bin/bash\n"
        "[[ -t 0 && -t 1 ]] || { echo 'A terminal is required'; exit 1; }\n"
        "echo 'Created cloud session: Test'\n"
        "echo 'View: https://claude.ai/code/session_01Cloud?from=cli&m=0'\n",
    )

    done = real_tmux.run("start", "worker", "7", "cloud")

    assert done.returncode == 0, done.stderr
    assert "cloud-7 started: https://claude.ai/code/session_01Cloud?from=cli&m=0" in done.stdout
    assert "cloud-7  working" in real_tmux.run("list").stdout
    assert not real_tmux.worktree(7).exists()


def test_a_start_reads_the_link_from_a_real_screen(real_tmux: Workflow) -> None:
    real_tmux.add_pull_request(7)

    done = real_tmux.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    link_file = real_tmux.worktree(7) / ".local" / "workflow" / "ticket-7.url"
    assert link_file.read_text() == SESSION_LINK + "\n"
    assert (real_tmux.state / "claude-ran").read_text() == (
        f"ticket-7: --effort medium --remote-control ticket-7 {_WORKER}\n"
    )


def test_a_real_window_is_200_by_50(real_tmux: Workflow) -> None:
    real_tmux.add_pull_request(7)
    real_tmux.run("start", "worker", "7")

    size = subprocess.run(
        [
            str(_TMUX),
            "-L",
            "ticket-7",
            "display-message",
            "-p",
            "-t",
            "=ticket-7:",
            "#{window_width}x#{window_height}",
        ],
        env=real_tmux.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert size.stdout.strip() == "200x50"


def test_a_link_that_scrolled_off_a_real_screen_is_still_found(real_tmux: Workflow) -> None:
    real_tmux.add_pull_request(7)
    real_tmux.fake(
        "claude",
        "#!/bin/bash\n"
        # One write, so the link is never seen on the screen before the lines push it off.
        f'printf "%s\\n" "Remote Control: {SESSION_LINK}" $(seq 80)\n'
        "exec /bin/sleep 600\n",
    )
    real_tmux.fake("sleep", "#!/bin/bash\nexec /bin/sleep 0.2\n")

    done = real_tmux.run("start", "worker", "7")
    screen = subprocess.run(
        [str(_TMUX), "-L", "ticket-7", "capture-pane", "-p", "-t", "=ticket-7:"],
        env=real_tmux.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert SESSION_LINK not in screen.stdout
    assert done.returncode == 0, done.stderr
    link_file = real_tmux.worktree(7) / ".local" / "workflow" / "ticket-7.url"
    assert link_file.read_text() == SESSION_LINK + "\n"


def test_a_real_session_is_listed_blocks_a_second_start_and_can_be_stopped(
    real_tmux: Workflow,
) -> None:
    real_tmux.add_pull_request(7)
    real_tmux.run("start", "worker", "7")

    listed = real_tmux.run("list")
    second = real_tmux.run("start", "audit", "7")
    stopped = real_tmux.run("stop", "ticket-7")
    listed_after = real_tmux.run("list")

    assert listed.stdout.startswith("ticket-7  busy  "), listed.stderr
    # Not "another start is under way": the running session does not keep the start's lock.
    assert "ticket-7 is already running" in second.stderr
    assert stopped.stdout.splitlines() == ["ticket-7 stopped"]
    assert listed_after.stdout == ""


def test_a_real_session_that_prints_no_link_is_stopped(real_tmux: Workflow) -> None:
    real_tmux.add_pull_request(7)
    real_tmux.fake("claude", '#!/bin/bash\necho "Do you trust this folder?"\nexec /bin/sleep 600\n')
    real_tmux.fake("sleep", "#!/bin/bash\nexec /bin/sleep 0.02\n")

    done = real_tmux.run("start", "worker", "7")

    assert done.returncode != 0
    assert "Do you trust this folder?" in done.stderr
    assert real_tmux.run("list").stdout == ""


def test_a_worker_started_by_a_tick_does_not_keep_the_lock_of_the_tick(
    real_tmux: Workflow,
) -> None:
    """The `tmux` server lives on after the tick; the lock must not live on with it."""
    real_tmux.add_pull_request(7)

    first = real_tmux.run_script("tick")
    asked_by_the_first = len(real_tmux.calls("gh pr list"))
    second = real_tmux.run_script("tick")

    assert first.returncode == 0, first.stderr
    assert real_tmux.run("list").stdout.startswith("ticket-7  busy  ")
    assert second.returncode == 0, second.stderr
    assert "still running" not in second.stdout
    assert len(real_tmux.calls("gh pr list")) == asked_by_the_first + 1


def test_a_tick_reads_audit_finished_from_a_real_screen_and_stops_the_audit(
    real_tmux: Workflow,
) -> None:
    real_tmux.add_pull_request(7, draft=False, labels=())
    real_tmux.fake(
        "claude",
        "#!/bin/bash\n"
        f'echo "Remote Control: {SESSION_LINK}"\n'
        'echo "Labelled and merged."\n'
        'echo "Audit finished."\n'
        "exec /bin/sleep 600\n",
    )
    real_tmux.run("start", "audit", "7")
    notes = real_tmux.worktree(7) / ".local" / "workflow"

    busy = real_tmux.run_script("tick")
    still_listed = real_tmux.run("list").stdout
    (notes / "audit-7.idle").touch()
    idle = real_tmux.run_script("tick")

    assert busy.returncode == 0, busy.stderr
    assert still_listed.startswith("audit-7  busy  ")
    assert "audit-7 stopped" in idle.stdout
    assert real_tmux.run("list").stdout == ""


def test_a_real_session_stopped_on_the_limit_is_listed_and_gets_its_instruction_typed(
    real_tmux: Workflow,
) -> None:
    """The sentence holds `&&` and `;`, which `tmux` must type and not take for its own."""
    real_tmux.add_pull_request(7)
    typed = real_tmux.state / "typed"
    real_tmux.fake(
        "claude",
        "#!/bin/bash\n"
        f'echo "Remote Control: {SESSION_LINK}"\n'
        'echo "  ⎿  You\'ve hit your session limit · resets 4:10pm (America/New_York)"\n'
        # The older limit screen's prompt, empty again once the line is sent, as Claude's is.
        f'read -r -p "> " line; echo "$line" > "{typed}"; printf "> "\n'
        "exec /bin/sleep 600\n",
    )
    real_tmux.run("start", "worker", "7")

    listed = real_tmux.run("list").stdout
    done = real_tmux.run("resend", "ticket-7")
    for _ in range(50):
        if typed.exists() and typed.read_text():
            break
        subprocess.run(["/bin/sleep", "0.1"], check=False)

    assert listed.startswith("ticket-7  limit  ")
    assert listed.endswith("  session limit  resets 4:10pm (America/New_York)\n")
    assert done.returncode == 0, done.stderr
    assert typed.read_text() == f"{_WORKER}\n"
    assert real_tmux.run("list").stdout.startswith("ticket-7  busy  ")


def test_real_codex_and_bridge_are_started_and_stopped_together(real_tmux: Workflow) -> None:
    real_tmux.add_pull_request(7)
    real_tmux.fake("codex", '#!/bin/bash\necho "Interactive Codex"\nexec /bin/sleep 600\n')
    done = real_tmux.run("start", "worker", "7", "medium", "codex")
    try:
        assert done.returncode == 0, done.stderr
        assert "ticket-7  busy" in real_tmux.run("list").stdout
        assert real_tmux.run("start", "review", "7").returncode != 0
        notes = real_tmux.notes(7)
        (notes / "ticket-7.idle").touch()
        real_tmux.list_pull_request(7, draft=False)
        real_tmux.fake(
            "claude",
            '#!/bin/bash\necho "Remote Control: ' + SESSION_LINK + '"\nexec /bin/sleep 600\n',
        )
        # Stop uses the real sockets, and cleanup keeps the still-open PR's worktree.
        assert real_tmux.run("stop", "ticket-7").returncode == 0
        dead = subprocess.run(
            [str(_TMUX), "-L", "codex-7", "has-session", "-t", "=codex-7"],
            env=real_tmux.env,
            capture_output=True,
            check=False,
        )
        assert dead.returncode != 0
    finally:
        subprocess.run(
            [str(_TMUX), "-L", "codex-7", "kill-server"],
            env=real_tmux.env,
            capture_output=True,
            check=False,
        )


# Environment prerequisite: a user's systemd that makes scopes; CI's runners have none.
@pytest.mark.skipif(not _has_user_scopes(), reason="no user systemd to make scopes")  # noqa: TID251
def test_a_runaway_test_run_is_stopped_at_its_session_limit_and_the_others_go_on(
    real_tmux: Workflow,
) -> None:
    """The real kernel and systemd."""
    (real_tmux.bin / "systemd-run").unlink()
    real_tmux.env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    real_tmux.env["WORKFLOW_CGROUP_ROOT"] = "/sys/fs/cgroup"
    real_tmux.env["WORKFLOW_PROC_ROOT"] = "/proc"
    log = real_tmux.state / "run-7.log"
    run = real_tmux.repo / "scripts" / "workflow" / "test-run"
    allocate = real_tmux.state / "allocate.py"
    allocate.write_text("held = []\nwhile True:\n    held.append(b'x' * (8 << 20))\n")
    real_tmux.fake(
        "claude",
        "#!/bin/bash\n"
        'if [ "$*" = "agents --json" ]; then echo "[]"; exit 0; fi\n'
        f'echo "Remote Control: {SESSION_LINK}"\n'
        'if [ "$WORKFLOW_SESSION" = ticket-7 ]; then\n'
        f"  bash {run} python3 {allocate} > {log} 2>&1\n"
        f'  echo "exit $?" >> {log}\n'
        "fi\n"
        "exec /bin/sleep 600\n",
    )
    limits = {"WORKFLOW_SESSION_MEMORY_MAX": "96M", "WORKFLOW_SESSION_SWAP_MAX": "0"}
    for number in (8, 7):
        real_tmux.add_pull_request(number)
        done = real_tmux.run("start", "worker", str(number), env=limits)
        assert done.returncode == 0, done.stderr

    deadline = time.monotonic() + 60
    while "exit " not in (log.read_text() if log.exists() else ""):
        assert time.monotonic() < deadline, "the runaway was not stopped"
        time.sleep(0.2)

    lines = log.read_text().splitlines()
    assert lines[-2] == (
        "stopped: the test run passed its memory limit of 96 MB. A loop or a leak is the likely "
        "cause, not the machine: find it before running again."
    )
    assert lines[-1] == "exit 137"
    # Only the runaway was killed: its own session and the other one still run.
    listed = real_tmux.run("list").stdout.splitlines()
    assert [line.split("  ")[0] for line in listed] == ["ticket-7", "ticket-8"]
    assert all("(limit 96 MB)" in line for line in listed), listed
