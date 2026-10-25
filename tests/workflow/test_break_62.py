"""Breaking tests for pull request 62: the merge gate split into modules by concern.

An installation runs Shipline from its own checkout with its configuration kept outside the
repository it works on. That configuration names three of the repository's own Python modules
as functions (conflicts, advisories, checkout), and those modules import Shipline's
`merge_handoff` by name. The tick, `merge-when-current` and the merge driver call the gate while
the change lands, so every name the gate held must stay importable from it, every moved body
must stay the same, and each daily flow must still work from a fresh process. Names are neutral
(`example-org/example-app`). `break_62_before/merge_gate.txt` is `merge_gate.py` of `main`
before the split.
"""

import ast
import builtins
import fcntl
import json
import os
import subprocess
import symtable
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from workflow.merge_gate_fixtures import GitHub, _rerun_failed_jobs
from workflow.merge_gate_fixtures import github as github_fixture

github = github_fixture

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "workflow"
BEFORE = Path(__file__).resolve().parent / "break_62_before" / "merge_gate.txt"
CONCERNS = (
    "merge_github",
    "merge_checks",
    "merge_health",
    "merge_images",
    "merge_squash",
    "merge_scratch",
    "merge_train",
)
# Every script that imports the gate, by itself or through another.
CALLERS = (
    "merge_driver",
    "tick_lost_check",
    "reviewer_start",
    "check_repair",
    "ci_minutes",
    "merge_train_metrics",
    "flow_page",
    "ci_demand",
    "workflow_metrics",
)


def _before_names() -> set[str]:
    """Every name `merge_gate.py` bound at its top level on main."""
    names: set[str] = set()
    tree = ast.parse(BEFORE.read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        for sub in ast.walk(node) if isinstance(node, (ast.Import, ast.ImportFrom, ast.Try)) else ():
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                names |= {(a.asname or a.name).split(".")[0] for a in sub.names}
    return names


@pytest.fixture
def example(tmp_path: Path) -> dict[str, Any]:
    """A checkout of example-app with an origin, its three function modules in `tools/`, and
    its configuration outside the repository."""
    repo = tmp_path / "example-app"
    origin = tmp_path / "origin.git"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "Example Owner",
        "GIT_AUTHOR_EMAIL": "owner@example.com",
        "GIT_COMMITTER_NAME": "Example Owner",
        "GIT_COMMITTER_EMAIL": "owner@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONPATH": str(SCRIPTS),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def git(*args: str, cwd: Path = repo) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=env)
    repo.mkdir()
    git("init", "-q", "-b", "main")
    git("remote", "add", "origin", "https://github.com/example-org/example-app.git")
    git("config", "remote.origin.pushurl", str(origin))
    tools = repo / "tools"
    tools.mkdir()
    marks = tmp_path / "marks"
    marks.mkdir()
    (tools / "conflicts.py").write_text(
        textwrap.dedent(
            f"""\
            from pathlib import Path

            from merge_handoff import Handoff


            def resolve(head, base):
                return ["shared.txt"]


            def renumber_files(head, base):
                return {{}}


            def taken_files(*words):
                # The note of pull request 7 in the state folder, by merge_handoff's own name.
                return [Handoff.path(Path({str(marks)!r}), "7").name, *words]
            """
        )
    )
    (tools / "advisories.py").write_text(
        textwrap.dedent(
            """\
            import merge_handoff


            def unreachable(text):
                assert merge_handoff.Handoff
                if "could not reach the advisory service of example-registry" in text:
                    return "example-registry"
                return None


            def answers(service):
                return service == "example-registry"
            """
        )
    )
    (tools / "checkout.py").write_text(
        textwrap.dedent(
            f"""\
            from pathlib import Path

            from merge_handoff import write_json


            def install_changed(checkout):
                write_json(Path({str(marks)!r}) / "installed.json", {{"checkout": str(checkout)}})
                return None


            def refusal(text, checkout):
                last = next(line for line in text.splitlines() if line.startswith("lint"))
                return "example-app refused the push: " + last
            """
        )
    )
    (repo / "shared.txt").write_text("original\n")
    git("add", ".")
    git("commit", "-q", "-m", "chore: start")
    git("push", "-q", "origin", "main")
    config = tmp_path / "config" / "example-app"
    config.mkdir(parents=True)
    (config / "shipline.json").write_text(
        json.dumps(
            {
                "repository": "example-org/example-app",
                "commands": {
                    "mechanical_conflicts": {
                        "run": "tools/conflicts.py",
                        "form": "functions",
                        "functions": {
                            "resolve": "resolve",
                            "renumber": "renumber_files",
                            "taken": "taken_files",
                        },
                    },
                    "advisory_watch": {
                        "run": "tools/advisories.py",
                        "form": "functions",
                        "functions": {"unreachable": "unreachable", "answers": "answers"},
                    },
                    "push_checkout": {
                        "run": "tools/checkout.py",
                        "form": "functions",
                        "functions": {"install": "install_changed", "refusal": "refusal"},
                    },
                },
            }
        )
    )
    env["SHIPLINE_CONFIG"] = str(config / "shipline.json")
    return {"repo": repo, "origin": origin, "env": env, "git": git, "marks": marks, "tmp": tmp_path}


def _python(example: dict[str, Any], code: str, *, cwd: Path | None = None) -> Any:
    """Run `code` in a fresh interpreter in the checkout; it prints one JSON answer last."""
    done = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(code)],
        cwd=cwd or example["repo"],
        env=example["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


# The contract: names, bodies and bindings.


def test_every_name_the_gate_held_on_main_is_still_importable_from_it(
    example: dict[str, Any],
) -> None:
    names = sorted(_before_names())
    answer = _python(
        example,
        f"""
        import importlib, json
        import merge_gate
        names = {names!r}
        missing = [name for name in names if not hasattr(merge_gate, name)]
        # Each moved name is the very object its new module holds, not a copy.
        copies = []
        for module in {list(CONCERNS)!r}:
            held = importlib.import_module(module)
            for name in names:
                if name in vars(held) and getattr(merge_gate, name) is not vars(held)[name]:
                    if name not in ("_paused", "_planning_reads"):
                        copies.append(module + "." + name)
        print(json.dumps([missing, copies, len(names)]))
        """,
    )
    missing, copies, count = answer
    assert count > 240
    assert missing == []
    assert copies == []


def test_every_moved_definition_is_the_one_main_had() -> None:
    def strip(tree: ast.AST) -> ast.AST:
        class Imports(ast.NodeTransformer):
            def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
                return None if node.module == "merge_gate" else node

        return Imports().visit(tree)

    def definitions(tree: ast.Module) -> dict[str, str]:
        found = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                found[node.name] = ast.dump(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                found["=" + ast.unparse(target)] = ast.dump(node)
        return found

    before = definitions(ast.parse(BEFORE.read_text()))
    after: dict[str, str] = {}
    twice = []
    for module in ("merge_gate", *CONCERNS):
        tree = strip(ast.parse((SCRIPTS / f"{module}.py").read_text()))
        assert isinstance(tree, ast.Module)
        for name, dumped in definitions(tree).items():
            if name in after:
                twice.append(name)
            after[name] = dumped
    assert twice == []
    assert sorted(set(before) - set(after)) == []
    assert [name for name in before if before[name] != after[name]] == []
    assert sorted(set(after) - set(before)) == ["__getattr__"]


def test_every_global_a_moved_function_reads_is_bound_when_it_runs(
    example: dict[str, Any],
) -> None:
    """A name a rare path reads but nothing binds fails only when that path is taken."""
    modules = ["merge_gate", *CONCERNS]
    loaded = _python(
        example,
        f"""
        import importlib, json
        print(json.dumps({{m: sorted(vars(importlib.import_module(m))) for m in {modules!r}}}))
        """,
    )
    unbound = []
    for module in modules:
        def walk(table: symtable.SymbolTable, where: str) -> None:
            for child in table.get_children():
                here = f"{where}.{child.get_name()}" if where else child.get_name()
                if child.get_type() == symtable.SymbolTableType.FUNCTION:
                    for symbol in child.get_symbols():
                        name = symbol.get_name()
                        if (
                            symbol.is_referenced()
                            and symbol.is_global()
                            and not symbol.is_assigned()
                            and name not in loaded[module]
                            and not hasattr(builtins, name)
                            and not (module == "merge_gate" and name in ("_paused", "_planning_reads"))
                        ):
                            unbound.append(f"{module}:{here}:{name}")
                walk(child, here)

        walk(symtable.symtable((SCRIPTS / f"{module}.py").read_text(), module, "exec"), "")
    assert unbound == []


def test_a_moved_function_reads_the_gate_names_at_call_time() -> None:
    """Callers and tests override the gate's names on `merge_gate`; a moved function that held
    its own binding of one would not see the override. Only names of other modules (the
    standard library, `merge_handoff`) may be held directly."""
    gate_names = _before_names()
    held = []
    for module in CONCERNS:
        source = (SCRIPTS / f"{module}.py").read_text()
        top = ast.parse(source)
        own = {
            node.name
            for node in top.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
        } | {
            n.id
            for node in top.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            for n in ast.walk(t)
            if isinstance(n, ast.Name)
        }

        def walk(table: symtable.SymbolTable, where: str) -> None:
            for child in table.get_children():
                here = f"{where}.{child.get_name()}" if where else child.get_name()
                if child.get_type() == symtable.SymbolTableType.FUNCTION:
                    for symbol in child.get_symbols():
                        name = symbol.get_name()
                        if symbol.is_referenced() and symbol.is_global() and name in gate_names:
                            if name in own and name != "_paused":
                                held.append(f"{module}:{here}:{name}")
                walk(child, here)

        walk(symtable.symtable(source, module, "exec"), "")
    assert held == []


@pytest.mark.parametrize("first", [*CONCERNS, *CALLERS])
def test_each_module_and_caller_imports_first_in_a_fresh_process(
    example: dict[str, Any], first: str
) -> None:
    answer = _python(
        example,
        f"""
        import importlib, json
        importlib.import_module({first!r})
        import merge_gate, merge_train, merge_github, merge_health
        print(json.dumps([
            merge_gate.Train is merge_train.Train,
            merge_gate.Refused is merge_github.Refused,
            merge_gate.Health is merge_health.Health,
            merge_gate.REPOSITORY,
        ]))
        """,
    )
    assert answer == [True, True, True, "example-org/example-app"]


# The two process variables the gate reassigns.


def test_the_lines_planning_reads_reach_every_reader_through_the_gate(
    example: dict[str, Any],
) -> None:
    """While the tick reconciles the line, main's head and pull requests come from its one
    planning read, whichever module asks."""
    base = example["git"]("rev-parse", "main")
    answer = _python(
        example,
        """
        import json
        import merge_gate, merge_train

        class Reads:
            base = "f" * 40
            def api(self, path, read, fresh=False):
                return {"planned": path}

        before = merge_gate._planning_reads
        merge_train._planning_reads = Reads()
        during = [
            merge_gate._planning_reads is merge_train._planning_reads,
            merge_gate.git("ls-remote", "origin", "refs/heads/main"),
            merge_gate.api("repos/example-org/example-app/pulls/7"),
        ]
        merge_train._planning_reads = None
        after = merge_gate._planning_reads
        print(json.dumps([before, during, after]))
        """,
    )
    assert answer == [
        None,
        [True, "f" * 40 + "\trefs/heads/main", {"planned": "repos/example-org/example-app/pulls/7"}],
        None,
    ]
    assert base != "f" * 40


def test_an_unlisted_run_pauses_once_a_process_across_modules(example: dict[str, Any]) -> None:
    answer = _python(
        example,
        """
        import json
        import merge_gate, merge_github

        slept = []
        merge_gate._ci_suites = lambda head: [1]
        merge_gate.api = lambda path, key=None, **given: []
        def command(*words, **given):
            slept.append(words)
            class Done:
                returncode = 0
            return Done()
        merge_gate.command = command
        for _ in range(3):
            merge_github._unlisted_runs("a" * 40, "runs", pull=True, scheduled=False)
        print(json.dumps([slept, merge_gate._paused, merge_github._paused]))
        """,
    )
    assert answer == [[["sleep", "30"]], True, True]


# The installation's daily flows: its function modules, its configuration outside the
# repository.


def test_a_push_runs_the_installations_checkout_and_advisory_modules(
    example: dict[str, Any],
) -> None:
    git = example["git"]
    git("checkout", "-q", "-b", "ticket/example")
    git("commit", "-q", "--allow-empty", "-m", "fix: example")
    hook = example["repo"] / ".git" / "hooks" / "pre-push"
    answer = _python(
        example,
        f"""
        import json
        from pathlib import Path
        import merge_gate

        hook = Path({str(hook)!r})
        folder = merge_gate.main_checkout() / merge_gate.CONFIG.state_folder
        folder.mkdir(parents=True, exist_ok=True)
        out = {{}}
        done = merge_gate.push("origin", "HEAD:refs/heads/ticket/example")
        out["pushed"] = [done.returncode, done.refusal]
        hook.write_text(
            "#!/bin/sh\\necho scanning\\n"
            "echo 'could not reach the advisory service of example-registry'\\nexit 1\\n"
        )
        hook.chmod(0o755)
        try:
            merge_gate.push("origin", "HEAD:refs/heads/ticket/example-two")
        except merge_gate.PushWaiting as error:
            out["advisory"] = [type(error).__name__, str(error)]
        out["down"] = merge_gate.service_down(folder)
        hook.write_text("#!/bin/sh\\necho checking\\necho 'lint: 2 problems'\\nexit 1\\n")
        done = merge_gate.push("origin", "HEAD:refs/heads/ticket/example-three")
        out["refused"] = [done.returncode != 0, done.refusal]
        print(json.dumps(out))
        """,
    )
    assert answer["pushed"] == [0, ""]
    assert answer["advisory"][0] == "ServiceWaiting"
    assert "example-registry" in answer["advisory"][1]
    assert answer["down"]["service"] == "example-registry"
    assert answer["refused"] == [True, "example-app refused the push: lint: 2 problems"]
    installed = json.loads((example["marks"] / "installed.json").read_text())
    assert installed == {"checkout": str(example["repo"])}
    origin = str(example["origin"])
    assert example["git"]("ls-remote", origin, "refs/heads/ticket/example").split()[0] == (
        example["git"]("rev-parse", "HEAD")
    )


def test_the_conflict_module_answers_through_the_gate_with_merge_handoffs_names(
    example: dict[str, Any],
) -> None:
    answer = _python(
        example,
        """
        import json
        import merge_gate, merge_handoff
        done = merge_gate.repository_command("mechanical_conflicts", "taken", "a" * 40, "b" * 40)
        answers = merge_gate.service_answers("example-registry")
        print(json.dumps([done.returncode, done.stdout.splitlines(), answers]))
        """,
    )
    assert answer[0] == 0
    assert answer[1] == ["merge-wanted-7", "a" * 40, "b" * 40]
    assert answer[2] is True


def test_a_missing_configuration_file_stops_the_gate_with_its_path(
    example: dict[str, Any],
) -> None:
    env = {**example["env"], "SHIPLINE_CONFIG": str(example["tmp"] / "gone" / "shipline.json")}
    done = subprocess.run(
        ["bash", str(SCRIPTS / "merge-when-current"), "--train-status"],
        cwd=example["repo"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "gone/shipline.json" in done.stderr
    # The error is the configuration's, not one of a module the split left half loaded.
    assert "ImportError" not in done.stderr and "circular" not in done.stderr
    assert "AttributeError" not in done.stderr


def test_a_missing_function_module_stops_the_gate_with_its_name(
    example: dict[str, Any],
) -> None:
    (example["repo"] / "tools" / "advisories.py").unlink()
    done = subprocess.run(
        ["bash", str(SCRIPTS / "merge-when-current"), "--train-status"],
        cwd=example["repo"],
        env=example["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "tools/advisories.py" in done.stderr
    assert "ImportError" not in done.stderr and "AttributeError" not in done.stderr


# A lost check, as the tick looks for one (`tick_lost_check.py`), and a read that times out.


def _fake_gh(example: dict[str, Any], body: str) -> None:
    folder = example["tmp"] / "bin"
    folder.mkdir(exist_ok=True)
    gh = folder / "gh"
    gh.write_text("#!/bin/sh\n" + body)
    gh.chmod(0o755)
    example["env"]["PATH"] = f"{folder}:{example['env']['PATH']}"


def _lost_pull(started: str) -> dict[str, Any]:
    return {
        "headRefOid": "c" * 40,
        "statusCheckRollup": [
            {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {
                "name": "CI: web",
                "status": "IN_PROGRESS",
                "conclusion": "",
                "startedAt": started,
                "detailsUrl": "https://github.com/example-org/example-app/actions/runs/30/job/31",
            },
        ],
    }


def _tick_lost_check(example: dict[str, Any], pull: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    folder = example["repo"] / ".local" / "workflow"
    folder.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "tick_lost_check.py"), str(folder), "7"],
        cwd=example["repo"],
        env={**example["env"], "WORKFLOW_NOW": "1792969200", "WORKFLOW_BACKGROUND_GITHUB": "1"},
        input=json.dumps(pull),
        capture_output=True,
        text=True,
        timeout=120,
    )


RUN_30 = json.dumps(
    {"id": 30, "status": "completed", "conclusion": "success", "updated_at": "2026-10-15T08:00:00Z"}
)


def test_a_check_github_lost_is_found_and_noted_once_by_the_ticks_lost_check(
    example: dict[str, Any],
) -> None:
    _fake_gh(example, f"echo '{RUN_30}'\n")
    pull = _lost_pull("2026-10-15T07:00:00Z")

    first = _tick_lost_check(example, pull)
    second = _tick_lost_check(example, pull)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    pending = example["repo"] / ".local" / "workflow" / "lost-checks.pending"
    notes = sorted(pending.iterdir())
    assert [note.name for note in notes] == [f"7-{'c' * 40}.json"]
    assert json.loads(notes[0].read_text()) == {
        "number": "7",
        "head": "c" * 40,
        "check": "CI: web",
        "run": 30,
        "conclusion": "success",
    }


def test_a_check_that_started_late_is_not_lost(example: dict[str, Any]) -> None:
    _fake_gh(example, f"echo '{RUN_30}'\n")
    # Started two minutes before now (23:00): it may still be running for real.
    done = _tick_lost_check(example, _lost_pull("2026-10-25T22:58:00Z"))
    assert done.returncode == 3, done.stdout + done.stderr


def test_two_lost_checks_at_once_note_it_once(example: dict[str, Any]) -> None:
    _fake_gh(example, f"sleep 0.5\necho '{RUN_30}'\n")
    pull = _lost_pull("2026-10-15T07:00:00Z")
    results: list[subprocess.CompletedProcess[str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(_tick_lost_check(example, pull)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [done.returncode for done in results] == [0, 0], [d.stderr for d in results]
    pending = example["repo"] / ".local" / "workflow" / "lost-checks.pending"
    assert [note.name for note in pending.iterdir()] == [f"7-{'c' * 40}.json"]


def test_an_unreadable_run_is_no_answer_for_the_lost_check(example: dict[str, Any]) -> None:
    _fake_gh(example, "echo 'HTTP 404: Not Found' >&2\nexit 1\n")
    done = _tick_lost_check(example, _lost_pull("2026-10-15T07:00:00Z"))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "cannot read a run of pull request 7" in done.stderr


def test_a_github_read_that_times_out_is_a_wait_for_the_tick_and_the_driver(
    example: dict[str, Any],
) -> None:
    """`--drive` is the merge driver's call: a wait exits 3, never a refusal."""
    answer = _python(
        example,
        """
        import json, sys
        import merge_gate
        from workflow_commands import CommandTimeout

        def stalled(*words, **given):
            raise CommandTimeout("gh pr view timed out after 30s", "")
        merge_gate.github_read = stalled
        out = []
        try:
            merge_gate.read_pull("7")
        except merge_gate.Waiting as error:
            out.append(["Waiting", str(error)])
        for argv in (["merge-when-current", "7"], ["merge-when-current", "--drive", "7"]):
            sys.argv = argv
            out.append(merge_gate.main())
        print(json.dumps(out))
        """,
    )
    assert answer[0][0] == "Waiting" and "timed out" in answer[0][1]
    assert answer[1:] == [1, 3]


# The merge lock: held too long by another call, and two calls at once.


def test_a_gate_call_waits_for_a_merge_lock_held_by_another_process(
    example: dict[str, Any],
) -> None:
    """The lock is held while the call starts; the call waits and reads nothing on GitHub, then
    goes on once the holder lets it go."""
    folder = example["repo"] / ".local" / "workflow"
    folder.mkdir(parents=True)
    calls = example["tmp"] / "gh-calls"
    _fake_gh(example, f"echo \"$*\" >> '{calls}'\necho 'HTTP 502: Bad Gateway' >&2\nexit 1\n")
    with (folder / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        call = subprocess.Popen(
            ["bash", str(SCRIPTS / "merge-when-current"), "7"],
            cwd=example["repo"],
            env={**example["env"], "WORKFLOW_READ_RETRY_DELAYS": "0"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert call.stdout is not None
            assert call.stdout.readline().strip() == "waiting for the merge lock"
            time.sleep(2)
            assert call.poll() is None
            assert not calls.exists() or "pr view" not in calls.read_text()
        except BaseException:
            call.kill()
            raise
    out, err = call.communicate(timeout=120)
    assert call.returncode == 1, out + err
    assert "merge waiting: cannot read pull request 7" in err
    assert "pr view 7" in calls.read_text()


# Through `merge-when-current` with the suite's stand-in for GitHub.

def test_two_gate_calls_at_once_for_one_pull_request_merge_it_once(github: GitHub) -> None:
    """The tick's driver and the reviewer's own call reach the gate at once. The squash moves
    main, as GitHub does; the call that waited for the lock must not squash on the old main."""
    workflow = github.workflow
    squash = workflow.git(
        "commit-tree", workflow.git("rev-parse", f"{github.head}^{{tree}}"), "-p", github.base,
        "-m", "fix: seven (#7)",
    )
    workflow.git("push", "origin", f"{squash}:refs/heads/prepared-squash")
    github.next_main = squash
    github.save()
    calls = [
        subprocess.Popen(
            ["bash", str(workflow.script.with_name("merge-when-current")), "7"],
            cwd=workflow.repo,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    ends = [call.communicate(timeout=120) for call in calls]
    codes = sorted(call.returncode for call in calls)
    assert len(workflow.calls("gh pr merge")) == 1, ends
    assert codes[0] == 0, ends
    assert codes[1] in (1, 2, 3), ends
    assert (workflow.state / "merged").read_text() == "7"


def test_a_ci_run_re_run_in_part_merges_by_its_carried_jobs(github: GitHub) -> None:
    _rerun_failed_jobs(github, 20, github.head, "CI: what to run")

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
