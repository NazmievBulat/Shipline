"""One file, `shipline.json` at the root of the main checkout, names what differs between the
repositories that use Shipline; `workflow_config.py` reads it, key by key over its defaults."""

import hashlib
import importlib.util
import fcntl
import json
import os
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

_DEFAULTS = {
    "repository": "example-owner/example-app",
    "state_folder": ".local/workflow",
    "worktree_setup": "scripts/setup-worktree",
    "checks": {
        "ci_result": "CI result",
        "ci_plan": "CI: what to run",
        "e2e_plan": "E2E: what to run",
        "images": "Docker images",
    },
    "annotations": {
        "merge_tested": "Merge tested",
        "files_tested": "Files tested",
        "server_tests": "Server tests",
        "web_suite": "Web suite",
        "browser_suite": "Browser suite",
    },
    "workflows": {"ci": "ci.yml", "e2e": "e2e.yml", "images": "images.yml"},
    "labels": {
        "workflow": "workflow",
        "audited": "audited",
        "priority": {
            "blocker": "priority: blocker",
            "high": "priority: high",
            "medium": "priority: medium",
            "low": "priority: low",
        },
    },
    "runner": {
        "labels": [],
        "identity_folder": "~/.config/shipline-runner",
        "prefix": None,
        "image_build": None,
    },
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _repository(
    tmp_path: Path, origin: str | None = "https://github.com/example-owner/example-app.git"
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    if origin is not None:
        _git(repo, "remote", "add", "origin", origin)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "chore: start")
    return repo


def _write(repo: Path, config: dict[str, Any] | str) -> None:
    text = config if isinstance(config, str) else json.dumps(config)
    (repo / "shipline.json").write_text(text)


def _as_json(config: Any) -> dict[str, Any]:
    return json.loads(json.dumps(workflow_config.as_dict(config)))


def test_without_a_file_every_value_is_its_default(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    assert _as_json(workflow_config.load(repo)) == _DEFAULTS


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/example-owner/example-app.git",
        "https://github.com/example-owner/example-app",
        "https://github.com/example-owner/example-app/",
        "https://github.com/example-owner/example-app.git/",
        "https://example-user@github.com/example-owner/example-app.git",
        "git@github.com:example-owner/example-app.git",
        "ssh://git@github.com/example-owner/example-app.git",
    ],
)
def test_the_default_repository_is_the_origin_remote(tmp_path: Path, origin: str) -> None:
    repo = _repository(tmp_path, origin)

    assert workflow_config.load(repo).repository == "example-owner/example-app"


def test_a_missing_key_falls_back_to_its_default_and_the_others_are_read(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _write(
        repo,
        {
            "state_folder": ".shipline/state",
            "checks": {"ci_result": "Build result"},
            "labels": {"priority": {"high": "urgent"}},
        },
    )

    expected = json.loads(json.dumps(_DEFAULTS))
    expected["state_folder"] = ".shipline/state"
    expected["checks"]["ci_result"] = "Build result"
    expected["labels"]["priority"]["high"] = "urgent"
    assert _as_json(workflow_config.load(repo)) == expected


def test_a_full_file_is_read_whole(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    example = json.loads((_WORKFLOW.parents[1] / "examples/shipline.json").read_text())
    _write(repo, example)

    assert _as_json(workflow_config.load(repo)) == example


def test_the_example_names_every_key(tmp_path: Path) -> None:
    example = json.loads((_WORKFLOW.parents[1] / "examples/shipline.json").read_text())

    def keys(value: Any, prefix: str = "") -> set[str]:
        if not isinstance(value, dict):
            return {prefix}
        return {key for name, inner in value.items() for key in keys(inner, f"{prefix}.{name}")}

    assert keys(example) == keys(_DEFAULTS)
    assert example["repository"].endswith("/example-app")
    assert example["runner"]["labels"]
    # One prefix names the runner pool, its labels among its names.
    assert example["runner"]["prefix"] == "example-app"
    assert all(label.startswith("example-app-") for label in example["runner"]["labels"])


def test_the_runner_prefix_is_read_and_must_be_a_name(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _write(repo, {"runner": {"prefix": "example-app-runner"}})

    assert workflow_config.load(repo).runner.prefix == "example-app-runner"

    _write(repo, {"runner": {"prefix": ""}})
    with pytest.raises(workflow_config.ConfigError, match="runner.prefix must be a name"):
        workflow_config.load(repo)


def test_the_image_build_command_is_read_and_must_be_a_command_line(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    command = "tools/shipline/build-runner-image --pull"
    _write(repo, {"runner": {"image_build": command}})

    assert workflow_config.load(repo).runner.image_build == command

    _write(repo, {"runner": {"image_build": " "}})
    with pytest.raises(workflow_config.ConfigError, match="runner.image_build must be a name"):
        workflow_config.load(repo)


def test_without_an_image_build_command_there_is_none(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _write(repo, {"runner": {"prefix": "example-app"}})

    assert workflow_config.load(repo).runner.image_build is None


def test_the_repository_named_in_the_file_wins_over_the_runner_settings_and_origin(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    settings = repo / ".github/runner/settings.sh"
    settings.parent.mkdir(parents=True)
    settings.write_text("# The pool's settings.\nrepository=settings-owner/settings-app\n")

    assert workflow_config.load(repo).repository == "settings-owner/settings-app"

    _write(repo, {"repository": "named-owner/named-app"})
    assert workflow_config.load(repo).repository == "named-owner/named-app"


# What Bash reads from the runner settings: every form a script may use names it.
_SETTINGS = {
    "default with a variable": "repository=${SHIPLINE_REPOSITORY:-example-org/example-app}\n",
    "readonly": "readonly repository=example-org/example-app\n",
    "declare": "declare -r repository=example-org/example-app\n",
    "set twice": "repository=example-org/old-app\nrepository=example-org/example-app\n",
    "export": "export repository='example-org/example-app'  # the pool's\n",
}


def _settings(repo: Path, text: str) -> Path:
    path = repo / ".github/runner/settings.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize("settings", list(_SETTINGS), ids=list(_SETTINGS))
def test_the_reader_names_the_repository_bash_reads_from_the_runner_settings(
    tmp_path: Path, settings: str
) -> None:
    # A clone of a fork: origin is not the repository the runners work on.
    repo = _repository(tmp_path, "https://github.com/example-fork/example-app.git")
    path = _settings(repo, _SETTINGS[settings])
    sourced = subprocess.run(
        ["bash", "-c", 'source "$1"; printf %s "$repository"', "bash", str(path)],
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert sourced == "example-org/example-app"

    done = subprocess.run(
        ["python3", str(_WORKFLOW / "workflow_config.py"), "shell", str(repo)],
        env={**_GIT_ENV, "SHIPLINE_REPOSITORY": "example-org/from-the-caller"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "SHIPLINE_REPOSITORY=example-org/example-app\n" in done.stdout


@pytest.mark.parametrize(
    "text",
    [
        # Names it, but Bash reads nothing.
        "repository=$(exit 3)\n",
        "repository=\"$WORKFLOW_SECRET\"\n",
        # Stops Bash before it reads one.
        "exit 4\nrepository=example-org/example-app\n",
        # Never ends.
        "while :; do :; done\nrepository=example-org/example-app\n",
    ],
    ids=["empty", "from the caller", "fails", "never ends"],
)
def test_runner_settings_that_name_a_repository_bash_cannot_read_are_refused(
    tmp_path: Path, text: str
) -> None:
    repo = _repository(tmp_path)
    _settings(repo, text)
    os.environ["WORKFLOW_SECRET"] = "example-org/from-the-caller"
    try:
        with pytest.raises(workflow_config.ConfigError, match=r"\.github/runner/settings\.sh"):
            workflow_config.load(repo)
    finally:
        del os.environ["WORKFLOW_SECRET"]


@pytest.mark.parametrize(
    "settings,origin,named",
    [
        ("repository=no-slash\n", None, r"^\.github/runner/settings\.sh: repository"),
        (None, "https://github.com/example-owner/..", r"^origin: repository"),
    ],
    ids=["runner settings", "origin"],
)
def test_a_repository_that_is_not_owner_name_is_refused_where_it_was_read(
    tmp_path: Path, settings: str | None, origin: str | None, named: str
) -> None:
    repo = _repository(tmp_path, origin)
    if settings is not None:
        _settings(repo, settings)

    with pytest.raises(workflow_config.ConfigError, match=named):
        workflow_config.load(repo)


def test_runner_settings_that_name_no_repository_leave_it_to_origin(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _settings(repo, "# Only the pool's own settings.\nrunner_count=2\n")

    assert workflow_config.load(repo).repository == "example-owner/example-app"


@pytest.mark.parametrize("repository", ["example-owner/.github", "example-owner/example.app"])
def test_a_repository_name_with_a_dot_is_read(tmp_path: Path, repository: str) -> None:
    repo = _repository(tmp_path)
    _write(repo, {"repository": repository})

    assert workflow_config.load(repo).repository == repository


@pytest.mark.parametrize("folder", ["state", ".local/workflow", "a/b/c"])
def test_a_state_folder_of_any_depth_leads_back_to_its_checkout(
    tmp_path: Path, folder: str
) -> None:
    repo = _repository(tmp_path)
    _write(repo, {"state_folder": folder})
    config = workflow_config.load(repo)

    assert config.state() == repo / folder
    assert config.checkout_of(config.state()) == repo
    worktree = repo / ".claude/worktrees/ticket-7"
    assert config.checkout_of(config.state(worktree)) == worktree


def test_a_worktree_reads_the_file_of_the_main_checkout(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _write(repo, {"state_folder": ".shipline/state"})
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(worktree))
    # A copy in the worktree, as on an old branch, is not what counts.
    _write(worktree, {"state_folder": ".elsewhere"})

    assert workflow_config.main_checkout(worktree) == repo
    assert workflow_config.load(worktree).state_folder == ".shipline/state"


@pytest.mark.parametrize(
    "text,named",
    [
        ("{", "not JSON"),
        ("[]", "an object"),
        ('{"repository": 7}', "repository"),
        ('{"repository": "no-slash"}', "repository"),
        # `repos/../..` would leave the repository's own API.
        ('{"repository": "../.."}', "repository"),
        ('{"repository": "example-owner/.."}', "repository"),
        ('{"repository": "./."}', "repository"),
        ('{"repository": "../example-app"}', "repository"),
        ('{"checks": {"ci_result": ""}}', "checks.ci_result"),
        ('{"checks": "CI result"}', "checks"),
        ('{"runner": {"labels": "home"}}', "runner.labels"),
        ('{"state_folder": "/var/workflow"}', "state_folder"),
        ('{"state_folder": "../workflow"}', "state_folder"),
        ('{"repositry": "example-owner/example-app"}', "repositry"),
        ('{"checks": {"ci_results": "CI result"}}', "checks.ci_results"),
    ],
)
def test_a_file_that_cannot_be_read_is_refused_by_name(
    tmp_path: Path, text: str, named: str
) -> None:
    repo = _repository(tmp_path)
    _write(repo, text)

    with pytest.raises(workflow_config.ConfigError, match=f"shipline.json.*{named}"):
        workflow_config.load(repo)
    done = subprocess.run(
        ["python3", str(_WORKFLOW / "workflow_config.py"), "shell", str(repo)],
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    assert done.stdout == ""
    assert "shipline.json" in done.stderr and named in done.stderr


@pytest.mark.parametrize(
    "origin", [None, "/srv/git/example-app.git", "https://git.example.com/example-owner/app.git"]
)
def test_no_repository_on_github_anywhere_is_refused(tmp_path: Path, origin: str | None) -> None:
    repo = _repository(tmp_path, origin=origin)

    config = workflow_config.load(repo)

    # What needs no repository is still read.
    assert config.state_folder == ".local/workflow"
    with pytest.raises(workflow_config.ConfigError, match="cannot tell which repository"):
        config.repository


_ODD = {
    "repository": "odd-owner/odd-app",
    "state_folder": "state with space/$HOME",
    "worktree_setup": "tools/set up 'it'",
    "checks": {"ci_result": 'Build "result" $(touch nope)'},
    "labels": {"priority": {"blocker": "p0: `now`"}},
    "runner": {
        "labels": ["home one", "home-two"],
        "prefix": "odd-app",
        "image_build": "tools/build 'image' $(touch built)",
    },
}


def _shell(repo: Path, script: str) -> str:
    """What a Bash script sees after it reads the values once."""
    done = subprocess.run(
        [
            "bash",
            "-c",
            'config=$(python3 "$1" shell "$2") || exit 1; eval "$config"; ' + script,
            "bash",
            str(_WORKFLOW / "workflow_config.py"),
            str(repo),
        ],
        cwd=repo,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_bash_reads_every_value_once_quoted_for_eval(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _write(repo, _ODD)

    printed = _shell(
        repo,
        'printf "%s\\n" "$SHIPLINE_REPOSITORY" "$SHIPLINE_STATE_FOLDER" '
        '"$SHIPLINE_WORKTREE_SETUP" "$SHIPLINE_CI_RESULT" "$SHIPLINE_CI_PLAN" '
        '"$SHIPLINE_E2E_PLAN" "$SHIPLINE_IMAGES" "$SHIPLINE_LABEL_WORKFLOW" '
        '"$SHIPLINE_LABEL_AUDITED" "$SHIPLINE_PRIORITY_BLOCKER" "$SHIPLINE_PRIORITY_HIGH" '
        '"$SHIPLINE_PRIORITY_MEDIUM" "$SHIPLINE_PRIORITY_LOW" "${#SHIPLINE_RUNNER_LABELS[@]}" '
        '"${SHIPLINE_RUNNER_LABELS[@]}" "$SHIPLINE_RUNNER_PREFIX" "$SHIPLINE_RUNNER_IMAGE_BUILD"',
    )

    assert printed.splitlines() == [
        "odd-owner/odd-app",
        "state with space/$HOME",
        "tools/set up 'it'",
        'Build "result" $(touch nope)',
        "CI: what to run",
        "E2E: what to run",
        "Docker images",
        "workflow",
        "audited",
        "p0: `now`",
        "priority: high",
        "priority: medium",
        "priority: low",
        "2",
        "home one",
        "home-two",
        "odd-app",
        "tools/build 'image' $(touch built)",
    ]
    assert not (repo / "nope").exists()
    assert not (repo / "built").exists()


def test_bash_without_a_file_reads_the_defaults(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    printed = _shell(
        repo,
        'printf "%s\\n" "$SHIPLINE_REPOSITORY" "$SHIPLINE_STATE_FOLDER" "$SHIPLINE_CI_RESULT" '
        '"${#SHIPLINE_RUNNER_LABELS[@]}" "$SHIPLINE_CONFIG" '
        '"${SHIPLINE_RUNNER_PREFIX-unset}" "${SHIPLINE_RUNNER_IMAGE_BUILD-unset}"',
    )

    assert printed.splitlines() == [
        "example-owner/example-app",
        ".local/workflow",
        "CI result",
        "0",
        "{}",
        "",
        "",
    ]


def _jq(config: str | None, labels: list[str], program: str = "priority_name") -> str:
    env = {**_GIT_ENV}
    if config is not None:
        env["SHIPLINE_CONFIG"] = config
    done = subprocess.run(
        ["jq", "-r", "-L", str(_WORKFLOW), f'include "priority"; {program}'],
        input=json.dumps({"labels": [{"name": name} for name in labels]}),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.mark.parametrize(
    "config", [None, "", "{}", '{"labels": {}}', '{"labels": {"priority": {}}}']
)
def test_jq_falls_back_to_the_default_labels(config: str | None) -> None:
    assert _jq(config, ["priority: blocker"]) == "blocker"
    assert _jq(config, ["priority: low"]) == "low"
    assert _jq(config, []) == "medium"
    assert _jq(config, ["workflow"], "workflow_label") == "workflow"


def test_bash_and_jq_read_the_same_labels(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _write(
        repo,
        {"labels": {"workflow": "shipline", "priority": {"blocker": "p0", "low": "p3"}}},
    )
    config = _shell(repo, 'printf "%s" "$SHIPLINE_CONFIG"')

    assert _jq(config, ["p0"]) == "blocker"
    assert _jq(config, ["p3", "priority: high"]) == "high"
    assert _jq(config, ["p3"]) == "low"
    # A default name the file replaced is no priority any more.
    assert _jq(config, ["priority: blocker"]) == "medium"
    assert _jq(config, [], "workflow_label") == "shipline"
    assert _shell(repo, 'printf "%s" "$SHIPLINE_LABEL_WORKFLOW"') == "shipline"


# Answers the merge gate's reads of the Actions API for one repository and its workflow files,
# and leaves everything else to the harness's stand-in: a read of any other repository or file
# finds no runs, and main's health is then unknown.
_ACTIONS_GH = r"""#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
api = "repos/@REPOSITORY@/actions/"
path = next((arg for arg in args if arg.startswith("repos/")), "")
if args[:1] == ["api"] and path.startswith(api):
    with open("@STATE@/calls.log", "a") as log:
        log.write("gh " + " ".join(args) + "\n")
    head = open("@STATE@/main-head").read().strip()
    found = re.match(r"actions/workflows/([^/]+)/runs[?](.*)", path[len("repos/@REPOSITORY@/"):])
    if found and found[1] == "@CI@" and f"head_sha={head}" in found[2]:
        print(json.dumps({"workflow_runs": [{
            "id": 501, "head_sha": head, "head_branch": "main", "event": "push",
            "status": "completed", "conclusion": "failure", "run_attempt": 1,
            "updated_at": "2026-10-20T10:00:00Z", "path": ".github/workflows/@CI@"}]}))
    elif found:
        print(json.dumps({"workflow_runs": []}))
    elif path.startswith(api + "runs/501/jobs"):
        print(json.dumps({"jobs": [
            {"id": 11, "name": "@CI_RESULT@", "status": "completed", "conclusion": "failure",
             "labels": ["ubuntu-latest"]}]}))
    else:
        sys.exit(f"gh: could not answer {' '.join(args)}")
    sys.exit(0)
os.execv("@HARNESS_GH@", ["gh", *args])
"""

_OTHER = {
    "repository": "example-owner/example-app",
    # One folder deep, where the default is two.
    "state_folder": ".shipline",
    "worktree_setup": "tools/prepare-worktree",
    "checks": {
        "ci_result": "Build result",
        "ci_plan": "Build: what to run",
        "e2e_plan": "Browser: what to run",
        "images": "Images",
    },
    "workflows": {"ci": "build.yml", "e2e": "browser.yml", "images": "container.yml"},
}


def _commit_on_main(workflow: Workflow, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = workflow.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
    workflow.git("add", *files)
    workflow.git("commit", "--quiet", "-m", "chore: use shipline.json")
    workflow.git("push", "--quiet", "origin", "main")


def test_another_repository_state_folder_and_check_names_reach_the_gate_the_tick_and_session(
    workflow: Workflow,
) -> None:
    _commit_on_main(
        workflow,
        {
            "shipline.json": json.dumps(_OTHER),
            "tools/prepare-worktree": '#!/bin/bash\nexec worktree-setup "$@"\n',
        },
    )
    (workflow.state / "main-head").write_text(workflow.git("rev-parse", "main"))
    harness_gh = workflow.bin / "gh-harness"
    (workflow.bin / "gh").rename(harness_gh)
    replacements = {
        "@REPOSITORY@": _OTHER["repository"],
        "@CI@": "build.yml",
        "@CI_RESULT@": "Build result",
        "@HARNESS_GH@": str(harness_gh),
    }
    gh = _ACTIONS_GH
    for placeholder, value in replacements.items():
        gh = gh.replace(placeholder, value)
    workflow.fake("gh", gh)
    state = Path(_OTHER["state_folder"])

    # A draft that gets its worker.
    workflow.add_pull_request(8)
    # A ready pull request whose reviewer waits, idle, for checks that have now finished.
    workflow.add_worktree(7, draft=False)
    workflow.running("review-7")
    notes = workflow.worktree(7) / state
    notes.mkdir(parents=True)
    started = notes / "review-7.started"
    started.touch()
    os.utime(started, (1_790_000_000, 1_790_000_000))
    (notes / "review-7.idle").touch()
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    box = "\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n"
    workflow.screen("● The checks are still running; I am waiting for them.\n" + box, "review-7")
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [
                {
                    "body": "<!-- workflow-review round=1 result=passed -->",
                    "commit_id": head,
                    "submitted_at": "2026-09-21T15:00:00Z",
                }
            ]
        )
    )
    (workflow.state / "rollup-7.json").write_text(
        json.dumps(
            [
                {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
                 "conclusion": "SUCCESS"}
                for name in ("Build result", "Images: what to run", "E2E")
            ]
        )
    )  # fmt: skip

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    # The gate: main's health, from the named repository, workflow file and check.
    status = (workflow.repo / state / "main-status").read_text()
    assert status.startswith("main: red since 2026-10-20T10:00:00Z, Build result ("), status
    assert any(
        "repos/example-owner/example-app/actions/workflows/build.yml/runs?head_sha=" in call
        for call in workflow.calls("gh api")
    )
    # The tick: the reviewer's round read from the named repository, and the finished checks
    # by their names.
    assert "gh api --paginate repos/example-owner/example-app/pulls/7/reviews" in workflow.calls()
    assert len(workflow.calls("tmux review-7 send-keys -t =review-7: -l The checks on ")) == 1
    assert (workflow.repo / state / f"review-7.checks-nudged-{head}").exists()
    # `session`: the worker's worktree set up by the named command, its notes in the folder.
    assert workflow.is_running("ticket-8")
    assert (workflow.worktree(8) / state / "ticket-8.url").exists()
    assert any(
        call.startswith("worktree-setup ") and call.endswith(str(workflow.worktree(8)))
        for call in workflow.calls()
    )
    assert (workflow.repo / ".git" / "shipline-tick.lock").exists()
    # Nothing of the defaults: no other repository asked, no default state folder made.
    assert not [call for call in workflow.calls("gh") if "example-org" in call]
    for checkout in (workflow.repo, workflow.worktree(7), workflow.worktree(8)):
        assert not (checkout / ".local" / "workflow").exists(), checkout


def _texts_sent_to_models(workflow: Workflow, high: str = "priority:+high") -> dict[str, str]:
    """The worker's sentence, the cloud worker's, the wake's message and the review prompt, as
    the scripts write them for Shipline's own repository: no runner settings, and `origin` is
    Shipline on GitHub."""
    (workflow.repo / ".github/runner/settings.sh").unlink()
    local = workflow.git("remote", "get-url", "origin")
    workflow.git("remote", "set-url", "origin", "https://github.com/NazmievBulat/Shipline.git")
    workflow.git("config", f"url.{local}.insteadOf", "https://github.com/NazmievBulat/Shipline.git")
    (workflow.repo / "AGENTS.md").write_text("Rules from main\n")
    workflow.git("add", "AGENTS.md")
    workflow.git("commit", "--quiet", "-m", "docs: main rules")
    workflow.git("push", "--quiet", "origin", "main")
    texts: dict[str, str] = {}

    workflow.add_pull_request(7)
    assert workflow.run_script("tick").returncode == 0
    texts["worker"] = (workflow.state / "args" / "ticket-7").read_text().splitlines()[-1]

    workflow.add_pull_request(8)
    done = workflow.run("start", "worker", "8", "cloud")
    assert done.returncode == 0, done.stderr
    texts["cloud"] = (workflow.state / "cloud-args").read_bytes().decode().split("\0")[-2]

    workflow.add_worktree(9, draft=False)
    workflow.fake("bwrap", '#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n')
    workflow.fake("codex", '#!/bin/bash\nprintf "%s" "${@: -1}" > "@STATE@/review-prompt"\n')
    done = workflow.run_script("review-panel", "9", "codex")
    assert done.returncode == 0, done.stderr
    texts["review"] = (workflow.state / "review-prompt").read_text()

    workflow.add_pull_request(12, draft=False, labels=("workflow", high))
    workflow.add_pull_request(13, labels=())
    workflow.orchestrator("idle")
    # What the tick's own wake looked at and told is forgotten: this wake tells it all again.
    for told in (workflow.root / "home/.local/state/workflow").glob("wake.*"):
        told.unlink()
    moment = "1791946800"  # 2026-10-13, 23:00 in New York: the night's check-in.
    done = workflow.run_script("wake", env={"WORKFLOW_NOW": moment, "TZ": "America/New_York"})
    assert done.returncode == 0, done.stderr
    texts["wake"] = workflow.sent()[-1].replace(str(workflow.root), "<root>")
    return texts


# The SHA-256 of each text as the scripts wrote it before they read `shipline.json`.
_TEXTS_BEFORE = {
    "worker": "a1ada6cfb39264bb4b6501807f3feb74fbf51c2ea3c32c5b5b56f77a23838b36",
    "cloud": "9603fac3798a46497342dda2b64491c2523c1e771d7620bb78e15b034c9d7bef",
    "review": "ea932fce613e8e9c075daa884a2adee29b64ee5610f1fcd922f27aa6cd39c49a",
    "wake": "af21558438c99d72e165c8fb8ab4febdd80204e43f532a399697b4131441d080",
}


def test_with_the_defaults_the_texts_sent_to_models_are_as_before(workflow: Workflow) -> None:
    texts = _texts_sent_to_models(workflow)

    assert "Run the repository's setup command, scripts/setup-worktree, when" in texts["cloud"]
    assert texts["review"].startswith(
        "You are an independent reviewer of pull request 9 in NazmievBulat/Shipline."
    )
    assert "ready high 12" in texts["wake"]
    hashes = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in texts.items()}
    assert hashes == _TEXTS_BEFORE


def test_the_texts_sent_to_models_take_the_named_values(workflow: Workflow) -> None:
    _commit_on_main(
        workflow,
        {
            "shipline.json": json.dumps(
                {
                    "repository": "example-owner/example-app",
                    "worktree_setup": "tools/prepare-worktree",
                    "labels": {"workflow": "workflow", "priority": {"high": "urgent"}},
                }
            )
        },
    )

    texts = _texts_sent_to_models(workflow, high="urgent")

    assert "Run the repository's setup command, tools/prepare-worktree, when" in texts["cloud"]
    assert texts["review"].startswith(
        "You are an independent reviewer of pull request 9 in example-owner/example-app."
    )
    assert "ready high 12" in texts["wake"]


_NAMED = {
    "state_folder": ".shipline/state",
    "labels": {"workflow": "shipline", "priority": {"high": "urgent"}},
}


def test_python_bash_node_and_jq_read_the_same_values(workflow: Workflow) -> None:
    _commit_on_main(workflow, {"shipline.json": json.dumps(_NAMED)})
    scripts = workflow.repo / "scripts/workflow"
    python = subprocess.run(
        [
            "python3",
            "-c",
            "import workflow_config, priority; config = workflow_config.current(); "
            "print(config.state_folder, config.labels.workflow, "
            "priority.of([{'name': 'urgent'}]), priority.of([{'name': 'priority: high'}]))",
        ],
        cwd=scripts,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    bash = subprocess.run(
        [
            "bash",
            "-c",
            'eval "$(python3 workflow_config.py shell)"; export SHIPLINE_CONFIG; '
            'echo "$SHIPLINE_STATE_FOLDER" "$SHIPLINE_LABEL_WORKFLOW"; '
            "for name in urgent 'priority: high'; do "
            '  jq -nr -L . --arg name "$name" '
            "    'include \"priority\"; {labels: [{name: $name}]} | priority_name'; "
            "done; jq -nr -L . 'include \"priority\"; workflow_label'",
        ],
        cwd=scripts,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert python == [".shipline/state", "shipline", "high", "medium"]
    assert bash == [".shipline/state", "shipline", "high", "medium", "shipline"]

    # Node: the bridge reads the turn Bash's `notify` wrote, both in the named state folder.
    workflow.add_worktree(7)
    notes = workflow.worktree(7) / ".shipline/state"
    notes.mkdir(parents=True)
    (notes / "ticket-7.codex").touch()
    workflow.running("ticket-7")
    workflow.running("codex-7")
    payload = {"type": "agent-turn-complete", "last-assistant-message": "Type 1.", "turn-id": "1"}
    done = workflow.run_script(
        "notify",
        "codex",
        json.dumps(payload),
        env={"WORKFLOW_SESSION": "ticket-7", "WORKFLOW_CODEX_BRIDGE": "1"},
    )
    assert done.returncode == 0, done.stderr
    assert (notes / "ticket-7.codex-turn.json").exists()
    bridge = subprocess.Popen(
        ["node", str(scripts / "codex-bridge.mjs"), str(workflow.worktree(7)), "ticket-7"],
        env=workflow.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "await_turn"}}  # fmt: skip
        answer, _ = bridge.communicate(json.dumps(request) + "\n", timeout=10)
    finally:
        bridge.kill()
    assert json.loads(answer.splitlines()[0])["result"]["content"][0]["text"] == "Type 1."
    assert not (workflow.worktree(7) / ".local").exists()


def test_the_named_labels_are_what_the_tick_starts_by_and_install_timer_creates(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _commit_on_main(
        workflow,
        {"shipline.json": json.dumps({"labels": {"workflow": "shipline", "audited": "checked"}})},
    )
    workflow.add_pull_request(7, labels=("shipline",))
    workflow.add_pull_request(8, labels=("workflow",))

    done = workflow.run_script("tick")

    assert workflow.is_running("ticket-7"), done.stderr
    assert not workflow.is_running("ticket-8")

    done = workflow.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    assert (workflow.state / "labels").read_text().split() == ["shipline", "checked"]


def test_the_identity_folder_and_repository_of_the_runners_come_from_the_file(
    workflow: Workflow,
) -> None:
    scripts = workflow.repo / "scripts/workflow"

    def folder() -> str:
        return subprocess.run(
            ["python3", "-c", "import github_identity; print(github_identity.folder())"],
            cwd=scripts,
            env=workflow.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def install(*repository: str) -> str:
        token = workflow.root / "token"
        token.write_text("github_pat_EXAMPLE\n")
        subprocess.run(
            ["python3", "github_identity.py", "install", *repository, "--token", str(token)],
            cwd=scripts,
            env=workflow.env,
            capture_output=True,
            text=True,
            check=False,
        )
        # The stand-in cannot answer the first check of the new identity; it names the repository.
        return workflow.calls("gh api repos/")[-1]

    assert folder() == str(workflow.root / "home/.config/shipline-runner")
    # The runner settings' repository, as the harness holds them.
    assert install().startswith("gh api repos/example-org/example-app/")

    _commit_on_main(
        workflow,
        {
            "shipline.json": json.dumps(
                {
                    "repository": "example-owner/example-app",
                    "runner": {"identity_folder": "~/.config/example-app-runner"},
                }
            )
        },
    )

    assert folder() == str(workflow.root / "home/.config/example-app-runner")
    assert install().startswith("gh api repos/example-owner/example-app/")
    flagged = install("--repository", "flag-owner/flag-app")
    assert flagged.startswith("gh api repos/flag-owner/flag-app/")


_GATE_READS = """
import json, subprocess, sys, types
sys.path.insert(0, "scripts/workflow")
# A repository's proof helper, so the gate words a squash's proof.
sys.modules["run_proof"] = types.SimpleNamespace(
    RECEIPT="Workflow-Proof: ", WHOLE="whole", passed_jobs=lambda parts: {"CI: web"}
)
import merge_gate as g

head, tree = "1" * 40, "2" * 40
names = json.loads(sys.argv[1])
g.api = lambda path, key=None, **given: [
    {"title": names["merge_tested"], "message": head},
    {"title": names["server_tests"], "message": "whole"},
    {"title": names["web_suite"], "message": "whole"},
    {"title": names["files_tested"], "message": tree},
    {"title": names["browser_suite"], "message": "whole"},
]
run = {"id": 5, "status": "completed", "conclusion": "success", "run_attempt": 1}
done = {"status": "completed", "conclusion": "success", "labels": []}
ci = [
    {"id": 1, "name": names["ci_plan"], **done},
    {"id": 2, "name": names["ci_result"], **done},
    {"id": 3, "name": "CI: server tests", **done},
    {"id": 8, "name": "CI: web", **done},
]
e2e = [{"id": 4, "name": names["e2e_plan"], **done}, {"id": 6, "name": "playwright", **done}]
failed = [{"id": 7, "name": "CI: lint", "status": "completed", "conclusion": "failure",
           "labels": ["self-hosted", "example-app-home"]}]
try:
    images = g.images_needed("HEAD", "HEAD~1", [], workflow_ref="HEAD")
except g.Refused:
    images = None
print(json.dumps({
    "every server test": g.whole_server_tests(run, ci, head),
    "fewer server tests": g.server_tests_selected(ci),
    "whole browser suite": g.whole_suite(run, e2e, tree),
    "whole web suite": '"CI: web"' in g.squash_proof(head, tree, {"ci": (run, ci)}),
    "images by their workflow": images,
    "re-run at home": g._rerunnable(run, failed, first=True),
}))
"""

_IMAGES_WORKFLOW = """name: Images
on:
  pull_request:
    paths:
      - "docker/**"
  push:
    branches: [main]
"""


def _gate_reads(workflow: Workflow, names: dict[str, str]) -> dict[str, bool | None]:
    done = subprocess.run(
        ["python3", "-B", "-c", _GATE_READS, json.dumps(names)],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_gate_reads_the_named_checks_titles_workflow_files_and_runner_labels(
    workflow: Workflow,
) -> None:
    default = {**_DEFAULTS["annotations"], **_DEFAULTS["checks"]}
    named = {
        "merge_tested": "Merge built",
        "files_tested": "Tree tested",
        "server_tests": "Server suite",
        "web_suite": "Web unit suite",
        "browser_suite": "Browser specs",
        "ci_result": "Build result",
        "ci_plan": "Build: what to run",
        "e2e_plan": "Browser: what to run",
        "images": "Images",
    }
    proven = {
        "every server test": True,
        "fewer server tests": False,
        "whole browser suite": True,
        "whole web suite": True,
        "images by their workflow": True,
        "re-run at home": True,
    }
    # A change under docker/ that the image workflow's path filter names.
    _commit_on_main(
        workflow,
        {".github/workflows/images.yml": _IMAGES_WORKFLOW, "docker/app/Dockerfile": "FROM x\n"},
    )
    # The harness's repository names its home runners and nothing else: the defaults count.
    assert _gate_reads(workflow, default) == proven

    # The same workflow as container.yml, with every name of the file, in one commit.
    workflow.git("rm", "--quiet", ".github/workflows/images.yml")
    _commit_on_main(
        workflow,
        {
            ".github/workflows/container.yml": _IMAGES_WORKFLOW,
            "docker/app/Dockerfile": "FROM y\n",
            "shipline.json": json.dumps(
                {
                    "annotations": {key: named[key] for key in _DEFAULTS["annotations"]},
                    "checks": {key: named[key] for key in _DEFAULTS["checks"]},
                    "workflows": {"images": "container.yml"},
                    "runner": {"labels": ["example-app-elsewhere"]},
                }
            ),
        },
    )

    assert _gate_reads(workflow, named) == {**proven, "re-run at home": False}
    unproven = {
        "every server test": False,
        "fewer server tests": True,
        "whole browser suite": False,
        "whole web suite": False,
        "images by their workflow": True,
        "re-run at home": False,
    }
    # The default titles, on checks of the named names: nothing is proven by them.
    checks = {key: named[key] for key in ("ci_plan", "e2e_plan", "ci_result")}
    assert _gate_reads(workflow, {**default, **checks}) == unproven


def test_without_a_repository_only_what_needs_one_stops(workflow: Workflow) -> None:
    # The harness's origin is a folder: with no runner settings, nothing names the repository.
    (workflow.repo / ".github/runner/settings.sh").unlink()
    scripts = workflow.repo / "scripts/workflow"
    shell = subprocess.run(
        ["python3", str(scripts / "workflow_config.py"), "shell"],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert shell.returncode == 0, shell.stderr
    assert "SHIPLINE_REPOSITORY=''\n" in shell.stdout
    assert "SHIPLINE_STATE_FOLDER=.local/workflow\n" in shell.stdout

    listed = workflow.run("list")
    assert listed.returncode == 0, listed.stderr

    tick = workflow.run_script("tick")
    assert tick.returncode == 1
    assert "cannot tell which repository" in tick.stderr


@pytest.mark.parametrize("file", ["valid", "refused"])
def test_the_codex_gh_takes_no_value_from_its_callers_environment(
    workflow: Workflow, file: str
) -> None:
    if file == "refused":
        _commit_on_main(workflow, {"shipline.json": '{"repositry": "example-owner/example-app"}'})
    body = workflow.root / "body.md"
    body.write_text("rewritten\n")

    def codex_gh(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(workflow.repo / "scripts/workflow/codex-bin/gh"), *args],
            cwd=workflow.repo,
            env={
                **workflow.env,
                "WORKFLOW_SESSION": "ticket-7",
                "WORKFLOW_GH": str(workflow.bin / "gh"),
                **env,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    # `workflow` would start the ticket, and another repository's pull request 7 is not this
    # worker's, whatever the caller's environment says.
    label = codex_gh(
        "pr", "create", "--draft", "--label", "workflow", "--title", "t", "--body", "b",
        SHIPLINE_PRIORITY_MEDIUM="workflow",
        SHIPLINE_PRIORITY_HIGH="workflow",
    )
    other = codex_gh(
        "api", "-X", "PATCH", "repos/example-owner/other-app/pulls/7", "-F", f"body=@{body}",
        SHIPLINE_REPOSITORY="example-owner/other-app",
    )

    for done in (label, other):
        assert done.returncode == 1
        assert "this command is refused" in done.stderr, done.stderr
    assert not workflow.calls("gh pr create")
    assert not workflow.calls("gh api")


@pytest.mark.parametrize("renamed", [False, True], ids=["same folder", "renamed folder"])
def test_a_second_tick_waits_for_the_first_across_a_change_of_the_state_folder(
    workflow: Workflow, renamed: bool
) -> None:
    # The lock is in the git common directory, which no configuration moves.
    common = Path(workflow.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    with (common / "shipline-tick.lock").open("w") as first_tick:
        fcntl.flock(first_tick, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if renamed:
            # The owner pulls a main that renames the state folder while the first tick runs.
            _commit_on_main(workflow, {"shipline.json": json.dumps({"state_folder": ".shipline"})})

        done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert "the tick before this one is still running" in done.stdout, done.stdout + done.stderr
    assert not workflow.calls("gh")
