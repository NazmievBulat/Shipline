"""Independent failure and recovery probes for PR 87; all outside services are stand-ins."""

import fcntl
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from workflow_harness import Workflow, install_shipline
from workflow.merge_gate_fixtures import _REPO, github as github_fixture

github = github_fixture


@pytest.fixture
def alarm(workflow: Workflow) -> Workflow:
    keys = workflow.root / "home/agents/.env"
    keys.parent.mkdir(exist_ok=True)
    keys.write_text("PUSHOVER_TOKEN=ExampleApplication\nPUSHOVER_USER=ExampleRecipient\n")
    keys.chmod(0o600)
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "main-status").write_text("main: green\n")
    workflow.env.update(WORKFLOW_NOW="1793894400", TZ="UTC")
    return workflow


def report(alarm: Workflow, status: int = 1, reason: str = "cannot fetch main"):
    return alarm.run_script("wake", "--tick-result", str(status), reason)


def main_state(alarm: Workflow, line: str) -> None:
    (alarm.repo / ".local/workflow/main-status").write_text(line + "\n")


@pytest.mark.parametrize("registration", [None, "", "closed-session"])
def test_absent_recipient_threshold_recovery_and_exact_fix(alarm, registration):
    if registration is not None:
        (alarm.repo / ".local/workflow/orchestrator").write_text(registration + "\n")
    for _ in range(2):
        assert report(alarm).returncode == 0
    assert alarm.pushes() == []
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 1
    message = alarm.pushes()[0].message
    assert "cannot fetch main" in message
    assert "write the open session's name to .local/workflow/orchestrator" in message
    assert report(alarm, 0, "").returncode == 0
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 2


def test_two_reports_at_once_spend_one_delivery(alarm):
    assert report(alarm).returncode == 0
    assert report(alarm).returncode == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: report(alarm), range(2)))
    assert [result.returncode for result in results] == [0, 0]
    assert len(alarm.pushes()) == 1
    counter = alarm.repo / ".local/workflow/tick-failures"
    assert counter.read_text().strip() == "3 1 0"
    assert counter.stat().st_mode & 0o777 == 0o600
    assert not list(counter.parent.glob("*.tmp"))


def test_open_recipient_then_closed_recipient_uses_both_channels_once(alarm):
    alarm.orchestrator("busy", name="example-session")
    for _ in range(4):
        assert report(alarm).returncode == 0
    assert len(alarm.sent()) == 1
    assert alarm.pushes() == []
    (alarm.state / "agents.json").write_text("[]")
    for _ in range(2):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 1


def test_independent_stretches_recover_without_resetting_each_other(alarm):
    main_state(alarm, "main: unknown, CI result missing after partial rerun")
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 1
    assert "Tick failed" in alarm.pushes()[0].message
    assert "CI result missing after partial rerun" in alarm.pushes()[0].message
    assert report(alarm, 0, "").returncode == 0
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 2
    assert "Main unknown" not in alarm.pushes()[1].message
    main_state(alarm, "main: green after rerun")
    assert report(alarm).returncode == 0
    main_state(alarm, "main: unknown, late check is unreadable")
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 3
    assert "Tick failed" not in alarm.pushes()[2].message
    assert "late check is unreadable" in alarm.pushes()[2].message


@pytest.mark.parametrize("contents", [None, ""])
def test_missing_or_empty_main_status_counts_unknown_on_successful_ticks(alarm, contents):
    path = alarm.repo / ".local/workflow/main-status"
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    for _ in range(3):
        assert report(alarm, 0, "").returncode == 0
    assert len(alarm.pushes()) == 1
    assert "Main unknown" in alarm.pushes()[0].message
    assert "Tick failed" not in alarm.pushes()[0].message


def test_failed_push_missing_keys_and_timeout_remain_pending(alarm):
    keys = alarm.root / "home/agents/.env"
    valid = keys.read_text()
    keys.unlink()
    for _ in range(2):
        assert report(alarm).returncode == 0
    assert report(alarm).returncode == 1
    assert alarm.pushes() == []
    assert "cannot fetch main" in alarm.missed_pushes.read_text()
    keys.write_text(valid)
    original = (alarm.bin / "curl").read_text()
    alarm.fake("curl", "#!/bin/bash\nexec /bin/sleep 30\n")
    alarm.env["WORKFLOW_NOTIFY_SECONDS"] = "0.15"
    started = time.monotonic()
    assert report(alarm).returncode == 1
    assert time.monotonic() - started < 5
    alarm.fake("curl", original)
    assert report(alarm).returncode == 0
    assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 1


def test_busy_alert_lock_releases_without_double_counting(alarm):
    lock = alarm.repo / ".local/workflow/workflow-alert.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        process = subprocess.Popen(
            ["bash", str(alarm.script.with_name("wake")), "--tick-result", "1", "cannot fetch main"],
            cwd=alarm.repo, env=alarm.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.25)
            assert process.poll() is None
            fcntl.flock(held, fcntl.LOCK_UN)
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert (lock.parent / "tick-failures").read_text().strip() == "1 0 0"


def test_alert_lock_held_past_its_bound_fails_without_corrupting_state(alarm):
    assert report(alarm).returncode == 0
    lock = alarm.repo / ".local/workflow/workflow-alert.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        assert report(alarm).returncode == 1
        assert 14 <= time.monotonic() - started < 25
    assert (lock.parent / "tick-failures").read_text().strip() == "1 0 0"
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 1


def test_session_listing_timeout_falls_back_to_the_phone(alarm):
    alarm.orchestrator("busy", name="example-session")
    alarm.fake("claude", "#!/bin/bash\nexec /bin/sleep 30\n")
    alarm.fake("timeout", '#!/bin/bash\n[[ $1 != 15 ]] || { shift; set -- 0.2 "$@"; }\n'
               'exec /usr/bin/timeout "$@"\n')
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 1
    assert alarm.sent() == []


@pytest.mark.parametrize("cache", [b"../escape\0", b"/invalid\0", b".\0", b"missing-terminator"])
def test_broken_config_never_trusts_invalid_cached_state_folders(alarm, cache):
    (alarm.repo / ".git/shipline-alert-state-folder").write_bytes(cache)
    (alarm.repo / "shipline.json").write_text("{")
    for _ in range(3):
        assert report(alarm).returncode == 0
    assert len(alarm.pushes()) == 1
    assert ".local/workflow/orchestrator" in alarm.pushes()[0].message
    assert not (alarm.root / "escape").exists()


def test_real_main_reads_lost_check_partial_rerun_and_later_outage(github, alarm):
    endpoint = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    saved = github.api.pop(endpoint)
    github.save()
    for _ in range(3):
        done = alarm.run_script("tick")
        assert done.returncode == 0, done.stderr
    assert len(alarm.pushes()) == 1
    assert "Main unknown" in alarm.pushes()[0].message
    assert "Tick failed" not in alarm.pushes()[0].message
    github.api[endpoint] = saved
    jobs = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]
    jobs[1].update(status="in_progress", conclusion=None)
    github.save()
    assert alarm.run_script("tick").returncode == 0
    assert not (alarm.repo / ".local/workflow/main-unknown-ticks").exists()
    jobs[1].update(status="completed", conclusion="success")
    github.save()
    assert alarm.run_script("tick").returncode == 0
    assert len(alarm.pushes()) == 1
    del github.api[endpoint]
    github.save()
    for _ in range(3):
        assert alarm.run_script("tick").returncode == 0
    assert len(alarm.pushes()) == 2


def test_notification_keys_stay_out_of_arguments_output_and_workflow_logs(alarm):
    for _ in range(3):
        done = report(alarm)
        assert done.returncode == 0
        assert done.stdout == done.stderr == ""
    [push] = alarm.pushes()
    log = alarm.root / "home/.local/state/workflow/wake.log"
    visible = "\n".join([*push.arguments, push.message, log.read_text()])
    assert "ExampleApplication" not in visible
    assert "ExampleRecipient" not in visible
    assert log.stat().st_mode & 0o777 == 0o600
    cache = alarm.repo / ".git/shipline-alert-state-folder"
    assert cache.stat().st_mode & 0o777 == 0o600


def test_error_shell_metacharacters_are_data_and_only_first_line_is_sent(alarm):
    reason = 'cannot fetch $(touch injected); `touch injected`; "quoted"\nsecond line'
    for _ in range(3):
        assert report(alarm, reason=reason).returncode == 0
    assert not (alarm.repo / "injected").exists()
    assert reason.splitlines()[0] in alarm.pushes()[0].message
    assert "second line" not in alarm.pushes()[0].message


def test_custom_installed_config_survives_failure_from_a_worktree(alarm):
    alarm.add_worktree(7)
    install_shipline(alarm)
    config = alarm.root / "installed-settings.json"
    values = json.loads((alarm.repo / "shipline.json").read_text())
    values["state_folder"] = ".local/example-state"
    config.write_text(json.dumps(values))
    alarm.env["SHIPLINE_CONFIG"] = str(config)
    custom = alarm.repo / ".local/example-state"
    custom.mkdir()
    (custom / "main-status").write_text("main: green\n")
    assert report(alarm, 0, "").returncode == 0
    config.write_text("{")
    for _ in range(3):
        done = alarm.run_script("wake", "--tick-result", "1", "configuration is not JSON",
                                cwd=alarm.worktree(7))
        assert done.returncode == 0, done.stderr
    assert len(alarm.pushes()) == 1
    assert ".local/example-state/orchestrator" in alarm.pushes()[0].message
    assert not (alarm.home / ".local/workflow/tick-failures").exists()


def test_real_ticks_skip_lock_contention_shadow_and_operator_stop(alarm):
    alarm.script.with_name("main-status").write_text("#!/bin/bash\nexit 0\n")
    alarm.break_session("list")
    for _ in range(2):
        assert alarm.run_script("tick").returncode != 0
    lock = alarm.repo / ".git/shipline-tick.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert alarm.run_script("tick").returncode == 0
    alarm.run_script("tick", "--shadow")
    assert report(alarm, 143, "stopped").returncode == 0
    assert alarm.pushes() == []
    assert alarm.run_script("tick").returncode != 0
    assert len(alarm.pushes()) == 1
    assert "session list failed" in alarm.pushes()[0].message


def test_a_harmless_stderr_warning_does_not_replace_the_failing_step_error(alarm):
    alarm.script.with_name("main-status").write_text("#!/bin/bash\nexit 0\n")
    alarm.add_pull_request(7)
    alarm.fake("worktree-setup", '#!/bin/bash\necho "worktree dependencies ready"\nexit 0\n')
    original = (alarm.bin / "tmux").read_text()
    alarm.fake("tmux-before-failure", original)
    alarm.fake(
        "tmux",
        '#!/bin/bash\nif [[ ${3:-} == new-session ]]; then\n'
        '  echo "cannot launch worker: session service unavailable" >&2\n  exit 1\nfi\n'
        'exec "$(dirname "$0")/tmux-before-failure" "$@"\n',
    )
    for _ in range(3):
        done = alarm.run_script("tick")
        assert done.returncode != 0
        assert "cannot launch worker: session service unavailable" in done.stderr
    message = alarm.pushes()[0].message
    if "cannot launch worker: session service unavailable" not in message:
        pytest.fail(f"phone alert replaced launch error with successful setup output: {message!r}", pytrace=False)
