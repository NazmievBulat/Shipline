"""What differs between the repositories that use Shipline, read from one file.

A repository keeps `shipline.json` at the root of its main checkout: the main checkout of the
repository being worked on (`SHIPLINE_REPO`, else the repository of the current directory; never
found from where these scripts are), found from the git common directory, so a worktree reads the
same file as the main checkout. A key the file leaves out keeps its default, key by key; a key the
file names that Shipline does not know, or a value of the wrong kind, is refused, so a typing
error never passes as a default.

The defaults are neutral. The repository is the `repository` Bash reads from
`.github/runner/settings.sh` when the runner pool names one, else the owner and name of the
`origin` remote on GitHub; the check names, annotation titles, workflow files and labels are the
ones Shipline's own CI and labels use; the state folder is `.local/workflow`; the worktree
set-up command, run only when present, is `scripts/setup-worktree`; the merge gate's scratch
branches begin `workflow/merge-batch-`; no runner prefix and no image build or hub cache
command, and the runner label `<prefix>-home` and planner label `<label>-planner`, whose labels
are the runner labels, main's runners' `<label>-main` among them; none without a prefix; no
machines named, so `flow-page` samples only the machine it runs on; no hunt areas, so one hunt
runs at a time, and no hunt plans file; no test command, which a repository names for its CI's
test job (`ci_proof.py test`), and no browser command, which it names for its E2E's browser job
(`ci_proof.py browser`).

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

FILE = "shipline.json"
# Where the runner pool names the repository its runners register with, and how long Bash may
# take to read it.
RUNNER_SETTINGS = ".github/runner/settings.sh"
RUNNER_SETTINGS_SECONDS = 5
# The prefix names a systemd unit and, as `<prefix>-runner`, a user of at most 32 characters.
_PREFIX = re.compile(r"[a-z](?:[a-z0-9-]{0,23}[a-z0-9])?")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")

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
        # The gate deletes the ones it named once no prefix needs them, and the example workflows
        # run the gate's dispatches on them on main's runners (examples/ci.yml).
        "scratch_prefix": "workflow/merge-batch-",
    },
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
    "commands": {
        # The command lines that run the repository's tests and its browser tests, from the root
        # of its checkout.
        "test": None,
        "browser": None,
    },
}
# The keys of one machine.
MACHINE_KEYS = ("name", "address")

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


@dataclass(frozen=True)
class Merge:
    scratch_prefix: str


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
class Commands:
    # None when the file names none.
    test: str | None
    browser: str | None


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
    merge: Merge
    machines: tuple[Machine, ...]
    hunts: Hunts
    commands: Commands
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
        elif name == "machines":
            merged[key] = _machines(given[key])
        elif name == "hunts.areas":
            merged[key] = _areas(given[key])
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


def _relative(value: str, name: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ConfigError(f"{FILE}: {name} must be a path inside the checkout, not {value}")
    return value


def _plans(value: str | None) -> str | None:
    if value is not None and not (
        _PLANS.fullmatch(value) and _relative(value, "hunts.plans") == value
    ):
        raise ConfigError(
            f"{FILE}: hunts.plans must be a path inside the checkout of letters, digits, dots, "
            f"dashes and underscores, not {value}"
        )
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


def runner_labels(label: str, planner_label: str | None = None) -> tuple[str, ...]:
    """The labels every machine's runners carry, so GitHub gives a job to whichever machine has
    one free: the base label, a spare's before it takes a slot, the merge train's first
    member's, main's runners', the Docker runners' and the planner's, `<label>-planner` unless
    the file names another."""
    return (
        label,
        f"{label}-standby",
        f"{label}-first",
        f"{label}-main",
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


def load(start: Path | None = None) -> Config:
    """The configuration of the repository `start` is in; without one, of the repository
    Shipline works on (`repository_checkout`)."""
    main = repository_checkout() if start is None else main_checkout(start)
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
    return Config(
        found_repository=repository,
        state_folder=_relative(values["state_folder"], "state_folder"),
        worktree_setup=_relative(values["worktree_setup"], "worktree_setup"),
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
        machines=tuple(Machine(**machine) for machine in values["machines"]),
        hunts=Hunts(areas=values["hunts"]["areas"], plans=_plans(values["hunts"]["plans"])),
        commands=Commands(**values["commands"]),
        given=given,
        main=main,
    )


@cache
def current() -> Config:
    """The configuration of the repository Shipline works on, read once a run."""
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
            "label": config.runner.label,
            "planner_label": config.runner.planner_label,
            "image_build": config.runner.image_build,
            "hub_cache": config.runner.hub_cache,
        },
        "merge": vars(config.merge).copy(),
        "machines": [
            {"name": machine.name, **({"address": machine.address} if machine.address else {})}
            for machine in config.machines
        ],
        "hunts": {
            "areas": {area: list(screens) for area, screens in config.hunts.areas.items()},
            "plans": config.hunts.plans,
        },
        "commands": vars(config.commands).copy(),
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
        # Empty when the file names none.
        "RUNNER_PREFIX": config.runner.prefix or "",
        "RUNNER_LABEL": config.runner.label or "",
        "RUNNER_PLANNER_LABEL": config.runner.planner_label or "",
        "RUNNER_IMAGE_BUILD": config.runner.image_build or "",
        "RUNNER_HUB_CACHE": config.runner.hub_cache or "",
        "HUNT_PLANS": config.hunts.plans or "",
        "CONFIG": json.dumps(config.given, separators=(",", ":")),
    }
    lines = [f"SHIPLINE_{key}={shlex.quote(value)}" for key, value in values.items()]
    labels = " ".join(shlex.quote(label) for label in config.runner.labels)
    lines.append(f"SHIPLINE_RUNNER_LABELS=({labels})")
    # The main checkout, exported: every program the script starts works on the same repository,
    # whatever folder it starts in.
    lines.append(f"export SHIPLINE_REPO={shlex.quote(str(config.main))}")
    # The hunt areas, and at the same place in the other array the screens of each, joined.
    areas = config.hunts.areas
    lines.append(f"SHIPLINE_HUNT_AREAS=({' '.join(shlex.quote(area) for area in areas)})")
    screens = " ".join(shlex.quote(", ".join(paths)) for paths in areas.values())
    lines.append(f"SHIPLINE_HUNT_SCREENS=({screens})")
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) not in (2, 3) or sys.argv[1] != "shell":
        print("usage: workflow_config.py shell [folder]", file=sys.stderr)
        return 2
    try:
        config = load(Path(sys.argv[2]) if len(sys.argv) == 3 else None)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    sys.stdout.write(shell(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
