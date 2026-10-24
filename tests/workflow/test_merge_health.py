"""Merge safety for health."""

import fcntl
import json
import subprocess
from copy import deepcopy
from typing import Any

import pytest

from workflow.merge_gate_fixtures import (
    GitHub,
    UNREACHABLE,
    _ADVISORIES_ONLY,
    _ADVISORY_LINES,
    _AUDIT,
    _DEPENDENCIES,
    _GH,
    _MAIN_UNREACHABLE,
    _REPO,
    _advisories_notice,
    _advisory_config,
    _advisory_watch,
    _dependencies_red,
    _images,
    _main_audit_failed,
    _pr_job,
    _pull_passed_over_mains_advisory,
    _scheduled_images,
    _train,
    _two_stale_pulls,
    advisory_service_answers,
    gate,
    github,
    pull_dependencies_red,
    rerun_calls,
)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_a_red_main_refuses_to_merge_and_reports_why(github: GitHub, conclusion: str) -> None:
    github.main(conclusion)

    done = github.run()

    assert done.returncode == 1
    assert "main: red since 2026-10-16T09:00:00Z, CI result, CI: web" in done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert "main: red" in (github.workflow.repo / ".local/workflow/main-status").read_text()


def test_main_failing_only_on_an_advisory_lets_a_green_pull_request_merge(
    github: GitHub,
) -> None:
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice())
    _pull_passed_over_mains_advisory(github)

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert "main: advisory since 2026-10-16T09:00:00Z" in done.stdout


def test_main_failing_on_another_check_besides_an_advisory_still_stops(github: GitHub) -> None:
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice(), others=("CI: web",))
    _pull_passed_over_mains_advisory(github)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize(
    "notes",
    [
        # A lock file it could not audit, or a tool that failed: no advisories notice.
        [],
        [{"title": "Advisory service unreachable", "annotation_level": "failure", "message": "npm"}],
        [{"title": None, "annotation_level": "failure", "message": "Process completed with exit code 2."}],
        # A notice that names no advisory, or cannot be read, proves nothing.
        [_advisories_notice([])],
        [{"title": _ADVISORIES_ONLY, "annotation_level": "notice", "message": "not a list"}],
    ],
    ids=["no notes", "service", "tool error", "empty notice", "unreadable notice"],
)
def test_an_audit_failing_for_another_reason_than_an_advisory_still_stops(
    github: GitHub, notes: list[dict[str, Any]]
) -> None:
    _advisory_config(github)
    _main_audit_failed(github, *notes)
    _pull_passed_over_mains_advisory(github)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_without_the_audit_named_an_advisory_on_main_still_stops(github: GitHub) -> None:
    _main_audit_failed(github, _advisories_notice(), audit=_DEPENDENCIES)
    _pull_passed_over_mains_advisory(github, _DEPENDENCIES)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert "advisory" not in github.workflow.run_script("main-status").stdout.split(";")[0]
    assert _train(github).get("main") == "red"


def test_the_main_line_and_the_train_status_name_the_advisory_state(github: GitHub) -> None:
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice())
    folder = github.workflow.repo / ".local/workflow"

    status = github.workflow.run_script("main-status")

    assert status.returncode == 0, status.stderr
    assert status.stdout == (
        f"main: advisory since 2026-10-16T09:00:00Z, CI result, {_AUDIT} ({github.base[:12]}): "
        "GHSA-1 in vite (path traversal), GHSA-2 in esbuild (open dev server); "
        "merging goes on, the update that fixes it goes first\n"
    )
    # Into the advisory state is told in the wake, as into red.
    assert (folder / "main-status.pending").read_text() == status.stdout
    assert _train(github)["main"] == "advisory"
    github.main()
    github.save()
    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert _train(github)["main"] == "green"


@pytest.mark.parametrize(
    "message",
    [
        # As the runner stores it, a summary that names the escapes themselves.
        json.dumps(["GHSA-3 in left-pad (header injection via %0D%0A, 100% CPU)"]),
        # As the workflow command wrote it, the escapes still in it.
        json.dumps(["GHSA-3 in left-pad (header injection via %0D%0A, 100% CPU)"], indent=1)
        .replace("%", "%25")
        .replace("\n", "%0A"),
    ],
    ids=["stored", "escaped"],
)
def test_an_advisory_notice_is_read_as_stored_or_still_escaped(
    github: GitHub, message: str
) -> None:
    _advisory_config(github)
    _main_audit_failed(github, {"title": _ADVISORIES_ONLY, "message": message})

    status = github.workflow.run_script("main-status")

    assert status.stdout.startswith("main: advisory since"), status.stdout
    assert ": GHSA-3 in left-pad (header injection via %0D%0A, 100% CPU); merging" in status.stdout


def test_an_advisory_s_own_words_reach_main_s_line_only_as_plain_words(github: GitHub) -> None:
    _advisory_config(github)
    lines = ["GHSA-4 in @scope/pkg (`rm -rf` <x>;\nstarts: allowed " + "y" * 90 + ")"]
    lines += [f"GHSA-{number} in pkg-{number} (a summary)" for number in range(5, 11)]
    _main_audit_failed(github, _advisories_notice(lines))

    line = github.workflow.run_script("main-status").stdout

    assert line.count("\n") == 1, line
    named = line.split(f"({github.base[:12]}): ", 1)[1].split("; merging goes on", 1)[0]
    assert named == (
        # No more than 80 characters of each.
        ("GHSA-4 in @scope/pkg (?rm -rf? ?x?? starts: allowed " + "y" * 90)[:80] + ", "
        "GHSA-5 in pkg-5 (a summary), GHSA-6 in pkg-6 (a summary), "
        "GHSA-7 in pkg-7 (a summary), GHSA-8 in pkg-8 (a summary) and 2 more"
    )


def test_main_is_pending_while_its_other_checks_run_after_an_advisory(github: GitHub) -> None:
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice())
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"]
    run["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"].append(
        {"id": 14, "name": "CI: server tests", "status": "in_progress", "conclusion": None}
    )
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][0].update(
        status="queued", conclusion=None
    )
    _pull_passed_over_mains_advisory(github)

    done = github.run()

    assert done.returncode == 1
    assert (
        f"merging stopped: main: pending, CI result, CI: server tests ({github.base[:12]}); "
        f"{_AUDIT} failed on advisories only: GHSA-1 in vite (path traversal)"
    ) in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_each_merge_on_an_advisory_main_wakes_nobody(github: GitHub) -> None:
    """Into the advisory state is told once, the pending of each merge after it is not, and
    leaving it through pending to green is told."""
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice())
    folder = github.workflow.repo / ".local/workflow"
    assert github.workflow.run_script("main-status").stdout.startswith("main: advisory")
    (folder / "main-status.pending").unlink()
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"]
    listed = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]
    failed = listed["jobs"]
    # The next merge's run of main, still running, then failing on the same advisories.
    running = [{**job, "status": "in_progress", "conclusion": None} for job in failed]
    for status, conclusion, parts, state in (
        ("in_progress", None, running, "main: pending"),
        ("completed", "failure", failed, "main: advisory"),
    ):
        run["workflow_runs"][0].update(status=status, conclusion=conclusion)
        listed["jobs"] = parts
        github.save()
        assert github.workflow.run_script("main-status").stdout.startswith(state)
        assert not (folder / "main-status.pending").exists()
    # The pending line it published said that main stays advisory.
    assert (folder / "main-status").read_text().startswith("main: advisory")

    # The update lands: main's run on it goes on for a while, then passes. Leaving the advisory
    # state is told once main is green.
    run["workflow_runs"][0].update(status="in_progress", conclusion=None)
    listed["jobs"] = running
    github.save()
    assert github.workflow.run_script("main-status").stdout.startswith("main: pending")
    assert not (folder / "main-status.pending").exists()
    assert (folder / "main-status").read_text().endswith("; still advisory\n")
    github.main()
    github.save()
    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert (folder / "main-status.pending").read_text().startswith("main: green")


def test_an_advisory_main_published_before_its_state_was_kept_wakes_nobody_at_a_merge(
    github: GitHub,
) -> None:
    """A main already advisory, published by a gate that did not keep the state through pending:
    its next merge is not told either."""
    _advisory_config(github)
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main-status").write_text(
        "main: advisory since 2026-10-16T08:00:00Z, CI result, CI: audit (0123456789ab): "
        "GHSA-1 in vite (path traversal); merging goes on, the update that fixes it goes first\n"
    )
    github.main()
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"]
    run["workflow_runs"][0].update(status="in_progress", conclusion=None)
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job.update(status="in_progress", conclusion=None)
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: pending")
    assert not (folder / "main-status.pending").exists()


def test_an_outage_beside_mains_advisories_is_red_and_says_what_was_found(
    github: GitHub,
) -> None:
    _advisory_config(github)
    _advisory_watch(github)
    outage = {
        "title": "Advisory service unreachable",
        "annotation_level": "failure",
        "message": "Refused because PyPI's advisory service could not be reached.",
    }
    _main_audit_failed(github, outage, _advisories_notice(_ADVISORY_LINES[:1]))

    line = github.workflow.run_script("main-status").stdout

    assert line.startswith("main: red"), line
    assert (
        f"; {_AUDIT} could not reach PyPI's advisory service, and found advisories: "
        "GHSA-1 in vite (path traversal)"
    ) in line
    assert "nothing was found wrong" not in line


def test_an_advisory_watch_that_fails_beside_mains_advisories_keeps_main_red(
    github: GitHub,
) -> None:
    """A watch that could not read the audit's annotations proves no lock file was audited whole:
    main is not advisory, and merging stops."""
    _advisory_config(github)
    _advisory_watch(github)
    github.workflow.fake("example-advisory", "#!/bin/bash\necho 'cannot read' >&2\nexit 1\n")
    outage = {
        "title": "Advisory service unreachable",
        "annotation_level": "failure",
        "message": "Refused because PyPI's advisory service could not be reached.",
    }
    _main_audit_failed(github, outage, _advisories_notice(_ADVISORY_LINES[:1]))
    _pull_passed_over_mains_advisory(github)

    line = github.workflow.run_script("main-status").stdout
    done = github.run()

    assert line.startswith("main: red"), line
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_an_advisory_whose_own_words_name_an_outage_is_still_advisories_alone(
    github: GitHub,
) -> None:
    """A published advisory's summary may say anything; only the audit's other annotations are
    read for an outage."""
    _advisory_config(github)
    _advisory_watch(github)
    lines = ["GHSA-5 in example-lib (fails when npm's advisory service could not be reached)"]
    _main_audit_failed(github, _advisories_notice(lines))

    line = github.workflow.run_script("main-status").stdout

    assert line.startswith("main: advisory since"), line
    assert "could not reach" not in line


@pytest.mark.parametrize("warned", [True, False])
def test_the_update_that_fixes_mains_advisory_is_a_blocker_and_goes_first(
    github: GitHub, warned: bool
) -> None:
    _advisory_config(github)
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": _AUDIT, "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = (
        [{"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}]
        if warned
        else []
    )
    _main_audit_failed(github, _advisories_notice())
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))

    assert github.workflow.run_script("main-status").returncode == 0

    train = _train(github)
    # Only a clean audit is the update: a pass over main's own advisory is not.
    assert train["first"] == ("7" if warned else "8")
    assert (train["priorities"]["8"] == "blocker") is not warned


@pytest.mark.parametrize("warned", [True, False])
def test_the_update_tested_on_an_earlier_main_is_the_fix_only_with_a_clean_audit(
    github: GitHub, warned: bool
) -> None:
    """Main moved under the update while the line kept merging: its latest green run with a
    clean audit proves it, whatever main that run tested."""
    _advisory_config(github)
    earlier = github.base
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30, tested_base=earlier)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": _AUDIT, "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = (
        [{"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}]
        if warned
        else []
    )
    _main_audit_failed(github, _advisories_notice())
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))

    assert github.workflow.run_script("main-status").returncode == 0

    train = _train(github)
    assert train["first"] == ("7" if warned else "8")
    assert (train["priorities"]["8"] == "blocker") is not warned


def test_a_stale_pull_request_is_refreshed_on_a_red_main(github: GitHub) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main("failure")
    github.ci(github.base, github.head, tested_base=old)
    _pr_job(github, "CI: web", "success")

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    assert not (workflow.state / "merged").exists()


def test_merging_goes_on_after_main_is_green_again(github: GitHub) -> None:
    github.main("failure")
    assert github.run().returncode == 1
    github.main()

    done = github.run()

    assert done.returncode == 0, done.stderr
    assert (github.workflow.state / "merged").exists()


def test_missing_ci_on_main_is_never_read_as_green(github: GitHub) -> None:
    github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": []
    }
    done = github.run()
    assert done.returncode == 1
    assert "main: pending" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_red_main_report_names_commits_since_last_green(github: GitHub) -> None:
    workflow = github.workflow
    green = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: first incompatible change")
    workflow.git("commit", "--allow-empty", "-m", "fix: second incompatible change")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main("failure")
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=100"
    ] = {"workflow_runs": [{"id": 5, "head_sha": green}]}
    github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={green}&per_page=100"] = {
        "workflow_runs": [
            {
                "id": 5,
                "head_sha": green,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    github.api[f"{_REPO}/actions/runs/5/jobs?per_page=100"] = {
        "jobs": [{"id": 6, "name": "CI result", "status": "completed", "conclusion": "success"}]
    }
    github.save()

    done = workflow.run_script("main-status")

    assert done.returncode == 0
    assert f"commits since last green main {green[:12]}" in done.stdout
    assert "fix: first incompatible change;" in done.stdout
    assert "fix: second incompatible change" in done.stdout


def test_the_tick_reports_a_red_main_through_the_wake_during_the_day(github: GitHub) -> None:
    workflow = github.workflow
    github.main("failure")
    github.save()
    workflow.orchestrator()

    done = workflow.run_script("tick", env={"TZ": "UTC", "WORKFLOW_NOW": "1792152000"})

    assert done.returncode == 0, done.stderr
    assert "main: red" in done.stdout
    [message] = workflow.sent()
    assert "CI: web" in message and "main: red since" in message
    assert "last green main" in message
    assert not (workflow.repo / ".local/workflow/main-status.pending").exists()


def test_only_a_red_or_unreadable_main_and_its_recovery_wake_the_orchestrator(
    github: GitHub,
) -> None:
    folder = github.workflow.repo / ".local/workflow"
    pending = folder / "main-status.pending"
    gate = github.workflow.repo / "scripts/workflow/merge_gate.py"
    published: dict[str, bool] = {}
    for line in [
        "main: green (aaaaaaaaaaaa)",
        "main: pending, CI result has not started (bbbbbbbbbbbb)",
        "main: green (bbbbbbbbbbbb)",
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc)",
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc); history read",
        "main: unknown, gh failed",
        "main: pending, CI result (dddddddddddd)",
        "main: green (dddddddddddd)",
    ]:
        pending.unlink(missing_ok=True)
        state = line.split(",")[0].split(" ")[1]
        subprocess.run(
            [
                "python3",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
                "from pathlib import Path; "
                "g.publish(Path(sys.argv[2]), g.Health(sys.argv[3], sys.argv[4]))",
                str(gate.parent),
                str(folder),
                state,
                line,
            ],
            check=True,
        )
        published[line] = pending.exists()

    assert published == {
        "main: green (aaaaaaaaaaaa)": False,
        "main: pending, CI result has not started (bbbbbbbbbbbb)": False,
        "main: green (bbbbbbbbbbbb)": False,
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc)": True,
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc); history read": False,
        "main: unknown, gh failed": True,
        "main: pending, CI result (dddddddddddd)": True,
        "main: green (dddddddddddd)": False,
    }


def test_a_manual_rerun_of_main_ci_counts_and_a_scheduled_one_does_not(github: GitHub) -> None:
    endpoint = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    pushed = github.api[endpoint]["workflow_runs"][0]
    pushed["conclusion"] = "failure"
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    for run_id, event in ((14, "workflow_dispatch"), (16, "schedule")):
        conclusion = "success" if event == "workflow_dispatch" else "failure"
        github.api[endpoint]["workflow_runs"].append(
            {**pushed, "id": run_id, "event": event, "conclusion": conclusion}
        )
        github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {
            "jobs": [
                {
                    "id": run_id + 1,
                    "name": "CI result",
                    "status": "completed",
                    "conclusion": conclusion,
                }
            ]
        }
    github.save()

    status = github.workflow.run_script("main-status")

    assert status.stdout.startswith("main: green"), status.stdout


def test_each_new_scheduled_failure_is_reported_and_an_older_one_is_not(github: GitHub) -> None:
    pending = github.workflow.repo / ".local/workflow/main-status.pending"
    _images(github, pull=False)
    _scheduled_images(github, status="completed", conclusion="failure")
    github.save()
    github.workflow.run_script("main-status")
    pending.unlink()

    runs = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"]
    runs.append({**runs[-1], "id": 60})
    github.api[f"{_REPO}/actions/runs/60/jobs?per_page=100"] = github.api[
        f"{_REPO}/actions/runs/50/jobs?per_page=100"
    ]
    github.save()
    github.workflow.run_script("main-status")
    assert "scheduled Docker images run 60 failed" in pending.read_text()
    pending.unlink()

    runs.append({**runs[-1], "id": 70, "conclusion": "success"})
    github.api[f"{_REPO}/actions/runs/70/jobs?per_page=100"] = {
        "jobs": [
            {
                "id": 71,
                "name": "Docker images: what to run",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green") and "scheduled" not in status.stdout
    assert not pending.exists()


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure"])
def test_tick_reruns_failed_home_jobs_once_and_reports_it(github: GitHub, conclusion: str) -> None:
    workflow = github.workflow
    github.main(conclusion)
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["self-hosted", "example-app-home-main"]
    github.save()

    done = workflow.run_script("tick")

    rerun_calls = [call for call in workflow.calls("gh") if "run rerun" in call]
    assert rerun_calls == ["gh run rerun 10 -R example-org/example-app --failed"], done.stderr
    note = "automatically re-ran failed jobs of main run 10 once"
    assert note in workflow.wake_log.read_text()
    assert note in (workflow.repo / ".local/workflow/main-status").read_text()
    assert "main: red" in (workflow.repo / ".local/workflow/main-status").read_text()
    # A merge-gate health refresh must preserve the note without another retry.
    assert note in workflow.run_script("main-status").stdout
    # GitHub can still return attempt 1 for a tick after accepting the request.
    workflow.run_script("tick")
    assert len([call for call in workflow.calls("gh") if "run rerun" in call]) == 1
    run["run_attempt"] = 2
    github.save()
    workflow.run_script("tick")
    assert len([call for call in workflow.calls("gh") if "run rerun" in call]) == 1


@pytest.mark.parametrize("first", ["example-app-home-main", "ubuntu-latest"])
def test_tick_reruns_a_failed_part_only_when_every_part_ran_at_home(
    github: GitHub, first: str
) -> None:
    workflow = github.workflow
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": "CI: server tests (1 of 2)", "status": "completed", "conclusion": "success"},
        {"id": 13, "name": "CI: server tests (2 of 2)", "status": "completed", "conclusion": "failure"},
    ]
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["self-hosted", "example-app-home-main"]
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["labels"] = [first]
    github.save()

    done = workflow.run_script("tick")

    reruns = [call for call in workflow.calls("gh") if "run rerun" in call]
    expected = ["gh run rerun 10 -R example-org/example-app --failed"]
    assert reruns == (expected if first == "example-app-home-main" else []), done.stderr
    assert "main: red" in (workflow.repo / ".local/workflow/main-status").read_text()


@pytest.mark.parametrize(
    "reason",
    [
        "second attempt",
        "in progress",
        "hosted",
        "unknown runner",
        "unknown attempt",
        "pull request",
    ],
)
def test_tick_does_not_rerun_ineligible_main_jobs(github: GitHub, reason: str) -> None:
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 2 if reason == "second attempt" else 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home-main"]
    if reason == "in progress":
        run["status"] = "in_progress"
    elif reason == "hosted":
        github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["labels"] = [
            "ubuntu-latest"
        ]
    elif reason == "unknown runner":
        github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1].pop("labels")
    elif reason == "unknown attempt":
        run.pop("run_attempt")
    elif reason == "pull request":
        run["event"] = "pull_request"
    github.save()
    github.workflow.run_script("tick")
    assert not [call for call in github.workflow.calls("gh") if "run rerun" in call]


@pytest.mark.parametrize("mode", ["read only", "request refused", "images"])
def test_main_retry_keeps_its_scope_and_reports_refusals(github: GitHub, mode: str) -> None:
    workflow = github.workflow
    github.main("failure")
    run_path = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    run = github.api[run_path]["workflow_runs"][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home-docker"]
    if mode == "images":
        github.api[run_path.replace("ci.yml", "images.yml")] = {"workflow_runs": [deepcopy(run)]}
        github.api[run_path] = {"workflow_runs": []}
    elif mode == "request refused":
        (workflow.state / "fail-rerun").touch()
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main-status").write_text("main: red since earlier\n")

    if mode == "read only":
        workflow.run_script("main-status")
        assert not [call for call in workflow.calls("gh") if "run rerun" in call]
        return
    workflow.run_script("tick")
    pending = (folder / "main-status.pending").read_text()
    assert "main run 10" in pending
    assert "main run 10" in workflow.wake_log.read_text()
    if mode == "request refused":
        assert "refused" in pending
        workflow.run_script("tick")
    assert len([call for call in workflow.calls("gh") if "run rerun" in call]) == 1


def test_a_failed_home_planner_cannot_authorize_hosted_dependents(github: GitHub) -> None:
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    run["event"] = "workflow_dispatch"
    parts = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]
    parts[0].update(name="CI: what to run", labels=["home-planner"])
    parts[1].update(name="CI result", labels=["example-app-home"])
    # A failed planner has not chosen where its skipped dependents will run. A manual
    # run may request machine=github; retrying that home planner would spend minutes.
    parts.append({"id": 13, "name": "CI: web", "status": "completed", "conclusion": "skipped"})
    github.save()
    github.workflow.run_script("tick")
    assert not [call for call in github.workflow.calls("gh") if "run rerun" in call]


def test_tick_does_not_retry_while_a_merge_gate_holds_its_lock(github: GitHub) -> None:
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home"]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        github.workflow.run_script("tick")
    assert not [call for call in github.workflow.calls("gh") if "run rerun" in call]


def test_tick_does_not_retry_a_head_that_left_main_during_the_github_read(github: GitHub) -> None:
    workflow = github.workflow
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home"]
    github.next_main = workflow.git(
        "commit-tree",
        workflow.git("rev-parse", f"{github.base}^{{tree}}"),
        "-p",
        github.base,
        "-m",
        "fix: main moved",
    )
    workflow.git("push", "origin", f"{github.next_main}:refs/heads/future-main")
    workflow.fake(
        "gh.py",
        _GH.replace(
            '        reply = data["api"][path]',
            """        if path.endswith("/actions/runs/10/jobs?per_page=100"):
            run(["git", "--git-dir", data["origin"], "update-ref",
                 "refs/heads/main", data["next_main"]], check=True)
        reply = data["api"][path]""",
        ),
    )
    github.save()
    workflow.run_script("tick")
    assert workflow.git("ls-remote", "origin", "refs/heads/main").split()[0] == github.next_main
    assert not [call for call in workflow.calls("gh") if "run rerun" in call]


def test_a_red_line_that_only_mentions_automatic_is_not_a_retry_report(github: GitHub) -> None:
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["name"] = (
        "CI: automatic checks"
    )
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main-status").write_text("main: red since earlier, CI result (abc)\n")

    github.workflow.run_script("tick")

    assert "main: red" in (folder / "main-status").read_text()
    # Red to red is not news; only a retry note of this tick's own is.
    assert not (folder / "main-status.pending").exists()


def test_main_red_for_the_audit_service_and_its_images_does_not_say_nothing_was_wrong(
    github: GitHub,
) -> None:
    _dependencies_red(github, annotation=_MAIN_UNREACHABLE)
    _images(github, pull=False, conclusion="failure")
    github.save()
    advisory_service_answers(github.workflow, answers=False)

    line = github.workflow.run_script("main-status").stdout

    assert "main: red" in line and "Docker images: image tests" in line
    assert "nothing was found wrong with main" not in line


@pytest.mark.parametrize("reason", ["no annotation", "another check failed too"])
def test_a_pull_request_red_for_more_than_the_audit_service_is_refused(
    github: GitHub, reason: str
) -> None:
    pull_dependencies_red(github, annotation=None if reason == "no annotation" else UNREACHABLE)
    if reason == "another check failed too":
        github.pulls["7"]["statusCheckRollup"].append(
            {"name": "CI: web", "status": "COMPLETED", "conclusion": "FAILURE"}
        )
        github.save()

    done = github.run()

    assert done.returncode == 1 and "merge refused" in done.stderr, done.stdout + done.stderr
    assert "failed checks" in done.stderr
