"""The tick's mechanical merge work. Every squash still goes through the merge gate."""

import fcntl
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import check_repair
import merge_gate as gate
import train_reads
from conflict_review import prepare as prepare_conflict_review
from merge_handoff import Handoff, write_json
from reviewer_start import start as start_reviewer


def _worktree(folder: Path, number: str) -> Path:
    return gate.CONFIG.checkout_of(folder) / ".claude" / "worktrees" / f"ticket-{number}"


def _sessions() -> set[str]:
    return {
        fields[0].split("-", 1)[1]
        for line in gate.command("bash", str(gate.HERE / "session"), "list").stdout.splitlines()
        if (fields := line.split())
        and fields[0].startswith(("review-", "audit-", "ticket-", "codex-", "cloud-"))
    }


def _hand_back(folder: Path, number: str, reason: str) -> None:
    # Revoke batch approval under the same lock the gate holds through a squash.
    with (folder / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = folder / f"merge-review-{number}"
        if not request.exists():
            try:
                note = Handoff.read(folder, number)
                head = note.head if note is not None else gate.read_pull(number)["headRefOid"]
            except gate.Refused, OSError, ValueError, TypeError, KeyError:
                head = ""
            local = gate.command(
                "git", "-C", str(_worktree(folder, number)), "rev-parse", "HEAD", check=False
            )
            write_json(
                request,
                {
                    "reason": reason,
                    "event": uuid.uuid4().hex,
                    "head": head,
                    "local_head": local.stdout.strip() if local.returncode == 0 else "",
                },
            )
        _queue_notice(folder, number, request)
        Handoff.path(folder, number).unlink(missing_ok=True)
        (folder / f"review-{number}.head").unlink(missing_ok=True)
        train = gate.Train(folder)
        train.approved.pop(number, None)
        train.save()
    print(f"merge hand-back {number}: {reason}", flush=True)


def _queue_notice(folder: Path, number: str, request: Path) -> None:
    data = json.loads(request.read_text())
    identity = data.get("event", "")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        identity = uuid.uuid4().hex
        data["event"] = identity
        write_json(request, data)
    if not (folder / "merge-handbacks.sent" / identity).exists():
        write_json(
            folder / "merge-handbacks.pending" / identity,
            {"number": number, "reason": data["reason"]},
        )


def _hand_to_worker(
    folder: Path, number: str, pull: dict[str, Any], repair: check_repair.Repair
) -> None:
    """A failing test of its own goes to a worker: the pull request becomes a draft, its
    handoff is revoked, and it keeps its place in the line as a blocked member.

    The note is written first, so an interruption at any step finishes on the next tick."""
    if not repair.event:
        repair.event = uuid.uuid4().hex
    repair.save(folder, number)
    if not pull["isDraft"]:
        try:
            gate.command("gh", "pr", "ready", number, "-R", gate.REPOSITORY, "--undo")
        except gate.Waiting:
            raise
        except gate.Refused:
            # Still ready: it goes to a reviewer, as before, and no worker repairs it.
            check_repair.clear(folder, number)
            raise
    reason = (
        f"pull request checks failed on its own test: a worker fixes job {repair.job} of run "
        f"{repair.run}, first failing test {repair.test}"
    )
    with (folder / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (folder / "merge-handbacks.sent" / repair.event).exists():
            write_json(
                folder / "merge-handbacks.pending" / repair.event,
                {"number": number, "reason": reason},
            )
        Handoff.path(folder, number).unlink(missing_ok=True)
        (folder / f"review-{number}.head").unlink(missing_ok=True)
        train = gate.Train(folder)
        train.approved.pop(number, None)
        train.blocked[number] = repair.head
        if number not in train.queue:
            train.queue.append(number)
        train.save()
    print(f"merge handoff {number}: {reason}", flush=True)


def _checks_failed(folder: Path, number: str, pull: dict[str, Any]) -> None:
    """A handoff whose checks failed for the pull request: a worker for a failing test of its
    own, one re-run for its runners, a reviewer for anything else."""
    if not gate.failed_checks(pull):
        waits = check_repair.rerun_running(folder, pull)
        if waits:
            print(f"merge handoff {number} waits: {waits}", flush=True)
        else:
            # Its line's run failed, not its own checks: a reviewer judges it, as before.
            _hand_back(folder, number, "pull request checks failed")
        return
    diagnosis = check_repair.diagnose(pull)
    if diagnosis.kind == "infrastructure":
        waits = check_repair.rerun(folder, diagnosis)
        if waits:
            print(f"merge handoff {number} waits: {waits}", flush=True)
        else:
            _hand_back(folder, number, f"pull request checks failed: {diagnosis.reason}")
        return
    if diagnosis.kind != "own" or diagnosis.repair is None:
        _hand_back(folder, number, f"pull request checks failed: {diagnosis.reason}")
        return
    earlier = check_repair.Repair.read(folder, number)
    if earlier is not None and earlier.head != pull["headRefOid"]:
        # The worker's fix failed on a test too: no second worker, a reviewer judges it.
        check_repair.clear(folder, number)
        _hand_back(
            folder,
            number,
            f"pull request checks failed again after a worker's fix: job {diagnosis.repair.job}"
            f" of run {diagnosis.repair.run}, first failing test {diagnosis.repair.test}",
        )
        return
    _hand_to_worker(folder, number, pull, earlier or diagnosis.repair)


def _hand_back_pull(folder: Path, path: Path, running: set[str]) -> dict[str, Any] | None:
    """The pull request whose hand-back still wants a fresh reviewer, or None."""
    number = path.name.removeprefix("merge-review-")
    # Recover an interruption between persisting the request and queuing its wake.
    _queue_notice(folder, number, path)
    if number in running:
        path.unlink()  # its own session takes the judgment; the wake names it
        return None
    pull = train_reads.pull(number, gate.read_pull)
    if pull["state"] != "OPEN" or pull["isDraft"]:
        # Drafts already have their worker path; closed pulls need no reviewer.
        path.unlink()
        return None
    labelled = gate.CONFIG.labels.workflow
    if not any(label.get("name") == labelled for label in pull.get("labels", [])):
        # Unlabelled children belong to the orchestrator's audit path. The wake
        # reports their hand-back; the tick never invents an audit or review for them.
        return None
    return pull


def _tidy_hand_backs(folder: Path) -> None:
    """Record every hand-back's notice and drop the ones that want no reviewer any more.

    Their reviewers are started by the tick one at a time (`_start_hand_back`), each at its
    pull request's place in the order of starts, so a hand-back never goes before a more
    urgent ticket.
    """
    running = _sessions()
    for path in sorted(folder.glob("merge-review-*")):
        if path.name.removeprefix("merge-review-").isdigit():
            _hand_back_pull(folder, path, running)
    for path in sorted(folder.glob("check-repair-*.json")):
        number = path.name.removeprefix("check-repair-").removesuffix(".json")
        # A pull request that merged or closed needs no repair, and its next failure none.
        if number.isdigit() and train_reads.pull(number, gate.read_pull)["state"] != "OPEN":
            path.unlink()
    for path in folder.glob("check-rerun-*.json"):
        # Its run has long finished its one re-run by then.
        if time.time() - path.stat().st_mtime > check_repair.RERUN_KEPT_SECONDS:
            path.unlink(missing_ok=True)


def _start_hand_back(folder: Path, number: str) -> int:
    """Start the reviewer a hand-back asks for. Return 75 when the start waits for memory."""
    path = folder / f"merge-review-{number}"
    if not path.exists():
        return 0
    pull = _hand_back_pull(folder, path, _sessions())
    if pull is None:
        return 0
    reviews = gate.api(f"{gate.API}/pulls/{number}/reviews")
    prepare_conflict_review(
        folder,
        number,
        _worktree(folder, number),
        json.loads(path.read_text()),
        pull["headRefOid"],
        reviews,
    )
    rounds = []
    for review in reviews:
        match = re.match(
            r"^<!-- workflow-review round=([0-9]+)(?: result=(sent-back|passed))? -->",
            review.get("body") or "",
        )
        if match:
            rounds.append((int(match[1]), match[2] or "sent-back"))
    next_round = max((r[0] for r in rounds), default=0) + 1
    result = start_reviewer(
        folder,
        number,
        pull["headRefOid"],
        next_round,
        sum(r[1] == "sent-back" for r in rounds),
        pull["headRefName"],
    )
    if result == 0:
        path.unlink()
    # A refused start keeps its note and notice for the next tick, as before.
    return 75 if result == 75 else 0


def _restack(folder: Path, number: str, note: Handoff) -> None:
    # Record children before changing any base. A crash after PATCH must still finish
    # the merge/push on the next tick, although that child is no longer listed on this base.
    if note.children is None:
        listed = gate.command(
            "gh",
            "pr",
            "list",
            "-R",
            gate.REPOSITORY,
            "--state",
            "open",
            "--base",
            note.branch,
            "--limit",
            "1000",
            "--json",
            "number",
        ).stdout
        note.children = [str(pull["number"]) for pull in json.loads(listed)]
        note.save(folder, number)
    for child in list(note.children):
        try:
            _restack_child(folder, child, note.branch)
        except gate.Waiting:
            raise  # GitHub unreadable: keep the child listed for the next tick
        except (gate.Refused, OSError, ValueError) as error:
            _hand_back(folder, child, f"stacked on {number}: {error}")
        note.children.remove(child)
        note.save(folder, number)
    Handoff.path(folder, number).unlink()


def _restack_child(folder: Path, number: str, parent_branch: str) -> None:
    with (folder / f"start-{number}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            # A session start may hold this lock while waiting for memory. The tick
            # must remain free to stop/squeeze sessions and retry the child later.
            raise gate.Waiting("the stacked branch is being prepared by another session") from error
        pull = gate.read_pull(number)
        if pull["state"] != "OPEN" or pull["baseRefName"] not in (
            parent_branch,
            "main",
        ):
            return
        gate.command("gh", "api", "-X", "PATCH", f"{gate.API}/pulls/{number}", "-f", "base=main")
        if number in _sessions():
            raise gate.Refused("its own session must merge main into its branch")
        worktree = _worktree(folder, number)
        if not worktree.is_dir():
            raise gate.Refused("no clean worktree is available to merge main")

        def git(*args: str, check: bool = True) -> str:
            return gate.command("git", "-C", str(worktree), *args, check=check).stdout.strip()

        if git("status", "--porcelain") or git("rev-parse", "HEAD") != pull["headRefOid"]:
            raise gate.Refused("its worktree is dirty or differs from the published head")
        if git("branch", "--show-current") != pull["headRefName"]:
            raise gate.Refused("its worktree is on a different branch")
        git("fetch", "origin", "main")
        done = gate.command(
            "git",
            "-C",
            str(worktree),
            "merge",
            "--no-ff",
            "--no-commit",
            "origin/main",
            check=False,
        )
        if done.returncode:
            conflicts = git("diff", "--name-only", "--diff-filter=U").splitlines()
            if conflicts:
                message = "merge main: conflict in " + ", ".join(conflicts)
                path = Path(git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_MSG"))
                path.write_text(message + "\n")
            raise gate.Refused("merge main conflicts or failed: " + (done.stderr or done.stdout))
        # Already-up-to-date merges have no MERGE_HEAD and need no empty commit.
        if (
            gate.command(
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--verify",
                "MERGE_HEAD",
                check=False,
            ).returncode
            == 0
        ):
            git("commit", "-m", "chore: merge main after stacked base merged")
        try:
            pushed = gate.push("origin", f"HEAD:{pull['headRefName']}", where=worktree)
        except gate.PushWaiting:
            # The child stays listed; the next tick merges main from the published head again.
            git("reset", "--keep", pull["headRefOid"])
            raise
        if pushed.returncode:
            raise gate.Refused("could not push stacked branch: " + pushed.refusal)
        # A child reviewed against its old base needs its own fresh independent reviewer.
        _hand_back(folder, number, "stacked base merged; review the branch against main")


def _drive_gate(folder: Path, number: str) -> bool:
    worktree = _worktree(folder, number)
    if not worktree.is_dir():
        _hand_back(folder, number, "the pull request worktree is missing")
        return False
    previous = Path.cwd()
    try:
        os.chdir(worktree)
        done = gate.command(
            "bash",
            str(gate.HERE / "merge-when-current"),
            "--drive",
            number,
            check=False,
        )
    finally:
        os.chdir(previous)
    print(done.stdout + done.stderr, end="", flush=True)
    # A squash can succeed before a later batch/receipt check refuses.
    if done.returncode not in (0, 2, 3):
        if gate.read_pull(number)["state"] == "MERGED":
            return True
        _hand_back(folder, number, done.stderr.strip() or "merge gate refused")
    return done.returncode == 0


def drive(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if not any(folder.glob("merge-wanted-*")) and not any(folder.glob("merge-review-*")):
        return
    for request in sorted(folder.glob("merge-review-*")):
        number = request.name.removeprefix("merge-review-")
        if number.isdigit():
            # A crash can leave the request beside the old authorization. Complete
            # its revocation under merge.lock before any gate or batch can act.
            _hand_back(folder, number, json.loads(request.read_text())["reason"])
    running = _sessions()
    refreshed: set[str] = set()
    driven: set[str] = set()
    for path in sorted(folder.glob("merge-wanted-*")):
        number = path.name.removeprefix("merge-wanted-")
        if not number.isdigit() or number in running:
            continue
        try:
            note = Handoff.read(folder, number)
            assert note is not None
            pull = train_reads.pull(number, gate.read_pull)
            if pull["state"] == "OPEN" and (
                pull["headRefOid"] != note.head
                or pull["headRefName"] != note.branch
                or not gate.ready(pull)
                or gate.failed_checks(pull)
            ):
                # A gate refresh or a re-run can follow the bulk snapshot. Each of these
                # revokes the passed review, so it rests on a fresh read.
                pull = train_reads.refresh_pull(number, gate.read_pull)
            if (
                pull["state"] == "OPEN"
                and pull["headRefOid"] == note.head
                and any(check.get("name") == gate.CI_RESULT for check in pull["statusCheckRollup"])
                and gate._passing_rollup(pull)
            ):
                # Its handed-off head passed: a later failing test is a first one again.
                check_repair.clear(folder, number)
            if pull["state"] == "MERGED":
                # Keep the parent note through temporary API/IO failures: deleting it
                # would lose the children already moved to main before a restart.
                try:
                    _restack(folder, number, note)
                except (gate.Refused, OSError, TypeError, ValueError, KeyError) as error:
                    print(f"stack follow-up {number} waits: {error}", flush=True)
            elif not gate.ready(pull):
                repair = check_repair.Repair.read(folder, number)
                if pull["isDraft"] and repair is not None and repair.head == note.head:
                    # Interrupted after it became a draft for its worker.
                    _hand_to_worker(folder, number, pull, repair)
                else:
                    _hand_back(folder, number, "pull request is no longer ready on main")
            elif note.children is not None:
                _hand_back(folder, number, "unfinished stack follow-up on an open pull request")
            elif pull["headRefOid"] != note.head or pull["headRefName"] != note.branch:
                _hand_back(folder, number, "head changed since the passed review handoff")
            elif gate.failed_checks(pull) or number in gate.Train(folder).blocked:
                # An advisory service the audit could not reach found nothing wrong: the
                # handoff waits, and its CI is re-run once the service answers. Under the
                # lock a gate call re-runs under, so only one of them asks GitHub.
                with (folder / "merge.lock").open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    waits = gate.pull_service_wait(pull, folder, number, retry=True)
                if not waits:
                    _checks_failed(folder, number, pull)
                elif waits.startswith("automatic"):
                    print(f"merge handoff {number}: {waits}", flush=True)
                else:
                    print(f"merge handoff {number} waits: {waits}", flush=True)
            elif gate.conflicting(pull):
                # The gate holds merge.lock through recognition, checks and push and
                # advances the same authorization as a normal stale-CI refresh.
                if _drive_gate(folder, number):
                    driven.add(number)
                refreshed.add(number)
        except gate.Waiting as error:
            print(f"merge handoff {number} waits: {error}", flush=True)
        except (gate.Refused, OSError, TypeError, ValueError, KeyError) as error:
            _hand_back(folder, number, str(error))

    # Train status selects the eligible front, passing a pending member only for a
    # green member tested on current main. It may merge several notes in a batch;
    # merged members keep their worktrees until the next pass finishes their children.
    status = gate.command("bash", str(gate.HERE / "merge-when-current"), "--train-status")
    number = json.loads(status.stdout)["first"]
    if (
        number
        and number not in refreshed
        and number not in _sessions()
        and Handoff.path(folder, number).exists()
        and _drive_gate(folder, number)
    ):
        driven.add(number)
    # Finish merges in this same tick, including every member of a batch.
    for path in sorted(folder.glob("merge-wanted-*")):
        number = path.name.removeprefix("merge-wanted-")
        # A no-merge tick needs no second sweep of GitHub. Every successful squash
        # either was driven above or left the gate's durable batch receipt.
        if not number.isdigit() or (
            train_reads.tick() is not None
            and number not in driven
            and not (folder / f"batch-merged-{number}").exists()
        ):
            continue
        if gate.read_pull(number)["state"] == "MERGED":
            note = Handoff.read(folder, number)
            if note is not None:
                try:
                    _restack(folder, number, note)
                except gate.Waiting as error:
                    # The child stays listed in the parent's note for the next tick.
                    print(f"stack follow-up {number} waits: {error}", flush=True)


if __name__ == "__main__":
    gate.install_signal_handlers()
    try:
        folder = gate.main_checkout() / gate.CONFIG.state_folder
        if sys.argv[1:] == ["--hand-backs"]:
            _tidy_hand_backs(folder)
        elif len(sys.argv) == 3 and sys.argv[1] == "--start-hand-back" and sys.argv[2].isdigit():
            sys.exit(_start_hand_back(folder, sys.argv[2]))
        elif len(sys.argv) == 1:
            drive(folder)
        else:
            raise gate.Refused("usage: merge_driver.py [--hand-backs | --start-hand-back N]")
    except (gate.Refused, OSError, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"merge driver waits for the next tick: {error}") from error
