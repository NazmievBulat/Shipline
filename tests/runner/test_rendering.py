"""One prefix names the whole runner pool: `runner/render-units.sh` writes the systemd units from
their templates in `runner/units/` and prints the pool's names, and for the prefix `example-app`
they are the files under `tests/runner/expected/`. Nothing but the prefix varies."""

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _ROOT / "runner"
_RENDER = _RUNNER / "render-units.sh"
_TEMPLATES = _RUNNER / "units"
_EXPECTED = Path(__file__).resolve().parent / "expected"

_SPEC = importlib.util.spec_from_file_location(
    "workflow_config", _ROOT / "scripts/workflow/workflow_config.py"
)
assert _SPEC and _SPEC.loader
workflow_config = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflow_config)

# Only what the renderer needs: never the caller's own settings.
_ENV = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "LANG": "C.UTF-8"}


def _run(*args: str, prefix: str | None = "example-app") -> subprocess.CompletedProcess[str]:
    env = dict(_ENV)
    if prefix is not None:
        env["RUNNER_PREFIX"] = prefix
    return subprocess.run(
        ["bash", str(_RENDER), *args], env=env, capture_output=True, text=True, check=False
    )


def _files(folder: Path) -> dict[str, str]:
    return {path.name: path.read_text() for path in sorted(folder.iterdir())}


def test_the_units_for_example_app_are_the_expected_ones(tmp_path: Path) -> None:
    done = _run(str(tmp_path / "units"))

    assert done.returncode == 0, done.stderr
    assert _files(tmp_path / "units") == _files(_EXPECTED / "units")


def test_the_names_for_example_app_are_the_expected_ones() -> None:
    done = _run("--names")

    assert done.returncode == 0, done.stderr
    assert done.stdout == (_EXPECTED / "names").read_text()


def test_only_the_prefix_varies(tmp_path: Path) -> None:
    assert _run(str(tmp_path / "units"), prefix="other-app").returncode == 0

    def swapped(text: str) -> str:
        return text.replace("example-app", "other-app")

    expected = {swapped(name): swapped(text) for name, text in _files(_EXPECTED / "units").items()}
    assert _files(tmp_path / "units") == expected
    assert _run("--names", prefix="other-app").stdout == swapped((_EXPECTED / "names").read_text())


def test_the_templates_hold_the_prefix_and_no_other_variable() -> None:
    for template in sorted(_TEMPLATES.iterdir()):
        text = template.read_text()
        assert set(re.findall(r"@\w*@", template.name)) == {"@PREFIX@"}, template.name
        assert set(re.findall(r"@\w*@", text)) <= {"@PREFIX@"}, template.name
        assert "example-app" not in text, template.name


def test_every_unit_is_named_by_the_prefix_and_names_no_one_s_home(tmp_path: Path) -> None:
    assert _run(str(tmp_path)).returncode == 0

    for name, text in _files(tmp_path).items():
        assert re.fullmatch(r"example-app-runner(?:-[a-z-]+)?@?\.(?:service|timer)", name)
        assert "/home/" not in text and "~" not in text, name


@pytest.mark.parametrize(
    "prefix", ["", "Example-app", "example_app", "example app", "-app", "a" * 26, "a/b", "a;b"]
)
def test_a_prefix_that_cannot_name_a_unit_and_a_user_is_refused_and_nothing_is_written(
    tmp_path: Path, prefix: str
) -> None:
    done = _run(str(tmp_path / "units"), prefix=prefix)

    assert done.returncode != 0
    assert "RUNNER_PREFIX" in done.stderr
    assert not (tmp_path / "units").exists()


def test_without_a_prefix_nothing_is_written(tmp_path: Path) -> None:
    done = _run(str(tmp_path / "units"), prefix=None)

    assert done.returncode != 0
    assert "RUNNER_PREFIX" in done.stderr
    assert not (tmp_path / "units").exists()


def test_the_labels_are_the_ones_the_merge_gate_reads() -> None:
    done = _run("--labels", "home", "office")

    assert done.returncode == 0, done.stderr
    assert tuple(done.stdout.splitlines()) == workflow_config.runner_labels(
        "example-app", ["home", "office"]
    )


def test_a_machine_name_that_cannot_name_a_label_is_refused() -> None:
    done = _run("--labels", "home", "../office")

    assert done.returncode != 0
    assert done.stdout == ""
