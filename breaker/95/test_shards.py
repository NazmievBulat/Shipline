"""Independent PR 95 checks; run this file alone from the checkout root."""

import ast
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
BASE = "aa4b353b046b22973d55e88991816985dd6def6c"


def job(name):
    match = re.search(
        rf"^  {name}:\n.*?(?=^  [\w-]+:|\Z)", WORKFLOW.read_text(), re.M | re.S
    )
    assert match
    return match[0]


def matrix():
    groups = re.split(r"^          - shard: ", job("tests"), flags=re.M)[1:]
    return [
        (
            int(group.splitlines()[0]),
            int(re.search(r"^            workers: (\d+)$", group, re.M)[1]),
            re.findall(r"^              (tests/\S+\.py)$", group, re.M),
        )
        for group in groups
    ]


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True, timeout=20
    ).stdout


def test_every_collectable_suite_file_has_one_nonempty_shard():
    groups = matrix()
    assert [number for number, _, _ in groups] == list(range(1, 9))
    assert all(files and workers in (4, 6) for _, workers, files in groups)
    actual = Counter(path for _, _, files in groups for path in files)
    expected = Counter(
        str(path.relative_to(ROOT))
        for folder in ("workflow", "runner", "tools")
        for path in (ROOT / "tests" / folder).rglob("*.py")
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    )
    assert actual == expected
    assert all(count == 1 for count in actual.values())
    # The only shard with the real CLI must still contain its caller.
    real_cli = re.search(r"if: matrix.shard == (\d+)\n        run: npm install", job("tests"))
    assert real_cli
    assert "tests/workflow/test_conflict_review.py" in dict(
        (number, files) for number, _, files in groups
    )[int(real_cli[1])]


def test_split_preserves_all_original_tests_decorators_and_helpers():
    path = "tests/workflow/test_merge_driver.py"
    original = ast.parse(git("show", f"{BASE}:{path}"))
    remaining = ast.parse((ROOT / path).read_text())
    moved = ast.parse((ROOT / "tests/workflow/test_merge_driver_repairs.py").read_text())

    def functions(tree):
        return Counter(
            ast.dump(node, include_attributes=False)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

    assert functions(original) == functions(remaining) + functions(moved)
    original_other = [node for node in original.body if not isinstance(node, ast.FunctionDef)]
    remaining_other = [node for node in remaining.body if not isinstance(node, ast.FunctionDef)]
    assert [ast.dump(node) for node in original_other] == [ast.dump(node) for node in remaining_other]
    assert len([node for node in moved.body if isinstance(node, ast.FunctionDef)]) == 23
    # Relocated fixtures are explicitly imported, rather than relying on test execution order.
    imported = {alias.name for node in moved.body if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert {"handed", "github"} <= imported


def test_workflow_keeps_hosted_runners_read_only_tokens_and_failure_isolation():
    text = WORKFLOW.read_text()
    assert "pull_request_target:" not in text
    assert re.search(r"^permissions:\n  contents: read$", text, re.M)
    assert not re.search(r"^\s+\w+: write$", text, re.M)
    for name in ("plan", "tests", "ci"):
        assert "runs-on: ubuntu-latest" in job(name)
    assert "timeout-minutes: 30" in job("tests")
    assert "fail-fast: false" in job("tests")
    assert 'name: "CI: server tests (${{ matrix.shard }} of 8)"' in job("tests")
    assert "needs: [plan, tests]" in job("ci")
    assert "if: always()" in job("ci")
    assert "persist-credentials: false" in job("tests")
    assert "--require-hashes -r requirements-test.txt" in job("tests")


@pytest.mark.parametrize("result,code", [("success success", 0), ("success skipped", 0),
    ("failure success", 1), ("success failure", 1), ("success cancelled", 1),
    ("cancelled success", 1), ("success timed_out", 1)])
def test_actual_result_step_rejects_failed_and_cancelled_dependencies(result, code):
    body = job("ci").split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in body.splitlines())
    done = subprocess.run(
        ["bash", "-eu", "-c", script], env={**os.environ, "RESULTS": result},
        cwd=ROOT, text=True, capture_output=True, timeout=10,
    )
    assert done.returncode == code, done.stdout + done.stderr


@pytest.mark.parametrize("workers", [4, 6])
@pytest.mark.parametrize("fails", [False, True])
def test_actual_parallel_command_runs_probe_and_propagates_failure(tmp_path, workers, fails):
    probe = tmp_path / "test_probe.py"
    probe.write_text("def test_probe():\n    assert " + str(not fails) + "\n")
    config = tmp_path / "shipline.json"
    config.write_text(json.dumps({"repository": "example-org/example-app", "commands": {
        "test": "python -m pytest -rs --cache-clear -o cache_dir=.local/probe-cache"
    }}))
    command = re.search(
        r"      - name: Tests\n        run: >-\n(.*?)(?=^      #)",
        job("tests"), re.M | re.S,
    )[1]
    command = " ".join(line.strip() for line in command.splitlines())
    command = command.replace("${{ matrix.workers }}", str(workers)).replace(
        "${{ matrix.files }}", shlex.quote(str(probe))
    )
    env = {**os.environ, "SHIPLINE_CONFIG": str(config), "SHIPLINE_REPO": str(ROOT),
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    done = subprocess.run(
        ["bash", "-eu", "-c", command], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=45,
    )
    assert done.returncode == int(fails), done.stdout + done.stderr
    assert ("1 failed" if fails else "1 passed") in done.stdout


def test_observed_current_head_shards_meet_amended_twenty_minute_target():
    observed = json.loads((Path(__file__).with_name("observed-run.json")).read_text())
    assert observed["conclusion"] == "success"
    assert observed["event"] == "pull_request"
    assert git("show", f"{observed['head_sha']}:.github/workflows/ci.yml") == WORKFLOW.read_text()
    shards = [part for part in observed["jobs"] if part["name"].startswith("CI: server tests (")]
    assert {part["name"] for part in shards} == {f"CI: server tests ({n} of 8)" for n in range(1, 9)}
    for part in shards:
        assert part["conclusion"] == "success"
        assert part["labels"] == ["ubuntu-latest"]
        duration = datetime.fromisoformat(part["completed_at"]) - datetime.fromisoformat(part["started_at"])
        assert duration.total_seconds() < 20 * 60, part["name"]
