"""Moves this machine's runners between CI and Docker, and adds or removes them, by the queues,
its load and the demand on its way.

A systemd user timer runs it every minute, beside the spare supervisor (`systemd/`). It changes
only this machine's own ordinary slots (`<prefix>-runner@<k>.service`) and Docker runner VMs
(`<prefix>-runner-docker@<k>.service`): never another machine's, never a main runner or the
planner, and only within the bounds of the machine's local settings:

    balance_ordinary=MIN-MAX
    balance_docker=MIN-MAX

A kind without bounds, or one the machine has no role for (`roles`), keeps its installed count
(`ordinary_jobs`, `docker_runners`) as both bounds: nothing changes there until its owner widens
them. A line `balance off` stops the balancer, and the counts stay as they are.

Each minute it reads, from GitHub under the runners' own identity and within the shared budget
(`github_budget.py`), the queued and running jobs for this machine's ordinary label
(`<prefix>-<machine>`) and Docker label (`<prefix>-<machine>-docker`) and the runners of the
pool; and from the machine, its 5-minute load against the session start ceiling of
`scripts/workflow/session` (the processors times WORKFLOW_LOAD_PER_PROCESSOR, 1.5) and its
available memory.

- It adds an ordinary slot when ordinary jobs have queued for 5 minutes, or a Docker runner when
  Docker jobs have, when there is room: load at least 4 under the ceiling, and at least 3 GB
  available for an ordinary slot or 8 GB for a Docker runner.
- It removes one of a kind when that kind had nothing queued for 15 minutes and the load holds
  session starts (above the ceiling). It moves one to the other kind when the other kind has
  queued for 5 minutes, this kind has nothing queued and there is no room to add: it drains
  one, then adds one of the other in the same step, when the memory the drained one frees is
  enough for the other. A runner goes only once GitHub lists it idle, never mid-job; the
  highest numbered one goes, and the step waits while it is busy.
- At most one change every 5 minutes.
- The forecast the tick writes (`~/.local/state/workflow/ci-demand.json`, or the path of
  `balance_forecast`) says how many ordinary and Docker jobs come within 30 minutes. This
  machine's share of a kind is that times its slots over all the pool's online slots of that
  kind on GitHub. A removal waits while the share would queue on one slot fewer; a slot is
  added before the wave when the share would queue on the slots there are and there is room,
  never beyond the bounds. A forecast older than 10 minutes is ignored, and the queues alone
  decide.

Every change is one line of `~/.local/state/workflow/runner-balance.log`: the time, what
changed, the queues, the forecast, the load and the memory. Its state (since when a kind's
queue was empty, the last change) is in `runner-balance.json` beside it.

The settings are a Bash file, `~/.config/shipline/runner.sh` unless SHIPLINE_RUNNER_SETTINGS
names another; a change of count is written back to its line there.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# The runner install keeps github_budget.py beside this file; the repository keeps it here.
if not (HERE / "github_budget.py").exists():
    sys.path.append(str(HERE.parent / "scripts" / "workflow"))
from github_budget import RUNNER, Limited, call  # noqa: E402

KINDS = ("docker", "ordinary")
# What the settings call each kind's installed count, and the systemd unit of its k-th runner.
COUNT_NAMES = {"ordinary": "ordinary_jobs", "docker": "docker_runners"}
WORDS = {"ordinary": "an ordinary slot", "docker": "a Docker runner"}
NAMES = {"ordinary": "ordinary", "docker": "Docker"}
QUEUED_FOR_SECONDS = 5 * 60
EMPTY_FOR_SECONDS = 15 * 60
CHANGE_EVERY_SECONDS = 5 * 60
FORECAST_FRESH_SECONDS = 10 * 60
# Room to add: the load this far under the ceiling, and this much memory for the kind added.
LOAD_MARGIN = 4
MEMORY_GB = {"ordinary": 3, "docker": 8}
# What a repository, the prefix and a machine's name may be: they go into GitHub's paths, the
# labels and the unit names. `.` and `..` are no owner or name.
REPOSITORY = re.compile(r"(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SETTINGS_SECONDS = 5
GITHUB_SECONDS = 30
PER_PAGE = 100

STATE = Path.home() / ".local/state/workflow"
LOG = STATE / "runner-balance.log"
STATE_FILE = STATE / "runner-balance.json"
LOCK = STATE / "runner-balance.lock"
DEFAULT_FORECAST = STATE / "ci-demand.json"
SETTINGS_READ = (
    "repository",
    "RUNNER_PREFIX",
    "machine",
    "roles",
    "ordinary_jobs",
    "docker_runners",
    "balance_ordinary",
    "balance_docker",
    "balance_forecast",
    "balance_off",
)


class Refused(Exception):
    """Something the balancer needs cannot be read or done: nothing more changes this minute."""


class PartlyChanged(Refused):
    """A move drained its runner and the other kind's did not start."""


def now() -> int:
    return int(os.environ.get("WORKFLOW_NOW") or time.time())


def settings_path() -> Path:
    given = os.environ.get("SHIPLINE_RUNNER_SETTINGS")
    return Path(given) if given else Path.home() / ".config/shipline/runner.sh"


def read_settings(path: Path) -> dict[str, str]:
    """The values the balancer reads, as Bash reads them from the file, `balance off` among
    them, with only PATH and HOME, for at most SETTINGS_SECONDS."""
    script = (
        'balance() { [[ ${1-} == off ]] && balance_off=1; return 0; }; '
        'source "$1" >/dev/null 2>&1; shift; '
        'for name in "$@"; do printf "%s=%s\\0" "$name" "${!name-}"; done'
    )
    try:
        done = subprocess.run(
            ["bash", "-c", script, "settings", str(path), *SETTINGS_READ],
            env={key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ},
            capture_output=True,
            text=True,
            timeout=SETTINGS_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refused(f"{path} ran for more than {SETTINGS_SECONDS} seconds") from None
    if done.returncode or not path.is_file():
        raise Refused(f"{path} cannot be read")
    return dict(item.split("=", 1) for item in done.stdout.split("\0") if item)


def _count(values: dict[str, str], kind: str) -> int:
    text = values.get(COUNT_NAMES[kind], "") or "0"
    if not text.isdigit():
        raise Refused(f"{COUNT_NAMES[kind]} is '{text}', not a count")
    return int(text)


def _bounds(values: dict[str, str], kind: str, count: int, roles: set[str]) -> tuple[int, int]:
    text = values.get(f"balance_{kind}", "")
    found = re.fullmatch(r"\s*([0-9]+)\s*-\s*([0-9]+)\s*", text)
    if kind not in roles or not found or int(found[1]) > int(found[2]):
        return count, count
    return int(found[1]), int(found[2])


@dataclass
class Machine:
    repository: str
    prefix: str
    name: str
    counts: dict[str, int]
    bounds: dict[str, tuple[int, int]]
    off: bool
    forecast_path: Path

    def label(self, kind: str) -> str:
        base = f"{self.prefix}-{self.name}"
        return base + "-docker" if kind == "docker" else base

    def unit(self, kind: str, k: int) -> str:
        return f"{self.prefix}-runner{'-docker' if kind == 'docker' else ''}@{k}.service"

    def runner_name(self, kind: str, k: int) -> str:
        return f"{self.label(kind)}-{k}"


def machine(path: Path) -> Machine:
    values = read_settings(path)
    for needed, pattern in (
        ("repository", REPOSITORY),
        ("RUNNER_PREFIX", NAME),
        ("machine", NAME),
    ):
        if not pattern.fullmatch(values.get(needed, "")):
            raise Refused(f"{path} names no {needed}")
    roles = set(values.get("roles", "").split())
    counts = {kind: _count(values, kind) for kind in KINDS}
    return Machine(
        repository=values["repository"],
        prefix=values["RUNNER_PREFIX"],
        name=values["machine"],
        counts=counts,
        bounds={kind: _bounds(values, kind, counts[kind], roles) for kind in KINDS},
        off=values.get("balance_off") == "1",
        forecast_path=Path(values.get("balance_forecast") or DEFAULT_FORECAST).expanduser(),
    )


# What the machine reads.


def load_and_ceiling() -> tuple[float | None, float | None]:
    """The 5-minute load and the session start ceiling, each None when it cannot be read."""
    try:
        per = float(os.environ.get("WORKFLOW_LOAD_PER_PROCESSOR") or 1.5)
        processors = int(subprocess.run(["nproc"], capture_output=True, text=True).stdout)
        ceiling = processors * per if processors > 0 and per > 0 else None
    except OSError, ValueError:
        ceiling = None
    try:
        path = os.environ.get("WORKFLOW_LOADAVG_FILE") or "/proc/loadavg"
        load = float(Path(path).read_text().split()[1])
    except OSError, ValueError, IndexError:
        load = None
    return load, ceiling


def available_gb() -> float | None:
    proc = Path(os.environ.get("WORKFLOW_PROC_ROOT") or "/proc")
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except OSError, ValueError, IndexError:
        pass
    return None


def read_forecast(path: Path, moment: int) -> dict[str, float] | None:
    """The jobs on their way, or None when there is no forecast younger than 10 minutes."""
    try:
        value = json.loads(path.read_text())
        at, ordinary, docker = value["at"], value["ordinary"], value["docker"]
    except OSError, ValueError, KeyError, TypeError:
        return None
    if not all(isinstance(number, (int, float)) for number in (at, ordinary, docker)):
        return None
    if moment - at > FORECAST_FRESH_SECONDS:
        return None
    return {"ordinary": float(ordinary), "docker": float(docker)}


# What GitHub says.


def _api(repository: str, path: str) -> Any:
    done = call(
        "balancer", "api", f"repos/{repository}/{path}", timeout=GITHUB_SECONDS, identity=RUNNER
    )
    if done.returncode:
        raise Refused(f"GitHub did not answer {path}: {done.stderr.strip()}")
    return json.loads(done.stdout)


def _seconds(text: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Queue:
    queued: int = 0
    running: int = 0
    # When the oldest queued job was queued.
    oldest: float | None = None


def queues(box: Machine) -> dict[str, Queue]:
    """The queued and running jobs of this machine's two labels."""
    found = {kind: Queue() for kind in KINDS}
    labels = {box.label(kind): kind for kind in KINDS}
    for status in ("queued", "in_progress"):
        runs = _api(box.repository, f"actions/runs?status={status}&per_page={PER_PAGE}")
        for run in runs["workflow_runs"]:
            jobs = _api(box.repository, f"actions/runs/{int(run['id'])}/jobs?per_page={PER_PAGE}")
            for job in jobs["jobs"]:
                kind = next((labels[label] for label in job.get("labels") or [] if label in labels), None)
                if kind is None:
                    continue
                queue = found[kind]
                if job.get("status") == "queued":
                    queue.queued += 1
                    at = _seconds(job.get("created_at") or job.get("started_at"))
                    if at is not None and (queue.oldest is None or at < queue.oldest):
                        queue.oldest = at
                elif job.get("status") == "in_progress":
                    queue.running += 1
    return found


def runners(box: Machine) -> list[dict[str, Any]]:
    return _api(box.repository, f"actions/runners?per_page={PER_PAGE}")["runners"]


def pool_slots(box: Machine, listed: list[dict[str, Any]]) -> dict[str, int]:
    """The pool's online runners of each kind, by their labels: `<prefix>-<machine>-docker` a
    Docker runner, `<prefix>-<machine>` an ordinary slot, a main runner and the planner
    neither."""
    found = dict.fromkeys(KINDS, 0)
    for runner in listed:
        if runner.get("status") != "online":
            continue
        for label in (label.get("name", "") for label in runner.get("labels") or []):
            if not label.startswith(box.prefix + "-") or label.endswith(("-main", "-planner")):
                continue
            found["docker" if label.endswith("-docker") else "ordinary"] += 1
            break
    return found


def idle(listed: list[dict[str, Any]], name: str) -> bool:
    """GitHub lists the runner and it runs no job."""
    return any(runner.get("name") == name and runner.get("busy") is False for runner in listed)


# The decision.


@dataclass
class View:
    moment: int
    counts: dict[str, int]
    bounds: dict[str, tuple[int, int]]
    queues: dict[str, Queue]
    load: float | None
    ceiling: float | None
    memory: float | None
    # This machine's share of the jobs on their way, by kind, or None without a forecast.
    share: dict[str, float] | None
    empty_since: dict[str, int | None]
    last_change: int | None
    forecast: dict[str, float] | None = None
    slots: dict[str, int] = field(default_factory=dict)

    def room(self, kind: str) -> bool:
        return (
            self.load is not None
            and self.ceiling is not None
            and self.load <= self.ceiling - LOAD_MARGIN
            and self.memory is not None
            and self.memory >= MEMORY_GB[kind]
        )

    def waited(self, kind: str) -> float:
        """How long the oldest job of the kind has queued, in seconds; 0 with none queued."""
        oldest = self.queues[kind].oldest
        return 0 if oldest is None or not self.queues[kind].queued else self.moment - oldest

    def waiting(self, kind: str) -> bool:
        return self.waited(kind) >= QUEUED_FOR_SECONDS

    def wave(self, kind: str) -> bool:
        return self.share is not None and self.share[kind] > self.counts[kind]

    def held_for_wave(self, kind: str) -> bool:
        return self.share is not None and self.share[kind] > self.counts[kind] - 1

    def held_by_load(self) -> bool:
        return self.load is not None and self.ceiling is not None and self.load > self.ceiling


def decide(view: View) -> tuple[str, ...] | None:
    """("add", kind), ("remove", kind), ("move", from, to) or None."""
    if view.last_change is not None and view.moment - view.last_change < CHANGE_EVERY_SECONDS:
        return None
    below_max = [kind for kind in KINDS if view.counts[kind] < view.bounds[kind][1]]
    for kind in sorted(below_max, key=view.waited, reverse=True):
        if view.room(kind) and (view.waiting(kind) or view.wave(kind)):
            return ("add", kind)
    for kind in KINDS:
        other = "ordinary" if kind == "docker" else "docker"
        if view.counts[kind] <= view.bounds[kind][0] or view.held_for_wave(kind):
            continue
        if (
            view.waiting(other)
            and not view.queues[kind].queued
            and other in below_max
            and not view.room(other)
            and view.memory is not None
            and view.memory + MEMORY_GB[kind] >= MEMORY_GB[other]
        ):
            return ("move", kind, other)
        since = view.empty_since[kind]
        if since is not None and view.moment - since >= EMPTY_FOR_SECONDS and view.held_by_load():
            return ("remove", kind)
    return None


# What it changes.


def write_count(path: Path, kind: str, count: int) -> None:
    """The new count on its own line of the settings, the rest of the file and its mode kept."""
    name = COUNT_NAMES[kind]
    text = path.read_text()
    line = f"{name}={count}"
    pattern = re.compile(rf"^[ \t]*{name}=.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    mode = path.stat().st_mode & 0o7777
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".", delete=False) as output:
        output.write(text)
    os.chmod(output.name, mode)
    os.replace(output.name, path)


def systemctl(*arguments: str) -> None:
    done = subprocess.run(
        ["systemctl", "--user", *arguments], capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise Refused(f"systemctl {' '.join(arguments)}: {done.stderr.strip() or done.stdout.strip()}")


def _save(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".", delete=False) as output:
        output.write(text)
    os.chmod(output.name, 0o600)
    os.replace(output.name, path)


def _log(view: View, what: str) -> None:
    def queue(kind: str) -> str:
        queued = view.queues[kind].queued
        return f"{kind} {queued}" + (f" (oldest {int(view.waited(kind) // 60)} min)" if queued else "")

    forecast = (
        "forecast none"
        if view.share is None or view.forecast is None
        else f"forecast ordinary {view.share['ordinary']:.1f} of {view.forecast['ordinary']:g}, "
        f"docker {view.share['docker']:.1f} of {view.forecast['docker']:g}"
    )
    load = "load unread" if view.load is None else f"load {view.load:.2f} of {view.ceiling:g}"
    memory = "memory unread" if view.memory is None else f"memory {view.memory:.1f} GB"
    line = (
        f"{datetime.fromtimestamp(view.moment, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} {what}; "
        f"queued {queue('ordinary')}, {queue('docker')}; "
        f"running ordinary {view.queues['ordinary'].running}, "
        f"docker {view.queues['docker'].running}; {forecast}; {load}; {memory}"
    )
    descriptor = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a") as log:
        os.fchmod(descriptor, 0o600)
        log.write(line + "\n")
    print(line)


def apply(
    change: tuple[str, ...], box: Machine, view: View, listed: list[dict[str, Any]], path: Path
) -> bool:
    """Make the change; False when it waits for a busy runner."""
    counts = view.counts
    if change[0] == "add":
        kind = change[1]
        systemctl("enable", "--now", box.unit(kind, counts[kind] + 1))
        write_count(path, kind, counts[kind] + 1)
        verb = "added" if kind == "ordinary" else "started"
        _log(view, f"{verb} {WORDS[kind]} ({counts[kind]} to {counts[kind] + 1})")
        return True
    kind = change[1]
    name = box.runner_name(kind, counts[kind])
    if not idle(listed, name):
        print(f"{name} is not idle on GitHub; it is stopped once it is")
        return False
    systemctl("disable", "--now", box.unit(kind, counts[kind]))
    write_count(path, kind, counts[kind] - 1)
    if change[0] == "remove":
        verb = "removed" if kind == "ordinary" else "stopped"
        _log(view, f"{verb} {WORDS[kind]} ({counts[kind]} to {counts[kind] - 1})")
        return True
    other = change[2]
    try:
        systemctl("enable", "--now", box.unit(other, counts[other] + 1))
    except Refused as error:
        _log(
            view,
            f"drained {WORDS[kind]} ({counts[kind]} to {counts[kind] - 1}) to move it, and "
            f"{WORDS[other]} did not start",
        )
        raise PartlyChanged(str(error)) from None
    write_count(path, other, counts[other] + 1)
    _log(
        view,
        f"moved {WORDS[kind]} to {WORDS[other]} ({NAMES[kind]} {counts[kind]} to "
        f"{counts[kind] - 1}, {NAMES[other]} {counts[other]} to {counts[other] + 1})",
    )
    return True


def balance() -> int:
    moment = now()
    path = settings_path()
    box = machine(path)
    if box.off:
        print(f"balance off in {path}; the counts stay as they are")
        return 0
    listed = runners(box)
    found = queues(box)
    load, ceiling = load_and_ceiling()
    state = {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except OSError, ValueError:
        pass
    empty_since = {}
    for kind in KINDS:
        before = (state.get("empty_since") or {}).get(kind)
        empty_since[kind] = None if found[kind].queued else before if before is not None else moment
    forecast = read_forecast(box.forecast_path, moment)
    slots = pool_slots(box, listed)
    share = (
        None
        if forecast is None
        else {
            kind: forecast[kind] * box.counts[kind] / max(slots[kind], box.counts[kind], 1)
            for kind in KINDS
        }
    )
    view = View(
        moment=moment,
        counts=box.counts,
        bounds=box.bounds,
        queues=found,
        load=load,
        ceiling=ceiling,
        memory=available_gb(),
        share=share,
        empty_since=empty_since,
        last_change=state.get("last_change"),
        forecast=forecast,
        slots=slots,
    )
    change = decide(view)
    changed = False
    try:
        changed = change is not None and apply(change, box, view, listed, path)
    except PartlyChanged:
        changed = True
        raise
    finally:
        state = {
            "empty_since": empty_since,
            "last_change": moment if changed else state.get("last_change"),
        }
        _save(STATE_FILE, json.dumps(state) + "\n")
    if change is None:
        print("nothing to change")
    return 0


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another balancer run is changing the runners; this one leaves")
            return 0
        try:
            return balance()
        except Limited:
            print("the GitHub budget is paused; nothing changed this minute")
            return 0
        except (Refused, OSError, ValueError, KeyError, TypeError) as error:
            print(f"runner balance: {error}; nothing more changed", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
