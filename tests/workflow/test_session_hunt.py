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
from workflow_harness import Workflow, install_shipline, session_arguments

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


@pytest.mark.parametrize("area", [(), ("--area", "phone"), ("plan", "claude", "phone")])
def test_hunt_app_keeps_settings_for_start_fresh_and_restart(workflow: Workflow, area: tuple[str, ...]) -> None:
    install_shipline(workflow)
    _configure(workflow)
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    shared = {"FEATURE": "off", "SHARED": "a 'quote' $value `word` = \\ end", "EMPTY": ""}
    config["hunts"].update(settings=shared, area_settings={"phone": {"FEATURE": "on", "PHONE": "yes"}})
    path.write_text(json.dumps(config))
    workflow.fake(
        "hunt-app",
        "#!/bin/bash\n"
        "python3 - \"$1\" <<'PYAPP'\n"
        "import json, os, sys\n"
        "values = {k: os.environ.get(k) for k in ('FEATURE', 'SHARED', 'EMPTY', 'PHONE')}\n"
        "print(json.dumps(values))\n"
        "with open('app-' + sys.argv[1] + '.json', 'w') as stream:\n"
        "    json.dump(values, stream)\n"
        "PYAPP\n",
    )
    started = _start(workflow, "one", *area)
    assert started.returncode == 0, started.stderr
    expected = {**shared, **({"FEATURE": "on", "PHONE": "yes"} if area else {})}
    report = (workflow.hunt_report("one") / "report.md").read_text()
    recorded = json.loads(report.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert recorded == expected
    assert "Settings" in report
    assert "Keep the settings already recorded at the top of report.md" in _command(workflow, "one")
    snapshot = workflow.hunt_worktree("one") / ".local/workflow/hunt-settings.json"
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert (workflow.hunt_report("one") / "report.md").stat().st_mode & 0o777 == 0o600
    # Editing the installation after launch must not alter this hunt's copy.
    config["hunts"]["settings"] = {"FEATURE": "changed"}
    config["hunts"]["area_settings"] = {}
    path.write_text(json.dumps(config))
    worktree = workflow.hunt_worktree("one")
    nested = worktree / "nested"
    nested.mkdir()
    for command in ("start", "fresh", "restart"):
        done = workflow.shipline("hunt-app", command, cwd=nested)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {**expected, "PHONE": expected.get("PHONE")}
    hunter_env = (workflow.state / "env" / "hunt-one").read_text()
    assert "FEATURE=" not in hunter_env
    stopped = workflow.run("stop", "hunt-one")
    assert stopped.returncode == 0, stopped.stderr
    assert json.loads((worktree / "app-stop.json").read_text()) == {**expected, "PHONE": expected.get("PHONE")}
    # A configured app run outside a hunt does not acquire hunt settings.
    ordinary = workflow.shipline("hunt-app", "start")
    assert ordinary.returncode == 0, ordinary.stderr
    assert all(value is None for value in json.loads(ordinary.stdout).values())


@pytest.mark.parametrize("words", [("",), ("--area",), ("--area", "phone", "--area", "billing"),
                                    ("plan", "claude", "phone", "--area", "billing")])
def test_an_incomplete_or_duplicate_area_option_is_refused(workflow: Workflow, words: tuple[str, ...]) -> None:
    _configure(workflow)
    done = _start(workflow, "one", *words)
    assert done.returncode == 2
    _nothing_started(workflow, "one")


@pytest.mark.parametrize("words", [("--area", "phone", "explore", "codex"),
                                    ("explore", "--area", "phone", "cursor")])
def test_the_area_option_can_surround_the_existing_hunt_options(workflow: Workflow, words: tuple[str, ...]) -> None:
    _configure(workflow)
    done = _start(workflow, "one", *words)
    assert done.returncode == 0, done.stderr
    assert _notes(workflow, "one").with_suffix(".area").read_text() == "phone\n"



def test_two_hunt_apps_keep_their_own_area_settings(workflow: Workflow) -> None:
    install_shipline(workflow)
    _configure(workflow)
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["hunts"].update(settings={"FEATURE": "shared"},
                           area_settings={"phone": {"FEATURE": "phone"},
                                          "billing": {"FEATURE": "billing"}})
    path.write_text(json.dumps(config))
    workflow.fake("hunt-app", '#!/bin/bash\nprintf "%s" "$FEATURE"\n')
    for name, area in (("one", "phone"), ("two", "billing")):
        started = _start(workflow, name, "--area", area)
        assert started.returncode == 0, started.stderr
    for name, area in (("one", "phone"), ("two", "billing")):
        done = workflow.shipline("hunt-app", "fresh", cwd=workflow.hunt_worktree(name))
        assert done.returncode == 0, done.stderr
        assert done.stdout == area



def test_hunt_settings_use_the_selected_relative_config_home(workflow: Workflow) -> None:
    install_shipline(workflow)
    caller = workflow.repo / "nested"
    caller.mkdir()
    config_home = caller / "config-home"
    installation = config_home / "shipline/example-org-example-app"
    installation.mkdir(parents=True)
    config = json.loads((workflow.repo / "shipline.json").read_text())
    config.update(state_folder=".local/hunt-state", hunts={"settings": {"FEATURE": "on"}})
    (installation / "shipline.json").write_text(json.dumps(config))
    workflow.fake("hunt-app", '#!/bin/bash\nprintf "%s" "$FEATURE"\n')

    started = workflow.shipline("session", "start", "hunt", "one", cwd=caller,
                               env={"XDG_CONFIG_HOME": "config-home"})

    assert started.returncode == 0, started.stderr
    report = (workflow.hunt_report("one") / "report.md").read_text()
    assert json.loads(report.split("```json\n", 1)[1].split("\n```", 1)[0]) == {"FEATURE": "on"}
    done = workflow.shipline("hunt-app", "fresh", cwd=workflow.hunt_worktree("one"),
                            env={"XDG_CONFIG_HOME": str(config_home)})
    assert done.returncode == 0, done.stderr
    assert done.stdout == "on"


@pytest.mark.parametrize("target", ["hunt-settings.json", "report.md"])
@pytest.mark.parametrize("stage", ["open", "write"])
def test_hunt_preparation_storage_error_releases_the_name(
    workflow: Workflow, target: str, stage: str
) -> None:
    _configure(workflow)
    hooks = workflow.root / "storage-fault"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "import errno, os\n"
        "from contextlib import contextmanager\n"
        f"target, stage = {target!r}, {stage!r}\n"
        "original_open, original_fdopen = os.open, os.fdopen\n"
        "fault_fd = None\n"
        "def fail():\n"
        "    raise OSError(errno.ENOSPC, 'example storage full')\n"
        "def open_file(path, flags, mode=0o777, **kwargs):\n"
        "    global fault_fd\n"
        "    selected = str(path).endswith('/' + target)\n"
        "    if selected and stage == 'open':\n"
        "        fail()\n"
        "    fd = original_open(path, flags, mode, **kwargs)\n"
        "    if selected:\n"
        "        fault_fd = fd\n"
        "    return fd\n"
        "class PartialWriter:\n"
        "    def __init__(self, stream):\n"
        "        self.stream = stream\n"
        "    def write(self, text):\n"
        "        self.stream.write(text[:8])\n"
        "        self.stream.flush()\n"
        "        fail()\n"
        "@contextmanager\n"
        "def open_stream(fd, *args, **kwargs):\n"
        "    with original_fdopen(fd, *args, **kwargs) as stream:\n"
        "        yield PartialWriter(stream) if fd == fault_fd and stage == 'write' else stream\n"
        "os.open, os.fdopen = open_file, open_stream\n"
    )

    failed = workflow.run("start", "hunt", "one", "--area", "phone",
                          env={"PYTHONPATH": str(hooks)})

    assert failed.returncode != 0
    assert "could not use hunt settings" in failed.stderr
    assert not workflow.is_running("hunt-one")
    assert not workflow.hunt_report("one").exists()
    assert not (workflow.hunt_worktree("one") / ".local/workflow/hunt-settings.json").exists()
    retried = _start(workflow, "one", "--area", "phone")
    assert retried.returncode == 0, retried.stderr
    assert workflow.is_running("hunt-one")


@pytest.mark.parametrize("count", [None, 0, 3])
def test_metrics_read_the_findings_after_the_saved_settings(
    workflow: Workflow, count: int | None
) -> None:
    _configure(workflow)
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    # A value resembling a count must never supply the report's findings count.
    config["hunts"]["settings"] = {"EXAMPLE": "Findings: 99", **{
        f"FEATURE_{i}": "on" for i in range(30)
    }}
    path.write_text(json.dumps(config))
    started = _start(workflow, "one", "--area", "phone")
    assert started.returncode == 0, started.stderr
    report = workflow.hunt_report("one") / "report.md"
    with report.open("a") as stream:
        stream.write("# Results\n\n" + (f"Findings: {count}\n" if count is not None else "Pending.\n"))
    done = subprocess.run(
        ["python3", "-c", "import json; from pathlib import Path; "
         "from workflow_metrics import read_hunts; print(json.dumps(read_hunts(Path.cwd())))"],
        cwd=workflow.repo,
        env={**workflow.env, "PYTHONPATH": str(workflow.script.parent)},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)[0][1:] == ["one", count]
