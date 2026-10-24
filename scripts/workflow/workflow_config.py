"""What differs between the repositories that use Shipline, read from one file.

A repository keeps `shipline.json` at the root of its main checkout: the main checkout of the
repository being worked on, found from the git common directory, so a worktree reads the same
file as the main checkout. A key the file leaves out keeps its default, key by key; a key the
file names that Shipline does not know, or a value of the wrong kind, is refused, so a typing
error never passes as a default.

The defaults are neutral. The repository is the `repository` Bash reads from
`.github/runner/settings.sh` when the runner pool names one, else the owner and name of the
`origin` remote on GitHub; the check names, annotation titles, workflow files and labels are the
ones Shipline's own CI and labels use; the state folder is `.local/workflow`; the worktree
set-up command, run only when present, is `scripts/setup-worktree`; no runner labels and no
runner prefix.

Python scripts import it (`current()`, once per run). A Bash script reads every value at once:

    config=$(python3 "$here/workflow_config.py" shell) || exit 1
    eval "$config"

which prints `SHIPLINE_<KEY>=value` lines quoted for `eval`, the file itself among them as
`SHIPLINE_CONFIG` for jq (`priority.jq`). Node (`codex-bridge.mjs`) and jq read the JSON with
the same defaults.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

HERE = Path(__file__).resolve().parent
FILE = "shipline.json"
# Where the runner pool names the repository its runners register with, and how long Bash may
# take to read it.
RUNNER_SETTINGS = ".github/runner/settings.sh"
RUNNER_SETTINGS_SECONDS = 5

# Every key the file may hold, with its default. The repository's default is found, not fixed.
DEFAULTS: dict[str, Any] = {
    "repository": None,
    "state_folder": ".local/workflow",
    "worktree_setup": "scripts/setup-worktree",
    "checks": {
        "ci_result": "CI result",
        "ci_plan": "CI: what to run",
        "e2e_plan": "E2E: what to run",
        # The prefix of every check of the image workflow.
        "images": "Docker images",
    },
    # The titles of the annotations the CI planners write and the merge gate reads.
    "annotations": {
        "merge_tested": "Merge tested",
        "files_tested": "Files tested",
        "server_tests": "Server tests",
        "web_suite": "Web suite",
        "browser_suite": "Browser suite",
    },
    "workflows": {"ci": "ci.yml", "e2e": "e2e.yml", "images": "images.yml"},
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
        # The runner labels of the machines the gate may re-run failed jobs on.
        "labels": [],
        "identity_folder": "~/.config/shipline-runner",
        # The prefix the runner pool names its services and containers by; without it, there are
        # no runner figures (`workflow_metrics.py`).
        "prefix": None,
    },
}

# `.` and `..` are no owner or name: `repos/../..` would leave the repository's own API.
_REPOSITORY = re.compile(r"(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+")


class ConfigError(Exception):
    """The file or the repository it is in cannot be read; nothing falls back to a guess."""


@dataclass(frozen=True)
class Checks:
    ci_result: str
    ci_plan: str
    e2e_plan: str
    images: str

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

    @property
    def identity_path(self) -> Path:
        return Path(self.identity_folder).expanduser()


@dataclass(frozen=True)
class Config:
    # None when nothing names it: an error only for a script that needs it.
    found_repository: str | None
    state_folder: str
    worktree_setup: str
    checks: Checks
    annotations: Annotations
    workflows: Workflows
    labels: Labels
    runner: Runner
    # The file as it was read, without the defaults: `{}` when there is none.
    given: dict[str, Any]
    main: Path

    @property
    def repository(self) -> str:
        if self.found_repository is None:
            raise ConfigError(
                f"cannot tell which repository the workflow works on: {FILE} names none, "
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


def main_checkout(start: Path = HERE) -> Path:
    done = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise ConfigError(f"{start} is not in a git repository: {done.stderr.strip()}")
    return Path(done.stdout.strip()).parent


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
        elif isinstance(default, dict):
            merged[key] = _merged(default, given[key], name + ".")
        elif isinstance(default, list):
            value = given[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ConfigError(f"{FILE}: {name} must be a list of names")
            merged[key] = value
        else:
            value = given[key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{FILE}: {name} must be a name, not {json.dumps(value)}")
            merged[key] = value
    return merged


def _relative(value: str, name: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ConfigError(f"{FILE}: {name} must be a path inside the checkout, not {value}")
    return value


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


def load(start: Path = HERE) -> Config:
    main = main_checkout(start)
    path = main / FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = "{}"
    except OSError as error:
        raise ConfigError(f"{FILE}: cannot be read: {error}") from error
    try:
        given = json.loads(text)
    except ValueError as error:
        raise ConfigError(f"{FILE} is not JSON: {error}") from error
    values = _merged(DEFAULTS, given, "")
    # Where the repository was read, so a refusal names that place.
    repository, source = values["repository"], FILE
    if not repository:
        repository, source = _from_runner_settings(main), RUNNER_SETTINGS
    if repository is None:
        repository, source = _from_origin(main), "origin"
    if len(set(values["labels"]["priority"].values())) != 4:
        raise ConfigError(f"{FILE}: labels.priority must name four different labels")
    if repository is not None and not _REPOSITORY.fullmatch(repository):
        raise ConfigError(f"{source}: repository must be owner/name, not {repository}")
    return Config(
        found_repository=repository,
        state_folder=_relative(values["state_folder"], "state_folder"),
        worktree_setup=_relative(values["worktree_setup"], "worktree_setup"),
        checks=Checks(**values["checks"]),
        annotations=Annotations(**values["annotations"]),
        workflows=Workflows(**values["workflows"]),
        labels=Labels(**values["labels"]),
        runner=Runner(
            labels=tuple(values["runner"]["labels"]),
            identity_folder=values["runner"]["identity_folder"],
            prefix=values["runner"]["prefix"],
        ),
        given=given,
        main=main,
    )


@cache
def current() -> Config:
    """The configuration of the repository these scripts are in, read once a run."""
    return load()


def as_dict(config: Config) -> dict[str, Any]:
    """Every value, defaults included, in the form of the file."""
    return {
        "repository": config.repository,
        "state_folder": config.state_folder,
        "worktree_setup": config.worktree_setup,
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
        },
    }


def shell(config: Config) -> str:
    """`KEY=value` lines for a Bash script's `eval`: every value, and the file for jq. The
    repository is empty when nothing names it, and a script that needs it stops on that."""
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
        "CONFIG": json.dumps(config.given, separators=(",", ":")),
    }
    lines = [f"SHIPLINE_{key}={shlex.quote(value)}" for key, value in values.items()]
    labels = " ".join(shlex.quote(label) for label in config.runner.labels)
    lines.append(f"SHIPLINE_RUNNER_LABELS=({labels})")
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) not in (2, 3) or sys.argv[1] != "shell":
        print("usage: workflow_config.py shell [folder]", file=sys.stderr)
        return 2
    try:
        config = load(Path(sys.argv[2]) if len(sys.argv) == 3 else HERE)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    sys.stdout.write(shell(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
