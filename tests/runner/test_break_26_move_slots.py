"""Breaking tests for pull request 26: a machine's PR slots move to another machine.

They replay a pool of two machines as an installation runs it every day, with neutral names: the
machine that also runs the planner, main's runners and Docker runners (`home`) gives its PR slots
to a second machine (`office`) that runs only ordinary and Docker runners. They also replay the
usage log an existing installation already keeps, and the failures such a pool meets: the other
machine down or just back, GitHub's list unreadable, two supervisors at once, settings left out.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from slot_stand_ins import (
    DOCKER,
    FIRST,
    LABEL,
    MAIN,
    PREFIX,
    ROUND,
    SHARED_JOB_MEMORY,
    STANDBY,
    Machine,
    make_machine,
    runner_name,
)

HOME = "home"
OFFICE = "office"
SPARE = runner_name(HOME, number=1)


def _settings(machine: Machine, text: str) -> None:
    path = machine.root / "home" / ".config" / "shipline" / "runner.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _home_settings(machine: Machine, slots: int, extra: str = "") -> None:
    """The machine that runs everything: planner, main, ordinary and Docker runners."""
    _settings(
        machine,
        f"machine={HOME}\nother_machines={OFFICE}\nroles='planner main ordinary docker'\n"
        f"ordinary_jobs={slots}\ndocker_runners=3\n{extra}",
    )


def _office_settings(machine: Machine, slots: int, extra: str = "") -> None:
    machine.name = OFFICE
    _settings(
        machine,
        f"machine={OFFICE}\nother_machines={HOME}\nroles='ordinary docker'\n"
        f"ordinary_jobs={slots}\ndocker_runners=2\n{extra}",
    )


@pytest.fixture
def home(tmp_path: Path) -> Machine:
    machine = make_machine(tmp_path)
    # Docker runners need a device for their virtual machines.
    (tmp_path / "no-kvm").write_text("")
    return machine


def _office_runner(status: str, number: int = 1, labels: list[str] | None = None):
    return (runner_name(OFFICE, number=number), status, labels or [STANDBY, LABEL, FIRST])


def _github_deletes_registrations(home: Machine) -> None:
    # The shared gh stand-in removes labels only; here GitHub also removes a registration.
    stand_in = home.root / "bin" / "gh"
    stand_in.rename(home.root / "bin" / "gh-labels")
    stand_in.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, re, sys\n"
        f"state = {str(home.state)!r}\n"
        "words = sys.argv[1:]\n"
        "path = next((w for w in words if w.startswith('repos/')), '')\n"
        "found = re.search(r'/actions/runners/([0-9]+)$', path)\n"
        "if found and '-X' in words and words[words.index('-X') + 1] == 'DELETE':\n"
        "    open(f'{state}/calls.log', 'a').write(' '.join(['gh', *words]) + '\\n')\n"
        "    runners = json.load(open(f'{state}/runners.json'))\n"
        "    runners['runners'] = [r for r in runners['runners'] if r['id'] != int(found[1])]\n"
        "    json.dump(runners, open(f'{state}/runners.json', 'w'))\n"
        "    sys.exit(0)\n"
        "os.execv(os.path.join(os.path.dirname(__file__), 'gh-labels'), ['gh', *words])\n"
    )
    stand_in.chmod(0o755)


def _installed_home(home: Machine) -> Machine:
    _home_settings(home, 0)
    done = home.install()
    assert done.returncode == 0, done.stderr
    (home.state / "calls.log").unlink(missing_ok=True)
    return home


# --- The move on the machine that gives its slots ---------------------------------------------


def test_home_with_every_role_installs_with_no_pr_slot_and_keeps_main_planner_and_docker(
    home: Machine,
) -> None:
    _home_settings(home, 0)

    done = home.install()

    assert done.returncode == 0, done.stderr
    settings = home.pool_settings.read_text().splitlines()
    assert "ordinary_jobs=0" in settings
    enabled = " ".join(home.calls("systemctl"))
    for unit in (
        f"{PREFIX}-runner-planner.service",
        f"{PREFIX}-runner-main.service",
        f"{PREFIX}-runner-main-2.service",
        f"{PREFIX}-runner@1.service",
        f"{PREFIX}-runner-spare.service",
        f"{PREFIX}-runner-docker@3.service",
    ):
        assert f"enable --now {unit}" in enabled, unit
    assert f"enable --now {PREFIX}-runner@2.service" not in enabled


def test_the_move_retires_homes_old_slot_registrations_and_nothing_else(home: Machine) -> None:
    """Before: 4 slots and a spare (home-1 to home-5). After: the spare alone. Main's, Docker's,
    the planner's and the other machine's registrations stay."""
    home.runners(
        *[(runner_name(HOME, number=n), "online", [STANDBY, LABEL, FIRST]) for n in range(1, 6)],
        *[(runner_name(HOME, "docker", n), "online", [DOCKER]) for n in range(1, 4)],
        (runner_name(HOME, "main"), "online", [MAIN]),
        (f"{HOME}-main-2", "online", [MAIN]),
        (f"{HOME}-planner", "online", [f"{LABEL}-planner"]),
        _office_runner("online", 1),
        _office_runner("online", 2),
    )
    _github_deletes_registrations(home)
    _home_settings(home, 0)

    done = home.install()

    assert done.returncode == 0, done.stderr
    deleted = sorted(
        call.rsplit("/", 1)[1] for call in home.calls("gh") if "-X DELETE" in call
    )
    # ids 2..5 are home-2..home-5
    assert deleted == ["2", "3", "4", "5"]


def test_after_the_move_home_takes_no_slot_while_office_has_its_runners_online(
    home: Machine,
) -> None:
    _installed_home(home)
    home.runners(_office_runner("online", 1), _office_runner("busy", 2), (SPARE, "online", [STANDBY]))

    assert home.ordinary_slots() == 0
    assert home.planner_slots() == 0
    done = home.spare_round()
    assert done.returncode == ROUND, done.stderr
    assert home.labels_of(SPARE) == ["self-hosted", STANDBY]


# --- The other machine down, then back --------------------------------------------------------


def test_office_switched_off_and_its_registrations_gone_home_takes_one_slot(home: Machine) -> None:
    _installed_home(home)
    home.runners((runner_name(OFFICE, "docker", 1), "offline", [DOCKER]), (SPARE, "online", [STANDBY]))

    done = home.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert home.labels_of(SPARE) == ["self-hosted", STANDBY, LABEL, FIRST]


def test_office_just_back_with_standby_runners_only_home_keeps_its_slot_until_labelled(
    home: Machine,
) -> None:
    """Office's fresh registrations carry only the standby label until office's own supervisor
    labels them: home must not give its slot back to runners that cannot take a job yet."""
    _installed_home(home)
    home.runners(
        _office_runner("online", 1, [STANDBY]),
        _office_runner("online", 2, [STANDBY]),
        (SPARE, "online", [STANDBY, LABEL, FIRST]),
    )

    done = home.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert home.labels_of(SPARE) == ["self-hosted", STANDBY, LABEL, FIRST]


def test_office_labelled_again_home_gives_the_slot_back(home: Machine) -> None:
    _installed_home(home)
    home.runners(_office_runner("online", 1), (SPARE, "online", [STANDBY, LABEL, FIRST]))

    done = home.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert home.labels_of(SPARE) == ["self-hosted", STANDBY]


def test_office_mains_lent_runner_does_not_count_as_office_being_up(home: Machine) -> None:
    """Office has no main role; but if any machine's main runner is lent to PR jobs, it is no PR
    slot: home's spare still takes one."""
    _installed_home(home)
    home.runners(
        (f"{OFFICE}-main-2", "online", [MAIN, LABEL, FIRST]),
        (f"{OFFICE}-planner", "online", [f"{LABEL}-planner", LABEL]),
        (SPARE, "online", [STANDBY]),
    )

    assert home.ordinary_slots() == 1


def test_github_unreadable_while_office_is_down_home_changes_nothing(home: Machine) -> None:
    _installed_home(home)
    home.runners((SPARE, "online", [STANDBY]))
    home.lose_github()

    done = home.spare_round()

    assert done.returncode == ROUND, done.stderr
    assert [call for call in home.calls("gh") if "/labels" in call] == []


def test_two_spare_supervisors_at_once_grant_the_fallback_slot_once(home: Machine) -> None:
    _installed_home(home)
    home.runners((SPARE, "online", [STANDBY]))
    lock = home.installed / "spare.lock"
    holder = subprocess.Popen(
        ["flock", str(lock), "sleep", "5"], env=home.env | {"PATH": "/usr/bin:/bin"}
    )
    try:
        subprocess.run(["sleep", "0.3"], check=True)
        second = home.spare_round()
    finally:
        holder.kill()
        holder.wait()

    assert second.returncode == 1
    assert [call for call in home.calls("gh") if "/labels" in call] == []


# --- The receiving machine: its own memory cap ------------------------------------------------


def _memory_flag(machine: Machine, kind: str) -> str:
    (machine.state / "calls.log").unlink(missing_ok=True)
    machine.start_job_container(kind)
    runs = [call for call in machine.calls("docker") if call.startswith("docker run")]
    assert runs, machine.calls("docker")
    words = runs[-1].split()
    return words[words.index("--memory") + 1]


def test_office_takes_more_jobs_with_its_own_cap_and_home_keeps_the_shared_one(
    tmp_path: Path,
) -> None:
    office = make_machine(tmp_path / "office")
    (tmp_path / "office" / "no-kvm").write_text("")
    _office_settings(office, 8, "job_memory=6g\n")
    done = office.install()
    assert done.returncode == 0, done.stderr

    assert _memory_flag(office, "ordinary") == "6g"

    home = make_machine(tmp_path / "home")
    (tmp_path / "home" / "no-kvm").write_text("")
    _installed_home(home)
    assert _memory_flag(home, "ordinary") == SHARED_JOB_MEMORY
    assert _memory_flag(home, "main") == SHARED_JOB_MEMORY


def test_office_drops_its_cap_and_reinstalls_and_the_shared_value_holds_again(
    tmp_path: Path,
) -> None:
    office = make_machine(tmp_path)
    (tmp_path / "no-kvm").write_text("")
    _office_settings(office, 8, "job_memory=6g\n")
    assert office.install().returncode == 0
    _office_settings(office, 8)

    done = office.install()

    assert done.returncode == 0, done.stderr
    assert _memory_flag(office, "ordinary") == SHARED_JOB_MEMORY


@pytest.mark.parametrize("given", ["6g 7g", "6g;touch x", "6G", "6gb", "0g", "6"])
def test_a_mistyped_cap_stops_the_install_before_any_runner_stops(
    tmp_path: Path, given: str
) -> None:
    office = make_machine(tmp_path)
    _office_settings(office, 8, f"job_memory='{given}'\n")

    done = office.install()

    assert done.returncode != 0
    assert office.calls("systemctl") == []
    assert not (office.checkout / "x").exists()


def test_a_cap_in_the_environment_never_reaches_a_job(tmp_path: Path) -> None:
    office = make_machine(tmp_path)
    _office_settings(office, 8)
    assert office.install().returncode == 0
    office.env = office.env | {"job_memory": "1g"}

    assert _memory_flag(office, "ordinary") == SHARED_JOB_MEMORY


# --- Refusals that keep PR jobs running -------------------------------------------------------


def test_home_with_no_spare_is_refused_no_slot(home: Machine) -> None:
    _home_settings(home, 0, "ordinary_spares=0\n")

    done = home.install()

    assert done.returncode != 0
    assert home.calls("systemctl") == []


def test_home_alone_is_refused_no_slot(home: Machine) -> None:
    _settings(
        home,
        f"machine={HOME}\nother_machines=\nroles='planner main ordinary docker'\n"
        "ordinary_jobs=0\ndocker_runners=3\n",
    )

    done = home.install()

    assert done.returncode != 0
    assert home.calls("systemctl") == []


# --- The week of usage the move rests on ------------------------------------------------------


def _line(at: datetime, pressure: float, available: int, busy: str) -> str:
    return (
        f"{at:%Y-%m-%dT%H:%M:%SZ} free_mb=5190 available_mb={available} swap_in_mb_s=0.1"
        f" swap_out_mb_s=7.9 cpu_pressure_60s={pressure:.2f} load_1m=51.64 busy={busy}"
    )


def _week(
    days: int,
    pressure: float,
    available: int,
    now: datetime,
    skip: tuple[int, int] | None = None,
    busy: str = "office-1,office-2,office-docker-1",
) -> list[str]:
    """A line a minute over the last `days` whole days before today; `skip` leaves out the
    minutes [start, end) of each day."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lines = []
    for back in range(days, 0, -1):
        start = today - timedelta(days=back)
        for minute in range(24 * 60):
            if skip and skip[0] <= minute < skip[1]:
                continue
            lines.append(_line(start + timedelta(minutes=minute), pressure, available, busy))
    return lines


def _report(machine: Machine, *lines: str, rotated: list[str] | None = None):
    """usage.py report --days 7 as runner/README.md says to run it: the installed copy, in the
    machine's home, with the log where an existing installation keeps it."""
    folder = machine.root / "home" / ".local" / "state" / "ci-runner"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "usage.log").write_text("".join(f"{line}\n" for line in lines))
    if rotated is not None:
        (folder / "usage.log.1").write_text("".join(f"{line}\n" for line in rotated))
    (folder / "usage.state").write_text(json.dumps({"boot": "x", "time": 0, "in": 0, "out": 0}))
    return subprocess.run(
        [sys.executable, str(machine.installed / "usage.py"), "report", "--days", "7"],
        env=machine.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def office(tmp_path: Path) -> Machine:
    machine = make_machine(tmp_path)
    (tmp_path / "no-kvm").write_text("")
    _office_settings(machine, 2)
    done = machine.install()
    assert done.returncode == 0, done.stderr
    return machine


NOW = datetime.now(UTC)


def test_a_week_of_room_found_where_an_existing_installation_keeps_its_log(
    office: Machine,
) -> None:
    done = _report(office, *_week(8, 9.5, 20000, NOW))

    assert done.returncode == 0, done.stdout
    assert done.stdout.strip().splitlines()[-1].startswith("Room every day: yes")


def test_the_week_split_across_the_rotated_log_still_counts(office: Machine) -> None:
    lines = _week(8, 9.5, 20000, NOW)

    done = _report(office, *lines[4000:], rotated=lines[:4000])

    assert done.returncode == 0, done.stdout


def test_home_like_load_shows_no_room(office: Machine) -> None:
    done = _report(office, *_week(8, 36.15, 9183, NOW, busy="home-1,home-docker-1,home-main"))

    assert done.returncode == 1
    assert "Room every day: no" in done.stdout


def test_a_machine_reinstalled_three_days_ago_is_not_idle(office: Machine) -> None:
    done = _report(office, *_week(3, 1.0, 50000, NOW))

    assert done.returncode == 1
    assert "no lines" in done.stdout


def test_a_day_with_two_hours_of_no_lines_is_not_idle(office: Machine) -> None:
    done = _report(office, *_week(8, 1.0, 50000, NOW, skip=(13 * 60 + 30, 15 * 60 + 30)))

    assert done.returncode == 1


def test_a_log_with_a_45_minute_gap_inside_one_hour_every_day(office: Machine) -> None:
    """Weak spot of the report (usage.py, #9): an hour with any line counts as measured."""
    done = _report(office, *_week(8, 1.0, 50000, NOW, skip=(14 * 60 + 5, 14 * 60 + 50)))

    assert done.returncode == 1, "an hour with 15 of 60 lines counted as measured\n" + done.stdout


def test_one_busy_hour_of_the_week_shows_no_room(office: Machine) -> None:
    lines = _week(8, 5.0, 30000, NOW)
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    busy_hour = today - timedelta(days=3) + timedelta(hours=16)
    lines = [
        _line(datetime.strptime(line[:20], "%Y-%m-%dT%H:%M:%SZ"), 40.0, 4000, "office-1")
        if line.startswith(f"{busy_hour:%Y-%m-%dT%H}")
        else line
        for line in lines
    ]

    done = _report(office, *lines)

    assert done.returncode == 1


# --- A move with the receiving machine down ---------------------------------------------------


def test_home_moving_its_slots_while_office_is_down_is_refused(home: Machine) -> None:
    """A move is both machines agreeing: with office off (no online ordinary runner of it in
    GitHub's list, which install.sh reads anyway), home's install must not drop its 4 slots to
    the spare's single fallback slot."""
    home.runners(
        *[(runner_name(HOME, number=n), "online", [STANDBY, LABEL, FIRST]) for n in range(1, 6)],
        _office_runner("offline", 1),
        _office_runner("offline", 2),
    )
    _github_deletes_registrations(home)
    _home_settings(home, 0)

    done = home.install()

    assert done.returncode != 0, "home installed with 0 PR slots while office was down"
    assert home.calls("systemctl") == []
