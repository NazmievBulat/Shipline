"""Breaking tests for pull request 67: CI job names the gate matches come from the config.

They replay a repository whose CI names its server tests `CI: Python (k of 3)`, beside a web
job, another service's job, a compatibility job, a dependency audit and the result, and whose planner notes
`Server tests`, `Web suite` and `Merge tested`. Its config names the server job `CI: Python`.
"""

import json
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from workflow.merge_gate_fixtures import (
    GitHub,
    _REPO,
    _batch_call,
    _batch_setup,
    _finish_batch,
    _merges,
    _prefixes,
    _rerun_failed_jobs,
    github,
)

_FIXTURES = (github,)

SERVER = "CI: Python"
OK = {"status": "completed", "conclusion": "success"}


def _configure(github: GitHub, checks: dict[str, Any]) -> None:
    path = github.workflow.repo / "shipline.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("checks", {}).update(checks)
    path.write_text(json.dumps(config))
    github.workflow.git("update-index", "--skip-worktree", "shipline.json")


def _ci_runs(github: GitHub) -> list[tuple[int, dict[str, Any]]]:
    """Each prefix's CI run ID, as `_finish_batch` numbered them, with its scratch."""
    found = []
    for index, member in enumerate(_prefixes(github)):
        scratch = member["scratch"]
        if scratch and "ci" in scratch["workflows"]:
            found.append((100 + index * 1000 + scratch["workflows"].index("ci") * 10, scratch))
    return found


def _installation_jobs(
    github: GitHub,
    *,
    server: str = SERVER,
    shards: list[int] = (1, 2, 3),
    failed_shard: int | None = None,
    server_note: str = "whole",
) -> None:
    """Every prefix's CI run lists the installation's jobs, all green unless one shard failed."""
    for identity, scratch in _ci_runs(github):
        jobs = github.api[f"{_REPO}/actions/runs/{identity}/jobs?per_page=100"]["jobs"]
        plan = next(job for job in jobs if job["name"] == "CI: what to run")
        result = next(job for job in jobs if job["name"] == "CI result")
        result["conclusion"] = "success"
        rest = [
            *(
                {"id": identity + 2 + n, "name": f"{server} ({n} of 3)", **OK,
                 **({"conclusion": "failure"} if n == failed_shard else {})}
                for n in shards
            ),
            {"id": identity + 6, "name": "CI: web", **OK},
            {"id": identity + 7, "name": "CI: worker", **OK},
            {"id": identity + 8, "name": "CI: older version on new tables", **OK},
            {"id": identity + 9, "name": "CI: dependencies", **OK},
        ]
        if failed_shard is not None:
            result["conclusion"] = "failure"
        jobs[:] = [plan, *rest, result]
        for job in rest:
            github.api[f"{_REPO}/check-runs/{job['id']}/annotations?per_page=100"] = []
        github.api[f"{_REPO}/check-runs/{identity + 1}/annotations?per_page=100"] = [
            {"title": "Server tests", "message": server_note},
            {"title": "Web suite", "message": "whole"},
            {"title": "Merge tested", "message": scratch["head"]},
        ]


@pytest.mark.parametrize("prefix_only", [None, ["server_whole"]])
def test_the_installations_green_prefix_with_sharded_python_jobs_merges(
    github: GitHub, prefix_only: list[str] | None,
) -> None:
    _batch_setup(github)
    _configure(github, {"server_tests": SERVER,
                        **({"prefix_only": prefix_only} if prefix_only else {})})
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    _installation_jobs(github)

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "did not prove" not in done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


def test_without_the_key_the_installations_prefix_is_still_short(github: GitHub) -> None:
    """Today's behaviour stays for a repository that names no server job: it must set the key."""
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    _installation_jobs(github)

    done = _batch_call(github)

    assert _merges(github) == []
    assert "did not prove its tested files" in done.stdout + done.stderr


@pytest.mark.parametrize("note", ["selected: 4 of 374 files", "none"])
def test_a_prefix_whose_python_shards_ran_a_selection_never_merges(
    github: GitHub, note: str,
) -> None:
    _batch_setup(github)
    _configure(github, {"server_tests": SERVER})
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    _installation_jobs(github, server_note=note)

    done = _batch_call(github)

    assert done.returncode != 0, done.stdout + done.stderr
    assert _merges(github) == []


def test_a_prefix_with_a_failed_python_shard_never_merges(github: GitHub) -> None:
    _batch_setup(github)
    _configure(github, {"server_tests": SERVER})
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    _installation_jobs(github, failed_shard=2)

    done = _batch_call(github)

    assert done.returncode != 0, done.stdout + done.stderr
    assert _merges(github) == []


def test_a_lost_python_shard_waits_then_the_late_shard_merges(github: GitHub) -> None:
    """GitHub lists two of the three shards; the third shows up a call later."""
    _batch_setup(github)
    _configure(github, {"server_tests": SERVER})
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    _installation_jobs(github, shards=[1, 3])

    waiting = _batch_call(github)

    assert _merges(github) == [], waiting.stdout + waiting.stderr
    assert "did not prove" not in waiting.stdout + waiting.stderr
    _installation_jobs(github)

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


def test_a_partial_rerun_of_one_python_shard_still_proves_the_whole_run(github: GitHub) -> None:
    """Shard 2 failed and only it was re-run: the planner and the other jobs are carried, so
    the planner's notes are those of the first attempt."""
    _batch_setup(github)
    _configure(github, {"server_tests": SERVER})
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    _installation_jobs(github)
    for identity, scratch in _ci_runs(github):
        notes = github.api[f"{_REPO}/check-runs/{identity + 1}/annotations?per_page=100"]
        jobs = github.api[f"{_REPO}/actions/runs/{identity}/jobs?per_page=100"]["jobs"]
        carried = [job["name"] for job in jobs if job["name"] not in (
            f"{SERVER} (2 of 3)", "CI result")]
        _rerun_failed_jobs(github, identity, scratch["head"], *carried)
        # The first attempt's planner keeps its notes; its carried copy has none.
        github.api[f"{_REPO}/check-runs/{identity + 1}/annotations?per_page=100"] = notes

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


def _gate(github: GitHub, code: str) -> subprocess.CompletedProcess[str]:
    workflow = github.workflow
    script = workflow.repo / "probe.py"
    script.write_text("import json, sys\nsys.path.insert(0, 'scripts/workflow')\n"
                      + textwrap.dedent(code))
    return subprocess.run([sys.executable, str(script)], cwd=workflow.repo, env=workflow.env,
                          capture_output=True, text=True, check=False, timeout=60)


def test_the_squash_proof_names_the_grouped_python_job_for_main_to_inherit(
    github: GitHub,
) -> None:
    _configure(github, {"server_tests": SERVER})
    done = _gate(github, """
        import merge_gate as g
        g.api = lambda *a, **k: [{"title": "Web suite", "message": "whole"},
                                 {"title": "Server tests", "message": "whole"}]
        ok = {"status": "completed", "conclusion": "success"}
        names = ["CI: what to run", "CI: Python (1 of 3)", "CI: Python (2 of 3)",
                 "CI: Python (3 of 3)", "CI: web", "CI: worker",
                 "CI: older version on new tables", "CI: dependencies", "CI result"]
        parts = [{"id": n, "name": name, **ok} for n, name in enumerate(names, 1)]
        grouped = g.checks(parts)
        print(json.dumps({
            "grouped": [job["name"] for job in grouped],
            "proof": json.loads(g.squash_proof("a" * 40, "b" * 40, {"ci": ({"id": 20}, parts)})
                                .removeprefix("Workflow-Proof: ")),
        }))
    """)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["grouped"].count(SERVER) == 1
    assert not any(name.startswith(f"{SERVER} (") for name in result["grouped"])
    jobs = result["proof"]["sources"][0]["jobs"]
    assert SERVER in jobs and "CI: web" in jobs and "CI: worker" in jobs


def test_image_shards_keep_their_own_names(github: GitHub) -> None:
    """The image workflow's sharded jobs are not grouped by the widened part pattern."""
    _configure(github, {"server_tests": SERVER})
    done = _gate(github, """
        import merge_gate as g
        ok = {"status": "completed", "conclusion": "success"}
        names = ["Docker images: what to run", "Docker images: browser tests (1 of 2)",
                 "Docker images: browser tests (2 of 2)", "Docker images: image tests",
                 "Docker images: release tests (1 of 2)", "Docker images: release tests (2 of 2)"]
        print(json.dumps([job["name"] for job in g.checks(
            [{"id": n, "name": name, **ok} for n, name in enumerate(names, 1)])]))
    """)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        "Docker images: what to run", "Docker images: browser tests (1 of 2)",
        "Docker images: browser tests (2 of 2)", "Docker images: image tests",
        "Docker images: release tests (1 of 2)", "Docker images: release tests (2 of 2)",
    ]


def test_a_shard_suffix_in_the_configured_name_is_not_accepted_silently(github: GitHub) -> None:
    """Low: a config naming the job with its suffix would never prove a whole run."""
    _configure(github, {"server_tests": "CI: Python (1 of 3)"})
    done = _gate(github, "import workflow_config\nprint(workflow_config.current().checks.server_tests)\n")
    assert done.returncode != 0 or done.stdout.strip() != "CI: Python (1 of 3)", done.stdout


@pytest.mark.parametrize("section,key", [("checks", "server_tests"), ("checks", "web"),
                                         ("checks", "older_version"),
                                         ("workflows", "ci_name"), ("workflows", "images_name")])
@pytest.mark.parametrize("value", [None, "", 3])
def test_a_mistyped_job_name_is_refused_not_matched_against_nothing(
    github: GitHub, section: str, key: str, value: Any,
) -> None:
    """A missing or mistyped setting: the config refuses it rather than never matching a job."""
    path = github.workflow.repo / "shipline.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault(section, {})[key] = value
    path.write_text(json.dumps(config))
    done = _gate(github, f"import workflow_config\n"
                         f"print(repr(workflow_config.current().{section}.{key}))\n")
    assert done.returncode != 0, done.stdout
