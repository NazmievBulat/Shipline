"""Planning reuses observations; a merge still reads its own current evidence."""

import importlib.util
import json
import subprocess
import sys
import time
from copy import deepcopy
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub, _hand_off, _handing_off, _waiting
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow

github = github_fixture
handed = handed_fixture
_API = "repos/example-org/example-app"


def _load(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[2] / f"scripts/workflow/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def reads_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # What train_reads reads this checkout's repository and workflow files with.
    monkeypatch.setitem(sys.modules, "workflow_config", _load("workflow_config"))
    module = _load("train_reads")
    monkeypatch.setenv("WORKFLOW_TRAIN_TICK", str(tmp_path))
    (tmp_path / "context.json").write_text(json.dumps({"started": time.time()}))
    return module


def _snapshot(folder: Path, *, status: str = "COMPLETED", conclusion: str = "SUCCESS") -> None:
    (folder / "pulls.json").write_text(
        json.dumps(
            {
                "7": {
                    "headRefOid": "abc",
                    "statusCheckRollup": [
                        {"status": status, "conclusion": conclusion, "detailsUrl": "run/1"}
                    ],
                }
            }
        )
    )


@pytest.mark.parametrize("change", ["none", "head", "main", "checks", "pending", "status_context"])
def test_only_unchanged_terminal_members_reuse_reads_between_ticks(
    reads_module: ModuleType,
    tmp_path: Path,
    change: str,
) -> None:
    _snapshot(tmp_path, status="IN_PROGRESS" if change == "pending" else "COMPLETED")
    if change == "status_context":
        (tmp_path / "pulls.json").write_text(
            json.dumps(
                {
                    "7": {
                        "headRefOid": "abc",
                        "statusCheckRollup": [{"__typename": "StatusContext", "state": "PENDING"}],
                    }
                }
            )
        )
    path = f"{_API}/actions/workflows/ci.yml/runs?head_sha=abc&event=pull_request&per_page=100"
    calls: list[str] = []

    def fetch() -> list[dict[str, Any]]:
        calls.append(path)
        return [{"id": 1, "status": "completed", "conclusion": "success"}]

    first = reads_module.Reads(tmp_path, "main")
    assert first.api(path, fetch)[0]["conclusion"] == "success"
    assert first.api(path, fetch)[0]["conclusion"] == "success"
    first.finish()
    assert len(calls) == 1
    (tmp_path / "api.json").unlink()  # another tick has a new temporary directory
    snapshot = json.loads((tmp_path / "pulls.json").read_text())
    if change == "head":
        snapshot["7"]["headRefOid"] = "def"
        path = path.replace("abc", "def")
    elif change == "checks":
        snapshot["7"]["statusCheckRollup"][0]["detailsUrl"] = "run/2"
    (tmp_path / "pulls.json").write_text(json.dumps(snapshot))
    second = reads_module.Reads(tmp_path, "other" if change == "main" else "main")
    second.api(path, fetch)
    assert len(calls) == (1 if change == "none" else 2)


def test_scratch_runs_are_polled_each_tick(reads_module: ModuleType, tmp_path: Path) -> None:
    _snapshot(tmp_path)
    path = f"{_API}/actions/workflows/ci.yml/runs?head_sha=scratch&event=workflow_dispatch"
    first = reads_module.Reads(tmp_path, "main")
    assert first.api(path, lambda: [{"status": "in_progress"}])[0]["status"] == "in_progress"
    first.finish()
    (tmp_path / "api.json").unlink()
    second = reads_module.Reads(tmp_path, "main")
    assert second.api(path, lambda: [{"status": "completed"}])[0]["status"] == "completed"


def test_fallback_and_fresh_gate_views_preserve_the_ticket_selection_snapshot(
    reads_module: ModuleType,
    tmp_path: Path,
) -> None:
    bulk = {"8": {"number": 8, "body": "Start: owner only", "headRefOid": "draft"}}
    path = tmp_path / "pulls.json"
    path.write_text(json.dumps(bulk))
    original = path.read_bytes()
    # Gate views have neither the number nor the description needed by ticket starts.
    fresh = {"headRefOid": "new-head", "state": "OPEN", "isDraft": False}

    def read_fresh(_number: str) -> dict[str, Any]:
        return fresh

    assert reads_module.pull("7", read_fresh) == fresh
    assert path.read_bytes() == original
    fresh = {**fresh, "headRefOid": "refreshed-head"}
    assert reads_module.refresh_pull("7", read_fresh) == fresh
    assert reads_module.planning_pulls(tmp_path)["7"] == fresh
    assert path.read_bytes() == original


def test_session_inspection_is_shared_and_stops_remove_their_rows(workflow: Workflow) -> None:
    workflow.running("review-7")
    path = workflow.root / "sessions"
    env = {"WORKFLOW_SESSION_SNAPSHOT": str(path)}
    first = workflow.run("list", env=env)
    assert first.returncode == 0, first.stderr
    before = workflow.calls("tmux")
    again = workflow.run("list", env=env)
    assert again.stdout == first.stdout
    assert workflow.calls("tmux") == before
    assert workflow.run("stop", "review-7", env=env).returncode == 0
    assert workflow.run("list", env=env).stdout == ""


def _context(handed: GitHub) -> dict[str, str]:
    path = handed.workflow.root / "tick-reads"
    path.mkdir()
    (path / "context.json").write_text(json.dumps({"started": time.time()}))
    (path / "pulls.json").write_text(json.dumps(handed.pulls))
    (path / "sessions").write_text("")
    return {"WORKFLOW_TRAIN_TICK": str(path), "WORKFLOW_SESSION_SNAPSHOT": str(path / "sessions")}


def test_status_reuses_the_reconcile_and_a_session_outside_the_tick_reads_again(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    env = _context(handed)
    first = workflow.run_script("merge-when-current", "--train-status", env=env)
    assert first.returncode == 0, first.stderr
    before = workflow.calls("gh")
    again = workflow.run_script("merge-when-current", "--train-status", env=env)
    assert again.returncode == 0, again.stderr
    assert again.stdout == first.stdout
    assert workflow.calls("gh") == before
    handed.pulls["7"]["isDraft"] = True
    handed.save()
    outside = workflow.run_script("merge-when-current", "--train-status")
    assert outside.returncode == 0, outside.stderr
    assert json.loads(outside.stdout)["waiting"] == []
    assert len(workflow.calls("gh pr view")) > len(
        [c for c in before if c.startswith("gh pr view")]
    )


def test_a_green_planning_cache_cannot_hide_checks_that_fail_before_the_gate(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    env = _context(handed)
    first = workflow.run_script("merge-when-current", "--train-status", env=env)
    assert first.returncode == 0, first.stderr
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    handed.save()
    done = workflow.run_script(
        "merge-when-current", "--drive", "7", env=env, cwd=workflow.worktree(7)
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "failed checks" in done.stderr
    assert not workflow.calls("gh pr merge")


def test_a_green_planning_cache_cannot_hide_failed_ci_jobs_before_the_gate(handed: GitHub) -> None:
    workflow = handed.workflow
    env = _context(handed)
    first = workflow.run_script("merge-when-current", "--train-status", env=env)
    assert first.returncode == 0, first.stderr
    jobs = f"{_API}/actions/runs/20/jobs?per_page=100"
    before = len([c for c in workflow.calls("gh api") if jobs in c])
    handed.api[jobs]["jobs"][1]["conclusion"] = "failure"
    handed.save()  # the pull rollup deliberately stays green and identical
    done = workflow.run_script(
        "merge-when-current", "--drive", "7", env=env, cwd=workflow.worktree(7)
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "CI run for this pull request failed" in done.stderr
    assert len([c for c in workflow.calls("gh api") if jobs in c]) > before
    assert not workflow.calls("gh pr merge")


def test_run_jobs_and_annotations_share_the_member_cache_across_ticks(
    reads_module: ModuleType,
    tmp_path: Path,
) -> None:
    _snapshot(tmp_path)
    # Loaded from this checkout, the cache knows its jobs by this checkout's repository.
    api = reads_module.workflow_config.current().api
    paths = [
        f"{api}/actions/workflows/ci.yml/runs?head_sha=abc&event=pull_request&per_page=100",
        f"{api}/actions/runs/20/jobs?per_page=100",
        f"{api}/check-runs/21/annotations?per_page=100",
    ]
    replies = [
        [{"id": 20, "status": "completed", "conclusion": "success"}],
        [{"id": 21, "status": "completed", "conclusion": "success"}],
        [{"title": "Merge tested", "message": "merge"}],
    ]
    calls: list[str] = []

    def read(path: str, reply: Any) -> Any:
        calls.append(path)
        return reply

    for _ in range(2):
        cache = reads_module.Reads(tmp_path, "main")
        for path, reply in zip(paths, replies, strict=True):
            assert cache.api(path, partial(read, path, reply)) == reply
        cache.finish()
        (tmp_path / "api.json").unlink()
    assert calls == paths
    changed = reads_module.Reads(tmp_path, "new-main")
    for path, reply in zip(paths, replies, strict=True):
        changed.api(path, partial(read, path, reply))
    assert calls == paths * 2


def test_an_earlier_attempts_jobs_and_notes_share_the_member_cache_across_ticks(
    reads_module: ModuleType,
    tmp_path: Path,
) -> None:
    _snapshot(tmp_path)
    api = reads_module.workflow_config.current().api
    # A re-run of the failed jobs alone carried "CI: what to run" into attempt 2 without notes.
    carried = f"{api}/check-runs/521/annotations?per_page=100"
    paths = [
        f"{api}/actions/workflows/ci.yml/runs?head_sha=abc&event=pull_request&per_page=100",
        f"{api}/actions/runs/20/jobs?per_page=100",
        carried,
        f"{api}/actions/runs/20/attempts/1/jobs?per_page=100",
        f"{api}/check-runs/21/annotations?per_page=100",
    ]
    replies = [
        [{"id": 20, "run_attempt": 2, "status": "completed", "conclusion": "success"}],
        [{"id": 521, "status": "completed", "conclusion": "success"}],
        [],
        [{"id": 21, "status": "completed", "conclusion": "success"}],
        [{"title": "Merge tested", "message": "merge"}],
    ]
    calls: list[str] = []

    def read(path: str, reply: Any) -> Any:
        calls.append(path)
        return reply

    for _ in range(2):
        cache = reads_module.Reads(tmp_path, "main")
        for path, reply in zip(paths, replies, strict=True):
            assert cache.api(path, partial(read, path, reply)) == reply
        cache.finish()
        (tmp_path / "api.json").unlink()
    # Only the carried job's empty notes are read again: they might not have been written yet.
    assert calls == [*paths, carried]


def test_a_gate_refresh_after_the_snapshot_keeps_its_advanced_handoff(handed: GitHub) -> None:
    workflow = handed.workflow
    env = _context(handed)
    old = handed.base
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    handed.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    handed.main()
    handed.ci(handed.base, handed.head, tested_base=old)
    handed.save()
    refresh = workflow.run_script("merge-when-current", "--drive", "7", cwd=workflow.worktree(7))
    assert refresh.returncode == 2, refresh.stdout + refresh.stderr
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    folder = workflow.repo / ".local/workflow"
    assert json.loads((folder / "merge-wanted-7").read_text())["head"] == head
    handed.pulls["7"]["headRefOid"] = head
    handed.pulls["7"]["statusCheckRollup"][0].update(status="QUEUED", conclusion=None)
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    handed.ci(handed.base, head, run_id=50)
    handed.api[
        f"{_API}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
    ]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    handed.save()

    done = subprocess.run(
        [sys.executable, "-B", str(workflow.script.parent / "merge_driver.py")],
        env={**workflow.env, **env},
        cwd=workflow.repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads((folder / "merge-wanted-7").read_text())["head"] == head
    assert not (folder / "merge-review-7").exists()
    assert not workflow.calls("gh pr merge")
    context = Path(env["WORKFLOW_TRAIN_TICK"])
    assert json.loads((context / "planning-pulls.json").read_text())["7"]["headRefOid"] == head
    assert json.loads((context / "pulls.json").read_text())["7"]["headRefOid"] != head


def test_a_failure_the_snapshot_shows_is_read_again_before_the_handoff_is_revoked(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    env = _context(handed)
    context = Path(env["WORKFLOW_TRAIN_TICK"])
    # The bulk snapshot saw a failed run; a re-run on the same head went green before the driver.
    stale = deepcopy(handed.pulls)
    stale["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    (context / "pulls.json").write_text(json.dumps(stale))
    folder = workflow.repo / ".local/workflow"

    done = subprocess.run(
        [sys.executable, "-B", str(workflow.script.parent / "merge_driver.py")],
        env={**workflow.env, **env},
        cwd=workflow.repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "checks failed" not in done.stdout + done.stderr
    assert not (folder / "merge-review-7").exists()


def test_a_large_pending_train_reads_each_member_once_in_a_no_merge_tick(handed: GitHub) -> None:
    workflow = handed.workflow
    folder = workflow.repo / ".local/workflow"
    original = deepcopy(handed.pulls["7"])
    (workflow.state / "tmux/review-7").touch()  # reserve its already allocated process ID
    for number in range(7, 37):
        key = str(number)
        head = handed.head if number == 7 else f"{number:040x}"
        branch = original["headRefName"] if number == 7 else f"ticket/{number}"
        handed.pulls[key] = {**deepcopy(original), "headRefOid": head, "headRefName": branch}
        handed.pulls[key]["statusCheckRollup"][0].update(status="QUEUED", conclusion=None)
        run = number * 1000
        handed.api[
            f"{_API}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
        ] = {
            "workflow_runs": [
                {
                    "id": run,
                    "head_sha": head,
                    "event": "pull_request",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
        }
        handed.api[f"{_API}/actions/runs/{run}/jobs?per_page=100"] = {
            "jobs": [
                {"id": run + 1, "name": "CI result", "status": "in_progress", "conclusion": None}
            ],
        }
        if number < 27:
            (folder / f"merge-wanted-{key}").write_text(
                json.dumps({"head": head, "round": 1, "branch": branch})
            )
        else:
            workflow.running(f"review-{key}")
    (workflow.state / "tmux/review-7").unlink()
    train = json.loads((folder / "merge-train.json").read_text())
    train["waiting"] = list(handed.pulls)
    (folder / "merge-train.json").write_text(json.dumps(train))
    handed.save()
    (workflow.state / "calls.log").write_text("")
    started = time.monotonic()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not workflow.calls("gh pr merge")
    assert len(workflow.calls("gh pr view")) == 1  # the acting gate's fresh read
    for number in range(7, 37):
        head = handed.pulls[str(number)]["headRefOid"]
        run_calls = [c for c in workflow.calls("gh api") if f"ci.yml/runs?head_sha={head}" in c]
        assert len(run_calls) == (2 if number == 7 else 1)
        job_calls = [c for c in workflow.calls("gh api") if f"/runs/{number * 1000}/jobs?" in c]
        assert len(job_calls) == (2 if number == 7 else 1)
    assert time.monotonic() - started < 180
    assert json.loads((folder / "merge-train.json").read_text())["waiting"] == list(handed.pulls)


def test_the_tick_reads_the_members_a_handoff_left_unread(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    # 8 waits ahead of it, though its pull request went back to draft.
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefOid": f"{8:040x}",
        "headRefName": "ticket/eight",
        "isDraft": True,
    }
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["8"]}))

    handed_off = _hand_off(github)

    assert handed_off.returncode == 0, handed_off.stdout + handed_off.stderr
    assert _waiting(workflow) == ["8", "7"]
    assert not workflow.calls("gh pr view 8")
    # At the turn of the line the tick reads 8, lets it go, and merges 7.
    workflow.list_pull_request(7, draft=False)
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {github.head}\n")
    workflow.run("stop", "review-7")
    github.batch_mode = True
    github.driver_mode = True
    github.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert _waiting(workflow) == []
