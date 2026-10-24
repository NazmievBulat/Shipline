"""Passed reviews leave; real ticks and the real gate do their mechanical merge work."""

import fcntl
import json
import re
import time
from copy import deepcopy
from pathlib import Path

import pytest
from workflow.test_merge_when_current import (
    ADVISORY,
    MEMORY_SHORTAGES,
    GitHub,
    _finish_prefix,
    _restored_before_eight,
    hook_runs,
    short_of_memory,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)
from workflow_harness import Workflow, instruction, session_arguments

github = github_fixture


def _folder(workflow: Workflow) -> Path:
    return workflow.repo / ".local" / "workflow"


def _note(workflow: Workflow, number: int = 7) -> Path:
    return _folder(workflow) / f"merge-wanted-{number}"


def _started(workflow: Workflow) -> list[str]:
    return [call.split()[1] for call in workflow.calls("tmux") if " new-session " in call]


def _publish(github: GitHub, result: str = "passed", head: str | None = None) -> None:
    github.api["repos/example-org/example-app/pulls/7/reviews"] = [
        {
            "body": f"<!-- workflow-review round=1 result={result} -->\nReviewed.",
            "commit_id": head or github.head,
        }
    ]
    github.save()


def _handoff(github: GitHub, number: int = 7) -> None:
    done = github.workflow.run_script(
        "merge-when-current", "--handoff", str(number), cwd=github.workflow.worktree(number)
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.fixture
def handed(github: GitHub) -> GitHub:
    workflow = github.workflow
    # The gate fixture starts in the main checkout. Put the pull request where sessions
    # actually use it, retaining the same real origin and CI annotations.
    workflow.git("checkout", "main")
    workflow.git("worktree", "add", str(workflow.worktree(7)), "ticket/seven")
    workflow.list_pull_request(7, draft=False)
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {github.head}\n")
    workflow.git("checkout", "ticket/seven", cwd=workflow.worktree(7))
    github.pulls["7"]["labels"] = [{"name": "workflow"}]
    _publish(github)
    _handoff(github)
    workflow.run("stop", "review-7")
    github.batch_mode = True
    github.driver_mode = True
    github.save()
    return github


def test_the_tick_drives_a_red_main_fix_handed_off_behind_another_pull(handed: GitHub) -> None:
    workflow = handed.workflow
    second = workflow.worktree(8)
    workflow.git("worktree", "add", "-b", "ticket/eight", str(second), handed.base)
    workflow.git("commit", "--allow-empty", "-m", "fix: eight", cwd=second)
    head = workflow.git("rev-parse", "HEAD", cwd=second)
    workflow.git("push", "origin", "HEAD:ticket/eight", cwd=second)
    handed.pulls["8"] = {
        **deepcopy(handed.pulls["7"]),
        "headRefOid": head,
        "headRefName": "ticket/eight",
    }
    handed.ci(handed.base, head, run_id=30)
    workflow.list_pull_request(8, draft=False)
    # Keep the stopped reviewer's slot while allocating the next fake process ID.
    (workflow.state / "tmux/review-7").touch()
    workflow.running("review-8")
    (workflow.state / "tmux/review-7").unlink()
    handed.main("failure")
    handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"].append(
        {"id": 29, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    handed.save()
    path = _folder(workflow) / "merge-train.json"
    train = json.loads(path.read_text())
    train["waiting"] = ["8", "7"]
    path.write_text(json.dumps(train))

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert not _note(workflow).exists()
    assert _started(workflow) == []
    assert json.loads(path.read_text())["waiting"] == ["8"]


def test_handoff_requires_a_passed_published_round_on_this_head(github: GitHub) -> None:
    workflow = github.workflow
    for result, head in [("sent-back", github.head), ("passed", github.base)]:
        _publish(github, result, head)
        done = workflow.run_script("merge-when-current", "--handoff", "7")
        assert done.returncode == 1
        assert not _note(workflow).exists()
    _publish(github)
    (workflow.repo / "uncommitted.txt").write_text("not published")
    done = workflow.run_script("merge-when-current", "--handoff", "7")
    assert done.returncode == 1
    assert not _note(workflow).exists()


@pytest.mark.parametrize("reviewer_finishing", [False, True])
def test_a_noted_green_pull_merges_on_a_tick_without_a_model(
    handed: GitHub,
    reviewer_finishing: bool,
) -> None:
    workflow = handed.workflow
    if reviewer_finishing:
        (workflow.state / "tmux/review-7").touch()  # restart the fixture's stopped session
        notes = workflow.worktree(7) / ".local/workflow"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "review-7.idle").touch()
        workflow.screen("● review finished\n\n> \n", "review-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert _started(workflow) == []
    assert not workflow.is_running("review-7")
    assert not _note(workflow).exists()
    assert workflow.worktree(7).exists()  # next cleanup can now remove it
    assert any(
        "--match-head-commit " + handed.head in call for call in workflow.calls("gh pr merge")
    )


def test_cleanup_keeps_a_merged_worktree_until_the_handoff_finishes(handed: GitHub) -> None:
    workflow = handed.workflow
    (workflow.state / "pr" / "7").write_text(f"MERGED ticket/seven {handed.head}\n")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert workflow.worktree(7).exists()
    assert "merge handoff" in done.stdout


@pytest.mark.parametrize("blocked", [False, True], ids=["stale", "blocked and refreshable"])
def test_a_stale_note_is_refreshed_then_merged_on_a_later_tick(
    handed: GitHub, blocked: bool
) -> None:
    workflow = handed.workflow
    old = handed.base
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    handed.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    handed.main()
    handed.ci(handed.base, handed.head, tested_base=old)
    handed.save()
    if blocked:
        # Its failed check was re-run green on the older main: blocked on its head, and
        # refreshable at its turn.
        _blocked_on_its_head(handed, refreshable=True)

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != handed.head
    assert workflow.git("rev-parse", "HEAD^2", cwd=workflow.worktree(7)) == handed.base
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == refreshed
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    train = json.loads((_folder(workflow) / "merge-train.json").read_text())
    assert train["approved"]["7"] == refreshed
    assert "7" not in train["blocked"]
    assert not (workflow.state / "merged").exists()
    assert _started(workflow) == []
    assert not list(_folder(workflow).glob("merge-review-*"))
    handed.head = refreshed
    handed.pulls["7"]["headRefOid"] = refreshed
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {refreshed}\n")
    handed.ci(handed.base, refreshed, run_id=50)
    handed.save()

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert any("--match-head-commit " + refreshed in call for call in workflow.calls("gh pr merge"))
    assert _started(workflow) == []


def test_a_lost_check_is_run_again_then_merged_on_a_later_tick(handed: GitHub) -> None:
    workflow = handed.workflow
    lost = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    runs = (
        "repos/example-org/example-app/actions/workflows/ci.yml/runs"
        f"?head_sha={handed.head}&event=pull_request&per_page=100"
    )
    handed.api[runs]["workflow_runs"][0]["updated_at"] = lost
    handed.pulls["7"]["statusCheckRollup"].append(
        {
            "__typename": "CheckRun",
            "name": "CI result",
            "status": "IN_PROGRESS",
            "conclusion": None,
            "startedAt": lost,
            "detailsUrl": "https://github.com/example-org/example-app/actions/runs/20/job/22",
        }
    )
    handed.save()

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    pushed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert workflow.git("log", "-1", "--format=%s", pushed, cwd=workflow.worktree(7)) == (
        "ci: run CI again, GitHub lost CI result of run 20"
    )
    assert json.loads(_note(workflow).read_text())["head"] == pushed
    assert not (workflow.state / "merged").exists()
    assert _started(workflow) == []
    handed.head = pushed
    handed.pulls["7"]["headRefOid"] = pushed
    handed.pulls["7"]["statusCheckRollup"].pop()
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {pushed}\n")
    handed.ci(handed.base, pushed, run_id=50)
    handed.save()

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert _started(workflow) == []


_VERSIONS = "migrations/versions/"


def _write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit(workflow: Workflow, root: Path, message: str) -> str:
    workflow.git("add", "-A", cwd=root)
    workflow.git("commit", "-m", message, cwd=root)
    workflow.git("push", "origin", "HEAD", cwd=root)
    return workflow.git("rev-parse", "HEAD", cwd=root)


def _mechanical_conflict(handed: GitHub, kind: str) -> list[str]:
    """Publish a passed head, then move real main so Git produces real conflicts."""
    workflow = handed.workflow
    root, branch = workflow.repo, workflow.worktree(7)
    common_list = "# Shared list\n\n- existing\n"
    if kind == "list inside fence":
        common_list = "# Shared list\n\n```text\n- existing\n```\n"
    elif kind == "list after inline backticks":
        common_list = "```literal `x` ```\n\n```\n- existing\n```\n"
    _write(root, "docs/list.md", common_list)
    _write(root, "docs/shared.md", "Shared text.\n")
    _write(root, "docs/decisions/0001-base.md", "# ADR 0001: Base\n")
    index = "# Records\n\n- [0001](0001-base.md) Base\n"
    if kind == "decision with instructions":
        real_index = Path(__file__).resolve().parents[2] / "docs/decisions/README.md"
        index = real_index.read_text().split("## Records\n", 1)[0] + "## Records\n\n"
        index += "- [0001](0001-base.md) Base\n"
    if kind == "decision with renamed link":
        _write(
            root,
            "docs/old-reference.md",
            "".join(
                f"Unchanged paragraph {number} to preserve rename detection.\n"
                for number in range(30)
            ),
        )
    _write(root, "docs/decisions/README.md", index)
    _write(root, _VERSIONS + "0001_base.py", 'revision = "0001"\ndown_revision = None\n')
    _commit(workflow, root, "test: common numbered files")
    workflow.git("merge", "main", cwd=branch)
    if kind != "migration only":
        _write(
            branch,
            "docs/list.md",
            common_list.replace("- existing\n", "- existing\n- branch item\n"),
        )
    checks: list[str] = []
    if kind in ("migration", "same migration path", "migration only"):
        _write(
            branch,
            _VERSIONS + "0002_branch.py",
            'revision = "0002"\ndown_revision = "0001"\n# branch migration\n',
        )
        _write(
            branch,
            _VERSIONS + "0003_next.py",
            'revision = "0003"\ndown_revision = "0002"\n# next migration\n',
        )
        checks = ["tests/persistence/test_migrations.py"]
    elif kind.startswith("decision"):
        _write(branch, "docs/decisions/0002-branch.md", "# ADR 0002: Branch\n")
        _write(
            branch,
            "docs/decisions/README.md",
            index + "- [0002](0002-branch.md) Branch\n",
        )
        _write(branch, "docs/reference.md", "See [branch](decisions/0002-branch.md).\n")
        _write(branch, "reference.py", '"""See docs/decisions/0002-branch.md (ADR 0002)."""\n')
        _write(branch, "docs/shared.md", "Shared text.\nBranch follows decision record 0002.\n")
        if kind == "decision with renamed link":
            workflow.git("mv", "docs/old-reference.md", "docs/renamed-reference.md", cwd=branch)
            renamed = branch / "docs/renamed-reference.md"
            renamed.write_text(renamed.read_text() + "See [branch](decisions/0002-branch.md).\n")
        checks = ["tests/test_decision_records.py"]
    handed.head = _commit(workflow, branch, "test: reviewed branch additions")
    handed.pulls["7"]["headRefOid"] = handed.head
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {handed.head}\n")
    _publish(handed)
    _handoff(handed)
    if kind != "migration only":
        _write(
            root, "docs/list.md", common_list.replace("- existing\n", "- existing\n- main item\n")
        )
    if "migration" in kind:
        name = "0002_branch.py" if kind == "same migration path" else "0002_main.py"
        _write(
            root, _VERSIONS + name, 'revision = "0002"\ndown_revision = "0001"\n# main migration\n'
        )
    elif kind == "decision with distinct numbers":
        # The usual case: main took the number above the branch's reservation, and only
        # the index conflicts, because both appended a line at its end.
        _write(root, "docs/decisions/0003-main.md", "# ADR 0003: Main\n")
        _write(
            root,
            "docs/decisions/README.md",
            index + "- [0003](0003-main.md) Main\n",
        )
    elif kind.startswith("decision"):
        _write(root, "docs/decisions/0002-main.md", "# ADR 0002: Main\n")
        # Main's own mention of its 0002 keeps its number in a file both sides change.
        _write(root, "docs/shared.md", "Main follows ADR 0002.\nShared text.\n")
        _write(
            root,
            "docs/decisions/README.md",
            index + "- [0002](0002-main.md) Main\n",
        )
        # Another open PR has already reserved 0008. The new record must take 0009.
        workflow.fake("gh-without-files", (workflow.bin / "gh").read_text())
        workflow.fake(
            "gh",
            "#!/bin/bash\n"
            'if [[ "$*" == "pr list --state open --limit 1000 --json files" ]]; then\n'
            '  echo \'[{"files":[{"path":"docs/decisions/0008-reserved.md"}]}]\'\n'
            'else\n  exec "$(dirname "$0")/gh-without-files" "$@"\nfi\n',
        )
    handed.base = _commit(workflow, root, "test: concurrent main additions")
    handed.main()
    handed.pulls["7"]["mergeable"] = "CONFLICTING"
    handed.save()
    return checks


@pytest.mark.parametrize(
    "kind",
    ["prose", "list replacement", "mixed", "released migration", "fenced list", "overlap"],
)
def test_non_mechanical_conflicts_are_left_for_the_reviewer(handed: GitHub, kind: str) -> None:
    workflow = handed.workflow
    _mechanical_conflict(handed, "migration" if kind == "released migration" else "list")
    branch = workflow.worktree(7)
    if kind == "released migration":
        _write(
            branch,
            _VERSIONS + "0001_base.py",
            'revision = "0001"\ndown_revision = None\n# edited released file\n',
        )
    elif kind in ("fenced list", "overlap"):
        shared = "```\ncode\n```\n" if kind == "fenced list" else "- same item\n"
        _write(branch, "docs/list.md", "# Shared list\n\n- existing\n- branch item\n" + shared)
        _write(workflow.repo, "docs/list.md", "# Shared list\n\n- existing\n- main item\n" + shared)
        handed.base = _commit(workflow, workflow.repo, "test: ambiguous list content")
        handed.main()
    else:
        path = "docs/list.md" if kind == "list replacement" else "docs/prose.md"
        _write(
            branch,
            path,
            "# Branch wording\n" if kind != "list replacement" else "# Shared list\n\n- replaced\n",
        )
        _write(
            workflow.repo,
            path,
            "# Main wording\n"
            if kind != "list replacement"
            else "# Shared list\n\n- main replacement\n",
        )
        handed.base = _commit(workflow, workflow.repo, "test: non-mechanical main wording")
        handed.main()
    # Re-authorize this branch head in fixture state, without bypassing production checks.
    handed.head = _commit(workflow, branch, "test: ambiguous branch change")
    handed.pulls["7"]["headRefOid"] = handed.head
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {handed.head}\n")
    note = json.loads(_note(workflow).read_text())
    note["head"] = handed.head
    _note(workflow).write_text(json.dumps(note))
    handed.save()

    done = workflow.run_script("tick")

    assert not _note(workflow).exists(), done.stdout + done.stderr
    assert _started(workflow) == ["review-7"]
    assert workflow.git("rev-parse", "HEAD", cwd=branch) == handed.head
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head
    assert workflow.git("diff", "--name-only", "--diff-filter=U", cwd=branch)
    assert not workflow.calls("pytest")


@pytest.mark.parametrize("kind", ["list inside fence", "list after inline backticks"])
def test_an_existing_fenced_list_needs_a_reviewer(handed: GitHub, kind: str) -> None:
    workflow = handed.workflow
    _mechanical_conflict(handed, kind)

    done = workflow.run_script("tick")

    assert not _note(workflow).exists(), done.stdout + done.stderr
    assert _started(workflow) == ["review-7"]
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == handed.head
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head
    assert workflow.git("diff", "--name-only", "--diff-filter=U", cwd=workflow.worktree(7))


def test_a_shared_identical_migration_is_not_renumbered_and_run_twice(handed: GitHub) -> None:
    workflow = handed.workflow
    _mechanical_conflict(handed, "same migration path")
    _write(
        workflow.repo,
        _VERSIONS + "0002_branch.py",
        'revision = "0002"\ndown_revision = "0001"\n# branch migration\n',
    )
    handed.base = _commit(workflow, workflow.repo, "test: the same migration landed on main")
    handed.main()
    handed.save()

    done = workflow.run_script("tick")

    assert not _note(workflow).exists(), done.stdout + done.stderr
    assert _started(workflow) == ["review-7"]
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == handed.head
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head
    assert not (workflow.worktree(7) / _VERSIONS / "0003_branch.py").exists()
    assert not workflow.calls("pytest")


@pytest.mark.parametrize("problem", ["failed", "conflict", "head", "refusal", "dirty"])
def test_judgment_drops_the_note_and_starts_exactly_one_fresh_reviewer(
    handed: GitHub,
    problem: str,
) -> None:
    workflow = handed.workflow
    if problem == "failed":
        handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    elif problem == "conflict":
        handed.pulls["7"]["mergeable"] = "CONFLICTING"
    elif problem == "head":
        workflow.git(
            "commit", "--allow-empty", "-m", "fix: changed after review", cwd=workflow.worktree(7)
        )
        handed.head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
        workflow.git("push", "origin", "HEAD:ticket/seven", cwd=workflow.worktree(7))
        handed.pulls["7"]["headRefOid"] = handed.head
        (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {handed.head}\n")
    elif problem == "refusal":
        handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"] = None
    else:
        (workflow.worktree(7) / "uncommitted").write_text("unfinished")
    handed.save()

    first = workflow.run_script("tick")
    assert not _note(workflow).exists(), first.stdout + first.stderr
    assert _started(workflow) == ["review-7"], first.stdout + first.stderr
    if problem == "dirty":
        # A recorded hand-back preserves this state for judgment. Ordinary reviewer
        # starts still reject dirty/unpushed work without the hand-back record.
        assert (workflow.worktree(7) / "uncommitted").read_text() == "unfinished"
    second = workflow.run_script("tick")

    assert _started(workflow) == ["review-7"], (
        first.stdout + first.stderr + second.stdout + second.stderr
    )
    assert not (workflow.state / "merged").exists()
    assert len(list((_folder(workflow) / "merge-handbacks.pending").iterdir())) == 1


@pytest.mark.parametrize("main_state", ["pending", "red"])
def test_red_or_pending_main_keeps_the_note_without_merging_or_starting_a_model(
    handed: GitHub,
    main_state: str,
) -> None:
    workflow = handed.workflow
    if main_state == "red":
        handed.main("failure")
    else:
        handed.api["repos/example-org/example-app/actions/runs/10/jobs?per_page=100"]["jobs"][
            0
        ].update(
            status="in_progress",
            conclusion=None,
        )
    handed.save()

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr

    assert _note(workflow).exists()
    assert not (workflow.state / "merged").exists()
    assert _started(workflow) == []


@pytest.mark.parametrize("unreadable", ["main", "pull request", "pull request runs"])
def test_unreadable_github_keeps_the_note_and_merges_once_it_can_be_read(
    handed: GitHub,
    unreadable: str,
) -> None:
    workflow = handed.workflow
    jobs = "repos/example-org/example-app/actions/runs/10/jobs?per_page=100"
    main_jobs = handed.api[jobs]
    if unreadable == "main":
        handed.api[jobs] = None
        handed.save()
    elif unreadable == "pull request":
        (workflow.bin / "gh").rename(workflow.bin / "gh-stand-in")
        workflow.fake(
            "gh",
            """#!/bin/bash
if [[ "$1 $2 $3" == "pr view 7" && -e "@STATE@/github-down" ]]; then
  echo 'HTTP 502: Bad Gateway' >&2
  exit 1
fi
exec "$(dirname "$0")/gh-stand-in" "$@"
""",
        )
        (workflow.state / "github-down").touch()
    else:
        # The connection drops while the gate reads this pull request's CI runs, after
        # the pull request itself and main were read.
        (workflow.bin / "gh").rename(workflow.bin / "gh-stand-in")
        workflow.fake(
            "gh",
            """#!/bin/bash
if [[ "$1" == api && "$*" == *"ci.yml/runs?"*"event=pull_request"* &&
      -e "@STATE@/github-down" ]]; then
  echo 'Get "https://api.github.com/repos": read tcp: connection reset by peer' >&2
  exit 1
fi
exec "$(dirname "$0")/gh-stand-in" "$@"
""",
        )
        (workflow.state / "github-down").touch()

    for _ in range(2):
        done = workflow.run_script("tick")
        assert _note(workflow).exists(), done.stdout + done.stderr

    assert not (workflow.state / "merged").exists()
    assert _started(workflow) == []
    assert not (_folder(workflow) / "merge-review-7").exists()
    handed.api[jobs] = main_jobs
    handed.save()
    (workflow.state / "github-down").unlink(missing_ok=True)

    done = workflow.run_script("tick")

    assert (workflow.state / "merged").read_text() == "7", done.stdout + done.stderr
    assert _started(workflow) == []


def test_a_pending_pr_run_is_retried_without_another_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"][0].update(
        status="in_progress",
        conclusion=None,
    )
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists()
    assert _started(workflow) == []
    assert not (workflow.state / "merged").exists()


def test_a_reviewer_that_handed_off_is_stopped_while_its_checks_still_run(
    handed: GitHub,
) -> None:
    """A passed round and the handoff are the reviewer's last work: it never waits in line."""
    workflow = handed.workflow
    handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"][0].update(
        status="in_progress",
        conclusion=None,
    )
    handed.save()
    (workflow.state / "tmux/review-7").touch()  # restart the fixture's stopped session
    notes = workflow.worktree(7) / ".local/workflow"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "review-7.idle").touch()
    workflow.screen("● Handed to the tick.\n\n  review finished\n\n> \n", "review-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not workflow.is_running("review-7")
    assert json.loads(_note(workflow).read_text())["head"] == handed.head
    assert _started(workflow) == []
    assert not (workflow.state / "merged").exists()
    assert not (_folder(workflow) / "rule-breaks.pending").exists()


@pytest.mark.parametrize("already_delivered", [False, True])
def test_an_interrupted_hand_back_recovers_its_notice_without_repeating_delivery(
    handed: GitHub,
    already_delivered: bool,
) -> None:
    workflow = handed.workflow
    identity = "a" * 32
    request = _folder(workflow) / "merge-review-7"
    request.write_text(json.dumps({"reason": "failed checks", "event": identity}))
    if already_delivered:
        sent = _folder(workflow) / "merge-handbacks.sent" / identity
        sent.parent.mkdir(parents=True)
        sent.write_text("delivered")
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    handed.save()
    workflow.orchestrator("busy")

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    assert _started(workflow) == ["review-7"]
    assert not _note(workflow).exists()
    assert len(workflow.sent()) == (0 if already_delivered else 1)
    if not already_delivered:
        assert "pull request 7: failed checks" in workflow.sent()[0]
    assert not list((_folder(workflow) / "merge-handbacks.pending").glob("*"))


def test_a_hand_back_is_reported_and_its_reviewer_start_deferred_while_memory_is_short(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    handed.save()
    workflow.orchestrator("busy")
    pressure = workflow.pressure_file
    pressure.write_text("some avg10=40.00 avg60=40.00 avg300=0.00 total=0\n")
    # Fail a wait immediately: a start that waits would hold the tick's lock.
    workflow.fake("sleep", '#!/bin/bash\necho "sleep $*" >> "@STATE@/calls.log"\nexit 91\n')

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("sleep") == []
    assert _started(workflow) == []
    assert len(workflow.sent()) == 1
    assert (_folder(workflow) / "merge-review-7").exists()

    pressure.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == ["review-7"]
    assert len(workflow.sent()) == 1


def test_hand_back_reviewers_start_by_priority(handed: GitHub) -> None:
    workflow = handed.workflow
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    head = workflow.add_pull_request(8, draft=False, labels=("workflow", "priority:+high"))
    handed.pulls["8"] = {
        **deepcopy(handed.pulls["7"]),
        "headRefName": workflow.branch(8),
        "headRefOid": head,
        "labels": [{"name": "workflow"}, {"name": "priority: high"}],
    }
    handed.api["repos/example-org/example-app/pulls/8/reviews"] = []
    handed.save()
    (_folder(workflow) / "merge-review-8").write_text(
        json.dumps({"reason": "pull request checks failed", "event": "b" * 32})
    )
    # An ordinary draft, no hand-back: a blocker goes before both.
    workflow.add_pull_request(9, labels=("workflow", "priority:+blocker"))
    workflow.orchestrator("busy")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    # 7's hand-back was recorded first, but 8 is the more urgent, and 9 more urgent still.
    assert _started(workflow) == ["ticket-9", "review-8", "review-7"]


def test_a_hand_back_start_that_waits_for_memory_keeps_the_later_starts_waiting(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    handed.pulls["7"]["labels"] = [{"name": "workflow"}, {"name": "priority: high"}]
    handed.save()
    workflow.list_pull_request(7, draft=False, labels=("workflow", "priority:+high"))
    workflow.add_pull_request(9)
    workflow.orchestrator("busy")
    pressure = workflow.pressure_file
    pressure.write_text("some avg10=40.00 avg60=40.00 avg300=0.00 total=0\n")
    workflow.fake("sleep", '#!/bin/bash\necho "sleep $*" >> "@STATE@/calls.log"\nexit 91\n')

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == []
    # The medium draft after it was not even prepared.
    assert not workflow.worktree(9).exists()

    pressure.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == ["review-7", "ticket-9"]


def test_an_interrupted_hand_back_cannot_merge_with_its_old_authorization(handed: GitHub) -> None:
    workflow = handed.workflow
    request = _folder(workflow) / "merge-review-7"
    request.write_text(
        json.dumps(
            {
                "reason": "gate refusal requires judgment",
                "event": "a" * 32,
                "head": handed.head,
                "local_head": handed.head,
            }
        )
    )
    # Both records survive an interruption after saving the request, before revocation.
    # Checks remain green, so only the hand-back can prevent the old gate authorization.
    direct = workflow.run_script("merge-when-current", "--drive", "7", cwd=workflow.worktree(7))
    assert direct.returncode == 1, direct.stdout + direct.stderr
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] is None

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not (workflow.state / "merged").exists()
    assert _started(workflow) == ["review-7"]
    assert not _note(workflow).exists()
    train = json.loads((_folder(workflow) / "merge-train.json").read_text())
    assert "7" not in train["approved"]


def test_a_hand_back_preserves_the_owners_two_send_back_limit(handed: GitHub) -> None:
    workflow = handed.workflow
    handed.api["repos/example-org/example-app/pulls/7/reviews"] = [
        {"body": f"<!-- workflow-review round={n} result={outcome} -->", "commit_id": handed.head}
        for n, outcome in [(1, "sent-back"), (2, "sent-back"), (3, "passed")]
    ]
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    handed.save()

    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    assert _started(workflow) == []
    assert "owner's allowance for round 4" in first.stdout
    assert not _note(workflow).exists()
    (_folder(workflow) / "review-7.allow-round").write_text("4\n")

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert _started(workflow) == ["review-7"]


def test_the_gate_itself_refuses_a_changed_handoff_head(handed: GitHub) -> None:
    workflow = handed.workflow
    # Simulate a push after the driver's preflight, before it acquires merge.lock.
    handed.pulls["7"]["headRefOid"] = handed.base
    handed.save()

    done = workflow.run_script("merge-when-current", "--drive", "7", cwd=workflow.worktree(7))

    assert done.returncode == 1
    assert "head changed" in done.stderr
    assert not workflow.calls("gh pr merge")


def test_a_new_passed_fix_rejoins_the_train_after_a_failed_head(handed: GitHub) -> None:
    workflow = handed.workflow
    train_path = _folder(workflow) / "merge-train.json"
    train = json.loads(train_path.read_text())
    train["blocked"]["7"] = handed.head
    train_path.write_text(json.dumps(train))
    _note(workflow).unlink()
    (workflow.state / "tmux/review-7").touch()
    workflow.git("commit", "--allow-empty", "-m", "fix: failed checks", cwd=workflow.worktree(7))
    handed.head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    workflow.git("push", "origin", "HEAD:ticket/seven", cwd=workflow.worktree(7))
    handed.pulls["7"]["headRefOid"] = handed.head
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {handed.head}\n")
    handed.ci(handed.base, handed.head)
    _publish(handed)

    _handoff(handed)

    train = json.loads(train_path.read_text())
    assert "7" in train["waiting"]
    assert "7" not in train["blocked"]
    assert train["approved"]["7"] == handed.head


def test_a_failed_refresh_push_keeps_the_commit_for_one_fresh_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    old = handed.base
    workflow.git("commit", "--allow-empty", "-m", "fix: main advanced")
    handed.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    handed.main()
    handed.ci(handed.base, handed.head, tested_base=old)
    handed.save()
    (workflow.state / "fail-refresh-push").touch()
    workflow.fake(
        "git",
        """#!/bin/bash
if [[ $1 == push && -e "@STATE@/fail-refresh-push" ]]; then
  rm "@STATE@/fail-refresh-push"
  echo 'temporary push failure' >&2
  exit 1
fi
exec /usr/bin/git "$@"
""",
    )

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    assert _started(workflow) == ["review-7"]
    assert not _note(workflow).exists()
    assert not workflow.calls("gh pr merge")
    assert workflow.git("rev-parse", "HEAD^2", cwd=workflow.worktree(7)) == handed.base
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head


def _child(github: GitHub, *, conflict: bool = False) -> tuple[Path, str]:
    workflow = github.workflow
    worktree = workflow.worktree(8)
    workflow.git("worktree", "add", "-b", "ticket/eight", str(worktree), github.head)
    (worktree / "shared.txt").write_text("child content\n")
    workflow.git("add", "shared.txt", cwd=worktree)
    workflow.git("commit", "-m", "fix: child", cwd=worktree)
    workflow.git("push", "origin", "HEAD:ticket/eight", cwd=worktree)
    head = workflow.git("rev-parse", "HEAD", cwd=worktree)
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefName": "ticket/eight",
        "headRefOid": head,
        "baseRefName": "ticket/seven",
    }
    github.api["repos/example-org/example-app/pulls/8/reviews"] = []
    workflow.list_pull_request(8, draft=False)
    (workflow.state / "pr" / "8").write_text(f"OPEN ticket/eight {head}\n")
    if conflict:
        (workflow.repo / "shared.txt").write_text("main content\n")
        workflow.git("add", "shared.txt")
        workflow.git("commit", "-m", "fix: main changed")
        workflow.git("push", "origin", "main")
    github.save()
    return worktree, head


def test_a_merge_restacks_a_clean_child_and_pushes_before_starting_its_reviewer(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    worktree, head = _child(handed)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == ["review-8"]
    saved = json.loads((workflow.state / "github.json").read_text())
    assert saved["pulls"]["8"]["baseRefName"] == "main"
    remote = workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0]
    assert remote != head
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == remote
    assert workflow.git("rev-parse", "HEAD^2", cwd=worktree) == workflow.git(
        "rev-parse", "origin/main"
    )
    assert (worktree / "shared.txt").read_text() == "child content\n"
    assert not _note(workflow).exists()


@pytest.mark.parametrize("stacked", [False, True], ids=["handoff", "stacked child"])
def test_tick_refresh_clears_generated_output_only_in_the_refreshed_worktree(
    handed: GitHub, stacked: bool
) -> None:
    workflow = handed.workflow
    if stacked:
        target, old_head = _child(handed)
        branch = "ticket/eight"
    else:
        target = workflow.worktree(7)
        old_head = handed.head
        branch = "ticket/seven"
        old_base = handed.base
        workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
        handed.base = workflow.git("rev-parse", "HEAD")
        workflow.git("push", "origin", "main")
        handed.main()
        handed.ci(handed.base, handed.head, tested_base=old_base)
        handed.save()
    config = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    given = json.loads(config.read_text())
    given["refresh"] = {"clear_generated": ["generated/types"]}
    config.write_text(json.dumps(given))
    exclude = Path(workflow.git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.write_text(exclude.read_text() + "\ngenerated/\n")
    for checkout in [workflow.repo, target, workflow.worktree(7)]:
        generated = checkout / "generated/types"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "stale").write_text("obsolete route\n")
    other = workflow.root / "other-worktree"
    workflow.git("worktree", "add", "--detach", str(other), "main")
    (other / "generated/types").mkdir(parents=True)
    (other / "generated/types/stale").write_text("another worktree\n")
    hooks = workflow.bin / "generated-hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/bash\nif [[ -e generated/types/stale ]]; then echo 'stale generated output' >&2; exit 1; fi\n")
    hook.chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not (target / "generated/types/stale").exists()
    assert (workflow.repo / "generated/types/stale").exists()
    assert (other / "generated/types/stale").read_text() == "another worktree\n"
    # A merged parent's worktree may be removed by the tick's ordinary cleanup.
    if stacked and workflow.worktree(7).exists():
        assert (workflow.worktree(7) / "generated/types/stale").exists()
    remote = workflow.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0]
    assert remote != old_head
    assert remote == workflow.git("rev-parse", "HEAD", cwd=target)


def test_an_unlabelled_stacked_child_keeps_its_audit_path(handed: GitHub) -> None:
    workflow = handed.workflow
    worktree, head = _child(handed)
    handed.pulls["8"]["labels"] = []
    workflow.list_pull_request(8, draft=False, labels=())
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == []
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) != head
    saved = json.loads((workflow.state / "github.json").read_text())
    assert saved["pulls"]["8"]["baseRefName"] == "main"
    assert len(list((_folder(workflow) / "merge-handbacks.pending").iterdir())) == 1


def test_a_stacked_cloud_workers_branch_is_left_to_its_own_session(handed: GitHub) -> None:
    workflow = handed.workflow
    worktree, head = _child(handed)
    # A cloud reservation counts as its own session even before the address arrives.
    record = _folder(workflow) / "cloud-8"
    record.write_text(json.dumps({"url": "", "status": "starting", "last_comment_at": None}))

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == head
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0] == head
    assert _started(workflow) == []
    assert record.exists()
    saved = json.loads((workflow.state / "github.json").read_text())
    assert saved["pulls"]["8"]["baseRefName"] == "main"
    notices = list((_folder(workflow) / "merge-handbacks.pending").iterdir())
    assert any("its own session must merge main" in notice.read_text() for notice in notices)


def test_a_restart_finishes_a_child_whose_base_was_already_changed(handed: GitHub) -> None:
    workflow = handed.workflow
    worktree, head = _child(handed)
    # Represent a crash immediately after recording children and PATCHing the base.
    saved = json.loads(_note(workflow).read_text())
    saved["children"] = ["8"]
    _note(workflow).write_text(json.dumps(saved))
    handed.pulls["7"]["state"] = "MERGED"
    handed.pulls["8"]["baseRefName"] = "main"
    # A real squash is a different commit even for an identical tree.
    squash = workflow.git(
        "commit-tree", f"{handed.head}^{{tree}}", "-p", handed.base, "-m", "Squash seven"
    )
    workflow.git("push", "origin", f"{squash}:main")
    (workflow.state / "pr" / "7").write_text(f"MERGED ticket/seven {handed.head}\n")
    handed.save()

    done = workflow.run_script("tick")

    assert not _note(workflow).exists(), done.stdout + done.stderr
    assert workflow.git("rev-parse", "HEAD^2", cwd=worktree) == squash
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) != head
    assert _started(workflow) == ["review-8"]


def test_an_unexpected_child_error_keeps_the_merged_parents_follow_up(handed: GitHub) -> None:
    workflow = handed.workflow
    worktree, head = _child(handed)
    saved = json.loads(_note(workflow).read_text())
    saved["children"] = ["8"]
    _note(workflow).write_text(json.dumps(saved))
    handed.pulls["7"]["state"] = "MERGED"
    squash = workflow.git(
        "commit-tree", f"{handed.head}^{{tree}}", "-p", handed.base, "-m", "Squash seven"
    )
    workflow.git("push", "origin", f"{squash}:main")
    (workflow.state / "pr" / "7").write_text(f"MERGED ticket/seven {handed.head}\n")
    # A malformed reading of the child must not discard the parent's list of children.
    base = handed.pulls["8"].pop("baseRefName")
    handed.save()

    done = workflow.run_script("tick")

    assert json.loads(_note(workflow).read_text())["children"] == ["8"], done.stdout + done.stderr
    assert not (_folder(workflow) / "merge-review-7").exists()
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == head
    handed.pulls["8"]["baseRefName"] = base
    handed.save()

    again = workflow.run_script("tick")

    # The ordinary tick started the ready child's reviewer meanwhile; it merges main.
    assert not _note(workflow).exists(), again.stdout + again.stderr
    saved = json.loads((workflow.state / "github.json").read_text())
    assert saved["pulls"]["8"]["baseRefName"] == "main"
    assert _started(workflow) == ["review-8"]


def test_a_real_stacked_conflict_is_preserved_and_gets_one_fresh_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    worktree, head = _child(handed, conflict=True)
    handed.base = workflow.git("rev-parse", "HEAD")
    handed.main()
    handed.pulls["7"]["state"] = "MERGED"
    (workflow.state / "pr" / "7").write_text(f"MERGED ticket/seven {handed.head}\n")
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == ["review-8"]
    assert workflow.git("diff", "--name-only", "--diff-filter=U", cwd=worktree) == "shared.txt"
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == head
    merge_message = Path(
        workflow.git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_MSG", cwd=worktree)
    )
    assert merge_message.read_text() == "merge main: conflict in shared.txt\n"
    again = workflow.run_script("tick")
    assert again.returncode == 0, again.stdout + again.stderr
    assert _started(workflow) == ["review-8"]


def _handed_batch(github: GitHub, *, prepared: bool = False) -> None:
    workflow = github.workflow
    if not prepared:
        github.prepare_batch()
    workflow.git("checkout", "main")
    github.driver_mode = True
    for number, pull in github.pulls.items():
        pull["labels"] = [{"name": "workflow"}]
        workflow.git("worktree", "add", str(workflow.worktree(int(number))), pull["headRefName"])
        workflow.list_pull_request(int(number), draft=False)
        (workflow.state / "pr" / number).write_text(
            f"OPEN {pull['headRefName']} {pull['headRefOid']}\n"
        )
        github.api[f"repos/example-org/example-app/pulls/{number}/reviews"] = [
            {
                "body": "<!-- workflow-review round=1 result=passed -->",
                "commit_id": pull["headRefOid"],
            }
        ]
    github.save()
    for number in github.pulls:
        _handoff(github, int(number))
        workflow.run("stop", f"review-{number}")


def _batch_e2e_not_whole(github: GitHub, run_id: int) -> None:
    """The saved prefixes' checks pass, but the first one's browser run was not the whole
    suite."""
    first = github.batch_state()["prefixes"]["members"][0]
    batch = first["scratch"]
    github.finish_batch(run_id=run_id)
    github.e2e(
        batch["head"],
        first["tree"],
        suite="selected: 3 of 48 spec files",
        run_id=run_id + 10,
        branch=batch["branch"],
    )
    github.save()


def _notes(workflow: Workflow) -> list[str]:
    return sorted(path.name for path in _folder(workflow).glob("merge-wanted-*"))


def test_a_batch_retest_keeps_the_handoffs(github: GitHub) -> None:
    _handed_batch(github)
    workflow = github.workflow
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    old = github.batch_state()["prefixes"]["members"][0]
    assert "e2e" in old["scratch"]["workflows"]
    # The dispatched browser run's planner did not note the whole suite on the tested files
    # (the gate's own message: "retesting next call").
    _batch_e2e_not_whole(github, 100)

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert "retesting next call" in second.stdout + second.stderr
    # A mechanical retest on a new scratch branch, every note kept, no model started.
    assert _started(workflow) == []
    assert _notes(workflow) == ["merge-wanted-7", "merge-wanted-8", "merge-wanted-9"]
    assert github.workflow.calls("gh pr merge") == []
    third = workflow.run_script("tick")
    assert third.returncode == 0, third.stdout + third.stderr
    new = github.batch_state()["prefixes"]["members"][0]
    assert new["scratch"]["branch"] != old["scratch"]["branch"]
    assert new["tree"] == old["tree"]
    assert _started(workflow) == []


def test_repeated_ticks_keep_running_work_beside_a_restored_handoff(github: GitHub) -> None:
    kept = _restored_before_eight(github, passed=False)
    _handed_batch(github, prepared=True)
    workflow = github.workflow

    def tick() -> None:
        github.save()
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        saved = json.loads((workflow.state / "github.json").read_text())
        github.api, github.pulls = saved["api"], saved["pulls"]

    for _ in range(3):
        tick()
        members = github.batch_state()["prefixes"]["members"]
        eight = next(member for member in members if member["number"] == "8")
        assert eight["scratch"]["branch"] == kept["scratch"]["branch"]
        assert eight["kept"] is True
        assert _notes(workflow) == ["merge-wanted-7", "merge-wanted-8", "merge-wanted-9"]
        assert not workflow.calls("gh pr merge")
    _finish_prefix(github, "8", run_id=1100)
    tick()
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["8"]
    assert _note(workflow, 7).exists() and not _note(workflow, 8).exists()
    assert _started(workflow) == []


def test_the_same_retest_twice_in_a_row_on_one_tree_hands_back(github: GitHub) -> None:
    _handed_batch(github)
    workflow = github.workflow
    assert workflow.run_script("tick").returncode == 0
    _batch_e2e_not_whole(github, 100)
    assert workflow.run_script("tick").returncode == 0
    assert _started(workflow) == []
    # The retest's new scratch branch, on the same tested files, falls short the same way.
    assert workflow.run_script("tick").returncode == 0
    _batch_e2e_not_whole(github, 200)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "twice in a row" in done.stdout + done.stderr
    # A real fault reaches a reviewer, as every refusal did before.
    assert _started(workflow) == ["review-7"]
    assert _notes(workflow) == ["merge-wanted-8", "merge-wanted-9"]
    assert github.workflow.calls("gh pr merge") == []


def _pending_front(github: GitHub, kind: str) -> None:
    prefix = "repos/example-org/example-app/actions"
    if kind == "rollup":
        github.pulls["7"]["statusCheckRollup"][0].update(status="QUEUED", conclusion=None)
    elif kind == "ci-job":
        github.api[f"{prefix}/runs/7000/jobs?per_page=100"]["jobs"][0].update(
            status="in_progress", conclusion=None
        )
    else:
        workflow = "images" if kind.startswith("images") else "ci"
        key = (
            f"{prefix}/workflows/{workflow}.yml/runs?head_sha={github.head}"
            "&event=pull_request&per_page=100"
        )
        if kind == "images-missing":
            # The draft's skipped image plan can remain in the rollup before the
            # ready event starts its required run. Skipped is not proof of that run.
            github.pulls["7"]["statusCheckRollup"].append(
                {
                    "name": "Docker images: what to run",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                }
            )
            return
        github.api[key] = {
            "workflow_runs": [
                {
                    "id": 7000,
                    "head_sha": github.head,
                    "event": "pull_request",
                    "status": "queued" if kind.endswith("queued") else "in_progress",
                    "conclusion": None,
                    "run_attempt": 2 if kind.endswith("rerun") else 1,
                }
            ]
        }


@pytest.mark.parametrize(
    "kind",
    [
        "ci-queued",
        "ci-rerun",
        "ci-job",
        "images-queued",
        "images-rerun",
        "images-missing",
        "rollup",
    ],
)
def test_pending_front_yields_to_fresh_green_second_and_recovers_its_place(
    github: GitHub, kind: str
) -> None:
    _handed_batch(github)
    workflow = github.workflow
    # Member 8 has fresh proof; 9 is also pending and must keep waiting.
    github.ci(github.base, github.pulls["8"]["headRefOid"], run_id=8000)
    github.pulls["9"]["statusCheckRollup"][0].update(status="QUEUED", conclusion=None)
    _pending_front(github, kind)
    github.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["8"]
    train = github.batch_state()
    assert train["waiting"] == ["7", "9"]
    assert _note(workflow, 7).exists() and not _note(workflow, 8).exists()
    assert _started(workflow) == []
    saved = json.loads((workflow.state / "github.json").read_text())
    github.api, github.pulls = saved["api"], saved["pulls"]
    github.base = workflow.git("ls-remote", "origin", "refs/heads/main").split()[0]
    github.main()
    github.ci(github.base, github.head, run_id=7100)
    github.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    github.save()

    again = workflow.run_script("tick")

    assert again.returncode == 0, again.stdout + again.stderr
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["8", "7"]
    assert github.batch_state()["waiting"] == ["9"]
    assert _note(workflow, 9).exists() and not _note(workflow, 7).exists()
    assert _started(workflow) == []


@pytest.mark.parametrize("later", ["pending", "stale", "failed", "unreadable"])
def test_pending_front_waits_when_nobody_behind_has_fresh_green_checks(
    github: GitHub, later: str
) -> None:
    _handed_batch(github)
    workflow = github.workflow
    _pending_front(github, "ci-queued")
    if later == "pending":
        for number in (8, 9):
            github.pulls[str(number)]["statusCheckRollup"][0].update(
                status="QUEUED", conclusion=None
            )
    elif later == "failed":
        for number in (8, 9):
            github.pulls[str(number)]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    elif later == "unreadable":
        for number in (8, 9):
            head = github.pulls[str(number)]["headRefOid"]
            github.api[
                f"repos/example-org/example-app/actions/workflows/ci.yml/runs?head_sha={head}"
                "&event=pull_request&per_page=100"
            ] = None
    github.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not workflow.calls("gh pr merge")
    assert github.batch_state()["waiting"] == ["7", "8", "9"]
    assert github.batch_state()["prefixes"] is None
    assert _note(workflow).exists()
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == github.head


@pytest.mark.parametrize("scenario", ["finish", "settling", "recover"])
def test_ready_members_go_past_a_pending_front_and_keep_it_first(
    github: GitHub, scenario: str
) -> None:
    _handed_batch(github)
    workflow = github.workflow
    for number in (8, 9):
        github.ci(github.base, github.pulls[str(number)]["headRefOid"], run_id=number * 1000)
    _pending_front(github, "ci-rerun")
    if scenario == "settling":
        # GitHub recomputes mergeability after the preceding squash moves main.
        github.after_merge["8"] = {"9": {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}}
    github.save()

    started = workflow.run_script("tick")

    assert started.returncode == 0, started.stdout + started.stderr
    # 8's own checks tested it on this main: it merges by them, and the prefix of 9 on top of it
    # starts in the same call.
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["8"]
    assert [m["number"] for m in github.batch_state()["prefixes"]["members"]] == ["9"]
    assert github.batch_state()["waiting"] == ["7", "9"]
    saved = json.loads((workflow.state / "github.json").read_text())
    github.api, github.pulls = saved["api"], saved["pulls"]
    github.after_merge = saved["after_merge"]
    if scenario == "recover":
        # Priority returns even while the later scratch run is still queued.
        github.base = workflow.git("ls-remote", "origin", "refs/heads/main").split()[0]
        github.main()
        github.ci(github.base, github.head, run_id=7100)
        github.pulls["7"]["statusCheckRollup"] = [
            {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ]
    else:
        github.finish_batch()
    if scenario == "settling":
        # Its squash waits while GitHub has not recomputed it, and goes once it has.
        github.save()
        waited = workflow.run_script("tick")
        assert waited.returncode == 0, waited.stdout + waited.stderr
        assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["8"]
        assert _note(workflow, 9).exists() and _started(workflow) == []
        github.pulls["9"].update(mergeable="MERGEABLE", mergeStateStatus="CLEAN")
    github.save()

    finished = workflow.run_script("tick")

    assert finished.returncode == 0, finished.stdout + finished.stderr
    merged = ["8", "7"] if scenario == "recover" else ["8", "9"]
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == merged
    assert github.batch_state()["waiting"] == (["9"] if scenario == "recover" else ["7"])
    for number in (7, 8, 9):
        assert _note(workflow, number).exists() == (str(number) not in merged)
    assert _started(workflow) == []


def test_a_stale_green_member_between_pending_front_and_ready_member_keeps_waiting(
    github: GitHub,
) -> None:
    _handed_batch(github)
    workflow = github.workflow
    github.ci(github.base, github.pulls["9"]["headRefOid"], run_id=9000)
    _pending_front(github, "ci-queued")
    github.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["9"]
    assert github.batch_state()["waiting"] == ["7", "8"]
    assert github.batch_state()["prefixes"] is None
    assert _note(workflow).exists() and _note(workflow, 8).exists()
    assert not _note(workflow, 9).exists()
    assert _started(workflow) == []


def test_a_fresh_green_front_merges_alone_while_a_later_member_is_pending(
    github: GitHub,
) -> None:
    _handed_batch(github)
    workflow = github.workflow
    # Nothing waits ahead of 7, so 9's pending checks must not send 7 to a scratch batch.
    for number in (7, 8):
        github.ci(github.base, github.pulls[str(number)]["headRefOid"], run_id=number * 1000)
    github.pulls["9"]["statusCheckRollup"][0].update(status="QUEUED", conclusion=None)
    github.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["7"]
    # The prefix of 8 on the new main starts meanwhile; 9's own checks still run.
    assert [m["number"] for m in github.batch_state()["prefixes"]["members"]] == ["8"]
    assert github.batch_state()["waiting"] == ["8", "9"]
    assert not _note(workflow, 7).exists()
    assert _note(workflow, 8).exists() and _note(workflow, 9).exists()


def test_three_handed_off_members_are_batched_and_merged_without_a_model(github: GitHub) -> None:
    _handed_batch(github)
    workflow = github.workflow
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    batch = github.batch_state()["prefixes"]
    assert [member["number"] for member in batch["members"]] == ["7", "8", "9"]
    assert _started(workflow) == []
    github.finish_batch()
    github.save()

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["7", "8", "9"]
    assert _started(workflow) == []
    assert not list(_folder(workflow).glob("merge-wanted-*"))


@pytest.mark.parametrize("stop_reason", ["red-main", "unreadable-member"])
def test_a_partial_handed_batch_keeps_the_remaining_notes_when_progress_stops(
    github: GitHub, stop_reason: str
) -> None:
    _handed_batch(github)
    workflow = github.workflow
    started = workflow.run_script("tick")
    assert started.returncode == 0, started.stdout + started.stderr
    github.finish_batch()
    if stop_reason == "red-main":
        github.after_main["7"] = "red"
    else:
        # The next member becomes unreadable immediately after the first squash.
        github.after_merge["7"] = {"8": {"fail_view": True}}
    github.save()

    done = workflow.run_script("tick")

    # An unreadable GitHub may stop this tick; it must preserve all unfinished notes.
    assert done.returncode in (0, 1), done.stdout + done.stderr
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["7"]
    assert _note(workflow, 8).exists() and _note(workflow, 9).exists()
    assert _started(workflow) == []
    assert not list(_folder(workflow).glob("merge-review-*"))


def test_an_isolated_scratch_failure_hands_back_once_with_green_pr_checks(github: GitHub) -> None:
    _handed_batch(github)
    workflow = github.workflow

    def tick() -> None:
        github.save()
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        saved = json.loads((workflow.state / "github.json").read_text())
        github.api, github.pulls = saved["api"], saved["pulls"]

    tick()  # Each member's prefix starts.
    github.finish_batch(conclusion="failure")
    tick()  # The first prefix failed: its member is blocked, the ones behind it rebuilt.
    assert _started(workflow) == ["review-7"]
    assert not _note(workflow).exists()
    tick()  # No repeated reviewer; the rebuilt prefixes run.

    assert _started(workflow) == ["review-7"]
    assert not _note(workflow).exists()
    assert _note(workflow, 8).exists() and _note(workflow, 9).exists()
    assert not workflow.calls("gh pr merge")
    train = github.batch_state()
    assert train["blocked"] == {"7": github.head}
    assert "7" in train["batch_failed"]
    assert all(c["conclusion"] == "SUCCESS" for c in github.pulls["7"]["statusCheckRollup"])
    assert [m["number"] for m in train["prefixes"]["members"]] == ["8", "9"]
    github.finish_batch(run_id=300)
    tick()
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["8", "9"]
    assert _started(workflow) == ["review-7"]
    assert not list(_folder(workflow).glob("merge-wanted-*"))


def test_a_stacked_startup_lock_keeps_the_parent_note_without_stalling_the_tick(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _child(handed)
    handed.pulls["7"]["state"] = "MERGED"
    (workflow.state / "pr" / "7").write_text(f"MERGED ticket/seven {handed.head}\n")
    handed.save()
    with (_folder(workflow) / "start-8.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # The harness times out if the tick blocks behind the held startup lock.
        done = workflow.run_script("tick")
        assert "being prepared" in done.stdout + done.stderr
        assert json.loads(_note(workflow).read_text())["children"] == ["8"]
        assert _started(workflow) == []
        assert workflow.calls("gh api -X PATCH") == []
        again = workflow.run_script("tick")
        assert "the tick before this one is still running" not in again.stdout
        assert _note(workflow).exists()

    again = workflow.run_script("tick")
    assert again.returncode == 0, again.stdout + again.stderr
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-8"]


@pytest.mark.parametrize("state", ["cancelled", "rerun"])
def test_lender_cancellation_keeps_handoff_and_train_position(handed: GitHub, state: str) -> None:
    import time

    workflow = handed.workflow
    note = Path(workflow.env["HOME"]) / ".local/state/ci-overflow/replanned.json"
    note.parent.mkdir(parents=True)
    note.write_text(
        json.dumps(
            {
                "20": {
                    "state": state,
                    "head": handed.head,
                    "attempt": 1,
                    "jobs": [22],
                    "updated_at": time.time(),
                }
            }
        )
    )
    handed.pulls["7"]["statusCheckRollup"][0].update(
        conclusion="CANCELLED",
        detailsUrl="https://github.com/example-org/example-app/actions/runs/20/job/22",
    )
    run = handed.api[
        f"repos/example-org/example-app/actions/workflows/ci.yml/runs?head_sha={handed.head}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    run.update(conclusion="cancelled", run_attempt=1)
    for job in handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"][
        "jobs"
    ]:
        job["conclusion"] = "cancelled"
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists(), done.stdout + done.stderr
    assert _started(workflow) == []
    train = json.loads((_folder(workflow) / "merge-train.json").read_text())
    assert train["blocked"] == {}
    assert train["waiting"][0] == "7"
    assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("problem", ["expired", "head", "job", "attempt", "failure", "abandoned"])
def test_other_failures_still_revoke_a_handoff(handed: GitHub, problem: str) -> None:
    import time

    workflow = handed.workflow
    note = Path(workflow.env["HOME"]) / ".local/state/ci-overflow/replanned.json"
    note.parent.mkdir(parents=True)
    entry = {
        "state": "given up" if problem == "abandoned" else "rerun",
        "head": "another-head" if problem == "head" else handed.head,
        "attempt": 1,
        "jobs": [999] if problem == "job" else [22],
        "updated_at": time.time() - (181 if problem == "expired" else 0),
    }
    note.write_text(json.dumps({"20": entry}))
    handed.pulls["7"]["statusCheckRollup"][0].update(
        conclusion="FAILURE" if problem == "failure" else "CANCELLED",
        detailsUrl=(
            "https://github.com/example-org/example-app/actions/runs/20/job/"
            + ("23" if problem == "attempt" else "22")
        ),
    )
    run = handed.api[
        f"repos/example-org/example-app/actions/workflows/ci.yml/runs?head_sha={handed.head}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    run.update(conclusion="cancelled", run_attempt=2 if problem == "attempt" else 1)
    for job in handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"][
        "jobs"
    ]:
        job["conclusion"] = "cancelled"
    handed.save()
    done = workflow.run_script("tick")
    assert not _note(workflow).exists(), done.stdout + done.stderr
    assert _started(workflow) == ["review-7"], done.stdout + done.stderr


def _low_budget_after(workflow: Workflow, command: str) -> None:
    original = (workflow.bin / "gh").read_text()
    workflow.fake("gh-before-budget", original)
    workflow.fake(
        "gh",
        """#!/usr/bin/env python3
import os, sys
from pathlib import Path
root = Path("@STATE@")
command = """
        + repr(command)
        + """
if ' '.join(sys.argv[1:]).startswith(command) and not (root / 'reserve-reached').exists():
    (root / 'reserve-reached').touch()
    print('* Request at now', file=sys.stderr)
    print('< HTTP/2.0 200 OK', file=sys.stderr)
    print('< X-Ratelimit-Resource: core', file=sys.stderr)
    print('< X-Ratelimit-Remaining: 499', file=sys.stderr)
    print('< X-Ratelimit-Reset: 2000000000', file=sys.stderr)
    print('* Request took 1ms', file=sys.stderr)
os.execv(str(Path(__file__).with_name('gh-before-budget')), ['gh', *sys.argv[1:]])
""",
    )


@pytest.mark.parametrize("restart", ["reset", "foreground"])
def test_reserve_reached_mid_tick_keeps_the_handoff_and_train_position(
    handed: GitHub, restart: str
) -> None:
    workflow = handed.workflow
    train_path = _folder(workflow) / "merge-train.json"
    waiting = json.loads(train_path.read_text())["waiting"]
    _low_budget_after(workflow, "pr view 7 -R")
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    assert (workflow.state / "reserve-reached").exists()
    assert not (workflow.state / "merged").exists()
    assert _note(workflow).exists()
    assert json.loads(train_path.read_text())["waiting"] == waiting
    assert not (_folder(workflow) / "merge-review-7").exists()
    assert _started(workflow) == []
    calls = workflow.calls("gh")
    again = workflow.run_script("tick")
    assert again.returncode == 0, again.stderr
    assert workflow.calls("gh") == calls
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget/budget.json"
    if restart == "reset":
        budget.write_text(json.dumps({"core": {"remaining": 499, "reset": 0}}))
        finished = workflow.run_script("tick")
    else:
        # The reserved requests remain available to an interactive gate. It still
        # performs the normal current-main, review, head and CI authorization checks.
        finished = workflow.run_script(
            "merge-when-current", "--drive", "7", cwd=workflow.worktree(7)
        )
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert any(
        "--match-head-commit " + handed.head in call for call in workflow.calls("gh pr merge")
    )


def test_low_budget_after_batch_dispatch_keeps_every_handoff_for_retry(github: GitHub) -> None:
    _handed_batch(github)
    workflow = github.workflow
    _low_budget_after(workflow, "workflow run ci.yml")
    before = github.batch_state()["waiting"]
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "reserve-reached").exists()
    assert not (workflow.state / "merged").exists()
    assert len(workflow.calls("gh workflow run")) == 1
    for number in (7, 8, 9):
        assert _note(workflow, number).exists()
        assert not (_folder(workflow) / f"merge-review-{number}").exists()
    assert github.batch_state()["waiting"] == before
    calls = workflow.calls("gh")
    assert workflow.run_script("tick").returncode == 0
    assert workflow.calls("gh") == calls


def test_a_main_retry_prevented_by_the_reserve_remains_eligible_after_reset(handed: GitHub) -> None:
    workflow = handed.workflow
    handed.main(conclusion="failure")
    handed.api[
        f"repos/example-org/example-app/actions/workflows/ci.yml/runs?head_sha={handed.base}&per_page=100"
    ]["workflow_runs"][0]["run_attempt"] = 1
    for job in handed.api["repos/example-org/example-app/actions/runs/10/jobs?per_page=100"][
        "jobs"
    ]:
        job["labels"] = ["example-app-home-main"]
    handed.save()
    _low_budget_after(workflow, "api --paginate repos/example-org/example-app/actions/runs/10/jobs")
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    assert not (_folder(workflow) / "main-rerun-10").exists()
    assert workflow.calls("gh run rerun") == []
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget/budget.json"
    budget.write_text(json.dumps({"core": {"remaining": 499, "reset": 0}}))
    second = workflow.run_script("tick")
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(workflow.calls("gh run rerun 10")) == 1
    assert (_folder(workflow) / "main-rerun-10").exists()


def test_a_handed_batch_whose_push_fails_twice_keeps_every_handoff(github: GitHub) -> None:
    _handed_batch(github)
    workflow = github.workflow
    short_of_memory(workflow, MEMORY_SHORTAGES[1], times=2)

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    assert hook_runs(workflow) == ["EIO_BACKEND=posix"] * 2
    assert all(_note(workflow, number).exists() for number in (7, 8, 9))
    assert not list(_folder(workflow).glob("merge-review-*"))
    assert _started(workflow) == []
    batch = github.batch_state()["prefixes"]["members"][0]["scratch"]
    assert batch["pushed"] is False

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    members = github.batch_state()["prefixes"]["members"]
    assert all(member["scratch"]["pushed"] for member in members)
    github.finish_batch()
    github.save()
    third = workflow.run_script("tick")
    assert third.returncode == 0, third.stdout + third.stderr
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["7", "8", "9"]
    assert _started(workflow) == []


@pytest.mark.parametrize("message", MEMORY_SHORTAGES)
def test_a_stacked_child_push_short_of_memory_is_retried_a_minute_later(
    handed: GitHub, message: str
) -> None:
    workflow = handed.workflow
    worktree, head = _child(handed)
    # Fail only the child's push: the parent's squash is GitHub's, not a push.
    short_of_memory(workflow, message, times=1)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    remote = workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0]
    assert remote != head
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == remote
    assert hook_runs(workflow) == ["EIO_BACKEND=posix"] * 2
    assert len(workflow.calls("sleep 60")) == 1
    assert _started(workflow) == ["review-8"]


def test_a_stacked_child_push_short_of_memory_twice_hands_back_naming_the_error(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _, head = _child(handed)
    short_of_memory(workflow, MEMORY_SHORTAGES[1], times=2)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0] == head
    assert hook_runs(workflow) == ["EIO_BACKEND=posix"] * 2
    assert len(workflow.calls("sleep 60")) == 1
    # The reviewer's start consumes the hand-back; the tick's own line keeps its reason.
    first = next(line for line in done.stdout.splitlines() if line.startswith("merge hand-back 8"))
    assert first.startswith("merge hand-back 8: stacked on 7: could not push stacked branch: ")
    assert MEMORY_SHORTAGES[1] in first and "a minute apart" in first, first
    assert _started(workflow) == ["review-8"]


def test_a_handed_batch_short_of_memory_for_three_ticks_keeps_every_handoff(
    github: GitHub,
) -> None:
    _handed_batch(github)
    workflow = github.workflow
    # Both attempts of three ticks: more than the bound for a hook refusing for another reason.
    short_of_memory(workflow, MEMORY_SHORTAGES[1], times=6)

    for _ in range(3):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert github.batch_state()["prefixes"]["members"][0]["scratch"]["pushed"] is False

    assert all(_note(workflow, number).exists() for number in (7, 8, 9))
    assert not list(_folder(workflow).glob("merge-review-*"))
    assert _started(workflow) == []
    fourth = workflow.run_script("tick")
    assert fourth.returncode == 0, fourth.stdout + fourth.stderr
    members = github.batch_state()["prefixes"]["members"]
    assert all(member["scratch"]["pushed"] for member in members)
    github.finish_batch()
    github.save()
    assert workflow.run_script("tick").returncode == 0
    assert [call.split()[3] for call in workflow.calls("gh pr merge")] == ["7", "8", "9"]
    assert _started(workflow) == []


# A clean merge of main whose only collision is a migration number.
_BASE = 'revision = "0001"\ndown_revision = None\n'
_BRANCH = 'revision = "0002"\ndown_revision = "0001"\n# branch migration\n'
_MAIN = 'revision = "0002"\ndown_revision = "0001"\n# main migration\n'
_MORE = 'revision = "0003"\ndown_revision = "0002"\n# more on main\n'


@pytest.fixture
def apart(github: GitHub) -> GitHub:
    """The pull request in its own worktree, as sessions use it, and main in the checkout."""
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("worktree", "add", str(workflow.worktree(7)), "ticket/seven")
    return github


def _both_add_0002(github: GitHub, kind: str, *, reviewed: bool = False) -> Path:
    """The branch adds 0002 (and more), then main adds its own 0002 in another file.

    Git merges the two cleanly: only the numbers collide, as the migration tool's two heads.
    """
    workflow = github.workflow
    root, branch = workflow.repo, workflow.worktree(7)
    _write(root, _VERSIONS + "0001_base.py", _BASE)
    _commit(workflow, root, "test: the released migration")
    workflow.git("merge", "main", cwd=branch)
    _write(branch, _VERSIONS + "0002_branch.py", _BRANCH)
    nexts = {
        "chain": 'revision = "0003"\ndown_revision = "0002"\n# next migration\n',
        "branch point": 'revision = "0003"\ndown_revision = "0001"\n# next migration\n',
        "merge migration": 'revision = "0003"\ndown_revision = ("0001", "0002")\n',
    }
    _write(branch, _VERSIONS + "0003_next.py", nexts.get(kind, nexts["chain"]))
    if kind in ("edited released", "edited released without collision"):
        _write(branch, _VERSIONS + "0001_base.py", _BASE + "# edited released file\n")
    github.head = _commit(workflow, branch, "feat: two migrations")
    github.pulls["7"]["headRefOid"] = github.head
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {github.head}\n")
    old = workflow.git("rev-parse", "main")
    github.ci(old, github.head)
    if reviewed:
        _publish(github)
        _handoff(github)
    # "stacked": the branch's 0002 is its parent's, which main has now squashed in.
    landed = {
        "edited released without collision": {},
        "main took two": {"0002_main.py": _MAIN, "0003_more.py": _MORE},
        "stacked": {"0002_branch.py": _BRANCH},
        "stacked and taken": {"0002_branch.py": _BRANCH, "0003_more.py": _MORE},
    }.get(kind, {"0002_main.py": _MAIN})
    for name, text in landed.items():
        _write(root, _VERSIONS + name, text)
    if landed:
        github.base = _commit(workflow, root, "feat: main's migrations")
    else:
        workflow.git("commit", "--allow-empty", "-m", "fix: main moved", cwd=root)
        workflow.git("push", "origin", "main", cwd=root)
        github.base = workflow.git("rev-parse", "HEAD", cwd=root)
    github.main()
    github.ci(github.base, github.head, tested_base=old)
    github.save()
    return branch


def _chain(branch: Path) -> list[tuple[str, str, str]]:
    """Each file with its revision and parent, as the migration tests read them."""
    found: list[tuple[str, str, str]] = []
    for path in sorted((branch / _VERSIONS).glob("[0-9]*.py")):
        text = path.read_text()
        revision = re.search(r'^revision = "(.*)"$', text, re.MULTILINE)
        parent = re.search(r"^down_revision = (.*)$", text, re.MULTILINE)
        assert revision and parent, path
        found.append((path.name, revision[1], parent[1].strip('"')))
    return found


def test_a_branch_whose_numbers_main_did_not_take_is_refreshed_as_it_is(apart: GitHub) -> None:
    workflow = apart.workflow
    branch = _both_add_0002(apart, "edited released without collision")

    done = workflow.run_script("merge-when-current", "7", cwd=branch)

    assert done.returncode == 2, done.stdout + done.stderr
    subject = workflow.git("log", "-1", "--format=%s", cwd=branch)
    assert subject == "chore: test against current main"
    assert [name for name, _, _ in _chain(branch)] == [
        "0001_base.py",
        "0002_branch.py",
        "0003_next.py",
    ]
    assert not workflow.calls("pytest")


def _stale_note(handed: GitHub) -> None:
    workflow = handed.workflow
    old = handed.base
    workflow.git("commit", "--allow-empty", "-m", "fix: main advanced")
    handed.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    handed.main()
    handed.ci(handed.base, handed.head, tested_base=old)
    handed.save()


def _blocked_on_its_head(handed: GitHub, *, refreshable: bool) -> None:
    # The line as a green re-run on an older main left it: blocked under the same head.
    path = _folder(handed.workflow) / "merge-train.json"
    train = json.loads(path.read_text())
    train["blocked"]["7"] = handed.head
    train["refreshable"] = ["7"] if refreshable else []
    path.write_text(json.dumps(train))


def test_a_refreshable_handoff_is_kept_and_refreshed_at_its_turn(handed: GitHub) -> None:
    workflow = handed.workflow
    _stale_note(handed)
    _blocked_on_its_head(handed, refreshable=True)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge hand-back 7" not in done.stdout
    assert not (_folder(workflow) / "merge-review-7").exists()
    assert _started(workflow) == []
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != handed.head
    assert workflow.git("rev-parse", "HEAD^2", cwd=workflow.worktree(7)) == handed.base
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    assert not (workflow.state / "merged").exists()


def test_a_refreshable_handoff_survives_a_dropped_read_of_its_runs(handed: GitHub) -> None:
    workflow = handed.workflow
    _stale_note(handed)
    _blocked_on_its_head(handed, refreshable=True)
    # The connection drops while the line reads this pull request's CI runs.
    (workflow.bin / "gh").rename(workflow.bin / "gh-stand-in")
    workflow.fake(
        "gh",
        """#!/bin/bash
if [[ "$1" == api && "$*" == *"ci.yml/runs?"*"event=pull_request"* &&
      -e "@STATE@/github-down" ]]; then
  echo 'Get "https://api.github.com/repos": read tcp: connection reset by peer' >&2
  exit 1
fi
exec "$(dirname "$0")/gh-stand-in" "$@"
""",
    )
    (workflow.state / "github-down").touch()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge hand-back 7" not in done.stdout
    train = json.loads((_folder(workflow) / "merge-train.json").read_text())
    assert train["refreshable"] == ["7"], train
    (workflow.state / "github-down").unlink()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge hand-back 7" not in done.stdout
    assert _started(workflow) == []
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert workflow.git("rev-parse", "HEAD^2", cwd=workflow.worktree(7)) == handed.base
    assert json.loads(_note(workflow).read_text())["head"] == refreshed


def test_a_blocked_handoff_that_is_not_refreshable_is_handed_back(handed: GitHub) -> None:
    workflow = handed.workflow
    _stale_note(handed)
    _blocked_on_its_head(handed, refreshable=False)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge hand-back 7: pull request checks failed" in done.stdout
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == handed.head


@pytest.mark.parametrize("refreshable", [False, True], ids=["failed", "failed and refreshable"])
def test_a_handoff_with_a_failed_check_is_handed_back(handed: GitHub, refreshable: bool) -> None:
    workflow = handed.workflow
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    handed.save()
    if refreshable:
        # A failing check wins over a refreshable mark the line saved before it failed.
        _blocked_on_its_head(handed, refreshable=True)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge hand-back 7: pull request checks failed" in done.stdout
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]
    assert not workflow.calls("gh pr merge")
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head


def test_a_refresh_refused_for_an_advisory_hands_back_to_one_fresh_reviewer(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _stale_note(handed)
    short_of_memory(workflow, ADVISORY, times=1)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]
    assert not (_folder(workflow) / "advisory-service-down").exists()


def test_tick_whole_refresh_is_accepted_without_scratch_suites(handed: GitHub) -> None:
    workflow = handed.workflow
    _stale_note(handed)
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != handed.head
    handed.head = refreshed
    handed.pulls["7"]["headRefOid"] = refreshed
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {refreshed}\n")
    handed.ci(handed.base, refreshed, run_id=50, server="whole")
    handed.api["repos/example-org/example-app/actions/runs/50/jobs?per_page=100"]["jobs"].append(
        {"id": 53, "name": "CI: server tests", "status": "completed", "conclusion": "success"}
    )
    handed.save()
    second = workflow.run_script("tick")
    assert second.returncode == 0, second.stdout + second.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert not workflow.calls("gh workflow run")


_RUNS = (
    "repos/example-org/example-app/actions/workflows/ci.yml/runs"
    "?head_sha={head}&event=pull_request&per_page=100"
)


def _github_answers_repairs(workflow: Workflow) -> None:
    """`gh pr ready N --undo` makes N a draft; `gh run view R --job J --log-failed` prints the
    failed log a test left in `job-logs/J`, and fails as GitHub does when there is none."""
    (workflow.bin / "gh").rename(workflow.bin / "gh-stand-in")
    workflow.fake(
        "gh",
        """#!/bin/bash
if [[ "$1 $2" == "pr ready" && " $* " == *" --undo "* ]]; then
  echo "gh $*" >> "@STATE@/calls.log"
  jq --arg n "$3" '.pulls[$n].isDraft = true' "@STATE@/github.json" > "@STATE@/github.json.new"
  mv "@STATE@/github.json.new" "@STATE@/github.json"
  read -r _ labels < "@STATE@/listing/$3"
  echo "true $labels" > "@STATE@/listing/$3"
  echo true > "@STATE@/draft/$3"
  exit 0
fi
if [[ "$1 $2" == "run view" && " $* " == *" --log-failed "* ]]; then
  echo "gh $*" >> "@STATE@/calls.log"
  while [[ $# -gt 0 && $1 != --job ]]; do shift; done
  [[ -f "@STATE@/job-logs/$2" ]] || { echo "failed to get run log: log not found" >&2; exit 1; }
  cat "@STATE@/job-logs/$2"
  exit 0
fi
exec "$(dirname "$0")/gh-stand-in" "$@"
""",
    )
    (workflow.state / "job-logs").mkdir(exist_ok=True)


_FAILED_TEST = (
    "2026-10-24T10:00:00.0000000Z \x1b[31mFAILED\x1b[0m tests/test_dates.py::test_parses_a_leap_day"
    " - AssertionError: assert 28 == 29\n"
)


def _fail(
    github: GitHub,
    *,
    log: str | None = _FAILED_TEST,
    conclusion: str = "failure",
    labels: tuple[str, ...] = ("example-app-home",),
    run_id: int = 20,
) -> None:
    """The pull request's CI run on its head failed in its "CI: server tests" job."""
    workflow = github.workflow
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    run = github.api[_RUNS.format(head=github.head)]["workflow_runs"][0]
    run.update(conclusion="failure", run_attempt=1)
    jobs = github.api[f"repos/example-org/example-app/actions/runs/{run_id}/jobs?per_page=100"]
    for job in jobs["jobs"]:
        job["labels"] = ["home-planner"]
        if job["name"] == "CI result":
            job["conclusion"] = "failure"
    job_id = run_id + 3
    jobs["jobs"].append(
        {
            "id": job_id,
            "name": "CI: server tests",
            "status": "completed",
            "conclusion": conclusion,
            "labels": list(labels),
        }
    )
    github.api[f"repos/example-org/example-app/check-runs/{job_id}/annotations?per_page=100"] = []
    if log is not None:
        (workflow.state / "job-logs" / str(job_id)).write_text(log)
    github.save()


def _repair_sentence(
    run: int = 20, test: str = "tests/test_dates.py::test_parses_a_leap_day"
) -> str:
    return instruction(
        f"Fix the failed check of pull request 7: job CI: server tests of run {run}, "
        f"first failing test {test}"
    )


def _fix_and_hand_off(github: GitHub, run_id: int = 50) -> None:
    """The worker pushes a fix and marks ready; a fresh reviewer passes it and hands it off."""
    workflow = github.workflow
    root = workflow.worktree(7)
    workflow.git("commit", "--allow-empty", "-m", "fix: parse a leap day", cwd=root)
    github.head = workflow.git("rev-parse", "HEAD", cwd=root)
    workflow.git("push", "origin", "HEAD:ticket/seven", cwd=root)
    github.pulls["7"].update(isDraft=False, headRefOid=github.head)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {github.head}\n")
    workflow.list_pull_request(7, draft=False)
    github.ci(github.base, github.head, run_id=run_id)
    reviews = github.api["repos/example-org/example-app/pulls/7/reviews"]
    reviews.append(
        {"body": "<!-- workflow-review round=2 result=passed -->\nFixed.", "commit_id": github.head}
    )
    github.save()
    _handoff(github)


def _behind(github: GitHub) -> None:
    """Pull request 8 joined the line after 7; it has no session or handoff, so it only waits."""
    workflow = github.workflow
    workflow.git("branch", "ticket/eight", github.base)
    workflow.git("push", "origin", "ticket/eight")
    head = workflow.git("rev-parse", "ticket/eight")
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefOid": head,
        "headRefName": "ticket/eight",
        "labels": [],
    }
    github.ci(github.base, head, run_id=30)
    workflow.list_pull_request(8, draft=False, labels=())
    (workflow.state / "pr" / "8").write_text(f"OPEN ticket/eight {head}\n")
    github.save()
    path = _folder(workflow) / "merge-train.json"
    train = json.loads(path.read_text())
    train["waiting"] = ["7", "8"]
    path.write_text(json.dumps(train))


def _train(workflow: Workflow) -> dict:
    return json.loads((_folder(workflow) / "merge-train.json").read_text())


def test_a_failing_test_goes_to_a_medium_worker_naming_the_run_and_keeps_the_line_place(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _behind(handed)
    failed = handed.head
    _fail(handed)

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    assert workflow.calls("gh pr ready") == ["gh pr ready 7 -R example-org/example-app --undo"]
    assert not _note(workflow).exists()
    assert _started(workflow) == []
    assert "7" not in _train(workflow)["approved"]
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert _train(workflow)["blocked"]["7"] == failed

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert _started(workflow) == ["ticket-7"]
    assert session_arguments(workflow.state, "ticket-7")[-1] == (
        f'claude --effort medium --remote-control ticket-7 "{_repair_sentence()}"'
    )
    assert _train(workflow)["waiting"] == ["7", "8"]
    workflow.run("stop", "ticket-7")
    _fix_and_hand_off(handed)
    workflow.run("stop", "review-7")

    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["blocked"]
    # While the fix's CI runs, the handoff waits in its earlier place.
    jobs = handed.api["repos/example-org/example-app/actions/runs/50/jobs?per_page=100"]["jobs"]
    jobs[-1].update(status="in_progress", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    handed.save()

    third = workflow.run_script("tick")

    assert third.returncode == 0, third.stdout + third.stderr
    assert _note(workflow).exists()
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["blocked"]
    jobs[-1].update(status="completed", conclusion="success")
    handed.pulls["7"]["statusCheckRollup"][0].update(status="COMPLETED", conclusion="SUCCESS")
    handed.save()

    third = workflow.run_script("tick")

    assert third.returncode == 0, third.stdout + third.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert _started(workflow) == ["ticket-7"]
    assert not (_folder(workflow) / "check-repair-7.json").exists()


@pytest.mark.parametrize("rerun", ["fails again", "passes"])
@pytest.mark.parametrize("failure", ["runner shut down", "timed out"])
def test_a_runner_error_is_rerun_once_then_handed_back_to_a_reviewer(
    handed: GitHub, failure: str, rerun: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    if failure == "timed out":
        _fail(handed, conclusion="timed_out", log=None)
    else:
        _fail(handed, log="##[error]The runner has received a shutdown signal.\n")

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        # Re-run once; until GitHub shows the new attempt, the request is not sent again.
        assert workflow.calls("gh run rerun") == [
            "gh run rerun 20 -R example-org/example-app --failed"
        ]
        assert _note(workflow).exists()
        assert _started(workflow) == []

    # GitHub shows the new attempt running: the handoff waits for it.
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    run.update(run_attempt=2, status="in_progress", conclusion=None)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job.update(status="in_progress", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists()
    assert _started(workflow) == []
    passed = rerun == "passes"
    run.update(status="completed", conclusion="success" if passed else "failure")
    for job in jobs:
        job.update(status="completed", conclusion="success")
    if not passed:
        jobs[-1]["conclusion"] = "failure" if failure == "runner shut down" else "timed_out"
        jobs[1]["conclusion"] = "failure"
    handed.pulls["7"]["statusCheckRollup"][0].update(
        status="COMPLETED", conclusion="SUCCESS" if passed else "FAILURE"
    )
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh run rerun")) == 1
    assert workflow.calls("gh pr ready") == []
    if passed:
        assert (workflow.state / "merged").read_text() == "7"
        assert _started(workflow) == []
    else:
        assert not _note(workflow).exists()
        assert _started(workflow) == ["review-7"]


def test_a_runner_error_on_runners_the_gate_may_not_rerun_on_is_handed_back(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(
        handed,
        labels=("ubuntu-latest",),
        log="The hosted runner lost communication with the server.\n",
    )

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == []
    assert _started(workflow) == ["review-7"]
    assert workflow.calls("gh pr ready") == []


def _handbacks(workflow: Workflow) -> list[str]:
    pending = _folder(workflow) / "merge-handbacks.pending"
    return [json.loads(path.read_text())["reason"] for path in sorted(pending.iterdir())]


@pytest.mark.parametrize("between", ["no tick", "a tick that saw only skipped checks"])
def test_a_second_failing_test_in_a_row_goes_to_a_reviewer_not_another_worker(
    handed: GitHub, between: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    _fix_and_hand_off(handed)
    workflow.run("stop", "review-7")
    if between != "no tick":
        # The fix's checks have not started: only a skipped check from its draft shows.
        handed.pulls["7"]["statusCheckRollup"] = [
            {"name": "Docker images: what to run", "status": "COMPLETED", "conclusion": "SKIPPED"}
        ]
        run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
        run.update(status="queued", conclusion=None)
        handed.save()
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _note(workflow).exists()
        handed.pulls["7"]["statusCheckRollup"] = [
            {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ]
        run.update(status="completed", conclusion="success")
        handed.save()
    _fail(handed, run_id=50)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh pr ready")) == 1
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]
    assert not (_folder(workflow) / "check-repair-7.json").exists()
    assert (
        "pull request checks failed again after a worker's fix: job CI: server tests of run 50, "
        "first failing test tests/test_dates.py::test_parses_a_leap_day"
    ) in _handbacks(workflow)


def test_a_failing_test_beside_a_cancelled_job_still_goes_to_a_worker(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    # Fail-fast cancelled another part when the test failed.
    jobs.insert(
        0,
        {"id": 19, "name": "CI: web", "status": "completed", "conclusion": "cancelled"},
    )
    handed.api["repos/example-org/example-app/check-runs/19/annotations?per_page=100"] = []
    (workflow.state / "job-logs/19").write_text("The operation was canceled.\n")
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh pr ready") == ["gh pr ready 7 -R example-org/example-app --undo"]
    assert _started(workflow) == []
    assert _handbacks(workflow) == [
        "pull request checks failed on its own test: a worker fixes job CI: server tests of "
        "run 20, first failing test tests/test_dates.py::test_parses_a_leap_day"
    ]


def test_a_failed_job_with_nothing_to_read_goes_to_a_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=None)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh pr ready") == []
    assert _started(workflow) == ["review-7"]


@pytest.mark.parametrize(
    ("log", "test"),
    [
        (
            "\x1b[31mFAIL\x1b[39m src/dates.test.ts > parses a leap day\n",
            "src/dates.test.ts > parses a leap day",
        ),
        (
            "  1) [chromium] › e2e/plan.spec.ts:12:5 › opens the plan\n",
            "[chromium] > e2e/plan.spec.ts:12:5 > opens the plan",
        ),
        (
            'FAILED tests/test_quote.py::test_reads["$(id)",`id`\\] - assert\n',
            "tests/test_quote.py::test_reads[ (id) id ]",
        ),
        ("Error: Process completed with exit code 1.\n", "not reported"),
        (
            "FAILED tests/test_dates.py::test_totals[two items] - assert 3 == 4\n",
            "tests/test_dates.py::test_totals[two items]",
        ),
        (
            # The server under test answered 500: no runner of GitHub's failed it.
            '"GET /health HTTP/1.1" 500 Internal Server Error\n',
            "not reported",
        ),
    ],
)
def test_the_worker_sentence_names_the_first_failing_test_in_safe_words(
    handed: GitHub, log: str, test: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=log)

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr

    assert session_arguments(workflow.state, "ticket-7")[-1] == (
        f'claude --effort medium --remote-control ticket-7 "{_repair_sentence(test=test)}"'
    )


@pytest.mark.parametrize("failure", ["runner lost", "timed out"])
def test_a_runner_error_beside_a_part_fail_fast_cancelled_is_rerun_once(
    handed: GitHub, failure: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    if failure == "timed out":
        _fail(handed, conclusion="timed_out", log=None)
    else:
        _fail(handed, log="The self-hosted runner: home-1 lost communication with the server.\n")
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    # Fail-fast cancelled the other part of the matrix when the runner failed.
    jobs.insert(
        0,
        {
            "id": 19,
            "name": "CI: server tests (2 of 2)",
            "status": "completed",
            "conclusion": "cancelled",
            "labels": ["example-app-home"],
        },
    )
    handed.api["repos/example-org/example-app/check-runs/19/annotations?per_page=100"] = []
    (workflow.state / "job-logs/19").write_text("##[error]The operation was canceled.\n")
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == ["gh run rerun 20 -R example-org/example-app --failed"]
    assert workflow.calls("gh pr ready") == []
    assert _started(workflow) == []
    assert _note(workflow).exists()


def test_a_failing_test_without_the_label_keeps_its_audit_path(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    workflow.list_pull_request(7, draft=False, labels=())
    handed.pulls["7"]["labels"] = []
    _fail(handed)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh pr ready") == []
    assert not _note(workflow).exists()
    assert not (_folder(workflow) / "check-repair-7.json").exists()
    assert (_folder(workflow) / "merge-review-7").exists()
    assert _started(workflow) == []
