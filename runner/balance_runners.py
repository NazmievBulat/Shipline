"""Moves this machine's runners between CI and Docker, and adds or removes them, by the queues,
its load and the demand on its way.

A systemd user timer runs it every minute (`systemd/example-app-runner-balance.timer`), installed
beside the spare supervisor (`keep-spare.sh`) and read like it: `settings.sh`, then the machine's
own `pool-settings.sh` that `install.sh` writes beside it. It changes only this machine's own
ordinary slots and Docker runners, never another machine's, never a main runner or the planner,
and only within the bounds of `pool-settings.sh`:

    balance_ordinary=MIN-MAX
    balance_docker=MIN-MAX

A kind without bounds, or one the machine has no role for (`roles`), keeps its installed count
(`ordinary_jobs`, `docker_runners`) as both bounds: nothing changes there until its owner widens
them. A line `balance off` stops the balancer, and the counts stay as they are.

An ordinary slot is one of the `ordinary_jobs` registrations that carry the pool's label
(`keep-spare.sh` gives it to that many): adding one starts the next unit,
`<prefix>-runner@<ordinary_jobs + ordinary_spares + 1>.service`, and writes the count one
higher; removing one writes it one lower and stops the highest unit. A Docker runner is
`<prefix>-runner-docker@<k>.service`, from 1 to `docker_runners`, only on a machine with
/dev/kvm. Every runner takes a place of the firewall's range: never more than `most_runners`.

Each minute it reads, from GitHub under the runners' own identity and within the shared budget
(`github_budget.py`), the queued and running jobs for the pool's labels, which every machine's
runners share (`label` and `docker_label` of `settings.sh`), and the runners of the pool; and
from the machine, its 5-minute load against the session start ceiling of
`scripts/workflow/session` (the processors times WORKFLOW_LOAD_PER_PROCESSOR, 1.5) and its
available memory.

- It adds an ordinary slot when ordinary jobs have queued for 5 minutes, or a Docker runner when
  Docker jobs have, when there is room: load at least 4 under the ceiling, and at least 3 GB
  available for an ordinary slot or 8 GB for a Docker runner.
- It removes one of a kind when that kind had nothing queued for 15 minutes and the load holds
  session starts (above the ceiling). It moves one to the other kind when the other kind has
  queued for 5 minutes, this kind has nothing queued and there is no room to add: it drains
  one, then adds one of the other in the same step, when the memory the drained one frees is
  enough for the other.
- A runner goes only once GitHub lists it online and idle, never mid-job: its registration is
  deleted before its unit stops, which GitHub refuses while it runs a job. The highest numbered
  one goes (`<machine>-<k>`, `<machine>-docker-<k>`, as `names.sh` names them), and the step
  waits while it is busy. A count is written before a runner stops and after a new one starts,
  each put back when the other half fails.
- At most one change every 5 minutes.
- The forecast the tick writes (`~/.local/state/workflow/ci-demand.json`, or the path of
  `balance_forecast`) says how many ordinary and Docker jobs come within 30 minutes. This
  machine's share of a kind is that times its slots over all the pool's online slots of that
  kind on GitHub. A removal waits while the share would queue on one slot fewer; a slot is
  added before the wave when the share would queue on the slots there are and there is room,
  never beyond the bounds; a kind the machine has none of gets its first one when that slot's
  share would be a whole job. A forecast older than 10 minutes is ignored, and the queues alone
  decide.

Every change is one line of `~/.local/state/workflow/runner-balance.log`: the time, what
changed, the queues, the forecast, the load and the memory. Its state (since when a kind's
queue was empty, the last change) is in `runner-balance.json` beside it.

    balance_runners.py            balance, once
    balance_runners.py kept FILE  the balancer's own lines of a pool-settings.sh, which a
                                  reinstall keeps (`install.sh`)
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# The runner install keeps github_budget.py beside this file; the repository keeps it here.
if not (HERE / "github_budget.py").exists():
    sys.path.append(str(HERE.parent / "scripts" / "workflow"))
from github_budget import RUNNER, Limited, call  # noqa: E402

KINDS = ("docker", "ordinary")
# What the settings call each kind's installed count.
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
# What a repository, the prefix, a machine's name and a label may be: they go into GitHub's
# paths, the unit names and the labels. `.` and `..` are no owner or name. The prefix and the
# machine are as `render-units.sh` takes them.
REPOSITORY = re.compile(r"(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+")
PREFIX = re.compile(r"[a-z]([a-z0-9-]{0,23}[a-z0-9])?")
MACHINE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SETTINGS_SECONDS = 5
# A Docker runner's VM may take a while to start; one that takes longer is not waited for.
SYSTEMCTL_SECONDS = 180
GITHUB_SECONDS = 30
PER_PAGE = 100
# What GitHub answers when a runner's registration is deleted while it runs a job.
BUSY = re.compile(r"currently running a job|HTTP 422")

STATE = Path.home() / ".local/state/workflow"
LOG = STATE / "runner-balance.log"
STATE_FILE = STATE / "runner-balance.json"
LOCK = STATE / "runner-balance.lock"
DEFAULT_FORECAST = STATE / "ci-demand.json"
SETTINGS = HERE / "settings.sh"
POOL_SETTINGS = HERE / "pool-settings.sh"
# The balancer's own lines of pool-settings.sh: a reinstall keeps them.
OWN_LINE = re.compile(r"[ \t]*(balance_(ordinary|docker|forecast)=.*|balance[ \t]+off)[ \t]*")
SETTINGS_READ = (
    "repository",
    "RUNNER_PREFIX",
    "machine",
    "label",
    "docker_label",
    "first_label",
    "main_label",
    "roles",
    "ordinary_jobs",
    "ordinary_spares",
    "docker_runners",
    "most_runners",
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


def read_settings() -> dict[str, str]:
    """The values the balancer reads, as the pool's scripts read them (settings.sh, then
    pool-settings.sh), `balance off` among them, with only PATH and HOME, for at most
    SETTINGS_SECONDS."""
    script = (
        '. "$1" >/dev/null 2>&1 || exit 1; '
        "balance() { [[ ${1-} == off ]] && balance_off=1; return 0; }; "
        '. "$2" >/dev/null 2>&1; shift 2; '
        'for name in "$@"; do printf "%s=%s\\0" "$name" "${!name-}"; done'
    )
    if not POOL_SETTINGS.is_file():
        raise Refused(f"{POOL_SETTINGS} is missing: install.sh writes it")
    try:
        done = subprocess.run(
            ["bash", "-c", script, "settings", str(SETTINGS), str(POOL_SETTINGS), *SETTINGS_READ],
            env={key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ},
            capture_output=True,
            text=True,
            timeout=SETTINGS_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refused(f"the settings ran for more than {SETTINGS_SECONDS} seconds") from None
    if done.returncode:
        raise Refused(f"{SETTINGS} cannot be read")
    return dict(item.split("=", 1) for item in done.stdout.split("\0") if item)


def _number(values: dict[str, str], name: str, default: int | None = None) -> int | None:
    text = values.get(name, "")
    if not text:
        return default
    if not text.isdigit():
        raise Refused(f"{name} is '{text}', not a count")
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
    labels: dict[str, str]
    # The jobs of the merge train's first member ask for its own label, which every slot carries
    # beside the pool's (keep-spare.sh), and so does one of main's runners while it is lent.
    first_label: str
    main_label: str
    counts: dict[str, int]
    bounds: dict[str, tuple[int, int]]
    # The registered ordinary runners beyond the slots, and the most runners of all kinds.
    spares: int
    most: int
    off: bool
    forecast_path: Path

    def label(self, kind: str) -> str:
        return self.labels[kind]

    def highest(self, kind: str) -> int:
        """The number of the kind's highest runner."""
        return self.counts[kind] + (self.spares if kind == "ordinary" else 0)

    def unit(self, kind: str, k: int) -> str:
        return f"{self.prefix}-runner{'-docker' if kind == 'docker' else ''}@{k}.service"

    def runner_name(self, kind: str, k: int) -> str:
        return f"{self.name}{'-docker' if kind == 'docker' else ''}-{k}"

    def places(self) -> int:
        """The places of the firewall's range the runners take."""
        return self.highest("ordinary") + self.counts["docker"]


def machine() -> Machine:
    values = read_settings()
    # The pool's units are named from its prefix: the environment's, the settings', or that of
    # the folder the pool is installed in, `<prefix>-runner`.
    values["RUNNER_PREFIX"] = (
        os.environ.get("RUNNER_PREFIX")
        or values.get("RUNNER_PREFIX")
        or HERE.name.removesuffix("-runner")
    )
    for needed, pattern in (
        ("repository", REPOSITORY),
        ("RUNNER_PREFIX", PREFIX),
        ("machine", MACHINE),
        ("label", LABEL),
    ):
        if not pattern.fullmatch(values.get(needed, "")):
            raise Refused(f"the settings name no {needed}")
    labels = {
        "ordinary": values["label"],
        "docker": values.get("docker_label") or values["label"] + "-docker",
    }
    first_label = values.get("first_label") or values["label"] + "-first"
    main_label = values.get("main_label") or values["label"] + "-main"
    for name, label in (
        ("docker_label", labels["docker"]),
        ("first_label", first_label),
        ("main_label", main_label),
    ):
        if not LABEL.fullmatch(label):
            raise Refused(f"the settings name no {name}")
    roles = set(values.get("roles", "").split())
    ordinary = _number(values, "ordinary_jobs")
    if ordinary is None:
        raise Refused("the settings name no ordinary_jobs")
    # An install that wrote no Docker count leaves its Docker runners as they are.
    docker = _number(values, "docker_runners")
    counts = {"ordinary": ordinary, "docker": docker or 0}
    bounds = {kind: _bounds(values, kind, counts[kind], roles) for kind in KINDS}
    if docker is None:
        bounds["docker"] = (0, 0)
    return Machine(
        repository=values["repository"],
        prefix=values["RUNNER_PREFIX"],
        name=values["machine"],
        labels=labels,
        first_label=first_label,
        main_label=main_label,
        counts=counts,
        bounds=bounds,
        spares=(_number(values, "ordinary_spares", 0) or 0) if "ordinary" in roles else 0,
        most=_number(values, "most_runners", 0) or 0,
        off=values.get("balance_off") == "1",
        forecast_path=Path(values.get("balance_forecast") or DEFAULT_FORECAST).expanduser(),
    )


def has_kvm() -> bool:
    return Path(os.environ.get("RUNNER_KVM_DEVICE") or "/dev/kvm").exists()


# What the machine reads.


def load_and_ceiling() -> tuple[float | None, float | None]:
    """The 5-minute load and the session start ceiling, each None when it cannot be read."""
    try:
        per = float(os.environ.get("WORKFLOW_LOAD_PER_PROCESSOR") or 1.5)
        processors = int(
            subprocess.run(["nproc"], capture_output=True, text=True, timeout=5).stdout
        )
        ceiling = processors * per if processors > 0 and per > 0 else None
    except OSError, ValueError, subprocess.SubprocessError:
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


def _items(repository: str, path: str, key: str) -> list[dict[str, Any]]:
    """Every item of a listing, from all its pages, one JSON object a line."""
    done = call(
        "balancer",
        "api",
        "--paginate",
        f"repos/{repository}/{path}",
        "--jq",
        f".{key}[] | @json",
        timeout=GITHUB_SECONDS,
        identity=RUNNER,
    )
    if done.returncode:
        raise Refused(f"GitHub did not answer {path}: {done.stderr.strip()}")
    items = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    if not all(isinstance(item, dict) for item in items):
        raise Refused(f"GitHub answered {path} with something that is not a list of objects")
    return items


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
    """The queued and running jobs of the pool's two labels, every machine's."""
    found = {kind: Queue() for kind in KINDS}
    labels = {box.label(kind): kind for kind in KINDS} | {box.first_label: "ordinary"}
    # A run that moved from queued to running between the two reads is in both.
    read: set[int] = set()
    for status in ("queued", "in_progress"):
        runs = _items(
            box.repository, f"actions/runs?status={status}&per_page={PER_PAGE}", "workflow_runs"
        )
        for run in runs:
            if int(run["id"]) in read:
                continue
            read.add(int(run["id"]))
            jobs = _items(
                box.repository, f"actions/runs/{int(run['id'])}/jobs?per_page={PER_PAGE}", "jobs"
            )
            for job in jobs:
                kind = next(
                    (labels[label] for label in job.get("labels") or [] if label in labels), None
                )
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
    return _items(box.repository, f"actions/runners?per_page={PER_PAGE}", "runners")


def pool_slots(box: Machine, listed: list[dict[str, Any]]) -> dict[str, int]:
    """The pool's online runners of each kind, every machine's: those with the pool's ordinary
    label and those with its Docker label. A main runner (also one lent to pull request jobs), a
    spare and the planner are neither."""
    found = dict.fromkeys(KINDS, 0)
    for runner in listed:
        labels = {_label(label) for label in runner.get("labels") or []}
        if runner.get("status") != "online" or box.main_label in labels:
            continue
        for kind in KINDS:
            found[kind] += box.label(kind) in labels
    return found


def _label(label: Any) -> str:
    return str(label.get("name", "")) if isinstance(label, dict) else ""


def labelled_slots(box: Machine, listed: list[dict[str, Any]]) -> int:
    """This machine's registrations that carry the pool's label (keep-spare.sh gives it to as
    many as the slots), busy or offline ones too."""
    own = re.compile(rf"{re.escape(box.name)}-[0-9]+")
    return sum(
        bool(own.fullmatch(str(runner.get("name"))))
        and box.label("ordinary") in {_label(label) for label in runner.get("labels") or []}
        for runner in listed
    )


def idle(listed: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The runner, when GitHub lists it online and running no job."""
    return next(
        (
            runner
            for runner in listed
            if runner.get("name") == name
            and runner.get("status") == "online"
            and runner.get("busy") is False
        ),
        None,
    )


def deregister(box: Machine, runner: dict[str, Any]) -> bool:
    """Delete the runner's registration: False when GitHub refuses because it runs a job."""
    path = f"repos/{box.repository}/actions/runners/{int(runner['id'])}"
    done = call("balancer", "api", "-X", "DELETE", path, timeout=GITHUB_SECONDS, identity=RUNNER)
    if done.returncode == 0:
        return True
    if BUSY.search(done.stderr + done.stdout):
        return False
    raise Refused(f"GitHub did not delete {runner.get('name')}: {done.stderr.strip()}")


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
    # Whether one more of a kind fits the bounds, the places and the machine (/dev/kvm): as it is,
    # and once one of the other kind is gone.
    can_add: dict[str, bool]
    can_move_into: dict[str, bool]
    forecast: dict[str, float] | None = None
    # The share a first slot of each kind would take, for a kind the machine has none of.
    first_share: dict[str, float] | None = None

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
        """The share would queue on the slots there are; with none, a first slot would take at
        least one whole job."""
        if self.share is None:
            return False
        if self.counts[kind] == 0:
            return self.first_share is not None and self.first_share[kind] >= 1
        return self.share[kind] > self.counts[kind]

    def held_for_wave(self, kind: str) -> bool:
        return self.share is not None and self.share[kind] > self.counts[kind] - 1

    def held_by_load(self) -> bool:
        return self.load is not None and self.ceiling is not None and self.load > self.ceiling


def decide(view: View) -> tuple[str, ...] | None:
    """("add", kind), ("remove", kind), ("move", from, to) or None."""
    if view.last_change is not None and view.moment - view.last_change < CHANGE_EVERY_SECONDS:
        return None
    addable = [kind for kind in KINDS if view.can_add[kind]]
    for kind in sorted(addable, key=view.waited, reverse=True):
        if view.room(kind) and (view.waiting(kind) or view.wave(kind)):
            return ("add", kind)
    for kind in KINDS:
        other = "ordinary" if kind == "docker" else "docker"
        if view.counts[kind] <= view.bounds[kind][0] or view.held_for_wave(kind):
            continue
        if (
            view.waiting(other)
            and not view.queues[kind].queued
            and view.can_move_into[other]
            and not view.room(other)
            and view.load is not None
            and view.ceiling is not None
            and view.memory is not None
            and view.memory + MEMORY_GB[kind] >= MEMORY_GB[other]
        ):
            return ("move", kind, other)
        since = view.empty_since[kind]
        if since is not None and view.moment - since >= EMPTY_FOR_SECONDS and view.held_by_load():
            return ("remove", kind)
    return None


# What it changes.


def write_count(kind: str, count: int) -> None:
    """The new count on its own line of pool-settings.sh, the rest of the file and its mode
    kept, replaced whole: a script that reads it meanwhile reads the old or the new."""
    path = POOL_SETTINGS
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
    try:
        done = subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refused(
            f"systemctl {' '.join(arguments)} ran for more than {SYSTEMCTL_SECONDS} seconds"
        ) from None
    if done.returncode:
        raise Refused(
            f"systemctl {' '.join(arguments)}: {done.stderr.strip() or done.stdout.strip()}"
        )


def _save(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".", delete=False) as output:
        output.write(text)
    os.chmod(output.name, 0o600)
    os.replace(output.name, path)


def _log(view: View, what: str) -> None:
    def queue(kind: str) -> str:
        queued = view.queues[kind].queued
        oldest = f" (oldest {int(view.waited(kind) // 60)} min)" if queued else ""
        return f"{kind} {queued}{oldest}"

    forecast = (
        "forecast none"
        if view.share is None or view.forecast is None
        else f"forecast ordinary {view.share['ordinary']:.1f} of {view.forecast['ordinary']:g}, "
        f"docker {view.share['docker']:.1f} of {view.forecast['docker']:g}"
    )
    ceiling = "?" if view.ceiling is None else f"{view.ceiling:g}"
    load = "load unread" if view.load is None else f"load {view.load:.2f} of {ceiling}"
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


def _start(box: Machine, kind: str) -> None:
    """Start the next runner of the kind, then write its count; a count that cannot be written
    stops the runner again."""
    unit = box.unit(kind, box.highest(kind) + 1)
    try:
        systemctl("enable", "--now", unit)
        write_count(kind, box.counts[kind] + 1)
    except (Refused, OSError):
        # A unit enabled that did not start would come up at the next boot, at a place of the
        # firewall's range no count holds.
        try:
            systemctl("disable", "--now", unit)
        except Refused as error:
            print(f"runner balance: {error}", file=sys.stderr)
        raise
    box.counts[kind] += 1


def _stop(box: Machine, kind: str, runner: dict[str, Any]) -> bool:
    """Write the count one lower, delete the highest runner's registration and stop its unit;
    False, with the count put back, when GitHub refuses because the runner took a job. A
    runner that does not stop gets its count back too."""
    count, unit = box.counts[kind], box.unit(kind, box.highest(kind))
    write_count(kind, count - 1)
    try:
        if not deregister(box, runner):
            write_count(kind, count)
            return False
        systemctl("disable", "--now", unit)
    except Refused:
        write_count(kind, count)
        raise
    box.counts[kind] -= 1
    return True


def apply(change: tuple[str, ...], box: Machine, view: View, listed: list[dict[str, Any]]) -> bool:
    """Make the change; False when it waits for a busy runner."""
    before = dict(box.counts)
    kind = change[1]
    if change[0] == "add":
        _start(box, kind)
        verb = "added" if kind == "ordinary" else "started"
        _log(view, f"{verb} {WORDS[kind]} ({before[kind]} to {box.counts[kind]})")
        return True
    name = box.runner_name(kind, box.highest(kind))
    runner = idle(listed, name)
    if runner is None or not _stop(box, kind, runner):
        print(f"{name} is not idle on GitHub; it is stopped once it is")
        return False
    if change[0] == "remove":
        verb = "removed" if kind == "ordinary" else "stopped"
        _log(view, f"{verb} {WORDS[kind]} ({before[kind]} to {box.counts[kind]})")
        return True
    other = change[2]
    try:
        _start(box, other)
    except (Refused, OSError) as error:
        _log(
            view,
            f"drained {WORDS[kind]} ({before[kind]} to {box.counts[kind]}) to move it, and "
            f"{WORDS[other]} did not start",
        )
        raise PartlyChanged(str(error)) from None
    _log(
        view,
        f"moved {WORDS[kind]} to {WORDS[other]} ({NAMES[kind]} {before[kind]} to "
        f"{box.counts[kind]}, {NAMES[other]} {before[other]} to {box.counts[other]})",
    )
    return True


def _fits(box: Machine, kind: str, listed: list[dict[str, Any]], *, freed: int = 0) -> bool:
    """One more of the kind fits its bounds, the firewall's places and the machine, and, for an
    ordinary slot, would take a job: every slot there is has its label already."""
    return (
        box.counts[kind] < box.bounds[kind][1]
        and box.places() - freed + 1 <= box.most
        and (kind != "docker" or has_kvm())
        and (kind != "ordinary" or labelled_slots(box, listed) >= box.counts[kind])
    )


def balance() -> int:
    moment = now()
    box = machine()
    if box.off:
        print(f"balance off in {POOL_SETTINGS}; the counts stay as they are")
        return 0
    found = queues(box)
    # After the queues, which take a call per run: the listing a stop is decided on is fresh.
    listed = runners(box)
    load, ceiling = load_and_ceiling()
    state: dict[str, Any] = {}
    try:
        state = json.loads(STATE_FILE.read_text())
    except OSError, ValueError:
        pass
    if not isinstance(state, dict) or not isinstance(state.get("empty_since") or {}, dict):
        state = {}
    if not isinstance(state.get("last_change"), (int, float)):
        state.pop("last_change", None)
    # A queue not watched for longer than the time it must stay empty (the balancer stopped, a
    # budget pause) is watched again from now.
    seen = state.get("seen_at")
    watched = isinstance(seen, (int, float)) and moment - seen <= EMPTY_FOR_SECONDS
    empty_since: dict[str, int | None] = {}
    for kind in KINDS:
        before = (state.get("empty_since") or {}).get(kind)
        before = before if watched and isinstance(before, (int, float)) else None
        empty_since[kind] = None if found[kind].queued else before if before is not None else moment
    forecast = read_forecast(box.forecast_path, moment)
    slots = pool_slots(box, listed)
    share = first_share = None
    if forecast is not None:
        share = {
            kind: forecast[kind] * box.counts[kind] / max(slots[kind], box.counts[kind], 1)
            for kind in KINDS
        }
        first_share = {kind: forecast[kind] / (slots[kind] + 1) for kind in KINDS}
    view = View(
        moment=moment,
        counts=dict(box.counts),
        bounds=box.bounds,
        queues=found,
        load=load,
        ceiling=ceiling,
        memory=available_gb(),
        share=share,
        empty_since=empty_since,
        last_change=state.get("last_change"),
        can_add={kind: _fits(box, kind, listed) for kind in KINDS},
        can_move_into={kind: _fits(box, kind, listed, freed=1) for kind in KINDS},
        forecast=forecast,
        first_share=first_share,
    )
    change = decide(view)
    changed = False
    try:
        changed = change is not None and apply(change, box, view, listed)
    except PartlyChanged:
        changed = True
        raise
    finally:
        state = {
            "empty_since": empty_since,
            "last_change": moment if changed else state.get("last_change"),
            "seen_at": moment,
        }
        _save(STATE_FILE, json.dumps(state) + "\n")
    if change is None:
        print("nothing to change")
    return 0


def kept(path: Path) -> int:
    """Print the balancer's own lines of a pool-settings.sh: its bounds, its forecast path and
    `balance off`."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return 0
    for line in lines:
        if OWN_LINE.fullmatch(line):
            print(line.strip())
    return 0


def main() -> int:
    if sys.argv[1:2] == ["kept"] and len(sys.argv) == 3:
        return kept(Path(sys.argv[2]))
    if len(sys.argv) > 1:
        print("usage: balance_runners.py [kept <pool-settings.sh>]", file=sys.stderr)
        return 2
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
        except (
            Refused,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            print(f"runner balance: {error}; nothing more changed", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
