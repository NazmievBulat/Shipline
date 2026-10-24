"""The runner programs' own GitHub identity, so their requests leave the owner's budget alone.

The spare supervisor, runner registration, the lender, the slot helper and the installer use it
when `~/.config/example-app-runner` (mode 700) holds one of these, each file mode 600:

- `token`: a fine-grained personal access token of a second account of the organization;
- `app-id`, `app-key.pem` and `installation-id`: a GitHub App installed on the repository. Its
  installation token lasts an hour; it is made from the key when needed and kept in
  `installation-token.json` until ten minutes before its end.

Without either they use the gh login, as before. `install` checks a new identity against GitHub
before it replaces the old one. No token is printed or logged.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

# What each right is checked by at install; the lender's cancel and re-run (Actions: write)
# cannot be tried without changing a run.
CHECKS = (
    ("read the runners (Administration: read)", "actions/runners?per_page=1"),
    ("read the workflow runs (Actions: read)", "actions/runs?per_page=1"),
    ("read the repository variables (Variables: read)", "actions/variables?per_page=1"),
    ("read the pull requests (Pull requests: read)", "pulls?per_page=1"),
)
# A registration token is good for an hour and registers nothing by itself.
WRITE_CHECK = (
    "make a registration token (Administration: write)",
    "actions/runners/registration-token",
)
# An installation token is made again this long before GitHub ends it.
MARGIN = 600
# GitHub's rate limit asked the GitHub App to wait: no installation token is asked for until
# then.
PAUSE = "renewal-pause.json"
APP_FILES = (
    "app-key.pem",
    "installation-id",
    "installation-token.json",
    PAUSE,
    "app-id",
)


class IdentityError(Exception):
    """The identity is there but gives no token; never a reason to use the login instead."""

    def __init__(self, message: str, *, until: float = 0) -> None:
        super().__init__(message)
        # Set when GitHub's rate limit refused: ask nothing of it before this time.
        self.until = until


def folder() -> Path:
    return Path.home() / ".config/example-app-runner"


def configured() -> bool:
    here = folder()
    return (here / "token").exists() or (here / "app-id").exists()


def token() -> str | None:
    """The token for a runner program's call, or None when this machine has no identity."""
    here = folder()
    # A token file wins: install writes it before it removes a GitHub App, and the id of a
    # GitHub App last.
    given = _given_token(here)
    if given is not None or not (here / "app-id").exists():
        return given
    # Install writes under the same lock, so the GitHub App read here is the one in place, and
    # a token made for one that install has since replaced never overwrites the new one's.
    with _locked(here):
        given = _given_token(here)
        if given is not None:
            return given
        try:
            app_id = (here / "app-id").read_text().strip()
            installation = (here / "installation-id").read_text().strip()
        except OSError as error:
            raise IdentityError(
                f"cannot read the files of the GitHub App in {here}: {error.strerror}"
            ) from None
        saved = _read(here / "installation-token.json")
        if saved.get("token") and saved.get("expires", 0) - time.time() > MARGIN:
            return str(saved["token"])
        # Every poll of every runner program comes here while the token is missing, so a
        # cooldown GitHub asked for holds them all, as the budget's pause does (AGENTS.md).
        until = _read(here / PAUSE).get("until", 0)
        if until > time.time():
            raise IdentityError(
                "GitHub's rate limit holds the token requests of the GitHub App until "
                + time.strftime("%H:%M:%S", time.localtime(until))
            )
        try:
            made = _installation_token(app_id, here / "app-key.pem", installation)
        except IdentityError as error:
            if error.until:
                _write(here / PAUSE, json.dumps({"until": error.until}))
            raise
        _write(here / "installation-token.json", json.dumps(made))
        return str(made["token"])


def install(
    repository: str,
    *,
    token_file: Path | None = None,
    app: tuple[str, Path] | None = None,
) -> str:
    """Check the new identity, then put it in place of the old one. Returns its kind."""
    if token_file is not None:
        try:
            given = _one_line(token_file.read_text())
        except OSError as error:
            raise IdentityError(f"cannot read {token_file}: {error.strerror}") from None
        _check(repository, given)
        here = _prepared()
        with _locked(here):
            _write(here / "token", given + "\n")
            for name in APP_FILES:
                (here / name).unlink(missing_ok=True)
        return "a token"
    assert app is not None
    app_id, key = app
    if not app_id.isdigit():
        raise IdentityError("the ID of the GitHub App is the number on its settings page (App ID)")
    try:
        key_text = key.read_text()
    except OSError as error:
        raise IdentityError(f"cannot read {key}: {error.strerror}") from None
    installed = _github(
        "GET", f"/repos/{repository}/installation", _signed(app_id, key)
    )
    installation = str(installed["id"])
    made = _installation_token(app_id, key, installation)
    _check(repository, made["token"])
    here = _prepared()
    # After a renewal for the GitHub App this replaces, which holds the lock while it asks
    # GitHub.
    with _locked(here):
        _write(here / "app-key.pem", key_text)
        _write(here / "installation-id", installation + "\n")
        _write(here / "installation-token.json", json.dumps(made))
        (here / PAUSE).unlink(missing_ok=True)
        # Last: a program that sees the id of the GitHub App finds everything else already there.
        _write(here / "app-id", app_id + "\n")
        (here / "token").unlink(missing_ok=True)
    return "a GitHub App"


def _given_token(here: Path) -> str | None:
    try:
        return _one_line((here / "token").read_text())
    except FileNotFoundError:
        return None
    except OSError as error:
        raise IdentityError(f"cannot read {here / 'token'}: {error.strerror}") from None


def _one_line(text: str) -> str:
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].strip() or len(lines[0].split()) != 1:
        raise IdentityError("the token file must hold the token alone, on one line")
    return lines[0].strip()


def _prepared() -> Path:
    here = folder()
    here.mkdir(mode=0o700, parents=True, exist_ok=True)
    here.chmod(0o700)
    return here


def _write(path: Path, text: str) -> None:
    # mkstemp makes the file for this user alone (0600) before anything is written to it.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(text)
    temporary.replace(path)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except OSError, ValueError:
        return {}


@contextlib.contextmanager
def _locked(here: Path) -> Iterator[None]:
    # Programs that start together make one installation token, not one each.
    with (here / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _check(repository: str, given: str) -> None:
    # Imported here: github_budget imports this module only when a runner identity is asked for.
    from github_budget import raw_gh

    environment = {
        key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"
    }
    environment["GH_TOKEN"] = given
    asked = [[f"repos/{repository}/{path}"] for _, path in CHECKS]
    asked.append(["-X", "POST", f"repos/{repository}/{WRITE_CHECK[1]}", "--silent"])
    for (what, _), arguments in zip((*CHECKS, WRITE_CHECK), asked, strict=True):
        try:
            done = subprocess.run(
                [raw_gh(), "api", *arguments],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise IdentityError(
                f"could not run gh to check the new identity: {error}"
            ) from None
        if done.returncode:
            reason = (done.stderr.strip().splitlines() or ["no answer"])[-1].replace(
                given, "[token]"
            )
            raise IdentityError(f"the new identity cannot {what}: {reason}")


def _installation_token(app_id: str, key: Path, installation: str) -> dict[str, Any]:
    answer = _github(
        "POST", f"/app/installations/{installation}/access_tokens", _signed(app_id, key)
    )
    try:
        expires = datetime.fromisoformat(answer["expires_at"]).timestamp()
        return {"token": str(answer["token"]), "expires": expires}
    except KeyError, TypeError, ValueError:
        raise IdentityError("GitHub's answer held no installation token") from None


def _unpadded(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signed(app_id: str, key: Path) -> str:
    """A JSON web token that names the GitHub App, signed with its key, good for nine minutes."""
    now = int(time.time())
    header = _unpadded(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    # A minute back for a clock that runs ahead of GitHub's; GitHub allows ten minutes at most.
    claims = {"iat": now - 60, "exp": now + 540, "iss": int(app_id)}
    payload = _unpadded(json.dumps(claims).encode())
    try:
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key)],
            input=f"{header}.{payload}".encode(),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise IdentityError(
            f"could not run openssl to sign with the key of the GitHub App: {error}"
        ) from None
    if signed.returncode or not signed.stdout:
        raise IdentityError(f"openssl could not sign with the private key of the GitHub App {key}")
    return f"{header}.{payload}.{_unpadded(signed.stdout)}"


def _github(method: str, path: str, signed: str) -> dict[str, Any]:
    url = os.environ.get("WORKFLOW_GITHUB_API_URL", "https://api.github.com") + path
    request = urllib.request.Request(
        url,
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {signed}",
            "Content-Type": "application/json",
            "User-Agent": "example-app-runner",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = str(json.loads(error.read()).get("message", ""))
        except OSError, ValueError, AttributeError:
            message = ""
        raise IdentityError(
            f"GitHub answered {error.code} to {method} {path}: {message}",
            until=_cooldown(error, message),
        ) from None
    except (OSError, ValueError) as error:
        raise IdentityError(f"could not ask GitHub {method} {path}: {error}") from None
    if not isinstance(value, dict):
        raise IdentityError(f"GitHub's answer to {method} {path} was not an object")
    return value


def _cooldown(error: urllib.error.HTTPError, message: str) -> float:
    """When a refusal is GitHub's rate limit, the time it allows the next request."""
    if error.code not in (403, 429):
        return 0
    wait = error.headers.get("Retry-After", "")
    reset = error.headers.get("X-RateLimit-Reset", "")
    if wait.isdigit():
        return time.time() + max(int(wait), 1)
    if error.headers.get("X-RateLimit-Remaining") == "0" and reset.isdigit():
        return float(reset)
    # A secondary limit without Retry-After: wait at least a minute.
    if error.code == 429 or "rate limit" in message.lower():
        return time.time() + 60
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install the runner programs' GitHub identity."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    installing = commands.add_parser("install")
    installing.add_argument("--repository", required=True)
    kind = installing.add_mutually_exclusive_group(required=True)
    kind.add_argument("--token", type=Path, help="a file holding a fine-grained token")
    kind.add_argument("--app", nargs=2, metavar=("APP_ID", "KEY_FILE"))
    arguments = parser.parse_args()
    try:
        made = install(
            arguments.repository,
            token_file=arguments.token,
            app=(arguments.app[0], Path(arguments.app[1])) if arguments.app else None,
        )
    except IdentityError as error:
        print(f"The runners' GitHub identity was not changed: {error}", file=sys.stderr)
        sys.exit(1)
    print(
        f"The runner programs now use their own GitHub identity, {made}, from {folder()}."
    )


if __name__ == "__main__":
    main()
