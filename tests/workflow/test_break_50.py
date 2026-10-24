"""Breaking tests for pull request 50: hot paths and the cheaper conflict reviewer, replayed as
a repository with a few files many tickets edit uses them every day."""

import dataclasses
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from workflow.test_conflict_review import (  # pyright: ignore[reportPrivateUsage]
    _begin_merge,
    _commit,
    _conflict_review,
    _reviewer,
    _write,
)
from workflow.test_merge_driver import (  # pyright: ignore[reportPrivateUsage]
    _folder,
)
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow.test_ticket_starts import (  # pyright: ignore[reportPrivateUsage]
    _draft,
    _labelled,
    _model,
    _notes,
    _pick,
    _push_change,
    _room_since,
    _start_now,
    _told,
)
from workflow_harness import Workflow

handed = handed_fixture
github = github_fixture

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/workflow"

# The three hot paths of a repository like the ones Shipline runs: one module of the server many
# tickets edit, one file of the server that keeps a draft, and the route folder of a page group.
_MODULE = "server/example_app/imports/persist.py"
_DRAFT = "server/example_app/onboarding/draft.py"
_PAGES = "web/src/app/(menu)/**"
_HOT = [_MODULE, _DRAFT, _PAGES]


def _hot(workflow: Workflow, *paths: str) -> None:
    config = workflow.repo / "shipline.json"
    config.write_text(
        json.dumps({**json.loads(config.read_text()), "hot_paths": list(paths)})
    )


def _ticket(*files: str) -> str:
    """A ticket's description as the orchestrator writes it: the files under their heading."""
    listed = "".join(f"- `{file}`\n" for file in files)
    return f"## Problem\n\nSomething.\n\n## Files\n\n{listed}\n## Out of scope\n\nNothing.\n"


def _on_main(workflow: Workflow, *files: str) -> None:
    for file in files:
        (workflow.repo / file).parent.mkdir(parents=True, exist_ok=True)
        (workflow.repo / file).write_text("on main\n")
    workflow.git("add", ".")
    workflow.git("commit", "--quiet", "-m", "feat: the app")
    workflow.git("push", "--quiet", "origin", "main")


# Ticket starts ---------------------------------------------------------------------------


def test_nine_tickets_on_the_one_module_start_one_at_a_time(workflow: Workflow) -> None:
    """A file nine open tickets name: one runs, the others wait on its hot path, and a ticket
    on other files still starts beside them."""
    _model(workflow)
    _hot(workflow, *_HOT)
    _on_main(
        workflow, _MODULE, _DRAFT, "web/src/app/(menu)/layout.tsx", "server/other.py"
    )
    workflow.add_pull_request(6, labels=("workflow",))
    (workflow.state / "body-6").write_text(_ticket(_MODULE, "server/example_app/a.py"))
    workflow.running("ticket-6")
    for number in range(7, 15):
        workflow.add_pull_request(number, labels=())
        # Each names the module beside a file of its own: only the hot path holds them.
        (workflow.state / f"body-{number}").write_text(
            _ticket(f"server/example_app/imports/n{number}.py", _MODULE)
        )
    workflow.add_pull_request(15, labels=())
    (workflow.state / "body-15").write_text(_ticket("server/other.py"))
    moment = _start_now()
    _room_since(workflow, moment)

    _pick(workflow, moment)

    assert _labelled(workflow) == ["15"]
    told = _told(workflow)
    assert told[0] == "started 15 (priority medium)"
    # Without the hot path, 7 to 14 share the module with 6 too; the wake names the hot path.
    assert told[1:] == [
        f"{number} waits: hot path {_MODULE} with 6" for number in range(7, 15)
    ]


@pytest.mark.parametrize(
    "named",
    [
        "web/src/app/(menu)",
        "web/src/app/(menu)/",
        "web/src/app/(menu)/reports/page.tsx",
    ],
    ids=["the folder", "the folder with a slash", "a new page"],
)
def test_a_ticket_on_the_page_group_waits_for_a_member_only_in_the_merge_line(
    workflow: Workflow, named: str
) -> None:
    """A reviewed pull request only waiting for its merge changes a page of the group; a
    ticket naming the group's folder, or a page not on main yet, waits for its squash."""
    _model(workflow)
    _hot(workflow, *_HOT)
    _on_main(
        workflow,
        "web/src/app/(menu)/layout.tsx",
        "web/src/app/(menu)/settings/page.tsx",
    )
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, "web/src/app/(menu)/settings/page.tsx")
    (_notes(workflow) / "merge-train.json").write_text(json.dumps({"waiting": ["6"]}))
    workflow.add_pull_request(7, labels=())
    (workflow.state / "body-7").write_text(_ticket(named))
    moment = _start_now()
    _room_since(workflow, moment)

    _pick(workflow, moment)

    assert _labelled(workflow) == []
    assert _told(workflow) == [f"7 waits: hot path {_PAGES} with 6"]


def test_tickets_on_two_different_hot_paths_run_together(workflow: Workflow) -> None:
    _model(workflow)
    _hot(workflow, *_HOT)
    _on_main(workflow, _MODULE, _DRAFT)
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, _MODULE)
    (_notes(workflow) / "merge-train.json").write_text(json.dumps({"waiting": ["6"]}))
    workflow.add_pull_request(7, labels=())
    (workflow.state / "body-7").write_text(_ticket(_DRAFT))
    moment = _start_now()
    _room_since(workflow, moment)

    _pick(workflow, moment)

    assert _labelled(workflow) == ["7"]


def test_the_ticket_starts_after_the_member_holding_its_hot_path_merged(
    workflow: Workflow,
) -> None:
    """The member leaves the line with its squash: the next pick starts the waiting ticket and
    tells it once."""
    _model(workflow)
    _hot(workflow, *_HOT)
    _on_main(workflow, _MODULE)
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, _MODULE)
    train = _notes(workflow) / "merge-train.json"
    train.write_text(json.dumps({"waiting": ["6"]}))
    _draft(workflow, 7, _MODULE)
    moment = _start_now()
    _room_since(workflow, moment)
    _pick(workflow, moment)
    assert _labelled(workflow) == []

    head = workflow.git("ls-remote", "origin", workflow.branch(6)).split()[0]
    workflow.set_pull_request(6, "MERGED", head)
    train.write_text(json.dumps({"waiting": []}))
    _room_since(workflow, moment + 600)
    _pick(workflow, moment + 600)

    assert _labelled(workflow) == ["7"]
    assert _told(workflow) == [
        f"7 waits: hot path {_MODULE} with 6",
        "started 7 (priority medium)",
    ]


# The merge line ----------------------------------------------------------------------------


def _merge_gate(monkeypatch: pytest.MonkeyPatch, hot_paths: list[str]) -> Any:
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    import merge_gate  # pyright: ignore[reportMissingImports]

    monkeypatch.setattr(
        merge_gate,
        "CONFIG",
        dataclasses.replace(merge_gate.CONFIG, hot_paths=tuple(hot_paths)),
    )
    return merge_gate


def _a_line(
    tmp_path: Path, members: list[tuple[str, str, list[str]]], hot_paths: list[str]
) -> dict[str, Any]:
    """A saved line of members `(number, priority, hot paths)` in their order of joining."""
    data = {
        "waiting": [number for number, _, _ in members],
        "priorities": {number: name for number, name, _ in members},
        "hot_paths": {
            number: {"head": "0" * 40, "patterns": hot_paths, "paths": paths}
            for number, _, paths in members
        },
    }
    (tmp_path / "merge-train.json").write_text(json.dumps(data))
    return data


@pytest.mark.parametrize("seed", range(40))
def test_a_line_of_thirty_keeps_priority_groups_and_joining_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """Ten to thirty members, mixed priorities, most on one of three hot paths."""
    merge_gate = _merge_gate(monkeypatch, _HOT)
    chance = random.Random(seed)
    members = []
    for index in range(chance.randint(10, 30)):
        paths = [hot for hot in _HOT if chance.random() < 0.3]
        members.append(
            (str(100 + index), chance.choice(["high", "medium", "low"]), paths)
        )
    _a_line(tmp_path, members, _HOT)
    joined = [number for number, _, _ in members]
    named = {number: name for number, name, _ in members}
    paths = {number: set(found) for number, _, found in members}
    rank = {"blocker": 0, "high": 1, "medium": 2, "low": 3}

    line = merge_gate.Train(tmp_path).line()

    assert sorted(line) == sorted(joined)
    # Priority order is never broken for a hot path.
    assert [rank[named[number]] for number in line] == sorted(
        rank[named[n]] for n in line
    )
    for level in ("high", "medium", "low"):
        served = [number for number in line if named[number] == level]
        # A group: members of one priority linked by a hot path, directly or through another.
        groups: list[set[str]] = []
        for number in served:
            linked = [
                group
                for group in groups
                if any(paths[number] & paths[o] for o in group)
            ]
            merged = {number}.union(*linked)
            groups = [group for group in groups if group not in linked] + [merged]
        for group in groups:
            places = sorted(served.index(number) for number in group)
            # Back to back, in their order of joining...
            assert places == list(range(places[0], places[0] + len(group)))
            in_line = [served[place] for place in places]
            assert in_line == sorted(group, key=joined.index)
        # ...each group at the place of its earliest member.
        firsts = [min(group, key=joined.index) for group in groups]
        assert [n for n in served if n in firsts] == sorted(firsts, key=joined.index)


def test_without_hot_paths_a_saved_line_with_hot_paths_is_served_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner takes the key out again: the line is the plain order at once."""
    merge_gate = _merge_gate(monkeypatch, [])
    _a_line(
        tmp_path,
        [("7", "medium", [_MODULE]), ("8", "medium", []), ("9", "medium", [_MODULE])],
        [_MODULE],
    )

    assert merge_gate.Train(tmp_path).line() == ["7", "8", "9"]


def test_a_changed_list_of_hot_paths_drops_what_the_old_one_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merge_gate = _merge_gate(monkeypatch, [_MODULE, _DRAFT])
    _a_line(
        tmp_path,
        [("7", "medium", [_MODULE]), ("8", "medium", []), ("9", "medium", [_MODULE])],
        [_MODULE],
    )

    assert merge_gate.Train(tmp_path).line() == ["7", "8", "9"]


@pytest.mark.parametrize("failing", ["ls-remote", "fetch"])
def test_a_stalled_github_is_waited_for_once_not_for_every_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """Under merge.lock, each git read of GitHub can take four tries of two minutes. A line of
    thirty members with new heads, while GitHub stalls, waits that long for each of them."""
    merge_gate = _merge_gate(monkeypatch, _HOT)
    numbers = [str(100 + index) for index in range(30)]
    train = merge_gate.Train(tmp_path)
    train.queue = numbers
    pulls = {number: {"headRefOid": f"{int(number):040x}"} for number in numbers}
    calls: list[str] = []

    def git(*args: str) -> str:
        calls.append(args[0])
        if args[0] == failing:
            raise merge_gate.Waiting(f"git {args[0]} timed out")
        return "f" * 40 + "\trefs/heads/main"

    def command(*args: str, check: bool = True, errors: str = "strict") -> Any:
        # No member's head is in the local store yet.
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(merge_gate, "git", git)
    monkeypatch.setattr(merge_gate, "command", command)

    train.read_hot_paths(pulls)

    stalled = calls.count(failing)
    worst = stalled * (4 * merge_gate.GIT_READ_TIMEOUT + 7)
    assert stalled <= 1, (
        f"{stalled} stalled reads, up to {worst // 60} minutes under merge.lock"
    )


# The conflict reviewer ------------------------------------------------------------------

_FILES = ["server/example_app/imports/persist.py", "web/src/app/menu/layout.tsx"]
# A page of a route group, a folder name in parentheses, as web frameworks name them.
_GROUP_FILES = [
    "server/example_app/imports/persist.py",
    "web/src/app/(menu)/layout.tsx",
]
_CONTEXT = "".join(f"# context {line}\n" for line in range(10))


def _two_file_conflict(
    workflow: Workflow, files: list[str] = _FILES
) -> tuple[str, str]:
    """A conflict of two lines on each side in each of two hot files: eight lines in all."""
    branch = workflow.worktree(7)
    for file in files:
        _write(
            branch, file, _CONTEXT + "value = 'branch'\nother = 'branch'\n" + _CONTEXT
        )
    head = _commit(workflow, branch)
    for file in files:
        _write(
            workflow.repo,
            file,
            _CONTEXT + "value = 'main'\nother = 'main'\n" + _CONTEXT,
        )
    main = _commit(workflow, workflow.repo)
    return head, main


def _resolve_both(workflow: Workflow) -> str:
    branch = workflow.worktree(7)
    for file in _FILES:
        _write(branch, file, _CONTEXT + "value = 'both'\nother = 'both'\n" + _CONTEXT)
    workflow.git("add", ".", cwd=branch)
    workflow.git(
        "commit", "--quiet", "-m", "merge main: conflict in two files", cwd=branch
    )
    workflow.git("push", "--quiet", "origin", "HEAD", cwd=branch)
    return workflow.git("rev-parse", "HEAD", cwd=branch)


@pytest.mark.parametrize(
    ("max_changed_lines", "reviewer"), [(8, "medium sonnet\n"), (7, "high opus\n")]
)
def test_a_few_lines_in_two_files_get_the_named_reviewer(
    workflow: Workflow, max_changed_lines: int, reviewer: str
) -> None:
    workflow.add_worktree(7, draft=False)
    head, main = _two_file_conflict(workflow)
    _conflict_review(workflow, max_changed_lines)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": _FILES})
    )

    assert _reviewer(workflow) == reviewer
    _resolve_both(workflow)
    # A restart of the reviewer after it committed the merge chooses the same.
    assert _reviewer(workflow) == reviewer


def test_the_named_reviewer_hands_off_a_small_resolution_in_two_files(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    head, main = _two_file_conflict(workflow)
    _conflict_review(workflow, 8)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": _FILES})
    )
    assert _reviewer(workflow) == "medium sonnet\n"
    resolved = _resolve_both(workflow)
    handed.pulls["7"]["headRefOid"] = resolved
    handed.api["repos/example-org/example-app/pulls/7/reviews"] = [
        {"body": "<!-- workflow-review round=1 result=passed -->", "commit_id": head},
        {
            "body": f"<!-- workflow-review round=2 result=passed -->\n"
            f"<!-- workflow-conflict extends={head} -->",
            "commit_id": resolved,
        },
    ]
    handed.save()
    (folder / "merge-wanted-7").unlink()

    done = workflow.run_script(
        "merge-when-current", "--handoff", "7", cwd=workflow.worktree(7)
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert (folder / "merge-wanted-7").exists()
    assert not (folder / "review-7.usual-reviewer").exists()


def test_a_small_conflict_in_a_route_group_page_gets_the_named_reviewer(
    workflow: Workflow,
) -> None:
    """The same eight lines, one file in the page group's folder: the conflict round the tick
    prepares is the named reviewer's as well."""
    workflow.add_worktree(7, draft=False)
    head, main = _two_file_conflict(workflow, _GROUP_FILES)
    _conflict_review(workflow, 8)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    # What the merge driver does on the conflict (merge_driver.py: prepare_conflict_review).
    request = {
        "head": head,
        "reason": f"non-mechanical conflict in {', '.join(_GROUP_FILES)}",
    }
    passed = [
        {"body": "<!-- workflow-review round=1 result=passed -->", "commit_id": head}
    ]
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "import json, sys; from pathlib import Path; import conflict_review as c; "
                "c.prepare(Path(sys.argv[1]), '7', Path(sys.argv[2]), json.loads(sys.argv[3]), "
                "sys.argv[4], json.loads(sys.argv[5]))"
            ),
            str(folder),
            str(workflow.worktree(7)),
            json.dumps(request),
            head,
            json.dumps(passed),
        ],
        cwd=workflow.script.parent,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    assert (folder / "review-7.conflict.json").exists(), (
        "no conflict scope: a full review"
    )
    assert _reviewer(workflow) == "medium sonnet\n"


@pytest.mark.parametrize("style", ["merge", "diff3", "zdiff3"])
def test_the_conflict_style_of_the_owner_s_git_does_not_hand_a_small_conflict_away(
    workflow: Workflow, style: str
) -> None:
    """An owner whose git shows the common ancestor too: its lines are not the conflict's."""
    # The two files are on main before the ticket, as hot files are.
    for file in _FILES:
        _write(
            workflow.repo,
            file,
            _CONTEXT + "value = 'base'\nother = 'base'\n" + _CONTEXT,
        )
    _commit(workflow, workflow.repo)
    workflow.add_worktree(7, draft=False)
    head, main = _two_file_conflict(workflow)
    workflow.git("config", "merge.conflictStyle", style)
    _conflict_review(workflow, 8)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": _FILES})
    )

    assert _reviewer(workflow) == "medium sonnet\n"


def test_session_start_review_passes_the_named_effort_and_model(
    workflow: Workflow,
) -> None:
    """What `claude` is started with, through `session start review` itself."""
    workflow.add_worktree(7, draft=False)
    head, main = _two_file_conflict(workflow)
    _conflict_review(workflow, 8)
    _begin_merge(workflow, main)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "review-7.conflict.json").write_text(
        json.dumps({"head": head, "main": main, "files": _FILES})
    )
    # The hand-back the merge driver wrote for the conflict, as `session` requires for it.
    (folder / "merge-review-7").write_text(
        json.dumps(
            {
                "reason": f"non-mechanical conflict in {', '.join(_FILES)}",
                "event": "0" * 32,
                "head": head,
                "local_head": head,
            }
        )
    )
    from workflow_harness import session_arguments

    done = workflow.run_script("session", "start", "review", "7")

    assert done.returncode == 0, done.stdout + done.stderr
    arguments = " ".join(session_arguments(workflow.state, "review-7"))
    assert "--effort medium --model sonnet" in arguments


# Settings --------------------------------------------------------------------------------


def _load(tmp_path: Path, given: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Load a shipline.json in a checkout of its own, as every script does at its start."""
    repo = tmp_path / "repo"
    (repo / "scripts/workflow").mkdir(parents=True)
    for name in ("workflow_config.py",):
        (repo / "scripts/workflow" / name).write_text((_SCRIPTS / name).read_text())
    (repo / "shipline.json").write_text(json.dumps(given))
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json, workflow_config as c; print(json.dumps(c.as_dict(c.load())))",
        ],
        cwd=repo / "scripts/workflow",
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_example_settings_load_with_both_keys(tmp_path: Path) -> None:
    example = json.loads((_SCRIPTS.parents[1] / "examples/shipline.json").read_text())
    done = _load(tmp_path, example)
    assert done.returncode == 0, done.stderr
    loaded = json.loads(done.stdout)
    assert loaded["hot_paths"] == example["hot_paths"]
    assert loaded["conflict_review"] == example["conflict_review"]


@pytest.mark.parametrize(
    "conflict_review",
    [
        {"effort": "medium", "model": "sonnet; rm -rf /", "max_changed_lines": 40},
        {"effort": "medium", "model": "$(id)", "max_changed_lines": 40},
        {
            "effort": "medium --dangerously-skip-permissions",
            "model": "x",
            "max_changed_lines": 1,
        },
        {
            "effort": "medium",
            "model": "--dangerously-skip-permissions",
            "max_changed_lines": 1,
        },
        {"effort": "medium", "model": "sonnet", "max_changed_lines": "40"},
        {"effort": "medium", "model": "sonnet"},
    ],
    ids=[
        "semicolon",
        "substitution",
        "effort flag",
        "model flag",
        "lines as text",
        "missing",
    ],
)
def test_a_wrong_conflict_reviewer_is_refused_at_load(
    tmp_path: Path, conflict_review: dict[str, Any]
) -> None:
    done = _load(tmp_path, {"conflict_review": conflict_review})
    assert done.returncode != 0
    assert "conflict_review" in done.stderr


@pytest.mark.parametrize(
    "hot_paths", [["/etc/passwd"], ["../outside/**"], ["!web/**"], "web/**", [""]]
)
def test_a_wrong_hot_path_is_refused_at_load(tmp_path: Path, hot_paths: Any) -> None:
    done = _load(tmp_path, {"hot_paths": hot_paths})
    assert done.returncode != 0
    assert "hot_paths" in done.stderr
