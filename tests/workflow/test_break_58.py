"""Breaking tests for pull request 58: registry and BuildKit transport errors as runner errors.

They replay a day of a repository whose image check runs six image builds at once on its
Docker runners, pulling base-image layers through a pull-through cache, whose image jobs wait
up to an hour and a half for a Docker runner, and whose merge line keeps several prefixes:

- a build that breaks off while pulling a base layer, as `gh run view --log-failed` prints it;
- the other builds still waiting for a runner when the first one failed;
- GitHub refusing a re-run while the run still runs;
- a re-run that waits long for a runner, the run list lagging behind its new attempt;
- the re-run failing the same way again, or a test failing beside the transport error;
- two calls at once, a re-run request that times out.
"""

import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest
from workflow.test_merge_driver import (
    _fail,
    _github_answers_repairs,
    _note,
    _started,
)
from workflow.test_merge_driver import (
    _train as _driver_train,
)
from workflow.test_merge_driver import (
    handed as handed_fixture,
)
from workflow.test_merge_when_current import (
    _REPO,
    GitHub,
    _batch_call,
    _batch_setup,
    _batch_state,
    _finish_batch,
    _scratch,
    rerun_calls,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture
handed = handed_fixture

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/workflow"
_DOCKER = "example-app-home-docker"
_IMAGES = ["api", "worker", "web", "scheduler", "migrations", "docs"]
# What `gh run view --job J --log-failed` prints: job, step, time, then BuildKit's own lines.
_PREFIX = "Docker images: build (api)\tBuild image\t2026-10-24T10:{:02d}:01.1234567Z "
_SHORT_READ = [
    "#1 [internal] load build definition from Dockerfile",
    "#1 DONE 0.0s",
    "#2 [internal] load metadata for docker.io/library/python:3.12-slim",
    "#2 DONE 0.4s",
    "#13 [base 1/6] FROM docker.io/library/python:3.12-slim@sha256:" + "a" * 64,
    "#13 resolve docker.io/library/python:3.12-slim@sha256:" + "a" * 64 + " 0.0s done",
    "#13 sha256:" + "b" * 64 + " 0B / 28.25MB 0.2s",
    "\x1b[31m#13 ERROR: short read: expected 28249323 bytes but got 0: unexpected EOF\x1b[0m",
    "------",
    " > [base 1/6] FROM docker.io/library/python:3.12-slim@sha256:" + "a" * 64 + ":",
    "------",
    ("failed to solve: failed to compute cache key: short read: expected 28249323 bytes but got"
    " 0: unexpected EOF"),
    "##[error]Process completed with exit code 1.",
]
SHORT_READ_LOG = "".join(_PREFIX.format(n) + line + "\n" for n, line in enumerate(_SHORT_READ))
FAILED_TEST = (
    "Docker images: image tests\tRun image tests\t2026-10-24T10:30:00.0000000Z FAILED "
    "tests/test_images.py::test_api_image_serves_health - AssertionError: assert 502 == 200\n"
)


# ---------------------------------------------------------------------------------------------
# The classification and the one retry, with GitHub answered by a stand-in module.


class _Waiting(Exception):
    pass


class _Refused(Exception):
    pass


class _TimedOut(_Refused):
    pass


def _fake_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jobs: list[dict[str, Any]],
    logs: dict[int, str],
    notes: dict[int, list[dict[str, Any]]] | None = None,
    rerun: Any = None,
) -> types.SimpleNamespace:
    calls: list[list[str]] = []
    woken: list[str] = []

    def api(path: str, key: str | None = None) -> Any:
        if "/annotations" in path:
            job = int(path.split("/check-runs/")[1].split("/")[0])
            return (notes or {}).get(job, [])
        return [dict(job) for job in jobs]

    def read_github(*args: str) -> Any:
        job = int(args[args.index("--job") + 1])
        if job in logs:
            return types.SimpleNamespace(returncode=0, stdout=logs[job], stderr="")
        return types.SimpleNamespace(
            returncode=1, stdout="", stderr="failed to get run log: log not found"
        )

    def command(*args: str, check: bool = True) -> Any:
        calls.append(list(args))
        if rerun is not None:
            return rerun()
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    gate = types.SimpleNamespace(
        API="repos/example-org/example-app",
        REPOSITORY="example-org/example-app",
        CI_PLAN="CI: what to run",
        E2E_PLAN="E2E: what to run",
        IMAGES_PLAN="Docker images: what to run",
        Waiting=_Waiting,
        Refused=_Refused,
        BudgetWaiting=type("BudgetWaiting", (_Waiting,), {}),
        api=api,
        read_github=read_github,
        transient=lambda error: "HTTP 5" in error or "timeout" in error.lower(),
        command=command,
        wake_log=woken.append,
        _failed_jobs=lambda parts: [
            job for job in parts if job.get("conclusion") not in (None, "success", "skipped")
        ],
        _rerunnable=lambda run, parts, first: run.get("status") == "completed"
        and run.get("run_attempt") == 1
        and all(job.get("status") == "completed" for job in parts),
        calls=calls,
        woken=woken,
    )
    monkeypatch.setitem(sys.modules, "merge_gate", gate)
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    return gate


@pytest.fixture
def repair(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    import check_repair

    return check_repair


def _run(status: str = "completed", attempt: int = 1) -> dict[str, Any]:
    return {
        "id": 700,
        "head_sha": "c" * 40,
        "status": status,
        "conclusion": "failure" if status == "completed" else None,
        "run_attempt": attempt,
    }


def _six_builds(failed: str = "failure", others: str = "success") -> list[dict[str, Any]]:
    jobs = [
        {"id": 701, "name": "Docker images: what to run", "status": "completed",
         "conclusion": "success", "labels": ["example-app-home-planner"]},
    ]
    for index, image in enumerate(_IMAGES):
        conclusion = failed if index == 0 else others
        jobs.append({
            "id": 710 + index, "name": f"Docker images: build ({image})",
            "status": "completed" if conclusion else "queued", "conclusion": conclusion,
            "labels": [_DOCKER],
        })
    jobs.append({"id": 720, "name": "Docker images: image tests", "status": "completed",
                 "conclusion": "skipped", "labels": [_DOCKER]})
    return jobs


def test_the_installation_s_short_read_as_gh_prints_it_is_a_runner_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    _fake_gate(monkeypatch, tmp_path, _six_builds(), {710: SHORT_READ_LOG})
    diagnosis = repair.diagnose_runs("c" * 40, [(_run(), _six_builds())])
    assert diagnosis.kind == "infrastructure", diagnosis


def test_fail_fast_cancelled_builds_beside_the_short_read_are_rerun_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    # With fail-fast, GitHub cancels the five builds still waiting for a Docker runner.
    jobs = _six_builds(others="cancelled")
    _fake_gate(monkeypatch, tmp_path, jobs, {710: SHORT_READ_LOG},
               notes={job["id"]: [{"title": "", "message": "The operation was canceled."}]
                      for job in jobs[2:7]})
    diagnosis = repair.diagnose_runs("c" * 40, [(_run(), jobs)])
    assert diagnosis.kind == "infrastructure", diagnosis


def test_the_buildx_annotation_alone_is_a_runner_error_when_the_log_is_not_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    note = {"title": "", "message": "buildx failed with: ERROR: failed to solve: failed to "
            "compute cache key: short read: expected 28249323 bytes but got 0: unexpected EOF"}
    _fake_gate(monkeypatch, tmp_path, _six_builds(), {}, notes={710: [note]})
    diagnosis = repair.diagnose_runs("c" * 40, [(_run(), _six_builds())])
    assert diagnosis.kind == "infrastructure", diagnosis


def test_a_run_whose_builds_still_wait_for_a_runner_is_not_judged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    jobs = _six_builds(others=None)  # five builds queued for a Docker runner
    gate = _fake_gate(monkeypatch, tmp_path, jobs, {710: SHORT_READ_LOG})
    assert repair.runner_wait(tmp_path, _run(status="in_progress"), jobs)
    # GitHub's run list already says completed while its jobs endpoint still shows them queued.
    assert repair.runner_wait(tmp_path, _run(), jobs)
    assert gate.calls == []
    assert not repair.marker(tmp_path, 700).exists()


@pytest.mark.parametrize("error", [
    "run 700 cannot be rerun; This workflow is already running",
    ("failed to rerun: HTTP 403: This workflow is already running "
    "(https://api.github.com/repos/example-org/example-app/actions/runs/700/rerun-failed-jobs)"),
])
def test_github_s_still_running_refusal_keeps_the_one_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType, error: str
) -> None:
    answers = [types.SimpleNamespace(returncode=1, stdout="", stderr=error)]
    gate = _fake_gate(
        monkeypatch, tmp_path, _six_builds(), {710: SHORT_READ_LOG},
        rerun=lambda: answers.pop(0) if answers else types.SimpleNamespace(
            returncode=0, stdout="", stderr=""),
    )
    first = repair.runner_wait(tmp_path, _run(), _six_builds())
    assert first and "still running" in first
    assert not repair.marker(tmp_path, 700).exists()
    second = repair.runner_wait(tmp_path, _run(), _six_builds())
    assert second and "automatically re-ran" in second
    assert len(gate.calls) == 2
    assert repair._state(repair.marker(tmp_path, 700)) == "requested"


def test_two_calls_at_once_send_one_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    # The reviewer's gate call and the tick judge the same failed run at the same moment.
    started = threading.Barrier(2)

    def slow() -> Any:
        time.sleep(0.3)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    gate = _fake_gate(monkeypatch, tmp_path, _six_builds(), {710: SHORT_READ_LOG}, rerun=slow)
    results: list[str | None] = []

    def judge() -> None:
        started.wait()
        results.append(repair.runner_wait(tmp_path, _run(), _six_builds()))

    threads = [threading.Thread(target=judge) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(gate.calls) == 1, gate.calls
    assert all(results), results
    assert len(gate.woken) == 1


def test_the_same_short_read_on_the_re_run_is_reported_once_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    gate = _fake_gate(monkeypatch, tmp_path, _six_builds(), {710: SHORT_READ_LOG})
    assert repair.runner_wait(tmp_path, _run(), _six_builds())
    # The run list lags behind the accepted re-run: still attempt 1, a failure.
    assert repair.runner_wait(tmp_path, _run(), _six_builds())
    for _ in range(3):
        # Attempt 2 failed the same build the same way; each later call judges it again.
        assert repair.runner_wait(tmp_path, _run(attempt=2), _six_builds()) is None
        assert repair.repeated(tmp_path, 700)
    assert len(gate.calls) == 1
    assert gate.woken[1:] == ["its runners failed it again in run 700"]


def test_a_failing_test_beside_the_short_read_is_the_pull_request_s(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    jobs = _six_builds()
    jobs[-1]["conclusion"] = "failure"
    gate = _fake_gate(monkeypatch, tmp_path, jobs, {710: SHORT_READ_LOG, 720: FAILED_TEST})
    assert repair.runner_wait(tmp_path, _run(), jobs) is None
    diagnosis = repair.diagnose_runs("c" * 40, [(_run(), jobs)])
    assert diagnosis.kind == "own"
    assert diagnosis.repair.test == "tests/test_images.py::test_api_image_serves_health"
    assert gate.calls == []


def test_a_rerun_request_that_times_out_is_never_sent_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repair: types.ModuleType
) -> None:
    def stall() -> Any:
        raise _TimedOut("gh run rerun 700 timed out")

    gate = _fake_gate(monkeypatch, tmp_path, _six_builds(), {710: SHORT_READ_LOG}, rerun=stall)
    with pytest.raises(_TimedOut):
        repair.runner_wait(tmp_path, _run(), _six_builds())
    gate.command = lambda *args, check=True: pytest.fail("a second rerun request was sent")
    # The ambiguous request keeps its claim: the prefix waits, as main's own retry does.
    assert "requested" in repair.runner_wait(tmp_path, _run(), _six_builds())


@pytest.mark.parametrize("error", [
    'failed to copy: httpReadSeeker: failed open: failed to do request: Get "http://'
    'example-hub-cache:5000/v2/library/python/blobs/sha256:' + "b" * 64 + '": dial tcp '
    "192.0.2.10:5000: i/o timeout",
    "failed to copy: read tcp 192.0.2.20:41234->192.0.2.10:5000: read: connection reset by peer",
    ('failed to do request: Head "https://example-hub-cache:5000/v2/library/python/manifests/'
    '3.12-slim?ns=docker.io": net/http: TLS handshake timeout'),
])
def test_a_base_layer_pull_through_the_cache_that_breaks_off_is_a_runner_error(
    repair: types.ModuleType, error: str
) -> None:
    lines = [*_SHORT_READ[:7], f"#13 ERROR: {error}", "------",
             f"ERROR: failed to solve: {error}"]
    log = "".join(_PREFIX.format(n) + line + "\n" for n, line in enumerate(lines))
    assert repair.transport_error(log)
    # Even when the solve summary is missing, the FROM vertex gives the error its context.
    assert repair.transport_error("\n".join(log.splitlines()[:8]))


@pytest.mark.parametrize("line", [
    # A server under test whose own work handles images (an upload resizer).
    ("api-1  | ERROR resolving image upload: read tcp 192.0.2.30:8000->192.0.2.31:41000: "
    "connection reset by peer"),
    "api-1  | WARN exporting image thumbnails: unexpected EOF",
])
def test_a_server_under_test_that_handles_images_is_not_a_runner_error(
    repair: types.ModuleType, line: str
) -> None:
    log = "Docker images: image tests\tSmoke test\t2026-10-24T10:30:00.0000000Z " + line + "\n"
    assert not repair.transport_error(log)


def test_a_pull_through_the_cache_host_that_times_out_is_a_runner_error(
    repair: types.ModuleType,
) -> None:
    # `docker pull` through the cache by its host name: the daemon names neither "registry"
    # nor a layer, only the registry API's `/v2/` path.
    log = ('Docker images: build (api)\tPull base\t2026-10-24T10:00:00.0000000Z Error response '
           'from daemon: Get "http://example-hub-cache:5000/v2/": net/http: TLS handshake timeout\n')
    assert repair.transport_error(log)


# ---------------------------------------------------------------------------------------------
# The same day through the real gate: the front member's prefix and its image run.


def _image_prefix(github: GitHub) -> tuple[dict[str, Any], str, str]:
    """Member 7's prefix whose image run builds six images on Docker runners; the api build
    failed pulling its base layer while the five others still wait for a runner."""
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    scratch = _scratch(github)
    assert scratch is not None
    path = (f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
            "&event=workflow_dispatch&per_page=100")
    run = github.api[path]["workflow_runs"][0]
    run.update(status="in_progress", conclusion=None, run_attempt=1,
               run_started_at="2026-10-24T09:00:00Z")
    key = f"{_REPO}/actions/runs/{run['id']}/jobs?per_page=100"
    jobs = github.api[key]["jobs"]
    for job in jobs:
        job.update(labels=["example-app-home-planner"] if job["name"].endswith("what to run")
                   else [_DOCKER])
    tests = next(job for job in jobs if job["name"] == "Docker images: image tests")
    tests.update(status="queued", conclusion=None)
    for index, image in enumerate(_IMAGES):
        jobs.insert(-1, {
            "id": run["id"] + 50 + index, "name": f"Docker images: build ({image})",
            "status": "completed" if index == 0 else "queued",
            "conclusion": "failure" if index == 0 else None, "labels": [_DOCKER],
            "created_at": "2026-10-24T09:00:00Z",
        })
        github.api[f"{_REPO}/check-runs/{run['id'] + 50 + index}/annotations?per_page=100"] = []
    logs = github.workflow.state / "job-logs"
    logs.mkdir(exist_ok=True)
    (logs / str(run["id"] + 50)).write_text(SHORT_READ_LOG)
    github.save()
    return scratch, path, key


def _jobs(github: GitHub, key: str) -> list[dict[str, Any]]:
    return github.api[key]["jobs"]


def _kept(github: GitHub, scratch: dict[str, Any]) -> None:
    state = _batch_state(github)
    assert _scratch(github)["branch"] == scratch["branch"]
    assert state["waiting"] == ["7", "8", "9"]
    assert "7" not in state["blocked"]


def test_a_prefix_image_build_that_broke_off_is_rerun_once_and_keeps_its_place_while_it_waits(
    github: GitHub,
) -> None:
    scratch, path, key = _image_prefix(github)
    workflow = github.workflow

    # The api build failed; the other five still wait for a Docker runner.
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert rerun_calls(workflow) == []
    _kept(github, scratch)

    # They finished; GitHub's run list says completed, but GitHub still refuses the re-run.
    run = github.api[path]["workflow_runs"][0]
    for job in _jobs(github, key):
        if job["status"] != "completed":
            job.update(status="completed", conclusion="success" if "build" in job["name"]
                       else "skipped")
    run.update(status="completed", conclusion="failure")
    (workflow.state / "fail-rerun").write_text(
        f"run {run['id']} cannot be rerun; This workflow is already running")
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert len(rerun_calls(workflow)) == 1
    _kept(github, scratch)
    (workflow.state / "fail-rerun").unlink()

    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert len(rerun_calls(workflow)) == 2
    _kept(github, scratch)

    # GitHub accepted it. Its list still shows attempt 1 for a while, then attempt 2 whose api
    # build and image tests wait 87 minutes for a Docker runner; the five others are carried.
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    run = github.api[path]["workflow_runs"][0]
    run.update(status="queued", conclusion=None, run_attempt=2)
    long_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 87 * 60))
    for job in _jobs(github, key):
        if job["name"] in ("Docker images: build (api)", "Docker images: image tests"):
            job.update(status="queued", conclusion=None, run_attempt=2, created_at=long_ago)
    github.save()
    for _ in range(3):
        done = _batch_call(github)
        assert done.returncode == 3, done.stdout + done.stderr
        _kept(github, scratch)
    assert len(rerun_calls(workflow)) == 2
    assert workflow.calls("gh run cancel") == []

    # The re-run passed: the member merges by its prefix, the line never restarted.
    run = github.api[path]["workflow_runs"][0]
    run.update(status="completed", conclusion="success")
    for job in _jobs(github, key):
        job.update(status="completed", conclusion="success")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(rerun_calls(workflow)) == 2


def test_a_prefix_image_build_that_breaks_off_again_is_handed_back_and_said_once(
    github: GitHub,
) -> None:
    _, path, key = _image_prefix(github)
    workflow = github.workflow
    run = github.api[path]["workflow_runs"][0]
    for job in _jobs(github, key):
        if job["status"] != "completed":
            job.update(status="completed", conclusion="success" if "build" in job["name"]
                       else "skipped")
    run.update(status="completed", conclusion="failure")
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert len(rerun_calls(workflow)) == 1
    run = github.api[path]["workflow_runs"][0]
    run.update(run_attempt=2)
    github.save()
    done = _batch_call(github)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "failed again after its runner retry" in done.stderr
    assert len(rerun_calls(workflow)) == 1
    assert "7" in _batch_state(github)["blocked"]
    said = workflow.wake_log.read_text().count(f"its runners failed it again in run {run['id']}")
    assert said == 1


def test_a_prefix_image_test_that_failed_beside_a_short_read_is_its_own_failure(
    github: GitHub,
) -> None:
    _, path, key = _image_prefix(github)
    workflow = github.workflow
    run = github.api[path]["workflow_runs"][0]
    tests = next(job for job in _jobs(github, key) if job["name"] == "Docker images: image tests")
    for job in _jobs(github, key):
        if job["status"] != "completed":
            job.update(status="completed", conclusion="success")
    tests["conclusion"] = "failure"
    (workflow.state / "job-logs" / str(tests["id"])).write_text(
        SHORT_READ_LOG.replace("Docker images: build (api)", "Docker images: image tests")
        + FAILED_TEST)
    github.api[f"{_REPO}/check-runs/{tests['id']}/annotations?per_page=100"] = []
    run.update(status="completed", conclusion="failure")
    done = _batch_call(github)
    assert done.returncode == 1, done.stdout + done.stderr
    assert rerun_calls(workflow) == []
    assert "7" in _batch_state(github)["blocked"]


# ---------------------------------------------------------------------------------------------
# A handed-off pull request whose own image run broke off the same way.


def test_a_handoff_whose_build_broke_off_keeps_its_handoff_through_a_long_rerun(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=SHORT_READ_LOG, labels=(_DOCKER,))
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == ["gh run rerun 20 -R example-org/example-app --failed"]
    # The re-run's job waits for a Docker runner for many ticks.
    runs = ("repos/example-org/example-app/actions/workflows/ci.yml/runs"
            f"?head_sha={handed.head}&event=pull_request&per_page=100")
    run = handed.api[runs]["workflow_runs"][0]
    run.update(run_attempt=2, status="queued", conclusion=None)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    jobs[-1].update(status="queued", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="QUEUED", conclusion=None)
    handed.save()
    for _ in range(3):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _note(workflow).exists()
        assert _started(workflow) == []
    assert len(workflow.calls("gh run rerun")) == 1
    assert _driver_train(workflow)["waiting"] == ["7"]
    # The re-run passed at last: the handoff merges with no reviewer started.
    run.update(status="completed", conclusion="success")
    for job in jobs:
        job.update(status="completed", conclusion="success")
    handed.pulls["7"]["statusCheckRollup"][0].update(status="COMPLETED", conclusion="SUCCESS")
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert _started(workflow) == []
    assert len(workflow.calls("gh run rerun")) == 1
