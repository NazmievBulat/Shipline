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

The defaults are neutral. The repository is the `repository` Bash reads from
`.github/runner/settings.sh` when the runner pool names one, else the owner and name of the
`origin` remote on GitHub; the check names, annotation titles, workflow files and labels are the
ones Shipline's own CI and labels use; the state folder is `.local/workflow`; the worktree
set-up command, run only when present, is `scripts/setup-worktree`; the merge gate's scratch
branches begin `workflow/merge-batch-`; no runner prefix and no image build or hub cache
command, and the runner label `<prefix>-home` and planner label `<label>-planner`, whose labels
are the runner labels, main's runners' `<label>-main` among them; none without a prefix; the
workflow and runner tests `tools/parity` compares are in `tests/workflow` and `tests/runner`,
and it knows of no differences; no machines named, so `flow-page` samples only the machine it
runs on; no hunt areas, so one hunt runs at a time, and no hunt plans file; no test command,
which a repository names for its CI's test job (`ci_proof.py test`), and no browser command,
which it names for its E2E's browser job (`ci_proof.py browser`); a question to the owner marked
`Owner only: no` gets its recommended reply after 15 minutes; a quiet cloud worker is woken
after 30 minutes; no paths that call for a closer review.

`review.high_effort_paths` are regular expressions, each searched for in every path a pull
request changes: a match runs its independent reviewer at high effort, not medium
(`review-effort`). A repository names there what it stores, what its models read, its
background work, locks and queues, and its account boundaries and security.

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
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

FILE = "shipline.json"
# Where the runner pool names the repository its runners register with, and how long Bash may
# take to read it.
RUNNER_SETTINGS = ".github/runner/settings.sh"
RUNNER_SETTINGS_SECONDS = 5
DEFAULT_DEPENDENCY_JOB = "CI: dependencies"
# The prefix names a systemd unit and, as `<prefix>-runner`, a user of at most 32 characters.
_PREFIX = re.compile(r"[a-z](?:[a-z0-9-]{0,23}[a-z0-9])?")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")

# Every key the file may hold, with its default. The repository's default is found, not fixed.
DEFAULTS: dict[str, Any] = {
    "repository": None,
    "install_folder": None,
    "state_folder": ".local/workflow",
    "worktree_setup": "scripts/setup-worktree",
    "refresh": {"clear_generated": []},
    "commands": {
        # The command line that runs the repository's tests, from the root of its checkout, by
        # `bash -c` (`ci_proof.py test`); and the one that runs its browser tests, the same way
        # (`ci_proof.py browser`).
        "test": None,
        "browser": None,
        # The repository's own commands Shipline calls, each split into words (`Config.command`).
        "browser_tests_for_changes": None,
        "server_tests_for_changes": None,
        "pre_train_checks": None,
        "mechanical_conflicts": None,
        "browser_plan": None,
        "hunt_app": None,
        "hunt_browser": None,
        "hunt_cursor": None,
        "advisory_watch": None,
        "push_checkout": None,
        # The checks the repository's pre-push hook runs (`ci_tests_for_changes.py --run`).
        "push_checks": None,
    },
    "checks": {
        "ci_result": "CI result",
        "ci_plan": "CI: what to run",
        "e2e_plan": "E2E: what to run",
        # The prefix of every check of the image workflow.
        "images": "Docker images",
        "server_tests": "CI: server tests",
        "web": "CI: web",
        "older_version": "CI: older version on new tables",
        # The job that audits the dependencies against published advisories (the gate reads
        # "CI: dependencies" when none is named), and the title of the notice it writes on main
        # when advisories alone failed it. Without the notice an advisory on main makes main red,
        # as any other failure.
        "dependency_audit": None,
        "advisory_annotation": None,
        "prefix_only": [],
    },
    # The titles of the annotations the CI planners write and the merge gate reads.
    "annotations": {
        "merge_tested": "Merge tested",
        "files_tested": "Files tested",
        "server_tests": "Server tests",
        "web_suite": "Web suite",
        "browser_suite": "Browser suite",
    },
    "workflows": {
        "ci": "ci.yml", "e2e": "e2e.yml", "images": "images.yml",
        "ci_name": "CI", "images_name": "Images",
    },
    "labels": {
        "workflow": "workflow",
        "audited": "audited",
        "priority": {
            "blocker": "priority: blocker",
            "high": "priority: high",
            "medium": "priority: medium",
            "low": "priority: low",
        },
    },
    "runner": {
        # The runner labels of the machines the gate may re-run failed jobs on; when the file
        # names none, they are built from the base label (`runner_labels`).
        "labels": [],
        "identity_folder": "~/.config/shipline-runner",
        # The prefix the runner pool names everything by: its images, network, services and
        # containers (`<prefix>-runner…`) and labels; without it, there are no runner figures
        # (`workflow_metrics.py`).
        "prefix": None,
        # The base label every machine's runners carry; `<prefix>-home` by default.
        "label": None,
        # The label the CI planner's runner carries; `<label>-planner` by default.
        "planner_label": None,
        # The command line, run from the main checkout, that builds the runner images; without
        # it, the pool has no images to run.
        "image_build": None,
        # The command line, run from the main checkout, that runs the Docker Hub cache of the
        # Docker-capable runners' virtual machines (`hub-cache.sh`); without it, they pull from
        # Docker Hub directly.
        "hub_cache": None,
    },
    "merge": {
        # The merge gate's own branches, each one prefix's test merge: this, then a random name.
        # The gate deletes them once no prefix needs them, and the example workflows run its
        # first run of a prefix there in the merge line's lane on main's runners (examples/ci.yml).
        "scratch_prefix": "workflow/merge-batch-",
    },
    # What `tools/parity` reads of a repository that keeps its own copy of the scripts: where its
    # workflow and runner tests are, and the file that lists the differences it expects.
    "parity": {"workflow_tests": "tests/workflow", "runner_tests": "tests/runner", "known": None},
    # The machines `flow-page` samples, each a name and, for another machine, the address `ssh`
    # reaches it at; one without an address is this machine. None named: only this machine.
    "machines": [],
    "hunts": {
        # Each area of the application a hunt may keep to: its name and the screens (paths) it
        # covers. With none, a hunt hunts every screen and runs alone (`session start hunt`).
        "areas": {},
        # The repository's file of hunt plans, which a hunt with a plan is told of.
        "plans": None,
    },
    # Path patterns of the few files many tickets edit (`*` stays inside a folder, `**` crosses
    # folders): two tickets on one never run at once, and the merge line serves its members
    # back to back.
    "hot_paths": [],
    # The reviewer of a small conflict on a handed-off pull request, `{"effort": ...,
    # "model": ..., "max_changed_lines": N}`: None is the usual one, Opus at high effort.
    "conflict_review": None,
    "review": {"high_effort_paths": []},
    "merge_train": {"queued_limit_minutes": 20},
    # Minutes a question to the owner marked `Owner only: no` waits before the tick types its
    # recommended reply; 0 turns that off.
    "answer_for_owner_after_minutes": 15,
    # Minutes without a push or a report after which the tick wakes a cloud worker; 0 never does.
    "cloud_wake_after_minutes": 30,
    # The suites a cloud worker's push runs as CI will (`ci_tests_for_changes.py`), by name.
    # Each is read by `_suites`, not key by key: the names are the repository's own.
    "cloud_pre_push": {"suites": {}},
}
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
class CheckGroup:
    workflow: str
    jobs: tuple[str, ...]
    suite: str | None = None

    def contains(self, name: str) -> bool:
        # Images is a whole workflow; its repository chooses the build and test job names.
        return any(name == job or name.startswith(job + " (") or
                   (job.endswith(":") and name.startswith(job + " ")) for job in self.jobs)


@dataclass(frozen=True)
class Checks:
    ci_result: str
    ci_plan: str
    e2e_plan: str
    images: str
    dependency_audit: str | None = None
    advisory_annotation: str | None = None
    prefix_only: list[str] = field(default_factory=list)
    server_tests: str = DEFAULTS["checks"]["server_tests"]
    web: str = DEFAULTS["checks"]["web"]
    older_version: str = DEFAULTS["checks"]["older_version"]

    @property
    def dependency_job(self) -> str:
        return self.dependency_audit or DEFAULT_DEPENDENCY_JOB

    def groups(self) -> dict[str, CheckGroup]:
        return {
            "images": CheckGroup("images", (f"{self.images}:",)),
            "browser_whole": CheckGroup("e2e", ("playwright",), "browser_suite"),
            "server_whole": CheckGroup("ci", (self.server_tests,), "server_tests"),
        }

    @property
    def images_plan(self) -> str:
        return f"{self.images}: what to run"


@dataclass(frozen=True)
class Annotations:
    merge_tested: str
    files_tested: str
    server_tests: str
    web_suite: str
    browser_suite: str


@dataclass(frozen=True)
class Workflows:
    ci: str
    e2e: str
    images: str
    ci_name: str
    images_name: str


@dataclass(frozen=True)
class Labels:
    workflow: str
    audited: str
    # Most urgent first: blocker, high, medium, low.
    priority: dict[str, str]


@dataclass(frozen=True)
class Runner:
    labels: tuple[str, ...]
    identity_folder: str
    prefix: str | None = None
    label: str | None = None
    planner_label: str | None = None
    image_build: str | None = None
    hub_cache: str | None = None

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


@dataclass(frozen=True)
class Merge:
    scratch_prefix: str


@dataclass(frozen=True)
class Parity:
    workflow_tests: str
    runner_tests: str
    known: str | None = None


@dataclass(frozen=True)
class Machine:
    name: str
    # None for this machine.
    address: str | None = None


@dataclass(frozen=True)
class Hunts:
    # In the order the file names them.
    areas: dict[str, tuple[str, ...]]
    plans: str | None = None


@dataclass(frozen=True)
class Refresh:
    clear_generated: tuple[str, ...]
    config_file: Path | str = FILE

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


@dataclass(frozen=True)
class Commands:
    # Each None when the file names none.
    test: str | dict[str, Any] | None
    browser: str | dict[str, Any] | None
    browser_tests_for_changes: str | dict[str, Any] | None
    server_tests_for_changes: str | dict[str, Any] | None
    pre_train_checks: str | dict[str, Any] | None
    mechanical_conflicts: str | dict[str, Any] | None
    browser_plan: str | dict[str, Any] | None
    hunt_app: str | dict[str, Any] | None
    hunt_browser: str | dict[str, Any] | None
    hunt_cursor: str | dict[str, Any] | None
    advisory_watch: str | dict[str, Any] | None
    push_checkout: str | dict[str, Any] | None
    push_checks: str | dict[str, Any] | None


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


@dataclass(frozen=True)
class CloudPrePush:
    # In the order the file names them.
    suites: tuple[Suite, ...] = ()


# The commands run as words, not by a shell: every one but `test` and `browser`, the command
# lines CI's jobs run by `bash -c` (`ci_proof.py`).
RUN_AS_WORDS = tuple(name for name in DEFAULTS["commands"] if name not in ("test", "browser"))


@dataclass(frozen=True)
class Review:
    high_effort_paths: tuple[str, ...]


@dataclass(frozen=True)
class ConflictReview:
    effort: str
    model: str
    # A resolution larger than this, in lines of the conflicting files, gets the usual reviewer.
    max_changed_lines: int


@dataclass(frozen=True)
class MergeTrain:
    queued_limit_minutes: int


@dataclass(frozen=True)
class Config:
    # None when nothing names it: an error only for a script that needs it.
    found_repository: str | None
    state_folder: str
    worktree_setup: str
    refresh: Refresh
    checks: Checks
    annotations: Annotations
    workflows: Workflows
    labels: Labels
    runner: Runner
    merge: Merge
    parity: Parity
    machines: tuple[Machine, ...]
    hunts: Hunts
    commands: Commands
    hot_paths: tuple[str, ...]
    # None when the file names none: the usual conflict reviewer.
    conflict_review: ConflictReview | None
    review: Review
    merge_train: MergeTrain
    answer_for_owner_after_minutes: int
    cloud_wake_after_minutes: int
    cloud_pre_push: CloudPrePush
    # The file as it was read, without the defaults: `{}` when there is none.
    given: dict[str, Any]
    main: Path
    path: Path
    install_folder: Path | None

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
        elif where == "commands." and isinstance(given[key], dict):
            merged[key] = _command_entry(given[key], key)
        elif name in _READERS:
            merged[key] = _READERS[name](given[key], name)
        elif name == "machines":
            merged[key] = _machines(given[key])
        elif name == "hunts.areas":
            merged[key] = _areas(given[key])
        elif name == "cloud_pre_push.suites":
            merged[key] = _suites(given[key])
        elif isinstance(default, dict):
            merged[key] = _merged(default, given[key], name + ".")
        elif isinstance(default, list):
            value = given[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ConfigError(f"{FILE}: {name} must be a list of names")
            merged[key] = value
        elif isinstance(default, int):
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


# The keys whose values are read by a reader of their own, by their full name.
_READERS = {"hot_paths": _hot_paths, "conflict_review": _conflict_review}


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
    if len(set(groups)) != len(groups) or set(groups) - set(Checks("", "", "", "").groups()):
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
    config = Config(
        found_repository=repository,
        state_folder=_relative(values["state_folder"], "state_folder"),
        worktree_setup=_relative(values["worktree_setup"], "worktree_setup"),
        refresh=refresh,
        checks=Checks(**values["checks"]),
        annotations=Annotations(**values["annotations"]),
        workflows=Workflows(**values["workflows"]),
        labels=Labels(**values["labels"]),
        runner=Runner(
            labels=labels,
            identity_folder=values["runner"]["identity_folder"],
            prefix=values["runner"]["prefix"],
            label=label,
            planner_label=planner_label,
            image_build=values["runner"]["image_build"],
            hub_cache=values["runner"]["hub_cache"],
        ),
        merge=Merge(scratch_prefix=scratch_prefix),
        parity=parity_of(given),
        machines=tuple(Machine(**machine) for machine in values["machines"]),
        hunts=Hunts(areas=values["hunts"]["areas"], plans=_plans(values["hunts"]["plans"])),
        commands=Commands(**values["commands"]),
        hot_paths=tuple(values["hot_paths"]),
        conflict_review=ConflictReview(**values["conflict_review"])
        if values["conflict_review"]
        else None,
        review=Review(
            high_effort_paths=_patterns(
                values["review"]["high_effort_paths"], "review.high_effort_paths"
            )
        ),
        merge_train=MergeTrain(**values["merge_train"]),
        answer_for_owner_after_minutes=values["answer_for_owner_after_minutes"],
        cloud_wake_after_minutes=values["cloud_wake_after_minutes"],
        cloud_pre_push=CloudPrePush(tuple(values["cloud_pre_push"]["suites"])),
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
    return Parity(**named)


@cache
def current() -> Config:
    """The configuration of the repository Shipline works on, read once a run."""
    return load()


def as_dict(config: Config) -> dict[str, Any]:
    """Every value, defaults included, in the form of the file."""
    return {
        "repository": config.repository,
        "install_folder": config.given.get("install_folder"),
        "state_folder": config.state_folder,
        "worktree_setup": config.worktree_setup,
        "refresh": {"clear_generated": list(config.refresh.clear_generated)},
        "checks": vars(config.checks).copy(),
        "annotations": vars(config.annotations).copy(),
        "workflows": vars(config.workflows).copy(),
        "labels": {
            "workflow": config.labels.workflow,
            "audited": config.labels.audited,
            "priority": dict(config.labels.priority),
        },
        "runner": {
            "labels": list(config.runner.labels),
            "identity_folder": config.runner.identity_folder,
            "prefix": config.runner.prefix,
            "label": config.runner.label,
            "planner_label": config.runner.planner_label,
            "image_build": config.runner.image_build,
            "hub_cache": config.runner.hub_cache,
        },
        "merge": vars(config.merge).copy(),
        "parity": vars(config.parity).copy(),
        "machines": [
            {"name": machine.name, **({"address": machine.address} if machine.address else {})}
            for machine in config.machines
        ],
        "hunts": {
            "areas": {area: list(screens) for area, screens in config.hunts.areas.items()},
            "plans": config.hunts.plans,
        },
        "commands": vars(config.commands).copy(),
        "hot_paths": list(config.hot_paths),
        "conflict_review": vars(config.conflict_review).copy() if config.conflict_review else None,
        "review": {"high_effort_paths": list(config.review.high_effort_paths)},
        "merge_train": vars(config.merge_train).copy(),
        "answer_for_owner_after_minutes": config.answer_for_owner_after_minutes,
        "cloud_wake_after_minutes": config.cloud_wake_after_minutes,
        "cloud_pre_push": {
            "suites": {
                suite.name: {
                    key: getattr(suite, key)
                    for key in SUITE_KEYS
                    if getattr(suite, key) is not None
                }
                for suite in config.cloud_pre_push.suites
            }
        },
    }


def shell(config: Config, *, posix: bool = False) -> str:
    """`KEY=value` lines for a Bash script's `eval`: every value, and the file for jq. The
    repository is empty when nothing names it, and a script that needs it stops on that. With
    `posix`, for a plain sh (`workflow_config.py sh`): each list is one value, its names quoted
    for sh and joined by spaces, as Bash would write its array."""
    values = {
        "REPOSITORY": config.found_repository or "",
        "STATE_FOLDER": config.state_folder,
        "WORKTREE_SETUP": config.worktree_setup,
        "CI_RESULT": config.checks.ci_result,
        "CI_PLAN": config.checks.ci_plan,
        "E2E_PLAN": config.checks.e2e_plan,
        "IMAGES": config.checks.images,
        "IMAGES_PLAN": config.checks.images_plan,
        **{f"ANNOTATION_{key.upper()}": value for key, value in vars(config.annotations).items()},
        **{f"WORKFLOW_{key.upper()}": value for key, value in vars(config.workflows).items()},
        "LABEL_WORKFLOW": config.labels.workflow,
        "LABEL_AUDITED": config.labels.audited,
        **{f"PRIORITY_{key.upper()}": value for key, value in config.labels.priority.items()},
        "IDENTITY_FOLDER": config.runner.identity_folder,
        # Empty when the file names none.
        "RUNNER_PREFIX": config.runner.prefix or "",
        "RUNNER_LABEL": config.runner.label or "",
        "RUNNER_PLANNER_LABEL": config.runner.planner_label or "",
        "RUNNER_IMAGE_BUILD": config.runner.image_build or "",
        "RUNNER_HUB_CACHE": config.runner.hub_cache or "",
        "HUNT_PLANS": config.hunts.plans or "",
        "ANSWER_FOR_OWNER_AFTER_MINUTES": str(config.answer_for_owner_after_minutes),
        "CONFIG": str(config.path) if os.path.lexists(config.path) else "",
        "CONFIG_JSON": json.dumps(config.given, separators=(",", ":")),
        "INSTALL_FOLDER": str(config.install_folder) if config.install_folder else "",
    }
    # Clear an inherited empty export too. A discovered checkout file must not become an
    # explicit external config for a child, which would invent an installation folder.
    lines = ["unset SHIPLINE_CONFIG"]
    lines += [f"SHIPLINE_{key}={shlex.quote(value)}" for key, value in values.items()]

    def listed(name: str, items: Iterable[str]) -> str:
        words = " ".join(shlex.quote(item) for item in items)
        return f"{name}={shlex.quote(words)}" if posix else f"{name}=({words})"

    lines.append(listed("SHIPLINE_RUNNER_LABELS", config.runner.labels))
    # The hunt areas, and at the same place in the other array the screens of each, joined.
    areas = config.hunts.areas
    lines.append(listed("SHIPLINE_HUNT_AREAS", areas))
    lines.append(listed("SHIPLINE_HUNT_SCREENS", (", ".join(paths) for paths in areas.values())))
    # Each command as its words, empty when the repository has none.
    for name in RUN_AS_WORDS:
        lines.append(listed(f"SHIPLINE_COMMAND_{name.upper()}", config.command(name) or []))
    # The main checkout, exported: every program the script starts works on the same repository,
    # whatever folder it starts in.
    lines.append(f"export SHIPLINE_REPO={shlex.quote(str(config.main))}")
    lines.append("export SHIPLINE_CONFIG_JSON SHIPLINE_INSTALL_FOLDER")
    if os.environ.get("SHIPLINE_CONFIG"):
        lines.append("export SHIPLINE_CONFIG")
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in ("shell", "sh", "json"):
        print("usage: workflow_config.py shell|sh|json [folder]", file=sys.stderr)
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
