"""`shipline ci-tests-for-changes` chooses the tests CI will run for a change, and a cloud
worker's push runs them first (`ci_tests_for_changes.py`)."""

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow, install_shipline

_WORKFLOW = Path(__file__).resolve().parents[2] / "scripts/workflow"
# Loaded from its file, so the scripts' folder is not put on the path of every other test.
_SPEC = importlib.util.spec_from_file_location("workflow_config", _WORKFLOW / "workflow_config.py")
assert _SPEC and _SPEC.loader
workflow_config = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflow_config)

_GIT_ENV = {
    "PATH": os.environ["PATH"],
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


def _config(tmp_path: Path, given: dict[str, Any]) -> Any:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(repo)], env=_GIT_ENV, check=True
    )
    (repo / "shipline.json").write_text(
        json.dumps({"repository": "example-owner/example-app", **given})
    )
    return workflow_config.load(repo)


def test_without_suites_a_cloud_push_runs_none(tmp_path: Path) -> None:
    assert _config(tmp_path, {}).cloud_pre_push.suites == ()


def test_each_suite_is_read_with_its_default_worker_count(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {
            "cloud_pre_push": {
                "suites": {
                    "server": {
                        "select": "tools/shipline/server-tests-for-changes",
                        "run": "tools/shipline/server-tests",
                        "part": "--shard={shard}/{shards}",
                    },
                    "browser": {
                        "select": "tools/shipline/browser-tests-for-changes",
                        "run": "tools/shipline/browser-tests",
                        "probe": "tools/shipline/browser-starts",
                    },
                    "web": {
                        "select": "tools/shipline/web-tests-for-changes",
                        "run": "tools/shipline/web-tests",
                        "workers": 3,
                    },
                }
            }
        },
    )

    server, browser, web = config.cloud_pre_push.suites
    assert (server.name, server.workers, server.part, server.probe) == (
        "server",
        4,
        "--shard={shard}/{shards}",
        None,
    )
    assert server.select == "tools/shipline/server-tests-for-changes"
    assert server.run == "tools/shipline/server-tests"
    assert (browser.name, browser.workers, browser.probe) == (
        "browser",
        2,
        "tools/shipline/browser-starts",
    )
    assert (web.name, web.workers, web.part) == ("web", 3, None)
    assert workflow_config.as_dict(config)["cloud_pre_push"] == {
        "suites": {
            "server": {
                "select": "tools/shipline/server-tests-for-changes",
                "run": "tools/shipline/server-tests",
                "workers": 4,
                "part": "--shard={shard}/{shards}",
            },
            "browser": {
                "select": "tools/shipline/browser-tests-for-changes",
                "run": "tools/shipline/browser-tests",
                "workers": 2,
                "probe": "tools/shipline/browser-starts",
            },
            "web": {
                "select": "tools/shipline/web-tests-for-changes",
                "run": "tools/shipline/web-tests",
                "workers": 3,
            },
        }
    }


@pytest.mark.parametrize(
    "suites,refusal",
    [
        ([], "cloud_pre_push.suites must be an object"),
        ({"server": "tests"}, "cloud_pre_push.suites.server must be an object"),
        ({"server": {"run": "x"}}, "cloud_pre_push.suites.server.select must be a command"),
        ({"server": {"select": "x"}}, "cloud_pre_push.suites.server.run must be a command"),
        (
            {"server": {"select": "x", "run": "y", "speed": 2}},
            "unknown key cloud_pre_push.suites.server.speed",
        ),
        (
            {"server": {"select": "x", "run": "y", "workers": 0}},
            "cloud_pre_push.suites.server.workers must be a whole number above 0",
        ),
        (
            {"server": {"select": "x", "run": "y", "workers": "4"}},
            "cloud_pre_push.suites.server.workers must be a whole number above 0",
        ),
        (
            {"server": {"select": "x", "run": "y", "workers": True}},
            "cloud_pre_push.suites.server.workers must be a whole number above 0",
        ),
        (
            {"server": {"select": "x", "run": "y", "probe": " "}},
            "cloud_pre_push.suites.server.probe must be a command",
        ),
        (
            {"server": {"select": "x", "run": "y", "part": "--shard=1/4"}},
            "cloud_pre_push.suites.server.part must name {shard} and {shards}",
        ),
        (
            {"two words": {"select": "x", "run": "y"}},
            "cloud_pre_push.suites: a suite's name is letters, digits, - and _, not two words",
        ),
    ],
)
def test_a_suite_that_cannot_run_is_refused_by_name(
    tmp_path: Path, suites: Any, refusal: str
) -> None:
    with pytest.raises(workflow_config.ConfigError, match=f"^shipline.json: {re.escape(refusal)}$"):
        _config(tmp_path, {"cloud_pre_push": {"suites": suites}})


def test_an_unknown_key_beside_the_suites_is_refused(tmp_path: Path) -> None:
    with pytest.raises(
        workflow_config.ConfigError, match="^shipline.json: unknown key cloud_pre_push.limit$"
    ):
        _config(tmp_path, {"cloud_pre_push": {"limit": 3}})


def test_the_push_checks_are_a_command_of_the_repository(tmp_path: Path) -> None:
    assert _config(tmp_path, {}).commands.push_checks is None
    named = _config(tmp_path / "named", {"commands": {"push_checks": "tools/push-checks"}})
    assert named.commands.push_checks == "tools/push-checks"


# A stand-in for a repository's selector, as CI would choose: a change to a workflow or to the
# lock file reaches the whole suite, each changed file of its own folder its own test, any other
# change none. It writes down what it was given.
_SELECTOR = """#!/usr/bin/env python3
import json, sys
kind, files_key, folder, suffix = sys.argv[1:5]
arguments = sys.argv[5:]
if arguments[0] == "--ci":
    arguments = arguments[1:]
base, flag, listing = arguments
assert flag == "--files-from", flag
files = open(listing).read().split()
with open("@STATE@/calls.log", "a") as log:
    log.write(f"select-{kind} {' '.join(sys.argv[5:-1])} {' '.join(files)}\\n")
reasons = [f"{path} changed" for path in files
           if path.startswith(".github/workflows/") or path == "package-lock.json"]
tests = sorted(f"{folder}/{path.rsplit('/', 1)[-1].split('.')[0]}{suffix}"
               for path in files if path.startswith(folder + "/"))
whole = bool(reasons)
print(json.dumps({"whole": whole, files_key: [] if whole else tests, "total": 40,
                  "reasons": reasons}))
"""

# Runs as a suite: writes down its words and who it ran as, and fails, hangs or ignores TERM
# as the test asks by a file named after it.
_RUNNER = """#!/bin/bash
name=$1
echo "run $* as=${RAN_AS:-self}" >> "@STATE@/calls.log"
if [ -e "@STATE@/ignore-term-$name" ]; then trap '' TERM; fi
if [ -e "@STATE@/hang-$name" ]; then /bin/sleep 30 & wait; /bin/sleep 30 & wait; fi
if [ -e "@STATE@/fail-$name" ]; then echo "$name: 1 test failed"; exit 1; fi
echo "$name: all passed"
"""

_PROBE = """#!/bin/bash
echo "probe as=${RAN_AS:-self}" >> "@STATE@/calls.log"
if [ -e "@STATE@/no-browser" ]; then echo "Failed to launch the browser: no display" >&2; exit 1; fi
"""

# `runuser -u <account> -- <command>`: runs the command, marked as run by that account.
_RUNUSER = """#!/bin/bash
echo "runuser $*" >> "@STATE@/calls.log"
[ "$1" = -u ] && [ "$3" = -- ] || exit 2
account=$2
shift 3
RAN_AS=$account exec "$@"
"""

_PUSH_CHECKS = """#!/bin/bash
echo "push-checks $*" >> "@STATE@/calls.log"
[ ! -e "@STATE@/push-checks-fail" ] || { echo "lint: 2 problems"; exit 1; }
"""

_SUITES = {
    "server": {
        "select": "selector server tests server _test.py",
        "run": "suite-run server --workers {workers}",
        "part": "--shard {shard}/{shards}",
    },
    "browser": {
        "select": "selector browser specs web .spec.ts --ci",
        "run": "suite-run browser --workers={workers}",
        "probe": "browser-starts",
    },
    "docs": {
        "select": "selector docs tests docs _check.py",
        "run": "suite-run docs",
    },
}

_CLOUD = {"SHIPLINE_CLOUD_WORKER": "1", "WORKFLOW_EUID": "1000"}


def _cloud_repository(workflow: Workflow, suites: dict[str, Any] = _SUITES) -> None:
    """Main names the suites and the push checks; the checkout is on a branch of its own."""
    for tool, text in (
        ("selector", _SELECTOR),
        ("suite-run", _RUNNER),
        ("browser-starts", _PROBE),
        ("runuser", _RUNUSER),
        ("push-checks", _PUSH_CHECKS),
    ):
        workflow.fake(tool, text)
    config = json.loads((workflow.repo / "shipline.json").read_text())
    config["commands"] = {"push_checks": "push-checks --quick"}
    config["cloud_pre_push"] = {"suites": suites}
    (workflow.repo / "shipline.json").write_text(json.dumps(config))
    for path in ("server/orders.py", "web/cart.ts", "docs/guide.md", "README.md"):
        (workflow.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (workflow.repo / path).write_text("first\n")
    workflow.git("add", "-A")
    workflow.git("commit", "--quiet", "-m", "chore: suites")
    workflow.git("push", "--quiet", "origin", "main")
    workflow.git("checkout", "--quiet", "-b", "ticket/change")


def _change(workflow: Workflow, *paths: str, commit: bool = True) -> None:
    for path in paths:
        (workflow.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (workflow.repo / path).write_text("changed\n")
    if commit:
        workflow.git("add", "-A")
        workflow.git("commit", "--quiet", "-m", "feat: a change")


def _run(workflow: Workflow, *args: str, env: dict[str, str] | None = None) -> Any:
    return workflow.run_script("ci-tests-for-changes", *args, env=env)


def _selections(workflow: Workflow) -> list[str]:
    return workflow.calls("select-")


def _runs(workflow: Workflow) -> list[str]:
    return workflow.calls("run ")


def _main_moves_on(workflow: Workflow) -> str:
    """Main gains a commit the branch does not have: the base is still where they parted."""
    base = workflow.git("rev-parse", "main")
    workflow.git("checkout", "--quiet", "main")
    _change(workflow, "server/billing.py")
    workflow.git("push", "--quiet", "origin", "main")
    workflow.git("checkout", "--quiet", "ticket/change")
    return base


@pytest.mark.parametrize(
    "paths,server,browser,docs",
    [
        # A server change: its own test, nothing else.
        (
            ("server/orders.py",),
            "server: 1 of 40 files: server/orders_test.py",
            "browser: none",
            "docs: none",
        ),
        # A workflow change: the whole of every suite, with its reason.
        (
            (".github/workflows/ci.yml",),
            "server: the whole suite (40 files)\n  because: .github/workflows/ci.yml changed",
            "browser: the whole suite (40 files)\n  because: .github/workflows/ci.yml changed",
            "docs: the whole suite (40 files)\n  because: .github/workflows/ci.yml changed",
        ),
        # A broad change: the lock file, and files of two suites.
        (
            ("package-lock.json", "server/orders.py", "web/cart.ts"),
            "server: the whole suite (40 files)\n  because: package-lock.json changed",
            "browser: the whole suite (40 files)\n  because: package-lock.json changed",
            "docs: the whole suite (40 files)\n  because: package-lock.json changed",
        ),
        # A change that reaches an extra suite of the repository's own.
        (
            ("docs/guide.md",),
            "server: none",
            "browser: none",
            "docs: 1 of 40 files: docs/guide_check.py",
        ),
    ],
)
def test_the_selection_is_the_selectors_answer_for_the_change_against_main(
    workflow: Workflow, paths: tuple[str, ...], server: str, browser: str, docs: str
) -> None:
    _cloud_repository(workflow)
    _change(workflow, *paths)
    base = _main_moves_on(workflow)

    done = _run(workflow)

    assert done.returncode == 0, done.stderr
    changed = " ".join(sorted(paths))
    assert _selections(workflow) == [
        f"select-server {base} --files-from {changed}",
        f"select-browser --ci {base} --files-from {changed}",
        f"select-docs {base} --files-from {changed}",
    ]
    assert done.stdout.endswith(f"{server}\n{browser}\n{docs}\n"), done.stdout
    assert f"against {base[:12]}, where this branch left origin/main" in done.stdout
    assert _runs(workflow) == []
    assert workflow.calls("push-checks") == []


def test_tracked_working_changes_are_part_of_the_change(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py")
    _change(workflow, "web/cart.ts", commit=False)
    (workflow.repo / "server/untracked.py").write_text("new\n")

    done = _run(workflow)

    assert done.returncode == 0, done.stderr
    assert "browser: 1 of 40 files: web/cart.spec.ts" in done.stdout
    assert all(
        call.endswith("--files-from server/orders.py web/cart.ts")
        for call in _selections(workflow)
    )


def test_a_person_and_a_cloud_worker_get_the_same_answer(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py", "web/cart.ts")

    person = _run(workflow)
    worker = _run(workflow, env=_CLOUD)

    assert person.returncode == worker.returncode == 0
    assert person.stdout == worker.stdout


def test_a_repository_calls_it_as_a_shipline_command_from_its_hook(workflow: Workflow) -> None:
    install_shipline(workflow)
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py")

    done = workflow.shipline("ci-tests-for-changes", "--run", "--extra-only", env=_CLOUD)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _runs(workflow) == ["run server --workers 4 server/orders_test.py as=self"]


def test_a_local_push_runs_no_extra_suite(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py", "web/cart.ts")

    done = _run(workflow, "--run", "--extra-only")

    assert done.returncode == 0, done.stderr
    assert "not a cloud worker" in done.stdout
    assert _selections(workflow) == []
    assert _runs(workflow) == []


def test_a_cloud_push_runs_the_push_checks_then_the_selected_suites(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py", "web/cart.ts")

    done = _run(workflow, "--run", env=_CLOUD)

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("push-checks") == ["push-checks --quick"]
    assert _runs(workflow) == [
        "run server --workers 4 server/orders_test.py as=self",
        "run browser --workers=2 web/cart.spec.ts as=self",
    ]
    assert re.search(r"^server: passed in 0m [0-9]+s$", done.stdout, re.MULTILINE)
    assert re.search(r"^browser: passed in 0m [0-9]+s$", done.stdout, re.MULTILINE)
    assert "docs: none" in done.stdout


def test_inside_the_hook_only_the_suites_run(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py")

    done = _run(workflow, "--run", "--extra-only", env=_CLOUD)

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("push-checks") == []
    assert _runs(workflow) == ["run server --workers 4 server/orders_test.py as=self"]


def test_the_whole_suite_runs_without_naming_files(workflow: Workflow) -> None:
    _cloud_repository(workflow, {"server": _SUITES["server"]})
    _change(workflow, ".github/workflows/ci.yml")

    done = _run(workflow, "--run", "--extra-only", env=_CLOUD)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _runs(workflow) == ["run server --workers 4 as=self"]


def test_a_failing_suite_refuses_the_push_after_every_suite_ran(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py", "web/cart.ts", "docs/guide.md")
    (workflow.state / "fail-server").touch()

    done = _run(workflow, "--run", "--extra-only", env=_CLOUD)

    assert done.returncode == 1
    assert [run.split()[1] for run in _runs(workflow)] == ["server", "browser", "docs"]
    assert "server: 1 test failed" in done.stdout
    assert re.search(r"^server: failed in 0m [0-9]+s$", done.stdout, re.MULTILINE)
    assert done.stdout.endswith("the push is refused: server failed\n")


def test_failing_push_checks_refuse_before_any_suite(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py")
    (workflow.state / "push-checks-fail").touch()

    done = _run(workflow, "--run", env=_CLOUD)

    assert done.returncode == 1
    assert "lint: 2 problems" in done.stdout
    assert done.stdout.endswith("the push is refused: the push checks failed\n")
    assert _runs(workflow) == []


def test_the_browser_selection_is_uncapped_and_its_failure_refuses(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    specs = [f"web/page{number}.ts" for number in range(6)]
    _change(workflow, *specs)
    (workflow.state / "fail-browser").touch()

    done = _run(workflow, "--run", "--extra-only", env=_CLOUD)

    assert done.returncode == 1
    assert "browser: 6 of 40 files: " in done.stdout
    expected = " ".join(f"web/page{number}.spec.ts" for number in range(6))
    assert _runs(workflow) == [f"run browser --workers=2 {expected} as=self"]
    assert done.stdout.endswith("the push is refused: browser failed\n")


def test_a_browser_that_cannot_start_leaves_the_suite_to_ci(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py", "web/cart.ts")
    (workflow.state / "no-browser").touch()

    done = _run(workflow, "--run", "--extra-only", env=_CLOUD)

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("probe") == ["probe as=self"]
    assert _runs(workflow) == ["run server --workers 4 server/orders_test.py as=self"]
    assert (
        "browser: left to CI: no browser can start as this user: "
        "Failed to launch the browser: no display"
    ) in done.stdout


def test_as_root_every_suite_and_the_probe_run_as_the_test_account(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py", "web/cart.ts")

    done = _run(
        workflow,
        "--run",
        env={**_CLOUD, "WORKFLOW_EUID": "0", "SHIPLINE_CLOUD_TEST_USER": "tests"},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("push-checks") == ["push-checks --quick"]
    assert workflow.calls("probe") == ["probe as=tests"]
    assert _runs(workflow) == [
        "run server --workers 4 server/orders_test.py as=tests",
        "run browser --workers=2 web/cart.spec.ts as=tests",
    ]
    assert workflow.calls("runuser") == [
        "runuser -u tests -- suite-run server --workers 4 server/orders_test.py",
        "runuser -u tests -- browser-starts",
        "runuser -u tests -- suite-run browser --workers=2 web/cart.spec.ts",
    ]


def test_as_root_without_a_test_account_nothing_runs(workflow: Workflow) -> None:
    _cloud_repository(workflow)
    _change(workflow, "server/orders.py")

    done = _run(workflow, "--run", env={**_CLOUD, "WORKFLOW_EUID": "0"})

    assert done.returncode == 1
    assert "SHIPLINE_CLOUD_TEST_USER" in done.stdout + done.stderr
    assert workflow.calls("push-checks") == []
    assert _runs(workflow) == []
    assert workflow.calls("runuser") == []


@pytest.mark.parametrize("ignores_term", [False, True])
def test_a_suite_past_its_limit_is_stopped_and_refuses_with_its_duration(
    workflow: Workflow, ignores_term: bool
) -> None:
    _cloud_repository(workflow, {"server": _SUITES["server"]})
    _change(workflow, "server/orders.py")
    (workflow.state / "hang-server").touch()
    if ignores_term:
        (workflow.state / "ignore-term-server").touch()

    done = _run(
        workflow,
        "--run",
        "--extra-only",
        env={**_CLOUD, "WORKFLOW_CLOUD_SUITE_SECONDS": "1", "WORKFLOW_CLOUD_SUITE_GRACE": "1"},
    )

    assert done.returncode == 1
    assert re.search(
        r"^server: timed out after 0m [12]s, the limit is 0m 1s$", done.stdout, re.MULTILINE
    ), done.stdout
    assert done.stdout.endswith("the push is refused: server timed out\n")


@pytest.mark.parametrize("named_by", ["argument", "environment"])
def test_one_part_of_the_server_suite_runs_and_ci_runs_the_rest(
    workflow: Workflow, named_by: str
) -> None:
    _cloud_repository(workflow)
    _change(workflow, ".github/workflows/ci.yml")
    (workflow.state / "no-browser").touch()
    arguments, env = ("--run", "--extra-only"), dict(_CLOUD)
    if named_by == "argument":
        arguments += ("--server-part", "2 of 4")
    else:
        env["SHIPLINE_CLOUD_SERVER_PART"] = "2 of 4"

    done = _run(workflow, *arguments, env=env)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _runs(workflow)[0] == "run server --workers 4 --shard 2/4 as=self"
    assert re.search(
        r"^server: part 2 of 4 passed in 0m [0-9]+s; CI runs the rest$", done.stdout, re.MULTILINE
    )


def test_a_failing_server_part_refuses(workflow: Workflow) -> None:
    _cloud_repository(workflow, {"server": _SUITES["server"]})
    _change(workflow, ".github/workflows/ci.yml")
    (workflow.state / "fail-server").touch()

    done = _run(workflow, "--run", "--extra-only", "--server-part", "1 of 4", env=_CLOUD)

    assert done.returncode == 1
    assert _runs(workflow) == ["run server --workers 4 --shard 1/4 as=self"]
    assert done.stdout.endswith("the push is refused: server failed\n")


@pytest.mark.parametrize("part", ["0 of 4", "5 of 4", "1/4", "one of four"])
def test_a_part_that_is_no_part_is_refused(workflow: Workflow, part: str) -> None:
    _cloud_repository(workflow, {"server": _SUITES["server"]})
    _change(workflow, "server/orders.py")

    done = _run(workflow, "--run", "--server-part", part, env=_CLOUD)

    assert done.returncode == 2
    assert '"K of N"' in done.stderr
    assert _runs(workflow) == []
