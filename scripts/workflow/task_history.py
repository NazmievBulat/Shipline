"""Materialize the opening task and a diff excluding workflow bookkeeping sections."""

import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

import workflow_config

_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      includesCreatedEdit
      lastEditedAt
      userContentEdits(last: 1) { nodes { diff deletedAt } }
      timelineItems(first: 1, itemTypes: [RENAMED_TITLE_EVENT]) {
        nodes { ... on RenamedTitleEvent { previousTitle } }
      }
    }
  }
}"""
_BOOKKEEPING = {
    "answers given for the owner",
    "questions for the owner",
    "weaker tests",
    "weaker-tests",
    "weaker-tests explanations",
    "worker notes",
    "worker's notes",
    "choices made",
    "worker choices",
}


def _task_sections(text: str) -> list[str]:
    lines: list[str] = []
    skipping: int | None = None
    fenced = False
    for line in text.splitlines():
        if line.startswith(("```", "~~~")):
            fenced = not fenced
        heading = None if fenced else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            level = len(heading[1])
            if skipping is not None and level <= skipping:
                skipping = None
            if skipping is None and heading[2].casefold() in _BOOKKEEPING:
                skipping = level
        if skipping is None:
            lines.append(line)
    # Adding a trailing bookkeeping section must not introduce a blank-line-only diff.
    return [line + "\n" for line in "\n".join(lines).strip().splitlines()]


def main() -> None:
    number, folder = sys.argv[1:]
    root = Path(folder)
    task = json.load(sys.stdin)
    title, body = task["title"], task["body"]
    note = ""
    owner, name = workflow_config.current().repository.split("/")
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_QUERY}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={number}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        response = json.loads(result.stdout)
        if response.get("errors"):
            raise ValueError("GraphQL errors")
        history = response["data"]["repository"]["pullRequest"]
        edits = history["userContentEdits"]["nodes"]
        if edits:
            if not history["includesCreatedEdit"] or edits[0]["deletedAt"] is not None:
                raise ValueError("opening revision unavailable")
            body = edits[0]["diff"]
            if not isinstance(body, str):
                raise ValueError("opening body unavailable")
        elif history.get("lastEditedAt") is not None:
            raise ValueError("edit history empty for an edited body")
        renamed = history["timelineItems"]["nodes"]
        if renamed:
            title = renamed[0]["previousTitle"]
            if not isinstance(title, str):
                raise ValueError("opening title unavailable")
    except (
        subprocess.SubprocessError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
    ):
        title, body = task["title"], task["body"]
        note = "History unavailable: reviewing the current description; opening task could not be read.\n"
    current = f"# {task['title']}\n\n{task['body']}\n"
    opened = f"# {title}\n\n{body}\n"
    (root / "task.md").write_text(current)
    (root / "task-as-opened.md").write_text(opened)
    (root / "task-history.md").write_text(note)
    (root / "task-changes.md").write_text(
        note
        or "".join(
            difflib.unified_diff(
                _task_sections(opened),
                _task_sections(current),
                fromfile="task-as-opened.md",
                tofile="task.md",
            )
        )
    )
    if note:
        print(note, end="")


if __name__ == "__main__":
    main()
