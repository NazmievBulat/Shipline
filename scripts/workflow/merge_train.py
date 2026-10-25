"""Persistent merge line, ordering and status."""

import fcntl
import json
import os
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import check_repair
import priority
import train_reads
from merge_handoff import Handoff, authorized

# Planning reads never replace the evidence checked before a merge.
_planning_reads: train_reads.Reads | None = None


def ready(pull: dict[str, Any]) -> bool:
    return pull["state"] == "OPEN" and not pull["isDraft"] and pull["baseRefName"] == "main"


def conflicting(pull: dict[str, Any]) -> bool:
    return pull["mergeable"] == "CONFLICTING" or pull["mergeStateStatus"] == "DIRTY"


def publish_first(number: str | None, batch: dict[str, Any] | None) -> None:
    """Tell the runners' lender which pull request is first, and the branch its batch is tested
    on: their CI and browser jobs get a runner before other pull requests' jobs
    (runner/lend_runners.py). The train is never held up for this note."""
    path = Path.home() / ".local/state/workflow/train-first.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
                json.dump({"number": number, "batch": batch["branch"] if batch else None}, output)
            os.replace(output.name, path)
            # Preserve the lender heartbeat: the gate cannot claim the service is alive.
            # Publishing the identity here makes a batch visible before its dispatch.
            planner = Path.home() / ".local/state/ci-overflow/home/first.json"
            if planner.exists():
                state = json.loads(planner.read_text())
                if not isinstance(state, dict):
                    raise ValueError("planner note is not an object")
                state.update(number=number, batch=batch["branch"] if batch else None)
                with tempfile.NamedTemporaryFile(mode="w", dir=planner.parent, delete=False) as output:
                    json.dump(state, output)
                os.chmod(output.name, 0o644)
                os.replace(output.name, planner)
    except (OSError, ValueError, TypeError) as error:
        print(
            f"could not note the train's first member for the runners: {error}",
            flush=True,
        )


class Train:
    """Persistent line, read and written only while holding merge.lock.

    Calling the gate is the reviewer's declaration that review has passed. Waiting
    entries never push. A fresh green rerun restores a failed head's earlier place.
    `queue` keeps the order of joining; `line()` is the order served, by priority first.
    """

    def __init__(self, folder: Path) -> None:
        from merge_gate import WINDOW_CEILING

        self.path = folder / "merge-train.json"
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.queue: list[str] = data.get("waiting", [])
        self.blocked: dict[str, str] = data.get("blocked", {})
        self.suspended: dict[str, str] = data.get("suspended", {})
        self.conflicts: dict[str, str] = data.get("conflicts", {})
        self.unavailable: list[str] = data.get("unavailable", [])
        self.pending: list[str] = data.get("pending", [])
        self.passing: list[str] = data.get("passing", [])
        self.turn: int = data.get("turn", 0)
        self.refreshable: set[str] = set(data.get("refreshable", []))
        self.approved: dict[str, str] = data.get("approved", {})
        # The first members of the line, each tested on main and the members ahead of it, on a
        # scratch branch of its own (_run_line). A batch or probes saved before it are dropped,
        # and the sweep deletes their branch.
        self.prefixes: dict[str, Any] | None = data.get("prefixes")
        # Older records are cumulative: each member contains every saved member before it.
        # Explicit dependencies let a restored member run beside work already on this main.
        ahead: list[str] = []
        for member in self.prefixes["members"] if self.prefixes else []:
            member.setdefault("ahead", list(ahead))
            ahead.append(member["number"])
        self.restored: list[str] = list(
            dict.fromkeys([*data.get("restored", []), *data.get("proven", {})])
        )
        # How many members are tested at once: one more after each merge, half after a failed
        # prefix, from 1 to WINDOW_CEILING.
        self.window: int = min(max(data.get("window", WINDOW_CEILING), 1), WINDOW_CEILING)
        # A failed prefix whose rerun passed on what main is now, with its member's head: that
        # member's next prefix is this run, not a new one.
        self.proven: dict[str, dict[str, Any]] = data.get("proven", {})
        self.batch_failed: dict[str, dict[str, Any]] = data.get("batch_failed", {})
        # A pull request merging alone whose own E2E or CI run was a selection: the whole
        # browser suite's or every server test's run of its tested merge, on a scratch branch
        # (_whole_suite_run). "workflows" names which; a record without it is the browser's.
        self.suites: dict[str, dict[str, Any]] = data.get("suites", {})
        # How each one's run stood at the last reconcile; not saved.
        self.suite_states: dict[str, str | None] = {}
        self.fixes: list[str] = data.get("fixes", [])
        self.proofs: dict[str, list[Any]] = data.get("proofs", {})
        # Each member's priority as its labels said at the last reconcile.
        self.priorities: dict[str, str] = data.get("priorities", {})
        # The hot paths of shipline.json each member's change touches, read on its head with
        # those hot paths: `{"head": ..., "patterns": [...], "paths": [...]}`.
        self.hot: dict[str, dict[str, Any]] = data.get("hot_paths", {})
        # The head each member's lost-check push made: a check lost on it is not pushed for again.
        self.reruns: dict[str, str] = data.get("reruns", {})
        # Each member's lost check at the last reconcile, with its head, for the tick's wake.
        self.lost: dict[str, dict[str, Any]] = data.get("lost", {})
        # The last scratch run that fell short in a way a new scratch branch retests: its tested
        # tree and why (_retest). Cleared once a scratch run proves its tree.
        self.retest: dict[str, str] | None = data.get("retest")
        # Scratch branches whose records left the line, with the workflows dispatched on them and
        # when: the sweep cancels their runs until it has seen one of each (_sweep_scratch).
        self.dropped: list[dict[str, Any]] = [
            entry
            for entry in data.get("dropped") or []
            if isinstance(entry, dict)
            and isinstance(entry.get("branch"), str)
            and isinstance(entry.get("requested"), list)
            and isinstance(entry.get("dropped"), (int, float))
        ]
        self.scratch = self.scratch_records()
        self.first = self.front()

    def scratch_records(self) -> dict[str, dict[str, Any]]:
        """Every scratch branch the line still names, with its record."""
        records = [
            *(
                member["scratch"]
                for member in (self.prefixes["members"] if self.prefixes else [])
                if member["scratch"]
            ),
            *(failure["batch"] for failure in self.batch_failed.values()),
            *self.proven.values(),
            *self.suites.values(),
        ]
        return {
            record["branch"]: record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("branch"), str)
        }

    def rank(self, number: str) -> tuple[int, int]:
        # Proven fixes share the first place, keeping their order of joining.
        return (
            (0, 0)
            if number in self.fixes
            else (1, priority.rank(self.priorities.get(number, priority.UNLABELLED)))
        )

    def line(self) -> list[str]:
        """The waiting members in the order they are served: the fix of a red main first,
        then by priority, then by the order of joining within one priority, members that
        share a hot path back to back at the place of the earliest of them."""
        return list(self.hot_groups())

    def hot_paths_of(self, number: str) -> set[str]:
        from merge_gate import CONFIG

        record = self.hot.get(number)
        if not record or record.get("patterns") != list(CONFIG.hot_paths):
            return set()
        return set(record.get("paths", []))

    def hot_groups(self) -> dict[str, str | None]:
        """Every member in the order served, with the hot path that put it beside a member of
        its group, or None. Members of one priority that share a hot path, directly or through
        another, make one group; it is served where its earliest member joined."""
        from merge_gate import CONFIG

        served: dict[str, str | None] = {}
        joined = sorted(self.queue, key=self.rank)
        if not CONFIG.hot_paths:
            return dict.fromkeys(joined)
        for number in joined:
            if number in served:
                continue
            group, why = [number], {number: None}
            for current in group:
                for other in joined:
                    if other in why or other in served or self.rank(other) != self.rank(number):
                        continue
                    both = self.hot_paths_of(current) & self.hot_paths_of(other)
                    if shared := [hot for hot in CONFIG.hot_paths if hot in both]:
                        group.append(other)
                        why[other] = shared[0]
                        why[current] = why[current] or shared[0]
            served.update((member, why[member]) for member in sorted(group, key=joined.index))
        return served

    def front(self) -> str | None:
        eligible = [
            number
            for number in self.line()
            if number not in self.suspended
            and number not in self.conflicts
            and number not in self.unavailable
            and (number not in self.blocked or number in self.refreshable)
        ]
        if eligible and eligible[0] in self.pending:
            return next((number for number in eligible if number in self.passing), eligible[0])
        return eligible[0] if eligible else None

    def save(self) -> None:
        from merge_gate import publish_first

        first = self.front()
        if first != self.first:
            self.turn += 1
            self.first = first
        # A record that left the line may have runs GitHub does not list yet.
        current = self.scratch_records()
        watched = {entry["branch"] for entry in self.dropped}
        for branch, record in self.scratch.items():
            # A dispatch sent but never answered may have reached GitHub too.
            sent = sorted({*record.get("requested", []), *record.get("dispatching", [])})
            if branch not in current and branch not in watched and sent:
                self.dropped.append(
                    {"branch": branch, "requested": sent, "dropped": int(time.time())}
                )
        self.scratch = current
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as output:
            json.dump(
                {
                    "waiting": self.queue,
                    "fixes": self.fixes,
                    "proofs": self.proofs,
                    "priorities": self.priorities,
                    "blocked": self.blocked,
                    "suspended": self.suspended,
                    "conflicts": self.conflicts,
                    "unavailable": self.unavailable,
                    "pending": self.pending,
                    "passing": self.passing,
                    "refreshable": sorted(self.refreshable),
                    "turn": self.turn,
                    "approved": self.approved,
                    "prefixes": self.prefixes,
                    "restored": self.restored,
                    "window": self.window,
                    "proven": self.proven,
                    "batch_failed": self.batch_failed,
                    "suites": self.suites,
                    "reruns": self.reruns,
                    "lost": self.lost,
                    "retest": self.retest,
                    "dropped": self.dropped,
                    # Only with hot paths named: without them the line is saved as before.
                    **({"hot_paths": self.hot} if self.hot else {}),
                },
                output,
            )
            temporary = output.name
        os.replace(temporary, self.path)
        publish_first(first, self.front_scratch(first))

    def front_scratch(self, first: str | None) -> dict[str, Any] | None:
        """The scratch branch whose runs decide the first member's turn: its prefix, or the
        whole suite of its tested merge."""
        members = self.prefixes["members"] if self.prefixes else []
        for member in members:
            if member["number"] == first and member["scratch"]:
                return member["scratch"]
        return self.suites.get(first or "")

    def merged_one(self) -> None:
        from merge_gate import WINDOW_CEILING

        self.window = min(self.window + 1, WINDOW_CEILING)

    def failed_prefix(self) -> None:
        self.window = max(self.window // 2, 1)

    def reconcile(self, known: dict[str, Any] | None = None, acting: str | None = None) -> None:
        from merge_gate import CONFIG, _batch_signal, git

        global _planning_reads
        context = train_reads.tick()
        if context is None:
            self._reconcile(known, acting)
            return
        pulls = train_reads.planning_pulls(context)
        # Preserve bulk-only fields (number/body) when the gate supplies its fresh view.
        for number, pull in (known or {}).items():
            pulls[number] = {**pulls.get(number, {}), **pull}
        train_reads.save(context / "planning-pulls.json", pulls)
        health = self.path.parent / "main-status"
        base = train_reads.load(context / "main.json").get("head")
        if not base:
            base = git("ls-remote", "origin", "refs/heads/main").split()[0]
            train_reads.save(context / "main.json", {"head": base})

        def inputs(queue: list[str]) -> list[Any]:
            # Local authorizations and owner questions can change while the tick runs.
            notes = []
            for number in queue:
                root = CONFIG.state(
                    CONFIG.checkout_of(self.path.parent) / ".claude/worktrees" / f"ticket-{number}"
                )
                paths = [
                    *root.glob("*.owner-blocked"),
                    *root.glob("*.interaction-blocked"),
                    *root.glob("*.pushed"),
                ]
                paths += [
                    Handoff.path(self.path.parent, number),
                    self.path.parent / f"merge-review-{number}",
                ]
                notes += [(str(path), path.stat().st_mtime_ns) for path in paths if path.exists()]
            return [
                health.read_text() if health.exists() else "",
                sorted(notes),
                (context / "sessions").read_text(),
            ]

        def signature() -> str:
            return train_reads.digest(
                [self.path.read_text() if self.path.exists() else "", base, pulls,
                 CONFIG.checks.prefix_only]
                + inputs(self.queue)
                # Named hot paths change the order, though nothing on GitHub changed.
                + ([list(CONFIG.hot_paths)] if CONFIG.hot_paths else [])
            )

        previous = train_reads.load(context / "reconciled.json")
        delegated = acting is None or Handoff.path(self.path.parent, acting).exists()
        if delegated and previous.get("signature") == signature():
            self.suite_states = previous["suite_states"]
            return
        # The members the scan reads, blocked ones included.
        queue = list(dict.fromkeys([*self.queue, *self.blocked]))
        seen = inputs(queue)
        reads = train_reads.Reads(self.path.parent, base)
        _planning_reads = reads
        try:
            self._reconcile(known, acting)
            _batch_signal(self)
            reads.finish()
        finally:
            _planning_reads = None
        # A note created or cleared while the scan ran may not be what it read: keep the
        # result only for the inputs it saw, so the next reconcile computes it again. The key
        # is taken first: a note changed after it leaves the key on the old note, a miss.
        key = signature()
        if inputs(queue) != seen:
            return
        train_reads.save(
            context / "reconciled.json",
            {
                "signature": key,
                "at": time.time(),
                "suite_states": self.suite_states,
            },
        )

    def _reconcile(self, known: dict[str, Any] | None = None, acting: str | None = None) -> None:
        from merge_gate import (
            CONFIG,
            HERE,
            Refused,
            Waiting,
            _batch_rerun_green,
            _passing_rollup,
            _proves_on,
            _pull_run_token,
            _suite_state,
            command,
            conflicting,
            failed_checks,
            fix_evidence,
            git,
            jobs,
            lost_check,
            main_health,
            note_lost,
            proves_advisory_fix,
            proves_main_fix,
            read_pull,
            ready,
            runs,
            state_of,
            tested_merge,
            verdict,
        )

        remaining: list[str] = []
        pulls: dict[str, Any] = {}
        suite_states: dict[str, str | None] = {}
        lost: dict[str, dict[str, Any]] = {}
        # A blocked head's CI run that cannot be read, or that GitHub lists no more, is no new
        # evidence: GitHub can lag on its check runs too, so its mark stays.
        marked, self.refreshable = self.refreshable, set()
        # Use the same session state and owner-question notes as the tick. A background
        # job leaves its session running and must not lose its turn merely for waiting.
        sessions = {
            fields[0]: fields[1]
            for line in command("bash", str(HERE / "session"), "list").stdout.splitlines()
            if len(fields := line.split()) >= 2
        }
        self.unavailable = []
        health_file = self.path.parent / "main-status"
        health = health_file.read_text().strip() if health_file.exists() else ""
        # Older state files removed failed entries from waiting altogether.
        for number in dict.fromkeys([*self.queue, *self.blocked]):
            pull = known[number] if known and number in known else read_pull(number)
            pulls[number] = pull
            try:
                repair = check_repair.Repair.read(self.path.parent, number)
            except ValueError:
                repair = None
            if (
                repair is not None
                and pull["state"] == "OPEN"
                and pull["baseRefName"] == "main"
                and (
                    pull["isDraft"] or self.blocked.get(number) not in (None, pull["headRefOid"])
                )
            ):
                # A failing test of its own went to a worker: as a draft, then on the fix's new
                # head until its reviewer calls the gate, it keeps its place, passed over.
                self.blocked.setdefault(number, repair.head)
                self.unavailable.append(number)
                remaining.append(number)
                continue
            if not ready(pull):
                self.blocked.pop(number, None)
                continue
            if number in self.blocked and self.blocked[number] != pull["headRefOid"]:
                # A fix on a new head joins at the back when its reviewer calls the gate.
                # Keep its audit eligible for a checks-finished wake until that claim; a check
                # GitHub lost beside its failure still ends that wait.
                self._failed_lost(number, pull, None, lost, failed=failed_checks(pull))
                continue
            if self.conflicts.get(number) != pull["headRefOid"]:
                self.conflicts.pop(number, None)
            notes = CONFIG.state(
                CONFIG.checkout_of(self.path.parent) / ".claude" / "worktrees" / f"ticket-{number}"
            )
            # The session running the gate is acting now, whatever note an answered permission
            # dialog or a wake by its own job left behind.
            delegated = (
                Handoff.path(self.path.parent, number).exists()
                or (self.path.parent / f"merge-review-{number}").exists()
            )
            available = (
                authorized(self.path.parent, number, pull["headRefOid"])
                if delegated
                else number == acting
            )
            for kind in ("review", "audit"):
                name = f"{kind}-{number}"
                if (
                    not delegated
                    and name in sessions
                    and sessions[name] != "limit"
                    and not any(
                        (notes / f"{name}.{suffix}").exists()
                        for suffix in ("owner-blocked", "interaction-blocked", "pushed")
                    )
                ):
                    available = True
            # GitHub can detect a conflict before any gate attempts a local merge. Keep
            # its queue slot, but let the next available pull proceed while it is resolved.
            if not available or conflicting(pull):
                self.unavailable.append(number)
            if self.suspended.get(number) != f"{pull['headRefOid']}:{health}":
                self.suspended.pop(number, None)
            suite = self.suites.get(number)
            try:
                suite_states[number] = (
                    _suite_state(suite) if suite and suite["member"] == pull["headRefOid"] else None
                )
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence neither blocks it nor lets another take its turn.
                suite_states[number] = None
            if failed_checks(pull):
                self.blocked[number] = pull["headRefOid"]
                self._failed_lost(number, pull, None, lost)
            elif suite_states[number] == "red" and self.blocked.get(number) == pull["headRefOid"]:
                # The gate blocked it when it told its session that a whole suite failed
                # (_whole_suite_run). Until then it keeps its turn, so the finished run
                # changes the turn and the tick wakes that session. A passing rerun lifts it.
                pass
            elif number != acting or number in self.blocked:
                # The rollup alone cannot prove that a rerun passed or tested current main.
                # Missing evidence keeps this entry blocked without holding up other pulls.
                try:
                    ci = runs("ci", pull["headRefOid"], pull=True)
                    parts = jobs(ci[0]) if ci else []
                    state = verdict(ci[0], parts)[0] if ci else "pending"
                    # A red CI run is a failure already, whatever its checks still show.
                    if state == "green" and not _passing_rollup(pull):
                        found = lost_check(pull, ci[:1])
                        if found:
                            lost[number] = {"head": pull["headRefOid"], **found}
                            note_lost(self.path.parent, number, pull["headRefOid"], found)
                    if state == "red":
                        self.blocked[number] = pull["headRefOid"]
                        self._failed_lost(number, pull, ci[:1], lost)
                    elif (
                        not ci
                        and number in marked
                        and self.blocked.get(number) == pull["headRefOid"]
                    ):
                        self.refreshable.add(number)
                    elif state == "green" and number in self.blocked:
                        # Its own green run cannot clear a failure its prefix run found. A
                        # new run or attempt of that prefix, or a fix, is needed first.
                        failure = self.batch_failed.get(number)
                        if failure and failure["pull"] == _pull_run_token(pull["headRefOid"]):
                            if not _batch_rerun_green(failure):
                                remaining.append(number)
                                continue
                            base = git("ls-remote", "origin", "refs/heads/main").split()[0]
                            if _proves_on(failure["batch"], base):
                                # The rerun tested it on what main is now. It gets its earlier
                                # slot back, and that run is its next prefix.
                                self.blocked.pop(number)
                                if number not in self.restored:
                                    self.restored.append(number)
                                if "under" in failure["batch"]:
                                    self.proven[number] = {
                                        **failure["batch"],
                                        "member": pull["headRefOid"],
                                    }
                            else:
                                self.refreshable.add(number)
                            self.batch_failed.pop(number)
                            remaining.append(number)
                            continue
                        self.batch_failed.pop(number, None)
                        base = git("ls-remote", "origin", "refs/heads/main").split()[0]
                        if tested_merge(parts, base, pull["headRefOid"]):
                            self.blocked.pop(number)
                        else:
                            # Still blocked, but its turn may refresh the stale green run.
                            self.refreshable.add(number)
                except Refused, KeyError, TypeError, ValueError, IndexError:
                    # An unread run changes nothing: a green re-run on this same head keeps
                    # its mark, so its turn still refreshes it.
                    if number in marked and self.blocked.get(number) == pull["headRefOid"]:
                        self.refreshable.add(number)
            remaining.append(number)
        self.queue = remaining
        # A label changed while waiting moves its pull request at this turn.
        self.priorities = {number: priority.of(pulls[number].get("labels")) for number in remaining}
        self.read_hot_paths(pulls)
        self.fixes = []
        proofs: dict[str, list[Any]] = {}
        # The update that fixes an advisory on main goes first too, so main turns green.
        if state_of(health) in ("red", "advisory"):
            try:
                base = git("ls-remote", "origin", "refs/heads/main").split()[0]
                current = main_health(base, self.path.parent)
                if current.state in ("red", "advisory"):
                    for number in remaining:
                        if number in self.unavailable or number in self.conflicts:
                            continue
                        # An advisory's fix is proven whatever main it was tested on: main
                        # moves under it while the line keeps merging.
                        advisory = current.state == "advisory"
                        evidence = fix_evidence(
                            pulls[number], "" if advisory else base, current, self.suites.get(number)
                        )
                        if evidence is None:
                            continue
                        judged = self.proofs.get(number)
                        if judged and judged[0] == evidence:
                            proven = judged[1]
                        else:
                            try:
                                proven = (
                                    proves_advisory_fix(pulls[number])
                                    if advisory
                                    else proves_main_fix(pulls[number], base, current)
                                )
                            except Waiting:
                                # An unreadable GitHub proves nothing; judge it again next time.
                                continue
                        proofs[number] = [evidence, proven]
                        if proven:
                            self.fixes.append(number)
                            self.suspended.pop(number, None)
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Missing health or proof must never promote a candidate.
                self.fixes = []
        self.proofs = proofs
        self.suspended = {
            number: token for number, token in self.suspended.items() if number in remaining
        }
        # A member that left the line, or pushed a new head, cannot reuse its isolated run.
        self.batch_failed = {
            number: failure
            for number, failure in self.batch_failed.items()
            if number in remaining and number in self.blocked
        }
        self.suites = {
            number: suite
            for number, suite in self.suites.items()
            if number in remaining and pulls[number]["headRefOid"] == suite["member"]
        }
        self.suite_states = suite_states
        # The acting member's lost check is judged by its own gate call.
        self.lost = {
            number: found
            for number, found in {**self.lost, **lost}.items()
            if (number in remaining or number in self.blocked)
            and found["head"] == pulls[number]["headRefOid"]
            and (number in lost or number == acting)
        }
        self.reruns = {number: head for number, head in self.reruns.items() if number in remaining}
        self.proven = {
            number: scratch
            for number, scratch in self.proven.items()
            if number in remaining and pulls[number]["headRefOid"] == scratch["member"]
        }
        self.restored = [number for number in self.restored if number in remaining]
        self._pending_turns(pulls, suite_states)
        self.save()

    def _failed_lost(
        self,
        number: str,
        pull: dict[str, Any],
        known: list[dict[str, Any]] | None,
        lost: dict[str, dict[str, Any]],
        *,
        failed: bool = True,
    ) -> None:
        """A failed member's check GitHub lost never completes, so its reviewer's checks-finished
        wake would never come: the tick gives it that wake, and the orchestrator is told. One
        not known to have failed failed only when a lost check's run did not pass."""
        from merge_gate import Refused, lost_check, note_lost

        try:
            found = lost_check(pull, known, failed=True)
        except Refused, KeyError, TypeError, ValueError, IndexError:
            # Unreadable runs prove nothing lost; the next reconcile looks again.
            return
        if found and (failed or found["conclusion"] != "success"):
            lost[number] = {"head": pull["headRefOid"], **found, "failed": True}
            note_lost(self.path.parent, number, pull["headRefOid"], found)

    def _pending_turns(self, pulls: dict[str, Any], suite_states: dict[str, str | None]) -> None:
        # Pending entries keep their FIFO slots. Only a green run tested on current main
        # can take their turn; stale green entries still wait for the usual refresh path.
        from merge_gate import (
            CONFIG,
            Refused,
            _passing_rollup,
            _suite_state,
            git,
            images_needed,
            jobs,
            runs,
            tested_merge,
            verdict,
        )

        self.pending = []
        self.passing = []
        if len(self.queue) < 2:
            return
        base = git("ls-remote", "origin", "refs/heads/main").split()[0]
        # A saved prefix proves its member's tree on this main, even after the squash of the
        # member ahead of it moved main.
        members = (
            self.prefixes["members"] if self.prefixes and self.prefixes["base"] == base else []
        )
        batched = {member["number"] for member in members}
        green: dict[str, list[dict[str, Any]]] = {}
        line = self.line()
        for number in line:
            if (
                number in self.blocked
                or number in self.suspended
                or number in self.conflicts
                or number in self.unavailable
            ):
                continue
            pull = pulls[number]
            head = pull["headRefOid"]
            if suite_states.get(number) == "pending":
                # A whole suite of its tested merge still runs, as its own checks would.
                self.pending.append(number)
                continue
            try:
                ci = runs("ci", head, pull=True)
                parts = jobs(ci[0]) if ci else []
                state = verdict(ci[0], parts)[0] if ci else "pending"
                if state == "pending" or not _passing_rollup(pull):
                    self.pending.append(number)
                    continue
                if state != "green":
                    continue
                member = next((m for m in members if m["number"] == number), None)
                if (
                    member
                    and member["scratch"]
                    and (member.get("kept") or number in self.restored)
                ):
                    # A restored prefix's own PR checks stay green while its replacement runs.
                    # A completed independent prefix takes a turn to prove its tree or have
                    # its failure handled by the gate; planning never authorizes its merge.
                    prefix = _suite_state(member["scratch"])
                    if prefix in (None, "pending"):
                        self.pending.append(number)
                        continue
                images = [] if "images" in CONFIG.checks.prefix_only else runs("images", head, pull=True)
                if images:
                    state = verdict(images[0], jobs(images[0]), images=True)[0]
                elif "images" not in CONFIG.checks.prefix_only and images_needed(head, base, pull["statusCheckRollup"]):
                    state = "pending"
                if state == "pending":
                    self.pending.append(number)
                elif state == "green" and (
                    pull["mergeable"] == "MERGEABLE"
                    or (
                        number in batched
                        and self.prefixes
                        and self.prefixes["merged"]
                        and pull["mergeable"] == "UNKNOWN"
                    )
                ):
                    green[number] = parts
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence never grants a later member a turn.
                continue
        # A prefix cannot go past a pending member it contains. Independent kept work can.
        kept = any(member.get("kept") for member in members)
        batched = {
            member["number"]
            for member in members
            if not member["ahead"]
            or (not kept and not set(member["ahead"]) & set(self.pending))
        }
        if self.pending:
            # Only a member behind a pending one takes its turn. A green member ahead of
            # every pending one is first anyway and keeps its single path.
            behind = line[line.index(self.pending[0]) + 1 :]
            for number, parts in green.items():
                if number not in behind:
                    continue
                try:
                    if number in batched or tested_merge(parts, base, pulls[number]["headRefOid"]):
                        self.passing.append(number)
                except Refused, KeyError, TypeError, ValueError, IndexError:
                    continue

    def read_hot_paths(self, pulls: dict[str, Any]) -> None:
        """Which hot paths each member's change touches, read again for a new head only."""
        from merge_gate import BudgetWaiting, CONFIG, Refused, Waiting, changed_files, git

        patterns = list(CONFIG.hot_paths)
        kept = {
            number: record
            for number, record in self.hot.items()
            if number in self.queue
            and record.get("head") == pulls[number]["headRefOid"]
            and record.get("patterns") == patterns
        }
        if patterns:
            base = None
            for number in self.queue:
                if number in kept:
                    continue
                head = pulls[number]["headRefOid"]
                try:
                    base = base or git("ls-remote", "origin", "refs/heads/main").split()[0]
                    paths = {
                        hot for file in changed_files(base, head) for hot in CONFIG.hot_paths_of(file)
                    }
                except BudgetWaiting:
                    raise
                except Waiting:
                    # GitHub stalls: the members not read yet are read at the next turn, not
                    # each waited for under the merge lock.
                    break
                except Refused, IndexError:
                    # Unread, it is served as without hot paths, and read again next time.
                    continue
                kept[number] = {
                    "head": head,
                    "patterns": patterns,
                    "paths": [hot for hot in patterns if hot in paths],
                }
        self.hot = kept

    def suspend(self, number: str, head: str, health: str) -> None:
        self.suspended[number] = f"{head}:{health}"
        self.save()

    def fail(self, number: str, head: str) -> None:
        self.blocked[number] = head
        if number not in self.queue:
            self.queue.append(number)
        self.refreshable.discard(number)
        self.save()

    def remove(self, number: str) -> None:
        self.queue = [entry for entry in self.queue if entry != number]
        self.blocked.pop(number, None)
        self.refreshable.discard(number)
        self.suspended.pop(number, None)
        self.conflicts.pop(number, None)
        self.approved.pop(number, None)
        self.batch_failed.pop(number, None)
        self.suites.pop(number, None)
        self.reruns.pop(number, None)
        self.lost.pop(number, None)
        self.proven.pop(number, None)
        self.restored = [entry for entry in self.restored if entry != number]
        self.save()

    def claim(self, number: str, pull: dict[str, Any]) -> bool:
        from merge_gate import Refused, Waiting, failed_checks, pull_service_wait

        self.approved[number] = pull["headRefOid"]
        self.reconcile({number: pull}, number)
        if failed_checks(pull):
            self.fail(number, pull["headRefOid"])
        if self.blocked.get(number) == pull["headRefOid"] and number not in self.refreshable:
            # It keeps its place, passed over, as any failed member until a green re-run.
            try:
                waits = pull_service_wait(pull, self.path.parent, number, retry=True)
            except Waiting:
                raise
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence of the service proves no wait: refused as before.
                waits = None
            if waits:
                raise Waiting(waits)
            if self.suite_states.get(number) == "red" and not failed_checks(pull):
                raise Refused(
                    "a whole suite run on this pull request's tested merge failed; treat it as "
                    "a failed check: fix and push, or rerun that run and call this gate again"
                )
            failure = self.batch_failed.get(number)
            if failure and not failed_checks(pull):
                raise Refused(
                    f"the {failure['workflow']} run of this pull request's prefix "
                    f"({failure['batch']['branch']}) failed on main and the members ahead of it; "
                    "treat it as a failed check: fix and push, or rerun that run and call this "
                    "gate again"
                )
            raise Refused(
                "this pull request failed checks; fix it before rejoining the merge train, "
                "or, for a flaky failure, call this gate again once a re-run is green"
            )
        if self.blocked.get(number) != pull["headRefOid"]:
            self.blocked.pop(number, None)
        if number not in self.queue:
            self.queue.append(number)
            self.reconcile({number: pull}, number)
        self.save()
        return self.first == number

    def member(self, number: str) -> dict[str, Any]:
        """What the line holds of one member alone."""
        from merge_gate import MEMBER_ENTRIES, MEMBER_MARKS

        return {
            "queued": number in self.queue,
            **{name: getattr(self, name).get(number) for name in MEMBER_ENTRIES},
            **{name: number in getattr(self, name) for name in MEMBER_MARKS},
        }

    def admit(self, view: "OwnView") -> None:
        """Take a member's own part from its view, keeping every other member as saved."""
        from merge_gate import MEMBER_ENTRIES

        number = view.number
        if view.left:
            self.queue = [entry for entry in self.queue if entry != number]
        if number in view.queue and number not in self.queue:
            self.queue.append(number)
            # Whether its checks still run is the tick's to read with the others': until then
            # it does not take the turn from a member whose run is green.
            if number not in self.pending:
                self.pending.append(number)
        for name in MEMBER_ENTRIES:
            entries, judged = getattr(self, name), getattr(view, name)
            entries.pop(number, None)
            if number in judged:
                entries[number] = judged[number]
        self.unavailable = [entry for entry in self.unavailable if entry != number]
        self.fixes = [entry for entry in self.fixes if entry != number]
        self.refreshable.discard(number)
        self.unavailable += [number] if number in view.unavailable else []
        self.fixes += [number] if number in view.fixes else []
        self.restored = [entry for entry in self.restored if entry != number]
        self.restored += [number] if number in view.restored else []
        self.refreshable |= {number} & view.refreshable
        self.save()


class OwnView(Train):
    """One member's part of the line, judged as `claim` judges it, apart from the other members.

    A handoff judges its own pull request with it before it takes the merge lock: it reads the
    saved line without the lock, reads GitHub only about this member and main, and saves
    nothing. The line takes its result under the lock (Train.admit); the other members' order is
    left to the turn of the line, whose merge driver reads them.
    """

    def __init__(self, folder: Path, number: str) -> None:
        from merge_gate import MEMBER_ENTRIES

        super().__init__(folder)
        self.number = number
        # Whether the member left the queue while judged: it joins again at the back.
        self.left = False
        # Whether the line's judgment of the member finished, so its result can be taken.
        self.judged = False
        self.queue = [entry for entry in self.queue if entry == number]
        for name in MEMBER_ENTRIES:
            entries = getattr(self, name)
            setattr(self, name, {number: entries[number]} if number in entries else {})
        self.unavailable = [entry for entry in self.unavailable if entry == number]
        self.fixes = [entry for entry in self.fixes if entry == number]
        self.restored = [entry for entry in self.restored if entry == number]
        self.refreshable &= {number}
        self.pending = []
        self.passing = []
        self.first = self.front()

    def reconcile(self, known: dict[str, Any] | None = None, acting: str | None = None) -> None:
        self.judged = False
        self._reconcile(known, acting)
        if self.number not in self.queue:
            self.left = True
        self.judged = True

    def save(self) -> None:
        self.first = self.front()


def _run_token(run: dict[str, Any]) -> str:
    return f"{run['id']}:{run.get('run_attempt', 1)}"


def _pull_run_token(head: str) -> str:
    from merge_gate import _run_token, runs

    return "/".join(
        _run_token(run)
        for workflow in ("ci", "images")
        for run in runs(workflow, head, pull=True)[:1]
    )


def _advance_line(train: Train, number: str, base: str) -> None:
    """The single path squashed the first member: the prefixes behind it stand on the new main
    when main is exactly that squash, and their window has room for the next members.

    Its own CI may have skipped a job main always runs: the next squash waits for main's own
    checks as after any single merge, so this squash does not count among the line's merges."""
    from merge_gate import _drop_line, _move_prefixes, _refill, git

    train.merged_one()
    train.save()
    record = train.prefixes
    git("fetch", "origin", "main")
    actual = git("rev-parse", "origin/main")
    if context := train_reads.tick():
        train_reads.save(context / "main.json", {"head": actual})
    if record is not None:
        member = next((m for m in record["members"] if m["number"] == number), None)
        if (
            record["base"] != base
            or member is None
            or git("show", "-s", "--format=%P", actual) != base
            or git("rev-parse", f"{actual}^{{tree}}") != member["tree"]
        ):
            _drop_line(train)
        else:
            _move_prefixes(train, number, actual)
            train.save()
    _refill(train, actual)


def _prefix_summary(scratch: dict[str, Any] | None) -> str:
    """A prefix's state in a word, from its runs as last read: "own checks" for a first member
    that merges by its own CI, then "not pushed", "starting", "running", "failed" or "passed"."""
    from merge_gate import _queue_recovery_pending

    if scratch is None:
        return "own checks"
    if not scratch["pushed"]:
        return "not pushed"
    seen = {run[0]: run for run in scratch.get("signal", []) if len(run) == 5}
    recovering = {
        workflow for workflow, run in seen.items()
        if _queue_recovery_pending(scratch, {
            "id": run[1], "run_attempt": run[2], "status": run[3], "conclusion": run[4]
        })
    }
    if any(
        run[3] == "completed" and run[4] != "success"
        for workflow, run in seen.items() if workflow not in recovering
    ):
        return "failed"
    if recovering:
        return "running"
    needed = set(scratch["workflows"])
    if scratch.get("image_source"):
        needed.add("images")
    if needed - set(seen):
        return "starting"
    if all(run[3] == "completed" for run in seen.values()):
        return "passed"
    return "running"


def train_status(folder: Path) -> int:
    # A shadow run (`tick --shadow`) only reads: the line as last saved, never the lock, which
    # the installation's own gate may want, nor a reconcile, which pushes and saves.
    from merge_gate import CONFIG, Train, _batch_signal, _prefix_summary, state_of

    shadow = bool(os.environ.get("SHIPLINE_SHADOW"))
    if not shadow:
        folder.mkdir(parents=True, exist_ok=True)
    with nullcontext() if shadow else (folder / "merge.lock").open("w") as lock:
        locked = False
        if lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                # A gate holds the lock, for a refresh through its push hooks. The tick reads the
                # last saved line, written whole, and a later tick reconciles it.
                pass
        train = Train(folder)
        if locked:
            train.reconcile()
            if train_reads.tick() is None:
                _batch_signal(train)
        published = folder / "main-status"
        print(
            json.dumps(
                {
                    # Main's state as last published: green, pending, red, advisory or unknown.
                    "main": (state_of(published.read_text()) or None)
                    if published.exists()
                    else None,
                    "first": train.first,
                    "prefix_only": CONFIG.checks.prefix_only,
                    # Keep failed entries' slots internally, but let the tick deliver their
                    # ordinary checks-finished wake instead of waiting for a train turn.
                    "waiting": [
                        number
                        for number in train.line()
                        if number not in train.blocked or number in train.refreshable
                    ],
                    "blocked": list(train.blocked),
                    # The priority each is served at: a proven fix of red main is a blocker.
                    "priorities": {
                        number: "blocker"
                        if number in train.fixes
                        else train.priorities.get(number, priority.UNLABELLED)
                        for number in train.line()
                    },
                    # The hot path that served each member beside one of its group.
                    **(
                        {
                            "hot_paths": {
                                number: hot
                                for number, hot in train.hot_groups().items()
                                if hot is not None
                            }
                        }
                        if CONFIG.hot_paths
                        else {}
                    ),
                    "turn": train.turn,
                    # A check GitHub lost on a member's head: the tick wakes its reviewer.
                    "lost": train.lost,
                    # How many members are tested at once, and each one's prefix with its runs
                    # as last read: a first member that merges by its own checks has none.
                    "window": train.window,
                    "prefixes": [
                        {
                            "number": member["number"],
                            "branch": member["scratch"]["branch"] if member["scratch"] else None,
                            "state": _prefix_summary(member["scratch"]),
                            "kept": bool(member.get("kept")),
                            "queued_retries": (member["scratch"] or {}).get("queued_retries", {}),
                            "image_source": (
                                {"member": source.get("number"), "tree": source["tree"],
                                 "run": next((run[1] for run in member["scratch"].get("signal", [])
                                              if len(run) == 5 and run[0] == "images"), None)}
                                if (source := (member["scratch"] or {}).get("image_source")) else None
                            ),
                            "runs": [
                                {
                                    "workflow": run[0],
                                    "id": run[1],
                                    "status": run[3],
                                    "conclusion": run[4],
                                }
                                for run in (member["scratch"] or {}).get("signal", [])
                                if len(run) == 5
                            ],
                        }
                        for member in (train.prefixes["members"] if train.prefixes else [])
                    ],
                }
            )
        )
    return 0
