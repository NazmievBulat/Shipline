"""Independent stop-hook probes for PR 97. Only external services are stand-ins."""

import fcntl
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"
NOW = 1_800_000_000
QUESTION = (
    "Recommended: refresh example-app against main and retain the fix.\n"
    "Owner only: yes\nReply refresh or leave draft."
)

GH = '''#!/usr/bin/env python3
import json, os, pathlib, time
state = pathlib.Path(os.environ["BREAK_STATE"])
mode = (state / "github-mode").read_text()
if mode == "hang":
    time.sleep(30)
if mode == "fail":
    raise SystemExit(1)
print("true")
'''

CURL = '''#!/usr/bin/env python3
import json, os, pathlib, sys, tempfile, time
state = pathlib.Path(os.environ["BREAK_STATE"])
sys.stdin.read()
mode = (state / "push-mode").read_text()
if mode == "hang":
    time.sleep(30)
if mode == "fail":
    raise SystemExit(22)
record = {}
for arg in sys.argv[1:]:
    if "@" in arg and arg.split("@", 1)[0] in ("title", "message", "url"):
        key, filename = arg.split("@", 1)
        record[key] = pathlib.Path(filename).read_text()
call = pathlib.Path(tempfile.mkdtemp(dir=state / "pushes"))
(call / "record.json").write_text(json.dumps(record))
if mode == "barrier":
    deadline = time.monotonic() + 5
    while len(list((state / "pushes").glob("*/record.json"))) < 2:
        if time.monotonic() > deadline:
            raise SystemExit(23)
        time.sleep(0.01)
'''


class Installation:
    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "example-app"
        self.repo.mkdir()
        self.notes = self.repo / ".claude/worktrees/ticket-7/.local/workflow"
        self.notes.mkdir(parents=True)
        self.state = root / "state"
        (self.state / "pushes").mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.home = root / "home"
        (self.home / "agents").mkdir(parents=True)
        self.keys = self.home / "agents/.env"
        self.keys.write_text("PUSHOVER_TOKEN=ExampleToken\nPUSHOVER_USER=ExampleUser\n")
        self.keys.chmod(0o600)
        self.scratch = root / "scratch"
        self.scratch.mkdir()
        self.scripts = root / "scripts"
        self.scripts.mkdir()
        for name in ("notify", "limit-reset", "screen-borders", "workflow_config.py"):
            shutil.copy2(SCRIPTS / name, self.scripts / name)
        for name, source in (("gh", GH), ("curl", CURL)):
            tool = self.bin / name
            tool.write_text(source)
            tool.chmod(0o700)
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "TMPDIR": str(self.scratch),
            "BREAK_STATE": str(self.state),
            "SHIPLINE_REPO": str(self.repo),
            "WORKFLOW_SESSION": "ticket-7",
            "WORKFLOW_NOTIFY_SECONDS": "0.5",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "LANG": "C.UTF-8",
        }
        subprocess.run(
            ["git", "init", "--quiet", str(self.repo)],
            env=self.env, capture_output=True, check=True, timeout=5,
        )
        (self.repo / "shipline.json").write_text("{}\n")
        (self.state / "github-mode").write_text("draft")
        (self.state / "push-mode").write_text("ok")

    def event(self, event: dict, at: int = NOW):
        return subprocess.run(
            ["bash", str(self.scripts / "notify")],
            cwd=self.repo, env={**self.env, "WORKFLOW_NOW": str(at)},
            input=json.dumps(event), capture_output=True, text=True, timeout=8,
        )

    def stop(self, message: str = QUESTION, at: int = NOW):
        done = self.event({"hook_event_name": "Stop", "last_assistant_message": message}, at)
        assert done.returncode == 0 and done.stdout == done.stderr == ""
        return done

    def prompt(self, source: str):
        return self.event({"hook_event_name": "UserPromptSubmit", "source": source,
                           "prompt": "refresh"})

    def note(self, suffix: str) -> Path:
        return self.notes / f"ticket-7.{suffix}"

    def pushes(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in (self.state / "pushes").glob("*/record.json")]

    def assert_asking(self, at: int = NOW):
        assert self.note("idle").exists()
        parts = self.note("asking").read_text().split()
        assert len(parts) == 2 and parts[0] == str(at) and parts[1].isdigit()


@pytest.fixture
def installation(tmp_path):
    return Installation(tmp_path)


@pytest.mark.parametrize("bridge", [False, True])
@pytest.mark.parametrize("marker", ["Owner only: yes", "Owner only: no",
                                    "**Owner only:** YES.", "> Owner only: no"])
@pytest.mark.parametrize("reply", ["Reply refresh or leave draft.",
                                   'Reply "refresh" or "leave draft".', "Reply 1 or 2."])
def test_question_rings_and_survives_restart_and_background_resume(installation, bridge, marker, reply):
    app = installation
    if bridge:
        app.note("codex").touch()
    message = f"Recommended: refresh example-app.\n{marker}\n{reply}\n  \n"
    app.stop(message)
    app.assert_asking()
    before = app.note("asking").read_text()
    app.prompt("background_task_output")
    # A fresh hook process, with the same persisted state, more than a day later.
    app.stop(message, NOW + 27 * 3600)
    app.assert_asking()
    assert app.note("asking").read_text() == before
    assert len(app.pushes()) == 1
    if bridge:
        assert not app.note("question").exists()
    else:
        question = json.loads(app.note("question").read_text())
        assert question["asked"] == before.strip()
        if "refresh or" in reply:
            assert "reply" not in question  # No new automatic answer syntax.


@pytest.mark.parametrize("message", [
    "Refreshed example-app; tests passed.",
    "Recommended: refresh example-app.\nI will reply after the checks finish.",
    "Owner only: yes\nReply refresh or leave draft.\n\nThe answer arrived; work resumed.",
    "The text Owner only: yes belongs in the question.\nTests passed.",
    "Owner only: yesterday\nTests passed.",
    "Owner only: yes, already answered.\nTests passed.",
])
def test_progress_clears_old_question_without_a_new_ring(installation, message):
    app = installation
    app.stop()
    app.prompt("background_task_output")
    app.stop(message, NOW + 60)
    assert len(app.pushes()) == 1
    assert app.note("idle").exists()
    assert not app.note("asking").exists()
    assert not app.note("question").exists()


def test_owner_answer_allows_a_new_ring_and_new_asking_time(installation):
    app = installation
    app.stop()
    app.prompt("user_input")
    assert not app.note("asking").exists()
    app.stop(at=NOW + 120)
    app.assert_asking(NOW + 120)
    assert len(app.pushes()) == 2


@pytest.mark.parametrize("fault", ["push-fail", "push-hang", "github-fail", "github-hang",
                                   "missing-keys", "missing-url", "missing-config"])
def test_service_failures_and_missing_optional_files_preserve_the_question(installation, fault):
    app = installation
    if fault.startswith("push-"):
        (app.state / "push-mode").write_text(fault.removeprefix("push-"))
    elif fault.startswith("github-"):
        (app.state / "github-mode").write_text(fault.removeprefix("github-"))
    elif fault == "missing-keys":
        app.keys.unlink()
    elif fault == "missing-config":
        (app.repo / "shipline.json").unlink()
    # No session URL is installed: notification still must work without one.
    started = time.monotonic()
    app.stop()
    assert time.monotonic() - started < 4
    app.assert_asking()
    (app.state / "push-mode").write_text("ok")
    (app.state / "github-mode").write_text("draft")
    if fault == "missing-keys":
        app.keys.write_text("PUSHOVER_TOKEN=ExampleToken\nPUSHOVER_USER=ExampleUser\n")
    app.stop(at=NOW + 3600)
    app.assert_asking()
    assert len(app.pushes()) == 1


def test_missing_notes_are_recreated(installation):
    app = installation
    shutil.rmtree(app.notes)
    app.stop()
    app.assert_asking()
    assert len(app.pushes()) == 1


def test_notification_does_not_wait_for_the_tick_lock(installation):
    app = installation
    with (app.repo / ".git/shipline-tick.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.stop()
        app.assert_asking()
        assert len(app.pushes()) == 1


def test_shell_metacharacters_in_reply_words_are_never_executed(installation):
    app = installation
    message = "Recommended: refresh.\nOwner only: yes\nReply $(touch injected) or `touch injected` ."
    app.stop(message)
    app.assert_asking()
    assert not (app.repo / "injected").exists()
    assert app.pushes()[0]["message"] == message
    assert "ExampleToken" not in app.note("asking").read_text()
    assert "ExampleToken" not in app.note("question").read_text()


def test_wake_reads_the_question_after_a_restart(installation):
    app = installation
    app.note("codex").touch()
    app.stop()
    # Exercise the actual consumer of these files, independently of wake's network reads.
    wake = (SCRIPTS / "wake").read_text()
    reader = "asking_notes() {" + wake.split("asking_notes() {", 1)[1].split("\n}\n", 1)[0] + "\n}"
    done = subprocess.run(
        ["bash", "-c", reader + '\nmain=$SHIPLINE_REPO\nSHIPLINE_STATE_FOLDER=.local/workflow\nasking_notes'],
        env=app.env, text=True, capture_output=True, timeout=5,
    )
    assert done.returncode == 0
    assert done.stdout == f"ticket-7 {NOW}\n"


@pytest.mark.parametrize("reply", ["Reply refresh or leave draft.", 'Reply "refresh" or "leave draft".'])
def test_native_codex_callback_then_bridge_stop_keeps_the_question(installation, reply):
    app = installation
    app.note("codex").touch()
    message = QUESTION.rsplit("\n", 1)[0] + "\n" + reply
    payload = {"type": "agent-turn-complete", "last-assistant-message": message,
               "turn-id": "example-turn", "thread-id": "example-thread"}
    done = subprocess.run(
        ["bash", str(app.scripts / "notify"), "codex", json.dumps(payload)],
        cwd=app.repo, env={**app.env, "WORKFLOW_CODEX_BRIDGE": "1"},
        capture_output=True, text=True, timeout=5,
    )
    assert done.returncode == 0 and done.stdout == done.stderr == ""
    assert app.pushes() == []
    turn = json.loads(app.note("codex-turn.json").read_text())
    assert turn["last-assistant-message"] == message
    app.stop(turn["last-assistant-message"])
    app.assert_asking()
    app.event({"hook_event_name": "UserPromptSubmit", "prompt": "__WORKFLOW_CODEX_TURN__"})
    app.stop(message, at=NOW + 27 * 3600)
    app.assert_asking()
    assert len(app.pushes()) == 1, "bridge recovery repeated the phone push without an owner answer"


def test_two_workers_stopping_at_once_keep_separate_questions(installation):
    app = installation
    other_notes = app.repo / ".claude/worktrees/ticket-8/.local/workflow"
    other_notes.mkdir(parents=True)
    other = Installation.__new__(Installation)
    other.__dict__.update(app.__dict__)
    other.env = {**app.env, "WORKFLOW_SESSION": "ticket-8"}
    (app.state / "push-mode").write_text("barrier")
    with ThreadPoolExecutor(max_workers=2) as workers:
        calls = [workers.submit(worker.stop) for worker in (app, other)]
        for call in calls:
            call.result(timeout=8)
    assert {push["title"] for push in app.pushes()} == {"ticket-7 needs you", "ticket-8 needs you"}
    assert (other_notes / "ticket-8.asking").read_text() == app.note("asking").read_text()


@pytest.mark.parametrize("reply", ["Reply refresh or leave draft.", 'Reply "refresh" or "leave draft".'])
def test_two_overlapping_deliveries_of_one_stop_ring_once(installation, reply):
    app = installation
    message = QUESTION.rsplit("\n", 1)[0] + "\n" + reply
    (app.state / "push-mode").write_text("barrier")
    with ThreadPoolExecutor(max_workers=2) as workers:
        calls = [workers.submit(app.stop, message) for _ in range(2)]
        for call in calls:
            call.result(timeout=8)
    app.assert_asking()
    assert len(app.pushes()) == 1, "two concurrent copies of the same Stop sent two phone pushes"
