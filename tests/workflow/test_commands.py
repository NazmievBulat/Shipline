"""A repository plugs its own checks into Shipline by commands named in `shipline.json`.

Each command is a stand-in on the test's `PATH` that writes down its arguments, its working
folder and what it read, and answers as the test says. Each is tried named and left out.
"""

import importlib.util
import json
import re
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest
from workflow.test_conflict_review import (  # pyright: ignore[reportPrivateUsage]
    _panel_conflict,
    _resolve,
)
from workflow.test_merge_driver import (  # pyright: ignore[reportPrivateUsage]
    _mechanical_conflict,
    _started,
)
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow.test_workflow_config import _installation  # pyright: ignore[reportPrivateUsage]
from workflow_harness import Workflow, install_shipline, session_arguments

github = github_fixture
handed = handed_fixture

_WORKFLOW = Path(__file__).resolve().parents[2] / "scripts/workflow"
# Loaded from its file, so the scripts' folder is not put on the path of every other test.
_SPEC = importlib.util.spec_from_file_location("workflow_config", _WORKFLOW / "workflow_config.py")
assert _SPEC and _SPEC.loader
workflow_config = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflow_config)

_COMMANDS = (
    "browser_tests_for_changes",
    "server_tests_for_changes",
    "pre_train_checks",
    "mechanical_conflicts",
    "browser_plan",
    "hunt_app",
    "hunt_browser",
    "hunt_cursor",
    "advisory_watch",
    "push_checkout",
    "push_checks",
)


def _configure(workflow: Workflow, **commands: str | dict | None) -> None:
    """Names `commands` in the main checkout's `shipline.json`; None leaves one out. Git looks
    past the changed file, so a gate that runs in the main checkout finds it clean."""
    path = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    config = json.loads(path.read_text())
    named = config.setdefault("commands", {})
    for name, line in commands.items():
        if line is None:
            named.pop(name, None)
        else:
            named[name] = line
    path.write_text(json.dumps(config))


def _stand_in(workflow: Workflow, program: str, answer: str = "", status: int = 0) -> None:
    """A command on the `PATH` that writes down how it was called, prints `answer` and exits
    with `status`. What it read on standard input is kept in `<state>/<program>.input`."""
    workflow.fake(
        program,
        "#!/bin/bash\n"
        f'printf "%s\\n" "{program} $* (in $PWD)" >> "@STATE@/calls.log"\n'
        f'cat > "@STATE@/{program}.input"\n'
        f"printf %b {json.dumps(answer)}\n"
        f"exit {status}\n",
    )


def _read(workflow: Workflow, program: str) -> str:
    return (workflow.state / f"{program}.input").read_text()


# The file


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "--initial-branch=main"],
        ["remote", "add", "origin", "https://github.com/example-owner/example-app.git"],
        ["-c", "user.name=Tester", "-c", "user.email=tester@example.com", "commit",
         "--quiet", "--allow-empty", "-m", "chore: start"],
    ):  # fmt: skip
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    return repo


def test_without_commands_none_is_named(tmp_path: Path) -> None:
    config = workflow_config.load(_repository(tmp_path))

    assert [config.command(name) for name in _COMMANDS] == [None] * len(_COMMANDS)
    assert workflow_config.as_dict(config)["commands"] == dict.fromkeys(
        ("test", "browser", *_COMMANDS)
    )


def test_a_command_s_program_is_found_in_the_main_checkout_from_a_worktree(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(
        json.dumps(
            {
                "commands": {
                    "pre_train_checks": "tools/shipline/pre-train-checks --fast",
                    "browser_plan": "python3 -B 'tools/shipline/browser plan.py'",
                }
            }
        )
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(worktree)],
        cwd=repo,
        capture_output=True,
        check=True,
    )

    config = workflow_config.load(worktree)

    assert config.command("pre_train_checks") == [
        str(repo / "tools/shipline/pre-train-checks"),
        "--fast",
    ]
    # A program named without a folder is found on the PATH; the script it runs, the first word
    # after it with a folder in it, is the main checkout's, never the pull request's copy.
    assert config.command("browser_plan") == [
        "python3",
        "-B",
        str(repo / "tools/shipline/browser plan.py"),
    ]
    assert config.command("hunt_app") is None
    assert workflow_config.as_dict(config)["commands"]["pre_train_checks"] == (
        "tools/shipline/pre-train-checks --fast"
    )


@pytest.mark.parametrize(
    ("line", "refusal"),
    [
        ("/opt/checks", "must be a path inside the checkout"),
        ("../checks", "must be a path inside the checkout"),
        ("python3 -B ../checks.py", "must be a path inside the checkout"),
        ("tools/'checks", "cannot be split into words"),
        ("   ", "must be a name"),
        (["tools/checks"], "must be a name"),
    ],
)
def test_a_command_that_cannot_be_run_from_the_checkout_is_refused(
    tmp_path: Path, line: object, refusal: str
) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(json.dumps({"commands": {"pre_train_checks": line}}))

    with pytest.raises(workflow_config.ConfigError, match=f"commands.pre_train_checks {refusal}"):
        workflow_config.load(repo)


def test_an_unknown_command_is_refused(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(json.dumps({"commands": {"pre_train_check": "x"}}))

    with pytest.raises(workflow_config.ConfigError, match="unknown key commands.pre_train_check"):
        workflow_config.load(repo)


def test_bash_reads_each_command_as_its_words(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(
        json.dumps({"commands": {"hunt_app": "tools/hunt app --port '8 0' $(touch nope)"}})
    )

    done = subprocess.run(
        [
            "bash",
            "-c",
            'eval "$(python3 "$1" shell "$2")" || exit 1; '
            'printf "%s\\n" "${#SHIPLINE_COMMAND_HUNT_APP[@]}" "${SHIPLINE_COMMAND_HUNT_APP[@]}" '
            '"${#SHIPLINE_COMMAND_PRE_TRAIN_CHECKS[@]}"',
            "bash",
            str(_WORKFLOW / "workflow_config.py"),
            str(repo),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert done.stdout.splitlines() == [
        "6",
        str(repo / "tools/hunt"),
        "app",
        "--port",
        "8 0",
        "$(touch",
        "nope)",
        "0",
    ]
    assert not (repo / "nope").exists()


# The merge gate


def _gate(
    workflow: Workflow, code: str, cwd: Path | None = None, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    """Runs `code` with the gate imported as `g`, in `cwd` (the main checkout when none is
    named), reading `stdin`; it prints what it found."""
    return subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g\n"
            + textwrap.dedent(code),
            str(workflow.script.parent),
        ],
        cwd=cwd or workflow.repo,
        env={**workflow.env, "SHIPLINE_REPO": str(workflow.repo)},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _pre_train_checks(workflow: Workflow, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _gate(
        workflow,
        """
        folder = g.main_checkout() / g.CONFIG.state_folder
        folder.mkdir(parents=True, exist_ok=True)
        try:
            g.pre_train_checks("7", folder)
        except g.Refused as error:
            print("refused:", error)
        else:
            print("passed")
        """,
        cwd,
    )


def test_the_pre_train_checks_are_run_in_the_pull_request_s_worktree(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    _stand_in(workflow, "example-checks", "a check failed\n", status=1)
    _configure(workflow, pre_train_checks="example-checks --fast")

    done = _pre_train_checks(workflow, worktree)

    assert workflow.calls("example-checks") == [f"example-checks --fast (in {worktree})"]
    assert done.stdout == (
        "a check failed\nrefused: pre-train checks failed; "
        "fix the inventory before joining the merge train\n"
    ), done.stderr


def test_a_command_never_reads_the_gate_s_own_input(workflow: Workflow) -> None:
    _stand_in(workflow, "example-checks")
    _configure(workflow, pre_train_checks="example-checks")

    done = _gate(
        workflow,
        """
        folder = g.main_checkout() / g.CONFIG.state_folder
        folder.mkdir(parents=True, exist_ok=True)
        g.pre_train_checks("7", folder)
        """,
        stdin="the gate's own input\n",
    )

    assert done.returncode == 0, done.stderr
    assert _read(workflow, "example-checks") == ""


def test_without_pre_train_checks_a_pull_request_is_admitted(workflow: Workflow) -> None:
    _configure(workflow, pre_train_checks=None)

    done = _pre_train_checks(workflow, workflow.add_worktree(7))

    assert done.stdout == "passed\n", done.stderr
    assert done.stderr == ""


def test_pre_train_checks_named_but_missing_stop_the_gate(workflow: Workflow) -> None:
    _configure(workflow, pre_train_checks="tools/shipline/pre-train-checks")

    done = _pre_train_checks(workflow, workflow.repo)

    assert "refused: cannot run the repository's pre_train_checks command" in done.stdout


@pytest.mark.parametrize("entry,status", [
    ({"run": "example-checks", "form": "exit", "wait_output": "^check could not finish"}, 1),
    ("example-checks", 75),
    ({"run": "example-checks", "form": "exit", "wait_exit": [42]}, 42),
])
def test_pre_train_wait_keeps_the_handoff_then_the_next_tick_merges(
    handed: GitHub, entry: str | dict, status: int,
) -> None:
    workflow = handed.workflow
    folder = workflow.repo / ".local/workflow"
    note = folder / "merge-wanted-7"
    before = note.read_text()
    _configure(workflow, pre_train_checks=entry)
    _stand_in(workflow, "example-checks", "check could not finish\nmore detail\n", status)

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    assert "pre-train checks of 7 wait: check could not finish" in first.stdout
    assert first.stdout.count("pre-train checks of 7 wait:") == 1
    assert note.read_text() == before
    assert "7" in json.loads((folder / "merge-train.json").read_text())["waiting"]
    assert "7" not in json.loads((folder / "merge-train.json").read_text())["approved"]
    assert "merge hand-back" not in first.stdout
    assert _started(workflow) == []
    assert not (workflow.state / "merged").exists()
    _stand_in(workflow, "example-checks", "checks passed\n")

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert not note.exists()
    assert _started(workflow) == []


def test_two_consecutive_pre_train_waits_hand_back_both_outcomes(handed: GitHub) -> None:
    workflow = handed.workflow
    _configure(workflow, pre_train_checks={
        "run": "example-checks", "form": "exit", "wait_output": "^check could not finish",
    })
    _stand_in(workflow, "example-checks", "check could not finish: first\n", 1)
    first = workflow.run_script("tick")
    assert "merge hand-back" not in first.stdout, first.stdout + first.stderr
    _stand_in(workflow, "example-checks", "check could not finish: second\n", 1)

    second = workflow.run_script("tick")

    assert "merge hand-back 7" in second.stdout, second.stdout + second.stderr
    assert "check could not finish: first" in second.stdout
    assert "check could not finish: second" in second.stdout
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert _started(workflow) == ["review-7"]
    assert not (workflow.state / "merged").exists()


def test_a_real_pre_train_failure_is_handed_back_at_once(handed: GitHub) -> None:
    workflow = handed.workflow
    _configure(workflow, pre_train_checks={
        "run": "example-checks", "form": "exit", "wait_output": "^check could not finish",
    })
    _stand_in(workflow, "example-checks", "inventory failed\n", 1)

    done = workflow.run_script("tick")

    assert "merge hand-back 7" in done.stdout, done.stdout + done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert _started(workflow) == ["review-7"]
    assert not (workflow.state / "merged").exists()


def test_handoff_records_a_wait_before_the_tick_retries(handed: GitHub) -> None:
    workflow = handed.workflow
    folder = workflow.repo / ".local/workflow"
    (folder / "merge-wanted-7").unlink()
    train_path = folder / "merge-train.json"
    train = json.loads(train_path.read_text())
    train["waiting"] = []
    train["approved"].pop("7", None)
    train_path.write_text(json.dumps(train))
    _configure(workflow, pre_train_checks="example-checks")
    _stand_in(workflow, "example-checks", "temporarily unavailable\n", 75)

    done = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))

    assert done.returncode == 0, done.stdout + done.stderr
    assert "pre-train checks of 7 wait: temporarily unavailable" in done.stdout
    assert (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert _started(workflow) == []
    _stand_in(workflow, "example-checks")
    tick = workflow.run_script("tick")
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert (workflow.state / "merged").read_text() == "7"


@pytest.mark.parametrize("key,value", [
    ("wait_output", "["), ("wait_output", 1),
    ("wait_exit", [0]), ("wait_exit", [True]), ("wait_exit", [256]),
    ("wait_exit", [-1]), ("wait_exit", "75"),
])
def test_bad_command_wait_options_name_the_key(tmp_path: Path, key: str, value: object) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(json.dumps({"commands": {"pre_train_checks": {
        "run": "example-checks", "form": "exit", key: value,
    }}}))
    with pytest.raises(workflow_config.ConfigError, match="commands.pre_train_checks." + key + " must"):
        workflow_config.load(repo)


@pytest.mark.parametrize("name", ["pre_train_checks", "push_checks", "hunt_app", "hunt_browser", "hunt_cursor"])
@pytest.mark.parametrize("code,answer,options,expected", [
    (1, "try later\n", {"wait_output": "^try later"}, 75),
    (1, "progress\ntry later\n", {"wait_output": "^try later$"}, 75),
    (42, "busy\n", {"wait_exit": [42]}, 75),
    (0, "try later\n", {"wait_output": "^try later"}, 0),
    (75, "failure\n", {"wait_exit": []}, 1),
    (1, "failure\n", {"wait_output": "^try later"}, 1),
])
def test_pass_fail_commands_report_waits_without_changing_output_or_arguments(
    workflow: Workflow, name: str, code: int, answer: str, options: dict, expected: int,
) -> None:
    install_shipline(workflow)
    _stand_in(workflow, "example-checks", answer, code)
    _configure(workflow, **{name: {
        "run": "example-checks", "form": "exit", "args": ["two words", "$(touch nope)"], **options,
    }})

    done = workflow.shipline(name.replace("_", "-"), "--fast")

    assert done.returncode == expected, done.stdout + done.stderr
    assert done.stdout == answer
    assert "two words $(touch nope) --fast" in workflow.calls("example-checks")[0]
    assert not (workflow.repo / "nope").exists()


@pytest.mark.parametrize("name", ["test", "browser"])
def test_test_and_browser_waits_keep_shell_semantics_and_checkout_root(workflow: Workflow, name: str) -> None:
    _stand_in(workflow, "example-checks", "try later\n", 1)
    _configure(workflow, **{name: {
        "run": "EXAMPLE_MODE=check example-checks", "form": "exit", "args": ["two words"],
        "wait_output": "^try later",
    }})
    done = subprocess.run(
        ["python3", str(workflow.script.parent / "ci_proof.py"), name, "--fast"],
        cwd=workflow.repo / "scripts", env=workflow.env, input="", capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert done.returncode == 75, done.stdout + done.stderr
    assert done.stdout == "try later\n"
    assert workflow.calls("example-checks") == [f"example-checks two words --fast (in {workflow.repo})"]


def test_a_wait_diagnostic_on_stderr_can_follow_progress_on_stdout(workflow: Workflow) -> None:
    install_shipline(workflow)
    workflow.fake("example-checks", '#!/bin/bash\necho progress\necho "try later" >&2\nexit 1\n')
    _configure(workflow, pre_train_checks={
        "run": "example-checks", "form": "exit", "wait_output": "^try later$",
    })

    done = workflow.shipline("pre-train-checks")

    assert done.returncode == 75, done.stdout + done.stderr
    assert done.stdout == "progress\n"
    assert done.stderr == "try later\n"


@pytest.mark.parametrize("name,subcommand", [
    ("mechanical_conflicts", "resolve"), ("mechanical_conflicts", "renumber"),
    ("advisory_watch", "answers"), ("push_checkout", "install"),
])
def test_gate_helpers_treat_configured_waits_as_waiting(workflow: Workflow, name: str, subcommand: str) -> None:
    _stand_in(workflow, "example-helper", "try later\n", 1)
    _configure(workflow, **{name: {
        "run": "example-helper --mode", "form": "lines", "wait_output": "^try later",
    }})
    done = _gate(workflow, f'''
        try:
            g.repository_command({name!r}, {subcommand!r})
        except g.Waiting as error:
            print("waiting:", error)
    ''')
    assert done.returncode == 0, done.stderr
    assert f"waiting: {name} waits: try later" in done.stdout


def test_function_helper_waits_can_match_an_exception(workflow: Workflow) -> None:
    module = _module(workflow, '''
        def install(checkout):
            raise RuntimeError("try later")
        def refusal(output, checkout):
            return "try later"
    ''')
    _configure(workflow, push_checkout={
        "run": module, "form": "functions", "wait_output": "^try later",
        "functions": {"install": "install", "refusal": "refusal"},
    })
    done = _gate(workflow, '''
        try:
            g.repository_command("push_checkout", "install", ".")
        except g.Waiting as error:
            print("waiting:", error)
        done = g.repository_command("push_checkout", "refusal", ".", input="failure")
        print("diagnostic:", done.returncode, done.stdout.strip())
    ''')
    assert done.returncode == 0, done.stderr
    assert "waiting: push_checkout waits: try later" in done.stdout
    assert "diagnostic: 0 try later" in done.stdout


@pytest.mark.parametrize("mechanical", [False, True], ids=["renumber", "resolve"])
def test_a_mechanical_helper_wait_leaves_a_clean_reviewed_worktree(github: GitHub, mechanical: bool) -> None:
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.save()
    _configure(workflow, mechanical_conflicts={
        "run": "example-helper", "form": "lines", "wait_output": "^try later",
    })
    _stand_in(workflow, "example-helper", "try later\n", 1)

    done = _gate(workflow, f'''
        folder = g.CONFIG.state()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            g.refresh_pull(g.read_pull("7"), {github.base!r}, g.Train(folder), "7", mechanical={mechanical!r})
        except g.Waiting as error:
            print("waiting:", error)
    ''')

    assert done.returncode == 0, done.stdout + done.stderr
    assert "waiting: mechanical_conflicts waits: try later" in done.stdout
    assert workflow.git("rev-parse", "HEAD") == github.head
    assert workflow.git("status", "--porcelain") == ""
    assert not (workflow.repo / workflow.git("rev-parse", "--git-path", "MERGE_HEAD")).exists()


def test_pre_train_wait_is_reset_by_a_pass_and_by_a_new_head(workflow: Workflow) -> None:
    _configure(workflow, pre_train_checks="example-checks")
    _stand_in(workflow, "example-checks", "try later\n", 75)
    worktree = workflow.add_worktree(7)
    first = _pre_train_checks(workflow, worktree)
    assert "pre-train checks of 7 wait: try later" in first.stdout, first.stderr
    _stand_in(workflow, "example-checks")
    assert _pre_train_checks(workflow, worktree).stdout == "passed\n"
    _stand_in(workflow, "example-checks", "try later\n", 75)
    assert "pre-train checks of 7 wait: try later" in _pre_train_checks(workflow, worktree).stdout
    workflow.git("commit", "--allow-empty", "-m", "fix: a different head", cwd=worktree)
    again = _pre_train_checks(workflow, worktree)
    assert "pre-train checks of 7 wait: try later" in again.stdout, again.stderr
    assert "refused:" not in again.stdout
    second = _pre_train_checks(workflow, worktree)
    third = _pre_train_checks(workflow, worktree)
    assert "could not finish twice" in second.stdout, second.stderr
    assert "could not finish twice" in third.stdout, third.stderr


def _changes(workflow: Workflow, *paths: str) -> None:
    for path in paths:
        (workflow.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (workflow.repo / path).write_text("changed\n")
    workflow.git("add", "-A")
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "feat: a change")


def _browser_needed(workflow: Workflow) -> str:
    done = _gate(workflow, 'print("needed" if g.browser_needed("HEAD~1", "HEAD") else "none")')
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize(
    ("printed", "status", "answer"),
    [
        ("needed\n", 0, "needed"),
        ("none\n", 0, "none"),
        # A crash, Python's exit 1 included, or an answer it does not give never skips them.
        ("", 1, "needed"),
        ("none\n", 1, "needed"),
        ("", 0, "needed"),
        ("no\n", 0, "needed"),
    ],
)
def test_the_browser_plan_reads_the_changed_paths_and_answers_none_only_by_saying_so(
    workflow: Workflow, printed: str, status: int, answer: str
) -> None:
    _stand_in(workflow, "example-plan", printed, status=status)
    _configure(workflow, browser_plan="example-plan --merge")
    _changes(workflow, "app.txt", "odd name.txt")

    assert _browser_needed(workflow) == answer
    assert workflow.calls("example-plan") == [f"example-plan --merge (in {workflow.repo})"]
    assert _read(workflow, "example-plan") == "app.txt\0odd name.txt\0"


@pytest.mark.parametrize("suite", [True, False])
def test_without_a_browser_plan_a_merge_needs_browser_tests_when_there_is_a_suite(
    workflow: Workflow, suite: bool
) -> None:
    """Every merge needs them while the merged tree holds the E2E workflow; without that file
    there is no suite to run."""
    _configure(workflow, browser_plan=None)
    if suite:
        (workflow.repo / ".github/workflows").mkdir(parents=True, exist_ok=True)
        (workflow.repo / ".github/workflows/e2e.yml").write_text("on: workflow_dispatch\n")
    _changes(workflow, "README.md")

    assert _browser_needed(workflow) == ("needed" if suite else "none")


def test_a_merge_that_changes_nothing_asks_no_browser_plan(workflow: Workflow) -> None:
    _stand_in(workflow, "example-plan")
    _configure(workflow, browser_plan="example-plan")
    _changes(workflow)

    assert _browser_needed(workflow) == "none"
    assert workflow.calls("example-plan") == []


def test_the_numbers_main_took_are_asked_of_the_conflict_command(workflow: Workflow) -> None:
    _stand_in(workflow, "example-conflicts", "migrations/0002_taken.py\n")
    _configure(workflow, mechanical_conflicts="example-conflicts")

    done = _gate(workflow, 'print(g._taken_migrations("abc", "def"))')

    assert done.stdout == "['migrations/0002_taken.py']\n", done.stderr
    assert workflow.calls("example-conflicts") == [
        f"example-conflicts taken abc def (in {workflow.repo})"
    ]


def test_a_failed_question_of_the_numbers_main_took_is_a_refusal(workflow: Workflow) -> None:
    _stand_in(workflow, "example-conflicts", "cannot read main\n", status=2)
    _configure(workflow, mechanical_conflicts="example-conflicts")

    done = _gate(
        workflow,
        """
        try:
            g._taken_migrations("abc", "def")
        except g.Refused as error:
            print("refused:", error)
        """,
    )

    assert done.stdout == "refused: could not compare numbered files: cannot read main\n"


def test_without_a_conflict_command_main_took_no_number(workflow: Workflow) -> None:
    _configure(workflow, mechanical_conflicts=None)

    done = _gate(workflow, 'print(g._taken_migrations("abc", "def"))')

    assert done.stdout == "[]\n", done.stderr


def _main_moved(github: GitHub) -> None:
    """Main moves on after pull request 7's CI tested it, so the gate refreshes it."""
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old)


@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.parametrize(
    "paths", [None, [], ["generated/types"], ["generated/ty*"], ["generated/**/st*"],
              ["**/types"]]
)
def test_refresh_clears_only_configured_ignored_output_before_the_push(
    github: GitHub, paths: list[str] | None, subdirectory: bool
) -> None:
    workflow = github.workflow
    _main_moved(github)
    config = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    given = json.loads(config.read_text())
    if paths is not None:
        given["refresh"] = {"clear_generated": paths}
    config.write_text(json.dumps(given))
    exclude = Path(workflow.git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.write_text(exclude.read_text() + "\ngenerated/\n")
    generated = workflow.repo / "generated"
    generated.mkdir()
    (generated / "types").mkdir()
    (generated / "types/stale").write_text("obsolete route\n")
    (generated / "cache").write_text("keep\n")
    other = workflow.worktree(99)
    workflow.git("worktree", "add", "--detach", str(other), "main")
    (other / "generated/types").mkdir(parents=True)
    (other / "generated/types/stale").write_text("another worktree\n")
    hooks = workflow.bin / "generated-hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/bash\nif [[ -e generated/types/stale ]]; then echo 'stale generated output' >&2; exit 1; fi\n")
    hook.chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))

    cwd = workflow.repo
    if subdirectory:
        cwd = workflow.repo / "nested"
        cwd.mkdir()
    github.save()
    done = workflow.run_script("merge-when-current", "7", cwd=cwd)

    assert done.returncode == (2 if paths else 1), done.stdout + done.stderr
    assert (generated / "types/stale").exists() == (not bool(paths))
    assert (generated / "cache").read_text() == "keep\n"
    assert (other / "generated/types/stale").read_text() == "another worktree\n"
    assert (workflow.repo / "shared.txt").read_text() == "original\n"
    if paths:
        assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == workflow.git("rev-parse", "HEAD")
    else:
        assert "stale generated output" in done.stderr


@pytest.mark.parametrize("kind", ["tracked", "not ignored", "outside", "symlink"])
def test_refresh_refuses_unsafe_config_without_deleting_anything(
    github: GitHub, kind: str
) -> None:
    workflow = github.workflow
    _main_moved(github)
    config = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    given = json.loads(config.read_text())
    exclude = Path(workflow.git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.write_text(exclude.read_text() + "\ngenerated/\n")
    generated = workflow.repo / "generated"
    generated.mkdir()
    (generated / "keep").write_text("keep\n")
    named = "generated/keep"
    if kind == "tracked":
        workflow.git("add", "-f", named)
    elif kind == "not ignored":
        named = "shared.txt"
    elif kind == "outside":
        named = "../outside"
        (workflow.root / "outside").write_text("keep\n")
    else:
        outside = workflow.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep\n")
        (generated / "link").symlink_to(outside, target_is_directory=True)
        named = "generated/link/keep"
    given["refresh"] = {"clear_generated": [named]}
    config.write_text(json.dumps(given))

    done = github.run()

    assert done.returncode != 0
    assert "refresh.clear_generated" in done.stderr
    assert (generated / "keep").read_text() == "keep\n"
    assert (workflow.repo / "shared.txt").read_text() == "original\n"
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == github.head
    if kind == "outside":
        assert (workflow.root / "outside").read_text() == "keep\n"
    elif kind == "symlink":
        assert (workflow.root / "outside/keep").read_text() == "keep\n"


def test_a_clean_refresh_asks_the_conflict_command_to_renumber(github: GitHub) -> None:
    workflow = github.workflow
    _stand_in(workflow, "example-conflicts", "db/0002_seven.py db/0003_seven.py\n")
    _configure(workflow, mechanical_conflicts="example-conflicts")
    _main_moved(github)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert workflow.calls("example-conflicts") == [
        f"example-conflicts renumber {github.head} {github.base} (in {workflow.repo})"
    ]
    assert workflow.git("log", "-1", "--format=%s") == (
        "merge main: renumber migration 0002_seven.py to 0003_seven.py"
    )


def test_a_number_main_took_that_cannot_be_moved_is_left_as_a_conflict(github: GitHub) -> None:
    workflow = github.workflow
    workflow.fake(
        "example-conflicts",
        "#!/bin/bash\necho db/0002_seven.py\necho 'its number is used twice' >&2\nexit 1\n",
    )
    _configure(workflow, mechanical_conflicts="example-conflicts")
    _main_moved(github)

    done = github.run()

    assert done.returncode == 1
    assert (
        "main took the number of db/0002_seven.py and it cannot be renumbered mechanically "
        "(its number is used twice); renumber the branch's numbered files after main's last, "
        "commit with 'merge main: conflict in db/0002_seven.py', push, then wait for CI"
    ) in done.stdout + done.stderr
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == github.head


def test_a_conflict_command_that_cannot_renumber_refuses_the_refresh(github: GitHub) -> None:
    workflow = github.workflow
    workflow.fake("example-conflicts", "#!/bin/bash\necho 'cannot read the chain' >&2\nexit 1\n")
    _configure(workflow, mechanical_conflicts="example-conflicts")
    _main_moved(github)

    done = github.run()

    assert done.returncode == 1
    assert "cannot read the chain" in done.stdout + done.stderr
    assert "merge main: conflict in" not in done.stdout + done.stderr


@pytest.mark.parametrize("carries_paths", [True, False])
def test_a_renumber_function_raising_with_paths_leaves_them_as_a_conflict(
    github: GitHub, carries_paths: bool
) -> None:
    """The function form's answer is the documented form's: an exception carrying the files
    it cannot move leaves them as a conflict; one without them refuses the refresh."""
    workflow = github.workflow
    exclude = Path(workflow.git("rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.write_text(exclude.read_text() + "\ntools/forms/\n__pycache__/\n")
    module = _module(workflow, f'''
        class CannotMove(Exception):
            pass
        def renumber_migrations(head, main):
            error = CannotMove("its number is used twice")
            if {carries_paths}:
                error.paths = ["db/0002_seven.py"]
            raise error
        def resolve(head, main):
            return []
        def taken_migrations(head, main):
            return []
    ''')
    _configure(workflow, mechanical_conflicts={
        "run": module, "form": "functions", "functions": {
            "resolve": "resolve", "renumber": "renumber_migrations", "taken": "taken_migrations",
        },
    })
    _main_moved(github)

    done = github.run()

    assert done.returncode == 1
    conflict = (
        "main took the number of db/0002_seven.py and it cannot be renumbered mechanically "
        "(its number is used twice); renumber the branch's numbered files after main's last, "
        "commit with 'merge main: conflict in db/0002_seven.py', push, then wait for CI"
    )
    if carries_paths:
        assert conflict in done.stdout + done.stderr
    else:
        assert "its number is used twice" in done.stdout + done.stderr
        assert "merge main: conflict in" not in done.stdout + done.stderr
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == github.head


def test_without_a_conflict_command_a_clean_refresh_renumbers_nothing(github: GitHub) -> None:
    workflow = github.workflow
    _configure(workflow, mechanical_conflicts=None)
    _main_moved(github)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert workflow.git("log", "-1", "--format=%s") == "chore: test against current main"


# A resolver that takes main's side of every conflicted file, stages it and names it.
_RESOLVER = (
    "#!/bin/bash\n"
    'echo "example-conflicts $* (in $PWD)" >> "@STATE@/calls.log"\n'
    "git diff --name-only --diff-filter=U > \"@STATE@/conflicted\"\n"
    'while IFS= read -r file; do git checkout --theirs -- "$file" && git add -- "$file"; done '
    '< "@STATE@/conflicted"\n'
    'cat "@STATE@/conflicted"\n'
)


def test_a_conflict_the_command_resolves_is_pushed_by_the_tick(handed: GitHub) -> None:
    workflow = handed.workflow
    workflow.fake("example-conflicts", _RESOLVER)
    _configure(workflow, mechanical_conflicts="example-conflicts")
    _mechanical_conflict(handed, "list")
    branch = workflow.worktree(7)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    resolved = [call for call in workflow.calls("example-conflicts") if " resolve " in call]
    assert resolved == [f"example-conflicts resolve {handed.head} {handed.base} (in {branch})"]
    assert workflow.git("log", "-1", "--format=%s", cwd=branch) == (
        "merge main: mechanical conflict in docs/list.md"
    )
    pushed = workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]
    assert pushed == workflow.git("rev-parse", "HEAD", cwd=branch)
    assert _started(workflow) == []


def test_a_conflict_the_command_cannot_resolve_is_left_for_a_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    _stand_in(workflow, "example-conflicts", "two edits of one line\n", status=1)
    _configure(workflow, mechanical_conflicts="example-conflicts")
    _mechanical_conflict(handed, "list")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == ["review-7"]
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head
    assert [call for call in workflow.calls("example-conflicts") if " resolve " in call]


def test_without_a_conflict_command_a_conflict_is_left_for_a_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    _configure(workflow, mechanical_conflicts=None)
    _mechanical_conflict(handed, "list")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == ["review-7"]
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head


def _failing_hook(workflow: Workflow, lines: list[str], *, stall: bool = False) -> None:
    """A pre-push hook that prints `lines` and refuses, or with `stall` sleeps past any deadline;
    it lives in `workflow.bin`, where it may run."""
    hooks = workflow.bin / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    printed = "".join(f"echo {json.dumps(line)}\n" for line in lines)
    end = "exec /bin/sleep 30" if stall else "exit 1"
    hook.write_text(f"#!/bin/bash\n{printed}{end}\n")
    hook.chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))


def _push(
    workflow: Workflow, cwd: Path | None = None, deadline: int | None = None
) -> dict[str, object]:
    """The gate pushes a new branch from `cwd`; what came back, or the wait it raised. A
    `deadline` is the gate's own for this test, shorter than its commands'."""
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "feat: pushed by the gate", cwd=cwd)
    done = _gate(
        workflow,
        (f"\n        g.COMMAND_TIMEOUT = {deadline}" if deadline else "")
        + """
        import json
        try:
            pushed = g.push("origin", "HEAD:refs/heads/ticket/pushed")
        except g.Waiting as error:
            print(json.dumps({"waiting": type(error).__name__, "reason": str(error)}))
        else:
            print(json.dumps({"returncode": pushed.returncode, "refusal": pushed.refusal}))
        """,
        cwd,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# A push checkout that installs nothing and words a refusal by the last line it read.
_PUSH_CHECKOUT = (
    "#!/bin/bash\n"
    'echo "example-checkout $* (in $PWD)" >> "@STATE@/calls.log"\n'
    'case $1 in\n'
    '  install) [[ ! -e "@STATE@/install-fails" ]] || { echo "lockfile is broken"; exit 1; } ;;\n'
    '  refusal) cat > "@STATE@/example-checkout.input"\n'
    '    echo "step: $(grep -m 1 secret "@STATE@/example-checkout.input")" ;;\n'
    "esac\n"
)


def test_the_push_checkout_installs_first_and_words_a_refusal(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.fake("example-checkout", _PUSH_CHECKOUT)
    _configure(workflow, push_checkout="example-checkout")
    _failing_hook(workflow, ["secret found in config.py", "and more"])

    pushed = _push(workflow, worktree / "scripts")

    assert pushed["refusal"] == "step: secret found in config.py"
    # The checkout's top, found with git, from a folder inside it.
    assert workflow.calls("example-checkout") == [
        f"example-checkout install {worktree} (in {worktree / 'scripts'})",
        f"example-checkout refusal {worktree} (in {worktree / 'scripts'})",
    ]
    read = _read(workflow, "example-checkout")
    assert "secret found in config.py\nand more\n" in read and "failed to push" in read


def test_the_push_checkout_words_a_timed_out_push_after_its_deadline(workflow: Workflow) -> None:
    workflow.fake("example-checkout", _PUSH_CHECKOUT)
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, push_checkout="example-checkout", advisory_watch="example-advisory")
    _failing_hook(workflow, ["$ sh scripts/slow-check.sh", "secret check", _UNREACHED], stall=True)

    pushed = _push(workflow, deadline=5)

    # The output read before the stop is worded by the repository; no outage is read in it.
    assert pushed["refusal"] == "git push timed out after 5s\nstep: secret check"
    assert f"secret check\n{_UNREACHED}\n" in _read(workflow, "example-checkout")
    assert workflow.calls("example-advisory") == []
    assert not _outage(workflow)[0].exists()


def test_a_failed_install_of_the_push_checkout_is_a_wait(workflow: Workflow) -> None:
    workflow.fake("example-checkout", _PUSH_CHECKOUT)
    _configure(workflow, push_checkout="example-checkout")
    (workflow.state / "install-fails").touch()

    pushed = _push(workflow)

    assert pushed == {"waiting": "PackagesWaiting", "reason": "lockfile is broken"}
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/pushed") == ""


def test_without_a_push_checkout_a_failed_push_is_refused_with_its_last_lines(
    workflow: Workflow,
) -> None:
    _configure(workflow, push_checkout=None)
    _failing_hook(workflow, [f"step {number}" for number in range(1, 16)])

    pushed = _push(workflow)

    refused = str(pushed["refusal"]).splitlines()
    assert len(refused) == 10 and "step 1" not in refused and "failed to push" in refused[-1]


# What a scan that could not reach an advisory service says.
_UNREACHED = "npm's advisory service could not be reached"
# An advisory watch that names npm when the text says so, and finds npm answering when
# `<state>/npm-answers` is there.
_ADVISORY_WATCH = (
    "#!/bin/bash\n"
    'echo "example-advisory $* (in $PWD)" >> "@STATE@/calls.log"\n'
    'case $1 in\n'
    '  unreachable) cat > "@STATE@/example-advisory.input"\n'
    f'    ! grep -q {json.dumps(_UNREACHED)} "@STATE@/example-advisory.input" || echo npm ;;\n'
    '  answers) [[ -e "@STATE@/$2-answers" ]] ;;\n'
    "esac\n"
)


def test_a_push_the_advisory_watch_finds_unreachable_is_a_wait(workflow: Workflow) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    _failing_hook(workflow, [_UNREACHED])

    pushed = _push(workflow)

    assert pushed["waiting"] == "ServiceWaiting"
    assert "could not reach npm's advisory service" in str(pushed["reason"])
    assert workflow.calls("example-advisory") == [
        f"example-advisory unreachable (in {workflow.repo})"
    ]


def _outage(workflow: Workflow) -> tuple[Path, Path]:
    """The gate's note of a service that is down, and the line the wake tells once."""
    folder = workflow.repo / ".local/workflow"
    return folder / "advisory-service-down", folder / "advisory-service.pending"


def test_the_first_unreachable_push_of_an_outage_notes_it_once(workflow: Workflow) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    _failing_hook(workflow, [_UNREACHED])
    down, pending = _outage(workflow)

    # 2026-10-22 02:46 UTC, then a push a day later.
    first = _gate_at(workflow, 1792637160)
    pending.unlink()
    second = _gate_at(workflow, 1792723560)

    assert first == second == "ServiceWaiting"
    assert json.loads(down.read_text()) == {"service": "npm", "since": "2026-10-22 02:46 UTC"}
    # Told once an outage: the wake removed the line, and the same outage writes none again.
    assert not pending.exists()


def test_an_outage_is_told_to_the_wake_in_one_line(workflow: Workflow) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    _failing_hook(workflow, [_UNREACHED])

    _gate_at(workflow, 1792637160)

    assert _outage(workflow)[1].read_text() == (
        "npm's advisory service unreachable since 2026-10-22 02:46 UTC\n"
    )


@pytest.mark.parametrize("note", ["not json", '{"since": "2026-10-22 02:46 UTC"}', "[]"])
def test_an_unreadable_outage_note_counts_as_none(workflow: Workflow, note: str) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    _failing_hook(workflow, [_UNREACHED])
    down, pending = _outage(workflow)
    down.parent.mkdir(parents=True, exist_ok=True)
    down.write_text(note)

    _gate_at(workflow, 1792637160)

    assert json.loads(down.read_text())["service"] == "npm"
    assert pending.exists()


def test_a_successful_push_ends_the_outage(workflow: Workflow) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    down, _ = _outage(workflow)
    down.parent.mkdir(parents=True, exist_ok=True)
    down.write_text(json.dumps({"service": "npm", "since": "2026-10-22 02:46 UTC"}))

    pushed = _push(workflow)

    assert pushed == {"returncode": 0, "refusal": ""}
    assert not down.exists()


@pytest.mark.parametrize("answers", [True, False])
def test_the_tick_s_refresh_ends_an_outage_once_the_service_answers(
    workflow: Workflow, answers: bool
) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    down, _ = _outage(workflow)
    down.parent.mkdir(parents=True, exist_ok=True)
    down.write_text(json.dumps({"service": "npm", "since": "2026-10-22 02:46 UTC"}))
    if answers:
        (workflow.state / "npm-answers").touch()

    _gate(
        workflow,
        """
        g.git = lambda *args: (_ for _ in ()).throw(g.Refused("no main in this test"))
        g._refresh_health(g.main_checkout() / g.CONFIG.state_folder, retry=True)
        """,
    )

    assert down.exists() is not answers
    assert workflow.calls("example-advisory") == [
        f"example-advisory answers npm (in {workflow.repo})"
    ]


def _gate_at(workflow: Workflow, now: int) -> object:
    """A push the hook refuses, made at `now`; the wait it raised, or what came back."""
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "feat: pushed by the gate")
    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g\n"
            "try:\n"
            "    print(g.push('origin', 'HEAD:refs/heads/ticket/pushed').returncode)\n"
            "except g.Waiting as error:\n"
            "    print(type(error).__name__)\n",
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env={**workflow.env, "SHIPLINE_REPO": str(workflow.repo), "WORKFLOW_NOW": str(now)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_a_failed_push_the_advisory_watch_names_no_service_for_is_refused(
    workflow: Workflow,
) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    _failing_hook(workflow, ["a secret was found"])

    pushed = _push(workflow)

    assert "a secret was found" in str(pushed["refusal"])


def test_without_an_advisory_watch_no_outage_is_read(workflow: Workflow) -> None:
    _configure(workflow, advisory_watch=None)
    _failing_hook(workflow, [_UNREACHED])

    pushed = _push(workflow)

    assert "waiting" not in pushed
    assert _UNREACHED in str(pushed["refusal"])


def test_the_advisory_watch_is_asked_whether_a_service_answers(workflow: Workflow) -> None:
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")

    before = _gate(workflow, 'print(g.service_answers("npm"))')
    (workflow.state / "npm-answers").touch()
    after = _gate(workflow, 'print(g.service_answers("npm"))')

    assert (before.stdout, after.stdout) == ("False\n", "True\n"), before.stderr
    assert workflow.calls("example-advisory") == [
        f"example-advisory answers npm (in {workflow.repo})"
    ] * 2


def test_the_advisory_watch_reads_the_annotations_of_a_failed_audit(github: GitHub) -> None:
    workflow = github.workflow
    workflow.fake("example-advisory", _ADVISORY_WATCH)
    _configure(workflow, advisory_watch="example-advisory")
    github.api["repos/example-org/example-app/check-runs/12/annotations?per_page=100"] = [
        {"title": "Audit", "message": f"Refused because {_UNREACHED}."}
    ]
    github.save()

    done = _gate(
        workflow,
        """
        parts = [
            {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
            {"id": 12, "name": "CI: dependencies", "status": "completed", "conclusion": "failure"},
        ]
        print(g.unreached_service(parts))
        """,
    )

    assert done.stdout == "npm\n", done.stderr
    assert _read(workflow, "example-advisory") == f"Refused because {_UNREACHED}.\n"


def test_without_an_advisory_watch_no_audit_is_read_as_an_outage(github: GitHub) -> None:
    _configure(github.workflow, advisory_watch=None)

    done = _gate(
        github.workflow,
        """
        parts = [
            {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
            {"id": 12, "name": "CI: dependencies", "status": "completed", "conclusion": "failure"},
        ]
        print(g.unreached_service(parts), g.service_answers("npm"))
        """,
    )

    assert done.stdout == "None False\n", done.stderr
    assert not [call for call in github.workflow.calls("gh") if "annotations" in call]


# The review panel's tests reached by a conflict's resolution


def _conflict_round(workflow: Workflow) -> Path:
    """A conflict round of pull request 7 the panel can prove; its panel folder."""
    _panel_conflict(workflow)
    resolved = _resolve(workflow)
    workflow.set_pull_request(7, "OPEN", resolved)
    return workflow.repo / ".local/workflow/reviews/7" / resolved


def test_the_panel_asks_the_repository_which_tests_a_resolution_reaches(
    workflow: Workflow,
) -> None:
    _stand_in(workflow, "example-server-tests", '{"whole": false, "tests": ["tests/test_a.py"]}')
    _stand_in(workflow, "example-browser-tests", "tests/browser/a.spec.ts\n")
    _configure(
        workflow,
        server_tests_for_changes="example-server-tests --quiet",
        browser_tests_for_changes="example-browser-tests",
    )
    root = _conflict_round(workflow)
    head = root.name

    done = workflow.run_script("review-panel", "7")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (root / "server-tests.json").read_text() == (
        '{"whole": false, "tests": ["tests/test_a.py"]}'
    )
    assert (root / "browser-tests.txt").read_text() == "tests/browser/a.spec.ts\n"
    calls = workflow.calls("example-")
    words = f"{head}^1 --files-from .review/conflict-files.txt"
    assert [re.sub(r"\(in .*/inputs\.\w+\)$", "(in inputs)", call) for call in calls] == [
        f"example-server-tests --quiet {words} (in inputs)",
        f"example-browser-tests {words} (in inputs)",
    ]


def test_without_test_selections_the_panel_says_the_whole_suites_run(workflow: Workflow) -> None:
    _configure(workflow, server_tests_for_changes=None, browser_tests_for_changes=None)
    root = _conflict_round(workflow)

    done = workflow.run_script("review-panel", "7")

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads((root / "server-tests.json").read_text()) == {
        "whole": True,
        "tests": [],
        "total": 0,
        "reasons": ["no server test selection is configured"],
    }
    assert (root / "browser-tests.txt").read_text() == ""
    assert (root / "task.md").read_text().endswith(
        "\nNo browser test selection is configured: CI runs the whole browser suite.\n"
    )


def test_a_test_selection_that_fails_stops_the_panel(workflow: Workflow) -> None:
    _stand_in(workflow, "example-server-tests", "cannot read the tests", status=1)
    _configure(workflow, server_tests_for_changes="example-server-tests")
    _conflict_round(workflow)

    done = workflow.run_script("review-panel", "7")

    assert done.returncode == 1
    assert "could not select tests for the conflict resolution" in done.stderr


# Hunts


def test_without_a_hunt_app_a_hunt_is_refused_in_a_sentence(workflow: Workflow) -> None:
    _configure(workflow, hunt_app=None)

    done = workflow.run("start", "hunt", "phone-1")

    assert done.returncode == 1
    assert done.stderr.strip() == (
        f"this repository names no hunt_app command in {workflow.repo / 'shipline.json'}, so a hunt has no app to use"
    )
    assert not workflow.hunt_worktree("phone-1").exists()
    assert not workflow.calls("tmux")


def test_without_a_cursor_hunter_a_cursor_hunt_is_refused_in_a_sentence(
    workflow: Workflow,
) -> None:
    _stand_in(workflow, "example-hunt-app")
    _configure(workflow, hunt_app="example-hunt-app", hunt_cursor=None)

    done = workflow.run("start", "hunt", "phone-1", "plan", "cursor")

    assert done.returncode == 1
    assert done.stderr.strip() == (
        f"this repository names no hunt_cursor command in {workflow.repo / 'shipline.json'}, so it has no Cursor hunter"
    )
    assert not workflow.calls("tmux")


def _hunt_command(workflow: Workflow, name: str) -> str:
    """The command line a hunt's `tmux` session was started with."""
    return session_arguments(workflow.state, name)[-1]


def test_a_cursor_hunt_runs_the_repository_s_cursor_hunter(workflow: Workflow) -> None:
    _stand_in(workflow, "example-hunt-app")
    _configure(workflow, hunt_app="example-hunt-app", hunt_cursor="example-cursor --sandbox")

    done = workflow.run("start", "hunt", "phone-1", "plan", "cursor")

    worktree = workflow.hunt_worktree("phone-1")
    report = workflow.hunt_report("phone-1")
    command = _hunt_command(workflow, "hunt-phone-1")
    assert command.startswith("example-cursor --sandbox "), done.stdout + done.stderr
    words = shlex.split(command.split(" 2>&1 | tee ")[0])
    assert words == [
        "example-cursor",
        "--sandbox",
        str(worktree),
        str(report),
        "Hunt for bugs with a plan, as hunt-phone-1, "
        "by the section Hunt for bugs of AGENTS.md in this worktree",
    ]
    assert command.endswith(f" 2>&1 | tee {report}/cursor.log")


@pytest.mark.parametrize("browser", [None, "tools/shipline/hunt-browser.mjs"])
def test_a_hunter_is_told_to_drive_the_browser_with_the_repository_s_command(
    workflow: Workflow, browser: str | None
) -> None:
    _stand_in(workflow, "example-hunt-app")
    _configure(workflow, hunt_app="example-hunt-app", hunt_browser=browser)

    done = workflow.run("start", "hunt", "phone-1")

    assert done.returncode == 0, done.stdout + done.stderr
    sentence = (
        "Hunt for bugs with a plan, as hunt-phone-1, "
        "by the section Hunt for bugs of AGENTS.md in this worktree"
    )
    if browser:
        sentence += "; drive the browser with shipline hunt-browser"
    assert _hunt_command(workflow, "hunt-phone-1").endswith(f'"{sentence}"')


def test_a_stopped_hunt_s_application_is_stopped_by_the_repository_s_command(
    workflow: Workflow,
) -> None:
    _stand_in(workflow, "example-hunt-app")
    _configure(workflow, hunt_app="example-hunt-app --quiet")
    workflow.add_hunt("phone-1")
    workflow.running("hunt-phone-1")

    done = workflow.run("stop", "hunt-phone-1")

    assert done.returncode == 0, done.stdout + done.stderr
    # In the hunt's worktree, where its hunter started the application.
    assert workflow.calls("example-hunt-app") == [
        f"example-hunt-app --quiet stop (in {workflow.hunt_worktree('phone-1')})"
    ]


def test_without_a_hunt_app_a_stopped_hunt_stops_no_application(workflow: Workflow) -> None:
    _stand_in(workflow, "example-hunt-app")
    _configure(workflow, hunt_app=None)
    workflow.add_hunt("phone-1")
    workflow.running("hunt-phone-1")

    done = workflow.run("stop", "hunt-phone-1")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not workflow.is_running("hunt-phone-1")
    assert workflow.calls("example-hunt-app") == []


# `shipline <command>`, as a session runs the repository's commands


def test_shipline_runs_the_repository_s_command_by_its_name(workflow: Workflow) -> None:
    install_shipline(workflow)
    _stand_in(workflow, "example-selection", "tests/browser/a.spec.ts\n")
    _configure(workflow, browser_tests_for_changes="example-selection --local")
    worktree = workflow.add_worktree(7)

    done = workflow.shipline(
        "browser-tests-for-changes",
        "origin/main",
        "--run",
        cwd=worktree,
        env={"SHIPLINE_REPO": str(workflow.repo)},
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == "tests/browser/a.spec.ts\n"
    assert workflow.calls("example-selection") == [
        f"example-selection --local origin/main --run (in {worktree})"
    ]


@pytest.mark.parametrize("name", ["browser-tests-for-changes", "server-tests-for-changes"])
def test_without_a_selection_shipline_selects_nothing_and_says_the_whole_suite_runs(
    workflow: Workflow, name: str
) -> None:
    install_shipline(workflow)

    done = workflow.shipline(name, "origin/main")

    key = name.replace("-", "_")
    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr == (
        f"this repository names no {key} command in {workflow.repo / 'shipline.json'}: "
        "no test is selected, and CI runs the whole suite\n"
    )


def test_without_a_command_shipline_says_the_repository_has_none(workflow: Workflow) -> None:
    install_shipline(workflow)

    done = workflow.shipline("hunt-browser", "open", "/")

    assert done.returncode == 1
    assert done.stderr == f"this repository names no hunt_browser command in {workflow.repo / 'shipline.json'}\n"


def test_shipline_knows_every_command_a_repository_may_name() -> None:
    launcher = (_WORKFLOW.parents[1] / "bin" / "shipline").read_text()
    listed = launcher.split("CONFIGURED=(\n", 1)[1].split(")", 1)[0].split()

    assert listed == [name.replace("_", "-") for name in _COMMANDS]


def test_without_pre_train_checks_shipline_has_none_to_fail(workflow: Workflow) -> None:
    install_shipline(workflow)
    _configure(workflow, pre_train_checks=None)

    done = workflow.shipline("pre-train-checks")

    assert done.returncode == 0
    assert done.stderr == (
        f"this repository names no pre_train_checks command in {workflow.repo / 'shipline.json'}: "
        "there is no check to fail\n"
    )


# Existing repository scripts can answer in their own form.


def _module(workflow: Workflow, source: str, name: str = "command.py") -> str:
    folder = workflow.repo / "tools" / "forms"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(textwrap.dedent(source))
    return f"tools/forms/{name}"


def test_function_conflicts_match_the_documented_answers(workflow: Workflow) -> None:
    module = _module(workflow, '''
        import subprocess
        def resolve(head, main):
            assert (head, main) == ("HEAD", "origin/main")
            subprocess.run(["git", "add", "resolved.txt"], check=True)
            return ["resolved.txt"]
        def renumber_migrations(head, main):
            if head == "broken":
                raise RuntimeError("cannot move db/0002_example.py")
            return {"db/0002_example.py": "db/0003_example.py"}
        def taken_migrations(head, main):
            return ["db/0002_example.py"]
    ''')
    _configure(workflow, mechanical_conflicts={
        "run": module, "form": "functions", "functions": {
            "resolve": "resolve", "renumber": "renumber_migrations", "taken": "taken_migrations",
        },
    })
    worktree = workflow.add_worktree(7)
    (worktree / "resolved.txt").write_text("resolved\n")
    # A pull request's copy must never supply the functions.
    (worktree / module).parent.mkdir(parents=True, exist_ok=True)
    (worktree / module).write_text("raise RuntimeError('untrusted copy')\n")
    done = _gate(workflow, '''
        import json
        answers = []
        for sub, head in [("resolve", "HEAD"), ("renumber", "HEAD"),
                          ("taken", "HEAD"), ("renumber", "broken")]:
            done = g.repository_command("mechanical_conflicts", sub, head, "origin/main")
            answers.append([done.returncode, done.stdout, done.stderr])
        print(json.dumps(answers))
    ''', worktree)
    assert done.returncode == 0, done.stderr
    answers = json.loads(done.stdout)
    assert answers[:3] == [
        [0, "resolved.txt\n", ""],
        [0, "db/0002_example.py db/0003_example.py\n", ""],
        [0, "db/0002_example.py\n", ""],
    ]
    assert answers[3] == [1, "", "cannot move db/0002_example.py\n"]
    assert workflow.git("diff", "--cached", "--name-only", cwd=worktree) == "resolved.txt"


def test_function_advisories_import_their_own_sibling_first(workflow: Workflow) -> None:
    _module(workflow, 'SERVICE = "example-service"\n', "merge_handoff.py")
    module = _module(workflow, '''
        import merge_handoff
        def unreachable(output):
            return merge_handoff.SERVICE if output == "down\\n" else None
        def answers(service):
            return service == merge_handoff.SERVICE
    ''')
    _configure(workflow, advisory_watch={
        "run": module, "form": "functions",
        "functions": {"unreachable": "unreachable", "answers": "answers"},
    })
    done = _gate(workflow, '''
        import json
        print(json.dumps([g.unreachable_service("down\\n"), g.unreachable_service("ok"),
                          g.service_answers("example-service"), g.service_answers("other")]))
    ''')
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["example-service", None, True, False]


@pytest.mark.parametrize("home", [False, True])
@pytest.mark.parametrize("qualified", [False, True])
def test_function_module_is_found_in_the_installation_folder(
    workflow: Workflow, qualified: bool, home: bool
) -> None:
    """A module is found as a program is: main first, then the installation folder, which is
    a config's own folder when it is kept outside the repository."""
    entry = {
        "run": ("@install/" if qualified else "") + "tools/advisories.py", "form": "functions",
        "functions": {"unreachable": "unreachable", "answers": "answers"},
    }
    if home:
        path = _installation(workflow)
        installed = path.parent
        path.write_text(json.dumps({"commands": {"advisory_watch": entry}}))
    else:
        installed = workflow.root / "installation"
        _configure(workflow, advisory_watch=entry)
        path = workflow.repo / "shipline.json"
        path.write_text(
            json.dumps({**json.loads(path.read_text()), "install_folder": str(installed)})
        )
    (installed / "tools").mkdir(parents=True)
    (installed / "tools" / "advisories.py").write_text(textwrap.dedent('''
        def unreachable(output):
            return "example-service" if output == "down\\n" else None
        def answers(service):
            return service == "example-service"
    '''))
    done = _gate(workflow, '''
        import json
        print(json.dumps([g.unreachable_service("down\\n"), g.service_answers("example-service")]))
    ''')
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["example-service", True]


def test_function_checkout_converts_paths_and_orders_refusal_arguments(workflow: Workflow) -> None:
    module = _module(workflow, '''
        from pathlib import Path
        def install_if_changed(checkout):
            assert isinstance(checkout, Path)
            print("installing the changed packages")
            return "lockfile is broken" if (checkout / "broken-install").exists() else None
        def refusal(output, checkout):
            assert isinstance(checkout, Path)
            assert (checkout / "scripts").is_dir()
            return "step: " + next(line for line in output.splitlines() if line == "check failed")
    ''')
    _configure(workflow, push_checkout={
        "run": module, "form": "functions",
        "functions": {"install": "install_if_changed", "refusal": "refusal"},
    })
    worktree = workflow.add_worktree(7)
    _failing_hook(workflow, ["check failed"])
    assert _push(workflow, worktree) == {"returncode": 1, "refusal": "step: check failed"}
    (worktree / "broken-install").touch()
    pushed = _push(workflow, worktree)
    assert pushed["waiting"] == "PackagesWaiting", pushed
    assert pushed["reason"].splitlines()[-1] == "lockfile is broken", pushed


@pytest.mark.parametrize("path, needed", [("docs/example.md", False), ("web/example.ts", True)])
def test_function_browser_plan_reads_byte_paths(workflow: Workflow, path: str, needed: bool) -> None:
    module = _module(workflow, '''
        def browser_needed(changed):
            assert all(isinstance(path, bytes) for path in changed)
            return not all(path.endswith(b".md") for path in changed)
    ''')
    # Put the planner in the baseline, so its own source is not part of the measured change.
    workflow.git("add", module)
    workflow.git("commit", "--quiet", "-m", "chore: prepare browser planner")
    _configure(workflow, browser_plan={
        "run": module, "form": "functions", "functions": {"browser_plan": "browser_needed"},
    })
    _changes(workflow, path)
    assert _browser_needed(workflow) == ("needed" if needed else "none")


def test_function_browser_plan_preserves_non_utf8_paths(workflow: Workflow) -> None:
    module = _module(workflow, '''
        def browser_needed(changed):
            assert changed == [b"web/\\xff.ts"]
            return False
    ''')
    _configure(workflow, browser_plan={
        "run": module, "form": "functions", "functions": {"browser_plan": "browser_needed"},
    })
    done = _gate(workflow, '''
        done = g.repository_command("browser_plan", input="web/\\udcff.ts\\0", errors="surrogateescape")
        print(done.returncode, done.stdout.strip())
    ''')
    assert done.returncode == 0, done.stderr
    assert done.stdout == "0 none\n"


def test_json_selectors_and_leading_arguments_reach_the_panel(workflow: Workflow) -> None:
    _stand_in(workflow, "example-server-tests", json.dumps({
        "all": False, "selected": ["tests/test_a.py"], "count": 3, "why": ["changed"],
    }))
    _stand_in(workflow, "example-browser-tests", '{"specs": ["tests/browser/a.spec.ts"]}')
    _configure(workflow,
        server_tests_for_changes={
            "run": "example-server-tests", "form": "json", "args": ["--mode", "test selection"],
            "fields": {"whole": "all", "tests": "selected", "total": "count", "reasons": "why"},
        },
        browser_tests_for_changes={
            "run": "example-browser-tests", "form": "json", "field": "specs",
        },
    )
    root = _conflict_round(workflow)
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads((root / "server-tests.json").read_text()) == {
        "whole": False, "tests": ["tests/test_a.py"], "total": 3, "reasons": ["changed"],
    }
    assert (root / "browser-tests.txt").read_text() == "tests/browser/a.spec.ts\n"
    assert "--mode test selection " + root.name + "^1 --files-from" in workflow.calls("example-server-tests")[0]


@pytest.mark.parametrize("form", ["lines", "exit"])
def test_explicit_forms_preserve_output_exit_and_literal_arguments(workflow: Workflow, form: str) -> None:
    install_shipline(workflow)
    _stand_in(workflow, "example-command", "check output\n", status=3)
    name = "browser_tests_for_changes" if form == "lines" else "pre_train_checks"
    _configure(workflow, **{name: {
        "run": "example-command --first", "form": form, "args": ["$(touch nope)", "two words"],
    }})
    done = workflow.shipline(name.replace("_", "-"), "origin/main")
    assert done.returncode == 3, done.stderr
    assert done.stdout == "check output\n"
    assert "--first $(touch nope) two words origin/main" in workflow.calls("example-command")[0]
    assert not (workflow.repo / "nope").exists()


@pytest.mark.parametrize("answer", ['{"specs": "wrong"}', '{"other": []}', 'not json'])
def test_bad_json_selection_is_a_failure(workflow: Workflow, answer: str) -> None:
    install_shipline(workflow)
    _stand_in(workflow, "example-browser-tests", answer)
    _configure(workflow, browser_tests_for_changes={
        "run": "example-browser-tests", "form": "json", "field": "specs",
    })
    done = workflow.shipline("browser-tests-for-changes", "origin/main")
    assert done.returncode != 0
    assert "browser_tests_for_changes" in done.stderr


@pytest.mark.parametrize("name, entry", [
    ("pre_train_checks", {"run": "example-check", "form": "json"}),
    ("browser_plan", {"run": "example-check", "form": "exit"}),
    ("hunt_app", {"run": "example-check", "form": "functions", "functions": {}}),
    ("browser_tests_for_changes", {"run": "example-check", "form": "json"}),
    ("server_tests_for_changes", {"run": "example-check", "form": "json", "fields": {"unknown": "all"}}),
    ("pre_train_checks", {"run": "example-check", "form": "exit", "args": "--fast"}),
    ("pre_train_checks", {"run": "example-check", "form": "exit", "extra": True}),
    ("pre_train_checks", {"run": "", "form": "exit"}),
])
def test_unsupported_or_malformed_forms_name_the_command(tmp_path: Path, name: str, entry: dict) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(json.dumps({"commands": {name: entry}}))
    with pytest.raises(workflow_config.ConfigError, match=f"commands.{name}"):
        workflow_config.load(repo)


def test_missing_function_is_refused_without_executing_the_module(workflow: Workflow) -> None:
    module = _module(workflow, '''
        from pathlib import Path
        Path("executed").touch()
        def unreachable(output):
            return None
    ''')
    _configure(workflow, advisory_watch={
        "run": module, "form": "functions",
        "functions": {"unreachable": "unreachable", "answers": "missing"},
    })
    with pytest.raises(workflow_config.ConfigError, match="commands.advisory_watch.*missing"):
        workflow_config.load(workflow.repo)
    assert not (workflow.repo / "executed").exists()


def test_test_command_object_keeps_shell_semantics_and_literal_leading_args(workflow: Workflow) -> None:
    _stand_in(workflow, "example-tests", "suite output\n", status=4)
    _configure(workflow, test={
        "run": "EXAMPLE_MODE=check example-tests", "form": "exit", "args": ["two words"],
    })
    done = subprocess.run(
        ["python3", str(workflow.script.parent / "ci_proof.py"), "test", "--quiet"],
        cwd=workflow.repo, env=workflow.env, input="", capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert done.returncode == 4, done.stderr
    assert done.stdout == "suite output\n"
    assert "example-tests two words --quiet" in workflow.calls("example-tests")[0]


@pytest.mark.parametrize("answer", [
    '{"all": "yes", "selected": [], "count": 0, "why": []}',
    '{"all": false, "selected": "tests/test_a.py", "count": 0, "why": []}',
    '{"all": false, "selected": [], "count": true, "why": []}',
    '{"all": false, "selected": [], "count": -1, "why": []}',
    '{"all": false, "selected": [], "count": 0, "why": [1]}',
])
def test_bad_server_json_is_a_failure(workflow: Workflow, answer: str) -> None:
    install_shipline(workflow)
    _stand_in(workflow, "example-server-tests", answer)
    _configure(workflow, server_tests_for_changes={
        "run": "example-server-tests", "form": "json",
        "fields": {"whole": "all", "tests": "selected", "total": "count", "reasons": "why"},
    })
    done = workflow.shipline("server-tests-for-changes", "origin/main")
    assert done.returncode != 0
    assert "server_tests_for_changes" in done.stderr


def test_json_selector_failure_preserves_its_output_and_status(workflow: Workflow) -> None:
    install_shipline(workflow)
    _stand_in(workflow, "example-browser-tests", "selection failed\n", status=7)
    _configure(workflow, browser_tests_for_changes={
        "run": "example-browser-tests", "form": "json", "field": "specs",
    })
    done = workflow.shipline("browser-tests-for-changes", "origin/main")
    assert done.returncode == 7
    assert done.stdout == "selection failed\n"


@pytest.mark.parametrize("result", ["None", "'none'", "1"])
def test_function_browser_plan_invalid_return_never_skips_tests(workflow: Workflow, result: str) -> None:
    module = _module(workflow, f'def browser_needed(changed):\n    return {result}\n')
    _configure(workflow, browser_plan={
        "run": module, "form": "functions", "functions": {"browser_plan": "browser_needed"},
    })
    _changes(workflow, "web/example.ts")
    assert _browser_needed(workflow) == "needed"


def test_function_module_cannot_escape_the_main_checkout(workflow: Workflow) -> None:
    outside = workflow.root / "outside.py"
    outside.write_text("def browser_needed(changed):\n    return False\n")
    module = _module(workflow, "")
    path = workflow.repo / module
    path.unlink()
    path.symlink_to(outside)
    _configure(workflow, browser_plan={
        "run": module, "form": "functions", "functions": {"browser_plan": "browser_needed"},
    })
    with pytest.raises(workflow_config.ConfigError, match="commands.browser_plan"):
        workflow_config.load(workflow.repo)


@pytest.mark.parametrize("form", ["json", "functions"])
def test_argument_only_calls_finish_with_standard_input_still_open(workflow: Workflow, form: str) -> None:
    if form == "json":
        workflow.fake("example-selection", '#!/bin/bash\necho \'{"specs": []}\'\n')
        name, arguments = "browser_tests_for_changes", ["origin/main"]
        entry = {"run": "example-selection", "form": "json", "field": "specs"}
    else:
        module = _module(workflow, '''
            def unreachable(output):
                return None
            def answers(service):
                return True
        ''', "advisories")
        name, arguments = "advisory_watch", ["answers", "example-service"]
        entry = {"run": module, "form": "functions", "functions": {
            "unreachable": "unreachable", "answers": "answers",
        }}
    _configure(workflow, **{name: entry})
    words = workflow_config.load(workflow.repo).command(name)
    process = subprocess.Popen(
        [*words, *arguments], cwd=workflow.repo, env=workflow.env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # Do not close stdin until the call exits: these calls take only argv arguments.
        assert process.wait(timeout=10) == 0
        output, error = process.communicate(timeout=10)
        assert output == "", error
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("result, status, output", [
    ("None", 1, ""), ("False", 1, ""), ("True", 0, ""), ("'available'", 0, "available\n"),
])
def test_function_answers_keeps_return_conversion(
    workflow: Workflow, result: str, status: int, output: str,
) -> None:
    module = _module(workflow, f'''
        def unreachable(output):
            return None
        def answers(service):
            return {result}
    ''')
    _configure(workflow, advisory_watch={
        "run": module, "form": "functions",
        "functions": {"unreachable": "unreachable", "answers": "answers"},
    })
    done = _gate(workflow, '''
        import json
        done = g.repository_command("advisory_watch", "answers", "example-service")
        print(json.dumps([done.returncode, done.stdout, done.stderr]))
    ''')
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [status, output, ""]
