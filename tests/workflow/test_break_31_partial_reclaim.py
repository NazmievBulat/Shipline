"""Breaking tests for pull request 31: a partial memory reclaim is not a failed squeeze.

The kernel's refusals are made with an `LD_PRELOAD` shim around libc's `write`, so they reach
`group_write.py` and any `dd` alike as a real errno, independent of the harness's Python hook.
"""

import errno
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from workflow_harness import Workflow

_MB = 1024 * 1024
_GB = 1024 * _MB

# Fails `write` with an errno for the files named in BREAK_REFUSE: entries "path|errno|flags"
# split by ";". Flag "once" fails only the first write to that file in this process; flag
# "slow" waits two seconds first, as a long reclaim does.
_SHIM = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static ssize_t (*real_write)(int, const void *, size_t);
static int used[64];

ssize_t write(int fd, const void *buf, size_t count) {
    if (!real_write) real_write = dlsym(RTLD_NEXT, "write");
    const char *spec = getenv("BREAK_REFUSE");
    char link[64], target[4096], copy[8192];
    if (spec && fd > 2) {
        snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
        ssize_t n = readlink(link, target, sizeof target - 1);
        if (n > 0) {
            target[n] = 0;
            strncpy(copy, spec, sizeof copy - 1);
            copy[sizeof copy - 1] = 0;
            int index = 0;
            char *save, *entry;
            for (entry = strtok_r(copy, ";", &save); entry;
                 entry = strtok_r(NULL, ";", &save), index++) {
                char *code = strchr(entry, '|');
                if (!code) continue;
                *code++ = 0;
                char *flags = strchr(code, '|');
                if (flags) *flags++ = 0; else flags = "";
                if (strcmp(entry, target) != 0) continue;
                if (strstr(flags, "slow")) sleep(2);
                if (strstr(flags, "once") && index < 64 && used[index]++) break;
                errno = atoi(code);
                return -1;
            }
        }
    }
    return real_write(fd, buf, count);
}
"""


@pytest.fixture(scope="module")
def shim(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not any(Path(p, "gcc").exists() for p in os.environ["PATH"].split(":")):
        pytest.skip("no compiler for the write shim")
    folder = tmp_path_factory.mktemp("shim")
    source = folder / "refuse.c"
    source.write_text(_SHIM)
    library = folder / "refuse.so"
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-O1", "-o", str(library), str(source), "-ldl"], check=True
    )
    return library


def _refuse(entries: dict[Path, str], library: Path) -> dict[str, str]:
    spec = ";".join(f"{path.resolve()}|{rule}" for path, rule in entries.items())
    return {"LD_PRELOAD": str(library), "BREAK_REFUSE": spec}


def _idle(workflow: Workflow, name: str) -> None:
    notes = workflow.worktree(int(name.split("-")[1])) / ".local" / "workflow"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"{name}.idle").touch()


def _codex_worker(workflow: Workflow, number: int, bridge: int, codex: int) -> tuple[Path, Path]:
    workflow.add_worktree(number)
    workflow.notes(number).mkdir(parents=True, exist_ok=True)
    (workflow.notes(number) / f"ticket-{number}.codex").touch()
    return (
        workflow.running(f"ticket-{number}", held=bridge),
        workflow.running(f"codex-{number}", held=codex),
    )


# --- the shim itself does what the tests count on ---------------------------------------------


def test_the_shim_makes_libc_write_fail_with_the_errno(shim: Path, tmp_path: Path) -> None:
    target = tmp_path / "memory.reclaim"
    target.write_text("")
    done = subprocess.run(
        [sys.executable, "-c", f"import os; os.write(os.open({str(target)!r}, os.O_WRONLY), b'1')"],
        env={**os.environ, **_refuse({target: str(errno.EAGAIN)}, shim)},
        capture_output=True,
        text=True,
    )
    assert "BlockingIOError" in done.stderr


# --- the daily flow: every idle session above 300 MB, every pass --------------------------------


def test_a_pass_over_many_idle_sessions_with_partial_reclaims_succeeds_every_time(
    workflow: Workflow, shim: Path
) -> None:
    """Several idle sessions, most of them reclaimed only in part, on pass after pass."""
    groups = {}
    for number, held in [(1, 4 * _GB), (2, 900 * _MB), (3, 12 * _GB), (4, 301 * _MB)]:
        workflow.add_worktree(number)
        groups[number] = workflow.running(f"ticket-{number}", held=held)
        _idle(workflow, f"ticket-{number}")
    bridge, codex = _codex_worker(workflow, 5, 2 * _GB, 6 * _GB)
    _idle(workflow, "ticket-5")
    partial = {groups[n] / "memory.reclaim": str(errno.EAGAIN) for n in (1, 3, 4)}
    partial[bridge / "memory.reclaim"] = str(errno.EAGAIN)
    partial[codex / "memory.reclaim"] = str(errno.EAGAIN)
    env = _refuse(partial, shim)

    for _ in range(3):
        done = workflow.run_script("tick", env=env)

        assert done.returncode == 0, done.stderr
        assert done.stderr == ""
        for number in (1, 2, 3, 4, 5):
            assert f"ticket-{number} squeezed:" in done.stdout
    assert (groups[2] / "memory.reclaim").read_text() == str(900 * _MB)


def test_a_partial_reclaim_of_the_codex_group_after_a_full_one_of_the_bridge(
    workflow: Workflow, shim: Path
) -> None:
    bridge, codex = _codex_worker(workflow, 7, 900 * _MB, 3 * _GB)

    done = workflow.run(
        "squeeze", "ticket-7", env=_refuse({codex / "memory.reclaim": str(errno.EAGAIN)}, shim)
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == "ticket-7 squeezed: 900 MB held\n"
    assert (bridge / "memory.reclaim").read_text() == str(900 * _MB)


def test_a_large_session_is_asked_for_all_it_holds(workflow: Workflow) -> None:
    """A session holding most of a 60 GB machine: the full byte count reaches the kernel."""
    group = workflow.running("ticket-7", held=58 * _GB + 12345)

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode == 0, done.stderr
    assert (group / "memory.reclaim").read_text() == str(58 * _GB + 12345)


def test_a_dd_with_any_wording_or_no_dd_at_all_decides_nothing(
    workflow: Workflow, shim: Path
) -> None:
    """After a coreutils upgrade `dd` may word EAGAIN any way, or fail on everything."""
    group = workflow.running("ticket-7", held=900 * _MB)
    workflow.fake("dd", "#!/bin/bash\necho 'dd: something new' >&2\nexit 9\n")

    done = workflow.run(
        "squeeze", "ticket-7", env=_refuse({group / "memory.reclaim": str(errno.EAGAIN)}, shim)
    )

    assert done.returncode == 0, done.stderr
    assert workflow.calls("dd") == []


def test_a_partial_reclaim_under_a_translated_locale_still_succeeds(
    workflow: Workflow, shim: Path
) -> None:
    group = workflow.running("ticket-7", held=900 * _MB)
    env = _refuse({group / "memory.reclaim": str(errno.EAGAIN)}, shim)
    env.update({"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"})

    done = workflow.run("squeeze", "ticket-7", env=env)

    assert done.returncode == 0, done.stderr


def test_a_reclaim_interrupted_by_a_signal_is_retried_not_failed(
    workflow: Workflow, shim: Path
) -> None:
    """The kernel returns EINTR from a reclaim when a signal is pending."""
    group = workflow.running("ticket-7", held=900 * _MB)

    done = workflow.run(
        "squeeze",
        "ticket-7",
        env=_refuse({group / "memory.reclaim": f"{errno.EINTR}|once"}, shim),
    )

    assert done.returncode == 0, done.stderr
    assert (group / "memory.reclaim").read_text() == str(900 * _MB)


# --- the failures that are real still fail -----------------------------------------------------


def test_a_real_kernel_refusal_fails_the_squeeze(workflow: Workflow) -> None:
    """A real errno from a real device, no shim: /dev/full refuses with ENOSPC."""
    group = workflow.running("ticket-7", held=900 * _MB)
    (group / "memory.reclaim").unlink()
    (group / "memory.reclaim").symlink_to("/dev/full")

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode == 1
    assert "memory.reclaim: No space left on device" in done.stderr
    assert done.stdout == ""


def test_a_kernel_without_memory_reclaim_fails_and_no_file_is_made(workflow: Workflow) -> None:
    group = workflow.running("ticket-7", held=900 * _MB)
    (group / "memory.reclaim").unlink()

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode == 1
    assert "memory.reclaim: No such file or directory" in done.stderr
    assert not (group / "memory.reclaim").exists()


def test_a_bridge_refused_for_another_reason_fails_and_the_tick_fails(
    workflow: Workflow, shim: Path
) -> None:
    workflow.add_worktree(7)
    group = workflow.running("ticket-7", held=900 * _MB)
    _idle(workflow, "ticket-7")

    done = workflow.run_script(
        "tick", env=_refuse({group / "memory.reclaim": str(errno.EPERM)}, shim)
    )

    assert done.returncode == 1
    assert "memory.reclaim: Operation not permitted" in done.stderr


def test_protect_fails_on_a_real_eagain(workflow: Workflow, shim: Path) -> None:
    group = workflow.running("ticket-7")

    done = workflow.run(
        "protect", "ticket-7", env=_refuse({group / "memory.low": str(errno.EAGAIN)}, shim)
    )

    assert done.returncode == 1
    assert done.stdout == ""


# --- two calls at once, a long reclaim ---------------------------------------------------------


def test_two_squeezes_of_one_session_at_once_both_succeed(workflow: Workflow, shim: Path) -> None:
    """A tick and an owner's squeeze meet on the same session while the kernel is slow."""
    group = workflow.running("ticket-7", held=900 * _MB)
    env = {
        **workflow.env,
        **_refuse({group / "memory.reclaim": f"{errno.EAGAIN}|slow"}, shim),
    }
    command = ["bash", str(workflow.script.parent / "session"), "squeeze", "ticket-7"]
    started = time.monotonic()
    both = [
        subprocess.Popen(command, cwd=workflow.repo, env=env, stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=30)[0] for process in both]

    assert [process.returncode for process in both] == [0, 0]
    assert outputs == ["ticket-7 squeezed: 900 MB held\n"] * 2
    assert time.monotonic() - started < 10


# --- exposure: the writer takes no shell and creates nothing --------------------------------


def test_a_value_or_path_with_shell_characters_is_written_as_it_is(tmp_path: Path) -> None:
    writer = Path(__file__).parents[2] / "scripts" / "workflow" / "group_write.py"
    target = tmp_path / "memory.low;touch pwned"
    target.write_text("old")

    done = subprocess.run(
        [sys.executable, "-B", str(writer), str(target), "$(touch pwned2)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stderr
    assert target.read_text() == "$(touch pwned2)"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["memory.low;touch pwned"]
