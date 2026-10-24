"""Breaking tests for pull request 59: a config and programs kept outside the repository.

Each test replays a daily use of an installation that adds no file to its repository for
Shipline. Its orchestrator writes the config into the home config folder, named after the
repository the runner settings name (owner and name the same word here, `example-app`), with
the small programs it needs and its parity known list beside it. The repository keeps its own
programs (`scripts/workflow/...`, `scripts/e2e-plan.py`), which the config names by relative
path. The tick, the gate and a pull loop read the main checkout; sessions run in worktrees
under `.claude/worktrees/ticket-<n>`; every command must run the same from each.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline

_REPOSITORY = "example-app/example-app"
_HERE = Path(__file__).resolve().parents[2]

# Each program says which copy ran, from where, with which arguments.
_PROGRAM = "import os, sys\nprint('{marker}', os.getcwd(), sys.argv[1:])\n"

_CONFIG = {
    "state_folder": ".local/example-state",
    "labels": {"workflow": "example-workflow"},
    "commands": {
        "pre_train_checks": "python3 scripts/workflow/example-check.py",
        "browser_plan": "python3 scripts/e2e-plan.py --plan",
        "hunt_app": "python3 @install/hunt-app.py",
        "advisory_watch": "python3 advisory.py.d/watch.py",
    },
    "parity": {"known": "parity.known"},
}


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _installation(workflow: Workflow, config: dict | None = None) -> Path:
    """Shipline in a checkout of its own; a repository with no shipline.json, whose runner
    settings name it; and the orchestrator's file in the home config folder. Returns it."""
    install_shipline(workflow)
    repo = workflow.repo
    (repo / ".github/runner/settings.sh").write_text(f"repository={_REPOSITORY}\n")
    (repo / "scripts/workflow").mkdir(parents=True, exist_ok=True)
    (repo / "scripts/workflow/example-check.py").write_text(_PROGRAM.format(marker="main-check"))
    (repo / "scripts/e2e-plan.py").write_text(_PROGRAM.format(marker="main-plan"))
    workflow.git("rm", "--quiet", "shipline.json")
    workflow.git("add", "-A")
    workflow.git("commit", "--quiet", "-m", "chore: the repository's own programs")
    workflow.git("push", "--quiet", "origin", "main")
    folder = Path(workflow.env["HOME"]) / ".config/shipline/example-app-example-app"
    folder.mkdir(parents=True)
    (folder / "hunt-app.py").write_text(_PROGRAM.format(marker="installed-hunt"))
    (folder / "advisory.py.d").mkdir()
    (folder / "advisory.py.d/watch.py").write_text(_PROGRAM.format(marker="installed-watch"))
    path = folder / "shipline.json"
    path.write_text(json.dumps(config or _CONFIG))
    return path


def _worktrees(workflow: Workflow) -> list[Path]:
    """Two ticket worktrees whose own copies of the programs would say so if they ran."""
    made = [workflow.add_worktree(7), workflow.add_worktree(8)]
    for worktree in made:
        (worktree / "scripts/workflow/example-check.py").write_text("raise SystemExit('worktree')\n")
        (worktree / "scripts/e2e-plan.py").write_text("raise SystemExit('worktree')\n")
        (worktree / "hunt-app.py").write_text("raise SystemExit('worktree')\n")
        (worktree / "shipline.json").write_text('{"state_folder": ".worktree"}')
    return made


def _config(workflow: Workflow, cwd: Path, *args: str, env: dict | None = None):
    return subprocess.run(
        ["python3", str(workflow.script.parent / "workflow_config.py"), *args],
        cwd=cwd,
        env={**workflow.env, **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_home_file_is_read_alike_from_main_and_every_ticket_worktree(
    workflow: Workflow,
) -> None:
    path = _installation(workflow)
    outputs = []
    for cwd in [workflow.repo, *_worktrees(workflow)]:
        done = _config(workflow, cwd, "shell")
        assert done.returncode == 0, done.stderr
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    assert f"SHIPLINE_CONFIG={path}\n" in outputs[0]
    assert f"SHIPLINE_INSTALL_FOLDER={path.parent}\n" in outputs[0]
    assert "SHIPLINE_STATE_FOLDER=.local/example-state\n" in outputs[0]
    assert "SHIPLINE_LABEL_WORKFLOW=example-workflow\n" in outputs[0]
    assert f"SHIPLINE_REPOSITORY={_REPOSITORY}\n" in outputs[0]


@pytest.mark.parametrize(
    ("command", "marker", "program"),
    [
        ("pre-train-checks", "main-check", "repo:scripts/workflow/example-check.py"),
        ("browser-plan", "main-plan", "repo:scripts/e2e-plan.py"),
        ("hunt-app", "installed-hunt", "home:hunt-app.py"),
        ("advisory-watch", "installed-watch", "home:advisory.py.d/watch.py"),
    ],
)
def test_each_command_runs_the_same_program_from_main_worktrees_and_a_pull_loop(
    workflow: Workflow, command: str, marker: str, program: str
) -> None:
    path = _installation(workflow)
    worktrees = _worktrees(workflow)
    elsewhere = workflow.root / "pull-loop"
    elsewhere.mkdir()
    runs = [(cwd, ()) for cwd in [workflow.repo, *worktrees]]
    # A loop outside the repository names it, as the timer and the hooks do.
    runs.append((elsewhere, ("--repo", str(workflow.repo))))
    for cwd, named in runs:
        done = workflow.shipline(*named, command, "an argument", cwd=cwd)
        assert done.returncode == 0, (cwd, done.stderr)
        ran_in = workflow.repo if named else cwd
        assert done.stdout == f"{marker} {ran_in} ['an argument']\n" if command != "browser-plan" \
            else done.stdout == f"{marker} {ran_in} ['--plan', 'an argument']\n", done.stdout
    # The Python readers (the gate imports the same loader) resolve the same absolute file.
    where, name = program.split(":")
    expected = (workflow.repo if where == "repo" else path.parent) / name
    for cwd in [workflow.repo, *worktrees]:
        done = subprocess.run(
            [
                "python3",
                "-c",
                "import sys, json; sys.path.insert(0, sys.argv[1]); import workflow_config as c; "
                "print(json.dumps(c.load().command(sys.argv[2])))",
                str(workflow.script.parent),
                command.replace("-", "_"),
            ],
            cwd=cwd,
            env=workflow.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert str(expected) in json.loads(done.stdout)


def test_the_codex_bridge_reads_the_home_state_folder_from_a_worktree(workflow: Workflow) -> None:
    _installation(workflow)
    worktree = _worktrees(workflow)[0]
    done = _config(workflow, workflow.root, "json", str(worktree))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["state_folder"] == ".local/example-state"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example-app/example-app.git",
        "git@github.com:example-app/example-app.git",
        "ssh://git@github.com/example-app/example-app",
    ],
)
def test_without_runner_settings_the_origin_finds_the_home_file(
    workflow: Workflow, url: str
) -> None:
    path = _installation(workflow)
    (workflow.repo / ".github/runner/settings.sh").unlink()
    workflow.git("remote", "set-url", "origin", url)
    for cwd in [workflow.repo]:
        done = _config(workflow, cwd, "shell")
        assert done.returncode == 0, done.stderr
        assert f"SHIPLINE_CONFIG={path}\n" in done.stdout


def test_parity_uses_the_known_list_beside_the_home_file(workflow: Workflow) -> None:
    path = _installation(workflow)
    # The repository's own copy of the tick differs from Shipline's by one word.
    ours = (workflow.home / "scripts/workflow/tick").read_text()
    assert "systemd user timer" in ours
    theirs = ours.replace("systemd user timer", "example-app timer", 1)
    (workflow.repo / "scripts/workflow/tick").write_text(theirs)
    (workflow.repo / "scripts/workflow/tick").chmod(0o755)
    workflow.git("add", "-A")
    worktree = _worktrees(workflow)[0]
    tools = workflow.home / "tools"
    tools.mkdir()
    (tools / "parity").write_text((_HERE / "tools/parity").read_text())

    def parity(cwd: Path, checkout: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(tools / "parity"), "--summary", str(checkout)],
            cwd=cwd, env=workflow.env, capture_output=True, text=True, timeout=60, check=False,
        )

    known = path.parent / "parity.known"
    # Without the known list in the installation folder the read is refused with its path.
    done = parity(workflow.repo, workflow.repo)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert done.stderr.startswith(f"{known}: cannot be read"), done.stderr
    known.write_text(
        "# The installation names its own timer.\n"
        "rewrite scripts/workflow/tick |systemd user timer|example-app timer|\n"
    )
    for cwd, checkout in [(workflow.repo, workflow.repo), (worktree, workflow.repo),
                          (workflow.root, workflow.repo)]:
        done = parity(cwd, checkout)
        assert done.returncode in (0, 1), done.stderr
        lines = done.stdout.splitlines()
        assert not any(line.endswith(" scripts/workflow/tick scripts/workflow/tick")
                       for line in lines), done.stdout
        assert not any(line.startswith("unused:") for line in lines), done.stdout


@pytest.mark.parametrize(
    "text",
    ['{"state_folder": ".local/exa', "", '{"labels": {"workflow": 7}}'],
    ids=["half-written", "truncated", "wrong-kind"],
)
def test_a_bad_home_file_stops_every_reader_and_names_the_file(
    workflow: Workflow, text: str
) -> None:
    path = _installation(workflow)
    path.write_text(text)
    for cwd in [workflow.repo, *_worktrees(workflow)]:
        done = _config(workflow, cwd, "shell")
        assert (done.returncode, done.stdout) == (1, ""), done.stdout
        assert done.stderr.startswith(str(path)), done.stderr
        # A gate check never passes as "no check to fail" on an unreadable config.
        checks = workflow.shipline("pre-train-checks", cwd=cwd)
        assert checks.returncode == 1, checks.stderr
        assert checks.stderr.startswith(str(path)), checks.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_an_unreadable_home_file_is_refused_not_skipped(workflow: Workflow) -> None:
    path = _installation(workflow)
    path.chmod(0)
    try:
        done = _config(workflow, workflow.repo, "shell")
    finally:
        path.chmod(0o600)
    assert done.returncode == 1
    assert done.stderr.startswith(str(path)), done.stderr


def test_a_missing_home_file_falls_back_to_the_defaults(workflow: Workflow) -> None:
    path = _installation(workflow)
    path.unlink()
    done = _config(workflow, workflow.repo, "shell")
    assert done.returncode == 0, done.stderr
    assert "SHIPLINE_STATE_FOLDER=.local/workflow\n" in done.stdout
    assert "SHIPLINE_CONFIG=''\n" in done.stdout
    assert "SHIPLINE_INSTALL_FOLDER=''\n" in done.stdout


def test_readers_at_once_while_the_orchestrator_replaces_the_file(workflow: Workflow) -> None:
    """The orchestrator rewrites its file by renaming a finished copy over it, while the tick,
    the gate and sessions read it. Every reader sees one whole file, never an error."""
    path = _installation(workflow)
    worktrees = _worktrees(workflow)
    versions = [dict(_CONFIG, state_folder=f".local/state-{n}") for n in range(2)]
    stop = threading.Event()

    def write() -> None:
        n = 0
        while not stop.is_set():
            spare = path.with_name(".shipline.json.new")
            spare.write_text(json.dumps(versions[n % 2]))
            os.replace(spare, path)
            n += 1

    writer = threading.Thread(target=write)
    writer.start()
    results: list[subprocess.CompletedProcess[str]] = []
    lock = threading.Lock()

    def read(cwd: Path) -> None:
        for _ in range(4):
            done = _config(workflow, cwd, "shell")
            with lock:
                results.append(done)

    readers = [threading.Thread(target=read, args=(cwd,))
               for cwd in [workflow.repo, *worktrees] * 3]
    try:
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join()
    finally:
        stop.set()
        writer.join()
    assert len(results) == 36
    for done in results:
        assert done.returncode == 0, done.stderr
        assert ("SHIPLINE_STATE_FOLDER=.local/state-0\n" in done.stdout
                or "SHIPLINE_STATE_FOLDER=.local/state-1\n" in done.stdout), done.stdout


def test_a_session_started_by_the_tick_finds_the_home_file(workflow: Workflow) -> None:
    """A session's environment is rebuilt from scratch: its own `shipline` calls must still
    read the home file, not fall back to the defaults."""
    path = _installation(workflow)
    workflow.add_pull_request(7)
    done = workflow.run("start", "worker", "7")
    assert done.returncode == 0, done.stderr
    lines = (workflow.state / "env" / "ticket-7").read_text().splitlines()
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    worktree = workflow.worktree(7)
    session_env = {key: env[key] for key in ("HOME", "PATH", "SHIPLINE_REPO") if key in env}
    done = subprocess.run(
        ["python3", str(workflow.script.parent / "workflow_config.py"), "shell"],
        cwd=worktree, env=session_env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert f"SHIPLINE_CONFIG={path}\n" in done.stdout
    # The session's notes go under the state folder the home file names.
    assert (worktree / ".local/example-state").is_dir()


@pytest.mark.parametrize("named", ["SHIPLINE_CONFIG", "XDG_CONFIG_HOME"])
def test_a_session_keeps_a_named_config(workflow: Workflow, named: str) -> None:
    """An installation that names its file (SHIPLINE_CONFIG, or its own XDG_CONFIG_HOME; the
    timer and the hooks keep either) starts sessions; their own `shipline` calls must read the
    same file as the tick that started them."""
    home = _installation(workflow)
    if named == "SHIPLINE_CONFIG":
        path = workflow.root / "named" / "shipline.json"
        workflow.env["SHIPLINE_CONFIG"] = str(path)
    else:
        workflow.env["XDG_CONFIG_HOME"] = str(workflow.root / "named")
        path = workflow.root / "named/shipline/example-app-example-app/shipline.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(_CONFIG, state_folder=".local/named-state")))
    workflow.add_pull_request(7)
    done = workflow.run("start", "worker", "7")
    assert done.returncode == 0, done.stderr
    worktree = workflow.worktree(7)
    # The tick's start used the named file.
    assert (worktree / ".local/named-state").is_dir()
    lines = (workflow.state / "env" / "ticket-7").read_text().splitlines()
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    done = subprocess.run(
        ["python3", str(workflow.script.parent / "workflow_config.py"), "shell"],
        cwd=worktree, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert home.exists()
    # The session reads the file its tick read.
    assert f"SHIPLINE_CONFIG={path}\n" in done.stdout, [
        line for line in done.stdout.splitlines() if line.startswith("SHIPLINE_CONFIG=")
        or line.startswith("SHIPLINE_STATE_FOLDER=")]


def test_a_tick_from_the_timer_works_on_the_home_file(workflow: Workflow) -> None:
    """The timer runs the tick in the main checkout. It starts workers only for the label the
    home file names, and keeps its notes in the home file's state folder."""
    _installation(workflow)
    workflow.add_pull_request(7, labels=("example-workflow",))
    workflow.add_pull_request(8, labels=("workflow",))
    done = workflow.shipline("--repo", str(workflow.repo), "tick", cwd=workflow.root)
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert not workflow.is_running("ticket-8")
    assert (workflow.worktree(7) / ".local/example-state").is_dir()
    assert not (workflow.worktree(7) / ".local/workflow").exists()


@pytest.mark.parametrize(
    "word",
    ["@install/../escape.py", "@install//etc/escape.py", "@install/a/../../escape.py",
     "../escape.py"],
)
def test_an_installation_program_cannot_leave_its_folder(workflow: Workflow, word: str) -> None:
    path = _installation(workflow)
    path.write_text(json.dumps({"commands": {"hunt_app": f"python3 {word}"}}))
    done = _config(workflow, workflow.repo, "shell")
    assert done.returncode == 1, done.stdout
    assert done.stderr.startswith(str(path)), done.stderr


def test_the_shell_form_cannot_be_made_to_run_a_value(workflow: Workflow) -> None:
    """eval must never run a value: neither one the orchestrator wrote nor the file's path."""
    _installation(workflow)
    value = "x$(touch ran)`touch ran`'; touch ran; '"
    named = workflow.root / "it's $(touch ran) `touch ran`" / "shipline.json"
    named.parent.mkdir()
    named.write_text(json.dumps({"state_folder": value, "install_folder": value}))
    for form, shell in (("shell", "bash"), ("sh", "sh")):
        done = subprocess.run(
            [shell, "-c", f'eval "$(python3 "$1" {form})" && printf "%s|%s|%s" '
             '"$SHIPLINE_STATE_FOLDER" "$SHIPLINE_CONFIG" "$SHIPLINE_INSTALL_FOLDER"',
             "x", str(workflow.script.parent / "workflow_config.py")],
            cwd=workflow.repo, env={**workflow.env, "SHIPLINE_CONFIG": str(named)},
            capture_output=True, text=True, timeout=30,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout == f"{value}|{named}|{named.parent / value}", done.stdout
        assert not (workflow.repo / "ran").exists(), form
