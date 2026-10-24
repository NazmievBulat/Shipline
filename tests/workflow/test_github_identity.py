"""The runner programs' own GitHub identity, apart from the owner's gh login.

Run against a stand-in `gh` that records the token each call carries, and a stand-in GitHub
for the two requests a GitHub App makes with its key.
"""

import base64
import contextlib
import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from github_app_stand_in import APP_TOKEN, REPOSITORY, App, served, token_of

_ROOT = Path(__file__).resolve().parents[2]
_BUDGET = _ROOT / "scripts/workflow/github_budget.py"
_IDENTITY = _ROOT / "scripts/workflow/github_identity.py"
_TOKEN = "github_pat_SECOND-ACCOUNT-SECRET"
# Records the token, prints it where gh's own debug output would carry it, and answers.
_FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['HOME'])
token = os.environ.get('GH_TOKEN')
with (root / 'calls').open('a') as output:
    output.write(json.dumps({'arguments': sys.argv[1:], 'token': token,
                             'github_token': os.environ.get('GITHUB_TOKEN')}) + '\\n')
answers = json.loads((root / 'answers').read_text()) if (root / 'answers').exists() else {}
answer = next((row for key, row in answers.items() if key in ' '.join(sys.argv[1:])), {})
print('* Request at now', file=sys.stderr)
print('> Authorization: token ' + str(token), file=sys.stderr)
print('< HTTP/2.0 ' + str(answer.get('status', 200)) + ' Reply', file=sys.stderr)
print('< X-Ratelimit-Remaining: ' + str(answer.get('remaining', 4900)), file=sys.stderr)
print('< X-Ratelimit-Reset: 2000000000', file=sys.stderr)
print('< X-Ratelimit-Resource: core', file=sys.stderr)
print('', file=sys.stderr)
print('{}', file=sys.stderr)
print('* Request took 1ms', file=sys.stderr)
print(answer.get('diagnostic', ''), file=sys.stderr, end='')
print(answer.get('body', '{}'))
sys.exit(answer.get('exit', 0))
"""


@pytest.fixture
def host(programs_folder: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(programs_folder))
    monkeypatch.delenv("WORKFLOW_GH", raising=False)
    monkeypatch.delenv("WORKFLOW_GITHUB_RAW_GH", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "a-token-of-the-environment")
    bin_folder = programs_folder / "bin"
    bin_folder.mkdir()
    gh = bin_folder / "gh"
    gh.write_text(_FAKE)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_folder}:{os.environ['PATH']}")
    return programs_folder


def _identity(host: Path) -> Path:
    return host / ".config/shipline-runner"


def _calls(host: Path) -> list[dict[str, Any]]:
    path = host / "calls"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _budget(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_BUDGET), *arguments],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _install(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_IDENTITY), "install", "--repository", REPOSITORY, *arguments],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _token_file(host: Path, text: str = _TOKEN + "\n") -> Path:
    given = host / "given-token"
    given.write_text(text)
    return given


def _everything_written(host: Path) -> str:
    """Every file the helpers keep, but the identity's own files, which hold the secrets."""
    text = ""
    for path in (host / ".local").rglob("*"):
        if path.is_file():
            text += path.read_text(errors="replace")
    return text


def test_with_the_token_file_every_runner_call_carries_that_token(host: Path) -> None:
    assert _install("--token", str(_token_file(host))).returncode == 0
    (host / "calls").unlink()

    done = _budget("call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runners")

    assert done.returncode == 0, done.stderr
    assert [call["token"] for call in _calls(host)] == [_TOKEN]
    # gh reads GH_TOKEN first; GITHUB_TOKEN is dropped too, so no other token can win.
    assert _calls(host)[0]["github_token"] is None
    assert _TOKEN not in done.stdout + done.stderr
    assert _TOKEN not in _everything_written(host)


def test_without_the_file_calls_use_the_login_and_say_so_once(host: Path) -> None:
    first = _budget(
        "call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runners"
    )
    second = _budget("call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runs")
    other = _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")

    assert first.returncode == second.returncode == other.returncode == 0
    # No GH_TOKEN: gh uses its own login, as before this identity existed.
    assert [call["token"] for call in _calls(host)] == [None, None, None]
    assert "using the gh login" in first.stderr
    assert "using the gh login" not in second.stderr
    # Each program says it once, in its own log.
    assert "lender: " in other.stderr and "using the gh login" in other.stderr


def test_a_call_without_an_identity_never_carries_the_runners_token(host: Path) -> None:
    assert _install("--token", str(_token_file(host))).returncode == 0
    (host / "calls").unlink()

    assert _budget("call", "tick", "api", "repos/o/r/pulls").returncode == 0

    assert [call["token"] for call in _calls(host)] == [None]


def test_the_runner_identitys_budget_is_kept_apart_from_the_logins(host: Path) -> None:
    assert _install("--token", str(_token_file(host))).returncode == 0
    (host / "answers").write_text(json.dumps({"runners": {"remaining": 10}}))

    low = _budget("call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runners")
    assert low.returncode == 0
    # The runner identity is near its limit, the login is not: only runner programs pause.
    assert _budget("ready", "--identity", "runner", "keep-spare").returncode == 75
    assert _budget("ready", "tick").returncode == 0
    assert _budget("call", "tick", "api", "repos/o/r/pulls").returncode == 0


def test_the_logins_low_budget_does_not_pause_the_runner_identity(host: Path) -> None:
    (host / "answers").write_text(json.dumps({"pulls": {"remaining": 10}}))
    assert _budget("call", "tick", "api", "repos/o/r/pulls").returncode == 0
    assert _budget("ready", "tick").returncode == 75
    # Without an identity of their own the runner programs share the login's pause.
    assert _budget("ready", "--identity", "runner", "keep-spare").returncode == 75

    assert _install("--token", str(_token_file(host))).returncode == 0

    assert _budget("ready", "--identity", "runner", "keep-spare").returncode == 0


def test_the_runner_identity_has_call_slots_of_its_own(host: Path) -> None:
    assert _install("--token", str(_token_file(host))).returncode == 0
    budget = host / ".local/state/workflow/github-budget"
    budget.mkdir(parents=True, exist_ok=True)
    held = [(budget / f"call-{number}.lock").open("a") for number in range(2)]
    try:
        for lock in held:
            fcntl.flock(lock, fcntl.LOCK_EX)
        # The login's two slots are taken; the runner identity's are free.
        done = _budget(
            "call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runners"
        )
        assert done.returncode == 0, done.stderr
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(
                [sys.executable, str(_BUDGET), "call", "tick", "api", "repos/o/r/pulls"],
                capture_output=True,
                timeout=3,
                check=False,
            )
    finally:
        for lock in held:
            lock.close()


def test_a_token_file_spoiled_after_install_fails_the_call_and_says_so_once(host: Path) -> None:
    assert _install("--token", str(_token_file(host))).returncode == 0
    (host / "calls").unlink()
    (_identity(host) / "token").write_text("two words\n")

    first = _budget("call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runs")
    second = _budget("call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runs")

    assert first.returncode == second.returncode == 1
    assert _calls(host) == []
    assert "one line" in first.stderr
    assert second.stderr == ""


def test_a_fault_is_said_again_once_fixed_and_back_and_a_new_fault_is_said_too(
    host: Path,
) -> None:
    assert _install("--token", str(_token_file(host))).returncode == 0
    token = _identity(host) / "token"

    def runner_call() -> subprocess.CompletedProcess[str]:
        return _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")

    token.write_text("two words\n")
    assert "one line" in runner_call().stderr
    token.write_text(_TOKEN + "\n")
    assert runner_call().returncode == 0
    token.write_text("two words\n")
    assert "one line" in runner_call().stderr
    token.chmod(0)
    assert "cannot read" in runner_call().stderr


def test_install_writes_the_token_for_this_user_alone_and_never_prints_it(host: Path) -> None:
    done = _install("--token", str(_token_file(host)))

    assert done.returncode == 0, done.stderr
    assert _TOKEN not in done.stdout + done.stderr
    folder = _identity(host)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "token").stat().st_mode) == 0o600
    assert (folder / "token").read_text().strip() == _TOKEN
    # It checked every right the programs use before saving, with the new token only.
    asked = [" ".join(call["arguments"]) for call in _calls(host)]
    assert asked == [
        f"api repos/{REPOSITORY}/actions/runners?per_page=1",
        f"api repos/{REPOSITORY}/actions/runs?per_page=1",
        f"api repos/{REPOSITORY}/actions/variables?per_page=1",
        f"api repos/{REPOSITORY}/pulls?per_page=1",
        f"api -X POST repos/{REPOSITORY}/actions/runners/registration-token --silent",
    ]
    assert {call["token"] for call in _calls(host)} == {_TOKEN}


def test_install_refuses_a_token_without_a_right_and_keeps_the_identity_it_had(
    host: Path,
) -> None:
    assert _install("--token", str(_token_file(host))).returncode == 0
    (host / "answers").write_text(
        json.dumps(
            {
                "variables": {
                    "status": 403,
                    "exit": 1,
                    "diagnostic": "gh: Resource not accessible by personal access token "
                    "(HTTP 403) github_pat_NEW-ONE\n",
                }
            }
        )
    )

    done = _install("--token", str(_token_file(host, "github_pat_NEW-ONE\n")))

    assert done.returncode == 1
    assert "variables" in done.stderr
    assert "HTTP 403" in done.stderr
    assert "github_pat_NEW-ONE" not in done.stdout + done.stderr
    assert (_identity(host) / "token").read_text().strip() == _TOKEN


@pytest.mark.parametrize("text", ["", "\n", "two words\n", "one\ntwo\n"])
def test_install_refuses_a_file_that_is_not_one_token(host: Path, text: str) -> None:
    done = _install("--token", str(_token_file(host, text)))

    assert done.returncode == 1
    assert "one line" in done.stderr
    assert not _identity(host).exists()
    assert _calls(host) == []


def test_a_new_token_replaces_an_app(host: Path, app: App) -> None:
    assert _install("--app", "123", str(app.key)).returncode == 0
    assert _install("--token", str(_token_file(host))).returncode == 0

    assert sorted(path.name for path in _identity(host).iterdir() if path.name != "lock") == [
        "token"
    ]


# --- A GitHub App ---


@pytest.fixture
def app(host: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    with served(host) as state:
        monkeypatch.setenv("WORKFLOW_GITHUB_API_URL", state.url)
        yield state


def _unpadded(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _check_signed(app: App, authorization: str) -> dict[str, Any]:
    """The JSON web token GitHub would read: signed with the key of the GitHub App, naming it."""
    assert authorization.startswith("Bearer ")
    header, payload, signature = authorization.removeprefix("Bearer ").split(".")
    assert json.loads(_unpadded(header)) == {"alg": "RS256", "typ": "JWT"}
    signed = app.key.parent / "signature"
    signed.write_bytes(_unpadded(signature))
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(app.public), "-signature", str(signed)],
        input=f"{header}.{payload}".encode(),
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    claims = json.loads(_unpadded(payload))
    # GitHub refuses a token that lives longer than ten minutes, and allows for clock drift.
    assert claims["iat"] <= time.time() - 30
    assert claims["exp"] - claims["iat"] <= 600
    return claims


def test_install_with_an_app_finds_its_installation_and_keeps_its_key_private(
    host: Path, app: App
) -> None:
    done = _install("--app", "123", str(app.key))

    assert done.returncode == 0, done.stderr
    assert APP_TOKEN not in done.stdout + done.stderr
    assert [(row["method"], row["path"]) for row in app.requests] == [
        ("GET", f"/repos/{REPOSITORY}/installation"),
        ("POST", "/app/installations/42/access_tokens"),
    ]
    for row in app.requests:
        assert _check_signed(app, row["authorization"])["iss"] == 123
    folder = _identity(host)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for name in ("app-id", "app-key.pem", "installation-id", "installation-token.json"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600, name
    assert (folder / "installation-id").read_text().strip() == "42"
    assert {call["token"] for call in _calls(host)} == {APP_TOKEN}


def test_runner_calls_carry_the_apps_installation_token_and_make_it_once_an_hour(
    host: Path, app: App
) -> None:
    assert _install("--app", "123", str(app.key)).returncode == 0
    app.requests.clear()
    (host / "calls").unlink()

    for _ in range(2):
        done = _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")
        assert done.returncode == 0, done.stderr

    assert [call["token"] for call in _calls(host)] == [APP_TOKEN, APP_TOKEN]
    # The token install made is still good for most of an hour.
    assert app.requests == []
    assert APP_TOKEN not in _everything_written(host)


def test_an_installation_token_near_its_end_is_made_again(host: Path, app: App) -> None:
    app.expires_in = 5 * 60
    assert _install("--app", "123", str(app.key)).returncode == 0
    app.requests.clear()

    done = _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")

    assert done.returncode == 0, done.stderr
    assert [(row["method"], row["path"]) for row in app.requests] == [
        ("POST", "/app/installations/42/access_tokens")
    ]


def _until(condition: Any, what: str) -> None:
    deadline = time.time() + 20
    while not condition():
        assert time.time() < deadline, what
        time.sleep(0.05)


def test_an_app_installed_while_the_old_one_renews_keeps_its_own_token(
    host: Path, app: App
) -> None:
    app.expires_in = 5 * 60
    assert _install("--app", "123", str(app.key)).returncode == 0
    app.expires_in = 3600
    app.requests.clear()
    app.held.add(123)
    command = [sys.executable, str(_BUDGET), "call", "--identity", "runner", "lender"]
    renewal = subprocess.Popen([*command, "api", "repos/o/r/actions/runs"], text=True)
    installing = [sys.executable, str(_IDENTITY), "install", "--repository", REPOSITORY]
    replacing = subprocess.Popen([*installing, "--app", "456", str(app.key)], text=True)
    try:
        # The renewal of the old GitHub App waits on GitHub while the new one is checked and put
        # in place.
        _until(lambda: app.requests, "the renewal never asked GitHub")
        _until(
            lambda: sum(call["token"] == token_of(456) for call in _calls(host)) == 5,
            "the new app was never checked",
        )
        with contextlib.suppress(subprocess.TimeoutExpired):
            replacing.wait(timeout=2)
    finally:
        app.release.set()
        assert renewal.wait(timeout=20) == 0
        assert replacing.wait(timeout=20) == 0

    done = _budget(*command[2:], "api", "repos/o/r/actions/runs")

    assert done.returncode == 0, done.stderr
    assert _calls(host)[-1]["token"] == token_of(456)


def test_an_identity_that_cannot_make_a_token_fails_the_call_without_using_the_login(
    host: Path, app: App
) -> None:
    app.expires_in = 5 * 60
    assert _install("--app", "123", str(app.key)).returncode == 0
    (host / "calls").unlink()
    app.refuse = 401

    first = _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")
    second = _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")

    assert first.returncode == second.returncode == 1
    # Falling back to the login is only for a machine with no identity at all.
    assert _calls(host) == []
    assert "401" in first.stderr
    assert "could not be decoded" in first.stderr
    # The same fault is said once, not every five seconds.
    assert second.stderr == ""


@pytest.mark.parametrize(
    ("status", "headers", "message", "least"),
    [
        (429, {"Retry-After": "300"}, "", 290),
        (403, {}, "You have exceeded a secondary rate limit", 50),
    ],
)
def test_a_rate_limited_renewal_asks_github_nothing_until_the_cooldown_ends(
    host: Path, app: App, status: int, headers: dict[str, str], message: str, least: int
) -> None:
    app.expires_in = 5 * 60
    assert _install("--app", "123", str(app.key)).returncode == 0
    app.requests.clear()
    app.refuse = status
    app.refusal_headers = headers
    app.refusal_message = message or app.refusal_message
    pause = _identity(host) / "renewal-pause.json"

    first = _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")
    second = _budget("call", "--identity", "runner", "keep-spare", "api", "repos/o/r/actions/runs")

    assert first.returncode == second.returncode == 1
    assert str(status) in first.stderr
    assert "rate limit holds" in second.stderr
    assert len(app.requests) == 1
    assert json.loads(pause.read_text())["until"] - time.time() > least
    assert stat.S_IMODE(pause.stat().st_mode) == 0o600

    # Once the cooldown is over, the next call asks again.
    app.refuse = None
    pause.write_text(json.dumps({"until": time.time() - 1}))
    done = _budget("call", "--identity", "runner", "lender", "api", "repos/o/r/actions/runs")
    assert done.returncode == 0, done.stderr
    assert len(app.requests) == 2


def test_a_refusal_that_is_not_the_rate_limit_holds_nothing(host: Path, app: App) -> None:
    app.expires_in = 5 * 60
    assert _install("--app", "123", str(app.key)).returncode == 0
    app.requests.clear()
    app.refuse = 403

    for _ in range(2):
        assert _budget("call", "--identity", "runner", "lender", "api", "x").returncode == 1

    assert len(app.requests) == 2
    assert not (_identity(host) / "renewal-pause.json").exists()


def test_install_refuses_an_app_github_does_not_accept(host: Path, app: App) -> None:
    app.refuse = 401

    done = _install("--app", "123", str(app.key))

    assert done.returncode == 1
    assert "401" in done.stderr
    assert not _identity(host).exists()


def test_install_refuses_a_key_openssl_cannot_sign_with(host: Path, app: App) -> None:
    bad = host / "not-a-key.pem"
    bad.write_text("not a key\n")

    done = _install("--app", "123", str(bad))

    assert done.returncode == 1
    assert "key" in done.stderr
    assert app.requests == []
    assert not _identity(host).exists()
