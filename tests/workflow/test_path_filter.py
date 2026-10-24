"""The merge gate reads a workflow's `paths:` by GitHub's filter rules, so it waits for an image
run exactly when GitHub starts one (scripts/workflow/merge_gate.py)."""

import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"

_GIT_ENV = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


@pytest.fixture(scope="module")
def gate() -> Any:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("merge_gate")
    finally:
        sys.path.remove(str(_SCRIPTS))


def _images_workflow(*patterns: str) -> str:
    entries = "".join(f'      - "{pattern}"\n' for pattern in patterns)
    return f"name: Images\non:\n  pull_request:\n    paths:\n{entries}  push:\n"


def _matches(gate: Any, patterns: list[str], path: str) -> bool:
    return gate.path_filter(patterns)(path)


def test_a_star_stays_in_its_folder(gate: Any) -> None:
    assert _matches(gate, ["src/*.go"], "src/main.go")
    assert not _matches(gate, ["src/*.go"], "src/testdata/stub.go")


@pytest.mark.parametrize(
    "pattern,matched,unmatched",
    [
        ("*", ["README.md", ".env"], ["docs/README.md"]),
        ("docs/*", ["docs/a.md"], ["docs/a/b.md", "docs"]),
        ("**", ["README.md", "a/b/c.txt"], []),
        ("docs/**", ["docs/a.md", "docs/a/b/c.md"], ["docs", "src/docs/a.md"]),
        ("**.js", ["a.js", "src/lib/a.js"], ["a.ts"]),
        # `**/` stands for no folder as well as any number of them.
        ("**/README.md", ["README.md", "a/README.md", "a/b/README.md"], ["XREADME.md"]),
        ("docs/**/*.md", ["docs/a.md", "docs/x/y/a.md"], ["docs/a.txt", "docsa.md"]),
        # `?` and `+` repeat what stands before them: zero or one, one or more.
        ("docs/a?b.md", ["docs/b.md", "docs/ab.md"], ["docs/aab.md", "docs/axb.md"]),
        ("docs/a+b.md", ["docs/ab.md", "docs/aaab.md"], ["docs/b.md"]),
        ("v[0-9]?.txt", ["v.txt", "v1.txt"], ["v12.txt"]),
        ("v[0-9]+.txt", ["v1.txt", "v123.txt"], ["v.txt", "va.txt"]),
        # A set is one character of it, a range included.
        ("v[0-9].txt", ["v0.txt", "v9.txt"], ["v10.txt", "va.txt", "v.txt"]),
        ("[abc].txt", ["a.txt", "c.txt"], ["d.txt", "ab.txt"]),
        ("[a-cX-Z0-1].txt", ["b.txt", "Y.txt", "1.txt"], ["d.txt", "W.txt", "2.txt"]),
        ("[-a].txt", ["-.txt", "a.txt"], ["b.txt"]),
        (r"[\]a].txt", ["].txt", "a.txt"], ["b.txt"]),
        # Repeating a repeated part repeats it once: `a+?` is any number of a.
        ("a+?b", ["b", "ab", "aaab"], ["xb"]),
        ("**/+a", ["a", "x/y/a"], ["xa"]),
        # A backslash makes the next character itself.
        (r"\*.txt", ["*.txt"], ["a.txt"]),
        (r"a\?.txt", ["a?.txt"], ["a.txt"]),
        (r"\[a].txt", ["[a].txt"], ["a.txt"]),
        (r"\!keep.txt", ["!keep.txt"], ["keep.txt"]),
        # Every other character is itself.
        ("a.b", ["a.b"], ["axb"]),
        ("a(b)|c{1}$^", ["a(b)|c{1}$^"], ["ab"]),
        ("a!b", ["a!b"], ["ab"]),
        # What GitHub does not document is read as the characters themselves.
        ("?a", ["?a"], ["a"]),
        ("+a", ["+a"], ["a", "aa"]),
        ("v[0-9.txt", ["v[0-9.txt"], ["v1.txt"]),
        ("[].txt", ["[].txt"], [".txt"]),
        ("[z-a].txt", ["z.txt", "-.txt", "a.txt"], ["m.txt"]),
        ("end\\", ["end\\"], ["end"]),
        # A range is within a-z, A-Z or 0-9; any other is the characters themselves, so a
        # range never reaches `/`.
        ("a[.-0]b", ["a.b", "a-b", "a0b"], ["a/b", "a1b"]),
        ("[0-z].txt", ["0.txt", "-.txt", "z.txt"], ["5.txt", "a.txt"]),
        # `**/` is zero or more folders only where a folder's name starts; inside a name,
        # `**` is any characters and the `/` stays.
        ("docs**/*.md", ["docs/a.md", "docsx/y/a.md"], ["docsREADME.md", "docs.md"]),
        ("src/a**/b.go", ["src/a/b.go", "src/ax/y/b.go"], ["src/ab.go"]),
    ],
)
def test_each_pattern_kind(
    gate: Any, pattern: str, matched: list[str], unmatched: list[str]
) -> None:
    for path in matched:
        assert _matches(gate, [pattern], path), (pattern, path)
    for path in unmatched:
        assert not _matches(gate, [pattern], path), (pattern, path)


def test_a_negated_pattern_takes_back_an_earlier_match_and_a_later_one_brings_it_back(
    gate: Any,
) -> None:
    patterns = ["src/**", "!src/generated/**", "src/generated/keep.go"]
    assert _matches(gate, patterns, "src/main.go")
    assert not _matches(gate, patterns, "src/generated/stub.go")
    assert _matches(gate, patterns, "src/generated/keep.go")
    # The last pattern that matches decides, whatever came before it.
    assert not _matches(gate, ["src/generated/keep.go", "!src/**"], "src/generated/keep.go")


def test_negated_patterns_alone_match_nothing(gate: Any) -> None:
    assert not _matches(gate, ["!docs/**"], "src/main.go")
    assert not _matches(gate, ["!docs/**"], "docs/a.md")
    assert not _matches(gate, ["!*", "!**"], "README.md")


def test_the_pull_request_filter_is_read_as_written(gate: Any) -> None:
    text = _images_workflow("docker/**", r"!docker/**/*.md", r"src/\\[gen].go")
    # The push filter is the push's own and never counts for a pull request.
    text += '    paths:\n      - "push-only/**"\n'
    assert gate.pull_request_paths(text) == ["docker/**", "!docker/**/*.md", r"src/\[gen].go"]
    with pytest.raises(gate.Refused):
        gate.pull_request_paths("name: Images\non:\n  pull_request:\n  push:\n")
    with pytest.raises(gate.Refused):
        gate.pull_request_paths("name: Images\non:\n  push:\n")


def test_only_the_pull_request_paths_list_is_read(gate: Any) -> None:
    text = (
        "name: Images\n"
        "on:\n"
        "  pull_request:\n"
        "    branches:\n"
        '      - "main"\n'
        "    paths:  # what the images are built from\n"
        "\n"
        '      - "docker/**"  # the Dockerfiles\n'
        "      # a note between entries\n"
        '      - "src/*.go"\n'
        "    types: [opened]\n"
        "jobs:\n"
        "  build:\n"
        "    needs:\n"
        '      - "lint"\n'
    )
    assert gate.pull_request_paths(text) == ["docker/**", "src/*.go"]
    # Without a `paths:` list, `paths-ignore:` is never read as one.
    ignored = 'on:\n  pull_request:\n    paths-ignore:\n      - "docs/**"\n'
    with pytest.raises(gate.Refused):
        gate.pull_request_paths(ignored)


@pytest.mark.parametrize(
    "entry",
    ["'src/**'", "src/**", r'"src/\x41"', '"src/**" extra', '  - "src/**"'],
)
def test_an_entry_the_gate_cannot_read_is_refused_not_skipped(gate: Any, entry: str) -> None:
    text = f'on:\n  pull_request:\n    paths:\n      - "docker/**"\n      - {entry}\n'
    with pytest.raises(gate.Refused):
        gate.pull_request_paths(text)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", *files)
    _git(repo, "commit", "--quiet", "-m", "chore: change")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, value in _GIT_ENV.items():
        monkeypatch.setenv(name, value)
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_images_needed_reads_the_changed_files_by_the_filter(gate: Any, repo: Path) -> None:
    workflow = _images_workflow("src/*.go", "docker/**", "!docker/**/*.md")
    base = _commit(repo, {".github/workflows/images.yml": workflow, "README.md": "x\n"})

    # One file, in a folder below the one the filter names: GitHub starts no run.
    stub = _commit(repo, {"src/testdata/stub.go": "package stub\n"})
    assert not gate.images_needed(stub, base, [])
    # One file the filter names.
    main = _commit(repo, {"src/main.go": "package main\n"})
    assert gate.images_needed(main, stub, [])

    # Several files: none named, one taken back, then one of them named.
    several = _commit(
        repo, {"src/a/b.go": "package b\n", "docker/notes.md": "x\n", "docs/a.md": "x\n"}
    )
    assert not gate.images_needed(several, main, [])
    named = _commit(repo, {"docs/b.md": "x\n", "docker/app/Dockerfile": "FROM x\n"})
    assert gate.images_needed(named, several, [])


def test_a_bare_star_never_matches_the_end_of_the_file_list(gate: Any, repo: Path) -> None:
    base = _commit(repo, {".github/workflows/images.yml": _images_workflow("*"), "a/b.txt": "x\n"})
    head = _commit(repo, {"a/c.txt": "x\n"})
    assert not gate.images_needed(head, base, [])
    assert gate.images_needed(_commit(repo, {"top.txt": "x\n"}), head, [])


def test_a_new_branch_lists_its_whole_tree(gate: Any, repo: Path) -> None:
    workflow = _images_workflow("docker/**", "!docker/**/*.md")
    head = _commit(repo, {".github/workflows/images.yml": workflow, "docker/README.md": "x\n"})
    assert not gate.images_needed(head, "0" * 40, [], workflow_ref=head)
    _commit(repo, {"docker/app/Dockerfile": "FROM x\n"})
    # The last commit changes only README.md, but a new branch's whole tree counts.
    tip = _commit(repo, {"README.md": "y\n"})
    assert gate.images_needed(tip, "0" * 40, [], workflow_ref=tip)
