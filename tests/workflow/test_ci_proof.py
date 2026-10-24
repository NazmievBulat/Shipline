"""`ci_proof.py` writes the notes the merge gate reads from a CI run, runs a repository's own test
command, and reads the `Workflow-Proof` line of a squash on main the way the gate writes it."""

import json
import re
import subprocess
import sys
import textwrap
import urllib.parse
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
                print(json.dumps([g.tested_merge(ci, base, head), g.whole_server_tests(run, ci, merge),
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


# What GitHub holds for a squash on main and the runs its proof names: CI run 20, whose planner
# (job 21) noted the merge it tested, and E2E run 30, whose planner (job 31) noted its files.
_BASE, _TREE, _SQUASH, _MERGE = "a" * 40, "c" * 40, "d" * 40, "e" * 40


def _github(proof: str) -> dict[str, Any]:
    done = {"status": "completed", "conclusion": "success"}
    return {
        f"{_API}/git/commits/{_SQUASH}": {
            "message": f"feat: seven (#7)\n\n* feat: seven\n\n{proof}\n",
            "tree": {"sha": _TREE},
            "parents": [{"sha": _BASE}],
        },
        f"{_API}/actions/runs/20": {"id": 20, "path": ".github/workflows/ci.yml", **done},
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
        f"{_API}/git/commits/{_MERGE}": {"tree": {"sha": _TREE}},
        f"{_API}/actions/runs/30": {"id": 30, "path": ".github/workflows/e2e.yml", **done},
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
                ci = [{"id": 21, "name": "CI: what to run", **ok}, {"id": 22, "name": "CI: web", **ok}]
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
        squash["message"] = squash["message"].replace("* feat: seven", "* feat: seven\n\n" + _receipt())
    elif change == "no proof":
        squash["message"] = "feat: seven (#7)\n"
    elif change == "not json":
        squash["message"] = "feat: seven (#7)\n\nWorkflow-Proof: {\"base\":\n"
    elif change == "a run by name":
        squash["message"] = squash["message"].replace('"run":20', '"run":"20"')

    assert _inherited(workflow, github) == {}


@pytest.mark.parametrize(
    "change", ["failed", "running", "another workflow", "another merge tree", "unreadable"]
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
    elif change == "unreadable":
        github[f"{_API}/check-runs/21/annotations?per_page=100"] = None

    assert _inherited(workflow, github) == {}
    # The other source still proves its own jobs.
    assert _inherited(workflow, github, "e2e") == {"E2E: what to run": 30, "playwright": 30}


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
            print(json.dumps(github[sys.argv[3]]))
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


def test_the_test_command_is_the_one_the_config_names(workflow: Workflow) -> None:
    (workflow.repo / "shipline.json").write_text(
        json.dumps({"commands": {"test": "echo \"ran in $PWD\" > ran.txt; sh -c 'exit 3'"}})
    )
    below = workflow.repo / "scripts"

    done = _proof(workflow, "test", "--fast", "tests/a test.py", cwd=below)

    assert done.returncode == 3, done.stderr
    assert (workflow.repo / "ran.txt").read_text() == f"ran in {workflow.repo}\n"


def test_the_test_command_is_given_the_job_s_own_arguments(workflow: Workflow) -> None:
    (workflow.repo / "shipline.json").write_text(
        json.dumps({"commands": {"test": "printf '[%s]' run"}})
    )

    done = _proof(workflow, "test", "--fast", "tests/a test.py")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "[run][--fast][tests/a test.py]"


def test_a_repository_without_a_test_command_fails_its_test_job_saying_so(
    workflow: Workflow,
) -> None:
    done = _proof(workflow, "test")

    assert done.returncode == 1
    assert done.stderr == (
        "shipline.json names no commands.test, the command that runs this repository's tests\n"
    )
