"""Merge safety for github."""

import errno
import fcntl
import json
import os
import signal
import subprocess
import time

import pytest
from workflow_harness import Workflow

from workflow.merge_gate_fixtures import (
    ADVISORY,
    GitHub,
    MEMORY_SHORTAGES,
    _REPO,
    _SUITE_RUNS,
    _batch_call,
    _batch_setup,
    _batch_state,
    _check_runs,
    _check_runs_path,
    _ci_runs,
    _finish_batch,
    _green_rerun_on_old_main,
    _numbers,
    _prefixes,
    _refusal,
    _scratch,
    _scratch_branches,
    _stale,
    _tick_context,
    _train,
    _unlisted_line,
    github,
    hook_runs,
    hook_runs_and_waits,
    short_of_memory,
)


def test_a_failed_github_read_never_permits_merging(github: GitHub) -> None:
    github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"] = None
    done = github.run()
    assert done.returncode == 1
    assert "main: unknown" in done.stderr
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("tick", [False, True])
def test_an_empty_runs_answer_keeps_a_green_rerun_on_old_main_in_the_line(
    github: GitHub, tick: bool
) -> None:
    _green_rerun_on_old_main(github)
    listed = github.api[_ci_runs(github.head)]
    # Asked for the runs of the head, GitHub answers none and then the run, in turns; the check
    # suite of its jobs lists none either.
    github.api[_ci_runs(github.head)] = {"_alternate": [{"workflow_runs": []}, listed]}
    _check_runs(github, github.head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()
    env = _tick_context(github) if tick else None
    for _ in range(3):
        done = github.workflow.run_script("merge-when-current", "--train-status", env=env)
        assert done.returncode == 0, done.stderr
        train = json.loads(done.stdout)
        assert train["blocked"] == ["7"]
        assert train["waiting"] == ["7", "8"]
        assert train["first"] == "7"
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert "failed checks" not in done.stderr
    assert github.workflow.git("rev-parse", "HEAD^2") == github.base
    # A call that read the head asked again half a minute after the empty answers, once.
    sleeps = github.workflow.calls("sleep")
    assert sleeps and set(sleeps) == {"sleep 30"} and len(sleeps) <= 4


def test_a_run_github_does_not_list_is_found_through_its_check_suite(github: GitHub) -> None:
    _green_rerun_on_old_main(github)
    [run] = github.api[_ci_runs(github.head)]["workflow_runs"]
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    _check_runs(github, github.head, ("CI: what to run", "completed"), ("CI result", "completed"))
    # A newer run of another workflow in the same check suite is not the CI run.
    github.api[_SUITE_RUNS] = {
        "workflow_runs": [
            {**run, "id": 70, "path": ".github/workflows/images.yml"},
            {**run, "path": ".github/workflows/ci.yml"},
        ]
    }
    github.save()
    for _ in range(2):
        train = _train(github)
        assert train["blocked"] == ["7"]
        assert train["waiting"] == ["7", "8"]
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert "failed checks" not in done.stderr
    assert not github.workflow.calls("sleep 30")


def test_a_green_rerun_github_stops_listing_keeps_its_place(github: GitHub) -> None:
    _green_rerun_on_old_main(github)
    assert _train(github)["waiting"] == ["7", "8"]
    # Then GitHub lists the run neither by its head nor by its check suite.
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    _check_runs(github, github.head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()
    for _ in range(2):
        train = _train(github)
        assert train["blocked"] == ["7"]
        assert train["waiting"] == ["7", "8"]
        assert train["first"] == "7"
    waits = github.run()
    assert waits.returncode == 1
    assert "failed checks" not in waits.stderr
    assert "has not started" in waits.stderr


@pytest.mark.parametrize("answer", [None, "HTTP 502"])
def test_a_green_rerun_whose_check_runs_cannot_be_read_keeps_its_place(
    github: GitHub, answer: str | None
) -> None:
    _green_rerun_on_old_main(github)
    assert _train(github)["waiting"] == ["7", "8"]
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    # GitHub fails the read of the head's check runs: unavailable, or with a server error.
    github.api[_check_runs_path(github.head)] = answer
    github.save()
    for _ in range(2):
        train = _train(github)
        assert train["blocked"] == ["7"]
        assert train["waiting"] == ["7", "8"]
        assert train["first"] == "7"


def test_a_moment_with_no_runs_and_no_check_runs_keeps_the_green_rerun_waiting(
    github: GitHub,
) -> None:
    _green_rerun_on_old_main(github)
    _check_runs(github, github.head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    listed = github.api[_ci_runs(github.head)]
    checks = github.api[_check_runs_path(github.head)]
    # Right after a re-run GitHub can lag on the head's check runs as on its runs: for a moment
    # it lists neither.
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    github.api[_check_runs_path(github.head)] = {"total_count": 0, "check_runs": []}
    github.save()
    train = _train(github)
    assert train["blocked"] == ["7"]
    assert train["waiting"] == ["7", "8"]
    assert train["first"] == "7"
    waits = github.run()
    assert waits.returncode == 1
    assert "failed checks" not in waits.stderr
    assert "has not started" in waits.stderr
    # Then it lists both again, and the member refreshes.
    github.api[_ci_runs(github.head)] = listed
    github.api[_check_runs_path(github.head)] = checks
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert "failed checks" not in done.stderr


def test_the_saved_line_after_an_empty_look_still_names_the_rerun_refreshable(
    github: GitHub,
) -> None:
    _green_rerun_on_old_main(github)
    _check_runs(github, github.head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    listed = github.api[_ci_runs(github.head)]
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    saved = json.loads((github.workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert saved["refreshable"] == ["7"]
    github.api[_ci_runs(github.head)] = listed
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert "failed checks" not in done.stderr


def test_a_partial_rerun_still_running_is_pending_and_keeps_no_stale_mark(
    github: GitHub,
) -> None:
    # A failed job re-run in part: its new attempt runs while the other jobs are finished.
    _green_rerun_on_old_main(github)
    _check_runs(
        github,
        github.head,
        ("CI: what to run", "completed"),
        ("CI: server tests", "in_progress"),
        ("CI result", "in_progress"),
    )
    [run] = github.api[_ci_runs(github.head)]["workflow_runs"]
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    github.api[_SUITE_RUNS] = {
        "workflow_runs": [
            {**run, "status": "in_progress", "conclusion": None, "path": ".github/workflows/ci.yml"}
        ]
    }
    github.save()
    train = _train(github)
    assert train["blocked"] == ["7"]
    assert train["waiting"] == ["8"]
    assert not github.workflow.calls("sleep 30")


def test_one_read_of_a_line_github_lists_no_runs_for_pauses_once_not_per_member(
    github: GitHub,
) -> None:
    # One reconciliation holds the merge lock: its pause does not grow with the line.
    _unlisted_line(github, 6)
    done = github.workflow.run_script("merge-when-current", "--train-status")
    assert done.returncode == 0, done.stderr
    assert github.workflow.calls("sleep 30") == ["sleep 30"]


def test_a_gate_call_on_a_line_github_lists_no_runs_for_pauses_once(github: GitHub) -> None:
    # The first member's gate call reconciles the whole line under the merge lock.
    _unlisted_line(github, 4)
    done = github.run()
    assert done.returncode != 0
    assert github.workflow.calls("sleep 30") == ["sleep 30"]


def test_red_main_pauses_once_for_earlier_main_commits_github_lists_no_runs_for(
    github: GitHub,
) -> None:
    workflow = github.workflow
    workflow.git("checkout", "main")
    earlier = []
    for name in ("first", "second", "third"):
        workflow.git("commit", "--allow-empty", "-m", f"fix: {name} change")
        earlier.append(workflow.git("rev-parse", "HEAD"))
    workflow.git("commit", "--allow-empty", "-m", "fix: the red change")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main("failure")
    # Main's history names three earlier green pushes, whose runs GitHub now lists nowhere.
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=100"
    ] = {
        "workflow_runs": [
            {"id": 5 + index, "head_sha": commit} for index, commit in enumerate(earlier)
        ]
    }
    for commit in earlier:
        github.api[_ci_runs(commit, pull=False)] = {"workflow_runs": []}
        _check_runs(github, commit, ("CI result", "completed"))
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()

    done = workflow.run_script("main-status")

    assert done.returncode == 0, done.stderr
    assert "main: red" in done.stdout
    assert workflow.calls("sleep 30") == ["sleep 30"]


@pytest.mark.parametrize(
    "checks",
    [
        (),
        (("CI: what to run", "in_progress"),),
        (("Docker images: what to run", "completed"),),
        (("CIFuzz", "completed"),),
    ],
    ids=["none", "running", "other workflow", "other prefix"],
)
def test_a_head_without_a_finished_ci_check_run_stays_pending(
    github: GitHub, checks: tuple[tuple[str, str], ...]
) -> None:
    _green_rerun_on_old_main(github)
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    if checks:
        _check_runs(github, github.head, *checks)
    github.save()
    train = _train(github)
    assert train["blocked"] == ["7"]
    assert train["waiting"] == ["8"]
    refused = github.run()
    assert refused.returncode == 1
    assert "once a re-run is green" in refused.stderr
    assert not [call for call in github.workflow.calls("gh") if "check_suite_id=" in call]
    assert not github.workflow.calls("sleep 30")


@pytest.mark.parametrize("listed", ["again", "check suite"])
def test_an_empty_runs_answer_for_main_is_not_read_as_ci_not_started(
    github: GitHub, listed: str
) -> None:
    [run] = github.api[_ci_runs(github.base, pull=False)]["workflow_runs"]
    if listed == "again":
        github.api[_ci_runs(github.base, pull=False)] = {
            "_alternate": [{"workflow_runs": []}, {"workflow_runs": [run]}]
        }
        github.api[_SUITE_RUNS] = {"workflow_runs": []}
    else:
        github.api[_ci_runs(github.base, pull=False)] = {"workflow_runs": []}
        github.api[_SUITE_RUNS] = {"workflow_runs": [{**run, "path": ".github/workflows/ci.yml"}]}
    _check_runs(github, github.base, ("CI result", "completed"))
    github.save()

    done = github.workflow.run_script("main-status")

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("main: green"), done.stdout


@pytest.mark.parametrize(
    "hang, interruption",
    [
        ("read", None),
        ("push", None),
        ("merge", None),
        ("read-exited", None),
        ("push", signal.SIGINT),
        ("merge", signal.SIGTERM),
        ("push", signal.SIGHUP),
    ],
)
def test_stalled_commands_release_lock_and_stop_descendants(
    github: GitHub,
    hang: str,
    interruption: int | None,
) -> None:
    workflow = github.workflow
    if hang == "push":
        # Force the refresh path through a real git push and its pre-push hook.
        github.ci(github.base, github.head, tested_base=workflow.git("rev-parse", "main^"))
    github.save()
    # Programs live beside the stand-ins: CI's temporary folder does not allow running them.
    release = workflow.state / "release-descendant"
    os.mkfifo(release)
    stall = workflow.bin / "stall"
    stall.write_text(
        "#!/bin/bash\n"
        "trap '' TERM\n"
        # Open both ends so the child blocks until the test releases it, even without
        # another writer. Publish readiness only once the descendant holds the FIFO.
        f"( exec 3<> '{release}'; echo entered > '{workflow.state / 'entered'}'; "
        f"read -r <&3; echo escaped > '{workflow.state / 'escaped'}' ) &\n"
        + ("exit 0\n" if hang == "read-exited" else "wait\n/bin/sleep 600\n")
    )
    stall.chmod(0o755)
    if hang == "push":
        hooks = workflow.bin / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text(f"#!/bin/bash\nexec '{stall}'\n")
        hook.chmod(0o755)
        workflow.git("config", "core.hooksPath", str(hooks))
    else:
        original = (workflow.bin / "gh").read_text()
        workflow.fake("gh-real", original)
        match = "pr view" if hang.startswith("read") else "pr merge"
        workflow.fake(
            "gh",
            f'#!/bin/bash\nif [[ "$1 $2" == "{match}" ]]; then exec "{stall}"; fi\n'
            f'exec "{workflow.bin / "gh-real"}" "$@"\n',
        )
    # Short deadlines only in this test's own gate; production has no test timing switch.
    command_timeout = 10
    entry = workflow.root / "gate.sh"
    entry.write_text(
        "#!/bin/bash\nexec python3 -B -c 'import sys; "
        f'sys.path.insert(0, "{workflow.script.parent}"); '
        "import merge_gate as g; "
        f"g.COMMAND_TIMEOUT = {command_timeout}; "
        "g.READ_TIMEOUT = "
        f"{1 if hang.startswith('read') and not interruption else command_timeout}; "
        "g.READ_RETRY_DELAYS = (); runner = g.run_command; "
        "g.run_command = lambda *a, **kw: runner(*a, **dict(kw, timeout="
        f"{command_timeout if interruption else 1} if "
        f'(a[:2] == ("git", "push") or a[:3] == ("gh", "pr", "merge")) else {command_timeout})); '
        'sys.exit(g.main())\' "$@"\n'
    )
    process = subprocess.Popen(
        ["bash", str(entry), "7"],
        cwd=workflow.repo,
        env=workflow.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        if interruption:
            # Allow the test's gate its command budget to reach the stall under load.
            deadline = time.monotonic() + command_timeout
            while not (workflow.state / "entered").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (workflow.state / "entered").exists()
            process.send_signal(interruption)
        output, error = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    assert (workflow.state / "entered").exists(), output + error
    if interruption:
        assert process.returncode != 0, output + error
    else:
        assert process.returncode == 1, output + error
        assert "timed out" in error
    with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # No test scheduling delay can release the child before cleanup. A FIFO with
        # no readers rejects this open immediately; a surviving child gets permission
        # to mutate state while we hold the released lock, and fails the test.
        try:
            writer = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
        else:
            with os.fdopen(writer, "w") as pipe:
                pipe.write("go\n")
            pytest.fail("descendant ignoring TERM survived cleanup after lock release")
        assert not (workflow.state / "escaped").exists()
    assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("read", ["api", "pull"])
@pytest.mark.parametrize(
    "status, attempts",
    [(502, 3), (503, 4), (404, 1), ("timeout", 3), ("timeout-exhausted", 4), ("client-timeout", 3)],
)
def test_github_reads_retry_server_errors_only(
    github: GitHub,
    read: str,
    status: int,
    attempts: int,
) -> None:
    workflow = github.workflow
    original = (workflow.bin / "gh").read_text()
    workflow.fake("gh-real", original)
    match = '[[ "$1" == api ]]' if read == "api" else '[[ "$1 $2" == "pr view" ]]'
    counter = workflow.state / "read-attempts"
    if isinstance(status, str):
        entry = workflow.script.parent / "merge-when-current"
        entry.write_text(
            "#!/bin/bash\nexec python3 -B -c 'import sys; "
            f'sys.path.insert(0, "{entry.parent}"); '
            "import merge_gate as g; g.READ_TIMEOUT = 0.3; "
            "g.READ_RETRY_DELAYS = (0.01, 0.02, 0.04); "
            'sys.exit(g.main())\' "$@"\n'
        )
    failure = (
        "exec /bin/sleep 600"
        if status in ("timeout", "timeout-exhausted")
        else "echo 'context deadline exceeded' >&2; exit 1"
        if status == "client-timeout"
        else f"echo 'gh: Server Error (HTTP {status})' >&2; exit 1"
    )
    workflow.fake(
        "gh",
        f"""#!/bin/bash
if {match} && [[ ! -e "{workflow.state / "read-succeeded"}" ]]; then
  count=0
  [[ ! -f "{counter}" ]] || read -r count < "{counter}"
  count=$((count + 1))
  echo "$count" > "{counter}"
  if (( count <= {2 if status in (502, "timeout", "client-timeout") else 9} )); then
    {failure}
  fi
  touch "{workflow.state / "read-succeeded"}"
fi
exec "{workflow.bin / "gh-real"}" "$@"
""",
    )
    done = github.run()
    assert int(counter.read_text()) == attempts
    if status in (502, "timeout", "client-timeout"):
        assert done.returncode == 0, done.stderr
        assert (workflow.state / "merged").read_text() == "7"
    else:
        assert done.returncode == 1
        assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("interruption", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
@pytest.mark.parametrize("starved_cleanup", [False, True], ids=["normal", "starved-cleanup"])
def test_interrupted_outer_gate_stops_inner_gate_command_before_releasing_lock(
    github: GitHub,
    interruption: int,
    starved_cleanup: bool,
) -> None:
    workflow = github.workflow
    release = workflow.state / "release-inner-command"
    os.mkfifo(release)
    workflow.fake("gh-real", (workflow.bin / "gh").read_text())
    workflow.fake(
        "gh",
        f'''#!/bin/bash
if [[ "$1 $2" == "pr view" ]]; then
  trap '' TERM
  {'kill -STOP "$PPID"' if starved_cleanup else ":"}
  ( exec 3<> "{release}"; echo entered > "{workflow.state / "entered"}";
    read -r <&3; echo escaped > "{workflow.state / "escaped"}" ) &
  wait
fi
exec "{workflow.bin / "gh-real"}" "$@"
''',
    )
    github.save()
    outer = workflow.bin / "outer.py"
    outer.write_text(
        f"""import os
import signal
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, {str(workflow.script.parent)!r})
import merge_gate as g
g.install_signal_handlers()
communicate = subprocess.Popen.communicate
def scheduled(self, *args, **kwargs):
    if {starved_cleanup!r} and Path({str(workflow.state / "entered")!r}).exists():
        # A stopped gate cannot clean up until scheduled. Expire any bounded
        # cleanup wait immediately; an unbounded wait lets its handler run.
        if kwargs.get("timeout") is not None:
            raise subprocess.TimeoutExpired(self.args, kwargs["timeout"])
        try:
            os.kill(self.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    return communicate(self, *args, **kwargs)
subprocess.Popen.communicate = scheduled
g.command("bash", {str(workflow.script.parent / "merge-when-current")!r}, "7")
"""
    )
    process = subprocess.Popen(
        ["python3", "-B", str(outer)],
        cwd=workflow.repo,
        env=workflow.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not (workflow.state / "entered").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (workflow.state / "entered").exists()
        process.send_signal(interruption)
        output, error = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    assert (workflow.state / "entered").exists(), output + error
    assert process.returncode != 0, output + error
    with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Readiness is published only after the descendant holds the FIFO. A live
        # reader after outer-gate exit proves cleanup released the lock too early.
        try:
            writer = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
        else:
            with os.fdopen(writer, "w") as pipe:
                pipe.write("go\n")
            pytest.fail("inner command survived cleanup after lock release")
        assert not (workflow.state / "escaped").exists()
    assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize(
    "interruption", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT]
)
def test_interrupt_during_spawn_stops_command_before_releasing_lock(
    workflow: Workflow, interruption: int
) -> None:
    # Reproduce the spawn window deterministically: Popen has created a live child,
    # but has not returned the handle to _run when the signal arrives.
    entered = workflow.state / "entered"
    release = workflow.state / "release-spawned-command"
    os.mkfifo(release)
    workflow.fake(
        "stall",
        "#!/bin/bash\ntrap '' TERM\n"
        f'exec 3<> "{release}"\n'
        f'echo entered > "{entered}"\nread -r <&3\n'
        f'echo escaped > "{workflow.state / "escaped"}"\n',
    )
    runner = workflow.bin / "interrupt-spawn.py"
    runner.write_text(
        f"""import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, {str(workflow.script.parent)!r})
import workflow_commands as w
w.install_signal_handlers()
original = subprocess.Popen.__init__
children = []
def interrupted_spawn(self, *args, **kwargs):
    original(self, *args, **kwargs)
    children.append(self)
    deadline = time.monotonic() + 5
    while not Path({str(entered)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert Path({str(entered)!r}).exists()
    os.kill(os.getpid(), {int(interruption)})
subprocess.Popen.__init__ = interrupted_spawn
try:
    with open({str(workflow.state / "lock")!r}, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            w.run("stall")
        except (SystemExit, KeyboardInterrupt):
            pass
        else:
            raise AssertionError("signal was swallowed")
    assert children[0].poll() is not None, "spawned command survived lock release"
    assert not Path({str(workflow.state / "escaped")!r}).exists()
finally:
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=5)
"""
    )
    done = subprocess.run(
        ["python3", "-B", str(runner)],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_nested_gate_is_not_cut_off_by_the_outer_command_deadline(github: GitHub) -> None:
    # The tick's `--drive` gate first waits for the lock another gate holds through its
    # push, so its whole run may outlast the deadline of any one command.
    workflow = github.workflow
    workflow.fake("gh-real", (workflow.bin / "gh").read_text())
    workflow.fake(
        "gh",
        f'''#!/bin/bash
if [[ "$1 $2" == "pr view" ]]; then /bin/sleep 1.5; fi
exec "{workflow.bin / "gh-real"}" "$@"
''',
    )
    github.save()
    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
            "g.COMMAND_TIMEOUT = 1; "
            'done = g.command("bash", sys.argv[1] + "/merge-when-current", "7", check=False); '
            "print(done.stdout + done.stderr); sys.exit(done.returncode)",
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"


@pytest.mark.parametrize("status, attempts", [(502, 3), (403, 1)])
def test_git_reads_of_github_retry_server_errors_only(
    github: GitHub, status: int, attempts: int
) -> None:
    workflow = github.workflow
    real = subprocess.check_output(["bash", "-c", "command -v git"], text=True).strip()
    counter = workflow.state / "git-read-attempts"
    url = "https://github.com/example-org/example-app.git/"
    workflow.fake(
        "git",
        f"""#!/bin/bash
if [[ "$1" == fetch || "$1" == ls-remote ]]; then
  count=0
  [[ ! -f "{counter}" ]] || read -r count < "{counter}"
  count=$((count + 1))
  echo "$count" > "{counter}"
  echo "$*" >> "{workflow.state / "git-reads"}"
  if (( count <= 2 )); then
    echo "fatal: unable to access '{url}': The requested URL returned error: {status}" >&2
    exit 128
  fi
fi
exec "{real}" "$@"
""",
    )
    done = github.run()
    assert int(counter.read_text()) >= attempts
    if status == 502:
        assert done.returncode == 0, done.stderr
        assert (workflow.state / "merged").read_text() == "7"
    else:
        # Each read is tried once: the refused fetch and the later batch-branch listing.
        reads = (workflow.state / "git-reads").read_text().splitlines()
        assert len(reads) == len(set(reads)), reads
        assert done.returncode == 1
        assert "returned error: 403" in done.stderr
        assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("message", MEMORY_SHORTAGES)
def test_a_batch_push_short_of_memory_is_retried_a_minute_later(
    github: GitHub, message: str
) -> None:
    _batch_setup(github)
    short_of_memory(github.workflow, message, times=1)

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert all(member["scratch"]["pushed"] for member in _prefixes(github))
    assert sorted(_scratch_branches(github)) == sorted(
        member["scratch"]["branch"] for member in _prefixes(github)
    )
    # The first prefix's push twice, a minute apart, then the two behind it.
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 4
    assert len(github.workflow.calls("sleep 60")) == 1


def test_a_batch_push_that_fails_twice_keeps_the_batch_for_the_next_call(github: GitHub) -> None:
    _batch_setup(github)
    short_of_memory(github.workflow, MEMORY_SHORTAGES[1], times=2)

    done = _batch_call(github)

    # A wait, not a refusal: nothing about the members changed.
    assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    assert "could not lock memory" in _refusal(done)
    train = _batch_state(github)
    assert _numbers(github) == ["7", "8", "9"]
    assert not any(member["scratch"]["pushed"] for member in _prefixes(github))
    assert sorted(train["approved"]) == ["7", "8", "9"]
    assert _scratch_branches(github) == []
    assert github.workflow.calls("gh workflow run") == []
    branches = [member["scratch"]["branch"] for member in _prefixes(github)]

    assert _batch_call(github).returncode == 3
    assert [member["scratch"]["branch"] for member in _prefixes(github)] == branches
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert [call.split()[3] for call in github.workflow.calls("gh pr merge")] == ["7", "8", "9"]


def test_a_batch_push_short_of_memory_on_standard_output_is_never_counted(
    github: GitHub,
) -> None:
    _batch_setup(github)
    # The scan prints Semgrep's report, io_uring error and all, on standard output; git's own
    # "failed to push some refs" is all that reaches standard error.
    short_of_memory(github.workflow, MEMORY_SHORTAGES[0], times=6, stream=1)

    for _ in range(3):
        done = _batch_call(github)
        assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    batch = _scratch(github)
    assert batch is not None
    assert batch["pushed"] is False and "push_failures" not in batch
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 6
    assert len(github.workflow.calls("sleep 60")) == 3
    assert _batch_call(github).returncode == 3
    assert all(member["scratch"]["pushed"] for member in _prefixes(github))


def test_every_push_of_the_gate_runs_its_hook_with_the_posix_backend(github: GitHub) -> None:
    _stale(github)
    short_of_memory(github.workflow, MEMORY_SHORTAGES[0], times=0)

    # Even a caller whose own environment names another backend.
    github.save()
    done = github.workflow.run_script("merge-when-current", "7", env={"EIO_BACKEND": "io_uring"})

    assert done.returncode == 2, done.stdout + done.stderr
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"]
    assert github.workflow.calls("sleep 60") == []


@pytest.mark.parametrize("message", MEMORY_SHORTAGES)
def test_a_refresh_push_short_of_memory_is_retried_a_minute_later(
    github: GitHub, message: str
) -> None:
    _stale(github)
    short_of_memory(github.workflow, message, times=1)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    workflow = github.workflow
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[
        0
    ] == workflow.git("rev-parse", "HEAD")
    # The minute is waited between the two pushes, not after them.
    assert hook_runs_and_waits(workflow) == [
        "pre-push EIO_BACKEND=posix",
        "sleep 60",
        "pre-push EIO_BACKEND=posix",
    ]


@pytest.mark.parametrize("stream", [1, 2])
@pytest.mark.parametrize("message", MEMORY_SHORTAGES)
def test_a_refresh_push_that_fails_twice_is_refused_naming_the_error(
    github: GitHub, message: str, stream: int
) -> None:
    _stale(github)
    short_of_memory(github.workflow, message, times=2, stream=stream)

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    # The scan's own words on the line the wake shows, not "$ sh scripts/security-scan.sh all".
    refusal = _refusal(done)
    assert refusal.startswith("merge refused: could not push the refreshed branch: "), refusal
    assert message in refusal and "a minute apart" in refusal, refusal
    assert len(github.workflow.calls("sleep 60")) == 1
    assert (
        github.workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]
        == github.head
    )
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 2


# Any other program out of memory (lint, the web tests) is not the scan's locked memory: a
# retry would not help, and a batch would wait on it for ever.
@pytest.mark.parametrize(
    "message", ["secret found in config.py", "bash: fork: Cannot allocate memory"]
)
def test_a_push_failing_for_another_reason_is_not_retried(github: GitHub, message: str) -> None:
    _stale(github)
    short_of_memory(github.workflow, message, times=1)

    done = github.run()

    assert done.returncode == 1 and message in done.stderr, done.stdout + done.stderr
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"]
    assert github.workflow.calls("sleep 60") == []


def test_a_batch_push_the_hook_keeps_refusing_is_refused_on_the_third_call(
    github: GitHub,
) -> None:
    _batch_setup(github)
    # Not a memory shortage: no retry within a call, and it never clears.
    short_of_memory(github.workflow, "secret found in config.py", times=99)

    for _ in range(2):
        done = _batch_call(github)
        assert done.returncode == 1 and "merge waiting" in done.stderr, done.stderr
        assert _numbers(github) == ["7", "8", "9"]
    done = _batch_call(github)

    assert done.returncode == 1 and "merge refused" in done.stderr, done.stderr
    assert "secret found" in done.stderr and "3 calls in a row" in done.stderr
    # The first prefix could not be pushed, and every prefix behind it holds it.
    assert _batch_state(github)["prefixes"] is None
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 3
    assert _scratch_branches(github) == []
    assert github.workflow.calls("gh workflow run") == []


def test_a_batch_push_that_stalls_past_its_deadline_waits_for_the_next_call(
    github: GitHub,
) -> None:
    _batch_setup(github)
    workflow = github.workflow
    hooks = workflow.bin / "hooks"
    stalled = workflow.state / "stalled"
    hooks.mkdir(exist_ok=True)
    # Its scan ran short of memory first: a stall counts all the same, and is never retried.
    (hooks / "pre-push").write_text(
        f'#!/bin/bash\n[ -e "{stalled}" ] && exit 0\ntouch "{stalled}"\n'
        f"echo {json.dumps(MEMORY_SHORTAGES[0])}\nexec /bin/sleep 30\n"
    )
    (hooks / "pre-push").chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))
    github.save()

    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
            "g.COMMAND_TIMEOUT = 5; sys.argv = ['merge-when-current', '7']; "
            "sys.exit(g.main())",
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    batch = _scratch(github)
    assert batch is not None
    assert batch["pushed"] is False and batch["push_failures"] == 1
    assert workflow.calls("sleep 60") == []
    assert _batch_call(github).returncode == 3
    assert all(member["scratch"]["pushed"] for member in _prefixes(github))


def test_a_push_that_times_out_is_handed_back_with_the_hooks_step_and_last_lines(
    github: GitHub,
) -> None:
    """The tick's gate refreshes the branch, and its hook stalls in a step past the deadline."""
    workflow = github.workflow
    # A passed review handed off from the pull request's worktree, then main moved on.
    workflow.git("checkout", "main")
    workflow.git("worktree", "add", str(workflow.worktree(7)), "ticket/seven")
    workflow.list_pull_request(7, draft=False)
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {github.head}\n")
    github.pulls["7"]["labels"] = [{"name": "workflow"}]
    github.api[f"{_REPO}/pulls/7/reviews"] = [
        {
            "body": "<!-- workflow-review round=1 result=passed -->\nReviewed.",
            "commit_id": github.head,
        }
    ]
    github.save()
    handed = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))
    assert handed.returncode == 0, handed.stdout + handed.stderr
    workflow.run("stop", "review-7")
    github.batch_mode = github.driver_mode = True
    old = github.base
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main()
    github.ci(github.base, github.head, tested_base=old)
    hooks = workflow.bin / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-push").write_text(
        "#!/bin/bash\n"
        "echo '$ sh scripts/slow-check.sh'\n"
        "echo 'checking 41 of 90' >&2\n"
        "exec /bin/sleep 30\n"
    )
    (hooks / "pre-push").chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))
    # The driver runs the gate it finds beside it: this one has a short deadline of its own.
    beside = workflow.root / "beside"
    beside.mkdir()
    (beside / "merge-when-current").write_text(
        "#!/bin/bash\nexec python3 -B -c 'import sys; "
        f'sys.path.insert(0, "{workflow.script.parent}"); '
        "import merge_gate as g; g.COMMAND_TIMEOUT = 5; "
        'sys.argv = ["merge-when-current", *sys.argv[1:]]; sys.exit(g.main())\' "$@"\n'
    )
    github.save()

    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "import merge_driver as d; d.gate.HERE = Path(sys.argv[2]); "
            "d._drive_gate(Path('.local/workflow').resolve(), '7')",
            str(workflow.script.parent),
            str(beside),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    refusal = _refusal(subprocess.CompletedProcess([], 0, "", done.stdout))
    assert refusal == (
        "merge refused: could not push the refreshed branch: git push timed out after 5s"
    ), done.stdout
    reason = json.loads((workflow.repo / ".local/workflow/merge-review-7").read_text())["reason"]
    assert reason.startswith(refusal), reason
    assert "$ sh scripts/slow-check.sh\nchecking 41 of 90" in reason, reason
    assert (
        workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == github.head
    )


def test_a_refresh_push_refused_for_an_advisory_is_still_refused(github: GitHub) -> None:
    _stale(github)
    short_of_memory(github.workflow, ADVISORY, times=1)

    done = github.run()

    assert done.returncode == 1 and "merge refused" in done.stderr, done.stdout + done.stderr
    assert "GHSA-1" in done.stderr
    assert not (github.workflow.repo / ".local/workflow/advisory-service-down").exists()
