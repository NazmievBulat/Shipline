"""Breaking tests for a push that outlasts its deadline (pull request 16).

The hook is a pre-push chain run through a package manager, as a repository's hook usually is:
a lint, the web unit tests, the server tests, a security scan and a dependency audit, each a
named script. The security scan is the step that hangs."""

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from workflow_harness import Workflow

STEPS = ("lint", "test:web", "test:server", "security", "audit")


def _gate(workflow: Workflow, code: str, cwd: Path | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g\n"
            + textwrap.dedent(code),
            str(workflow.script.parent),
        ],
        cwd=cwd or workflow.repo,
        env=workflow.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _push(
    workflow: Workflow, deadline: int, *, ref: str = "ticket/pushed", cwd: Path | None = None
) -> subprocess.Popen[str]:
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "feat: pushed by the gate", cwd=cwd)
    return _gate(
        workflow,
        f"""
        import json
        g.COMMAND_TIMEOUT = {deadline}
        try:
            pushed = g.push("origin", "HEAD:refs/heads/{ref}")
        except g.Waiting as error:
            print(json.dumps({{"waiting": type(error).__name__, "reason": str(error)}}))
        else:
            print(json.dumps({{"returncode": pushed.returncode, "refusal": pushed.refusal,
                               "timed_out": pushed.timed_out}}))
        """,
        cwd=cwd,
    )


def _result(process: subprocess.Popen[str]) -> dict:
    stdout, stderr = process.communicate(timeout=90)
    assert process.returncode == 0, stdout + stderr
    return json.loads(stdout)


def _chain(
    workflow: Workflow, steps: dict[str, str], *, run_step: str = '"$script"\n'
) -> None:
    """A pre-push hook that runs the named steps through a stand-in package manager, which
    prints each step's header the way one does, and fails the chain on the first failure."""
    bin_folder = workflow.bin
    runner = bin_folder / "pm"
    runner.write_text(
        "#!/bin/bash\n"
        "# pm run <step>: the step's header, then the step itself.\n"
        'step=$2\nscript="' + str(bin_folder) + '/steps/${step//:/-}"\n'
        "echo\n"
        'echo "> example-app@1.0.0 $step /work/example-app"\n'
        'echo "> sh scripts/${step//:/-}.sh"\n'
        "echo\n" + run_step
    )
    runner.chmod(0o755)
    folder = bin_folder / "steps"
    folder.mkdir(exist_ok=True)
    for name, body in steps.items():
        path = folder / name.replace(":", "-")
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)
    hooks = bin_folder / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(
        "#!/bin/bash\nset -e\n"
        f'echo pre-push >> "{workflow.state}/calls.log"\n'
        + "".join(f"pm run {name}\n" for name in steps)
    )
    hook.chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))


def _daily(scan: str) -> dict[str, str]:
    return {
        "lint": "echo 'eslint: 0 problems'\n",
        "test:web": "echo ' Test Files  48 passed (48)'\necho '      Tests  612 passed (612)'\n",
        "test:server": "echo '412 passed in 31.20s'\n",
        "security": scan,
        "audit": "echo 'No known vulnerabilities found'\n",
    }


HUNG_SCAN = (
    "echo 'semgrep: scanning 812 files with 1062 rules'\n"
    "echo 'semgrep: 640 of 812 files'\n"
    "exec /bin/sleep 30\n"
)


def test_a_hung_security_scan_is_named_with_its_last_lines(workflow: Workflow) -> None:
    """The daily chain hangs in its security scan: the refusal leads with the deadline and
    names the scan's step and its last line, short enough for a small screen."""
    _chain(workflow, _daily(HUNG_SCAN))

    started = time.monotonic()
    pushed = _result(_push(workflow, 5))
    took = time.monotonic() - started

    assert pushed["returncode"] != 0 and pushed["timed_out"] is True, pushed
    lines = pushed["refusal"].splitlines()
    assert lines[0] == "git push timed out after 5s", lines
    assert "> example-app@1.0.0 security /work/example-app" in lines, lines
    assert lines[-1] == "semgrep: 640 of 812 files", lines
    assert len(lines) <= 21, lines
    # The audit after the scan never ran, and nothing waited past the deadline's grace.
    assert "No known vulnerabilities found" not in pushed["refusal"]
    assert took < 5 + 10, took


def test_a_package_manager_that_reports_the_stop_keeps_the_step_named(
    workflow: Workflow,
) -> None:
    """A package manager prints its own failure when the stop reaches it, and git its own: the
    hung step stays in the refusal."""
    trap = (
        "trap 'echo \" ELIFECYCLE  Command failed with signal SIGTERM.\"; exit 143' TERM\n"
        '"$script" &\nwait $!\nexit $?\n'
    )
    _chain(workflow, _daily(HUNG_SCAN), run_step=trap)

    pushed = _result(_push(workflow, 5))

    lines = pushed["refusal"].splitlines()
    assert lines[0] == "git push timed out after 5s", lines
    assert "> example-app@1.0.0 security /work/example-app" in lines, lines
    assert "semgrep: 640 of 812 files" in lines, lines
    assert " ELIFECYCLE  Command failed with signal SIGTERM." in pushed["refusal"], lines


def test_a_scan_that_ignores_the_stop_is_killed_and_still_refused_in_time(
    workflow: Workflow,
) -> None:
    """The scan stalls under memory pressure and ignores SIGTERM: it is killed after the grace,
    and its last line still comes back."""
    scan = (
        "trap '' TERM\n"
        "echo 'gitleaks: scanning 3 commits'\n"
        "/bin/sleep 30 &\nwhile :; do wait $! && break; done\n"
    )
    _chain(workflow, _daily(scan))

    started = time.monotonic()
    pushed = _result(_push(workflow, 5))

    assert time.monotonic() - started < 5 + 10
    lines = pushed["refusal"].splitlines()
    assert lines[0] == "git push timed out after 5s", lines
    assert lines[-1] == "gitleaks: scanning 3 commits", lines
    assert "> example-app@1.0.0 security /work/example-app" in lines, lines


def test_a_scan_child_in_its_own_session_does_not_hold_the_refusal(workflow: Workflow) -> None:
    """A child of the scan starts its own session and keeps the output pipe open."""
    scan = (
        "echo 'semgrep: starting the core'\n"
        "setsid /bin/sleep 30 &\n"
        "exec /bin/sleep 30\n"
    )
    _chain(workflow, _daily(scan))

    started = time.monotonic()
    pushed = _result(_push(workflow, 5))

    assert time.monotonic() - started < 5 + 10
    assert pushed["refusal"].splitlines()[-1] == "semgrep: starting the core", pushed


def test_the_wait_for_a_push_slot_is_not_counted_against_the_deadline(
    workflow: Workflow,
) -> None:
    """Both machine-wide push slots are held by other pushes for 3 s; the gate's push waits,
    then its 3 s hook passes inside a 5 s deadline."""
    _chain(workflow, _daily("/bin/sleep 3\necho 'semgrep: 0 findings'\n"))
    queue = Path(workflow.env["HOME"]) / ".local/state/workflow/pushes"
    queue.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time\n"
                "held = [open(f'{sys.argv[1]}/slot-{n}.lock', 'a+') for n in range(2)]\n"
                "[fcntl.flock(f, fcntl.LOCK_EX) for f in held]\n"
                "print('held', flush=True); time.sleep(3)\n"
            ),
            str(queue),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline() == "held\n"

    pushed = _result(_push(workflow, 5))
    holder.wait(timeout=30)

    assert pushed["returncode"] == 0, pushed


def test_two_pushes_that_hang_at_once_each_keep_their_own_lines(workflow: Workflow) -> None:
    """Two gates push two branches from two checkouts at once and both hang: each refusal is
    its own push's, never the other's."""
    scan = (
        'read -r _ _ remote _ < "$WORKFLOW_PUSH_REFS"\n'
        'echo "semgrep: scanning for $remote"\n'
        "exec /bin/sleep 30\n"
    )
    _chain(workflow, _daily(scan))
    # The hook reads its refs first and hands them to the scan.
    hook = workflow.bin / "hooks" / "pre-push"
    hook.write_text(
        hook.read_text().replace(
            "set -e\n",
            'set -e\nexport WORKFLOW_PUSH_REFS=$(mktemp)\ncat > "$WORKFLOW_PUSH_REFS"\n',
        )
    )
    other = workflow.root / "other"
    workflow.git("worktree", "add", "--quiet", "--detach", str(other), "HEAD")

    first = _push(workflow, 5, ref="ticket/first")
    second = _push(workflow, 5, ref="ticket/second", cwd=other)
    one, two = _result(first), _result(second)

    assert one["refusal"].splitlines()[-1] == "semgrep: scanning for refs/heads/ticket/first", one
    assert two["refusal"].splitlines()[-1] == "semgrep: scanning for refs/heads/ticket/second", two
    assert "ticket/second" not in one["refusal"] and "ticket/first" not in two["refusal"]


def test_a_retry_after_a_memory_shortage_that_hangs_is_refused_with_its_own_lines(
    workflow: Workflow,
) -> None:
    """The scan runs short of locked memory, the gate tries once more a minute later, and that
    second try hangs: the refusal is the deadline and the second try's lines."""
    marker = workflow.state / "first-try"
    scan = (
        f'if [ ! -e "{marker}" ]; then touch "{marker}"\n'
        "  echo 'semgrep: io_uring_queue_init failed: could not lock memory'; exit 1\n"
        "fi\n"
        "echo 'semgrep: second try, 120 of 812 files'\n"
        "exec /bin/sleep 30\n"
    )
    _chain(workflow, _daily(scan))

    pushed = _result(_push(workflow, 5))

    lines = pushed["refusal"].splitlines()
    assert lines[0] == "git push timed out after 5s", lines
    assert lines[-1] == "semgrep: second try, 120 of 812 files", lines
    assert not any("could not lock memory" in line for line in lines), lines
    assert workflow.calls("sleep 60") == ["sleep 60"]
    assert workflow.calls("pre-push") == ["pre-push", "pre-push"]


def test_web_tests_that_print_much_before_the_scan_hangs_leave_a_short_refusal(
    workflow: Workflow,
) -> None:
    """The web unit tests print 50,000 lines, then the scan hangs."""
    steps = _daily(HUNG_SCAN)
    steps["test:web"] = "seq -f ' ✓ src/example-%g.test.ts (3 tests)' 50000\n"
    _chain(workflow, steps)

    pushed = _result(_push(workflow, 5))

    lines = pushed["refusal"].splitlines()
    assert lines[0] == "git push timed out after 5s", lines
    assert len(lines) <= 21 and lines[-1] == "semgrep: 640 of 812 files", lines


@pytest.mark.xfail(
    strict=True, reason="low: with no push checkout only the last 20 lines are kept, as asked"
)
def test_a_scan_with_a_long_banner_keeps_its_step_name(workflow: Workflow) -> None:
    """With no push checkout, a scan that prints more than 20 lines before it hangs."""
    banner = "".join(f"echo '  rule pack {n}: loaded'\n" for n in range(30))
    _chain(workflow, _daily(banner + "exec /bin/sleep 30\n"))

    pushed = _result(_push(workflow, 5))

    lines = pushed["refusal"].splitlines()
    assert lines[0] == "git push timed out after 5s", lines
    assert any("> example-app@1.0.0 security" in line for line in lines), lines


@pytest.mark.xfail(strict=True, reason="low: the same decoding error stops the push on main")
def test_a_scan_stopped_in_the_middle_of_a_character_is_still_refused_as_a_timeout(
    workflow: Workflow,
) -> None:
    """The scan is killed after it wrote the first bytes of a character."""
    scan = "echo 'semgrep: 640 of 812 files'\nprintf '\\xe2\\x9c'\nexec /bin/sleep 30\n"
    _chain(workflow, _daily(scan))

    pushed = _result(_push(workflow, 5))

    assert "returncode" in pushed, pushed
    assert pushed["refusal"].splitlines()[0] == "git push timed out after 5s", pushed
