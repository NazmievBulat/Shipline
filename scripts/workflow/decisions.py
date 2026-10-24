"""Every decision of the orchestrator is a record of a fixed shape, written by `decide` (RULES.md,
"For the orchestrator session"), never as chat alone.

    decide start N [--after M] [--owner WORDS] [--evidence TEXT]
    decide hold N --after M [--owner WORDS] [--evidence TEXT]
    decide answer N --area AREA --question TEXT --meaning TEXT (--reply WORDS | --owner WORDS)
    decide allowance N --round R --owner WORDS
    decide hand-back N --session NAME --judgment TEXT --message TEXT
    decide restart N --session NAME --judgment TEXT --message TEXT
    decide program N claude|codex|cloud [--evidence TEXT]
    decide --metrics SINCE UNTIL      counts by kind, hold episodes and answers per ticket, as
                                      JSON; SINCE and UNTIL in seconds or as ISO times

Each record goes to `.local/workflow/decisions.jsonl` of the main checkout, one JSON line: a
stable id, the kind, the pull request, the choice, the rule it follows (a heading of RULES.md),
the evidence, whether it is delegated or the owner's own word (with his exact words), the UTC
time and the orchestrator session (`.local/workflow/orchestrator`). A refusal is a record too.

Each kind runs its checks first and refuses with the reason when one fails; then it writes a
pending line, does the act itself and writes the line that closes it, so record and act cannot
drift apart. The record's line is printed, for the orchestrator to quote in a message:

- `start`: a draft without the label `workflow`, with a priority label, the next in priority and
  age, while the 5-hour window allows a start and no running ticket shares its files; with
  `--after M` a start that shares files with M becomes a hold. A ticket marked `Start: owner
  only` needs the owner's word, which lifts the order and overlap checks too, never the usage
  window's. The act adds the label.
- `hold`: N waits for M, which shares its files (another reason needs the owner's word). The act
  writes `N after M  # <id>` into `start-plan`. The hold ends, and its line goes, when either
  closes or N gets the label; `decide` and the tick's pick end it.
- `answer`: an answer in money, privacy, legal text or text sent to a model needs the owner's
  exact words; a product decision may be delegated. The act adds the entry under `Answers given
  for the owner` in the description, where the reviewer reads it.
- `allowance`: the owner's words, for exactly the next independent round. The act writes
  `review-N.allow-round`.
- `hand-back`, `restart`: the judgment, the target session and the message; nothing else is done.
- `program`: the `Worker: Codex (a bug fix with no new UI)` or `Worker: cloud` line in the
  description, and `worker-program-N`, which the tick reads for the next worker of N. The tick
  keeps its screen checks.

A decision interrupted after its pending line is finished by the next call of `decide`, of any
kind, before anything else: a pending start is checked again against the current head, usage
and files; every other act is repeated, on the description and files as they are now, so edits
made meanwhile are kept, and only a cut-off copy of its own entry is replaced. Only `decide`
writes the record file, under its lock: a line cut off mid-write is a decision's own and is
dropped. The tick takes the same lock for its starts and skips a ticket with an unfinished one.

    decisions.py unfinished              the pull requests with an unfinished decision
    decisions.py wake                    for the wake: held tickets, and the answers given and
                                         starts refused since the last check-in
    decisions.py wake-told CURSOR        the wake delivered what it was given up to CURSOR
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import priority
import workflow_config
from github_budget import Limited, call

KINDS = ("start", "hold", "answer", "allowance", "hand-back", "restart", "program")
# The heading of RULES.md each kind follows.
RULES = {
    "start": "For the orchestrator session",
    "hold": "For the orchestrator session",
    "answer": "For the orchestrator session",
    "allowance": '"Review pull request N"',
    "hand-back": "For the orchestrator session",
    "restart": "For the orchestrator session",
    "program": "For the orchestrator session",
}
PROGRAM_RULES = {
    "claude": "For the orchestrator session",
    "codex": "Codex workers",
    "cloud": "Cloud workers",
}
# The subject areas of an answer, and whether only the owner answers in it.
AREAS = {
    "money": True,
    "privacy": True,
    "legal": True,
    "model-text": True,
    "product": False,
}
PROGRAMS = {
    "claude": None,
    "codex": "Worker: Codex (a bug fix with no new UI)",
    "cloud": "Worker: cloud",
}
ANSWERS = "Answers given for the owner"
ANSWERS_HEADING = re.compile(r"^#{1,6}[ \t]+Answers given for the owner[ \t]*:?[ \t]*$")
ANY_HEADING = re.compile(r"^#{1,6}[ \t]")
RECORDS = "decisions.jsonl"
CURSOR = "decisions.wake-cursor"
LOCK = "shipline-decide.lock"
# How long a call waits for the lock: the tick holds it while it starts workers.
LOCK_SECONDS = 300
# The answers and refused starts one check-in tells at most; the rest are named by their count.
TOLD_EVENTS = 10

CONFIG = workflow_config.current()
LABEL = CONFIG.labels.workflow
here = Path(__file__).resolve().parent


class Refused(Exception):
    """A check failed: the decision is recorded as refused, with this reason."""


class AlreadyStarted(Refused):
    """A start of a ticket that has the label: recorded with this reason, told as started."""


class Failed(Exception):
    """GitHub or a file could not be read or written: nothing new is decided."""


def now() -> int:
    return int(os.environ.get("WORKFLOW_NOW") or time.time())


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def moment_of(text: str) -> float:
    """SINCE or UNTIL: seconds, or an ISO time (UTC when it names no zone)."""
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        return float(text)
    when = datetime.fromisoformat(text)
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()


def duration(seconds: float) -> str:
    """ "2h17m", as the wake writes it."""
    seconds = max(int(seconds), 0)
    return f"{seconds // 3600}h{seconds % 3600 // 60}m"


def has_line(body: str, mark: str) -> bool:
    return any(line.strip() == mark for line in body.splitlines())


def write_file(path: Path, text: str) -> None:
    """Whole or not at all, readable by the owner alone."""
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as out:
        out.write(text)
    os.chmod(out.name, 0o600)
    os.replace(out.name, path)


# The records.


def read_lines(notes: Path) -> list[dict[str, Any]]:
    """The whole lines of the record file, in order. A line cut off mid-write is not one."""
    try:
        text = (notes / RECORDS).read_text()
    except FileNotFoundError:
        return []
    lines = []
    for line in text.split("\n")[:-1]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            lines.append(record)
    return lines


def records(notes: Path) -> dict[str, dict[str, Any]]:
    """Each decision as its last line says it is, in the order the decisions were made."""
    found: dict[str, dict[str, Any]] = {}
    for line in read_lines(notes):
        found[line["id"]] = line
    return found


def append(notes: Path, record: dict[str, Any]) -> None:
    """One line in one write, on disk before the act it announces."""
    descriptor = os.open(notes / RECORDS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(record) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def repair(notes: Path) -> None:
    """Drop a line cut off mid-write. Only `decide` writes the file, under its lock, so it is a
    decision's own: its pending line, whose act had not begun, or the line closing a pending
    record, which this call finishes."""
    path = notes / RECORDS
    try:
        text = path.read_bytes()
    except FileNotFoundError:
        return
    if text and not text.endswith(b"\n"):
        cut = text.rfind(b"\n") + 1
        with path.open("r+b") as file:
            file.truncate(cut)
        print(f"dropped a record line cut off mid-write: {text[cut:][:60]!r}", file=sys.stderr)


def unfinished(notes: Path) -> set[str]:
    """The pull requests with a decision whose act has not finished."""
    return {
        str(record["number"])
        for record in records(notes).values()
        if record.get("status") == "pending"
    }


def open_holds(notes: Path) -> list[dict[str, Any]]:
    return [
        record
        for record in records(notes).values()
        if record.get("kind") == "hold" and record.get("status") == "done"
    ]


def plan_without(notes: Path, identity: str) -> None:
    """`start-plan` without the line of this hold; every other line kept as it is now."""
    path = notes / "start-plan"
    try:
        lines = path.read_text().splitlines(keepends=True)
    except FileNotFoundError:
        return
    kept = [line for line in lines if f"# {identity}" not in line]
    if kept != lines:
        write_file(path, "".join(kept))


def close_holds(notes: Path, open_pulls: dict[str, dict[str, Any]], moment: int) -> None:
    """End each hold whose ticket or the one it waits for closed, or whose ticket started.
    The caller holds the lock of `decide`."""
    for hold in open_holds(notes):
        number, after = str(hold["number"]), str(hold["details"]["after"])
        if number not in open_pulls:
            because = f"{number} closed"
        elif after not in open_pulls:
            because = f"{after} closed"
        elif LABEL in {label.get("name") for label in open_pulls[number].get("labels") or []}:
            because = f"{number} started"
        else:
            continue
        plan_without(notes, hold["id"])
        append(
            notes, {**hold, "status": "ended", "ended_at": iso(moment), "ended_because": because}
        )


# GitHub, the sessions and the files.


@dataclass
class Context:
    notes: Path
    moment: int
    session: str
    pulls: dict[str, dict[str, Any]] | None = None
    queue: Any = None

    def open_pulls(self) -> dict[str, dict[str, Any]]:
        """The open pull requests, read once a call, as the tick's snapshot has them."""
        if self.pulls is None:
            import tick_snapshot

            done = gh(
                "pr", "list", "--state", "open", "--limit", "1000", "--json", tick_snapshot.FIELDS
            )
            listed = json.loads(done.stdout)
            if not isinstance(listed, list):
                raise Failed("the list of pull requests could not be read")
            self.pulls = {
                str(pull["number"]): pull for pull in listed if pull.get("state", "OPEN") == "OPEN"
            }
        return self.pulls

    def pull(self, number: int) -> dict[str, Any]:
        found = self.open_pulls().get(str(number))
        if found is None:
            raise Refused(f"{number} is not an open pull request")
        return found

    def starts(self) -> Any:
        """The waiting and running tickets, as the tick's pick sees them."""
        if self.queue is None:
            import ticket_starts

            self.queue = ticket_starts.Queue.read(CONFIG.main, self.open_pulls())
            self.queue.look()
        return self.queue


def gh(*arguments: str) -> subprocess.CompletedProcess[str]:
    done = call("decide", *arguments, background=False)
    if done.returncode != 0:
        raise Failed(f"gh {' '.join(arguments[:2])} failed: {done.stderr.strip()}")
    return done


def set_body(number: int, body: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".body-{number}.", dir=CONFIG.state())
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write(body)
        gh("api", "-X", "PATCH", f"{CONFIG.api}/pulls/{number}", "-F", f"body=@{name}")
    finally:
        os.unlink(name)


def body_now(number: int) -> str:
    """The description as GitHub has it now: an edit made since this call began is kept."""
    import tick_snapshot

    done = gh("pr", "view", str(number), "--json", tick_snapshot.FIELDS)
    found = json.loads(done.stdout)
    if not isinstance(found, dict) or found.get("state", "OPEN") != "OPEN":
        raise Refused(f"{number} is not an open pull request")
    return found.get("body") or ""


def same_text(first: str, second: str) -> bool:
    """The same lines, whatever their line ends and the newlines at the end."""
    return first.replace("\r\n", "\n").rstrip("\n") == second.replace("\r\n", "\n").rstrip("\n")


def labels_of(pull: dict[str, Any]) -> set[str]:
    return {label.get("name") for label in pull.get("labels") or []}


def sessions() -> set[str]:
    done = subprocess.run(
        ["bash", str(here / "session"), "list"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if done.returncode != 0:
        raise Failed(f"session list failed: {done.stderr.strip()}")
    return {line.split()[0] for line in done.stdout.splitlines() if line.split()}


def rounds(number: int) -> tuple[int, int]:
    """The next independent round, one above the highest published, and how many rounds were
    sent back, from every page of the reviews, as the tick counts them for its reviewer starts."""
    text = gh("api", "--paginate", f"{CONFIG.api}/pulls/{number}/reviews").stdout
    decoder, position, published, sent_back = json.JSONDecoder(), 0, [0], 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        page, position = decoder.raw_decode(text, position)
        for review in page if isinstance(page, list) else []:
            body = review.get("body") or "" if isinstance(review, dict) else ""
            marker = r"<!-- workflow-review round=([0-9]+)( result=(sent-back|passed))? -->"
            if found := re.match(marker, body):
                published.append(int(found[1]))
                # A marker from before outcomes were recorded counts as sent back.
                sent_back += found[3] != "passed"
    return max(published) + 1, sent_back


# The checks: each returns the kind, the choice, its details and its evidence, or refuses.


@dataclass
class Plan:
    kind: str
    choice: str
    details: dict[str, Any]
    evidence: list[str]


def check_start(context: Context, number: int, after: int | None, owner: str | None) -> Plan:
    import ticket_starts

    pull = context.pull(number)
    # Before the draft check: a started ticket its worker marked ready is started all the same.
    if LABEL in labels_of(pull):
        raise AlreadyStarted(f"already started: {number} has the label {LABEL}")
    if not pull.get("isDraft"):
        raise Refused(f"{number} is not a draft")
    names = CONFIG.labels.priority
    if not any(names[name] in labels_of(pull) for name in priority.PRIORITIES):
        raise Refused(f"{number} has no priority label")
    if waits := ticket_starts.usage_wait(context.moment):
        raise Refused(waits)
    body = pull.get("body") or ""
    if has_line(body, ticket_starts.OWNER_MARK) and owner is None:
        raise Refused(f"{number} is marked {ticket_starts.OWNER_MARK}: it needs the owner's word")
    head = pull.get("headRefOid")
    evidence = [f"head {head}", f"priority {priority.of(pull.get('labels'))}"]
    details: dict[str, Any] = {"head": head, "after": after}
    if owner is not None:
        evidence.append("the owner's word: no order or overlap check")
        return Plan("start", f"start {number}", details, evidence)

    queue = context.starts()
    if queue.held_all:
        raise Refused("hold all in start-plan")
    if queue.unclear:
        raise Refused(f"start-plan line not understood: {queue.unclear[0]}")
    if str(number) in queue.busy:
        raise Refused(f"{number} runs already: a session, a cloud record or the merge train")
    found = queue.files_of(pull)
    reason = queue.reason(pull, found)
    if reason is not None and not reason.startswith("shares "):
        raise Refused(reason)
    if reason is not None:
        shared = queue.sharing(found)
        sharing = "; ".join(f"shares {path} with {other}" for other, path in shared)
        if after is None:
            raise Refused(f"{sharing}: a start needs no shared files, or --after for a hold")
        if str(after) not in {other for other, _ in shared}:
            raise Refused(f"{sharing}, not with {after}")
        return hold_plan(context, pull, after, found, evidence, via="start")
    # Ahead of it, a ticket that could start now goes first.
    for other in queue.candidates:
        if other["number"] == number or ticket_starts.order(other) >= ticket_starts.order(pull):
            continue
        if queue.reason(other, queue.files.of(other, queue.fetched)) is None:
            name = priority.of(other.get("labels"))
            raise Refused(f"out of order: {other['number']} goes first (priority {name})")
    evidence.append("next in priority and age")
    evidence.append(f"files {', '.join(found or [])}; no running ticket shares one")
    if after is not None:
        evidence.append(f"--after {after} not needed: nothing shared")
    return Plan("start", f"start {number}", details, evidence)


def hold_plan(
    context: Context,
    pull: dict[str, Any],
    after: int,
    found: list[str] | None,
    evidence: list[str],
    via: str = "hold",
) -> Plan:
    import ticket_starts

    number = pull["number"]
    theirs = context.starts().files_of(context.pull(after))
    shared = list(
        dict.fromkeys(
            both
            for mine in found or []
            for their in theirs or []
            if (both := ticket_starts.overlap(mine, their))
        )
    )
    for hold in open_holds(context.notes):
        if hold["number"] == number and hold["details"]["after"] == after:
            raise Refused(f"{number} is already held after {after} ({hold['id']})")
    evidence.append(
        f"shares {', '.join(shared)} with {after}" if shared else f"shares no file with {after}"
    )
    return Plan(
        "hold",
        f"hold {number} after {after}",
        {"after": after, "shared": shared, "via": via},
        evidence,
    )


def check_hold(context: Context, number: int, after: int, owner: str | None) -> Plan:
    pull = context.pull(number)
    if after == number:
        raise Refused(f"{number} cannot wait for itself")
    context.pull(after)
    if not pull.get("isDraft") or LABEL in labels_of(pull):
        raise Refused(f"{number} is not a draft waiting for its start")
    queue = context.starts()
    found = queue.files_of(pull)
    theirs = queue.files_of(context.pull(after))
    plan = hold_plan(context, pull, after, found, [])
    if owner is None:
        if found is None or theirs is None:
            raise Refused(f"the files of {number if found is None else after} are not known yet")
        if not plan.details["shared"]:
            raise Refused(
                f"{number} shares no file with {after}: another reason needs the owner's word"
            )
    return plan


def check_answer(
    context: Context,
    number: int,
    area: str,
    question: str,
    meaning: str,
    reply: str | None,
    owner: str | None,
) -> Plan:
    if area not in AREAS:
        raise Refused(f"{area} is not a subject area: one of {', '.join(AREAS)}")
    if AREAS[area] and owner is None:
        raise Refused(f"{area}: only the owner answers in this area; give his exact words")
    words = owner if owner is not None else reply
    if words is None:
        raise Refused("an answer needs its words: --reply, or the owner's own with --owner")
    if owner is not None and reply is not None and reply != owner:
        raise Refused("the reply is the owner's words: --reply differs from --owner")
    for name, text in (("question", question), ("meaning", meaning), ("reply", words)):
        if not text.strip() or "\n" in text or "\r" in text:
            raise Refused(f"the {name} is one line of words")
    body = context.pull(number).get("body") or ""
    date = datetime.fromtimestamp(context.moment, UTC).strftime("%Y-%m-%d")
    entry = f'- {date}: {question.strip()}: "{words.strip()}", {meaning.strip().rstrip(".")}.'
    if entry in body.replace("\r\n", "\n").split("\n"):
        raise Refused(f"the description already has the entry: {entry}")
    return Plan(
        "answer",
        entry,
        {
            "area": area,
            "question": question.strip(),
            "reply": words.strip(),
            "meaning": meaning.strip(),
            "entry": entry,
        },
        [f"the description of {number}, under {ANSWERS}"],
    )


def check_allowance(context: Context, number: int, round: int, owner: str | None) -> Plan:
    if owner is None:
        raise Refused("an allowance needs the owner's words (--owner)")
    context.pull(number)
    wanted, sent_back = rounds(number)
    if sent_back < 2:
        times = "round" if sent_back == 1 else "rounds"
        raise Refused(
            f"{number} has {sent_back} {times} sent back: an allowance is needed only after two"
        )
    if round != wanted:
        raise Refused(f"the next independent round is {wanted}, not {round}")
    return Plan(
        "allowance",
        f"round {round} of {number}",
        {"round": round},
        [f"published rounds up to {wanted - 1}, read from every page of the reviews"],
    )


def check_message(
    context: Context, kind: str, number: int, session: str, judgment: str, message: str
) -> Plan:
    context.pull(number)
    if not re.fullmatch(rf"(ticket|review|audit|codex)-{number}", session):
        raise Refused(f"{session} is not a session of pull request {number}")
    if kind == "hand-back" and session not in sessions():
        raise Refused(f"{session} is not running")
    if not judgment.strip() or not message.strip():
        raise Refused(f"a {kind} needs its judgment and its message")
    return Plan(
        kind,
        f"{kind} to {session}",
        {"session": session, "judgment": judgment, "message": message},
        [],
    )


def check_program(context: Context, number: int, program: str) -> Plan:
    if program not in PROGRAMS:
        raise Refused(f"{program} is not a worker program: one of {', '.join(PROGRAMS)}")
    pull = context.pull(number)
    if not pull.get("isDraft"):
        raise Refused(f"{number} is not a draft: its next worker is a fix round after a send-back")
    return Plan(
        "program",
        f"{program} for {number}",
        {"program": program, "line": PROGRAMS[program]},
        ["the tick keeps its screen checks for Codex and the cloud"],
    )


# The acts: each can run again, on the description and files as they are now.


def with_entry(body: str, entry: str) -> str:
    """The description with the entry under its answers heading. A cut-off copy of the entry
    is completed; every other line stays as it is."""
    lines = body.replace("\r\n", "\n").split("\n")
    if entry in lines:
        return body
    start = entry[: entry.index(': "') + 2]
    for index, line in enumerate(lines):
        if line.startswith(start) and entry.startswith(line.rstrip()):
            lines[index] = entry
            return "\n".join(lines)
    heading = next((i for i, line in enumerate(lines) if ANSWERS_HEADING.match(line.strip())), None)
    if heading is None:
        return body.rstrip("\n") + f"\n\n## {ANSWERS}\n\n{entry}\n"
    end = next(
        (i for i in range(heading + 1, len(lines)) if ANY_HEADING.match(lines[i].strip())),
        len(lines),
    )
    last = max((i for i in range(heading + 1, end) if lines[i].strip()), default=heading)
    at = last + 1
    if last == heading:
        lines[at:at] = ["", entry]
    else:
        lines[at:at] = [entry]
    return "\n".join(lines)


def with_program(body: str, line: str | None) -> str:
    kept = [
        text
        for text in body.replace("\r\n", "\n").split("\n")
        if text.strip() not in PROGRAMS.values()
    ]
    text = "\n".join(kept).rstrip("\n")
    return f"{text}\n\n{line}\n" if line else f"{text}\n"


def act(context: Context, record: dict[str, Any]) -> None:
    kind, number, details = record["kind"], record["number"], record["details"]
    if kind == "start":
        gh("api", "-X", "POST", f"{CONFIG.api}/issues/{number}/labels", "-f", f"labels[]={LABEL}")
        (context.notes / "last-start").write_text(f"{now()}\n")
    elif kind == "hold":
        path = context.notes / "start-plan"
        try:
            text = path.read_text()
        except FileNotFoundError:
            text = ""
        line = f"{number} after {details['after']}  # {record['id']}"
        lines = text.splitlines()
        ours = [index for index, old in enumerate(lines) if f"# {record['id']}" in old]
        if ours and lines[ours[0]] == line:
            return
        if ours:
            lines[ours[0]] = line
        else:
            lines.append(line)
        write_file(path, "\n".join(lines) + "\n")
    elif kind == "answer":
        context.pull(number)
        body = body_now(number)
        if not same_text(changed := with_entry(body, details["entry"]), body):
            set_body(number, changed)
    elif kind == "allowance":
        write_file(context.notes / f"review-{number}.allow-round", f"{details['round']}\n")
    elif kind == "program":
        context.pull(number)
        body = body_now(number)
        if not same_text(changed := with_program(body, details["line"]), body):
            set_body(number, changed)
        write_file(context.notes / f"worker-program-{number}", f"{details['program']}\n")


def finish(context: Context, record: dict[str, Any]) -> dict[str, Any]:
    """Finish an interrupted decision: a start is checked again, every other act repeated. One
    that can no longer be done, as on a pull request closed meanwhile, is closed as refused."""
    try:
        if record["kind"] == "start":
            pull = context.open_pulls().get(str(record["number"]))
            if pull is None or LABEL not in labels_of(pull):
                plan = check_start(
                    context, record["number"], record["details"].get("after"), record["owner_words"]
                )
                if plan.kind != "start":
                    raise Refused(f"it is now {plan.choice}; decide again")
                act(context, record)
        else:
            act(context, record)
    except Refused as refusal:
        closed = {**record, "status": "refused", "reason": f"checked again: {refusal}"}
        append(context.notes, closed)
        return closed
    done = {**record, "status": "done"}
    append(context.notes, done)
    return done


@contextmanager
def locked() -> Iterator[None]:
    common = subprocess.run(
        ["git", "-C", str(CONFIG.main), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if common.returncode != 0:
        raise Failed(f"cannot find the git common directory: {common.stderr.strip()}")
    with (Path(common.stdout.strip()) / LOCK).open("a") as lock:
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise Failed("the tick holds the lock of decide; try again") from None
                time.sleep(1)
        yield


def decide(kind: str, number: int, options: argparse.Namespace) -> int:
    notes = CONFIG.state()
    notes.mkdir(parents=True, exist_ok=True)
    try:
        session = (notes / "orchestrator").read_text().splitlines()[0].strip()
    except OSError, IndexError:
        session = ""
    if not session:
        print("the orchestrator is not registered in .local/workflow/orchestrator", file=sys.stderr)
        return 3
    # Blank words are no word of the owner's.
    owner = options.owner if options.owner and options.owner.strip() else None
    with locked():
        context = Context(notes, now(), session)
        repair(notes)
        for record in list(records(notes).values()):
            if record.get("status") == "pending":
                finished = finish(context, record)
                print(f"finished an interrupted decision: {json.dumps(finished)}", file=sys.stderr)
                # The checks below read the state that act left.
                context.pulls = context.queue = None
        close_holds(notes, context.open_pulls(), context.moment)
        try:
            if kind == "start":
                plan = check_start(context, number, options.after, owner)
            elif kind == "hold":
                plan = check_hold(context, number, options.after, owner)
            elif kind == "answer":
                plan = check_answer(
                    context,
                    number,
                    options.area,
                    options.question,
                    options.meaning,
                    options.reply,
                    owner,
                )
            elif kind == "allowance":
                plan = check_allowance(context, number, options.round, owner)
            elif kind in ("hand-back", "restart"):
                plan = check_message(
                    context, kind, number, options.session, options.judgment, options.message
                )
            else:
                plan = check_program(context, number, options.program)
            refusal = None
        except Refused as refused:
            refusal = str(refused)
            started = isinstance(refused, AlreadyStarted)
            plan = Plan(kind, f"{kind} {number}", {"already_started": True} if started else {}, [])
        stamp = datetime.fromtimestamp(context.moment, UTC).strftime("%Y%m%dT%H%M%SZ")
        rule = PROGRAM_RULES.get(plan.details.get("program", ""), "") or RULES[plan.kind]
        record = {
            "id": f"{plan.kind}-{number}-{stamp}-{secrets.token_hex(3)}",
            "status": "pending" if refusal is None else "refused",
            "kind": plan.kind,
            "number": number,
            "choice": plan.choice,
            "rule": rule,
            "evidence": ([options.evidence] if options.evidence else []) + plan.evidence,
            "authority": "delegated" if owner is None else "owner",
            "owner_words": owner,
            "at": iso(context.moment),
            "session": session,
            "details": plan.details,
        }
        if refusal is not None:
            record["reason"] = refusal
            append(notes, record)
            print(json.dumps(record))
            print(
                refusal if plan.details.get("already_started") else f"refused: {refusal}",
                file=sys.stderr,
            )
            return 1
        append(notes, record)
        act(context, record)
        done = {**record, "status": "done"}
        append(notes, done)
        print(json.dumps(done))
    return 0


# What the wake and the metrics read.


def wake(notes: Path, moment: int) -> dict[str, Any]:
    lines = read_lines(notes)
    try:
        told = int((notes / CURSOR).read_text().split()[0])
    except OSError, ValueError, IndexError:
        told = 0
    held = []
    for hold in open_holds(notes):
        shared = hold["details"].get("shared") or []
        why = f"shares {', '.join(shared)}" if shared else "the owner's word"
        age = duration(moment - instant(hold["at"]))
        held.append(f"{hold['number']} held after {hold['details']['after']}: {why} ({age})")
    events = []
    for line in lines[told:]:
        if line.get("kind") == "answer" and line.get("status") == "done":
            details = line["details"]
            events.append(
                f'answered {line["number"]} ({details["area"]}): {details["question"]}: "{details["reply"]}"'
            )
        elif line.get("kind") == "start" and line.get("status") == "refused":
            if (line.get("details") or {}).get("already_started"):
                events.append(f"{line['number']} already started")
            else:
                events.append(f"start of {line['number']} refused: {line.get('reason')}")
    if len(events) > TOLD_EVENTS:
        events = [*events[:TOLD_EVENTS], f"and {len(events) - TOLD_EVENTS} more in {RECORDS}"]
    return {"held": held, "events": events, "cursor": len(lines)}


def wake_told(notes: Path, cursor: int) -> None:
    write_file(notes / CURSOR, f"{cursor}\n")


def metrics(notes: Path, since: float, until: float, moment: float | None = None) -> dict[str, Any]:
    """Counts by kind of the decisions made in the window, each hold episode's hours within it,
    and the answers per ticket."""
    moment = now() if moment is None else moment
    kinds = {kind: {"done": 0, "refused": 0} for kind in KINDS}
    holds, answers = [], Counter()
    for record in records(notes).values():
        try:
            at = instant(record["at"])
        except KeyError, TypeError, ValueError:
            continue
        status = {"ended": "done"}.get(record.get("status"), record.get("status"))
        if since <= at < until and status in ("done", "refused") and record.get("kind") in kinds:
            kinds[record["kind"]][status] += 1
            if record["kind"] == "answer" and status == "done":
                answers[str(record["number"])] += 1
        if record.get("kind") == "hold" and status == "done":
            ended = record.get("ended_at")
            end = instant(ended) if ended else moment
            inside = min(end, until) - max(at, since)
            if inside > 0:
                holds.append(
                    {
                        "number": record["number"],
                        "after": record["details"]["after"],
                        "hours": round(inside / 3600, 2),
                        "open": ended is None,
                    }
                )
    return {"kinds": kinds, "holds": holds, "answers": dict(answers)}


def parse(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="decide", description=__doc__.split("\n\n")[0])
    kinds = parser.add_subparsers(dest="kind", required=True)
    for kind in KINDS:
        one = kinds.add_parser(kind)
        one.add_argument("number", type=int)
        one.add_argument("--owner", help="the owner's exact words, when it is his decision")
        one.add_argument("--evidence", help="what the decision rests on")
        if kind in ("start", "hold"):
            one.add_argument("--after", type=int, required=kind == "hold")
        if kind == "answer":
            one.add_argument("--area", required=True)
            one.add_argument("--question", required=True)
            one.add_argument("--meaning", required=True)
            one.add_argument("--reply")
        if kind == "allowance":
            one.add_argument("--round", type=int, required=True)
        if kind in ("hand-back", "restart"):
            one.add_argument("--session", required=True)
            one.add_argument("--judgment", required=True)
            one.add_argument("--message", required=True)
        if kind == "program":
            one.add_argument("program")
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    notes = CONFIG.state()
    if arguments[:1] == ["--metrics"]:
        if len(arguments) != 3:
            print("usage: decide --metrics SINCE UNTIL", file=sys.stderr)
            return 2
        since, until = (moment_of(word) for word in arguments[1:])
        print(json.dumps(metrics(notes, since, until), indent=2))
        return 0
    if arguments == ["unfinished"]:
        print("\n".join(sorted(unfinished(notes), key=int)))
        return 0
    if arguments == ["wake"]:
        print(json.dumps(wake(notes, now())))
        return 0
    if len(arguments) == 2 and arguments[0] == "wake-told" and arguments[1].isdigit():
        wake_told(notes, int(arguments[1]))
        return 0
    options = parse(arguments)
    return decide(options.kind, options.number, options)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Failed as error:
        print(f"decide: {error}", file=sys.stderr)
        sys.exit(3)
    except Limited:
        print("decide: the GitHub budget is low; nothing new was decided", file=sys.stderr)
        sys.exit(75)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"decide: {error}", file=sys.stderr)
        sys.exit(3)
