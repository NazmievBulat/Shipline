"""Independent real-Git adversarial checks for PR 86.

Run this file alone with requirements-test.txt installed. Set TMPDIR and
pytest --basetemp to directories inside the checkout.
"""

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPAIR = ROOT / "scripts/workflow/merge_health.py"
NAME = "Bulat Nazmiev"
EMAIL = "82903523+NazmievBulat@users.noreply.github.com"


class Repository:
    def __init__(self, root):
        self.root = root
        self.main = root / "example-app"
        self.origin = root / "origin.git"
        self.state = self.main / ".local/workflow"
        self.marker = root / "tick-1/attempted"
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": str(root / "empty-config"),
                    "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        (root / "empty-config").touch()
        self.git("init", "--bare", str(self.origin), cwd=root)
        self.git("init", "-b", "main", str(self.main), cwd=root)
        self.git("config", "user.name", NAME)
        self.git("config", "user.email", EMAIL)
        self.git("config", "gc.auto", "0")
        self.git("config", "core.logAllRefUpdates", "true")
        (self.main / "example.txt").write_text("original\n")
        self.git("add", "example.txt")
        self.git("commit", "-m", "test: initial state")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-u", "origin", "main")

    def git(self, *args, cwd=None, check=True, input=None):
        done = subprocess.run(["git", *args], cwd=cwd or self.main,
                              env=self.env, text=True, capture_output=True,
                              input=input, timeout=15)
        if check:
            assert done.returncode == 0, done.stderr
        return done.stdout.strip() if check else done

    def object(self, oid):
        return self.main / ".git/objects" / oid[:2] / oid[2:]

    def empty(self, oid):
        path = self.object(oid)
        path.chmod(0o600)
        path.write_bytes(b"")
        return path

    def broken_branch(self, name="ticket/example", pushed=True):
        if pushed:
            self.git("push", "origin", f"main:refs/heads/{name}")
        oid = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD",
                       "-m", "test: interrupted commit")
        self.git("update-ref", "-m", "interrupted write", f"refs/heads/{name}", oid)
        return oid, self.empty(oid)

    def worktree(self, name="example", managed=True):
        directory = self.main / (".claude/worktrees" if managed else "other-worktrees") / name
        self.git("worktree", "add", "-b", f"work/{name}", str(directory), "main")
        return directory

    def bad_cache(self, worktree):
        (worktree / "example.txt").write_text("staged version\n")
        self.git("add", "example.txt", cwd=worktree)
        tree = self.git("write-tree", cwd=worktree)
        (worktree / "example.txt").write_text("unstaged version\n")
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        self.empty(tree)
        return tree, index

    def run(self, *command, marker=None, env=None):
        return subprocess.run(
            [sys.executable, "-B", str(REPAIR), "--repair-step", str(self.main),
             str(self.state), str(marker or self.marker),
             *(command or ("git", "-C", str(self.main), "fetch", "origin"))],
            cwd=self.main, env=env or self.env, capture_output=True, text=True, timeout=25,
        )

    def notes(self):
        return [json.loads(path.read_text())["line"]
                for path in sorted((self.state / "git-repair.pending").glob("*.json"))]

    def next_tick(self, number=2):
        self.marker = self.root / f"tick-{number}/attempted"


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path)


def test_six_empty_objects_recover_without_changing_nonempty_objects(repo):
    oid, broken = repo.broken_branch()
    empties = [broken]
    for i in range(5):
        extra = repo.git("hash-object", "-w", "--stdin", input=f"interrupted {i}\n")
        empties.append(repo.empty(extra))
    before = {p: p.read_bytes() for p in (repo.main / ".git/objects").glob("??/*") if p.stat().st_size}
    result = repo.run()
    assert result.returncode == 0, result.stderr
    assert repo.git("rev-parse", "ticket/example") == repo.base
    assert all(not path.exists() for path in empties)
    assert all(path.read_bytes() == content for path, content in before.items())
    saved = list((repo.state / "git-repairs").glob("*/objects/??/*"))
    assert len(saved) == 6 and all(path.read_bytes() == b"" for path in saved)
    assert "moved 6 empty object files" in repo.notes()[0]
    assert oid not in (repo.main / ".git/logs/refs/heads/ticket/example").read_text()


def test_dirty_cache_tree_keeps_files_and_backs_up_index(repo):
    dirty = repo.worktree()
    healthy = repo.worktree("healthy")
    _, index = repo.bad_cache(dirty)
    before = index.read_bytes()
    healthy_index = Path(repo.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=healthy))
    healthy_before = healthy_index.read_bytes()
    (dirty / "untracked.txt").write_text("untracked\n")
    repo.broken_branch()
    result = repo.run()
    assert result.returncode == 0, result.stderr
    assert (dirty / "example.txt").read_text() == "unstaged version\n"
    assert (dirty / "untracked.txt").read_text() == "untracked\n"
    assert repo.git("diff", "--name-only", cwd=dirty) == "example.txt"
    assert healthy_index.read_bytes() == healthy_before
    backups = list((repo.state / "git-repairs").glob("*/indexes/example/index"))
    assert len(backups) == 1 and backups[0].read_bytes() == before


def test_unpushed_branch_remains_named_and_unmoved(repo):
    oid, _ = repo.broken_branch(pushed=False)
    result = repo.run()
    assert result.returncode != 0
    assert repo.git("rev-parse", "ticket/example") == oid
    assert "refs/heads/ticket/example: no usable pushed head; left alone" in repo.notes()[0]
    repo.next_tick()
    assert repo.run().returncode != 0
    assert len(repo.notes()) == 1


def test_healthy_and_unrelated_failures_do_not_repair(repo):
    assert repo.run().returncode == 0
    assert repo.run("git", "show", "unknown-example-ref").returncode != 0
    repo.git("remote", "set-url", "origin", str(repo.root / "missing-origin"))
    assert repo.run().returncode != 0
    assert not repo.marker.exists()
    assert not (repo.state / "git-repairs").exists()


def test_merge_lock_is_nonblocking_and_later_tick_can_recover(repo):
    _, broken = repo.broken_branch()
    repo.state.mkdir(parents=True)
    start = time.monotonic()
    with (repo.state / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = repo.run()
    assert time.monotonic() - start < 5
    assert result.returncode != 0 and broken.exists()
    assert "merge lock is busy" in repo.notes()[0]
    assert not (repo.state / "git-repairs").exists()
    repo.next_tick()
    assert repo.run().returncode == 0
    assert not broken.exists()
    assert not (repo.state / "git-repair.failed").exists()


def test_simultaneous_calls_share_one_repair_attempt(repo):
    repo.broken_branch()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repo.run(), range(2)))
    assert any(result.returncode == 0 for result in results)
    assert repo.git("rev-parse", "ticket/example") == repo.base
    assert len(list((repo.state / "git-repairs").iterdir())) == 1
    assert len(repo.notes()) == 1


def test_one_attempt_per_tick_even_after_new_corruption(repo):
    repo.broken_branch()
    assert repo.run().returncode == 0
    _, second = repo.broken_branch("ticket/second")
    assert repo.run().returncode != 0
    assert second.exists()
    assert len(repo.notes()) == 1
    repo.next_tick()
    assert repo.run().returncode == 0


def test_partial_repair_resumes_after_worktree_index_lock_is_released(repo):
    dirty = repo.worktree()
    tree, index = repo.bad_cache(dirty)
    repo.broken_branch()
    lock = index.with_name("index.lock")
    lock.touch()
    first = repo.run()
    assert first.returncode != 0 and "git reset failed" in first.stderr
    assert not repo.object(tree).exists()
    lock.unlink()
    repo.next_tick()
    second = repo.run()
    assert second.returncode == 0, second.stderr
    checked = repo.git("fsck", "--cache", "--no-reflogs", "--connectivity-only", "--no-dangling", check=False)
    assert checked.returncode == 0, "later tick left the repaired branch's worktree cache-tree broken"
    assert not (repo.state / "git-repair.failed").exists()
    assert (dirty / "example.txt").read_text() == "unstaged version\n"


def test_worktree_outside_managed_folder_is_not_reset(repo):
    outside = repo.worktree(managed=False)
    _, index = repo.bad_cache(outside)
    before = index.read_bytes()
    repo.broken_branch()
    result = repo.run()
    assert result.returncode == 0, result.stderr
    assert index.read_bytes() == before
    assert "rebuilt index" not in result.stderr


def test_backup_and_note_paths_are_private_and_relative(repo):
    repo.broken_branch()
    assert repo.run().returncode == 0
    backup = next((repo.state / "git-repairs").iterdir())
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    event = next((repo.state / "git-repair.pending").glob("*.json"))
    assert stat.S_IMODE(event.stat().st_mode) == 0o600
    assert str(repo.root) not in event.read_text()
    assert "could not finish" not in event.read_text()


def test_ref_lock_failure_stops_before_reflogs_and_recovers_next_tick(repo):
    _, _ = repo.broken_branch()
    log = repo.main / ".git/logs/refs/heads/ticket/example"
    before = log.read_bytes()
    lock = repo.main / ".git/refs/heads/ticket/example.lock"
    lock.touch()
    result = repo.run()
    assert result.returncode != 0
    assert log.read_bytes() == before
    assert "git update-ref failed" in repo.notes()[0]
    lock.unlink()
    repo.next_tick()
    assert repo.run().returncode == 0


def test_linked_object_directory_is_never_followed(repo):
    repo.broken_branch()
    fanout = next(repo.main / ".git/objects" / f"{i:02x}" for i in range(256)
                  if not (repo.main / ".git/objects" / f"{i:02x}").exists())
    target = repo.root / "linked-objects"
    target.mkdir()
    empty = target / ("a" * 38)
    empty.touch()
    fanout.symlink_to(target, target_is_directory=True)
    result = repo.run()
    assert result.returncode != 0
    assert empty.exists() and fanout.is_symlink()
    assert "symbolic link" in result.stderr


def test_fetch_timeout_is_bounded_and_does_not_repeat_in_same_tick(repo):
    # Inject at subprocess.run only for the repair fetch; all other Git is real.
    snippet = '''
import subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import merge_health
real = subprocess.run
calls = []
def run(args, **kwargs):
    if args[0] == "git" and "fetch" in args:
        calls.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
    return real(args, **kwargs)
merge_health.subprocess.run = run
main, state, marker = map(Path, sys.argv[2:])
assert not merge_health.repair_repository(main, state, marker)
assert not merge_health.repair_repository(main, state, marker)
assert calls == [120]
assert "timed out" in next((state / "git-repair.pending").glob("*.json")).read_text()
'''
    repo.broken_branch()
    result = subprocess.run([sys.executable, "-c", snippet, str(REPAIR.parent), str(repo.main),
                             str(repo.state), str(repo.marker)], env=repo.env, capture_output=True,
                            text=True, timeout=25)
    assert result.returncode == 0, result.stderr


def test_hidden_session_fetch_error_is_probed_then_retried(repo):
    repo.broken_branch()
    command = '''
import subprocess, sys
result = subprocess.run(["git", "fetch", "origin"], capture_output=True)
if result.returncode:
    print("cannot fetch from origin", file=sys.stderr)
else:
    print("example session started")
sys.exit(result.returncode)
'''
    result = repo.run(sys.executable, "-c", command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "example session started\n"
    assert "cannot fetch from origin" not in result.stderr
    assert len(repo.notes()) == 1


def test_success_status_with_real_object_error_still_triggers_repair(repo):
    repo.broken_branch()
    command = '''
import subprocess, sys
result = subprocess.run(["git", "fetch", "origin"], capture_output=True, text=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
print("main: unknown" if result.returncode else "main: green")
'''
    result = repo.run(sys.executable, "-c", command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "main: green\n"
    assert "bad object" not in result.stderr
    assert len(repo.notes()) == 1


def test_branch_shell_metacharacters_are_data_not_commands(repo):
    name = "ticket/$(touch${IFS}example-sentinel)"
    repo.broken_branch(name)
    result = repo.run()
    assert result.returncode == 0, result.stderr
    assert not (repo.main / "example-sentinel").exists()
    assert repo.git("rev-parse", name) == repo.base


def test_disappeared_registered_worktree_is_skipped(repo):
    import shutil
    absent = repo.worktree()
    shutil.rmtree(absent)
    repo.broken_branch()
    result = repo.run()
    assert result.returncode == 0, result.stderr
    assert not absent.exists()


def test_nonempty_corrupt_object_is_not_quarantined(repo):
    _, path = repo.broken_branch()
    path.write_bytes(b"example nonempty invalid object")
    before = path.read_bytes()
    result = repo.run()
    assert result.returncode != 0
    assert path.read_bytes() == before
    assert not list((repo.state / "git-repairs").glob("*/objects/??/*"))


def test_successful_retry_can_report_a_second_failure_without_repairing_twice(repo):
    repo.broken_branch()
    # Recovery succeeds; the retried user step still fails for an unrelated reason.
    command = '''
import subprocess, sys
result = subprocess.run(["git", "fetch", "origin"], capture_output=True, text=True)
if result.returncode:
    sys.stderr.write(result.stderr)
    sys.exit(1)
print("example check unavailable", file=sys.stderr)
sys.exit(7)
'''
    result = repo.run(sys.executable, "-c", command)
    assert result.returncode == 7
    assert "example check unavailable" in result.stderr
    assert len(repo.notes()) == 1


def test_repair_fetch_timeout_bounds_a_stalled_git_transport(repo, monkeypatch):
    # Real Git starts an upload-pack child for local transport, just as network
    # transports have helpers. Shrink the production timeout to keep the test fast.
    transport = repo.root / "stalled-upload-pack"
    transport.write_text("#!/bin/sh\nsleep 2\n")
    transport.chmod(0o700)
    repo.git("config", "remote.origin.uploadpack", str(transport))
    monkeypatch.syspath_prepend(str(REPAIR.parent))
    import merge_health
    real_run = subprocess.run
    def bounded_run(*args, **kwargs):
        assert kwargs["timeout"] == 120
        kwargs["timeout"] = 0.1
        kwargs["env"] = repo.env
        return real_run(*args, **kwargs)
    monkeypatch.setattr(merge_health.subprocess, "run", bounded_run)
    started = time.monotonic()
    with pytest.raises(merge_health.RepairFailed, match="timed out"):
        merge_health._repair_command(repo.main, "fetch", "origin")
    elapsed = time.monotonic() - started
    assert elapsed < 1, "repair timeout waited for a stalled Git transport child to exit"
