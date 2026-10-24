"""The tick keeps its own Shipline checkout current: at most every 10 minutes it fetches
Shipline's `main`, takes a commit whose CI run passed by a fast-forward of a clean checkout, and
moves back to the last good commit by itself when the new one fails 3 times in a row. A pin
stops it."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from workflow_harness import LAUNCHER, SCRIPTS, Workflow, install_shipline

_SLUG = "example-owner/shipline"
_URL = f"https://github.com/{_SLUG}.git"
_START = 1_790_000_000
_MOVED = 10

# Answers only the question the self-update asks: the CI runs of one commit of Shipline's main.
# `ci/<sha>` holds "<status> <conclusion>"; without it no run exists yet.
_FAKE_GH = """#!/bin/bash
echo "gh $*" >> "@STATE@/gh.log"
prefix="api repos/example-owner/shipline/actions/runs?head_sha="
case "$1 $2" in
  "$prefix"*) ;;
  *) [ -x "@STATE@/gh-harness" ] && exec "@STATE@/gh-harness" "$@"; exit 1 ;;
esac
[ ! -e "@STATE@/ci-unreadable" ] || exit 1
sha=${2#*head_sha=}
sha=${sha%%&*}
case "$2" in *"&event=push"*) ;; *) exit 1 ;; esac
if [ -f "@STATE@/ci/$sha" ]; then
  read -r status conclusion < "@STATE@/ci/$sha"
  jq -n --arg sha "$sha" --arg status "$status" --arg conclusion "$conclusion" \\
    '{total_count: 2, workflow_runs: [
      {id: 7, run_number: 7, path: ".github/workflows/other.yml", head_sha: $sha,
       event: "push", status: "completed", conclusion: "success",
       created_at: "2026-10-24T10:00:00Z"},
      {id: 8, run_number: 8, path: ".github/workflows/ci.yml", head_sha: $sha,
       event: "push", status: $status,
       conclusion: (if $conclusion == "-" then null else $conclusion end),
       created_at: "2026-10-24T10:00:00Z"}]}'
else
  echo '{"total_count": 0, "workflow_runs": []}'
fi
"""


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


class Shipline:
    """A Shipline checkout cloned from a fake remote, a working copy that lands new commits on
    that remote's main, the CI results GitHub would give, and a repository the tick works on."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / "state"
        (self.state / "ci").mkdir(parents=True)
        bin_folder = root / "bin"
        bin_folder.mkdir()
        gh = bin_folder / "gh"
        gh.write_text(_FAKE_GH.replace("@STATE@", str(self.state)))
        gh.chmod(0o755)
        (root / "home").mkdir()
        self.env = {
            "PATH": f"{bin_folder}:{os.environ['PATH']}",
            "HOME": str(root / "home"),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": "tester@example.com",
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": "tester@example.com",
            "WORKFLOW_NOW": str(_START),
        }
        self.now = _START
        # The repository the tick works on, apart from Shipline.
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, self.env, "init", "--quiet", "--initial-branch=main")
        self.env["SHIPLINE_REPO"] = str(self.repo)

        self.upstream = root / "upstream.git"
        _git(root, self.env, "init", "--quiet", "--bare", "--initial-branch=main", str(self.upstream))
        self.dev = root / "dev"
        self.dev.mkdir()
        _git(self.dev, self.env, "init", "--quiet", "--initial-branch=main")
        shutil.copytree(
            SCRIPTS, self.dev / "scripts" / "workflow", ignore=shutil.ignore_patterns("__pycache__")
        )
        (self.dev / "bin").mkdir()
        shutil.copy2(LAUNCHER, self.dev / "bin" / "shipline")
        (self.dev / ".gitignore").write_text("__pycache__/\n.local/\n")
        (self.dev / "VERSION").write_text("1\n")
        _git(self.dev, self.env, "add", "-A")
        _git(self.dev, self.env, "commit", "--quiet", "-m", "chore: shipline")
        _git(self.dev, self.env, "remote", "add", "origin", str(self.upstream))
        _git(self.dev, self.env, "push", "--quiet", "origin", "main")
        self.first = self.head_of(self.dev)

        self.home = root / "shipline"
        _git(root, self.env, "clone", "--quiet", str(self.upstream), str(self.home))
        # Named as GitHub names it; git fetches it from the fake remote.
        self.git("remote", "set-url", "origin", _URL)
        self.git("config", f"url.{self.upstream}.insteadOf", _URL)
        self.notes = self.home / ".local" / "shipline"

    def git(self, *args: str) -> str:
        return _git(self.home, self.env, *args)

    def head_of(self, folder: Path) -> str:
        return _git(folder, self.env, "rev-parse", "HEAD")

    @property
    def head(self) -> str:
        return self.head_of(self.home)

    def land(self, ci: str | None = "completed success") -> str:
        """A new commit on Shipline's main, with its CI run as given. Returns it."""
        version = self.dev / "VERSION"
        version.write_text(f"{int(version.read_text()) + 1}\n")
        _git(self.dev, self.env, "commit", "--quiet", "-am", "fix: next")
        _git(self.dev, self.env, "push", "--quiet", "origin", "main")
        sha = self.head_of(self.dev)
        if ci is not None:
            self.ci(sha, ci)
        return sha

    def ci(self, sha: str, result: str) -> None:
        (self.state / "ci" / sha).write_text(result + "\n")

    def later(self, minutes: int) -> None:
        self.now += minutes * 60
        self.env["WORKFLOW_NOW"] = str(self.now)

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", "-B", str(self.home / "scripts" / "workflow" / "self_update.py"), *args],
            cwd=self.repo,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def tick(self, code: int = 0, env: dict[str, str] | None = None) -> int:
        """One tick as the tick runs it: the start, then its end with its exit. Returns what
        the start exited with."""
        pid = str(os.getpid())
        started = self.run("start", pid, env=env)
        assert started.returncode in (0, _MOVED), started.stderr
        ended = self.run("end", pid, str(code), env=env)
        assert ended.returncode == 0, ended.stderr
        self.later(3)
        return started.returncode

    def good(self) -> None:
        """The checkout's commit runs the ticks that make it good."""
        for _ in range(5):
            self.tick()

    def notices(self) -> list[str]:
        folder = self.notes / "wake.pending"
        if not folder.is_dir():
            return []
        return [json.loads(path.read_text())["line"] for path in sorted(folder.glob("*.json"))]

    def versions(self) -> list[str]:
        path = self.notes / "versions"
        return path.read_text().split() if path.exists() else []

    def ci_reads(self) -> list[str]:
        log = self.state / "gh.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def shipline(tmp_path: Path) -> Shipline:
    return Shipline(tmp_path)


def test_main_moved_with_its_ci_passed_is_taken_by_a_fast_forward(shipline: Shipline) -> None:
    shipline.good()
    landed = shipline.land()
    shipline.later(10)

    assert shipline.tick() == _MOVED

    assert shipline.head == landed
    assert shipline.git("symbolic-ref", "--short", "HEAD") == "main"
    assert shipline.git("status", "--porcelain") == ""
    assert shipline.ci_reads() == [
        f"gh api repos/{_SLUG}/actions/runs?head_sha={landed}&event=push&per_page=100"
    ]
    # Not good yet: it runs its 5 ticks first.
    assert shipline.versions() == [shipline.first]


@pytest.mark.parametrize(
    "ci", ["completed failure", "in_progress -", "queued -", "completed cancelled", None]
)
def test_main_is_not_taken_while_its_ci_is_red_or_pending(shipline: Shipline, ci: str | None) -> None:
    shipline.good()
    shipline.land(ci)
    shipline.later(10)

    assert shipline.tick() == 0

    assert shipline.head == shipline.first


def test_main_is_not_taken_while_its_ci_cannot_be_read(shipline: Shipline) -> None:
    shipline.good()
    shipline.land()
    (shipline.state / "ci-unreadable").touch()
    shipline.later(10)

    assert shipline.tick() == 0

    assert shipline.head == shipline.first


def test_main_is_fetched_at_most_once_every_10_minutes(shipline: Shipline) -> None:
    shipline.tick()
    landed = shipline.land(ci="in_progress -")
    shipline.later(10)
    assert shipline.tick() == 0
    shipline.ci(landed, "completed success")

    # Ticks 3, 6 and 9 minutes after that fetch neither fetch nor read CI again.
    for _ in range(3):
        assert shipline.tick() == 0
    assert shipline.head == shipline.first
    assert len(shipline.ci_reads()) == 1

    assert shipline.tick() == _MOVED
    assert shipline.head == landed


def test_a_checkout_with_local_changes_is_left_alone_and_named_once(shipline: Shipline) -> None:
    shipline.good()
    shipline.land()
    (shipline.home / "VERSION").write_text("mine\n")

    for _ in range(4):
        shipline.later(10)
        assert shipline.tick() == 0

    assert shipline.head == shipline.first
    assert (shipline.home / "VERSION").read_text() == "mine\n"
    assert shipline.notices() == [
        "shipline not updated: its checkout has local changes"
    ]


def test_a_checkout_with_commits_of_its_own_is_left_alone(shipline: Shipline) -> None:
    shipline.good()
    shipline.land()
    shipline.git("commit", "--quiet", "--allow-empty", "-m", "chore: mine")
    own = shipline.head
    shipline.later(10)

    assert shipline.tick() == 0

    assert shipline.head == own
    assert shipline.notices() == [
        "shipline not updated: its checkout has local changes"
    ]


def test_a_version_is_good_after_5_ticks_without_failing(shipline: Shipline) -> None:
    shipline.good()
    landed = shipline.land()
    shipline.later(10)
    shipline.tick()
    for _ in range(3):
        shipline.tick()
    shipline.tick(code=1)
    for _ in range(4):
        shipline.tick()
    assert shipline.versions() == [shipline.first]

    shipline.tick()

    assert shipline.versions() == [landed, shipline.first]


def test_the_last_three_good_commits_are_kept(shipline: Shipline) -> None:
    shipline.good()
    landed = []
    for _ in range(3):
        landed.append(shipline.land())
        shipline.later(10)
        assert shipline.tick() == _MOVED
        shipline.good()

    assert shipline.versions() == [*reversed(landed)]


def test_three_failed_ticks_in_a_row_roll_back_and_two_do_not(shipline: Shipline) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    assert shipline.tick(code=1) == _MOVED
    shipline.tick(code=1)
    shipline.tick()
    shipline.tick(code=1)
    shipline.tick(code=1)

    # Two in a row: it stays.
    assert shipline.tick(code=1) == 0
    assert shipline.head == bad
    assert shipline.notices() == []

    # The third: the next tick starts on the last good commit.
    assert shipline.tick() == _MOVED
    assert shipline.head == shipline.first
    assert shipline.git("status", "--porcelain") == ""
    assert shipline.notices() == [
        f"shipline rolled back: {bad} -> {shipline.first}, the tick failed 3 times in a row "
        "(last: exit 1)"
    ]


def test_a_tick_that_ended_without_its_end_counts_as_failed(shipline: Shipline) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    for _ in range(3):
        gone = subprocess.Popen(["true"])
        gone.wait()
        started = shipline.run("start", str(gone.pid))
        assert started.returncode in (0, _MOVED), started.stderr
        shipline.later(3)
    assert shipline.head == bad

    assert shipline.tick() == _MOVED

    assert shipline.head == shipline.first
    assert shipline.notices() == [
        f"shipline rolled back: {bad} -> {shipline.first}, the tick failed 3 times in a row "
        "(last: it ended without its end)"
    ]


def test_a_skipped_tick_counts_neither_way(shipline: Shipline) -> None:
    shipline.good()
    shipline.land()
    shipline.later(10)
    pid = str(os.getpid())
    for _ in range(5):
        assert shipline.run("start", pid).returncode in (0, _MOVED)
        assert shipline.run("end", pid, "0", "skipped").returncode == 0

    assert len(shipline.versions()) == 1


_TRACEBACK = 'Traceback (most recent call last):\n  File "merge_gate.py"\nKeyError: \'head\'\n'


def _gate(shipline: Shipline, code: int, stderr: str) -> None:
    """One run of the merge gate by the tick's merge driver, as the driver reports it."""
    done = shipline.run("gate", str(code), stderr)
    assert done.returncode == 0, done.stderr


def test_three_crashed_merge_gate_runs_in_a_row_roll_back(shipline: Shipline) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    shipline.tick()
    _gate(shipline, 1, _TRACEBACK)
    _gate(shipline, 137, "")
    # A refusal and a wait are answers, not crashes; only a crash breaks the row.
    _gate(shipline, 1, "merge refused: check is not passing: CI\n")
    _gate(shipline, 3, "merge waiting: main moved during the freshness check\n")
    _gate(shipline, 2, "")
    _gate(shipline, 1, "merge refused: git push origin timed out after 600s\n")
    _gate(shipline, 0, "")
    _gate(shipline, 1, _TRACEBACK)
    _gate(shipline, 1, "merge refused: gh pr merge 7 timed out after 600s\n")
    # What the pull request's own checks printed rides on a refusal: still a refusal.
    _gate(shipline, 1, "merge refused: pre-train checks failed:\n" + _TRACEBACK)
    _gate(shipline, 1, "merge refused: pre-train checks failed:\ntest timed out after 30s\n")
    _gate(shipline, 1, _TRACEBACK)
    _gate(shipline, 1, "merge refused: gh pr merge 7 timed out after 600s\n")
    assert shipline.tick() == 0
    assert shipline.head == bad

    _gate(shipline, 1, _TRACEBACK)
    assert shipline.tick() == _MOVED

    assert shipline.head == shipline.first
    assert shipline.notices() == [
        f"shipline rolled back: {bad} -> {shipline.first}, the merge gate crashed 3 times in a "
        "row (last: KeyError: 'head')"
    ]


def test_a_timed_out_or_unknown_gate_exit_is_a_crash(shipline: Shipline) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    shipline.tick()
    _gate(shipline, 1, "merge refused: git push origin timed out after 600s\n")
    _gate(shipline, 4, "")
    _gate(shipline, 137, "")

    assert shipline.tick() == _MOVED

    assert shipline.head == shipline.first
    assert shipline.notices() == [
        f"shipline rolled back: {bad} -> {shipline.first}, the merge gate crashed 3 times in a "
        "row (last: exit 137)"
    ]


def test_a_rolled_back_commit_is_not_taken_again_until_main_moves_past_it(
    shipline: Shipline,
) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    for _ in range(3):
        shipline.tick(code=1)
    assert shipline.tick() == _MOVED
    assert shipline.head == shipline.first

    for _ in range(4):
        shipline.later(10)
        assert shipline.tick() == 0
    assert shipline.head == shipline.first
    assert all(bad not in read for read in shipline.ci_reads()[1:])

    fixed = shipline.land()
    shipline.later(10)
    assert shipline.tick() == _MOVED
    assert shipline.head == fixed


def test_a_good_version_is_not_rolled_back(shipline: Shipline) -> None:
    shipline.good()
    landed = shipline.land()
    shipline.later(10)
    shipline.tick()
    shipline.good()

    for _ in range(4):
        assert shipline.tick(code=1) == 0

    assert shipline.head == landed
    assert shipline.notices() == []


@pytest.mark.parametrize("by", ["variable", "file"])
def test_a_pin_stops_updates_and_is_named_once(shipline: Shipline, by: str) -> None:
    shipline.good()
    env = {}
    if by == "variable":
        env["SHIPLINE_PIN"] = shipline.first
        named = f"SHIPLINE_PIN ({shipline.first})"
    else:
        (shipline.notes / "pin").write_text(shipline.first + "\n")
        named = f".local/shipline/pin ({shipline.first})"
    shipline.land()

    for _ in range(3):
        shipline.later(10)
        assert shipline.tick(env=env) == 0

    assert shipline.head == shipline.first
    assert shipline.ci_reads() == []
    assert shipline.notices() == [f"shipline pinned by {named}: not updated"]


def test_a_pin_holds_a_failing_version(shipline: Shipline) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    shipline.tick()
    (shipline.notes / "pin").write_text("")

    for _ in range(4):
        assert shipline.tick(code=1) == 0

    assert shipline.head == bad


def test_a_checkout_without_a_remote_is_left_alone_and_holds_no_notes(shipline: Shipline) -> None:
    shipline.git("remote", "remove", "origin")

    assert shipline.tick() == 0

    assert not (shipline.home / ".local").exists()


def test_a_checkout_that_is_the_repository_itself_is_left_alone(shipline: Shipline) -> None:
    shipline.land()
    shipline.later(10)

    assert shipline.tick(env={"SHIPLINE_REPO": str(shipline.home)}) == 0

    assert shipline.head == shipline.first
    assert not (shipline.home / ".local").exists()


# Through the tick and the wake themselves.


def _harness_shipline(workflow: Workflow, upstream_ci: str) -> tuple[Path, str]:
    """The harness's own Shipline checkout, given a remote that has one commit more on main,
    and a `gh` that answers its CI read. Returns the checkout and that commit."""
    home = install_shipline(workflow)
    env = {k: v for k, v in workflow.env.items() if not k.startswith("GIT_DIR")}
    upstream = workflow.root / "shipline-upstream.git"
    _git(workflow.root, env, "init", "--quiet", "--bare", "--initial-branch=main", str(upstream))
    _git(home, env, "remote", "add", "origin", _URL)
    _git(home, env, "config", f"url.{upstream}.insteadOf", _URL)
    (home / ".gitignore").write_text("__pycache__/\n.local/\n")
    _git(home, env, "add", ".gitignore")
    _git(home, env, "commit", "--quiet", "-m", "chore: ignore the notes")
    _git(home, env, "push", "--quiet", "-u", "origin", "main")
    dev = workflow.root / "shipline-dev"
    _git(workflow.root, env, "clone", "--quiet", str(upstream), str(dev))
    # The new version's tick says so, to show the tick that runs is the new one.
    tick = dev / "scripts" / "workflow" / "tick"
    text = tick.read_text()
    marker = 'here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)\n'
    assert marker in text
    tick.write_text(text.replace(marker, marker + 'echo "the new tick runs"\n', 1))
    _git(dev, env, "commit", "--quiet", "-am", "fix: the new tick")
    _git(dev, env, "push", "--quiet", "origin", "main")
    landed = _git(dev, env, "rev-parse", "HEAD")
    # The harness's `gh` answers everything else.
    harness = workflow.state / "gh-harness"
    shutil.copy2(workflow.bin / "gh", harness)
    workflow.fake("gh", _FAKE_GH)
    (workflow.state / "ci").mkdir()
    (workflow.state / "ci" / landed).write_text(upstream_ci + "\n")
    return home, landed


def test_the_tick_moves_its_checkout_and_runs_the_new_version_at_once(workflow: Workflow) -> None:
    home, landed = _harness_shipline(workflow, "completed success")
    env = {"SHIPLINE_REPO": str(workflow.repo)}

    done = workflow.shipline("tick", env=env)

    assert done.returncode == 0, done.stderr
    assert _git(home, workflow.env, "rev-parse", "HEAD") == landed
    assert done.stdout.count("the new tick runs") == 1
    # The tick's run is counted on the new version, and it is the only one running.
    trial = json.loads((home / ".local" / "shipline" / "trial.json").read_text())
    assert trial["sha"] == landed
    assert trial["ticks"] == 1
    assert not any((home / ".local" / "shipline" / "runs").iterdir())


def test_the_tick_counts_its_own_failed_exit(workflow: Workflow) -> None:
    home, _ = _harness_shipline(workflow, "in_progress -")
    (workflow.state / "fail-gh-list").touch()
    env = {"SHIPLINE_REPO": str(workflow.repo)}

    done = workflow.shipline("tick", env=env)

    assert done.returncode == 1
    trial = json.loads((home / ".local" / "shipline" / "trial.json").read_text())
    assert trial["tick_failures"] == 1
    assert trial["ticks"] == 0


def test_a_tick_skipped_for_the_one_before_it_is_not_counted(workflow: Workflow) -> None:
    home, _ = _harness_shipline(workflow, "in_progress -")
    common = Path(workflow.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    env = {"SHIPLINE_REPO": str(workflow.repo)}
    with (common / "shipline-tick.lock").open("w") as lock:
        import fcntl

        fcntl.flock(lock, fcntl.LOCK_EX)
        done = workflow.shipline("tick", env=env)

    assert done.returncode == 0, done.stderr
    assert "the tick before this one is still running" in done.stdout
    trial = json.loads((home / ".local" / "shipline" / "trial.json").read_text())
    assert trial["ticks"] == trial["tick_failures"] == 0


def test_the_wake_tells_a_roll_back_once_at_any_hour(workflow: Workflow) -> None:
    home = install_shipline(workflow)
    workflow.orchestrator("busy")
    pending = home / ".local" / "shipline" / "wake.pending"
    pending.mkdir(parents=True)
    line = f"shipline rolled back: {'b' * 40} -> {'a' * 40}, the tick failed 3 times in a row"
    (pending / "1790000000-rolled-back.json").write_text(json.dumps({"line": line}))
    (workflow.state / "send-fails").touch()
    # Noon in New York, when the wake looks in only for what cannot wait.
    noon = {"WORKFLOW_NOW": "1791907200", "TZ": "America/New_York"}

    assert workflow.shipline("wake", env=noon).returncode == 1
    (workflow.state / "send-fails").unlink()
    told = workflow.shipline("wake", env={**noon, "WORKFLOW_NOW": "1791907500"})
    again = workflow.shipline("wake", env={**noon, "WORKFLOW_NOW": "1791907800"})

    assert told.returncode == again.returncode == 0, told.stderr
    assert f"\n{line}\n" in workflow.sent()[-1]
    assert len(workflow.sent()) == 2  # one failed attempt, then one delivered message
    assert not any(pending.iterdir())


def test_the_merge_driver_counts_a_crash_of_the_gate_it_ran(workflow: Workflow) -> None:
    home, _ = _harness_shipline(workflow, "in_progress -")
    env = {**workflow.env, "SHIPLINE_REPO": str(workflow.repo), "WORKFLOW_BACKGROUND_GITHUB": "1"}
    assert workflow.shipline("tick", env=env).returncode == 0
    workflow.add_worktree(7, draft=False)
    scripts = home / "scripts" / "workflow"
    (scripts / "merge-when-current").write_text(
        "#!/bin/bash\necho 'Traceback (most recent call last):' >&2\n"
        "echo \"KeyError: 'head'\" >&2\nexit 1\n"
    )
    # Only the gate run itself: what the driver does after it reads GitHub, not stood in here.
    drive = (
        "import sys\nfrom pathlib import Path\nimport merge_driver\n"
        "try:\n    merge_driver._drive_gate(Path(sys.argv[1]), '7')\n"
        "except merge_driver.gate.Waiting:\n    pass\n"
    )

    done = subprocess.run(
        ["python3", "-B", "-c", drive, str(workflow.repo / ".local" / "workflow")],
        cwd=scripts,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    trial = json.loads((home / ".local" / "shipline" / "trial.json").read_text())
    assert trial["gate_failures"] == 1
    assert trial["last_gate"] == "KeyError: 'head'"


def test_the_reason_of_a_roll_back_is_one_short_line(shipline: Shipline) -> None:
    shipline.good()
    shipline.land()
    shipline.later(10)
    shipline.tick()
    for _ in range(3):
        _gate(shipline, 1, "Traceback (most recent call last):\nValueError: " + "x\x1b" * 400)

    assert shipline.tick() == _MOVED

    [line] = shipline.notices()
    assert line.endswith("(last: ValueError: " + "x" * 185 + "...)")
    assert "\x1b" not in line


def test_a_tick_that_already_moved_does_not_move_again(shipline: Shipline) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    for _ in range(3):
        shipline.tick(code=1)
    pid = str(os.getpid())
    assert shipline.run("start", pid).returncode == _MOVED
    shipline.land()
    shipline.later(10)

    # The tick runs again as the version it moved to, which moves nothing in this tick.
    again = shipline.run("start", pid, "moved")

    assert again.returncode == 0, again.stderr
    assert shipline.head == shipline.first != bad
    assert (shipline.notes / "runs" / pid).read_text().split()[0] == shipline.first


def test_nothing_moves_while_another_tick_runs_from_the_checkout(shipline: Shipline) -> None:
    shipline.good()
    other = subprocess.Popen(["sleep", "60"])
    try:
        assert shipline.run("start", str(other.pid)).returncode == 0
        landed = shipline.land()
        shipline.later(10)
        assert shipline.tick() == 0
        assert shipline.head == shipline.first
    finally:
        other.kill()
        other.wait()
    assert shipline.run("end", str(other.pid), "0").returncode == 0
    shipline.later(10)

    assert shipline.tick() == _MOVED
    assert shipline.head == landed


def test_a_run_whose_pid_was_taken_by_another_process_counts_as_failed(
    shipline: Shipline,
) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    shipline.tick()
    # A tick ended without its end, and its pid now belongs to a process that is no tick.
    other = subprocess.Popen(["sleep", "60"])
    try:
        (shipline.notes / "runs" / str(other.pid)).write_text(f"{bad} 1\n")

        shipline.tick()

        # Its failure set the count back: only this tick's pass is counted since.
        trial = json.loads((shipline.notes / "trial.json").read_text())
        assert trial["ticks"] == 1
        assert trial["last_tick"] == "it ended without its end"
        assert not (shipline.notes / "runs" / str(other.pid)).exists()
    finally:
        other.kill()
        other.wait()


def test_an_update_before_any_version_is_good_goes_back_to_the_commit_it_came_from(
    shipline: Shipline,
) -> None:
    shipline.tick()
    bad = shipline.land()
    shipline.later(10)
    for _ in range(3):
        shipline.tick(code=1)

    assert shipline.tick() == _MOVED

    assert shipline.head == shipline.first
    assert shipline.notices()[0].startswith(f"shipline rolled back: {bad} -> {shipline.first}, ")


def test_a_checkout_inside_another_repository_is_left_alone(shipline: Shipline) -> None:
    # A Shipline copy that is no checkout of its own, in a repository that tracks a folder above.
    outer = shipline.root / "outer"
    outer.mkdir()
    _git(outer, shipline.env, "init", "--quiet", "--initial-branch=main")
    _git(outer, shipline.env, "remote", "add", "origin", _URL)
    copy = outer / "shipline"
    shutil.copytree(shipline.home, copy, ignore=shutil.ignore_patterns(".git"))
    _git(outer, shipline.env, "add", "-A")
    _git(outer, shipline.env, "commit", "--quiet", "-m", "chore: copy")

    done = subprocess.run(
        ["python3", "-B", str(copy / "scripts" / "workflow" / "self_update.py"), "start", "1"],
        cwd=shipline.repo,
        env=shipline.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert not (copy / ".local").exists()


def test_git_variables_of_the_caller_do_not_turn_it_on_another_repository(
    shipline: Shipline,
) -> None:
    shipline.good()
    landed = shipline.land()
    shipline.later(10)
    elsewhere = {"GIT_DIR": str(shipline.repo / ".git"), "GIT_WORK_TREE": str(shipline.repo)}

    assert shipline.tick(env=elsewhere) == _MOVED

    assert shipline.head == landed
    assert _git(shipline.repo, shipline.env, "log", "--oneline", "--all") == ""


def test_a_tick_stopped_by_a_signal_counts_as_failed(workflow: Workflow) -> None:
    home, _ = _harness_shipline(workflow, "in_progress -")
    # The tick is stopped while it waits for the sessions to be listed.
    workflow.fake("tmux", "#!/bin/bash\n/bin/sleep 30\n")
    env = {**workflow.env, "SHIPLINE_REPO": str(workflow.repo)}
    runs = home / ".local" / "shipline" / "runs"
    tick = subprocess.Popen(
        ["bash", str(home / "bin" / "shipline"), "tick"],
        cwd=workflow.repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (runs / str(tick.pid)).exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(0.5)
        tick.terminate()
        assert tick.wait(timeout=60) == 143
    finally:
        tick.kill()
        subprocess.run(["pkill", "-f", "/bin/sleep 30"], check=False)

    trial = json.loads((home / ".local" / "shipline" / "trial.json").read_text())
    assert trial["tick_failures"] == 1
    assert trial["last_tick"] == "exit 143"
