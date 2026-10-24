"""Merge safety for checks."""

import json
from copy import deepcopy
from typing import Any

import pytest

from workflow.merge_gate_fixtures import (
    GitHub,
    _DEPENDENCIES,
    _MAIN_IMAGES,
    _OLDER,
    _PYTHON,
    _REPO,
    _attempt_job,
    _attempt_path,
    _attempt_times,
    _cancelled_by_github,
    _images,
    _main_ci_run,
    _main_image_run,
    _main_jobs,
    _main_red_in_one_part,
    _main_red_on_an_advisory,
    _notes_api,
    _notes_by_ci_proof,
    _notes_path,
    _pr_job,
    _pr_parts,
    _pull_skipped_older,
    _receipt,
    _rerun_failed_jobs,
    _scheduled_images,
    _squash_with_pending_main,
    _train,
    _two_stale_pulls,
    gate,
    github,
)


def test_a_pull_request_whose_ci_noted_its_tested_merge_with_ci_proof_merges(
    github: GitHub,
) -> None:
    _notes_by_ci_proof(github, "merge", "--whole", "server_tests")

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"


@pytest.mark.parametrize("checked_out", ["head", "nothing"])
def test_a_pull_request_whose_ci_did_not_note_its_tested_merge_is_never_merged(
    github: GitHub, checked_out: str
) -> None:
    if checked_out == "nothing":
        # The planner never ran ci_proof.py: the run holds no notes.
        github.api[f"{_REPO}/check-runs/21/annotations?per_page=100"] = []
    else:
        _notes_by_ci_proof(github, checked_out, "--whole", "server_tests")

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert "CI requested" in done.stdout
    assert not (github.workflow.state / "merged").exists()


def test_a_clean_audit_that_ran_in_a_full_rerun_is_not_read_as_its_older_advisory(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A full re-run ran every job again: the audit's attempt is the run's latest, and a clean
    # audit writes no notes. Its pass over main's advisory in attempt 1 is older evidence.
    first = {"id": 39, "name": _DEPENDENCIES, "run_id": 30, "run_attempt": 1, **_attempt_times(1)}
    replies = {
        f"{gate.API}/check-runs/539/annotations?per_page=100": [],
        f"{gate.API}/actions/runs/30/attempts/1/jobs?per_page=100": [
            {**first, "head_sha": "a" * 40, "status": "completed", "conclusion": "success"}
        ],
        f"{gate.API}/check-runs/39/annotations?per_page=100": [
            {"title": "Advisory on main", "message": "GHSA-1 is on main too"}
        ],
    }
    monkeypatch.setattr(gate, "api", lambda path, key=None, *, paginate=True: replies[path])
    latest = {
        **first,
        **_attempt_times(2),
        "id": 539,
        "run_attempt": 2,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
    }
    assert gate.strict_jobs([latest])[0]["conclusion"] == "success"


@pytest.mark.parametrize(
    "case",
    [
        "carried",
        "failed copy",
        "still running",
        "unreadable",
        "ran again",
        "ran again, started the second it was created",
        "ran again, its clock a second behind",
    ],
)
def test_a_carried_job_reads_the_notes_of_the_attempt_it_ran_in(
    gate: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # Attempt 2 re-ran the failed jobs and carried the planner, which ran in attempt 1.
    if case.startswith("ran again"):
        job = _attempt_job(531, 2)  # a full re-run ran it again: created before it started
        if case.endswith("created"):
            job["started_at"] = job["created_at"]
        elif case.endswith("behind"):
            # It looks carried, but no earlier copy ran at its times.
            job["created_at"] = job["started_at"].replace(":02Z", ":03Z")
    else:
        job = _attempt_job(531, 2, None if case == "still running" else "success", ran=1)
    calls = _notes_api(
        gate,
        monkeypatch,
        {
            _notes_path(gate, 531): [],
            _attempt_path(gate, 1): gate.Waiting("gh api failed: HTTP 502")
            if case == "unreadable"
            else [_attempt_job(31, 1, "failure" if case == "failed copy" else "success")],
            _notes_path(gate, 31): [{"title": "Merge tested", "message": "first"}],
        },
    )
    if case == "unreadable":
        # Unread is not unannotated: the gate waits for GitHub instead of deciding.
        with pytest.raises(gate.Waiting):
            gate.job_notes(job)
        return
    notes = gate.job_notes(job)
    assert notes == ([{"title": "Merge tested", "message": "first"}] if case == "carried" else [])
    if case == "still running" or case.startswith("ran again") and not case.endswith("behind"):
        assert calls == [_notes_path(gate, 531)]


def test_a_job_carried_twice_reads_the_attempt_it_ran_in_not_an_older_copy(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It ran in attempt 2, which wrote "second"; attempt 3 carried it, and attempt 4 again.
    # Attempt 1's copy ran at other times, so its "first" is another run of the job.
    _notes_api(
        gate,
        monkeypatch,
        {
            _notes_path(gate, 731): [],
            _attempt_path(gate, 3): [_attempt_job(631, 3, ran=2)],
            _notes_path(gate, 631): [],
            _attempt_path(gate, 2): [_attempt_job(531, 2)],
            _notes_path(gate, 531): [{"title": "Merge tested", "message": "second"}],
            _attempt_path(gate, 1): [_attempt_job(31, 1)],
            _notes_path(gate, 31): [{"title": "Merge tested", "message": "first"}],
        },
    )
    assert gate.job_notes(_attempt_job(731, 4, ran=2)) == [
        {"title": "Merge tested", "message": "second"}
    ]


def test_a_fix_whose_full_rerun_passed_its_audit_clean_takes_the_red_main_turn(
    github: GitHub,
) -> None:
    """Main is red on "CI: dependencies". Pull request 8's first attempt passed over an
    advisory main had then; its full re-run, on the current main, passed every job and its
    audit clean. It is the fix of the red main, as it would be with no earlier attempt."""
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    jobs = f"{_REPO}/actions/runs/30/jobs?per_page=100"
    audit = {"id": 39, "name": _DEPENDENCIES, "status": "completed", "conclusion": "success"}
    first = [*deepcopy(github.api[jobs]["jobs"]), dict(audit)]
    for job in first:
        job.update(run_id=30, run_attempt=1, head_sha=head, **_attempt_times(1))
    github.api[f"{_REPO}/actions/runs/30/attempts/1/jobs?per_page=100"] = {"jobs": first}
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = [
        {"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}
    ]
    # Attempt 2 ran every job again: new IDs, the planner's own notes, a clean audit.
    latest = github.api[jobs]["jobs"]
    latest.append(dict(audit))
    planner_notes = github.api[f"{_REPO}/check-runs/31/annotations?per_page=100"]
    for job in latest:
        job.update(
            id=500 + job["id"], run_id=30, run_attempt=2, head_sha=head, **_attempt_times(2)
        )
    github.api[f"{_REPO}/check-runs/531/annotations?per_page=100"] = planner_notes
    github.api[f"{_REPO}/check-runs/532/annotations?per_page=100"] = []
    github.api[f"{_REPO}/check-runs/539/annotations?per_page=100"] = []
    runs = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
    github.api[runs]["workflow_runs"][0]["run_attempt"] = 2
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": _DEPENDENCIES, "status": "completed", "conclusion": "failure"},
    ]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "8"


@pytest.mark.parametrize("earlier", ["same", "same, re-run twice", "other main", "other head"])
def test_a_fix_whose_failed_jobs_alone_were_rerun_merges_on_red_main(
    github: GitHub, earlier: str
) -> None:
    workflow = github.workflow
    github.main("failure")
    if earlier == "other main":
        github.ci(github.base, github.head, tested_base=workflow.git("rev-parse", "main^"))
    _pr_job(github, "CI: web", "success")
    _rerun_failed_jobs(
        github,
        20,
        github.head,
        "CI: what to run",
        earlier_head=f"{9:040x}" if earlier == "other head" else None,
        attempts=3 if earlier == "same, re-run twice" else 2,
    )

    done = github.run()

    if earlier.startswith("same"):
        assert done.returncode == 0, done.stdout + done.stderr
        assert (workflow.state / "merged").read_text() == "7"
        assert "passed every check failing on main: CI result, CI: web" in done.stdout
    else:
        # The first attempt tested another merge: the pull request is refreshed, not merged.
        assert done.returncode == 2, done.stdout + done.stderr
        assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("earlier", ["same", "other main", "other head"])
def test_a_fix_whose_failed_jobs_alone_were_rerun_takes_the_red_main_turn(
    github: GitHub, earlier: str
) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    head = github.pulls["8"]["headRefOid"]
    github.ci(
        github.base,
        head,
        run_id=30,
        tested_base=workflow.git("rev-parse", "main^") if earlier == "other main" else None,
    )
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    _rerun_failed_jobs(
        github,
        30,
        head,
        "CI: what to run",
        earlier_head=f"{9:040x}" if earlier == "other head" else None,
    )
    github.main("failure")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == ("8" if earlier == "same" else "7")


def test_a_pass_over_mains_advisory_carried_into_a_rerun_is_not_the_fix(
    github: GitHub,
) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: dependencies", "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = [
        {"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}
    ]
    # Its warning is on the first attempt; the re-run carried the job without it. The planner
    # ran again with its notes, so the warning alone keeps 8 from being the fix.
    _rerun_failed_jobs(github, 30, head, "CI: dependencies")
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": "CI: dependencies", "status": "completed", "conclusion": "failure"},
    ]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "7"


@pytest.mark.parametrize("conclusion", ["skipped", None])
def test_a_pull_request_that_did_not_pass_main_failing_check_stays_blocked(
    github: GitHub, conclusion: str | None
) -> None:
    github.main("failure")
    if conclusion:
        _pr_job(github, "CI: web", conclusion)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert "CI: web" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_pull_request_that_passed_only_over_mains_advisory_is_not_its_fix(
    github: GitHub,
) -> None:
    _main_red_on_an_advisory(github, warned=True)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_pull_request_whose_audit_is_clean_on_a_red_main_is_its_fix(github: GitHub) -> None:
    _main_red_on_an_advisory(github, warned=False)

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"passed every check failing on main: CI result, {_DEPENDENCIES}" in done.stdout


def test_an_audit_passed_over_mains_advisory_is_not_inherited_by_main(github: GitHub) -> None:
    _pr_job(github, _DEPENDENCIES, "success")
    github.api[f"{_REPO}/check-runs/29/annotations?per_page=100"] = [
        {"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}
    ]

    _squash_with_pending_main(github)

    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    assert _DEPENDENCIES not in receipt["ci_passed"]
    assert receipt["ci_unproven"] == [_DEPENDENCIES]
    _main_jobs(github, (_DEPENDENCIES, "completed", "failure"))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout


def test_a_clean_audit_is_inherited_by_main(github: GitHub) -> None:
    _pr_job(github, _DEPENDENCIES, "success")
    github.api[f"{_REPO}/check-runs/29/annotations?per_page=100"] = []

    _squash_with_pending_main(github)

    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    assert _DEPENDENCIES in receipt["ci_passed"]
    assert receipt["ci_unproven"] == []


def test_a_missing_provenance_requests_new_ci_even_when_main_is_already_in_the_branch(
    github: GitHub,
) -> None:
    github.api[f"{_REPO}/check-runs/21/annotations?per_page=100"] = []
    done = github.run()
    assert done.returncode == 2, done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert github.workflow.git("rev-parse", "HEAD") != github.head
    assert "CI requested" in done.stdout


def test_a_check_of_a_different_pr_head_cannot_prove_freshness(github: GitHub) -> None:
    github.ci(github.base, github.base)
    path = (
        f"{_REPO}/actions/workflows/ci.yml/runs"
        f"?head_sha={github.head}&event=pull_request&per_page=100"
    )
    cast_runs: dict[str, Any] = github.api[path]
    cast_runs["workflow_runs"] = []
    done = github.run()
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_a_pending_or_failed_check_stops_even_a_fresh_pr(github: GitHub) -> None:
    pull: dict[str, Any] = github.pulls["7"]
    pull["statusCheckRollup"].append(
        {"name": "playwright", "status": "IN_PROGRESS", "conclusion": None}
    )
    done = github.run()
    assert done.returncode == 1
    assert "playwright" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_main_inherits_the_exact_ci_run_that_tested_its_squashed_tree(github: GitHub) -> None:
    # An older run exists too: only the latest, validated run may be credited.
    endpoint = (
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    )
    older = deepcopy(github.api[endpoint]["workflow_runs"][0])
    github.ci(github.base, github.head, run_id=70)
    github.api[endpoint]["workflow_runs"].append(older)
    _squash_with_pending_main(github)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "CI run 70" in status.stdout
    assert "CI run 20" not in status.stdout


def test_a_different_squash_tree_waits_for_main_ci(github: GitHub) -> None:
    _squash_with_pending_main(github, different=True)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout


def test_main_failure_overrides_inherited_green_and_stops_the_next_merge(github: GitHub) -> None:
    _squash_with_pending_main(github)
    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    github.main("failure")
    github.ci(github.base, github.head, run_id=80)
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "merging stopped: main: red" in done.stderr
    assert len(github.workflow.calls("gh pr merge")) == 1


@pytest.mark.parametrize("image_state", ["missing", "pending", "failed"])
def test_main_reuses_images_on_the_same_tree_but_its_own_failure_wins(
    github: GitHub, image_state: str
) -> None:
    _images(github, release_conclusion="success")
    _squash_with_pending_main(github)
    if image_state != "missing":
        _images(github, pull=False, conclusion="failure" if image_state == "failed" else "success")
        if image_state == "pending":
            endpoint = (
                f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
            )
            github.api[endpoint]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()
    status = github.workflow.run_script("main-status")
    if image_state == "failed":
        assert status.stdout.startswith("main: red"), status.stdout
    else:
        assert status.stdout.startswith("main: green"), status.stdout
        assert "Docker images run 40" in status.stdout


def test_inheritance_does_not_cover_a_later_untested_main_commit(github: GitHub) -> None:
    squash = _squash_with_pending_main(github)
    later = github.workflow.git(
        "commit-tree",
        github.workflow.git("rev-parse", f"{squash}^{{tree}}"),
        "-p",
        squash,
        "-m",
        "chore: unrelated hand push",
    )
    github.workflow.git("push", "origin", f"{later}:main")
    github.base = later
    github.main()
    endpoint = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={later}&per_page=100"
    github.api[endpoint]["workflow_runs"] = []
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout


def test_the_next_gate_can_merge_before_main_ci_has_even_started(github: GitHub) -> None:
    _squash_with_pending_main(github)
    endpoint = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    github.api[endpoint]["workflow_runs"] = []
    github.ci(github.base, github.head, run_id=80)
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "inherited from CI run 20" in done.stdout
    assert len(github.workflow.calls("gh pr merge")) == 2


def test_inherited_ci_cannot_replace_required_images_without_image_evidence(github: GitHub) -> None:
    _squash_with_pending_main(github)
    # A push's image workflow can have started even though no PR image result was available.
    _images(github, pull=False)
    endpoint = f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
    github.api[endpoint]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout
    assert "Docker images" in status.stdout


def test_scheduled_failures_are_still_reported_when_main_inherits_green(github: GitHub) -> None:
    _squash_with_pending_main(github)
    _scheduled_images(github, status="completed", conclusion="failure")
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "scheduled Docker images run 50 failed" in status.stdout


@pytest.mark.parametrize(
    "receipt", ["missing", "invalid", "missing-fields", "wrong-tree", "without-unproven-jobs"]
)
def test_unusable_inheritance_evidence_waits_for_main(github: GitHub, receipt: str) -> None:
    _squash_with_pending_main(github)
    path = github.workflow.repo / ".local/workflow/main-tree-green.json"
    if receipt == "missing":
        path.unlink()
    elif receipt == "invalid":
        path.write_text("{interrupted")
    elif receipt == "missing-fields":
        path.write_text("{}")
    elif receipt == "without-unproven-jobs":
        # A receipt from before it named the jobs the pull request skipped.
        data = json.loads(path.read_text())
        del data["ci_unproven"]
        path.write_text(json.dumps(data))
    else:
        data = json.loads(path.read_text())
        data["tree"] = "0" * 40
        path.write_text(json.dumps(data))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout


def test_identical_tree_with_an_unexpected_squash_parent_gets_no_receipt(github: GitHub) -> None:
    _squash_with_pending_main(github, wrong_parent=True)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout
    assert not (github.workflow.repo / ".local/workflow/main-tree-green.json").exists()


def test_inherited_ci_waits_for_required_images_when_its_receipt_lacks_them(github: GitHub) -> None:
    workflow = github.workflow
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    _images(github)
    _squash_with_pending_main(github)
    path = workflow.repo / ".local/workflow/main-tree-green.json"
    receipt = json.loads(path.read_text())
    receipt["images_run"] = None
    path.write_text(json.dumps(receipt))
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending, Docker images"), status.stdout
    _images(github, pull=False)
    github.save()
    assert workflow.run_script("main-status").stdout.startswith("main: green")


@pytest.mark.parametrize("main_images", ["missing", "pending"])
def test_skipped_pr_release_checks_cannot_cover_main_release_tests(
    github: GitHub, main_images: str
) -> None:
    workflow = github.workflow
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    _images(github)  # The PR skipped release tests; main's push will run them.
    _squash_with_pending_main(github)
    if main_images == "pending":
        _images(github, pull=False)
        endpoint = f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
        github.api[endpoint]["workflow_runs"][0].update(status="in_progress", conclusion=None)
        release = github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"][-1]
        release.update(status="in_progress", conclusion=None)
    github.save()
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout
    assert "Docker images" in status.stdout


def test_an_earlier_passed_image_run_does_not_stand_in_for_main_release_tests(
    github: GitHub,
) -> None:
    workflow = github.workflow
    # The image run of an earlier push of main passed; it is in main's list of image runs.
    _main_image_run(github, 90, github.base)
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    _images(github)  # The PR skipped release tests; main's push will run them.
    _squash_with_pending_main(github)
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending, Docker images have not started"), status.stdout
    _images(github, pull=False, release_conclusion="success")
    github.save()
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout


def test_a_ci_job_the_pull_request_skipped_waits_for_mains_own_success(github: GitHub) -> None:
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    assert receipt["ci_passed"] == ["CI result", "CI: what to run"]
    assert receipt["ci_unproven"] == [_OLDER]
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: pending, {_OLDER}"), status.stdout
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "in_progress", None))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: pending, {_OLDER}"), status.stdout
    # Main's own result for the skipped job is enough; the jobs the pull request passed are
    # inherited while main still runs them.
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "completed", "success"))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "inherited from CI run 20" in status.stdout


def test_mains_failure_of_a_job_the_pull_request_skipped_turns_main_red(github: GitHub) -> None:
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "completed", "failure"))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout
    assert _OLDER in status.stdout


def test_no_merge_while_main_waits_for_a_job_the_pull_request_skipped(github: GitHub) -> None:
    # The next merge would push main and cancel the very run the gate waits for.
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    _main_jobs(github, (_OLDER, "in_progress", None))
    github.ci(github.base, github.head, run_id=80)
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"merging stopped: main: pending, {_OLDER}" in done.stderr
    assert len(github.workflow.calls("gh pr merge")) == 1


def test_main_waits_for_every_part_of_a_check_the_pull_request_skipped(github: GitHub) -> None:
    # Skipped, the job has its name alone; on main it runs in parts, named with their part.
    _pr_job(github, _PYTHON, "skipped")
    _squash_with_pending_main(github)
    assert _receipt(github)["ci_unproven"] == [_PYTHON]

    _main_jobs(
        github,
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "in_progress", None),
    )
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: pending, {_PYTHON} ("), status.stdout

    _main_jobs(
        github,
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "completed", "success"),
    )
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "inherited from CI run 20" in status.stdout


def test_one_failed_part_on_main_makes_the_whole_check_red(github: GitHub) -> None:
    _pr_job(github, _PYTHON, "skipped")
    _squash_with_pending_main(github)
    _main_jobs(
        github,
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "completed", "failure"),
    )
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: red since 2026-10-16T09:00:00Z, {_PYTHON} ("), (
        status.stdout
    )
    assert "of 2)" not in status.stdout


def test_main_inherits_a_check_whose_every_part_passed(github: GitHub) -> None:
    _pr_parts(github, "success", "success")
    _squash_with_pending_main(github)

    receipt = _receipt(github)
    assert _PYTHON in receipt["ci_passed"]
    assert receipt["ci_unproven"] == []


def test_an_inherited_main_run_still_running_is_cancelled_and_main_stays_green(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    assert workflow.calls("gh run cancel") == ["gh run cancel 10 -R example-org/example-app"]
    receipt = _receipt(github)
    assert receipt["cancelled_inherited"] == [{"run": 10, "attempt": 1, "tree": receipt["tree"]}]
    _cancelled_by_github(github)
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout + status.stderr
    assert "inherited from CI run 20" in status.stdout
    assert workflow.run_script("main-status", "--tick-main-status").stdout.startswith("main: green")
    assert len(workflow.calls("gh run cancel")) == 1


def test_a_main_run_cancelled_otherwise_is_still_red(github: GitHub) -> None:
    _squash_with_pending_main(github)
    _cancelled_by_github(github)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr
    # Listed for another tree, it is not this main's.
    path = github.workflow.repo / ".local/workflow/main-tree-green.json"
    elsewhere = [{"run": 10, "attempt": 1, "tree": "0" * 40}]
    path.write_text(json.dumps({**_receipt(github), "cancelled_inherited": elsewhere}))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr


def test_a_listed_run_that_failed_before_its_cancel_is_still_red(github: GitHub) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)
    assert workflow.run_script("main-status", "--tick-main-status").stdout.startswith("main: green")
    _main_ci_run(github).update(status="completed", conclusion="cancelled")
    _main_jobs(github, ("CI result", "completed", "cancelled"), ("CI: web", "completed", "failure"))
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr
    assert "CI: web" in status.stdout


def test_a_main_run_not_yet_inherited_is_never_cancelled(github: GitHub) -> None:
    workflow = github.workflow
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "in_progress", None))

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith(f"main: pending, {_OLDER}"), done.stdout + done.stderr
    assert workflow.calls("gh run cancel") == []
    assert "cancelled_inherited" not in _receipt(github)
    # The plain status, which holds no lock, cancels nothing either.
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "completed", "success"))
    assert workflow.run_script("main-status").stdout.startswith("main: green")
    assert workflow.calls("gh run cancel") == []


def test_the_cancelled_runs_of_another_tree_are_pruned(github: GitHub) -> None:
    _squash_with_pending_main(github)
    path = github.workflow.repo / ".local/workflow/main-tree-green.json"
    older = {"run": 5, "attempt": 1, "tree": "0" * 40}
    path.write_text(json.dumps({**_receipt(github), "cancelled_inherited": [older]}))

    done = github.workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    receipt = _receipt(github)
    assert receipt["cancelled_inherited"] == [{"run": 10, "attempt": 1, "tree": receipt["tree"]}]


def test_a_refused_cancel_of_main_is_logged_once(github: GitHub) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)
    github.refuse_cancel = [10]
    github.save()

    for _ in range(2):
        done = workflow.run_script("main-status", "--tick-main-status")
        assert done.stdout.startswith("main: green"), done.stdout + done.stderr

    assert len(workflow.calls("gh run cancel")) == 1
    notes = [line for line in workflow.wake_log.read_text().splitlines() if "run 10" in line]
    assert len(notes) == 1 and "Cannot cancel a workflow run that is completed." in notes[0]


def test_an_inherited_image_run_of_main_is_left_to_finish(github: GitHub) -> None:
    # Main is judged by its latest image run on any earlier push too: cancelled, this one
    # would decide main once a later push replaced its receipt.
    workflow = github.workflow
    _images(github, release_conclusion="success")
    _squash_with_pending_main(github)
    _main_image_run(github, 60, github.base, status="in_progress", conclusion=None)
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [github.api[_MAIN_IMAGES]["workflow_runs"][0]]
    }
    github.save()

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    assert "Docker images run 40" in done.stdout
    assert workflow.calls("gh run cancel") == ["gh run cancel 10 -R example-org/example-app"]


def test_a_cancelled_inherited_run_whose_summary_job_failed_after_the_cancel_stays_green(
    github: GitHub,
) -> None:
    # "CI result" runs even in a cancelled run, and fails for the jobs cancelled under it.
    workflow = github.workflow
    _squash_with_pending_main(github)
    assert workflow.run_script("main-status", "--tick-main-status").stdout.startswith("main: green")
    _main_ci_run(github).update(status="completed", conclusion="cancelled")
    _main_jobs(github, ("CI result", "completed", "failure"), ("CI: web", "completed", "cancelled"))
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout + status.stderr


def test_a_re_run_of_main_is_never_cancelled(github: GitHub) -> None:
    # A second attempt, such as the tick's own retry of a failed main, decides main itself.
    workflow = github.workflow
    _squash_with_pending_main(github)
    _main_ci_run(github)["run_attempt"] = 2
    github.save()

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    assert workflow.calls("gh run cancel") == []
    # Cancelled by someone else, it is red as any other cancelled run.
    _cancelled_by_github(github)
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr


def test_a_cancel_lost_to_a_github_outage_is_tried_again(github: GitHub) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)
    github.cancel_unavailable = [10]
    github.save()

    for _ in range(2):
        done = workflow.run_script("main-status", "--tick-main-status")
        assert done.stdout.startswith("main: green"), done.stdout + done.stderr

    assert len(workflow.calls("gh run cancel 10 ")) == 2
    assert not workflow.wake_log.exists() or "run 10" not in workflow.wake_log.read_text()
    assert len(workflow.calls("gh run cancel")) == 2
    # Accepted, it is not asked again while GitHub still lists the run as finishing.
    workflow.run_script("main-status", "--tick-main-status")
    assert len(workflow.calls("gh run cancel")) == 2


def test_a_fix_that_passed_every_part_of_mains_failed_check_merges(github: GitHub) -> None:
    _main_red_in_one_part(github)
    # Dealt to parts otherwise than main's run was: the check is what has to pass.
    _pr_parts(github, "success")

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert f"passed every check failing on main: CI result, {_PYTHON}" in done.stdout


def test_a_pull_request_with_a_failed_part_is_not_the_fix(github: GitHub) -> None:
    _main_red_in_one_part(github)
    _pr_parts(github, "failure", "success")

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert not (github.workflow.state / "merged").exists()
