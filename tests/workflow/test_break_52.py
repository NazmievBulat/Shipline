"""Breaking tests for pull request 52: deferred check groups and the daily savings line.

Each test replays a daily use of a repository that lists every group in
`checks.prefix_only`: three image browser parts, the image tests and two release parts only a
release change runs; a whole browser suite in three parts; server tests in four parts; a merge
line of up to four prefixes; and a wake that tells the orchestrator once a day what was saved.
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow

_REPO = "repos/example-org/example-app"
_BASE = "b" * 40
_TREE = "7" * 40
_DAY = "2026-10-12"
# A wake shortly after UTC midnight reports the day before.
_NOW = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc).timestamp()

# Answers every read the savings report makes for a day of merged pull requests, each merged by
# its prefix with three scratch runs (CI, E2E, images) and some own CI runs (ready, pushes,
# refreshes). Every read is logged, and each one takes LATENCY seconds, like `gh api` does.
_FAKE_GH = r'''#!@PYTHON@
import json, os, re, sys, time
from pathlib import Path
from typing import Any
args = sys.argv[1:]
before = os.environ.get("FAKE_GH_BEFORE")
SAVINGS = ("search/issues?", "@REPO@/pulls/", "@REPO@/git/commits/", "@REPO@/actions/",
           "@REPO@/check-runs/")
if not args or args[0] != "api" or not args[-1].startswith(SAVINGS):
    if before:
        os.execv(before, ["gh", *args])
    sys.exit("only savings reads are expected")
path = args[-1]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(path + "\n")
time.sleep(float(os.environ.get("FAKE_GH_LATENCY", "0")))
pulls, own_runs = int(os.environ["FAKE_PULLS"]), int(os.environ["FAKE_OWN_RUNS"])
repo, tree, base, day = "@REPO@", "@TREE@", "@BASE@", "@DAY@"
full = repo.removeprefix("repos/")
ok = {"status": "completed", "conclusion": "success"}

def sha(kind, n):
    return f"{kind}{n:039x}"[-40:] if kind != "s" else f"{n:040x}"

def timed(job_id, name, minutes, conclusion="success"):
    job = {"id": job_id, "name": name, "status": "completed", "conclusion": conclusion,
           "started_at": f"{day}T10:00:00Z", "created_at": f"{day}T09:59:00Z",
           "completed_at": f"{day}T10:{minutes:02d}:00Z"}
    if conclusion == "success":
        job["runner_name"] = "example-runner"
    return job

def answer(value):
    print(json.dumps(value))
    sys.exit(0)

if path.startswith("search/issues?"):
    answer({"total_count": pulls, "incomplete_results": False,
            "items": [{"number": n} for n in range(1, pulls + 1)]})
if m := re.fullmatch(rf"{repo}/pulls/(\d+)", path):
    n = int(m[1])
    answer({"merged_at": f"{day}T18:00:00Z", "created_at": f"{day}T08:00:00Z",
            "merge_commit_sha": sha("s", n), "head": {"ref": f"ticket/{n}"},
            "base": {"ref": "main"}})
if m := re.fullmatch(rf"{repo}/git/commits/([0-9a-f]{{40}})", path):
    commit = m[1]
    if commit.startswith("a"):
        n = int(commit[1:], 16)
        answer({"tree": {"sha": tree}, "parents": [{"sha": base}, {"sha": sha("c", n)}]})
    n = int(commit, 16)
    sources = [
        {"workflow": "ci", "run": n * 100 + 2,
         "jobs": ["CI result", "CI: server tests", "CI: what to run"]},
        {"workflow": "e2e", "run": n * 100 + 3, "jobs": ["E2E: what to run", "playwright"]},
        {"workflow": "images", "run": n * 100 + 4,
         "jobs": ["Docker images: browser tests", "Docker images: image tests",
                  "Docker images: what to run"]},
    ]
    proof = json.dumps({"base": base, "tree": tree, "sources": sources}, separators=(",", ":"))
    answer({"message": f"fix: change {n}\n\nWorkflow-Proof: {proof}", "tree": {"sha": tree},
            "parents": [{"sha": base}]})
if m := re.fullmatch(rf"{repo}/actions/workflows/ci\.yml/runs\?(.*)", path):
    n = int(re.search(r"branch=ticket%2F(\d+)", m[1])[1])
    answer({"workflow_runs": [
        {"id": n * 100 + 50 + k, "event": "pull_request", "head_branch": f"ticket/{n}",
         "created_at": f"{day}T1{k}:00:00Z", "pull_requests": [{"number": n}],
         "head_repository": {"full_name": full}}
        for k in range(own_runs)]})
if m := re.fullmatch(rf"{repo}/actions/runs/(\d+)", path):
    run = int(m[1])
    n, kind = divmod(run, 100)
    workflow = {2: "ci", 3: "e2e", 4: "images"}[kind]
    answer({"id": run, **ok, "path": f".github/workflows/{workflow}.yml",
            "head_repository": {"full_name": full}, "event": "workflow_dispatch",
            "head_branch": "workflow/merge-batch-example", "head_sha": sha("a", n)})
if m := re.fullmatch(rf"{repo}/actions/runs/(\d+)/jobs\?per_page=100", path):
    run = int(m[1])
    kind = run % 100
    first = run * 10
    if kind == 2:
        jobs = [timed(first, "CI: what to run", 1), timed(first + 9, "CI result", 1),
                *(timed(first + p, f"CI: server tests ({p} of 4)", 12) for p in range(1, 5))]
    elif kind == 3:
        jobs = [timed(first, "E2E: what to run", 1),
                *(timed(first + p, f"playwright ({p} of 3)", 14) for p in range(1, 4))]
    elif kind == 4:
        jobs = [timed(first, "Docker images: what to run", 1),
                *(timed(first + p, f"Docker images: browser tests ({p} of 3)", 13) for p in range(1, 4)),
                timed(first + 4, "Docker images: image tests", 11),
                *(timed(first + 4 + p, f"Docker images: release tests ({p} of 2)", 0, "skipped")
                  for p in range(1, 3))]
    else:
        jobs = [timed(first, "CI: what to run", 1), timed(first + 1, "CI: fast checks", 4)]
    answer({"total_count": len(jobs), "jobs": jobs})
if m := re.fullmatch(rf"{repo}/check-runs/(\d+)/annotations\?per_page=100", path):
    run = int(m[1]) // 10
    n, kind = divmod(run, 100)
    if kind == 2:
        answer([{"title": "Merge tested", "message": sha("a", n)},
                {"title": "Server tests", "message": "whole"}])
    if kind == 3:
        answer([{"title": "Files tested", "message": tree},
                {"title": "Browser suite", "message": "whole"}])
    if kind == 4:
        answer([{"title": "Files tested", "message": tree}])
    answer([{"title": "Prefix-only groups",
             "message": json.dumps(["images", "browser_whole", "server_whole"])}])
sys.exit(f"unexpected read: {path}")
'''


def _savings_day_of(workflow: Workflow, pulls: int, own_runs: int, latency: float = 0) -> Path:
    (workflow.repo / "shipline.json").write_text(json.dumps({
        "checks": {"prefix_only": ["images", "browser_whole", "server_whole"]}
    }))
    fake = (
        _FAKE_GH.replace("@PYTHON@", sys.executable).replace("@REPO@", _REPO)
        .replace("@TREE@", _TREE).replace("@BASE@", _BASE).replace("@DAY@", _DAY)
    )
    original = workflow.bin / "gh"
    if original.exists():
        workflow.fake("gh-before-savings", original.read_text())
        workflow.env["FAKE_GH_BEFORE"] = str(workflow.bin / "gh-before-savings")
    workflow.fake("gh", fake)
    log = workflow.root / "gh-reads.log"
    workflow.env.update(FAKE_GH_LOG=str(log), FAKE_PULLS=str(pulls),
                        FAKE_OWN_RUNS=str(own_runs), FAKE_GH_LATENCY=str(latency))
    return log


def _report(workflow: Workflow, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(workflow.script.parent / "prefix_savings.py"), "--now", str(_NOW)],
        cwd=workflow.repo, env=workflow.env, capture_output=True, text=True, timeout=timeout,
        check=False,
    )


def test_one_merged_pull_request_is_priced_by_its_prefix_runs(workflow: Workflow) -> None:
    """A repository with every group listed: four server parts, three browser parts, three image browser
    parts plus the image tests, release parts skipped; four own CI runs each deferred all."""
    _savings_day_of(workflow, pulls=1, own_runs=4)
    done = _report(workflow)
    assert done.returncode == 0, done.stderr
    # images 3*13 + 11 = 50 min, browser 3*14 = 42, server 4*12 = 48; each times 4 own runs.
    assert done.stdout.strip() == (
        f"Prefix-only runner minutes saved ({_DAY} UTC): images 200.00; "
        "browser_whole 168.00; server_whole 192.00; total 560.00."
    )


# `gh api` takes about 0.4 s a read from a machine with a good connection (measured here, five
# reads of one run: 0.38 to 0.69 s). The wake gives the whole report 120 s.
_READ_SECONDS = 0.4
_WAKE_DEADLINE = 120


@pytest.mark.parametrize("pulls", [5, 10, 15, 20])
def test_a_busy_day_report_fits_in_the_wakes_deadline(workflow: Workflow, pulls: int) -> None:
    """Every read is made again from scratch at each try; nothing is kept between ticks."""
    log = _savings_day_of(workflow, pulls=pulls, own_runs=4)
    done = _report(workflow)
    assert done.returncode == 0, done.stderr
    reads = len(log.read_text().splitlines())
    print(f"{pulls} merged pull requests: {reads} reads, about {reads * _READ_SECONDS:.0f} s")
    assert reads * _READ_SECONDS <= _WAKE_DEADLINE, (
        f"{pulls} merged pull requests need {reads} GitHub reads, about "
        f"{reads * _READ_SECONDS:.0f} s at {_READ_SECONDS} s a read; the wake stops the report "
        f"at {_WAKE_DEADLINE} s and keeps nothing of it"
    )


@pytest.mark.timeout(400)
def test_a_report_cut_at_the_deadline_is_lost_and_started_over_by_the_next_tick(
    workflow: Workflow,
) -> None:
    """A night check-in after a day of 15 merged pull requests, reads at 0.4 s each."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.add_pull_request(9, draft=False)
    workflow.usage(76, "2026-10-14T05:10:00+00:00", seven=41)
    workflow.orchestrator("idle")
    log = _savings_day_of(workflow, pulls=15, own_runs=4, latency=_READ_SECONDS)
    started = time.monotonic()
    done = subprocess.run(
        ["bash", str(workflow.script.parent / "wake")], cwd=workflow.repo,
        env={**workflow.env, "WORKFLOW_NOW": str(int(_NOW)), "TZ": "America/New_York"},
        capture_output=True, text=True, timeout=600, check=False,
    )
    took = time.monotonic() - started
    reads = len(log.read_text().splitlines())
    folder = workflow.repo / ".local/workflow"
    kept = sorted(path.name for path in folder.glob("prefix-savings*"))
    told = "\n".join(workflow.sent())
    print(f"wake took {took:.0f} s, {reads} reads, kept {kept}, told: {told[:200]!r}")
    # The tick, which runs every 3 minutes and holds its lock meanwhile, waited for all of it.
    assert "Prefix-only runner minutes saved" in told, (
        f"no savings line after {reads} reads in {took:.0f} s of the tick; kept {kept}: "
        "the next tick reads it all again"
    )


# The gate's own replays: real Git histories and the gate's GitHub stand-in.
from workflow.test_merge_when_current import (  # noqa: E402
    _REPO as _GATE_REPO,
    GitHub,
    _batch_call,
    _batch_setup,
    _batch_state,
    _changes,
    _finish_batch,
    _finish_prefix,
    _prefix_only,
    _prefixes,
    _rerun_failed_jobs,
    _suite,
    github,  # noqa: F401  (the fixture)
)

_ALL = ["images", "browser_whole", "server_whole"]


def _six_image_jobs(github: GitHub, run_id: int, failed_part: int | None = None) -> None:
    """The image check's six jobs: three browser parts, the image tests, and two release parts
    that only a release change runs, after its planner."""
    def job(offset: int, name: str, conclusion: str = "success") -> dict[str, Any]:
        return {"id": run_id + offset, "name": name, "status": "completed", "conclusion": conclusion}

    github.api[f"{_GATE_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {"jobs": [
        job(1, "Docker images: what to run"),
        *(job(1 + p, f"Docker images: browser tests ({p} of 3)",
              "failure" if p == failed_part else "success") for p in (1, 2, 3)),
        job(5, "Docker images: image tests"),
        *(job(5 + p, f"Docker images: release tests ({p} of 2)", "skipped") for p in (1, 2)),
    ]}


def _image_run(github: GitHub, scratch: dict[str, Any]) -> dict[str, Any]:
    return github.api[
        f"{_GATE_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0]


def _proof_of(github: GitHub) -> dict[str, Any]:
    squash = github.workflow.git("rev-parse", "origin/main")
    message = github.workflow.git("show", "-s", "--format=%B", squash)
    return json.loads(next(line.removeprefix("Workflow-Proof: ")
                           for line in message.splitlines() if line.startswith("Workflow-Proof: ")))


def test_a_first_member_alone_merges_after_one_failed_image_part_is_rerun_alone(
    github: GitHub,
) -> None:
    """A docker change merging alone: its image check runs only on the gate's scratch branch;
    one browser part fails, the owner re-runs the failed jobs alone, and the next call merges."""
    tree = _changes(github, "docker/change.txt")
    _prefix_only(github, _ALL)
    # Its own image run, from before the setting or late, failed: it no longer decides.
    from workflow.test_merge_when_current import _images
    _images(github, conclusion="failure")
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    scratch = _suite(github)
    assert "images" in scratch["workflows"]
    _finish_prefix(github, "7", scratch=scratch, tree=tree, run_id=800)
    image_id = 800 + 10 * scratch["workflows"].index("images")
    _six_image_jobs(github, image_id, failed_part=2)
    _image_run(github, scratch).update(conclusion="failure")
    done = _batch_call(github)
    assert done.returncode == 1 and "failed" in done.stderr, done.stdout + done.stderr
    assert not (github.workflow.state / "merged").exists()
    # Re-run of the failed jobs alone: every other job is carried, its planner without notes.
    _six_image_jobs(github, image_id)
    _image_run(github, scratch).update(conclusion="success")
    _rerun_failed_jobs(
        github, image_id, scratch["head"], "Docker images: what to run",
        "Docker images: browser tests (1 of 3)", "Docker images: browser tests (3 of 3)",
        "Docker images: image tests",
    )
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    proof = _proof_of(github)
    images = [source for source in proof["sources"] if source["workflow"] == "images"]
    assert images and images[0]["run"] == image_id
    assert {"Docker images: browser tests", "Docker images: image tests"} <= set(images[0]["jobs"])
    assert proof["tree"] == tree


@pytest.mark.timeout(400)
def test_a_line_of_four_with_every_group_listed_merges_each_by_its_prefix(github: GitHub) -> None:
    """Four ready members, the first a docker change; each own run was a selection."""
    _batch_setup(github, count=4, images=True, selected=True)
    _prefix_only(github, _ALL)
    merged: list[str] = []
    for round_ in range(6):
        done = _batch_call(github)
        merged = [n for n, pull in github.pulls.items() if pull["state"] == "MERGED"]
        if len(merged) == 4:
            break
        assert done.returncode in (0, 3), done.stdout + done.stderr
        if not _prefixes(github):
            continue
        _finish_batch(github, run_id=100 + 10_000 * round_)
        for member in _prefixes(github):
            scratch = member["scratch"]
            if scratch and "images" in scratch["workflows"]:
                image_id = 100 + 10_000 * round_ + 1000 * _prefixes(github).index(member) \
                    + 10 * scratch["workflows"].index("images")
                _six_image_jobs(github, image_id)
    assert sorted(merged) == ["10", "7", "8", "9"], _batch_state(github)


from workflow.test_lost_check import _stamp, _waiting_reviewer  # noqa: E402
from workflow.test_merge_when_current import _images, line_up  # noqa: E402


def _lose_own_image_check(github: GitHub) -> None:
    """Its own image run finished an hour ago, green; GitHub's rollup still lists its planner
    as running: a lost check, the kind the tick looks for while a reviewer waits."""
    _images(github)
    run = github.api[
        f"{_GATE_REPO}/actions/workflows/images.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    run["updated_at"] = _stamp(3600)
    github.api[f"{_GATE_REPO}/actions/runs/{run['id']}"] = run
    github.pulls["7"]["statusCheckRollup"].append({
        "__typename": "CheckRun",
        "name": "Docker images: what to run",
        "status": "IN_PROGRESS",
        "conclusion": None,
        "startedAt": _stamp(3900),
        "detailsUrl": f"https://github.com/{_GATE_REPO.removeprefix('repos/')}/actions/runs/"
        f"{run['id']}/job/{run['id'] + 1}",
    })
    github.save()


@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.parametrize("in_line", [True, False])
def test_a_reviewer_waiting_on_checks_is_woken_when_its_own_image_check_is_lost(
    github: GitHub, listed: bool, in_line: bool,
) -> None:
    """Every other check of the pull request passed. Without the setting the tick finds the lost
    image check and wakes the reviewer; with images listed the gate no longer needs that check,
    so the reviewer must still be woken, by its checks or as a lost check."""
    workflow = github.workflow
    if listed:
        _prefix_only(github, ["images"])
    _lose_own_image_check(github)
    if in_line:
        line_up(github, ["7"])
    _waiting_reviewer(github)
    for _ in range(3):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    sent = workflow.calls("tmux review-7 send-keys")
    log = Path(workflow.env["HOME"]) / ".local/state/workflow/wake.log"
    assert sent, (
        "three ticks and the waiting reviewer was never woken; "
        f"wake log: {log.read_text() if log.exists() else '(none)'}"
    )


@pytest.mark.parametrize("held", [False, True])
def test_low_a_wake_beside_one_holding_the_savings_lock_drops_its_reset_names(
    workflow: Workflow, held: bool,
) -> None:
    """Low, not reached in use: the tick runs one wake at a time under its own lock. A wake run
    by hand while another holds the savings lock exits 0 at once, before it tells or keeps the
    sessions it was given."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.usage(76, "2026-10-14T05:10:00+00:00", seven=41)
    workflow.orchestrator("idle")
    _savings_day_of(workflow, pulls=1, own_runs=1)
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(["flock", str(folder / "prefix-savings.lock"), "sleep", "30" if held else "0"])
    try:
        time.sleep(0.5)
        done = subprocess.run(
            ["bash", str(workflow.script.parent / "wake"), "ticket-7"], cwd=workflow.repo,
            env={**workflow.env, "WORKFLOW_NOW": str(int(_NOW)), "TZ": "America/New_York"},
            capture_output=True, text=True, timeout=60, check=False,
        )
    finally:
        holder.kill()
    pending = Path(workflow.env["HOME"]) / ".local/state/workflow/wake.pending"
    kept = pending.exists() and "ticket-7" in pending.read_text()
    assert done.returncode == 0
    assert workflow.sent() or kept, "the reset of ticket-7 was neither told nor kept"
