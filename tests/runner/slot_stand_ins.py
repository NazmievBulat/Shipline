"""A machine of the runner pool, installed by `runner/install.sh` from a copy of `runner/`, with
stand-ins for what its scripts touch.

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
from dataclasses import dataclass, field
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
# GitHub's runner list and runner labels, kept in runners.json: missing, GitHub cannot be read.
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
found = re.search(r"/actions/runners(?:/([0-9]+)/labels(?:/([^/]+))?)?$", path)
if not found:
    sys.exit(1)
if found[1] is None:
    if method != "GET":
        sys.exit(1)
    answer = runners
else:
    (runner,) = [r for r in runners["runners"] if r["id"] == int(found[1])]
    names = [label["name"] for label in runner["labels"]]
    if method == "POST":
        names += [label for label in labels if label not in names]
    elif method == "DELETE":
        names = [name for name in names if name != found[2]]
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
    def source(self) -> Path:
        """The copy of `runner/` that is installed from."""
        return self.root / "source"

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
    ) -> None:
        """Write the machine's local settings: its ordinary pull request slots, whether it is
        the only machine of the pool, the memory cap of one ordinary job, and its spares."""
        machines = self.source / "machines"
        shutil.rmtree(machines, ignore_errors=True)
        machines.mkdir()
        if not alone:
            (machines / f"{OTHER}.sh").write_text(
                "roles='ordinary'\nordinary_jobs=2\ndocker_runners=0\n"
            )
        lines = ["roles='ordinary'", f"ordinary_jobs={slots}", "docker_runners=0"]
        if job_memory is not None:
            lines.append(f"job_memory={job_memory}")
        if spares is not None:
            lines.append(f"ordinary_spares={spares}")
        (machines / f"{self.name}.sh").write_text("\n".join(lines) + "\n")

    def runners(self, *listed: tuple[str, str, list[str]]) -> None:
        """What GitHub lists: (name, status, labels) of each runner."""
        runners = [
            {
                "id": number,
                "name": name,
                "os": "Linux",
                "status": status,
                "busy": False,
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

    def install(self) -> subprocess.CompletedProcess[str]:
        return self._run("sh", str(self.source / "install.sh"), "--machine", self.name)

    def installed_as(self, slots: int, **settings: str | int) -> None:
        """Install the machine with its settings, then forget what the install called."""
        self.settle(slots, **settings)  # type: ignore[arg-type]
        done = self.install()
        assert done.returncode == 0, done.stderr
        (self.state / "calls.log").unlink(missing_ok=True)

    def spare_round(self) -> subprocess.CompletedProcess[str]:
        """One round of the spare supervisor, up to its first pause (ROUND)."""
        return self._run("sh", str(self.installed / "keep-spare.sh"))

    def ordinary_slots(self) -> int:
        """The ordinary slots of this machine now, from the runner list the spare supervisor
        read: called as keep-spare.sh calls it."""
        listing = self.root / "listing.tsv"
        runners = json.loads((self.state / "runners.json").read_text())["runners"]
        listing.write_text(
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
        done = self._run(
            "sh",
            "-c",
            ". ./settings.sh && . ./pool-settings.sh && python3 ./ordinary_slots.py "
            '"$ordinary_jobs" "$ordinary_blacksmith_jobs" "$repository" '
            '"$other_machines" "$label" "$1"',
            "ordinary-slots",
            str(listing),
            cwd=self.installed,
        )
        assert done.returncode == 0, done.stderr
        return int(done.stdout.strip())

    def start_job_container(self) -> subprocess.CompletedProcess[str]:
        """Start an ordinary job's container as keep-runner.sh starts it."""
        return self._run(
            "sh",
            "-c",
            ". ./settings.sh && . ./pool-settings.sh && . ./runner-container.sh && "
            f"start_container ordinary {PREFIX}-runner-1 {PREFIX}-runner-1 </dev/null",
            cwd=self.installed,
        )

    def calls(self, program: str) -> list[str]:
        log = self.state / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.split(" ", 1)[0] == program]


def make_machine(root: Path) -> Machine:
    machine = Machine(root)
    machine.state.mkdir(parents=True)
    shutil.copytree(RUNNER, machine.source)
    # The images are built by a stand-in docker; install.sh copies their Dockerfiles.
    (machine.source / "Dockerfile").touch()
    (machine.source / "vm.Dockerfile").touch()
    stand_ins = root / "bin"
    stand_ins.mkdir()
    for program, text in {**_STAND_INS, "gh": _GH}.items():
        path = stand_ins / program
        path.write_text(text.replace("@STATE@", str(machine.state)))
        path.chmod(0o755)
    machine.runners()
    home = root / "home"
    home.mkdir()
    real = {os.path.dirname(shutil.which(tool) or "/usr/bin/x") for tool in ("python3", "jq")}
    machine.env = {
        "PATH": ":".join([str(stand_ins), *sorted(real), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "RUNNER_PREFIX": PREFIX,
        "RUNNER_KVM_DEVICE": str(root / "no-kvm"),
        "WORKFLOW_GITHUB_RAW_GH": str(stand_ins / "gh"),
    }
    return machine
