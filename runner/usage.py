"""What a machine's CI load leaves free, one line a minute, and the busiest hour of each day.

`sample-usage.sh`, run every minute by `<prefix>-runner-usage.timer` on every machine with
runners, appends one line to `~/.local/state/ci-runner/usage.log`:

    2026-10-23T02:31:00Z free_mb=10240 available_mb=22630 swap_in_mb_s=0.0 swap_out_mb_s=0.0 \
cpu_pressure_60s=8.12 load_1m=15.20 busy=second-1,second-docker-2

Memory is in MiB, swap in MiB a second over the time since the line before (`?` on the first
line after a restart), CPU pressure is the share of the last 60 seconds in which some task waited
for a processor (`/proc/pressure/cpu`), and `busy` names this machine's runners that GitHub
listed as running a job (`none`, or `unknown` without a fresh listing). When its first line is 31 days
old the log is moved to `usage.log.1`, so the two files together always hold a month or more.

`python3 usage.py report [--days N]` reads both files and prints, for each of the last N days
(UTC), the hour with the highest mean CPU pressure, that mean, and the least memory available in
that hour; and whether every one of those hours had room (README.md, "A second machine").

Before that last line it says how many more PR jobs that room holds: none unless every day had
room, else the least memory available at a busiest hour, less the margin, in jobs of the cap one
ordinary job of this machine gets (`--job-memory`, settings.sh's `memory` when it names none).
`python3 usage.py room [--days N] [--job-memory 6g]` prints that number alone, which install.sh
reads before it lets this machine take more PR slots (README.md, "Moving a machine's PR slots").
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

LOG_FOLDER = Path.home() / ".local/state/ci-runner"
# By age, not size: a line is longer the more runners are busy. Just after a move the moved file
# alone holds this many days.
LOG_DAYS = 31
# The spare supervisor replaces the listing every five seconds (keep-spare.sh); the planner
# counts nothing from one older than this, and neither does the sampler.
LISTING_SECONDS = 20
# Room at the busiest hour, as a measured week of use set it.
MOST_PRESSURE = 25.0
LEAST_AVAILABLE_MB = 8 * 1024
# The cap of one ordinary job when the machine names none: settings.sh's `memory`.
JOB_MEMORY = "10g"


def _meminfo(proc: Path) -> dict[str, int]:
    values = {}
    for line in (proc / "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        values[name] = int(rest.split()[0])
    return values


def _vmstat(proc: Path) -> dict[str, int]:
    return {
        name: int(value)
        for name, value in (line.split() for line in (proc / "vmstat").read_text().splitlines())
    }


def _cpu_pressure(proc: Path) -> float:
    some = (proc / "pressure/cpu").read_text().splitlines()[0]
    return float(dict(field.split("=") for field in some.split()[1:])["avg60"])


def _busy(machine: str, listing: Path, now: float) -> str:
    try:
        if not 0 <= now - listing.stat().st_mtime < LISTING_SECONDS:
            return "unknown"
        rows = listing.read_text().splitlines()
    except OSError:
        return "unknown"
    names = sorted(
        fields[1]
        for fields in (row.split("\t") for row in rows)
        if len(fields) == 5 and fields[1].startswith(f"{machine}-") and fields[3] == "true"
    )
    return ",".join(names) or "none"


def _swap(proc: Path, state: Path, now: float) -> tuple[str, str]:
    """Swap in and out in MiB a second since the last sample, `?` when there is none to compare."""
    vmstat = _vmstat(proc)
    boot = (proc / "sys/kernel/random/boot_id").read_text().strip()
    current = {"boot": boot, "time": now, "in": vmstat["pswpin"], "out": vmstat["pswpout"]}
    try:
        last = json.loads(state.read_text())
    except OSError, ValueError:
        last = None
    temporary = state.with_name(state.name + ".new")
    temporary.write_text(json.dumps(current))
    temporary.replace(state)
    if (
        not isinstance(last, dict)
        or last.get("boot") != boot
        or now - last["time"] <= 0
        or current["in"] < last["in"]
        or current["out"] < last["out"]
    ):
        return "?", "?"
    page = os.sysconf("SC_PAGE_SIZE")
    seconds = now - last["time"]
    return tuple(  # type: ignore[return-value]
        f"{(current[way] - last[way]) * page / 1048576 / seconds:.1f}" for way in ("in", "out")
    )


def _first_time(log: Path) -> float:
    """When the log's first line was written; never for a log that is missing or unreadable."""
    try:
        with log.open() as lines:
            stamp = lines.readline().split(maxsplit=1)[0]
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (OSError, IndexError, ValueError):
        return float("inf")


def sample(
    machine: str,
    *,
    proc: Path = Path("/proc"),
    folder: Path = LOG_FOLDER,
    listing: Path | None = None,
    now: float | None = None,
) -> str:
    """Appends this minute's line to the log and returns it."""
    now = time.time() if now is None else now
    listing = listing or Path(__file__).with_name("runners.tsv")
    folder.mkdir(parents=True, exist_ok=True)
    memory = _meminfo(proc)
    swap_in, swap_out = _swap(proc, folder / "usage.state", now)
    load = (proc / "loadavg").read_text().split()[0]
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        f"{stamp} free_mb={memory['MemFree'] // 1024}"
        f" available_mb={memory['MemAvailable'] // 1024}"
        f" swap_in_mb_s={swap_in} swap_out_mb_s={swap_out}"
        f" cpu_pressure_60s={_cpu_pressure(proc):.2f} load_1m={load}"
        f" busy={_busy(machine, listing, now)}"
    )
    log = folder / "usage.log"
    if _first_time(log) <= now - LOG_DAYS * 86400:
        log.replace(folder / "usage.log.1")
    with log.open("a") as output:
        output.write(line + "\n")
    return line


def _lines(folder: Path) -> list[dict[str, str]]:
    rows = []
    for name in ("usage.log.1", "usage.log"):
        try:
            text = (folder / name).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            # A line cut short by a full disk is no reason to report nothing.
            if not line.strip():
                continue
            stamp, *fields = line.split()
            row = dict(field.split("=", 1) for field in fields if "=" in field)
            row["time"] = stamp
            rows.append(row)
    return rows


def _megabytes(size: str) -> int:
    """A cap of whole gigabytes, as install.sh takes `job_memory` (`6g`), in MiB."""
    if not (size.endswith("g") and size[:-1].isdigit() and int(size[:-1]) > 0):
        raise ValueError(f"a job's memory is whole gigabytes, such as 6g, not {size!r}")
    return int(size[:-1]) * 1024


def jobs_with_room(room: bool, least_available_mb: int, job_memory: str) -> int:
    """How many more ordinary jobs of `job_memory` each the measured room holds: what was least
    available at a busiest hour, less the margin. None without room every day."""
    if not room:
        return 0
    return max(0, (least_available_mb - LEAST_AVAILABLE_MB) // _megabytes(job_memory))


def report(
    days: int,
    *,
    folder: Path = LOG_FOLDER,
    now: datetime | None = None,
    job_memory: str = JOB_MEMORY,
) -> tuple[str, bool]:
    """The busiest hour of each of the last `days` days, how many more ordinary jobs their room
    holds, and whether all of them had room.

    A day with an hour without lines counts as no room, as does a day with no line at all: a
    week of room needs a week of lines, and the hours a machine was off may be its busiest.
    """
    text, room, _ = _measured(days, folder=folder, now=now, job_memory=job_memory)
    return text, room


def room_for_jobs(
    days: int,
    *,
    folder: Path = LOG_FOLDER,
    now: datetime | None = None,
    job_memory: str = JOB_MEMORY,
) -> int:
    """How many more ordinary jobs the last `days` days measured room for (`report`)."""
    return _measured(days, folder=folder, now=now, job_memory=job_memory)[2]


def _measured(
    days: int, *, folder: Path, now: datetime | None, job_memory: str
) -> tuple[str, bool, int]:
    now = now or datetime.now(UTC)
    first = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    hours: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for row in _lines(folder):
        try:
            sampled = (float(row["cpu_pressure_60s"]), int(row["available_mb"]))
        except KeyError, ValueError:
            continue
        if row["time"][:10] >= first:
            hours[row["time"][:13]].append(sampled)
    out = []
    room = True
    least = None
    for day in [(now - timedelta(days=back)).strftime("%Y-%m-%d") for back in range(days, 0, -1)]:
        of_day = {hour: rows for hour, rows in hours.items() if hour.startswith(day)}
        if not of_day:
            out.append(f"{day}  no lines")
            room = False
            continue
        hour, rows = max(of_day.items(), key=lambda item: sum(p for p, _ in item[1]) / len(item[1]))
        pressure = sum(p for p, _ in rows) / len(rows)
        available = min(a for _, a in rows)
        least = available if least is None else min(least, available)
        fits = (
            pressure < MOST_PRESSURE and available > LEAST_AVAILABLE_MB and len(of_day) == 24
        )
        room = room and fits
        verdict = "room" if fits else "no room"
        if len(of_day) < 24:
            verdict += f" (lines in only {len(of_day)} of 24 hours)"
        out.append(
            f"{day}  busiest hour {hour[11:]}:00 UTC  CPU pressure {pressure:.1f}%"
            f"  least available {available / 1024:.1f} GiB  {verdict}"
        )
    jobs = jobs_with_room(room, least or 0, job_memory)
    out.append(
        f"Room for {jobs} more PR jobs of {job_memory} each (the least available at a busiest"
        f" hour, less {LEAST_AVAILABLE_MB // 1024} GiB)"
    )
    out.append(
        f"Room every day: {'yes' if room else 'no'} (CPU pressure under {MOST_PRESSURE:.0f}% and"
        f" more than {LEAST_AVAILABLE_MB // 1024} GiB available at the busiest hour)"
    )
    return "\n".join(out), room, jobs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sample").add_argument("machine")
    for name in ("report", "room"):
        command = commands.add_parser(name)
        command.add_argument("--days", type=int, default=7)
        command.add_argument("--job-memory", default=JOB_MEMORY)
    arguments = parser.parse_args()
    if arguments.command == "sample":
        sample(arguments.machine)
    elif arguments.command == "room":
        print(room_for_jobs(arguments.days, job_memory=arguments.job_memory))
    else:
        text, room = report(arguments.days, job_memory=arguments.job_memory)
        print(text)
        sys.exit(0 if room else 1)
