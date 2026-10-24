"""`shipline ci-tests-for-changes` chooses the tests CI will run for a change, and a cloud
worker's push runs them first (`ci_tests_for_changes.py`)."""

import importlib.util
import json
import re
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


def _config(tmp_path: Path, given: dict[str, Any]) -> Any:
    repo = tmp_path / "repo"
    repo.mkdir()
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
