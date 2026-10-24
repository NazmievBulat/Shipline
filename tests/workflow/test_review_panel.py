"""Panel processes get independent detached checkouts and cannot write to the branch."""

import fcntl
import json
import shlex
import shutil
import subprocess
import time

import pytest
from workflow_harness import Workflow


def _panel(workflow: Workflow) -> None:
    (workflow.repo / "AGENTS.md").write_text("Rules from main\n")
    workflow.git("add", "AGENTS.md")
    workflow.git("commit", "--quiet", "-m", "docs: main rules")
    workflow.git("push", "--quiet", "origin", "main")
    workflow.add_worktree(7, draft=False)
    # The stand-in verifies the read-only mount contract; real tools stay outside tests.
    workflow.fake(
        "bwrap",
        """#!/bin/bash
[ "$1 $2 $3" = "--ro-bind / /" ] || exit 99
while [ "$1" != -- ]; do
  if [ "$1" = --chdir ]; then shift; cd "$1" || exit 99; fi
  if [ "$1" = --bind ]; then
    shift
    case "$1" in */workflow-review-*) ;; *) exit 98 ;; esac
    [ "$1" = "$2" ] && [ "$1" = "$HOME" ] || exit 98
    shift
  fi
  shift
done
shift
exec "$@"
""",
    )
    for tool in ("agent", "codex"):
        workflow.fake(
            tool,
            """#!/bin/bash
echo "$PWD" > "@STATE@/@TOOL@-cwd"
git symbolic-ref -q HEAD && exit 99
cat .review/task.md .review/diff.patch .review/workflow-rules.md
echo "$*"
echo "@TOOL@ started" > "@STATE@/@TOOL@-started"
if [ "@TOOL@" = agent ]; then
  while [ ! -e "@STATE@/codex-started" ]; do /bin/sleep 0.05; done
fi
echo "@TOOL@ report"
exit 17
""",
        )


def test_panel_runs_in_detached_copies(workflow: Workflow) -> None:
    _panel(workflow)
    before = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    report = workflow.repo / ".local/workflow/reviews/7" / before
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stderr
    assert (workflow.state / "agent-started").exists()
    assert (workflow.state / "codex-started").exists()
    for who, tool in (("cursor", "agent"), ("codex", "codex")):
        text = (report / f"{who}.md").read_text()
        assert f"{tool} report" in text and "exit 17" in text
        assert f"skipped: {who} exited unsuccessfully" in text
        assert "Test ticket" in text
        assert "black-box API" in text and "browser" in text
    copies = [(workflow.state / f"{tool}-cwd").read_text().strip() for tool in ("agent", "codex")]
    assert len(set(copies)) == 2
    assert str(workflow.worktree(7)) not in copies
    assert workflow.git("status", "--porcelain", cwd=workflow.worktree(7)) == ""
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == before


@pytest.mark.parametrize("reviewers, codex_built", [((), False), ((), True), (("agy",), False)])
def test_panel_never_starts_agy(
    workflow: Workflow, reviewers: tuple[str, ...], codex_built: bool
) -> None:
    _panel(workflow)
    if codex_built:
        notes = workflow.repo / ".local/workflow"
        notes.mkdir(parents=True)
        (notes / "codex-built-7").touch()
        workflow.fake("agent", '#!/bin/bash\ntouch "@STATE@/agent-started"\n')
    workflow.fake("agy", '#!/bin/bash\ntouch "@STATE@/agy-started"\n')
    done = workflow.run_script("review-panel", "7", *reviewers)
    assert done.returncode == (2 if reviewers else 0), done.stderr
    assert "agy may arrive later" not in done.stdout
    assert not (workflow.state / "agy-started").exists()
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    report = workflow.repo / ".local/workflow/reviews/7" / head
    assert not (report / "agy.md").exists()
    assert not (report / "agy.md.pending").exists()
    if not reviewers:
        assert (workflow.state / "agent-started").exists()
        assert (workflow.state / "codex-started").exists() is not codex_built


def test_missing_panel_tool_is_skipped(workflow: Workflow) -> None:
    _panel(workflow)
    # Use a tool lookup stand-in, independent of tools installed on the test runner.
    script = workflow.script.with_name("review-panel")
    text = script.read_text()
    # Without the lookup the stand-in agent would wait for Codex until the 30-minute timeout.
    assert "command -v agent" in text
    script.write_text(text.replace("command -v agent", "command -v workflow-missing-agent"))
    done = workflow.run_script("review-panel", "7", "cursor")
    assert done.returncode == 0, done.stderr
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    report = workflow.repo / ".local/workflow/reviews/7" / head / "cursor.md"
    assert "skipped" in report.read_text().lower()
    assert not (workflow.state / "agent-started").exists()


def test_panel_reuses_reports_of_the_same_head_and_starts_again_on_a_new_one(
    workflow: Workflow,
) -> None:
    _panel(workflow)
    worktree = workflow.worktree(7)
    first = workflow.git("rev-parse", "HEAD", cwd=worktree)
    assert workflow.run_script("review-panel", "7", "codex").returncode == 0
    (workflow.state / "codex-started").unlink()
    assert workflow.run_script("review-panel", "7", "codex").returncode == 0
    assert not (workflow.state / "codex-started").exists(), "the same head was reviewed twice"
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "fix: second head", cwd=worktree)
    workflow.git("push", "--quiet", "origin", "HEAD", cwd=worktree)
    second = workflow.git("rev-parse", "HEAD", cwd=worktree)
    record = workflow.state / "pr" / "7"
    record.write_text(record.read_text().replace(first, second))
    assert workflow.run_script("review-panel", "7", "codex").returncode == 0
    assert (workflow.state / "codex-started").exists()
    reviews = workflow.repo / ".local/workflow/reviews/7"
    assert (reviews / first / "codex.md").exists()
    assert (reviews / second / "codex.md").exists()


def test_panel_excludes_codex_for_codex_built_ticket(workflow: Workflow) -> None:
    _panel(workflow)
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True)
    (notes / "codex-built-7").touch()
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    folder = workflow.repo / ".local/workflow/reviews/7" / head
    folder.mkdir(parents=True)
    (folder / "codex.md.pending").write_text("half a review\n")
    done = workflow.run_script("review-panel", "7", "codex")
    assert done.returncode == 0, done.stderr
    assert not (workflow.state / "codex-started").exists()
    assert not (folder / "codex.md.pending").exists()


def test_reviewer_can_initialize_private_cli_state_without_writing_owner_home(
    workflow: Workflow,
) -> None:
    _panel(workflow)
    owner_home = workflow.root / "home"
    (owner_home / ".codex").mkdir()
    (owner_home / ".codex/auth.json").write_text('"fixture credential"')
    workflow.fake(
        "codex",
        f"""#!/bin/bash
[ "$HOME" != {shlex.quote(str(owner_home))} ] || exit 91
[ -f "$HOME/.codex/auth.json" ] || exit 92
echo initialized > "$HOME/.codex/runtime-state"
echo 'private CLI state initialized'
""",
    )
    done = workflow.run_script("review-panel", "7", "codex")
    assert done.returncode == 0, done.stderr
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    report = workflow.repo / ".local/workflow/reviews/7" / head / "codex.md"
    assert "private CLI state initialized" in report.read_text()
    assert "exit 0" in report.read_text()
    assert not (owner_home / ".codex/runtime-state").exists()


def test_panel_materializes_main_workflow_rules_for_an_older_branch(workflow: Workflow) -> None:
    _panel(workflow)
    (workflow.repo / "AGENTS.md").write_text("Updated rules from main\n")
    workflow.git("commit", "--quiet", "-am", "docs: update main rules")
    workflow.git("push", "--quiet", "origin", "main")
    done = workflow.run_script("review-panel", "7", "codex")
    assert done.returncode == 0, done.stderr
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    report = workflow.repo / ".local/workflow/reviews/7" / head / "codex.md"
    assert "Updated rules from main" in report.read_text()
    assert ".review/workflow-rules.md" in report.read_text()


def test_real_sandbox_refuses_writes_to_branch_checkout_and_git_metadata(
    workflow: Workflow,
) -> None:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        # Environment prerequisite: requires bubblewrap and host support for nested sandboxes.
        pytest.skip("bubblewrap is not installed")  # noqa: TID251
    probe = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--", "/bin/true"], capture_output=True, check=False
    )
    if probe.returncode:
        # Environment prerequisite: requires bubblewrap and host support for nested sandboxes.
        pytest.skip("host does not allow a nested filesystem sandbox")  # noqa: TID251
    _panel(workflow)
    workflow.fake("bwrap", f'#!/bin/bash\nexec {shlex.quote(bwrap)} "$@"\n')
    branch_file = workflow.worktree(7) / "AGENTS.md"
    workflow.fake(
        "codex",
        f"""#!/bin/bash
if (echo changed > {shlex.quote(str(branch_file))}) 2>/dev/null; then exit 90; fi
if (echo changed > AGENTS.md) 2>/dev/null; then exit 91; fi
if git -C {shlex.quote(str(workflow.repo))} update-ref refs/heads/main HEAD 2>/dev/null; then
  exit 92
fi
echo initialized > "$HOME/.codex/runtime-state" || exit 93
echo 'branch and metadata writes refused; runtime writable'
""",
    )
    done = workflow.run_script("review-panel", "7", "codex")
    assert done.returncode == 0, done.stderr
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    report = workflow.repo / ".local/workflow/reviews/7" / head / "codex.md"
    assert "branch and metadata writes refused; runtime writable" in report.read_text()
    assert "exit 0" in report.read_text()
    assert branch_file.read_text() == "Rules from main\n"


def test_panel_gives_both_reviewers_the_weaker_tests_report(workflow: Workflow) -> None:
    _panel(workflow)
    worktree = workflow.worktree(7)
    test = worktree / "test_sample.py"
    test.write_text("def test_sample():\n    assert value == 2\n")
    workflow.git("add", ".", cwd=worktree)
    workflow.git("commit", "--quiet", "-m", "test: exact assertion", cwd=worktree)
    # Make the exact test part of the base, then publish its weakening as the PR.
    workflow.git("merge", "--ff-only", workflow.branch(7), cwd=workflow.repo)
    workflow.git("push", "--quiet", "origin", "main", cwd=workflow.repo)
    test.write_text("def test_sample():\n    assert value\n")
    workflow.git("commit", "--quiet", "-am", "test: weaken", cwd=worktree)
    workflow.git("push", "--quiet", "origin", "HEAD", cwd=worktree)
    head = workflow.git("rev-parse", "HEAD", cwd=worktree)
    workflow.set_pull_request(7, "OPEN", head)
    for tool in ("agent", "codex"):
        workflow.fake(tool, "#!/bin/bash\ncat .review/weaker-tests.md\n")
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stderr
    folder = workflow.repo / ".local/workflow/reviews/7" / head
    report = (folder / "weaker-tests.md").read_text()
    assert "test_sample.py:2" in report and "assertion weakened" in report
    for tool in ("cursor", "codex"):
        assert report in (folder / f"{tool}.md").read_text()


def _counting_codex(workflow: Workflow) -> None:
    """Codex that counts its starts; the first finishes only once the test releases it."""
    workflow.fake(
        "codex",
        """#!/bin/bash
[ -e "@STATE@/codex-starts" ] && first=false || first=true
echo start >> "@STATE@/codex-starts"
while $first && [ ! -e "@STATE@/codex-release" ]; do /bin/sleep 0.05; done
echo "codex report"
""",
    )


def test_abandoned_pending_report_is_reviewed_again(workflow: Workflow) -> None:
    _panel(workflow)
    _counting_codex(workflow)
    (workflow.state / "codex-release").touch()
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    folder = workflow.repo / ".local/workflow/reviews/7" / head
    folder.mkdir(parents=True)
    # Partial output of a review whose process is gone, as an interrupted panel leaves it.
    (folder / "codex.md.pending").write_text("half a review\n")
    done = workflow.run_script("review-panel", "7", "codex")
    assert done.returncode == 0, done.stderr
    assert (workflow.state / "codex-starts").read_text() == "start\n"
    text = (folder / "codex.md").read_text()
    assert "codex report" in text and "exit 0" in text
    assert "half a review" not in text
    assert not (folder / "codex.md.pending").exists()


def test_review_still_running_after_its_panel_died_is_not_started_twice(
    workflow: Workflow,
) -> None:
    _panel(workflow)
    _counting_codex(workflow)
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    folder = workflow.repo / ".local/workflow/reviews/7" / head
    script = workflow.script.parent / "review-panel"
    panel = subprocess.Popen(
        ["bash", str(script), "7", "codex"],
        cwd=workflow.repo,
        env=workflow.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    starts = workflow.state / "codex-starts"
    try:
        deadline = time.monotonic() + 60
        while not starts.exists():
            assert time.monotonic() < deadline, "codex never started"
            assert panel.poll() is None, "panel ended before codex started"
            time.sleep(0.05)
        # The panel itself dies (a closed terminal, a killed tool call); its reviewer runs on.
        panel.kill()
        panel.wait()
        assert (folder / "codex.md.pending").exists()
        again = workflow.run_script("review-panel", "7", "codex")
        assert again.returncode == 0, again.stderr
        assert starts.read_text() == "start\n", "a running review was started a second time"
        assert (folder / "codex.md.pending").exists()
    finally:
        (workflow.state / "codex-release").touch()
    deadline = time.monotonic() + 60
    while not (folder / "codex.md").exists():
        assert time.monotonic() < deadline, "the running review never completed"
        time.sleep(0.05)
    assert "codex report" in (folder / "codex.md").read_text()
    assert starts.read_text() == "start\n"


def test_abandoned_review_is_retried_beside_one_still_running(workflow: Workflow) -> None:
    _panel(workflow)
    _counting_codex(workflow)
    (workflow.state / "codex-release").touch()
    workflow.fake("agent", '#!/bin/bash\ntouch "@STATE@/agent-started"\n')
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    folder = workflow.repo / ".local/workflow/reviews/7" / head
    folder.mkdir(parents=True)
    (folder / "cursor.md.pending").write_text("cursor still writing\n")
    (folder / "codex.md.pending").write_text("half a review\n")
    # A live Cursor review holds its own lock; the Codex one was abandoned.
    with (folder / "cursor.md.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stderr
    assert "cursor review of" in done.stdout and "still running" in done.stdout
    assert not (workflow.state / "agent-started").exists()
    assert (folder / "cursor.md.pending").read_text() == "cursor still writing\n"
    assert (workflow.state / "codex-starts").read_text() == "start\n"
    assert "codex report" in (folder / "codex.md").read_text()


def test_report_published_while_the_lock_is_taken_is_not_reviewed_again(
    workflow: Workflow,
) -> None:
    _panel(workflow)
    _counting_codex(workflow)
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    folder = workflow.repo / ".local/workflow/reviews/7" / head
    folder.mkdir(parents=True)
    (folder / "codex.md.pending").write_text("almost done\n")
    (workflow.state / "codex-release").touch()
    real = shutil.which("flock")
    assert real
    # The surviving reviewer publishes its report just as the panel takes the reviewer lock.
    workflow.fake(
        "flock",
        f"""#!/bin/bash
if [ "$2" = 8 ] && [ -e {shlex.quote(str(folder / "codex.md.pending"))} ]; then
  mv {shlex.quote(str(folder / "codex.md.pending"))} {shlex.quote(str(folder / "codex.md"))}
fi
exec {shlex.quote(real)} "$@"
""",
    )
    done = workflow.run_script("review-panel", "7", "codex")
    assert done.returncode == 0, done.stderr
    assert not (workflow.state / "codex-starts").exists(), "a published report was reviewed again"
    assert (folder / "codex.md").read_text() == "almost done\n"


@pytest.mark.parametrize(
    "change",
    ["unedited", "removed", "answers", "notes", "unreadable", "deleted", "missing", "renamed"],
)
def test_panel_preserves_opening_task_and_reports_requirement_changes(
    workflow: Workflow, change: str
) -> None:
    _panel(workflow)
    opened = "## What done means\n\n- Keep both requirements.\n- Keep this requirement too.\n"
    current = opened
    if change == "removed":
        current = opened.replace("- Keep this requirement too.\n", "")
    elif change == "answers":
        current += "\n## Answers given for the owner\n\nApproved.\n"
    elif change == "notes":
        current += (
            "\n## Questions for the owner\n\nQuestion.\n"
            "\n## Weaker tests\n\nExplanation.\n### Evidence\n\nProof.\n"
            "\n## Choices made\n\nImplementation notes.\n"
        )
        # Ignoring notes must stop at the next task section, including after subheadings.
        opened += "\n## Out of scope\n\nNo other changes.\n"
        current += "\n## Out of scope\n\nNo other changes.\n"
    (workflow.state / "body-7").write_text(current)
    edits = [] if change == "unedited" else [{"diff": opened, "deletedAt": None}]
    if change == "deleted":
        edits = [{"diff": None, "deletedAt": "2026-10-19T00:00:00Z"}]
    history = {
        "includesCreatedEdit": change != "missing",
        "userContentEdits": {"nodes": edits},
        "timelineItems": {
            "nodes": [{"previousTitle": "Original title"}] if change == "renamed" else []
        },
    }
    (workflow.state / "task-history.json").write_text(
        json.dumps({"data": {"repository": {"pullRequest": history}}})
    )
    if change == "unreadable":
        (workflow.state / "fail-task-history").touch()
    for tool in ("agent", "codex"):
        workflow.fake(tool, "#!/bin/bash\ncat .review/task-as-opened.md .review/task-changes.md\n")
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stderr
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    folder = workflow.repo / ".local/workflow/reviews/7" / head
    changes = (folder / "task-changes.md").read_text()
    original = (folder / "task-as-opened.md").read_text()
    title = "Original title" if change == "renamed" else "Test ticket"
    expected_body = (
        opened
        if change not in {"unedited", "unreadable", "deleted", "missing"}
        else opened.rstrip("\n")
    )
    assert original == f"# {title}\n\n{expected_body}\n"
    if change == "removed":
        assert "- Keep this requirement too." in original
        assert "-- Keep this requirement too." in changes
    elif change == "renamed":
        assert "-# Original title" in changes and "+# Test ticket" in changes
    elif change in {"unreadable", "deleted", "missing"}:
        assert "History unavailable" in changes and "History unavailable" in done.stdout
    else:
        assert changes == ""
    for reviewer in ("cursor", "codex"):
        report = (folder / f"{reviewer}.md").read_text()
        assert original in report
        if changes:
            assert changes in report
        # Once from the reviewer's copy of task-changes.md, once added by the panel itself.
        assert report.count("History unavailable") == (
            2 if change in {"unreadable", "deleted", "missing"} else 0
        )
