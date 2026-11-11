"""`wake` gives the orchestrator session a check-in from outside it, and writes what it did."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workflow_harness import Workflow, install_shipline
from workflow.test_usage import _ANSWER, _usage_answer
from workflow.test_ci_proof import _github, _python, _receipt, _API, _SQUASH, _TREE

_NEW_YORK = ZoneInfo("America/New_York")


def _at(hour: int, minute: int = 0, day: int = 13) -> dict[str, str]:
    """The clock of the wake, read by the machine's clock in New York."""
    moment = datetime(2026, 10, day, hour, minute, tzinfo=_NEW_YORK)
    return {"WORKFLOW_NOW": str(int(moment.timestamp())), "TZ": "America/New_York"}


@pytest.fixture
def night(workflow: Workflow) -> Workflow:
    """Two sessions run, three pull requests are open, the usage is known."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.add_pull_request(9, draft=False)
    workflow.add_pull_request(12, labels=())
    workflow.usage(76, "2026-10-14T05:10:00+00:00", seven=41)
    return workflow


def _wake(
    workflow: Workflow, *woken: str, at: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return workflow.run_script("wake", *woken, env=at or _at(23, 0))


@pytest.mark.parametrize("git_environment", [False, True])
def test_check_in_names_the_installed_shipline_commit(night: Workflow, git_environment: bool) -> None:
    home = install_shipline(night)
    sha = subprocess.run(
        ["git", "-C", str(home), "rev-parse", "--short", "HEAD"],
        env=night.env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    night.orchestrator("idle")
    env = _at(23, 0)
    if git_environment:
        env.update(GIT_DIR=str(night.repo / ".git"), GIT_WORK_TREE=str(night.repo))
    done = _wake(night, at=env)
    assert done.returncode == 0, done.stderr
    assert f"shipline: {sha}" in night.sent()[-1]
    subprocess.run(
        ["git", "-C", str(home), "commit", "--quiet", "--allow-empty", "-m", "fix: next"],
        env=night.env, capture_output=True, text=True, check=True,
    )
    next_sha = subprocess.run(
        ["git", "-C", str(home), "rev-parse", "--short", "HEAD"],
        env=night.env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    done = _wake(night, at=_at(23, 21))
    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == 2
    assert f"shipline: {next_sha}" in night.sent()[-1]


def test_git_repair_is_delivered_once_in_daylight_even_when_busy(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/git-repair.pending"
    pending.mkdir(parents=True)
    line = "git repository repair: moved 1 empty object files to .local/workflow/git-repairs/example"
    event = pending / "example.json"
    event.write_text(json.dumps({"line": line}))
    (night.state / "send-fails").touch()
    assert _wake(night, at=_at(12, 0)).returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    assert _wake(night, at=_at(12, 6)).returncode == 0
    assert line in night.sent()[-1]
    assert not event.exists()
    assert (night.repo / ".local/workflow/git-repair.sent/example.json").exists()
    sent = len(night.sent())
    _wake(night, at=_at(12, 12))
    assert len(night.sent()) == sent


def _savings_day(workflow: Workflow) -> None:
    """One merged PR, three ready/push/refresh runs, a duplicate ID and a skipped draft."""
    groups = ["images", "browser_whole", "server_whole"]
    (workflow.repo / "shipline.json").write_text(json.dumps({"checks": {"prefix_only": groups}}))
    sources = [
        {"workflow": "ci", "run": 20, "jobs": ["CI: server tests"]},
        {"workflow": "e2e", "run": 30, "jobs": ["playwright"]},
        {"workflow": "images", "run": 40, "jobs": ["Docker images: build", "Docker images: image tests"]},
    ]
    data = _github(_receipt(sources=sources))
    ok = {"status": "completed", "conclusion": "success"}
    data[f"{_API}/actions/runs/40"] = {
        **data[f"{_API}/actions/runs/30"], "id": 40, "path": ".github/workflows/images.yml",
    }
    data[f"{_API}/actions/runs/40/jobs?per_page=100"] = {"jobs": [
        {"id": 41, "name": "Docker images: what to run", **ok},
        {"id": 42, "name": "Docker images: build", **ok},
        {"id": 43, "name": "Docker images: image tests", **ok},
    ]}
    data[f"{_API}/check-runs/41/annotations?per_page=100"] = [{"title": "Files tested", "message": _TREE}]
    for run in (20, 30, 40):
        data[f"{_API}/actions/runs/{run}"].update(
            event="workflow_dispatch", head_branch="workflow/merge-batch-example")
    for run, durations in [(20, [0, 5, 5, 0, 0, 0]), (30, [0, 12]), (40, [0, 5, 7])]:
        for job, minutes in zip(data[f"{_API}/actions/runs/{run}/jobs?per_page=100"]["jobs"], durations):
            job.update(runner_name="example-runner", started_at="2026-10-12T10:00:00Z",
                       completed_at=f"2026-10-12T10:{minutes:02d}:00Z")
    data[f"{_API}/pulls/7"] = {
        "merged_at": "2026-10-12T18:00:00Z", "created_at": "2026-10-11T09:00:00Z",
        "merge_commit_sha": _SQUASH, "head": {"ref": "ticket/seven"}, "base": {"ref": "main"},
    }
    runs = []
    for run, deferred in [(100, groups), (200, ["images", "server_whole"]),
                          (300, ["browser_whole"]), (400, [])]:
        runs.append({"id": run, "head_branch": "ticket/seven", "event": "pull_request",
                     "created_at": "2026-10-12T11:00:00Z", "pull_requests": [{"number": 7}],
                     "head_repository": {"full_name": "example-org/example-app"}})
        data[f"{_API}/actions/runs/{run}/jobs?per_page=100"] = {"jobs": [
            {"id": run + 1, "name": "CI: what to run", **ok,
             "conclusion": "success" if deferred else "skipped"}
        ]}
        data[f"{_API}/check-runs/{run + 1}/annotations?per_page=100"] = [
            {"title": "Prefix-only groups", "message": json.dumps(deferred)}
        ]
    data["pr-runs"] = {"workflow_runs": [*runs, runs[0]]}
    fixture = workflow.state / "savings.json"
    fixture.write_text(json.dumps(data))
    workflow.fake("gh-before-savings", (workflow.bin / "gh").read_text())
    workflow.fake("gh", f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args and args[0] == "api":
    path = args[-1]
    data = json.loads(Path({str(fixture)!r}).read_text())
    if path.startswith("search/issues?"):
        print(json.dumps({{"total_count": 1, "items": [{{"number": 7}}]}}))
    elif "/actions/workflows/ci.yml/runs?branch=" in path:
        print(json.dumps(data["pr-runs"]))
    elif path in data:
        print(json.dumps(data[path]))
    else:
        sys.exit("unexpected savings API request")
else:
    os.execv({str(workflow.bin / "gh-before-savings")!r}, ["gh", *args])
''')


def test_daily_prefix_savings_from_merged_proof_counts_only_avoided_runs(night: Workflow) -> None:
    _savings_day(night)
    night.orchestrator("idle")
    done = _wake(night, at=_at(3, 0))
    assert done.returncode == 0, done.stderr
    line = ("Prefix-only runner minutes saved (2026-10-12 UTC): images 24.00; "
            "browser_whole 24.00; server_whole 20.00; total 68.00.")
    assert line in night.sent()[-1]


def test_a_shadow_wake_keeps_no_savings_report_and_the_real_wake_still_sends_it(
    night: Workflow,
) -> None:
    _savings_day(night)
    night.orchestrator("idle")
    shadow = night.root / "shadow.log"
    done = night.run_script("wake", env={**_at(3, 0), "SHIPLINE_SHADOW": str(shadow)})
    assert done.returncode == 0, done.stderr
    state = night.repo / ".local/workflow"
    assert not list(state.glob("prefix-savings*"))
    assert "Prefix-only runner minutes saved" not in shadow.read_text()
    assert _wake(night, at=_at(3, 0)).returncode == 0
    assert "saved (2026-10-12 UTC): images 24.00" in night.sent()[-1]


def test_daily_prefix_savings_retries_delivery_then_sends_once(night: Workflow) -> None:
    _savings_day(night)
    night.orchestrator("idle")
    (night.state / "send-fails").touch()
    first = _wake(night, at=_at(3, 0))
    assert first.returncode == 1
    (night.state / "send-fails").unlink()
    done = _wake(night, at=_at(3, 21))
    assert done.returncode == 0, done.stderr
    line = ("Prefix-only runner minutes saved (2026-10-12 UTC): images 24.00; "
            "browser_whole 24.00; server_whole 20.00; total 68.00.")
    assert line in night.sent()[-1]
    _wake(night, at=_at(3, 42))
    assert len(night.sent()) == 2
    assert (night.repo / ".local/workflow/prefix-savings.sent").read_text().strip() == "2026-10-12"


def test_an_unsent_daily_savings_report_survives_utc_midnight(night: Workflow) -> None:
    _savings_day(night)
    night.orchestrator("idle")
    (night.state / "send-fails").touch()
    # A reset check-in just before UTC midnight; ordinary night check-ins start afterward.
    assert _wake(night, "ticket-7", at=_at(19, 59)).returncode == 1
    pending = night.repo / ".local/workflow/prefix-savings-2026-10-12.pending"
    assert pending.exists()
    (night.state / "send-fails").unlink()
    done = _wake(night, at=_at(20, 5))
    assert done.returncode == 0, done.stderr
    assert "saved (2026-10-12 UTC): images 24.00" in night.sent()[-1]
    assert not pending.exists()
    sent = night.repo / ".local/workflow/prefix-savings.sent"
    assert sent.read_text().strip() == "2026-10-12"
    assert _wake(night, at=_at(20, 26)).returncode == 0
    assert "saved (2026-10-13 UTC): images 0.00" in night.sent()[-1]
    assert sent.read_text().strip() == "2026-10-13"
    assert _wake(night, at=_at(20, 47)).returncode == 0
    assert len(night.sent()) == 3


def test_daily_savings_resumes_completed_pulls_after_a_read_failure(night: Workflow) -> None:
    _savings_day(night)
    progress = night.state / "prefix-savings.progress"
    fixture = night.state / "savings.json"
    code = '''
        from pathlib import Path
        import json
        import prefix_savings as savings
        progress, fixture = map(Path, sys.argv[2:])
        data = json.loads(fixture.read_text())
        api = "repos/example-org/example-app"
        paths = []
        def read(path, key=None, **kwargs):
            paths.append(path)
            if path.startswith("search/issues?"):
                return {"total_count": 2, "items": [{"number": 7}, {"number": 8}]}
            if path == api + "/pulls/8":
                if not progress.exists() or "7" not in json.loads(progress.read_text())["done"]:
                    raise AssertionError("first PR was not checkpointed")
                if sys.argv[0] != "retry":
                    raise p.Unreadable("temporary read failure")
                return data[api + "/pulls/7"]
            if "/actions/workflows/ci.yml/runs?" in path:
                value = data["pr-runs"]
                # The same branch/run fixture, attributed to the current PR.
                value = json.loads(json.dumps(value))
                number = 8 if api + "/pulls/8" in paths else 7
                for run in value["workflow_runs"]:
                    run["pull_requests"] = [{"number": number}]
            else:
                value = data[path]
            return value[key] if key else value
        try:
            savings.report("2026-10-12", read, progress=progress)
        except p.Unreadable:
            pass
        assert json.loads(progress.read_text())["done"]["7"]["images"] == 24
        assert progress.stat().st_mode & 0o777 == 0o600
        sys.argv[0] = "retry"
        paths.clear()
        line = savings.report("2026-10-12", read, progress=progress)
        assert "images 48.00" in line and "total 136.00" in line
        assert not any(path.startswith("search/issues?") or path == api + "/pulls/7" for path in paths)
    '''
    done = _python(night, code, str(progress), str(fixture))
    assert done.returncode == 0, done.stdout + done.stderr


def test_failed_savings_reads_back_off_and_keep_their_day_across_midnight(night: Workflow) -> None:
    _savings_day(night)
    night.orchestrator("idle")
    fixture = night.state / "savings.json"
    data = json.loads(fixture.read_text())
    complete = fixture.read_text()
    del data[f"{_API}/git/commits/{_SQUASH}"]
    fixture.write_text(json.dumps(data))
    reads = night.state / "savings-reads.log"
    fake = night.bin / "gh"
    fake.write_text(fake.read_text().replace('    path = args[-1]\n',
        f'    path = args[-1]\n    with open({str(reads)!r}, "a") as log: log.write(path + "\\n")\n'))
    assert _wake(night, "ticket-7", at=_at(19, 50)).returncode == 0
    progress = night.repo / ".local/workflow/prefix-savings-2026-10-12.progress"
    assert progress.exists()
    assert json.loads(progress.read_text())["date"] == "2026-10-12"
    count = len(reads.read_text().splitlines())
    assert _wake(night, "ticket-7", at=_at(19, 53)).returncode == 0
    assert len(reads.read_text().splitlines()) == count
    fixture.write_text(complete)
    assert _wake(night, at=_at(20, 14)).returncode == 0
    assert "saved (2026-10-12 UTC): images 24.00" in night.sent()[-1]
    assert not progress.exists()
    assert not progress.with_suffix(".progress.retry").exists()


def test_savings_resumes_inside_one_pull_request_without_recounting_runs(night: Workflow) -> None:
    _savings_day(night)
    done = _python(night, '''
        import json
        from pathlib import Path
        import prefix_savings as savings
        progress, fixture = map(Path, sys.argv[2:])
        data = json.loads(fixture.read_text())
        api = "repos/example-org/example-app"
        paths, failing = [], True
        def read(path, key=None, **kwargs):
            paths.append(path)
            if path.startswith("search/issues?"):
                return {"total_count": 1, "items": [{"number": 7}]}
            if path == api + "/check-runs/201/annotations?per_page=100" and failing:
                raise p.Unreadable("temporary read failure")
            value = data["pr-runs"] if "/actions/workflows/ci.yml/runs?" in path else data[path]
            return value[key] if key else value
        try:
            savings.report("2026-10-12", read, progress=progress)
        except p.Unreadable:
            pass
        current = json.loads(progress.read_text())["current"]
        assert current["next"] == 1 and current["totals"]["images"] == 12
        failing = False
        paths.clear()
        line = savings.report("2026-10-12", read, progress=progress)
        assert "images 24.00" in line and "total 68.00" in line
        assert all("/actions/runs/100/" not in path for path in paths)
        assert all("/git/commits/" not in path and "/pulls/" not in path for path in paths)
    ''', str(night.state / "prefix-savings.progress"), str(night.state / "savings.json"))
    assert done.returncode == 0, done.stdout + done.stderr


def test_at_night_an_idle_orchestrator_gets_a_check_in(night: Workflow) -> None:
    night.orchestrator("idle")

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    assert "Recipient: Check-in" in prompt
    assert "Wake check-in, 23:00." in prompt
    assert "5-hour window 76% used, resets in 2h10m, weekly 41%" in prompt
    assert "Sessions: ticket-7 busy." in prompt
    assert "drafts 7; waiting tickets 12; ready medium 9 (checks passing). Log: ~/" in prompt
    assert "asking the owner" not in prompt
    assert "start nothing above 85% of the 5-hour window unless it resets within the hour" in prompt


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("kind", ["not started", "missing", "non-numeric"])
def test_the_check_in_names_not_started_usage_and_its_start_permission(
    night: Workflow, source: str, kind: str
) -> None:
    night.orchestrator("idle")
    answer = {"seven_day": _ANSWER["seven_day"]}
    if kind != "missing":
        answer["five_hour"] = {
            "utilization": 0.0 if kind == "not started" else "0",
            "resets_at": None,
        }
    _usage_answer(night, answer, source)

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    expected = (
        "5-hour window 0% used, not started" if kind == "not started"
        else "5-hour window unknown"
    )
    assert expected in prompt
    assert expected in night.wake_log.read_text()
    gate = "allowed" if kind == "not started" else "unknown"
    assert f"starts: {gate}" in night.wake_log.read_text()
    assert "never merge, never force-push, never edit code" in prompt


def test_a_merge_hand_back_is_told_once_at_any_hour_and_retained_on_failed_delivery(
    night: Workflow,
) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/merge-handbacks.pending"
    pending.mkdir(parents=True)
    event = pending / "one-event"
    event.write_text(json.dumps({"number": "9", "reason": "head changed after review"}))
    (night.state / "send-fails").touch()

    first = _wake(night, at=_at(12, 0))
    assert first.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    second = _wake(night, at=_at(12, 5))
    third = _wake(night, at=_at(12, 10))

    assert second.returncode == third.returncode == 0
    assert "pull request 9: head changed after review" in night.sent()[-1]
    assert not event.exists()
    assert len(night.sent()) == 2  # one failed attempt, then one delivered message


def test_an_unreachable_advisory_service_is_told_once_at_any_hour(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/advisory-service.pending"
    pending.write_text("npm's advisory service unreachable since 2026-10-22 02:46 UTC\n")
    (night.state / "send-fails").touch()

    assert _wake(night, at=_at(12, 0)).returncode == 1
    assert pending.exists()
    (night.state / "send-fails").unlink()
    second = _wake(night, at=_at(12, 5))
    third = _wake(night, at=_at(12, 10))

    assert second.returncode == third.returncode == 0
    assert (
        "Package audit: npm's advisory service unreachable since 2026-10-22 02:46 UTC; "
        "pushes wait for it, nothing was found wrong."
    ) in night.sent()[-1]
    assert not pending.exists()
    assert len(night.sent()) == 2  # one failed attempt, then one delivered message


def test_machine_error_notice_waits_for_delivery_and_is_told_once(night: Workflow) -> None:
    night.orchestrator("busy")
    folder = night.repo / ".local/workflow/stuck.pending"
    folder.mkdir(parents=True, exist_ok=True)
    notice = folder / "machine-errors-example.json"
    line = "Machine error: No space left on device; free disk space; gate commands wait"
    notice.write_text(json.dumps({"line": line}))
    (night.state / "send-fails").touch()
    assert _wake(night, at=_at(12, 0)).returncode == 1
    assert notice.exists()
    (night.state / "send-fails").unlink()
    assert _wake(night, at=_at(12, 5)).returncode == 0
    assert line in night.sent()[-1]
    assert not notice.exists()
    assert _wake(night, at=_at(12, 10)).returncode == 0
    assert len(night.sent()) == 2


def test_main_with_only_an_advisory_is_named_on_its_main_line_at_any_hour(
    night: Workflow,
) -> None:
    """The gate publishes an advisory main as a state of its own, and notes the change."""
    night.orchestrator("busy")
    folder = night.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    line = (
        "main: advisory since 2026-10-13T15:00:00Z, CI result, CI: audit (0123456789ab): "
        "GHSA-1 in vite (path traversal); merging goes on, the update that fixes it goes first"
    )
    (folder / "main-status").write_text(line + "\n")
    (folder / "main-status.pending").write_text(line + "\n")

    done = _wake(night, at=_at(12, 0))

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    assert f"; {line}. Log: ~/" in prompt
    assert not (folder / "main-status.pending").exists()


def test_a_lost_check_is_told_once_at_any_hour_on_a_line_of_its_own(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/lost-checks.pending"
    pending.mkdir(parents=True)
    event = pending / f"494-{'a' * 40}.json"
    event.write_text(
        json.dumps(
            {
                "number": "494",
                "head": "a" * 40,
                "check": "CI result",
                "run": 123,
                "conclusion": "success",
            }
        )
    )
    # A note the gate is still writing is not read.
    (pending / "tmpwriting").write_text("{")
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(12, 0))
    assert failed.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    first = _wake(night, at=_at(12, 5))
    second = _wake(night, at=_at(12, 10))

    assert first.returncode == second.returncode == 0, first.stderr
    [_, prompt] = night.sent()  # one failed attempt, then one delivered message
    assert "\nlost check: CI result of run 123 on pull request 494\n" in prompt
    assert not event.exists()
    assert (night.repo / ".local/workflow/lost-checks.sent" / event.name).exists()
    assert "  lost check: CI result of run 123 on pull request 494" in night.wake_log.read_text()


def test_a_rule_break_is_told_once_at_any_hour_on_a_line_of_its_own(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/rule-breaks.pending"
    pending.mkdir(parents=True)
    event = pending / f"review-9-{'b' * 40}.json"
    event.write_text(json.dumps({"name": "review-9", "number": "9", "head": "b" * 40}))
    # A note the tick is still writing is not read.
    (pending / f".review-9-{'c' * 40}.json.tmp").write_text("{")
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(12, 0))
    assert failed.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    first = _wake(night, at=_at(12, 5))
    second = _wake(night, at=_at(12, 10))

    assert first.returncode == second.returncode == 0, first.stderr
    [_, prompt] = night.sent()  # one failed attempt, then one delivered message
    line = "rule break: review-9 said review finished without a merge handoff"
    assert f"\n{line}\n" in prompt
    assert not event.exists()
    assert (night.repo / ".local/workflow/rule-breaks.sent" / event.name).exists()
    assert f"  {line}" in night.wake_log.read_text()


def test_the_message_goes_through_a_small_model_with_one_tool_and_no_settings(
    night: Workflow,
) -> None:
    night.orchestrator("idle")

    _wake(night)

    arguments = night.sent()[0].splitlines()
    assert arguments[0] == "-p"
    for pair in (
        ["--model", "claude-haiku-5-5"],
        ["--setting-sources", ""],
        ["--tools", "SendMessage"],
    ):
        at = arguments.index(pair[0])
        assert arguments[at : at + 2] == pair


def test_the_log_says_what_was_seen_and_what_was_done(night: Workflow) -> None:
    night.orchestrator("idle")

    _wake(night)

    log = night.wake_log.read_text().splitlines()
    assert log[0] == "2026-10-13 23:00:00  every 20 minutes"
    assert log[1] == (
        "  usage: 5-hour window 76% used, resets in 2h10m, weekly 41%;"
        " /tmp file slots: 10% used (100/1000);"
        " load 0.40 over 5 minutes, ceiling 6 on 4 processors; above it starts wait"
    )
    assert log[2] == (
        "  sessions: ticket-7 busy; pull requests: drafts 7; waiting tickets 12;"
        " ready medium 9 (checks passing); starts: allowed"
        f"; shipline: {night.git('rev-parse', '--short', 'HEAD')}; tmp pressure: false"
    )
    assert log[3].startswith("  told Check-in: Wake check-in, 23:00.")


def test_nothing_new_is_not_told_twice(night: Workflow) -> None:
    night.orchestrator("idle")

    _wake(night, at=_at(23, 0))
    _wake(night, at=_at(23, 21))

    assert len(night.sent()) == 1
    assert night.wake_log.read_text().splitlines()[-1] == (
        "  nothing changed since the last check-in; Check-in not told"
    )


def test_the_check_in_groups_the_ready_pull_requests_by_priority(night: Workflow) -> None:
    night.orchestrator("idle")
    night.list_pull_request(7, draft=False, labels=("workflow", "priority:+low"))
    night.list_pull_request(9, draft=False, labels=("workflow", "priority:+high"))
    night.add_pull_request(13, draft=False, labels=("priority:+blocker",))
    night.add_pull_request(14, draft=False)

    _wake(night)

    [prompt] = night.sent()
    assert (
        "Open pull requests: waiting tickets 12; ready blocker 13 (checks passing);"
        " ready high 9 (checks passing); ready medium 14 (checks passing);"
        " ready low 7 (checks passing). Log: ~/"
    ) in prompt


def test_a_change_is_told_at_the_next_look(night: Workflow) -> None:
    night.orchestrator("idle")
    _wake(night, at=_at(23, 0))
    night.list_pull_request(7, draft=False)

    _wake(night, at=_at(23, 21))

    assert len(night.sent()) == 2
    assert "ready medium 7 (checks passing), 9 (checks passing)" in night.sent()[1]


def test_it_looks_every_20_minutes_and_not_at_each_tick(night: Workflow) -> None:
    night.orchestrator("idle")
    _wake(night, at=_at(23, 0))
    night.list_pull_request(7, draft=False)

    early = _wake(night, at=_at(23, 18))
    sent_early = len(night.sent())
    _wake(night, at=_at(23, 20))

    assert early.returncode == 0
    assert sent_early == 1
    assert len(night.sent()) == 2


@pytest.mark.parametrize(
    ("hour", "looks"), [(8, False), (12, False), (19, False), (20, True), (3, True)]
)
def test_it_looks_in_only_at_night(night: Workflow, hour: int, looks: bool) -> None:
    night.orchestrator("idle")

    done = _wake(night, at=_at(hour, 0))

    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == int(looks)
    assert night.wake_log.exists() == looks


def test_a_busy_orchestrator_is_left_to_its_work(night: Workflow) -> None:
    night.orchestrator("busy")

    _wake(night)

    assert night.sent() == []
    assert night.wake_log.read_text().splitlines()[-1] == "  Check-in is busy; not told"


def test_after_a_reset_it_is_told_at_once_at_any_hour_and_even_when_busy(night: Workflow) -> None:
    night.orchestrator("busy")
    _wake(night, at=_at(9, 59))

    done = _wake(night, "ticket-7", "audit-9", at=_at(10, 0))

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    assert "Wake check-in, 10:00, after a reset." in prompt
    assert "Sent their instruction again: ticket-7 audit-9." in prompt
    log = night.wake_log.read_text()
    assert "2026-10-13 10:00:00  after a reset" in log
    assert "  the tick sent its instruction again to: ticket-7 audit-9" in log


def test_without_an_orchestrator_nobody_is_told_and_it_is_written_down(night: Workflow) -> None:
    done = _wake(night)

    assert done.returncode == 0, done.stderr
    assert night.sent() == []
    assert night.wake_log.read_text().splitlines()[-1] == (
        f"  orchestrator registration could not be read: "
        f"{night.repo}/.local/workflow/orchestrator; nobody was told"
    )


def test_a_message_that_was_not_delivered_fails_and_is_tried_at_the_next_look(
    night: Workflow,
) -> None:
    night.orchestrator("idle")
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(23, 0))
    (night.state / "send-fails").unlink()
    again = _wake(night, at=_at(23, 20))

    assert failed.returncode == 1
    assert "could not be sent" in failed.stderr
    assert "  the check-in could not be sent to Check-in" in night.wake_log.read_text()
    assert again.returncode == 0, again.stderr
    assert len(night.sent()) == 2


def test_unreadable_usage_and_pull_requests_are_said_and_do_not_stop_the_check_in(
    workflow: Workflow,
) -> None:
    workflow.orchestrator("idle")
    (workflow.state / "fail-gh-list").touch()

    done = _wake(workflow)

    assert done.returncode == 0, done.stderr
    [prompt] = workflow.sent()
    assert "Usage: the usage could not be read; /tmp file slots: 10% used (100/1000);" in prompt
    assert "Sessions: none. Open pull requests: could not be read." in prompt


def test_it_starts_nothing_merges_nothing_and_types_into_no_session(night: Workflow) -> None:
    night.orchestrator("idle")

    _wake(night, "ticket-7")

    assert [call for call in night.calls("tmux") if " new-session " in call] == []
    assert [call for call in night.calls("tmux") if " send-keys " in call] == []
    assert [call for call in night.calls("gh") if " list " not in call] == []


def test_what_claude_prints_besides_the_answer_does_not_hide_a_delivery(night: Workflow) -> None:
    """Live, a warning on the error output made a delivered message count as not delivered."""
    night.orchestrator("idle")
    (night.state / "stderr-warning").touch()

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == 1
    send_log = night.wake_log.with_name("wake.send.log").read_text()
    assert "Warning: a line that is not JSON" in send_log


def test_claude_is_given_no_standard_input(night: Workflow) -> None:
    night.orchestrator("idle")
    night.fake(
        "claude",
        (night.bin / "claude")
        .read_text()
        .replace(
            '[ "$1" = -p ] || exit 1', '[ "$1" = -p ] || exit 1\n[ ! -p /dev/stdin ] || exit 1'
        ),
    )

    done = _wake(night, "ticket-7")

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "answer",
    [
        # The tool refused: no such session.
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"SendMessage"}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
        '"is_error":true}]}}\n{"type":"result","is_error":false,"result":"sent"}',
        # The model said it sent the message without calling the tool.
        '{"type":"result","is_error":false,"result":"sent"}',
    ],
)
def test_a_message_the_tool_did_not_deliver_is_not_counted_as_told(
    night: Workflow, answer: str
) -> None:
    night.orchestrator("idle")
    (night.state / "answer").write_text(answer + "\n")
    night.fake(
        "claude",
        (night.bin / "claude")
        .read_text()
        .replace(
            '[ "$1" = -p ] || exit 1', '[ "$1" = -p ] || exit 1\ncat "@STATE@/answer"; exit 0'
        ),
    )

    failed = _wake(night, at=_at(23, 0))

    assert failed.returncode == 1
    assert "  the check-in could not be sent to Check-in" in night.wake_log.read_text()
    assert not night.wake_log.with_name("wake.told").exists()


def test_sessions_that_could_not_be_read_are_not_said_to_be_none(night: Workflow) -> None:
    night.orchestrator("idle")
    night.break_session("list")

    _wake(night)

    [prompt] = night.sent()
    assert "Sessions: could not be read." in prompt


def test_the_orchestrator_is_told_when_starting_becomes_allowed_again(night: Workflow) -> None:
    """Above 85% it waits to start anything; nothing else changes when the window resets."""
    night.orchestrator("idle")
    night.usage(90, "2026-10-14T05:10:00+00:00")
    _wake(night, at=_at(23, 0))
    night.usage(90, "2026-10-14T05:10:00+00:00")
    _wake(night, at=_at(23, 20))
    night.usage(3, "2026-10-14T09:40:00+00:00")

    _wake(night, at=_at(23, 40))

    assert len(night.sent()) == 2
    assert "starts: held" in night.wake_log.read_text()
    assert "starts: allowed" in night.wake_log.read_text().splitlines()[-2]


def test_the_tick_gives_a_check_in_at_night_and_still_starts_a_worker(night: Workflow) -> None:
    night.orchestrator("idle")
    night.add_pull_request(15)

    done = night.run_script("tick", env=_at(23, 0))

    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == 1
    assert [call for call in night.calls("tmux ticket-15 new-session")] != []


def test_a_tick_whose_check_in_failed_fails_and_still_starts_a_worker(night: Workflow) -> None:
    night.orchestrator("idle")
    night.add_pull_request(15)
    (night.state / "send-fails").touch()

    done = night.run_script("tick", env=_at(23, 0))

    assert done.returncode == 1
    assert [call for call in night.calls("tmux ticket-15 new-session")] != []


def test_checks_that_turn_green_are_told(night: Workflow) -> None:
    """A ready pull request whose checks pass can be merged: that is news."""
    night.orchestrator("idle")
    (night.state / "checks").mkdir()
    (night.state / "checks" / "9").write_text("PENDING\n")
    _wake(night, at=_at(23, 0))
    (night.state / "checks" / "9").write_text("SUCCESS\n")

    _wake(night, at=_at(23, 20))

    assert "ready medium 9 (checks pending)" in night.sent()[0]
    assert "ready medium 9 (checks passing)" in night.sent()[1]


def test_a_failing_check_is_named(night: Workflow) -> None:
    night.orchestrator("idle")
    (night.state / "checks").mkdir()
    (night.state / "checks" / "9").write_text("FAILURE\n")

    _wake(night)

    assert "ready medium 9 (checks failing)" in night.sent()[0]


def test_a_reset_check_in_that_failed_is_tried_again_by_day(night: Workflow) -> None:
    """The tick passes the names once; the wake keeps them until the orchestrator is told."""
    night.orchestrator("idle")
    (night.state / "send-fails").touch()
    failed = _wake(night, "ticket-7", at=_at(10, 0))
    (night.state / "send-fails").unlink()

    soon = _wake(night, at=_at(10, 3))
    sent_soon = len(night.sent())
    again = _wake(night, at=_at(10, 6))
    after = _wake(night, at=_at(10, 12))

    assert failed.returncode == 1
    assert soon.returncode == 0
    assert sent_soon == 1
    assert again.returncode == 0, again.stderr
    assert len(night.sent()) == 2
    assert "Sent their instruction again: ticket-7." in night.sent()[1]
    assert after.returncode == 0
    assert len(night.sent()) == 2


def test_a_reset_check_in_waits_for_an_orchestrator_to_open(night: Workflow) -> None:
    _wake(night, "ticket-7", at=_at(10, 0))
    night.orchestrator("busy")

    _wake(night, "audit-9", at=_at(10, 3))

    [prompt] = night.sent()
    assert "Sent their instruction again: ticket-7 audit-9." in prompt


@pytest.mark.parametrize("name", ["Check-in", "Evening check-in"])
def test_only_the_registered_session_gets_the_check_in(night: Workflow, name: str) -> None:
    night.orchestrator("idle", name=name)
    agents = night.state / "agents.json"
    listed = json.loads(agents.read_text())
    listed.insert(
        0,
        {
            "name": "App build and testing",
            "kind": "interactive",
            "status": "idle",
            "cwd": str(night.repo),
        },
    )
    agents.write_text(json.dumps(listed))

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    assert f"Recipient: {name}\n" in prompt
    assert "Recipient: App build and testing" not in prompt


@pytest.mark.parametrize("registration", [None, "", "Closed session\n"])
def test_without_a_registered_open_session_pending_messages_are_kept(
    night: Workflow, registration: str | None
) -> None:
    night.orchestrator("idle")
    path = night.repo / ".local" / "workflow" / "orchestrator"
    if registration is None:
        path.unlink()
    else:
        path.write_text(registration)

    done = _wake(night, "ticket-7", at=_at(10, 0))

    assert done.returncode == 1  # No phone keys: the fallback fails without consuming delivery.
    assert night.sent() == []
    assert night.wake_log.with_name("wake.pending").read_text() == "ticket-7\n"
    assert not night.wake_log.with_name("wake.told").exists()
    assert "nobody was told" in night.wake_log.read_text()
    assert "orchestrator" in night.wake_log.read_text().splitlines()[-1]
    path.write_text("Check-in\n")

    again = _wake(night, at=_at(10, 6))

    assert again.returncode == 0, again.stderr
    [prompt] = night.sent()
    assert "Recipient: Check-in\n" in prompt
    assert "Sent their instruction again: ticket-7." in prompt
    assert not night.wake_log.with_name("wake.pending").exists()


@pytest.mark.parametrize("kind,cwd", [("background", "main"), ("interactive", "worktree")])
def test_the_registered_name_must_be_interactive_in_the_main_checkout(
    night: Workflow, kind: str, cwd: str
) -> None:
    night.orchestrator("idle")
    agents = night.state / "agents.json"
    listed = json.loads(agents.read_text())
    listed[-1]["kind"] = kind
    listed[-1]["cwd"] = str(night.repo if cwd == "main" else night.worktree(7))
    agents.write_text(json.dumps(listed))

    done = _wake(night, "ticket-7")

    assert done.returncode == 1  # No phone keys; the message remains pending.
    assert night.sent() == []
    assert night.wake_log.with_name("wake.pending").read_text() == "ticket-7\n"
    assert "named orchestrator (Check-in) is not open" in night.wake_log.read_text()


def _stop(workflow: Workflow, name: str, message: str, at: dict[str, str]) -> None:
    """The stop hook of a running session, as `notify` gets it."""
    done = workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": message}, session=name, env=at
    )
    assert done.returncode == 0, done.stderr


@pytest.fixture
def day(night: Workflow) -> Workflow:
    """A reviewer runs beside the worker, and the orchestrator is busy at its own work."""
    night.running("review-7")
    night.orchestrator("busy")
    return night


@pytest.fixture
def phone(day: Workflow) -> Workflow:
    from workflow.test_notify import _TOKEN, _USER

    keys = day.root / "home/agents/.env"
    keys.parent.mkdir(exist_ok=True)
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")
    return day


def _close_orchestrator(workflow: Workflow) -> None:
    (workflow.state / "agents.json").write_text("[]")


@pytest.mark.parametrize("hour", [0, 12])
def test_closed_orchestrator_rings_each_question_once_and_later_delivers(
    phone: Workflow, hour: int,
) -> None:
    _close_orchestrator(phone)
    # Record the questions without a Stop-hook push, to isolate wake's fallback.
    notes = phone.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "review-7.idle").touch()
    (notes / "review-7.asking").write_text(f"{_at(hour)['WORKFLOW_NOW']}\n")
    for minute in (8, 14, 20):
        done = _wake(phone, at=_at(hour, minute))
        assert done.returncode == 0, done.stderr
    [first] = phone.pushes()
    assert first.title == "Workflow needs you"
    assert first.message == "Orchestrator is not open.\nasking the owner: review-7 (8 min)"
    assert phone.sent() == []

    # A second question in the same session is new even though its first line is unchanged.
    (notes / "review-7.asking").write_text(f"{_at(hour, 21)['WORKFLOW_NOW']}\n")
    for minute in (26, 32):
        assert _wake(phone, at=_at(hour, minute)).returncode == 0
    assert len(phone.pushes()) == 2
    assert phone.pushes()[1].message == "asking the owner: review-7 (5 min)"

    phone.orchestrator("busy")
    assert _wake(phone, at=_at(hour, 38)).returncode == 0
    assert len(phone.pushes()) == 2
    [prompt] = phone.sent()
    assert "asking the owner: review-7 (17 min)" in prompt

    _close_orchestrator(phone)
    (notes / "review-7.asking").write_text(f"{_at(hour, 39)['WORKFLOW_NOW']}\n")
    assert _wake(phone, at=_at(hour, 44)).returncode == 0
    assert len(phone.pushes()) == 3
    assert phone.pushes()[-1].message.startswith("Orchestrator is not open.\n")


def test_closed_orchestrator_plain_check_ins_do_not_ring(phone: Workflow) -> None:
    _close_orchestrator(phone)
    for minute in (0, 21, 42):
        assert _wake(phone, at=_at(23, minute)).returncode == 0
    assert phone.pushes() == []
    phone.orchestrator("idle")
    assert _wake(phone, at=_at(0, 3, day=14)).returncode == 0
    assert len(phone.sent()) == 1
    assert phone.pushes() == []


def test_closed_orchestrator_rings_each_pending_message_first_line(phone: Workflow) -> None:
    _close_orchestrator(phone)
    folder = phone.repo / ".local/workflow/stuck.pending"
    folder.mkdir()
    lines = ["stuck? ticket-7 (45 min)", "starts held: usage unread for 1 h, 2 tickets waiting"]
    for index, line in enumerate(lines):
        (folder / f"{index}.json").write_text(json.dumps({"line": line + "\nMore detail"}))
    for minute in (0, 6, 12):
        assert _wake(phone, at=_at(12, minute)).returncode == 0
    assert [push.message for push in phone.pushes()] == [
        "Orchestrator is not open.\n" + lines[0], lines[1],
    ]
    assert len(list(folder.iterdir())) == 2  # Phone receipts do not consume session delivery.
    phone.orchestrator("busy")
    assert _wake(phone, at=_at(12, 18)).returncode == 0
    assert all(line in phone.sent()[0] for line in lines)
    assert list(folder.iterdir()) == []
    assert len(phone.pushes()) == 2


def test_closed_orchestrator_failed_push_is_retried_and_shadow_does_not_acknowledge(
    phone: Workflow,
) -> None:
    _close_orchestrator(phone)
    folder = phone.repo / ".local/workflow/stuck.pending"
    folder.mkdir()
    (folder / "example.json").write_text(json.dumps({"line": "stuck? ticket-7 (45 min)"}))
    shadow = phone.root / "shadow.log"
    assert _wake(phone, at={**_at(12), "SHIPLINE_SHADOW": str(shadow)}).returncode == 0
    assert phone.pushes() == []
    (phone.state / "pushover-fails").touch()
    assert _wake(phone, at=_at(12)).returncode == 1
    (phone.state / "pushover-fails").unlink()
    for minute in (6, 12):
        assert _wake(phone, at=_at(12, minute)).returncode == 0
    assert len(phone.pushes()) == 2  # One failed attempt, then one successful delivery.
    assert all(push.message.startswith("Orchestrator is not open.\n") for push in phone.pushes())


@pytest.mark.parametrize("kind,payload,line", [
    ("merge-handbacks", {"number": 7, "reason": "needs judgment"}, "pull request 7: needs judgment"),
    ("cloud-events", {"name": "cloud-7", "message": "question", "url": ""}, "cloud-7: question ()"),
    ("lost-checks", {"number": 7, "check": "CI", "run": 123}, "lost check: CI of run 123 on pull request 7"),
    ("rule-breaks", {"name": "review-7"}, "rule break: review-7 said review finished without a merge handoff"),
    ("ticket-starts-alert", {"line": "starts held: usage unread"}, "starts held: usage unread"),
    ("analyses", {"name": "day-example", "report": "report.md", "url": "", "proposals": []},
     "Workflow analysis day-example ended without publishing its report (report.md)."),
])
def test_closed_orchestrator_rings_any_hour_queue_once(
    phone: Workflow, kind: str, payload: dict, line: str,
) -> None:
    _close_orchestrator(phone)
    folder = phone.repo / f".local/workflow/{kind}.pending"
    folder.mkdir()
    event = folder / "example.json"
    event.write_text(json.dumps(payload))
    for minute in (0, 6, 12):
        # Producers may replace the same event on each retry until session delivery.
        replacement = folder / ".replacement"
        replacement.write_text(json.dumps(payload))
        replacement.replace(event)
        done = _wake(phone, at=_at(12, minute))
        assert done.returncode == 0, done.stderr
    [push] = phone.pushes()
    assert push.message == "Orchestrator is not open.\n" + line
    assert event.exists()
    phone.orchestrator("busy")
    assert _wake(phone, at=_at(12, 18)).returncode == 0
    assert line in phone.sent()[0]
    assert not event.exists()
    assert len(phone.pushes()) == 1


@pytest.mark.parametrize("filename,line", [
    ("main-status.pending", "main: red, CI failed"),
    ("advisory-service.pending", "advisory service unreachable"),
])
def test_closed_orchestrator_rings_singleton_status_notices_once(
    phone: Workflow, filename: str, line: str,
) -> None:
    _close_orchestrator(phone)
    event = phone.repo / ".local/workflow" / filename
    event.write_text(line + "\nFurther details\n")
    for minute in (0, 6, 12):
        done = _wake(phone, at=_at(12, minute))
        assert done.returncode == 0, done.stderr
    [push] = phone.pushes()
    assert push.message == "Orchestrator is not open.\n" + line
    assert event.exists()


def test_parallel_closed_orchestrator_wakes_ring_each_notice_once(phone: Workflow) -> None:
    from concurrent.futures import ThreadPoolExecutor

    _close_orchestrator(phone)
    folder = phone.repo / ".local/workflow/stuck.pending"
    folder.mkdir()
    (folder / "example.json").write_text(json.dumps({"line": "stuck? ticket-7 (45 min)"}))
    # Explicit names bypass pacing, making both callers attempt the same pending notices.
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(_wake, phone, "ticket-7", at=_at(12)) for _ in range(2)]
        results = [call.result() for call in calls]
    assert all(done.returncode == 0 for done in results), [done.stderr for done in results]
    assert [push.message for push in phone.pushes()] == [
        "Orchestrator is not open.\nstuck? ticket-7 (45 min)",
        "Sent their instruction again: ticket-7.",
    ]


def test_closed_orchestrator_reset_names_ring_once_until_session_delivery(phone: Workflow) -> None:
    _close_orchestrator(phone)
    assert _wake(phone, "ticket-7", at=_at(12)).returncode == 0
    assert _wake(phone, at=_at(12, 6)).returncode == 0
    assert _wake(phone, "audit-9", at=_at(12, 7)).returncode == 0
    assert _wake(phone, at=_at(12, 13)).returncode == 0
    assert [push.message for push in phone.pushes()] == [
        "Orchestrator is not open.\nSent their instruction again: ticket-7.",
        "Sent their instruction again: audit-9.",
    ]
    phone.orchestrator("busy")
    assert _wake(phone, at=_at(12, 19)).returncode == 0
    assert "Sent their instruction again: ticket-7 audit-9." in phone.sent()[0]
    _close_orchestrator(phone)
    assert _wake(phone, "ticket-7", at=_at(12, 20)).returncode == 0
    assert len(phone.pushes()) == 3
    assert phone.pushes()[-1].message.startswith("Orchestrator is not open.\n")


def test_a_question_to_the_owner_is_named_once_at_any_hour(day: Workflow) -> None:
    _stop(day, "review-7", "Merge it or send it back?\n\nReply 1 or 2", _at(10, 0))

    told = _wake(day, at=_at(10, 8))
    again = _wake(day, at=_at(10, 30))

    assert told.returncode == 0, told.stderr
    assert again.returncode == 0, again.stderr
    [prompt] = day.sent()
    assert "(checks passing).\nasking the owner: review-7 (8 min)\nLog: ~/" in prompt
    assert "  asking the owner: review-7 (8 min)" in day.wake_log.read_text()


def test_a_session_waiting_for_its_own_job_is_not_named(day: Workflow) -> None:
    day.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})
    _stop(day, "review-7", "Checks are running; I continue when they finish.", _at(10, 0))

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_finished_session_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Merged as one commit.\n\nreview finished", _at(10, 0))

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_question_that_was_answered_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    day.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"}, session="review-7")

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_new_question_of_the_same_session_is_named_again(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    _wake(day, at=_at(10, 3))
    day.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"}, session="review-7")
    _stop(day, "review-7", "The checks failed. Reply 3 or 4", _at(10, 20))

    _wake(day, at=_at(10, 26))

    assert len(day.sent()) == 2
    assert "\nasking the owner: review-7 (6 min)\n" in day.sent()[1]


def test_several_sessions_asking_are_named_on_one_line(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    _stop(day, "ticket-7", "Which limit do you want: 500 or 1,000?", _at(10, 7))

    _wake(day, at=_at(10, 15))

    [prompt] = day.sent()
    assert "\nasking the owner: review-7 (15 min), ticket-7 (8 min)\n" in prompt


def test_a_session_that_no_longer_runs_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    (day.state / "tmux" / "review-7").unlink()

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_question_whose_check_in_failed_is_tried_again(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    (day.state / "send-fails").touch()
    failed = _wake(day, at=_at(10, 3))
    (day.state / "send-fails").unlink()

    soon = _wake(day, at=_at(10, 6))
    sent_soon = len(day.sent())
    _wake(day, at=_at(10, 9))

    assert failed.returncode == 1
    assert soon.returncode == 0
    assert sent_soon == 1
    assert len(day.sent()) == 2
    assert "\nasking the owner: review-7 (9 min)\n" in day.sent()[1]


def test_a_question_already_told_is_still_named_in_a_later_check_in(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(22, 0))
    day.orchestrator("idle")
    _wake(day, at=_at(22, 1))
    day.list_pull_request(7, draft=False)

    _wake(day, at=_at(22, 21))

    assert len(day.sent()) == 2
    assert "\nasking the owner: review-7 (21 min)\n" in day.sent()[1]


def test_a_session_working_again_on_its_own_job_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    day.notify(
        {"hook_event_name": "UserPromptSubmit", "source": "background_task_output"},
        session="review-7",
    )

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_codex_bridge_whose_codex_asks_is_named(day: Workflow) -> None:
    (day.notes(7) / "ticket-7.codex").parent.mkdir(parents=True, exist_ok=True)
    (day.notes(7) / "ticket-7.codex").touch()
    _stop(day, "ticket-7", "Codex asks: which limit, 500 or 1,000?", _at(10, 0))

    _wake(day, at=_at(10, 4))

    [prompt] = day.sent()
    assert "\nasking the owner: ticket-7 (4 min)\n" in prompt


def test_a_question_of_a_session_that_ended_brings_no_check_in_by_day(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    (day.state / "tmux" / "review-7").unlink()
    day.orchestrator("idle")

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_question_told_before_its_own_job_ran_is_not_told_again(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    _wake(day, at=_at(10, 3))
    day.notify(
        {"hook_event_name": "UserPromptSubmit", "source": "background_task_output"},
        session="review-7",
    )
    _wake(day, "ticket-7", at=_at(10, 9))
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 12))

    _wake(day, at=_at(10, 18))

    assert len(day.sent()) == 2
    assert "asking the owner" not in day.sent()[1]


def test_pull_request_read_retries_server_errors_before_check_in(night: Workflow) -> None:
    night.orchestrator("idle")
    night.fake("gh-real", (night.bin / "gh").read_text())
    counter = night.state / "list-attempts"
    night.fake(
        "gh",
        f'''#!/bin/bash
if [[ "$1 $2" == "pr list" ]]; then
  count=0
  [[ ! -f "{counter}" ]] || read -r count < "{counter}"
  count=$((count + 1))
  echo "$count" > "{counter}"
  if ((count <= 2)); then echo 'gh: Server Error (HTTP 502)' >&2; exit 1; fi
fi
exec "{night.bin / "gh-real"}" "$@"
''',
    )

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    assert counter.read_text().strip() == "3"
    [prompt] = night.sent()
    assert "drafts 7; waiting tickets 12; ready medium 9 (checks passing)" in prompt


def _analysis(workflow: Workflow, url: str, proposals: list[str]) -> Path:
    pending = workflow.repo / ".local/workflow/analyses.pending"
    pending.mkdir(parents=True, exist_ok=True)
    event = pending / "day-2026-10-13.json"
    event.write_text(
        json.dumps(
            {
                "name": "day-2026-10-13",
                "report": ".local/analyses/day-2026-10-13/report.md",
                "url": url,
                "proposals": proposals,
            }
        )
    )
    # A note the tick is still writing is not read.
    (pending / ".day-2026-10-14.json").write_text("{")
    return event


def test_a_finished_analysis_is_told_once_by_day_with_its_link_and_proposals(
    night: Workflow,
) -> None:
    night.orchestrator("busy")
    event = _analysis(
        night,
        "https://docs.example.com/analysis-1",
        ["1. The tick starts tickets by itself", "2. A lost check is re-run by the gate"],
    )
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(9, 0))
    assert failed.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    first = _wake(night, at=_at(9, 5))
    second = _wake(night, at=_at(9, 10))

    assert first.returncode == second.returncode == 0, first.stderr
    [_, prompt] = night.sent()  # one failed attempt, then one delivered message
    assert (
        " Workflow analysis day-2026-10-13 (report https://docs.example.com/analysis-1): "
        "1. The tick starts tickets by itself; 2. A lost check is re-run by the gate." in prompt
    )
    assert not event.exists()


def test_an_analysis_without_proposals_says_so(night: Workflow) -> None:
    night.orchestrator("idle")
    _analysis(night, "https://docs.example.com/analysis-2", [])

    done = _wake(night, at=_at(9, 0))

    assert done.returncode == 0, done.stderr
    assert (
        " Workflow analysis day-2026-10-13 (report https://docs.example.com/analysis-2): "
        "no proposals." in night.sent()[-1]
    )


def test_an_analysis_that_ended_without_its_report_is_told(night: Workflow) -> None:
    night.orchestrator("idle")
    _analysis(night, "", [])

    done = _wake(night, at=_at(9, 0))

    assert done.returncode == 0, done.stderr
    assert (
        " Workflow analysis day-2026-10-13 ended without publishing its report "
        "(.local/analyses/day-2026-10-13/report.md)." in night.sent()[-1]
    )


# The questions the tick answered for the owner (`tick`, "Owner only: no").


def _answered(workflow: Workflow, name: str, reply: str, at: dict[str, str]) -> None:
    folder = workflow.repo / ".local" / "workflow" / "answered-for-owner.pending"
    folder.mkdir(parents=True, exist_ok=True)
    moment = int(at["WORKFLOW_NOW"])
    (folder / f"{moment}-{name}.json").write_text(
        json.dumps({"name": name, "reply": reply, "at": moment})
    )


def test_answers_given_overnight_wait_for_the_morning_check_in_and_are_listed_once(
    night: Workflow,
) -> None:
    night.orchestrator("idle")
    _answered(night, "ticket-7", "1", _at(23, 10))
    _answered(night, "review-7", "merge it", _at(3, 12, day=14))

    _wake(night, at=_at(23, 20))
    _wake(night, at=_at(7, 50, day=14))
    night.orchestrator("busy")
    morning = _wake(night, at=_at(8, 0, day=14))
    _wake(night, at=_at(8, 5, day=14))
    _wake(night, at=_at(8, 30, day=14))

    assert morning.returncode == 0, morning.stderr
    sent = night.sent()
    assert len(sent) == 2
    assert "answered for the owner" not in sent[0]
    assert (
        '\nanswered for the owner: ticket-7, "1" (23:10)\n'
        'answered for the owner: review-7, "merge it" (03:12)\n'
    ) in sent[1]
    assert not list((night.repo / ".local/workflow/answered-for-owner.pending").iterdir())
    assert 'answered for the owner: ticket-7, "1" (23:10)' in night.wake_log.read_text()


def test_an_answer_given_in_the_day_is_told_at_the_next_look_once(day: Workflow) -> None:
    _answered(day, "review-7", "1", _at(14, 0))

    told = _wake(day, at=_at(14, 3))
    _wake(day, at=_at(14, 10))
    _wake(day, at=_at(15, 0))

    assert told.returncode == 0, told.stderr
    [prompt] = day.sent()
    assert '\nanswered for the owner: review-7, "1" (14:00)\n' in prompt


def test_an_answer_the_wake_could_not_send_is_sent_later(day: Workflow) -> None:
    _answered(day, "review-7", "1", _at(14, 0))
    (day.state / "send-fails").touch()
    _wake(day, at=_at(14, 3))
    (day.state / "send-fails").unlink()

    _wake(day, at=_at(14, 9))

    assert '\nanswered for the owner: review-7, "1" (14:00)\n' in day.sent()[-1]
    assert not list((day.repo / ".local/workflow/answered-for-owner.pending").iterdir())



def test_a_prefix_queue_warning_is_told_once_and_kept_until_delivery(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/prefix-queues.pending"
    pending.mkdir(parents=True)
    event = pending / "100-2.json"
    line = "scratch run 100 attempt 2 has a job queued past 20 minutes; still waiting"
    event.write_text(json.dumps({"line": line}))
    (night.state / "send-fails").touch()
    assert _wake(night, at=_at(12, 0)).returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    assert _wake(night, at=_at(12, 5)).returncode == 0
    assert _wake(night, at=_at(12, 10)).returncode == 0
    assert len(night.sent()) == 2
    assert f"\n{line}\n" in night.sent()[-1]
    assert not event.exists()
    assert (pending.with_name("prefix-queues.sent") / event.name).exists()


def test_a_runner_download_notice_is_delivered_once_and_retained_on_failure(night: Workflow) -> None:
    night.orchestrator("busy")
    folder = night.repo / ".local/workflow"
    pending = folder / "runner-downloads.pending/20.json"
    pending.parent.mkdir(parents=True)
    line = (
        "Runner example-runner failed image downloads again in run 20: "
        "short read: expected 1048576 bytes but got 0: unexpected EOF; repair its cache"
    )
    pending.write_text(json.dumps({"line": line}))
    (night.state / "send-fails").touch()
    done = _wake(night, at=_at(11))
    assert done.returncode == 1
    assert pending.exists()
    (night.state / "send-fails").unlink()
    done = _wake(night, at=_at(11, 6))
    assert done.returncode == 0, done.stderr
    assert night.sent()[-1].count(line) == 1
    assert not pending.exists()
    assert (folder / "runner-downloads.sent/20.json").exists()
    count = len(night.sent())
    done = _wake(night, at=_at(11, 12))
    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == count


@pytest.mark.parametrize("registration", ["missing", "empty", "closed"])
def test_three_failed_ticks_without_an_orchestrator_ring_once(
    night: Workflow, registration: str,
) -> None:
    from workflow.test_notify import _TOKEN, _USER

    keys = night.root / "home/agents/.env"
    keys.parent.mkdir(exist_ok=True)
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")
    night.orchestrator("idle", name="available")
    folder = night.repo / ".local/workflow"
    (folder / "main-status").write_text("main: green\n")
    if registration == "missing":
        (folder / "orchestrator").unlink()
    else:
        (folder / "orchestrator").write_text("" if registration == "empty" else "closed\n")
    error = "fatal: cannot fetch main\nMore detail that must not ring."
    for _ in range(2):
        night.run_script("wake", "--tick-result", "1", error, env=_at(12))
    assert night.pushes() == []
    for _ in range(2):
        night.run_script("wake", "--tick-result", "1", error, env=_at(12))
    [push] = night.pushes()
    assert "fatal: cannot fetch main" in push.message
    assert "More detail" not in push.message
    assert "write the open session's name to .local/workflow/orchestrator" in push.message
    assert "main checkout" in push.message
    assert "phone" in night.wake_log.read_text()
    assert night.sent() == []

    night.run_script("wake", "--tick-result", "0", "", env=_at(12))
    for _ in range(3):
        night.run_script("wake", "--tick-result", "1", error, env=_at(12))
    assert len(night.pushes()) == 2


@pytest.mark.parametrize("status", ["idle", "busy"])
def test_persistent_failure_tells_the_open_orchestrator_without_a_phone_push(
    night: Workflow, status: str,
) -> None:
    night.orchestrator(status)
    for _ in range(4):
        done = night.run_script("wake", "--tick-result", "1", "fatal: cannot fetch main", env=_at(12))
        assert done.returncode == 0, done.stderr
    [message] = night.sent()
    assert "fatal: cannot fetch main" in message
    assert night.pushes() == []
    assert not night.missed_pushes.exists()


def test_losing_the_orchestrator_during_a_failure_stretch_still_rings(night: Workflow) -> None:
    from workflow.test_notify import _TOKEN, _USER

    keys = night.root / "home/agents/.env"
    keys.parent.mkdir(exist_ok=True)
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")
    night.orchestrator("busy")
    for _ in range(3):
        night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    assert len(night.sent()) == 1
    assert night.pushes() == []
    (night.state / "agents.json").write_text("[]")
    for _ in range(2):
        night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    assert len(night.pushes()) == 1


def test_unknown_main_has_its_own_stretch_and_readable_main_resets_it(night: Workflow) -> None:
    from workflow.test_notify import _TOKEN, _USER

    keys = night.root / "home/agents/.env"
    keys.parent.mkdir(exist_ok=True)
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")
    folder = night.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    report = folder / "main-status"
    report.write_text("main: unknown, cannot read CI\n")
    for _ in range(2):
        night.run_script("wake", "--tick-result", "0", "", env=_at(12))
    assert night.pushes() == []
    for _ in range(2):
        night.run_script("wake", "--tick-result", "0", "", env=_at(12))
    [push] = night.pushes()
    assert "main: unknown, cannot read CI" in push.message
    report.write_text("main: pending\n")
    night.run_script("wake", "--tick-result", "1", "another failure", env=_at(12))
    report.write_text("main: unknown, cannot read CI\n")
    for _ in range(3):
        night.run_script("wake", "--tick-result", "0", "", env=_at(12))
    assert len(night.pushes()) == 2


def test_shadow_result_neither_counts_nor_rings(night: Workflow) -> None:
    for _ in range(4):
        night.run_script("wake", "--tick-result", "1", "failure",
                         env={**_at(12), "SHIPLINE_SHADOW": str(night.root / "shadow.log")})
    assert night.pushes() == []
    assert not (night.repo / ".local/workflow/tick-failures").exists()
    assert night.sent() == []



def test_a_missed_phone_push_is_retried_until_delivered_once(night: Workflow) -> None:
    from workflow.test_notify import _TOKEN, _USER

    keys = night.root / "home/agents/.env"
    keys.parent.mkdir(exist_ok=True)
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")
    for _ in range(2):
        night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    (night.state / "pushover-fails").touch()
    failed = night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    assert failed.returncode == 1
    assert len(night.pushes()) == 1
    [line] = night.missed_pushes.read_text().splitlines()
    assert "Workflow needs you" in line and "cannot fetch main" in line
    assert "orchestrator" in line
    (night.state / "pushover-fails").unlink()
    for _ in range(2):
        done = night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
        assert done.returncode == 0, done.stderr
    assert len(night.pushes()) == 2  # one failed attempt, then one delivered push


def test_an_undelivered_alert_to_an_open_orchestrator_is_retried(night: Workflow) -> None:
    night.orchestrator("busy")
    for _ in range(2):
        night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    (night.state / "send-fails").touch()
    failed = night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    assert failed.returncode == 1
    (night.state / "send-fails").unlink()
    delivered = night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    assert delivered.returncode == 0, delivered.stderr
    assert len(night.sent()) == 2
    assert night.pushes() == []
    assert not night.missed_pushes.exists()


@pytest.mark.parametrize("status", ["129", "130", "143"])
def test_an_operator_stop_neither_advances_nor_resets_a_failure_stretch(
    night: Workflow, status: str,
) -> None:
    for _ in range(2):
        night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    for _ in range(3):
        done = night.run_script("wake", "--tick-result", status, "stopped", env=_at(12))
        assert done.returncode == 0, done.stderr
    assert not night.missed_pushes.exists()
    night.run_script("wake", "--tick-result", "1", "cannot fetch main", env=_at(12))
    [line] = night.missed_pushes.read_text().splitlines()
    assert "Tick failed 3 runs in a row: cannot fetch main" in line


@pytest.fixture
def held_starts(workflow: Workflow) -> Workflow:
    from workflow.test_ticket_starts import _draft
    from workflow.test_notify import _TOKEN, _USER

    workflow.orchestrator("busy")
    for number in (7, 8):
        _draft(workflow, number, "README.md", body="## Files\n\nREADME.md\n")
    cache = workflow.state / "unread-usage.json"
    cache.write_text("{}")
    workflow.env["WORKFLOW_USAGE_CACHE"] = str(cache)
    keys = workflow.root / "home/agents/.env"
    keys.parent.mkdir(exist_ok=True)
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")
    return workflow


def _held_pick(workflow: Workflow, minute: int, *, hour: int = 12) -> None:
    from workflow.test_ticket_starts import _pick, _room_since

    moment = int(_at(hour)["WORKFLOW_NOW"]) + minute * 60
    _room_since(workflow, moment)
    _pick(workflow, moment)


def _held_wake(
    workflow: Workflow, minute: int, *, hour: int = 12, **extra: str
) -> subprocess.CompletedProcess[str]:
    at = {**_at(hour), **extra}
    at["WORKFLOW_NOW"] = str(int(at["WORKFLOW_NOW"]) + minute * 60)
    return _wake(workflow, at=at)


@pytest.mark.parametrize("minutes", [59, 60])
def test_unread_usage_is_quiet_for_the_first_hour(held_starts: Workflow, minutes: int) -> None:
    _held_pick(held_starts, 0)
    _held_pick(held_starts, minutes)
    assert _held_wake(held_starts, minutes).returncode == 0
    assert held_starts.sent() == []
    assert held_starts.pushes() == []


@pytest.mark.parametrize("hour", [0, 12])
def test_unread_usage_tells_a_busy_orchestrator_once_at_any_hour(
    held_starts: Workflow, hour: int,
) -> None:
    for minute in (0, 59, 61, 67, 95):
        _held_pick(held_starts, minute, hour=hour)
        done = _held_wake(held_starts, minute, hour=hour)
        assert done.returncode == 0, done.stderr
    [message] = held_starts.sent()
    assert "starts held: usage unread for 1 h, 2 tickets waiting" in message
    assert held_starts.pushes() == []


def test_unread_usage_rings_once_after_two_hours(held_starts: Workflow) -> None:
    for minute in (0, 61, 120, 126, 180):
        _held_pick(held_starts, minute)
        done = _held_wake(held_starts, minute)
        assert done.returncode == 0, done.stderr
    [push] = held_starts.pushes()
    assert push.message == "starts held: usage unread for 2 h, 2 tickets waiting"
    assert len(held_starts.sent()) == 1


def test_readable_usage_ends_the_alarm_once_and_a_new_stretch_rearms(held_starts: Workflow) -> None:
    for minute in (0, 61):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    cache = Path(held_starts.env["WORKFLOW_USAGE_CACHE"])
    # A readable window above the ceiling still ends the unread-usage alarm.
    cache.write_text(json.dumps({"five_hour": {
        "utilization": 95, "resets_at": "2026-10-14T05:10:00+00:00",
    }}))
    for minute in (67, 73):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    assert sum("starts resumed" in message for message in held_starts.sent()) == 1
    assert held_starts.pushes() == []
    cache.write_text("{}")
    for minute in (80, 141, 200):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    assert sum("starts held:" in message for message in held_starts.sent()) == 2
    assert len(held_starts.pushes()) == 1


@pytest.mark.parametrize("delivered", [False, True])
def test_an_interrupted_usage_alert_still_reports_the_end(
    held_starts: Workflow, delivered: bool,
) -> None:
    _held_pick(held_starts, 0)
    record = held_starts.repo / ".local/workflow/ticket-starts-usage.json"
    before_alert = record.read_text()
    _held_pick(held_starts, 61)
    # Publication succeeded, but the pick stopped before saving its new flags.
    record.write_text(before_alert)
    if delivered:
        assert _held_wake(held_starts, 61).returncode == 0
    Path(held_starts.env["WORKFLOW_USAGE_CACHE"]).write_text(json.dumps({"five_hour": {
        "utilization": 95, "resets_at": "2026-10-14T05:10:00+00:00",
    }}))
    for minute in (67, 73):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    assert sum("starts held:" in message for message in held_starts.sent()) == 1
    assert sum("starts resumed" in message for message in held_starts.sent()) == 1
    assert held_starts.pushes() == []


@pytest.mark.parametrize("reason", ["no tickets", "load", "plan", "ticket plan", "ceiling"])
def test_other_start_holds_never_raise_the_unread_alarm(held_starts: Workflow, reason: str) -> None:
    if reason == "no tickets":
        for number in (7, 8):
            held_starts.set_pull_request(number, "CLOSED", held_starts.git("rev-parse", "HEAD"))
    elif reason == "load":
        held_starts.loadavg_file.write_text("9.00 9.00 9.00 1/100 1234\n")
    elif reason in ("plan", "ticket plan"):
        (held_starts.repo / ".local/workflow/start-plan").write_text(
            "hold all\n" if reason == "plan" else "hold 7\nhold 8\n"
        )
    else:
        Path(held_starts.env["WORKFLOW_USAGE_CACHE"]).write_text(json.dumps({"five_hour": {
            "utilization": 95, "resets_at": "2026-10-14T05:10:00+00:00",
        }}))
    for minute in (0, 61, 120, 180):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    assert held_starts.sent() == []
    assert held_starts.pushes() == []


def test_unread_usage_alert_retries_delivery(held_starts: Workflow) -> None:
    _held_pick(held_starts, 0)
    _held_pick(held_starts, 61)
    (held_starts.state / "send-fails").touch()
    assert _held_wake(held_starts, 61).returncode == 1
    (held_starts.state / "send-fails").unlink()
    assert _held_wake(held_starts, 67).returncode == 0
    assert "starts held:" in held_starts.sent()[-1]
    _held_pick(held_starts, 120)
    (held_starts.state / "pushover-fails").touch()
    assert _held_wake(held_starts, 120).returncode == 1
    (held_starts.state / "pushover-fails").unlink()
    assert _held_wake(held_starts, 126).returncode == 0
    assert _held_wake(held_starts, 132).returncode == 0
    assert len(held_starts.pushes()) == 2  # failed attempt, then one delivery


def test_shadow_wake_neither_rings_nor_acknowledges_the_alarm(held_starts: Workflow) -> None:
    for minute in (0, 120):
        _held_pick(held_starts, minute)
    shadow = held_starts.root / "shadow.log"
    assert _held_wake(held_starts, 120, SHIPLINE_SHADOW=str(shadow)).returncode == 0
    assert held_starts.sent() == []
    assert held_starts.pushes() == []
    assert _held_wake(held_starts, 120).returncode == 0
    assert "starts held:" in held_starts.sent()[-1]
    assert len(held_starts.pushes()) == 1


def test_another_hold_interrupts_the_unread_stretch(held_starts: Workflow) -> None:
    _held_pick(held_starts, 0)
    _held_pick(held_starts, 59)
    held_starts.loadavg_file.write_text("9.00 9.00 9.00 1/100 1234\n")
    _held_pick(held_starts, 61)
    held_starts.loadavg_file.write_text("0.50 0.40 0.30 1/100 1234\n")
    for minute in (70, 129):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    assert held_starts.sent() == []
    _held_pick(held_starts, 131)
    assert _held_wake(held_starts, 131).returncode == 0
    assert "usage unread for 1 h" in held_starts.sent()[-1]
    assert held_starts.pushes() == []


def test_the_phone_alarm_does_not_need_an_open_orchestrator(held_starts: Workflow) -> None:
    (held_starts.state / "agents.json").write_text("[]")
    for minute in (0, 120, 126):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    assert held_starts.sent() == []
    assert len(held_starts.pushes()) == 1
    held_starts.orchestrator("busy")
    assert _held_wake(held_starts, 132).returncode == 0
    assert "starts held:" in held_starts.sent()[-1]


def test_parallel_wakes_ring_the_unread_usage_alarm_once(held_starts: Workflow) -> None:
    for minute in (0, 61):
        _held_pick(held_starts, minute)
        assert _held_wake(held_starts, minute).returncode == 0
    _held_pick(held_starts, 120)
    env = {**held_starts.env, **_at(14)}
    command = ["bash", str(held_starts.script.parent / "wake")]
    first = subprocess.Popen(command, cwd=held_starts.repo, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(command, cwd=held_starts.repo, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.kill()
                process.wait()
    assert len(held_starts.pushes()) == 1


def test_a_failed_phone_push_does_not_silence_the_orchestrator_alert(held_starts: Workflow) -> None:
    for minute in (0, 120):
        _held_pick(held_starts, minute)
    (held_starts.state / "pushover-fails").touch()
    assert _held_wake(held_starts, 120).returncode == 1
    [message] = held_starts.sent()
    assert "starts held: usage unread for 2 h, 2 tickets waiting" in message
    (held_starts.state / "pushover-fails").unlink()
    assert _held_wake(held_starts, 126).returncode == 0
    assert len(held_starts.pushes()) == 2
    assert len(held_starts.sent()) == 1
