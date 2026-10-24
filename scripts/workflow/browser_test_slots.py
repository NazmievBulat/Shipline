"""Machine-wide FIFO admission for local browser runs, held by kernel file locks."""

import fcntl
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import workflow_config


def run(command: list[str], root: Path, env: dict[str, str]) -> int:
    count = int(env.get("WORKFLOW_BROWSER_SLOTS", "3"))
    if count < 1:
        raise ValueError("WORKFLOW_BROWSER_SLOTS must be a positive integer")
    state = Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    queue = state / "workflow/browser-tests"
    queue.mkdir(parents=True, exist_ok=True)
    notes = root / workflow_config.current().state_folder
    notes.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        activity = stack.enter_context((notes / "browser-tests.lock").open("a+"))
        fcntl.flock(activity, fcntl.LOCK_SH)
        guard = stack.enter_context((queue / "queue.lock").open("a+"))
        fcntl.flock(guard, fcntl.LOCK_EX)
        ticket = queue / f"wait-{time.time_ns():020d}-{os.getpid()}"
        waiting = stack.enter_context(ticket.open("w"))
        fcntl.flock(waiting, fcntl.LOCK_EX)
        fcntl.flock(guard, fcntl.LOCK_UN)
        announced = False
        try:
            while True:
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
                        for number in range(count):
                            slot = (queue / f"slot-{number}.lock").open("a+")
                            try:
                                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                slot.close()
                                continue
                            stack.enter_context(slot)
                            ticket.unlink()
                            # The child retains admission if this supervisor is killed.
                            fcntl.flock(guard, fcntl.LOCK_UN)
                            return subprocess.run(
                                command,
                                cwd=root,
                                env=env,
                                pass_fds=(slot.fileno(), activity.fileno()),
                                check=False,
                            ).returncode
                    if not announced:
                        print(
                            f"waiting for a browser test slot, {ahead} ahead",
                            file=sys.stderr,
                            flush=True,
                        )
                        announced = True
                finally:
                    fcntl.flock(guard, fcntl.LOCK_UN)
                time.sleep(0.1)
        finally:
            ticket.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:], Path.cwd(), dict(os.environ)))
