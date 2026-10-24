"""A stand-in program answered by a thread of the test instead of a process of its own.

The merge gate's tests call their `gh` stand-in hundreds of times, and a stand-in written in
Python spends most of each call starting Python: 36 ms, against 3 ms for bash. So the program on
the `PATH` is a few lines of bash that send their arguments and folder to this server over a
local socket and print what it answers. The answer comes from `answer(args, cwd, env, out, err)` in
a Python file the test writes, loaded again whenever that file changes, so a test can still change
its stand-in. `answer` writes to `out` and `err`, ends with `sys.exit` as a program does, and
returns "delegate" to have the call run by another stand-in in the same folder instead.
"""

import contextlib
import os
import runpy
import socketserver
import threading
import traceback
from io import StringIO
from pathlib import Path
from typing import Any

# Bash reads the answer up to each NUL: the exit code, standard error, then standard output.
_CLIENT = """#!/bin/bash
# Asked of the test's server (tests/stand_in_server.py).
exec 3<>/dev/tcp/127.0.0.1/@PORT@ || exit 1
printf '%s\\0' "$#" "$PWD" "$@" >&3
IFS= read -r -d '' code <&3
IFS= read -r -d '' err <&3
IFS= read -r -d '' out <&3
exec 3<&-
if [ "$code" = delegate ]; then exec "${0%/*}/@DELEGATE@" "$@"; fi
printf '%s' "$err" >&2
printf '%s' "$out"
exit "$code"
"""


class StandInServer:
    """Answers the calls of one stand-in from `source`, with `env` for what it runs."""

    def __init__(self, source: Path, env: dict[str, str]) -> None:
        self.source = source
        self.env = env
        self._loaded: str | None = None
        self._answer: Any = None
        self._lock = threading.Lock()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner.handle(self.request)

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        # A call that waits on the test (a pipe it has not opened) must not hold the end of it.
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def client(self, delegate: str) -> str:
        """The bash program to put on the `PATH`; `delegate` names the stand-in it may hand to."""
        port = str(self._server.server_address[1])
        return _CLIENT.replace("@PORT@", port).replace("@DELEGATE@", delegate)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _current(self) -> Any:
        with self._lock:
            # The text, not the file's time: a rewrite can keep both its size and its time.
            text = self.source.read_text()
            if text != self._loaded:
                # Compiled from the text each time it changes; no bytecode file is kept.
                namespace = runpy.run_path(str(self.source), run_name="stand_in")
                self._answer, self._loaded = namespace["answer"], text
            return self._answer

    def handle(self, connection: Any) -> None:
        received = b""
        fields: list[bytes] = []
        # The number of arguments, the folder, then the arguments, each ended by a NUL.
        while not fields or len(fields) < int(fields[0]) + 2:
            chunk: bytes = connection.recv(65536)
            if not chunk:
                return
            received += chunk
            *ended, received = received.split(b"\0")
            fields += ended
        cwd, *args = (os.fsdecode(field) for field in fields[1:])
        out, err = StringIO(), StringIO()
        try:
            code = 0
            if self._current()(args, cwd, self.env, out, err) == "delegate":
                connection.sendall(b"delegate\0")
                return
        except SystemExit as stop:
            # As Python does when a program ends with `sys.exit(...)`.
            if stop.code is None or isinstance(stop.code, int):
                code = stop.code or 0
            else:
                err.write(f"{stop.code}\n")
                code = 1
        except Exception:  # noqa: BLE001 - a program that fails prints its traceback and exits 1
            err.write(traceback.format_exc())
            code = 1
        reply = f"{code}\0{err.getvalue()}\0{out.getvalue()}"
        # The caller may have been stopped while it waited, as a timed-out command is.
        with contextlib.suppress(OSError):
            connection.sendall(reply.encode("utf-8", "surrogateescape"))
