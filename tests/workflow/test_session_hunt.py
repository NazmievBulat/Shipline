"""Two hunts may run at once, each in its own area of the application, and never three.

A repository names its areas in `shipline.json`, under `hunts.areas`. A hunt without an area
hunts every screen and runs alone; with no areas configured, one hunt runs at a time.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from workflow_harness import Workflow, session_arguments

_AREAS = {"phone": ["/calls", "/contacts"], "billing": ["/invoices", "/settings/billing"]}
_RULES = "by the section Hunt for bugs of AGENTS.md in this worktree"


def _configure(
    workflow: Workflow, areas: dict[str, list[str]] = _AREAS, plans: str | None = None
) -> None:
    """The hunts in the main checkout's `shipline.json`, beside what the harness holds."""
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["hunts"] = {"areas": areas, **({"plans": plans} if plans else {})}
    path.write_text(json.dumps(config))


def _notes(workflow: Workflow, hunt: str) -> Path:
    return workflow.hunt_worktree(hunt) / ".local" / "workflow" / f"hunt-{hunt}"


def _command(workflow: Workflow, hunt: str) -> str:
    """What the hunt's `tmux` runs: the program and the sentence it is started with."""
    return session_arguments(workflow.state, f"hunt-{hunt}")[-1]


def _start(workflow: Workflow, *words: str):
    return workflow.run("start", "hunt", *words)


_PLAN_IN_PHONE = (
    f"Hunt for bugs with a plan, as hunt-one, {_RULES}. Your area is phone: /calls, /contacts. "
    "Stay off the other areas' screens except to pass through them, "
    "and take only the plan's tasks marked phone."
)
_EXPLORE_IN_PHONE = (
    f"Hunt for bugs exploring, as hunt-one, {_RULES}. Your area is phone: /calls, /contacts. "
    "Stay off the other areas' screens except to pass through them."
)


@pytest.mark.parametrize(
    "kind,sentence", [("plan", _PLAN_IN_PHONE), ("explore", _EXPLORE_IN_PHONE)],
    ids=["plan", "explore"],
)  # fmt: skip
def test_a_hunt_with_an_area_is_told_its_area_and_keeps_it_in_a_note(
    workflow: Workflow, kind: str, sentence: str
) -> None:
    _configure(workflow)

    done = _start(workflow, "one", kind, "claude", "phone")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("hunt-one")
    assert f'"{sentence}"' in _command(workflow, "one")
    assert _notes(workflow, "one").with_suffix(".area").read_text() == "phone\n"
    # Beside the note of its start, which the tick's 65 minutes are read from.
    assert _notes(workflow, "one").with_suffix(".started").exists()


@pytest.mark.parametrize("configured", [True, False], ids=["areas", "no areas"])
def test_a_hunt_without_an_area_gets_the_old_sentence_and_no_note(
    workflow: Workflow, configured: bool
) -> None:
    if configured:
        _configure(workflow)

    done = _start(workflow, "one", "plan", "claude")

    assert done.returncode == 0, done.stderr
    assert f'"Hunt for bugs with a plan, as hunt-one, {_RULES}"' in _command(workflow, "one")
    assert not _notes(workflow, "one").with_suffix(".area").exists()


@pytest.mark.parametrize("program", ["claude", "codex", "cursor"])
def test_a_second_hunt_on_another_area_starts_beside_the_first(
    workflow: Workflow, program: str
) -> None:
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "phone").returncode == 0

    done = _start(workflow, "two", "explore", program, "billing")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("hunt-one") and workflow.is_running("hunt-two")
    assert _notes(workflow, "two").with_suffix(".area").read_text() == "billing\n"
    assert "Your area is billing: /invoices, /settings/billing." in _command(
        workflow, "two"
    ).replace("\\", "")
    # The first hunt's worktree, notes and report are its own still.
    assert _notes(workflow, "one").with_suffix(".area").read_text() == "phone\n"
    assert workflow.hunt_report("one").is_dir() and workflow.hunt_report("two").is_dir()


@pytest.mark.parametrize(
    "second,refusal",
    [
        (("phone",), "hunt-one hunts area phone; a second hunt must name another area"),
        ((), "hunt-one hunts area phone; a second hunt must name another area"),
    ],
    ids=["same area", "no area"],
)
def test_a_second_hunt_must_name_another_area(
    workflow: Workflow, second: tuple[str, ...], refusal: str
) -> None:
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "phone").returncode == 0

    done = _start(workflow, "two", "plan", "claude", *second)

    assert done.returncode == 1
    assert done.stderr.strip().splitlines()[-1] == refusal
    _nothing_started(workflow, "two")


def test_no_hunt_starts_beside_one_on_every_screen(workflow: Workflow) -> None:
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude").returncode == 0

    done = _start(workflow, "two", "plan", "claude", "billing")

    assert done.returncode == 1
    assert done.stderr.strip().splitlines()[-1] == (
        "hunt-one hunts every screen; a second hunt starts only beside one with an area"
    )
    _nothing_started(workflow, "two")


def test_a_third_hunt_is_refused(workflow: Workflow) -> None:
    _configure(workflow, {**_AREAS, "maps": ["/maps"]})
    assert _start(workflow, "one", "plan", "claude", "phone").returncode == 0
    assert _start(workflow, "two", "plan", "claude", "billing").returncode == 0

    done = _start(workflow, "three", "plan", "claude", "maps")

    assert done.returncode == 1
    assert done.stderr.strip().splitlines()[-1] == (
        "hunt-one and hunt-two are still running; two hunts run at most"
    )
    _nothing_started(workflow, "three")


@pytest.mark.parametrize("configured", [True, False], ids=["areas", "no areas"])
def test_an_area_that_is_not_configured_is_refused(workflow: Workflow, configured: bool) -> None:
    if configured:
        _configure(workflow)

    done = _start(workflow, "one", "plan", "claude", "maps")

    assert done.returncode == 1
    assert "maps" in done.stderr and "hunts.areas" in done.stderr
    _nothing_started(workflow, "one")


@pytest.mark.parametrize("words", [("one", "phone"), ("one", "plan", "phone")])
def test_the_area_comes_only_after_the_kind_and_the_program(
    workflow: Workflow, words: tuple[str, ...]
) -> None:
    _configure(workflow)

    done = _start(workflow, *words)

    assert done.returncode == 2
    assert "usage:" in done.stderr
    _nothing_started(workflow, "one")


def test_with_no_areas_configured_one_hunt_runs_at_a_time(workflow: Workflow) -> None:
    assert _start(workflow, "one", "plan", "claude").returncode == 0

    done = _start(workflow, "two", "plan", "codex")

    assert done.returncode == 1
    assert done.stderr.strip().splitlines()[-1] == (
        "hunt-one is still running; one hunt runs at a time"
    )
    _nothing_started(workflow, "two")


@pytest.mark.parametrize("area", [(), ("phone",)], ids=["every screen", "an area"])
def test_a_plan_hunt_is_told_where_the_plans_are_when_the_file_names_them(
    workflow: Workflow, area: tuple[str, ...]
) -> None:
    _configure(workflow, plans="docs/hunt-plans.md")

    done = _start(workflow, "one", "plan", "claude", *area)

    assert done.returncode == 0, done.stderr
    first = f"Hunt for bugs with a plan, as hunt-one, {_RULES}."
    sentence = f"{first} The plans are in docs/hunt-plans.md."
    if area:
        sentence += _PLAN_IN_PHONE.removeprefix(first)
    assert f'"{sentence}"' in _command(workflow, "one")


def test_an_exploring_hunt_is_not_told_of_the_plans(workflow: Workflow) -> None:
    _configure(workflow, plans="docs/hunt-plans.md")

    done = _start(workflow, "one", "explore", "claude")

    assert done.returncode == 0, done.stderr
    assert f'"Hunt for bugs exploring, as hunt-one, {_RULES}"' in _command(workflow, "one")


@pytest.mark.parametrize("program", ["claude", "codex"])
def test_the_sentence_reaches_the_hunter_word_for_word_through_the_shell(
    workflow: Workflow, program: str
) -> None:
    _configure(workflow, {"phone": ["/contacts/[id]/*", "/~me:@%+{x}"]}, "docs/hunt-plans.md")
    assert _start(workflow, "one", "plan", program, "phone").returncode == 0
    # A hunter that writes down the last word it was given: its sentence.
    hunter = workflow.root / "hunter"
    hunter.mkdir()
    (hunter / program).write_text(
        f'#!/bin/bash\nprintf "%s" "${{@: -1}}" > {workflow.root / "sentence"}\n'
    )
    (hunter / program).chmod(0o755)

    subprocess.run(
        ["bash", "-c", _command(workflow, "one")],
        cwd=workflow.hunt_worktree("one"),
        env={**workflow.env, "PATH": f"{hunter}:{workflow.env['PATH']}"},
        check=True,
    )

    assert (workflow.root / "sentence").read_text() == (
        f"Hunt for bugs with a plan, as hunt-one, {_RULES}. The plans are in docs/hunt-plans.md. "
        "Your area is phone: /contacts/[id]/*, /~me:@%+{x}. Stay off the other areas' screens "
        "except to pass through them, and take only the plan's tasks marked phone."
    )


def test_stop_forgets_the_area_with_the_other_notes(workflow: Workflow) -> None:
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "phone").returncode == 0

    done = workflow.run("stop", "hunt-one")

    assert done.returncode == 0, done.stderr
    assert not _notes(workflow, "one").with_suffix(".area").exists()
    assert not _notes(workflow, "one").with_suffix(".started").exists()


def _two_hunts(workflow: Workflow) -> None:
    _configure(workflow)
    assert _start(workflow, "one", "plan", "claude", "phone").returncode == 0
    assert _start(workflow, "two", "explore", "claude", "billing").returncode == 0
    for name in ("one", "two"):
        (workflow.hunt_report(name) / "report.md").write_text(f"# Hunt {name}\n")


def _left_alone(workflow: Workflow, name: str, area: str) -> None:
    assert workflow.is_running(f"hunt-{name}")
    assert workflow.hunt_worktree(name).is_dir()
    assert _notes(workflow, name).with_suffix(".area").read_text() == f"{area}\n"
    assert _notes(workflow, name).with_suffix(".started").exists()
    assert _notes(workflow, name).with_suffix(".url").exists()
    assert (workflow.hunt_report(name) / "report.md").exists()


def test_ending_one_hunt_and_its_cleanup_leave_the_other_alone(workflow: Workflow) -> None:
    _two_hunts(workflow)

    stopped = workflow.run("stop", "hunt-one")
    cleaned = workflow.run("cleanup", "hunts")

    assert stopped.returncode == 0, stopped.stderr
    assert cleaned.returncode == 0, cleaned.stderr
    assert not workflow.hunt_worktree("one").exists()
    assert (workflow.hunt_report("one") / "report.md").exists()
    _left_alone(workflow, "two", "billing")


def test_the_tick_stops_only_the_hunt_that_is_over_its_time(workflow: Workflow) -> None:
    _two_hunts(workflow)
    long_ago = time.time() - 66 * 60
    os.utime(_notes(workflow, "one").with_suffix(".started"), (long_ago, long_ago))

    done = workflow.run_script("tick")

    assert "hunt-one is over its time" in done.stdout, done.stdout + done.stderr
    assert not workflow.is_running("hunt-one")
    assert not workflow.hunt_worktree("one").exists()
    assert (workflow.hunt_report("one") / "report.md").exists()
    _left_alone(workflow, "two", "billing")


def test_a_freed_area_takes_a_new_hunt(workflow: Workflow) -> None:
    _two_hunts(workflow)
    assert workflow.run("stop", "hunt-one").returncode == 0

    done = _start(workflow, "three", "plan", "claude", "phone")

    assert done.returncode == 0, done.stderr
    _left_alone(workflow, "two", "billing")


def _nothing_started(workflow: Workflow, name: str) -> None:
    assert not workflow.is_running(f"hunt-{name}")
    assert not workflow.hunt_worktree(name).exists()
    assert not workflow.hunt_report(name).exists()
