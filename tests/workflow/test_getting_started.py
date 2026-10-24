"""A newcomer installs Shipline and merges a first pull request through it, as
`docs/getting-started.md` says. `shipline install <repo-path>` checks what Shipline needs and
says each thing that is missing, changing nothing then; otherwise it creates the labels and
writes the timer and the hooks. Then a sample repository set up from nothing, by the guide's own
commands, gets one ticket that a stand-in worker builds, a stand-in reviewer hands off and the
gate merges."""

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest
from stand_in_server import StandInServer
from workflow.test_ci import _job
from workflow.test_ci_proof import annotations
from workflow.test_merge_when_current import _GH, GitHub
from workflow_harness import Workflow, install_shipline

_REPOSITORY = "example-org/example-app"
_PRIORITIES = ["priority: blocker", "priority: high", "priority: medium", "priority: low"]
_LABELS = ["workflow", "audited", *_PRIORITIES]
# Where the programs of the machine are linked, each folder without one of them.
_HIDDEN = Path(tempfile.mkdtemp(prefix="shipline-path-"))
atexit.register(shutil.rmtree, _HIDDEN, ignore_errors=True)


@cache
def _path_without(*names: str) -> str:
    """A folder that holds every program on the test's `PATH` but `names`, as links."""
    folder = _HIDDEN / "-".join(names)
    folder.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            link = folder / entry.name
            if entry.name in names or link.is_symlink():
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


def _hide(workflow: Workflow, *names: str) -> None:
    """`names` are on neither the stand-ins' folder nor the machine's `PATH`."""
    for name in names:
        (workflow.bin / name).unlink(missing_ok=True)
    workflow.env["PATH"] = f"{workflow.bin}:{_path_without(*names)}"


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
    assert (workflow.state / "labels").read_text().splitlines() == [
        "bug",
        "workflow",
        *_PRIORITIES,
        "audited",
    ]
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
    "old-node": (
        "node",
        "#!/bin/bash\necho v20.11.1\n",
        "missing: node 22 or later (found 20.11.1)",
    ),
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
    _hide(workflow, "tmux", "jq")
    (workflow.state / "signed-out").touch()
    (workflow.state / "labels").write_text("")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    assert done.stderr.splitlines() == [
        "missing: tmux",
        "missing: jq",
        "missing: gh signed in (run: gh auth login)",
        "nothing was installed",
    ]
    _changed_nothing(workflow, "")


def test_settings_that_are_no_json_object_install_nothing(workflow: Workflow) -> None:
    settings = Path(workflow.env["HOME"]) / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("[]\n")
    (workflow.state / "labels").write_text("")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    assert done.stderr.splitlines() == [
        f"{settings} is not a JSON object, where the hooks go",
        "nothing was installed",
    ]
    assert settings.read_text() == "[]\n"
    shutil.rmtree(settings.parent)
    _changed_nothing(workflow, "")


def test_the_timer_starts_last(workflow: Workflow) -> None:
    """A step that fails leaves no tick running for sessions without their hooks or labels."""
    (workflow.state / "labels").write_text("workflow\naudited\n")
    (workflow.state / "fail-gh-label-create").touch()

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode != 0
    assert workflow.calls("systemctl") == ["systemctl --user show-environment"]
    assert not (Path(workflow.env["HOME"]) / ".config").exists()


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
        " someone allowed to push to it can create them with:",
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


def test_labels_there_in_another_case_are_kept_and_named_for_the_config(
    workflow: Workflow,
) -> None:
    """GitHub holds one label per name in any case; the tick and the gate read the names
    shipline.json gives exactly, so a label GitHub spells otherwise is named for the file."""
    (workflow.state / "labels").write_text("Workflow\nPriority: High\nbug\n")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 0, done.stderr
    assert [call.split(" --color")[0] for call in workflow.calls("gh label create")] == [
        "gh label create priority: blocker",
        "gh label create priority: medium",
        "gh label create priority: low",
        "gh label create audited",
    ]
    assert done.stdout.splitlines()[:2] == [
        'note: GitHub spells the label "workflow" as "Workflow"; the tick and the gate read'
        ' labels by their exact names, so set labels.workflow to "Workflow" in shipline.json',
        'note: GitHub spells the label "priority: high" as "Priority: High"; the tick and the'
        ' gate read labels by their exact names, so set labels.priority.high to'
        ' "Priority: High" in shipline.json',
    ]


def test_a_refusal_names_only_labels_missing_in_any_case(workflow: Workflow) -> None:
    (workflow.state / "labels").write_text("WORKFLOW\npriority: High\nAudited\n")
    (workflow.state / "no-push").touch()

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 1
    named = [re.search(r"gh label create (.+) --repo", line) for line in done.stderr.splitlines()]
    assert [found[1] for found in named if found] == [
        "priority:\\ blocker",
        "priority:\\ medium",
        "priority:\\ low",
    ]


def test_every_call_to_github_gives_up_after_30_seconds(workflow: Workflow) -> None:
    """The timer step's own calls too: a stalled connection never hangs the install."""
    workflow.trace_timeouts()
    (workflow.state / "labels").write_text("")

    done = workflow.shipline("install", str(workflow.repo))

    assert done.returncode == 0, done.stderr
    called = workflow.calls("gh ")
    assert called and any(call.startswith("gh label list") for call in called[1:])
    assert workflow.calls("timeout ") == ["timeout 30"] * len(called)


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
def test_a_repository_that_cannot_be_read_installs_nothing(
    workflow: Workflow, problem: str
) -> None:
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


# Shipline's checkout, its guide, and the address the guide clones Shipline from.
_SOURCE = Path(__file__).resolve().parents[2]
_GUIDE = _SOURCE / "docs" / "getting-started.md"
_SHIPLINE_URL = "https://github.com/NazmievBulat/Shipline.git"
_API = f"repos/{_REPOSITORY}"
# The stand-in that answers what the guide asks of GitHub beyond the harness: opening a ticket.
_GH_TICKET = """#!/bin/bash
if [ "$1 $2" = "pr create" ]; then
  echo "gh $*" >> "@STATE@/calls.log"
  printf '%s\\n' "$@" > "@STATE@/created"
  git branch --show-current > "@STATE@/created-branch"
  echo "https://github.com/example-org/example-app/pull/7"
  exit 0
fi
exec "${0%/*}/gh-original" "$@"
"""
_SAMPLE_TEST = """import unittest
from pathlib import Path


class Readme(unittest.TestCase):
    def test_the_readme_names_the_application(self):
        self.assertTrue(Path("README.md").read_text().startswith("# example-app\\n"))
"""


def _guide() -> dict[str, list[tuple[str, str]]]:
    """The code blocks of the guide, by the heading of their section: language and text."""
    sections: dict[str, list[tuple[str, str]]] = {}
    heading = ""
    text = _GUIDE.read_text()
    for found in re.finditer(r"^## ([^\n]+)$|^```(\w+)\n(.*?)^```$", text, re.M | re.S):
        if found[1]:
            heading = found[1]
        else:
            sections.setdefault(heading, []).append((found[2], found[3]))
    # Every block is one of these: none is left out unread.
    assert text.count("```") == 2 * sum(len(blocks) for blocks in sections.values())
    return sections


def _published_shipline(workflow: Workflow) -> Path:
    """Shipline as GitHub serves it: a repository of the scripts, the launcher, the example CI
    and what git leaves out. The guide's clone of its address is answered from here."""
    published = workflow.root / "published-shipline"
    assert workflow.home, "install_shipline first"
    shutil.copytree(
        workflow.home / "scripts" / "workflow",
        published / "scripts" / "workflow",
        ignore=shutil.ignore_patterns("__pycache__", "pre-train-checks"),
    )
    (published / "bin").mkdir()
    shutil.copy2(_SOURCE / "bin" / "shipline", published / "bin" / "shipline")
    shutil.copy2(_SOURCE / ".gitignore", published / ".gitignore")
    (published / "examples").mkdir()
    shutil.copy2(_SOURCE / "examples" / "ci.yml", published / "examples" / "ci.yml")
    for args in (
        ["init", "--quiet", "--initial-branch=main"],
        ["add", "-A"],
        ["commit", "--quiet", "-m", "chore: shipline"],
    ):
        workflow.git(*args, cwd=published)
    return published


def _sample(workflow: Workflow) -> None:
    """A repository set up from nothing, its origin a bare repository of the test's."""
    origin = workflow.root / "origin.git"
    shutil.rmtree(workflow.repo)
    shutil.rmtree(origin)
    workflow.git(
        "init", "--quiet", "--bare", "--initial-branch=main", str(origin), cwd=workflow.root
    )
    workflow.repo.mkdir()
    workflow.git("init", "--quiet", "--initial-branch=main")
    (workflow.repo / "README.md").write_text("# example-app\n")
    (workflow.repo / "test_readme.py").write_text(_SAMPLE_TEST)
    workflow.git("add", "-A")
    workflow.git("commit", "--quiet", "-m", "chore: start")
    workflow.git("remote", "add", "origin", str(origin))
    workflow.git("push", "--quiet", "-u", "origin", "main")


def _run(workflow: Workflow, script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Commands as the reader runs them, `shipline` found through `~/.local/bin`."""
    home = workflow.env["HOME"]
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd,
        env={**workflow.env, "PATH": f"{workflow.env['PATH']}:{home}/.local/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _follow(workflow: Workflow, heading: str, ran: list[str], cwd: Path | None = None) -> str:
    """Does what the guide's section says: writes its JSON as `shipline.json`, runs its shell
    commands. Returns what they printed."""
    printed = ""
    for language, text in _guide()[heading]:
        if language == "json":
            (workflow.repo / "shipline.json").write_text(text)
            continue
        assert language == "sh", language
        done = _run(workflow, text, cwd or workflow.repo)
        assert done.returncode == 0, f"{heading}:\n{text}\n{done.stdout}{done.stderr}"
        printed += done.stdout
    ran.append(heading)
    return printed


def _tick(workflow: Workflow) -> str:
    """The tick as the installed service runs it: its folder, its environment, its command."""
    unit = Path(workflow.env["HOME"]) / ".config" / "systemd" / "user" / "workflow-tick.service"
    lines = unit.read_text().splitlines()
    folder = next(line.split("=", 1)[1] for line in lines if line.startswith("WorkingDirectory="))
    env = dict(
        line.removeprefix('Environment="').removesuffix('"').split("=", 1)
        for line in lines
        if line.startswith("Environment=")
    )
    start = next(line.split("=", 1)[1] for line in lines if line.startswith("ExecStart="))
    done = subprocess.run(
        ["bash", "-c", start],
        cwd=folder,
        env={**workflow.env, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def _step(workflow_file: Path, job: str, name: str) -> str:
    """The command of one step of a job of the repository's CI workflow."""
    found = re.search(
        rf"- name: {re.escape(name)}\n(?:\s+#.*\n|\s+\w[\w-]*:.*\n)*?\s+run: (.+)\n",
        _job(workflow_file, job),
    )
    assert found, (job, name)
    return found[1]


def _ci(github: GitHub, head: str, run_id: int) -> None:
    """The pull request's CI, as GitHub runs the repository's own workflow on its merge into
    main: the planner's steps and the test job's, each in a checkout of that merge of its own;
    GitHub keeps what the planner noted as its annotations."""
    workflow = github.workflow
    tree = workflow.git("merge-tree", "--write-tree", github.base, head).splitlines()[0]
    merge = workflow.git(
        "commit-tree", tree, "-p", github.base, "-p", head, "-m", "Merge head into main"
    )
    printed = {}
    for job, steps in {
        "plan": ["Shipline", "The notes the merge gate reads"],
        "tests": ["Shipline", "Tests"],
    }.items():
        checkout = workflow.root / f"ci-{run_id}-{job}"
        temp = workflow.root / f"ci-{run_id}-{job}-temp"
        temp.mkdir()
        workflow.git("worktree", "add", "--quiet", "--detach", str(checkout), merge)
        printed[job] = ""
        for name in steps:
            command = _step(checkout / ".github" / "workflows" / "ci.yml", job, name)
            done = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", command],
                cwd=checkout,
                env={**workflow.env, "RUNNER_TEMP": str(temp)},
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            assert done.returncode == 0, f"{job}: {command}\n{done.stdout}{done.stderr}"
            printed[job] += done.stdout + done.stderr
    jobs = ["CI: what to run", "CI: server tests", "CI result"]
    github.api[
        f"{_API}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
    ] = {
        "workflow_runs": [
            {
                "id": run_id,
                "head_sha": head,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    github.api[f"{_API}/actions/runs/{run_id}/jobs?per_page=100"] = {
        "jobs": [
            {"id": run_id + 1 + index, "name": name, "status": "completed", "conclusion": "success"}
            for index, name in enumerate(jobs)
        ]
    }
    github.api[f"{_API}/check-runs/{run_id + 1}/annotations?per_page=100"] = annotations(
        printed["plan"]
    )
    github.api[f"{_API}/git/commits/{merge}"] = {
        "parents": [{"sha": github.base}, {"sha": head}],
        "tree": {"sha": tree},
    }
    github.pulls["7"]["statusCheckRollup"] = [
        {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"} for name in jobs
    ]
    # The repository's own tests ran, through its configured command.
    assert "Ran 1 test" in printed["tests"]


def _said(workflow: Workflow, name: str, words: str) -> None:
    """A stand-in session ended its turn with these words."""
    notes = workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"{name}.idle").touch()
    workflow.screen(f"● Done.\n\n  {words}\n\n> \n", name)


@pytest.fixture
def github_stand_in(workflow: Workflow) -> Iterator[GitHub]:
    """GitHub as the gate's tests stand in for it, before any repository exists there."""
    workflow.fake("gh-original", (workflow.bin / "gh").read_text())
    workflow.fake("gh-ticket", _GH_TICKET)
    workflow.fake("gh.py", _GH)
    server = StandInServer(workflow.bin / "gh.py", workflow.env)
    workflow.fake("gh", server.client("gh-ticket"))
    try:
        yield GitHub(workflow, {}, {}, "", "")
    finally:
        server.close()


def test_a_newcomer_installs_shipline_and_merges_a_first_pull_request_through_it(
    workflow: Workflow, github_stand_in: GitHub
) -> None:
    github = github_stand_in
    ran: list[str] = []
    published = _published_shipline(workflow)
    # Every clone of Shipline's address, the guide's and CI's, is answered from the test's.
    workflow.env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{published}.insteadOf",
            "GIT_CONFIG_VALUE_0": _SHIPLINE_URL,
        }
    )
    _sample(workflow)
    home = Path(workflow.env["HOME"])

    _follow(workflow, "Get Shipline", ran, cwd=home)
    workflow.home = home / ".local" / "share" / "shipline"
    _follow(workflow, "Set up your repository", ran)
    github.base = workflow.git("rev-parse", "main")
    assert workflow.git("rev-parse", "origin/main") == github.base
    github.main()
    # The sample keeps no image or browser workflow.
    github.missing_workflows = ["images.yml", "e2e.yml"]
    github.api[_API] = {"full_name": _REPOSITORY, "permissions": {"push": True}}
    github.save()
    (workflow.state / "labels").write_text("")
    _follow(workflow, "Install it for your repository", ran)
    assert sorted((workflow.state / "labels").read_text().splitlines()) == sorted(_LABELS)

    _follow(workflow, "Open a first ticket", ran)
    created = (workflow.state / "created").read_text().splitlines()
    assert created[:5] == ["pr", "create", "--draft", "--label", "workflow"]
    assert (workflow.state / "created-branch").read_text() == "ticket/hello\n"
    assert workflow.git("branch", "--show-current") == "main"
    # GitHub lists it: a draft labelled `workflow`.
    ticket = workflow.git("rev-parse", "origin/ticket/hello")
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/hello {ticket}\n")
    workflow.list_pull_request(7, draft=True)
    github.api[f"{_API}/pulls/7/reviews"] = []
    github.save()

    # The tick starts a worker for the draft, in a worktree of the ticket's branch.
    _tick(workflow)
    assert workflow.is_running("ticket-7")
    worktree = workflow.worktree(7)
    assert workflow.git("branch", "--show-current", cwd=worktree) == "ticket/hello"
    listed = _follow(workflow, "Watch it go through", ran)
    assert listed.split()[0] == "ticket-7"

    # The worker builds the change, pushes it, marks the pull request ready and finishes.
    (worktree / "README.md").write_text("# example-app\nHello.\n")
    workflow.git("commit", "--quiet", "-am", "docs: the README says hello", cwd=worktree)
    workflow.git("push", "--quiet", "origin", "HEAD:ticket/hello", cwd=worktree)
    head = workflow.git("rev-parse", "HEAD", cwd=worktree)
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/hello {head}\n")
    workflow.list_pull_request(7, draft=False)
    github.head = head
    github.pulls["7"] = {
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "headRefName": "ticket/hello",
        "headRefOid": head,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "labels": [{"name": "workflow"}],
    }
    _ci(github, head, run_id=20)
    github.save()
    _said(workflow, "ticket-7", "Worker finished.")

    # The tick stops the worker and starts a fresh reviewer.
    _tick(workflow)
    assert not workflow.is_running("ticket-7")
    assert workflow.is_running("review-7")

    # The reviewer publishes a passed round and hands the merge to the tick.
    github.api[f"{_API}/pulls/7/reviews"] = [
        {"body": "<!-- workflow-review round=1 result=passed -->\nReviewed.", "commit_id": head}
    ]
    github.save()
    handoff = _run(workflow, "shipline merge-when-current --handoff 7", worktree)
    assert handoff.returncode == 0, handoff.stdout + handoff.stderr
    _said(workflow, "review-7", "Review finished.")
    github.batch_mode = True
    github.driver_mode = True
    github.save()

    # The tick merges it through the gate: one squashed commit on main.
    ticked = _tick(workflow)
    assert (workflow.state / "merged").exists(), ticked
    assert (workflow.state / "merged").read_text() == "7"
    assert any(
        f"--squash --match-head-commit {head}" in call for call in workflow.calls("gh pr merge")
    )
    readme = workflow.git("--git-dir", str(workflow.root / "origin.git"), "show", "main:README.md")
    assert readme == "# example-app\nHello."
    assert not workflow.is_running("review-7")

    _follow(workflow, "Stop it", ran)
    assert workflow.calls("systemctl")[-1] == "systemctl --user disable --now workflow-tick.timer"
    # Every command the guide shows ran here.
    assert ran == list(_guide())


def test_the_readme_points_to_the_guide() -> None:
    readme = (_SOURCE / "README.md").read_text()
    section = readme.split("\n## Getting started\n", 1)[1].split("\n## ", 1)[0]

    assert "[`docs/getting-started.md`](docs/getting-started.md)" in section
