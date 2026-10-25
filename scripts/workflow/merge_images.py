"""Image runs and workflow path selection."""

import json
import re
import sys
from collections.abc import Callable, Iterator
from typing import Any

import workflow_config


class MainImages:
    """Main's image runs, newest first, read a page at a time and only as far as needed."""

    def __init__(self) -> None:
        self.found: list[dict[str, Any]] = []
        self.page = 0
        self.last = False

    def __iter__(self) -> Iterator[dict[str, Any]]:
        from merge_gate import API, WORKFLOW_FILES, workflow_runs

        index = 0
        while True:
            if index < len(self.found):
                yield self.found[index]
                index += 1
                continue
            if self.last:
                return
            # Nightly runs fill pages too: a missing page must never forget an older failure.
            self.page += 1
            runs_page = workflow_runs(
                "images",
                f"{API}/actions/workflows/{WORKFLOW_FILES['images']}/runs"
                f"?branch=main&per_page=100&page={self.page}",
                paginate=False,
            )
            self.last = len(runs_page) < 100
            self.found += [
                run
                for run in runs_page
                if run.get("head_branch") == "main"
                and run["event"] not in ("pull_request", "schedule")
            ]


def main_image_runs(head: str, recent: MainImages) -> list[dict[str, Any]]:
    # A push that changes nothing in the images gets no image run, and must not hide the failure
    # of an earlier one: main's image state is its latest completed image run, whichever push of
    # main it was for, plus any newer one still running. The head's own latest run always counts.
    from merge_gate import command, runs

    own = runs("images", head)[:1]
    ancestors = {head: True}
    found: list[dict[str, Any]] = []
    for run in recent:
        commit = run["head_sha"]
        if commit not in ancestors:
            ancestors[commit] = not command(
                "git", "merge-base", "--is-ancestor", commit, head, check=False
            ).returncode
        if not ancestors[commit]:
            continue
        found.append(run)
        # A re-run keeps its place: its first attempt was the latest completed one, and an
        # older failure it replaced must not come back while it runs again.
        if run["status"] == "completed" or run.get("run_attempt", 1) > 1:
            break
    seen = {run["id"] for run in own}
    return own + [run for run in found if run["id"] not in seen]


def main_images_needed(head: str, ci: list[dict[str, Any]]) -> bool:
    # CI records the whole push range, which may contain several commits. A later manual CI
    # run has no push payload, so consult the original push run for the same main head.
    from merge_gate import CI_PLAN, Refused, command, git, images_needed, job_notes, jobs

    push = next((run for run in ci if run["event"] == "push"), None)
    before = None
    if push:
        for job in jobs(push):
            if job["name"] != CI_PLAN:
                continue
            notes = job_notes(job)
            before = next(
                (note["message"].strip() for note in notes if note.get("title") == "Push base"),
                None,
            )
    # Before the notice existed, main's merges were single squashed commits.
    before = before or git("rev-parse", f"{head}^")
    if not re.fullmatch(r"[0-9a-f]{40}", before):
        raise Refused("main CI did not record a valid push base")
    if before != "0" * 40 and command("git", "cat-file", "-e", before, check=False).returncode:
        git("fetch", "origin", before)
    return images_needed(head, before, [], workflow_ref=head)


def images_needed(
    head: str, base: str, rollup: list[dict[str, Any]], *, workflow_ref: str | None = None
) -> bool:
    from merge_gate import (
        IMAGES,
        WORKFLOW_FILES,
        command,
        git,
        path_filter,
        pull_request_paths,
    )

    if any(check.get("name", "").startswith(IMAGES) for check in rollup):
        return True
    # Use the actual workflow's path filter, without a second maintained list of image inputs.
    # A repository that holds no image workflow has no image checks, so no change needs them.
    path = f".github/workflows/{WORKFLOW_FILES['images']}"
    if not git("ls-tree", "--name-only", workflow_ref or base, "--", path):
        return False
    workflow = git("show", f"{workflow_ref or base}:{path}")
    matches = path_filter(pull_request_paths(workflow))
    # NUL delimiters preserve filenames that Git would otherwise quote or split across lines;
    # the last NUL ends the list, and the empty entry after it is no file.
    paths = (
        command("git", "ls-tree", "-r", "--name-only", "-z", head, errors="surrogateescape")
        if base == "0" * 40
        else command("git", "diff", "--name-only", "-z", f"{base}...{head}", errors="surrogateescape")
    ).stdout.split("\0")[:-1]
    return any(matches(path) for path in paths)


def image_difference(before: str, after: str) -> tuple[list[str], bool]:
    """Exact tree differences, and whether any can change an image proof.

    Renames include both paths. The workflow itself must also stay identical for reuse,
    even when its author omitted it from its own filter.
    """
    from merge_gate import WORKFLOW_FILES, command, git

    paths = command(
        "git", "diff", "--no-renames", "--name-only", "-z", before, after,
        errors="surrogateescape",
    ).stdout.split("\0")[:-1]
    path = f".github/workflows/{WORKFLOW_FILES['images']}"
    if path in paths:
        return paths, True
    if not git("ls-tree", "--name-only", before, "--", path):
        return paths, False
    matches = path_filter(pull_request_paths(git("show", f"{before}:{path}")))
    return paths, any(matches(changed) for changed in paths)


def main_image_source(base: str) -> dict[str, Any] | None:
    """Main's last complete image proof, including a prefix proof it inherited."""
    from merge_gate import (
        IMAGES_PLAN, NOTES, MainImages, Refused, Waiting, _planner_notes, git, inherited_green, jobs,
        main_image_runs, workflow_verdict,
    )

    def parts_for(run: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return jobs(run)
        except Waiting:
            raise
        except Refused:
            return []

    receipt = inherited_green(base)
    if receipt and receipt.get("images_complete") is True:
        source = receipt.get("images_source")
        if not source:
            run_id = receipt.get("images_run")
            if type(run_id) is int and run_id > 0:
                # Older receipts proved images on exactly the recorded main tree.
                source = {"run": {"id": run_id}, "tree": receipt["tree"]}
        if source:
            found = image_source_runs(source)
            parts = parts_for(found[0]) if found else []
            if (found and workflow_verdict("images", found[0], parts)[0] == "green"
                    and complete_image_jobs(parts, allow_skipped=True)):
                return source
    for run in main_image_runs(base, MainImages()):
        parts = parts_for(run)
        if workflow_verdict("images", run, parts)[0] != "green" or not complete_image_jobs(parts, allow_skipped=True):
            return None
        trees = [message for title, message in _planner_notes(parts, IMAGES_PLAN)
                 if title == NOTES.files_tested]
        if len(trees) != 1 or not re.fullmatch(r"[0-9a-f]{40}", trees[0]):
            return None
        # A main run must actually have tested the main commit it names.
        if trees[0] != git("rev-parse", f"{run['head_sha']}^{{tree}}"):
            return None
        if not image_difference(trees[0], base)[1]:
            return {"run": {"id": run["id"], "head_sha": run["head_sha"]}, "tree": trees[0]}
        return None
    return None


def complete_image_jobs(parts: list[dict[str, Any]], *, allow_skipped: bool = False) -> bool:
    """Every selected image job passed, including every part of each selected matrix.

    Optional jobs may be skipped for a prefix proof. Main's complete receipt retains its
    stricter rule: a release job a pull request skipped still needs main's own success.
    """
    from merge_gate import IMAGES_PLAN, passed_jobs

    passed = passed_jobs(parts)
    return (
        any(job["name"] != IMAGES_PLAN and job.get("conclusion") == "success" for job in parts)
        and all(
            (allow_skipped and job.get("conclusion") == "skipped")
            or re.sub(r" \(\d+ of \d+\)$", "", job["name"]) in passed
            for job in parts
        )
    )


def image_source_runs(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the source's current attempt, never the saved run status from a receipt."""
    from merge_gate import API, MainImages, Refused, Waiting, _batch_runs, api, main_image_runs

    try:
        if "main_after" in source:
            return []  # Its member has not yet been squashed onto main.
        if "main" in source:
            return main_image_runs(source["main"], MainImages())[:1]
        if "scratch" in source:
            return _batch_runs(source["scratch"], "images")
        current = api(f"{API}/actions/runs/{source['run']['id']}")
    except Waiting:
        raise
    except Refused:
        return []
    return [current] if (
        current.get("id") == source["run"]["id"]
        and ("head_sha" not in source["run"]
             or current.get("head_sha") == source["run"]["head_sha"])
    ) else []


def prefix_image_source(member: dict[str, Any], train: Any) -> dict[str, Any] | None:
    """A borrowed prefix proof follows its source's replacement branch until it merges."""
    from merge_gate import git, main_image_source

    source = member["scratch"].get("image_source")
    if source and "main_after" in source:
        if train.prefixes and any(m["number"] == source["main_after"]
                                  for m in train.prefixes["members"]):
            return source
        if not train.prefixes:
            return None
        base = train.prefixes["base"]
        source = {"main": base, "tree": git("rev-parse", f"{base}^{{tree}}")}
    if source and "main" in source:
        # Keep the pending main reference until real image jobs prove this tree. Once
        # passed, retain their run identity just like any other borrowed proof.
        return main_image_source(source["main"]) or source
    if source and "number" in source:
        current = next((m for m in train.prefixes["members"]
                        if m["number"] == source["number"]), None) if train.prefixes else None
        if current is not None:
            scratch = current["scratch"]
            if scratch is None or "images" not in scratch["workflows"]:
                return None
            source = {"number": current["number"], "tree": current["tree"],
                      "scratch": scratch}
    return source


def pull_request_paths(workflow: str, *, error: type[Exception] | None = None) -> list[str]:
    """The `paths:` of a workflow's `pull_request:` trigger, each entry as YAML reads it.

    Each entry is a double-quoted string on a line of its own, a comment after it allowed.
    Any other line in the list is refused, never skipped: a pattern the gate did not read
    could start a run the gate never waits for.
    """
    if error is None:
        from merge_gate import Refused

        error = Refused

    patterns: list[str] = []
    trigger = listed = False
    for line in workflow.splitlines():
        if re.fullmatch(r"\s*(?:#.*)?", line):
            continue
        if not line.startswith("   "):
            trigger = re.fullmatch(r"  pull_request:\s*(?:#.*)?", line) is not None
            listed = False
        elif trigger and not line.startswith("     "):
            # An anchor (`paths: &name`) lets the push trigger share the same list.
            listed = re.fullmatch(r"    paths:\s*(?:&[^\s#]+\s*)?(?:#.*)?", line) is not None
        elif listed:
            entry = re.fullmatch(r'      - "((?:[^"\\]|\\.)+)"\s*(?:#.*)?', line)
            try:
                # Only the escapes YAML shares with JSON are read; any other is refused.
                if entry:
                    patterns.append(json.loads(f'"{entry[1]}"'))
                    continue
            except json.JSONDecodeError:
                pass
            raise error(f"could not read the Images workflow path filter: {line.strip()}")
    if not patterns:
        raise error("could not read the Images workflow path filter")
    return patterns


def path_filter(patterns: list[str]) -> Callable[[str], bool]:
    """Whether a changed file starts the workflow, by GitHub's filter pattern rules.

    The patterns are read in order and the last one that matches a file decides: a pattern
    starting with `!` takes the file back, so negated patterns alone match nothing.
    """
    rules = [
        (pattern.startswith("!"), workflow_config.path_pattern(pattern.removeprefix("!")))
        for pattern in patterns
    ]

    def matches(path: str) -> bool:
        started = False
        for negated, rule in rules:
            if rule.fullmatch(path):
                started = not negated
        return started

    return matches


def browser_needed(base: str, merge: str) -> bool:
    """Whether a merge needs browser tests at all, by the repository's `browser_plan`.

    The gate reads the exact difference from main, so a merge that changes nothing needs
    none; the planner runs on an empty diff only because it cannot tell one from a misread.
    The plan reads the changed paths, each ended by a NUL, exits 0 and prints `needed` or
    `none`. Only `none` with exit 0 means none: a plan that crashes, as a Python one does with
    exit 1, never skips the browser tests. Without a plan every merge needs them while the
    merged tree holds the E2E workflow, and none does without that file.
    """
    from merge_gate import CONFIG, WORKFLOW_FILES, command, repository_command, said

    changed = command(
        "git", "diff", "--no-renames", "--name-only", "-z", base, merge, errors="surrogateescape"
    ).stdout.split("\0")[:-1]
    if not changed:
        return False
    if CONFIG.command("browser_plan") is None:
        suite = f"{merge}:.github/workflows/{WORKFLOW_FILES['e2e']}"
        return not command("git", "cat-file", "-e", suite, check=False).returncode
    done = repository_command(
        "browser_plan", input="".join(f"{path}\0" for path in changed), errors="surrogateescape"
    )
    assert done is not None
    answer = "" if done.returncode else done.stdout.strip()
    if answer == "none":
        return False
    if answer != "needed":
        print(
            f"the browser plan could not answer, so browser tests run: {said(done)}",
            file=sys.stderr,
            flush=True,
        )
    return True


def inherited_image_jobs(
    parts: list[dict[str, Any]], tree: str, branch: str
) -> list[dict[str, Any]]:
    from merge_gate import (
        API,
        IMAGES,
        IMAGES_PLAN,
        NOTES,
        Refused,
        WORKFLOW_FILES,
        _planner_notes,
        api,
        jobs,
        passed_jobs,
    )

    notes = _planner_notes(parts, IMAGES_PLAN)
    sources = [message for title, message in notes if title == "Inherited image run"]
    if not sources:
        return parts
    if len(sources) != 1 or not sources[0].isdigit():
        raise Refused("invalid inherited image run")
    run = api(f"{API}/actions/runs/{sources[0]}")
    if (
        run.get("event") != "pull_request"
        or run.get("head_branch") != branch
        or run.get("path") != f".github/workflows/{WORKFLOW_FILES['images']}"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise Refused("inherited image run did not pass on this branch")
    original = jobs(run)
    if (NOTES.files_tested, tree) not in _planner_notes(original, IMAGES_PLAN):
        raise Refused("inherited image run tested other files")
    proven = passed_jobs(original)
    effective = []
    for job in parts:
        if job.get("conclusion") == "skipped":
            group = [
                old
                for old in original
                if old["name"] == job["name"] or old["name"].startswith(job["name"] + " (")
            ]
            if group and job["name"] in proven:
                effective.extend(group)
                continue
            if job["name"] in (f"{IMAGES}: browser tests", f"{IMAGES}: image tests"):
                raise Refused("inherited image run did not pass every required part")
        effective.append(job)
    return effective
