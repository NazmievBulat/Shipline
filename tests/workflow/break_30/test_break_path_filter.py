"""Breaking tests for pull request 30: the gate's image check read by GitHub's path filter rules.

Each test replays how a repository's image workflow is filtered in daily use, with neutral names,
and checks the gate waits for an image run exactly when GitHub would start one.
"""

import importlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "workflow"

_GIT_ENV = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "owner@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "owner@example.com",
}

# About twenty entries: whole folders, single files and one single-level glob of Go sources in
# a service folder that also has nested test data.
_ENTRIES = [
    "docker/**",
    "tests/images/**",
    "example-app/**",
    "example-server/Dockerfile",
    "example-server/go.mod",
    "example-server/go.sum",
    "example-server/*.go",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "example-app/package.json",
    "README.md",
    "tests/fixtures/seed.json",
    "scripts/build-image.sh",
    "config/nginx.conf",
    "config/env.example",
    ".dockerignore",
    "Makefile",
    "requirements.txt",
    "requirements-lock.txt",
]


def _workflow(entries: list[str], *, block_types: bool = False) -> str:
    listed = "".join(f'      - "{entry}"\n' for entry in entries)
    types = (
        "    types:\n"
        "      - ready_for_review\n"
        "      - opened\n"
        "      - synchronize\n"
        "      - reopened\n"
        if block_types
        else "    types: [ready_for_review, opened, synchronize, reopened]\n"
    )
    return (
        "name: Images\n"
        "\n"
        "on:\n"
        "  pull_request:\n"
        f"{types}"
        "    paths:\n"
        f"{listed}"
        "  push:\n"
        "    branches: [main]\n"
        "    paths:\n"
        f"{listed}"
        "  schedule:\n"
        '    - cron: "17 3 * * *"\n'
        "  workflow_dispatch:\n"
        "\n"
        "concurrency:\n"
        "  group: images-${{ github.ref }}\n"
        "  cancel-in-progress: true\n"
        "\n"
        "jobs:\n"
        "  plan:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        '      - run: "echo plan"\n'
    )


@pytest.fixture(scope="module")
def gate() -> Any:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("merge_gate")
    finally:
        sys.path.remove(str(_SCRIPTS))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], *, remove: tuple[str, ...] = ()) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if files:
        _git(repo, "add", *files)
    if remove:
        _git(repo, "rm", "--quiet", *remove)
    _git(repo, "commit", "--quiet", "-m", "chore: change")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, value in _GIT_ENV.items():
        monkeypatch.setenv(name, value)
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _base(gate: Any, repo: Path, *, block_types: bool = False) -> str:
    name = gate.WORKFLOW_FILES["images"]
    return _commit(
        repo,
        {
            f".github/workflows/{name}": _workflow(_ENTRIES, block_types=block_types),
            "example-server/main.go": "package main\n",
            "example-server/testdata/case.go": "package testdata\n",
            "README.md": "x\n",
        },
    )


@pytest.mark.parametrize("block_types", [False, True])
def test_the_real_shape_of_the_workflow_is_read(gate: Any, block_types: bool) -> None:
    # Types as a flow list and as a block list, a push filter, a schedule and jobs after it.
    text = _workflow(_ENTRIES, block_types=block_types)
    assert gate.pull_request_paths(text) == _ENTRIES


def test_windows_line_endings_are_read(gate: Any) -> None:
    text = _workflow(_ENTRIES).replace("\n", "\r\n")
    assert gate.pull_request_paths(text) == _ENTRIES


@pytest.mark.parametrize(
    "changed",
    [
        # Nested test data under the service whose filter is one level of Go sources.
        "example-server/testdata/case.go",
        "example-server/testdata/golden/out.go",
        "example-server/internal/handler.go",
        # Siblings beside single-file entries.
        "example-server/go.work",
        "example-server/go.sum.orig",
        "example-server/package-lock.json",
        "docs/README.md",
        "README.markdown",
        "tests/fixtures/seed.json.bak",
        "tests/fixtures/other.json",
        "config/nginx.conf.d/extra.conf",
        "requirements-dev.txt",
        # Beside the folders the filter names.
        "dockerfiles/a",
        "tests/e2e/a.spec.ts",
    ],
)
def test_a_file_the_filter_does_not_name_needs_no_run(
    gate: Any, repo: Path, changed: str
) -> None:
    base = _base(gate, repo)
    head = _commit(repo, {changed: "x\n"})
    assert not gate.images_needed(head, base, [])
    # The same change reaching main: main's health never waits for an image run.
    assert not gate.images_needed(head, base, [], workflow_ref=head)


@pytest.mark.parametrize(
    "changed",
    [
        "example-server/main.go",
        "example-server/handler.go",
        "example-server/go.sum",
        "docker/app/Dockerfile",
        "tests/images/a/b/c.sh",
        "example-app/src/index.ts",
        "package-lock.json",
        "README.md",
        "tests/fixtures/seed.json",
        ".dockerignore",
    ],
)
def test_a_file_the_filter_names_needs_a_run(gate: Any, repo: Path, changed: str) -> None:
    base = _base(gate, repo)
    head = _commit(repo, {changed: "y\n"})
    assert gate.images_needed(head, base, [])


def test_a_deleted_file_the_filter_names_needs_a_run(gate: Any, repo: Path) -> None:
    base = _base(gate, repo)
    head = _commit(repo, {}, remove=("example-server/main.go",))
    assert gate.images_needed(head, base, [])


def test_a_main_push_of_several_merges_is_read_as_one_range(gate: Any, repo: Path) -> None:
    # Main merges several times an hour; one push run's range can hold several merges.
    before = _base(gate, repo)
    _commit(repo, {"example-server/testdata/one.go": "x\n"})
    _commit(repo, {"docs/README.md": "x\n"})
    quiet = _commit(repo, {"example-server/package-lock.json": "x\n"})
    assert not gate.images_needed(quiet, before, [], workflow_ref=quiet)
    loud = _commit(repo, {"example-server/extra.go": "x\n"})
    assert gate.images_needed(loud, before, [], workflow_ref=loud)


def test_main_reads_the_push_base_its_ci_recorded(
    gate: Any, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _base(gate, repo)
    _commit(repo, {"example-server/main.go": "package main // a\n"})
    head = _commit(repo, {"example-server/testdata/case.go": "package testdata // b\n"})
    ci = [{"id": 1, "event": "push"}]
    monkeypatch.setattr(gate, "jobs", lambda run: [{"name": gate.CI_PLAN, "id": 7}])
    monkeypatch.setattr(
        gate, "api", lambda path, key=None, **_: [{"title": "Push base", "message": before}]
    )
    # The range holds a named file, so main waits for its run.
    assert gate.main_images_needed(head, ci)
    # A later manual CI run, with no push payload: only the head's own merge counts.
    assert not gate.main_images_needed(head, [{"id": 2, "event": "workflow_dispatch"}])


def test_the_image_check_on_the_pull_request_still_counts(gate: Any, repo: Path) -> None:
    # A late or re-run image check that GitHub did start is waited for, whatever the files.
    base = _base(gate, repo)
    head = _commit(repo, {"docs/README.md": "x\n"})
    rollup = [{"name": f"{gate.IMAGES}: build"}]
    assert gate.images_needed(head, base, rollup)


def test_a_missing_workflow_is_refused(gate: Any, repo: Path) -> None:
    base = _commit(repo, {"README.md": "x\n"})
    head = _commit(repo, {"example-server/main.go": "x\n"})
    with pytest.raises(gate.Refused):
        gate.images_needed(head, base, [])


def test_odd_file_names_are_matched_whole(gate: Any, repo: Path) -> None:
    base = _base(gate, repo)
    # A newline, a quote and a non-UTF-8 byte in a name below the service folder.
    name = os.fsdecode(b"example-server/testdata/a\nb\"\xff.go")
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x\n")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "--quiet", "-m", "chore: odd")
    head = _git(repo, "rev-parse", "HEAD")
    assert not gate.images_needed(head, base, [])


def test_real_patterns_against_a_long_name_are_fast(gate: Any) -> None:
    matches = gate.path_filter(_ENTRIES)
    names = [
        "example-server/" + "a" * 4000 + ".gox",
        "example-server/" + "/".join(["d"] * 1500) + "/x.go",
        "docker" + "/a" * 2000,
        "x" * 4096,
    ]
    started = time.monotonic()
    for name in names * 50:
        matches(name)
    assert time.monotonic() - started < 2


def test_two_calls_at_once_agree(gate: Any) -> None:
    matches = gate.path_filter(_ENTRIES)
    cases = {
        "example-server/main.go": True,
        "example-server/testdata/case.go": False,
        "README.md": True,
        "docs/README.md": False,
    }
    wrong: list[str] = []

    def check() -> None:
        for _ in range(2000):
            for name, expected in cases.items():
                if matches(name) is not expected:
                    wrong.append(name)

    threads = [threading.Thread(target=check) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not wrong


def test_a_refusal_names_no_more_than_the_line(gate: Any) -> None:
    # A line the gate cannot read is quoted back in the refusal, which reaches comments.
    text = "on:\n  pull_request:\n    paths:\n      - 'docker/**'\n"
    with pytest.raises(gate.Refused) as raised:
        gate.pull_request_paths(text)
    assert str(raised.value) == "could not read the Images workflow path filter: - 'docker/**'"
