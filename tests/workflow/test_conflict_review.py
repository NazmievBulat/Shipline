"""A conflict round reuses only proved review evidence, through the real workflow scripts."""

import json
import subprocess
from pathlib import Path

import pytest
from workflow.test_independent_review import (  # pyright: ignore[reportPrivateUsage]
    _idle,
    _rule_breaks,
)
from workflow.test_merge_driver import _folder, _publish  # pyright: ignore[reportPrivateUsage]
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow.test_review_panel import _panel  # pyright: ignore[reportPrivateUsage]
from workflow_harness import SESSION_LINK, Workflow, instruction, session_arguments

handed = handed_fixture
github = github_fixture

_FILE = "src/example_app/conflict.py"
_PREFIX = "router = APIRouter(prefix='/api')\n" + "".join(
    f"# context {line}\n" for line in range(12)
)
_SUFFIX = "".join(f"# following {line}\n" for line in range(12))


def _write(root: Path, path: str, content: str) -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)


def _commit(workflow: Workflow, root: Path) -> str:
    workflow.git("add", ".", cwd=root)
    workflow.git("commit", "--quiet", "-m", "test: conflict", cwd=root)
    workflow.git("push", "--quiet", "origin", "HEAD", cwd=root)
    return workflow.git("rev-parse", "HEAD", cwd=root)


def _conflict(workflow: Workflow) -> tuple[str, str]:
    branch = workflow.worktree(7)
    _write(
        branch,
        _FILE,
        _PREFIX + "result = 'branch'\n@router.get('/branch')\ndef branch(): pass\n" + _SUFFIX,
    )
    _write(branch, "branch-only.py", "result = 'passed with the branch'\n")
    head = _commit(workflow, branch)
    _write(
        workflow.repo,
        _FILE,
        _PREFIX + "result = 'main'\n@router.get('/main')\ndef main(): pass\n" + _SUFFIX,
    )
    # This clean main addition must survive the merge without expanding review scope.
    _write(workflow.repo, "main-only.py", "result = 'already reviewed on main'\n")
    main = _commit(workflow, workflow.repo)
    return head, main


def _begin_merge(workflow: Workflow, main: str) -> None:
    done = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", main],
        cwd=workflow.worktree(7),
        env=workflow.env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 1, done.stdout + done.stderr


def _resolve(workflow: Workflow, extra: str = "") -> str:
    branch = workflow.worktree(7)
    _write(branch, _FILE, _PREFIX + "result = 'main and branch'\n" + _SUFFIX)
    if extra == "file":
        _write(branch, "unrelated.py", "unreviewed = True\n")
    elif extra == "long":
        # Seven lines where the conflict was: both sides and a line of their own.
        both = "".join(f"result_{line} = 'main and branch'\n" for line in range(7))
        _write(branch, _FILE, _PREFIX + both + _SUFFIX)
    elif extra == "hunk":
        _write(branch, _FILE, "# unreviewed\n" + (branch / _FILE).read_text())
    if extra == "crlf":
        file = branch / _FILE
        file.write_bytes(file.read_bytes().replace(b"\n", b"\r\n"))
    workflow.git("add", ".", cwd=branch)
    workflow.git("commit", "--quiet", "-m", f"merge main: conflict in {_FILE}", cwd=branch)
    workflow.git("push", "--quiet", "origin", "HEAD", cwd=branch)
    return workflow.git("rev-parse", "HEAD", cwd=branch)


@pytest.mark.parametrize("review", ["passed", "missing", "sent-back", "wrong head"])
def test_tick_starts_conflict_sentence_only_with_passed_handed_off_head(
    handed: GitHub, review: str
) -> None:
    workflow = handed.workflow
    head, main = _conflict(workflow)
    handed.head, handed.base = head, main
    handed.pulls["7"]["headRefOid"] = head
    handed.pulls["7"]["mergeable"] = "CONFLICTING"
    handed.main()
    handed.ci(main, head, tested_base=handed.workflow.git("merge-base", main, head))
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    note = _folder(workflow) / "merge-wanted-7"
    data = json.loads(note.read_text())
    data["head"] = head
    note.write_text(json.dumps(data))
    _publish(
        handed,
        "sent-back" if review == "sent-back" else "passed",
        main if review == "wrong head" else head,
    )
    if review == "missing":
        handed.api["repos/example-org/example-app/pulls/7/reviews"] = []
        handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    args = " ".join(session_arguments(workflow.state, "review-7"))
    sentence = (
        f"Review conflict resolution for pull request 7 extending passed head {head} "
        f"in files {_FILE}"
        if review == "passed"
        else "Review pull request 7"
    )
    assert instruction(sentence) in args
    # A conflict resolution's reviewer stays high; this change names no path that needs it.
    effort = "high" if review == "passed" else "medium"
    assert f"--effort {effort} --model opus" in args
    assert workflow.git("rev-parse", "MERGE_HEAD", cwd=workflow.worktree(7)) == main
    scope = _folder(workflow) / "review-7.conflict.json"
    assert scope.exists() == (review == "passed")


@pytest.mark.parametrize("state", ["unresolved", "resolved", "moved on", "no worktree"])
def test_a_scope_names_the_conflict_only_while_the_worktree_holds_its_merge(
    workflow: Workflow, state: str
) -> None:
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )
    if state != "unresolved":
        _resolve(workflow)
    if state == "moved on":
        # A sent-back round's worker pushed a fix; its fresh reviewer reviews the whole change.
        workflow.git("commit", "--allow-empty", "-m", "fix: send-back", cwd=workflow.worktree(7))
    elif state == "no worktree":
        workflow.git("worktree", "remove", "--force", str(workflow.worktree(7)))

    done = subprocess.run(
        [
            "python3",
            "-B",
            str(workflow.script.parent / "conflict_review.py"),
            "sentence",
            str(folder),
            "7",
        ],
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == (
        f"Review conflict resolution for pull request 7 extending passed head {head} "
        f"in files {_FILE}\n"
        if state in {"unresolved", "resolved"}
        else "Review pull request 7\n"
    )
    if state == "no worktree":
        # The tick prepares a hand-back's scope before its start; a missing worktree must
        # leave the ordinary review to start instead of failing the start on every tick.
        reviews = [{"body": "<!-- workflow-review round=1 result=passed -->", "commit_id": head}]
        request = {"reason": f"non-mechanical conflict in {_FILE}", "head": head}
        prepared = subprocess.run(
            [
                "python3",
                "-B",
                "-c",
                "import json, sys; from pathlib import Path; from conflict_review import prepare; "
                "prepare(Path(sys.argv[1]), '7', Path(sys.argv[2]), json.loads(sys.argv[3]), "
                "sys.argv[4], json.loads(sys.argv[5]))",
                str(folder),
                str(workflow.worktree(7)),
                json.dumps(request),
                head,
                json.dumps(reviews),
            ],
            cwd=workflow.script.parent,
            env=workflow.env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        assert not (folder / "review-7.conflict.json").exists()


def _panel_conflict(workflow: Workflow) -> tuple[str, str]:
    _panel(workflow)
    head, main = _conflict(workflow)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps(
            {
                "head": head,
                "main": main,
                "files": [_FILE],
            }
        )
    )
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [
                {
                    "body": (
                        "<!-- workflow-review round=1 result=passed -->\n"
                        "Earlier independent evidence."
                    ),
                    "commit_id": head,
                }
            ]
        )
    )
    _write(
        workflow.repo,
        ".github/path-filters.yml",
        'python:\n  - "**/*.py"\nci:\n  - ".github/workflows/ci.yml"\n',
    )
    for file, content in {
        "tests/test_conflict.py": "from example_app.conflict import result\n",
        "tests/test_unrelated.py": "def test_unrelated(): pass\n",
        "src/example_app/api/main.py": "from example_app.api import conflict_routes\n",
        "src/example_app/api/conflict_routes.py": (
            "from example_app.conflict import result\n"
            "router = APIRouter(prefix='/api')\n"
            "@router.get('/conflict')\ndef get(): return result\n"
        ),
        "tests/pages/conflict.page.ts": 'page.goto("/conflict"); send("/api/conflict")',
        "tests/browser/conflict.spec.ts": 'import "../pages/conflict.page"',
        "tests/browser/unrelated.spec.ts": 'page.goto("/unrelated")',
        "tests/browser/branch.spec.ts": 'send("/api/branch")',
        "tests/browser/main.spec.ts": 'send("/api/main")',
    }.items():
        _write(workflow.repo, file, content)
    main = _commit(workflow, workflow.repo)
    (folder / "review-7.conflict.json").write_text(
        json.dumps(
            {
                "head": head,
                "main": main,
                "files": [_FILE],
            }
        )
    )
    _begin_merge(workflow, main)
    return head, main


@pytest.mark.parametrize("extra", ["file", "hunk", "commit", "revoked", "corrupt"])
def test_panel_falls_back_to_a_full_review(workflow: Workflow, extra: str) -> None:
    _panel_conflict(workflow)
    resolved = _resolve(workflow, extra)
    if extra == "commit":
        workflow.git(
            "commit", "--allow-empty", "-m", "fix: additional work", cwd=workflow.worktree(7)
        )
        resolved = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
        workflow.git("push", "origin", "HEAD", cwd=workflow.worktree(7))
    workflow.set_pull_request(7, "OPEN", resolved)
    if extra == "revoked":
        (workflow.state / "reviews-7.json").write_text("[]")
    elif extra == "corrupt":
        (_folder(workflow) / "review-7.conflict.json").write_text("unreadable")

    done = workflow.run_script("review-panel", "7")

    assert done.returncode == 0, done.stdout + done.stderr
    report = (_folder(workflow) / "reviews/7" / resolved / "codex.md").read_text()
    assert "Full review required:" in report
    assert "# Resolution against parent" not in report


@pytest.mark.parametrize(
    "extra", ["", "file", "hunk", "commit", "revoked", "scope", "extends", "marker", "crlf"]
)
def test_gate_handoff_checks_conflict_round_scope(handed: GitHub, extra: str) -> None:
    workflow = handed.workflow
    head, main = _conflict(workflow)
    _begin_merge(workflow, main)
    scope = _folder(workflow) / "review-7.conflict.json"
    scope.write_text(json.dumps({"head": head, "main": main, "files": [_FILE]}))
    resolved = _resolve(workflow, extra)
    if extra == "commit":
        workflow.git("commit", "--allow-empty", "-m", "fix: extra", cwd=workflow.worktree(7))
        resolved = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
        workflow.git("push", "origin", "HEAD", cwd=workflow.worktree(7))
    handed.pulls["7"]["headRefOid"] = resolved
    handed.api["repos/example-org/example-app/pulls/7/reviews"] = [
        {"body": "<!-- workflow-review round=1 result=passed -->", "commit_id": head},
        {
            "body": (
                "<!-- workflow-review round=2 result=passed -->\n"
                f"<!-- workflow-conflict extends={head} -->"
            ),
            "commit_id": resolved,
        },
    ]
    if extra == "revoked":
        handed.api["repos/example-org/example-app/pulls/7/reviews"][0]["body"] = (
            "<!-- workflow-review round=1 result=sent-back -->"
        )
    elif extra == "scope":
        scope.unlink()
    elif extra == "extends":
        handed.api["repos/example-org/example-app/pulls/7/reviews"][1]["body"] = (
            "<!-- workflow-review round=2 result=passed -->\n"
            f"<!-- workflow-conflict extends={main} -->"
        )
    elif extra == "marker":
        handed.api["repos/example-org/example-app/pulls/7/reviews"][1]["body"] = (
            "<!-- workflow-review round=2 result=passed -->\n"
            "<!-- workflow-conflict extends=unreadable -->"
        )
    handed.save()
    (_folder(workflow) / "merge-wanted-7").unlink()

    done = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))

    assert done.returncode == (1 if extra else 0), done.stdout + done.stderr
    assert (_folder(workflow) / "merge-wanted-7").exists() == (not extra)
    if extra:
        assert "conflict round needs full review" in done.stdout + done.stderr
    else:
        # Retrying a successful handoff must not lose the conflict evidence.
        again = workflow.run_script(
            "merge-when-current", "--handoff", "7", cwd=workflow.worktree(7)
        )
        assert again.returncode == 0, again.stdout + again.stderr


def _conflict_review(workflow: Workflow, max_changed_lines: int | None) -> None:
    """`shipline.json` names a cheaper conflict reviewer, or none."""
    config = workflow.repo / "shipline.json"
    named = json.loads(config.read_text())
    named.pop("conflict_review", None)
    if max_changed_lines is not None:
        named["conflict_review"] = {
            "effort": "medium",
            "model": "sonnet",
            "max_changed_lines": max_changed_lines,
        }
    config.write_text(json.dumps(named))


@pytest.mark.parametrize(
    ("max_changed_lines", "reviewer"),
    [(None, "--effort high --model opus"), (6, "--effort medium --model sonnet"),
     (5, "--effort high --model opus")],  # fmt: skip
    ids=["usual", "within", "over"],
)
def test_the_tick_starts_the_named_reviewer_for_a_small_conflict(
    handed: GitHub, max_changed_lines: int | None, reviewer: str
) -> None:
    """Both sides of the conflicting hunk are six lines: the resolution decides that many."""
    workflow = handed.workflow
    head, main = _conflict(workflow)
    _conflict_review(workflow, max_changed_lines)
    handed.head, handed.base = head, main
    handed.pulls["7"]["headRefOid"] = head
    handed.pulls["7"]["mergeable"] = "CONFLICTING"
    handed.main()
    handed.ci(main, head, tested_base=handed.workflow.git("merge-base", main, head))
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    note = _folder(workflow) / "merge-wanted-7"
    note.write_text(json.dumps({**json.loads(note.read_text()), "head": head}))
    _publish(handed, "passed", head)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    args = " ".join(session_arguments(workflow.state, "review-7"))
    assert instruction(
        f"Review conflict resolution for pull request 7 extending passed head {head} "
        f"in files {_FILE}"
    ) in args
    assert reviewer in args
    record = _folder(workflow) / "review-7.conflict-reviewer"
    assert record.exists() == (reviewer.endswith("sonnet"))


def _reviewer(workflow: Workflow) -> str:
    done = subprocess.run(
        [
            "python3",
            "-B",
            str(workflow.script.parent / "conflict_review.py"),
            "reviewer",
            str(_folder(workflow)),
            "7",
        ],
        # `session` runs it from the repository's checkout, whose shipline.json it reads.
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


@pytest.mark.parametrize(
    ("state", "reviewer"),
    [
        ("unresolved", "medium sonnet\n"),
        ("resolved", "medium sonnet\n"),
        ("long", "high opus\n"),
        ("file", "high opus\n"),
        ("hunk", "high opus\n"),
        ("moved on", "high opus\n"),
    ],
)
def test_a_resolution_over_its_lines_or_in_another_file_gets_the_usual_reviewer(
    workflow: Workflow, state: str, reviewer: str
) -> None:
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    _conflict_review(workflow, 6)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )
    if state != "unresolved":
        _resolve(workflow, "" if state in {"resolved", "moved on"} else state)
    if state == "moved on":
        workflow.git("commit", "--allow-empty", "-m", "fix: send-back", cwd=workflow.worktree(7))

    assert _reviewer(workflow) == reviewer
    assert (folder / "review-7.conflict-reviewer").exists() == (reviewer == "medium sonnet\n")


def test_without_a_named_conflict_reviewer_every_conflict_gets_the_usual_one(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )

    assert _reviewer(workflow) == "high opus\n"
    assert not (folder / "review-7.conflict-reviewer").exists()


@pytest.mark.parametrize(
    ("resolution", "named", "refusal"),
    [
        ("", True, None),
        (
            "long",
            True,
            f"the conflict and its resolution take 7 lines in {_FILE}, more than the 6 of "
            "conflict_review.max_changed_lines",
        ),
        ("file", True, "the resolution changes unrelated.py too"),
        ("hunk", True, f"{_FILE}: resolution changes lines outside conflicting hunks"),
        ("full", True, "the reviewer of a small conflict passes no full review"),
        ("long", False, None),
    ],
    ids=["within", "over", "file", "hunk", "full review", "over by the usual reviewer"],
)
def test_the_gate_hands_a_larger_resolution_to_the_usual_reviewer(
    handed: GitHub, resolution: str, named: bool, refusal: str | None
) -> None:
    workflow = handed.workflow
    head, main = _conflict(workflow)
    _conflict_review(workflow, 6)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )
    if named:
        # The named reviewer was started for the six lines of the conflicting hunk.
        assert _reviewer(workflow) == "medium sonnet\n"
    resolved = _resolve(workflow, "" if resolution == "full" else resolution)
    handed.pulls["7"]["headRefOid"] = resolved
    # A full-review fallback publishes its round without the conflict marker.
    marker = "" if resolution == "full" else f"\n<!-- workflow-conflict extends={head} -->"
    handed.api["repos/example-org/example-app/pulls/7/reviews"] = [
        {"body": "<!-- workflow-review round=1 result=passed -->", "commit_id": head},
        {"body": f"<!-- workflow-review round=2 result=passed -->{marker}", "commit_id": resolved},
    ]
    handed.save()
    (folder / "merge-wanted-7").unlink()

    done = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))

    assert done.returncode == (1 if refusal else 0), done.stdout + done.stderr
    assert (folder / "merge-wanted-7").exists() == (refusal is None)
    assert (folder / "review-7.usual-reviewer").exists() == (refusal is not None)
    if refusal:
        assert (
            f"this round needs the usual reviewer: {refusal}; end with review finished, and the "
            "tick starts the usual reviewer"
        ) in done.stdout + done.stderr


def test_a_large_conflict_resolved_in_a_few_lines_is_still_the_usual_reviewer_s(
    workflow: Workflow,
) -> None:
    """Six lines of sides, one written: a restart after the resolution measures six."""
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    _conflict_review(workflow, 5)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )
    _resolve(workflow)

    assert _reviewer(workflow) == "high opus\n"


def test_a_reviewer_the_gate_sent_to_the_usual_one_is_stopped_without_a_rule_break(
    workflow: Workflow,
) -> None:
    """Its pushed resolution is small enough for it: the gate's word alone keeps it away."""
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    _conflict_review(workflow, 6)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )
    assert _reviewer(workflow) == "medium sonnet\n"
    resolved = _resolve(workflow)
    workflow.set_pull_request(7, "OPEN", resolved)
    # The gate refused its full-review round on the resolved head.
    (folder / "review-7.usual-reviewer").write_text(resolved + "\n")
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen(
        f"Remote Control: {SESSION_LINK}\n● Handed back.\n\n  review finished\n\n> \n", "review-7"
    )

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    assert _rule_breaks(workflow) == []
    (workflow.state / "screen" / "review-7").unlink()
    second = workflow.run_script("tick")
    assert second.returncode == 0, second.stdout + second.stderr
    # The fresh reviewer is the usual one, and stays so on this head.
    assert workflow.is_running("review-7")
    assert "--effort high --model opus" in " ".join(session_arguments(workflow.state, "review-7"))
    assert not (folder / "review-7.conflict-reviewer").exists()
    assert _reviewer(workflow) == "high opus\n"
    # A new head is measured again.
    workflow.git("commit", "--allow-empty", "-m", "fix: send-back", cwd=workflow.worktree(7))
    _reviewer(workflow)
    assert not (folder / "review-7.usual-reviewer").exists()
