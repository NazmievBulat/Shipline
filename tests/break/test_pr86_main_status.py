"""Real main-status recovery while GitHub check evidence is missing or delayed."""

import json
import sys

import pytest

from test_pr86_recovery import Repository, ROOT


@pytest.fixture
def main_status(tmp_path):
    repo = Repository(tmp_path)
    (repo.main / "shipline.json").write_text(json.dumps({
        "repository": "example-org/example-app", "runner": {"labels": ["example-app-home"]}}))
    repo.broken_branch()
    binary = tmp_path / "bin"
    binary.mkdir()
    source = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
root = Path(os.environ["BREAK_TEST_ROOT"])
mode = (root / "check-mode").read_text()
args = sys.argv[1:]
with (root / "gh-calls.jsonl").open("a") as output:
    output.write(json.dumps(args) + "\\n")
if args[:2] == ["run", "rerun"]:
    sys.exit(0)
path = args[-1]
head = os.environ["BREAK_TEST_HEAD"]
status = "in_progress" if mode == "partial" else "completed"
conclusion = None if mode == "partial" else "failure" if mode == "failed" else "success"
ci = {{"id": 10, "head_sha": head, "event": "push", "head_branch": "main",
       "status": status, "conclusion": conclusion,
       "run_attempt": 2 if mode in ("partial", "green") else 1,
       "updated_at": "2026-11-01T12:00:00Z"}}
images = {{**ci, "id": 20, "status": "completed", "conclusion": "success"}}
def job(number, name, state, result):
    return {{"id": number, "name": name, "status": state, "conclusion": result,
            "labels": ["example-app-home"]}}
if "/workflows/ci.yml/runs?" in path:
    reply = {{"workflow_runs": [ci]}}
elif "/workflows/images.yml/runs?" in path:
    reply = {{"workflow_runs": [images]}}
elif "/actions/runs/10/jobs?" in path:
    reply = {{"jobs": [] if mode == "missing" else [
        job(11, "CI result", status, conclusion),
        job(12, "CI: web", status, conclusion)]}}
elif "/actions/runs/20/jobs?" in path:
    reply = {{"jobs": [job(21, "Docker images: what to run", "completed", "success"),
                        job(22, "Docker images: image tests", "completed", "success")]}}
elif path.endswith("/annotations?per_page=100"):
    reply = []
else:
    sys.exit("unexpected example API request")
print(json.dumps(reply))
'''
    gh = binary / "gh"
    gh.write_text(source)
    gh.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    repo.env.update({"PATH": str(binary) + ":" + repo.env["PATH"], "HOME": str(home),
                     "XDG_CONFIG_HOME": str(home / ".config"), "SHIPLINE_REPO": str(repo.main),
                     "BREAK_TEST_ROOT": str(tmp_path), "BREAK_TEST_HEAD": repo.base})
    return repo


def run_status(repo, mode):
    (repo.root / "check-mode").write_text(mode)
    return repo.run("bash", str(ROOT / "scripts/workflow/main-status"), "--tick-main-status")


def reruns(repo):
    return [args for line in (repo.root / "gh-calls.jsonl").read_text().splitlines()
            if (args := json.loads(line))[:2] == ["run", "rerun"]]


def test_lost_check_stays_pending_after_recovery_until_late_results_arrive(main_status):
    repo = main_status
    missing = run_status(repo, "missing")
    assert missing.returncode == 0, missing.stderr
    assert missing.stdout.startswith("main: pending"), missing.stdout
    assert len(repo.notes()) == 1 and not reruns(repo)
    repo.next_tick()
    late = run_status(repo, "green")
    assert late.returncode == 0, late.stderr
    assert late.stdout.startswith("main: green"), late.stdout
    assert len(repo.notes()) == 1 and not reruns(repo)


def test_partial_ci_rerun_never_duplicates_the_request_after_recovery(main_status):
    repo = main_status
    first = run_status(repo, "failed")
    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith("main: red"), first.stdout
    assert len(reruns(repo)) == 1
    repo.next_tick()
    delayed = run_status(repo, "failed")
    assert delayed.returncode == 0, delayed.stderr
    assert len(reruns(repo)) == 1
    repo.next_tick(3)
    partial = run_status(repo, "partial")
    assert partial.returncode == 0, partial.stderr
    assert partial.stdout.startswith("main: pending"), partial.stdout
    assert len(reruns(repo)) == 1
    repo.next_tick(4)
    completed = run_status(repo, "green")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("main: green"), completed.stdout
    assert len(reruns(repo)) == 1
    assert len(repo.notes()) == 1
