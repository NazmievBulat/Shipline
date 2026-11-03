"""Independent breaker cases for PR 78; run this file alone.

Run with TMPDIR and pytest --basetemp inside the checkout. External programs are
local stand-ins; no model, application server or GitHub request is launched.
"""

import concurrent.futures
import fcntl
import json
import os
import subprocess

import pytest
from workflow_harness import install_shipline, make_workflow


@pytest.fixture
def example(tmp_path, monkeypatch):
    # Give even the disposable fixture commits the repository's required identity.
    original_run = subprocess.run

    def run(words, *args, **kwargs):
        if isinstance(words, (list, tuple)) and words and words[0] == "git":
            env = dict(kwargs.get("env", os.environ))
            for prefix in ("GIT_AUTHOR", "GIT_COMMITTER"):
                env[prefix + "_NAME"] = "Bulat Nazmiev"
                env[prefix + "_EMAIL"] = "82903523+NazmievBulat@users.noreply.github.com"
            kwargs["env"] = env
        return original_run(words, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    programs = tmp_path / "programs"
    programs.mkdir()
    made = make_workflow(tmp_path, programs)
    install_shipline(made)
    path = made.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["hunts"] = {
        "areas": {"first": ["/first"], "second": ["/second"]},
        "settings": {"FEATURE_EXAMPLE": "off", "SHARED_EXAMPLE": "keep"},
        "area_settings": {"first": {"FEATURE_EXAMPLE": "on"},
                          "second": {"FEATURE_EXAMPLE": "second"}},
    }
    path.write_text(json.dumps(config))
    made.fake("hunt-app", "#!/bin/bash\n"
              "python3 - \"$1\" <<'APP'\n"
              "import json, os, sys\n"
              "from pathlib import Path\n"
              "data = {key: os.environ.get(key) for key in "
              "('FEATURE_EXAMPLE', 'SHARED_EXAMPLE', 'LITERAL_EXAMPLE')}\n"
              "Path('example-' + sys.argv[1] + '.json').write_text(json.dumps(data))\n"
              "print(json.dumps(data))\n"
              "APP\n")
    return made


def configure(example, change):
    path = example.repo / "shipline.json"
    config = json.loads(path.read_text())
    change(config)
    path.write_text(json.dumps(config))


def snapshot(example, name="example"):
    return example.hunt_worktree(name) / ".local/workflow/hunt-settings.json"


def start(example, name="example", area=None):
    words = ["start", "hunt", name]
    if area:
        words.extend(["--area", area])
    return example.run(*words)


def test_settings_survive_config_edit_and_all_app_lifecycle_calls(example):
    assert start(example, area="first").returncode == 0
    configure(example, lambda c: c["hunts"].update(settings={"FEATURE_EXAMPLE": "changed"},
                                                   area_settings={}))
    nested = example.hunt_worktree("example") / "nested"
    nested.mkdir()
    for action in ("start", "fresh", "restart", "stop"):
        done = example.shipline("hunt-app", action, cwd=nested)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {"FEATURE_EXAMPLE": "on", "SHARED_EXAMPLE": "keep",
                                           "LITERAL_EXAMPLE": None}
    report = example.hunt_report("example") / "report.md"
    assert json.loads(report.read_text().split("```json\n")[1].split("\n```")[0]) == {
        "FEATURE_EXAMPLE": "on", "SHARED_EXAMPLE": "keep"}
    assert report.stat().st_mode & 0o777 == 0o600
    assert snapshot(example).stat().st_mode & 0o777 == 0o600
    assert "FEATURE_EXAMPLE=" not in (example.state / "env/hunt-example").read_text()


def test_no_area_keeps_shared_settings_and_ordinary_app_has_none(example):
    assert start(example).returncode == 0
    done = example.shipline("hunt-app", "start", cwd=example.hunt_worktree("example"))
    assert done.returncode == 0
    assert json.loads(done.stdout)["FEATURE_EXAMPLE"] == "off"
    ordinary = example.shipline("hunt-app", "start")
    assert ordinary.returncode == 0
    assert all(value is None for value in json.loads(ordinary.stdout).values())


def test_settings_are_literal_environment_data_and_fenced_report_data(example):
    value = '$(touch example-injected); `touch example-injected`; " \\ ```'
    configure(example, lambda c: c["hunts"]["settings"].update(LITERAL_EXAMPLE=value))
    assert start(example, area="first").returncode == 0
    done = example.shipline("hunt-app", "fresh", cwd=example.hunt_worktree("example"))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["LITERAL_EXAMPLE"] == value
    assert not (example.hunt_worktree("example") / "example-injected").exists()
    text = (example.hunt_report("example") / "report.md").read_text()
    assert text.count("```") == 2
    assert json.loads(text.split("```json\n")[1].split("\n```")[0])["LITERAL_EXAMPLE"] == value


@pytest.mark.parametrize("setting,value", [("bad-name", "example"), ("FEATURE_EXAMPLE", "a\nb"),
                                           ("FEATURE_EXAMPLE", "a\rb"), ("FEATURE_EXAMPLE", "a\0b"),
                                           ("FEATURE_EXAMPLE", 1)])
def test_invalid_settings_are_rejected_without_echoing_values(example, setting, value):
    configure(example, lambda c: c["hunts"].update(settings={setting: value}))
    done = start(example)
    assert done.returncode != 0
    assert "must" in done.stderr
    if setting == "FEATURE_EXAMPLE" and isinstance(value, str):
        assert value not in done.stderr
    assert not example.hunt_report("example").exists()


def test_unknown_area_settings_are_rejected(example):
    configure(example, lambda c: c["hunts"]["area_settings"].update(unknown={"FEATURE_EXAMPLE": "on"}))
    done = start(example)
    assert done.returncode != 0
    assert "absent from hunts.areas" in done.stderr


def test_launch_refusal_can_be_retried_with_the_same_hunt_name(example):
    refusal = example.state / "refuse-launch-hunt-example"
    refusal.touch()
    assert start(example, area="first").returncode != 0
    assert not example.hunt_report("example").exists()
    refusal.unlink()
    retried = start(example, area="first")
    assert retried.returncode == 0, retried.stderr
    assert json.loads(snapshot(example).read_text())["FEATURE_EXAMPLE"] == "on"


def test_missing_session_link_can_be_retried(example):
    example.screen("Example hunter has no link yet\n")
    first = start(example, area="first")
    assert first.returncode != 0
    assert "no session link" in first.stderr
    assert not example.hunt_report("example").exists()
    # A new launch refusal proves recovery reaches launch instead of a stale report.
    (example.state / "refuse-launch-hunt-example").touch()
    second = start(example, area="first")
    assert "has a report already" not in second.stderr
    assert not example.hunt_report("example").exists()


def test_two_calls_cannot_mix_area_settings_and_refused_call_is_retryable(example):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(start, example, "example-" + area, area)
                   for area in ("first", "second")]
        results = [job.result(timeout=30) for job in pending]
    for area, result in zip(("first", "second"), results):
        if result.returncode:
            assert "under way" in result.stderr, result.stderr
            result = start(example, "example-" + area, area)
        assert result.returncode == 0, result.stderr
        done = example.shipline("hunt-app", "fresh", cwd=example.hunt_worktree("example-" + area))
        assert done.returncode == 0
        assert json.loads(done.stdout)["FEATURE_EXAMPLE"] == ("on" if area == "first" else "second")


def test_long_held_start_lock_refuses_without_leaving_settings_or_report(example):
    lock = example.repo / ".local/workflow/start-hunt.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = start(example, area="first")
        assert done.returncode != 0
        assert "under way" in done.stderr
        assert not snapshot(example).exists()
        assert not example.hunt_report("example").exists()
    assert start(example, area="first").returncode == 0


def test_partial_snapshot_does_not_silently_skip_stopping_the_running_app(example):
    assert start(example, area="first").returncode == 0
    assert example.shipline("hunt-app", "start", cwd=example.hunt_worktree("example")).returncode == 0
    snapshot(example).write_text('{"FEATURE_EXAMPLE":')
    stopped = example.run("stop", "hunt-example")
    stop_record = example.hunt_worktree("example") / "example-stop.json"
    safe_shutdown = stopped.returncode != 0 or stop_record.exists()
    assert safe_shutdown, (
        "stop returned success without calling hunt_app stop after snapshot corruption")
    if stopped.returncode:
        assert example.hunt_worktree("example").exists()


def test_report_write_failure_does_not_permanently_block_the_hunt_name(example):
    hooks = example.root / "example-hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "import errno, os\n"
        "original_open = os.open\n"
        "def fail_report(path, flags, mode=0o777, **kwargs):\n"
        "    if str(path).endswith('/report.md'):\n"
        "        raise OSError(errno.ENOSPC, 'example storage full')\n"
        "    return original_open(path, flags, mode, **kwargs)\n"
        "os.open = fail_report\n")
    failed = example.run("start", "hunt", "example", "--area", "first",
                         env={"PYTHONPATH": str(hooks)})
    assert failed.returncode != 0
    assert "could not use hunt settings" in failed.stderr
    retried = start(example, area="first")
    recovered = retried.returncode == 0
    assert recovered, "retry after storage recovery refused: a hunt named example has a report already"


def test_report_header_preserves_findings_count_for_daily_metrics(example):
    assert start(example, area="first").returncode == 0
    report = example.hunt_report("example") / "report.md"
    with report.open("a") as stream:
        stream.write("Findings: 3\n\n## Example finding\n\nExample details.\n")
    # Exercise the existing consumer without making its unrelated GitHub reads.
    done = subprocess.run(
        ["python3", "-c",
         "import json, sys; from pathlib import Path; "
         "from workflow_metrics import read_hunts; "
         "print(json.dumps(read_hunts(Path(sys.argv[1]))))", str(example.repo)],
        cwd=example.repo,
        env={**example.env, "PYTHONPATH": str(example.script.parent)},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert done.returncode == 0, done.stderr
    count = json.loads(done.stdout)[0][2]
    assert count == 3, f"daily hunt findings count: expected 3, got {count}"


def test_legacy_hunt_without_snapshot_can_still_stop_its_app(example):
    assert start(example, area="first").returncode == 0
    snapshot(example).unlink()
    stopped = example.run("stop", "hunt-example")
    assert stopped.returncode == 0, stopped.stderr
    assert (example.hunt_worktree("example") / "example-stop.json").exists()


def test_hunt_shutdown_uses_saved_settings_after_config_edit(example):
    assert start(example, area="first").returncode == 0
    configure(example, lambda c: c["hunts"].update(settings={"FEATURE_EXAMPLE": "changed"},
                                                   area_settings={}))
    stopped = example.run("stop", "hunt-example")
    assert stopped.returncode == 0, stopped.stderr
    record = example.hunt_worktree("example") / "example-stop.json"
    assert json.loads(record.read_text())["FEATURE_EXAMPLE"] == "on"
