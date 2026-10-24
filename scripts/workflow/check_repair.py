"""A handed-off pull request whose checks failed: its own failing test, or the runners'.

A failing test of the pull request's own goes to a worker at medium effort, its sentence naming
the failed job, its run and the first failing test, while the pull request keeps its place in
the merge train as a blocked member. A failure of the runners (a lost or shut-down runner, a
timed-out job) is re-run once under the rules of main's retry. Anything else, a failure that
cannot be read, and a second failing test in a row are judged by a fresh reviewer.
"""

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import workflow_config
from merge_handoff import write_json
from workflow_commands import GIT_READ_TIMEOUT, run

_SHA = re.compile(r"[0-9a-f]{40}")
_COLOUR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# The first failing test, by the first of these that matches anywhere in the diagnostics.
_TESTS = (
    # pytest: "FAILED tests/test_dates.py::test_leap_day - AssertionError", its parameters whole
    # even with a space ("test_totals[two items]")
    re.compile(r"\b(?:FAILED|ERROR) (\S+\.py::(?:\[[^\]\n]*\]|\S)+)"),
    # Vitest: "FAIL src/dates.test.ts > parses a leap day"
    re.compile(r"\bFAIL\s+(\S+\.(?:test|spec)\.[A-Za-z]+(?: > [^\n]+)?)"),
    # Playwright: "1) [chromium] › e2e/plan.spec.ts:12:5 › opens the plan"
    re.compile(r"\b\d+\) ((?:\[[^\]\n]+\] › )?\S+\.spec\.ts:\d+:\d+ › [^\n]+)"),
)
# What GitHub and its runners write when a job failed for them, not for its code.
_INFRASTRUCTURE = re.compile(
    r"runner\b[^\n]*\b(?:lost|disconnect\w*|shutdown|not received|communication)"
    r"|\b(?:lost|unable to contact)\b[^\n]*\brunner"
    r"|\bjob\b[^\n]*\bexceeded\b[^\n]*\b(?:time|maximum execution)"
    r"|Failed to download action"
    # GitHub's, not an HTTP status a server under test answered ("500 Internal Server Error")
    r"|(?<![0-9] )(?<![0-9] \()Internal server error",
    re.IGNORECASE,
)
# A transport failure needs build or registry context. A server under test can use the
# same words; a BuildKit vertex's FROM/metadata/export line supplies context to its error.
_TRANSPORT = re.compile(
    r"\b(?:short read|unexpected EOF|connection reset by peer|i/o timeout|TLS handshake timeout)\b",
    re.IGNORECASE,
)
_BUILD_TRANSPORT = re.compile(
    r"\b(?:registry|pull-through cache|failed to solve|failed to compute cache key)\b"
    r"|\b(?:pull\w*|resolv\w*|export\w*)\b[^\n]*\b(?:layer|manifest|image)\b"
    r"|\b(?:layer|manifest)\b[^\n]*\b(?:pull\w*|resolv\w*|export\w*)\b"
    r"|\bload metadata for\b|#\d+\s+(?:\[[^\]\n]+\]\s+)?(?-i:FROM)\s+\S+",
    re.IGNORECASE,
)
_VERTEX = re.compile(r"(?<!\w)#(\d+)\s+")


NOT_REPORTED = "not reported"
# What a sentence may hold of GitHub's words: the sentence goes inside double quotes in a shell
# command, so no quotation mark, dollar, backtick, backslash or comma (which ends its parts).
_UNSAFE = re.compile(r"[^A-Za-z0-9 _.:/()\[\]+=@#>-]+")
_LONGEST = 200
# How long the note of a run's one re-run is kept.
RERUN_KEPT_SECONDS = 7 * 86400


def safe(text: str) -> str:
    """GitHub's words as a sentence may hold them."""
    # Playwright's arrow between a test's file and its name reads as ">".
    return " ".join(_UNSAFE.sub(" ", text.replace("›", ">")).split())[:_LONGEST].strip()


def first_test(diagnostics: str) -> str | None:
    for pattern in _TESTS:
        found = pattern.search(diagnostics)
        if found:
            return found[1].strip()
    return None


def transport_error(diagnostics: str) -> bool:
    lines = diagnostics.splitlines()
    vertices = {
        vertex[1]
        for line in lines
        if _BUILD_TRANSPORT.search(line) and (vertex := _VERTEX.search(line))
    }
    return any(
        _TRANSPORT.search(line)
        and (
            _BUILD_TRANSPORT.search(line)
            or ((vertex := _VERTEX.search(line)) and vertex[1] in vertices)
        )
        for line in lines
    )


def path(folder: Path, number: str) -> Path:
    return folder / f"check-repair-{number}.json"


@dataclass
class Repair:
    """The failing test a worker was started for, on the head it failed on."""

    head: str
    run: int
    job: str
    test: str
    # The wake's notice of it, once.
    event: str = ""

    @classmethod
    def read(cls, folder: Path, number: str) -> Repair | None:
        try:
            repair = cls(**json.loads(path(folder, number).read_text()))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError) as error:
            raise ValueError(f"invalid check repair note: {error}") from error
        if (
            not isinstance(repair.head, str)
            or not _SHA.fullmatch(repair.head)
            or type(repair.run) is not int
            or not isinstance(repair.job, str)
            or repair.job != safe(repair.job)
            or not repair.job
            or not isinstance(repair.test, str)
            or repair.test != safe(repair.test)
            or not repair.test
            or not isinstance(repair.event, str)
        ):
            raise ValueError("invalid check repair note")
        return repair

    def save(self, folder: Path, number: str) -> None:
        write_json(path(folder, number), asdict(self))


def clear(folder: Path, number: str) -> None:
    path(folder, number).unlink(missing_ok=True)


@dataclass
class Diagnosis:
    """What failed the latest runs of a head.

    `kind` is "own" (a failing test of the pull request's own, named by `repair`),
    "infrastructure" (only the runners failed it; `runs` holds each failed run with its jobs)
    or "judgment" (anything else, a reviewer's to judge; `reason` says why)."""

    kind: str
    reason: str
    repair: Repair | None = None
    runs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = field(default_factory=list)


def _failed_log(gate: Any, run_id: int, job_id: int) -> str:
    done = gate.read_github(
        "run", "view", str(run_id), "-R", gate.REPOSITORY, "--job", str(job_id), "--log-failed"
    )
    if done.returncode:
        error = done.stderr.strip() or done.stdout.strip()
        if gate.transient(error):
            raise gate.Waiting(f"cannot read the log of job {job_id} of run {run_id}: {error}")
        # An expired or missing log is no diagnostics; the annotations may still tell.
        return ""
    return _COLOUR.sub("", done.stdout)


def diagnose(pull: dict[str, Any]) -> Diagnosis:
    """Read the latest CI, image and E2E runs of the pull request's head: each failed job's
    failed log and annotations. Raises Waiting while one of those runs is still running."""
    import merge_gate as gate

    head = pull["headRefOid"]
    failed_runs = []
    for workflow in ("ci", "images", "e2e"):
        latest = gate.runs(workflow, head, pull=True)[:1]
        for found in latest:
            if found.get("status") != "completed":
                raise gate.Waiting(f"run {found['id']} of the failed head is still running")
            if found.get("conclusion") not in ("success", "skipped", "neutral"):
                failed_runs.append(found)
    return diagnose_runs(
        head,
        [
            (found, gate.api(f"{gate.API}/actions/runs/{found['id']}/jobs?per_page=100", "jobs"))
            for found in failed_runs
        ],
    )


def diagnose_runs(
    head: str, failed_runs: list[tuple[dict[str, Any], list[dict[str, Any]]]]
) -> Diagnosis:
    """Classify completed runs, including a prefix or whole suite's scratch runs."""
    import merge_gate as gate

    own: list[tuple[str, Repair]] = []
    # Why a reviewer judges it, unless a job named a failing test of its own.
    judgments: list[str] = []
    infrastructure: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for found, parts in failed_runs:
        if found.get("status") != "completed" or any(
            job.get("status") != "completed" for job in parts
        ):
            raise gate.Waiting(f"run {found['id']} of the failed head is still running")
        failed = [
            job
            for job in parts
            if job.get("conclusion") in ("failure", "timed_out", "cancelled")
            and not job.get("name", "").endswith("result")
        ]
        runners_only = bool(failed)
        runners = False
        cancelled: list[str] = []
        for job in failed:
            notes = gate.api(f"{gate.API}/check-runs/{job['id']}/annotations?per_page=100")
            diagnostics = "\n".join(
                [
                    _failed_log(gate, found["id"], job["id"]),
                    *(f"{note.get('title') or ''} {note.get('message') or ''}" for note in notes),
                ]
            )
            test = first_test(diagnostics)
            if test is None and (
                job["conclusion"] == "timed_out"
                or _INFRASTRUCTURE.search(diagnostics)
                or transport_error(diagnostics)
            ):
                runners = True
                continue
            name = safe(job.get("name", "")) or f"job {job['id']}"
            if job["conclusion"] != "failure" and test is None:
                # A job cancelled beside a failing one (fail-fast) says nothing of its own.
                cancelled.append(f"{name} of run {found['id']} was cancelled")
                continue
            runners_only = False
            if not diagnostics.strip():
                # Nothing to read is no repair to guess.
                return Diagnosis(
                    "judgment", f"{name} of run {found['id']} failed without a log or annotation"
                )
            else:
                repair = Repair(head, found["id"], name, safe(test or "") or NOT_REPORTED)
                own.append((repair.test, repair))
        if cancelled and not runners:
            # Cancelled with no job its runners failed beside it: a reviewer judges it.
            runners_only = False
            judgments.extend(cancelled)
        if runners_only:
            infrastructure.append((found, parts))
    if own:
        # A job that names its failing test goes before one that does not.
        return Diagnosis(
            "own",
            "a test of its own failed",
            next((repair for test, repair in own if test != NOT_REPORTED), own[0][1]),
        )
    if judgments:
        return Diagnosis("judgment", judgments[0])
    if infrastructure and len(infrastructure) == len(failed_runs):
        return Diagnosis("infrastructure", "its runners failed it", runs=infrastructure)
    return Diagnosis("judgment", "no failed job of its latest runs could be judged")


def runner_wait(
    folder: Path, found: dict[str, Any], parts: list[dict[str, Any]]
) -> str | None:
    """A red scratch run waits for completion, then gets the handoff's one runner retry."""
    import merge_gate as gate

    if found.get("status") != "completed" or any(
        job.get("status") != "completed" for job in parts
    ):
        return f"run {found['id']} is still running; waiting before judging its failed jobs"
    try:
        # The verdict groups matrix parts into one check. Diagnostics and retry eligibility
        # need every original job's ID, status and runner labels.
        parts = gate.api(f"{gate.API}/actions/runs/{found['id']}/jobs?per_page=100", "jobs")
        if any(job.get("status") != "completed" for job in parts):
            return f"run {found['id']} is still running; waiting before judging its failed jobs"
        if not marker(folder, found["id"]).exists() and not _retryable(gate, found, parts):
            return None
        diagnosis = diagnose_runs(found["head_sha"], [(found, parts)])
    except gate.Waiting:
        raise
    except gate.Refused, KeyError, TypeError, ValueError, IndexError:
        # Missing or unreadable diagnostics do not authorize an automatic retry.
        return None
    if diagnosis.kind != "infrastructure":
        return None
    return rerun(folder, diagnosis)


def _retryable(gate: Any, found: dict[str, Any], parts: list[dict[str, Any]]) -> bool:
    """Main's retry rules: the run completed at its first attempt, every part completed, its
    failed jobs ran on the repository's own runners, and no planner failed."""
    plans = {gate.CI_PLAN, gate.E2E_PLAN, gate.IMAGES_PLAN}
    return gate._rerunnable(found, parts, first=True) and not any(
        job.get("name") in plans for job in gate._failed_jobs(parts)
    )


_REQUESTED = "the re-run of run {run} is requested; GitHub has not shown it yet"


def marker(folder: Path, run_id: int) -> Path:
    return folder / f"check-rerun-{run_id}.json"


def _state(claim: Path) -> str:
    try:
        return str(json.loads(claim.read_text()).get("state"))
    except (OSError, ValueError, AttributeError):
        # Cut short while it was claimed.
        return "claim"


def repeated(folder: Path, run_id: int) -> bool:
    """A scratch failure's hand-back must tell the wake that its runner retry failed too."""
    return _state(marker(folder, run_id)) == "repeated"


def rerun_running(folder: Path, pull: dict[str, Any]) -> str | None:
    """Why a handoff whose checks no longer show a failure waits: a re-run the tick asked for
    still runs, or passed and is the line's to judge. None when no such re-run is known."""
    import merge_gate as gate

    for workflow in ("ci", "images", "e2e"):
        for found in gate.runs(workflow, pull["headRefOid"], pull=True)[:1]:
            if marker(folder, found["id"]).exists() and found.get("conclusion") != "failure":
                return f"the re-run of run {found['id']} is running"
    return None


def rerun(folder: Path, diagnosis: Diagnosis) -> str | None:
    """Re-run the failed jobs of each run the runners failed, once: why the handoff waits for
    it, or None when it may not be re-run, or its re-run failed too."""
    import merge_gate as gate

    waits = []
    for found, parts in diagnosis.runs:
        claim = marker(folder, found["id"])
        if claim.exists():
            if _state(claim) != "refused" and found.get("run_attempt") == 1:
                # Sent, or perhaps sent when interrupted: never asked for twice.
                waits.append(_REQUESTED.format(run=found["id"]))
                continue
            # Its one re-run failed again, or was refused: a reviewer judges it.
            if _state(claim) != "refused":
                diagnosis.reason = f"its runners failed it again in run {found['id']}"
                if _state(claim) != "repeated":
                    gate.wake_log(diagnosis.reason)
                    write_json(claim, {"state": "repeated"})
            return None
        if not _retryable(gate, found, parts):
            return None
    for found, _ in diagnosis.runs:
        claim = marker(folder, found["id"])
        if claim.exists():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        # Claimed before GitHub is asked: an interrupted request is never sent twice.
        try:
            with claim.open("x") as output:
                json.dump({"state": "claim"}, output)
        except FileExistsError:
            waits.append(_REQUESTED.format(run=found["id"]))
            continue
        try:
            done = gate.command(
                "gh",
                "run",
                "rerun",
                str(found["id"]),
                "-R",
                gate.REPOSITORY,
                "--failed",
                check=False,
            )
        except gate.BudgetWaiting:
            # Nothing was sent: the run stays eligible.
            claim.unlink()
            raise
        if done.returncode:
            error = done.stderr.strip() or done.stdout.strip()
            if "this workflow is already running" in error.lower():
                # GitHub definitely refused the request: no retry was consumed. Its run
                # list can lag behind the other jobs still running in this attempt.
                claim.unlink()
                waits.append(f"run {found['id']} is still running; waiting to re-run failed jobs")
                continue
            write_json(claim, {"state": "refused", "error": error})
            gate.wake_log(f"automatic retry of run {found['id']} refused: {error}")
            return None
        write_json(claim, {"state": "requested"})
        note = f"automatically re-ran failed jobs of run {found['id']} once: its runners failed it"
        gate.wake_log(note)
        waits.append(note)
    return "; ".join(waits)


def _holds(folder: Path, number: str, repair: Repair) -> bool:
    """The kept worktree is still at the head that failed: no fix has been made yet."""
    root = workflow_config.current().checkout_of(folder) / ".claude/worktrees" / f"ticket-{number}"
    done = run("git", "-C", str(root), "rev-parse", "HEAD", timeout=GIT_READ_TIMEOUT)
    return done.returncode == 0 and done.stdout.strip() == repair.head


def sentence(folder: Path, number: str) -> str:
    """The sentence a worker of pull request N is started with."""
    try:
        repair = Repair.read(folder, number)
    except ValueError:
        repair = None
    # A note outlives its fix. A later start (after a send-back) finds the worktree moved on and
    # gets the ordinary sentence.
    if repair is not None and _holds(folder, number, repair):
        return (
            f"Fix the failed check of pull request {number}: job {repair.job} of run "
            f"{repair.run}, first failing test {repair.test}"
        )
    return f"Implement pull request {number}"


def main() -> None:
    action, folder, number = sys.argv[1:4]
    if action == "sentence":
        sys.stdout.write(sentence(Path(folder), number) + "\n")


if __name__ == "__main__":
    main()
