"""Lend one of main's idle runners to pull request jobs, and the merge train's first member first.

Runs on the machine with main's runners as `<prefix>-runner-lend.service`, as the runner
programs' own GitHub identity (github_identity.py), or the owner's `gh` login without one, as the
spare supervisor does. Main's runners register with `<label>-main` alone. While no job of a push
to main, nor of a dispatched run, the merge gate's runs of its prefixes among them, is queued or
running for main's runners (the merge line's runs are main's own: examples/ci.yml), this gives
one idle main runner the pull request labels too: `<label>-first`, which the jobs of the train's
first member ask for, and `<label>`, which every other pull request job asks for. While a job of
the first member waits, the lent runner keeps only the first label, so it takes that job before
older ones. When a job of main or of a dispatched run is under way, an idle lent runner loses
both. A busy runner is never relabelled: its labels go with its registration when the job ends.

The merge gate writes the train's first member to `~/.local/state/workflow/train-first.json`
(scripts/workflow/merge_gate.py). This copies it, with the time, into the folder the planner's
container reads (`home/first.json`), so the planner sends that member's runs to the first label
only while this service runs to label runners for it. A run of the first member planned before it
became first still asks for `<label>` and waits behind older runs: it is cancelled and
run again once, so that its planner sends it to the first label.
"""

import contextlib
import fcntl
import json
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from github_budget import RUNNER, Limited, call, poll_delay, wait

# Only these two workflows run on the pull request runners and have a planner that knows the
# first label (.github/workflows/ci.yml, e2e.yml).
WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/e2e.yml")
Api = Callable[..., Any]
# How many rounds a run this cancelled is tried again when GitHub refuses or cannot be read.
RERUN_TRIES = 5


@dataclass(frozen=True)
class Labels:
    home: str
    first: str
    main: str


@dataclass(frozen=True)
class Registration:
    id: int
    name: str
    online: bool
    busy: bool
    labels: frozenset[str]


def lending(
    runners: list[Registration],
    labels: Labels,
    *,
    main_waiting: bool,
    first_waiting: bool,
    most: int,
) -> list[tuple[str, int, str]]:
    """The label changes, as ("add" | "remove", runner id, label), that make main's runners lent
    as they should be. At most `most` main registrations carry a pull request label, and
    at least one online main registration is kept unlent, including during runner replacement."""
    lent_labels = {labels.home, labels.first}
    mains = sorted(
        (runner for runner in runners if labels.main in runner.labels),
        key=lambda runner: runner.id,
    )
    lent = [runner for runner in mains if runner.labels & lent_labels]
    # Registrations disappear between ephemeral jobs or stay offline after a failure. Counting
    # them as main capacity could lend its only online runner to a long pull request job.
    most = min(most, max(0, sum(runner.online for runner in mains) - 1))
    # A busy lent runner keeps its labels until its job ends; it still counts.
    room = (
        0
        if main_waiting
        else max(0, most - sum(runner.busy or not runner.online for runner in lent))
    )
    idle_lent = [runner for runner in lent if runner.online and not runner.busy]
    # One that came back online with its labels after another was lent makes one too many.
    kept, given_back = (
        idle_lent[:room],
        idle_lent[room:] + [runner for runner in lent if not runner.online and not runner.busy],
    )
    kept += [
        runner for runner in mains if runner.online and not runner.busy and runner not in lent
    ][: room - len(kept)]
    wanted = {labels.first} if first_waiting else lent_labels
    changes: list[tuple[str, int, str]] = []
    for runner in given_back:
        changes += [("remove", runner.id, label) for label in sorted(runner.labels & lent_labels)]
    for runner in kept:
        changes += [("add", runner.id, label) for label in sorted(wanted - runner.labels)]
        changes += [
            ("remove", runner.id, label) for label in sorted((runner.labels & lent_labels) - wanted)
        ]
    return changes


def gh(*arguments: str) -> Any:
    done = call("lender", "api", *arguments, identity=RUNNER)
    done.check_returncode()
    return json.loads(done.stdout) if done.stdout.strip() else None


class RoundReads:
    """Reuse repeated queue reads within one round, until any mutation is attempted."""

    def __init__(self) -> None:
        self.reads: dict[tuple[str, ...], Any] = {}

    def __call__(self, *arguments: str) -> Any:
        if "-X" in arguments:
            self.reads.clear()
            return gh(*arguments)
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/[0-9]+", arguments[0]):
            # Cancellation polls must see completion between attempts, within a round.
            return gh(*arguments)
        if arguments not in self.reads:
            self.reads[arguments] = gh(*arguments)
        return self.reads[arguments]


def registrations(api: Api, repository: str, listing: Path | None = None) -> list[Registration]:
    if listing is not None:
        try:
            # The supervisor replaces this file atomically after a complete read. A stale
            # or malformed list must fall back to GitHub, never to partial registrations.
            if 0 <= time.time() - listing.stat().st_mtime < 10:
                shared: list[Registration] = []
                for line in listing.read_text().splitlines():
                    identity, name, status, busy, labels = line.split("\t")
                    if (
                        not identity.isdigit()
                        or status not in {"online", "offline"}
                        or busy not in {"true", "false"}
                    ):
                        raise ValueError("invalid runner listing")
                    shared.append(
                        Registration(
                            int(identity),
                            name,
                            status == "online",
                            busy == "true",
                            frozenset(labels.split(",")),
                        )
                    )
                return shared
        except (OSError, ValueError):
            pass
    runners: list[Registration] = []
    page = 1
    while True:
        batch = api(f"repos/{repository}/actions/runners?per_page=100&page={page}")["runners"]
        runners += [
            Registration(
                id=row["id"],
                name=row["name"],
                online=row["status"] == "online",
                busy=row["busy"],
                labels=frozenset(label["name"] for label in row["labels"]),
            )
            for row in batch
        ]
        if len(batch) < 100:
            return runners
        page += 1


def _active(api: Api, path: str) -> list[dict[str, Any]]:
    return [run for run in api(path)["workflow_runs"] if run["status"] != "completed"]


def _jobs_under_way(api: Api, repository: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = api(f"repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    return [job for job in jobs if job["status"] != "completed"]


def main_waiting(api: Api, repository: str, labels: Labels) -> bool:
    """A job of a push to main, or of a dispatched run, is queued or running for main's runners.

    The merge gate dispatches its prefixes' runs, and their jobs ask for main's label: what a
    job asks for decides, not what its branch is called. Only runs under way are listed, so
    finished dispatches never push one off the page."""
    dispatched = f"repos/{repository}/actions/runs?event=workflow_dispatch&per_page=20&status="
    runs = [
        *_active(api, f"repos/{repository}/actions/runs?event=push&branch=main&per_page=10"),
        *_active(api, f"{dispatched}queued"),
        *_active(api, f"{dispatched}in_progress"),
    ]
    return any(
        labels.main in job["labels"]
        for run in runs
        for job in _jobs_under_way(api, repository, run)
    )


class First:
    """The train's first member as the gate wrote it, the runs of it that are under way, and the
    runs this service cancelled to plan again, kept in a file so that a restart reruns them."""

    def __init__(
        self,
        api: Api,
        repository: str,
        train: Path,
        replanned: Path,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api = api
        self.pause = pause
        self.repository = repository
        self.train = train
        self.replanned_path = replanned
        self.branches: dict[str, str] = {}
        try:
            self.replanned: dict[str, dict[str, Any]] = json.loads(replanned.read_text())
        except (OSError, ValueError):
            self.replanned = {}

    def record(self) -> dict[str, Any]:
        try:
            saved = json.loads(self.train.read_text())
            number, batch = saved.get("number"), saved.get("batch")
            if (number is None or str(number).isdigit()) and (batch is None or type(batch) is str):
                return {"number": number and str(number), "batch": batch}
        except (OSError, ValueError, AttributeError):
            pass
        return {"number": None, "batch": None}

    def runs(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        repository = self.repository
        found: list[dict[str, Any]] = []
        if number := record["number"]:
            if number not in self.branches:
                self.branches[number] = self.api(f"repos/{repository}/pulls/{number}")["head"][
                    "ref"
                ]
            found += _active(
                self.api,
                f"repos/{repository}/actions/runs?event=pull_request&per_page=20"
                f"&branch={self.branches[number]}",
            )
        if batch := record["batch"]:
            found += _active(
                self.api,
                f"repos/{repository}/actions/runs?event=workflow_dispatch&per_page=20&branch={batch}",
            )
        return [run for run in found if run["path"] in WORKFLOWS]

    def look(self, record: dict[str, Any], labels: Labels, *, cancel: bool = True) -> bool:
        """Whether a job of the first member waits for the first label. A run whose jobs wait for
        the ordinary label was planned before its pull request became first: it is cancelled
        here and run again by `rerun` once it has stopped."""
        waiting = False
        planned: set[str] = set()
        cancelled: list[str] = []
        for run in self.runs(record):
            all_jobs = self.api(
                f"repos/{self.repository}/actions/runs/{run['id']}/jobs?per_page=100"
            )["jobs"]
            jobs = [job for job in all_jobs if job["status"] == "queued"]
            planning = not all_jobs or all(
                job.get("name") in ("CI: what to run", "E2E: what to run") for job in all_jobs
            )
            key = str(run["id"])
            # A batch's runs are dispatched while its member is first, so their planner saw it;
            # the gate would read a cancelled one as a failed batch.
            cancellable = key not in self.replanned and run["event"] == "pull_request"
            stale = any(
                labels.home in job["labels"] and labels.first not in job["labels"] for job in jobs
            )
            # A job asking for the ordinary label waits for the first one only once its run is
            # planned again; one that will not be (a run already planned again once, or a
            # batch's) must not keep the lent runner from every job it could take.
            waiting |= (
                planning
                or any(labels.first in job["labels"] for job in jobs)
                or (cancellable and stale)
            )
            if not planning and run.get("run_attempt", 1) > self.replanned.get(key, {}).get(
                "attempt", 0
            ):
                planned.add(key)
            if cancel and cancellable and stale:
                # Noted before asking: a cancel whose answer is lost is still run again.
                self.replanned[key] = {
                    "state": "cancelled",
                    "tries": 0,
                    "head": run.get("head_sha"),
                    "number": record["number"],
                    "jobs": [job["id"] for job in all_jobs if "id" in job],
                    "attempt": run.get("run_attempt", 1),
                    "updated_at": time.time(),
                    "sent": False,
                }
                self.save()
                self.api("-X", "POST", f"repos/{self.repository}/actions/runs/{key}/cancel")
                self.replanned[key]["sent"] = True
                cancelled.append(key)
        # Run them again as soon as they have stopped: until then the pull request's checks read
        # as cancelled to the merge gate.
        for _ in range(10 if cancelled else 0):
            for key in cancelled:
                if self.replanned[key]["state"] == "cancelled":
                    # A failure here is not counted: the next rounds try again, counted.
                    with contextlib.suppress(
                        OSError,
                        subprocess.SubprocessError,
                        ValueError,
                        KeyError,
                        TypeError,
                    ):
                        self.replanned[key]["state"] = self._rerun(key)
            self.save()
            if all(self.replanned[key]["state"] != "cancelled" for key in cancelled):
                break
            self.pause(2)
        waiting |= any(
            entry.get("number") == record["number"]
            and record["number"] is not None
            and entry["state"] in ("cancelled", "rerun")
            and 0 <= time.time() - entry.get("updated_at", 0) <= 180
            for key, entry in self.replanned.items()
            if key not in planned
        )
        return waiting

    def rerun(self) -> None:
        """Run again each run this cancelled, once it has stopped. One that cannot be read or run
        again is tried in the next rounds, a few times, without holding up the lending."""
        for key, entry in list(self.replanned.items()):
            if entry["state"] != "cancelled":
                continue
            try:
                entry["state"] = self._rerun(key, resend=True)
            except (
                OSError,
                subprocess.SubprocessError,
                ValueError,
                KeyError,
                TypeError,
            ) as error:
                entry["tries"] += 1
                if entry["tries"] >= RERUN_TRIES:
                    entry["state"] = "given up"
                print(f"Could not run {key} again: {error}", file=sys.stderr, flush=True)
            self.save()
        # Only the newest run ids are still worth remembering.
        for key in sorted(self.replanned, key=int)[:-200]:
            del self.replanned[key]
        self.save()

    def _rerun(self, key: str, *, resend: bool = False) -> str:
        run = self.api(f"repos/{self.repository}/actions/runs/{key}")
        if run["status"] != "completed":
            entry = self.replanned[key]
            if (
                resend
                and not entry.get("sent", True)
                and run.get("run_attempt", 1) == entry.get("attempt", 1)
            ):
                # The cancel failed, or its answer was lost: ask again, counted as a try if
                # this fails too.
                self.api("-X", "POST", f"repos/{self.repository}/actions/runs/{key}/cancel")
                entry["sent"] = True
            return "cancelled"
        if run["conclusion"] != "cancelled":
            # The cancel never took effect: the run went on to its end.
            return "not cancelled"
        # A push after the cancel started a newer run of the same workflow. Running the old one
        # again would test an old head and, in the workflow's concurrency group, cancel the new.
        newer = self.api(
            f"repos/{self.repository}/actions/runs?event={run['event']}&per_page=20"
            f"&branch={run['head_branch']}"
        )["workflow_runs"]
        if any(other["path"] == run["path"] and other["id"] > run["id"] for other in newer):
            return "superseded"
        self.api("-X", "POST", f"repos/{self.repository}/actions/runs/{key}/rerun")
        return "rerun"

    def save(self) -> None:
        _replace(self.replanned_path, self.replanned)


def _replace(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        json.dump(value, output)
    Path(output.name).chmod(mode)
    Path(output.name).replace(path)


def round_once(
    api: Api,
    repository: str,
    labels: Labels,
    most: int,
    first: First,
    published: Path,
    own: str,
    listing: Path | None = None,
) -> None:
    record = first.record()
    first_waiting = first.look(record, labels, cancel=False)
    # Only this machine's main runners: another machine's lender, if any, labels its own.
    runners = [
        runner
        for runner in registrations(api, repository, listing)
        if labels.main not in runner.labels or runner.name.startswith(own)
    ]
    waiting = main_waiting(api, repository, labels)
    changes = lending(runners, labels, main_waiting=waiting, first_waiting=first_waiting, most=most)
    for change, runner, label in changes:
        if change == "add":
            api(
                "-X",
                "POST",
                f"repos/{repository}/actions/runners/{runner}/labels",
                "-f",
                f"labels[]={label}",
            )
        else:
            api(
                "-X",
                "DELETE",
                f"repos/{repository}/actions/runners/{runner}/labels/{label}",
            )
    # A lent runner free for any pull request job keeps a run home, as a free ordinary one does.
    free = sum(
        labels.main in runner.labels
        and runner.online
        and not runner.busy
        and (
            ("add", runner.id, labels.home) in changes
            or (labels.home in runner.labels and ("remove", runner.id, labels.home) not in changes)
        )
        for runner in runners
    )
    first_free = sum(
        labels.main in runner.labels
        and runner.online
        and not runner.busy
        and (
            ("add", runner.id, labels.first) in changes
            or (
                labels.first in runner.labels and ("remove", runner.id, labels.first) not in changes
            )
        )
        for runner in runners
    )
    # The planner reads this; an old copy means nobody labels runners for the first member.
    # Readable by the planner's container, whose user is not this one.
    # Serialize with the gate's identity update. A round that read the old first member
    # must not overwrite a newly published batch just before its planner starts.
    first.train.parent.mkdir(parents=True, exist_ok=True)
    with first.train.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if first.record() != record:
            return
        _replace(
            published,
            {
                **record,
                "free": free,
                "first_free": first_free,
                "updated_at": datetime.now(UTC).isoformat(),
            },
            0o644,
        )
    # The planner and the reserved runner must be ready before a rerun can start.
    first.look(record, labels)
    first.rerun()


def main() -> None:
    repository, home, first_label, main_label = sys.argv[1:5]
    most, seconds, own = int(sys.argv[5]), float(sys.argv[6]), sys.argv[7]
    labels = Labels(home=home, first=first_label, main=main_label)
    state = Path.home() / ".local/state"
    api = RoundReads()
    first = First(
        api,
        repository,
        state / "workflow/train-first.json",
        state / "ci-overflow/replanned.json",
    )
    while True:
        wait("lender", identity=RUNNER)
        api.reads.clear()
        try:
            round_once(
                api,
                repository,
                labels,
                most,
                first,
                state / "ci-overflow/home/first.json",
                own,
                Path(__file__).with_name("runners.tsv"),
            )
        except Limited:
            continue
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            # A failed write may have taken effect: the next round reads the labels again.
            print(f"Lending skipped this round: {error}", file=sys.stderr, flush=True)
        time.sleep(poll_delay(seconds))


if __name__ == "__main__":
    main()
