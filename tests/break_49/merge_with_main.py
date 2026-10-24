"""Builds the tree the breaking tests also run on: pull request 49 merged with main, its two
conflicts in ci.yml resolved the plain way (each side's condition kept, joined with `&&`).

    python3 tests/break_49/merge_with_main.py <empty folder>
    SHIPLINE_TREE=<that folder> python3 -m pytest -p no:xdist tests/break_49

Only ci.yml, examples/ci.yml and RULES.md are written: the tests read nothing else."""

import re
import subprocess
import sys
from pathlib import Path

PULL = "origin/ticket/draft-runs-no-ci"
MAIN = "origin/main"
GUARD = "github.event_name != 'pull_request' || github.event.pull_request.draft == false"


def show(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], check=True, capture_output=True, text=True
    ).stdout


def merged_ci() -> str:
    text = show(MAIN, ".github/workflows/ci.yml")
    pull = show(PULL, ".github/workflows/ci.yml")
    text = text.replace(
        "  pull_request:\n  push:",
        "  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n  push:",
    )
    assert "ready_for_review" in pull and "ready_for_review" in text
    text, shards = re.subn(
        r"(    if: >-\n      )\$\{\{ (!contains\(fromJSON\(needs\.plan\.outputs\.inherited\), "
        r"'CI: server tests'\)) \}\}",
        lambda found: f"{found[1]}${{{{ ({GUARD}) && {found[2]} }}}}",
        text,
    )
    text, result = re.subn(
        r"(    name: CI\n)    if: always\(\)\n", rf"\1    if: always() && ({GUARD})\n", text
    )
    assert shards == 1 and result == 1, (shards, result)
    return text


def main(folder: Path) -> None:
    files = {
        ".github/workflows/ci.yml": merged_ci(),
        "examples/ci.yml": show(MAIN, "examples/ci.yml"),
        "scripts/workflow/RULES.md": show(PULL, "scripts/workflow/RULES.md"),
    }
    for path, text in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_text(text)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
