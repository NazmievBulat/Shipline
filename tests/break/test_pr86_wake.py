"""Exercise the real wake script with local boundary stand-ins, one file per run."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_pr86_recovery import Repository, ROOT


@pytest.fixture
def wake(tmp_path):
    repo = Repository(tmp_path)
    repo.broken_branch()
    assert repo.run().returncode == 0
    (repo.main / "shipline.json").write_text(json.dumps({"repository": "example-org/example-app"}))
    (repo.state / "orchestrator").write_text("example-orchestrator\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    scripts = {
        "gh": "#!/bin/bash\necho none\n",
        "tmux": "#!/bin/bash\nexit 1\n",
        "curl": "#!/bin/bash\nexit 88\n",
        "nproc": "#!/bin/bash\necho 4\n",
        "df": "#!/bin/bash\nprintf 'Filesystem Inodes IUsed IFree IUse%% Mounted on\\nexample 1000 100 900 10%% .\\n'\n",
        "claude": f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
root = Path(os.environ["BREAK_TEST_ROOT"])
if sys.argv[1] == "agents":
    print(json.dumps([{{"kind": "interactive", "cwd": os.environ["SHIPLINE_REPO"],
                       "name": "example-orchestrator", "status": "busy"}}]))
else:
    if (root / "send-fails").exists():
        sys.exit(1)
    with (root / "messages.jsonl").open("a") as output:
        output.write(json.dumps(sys.argv[-1]) + "\\n")
    print(json.dumps({{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "name": "SendMessage", "id": "example-send"}}]}}}}))
    print(json.dumps({{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "example-send", "content": "sent"}}]}}}}))
''',
    }
    for name, source in scripts.items():
        path = binary / name
        path.write_text(source)
        path.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    load = tmp_path / "loadavg"
    load.write_text("0.10 0.10 0.10 1/100 10\n")
    repo.env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                     "PATH": str(binary) + ":" + repo.env["PATH"], "SHIPLINE_REPO": str(repo.main),
                     "BREAK_TEST_ROOT": str(tmp_path), "WORKFLOW_LOADAVG_FILE": str(load),
                     "WORKFLOW_PROC_ROOT": str(tmp_path / "proc"),
                     "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
                     "WORKFLOW_PRESSURE_FILE": str(tmp_path / "pressure"),
                     "WORKFLOW_SWAPS_FILE": str(tmp_path / "swaps"), "TZ": "UTC"})
    return repo


def run_wake(repo, now):
    return subprocess.run(["bash", str(ROOT / "scripts/workflow/wake")], cwd=repo.main,
                          env={**repo.env, "WORKFLOW_NOW": str(now)},
                          capture_output=True, text=True, timeout=25)


def messages(repo):
    path = repo.root / "messages.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("hour", [2, 12, 21])
def test_repair_is_delivered_to_busy_orchestrator_at_any_hour_exactly_once(wake, hour):
    now = 1793836800 + hour * 3600
    line = wake.notes()[0]
    result = run_wake(wake, now)
    assert result.returncode == 0, result.stderr
    assert len(messages(wake)) == 1 and line in messages(wake)[0]
    assert not wake.notes()
    assert len(list((wake.state / "git-repair.sent").glob("*.json"))) == 1
    result = run_wake(wake, now + 360)
    assert result.returncode == 0, result.stderr
    assert len(messages(wake)) == 1


def test_failed_delivery_is_durable_and_retried_once(wake):
    now = 1793880000
    (wake.root / "send-fails").touch()
    result = run_wake(wake, now)
    assert result.returncode == 1
    assert len(wake.notes()) == 1 and not messages(wake)
    (wake.root / "send-fails").unlink()
    result = run_wake(wake, now + 360)
    assert result.returncode == 0, result.stderr
    assert len(messages(wake)) == 1
    assert not wake.notes()
    assert run_wake(wake, now + 720).returncode == 0
    assert len(messages(wake)) == 1
