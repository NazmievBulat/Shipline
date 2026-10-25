"""Breaking tests for pull request 63: config keys declared once, their documentation generated.

Each test loads the same files with the loader as it was before the change
(`break_63_before/workflow_config.py`, main at 5468b5c) and with the changed one, and compares
what an installation reads: every value, `as_dict`, each command's words, the `shell`, `sh`
and `json` output, and the refusal a damaged file gets.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AFTER = _ROOT / "scripts/workflow/workflow_config.py"
_BEFORE = Path(__file__).resolve().parent / "break_63_before/workflow_config.py"
_REPOSITORY = "example-owner/example-app"

_GIT_ENV = {
    "PATH": os.environ["PATH"],
    "LANG": "C.UTF-8",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "owner@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "owner@example.com",
}

# Everything a script reads from the loaded config, printed as JSON.
_DUMP = r"""
import dataclasses, importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("workflow_config", sys.argv[1])
wc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc)
try:
    config = wc.current()
except wc.ConfigError as error:
    print(json.dumps({"refused": str(error)}))
    sys.exit(0)
out = {
    "repr": repr(config),
    "as_dict": wc.as_dict(config),
    "as_dict_order": json.dumps(wc.as_dict(config)),
    "fields": {
        type(part).__name__: [item.name for item in dataclasses.fields(part)]
        for part in (config, config.checks, config.annotations, config.workflows, config.labels,
                     config.runner, config.merge, config.parity, config.hunts, config.refresh,
                     config.commands, config.cloud_pre_push, config.review, config.merge_train)
    },
    "run_as_words": list(wc.RUN_AS_WORDS),
    "commands": {name: config.command(name) for name in wc.RUN_AS_WORDS},
    "repository": config.repository,
    "api": config.api,
    "state": str(config.state()),
    "install_folder": str(config.install_folder),
    "images_plan": config.checks.images_plan,
    "groups": {k: dataclasses.astuple(v) for k, v in config.checks.groups().items()},
    "main_label": config.runner.main_label,
    "main_batch_label": config.runner.main_batch_label,
    "identity_path": str(config.runner.identity_path),
    "parity_known": None if config.parity.known is None else str(config.file(config.parity.known)),
    "hot": config.hot_paths_of("src/example_app/models.py"),
    "suites": [[config.words(s.run), s.workers] for s in config.cloud_pre_push.suites],
}
print(json.dumps(out))
"""


def _git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), *args], env=_GIT_ENV, check=True,
                   capture_output=True, timeout=30)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


# An installation's own file, kept outside the repository under ~/.config/shipline/<owner>-<name>.
INSTALLED: dict[str, Any] = {
    "repository": _REPOSITORY,
    "state_folder": ".local/shipline",
    "labels": {
        "workflow": "shipline",
        "audited": "audited",
        "priority": {
            "low": "p3",
            "blocker": "p0",
            "high": "p1",
            "medium": "p2",
        },
    },
    "checks": {
        "ci_result": "Build result",
        "ci_plan": "Build: what to run",
        "e2e_plan": "Browser: what to run",
        "images": "Images",
        "dependency_audit": "Build: dependencies",
        "advisory_annotation": "Advisory",
        "prefix_only": ["server_whole", "images"],
    },
    "runner": {
        "prefix": "example-app",
        "identity_folder": "~/.config/example-app-runner",
        "image_build": "@install/tools/build-runner-images --push",
        "hub_cache": "tools/shipline/hub-cache",
    },
    "commands": {
        "test": "tools/run-tests --workers 4",
        "browser": "tools/run-browser-tests",
        "browser_tests_for_changes": {
            "run": "@install/tools/browser-tests-for-changes",
            "form": "json",
            "field": "specs",
            "args": ["--ci", "--base", "main"],
        },
        "server_tests_for_changes": {
            "run": "python3 @install/tools/server_tests.py",
            "form": "json",
            "fields": {"whole": "all", "tests": "paths", "total": "count", "reasons": "why"},
        },
        "pre_train_checks": {"run": "@install/tools/pre-train", "form": "exit", "args": ["-q"]},
        "mechanical_conflicts": {
            "run": "@install/tools/conflicts.py",
            "form": "functions",
            "functions": {"resolve": "resolve_all", "renumber": "renumber", "taken": "taken"},
        },
        "browser_plan": {
            "run": "@install/tools/plan.py",
            "form": "functions",
            "functions": {"browser_plan": "plan"},
        },
        "push_checkout": {"run": "tools/shipline/push-checkout", "form": "lines",
                          "args": ["--checkout", "two words"]},
        "hunt_app": "@install/tools/hunt-app",
        "hunt_browser": "node tools/shipline/hunt-browser.mjs --headless",
        "hunt_cursor": "tools/shipline/hunt-cursor",
        "advisory_watch": "python3 -u tools/shipline/advisory_watch.py",
        "push_checks": "@install/tools/push-checks",
    },
    "hunts": {
        "areas": {
            "phone": ["/calls", "/contacts"],
            "billing": ["/invoices", "/settings/billing"],
            "admin": ["/admin/users"],
        },
        "plans": "docs/hunt-plans.md",
    },
    "parity": {"known": "@install/parity-known.txt"},
}


class Setup:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        self.main = base / "example-app"
        self.install = self.home / ".config/shipline/example-owner-example-app"
        self.config = self.install / "shipline.json"
        self.main.mkdir(parents=True)
        _git(self.main, "init", "-q", "-b", "main")
        _git(self.main, "remote", "add", "origin", f"https://github.com/{_REPOSITORY}.git")
        # Some programs live in the checkout, the rest only in the installation.
        for name in ("tools/run-tests", "tools/shipline/push-checkout", "tools/shipline/hunt-cursor"):
            _write(self.main / name, "#!/bin/sh\n", 0o755)
        for name in ("tools/build-runner-images", "tools/browser-tests-for-changes",
                     "tools/pre-train", "tools/hunt-app", "tools/push-checks"):
            _write(self.install / name, "#!/bin/sh\n", 0o755)
        _write(self.install / "tools/server_tests.py", "print('{}')\n")
        _write(self.install / "tools/conflicts.py",
               "def resolve_all(head, main):\n    return []\n"
               "def renumber(head, main):\n    return []\n"
               "def taken(head, main):\n    return []\n")
        _write(self.install / "tools/plan.py", "def plan(paths):\n    return 'needed'\n")
        _write(self.install / "parity-known.txt", "")
        self.write(INSTALLED)

    def write(self, given: Any) -> None:
        _write(self.config, given if isinstance(given, str) else json.dumps(given, indent=2), 0o600)

    def env(self, **extra: str) -> dict[str, str]:
        env = {**_GIT_ENV, "HOME": str(self.home), "SHIPLINE_REPO": str(self.main)}
        env.pop("XDG_CONFIG_HOME", None)
        env.update(extra)
        return env

    def normal(self, text: str, module: Path) -> str:
        # The loaders sit in different folders; `workflow_commands.py` is named beside each.
        return text.replace(str(module.parent), "<scripts>")

    def run(self, module: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        done = subprocess.run([sys.executable, str(module), *args], cwd=self.main,
                              env=env or self.env(), capture_output=True, text=True, timeout=60)
        return done.returncode, self.normal(done.stdout, module), self.normal(done.stderr, module)

    def dump(self, module: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
        done = subprocess.run([sys.executable, "-c", _DUMP, str(module)], cwd=self.main,
                              env=env or self.env(), capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        return json.loads(self.normal(done.stdout, module))

    def same(self, env: dict[str, str] | None = None) -> dict[str, Any]:
        before, after = self.dump(_BEFORE, env), self.dump(_AFTER, env)
        # Key order of as_dict is checked on its own below: no script prints it.
        before.pop("as_dict_order", None), after.pop("as_dict_order", None)
        assert after == before
        for mode in ("shell", "sh", "json"):
            assert self.run(_AFTER, mode, env=env) == self.run(_BEFORE, mode, env=env), mode
        return after


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def test_the_installed_file_loads_and_prints_the_same(setup: Setup) -> None:
    after = setup.same()
    assert "refused" not in after
    assert after["state"] == str(setup.main / ".local/shipline")
    assert after["install_folder"] == str(setup.install)
    code, out, _ = setup.run(_AFTER, "shell")
    assert code == 0
    assert f"SHIPLINE_CONFIG={setup.config}" in out
    assert "SHIPLINE_HUNT_AREAS=(phone billing admin)" in out


def test_the_installed_file_named_explicitly_loads_the_same(setup: Setup) -> None:
    setup.same(setup.env(SHIPLINE_CONFIG=str(setup.config)))


def test_the_installed_file_through_a_config_home_loads_the_same(setup: Setup) -> None:
    setup.same(setup.env(XDG_CONFIG_HOME=str(setup.home / ".config")))


def test_a_runner_with_its_own_labels_and_install_folder_loads_the_same(setup: Setup) -> None:
    given = json.loads(json.dumps(INSTALLED))
    given["runner"].update(label="example-app-box", planner_label="example-app-plan",
                           labels=["example-app-box", "example-app-plan"])
    given["install_folder"] = "."
    setup.write(given)
    setup.same()


def test_a_minimal_installed_file_keeps_every_default_the_same(setup: Setup) -> None:
    setup.write({"repository": _REPOSITORY})
    setup.same()


def test_no_file_at_all_loads_the_same(setup: Setup) -> None:
    setup.config.unlink()
    after = setup.same()
    assert after["repository"] == _REPOSITORY


def test_a_checkout_file_loads_the_same(setup: Setup) -> None:
    setup.config.unlink()
    given = {key: value for key, value in INSTALLED.items() if key not in ("parity",)}
    given["commands"] = {key: value for key, value in INSTALLED["commands"].items()
                         if not json.dumps(value).count("@install/")}
    _write(setup.main / "shipline.json", json.dumps(given))
    setup.same()


# Damaged files an installation has met or could meet after an edit; each must be refused with
# the same words and exit.
def _with(path: str, value: Any) -> dict[str, Any]:
    given = json.loads(json.dumps(INSTALLED))
    target = given
    *parents, last = path.split(".")
    for part in parents:
        target = target.setdefault(part, {})
    if value is _DROP:
        target.pop(last, None)
    else:
        target[last] = value
    return given


_DROP = object()
DAMAGED = {
    "unknown top key": _with("hunt_areas", {}),
    "unknown nested key": _with("checks.ci_results", "x"),
    "unknown priority": _with("labels.priority.urgent", "p9"),
    "priority not an object": _with("labels.priority", "p0"),
    "labels not an object": _with("labels", ["shipline"]),
    "state folder outside": _with("state_folder", "../state"),
    "state folder a list": _with("state_folder", [".local"]),
    "state folder empty": _with("state_folder", " "),
    "repository bad": _with("repository", "example-app"),
    "repository a number": _with("repository", 7),
    "prefix_only unknown": _with("checks.prefix_only", ["images", "web"]),
    "prefix_only twice": _with("checks.prefix_only", ["images", "images"]),
    "prefix_only a name": _with("checks.prefix_only", "images"),
    "runner prefix bad": _with("runner.prefix", "Example_App"),
    "runner labels a name": _with("runner.labels", "example-app-home"),
    "runner label bad": _with("runner.label", "-x"),
    "identity folder a number": _with("runner.identity_folder", 3),
    "command a list": _with("commands.test", ["tools/run-tests"]),
    "command empty": _with("commands.hunt_app", ""),
    "command outside": _with("commands.hunt_app", "../tools/hunt-app"),
    "command unbalanced quote": _with("commands.hunt_cursor", "tools/x 'a"),
    "command form unknown": _with("commands.pre_train_checks", {"run": "tools/x", "form": "json"}),
    "command entry unknown key": _with("commands.push_checks", {"run": "tools/x", "form": "exit",
                                                                "timeout": 3}),
    "install without folder": _with("commands.push_checks", "@install/../x"),
    "functions missing": _with("commands.mechanical_conflicts.functions", {"resolve": "resolve_all"}),
    "function undefined": _with("commands.browser_plan.functions", {"browser_plan": "nope"}),
    "areas a list": _with("hunts.areas", ["phone"]),
    "area bad name": _with("hunts.areas", {"Phone": ["/calls"]}),
    "area empty screens": _with("hunts.areas", {"phone": []}),
    "area screen bad": _with("hunts.areas", {"phone": ["calls"]}),
    "plans outside": _with("hunts.plans", "../plans.md"),
    "plans with space": _with("hunts.plans", "docs/hunt plans.md"),
    "plans a number": _with("hunts.plans", 1),
    "parity known outside": _with("parity.known", "../known.txt"),
    "parity known a list": _with("parity.known", ["x"]),
    "parity unknown key": _with("parity.tests", "x"),
    "minutes a bool": _with("answer_for_owner_after_minutes", True),
    "minutes too many": _with("cloud_wake_after_minutes", 100000),
    "minutes a string": _with("merge_train.queued_limit_minutes", "20"),
    "hot paths a name": _with("hot_paths", "src/"),
    "machines a dict": _with("machines", {"name": "x"}),
    "conflict review partial": _with("conflict_review", {"effort": "medium"}),
    "suites a list": _with("cloud_pre_push.suites", ["server"]),
    "high effort paths bad": _with("review.high_effort_paths", ["("]),
    "scratch prefix bad": _with("merge.scratch_prefix", "workflow/merge"),
    "refresh not ignored": _with("refresh.clear_generated", ["build"]),
    "install folder a number": _with("install_folder", 3),
    "file a list": [INSTALLED],
    "commands not an object": _with("commands", "tools/run-tests"),
    "section null": _with("checks", None),
    "value null": _with("checks.ci_result", None),
}


def test_as_dict_keeps_its_key_order(setup: Setup) -> None:
    # Low, not reached in use: no script prints or iterates as_dict, only tests compare it.
    assert setup.dump(_AFTER)["as_dict_order"] == setup.dump(_BEFORE)["as_dict_order"]


@pytest.mark.parametrize("name", list(DAMAGED))
def test_a_damaged_file_is_refused_the_same(setup: Setup, name: str) -> None:
    setup.write(DAMAGED[name])
    before = setup.run(_BEFORE, "shell")
    after = setup.run(_AFTER, "shell")
    assert after == before
    assert after[0] == 1
    assert setup.dump(_AFTER) == setup.dump(_BEFORE)


def test_damaged_json_and_a_missing_explicit_file_are_refused_the_same(setup: Setup) -> None:
    setup.write('{"repository": "example-owner/example-app",')
    assert setup.run(_AFTER, "sh") == setup.run(_BEFORE, "sh")
    env = setup.env(SHIPLINE_CONFIG=str(setup.install / "missing.json"))
    assert setup.run(_AFTER, "shell", env=env) == setup.run(_BEFORE, "shell", env=env)


def test_runner_settings_that_hang_are_refused_the_same(setup: Setup) -> None:
    setup.write({"state_folder": ".local/shipline"})
    _write(setup.main / ".github/runner/settings.sh", "sleep 30\nrepository=example-owner/example-app\n")
    after = setup.run(_AFTER, "shell")
    assert after == setup.run(_BEFORE, "shell")
    assert after[0] == 1 and "more than 5 seconds" in after[2]


def test_two_loads_at_once_print_the_same(setup: Setup) -> None:
    results: list[tuple[int, str, str]] = []
    threads = [threading.Thread(target=lambda: results.append(setup.run(_AFTER, "shell")))
               for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and len(set(results)) == 1
    assert results[0] == setup.run(_BEFORE, "shell")


def test_the_docs_check_reads_no_configuration_and_writes_nothing(setup: Setup) -> None:
    # `docs` runs from Shipline's own checkout: a damaged installation file must not change it,
    # and a check without --write must leave the tracked files as they are.
    setup.write("not json")
    tracked = {path: path.read_bytes() for path in (_ROOT / "examples/shipline.json",
                                                   _ROOT / "scripts/workflow/README.md")}
    done = subprocess.run([sys.executable, str(_AFTER), "docs"], cwd=setup.main,
                          env=setup.env(SHIPLINE_CONFIG=str(setup.config)),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert {path: path.read_bytes() for path in tracked} == tracked


def test_the_usage_line_still_refuses_an_unknown_mode(setup: Setup) -> None:
    code, _, err = setup.run(_AFTER, "yaml")
    assert code == 2 and "shell|sh|json" in err
    code, _, err = setup.run(_AFTER, "docs", "--check")
    assert code == 2


def test_a_file_that_also_names_the_rest_loads_the_same(setup: Setup) -> None:
    given = json.loads(json.dumps(INSTALLED))
    given["labels"]["priority"] = {"blocker": "p0"}
    given.update(
        machines=[{"name": "this machine"}, {"name": "runner-1", "address": "runner@runner-1.example"}],
        hot_paths=["src/example_app/models.py", "web/src/pages/**"],
        conflict_review={"effort": "medium", "model": "sonnet", "max_changed_lines": 40},
        review={"high_effort_paths": ["^db/migrations/", "(^|/)locks?\\.py$"]},
        merge_train={"queued_limit_minutes": 0},
        answer_for_owner_after_minutes=0,
        cloud_wake_after_minutes=1440,
        cloud_pre_push={"suites": {
            "server": {"select": "tools/run-tests --select", "run": "tools/run-tests {workers}",
                       "part": "--shard {shard}/{shards}"},
            "lint": {"select": "@install/tools/push-checks", "run": "@install/tools/push-checks"},
        }},
        annotations={"merge_tested": "Merge built"},
        workflows={"ci": "build.yml"},
        merge={"scratch_prefix": "shipline/batch/"},
        worktree_setup="tools/prepare-worktree",
    )
    setup.write(given)
    after = setup.same()
    assert after["hot"] == ["src/example_app/models.py"]


def test_every_section_keeps_its_constructor() -> None:
    import importlib.util
    import inspect

    def load(path: Path, name: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    before, after = load(_BEFORE, "before_63"), load(_AFTER, "after_63")
    for name in ("Checks", "Annotations", "Workflows", "Labels", "Runner", "Merge", "Parity",
                 "Machine", "Hunts", "Refresh", "Commands", "Suite", "CloudPrePush", "Review",
                 "ConflictReview", "MergeTrain", "Config"):
        old = inspect.signature(getattr(before, name)).parameters
        new = inspect.signature(getattr(after, name)).parameters
        assert list(new) == list(old), name
        assert [p.default is inspect.Parameter.empty for p in new.values()] == [
            p.default is inspect.Parameter.empty for p in old.values()], name
    assert after.Checks("a", "b", "c", "d") == after.Checks("a", "b", "c", "d", None, None, [])
    assert after.CloudPrePush().suites == () and after.Hunts({}).plans is None
