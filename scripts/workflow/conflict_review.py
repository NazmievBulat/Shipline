"""Evidence and scope for a review which extends a passed round after a conflict.

    conflict_review.py sentence <folder> <N>    the sentence the fresh reviewer starts with
    conflict_review.py reviewer <folder> <N>    its effort and model, `high opus` as usual
    conflict_review.py panel <folder> <N> <root> <head>

The reviewer is the one `conflict_review` of `shipline.json` names while the kept worktree holds
the conflict's merge and it stays within `max_changed_lines`: both sides of every conflicting
hunk before the merge is committed, the lines the resolution wrote there after, and no other
file. The choice is noted in `review-N.conflict-reviewer`, so the gate can hand a resolution
that grew past it to the usual reviewer (`review-N.usual-reviewer`, which keeps that head with
the usual reviewer).
"""

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import workflow_config
from merge_handoff import write_json
from workflow_commands import GIT_READ_TIMEOUT, CommandTimeout, run

_ROUND = re.compile(r"^<!-- workflow-review round=([0-9]+) result=(sent-back|passed) -->")
_SHA = re.compile(r"[0-9a-f]{40}")
_PATH = re.compile(r"[A-Za-z0-9_.@/-]+")
# The reviewer of every conflict when shipline.json names no other.
USUAL = ("high", "opus")


def _git(root: Path, *args: str, check: bool = True) -> str:
    done = run("git", "-C", str(root), *args, timeout=GIT_READ_TIMEOUT)
    if check and done.returncode:
        raise ValueError(done.stderr or done.stdout)
    return done.stdout


def _blob(root: Path, ref: str, file: str) -> str:
    # text=True normalizes CRLF, which would hide edits to every untouched line.
    return subprocess.check_output(
        ["git", "-C", str(root), "show", f"{ref}:{file}"], timeout=GIT_READ_TIMEOUT
    ).decode("utf-8")


def passed(reviews: list[dict[str, Any]], head: str) -> dict[str, Any] | None:
    """The latest marked round on this head must have passed."""
    marked = [
        (int(match[1]), index, match[2], review)
        for index, review in enumerate(reviews)
        if review.get("commit_id") == head and (match := _ROUND.match(review.get("body") or ""))
    ]
    latest = max(marked, key=lambda item: item[:2]) if marked else None
    return latest[3] if latest and latest[2] == "passed" else None


@dataclass
class Scope:
    head: str
    main: str
    files: list[str]

    @staticmethod
    def path(folder: Path, number: str) -> Path:
        return folder / f"review-{number}.conflict.json"

    @classmethod
    def read(cls, folder: Path, number: str) -> Scope | None:
        path = cls.path(folder, number)
        if not path.exists():
            return None
        scope = cls(**json.loads(path.read_text()))
        if (
            not isinstance(scope.head, str)
            or not _SHA.fullmatch(scope.head)
            or not isinstance(scope.main, str)
            or not _SHA.fullmatch(scope.main)
            or not isinstance(scope.files, list)
            or not scope.files
            or any(
                not isinstance(file, str)
                or not _PATH.fullmatch(file)
                or file.startswith(("-", "/"))
                or ".." in file.split("/")
                for file in scope.files
            )
        ):
            raise ValueError("invalid conflict review scope")
        return scope

    def automatic(self, root: Path) -> str:
        """The tree of the automatic merge, its conflicts written in it with their markers."""
        merged = run(
            "git",
            "-C",
            str(root),
            "merge-tree",
            "--write-tree",
            self.head,
            self.main,
            timeout=GIT_READ_TIMEOUT,
        )
        tree = merged.stdout.splitlines()[0] if merged.stdout else ""
        if merged.returncode != 1 or not _SHA.fullmatch(tree):
            raise ValueError("could not reconstruct the conflicting automatic merge")
        return tree

    def block(self, sides: bool = False) -> re.Pattern[str]:
        """One conflict block of the automatic merge; with `sides`, each side a group."""
        side = "(.*?)" if sides else ".*?"
        return re.compile(
            rf"^<<<<<<< {self.head}\n{side}^=======\n{side}^>>>>>>> {self.main}\n?",
            re.MULTILINE | re.DOTALL,
        )

    def written(self, root: Path, head: str, file: str, tree: str) -> list[str]:
        """What the resolution wrote in place of each conflict block of one file. Refused when
        it changed a line outside them, as `validate` refuses it."""
        before = _blob(root, tree, file)
        after = _blob(root, head, file)
        untouched = self.block().split(before)
        if len(untouched) < 2 or "\0" in before + after:
            raise ValueError(f"{file}: cannot identify text conflict blocks")
        found = re.fullmatch("(.*?)".join(re.escape(part) for part in untouched), after, re.DOTALL)
        if found is None:
            raise ValueError(f"{file}: resolution changes lines outside conflicting hunks")
        return list(found.groups())

    def size(self, root: Path, head: str) -> tuple[int, list[str]]:
        """The lines of the conflicting files the resolution at `head` decides, and the other
        files it changes. Both sides of every conflicting hunk count, and once the merge is
        committed the lines it wrote in their place, when they are more: a large conflict
        resolved in a few lines is still a large one."""
        tree = self.automatic(root)
        sides = sum(
            len(side.splitlines())
            for file in self.files
            for found in self.block(sides=True).finditer(_blob(root, tree, file))
            for side in found.groups()
        )
        if head == self.head:
            return sides, []
        if _git(root, "rev-list", "--parents", "-n", "1", head).split() != [
            head,
            self.head,
            self.main,
        ]:
            raise ValueError(
                "resolution must be one merge of the handed-off head and recorded main"
            )
        changed = _git(root, "diff", "--no-renames", "--name-only", "-z", tree, head).split("\0")
        outside = sorted(set(filter(None, changed)) - set(self.files))
        written = sum(
            len(part.splitlines())
            for file in self.files
            for part in self.written(root, head, file, tree)
        )
        return max(sides, written), outside

    def validate(self, root: Path, head: str) -> str:
        """Refuse extra commits, changed parents, and edits outside conflict blocks."""
        parents = _git(root, "rev-list", "--parents", "-n", "1", head).split()
        if parents != [head, self.head, self.main]:
            raise ValueError(
                "resolution must be one merge of the handed-off head and recorded main"
            )
        tree = self.automatic(root)
        changed = _git(root, "diff", "--no-renames", "--name-only", "-z", tree, head).split("\0")
        if outside := set(filter(None, changed)) - set(self.files):
            raise ValueError(
                "resolution changes files outside the conflict: " + ", ".join(sorted(outside))
            )
        for file in self.files:
            # Only ordinary text conflicts can prove that untouched lines stayed untouched.
            modes = [_git(root, "ls-tree", ref, "--", file).split() for ref in (tree, head)]
            if (
                any(not entry or entry[0] not in {"100644", "100755"} for entry in modes)
                or modes[0][0] != modes[1][0]
            ):
                raise ValueError(f"{file}: non-text or mode-changing resolution needs full review")
            self.written(root, head, file, tree)
            if re.search(
                r"^(?:<<<<<<< |=======\s*$|>>>>>>> )", _blob(root, head, file), re.MULTILINE
            ):
                raise ValueError(f"{file}: conflict markers remain")
        return tree

    def diff(self, root: Path, head: str) -> str:
        return "\n".join(
            f"# Resolution against parent {parent}\n"
            + _git(root, "diff", "--no-ext-diff", parent, head, "--", *self.files)
            for parent in (self.head, self.main)
        )


def prepare(
    folder: Path,
    number: str,
    root: Path,
    request: dict[str, Any],
    published_head: str,
    reviews: list[dict[str, Any]],
) -> None:
    path = Scope.path(folder, number)
    path.unlink(missing_ok=True)
    head = request.get("head", "")
    if (
        "non-mechanical conflict in " not in request.get("reason", "")
        or head != published_head
        or not passed(reviews, head)
        # An unreadable worktree must not keep the reviewer from starting at all.
        or _git(root, "rev-parse", "HEAD", check=False).strip() != head
    ):
        return
    main = _git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False).strip()
    files = _git(root, "diff", "--name-only", "--diff-filter=U", "-z", check=False).split("\0")
    files = sorted(filter(None, files))
    if _SHA.fullmatch(main) and files:
        write_json(path, asdict(Scope(head, main, files)))
        try:
            Scope.read(folder, number)
        except ValueError:
            path.unlink()  # Unusual path names use the full review sentence.


def _holds(folder: Path, number: str, scope: Scope) -> bool:
    """The kept worktree is still at this merge: unresolved, or committed as that one merge."""
    root = _worktree(folder, number)
    head = _git(root, "rev-parse", "HEAD", check=False).strip()
    if head == scope.head:
        return _git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False).strip() == scope.main
    parents = _git(root, "rev-list", "--parents", "-n", "1", "HEAD", check=False).split()
    return parents == [head, scope.head, scope.main]


def _worktree(folder: Path, number: str) -> Path:
    return workflow_config.current().checkout_of(folder) / ".claude/worktrees" / f"ticket-{number}"


def reviewer(folder: Path, number: str) -> tuple[str, str]:
    """The effort and model of the fresh reviewer of pull request N, noted for the gate."""
    named = workflow_config.current().conflict_review
    record = folder / f"review-{number}.conflict-reviewer"
    usual = folder / f"review-{number}.usual-reviewer"
    chosen, scope = USUAL, None
    try:
        root = _worktree(folder, number)
        head = _git(root, "rev-parse", "HEAD").strip()
        # The gate sent this head to the usual reviewer: it stays there until the head moves.
        if usual.exists() and usual.read_text().strip() != head:
            usual.unlink()
        if named is not None and not usual.exists():
            scope = Scope.read(folder, number)
            if scope and _holds(folder, number, scope):
                lines, outside = scope.size(root, head)
                if not outside and lines <= named.max_changed_lines:
                    chosen = (named.effort, named.model)
    except ValueError, TypeError, OSError, subprocess.SubprocessError, CommandTimeout:
        # A conflict that cannot be measured gets the usual reviewer.
        chosen = USUAL
    if chosen == USUAL or scope is None:
        record.unlink(missing_ok=True)
    else:
        write_json(
            record,
            {"head": scope.head, "main": scope.main, "effort": chosen[0], "model": chosen[1]},
        )
    return chosen


def named_reviewer(folder: Path, number: str) -> bool:
    """Whether the reviewer last started for pull request N is the one `conflict_review`
    names: every start notes its choice anew."""
    return (folder / f"review-{number}.conflict-reviewer").exists()


def sentence(folder: Path, number: str) -> str:
    try:
        scope = Scope.read(folder, number)
    except ValueError, TypeError:
        scope = None
    # A scope outlives its round. A later ordinary start (after a send-back, a worker's
    # push or a rule break) finds the worktree moved on and gets the ordinary sentence.
    if scope and _holds(folder, number, scope):
        return (
            f"Review conflict resolution for pull request {number} extending passed head "
            f"{scope.head} in files {', '.join(scope.files)}"
        )
    return f"Review pull request {number}"


def panel(folder: Path, number: str, root: Path, head: str, reviews: list[dict[str, Any]]) -> bool:
    """Write the panel's evidence, or state why an ordinary full review is required."""
    try:
        scope = Scope.read(folder, number)
        if scope is None:
            return False
        earlier = passed(reviews, scope.head)
        if earlier is None:
            raise ValueError("no passed round on the handed-off head")
        scope.validate(root, head)
    except (ValueError, TypeError) as error:
        reason = f"Full review required: {error}.\n"
        with (root / ".review/task.md").open("a") as task:
            task.write("\n" + reason)
        sys.stderr.write(reason)
        return False
    with (root / ".review/task.md").open("a") as task:
        task.write(
            f"\n## Conflict round extending passed head {scope.head}\n\n"
            "Review only the resolution against both parents and the tests reached from the "
            "conflicting files. The earlier passed round stands for untouched changes. "
            "New logic or changed test expectations beyond resolving the conflict need a full "
            "review; report that fallback and its reason.\n\n"
            f"Conflicting files: {', '.join(scope.files)}\n\n"
            f"Earlier passed independent round:\n\n{earlier['body']}\n"
        )
    (root / ".review/diff.patch").write_text(scope.diff(root, head))
    (root / ".review/conflict-files.txt").write_text("\n".join(scope.files) + "\n")
    # Check weakened tests against BOTH parents; a branch may have relaxed a main test.
    from weaker_tests import _is_test, compare, markdown

    items = []
    for file in scope.files:
        if _is_test(file):
            after = _git(root, "show", f"{head}:{file}")
            for parent in (scope.head, scope.main):
                items.extend(compare(file, _git(root, "show", f"{parent}:{file}"), after))
    (root / ".review/weaker-tests.md").write_text(markdown(items))
    return True


def main() -> None:
    action, folder_arg, number = sys.argv[1:4]
    folder = Path(folder_arg)
    if action == "sentence":
        sys.stdout.write(sentence(folder, number) + "\n")
    elif action == "reviewer":
        sys.stdout.write(" ".join(reviewer(folder, number)) + "\n")
    elif action == "panel":
        root = Path(sys.argv[4])
        head = sys.argv[5]
        reviews = json.load(sys.stdin)
        sys.exit(0 if panel(folder, number, root, head, reviews) else 2)


if __name__ == "__main__":
    main()
