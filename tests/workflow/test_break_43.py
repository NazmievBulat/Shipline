"""Breaking tests for pull request 43: two hunts at once, each in its own area.

They replay a day of hunts as a repository with three areas of a web application runs them,
and the failures such a day meets: two starts at once, a start that holds the lock for long,
a second hunter that ends as it starts, the tick stopping one hunt while the other runs.
"""

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from workflow_harness import Workflow, session_arguments

_AREAS = {
    "first-run": ["/consent", "/welcome"],
    "profile": ["/profile", "/career-profile", "/import"],
    "results": ["/builder", "/documents", "/documents/[id]"],
}
_RULES = "by the section Hunt for bugs of AGENTS.md in this worktree"


def _configure(
    workflow: Workflow, areas: dict = _AREAS, plans: str | None = None
) -> None:
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["hunts"] = {"areas": areas, **({"plans": plans} if plans else {})}
    path.write_text(json.dumps(config))


def _notes(workflow: Workflow, hunt: str) -> Path:
    return workflow.hunt_worktree(hunt) / ".local" / "workflow" / f"hunt-{hunt}"


def _start(workflow: Workflow, *words: str):
    return workflow.run("start", "hunt", *words)


def _running_hunts(workflow: Workflow) -> list[str]:
    return sorted(
        p.name
        for p in (workflow.state / "tmux").iterdir()
        if p.name.startswith("hunt-")
    )


def _over_its_time(workflow: Workflow, hunt: str) -> None:
    long_ago = time.time() - 66 * 60
    os.utime(_notes(workflow, hunt).with_suffix(".started"), (long_ago, long_ago))


def test_a_day_of_hunts_over_three_areas(workflow: Workflow) -> None:
    """Two hunts of different programs side by side, a third refused, the tick ending the first
    at its 65 minutes, and the freed place taken by the third."""
    _configure(workflow, plans="docs/hunt-plans.md")

    first = _start(workflow, "plan-first-run-1", "plan", "claude", "first-run")
    second = _start(workflow, "explore-results-1", "explore", "codex", "results")
    third = _start(workflow, "plan-profile-1", "plan", "claude", "profile")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert third.returncode == 1
    assert third.stderr.strip().splitlines()[-1] == (
        "hunt-explore-results-1 and hunt-plan-first-run-1 are still running; two hunts run at most"
    )
    command = session_arguments(workflow.state, "hunt-plan-first-run-1")[-1]
    assert (
        f'"Hunt for bugs with a plan, as hunt-plan-first-run-1, {_RULES}. The plans are in '
        "docs/hunt-plans.md. Your area is first-run: /consent, /welcome. Stay off the other "
        "areas' screens except to pass through them, and take only the plan's tasks marked "
        'first-run."'
    ) in command
    explore = session_arguments(workflow.state, "hunt-explore-results-1")[-1].replace(
        "\\", ""
    )
    assert "Your area is results: /builder, /documents, /documents/[id]." in explore
    assert (
        "plans" not in explore.split("Hunt for bugs exploring", 1)[1].split("2>&1")[0]
    )
    for name in ("plan-first-run-1", "explore-results-1"):
        (workflow.hunt_report(name) / "report.md").write_text(f"# {name}\n")

    _over_its_time(workflow, "plan-first-run-1")
    tick = workflow.run_script("tick")
    again = _start(workflow, "plan-profile-1", "plan", "claude", "profile")

    assert "hunt-plan-first-run-1 is over its time" in tick.stdout, (
        tick.stdout + tick.stderr
    )
    assert not workflow.hunt_worktree("plan-first-run-1").exists()
    assert (workflow.hunt_report("plan-first-run-1") / "report.md").exists()
    assert again.returncode == 0, again.stderr
    assert _running_hunts(workflow) == ["hunt-explore-results-1", "hunt-plan-profile-1"]
    assert (
        _notes(workflow, "explore-results-1").with_suffix(".area").read_text()
        == "results\n"
    )
    assert (workflow.hunt_report("explore-results-1") / "report.md").exists()


@pytest.mark.parametrize("second_area", ["first-run", "profile"], ids=["same", "other"])
def test_two_starts_at_once_never_leave_two_hunts_on_one_area(
    workflow: Workflow, second_area: str
) -> None:
    _configure(workflow)
    for round_ in range(6):
        names = (f"a{round_}", f"b{round_}")
        starts = [
            subprocess.Popen(
                [str(workflow.script), "start", "hunt", name, "plan", "claude", area],
                cwd=workflow.repo, env=workflow.env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for name, area in zip(names, ("first-run", second_area))
        ]  # fmt: skip
        for start in starts:
            start.communicate(timeout=120)
        running = _running_hunts(workflow)
        areas = [
            _notes(workflow, name[5:]).with_suffix(".area").read_text()
            for name in running
        ]
        assert len(set(areas)) == len(areas), (round_, running, areas)
        assert running, round_
        for name in running:
            assert workflow.run("stop", name).returncode == 0
        workflow.run("cleanup", "hunts")


class _HeldLock:
    """Another start that holds the start lock, as one waiting minutes for memory does."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a")
        fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def test_a_start_held_long_neither_holds_the_tick_nor_loses_the_ended_hunt(
    workflow: Workflow,
) -> None:
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "first-run").returncode == 0
    assert _start(workflow, "two", "explore", "claude", "results").returncode == 0
    for name in ("one", "two"):
        (workflow.hunt_report(name) / "report.md").write_text(f"# {name}\n")
    _over_its_time(workflow, "one")
    lock = _HeldLock(workflow.repo / ".local" / "workflow" / "start-hunt.lock")
    try:
        tick = workflow.run_script("tick")
        start = _start(workflow, "three", "plan", "claude", "profile")

        assert "hunt-one is over its time" in tick.stdout, tick.stdout + tick.stderr
        assert not workflow.is_running("hunt-one")
        # Its worktree waits for the lock; its report is kept.
        assert workflow.hunt_worktree("one").is_dir()
        assert (workflow.hunt_report("one") / "report.md").exists()
        assert start.returncode == 1
        assert "the start of another hunt is under way" in start.stderr
    finally:
        lock.release()

    cleaned = workflow.run("cleanup", "hunts")

    assert cleaned.returncode == 0, cleaned.stderr
    assert not workflow.hunt_worktree("one").exists()
    assert (workflow.hunt_report("one") / "report.md").exists()
    assert workflow.is_running("hunt-two")
    assert _notes(workflow, "two").with_suffix(".area").read_text() == "results\n"
    assert _notes(workflow, "two").with_suffix(".url").exists()
    assert (workflow.hunt_report("two") / "report.md").exists()


@pytest.mark.parametrize("program", ["claude", "codex"])
def test_a_second_hunter_that_ends_as_it_starts_leaves_the_first_alone(
    workflow: Workflow, program: str
) -> None:
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "first-run").returncode == 0
    (workflow.hunt_report("one") / "report.md").write_text("# one\n")
    (workflow.state / "dies-at-once").write_text("hunt-two\n")
    # A Claude hunter that ended gives no link.
    workflow.screen("", "hunt-two")

    done = _start(workflow, "two", "explore", program, "results")

    assert done.returncode == 1
    assert not workflow.is_running("hunt-two")
    assert not _notes(workflow, "two").with_suffix(".area").exists()
    assert workflow.is_running("hunt-one")
    assert _notes(workflow, "one").with_suffix(".area").read_text() == "first-run\n"
    assert (workflow.hunt_report("one") / "report.md").exists()
    # The first hunt's area is still held, and the second's is free for another try.
    (workflow.state / "dies-at-once").unlink()
    (workflow.state / "screen" / "hunt-two").unlink()
    same = _start(workflow, "three", "plan", "claude", "first-run")
    assert same.returncode == 1
    assert "hunt-one hunts area first-run" in same.stderr
    retry = _start(workflow, "four", "explore", program, "results")
    assert retry.returncode == 0, retry.stderr


def test_the_application_of_each_hunt_is_stopped_by_its_own_script(
    workflow: Workflow,
) -> None:
    """Each hunt's copy of the application is stopped through the script in its own worktree;
    ending one never calls the other's."""
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "first-run").returncode == 0
    assert _start(workflow, "two", "explore", "claude", "results").returncode == 0
    log = workflow.root / "app-stops"
    for name in ("one", "two"):
        script = workflow.hunt_worktree(name) / "scripts" / "workflow" / "hunt-app"
        script.write_text(f'#!/bin/bash\necho "{name} $1 $PWD" >> "{log}"\n')

    assert workflow.run("stop", "hunt-one").returncode == 0
    assert workflow.run("cleanup", "hunts").returncode == 0

    stops = log.read_text().splitlines()
    assert stops and all(line.startswith("one stop ") for line in stops), stops
    assert workflow.is_running("hunt-two")


@pytest.mark.parametrize(
    "screen,accepted",
    [("/documents/[id]", True), ("/career-profile", True), ("/welcome?step=2", False),
     ("/documents/:id", True), ("/a b", False), ('/"x', False), ("/$(id)", False)],
)  # fmt: skip
def test_screens_an_application_names(
    workflow: Workflow, screen: str, accepted: bool
) -> None:
    _configure(workflow, {"first-run": [screen]})

    started = _start(workflow, "one", "plan", "claude", "first-run")

    assert (started.returncode == 0) is accepted, started.stderr
    if not accepted:
        assert "hunts.areas.first-run must be a list of screens" in started.stderr


def test_the_application_script_is_run_from_the_main_checkout_not_the_hunt_worktree(
    workflow: Workflow,
) -> None:
    """Records where an application script runs when a hunt ends: in the main checkout, with
    nothing that names the hunt but the script's own path. A script that finds its copy of the
    application by its working folder, rather than by its own path, could not tell two hunts
    apart. Low: the application script is the repository's own, and this was so before."""
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "first-run").returncode == 0
    log = workflow.root / "app-stops"
    script = workflow.hunt_worktree("one") / "scripts" / "workflow" / "hunt-app"
    script.write_text(
        f'#!/bin/bash\necho "$PWD ${{WORKFLOW_SESSION:-none}}" >> "{log}"\n'
    )

    assert workflow.run("stop", "hunt-one").returncode == 0

    assert log.read_text().splitlines() == [f"{workflow.repo} none"]


def test_a_second_hunt_right_after_the_first_is_not_held_for_its_memory(
    workflow: Workflow,
) -> None:
    """Records that a hunt started seconds after another goes ahead at once: the memory check
    sees none of the first hunt's application, which takes a minute or two to build. The tick's
    own starts wait 5 minutes after any start; a hunt's start does not."""
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "first-run").returncode == 0
    first = (workflow.repo / ".local" / "workflow" / "last-start").read_text()

    done = _start(workflow, "two", "explore", "claude", "results")

    assert done.returncode == 0, done.stderr
    assert "looking again" not in done.stderr
    assert (
        int((workflow.repo / ".local" / "workflow" / "last-start").read_text())
        - int(first)
        < 60
    )
