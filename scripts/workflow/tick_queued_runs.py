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
import train_reads
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


def _free(
    run: dict[str, Any], active: list[dict[str, Any]], since: float, entries: dict[str, Any],
) -> bool:
    for other in active:
        if other["id"] == run["id"] or other.get("status") == "completed":
            continue
        if other.get("status") == "in_progress":
            return False
        stamp = gate._seconds(other.get("created_at"))
        if other.get("run_attempt", 1) > 1:
            jobs = _unstarted(other)
            if jobs is None:
                return False
            stamps = [gate._seconds(job.get("created_at")) for job in jobs]
            stamp = min((value for value in stamps if value is not None), default=None)
        entry = entries.get(str(other["id"]), {})
        observed = entry.get("observed", {}).get(str(other.get("run_attempt", 1)))
        if (stamp is None and entry.get("head") == other.get("head_sha")
            and type(observed) in (int, float) and 0 <= observed <= time.time()):
            stamp = observed
        # Missing queue order or work already executing is no evidence of spare capacity.
        if stamp is None or (stamp, other["id"]) < (since, run["id"]):
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
    entry["requested_at"] = time.time()
    save()  # A restart never repeats a request GitHub accepted but has not reflected yet.
    try:
        gate.command("gh", *args)
    except gate.BudgetWaiting:
        restore()
        raise
    except gate.Refused as error:
        if gate.transient(str(error)):
            if state == "cancel requested":
                entry["state"] = "cancel retry"
                save()
            else:
                restore()
        else:
            entry["state"] = "refused"
            save()
        raise
    finally:
        # A following driver must not reuse a pre-cancellation planning response.
        if gate._planning_reads is not None:
            gate._planning_reads.invalidate()
        elif tick := train_reads.tick():
            train_reads.save(tick / "api.json", {})


def _refresh(folder: Path, number: str, pull: dict[str, Any], run: dict[str, Any], workflow: str) -> None:
    worktree = _worktree(folder, number)
    if not worktree.is_dir():
        raise gate.Refused("the clean pull request worktree is missing")
    before = Path.cwd()
    try:
        os.chdir(worktree)
        if gate.git("branch", "--show-current") != pull["headRefName"]:
            raise gate.Refused("the pull request worktree is on a different branch")
        train = gate.Train(folder)
        pending_path = folder / f"merge-refresh-{number}.json"
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
        if pending is not None:
            note = gate.Handoff.read(folder, number)
            if (not pending.get("queue_recovery")
                or pull["headRefOid"] not in (pending["head"], pending["commit"])
                or pending["branch"] != pull["headRefName"]
                or type(pending["failures"]) is not int or not 0 <= pending["failures"] < 3
                or (note is None and pending["round"] is not None)
                or (note is not None and (note.head != pending["head"]
                    or note.branch != pending["branch"] or note.round != pending["round"]))):
                raise gate.Refused("the saved recovery no longer matches the pull request")
            commit = pending["commit"]
            if (gate.git("rev-parse", "HEAD") != commit or gate.git("status", "--porcelain")
                or gate.git("show", "-s", "--format=%P", commit) != pending["head"]
                or gate.git("rev-parse", f"{commit}^{{tree}}")
                != gate.git("rev-parse", f"{pending['head']}^{{tree}}")):
                raise gate.Refused("the saved recovery worktree is no longer clean at its empty commit")
            remote = gate.git("ls-remote", "origin", f"refs/heads/{pending['branch']}").split()[:1]
            if remote == [commit]:
                # The previous push can succeed even when its response is lost.
                gate._finish_refresh(train, number, pending["head"], rerun=True)
            elif remote == [pending["head"]] and pull["headRefOid"] == pending["head"]:
                gate._push_refreshed(pull, train, number, refresh=pending)
            else:
                raise gate.Refused("the remote branch changed during empty commit recovery")
            return
        gate.rerun_lost(pull, train, number, {
            "check": workflow, "run": run["id"], "conclusion": "success",
        }, base=gate.git("rev-parse", "origin/main"), never_started=True)
    finally:
        os.chdir(before)


def recover(folder: Path, pulls: dict[str, Any]) -> None:
    path = folder / "never-started-runs.json"
    data = json.loads(path.read_text()) if path.exists() else {"runs": {}, "heads": {}}

    def save() -> None:
        write_json(path, data)

    # An accepted push with a lost response can disappear from the candidate scan:
    # its old run no longer belongs to the PR head. Finish its exact saved receipt
    # without pushing again, even if the replacement runs have already started.
    for number, snapshot in pulls.items():
        pending_path = folder / f"merge-refresh-{number}.json"
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
        if not pending or not pending.get("queue_recovery") or snapshot.get("headRefOid") != pending["commit"]:
            continue
        pull = gate.read_pull(str(number))
        if (pull.get("state") != "OPEN" or pull.get("isDraft")
            or pull.get("baseRefName") != "main" or pull.get("headRefName") == "main"
            or pull.get("headRefName") != pending["branch"] or pull.get("headRefOid") != pending["commit"]):
            continue
        owner = next(((key, entry) for key, entry in data["runs"].items()
                      if entry.get("number") == str(number) and entry.get("head") == pending["head"]
                      and entry.get("state") in ("rerun requested", "finished")), None)
        if owner is None:
            continue
        key, entry = owner
        try:
            _refresh(folder, str(number), pull, {"id": key}, "")
        except gate.Refused:
            continue
        data["heads"][f"{number}-{pending['head']}"] = key
        entry["pushed_head"] = pending["commit"]
        entry["pushed_at"] = time.time()
        save()

    queued = gate.api(f"{gate.API}/actions/runs?status=queued&per_page=100", "workflow_runs", fresh=True)
    candidates = {str(run["id"]): run for run in queued if run.get("status") == "queued"}
    # A cancelled run disappears from the queue. Keep watching its claimed first attempt.
    for key, entry in data["runs"].items():
        if entry.get("state") in ("cancel requested", "cancel retry", "rerun requested") and key not in candidates:
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
        entry["head"] = head
        entry["number"] = number
        retry_attempt = entry.get("retry_attempt")
        if (retry_attempt is not None and attempt == retry_attempt + 1
            and entry.get("state") in ("cancel requested", "cancel retry")):
            # A rerun may have been accepted even when its response was lost.
            entry["state"] = "rerun requested"
            save()
        if entry.get("state") in ("cancel requested", "cancel retry") and attempt == retry_attempt:
            if run.get("status") == "completed" and run.get("conclusion") == "cancelled":
                try:
                    _request(entry, save, "rerun requested", "run", "rerun", key, "-R", gate.REPOSITORY)
                    active = None
                except gate.Refused:
                    _notice(folder, number, run, workflow, "rerun", "rerun could not be requested")
                    raise
            elif run.get("status") == "completed":
                entry["state"] = "finished"
                save()
            if entry.get("state") != "cancel retry" or run.get("status") != "queued":
                continue
        if run.get("status") != "queued" or entry.get("state") == "refused":
            awaiting_retry = (
                entry.get("state") == "rerun requested" and attempt == retry_attempt
                and run.get("conclusion") == "cancelled"
            )
            if run.get("status") == "completed" and not awaiting_retry:
                if (run.get("conclusion") == "success" and retry_attempt is not None
                    and attempt == retry_attempt + 1):
                    entry.setdefault("finished_at", time.time())
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
            active = gate.api(
                f"{gate.API}/actions/runs?status=queued&per_page=100", "workflow_runs", fresh=True
            )
            for status in ACTIVE[1:]:
                active.extend(gate.api(
                    f"{gate.API}/actions/runs?status={status}&per_page=100", "workflow_runs", fresh=True
                ))
        if not _free(run, active, since, data["runs"]):
            continue
        if main:
            _notice(folder, number, run, workflow, "stuck", "still waiting; main is never cancelled")
            continue
        # Both cancellation and an empty commit can replace work. Recheck after
        # capacity reads, which may have taken long enough for a job to start.
        current = gate.api(f"{gate.API}/actions/runs/{key}", paginate=False, fresh=True)
        if (current.get("status") != "queued"
            or any(current.get(field) != run.get(field) for field in
                   ("head_sha", "head_branch", "event", "path", "run_attempt"))
            or _unstarted(current) is None):
            continue
        if "state" not in entry or entry["state"] == "cancel retry":
            _notice(folder, number, run, workflow, "stuck", "requesting cancellation and one rerun")
            entry["head"] = head
            entry["retry_attempt"] = attempt
            _request(entry, save, "cancel requested", "api", "-X", "POST",
                     f"{gate.API}/actions/runs/{key}/cancel")
            active = None
        elif (retry_attempt is not None and attempt == retry_attempt + 1
              and entry.get("state") == "rerun requested"):
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
            entry["pushed_head"] = gate.Train(folder).reruns[number]
            entry["pushed_at"] = time.time()
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
