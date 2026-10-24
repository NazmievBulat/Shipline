"""Break tests for pull request 48: `review-effort N` and `session start review N`.

Each test replays a daily use of a repository that marks its risky paths, or a failure that
use meets, with neutral names. They run against the pull request's own scripts through the
same stand-ins its tests use.
"""

import importlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pytest
from workflow_harness import Workflow, session_arguments

# A repository's own `review.high_effort_paths`, one reason per line, as a real one writes them.
_PATTERNS = [
    # stored data: persistence and store modules, migrations, SQL, database roles
    "(^|/)(store|stores|persistence)/",
    "^db/migrations/",
    "\\.sql$",
    "^db/roles/",
    # what a model reads: prompts, the writer's input, masking, evaluations, the model gateway
    "^app/prompts/",
    "^app/writer/input",
    "^app/masking/",
    "^evals/",
    "^app/model_gateway/",
    # background work and locks: queues, leases, workers, the workflow's scripts and slots
    "^app/queues/",
    "^app/leases/",
    "^app/workers/",
    "^scripts/workflow/",
    # account boundaries and security
    "^app/auth/",
    "^app/sessions/",
    "password",
    "^app/accounts/",
    "^app/api/",
    "^scripts/security/",
    "^\\.github/workflows/security",
    "^app/mail/webhook",
]

_HIGH = [
    "app/store/orders_store.py",
    "lib/persistence/cache.py",
    "db/migrations/0007_add_index.sql",
    "reports/monthly.sql",
    "db/roles/reader.yml",
    "app/prompts/summary.txt",
    "app/writer/input_builder.py",
    "app/masking/rules.py",
    "evals/summary_eval.py",
    "app/model_gateway/client.py",
    "app/queues/mail_queue.py",
    "app/leases/lease.py",
    "app/workers/sync.py",
    "scripts/workflow/push_slots.py",
    "scripts/workflow/browser_test_slots.py",
    "scripts/workflow/github_budget.py",
    "app/auth/login.py",
    "app/sessions/cookie.py",
    "app/users/password_reset.py",
    "app/accounts/settings.py",
    "app/api/routes.py",
    "scripts/security/scan.sh",
    ".github/workflows/security.yml",
    "app/mail/webhook.py",
]

_ORDINARY = [
    "app/views/home.html",
    "docs/getting-started.md",
    "app/static/logo.svg",
    "app/components/button.tsx",
    "README.md",
]

_TICKET = """## Problem

The home page shows the wrong count.

## What done means

- The count matches the list.

## Out of scope

Nothing else.

## Files

- `app/views/home.html`

Review: high
"""


def _configure(workflow: Workflow, review: object = None) -> None:
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["review"] = {"high_effort_paths": _PATTERNS} if review is None else review
    path.write_text(json.dumps(config))


def _edit(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )


def _pull_request(
    workflow: Workflow, number: int, diff: str, body: str = "Ordinary."
) -> None:
    workflow.add_pull_request(number)
    (workflow.state / f"diff-{number}").write_bytes(diff.encode())
    (workflow.state / f"body-{number}").write_bytes(body.encode())


def _effort(workflow: Workflow, number: int = 7) -> tuple[str, str]:
    done = workflow.run_script("review-effort", str(number))
    assert done.returncode == 0, done.stderr
    return done.stdout.strip(), done.stderr


def _started_effort(workflow: Workflow) -> str:
    command = session_arguments(workflow.state, "review-7")[-1]
    assert command.startswith("claude --effort "), command
    return command.split()[2]


# --- the daily flow: each group's paths, ordinary paths, a mixed day ---------------------------


@pytest.mark.parametrize("path", _HIGH)
def test_each_risky_path_of_a_real_configuration_gives_high(
    workflow: Workflow, path: str
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit(_ORDINARY[0]) + _edit(path))

    level, reason = _effort(workflow)

    assert level == "high"
    assert path in reason


def test_ordinary_paths_of_a_real_configuration_give_medium(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, "".join(_edit(path) for path in _ORDINARY))

    assert _effort(workflow) == ("medium", "")


def test_a_day_of_thirty_reviews_each_gets_its_own_level_even_read_at_once(
    workflow: Workflow,
) -> None:
    _configure(workflow)
    expected = {}
    for number in range(1, 31):
        if number % 3 == 0:
            paths = [_ORDINARY[number % 5], _HIGH[number % len(_HIGH)]]
            expected[number] = "high"
        else:
            paths = [_ORDINARY[number % 5]]
            expected[number] = "medium"
        _pull_request(workflow, number, "".join(_edit(path) for path in paths))

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = dict(
            zip(
                expected,
                pool.map(lambda n: _effort(workflow, n)[0], expected),
                strict=True,
            )
        )

    assert found == expected


# --- `Review: high` written by the orchestrator in a ticket ------------------------------------


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_review_high_in_a_ticket_description_with_the_builder_s_choices_below(
    workflow: Workflow, newline: str
) -> None:
    _configure(workflow)
    body = (
        _TICKET
        + "\n## Choices\n\n- Kept the template.\n\n## Runs\n\n```\n1 passed\n```\n"
    )
    _pull_request(workflow, 7, _edit(_ORDINARY[0]), body.replace("\n", newline))

    level, reason = _effort(workflow)

    assert level == "high"
    assert "Review: high" in reason


def test_a_session_start_follows_review_high_in_the_ticket(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit(_ORDINARY[0]), _TICKET)

    done = workflow.run("start", "review", "7")

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == "high"


@pytest.mark.parametrize("level", ["low", "medium"])
def test_a_level_given_by_hand_wins_over_review_high_and_a_risky_path(
    workflow: Workflow, level: str
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit("db/migrations/0007_add_index.sql"), _TICKET)

    done = workflow.run("start", "review", "7", level)

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == level


@pytest.mark.parametrize("path", ["app/queues/mail_queue.py", "app/views/home.html"])
def test_a_session_start_without_a_level_follows_the_paths(
    workflow: Workflow, path: str
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit(path))

    done = workflow.run("start", "review", "7")

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == ("high" if "queues" in path else "medium")


# --- an empty, unreadable or ambiguous diff ----------------------------------------------------


def test_a_fresh_ticket_with_only_its_empty_commit_gives_high(
    workflow: Workflow,
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, "")

    assert _effort(workflow)[0] == "high"
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == "high"


def test_a_pure_rename_out_of_a_risky_folder_gives_high(workflow: Workflow) -> None:
    _configure(workflow)
    diff = (
        "diff --git a/app/auth/tokens.py b/app/legacy/tokens.py\n"
        "similarity index 100%\n"
        "rename from app/auth/tokens.py\n"
        "rename to app/legacy/tokens.py\n"
    )
    _pull_request(workflow, 7, diff)

    level, reason = _effort(workflow)

    assert level == "high"
    assert "app/auth/tokens.py" in reason


def test_a_deleted_migration_gives_high(workflow: Workflow) -> None:
    _configure(workflow)
    diff = (
        "diff --git a/db/migrations/0001_init.sql b/db/migrations/0001_init.sql\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/db/migrations/0001_init.sql\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-create table example_app (id int);\n"
    )
    _pull_request(workflow, 7, diff)

    assert _effort(workflow)[0] == "high"


@pytest.mark.parametrize(
    "diff",
    [
        # A rename whose header cannot be split and that names its sides nowhere else.
        "diff --git a/app/x b/y b/app/z b/w\nsimilarity index 100%\n",
        # A rename whose new side is a quoted name git never writes.
        (
            "diff --git a/app/a.py b/app/b.py\nsimilarity index 100%\n"
            'rename from app/a.py\nrename to "app/\\q.py"\n'
        ),
        # A block cut off after its header.
        _edit("app/views/home.html") + "diff --git\n",
        # Text that is no diff at all, as a proxy's error page would be.
        "<html><body>Service unavailable</body></html>\n",
    ],
)
def test_an_ambiguous_or_unreadable_diff_gives_high_never_medium(
    workflow: Workflow, diff: str
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, diff)

    level, reason = _effort(workflow)

    assert level == "high"
    assert "cannot tell" in reason


def test_a_hunk_line_that_looks_like_a_header_does_not_hide_a_risky_path(
    workflow: Workflow,
) -> None:
    _configure(workflow)
    diff = (
        "diff --git a/docs/notes.md b/docs/notes.md\n"
        "index 1111111..2222222 100644\n"
        "--- a/docs/notes.md\n"
        "+++ b/docs/notes.md\n"
        "@@ -1,2 +1,2 @@\n"
        "---- a/app/views/home.html\n"
        "++++ b/app/views/home.html\n"
        "+rename from x\n"
    ) + _edit("app/auth/login.py")
    _pull_request(workflow, 7, diff)

    level, reason = _effort(workflow)

    assert level == "high"
    assert "app/auth/login.py" in reason


# --- failures: a slow or flaky GitHub, a missing or wrong setting ------------------------------


def test_a_transient_github_error_on_the_diff_is_retried_not_misread(
    workflow: Workflow,
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit("app/views/home.html"))
    real = workflow.bin / "gh-real"
    (workflow.bin / "gh").rename(real)
    workflow.fake(
        "gh",
        "#!/bin/bash\n"
        'if [ "$1 $2" = "pr diff" ] && [ ! -f "@STATE@/failed-once" ]; then\n'
        '  touch "@STATE@/failed-once"; echo "HTTP 502: Bad Gateway" >&2; exit 1\n'
        "fi\n"
        f'exec "{real}" "$@"\n',
    )

    assert _effort(workflow) == ("medium", "")


def _import_review_effort(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> object:
    for name in ("review_effort", "workflow_config", "workflow_commands"):
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(workflow.script.parent))
    for key, value in workflow.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(workflow.repo)
    return importlib.import_module("review_effort")


def test_a_github_that_hangs_gives_high_within_its_deadlines(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit("app/views/home.html"))
    workflow.fake("gh", "#!/bin/bash\nexec sleep 600\n")
    module = _import_review_effort(workflow, monkeypatch)
    commands = importlib.import_module("workflow_commands")
    monkeypatch.setattr(
        module, "github_read", partial(commands.github_read, timeout=1, delays=(0.1,))
    )
    monkeypatch.setattr(sys, "argv", ["review_effort.py", "7"])

    began = time.monotonic()
    code = module.main()  # type: ignore[attr-defined]
    took = time.monotonic() - began

    assert code == 0
    assert took < 10
    # A real hang costs at most every read's timeouts and pauses, well inside the tick's
    # deadline for a whole start.
    worst = (
        2
        * (
            commands.READ_TIMEOUT * (len(commands.READ_RETRY_DELAYS) + 1)
            + sum(commands.READ_RETRY_DELAYS)
        )
        + 30
    )
    assert worst < commands.COMMAND_TIMEOUT


def test_a_hang_prints_high(
    workflow: Workflow,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit("app/views/home.html"))
    workflow.fake("gh", "#!/bin/bash\nexec sleep 600\n")
    module = _import_review_effort(workflow, monkeypatch)
    commands = importlib.import_module("workflow_commands")
    monkeypatch.setattr(
        module, "github_read", partial(commands.github_read, timeout=1, delays=())
    )
    monkeypatch.setattr(sys, "argv", ["review_effort.py", "7"])

    assert module.main() == 0  # type: ignore[attr-defined]

    out = capsys.readouterr()
    assert out.out == "high\n"
    assert "cannot tell" in out.err


@pytest.mark.parametrize(
    "review",
    [
        {"high_effort_paths": "^db/"},
        {"high_effort_paths": ["^db/", ""]},
        {"high_effort_paths": ["(unclosed"]},
        {"high_effort_path": ["^db/"]},
    ],
)
def test_a_wrong_setting_gives_high_not_medium(
    workflow: Workflow, review: object
) -> None:
    _configure(workflow, review)
    _pull_request(workflow, 7, _edit("app/views/home.html"))

    level, reason = _effort(workflow)

    assert level == "high"
    assert "cannot tell" in reason


def test_without_any_shipline_json_an_ordinary_change_gives_medium(
    workflow: Workflow,
) -> None:
    (workflow.repo / "shipline.json").unlink()
    _pull_request(workflow, 7, _edit("app/views/home.html"))

    assert _effort(workflow) == ("medium", "")


# --- security: what reaches a shell ------------------------------------------------------------


@pytest.mark.parametrize(
    "number", ["7;touch hacked", "$(touch hacked)", "-7", "07 08", ""]
)
def test_a_number_that_is_not_one_is_refused_and_runs_nothing(
    workflow: Workflow, number: str
) -> None:
    _pull_request(workflow, 7, _edit("app/views/home.html"))

    done = workflow.run_script("review-effort", number)

    assert done.returncode == 2
    assert not workflow.calls("gh")
    assert not (workflow.repo / "hacked").exists()


def test_a_path_with_shell_text_never_reaches_the_reviewer_command(
    workflow: Workflow,
) -> None:
    _configure(workflow)
    # Git quotes a name with a double quote; the rest stays as written.
    name = 'app/views/$(touch hacked)`touch hacked2`\\".html'
    diff = (
        f'diff --git "a/{name}" "b/{name}"\n'
        "index 1111111..2222222 100644\n"
        f'--- "a/{name}"\n'
        f'+++ "b/{name}"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    _pull_request(workflow, 7, diff)

    done = workflow.run("start", "review", "7")

    assert done.returncode == 0, done.stderr
    assert _started_effort(workflow) == "medium"
    assert not list(workflow.root.rglob("hacked*"))


def test_review_effort_reads_only_and_writes_no_file(workflow: Workflow) -> None:
    _configure(workflow)
    _pull_request(workflow, 7, _edit("app/views/home.html"))
    before = subprocess.run(
        ["git", "-C", str(workflow.repo), "status", "--porcelain", "--ignored"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    _effort(workflow)

    after = subprocess.run(
        ["git", "-C", str(workflow.repo), "status", "--porcelain", "--ignored"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after == before
    assert not [
        call
        for call in workflow.calls("gh")
        if " pr " in f" {call} "
        and any(
            word in call for word in ("edit", "comment", "review ", "merge", "ready")
        )
    ]


# --- the parser against what git itself writes -------------------------------------------------


def test_every_path_of_a_real_git_diff_is_found(
    workflow: Workflow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Renames, copies, deletions, binaries, spaces, ` b/` inside a name and non-ASCII names,
    as git writes them, give exactly the paths `git diff --name-status` gives."""
    module = _import_review_effort(workflow, monkeypatch)
    repo = tmp_path / "example-app"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=example",
                "-c",
                "user.email=example-app@users.noreply.github.com",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    git("init", "-q", "-b", "main")
    files = {
        "app/auth/login.py": "a\n" * 20,
        "app/x b/y.py": "b\n" * 20,
        "db/migrations/0001 init.sql": "c\n" * 20,
        "app/views/home.html": "d\n" * 20,
        "app/prompts/résumé.txt": "e\n" * 20,
        "app/static/logo.png": None,
        "app/old name.py": "f\n" * 20,
    }
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            path.write_bytes(bytes(range(256)) * 4)
        else:
            path.write_text(text)
    git("add", "-A")
    git("commit", "-qm", "base")
    for folder in ("app/legacy", "app/z b", "archive"):
        (repo / folder).mkdir(parents=True, exist_ok=True)
    git("mv", "app/auth/login.py", "app/legacy/login.py")
    git("mv", "app/x b/y.py", "app/z b/w.py")
    git("mv", "db/migrations/0001 init.sql", "archive/0001 init.sql")
    git("rm", "-q", "app/prompts/résumé.txt")
    (repo / "app/static/logo.png").write_bytes(bytes(range(255, -1, -1)) * 4)
    (repo / "app/views/home.html").write_text("d\n" * 19 + "changed\n")
    git("mv", "app/old name.py", "app/new\tname.py")
    git("add", "-A")
    git("commit", "-qm", "change")

    diff = git("diff", "-M", "HEAD~1", "HEAD")
    listed = git("diff", "-M", "--name-status", "-z", "HEAD~1", "HEAD").split("\0")
    expected: set[str] = set()
    at = 0
    while at < len(listed) - 1:
        status = listed[at]
        count = 2 if status[0] in "RC" else 1
        expected.update(listed[at + 1 : at + 1 + count])
        at += 1 + count

    assert set(module.changed_paths(diff)) == expected  # type: ignore[attr-defined]
