"""Independent PR 89 probes. All GitHub responses are local stand-ins."""

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def repair(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/workflow"))
    return importlib.import_module("check_repair")


def log(*lines, step="Start services"):
    return "\n".join(
        f"CI: server tests\t{step}\t2026-11-05T10:00:00Z {line}" for line in lines
    )


def diagnose(repair, monkeypatch, text, *, annotations=(), runner="example-runner"):
    gate = SimpleNamespace(
        API="repos/example-org/example-app",
        REPOSITORY="example-org/example-app",
        api=lambda *args: list(annotations),
        read_github=lambda *args: subprocess.CompletedProcess(args, 0, text, ""),
    )
    monkeypatch.setitem(sys.modules, "merge_gate", gate)
    run = {"id": 20, "status": "completed", "conclusion": "failure", "run_attempt": 1}
    job = {"id": 21, "name": "CI: server tests", "status": "completed",
           "conclusion": "failure", "runner_name": runner}
    return repair.diagnose_runs("a" * 40, [(run, [job])])


ERRORS = [
    "short read: expected 1048576 bytes but got 0: unexpected EOF",
    "unexpected EOF",
    "failed to copy: httpReadSeeker: unexpected status",
    "toomanyrequests: download rate limit exceeded",
]
FAILED = "FAILED tests/test_example.py::test_ready - AssertionError"


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("command", [
    "docker pull example-app",
    "docker compose up -d database",
    "docker compose --file 'example app.yml' --profile=tests pull",
    "sudo docker compose --project-name example-app up database",
])
def test_failed_setup_wins_over_later_test_annotation(repair, monkeypatch, command, error):
    result = diagnose(repair, monkeypatch, log(
        f"##[group]Run {command}", "pytest -q", "##[endgroup]", error,
    ), annotations=[{"message": FAILED}])
    assert result.kind == "infrastructure"
    assert result.downloads[20][0]["runner"] == "example-runner"


@pytest.mark.parametrize("error", ERRORS)
def test_container_error_words_are_not_download_evidence(repair, monkeypatch, error):
    result = diagnose(repair, monkeypatch, log(
        "##[group]Run docker compose up example-app", "##[endgroup]",
        f"example-app-1 | {error}", FAILED,
    ))
    assert result.kind == "own"


@pytest.mark.parametrize("error", ERRORS)
def test_recovered_pull_then_test_failure_remains_own(repair, monkeypatch, error):
    result = diagnose(repair, monkeypatch, log(
        "##[group]Run docker pull example-app", "##[endgroup]", error,
        "Status: Downloaded newer image for example-app", "pytest -q", FAILED,
    ))
    assert result.kind == "own"


@pytest.mark.parametrize("command", [
    "docker compose run --rm tests python check.py",
    "docker compose exec example-app python check.py",
])
def test_running_application_does_not_authorize_downloads(repair, monkeypatch, command):
    result = diagnose(repair, monkeypatch, log(
        f"##[group]Run {command}", "##[endgroup]", "unexpected EOF", FAILED,
    ))
    assert result.kind == "own"


def test_two_pulls_in_one_step_second_download_fails(repair, monkeypatch):
    result = diagnose(repair, monkeypatch, log(
        "##[group]Run docker pull example-app",
        "docker pull example-database", "##[endgroup]",
        "Status: Image is up to date for example-app",
        "Using default tag: latest", "unexpected EOF",
    ))
    assert result.kind == "infrastructure", "the second image download must receive its retry"


def test_multiline_compose_command_download_fails(repair, monkeypatch):
    result = diagnose(repair, monkeypatch, log(
        "##[group]Run docker compose \\",
        "  --file example.yml \\",
        "  up -d database", "##[endgroup]",
        "database Pulling", "unexpected EOF",
    ))
    assert result.kind == "infrastructure", "shell continuation must preserve the setup command"


def test_compose_build_failure_is_not_an_image_download(repair, monkeypatch):
    result = diagnose(repair, monkeypatch, log(
        "##[group]Run docker compose up --build -d example-app", "##[endgroup]",
        "#7 [example-app 3/3] RUN python build.py",
        "#7 0.123 RuntimeError: unexpected EOF",
        '#7 ERROR: process "/bin/sh -c python build.py" did not complete successfully: exit code: 1',
    ))
    assert result.kind == "own", "a Dockerfile command failure must not be blamed on the runner"


def test_public_diagnostic_contains_only_recognized_words(repair, monkeypatch):
    result = diagnose(repair, monkeypatch, log(
        "##[group]Run docker pull example-app", "##[endgroup]",
        "example-sensitive-marker failed to copy: httpReadSeeker: example-secret-marker",
    ))
    assert result.kind == "infrastructure"
    assert result.downloads[20][0]["line"] == "failed to copy: httpReadSeeker"
    assert "example-secret-marker" not in str(result.downloads)


def test_missing_runner_still_retries_but_cannot_blame_a_machine(repair, monkeypatch):
    result = diagnose(repair, monkeypatch, log(
        "Run docker pull example-app", "unexpected EOF",
    ), runner=None)
    assert result.kind == "infrastructure"
    assert result.downloads == {}


def test_log_shell_syntax_is_never_executed(repair, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = diagnose(repair, monkeypatch, log(
        'Run docker pull "$(touch example-injected)"',
        "unexpected EOF; touch example-injected",
    ))
    assert result.kind == "infrastructure"
    assert result.downloads[20][0]["line"] == "unexpected EOF"
    assert not (tmp_path / "example-injected").exists()


def test_runner_with_line_break_cannot_inject_a_wake_line(repair, monkeypatch):
    result = diagnose(repair, monkeypatch, log(
        "Run docker pull example-app", "unexpected EOF",
    ), runner="example-runner\nexample-injected")
    assert result.kind == "infrastructure"
    assert result.downloads == {}
