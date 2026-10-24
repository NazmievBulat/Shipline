"""Bounded, real-process tests of the push queue (no browser or server)."""

import ast
import fcntl
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/workflow/push_slots.py"


def _wait(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), str(path)


def test_waiter_runs_after_a_killed_run_releases_its_slot(tmp_path: Path) -> None:
    slots = 2
    processes: list[subprocess.Popen[str]] = []
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    try:
        for number in range(slots + 1):
            folder = tmp_path / str(number)
            folder.mkdir()
            started = folder / "started"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(_SCRIPT),
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import time; "
                    "Path('started').touch(); time.sleep(30)",
                ],
                cwd=folder,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            processes.append(process)
            if number < slots:
                _wait(started)
        queue = tmp_path / "state/workflow/pushes"
        deadline = time.monotonic() + 5
        while not list(queue.glob("wait-*")) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert list(queue.glob("wait-*")), "third run never joined the queue"
        # A ticket is created before admission is checked. Wait for the flushed
        # announcement so freeing a slot cannot race that first check.
        assert processes[-1].stderr is not None
        assert select.select([processes[-1].stderr], [], [], 5)[0]
        assert processes[-1].stderr.readline().strip() == ("waiting for a push slot: 0 ahead")
        assert not (tmp_path / str(slots) / "started").exists()
        # Kill the complete run, as when a session is stopped or crashes.
        os.killpg(processes[0].pid, signal.SIGKILL)
        processes[0].wait(timeout=5)
        _wait(tmp_path / str(slots) / "started")
        os.killpg(processes[-1].pid, signal.SIGTERM)
        processes[-1].wait(timeout=5)
        # Keep reading through the same wrapper: readline may have buffered more.
        error = processes[-1].stderr.read()
        assert "waiting for a push slot" not in error
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


def test_fifo_skips_dead_waiters_and_retains_a_surviving_child_slot(tmp_path: Path) -> None:
    processes: list[subprocess.Popen[str]] = []
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}
    queue = tmp_path / "state/workflow/pushes"
    queue.mkdir(parents=True)
    reserved = (queue / "slot-1.lock").open("a+")
    fcntl.flock(reserved, fcntl.LOCK_EX)
    try:
        for number in range(4):
            folder = tmp_path / str(number)
            folder.mkdir()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(_SCRIPT),
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import os,time,sys; "
                    "import subprocess; "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
                    "close_fds=False); "
                    "Path('started').write_text(str(os.getpid())); time.sleep(30)",
                ],
                cwd=folder,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            processes.append(process)
            if number == 0:
                _wait(folder / "started")
            else:
                deadline = time.monotonic() + 5
                while not list(queue.glob(f"wait-*-{process.pid}")) and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert list(queue.glob(f"wait-*-{process.pid}"))
                assert process.stderr is not None
                assert select.select([process.stderr], [], [], 5)[0]
                assert process.stderr.readline().strip() == (
                    f"waiting for a push slot: {number - 1} ahead"
                )
        # A dead waiter must not block the remaining FIFO queue.
        processes[1].kill()
        processes[1].wait(timeout=5)
        # The surviving hook command must retain its slot after the supervisor dies.
        processes[0].kill()
        processes[0].wait(timeout=5)
        time.sleep(0.2)
        assert not (tmp_path / "2/started").exists()
        os.killpg(processes[0].pid, signal.SIGKILL)
        _wait(tmp_path / "2/started")
        assert not (tmp_path / "3/started").exists()
        os.killpg(processes[2].pid, signal.SIGKILL)
        _wait(tmp_path / "3/started")
    finally:
        reserved.close()
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


def test_hook_keeps_all_commands_and_stops_on_failure(
    tmp_path: Path, programs_folder: Path
) -> None:
    root = _SCRIPT.parents[2]
    bin_dir = programs_folder
    npm = bin_dir / "npm"
    npm.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n[ "$*" != "$FAIL" ]\n')
    npm.chmod(0o755)
    calls = tmp_path / "calls"
    for failing, expected in [
        ("", ["run security:scan", "run lint", "run typecheck", "run test:web"]),
        ("run security:scan", ["run security:scan"]),
        ("run lint", ["run security:scan", "run lint"]),
        ("run typecheck", ["run security:scan", "run lint", "run typecheck"]),
        ("run test:web", ["run security:scan", "run lint", "run typecheck", "run test:web"]),
    ]:
        calls.write_text("")
        done = subprocess.run(
            ["sh", "-e", ".husky/pre-push"],
            cwd=root,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "XDG_STATE_HOME": str(tmp_path),
                "CALLS": str(calls),
                "FAIL": failing,
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert calls.read_text().splitlines() == expected
        assert (done.returncode == 0) == (not failing), done.stderr


def test_hook_runs_every_check_with_the_posix_backend(
    tmp_path: Path, programs_folder: Path
) -> None:
    """Semgrep locks no memory on the posix backend, so a push from any worktree on this machine
    never fails for the sessions' shared budget of locked memory."""
    root = _SCRIPT.parents[2]
    npm = programs_folder / "npm"
    npm.write_text('#!/bin/sh\necho "$* EIO_BACKEND=${EIO_BACKEND:-}" >> "$CALLS"\n')
    npm.chmod(0o755)
    calls = tmp_path / "calls"
    for given in [None, "io_uring"]:
        calls.write_text("")
        env = {key: value for key, value in os.environ.items() if key != "EIO_BACKEND"}
        done = subprocess.run(
            ["sh", "-e", ".husky/pre-push"],
            cwd=root,
            env={
                **env,
                **({"EIO_BACKEND": given} if given else {}),
                "PATH": f"{programs_folder}:{os.environ['PATH']}",
                "XDG_STATE_HOME": str(tmp_path),
                "CALLS": str(calls),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert done.returncode == 0, done.stderr
        assert calls.read_text().splitlines() == [
            f"run {check} EIO_BACKEND=posix"
            for check in ("security:scan", "lint", "typecheck", "test:web")
        ]


def test_hook_waits_for_a_slot_before_any_check(tmp_path: Path, programs_folder: Path) -> None:
    root = _SCRIPT.parents[2]
    queue = tmp_path / "workflow/pushes"
    queue.mkdir(parents=True)
    locks = [(queue / f"slot-{number}.lock").open("a+") for number in range(2)]
    for lock in locks:
        fcntl.flock(lock, fcntl.LOCK_EX)
    bin_dir = programs_folder
    npm = bin_dir / "npm"
    npm.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n')
    npm.chmod(0o755)
    calls = tmp_path / "calls"
    process = subprocess.Popen(
        ["sh", "-e", ".husky/pre-push"],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "XDG_STATE_HOME": str(tmp_path),
            "CALLS": str(calls),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert process.stderr is not None
        assert select.select([process.stderr], [], [], 5)[0]
        assert process.stderr.readline().strip() == "waiting for a push slot: 0 ahead"
        assert not calls.exists()
        locks[0].close()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr
        assert calls.read_text().splitlines() == [
            "run security:scan",
            "run lint",
            "run typecheck",
            "run test:web",
        ]
    finally:
        for lock in locks:
            lock.close()
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)


def test_gate_push_wait_is_outside_timeout_and_hook_reuses_slot(
    tmp_path: Path, programs_folder: Path
) -> None:
    """An occupied queue lasts longer than the deadline; the admitted hook still finishes."""
    queue = tmp_path / "workflow/pushes"
    queue.mkdir(parents=True)
    locks = [(queue / f"slot-{number}.lock").open("a+") for number in range(2)]
    for lock in locks:
        fcntl.flock(lock, fcntl.LOCK_EX)
    bin_dir = programs_folder
    git = bin_dir / "git"
    git.write_text(
        '#!/bin/sh\nexec python3 "$SCRIPTS/push_slots.py" '
        "python3 -c \"from pathlib import Path; Path('$STARTED').touch()\"\n"
    )
    git.chmod(0o755)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import workflow_commands as w; import sys; "
            "sys.exit(w.run('git', 'push', timeout=5).returncode)",
        ],
        env={
            **os.environ,
            "XDG_STATE_HOME": str(tmp_path),
            "PYTHONPATH": str(_SCRIPT.parent),
            "SCRIPTS": str(_SCRIPT.parent),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STARTED": str(tmp_path / "started"),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert process.stderr is not None
        assert select.select([process.stderr], [], [], 5)[0]
        assert process.stderr.readline().strip() == "waiting for a push slot: 0 ahead"
        time.sleep(5.2)
        assert process.poll() is None
        locks[0].close()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr
        assert (tmp_path / "started").exists()
        assert "waiting for a push slot" not in stderr
    finally:
        for lock in locks:
            lock.close()
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)


def test_real_git_passes_admission_through_husky_to_hook(
    tmp_path: Path, programs_folder: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    remote = tmp_path / "remote.git"

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("commit", "--allow-empty", "-m", "test")
    git("init", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    hooks = programs_folder / "hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", str(hooks))
    hook = hooks / "pre-push"
    hook.write_text('#!/bin/sh\nexec sh -e "$SCRIPTS/../../.husky/pre-push"\n')
    hook.chmod(0o755)
    # The real hook runs relative to the repository; expose its scripts in this throwaway repo.
    (repository / "scripts").symlink_to(_SCRIPT.parents[1])
    bin_dir = programs_folder
    npm = bin_dir / "npm"
    npm.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n')
    npm.chmod(0o755)
    queue = tmp_path / "workflow/pushes"
    queue.mkdir(parents=True)
    with (queue / "slot-1.lock").open("a+") as reserved:
        fcntl.flock(reserved, fcntl.LOCK_EX)
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import workflow_commands as w; import sys; "
                "sys.exit(w.run('git', 'push', 'origin', 'HEAD', timeout=5).returncode)",
            ],
            cwd=repository,
            env={
                **os.environ,
                "XDG_STATE_HOME": str(tmp_path),
                "PYTHONPATH": str(_SCRIPT.parent),
                "SCRIPTS": str(_SCRIPT.parent),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "CALLS": str(tmp_path / "calls"),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "calls").read_text().splitlines() == [
        "run security:scan",
        "run lint",
        "run typecheck",
        "run test:web",
    ]
    assert "waiting for a push slot" not in done.stderr


def test_hook_wrapper_parses_on_system_python() -> None:
    # A project's own Python may differ from the host's python3 used by Git hooks.
    ast.parse(_SCRIPT.read_text(), feature_version=(3, 10))
