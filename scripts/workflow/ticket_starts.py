"""The tick starts the next draft ticket by itself (AGENTS.md, "For the orchestrator session").

    ticket_starts.py files <main> <N>          the files of ticket N, one path per line
    ticket_starts.py pick <main> <snapshot>    start the next ticket when one may start

The files of a ticket are kept in `.local/workflow/files-N` of the main checkout, one path per
line, with their source noted in `files-N.source`:

- `diff <head>`: the pull request has a real diff, and its file list is the answer. No model.
- `heading <body> <head>`: the description names the files under a `Files` heading. No model.
- `description <body> <head>`: one short `claude -p` on Claude Haiku 5.5 read the description
  and answered which files or directories the change will touch.

`<body>` is a digest of the description and `<head>` the head the answer was found on: the
model is asked again only when the description changes, and the diff is looked at again only
when the head moves.

A pick takes the drafts without the label `workflow`, leaving out a cloud worker's (its
`cloud-N` record, or the line `Worker: cloud`) and one only the owner starts (the line
`Start: owner only`), and orders them by priority, then the older ticket. The first that may
start is the one whose files overlap no running ticket's (an open pull request labelled
`workflow`, one with a session or a cloud record, one in the merge train), except a ready
train member with no session or cloud record, and that no line of
`.local/workflow/start-plan` holds back:

    609 after 605     609 waits while 605 is open
    hold 590          590 does not start
    hold all          nothing starts by itself

A line not understood holds every start too, as `hold all` does, until it is written so it is.

It starts, by getting the label `workflow` (the next tick's worker start does the rest), only when
memory and the load have been under the ceilings of `session start` for 10 minutes, nothing
started in the last 5 and the 5-hour usage window allows starts (not above 85% unless it resets
within the hour). One start a pick. What started, and why the next one waits, is noted once
per change in `ticket-starts.pending/` for the wake. The machine's reason is told in one
wording, so a load that comes and goes is not told at every tick; the tick prints which.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import priority
import tick_snapshot
import workflow_config
from github_budget import Limited, call

# Memory and load under the ceilings this long before an automatic start.
ROOM_SECONDS = 10 * 60
# No automatic start this soon after any start, the tick's or a person's.
QUIET_SECONDS = 5 * 60
# The tick looks every 3 minutes. A longer gap between two looks is not known to have had room.
LOOK_GAP_SECONDS = 6 * 60
# Model calls one pick may make: the first pass over many new drafts is spread over ticks.
MODEL_CALLS_PER_PICK = 3
# A description the model gave no path for is asked again only after this, so a few such
# tickets cannot take every call of every pick.
ASK_AGAIN_SECONDS = 30 * 60
MODEL = "claude-haiku-5-5"
MODEL_SECONDS = 90
# The usage rule the orchestrator kept for its starts (AGENTS.md, the wake's check-in).
USAGE_CEILING = 85
USAGE_RESET_SECONDS = 3600
# What the wake is told while the machine holds the next start, whichever of its rules does.
MACHINE_WAIT = (
    "the machine (a start needs 10 minutes of room, 5 minutes since the last start and the "
    "usage window)"
)
# The wait lines of one pick told at most.
TOLD_WAITS = 8

CONFIG = workflow_config.current()
LABEL = CONFIG.labels.workflow
CLOUD_MARK = "Worker: cloud"
OWNER_MARK = "Start: owner only"
# Any file name of the repository, route folders like `[runId]` and `(group)` among them.
PATH = re.compile(r"[^\s`'\"<>|*?:/-][^\s`'\"<>|*?:]*")
FILES_HEADING = re.compile(r"^#{1,6}[ \t]+Files[ \t]*:?[ \t]*$", re.IGNORECASE)
ANY_HEADING = re.compile(r"^#{1,6}[ \t]")
PLAN_AFTER = re.compile(r"([0-9]+)[ \t]+after[ \t]+([0-9]+)")
PLAN_HOLD = re.compile(r"hold[ \t]+([0-9]+|all)")

PROMPT = (
    "You read the description of a code change in one repository and name the files or "
    "directories the change will touch. Answer with paths relative to the repository root, "
    "one per line, and nothing else: no bullets, no quotes, no comments. Name a directory "
    "when the change spreads over several files in it. Use only paths that exist or would be "
    "created beside the directories listed."
)

here = Path(__file__).resolve().parent


def now() -> int:
    return int(os.environ.get("WORKFLOW_NOW") or time.time())


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def clean_path(text: str) -> str | None:
    """A path as a ticket or the model writes it, or None when it is not one."""
    text = re.sub(r"^\s*(?:[-*+]|[0-9]+[.)])\s+", "", text).strip().strip("`'\"").strip()
    text = re.sub(r"(/\*\*?)+$", "", text).rstrip("/").removeprefix("./")
    # A line number after a file names the file.
    text = re.sub(r":[0-9]+(?:-[0-9]+)?$", "", text)
    if not text or not PATH.fullmatch(text) or ".." in text.split("/"):
        return None
    return text


def overlap(first: str, second: str) -> str | None:
    """The shared part when one path is the other or holds it, the more precise of the two."""
    # `.` is the whole repository, and holds every path.
    if first == ".":
        return second
    if second == ".":
        return first
    if first == second or first.startswith(second + "/"):
        return first
    if second.startswith(first + "/"):
        return second
    return None


def has_line(body: str, mark: str) -> bool:
    return any(line.strip() == mark for line in body.splitlines())


def heading_files(body: str) -> list[str]:
    """The paths under a `Files` heading: backticked ones, or the first word of each item."""
    paths: list[str] = []
    inside = False
    for line in body.splitlines():
        if FILES_HEADING.match(line.strip()):
            inside = True
            continue
        if inside and ANY_HEADING.match(line.strip()):
            break
        if not inside or not line.strip():
            continue
        quoted = re.findall(r"`([^`]+)`", line)
        words = quoted or [re.sub(r"^\s*(?:[-*+]|[0-9]+[.)])\s+", "", line).split()[0]]
        paths += [path for word in words if (path := clean_path(word))]
    return list(dict.fromkeys(paths))


def model_files(body: str, main: Path) -> list[str] | None:
    """Ask the model once; None when it gave no path."""
    folders = git(main, "ls-tree", "-r", "-d", "--name-only", "HEAD")
    question = (
        f"The directories of the repository:\n{folders}\n\n"
        f"The description of the change, between the lines of dashes:\n----------\n{body}\n"
        "----------"
    )
    state = Path.home() / ".local" / "state" / "workflow"
    state.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                MODEL,
                "--setting-sources",
                "",
                "--tools",
                "",
                "--system-prompt",
                PROMPT,
                question,
            ],
            cwd=state,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=MODEL_SECONDS,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if done.returncode != 0:
        return None
    paths = [path for line in done.stdout.splitlines() if (path := clean_path(line))]
    return list(dict.fromkeys(paths)) or None


def git(main: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(main), *arguments], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise OSError(f"git {arguments[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


@dataclass
class Files:
    """Finds and keeps the files of tickets for one pick, with its share of model calls."""

    main: Path
    model_calls: int = MODEL_CALLS_PER_PICK
    # The tickets the model was asked about in this pick, answered or not.
    asked: set[str] = field(default_factory=set)

    @property
    def notes(self) -> Path:
        return self.main / CONFIG.state_folder

    def kept(self, number: str) -> tuple[list[str], list[str]] | None:
        """The saved paths and the words of their source."""
        try:
            paths = (self.notes / f"files-{number}").read_text().splitlines()
            source = (self.notes / f"files-{number}.source").read_text().split()
        except OSError:
            return None
        return (paths, source) if paths and source else None

    def keep(self, number: str, paths: list[str], *source: str) -> list[str]:
        for name, text in (
            (f"files-{number}", "\n".join(paths)),
            (f"files-{number}.source", " ".join(source)),
        ):
            temporary = self.notes / f".{name}.new"
            temporary.write_text(text + "\n")
            temporary.replace(self.notes / name)
        return paths

    def fetch(self, pulls: list[dict[str, Any]]) -> set[str]:
        """Fetch the heads not yet looked at, with main, in one call. The numbers fetched."""
        wanted = []
        for pull in pulls:
            kept = self.kept(str(pull["number"]))
            if kept is None or kept[1][-1] != pull["headRefOid"]:
                wanted.append(pull)
        if not wanted:
            return set()
        main = "+refs/heads/main:refs/workflow/files/main"
        specs = {
            str(
                pull["number"]
            ): f"+refs/heads/{pull['headRefName']}:refs/workflow/files/{pull['number']}"
            for pull in wanted
        }
        try:
            git(
                self.main,
                "fetch",
                "--quiet",
                "--no-write-fetch-head",
                "origin",
                main,
                *specs.values(),
            )
            return set(specs)
        except OSError as error:
            print(f"cannot fetch the heads of tickets together: {error}", file=sys.stderr)
        # One branch gone, as after a rename, fails the whole fetch: the others are fetched alone.
        fetched = set()
        for number, spec in specs.items():
            try:
                git(self.main, "fetch", "--quiet", "--no-write-fetch-head", "origin", main, spec)
                fetched.add(number)
            except OSError as error:
                print(f"cannot fetch the head of {number}: {error}", file=sys.stderr)
        return fetched

    def diff(self, number: str) -> list[str]:
        """The files the diff names, as git names them: every one is a file of the repository."""
        ref = f"refs/workflow/files/{number}"
        base = git(self.main, "merge-base", "refs/workflow/files/main", ref)
        listed = git(self.main, "diff", "--name-only", "--no-renames", "-z", base, ref)
        return [path for path in listed.split("\0") if path and "\n" not in path]

    def of(self, pull: dict[str, Any], fetched: set[str]) -> list[str] | None:
        """The files of one pull request, or None when they are not known yet."""
        number, head = str(pull["number"]), pull["headRefOid"]
        body = pull.get("body") or ""
        kept = self.kept(number)
        if kept and kept[1][-1] == head and (kept[1][0] == "diff" or kept[1][1] == digest(body)):
            return kept[0]
        if number in fetched:
            try:
                if paths := self.diff(number):
                    return self.keep(number, paths, "diff", head)
            except OSError as error:
                # Fetched because the head moved: an answer kept for an older head may miss
                # what the new one changes.
                print(f"cannot read the diff of {number}: {error}", file=sys.stderr)
                return None
            seen = head
        elif kept and kept[1][-1] == head:
            # Only the description changed: the diff at this head is known to be empty.
            seen = head
        else:
            # The head could not be fetched: what it changes is not known until it is. An answer
            # kept for an older head, or one read from the description, may miss a file it
            # shares with a draft.
            return None
        if paths := heading_files(body):
            return self.keep(number, paths, "heading", digest(body), seen)
        if kept and kept[1][0] == "description" and kept[1][1] == digest(body):
            return self.keep(number, kept[0], "description", digest(body), seen)
        if self.model_calls <= 0 or number in self.asked or self.asked_lately(number, body):
            return None
        self.model_calls -= 1
        self.asked.add(number)
        failed = self.notes / f"files-{number}.asked"
        if paths := model_files(body, self.main):
            failed.unlink(missing_ok=True)
            return self.keep(number, paths, "description", digest(body), seen)
        failed.write_text(f"{digest(body)} {now()}\n")
        return None

    def asked_lately(self, number: str, body: str) -> bool:
        """Whether the model gave no path for this description in the last half hour."""
        try:
            asked, at = (self.notes / f"files-{number}.asked").read_text().split()
            return asked == digest(body) and now() - int(at) < ASK_AGAIN_SECONDS
        except OSError, ValueError:
            return False


def read_plan(path: Path) -> tuple[bool, set[str], dict[str, set[str]], list[str]]:
    """`hold all`, the held tickets, what each ticket waits for, and the lines not understood."""
    held_all, held, after, unclear = False, set(), {}, []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if found := PLAN_HOLD.fullmatch(line):
            if found[1] == "all":
                held_all = True
            else:
                held.add(found[1])
        elif found := PLAN_AFTER.fullmatch(line):
            after.setdefault(found[1], set()).add(found[2])
        else:
            unclear.append(line)
    return held_all, held, after, unclear


def labels_of(pull: dict[str, Any]) -> set[str]:
    return {label.get("name") for label in pull.get("labels") or []}


def running_numbers() -> set[str]:
    done = subprocess.run(
        ["bash", str(here / "session"), "list"], capture_output=True, text=True, check=False
    )
    return set(re.findall(r"^(?:ticket|audit|review|codex)-([0-9]+)\s", done.stdout, re.M))


def train_numbers(notes: Path) -> set[str]:
    try:
        train = json.loads((notes / "merge-train.json").read_text())
    except OSError, ValueError:
        return set()
    return (
        {str(number) for number in train.get("waiting", [])} if isinstance(train, dict) else set()
    )


def machine_wait(notes: Path, moment: int) -> str | None:
    """Why the machine holds a start now, or None. Counts the minutes of room as it goes."""
    room = subprocess.run(
        ["bash", str(here / "session"), "room"], capture_output=True, text=True, check=False
    )
    record = notes / "room"
    try:
        since, looked = (int(word) for word in record.read_text().split())
    except OSError, ValueError:
        since = looked = None
    if room.returncode != 0:
        record.unlink(missing_ok=True)
        return "memory or load is over the ceilings"
    if since is None or looked is None or moment - looked > LOOK_GAP_SECONDS:
        since = moment
    record.write_text(f"{since} {moment}\n")
    if moment - since < ROOM_SECONDS:
        return "memory and load have been under the ceilings for less than 10 minutes"
    try:
        last = int((notes / "last-start").read_text().split()[0])
    except OSError, ValueError, IndexError:
        last = 0
    if moment - last < QUIET_SECONDS:
        return "a session started in the last 5 minutes"
    return usage_wait(moment)


def usage_wait(moment: int) -> str | None:
    """Why the 5-hour usage window holds a start now, or None. `decide` asks it too."""
    usage = subprocess.run(
        ["bash", str(here / "usage")], capture_output=True, text=True, check=False
    )
    for line in usage.stdout.splitlines():
        words = line.split()
        if not (len(words) == 3 and words[0] == "five_hour" and words[1].isdigit()):
            continue
        if (
            words[2].isdigit()
            and int(words[1]) > USAGE_CEILING
            and (int(words[2]) - moment > USAGE_RESET_SECONDS)
        ):
            return f"the 5-hour usage window is {words[1]}% used"
    return None


def tell(notes: Path, lines: dict[str, str], started: str | None) -> None:
    """Note for the wake each line that changed since the last pick, and the start."""
    status = notes / "start-status.json"
    try:
        told = json.loads(status.read_text())
    except OSError, ValueError:
        told = {}
    changed = [line for key, line in lines.items() if told.get(key) != line]
    if started:
        changed.insert(0, started)
    if changed:
        pending = notes / "ticket-starts.pending"
        pending.mkdir(exist_ok=True)
        event = pending / f"{now()}-{os.getpid()}.json"
        temporary = pending / f".{event.name}"
        temporary.write_text(json.dumps({"lines": changed}) + "\n")
        temporary.replace(event)
    temporary = notes / ".start-status.json.new"
    temporary.write_text(json.dumps(lines) + "\n")
    temporary.replace(status)
    log = Path.home() / ".local" / "state" / "workflow" / "wake.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as file:
        for line in changed:
            file.write(f"{now()} ticket start: {line}\n")


def add_label(number: str) -> bool:
    done = call(
        "tick",
        "api",
        "-X",
        "POST",
        f"{CONFIG.api}/issues/{number}/labels",
        "-f",
        f"labels[]={LABEL}",
    )
    return done.returncode == 0


def order(pull: dict[str, Any]) -> tuple[int, int]:
    """Where a ticket stands among the waiting: by priority, then the older ticket."""
    return (priority.rank(priority.of(pull.get("labels"))), pull["number"])


@dataclass
class Queue:
    """The drafts that wait for their start and the tickets that run, as one pick sees them."""

    main: Path
    open_pulls: dict[str, dict[str, Any]]
    held_all: bool
    held: set[str]
    after: dict[str, set[str]]
    unclear: list[str]
    busy: dict[str, dict[str, Any]]
    candidates: list[dict[str, Any]]
    files: Files = field(init=False)
    fetched: set[str] = field(default_factory=set)
    busy_files: dict[str, list[str] | None] = field(default_factory=dict)

    @classmethod
    def read(cls, main: Path, open_pulls: dict[str, dict[str, Any]]) -> Queue:
        notes = main / CONFIG.state_folder
        held_all, held, after, unclear = read_plan(notes / "start-plan")
        sessions = running_numbers()
        cloud = {path.name.removeprefix("cloud-") for path in notes.glob("cloud-[0-9]*")}
        train = train_numbers(notes)
        working = sessions | cloud | train
        busy = {
            number: pull
            for number, pull in open_pulls.items()
            if (LABEL in labels_of(pull) or number in working)
            and not (
                number in train
                and not pull.get("isDraft")
                and number not in sessions
                and number not in cloud
            )
        }
        candidates = sorted(
            (
                pull
                for number, pull in open_pulls.items()
                if pull.get("isDraft")
                and number not in busy
                and not has_line(pull.get("body") or "", CLOUD_MARK)
                and not has_line(pull.get("body") or "", OWNER_MARK)
            ),
            key=order,
        )
        return cls(main, open_pulls, held_all, held, after, unclear, busy, candidates)

    def __post_init__(self) -> None:
        self.files = Files(self.main)

    def look(self, *more: dict[str, Any]) -> None:
        """Fetch the heads of the running tickets, the waiting ones and `more`, and find the
        files of the running ones."""
        self.fetched |= self.files.fetch(list(self.busy.values()) + self.candidates + list(more))
        self.busy_files = {
            number: self.files.of(pull, self.fetched) for number, pull in sorted(self.busy.items())
        }

    def files_of(self, pull: dict[str, Any]) -> list[str] | None:
        if str(pull["number"]) not in self.fetched:
            self.fetched |= self.files.fetch([pull])
        return self.files.of(pull, self.fetched)

    @property
    def unknown(self) -> list[str]:
        return sorted(
            (number for number, found in self.busy_files.items() if found is None), key=int
        )

    def sharing(self, found: list[str]) -> list[tuple[str, str]]:
        """Each running ticket that shares a path with `found`, with the first path shared."""
        shared = []
        for other, theirs in self.busy_files.items():
            both = next(
                (both for mine in found for their in theirs or [] if (both := overlap(mine, their))),
                None,
            )
            if both:
                shared.append((other, both))
        return shared

    def reason(self, pull: dict[str, Any], found: list[str] | None) -> str | None:
        """Why this draft does not start now, by the plan and the files; None when it may."""
        number = str(pull["number"])
        if number in self.held:
            return f"hold {number} in start-plan"
        if waits := sorted(self.after.get(number, set()) & set(self.open_pulls), key=int):
            return f"{number} after {', '.join(waits)} in start-plan"
        if pull.get("baseRefName") != "main":
            base = pull.get("baseRefName")
            branches = {other.get("headRefName"): n for n, other in self.open_pulls.items()}
            return f"built on {branches.get(base, base)}, not on main"
        if found is None:
            return "its files are not known yet"
        if unknown := self.unknown:
            return f"the files of {', '.join(unknown)} are not known yet"
        if shared := self.sharing(found):
            other, both = shared[0]
            return f"shares {both} with {other}"
        return None


def pick(main: Path, snapshot: Path) -> int:
    import decisions

    notes = main / CONFIG.state_folder
    notes.mkdir(parents=True, exist_ok=True)
    moment = now()
    pulls: dict[str, dict[str, Any]] = json.loads(snapshot.read_text())
    open_pulls = {number: pull for number, pull in pulls.items() if pull.get("state") == "OPEN"}
    # A hold `decide` recorded ends when its ticket or the one it waits for closes, or once
    # its ticket started. The tick holds the lock of `decide` while it picks.
    decisions.repair(notes)
    decisions.close_holds(notes, open_pulls, moment)
    unfinished = decisions.unfinished(notes)
    queue = Queue.read(main, open_pulls)
    # Measured every pick, so the minutes of room are counted even while nothing waits.
    machine = machine_wait(notes, moment)

    if queue.held_all:
        tell(notes, {"all": "automatic starts are held: hold all in start-plan"}, None)
        return 0
    if queue.unclear:
        # A rule mistyped may be a hold: nothing starts until it is written so it is understood.
        line = f"automatic starts are held: start-plan line not understood: {queue.unclear[0]}"
        tell(notes, {"all": line}, None)
        return 0

    if not queue.candidates:
        # Nothing waits: the files of running tickets matter only beside a draft's.
        tell(notes, {}, None)
        return 0
    queue.look()

    lines: dict[str, str] = {}
    chosen: dict[str, Any] | None = None
    for pull in queue.candidates:
        number = str(pull["number"])
        found = queue.files.of(pull, queue.fetched)
        if number in unfinished:
            # The orchestrator's decision on it is not finished: the next call of `decide` does.
            reason = f"a decision on {number} is not finished"
        else:
            reason = queue.reason(pull, found)
        if reason is None:
            chosen = pull
            break
        if len(lines) < TOLD_WAITS:
            lines[number] = f"{number} waits: {reason}"
    # Files are found for the drafts after the chosen one too, once each, as the calls allow.
    for pull in queue.candidates:
        if queue.files.model_calls <= 0:
            break
        queue.files.of(pull, queue.fetched)

    started = None
    if chosen is not None:
        number = str(chosen["number"])
        name = priority.of(chosen.get("labels"))
        if machine is None:
            # Finding the files may have taken minutes of fetches and model calls: the machine
            # is looked at again just before the start.
            machine = machine_wait(notes, now())
        if machine is not None:
            print(f"{number} waits: {machine}")
            lines["next"] = f"{number} waits: {MACHINE_WAIT}"
        elif add_label(number):
            (notes / "last-start").write_text(f"{now()}\n")
            started = f"started {number} (priority {name})"
            print(started)
        else:
            print(
                f"cannot add the label {LABEL} to {number}; the next tick tries again",
                file=sys.stderr,
            )
            tell(notes, lines, None)
            return 1
    tell(notes, lines, started)
    return 0


def files_of(main: Path, number: str) -> int:
    snapshot = os.environ.get("WORKFLOW_PR_SNAPSHOT")
    pull = None
    if snapshot:
        pull = json.loads(Path(snapshot).read_text()).get(number)
    if pull is None:
        done = call("tick", "pr", "view", number, "--json", tick_snapshot.FIELDS)
        done.check_returncode()
        pull = {**json.loads(done.stdout), "number": int(number)}
    finder = Files(main, model_calls=1)
    (main / CONFIG.state_folder).mkdir(parents=True, exist_ok=True)
    found = finder.of(pull, finder.fetch([pull]))
    if found is None:
        print(f"the files of {number} could not be found; asked again next time", file=sys.stderr)
        return 1
    print("\n".join(found))
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "files" and sys.argv[3].isdigit():
            sys.exit(files_of(Path(sys.argv[2]), sys.argv[3]))
        if len(sys.argv) == 4 and sys.argv[1] == "pick":
            sys.exit(pick(Path(sys.argv[2]), Path(sys.argv[3])))
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        sys.exit(2)
    except Limited:
        sys.exit(75)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"ticket start: {error}", file=sys.stderr)
        sys.exit(1)
