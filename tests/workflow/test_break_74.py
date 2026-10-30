"""Independent break tests for stacked children after their parent merges."""

import fcntl
import json
import subprocess
import sys
import time
from copy import deepcopy

import pytest
from workflow.test_merge_driver import github, handed


def _folder(case):
    return case.workflow.repo / ".local/workflow"


def _note(case):
    return _folder(case) / "merge-wanted-7"


def _saved(case):
    return json.loads((case.workflow.state / "github.json").read_text())


def _child(case, *, draft=True, keep=False):
    flow = case.workflow
    tree = flow.worktree(8)
    flow.git("worktree", "add", "-b", "ticket/eight", str(tree), case.head)
    if keep:
        (tree / "child.txt").write_text("example child\n")
        flow.git("add", "child.txt", cwd=tree)
    flow.git("commit", "--allow-empty", "-m", "test: example child", cwd=tree)
    head = flow.git("rev-parse", "HEAD", cwd=tree)
    flow.git("push", "origin", "HEAD:ticket/eight", cwd=tree)
    case.pulls["8"] = {
        **deepcopy(case.pulls["7"]),
        "headRefName": "ticket/eight",
        "headRefOid": head,
        "baseRefName": "ticket/seven",
        "isDraft": draft,
    }
    case.api["repos/example-org/example-app/pulls/8/reviews"] = []
    flow.list_pull_request(8, draft=draft)
    (flow.state / "pr/8").write_text(f"OPEN ticket/eight {head}\n")
    if not keep:
        flow.git("worktree", "remove", str(tree))
    case.save()
    return tree, head


def _run(case):
    done = case.workflow.run_python("merge_driver.py")
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def _quiet(case, tree, head):
    flow = case.workflow
    assert _saved(case)["pulls"]["8"]["baseRefName"] == "main"
    assert not tree.exists()
    assert flow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0] == head
    assert not (_folder(case) / "merge-review-8").exists()
    for kind in ("pending", "sent"):
        assert not list((_folder(case) / f"merge-handbacks.{kind}").glob("*"))
    assert not flow.sent()


@pytest.mark.parametrize("draft", [True, False], ids=["draft", "ready"])
@pytest.mark.parametrize("checks", ["missing", "pending", "rerun"])
def test_repeated_ticks_leave_unstarted_children_quiet(handed, draft, checks):
    tree, head = _child(handed, draft=draft)
    handed.pulls["8"]["statusCheckRollup"] = [] if checks == "missing" else [{
        "name": "CI result", "status": "IN_PROGRESS", "conclusion": None,
    }]
    handed.save()
    first = _run(handed)
    assert first.count("stacked 8 moved to main; its next session merges main") == 1
    _quiet(handed, tree, head)
    for _ in range(3):
        assert "stacked 8" not in _run(handed)
        _quiet(handed, tree, head)
    if checks == "rerun":
        data = _saved(handed)
        data["pulls"]["8"]["statusCheckRollup"] = [{
            "name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS",
        }]
        (handed.workflow.state / "github.json").write_text(json.dumps(data))
    if not draft:
        done = handed.workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        starts = [c for c in handed.workflow.calls("tmux") if " new-session " in c]
        assert len(starts) == 1 and "review-8" in starts[0]
        assert not (_folder(handed) / "merge-review-8").exists()


def test_start_lock_stays_bounded_then_finishes_once(handed):
    tree, head = _child(handed)
    with (_folder(handed) / "start-8.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for _ in range(2):
            before = time.monotonic()
            output = _run(handed)
            assert time.monotonic() - before < 15
            assert "stacked 8 moved" not in output
            assert json.loads(_note(handed).read_text())["children"] == ["8"]
            assert not (_folder(handed) / "merge-review-8").exists()
    assert _run(handed).count("stacked 8 moved") == 1
    _quiet(handed, tree, head)


@pytest.mark.parametrize("base", ["ticket/seven", "main"])
def test_restart_replays_saved_children_without_a_worktree(handed, base):
    tree, head = _child(handed)
    note = json.loads(_note(handed).read_text())
    note["children"] = ["8"]
    _note(handed).write_text(json.dumps(note))
    handed.pulls["7"]["state"] = "MERGED"
    handed.pulls["8"]["baseRefName"] = base
    handed.save()
    assert _run(handed).count("stacked 8 moved") == 1
    assert not _note(handed).exists()
    _quiet(handed, tree, head)


def test_unreadable_child_keeps_retry_state(handed):
    tree, head = _child(handed)
    handed.pulls["8"]["fail_view"] = True
    handed.save()
    assert "stacked 8 moved" not in _run(handed)
    assert json.loads(_note(handed).read_text())["children"] == ["8"]
    assert not (_folder(handed) / "merge-review-8").exists()
    data = _saved(handed)
    data["pulls"]["8"].pop("fail_view")
    (handed.workflow.state / "github.json").write_text(json.dumps(data))
    assert _run(handed).count("stacked 8 moved") == 1
    _quiet(handed, tree, head)


@pytest.mark.parametrize("session", ["ticket-8", "review-8", "cloud-8"])
def test_running_session_is_not_silently_skipped(handed, session):
    tree, head = _child(handed)
    if session == "cloud-8":
        (_folder(handed) / session).write_text(json.dumps({
            "url": "", "status": "starting", "last_comment_at": None,
        }))
    else:
        # The stopped parent's process slot is retained by this fixture.
        (handed.workflow.state / "tmux/review-7").touch()
        handed.workflow.running(session)
        (handed.workflow.state / "tmux/review-7").unlink()
    output = _run(handed)
    assert "stacked 8 moved" not in output
    assert "its own session must merge main" in output
    assert (_folder(handed) / "merge-review-8").exists()
    assert not tree.exists()
    assert handed.workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0] == head


@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty"])
def test_present_worktree_still_uses_existing_handback_path(handed, dirty):
    tree, head = _child(handed, keep=True)
    if dirty:
        (tree / "child.txt").write_text("unfinished example\n")
    output = _run(handed)
    assert "stacked 8 moved" not in output
    assert (_folder(handed) / "merge-review-8").exists()
    remote = handed.workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0]
    if dirty:
        assert remote == head
        assert (tree / "child.txt").read_text() == "unfinished example\n"
    else:
        assert remote != head
        assert handed.workflow.git("rev-parse", "HEAD^2", cwd=tree) == handed.workflow.git(
            "rev-parse", "origin/main"
        )


def test_two_ticks_do_not_create_a_handback(handed):
    tree, head = _child(handed)
    flow = handed.workflow
    command = ["python3", "-B", str(flow.script.parent / "merge_driver.py")]
    processes = [subprocess.Popen(
        command, cwd=flow.repo, env=flow.env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for _ in range(2)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    _run(handed)
    _quiet(handed, tree, head)
    assert not _note(handed).exists()


def test_a_lost_patch_response_keeps_the_unstarted_child_retryable(handed):
    tree, head = _child(handed)
    flow = handed.workflow
    note = json.loads(_note(handed).read_text())
    note["children"] = ["8"]
    _note(handed).write_text(json.dumps(note))
    handed.pulls["7"]["state"] = "MERGED"
    handed.save()
    source = flow.bin / "gh.py"
    anchor = '''        number = args[3].split("/")[-1]
        data["pulls"][number]["baseRefName"] = "main"
        (state / "github.json").write_text(json.dumps(data))'''
    assert source.read_text().count(anchor) == 1
    source.write_text(source.read_text().replace(anchor, anchor + '''
        import time
        time.sleep(2)  # The update succeeded, but the response misses the deadline.
'''))
    # Shorten the real command deadline in memory; do not modify production files.
    launcher = '''import runpy, sys
from pathlib import Path
script = Path(sys.argv[1])
sys.path.insert(0, str(script.parent))
import merge_gate
original_command = merge_gate.run_command
def bounded_patch(*args, **kwargs):
    if args[:4] == ("gh", "api", "-X", "PATCH"):
        kwargs["timeout"] = 0.25
    return original_command(*args, **kwargs)
merge_gate.run_command = bounded_patch
sys.argv = [str(script)]
runpy.run_path(str(script), run_name="__main__")
'''
    done = subprocess.run(
        [sys.executable, "-B", "-c", launcher, str(flow.script.parent / "merge_driver.py")],
        cwd=flow.repo, env=flow.env, capture_output=True, text=True, timeout=15,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert _saved(handed)["pulls"]["8"]["baseRefName"] == "main"
    assert not tree.exists()
    assert flow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0] == head
    assert not (_folder(handed) / "merge-review-8").exists(), (
        "base PATCH succeeded, but its lost response caused a hand-back for an unstarted child"
    )
    assert json.loads(_note(handed).read_text())["children"] == ["8"]
