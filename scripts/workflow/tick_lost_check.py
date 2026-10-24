"""Whether GitHub lost a check of a reviewer or audit that waits for its checks before it joined
the merge line, by the gate's own definition (`merge_gate.lost_check`). The tick calls it:

    tick_lost_check.py <state folder> <number> < pull.json

It exits 0 when a check was lost, noted for the wake once per head (`merge_gate.note_lost`), 3
when none was, and 2 when a run could not be read; any other end is a failure too, never "none".
A shadow tick (`tick --shadow`) notes it for its own wake, in SHIPLINE_SHADOW_NOTES, unless the
installation's own notes hold it; the installation's notes are read, never written.
Each workflow run is read at most once per tick: its answer, an unreadable one too, is kept in
the tick's own folder, which the tick removes when it exits. Outside a tick every run is read.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import merge_gate
import train_reads

CACHE = "lost-check-runs.json"
NONE = 3


class Unreadable(Exception):
    """GitHub gave no run for the id; a later tick asks again."""


def reader(folder: Path | None) -> Callable[[str], dict[str, Any]]:
    def read(run: str) -> dict[str, Any]:
        cache = train_reads.load(folder / CACHE) if folder else {}
        if run not in cache:
            try:
                answer = merge_gate.read_run(run)
                if not isinstance(answer, dict):
                    raise ValueError(f"not a run: {answer!r}")
                cache[run] = {"run": answer}
            except (merge_gate.Refused, ValueError) as error:
                cache[run] = {"unreadable": str(error)}
            if folder:
                train_reads.save(folder / CACHE, cache)
        if "unreadable" in cache[run]:
            raise Unreadable(cache[run]["unreadable"])
        return cache[run]["run"]

    return read


def main(folder: Path, number: str) -> int:
    pull = json.load(sys.stdin)
    try:
        found = merge_gate.lost_check(pull, read=reader(train_reads.tick()))
    except Unreadable as error:
        print(f"cannot read a run of pull request {number}'s checks: {error}", file=sys.stderr)
        return 2
    if not found:
        return NONE
    if os.environ.get("SHIPLINE_SHADOW"):
        name = f"{number}-{pull['headRefOid']}.json"
        if any((folder / f"lost-checks.{kind}" / name).exists() for kind in ("pending", "sent")):
            return 0
        notes = os.environ.get("SHIPLINE_SHADOW_NOTES")
        if not notes:
            return 0
        folder = Path(notes)
    merge_gate.note_lost(folder, number, pull["headRefOid"], found)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(Path(sys.argv[1]), sys.argv[2]))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"cannot look for a lost check: {error}", file=sys.stderr)
        sys.exit(2)
