"""Run the unchanged wake script with local stand-ins; never contact a messenger."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def wake(tmp_path):
    root = tmp_path / "example-app"
    scripts = root / "scripts/workflow"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "scripts/workflow/wake"
    shutil.copy2(source, scripts / "wake")
    state = root / ".local/workflow"
    state.mkdir(parents=True)
    (state / "orchestrator").write_text("example-orchestrator\n")
    config = (
        f"SHIPLINE_REPO={shlex.quote(str(root))}\n"
        "SHIPLINE_STATE_FOLDER=.local/workflow\n"
        "SHIPLINE_CONFIG_JSON='{}'\n"
    )
    (scripts / "github_budget.py").write_text("pass\n")
    (scripts / "workflow_config.py").write_text(f"print({config!r})\n")
    (scripts / "decisions.py").write_text(
        'print(\'{"cursor":0,"held":[],"events":[]}\')\n'
    )
    (scripts / "workflow_commands.py").write_text('print("none")\n')
    (scripts / "priority.jq").write_text("")
    (scripts / "session").write_text('[[ $1 != load ]] || echo "load 0"\n')
    (scripts / "usage").write_text("echo 'five_hour 20 9999999999 started'\n")
    programs = root / "programs"
    programs.mkdir()
    messenger = programs / "claude"
    messenger.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ["EXAMPLE_APP_ROOT"])
if sys.argv[1] == "agents":
    print(json.dumps([{"kind":"interactive", "cwd":str(root),
                       "name":"example-orchestrator", "status":"busy"}]))
    raise SystemExit(0)
with (root / "messages.jsonl").open("a") as output:
    output.write(json.dumps(sys.argv[-1]) + "\\n")
if (root / "send-fails").exists():
    raise SystemExit(1)
if (root / "hold-send").exists():
    (root / "send-entered").touch()
    deadline = time.monotonic() + 5
    while not (root / "release-send").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not (root / "release-send").exists():
        raise SystemExit(1)
print(json.dumps({"type":"assistant", "message":{"content":[
    {"type":"tool_use", "name":"SendMessage", "id":"example-message"}]}}))
print(json.dumps({"type":"user", "message":{"content":[
    {"type":"tool_result", "tool_use_id":"example-message", "content":"sent"}]}}))
''')
    messenger.chmod(0o700)
    env = {**os.environ, "HOME": str(root / "example-home"),
           "EXAMPLE_APP_ROOT": str(root), "PATH": f"{programs}:{os.environ['PATH']}",
           "TZ": "UTC"}
    for key in ("SHIPLINE_SHADOW", "SHIPLINE_SHADOW_NOTES", "SHIPLINE_CONFIG_JSON"):
        env.pop(key, None)

    def command(offset=0):
        return ["bash", str(scripts / "wake")], {
            **env, "WORKFLOW_NOW": str(1793876400 + offset),
        }

    def run(offset=0):
        args, child_env = command(offset)
        return subprocess.run(args, env=child_env, cwd=root, capture_output=True,
                              text=True, timeout=15)

    def messages():
        path = root / "messages.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    pending = state / "runner-downloads.pending/20.json"
    pending.parent.mkdir()
    line = "Runner example-runner failed image downloads again in run 20: unexpected EOF; repair its cache"
    pending.write_text(json.dumps({"line": line}))
    return SimpleNamespace(root=root, state=state, pending=pending, run=run,
                           command=command, messages=messages, line=line)


def test_busy_daytime_orchestrator_receives_download_notice_once(wake):
    done = wake.run()
    assert done.returncode == 0, done.stderr
    assert len(wake.messages()) == 1
    assert wake.messages()[0].count(wake.line) == 1
    assert not wake.pending.exists()
    assert (wake.state / "runner-downloads.sent/20.json").exists()
    assert wake.run(360).returncode == 0
    assert len(wake.messages()) == 1


def test_failed_delivery_keeps_notice_until_later_success(wake):
    (wake.root / "send-fails").touch()
    assert wake.run().returncode == 1
    assert wake.pending.exists()
    (wake.root / "send-fails").unlink()
    assert wake.run(360).returncode == 0
    assert not wake.pending.exists()
    assert len(wake.messages()) == 2


def test_missing_orchestrator_retains_download_notice(wake):
    (wake.state / "orchestrator").unlink()
    assert wake.run().returncode == 0
    assert wake.pending.exists()
    assert wake.messages() == []


def test_lost_check_and_download_notices_share_delivery_without_loss(wake):
    lost = wake.state / "lost-checks.pending/example.json"
    lost.parent.mkdir()
    lost.write_text(json.dumps({"check": "CI result", "run": 30, "number": 7}))
    assert wake.run().returncode == 0
    message = wake.messages()[0]
    assert wake.line in message
    assert "lost check: CI result of run 30 on pull request 7" in message
    assert (wake.state / "lost-checks.sent/example.json").exists()
    assert (wake.state / "runner-downloads.sent/20.json").exists()
