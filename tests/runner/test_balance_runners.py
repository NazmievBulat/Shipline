"""Each machine moves its runners between CI and Docker, and adds or removes them, by the queues,
its load and the demand on its way (`runner/balance_runners.py`)."""

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

BALANCER = Path(__file__).resolve().parents[2] / "runner" / "balance_runners.py"
NOW = 1792828200
MINUTE = 60
ORDINARY = "example-app-box"
DOCKER = "example-app-box-docker"

# Answers the queued and running runs, each run's jobs and the runners from `github.json`, and
# writes every call to the log.
_FAKE_GH = """#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

state = Path("@STATE@")
arguments = sys.argv[1:]
with open(state / "calls.log", "a") as log:
    log.write("gh " + " ".join(arguments) + "\\n")
if (state / "fail-gh").exists():
    sys.exit(1)
github = json.loads((state / "github.json").read_text())
path = arguments[-1]
api = "repos/example-org/example-app/actions"
if found := re.fullmatch(api + r"/runs/(\\d+)/jobs\\?per_page=100", path):
    print(json.dumps({"jobs": github["jobs"].get(found[1], [])}))
elif urlsplit(path).path == api + "/runs":
    status = parse_qs(urlsplit(path).query)["status"][0]
    runs = [run for run in github["runs"] if run["status"] == status]
    print(json.dumps({"total_count": len(runs), "workflow_runs": runs}))
elif path == api + "/runners?per_page=100":
    print(json.dumps({"total_count": len(github["runners"]), "runners": github["runners"]}))
else:
    print(f"gh: could not answer {arguments}", file=sys.stderr)
    sys.exit(1)
"""

_FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "@STATE@/calls.log"
[ ! -e "@STATE@/fail-systemctl" ]
"""


def _iso(moment: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Machine:
    """One machine of the pool: its settings, readings, GitHub's answers and the balancer's runs."""

    def __init__(self, root: Path, programs: Path) -> None:
        self.root = root
        self.state = root / "state"
        self.state.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.programs = programs
        self.settings_file = root / "runner.sh"
        self.proc = root / "proc"
        self.proc.mkdir()
        self.now = NOW
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[str, list[dict[str, Any]]] = {}
        self.runners: list[dict[str, Any]] = []
        self.fake("gh", _FAKE_GH)
        self.fake("systemctl", _FAKE_SYSTEMCTL)
        # Four processors: a session start waits above a load of 6, so there is room up to 2.
        self.fake("nproc", "#!/bin/bash\necho 4\n")
        self.settings(ordinary=3, docker=1, bounds="balance_ordinary=2-5\nbalance_docker=0-2\n")
        self.load(1.0)
        self.memory(20)
        self.registered(ordinary=3, docker=1)

    def fake(self, tool: str, text: str) -> None:
        path = self.programs / tool
        path.write_text(text.replace("@STATE@", str(self.state)))
        path.chmod(0o755)

    def settings(self, *, ordinary: int, docker: int, bounds: str = "", extra: str = "") -> None:
        self.settings_file.write_text(
            "# The settings of this machine.\n"
            "repository=example-org/example-app\n"
            "RUNNER_PREFIX=example-app\n"
            "machine=box\n"
            'roles="ordinary docker"\n'
            f"ordinary_jobs={ordinary}\n"
            f"docker_runners={docker}\n" + bounds + extra
        )
        self.settings_file.chmod(0o640)

    def count(self, name: str) -> int:
        found = [
            line.split("=", 1)[1]
            for line in self.settings_file.read_text().splitlines()
            if line.startswith(name + "=")
        ]
        assert len(found) == 1, self.settings_file.read_text()
        return int(found[0])

    def load(self, five_minutes: float) -> None:
        (self.proc / "loadavg").write_text(f"0.50 {five_minutes:.2f} 0.30 1/100 1234\n")

    def memory(self, available_gb: float) -> None:
        kb = int(available_gb * 1024 * 1024)
        (self.proc / "meminfo").write_text(
            f"MemTotal:       65536000 kB\nMemFree:         1000000 kB\nMemAvailable:   {kb} kB\n"
        )

    def registered(self, *, ordinary: int, docker: int, busy: tuple[str, ...] = ()) -> None:
        """The runners GitHub lists: this machine's, its main runner, the planner and another
        machine's two ordinary slots."""
        names = [(f"example-app-box-{k}", ORDINARY) for k in range(1, ordinary + 1)]
        names += [(f"example-app-box-docker-{k}", DOCKER) for k in range(1, docker + 1)]
        names += [
            ("example-app-box-main-1", "example-app-box-main"),
            ("example-app-planner-1", "example-app-planner"),
            ("example-app-other-1", "example-app-other"),
            ("example-app-other-2", "example-app-other"),
        ]
        self.runners = [
            {
                "id": number,
                "name": name,
                "status": "online",
                "busy": name in busy,
                "labels": [{"name": "self-hosted"}, {"name": label}],
            }
            for number, (name, label) in enumerate(names, start=1)
        ]

    def queue(self, label: str, *ages_in_minutes: int, running: int = 0) -> None:
        """Jobs for `label` queued this many minutes ago, and some running, each in a run."""
        for age in ages_in_minutes:
            self._job(label, "queued", self.now - age * MINUTE)
        for _ in range(running):
            self._job(label, "in_progress", self.now - 600)

    def _job(self, label: str, status: str, created: int) -> None:
        run = 1000 + len(self.runs)
        run_status = "queued" if status == "queued" and run % 2 else "in_progress"
        self.runs.append({"id": run, "status": run_status, "created_at": _iso(created)})
        self.jobs[str(run)] = [
            {"status": status, "labels": ["self-hosted", label], "created_at": _iso(created)},
            {"status": "completed", "labels": ["self-hosted", label], "created_at": _iso(created)},
        ]

    def clear_queue(self) -> None:
        self.runs, self.jobs = [], {}

    def forecast(self, ordinary: int, docker: int, *, age: int = 60) -> None:
        path = self.home / ".local/state/workflow/ci-demand.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "at": self.now - age,
                    "within_minutes": 30,
                    "ordinary": ordinary,
                    "docker": docker,
                    "pulls": {"cloud": [], "busy": [], "ready": [], "prefixes": []},
                }
            )
        )

    def run(self, *, after_minutes: int = 0, code: int = 0) -> subprocess.CompletedProcess[str]:
        self.now += after_minutes * MINUTE
        (self.state / "github.json").write_text(
            json.dumps({"runs": self.runs, "jobs": self.jobs, "runners": self.runners})
        )
        done = subprocess.run(
            ["python3", "-B", str(BALANCER)],
            env={
                "PATH": f"{self.programs}:{os.environ['PATH']}",
                "HOME": str(self.home),
                "LANG": "C.UTF-8",
                "SHIPLINE_RUNNER_SETTINGS": str(self.settings_file),
                "WORKFLOW_NOW": str(self.now),
                "WORKFLOW_LOADAVG_FILE": str(self.proc / "loadavg"),
                "WORKFLOW_PROC_ROOT": str(self.proc),
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == code, done.stdout + done.stderr
        return done

    def calls(self, tool: str) -> list[str]:
        log = self.state / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith(tool)]

    def changes(self) -> list[str]:
        """What changed, from the log: the words after the time, before the first `;`."""
        log = self.home / ".local/state/workflow/runner-balance.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line.split(" ", 1)[1].split(";")[0] for line in lines]

    def log_lines(self) -> list[str]:
        return (self.home / ".local/state/workflow/runner-balance.log").read_text().splitlines()

    def forget_calls(self) -> None:
        (self.state / "calls.log").unlink(missing_ok=True)


@pytest.fixture
def machine(tmp_path: Path) -> Iterator[Machine]:
    # A folder that allows running programs: CI gives pytest one in `/dev/shm`, which does not.
    programs = Path(tempfile.mkdtemp(prefix="balance-programs-"))
    try:
        yield Machine(tmp_path, programs)
    finally:
        shutil.rmtree(programs, ignore_errors=True)


def test_ordinary_jobs_queued_for_five_minutes_add_a_slot_when_there_is_room(
    machine: Machine,
) -> None:
    machine.queue(ORDINARY, 6, 2, running=3)

    machine.run()

    assert machine.calls("systemctl") == [
        "systemctl --user enable --now example-app-runner@4.service"
    ]
    assert machine.count("ordinary_jobs") == 4
    assert machine.count("docker_runners") == 1
    [line] = machine.log_lines()
    assert line == (
        f"{_iso(NOW)} added an ordinary slot (3 to 4); queued ordinary 2 (oldest 6 min), "
        "docker 0; running ordinary 3, docker 0; forecast none; load 1.00 of 6; memory 20.0 GB"
    )
    # The settings keep their other lines and their mode.
    assert "machine=box\n" in machine.settings_file.read_text()
    assert machine.settings_file.stat().st_mode & 0o777 == 0o640


def test_a_queue_younger_than_five_minutes_adds_nothing(machine: Machine) -> None:
    machine.queue(ORDINARY, 4, 1)

    machine.run()

    assert machine.calls("systemctl") == []
    assert machine.changes() == []


@pytest.mark.parametrize(
    ("load", "memory", "added"),
    [(2.0, 3.0, True), (2.1, 20, False), (1.0, 2.9, False)],
    ids=["room", "load-not-four-under-the-ceiling", "too-little-memory"],
)
def test_an_ordinary_slot_needs_load_four_under_the_ceiling_and_three_gigabytes(
    machine: Machine, load: float, memory: float, added: bool
) -> None:
    # The Docker runner is at its minimum: nothing can move from it.
    machine.settings(ordinary=3, docker=1, bounds="balance_ordinary=2-5\nbalance_docker=1-2\n")
    machine.queue(ORDINARY, 10)
    machine.load(load)
    machine.memory(memory)

    machine.run()

    assert machine.changes() == (["added an ordinary slot (3 to 4)"] if added else [])


@pytest.mark.parametrize(("memory", "added"), [(8.0, True), (7.9, False)])
def test_a_docker_runner_needs_eight_gigabytes(
    machine: Machine, memory: float, added: bool
) -> None:
    machine.settings(ordinary=3, docker=1, bounds="balance_ordinary=3-5\nbalance_docker=0-2\n")
    machine.queue(DOCKER, 7)
    machine.memory(memory)

    machine.run()

    if added:
        assert machine.calls("systemctl") == [
            "systemctl --user enable --now example-app-runner-docker@2.service"
        ]
        assert machine.count("docker_runners") == 2
    else:
        assert machine.calls("systemctl") == []


def _held_by_load(machine: Machine) -> None:
    """Docker had nothing queued for 15 minutes, and the load holds every session start."""
    machine.load(6.5)
    machine.run()
    machine.forget_calls()
    machine.now += 15 * MINUTE


def test_an_idle_kind_is_removed_when_the_load_holds_session_starts(machine: Machine) -> None:
    _held_by_load(machine)

    machine.run()

    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner-docker@1.service"
    ]
    assert machine.count("docker_runners") == 0
    assert machine.changes() == ["stopped a Docker runner (1 to 0)"]


def test_nothing_queued_for_less_than_fifteen_minutes_keeps_the_runner(machine: Machine) -> None:
    machine.load(6.5)
    machine.run()

    machine.run(after_minutes=14)

    assert machine.calls("systemctl") == []


def test_a_busy_runner_is_never_stopped(machine: Machine) -> None:
    _held_by_load(machine)
    machine.registered(ordinary=3, docker=1, busy=("example-app-box-docker-1",))

    machine.run()

    assert machine.calls("systemctl") == []
    assert machine.count("docker_runners") == 1

    machine.registered(ordinary=3, docker=1)
    machine.run(after_minutes=1)

    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner-docker@1.service"
    ]


def test_a_runner_github_does_not_list_is_not_stopped(machine: Machine) -> None:
    _held_by_load(machine)
    machine.registered(ordinary=3, docker=0)

    machine.run()

    assert machine.calls("systemctl") == []


def test_the_highest_slot_goes_and_main_and_the_planner_are_never_touched(
    machine: Machine,
) -> None:
    machine.settings(ordinary=3, docker=0, bounds="balance_ordinary=1-5\n")
    machine.registered(ordinary=3, docker=0)
    _held_by_load(machine)

    machine.run()

    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner@3.service"
    ]
    assert machine.changes() == ["removed an ordinary slot (3 to 2)"]


def test_the_bounds_hold(machine: Machine) -> None:
    machine.settings(ordinary=5, docker=0, bounds="balance_ordinary=2-5\nbalance_docker=0-2\n")
    machine.registered(ordinary=5, docker=0)
    machine.queue(ORDINARY, 30)

    machine.run()

    assert machine.calls("systemctl") == []
    machine.settings(ordinary=2, docker=0, bounds="balance_ordinary=2-5\nbalance_docker=0-2\n")
    machine.registered(ordinary=2, docker=0)
    machine.clear_queue()
    _held_by_load(machine)
    machine.run()
    assert machine.calls("systemctl") == []


def test_no_bounds_keep_the_installed_counts(machine: Machine) -> None:
    machine.settings(ordinary=3, docker=1)
    machine.queue(ORDINARY, 30)

    machine.run()

    assert machine.calls("systemctl") == []
    machine.clear_queue()
    _held_by_load(machine)
    machine.run()
    assert machine.calls("systemctl") == []


def test_one_change_every_five_minutes(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    machine.run()
    machine.registered(ordinary=4, docker=1)

    machine.run(after_minutes=4)

    assert machine.count("ordinary_jobs") == 4
    machine.run(after_minutes=1)
    assert machine.count("ordinary_jobs") == 5
    assert machine.changes() == ["added an ordinary slot (3 to 4)", "added an ordinary slot (4 to 5)"]


def test_moving_a_docker_runner_to_an_ordinary_slot_drains_it_first(machine: Machine) -> None:
    # Ordinary jobs wait, Docker has none, and the load leaves no room to add.
    machine.queue(ORDINARY, 8)
    machine.load(3.0)
    machine.registered(ordinary=3, docker=1, busy=("example-app-box-docker-1",))

    machine.run()

    assert machine.calls("systemctl") == []
    machine.registered(ordinary=3, docker=1)
    machine.run(after_minutes=1)

    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner-docker@1.service",
        "systemctl --user enable --now example-app-runner@4.service",
    ]
    assert (machine.count("docker_runners"), machine.count("ordinary_jobs")) == (0, 4)
    assert machine.changes() == [
        "moved a Docker runner to an ordinary slot (Docker 1 to 0, ordinary 3 to 4)"
    ]


def test_a_move_needs_room_in_memory_for_what_it_adds(machine: Machine) -> None:
    # An ordinary slot frees 3 GB: with 4 more there is still no room for a Docker VM.
    machine.queue(DOCKER, 8)
    machine.load(3.0)
    machine.memory(4)

    machine.run()

    assert machine.calls("systemctl") == []


def test_balance_off_changes_nothing(machine: Machine) -> None:
    machine.settings(
        ordinary=3,
        docker=1,
        bounds="balance_ordinary=2-5\nbalance_docker=0-2\n",
        extra="balance off\n",
    )
    machine.queue(ORDINARY, 30)

    done = machine.run()

    assert machine.calls("systemctl") == []
    assert machine.count("ordinary_jobs") == 3
    assert "balance off" in done.stdout


def test_a_forecast_of_a_wave_holds_a_removal_the_empty_queue_would_allow(
    machine: Machine,
) -> None:
    _held_by_load(machine)
    # One Docker job is on its way, and this machine has the pool's only Docker runner; 5
    # ordinary jobs, 3 of them this machine's share, would queue on 2 of its slots.
    machine.forecast(ordinary=5, docker=1)

    machine.run()

    assert machine.calls("systemctl") == []
    assert (machine.count("docker_runners"), machine.count("ordinary_jobs")) == (1, 3)


def test_a_stale_forecast_is_ignored(machine: Machine) -> None:
    _held_by_load(machine)
    machine.forecast(ordinary=5, docker=1, age=11 * MINUTE)

    machine.run()

    assert machine.count("docker_runners") == 0


def test_a_forecast_brings_a_slot_back_before_the_wave(machine: Machine) -> None:
    # 10 ordinary jobs on their way; this machine has 3 of the pool's 5 ordinary slots: 6 are its.
    machine.forecast(ordinary=10, docker=0)

    machine.run()

    assert machine.changes() == ["added an ordinary slot (3 to 4)"]
    assert "forecast ordinary 6.0 of 10, docker 0.0 of 0" in machine.log_lines()[0]


def test_a_forecast_never_adds_beyond_the_bounds(machine: Machine) -> None:
    machine.settings(ordinary=5, docker=1, bounds="balance_ordinary=2-5\nbalance_docker=0-1\n")
    machine.registered(ordinary=5, docker=1)
    machine.forecast(ordinary=40, docker=9)

    machine.run()

    assert machine.calls("systemctl") == []


def test_two_runs_at_once_change_once(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    lock = machine.home / ".local/state/workflow/runner-balance.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = machine.run()

    assert machine.calls("systemctl") == []
    assert "another balancer run" in done.stdout


def test_a_failed_github_read_changes_nothing(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    (machine.state / "fail-gh").touch()

    machine.run(code=1)

    assert machine.calls("systemctl") == []
    assert machine.count("ordinary_jobs") == 3


def test_a_unit_that_does_not_start_keeps_the_count(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    (machine.state / "fail-systemctl").touch()

    machine.run(code=1)

    assert machine.count("ordinary_jobs") == 3
    assert machine.changes() == []


def test_a_kind_the_machine_has_no_role_for_is_left_alone(machine: Machine) -> None:
    machine.settings(
        ordinary=3,
        docker=0,
        bounds="balance_ordinary=2-5\nbalance_docker=0-2\n",
        extra='roles="ordinary"\n',
    )
    machine.queue(DOCKER, 30)

    machine.run()

    assert machine.calls("systemctl") == []


def test_the_log_and_state_are_for_the_owner_alone(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)

    machine.run()

    folder = machine.home / ".local/state/workflow"
    for name in ("runner-balance.log", "runner-balance.json"):
        assert (folder / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "line",
    ["RUNNER_PREFIX=../x", "machine=box/other", "machine='box name'", "repository=../.."],
)
def test_settings_that_would_leave_their_names_change_nothing(machine: Machine, line: str) -> None:
    machine.settings(ordinary=3, docker=1, bounds="balance_ordinary=2-5\n", extra=line + "\n")
    machine.queue(ORDINARY, 10)

    done = machine.run(code=1)

    assert machine.calls("systemctl") == [] and machine.calls("gh") == []
    assert "names no" in done.stderr
