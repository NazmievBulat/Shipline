"""Durable review authorization, shared by the gate and the tick's merge driver."""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        json.dump(value, output)
        temporary = output.name
    os.replace(temporary, path)


@dataclass
class Handoff:
    head: str
    round: int
    branch: str
    children: list[str] | None = None

    @staticmethod
    def path(folder: Path, number: str) -> Path:
        return folder / f"merge-wanted-{number}"

    @classmethod
    def read(cls, folder: Path, number: str) -> "Handoff | None":
        path = cls.path(folder, number)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        note = cls(**data)
        if (
            not isinstance(note.head, str)
            or not re.fullmatch(r"[0-9a-f]{40}", note.head)
            or type(note.round) is not int
            or note.round < 1
            or not isinstance(note.branch, str)
            or not note.branch
            or (
                note.children is not None
                and (
                    not isinstance(note.children, list)
                    or any(not isinstance(n, str) or not n.isdigit() for n in note.children)
                )
            )
        ):
            raise ValueError("invalid merge handoff")
        return note

    def save(self, folder: Path, number: str) -> None:
        write_json(self.path(folder, number), asdict(self))


def authorized(folder: Path, number: str, head: str) -> bool:
    # The durable request revokes permission immediately, even if a crash precedes
    # deletion of the old note or removal of the train's cached approval.
    if (folder / f"merge-review-{number}").exists():
        return False
    try:
        note = Handoff.read(folder, number)
        return note is not None and note.head == head and note.children is None
    except OSError, TypeError, ValueError:
        return False
