"""Tests that try to break `tools/parity` (#10) the way an installation uses it: its own layout,
the run before the switch, the run after it, a large run shared in short, and the failures a run
meets (a hung git, two runs at once, a missing setting)."""

import importlib.machinery
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools/parity"
_CONFIG = _ROOT / "scripts/workflow/workflow_config.py"

# The installation keeps its tests below its server folder and its known list beside them.
_SERVER_CONFIG = {
    "parity": {
        "workflow_tests": "server/tests/workflow",
        "runner_tests": "server/tests/runner",
        "known": "server/tests/parity-known.txt",
    }
}
_TICK = '#!/bin/bash\nset -euo pipefail\necho "tick for $SHIPLINE_REPOSITORY"\nexit 0\n'
_THEIR_TICK = _TICK.replace("$SHIPLINE_REPOSITORY", "example-owner/example-app")
_RULES = "# Rules\n\nEvery change is a pull request.\n"


def _checkout(folder: Path, files: dict[str, str], commit: bool = False) -> Path:
    folder.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    for name, text in files.items():
        path = folder / name.removesuffix("*")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755 if name.endswith("*") else 0o644)
    subprocess.run(["git", "-C", str(folder), "add", "-A"], check=True)
    if commit:
        subprocess.run(
            [
                "git",
                "-C",
                str(folder),
                "-c",
                "user.name=Example",
                "-c",
                "user.email=owner@example.com",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
    return folder


def _shipline(tmp_path: Path, files: dict[str, str]) -> Path:
    root = _checkout(tmp_path / "shipline", files)
    (root / "tools").mkdir(exist_ok=True)
    shutil.copy(_TOOL, root / "tools/parity")
    (root / "scripts/workflow").mkdir(parents=True, exist_ok=True)
    shutil.copy(_CONFIG, root / "scripts/workflow/workflow_config.py")
    return root


def _parity(shipline: Path, checkout: Path | str, *options: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(shipline / "tools/parity"), *options, str(checkout)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _ours() -> dict[str, str]:
    return {
        "scripts/workflow/RULES.md": _RULES,
        "scripts/workflow/tick*": _TICK,
        "scripts/workflow/merge-when-current*": "#!/bin/bash\necho merge\n",
        "tests/workflow/test_tick.py": 'REPO = "$SHIPLINE_REPOSITORY"\ndef test(): pass\n',
        "tests/runner/test_pool.py": "def test(): pass\n",
        "runner/pool.sh": "prefix=$1\n",
        "tests/workflow_harness.py": "def run(): pass\n",
    }


def _theirs() -> dict[str, str]:
    return {
        "AGENTS.md": _RULES,
        "scripts/workflow/tick*": _THEIR_TICK,
        "scripts/workflow/merge-when-green*": "#!/bin/bash\necho merge\n",
        # The installation's own helper, which stays in it.
        "scripts/workflow/hunt-app*": "#!/bin/bash\necho hunt\n",
        "server/tests/workflow/test_tick.py": 'REPO = "example-owner/example-app"\n'
        "def test(): pass\n",
        "server/tests/workflow/test_hunt_app.py": "def test(): pass\n",
        "server/tests/runner/test_pool.py": "def test(): pass\n",
        ".github/runner/pool.sh": "prefix=$1\n",
        "server/tests/workflow_harness.py": "def run(): pass\n",
        "server/app.py": "print('app')\n",
    }


_KNOWN = (
    "# Shipline reads the repository from its config, where the copy names it.\n"
    'rewrite scripts/workflow/* |$SHIPLINE_REPOSITORY|example-owner/example-app|\n'
    "# The tests name the repository the same way.\n"
    'rewrite tests/workflow/* |$SHIPLINE_REPOSITORY|example-owner/example-app|\n'
    "# The copy keeps the older name of the merge helper.\n"
    "rename scripts/workflow/merge-when-current scripts/workflow/merge-when-green\n"
    "# The installation's own hunt stays in it.\n"
    "rename - scripts/workflow/hunt-app\n"
    "# Its test stays with it.\n"
    "rename - server/tests/workflow/test_hunt_app.py\n"
    "# The test harness is compared too, from where the installation keeps it.\n"
    "rename tests/workflow_harness.py server/tests/workflow_harness.py\n"
)


def _installation(tmp_path: Path, files: dict[str, str], known: str, **kw: bool) -> Path:
    files = {
        **files,
        "shipline.json": json.dumps(_SERVER_CONFIG),
        "server/tests/parity-known.txt": known,
    }
    return _checkout(tmp_path / "app", files, **kw)


# --- The run before the switch -------------------------------------------------------------


def test_the_installation_s_layout_with_its_known_list_prints_nothing(tmp_path: Path) -> None:
    done = _parity(_shipline(tmp_path, _ours()), _installation(tmp_path, _theirs(), _KNOWN))

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_change_the_installation_gained_is_printed_in_each_of_its_places(
    tmp_path: Path,
) -> None:
    theirs = _theirs()
    theirs["scripts/workflow/tick*"] += "echo gained\n"
    theirs["server/tests/workflow/test_tick.py"] += "def test_gained(): pass\n"
    theirs["server/tests/runner/test_pool.py"] += "def test_gained(): pass\n"
    theirs[".github/runner/pool.sh"] += "echo gained\n"
    theirs["AGENTS.md"] += "A gained rule.\n"
    theirs["scripts/workflow/merge-when-green*"] += "echo gained\n"
    theirs["server/tests/workflow_harness.py"] += "def gained(): pass\n"
    theirs["server/tests/workflow/test_new.py"] = "def test(): pass\n"

    done = _parity(
        _shipline(tmp_path, _ours()), _installation(tmp_path, theirs, _KNOWN), "--summary"
    )

    assert (done.returncode, done.stderr) == (1, "")
    assert done.stdout.splitlines() == [
        "1+ 0- runner/pool.sh .github/runner/pool.sh",
        "1+ 0- scripts/workflow/RULES.md AGENTS.md",
        "1+ 0- scripts/workflow/merge-when-current scripts/workflow/merge-when-green",
        "1+ 0- scripts/workflow/tick scripts/workflow/tick",
        "1+ 0- - server/tests/workflow/test_new.py",
        "1+ 0- tests/runner/test_pool.py server/tests/runner/test_pool.py",
        "1+ 0- tests/workflow/test_tick.py server/tests/workflow/test_tick.py",
        "1+ 0- tests/workflow_harness.py server/tests/workflow_harness.py",
    ]


def test_a_shipline_json_the_orchestrator_left_untracked_is_read(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "app", _theirs())
    (checkout / "shipline.json").write_text(json.dumps(_SERVER_CONFIG))
    (checkout / "server/tests/parity-known.txt").write_text(_KNOWN)

    done = _parity(_shipline(tmp_path, _ours()), checkout)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_known_list_kept_in_the_compared_tests_can_name_itself(tmp_path: Path) -> None:
    config = json.loads(json.dumps(_SERVER_CONFIG))
    config["parity"]["known"] = "server/tests/workflow/parity-known.txt"
    known = _KNOWN + "# The list itself.\nrename - server/tests/workflow/parity-known.txt\n"
    files = {
        **_theirs(),
        "shipline.json": json.dumps(config),
        "server/tests/workflow/parity-known.txt": known,
    }

    done = _parity(_shipline(tmp_path, _ours()), _checkout(tmp_path / "app", files))

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_known_list_saved_with_windows_line_ends_is_read(tmp_path: Path) -> None:
    known = _KNOWN.replace("\n", "\r\n")

    done = _parity(_shipline(tmp_path, _ours()), _installation(tmp_path, _theirs(), known))

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_without_its_config_the_installation_s_tests_are_not_silently_equal(
    tmp_path: Path,
) -> None:
    # The setting is missing: Shipline's tests show as missing there, never as nothing.
    done = _parity(_shipline(tmp_path, _ours()), _checkout(tmp_path / "app", _theirs()))

    assert done.returncode == 1
    assert "--- shipline/tests/workflow/test_tick.py" in done.stdout
    assert "--- shipline/tests/runner/test_pool.py" in done.stdout


def test_a_checkout_named_by_a_relative_path_or_a_trailing_slash_is_read(
    tmp_path: Path,
) -> None:
    shipline = _shipline(tmp_path, _ours())
    _installation(tmp_path, _theirs(), _KNOWN)
    for name in ("app", "app/", "./app//"):
        done = subprocess.run(
            [sys.executable, "-B", str(shipline / "tools/parity"), name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert (done.returncode, done.stdout, done.stderr) == (0, "", ""), name


def test_a_worktree_of_the_installation_is_read(tmp_path: Path) -> None:
    checkout = _installation(tmp_path, _theirs(), _KNOWN, commit=True)
    worktree = tmp_path / "app-worktree"
    subprocess.run(
        ["git", "-C", str(checkout), "worktree", "add", "-q", str(worktree)], check=True
    )

    done = _parity(_shipline(tmp_path, _ours()), worktree)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


# --- The run after the switch ---------------------------------------------------------------

# After the switch the installation runs `shipline <command>`: its own helpers moved to its
# tools folder, and nothing generic is left in Shipline's places.
_AFTER = (
    "# After the switch the scripts come from Shipline.\n"
    "rename scripts/workflow/ -\n"
    "# So do its tests.\n"
    "rename tests/workflow/ -\n"
    "# And the runner tests.\n"
    "rename tests/runner/ -\n"
    "# And the runner pool.\n"
    "rename runner/ -\n"
    "# Shipline's rules are read from Shipline.\n"
    "rename scripts/workflow/RULES.md -\n"
    "# The installation's own rules stay.\n"
    "rename - AGENTS.md\n"
)


def _after_switch() -> dict[str, str]:
    return {
        "AGENTS.md": "# example-app\n\nRead Shipline's rules.\n",
        "tools/hunt-app*": "#!/bin/bash\necho hunt\n",
        "server/app.py": "print('app')\n",
    }


def test_after_the_switch_nothing_left_behind_prints_nothing(tmp_path: Path) -> None:
    done = _parity(_shipline(tmp_path, _ours()), _installation(tmp_path, _after_switch(), _AFTER))

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_after_the_switch_a_generic_file_left_behind_is_printed(tmp_path: Path) -> None:
    theirs = {
        **_after_switch(),
        "scripts/workflow/tick*": _THEIR_TICK,
        "server/tests/workflow/test_tick.py": "def test(): pass\n",
        ".github/runner/pool.sh": "prefix=$1\n",
    }

    done = _parity(
        _shipline(tmp_path, _ours()), _installation(tmp_path, theirs, _AFTER), "--summary"
    )

    assert (done.returncode, done.stderr) == (1, "")
    assert done.stdout.splitlines() == [
        "1+ 0- - .github/runner/pool.sh",
        "4+ 0- - scripts/workflow/tick",
        "1+ 0- - server/tests/workflow/test_tick.py",
    ]


def test_after_the_switch_the_installation_s_config_still_loads(tmp_path: Path) -> None:
    sys.path.insert(0, str(_ROOT / "scripts/workflow"))
    try:
        import workflow_config
    finally:
        sys.path.pop(0)
    checkout = _installation(tmp_path, _after_switch(), _AFTER)

    parity = workflow_config.parity_of(json.loads((checkout / "shipline.json").read_text()))

    assert vars(parity) == _SERVER_CONFIG["parity"]


# --- A large run, shared in short -----------------------------------------------------------


def _many(count: int, lines: int, changed: bool) -> dict[str, str]:
    files = {}
    for number in range(count):
        tree = ("scripts/workflow", "tests/workflow")[number % 2]
        body = [f"line {at} of file {number}" for at in range(lines)]
        if changed:
            body = [text + " changed" if at % 2 else text for at, text in enumerate(body)]
        files[f"{tree}/file_{number:03}.py"] = "\n".join(body) + "\n"
    return files


def test_a_run_of_192_files_and_tens_of_thousands_of_lines_is_quick_and_short_in_summary(
    tmp_path: Path,
) -> None:
    ours = _many(192, 120, changed=False)
    theirs = {
        name.replace("tests/workflow/", "server/tests/workflow/"): text
        for name, text in _many(192, 120, changed=True).items()
    }
    shipline = _shipline(tmp_path, ours)
    checkout = _installation(tmp_path, theirs, "")

    started = time.monotonic()
    full = _parity(shipline, checkout)
    short = _parity(shipline, checkout, "--summary")
    took = time.monotonic() - started

    assert (full.returncode, full.stderr) == (1, "")
    assert len(full.stdout.splitlines()) > 30000
    assert (short.returncode, short.stderr) == (1, "")
    assert len(short.stdout.splitlines()) == 192
    assert all(line.startswith("60+ 60- ") for line in short.stdout.splitlines())
    assert took < 60


def test_a_large_fixture_with_repeated_lines_is_compared_in_time(tmp_path: Path) -> None:
    rows = [json.dumps({"id": at, "state": "done"}, indent=2) for at in range(4000)]
    ours = {"tests/workflow/fixtures/runs.json": "[\n" + ",\n".join(rows) + "\n]\n"}
    theirs_rows = [row.replace('"done"', '"failed"') if at % 7 == 0 else row for at, row in
                   enumerate(rows)]
    theirs = {
        "server/tests/workflow/fixtures/runs.json": "[\n" + ",\n".join(theirs_rows) + "\n]\n"
    }

    started = time.monotonic()
    done = _parity(_shipline(tmp_path, ours), _installation(tmp_path, theirs, ""), "--summary")

    assert (done.returncode, done.stderr) == (1, "")
    assert done.stdout.startswith("572+ 572- tests/workflow/fixtures/runs.json")
    assert time.monotonic() - started < 30


# --- Failures a run meets ---------------------------------------------------------------------


def test_two_runs_at_once_print_the_same(tmp_path: Path) -> None:
    theirs = _theirs()
    theirs["scripts/workflow/tick*"] += "echo gained\n"
    shipline = _shipline(tmp_path, _ours())
    checkout = _installation(tmp_path, theirs, _KNOWN)

    with ThreadPoolExecutor(6) as pool:
        runs = list(pool.map(lambda _: _parity(shipline, checkout), range(6)))

    assert {(run.returncode, run.stdout, run.stderr) for run in runs} == {
        (runs[0].returncode, runs[0].stdout, "")
    }
    assert runs[0].returncode == 1


def test_a_run_while_the_installation_s_index_is_locked_still_reads_it(tmp_path: Path) -> None:
    checkout = _installation(tmp_path, _theirs(), _KNOWN)
    # Another session is in the middle of `git add`.
    (checkout / ".git/index.lock").write_text("")

    done = _parity(_shipline(tmp_path, _ours()), checkout)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("parity_tool", str(_TOOL))
    spec = importlib.util.spec_from_loader("parity_tool", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_a_git_that_hangs_is_given_up_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool()
    monkeypatch.setattr(tool, "GIT_SECONDS", 1)
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    (bin_folder / "git").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_folder / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_folder}:/usr/bin:/bin")
    (tmp_path / "app").mkdir()
    out = io.TextIOWrapper(io.BytesIO())
    err = io.StringIO()

    started = time.monotonic()
    with redirect_stdout(out), redirect_stderr(err):
        code = tool.main([str(tmp_path / "app")])

    assert code == 2
    out.flush()
    assert out.buffer.getvalue() == b""
    assert time.monotonic() - started < 10


def test_a_known_list_the_config_names_but_that_is_missing_stops_the_run(
    tmp_path: Path,
) -> None:
    checkout = _installation(tmp_path, _theirs(), _KNOWN)
    (checkout / "server/tests/parity-known.txt").unlink()

    done = _parity(_shipline(tmp_path, _ours()), checkout)

    assert (done.returncode, done.stdout) == (2, "")
    assert "server/tests/parity-known.txt" in done.stderr


def test_an_entry_that_no_longer_matches_after_the_helper_moved_is_printed(
    tmp_path: Path,
) -> None:
    theirs = _theirs()
    del theirs["scripts/workflow/hunt-app*"]
    theirs["tools/hunt-app*"] = "#!/bin/bash\necho hunt\n"

    done = _parity(_shipline(tmp_path, _ours()), _installation(tmp_path, theirs, _KNOWN))

    assert (done.returncode, done.stderr) == (1, "")
    assert done.stdout == (
        "unused: server/tests/parity-known.txt:8: rename - scripts/workflow/hunt-app\n"
    )


# --- Low, not reached in use ------------------------------------------------------------------


def test_low_a_git_that_hangs_is_named_as_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Low: the run stops with exit 2 as it should, but says the folder is no checkout."""
    tool = _load_tool()
    monkeypatch.setattr(tool, "GIT_SECONDS", 1)
    bin_folder = tmp_path / "bin"
    bin_folder.mkdir()
    (bin_folder / "git").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_folder / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_folder}:/usr/bin:/bin")
    (tmp_path / "app").mkdir()
    err = io.StringIO()

    with redirect_stdout(io.TextIOWrapper(io.BytesIO())), redirect_stderr(err):
        tool.main([str(tmp_path / "app")])

    assert "took more than 1 seconds" in err.getvalue()


def test_low_a_command_in_the_checkout_s_own_git_config_is_not_run(tmp_path: Path) -> None:
    """Low: the checkout is the installation's own; its local git config is not repository
    content, so no pull request can set it."""
    checkout = _installation(tmp_path, _theirs(), _KNOWN)
    marker = tmp_path / "ran"
    hook = tmp_path / "hook"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\necho\n')
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(checkout), "config", "core.fsmonitor", str(hook)], check=True)

    _parity(_shipline(tmp_path, _ours()), checkout)

    assert not marker.exists()
