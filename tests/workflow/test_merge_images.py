"""Merge safety for images."""

from typing import Any

import pytest

from workflow.merge_gate_fixtures import (
    GitHub,
    _MAIN_IMAGES,
    _REPO,
    _images,
    _main_image_run,
    _push_without_images,
    _rerun_failed_jobs,
    _scheduled_images,
    _squash_with_pending_main,
    github,
    rerun_calls,
)


def test_a_failed_image_check_on_main_stops_merging(github: GitHub) -> None:
    _images(github, pull=False, conclusion="failure")
    done = github.run()
    assert done.returncode == 1
    assert "main: red since" in done.stderr and "Docker images: image tests" in done.stderr


def test_images_passed_on_the_tested_tree_allow_merging_with_unused_jobs_skipped(
    github: GitHub,
) -> None:
    _images(github)
    done = github.run()
    assert done.returncode == 0, done.stderr


def test_images_whose_failed_jobs_alone_were_rerun_still_prove_their_tree(
    github: GitHub,
) -> None:
    _images(github)
    _rerun_failed_jobs(github, 40, github.head, "Docker images: what to run")
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"


def test_images_of_an_older_merge_tree_request_new_ci(github: GitHub) -> None:
    _images(github, tree="old-tree")
    done = github.run()
    assert done.returncode == 2, done.stderr
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize(
    "filename", ["Dockerfile", "é.txt", "tab\tname.txt", "line\nname.txt", "raw\udcff.txt"]
)
def test_a_required_image_run_that_has_not_started_is_waited_for(
    github: GitHub, filename: str
) -> None:
    workflow = github.workflow
    workflow.git("config", "core.quotePath", "true")
    image_input = workflow.repo / "docker" / filename
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    done = github.run()
    assert done.returncode == 1
    assert "Docker images checks have not started" in done.stderr
    assert not (workflow.state / "merged").exists()


def test_a_failure_on_the_second_page_of_jobs_stops_merging(github: GitHub) -> None:
    endpoint = f"{_REPO}/actions/runs/20/jobs?per_page=100"
    first: dict[str, Any] = github.api[endpoint]
    github.api[endpoint] = {
        "_pages": [
            first,
            {
                "jobs": [
                    {"id": 24, "name": "CI: web", "status": "completed", "conclusion": "failure"}
                ]
            },
        ]
    }
    done = github.run()
    assert done.returncode == 1
    assert "latest CI run" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_merge_provenance_on_the_second_page_is_read(github: GitHub) -> None:
    endpoint = f"{_REPO}/check-runs/21/annotations?per_page=100"
    original = github.api[endpoint]
    github.api[endpoint] = {
        "_pages": [[{"title": "other notice", "message": "unrelated"}], original]
    }
    done = github.run()
    assert done.returncode == 0, done.stderr


def test_main_turning_red_while_pr_checks_are_read_stops_the_merge(github: GitHub) -> None:
    github.main("failure")
    github.save()
    after = github.workflow.state / "github-after-annotations.json"
    after.write_text((github.workflow.state / "github.json").read_text())
    github.main()
    done = github.run()
    assert done.returncode == 1
    assert "main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_failed_image_run_keeps_main_red_after_a_push_without_images(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: red since 2026-10-18T07:30:23Z"), status.stdout
    assert "Docker images: browser tests (1 of 2)" in status.stdout
    assert f"Docker images run 60 of {earlier[:12]}" in status.stdout
    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_later_passing_image_run_on_main_turns_it_green_again(github: GitHub) -> None:
    failed = _push_without_images(github)
    _main_image_run(github, 60, failed, conclusion="failure")
    passed = _push_without_images(github)
    _main_image_run(github, 70, passed)
    _push_without_images(github)
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: green"), status.stdout
    assert done.returncode == 0, done.stderr
    assert (github.workflow.state / "merged").exists()


def test_a_green_rerun_of_the_failed_image_run_turns_main_green(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    github.save()
    assert github.workflow.run_script("main-status").stdout.startswith("main: red")
    # A re-run keeps its run id and replaces its conclusion.
    _main_image_run(github, 60, earlier)
    del github.api[_MAIN_IMAGES]["workflow_runs"][1]
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert github.run().returncode == 0


def test_an_image_run_in_progress_on_an_earlier_push_holds_main_pending(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier)
    _main_image_run(
        github, 60, earlier, status="in_progress", conclusion=None, event="workflow_dispatch"
    )
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: pending"), status.stdout
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_a_failed_image_run_behind_one_in_progress_keeps_main_red(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, conclusion="failure")
    _main_image_run(
        github, 60, earlier, status="in_progress", conclusion=None, event="workflow_dispatch"
    )
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: red")
    assert github.run().returncode == 1
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("receipt", [False, True])
def test_superseded_image_cancellation_waits_or_inherits(github: GitHub, receipt: bool) -> None:
    if receipt:
        _images(github, release_conclusion="success")
        earlier = github.base
        _squash_with_pending_main(github)
    else:
        earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, conclusion="cancelled")
    _main_image_run(github, 60, github.base, status="in_progress", conclusion=None)
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [github.api[_MAIN_IMAGES]["workflow_runs"][0]]
    }
    github.save()

    status = github.workflow.run_script("main-status")

    expected = "green" if receipt else "pending"
    assert status.stdout.startswith(f"main: {expected}"), status.stdout + status.stderr
    if receipt:
        assert "Docker images run 40" in status.stdout
    else:
        assert github.run().returncode == 1
        assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("newer", ["none", "push", "schedule", "pull_request", "unrelated"])
@pytest.mark.parametrize("job_conclusion", ["cancelled", "failure"])
def test_only_superseded_image_cancellations_are_pending_and_never_retried(
    github: GitHub, newer: str, job_conclusion: str
) -> None:
    # Keep the cancelled run on current main so the existing main-moved retry guard
    # cannot make this test pass by itself.
    _main_image_run(github, 50, github.base, conclusion="cancelled")
    cancelled = github.api[_MAIN_IMAGES]["workflow_runs"][0]
    cancelled["run_attempt"] = 1
    parts = github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"]["jobs"]
    parts[1].update(conclusion=job_conclusion, labels=["example-app-home-docker"])
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [cancelled]
    }
    if newer != "none":
        _main_image_run(
            github,
            60,
            "f" * 40 if newer == "unrelated" else github.base,
            status="in_progress",
            conclusion=None,
            event=newer if newer in ("schedule", "pull_request") else "push",
        )
        if newer == "push":
            github.api[
                f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
            ]["workflow_runs"].insert(0, github.api[_MAIN_IMAGES]["workflow_runs"][0])
    github.save()

    status = github.workflow.run_script("main-status")
    expected = "pending" if newer == "push" and job_conclusion == "cancelled" else "red"
    assert status.stdout.startswith(f"main: {expected}"), status.stdout + status.stderr
    github.workflow.run_script("tick")
    rerun_calls = [call for call in github.workflow.calls("gh") if "run rerun" in call]
    if newer == "push":
        assert not rerun_calls
        assert not (github.workflow.repo / ".local/workflow/main-rerun-50").exists()
    else:
        assert rerun_calls == ["gh run rerun 50 -R example-org/example-app --failed"]


def test_an_earlier_green_image_run_does_not_stand_in_for_the_heads_own(github: GitHub) -> None:
    workflow = github.workflow
    earlier = github.base
    workflow.git("checkout", "main")
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: main image input")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head)
    _main_image_run(github, 50, earlier)
    github.save()

    assert "main: pending, Docker images have not started" in (
        workflow.run_script("main-status").stdout
    )
    assert github.run().returncode == 1


def test_scheduled_and_unrelated_image_runs_do_not_decide_main(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier)
    _main_image_run(github, 60, earlier, conclusion="failure", event="schedule")
    # A run of a commit that is not in main's history (an old force-push) proves nothing.
    _main_image_run(github, 70, "f" * 40, conclusion="failure")
    _main_image_run(github, 80, earlier, conclusion="failure", event="pull_request")
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert github.run().returncode == 0


def test_a_failed_image_run_on_a_later_page_still_keeps_main_red(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    first = github.api[_MAIN_IMAGES]
    github.api[_MAIN_IMAGES.replace("&page=1", "&page=2")] = first
    # A hundred nightly runs fill the first page and push the failure onto the second.
    github.api[_MAIN_IMAGES] = {
        "workflow_runs": [
            {
                "id": 1000 - night,
                "head_sha": github.base,
                "head_branch": "main",
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
            }
            for night in range(100)
        ]
    }
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: red")
    assert github.run().returncode == 1


def test_a_fix_whose_images_passed_the_check_failing_on_main_merges(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    _images(github)
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"].append(
        {
            "id": 44,
            "name": "Docker images: browser tests (1 of 2)",
            "status": "completed",
            "conclusion": "success",
        }
    )

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        "passed every check failing on main: Docker images: browser tests (1 of 2)" in done.stdout
    )


def test_a_rerun_in_progress_of_mains_latest_image_run_holds_main_pending(
    github: GitHub,
) -> None:
    # The re-run's first attempt was main's latest completed image run: the older failure it
    # replaced must not come back while it runs again, nor let a "fix" merge meanwhile.
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, conclusion="failure")
    _main_image_run(github, 60, github.base, status="in_progress", conclusion=None)
    head_run = github.api[_MAIN_IMAGES]["workflow_runs"][0]
    head_run["run_attempt"] = 2
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [head_run]
    }
    _images(github)
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"].append(
        {
            "id": 44,
            "name": "Docker images: browser tests (1 of 2)",
            "status": "completed",
            "conclusion": "success",
        }
    )
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: pending"), status.stdout
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_an_older_image_run_running_again_behind_a_newer_completed_one_does_not_hold_main(
    github: GitHub,
) -> None:
    # The newer completed run decides main; whatever the older one finds, it cannot change that.
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, status="in_progress", conclusion=None)
    github.api[_MAIN_IMAGES]["workflow_runs"][0]["run_attempt"] = 2
    _main_image_run(github, 60, earlier)
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert github.run().returncode == 0


def test_the_tick_reports_a_failed_image_run_after_a_push_without_images(github: GitHub) -> None:
    workflow = github.workflow
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    github.save()
    workflow.orchestrator()

    done = workflow.run_script("tick", env={"TZ": "UTC", "WORKFLOW_NOW": "1792152000"})

    assert done.returncode == 0, done.stderr
    [message] = workflow.sent()
    assert "main: red since" in message
    assert "Docker images: browser tests (1 of 2)" in message


@pytest.mark.parametrize(
    "filename", ["Dockerfile", "é.txt", "tab\tname.txt", "line\nname.txt", "raw\udcff.txt"]
)
@pytest.mark.parametrize("initial_push", [False, True])
@pytest.mark.parametrize("rerun", [False, True])
def test_main_waits_for_required_images_of_the_whole_push(
    github: GitHub, filename: str, initial_push: bool, rerun: bool
) -> None:
    workflow = github.workflow
    before = "0" * 40 if initial_push else github.base
    workflow.git("config", "core.quotePath", "true")
    workflow.git("checkout", "main")
    image_input = workflow.repo / "docker" / filename
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: main image input")
    # A push can contain more than one commit. Its last commit alone misses this image input.
    workflow.git("commit", "--allow-empty", "-m", "chore: next commit in the same push")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main()
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"].append(
        {"id": 13, "name": "CI: what to run", "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/13/annotations?per_page=100"] = [
        {"title": "Push base", "message": before}
    ]
    if rerun:
        # Main's CI re-ran its failed jobs alone and carried its planner without the note.
        _rerun_failed_jobs(github, 10, github.base, "CI: what to run")
    github.save()

    status = workflow.run_script("main-status")
    refused = github.run()

    assert "main: pending, Docker images" in status.stdout
    assert refused.returncode == 1
    assert not (workflow.state / "merged").exists()
    _images(github, pull=False)
    github.save()
    assert "main: green" in workflow.run_script("main-status").stdout


def test_a_scheduled_image_run_in_progress_does_not_hold_main(github: GitHub) -> None:
    _images(github, pull=False)
    _scheduled_images(github)
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: green"), status.stdout
    assert done.returncode == 0, done.stderr


def test_a_failed_scheduled_image_run_is_reported_once_and_does_not_close_the_gate(
    github: GitHub,
) -> None:
    folder = github.workflow.repo / ".local/workflow"
    _images(github, pull=False)
    _scheduled_images(github, status="completed", conclusion="failure")
    github.save()

    status = github.workflow.run_script("main-status")

    assert status.stdout.startswith("main: green"), status.stdout
    assert "scheduled Docker images run 50 failed" in status.stdout
    assert "Docker images: browser tests (1 of 2)" in status.stdout
    report = (folder / "main-status.pending").read_text()
    assert "scheduled Docker images run 50 failed" in report
    (folder / "main-status.pending").unlink()
    github.workflow.run_script("main-status")
    assert not (folder / "main-status.pending").exists()
    assert github.run().returncode == 0


def test_main_still_waits_for_the_image_run_of_its_push(github: GitHub) -> None:
    _images(github, pull=False)
    pushed = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"][0]
    pushed.update(status="in_progress", conclusion=None)
    _scheduled_images(github, status="completed", conclusion="success")
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert "main: pending" in status.stdout, status.stdout
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("broken", ["", "tree", "branch", "matrix"])
def test_gate_verifies_reused_image_evidence_before_merging(github: GitHub, broken: str) -> None:
    _images(github)
    current = github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"]
    current[1]["conclusion"] = "skipped"
    current.append(
        {
            "id": 44,
            "name": "Docker images: browser tests",
            "status": "completed",
            "conclusion": "skipped",
        }
    )
    github.api[f"{_REPO}/check-runs/41/annotations?per_page=100"].append(
        {"title": "Inherited image run", "message": "80"}
    )
    github.api[f"{_REPO}/actions/runs/80"] = {
        "id": 80,
        "event": "pull_request",
        "path": ".github/workflows/images.yml",
        "head_branch": "other" if broken == "branch" else "ticket/seven",
        "status": "completed",
        "conclusion": "success",
    }
    original = [
        {
            "id": 81,
            "name": "Docker images: what to run",
            "status": "completed",
            "conclusion": "success",
        },
        {
            "id": 82,
            "name": "Docker images: image tests",
            "status": "completed",
            "conclusion": "success",
        },
        *[
            {
                "id": 82 + i,
                "name": f"Docker images: browser tests ({i} of 3)",
                "status": "completed",
                "conclusion": "success",
            }
            for i in range(1, 3 if broken == "matrix" else 4)
        ],
    ]
    github.api[f"{_REPO}/actions/runs/80/jobs?per_page=100"] = {"jobs": original}
    github.api[f"{_REPO}/check-runs/81/annotations?per_page=100"] = [
        {
            "title": "Files tested",
            "message": "other"
            if broken == "tree"
            else github.workflow.git("rev-parse", f"{github.head}^{{tree}}"),
        }
    ]
    done = github.run()
    assert done.returncode == (1 if broken else 0), done.stdout + done.stderr
    assert (github.workflow.state / "merged").exists() is (not broken)
