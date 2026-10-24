"""Breaking tests for pull request 53: the flow page runs from Shipline's own checkout, as the
tick does.

Each test installs the page as an owner would, then runs the units' commands the way systemd
would: in the unit's folder, with only the unit's environment and the user's own variables."""

import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline

_STARTED = 20


def _units(workflow: Workflow) -> Path:
    return Path(workflow.env["HOME"]) / ".config" / "systemd" / "user"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _settings(workflow: Workflow, text: str, checkout: Path | None = None) -> None:
    settings = (checkout or workflow.repo) / ".github" / "runner" / "settings.sh"
    settings.write_text(settings.read_text() + text)


def _as_systemd(workflow: Workflow, unit: Path) -> tuple[list[str], Path, dict[str, str]]:
    """The command, folder and environment systemd gives the unit's ExecStart."""
    lines = unit.read_text().splitlines()

    def value(key: str) -> str:
        return next(line.split("=", 1)[1] for line in lines if line.startswith(f"{key}="))

    folder = Path(value("WorkingDirectory").replace("%%", "%"))
    argv = shlex.split(value("ExecStart").replace("%%", "%").replace("$$", "$"))
    env = {
        key: workflow.env[key]
        for key in ("HOME", "USER", "LANG", "XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
    }
    for line in lines:
        if line.startswith('Environment="'):
            key, given = line[len('Environment="') : -1].split("=", 1)
            env[key] = given.replace("%%", "%")
    return argv, folder, env


def _pass(workflow: Workflow) -> subprocess.CompletedProcess[str]:
    argv, folder, env = _as_systemd(workflow, _units(workflow) / "flow-page.service")
    return subprocess.run(
        argv, cwd=folder, env=env, capture_output=True, text=True, timeout=90, check=False
    )


def _serve(workflow: Workflow) -> subprocess.Popen[str]:
    argv, folder, env = _as_systemd(workflow, _units(workflow) / "flow-page-serve.service")
    server = subprocess.Popen(
        argv, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(server.stdout.readline()), daemon=True)
    reader.start()
    reader.join(_STARTED)
    if not lines or "serving" not in lines[0]:
        server.kill()
        _, error = server.communicate(timeout=10)
        pytest.fail(f"the server did not start: {lines} {error}")
    return server


def _get(port: int, path: str = "/index.html") -> tuple[int, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as answer:
        return answer.status, answer.read().decode()


def _stop(server: subprocess.Popen[str]) -> None:
    server.kill()
    server.communicate(timeout=10)


def _link(workflow: Workflow, home: Path) -> Path:
    """`~/.local/bin/shipline`, as `install-timer` points it at the checkout."""
    link = Path(workflow.env["HOME"]) / ".local" / "bin" / "shipline"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(home / "bin" / "shipline")
    return link


def _flow_units(workflow: Workflow) -> list[str]:
    return sorted(path.name for path in _units(workflow).iterdir() if "flow-page" in path.name)


def test_installed_from_a_worktree_through_the_link_the_page_is_written_and_served_after_the_worktree_goes(
    workflow: Workflow,
) -> None:
    """The owner installs from a worktree of the app, through the `shipline` link; the pull
    request of that worktree then merges and its worktree is removed. The timer's pass and the
    server, run as systemd runs them, read the main checkout and keep serving."""
    home = install_shipline(workflow)
    link = _link(workflow, home)
    worktree = workflow.add_worktree(7)
    port = _free_port()
    _settings(workflow, f"flow_page_address=127.0.0.1\nflow_page_port={port}\n")

    installed = subprocess.run(
        ["bash", str(link), "install-flow-page-timer"],
        cwd=worktree,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    for name in ("flow-page.service", "flow-page-serve.service"):
        text = (_units(workflow) / name).read_text()
        assert str(worktree) not in text
        assert f"{home}/bin/shipline" in text
    workflow.git("worktree", "remove", "--force", str(worktree))

    written = _pass(workflow)
    assert written.returncode == 0, written.stdout + written.stderr
    page = Path(workflow.env["HOME"]) / ".local" / "state" / "workflow" / "flow-page" / "site"
    assert (page / "index.html").is_file()

    server = _serve(workflow)
    try:
        status, body = _get(port)
        assert status == 200
        assert body == (page / "index.html").read_text()
    finally:
        _stop(server)


def test_the_units_command_reads_the_app_s_settings_even_from_shipline_s_folder(
    workflow: Workflow,
) -> None:
    """The units' command names the app; started from Shipline's own checkout, which is a
    git repository with settings of its own, it still reads the app's."""
    home = install_shipline(workflow)
    _settings(workflow, "flow_page_port=8601\n")
    (home / ".github" / "runner").mkdir(parents=True)
    (home / ".github" / "runner" / "settings.sh").write_text("flow_page_port=9999\n")
    done = workflow.shipline("install-flow-page-timer")
    assert done.returncode == 0, done.stderr

    for name in ("flow-page.service", "flow-page-serve.service"):
        argv, _, env = _as_systemd(workflow, _units(workflow) / name)
        command = argv[: argv.index("flow-page") + 1]
        shown = subprocess.run(
            [*command, "--settings"],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert shown.returncode == 0, shown.stderr
        assert shown.stdout.split() == ["127.0.0.1", "8601"]


def test_the_server_keeps_serving_across_an_update_of_shipline_and_restarts_without_reinstall(
    workflow: Workflow,
) -> None:
    """Shipline's checkout moves on (a self-update) while the page is served: the server keeps
    answering, and when it is started again (Restart=always) the same unit serves again."""
    home = install_shipline(workflow)
    port = _free_port()
    _settings(workflow, f"flow_page_address=127.0.0.1\nflow_page_port={port}\n")
    done = workflow.shipline("install-flow-page-timer")
    assert done.returncode == 0, done.stderr
    assert _pass(workflow).returncode == 0
    before = {name: (_units(workflow) / name).read_text() for name in _flow_units(workflow)}

    server = _serve(workflow)
    try:
        assert _get(port)[0] == 200
        env = {**workflow.env, "GIT_DIR": str(home / ".git"), "GIT_WORK_TREE": str(home)}
        script = home / "scripts" / "workflow" / "flow_page_html.py"
        script.write_text(script.read_text() + "\n# A newer Shipline.\n")
        subprocess.run(["git", "commit", "-qam", "chore: update"], cwd=home, env=env, check=True)
        assert _get(port)[0] == 200
    finally:
        _stop(server)

    server = _serve(workflow)
    try:
        assert _get(port)[0] == 200
    finally:
        _stop(server)
    assert {name: (_units(workflow) / name).read_text() for name in _flow_units(workflow)} == before


def test_installing_twice_and_after_uninstall_leaves_one_of_each_unit(workflow: Workflow) -> None:
    install_shipline(workflow)
    _settings(workflow, "flow_page_port=8601\n")
    for step in ("install-flow-page-timer", "install-flow-page-timer", "uninstall", "install-flow-page-timer"):
        done = workflow.shipline(step)
        assert done.returncode == 0, f"{step}: {done.stderr}"

    assert _flow_units(workflow) == ["flow-page-serve.service", "flow-page.service", "flow-page.timer"]
    for name in ("flow-page.service", "flow-page-serve.service"):
        starts = [
            line for line in (_units(workflow) / name).read_text().splitlines()
            if line.startswith("ExecStart=")
        ]
        assert len(starts) == 1, starts


def test_uninstall_takes_the_flow_page_off_too(workflow: Workflow) -> None:
    """The flow page's units now run Shipline's launcher. `shipline uninstall` promises to take
    Shipline off in one step; a server it leaves restarts every 30 seconds once Shipline's
    checkout is gone."""
    install_shipline(workflow)
    _settings(workflow, "flow_page_port=8601\n")
    assert workflow.shipline("install-flow-page-timer").returncode == 0

    done = workflow.shipline("uninstall")

    assert done.returncode == 0, done.stderr
    left = [
        name for name in _flow_units(workflow)
        if "/bin/shipline" in (_units(workflow) / name).read_text()
    ]
    assert left == [], f"left running Shipline after uninstall: {left}"


def test_without_a_repository_nothing_is_installed(workflow: Workflow, tmp_path: Path) -> None:
    install_shipline(workflow)
    outside = tmp_path / "outside"
    outside.mkdir()

    done = workflow.shipline("install-flow-page-timer", cwd=outside)

    assert done.returncode != 0
    assert "no git repository" in done.stderr
    assert not _units(workflow).exists() or _flow_units(workflow) == []
    assert workflow.calls("systemctl") == []


def test_settings_that_cannot_be_used_leave_the_installed_units_as_they_were(
    workflow: Workflow,
) -> None:
    install_shipline(workflow)
    _settings(workflow, "flow_page_port=8601\n")
    assert workflow.shipline("install-flow-page-timer").returncode == 0
    before = {name: (_units(workflow) / name).read_text() for name in _flow_units(workflow)}
    _settings(workflow, "flow_page_port=port\n")

    done = workflow.shipline("install-flow-page-timer")

    assert done.returncode != 0
    assert {name: (_units(workflow) / name).read_text() for name in _flow_units(workflow)} == before


def test_two_installs_at_once_leave_whole_units(workflow: Workflow) -> None:
    home = install_shipline(workflow)
    _settings(workflow, "flow_page_port=8601\n")
    assert workflow.shipline("install-flow-page-timer").returncode == 0
    alone = {name: (_units(workflow) / name).read_text() for name in _flow_units(workflow)}

    runs = [
        subprocess.Popen(
            ["bash", str(home / "bin" / "shipline"), "install-flow-page-timer"],
            cwd=workflow.repo,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    for run in runs:
        _, error = run.communicate(timeout=60)
        assert run.returncode == 0, error

    assert {name: (_units(workflow) / name).read_text() for name in _flow_units(workflow)} == alone


def test_a_settings_file_that_hangs_stops_the_installer_in_time(workflow: Workflow) -> None:
    """The settings are read with a 5-second limit; a file that starts a child that hangs must
    not hold the installer past it."""
    install_shipline(workflow)
    _settings(workflow, "/usr/bin/sleep 40\nflow_page_port=8601\n")

    started = time.monotonic()
    done = workflow.shipline("install-flow-page-timer")
    took = time.monotonic() - started

    assert done.returncode != 0
    assert took < 20, f"the installer took {took:.0f} seconds"
    assert os.path.exists(workflow.repo)
