"""A stopped workflow command takes every process it started with it (real processes, no server)."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from contextlib import suppress
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TYPE_CHECKING

import pytest
from workflow_harness import SCRIPTS, Workflow

if TYPE_CHECKING:
    from workflow.merge_gate_fixtures import GitHub


# The merge fixtures import the process helpers below through test_ci_proof. Load them only
# during fixture setup, after both modules have finished importing.
@pytest.fixture
def gate() -> Iterator[Any]:
    from workflow.merge_gate_fixtures import gate as fixture

    yield from fixture.__wrapped__()


@pytest.fixture
def github(workflow: Workflow) -> Iterator[GitHub]:
    from workflow.merge_gate_fixtures import github as fixture

    yield from fixture.__wrapped__(workflow)


@pytest.fixture
def handed(github: GitHub) -> GitHub:
    from workflow.test_merge_driver import handed as fixture

    return fixture.__wrapped__(github)


_MERGE_502 = (
    '* Request at now\n> POST /graphql HTTP/1.1\n'
    '< HTTP/2.0 502 Bad Gateway\n* Request took 1ms\n'
    'non-200 OK status code: 502 Bad Gateway body: "<html>502 Bad Gateway</html>"'
)
_SERVER_ERRORS = [
    _MERGE_502,
    "non-200 OK status code: 503 Service Unavailable",
    "remote: Internal Server Error",
    "! [remote rejected]     HEAD -> ticket/seven (Internal Server Error)",
    "! [remote rejected]     HEAD -> ticket/seven (Service Unavailable)",
    "! [remote rejected]     HEAD -> ticket/seven (Bad Gateway)",
    "remote: Internal Server Error\n"
    "! [remote rejected]     HEAD -> ticket/seven (Internal Server Error)",
]


@pytest.mark.parametrize("error", _SERVER_ERRORS)
def test_github_incident_texts_are_transient(gate: Any, error: str) -> None:
    assert gate.transient(error)


@pytest.mark.parametrize("error", _SERVER_ERRORS)
@pytest.mark.parametrize("client_error", [
    "gh: forbidden (HTTP 403)",
    "fatal: The requested URL returned error: 404",
    "non-200 OK status code: 422 Unprocessable Entity",
    "< HTTP/2.0 401 Unauthorized",
])
def test_a_client_error_wins_over_github_incident_texts(
    gate: Any, error: str, client_error: str
) -> None:
    assert not gate.transient(error + "\n" + client_error)
    assert not gate.transient(client_error + "\n" + error)
    assert not gate._transient_refresh_push(error + "\n" + client_error)


def _merge_error(github: GitHub, *, completed: bool = False, changed_head: bool = False) -> None:
    workflow = github.workflow
    workflow.fake("gh-before-error", (workflow.bin / "gh").read_text())
    patch = (
        "import json, sys; from pathlib import Path; p = Path(sys.argv[1]); "
        "data = json.loads(p.read_text()); data['pulls']['7']['headRefOid'] = 'f' * 40; "
        "p.write_text(json.dumps(data))"
    )
    workflow.fake(
        "gh",
        "#!/bin/bash\n"
        'if [[ "$1 $2" == "pr merge" ]]; then\n'
        + (f'{shlex.quote(str(workflow.bin / "gh-before-error"))} "$@" || exit\n'
           if completed else "")
        + (f"{shlex.quote(sys.executable)} -c {shlex.quote(patch)} "
           f"{shlex.quote(str(workflow.state / 'github.json'))}\n" if changed_head else "")
        + f"printf '%s\\n' {shlex.quote(_MERGE_502)} >&2\nexit 1\nfi\n"
        + f'exec {shlex.quote(str(workflow.bin / "gh-before-error"))} "$@"\n',
    )


@pytest.mark.parametrize("completed,changed_head", [(False, False), (True, False), (True, True)])
def test_a_failed_merge_is_read_back_before_its_result_is_decided(
    handed: GitHub, completed: bool, changed_head: bool
) -> None:
    from workflow.test_merge_driver import _note, _train

    workflow = handed.workflow
    _merge_error(handed, completed=completed, changed_head=changed_head)
    done = workflow.run_script(
        "merge-when-current", "--drive", "7", cwd=workflow.worktree(7)
    )
    merged = completed and not changed_head
    assert done.returncode == (0 if merged else 3), done.stdout + done.stderr
    assert ("7" not in _train(workflow)["waiting"]) == merged
    assert _note(workflow).exists()
    if merged:
        receipt = json.loads((_note(workflow).parent / "main-tree-green.json").read_text())
        assert receipt["main"] == workflow.git("rev-parse", "origin/main")
    if not merged:
        assert "merge waiting:" in done.stderr
        assert _train(workflow)["approved"]["7"] == handed.head
    calls = workflow.calls("gh")
    merge = next(i for i, call in enumerate(calls) if call.startswith("gh pr merge")) if completed else -1
    if completed:
        assert calls[merge + 1].startswith("gh pr view 7 ")


@pytest.mark.parametrize("error", _SERVER_ERRORS[2:])
def test_a_github_push_rejection_keeps_the_refresh_and_handoff(
    handed: GitHub, error: str
) -> None:
    from workflow.test_merge_driver import _folder, _note, _stale_refresh, _started, _train

    workflow = handed.workflow
    _stale_refresh(handed)
    # Each rejection is one push's output, including the captured two-line form.
    workflow.fake(
        "git",
        '#!/bin/bash\nif [[ $1 == push ]]; then\n'
        f"printf '%s\\n' {shlex.quote(error)} >&2\nexit 1\nfi\n"
        'exec /usr/bin/git "$@"\n',
    )
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "merge hand-back 7" not in first.stdout
    assert _note(workflow).exists()
    assert _train(workflow)["approved"]["7"] == handed.head
    assert _started(workflow) == []
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != handed.head
    assert json.loads((_folder(workflow) / "merge-refresh-7.json").read_text())["commit"] == refreshed

    workflow.fake("git", '#!/bin/bash\nexec /usr/bin/git "$@"\n')
    second = workflow.run_script("tick")
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert _started(workflow) == []


def test_github_server_errors_reach_the_wake_once_across_merge_and_push(handed: GitHub) -> None:
    from workflow.test_merge_driver import _drop_refresh_pushes, _folder, _stale_refresh

    workflow = handed.workflow
    workflow.orchestrator("idle")
    _merge_error(handed)
    for _ in range(2):
        done = workflow.run_script(
            "merge-when-current", "--drive", "7", cwd=workflow.worktree(7)
        )
        assert done.returncode == 3, done.stdout + done.stderr
    wake = workflow.run_script("wake")
    assert wake.returncode == 0, wake.stdout + wake.stderr
    line = "GitHub server errors: merges and pushes wait"
    assert sum(message.count(line) for message in workflow.sent()) == 1

    _stale_refresh(handed)
    _drop_refresh_pushes(workflow, [_SERVER_ERRORS[3]])
    assert workflow.run_script("tick").returncode == 0
    assert sum(message.count(line) for message in workflow.sent()) == 1
    assert not list((_folder(workflow) / "stuck.pending").glob("*.json"))


# The command `test-run` runs: a test in `timeout`'s process group, and a child that starts a
# session of its own, as Playwright's browsers do. Both hold the output pipes of the gate.
_TESTS = (
    "import subprocess, sys, time\n"
    "detached = subprocess.Popen(['sleep', '600'], start_new_session=True)\n"
    "open(sys.argv[2], 'w').write(str(detached.pid))\n"
    "open(sys.argv[1], 'w').write(str(subprocess.os.getpid()))\n"
    "time.sleep(600)\n"
)


def _ended(pid: int) -> bool:
    """Whether the process has ended. It is reaped by whoever adopted it, a moment later."""
    status = Path(f"/proc/{pid}/status")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if "\nState:\tZ" in status.read_text():
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


def _pid(path: Path) -> int:
    deadline = time.monotonic() + 20
    while not path.exists() or not path.read_text().strip():
        assert time.monotonic() < deadline, f"{path.name} never started"
        time.sleep(0.05)
    return int(path.read_text())


@pytest.mark.parametrize("stop", ["signal", "time limit"])
def test_a_stopped_push_takes_the_tests_its_hook_started_with_it(
    tmp_path: Path, programs_folder: Path, stop: str
) -> None:
    """A gate stopped during its push ends, with the tests of the hook and the push slot gone."""
    limit = "8" if stop == "time limit" else "None"
    test, detached, orphan = tmp_path / "test", tmp_path / "detached", tmp_path / "orphan"
    tests = tmp_path / "tests.py"
    tests.write_text(_TESTS)
    # git runs the hook, which runs the tests through `test-run`, and waits for it. A child of
    # the hook in a session of its own loses its parent to the first kill, and with it its place
    # in the run's tree.
    git = programs_folder / "git"
    git.write_text(
        f'#!/bin/bash\nsetsid sleep 600 & echo $! > "{orphan}"\n'
        f'bash "{SCRIPTS / "test-run"}" "{sys.executable}" "{tests}" "{test}" "{detached}"\n'
    )
    git.chmod(0o755)
    gate = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import workflow_commands as w; w.install_signal_handlers(); "
            f"w.run('git', 'push', timeout={limit})",
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(SCRIPTS),
            "PATH": f"{programs_folder}:{os.environ['PATH']}",
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "WORKFLOW_SESSION": "ticket-7",
            # No control group file system: the run has only its process groups, as when
            # `test-run` cannot make a group of its own.
            "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pids = []
    try:
        pids = [_pid(test), _pid(detached), _pid(orphan)]

        if stop == "signal":
            gate.send_signal(signal.SIGTERM)

        error = gate.communicate(timeout=20)[1]
        if stop == "signal":
            assert gate.returncode == 128 + signal.SIGTERM
        else:
            assert "CommandTimeout: git push timed out after 8s" in error
        assert [pid for pid in pids if not _ended(pid)] == []
        slots = sorted((tmp_path / "state/workflow/pushes").glob("slot-*.lock"))
        assert slots
        for path in slots:
            with path.open("a+") as slot:
                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for pid in pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            os.killpg(gate.pid, signal.SIGKILL)
        gate.communicate()


def test_a_number_known_from_the_run_but_now_used_by_another_process_is_left_alone() -> None:
    """A process of the run that ended may leave its number to an unrelated one."""
    other = subprocess.Popen(["sleep", "600"])
    try:
        # No process has the leader's number as its session: only what is known is of the run.
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import workflow_commands as w\n"
                "pid = int(sys.argv[2]); start = w._processes()[pid][3]\n"
                "print(sorted(w._run_processes(2**30, {(pid, start + 1)})) == [])\n"
                "print(sorted(w._run_processes(2**30, {(pid, start)})) == [(pid, start)])\n",
                str(SCRIPTS),
                str(other.pid),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        other.kill()
        other.wait()

    assert done.stdout.split() == ["True", "True"], done.stdout + done.stderr


def test_a_timed_out_command_carries_the_output_read_before_its_stop() -> None:
    """What a command printed before its deadline is kept; its message stays the same."""
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import workflow_commands as w\n"
            "try:\n"
            "    w.run('sh', '-c', 'echo first; echo second >&2; exec sleep 600', timeout=3)\n"
            "except w.CommandTimeout as error:\n"
            "    print(error)\n"
            "    print(error.output, end='')\n",
            str(SCRIPTS),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.stdout.splitlines() == [
        "sh -c echo first; echo second >&2; exec sleep 600 timed out after 3s",
        "first",
        "second",
    ], done.stdout + done.stderr
