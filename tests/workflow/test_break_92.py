"""Independent PR 92 probes. Only the usage service is replaced; callers are real.

Run this file alone with pytest. No GitHub requests or model calls are made.
"""

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"
START = 1_800_000_000
FIVE_RESET = START + 7200
SEVEN_RESET = START + 4 * 86400
SYNTHETIC_TOKEN = "example-test-credential"


def answer(five=42, seven=31):
    return {
        "five_hour": {
            "utilization": five,
            "resets_at": datetime.fromtimestamp(FIVE_RESET, timezone.utc).isoformat(),
        },
        "seven_day": {
            "utilization": seven,
            "resets_at": datetime.fromtimestamp(SEVEN_RESET, timezone.utc).isoformat(),
        },
    }


LINES = f"five_hour 42 {FIVE_RESET}\nseven_day 31 {SEVEN_RESET}\n"


class Installation:
    def __init__(self, root):
        self.root = root
        self.repo = root / "example-app"
        self.control = root / "endpoint"
        self.bin = root / "bin"
        for directory in (self.repo, self.control, self.bin, root / "home"):
            directory.mkdir()
        (root / "empty-config").touch()
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "GIT_CONFIG_GLOBAL": str(root / "empty-config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "SHIPLINE_REPO": str(self.repo),
            "WORKFLOW_USAGE_CACHE": str(root / "status-cache.json"),
            "WORKFLOW_CREDENTIALS": str(root / "credentials.json"),
            "WORKFLOW_NOW": str(START),
            "EXAMPLE_ENDPOINT": str(self.control),
        }
        self.git("init", "--quiet", "--initial-branch=main")
        self.config = self.repo / "shipline.json"
        self.config.write_text(json.dumps({"repository": "example-org/example-app"}))
        self.credentials = root / "credentials.json"
        self.credentials.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": SYNTHETIC_TOKEN},
        }))
        self.cache = root / "status-cache.json"
        self.cache.write_text(json.dumps(answer(12)))
        os.utime(self.cache, (START - 86400, START - 86400))
        self.respond()
        self.write("headers", "HTTP/2 200\r\n\r\n")
        self.write("exit", "0")
        curl = self.bin / "curl"
        curl.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys, time
p = pathlib.Path(os.environ['EXAMPLE_ENDPOINT'])
args = sys.argv[1:]
with (p / 'calls').open('a') as out:
    out.write('request\\n')
(p / 'args').write_text(json.dumps(args))
# The credential is consumed without being recorded.
sys.stdin.read()
assert '--max-time' in args
assert 0 < float(args[args.index('--max-time') + 1]) <= 4
header = pathlib.Path(args[args.index('--dump-header') + 1])
header.write_bytes((p / 'headers').read_bytes())
(p / 'in-flight').touch()
deadline = time.monotonic() + 8
while (p / 'hold').exists():
    if time.monotonic() >= deadline:
        sys.exit(28)
    time.sleep(0.01)
sys.stdout.write((p / 'body').read_text())
sys.exit(int((p / 'exit').read_text()))
""")
        curl.chmod(0o700)

    def git(self, *args, cwd=None):
        done = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, timeout=10)
        assert done.returncode == 0, "example repository setup failed"
        return done.stdout.strip()

    @property
    def state(self):
        return self.repo / json.loads(self.config.read_text()).get(
            "state_folder", ".local/workflow"
        )

    def write(self, name, value):
        (self.control / name).write_text(value)

    def respond(self, value=None):
        self.write("body", json.dumps(answer() if value is None else value))

    def failure(self, header="", code=22, status=429):
        self.write("exit", str(code))
        self.write("headers", f"HTTP/2 {status}\r\n{header}\r\n")
        self.respond({"error": "example service unavailable"})

    @property
    def requests(self):
        path = self.control / "calls"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def run(self, now=START, *, kind="usage", env=None, cwd=None):
        environ = {**self.env, "WORKFLOW_NOW": str(now), **(env or {})}
        if kind == "usage":
            command = ["bash", str(SCRIPTS / "usage")]
        else:
            code = (
                "import sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); "
            )
            if kind == "ticket":
                code += (
                    "import ticket_starts; "
                    "reason = ticket_starts.usage_wait(int(sys.argv[2])); "
                    "print(reason or 'allowed'); sys.exit(reason is not None)"
                )
            else:
                code += (
                    "import cloud_workers; "
                    "allowed = cloud_workers._allowed(Path(sys.argv[1])); "
                    "print('allowed' if allowed else 'held'); sys.exit(not allowed)"
                )
            command = [sys.executable, "-B", "-c", code, str(SCRIPTS), str(now)]
        return subprocess.run(command, cwd=cwd or self.repo, env=environ,
                              text=True, capture_output=True, timeout=12)

    def start(self, now):
        return subprocess.Popen(["bash", str(SCRIPTS / "usage")], cwd=self.repo,
                                env={**self.env, "WORKFLOW_NOW": str(now)},
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)

    def wait_in_flight(self):
        deadline = time.monotonic() + 5
        while not (self.control / "in-flight").exists():
            assert time.monotonic() < deadline, "request did not reach the stand-in"
            time.sleep(0.01)


@pytest.fixture
def app(tmp_path):
    return Installation(tmp_path)


def test_daily_callers_share_figures_and_release_ticket_starts(app):
    for kind in ("usage", "ticket", "cloud", "usage", "usage") * 2:
        result = app.run(kind=kind)
        assert result.returncode == 0, result.stderr
        assert result.stdout == (LINES if kind == "usage" else "allowed\n")
    assert app.requests == 1


@pytest.mark.parametrize("header,pause", [
    ("Retry-After: 92\r\n", 300),
    ("rEtRy-AfTeR: 000600\r\n", 600),
    ("Retry-After: " + format_datetime(datetime.fromtimestamp(
        START + 900, timezone.utc), usegmt=True) + "\r\n", 600),
    ("Retry-After: nonsense\r\n", 300),
    ("Retry-After: 9999999999999999999999999999999999\r\n", 300),
    ("", 300),
])
def test_shared_failure_backoff_and_recovery(app, header, pause):
    assert app.run().stdout == LINES
    app.failure(header)
    failed_at = START + 300
    assert app.run(failed_at).stdout == LINES
    for elapsed in (1, 91, 92, pause - 1):
        for kind in ("usage", "ticket", "cloud"):
            result = app.run(failed_at + elapsed, kind=kind)
            assert result.returncode == 0, result.stderr
        assert app.requests == 2
    app.write("exit", "0")
    app.respond(answer(45))
    assert app.run(failed_at + pause).stdout.startswith("five_hour 45 ")
    assert app.requests == 3


@pytest.mark.parametrize("code,status", [(22, 500), (22, 503), (28, 0), (0, 200)])
def test_transport_and_invalid_payload_failures_keep_the_previous_answer(app, code, status):
    assert app.run().stdout == LINES
    app.failure("Retry-After: 600\r\n", code=code, status=status)
    assert app.run(START + 300).stdout == LINES
    assert app.run(START + 899, kind="ticket").returncode == 0
    assert app.requests == 2


@pytest.mark.parametrize("seed", [False, True])
def test_held_lock_returns_immediately_without_a_request(app, seed):
    if seed:
        assert app.run().stdout == LINES
    app.state.mkdir(parents=True, exist_ok=True)
    with (app.state / "usage-request.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        result = app.run(START + 301)
        assert time.monotonic() - started < 2
    assert result.returncode == (0 if seed else 1)
    assert result.stdout == (LINES if seed else "")
    assert app.requests == int(seed)


@pytest.mark.parametrize("seed", [False, True])
def test_ten_simultaneous_callers_make_one_new_request(app, seed):
    if seed:
        assert app.run().stdout == LINES
        (app.control / "in-flight").unlink()
    (app.control / "hold").touch()
    leader = app.start(START + 300)
    try:
        app.wait_in_flight()
        with ThreadPoolExecutor(max_workers=9) as pool:
            results = list(pool.map(lambda _: app.run(START + 300), range(9)))
        for result in results:
            assert result.returncode == (0 if seed else 1), result.stderr
            assert result.stdout == (LINES if seed else "")
        assert app.requests == int(seed) + 1
    finally:
        (app.control / "hold").unlink(missing_ok=True)
        stdout, stderr = leader.communicate(timeout=10)
    assert leader.returncode == 0, stderr
    assert stdout == LINES


def test_killed_request_retains_admission_and_old_atomic_answer(app):
    assert app.run().stdout == LINES
    before = (app.state / "usage-answer.json").read_bytes()
    (app.control / "in-flight").unlink()
    (app.control / "hold").touch()
    app.respond(answer(99))
    process = app.start(START + 300)
    try:
        app.wait_in_flight()
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        (app.control / "hold").unlink(missing_ok=True)
    assert (app.state / "usage-answer.json").read_bytes() == before
    assert app.run(START + 599).stdout == LINES
    assert app.requests == 2
    assert app.run(START + 600).stdout.startswith("five_hour 99 ")
    assert app.requests == 3


@pytest.mark.parametrize("age,usable", [(1799, True), (1800, True), (1801, False)])
def test_outage_expiry_propagates_to_start_checks(app, age, usable):
    assert app.run().stdout == LINES
    app.failure("Retry-After: 3600\r\n")
    assert app.run(START + 300).returncode == 0
    result = app.run(START + age)
    assert result.returncode == (0 if usable else 1)
    assert result.stdout == (LINES if usable else "")
    result = app.run(START + age, kind="ticket")
    assert result.returncode == (0 if usable else 1)
    if not usable:
        assert "could not be read" in result.stdout
    assert app.requests == 2


@pytest.mark.parametrize("missing", ["credentials", "status", "config", "answer"])
def test_missing_optional_files_do_not_break_a_usable_kept_answer(app, missing):
    assert app.run().stdout == LINES
    path = {"credentials": app.credentials, "status": app.cache,
            "config": app.config, "answer": app.state / "usage-answer.json"}[missing]
    path.unlink()
    result = app.run(START + 100)
    assert result.returncode == (1 if missing == "answer" else 0)
    assert result.stdout == ("" if missing == "answer" else LINES)
    assert app.requests == 1


def test_missing_credentials_do_not_hide_the_kept_answer_after_request_pause(app):
    assert app.run().stdout == LINES
    app.credentials.unlink()
    assert app.run(START + 400).stdout == LINES
    assert app.requests == 1
    assert app.run(START + 1801).stdout == ""


def test_fresh_status_cache_wins_without_mutating_shared_files(app):
    assert app.run().stdout == LINES
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in app.state.iterdir()}
    app.cache.write_text(json.dumps(answer(8)))
    os.utime(app.cache, (START + 301, START + 301))
    assert app.run(START + 301).stdout.startswith("five_hour 8 ")
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in app.state.iterdir()}
    assert after == before
    assert app.requests == 1


def test_kept_null_windows_remain_not_started_for_both_start_callers(app):
    app.respond({"five_hour": None, "seven_day": None})
    assert app.run().returncode == 0
    for now in (START + 100, START + 299):
        result = app.run(now)
        assert result.stdout == (
            f"five_hour 0 {now + 18000} not_started\n"
            f"seven_day 0 {now + 604800} not_started\n"
        )
        assert app.run(now, kind="ticket").returncode == 0
        assert app.run(now, kind="cloud").returncode == 0
    assert app.requests == 1


@pytest.mark.parametrize("seed", [False, True])
def test_shadow_reads_neither_request_nor_modify_files(app, seed):
    if seed:
        assert app.run().returncode == 0
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in app.state.glob("*")}
    existed = app.state.exists()
    result = app.run(START + 301, env={"SHIPLINE_SHADOW": "example-shadow"})
    assert result.returncode == (0 if seed else 1)
    assert result.stdout == (LINES if seed else "")
    assert app.state.exists() == existed
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in app.state.glob("*")} == before
    assert app.requests == int(seed)


def test_state_is_private_and_never_retains_credential_or_extra_response_fields(app):
    app.respond({**answer(), "extra": SYNTHETIC_TOKEN})
    previous = os.umask(0)
    try:
        result = app.run()
    finally:
        os.umask(previous)
    assert result.returncode == 0, result.stderr
    assert SYNTHETIC_TOKEN not in result.stdout + result.stderr
    assert SYNTHETIC_TOKEN not in (app.control / "args").read_text()
    for path in app.state.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert SYNTHETIC_TOKEN not in path.read_text()
    assert not list(app.state.glob("usage-headers.*"))
    assert set(json.loads((app.state / "usage-answer.json").read_text())) == {
        "received_at", "lines",
    }


def test_shell_metacharacters_in_configured_state_path_are_literal(app):
    config = json.loads(app.config.read_text())
    config["state_folder"] = ".local/example state;$(touch example-marker)"
    app.config.write_text(json.dumps(config))
    result = app.run()
    assert result.returncode == 0, result.stderr
    assert result.stdout == LINES
    assert (app.state / "usage-answer.json").exists()
    assert not (app.repo / "example-marker").exists()


def test_last_http_header_block_controls_backoff(app):
    assert app.run().stdout == LINES
    app.failure()
    app.write("headers", "HTTP/1.1 200 Connection established\r\n"
              "Retry-After: 3600\r\n\r\nHTTP/2 429\r\nRetry-After: 600\r\n\r\n")
    assert app.run(START + 300).stdout == LINES
    assert int((app.state / "usage-next-request").read_text()) == START + 900


def test_failure_with_no_previous_answer_is_silent_and_suppresses_retries(app):
    app.failure("Retry-After: 600\r\n")
    for now in (START, START + 1, START + 92, START + 300, START + 599):
        result = app.run(now)
        assert result.returncode == 1
        assert result.stdout == ""
    assert app.requests == 1


def test_corrupt_kept_json_fails_closed_during_backoff(app):
    assert app.run().returncode == 0
    (app.state / "usage-answer.json").write_text('{"received_at":')
    result = app.run(START + 100)
    assert result.returncode == 1
    assert result.stdout == ""
    assert app.run(START + 100, kind="ticket").returncode == 1
    assert app.requests == 1


@pytest.mark.parametrize("percent,remaining,allowed", [
    (85, 7200, True), (86, 7200, False), (100, 3600, True),
])
def test_kept_figures_preserve_the_start_ceiling_and_reset_rule(
    app, percent, remaining, allowed
):
    value = answer(percent)
    value["five_hour"]["resets_at"] = datetime.fromtimestamp(
        START + 300 + remaining, timezone.utc
    ).isoformat()
    app.respond(value)
    assert app.run().returncode == 0
    app.failure("Retry-After: 600\r\n")
    assert app.run(START + 300).returncode == 0
    for kind in ("ticket", "cloud"):
        result = app.run(START + 300, kind=kind)
        assert result.returncode == (0 if allowed else 1), result.stderr
    assert app.requests == 2


def test_worktree_and_external_config_share_the_main_installation_state(app):
    # Use relative worktree metadata when supported, keeping even fixture metadata portable.
    help_text = subprocess.run(
        ["git", "worktree", "add", "-h"], cwd=app.repo, env=app.env,
        text=True, capture_output=True, timeout=5,
    )
    relative = ["--relative-paths"] if "--relative-paths" in help_text.stdout else []
    app.git("config", "user.name", "Bulat Nazmiev")
    app.git("config", "user.email", "82903523+NazmievBulat@users.noreply.github.com")
    app.git("add", "shipline.json")
    app.git("commit", "--quiet", "-m", "test: example installation")
    worktree = app.root / "example-worktree"
    app.git("worktree", "add", "--quiet", "--detach", *relative, str(worktree), "HEAD")
    external = app.root / "installation.json"
    external.write_text(json.dumps({
        "repository": "example-org/example-app", "state_folder": ".local/example state",
    }))
    app.env["SHIPLINE_CONFIG"] = str(external)
    assert app.run().stdout == LINES
    app.env["SHIPLINE_REPO"] = str(worktree)
    assert app.run(cwd=worktree).stdout == LINES
    assert app.run(kind="ticket", cwd=worktree).returncode == 0
    assert app.requests == 1
    assert (app.repo / ".local/example state/usage-answer.json").is_file()
    assert not (worktree / ".local/example state").exists()


def test_limit_reset_uses_kept_figures_under_its_short_hook_deadline(app):
    app.respond(answer(100))
    assert app.run().returncode == 0
    app.failure("Retry-After: 600\r\n")
    assert app.run(START + 300).returncode == 0
    clock = datetime.fromtimestamp(FIVE_RESET, timezone.utc).strftime("%I:%M%p").lower()
    code = (
        'source "$1"; usage_reset_for_clock "usage limit" "$2" || exit 1; '
        'printf "%s\\n" "$limit_at"'
    )
    result = subprocess.run(
        ["bash", "-c", code, "bash", str(SCRIPTS / "limit-reset"), clock],
        cwd=app.repo, env={**app.env, "WORKFLOW_NOW": str(START + 301)},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == ((FIVE_RESET + 119) // 60) * 60
    assert app.requests == 2
