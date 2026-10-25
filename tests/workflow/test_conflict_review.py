"""A conflict round reuses only proved review evidence, through the real workflow scripts."""

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from workflow.test_codex_worker import _relay, _rpc  # pyright: ignore[reportPrivateUsage]
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


@pytest.mark.parametrize("new_head", [False, True])
def test_a_conflict_reviewer_is_nudged_only_for_a_later_conflicting_head(
    handed: GitHub, new_head: bool
) -> None:
    workflow = handed.workflow
    head, main = _conflict(workflow)
    handed.head, handed.base = head, main
    handed.pulls["7"].update(headRefOid=head, mergeable="CONFLICTING")
    handed.main()
    handed.ci(main, head, tested_base=workflow.git("merge-base", main, head))
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    note = _folder(workflow) / "merge-wanted-7"
    data = json.loads(note.read_text())
    data["head"] = head
    note.write_text(json.dumps(data))
    _publish(handed, head=head)

    started = workflow.run_script("tick")
    assert started.returncode == 0, started.stdout + started.stderr
    assert "Review conflict resolution for pull request 7" in " ".join(
        session_arguments(workflow.state, "review-7")
    )
    # The reviewer resolves the merge, while GitHub still reports the passed head.
    resolved = _resolve(workflow)
    _idle(workflow, "review-7")
    workflow.screen(
        "● Checking the resolution.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n",
        "review-7",
    )
    if new_head:
        handed.pulls["7"]["headRefOid"] = resolved
        (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {resolved}\n")
        handed.save()

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr

    nudges = workflow.calls("tmux review-7 send-keys -t =review-7: -l GitHub reports")
    assert len(nudges) == int(new_head)
    if new_head:
        assert f"merge conflict on {resolved}" in nudges[0]


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


def _program(workflow: Workflow, program: str = "codex", **options: object) -> None:
    config = workflow.repo / "shipline.json"
    values = json.loads(config.read_text())
    values["conflict_review"] = {"program": program, **options}
    config.write_text(json.dumps(values))


def _record_handback(workflow: Workflow) -> None:
    (_folder(workflow) / "merge-review-7").write_text(
        json.dumps(
            {
                "head": workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)),
                "event": "conflict-event",
                "reason": "non-mechanical conflict in " + _FILE,
            }
        )
    )


def _codex_conflict(workflow: Workflow) -> tuple[str, str]:
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )
    _program(workflow)
    _record_handback(workflow)
    return head, main


def test_codex_conflict_starts_with_its_sentence_and_bridge(workflow: Workflow) -> None:
    head, _ = _codex_conflict(workflow)
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    args = shlex.split(session_arguments(workflow.state, "codex-7")[-1])
    assert instruction(
        f"Review conflict resolution for pull request 7 extending passed head {head} "
        f"in files {_FILE}"
    ) in args
    assert "workspace-write" in args
    grants = [args[index + 1] for index, word in enumerate(args) if word == "--add-dir"]
    assert str(_folder(workflow)) in grants
    bridge = shlex.split(session_arguments(workflow.state, "review-7")[-1])
    assert bridge[bridge.index("--tools") + 1] == ""
    assert "codex-bridge" in (workflow.notes(7) / "review-7.codex-config.json").read_text()
    assert (workflow.notes(7) / "review-7.codex").exists()
    assert (workflow.notes(7) / "review-7.started").exists()
    assert not (_folder(workflow) / "codex-built-7").exists()
    done = workflow.run("stop", "review-7")
    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("codex-7")
    assert not workflow.is_running("review-7")


def test_conflict_replaces_a_crashed_workers_codex_flag(workflow: Workflow) -> None:
    _codex_conflict(workflow)
    notes = workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "ticket-7.codex").touch()
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    assert not (notes / "ticket-7.codex").exists()
    assert (_folder(workflow) / "codex-built-7").exists()
    (workflow.state / "tmux/review-7").unlink()
    listed = workflow.run("list")
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.startswith("review-7  idle")
    assert "ticket-7  " not in listed.stdout


@pytest.mark.parametrize("built", [False, True])
def test_codex_resolution_gets_claude_and_cursor(workflow: Workflow, built: bool) -> None:
    _panel_conflict(workflow)
    _program(workflow)
    _record_handback(workflow)
    if built:
        (_folder(workflow) / "codex-built-7").touch()
    assert workflow.run("start", "review", "7").returncode == 0
    resolved = _resolve(workflow)
    workflow.set_pull_request(7, "OPEN", resolved)
    for tool in ("agent", "codex"):
        workflow.fake(tool, '#!/bin/bash\necho "@TOOL@ report"\n')
    workflow.fake(
        "claude",
        """#!/bin/bash
strict=false
config="" tools=""
while (($#)); do
  case $1 in
    --strict-mcp-config) strict=true ;;
    --mcp-config) shift; config=$1 ;;
    --tools) shift; tools=$1 ;;
  esac
  shift
done
$strict && [[ $tools == Read,Grep,Glob && -f $config ]] || exit 91
[[ $(cat "$config") == '{"mcpServers":{}}' ]] || exit 92
echo 'claude report'
""",
    )
    done = workflow.run_script("review-panel", "7", "codex", "cursor")
    assert done.returncode == 0, done.stdout + done.stderr
    reports = _folder(workflow) / "reviews/7" / resolved
    assert "claude report" in (reports / "claude.md").read_text()
    assert "agent report" in (reports / "cursor.md").read_text()
    assert not (reports / "codex.md").exists()


def test_crash_before_panel_preserves_codex_resolution_provenance(workflow: Workflow) -> None:
    _panel_conflict(workflow)
    _program(workflow)
    _record_handback(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    resolved = _resolve(workflow, "hunk")
    workflow.set_pull_request(7, "OPEN", resolved)
    # The resolver ended after pushing, before it could run a panel or request a fallback.
    assert workflow.run("stop", "review-7").returncode == 0
    assert workflow.run("start", "review", "7").returncode == 0
    assert not workflow.is_running("codex-7")
    args = " ".join(session_arguments(workflow.state, "review-7"))
    assert "--model opus" in args and "--mcp-config" not in args
    workflow.fake("agent", '#!/bin/bash\necho "Cursor report"\n')
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    reports = _folder(workflow) / "reviews/7" / resolved
    assert "Codex resolved a conflict" in (reports / "codex.md").read_text()
    assert "Cursor report" in (reports / "cursor.md").read_text()


@pytest.mark.parametrize("extra", ["file", "hunk", "revoked", "corrupt", "full"])
def test_codex_panel_fallback_returns_to_claude(workflow: Workflow, extra: str) -> None:
    _panel_conflict(workflow)
    _program(workflow)
    _record_handback(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    resolved = _resolve(workflow, extra)
    workflow.set_pull_request(7, "OPEN", resolved)
    if extra == "revoked":
        (workflow.state / "reviews-7.json").write_text("[]")
    elif extra == "corrupt":
        (_folder(workflow) / "review-7.conflict.json").write_text("invalid")
    done = workflow.run_script(
        "review-panel", "7", *(["--full-review"] if extra == "full" else [])
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "Full review required" in done.stderr
    assert (_folder(workflow) / "review-7.usual-reviewer").read_text().strip() == resolved
    assert not (_folder(workflow) / "reviews/7" / resolved / "codex.md").exists()
    assert workflow.run("stop", "review-7").returncode == 0
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    args = " ".join(session_arguments(workflow.state, "review-7"))
    assert "--model opus" in args and "--mcp-config" not in args
    assert not workflow.is_running("codex-7")
    workflow.fake("agent", '#!/bin/bash\necho "Cursor report"\n')
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    full = _folder(workflow) / "reviews/7" / resolved / "full-review"
    assert "Codex resolved a conflict" in (full / "codex.md").read_text()
    assert "Cursor report" in (full / "cursor.md").read_text()


def test_conflict_program_is_validated_by_name(workflow: Workflow) -> None:
    _program(workflow, "unknown")
    done = workflow.run("start", "review", "7")
    assert done.returncode != 0
    assert "conflict_review.program must be one of claude, codex" in done.stderr


@pytest.mark.parametrize("program", ["claude", "codex"])
def test_program_alone_and_optional_conflict_limits(workflow: Workflow, program: str) -> None:
    _codex_conflict(workflow)
    _program(workflow, program)
    assert _reviewer(workflow) == "high opus\n"
    _program(workflow, program, effort="medium", model="sonnet", max_changed_lines=5)
    assert _reviewer(workflow) == "high opus\n"
    assert not (_folder(workflow) / "review-7.conflict-reviewer").exists()


def test_codex_conflict_honors_effort_and_explicit_model(workflow: Workflow) -> None:
    _codex_conflict(workflow)
    _program(workflow, effort="medium", model="gpt-example", max_changed_lines=6)
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    args = shlex.split(session_arguments(workflow.state, "codex-7")[-1])
    assert "model_reasoning_effort=medium" in args
    assert args[args.index("--model") + 1] == "gpt-example"


def test_conflict_bridge_relays_question_and_recorded_answer(workflow: Workflow) -> None:
    _codex_conflict(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    message = 'Recommend 1.\nOwner only: yes\nType "1".'
    done = workflow.run_script(
        "notify",
        "codex",
        json.dumps({"type": "agent-turn-complete", "last-assistant-message": message}),
        env={"WORKFLOW_SESSION": "review-7", "WORKFLOW_CODEX_BRIDGE": "1"},
    )
    assert done.returncode == 0
    with _relay(workflow, "review-7") as relay:
        turn = _rpc(relay, "tools/call", {"name": "await_turn"})
        assert turn["result"]["content"][0]["text"] == message
        workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"}, "review-7")
        assert "result" in _rpc(relay, "tools/call", {"name": "reply"})
    assert (workflow.notes(7) / "review-7.codex-answered").exists()


@pytest.mark.parametrize("lost_bridge", [False, True])
def test_conflict_codex_quota_and_bridge_recovery(workflow: Workflow, lost_bridge: bool) -> None:
    _codex_conflict(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    workflow.screen(
        "■ You've hit your usage limit. Try again at 4:00 PM.\n\u203a \n", "codex-7"
    )
    if lost_bridge:
        (workflow.state / "tmux/review-7").unlink()
    listed = workflow.run("list")
    assert listed.returncode == 0, listed.stderr
    assert "review-7  limit" in listed.stdout and "Codex limit" in listed.stdout
    assert "resets" not in listed.stdout
    if lost_bridge:
        done = workflow.run("bridge", "review-7")
        assert done.returncode == 0, done.stderr
        assert workflow.is_running("review-7") and workflow.is_running("codex-7")


def test_tick_handback_starts_codex_conflict_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    head, main = _conflict(workflow)
    _program(workflow)
    handed.head, handed.base = head, main
    handed.pulls["7"]["headRefOid"] = head
    handed.pulls["7"]["mergeable"] = "CONFLICTING"
    handed.main()
    handed.ci(main, head, tested_base=workflow.git("merge-base", main, head))
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    note = _folder(workflow) / "merge-wanted-7"
    note.write_text(json.dumps({**json.loads(note.read_text()), "head": head}))
    _publish(handed, "passed", head)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.is_running("codex-7") and workflow.is_running("review-7")
    args = shlex.split(session_arguments(workflow.state, "codex-7")[-1])
    assert args[-1] == instruction(
        f"Review conflict resolution for pull request 7 extending passed head {head} "
        f"in files {_FILE}"
    )


@pytest.mark.parametrize("lost_bridge", [False, True])
def test_tick_reads_codex_conflict_completion_and_starts_full_review(
    workflow: Workflow, lost_bridge: bool
) -> None:
    _codex_conflict(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    resolved = _resolve(workflow, "hunk")
    workflow.set_pull_request(7, "OPEN", resolved)
    (_folder(workflow) / "review-7.usual-reviewer").write_text(resolved + "\n")
    done = workflow.run_script(
        "notify",
        "codex",
        json.dumps(
            {
                "type": "agent-turn-complete",
                "last-assistant-message": "Full review required.\nreview finished",
            }
        ),
        env={"WORKFLOW_SESSION": "review-7", "WORKFLOW_CODEX_BRIDGE": "1"},
    )
    assert done.returncode == 0
    assert (workflow.notes(7) / "review-7.codex-turn.json").exists()
    if lost_bridge:
        (workflow.state / "tmux/review-7").unlink()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not workflow.is_running("codex-7")
    assert workflow.is_running("review-7")
    assert "--mcp-config" not in " ".join(session_arguments(workflow.state, "review-7"))
    assert not _rule_breaks(workflow)


@pytest.mark.parametrize(
    "operation", ["publish", "dash-file", "other", "approve", "full", "ready", "body", "ticket"]
)
def test_codex_conflict_gh_allows_only_its_own_conflict_round(
    workflow: Workflow, operation: str
) -> None:
    head, _ = _codex_conflict(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    resolved = _resolve(workflow)
    payload = workflow.root / "round.json"
    if operation == "dash-file":
        payload = workflow.worktree(7) / "--round.json"
    body = "<!-- workflow-review round=2 result=passed -->\n"
    if operation != "full":
        body += f"<!-- workflow-conflict extends={head} -->\n"
    payload.write_text(
        json.dumps(
            {
                "body": body,
                "commit_id": resolved,
                "event": "APPROVE" if operation == "approve" else "COMMENT",
            }
        )
    )
    number = 8 if operation == "other" else 7
    args = [
        "api", "-X", "POST", f"repos/example-org/example-app/pulls/{number}/reviews",
        "--input", payload.name if operation == "dash-file" else str(payload),
    ]
    if operation == "ready":
        args = ["pr", "ready", "7"]
    elif operation == "body":
        args = [
            "api", "-X", "PATCH", "repos/example-org/example-app/pulls/7", "-F",
            "body=@round.json",
        ]
    elif operation == "ticket":
        args = ["pr", "create", "--draft"]
    env = dict(
        line.split("=", 1)
        for line in (workflow.state / "env/codex-7").read_text().splitlines()
    )
    workflow.fake("gh", '#!/bin/bash\necho allowed\n')
    shim = Path(env["PATH"].split(":")[0]) / "gh"
    done = subprocess.run(
        ["bash", str(shim), *args], cwd=workflow.worktree(7), env=env,
        text=True, capture_output=True, check=False,
    )
    assert (done.returncode == 0) == (operation in {"publish", "dash-file"}), done.stdout + done.stderr


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


def test_the_common_ancestor_shown_by_the_owner_s_git_is_not_counted(workflow: Workflow) -> None:
    """With `merge.conflictStyle` at `diff3`, git writes the common ancestor into each block too:
    its lines are not the conflict's."""
    workflow.add_worktree(7, draft=False)
    head, main = _conflict(workflow)
    workflow.git("config", "merge.conflictStyle", "diff3")
    _conflict_review(workflow, 6)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": [_FILE]})
    )

    assert _reviewer(workflow) == "medium sonnet\n"


@pytest.mark.parametrize(
    ("file", "kept"),
    [
        ("web/src/app/(menu)/settings/page.tsx", True),
        ("web/src/app/runs/[runId]/page.tsx", True),
        ("web/src/app/odd name.tsx", False),
        ("web/src/app/$(touch x).tsx", False),
        ("web/src/app/`id`.tsx", False),
        ('web/src/app/"page".tsx', False),
        ("web/src/app/a,b.tsx", False),
    ],
)
def test_a_scope_takes_route_folders_and_nothing_a_command_line_expands(
    workflow: Workflow, file: str, kept: bool
) -> None:
    """A route group `(menu)` or a dynamic segment `[runId]` is an ordinary folder of a web
    app: its conflict gets the conflict round, and so the reviewer `conflict_review` names."""
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": "a" * 40, "main": "b" * 40, "files": [file]})
    )

    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; from pathlib import Path; from conflict_review import Scope; "
            "print(Scope.read(Path(sys.argv[1]), '7').files)",
            str(folder),
        ],
        cwd=workflow.script.parent,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )

    if kept:
        assert done.returncode == 0, done.stdout + done.stderr
        assert done.stdout == f"{[file]}\n"
    else:
        assert "invalid conflict review scope" in done.stderr


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
