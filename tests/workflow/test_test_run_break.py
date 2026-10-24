"""Breaking tests for pull request 21: `test-run` keeps the shell's job notice quiet.

Each test tries to make the shell of `test-run` print a line of its own, or to lose the
command's own error output, status or descriptors, now that the shell's stderr is `/dev/null`
while the command runs.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from workflow_harness import SCRIPTS


def _env(tmp_path: Path, delay: str | None, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "WORKFLOW_SESSION"}
    env["WORKFLOW_SESSION"] = "ticket-7"
    if delay is not None:
        slow = tmp_path / "slow.sh"
        slow.write_text(f"trap 'sleep {delay}' DEBUG\n")
        env["BASH_ENV"] = str(slow)
    return {**env, **extra}


def _run(
    tmp_path: Path, *command: str, delay: str | None = None, **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPTS / "test-run"), *command],
        env=_env(tmp_path, delay, **extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("delay", [None, "0.001", "0.01", "0.05"])
def test_many_killed_commands_at_many_timings_print_nothing(tmp_path: Path, delay: str) -> None:
    """The reap lands at a different point of the script on each timing."""
    printed = []
    for _ in range(8):
        done = _run(tmp_path, "bash", "-c", "kill -9 $$", delay=delay)
        assert done.returncode == 137
        if done.stderr:
            printed.append(done.stderr)
    assert printed == []


@pytest.mark.parametrize("name", ["SEGV", "ABRT", "TERM", "USR1", "ALRM"])
def test_a_command_killed_by_another_signal_prints_no_notice(tmp_path: Path, name: str) -> None:
    """Bash names other signals too ("Segmentation fault", "Aborted", "Terminated")."""
    number = signal.Signals[f"SIG{name}"].value
    done = _run(tmp_path, "bash", "-c", f"ulimit -c 0; kill -{name} $$", delay="0.01")

    assert done.returncode == 128 + number
    assert done.stderr == ""


def test_the_command_s_own_error_output_before_its_kill_still_reaches_the_caller(
    tmp_path: Path,
) -> None:
    done = _run(tmp_path, "bash", "-c", "echo before >&2; kill -9 $$", delay="0.01")

    assert done.returncode == 137
    assert done.stderr == "before\n"


def test_the_command_holds_no_descriptor_the_run_saved(tmp_path: Path) -> None:
    """The descriptor that holds the script's stderr is not passed on to the command."""
    probe = "echo /proc/$$/fd/*"
    outside = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=30, check=False
    )
    done = _run(tmp_path, "bash", "-c", probe)

    def numbers(text: str) -> set[str]:
        return {path.rsplit("/", 1)[1] for path in text.split()}

    assert done.returncode == 0
    assert numbers(done.stdout) == numbers(outside.stdout)


@pytest.mark.parametrize("number", [3, 10, 11])
def test_a_caller_s_descriptor_reaches_the_command_unchanged(tmp_path: Path, number: int) -> None:
    """Bash picks a free descriptor of 10 or above for the saved stderr: a caller's 10 stays."""
    out = tmp_path / "out"
    caller = f"exec {number}>{out}; bash {SCRIPTS / 'test-run'} bash -c 'echo reached >&{number}'"
    done = subprocess.run(
        ["bash", "-c", caller],
        env=_env(tmp_path, None),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0
    assert done.stderr == ""
    assert out.read_text() == "reached\n"


def test_the_time_limit_line_follows_the_command_s_error_output(tmp_path: Path) -> None:
    done = _run(
        tmp_path, "bash", "-c", "echo mine >&2; sleep 30",
        delay="0.01", WORKFLOW_TEST_RUN_SECONDS="1", WORKFLOW_TEST_RUN_GRACE_SECONDS="1",
    )  # fmt: skip

    assert done.returncode == 124
    lines = done.stderr.splitlines()
    assert lines[0] == "mine"
    assert lines[-1].startswith("stopped: the test run ran longer than its limit of 1 s.")
    assert len(lines) == 2


def test_a_command_that_ignores_the_stop_and_is_killed_at_the_limit_prints_no_notice(
    tmp_path: Path,
) -> None:
    """`timeout` kills itself with SIGKILL: the shell reaps a job killed by a signal."""
    done = _run(
        tmp_path, "bash", "-c", "trap '' TERM; sleep 30",
        delay="0.01", WORKFLOW_TEST_RUN_SECONDS="1", WORKFLOW_TEST_RUN_GRACE_SECONDS="1",
    )  # fmt: skip

    assert done.returncode == 137
    assert len(done.stderr.splitlines()) == 1
    assert done.stderr.startswith("stopped: the test run ran longer than its limit of 1 s.")


@pytest.mark.parametrize("delay", [None, "0.01"])
def test_a_stop_sent_to_the_run_prints_no_notice_of_the_command(
    tmp_path: Path, delay: str | None
) -> None:
    """`timeout` passes the stop on and then kills itself with it: a job killed by a signal."""
    started = tmp_path / "started"
    run = subprocess.Popen(
        ["bash", str(SCRIPTS / "test-run"), "bash", "-c", f"touch {started}; sleep 30"],
        env=_env(tmp_path, delay),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not started.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(0.3)
        run.terminate()
        _, stderr = run.communicate(timeout=30)

        assert run.returncode != 0
        assert "Terminated" not in stderr
        assert "trap - INT QUIT" not in stderr
    finally:
        run.kill()


def test_a_run_inside_a_run_whose_command_is_killed_prints_nothing(tmp_path: Path) -> None:
    inner = f"bash {SCRIPTS / 'test-run'} bash -c 'kill -9 $$'; exit $?"
    done = _run(tmp_path, "bash", "-c", inner, delay="0.01")

    assert done.returncode == 137
    assert done.stderr == ""


def test_a_command_that_is_not_found_still_says_so(tmp_path: Path) -> None:
    """An error of the command's own start goes to the caller, not to `/dev/null`."""
    done = _run(tmp_path, "no-such-command-here")

    assert done.returncode == 127
    assert "no-such-command-here" in done.stderr
