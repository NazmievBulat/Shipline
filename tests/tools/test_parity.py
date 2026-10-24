"""`tools/parity` prints every difference between Shipline and an installation's own copy of the
scripts, except those the installation's known list names: a path rename or a text rewrite, each
with its reason."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools/parity"
_KNOWN = _ROOT / "tools/parity-known.txt"
_CONFIG = _ROOT / "scripts/workflow/workflow_config.py"
# Where the installations of these tests keep their known list.
_AT = "tools/shipline/parity-known.txt"

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


def _shipline(tmp_path: Path, files: dict[str, str]) -> Path:
    """A Shipline checkout with the real tool and `files`. The config reader the tool imports is
    there untracked, so it is not compared."""
    root = _checkout(tmp_path / "shipline", files)
    (root / "tools").mkdir(exist_ok=True)
    shutil.copy(_TOOL, root / "tools/parity")
    (root / "scripts/workflow").mkdir(parents=True, exist_ok=True)
    shutil.copy(_CONFIG, root / "scripts/workflow/workflow_config.py")
    return root


def _parity(
    shipline: Path, checkout: Path, *options: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # Run by the interpreter: CI's temporary folders may not run programs.
    return subprocess.run(
        [sys.executable, "-B", str(shipline / "tools/parity"), *options, str(checkout)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _both(
    tmp_path: Path,
    ours: dict[str, str],
    theirs: dict[str, str],
    known: str | None = None,
    *options: str,
) -> subprocess.CompletedProcess[str]:
    """Compares `ours` with `theirs`, an installation that names `known` as its known list."""
    if known is not None:
        theirs = {**theirs, "shipline.json": json.dumps({"parity": {"known": _AT}}), _AT: known}
    return _parity(_shipline(tmp_path, ours), _checkout(tmp_path / "app", theirs), *options)


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
        (
            '{"parity": {"workflow_tests": 3}}',
            "shipline.json: parity.workflow_tests must be a name",
        ),
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
    assert done.stderr.startswith(str(tmp_path / "app") + "/" + refusal)


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
        "mode: shipline/scripts/workflow/tick is an executable file, "
        "checkout/scripts/workflow/tick is a file\n"
    )


def test_an_empty_file_on_one_side_only_is_printed(tmp_path: Path) -> None:
    done = _both(
        tmp_path,
        {"tests/workflow/__init__.py": ""},
        {"scripts/workflow/hook*": ""},
        None,
        "--summary",
    )

    assert done.returncode == 1
    assert done.stdout == "0+ 0- - scripts/workflow/hook\n0+ 0- tests/workflow/__init__.py -\n"


def test_a_binary_file_that_differs_is_printed(tmp_path: Path) -> None:
    shipline = _shipline(tmp_path, {})
    checkout = _checkout(tmp_path / "app", {})
    for root, data in ((shipline, b"\xff\x00a"), (checkout, b"\xff\x00b")):
        (root / "scripts/workflow").mkdir(parents=True, exist_ok=True)
        (root / "scripts/workflow/icon.png").write_bytes(data)
        subprocess.run(["git", "-C", str(root), "add", "scripts/workflow/icon.png"], check=True)

    done = _parity(shipline, checkout)

    assert (done.returncode, done.stderr) == (1, "")
    assert done.stdout == (
        "binary files differ: shipline/scripts/workflow/icon.png checkout/scripts/workflow/icon.png\n"
    )


def test_a_symlink_differs_from_a_file_holding_its_target(tmp_path: Path) -> None:
    shipline = _shipline(tmp_path, {"scripts/workflow/gh": "../gh-bin/gh"})
    checkout = _checkout(tmp_path / "app", {})
    (checkout / "scripts/workflow").mkdir(parents=True)
    (checkout / "scripts/workflow/gh").symlink_to("../gh-bin/gh")
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)

    done = _parity(shipline, checkout)

    assert done.returncode == 1
    assert done.stdout.splitlines()[-1] == (
        "mode: shipline/scripts/workflow/gh is a file, checkout/scripts/workflow/gh is a symlink"
    )


def test_a_missing_line_break_at_the_end_is_printed(tmp_path: Path) -> None:
    done = _both(tmp_path, {"scripts/workflow/x": "a\n"}, {"scripts/workflow/x": "a"})

    assert done.returncode == 1
    assert done.stdout.splitlines()[-3:] == ["-a", "+a", "\\ No newline at end of file"]


def test_the_index_is_compared_not_the_working_tree(tmp_path: Path) -> None:
    shipline = _shipline(tmp_path, {"scripts/workflow/tick*": _TICK})
    checkout = _checkout(tmp_path / "app", {"scripts/workflow/tick*": _TICK})
    # A change not added yet is no change of the checkout.
    (checkout / "scripts/workflow/tick").write_text("edited\n")

    done = _parity(shipline, checkout)

    assert (done.returncode, done.stdout) == (0, "")


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
    assert done.stdout.splitlines()[:2] == [
        "--- /dev/null",
        "+++ checkout/scripts/workflow/merge-line",
    ]


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


def test_a_known_rewrite_beside_an_unknown_change_still_shows_the_change(tmp_path: Path) -> None:
    known = "# The repository.\nrewrite scripts/workflow/tick |$SHIPLINE_REPOSITORY|example/app|\n"
    theirs = _TICK.replace("$SHIPLINE_REPOSITORY", "example/app").replace("exit 0", "exit 5")

    done = _both(
        tmp_path, {"scripts/workflow/tick*": _TICK}, {"scripts/workflow/tick*": theirs}, known
    )

    assert done.returncode == 1
    assert [line for line in done.stdout.splitlines()[2:] if line[:1] in "+-"] == [
        "-exit 0",
        "+exit 5",
    ]


def test_rewrites_do_not_apply_to_each_other_s_results(tmp_path: Path) -> None:
    known = (
        "# One name.\nrewrite scripts/workflow/x |alpha|beta|\n"
        "# Another name.\nrewrite scripts/workflow/x |beta|gamma|\n"
    )

    done = _both(
        tmp_path, {"scripts/workflow/x": "alpha\n"}, {"scripts/workflow/x": "gamma\n"}, known
    )

    assert done.returncode == 1
    assert "-beta" in done.stdout.splitlines() and "+gamma" in done.stdout.splitlines()


def test_only_a_star_is_a_wildcard_in_a_rewrite_s_path(tmp_path: Path) -> None:
    known = "# The repository.\nrewrite scripts/workflow/[xy] |a|b|\n"

    done = _both(tmp_path, {"scripts/workflow/x": "a\n"}, {"scripts/workflow/x": "b\n"}, known)

    assert done.returncode == 1
    assert "-a" in done.stdout.splitlines()
    assert done.stdout.splitlines()[-1].startswith(f"unused: {tmp_path / 'app' / _AT}:2: ")


def test_a_rename_of_the_scripts_folder_leaves_the_rules_paired_with_agents_md(
    tmp_path: Path,
) -> None:
    known = "# Its scripts live elsewhere.\nrename scripts/workflow/ lib/workflow/\n"

    done = _both(
        tmp_path,
        {"scripts/workflow/tick*": _TICK, "scripts/workflow/RULES.md": "# Rules\n"},
        {"lib/workflow/tick*": _TICK, "AGENTS.md": "# Rules\n"},
        known,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_star_in_a_rewrite_s_path_reaches_below_folders(tmp_path: Path) -> None:
    known = "# The repository.\nrewrite scripts/workflow/* |$SHIPLINE_REPOSITORY|example/app|\n"
    theirs = _TICK.replace("$SHIPLINE_REPOSITORY", "example/app")

    done = _both(
        tmp_path,
        {"scripts/workflow/bin/tick*": _TICK},
        {"scripts/workflow/bin/tick*": theirs},
        known,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_file_rename_inside_a_renamed_folder_wins(tmp_path: Path) -> None:
    known = (
        "# Its hunts keep their own folder.\nrename scripts/workflow/hunts/ scripts/hunts/\n"
        "# Under an older name there.\nrename scripts/workflow/hunts/plan scripts/hunts/plans\n"
    )

    done = _both(
        tmp_path,
        {"scripts/workflow/hunts/a": "1\n", "scripts/workflow/hunts/plan": "2\n"},
        {"scripts/hunts/a": "1\n", "scripts/hunts/plans": "2\n"},
        known,
    )

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_file_kept_in_shipline_only_leaves_the_checkout_s_own_file_printed(
    tmp_path: Path,
) -> None:
    known = "# Shipline's own notes.\nrename scripts/workflow/README.md -\n"

    done = _both(
        tmp_path,
        {"scripts/workflow/README.md": "notes\n"},
        {"scripts/workflow/README.md": "notes\n"},
        known,
        "--summary",
    )

    assert (done.returncode, done.stdout) == (1, "1+ 0- - scripts/workflow/README.md\n")


def test_the_summary_counts_what_is_left_after_the_rewrites(tmp_path: Path) -> None:
    known = "# The repository.\nrewrite scripts/workflow/tick |$SHIPLINE_REPOSITORY|example/app|\n"
    theirs = _TICK.replace("$SHIPLINE_REPOSITORY", "example/app").replace("exit 0", "exit 5")

    done = _both(
        tmp_path,
        {"scripts/workflow/tick*": _TICK},
        {"scripts/workflow/tick*": theirs},
        known,
        "--summary",
    )

    assert (done.returncode, done.stdout) == (
        1,
        "1+ 1- scripts/workflow/tick scripts/workflow/tick\n",
    )


def test_a_known_difference_found_nowhere_is_printed(tmp_path: Path) -> None:
    known = (
        "# The repository.\n"
        "rewrite scripts/workflow/tick |$SHIPLINE_NOWHERE|example/app|\n"
        "\n"
        "# An old name.\n"
        "rename scripts/workflow/old scripts/workflow/older\n"
    )
    done = _both(
        tmp_path, {"scripts/workflow/tick*": _TICK}, {"scripts/workflow/tick*": _TICK}, known
    )

    assert done.returncode == 1
    assert done.stdout == (
        f"unused: {tmp_path / 'app' / _AT}:2: rewrite scripts/workflow/tick |$SHIPLINE_NOWHERE|example/app|\n"
        f"unused: {tmp_path / 'app' / _AT}:5: rename scripts/workflow/old scripts/workflow/older\n"
    )


@pytest.mark.parametrize(
    "known,refusal",
    [
        ("rename scripts/workflow/a scripts/workflow/b\n", "line 1: no reason above it"),
        ("#\nrename scripts/workflow/a scripts/workflow/b\n", "line 2: no reason above it"),
        ("# Why.\n\nrename scripts/workflow/a scripts/workflow/b\n", "line 3: no reason above it"),
        (
            "# Why.\nrename scripts/workflow/a/ scripts/workflow/b\n",
            "line 2: a folder is renamed to",
        ),
        (
            "# Why.\nmove scripts/workflow/a scripts/workflow/b\n",
            "line 2: not a rename or a rewrite",
        ),
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
    assert done.stderr.startswith(f"{tmp_path / 'app' / _AT}: {refusal}")


def test_a_folder_that_is_no_checkout_is_refused(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()

    done = _parity(_shipline(tmp_path, {}), tmp_path / "plain")

    assert done.returncode == 2
    assert done.stdout == ""
    assert "is not a git checkout" in done.stderr


def test_a_git_that_hangs_is_named_as_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = importlib.machinery.SourceFileLoader("parity", str(_TOOL))
    tool = importlib.util.module_from_spec(importlib.util.spec_from_loader("parity", loader))
    loader.exec_module(tool)
    monkeypatch.setattr(tool, "GIT_SECONDS", 1)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/git").write_text("#!/bin/sh\nexec sleep 30\n")
    (tmp_path / "bin/git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    with pytest.raises(tool.Refused, match="git rev-parse took more than 1 seconds"):
        tool.tracked(tmp_path)


def test_a_command_the_checkout_s_git_config_names_is_not_run(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "app", {"scripts/workflow/tick*": _TICK})
    ran = tmp_path / "ran"
    (tmp_path / "monitor").write_text(f'#!/bin/sh\ntouch "{ran}"\necho\n')
    (tmp_path / "monitor").chmod(0o755)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "core.fsmonitor", str(tmp_path / "monitor")],
        check=True,
    )

    done = _parity(_shipline(tmp_path, {"scripts/workflow/tick*": _TICK}), checkout)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert not ran.exists()


def test_it_is_run_with_one_checkout() -> None:
    done = subprocess.run(
        [sys.executable, "-B", str(_TOOL)], capture_output=True, text=True, timeout=60, check=False
    )

    assert (done.returncode, done.stderr) == (2, "usage: tools/parity [--summary] CHECKOUT\n")


def test_a_checkout_in_the_middle_of_a_merge_is_refused(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "app", {"scripts/workflow/x": "base\n"})
    git = [
        "git",
        "-C",
        str(checkout),
        "-c",
        "user.name=Example",
        "-c",
        "user.email=owner@example.com",
    ]
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "other"], check=True)
    (checkout / "scripts/workflow/x").write_text("other\n")
    subprocess.run([*git, "commit", "-qam", "other"], check=True)
    subprocess.run([*git, "checkout", "-q", "-"], check=True)
    (checkout / "scripts/workflow/x").write_text("ours\n")
    subprocess.run([*git, "commit", "-qam", "ours"], check=True)
    subprocess.run([*git, "merge", "-q", "other"], capture_output=True, check=False)

    done = _parity(_shipline(tmp_path, {"scripts/workflow/x": "other\n"}), checkout)

    assert done.returncode == 2
    assert done.stdout == ""
    assert "is in the middle of a merge: scripts/workflow/x is not resolved" in done.stderr


def test_a_test_folder_is_named_as_git_names_it(tmp_path: Path) -> None:
    theirs = {
        "shipline.json": '{"parity": {"workflow_tests": "./app//tests/"}}',
        "app/tests/test_tick.py": "def test(): pass\n",
    }

    done = _both(tmp_path, {"tests/workflow/test_tick.py": "def test(): pass\n"}, theirs)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_submodule_is_compared_by_its_commit(tmp_path: Path) -> None:
    sha = "1" * 40
    shipline = _shipline(tmp_path, {})
    checkout = _checkout(tmp_path / "app", {})
    for root, commit in ((shipline, sha), (checkout, "2" * 40)):
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{commit},scripts/workflow/vendor",
            ],
            check=True,
        )

    done = _parity(shipline, checkout)

    assert done.returncode == 1
    assert [line for line in done.stdout.splitlines()[2:] if line[:1] in "+-"] == [
        f"-Subproject commit {sha}",
        f"+Subproject commit {'2' * 40}",
    ]


def test_a_path_git_holds_that_is_not_utf_8_is_printed(tmp_path: Path) -> None:
    shipline = _shipline(tmp_path, {})
    checkout = _checkout(tmp_path / "app", {})
    folder = checkout / "scripts/workflow"
    folder.mkdir(parents=True)
    with open(bytes(folder) + b"/bad\xff", "w") as file:
        file.write("x\n")
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)

    done = subprocess.run(
        [sys.executable, "-B", str(shipline / "tools/parity"), "--summary", str(checkout)],
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8:strict"},
    )

    assert (done.returncode, done.stderr) == (1, b"")
    assert done.stdout == b"1+ 0- - scripts/workflow/bad\\udcff\n"


def test_without_a_known_list_in_its_config_nothing_is_ignored(tmp_path: Path) -> None:
    shipline = _shipline(tmp_path, {"scripts/workflow/merge-line*": _TICK})
    # Shipline's own list does not speak for an installation.
    (shipline / "tools/parity-known.txt").write_text(
        "# The merge line.\nrename scripts/workflow/merge-line scripts/workflow/merge-train\n"
    )
    subprocess.run(["git", "-C", str(shipline), "add", "-A"], check=True)
    checkout = _checkout(tmp_path / "app", {"scripts/workflow/merge-train*": _TICK})

    done = _parity(shipline, checkout)

    assert done.returncode == 1
    assert (
        "+++ /dev/null" in done.stdout
        and "+++ checkout/scripts/workflow/merge-train" in done.stdout
    )


def test_a_known_list_the_config_names_but_that_is_not_there_is_refused(tmp_path: Path) -> None:
    theirs = {"shipline.json": json.dumps({"parity": {"known": _AT}})}

    done = _both(tmp_path, {}, theirs)

    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith(f"{tmp_path / 'app' / _AT}: cannot be read")


def test_the_summary_gives_one_line_per_file(tmp_path: Path) -> None:
    ours = {"scripts/workflow/tick*": _TICK, "scripts/workflow/gone": "a\nb\n", "runner/x": "1\n"}
    theirs = {
        "scripts/workflow/tick*": _TICK.replace("exit 0", "echo done\nexit 3"),
        "tests/workflow/test_new.py": "def test(): pass\n",
        ".github/runner/x*": "1\n",
    }
    known = "# An old name.\nrename scripts/workflow/old scripts/workflow/older\n"

    done = _both(tmp_path, ours, theirs, known, "--summary")

    assert (done.returncode, done.stderr) == (1, "")
    assert done.stdout == (
        "0+ 0- runner/x .github/runner/x\n"
        "0+ 2- scripts/workflow/gone -\n"
        "2+ 1- scripts/workflow/tick scripts/workflow/tick\n"
        "1+ 0- - tests/workflow/test_new.py\n"
        f"unused: {tmp_path / 'app' / _AT}:2: rename scripts/workflow/old scripts/workflow/older\n"
    )


def test_shipline_s_example_known_list_is_read(tmp_path: Path) -> None:
    entries = [
        line
        for line in _KNOWN.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    done = _both(tmp_path, {}, {}, _KNOWN.read_text())

    # Nothing to match here, so every entry is printed as unused, and none is refused.
    assert entries
    assert (done.returncode, done.stderr) == (1, "")
    assert done.stdout.splitlines() == [
        f"unused: {tmp_path / 'app' / _AT}:{number}: {line}"
        for number, line in enumerate(_KNOWN.read_text().splitlines(), 1)
        if line in entries
    ]


def test_shipline_s_tool_runs() -> None:
    assert _TOOL.stat().st_mode & 0o111
    done = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "-s", "tools/parity"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.startswith("100755 ")


@pytest.mark.parametrize("source", ["home", "xdg", "explicit"])
@pytest.mark.parametrize("qualified", [False, True])
def test_parity_reads_an_installation_config_and_its_known_list(
    tmp_path: Path, source: str, qualified: bool
) -> None:
    shipline = _shipline(tmp_path, {"scripts/workflow/tick": "before\n"})
    checkout = _checkout(tmp_path / "app", {"scripts/workflow/tick": "after\n",
                                            "shipline.json": "{"})
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                    "https://github.com/example-org/example-app.git"], check=True)
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    env.pop("SHIPLINE_CONFIG", None)
    env.pop("XDG_CONFIG_HOME", None)
    if source == "explicit":
        path = tmp_path / "installation" / "settings.json"
        env["SHIPLINE_CONFIG"] = str(path)
    else:
        base = tmp_path / "home" / ".config"
        if source == "xdg":
            base = tmp_path / "config"
            env["XDG_CONFIG_HOME"] = str(base)
        path = base / "shipline/example-org-example-app/shipline.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"install_folder": "programs", "parity": {
        "known": ("@install/" if qualified else "") + "parity.known"},
        "installation_only_key": "parity ignores other sections"}))
    installed = path.parent / "programs"
    installed.mkdir()
    known = installed / "parity.known"
    known.write_text("# Expected installation change.\nrewrite scripts/workflow/tick |before|after|\n")
    done = _parity(shipline, checkout, env=env)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    # The normal checkout-first order still applies, unless @install/ was requested.
    (checkout / "parity.known").write_text("")
    done = _parity(shipline, checkout, env=env)
    assert done.returncode == (0 if qualified else 1), done.stderr
    if qualified:
        known.unlink()
        done = _parity(shipline, checkout, env=env)
        assert done.returncode == 2
        assert done.stderr.startswith(str(known) + ": cannot be read")
