"""`tools/parity` prints every difference between Shipline and an installation's own copy of the
scripts, except those `tools/parity-known.txt` lists: a path rename or a text rewrite, each with
its reason."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools/parity"
_KNOWN = _ROOT / "tools/parity-known.txt"
_CONFIG = _ROOT / "scripts/workflow/workflow_config.py"

_TICK = '#!/bin/bash\nset -euo pipefail\necho "tick for $SHIPLINE_REPOSITORY"\nexit 0\n'


def _checkout(folder: Path, files: dict[str, str]) -> Path:
    """A git checkout holding `files`; a name ending in `*` is executable, without the `*`."""
    folder.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    for name, text in files.items():
        path = folder / name.removesuffix("*")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755 if name.endswith("*") else 0o644)
    subprocess.run(["git", "-C", str(folder), "add", "-A"], check=True)
    return folder


def _shipline(tmp_path: Path, files: dict[str, str], known: str = "") -> Path:
    """A Shipline checkout with the real tool, `files` and `known` as its known differences.
    The config reader the tool imports is there untracked, so it is not compared."""
    root = _checkout(tmp_path / "shipline", {**files, "tools/parity-known.txt": known})
    shutil.copy(_TOOL, root / "tools/parity")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    (root / "scripts/workflow").mkdir(parents=True, exist_ok=True)
    shutil.copy(_CONFIG, root / "scripts/workflow/workflow_config.py")
    return root


def _parity(shipline: Path, checkout: Path) -> subprocess.CompletedProcess[str]:
    # Run by the interpreter: CI's temporary folders may not run programs.
    return subprocess.run(
        [sys.executable, "-B", str(shipline / "tools/parity"), str(checkout)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _both(
    tmp_path: Path, ours: dict[str, str], theirs: dict[str, str], known: str = ""
) -> subprocess.CompletedProcess[str]:
    return _parity(_shipline(tmp_path, ours, known), _checkout(tmp_path / "app", theirs))


def test_the_same_files_print_nothing(tmp_path: Path) -> None:
    files = {"scripts/workflow/tick*": _TICK, "tests/workflow/test_tick.py": "def test(): pass\n"}

    done = _both(tmp_path, files, files)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_each_tree_is_compared_with_its_place_in_the_checkout(tmp_path: Path) -> None:
    ours = {
        "scripts/workflow/tick*": _TICK,
        "scripts/workflow/RULES.md": "# Rules\n",
        "tests/workflow/test_tick.py": "def test(): pass\n",
        "tests/runner/test_names.py": "def test(): pass\n",
        "runner/names.sh": "prefix=$1\n",
    }
    theirs = {
        "scripts/workflow/tick*": _TICK,
        "AGENTS.md": "# Rules\n",
        "tests/workflow/test_tick.py": "def test(): pass\n",
        "tests/runner/test_names.py": "def test(): pass\n",
        ".github/runner/names.sh": "prefix=$1\n",
    }

    done = _both(tmp_path, ours, theirs)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_the_checkout_s_config_says_where_its_tests_are(tmp_path: Path) -> None:
    config = '{"parity": {"workflow_tests": "app/tests/flow", "runner_tests": "ops/runner-tests"}}'
    ours = {
        "tests/workflow/test_tick.py": "def test(): pass\n",
        "tests/runner/test_names.py": "def test(): pass\n",
    }
    theirs = {
        "shipline.json": config,
        "app/tests/flow/test_tick.py": "def test(): pass\n",
        "ops/runner-tests/test_names.py": "def test(): pass\n",
        # Where Shipline keeps them is no longer compared.
        "tests/workflow/test_other.py": "def test(): pass\n",
    }

    done = _both(tmp_path, ours, theirs)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_config_that_names_one_test_folder_keeps_the_other_s_default(tmp_path: Path) -> None:
    theirs = {
        "shipline.json": '{"parity": {"workflow_tests": "app/tests/flow"}}',
        "app/tests/flow/test_tick.py": "def test(): pass\n",
        "tests/runner/test_names.py": "def test(): pass\n",
    }
    ours = {
        "tests/workflow/test_tick.py": "def test(): pass\n",
        "tests/runner/test_names.py": "def test(): pass\n",
    }

    done = _both(tmp_path, ours, theirs)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


@pytest.mark.parametrize(
    "config,refusal",
    [
        ("{", "shipline.json is not JSON"),
        ('{"parity": []}', "shipline.json: parity"),
        ('{"parity": {"workflow_test": "x"}}', "shipline.json: unknown key parity.workflow_test"),
        ('{"parity": {"workflow_tests": 3}}', "shipline.json: parity.workflow_tests must be a name"),
        (
            '{"parity": {"runner_tests": "../elsewhere"}}',
            "shipline.json: parity.runner_tests must be a path inside the checkout",
        ),
        (
            '{"parity": {"runner_tests": "/srv/tests"}}',
            "shipline.json: parity.runner_tests must be a path inside the checkout",
        ),
    ],
)
def test_a_checkout_config_it_cannot_read_is_refused(
    tmp_path: Path, config: str, refusal: str
) -> None:
    done = _both(tmp_path, {}, {"shipline.json": config})

    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith(refusal)


def test_files_outside_the_trees_are_not_compared(tmp_path: Path) -> None:
    done = _both(
        tmp_path,
        {"README.md": "Shipline\n", "scripts/other/x": "1\n"},
        {"README.md": "example-app\n", "scripts/other/x": "2\n", "src/app.py": "pass\n"},
    )

    assert (done.returncode, done.stdout) == (0, "")


@pytest.mark.parametrize(
    "theirs,shown",
    [
        # A changed line.
        (_TICK.replace("exit 0", "exit 3"), ["-exit 0", "+exit 3"]),
        # An added line.
        (_TICK.replace("exit 0\n", "echo done\nexit 0\n"), ["+echo done"]),
        # A removed line.
        (_TICK.replace("set -euo pipefail\n", ""), ["-set -euo pipefail"]),
    ],
)
def test_a_changed_added_or_removed_line_is_printed(
    tmp_path: Path, theirs: str, shown: list[str]
) -> None:
    done = _both(tmp_path, {"scripts/workflow/tick*": _TICK}, {"scripts/workflow/tick*": theirs})

    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert lines[:2] == ["--- shipline/scripts/workflow/tick", "+++ checkout/scripts/workflow/tick"]
    for line in shown:
        assert line in lines
    # Only the differing lines are marked.
    assert [line for line in lines[2:] if line[:1] in "+-"] == shown


def test_a_file_only_on_one_side_is_printed_whole(tmp_path: Path) -> None:
    done = _both(
        tmp_path,
        {"scripts/workflow/tick*": _TICK, "scripts/workflow/gone": "ours\n"},
        {"scripts/workflow/tick*": _TICK, "tests/workflow/test_new.py": "def test(): pass\n"},
    )

    assert done.returncode == 1
    assert done.stdout == (
        "--- shipline/scripts/workflow/gone\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-ours\n"
        "--- /dev/null\n"
        "+++ checkout/tests/workflow/test_new.py\n"
        "@@ -0,0 +1 @@\n"
        "+def test(): pass\n"
    )


def test_a_file_that_lost_its_executable_bit_is_printed(tmp_path: Path) -> None:
    done = _both(tmp_path, {"scripts/workflow/tick*": _TICK}, {"scripts/workflow/tick": _TICK})

    assert done.returncode == 1
    assert done.stdout == (
        "mode: shipline/scripts/workflow/tick is executable, "
        "checkout/scripts/workflow/tick is not executable\n"
    )


def test_an_untracked_file_is_not_compared(tmp_path: Path) -> None:
    shipline = _shipline(tmp_path, {"scripts/workflow/tick*": _TICK})
    checkout = _checkout(tmp_path / "app", {"scripts/workflow/tick*": _TICK})
    (checkout / "scripts/workflow/__pycache__").mkdir()
    (checkout / "scripts/workflow/__pycache__/x.pyc").write_bytes(b"\0")
    (shipline / "tests/workflow").mkdir(parents=True)
    (shipline / "tests/workflow/scratch.txt").write_text("notes\n")

    done = _parity(shipline, checkout)

    assert (done.returncode, done.stdout) == (0, "")


def test_a_known_rename_is_not_printed(tmp_path: Path) -> None:
    known = (
        "# The merge line is called the merge train in the checkout.\n"
        "rename scripts/workflow/merge-line scripts/workflow/merge-train\n"
    )
    done = _both(
        tmp_path,
        {"scripts/workflow/merge-line*": _TICK},
        {"scripts/workflow/merge-train*": _TICK},
        known,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_known_folder_rename_moves_every_file_below_it(tmp_path: Path) -> None:
    known = "# Its hunts keep their own folder.\nrename scripts/workflow/hunts/ scripts/hunts/\n"
    done = _both(
        tmp_path,
        {"scripts/workflow/hunts/a": "1\n", "scripts/workflow/hunts/b/c": "2\n"},
        {"scripts/hunts/a": "1\n", "scripts/hunts/b/c": "2\n"},
        known,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_renamed_file_still_shows_its_changed_lines(tmp_path: Path) -> None:
    known = "# The merge line.\nrename scripts/workflow/merge-line scripts/workflow/merge-train\n"
    done = _both(
        tmp_path,
        {"scripts/workflow/merge-line*": _TICK},
        {"scripts/workflow/merge-train*": _TICK.replace("exit 0", "exit 4")},
        known,
    )

    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert lines[:2] == [
        "--- shipline/scripts/workflow/merge-line",
        "+++ checkout/scripts/workflow/merge-train",
    ]
    assert [line for line in lines[2:] if line[:1] in "+-"] == ["-exit 0", "+exit 4"]


def test_a_file_renamed_away_leaves_its_old_name_on_the_other_side_unpaired(
    tmp_path: Path,
) -> None:
    known = "# The merge line.\nrename scripts/workflow/merge-line scripts/workflow/merge-train\n"
    done = _both(
        tmp_path,
        {"scripts/workflow/merge-line*": _TICK},
        {"scripts/workflow/merge-train*": _TICK, "scripts/workflow/merge-line*": _TICK},
        known,
    )

    assert done.returncode == 1
    assert done.stdout.splitlines()[:2] == ["--- /dev/null", "+++ checkout/scripts/workflow/merge-line"]


def test_a_known_file_kept_on_one_side_only_is_not_printed(tmp_path: Path) -> None:
    known = (
        "# The repository's own hunt stays in it.\n"
        "rename - scripts/workflow/hunt-app\n"
        "# Shipline's own notes on its scripts.\n"
        "rename scripts/workflow/README.md -\n"
    )
    done = _both(
        tmp_path,
        {"scripts/workflow/README.md": "notes\n"},
        {"scripts/workflow/hunt-app*": _TICK},
        known,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_known_rewrite_is_not_printed(tmp_path: Path) -> None:
    known = (
        "# Shipline reads the repository from its config; the checkout names it.\n"
        'rewrite scripts/workflow/tick |"tick for $SHIPLINE_REPOSITORY"|"tick for example/app"|\n'
    )
    done = _both(
        tmp_path,
        {"scripts/workflow/tick*": _TICK},
        {"scripts/workflow/tick*": _TICK.replace("$SHIPLINE_REPOSITORY", "example/app")},
        known,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_rewrite_may_span_lines_and_hold_a_backslash(tmp_path: Path) -> None:
    ours = "a\nprintf 'x\\n'\nb\n"
    theirs = "a\nprintf 'y\\n'\nextra\nb\n"
    known = "# Its own extra step.\nrewrite scripts/workflow/x ,printf 'x\\\\n',printf 'y\\\\n'\\nextra,\n"

    done = _both(tmp_path, {"scripts/workflow/x": ours}, {"scripts/workflow/x": theirs}, known)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_rewrite_applies_only_to_the_files_it_names(tmp_path: Path) -> None:
    known = "# The repository.\nrewrite scripts/workflow/t* |$SHIPLINE_REPOSITORY|example/app|\n"
    other = _TICK.replace("tick for", "wake for")
    done = _both(
        tmp_path,
        {"scripts/workflow/tick*": _TICK, "scripts/workflow/wake*": other},
        {
            "scripts/workflow/tick*": _TICK.replace("$SHIPLINE_REPOSITORY", "example/app"),
            "scripts/workflow/wake*": other.replace("$SHIPLINE_REPOSITORY", "example/app"),
        },
        known,
    )

    assert done.returncode == 1
    assert done.stdout.splitlines()[:2] == [
        "--- shipline/scripts/workflow/wake",
        "+++ checkout/scripts/workflow/wake",
    ]
    assert "tick" not in done.stdout


def test_a_known_difference_found_nowhere_is_printed(tmp_path: Path) -> None:
    known = (
        "# The repository.\n"
        "rewrite scripts/workflow/tick |$SHIPLINE_NOWHERE|example/app|\n"
        "\n"
        "# An old name.\n"
        "rename scripts/workflow/old scripts/workflow/older\n"
    )
    done = _both(tmp_path, {"scripts/workflow/tick*": _TICK}, {"scripts/workflow/tick*": _TICK}, known)

    assert done.returncode == 1
    assert done.stdout == (
        "unused: tools/parity-known.txt:2: rewrite scripts/workflow/tick "
        "|$SHIPLINE_NOWHERE|example/app|\n"
        "unused: tools/parity-known.txt:5: rename scripts/workflow/old scripts/workflow/older\n"
    )


@pytest.mark.parametrize(
    "known,refusal",
    [
        ("rename scripts/workflow/a scripts/workflow/b\n", "line 1: no reason above it"),
        ("# Why.\nmove scripts/workflow/a scripts/workflow/b\n", "line 2: not a rename or a rewrite"),
        ("# Why.\nrename scripts/workflow/a\n", "line 2: a rename names two paths"),
        ("# Why.\nrename - -\n", "line 2: a rename names two paths"),
        ("# Why.\nrewrite scripts/workflow/a |x|y\n", "line 2: a rewrite is PATH |FROM|TO|"),
        ("# Why.\nrewrite scripts/workflow/a |x|x|\n", "line 2: a rewrite changes its text"),
        ("# Why.\nrewrite scripts/workflow/a ||y|\n", "line 2: a rewrite changes its text"),
        ("# Why.\nrename /etc/a scripts/workflow/b\n", "line 2: /etc/a is not a path inside"),
        ("# Why.\nrename scripts/workflow/a ../b\n", "line 2: ../b is not a path inside"),
    ],
)
def test_a_known_list_it_cannot_read_is_refused(tmp_path: Path, known: str, refusal: str) -> None:
    done = _both(tmp_path, {"scripts/workflow/a": "1\n"}, {"scripts/workflow/a": "2\n"}, known)

    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith(f"tools/parity-known.txt: {refusal}")


def test_a_folder_that_is_no_checkout_is_refused(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()

    done = _parity(_shipline(tmp_path, {}), tmp_path / "plain")

    assert done.returncode == 2
    assert done.stdout == ""
    assert "is not a git checkout" in done.stderr


def test_it_is_run_with_one_checkout(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-B", str(_TOOL)], capture_output=True, text=True, timeout=60, check=False
    )

    assert (done.returncode, done.stderr) == (2, "usage: tools/parity CHECKOUT\n")


def test_shipline_s_own_known_list_is_read_and_its_tool_runs() -> None:
    assert _KNOWN.is_file()
    assert _TOOL.stat().st_mode & 0o111
    done = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "-s", "tools/parity"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.startswith("100755 ")
