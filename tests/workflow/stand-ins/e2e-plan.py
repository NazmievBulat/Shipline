"""A stand-in for the browser-test planner the merge gate asks, `scripts/e2e-plan.py`.

The tests read only its answer to one question: whether a change needs browser tests. A change
to the workflow, the tests, the docs or Markdown alone needs none; any other change does, and so
does an empty one, which the planner cannot tell from a misread.
"""

_NONE_NEEDED = (b"scripts/workflow/", b"tests/", b"docs/")


def browser_needed(changed: list[bytes]) -> bool:
    return not changed or not all(
        path.startswith(_NONE_NEEDED) or path.endswith(b".md") for path in changed
    )
