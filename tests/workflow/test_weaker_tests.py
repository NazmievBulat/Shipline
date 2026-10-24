"""The advisory report must catch weakened tests without penalizing new coverage."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/workflow/weaker_tests.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("weaker_tests", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "path,before,after,kind",
    [
        (
            "test_x.py",
            "def test_x():\n    assert x == 2\n",
            "def test_x():\n    pass\n",
            "assertion removed",
        ),
        (
            "test_x.py",
            "def test_x():\n    assert x == 2\n",
            "def test_x():\n    assert x in [1, 2]\n",
            "assertion weakened",
        ),
        ("x.test.ts", "expect(x).toBe(2)\n", "expect(x).toBeTruthy()\n", "assertion weakened"),
        (
            "x.spec.ts",
            "await expect(x).toHaveCount(2)\n",
            "expect(await x.count()).toBeGreaterThanOrEqual(1)\n",
            "assertion weakened",
        ),
        ("test_x.py", "with pytest.raises(ValueError):\n    f()\n", "f()\n", "assertion removed"),
        (
            "test_x.py",
            "def test_x():\n    assert elapsed < 1\n",
            "def test_x():\n    assert elapsed < 5\n",
            "numeric limit loosened",
        ),
        ("test_x.py", "timeout = 10\n", "timeout = 20\n", "numeric limit loosened"),
        ("x.spec.ts", "const retries = 0\n", "const retries = 2\n", "numeric limit loosened"),
        (
            "test_x.py",
            "def test_x():\n    assert x\n",
            "@pytest.mark.xfail\ndef test_x():\n    assert x\n",
            "skip/focus/retry marker added",
        ),
        (
            "x.spec.ts",
            "test('works', () => {})\n",
            "test.fixme('works', () => {})\n",
            "skip/focus/retry marker added",
        ),
        ("test_x.py", "def test_x():\n    assert x\n", "", "test deleted"),
        ("x.spec.ts", "test('works', () => {\n expect(x).toBe(1)\n})\n", "", "test deleted"),
    ],
)
def test_finds_weaker_test_changes(path: str, before: str, after: str, kind: str) -> None:
    items = _module().compare(path, before, after)
    assert any(item.kind == kind for item in items), items
    assert all(item.path == path and item.line >= 1 for item in items)


@pytest.mark.parametrize(
    "before,after",
    [
        ("", "def test_new():\n    assert x == 2\n"),
        ("def test_old():\n    assert x == 2\n", "def test_new():\n    assert x == 2\n"),
        ("def test_x():\n    assert x == 2\n", "def test_x():\n    assert (x == 2)\n"),
        ("# assert x == 1\n", "# assert x\n"),
        ("timeout = 20\n", "timeout = 10\n"),
    ],
)
def test_additions_renames_formatting_and_tighter_limits_are_quiet(before: str, after: str) -> None:
    assert _module().compare("test_x.py", before, after) == []


def test_assertion_count_and_multiline_expect() -> None:
    items = _module().compare(
        "x.test.ts",
        "expect(\n  x,\n).toBe(2)\nexpect(y).toBe(3)\n",
        "expect(x).toBe(2)\n",
    )
    assert any(item.kind == "assertion count decreased" for item in items)
    assert any(item.kind == "assertion removed" and "y" in item.detail for item in items)


@pytest.mark.parametrize(
    "path,before,after,kind",
    [
        (
            "test_x.py",
            "@pytest.mark.skipif(no_db)\ndef test_x():\n    assert x\n",
            "def test_x():\n    assert x\n\n@pytest.mark.skipif(no_db)\n"
            "def test_y():\n    assert y\n",
            "skip/focus/retry marker added",
        ),
        (
            "x.test.ts",
            "expect(x).toBeCloseTo(1, 5)\n",
            "expect(x).toBeCloseTo(1, 2)\n",
            "numeric limit loosened",
        ),
        (
            "test_x.py",
            "def test_x():\n    assert size > 100\n",
            "def test_x():\n    assert size > 10\n",
            "numeric limit loosened",
        ),
        (
            "test_x.py",
            "with pytest.raises(ValueError, match='bad'):\n    f()\n",
            "with pytest.raises(ValueError):\n    f()\n",
            "assertion weakened",
        ),
    ],
)
def test_more_weaker_changes(path: str, before: str, after: str, kind: str) -> None:
    assert any(item.kind == kind for item in _module().compare(path, before, after))


def test_increasing_a_minimum_size_is_stricter() -> None:
    assert _module().compare("test_x.py", "assert size > 10\n", "assert size > 100\n") == []


def test_cli_reports_deleted_files_renames_and_working_changes(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "test_renamed.py").write_text("def test_x():\n    assert x == 2\n")
    (tmp_path / "test_deleted.py").write_text("def test_deleted():\n    assert y == 3\n")
    (tmp_path / "test_changed.py").write_text("def test_changed():\n    assert z == 4\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    git("tag", "base")
    git("mv", "test_renamed.py", "test_new_name.py")
    git("rm", "test_deleted.py")
    git("commit", "-qam", "delete and rename")
    (tmp_path / "test_changed.py").write_text("def test_changed():\n    assert z\n")
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "base", "--working-tree"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "`test_deleted.py:1` — **test file deleted**" in done.stdout
    assert "test_changed.py:2" in done.stdout and "assertion weakened" in done.stdout
    assert "test_new_name.py" not in done.stdout


def test_changed_code_does_not_hide_a_looser_test_limit(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "test_x.py").write_text("timeout = 10\n")
    (tmp_path / "code.py").write_text("timeout = 10\n")
    git("add", ".")
    git("commit", "-qm", "baseline")
    git("tag", "base")
    (tmp_path / "test_x.py").write_text("timeout = 20\n")
    (tmp_path / "code.py").write_text("timeout = 20\n")
    git("commit", "-qam", "longer")
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "base"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "test_x.py:1" in done.stdout and "numeric limit loosened" in done.stdout
    assert "code.py:" not in done.stdout


@pytest.mark.parametrize(
    "source",
    [
        "@pytest.mark.skip(reason='needs a key')\ndef test_x():\n    assert x\n",
        "@pytest.mark.skipif(no_key, reason='needs a key')\ndef test_x():\n    assert x\n",
        "@pytest.mark.xfail(reason='broken')\ndef test_x():\n    assert x\n",
        "@pytest.mark.flaky(reruns=2)\ndef test_x():\n    assert x\n",
    ],
)
def test_python_markers_with_reasons_are_still_reported(source: str) -> None:
    assert any(
        item.kind == "skip/focus/retry marker added"
        for item in _module().compare("test_x.py", "def test_x():\n    assert x\n", source)
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ('assert elapsed < 1, "slow"\n', 'assert elapsed < 5, "slow"\n'),
        ("assert 100 < size\n", "assert 10 < size\n"),
        ("assert 10 > elapsed\n", "assert 20 > elapsed\n"),
    ],
)
def test_message_and_reversed_comparison_limits(before: str, after: str) -> None:
    assert any(
        item.kind == "numeric limit loosened"
        for item in _module().compare("test_x.py", before, after)
    )


@pytest.mark.parametrize("name", ["test", "it"])
def test_table_driven_test_deletion_is_reported(name: str) -> None:
    before = f'{name}.each([1, 2])("handles %s", value => {{ verify(value) }})\n'
    assert any(item.kind == "test deleted" for item in _module().compare("x.test.ts", before, ""))
    assert _module().compare("x.test.ts", before, before.replace("handles", "checks")) == []


def test_matching_a_refactored_assertion_does_not_hide_a_removed_timing_bound() -> None:
    before = (
        "assert time.monotonic() - started < 1\nassert report_text([read]).count('\\n') == 19999\n"
    )
    after = "assert text.count('\\n') == 19999\n"
    items = _module().compare("test_x.py", before, after)
    removed = [item for item in items if item.kind == "assertion removed"]
    assert len(removed) == 1 and "time.monotonic" in removed[0].detail


@pytest.mark.parametrize(
    "before,after",
    [
        ("assert elapsed < 1\n", "assert elapsed\n"),
        ("assert elapsed < 1\n", "assert elapsed <= 1\n"),
    ],
)
def test_replacing_a_bound_with_truthiness_or_inclusion_is_reported(
    before: str, after: str
) -> None:
    assert any(
        item.kind == "assertion weakened" for item in _module().compare("test_x.py", before, after)
    )


def test_removed_page_object_verification_is_an_assertion_removal() -> None:
    before = """test("x", async () => {
  await signup.verifyContinueIsOff()
  await dashboard.verifyVisible()
})
"""
    after = before.replace("  await signup.verifyContinueIsOff()\n", "")
    items = _module().compare("x.spec.ts", before, after)
    assert any(
        item.kind == "assertion removed" and "verifyContinueIsOff" in item.detail for item in items
    )
    assert any(item.kind == "assertion count decreased" for item in items)
    assert (
        _module().compare("x.spec.ts", before, before.replace('test("x"', 'test("renamed"')) == []
    )


@pytest.mark.parametrize(
    "path,before,after",
    [
        (
            "x.test.ts",
            "test('a', () => { expect(x).toBe(1) })\n",
            "test.skipIf(ci)('a', () => { expect(x).toBe(1) })\n",
        ),
        (
            "x.test.ts",
            "describe('a', () => { it('b', () => { expect(x).toBe(1) }) })\n",
            "describe.skipIf(ci)('a', () => { it('b', () => { expect(x).toBe(1) }) })\n",
        ),
        (
            "x.test.ts",
            "test('a', () => { expect(x).toBe(1) })\n",
            "test.runIf(local)('a', () => { expect(x).toBe(1) })\n",
        ),
        (
            "test_x.py",
            "def test_x():\n    assert x\n",
            "yaml = pytest.importorskip('yaml')\n\ndef test_x():\n    assert x\n",
        ),
        (
            "test_x.py",
            "def test_x():\n    assert x\n",
            "@unittest.skipIf(no_key, 'needs a key')\ndef test_x():\n    assert x\n",
        ),
    ],
)
def test_conditional_skips_are_reported(path: str, before: str, after: str) -> None:
    assert any(
        item.kind == "skip/focus/retry marker added"
        for item in _module().compare(path, before, after)
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ("expect(obj).toEqual({ a: 1, b: 2 })\n", "expect(obj).toMatchObject({ a: 1 })\n"),
        ("expect(obj).toStrictEqual({ a: 1 })\n", "expect(obj).toEqual({ a: 1 })\n"),
        ("expect(fn).toHaveBeenCalledTimes(2)\n", "expect(fn).toHaveBeenCalled()\n"),
        (
            "expect(obj).toEqual({ a: 1, b: 2 })\n",
            "expect(obj).toEqual(expect.objectContaining({ a: 1 }))\n",
        ),
    ],
)
def test_partial_or_at_least_matchers_are_weaker(before: str, after: str) -> None:
    assert any(
        item.kind == "assertion weakened" for item in _module().compare("x.test.ts", before, after)
    )


@pytest.mark.parametrize(
    "after",
    [
        "test.fail('works', () => {})\n",
        "test('works', () => { test.fail(isMobile, 'broken on phones') })\n",
    ],
)
def test_expected_failure_is_reported(after: str) -> None:
    assert any(
        item.kind == "skip/focus/retry marker added"
        for item in _module().compare("x.spec.ts", "test('works', () => {})\n", after)
    )


def test_a_deleted_test_file_is_one_item(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "test_gone.py").write_text(
        "def test_a():\n    assert a == 1\n    assert b == 2\n\ndef test_b():\n    assert c == 3\n"
    )
    git("add", ".")
    git("commit", "-qm", "baseline")
    git("tag", "base")
    git("rm", "-q", "test_gone.py")
    git("commit", "-qm", "delete")
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "base", "--json"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert [item["kind"] for item in json.loads(done.stdout)] == ["test file deleted"]


_STORY_FILE = (
    "export const Quiet: Story = { render: () => <Step /> }\n"
    "\n"
    "export const Refuses: Story = {\n"
    "  render: () => <Step />,\n"
    "  play: async ({ canvasElement }) => {\n"
    '    await expect(within(canvasElement).getByRole("alert")).toHaveTextContent("Enter it.")\n'
    "  },\n"
    "}\n"
)


def test_a_story_that_plays_is_a_test_and_a_story_that_only_draws_is_not() -> None:
    module = _module()
    assert [name for _, name, _ in module._tests("x.stories.tsx", _STORY_FILE)] == ["Refuses"]
    items = module.compare("x.stories.tsx", _STORY_FILE, _STORY_FILE.split("\n\n")[0] + "\n")
    assert {item.kind for item in items} >= {"test deleted", "assertion removed"}


def test_the_marker_above_a_story_names_the_browser_test_it_took_over() -> None:
    marked = (
        '// Moved from tests/browser/form.spec.ts: "refuses a blank name"\n'
        "// (it ran on a laptop and on a phone).\n" + _STORY_FILE.split("\n\n")[1]
    )
    assert _module().moved_to(marked) == {
        ("tests/browser/form.spec.ts", "refuses a blank name"): ["Refuses"]
    }


def test_a_browser_test_moved_to_a_story_is_named_with_it(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    spec = tmp_path / "tests/browser/form.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        'test("refuses a blank name", async () => {\n  await form.verifyRefused()\n})\n'
        "test(`keeps focus with ${key}`, async () => {\n  await form.verifyFocus(key)\n})\n"
        'test("is never moved", async () => {\n  await form.verifyStays()\n})\n'
    )
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-qm", "baseline")
    git("tag", "base")
    spec.write_text("")
    stories = tmp_path / "ui/src/form.stories.tsx"
    stories.parent.mkdir(parents=True)
    stories.write_text(
        '// Moved from tests/browser/form.spec.ts: "refuses a blank name"\n'
        + _STORY_FILE.split("\n\n")[1]
        + '\n// Moved from tests/browser/form.spec.ts: "keeps focus with ${key}"\n'
        + _STORY_FILE.split("\n\n")[1].replace("Refuses", "KeepsFocus")
    )
    git("add", ".")
    git("commit", "-qm", "move")
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "base"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert (
        '**test moved to a story**: `"refuses a blank name" → '
        "ui/src/form.stories.tsx \u203a Refuses`" in done.stdout
    )
    assert "ui/src/form.stories.tsx \u203a KeepsFocus`" in done.stdout
    # A test no story took over is still a deletion.
    assert '**test deleted**: `"is never moved"`' in done.stdout


def test_a_story_spread_from_another_counts_as_a_test() -> None:
    spread = (
        "export const Quiet: Story = { render: () => <Step /> }\n"
        "\n"
        "export const Again: Story = { ...Refuses, args: { name: '' } }\n"
    )
    module = _module()
    assert [name for _, name, _ in module._tests("x.stories.tsx", spread)] == ["Again"]


def test_props_spread_in_a_render_do_not_make_a_test() -> None:
    drawn = "export const Drawn: Story = { render: (args) => <Step {...args} /> }\n"
    assert _module()._tests("x.stories.tsx", drawn) == []


def test_a_story_made_by_a_call_counts_as_a_test() -> None:
    # Its play function may come from the call, so deleting it is reported, never silent.
    made = (
        "export const Quiet: Story = { render: () => <Step /> }\n"
        "\n"
        'export const ByEnter: Story = removeWith("Enter")\n'
    )
    module = _module()
    assert [name for _, name, _ in module._tests("x.stories.tsx", made)] == ["ByEnter"]
    items = module.compare("x.stories.tsx", made, made.split("\n\n")[0] + "\n")
    assert [item.kind for item in items] == ["test deleted"]


def test_a_helper_of_play_functions_is_read_like_a_story(tmp_path: Path) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    # Several stories share its checks, as tests/pages/ helpers are shared by specs.
    helper = tmp_path / "ui/src/components/rows.play.ts"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "export async function removeRow(canvas: HTMLElement) {\n"
        '  await userEvent.keyboard("{Enter}")\n'
        "  await expect(within(canvas).getByRole('button')).toHaveFocus()\n"
        "}\n"
    )
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-qm", "baseline")
    git("tag", "base")
    helper.write_text(
        "export async function removeRow(canvas: HTMLElement) {\n"
        '  await userEvent.keyboard("{Enter}")\n'
        "}\n"
    )
    git("commit", "-qam", "weaker")
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "base"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "ui/src/components/rows.play.ts" in done.stdout
    assert "**assertion removed**" in done.stdout
