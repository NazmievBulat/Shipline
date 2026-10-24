"""What a session's Bash tool runs, read from the processes of its control groups.

    running_command.py <proc root> <control group folder>...

Prints one JSON object: `running`, the first line of the command the session started last
(empty when none runs), and `finds_itself`, the first line of a running wait loop that can
never end because its `pgrep -f` or `ps | grep` pattern matches the loop's own shell, or null.

Claude Code runs a Bash command as `bash -c "source <snapshot> ... && eval '<command>' ..."`.
`pgrep` never counts itself, but it counts that shell, whose command line holds the pattern:
`while pgrep -f "codex exec"; do sleep 30; done` waits for as long as its own shell lives.

Only a loop is named, by the search that decides it: the last command of its condition, between
`while` and `do`. A single search that finds itself ends at once, and so do `until pgrep ...`,
`while ! pgrep ...` and `while [ -z "$(pgrep ...)" ]`; a search in the loop's body or after
`done`, or a condition joined by `&&`, leaves the end to another command. A search narrowed to children, a session, a terminal or a
pid file (`-P`, `-s`, `-g`, `-t`, `-F`), an exact match (`-x`), one that leaves out its
ancestors (`-A`), a `ps` that lists chosen processes only (`-p "$pid"`, `-C`, `-u`), and a
`ps | grep` piped on into `grep -v` are not named: they may end.
"""

import json
import re
import shlex
import sys
from pathlib import Path

# The pgrep options that take a value, and those that keep the shell itself out of the search.
_PGREP_VALUE = set("dgGPstuUFr")
_PGREP_NARROWED = set("PsgtFxA")
_PGREP_LONG_NARROWED = (
    "--parent",
    "--session",
    "--pgroup",
    "--terminal",
    "--pidfile",
    "--exact",
    "--ignore-ancestors",
    "--ns",
    "--nslist",
)
_GREP_VALUE = set("efmABCD")
# The ps options that choose processes, in the standard, long and BSD forms.
_PS_SELECTING = set("pqCstgGuU")
_PS_LONG_SELECTING = (
    "--pid",
    "--quick-pid",
    "--ppid",
    "--sid",
    "--tty",
    "--Group",
    "--group",
    "--User",
    "--user",
)
_PS_BSD_SELECTING = set("pqtU")
# The long ps options whose value follows as a word of its own.
_PS_LONG_VALUE = (
    "--format",
    "--width",
    "--cols",
    "--columns",
    "--rows",
    "--lines",
    "--sort",
    *_PS_LONG_SELECTING,
)
_SHORT_LINE = 160


def _processes(proc: Path, groups: list[Path]) -> list[tuple[int, list[str]]]:
    """Every process of the groups and their subgroups, oldest first, with its arguments."""
    pids: set[int] = set()
    for group in groups:
        for listing in [group / "cgroup.procs", *group.rglob("*/cgroup.procs")]:
            try:
                pids.update(int(word) for word in listing.read_text().split() if word.isdigit())
            except OSError:
                continue
    found = []
    for pid in pids:
        try:
            raw = (proc / str(pid) / "cmdline").read_bytes()
        except OSError:
            continue
        arguments = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
        if arguments:
            found.append((_started(proc, pid), arguments))
    return [(started, arguments) for started, arguments in sorted(found)]


def _started(proc: Path, pid: int) -> int:
    """When the process started, in clock ticks since boot; the pid when that cannot be read."""
    try:
        stat = (proc / str(pid) / "stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return pid


def _tool_command(arguments: list[str]) -> str | None:
    """The command of a Claude Code Bash tool call, or None for any other process."""
    if len(arguments) < 3 or not arguments[0].endswith("sh") or arguments[1] != "-c":
        return None
    script = arguments[-1]
    if "shell-snapshots/" not in script or "eval " not in script:
        return None
    try:
        words = shlex.split(script[script.index("eval ") + len("eval ") :])
    except ValueError:
        return None
    return words[0] if words else None


def _words(command: str, depth: int = 0) -> list[str]:
    """The words and operators of a command, with substitutions opened up into words.

    A substitution in double quotes stays one word after the first split, and is split again.
    """
    flat = re.sub(r"\$\(|`", " ( ", command)
    lexer = shlex.shlex(flat, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return []
    if depth >= 3:
        return words
    opened = []
    for word in words:
        opened.extend(_words(word, depth + 1) if " ( " in word else [word])
    return opened


def _pgrep_pattern(words: list[str]) -> str | None:
    """The pattern of a `pgrep -f` that the shell itself would match, or None."""
    full = ignore_case = False
    index = 0
    while index < len(words) and words[index].startswith("-") and words[index] != "--":
        word = words[index]
        index += 1
        if word.startswith("--"):
            if word.split("=")[0] in _PGREP_LONG_NARROWED:
                return None
            full = full or word == "--full"
            ignore_case = ignore_case or word == "--ignore-case"
            continue
        for at, letter in enumerate(word[1:]):
            if letter in _PGREP_NARROWED:
                return None
            full = full or letter == "f"
            ignore_case = ignore_case or letter == "i"
            if letter in _PGREP_VALUE:
                if at + 2 == len(word):
                    index += 1
                break
    if index < len(words) and words[index] == "--":
        index += 1
    if not full or index >= len(words):
        return None
    return ("(?i)" if ignore_case else "") + words[index]


def _grep_pattern(words: list[str]) -> str | None:
    """The pattern a `grep` searches for, as a Python expression, or None."""
    fixed = extended = ignore_case = False
    index = 0
    while index < len(words) and words[index].startswith("-") and words[index] != "--":
        word = words[index]
        index += 1
        for at, letter in enumerate(word[1:]):
            if letter == "v":
                return None
            fixed = fixed or letter == "F"
            extended = extended or letter == "E"
            ignore_case = ignore_case or letter == "i"
            if letter in _GREP_VALUE:
                if at + 2 == len(word):
                    index += 1
                break
    if index < len(words) and words[index] == "--":
        index += 1
    if index >= len(words):
        return None
    pattern = words[index]
    if fixed:
        pattern = re.escape(pattern)
    elif not extended:
        pattern = _basic(pattern)
    return ("(?i)" if ignore_case else "") + pattern


def _ps_selects(words: list[str]) -> bool:
    """Whether `ps` lists chosen processes only (`-p "$pid"`, `-C codex`, `--ppid 1`, `t pts/1`),
    which leave the loop's own shell out. Selections add up: with `-e`, `-A` or `ax` every
    process is listed whatever else is chosen."""
    skip = False
    chosen = everything = False
    bsd = ""
    for word in words:
        if skip:
            # The value of a format or width option, not a choice of processes.
            skip = False
            continue
        if word.startswith("--"):
            option = word.split("=")[0]
            chosen = chosen or option in _PS_LONG_SELECTING
            skip = "=" not in word and option in _PS_LONG_VALUE
        elif word.startswith("-"):
            letters = word[1:].split("o", 1)[0].split("O", 1)[0]
            chosen = chosen or bool(set(letters) & _PS_SELECTING)
            everything = everything or bool(set(letters) & set("eA"))
            skip = word.endswith(("o", "O"))
        elif word.isalpha():
            bsd += word
            chosen = chosen or bool(set(word) & _PS_BSD_SELECTING)
        elif word[:1].isdigit() or word.startswith("$"):
            # A bare pid, as in `ps 4242`.
            chosen = True
    everything = everything or {"a", "x"} <= set(bsd)
    return chosen and not everything


def _basic(pattern: str) -> str:
    """A GNU basic expression as a Python one: there `+?(){}|` stand for themselves, and with a
    backslash before them (`codex\\|exec`) they have their special meaning."""
    out = []
    at = 0
    while at < len(pattern):
        letter = pattern[at]
        if letter == "\\" and at + 1 < len(pattern):
            following = pattern[at + 1]
            out.append(following if following in "+?(){}|" else letter + following)
            at += 2
            continue
        out.append("\\" + letter if letter in "+?(){}|" else letter)
        at += 1
    return "".join(out)


def _searches(words: list[str]) -> list[list[str]]:
    """The searches of the process list that decide whether a loop goes on. Each is the
    patterns that must all match a process for it to be found: one for `pgrep`, one per `grep`
    after `ps`."""
    searches = []
    for at, word in enumerate(words):
        if word == "while":
            searches.extend(_condition_searches(words[at + 1 :]))
    return searches


def _condition_searches(words: list[str]) -> list[list[str]]:
    """The searches of the last command of a loop's condition, between `while` and `do`: that
    command alone decides whether the loop goes on. A search in the loop's body or after it does
    not. `&&` lets another command end the loop, and `!` or `-z` turn a found process into its
    end, so a condition holding them is left out."""
    if "do" not in words:
        return []
    condition = words[: words.index("do")]
    while condition and condition[-1] in {";", "&"}:
        condition.pop()
    cut = max((i for i, w in enumerate(condition) if w in {";", "&"}), default=-1)
    words = condition[cut + 1 :]
    if {"&&", "!", "-z"} & set(words):
        return []
    searches = []
    for at, word in enumerate(words):
        rest = words[at + 1 :]
        end = next((i for i, w in enumerate(rest) if w in {";", "&&", "||", "|", ")", "&"}), None)
        own = rest if end is None else rest[:end]
        if word.rsplit("/", 1)[-1] == "pgrep":
            # What pgrep found and a pipe filters further (`| grep -q '^4242$'`) can end.
            if end is not None and rest[end] == "|":
                continue
            pattern = _pgrep_pattern(own)
            if pattern is not None:
                searches.append([pattern])
        elif word.rsplit("/", 1)[-1] == "ps" and not _ps_selects(own):
            stop = next(
                (i for i, w in enumerate(rest) if w in {";", "&&", "||", ")", "&"}),
                len(rest),
            )
            pipeline = rest[:stop]
            greps = [
                i for i, w in enumerate(pipeline) if w == "grep" and i and pipeline[i - 1] == "|"
            ]
            patterns = []
            for i in greps:
                after = pipeline[i + 1 :]
                cut = next((j for j, w in enumerate(after) if w == "|"), len(after))
                pattern = _grep_pattern(after[:cut])
                if pattern is None:
                    patterns = []
                    break
                patterns.append(pattern)
            if patterns:
                searches.append(patterns)
    return searches


def finds_itself(arguments: list[str], command: str) -> bool:
    """Whether a search of the loop in `command` matches the shell that runs it."""
    line = " ".join(arguments)
    for patterns in _searches(_words(command)):
        try:
            if all(re.search(pattern, line) for pattern in patterns):
                return True
        except re.error:
            continue
    return False


def _first_line(command: str) -> str:
    line = next((line.strip() for line in command.splitlines() if line.strip()), "")
    return line if len(line) <= _SHORT_LINE else line[: _SHORT_LINE - 1] + "…"


def read(proc: Path, groups: list[Path]) -> dict[str, str | None]:
    running = ""
    looping = None
    for arguments in (arguments for _, arguments in _processes(proc, groups)):
        command = _tool_command(arguments)
        if command is None:
            continue
        running = _first_line(command)
        if looping is None and finds_itself(arguments, command):
            looping = running
    return {"running": running, "finds_itself": looping}


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: running_command.py <proc root> <control group folder>...", file=sys.stderr)
        return 2
    proc = Path(sys.argv[1])
    print(json.dumps(read(proc, [Path(folder) for folder in sys.argv[2:]])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
