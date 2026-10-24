"""Conservative, advisory comparison of test strength; no model or third party parser needed."""

from __future__ import annotations

import argparse
import ast
import difflib
import io
import json
import re
import subprocess
import token
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Item:
    path: str
    line: int
    kind: str
    detail: str


# Tokenize JS strings/comments before looking for assertions: example source in a string is
# not an assertion. Keep positions/newlines for useful review locations.
_JS_TOKEN = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|"
    r"`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|===|!==|==|!=|<=|>=|\S"
)
_MARKER = re.compile(
    r"\b(?:pytest\.(?:mark\.(?:skipif|skip|xfail|flaky)|skip|xfail|importorskip)|"
    r"unittest\.(?:skipIf|skipUnless|skip|expectedFailure)|"
    r"(?:test|it|describe)\.(?:skipIf|runIf|skip|only|fixme|todo|fail|fails|failing)|"
    r"(?:retries|retry|reruns)|expect\.soft)\b"
)
# A story with a play function is a test: a story test runner runs each one.
_STORY = re.compile(r"^export const (\w+)\b[^=\n]*=", re.MULTILINE)
# Written above a story that took over a browser test:
# `// Moved from <spec>: "<title>"`.
_MOVED = re.compile(r'^// Moved from (\S+\.spec\.ts): "(.+)"$', re.MULTILINE)
_NUMBER = re.compile(r"(?<![\w])\d[\d_]*(?:\.\d+)?")
_LIMIT = re.compile(
    r"timeout|duration|elapsed|tolerance|retry|retries|reruns|bound|size|limit|approx|delta",
    re.IGNORECASE,
)


def _js_tokens(source: str) -> list[tuple[str, int]]:
    return [
        (m.group(), m.start())
        for m in _JS_TOKEN.finditer(source)
        if not m.group().startswith(("//", "/*"))
    ]


def _balanced(tokens: list[tuple[str, int]], start: int) -> int:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index][0] == "(":
            depth += 1
        elif tokens[index][0] == ")":
            depth -= 1
            if depth == 0:
                return index
    return start


def _assertions(path: str, source: str) -> list[tuple[int, str]]:
    if path.endswith(".py"):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                found.append((node.lineno, "assert " + ast.unparse(node.test)))
            elif (
                isinstance(node, ast.Call) and ast.unparse(node.func) == "pytest.raises"
            ):
                found.append((node.lineno, ast.unparse(node)))
        return sorted(found)
    tokens = _js_tokens(source)
    found = []
    for i, (word, offset) in enumerate(tokens):
        # Browser specs may delegate assertions to named page-object methods, the same
        # convention as the `expect-expect` lint rule.
        if (
            re.match(r"^(?:verify|expect)[A-Z]", word)
            and i > 0
            and tokens[i - 1][0] == "."
            and i + 1 < len(tokens)
            and tokens[i + 1][0] == "("
        ):
            end = _balanced(tokens, i + 1) + 1
            found.append(
                (
                    source.count("\n", 0, offset) + 1,
                    "".join(t[0] for t in tokens[i:end]).replace(",)", ")"),
                )
            )
            continue
        if word != "expect":
            continue
        start = i + 1
        while start < len(tokens) and tokens[start][0] in (".", "soft", "poll"):
            start += 1
        if start >= len(tokens) or tokens[start][0] != "(":
            continue
        end = _balanced(tokens, start) + 1
        # Follow .not/.resolves/.rejects and the matcher call, including multiline chains.
        while end + 1 < len(tokens) and tokens[end][0] == ".":
            end += 2
            if end < len(tokens) and tokens[end][0] == "(":
                end = _balanced(tokens, end) + 1
        found.append(
            (
                source.count("\n", 0, offset) + 1,
                "".join(t[0] for t in tokens[i:end]).replace(",)", ")"),
            )
        )
    return found


def _stories(source: str) -> list[tuple[int, str, str]]:
    starts = list(_STORY.finditer(source))
    found = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(source)
        body = source[match.end() : end]
        words = [word for word, _ in _js_tokens(body)]
        # A story made by a call, or spread from another, may get its play function there;
        # it counts as a test.
        made = bool(words) and words[0] != "{"
        # Only a story that begins by spreading another (`{ ...Other, args }`): props spread
        # inside its render do not bring a play function.
        spread = words[:4] == ["{", ".", ".", "."]
        if made or spread or "play" in words:
            found.append(
                (source.count("\n", 0, match.start()) + 1, match.group(1), body)
            )
    return found


def moved_to(source: str) -> dict[tuple[str, str], list[str]]:
    """The browser tests a story file says it took over: (spec, title) → its stories."""
    moves: dict[tuple[str, str], list[str]] = {}
    for match in _MOVED.finditer(source):
        story = _STORY.search(source, match.end())
        if story:
            moves.setdefault((match.group(1), match.group(2)), []).append(
                story.group(1)
            )
    return moves


def _tests(path: str, source: str) -> list[tuple[int, str, str]]:
    if path.endswith(".stories.tsx"):
        return _stories(source)
    if path.endswith(".py"):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        return [
            (n.lineno, n.name, ast.dump(ast.Module(body=n.body, type_ignores=[])))
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            and n.name.startswith("test_")
        ]
    tokens = _js_tokens(source)
    found = []
    for i, (word, offset) in enumerate(tokens):
        if word not in ("test", "it") or i + 2 >= len(tokens):
            continue
        start = i + 1
        while start + 1 < len(tokens) and tokens[start][0] == ".":
            modifier = tokens[start + 1][0]
            start += 2
            if modifier in ("each", "for", "skipIf", "runIf") and start < len(tokens):
                if tokens[start][0] == "(":
                    start = _balanced(tokens, start) + 1
                elif tokens[start][0].startswith("`"):
                    start += 1
        if (
            start + 1 < len(tokens)
            and tokens[start][0] == "("
            and tokens[start + 1][0].startswith(("'", '"', "`"))
        ):
            end = _balanced(tokens, start)
            found.append(
                (
                    source.count("\n", 0, offset) + 1,
                    tokens[start + 1][0],
                    "".join(t[0] for t in tokens[start + 2 : end]),
                )
            )
    return found


def _clean_lines(path: str, source: str) -> list[str]:
    if path.endswith(".py"):
        # Mask only the string/comment span: a skip's reason or assertion message must
        # not erase the executable part of that same line.
        lines = source.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        masked = list(source)
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for part in tokens:
                if part.type not in (
                    token.STRING,
                    token.COMMENT,
                ) and not token.tok_name[part.type].startswith("FSTRING"):
                    continue
                start = offsets[part.start[0] - 1] + part.start[1]
                end = offsets[part.end[0] - 1] + part.end[1]
                masked[start:end] = [
                    "\n" if c == "\n" else " " for c in source[start:end]
                ]
        except (tokenize.TokenError, IndentationError):
            pass
        return "".join(masked).splitlines()
    masked = list(source)
    for match in _JS_TOKEN.finditer(source):
        if match.group().startswith(("//", "/*", "'", '"', "`")):
            masked[match.start() : match.end()] = [
                "\n" if c == "\n" else " " for c in match.group()
            ]
    return "".join(masked).splitlines()


def _weaker(old: str, new: str) -> bool:
    if old.startswith("assert ") and new.startswith("assert "):
        previous = ast.parse(old).body[0]
        current = ast.parse(new).body[0]
        if isinstance(previous, ast.Assert) and isinstance(current, ast.Assert):
            was, now = previous.test, current.test
            if isinstance(was, ast.Compare):
                if not isinstance(now, ast.Compare) or len(now.ops) < len(was.ops):
                    return True
                inclusive = {(ast.Lt, ast.LtE), (ast.Gt, ast.GtE)}
                if any(
                    (type(a), type(b)) in inclusive for a, b in zip(was.ops, now.ops)
                ):
                    return True
    exact = (
        r"\.to(?:Be|Equal|StrictEqual|HaveCount|HaveLength|HaveText|HaveValue|"
        r"HaveBeenCalledTimes|HaveBeenCalledWith)\("
    )
    vague = (
        r"\.to(?:BeTruthy|BeDefined|BeFalsy|BeGreaterThan|BeGreaterThanOrEqual|Contain|"
        r"ContainText|Match|MatchObject|HaveBeenCalled|HaveProperty)\("
    )
    # A partial matcher inside an exact one still lets unchecked fields change.
    partial = r"expect\.(?:objectContaining|arrayContaining|stringContaining|any|anything)\("
    return bool(
        (re.search(exact, old) and re.search(vague, new))
        or (".toStrictEqual(" in old and ".toStrictEqual(" not in new)
        or (re.search(partial, new) and not re.search(partial, old))
        or ("==" in old and "==" not in new)
        or (
            "pytest.raises" in old
            and re.search(r"\b(?:Exception|BaseException)\b", new)
        )
        or ("match=" in old and "match=" not in new)
        or (".not." not in old and ".not." in new)
    )


def compare(path: str, before: str, after: str) -> list[Item]:
    """Compare full old/new sources. Deleted locations refer to the old file."""
    items: list[Item] = []
    old_asserts, new_asserts = _assertions(path, before), _assertions(path, after)
    remaining = Counter(text for _, text in new_asserts)
    removed = []
    for line, text in old_asserts:
        if remaining[text]:
            remaining[text] -= 1
        else:
            removed.append((line, text))
    added = [(line, text) for line, text in new_asserts if remaining[text]]
    # Match strongest similarities first: a removed timing check must not consume the
    # replacement for a separate exact-count assertion just because it comes first.
    pairs = sorted(
        (
            (difflib.SequenceMatcher(None, old[1], new[1]).ratio(), old, new)
            for old in removed
            for new in added
        ),
        reverse=True,
    )
    matched_old: set[tuple[int, str]] = set()
    matched_new: set[tuple[int, str]] = set()
    for similarity, old, new in pairs:
        if similarity < 0.35 or old in matched_old or new in matched_new:
            continue
        matched_old.add(old)
        matched_new.add(new)
        if _weaker(old[1], new[1]):
            items.append(
                Item(path, new[0], "assertion weakened", f"{old[1]} → {new[1]}")
            )
    for line, text in removed:
        if (line, text) not in matched_old:
            items.append(Item(path, line, "assertion removed", text))
    if len(new_asserts) < len(old_asserts):
        items.append(
            Item(
                path,
                1,
                "assertion count decreased",
                f"{len(old_asserts)} → {len(new_asserts)}",
            )
        )

    old_tests, new_tests = _tests(path, before), _tests(path, after)
    # Match names first, then renamed bodies. Literal changes in a test title or expected
    # wording are not deletions; assertion weakening is judged separately above.
    old_names = {t[1] for t in old_tests}
    new_names = {t[1] for t in new_tests}
    unmatched = [t for t in new_tests if t[1] not in old_names]
    for line, name, body in old_tests:
        if name in new_names:
            continue

        def skeleton(text: str) -> str:
            return re.sub(
                r"'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\"",
                "<literal>",
                text,
            )

        same = next((t for t in unmatched if t[2] == body), None)
        if same is None and unmatched:
            best = max(
                unmatched,
                key=lambda t: difflib.SequenceMatcher(
                    None, skeleton(body), skeleton(t[2]), autojunk=False
                ).ratio(),
            )
            similarity = difflib.SequenceMatcher(
                None, skeleton(body), skeleton(best[2]), autojunk=False
            ).ratio()
            if similarity >= 0.72:
                same = best
        if same:
            unmatched.remove(same)
        else:
            items.append(Item(path, line, "test deleted", name))

    old_lines, new_lines = _clean_lines(path, before), _clean_lines(path, after)

    # Compare a marker together with the next statement (a Python decorator belongs to
    # its test). Moving a skip from one test to another must not cancel it out globally.
    def marker_entries(lines: list[str]) -> list[tuple[int, str, str]]:
        entries = []
        for index, text in enumerate(lines):
            for match in _MARKER.finditer(text):
                context = text.strip()
                if text.lstrip().startswith("@") and index + 1 < len(lines):
                    context += lines[index + 1].strip()
                entries.append((index + 1, match.group(), context))
        return entries

    markers = Counter(
        (marker, context) for _, marker, context in marker_entries(old_lines)
    )
    for number, marker, context in marker_entries(new_lines):
        key = (marker, context)
        if markers[key]:
            markers[key] -= 1
        else:
            items.append(Item(path, number, "skip/focus/retry marker added", context))
    # Compare changed lines with the same expression after replacing numeric literals.
    numeric: dict[str, list[tuple[int, list[float], str]]] = {}
    for number, line in enumerate(old_lines, 1):
        if _NUMBER.search(line):
            key = re.sub(r"\s+", "", _NUMBER.sub("#", line))
            numeric.setdefault(key, []).append(
                (
                    number,
                    [float(n.replace("_", "")) for n in _NUMBER.findall(line)],
                    line,
                )
            )
    for number, line in enumerate(new_lines, 1):
        key = re.sub(r"\s+", "", _NUMBER.sub("#", line))
        if key not in numeric or not numeric[key]:
            continue
        _, values, old = numeric[key].pop(0)
        new = [float(n.replace("_", "")) for n in _NUMBER.findall(line)]
        # Inspect the operator beside each number, so reversed comparisons and chains
        # have the right direction (100 < size is a minimum, size < 100 a maximum).
        looser = False
        for index, match in enumerate(_NUMBER.finditer(line)):
            left = re.search(r"([<>]=?)\s*$", line[: match.start()])
            right = re.match(r"\s*([<>]=?)", line[match.end() :])
            if left:
                upper = left.group(1).startswith("<")
                lower = not upper
            elif right:
                lower = right.group(1).startswith("<")
                upper = not lower
            else:
                lower = "toBeGreaterThan" in line
                upper = (
                    "toBeLessThan" in line
                    or "abs=" in line
                    or "rel=" in line
                    or bool(_LIMIT.search(line) and not lower)
                )
            looser |= (new[index] > values[index] and upper) or (
                new[index] < values[index] and lower
            )
        if "toBeCloseTo" in line and len(values) >= 2 and new[-1] < values[-1]:
            looser = True
        if looser:
            items.append(
                Item(
                    path,
                    number,
                    "numeric limit loosened",
                    f"{old.strip()} → {line.strip()}",
                )
            )
    return items


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def _is_test(path: str) -> bool:
    p = Path(path)
    if p.name in {"playwright.config.ts", "vitest.config.ts", "pytest.ini", "tox.ini"}:
        return True
    return (
        (p.suffix == ".py" and (p.name.startswith("test_") or "tests" in p.parts))
        or bool(re.search(r"\.(?:test|spec|stories|play)\.[cm]?[jt]sx?$", path))
        or path.startswith("tests/pages/")
    )


def report(base: str, head: str = "HEAD", working: bool = False) -> list[Item]:
    ancestor = _git("merge-base", base, head).strip()
    args = ["diff", "--name-status", "-z", "--find-renames", ancestor]
    if not working:
        args.append(head)
    fields = _git(*args).split("\0")
    items = []
    moves: dict[tuple[str, str], list[str]] = {}
    index = 0
    while index < len(fields) and fields[index]:
        status, old_path = fields[index : index + 2]
        index += 2
        new_path = old_path
        if status.startswith(("R", "C")):
            new_path = fields[index]
            index += 1
        if not (_is_test(old_path) or _is_test(new_path)):
            continue
        before = "" if status == "A" else _git("show", f"{ancestor}:{old_path}")
        if status == "D":
            # One item: listing each of its assertions and tests again only adds noise.
            items.append(
                Item(old_path, 1, "test file deleted", "entire test file removed")
            )
            continue
        if working:
            after = Path(new_path).read_text()
        else:
            after = _git("show", f"{head}:{new_path}")
        items.extend(compare(new_path, before, after))
        if new_path.endswith(".stories.tsx"):
            for key, stories in moved_to(after).items():
                moves.setdefault(key, []).extend(f"{new_path} › {s}" for s in stories)
    # A browser test taken over by a story in the same change is named as moved, with
    # its story: the reviewer still explains it, and reads the story beside the test.
    return [
        Item(
            item.path,
            item.line,
            "test moved to a story",
            f"{item.detail} → {', '.join(moves[key])}",
        )
        if item.kind == "test deleted"
        and (key := (item.path, item.detail.strip("\"'`"))) in moves
        else item
        for item in items
    ]


def markdown(items: list[Item]) -> str:
    lines = [
        "# Weaker tests",
        "",
        "Advisory only. Each item needs a reviewer explanation and evidence that the test still catches its bug.",
        "Deleted locations refer to the old file. This heuristic report is not proof that other tests stayed strong.",
        "",
        f"Items: {len(items)}",
        "",
    ]
    for item in items:
        detail = item.detail.replace("`", "'").replace("\n", " ")
        lines.append(f"- `{item.path}:{item.line}` — **{item.kind}**: `{detail}`")
    if not items:
        lines.append("No weaker-test candidates found.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "number",
        nargs="?",
        type=int,
        help="pull request number (read-only GitHub lookup)",
    )
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="include tracked uncommitted changes",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.number:
        task = json.loads(
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(args.number),
                    "--json",
                    "baseRefName,headRefOid",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        args.base, args.head = "origin/" + task["baseRefName"], task["headRefOid"]
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", task["baseRefName"], args.head],
            check=True,
        )
    items = report(args.base, args.head, args.working_tree)
    print(
        json.dumps([vars(item) for item in items]) if args.json else markdown(items),
        end="\n" if args.json else "",
    )


if __name__ == "__main__":
    main()
