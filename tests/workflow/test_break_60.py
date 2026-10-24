"""Breaking tests for pull request 60: repository commands in their own forms.

They replay how an installation names its existing scripts in place, with neutral names: a
conflict module the old gate imported, an advisory module importing a sibling that has the
same name as one of Shipline's, a checkout module, a CI planner whose file name has a dash,
selectors run through an interpreter or a wrapper, and the checks and hunts as they are. Then
the failures such a repository meets: a function that hangs, calls at once, a missing module,
a pull request's worktree with files of the same names."""

import json
import os
import subprocess
import textwrap
import threading
import time
from pathlib import Path

from workflow.test_commands import (  # pyright: ignore[reportPrivateUsage]
    _browser_needed,
    _changes,
    _configure,
    _failing_hook,
    _gate,
    _main_moved,
    _push,
    workflow_config,
)
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow, install_shipline

github = github_fixture


def _write(workflow: Workflow, path: str, source: str) -> str:
    target = workflow.repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(source))
    return path


def _ignored(workflow: Workflow, *patterns: str) -> None:
    """The repository ignores its own module files here, and Python's bytecode, as one does."""
    exclude = Path(
        workflow.git(
            "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
        )
    )
    exclude.write_text(exclude.read_text() + "".join(f"\n{p}" for p in patterns) + "\n")


# The conflict module as the old gate imported it: its own exception, git run in the folder.
_CONFLICTS = """
    import subprocess


    class MechanicalConflict(Exception):
        pass


    def _git(*args):
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout


    def resolve(head: str, main: str) -> list[str]:
        files = _git("diff", "--name-only", "--diff-filter=U").split()
        for path in files:
            if not path.startswith("db/"):
                raise MechanicalConflict(f"{path} is not a numbered file")
            _git("checkout", "--theirs", "--", path)
            _git("add", "--", path)
        return files


    def taken_migrations(head: str, main: str) -> list[str]:
        return [line for line in open("taken.txt").read().split() if line]


    def renumber_migrations(head: str, main: str) -> dict[str, str]:
        taken = taken_migrations(head, main)
        if "db/0002_seven.py" in taken:
            raise MechanicalConflict("db/0002_seven.py: its number is used twice")
        return {}
"""

_CONFLICT_FUNCTIONS = {
    "run": "scripts/workflow/mechanical_conflicts.py",
    "form": "functions",
    "functions": {
        "resolve": "resolve",
        "renumber": "renumber_migrations",
        "taken": "taken_migrations",
    },
}


def test_in_place_conflict_module_resolves_and_lists_taken_numbers(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _write(workflow, "scripts/workflow/mechanical_conflicts.py", _CONFLICTS)
    _configure(workflow, mechanical_conflicts=_CONFLICT_FUNCTIONS)
    worktree = workflow.add_worktree(7)
    (worktree / "taken.txt").write_text("db/0002_seven.py\n")
    done = _gate(
        workflow,
        """
        import json
        taken = g.repository_command("mechanical_conflicts", "taken", "HEAD", "origin/main")
        resolved = g.repository_command("mechanical_conflicts", "resolve", "HEAD", "origin/main")
        print(json.dumps([[taken.returncode, taken.stdout], [resolved.returncode, resolved.stdout]]))
    """,
        worktree,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [[0, "db/0002_seven.py\n"], [0, ""]]


def test_in_place_conflict_module_refusal_keeps_its_message(workflow: Workflow) -> None:
    install_shipline(workflow)
    _write(
        workflow,
        "scripts/workflow/mechanical_conflicts.py",
        _CONFLICTS.replace(
            'files = _git("diff", "--name-only", "--diff-filter=U").split()',
            'files = ["app.ts"]',
        ),
    )
    _configure(workflow, mechanical_conflicts=_CONFLICT_FUNCTIONS)
    done = _gate(
        workflow,
        """
        done = g.repository_command("mechanical_conflicts", "resolve", "HEAD", "origin/main")
        print(done.returncode, g.said(done))
    """,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "1 app.ts is not a numbered file\n"


def test_in_place_renumber_that_cannot_move_a_file_is_left_as_a_conflict(
    github: GitHub,
) -> None:
    """Through the documented form the gate records the conflict, names the file and tells
    the session to renumber it. The same module, named in place, must lead to the same."""
    workflow = github.workflow
    _ignored(
        workflow,
        "scripts/workflow/mechanical_conflicts.py",
        "taken.txt",
        "__pycache__/",
    )
    _write(workflow, "scripts/workflow/mechanical_conflicts.py", _CONFLICTS)
    (workflow.repo / "taken.txt").write_text("db/0002_seven.py\n")
    _configure(workflow, mechanical_conflicts=_CONFLICT_FUNCTIONS)
    _main_moved(github)

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert (
        "main took the number of db/0002_seven.py and it cannot be renumbered mechanically"
    ) in done.stdout + done.stderr


def test_in_place_renumber_moves_nothing_on_a_clean_refresh(github: GitHub) -> None:
    workflow = github.workflow
    _ignored(
        workflow,
        "scripts/workflow/mechanical_conflicts.py",
        "taken.txt",
        "__pycache__/",
    )
    _write(workflow, "scripts/workflow/mechanical_conflicts.py", _CONFLICTS)
    (workflow.repo / "taken.txt").write_text("\n")
    _configure(workflow, mechanical_conflicts=_CONFLICT_FUNCTIONS)
    _main_moved(github)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert (
        workflow.git("log", "-1", "--format=%s") == "chore: test against current main"
    )


# The advisory module imports its sibling `merge_handoff`, which Shipline also has; beside it
# is the repository's own `advisory_watch.py`, an audit timer of its own.
def _advisories(workflow: Workflow) -> None:
    _write(
        workflow,
        "scripts/workflow/merge_handoff.py",
        """
        SERVICES = {"npm": "registry.example.org", "PyPI": "pypi.example.org"}
        def note_down(folder):
            raise AssertionError("Shipline notes the outage itself")
    """,
    )
    _write(
        workflow,
        "scripts/workflow/advisory_watch.py",
        """
        raise AssertionError("the audit timer is not Shipline's advisory_watch")
    """,
    )
    _write(
        workflow,
        "scripts/workflow/advisory_service.py",
        """
        import re
        import merge_handoff
        from merge_handoff import note_down


        def unreachable(output: str) -> str | None:
            for service, host in merge_handoff.SERVICES.items():
                if re.search(rf"getaddrinfo .* {re.escape(host)}", output):
                    return service
            return None


        def answers(service: str) -> bool:
            return service in merge_handoff.SERVICES and service != "PyPI"


        def note_up(folder):
            raise AssertionError("Shipline notes the outage itself")
    """,
    )
    _configure(
        workflow,
        advisory_watch={
            "run": "scripts/workflow/advisory_service.py",
            "form": "functions",
            "functions": {"unreachable": "unreachable", "answers": "answers"},
        },
    )


def test_in_place_advisory_module_reads_an_outage_in_a_push(workflow: Workflow) -> None:
    install_shipline(workflow)
    _advisories(workflow)
    worktree = workflow.add_worktree(7)
    _failing_hook(
        workflow,
        [
            "audit: request failed",
            "getaddrinfo ENOTFOUND registry.example.org",
            "check failed",
        ],
    )
    pushed = _push(workflow, worktree)
    assert pushed["waiting"] == "ServiceWaiting", pushed
    assert "npm's advisory service" in str(pushed["reason"])
    done = _gate(
        workflow,
        """
        import json
        print(json.dumps([g.service_answers("npm"), g.service_answers("PyPI"),
                          g.unreachable_service("no outage here")]))
    """,
        worktree,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [True, False, None]


def _words(workflow: Workflow, name: str) -> list[str]:
    """The command's words as Shipline's own checkout gives them to every caller."""
    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import json, sys; sys.path.insert(0, sys.argv[1]); import workflow_config as c; "
            + f"print(json.dumps(c.load().command({name!r})))",
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(done.stdout)


def test_in_place_advisory_module_ignores_a_worktree_sibling_of_the_same_name(
    workflow: Workflow,
) -> None:
    """A pull request's worktree, the folder the call runs in, may hold files of the same
    names; none of them may run."""
    install_shipline(workflow)
    _advisories(workflow)
    worktree = workflow.add_worktree(7)
    marker = workflow.root / "pull-request-code-ran"
    for name in (
        "merge_handoff.py",
        "re.py",
        "advisory_service.py",
        "push_slots.py",
        "json.py",
        "importlib.py",
        "pathlib.py",
    ):
        (worktree / name).write_text(f"open({str(marker)!r}, 'w').write('{name}')\n")
    done = subprocess.run(
        [*_words(workflow, "advisory_watch"), "answers", "npm"],
        cwd=worktree,
        env=workflow.env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text()


# The checkout module: the old gate called its functions with a Path.
_CHECKOUT = """
    import subprocess
    from pathlib import Path


    def top(where) -> Path:
        return Path(subprocess.run(
            ["git", "-C", str(where), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        ).stdout.strip())


    def install_if_changed(checkout: Path) -> str | None:
        if (checkout / "lock-broken").exists():
            return "the lockfile does not match package.json"
        return None


    def refusal(output: str, checkout: Path) -> str:
        assert top(checkout) == checkout
        failed = [line for line in output.splitlines() if line.startswith("step ")]
        return failed[-1] if failed else ""
"""


def _checkout(workflow: Workflow, source: str = _CHECKOUT) -> None:
    _write(workflow, "scripts/workflow/pushing_checkout.py", source)
    _configure(
        workflow,
        push_checkout={
            "run": "scripts/workflow/pushing_checkout.py",
            "form": "functions",
            "functions": {"install": "install_if_changed", "refusal": "refusal"},
        },
    )


def test_in_place_checkout_module_words_a_refusal_and_a_failed_install(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _checkout(workflow)
    worktree = workflow.add_worktree(7)
    _failing_hook(workflow, ["step lint", "step unit tests", "1 failed"])
    assert _push(workflow, worktree) == {"returncode": 1, "refusal": "step unit tests"}
    (worktree / "lock-broken").touch()
    assert _push(workflow, worktree) == {
        "waiting": "PackagesWaiting",
        "reason": "the lockfile does not match package.json",
    }


def test_in_place_install_that_reports_progress_still_gives_its_reason(
    workflow: Workflow,
) -> None:
    """A function the old gate called in its own process may print what it does; its returned
    string is still the reason of the wait, as the ticket states."""
    install_shipline(workflow)
    _checkout(
        workflow,
        _CHECKOUT.replace(
            '        if (checkout / "lock-broken").exists():',
            '        print(f"installing the packages of {checkout.name}")\n'
            '        if (checkout / "lock-broken").exists():',
        ),
    )
    worktree = workflow.add_worktree(7)
    (worktree / "lock-broken").touch()
    _failing_hook(workflow, ["step lint"])
    pushed = _push(workflow, worktree)
    assert pushed["waiting"] == "PackagesWaiting", pushed
    assert "the lockfile does not match package.json" in str(pushed["reason"]), pushed


def test_in_place_install_whose_installer_warns_still_gives_its_reason(
    workflow: Workflow,
) -> None:
    """The installer the function runs writes its warnings to standard error, as package
    installers do, and the function returns its own reason when it fails."""
    install_shipline(workflow)
    workflow.fake(
        "example-installer",
        "#!/bin/bash\necho 'warn deprecated example-package@1.0.0' >&2\nexit 1\n",
    )
    _checkout(
        workflow,
        _CHECKOUT.replace(
            '        if (checkout / "lock-broken").exists():',
            '        if subprocess.run(["example-installer"], cwd=checkout).returncode:',
        ),
    )
    worktree = workflow.add_worktree(7)
    _failing_hook(workflow, ["step lint"])
    pushed = _push(workflow, worktree)
    assert pushed["waiting"] == "PackagesWaiting", pushed
    assert "the lockfile does not match package.json" in str(pushed["reason"]), pushed


# The CI planner: a script with a dash in its name, run by CI as a program.
_PLANNER = """
    import os
    import subprocess
    import sys

    QUIET = (b".github/workflows/", b"runner/", b"docs/")


    def browser_needed(changed: list[bytes]) -> bool:
        return not all(path.startswith(QUIET) or path.endswith(b".md") for path in changed)


    def main() -> int:
        event = os.environ["GITHUB_EVENT_NAME"]
        changed = subprocess.run(
            ["git", "diff", "--name-only", "-z", "HEAD^1", "HEAD"],
            check=True, capture_output=True,
        ).stdout.split(b"\\0")[:-1]
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"browser={str(browser_needed(changed)).lower()}\\n")
            output.write(f"event={event}\\n")
        return 0


    if __name__ == "__main__":
        sys.exit(main())
"""


def test_in_place_ci_planner_answers_the_gate(workflow: Workflow) -> None:
    install_shipline(workflow)
    _write(workflow, "scripts/e2e-plan.py", _PLANNER)
    workflow.git("add", "scripts/e2e-plan.py")
    workflow.git("commit", "--quiet", "-m", "chore: the planner")
    _configure(
        workflow,
        browser_plan={
            "run": "scripts/e2e-plan.py",
            "form": "functions",
            "functions": {"browser_plan": "browser_needed"},
        },
    )
    _changes(workflow, "docs/example.md", ".github/workflows/ci.yml", "notes/readme.md")
    assert _browser_needed(workflow) == "none"
    _changes(workflow, "docs/example.md", "web/src/example.ts")
    assert _browser_needed(workflow) == "needed"


# The selectors and the checks, named as they already are.


def test_server_selector_through_an_interpreter_reads_the_main_checkout(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    answer = {
        "whole": False,
        "tests": ["backend/tests/test_example.py"],
        "total": 1,
        "reasons": ["changed"],
    }
    workflow.fake(
        "uv",
        "#!/bin/bash\n"
        'printf "%s\\n" "uv $* (in $PWD)" >> "@STATE@/calls.log"\n'
        f"echo {json.dumps(json.dumps(answer))}\n",
    )
    _configure(
        workflow,
        server_tests_for_changes=(
            "uv run --project backend python scripts/workflow/server_tests_for_changes.py"
        ),
    )
    worktree = workflow.add_worktree(7)
    done = workflow.shipline("server-tests-for-changes", "origin/main", cwd=worktree)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == answer
    script = workflow.repo / "scripts/workflow/server_tests_for_changes.py"
    assert workflow.calls("uv") == [
        f"uv run --project backend python {script} origin/main (in {worktree})"
    ]


def _browser_wrapper(workflow: Workflow) -> None:
    """The wrapper answers as a selector does: without `--run`, either the specs a line or,
    with `--ci`, a JSON object, by its own words."""
    workflow.fake(
        "uv",
        "#!/bin/bash\n"
        'printf "%s\\n" "uv $* (in $PWD)" >> "@STATE@/calls.log"\n'
        'if [[ " $* " == *" --ci "* ]]; then\n'
        '  echo \'{"specs": ["e2e/example.spec.ts"], "projects": ["laptop"]}\'\n'
        "else\n"
        "  echo e2e/example.spec.ts\n"
        "fi\n",
    )
    _write(
        workflow,
        "scripts/workflow/browser-tests-for-changes",
        '#!/bin/bash\nexec uv run --project backend python browser_tests_for_changes.py "$@"\n',
    )
    (workflow.repo / "scripts/workflow/browser-tests-for-changes").chmod(0o755)


def test_browser_wrapper_in_lines_and_json_forms_selects_the_same_specs(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _browser_wrapper(workflow)
    worktree = workflow.add_worktree(7)
    files = worktree / "files.txt"
    files.write_text("web/src/example.ts\n")
    answers = []
    for entry in (
        "scripts/workflow/browser-tests-for-changes",
        {"run": "scripts/workflow/browser-tests-for-changes", "form": "lines"},
        {
            "run": "scripts/workflow/browser-tests-for-changes",
            "form": "json",
            "field": "specs",
            "args": ["--ci"],
        },
    ):
        _configure(workflow, browser_tests_for_changes=entry)
        done = workflow.shipline(
            "browser-tests-for-changes",
            "origin/main",
            "--files-from",
            str(files),
            cwd=worktree,
        )
        assert done.returncode == 0, done.stderr
        answers.append(done.stdout)
    assert answers == ["e2e/example.spec.ts\n"] * 3
    assert workflow.calls("uv")[-1] == (
        "uv run --project backend python browser_tests_for_changes.py --ci origin/main "
        f"--files-from {files} (in {worktree})"
    )


def test_checks_and_hunts_named_as_today_keep_their_words(workflow: Workflow) -> None:
    install_shipline(workflow)
    for path in (
        "scripts/workflow/pre-train-checks",
        "scripts/workflow/hunt-app",
        "scripts/workflow/cursor-hunt",
        "scripts/workflow/hunt-browser.mjs",
    ):
        _write(workflow, path, "#!/bin/bash\n")
    _configure(
        workflow,
        pre_train_checks={"run": "scripts/workflow/pre-train-checks", "form": "exit"},
        hunt_app="scripts/workflow/hunt-app",
        hunt_browser={"run": "node scripts/workflow/hunt-browser.mjs", "form": "exit"},
        hunt_cursor="scripts/workflow/cursor-hunt",
    )
    config = workflow_config.load(workflow.repo)
    main = workflow.repo
    assert config.command("pre_train_checks") == [
        str(main / "scripts/workflow/pre-train-checks")
    ]
    assert config.command("hunt_app") == [str(main / "scripts/workflow/hunt-app")]
    assert config.command("hunt_browser") == [
        "node",
        str(main / "scripts/workflow/hunt-browser.mjs"),
    ]
    assert config.command("hunt_cursor") == [str(main / "scripts/workflow/cursor-hunt")]


def test_both_shell_exports_give_a_json_entry_back_word_for_word(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _browser_wrapper(workflow)
    entry = {
        "run": "scripts/workflow/browser-tests-for-changes",
        "form": "json",
        "field": 'it\'s "specs" $(x)',
        "args": ["--ci", "a b", "'quoted'"],
    }
    _configure(workflow, browser_tests_for_changes=entry)
    words = _words(workflow, "browser_tests_for_changes")
    script = workflow.script.parent / "workflow_config.py"
    for kind, shell, show in (
        (
            "shell",
            "bash",
            'printf "%s\\n" "${SHIPLINE_COMMAND_BROWSER_TESTS_FOR_CHANGES[@]}"',
        ),
        (
            "sh",
            "sh",
            'eval "set -- $SHIPLINE_COMMAND_BROWSER_TESTS_FOR_CHANGES"; printf "%s\\n" "$@"',
        ),
    ):
        done = subprocess.run(
            [
                shell,
                "-c",
                f'eval "$(python3 "$1" {kind})" && {show}',
                shell,
                str(script),
            ],
            cwd=workflow.repo,
            env=workflow.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines() == words, kind


# The failures


def test_a_hanging_function_is_stopped_at_the_gate_s_deadline_with_its_children(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    marker = workflow.root / "child.pid"
    _write(
        workflow,
        "scripts/workflow/advisory_service.py",
        f"""
        import subprocess
        def unreachable(output):
            return None
        def answers(service):
            child = subprocess.Popen(["/bin/sleep", "300"])
            open({str(marker)!r}, "w").write(str(child.pid))
            child.wait()
            return True
    """,
    )
    _configure(
        workflow,
        advisory_watch={
            "run": "scripts/workflow/advisory_service.py",
            "form": "functions",
            "functions": {"unreachable": "unreachable", "answers": "answers"},
        },
    )
    started = time.monotonic()
    done = _gate(
        workflow,
        """
        g.COMMAND_TIMEOUT = 2
        try:
            print("answered", g.service_answers("npm"))
        except g.TimedOut as error:
            print("timed out")
    """,
    )
    assert done.stdout == "timed out\n", done.stderr
    assert time.monotonic() - started < 30
    pid = int(marker.read_text())
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    assert state == "Z", f"the function's child {pid} outlived the call"


def test_calls_at_once_each_get_their_own_answer(workflow: Workflow) -> None:
    install_shipline(workflow)
    _write(workflow, "scripts/workflow/mechanical_conflicts.py", _CONFLICTS)
    _configure(workflow, mechanical_conflicts=_CONFLICT_FUNCTIONS)
    worktrees = [workflow.add_worktree(number) for number in range(7, 13)]
    for worktree in worktrees:
        (worktree / "taken.txt").write_text(f"db/{worktree.name}.py\n")
    results: dict[Path, subprocess.CompletedProcess[str]] = {}

    def call(worktree: Path) -> None:
        results[worktree] = _gate(
            workflow,
            """
            done = g.repository_command("mechanical_conflicts", "taken", "HEAD", "origin/main")
            print(done.returncode, done.stdout.strip())
        """,
            worktree,
        )

    threads = [threading.Thread(target=call, args=(w,)) for w in worktrees]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    for worktree in worktrees:
        assert results[worktree].stdout == f"0 db/{worktree.name}.py\n", results[
            worktree
        ].stderr


def test_a_module_missing_from_the_main_checkout_names_its_command(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _configure(workflow, mechanical_conflicts=_CONFLICT_FUNCTIONS)
    done = workflow.shipline("pre-train-checks")
    assert done.returncode != 0
    assert "commands.mechanical_conflicts" in done.stderr
    assert "mechanical_conflicts.py" in done.stderr


def test_a_call_leaves_the_main_checkout_clean_where_bytecode_is_ignored(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _ignored(workflow, "__pycache__/")
    _write(workflow, "scripts/workflow/mechanical_conflicts.py", _CONFLICTS)
    workflow.git("add", "scripts/workflow/mechanical_conflicts.py")
    workflow.git("commit", "--quiet", "-m", "chore: conflict rules")
    _configure(workflow, mechanical_conflicts=_CONFLICT_FUNCTIONS)
    (workflow.repo / "taken.txt").write_text("\n")
    _ignored(workflow, "taken.txt")
    done = _gate(
        workflow,
        """
        done = g.repository_command("mechanical_conflicts", "taken", "HEAD", "origin/main")
        print(done.returncode)
    """,
    )
    assert done.stdout == "0\n", done.stderr
    assert workflow.git("status", "--porcelain") == ""
