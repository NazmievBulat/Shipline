"""A conflict round reuses only proved review evidence, through the real workflow scripts."""

import json
import subprocess
from pathlib import Path

import pytest
from workflow.test_merge_driver import _folder, _publish  # pyright: ignore[reportPrivateUsage]
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow.test_review_panel import _panel  # pyright: ignore[reportPrivateUsage]
from workflow_harness import Workflow, instruction, session_arguments

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
    assert "--effort high --model opus" in args
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
