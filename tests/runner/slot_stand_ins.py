"""A machine of the runner pool, with stand-ins for what its scripts touch.

`systemctl`, `docker` and `gh` are small scripts placed first on the `PATH`; they write what they
were asked to a log, and `gh` answers GitHub's runner list from a file the test prepares. The
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
REPOSITORY = "example-owner/example-app"
# The label every ordinary pull request job asks for.
ORDINARY = f"{PREFIX}-home"
SHARED_JOB_MEMORY = "8g"


def runner_name(machine: str, kind: str, number: int = 1) -> str:
    """The name a machine's runner registers with GitHub by."""
    return f"{PREFIX}-{machine}-{kind}-{number}"


_LOGGING = """#!/bin/bash
printf '%s\\n' "${0##*/} $*" >> "@STATE@/calls.log"
"""
# Answers the runner list from runners.json, and fails as GitHub does when it is gone; a label
# added or removed is written down only.
_GH = (
    _LOGGING
    + """\
case "$*" in
  *"actions/runners/"*"/labels"*) exit 0 ;;
  *"actions/runners"*) cat "@STATE@/runners.json" 2>/dev/null || exit 1; exit 0 ;;
esac
exit 1
"""
)


@dataclass
class Machine:
    root: Path
    name: str = "machine-two"
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

    def settle(self, **settings: str | int) -> None:
        """Write the machine's local settings, one `key=value` line each."""
        self.local_settings.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"runner_prefix={PREFIX}", f"repository={REPOSITORY}", f"machine={self.name}"]
        lines += [f"{key}={value}" for key, value in settings.items()]
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
        return self.run("runner-container.sh", f"{PREFIX}-ordinary-1")

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
        "SHIPLINE_RUNNER_SETTINGS": str(machine.local_settings),
        "GH_TOKEN": "stand-in",
    }
    return machine
