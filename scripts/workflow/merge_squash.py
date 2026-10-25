"""Squash trees, messages and bounded merge attempts."""

import json
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def _merge_tree(base: str, head: str) -> str | None:
    from merge_gate import Refused, command, git

    if command("git", "cat-file", "-e", head, check=False).returncode:
        git("fetch", "origin", head)
    done = command("git", "merge-tree", "--write-tree", base, head, check=False)
    if done.returncode == 1:
        return None
    if done.returncode:
        raise Refused(f"could not build batch tree: {done.stderr.strip()}")
    tree = done.stdout.splitlines()[0]
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise Refused("git did not return a batch tree")
    return tree


def changed_files(base: str, head: str) -> list[str]:
    """The files a pull request's change touches: from where it left main to its head."""
    from merge_gate import command, git

    for commit, ref in ((base, "refs/heads/main"), (head, head)):
        if command("git", "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
            git("fetch", "origin", ref)
    fork = command("git", "merge-base", base, head).stdout.strip()
    listed = command("git", "diff", "--name-only", "--no-renames", "-z", fork, head).stdout
    return [path for path in listed.split("\0") if path]


def _taken_migrations(head: str, base: str) -> list[str]:
    """The branch's numbered files whose number main took, by the repository's command."""
    from merge_gate import Refused, nonblank_lines, repository_command, said

    done = repository_command("mechanical_conflicts", "taken", head, base)
    if done is None:
        return []
    if done.returncode:
        raise Refused(f"could not compare numbered files: {said(done)}")
    return nonblank_lines(done.stdout)


def squash_proof(
    base: str,
    tree: str,
    evidence: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> str:
    """The `Workflow-Proof` line of a squash, or none where the repository writes no proof."""
    from merge_gate import CI_PLAN, CONFIG, NOTES, RECEIPT, _planner_notes, passed_jobs

    if RECEIPT is None:
        return ""
    sources = []
    for key, (run, parts) in evidence.items():
        workflow = key.split("_", 1)[0]
        if workflow not in ("ci", "e2e") and not (
            workflow == "images" and "images" in CONFIG.checks.prefix_only
        ):
            continue
        passed = sorted(passed_jobs(parts))
        if workflow == "ci":
            # The web job can pass with only lint, or without its story tests. Main needs both.
            if (NOTES.web_suite, "whole") not in _planner_notes(parts, CI_PLAN):
                passed = [name for name in passed if name != CONFIG.checks.web]
        sources.append({"workflow": workflow, "run": run["id"], "jobs": passed})
    return RECEIPT + json.dumps(
        {"base": base, "tree": tree, "sources": sources}, separators=(",", ":")
    )


def commit_list(number: str) -> str:
    """GitHub's own squash body, the branch's commit messages, which a body file replaces.

    Main's history keeps them; when they cannot be read, the squash carries the proof alone."""
    from merge_gate import REPOSITORY, Waiting, read_github

    try:
        done = read_github("pr", "view", number, "-R", REPOSITORY, "--json", "commits")
        commits = json.loads(done.stdout)["commits"] if not done.returncode else []
        return "\n\n".join(
            "* " + "\n\n".join(part for part in (c["messageHeadline"], c["messageBody"]) if part)
            for c in commits
        )
    except (Waiting, ValueError, KeyError, TypeError) as error:
        print(f"could not read the commits of pull request {number}: {error}", flush=True)
        return ""


@contextmanager
def squash_body(number: str, proof: str) -> Iterator[str]:
    from merge_gate import commit_list

    listed = commit_list(number)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt") as body:
        body.write("\n\n".join(part for part in (listed, proof) if part) + "\n")
        body.flush()
        yield body.name


def _batch_squash(number: str, head: str, base: str, proof: str) -> str | None:
    """Squash one member: None once GitHub shows it merged, otherwise why it did not."""
    from merge_gate import (
        REPOSITORY,
        SQUASH_ATTEMPTS,
        SQUASH_RETRY_SECONDS,
        command,
        git,
        read_pull,
        squash_body,
    )

    reason = ""
    moved = f"main moved before batch member {number} merged; it needs fresh tests"
    for attempt in range(1, SQUASH_ATTEMPTS + 1):
        # A retry follows a wait, during which main may have moved to an untested tree.
        if attempt > 1 and git("ls-remote", "origin", "refs/heads/main").split()[0] != base:
            return moved
        with squash_body(number, proof) as body:
            done = command(
                "gh",
                "pr",
                "merge",
                number,
                "-R",
                REPOSITORY,
                "--body-file",
                body,
                "--squash",
                "--match-head-commit",
                head,
                check=False,
            )
        if not done.returncode or read_pull(number)["state"] == "MERGED":
            return None
        reason = done.stderr.strip() or done.stdout.strip()
        print(f"squash of {number} refused (attempt {attempt}): {reason}", flush=True)
        if git("ls-remote", "origin", "refs/heads/main").split()[0] != base:
            return moved
        if attempt < SQUASH_ATTEMPTS:
            # GitHub refuses a merge for a short while after its base moved.
            command("sleep", str(SQUASH_RETRY_SECONDS))
    return f"the squash of batch member {number} failed: {reason}"
