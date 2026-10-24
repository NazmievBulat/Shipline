"""FIFO machine-wide admission for pushes, using crash-safe kernel file locks."""

import fcntl
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

# The number of slots was chosen from measured hook resource use.
PUSH_SLOTS = 2
SLOT_FD_ENV = "WORKFLOW_PUSH_SLOT_FD"


def _queue() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    queue = state / "workflow/pushes"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _inherited(queue: Path) -> int | None:
    try:
        fd = int(os.environ.get(SLOT_FD_ENV, "-1"))
        held = os.fstat(fd)
        for number in range(PUSH_SLOTS):
            path = queue / f"slot-{number}.lock"
            if path.exists():
                candidate = path.stat()
                if (held.st_dev, held.st_ino) == (candidate.st_dev, candidate.st_ino):
                    return fd
    except (ValueError, OSError) as _error:
        pass
    return None


@contextmanager
def admission() -> Iterator[int]:
    queue = _queue()
    inherited = _inherited(queue)
    if inherited is not None:
        yield inherited
        return
    with (queue / "queue.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        ticket = queue / f"wait-{time.time_ns():020d}-{os.getpid()}"
        with ticket.open("w") as waiting:
            fcntl.flock(waiting, fcntl.LOCK_EX)
            fcntl.flock(guard, fcntl.LOCK_UN)
            announced = False
            slot: IO[str] | None = None
            try:
                while slot is None:
                    fcntl.flock(guard, fcntl.LOCK_EX)
                    try:
                        ahead = 0
                        for other in sorted(queue.glob("wait-*")):
                            if other == ticket:
                                break
                            with other.open("a+") as probe:
                                try:
                                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                except BlockingIOError:
                                    ahead += 1
                                else:
                                    other.unlink(missing_ok=True)
                        if ahead == 0:
                            for number in range(PUSH_SLOTS):
                                candidate = (queue / f"slot-{number}.lock").open("a+")
                                try:
                                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                except BlockingIOError:
                                    candidate.close()
                                else:
                                    slot = candidate
                                    ticket.unlink()
                                    break
                        if slot is None and not announced:
                            print(
                                f"waiting for a push slot: {ahead} ahead",
                                file=sys.stderr,
                                flush=True,
                            )
                            announced = True
                    finally:
                        fcntl.flock(guard, fcntl.LOCK_UN)
                    if slot is None:
                        time.sleep(0.1)
                with slot:
                    yield slot.fileno()
            finally:
                ticket.unlink(missing_ok=True)


if __name__ == "__main__":
    with admission() as slot_fd:
        # The hook shell retains the lock if this supervisor or its launching git dies.
        sys.exit(
            subprocess.run(
                sys.argv[1:],
                env={**os.environ, SLOT_FD_ENV: str(slot_fd)},
                pass_fds=(slot_fd,),
                check=False,
            ).returncode
        )
