"""Independent break tests for PR 79; transports return synthetic GitHub responses.

Run this file alone. Local runs should set --basetemp to a directory in the checkout.
No real credentials, network requests, or installed budget state are used.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    helper = Path(__file__).resolve().parents[2] / "scripts/workflow/github_budget.py"
    spec = importlib.util.spec_from_file_location("breaker_79_budget", helper)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(module, "folder", lambda: state)
    monkeypatch.setattr(module, "raw_gh", lambda: "example-gh")
    for key in (
        "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
        "SHIPLINE_SHADOW", module.SLOT_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(module=module, state=state)


def trace(status: int, headers: dict[str, Any] | None = None, message: str = "",
          body: Any = None) -> str:
    lines = ["* Request at now", "> Authorization: token EXAMPLE-REQUEST-CREDENTIAL",
             "EXAMPLE-PRIVATE-REQUEST-BODY", f"< HTTP/2.0 {status} Reply"]
    lines.extend(f"< {key}: {value}" for key, value in (headers or {}).items())
    payload = {"message": message} if body is None else body
    lines.extend(("", json.dumps(payload), "* Request took 1ms", ""))
    return "\n".join(lines)


def transport(status: int, headers: dict[str, Any] | None = None, message: str = "",
              *, body: Any = None, stdout: str = "example-output") -> Any:
    def execute(command: list[str], env: dict[str, str], timeout: float | None,
                slot: int) -> subprocess.CompletedProcess[str]:
        assert env["GH_DEBUG"] == "api"
        assert timeout == 30
        assert slot >= 0
        return subprocess.CompletedProcess(command, int(status >= 400), stdout,
                                           trace(status, headers, message, body))
    return execute


def rows(budget: SimpleNamespace) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (budget.state / "requests.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("status,headers,message,limited", [
    (403, {}, "Permission to inspect rate limit settings denied", False),
    (403, {}, "Forbidden", False),
    (500, {}, "API rate limit exceeded", False),
    (429, {}, "Slow down", True),
    (403, {"Retry-After": "90"}, "Forbidden", True),
    (403, {"X-RateLimit-Remaining": "0"}, "Forbidden", True),
    (403, {}, "You have exceeded a secondary rate limit", True),
    (403, {}, "API rate limit exceeded", True),
])
def test_classifies_http_responses_without_cli_keyword_false_positives(
    budget: SimpleNamespace, status: int, headers: dict[str, str], message: str, limited: bool,
) -> None:
    module = budget.module
    base = transport(status, headers, message)

    def execute(*args: Any) -> subprocess.CompletedProcess[str]:
        done = base(*args)
        done.stderr += "gh: operation on rate limit settings failed\n"
        return done

    done = module.call("example-app", "pr", "checks", "7", execute=execute)
    assert done.returncode == 1
    assert done.stdout == "example-output"
    assert done.stderr == "gh: operation on rate limit settings failed\n"
    assert bool(module.delay("example-poller")) is limited
    assert len(rows(budget)) == 1
    assert bool(rows(budget)[0].get("throttled")) is limited


def test_timeout_does_not_poison_the_next_call_or_leave_a_slot_locked(budget: SimpleNamespace) -> None:
    module = budget.module

    def timeout(command: list[str], env: dict[str, str], seconds: float, slot: int) -> Any:
        raise subprocess.TimeoutExpired(command, seconds, stderr="rate limit lookup timed out")

    with pytest.raises(subprocess.TimeoutExpired):
        module.call("example-app", "pr", "checks", "7", execute=timeout)
    assert module.delay("example-poller") == 0
    done = module.call("example-app", "pr", "checks", "7", execute=transport(200))
    assert done.returncode == 0
    assert len(rows(budget)) == 1
    for number in range(module.MAX_IN_FLIGHT):
        with (budget.state / f"call-{number}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_empty_or_interrupted_budget_file_does_not_invent_a_cooldown(
    budget: SimpleNamespace,
) -> None:
    module = budget.module
    assert module.delay("example-app") == 0
    (budget.state / "budget.json").write_text('{"core":')
    done = module.call("example-app", "pr", "checks", "7", execute=transport(200))
    assert done.returncode == 0
    assert module.delay("example-app") == 0
    assert json.loads((budget.state / "budget.json").read_text()) == {}


def test_legacy_secondary_state_expires_and_does_not_invent_a_source(
    budget: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    module = budget.module
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    module.save(budget.state / "budget.json", {"secondary": {"remaining": 0, "reset": 1060}})
    assert module.delay("example-app") == 61
    message = capsys.readouterr().err
    assert "GitHub's secondary limit;" in message
    assert "from" not in message
    clock[0] = 1062
    assert module.delay("example-app") == 0


def test_a_temporarily_held_state_lock_serializes_without_sending_a_request(
    budget: SimpleNamespace,
) -> None:
    module = budget.module
    attempted = threading.Event()
    sent = threading.Event()

    def request() -> Any:
        attempted.set()
        return module.call("example-app", "pr", "checks", "7", execute=execute)

    def execute(*args: Any) -> Any:
        sent.set()
        return transport(200)(*args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with (budget.state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            future = pool.submit(request)
            assert attempted.wait(2)
            assert not sent.wait(0.2)
        assert future.result(timeout=3).returncode == 0
    assert sent.is_set()
    assert len(rows(budget)) == 1


def test_two_inflight_limits_preserve_the_longer_deadline_and_its_source(
    budget: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    module = budget.module
    meeting = threading.Barrier(2, timeout=3)
    long_recorded = threading.Event()

    def request(name: str, retry: str) -> Any:
        def execute(*args: Any) -> Any:
            meeting.wait()
            if retry == "60":
                assert long_recorded.wait(3)
            return transport(429, {"Retry-After": retry}, "Slow down")(*args)
        try:
            return module.call(name, "pr", "checks", "7", execute=execute)
        finally:
            if retry == "180":
                long_recorded.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        long = pool.submit(request, "example-long", "180")
        short = pool.submit(request, "example-short", "60")
        assert long.result(timeout=5).returncode == 1
        assert short.result(timeout=5).returncode == 1
    state = module.read(budget.state / "budget.json")
    assert state["secondary"]["program"] == "example-long"
    assert 170 < module.delay("example-observer") <= 181
    assert "429 from example-long" in capsys.readouterr().err
    assert len(rows(budget)) == 2
    assert all(row["throttled"] for row in rows(budget))
    with pytest.raises(module.Limited):
        module.call("example-late", "pr", "checks", "7", execute=transport(200))
    assert len(rows(budget)) == 2


def test_hourly_reserve_does_not_block_foreground_and_search_does_not_keep_a_reserve(
    budget: SimpleNamespace,
) -> None:
    module = budget.module
    reset = int(time.time()) + 90
    module.save(budget.state / "budget.json", {
        "core": {"remaining": 499, "reset": reset},
        "search": {"remaining": 1, "reset": reset + 30},
    })
    assert module.delay("example-background") > 0
    assert module.delay("example-foreground", background=False) == 0


@pytest.mark.parametrize("arguments", [
    ("pr", "comment", "7", "--edit-last", "--body", "EXAMPLE-PRIVATE-PAYLOAD"),
    ("pr", "merge", "7", "-sdb", "EXAMPLE-PRIVATE-PAYLOAD"),
    ("api", "graphql", "-Fquery=EXAMPLE-PRIVATE-PAYLOAD", "--paginate"),
    ("api", "repos/example-org/example-app/issues", "--input=EXAMPLE-PRIVATE-PAYLOAD"),
    ("api", "repos/example-org/example-app/issues?body=EXAMPLE-PRIVATE-PAYLOAD"),
    ("api", "repos/example-org/example-app/issues", "--silent", "-H", "EXAMPLE-PRIVATE-PAYLOAD"),
])
def test_request_payloads_are_absent_from_state_and_report(
    budget: SimpleNamespace, arguments: tuple[str, ...], capsys: pytest.CaptureFixture[str],
) -> None:
    module = budget.module
    module.call("example-app", *arguments, execute=transport(429, message="Slow down"))
    module.report(60)
    output = capsys.readouterr().out
    saved = "".join(path.read_text() for path in budget.state.iterdir() if path.is_file())
    for private in ("EXAMPLE-PRIVATE-PAYLOAD", "EXAMPLE-REQUEST-CREDENTIAL",
                    "EXAMPLE-PRIVATE-REQUEST-BODY"):
        assert private not in saved + output


def test_rotated_and_truncated_logs_keep_recent_throttles_once(
    budget: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    module = budget.module
    now = time.time()
    base = {"program": "example-app", "requests": 1, "unchanged": 0, "core": 1, "graphql": 0}
    old = {**base, "at": now - 120, "throttled": True, "message": "old"}
    recent = {**base, "at": now - 5, "throttled": True, "status": 429, "message": "Slow down"}
    (budget.state / "requests.previous.jsonl").write_text(json.dumps(old) + "\n[]\n{partial\n")
    (budget.state / "requests.jsonl").write_text(
        json.dumps(recent) + '\n{"at":\n' + json.dumps({**base, "at": now}) + "\n"
    )
    module.report(60)
    output = capsys.readouterr().out
    assert "Malformed lines skipped: 3" in output
    assert "example-app\t2\t0\t2\t0" in output
    assert [json.loads(line) for line in output.splitlines() if line.startswith("{")] == [recent]


def test_a_lost_check_does_not_restore_a_cached_success_or_arm_a_pause(
    budget: SimpleNamespace,
) -> None:
    module = budget.module
    arguments = ("api", "repos/example-org/example-app/check-runs/7")
    success = {"status": "completed", "conclusion": "success"}
    first = module.call("example-app", *arguments, execute=transport(
        200, {"ETag": '"check-v1"'}, body=success, stdout=json.dumps(success)))
    assert json.loads(first.stdout) == success
    missing = module.call("example-app", *arguments, execute=transport(
        404, message="Check not found while reading rate limit settings", stdout=""))
    assert missing.returncode == 1
    assert missing.stdout == ""
    assert module.delay("example-app") == 0

    def reappeared(command: list[str], *rest: Any) -> Any:
        assert "If-None-Match:" not in " ".join(command)
        return transport(200, body=success, stdout=json.dumps(success))(command, *rest)

    assert module.call("example-app", *arguments, execute=reappeared).returncode == 0
    assert len(rows(budget)) == 3
    assert not any(row.get("throttled") for row in rows(budget))


def test_a_late_check_replaces_a_cached_empty_collection(budget: SimpleNamespace) -> None:
    module = budget.module
    arguments = ("api", "--paginate", "repos/example-org/example-app/commits/example-head/check-runs")
    empty = {"total_count": 0, "check_runs": []}
    module.call("example-app", *arguments, execute=transport(
        200, {"ETag": '"empty"'}, body=empty, stdout=json.dumps(empty)))
    late = {"total_count": 1, "check_runs": [{"id": 7, "status": "in_progress"}]}

    def arrived(command: list[str], *rest: Any) -> Any:
        assert "If-None-Match: \"empty\"" in command
        return transport(200, {"ETag": '"late"'}, body=late,
                         stdout=json.dumps(late))(command, *rest)

    done = module.call("example-app", *arguments, execute=arrived)
    assert json.loads(done.stdout) == late
    assert module.delay("example-app") == 0


def test_partial_rerun_growing_a_full_page_is_fetched_without_old_etag(
    budget: SimpleNamespace,
) -> None:
    module = budget.module
    arguments = ("api", "--paginate", "repos/example-org/example-app/actions/runs/7/jobs?per_page=2")
    jobs = {"total_count": 2, "jobs": [
        {"id": 7, "conclusion": "success"}, {"id": 8, "conclusion": "success"},
    ]}
    module.call("example-app", *arguments, execute=transport(
        200, {"ETag": '"first-page"'}, body=jobs, stdout=json.dumps(jobs)))
    pending = {"total_count": 3, "jobs": [{"id": 9, "status": "queued"}]}
    expected = json.dumps(jobs) + "\n" + json.dumps(pending)

    def rerun(command: list[str], *rest: Any) -> Any:
        assert not any(arg.startswith("If-None-Match:") for arg in command)
        first = trace(200, {"ETag": '"first-page"', "Link": '<next>; rel="next"'}, body=jobs)
        second = trace(200, {"ETag": '"second-page"'}, body=pending)
        return subprocess.CompletedProcess(command, 0, expected, first + second)

    done = module.call("example-app", *arguments, execute=rerun)
    assert done.stdout == expected
    assert rows(budget)[-1]["requests"] == 2
    assert module.delay("example-app") == 0


@pytest.mark.parametrize("subject_kind", ["user", "installation", "client"])
def test_primary_limit_subject_identifiers_are_not_persisted_or_reported(
    budget: SimpleNamespace, subject_kind: str, capsys: pytest.CaptureFixture[str],
) -> None:
    """GitHub's primary-limit message names a caller; all values here are synthetic."""
    module = budget.module
    identifier = str(42424242)
    if subject_kind == "client":
        identifier = ".".join(str(part) for part in (192, 0, 2, 1))
        subject = identifier
    else:
        subject = f"{subject_kind} ID {identifier}"
    message = f"API rate limit exceeded for {subject}."
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": int(time.time()) + 90,
               "X-RateLimit-Resource": "core"}
    done = module.call("example-app", "pr", "checks", "7",
                       execute=transport(403, headers, message))
    assert done.returncode == 1
    module.report(60)
    output = capsys.readouterr().out
    persisted = (budget.state / "requests.jsonl").read_text()
    # Use a fixed failure description; even synthetic identifiers need not enter reports.
    leaked = identifier in persisted or identifier in output
    assert not leaked, "primary-limit subject identifier was persisted and printed by report"
