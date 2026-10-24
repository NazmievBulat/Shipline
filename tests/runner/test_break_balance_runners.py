"""Breaking tests for the runner balancer (`runner/balance_runners.py`), written apart from its
builder: a pool of two machines in everyday use, each with two ordinary slots, a spare, three
Docker runners, two main runners (one lent to pull requests) and the planner.

They reuse the balancer's own harness (`test_balance_runners.py`): stand-ins for gh, systemctl,
nproc, the load and the memory."""

import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from test_balance_runners import (
    _FAKE_GH,
    DOCKER,
    MINUTE,
    ORDINARY,
    STANDBY,
    Machine,
    machine,
)

__all__ = ["machine"]

FIRST = "example-app-home-first"
MAIN = "example-app-home-main"
# The units and runners the balancer must never touch.
NEVER = ("-main", "-planner", "-spare", "-lend", "-image", "-usage", "-firewall")


def _everyday(
    box: Machine,
    *,
    bounds: str = "",
    ordinary: int = 2,
    docker: int = 3,
    extra: str = "",
) -> None:
    """A machine with 32 processors (a session start waits above a load of 48), two ordinary slots
    and a spare, three Docker runners and main's two runners, one of them lent."""
    box.fake("nproc", "#!/bin/bash\necho 32\n")
    box.settings_file.write_text(
        "repository='example-owner/example-app'\n"
        "prefix='example-app'\n"
        "label='example-app-home'\n"
        "machine=box\n"
        "roles='ordinary docker main'\n"
        "other_machines='other'\n"
        f"ordinary_jobs={ordinary}\n"
        f"ordinary_blacksmith_jobs={ordinary}\n"
        "ordinary_spares=1\n"
        f"docker_runners={docker}\n" + bounds + extra
    )
    box.settings_file.chmod(0o640)
    _listed(box, ordinary=ordinary, docker=docker)
    box.load(30.0)
    box.memory(12)


def _listed(
    box: Machine,
    *,
    ordinary: int = 2,
    docker: int = 3,
    other_docker: int = 1,
    busy: tuple[str, ...] = (),
) -> None:
    names = [(f"box-{k}", [ORDINARY, FIRST]) for k in range(1, ordinary + 1)]
    names += [(f"box-{ordinary + 1}", [STANDBY])]
    names += [(f"box-docker-{k}", [DOCKER]) for k in range(1, docker + 1)]
    names += [
        ("box-main", [MAIN]),
        # Lent to pull requests by the lender: the pool's label and the first label beside main's.
        ("box-main-2", [MAIN, ORDINARY, FIRST]),
        ("box-planner", ["home-planner"]),
        ("other-1", [ORDINARY, FIRST]),
        ("other-2", [ORDINARY, FIRST]),
        ("other-3", [STANDBY]),
    ]
    names += [(f"other-docker-{k}", [DOCKER]) for k in range(1, other_docker + 1)]
    box.runners = [
        {
            "id": number,
            "name": name,
            "status": "online",
            "busy": name in busy,
            "labels": [{"name": "self-hosted"}] + [{"name": label} for label in labels],
        }
        for number, (name, labels) in enumerate(names, start=1)
    ]


def _untouched(box: Machine) -> None:
    for call in box.calls("systemctl"):
        assert not any(word in call for word in NEVER), call
    for name in box.deleted():
        assert not name.endswith(("-main", "-main-2", "-planner")), name


# 1. With no bounds, nothing changes on either machine after the upgrade.


def test_no_bounds_change_nothing_through_a_whole_day(machine: Machine) -> None:
    _everyday(machine)
    # A morning wave: both kinds queue deep, with room, and a forecast of more.
    machine.queue(ORDINARY, 20, 12, 8, 6, running=5)
    machine.queue(DOCKER, 25, 9, running=3)
    machine.forecast(30, 10)
    machine.run()
    machine.run(after_minutes=6)
    # Afternoon: nothing queued for an hour while the sessions hold the load over the ceiling.
    machine.clear_queue()
    machine.load(55.0)
    machine.forecast(0, 0)
    for _ in range(6):
        machine.run(after_minutes=10)

    assert machine.calls("systemctl") == []
    assert machine.calls("gh api -X DELETE") == []
    assert (machine.count("ordinary_jobs"), machine.count("docker_runners")) == (2, 3)


# 2. Once bounds are widened, it grows and shrinks each kind by its own queue.


def test_widened_bounds_add_a_slot_for_an_ordinary_queue(machine: Machine) -> None:
    _everyday(machine, bounds="balance_ordinary=2-4\nbalance_docker=3-5\n")
    machine.queue(ORDINARY, 7, 2, running=4)
    machine.load(40.0)

    machine.run()

    # The next unit beyond the two slots and the spare.
    assert machine.calls("systemctl") == [
        "systemctl --user enable --now example-app-runner@4.service"
    ]
    assert machine.count("ordinary_jobs") == 3
    _untouched(machine)


def test_widened_bounds_start_a_docker_runner_for_queued_image_checks(
    machine: Machine,
) -> None:
    _everyday(machine, bounds="balance_ordinary=2-4\nbalance_docker=3-5\n")
    machine.queue(DOCKER, 6, running=3)

    machine.run()

    assert machine.calls("systemctl") == [
        "systemctl --user enable --now example-app-runner-docker@4.service"
    ]
    assert machine.count("docker_runners") == 4


def test_no_room_no_slot_at_the_ceiling_of_the_day(machine: Machine) -> None:
    _everyday(machine, bounds="balance_ordinary=2-4\nbalance_docker=3-5\n")
    machine.queue(ORDINARY, 9)
    machine.queue(DOCKER, 9)
    # Load 45 of 48: not 4 under the ceiling.
    machine.load(45.0)

    machine.run()

    assert machine.calls("systemctl") == []


def test_the_load_holding_session_starts_shrinks_an_idle_kind(machine: Machine) -> None:
    _everyday(machine, bounds="balance_ordinary=1-4\nbalance_docker=1-5\n")
    machine.load(50.0)
    machine.run()

    machine.run(after_minutes=15)

    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner-docker@3.service"
    ]
    assert machine.deleted() == ["box-docker-3"]
    assert machine.count("docker_runners") == 2
    _untouched(machine)


def test_a_docker_runner_in_the_middle_of_a_job_is_never_stopped(
    machine: Machine,
) -> None:
    _everyday(machine, bounds="balance_ordinary=1-4\nbalance_docker=1-5\n")
    machine.load(50.0)
    machine.run()
    _listed(machine, busy=("box-docker-3",))

    machine.run(after_minutes=15)
    machine.run(after_minutes=1)

    assert machine.calls("systemctl") == []
    assert machine.calls("gh api -X DELETE") == []
    assert machine.count("docker_runners") == 3


def test_the_lent_main_runner_and_the_spare_are_no_slot_to_share_the_forecast_with(
    machine: Machine,
) -> None:
    # Pool: box-1, box-2, other-1, other-2 carry the label; the lent main runner and the spares
    # are not slots. Forecast 6 ordinary: this machine's share is 6 x 2 / 4 = 3, over its 2.
    _everyday(machine, bounds="balance_ordinary=2-4\nbalance_docker=3-5\n")
    machine.forecast(6, 0)

    machine.run()

    assert machine.calls("systemctl") == [
        "systemctl --user enable --now example-app-runner@4.service"
    ]


# 3. A gh call that fails or hits the budget leaves the counts as they are.

_BUDGET_LOW = """
if path == api + "/runners?per_page=100" and (state / "budget-low-after-runners").exists():
    # The listing's answer says the hourly budget has fallen under the background reserve.
    sys.stderr.write(
        "* Request at now\\n< HTTP/2.0 200 OK\\n< X-Ratelimit-Remaining: 450\\n"
        "< X-Ratelimit-Reset: @RESET@\\n< X-Ratelimit-Resource: core\\n* Request took 10ms\\n"
    )
if arguments[:3] == ["api", "-X", "DELETE"] and (state / "slow-delete").exists():
    import time
    time.sleep(40)
"""


def _gh_with_budget(box: Machine) -> None:
    marker = 'api = "repos/example-owner/example-app/actions"\n'
    text = _FAKE_GH.replace(
        marker, marker + _BUDGET_LOW.replace("@RESET@", str(int(time.time()) + 3600))
    )
    box.fake("gh", text)


def _ready_to_stop_a_docker_runner(box: Machine) -> None:
    _everyday(box, bounds="balance_ordinary=1-4\nbalance_docker=1-5\n")
    _gh_with_budget(box)
    box.load(50.0)
    box.run()
    box.now += 15 * MINUTE


def test_the_budget_running_low_mid_step_leaves_the_counts_as_they_are(
    machine: Machine,
) -> None:
    _ready_to_stop_a_docker_runner(machine)
    (machine.state / "budget-low-after-runners").touch()

    done = machine.run()

    # Nothing was stopped and no registration was deleted, so the count must still say three.
    assert machine.calls("systemctl") == []
    assert machine.calls("gh api -X DELETE") == []
    assert machine.count("docker_runners") == 3, done.stdout + done.stderr


def test_a_delete_that_times_out_leaves_the_counts_as_they_are(
    machine: Machine,
) -> None:
    _ready_to_stop_a_docker_runner(machine)
    (machine.state / "slow-delete").touch()

    done = machine.run(code=1)

    assert machine.calls("systemctl") == []
    assert machine.count("docker_runners") == 3, done.stdout + done.stderr


def test_a_failed_runners_read_leaves_the_counts_as_they_are(machine: Machine) -> None:
    _ready_to_stop_a_docker_runner(machine)
    (machine.state / "fail-gh").touch()

    machine.run(code=1)

    assert machine.calls("systemctl") == []
    assert machine.count("docker_runners") == 3


# 4. Two machines balancing at once must not both remove the last runner of a kind.


@pytest.mark.parametrize("both_at_once", [False, True])
def test_two_machines_never_both_stop_the_pools_last_docker_runners(
    tmp_path, both_at_once: bool
) -> None:
    programs = [tempfile.mkdtemp(prefix="balance-programs-") for _ in range(2)]
    try:
        boxes = []
        for index, (name, other) in enumerate((("box", "other"), ("other", "box"))):
            root = tmp_path / name
            root.mkdir()
            box = Machine(root, Path(programs[index]))
            # The README's own example bounds, each machine with its one Docker runner.
            _everyday(
                box, docker=1, bounds="balance_ordinary=2-6\nbalance_docker=0-2\n"
            )
            text = box.settings_file.read_text()
            text = text.replace("machine=box", f"machine={name}").replace(
                "other_machines='other'", f"other_machines='{other}'"
            )
            box.settings_file.write_text(text)
            boxes.append(box)
        # One listing for both: box-docker-1 and other-docker-1 are the pool's only Docker runners.
        for box in boxes:
            box.load(50.0)
            box.run()
            box.now += 15 * MINUTE
        if both_at_once:
            running = [box.start() for box in boxes]
            for process in running:
                process.wait(timeout=60)
        else:
            for box in boxes:
                box.run()
        stopped = [
            call
            for box in boxes
            for call in box.calls("systemctl")
            if "disable --now" in call
        ]
        left = sum(box.count("docker_runners") for box in boxes)
        assert left >= 1, f"the pool has no Docker runner left: {stopped}"
    finally:
        for folder in programs:
            shutil.rmtree(folder, ignore_errors=True)


# 5. The forecast and the owner's override.


def test_balance_off_in_the_installed_settings_keeps_every_count_through_a_wave(
    machine: Machine,
) -> None:
    _everyday(
        machine,
        bounds="balance_ordinary=1-6\nbalance_docker=0-6\n",
        extra="balance off\n",
    )
    machine.queue(ORDINARY, 20, 20)
    machine.queue(DOCKER, 20)
    machine.forecast(30, 10)
    machine.run()
    machine.clear_queue()
    machine.load(60.0)
    machine.run(after_minutes=20)

    assert machine.calls("systemctl") == []
    assert machine.calls("gh") == []


def test_a_forecast_ten_minutes_old_is_ignored_and_one_nine_minutes_old_holds(
    machine: Machine,
) -> None:
    _everyday(machine, bounds="balance_ordinary=1-4\nbalance_docker=3-5\n")
    machine.load(50.0)
    machine.run()
    machine.now += 15 * MINUTE
    machine.forecast(8, 0, age=9 * MINUTE)
    machine.run()
    assert machine.calls("systemctl") == []

    machine.forecast(8, 0, age=11 * MINUTE)
    machine.run(after_minutes=1)
    assert machine.calls("systemctl") == [
        "systemctl --user disable --now example-app-runner@3.service"
    ]


def test_a_forecast_file_the_tick_is_writing_is_no_forecast(machine: Machine) -> None:
    _everyday(machine, bounds="balance_ordinary=2-4\nbalance_docker=3-5\n")
    path = machine.home / ".local/state/workflow/ci-demand.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in (
        "",
        '{"at": 1',
        json.dumps({"at": "now", "ordinary": 5, "docker": 1}),
        "[]",
    ):
        path.write_text(text)
        machine.run()
    assert machine.calls("systemctl") == []


# 6. Exposure: what the balancer writes is the owner's alone and names no secret.


def test_the_log_names_no_token_and_the_settings_keep_their_mode(
    machine: Machine,
) -> None:
    _everyday(machine, bounds="balance_ordinary=2-4\nbalance_docker=3-5\n")
    machine.queue(ORDINARY, 7)
    machine.run(
        env={"GH_TOKEN": "example-secret-token", "GITHUB_TOKEN": "example-secret-token"}
    )

    log = machine.home / ".local/state/workflow/runner-balance.log"
    assert "example-secret-token" not in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600
    assert machine.settings_file.stat().st_mode & 0o777 == 0o640
    leftovers = [
        path.name for path in machine.installed.iterdir() if path.name.startswith(".")
    ]
    assert leftovers == []


def test_a_settings_value_cannot_run_a_command(machine: Machine) -> None:
    _everyday(machine, bounds="balance_ordinary=2-4\nbalance_docker=3-5\n")
    marker = machine.root / "ran"
    text = machine.settings_file.read_text().replace(
        "label='example-app-home'", f"label='example-app-home$(touch {marker})'"
    )
    machine.settings_file.write_text(text)

    machine.run(code=1)

    assert not marker.exists()


def test_after_a_budget_pause_mid_step_the_count_and_the_running_units_stay_apart(
    machine: Machine,
) -> None:
    """The pause passes; the balancer goes on from the lowered count and a Docker runner it never
    stopped keeps running beyond the count."""
    _ready_to_stop_a_docker_runner(machine)
    (machine.state / "budget-low-after-runners").touch()
    machine.run()
    (machine.state / "budget-low-after-runners").unlink()
    for path in (machine.home / ".local/state/workflow/github-budget").glob(
        "*budget.json"
    ):
        path.unlink()

    machine.run(after_minutes=6)

    stopped = [call for call in machine.calls("systemctl") if "disable --now" in call]
    # The units still running: docker@1 to docker@3, less the ones stopped.
    running = {f"example-app-runner-docker@{k}.service" for k in (1, 2, 3)} - {
        call.rsplit(" ", 1)[1] for call in stopped
    }
    assert len(running) == machine.count("docker_runners"), (stopped, running)
