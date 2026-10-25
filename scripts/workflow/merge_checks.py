"""CI jobs, results and proof of the tested tree."""

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from merge_health import Health
from merge_train import Train


def _ci_check(name: str) -> bool:
    from merge_gate import CI_PREFIX

    return re.match(rf"{re.escape(CI_PREFIX)}(?:$|[\s:(])", name) is not None


def _ci_suites(head: str) -> list[int]:
    """The check suites of the CI jobs that finished on `head`, as its check runs show them."""
    from merge_gate import API, _ci_check, api

    return sorted(
        {
            check["check_suite"]["id"]
            for check in api(f"{API}/commits/{head}/check-runs?per_page=100", "check_runs")
            if _ci_check(check.get("name", ""))
            and check.get("status") == "completed"
            and check.get("check_suite")
        }
    )


def jobs(run: dict[str, Any]) -> list[dict[str, Any]]:
    from merge_gate import API, api, checks

    return checks(api(f"{API}/actions/runs/{run['id']}/jobs?per_page=100", "jobs"))


def carried(job: dict[str, Any]) -> bool:
    """Whether GitHub carried this job into its attempt from an earlier one, without running it.

    A re-run of only the failed jobs lists each job that passed before as a copy in the new
    attempt: a new ID, the new attempt's `run_attempt`, no notes, and the `started_at` and
    `completed_at` of the attempt it ran in, but the `created_at` of the re-run. A job that ran
    in its attempt started after it was created.
    """
    from merge_gate import _seconds

    started, created = _seconds(job.get("started_at")), _seconds(job.get("created_at"))
    return started is not None and created is not None and started < created


def job_notes(job: dict[str, Any]) -> list[dict[str, Any]]:
    """The notes of a job, from the attempt of its run where it ran.

    A job carried into a re-run has no notes of its own. Its notes are those of the copy, in an
    earlier attempt of the same run, with the same name, head and times, that passed and ran
    there; a job carried by two re-runs has a carried copy in the attempt between. A job that
    ran in its own attempt has only its own notes, however few.
    """
    from merge_gate import API, api, carried

    notes = api(f"{API}/check-runs/{job['id']}/annotations?per_page=100")
    attempt, run, head = job.get("run_attempt"), job.get("run_id"), job.get("head_sha")
    if (
        notes
        or job.get("conclusion") != "success"
        or not carried(job)
        or not isinstance(attempt, int)
        or run is None
        or not head
    ):
        return notes
    for earlier in range(attempt - 1, 0, -1):
        for copy in api(f"{API}/actions/runs/{run}/attempts/{earlier}/jobs?per_page=100", "jobs"):
            if (
                copy.get("name") == job["name"]
                and copy.get("head_sha") == head
                and copy.get("conclusion") == "success"
                and copy.get("started_at") == job.get("started_at")
                and copy.get("completed_at") == job.get("completed_at")
                and not carried(copy)
            ):
                return api(f"{API}/check-runs/{copy['id']}/annotations?per_page=100")
    return []


# A CI job run in parts has a ` (k of n)` suffix, and its name alone when skipped.
# Image jobs keep their existing, separate grouping.
_PART = re.compile(r"(.+) \((\d+) of (\d+)\)")


def checks(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The jobs of a CI run as the checks they are: the parts of one job are one check.

    It passed only when every part passed, failed when one failed, and is running while one
    runs or while a part is missing. Then a pull request that skipped it is judged by main's
    whole check, and a fix of a red main must pass every part, however the tests were dealt to
    the parts.
    """
    from merge_gate import CI_PREFIX, CONFIG, _PART

    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in parts:
        matched = _PART.fullmatch(job.get("name", ""))
        if matched and not (
            matched[1].startswith(CI_PREFIX + ": ")
            or matched[1] in (
                CONFIG.checks.server_tests, CONFIG.checks.web, CONFIG.checks.older_version,
            )
        ):
            matched = None
        grouped.setdefault(matched[1] if matched else job.get("name", ""), []).append(job)
    merged: list[dict[str, Any]] = []
    for name, group in grouped.items():
        if len(group) == 1 and group[0].get("name") == name:
            merged.append(group[0])
            continue
        conclusions = [job.get("conclusion") for job in group]
        failed = [c for c in conclusions if c not in (None, "success", "skipped")]
        dealt = sorted(
            (int(matched[3]), int(matched[2]))
            for matched in (_PART.fullmatch(job.get("name", "")) for job in group)
            if matched
        )
        whole = dealt == [(len(group), number) for number in range(1, len(group) + 1)]
        merged.append(
            {
                **group[0],
                "name": name,
                "status": "completed"
                if whole and all(job.get("status") == "completed" for job in group)
                else "in_progress",
                "conclusion": failed[0]
                if failed
                else conclusions[0]
                if whole and len(set(conclusions)) == 1
                else None,
                # Where every part ran: a retry at home must not reach a part that ran away.
                "labels": sorted(
                    set.intersection(*(set(job.get("labels") or []) for job in group))
                ),
            }
        )
    return merged


def verdict(
    run: dict[str, Any],
    parts: list[dict[str, Any]],
    *,
    images: bool = False,
    plan: str | None = None,
) -> tuple[str, list[str]]:
    from merge_gate import CI_RESULT, IMAGES_PLAN, cancelled_inherited, lender_replanning

    label = plan or ("Images" if images else CI_RESULT)
    plan = plan or (IMAGES_PLAN if images else CI_RESULT)
    required = [job for job in parts if job["name"] == plan]
    failed = [
        job["name"] for job in parts if job.get("conclusion") not in (None, "success", "skipped")
    ]
    settled = (None, "success", "skipped", "cancelled")
    if (
        run.get("conclusion") == "cancelled"
        and all(job.get("conclusion") in settled for job in parts)
        and lender_replanning(
            str(run["id"]), head=run.get("head_sha"), attempt=run.get("run_attempt", 1)
        )
    ):
        return "pending", [plan]
    if (
        run.get("conclusion") == "cancelled"
        # The summary job runs even in a cancelled run, and fails for the jobs cancelled under it.
        and all(job.get("conclusion") in settled or job["name"] == plan for job in parts)
        and cancelled_inherited(run)
    ):
        # The gate cancelled it, still running, once main inherited its result: that result
        # stands for it.
        return "pending", [plan]
    if failed or run.get("conclusion") not in (None, "success"):
        return "red", failed or [label]
    if (
        run["status"] != "completed"
        or not parts
        or not required
        or any(job["status"] != "completed" for job in parts)
        or any(job.get("conclusion") != "success" for job in required)
        or run.get("conclusion") != "success"
    ):
        return "pending", [plan]
    return "green", []


def workflow_verdict(
    workflow: str, run: dict[str, Any], parts: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    from merge_gate import E2E_PLAN, verdict

    return verdict(
        run, parts, images=workflow == "images", plan=E2E_PLAN if workflow == "e2e" else None
    )


def inherited_green(head: str) -> dict[str, Any] | None:
    """The gate alone writes this receipt, after observing the actual squash on main."""
    from merge_gate import CONFIG, git, main_checkout

    path = main_checkout() / CONFIG.state_folder / "main-tree-green.json"
    try:
        receipt = json.loads(path.read_text())
        if (
            receipt["main"] == head
            and receipt["tree"] == git("rev-parse", f"{head}^{{tree}}")
            and isinstance(receipt["ci_run"], int)
            and receipt["ci_run"] > 0
        ):
            return receipt
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _cancel_entry(run: dict[str, Any], tree: str) -> dict[str, Any]:
    # A re-run is another attempt: only the attempt the gate cancelled reads as inherited.
    return {"run": run["id"], "attempt": run.get("run_attempt", 1), "tree": tree}


def cancelled_inherited(run: dict[str, Any]) -> bool:
    """Whether the gate cancelled this attempt of a run of main because main inherited its
    result on this very tree. Any other cancelled run of main is red."""
    from merge_gate import _cancel_entry, inherited_green

    if run.get("head_branch") != "main" or not run.get("head_sha"):
        return False
    receipt = inherited_green(run["head_sha"])
    listed = receipt.get("cancelled_inherited") if receipt else None
    return isinstance(listed, list) and _cancel_entry(run, receipt["tree"]) in listed


def cancel_inherited(
    head: str, receipt: dict[str, Any], ci: list[dict[str, Any]], folder: Path
) -> None:
    """Cancel main's CI runs of `head` still queued or running whose result main inherited. Each
    is listed in the receipt first, with the tree it belongs to, so its cancellation never
    turns main red; the runs of another tree leave the list.

    Only CI: main is judged by the CI runs of its head alone, but by its latest image run on any
    earlier push too, where a cancelled one would decide main once a later receipt replaced its
    entry. A re-run, the gate's own retry of a failed main included, is never cancelled: its
    result decides main."""
    from merge_gate import _cancel, _cancel_entry

    found = [
        run
        for run in ci
        if run["head_sha"] == head
        and run.get("status") != "completed"
        and run.get("run_attempt", 1) == 1
    ]
    if not found:
        return
    tree = receipt["tree"]
    listed = [
        entry
        for entry in receipt.get("cancelled_inherited") or []
        if isinstance(entry, dict) and entry.get("tree") == tree
    ]
    listed += [_cancel_entry(run, tree) for run in found if _cancel_entry(run, tree) not in listed]
    if listed != receipt.get("cancelled_inherited"):
        with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as output:
            json.dump({**receipt, "cancelled_inherited": listed}, output)
            temporary = output.name
        os.replace(temporary, folder / "main-tree-green.json")
    _cancel(found, folder, f"main {head[:12]}, inherited on identical tree {tree}")


def strict_jobs(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A pull request's dependency audit passes over an advisory that main has too, with an
    "Advisory on main" warning (scripts/audit-against-base.py). That pass is not main's check:
    it neither makes the pull request the fix of a red main nor stands in for main's own run."""
    from merge_gate import DEPENDENCIES, job_notes

    strict: list[dict[str, Any]] = []
    for job in parts:
        if job["name"] == DEPENDENCIES and job.get("conclusion") == "success":
            notes = job_notes(job)
            if any(note.get("title") == "Advisory on main" for note in notes):
                job = {**job, "conclusion": "passed over an advisory on main"}
        strict.append(job)
    return strict


def record_green(
    folder: Path,
    base: str,
    tree: str,
    ci: dict[str, Any],
    ci_jobs: list[dict[str, Any]],
    images: list[dict[str, Any]],
    image_jobs: list[dict[str, Any]],
) -> None:
    # GitHub's squash may differ, or somebody may push main outside the lock. Either case
    # loses this optimization. A receipt is scoped to the observed commit, never future trees.
    from merge_gate import (
        IMAGES, IMAGES_PLAN, NOTES, _planner_notes, complete_image_jobs, git, image_difference,
        image_source_runs, jobs, main_image_source, workflow_verdict,
    )

    git("fetch", "origin", "main")
    head = git("rev-parse", "origin/main")
    if (
        git("show", "-s", "--format=%P", head) != base
        or git("rev-parse", f"{head}^{{tree}}") != tree
    ):
        print("main did not match the tested squash; waiting for main's own checks", flush=True)
        return
    if (not images or not complete_image_jobs(image_jobs)) and not image_difference(base, tree)[1]:
        # A neutral own-check merge can have no images, or skip release checks. Preserve
        # main's complete source before replacing its receipt, revalidating its attempt.
        source = main_image_source(base)
        if source:
            images = image_source_runs(source)
            image_jobs = jobs(images[0]) if images else []
            if images and workflow_verdict("images", images[0], image_jobs)[0] != "green":
                images, image_jobs = [], []
    images_complete = complete_image_jobs(image_jobs)
    image_tree = None
    differing: list[str] = []
    if images_complete:
        trees = [message for title, message in _planner_notes(image_jobs, IMAGES_PLAN)
                 if title == NOTES.files_tested]
        if len(trees) == 1 and re.fullmatch(r"[0-9a-f]{40}", trees[0]):
            image_tree = trees[0]
            differing, relevant = image_difference(image_tree, tree)
            images_complete = not relevant
        else:
            images_complete = False
    # Skipped by the pull request's path filters: main's own run must pass these itself.
    unproven = sorted(job["name"] for job in ci_jobs if job.get("conclusion") != "success")
    with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as output:
        json.dump(
            {
                "main": head,
                "tree": tree,
                "ci_run": ci["id"],
                "ci_passed": sorted(
                    job["name"] for job in ci_jobs if job.get("conclusion") == "success"
                ),
                "ci_unproven": unproven,
                "images_run": images[0]["id"] if images else None,
                # Main runs release tests that ordinary PRs skip. A skipped PR job cannot
                # supply the green result for those checks on main.
                "images_complete": images_complete,
                "images_tree": image_tree,
                "images_differing_files": differing,
                "images_source": {
                    "run": {key: images[0][key] for key in ("id", "head_sha") if key in images[0]},
                    "tree": image_tree,
                } if images_complete else None,
            },
            output,
        )
        temporary = output.name
    os.replace(temporary, folder / "main-tree-green.json")
    source = (
        f"CI run {ci['id']}"
        + (f" and {IMAGES} run {images[0]['id']}" if images_complete else "")
        + (f"; main must pass {', '.join(unproven)} itself" if unproven else "")
    )
    print(
        f"main {head[:12]} recorded {source} for inheritance on tree {tree}"
        + (f"; images differ in {len(differing)} files: {json.dumps(differing[:5])}"
           if images_complete and differing else ""),
        flush=True,
    )


def whole_suite(run: dict[str, Any], parts: list[dict[str, Any]], tree: str) -> bool:
    """A green E2E run that ran every browser test on exactly this tree.

    E2E's planner notes "Browser suite" and "Files tested" (.github/workflows/e2e.yml). Only
    "whole" says that every spec ran: a pull request's run of the specs its change reaches
    says "selected: ...", and a run from before the note existed says nothing.
    """
    from merge_gate import E2E_PLAN, NOTES, _planner_notes, workflow_verdict

    if workflow_verdict("e2e", run, parts)[0] != "green" or not any(
        job["name"] == "playwright" and job.get("conclusion") == "success" for job in parts
    ):
        return False
    return {(NOTES.files_tested, tree), (NOTES.browser_suite, "whole")} <= _planner_notes(
        parts, E2E_PLAN
    )


def _planner_notes(parts: list[dict[str, Any]], plan: str) -> set[tuple[Any, str]]:
    from merge_gate import job_notes

    planner = next(job for job in parts if job["name"] == plan)
    return {(note.get("title"), note["message"].strip()) for note in job_notes(planner)}


def server_tests_selected(parts: list[dict[str, Any]]) -> bool:
    """Whether a pull request's CI ran fewer than every server test.

    CI's planner notes "Server tests" (.github/workflows/ci.yml): "whole" when every server test
    ran, "none" when the change needs no Python job (main runs it, and passes it itself before
    it is inherited), and "selected: ..." for the test files the change reaches. A run from
    before the note existed says nothing, which proves nothing either.
    """
    from merge_gate import CI_PLAN, NOTES, _planner_notes

    notes = _planner_notes(parts, CI_PLAN)
    return not {(NOTES.server_tests, "whole"), (NOTES.server_tests, "none")} & notes


def whole_server_tests(run: dict[str, Any], parts: list[dict[str, Any]], head: str) -> bool:
    """A green CI run that ran every server test on exactly this commit, run in parts or not."""
    from merge_gate import CI_PLAN, CONFIG, NOTES, _planner_notes, checks, verdict

    if verdict(run, parts)[0] != "green" or not any(
        job["name"] == CONFIG.checks.server_tests and job.get("conclusion") == "success"
        for job in checks(parts)
    ):
        return False
    return {(NOTES.merge_tested, head), (NOTES.server_tests, "whole")} <= _planner_notes(
        parts, CI_PLAN
    )


def _proves_whole(
    workflow: str, run: dict[str, Any], parts: list[dict[str, Any]], record: dict[str, Any]
) -> bool:
    from merge_gate import (
        CI_PLAN,
        CONFIG,
        IMAGES_PLAN,
        NOTES,
        _planner_notes,
        verdict,
        whole_server_tests,
        whole_suite,
        workflow_verdict,
    )

    if workflow == "images":
        return (
            workflow_verdict(workflow, run, parts)[0] == "green"
            and (NOTES.files_tested, record["tree"]) in _planner_notes(parts, IMAGES_PLAN)
            and any(job["name"] != IMAGES_PLAN and job.get("conclusion") == "success"
                    and CONFIG.checks.groups()["images"].contains(job["name"])
                    for job in parts)
        )
    if workflow == "ci":
        if "server_whole" in CONFIG.checks.prefix_only and {
            (NOTES.merge_tested, record["head"]), (NOTES.server_tests, "none")
        } <= _planner_notes(parts, CI_PLAN):
            return verdict(run, parts)[0] == "green"
        return whole_server_tests(run, parts, record["head"])
    return whole_suite(run, parts, record["tree"])


def tested_merge(parts: list[dict[str, Any]], base: str, head: str) -> str | None:
    from merge_gate import API, CI_PLAN, NOTES, api, job_notes

    for job in parts:
        if job["name"] != CI_PLAN:
            continue
        for note in job_notes(job):
            if note.get("title") != NOTES.merge_tested:
                continue
            merge = note["message"].strip()
            if not re.fullmatch(r"[0-9a-f]{40}", merge):
                continue
            commit = api(f"{API}/git/commits/{merge}")
            if [parent["sha"] for parent in commit["parents"]] == [base, head]:
                return merge
    return None


def _seconds(stamp: Any) -> float | None:
    try:
        moment = datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError, OverflowError, OSError:
        return None
    # GitHub writes 0001-01-01 for a check that never started.
    return moment if moment > 0 else None


def lost_check(
    pull: dict[str, Any],
    known: list[dict[str, Any]] | None = None,
    read: Callable[[str], dict[str, Any]] | None = None,
    *,
    failed: bool = False,
) -> dict[str, Any] | None:
    """A check the rollup lists as queued or running whose workflow run completed at least
    LOST_AFTER_SECONDS ago, when every check not passing is lost so: a check still running for
    real may yet fail, and is waited for first. Each read run costs a request from the shared
    budget, so a check that started less than that ago ends the search: its run cannot have
    completed so long ago. `read` reads a run by its id; the tick passes one that keeps each
    answer for the rest of the tick. A failed pull request's completed checks have had their
    say: then only the checks not completed must each be lost.
    """
    from merge_gate import (
        LOST_AFTER_SECONDS,
        _completed_check,
        _passing_check,
        _seconds,
        own_checks,
        read_run,
    )

    now = float(os.environ.get("WORKFLOW_NOW") or time.time())
    seen = {str(run["id"]): run for run in known or []}
    # Every check that cannot be lost is ruled out before any run is read.
    waiting = []
    for check in own_checks(pull):
        if _passing_check(check) or (failed and _completed_check(check)):
            continue
        if check.get("__typename") == "StatusContext" or check.get("status") == "COMPLETED":
            return None
        match = re.search(r"/actions/runs/(\d+)(?:/|$)", check.get("detailsUrl") or "")
        started = _seconds(check.get("startedAt"))
        if not match or (started is not None and now - started < LOST_AFTER_SECONDS):
            return None
        waiting.append((check, match[1]))
    found = []
    for check, run_id in waiting:
        if run_id not in seen:
            seen[run_id] = (read or read_run)(run_id)
        run = seen[run_id]
        finished = _seconds(run.get("updated_at"))
        if (
            run.get("status") != "completed"
            or finished is None
            or now - finished < LOST_AFTER_SECONDS
        ):
            return None
        found.append(
            {
                "check": check.get("name", "unknown"),
                "run": int(run_id),
                "conclusion": run.get("conclusion"),
            }
        )
    # A failed run's check is named first: it is a failure, never pushed for.
    return min(found, key=lambda lost: lost["conclusion"] == "success", default=None)


def note_lost(folder: Path, number: str, head: str, lost: dict[str, Any]) -> None:
    """Tell the orchestrator through the wake, once per head."""
    name = f"{number}-{head}.json"
    pending = folder / "lost-checks.pending"
    if (pending / name).exists() or (folder / "lost-checks.sent" / name).exists():
        return
    try:
        pending.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=pending, delete=False) as output:
            json.dump({"number": number, "head": head, **lost}, output)
        os.replace(output.name, pending / name)
    except OSError as error:
        print(f"could not note the lost check for the wake: {error}", flush=True)


def rerun_lost(pull: dict[str, Any], train: "Train", number: str, lost: dict[str, Any]) -> int:
    """One empty commit gets GitHub a new check list; only one per head, so a check lost again
    on the commit pushed for it is reported to the orchestrator and not pushed for."""
    from merge_gate import Refused, Waiting, _commit_and_push, git, note_lost

    head = pull["headRefOid"]
    what = f"GitHub lost {lost['check']} of run {lost['run']}"
    note_lost(train.path.parent, number, head, lost)
    if lost["conclusion"] != "success":
        raise Refused(
            f"{what}, which ended {lost['conclusion']}: treat it as a failed check; "
            "the orchestrator has been told"
        )
    if train.reruns.get(number) == head:
        raise Refused(
            f"{what} again, on the commit this gate pushed to run CI again; "
            "it is not pushed for twice, and the orchestrator has been told"
        )
    if git("rev-parse", "HEAD") == train.reruns.get(number):
        raise Waiting("CI was requested again; GitHub does not show the pushed head yet")
    if git("rev-parse", "HEAD") != head or not git("branch", "--show-current"):
        raise Refused(
            f"{what}; run this gate from the pull request's clean branch worktree to run CI again"
        )
    if git("status", "--porcelain"):
        raise Refused(f"{what}; the pull request worktree has uncommitted changes")
    _commit_and_push(pull, train, number, f"ci: run CI again, {what}")
    train.reruns[number] = git("rev-parse", "HEAD")
    train.save()
    print(
        f"{what} after its run finished; an empty commit was pushed to run CI again: "
        "wait for the new run, then call this gate again",
        flush=True,
    )
    return 2


def lender_replanning(
    run_id: str,
    *,
    head: str | None = None,
    attempt: int | None = None,
    job_id: int | None = None,
) -> bool:
    """Only the lender's recorded cancellation is pending, never a successful check.

    Bound the exception to the cancelled attempt and a recent cancellation. An abandoned
    rerun or a failed new attempt must still reach the ordinary failure handling.
    """
    try:
        notes = json.loads((Path.home() / ".local/state/ci-overflow/replanned.json").read_text())
        note = notes[run_id]
        return (
            note["state"] in ("cancelled", "rerun")
            and 0 <= time.time() - note["updated_at"] <= 180
            and (head is None or note["head"] == head)
            and (attempt is None or note["attempt"] == attempt)
            and (job_id is None or job_id in note["jobs"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _lender_cancelled_check(check: dict[str, Any], head: str | None) -> bool:
    from merge_gate import lender_replanning

    match = re.search(r"/actions/runs/(\d+)/job/(\d+)(?:/|$)", check.get("detailsUrl", ""))
    return bool(
        check.get("conclusion") == "CANCELLED"
        and match
        and lender_replanning(match[1], head=head, job_id=int(match[2]))
    )


def own_checks(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """Selected tests still decide the PR; deferred image jobs decide only its scratch run."""
    from merge_gate import CONFIG, IMAGES

    return [check for check in pull["statusCheckRollup"] if not (
        "images" in CONFIG.checks.prefix_only
        and (check.get("name", "") == IMAGES or check.get("name", "").startswith(IMAGES + ":"))
    )]


def _failed_rollup(pull: dict[str, Any]) -> list[dict[str, Any]]:
    from merge_gate import _lender_cancelled_check, own_checks

    return [
        check
        for check in own_checks(pull)
        if (
            check.get("conclusion")
            in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE")
            and not _lender_cancelled_check(check, pull.get("headRefOid"))
        )
        or check.get("state") in ("FAILURE", "ERROR")
    ]


def failed_checks(pull: dict[str, Any]) -> bool:
    from merge_gate import _failed_rollup

    return bool(_failed_rollup(pull))


def fix_evidence(
    pull: dict[str, Any], base: str, health: Health, suite: dict[str, Any] | None = None
) -> str | None:
    """What a judgment of `proves_main_fix` rests on, or None when its CI has not passed.

    The tick reconciles every 3 minutes and each proof costs several GitHub requests from
    the budget every session shares, so a judgment is kept until this evidence changes.
    """
    from merge_gate import CI_RESULT, CONFIG, _batch_runs, _suite_workflows

    if not any(
        check.get("name") == CI_RESULT and check.get("conclusion") == "SUCCESS"
        for check in pull["statusCheckRollup"]
    ):
        return None
    checks = sorted(
        json.dumps(check, sort_keys=True, default=str) for check in pull["statusCheckRollup"]
    )
    evidence: list[Any] = [pull["headRefOid"], base, sorted(health.failing), checks]
    if CONFIG.checks.prefix_only:
        # Scratch checks finish independently of the PR rollup. A new attempt can also
        # withdraw a previously proven fix, so both successful and failed judgments expire.
        scratch = [
            [workflow, run["id"], run.get("run_attempt", 1), run["status"], run.get("conclusion")]
            for workflow in _suite_workflows(suite)
            for run in _batch_runs(suite, workflow)
        ] if suite else []
        evidence += [CONFIG.checks.prefix_only, suite, scratch]
    return hashlib.sha256(json.dumps(evidence).encode()).hexdigest()


def proves_main_fix(pull: dict[str, Any], base: str, health: Health) -> bool:
    """Prioritize only fresh green evidence; the gate still verifies before merging."""
    from merge_gate import (
        API,
        CONFIG,
        IMAGES_PLAN,
        NOTES,
        Refused,
        Train,
        Waiting,
        _batch_runs,
        _proves_whole,
        _suite_workflows,
        api,
        failed_checks,
        job_notes,
        jobs,
        runs,
        strict_jobs,
        tested_merge,
        verdict,
    )

    try:
        if failed_checks(pull):
            return False
        head = pull["headRefOid"]
        ci = runs("ci", head, pull=True)
        if not ci:
            return False
        parts = jobs(ci[0])
        if verdict(ci[0], parts)[0] != "green":
            return False
        merge = tested_merge(parts, base, head)
        if merge is None:
            return False
        # Judge it as the gate does: a pass over main's own advisory is not the fix.
        passed = {job["name"] for job in strict_jobs(parts) if job.get("conclusion") == "success"}
        if set(health.failing) <= passed:
            return True
        if CONFIG.checks.prefix_only:
            tree = api(f"{API}/git/commits/{merge}")["tree"]["sha"]
            for suite in Train(CONFIG.state()).suites.values():
                if [suite["base"], suite["member"], suite["tree"]] != [base, head, tree]:
                    continue
                for workflow in _suite_workflows(suite):
                    found = _batch_runs(suite, workflow)
                    if found:
                        proven = jobs(found[0])
                        if _proves_whole(workflow, found[0], proven, suite):
                            passed |= {job["name"] for job in strict_jobs(proven)
                                       if job.get("conclusion") == "success"}
            if set(health.failing) <= passed:
                return True
            if "images" in CONFIG.checks.prefix_only:
                return False
        # Image failures need evidence for the very same tree as the fresh CI merge.
        images = runs("images", head, pull=True)
        if not images:
            return False
        image_jobs = jobs(images[0])
        if verdict(images[0], image_jobs, images=True)[0] != "green":
            return False
        tree = api(f"{API}/git/commits/{merge}")["tree"]["sha"]
        plan = next(job for job in image_jobs if job["name"] == IMAGES_PLAN)
        notes = job_notes(plan)
        if not any(
            note.get("title") == NOTES.files_tested and note["message"].strip() == tree
            for note in notes
        ):
            return False
        passed |= {job["name"] for job in image_jobs if job.get("conclusion") == "success"}
        return set(health.failing) <= passed
    except Waiting:
        raise
    except Refused, KeyError, TypeError, ValueError, IndexError, StopIteration:
        return False


def proves_advisory_fix(pull: dict[str, Any]) -> bool:
    """Whether a pull request is the update that fixes an advisory on main: its latest CI run is
    green and its own audit passed clean, without the "Advisory on main" warning, whatever main
    that run tested. Only its place in the line rests on this; the gate verifies its merge as
    any other's."""
    from merge_gate import (
        DEPENDENCIES,
        Refused,
        Waiting,
        failed_checks,
        jobs,
        runs,
        strict_jobs,
        verdict,
    )

    try:
        if failed_checks(pull):
            return False
        ci = runs("ci", pull["headRefOid"], pull=True)
        if not ci:
            return False
        parts = jobs(ci[0])
        if verdict(ci[0], parts)[0] != "green":
            return False
        return any(
            job["name"] == DEPENDENCIES and job.get("conclusion") == "success"
            for job in strict_jobs(parts)
        )
    except Waiting:
        raise
    except Refused, KeyError, TypeError, ValueError, IndexError:
        return False


def _passing_check(check: dict[str, Any]) -> bool:
    if check.get("__typename") == "StatusContext":
        return check.get("state") == "SUCCESS"
    return check.get("status") == "COMPLETED" and check.get("conclusion") in (
        "SUCCESS",
        "SKIPPED",
        "NEUTRAL",
    )


def _completed_check(check: dict[str, Any]) -> bool:
    if check.get("__typename") == "StatusContext":
        return check.get("state") in ("SUCCESS", "FAILURE", "ERROR")
    return check.get("status") == "COMPLETED"


def _passing_rollup(pull: dict[str, Any]) -> bool:
    from merge_gate import _passing_check, own_checks

    return all(_passing_check(check) for check in own_checks(pull))
