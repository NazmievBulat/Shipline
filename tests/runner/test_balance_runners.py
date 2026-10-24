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

_ROOT = Path(__file__).resolve().parents[2]
# What install.sh copies beside the balancer: the pool's settings and the GitHub budget.
_INSTALLED = [
    _ROOT / "runner/balance_runners.py",
    _ROOT / "runner/settings.sh",
    _ROOT / "scripts/workflow/github_budget.py",
    _ROOT / "scripts/workflow/github_identity.py",
]
NOW = 1792828200
MINUTE = 60
# The pool's labels (settings.sh), shared by every machine.
ORDINARY = "example-app-home"
DOCKER = "example-app-home-docker"
STANDBY = "example-app-home-standby"
API = "repos/example-owner/example-app/actions"

# Answers the queued and running runs, each run's jobs and the runners from `github.json`, deletes
# a runner's registration unless it runs a job (or `busy-on-delete` names it, a job it took since
# the listing), and writes every call to the log.
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
path = next(argument for argument in arguments if argument.startswith("repos/"))


def answer(key, items):
    # With --jq the caller asked for one item a line, from every page.
    if "--jq" in arguments:
        for item in items:
            print(json.dumps(item))
    else:
        print(json.dumps({"total_count": len(items), key: items}))


api = "repos/example-owner/example-app/actions"
if arguments[:3] == ["api", "-X", "DELETE"]:
    found = re.fullmatch(api + r"/runners/(\\d+)", path)
    runner = next(runner for runner in github["runners"] if runner["id"] == int(found[1]))
    busy = state / "busy-on-delete"
    if runner["busy"] or (busy.exists() and busy.read_text().strip() == runner["name"]):
        print("gh: Runner is currently running a job (HTTP 422)", file=sys.stderr)
        sys.exit(1)
    github["runners"].remove(runner)
    (state / "github.json").write_text(json.dumps(github))
elif found := re.fullmatch(api + r"/runs/(\\d+)/jobs\\?per_page=100", path):
    answer("jobs", github["jobs"].get(found[1], []))
elif urlsplit(path).path == api + "/runs":
    status = parse_qs(urlsplit(path).query)["status"][0]
    # A run that moved from queued to running between the two reads is in both.
    answer("workflow_runs", [run for run in github["runs"] if run["status"] in (status, "both")])
elif path == api + "/runners?per_page=100":
    answer("runners", github["runners"])
else:
    print(f"gh: could not answer {arguments}", file=sys.stderr)
    sys.exit(1)
"""

# `fail-systemctl` makes every call fail, `fail-systemctl-<unit>` the calls for that unit, and
# `slow-systemctl` makes each take a second.
_FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "@STATE@/calls.log"
[ ! -e "@STATE@/slow-systemctl" ] || /bin/sleep 1
[ ! -e "@STATE@/fail-systemctl" ] && [ ! -e "@STATE@/fail-systemctl-${@: -1}" ]
"""


def _iso(moment: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Machine:
    """One machine of the pool: the balancer installed as install.sh installs it, with its
    pool-settings.sh, its readings, GitHub's answers and the balancer's runs."""

    def __init__(self, root: Path, programs: Path) -> None:
        self.root = root
        self.state = root / "state"
        self.state.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.programs = programs
        self.installed = self.home / ".local/share/example-app-runner"
        self.installed.mkdir(parents=True)
        for source in _INSTALLED:
            shutil.copy2(source, self.installed / source.name)
        self.balancer = self.installed / "balance_runners.py"
        self.settings_file = self.installed / "pool-settings.sh"
        self.proc = root / "proc"
        self.proc.mkdir()
        self.kvm = root / "kvm"
        self.kvm.touch()
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

    def settings(
        self, *, ordinary: int, docker: int | None, bounds: str = "", extra: str = ""
    ) -> None:
        """pool-settings.sh as install.sh writes it, with the balancer's lines."""
        self.settings_file.write_text(
            "machine=box\n"
            "roles='ordinary docker'\n"
            "other_machines='other'\n"
            f"ordinary_jobs={ordinary}\n"
            f"ordinary_blacksmith_jobs={ordinary}\n"
            + ("" if docker is None else f"docker_runners={docker}\n")
            + bounds
            + extra
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

    def registered(
        self,
        *,
        ordinary: int,
        docker: int,
        busy: tuple[str, ...] = (),
        unlabelled: tuple[str, ...] = (),
        lent: bool = False,
    ) -> None:
        """The runners GitHub lists: this machine's slots and its spare, its Docker runners, its
        main runner (`lent`: with the pool's label beside its own), the planner and another
        machine's two slots. A slot named in `unlabelled` waits for keep-spare.sh's label."""
        names = [
            (f"box-{k}", STANDBY if f"box-{k}" in unlabelled else ORDINARY)
            for k in range(1, ordinary + 1)
        ]
        names += [(f"box-{ordinary + 1}", STANDBY)]
        names += [(f"box-docker-{k}", DOCKER) for k in range(1, docker + 1)]
        names += [
            ("box-main", "example-app-home-main"),
            ("box-planner", "home-planner"),
            ("other-1", ORDINARY),
            ("other-2", ORDINARY),
        ]
        self.runners = [
            {
                "id": number,
                "name": name,
                "status": "online",
                "busy": name in busy,
                "labels": [{"name": "self-hosted"}, {"name": label}]
                + ([{"name": ORDINARY}] if lent and name == "box-main" else []),
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

    def _write_github(self) -> None:
        (self.state / "github.json").write_text(
            json.dumps({"runs": self.runs, "jobs": self.jobs, "runners": self.runners})
        )

    def _env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.programs}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "WORKFLOW_NOW": str(self.now),
            "WORKFLOW_LOADAVG_FILE": str(self.proc / "loadavg"),
            "WORKFLOW_PROC_ROOT": str(self.proc),
            "RUNNER_KVM_DEVICE": str(self.kvm),
        }

    def run(
        self, *, after_minutes: int = 0, code: int = 0, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.now += after_minutes * MINUTE
        self._write_github()
        done = subprocess.run(
            ["python3", "-B", str(self.balancer)],
            env={**self._env(), **(env or {})},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == code, done.stdout + done.stderr
        return done

    def start(self) -> subprocess.Popen[str]:
        """A run in the background, for two at once."""
        self._write_github()
        return subprocess.Popen(
            ["python3", "-B", str(self.balancer)],
            env=self._env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def calls(self, tool: str) -> list[str]:
        log = self.state / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith(tool)]

    def deleted(self) -> list[str]:
        """The runners whose registrations were deleted, by name."""
        listed = {str(runner["id"]): runner["name"] for runner in self.runners}
        return [
            listed[call.rsplit("/", 1)[1]]
            for call in self.calls("gh api -X DELETE")
            if call.rsplit("/", 1)[1] in listed
        ]

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
        "systemctl --user enable --now example-app-runner@5.service"
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
    machine.registered(ordinary=3, docker=1, busy=("box-docker-1",))

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


def test_a_runner_that_took_a_job_since_the_listing_is_not_stopped(machine: Machine) -> None:
    _held_by_load(machine)
    (machine.state / "busy-on-delete").write_text("box-docker-1\n")

    machine.run()

    assert machine.calls("systemctl") == []
    assert machine.count("docker_runners") == 1
    assert len(machine.calls("gh api -X DELETE")) == 1


def test_an_offline_runner_is_not_stopped(machine: Machine) -> None:
    _held_by_load(machine)
    for runner in machine.runners:
        if runner["name"] == "box-docker-1":
            runner["status"] = "offline"

    machine.run()

    assert machine.calls("systemctl") == []


def test_a_runner_that_does_not_stop_keeps_its_count(machine: Machine) -> None:
    _held_by_load(machine)
    (machine.state / "fail-systemctl").touch()

    machine.run(code=1)

    assert machine.count("docker_runners") == 1
    assert machine.changes() == []


def test_a_move_whose_new_runner_does_not_start_says_what_it_did(machine: Machine) -> None:
    machine.queue(ORDINARY, 8)
    machine.load(3.0)
    (machine.state / "fail-systemctl-example-app-runner@5.service").touch()

    machine.run(code=1)

    assert (machine.count("docker_runners"), machine.count("ordinary_jobs")) == (0, 3)
    assert machine.changes() == [
        "drained a Docker runner (1 to 0) to move it, and an ordinary slot did not start"
    ]
    # It was a change: the next one waits its 5 minutes.
    (machine.state / "fail-systemctl-example-app-runner@5.service").unlink()
    machine.forget_calls()
    machine.load(1.0)
    machine.run(after_minutes=1)
    assert machine.calls("systemctl") == []


def test_the_highest_slot_goes_and_main_and_the_planner_are_never_touched(
    machine: Machine,
) -> None:
    machine.settings(ordinary=3, docker=0, bounds="balance_ordinary=1-5\n")
    machine.registered(ordinary=3, docker=0)
    _held_by_load(machine)

    machine.run()

    # The highest is the spare beyond the three slots: keep-spare.sh moves the label off a slot.
    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner@4.service"
    ]
    assert machine.deleted() == ["box-4"]
    assert machine.count("ordinary_jobs") == 2
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
    assert machine.changes() == [
        "added an ordinary slot (3 to 4)",
        "added an ordinary slot (4 to 5)",
    ]


def test_moving_a_docker_runner_to_an_ordinary_slot_drains_it_first(machine: Machine) -> None:
    # Ordinary jobs wait, Docker has none, and the load leaves no room to add.
    machine.queue(ORDINARY, 8)
    machine.load(3.0)
    machine.registered(ordinary=3, docker=1, busy=("box-docker-1",))

    machine.run()

    assert machine.calls("systemctl") == []
    machine.registered(ordinary=3, docker=1)
    machine.run(after_minutes=1)

    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner-docker@1.service",
        "systemctl --user enable --now example-app-runner@5.service",
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


def test_a_forecast_starts_a_first_docker_runner_when_its_share_is_a_whole_job(
    machine: Machine,
) -> None:
    machine.settings(ordinary=3, docker=0, bounds="balance_ordinary=2-5\nbalance_docker=0-2\n")
    machine.registered(ordinary=3, docker=0)
    # No Docker runner in the pool: a first one would take both jobs on their way.
    machine.forecast(ordinary=0, docker=2)

    machine.run()

    assert machine.changes() == ["started a Docker runner (0 to 1)"]


def test_a_forecast_never_adds_beyond_the_bounds(machine: Machine) -> None:
    machine.settings(ordinary=5, docker=1, bounds="balance_ordinary=2-5\nbalance_docker=0-1\n")
    machine.registered(ordinary=5, docker=1)
    machine.forecast(ordinary=40, docker=9)

    machine.run()

    assert machine.calls("systemctl") == []


def test_a_run_while_another_holds_the_lock_leaves(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    lock = machine.home / ".local/state/workflow/runner-balance.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = machine.run()

    assert machine.calls("systemctl") == []
    assert "another balancer run" in done.stdout


def test_two_runs_at_once_change_once(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    (machine.state / "slow-systemctl").touch()

    first, second = machine.start(), machine.start()

    assert [first.wait(timeout=60), second.wait(timeout=60)] == [0, 0]
    assert machine.calls("systemctl") == [
        "systemctl --user enable --now example-app-runner@5.service"
    ]
    assert machine.count("ordinary_jobs") == 4


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
    [
        "RUNNER_PREFIX=../x",
        "RUNNER_PREFIX=Example-App",
        "RUNNER_PREFIX=example-app-",
        "machine=box/other",
        "machine='box name'",
        "machine=Box",
        "repository=../..",
    ],
)
def test_settings_that_would_leave_their_names_change_nothing(machine: Machine, line: str) -> None:
    machine.settings(ordinary=3, docker=1, bounds="balance_ordinary=2-5\n", extra=line + "\n")
    machine.queue(ORDINARY, 10)

    done = machine.run(code=1)

    assert machine.calls("systemctl") == [] and machine.calls("gh") == []
    assert "name no" in done.stderr


def test_a_state_file_that_is_not_the_balancers_is_started_over(machine: Machine) -> None:
    folder = machine.home / ".local/state/workflow"
    folder.mkdir(parents=True)
    (folder / "runner-balance.json").write_text('["not", "a", "state"]')
    machine.queue(ORDINARY, 10)

    machine.run()

    assert machine.changes() == ["added an ordinary slot (3 to 4)"]


def test_the_units_are_named_by_the_folder_the_pool_is_installed_in_or_the_environment(
    machine: Machine,
) -> None:
    machine.queue(ORDINARY, 10)

    machine.run(env={"RUNNER_PREFIX": "other-app"})

    assert machine.calls("systemctl") == [
        "systemctl --user enable --now other-app-runner@5.service"
    ]


def test_no_docker_runner_starts_without_kvm(machine: Machine) -> None:
    machine.settings(ordinary=3, docker=1, bounds="balance_ordinary=3-5\nbalance_docker=0-2\n")
    machine.queue(DOCKER, 10)
    machine.kvm.unlink()

    machine.run()

    assert machine.calls("systemctl") == []


def test_the_runners_never_take_more_places_than_the_firewalls_range(machine: Machine) -> None:
    # Three slots, the spare and one Docker runner take five places: there are five.
    machine.settings(
        ordinary=3,
        docker=1,
        bounds="balance_ordinary=2-5\nbalance_docker=1-2\n",
        extra="most_runners=5\n",
    )
    machine.queue(ORDINARY, 10)

    machine.run()

    assert machine.calls("systemctl") == []


def test_an_install_that_wrote_no_docker_count_keeps_its_docker_runners(machine: Machine) -> None:
    machine.settings(ordinary=3, docker=None, bounds="balance_ordinary=3-5\nbalance_docker=0-3\n")
    machine.queue(DOCKER, 10)
    _held_by_load(machine)

    machine.run()

    assert machine.calls("systemctl") == []
    assert "docker_runners" not in machine.settings_file.read_text()


def test_a_runners_registration_goes_before_its_unit(machine: Machine) -> None:
    _held_by_load(machine)

    machine.run()

    calls = [call for call in machine.calls("") if call.startswith(("gh api -X", "systemctl"))]
    assert calls == [
        f"gh api -X DELETE {API}/runners/{_id(machine, 'box-docker-1')}",
        "systemctl --user disable --now example-app-runner-docker@1.service",
    ]


def _id(machine: Machine, name: str) -> int:
    return next(runner["id"] for runner in machine.runners if runner["name"] == name)


def test_a_reinstall_keeps_the_balancers_own_lines(machine: Machine) -> None:
    machine.settings(
        ordinary=3,
        docker=1,
        bounds="balance_ordinary=2-5\nbalance_docker=0-2\n",
        extra="balance_forecast=/srv/example/ci-demand.json\n  balance off\nbalancer=x\n",
    )

    done = subprocess.run(
        ["python3", "-B", str(machine.balancer), "kept", str(machine.settings_file)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "balance_ordinary=2-5\nbalance_docker=0-2\n"
        "balance_forecast=/srv/example/ci-demand.json\nbalance off\n"
    )


def test_the_settings_may_name_the_pools_label(machine: Machine) -> None:
    machine.settings(
        ordinary=3,
        docker=1,
        bounds="balance_ordinary=2-5\nbalance_docker=0-2\n",
        extra="label=example-app-lab\ndocker_label=example-app-lab-docker\n",
    )
    machine.queue(ORDINARY, 10)
    machine.queue("example-app-lab-docker", 10)
    machine.memory(9)

    machine.run()

    # Only the lab's Docker jobs wait for this machine's pool.
    assert machine.changes() == ["started a Docker runner (1 to 2)"]


def test_every_pool_script_reads_a_pool_settings_that_says_balance_off(machine: Machine) -> None:
    machine.settings(ordinary=3, docker=1, extra="balance off\n")

    # As keep-spare.sh and keep-runner.sh read them: in `sh`, stopping at any failed command.
    done = subprocess.run(
        [
            "sh",
            "-eu",
            "-c",
            '. "$1/settings.sh"; . "$1/pool-settings.sh"; echo "$ordinary_jobs"',
            "sh",
            str(machine.installed),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (done.returncode, done.stdout) == (0, "3\n"), done.stderr


# keep-spare.sh's GitHub calls and its slot count, beside it as install.sh installs them: the
# listing comes from `listing.tsv`, a label granted joins it there, and each call goes to the log.
_SPARE_BUDGET = """import sys
from pathlib import Path

state = Path("@STATE@")
operation, *rest = sys.argv[1:]
if operation == "jitter":
    print(0)
elif operation == "call":
    arguments = rest[3:]
    with open(state / "calls.log", "a") as log:
        log.write("gh " + " ".join(arguments) + "\\n")
    listing = state / "listing.tsv"
    if "--paginate" in arguments:
        print(listing.read_text(), end="")
    elif arguments[:3] == ["api", "-X", "POST"]:
        number = arguments[3].split("/")[-2]
        rows = [line.split("\\t") for line in listing.read_text().splitlines()]
        for row in rows:
            if row[0] == number:
                row[4] += ",example-app-home,example-app-home-first"
        listing.write_text("".join("\\t".join(row) + "\\n" for row in rows))
"""

# The first sleep is the balancer adding a slot; the second ends the supervisor.
_SPARE_SLEEP = """#!/bin/bash
echo sleep >> "@STATE@/sleeps"
case $(wc -l < "@STATE@/sleeps") in
  1) sed -i 's/^ordinary_jobs=1$/ordinary_jobs=2/' "@INSTALLED@/pool-settings.sh" ;;
  *) kill -TERM "$PPID" ;;
esac
"""


def test_the_spare_supervisor_reads_the_slots_the_balancer_wrote(machine: Machine) -> None:
    for name in ("keep-spare.sh", "names.sh"):
        shutil.copy2(_ROOT / "runner" / name, machine.installed / name)
    state = str(machine.state)
    (machine.installed / "github_budget.py").write_text(_SPARE_BUDGET.replace("@STATE@", state))
    (machine.installed / "ordinary_slots.py").write_text("import sys\nprint(sys.argv[1])\n")
    machine.fake("sleep", _SPARE_SLEEP.replace("@INSTALLED@", str(machine.installed)))
    machine.settings(ordinary=1, docker=0)
    (machine.state / "listing.tsv").write_text(
        "".join(f"{k}\tbox-{k}\tonline\tfalse\tself-hosted,{STANDBY}\n" for k in (1, 2, 3))
    )

    done = subprocess.run(
        ["sh", str(machine.installed / "keep-spare.sh")],
        env=machine._env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 130, done.stderr
    granted = [call for call in machine.calls("gh api -X POST")]
    assert [call.split()[4] for call in granted] == [
        "repos/example-owner/example-app/actions/runners/1/labels",
        "repos/example-owner/example-app/actions/runners/2/labels",
    ]


# What install.sh calls that is not the balancer's: GitHub (through the budget), the images, Docker
# and the login manager, answered so that only the install's own steps run.
_INSTALL_BUDGET = """import sys

RUNNER = "runner"


class Limited(Exception):
    pass


def call(*arguments, **options):
    raise Limited()


if __name__ == "__main__":
    with open("@STATE@/calls.log", "a") as log:
        log.write("budget " + " ".join(sys.argv[1:]) + "\\n")
"""


def test_a_reinstall_keeps_the_bounds_and_writes_the_docker_count(machine: Machine) -> None:
    source = machine.root / "source"
    shutil.copytree(
        _ROOT / "runner",
        source,
        symlinks=False,
        ignore=shutil.ignore_patterns("github_budget.py", "github_identity.py", "__pycache__"),
    )
    (source / "github_budget.py").write_text(_INSTALL_BUDGET.replace("@STATE@", str(machine.state)))
    for name in ("Dockerfile", "vm.Dockerfile"):
        (source / name).write_text("FROM scratch\n")
    for name in ("install-github-budget.sh", "build-images.sh"):
        (source / name).write_text("#!/bin/sh\n")
        (source / name).chmod(0o755)
    (source / "machines").mkdir()
    (source / "machines/box.sh").write_text(
        "roles='ordinary docker'\nordinary_jobs=2\ndocker_runners=1\n"
    )
    (source / "machines/other.sh").write_text(
        "roles='ordinary'\nordinary_jobs=1\ndocker_runners=0\n"
    )
    machine.fake("loginctl", '#!/bin/bash\necho "loginctl $*" >> "@STATE@/calls.log"\n')
    # Docker is asked while the runners are down: a balancer run then would find them gone.
    lock = machine.home / ".local/state/workflow/runner-balance.lock"
    machine.fake(
        "docker",
        "#!/bin/bash\n"
        f'if flock -n "{lock}" true 2>/dev/null; then echo free; else echo held; fi'
        ' >> "@STATE@/balancer-lock"\n',
    )
    # The machine as the balancer left it: other counts, and its own lines.
    machine.settings(
        ordinary=4,
        docker=2,
        bounds="balance_ordinary=2-5\nbalance_docker=0-2\n",
        extra="balance off\n",
    )

    done = subprocess.run(
        ["sh", str(source / "install.sh")],
        env=machine._env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    settings = machine.settings_file.read_text()
    # The counts are the machine's from machines/box.sh again; the balancer's lines stay.
    assert "ordinary_jobs=2\n" in settings and "docker_runners=1\n" in settings
    # The units the balancer numbers after the slots.
    assert "ordinary_spares=1\n" in settings
    assert set((machine.state / "balancer-lock").read_text().split()) == {"held"}
    assert settings.endswith("balance_ordinary=2-5\nbalance_docker=0-2\nbalance off\n")
    units = machine.home / ".config/systemd/user"
    assert (units / "example-app-runner-balance.timer").is_file()
    assert (units / "example-app-runner-balance.service").is_file()
    assert "systemctl --user enable --now example-app-runner-balance.timer" in machine.calls(
        "systemctl"
    )


def test_no_slot_is_added_while_one_waits_for_its_label(machine: Machine) -> None:
    # keep-spare.sh gives the label to as many registrations as the slots: one has not got it
    # (the Blacksmith slot count, say), so another slot would not take a job either.
    machine.registered(ordinary=3, docker=1, unlabelled=("box-3",))
    machine.settings(ordinary=3, docker=1, bounds="balance_ordinary=2-5\nbalance_docker=1-2\n")
    machine.queue(ORDINARY, 10)

    machine.run()

    assert machine.calls("systemctl") == []


def test_a_unit_that_does_not_start_is_switched_off_again(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    (machine.state / "fail-systemctl-example-app-runner@5.service").touch()

    machine.run(code=1)

    assert machine.calls("systemctl") == [
        "systemctl --user enable --now example-app-runner@5.service",
        "systemctl --user disable --now example-app-runner@5.service",
    ]
    assert machine.count("ordinary_jobs") == 3


def test_the_merge_lines_first_jobs_queue_for_the_ordinary_slots(machine: Machine) -> None:
    machine.queue("example-app-home-first", 10)

    machine.run()

    assert machine.changes() == ["added an ordinary slot (3 to 4)"]


def test_a_lent_main_runner_is_no_slot_of_the_pool(machine: Machine) -> None:
    machine.registered(ordinary=3, docker=1, lent=True)
    machine.forecast(ordinary=10, docker=0)

    machine.run()

    assert "forecast ordinary 6.0 of 10" in machine.log_lines()[0]


def test_a_run_read_twice_counts_once(machine: Machine) -> None:
    machine.queue(ORDINARY, 10)
    machine.runs[0]["status"] = "both"

    machine.run()

    assert "queued ordinary 1 (oldest 10 min)" in machine.log_lines()[0]


def test_a_queue_not_watched_for_fifteen_minutes_is_watched_again(machine: Machine) -> None:
    machine.load(6.5)
    machine.run()
    machine.forget_calls()

    # The balancer did not run for 16 minutes (a reboot, a budget pause): nothing says the queue
    # stayed empty all that time.
    machine.run(after_minutes=16)

    assert machine.calls("systemctl") == []
