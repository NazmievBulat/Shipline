"""The effort level of pull request N's independent reviewer, for `session start review N`.

`high` when the description has the line `Review: high` on its own, or when a path the change
touches, either side of a rename or a deletion included, matches one of the repository's
`review.high_effort_paths` (`workflow_config.py`) or is one of Shipline's own concurrent
scripts; `medium` otherwise. Whatever keeps it from telling (an empty, unreadable or
ambiguous diff, a `gh` that fails, a configuration that cannot be read) gives `high`, the
level every reviewer ran at before. Every `high` says why on standard error.
"""

import re
import subprocess
import sys
from pathlib import Path

import workflow_config
from workflow_commands import CommandTimeout, github_read

HERE = Path(__file__).resolve().parent
MARK = "Review: high"
# Shipline's own scripts that hold locks, slots or queues, beside this file.
OWN_SCRIPTS = re.compile(
    r"(session|tick|review-panel|merge[-_][^/]*|github_budget\.py|[a-z_]+_slots\.py"
    r"|workflow_commands\.py)"
)
_HEADER = "diff --git"
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


class Unknown(Exception):
    """Why the level cannot be told."""


def _unquote(text: str) -> str:
    """A path as git writes it between quotes: C escapes, and octal for each byte not ASCII."""
    if len(text) < 2 or not text.endswith('"'):
        raise ValueError(text)
    raw, inner, at = bytearray(), text[1:-1], 0
    while at < len(inner):
        char = inner[at]
        if char == '"':
            raise ValueError(text)
        if char != "\\":
            raw += char.encode()
            at += 1
        elif inner[at + 1 : at + 2] in _ESCAPES:
            raw.append(_ESCAPES[inner[at + 1]])
            at += 2
        elif re.fullmatch(r"[0-7]{3}", inner[at + 1 : at + 4]):
            raw.append(int(inner[at + 1 : at + 4], 8))
            at += 4
        else:
            raise ValueError(text)
    return raw.decode()


def _path(text: str, prefix: str = "") -> str | None:
    """One side of a change as a line names it: None for `/dev/null`."""
    # Git ends the name with a tab when it holds a space, after its closing quote too.
    text = text.removesuffix("\t")
    if text.startswith('"'):
        text = _unquote(text)
    if text == "/dev/null":
        return None
    if not text.startswith(prefix) or len(text) == len(prefix):
        raise ValueError(text)
    return text[len(prefix) :]


def _header(rest: str) -> list[str]:
    """The paths of `diff --git a/X b/Y` when its words say them for certain, else none."""
    if not rest.startswith(" "):
        raise ValueError(rest)
    rest = rest[1:]
    if rest.startswith('"'):
        end = re.match(r'"(?:[^"\\]|\\.)*"', rest)
        if not end or rest[end.end() : end.end() + 1] != " ":
            raise ValueError(rest)
        return [_path(rest[: end.end()], "a/") or "", _path(rest[end.end() + 1 :], "b/") or ""]
    if rest.endswith('"') and ' "' in rest:
        old, new = rest.rsplit(' "', 1)
        return [_path(old, "a/") or "", _path('"' + new, "b/") or ""]
    # `a/P b/P`: the same path twice, which a space inside it cannot make ambiguous.
    half = (len(rest) - 5) // 2
    if half > 0 and rest == f"a/{rest[2 : 2 + half]} b/{rest[2 : 2 + half]}":
        return [rest[2 : 2 + half]]
    if rest.count(" b/") == 1 and rest.startswith("a/"):
        old, new = rest.split(" b/")
        return [old[2:], new] if old[2:] and new else []
    return []


def changed_paths(diff: str) -> list[str]:
    """Every path a pull request's diff names, both sides of a rename or a deletion."""
    if not diff.strip():
        raise Unknown("the diff is empty")
    # Git ends a line only at a line feed; `splitlines` would cut a path at other characters.
    lines = diff.removesuffix("\n").split("\n")
    if not lines[0].startswith(_HEADER):
        raise Unknown(f"the diff does not start with a file: {lines[0][:200]!r}")
    paths: list[str] = []
    blocks: list[list[str]] = []
    for line in lines:
        if line.startswith(_HEADER):
            blocks.append([line])
        elif not blocks[-1][-1].startswith("@@"):
            # The lines of a block above its first hunk; hunk lines may look like them.
            blocks[-1].append(line)
    for block in blocks:
        found: list[str] = []
        try:
            found += _header(block[0][len(_HEADER) :])
            for line in block[1:]:
                for word, prefix in (("--- ", "a/"), ("+++ ", "b/")):
                    if line.startswith(word):
                        found.append(_path(line[len(word) :], prefix) or "")
                for word in ("rename from ", "rename to ", "copy from ", "copy to "):
                    if line.startswith(word):
                        found.append(_path(line[len(word) :]) or "")
                binary = re.fullmatch(r"Binary files (.+) and (.+) differ", line)
                if binary and line.count(" and ") == 1:
                    found += [_path(binary[1], "a/") or "", _path(binary[2], "b/") or ""]
        except ValueError:
            # Undecodable bytes too: a UnicodeDecodeError is a ValueError.
            raise Unknown(
                f"the diff names a path it cannot be read from: {block[0][:200]!r}"
            ) from None
        found = [path for path in found if path]
        if not found:
            raise Unknown(f"the diff does not say which file this is: {block[0][:200]!r}")
        paths += found
    return list(dict.fromkeys(paths))


def _own_prefix() -> str:
    """Where Shipline's scripts are in the checkout, as the paths of a diff name them."""
    done = subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "--show-prefix"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if done.returncode:
        raise Unknown(f"cannot tell where Shipline's scripts are: {done.stderr.strip()}")
    return done.stdout.strip()


def _read(*arguments: str) -> str:
    try:
        done = github_read(*arguments)
    except CommandTimeout as error:
        raise Unknown(str(error)) from None
    if done.returncode:
        said = (done.stderr or done.stdout).strip()[-500:]
        raise Unknown(f"gh {' '.join(arguments[:2])} {arguments[2]} failed: {said}")
    return done.stdout


def level(number: str) -> tuple[str, str]:
    """The level, and why when it is high."""
    config = workflow_config.current()
    body = _read("pr", "view", number, "--json", "body", "--jq", ".body")
    if any(line.strip() == MARK for line in body.splitlines()):
        return "high", f"the description says {MARK}"
    paths = changed_paths(_read("pr", "diff", number, "--color", "never"))
    own = re.compile(re.escape(_own_prefix()) + OWN_SCRIPTS.pattern)
    for path in paths:
        if own.fullmatch(path):
            return "high", f"{path} is one of Shipline's own concurrent scripts"
        for pattern in config.review.high_effort_paths:
            if re.search(pattern, path):
                return "high", f"{path} matches {pattern} of review.high_effort_paths"
    return "medium", ""


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9]+", sys.argv[1]):
        print("usage: review-effort N", file=sys.stderr)
        return 2
    try:
        found, reason = level(sys.argv[1])
    # A ValueError: a diff that is not UTF-8, which `gh`'s output is read as.
    except (
        Unknown,
        workflow_config.ConfigError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        found, reason = "high", f"cannot tell: {error}"
    if reason:
        print(f"review-effort {sys.argv[1]}: high, as {reason}", file=sys.stderr)
    print(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
