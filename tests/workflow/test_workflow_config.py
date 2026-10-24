"""One file, `shipline.json` at the root of the main checkout, names what differs between the
repositories that use Shipline; `workflow_config.py` reads it, key by key over its defaults."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

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
    "runner": {"labels": [], "identity_folder": "~/.config/shipline-runner"},
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


def test_no_repository_anywhere_is_refused(tmp_path: Path) -> None:
    repo = _repository(tmp_path, origin=None)

    with pytest.raises(workflow_config.ConfigError, match="repository"):
        workflow_config.load(repo)


_ODD = {
    "repository": "odd-owner/odd-app",
    "state_folder": "state with space/$HOME",
    "worktree_setup": "tools/set up 'it'",
    "checks": {"ci_result": 'Build "result" $(touch nope)'},
    "labels": {"priority": {"blocker": "p0: `now`"}},
    "runner": {"labels": ["home one", "home-two"]},
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
        '"${SHIPLINE_RUNNER_LABELS[@]}"',
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
    ]
    assert not (repo / "nope").exists()


def test_bash_without_a_file_reads_the_defaults(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    printed = _shell(
        repo,
        'printf "%s\\n" "$SHIPLINE_REPOSITORY" "$SHIPLINE_STATE_FOLDER" "$SHIPLINE_CI_RESULT" '
        '"${#SHIPLINE_RUNNER_LABELS[@]}" "$SHIPLINE_CONFIG"',
    )

    assert printed.splitlines() == ["example-owner/example-app", ".local/workflow", "CI result", "0", "{}"]


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


@pytest.mark.parametrize("config", [None, "{}", '{"labels": {}}', '{"labels": {"priority": {}}}'])
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
