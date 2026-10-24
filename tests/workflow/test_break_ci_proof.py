"""Tests that try to break `ci_proof.py` and the gate's reading of what it writes (pull request
#6). Each states what the change claims, and checks it at a boundary the change's own tests leave
open."""

import json
import subprocess
import sys
import textwrap
from typing import Any

import pytest
from workflow_harness import Workflow

_API = "repos/example-org/example-app"
_BASE, _HEAD, _TREE, _SQUASH, _MERGE = "a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40
_OK = {"status": "completed", "conclusion": "success"}


def _run(workflow: Workflow, code: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Runs `code` with `ci_proof` imported as `p`."""
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


def _github() -> dict[str, Any]:
    """A squash on main whose proof names CI run 20, which passed whole on the squash's tree."""
    proof = {
        "base": _BASE,
        "tree": _TREE,
        "sources": [
            {"workflow": "ci", "run": 20, "jobs": ["CI result", "CI: server tests"]}
        ],
    }
    return {
        f"{_API}/git/commits/{_SQUASH}": {
            "message": "feat: seven (#7)\n\nWorkflow-Proof: "
            + json.dumps(proof)
            + "\n",
            "tree": {"sha": _TREE},
            "parents": [{"sha": _BASE}],
        },
        f"{_API}/actions/runs/20": {
            "id": 20,
            "path": ".github/workflows/ci.yml",
            "head_sha": _MERGE,
            "head_repository": {"full_name": "example-org/example-app"},
            **_OK,
        },
        f"{_API}/actions/runs/20/jobs?per_page=100": {
            "jobs": [
                {"id": 21, "name": "CI: what to run", **_OK},
                {"id": 22, "name": "CI: server tests (1 of 4)", **_OK},
                {"id": 23, "name": "CI: server tests (2 of 4)", **_OK},
                {"id": 24, "name": "CI: server tests (3 of 4)", **_OK},
                {"id": 25, "name": "CI: server tests (4 of 4)", **_OK},
                {"id": 26, "name": "CI result", **_OK},
            ]
        },
        f"{_API}/check-runs/21/annotations?per_page=100": [
            {"title": "Merge tested", "message": _MERGE},
            {"title": "Files tested", "message": _TREE},
            {"title": "Server tests", "message": "whole"},
        ],
        f"{_API}/git/commits/{_MERGE}": {
            "tree": {"sha": _TREE},
            "parents": [{"sha": _BASE}],
        },
    }


def _inherited(
    workflow: Workflow, github: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    return _run(
        workflow,
        """
        import json
        github = json.loads(sys.argv[2])
        def read(path, key=None):
            reply = github[path]
            if reply is None:
                raise p.Unreadable(path)
            return reply[key] if key else reply
        print(json.dumps(p.inherited(sys.argv[3], "ci", read)))
        """,
        json.dumps(github),
        _SQUASH,
    )


def test_the_baseline_is_inherited(workflow: Workflow) -> None:
    done = _inherited(workflow, _github())

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"CI result": 20, "CI: server tests": 20}


def test_a_noted_merge_without_parents_inherits_nothing_rather_than_failing_main(
    workflow: Workflow,
) -> None:
    """`ci_proof.py`: "whatever cannot be read means every job runs". A noted commit GitHub
    lists with no parents (a root commit of the same tree) is such an answer; it must not stop
    main's planner, which would turn main's CI red."""
    github = _github()
    github[f"{_API}/git/commits/{_MERGE}"]["parents"] = []
    github[f"{_API}/actions/runs/20"]["head_sha"] = "f" * 40

    done = _inherited(workflow, github)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {}


def test_an_answer_too_deep_to_read_inherits_nothing_rather_than_failing_main(
    workflow: Workflow,
) -> None:
    """The same claim for the reader of `gh api` itself: an answer it cannot read must read as
    unreadable, as a proof line too deep already does."""
    done = _run(
        workflow,
        """
        import json, subprocess
        # The config is read first, with the real git; only `gh api` answers too deep.
        p.workflow_config.current()
        def run(command, **given):
            assert command[:2] == ["gh", "api"], command
            return subprocess.CompletedProcess(command, 0, stdout="[" * 100_000, stderr="")
        p.subprocess.run = run
        print(json.dumps(p.inherited(sys.argv[2], "ci")))
        """,
        _SQUASH,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {}


@pytest.mark.parametrize(
    "state", ["none", "selected: 40 of 40 files", "Whole", " whole"]
)
def test_a_server_job_is_inherited_only_on_the_exact_whole_note(
    workflow: Workflow, state: str
) -> None:
    github = _github()
    github[f"{_API}/check-runs/21/annotations?per_page=100"][2]["message"] = state

    done = _inherited(workflow, github)

    assert done.returncode == 0, done.stderr
    # " whole" reads as "whole", as the gate strips a note too.
    expected = (
        {"CI result": 20, "CI: server tests": 20}
        if state == " whole"
        else {"CI result": 20}
    )
    assert json.loads(done.stdout) == expected


@pytest.mark.parametrize(
    "shards", [[1, 2, 3], [1, 2, 3, 3], [1, 2, 4, 4], [2, 3, 4, 5]], ids=str
)
def test_a_sharded_job_with_a_lost_or_doubled_shard_is_never_inherited(
    workflow: Workflow, shards: list[int]
) -> None:
    """A shard lost, re-run in part and listed twice, or numbered past its count."""
    github = _github()
    jobs = github[f"{_API}/actions/runs/20/jobs?per_page=100"]["jobs"]
    github[f"{_API}/actions/runs/20/jobs?per_page=100"]["jobs"] = [
        jobs[0],
        *(
            {"id": 40 + n, "name": f"CI: server tests ({n} of 4)", **_OK}
            for n in shards
        ),
        jobs[-1],
    ]

    done = _inherited(workflow, github)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"CI result": 20}


def test_a_squash_s_forged_second_proof_disables_inheritance_without_failing(
    workflow: Workflow,
) -> None:
    """A branch commit's own `Workflow-Proof` line, listed in the squash before the gate's,
    makes main run every job: never a skip on the branch's word, never a red planner."""
    github = _github()
    squash = github[f"{_API}/git/commits/{_SQUASH}"]
    forged = {
        "base": _BASE,
        "tree": _TREE,
        "sources": [{"workflow": "ci", "run": 99, "jobs": []}],
    }
    squash["message"] = squash["message"].replace(
        "\n\nWorkflow-Proof",
        "\n\n* feat\n\nWorkflow-Proof: " + json.dumps(forged) + "\n\nWorkflow-Proof",
    )

    done = _inherited(workflow, github)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {}


def test_the_gate_takes_shipline_s_own_sharded_server_tests_as_ci_proof_does(
    workflow: Workflow,
) -> None:
    """Shipline's own CI, which the change says proves the pieces on Shipline itself, runs the
    server tests as `CI: server tests (N of 4)`. `ci_proof.py` takes such a run, green and noted
    whole, as `CI: server tests` passed whole. The gate's check of a dispatched whole run
    (`whole_server_tests`) must take the same run, or every merge of a repository whose CI is
    shaped like Shipline's is refused with "did not prove its tested files"."""
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
                merge = sys.argv[2]
                notes = [{"title": "Merge tested", "message": merge},
                         {"title": "Server tests", "message": "whole"}]
                g.api = lambda path, key=None, **given: notes
                ok = {"status": "completed", "conclusion": "success"}
                run = {"id": 5, **ok}
                parts = [{"id": 1, "name": "CI: what to run", **ok},
                         *({"id": 1 + n, "name": f"CI: server tests ({n} of 4)", **ok}
                           for n in range(1, 5)),
                         {"id": 9, "name": "CI result", **ok}]
                print(json.dumps(["CI: server tests" in p.passed_jobs(parts),
                                  g.whole_server_tests(run, parts, merge)]))
                """
            ),
            str(workflow.script.parent),
            _MERGE,
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [True, True]


def test_the_notes_of_a_title_with_a_line_break_stay_one_notice(
    workflow: Workflow,
) -> None:
    """A title from the config cannot add a workflow command of its own."""
    (workflow.repo / "shipline.json").write_text(
        json.dumps({"annotations": {"server_tests": "Suite\n::error::forged"}})
    )

    done = subprocess.run(
        [
            sys.executable,
            "-B",
            str(workflow.script.parent / "ci_proof.py"),
            "notes",
            "--whole",
            "server_tests",
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert not any(line.startswith("::error") for line in done.stdout.splitlines())
    assert len(done.stdout.splitlines()) == 3
