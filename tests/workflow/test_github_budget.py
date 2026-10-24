"""Background clients keep their reserve and revalidate cached responses with fake gh."""

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from workflow_harness import Workflow, instruction

_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _ROOT / "scripts/workflow/github_budget.py"
_FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['HOME'])
with (root / 'calls').open('a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\\n')
answer = json.loads((root / 'answer').read_text())
if answer.get('hold'):
    import fcntl, time
    with (root / 'active.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        file = root / 'active.json'
        saved = (json.loads(file.read_text()) if file.exists()
                 else {'active': 0, 'peak': 0, 'starts': []})
        saved['active'] += 1
        saved['peak'] = max(saved['peak'], saved['active'])
        saved['starts'].append(time.time())
        file.write_text(json.dumps(saved))
    time.sleep(answer['hold'])
    with (root / 'active.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(file.read_text())
        saved['active'] -= 1
        file.write_text(json.dumps(saved))
if answer.get('stall'):
    import time
    time.sleep(2)
print('* Request at now', file=sys.stderr)
print('* Request to hidden', file=sys.stderr)
print('> Authorization: token THIS-MUST-NOT-BE-SAVED', file=sys.stderr)
print('PRIVATE-REQUEST-BODY', file=sys.stderr)
print('< HTTP/2.0 ' + str(answer.get('status', 200)) + ' Reply', file=sys.stderr)
for key, value in answer.get('headers', {}).items():
    print('< ' + key + ': ' + str(value), file=sys.stderr)
print('', file=sys.stderr)
print(answer.get('debug_body', answer.get('body', '')), file=sys.stderr)
print('* Request took 1ms', file=sys.stderr)
for page in answer.get('more', []):
    print('* Request at next', file=sys.stderr)
    print('< HTTP/2.0 200 OK', file=sys.stderr)
    print('< Etag: "last-page"', file=sys.stderr)
    print('', file=sys.stderr)
    print(json.dumps(page), file=sys.stderr)
    print('* Request took 1ms', file=sys.stderr)
print(answer.get('diagnostic', ''), file=sys.stderr, end='')
print(answer.get('body', ''), end='')
sys.exit(answer.get('exit', 0))
"""


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("budget_test", _HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host(programs_folder: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The fake gh and the installed wrapper run from this home, so it cannot be tmp_path:
    # CI puts that in /dev/shm, where nothing runs.
    monkeypatch.setenv("HOME", str(programs_folder))
    monkeypatch.delenv("WORKFLOW_GH", raising=False)
    monkeypatch.delenv("WORKFLOW_GITHUB_RAW_GH", raising=False)
    bin_folder = programs_folder / "bin"
    bin_folder.mkdir()
    gh = bin_folder / "gh"
    gh.write_text(_FAKE)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_folder}:{os.environ['PATH']}")
    return programs_folder


def _answer(host: Path, *, remaining: int = 4900, status: int = 200, **extra: Any) -> None:
    (host / "answer").write_text(
        json.dumps(
            {
                "status": status,
                "headers": {
                    "X-Ratelimit-Remaining": remaining,
                    "X-Ratelimit-Reset": 2_000_000_000,
                    "X-Ratelimit-Resource": "core",
                    "Etag": '"v1"',
                },
                "body": '[{"id": 1}]',
                **extra,
            }
        )
    )


def _run(operation: str = "call", *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_HELPER), operation, "test", *arguments],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_low_response_stops_other_processes_once_until_the_server_reset(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answer(host, remaining=499)
    first = _run("call", "api", "repos/owner/repo/actions/runners")
    assert first.returncode == 0
    assert first.stdout == '[{"id": 1}]'
    second = _run("call", "api", "repos/owner/repo/actions/runs")
    third = _run("ready")
    assert second.returncode == third.returncode == 75
    assert second.stderr.count("budget low") == 1
    assert third.stderr == ""
    assert len((host / "calls").read_text().splitlines()) == 1
    module = _module()
    monkeypatch.setattr(module.time, "time", lambda: 2_000_000_002)
    assert module.delay("test") == 0


def test_wait_sleeps_without_polling_github(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(host, remaining=0)
    _run("call", "api", "repos/owner/repo/actions/runners")
    module = _module()
    clock = [1_999_999_970.0]
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "time", lambda: clock[0])

    def pause(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(module.time, "sleep", pause)
    module.wait("test")
    assert sleeps == [31]
    assert len((host / "calls").read_text().splitlines()) == 1


def test_conditional_read_returns_the_whole_cached_stdout_on_304(host: Path) -> None:
    _answer(host)
    first = _run("call", "api", "--paginate", "repos/owner/repo/pulls/1/reviews")
    _answer(host, status=304, body="", exit=1, diagnostic="gh: no JSON on 304\n")
    second = _run("call", "api", "--paginate", "repos/owner/repo/pulls/1/reviews")
    assert first.returncode == second.returncode == 0
    assert second.stdout == first.stdout
    assert second.stderr == ""
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert calls[1][-2:] == ["-H", 'If-None-Match: "v1"']
    log = (host / ".local/state/workflow/github-budget/requests.jsonl").read_text()
    assert json.loads(log.splitlines()[-1])["unchanged"] == 1


def test_changed_etag_updates_the_response_without_delaying_a_poll(host: Path) -> None:
    _answer(host)
    _run("call", "api", "repos/owner/repo/actions/runners")
    _answer(host, body='[{"id": 2}]', headers={"Etag": '"v2"'})
    changed = _run("call", "api", "repos/owner/repo/actions/runners")
    assert changed.stdout == '[{"id": 2}]'
    _answer(host, status=304, body="")
    assert _run("call", "api", "repos/owner/repo/actions/runners").stdout == changed.stdout
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert calls[-1][-1] == 'If-None-Match: "v2"'


def test_a_failed_read_never_uses_a_cached_success(host: Path) -> None:
    _answer(host)
    _run("call", "api", "repos/owner/repo/actions/runners")
    _answer(host, status=403, body="", exit=1, diagnostic="gh: forbidden (HTTP 403)\n")
    failed = _run("call", "api", "repos/owner/repo/actions/runners")
    assert failed.returncode == 1
    assert failed.stdout == ""
    assert failed.stderr == "gh: forbidden (HTTP 403)\n"
    assert _run("ready").returncode == 0


def test_a_multi_page_response_is_never_revalidated_using_only_its_first_page(host: Path) -> None:
    _answer(host, headers={"Etag": '"first"', "Link": '<https://api.github.com/next>; rel="next"'})
    _run("call", "api", "--paginate", "repos/owner/repo/actions/runners")
    _answer(host)
    _run("call", "api", "--paginate", "repos/owner/repo/actions/runners")
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert all("-H" not in call for call in calls)


def test_a_single_page_becoming_multiple_pages_discards_the_old_collection_cache(
    host: Path,
) -> None:
    _answer(host)
    args = ("api", "--paginate", "repos/owner/repo/pulls/1/reviews?per_page=2")
    assert _run("call", *args).returncode == 0
    _answer(
        host,
        body='[{"id": 1}, {"id": 2}]\n[{"id": 3}]',
        debug_body='[{"id": 1}, {"id": 2}]',
        more=[[{"id": 3}]],
        headers={"Etag": '"v2"', "Link": '<https://api.github.com/next>; rel="next"'},
    )
    assert _run("call", *args).stdout.endswith('[{"id": 3}]')
    _answer(host)
    assert _run("call", *args).returncode == 0
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert calls[1][-2:] == ["-H", 'If-None-Match: "v1"']
    assert "-H" not in calls[2]


def test_a_full_single_page_cannot_hide_new_items_on_a_second_page(host: Path) -> None:
    # ETags of array endpoints may cover only the body. A new page does not change
    # a full first page's body, even though its formerly absent Link header changes.
    _answer(host, body='[{"id": 1}, {"id": 2}]')
    args = ("api", "--paginate", "repos/owner/repo/pulls/1/reviews?per_page=2")
    for _ in range(2):
        assert _run("call", *args).returncode == 0
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert all("-H" not in call for call in calls)


@pytest.mark.parametrize("arguments", [("-X", "POST"), ("-f", "labels[]=first"), ("--input", "-")])
def test_mutations_are_never_cached(host: Path, arguments: tuple[str, ...]) -> None:
    _answer(host)
    for _ in range(2):
        assert (
            _run("call", "api", "repos/owner/repo/actions/runners/1/labels", *arguments).returncode
            == 0
        )
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert all("-H" not in call for call in calls)


def test_secondary_limit_uses_retry_after_without_retrying(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    _answer(host, status=429, exit=1, headers={"Retry-After": 120}, diagnostic="gh: rate limit\n")
    assert module.call("test", "api", "repos/owner/repo/actions/runners").returncode == 1
    assert module.delay("test") == 121
    with pytest.raises(module.Limited):
        module.call("test", "api", "repos/owner/repo/actions/runners")
    assert len((host / "calls").read_text().splitlines()) == 1


def test_http_debugging_never_reaches_diagnostics_or_telemetry(host: Path) -> None:
    _answer(host)
    done = _run("call", "api", "repos/owner/repo/actions/runners")
    assert done.stderr == ""
    for path in (host / ".local").rglob("*"):
        if path.is_file():
            assert "THIS-MUST-NOT-BE-SAVED" not in path.read_text()
            assert "PRIVATE-REQUEST-BODY" not in path.read_text()


@pytest.mark.parametrize("script", ["tick", "wake"])
def test_tick_and_wake_leave_the_reserve_without_calling_github(
    workflow: Workflow, script: str
) -> None:
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget"
    budget.mkdir(parents=True)
    (budget / "budget.json").write_text(
        json.dumps({"core": {"remaining": 499, "reset": 2_000_000_000}})
    )
    first = workflow.run_script(script)
    second = workflow.run_script(script)
    assert first.returncode == second.returncode == 0
    # Tick also gives wake a chance to deliver local notifications.
    assert first.stderr.count("budget low") == (2 if script == "tick" else 1)
    assert second.stderr == ""
    assert workflow.calls("gh") == []


def test_tick_batches_metadata_for_many_active_reviewers(workflow: Workflow) -> None:
    for number in (7, 8, 9):
        workflow.add_worktree(number, draft=False)
        workflow.running(f"review-{number}")
        notes = workflow.notes(number)
        notes.mkdir(parents=True)
        (notes / f"review-{number}.started").touch()
        (notes / f"review-{number}.checks-wait").touch()
        workflow.screen(
            "● Waiting for checks.\n\n" + "─" * 40 + "\n> \n" + "─" * 40, f"review-{number}"
        )
        (workflow.state / f"rollup-{number}.json").write_text(
            json.dumps(
                [
                    {
                        "__typename": "CheckRun",
                        "name": "CI result",
                        "status": "IN_PROGRESS",
                        "conclusion": None,
                    }
                ]
            )
        )
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh pr list --state open --limit 1000")) == 1
    assert workflow.calls("gh pr view") == []
    assert workflow.calls("tmux review-7 send-keys") == []
    assert workflow.calls("tmux review-8 send-keys") == []
    assert workflow.calls("tmux review-9 send-keys") == []


def test_absent_snapshot_entry_requires_a_successful_live_read_before_cleanup(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    snapshot = workflow.root / "snapshot.json"
    snapshot.write_text("{}")
    failure = workflow.state / "fail-gh-checks"
    failure.touch()
    first = workflow.run("cleanup", env={"WORKFLOW_PR_SNAPSHOT": str(snapshot)})
    assert first.returncode == 0, first.stderr
    assert worktree.exists()
    assert "cannot read pull request 7" in first.stderr
    failure.unlink()
    second = workflow.run("cleanup", env={"WORKFLOW_PR_SNAPSHOT": str(snapshot)})
    assert second.returncode == 0, second.stderr
    assert not worktree.exists()
    assert len(workflow.calls("gh pr view 7")) == 2


def test_budgeted_reads_preserve_timeout_retries_and_the_gate_exception(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", [str(_HELPER.parent), *sys.path])
    commands = importlib.import_module("workflow_commands")
    monkeypatch.setenv("WORKFLOW_BACKGROUND_GITHUB", "1")
    _answer(host, stall=True)
    with pytest.raises(commands.CommandTimeout):
        commands.github_read("api", "repos/owner/repo/actions/runners", timeout=0.2, delays=(0,))
    assert len((host / "calls").read_text().splitlines()) == 2


@pytest.mark.parametrize("status,retry", [(403, None), (403, 120), (429, None), (429, 120)])
def test_secondary_limits_pause_even_with_thousands_of_primary_requests_left(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    retry: int | None,
) -> None:
    module = _module()
    clock = [1000.0]
    real_time = module.time
    clock_time = SimpleNamespace(
        time=lambda: clock[0],
        sleep=real_time.sleep,
        gmtime=real_time.gmtime,
        strftime=real_time.strftime,
    )
    monkeypatch.setattr(module, "time", clock_time)
    _answer(
        host,
        status=status,
        remaining=3072,
        exit=1,
        body='{"message":"You have exceeded a secondary rate limit"}',
        diagnostic="gh: API rate limit exceeded (HTTP 403)\n",
    )
    if retry is not None:
        path = host / "answer"
        answer = json.loads(path.read_text())
        answer["headers"]["Retry-After"] = retry
        path.write_text(json.dumps(answer))
    assert module.call("test", "api", "repos/owner/repo/actions/runners").returncode == 1
    expected = (retry or 60) + 1
    assert module.delay("test") == expected
    assert module.delay("test") == expected
    assert capsys.readouterr().err.count("requests pause") == 1
    with pytest.raises(module.Limited):
        module.call("test", "api", "repos/owner/repo/actions/runners")
    assert len((host / "calls").read_text().splitlines()) == 1

    # Foreground sessions share secondary backoff, while retaining the primary reserve.
    def pause(seconds: float) -> None:
        assert len((host / "calls").read_text().splitlines()) == 1
        clock[0] += seconds

    clock_time.sleep = pause
    _answer(host, remaining=3071)
    assert (
        module.call("test", "api", "repos/owner/repo/actions/runners", background=False).returncode
        == 0
    )
    assert clock[0] >= 1000 + expected
    assert len((host / "calls").read_text().splitlines()) == 2


def test_a_small_per_minute_resource_does_not_stop_background_work(host: Path) -> None:
    # A session's REST search answers from its own 30-a-minute resource. It is never
    # near the hourly reserve, so it must not pause the supervisors and the tick.
    module = _module()
    module.save(
        module.folder() / "budget.json",
        {
            "core": {"remaining": 4000, "reset": 2_000_000_000},
            "search": {"remaining": 29, "reset": 2_000_000_000},
        },
    )
    assert module.delay("background") == 0
    module.save(
        module.folder() / "budget.json", {"search": {"remaining": 0, "reset": 2_000_000_000}}
    )
    assert module.delay("background") > 0


def test_foreground_can_use_the_reserve_while_background_calls_wait(host: Path) -> None:
    module = _module()
    module.save(
        module.folder() / "budget.json", {"core": {"remaining": 499, "reset": 2_000_000_000}}
    )
    _answer(host, remaining=498)
    with pytest.raises(module.Limited):
        module.call("background", "api", "repos/owner/repo/actions/runners")
    assert (
        module.call(
            "sessions", "api", "repos/owner/repo/actions/runners", background=False
        ).returncode
        == 0
    )
    assert len((host / "calls").read_text().splitlines()) == 1


def test_parallel_programs_share_two_slots_and_spread_their_starts(host: Path) -> None:
    _answer(host, hold=0.5)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(_HELPER),
                "call",
                f"program-{n}",
                "api",
                "repos/owner/repo/actions/runners",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(5)
    ]
    try:
        for process in processes:
            out, err = process.communicate(timeout=10)
            assert process.returncode == 0, out + err
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    active = json.loads((host / "active.json").read_text())
    assert active["peak"] == 2
    assert active["active"] == 0
    assert len(active["starts"]) == 5


def test_polling_jitter_breaks_fixed_phase_and_keeps_the_existing_average() -> None:
    module = _module()
    samples = [module.poll_delay(5) for _ in range(100)]
    assert all(4 <= value <= 6 for value in samples)
    assert len(set(samples)) > 90


def test_shared_admission_spaces_starts_on_the_host_clock(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    clock = [1000.0]

    def pause(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0], sleep=pause))
    stamps: list[float] = []
    for _ in range(3):
        with module.admitted(("api", "repos/owner/repo/actions/runners")):
            stamps.append(clock[0])
    assert all(0.099 <= later - earlier <= 0.301 for earlier, later in pairwise(stamps))


@pytest.mark.parametrize(
    "arguments",
    [
        ("pr", "merge", "7"),
        ("pr", "create", "--draft"),
        ("workflow", "run", "ci.yml"),
        ("run", "rerun", "10"),
        ("api", "-X", "POST", "repos/owner/repo/pulls/7/reviews"),
    ],
)
def test_changes_on_github_start_a_second_or_more_apart(
    host: Path, monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    """The workflow tests skip these waits (workflow_harness.py); this keeps them honest."""
    module = _module()
    clock = [1000.0]

    def pause(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0], sleep=pause))
    stamps: list[float] = []
    for _ in range(3):
        with module.admitted(arguments):
            stamps.append(clock[0])
    assert all(1.0 <= later - earlier <= 1.5 for earlier, later in pairwise(stamps))


def test_an_inflight_response_cannot_shorten_another_retry_after(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.setattr(module.time, "time", lambda: 1000)

    def execute(
        command: list[str], _env: dict[str, str], _seconds: float | None, _fd: int
    ) -> subprocess.CompletedProcess[str]:
        # Another outstanding request has already installed the longer cooldown.
        module.save(module.folder() / "budget.json", {"secondary": {"remaining": 0, "reset": 1120}})
        trace = (
            "* Request at now\n< HTTP/2.0 403 Forbidden\n< Retry-After: 60\n\n"
            '{"message":"secondary rate limit"}\n* Request took 1ms\n'
        )
        return subprocess.CompletedProcess(command, 1, "", trace)

    assert (
        module.call("test", "api", "repos/owner/repo/actions/runners", execute=execute).returncode
        == 1
    )
    assert module.delay("test") == 121


def test_secondary_limit_is_recognized_from_the_response_body_without_cli_text(host: Path) -> None:
    _answer(
        host,
        status=403,
        remaining=3072,
        exit=1,
        body='{"message":"You have exceeded a secondary rate limit"}',
        diagnostic="",
    )
    done = _run("call", "api", "repos/owner/repo/actions/runners")
    assert done.returncode == 1
    assert done.stderr.count("requests pause") == 1
    assert _run("ready").returncode == 75
    assert len((host / "calls").read_text().splitlines()) == 1


def test_foreground_wrapper_preserves_binary_downloads(host: Path) -> None:
    fake = host / "bin/gh"
    fake.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(b'PK\\xff\\r\\n\\x00')\n"
    )
    result = subprocess.run(
        [sys.executable, str(_HELPER), "foreground", "api", "repos/o/r/actions/artifacts/1/zip"],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == b"PK\xff\r\n\x00"


def test_low_budget_wake_still_delivers_local_disk_warning(workflow: Workflow) -> None:
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget"
    budget.mkdir(parents=True)
    (budget / "budget.json").write_text(
        json.dumps({"core": {"remaining": 499, "reset": 2_000_000_000}})
    )
    workflow.orchestrator("busy")
    df = workflow.bin / "df"
    df.write_text(
        "#!/bin/sh\necho 'Filesystem Inodes IUsed IFree IUse% Mounted on'\n"
        "echo 'tmp 1000 900 100 90% /tmp'\n"
    )
    result = workflow.run_script("wake", env={"WORKFLOW_NOW": "1791914400"})
    assert result.returncode == 0, result.stderr
    assert workflow.calls("gh") == []
    [message] = workflow.sent()
    assert "WARNING above 80%" in message
    assert "Open pull requests: could not be read" in message


@pytest.mark.parametrize("script", ["wake", "tick"])
def test_backoff_keeps_local_warning_delivery(workflow: Workflow, script: str) -> None:
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget"
    budget.mkdir(parents=True)
    (budget / "budget.json").write_text(
        json.dumps({"secondary": {"remaining": 0, "reset": 2_000_000_000}})
    )
    workflow.orchestrator("busy")
    (workflow.bin / "df").write_text(
        "#!/bin/sh\necho 'Filesystem Inodes IUsed IFree IUse% Mounted on'\n"
        "echo 'tmp 1000 900 100 90% /tmp'\n"
    )
    done = workflow.run_script(script, env={"WORKFLOW_NOW": "1791914400"})
    assert done.returncode == 0, done.stderr
    assert workflow.calls("gh") == []
    assert "WARNING above 80%" in workflow.sent()[0]


def test_a_budget_pause_still_resends_after_a_usage_reset_and_tells_the_wake(
    workflow: Workflow,
) -> None:
    # Resending needs no GitHub. A pause that lasts until the hourly reset must not hold
    # a session stopped on its usage limit for that hour.
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(
        "  ⎿  You've hit your session limit · resets 3am (UTC)\n" + "─" * 40 + "\n> \n",
        "ticket-7",
    )
    workflow.orchestrator("idle")
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget"
    budget.mkdir(parents=True)
    (budget / "budget.json").write_text(
        json.dumps({"core": {"remaining": 499, "reset": 2_000_000_000}})
    )
    moment = {"WORKFLOW_NOW": "1792119720", "TZ": "UTC"}  # 2026-10-16 03:02 UTC
    done = workflow.run_script("tick", env=moment)
    assert done.returncode == 0, done.stderr
    assert workflow.calls("gh") == []
    assert workflow.calls("tmux ticket-7 send-keys -t =ticket-7: -l ") == [
        "tmux ticket-7 send-keys -t =ticket-7: -l " + instruction("Implement pull request 7")
    ]
    assert any("Sent their instruction again: ticket-7." in sent for sent in workflow.sent())


@pytest.mark.parametrize("run", [False, True])
def test_watch_releases_admission_between_polls(
    host: Path, monkeypatch: pytest.MonkeyPatch, run: bool
) -> None:
    import fcntl

    module = _module()
    if run:
        _answer(host, body='{"status":"in_progress","conclusion":""}')
    else:
        _answer(host, body="CI\tpending\t0\turl\n", exit=8)
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        # Admission's own start-spacing sleeps may occur before the watch pause.
        if seconds < 1:
            return
        pauses.append(seconds)
        for slot in module.folder().glob("call-*.lock"):
            with slot.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if run:
            _answer(host, body='{"status":"completed","conclusion":"failure"}')
        else:
            _answer(host, body="CI\tpass\t1s\turl\n")

    monkeypatch.setattr(module.time, "sleep", pause)
    args = ("run", "watch", "123", "--exit-status") if run else ("pr", "checks", "7", "--watch")
    done = module.call("sessions", *args, background=False)
    assert done.returncode == (1 if run else 0)
    assert len(pauses) == 1
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert len(calls) == 2
    assert all("watch" not in call and "--watch" not in call for call in calls)


@pytest.mark.parametrize("fail_fast", [False, True])
def test_checks_watch_keeps_waiting_after_a_failure_unless_fail_fast(
    host: Path, monkeypatch: pytest.MonkeyPatch, fail_fast: bool
) -> None:
    module = _module()
    _answer(host, body="Python\tfail\t1s\turl\nWeb\tpending\t0\turl\n", exit=1)

    def pause(seconds: float) -> None:
        if seconds >= 1:
            _answer(host, body="Python\tpass\t1s\turl\nWeb\tpass\t1s\turl\n")

    monkeypatch.setattr(module.time, "sleep", pause)
    flags = ("--fail-fast",) if fail_fast else ()
    done = module.call(
        "sessions",
        "-R",
        "owner/repo",
        "pr",
        "checks",
        "7",
        "--watch",
        *flags,
        background=False,
    )
    assert done.returncode == int(fail_fast)
    calls = [json.loads(line) for line in (host / "calls").read_text().splitlines()]
    assert len(calls) == (1 if fail_fast else 2)
    assert all(call[:4] == ["-R", "owner/repo", "pr", "checks"] for call in calls)


@pytest.mark.parametrize(
    "arguments", [("pr", "create"), ("api", "repos/owner/repo/actions/runners")]
)
@pytest.mark.parametrize("fragmented", [False, True])
def test_interactive_foreground_keeps_terminal_and_filters_debug(
    host: Path, arguments: tuple[str, ...], fragmented: bool
) -> None:
    import pty

    (host / "bin/gh").write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "assert all(x.isatty() for x in (sys.stdin, sys.stdout, sys.stderr))\n"
        "print('Title?', flush=True)\nanswer = input()\n"
        + (
            "sys.stderr.write('\\x1b'); sys.stderr.flush()\nimport time; time.sleep(0.05)\n"
            "print('[?25h* Request at now', file=sys.stderr)\n"
            if fragmented
            else "print('\\x1b[?25h* Request at now', file=sys.stderr)\n"
        )
        + "print('> Authorization: SECRET', file=sys.stderr)\n"
        "print('< HTTP/2.0 200 OK', file=sys.stderr)\n"
        "print('< X-Ratelimit-Remaining: 4001', file=sys.stderr)\n"
        "print('< X-Ratelimit-Reset: 2000000000', file=sys.stderr)\n"
        "print('< X-Ratelimit-Resource: core', file=sys.stderr)\n"
        "print('* Request took 1ms', file=sys.stderr)\n"
        "print('Created ' + answer, flush=True)\n"
    )
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            [sys.executable, str(_HELPER), "foreground", *arguments],
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
        ) as process:
            os.close(slave)
            slave = -1
            os.write(master, b"example title\n")
            output = bytearray()
            while True:
                try:
                    part = os.read(master, 4096)
                except OSError:
                    break
                if not part:
                    break
                output.extend(part)
            _, diagnostic = process.communicate(timeout=5)
            assert process.returncode == 0, diagnostic
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
    assert b"Created example title" in output
    assert b"SECRET" not in diagnostic
    state = json.loads((host / ".local/state/workflow/github-budget/budget.json").read_text())
    assert state["core"]["remaining"] == 4001
    key = _module()._cache_key(arguments)
    if key:
        assert not (host / ".local/state/workflow/github-budget" / key).exists()


def test_tick_does_not_repeat_reset_notification_when_wake_hits_backoff(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    rule = "─" * 40
    workflow.screen(
        f"You've hit your session limit · resets 4pm (America/New_York)\n{rule}\n> \n{rule}\n",
        "ticket-7",
    )
    workflow.orchestrator("busy")
    gh = workflow.bin / "gh"
    real_gh = gh.with_name("gh-wrapped")
    gh.rename(real_gh)
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget/budget.json"
    gh.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\n"
        "if sys.argv[1:3] == ['pr', 'list'] and '--jq' in sys.argv:\n"
        f"    p = Path({str(budget)!r}); p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    p.write_text(json.dumps({'secondary': {'remaining': 0, 'reset': 2000000000}}))\n"
        f"os.execv({str(real_gh)!r}, [{str(real_gh)!r}, *sys.argv[1:]])\n"
    )
    gh.chmod(0o755)
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": "1791921660", "TZ": "UTC"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux ticket-7 send-keys -t =ticket-7: -l ")) == 1
    [message] = workflow.sent()
    assert "ticket-7" in message
    assert budget.exists()
