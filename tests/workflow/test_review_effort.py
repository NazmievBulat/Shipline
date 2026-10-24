"""`review-effort N` says the effort level of pull request N's independent reviewer: `medium`,
or `high` for the paths a repository names in `review.high_effort_paths`, for Shipline's own
concurrent scripts, for a description that says `Review: high`, and whenever it cannot tell."""

import json
from pathlib import Path

import pytest
from workflow.test_conflict_review import (  # pyright: ignore[reportPrivateUsage]
    _FILE,
    _begin_merge,
    _conflict,
    _resolve,
)
from workflow_harness import Workflow, instruction, session_arguments

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "shipline.json"

# A path of each group the example names, and why it needs a closer look.
_GROUPS = {
    "stored data": "db/migrations/0042_add_orders_index.sql",
    "what a model reads": "prompts/summary.md",
    "background work, locks and queues": "app/jobs/nightly_report.py",
    "account boundaries and security": "app/auth/sessions.py",
}
_ORDINARY = "app/views/home.html"


def _configure(workflow: Workflow, review: dict[str, object] | None = None) -> None:
    """The example's review paths, or the ones given, beside the stand-in's other values."""
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["review"] = (
        json.loads(_EXAMPLE.read_text())["review"] if review is None else review
    )
    path.write_text(json.dumps(config))


def _change(*paths: str) -> str:
    """The diff GitHub gives of a change that edits each path."""
    return "".join(
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
        for path in paths
    )


def _pull_request(workflow: Workflow, diff: str, body: str = "Backend workflow maintenance.") -> None:
    workflow.add_pull_request(7)
    (workflow.state / "diff-7").write_text(diff)
    (workflow.state / "body-7").write_text(body)


def _effort(workflow: Workflow) -> tuple[str, str]:
    done = workflow.run_script("review-effort", "7")
    assert done.returncode == 0, done.stderr
    return done.stdout, done.stderr


def _started_effort(workflow: Workflow) -> str:
    command = session_arguments(workflow.state, "review-7")[-1]
    assert command.startswith("claude --effort "), command
    return command.split()[2]


@pytest.mark.parametrize("group", list(_GROUPS))
def test_a_path_of_each_group_of_the_example_gives_high(workflow: Workflow, group: str) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_ORDINARY, _GROUPS[group]))

    level, reason = _effort(workflow)

    assert level == "high\n"
    assert _GROUPS[group] in reason


def test_an_ordinary_path_gives_medium(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_ORDINARY, "docs/getting-started.md"))

    assert _effort(workflow) == ("medium\n", "")


def test_without_paths_named_an_ordinary_change_gives_medium(workflow: Workflow) -> None:
    _pull_request(workflow, _change(_ORDINARY))

    assert _effort(workflow) == ("medium\n", "")


@pytest.mark.parametrize(
    "script",
    [
        "session",
        "tick",
        "merge-when-current",
        "merge_gate.py",
        "github_budget.py",
        "push_slots.py",
        "workflow_commands.py",
        "browser_test_slots.py",
        "review-panel",
    ],
)
def test_shipline_s_own_concurrent_scripts_always_give_high(
    workflow: Workflow, script: str
) -> None:
    _configure(workflow, {"high_effort_paths": []})
    _pull_request(workflow, _change(f"scripts/workflow/{script}"))

    assert _effort(workflow)[0] == "high\n"


def test_another_file_beside_shipline_s_scripts_gives_medium(workflow: Workflow) -> None:
    _pull_request(workflow, _change("scripts/workflow/README.md"))

    assert _effort(workflow)[0] == "medium\n"


@pytest.mark.parametrize("side", ["from", "to"])
def test_either_side_of_a_rename_counts(workflow: Workflow, side: str) -> None:
    _configure(workflow)
    marked = _GROUPS["stored data"]
    old, new = (marked, _ORDINARY) if side == "from" else (_ORDINARY, marked)
    _pull_request(
        workflow,
        f"diff --git a/{old} b/{new}\n"
        "similarity index 100%\n"
        f"rename from {old}\n"
        f"rename to {new}\n",
    )

    assert _effort(workflow)[0] == "high\n"


def test_a_deleted_path_counts(workflow: Workflow) -> None:
    _configure(workflow)
    marked = _GROUPS["account boundaries and security"]
    _pull_request(
        workflow,
        f"diff --git a/{marked} b/{marked}\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        f"--- a/{marked}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n",
    )

    assert _effort(workflow)[0] == "high\n"


def test_a_binary_file_and_a_path_with_spaces_are_read(workflow: Workflow) -> None:
    _configure(workflow, {"high_effort_paths": [r"^assets/.* logo\.png$", r"^db/"]})
    _pull_request(
        workflow,
        "diff --git a/assets/brand logo.png b/assets/brand logo.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/assets/brand logo.png and b/assets/brand logo.png differ\n",
    )

    assert _effort(workflow)[0] == "high\n"


def test_a_quoted_path_is_read_as_git_wrote_it(workflow: Workflow) -> None:
    _configure(workflow, {"high_effort_paths": ["^db/café/"]})
    _pull_request(
        workflow,
        'diff --git "a/db/caf\\303\\251/rows.sql" "b/db/caf\\303\\251/rows.sql"\n'
        "index 1111111..2222222 100644\n"
        '--- "a/db/caf\\303\\251/rows.sql"\n'
        '+++ "b/db/caf\\303\\251/rows.sql"\n'
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n",
    )

    assert _effort(workflow)[0] == "high\n"


def test_a_quoted_path_with_a_space_is_read(workflow: Workflow) -> None:
    # Git ends a name that holds a space with a tab, after its closing quote too.
    _configure(workflow, {"high_effort_paths": ["^docs/"]})
    _pull_request(
        workflow,
        'diff --git "a/app/caf\\303\\251 menu.html" "b/app/caf\\303\\251 menu.html"\n'
        "index 1111111..2222222 100644\n"
        '--- "a/app/caf\\303\\251 menu.html"\t\n'
        '+++ "b/app/caf\\303\\251 menu.html"\t\n'
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n",
    )

    assert _effort(workflow) == ("medium\n", "")


def test_a_path_is_cut_only_at_a_line_feed(workflow: Workflow) -> None:
    # Cut at the line separator, the path would be `app/x`, which the pattern misses.
    _configure(workflow, {"high_effort_paths": [r"locks\.py$"]})
    path = "app/x\u2028locks.py"
    _pull_request(workflow, _change(path))

    assert _effort(workflow)[0] == "high\n"


def test_a_diff_that_is_not_utf_8_gives_high(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, "")
    (workflow.state / "diff-7").write_bytes(_change("app/strings.po").encode() + b"+caf\xe9\n")

    level, reason = _effort(workflow)

    assert level == "high\n"
    assert reason


@pytest.mark.parametrize(
    "body",
    [
        "Review: high",
        "## Problem\n\nA change.\n\nReview: high\n",
        "## Problem\r\n\r\n  Review: high  \r\n",
    ],
)
def test_review_high_on_a_line_of_its_own_gives_high(workflow: Workflow, body: str) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_ORDINARY), body)

    level, reason = _effort(workflow)

    assert level == "high\n"
    assert "Review: high" in reason


@pytest.mark.parametrize(
    "body", ["It needs no Review: high here.", "Review: high, maybe", "review: high"]
)
def test_review_high_inside_a_line_does_not_count(workflow: Workflow, body: str) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_ORDINARY), body)

    assert _effort(workflow)[0] == "medium\n"


def test_an_empty_diff_gives_high(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, "")

    level, reason = _effort(workflow)

    assert level == "high\n"
    assert "empty" in reason


@pytest.mark.parametrize(
    "diff",
    [
        # Spaces make the two sides of this header impossible to tell apart, and nothing
        # after it names the paths.
        "diff --git a/a b/c b/a b/d\nold mode 100644\nnew mode 100755\n",
        "not a diff at all\n",
        _change(_ORDINARY) + "diff --git\n",
    ],
)
def test_an_unreadable_or_ambiguous_diff_gives_high(workflow: Workflow, diff: str) -> None:
    _configure(workflow)
    _pull_request(workflow, diff)

    level, reason = _effort(workflow)

    assert level == "high\n"
    assert reason


@pytest.mark.parametrize("read", ["diff", "view"])
def test_a_failing_gh_gives_high(workflow: Workflow, read: str) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_ORDINARY))
    (workflow.state / f"fail-{read}-7").touch()

    level, reason = _effort(workflow)

    assert level == "high\n"
    assert reason


def test_a_configuration_that_cannot_be_read_gives_high(workflow: Workflow) -> None:
    _configure(workflow, {"high_effort_paths": ["("]})
    _pull_request(workflow, _change(_ORDINARY))

    level, reason = _effort(workflow)

    assert level == "high\n"
    assert "high_effort_paths" in reason


def test_a_start_without_a_level_runs_an_ordinary_review_at_medium(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_ORDINARY))

    done = workflow.run("start", "review", "7")

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == "medium"


def test_a_start_without_a_level_runs_a_marked_review_at_high(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_GROUPS["stored data"]))

    done = workflow.run("start", "review", "7")

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == "high"
    assert _GROUPS["stored data"] in done.stderr


@pytest.mark.parametrize("level", ["low", "medium", "xhigh", "max"])
def test_a_level_given_by_hand_wins(workflow: Workflow, level: str) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_GROUPS["stored data"]))

    done = workflow.run("start", "review", "7", level)

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == level
    assert not workflow.calls("gh pr diff")


def test_a_start_that_cannot_tell_runs_at_high(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, _change(_ORDINARY))
    (workflow.state / "fail-diff-7").touch()

    done = workflow.run("start", "review", "7")

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == "high"


@pytest.mark.parametrize("said", ["exit 1", "echo low", "echo"])
def test_a_start_whose_review_effort_fails_or_says_another_word_runs_at_high(
    workflow: Workflow, said: str
) -> None:
    _pull_request(workflow, _change(_ORDINARY))
    (workflow.script.parent / "review-effort").write_text(f"#!/bin/bash\n{said}\n")

    done = workflow.run("start", "review", "7")

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == "high"


def test_a_start_that_waits_for_memory_reads_github_once_it_goes_ahead(
    workflow: Workflow,
) -> None:
    _pull_request(workflow, _change(_ORDINARY))
    workflow.pressure_file.write_text(
        "some avg10=0.00 avg60=50.00 avg300=0.04 total=1\nfull avg10=0.00 avg60=0.05 avg300=0.04 total=1\n"
    )

    done = workflow.run("start", "review", "7", env={"WORKFLOW_DEFER_START": "1"})

    assert done.returncode == 75, done.stderr
    assert not workflow.calls("gh pr diff")


@pytest.mark.parametrize("level", [None, "medium"])
def test_a_conflict_resolution_reviewer_stays_high(workflow: Workflow, level: str | None) -> None:
    _configure(workflow)
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    _begin_merge(workflow, main)
    folder = workflow.repo / ".local" / "workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )
    workflow.set_pull_request(7, "OPEN", _resolve(workflow))
    (workflow.state / "diff-7").write_text(_change(_ORDINARY))

    done = workflow.run("start", "review", "7", *([level] if level else []))

    assert done.returncode == 0, done.stderr
    arguments = " ".join(session_arguments(workflow.state, "review-7"))
    assert instruction(
        f"Review conflict resolution for pull request 7 extending passed head {head} "
        f"in files {_FILE}"
    ) in arguments
    assert _started_effort(workflow) == "high"
