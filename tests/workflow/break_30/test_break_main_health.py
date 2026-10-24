"""Breaking tests for pull request 30, end to end: a merge to main that changes only a file the
image filter does not name leaves main green and the next pull request merges."""

import pytest
from test_merge_when_current import GitHub, github  # noqa: F401  (the fixture)

_FILTER = (
    "on:\n"
    "  pull_request:\n"
    "    types: [ready_for_review, opened, synchronize, reopened]\n"
    "    paths:\n"
    '      - "docker/**"\n'
    '      - "example-server/*.go"\n'
    '      - "example-server/go.sum"\n'
    '      - "README.md"\n'
    "  push:\n"
    "    branches: [main]\n"
    "    paths:\n"
    '      - "docker/**"\n'
    '      - "example-server/*.go"\n'
    '      - "example-server/go.sum"\n'
    '      - "README.md"\n'
    "  schedule:\n"
    '    - cron: "17 3 * * *"\n'
)


def _filter_on_main(github: GitHub) -> None:
    workflow = github.workflow
    workflow.git("checkout", "main")
    (workflow.repo / ".github/workflows/images.yml").write_text(_FILTER)
    workflow.git("add", ".github")
    workflow.git("commit", "-m", "chore: image filter")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")


def _merge_to_main(github: GitHub, files: dict[str, str]) -> None:
    workflow = github.workflow
    workflow.git("checkout", "main")
    for name, text in files.items():
        path = workflow.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        workflow.git("add", name)
    workflow.git("commit", "-m", "fix: a merge")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head)
    github.save()


@pytest.mark.parametrize(
    "changed",
    [
        "example-server/testdata/case.go",
        "example-server/testdata/golden/deep.go",
        "example-server/go.sum.orig",
        "docs/README.md",
    ],
)
def test_main_stays_green_and_the_next_merge_goes(github: GitHub, changed: str) -> None:
    _filter_on_main(github)
    _merge_to_main(github, {changed: "x\n"})

    status = github.workflow.run_script("main-status").stdout
    assert status.startswith("main: green"), status
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("changed", ["example-server/main.go", "example-server/go.sum"])
def test_main_waits_for_the_run_github_starts(github: GitHub, changed: str) -> None:
    _filter_on_main(github)
    _merge_to_main(github, {changed: "x\n"})

    status = github.workflow.run_script("main-status").stdout
    assert "main: pending, Docker images have not started" in status, status
    assert github.run().returncode == 1


def test_a_filter_entry_the_gate_cannot_read_says_why(github: GitHub) -> None:
    # A plain (unquoted) entry is valid YAML that GitHub reads; the gate refuses the filter.
    workflow = github.workflow
    workflow.git("checkout", "main")
    (workflow.repo / ".github/workflows/images.yml").write_text(
        _FILTER.replace('      - "README.md"\n', "      - README.md\n", 1)
    )
    workflow.git("add", ".github")
    workflow.git("commit", "-m", "chore: image filter")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    _merge_to_main(github, {"example-server/testdata/case.go": "x\n"})

    status = github.workflow.run_script("main-status").stdout
    assert "could not read the Images workflow path filter: - README.md" in status, status
