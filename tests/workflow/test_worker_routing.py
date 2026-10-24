"""Codex provenance and the default program still route interface work to Claude."""

import shlex

import pytest
from workflow_harness import Workflow


@pytest.mark.parametrize("sent_back", [False, True])
@pytest.mark.parametrize("folder", ["web", "ui"])
@pytest.mark.parametrize("program", ["claude", "codex"])
def test_screen_changes_and_their_fix_rounds_get_claude(
    workflow: Workflow, folder: str, sent_back: bool, program: str
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "diff-7").write_text(
        f"diff --git a/{folder}/src/screen.tsx b/{folder}/src/screen.tsx\n"
        "--- a/screen.tsx\n+++ b/screen.tsx\n@@ -1 +1 @@\n"
        "-<button>Old</button>\n+<button>New</button>\n"
    )
    if sent_back:
        (workflow.state / "reviews-7.json").write_text(
            '[{"body":"<!-- workflow-review round=1 result=sent-back --> Fix the screen"}]'
        )
        notes = workflow.repo / ".local/workflow"
        notes.mkdir(parents=True)
        (notes / "codex-built-7").touch()
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": program})
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")


@pytest.mark.parametrize("program", ["claude", "codex"])
@pytest.mark.parametrize("codex_built", [False, True])
def test_sent_back_backend_keeps_codex_or_uses_default(
    workflow: Workflow, program: str, codex_built: bool
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "diff-7").write_text(
        "diff --git a/backend/tool.py b/backend/tool.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    (workflow.state / "reviews-7.json").write_text(
        '[{"body":"<!-- workflow-review round=1 result=sent-back --> Fix the backend"}]'
    )
    if codex_built:
        notes = workflow.repo / ".local/workflow"
        notes.mkdir(parents=True)
        (notes / "codex-built-7").touch()

    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": program})

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7") is (codex_built or program == "codex")


@pytest.mark.parametrize("first", ["manual", "tick"])
def test_manual_codex_and_tick_cannot_start_two_workers(workflow: Workflow, first: str) -> None:
    workflow.add_pull_request(7)
    tick = ["bash", str(workflow.script.with_name("tick"))]
    manual = ["bash", str(workflow.script), "start", "worker", "7", "high", "codex"]
    contender = tick if first == "manual" else manual
    # The first start holds the real per-PR flock while it sets up the worktree, which takes
    # minutes. Run the competing entry point then, once, before either tmux server exists.
    # (A tick no longer waits for memory under the lock, so setup is the window both share.)
    workflow.fake(
        "worktree-setup",
        '#!/bin/bash\necho "worktree-setup $* in $PWD" >> "@STATE@/calls.log"\n'
        '[ ! -e "@STATE@/contender.status" ] || exit 0\n'
        f'{shlex.join(contender)} > "@STATE@/contender.out" 2> "@STATE@/contender.err"\n'
        'echo $? > "@STATE@/contender.status"\n',
    )
    env = {"WORKFLOW_WORKER_PROGRAM": "claude"}
    done = (
        workflow.run("start", "worker", "7", "high", "codex", env=env)
        if first == "manual"
        else workflow.run_script("tick", env=env)
    )

    assert done.returncode == 0, done.stderr
    assert (workflow.state / "contender.status").read_text().strip() != "0"
    assert (
        "another start for pull request 7 is under way"
        in (workflow.state / "contender.err").read_text()
    )
    assert len(workflow.calls("tmux ticket-7 new-session")) == 1
    assert len(workflow.calls("tmux codex-7 new-session")) == (1 if first == "manual" else 0)
    # Once the lock is released, the running-session check still refuses a second worker.
    again = workflow.run("start", "worker", "7", "high", "codex")
    assert again.returncode != 0
    assert "ticket-7 is already running" in again.stderr
    assert len(workflow.calls("tmux ticket-7 new-session")) == 1


@pytest.mark.parametrize(
    "body",
    [
        "Change the button wording in ui/src/cart/cart.tsx.",
        "Add a screen under web/src/app/settings.",
        "Redesign the settings page and its layout.",
        "Redesign the settings page layout in web. In ui change only test ids.",
        "Make the upload dialog close on Escape; add tests.",
        "Fix the UI of the address form; add a test.",
        "Add a modal to confirm deletion. Tests included.",
        "Open the account menu on hover. Tests included.",
        "Make the invoice preview scroll smoothly; add tests.",
        "Change the sidebar colour; add tests.",
    ],
)
def test_empty_screen_ticket_gets_claude(workflow: Workflow, body: str) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "body-7").write_text(body)
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "codex"})
    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("codex-7")
    assert workflow.is_running("ticket-7")


@pytest.mark.parametrize(
    ("title", "body", "codex"),
    [
        pytest.param(
            "Fix Save button layout", "Include regression tests.", False, id="screen-title"
        ),
        pytest.param(
            "Add test ids to the cart screen",
            "Only change test ids in ui.",
            False,
            id="screen-title-with-exception",
        ),
        pytest.param("Test ticket", "Only change test ids in ui.", True, id="exception"),
        pytest.param("Test ticket", "Add a backend retry with tests.", True, id="backend"),
        pytest.param("Tidy up", "Tidy the old helpers.", False, id="no-scope"),
    ],
)
def test_empty_ticket_title_and_exception(
    workflow: Workflow, title: str, body: str, codex: bool
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "title-7").write_text(title)
    (workflow.state / "body-7").write_text(body)
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "codex"})
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7") is codex


@pytest.mark.parametrize(
    ("patch", "codex"),
    [
        pytest.param(
            "diff --git a/backend/tool.py b/backend/tool.py\n@@ -1 +1 @@\n-old\n+new\n",
            True,
            id="backend",
        ),
        pytest.param(
            "diff --git a/ui/src/button.tsx b/ui/src/button.tsx\n"
            '@@ -1 +1 @@\n-<button data-testid="old">Save</button>\n'
            '+<button data-testid="new">Save</button>\n',
            True,
            id="test-ids",
        ),
        pytest.param(
            "diff --git a/web/src/routes.ts b/web/src/routes.ts\n"
            '@@ -1 +1 @@\n-export const routes = ["/"]\n'
            '+export const routes = ["/", "/settings"]\n',
            True,
            id="route-list",
        ),
        pytest.param(
            "diff --git a/web/src/routes.ts b/web/src/routes.ts\n"
            "--- a/web/src/routes.ts\n+++ b/web/src/routes.ts\n"
            '@@ -1 +1 @@\n-const title = "Home"\n+const title = "Settings"\n',
            False,
            id="route-title",
        ),
        pytest.param(
            "diff --git a/web/src/routes.ts b/web/src/routes.ts\n"
            '@@ -1 +1 @@\n-const routes = "/"\n+const routes = "/settings"\n',
            False,
            id="unbracketed-route",
        ),
        pytest.param(
            "diff --git a/web/src/routes.ts b/web/src/routes.ts\n"
            '@@ -1 +1 @@\n-const titles = ["Home"]\n+const titles = ["Settings"]\n',
            False,
            id="non-path-array",
        ),
        pytest.param(
            "diff --git a/ui/src/form.tsx b/ui/src/form.tsx\n"
            "--- a/ui/src/form.tsx\n+++ b/ui/src/form.tsx\n"
            "@@ -1,3 +1,2 @@\n <form>\n-<Save />\n <Name />\n"
            "@@ -9,2 +8,3 @@\n <Email />\n+<Save />\n </form>\n",
            False,
            id="moved-element",
        ),
    ],
)
@pytest.mark.parametrize("fix_round", [False, True])
def test_backend_and_only_test_ids_or_route_list_routing(
    workflow: Workflow, patch: str, codex: bool, fix_round: bool
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "diff-7").write_text(patch)
    if fix_round:
        notes = workflow.repo / ".local/workflow"
        notes.mkdir(parents=True)
        (notes / "codex-built-7").touch()
        (workflow.state / "reviews-7.json").write_text(
            '[{"body":"<!-- workflow-review round=1 result=sent-back --> Fix the regression"}]'
        )
    done = workflow.run_script(
        "tick", env={"WORKFLOW_WORKER_PROGRAM": "claude" if fix_round else "codex"}
    )
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7") is codex


@pytest.mark.parametrize(
    ("color_diff", "after_lines"),
    [
        pytest.param("---color: red\n+--color: blue\n", 2, id="change"),
        pytest.param("---color: red\n", 1, id="deletion"),
    ],
)
def test_test_id_and_column_zero_css_change_gets_claude(
    workflow: Workflow, color_diff: str, after_lines: int
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "diff-7").write_text(
        "diff --git a/ui/src/button.tsx b/ui/src/button.tsx\n"
        "--- a/ui/src/button.tsx\n+++ b/ui/src/button.tsx\n"
        f"@@ -1,2 +1,{after_lines} @@\n"
        '-<button data-testid="old">Save</button>\n'
        '+<button data-testid="new">Save</button>\n'
        f"{color_diff}"
    )
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "codex"})
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")


CODEX_LINE = "Worker: Codex (a bug fix with no new UI)"


@pytest.mark.parametrize(
    ("body", "codex"),
    [
        pytest.param(f"Fix the Save button.\n\n{CODEX_LINE}\n", True, id="line"),
        pytest.param(f"Fix the Save button.\n  {CODEX_LINE}  \n", True, id="line-with-spaces"),
        pytest.param(
            f"Fix the Save button.\r\n{CODEX_LINE}\r\nMore.", True, id="line-from-github-web"
        ),
        pytest.param("Fix the Save button.", False, id="no-line"),
        pytest.param(f"Fix the Save button. `{CODEX_LINE}`", False, id="line-inside-a-sentence"),
        pytest.param(f"Fix the Save button.\n{CODEX_LINE}, maybe\n", False, id="line-with-more"),
    ],
)
@pytest.mark.parametrize("ticket", ["screen-diff", "screen-title"])
def test_bug_fix_line_sends_screen_work_to_codex(
    workflow: Workflow, body: str, codex: bool, ticket: str
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "body-7").write_text(body)
    if ticket == "screen-diff":
        (workflow.state / "diff-7").write_text(
            "diff --git a/web/src/screen.tsx b/web/src/screen.tsx\n"
            "--- a/screen.tsx\n+++ b/screen.tsx\n@@ -1 +1 @@\n"
            "-<button>Old</button>\n+<button>New</button>\n"
        )
    else:
        (workflow.state / "title-7").write_text("Fix Save button layout on the settings page")
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "codex"})
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7") is codex


@pytest.mark.parametrize("line", [True, False])
def test_sent_back_bug_fix_with_the_line_keeps_codex_for_its_screen_fix(
    workflow: Workflow, line: bool
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "body-7").write_text(
        f"Fix the Save button.\n\n{CODEX_LINE}" if line else "Fix the Save button."
    )
    (workflow.state / "diff-7").write_text(
        "diff --git a/ui/src/button.tsx b/ui/src/button.tsx\n"
        "--- a/button.tsx\n+++ b/button.tsx\n@@ -1 +1 @@\n"
        "-<button>Old</button>\n+<button>New</button>\n"
    )
    (workflow.state / "reviews-7.json").write_text(
        '[{"body":"<!-- workflow-review round=1 result=sent-back --> Fix the screen"}]'
    )
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True)
    (notes / "codex-built-7").touch()
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "claude"})
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7") is line


@pytest.mark.parametrize(
    ("patch", "codex"),
    [
        pytest.param(
            "diff --git a/backend/tool.py b/backend/tool.py\n@@ -1 +1 @@\n-old\n+new\n",
            True,
            id="backend",
        ),
        pytest.param(
            "diff --git a/web/src/screen.tsx b/web/src/screen.tsx\n"
            "--- a/screen.tsx\n+++ b/screen.tsx\n@@ -1 +1 @@\n"
            "-<button>Old</button>\n+<button>New</button>\n",
            False,
            id="screen",
        ),
    ],
)
def test_unreadable_description_leaves_routing_by_the_diff(
    workflow: Workflow, patch: str, codex: bool
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "diff-7").write_text(patch)
    (workflow.state / "body-7").write_text(f"Fix it.\n{CODEX_LINE}\n")
    (workflow.state / "fail-view-7").touch()
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "codex"})
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert workflow.is_running("codex-7") is codex


def test_the_line_does_not_override_the_claude_default(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "body-7").write_text(f"Fix the Save button.\n\n{CODEX_LINE}\n")
    done = workflow.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "claude"})
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")
