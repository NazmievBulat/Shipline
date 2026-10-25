"""What differs between the repositories that use Shipline, read from one file.

Shipline reads one file: `SHIPLINE_CONFIG` when set (a missing explicit file is refused),
else the first present of `$XDG_CONFIG_HOME/shipline/<owner>-<name>/shipline.json` (the config
home defaults to `~/.config`) and `shipline.json` at the root of the repository's main checkout.
The repository for lookup is the runner settings' `repository`, else the GitHub `origin`.
The main checkout is the repository being worked on (`SHIPLINE_REPO`, else the current
folder's repository), never found from where these scripts are. Worktrees share its config.
The installation folder is the external config's parent, or `install_folder` resolved against
the selected file. Relative programs come from main first, then this folder; `@install/` uses
only the installation. Files are never merged. Missing keys keep their defaults; unknown keys
and values of the wrong kind are refused with the selected file's path.

Defaults and key meanings are generated in `README.md`, "The configuration file".
Add a declaration below and run `python3 scripts/workflow/workflow_config.py docs --write`.

Python scripts import it (`current()`, once per run). A Bash script reads every value at once:

    config=$(python3 "$here/workflow_config.py" shell) || exit 1
    eval "$config"

which prints `SHIPLINE_<KEY>=value` lines quoted for `eval`, the selected file's path as
`SHIPLINE_CONFIG`, its contents as `SHIPLINE_CONFIG_JSON` for jq (`priority.jq`), and the
installation folder as `SHIPLINE_INSTALL_FOLDER`. A plain sh (`runner/install.sh`) reads
`workflow_config.py sh`, the same with each list as one value. Node (`codex-bridge.mjs`) and jq
read the JSON with the same defaults.
"""

import ast
import errno
import fnmatch
import glob
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable

FILE = "shipline.json"
# Where the runner pool names the repository its runners register with, and how long Bash may
# take to read it.
RUNNER_SETTINGS = ".github/runner/settings.sh"
RUNNER_SETTINGS_SECONDS = 5
# The prefix names a systemd unit and, as `<prefix>-runner`, a user of at most 32 characters.
_PREFIX = re.compile(r"[a-z](?:[a-z0-9-]{0,23}[a-z0-9])?")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")

# The most such a wait may be: a day. Bash would wrap a number far larger.
MOST_MINUTES = 24 * 60
# The keys of one machine.
MACHINE_KEYS = ("name", "address")

EFFORTS = ("low", "medium", "high", "xhigh", "max")
# A model name as `claude --model` takes it: a word a command line can hold unquoted.
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# A suite's keys, and its worker count when it names none: by its name, else one.
SUITE_KEYS = ("select", "run", "workers", "part", "probe")
SUITE_WORKERS = {"server": 4, "browser": 2}
_SUITE_NAME = re.compile(r"[A-Za-z0-9_-]+")

# `.` and `..` are no owner or name: `repos/../..` would leave the repository's own API.
_REPOSITORY = re.compile(r"(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+")
# A branch name's beginning, ended by `-` or `/` so the random name after it stands apart:
# words of letters, digits, `_`, `.` and `-`, none beginning with `.` or `-` nor ending in
# `.lock`, joined by single `/`; nothing `git ls-remote` reads as a pattern, and not a full ref.
_WORD = r"(?!refs/)[A-Za-z0-9_][A-Za-z0-9_.-]*(?<!\.lock)(?<!\.)"
_SCRATCH_PREFIX = re.compile(rf"(?!.*\.\.){_WORD}(?:/{_WORD})*[-/]")
# What `ssh` reads as a destination and nothing else: `user@host`, a name or an address, never a
# word that starts with a dash, which it would read as one of its own options.
_ADDRESS = re.compile(r"(?!-)[A-Za-z0-9_.@:\[\]-]+")
# An area is the last word of `session start hunt`, and the area and its screens go into the
# sentence a hunt is started with, inside double quotes in a shell command: no quotation mark,
# dollar, backtick, backslash or space. The screens are joined with commas there.
_AREA = re.compile(r"[a-z0-9][a-z0-9-]*")
_SCREEN = re.compile(r"/[A-Za-z0-9/._~:@%+*\[\]{}-]*")
_PLANS = re.compile(r"[A-Za-z0-9/._-]+")


class ConfigError(Exception):
    """The file or the repository it is in cannot be read; nothing falls back to a guess."""


@contextmanager
def _file_errors(path: Path | str) -> Iterator[None]:
    """Attach the selected file's path to refusals from the shared value validators."""
    try:
        yield
    except ConfigError as error:
        message = str(error)
        if message.startswith(FILE):
            raise ConfigError(str(path) + message[len(FILE):]) from None
        raise


@dataclass(frozen=True)
class Setting:
    kind: str
    default: Any
    meaning: str
    example: Any
    name: str | None = None
    # Stable positions preserve the shell interface while declarations and docs stay sorted.
    shell: tuple[int, str] | None = None
    derived_shell: tuple[int, str, str] | None = None
    convert: Callable[[Any], Any] | None = None
    serialize: Callable[[Any], Any] | None = None
    command_docs: tuple[str, str, str] | None = None


_UNSET = object()


def option(
    kind: str, default: Any, meaning: str, *, example: Any = _UNSET,
    name: str | None = None, shell: tuple[int, str] | None = None,
    derived_shell: tuple[int, str, str] | None = None,
    convert: Callable[[Any], Any] | None = None,
    serialize: Callable[[Any], Any] | None = None,
    command_docs: tuple[str, str, str] | None = None, optional: bool = False,
    position: int = sys.maxsize,
) -> Any:
    """A config key's validation kind, default, documentation and outward interfaces."""
    setting = Setting(kind, default, meaning, default if example is _UNSET else example,
                      name, shell, derived_shell, convert, serialize, command_docs)
    arguments: dict[str, Any] = {"metadata": {"setting": setting, "position": position}}
    if optional:
        arguments.update(default_factory=lambda: (
            convert(deepcopy(default)) if convert else deepcopy(default)
        ))
    return field(**arguments)


def section(
    cls: type | None = None, *, mapping: bool = False, position: int = sys.maxsize,
) -> Any:
    return field(metadata={"section": cls, "mapping": mapping, "position": position})


def config_section(cls: type) -> type:
    """Keep existing positional constructors while declarations are alphabetic in source."""
    def order(name: str) -> tuple[bool, int]:
        item = getattr(cls, name)
        optional = item.default is not MISSING or item.default_factory is not MISSING
        return optional, item.metadata["position"]

    cls.__annotations__ = dict(sorted(cls.__annotations__.items(), key=lambda pair: order(pair[0])))
    return dataclass(frozen=True)(cls)


def _declared_fields(cls: type) -> list[Any]:
    return sorted(fields(cls), key=lambda item: (
        item.metadata["setting"].name or item.name
        if "setting" in item.metadata else item.name
    ))


@dataclass(frozen=True)
class CheckGroup:
    workflow: str
    jobs: tuple[str, ...]
    suite: str | None = None

    def contains(self, name: str) -> bool:
        # Images is a whole workflow; its repository chooses the build and test job names.
        return any(name == job or name.startswith(job + " (") or
                   (job.endswith(":") and name.startswith(job + " ")) for job in self.jobs)


@config_section
class Checks:
    advisory_annotation: str | None = option('name', None, 'The advisory notice title on main; omitted means advisories make main red.', example='Advisory', optional=True, position=5)
    ci_plan: str = option('name', 'CI: what to run', 'The CI planner check name.', example='Build: what to run', shell=(5, 'CI_PLAN'), position=1)
    ci_result: str = option('name', 'CI result', 'The check that sums up CI.', example='Build result', shell=(4, 'CI_RESULT'), position=0)
    dependency_audit: str | None = option('name', None, 'The dependency audit job name; omitted means CI: dependencies.', example='Build: dependencies', optional=True, position=4)
    e2e_plan: str = option('name', 'E2E: what to run', 'The browser suite planner check name.', example='Browser: what to run', shell=(6, 'E2E_PLAN'), position=2)
    images: str = option('name', 'Docker images', 'The prefix of image checks; its planner appends : what to run.', example='Images', shell=(7, 'IMAGES'), derived_shell=(8, 'IMAGES_PLAN', 'images_plan'), position=3)
    prefix_only: list[str] = option('names', [], 'Distinct groups run only in merge prefixes: images, browser_whole and server_whole.', example=['images', 'browser_whole', 'server_whole'], optional=True, position=6)

    def groups(self) -> dict[str, CheckGroup]:
        return {
            "images": CheckGroup("images", (f"{self.images}:",)),
            "browser_whole": CheckGroup("e2e", ("playwright",), "browser_suite"),
            "server_whole": CheckGroup("ci", ("CI: server tests",), "server_tests"),
        }

    @property
    def images_plan(self) -> str:
        return f"{self.images}: what to run"


@config_section
class Annotations:
    browser_suite: str = option('name', 'Browser suite', 'The browser suite annotation title read by the merge gate.', example='Browser specs', shell=(13, 'ANNOTATION_BROWSER_SUITE'), position=4)
    files_tested: str = option('name', 'Files tested', 'The tested tree annotation title read by the merge gate.', example='Tree tested', shell=(10, 'ANNOTATION_FILES_TESTED'), position=1)
    merge_tested: str = option('name', 'Merge tested', 'The tested merge annotation title read by the merge gate.', example='Merge built', shell=(9, 'ANNOTATION_MERGE_TESTED'), position=0)
    server_tests: str = option('name', 'Server tests', 'The server tests annotation title read by the merge gate.', example='Server suite', shell=(11, 'ANNOTATION_SERVER_TESTS'), position=2)
    web_suite: str = option('name', 'Web suite', 'The web suite annotation title read by the merge gate.', example='Web unit suite', shell=(12, 'ANNOTATION_WEB_SUITE'), position=3)


@config_section
class Workflows:
    ci: str = option('name', 'ci.yml', 'The CI workflow file under .github/workflows/.', example='build.yml', shell=(14, 'WORKFLOW_CI'), position=0)
    e2e: str = option('name', 'e2e.yml', 'The browser workflow file under .github/workflows/.', example='browser.yml', shell=(15, 'WORKFLOW_E2E'), position=1)
    images: str = option('name', 'images.yml', 'The image workflow file under .github/workflows/.', shell=(16, 'WORKFLOW_IMAGES'), position=2)


@config_section
class Priorities:
    blocker: str = option('name', 'priority: blocker', 'The blocker priority label.', shell=(19, 'PRIORITY_BLOCKER'), position=0)
    high: str = option('name', 'priority: high', 'The high priority label.', shell=(20, 'PRIORITY_HIGH'), position=1)
    low: str = option('name', 'priority: low', 'The low priority label.', shell=(22, 'PRIORITY_LOW'), position=3)
    medium: str = option('name', 'priority: medium', 'The medium priority label.', shell=(21, 'PRIORITY_MEDIUM'), position=2)


@config_section
class Labels:
    audited: str = option('name', 'audited', 'The label that marks an audit.', shell=(18, 'LABEL_AUDITED'), position=1)
    priority: dict[str, str] = section(Priorities, mapping=True, position=2)
    workflow: str = option('name', 'workflow', 'The label that starts a ticket.', example='shipline', shell=(17, 'LABEL_WORKFLOW'), position=0)


@config_section
class Runner:
    hub_cache: str | None = option('name', None, 'The command running the Docker Hub cache; omitted means direct pulls.', example='tools/shipline/hub-cache', optional=True, shell=(28, 'RUNNER_HUB_CACHE'), position=6)
    identity_folder: str = option('name', '~/.config/shipline-runner', "The folder holding the runner programs' own GitHub identity.", shell=(23, 'IDENTITY_FOLDER'), position=1)
    image_build: str | None = option('name', None, 'The command that builds runner images; omitted means the pool cannot be installed.', example='tools/shipline/build-runner-images', optional=True, shell=(27, 'RUNNER_IMAGE_BUILD'), position=5)
    label: str | None = option('name', None, 'The base runner label; derived as `<prefix>-home` when omitted.', example='example-app-home', optional=True, shell=(25, 'RUNNER_LABEL'), position=3)
    labels: tuple[str, ...] = option('names', [], 'The runner labels eligible for failed-job reruns; derived from the base label when omitted.', example=['example-app-home', 'example-app-home-standby', 'example-app-home-first', 'example-app-home-main', 'example-app-home-main-batch', 'example-app-home-docker', 'example-app-home-planner'], shell=(34, 'RUNNER_LABELS'), position=0)
    planner_label: str | None = option('name', None, 'The planner runner label; derived as `<label>-planner` when omitted.', example='example-app-home-planner', optional=True, shell=(26, 'RUNNER_PLANNER_LABEL'), position=4)
    prefix: str | None = option('name', None, 'The word naming runner images, services, network and containers; omitted means no runner figures.', example='example-app', optional=True, shell=(24, 'RUNNER_PREFIX'), position=2)

    @property
    def identity_path(self) -> Path:
        return Path(self.identity_folder).expanduser()

    @property
    def main_label(self) -> str | None:
        """The label of main's runners; None without a runner pool."""
        return None if self.label is None else f"{self.label}-main"

    @property
    def main_batch_label(self) -> str | None:
        """The merge line's lane on main's runners, which they carry while no push to main
        waits (runner/lend_runners.py); None without a runner pool."""
        return None if self.label is None else f"{self.label}-main-batch"


@config_section
class Merge:
    scratch_prefix: str = option('name', 'workflow/merge-batch-', 'The beginning of scratch branch names for tested merge prefixes, ended by - or /.', position=0)


@config_section
class Parity:
    known: str | None = option('name', None, 'The expected differences file read by tools/parity; @install/ requires an installation folder.', example='tools/shipline/parity-known.txt', optional=True, position=2)
    runner_tests: str = option('name', 'tests/runner', 'The runner tests folder compared by tools/parity.', example='tools/runner/tests', position=1)
    workflow_tests: str = option('name', 'tests/workflow', 'The workflow tests folder compared by tools/parity.', example='tools/shipline/tests', position=0)


@dataclass(frozen=True)
class Machine:
    name: str
    # None for this machine.
    address: str | None = None


@config_section
class Hunts:
    # In the order the file names them.
    areas: dict[str, tuple[str, ...]] = option('areas', {}, 'Area names mapped to lists of screen paths; omitted means one hunt runs at a time, on every screen.', example={'phone': ['/calls', '/contacts'], 'billing': ['/invoices', '/settings/billing']}, shell=(35, 'HUNT_AREAS'), position=0)
    plans: str | None = option('name', None, 'The checkout path of hunt plans named in a hunt sentence.', example='docs/hunt-plans.md', optional=True, shell=(29, 'HUNT_PLANS'), convert=lambda value: _plans(value), position=1)


@config_section
class Refresh:
    clear_generated: tuple[str, ...] = option('names', [], 'Ignored, untracked generated paths or globs cleared after a refresh; tracked paths, links and paths outside the checkout are refused.', convert=tuple, position=0)
    config_file: Path | str = field(default=FILE, metadata={"position": 1})

    def targets(self, checkout: Path) -> list[str]:
        """Validate everything before deleting anything, without following symbolic links.

        Check again in the refreshed worktree: its index and ignore rules may have changed
        when main was merged. Git supplies the ignored, untracked files eligible for deletion.
        """
        with _file_errors(self.config_file):
            return self._targets(checkout)

    def _targets(self, checkout: Path) -> list[str]:
        root = checkout.resolve()
        targets: set[str] = set()
        for pattern in self.clear_generated:
            _relative(pattern, "refresh.clear_generated")
            parts = PurePosixPath(pattern).parts
            if ".git" in parts or "\\" in pattern or any(ord(char) < 32 for char in pattern):
                raise ConfigError(f"{FILE}: refresh.clear_generated contains an unsafe path")
            try:
                matches = list(_generated_matches(root, parts))
            except OSError:
                raise ConfigError(
                    f"{FILE}: refresh.clear_generated could not inspect configured output"
                ) from None
            # A missing literal or an unmatched glob must still be covered by ignore rules.
            for path in matches or [root / pattern]:
                relative = path.relative_to(root).as_posix()
                if relative == "." or ".git" in path.relative_to(root).parts:
                    raise ConfigError(
                        f"{FILE}: refresh.clear_generated cannot name git metadata or the checkout"
                    )
                _generated_no_links(root, path)
                ignored = _generated_git(root, "check-ignore", "--no-index", "-q", "--", relative)
                if ignored.returncode != 0:
                    raise ConfigError(f"{FILE}: refresh.clear_generated path is not ignored: {relative}")
                files = _generated_git(
                    root, "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                    "--", f":(literal){relative}",
                )
                if files.returncode or files.stdout:
                    raise ConfigError(
                        f"{FILE}: refresh.clear_generated path contains tracked or "
                        f"non-ignored files: {relative}"
                    )
                targets.add(relative)
        return sorted(targets)

    def clear(self, checkout: Path) -> None:
        with _file_errors(self.config_file):
            self._clear(checkout)

    def _clear(self, checkout: Path) -> None:
        targets = self.targets(checkout)
        allowed: set[str] = set()
        for relative in targets:
            files = _generated_git(
                checkout, "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                "--", f":(literal){relative}",
            )
            if files.returncode:
                raise ConfigError(f"{FILE}: refresh.clear_generated could not list ignored files")
            for name in files.stdout.split("\0"):
                if not name:
                    continue
                if name != relative and not name.startswith(relative + "/"):
                    raise ConfigError(f"{FILE}: refresh.clear_generated file is outside its target")
                _relative(name, "refresh.clear_generated")
                allowed.add(name)
        # Hold every directory open without following links. Only Git's approved files are
        # unlinked; a newly created file is retained, along with any directory it keeps busy.
        for relative in targets:
            descriptor = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = PurePosixPath(relative).parts
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                _remove_generated(descriptor, parts[-1], relative, allowed)
            except FileNotFoundError:
                pass  # A previous, overlapping target already cleared it, or it was absent.
            except OSError:
                raise ConfigError(f"{FILE}: refresh.clear_generated could not clear output") from None
            finally:
                os.close(descriptor)


def _remove_generated(parent: int, name: str, relative: str, allowed: set[str]) -> None:
    mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    if stat.S_ISLNK(mode):
        raise ConfigError(f"{FILE}: refresh.clear_generated cannot traverse a symbolic link")
    if stat.S_ISDIR(mode):
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            children = os.listdir(descriptor)
            if name == ".git" or ".git" in children:
                raise ConfigError(f"{FILE}: refresh.clear_generated cannot enter a nested checkout")
            for child in children:
                try:
                    _remove_generated(descriptor, child, relative + "/" + child, allowed)
                except FileNotFoundError:
                    pass
        finally:
            os.close(descriptor)
        try:
            os.rmdir(name, dir_fd=parent)
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
    elif relative in allowed:
        os.unlink(name, dir_fd=parent)


def _generated_git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError(f"{FILE}: refresh.clear_generated could not inspect the worktree") from None


def _generated_matches(
    root: Path, parts: tuple[str, ...], *, top: bool = True
) -> Iterator[Path]:
    """Shell-style segments, including **, with no traversal through a symbolic link."""
    if root.is_symlink():
        raise ConfigError(f"{FILE}: refresh.clear_generated cannot traverse a symbolic link")
    if not top and (
        root.name == ".git" or (root / ".git").is_symlink() or (root / ".git").exists()
    ):
        raise ConfigError(f"{FILE}: refresh.clear_generated cannot enter a nested checkout")
    if not parts:
        if root.exists():
            yield root
    elif parts[0] == "**":
        yield from _generated_matches(root, parts[1:], top=top)
        if root.is_dir():
            for child in root.iterdir():
                # Recursive discovery stays inside this checkout and never follows a link.
                # A link or nested checkout selected explicitly is still refused above.
                if child.name == ".git":
                    continue
                if child.is_symlink():
                    if len(parts) == 1:
                        raise ConfigError(
                            f"{FILE}: refresh.clear_generated cannot traverse a symbolic link"
                        )
                    continue
                if child.is_dir():
                    if (child / ".git").is_symlink() or (child / ".git").exists():
                        continue
                    yield from _generated_matches(child, parts, top=False)
                elif len(parts) == 1:
                    yield child
    elif glob.has_magic(parts[0]):
        if root.is_dir():
            for child in root.iterdir():
                if fnmatch.fnmatchcase(child.name, parts[0]):
                    yield from _generated_matches(child, parts[1:], top=False)
    else:
        yield from _generated_matches(root / parts[0], parts[1:], top=False)


def _generated_no_links(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ConfigError(f"{FILE}: refresh.clear_generated cannot traverse a symbolic link")
        if (current / ".git").is_symlink() or (current / ".git").exists():
            raise ConfigError(f"{FILE}: refresh.clear_generated cannot enter a nested checkout")
    if path.is_dir():
        for folder, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                if name == ".git" or (Path(folder) / name).is_symlink():
                    raise ConfigError(
                        f"{FILE}: refresh.clear_generated contains git metadata or a symbolic link"
                    )


@config_section
class Commands:
    # Each None when the file names none.
    advisory_watch: str | dict[str, Any] | None = option('command', None, 'The command that detects an advisory service outage and checks whether the service answers again.', example='tools/shipline/advisory-watch', shell=(45, 'COMMAND_ADVISORY_WATCH'), command_docs=("the merge gate: `unreachable` with a failed push's output, or a failed audit's annotations (without the advisories notice, `checks.advisory_annotation`), on standard input; `answers <service>`", '`unreachable` prints the advisory service the text says was unreachable, or nothing; `answers` exits 0 when the service answers again. Beside the advisories notice, a service it names, or a failure of its own, keeps main red', "no outage is read: a failed push is refused as it is, and main's advisories notice stands alone"), position=10)
    browser: str | dict[str, Any] | None = option('command', None, "The repository browser test command run by bash from the checkout with the E2E job's arguments.", example='tools/run-browser-tests', command_docs=("E2E's browser job: the job's own arguments.", "The command's output and exit code.", 'The browser job fails and asks for commands.browser.'), position=1)
    browser_plan: str | dict[str, Any] | None = option('command', None, 'The command that reads NUL-ended changed paths and prints needed or none.', example='tools/shipline/browser-plan', shell=(41, 'COMMAND_BROWSER_PLAN'), command_docs=('the merge gate: the changed paths on standard input, each ended by a NUL', 'prints `needed` or `none` and exits 0; anything else, a crash or another exit included, counts as needed', 'every merge that changes a file needs browser tests while the merged tree holds the E2E workflow (`workflows.e2e`), and none does without it'), position=6)
    browser_tests_for_changes: str | dict[str, Any] | None = option('command', None, 'The command that selects browser spec paths for a changed base.', example={'run': 'tools/shipline/browser-tests-for-changes', 'form': 'json', 'field': 'specs', 'args': ['--ci']}, shell=(37, 'COMMAND_BROWSER_TESTS_FOR_CHANGES'), command_docs=("the review panel in a conflict round, and sessions: `<base> [--files-from <file>]` and the session's own words", 'the browser specs the change reaches, one path a line', 'nothing is selected, and CI runs the whole browser suite'), position=2)
    hunt_app: str | dict[str, Any] | None = option('command', None, 'The command that manages the application under a hunt, including stop.', example='tools/shipline/hunt-app', shell=(42, 'COMMAND_HUNT_APP'), command_docs=("`session` when a hunt stops: `stop`, in the hunt's worktree; the hunter itself, by `shipline hunt-app`", '', '`session start hunt` refuses'), position=7)
    hunt_browser: str | dict[str, Any] | None = option('command', None, 'The browser command named in a hunt sentence.', example='node tools/shipline/hunt-browser.mjs', shell=(43, 'COMMAND_HUNT_BROWSER'), command_docs=('the hunter, by `shipline hunt-browser`, which its sentence names', '', 'the sentence names none'), position=8)
    hunt_cursor: str | dict[str, Any] | None = option('command', None, 'The command that starts a Cursor hunt with its worktree, report folder and sentence.', example='tools/shipline/hunt-cursor', shell=(44, 'COMMAND_HUNT_CURSOR'), command_docs=('`session start hunt <name> <kind> cursor`: `<worktree> <report folder> <sentence>`', "what it prints is kept in the report folder's `cursor.log`", 'a Cursor hunt is refused'), position=9)
    mechanical_conflicts: str | dict[str, Any] | None = option('command', None, 'The command that resolves conflicts, renumbers files and reports taken numbers.', example='tools/shipline/mechanical-conflicts', shell=(40, 'COMMAND_MECHANICAL_CONFLICTS'), command_docs=("the merge gate's refresh: `resolve <head> <main>` with the merge of main in progress; `renumber <head> <main>` after a clean merge; `taken <head> <main>` before a batch", "`resolve` stages its resolution, prints each file it resolved and exits 0 only when it resolved them all; `renumber` stages its moves and prints `<old path> <new path>` a line (paths without spaces), or exits non-zero, naming on standard output the files it cannot move (left as a conflict) and why on standard error; `taken` prints the branch's files whose number main took", 'every conflict is left to the session, nothing is renumbered and main took no number'), position=5)
    pre_train_checks: str | dict[str, Any] | None = option('command', None, 'The checks run before a pull request is admitted to the merge train.', example={'run': 'tools/shipline/pre-train-checks', 'form': 'exit'}, shell=(39, 'COMMAND_PRE_TRAIN_CHECKS'), command_docs=('the merge gate, before a pull request joins the merge train, in its worktree', 'exits non-zero when a check failed; what it prints is shown', 'every pull request is admitted'), position=4)
    push_checkout: str | dict[str, Any] | None = option('command', None, 'The command that installs changed checkout files and explains a failed push hook.', example='tools/shipline/push-checkout', shell=(46, 'COMMAND_PUSH_CHECKOUT'), command_docs=("the merge gate, before each push and after one that failed: `install <checkout>`; `refusal <checkout>` with the push's whole output on standard input", "`install` installs what changed since its last install and exits non-zero with the reason when it failed, which is a wait; `refusal` prints the hook's failed step with its last lines", 'nothing is installed, and a failed push is refused with its last 10 lines'), position=11)
    push_checks: str | dict[str, Any] | None = option('command', None, "The checks run by the repository's pre-push hook.", example='tools/shipline/push-checks', shell=(47, 'COMMAND_PUSH_CHECKS'), command_docs=('The pre-push hook: the selected changes.', "The command's output and exit code.", 'No additional push checks run.'), position=12)
    server_tests_for_changes: str | dict[str, Any] | None = option('command', None, 'The command that selects server tests and returns whole, tests, total and reasons.', example='tools/shipline/server-tests-for-changes', shell=(38, 'COMMAND_SERVER_TESTS_FOR_CHANGES'), command_docs=('the review panel in a conflict round, and sessions: `<base> [--files-from <file>]`', 'one JSON object, `{"whole": bool, "tests": [paths], "total": int, "reasons": [str]}`', 'the whole server suite: `{"whole": true, "tests": [], "total": 0, "reasons": ["no server test selection is configured"]}`'), position=3)
    test: str | dict[str, Any] | None = option('command', None, "The repository test command run by bash from the checkout with the CI job's arguments.", example='tools/run-tests', command_docs=("CI's test job: the job's own arguments.", "The command's output and exit code.", 'The test job fails and asks for commands.test.'), position=0)


@dataclass(frozen=True)
class Suite:
    name: str
    # The command lines, as the file names them: `select` is given the base and the changed
    # files, `run` the selected files, `{workers}` in it the worker count.
    select: str
    run: str
    workers: int
    # Added to `run` for one part of the suite: `{shard}` of `{shards}`.
    part: str | None = None
    # Exits 0 when the suite's browser can start.
    probe: str | None = None


@config_section
class CloudPrePush:
    # In the order the file names them.
    suites: tuple[Suite, ...] = option('suites', {}, 'Named cloud pre-push suites with select, run, workers and optional part and probe commands.', example={'server': {'select': 'tools/shipline/server-tests-for-changes', 'run': 'tools/shipline/server-tests --workers {workers}', 'workers': 4, 'part': '--shard {shard}/{shards}'}, 'browser': {'select': 'tools/shipline/browser-tests-for-changes --ci', 'run': 'tools/shipline/browser-tests --workers {workers}', 'workers': 2, 'probe': 'tools/shipline/browser-starts'}}, optional=True, convert=tuple, serialize=lambda config: {suite.name: {key: getattr(suite, key) for key in SUITE_KEYS if getattr(suite, key) is not None} for suite in config.suites}, position=0)


@config_section
class Review:
    high_effort_paths: tuple[str, ...] = option('names', [], 'Regular expressions selecting changed paths for an independent review at high effort.', example=['^db/migrations/', '^db/schema\\.sql$', '^app/models/', '^prompts/', '(^|/)AGENTS\\.md$', '\\.prompt\\.md$', '^app/jobs/', '^app/queues/', '(^|/)locks?\\.py$', '^app/auth/', '^app/permissions/', '^config/secrets'], convert=lambda value: _patterns(value, "review.high_effort_paths"), position=0)


@dataclass(frozen=True)
class ConflictReview:
    effort: str
    model: str
    # A resolution larger than this, in lines of the conflicting files, gets the usual reviewer.
    max_changed_lines: int


@config_section
class MergeTrain:
    queued_limit_minutes: int = option('minutes', 20, 'Minutes a scratch CI run may remain queued before its wait is bounded.', position=0)


@config_section
class Config:
    annotations: Annotations = section(position=5)
    answer_for_owner_after_minutes: int = option('minutes', 15, 'Minutes before the tick answers a question marked Owner only: no; 0 disables it, at most 1440.', shell=(30, 'ANSWER_FOR_OWNER_AFTER_MINUTES'), position=18)
    checks: Checks = section(position=4)
    cloud_pre_push: CloudPrePush = section(position=20)
    cloud_wake_after_minutes: int = option('minutes', 30, 'Minutes without a push or report before a cloud worker is woken; 0 disables it.', example=45, position=19)
    commands: Commands = section(position=13)
    conflict_review: ConflictReview | None = option('conflict', None, 'The effort, model and max_changed_lines for a small handed-off conflict; omitted means the usual reviewer.', example={'effort': 'medium', 'model': 'sonnet', 'max_changed_lines': 40}, position=15)
    hot_paths: tuple[str, ...] = option('paths', [], 'GitHub path patterns whose matching tickets run separately and merge back to back within their priority.', example=['src/example_app/models.py', 'web/src/pages/**'], convert=tuple, position=14)
    hunts: Hunts = section(position=12)
    install_folder: Path | None = option('name', None, 'The installation folder, resolved against the selected file; external configs use their parent when omitted.', example='.', serialize=lambda config: config.given.get("install_folder"), position=24)
    labels: Labels = section(position=7)
    machines: tuple[Machine, ...] = option('machines', [], 'Machines sampled by flow-page, each with a name and optional SSH address; omitted samples this machine.', example=[{'name': 'this machine'}, {'name': 'runner-1', 'address': 'runner@runner-1.example'}], position=11)
    merge: Merge = section(position=9)
    merge_train: MergeTrain = section(position=17)
    parity: Parity = section(position=10)
    refresh: Refresh = section(position=3)
    found_repository: str | None = option('name', None, 'The GitHub repository as owner/name; discovered from runner settings, then origin when omitted.', example='example-owner/example-app', name='repository', shell=(1, 'REPOSITORY'), position=0)
    review: Review = section(position=16)
    runner: Runner = section(position=8)
    state_folder: str = option('name', '.local/workflow', 'The folder of workflow notes in the main checkout and each worktree.', example='.local/shipline', shell=(2, 'STATE_FOLDER'), convert=lambda value: _relative(value, "state_folder"), position=1)
    workflows: Workflows = section(position=6)
    worktree_setup: str = option('name', 'scripts/setup-worktree', 'The command that prepares a new worktree, run only when present.', example='tools/prepare-worktree', shell=(3, 'WORKTREE_SETUP'), convert=lambda value: _relative(value, "worktree_setup"), position=2)
    # The file as read, without defaults. Context fields are not config declarations.
    given: dict[str, Any] = field(metadata={"position": 21})
    main: Path = field(metadata={"position": 22})
    path: Path = field(metadata={"position": 23})

    def command(self, name: str) -> list[str] | None:
        """A command's words, with its script resolved from main or the installation folder.
        The other words are passed as written."""
        if name not in RUN_AS_WORDS:
            raise KeyError(name)
        line = getattr(self.commands, name)
        if line is None:
            return None
        entry = line if isinstance(line, dict) else {"run": line, "form": "lines"}
        with _file_errors(self.path):
            if entry["form"] == "functions":
                words = [str(self.file(entry["run"]))]
            else:
                words = self._resolve(_words(entry["run"], name))
        words += entry.get("args", [])
        if entry["form"] in ("functions", "json"):
            # The same argv works for the gate, shell callers and the launcher. Conversion
            # runs inside the existing caller's command timeout and process group.
            return [
                sys.executable,
                str(Path(__file__).with_name("workflow_commands.py")),
                "--repository-command", name, json.dumps(entry), *words,
            ]
        return words

    def words(self, line: str) -> list[str]:
        """A cloud pre-push suite's command line, resolved as a command is."""
        return self._resolve(shlex.split(line))

    def file(self, word: str) -> Path:
        """A main-checkout path first, then an installed path; @install/ is installed only."""
        return resolve_file(word, self.main, self.install_folder, self.path)

    def _resolve(self, words: list[str]) -> list[str]:
        script = _script(words)
        if script is not None:
            words[script] = str(self.file(words[script]))
        return words

    @property
    def repository(self) -> str:
        if self.found_repository is None:
            raise ConfigError(
                f"cannot tell which repository the workflow works on: {self.path} names none, "
                f"{RUNNER_SETTINGS} is missing or names none, and origin names no GitHub "
                "repository"
            )
        return self.found_repository

    @property
    def api(self) -> str:
        return f"repos/{self.repository}"

    def state(self, checkout: Path | None = None) -> Path:
        """The state folder of a checkout: the main checkout's when none is named."""
        return (checkout or self.main) / self.state_folder

    def checkout_of(self, state: Path) -> Path:
        """The checkout a state folder is in, however many folders deep it is named."""
        return state.parents[len(PurePosixPath(self.state_folder).parts) - 1]

    def hot_paths_of(self, path: str) -> list[str]:
        """The hot paths a file of the repository matches, in the order of the file."""
        return [hot for hot in self.hot_paths if _hot_pattern(hot).fullmatch(path)]


def _declarations(cls: type = Config, prefix: str = "") -> Iterator[tuple[str, Setting]]:
    for item in _declared_fields(cls):
        if setting := item.metadata.get("setting"):
            yield prefix + (setting.name or item.name), setting
        elif "section" in item.metadata:
            yield from _declarations(item.metadata["section"] or item.type,
                                     prefix + item.name + ".")


def _defaults(cls: type = Config) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in fields(cls):
        if setting := item.metadata.get("setting"):
            result[setting.name or item.name] = deepcopy(setting.default)
        elif "section" in item.metadata:
            result[item.name] = _defaults(item.metadata["section"] or item.type)
    return result


def _configured(cls: type, values: dict[str, Any], **overrides: Any) -> Any:
    """Build declared sections and apply only their existing value conversions."""
    arguments = {}
    for item in fields(cls):
        if item.name in overrides:
            continue
        if setting := item.metadata.get("setting"):
            value = values[setting.name or item.name]
            arguments[item.name] = setting.convert(value) if setting.convert else value
        elif "section" in item.metadata:
            section_type = item.metadata["section"] or item.type
            child = _configured(section_type, values[item.name])
            arguments[item.name] = vars(child).copy() if item.metadata["mapping"] else child
    return cls(**arguments, **overrides)


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_value(value) for key, value in vars(value).items() if value is not None}
    if isinstance(value, dict):
        return {key: _json_value(value) for key, value in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _as_values(config: Any) -> dict[str, Any]:
    result = {}
    for item in fields(config):
        if setting := item.metadata.get("setting"):
            key = setting.name or item.name
            value = setting.serialize(config) if setting.serialize else getattr(config, key)
            result[key] = _json_value(value)
        elif "section" in item.metadata:
            value = getattr(config, item.name)
            result[item.name] = _json_value(value) if item.metadata["mapping"] else _as_values(value)
    return result


DECLARATIONS = dict(_declarations())
DEFAULTS = _defaults()
# Shell arrays keep their established order; the fields themselves are sorted by name.
RUN_AS_WORDS = tuple(
    name.removeprefix("commands.")
    for name, setting in sorted(DECLARATIONS.items(), key=lambda pair: pair[1].shell or (0, ""))
    if name.startswith("commands.") and setting.shell
)


def resolve_file(word: str, main: Path, install_folder: Path | None, config_file: Path) -> Path:
    """Resolve a validated relative program or known-list path, with checkout precedence."""
    if word.startswith("@install/"):
        if install_folder is None:
            raise ConfigError(f"{config_file}: @install/ requires an install_folder")
        return install_folder / word.removeprefix("@install/")
    checkout = main / word
    if install_folder is not None and not os.path.lexists(checkout):
        return install_folder / word
    return checkout


def main_checkout(start: Path) -> Path:
    done = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise ConfigError(f"{start} is not in a git repository: {done.stderr.strip()}")
    return Path(done.stdout.strip()).parent


def repository_checkout() -> Path:
    """The main checkout of the repository Shipline works on: the one `SHIPLINE_REPO` names
    (`shipline --repo` sets it), else the one the current directory is in. Never found from
    where these scripts are: they are in Shipline's own checkout."""
    named = os.environ.get("SHIPLINE_REPO")
    if named:
        try:
            return main_checkout(Path(named))
        except ConfigError:
            raise ConfigError(
                f"SHIPLINE_REPO names {named}, which is in no git repository"
            ) from None
    here = Path.cwd()
    try:
        return main_checkout(here)
    except ConfigError:
        raise ConfigError(
            f"{here} is in no git repository: run Shipline inside the checkout of the repository "
            "it works on, or name that checkout with SHIPLINE_REPO or `shipline --repo <path>`."
        ) from None


def _merged(defaults: dict[str, Any], given: Any, where: str) -> dict[str, Any]:
    if not isinstance(given, dict):
        raise ConfigError(f"{FILE}: {where or 'the file'} must be an object")
    unknown = sorted(set(given) - set(defaults))
    if unknown:
        raise ConfigError(f"{FILE}: unknown key {', '.join(where + name for name in unknown)}")
    merged: dict[str, Any] = {}
    for key, default in defaults.items():
        name = where + key
        if key not in given:
            merged[key] = default
        elif name not in DECLARATIONS:
            merged[key] = _merged(default, given[key], name + ".")
        elif DECLARATIONS[name].kind == "command" and isinstance(given[key], dict):
            merged[key] = _command_entry(given[key], key)
        elif DECLARATIONS[name].kind in ("paths", "conflict", "machines", "areas", "suites"):
            kind = DECLARATIONS[name].kind
            readers = {"paths": _hot_paths, "conflict": _conflict_review,
                       "machines": lambda value, _: _machines(value),
                       "areas": lambda value, _: _areas(value),
                       "suites": lambda value, _: _suites(value)}
            merged[key] = readers[kind](given[key], name)
        elif DECLARATIONS[name].kind == "names":
            value = given[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ConfigError(f"{FILE}: {name} must be a list of names")
            merged[key] = value
        elif DECLARATIONS[name].kind == "minutes":
            value = given[key]
            # `true` is an int to Python, and no number of minutes.
            if type(value) is not int or not 0 <= value <= MOST_MINUTES:
                raise ConfigError(
                    f"{FILE}: {name} must be a whole number from 0 to {MOST_MINUTES}, "
                    f"not {json.dumps(value)}"
                )
            merged[key] = value
        else:
            value = given[key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{FILE}: {name} must be a name, not {json.dumps(value)}")
            merged[key] = value
    return merged


def _hot_paths(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{FILE}: {name} must be a list of paths")
    for item in value:
        path = PurePosixPath(item)
        # A `!` takes files back in a path filter; here it would match nothing.
        if path.is_absolute() or ".." in path.parts or item.startswith("!"):
            raise ConfigError(f"{FILE}: {name} must be paths inside the checkout, not {item}")
    return value


def _conflict_review(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{FILE}: {name} must be an object")
    keys = ("effort", "model", "max_changed_lines")
    if unknown := sorted(set(value) - set(keys)):
        raise ConfigError(f"{FILE}: unknown key {', '.join(f'{name}.{key}' for key in unknown)}")
    if missing := [key for key in keys if key not in value]:
        raise ConfigError(f"{FILE}: {name} must name {', '.join(missing)}")
    if value["effort"] not in EFFORTS:
        raise ConfigError(f"{FILE}: {name}.effort must be one of {', '.join(EFFORTS)}")
    # `codex` and `cloud` name kinds of worker for `session start`, not a model.
    if (
        not isinstance(value["model"], str)
        or not _MODEL.fullmatch(value["model"])
        or value["model"] in ("codex", "cloud")
    ):
        raise ConfigError(
            f"{FILE}: {name}.model must be a model name, not {json.dumps(value['model'])}"
        )
    lines = value["max_changed_lines"]
    if not isinstance(lines, int) or isinstance(lines, bool) or lines < 1:
        raise ConfigError(f"{FILE}: {name}.max_changed_lines must be a whole number above 0")
    return value


def path_pattern(pattern: str) -> re.Pattern[str]:
    """One path pattern as a regular expression, by the rules of GitHub's path filters (the merge
    gate's `path_filter`, `hot_paths`). `*` is any characters but `/`, `**` is any characters,
    and `**/` where a folder's name starts is zero or more folders (inside a name, `**` is any
    characters and the `/` stays). `?` and `+` make what stands before them
    zero or one, or one or more, of it. `[...]` is one character of its set, with `-` ranges.
    A backslash makes the next character itself, and every other character is itself.

    GitHub does not document a `?` or `+` with nothing before it, or a `[` that no `]` closes
    (nor an empty `[]`): each of those is read as the character itself.
    """
    # Each part is what one piece of the pattern matches and how often it may stand: at least
    # `fewest` (0 or 1) times, and at most once or, with `many`, any number of times. Repeating
    # a part only changes those two, so no repetition ever nests inside another.
    parts: list[tuple[str, int, bool]] = []
    at = 0
    while at < len(pattern):
        char = pattern[at]
        if pattern.startswith("**/", at) and pattern[at - 1 : at] in ("", "/"):
            # Zero or more folders: repeating it, however, is still that.
            parts.append(("(?:.*/)?", 1, False))
            at += 3
            continue
        if pattern.startswith("**", at):
            parts.append((".", 0, True))
            at += 2
            continue
        at += 1
        if char == "*":
            parts.append(("[^/]", 0, True))
        elif char in "?+" and parts:
            expression, fewest, many = parts[-1]
            if expression != "(?:.*/)?":
                parts[-1] = (expression, 0, many) if char == "?" else (expression, fewest, True)
        elif char == "\\" and at < len(pattern):
            parts.append((re.escape(pattern[at]), 1, False))
            at += 1
        elif char == "[" and (found := _pattern_set(pattern, at)):
            expression, at = found
            parts.append((expression, 1, False))
        else:
            parts.append((re.escape(char), 1, False))
    times = {(1, False): "", (0, False): "?", (1, True): "+", (0, True): "*"}
    return re.compile(
        "".join(expression + times[fewest, many] for expression, fewest, many in parts),
        flags=re.DOTALL,
    )


def _pattern_set(pattern: str, at: int) -> tuple[str, int] | None:
    """The set that starts at `at`, just after a `[`, as one character of a regular expression,
    and where the pattern goes on after its `]`; None when no `]` closes a set of at least one
    character. A backslash makes the next character itself. A `-` between two characters of
    a-z, of A-Z or of 0-9 is the range from the first to the second, as GitHub allows ranges
    only there; any other `-`, and a range that runs backwards, is the characters themselves.
    """
    chars: list[tuple[str, bool]] = []
    while at < len(pattern) and (pattern[at] != "]" or not chars):
        if pattern[at] == "]":
            return None
        escaped = pattern[at] == "\\" and at + 1 < len(pattern)
        if escaped:
            at += 1
        chars.append((pattern[at], escaped))
        at += 1
    if at == len(pattern):
        return None
    members: list[str] = []
    index = 0
    while index < len(chars):
        char = chars[index][0]
        last = chars[index + 2][0] if index + 2 < len(chars) else ""
        ranged = any(first <= char <= last <= final for first, final in ("az", "AZ", "09"))
        if chars[index + 1 : index + 2] == [("-", False)] and ranged:
            members.append(f"{re.escape(char)}-{re.escape(last)}")
            index += 3
        else:
            members.append(re.escape(char))
            index += 1
    return f"[{''.join(members)}]", at + 1


@cache
def _hot_pattern(pattern: str) -> re.Pattern[str]:
    return path_pattern(pattern)
def _machines(given: Any) -> list[dict[str, str]]:
    if not isinstance(given, list):
        raise ConfigError(f"{FILE}: machines must be a list of machines")
    machines = []
    for machine in given:
        if not isinstance(machine, dict):
            raise ConfigError(f"{FILE}: machines must be a list of machines")
        unknown = sorted(set(machine) - set(MACHINE_KEYS))
        if unknown:
            raise ConfigError(
                f"{FILE}: unknown key {', '.join('machines.' + key for key in unknown)}"
            )
        name = machine.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{FILE}: machines.name must be a name, not {json.dumps(name)}")
        address = machine.get("address")
        if "address" in machine and not (isinstance(address, str) and _ADDRESS.fullmatch(address)):
            raise ConfigError(
                f"{FILE}: machines.address must be what ssh reaches a machine at, "
                f"not {json.dumps(address)}"
            )
        machines.append({key: machine[key] for key in MACHINE_KEYS if key in machine})
    if len({machine["name"] for machine in machines}) != len(machines):
        raise ConfigError(f"{FILE}: machines must name each machine once")
    return machines


def _areas(given: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(given, dict):
        raise ConfigError(f"{FILE}: hunts.areas must be an object of area names to screens")
    areas: dict[str, tuple[str, ...]] = {}
    for area, screens in given.items():
        if not _AREA.fullmatch(area):
            raise ConfigError(
                f"{FILE}: hunts.areas: {json.dumps(area)} is not an area name: small letters, "
                "digits and dashes"
            )
        if (
            not isinstance(screens, list)
            or not screens
            or not all(isinstance(screen, str) and _SCREEN.fullmatch(screen) for screen in screens)
        ):
            raise ConfigError(
                f"{FILE}: hunts.areas.{area} must be a list of screens, each a path that starts "
                "with / and has no space, comma, quotation mark, dollar, backtick or backslash"
            )
        areas[area] = tuple(screens)
    return areas


def _suites(given: Any) -> tuple[Suite, ...]:
    where = "cloud_pre_push.suites"
    if not isinstance(given, dict):
        raise ConfigError(f"{FILE}: {where} must be an object")
    suites = []
    for name, suite in given.items():
        if not _SUITE_NAME.fullmatch(name):
            raise ConfigError(
                f"{FILE}: {where}: a suite's name is letters, digits, - and _, not {name}"
            )
        if not isinstance(suite, dict):
            raise ConfigError(f"{FILE}: {where}.{name} must be an object")
        unknown = sorted(set(suite) - set(SUITE_KEYS))
        if unknown:
            raise ConfigError(
                f"{FILE}: unknown key {', '.join(f'{where}.{name}.{key}' for key in unknown)}"
            )
        for key in ("select", "run", "part", "probe"):
            if key in ("part", "probe") and key not in suite:
                continue
            value = suite.get(key)
            try:
                words = shlex.split(value) if isinstance(value, str) else []
            except ValueError:
                words = []
            if not words:
                raise ConfigError(f"{FILE}: {where}.{name}.{key} must be a command")
            # The part is arguments only; the others name a program, as a command does.
            script = None if key == "part" else _script(words)
            if script is not None:
                _program(words[script], f"{where}.{name}.{key}")
        workers = suite.get("workers", SUITE_WORKERS.get(name, 1))
        if type(workers) is not int or workers < 1:
            raise ConfigError(f"{FILE}: {where}.{name}.workers must be a whole number above 0")
        part = suite.get("part")
        if part is not None and not ("{shard}" in part and "{shards}" in part):
            raise ConfigError(f"{FILE}: {where}.{name}.part must name {{shard}} and {{shards}}")
        suites.append(
            Suite(name, suite["select"], suite["run"], workers, part, suite.get("probe"))
        )
    return tuple(suites)


def _relative(value: str, name: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ConfigError(f"{FILE}: {name} must be a path inside the checkout, not {value}")
    return value


# Each command keeps its documented answer; a form only translates how it is obtained.
_FUNCTIONS = {
    "mechanical_conflicts": {"resolve", "renumber", "taken"},
    "advisory_watch": {"unreachable", "answers"},
    "push_checkout": {"install", "refusal"},
    "browser_plan": {"browser_plan"},
}
_EXIT_COMMANDS = {
    "test", "browser", "pre_train_checks", "hunt_app", "hunt_browser", "hunt_cursor", "push_checks",
}
_SERVER_FIELDS = {"whole", "tests", "total", "reasons"}


def _command_entry(entry: dict[str, Any], name: str) -> dict[str, Any]:
    where = f"commands.{name}"
    form = entry.get("form")
    allowed = {"lines"}
    if name in _EXIT_COMMANDS:
        allowed.add("exit")
    if name in _FUNCTIONS:
        allowed.add("functions")
    if name in ("browser_tests_for_changes", "server_tests_for_changes"):
        allowed.add("json")
    if not isinstance(form, str) or form not in allowed:
        raise ConfigError(f"{FILE}: {where} does not support form {form!r}")
    keys = {"run", "form", "args"}
    if form == "functions":
        keys.add("functions")
    elif form == "json":
        keys.add("field" if name == "browser_tests_for_changes" else "fields")
    unknown = sorted(set(entry) - keys)
    if unknown:
        raise ConfigError(f"{FILE}: unknown key {where}.{unknown[0]}")
    line = entry.get("run")
    if not isinstance(line, str) or not line.strip():
        raise ConfigError(f"{FILE}: {where}.run must be a command")
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(
        isinstance(word, str) and "\0" not in word for word in args
    ):
        raise ConfigError(f"{FILE}: {where}.args must be a list of words")
    if form == "functions":
        _program(line, where)
        functions = entry.get("functions")
        if (
            not isinstance(functions, dict)
            or set(functions) != _FUNCTIONS[name]
            or not all(
                isinstance(value, str) and value.isidentifier() for value in functions.values()
            )
        ):
            raise ConfigError(
                f"{FILE}: {where}.functions must map "
                f"{', '.join(sorted(_FUNCTIONS[name]))} to function names"
            )
        if args:
            raise ConfigError(f"{FILE}: {where}.args is only for command lines")
    elif form == "json":
        if name == "browser_tests_for_changes":
            field = entry.get("field")
            if not isinstance(field, str) or not field:
                raise ConfigError(f"{FILE}: {where}.field must name the specs field")
        else:
            fields = entry.get("fields")
            if (
                not isinstance(fields, dict)
                or set(fields) != _SERVER_FIELDS
                or not all(isinstance(value, str) and value for value in fields.values())
            ):
                raise ConfigError(
                    f"{FILE}: {where}.fields must map whole, tests, total, reasons to field names"
                )
    if form != "functions" and name not in ("test", "browser"):
        _words(line, name)
    return entry.copy()


def _function_file(
    main: Path, install_folder: Path | None, config_file: Path, name: str, entry: dict[str, Any]
) -> None:
    # Parse, never import: configuration is also read in planning and shadow runs.
    where = f"{FILE}: commands.{name}"
    path = resolve_file(entry["run"], main, install_folder, config_file)
    root = main if path == main / entry["run"] else install_folder
    assert root is not None
    try:
        path.resolve().relative_to(root.resolve())
        tree = ast.parse(path.read_bytes(), filename=entry["run"])
    except (OSError, SyntaxError, ValueError):
        raise ConfigError(
            f"{where} cannot read a Python module inside the checkout or installation folder: {entry['run']}"
        ) from None
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for function in entry["functions"].values():
        if function not in defined:
            raise ConfigError(f"{where} module does not define function {function}")


def _program(value: str, name: str) -> str:
    _relative(value.removeprefix("@install/"), name)
    return value


def _words(line: str, name: str) -> list[str]:
    """A command line split into words as the shell splits them, with nothing expanded."""
    where = f"commands.{name}"
    try:
        words = shlex.split(line)
    except ValueError as error:
        raise ConfigError(f"{FILE}: {where} cannot be split into words: {error}") from None
    if not words or not words[0] or any("\0" in word for word in words):
        raise ConfigError(f"{FILE}: {where} must be a command")
    script = _script(words)
    if script is not None:
        _program(words[script], where)
    return words


def _script(words: list[str]) -> int | None:
    """Which word is the repository's own program, read from its main checkout wherever the
    command runs, so a pull request's worktree never supplies it: a first word with a folder
    in it, or, after a program found on the PATH (an interpreter), the first word with a folder
    in it that is no option. None when every word is found elsewhere."""
    if not words:
        return None
    if "/" in words[0]:
        return 0
    return next(
        (index for index, word in enumerate(words) if "/" in word and not word.startswith("-")),
        None,
    )


def _plans(value: str | None) -> str | None:
    if value is not None and not (
        _PLANS.fullmatch(value) and _relative(value, "hunts.plans") == value
    ):
        raise ConfigError(
            f"{FILE}: hunts.plans must be a path inside the checkout of letters, digits, dots, "
            f"dashes and underscores, not {value}"
        )
    return value


def _patterns(values: list[str], name: str) -> tuple[str, ...]:
    for value in values:
        try:
            re.compile(value)
        except re.error as error:
            raise ConfigError(
                f"{FILE}: {name} must be regular expressions, not {json.dumps(value)}: {error}"
            ) from None
    return tuple(values)


def _from_runner_settings(main: Path) -> str | None:
    """The `repository` Bash reads from the runner settings, as `tick` and the Codex worker's
    `gh` read it when they sourced the file: in a `bash` of its own, with only PATH and HOME,
    for at most RUNNER_SETTINGS_SECONDS. None when the file names no `repository`."""
    path = main / RUNNER_SETTINGS
    try:
        text = path.read_text()
    except OSError:
        return None
    if not re.search(r"\brepository=", text):
        return None
    refused = f"{RUNNER_SETTINGS} names a repository that Bash cannot read from it"
    try:
        done = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1" >/dev/null 2>&1; printf %s "${repository-}"',
                "settings",
                str(path),
            ],
            env={key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ},
            capture_output=True,
            text=True,
            timeout=RUNNER_SETTINGS_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConfigError(
            f"{refused}: it ran for more than {RUNNER_SETTINGS_SECONDS} seconds"
        ) from None
    if done.returncode or not done.stdout:
        raise ConfigError(f"{refused}: it read {done.stdout!r} and exited {done.returncode}")
    return done.stdout


def runner_labels(label: str, planner_label: str | None = None) -> tuple[str, ...]:
    """The labels every machine's runners carry, so GitHub gives a job to whichever machine has
    one free: the base label, a spare's before it takes a slot, the merge train's first
    member's, main's runners', the merge line's lane on them, the Docker runners' and the
    planner's, `<label>-planner` unless the file names another."""
    return (
        label,
        f"{label}-standby",
        f"{label}-first",
        f"{label}-main",
        f"{label}-main-batch",
        f"{label}-docker",
        planner_label or f"{label}-planner",
    )


def _from_origin(main: Path) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(main), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
    )
    url = done.stdout.strip().removesuffix("/").removesuffix(".git")
    # `git clone https://name@github.com/...` keeps the user name in the URL.
    found = re.fullmatch(
        r"(?:https://(?:[^/@]+@)?|ssh://git@|git@)github\.com[:/]([^/]+/[^/]+)", url
    )
    return found[1] if done.returncode == 0 and found else None


@dataclass(frozen=True)
class ConfigFile:
    path: Path
    given: dict[str, Any]
    install_folder: Path | None
    default_repository: str | None
    repository_source: str


def _default_repository(main: Path) -> tuple[str | None, str]:
    repository, source = _from_runner_settings(main), RUNNER_SETTINGS
    if repository is None:
        repository, source = _from_origin(main), "origin"
    if repository is not None and not _REPOSITORY.fullmatch(repository):
        raise ConfigError(f"{source}: repository must be owner/name, not {repository}")
    return repository, source


def read_config(main: Path) -> ConfigFile:
    """Select one file, never merge files. An explicit path must be readable even if missing.

    Parity shares this lookup but validates only its own section of the selected document.
    """
    explicit = os.environ.get("SHIPLINE_CONFIG")
    repository, source = None, "origin"
    installed = bool(explicit)
    path = Path(os.path.abspath(Path(explicit).expanduser())) if explicit else main / FILE
    if not explicit:
        repository, source = _default_repository(main)
        if repository is not None:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            candidate = base.expanduser().absolute() / "shipline" / repository.replace("/", "-") / FILE
            # lstat also selects a dangling link; an unreadable installation never falls back.
            try:
                candidate.lstat()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise ConfigError(f"{candidate}: cannot be read: {error}") from None
            else:
                path, installed = candidate, True
    try:
        text = path.read_text()
    except FileNotFoundError as error:
        if installed or os.path.lexists(path):
            raise ConfigError(f"{path}: cannot be read: {error}") from None
        text = "{}"
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"{path}: cannot be read: {error}") from None
    try:
        given = json.loads(text)
    except ValueError as error:
        raise ConfigError(f"{path} is not JSON: {error}") from None
    if not isinstance(given, dict):
        raise ConfigError(f"{path}: the file must be an object")
    folder = path.parent if installed else None
    if "install_folder" in given:
        named = given["install_folder"]
        if not isinstance(named, str) or not named.strip() or "\0" in named:
            raise ConfigError(f"{path}: install_folder must be a path")
        folder = Path(os.path.abspath(path.parent / Path(named).expanduser()))
    return ConfigFile(path, given, folder, repository, source)


def load(start: Path | None = None) -> Config:
    """The configuration of the repository `start` is in; without one, of the repository
    Shipline works on (`repository_checkout`)."""
    main = repository_checkout() if start is None else main_checkout(start)
    document = read_config(main)
    with _file_errors(document.path):
        return _load(main, start, document)


def _load(main: Path, start: Path | None, document: ConfigFile) -> Config:
    given = document.given
    values = _merged(DEFAULTS, given, "")
    groups = values["checks"]["prefix_only"]
    if len(set(groups)) != len(groups) or set(groups) - set(_configured(Checks, values["checks"]).groups()):
        raise ConfigError(
            f"{FILE}: checks.prefix_only must name distinct groups: images, browser_whole, server_whole"
        )
    # Where the repository was read, so a refusal names that place.
    repository, source = values["repository"], str(document.path)
    if not repository:
        repository, source = document.default_repository, document.repository_source
        if repository is None:
            repository, source = _default_repository(main)
    if len(set(values["labels"]["priority"].values())) != 4:
        raise ConfigError(f"{FILE}: labels.priority must name four different labels")
    if repository is not None and not _REPOSITORY.fullmatch(repository):
        raise ConfigError(f"{source}: repository must be owner/name, not {repository}")
    prefix = values["runner"]["prefix"]
    if prefix is not None and not _PREFIX.fullmatch(prefix):
        raise ConfigError(
            f"{FILE}: runner.prefix must be at most 25 lowercase letters, digits and hyphens, "
            f"starting with a letter and ending with a letter or digit, not {prefix}"
        )
    label = values["runner"]["label"] or (prefix and f"{prefix}-home")
    planner_label = values["runner"]["planner_label"] or (label and f"{label}-planner")
    for key, value in (("label", label), ("planner_label", planner_label)):
        if value is not None and not _LABEL.fullmatch(value):
            raise ConfigError(
                f"{FILE}: runner.{key} must be lowercase letters, digits and hyphens, starting "
                f"and ending with a letter or digit, not {value}"
            )
    labels = tuple(values["runner"]["labels"])
    if "labels" not in given.get("runner", {}) and label is not None:
        labels = runner_labels(label, planner_label)
    scratch_prefix = values["merge"]["scratch_prefix"]
    if not _SCRATCH_PREFIX.fullmatch(scratch_prefix):
        raise ConfigError(
            f"{FILE}: merge.scratch_prefix must be the beginning of a branch name, ended by "
            f"- or /, not {scratch_prefix}"
        )
    for name in RUN_AS_WORDS:
        entry = values["commands"][name]
        if isinstance(entry, dict):
            if entry["form"] == "functions":
                _function_file(main, document.install_folder, document.path, name, entry)
            else:
                _words(entry["run"], name)
        elif entry is not None:
            _words(entry, name)
    refresh = Refresh(tuple(values["refresh"]["clear_generated"]), document.path)
    if refresh.clear_generated:
        refresh.targets(main)
        working = start or Path.cwd()
        try:
            if main_checkout(working) != main:
                working = main
        except ConfigError:
            working = main
        active = _generated_git(working, "rev-parse", "--show-toplevel")
        if active.returncode:
            raise ConfigError(f"{FILE}: refresh.clear_generated cannot find the worktree")
        refresh.targets(Path(active.stdout.strip()))
    config = _configured(
        Config, values,
        found_repository=repository,
        refresh=refresh,
        runner=_configured(Runner, values["runner"], labels=labels,
                           label=label, planner_label=planner_label),
        parity=parity_of(given),
        machines=tuple(Machine(**machine) for machine in values["machines"]),
        conflict_review=ConflictReview(**values["conflict_review"])
        if values["conflict_review"] else None,
        given=given,
        main=main,
        path=document.path,
        install_folder=document.install_folder,
    )
    # Resolve now as well as at the call site: an impossible @install/ is a config refusal.
    for name in RUN_AS_WORDS:
        config.command(name)
    for suite in config.cloud_pre_push.suites:
        for line in (suite.select, suite.run, suite.probe):
            if line is not None:
                config.words(line)
    if config.parity.known is not None:
        config.file(config.parity.known)
    return config


def parity_of(given: Any) -> Parity:
    """The `parity` section of a file as read, checked as `load` checks it. `tools/parity` reads
    only this of another repository's file, whose other keys its own scripts may read."""
    if not isinstance(given, dict):
        raise ConfigError(f"{FILE}: the file must be an object")
    only = {"parity": given.get("parity", {})}
    values = _merged({"parity": DEFAULTS["parity"]}, only, "")["parity"]
    # As git names a path: `./tests//flow/` is `tests/flow`.
    named = {
        key: None if value is None else str(PurePosixPath(
            (_program if key == "known" else _relative)(value, f"parity.{key}")
        ))
        for key, value in values.items()
    }
    return _configured(Parity, named)


@cache
def current() -> Config:
    """The configuration of the repository Shipline works on, read once a run."""
    return load()


def as_dict(config: Config) -> dict[str, Any]:
    """Every value, defaults included, in the form of the file."""
    return _as_values(config)


def _shell_fields(
    config: Any, cls: type | None = None, root: Config | None = None,
) -> Iterator[tuple[int, str, Any]]:
    cls = cls or type(config)
    root = root or config
    for item in fields(cls):
        value = config[item.name] if isinstance(config, dict) else getattr(config, item.name)
        if setting := item.metadata.get("setting"):
            if setting.shell is None:
                continue
            position, name = setting.shell
            if setting.kind == "command":
                value = root.command(item.name) or []
            elif setting.kind == "areas":
                yield position + 1, name.removesuffix("_AREAS") + "_SCREENS", [
                    ", ".join(paths) for paths in value.values()
                ]
                value = list(value)
            yield position, name, value
            if setting.derived_shell:
                position, name, attribute = setting.derived_shell
                yield position, name, getattr(config, attribute)
        elif "section" in item.metadata:
            yield from _shell_fields(value, item.metadata["section"] or item.type, root)


def shell(config: Config, *, posix: bool = False) -> str:
    """`KEY=value` lines for a Bash script's `eval`: every value, and the file for jq. The
    repository is empty when nothing names it, and a script that needs it stops on that. With
    `posix`, for a plain sh (`workflow_config.py sh`): each list is one value, its names quoted
    for sh and joined by spaces, as Bash would write its array."""
    def listed(name: str, items: Iterable[str]) -> str:
        words = " ".join(shlex.quote(item) for item in items)
        return f"{name}={shlex.quote(words)}" if posix else f"{name}=({words})"

    values = list(_shell_fields(config)) + [
        (31, "CONFIG", str(config.path) if os.path.lexists(config.path) else ""),
        (32, "CONFIG_JSON", json.dumps(config.given, separators=(",", ":"))),
        (33, "INSTALL_FOLDER", str(config.install_folder) if config.install_folder else ""),
    ]
    # Clear an inherited empty export too. A discovered checkout file must not become an
    # explicit external config for a child, which would invent an installation folder.
    lines = ["unset SHIPLINE_CONFIG"]
    for _, key, value in sorted(values):
        name = f"SHIPLINE_{key}"
        if isinstance(value, (list, tuple)):
            lines.append(listed(name, value))
        else:
            lines.append(f"{name}={shlex.quote(str(value) if value is not None else '')}")
    # The main checkout, exported: every program the script starts works on the same repository,
    # whatever folder it starts in.
    lines.append(f"export SHIPLINE_REPO={shlex.quote(str(config.main))}")
    lines.append("export SHIPLINE_CONFIG_JSON SHIPLINE_INSTALL_FOLDER")
    if os.environ.get("SHIPLINE_CONFIG"):
        lines.append("export SHIPLINE_CONFIG")
    return "\n".join(lines) + "\n"


DOCS_COMMAND = "python3 scripts/workflow/workflow_config.py docs --write"
_CONFIG_MARKERS = ("<!-- shipline-config:start -->", "<!-- shipline-config:end -->")
_COMMAND_MARKERS = ("<!-- shipline-commands:start -->", "<!-- shipline-commands:end -->")


def _example(cls: type = Config) -> dict[str, Any]:
    """Declared key order, retaining the meaningful order of user-named maps."""
    result = {}
    for item in _declared_fields(cls):
        if setting := item.metadata.get("setting"):
            result[setting.name or item.name] = deepcopy(setting.example)
        elif "section" in item.metadata:
            result[item.name] = _example(item.metadata["section"] or item.type)
    return result


def _cell(text: str) -> str:
    # Escape even inside code spans: Markdown table parsers still split on literal pipes.
    return text.replace("|", "&#124;").replace("\n", " ")


def _replace_block(text: str, markers: tuple[str, str], generated: str) -> str:
    start, end = markers
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
        raise ConfigError(f"README.md: expected one ordered pair of {start} and {end}")
    before, after = text.split(start)
    _, after = after.split(end)
    return before + start + "\n" + generated + "\n" + end + after


def documentation(root: Path) -> dict[Path, str]:
    keys = ["| Key | Kind | What it names | Default |", "| --- | --- | --- | --- |"]
    commands = ["| Command | Called by, with | Answers | Without it |",
                "| --- | --- | --- | --- |"]
    for name, setting in _declarations():
        default = "none" if setting.default is None else "`" + json.dumps(
            setting.default, ensure_ascii=False
        ) + "`"
        keys.append(f"| `{name}` | {setting.kind} | {_cell(setting.meaning)} | {_cell(default)} |")
        if name.startswith("commands."):
            called, answers, without = setting.command_docs or (
                setting.meaning, "The command's output and exit code.", "No command is run."
            )
            commands.append("| `" + name.removeprefix("commands.") + "` | " + " | ".join(
                _cell(value) for value in (called, answers, without)
            ) + " |")
    readme = root / "scripts/workflow/README.md"
    text = _replace_block(readme.read_text(), _CONFIG_MARKERS, "\n".join(keys))
    text = _replace_block(text, _COMMAND_MARKERS, "\n".join(commands))
    return {root / "examples/shipline.json": json.dumps(_example(), indent=2, ensure_ascii=False) + "\n",
            readme: text}


def docs(*, write: bool = False) -> int:
    root = Path(__file__).resolve().parents[2]
    try:
        generated = documentation(root)
        stale = [path for path, text in generated.items()
                 if not path.exists() or path.read_text() != text]
        if write:
            for path in stale:
                path.write_text(generated[path])
        elif stale:
            for path in stale:
                print(f"{path.relative_to(root)} differs from the config declarations")
            print(f"Run {DOCS_COMMAND}")
            return 1
    except (ConfigError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    if sys.argv[1:] in (["docs"], ["docs", "--write"]):
        return docs(write=sys.argv[-1] == "--write")
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in ("shell", "sh", "json"):
        print("usage: workflow_config.py shell|sh|json [folder] | docs [--write]", file=sys.stderr)
        return 2
    try:
        config = load(Path(sys.argv[2]) if len(sys.argv) == 3 else None)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    if sys.argv[1] == "json":
        print(json.dumps(config.given))
    else:
        sys.stdout.write(shell(config, posix=sys.argv[1] == "sh"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
