"""`ci_proof.py` writes the notes the merge gate reads from a CI run, runs a repository's own test
command, and reads the `Workflow-Proof` line of a squash on main the way the gate writes it."""

import base64
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow

_API = "repos/example-org/example-app"
_NOTICE = re.compile(r"::notice title=([^:]*)::(.*)")


def annotations(printed: str) -> list[dict[str, str]]:
    """The annotations GitHub makes of the notices a job printed, as its API lists them."""
    found = []
    for line in printed.splitlines():
        if notice := _NOTICE.fullmatch(line):
            title, message = (urllib.parse.unquote(part) for part in notice.groups())
            found.append({"title": title, "message": message})
    return found


def _proof(
    workflow: Workflow, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(workflow.script.parent / "ci_proof.py"), *args],
        cwd=cwd or workflow.repo,
        env={**workflow.env, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _python(workflow: Workflow, code: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Runs `code` with `ci_proof` imported as `p` and the gate as `g`."""
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import ci_proof as p\n"
            + textwrap.dedent(code),
            str(workflow.script.parent),
            *args,
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _tested_merge(workflow: Workflow) -> tuple[str, str, str]:
    """A pull request's branch and GitHub's merge of it with main, checked out as CI checks it
    out; the merge, its tree and the branch's head."""
    workflow.git("checkout", "--quiet", "-b", "ticket/seven")
    (workflow.repo / "app.txt").write_text("changed\n")
    workflow.git("add", "app.txt")
    workflow.git("commit", "--quiet", "-m", "feat: seven")
    head = workflow.git("rev-parse", "HEAD")
    workflow.git("checkout", "--quiet", "main")
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "fix: main moved")
    workflow.git("merge", "--quiet", "--no-ff", "-m", "Merge seven into main", head)
    merge = workflow.git("rev-parse", "HEAD")
    workflow.git("checkout", "--quiet", "--detach", merge)
    return merge, workflow.git("rev-parse", "HEAD^{tree}"), head


@pytest.mark.parametrize("group", ["images", "browser_whole", "server_whole"])
@pytest.mark.parametrize("event", ["pull_request", "workflow_dispatch", "push"])
@pytest.mark.parametrize("configured", [False, True])
def test_prefix_only_output_defers_only_configured_needed_pr_groups(
    workflow: Workflow, group: str, event: str, configured: bool,
) -> None:
    (workflow.repo / "shipline.json").write_text(json.dumps({
        "checks": {"prefix_only": [group] if configured else []}
    }))
    output = workflow.root / "outputs"
    done = _proof(workflow, "prefix-only", "--needed", group,
                  env={"GITHUB_EVENT_NAME": event, "GITHUB_OUTPUT": str(output)})
    assert done.returncode == 0, done.stderr
    expected = [group] if configured and event == "pull_request" else []
    assert json.loads(output.read_text().split("=", 1)[1]) == expected
    assert annotations(done.stdout) == [{"title": "Prefix-only groups", "message": json.dumps(expected)}]


def test_a_group_no_path_filter_needs_is_not_deferred(workflow: Workflow) -> None:
    (workflow.repo / "shipline.json").write_text(json.dumps({
        "checks": {"prefix_only": ["images", "browser_whole", "server_whole"]}
    }))
    done = _proof(workflow, "prefix-only", env={"GITHUB_EVENT_NAME": "pull_request"})
    assert done.returncode == 0, done.stderr
    assert annotations(done.stdout) == [{"title": "Prefix-only groups", "message": "[]"}]


def test_a_whole_run_notes_its_tested_merge_its_files_and_whole_suites(
    workflow: Workflow,
) -> None:
    merge, tree, _ = _tested_merge(workflow)

    done = _proof(workflow, "notes", "--whole", "server_tests", "--whole", "web_suite")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        f"::notice title=Merge tested::{merge}",
        f"::notice title=Files tested::{tree}",
        "::notice title=Server tests::whole",
        "::notice title=Web suite::whole",
    ]


def test_a_selected_run_notes_how_many_of_its_suite_ran(workflow: Workflow) -> None:
    _tested_merge(workflow)

    done = _proof(workflow, "notes", "--selected", "server_tests", "3", "40", "files")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[2:] == ["::notice title=Server tests::selected: 3 of 40 files"]


def test_a_skipped_suite_is_noted_as_none(workflow: Workflow) -> None:
    _tested_merge(workflow)

    done = _proof(workflow, "notes", "--whole", "server_tests", "--none", "browser_suite")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[2:] == [
        "::notice title=Server tests::whole",
        "::notice title=Browser suite::none",
    ]


def test_the_notes_carry_the_titles_the_config_names(workflow: Workflow) -> None:
    (workflow.repo / "shipline.json").write_text(
        json.dumps({"annotations": {"merge_tested": "Merge: built, here", "server_tests": "Suite"}})
    )
    merge, _, _ = _tested_merge(workflow)

    done = _proof(workflow, "notes", "--whole", "server_tests")

    assert done.returncode == 0, done.stderr
    # A title is a property of the workflow command: its `:` and `,` are written escaped.
    assert done.stdout.splitlines()[0] == f"::notice title=Merge%3A built%2C here::{merge}"
    assert done.stdout.splitlines()[2] == "::notice title=Suite::whole"


@pytest.mark.parametrize(
    "args",
    [
        ["--whole", "unit_tests"],
        ["--whole", "server_tests", "--none", "server_tests"],
        ["--selected", "server_tests", "41", "40", "files"],
        ["--selected", "server_tests", "three", "40", "files"],
        ["--selected", "server_tests", "²", "40", "files"],
        ["--selected", "server_tests", "3", "40", ""],
        ["--selected", "server_tests", "3", "40", "files\n::error::forged"],
    ],
)
def test_a_note_that_cannot_be_read_back_is_refused(workflow: Workflow, args: list[str]) -> None:
    _tested_merge(workflow)

    done = _proof(workflow, "notes", *args)

    assert done.returncode == 2
    assert done.stdout == ""
    assert "usage: ci_proof.py notes" in done.stderr


def test_the_gate_reads_the_notes_as_written(workflow: Workflow) -> None:
    """What the gate takes from a planner's annotations, once GitHub holds the notes."""
    merge, tree, head = _tested_merge(workflow)
    written = _proof(workflow, "notes", "--whole", "server_tests", "--whole", "browser_suite")
    notes = annotations(written.stdout)

    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """\
                import json, sys
                sys.path.insert(0, sys.argv[1])
                import merge_gate as g
                notes, merge, head, tree = json.loads(sys.argv[2])
                base = "b" * 40
                g.api = lambda path, key=None, **given: (
                    {"parents": [{"sha": base}, {"sha": head}]} if path.endswith(merge) else notes
                )
                run = {"id": 5, "status": "completed", "conclusion": "success"}
                done = {"status": "completed", "conclusion": "success"}
                ci = [{"id": 1, "name": "CI: what to run", **done},
                      {"id": 2, "name": "CI result", **done},
                      {"id": 3, "name": "CI: server tests", **done}]
                e2e = [{"id": 4, "name": "E2E: what to run", **done},
                       {"id": 6, "name": "playwright", **done}]
                print(json.dumps([g.tested_merge(ci, base, head),
                                  g.whole_server_tests(run, ci, merge),
                                  g.whole_suite(run, e2e, tree), g.server_tests_selected(ci)]))
                """
            ),
            str(workflow.script.parent),
            json.dumps([notes, merge, head, tree]),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert json.loads(done.stdout) == [merge, True, True, False], done.stderr


@pytest.mark.parametrize(
    ("shards", "whole"), [([1, 2, 3, 4], True), ([1, 2, 4], False), ([1, 2, 2, 3, 4], False)]
)
def test_the_gate_takes_server_tests_in_parts_as_ci_proof_does(
    workflow: Workflow, shards: list[int], whole: bool
) -> None:
    """Shipline's own CI runs `CI: server tests (N of 4)`: the gate's whole-suite proof takes
    the parts as the one job, as `passed_jobs` does, and only when each part ran once."""
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """\
                import json, sys
                sys.path.insert(0, sys.argv[1])
                import merge_gate as g
                import ci_proof as p
                merge, shards = sys.argv[2], json.loads(sys.argv[3])
                notes = [{"title": "Merge tested", "message": merge},
                         {"title": "Server tests", "message": "whole"}]
                g.api = lambda path, key=None, **given: notes
                ok = {"status": "completed", "conclusion": "success"}
                parts = [{"id": 1, "name": "CI: what to run", **ok},
                         *({"id": 10 + n, "name": f"CI: server tests ({n} of 4)", **ok}
                           for n in shards),
                         {"id": 2, "name": "CI result", **ok}]
                print(json.dumps(["CI: server tests" in p.passed_jobs(parts),
                                  g.whole_server_tests({"id": 5, **ok}, parts, merge)]))
                """
            ),
            str(workflow.script.parent),
            _MERGE,
            json.dumps(shards),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert json.loads(done.stdout) == [whole, whole], done.stderr


def test_a_whole_suites_request_is_its_own_line(workflow: Workflow) -> None:
    done = _python(
        workflow,
        """
        print([p.whole_requested(message) for message in (
            "chore: test against current main\\n\\nWorkflow-Whole-Suites: true\\n",
            "chore: test against current main\\n\\nWorkflow-Whole-Suites: true, please\\n",
            "chore: test against current main\\n",
        )])
        """,
    )

    assert done.stdout == "[True, False, False]\n", done.stderr


def test_a_job_in_parts_passed_once_every_part_passed(workflow: Workflow) -> None:
    done = _python(
        workflow,
        """
        ok = {"status": "completed", "conclusion": "success"}
        parts = [
            {"name": "CI: server tests (1 of 2)", **ok},
            {"name": "CI: server tests (2 of 2)", **ok},
            {"name": "CI: web (1 of 2)", **ok},
            {"name": "CI: lint", "status": "completed", "conclusion": "skipped"},
            {"name": "CI: audit", "status": "completed", "conclusion": "failure"},
            {"name": "CI result", **ok},
        ]
        print(sorted(p.passed_jobs(parts)))
        """,
    )

    assert done.stdout == "['CI result', 'CI: server tests']\n", done.stderr


# What GitHub holds for a squash on main and the runs its proof names: the pull request's CI
# run 20 on its head, whose planner (job 21) noted the merge it tested, and E2E run 30, whose
# planner (job 31) noted its files.
_BASE, _HEAD, _TREE, _SQUASH, _MERGE = "a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40


def _github(proof: str) -> dict[str, Any]:
    done = {"status": "completed", "conclusion": "success"}
    run = {"head_sha": _HEAD, "head_repository": {"full_name": "example-org/example-app"}, **done}
    return {
        f"{_API}/git/commits/{_SQUASH}": {
            "message": f"feat: seven (#7)\n\n* feat: seven\n\n{proof}\n",
            "tree": {"sha": _TREE},
            "parents": [{"sha": _BASE}],
        },
        f"{_API}/actions/runs/20": {"id": 20, "path": ".github/workflows/ci.yml", **run},
        f"{_API}/actions/runs/20/jobs?per_page=100": {
            "jobs": [
                {"id": 21, "name": "CI: what to run", **done},
                {"id": 22, "name": "CI: server tests (1 of 2)", **done},
                {"id": 23, "name": "CI: server tests (2 of 2)", **done},
                {"id": 24, "name": "CI: web", **done},
                {"id": 25, "name": "CI: lint", "status": "completed", "conclusion": "failure"},
                {"id": 26, "name": "CI result", **done},
            ]
        },
        f"{_API}/check-runs/21/annotations?per_page=100": [
            {"title": "Merge tested", "message": _MERGE},
            {"title": "Server tests", "message": "whole"},
            {"title": "Web suite", "message": "whole"},
        ],
        f"{_API}/git/commits/{_MERGE}": {
            "tree": {"sha": _TREE},
            "parents": [{"sha": "9" * 40}, {"sha": _HEAD}],
        },
        f"{_API}/actions/runs/30": {"id": 30, "path": ".github/workflows/e2e.yml", **run},
        f"{_API}/actions/runs/30/jobs?per_page=100": {
            "jobs": [
                {"id": 31, "name": "E2E: what to run", **done},
                {"id": 32, "name": "playwright", **done},
            ]
        },
        f"{_API}/check-runs/31/annotations?per_page=100": [
            {"title": "Files tested", "message": _TREE},
            {"title": "Browser suite", "message": "whole"},
        ],
    }


def _receipt(**changed: Any) -> str:
    proof = {
        "base": _BASE,
        "tree": _TREE,
        "sources": [
            {"workflow": "ci", "run": 20, "jobs": ["CI result", "CI: server tests", "CI: web"]},
            {"workflow": "e2e", "run": 30, "jobs": ["E2E: what to run", "playwright"]},
        ],
        **changed,
    }
    return "Workflow-Proof: " + json.dumps(proof, separators=(",", ":"))


def _inherited(workflow: Workflow, github: dict[str, Any], name: str = "ci") -> dict[str, int]:
    done = _python(
        workflow,
        """
        import json
        github = json.loads(sys.argv[2])
        def read(path, key=None):
            reply = github[path]
            if reply is None:
                raise p.Unreadable(path)
            return reply[key] if key else reply
        print(json.dumps(p.inherited(sys.argv[3], sys.argv[4], read)))
        """,
        json.dumps(github),
        _SQUASH,
        name,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_proof_on_the_identical_tree_lets_main_inherit_the_jobs_that_passed(
    workflow: Workflow,
) -> None:
    github = _github(_receipt())

    assert _inherited(workflow, github) == {"CI result": 20, "CI: server tests": 20, "CI: web": 20}
    assert _inherited(workflow, github, "e2e") == {"E2E: what to run": 30, "playwright": 30}


@pytest.mark.parametrize("suite,key,name", [
    ("Server tests", "server_tests", "X"), ("Web suite", "web", "Web tests"),
])
@pytest.mark.parametrize("note", ["whole", "selected: 1 of 3 files", "none", None])
def test_renamed_jobs_are_inherited_only_when_their_whole_suite_is_proven(
    workflow: Workflow, suite: str, key: str, name: str, note: str | None,
) -> None:
    (workflow.repo / "shipline.json").write_text(json.dumps({"checks": {key: name}}))
    github = _github(_receipt(sources=[{"workflow": "ci", "run": 20, "jobs": [name]}]))
    ok = {"status": "completed", "conclusion": "success"}
    github[f"{_API}/actions/runs/20/jobs?per_page=100"]["jobs"] = [
        {"id": 21, "name": "CI: what to run", **ok},
        *({"id": 30 + n, "name": f"{name} ({n} of 3)", **ok} for n in (1, 2, 3)),
    ]
    notes = [{"title": "Merge tested", "message": _MERGE}]
    if note is not None:
        notes.append({"title": suite, "message": note})
    github[f"{_API}/check-runs/21/annotations?per_page=100"] = notes

    assert _inherited(workflow, github) == ({name: 20} if note == "whole" else {})


@pytest.mark.parametrize("whole", [True, False])
def test_the_gate_records_a_renamed_web_job_only_with_whole_suite_notes(
    workflow: Workflow, whole: bool,
) -> None:
    (workflow.repo / "shipline.json").write_text(json.dumps({"checks": {"web": "Web tests"}}))
    done = _python(workflow, """
        import merge_gate as g
        g.api = lambda *args, **kwargs: (
            [{"title": "Web suite", "message": "whole"}] if sys.argv[2] == "True" else []
        )
        ok = {"status": "completed", "conclusion": "success"}
        parts = [{"id": 1, "name": "CI: what to run", **ok},
                 {"id": 2, "name": "Web tests", **ok}]
        print(g.squash_proof("a" * 40, "b" * 40, {"ci": ({"id": 20}, parts)}))
    """, str(whole))
    assert done.returncode == 0, done.stderr
    proof = json.loads(done.stdout.removeprefix("Workflow-Proof: "))
    assert ("Web tests" in proof["sources"][0]["jobs"]) == whole


@pytest.mark.parametrize("invalid", [None, "tree", "failed", "fork", "missing part", "carried planner"])
def test_main_inherits_image_parts_only_from_valid_scratch_proof(
    workflow: Workflow, invalid: str | None,
) -> None:
    (workflow.repo / "shipline.json").write_text(json.dumps({"checks": {"prefix_only": ["images"]}}))
    github = _github(_receipt(sources=[{
        "workflow": "images", "run": 40, "jobs": ["Docker images: browser tests"]
    }]))
    ok = {"status": "completed", "conclusion": "success"}
    github[f"{_API}/actions/runs/40"] = {
        "id": 40, "event": "workflow_dispatch", "head_sha": _MERGE,
        "head_repository": {"full_name": "example-org/example-app"},
        "path": ".github/workflows/images.yml", **ok,
    }
    planner = {"id": 41, "name": "Docker images: what to run", **ok}
    parts = [planner, *({"id": 41 + n, "name": f"Docker images: browser tests ({n} of 3)", **ok}
                         for n in (1, 2, 3))]
    github[f"{_API}/actions/runs/40/jobs?per_page=100"] = {"jobs": parts}
    notes = [{"title": "Files tested", "message": _TREE}]
    github[f"{_API}/check-runs/41/annotations?per_page=100"] = notes
    if invalid == "tree":
        notes[0]["message"] = "0" * 40
    elif invalid == "failed":
        parts[-1]["conclusion"] = "failure"
    elif invalid == "fork":
        github[f"{_API}/actions/runs/40"]["head_repository"]["full_name"] = "example-other/example-app"
    elif invalid == "missing part":
        parts.pop()
    elif invalid == "carried planner":
        planner.update(run_id=40, run_attempt=2, head_sha=_MERGE,
                       created_at="2026-10-12T12:00:00Z", started_at="2026-10-12T10:00:00Z",
                       completed_at="2026-10-12T10:01:00Z")
        github[f"{_API}/check-runs/41/annotations?per_page=100"] = []
        github[f"{_API}/actions/runs/40/attempts/1/jobs?per_page=100"] = {"jobs": [
            {**planner, "id": 45, "run_attempt": 1, "created_at": "2026-10-12T09:59:00Z"}
        ]}
        github[f"{_API}/check-runs/45/annotations?per_page=100"] = notes
    expected = {"Docker images: browser tests": 40} if invalid in (None, "carried planner") else {}
    assert _inherited(workflow, github, "images") == expected


@pytest.mark.parametrize("difference", ["neutral", "excluded", "image", "rename", "mode", "workflow", "truncated", "unreadable"])
def test_image_inheritance_checks_the_complete_tree_difference(
    workflow: Workflow, difference: str,
) -> None:
    github = _github(_receipt(sources=[{
        "workflow": "images", "run": 40, "jobs": ["Docker images: image tests"]
    }]))
    old_tree, blob = "1" * 40, "2" * 40
    ok = {"status": "completed", "conclusion": "success"}
    github[f"{_API}/actions/runs/40"] = {
        "id": 40, "event": "workflow_dispatch", "head_sha": _MERGE,
        "head_repository": {"full_name": "example-org/example-app"},
        "path": ".github/workflows/images.yml", **ok,
    }
    github[f"{_API}/actions/runs/40/jobs?per_page=100"] = {"jobs": [
        {"id": 41, "name": "Docker images: what to run", **ok},
        {"id": 42, "name": "Docker images: image tests", **ok},
    ]}
    github[f"{_API}/check-runs/41/annotations?per_page=100"] = [
        {"title": "Files tested", "message": old_tree}
    ]
    image_workflow = {"path": ".github/workflows/images.yml", "type": "blob", "mode": "100644", "sha": blob}
    before = [image_workflow]
    after = [image_workflow.copy()]
    path = {"neutral": "docs/change.md", "excluded": "docker/README.md"}.get(difference, "docker/input")
    after.append({"path": path, "type": "blob", "mode": "100644", "sha": "3" * 40})
    if difference == "rename":
        before.append({**after.pop(), "path": "docker/old"})
        after.append({"path": "docs/old", "type": "blob", "mode": "100644", "sha": "3" * 40})
    if difference == "mode":
        before.append({**after[-1], "mode": "100755"})
    if difference == "workflow":
        after[:] = [{**image_workflow, "sha": "4" * 40}]
    github[f"{_API}/git/trees/{old_tree}?recursive=1"] = {"tree": before, "truncated": False}
    github[f"{_API}/git/trees/{_TREE}?recursive=1"] = {"tree": after, "truncated": difference == "truncated"}
    text = 'on:\n  pull_request:\n    paths:\n      - "docker/**"\n      - "!docker/**/*.md"\n'
    github[f"{_API}/git/blobs/{blob}"] = {
        "encoding": "base64", "content": base64.b64encode(text.encode()).decode()
    }
    if difference == "unreadable":
        github[f"{_API}/git/trees/{old_tree}?recursive=1"] = None
    expected = {"Docker images: image tests": 40} if difference in ("neutral", "excluded") else {}
    assert _inherited(workflow, github, "images") == expected


def test_the_gate_s_own_proof_is_read_back(workflow: Workflow) -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """\
                import json, sys
                sys.path.insert(0, sys.argv[1])
                import merge_gate as g
                g.api = lambda path, key=None, **given: [{"title": "Web suite", "message": "whole"}]
                ok = {"status": "completed", "conclusion": "success"}
                ci = [{"id": 21, "name": "CI: what to run", **ok},
                      {"id": 22, "name": "CI: web", **ok}]
                print(g.squash_proof(sys.argv[2], sys.argv[3], {"ci": ({"id": 20}, ci)}))
                """
            ),
            str(workflow.script.parent),
            _BASE,
            _TREE,
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.stdout.startswith("Workflow-Proof: {"), done.stderr

    assert _inherited(workflow, _github(done.stdout.strip())) == {
        "CI: what to run": 20,
        "CI: web": 20,
    }


@pytest.mark.parametrize(
    "change",
    [
        "another tree",
        "another parent",
        "a merge",
        "two proofs",
        "no proof",
        "not json",
        "a run by name",
        "a nested proof",
    ],
)
def test_a_proof_that_does_not_match_the_squash_is_never_inherited(
    workflow: Workflow, change: str
) -> None:
    github = _github(_receipt())
    squash = github[f"{_API}/git/commits/{_SQUASH}"]
    if change == "another tree":
        squash["tree"]["sha"] = "f" * 40
    elif change == "another parent":
        squash["parents"] = [{"sha": "f" * 40}]
    elif change == "a merge":
        squash["parents"].append({"sha": "f" * 40})
    elif change == "two proofs":
        # A branch's commit carried a line of its own, which the squash lists before the gate's.
        listed = "* feat: seven\n\n" + _receipt()
        squash["message"] = squash["message"].replace("* feat: seven", listed)
    elif change == "no proof":
        squash["message"] = "feat: seven (#7)\n"
    elif change == "not json":
        squash["message"] = "feat: seven (#7)\n\nWorkflow-Proof: {\"base\":\n"
    elif change == "a run by name":
        squash["message"] = squash["message"].replace('"run":20', '"run":"20"')
    elif change == "a nested proof":
        # Deeper than Python's JSON reader goes.
        squash["message"] = "feat: seven (#7)\n\nWorkflow-Proof: " + "[" * 100_000 + "\n"

    assert _inherited(workflow, github) == {}


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "running",
        "another workflow",
        "another merge tree",
        "a merge without parents",
        "another run's merge",
        "a fork's run",
        "no planner",
        "unreadable",
    ],
)
def test_a_source_run_that_does_not_prove_the_tree_is_never_inherited(
    workflow: Workflow, change: str
) -> None:
    github = _github(_receipt())
    run = github[f"{_API}/actions/runs/20"]
    if change == "failed":
        run["conclusion"] = "failure"
    elif change == "running":
        run.update(status="in_progress", conclusion=None)
    elif change == "another workflow":
        run["path"] = ".github/workflows/images.yml"
    elif change == "another merge tree":
        github[f"{_API}/git/commits/{_MERGE}"]["tree"]["sha"] = "f" * 40
    elif change == "a merge without parents":
        github[f"{_API}/git/commits/{_MERGE}"]["parents"] = []
    elif change == "another run's merge":
        # The run noted a merge of another head: its notes do not speak for what it ran.
        run["head_sha"] = "f" * 40
    elif change == "a fork's run":
        run["head_repository"] = {"full_name": "example-fork/example-app"}
    elif change == "no planner":
        github[f"{_API}/actions/runs/20/jobs?per_page=100"]["jobs"].pop(0)
    elif change == "unreadable":
        github[f"{_API}/check-runs/21/annotations?per_page=100"] = None

    assert _inherited(workflow, github) == {}
    # The other source still proves its own jobs.
    assert _inherited(workflow, github, "e2e") == {"E2E: what to run": 30, "playwright": 30}


def test_a_dispatched_run_proves_the_commit_it_ran_on(workflow: Workflow) -> None:
    """The gate's run of a scratch branch notes that branch's own head as the merge it tested."""
    github = _github(_receipt())
    github[f"{_API}/actions/runs/20"]["head_sha"] = _MERGE
    github[f"{_API}/git/commits/{_MERGE}"]["parents"] = [{"sha": _BASE}]

    assert _inherited(workflow, github) == {"CI result": 20, "CI: server tests": 20, "CI: web": 20}


def test_a_browser_run_of_a_fork_is_never_inherited(workflow: Workflow) -> None:
    github = _github(_receipt())
    github[f"{_API}/actions/runs/30"]["head_repository"] = {"full_name": "example-fork/x"}

    assert _inherited(workflow, github, "e2e") == {}


def test_a_suite_job_is_inherited_only_when_its_suite_ran_whole(workflow: Workflow) -> None:
    github = _github(_receipt())
    github[f"{_API}/check-runs/21/annotations?per_page=100"] = [
        {"title": "Merge tested", "message": _MERGE},
        {"title": "Server tests", "message": "selected: 3 of 40 files"},
        {"title": "Web suite", "message": "whole"},
    ]

    assert _inherited(workflow, github) == {"CI result": 20, "CI: web": 20}


def test_a_job_the_proof_names_is_inherited_only_when_it_passed_in_its_run(
    workflow: Workflow,
) -> None:
    receipt = _receipt(
        sources=[{"workflow": "ci", "run": 20, "jobs": ["CI: lint", "CI: server tests"]}]
    )

    assert _inherited(workflow, _github(receipt)) == {"CI: server tests": 20}


@pytest.mark.parametrize("missing", ["Files tested", "Browser suite"])
def test_a_browser_run_proves_only_the_whole_suite_on_the_tree(
    workflow: Workflow, missing: str
) -> None:
    github = _github(_receipt())
    notes = github[f"{_API}/check-runs/31/annotations?per_page=100"]
    github[f"{_API}/check-runs/31/annotations?per_page=100"] = [
        note for note in notes if note["title"] != missing
    ]

    assert _inherited(workflow, github, "e2e") == {}


def test_any_other_workflow_is_never_inherited(workflow: Workflow) -> None:
    github = _github(_receipt(sources=[{"workflow": "images", "run": 20, "jobs": ["CI result"]}]))

    assert _inherited(workflow, github, "images") == {}


def _gh(workflow: Workflow, github: dict[str, Any]) -> None:
    """A `gh` that answers `gh api --paginate PATH` from `github`, and fails on any other path."""
    (workflow.state / "github.json").write_text(json.dumps(github))
    workflow.fake(
        "gh",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            print("gh " + " ".join(sys.argv[1:]), file=open("@STATE@/calls.log", "a"))
            github = json.load(open("@STATE@/github.json"))
            if sys.argv[1:3] != ["api", "--paginate"] or sys.argv[3] not in github:
                sys.exit("HTTP 404: Not Found")
            reply = github[sys.argv[3]]
            paged = isinstance(reply, dict) and "_pages" in reply
            for page in reply["_pages"] if paged else [reply]:
                print(json.dumps(page))
            """
        ),
    )


def test_main_s_planner_names_each_inherited_job_and_hands_them_on(workflow: Workflow) -> None:
    _gh(workflow, _github(_receipt()))
    output = workflow.root / "github-output"

    done = _proof(
        workflow, "inherited", "ci", env={"GITHUB_SHA": _SQUASH, "GITHUB_OUTPUT": str(output)}
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "::notice title=Inherited job::CI result: run 20",
        "::notice title=Inherited job::CI: server tests: run 20",
        "::notice title=Inherited job::CI: web: run 20",
    ]
    assert output.read_text() == (
        'inherited=["CI result", "CI: server tests", "CI: web"]\n'
    )


def test_main_s_planner_reads_every_page_of_a_run_s_jobs(workflow: Workflow) -> None:
    github = _github(_receipt())
    jobs = github[f"{_API}/actions/runs/20/jobs?per_page=100"]["jobs"]
    github[f"{_API}/actions/runs/20/jobs?per_page=100"] = {
        "_pages": [{"jobs": jobs[:2]}, {"jobs": jobs[2:]}]
    }
    _gh(workflow, github)

    done = _proof(workflow, "inherited", "ci", env={"GITHUB_SHA": _SQUASH})

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == (
        'inherited=["CI result", "CI: server tests", "CI: web"]'
    )


def test_main_s_planner_inherits_nothing_when_github_cannot_be_read(workflow: Workflow) -> None:
    _gh(workflow, {})
    output = workflow.root / "github-output"

    done = _proof(
        workflow, "inherited", "ci", env={"GITHUB_SHA": _SQUASH, "GITHUB_OUTPUT": str(output)}
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert "every job runs" in done.stderr
    assert output.read_text() == "inherited=[]\n"


def test_main_s_planner_inherits_nothing_when_github_answers_too_deep_to_read(
    workflow: Workflow,
) -> None:
    # Deeper than Python's JSON reader goes.
    workflow.fake("gh", "#!/usr/bin/env python3\nprint('[' * 100_000)\n")

    done = _proof(workflow, "inherited", "ci", env={"GITHUB_SHA": _SQUASH})

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["inherited=[]"]
    assert "every job runs" in done.stderr


# The test job's command and the browser job's: each runs the command of its own name.
_COMMANDS = ["test", "browser"]


@pytest.mark.parametrize("command", _COMMANDS)
@pytest.mark.parametrize("checkout", ["main", "worktree"])
def test_the_test_command_is_the_one_the_config_names(
    workflow: Workflow, checkout: str, command: str
) -> None:
    """It runs from the root of the checkout it is called in."""
    other = next(name for name in _COMMANDS if name != command)
    (workflow.repo / "shipline.json").write_text(
        json.dumps(
            {
                "commands": {
                    command: "echo \"ran in $PWD\" > ran.txt; sh -c 'exit 3'",
                    other: "echo other > ran.txt",
                }
            }
        )
    )
    root = workflow.repo
    if checkout == "worktree":
        root = workflow.root / "worktree"
        workflow.git("worktree", "add", "--quiet", "--detach", str(root))

    done = subprocess.run(
        [sys.executable, "-B", str(root / "scripts/workflow/ci_proof.py"), command],
        cwd=root / "scripts",
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 3, done.stderr
    assert (root / "ran.txt").read_text() == f"ran in {root}\n"


@pytest.mark.parametrize("command", _COMMANDS)
def test_the_test_command_is_given_the_job_s_own_arguments(
    workflow: Workflow, command: str
) -> None:
    (workflow.repo / "shipline.json").write_text(
        json.dumps({"commands": {command: "printf '[%s]' run"}})
    )

    done = _proof(workflow, command, "--fast", "tests/a test.py")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "[run][--fast][tests/a test.py]"


@pytest.mark.parametrize(
    "command,tests", [("test", "tests"), ("browser", "browser tests")], ids=_COMMANDS
)
def test_a_repository_without_a_test_command_fails_its_test_job_saying_so(
    workflow: Workflow, command: str, tests: str
) -> None:
    done = _proof(workflow, command)

    assert done.returncode == 1
    assert done.stderr == (
        f"{workflow.repo / 'shipline.json'} names no commands.{command}, "
        f"the command that runs this repository's {tests}\n"
    )


def _lane(workflow: Workflow, home: Path, note: str | None) -> subprocess.CompletedProcess[str]:
    """`main-lane` with the lender's note in `home`, as the pool's planner sees it."""
    if note is not None:
        path = home / ".local/state/ci-overflow/home/first.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note)
    return _proof(workflow, "main-lane", env={"HOME": str(home), "GITHUB_OUTPUT": ""})


def _note(free: object, seconds_ago: float = 5) -> str:
    at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return json.dumps(
        {"number": None, "batch": None, "batch_free": free, "updated_at": at.isoformat()}
    )


@pytest.mark.parametrize(
    "note,lane",
    [
        # The lender saw one of main's runners online and idle with the merge line's lane.
        (lambda: _note(1), "true"),
        (lambda: _note(2), "true"),
        # None free: the prefix takes the ordinary choice and never waits on main's runners.
        (lambda: _note(0), "false"),
        # An old note: nobody labels main's runners now.
        (lambda: _note(1, seconds_ago=300), "false"),
        # On GitHub's own runners there is no note; nor is a broken one read as free.
        (lambda: None, "false"),
        (lambda: "{", "false"),
        (lambda: _note("1"), "false"),
        (lambda: _note(True), "false"),
        (lambda: json.dumps({"batch_free": 1}), "false"),
        (lambda: json.dumps({"batch_free": 1, "updated_at": "yesterday"}), "false"),
        (lambda: json.dumps([1]), "false"),
    ],
    ids=[
        "one free",
        "two free",
        "none free",
        "an old note",
        "no note",
        "a broken note",
        "a count as text",
        "a count as true",
        "no time",
        "a time that is none",
        "not an object",
    ],
)
def test_the_planner_takes_main_s_lane_only_when_the_lender_saw_it_free(
    workflow: Workflow, tmp_path: Path, note: Callable[[], str | None], lane: str
) -> None:
    # Written when the test runs: a note is fresh for a minute only.
    done = _lane(workflow, tmp_path / "home", note())

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"main_lane={lane}\n"


def test_planners_reading_one_note_take_no_more_lane_than_it_names_free(
    workflow: Workflow, tmp_path: Path
) -> None:
    """The gate dispatches several prefixes at once: their planners read the same note, and
    only as many as it names free take main's lane; a later note admits again."""
    home = tmp_path / "home"

    note = _note(2)

    taken = [_lane(workflow, home, note).stdout for _ in range(3)]

    assert taken == ["main_lane=true\n", "main_lane=true\n", "main_lane=false\n"]

    assert _lane(workflow, home, _note(1, seconds_ago=1)).stdout == "main_lane=true\n"


def test_a_planner_that_cannot_claim_the_lane_does_not_take_it(
    workflow: Workflow, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".local/state/ci-overflow").mkdir(parents=True)
    # The ledger is a file, not a folder: nothing can be claimed there.
    (home / ".local/state/ci-overflow/ledger").write_text("")

    assert _lane(workflow, home, _note(2)).stdout == "main_lane=false\n"


def test_old_claims_are_cleared(workflow: Workflow, tmp_path: Path) -> None:
    home = tmp_path / "home"
    ledger = home / ".local/state/ci-overflow/ledger"
    ledger.mkdir(parents=True)
    old = ledger / "main-lane-old-0"
    old.write_text("")
    os.utime(old, (0, 0))
    other = ledger / "another-planner-file"
    other.write_text("")
    os.utime(other, (0, 0))

    assert _lane(workflow, home, _note(1)).stdout == "main_lane=true\n"
    assert not old.exists() and other.exists()


@pytest.mark.parametrize(
    "updated_at",
    [
        # A note from the future, as a clock set wrong would write it.
        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        # A time without its zone.
        datetime.now(UTC).replace(tzinfo=None).isoformat(),
    ],
    ids=["from the future", "without its zone"],
)
def test_a_note_of_a_time_that_cannot_be_now_is_not_taken(
    workflow: Workflow, tmp_path: Path, updated_at: str
) -> None:
    note = json.dumps({"batch_free": 1, "updated_at": updated_at})

    assert _lane(workflow, tmp_path / "home", note).stdout == "main_lane=false\n"
