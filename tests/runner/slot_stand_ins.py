"""A machine of the runner pool, installed by `runner/install.sh` from a repository's main checkout,
with stand-ins for what its scripts touch.

`systemctl`, `docker`, `loginctl`, `sleep` and `gh` are small scripts placed first on the `PATH`;
they write what they were asked to a log. `gh` keeps GitHub's runner list in a file the test
prepares, and applies to it the labels a script adds or removes, so a test reads the labels a
runner ends with. `sleep` ends a script at its first pause, so a supervisor runs one round.

Every name the pool's scripts are known by (their settings, files and arguments) is in this file
alone, so the tests read as what a machine does.
"""

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[2] / "runner"
PREFIX = "example-app"
REPOSITORY = "example-owner/example-app"
# Every machine's runners carry the same labels (settings.sh): ordinary pull request jobs ask for
# the base label, the train's first member for the first label, and an ordinary runner carries
# the standby label until the spare supervisor gives it a slot. Machines differ only by their
# runners' names.
LABEL = f"{PREFIX}-home"
STANDBY = f"{LABEL}-standby"
FIRST = f"{LABEL}-first"
MAIN = f"{LABEL}-main"
DOCKER = f"{LABEL}-docker"
# The machine that gives its pull request slots, and the other machine of the pool.
MACHINE = "home"
OTHER = "office"
# The memory cap of one ordinary job that settings.sh gives every machine.
SHARED_JOB_MEMORY = "10g"
# How `sleep` ends a script: it ran one round, up to its first pause.
ROUND = 75


def runner_name(machine: str, kind: str = "", number: int = 1) -> str:
    """The name a machine's runner registers with GitHub by: `<machine>-1` for an ordinary one,
    `<machine>-main` and `<machine>-docker-1` for main's and Docker ones (names.sh)."""
    if kind == "main":
        return f"{machine}-main"
    return f"{machine}-{kind}-{number}" if kind else f"{machine}-{number}"


def ordinary_unit(number: int) -> str:
    return f"{PREFIX}-runner@{number}.service"


SPARE_UNIT = f"{PREFIX}-runner-spare.service"

_LOGGING = """#!/bin/bash
printf '%s\\n' "${0##*/} $*" >> "@STATE@/calls.log"
"""
_STAND_INS = {
    "systemctl": _LOGGING,
    "docker": _LOGGING,
    "loginctl": _LOGGING + "echo yes\n",
    "sleep": _LOGGING + f"exit {ROUND}\n",
}
# GitHub's runner list, runner labels and registrations, kept in runners.json: missing, GitHub
# cannot be read.
_GH = """#!/usr/bin/env python3
import json, re, subprocess, sys

state = "@STATE@"
with open(f"{state}/calls.log", "a") as log:
    log.write(" ".join(["gh", *sys.argv[1:]]) + "\\n")
arguments = sys.argv[1:]
if arguments[:1] != ["api"]:
    sys.exit(1)
method, path, labels, jq, rest = "GET", None, [], None, arguments[1:]
while rest:
    word = rest.pop(0)
    if word in ("-X", "--method"):
        method = rest.pop(0).upper()
    elif word in ("-f", "-F", "--field", "--raw-field"):
        key, _, value = rest.pop(0).partition("=")
        if key in ("labels[]", "labels"):
            labels.append(value)
    elif word in ("-q", "--jq"):
        jq = rest.pop(0)
    elif word in ("-H", "--header", "--hostname"):
        rest.pop(0)
    elif not word.startswith("-"):
        path = word
try:
    with open(f"{state}/runners.json") as listed:
        runners = json.load(listed)
except OSError:
    sys.exit(1)
path = (path or "").split("?", 1)[0]
# The rights github_identity.py checks a new identity by, which this GitHub grants.
if re.search(r"/(actions/runs|actions/variables|pulls)$", path) and method == "GET":
    print("{}")
    sys.exit(0)
if path.endswith("/actions/runners/registration-token") and method == "POST":
    print(json.dumps({"token": "stand-in-registration"}))
    sys.exit(0)
found = re.search(r"/actions/runners(?:/([0-9]+)(/labels(?:/([^/]+))?)?)?$", path)
if not found:
    sys.exit(1)
if found[1] is None:
    if method != "GET":
        sys.exit(1)
    answer = runners
elif found[2] is None:
    # A registration removed, as install.sh removes those of retired runners.
    if method != "DELETE":
        sys.exit(1)
    runners["runners"] = [r for r in runners["runners"] if r["id"] != int(found[1])]
    with open(f"{state}/runners.json", "w") as kept:
        json.dump(runners, kept)
    sys.exit(0)
else:
    (runner,) = [r for r in runners["runners"] if r["id"] == int(found[1])]
    names = [label["name"] for label in runner["labels"]]
    if method == "POST":
        names += [label for label in labels if label not in names]
    elif method == "DELETE":
        names = [name for name in names if name != found[3]]
    else:
        sys.exit(1)
    runner["labels"] = [{"name": name} for name in names]
    with open(f"{state}/runners.json", "w") as kept:
        json.dump(runners, kept)
    answer = {"total_count": len(names), "labels": runner["labels"]}
text = json.dumps(answer)
if jq is not None:
    text = subprocess.run(["jq", "-r", jq], input=text, capture_output=True, text=True).stdout
print(text, end="" if jq is not None else "\\n")
"""


@dataclass
class Machine:
    root: Path
    name: str = MACHINE
    env: dict[str, str] = field(default_factory=dict)

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def checkout(self) -> Path:
        """The repository's main checkout, which install.sh runs in."""
        return self.root / "checkout"

    @property
    def installed(self) -> Path:
        return self.root / "home" / ".local" / "share" / f"{PREFIX}-runner"

    @property
    def pool_settings(self) -> Path:
        return self.installed / "pool-settings.sh"

    def settle(
        self,
        slots: int,
        alone: bool = False,
        job_memory: str | None = None,
        spares: int | None = None,
        extra: str = "",
    ) -> None:
        """Write the machine's local settings: its ordinary pull request slots, whether it is
        the only machine of the pool, the memory cap of one ordinary job, its spares, and any
        other line."""
        settings = self.root / "home" / ".config" / "shipline" / "runner.sh"
        settings.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"machine={self.name}",
            f"other_machines={'' if alone else (MACHINE if self.name == OTHER else OTHER)}",
            "roles='ordinary'",
            f"ordinary_jobs={slots}",
        ]
        if job_memory is not None:
            lines.append(f"job_memory={job_memory}")
        if spares is not None:
            lines.append(f"ordinary_spares={spares}")
        if extra:
            lines.append(extra)
        settings.write_text("\n".join(lines) + "\n")

    def runners(self, *listed: tuple[str, str, list[str]]) -> None:
        """What GitHub lists: (name, status, labels) of each runner. A runner that is "busy" is
        online and running a job."""
        runners = [
            {
                "id": number,
                "name": name,
                "os": "Linux",
                "status": "online" if status == "busy" else status,
                "busy": status == "busy",
                "labels": [{"name": label} for label in ["self-hosted", *labels]],
            }
            for number, (name, status, labels) in enumerate(listed, start=1)
        ]
        (self.state / "runners.json").write_text(
            json.dumps({"total_count": len(runners), "runners": runners})
        )

    def labels_of(self, name: str) -> list[str]:
        """The labels GitHub has for a runner now."""
        listed = json.loads((self.state / "runners.json").read_text())["runners"]
        (runner,) = [runner for runner in listed if runner["name"] == name]
        return [label["name"] for label in runner["labels"]]

    def measure(self, available_gib: float, pressure: float = 5.0, days: int = 7) -> None:
        """A usage log of the last `days` whole days (UTC), a line every hour, as the usage timer
        writes it: this much memory available and this CPU pressure throughout."""
        folder = self.root / "home" / ".local" / "state" / "ci-runner"
        folder.mkdir(parents=True, exist_ok=True)
        # Up to this hour, and a day more than asked: the report looks back from when it runs,
        # which may be past midnight UTC by then.
        this_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        lines = [
            f"{(this_hour - timedelta(hours=hour)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f" free_mb=1024 available_mb={int(available_gib * 1024)} swap_in_mb_s=0.0"
            f" swap_out_mb_s=0.0 cpu_pressure_60s={pressure} load_1m=1.0 busy=none"
            for hour in range((days + 1) * 24, -1, -1)
        ]
        (folder / "usage.log").write_text("".join(f"{line}\n" for line in lines))

    def usage(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """usage.py of the installed copy, as runner/README.md says to run it."""
        return self._run("python3", str(self.installed / "usage.py"), *arguments)

    def lose_github(self) -> None:
        """GitHub's runner list cannot be read."""
        (self.state / "runners.json").unlink()

    def _run(self, *command: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            env=self.env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def install(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._run("sh", str(RUNNER / "install.sh"), *arguments, cwd=self.checkout)

    @property
    def identity(self) -> Path:
        """The runners' own GitHub identity (github_identity.py)."""
        return self.root / "home" / ".config" / "shipline-runner"

    def installation(self, identity: bool = True) -> dict[str, bytes]:
        """Every file the install put in place: the installed copy, the units and, unless told
        otherwise, the runners' GitHub identity."""
        home = self.root / "home"
        folders = [self.installed, home / ".config/systemd/user"]
        if identity:
            folders.append(self.identity)
        return {
            str(path): path.read_bytes()
            for folder in folders
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        }

    def installed_as(self, slots: int, **settings: str | int) -> None:
        """Install the machine with its settings, then forget what the install called."""
        self.settle(slots, **settings)  # type: ignore[arg-type]
        done = self.install()
        assert done.returncode == 0, done.stderr
        (self.state / "calls.log").unlink(missing_ok=True)

    def spare_round(self) -> subprocess.CompletedProcess[str]:
        """One round of the spare supervisor, up to its first pause (ROUND)."""
        return self._run("sh", str(self.installed / "keep-spare.sh"))

    def _listing(self, path: Path, age: float = 0) -> None:
        """Write GitHub's runner list as the spare supervisor saves it, `age` seconds old."""
        runners = json.loads((self.state / "runners.json").read_text())["runners"]
        path.write_text(
            "".join(
                "\t".join(
                    [
                        str(runner["id"]),
                        runner["name"],
                        runner["status"],
                        str(runner["busy"]).lower(),
                        ",".join(label["name"] for label in runner["labels"]),
                    ]
                )
                + "\n"
                for runner in runners
            )
        )
        then = time.time() - age
        os.utime(path, (then, then))

    def ordinary_slots(self, age: float = 0) -> int:
        """The ordinary slots of this machine now, from the runner list the spare supervisor
        read `age` seconds ago: called as keep-spare.sh calls it."""
        listing = self.root / "listing.tsv"
        self._listing(listing, age)
        done = self._run(
            "sh",
            "-c",
            ". ./settings.sh && . ./pool-settings.sh && . ./names.sh && python3 ./ordinary_slots.py "
            '"$ordinary_jobs" "$ordinary_blacksmith_jobs" "$repository" "$label" '
            '"$other_machines" "$1"',
            "ordinary-slots",
            str(listing),
            cwd=self.installed,
        )
        assert done.returncode == 0, done.stderr
        return int(done.stdout.strip())

    def planner_slots(self) -> int:
        """The ordinary slots of this machine the planner's snapshot counts (planner-state.py),
        from the runner list the spare supervisor saved beside it."""
        self._listing(self.installed / "runners.tsv")
        done = self._run(
            "sh",
            "-c",
            ". ./settings.sh && . ./pool-settings.sh && . ./names.sh && python3 -c '"
            "import importlib.util, sys\n"
            'spec = importlib.util.spec_from_file_location("planner_state", "planner-state.py")\n'
            "state = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(state)\n"
            "print(state._slots(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4],"
            " sys.argv[5].split()))' "
            '"$ordinary_jobs" "$ordinary_blacksmith_jobs" "$repository" "$label" "$other_machines"',
            cwd=self.installed,
        )
        assert done.returncode == 0, done.stderr
        return int(done.stdout.strip())

    def start_job_container(self, kind: str = "ordinary") -> subprocess.CompletedProcess[str]:
        """Start a job's container as keep-runner.sh starts it: an ordinary one, or main's."""
        return self._run(
            "sh",
            "-c",
            ". ./settings.sh && . ./pool-settings.sh && . ./names.sh && . ./runner-container.sh && "
            f"start_container {kind} {PREFIX}-runner-1 {PREFIX}-runner-1 </dev/null",
            cwd=self.installed,
        )

    def calls(self, program: str) -> list[str]:
        log = self.state / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.split(" ", 1)[0] == program]


def _git(machine: Machine, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=machine.checkout, env=machine.env, capture_output=True, check=True
    )


def make_machine(root: Path) -> Machine:
    machine = Machine(root)
    machine.state.mkdir(parents=True)
    stand_ins = root / "bin"
    stand_ins.mkdir()
    for program, text in {**_STAND_INS, "gh": _GH}.items():
        path = stand_ins / program
        path.write_text(text.replace("@STATE@", str(machine.state)))
        path.chmod(0o755)
    machine.runners()
    home = root / "home"
    home.mkdir()
    real = {
        os.path.dirname(shutil.which(tool) or "/usr/bin/x") for tool in ("python3", "jq", "git")
    }
    machine.env = {
        "PATH": ":".join([str(stand_ins), *sorted(real), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "RUNNER_KVM_DEVICE": str(root / "no-kvm"),
        "WORKFLOW_GITHUB_RAW_GH": str(stand_ins / "gh"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    # The repository's shipline.json on main, as last fetched: it names the pool.
    machine.checkout.mkdir()
    _git(machine, "init", "--quiet", "--initial-branch=main")
    config = {"repository": REPOSITORY, "runner": {"prefix": PREFIX, "image_build": "true"}}
    (machine.checkout / "shipline.json").write_text(json.dumps(config))
    _git(machine, "add", "--all")
    _git(machine, "commit", "--quiet", "-m", "main")
    _git(machine, "update-ref", "refs/remotes/origin/main", "HEAD")
    return machine
