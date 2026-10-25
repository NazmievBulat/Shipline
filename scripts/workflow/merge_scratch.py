"""Scratch runs and cumulative merge prefixes."""

import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

import check_repair
import train_reads

from merge_health import Health
from merge_train import Train


def _batch_ready(pull: dict[str, Any], base: str, *, settling: bool = False) -> bool:
    # Right after an earlier member's squash moves main, GitHub reports UNKNOWN until it
    # recomputes. The caller then proves a clean merge with its own prefix-tree check.
    from merge_gate import (
        CONFIG,
        _passing_rollup,
        command,
        conflicting,
        git,
        images_needed,
        jobs,
        ready,
        runs,
        verdict,
    )

    mergeable = ("MERGEABLE", "UNKNOWN") if settling else ("MERGEABLE",)
    if (
        not ready(pull)
        or conflicting(pull)
        or pull["mergeable"] not in mergeable
        or not _passing_rollup(pull)
    ):
        return False
    head = pull["headRefOid"]
    if command("git", "cat-file", "-e", head, check=False).returncode:
        git("fetch", "origin", head)
    ci = runs("ci", head, pull=True)
    parts = jobs(ci[0]) if ci else []
    if not ci or verdict(ci[0], parts)[0] != "green":
        return False
    # The older-version job also passes when no migration changed. Only an actual
    # compatibility check depends on this member's own base; keep it on the single
    # path instead of comparing the old application of a combined prefix.
    if any(
        job["name"] == "CI: older version on new tables" and job.get("conclusion") != "skipped"
        for job in parts
    ) and git(
        "diff", "--name-only", f"{base}...{head}", "--",
        "migrations/versions",
    ):
        return False
    if "images" in CONFIG.checks.prefix_only:
        return True
    images = runs("images", head, pull=True)
    if images_needed(head, base, pull["statusCheckRollup"]) and not images:
        return False
    return not images or verdict(images[0], jobs(images[0]), images=True)[0] == "green"


def _batch_runs(batch: dict[str, Any], workflow: str) -> list[dict[str, Any]]:
    from merge_gate import API, WORKFLOW_FILES, api

    return sorted(
        (
            run
            for run in api(
                f"{API}/actions/workflows/{WORKFLOW_FILES[workflow]}/runs?head_sha={batch['head']}"
                "&event=workflow_dispatch&per_page=100",
                "workflow_runs",
            )
            if run["head_sha"] == batch["head"]
            and run["head_branch"] == batch["branch"]
            and run["event"] == "workflow_dispatch"
        ),
        key=lambda run: run["id"],
        reverse=True,
    )[:1]


def _suite_workflows(suite: dict[str, Any]) -> list[str]:
    return suite.get("workflows", ["e2e"])


def _suite_state(suite: dict[str, Any]) -> str | None:
    """How a tested merge's dispatched whole suites stand: None before they were requested."""
    from merge_gate import (
        _batch_runs,
        _queue_recovery_pending,
        _suite_workflows,
        jobs,
        workflow_verdict,
    )

    if not suite["requested"]:
        return None
    states = []
    for workflow in _suite_workflows(suite):
        found = _batch_runs(suite, workflow)
        states.append(
            workflow_verdict(workflow, found[0], jobs(found[0]))[0]
            if found and not _queue_recovery_pending(suite, found[0]) else "pending"
        )
    return "red" if "red" in states else "pending" if "pending" in states else "green"


def _queue_recovery_pending(record: dict[str, Any], run: dict[str, Any]) -> bool:
    entry = record.get("queued_retries", {}).get(str(run["id"]), {})
    return (
        entry.get("retry_attempt") == run.get("run_attempt") == 1
        and entry.get("state") in ("cancel requested", "rerun requested", "rerun refused")
        and (run.get("status") != "completed" or run.get("conclusion") == "cancelled")
    )


def _queue_warning(folder: Path, run: dict[str, Any], line: str) -> None:
    from merge_gate import wake_log

    name = f"{run['id']}-{run['run_attempt']}.json"
    pending = folder / "prefix-queues.pending"
    if (pending / name).exists() or (folder / "prefix-queues.sent" / name).exists():
        return
    pending.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=pending, delete=False) as output:
        json.dump({"line": line}, output)
    os.replace(output.name, pending / name)
    wake_log(line)


def _queued_prefix(
    train: Train, record: dict[str, Any], workflow: str, run: dict[str, Any], *, quiet: bool
) -> None:
    """Recover only scratch test jobs in the batch lane. Claims live in the saved train,
    before each mutation, so a restart can finish cancellation without repeating a retry."""
    from merge_gate import (
        API,
        BudgetWaiting,
        CONFIG,
        REPOSITORY,
        Refused,
        SCRATCH_PREFIX,
        Waiting,
        _cancel,
        _planning_reads,
        _queue_warning,
        _seconds,
        api,
        command,
        transient,
    )

    label = CONFIG.runner.label
    attempt = run.get("run_attempt")
    if (
        workflow not in ("ci", "e2e")
        or not label
        or not record["branch"].startswith(SCRATCH_PREFIX)
        or run.get("head_branch") != record["branch"]
        or run.get("head_sha") != record["head"]
        or run.get("event") != "workflow_dispatch"
        or type(attempt) is not int
        or attempt < 1
    ):
        return
    key = str(run["id"])
    entry = record.get("queued_retries", {}).get(key, {})
    folder = train.path.parent
    if entry.get("retry_attempt") == attempt == 1:
        if entry["state"] == "cancel requested":
            if run["status"] != "completed":
                try:
                    _cancel([run], folder, f"scratch queue recovery on {record['branch']}")
                finally:
                    if _planning_reads is not None:
                        _planning_reads.invalidate()
                return
            if run.get("conclusion") == "cancelled":
                # Claim first: an interrupted request is not sent again. One lost to a GitHub
                # outage or a stall is: only a fresh read of this cancelled first attempt leads
                # back here, and a retry GitHub took has made attempt 2.
                entry["state"] = "rerun requested"
                train.save()
                try:
                    done = command("gh", "run", "rerun", key, "-R", REPOSITORY, check=False)
                    error = (done.stderr.strip() or done.stdout.strip()) if done.returncode else ""
                except BudgetWaiting:
                    entry["state"] = "cancel requested"
                    train.save()
                    raise
                except Refused as failure:
                    error = str(failure)
                finally:
                    if _planning_reads is not None:
                        _planning_reads.invalidate()
                if error and transient(error):
                    entry["state"] = "cancel requested"
                    train.save()
                    raise Waiting(f"could not re-run scratch run {key} yet: {error}")
                if error:
                    entry["state"] = "rerun refused"
                    train.save()
                    _queue_warning(folder, run, f"scratch run {key} queue recovery rerun refused")
                else:
                    # Attempt 2 is the same run: a dropped prefix must still be able to cancel it.
                    (folder / f"cancel-{key}").unlink(missing_ok=True)
                return
            # Cancellation lost a race to the run finishing: judge its real result.
        elif entry["state"] in ("rerun requested", "rerun refused"):
            return
    if run.get("status") == "completed":
        return
    # Read raw matrix jobs: checks() merges parts and would hide a queued shard's timestamp.
    parts = api(f"{API}/actions/runs/{key}/jobs?per_page=100", "jobs")
    queued = [
        job for job in parts
        if job.get("status") == "queued"
        and job.get("run_attempt", attempt) == attempt
        and (
            f"{label}-main-batch" in (job.get("labels") or [])
            or (entry.get("retry_attempt") == 1 and label in (job.get("labels") or []))
        )
    ]
    if not queued:
        return
    before = json.dumps(entry, sort_keys=True)
    record.setdefault("queued_retries", {})[key] = entry
    observed = entry.setdefault("jobs", {})
    now = time.time()
    ages = []
    for job in queued:
        identity = f"{attempt}:{job['id']}"
        stamp = _seconds(job.get("created_at"))
        # No timestamp is no evidence of an old wait. Persist the first observation instead.
        since = observed.setdefault(identity, stamp if stamp is not None and stamp <= now else now)
        ages.append(now - since)
    limit = CONFIG.merge_train.queued_limit_minutes
    if max(ages) > limit * 60:
        if attempt == 1 and "retry_attempt" not in entry:
            entry.update(state="cancel requested", retry_attempt=1)
            train.save()
            try:
                _cancel([run], folder, f"scratch queue recovery on {record['branch']}")
            finally:
                if _planning_reads is not None:
                    _planning_reads.invalidate()
        elif attempt > 1:
            line = (
                f"scratch run {key} attempt {attempt} has a job queued past {limit} minutes; "
                "still waiting"
            )
            entry["warning"] = line
            _queue_warning(folder, run, line)
            if not quiet and attempt not in entry.setdefault("output_attempts", []):
                entry["output_attempts"].append(attempt)
                train.save()
                print(line, flush=True)
    if json.dumps(entry, sort_keys=True) != before:
        train.save()


def _batch_signal(train: Train) -> None:
    """Let the tick wake the front again when its scratch checks finish, on unchanged PR heads.

    Every prefix's runs are noted for --train-status; only the first member's, whose runs decide
    its turn, change the turn."""
    from merge_gate import (
        _batch_runs,
        _batch_service_answered,
        _queued_prefix,
        _suite_workflows,
        prefix_image_source,
        image_source_runs,
    )

    members = train.prefixes["members"] if train.prefixes else []
    scratch = [
        (member["scratch"], member["number"] == train.first)
        for member in members
        if member["scratch"]
    ]
    if train.first in train.suites:
        scratch.append((train.suites[train.first], True))
    noted = changed = False
    for record, first in scratch:
        found = [
            (workflow, run)
            for workflow in _suite_workflows(record)
            for run in _batch_runs(record, workflow)
        ]
        member = next((m for m in members if m["scratch"] is record), None)
        source = prefix_image_source(member, train) if member else None
        if source:
            record["image_source"] = source
            found += [("images", run) for run in image_source_runs(source)]
        for workflow, run in found:
            if not (source and workflow == "images"):
                _queued_prefix(train, record, workflow, run, quiet=True)
        signal = [
            [
                workflow,
                run["id"],
                run.get("run_attempt", 1),
                run["status"],
                run.get("conclusion"),
            ]
            for workflow, run in found
        ]
        if _batch_service_answered(record, train.path.parent):
            # Its red run does not change when the service answers again: only this does.
            signal.append(["advisory service answers"])
        if signal != record.get("signal"):
            record["signal"] = signal
            noted = True
            changed |= first
    if changed:
        train.turn += 1
    if noted:
        train.save()


def _batch_service_answered(batch: dict[str, Any], folder: Path) -> bool:
    """Whether a batch, or a whole suite's run, whose CI waits for an advisory service may
    re-run now: the service answers again, or its re-run was already sent (kept true, so that
    sending it is no wake)."""
    from merge_gate import (
        Refused,
        _batch_runs,
        _suite_workflows,
        jobs,
        service_answers,
        unreached_service,
    )

    if "ci" not in _suite_workflows(batch):
        return False
    for run in _batch_runs(batch, "ci"):
        if run.get("status") != "completed" or run.get("conclusion") != "failure":
            return False
        if any(folder.glob(f"batch-rerun-{run['id']}-*")):
            return True
        try:
            service = unreached_service(jobs(run))
        except Refused:
            # Unread now is read on a later tick; the gate call decides the wait itself.
            return False
        return service is not None and service_answers(service)
    return False


def _batch_rerun_green(failure: dict[str, Any]) -> bool:
    from merge_gate import _batch_runs, _run_token, jobs, workflow_verdict

    batch = failure["batch"]
    retried = _batch_runs(batch, failure["workflow"])
    if not retried or _run_token(retried[0]) == failure["run"]:
        return False
    return all(
        found and workflow_verdict(workflow, found[0], jobs(found[0]))[0] == "green"
        for workflow in batch["workflows"]
        for found in [_batch_runs(batch, workflow)]
    )


def _batch_wait(message: str) -> int:
    from merge_gate import HERE, command

    print(message, flush=True)
    command("bash", str(HERE / "notify"), "waiting")
    return 3


def _retest(train: Train, tree: str, reason: str) -> NoReturn:
    """A scratch run that fell short without failing waits for a retest on a new scratch branch.

    The caller has dropped the scratch record, so the next call tests the same merge again and
    the hand-offs stay. The same shortfall twice in a row on one tree is a real fault: a refusal.
    """
    from merge_gate import Refused, Waiting

    if train.retest == {"tree": tree, "reason": reason}:
        raise Refused(f"{reason}, twice in a row on the same tested files")
    train.retest = {"tree": tree, "reason": reason}
    # A reviewer waiting in the line is woken once per turn; this one needs its next call.
    train.turn += 1
    train.save()
    raise Waiting(f"{reason}; retesting next call")


def _batch_report(train: Train, merged: list[str], acting: str, message: str) -> int:
    others = [number for number in merged if number != acting]
    print(
        f"merge train: pull request {acting} merged in a batch "
        f"(merged in this call: {', '.join(merged)}); {message}",
        flush=True,
    )
    if others:
        print(
            "the tick tells the sessions of "
            + ", ".join(others)
            + " that their pull requests merged, and they do their own after-merge duties; "
            f"do the after-merge duties of {acting} only",
            flush=True,
        )
    if train.queue:
        print("still in the line: " + ", ".join(train.line()), flush=True)
    return 0


def _sweep_scratch(folder: Path) -> None:
    """Delete scratch branches that no saved batch, isolated failure awaiting a rerun or whole
    browser suite run names, each once its runs still queued or running are cancelled. A branch
    whose runs cannot be read for now, or whose cancel is lost to an outage, stops the sweep and
    is kept, with those after it, for the next; one that keeps failing is kept and passed over.

    GitHub lists a dispatched run only some time after it accepted the dispatch: the runs of a
    dropped branch are looked for, and cancelled, until one of each workflow dispatched on it
    has been seen, or DROPPED_RUNS_SECONDS after the drop, when the gate gives up and says so."""
    from merge_gate import (
        DROPPED_RUNS_SECONDS,
        Refused,
        SCRATCH_PREFIX,
        Train,
        Waiting,
        _cancel,
        _scratch_runs,
        _seen,
        git,
        push,
        wake_log,
    )

    try:
        train = Train(folder)
        before = json.dumps(train.dropped, sort_keys=True)
        kept = set(train.scratch_records())
        listed = git("ls-remote", "origin", f"refs/heads/{SCRATCH_PREFIX}*")
        # `ls-remote` also lists a branch that only ends like one of the gate's: only a branch
        # of the gate's namespace is ever deleted.
        branches = [
            branch
            for line in listed.splitlines()
            if (branch := line.split()[1].removeprefix("refs/heads/")).startswith(SCRATCH_PREFIX)
        ]
    except (Refused, OSError, KeyError, IndexError, TypeError, ValueError) as error:
        print(f"could not delete old batch branches: {error}", flush=True)
        return
    watched = {entry["branch"]: entry for entry in train.dropped}
    try:
        for branch in branches:
            if branch in kept:
                continue
            try:
                # A dropped prefix's runs would hold runners ahead of the live prefixes' runs.
                found = _scratch_runs(branch)
                _cancel(found, folder, f"scratch branch {branch}", report=True)
                if branch in watched:
                    _seen(watched[branch], found)
                deleted = push("origin", "--delete", f"refs/heads/{branch}")
                if deleted.returncode:
                    raise Refused(deleted.refusal)
            except Waiting:
                raise
            except Refused as error:
                print(f"could not delete old batch branch {branch}: {error}", flush=True)
        for entry in list(train.dropped):
            branch = entry["branch"]
            try:
                if branch in kept:
                    train.dropped.remove(entry)
                    continue
                if branch not in branches:
                    # Past the deadline too: one last look before giving up.
                    found = _scratch_runs(branch)
                    _cancel(found, folder, f"scratch branch {branch}", report=True)
                    _seen(entry, found)
                missing = sorted(set(entry["requested"]) - set(entry.get("seen", [])))
                if not missing:
                    train.dropped.remove(entry)
                elif time.time() - entry["dropped"] > DROPPED_RUNS_SECONDS:
                    wake_log(
                        f"gave up looking for the {', '.join(missing)} run of dropped scratch "
                        f"branch {branch}: GitHub listed none within "
                        f"{DROPPED_RUNS_SECONDS // 60} minutes"
                    )
                    train.dropped.remove(entry)
            except Waiting:
                raise
            except Refused as error:
                print(f"could not look for the runs of {branch}: {error}", flush=True)
    except (Refused, OSError, KeyError, IndexError, TypeError, ValueError) as error:
        print(f"could not delete old batch branches: {error}", flush=True)
    finally:
        if json.dumps(train.dropped, sort_keys=True) != before:
            try:
                train.save()
            except OSError as error:
                print(f"could not save the dropped scratch branches: {error}", flush=True)


def _seen(entry: dict[str, Any], found: list[dict[str, Any]]) -> None:
    """Note in a dropped branch's entry each dispatched workflow a run of which GitHub listed:
    each was cancelled, or had finished."""
    from merge_gate import WORKFLOW_FILES

    files = {
        WORKFLOW_FILES[workflow]: workflow
        for workflow in entry["requested"]
        if workflow in WORKFLOW_FILES
    }
    seen = set(entry.get("seen", []))
    seen |= {
        files[name]
        for run in found
        if run.get("event") == "workflow_dispatch"
        and (name := str(run.get("path", "")).rsplit("/", 1)[-1]) in files
    }
    entry["seen"] = sorted(seen)


def _scratch_runs(branch: str) -> list[dict[str, Any]]:
    """Every run of a scratch branch the gate made, across every workflow, read fresh from GitHub.

    The namespace includes branches from older saved state; branches outside it, and a pull
    request's own run, are never the gate's to cancel."""
    from merge_gate import API, SCRATCH_PREFIX, _api

    if not branch.startswith(SCRATCH_PREFIX):
        return []
    return [
        run
        for run in _api(
            f"{API}/actions/runs?branch={quote(branch, safe='/')}&per_page=100", "workflow_runs"
        )
        if run.get("head_branch") == branch and run.get("event") != "pull_request"
    ]


@contextmanager
def _sweeping(folder: Path) -> Generator[None]:
    from merge_gate import _sweep_scratch

    try:
        yield
    finally:
        _sweep_scratch(folder)


def _push_scratch(
    train: Train, record: dict[str, Any], whose: str, drop: Callable[[], None]
) -> None:
    """Push a batch's, or a whole suite's, test merge to its scratch branch once."""
    from merge_gate import BATCH_PUSH_ATTEMPTS, Refused, Waiting, push, short_of_memory

    if record["pushed"]:
        return
    failure: str | None = None
    output = ""
    try:
        pushed = push("origin", f"{record['head']}:refs/heads/{record['branch']}")
        if pushed.returncode:
            failure = pushed.refusal
            # A push that keeps stalling ends the batch, whatever its scan printed first.
            output = "" if pushed.timed_out else pushed.output
    except Waiting:
        raise
    except Refused as error:
        failure = str(error)
    if failure is not None:
        reason = f"could not push {whose} scratch branch: " + failure
        # The locked memory returns when another session's scan ends, so a shortage
        # never ends the batch; a hook refusing for another reason, or a stall, does.
        if not short_of_memory(output):
            record["push_failures"] = record.get("push_failures", 0) + 1
            if record["push_failures"] >= BATCH_PUSH_ATTEMPTS:
                drop()
                raise Refused(f"{reason} ({BATCH_PUSH_ATTEMPTS} calls in a row)")
        train.save()
        # The members are unchanged and still approved: the batch is pushed next call.
        raise Waiting(reason)
    record["pushed"] = True
    train.save()


def _dispatch_scratch(train: Train, record: dict[str, Any], workflow: str) -> None:
    from merge_gate import REPOSITORY, Refused, WORKFLOW_FILES, _batch_runs, command

    if workflow in record["requested"]:
        return
    if _batch_runs(record, workflow):
        record["requested"].append(workflow)
        train.save()
        return
    # Save only an accepted dispatch. If interrupted around the request, the
    # lookup above recovers an observable run; a never-sent request can retry.
    # Until GitHub answers, the workflow is noted as being dispatched: dropped meanwhile, the
    # record's branch is still watched for its run (_sweep_scratch).
    record["dispatching"] = sorted({*record.get("dispatching", []), workflow})
    train.save()
    dispatched = command(
        "gh",
        "workflow",
        "run",
        WORKFLOW_FILES[workflow],
        "-R",
        REPOSITORY,
        "--ref",
        record["branch"],
        check=False,
    )
    record["dispatching"].remove(workflow)
    if dispatched.returncode:
        train.save()
        raise Refused(f"batch {workflow} dispatch failed: {dispatched.stderr.strip()}")
    record["requested"].append(workflow)
    train.save()


def _proves_on(scratch: dict[str, Any], base: str) -> bool:
    """Whether a green rerun of a prefix tested its member on what main is now: on the tree the
    prefix put it on. A batch saved before prefixes names the main it was tested on instead."""
    from merge_gate import command, git

    if "under" not in scratch:
        return scratch.get("base") == base
    if command("git", "cat-file", "-e", base, check=False).returncode:
        git("fetch", "origin", "main")
    return git("rev-parse", f"{base}^{{tree}}") == scratch["under"]


def _prefix_scratch(
    commit: str, base: str, under: str, image_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A prefix's scratch branch, before it is pushed: `commit` is main and the members ahead,
    merged with this member; `under`, the tree it was put on."""
    from merge_gate import (
        SCRATCH_PREFIX, WORKFLOW_FILES, Refused, Waiting, browser_needed, git,
        image_difference, main_image_source,
    )

    workflows = ["ci"]
    source = None
    path = f".github/workflows/{WORKFLOW_FILES['images']}"
    if git("ls-tree", "--name-only", commit, "--", path):
        if not image_difference(under, commit)[1]:
            source = image_source or main_image_source(base)
            if source:
                try:
                    if image_difference(source["tree"], commit)[1]:
                        source = None
                except Waiting:
                    raise
                except Refused:
                    source = None
        if source is None:
            workflows.append("images")
    # Each member's own E2E run may have been a selection; a prefix runs the whole suite.
    if browser_needed(base, commit):
        workflows.append("e2e")
    return {
        "head": commit,
        "under": under,
        "branch": f"{SCRATCH_PREFIX}{uuid.uuid4().hex}",
        "workflows": workflows,
        "requested": [],
        "pushed": False,
        **({"image_source": source} if source else {}),
    }


def _extend_line(train: Train, base: str, fresh: bool) -> None:
    """Add the next members of the line to the prefixes, up to the window, each merged onto the
    prefix ahead of it.

    With `fresh`, the front's own CI tested its merge with this main: its prefix needs no run,
    and it merges by its own checks on the single path. A front whose failed prefix passed when
    run again on this very main has that run as its prefix. A new line starts only where it tests
    a member that would otherwise wait: two members, a stale front while the window is one, or
    that passed rerun.
    """
    from merge_gate import (
        _batch_ready,
        _merge_tree,
        _prefix_scratch,
        _restore_prefixes,
        _taken_migrations,
        git,
        read_pull,
    )

    record = train.prefixes or {"base": base, "merged": 0, "members": []}
    members = record["members"]
    if members:
        _restore_prefixes(train, record, base)
        if train.restored and any(member.get("kept") for member in members):
            # Only work saved at restoration may go first. New prefixes wait until it
            # drains, then include the restored front in their dependencies.
            return
    first = train.front()
    starting = not members
    if (
        first is None
        or first in train.blocked
        or len(members) >= train.window
        or (starting and len(train.queue) < 2 and first not in train.proven)
    ):
        return
    line = train.line()
    current = members[-1]["commit"] if members else base
    added: list[dict[str, Any]] = []
    reused: dict[str, Any] | None = None
    for number in line[line.index(members[-1]["number"] if members else first) + bool(members) :]:
        if len(members) + len(added) >= train.window:
            break
        if any(member["number"] == number for member in members):
            continue
        if (
            number in train.blocked
            or number in train.suspended
            or number in train.conflicts
            or number in train.unavailable
            or number in train.pending
        ):
            continue
        pull = read_pull(number)
        head = pull["headRefOid"]
        if train.approved.get(number) != head:
            # An old queue entry or an unreviewed new head cannot authorize a squash.
            break
        if not _batch_ready(pull, base, settling=bool(record["merged"])):
            break
        tree = _merge_tree(current, head)
        if tree is not None and _taken_migrations(head, current):
            # Its own refresh renumbers a migration whose number main or an earlier member
            # took; merged here as it is, its prefix run would only find two migration heads.
            tree = None
        if tree is None:
            # A conflict with a member ahead keeps this pull request's place for its own turn.
            break
        under = git("rev-parse", f"{current}^{{tree}}")
        proven = train.proven.get(number)
        if (
            starting
            and not added
            and proven
            and proven["member"] == head
            and proven["under"] == under
            and git("rev-parse", f"{proven['head']}^{{tree}}") == tree
        ):
            # Its failed prefix passed when it was run again, on this very main.
            reused = proven
            current = proven["head"]
            scratch: dict[str, Any] | None = {
                key: value for key, value in proven.items() if key != "member"
            }
        else:
            current = git(
                "commit-tree",
                tree,
                "-p",
                current,
                "-p",
                head,
                "-m",
                f"chore: test merge train pull request {number}",
            )
            own = fresh and starting and not added and number == first
            previous = (members + added)[-1] if members or added else None
            source = None
            if previous and previous["scratch"]:
                source = (
                    {"number": previous["number"], "tree": previous["tree"],
                     "scratch": previous["scratch"]}
                    if "images" in previous["scratch"]["workflows"]
                    else previous["scratch"].get("image_source")
                )
            scratch = None if own else _prefix_scratch(current, base, under, source)
        added.append(
            {
                "number": number,
                "head": head,
                "tree": tree,
                "commit": current,
                "scratch": scratch,
                "ahead": [
                    *(members[-1]["ahead"] + [members[-1]["number"]] if members else []),
                    *(member["number"] for member in added),
                ],
            }
        )
    if starting and not (
        added
        and added[0]["number"] == first
        and (
            len(added) >= 2
            or (train.window == 1 and added[0]["scratch"])
            or reused is not None
        )
    ):
        return
    if added:
        if reused is not None:
            train.proven.pop(added[0]["number"], None)
        members.extend(added)
        train.prefixes = record
        train.save()


def _restore_prefixes(train: Train, record: dict[str, Any], base: str) -> None:
    """A restored member tests on main alone; prefixes already built there keep their work.

    Retained work may already fill the window. It is never evicted to make room for the
    restored member's own prefix; new branches start only when the window has room.
    """
    from merge_gate import (
        _batch_ready,
        _merge_tree,
        _prefix_scratch,
        _taken_migrations,
        git,
        read_pull,
    )

    members = record["members"]
    line = train.line()
    for number in line:
        if number not in train.restored or any(m["number"] == number for m in members):
            continue
        if not any(
            line.index(number) < line.index(m["number"])
            for m in members
            if m["number"] not in train.restored
        ):
            continue
        if (
            number in train.blocked
            or number in train.suspended
            or number in train.conflicts
            or number in train.unavailable
            or number in train.pending
        ):
            continue
        pull = read_pull(number)
        head = pull["headRefOid"]
        if train.approved.get(number) != head or not _batch_ready(pull, base):
            continue
        tree = _merge_tree(base, head)
        if tree is None or _taken_migrations(head, base):
            continue
        under = git("rev-parse", f"{base}^{{tree}}")
        proven = train.proven.get(number)
        if (
            proven
            and proven["member"] == head
            and proven["under"] == under
            and git("rev-parse", f"{proven['head']}^{{tree}}") == tree
        ):
            scratch = {key: value for key, value in proven.items() if key != "member"}
            commit = proven["head"]
            train.proven.pop(number)
        else:
            commit = git(
                "commit-tree", tree, "-p", base, "-p", head, "-m",
                f"chore: test merge train pull request {number}",
            )
            scratch = _prefix_scratch(commit, base, under)
        for member in members:
            member["kept"] = True
        members.append(
            {"number": number, "head": head, "tree": tree, "commit": commit,
             "scratch": scratch, "ahead": []}
        )
        members.sort(key=lambda member: line.index(member["number"]))
        train.save()


def _drop_prefix(train: Train, number: str) -> None:
    """Forget this member and prefixes containing it, retaining independent work."""
    if train.prefixes:
        members = train.prefixes["members"]
        members[:] = [m for m in members if m["number"] != number and number not in m["ahead"]]
        if not members:
            train.prefixes = None
    train.save()


def _move_prefixes(train: Train, number: str, base: str) -> None:
    """Keep only prefixes that contain the squash now on main, removing that dependency."""
    assert train.prefixes is not None
    record = train.prefixes
    remaining = []
    for member in record["members"]:
        if number in member["ahead"]:
            member["ahead"].remove(number)
            remaining.append(member)
    record["members"] = remaining
    record["base"] = base
    if not remaining:
        train.prefixes = None


def _drop_line(train: Train) -> None:
    """Forget every prefix, for example when main changes; the sweep deletes their branches."""
    train.prefixes = None
    train.save()


def _first_invalid(train: Train, record: dict[str, Any]) -> int:
    """The index of the first member whose prefix no longer stands, or how many there are.

    A prefix stands while its member waits at its approved head, eligible, in the order the
    line serves them. A restored front may have independent kept work beside it."""
    from merge_gate import read_pull, ready

    line = train.line()
    front = train.front()
    independent = any(
        member["number"] == front and not member["ahead"] for member in record["members"]
    )
    present = []
    for member in record["members"]:
        if member["number"] not in line:
            break
        present.append(member["number"])
    served = sorted(present, key=line.index)
    for index, member in enumerate(record["members"]):
        number = member["number"]
        if (
            index >= len(present)
            or served[index] != number
            or (
                index == 0 and number != front
                and front not in train.restored
                and not independent
            )
            or train.approved.get(number) != member["head"]
            or number in train.blocked
            or number in train.suspended
            or number in train.conflicts
            or number in train.unavailable
        ):
            return index
        pull = read_pull(number)
        if pull["headRefOid"] != member["head"] or not ready(pull):
            return index
    return len(record["members"])


def _start_prefixes(train: Train) -> None:
    """Push each prefix to its scratch branch and dispatch its runs, each once.

    Only the first member's prefix decides this call: a later one that cannot start yet is
    tried again at the next call, and never keeps a member ahead of it from merging."""
    from merge_gate import Refused, _dispatch_scratch, _drop_prefix, _push_scratch, image_difference

    members = train.prefixes["members"] if train.prefixes else []
    started = sum(m["scratch"] is None or m["scratch"]["pushed"] for m in members)
    for member in list(members):
        scratch = member["scratch"]
        if scratch is None:
            continue
        if "images" not in scratch["workflows"]:
            # A former source can be replaced by a nearer prefix's fallback image run.
            previous = next((m for m in reversed(members[:members.index(member)])
                             if m["number"] in member["ahead"] and m["scratch"]
                             and "images" in m["scratch"]["workflows"]), None)
            if previous and not image_difference(previous["tree"], member["tree"])[1]:
                scratch["image_source"] = {
                    "number": previous["number"], "tree": previous["tree"],
                    "scratch": previous["scratch"],
                }
        if not scratch["pushed"] and started >= train.window:
            continue
        try:
            was_pushed = scratch["pushed"]
            _push_scratch(
                train,
                scratch,
                f"pull request {member['number']}'s prefix",
                lambda number=member["number"]: _drop_prefix(train, number),
            )
            started += not was_pushed
            for workflow in scratch["workflows"]:
                _dispatch_scratch(train, scratch, workflow)
        except Refused as error:
            if member["number"] == train.front():
                raise
            print(f"the prefix of pull request {member['number']} starts later: {error}", flush=True)
            return


def _refill(train: Train, base: str) -> None:
    """After a merge, the window has room: the next members' prefixes start on the new main.
    A new line starts only on a green main, as at a gate call. The merge is done; a prefix that
    cannot start yet starts at the next call."""
    from merge_gate import (
        MERGING,
        Refused,
        _drop_line,
        _extend_line,
        _start_prefixes,
        refresh_health,
    )

    try:
        if train.prefixes is not None and train.prefixes["base"] != base:
            _drop_line(train)
        if train.prefixes is None:
            current, health = refresh_health(train.path.parent)
            if current != base or health.state not in MERGING:
                return
        _extend_line(train, base, False)
        _start_prefixes(train)
    except (Refused, KeyError, TypeError, ValueError, IndexError) as error:
        print(f"the next prefixes start at the next call: {error}", flush=True)


@dataclass
class Prefix:
    """How a member's prefix run stands: "green", "pending", "red" or "short" (passed without
    proving its tested files), with what to say and, when green, its runs."""

    state: str
    message: str
    evidence: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]
    workflow: str = ""
    run: dict[str, Any] | None = None


def _prefix_state(member: dict[str, Any], train: Train) -> Prefix:
    from merge_gate import (
        Prefix,
        Refused,
        Waiting,
        _batch_runs,
        _proves_whole,
        _queue_recovery_pending,
        _queued_prefix,
        batch_service_wait,
        complete_image_jobs,
        git,
        jobs,
        image_difference,
        image_source_runs,
        prefix_image_source,
        whole_suite,
        workflow_verdict,
    )

    folder = train.path.parent
    scratch, number, tree = member["scratch"], member["number"], member["tree"]
    whose = f"pull request {number}'s prefix"
    evidence: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    pending: list[str] = []
    short: list[str] = []

    def replacement_images() -> Prefix:
        scratch.pop("image_source", None)
        scratch["workflows"].append("images")
        train.save()
        return Prefix("pending", f"waiting for replacement images of {whose}", evidence)

    source = prefix_image_source(member, train)
    work = [(workflow, scratch, tree) for workflow in scratch["workflows"]]
    if source:
        work.append(("images", source.get("scratch"), source["tree"]))
    elif "image_source" in scratch:
        return replacement_images()
    for workflow, tested, tested_tree in work:
        borrowed = workflow == "images" and source is not None
        found = image_source_runs(source) if borrowed else _batch_runs(tested, workflow)
        try:
            parts = jobs(found[0]) if found else []
            changed_images = borrowed and image_difference(tested_tree, tree)[1]
        except Waiting:
            raise
        except Refused:
            if borrowed:
                return replacement_images()
            raise
        if borrowed and (
            not found
            or workflow_verdict(workflow, found[0], parts)[0] == "red"
            or changed_images
        ):
            # A merged or dropped source with no usable result cannot leave this prefix
            # waiting forever or authorize a merge. Its own images replace that proof.
            return replacement_images()
        if not found:
            pending.append(f"waiting for {workflow} of {whose} to start")
            continue
        if not borrowed:
            _queued_prefix(train, scratch, workflow, found[0], quiet=False)
        if not borrowed and _queue_recovery_pending(scratch, found[0]):
            pending.append(f"waiting for {workflow} queue recovery of {whose}")
            continue
        state, failing = workflow_verdict(workflow, found[0], parts)
        if state == "red" and workflow == "ci":
            try:
                waits = batch_service_wait(found[0], parts, folder)
            except Waiting:
                raise
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence of the service proves no wait: a failure as before.
                waits = None
            if waits:
                pending.append(waits)
                continue
        if state == "red":
            waits = check_repair.runner_wait(folder, found[0], parts)
            if waits:
                pending.append(waits)
                continue
        if state == "red":
            failure = (
                "failed again after its runner retry"
                if check_repair.repeated(folder, found[0]["id"])
                else "failed"
            )
            return Prefix(
                "red",
                f"{workflow} run {found[0]['id']} of {whose} {failure} ({', '.join(failing)})",
                evidence,
                workflow,
                found[0],
            )
        if state != "green":
            pending.append(f"waiting for {workflow} checks of {whose}")
            continue
        # A manual run is every server test and the whole browser suite; the notes say so,
        # whatever a later CI plans, and they name the tested files.
        if not (
            _proves_whole("ci", found[0], parts, {**scratch, "tree": tree})
            if workflow == "ci"
            else whole_suite(found[0], parts, tree)
            if workflow == "e2e"
            else _proves_whole("images", found[0], parts, {"tree": tested_tree})
        ) or (borrowed and not complete_image_jobs(parts)):
            if borrowed:
                return replacement_images()
            short.append(
                f"{whose} e2e did not prove the whole browser suite on its tested files"
                if workflow == "e2e"
                else f"{whose} {workflow} did not prove its tested files"
            )
        evidence[workflow] = found[0], parts
    if pending:
        return Prefix("pending", pending[0], evidence)
    remote = git("ls-remote", "origin", f"refs/heads/{scratch['branch']}").split()
    if not remote or remote[0] != scratch["head"]:
        short.append("the gate's scratch branch changed")
    if short:
        return Prefix("short", short[0], evidence)
    return Prefix("green", "", evidence)


def _run_line(
    train: Train, base: str, health: Health, folder: Path, acting: str, fresh: bool
) -> int | None:
    """Test the first members of the line at once, each as a prefix: main and every member
    ahead of it, on a scratch branch of its own. A member merges once its prefix passed and
    every member ahead of it merged. None leaves the call to the single path."""
    from merge_gate import (
        MERGING,
        Refused,
        Waiting,
        _batch_report,
        _batch_wait,
        _drop_line,
        _drop_prefix,
        _extend_line,
        _first_invalid,
        _merge_prefixes,
        _start_prefixes,
    )

    if train.prefixes is not None and train.prefixes["base"] != base:
        print("main moved outside the merge train; its prefixes are tested again", flush=True)
        _drop_line(train)
    if health.state == "red":
        # The fix of a red main merges by its own evidence, on the single path.
        if train.prefixes is not None:
            _drop_line(train)
        return None
    record = train.prefixes
    if health.state not in MERGING and not (
        record and record["merged"] and health.state == "pending"
    ):
        if record is None:
            return None
        raise Waiting("merging stopped: " + health.line)
    while record is not None:
        valid = _first_invalid(train, record)
        if valid == len(record["members"]):
            break
        number = record["members"][valid]["number"]
        print(
            f"merge train: the prefix of pull request {number} "
            "and those containing it are tested again",
            flush=True,
        )
        _drop_prefix(train, number)
        record = train.prefixes
    _extend_line(train, base, fresh)
    if train.prefixes is None:
        return None
    _start_prefixes(train)
    merged: list[str] = []

    def stop(message: str, *, then: str = "wait") -> int | None:
        """End this call. Once the caller's own pull request merged, report that merge."""
        if acting in merged:
            return _batch_report(train, merged, acting, message)
        if then == "single":
            print(message, flush=True)
            return None
        if then == "refuse":
            raise Refused(message)
        return _batch_wait(message)

    try:
        return _merge_prefixes(train, folder, acting, merged, stop)
    except (Refused, KeyError, TypeError, ValueError, IndexError) as error:
        if acting in merged:
            return _batch_report(train, merged, acting, f"the merge train stopped: {error}")
        raise


def _prefix_failed(
    train: Train,
    member: dict[str, Any],
    found: Prefix,
    acting: str,
    stop: Callable[..., int | None],
) -> int | None:
    """Block the first member whose prefix run failed, as a failed check: every member ahead of
    it merged, so the failure is its own. The prefixes behind it hold its change and are built
    again from the new front, without it, and dispatched."""
    from merge_gate import (
        Refused,
        _drop_prefix,
        _extend_line,
        _pull_run_token,
        _run_token,
        _start_prefixes,
    )

    number, base = member["number"], train.prefixes["base"] if train.prefixes else ""
    assert found.run is not None
    train.batch_failed[number] = {
        "pull": _pull_run_token(member["head"]),
        "batch": member["scratch"],
        "workflow": found.workflow,
        "run": _run_token(found.run),
    }
    _drop_prefix(train, number)
    train.failed_prefix()
    train.fail(number, member["head"])
    # Its own checks stay green and unchanged, so no checks wake would come.
    train.turn += 1
    train.save()
    try:
        _extend_line(train, base, False)
        _start_prefixes(train)
    except (Refused, KeyError, TypeError, ValueError, IndexError) as error:
        print(f"the prefixes behind it start at the next call: {error}", flush=True)
    message = f"{found.message} on main and the members ahead of it"
    if number == acting:
        raise Refused(
            f"{message}; treat it as a failed check: fix and push, or rerun that run and call "
            "this gate again"
        )
    return stop(f"merge train: {message}; it is blocked as a failed check")


def _merge_prefixes(
    train: Train,
    folder: Path,
    acting: str,
    merged: list[str],
    stop: Callable[..., int | None],
) -> int | None:
    from merge_gate import (
        MERGING,
        Refused,
        SCRATCH_PREFIX,
        Waiting,
        _batch_ready,
        _batch_report,
        _batch_squash,
        _batch_wait,
        _drop_line,
        _drop_prefix,
        _merge_tree,
        _move_prefixes,
        _prefix_failed,
        _prefix_state,
        _refill,
        _retest,
        git,
        read_pull,
        record_green,
        refresh_health,
        squash_proof,
        strict_jobs,
    )

    while train.prefixes is not None:
        record = train.prefixes
        member = next((m for m in record["members"] if m["number"] == train.front()), None)
        if member is None or member["ahead"]:
            if merged:
                _refill(train, record["base"])
            return stop("merge train: waiting for the members ahead of the next prefix")
        number, head, tree = member["number"], member["head"], member["tree"]
        if member["scratch"] is None:
            # Its own CI tested its merge with this main: it merges by its own checks.
            if number == acting:
                return None
            return stop(f"merge train: pull request {number} merges by its own checks first")
        found = _prefix_state(member, train)
        if found.state == "pending":
            if merged:
                _refill(train, record["base"])
            return stop(f"merge train: {found.message}")
        if found.state == "red":
            return _prefix_failed(train, member, found, acting, stop)
        if found.state == "green" and train.retest:
            train.retest = None
            train.save()
        if found.state == "short":
            # Retested on a new scratch branch; the prefixes behind it hold the same commit.
            member["scratch"] = {
                **{key: member["scratch"][key] for key in ("head", "under", "workflows")},
                **({"image_source": member["scratch"]["image_source"]}
                   if "image_source" in member["scratch"] else {}),
                "branch": f"{SCRATCH_PREFIX}{uuid.uuid4().hex}",
                "requested": [],
                "pushed": False,
            }
            train.save()
            try:
                _retest(train, tree, found.message)
            except Refused as error:
                if acting in merged:
                    return stop(str(error))
                raise
        train.reconcile(acting=acting)
        pull = read_pull(number)
        current_base, current_health = refresh_health(folder)
        if current_base != record["base"]:
            _drop_line(train)
            return stop(
                "main moved during the merge train; the prefixes are tested again",
                then="refuse",
            )
        if current_health.state not in MERGING and (
            not record["merged"] or current_health.state != "pending"
        ):
            if current_health.state == "red":
                _drop_line(train)
                return stop(
                    "main is red; remaining members use the single fix rule",
                    then="single",
                )
            if acting in merged:
                return stop("merging stopped: " + current_health.line)
            raise Waiting("merging stopped: " + current_health.line)
        if (
            number != train.front()
            or train.approved.get(number) != head
            or pull["headRefOid"] != head
            or not _batch_ready(pull, current_base, settling=bool(record["merged"]))
        ):
            _drop_prefix(train, number)
            train.turn += 1
            train.save()
            return stop(f"merge train: prefix member {number} is no longer ready")
        if _merge_tree(current_base, head) != tree:
            _drop_line(train)
            return stop(
                f"the squash of {number} differs from its prefix; the members return to their "
                "own single gate path",
                then="single",
            )
        failure = _batch_squash(
            number,
            head,
            current_base,
            squash_proof(
                current_base,
                tree,
                {
                    workflow: (run, strict_jobs(parts) if workflow == "ci" else parts)
                    for workflow, (run, parts) in found.evidence.items()
                },
            ),
        )
        if failure:
            _drop_line(train)
            return stop(failure, then="refuse")
        # Recorded before anything else can fail. Another member's reviewer or audit ended
        # its turn waiting in the train; the tick reads this note and tells it to finish by
        # the after-merge rules. For the caller it keeps the merge known when a later step
        # of this call fails.
        merged.append(number)
        note = folder / f"batch-merged-{number}"
        note.write_text(json.dumps({"by": acting, "head": head}) + "\n")
        git("fetch", "origin", "main")
        actual = git("rev-parse", "origin/main")
        if context := train_reads.tick():
            train_reads.save(context / "main.json", {"head": actual})
        note.write_text(json.dumps({"by": acting, "head": head, "squash": actual}) + "\n")
        if (
            git("show", "-s", "--format=%P", actual) != current_base
            or git("rev-parse", f"{actual}^{{tree}}") != tree
        ):
            train.prefixes = None
            train.remove(number)
            return stop(
                "main did not match the prefix squash; waiting for main's own checks",
                then="refuse",
            )
        record["merged"] += 1
        # Only prefixes containing this squash still test the new main. Independent ones
        # get a new prefix there; descendants of the kept member retain their runs.
        _move_prefixes(train, number, actual)
        train.merged_one()
        train.remove(number)
        ci, parts = found.evidence["ci"]
        image = found.evidence.get("images")
        try:
            # Main is exactly the tree this member's prefix tested.
            record_green(
                folder,
                current_base,
                tree,
                ci,
                strict_jobs(parts),
                [image[0]] if image else [],
                image[1] if image else [],
            )
        except (Refused, OSError) as error:
            print(f"could not record inherited prefix green: {error}", flush=True)
        if train.restored or (
            train.prefixes and any(m.get("kept") for m in train.prefixes["members"])
        ):
            # The squash dropped independent retests, even when the last kept prefix
            # merged. Recompute pending state before filling slots on the new main.
            train.reconcile(acting=acting)
            _refill(train, actual)
    if acting not in merged:
        # Unreachable while the caller is the first member; never claim its merge.
        return _batch_wait("merge train: prefixes merged " + ", ".join(merged))
    _refill(train, git("rev-parse", "origin/main"))
    return _batch_report(train, merged, acting, "every tested member merged")


def _suite_of(train: Train, number: str, base: str, head: str, tree: str) -> dict[str, Any] | None:
    suite = train.suites.get(number)
    if suite and [suite["base"], suite["member"], suite["tree"]] == [base, head, tree]:
        return suite
    return None


def _whole_suite_run(
    train: Train, number: str, base: str, head: str, tree: str, needed: list[str]
) -> int | None:
    """Run every browser test, every server test, or both, on the tested merge of a pull request
    that merges alone.

    Its own E2E and CI runs may have tested only the specs and the server test files its change
    reaches. As for a batch, the gate pushes a commit of the tested files to a scratch branch
    and dispatches the `needed` workflows there; a manual run is the whole suite. None once
    each of those runs passed on this very tree.
    """
    from merge_gate import (
        Refused,
        SCRATCH_PREFIX,
        WHOLE,
        Waiting,
        _batch_runs,
        _batch_wait,
        _dispatch_scratch,
        _proves_whole,
        _push_scratch,
        _queue_recovery_pending,
        _queued_prefix,
        _retest,
        _service_wait,
        _suite_of,
        _suite_workflows,
        git,
        jobs,
        workflow_verdict,
    )

    suite = _suite_of(train, number, base, head, tree)
    if suite is None:
        suite = {
            "base": base,
            "member": head,
            "tree": tree,
            "head": git(
                "commit-tree",
                tree,
                "-p",
                base,
                "-p",
                head,
                "-m",
                f"chore: whole suite for pull request {number}",
            ),
            "branch": f"{SCRATCH_PREFIX}{uuid.uuid4().hex}",
            "workflows": [],
            "requested": [],
            "pushed": False,
        }
        train.suites[number] = suite
    # A record saved before it named its workflows ran the browser suite.
    suite["workflows"] = sorted(set(_suite_workflows(suite)) | set(needed))
    train.save()

    def drop() -> None:
        train.suites.pop(number, None)
        train.save()

    _push_scratch(train, suite, "the whole suite's", drop)
    for workflow in suite["workflows"]:
        _dispatch_scratch(train, suite, workflow)
    for workflow in suite["workflows"]:
        found = _batch_runs(suite, workflow)
        if not found:
            return _batch_wait(f"merge train: waiting for {WHOLE[workflow]} to start")
        _queued_prefix(train, suite, workflow, found[0], quiet=False)
        parts = jobs(found[0])
        if _queue_recovery_pending(suite, found[0]):
            return _batch_wait(f"merge train: waiting for {WHOLE[workflow]} queue recovery")
        state, failing = workflow_verdict(workflow, found[0], parts)
        if state == "red" and workflow == "ci":
            # The same package audit as a batch's: a run red only because it could not reach
            # an advisory service waits, and is re-run once the service answers.
            try:
                waits = _service_wait(
                    found[0],
                    parts,
                    train.path.parent,
                    f"run {found[0]['id']} of {WHOLE[workflow]}",
                    "batch-rerun",
                    "by the gate call at the front of the train",
                    retry=True,
                )
            except Waiting:
                raise
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence of the service proves no wait: refused as before.
                waits = None
            if waits:
                return _batch_wait(f"merge train: {waits}")
        if state == "red":
            waits = check_repair.runner_wait(train.path.parent, found[0], parts)
            if waits:
                return _batch_wait(f"merge train: {waits}")
        if state == "red":
            # Blocked like a failed check, and kept: a rerun of this same run, once green,
            # restores its place and lets the next call merge.
            train.fail(number, head)
            failure = (
                "failed again after its runner retry"
                if check_repair.repeated(train.path.parent, found[0]["id"])
                else "failed"
            )
            raise Refused(
                f"{WHOLE[workflow]}, run {found[0]['id']}, {failure} on this pull request's "
                f"tested merge ({', '.join(failing)}); treat it as a failed check: fix and push, "
                "or rerun that run and call this gate again"
            )
        if state != "green":
            return _batch_wait(f"merge train: waiting for {WHOLE[workflow]} on the tested merge")
        suite.setdefault("passed", {})[workflow] = found[0]["id"]
        train.save()
        if not _proves_whole(workflow, found[0], parts, suite):
            drop()
            _retest(
                train,
                tree,
                f"the run of {WHOLE[workflow]} did not prove its tested files",
            )
    if train.retest:
        train.retest = None
        train.save()
    return None
