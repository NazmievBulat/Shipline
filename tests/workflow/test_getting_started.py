"""A newcomer installs Shipline and merges a first pull request through it, as
`docs/getting-started.md` says. `shipline install <repo-path>` checks what Shipline needs and
says each thing that is missing, changing nothing then; otherwise it creates the labels and
writes the timer and the hooks. Then a sample repository set up from nothing, by the guide's own
commands, gets one ticket that a stand-in worker builds, a stand-in reviewer hands off and the
gate merges."""

import atexit
import json
import os
import shutil
import tempfile
from functools import cache
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline

_REPOSITORY = "example-org/example-app"
_PRIORITIES = ["priority: blocker", "priority: high", "priority: medium", "priority: low"]
_LABELS = ["workflow", "audited", *_PRIORITIES]
# Where the programs of the machine are linked, each folder without one of them.
_HIDDEN = Path(tempfile.mkdtemp(prefix="shipline-path-"))
atexit.register(shutil.rmtree, _HIDDEN, ignore_errors=True)


@cache
def _path_without(name: str) -> str:
    """A folder that holds every program on the test's `PATH` but `name`, as links."""
    folder = _HIDDEN / name
    folder.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            link = folder / entry.name
            if entry.name == name or link.is_symlink():
                continue
            if entry.is_file() and os.access(entry.path, os.X_OK):
                link.symlink_to(entry.path)
    return str(folder)


def _node(workflow: Workflow, version: str = "v22.12.0") -> None:
    workflow.fake("node", f'#!/bin/bash\n[ "$1" = --version ] && echo {version}\n')


@pytest.fixture
def workflow(workflow: Workflow) -> Workflow:
    """The harness's repository, with Shipline in a checkout of its own and Node 22."""
    install_shipline(workflow)
    _node(workflow)
    return workflow


def _hide(workflow: Workflow, name: str) -> None:
    """`name` is on neither the stand-ins' folder nor the machine's `PATH`."""
    (workflow.bin / name).unlink(missing_ok=True)
    workflow.env["PATH"] = f"{workflow.bin}:{_path_without(name)}"


def _changed_nothing(workflow: Workflow, labels: str) -> None:
    home = Path(workflow.env["HOME"])
    assert not (home / ".config").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".local" / "bin").exists()
    assert (workflow.state / "labels").read_text() == labels
    assert workflow.calls("gh label create") == []
    assert [call for call in workflow.calls("systemctl") if "show-environment" not in call] == []


def _hooks(workflow: Workflow) -> list[str]:
    settings = Path(workflow.env["HOME"]) / ".claude" / "settings.json"
    hooks = json.loads(settings.read_text())["hooks"]
    return [hook["command"] for entries in hooks.values() for e in entries for hook in e["hooks"]]


@pytest.mark.parametrize("named", ["absolute", "relative"])
def test_install_writes_the_timer_and_the_hooks_and_creates_the_labels(
    workflow: Workflow, named: str
) -> None:
    (workflow.state / "labels").write_text("bug\nworkflow\n")
    path, cwd = (str(workflow.repo), workflow.root) if named == "absolute" else (".", workflow.repo)

    done = workflow.shipline("install", path, cwd=cwd)

    assert done.returncode == 0, done.stderr
    assert (workflow.state / "labels").read_text().splitlines() == ["bug", *_LABELS]
    assert workflow.calls("gh label create priority") == [
        "gh label create priority: blocker --color B60205 --description Stops every merge or push"
        " now: a red main, a push refused for everyone, a deadlocked train",
        "gh label create priority: high --color D93F0B --description Removes a bottleneck: CI"
        " capacity, a flaky test, a stalled or slow workflow",
        "gh label create priority: medium --color FBCA04 --description Anything else",
        "gh label create priority: low --color CCCCCC --description Only when the owner says so",
    ]
    home = Path(workflow.env["HOME"])
    service = (home / ".config/systemd/user/workflow-tick.service").read_text().splitlines()
    launcher = workflow.home / "bin" / "shipline"
    assert f'Environment="SHIPLINE_REPO={workflow.repo}"' in service
    assert f'ExecStart="{launcher}" tick' in service
    assert "systemctl --user enable --now workflow-tick.timer" in workflow.calls("systemctl")
    assert set(_hooks(workflow)) == {
        f"bash {launcher} --repo {workflow.repo} notify 2>/dev/null || true"
    }
    assert (home / ".local" / "bin" / "shipline").resolve() == launcher.resolve()
    assert done.stdout.splitlines()[-1] == (
        f"Shipline is installed for {_REPOSITORY}: the tick runs every 3 minutes"
    )


def test_installing_again_adds_nothing(workflow: Workflow) -> None:
    first = workflow.shipline("install", str(workflow.repo))
    labels = (workflow.state / "labels").read_text()

    again = workflow.shipline("install", str(workflow.repo))

    assert first.returncode == again.returncode == 0, first.stderr + again.stderr
    assert (workflow.state / "labels").read_text() == labels
    assert len(workflow.calls("gh label create")) == len(_LABELS)
    assert "the workflow hooks are already in" in again.stdout


# What is missing, how the test takes it away, and the one line that names it.
_MISSING = {
    "linux": ("uname", "#!/bin/bash\necho Darwin\n", "missing: Linux (this is Darwin)"),
    "systemd": (
        "systemctl",
        '#!/bin/bash\necho "systemctl $*" >> "@STATE@/calls.log"\n'
        'echo "Failed to connect to bus" >&2\nexit 1\n',
        "missing: systemd user units (systemctl --user cannot reach the user's service manager)",
    ),
    "tmux": ("tmux", None, "missing: tmux"),
    "git": ("git", None, "missing: git"),
    "jq": ("jq", None, "missing: jq"),
    "gh": ("gh", None, "missing: gh"),
    "signed-out": ("gh", "signed-out", "missing: gh signed in (run: gh auth login)"),
    "old-python": (
        "python3",
        "#!/bin/bash\necho 3.13\n",
        "missing: python3 3.14 or later (found 3.13)",
    ),
    "node": ("node", None, "missing: node 22 or later"),
    "old-node": ("node", "#!/bin/bash\necho v20.11.1\n", "missing: node 22 or later (found 20.11.1)"),
    "claude": ("claude", None, "missing: claude (Claude Code)"),
}


@pytest.mark.parametrize("case", list(_MISSING))
def test_install_says_exactly_what_is_missing_and_changes_nothing(
    workflow: Workflow, case: str
) -> None:
    program, stand_in, line = _MISSING[case]
    if stand_in is None:
        _hide(workflow, program)
    elif stand_in == "signed-out":
        (workflow.state / "signed-out").touch()
    else:
        workflow.fake(program, stand_in)
    (workflow.state / "labels").write_text("bug\n")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    assert done.stderr.splitlines() == [line, "nothing was installed"]
    _changed_nothing(workflow, "bug\n")


def test_install_names_every_requirement_that_is_missing_at_once(workflow: Workflow) -> None:
    _hide(workflow, "tmux")
    workflow.env["PATH"] = workflow.env["PATH"].replace(_path_without("tmux"), _path_without("jq"))
    (workflow.state / "signed-out").touch()
    (workflow.state / "labels").write_text("")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    assert done.stderr.splitlines() == [
        "missing: jq",
        "missing: gh signed in (run: gh auth login)",
        "nothing was installed",
    ]
    _changed_nothing(workflow, "")


def test_codex_is_not_needed(workflow: Workflow) -> None:
    _hide(workflow, "codex")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 0, done.stderr
    assert "codex not found: only Codex workers need it" in done.stdout.splitlines()


def test_labels_the_account_may_not_create_are_named_with_their_command(
    workflow: Workflow,
) -> None:
    (workflow.state / "labels").write_text("workflow\npriority: high\n")
    (workflow.state / "no-push").touch()

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    assert done.stderr.splitlines() == [
        f"missing: labels on {_REPOSITORY} that this gh account may not create;"
        " someone who may can create them with:",
        f"  gh label create audited --repo {_REPOSITORY}",
        f"  gh label create priority:\\ blocker --repo {_REPOSITORY}",
        f"  gh label create priority:\\ medium --repo {_REPOSITORY}",
        f"  gh label create priority:\\ low --repo {_REPOSITORY}",
        "nothing was installed",
    ]
    _changed_nothing(workflow, "workflow\npriority: high\n")


def test_labels_that_are_all_there_need_no_right_to_create_them(workflow: Workflow) -> None:
    (workflow.state / "labels").write_text("\n".join(_LABELS) + "\n")
    (workflow.state / "no-push").touch()

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 0, done.stderr
    assert workflow.calls("gh label create") == []
    assert not [call for call in workflow.calls("gh") if call.startswith("gh api repos/")]


def test_labels_that_cannot_be_read_install_nothing(workflow: Workflow) -> None:
    (workflow.state / "fail-gh-labels").touch()
    (workflow.state / "labels").write_text("")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    # After what gh said itself.
    assert done.stderr.splitlines()[-2:] == [
        f"cannot read the labels of {_REPOSITORY} with gh",
        "nothing was installed",
    ]
    _changed_nothing(workflow, "")


@pytest.mark.parametrize("problem", ["no-folder", "no-repository", "bad-config"])
def test_a_repository_that_cannot_be_read_installs_nothing(workflow: Workflow, problem: str) -> None:
    folder = workflow.root / "elsewhere"
    if problem == "no-repository":
        folder.mkdir()
    if problem == "bad-config":
        folder = workflow.repo
        (folder / "shipline.json").write_text('{"label": {}}\n')
    (workflow.state / "labels").write_text("")

    done = workflow.shipline("install", str(folder))

    assert done.returncode == 1
    assert done.stderr.splitlines() == [
        {
            "no-folder": f"{folder} is no folder",
            "no-repository": f"{folder} is in no git repository",
            "bad-config": "shipline.json: unknown key label",
        }[problem],
        "nothing was installed",
    ]
    _changed_nothing(workflow, "")


@pytest.mark.parametrize("args", [[], ["one", "two"], [""]])
def test_install_names_one_repository(workflow: Workflow, args: list[str]) -> None:
    done = workflow.shipline("install", *args)

    assert done.returncode == 2
    assert done.stderr == "usage: shipline install <repo-path>\n"
