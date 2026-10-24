"""Breaking tests for the lender (runner/lend_runners.py) as an installation runs it daily: two of
main's runners on one machine, one or two lent to pull request jobs while main has none queued,
the merge train's first member preferred, and now the merge gate's prefix runs counted as main's
own. Each round is run against a stand-in GitHub that answers from a table."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts/workflow"))
_SPEC = importlib.util.spec_from_file_location("lend_runners_break", _ROOT / "runner/lend_runners.py")
assert _SPEC and _SPEC.loader
lend_runners = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lend_runners)

_REPOSITORY = "example-owner/example-app"
_API = f"repos/{_REPOSITORY}"
LABELS = lend_runners.Labels(
    home="example-app-home", first="example-app-home-first", main="example-app-home-main"
)
_BATCH = "workflow/merge-batch-" + "ab" * 16
_E2E = ".github/workflows/e2e.yml"


def _runner(identity: int, name: str, *labels: str, busy: bool = False) -> dict[str, Any]:
    return {
        "id": identity,
        "name": name,
        "status": "online",
        "busy": busy,
        "labels": [{"name": label} for label in ["self-hosted", *labels]],
    }


def _run(identity: int, event: str, branch: str, status: str, **more: Any) -> dict[str, Any]:
    return {
        "id": identity,
        "event": event,
        "head_branch": branch,
        "path": more.get("path", ".github/workflows/ci.yml"),
        "status": status,
        "head_sha": "1" * 40,
        "run_attempt": more.get("attempt", 1),
    }


def _job(identity: int, name: str, status: str, *labels: str) -> dict[str, Any]:
    return {"id": identity, "name": name, "status": status, "labels": ["self-hosted", *labels]}


class GitHub:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.changes: list[tuple[str, ...]] = []
        self.runners = [
            _runner(1, "home-main", LABELS.main),
            _runner(2, "home-main-2", LABELS.main),
        ]

    def __call__(self, *arguments: str) -> Any:
        if arguments[0] == "-X":
            self.changes.append(arguments)
            path = arguments[2]
            if "/labels" in path:
                runner = next(r for r in self.runners if f"/runners/{r['id']}/" in path)
                if arguments[1] == "POST":
                    runner["labels"].append({"name": arguments[4].split("=", 1)[1]})
                else:
                    gone = path.rsplit("/", 1)[1]
                    runner["labels"] = [x for x in runner["labels"] if x["name"] != gone]
            return None
        path = arguments[0]
        if path.startswith(f"{_API}/actions/runners?"):
            return {"runners": self.runners}
        if path.startswith(f"{_API}/actions/runs?"):
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
            found = [
                run
                for run in sorted(self.runs, key=lambda run: -run["id"])
                if run["event"] == query["event"]
                and ("branch" not in query or run["head_branch"] == query["branch"])
                and ("status" not in query or run["status"] == query["status"])
            ]
            return {"workflow_runs": found[: int(query["per_page"])]}
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": self.jobs[int(path.split("/")[-2])]}
        if "/pulls/" in path:
            return {"head": {"ref": "feature/example"}}
        raise AssertionError(f"unexpected read {arguments}")

    def labels_of(self, identity: int) -> set[str]:
        runner = next(r for r in self.runners if r["id"] == identity)
        return {label["name"] for label in runner["labels"]} - {"self-hosted"}


def _round(github: GitHub, tmp_path: Path, most: int = 1) -> list[tuple[str, ...]]:
    github.changes = []
    first = lend_runners.First(
        github,
        _REPOSITORY,
        tmp_path / "train-first.json",
        tmp_path / "replanned.json",
        pause=lambda seconds: None,
    )
    lend_runners.round_once(
        github, _REPOSITORY, LABELS, most, first, tmp_path / "first.json", "home-main"
    )
    return github.changes


def _lent(github: GitHub) -> list[int]:
    return [r["id"] for r in github.runners if LABELS.home in github.labels_of(r["id"])]


def test_a_day_lend_hold_for_the_merge_line_then_lend_again(tmp_path: Path) -> None:
    """Idle: one runner lent. A wave: the gate dispatches four prefixes; while any of their jobs
    is queued or running for main's label nothing is lent. They finish: lent again."""
    github = GitHub()
    _round(github, tmp_path)
    assert _lent(github) == [1]

    # The gate dispatches CI and E2E for four prefixes: queued first, then one runs.
    for prefix in range(4):
        for offset, path in ((0, ".github/workflows/ci.yml"), (1, _E2E)):
            identity = 100 + prefix * 2 + offset
            github.runs.append(
                _run(identity, "workflow_dispatch", f"{_BATCH[:-1]}{prefix}", "queued", path=path)
            )
            github.jobs[identity] = [_job(identity, "CI: what to run", "queued", LABELS.main)]
    _round(github, tmp_path)
    assert _lent(github) == []

    github.runs[0]["status"] = "in_progress"
    github.jobs[100] = [_job(100, "CI: server tests", "in_progress", LABELS.main)]
    for run in github.runs[1:]:
        run["status"] = "completed"
        github.jobs[run["id"]] = [_job(run["id"], "x", "completed", LABELS.main)]
    github.runners[1]["busy"] = True
    _round(github, tmp_path)
    assert _lent(github) == [], "lent while a prefix job still runs on main's runners"

    github.runs[0]["status"] = "completed"
    github.jobs[100] = [_job(100, "CI: server tests", "completed", LABELS.main)]
    github.runners[1]["busy"] = False
    _round(github, tmp_path)
    assert _lent(github) == [1]
    published = json.loads((tmp_path / "first.json").read_text())
    assert published["free"] == 1


def test_a_prefix_run_re_run_in_part_holds_the_lending_again(tmp_path: Path) -> None:
    """The gate re-runs a failed prefix's failed jobs: the completed run goes back under way, a
    new attempt, its re-run job queued for main's label."""
    github = GitHub()
    github.runs.append(_run(200, "workflow_dispatch", _BATCH, "completed"))
    github.jobs[200] = [_job(1, "CI: server tests", "completed", LABELS.main)]
    _round(github, tmp_path)
    assert _lent(github) == [1]

    github.runs[0].update(status="queued", run_attempt=2)
    github.jobs[200] = [_job(2, "CI: server tests", "queued", LABELS.main)]
    _round(github, tmp_path)
    assert _lent(github) == []


def test_the_first_member_s_batch_is_never_cancelled_and_main_is_not_lent(tmp_path: Path) -> None:
    """The gate names the train's first member and its batch. The batch's run asks for main's
    label: the lender must neither cancel it (the gate would read a failed prefix) nor lend."""
    github = GitHub()
    (tmp_path / "train-first.json").write_text(json.dumps({"number": "7", "batch": _BATCH}))
    github.runs.append(_run(300, "workflow_dispatch", _BATCH, "in_progress"))
    github.jobs[300] = [
        _job(1, "CI: what to run", "completed", LABELS.main),
        _job(2, "CI: server tests", "queued", LABELS.main),
    ]

    changes = _round(github, tmp_path, most=2)

    assert not any("cancel" in " ".join(change) for change in changes)
    assert _lent(github) == []


def test_a_push_to_main_and_a_prefix_at_once(tmp_path: Path) -> None:
    """Two calls at once on GitHub's side: main's push queued while prefixes run. Neither
    runner is lent, and a lent idle runner comes back."""
    github = GitHub()
    github.runners[0]["labels"] += [{"name": LABELS.home}, {"name": LABELS.first}]
    github.runs += [
        _run(400, "workflow_dispatch", _BATCH, "in_progress"),
        _run(401, "push", "main", "queued"),
    ]
    github.jobs[400] = [_job(1, "CI: server tests", "in_progress", LABELS.main)]
    github.jobs[401] = [_job(2, "CI: what to run", "queued", LABELS.main)]
    github.runners[1]["busy"] = True

    _round(github, tmp_path, most=2)

    assert _lent(github) == []
    assert github.labels_of(1) == {LABELS.main}
