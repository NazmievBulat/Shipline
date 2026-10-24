"""A machine of the runner pool, with stand-ins for what its scripts touch.

`systemctl`, `docker` and `gh` are small scripts placed first on the `PATH`; they write what they
were asked to a log. `gh` keeps GitHub's runner list in a file the test prepares, and applies to
it the labels a script adds, sets or removes, so a test reads the labels a runner ends with. The
machine's local settings and its pool settings are files in the test's own directory.

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
# Every machine's runners carry the same labels, built from one base label (`runner.label`):
# ordinary pull request jobs ask for it, and a spare carries the standby one until it takes a
# slot. Machines differ only by their runners' names.
LABEL = f"{PREFIX}-home"
STANDBY = f"{LABEL}-standby"
MAIN = f"{LABEL}-main"
DOCKER = f"{LABEL}-docker"
# This machine, and the other machine of the pool.
MACHINE = "machine-two"
OTHER = "machine-one"
SHARED_JOB_MEMORY = "8g"
SPARE_UNIT = f"{PREFIX}-runner-spare.service"
ORDINARY_UNIT = f"{PREFIX}-runner@"


def runner_name(machine: str, kind: str = "", number: int = 1) -> str:
    """The name a machine's runner registers with GitHub by: `<machine>-1` for an ordinary one,
    `<machine>-<kind>-1` for the others."""
    return f"{machine}-{kind}-{number}" if kind else f"{machine}-{number}"


_LOGGING = """#!/bin/bash
printf '%s\\n' "${0##*/} $*" >> "@STATE@/calls.log"
"""
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
    elif word == "--input":
        source = rest.pop(0)
        given = json.load(sys.stdin if source == "-" else open(source))
        labels += given.get("labels", [])
    elif word in ("-q", "--jq"):
        jq = rest.pop(0)
    elif word == "--paginate":
        pass
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
    elif method == "PUT":
        names = ["self-hosted", *labels]
    elif method == "DELETE":
        names = [name for name in names if found[2] is None or name != found[2]]
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
    def local_settings(self) -> Path:
        return self.root / "home" / ".config" / "shipline" / "runner.sh"

    @property
    def pool_settings(self) -> Path:
        return self.root / "home" / ".config" / PREFIX / "pool.sh"

    def settle(self, slots: int, alone: bool = False, job_memory: str | None = None) -> None:
        """Write the machine's local settings: its ordinary pull request slots, whether it is
        the only machine of the pool, and the memory cap of one ordinary job."""
        self.local_settings.parent.mkdir(parents=True, exist_ok=True)
        others = "" if alone else OTHER
        lines = [f"machine={self.name}", f"other_machines=({others})", f"ordinary_jobs={slots}"]
        if job_memory is not None:
            lines.append(f"job_memory={job_memory}")
        self.local_settings.write_text("\n".join(lines) + "\n")

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

    def run(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(RUNNER / script), *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def install(self) -> subprocess.CompletedProcess[str]:
        return self.run("install.sh")

    def keep_spare(self) -> subprocess.CompletedProcess[str]:
        """One round of the spare's supervisor."""
        return self.run("keep-spare.sh", "--once")

    def ordinary_slots(self) -> int:
        """The ordinary slots this machine may hold now, as `ordinary_slots.py` counts them."""
        done = subprocess.run(
            ["python3", str(RUNNER / "ordinary_slots.py")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return int(done.stdout.strip())

    def start_job_container(self) -> subprocess.CompletedProcess[str]:
        return self.run("runner-container.sh", runner_name(self.name))

    def calls(self, program: str) -> list[str]:
        log = self.state / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.split(" ", 1)[0] == program]


def make_machine(root: Path) -> Machine:
    machine = Machine(root)
    machine.state.mkdir(parents=True)
    stand_ins = root / "bin"
    stand_ins.mkdir()
    for program, text in {"systemctl": _LOGGING, "docker": _LOGGING, "gh": _GH}.items():
        path = stand_ins / program
        path.write_text(text.replace("@STATE@", str(machine.state)))
        path.chmod(0o755)
    machine.runners()
    home = root / "home"
    home.mkdir()
    machine.env = {
        "PATH": f"{stand_ins}:{os.path.dirname(shutil.which('bash') or '/bin/bash')}:/usr/bin:/bin",
        "HOME": str(home),
        "RUNNER_PREFIX": PREFIX,
        "SHIPLINE_RUNNER_SETTINGS": str(machine.local_settings),
        "GH_TOKEN": "stand-in",
    }
    return machine
