"""Recover CI and browser runs GitHub never started, under the gate's mutation lock."""

import fcntl
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import merge_gate as gate
from merge_driver import _worktree
from merge_handoff import write_json

ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")


def _notice(folder: Path, number: str, run: dict[str, Any], workflow: str, kind: str, text: str) -> None:
    name = f"{run['id']}-{run['run_attempt']}-{kind}.json"
    pending = folder / "never-started.pending"
    if (pending / name).exists() or (folder / "never-started.sent" / name).exists():
        return
    write_json(pending / name, {
        "number": number, "head": run["head_sha"], "run": run["id"],
        "check": f"{workflow} (never started)",
        "line": f"GitHub never started {workflow} of run {run['id']} on "
        f"{'main' if number == 'main' else 'pull request ' + number}; {text}",
    })


def _unstarted(run: dict[str, Any]) -> list[dict[str, Any]] | None:
    jobs = gate.api(f"{gate.API}/actions/runs/{run['id']}/jobs?per_page=100", "jobs", fresh=True)
    # Like scratch recovery, inspect raw matrix parts and only the current attempt.
    if any(
        job.get("run_attempt", run["run_attempt"]) != run["run_attempt"]
        or job.get("status") != "queued"
        or gate._seconds(job.get("started_at")) is not None
        or job.get("runner_id")
        for job in jobs
    ):
        return None
    return jobs


def _free(run: dict[str, Any], active: list[dict[str, Any]]) -> bool:
    since = gate._seconds(run.get("created_at"))
    for other in active:
        if other["id"] == run["id"] or other.get("status") == "completed":
            continue
        stamp = gate._seconds(other.get("created_at"))
        # Missing queue order or work already executing is no evidence of spare capacity.
        if (other.get("status") == "in_progress" or since is None or stamp is None
            or (stamp, other["id"]) < (since, run["id"])):
            return False
    return True


def _request(entry: dict[str, Any], save: Callable[[], None], state: str, *args: str) -> None:
    before = entry.get("state")

    def restore() -> None:
        if before is None:
            entry.pop("state", None)
        else:
            entry["state"] = before
        save()

    entry["state"] = state
    save()  # A restart never repeats a request GitHub accepted but has not reflected yet.
    try:
        gate.command("gh", *args)
    except gate.BudgetWaiting:
        restore()
        raise
    except gate.Refused as error:
        if gate.transient(str(error)):
            restore()
        else:
            entry["state"] = "refused"
            save()
        raise


def _refresh(folder: Path, number: str, pull: dict[str, Any], run: dict[str, Any], workflow: str) -> None:
    worktree = _worktree(folder, number)
    if not worktree.is_dir():
        raise gate.Refused("the clean pull request worktree is missing")
    before = Path.cwd()
    try:
        os.chdir(worktree)
        if gate.git("branch", "--show-current") != pull["headRefName"]:
            raise gate.Refused("the pull request worktree is on a different branch")
        gate.rerun_lost(pull, gate.Train(folder), number, {
            "check": workflow, "run": run["id"], "conclusion": "success",
        }, base=gate.git("rev-parse", "origin/main"), never_started=True)
    finally:
        os.chdir(before)


def recover(folder: Path, pulls: dict[str, Any]) -> None:
    path = folder / "never-started-runs.json"
    data = json.loads(path.read_text()) if path.exists() else {"runs": {}, "heads": {}}

    def save() -> None:
        write_json(path, data)

    queued = gate.api(f"{gate.API}/actions/runs?status=queued&per_page=100", "workflow_runs", fresh=True)
    candidates = {str(run["id"]): run for run in queued if run.get("status") == "queued"}
    # A cancelled run disappears from the queue. Keep watching its claimed first attempt.
    for key, entry in data["runs"].items():
        if entry.get("state") in ("cancel requested", "rerun requested") and key not in candidates:
            candidates[key] = gate.read_run(key)
    active = None
    for key, listed in candidates.items():
        workflow = next((name.upper() for name in ("ci", "e2e") if
            listed.get("path", "").split("@")[0] == f".github/workflows/{gate.WORKFLOW_FILES[name]}"), None)
        attempt = listed.get("run_attempt")
        if workflow is None or type(attempt) is not int or attempt < 1:
            continue
        branch, head = listed.get("head_branch"), listed.get("head_sha")
        main = branch == "main" and listed.get("event") != "pull_request"
        number = "main" if main else next((str(number) for number, pull in pulls.items() if
            pull.get("state") == "OPEN" and not pull.get("isDraft")
            and pull.get("baseRefName") == "main" and pull.get("headRefName") == branch
            and pull.get("headRefOid") == head and branch != "main"
            and listed.get("event") == "pull_request"), None)
        if number is None:
            if key in data["runs"]:
                data["runs"][key]["state"] = "finished"
                save()
            continue
        # Re-read before acting: the tick snapshot and queued listing may already be stale.
        run = gate.read_run(key)
        if any(run.get(field) != listed.get(field) for field in
               ("head_sha", "head_branch", "event", "path", "run_attempt")):
            continue
        if not main:
            pull = gate.read_pull(number)
            if (pull.get("state") != "OPEN" or pull.get("isDraft")
                or pull.get("baseRefName") != "main" or pull.get("headRefName") != branch
                or pull.get("headRefOid") != head):
                continue
        entry = data["runs"].setdefault(key, {})
        if entry.get("state") == "cancel requested" and attempt == 1:
            if run.get("status") == "completed" and run.get("conclusion") == "cancelled":
                try:
                    _request(entry, save, "rerun requested", "run", "rerun", key, "-R", gate.REPOSITORY)
                except gate.Refused:
                    _notice(folder, number, run, workflow, "rerun", "rerun could not be requested")
                    raise
            elif run.get("status") == "completed":
                entry["state"] = "finished"
                save()
            continue
        if run.get("status") != "queued" or entry.get("state") == "refused":
            if run.get("status") == "completed":
                entry["state"] = "finished"
                save()
            continue
        jobs = _unstarted(run)
        if jobs is None:
            continue
        now = time.time()
        stamps = [gate._seconds(job.get("created_at")) for job in jobs]
        if attempt == 1:
            stamps.append(gate._seconds(run.get("created_at")))
        stamp = min((stamp for stamp in stamps if stamp is not None and stamp <= now), default=now)
        since = entry.setdefault("observed", {}).setdefault(str(attempt), stamp)
        save()  # Missing timestamps age durably, rather than borrowing attempt one's age.
        if now - since <= gate.CONFIG.merge_train.queued_limit_minutes * 60:
            continue
        if active is None:
            active = list(queued)
            for status in ACTIVE[1:]:
                active.extend(gate.api(
                    f"{gate.API}/actions/runs?status={status}&per_page=100", "workflow_runs", fresh=True
                ))
        if not _free(run, active):
            continue
        if main:
            _notice(folder, number, run, workflow, "stuck", "still waiting; main is never cancelled")
        elif attempt == 1 and "state" not in entry:
            current = gate.read_run(key)
            if (current.get("status") != "queued"
                or any(current.get(field) != run.get(field) for field in
                       ("head_sha", "head_branch", "event", "path", "run_attempt"))
                or _unstarted(current) is None):
                continue
            _notice(folder, number, run, workflow, "stuck", "requesting cancellation and one rerun")
            _request(entry, save, "cancel requested", "api", "-X", "POST",
                     f"{gate.API}/actions/runs/{key}/cancel")
        elif attempt == 2 and entry.get("state") == "rerun requested":
            identity = f"{number}-{head}"
            if identity in data["heads"]:
                _notice(folder, number, run, workflow, "stuck", "not pushed for twice on the same head")
                continue
            try:
                _refresh(folder, number, pull, run, workflow)
            except gate.Refused as error:
                # Push hooks can print paths or credentials. Wake notices name the case,
                # never copy raw command diagnostics into durable records.
                reason = "empty commit recovery refused; the clean worktree or push needs attention"
                if "uncommitted changes" in str(error):
                    reason = "empty commit refused: the pull request worktree has uncommitted changes"
                elif "not pushed for twice" in str(error):
                    reason = "not pushed for twice on the recovery head"
                elif isinstance(error, gate.Waiting):
                    reason = "waiting for the empty commit recovery or its published head"
                _notice(folder, number, run, workflow, "stuck", reason)
                continue
            data["heads"][identity] = key
            save()
            _notice(folder, number, run, workflow, "stuck", "an empty commit was pushed to run CI again")


def main(folder: Path, snapshot: Path) -> None:
    if os.environ.get("SHIPLINE_SHADOW"):
        return
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "merge.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        recover(folder, json.loads(snapshot.read_text()))


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]), Path(sys.argv[2]))
    except (gate.Refused, OSError, ValueError, TypeError, KeyError) as error:
        print(f"cannot recover a never-started run: {error}", file=sys.stderr)
        sys.exit(1)
