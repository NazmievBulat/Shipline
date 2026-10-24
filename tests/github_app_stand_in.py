"""A stand-in for the two requests a GitHub App makes with its key, and a key made for it."""

import base64
import json
import shutil
import subprocess
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_TOKEN = "ghs_APP-INSTALLATION-SECRET"
REPOSITORY = "example-org/example-app"


def token_of(app_id: int) -> str:
    """The installation token GitHub gives the app with this id; app 123 gets APP_TOKEN."""
    return APP_TOKEN if app_id == 123 else f"{APP_TOKEN}-{app_id}"


def _issuer(authorization: str) -> int:
    payload = authorization.removeprefix("Bearer ").split(".")[1]
    return int(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["iss"])


class App:
    def __init__(self, key: Path, public: Path, url: str) -> None:
        self.key = key
        self.public = public
        # Set as WORKFLOW_GITHUB_API_URL, where github_identity.py sends the app's requests.
        self.url = url
        self.requests: list[dict[str, Any]] = []
        self.expires_in = 3600
        self.refuse: int | None = None
        # The message and headers of a refusal, such as Retry-After.
        self.refusal_message = "A JSON web token could not be decoded"
        self.refusal_headers: dict[str, str] = {}
        # The installation token requests of these app ids wait until `release` is set.
        self.held: set[int] = set()
        self.release = threading.Event()


@contextmanager
def served(folder: Path) -> Generator[App]:
    """A new key in `folder`, and GitHub's installation of it on REPOSITORY, id 42."""
    if shutil.which("openssl") is None:
        raise AssertionError("openssl is needed: it signs a GitHub App's requests")
    key = folder / "app-key.pem"
    public = folder / "app-public.pem"
    subprocess.run(
        [
            *("openssl", "genpkey", "-algorithm", "RSA"),
            *("-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)],
        check=True,
        capture_output=True,
    )
    state = App(key, public, "")

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "body": self.rfile.read(length).decode() if length else "",
                }
            )
            if state.refuse:
                self.send_response(state.refuse)
                body: dict[str, Any] = {"message": state.refusal_message}
            elif self.path == f"/repos/{REPOSITORY}/installation":
                self.send_response(200)
                body = {"id": 42}
            elif self.path == "/app/installations/42/access_tokens":
                issuer = _issuer(self.headers.get("Authorization", ""))
                if issuer in state.held:
                    state.release.wait(timeout=20)
                self.send_response(201)
                expires = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + state.expires_in)
                )
                body = {"token": token_of(issuer), "expires_at": expires}
            else:
                self.send_response(404)
                body = {"message": "Not Found"}
            if state.refuse:
                for name, value in state.refusal_headers.items():
                    self.send_header(name, value)
            data = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _answer
        do_POST = _answer

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
