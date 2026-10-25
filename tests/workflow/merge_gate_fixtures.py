"""Shared merge-gate stand-ins, fixtures and history builders."""

import errno
import fcntl
import importlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from stand_in_server import StandInServer
from workflow.test_ci_proof import annotations
from workflow_harness import Workflow

_GH = r"""import builtins
import fcntl
import json
import subprocess
import sys
from functools import partial
from pathlib import Path
state = Path("@STATE@")
def answer(args, cwd, env, out, err):
    # A program's answer, run by the test's server (stand_in_server.py).
    print = partial(builtins.print, file=out)
    def git(words, check, **given):
        # What git prints reaches the caller, as it did when this stand-in was a program.
        done = subprocess.run(words, env=env, capture_output=True, text=True, **given)
        err.write(done.stderr)
        if check and done.returncode:
            raise subprocess.CalledProcessError(done.returncode, words)
        return done.stdout
    def check_output(words, text, **given):
        return git(words, True, **given)
    def run(words, check=False):
        out.write(git(words, check))
    with (state / "calls.log").open("a") as log:
        log.write("gh " + " ".join(args) + "\n")
    data = json.loads((state / "github.json").read_text())
    probe = Path(data.get("lock_probe") or "/nonexistent/merge.lock")
    if probe.parent.is_dir():
        # A call made while a gate holds the merge lock is written down as well.
        with probe.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                with (state / "locked-calls.log").open("a") as log:
                    log.write("gh " + " ".join(args) + "\n")
    pause = state / "pause-call"
    if pause.exists() and pause.read_text().strip() in " ".join(args):
        # The call waits until the test lets it go on.
        pause.unlink()
        with (state / "call-entered").open("w") as pipe:
            pipe.write("entered\n")
        with (state / "call-continue").open() as pipe:
            pipe.read(1)
    if args[:2] == ["pr", "view"] and "-R" in args and args[2] in data["pulls"]:
        if data["pulls"][args[2]].get("fail_view"):
            sys.exit("GitHub unavailable")
        if data.get("view_moves_main") == args[2]:
            # Another push lands while the gate reads this pull request.
            data["view_moves_main"] = None
            def bare(*words):
                return check_output(
                    ["git", "--git-dir", data["origin"], *words], text=True).strip()
            moved = bare("commit-tree", "main^{tree}", "-p", "main", "-m", "concurrent push")
            bare("update-ref", "refs/heads/main", moved)
            (state / "github.json").write_text(json.dumps(data))
        for check in data["pulls"][args[2]]["statusCheckRollup"]:
            check.setdefault("__typename", "StatusContext" if "state" in check else "CheckRun")
        print(json.dumps(data["pulls"][args[2]]))
        changed = data.get("after_view", {}).pop(args[2], None)
        if changed:
            # The pull request changes on GitHub right after this read.
            data["pulls"][args[2]].update(changed)
            (state / "github.json").write_text(json.dumps(data))
        pushes = data.get("push_after_view", {}).get(args[2])
        if pushes:
            # Another push lands on its branch right after this read, or after a later one.
            if pushes.pop(0):
                branch = "refs/heads/" + data["pulls"][args[2]]["headRefName"]
                def bare(*words):
                    return check_output(
                        ["git", "--git-dir", data["origin"], *words], text=True).strip()
                moved = bare("commit-tree", branch + "^{tree}", "-p", branch, "-m", "another push")
                bare("update-ref", branch, moved)
            (state / "github.json").write_text(json.dumps(data))
        lines = data.get("line_after_view", {}).get(args[2])
        if lines:
            # The tick saves another line right after this read, or after a later one.
            line = lines.pop(0)
            if line is not None:
                train = Path(data["lock_probe"]).with_name("merge-train.json")
                train.write_text(json.dumps(line))
            (state / "github.json").write_text(json.dumps(data))
    elif (args[:2] == ["pr", "list"]
          and "--state" in args and "--jq" not in args and "--base" not in args):
        # Both bulk and individual reads describe the same GitHub state. Keep ordinary
        # tickets from the harness listing alongside the gate fixture's richer members.
        members = {}
        for listed in (state / "listing").iterdir():
            n = listed.name
            if n in data["pulls"]:
                p = data["pulls"][n]
                status, branch, head = p["state"], p["headRefName"], p["headRefOid"]
            else:
                status, branch, head = (state / "pr" / n).read_text().split()
            draft, *labels = listed.read_text().split()
            members[n] = {"number": int(n), "state": status, "isDraft": draft == "true",
                          "baseRefName": "main", "headRefName": branch, "headRefOid": head,
                          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                          "statusCheckRollup": [], "body": "Backend workflow maintenance.",
                          "labels": [{"name": label.replace("+", " ")} for label in labels]}
        members.update({n: {"number": int(n), "labels": [], "body": "Backend workflow maintenance.",
                            **members.get(n, {}), **p} for n, p in data["pulls"].items()})
        for member in members.values():
            for check in member["statusCheckRollup"]:
                check.setdefault("__typename", "StatusContext" if "state" in check else "CheckRun")
        print(json.dumps([p for p in members.values() if p["state"] == "OPEN"]))
    elif data.get("driver_mode") and args[:2] == ["pr", "list"] and "--base" in args:
        base = args[args.index("--base") + 1]
        print(json.dumps([{"number": int(n)} for n, p in data["pulls"].items()
                          if p["state"] == "OPEN" and p["baseRefName"] == base]))
    elif data.get("driver_mode") and args[:3] == ["api", "-X", "PATCH"]:
        number = args[3].split("/")[-1]
        data["pulls"][number]["baseRefName"] = "main"
        (state / "github.json").write_text(json.dumps(data))
    elif args[:2] == ["pr", "merge"]:
        refusals = data.get("refuse_merge", {})
        if refusals.get(args[2]):
            refusals[args[2]] -= 1
            if data.get("refusal_moves_main"):
                moved = check_output(["git", "--git-dir", data["origin"], "commit-tree",
                    "main^{tree}", "-p", "main", "-m", "concurrent push"], text=True).strip()
                run(["git", "--git-dir", data["origin"], "update-ref",
                                "refs/heads/main", moved], check=True)
            (state / "github.json").write_text(json.dumps(data))
            sys.exit("Base branch was modified. Review and try the merge again.")
        if (state / "block-first-merge").exists():
            (state / "block-first-merge").unlink()
            with (state / "merge-entered").open("w") as pipe:
                pipe.write("entered\n")
            with (state / "merge-continue").open() as pipe:
                pipe.read(1)
        if data.get("next_main"):
            run(["git", "--git-dir", data["origin"], "update-ref",
                            "refs/heads/main", data["next_main"]], check=True)
        (state / "merged").write_text(args[2])
        if data.get("batch_mode"):
            def bare(*words):
                return check_output(
                    ["git", "--git-dir", data["origin"], *words], text=True).strip()
            base = bare("rev-parse", "main")
            head = data["pulls"][args[2]]["headRefOid"]
            if args[-1] != head:
                sys.exit("head changed")
            tree = bare("merge-tree", "--write-tree", base, head).splitlines()[0]
            body = ""
            if "--body-file" in args:
                body = Path(args[args.index("--body-file") + 1]).read_text()
            actual = bare(
                "commit-tree", tree, "-p", base, "-m", "Squash " + args[2] + "\n\n" + body)
            bare("update-ref", "refs/heads/main", actual, base)
            data["pulls"][args[2]]["state"] = "MERGED"
            if data.get("driver_mode"):
                pull = data["pulls"][args[2]]
                (state / "pr" / args[2]).write_text(
                    f"MERGED {pull['headRefName']} {pull['headRefOid']}\n")
            # Main turns red, or another push lands, right after this squash.
            after_main = data.get("after_main", {}).pop(args[2], None)
            if after_main and after_main.startswith("on-view-of-"):
                data["view_moves_main"] = after_main.removeprefix("on-view-of-")
            if after_main == "unreachable":
                # The gate cannot fetch main after this squash.
                Path(data["origin"]).rename(data["origin"] + ".away")
            if after_main == "pushed-over":
                moved = bare("commit-tree", tree, "-p", actual, "-m", "concurrent push")
                bare("update-ref", "refs/heads/main", moved, actual)
            # What GitHub shows of the other pull requests right after this squash moved main.
            for number, patch in data.get("after_merge", {}).pop(args[2], {}).items():
                data["pulls"][number].update(patch)
            for workflow in ("ci", "images"):
                prefix = "repos/example-org/example-app/actions/workflows/"
                data["api"][prefix + f"{workflow}.yml/runs?head_sha={actual}&per_page=100"] = {
                    "workflow_runs": []}
            if after_main == "red":
                data["api"][prefix + f"ci.yml/runs?head_sha={actual}&per_page=100"] = {
                    "workflow_runs": [{"id": 60, "head_sha": actual, "event": "push",
                        "head_branch": "main", "status": "completed", "conclusion": "failure",
                        "updated_at": "2026-10-16T10:00:00Z"}]}
                data["api"]["repos/example-org/example-app/actions/runs/60/jobs?per_page=100"] = {
                    "jobs": [{"id": 61, "name": "CI result", "status": "completed",
                        "conclusion": "failure"}, {"id": 62, "name": "CI: web",
                        "status": "completed", "conclusion": "failure"}]}
            (state / "github.json").write_text(json.dumps(data))
    elif args[:2] == ["workflow", "run"]:
        common = check_output(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                              cwd=cwd, text=True).strip()
        train = Path(common).parent / ".local/workflow/merge-train.json"
        # The state during a batch's first dispatch; E2E's follows CI's.
        if not (state / "batch-state-during-dispatch").exists():
            (state / "batch-state-during-dispatch").write_text(train.read_text())
        if (state / "reject-batch-dispatch").exists():
            sys.exit("workflow dispatch rejected")
    elif args[:2] == ["run", "view"] and "--log-failed" in args:
        job = args[args.index("--job") + 1]
        log = state / "job-logs" / job
        if not log.exists():
            sys.exit("failed to get run log: log not found")
        print(log.read_text())
    elif args[:2] == ["run", "rerun"]:
        unavailable = state / "rerun-unavailable"
        if unavailable.exists():
            # GitHub fails this re-run once, as in an outage.
            message = unavailable.read_text()
            unavailable.unlink()
            sys.exit(message)
        if (state / "fail-rerun").exists():
            sys.exit((state / "fail-rerun").read_text().strip() or "rerun refused")
    elif args[:2] == ["run", "cancel"]:
        # Each cancel is written down with the scratch branches GitHub still has at that moment.
        listed = check_output(["git", "--git-dir", data["origin"], "for-each-ref",
            "--format=%(refname:short)", "refs/heads/workflow/merge-batch-*"], text=True).split()
        with (state / "cancelled.log").open("a") as log:
            log.write(" ".join([args[2], *listed]) + "\n")
        if int(args[2]) in data.get("refuse_cancel", []):
            sys.exit("Cannot cancel a workflow run that is completed.")
        unavailable = data.get("cancel_unavailable", [])
        if int(args[2]) in unavailable:
            # GitHub fails this cancel once, as in an outage.
            unavailable.remove(int(args[2]))
            (state / "github.json").write_text(json.dumps(data))
            sys.exit("HTTP 502: Bad Gateway")
    elif args[0] == "api":
        path = args[-1]
        # GitHub knows no workflow the repository does not hold.
        if any(f"/actions/workflows/{name}/" in path for name in data.get("missing_workflows", [])):
            sys.exit("gh: Not Found (HTTP 404)")
        if path not in data["api"]:
            if "event=workflow_dispatch" in path or "/actions/runs?branch=" in path:
                print('{"workflow_runs": []}')
                sys.exit(0)
            if "/check-runs?" in path:
                print('{"total_count": 0, "check_runs": []}')
                sys.exit(0)
            sys.exit("unexpected API request: " + path)
        reply = data["api"][path]
        answers = reply.get("_alternate") if isinstance(reply, dict) else None
        if answers is not None:
            # GitHub answers the same query in turns: this one, then the next.
            reply = answers[0]
            answers.append(answers.pop(0))
            (state / "github.json").write_text(json.dumps(data))
        if reply is None:
            sys.exit("GitHub unavailable")
        if isinstance(reply, str):
            sys.exit("gh: " + reply)  # a server error, such as "HTTP 502"
        pages = reply.get("_pages") if isinstance(reply, dict) else None
        if pages is not None:
            for page in (pages if "--paginate" in args else pages[:1]):
                print(json.dumps(page))
        else:
            print(json.dumps(reply))
        if "/annotations?" in path and (state / "github-after-annotations.json").exists():
            after = (state / "github-after-annotations.json").read_text()
            (state / "github.json").write_text(after)
    else:
        # Everything else is answered by the harness's own stand-in.
        return "delegate"
"""

_REPO = "repos/example-org/example-app"
_MAIN_IMAGES = f"{_REPO}/actions/workflows/images.yml/runs?branch=main&per_page=100&page=1"


@dataclass
class GitHub:
    workflow: Workflow
    api: dict[str, Any]
    pulls: dict[str, Any]
    base: str
    head: str
    next_main: str | None = None
    batch_mode: bool = False
    driver_mode: bool = False
    refuse_merge: dict[str, int] = field(default_factory=dict[str, int])
    refusal_moves_main: bool = False
    after_main: dict[str, str] = field(default_factory=dict[str, str])
    after_merge: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=dict[str, dict[str, dict[str, Any]]]
    )
    # What GitHub shows of a pull request right after the gate's next read of it.
    after_view: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    # The merge lock whose holder's calls the stand-in writes down (locked_calls).
    lock_probe: str | None = None
    # The line saved beside that lock after each of the gate's reads of a pull request.
    line_after_view: dict[str, list[dict[str, Any] | None]] = field(
        default_factory=dict[str, list[dict[str, Any] | None]]
    )
    # Whether another push lands on a pull request's branch after each of the gate's reads of it.
    push_after_view: dict[str, list[bool]] = field(default_factory=dict[str, list[bool]])
    # The runs whose cancel GitHub refuses, and those whose next cancel fails in an outage.
    refuse_cancel: list[int] = field(default_factory=list[int])
    cancel_unavailable: list[int] = field(default_factory=list[int])
    # The workflow files the repository does not hold, whose runs GitHub cannot list.
    missing_workflows: list[str] = field(default_factory=list[str])

    def save(self) -> None:
        (self.workflow.state / "github.json").write_text(
            json.dumps(
                {
                    "api": self.api,
                    "pulls": self.pulls,
                    "next_main": self.next_main,
                    "origin": str(self.workflow.root / "origin.git"),
                    "batch_mode": self.batch_mode,
                    "driver_mode": self.driver_mode,
                    "refuse_merge": self.refuse_merge,
                    "refusal_moves_main": self.refusal_moves_main,
                    "after_main": self.after_main,
                    "after_merge": self.after_merge,
                    "after_view": self.after_view,
                    "lock_probe": self.lock_probe,
                    "line_after_view": self.line_after_view,
                    "push_after_view": self.push_after_view,
                    "refuse_cancel": self.refuse_cancel,
                    "cancel_unavailable": self.cancel_unavailable,
                    "missing_workflows": self.missing_workflows,
                }
            )
        )

    def run(self) -> subprocess.CompletedProcess[str]:
        self.save()
        return self.workflow.run_script("merge-when-current", "7")

    def prepare_batch(self) -> None:
        """Reuse the gate's real-Git batch scenario in tick integration tests."""
        _batch_setup(self)

    def batch_state(self) -> dict[str, Any]:
        return _batch_state(self)

    def finish_batch(self, *, conclusion: str = "success", run_id: int = 100) -> None:
        _finish_batch(self, conclusion=conclusion, run_id=run_id)

    def ci(
        self,
        base: str,
        head: str,
        *,
        tested_base: str | None = None,
        run_id: int = 20,
        server: str | None = "whole",
    ) -> None:
        """The pull request's CI run on `head`; `server` is its "Server tests" note."""
        merge = self.workflow.git(
            "commit-tree",
            self.workflow.git("rev-parse", f"{head}^{{tree}}"),
            "-p",
            tested_base or base,
            "-p",
            head,
            "-m",
            "Test merge",
        )
        self.api[
            f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
        ] = {
            "workflow_runs": [
                {
                    "id": run_id,
                    "head_sha": head,
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }
        self.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {
            "jobs": [
                {
                    "id": run_id + 1,
                    "name": "CI: what to run",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": run_id + 2,
                    "name": "CI result",
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
        }
        self.api[f"{_REPO}/check-runs/{run_id + 1}/annotations?per_page=100"] = [
            {"title": "Merge tested", "message": merge},
            *([{"title": "Server tests", "message": server}] if server is not None else []),
        ]
        self.api[f"{_REPO}/git/commits/{merge}"] = {
            "parents": [{"sha": tested_base or base}, {"sha": head}],
            "tree": {"sha": self.workflow.git("rev-parse", f"{head}^{{tree}}")},
        }
        self.api[
            f"{_REPO}/actions/workflows/images.yml/runs?head_sha={head}&event=pull_request&per_page=100"
        ] = {"workflow_runs": []}
        # The pull request's own E2E run, of the whole suite unless a test says otherwise.
        self.e2e(head, self.workflow.git("rev-parse", f"{head}^{{tree}}"), run_id=run_id + 900_000)

    def e2e(
        self,
        head: str,
        tree: str,
        *,
        suite: str = "whole",
        run_id: int,
        conclusion: str = "success",
        branch: str | None = None,
    ) -> None:
        """An E2E run on `head`: the pull request's own, or a dispatched one on `branch`."""
        event = "workflow_dispatch" if branch else "pull_request"
        self.api[
            f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={head}&event={event}&per_page=100"
        ] = {
            "workflow_runs": [
                {
                    "id": run_id,
                    "head_sha": head,
                    "head_branch": branch or "ticket/seven",
                    "event": event,
                    "status": "completed",
                    "conclusion": conclusion,
                }
            ]
        }
        self.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {
            "jobs": [
                {
                    "id": run_id + 1,
                    "name": "E2E: what to run",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": run_id + 2,
                    "name": "playwright",
                    "status": "completed",
                    "conclusion": conclusion,
                },
            ]
        }
        self.api[f"{_REPO}/check-runs/{run_id + 1}/annotations?per_page=100"] = [
            {"title": "Files tested", "message": tree},
            {"title": "Browser suite", "message": suite},
        ]

    def ci_dispatched(
        self,
        head: str,
        branch: str,
        *,
        run_id: int,
        server: str = "whole",
        conclusion: str = "success",
        merge: str | None = None,
        python: str | None = None,
    ) -> None:
        """A manual CI run of the gate's scratch branch `branch` at `head`; `python` is how its
        "CI: server tests" ended, as the run did unless it says otherwise."""
        self.api[
            f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=workflow_dispatch&per_page=100"
        ] = {
            "workflow_runs": [
                {
                    "id": run_id,
                    "head_sha": head,
                    "head_branch": branch,
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": conclusion,
                }
            ]
        }
        self.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {
            "jobs": [
                {
                    "id": run_id + 1,
                    "name": "CI: what to run",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": run_id + 2,
                    "name": "CI: server tests",
                    "status": "completed",
                    "conclusion": python or conclusion,
                },
                {
                    "id": run_id + 3,
                    "name": "CI result",
                    "status": "completed",
                    "conclusion": conclusion,
                },
            ]
        }
        self.api[f"{_REPO}/check-runs/{run_id + 1}/annotations?per_page=100"] = [
            {"title": "Merge tested", "message": merge or head},
            {"title": "Server tests", "message": server},
        ]

    def main(self, conclusion: str = "success") -> None:
        self.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={self.base}&per_page=100"] = {
            "workflow_runs": [
                {
                    "id": 10,
                    "head_sha": self.base,
                    "event": "push",
                    "head_branch": "main",
                    "status": "completed",
                    "conclusion": conclusion,
                    "updated_at": "2026-10-16T09:00:00Z",
                }
            ]
        }
        self.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"] = {
            "jobs": [
                {"id": 11, "name": "CI result", "status": "completed", "conclusion": conclusion},
                {"id": 12, "name": "CI: web", "status": "completed", "conclusion": conclusion},
            ]
        }
        self.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={self.base}&per_page=100"] = {
            "workflow_runs": []
        }
        self.api.setdefault(_MAIN_IMAGES, {"workflow_runs": []})
        self.api[
            f"{_REPO}/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=100"
        ] = {"workflow_runs": []}


@pytest.fixture
def github(workflow: Workflow) -> Iterator[GitHub]:
    images = workflow.repo / ".github/workflows/images.yml"
    images.parent.mkdir(parents=True, exist_ok=True)
    images.write_text('on:\n  pull_request:\n    paths:\n      - "docker/**"\n  push:\n')
    (workflow.repo / "shared.txt").write_text("original\n")
    workflow.git("add", ".github")
    workflow.git("add", "shared.txt")
    workflow.git("commit", "-m", "chore: image workflow")
    workflow.git("push", "origin", "main")
    base = workflow.git("rev-parse", "main")
    workflow.git("checkout", "-b", "ticket/seven")
    workflow.git("commit", "--allow-empty", "-m", "fix: seven")
    head = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    workflow.fake("gh-original", (workflow.bin / "gh").read_text())
    workflow.fake("gh.py", _GH)
    server = StandInServer(workflow.bin / "gh.py", workflow.env)
    workflow.fake("gh", server.client("gh-original"))
    made = GitHub(
        workflow,
        {},
        {
            "7": {
                "state": "OPEN",
                "isDraft": False,
                "baseRefName": "main",
                "headRefName": "ticket/seven",
                "headRefOid": head,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
            }
        },
        base,
        head,
    )
    workflow.running("review-7")
    made.main()
    made.ci(base, head)
    try:
        yield made
    finally:
        server.close()


def _two_stale_pulls(github: GitHub) -> Path:
    workflow = github.workflow
    second = workflow.root / "second"
    workflow.running("review-8")
    workflow.git("worktree", "add", "-b", "ticket/eight", str(second), github.base)
    workflow.git("commit", "--allow-empty", "-m", "fix: eight", cwd=second)
    head = workflow.git("rev-parse", "HEAD", cwd=second)
    workflow.git("push", "origin", "HEAD:ticket/eight", cwd=second)
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefOid": head,
        "headRefName": "ticket/eight",
    }
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old)
    github.ci(github.base, head, tested_base=old, run_id=30)
    github.save()
    return second


def _refreshed_first(github: GitHub) -> None:
    github.head = github.workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head, run_id=50)
    github.save()


def _waiting_reviewers(github: GitHub) -> None:
    workflow = github.workflow
    for number in (7, 8):
        workflow.add_worktree(number, draft=False)
        head = github.pulls[str(number)]["headRefOid"]
        workflow.set_pull_request(number, "OPEN", head)
        if not workflow.is_running(f"review-{number}"):
            workflow.running(f"review-{number}")
        notes = workflow.notes(number)
        notes.mkdir(parents=True, exist_ok=True)
        (notes / f"review-{number}.started").touch()
        (notes / f"review-{number}.checks-wait").touch()
        workflow.screen(
            "● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n",
            f"review-{number}",
        )
        (workflow.state / f"rollup-{number}.json").write_text(
            json.dumps(
                [
                    {
                        "__typename": "CheckRun",
                        "name": "CI result",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    }
                ]
            )
        )


def _labelled(github: GitHub, labels: dict[str, list[str]]) -> None:
    for number, names in labels.items():
        github.pulls[number]["labels"] = [{"name": name} for name in names]
    github.save()


def _line(github: GitHub, waiting: list[str]) -> None:
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": waiting}))


def _members_changing(github: GitHub, changes: dict[str, str]) -> None:
    """Members joined in this order, each one commit on main changing its own file."""
    workflow = github.workflow
    names = {"7": "seven", "8": "eight", "9": "nine", "10": "ten"}
    for run, (number, path) in enumerate(changes.items()):
        branch = f"ticket/{names[number]}"
        checkout = workflow.repo if number == "7" else workflow.root / names[number]
        if number != "7":
            workflow.running(f"review-{number}")
            workflow.git("worktree", "add", "-b", branch, str(checkout), github.base)
        (checkout / path).parent.mkdir(parents=True, exist_ok=True)
        (checkout / path).write_text(f"changed by {number}\n")
        workflow.git("add", path, cwd=checkout)
        workflow.git("commit", "--quiet", "-m", f"feat: {number}", cwd=checkout)
        head = workflow.git("rev-parse", "HEAD", cwd=checkout)
        workflow.git("push", "--quiet", "origin", f"HEAD:{branch}", cwd=checkout)
        github.pulls[number] = {
            **deepcopy(github.pulls["7"]),
            "headRefOid": head,
            "headRefName": branch,
        }
        if number == "7":
            github.head = head
        github.ci(github.base, head, run_id=20 + 10 * run)
    github.save()
    _line(github, list(changes))


def _hot(github: GitHub, *paths: str) -> None:
    config = github.workflow.repo / "shipline.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "hot_paths": list(paths)}))


def _notes_by_ci_proof(github: GitHub, checked_out: str, *suites: str) -> None:
    """The pull request's CI as the example CI runs it (examples/ci.yml): its planner checked
    out `checked_out`, GitHub's merge of the pull request into main unless a test says
    otherwise, and wrote its notes with ci_proof.py; GitHub holds them as the planner's
    annotations."""
    workflow = github.workflow
    merge = workflow.git(
        "commit-tree", f"{github.head}^{{tree}}", "-p", github.base, "-p", github.head,
        "-m", "Merge seven into main",
    )
    github.api[f"{_REPO}/git/commits/{merge}"] = {
        "parents": [{"sha": github.base}, {"sha": github.head}],
        "tree": {"sha": workflow.git("rev-parse", f"{github.head}^{{tree}}")},
    }
    # The head itself, on main as it was when the branch began.
    github.api[f"{_REPO}/git/commits/{github.head}"] = {
        "parents": [{"sha": workflow.git("rev-parse", f"{github.head}^")}],
        "tree": {"sha": workflow.git("rev-parse", f"{github.head}^{{tree}}")},
    }
    checkout = workflow.root / "ci-checkout"
    workflow.git("worktree", "add", "--quiet", "--detach", str(checkout),
                 merge if checked_out == "merge" else github.head)
    written = subprocess.run(
        ["python3", "-B", str(workflow.script.parent / "ci_proof.py"), "notes", *suites],
        cwd=checkout, env=workflow.env, capture_output=True, text=True, timeout=60, check=True,
    )
    github.api[f"{_REPO}/check-runs/21/annotations?per_page=100"] = annotations(written.stdout)


def _pr_job(github: GitHub, name: str, conclusion: str) -> None:
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"].append(
        {"id": 29, "name": name, "status": "completed", "conclusion": conclusion}
    )


def _attempt_times(attempt: int) -> dict[str, str]:
    """When attempt `attempt` of a run was created, started and completed: each re-run an
    hour after the one before."""
    hour = 8 + attempt
    return {
        "created_at": f"2026-10-24T{hour:02}:00:00Z",
        "started_at": f"2026-10-24T{hour:02}:00:02Z",
        "completed_at": f"2026-10-24T{hour:02}:05:00Z",
    }


def _rerun_failed_jobs(
    github: GitHub,
    run_id: int,
    head: str,
    *carried: str,
    earlier_head: str | None = None,
    attempts: int = 2,
) -> None:
    """Run `run_id` failed a job and re-ran its failed jobs alone until its last attempt,
    `attempts`, passed. Each re-run carried the `carried` jobs from the first attempt as GitHub
    does: in each later attempt a copy with a new job ID, that attempt's `run_attempt`, no
    notes, the original `started_at` and `completed_at`, and the later `created_at` of the
    re-run. The first attempt ran on `earlier_head`, the head of the run unless a test says
    otherwise."""
    for reply in github.api.values():
        for run in reply.get("workflow_runs", []) if isinstance(reply, dict) else []:
            if run["id"] == run_id:
                run["run_attempt"] = attempts
                run["run_started_at"] = _attempt_times(attempts)["created_at"]
    latest = github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"]["jobs"]
    for attempt in range(1, attempts + 1):
        listed = deepcopy(latest) if attempt < attempts else latest
        for job in listed:
            job.update(
                run_id=run_id,
                run_attempt=attempt,
                head_sha=earlier_head or head if attempt == 1 else head,
                **_attempt_times(attempt),
            )
            if job["name"] not in carried and attempt < attempts:
                job["conclusion"] = "failure"
            if job["name"] in carried and attempt > 1:
                job.update(
                    {
                        key: value
                        for key, value in _attempt_times(1).items()
                        if key != "created_at"
                    },
                    id=run_id + 500 * (attempt - 1) + job["id"] % 100,
                )
                github.api[f"{_REPO}/check-runs/{job['id']}/annotations?per_page=100"] = []
        if attempt < attempts:
            github.api[f"{_REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"] = {
                "jobs": listed
            }


@pytest.fixture
def gate() -> Iterator[Any]:
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "workflow"
    sys.path.insert(0, str(scripts))
    try:
        yield importlib.import_module("merge_gate")
    finally:
        sys.path.remove(str(scripts))


def _attempt_job(
    job_id: int, attempt: int, conclusion: str | None = "success", *, ran: int | None = None
) -> dict[str, Any]:
    """The planner of run 30 as attempt `attempt` lists it; `ran` is the attempt it ran in,
    `attempt` unless GitHub carried it from an earlier one."""
    return {
        "id": job_id,
        "name": "CI: what to run",
        "run_id": 30,
        "run_attempt": attempt,
        "head_sha": "a" * 40,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        **{
            key: value
            for key, value in _attempt_times(ran or attempt).items()
            if key != "created_at"
        },
        "created_at": _attempt_times(attempt)["created_at"],
    }


def _notes_api(
    gate: Any, monkeypatch: pytest.MonkeyPatch, replies: dict[str, Any]
) -> list[str]:
    calls: list[str] = []

    def api(path: str, key: str | None = None, *, paginate: bool = True) -> Any:
        calls.append(path)
        if isinstance(replies[path], Exception):
            raise replies[path]
        return replies[path]

    monkeypatch.setattr(gate, "api", api)
    return calls


def _notes_path(gate: Any, job_id: int) -> str:
    return f"{gate.API}/check-runs/{job_id}/annotations?per_page=100"


def _attempt_path(gate: Any, attempt: int) -> str:
    return f"{gate.API}/actions/runs/30/attempts/{attempt}/jobs?per_page=100"


_DEPENDENCIES = "CI: dependencies"


def _main_red_on_an_advisory(github: GitHub, *, warned: bool) -> None:
    """Main failed "CI: dependencies"; the pull request's own run passed it, over an advisory
    main has too when `warned` (scripts/audit-against-base.py)."""
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": _DEPENDENCIES, "status": "completed", "conclusion": "failure"},
    ]
    _pr_job(github, _DEPENDENCIES, "success")
    github.api[f"{_REPO}/check-runs/29/annotations?per_page=100"] = (
        [{"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}]
        if warned
        else []
    )


# A repository that names its dependency audit and the notice the audit writes on main when only
# advisories failed it (shipline.json, checks.dependency_audit and checks.advisory_annotation).
_AUDIT = "CI: audit"
_ADVISORIES_ONLY = "Dependency audit: advisories only"
_ADVISORY_LINES = ["GHSA-1 in vite (path traversal)", "GHSA-2 in esbuild (open dev server)"]


def _advisory_config(github: GitHub) -> None:
    """Names the audit and its notice, beside the harness's runner labels; git keeps the file as
    committed, so the checkout stays clean."""
    path = github.workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["checks"] = {"dependency_audit": _AUDIT, "advisory_annotation": _ADVISORIES_ONLY}
    path.write_text(json.dumps(config))
    github.workflow.git("update-index", "--skip-worktree", "shipline.json")


def _advisory_watch(github: GitHub) -> None:
    """The repository's advisory watch (`commands.advisory_watch`): it names the service an
    output says could not be reached, as "<service>'s advisory service could not be reached"."""
    github.workflow.fake(
        "example-advisory",
        "#!/bin/bash\n"
        'echo "example-advisory $* (in $PWD)" >> "@STATE@/calls.log"\n'
        "case $1 in\n"
        "  unreachable) grep -o \"[A-Za-z]*'s advisory service could not be reached\" "
        "| head -n 1 | cut -d \"'\" -f 1 ;;\n"
        "  answers) exit 1 ;;\n"
        "esac\n",
    )
    path = github.workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config.setdefault("commands", {})["advisory_watch"] = "example-advisory"
    path.write_text(json.dumps(config))
    github.workflow.git("update-index", "--skip-worktree", "shipline.json")


def _main_audit_failed(
    github: GitHub, *notes: dict[str, Any], audit: str = _AUDIT, others: tuple[str, ...] = ()
) -> None:
    """Main's CI failed in its dependency audit, with `notes` on it, and in `others`."""
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": audit, "status": "completed", "conclusion": "failure"},
        *(
            {"id": 13 + index, "name": name, "status": "completed", "conclusion": "failure"}
            for index, name in enumerate(others)
        ),
    ]
    github.api[f"{_REPO}/check-runs/12/annotations?per_page=100"] = list(notes)
    github.save()


def _advisories_notice(lines: list[str] = _ADVISORY_LINES) -> dict[str, Any]:
    return {
        "title": _ADVISORIES_ONLY,
        "annotation_level": "notice",
        "message": json.dumps(lines),
    }


def _pull_passed_over_mains_advisory(github: GitHub, audit: str = _AUDIT) -> None:
    """The pull request's own audit passed, warning of the advisory main has too."""
    _pr_job(github, audit, "success")
    github.api[f"{_REPO}/check-runs/29/annotations?per_page=100"] = [
        {
            "title": "Advisory on main",
            "annotation_level": "warning",
            "message": "GHSA-1 in vite (path traversal) is on main too, so this branch is not "
            "refused for it; main's own check fails for it until a pull request fixes it there.",
        }
    ]


def _images(
    github: GitHub,
    *,
    pull: bool = True,
    conclusion: str = "success",
    tree: str | None = None,
    release_conclusion: str = "skipped",
) -> None:
    head = github.head if pull else github.base
    event = "&event=pull_request" if pull else ""
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={head}{event}&per_page=100"] = {
        "workflow_runs": [
            {
                "id": 40,
                "head_sha": head,
                "head_branch": "ticket/seven" if pull else "main",
                "event": "pull_request" if pull else "push",
                "status": "completed",
                "conclusion": conclusion,
                "updated_at": "2026-10-16T09:01:00Z",
            }
        ]
    }
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"] = {
        "jobs": [
            {
                "id": 41,
                "name": "Docker images: what to run",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 42,
                "name": "Docker images: image tests",
                "status": "completed",
                "conclusion": conclusion,
            },
            {
                "id": 43,
                "name": "Docker images: release tests",
                "status": "completed",
                "conclusion": release_conclusion,
            },
        ]
    }
    github.api[f"{_REPO}/check-runs/41/annotations?per_page=100"] = [
        {
            "title": "Files tested",
            "message": tree or github.workflow.git("rev-parse", f"{head}^{{tree}}"),
        }
    ]


def _push_without_images(github: GitHub) -> str:
    # A push that changes nothing in the images gets no image run of its own.
    earlier = github.base
    github.workflow.git("checkout", "main")
    github.workflow.git("commit", "--allow-empty", "-m", "fix: nothing in the images")
    github.base = github.workflow.git("rev-parse", "HEAD")
    github.workflow.git("push", "origin", "main")
    github.workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head)
    return earlier


def _main_image_run(
    github: GitHub,
    run_id: int,
    commit: str,
    *,
    status: str = "completed",
    conclusion: Any = "success",
    event: str = "push",
) -> None:
    github.api[_MAIN_IMAGES]["workflow_runs"].insert(
        0,
        {
            "id": run_id,
            "head_sha": commit,
            "head_branch": "main",
            "event": event,
            "status": status,
            "conclusion": conclusion,
            "updated_at": "2026-10-18T07:30:23Z",
        },
    )
    github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {
        "jobs": [
            {
                "id": run_id + 1,
                "name": "Docker images: what to run",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": run_id + 2,
                "name": "Docker images: browser tests (1 of 2)",
                "status": status,
                "conclusion": conclusion,
            },
        ]
    }


def _scheduled_images(
    github: GitHub, *, status: str = "in_progress", conclusion: Any = None
) -> None:
    # The nightly image check runs on main's head too, with a newer id than the push's run.
    endpoint = f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
    github.api[endpoint]["workflow_runs"].append(
        {
            "id": 50,
            "head_sha": github.base,
            "head_branch": "main",
            "event": "schedule",
            "status": status,
            "conclusion": conclusion,
            "updated_at": "2026-10-17T07:30:00Z",
        }
    )
    github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"] = {
        "jobs": [
            {
                "id": 51,
                "name": "Docker images: what to run",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 52,
                "name": "Docker images: browser tests (1 of 2)",
                "status": status,
                "conclusion": conclusion,
            },
        ]
    }


def _squash_with_pending_main(
    github: GitHub, *, different: bool = False, wrong_parent: bool = False
) -> str:
    workflow = github.workflow
    tree = workflow.git("rev-parse", f"{github.head}^{{tree}}")
    if different:
        (workflow.repo / "unexpected.txt").write_text("different squash result\n")
        workflow.git("add", "unexpected.txt")
        tree = workflow.git("write-tree")
        workflow.git("reset", "--hard", "HEAD")
    squash = workflow.git(
        "commit-tree",
        tree,
        "-p",
        github.head if wrong_parent else github.base,
        "-m",
        "fix: seven squashed",
    )
    workflow.git("push", "origin", f"{squash}:refs/heads/prepared-merge")
    github.next_main = squash
    old = github.base
    github.base = squash
    github.main()
    ci = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={squash}&per_page=100"]
    ci["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.base = old
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = []
    github.base = squash
    github.next_main = None
    github.save()
    return squash


_OLDER = "CI: older version on new tables"


def _pull_skipped_older(github: GitHub) -> None:
    # Path filters skip this job on a pull request without a migration; main runs every job.
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"].append(
        {"id": 23, "name": _OLDER, "status": "completed", "conclusion": "skipped"}
    )


def _main_jobs(github: GitHub, *jobs: tuple[str, str, str | None]) -> None:
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 100 + index, "name": name, "status": status, "conclusion": conclusion}
        for index, (name, status, conclusion) in enumerate(jobs)
    ]
    github.save()


_PYTHON = "CI: server tests"


def _pr_parts(github: GitHub, *conclusions: str) -> None:
    """The pull request's "CI: server tests" in parts, named by GitHub with its part."""
    for number, conclusion in enumerate(conclusions, start=1):
        github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"].append(
            {
                "id": 40 + number,
                "name": f"{_PYTHON} ({number} of {len(conclusions)})",
                "status": "completed",
                "conclusion": conclusion,
            }
        )


def _receipt(github: GitHub) -> dict[str, Any]:
    return json.loads((github.workflow.repo / ".local/workflow/main-tree-green.json").read_text())


def _main_ci_run(github: GitHub) -> dict[str, Any]:
    return github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]


def _cancelled_by_github(github: GitHub) -> None:
    """GitHub shows main's CI run 10 cancelled, every job with it."""
    _main_ci_run(github).update(status="completed", conclusion="cancelled")
    _main_jobs(
        github, ("CI result", "completed", "cancelled"), ("CI: web", "completed", "cancelled")
    )


def _main_red_in_one_part(github: GitHub) -> None:
    github.main("failure")
    _main_jobs(
        github,
        ("CI result", "completed", "failure"),
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "completed", "failure"),
    )


def _blocked_first(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    assert github.run().returncode == 1
    github.save()


def _train(github: GitHub) -> dict[str, Any]:
    done = github.workflow.run_script("merge-when-current", "--train-status")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# The train scenarios `test_lost_check.py` builds on.
two_stale_pulls = _two_stale_pulls
line_up = _line
train_status = _train
blocked_first = _blocked_first
refreshed_first = _refreshed_first
waiting_reviewers = _waiting_reviewers


def _green_rerun_on_old_main(github: GitHub) -> None:
    """The blocked first member's failed check, re-run green on main before main moved again."""
    _blocked_first(github)
    old = github.base
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved again")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old, run_id=50)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    github.save()


def _ci_runs(head: str, *, pull: bool = True) -> str:
    event = "&event=pull_request" if pull else ""
    return f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}{event}&per_page=100"


def _check_runs_path(head: str) -> str:
    return f"{_REPO}/commits/{head}/check-runs?per_page=100"


# The runs of check suite 5000, which holds every check run `_check_runs` shows.
_SUITE_RUNS = f"{_REPO}/actions/runs?check_suite_id=5000&per_page=100"


def _check_runs(github: GitHub, head: str, *checks: tuple[str, str]) -> None:
    """The check runs GitHub shows on `head`, by name and status, all in check suite 5000."""
    github.api[_check_runs_path(head)] = {
        "total_count": len(checks),
        "check_runs": [
            {
                "id": 5001 + index,
                "name": name,
                "status": status,
                "conclusion": "success" if status == "completed" else None,
                "check_suite": {"id": 5000},
            }
            for index, (name, status) in enumerate(checks)
        ],
    }


def _tick_context(github: GitHub) -> dict[str, str]:
    path = github.workflow.root / "tick-reads"
    path.mkdir()
    (path / "context.json").write_text(json.dumps({"started": time.time()}))
    (path / "pulls.json").write_text(json.dumps(github.pulls))
    env = {"WORKFLOW_TRAIN_TICK": str(path), "WORKFLOW_SESSION_SNAPSHOT": str(path / "sessions")}
    # The tick's snapshot of the sessions, as its first listing takes it.
    assert github.workflow.run("list", env=env).returncode == 0
    return env


def _unlisted_line(github: GitHub, members: int) -> list[str]:
    """A line of `members` pull requests whose CI jobs all finished, but whose CI runs GitHub
    lists neither by head nor by check suite, as when a rate limit hits them all at once."""
    numbers = [str(7 + index) for index in range(members)]
    for index, number in enumerate(numbers):
        if number not in github.pulls:
            github.pulls[number] = {
                **deepcopy(github.pulls["7"]),
                "headRefOid": f"{index + 1:040x}",
                "headRefName": f"ticket/member-{number}",
            }
            github.workflow.running(f"review-{number}")
        head = github.pulls[number]["headRefOid"]
        github.api[_ci_runs(head)] = {"workflow_runs": []}
        _check_runs(github, head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()
    _line(github, numbers)
    return numbers


def _batch_call(github: GitHub, number: str = "7") -> subprocess.CompletedProcess[str]:
    github.save()
    done = github.workflow.run_script("merge-when-current", number)
    saved = json.loads((github.workflow.state / "github.json").read_text())
    github.api, github.pulls = saved["api"], saved["pulls"]
    github.refuse_merge, github.after_merge = saved["refuse_merge"], saved["after_merge"]
    github.after_main = saved["after_main"]
    github.cancel_unavailable = saved["cancel_unavailable"]
    return done


def _batch_state(github: GitHub) -> dict[str, Any]:
    return json.loads((github.workflow.repo / ".local/workflow/merge-train.json").read_text())


def _batch_setup(
    github: GitHub,
    *,
    count: int = 3,
    conflict: bool = False,
    images: bool = False,
    images_member: int = 7,
    migration: bool = False,
    compatibility: str | None = None,
    selected: bool = False,
) -> None:
    """Members 7 on, each green on a main that has moved since. With `selected`, their own CI
    and E2E ran only the server tests and specs their change reaches, as most do."""
    workflow = github.workflow
    original = github.base
    github.batch_mode = True
    for number in range(7, 7 + count):
        if number != 7:
            workflow.git("checkout", "-b", f"ticket/{number}", original)
        filename = "shared.txt" if conflict and number in (7, 9) else f"change-{number}.txt"
        if images and number == images_member:
            filename = "docker/change.txt"
            (workflow.repo / "docker").mkdir()
        if (migration and number in (8, 9)) or str(number) == compatibility:
            # Different files with one number: Git merges them cleanly, the migration tool has two heads.
            filename = f"migrations/versions/0001_m{number}.py"
            (workflow.repo / filename).parent.mkdir(parents=True, exist_ok=True)
        (workflow.repo / filename).write_text(f"change by {number}\n")
        workflow.git("add", filename)
        workflow.git("commit", "-m", f"fix: member {number}")
        head = workflow.git("rev-parse", "HEAD")
        branch = workflow.git("branch", "--show-current")
        workflow.git("push", "origin", f"HEAD:{branch}")
        github.pulls[str(number)] = {
            **deepcopy(github.pulls["7"]),
            "headRefOid": head,
            "headRefName": branch,
        }
        if not workflow.is_running(f"review-{number}"):
            workflow.running(f"review-{number}")
        github.ci(original, head, run_id=number * 1000)
    github.head = github.pulls["7"]["headRefOid"]
    if images:
        github.head = github.pulls[str(images_member)]["headRefOid"]
        _images(github)
        github.head = github.pulls["7"]["headRefOid"]
    workflow.git("checkout", "ticket/seven")
    # Each reviewer calls the actual gate. Pending main prevents a premature first merge.
    github.main()
    github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={original}&per_page=100"][
        "workflow_runs"
    ][0].update(status="in_progress", conclusion=None)
    assert _batch_call(github).returncode == 1
    for number in range(8, 7 + count):
        assert _batch_call(github, str(number)).returncode == 3
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "chore: move main")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    for number in range(7, 7 + count) if selected else ():
        head = github.pulls[str(number)]["headRefOid"]
        github.ci(original, head, run_id=number * 1000, server=SELECTED)
        tree = workflow.git("rev-parse", f"{head}^{{tree}}")
        github.e2e(head, tree, suite="selected: 3 of 48 spec files", run_id=number * 1000 + 900_000)


def _prefixes(github: GitHub) -> list[dict[str, Any]]:
    """The members tested at once, each with its prefix's scratch branch (None: its own CI)."""
    line = _batch_state(github)["prefixes"]
    return line["members"] if line else []


def _numbers(github: GitHub) -> list[str]:
    return [member["number"] for member in _prefixes(github)]


def _scratch(github: GitHub, number: str | None = None) -> dict[str, Any] | None:
    """The scratch branch of a member's prefix, the first member's by default."""
    members = _prefixes(github)
    found = [m for m in members if number is None or m["number"] == number][:1]
    return found[0]["scratch"] if found else None


def _finish_prefix(
    github: GitHub,
    number: str,
    *,
    conclusion: str = "success",
    run_id: int = 100,
    scratch: dict[str, Any] | None = None,
    tree: str | None = None,
) -> None:
    """Every run of one member's prefix ends with `conclusion`: workflow i is run `run_id + 10i`."""
    if scratch is None:
        member = next(m for m in _prefixes(github) if m["number"] == number)
        scratch, tree = member["scratch"], member["tree"]
    assert scratch is not None and tree is not None
    for index, workflow in enumerate(scratch["workflows"]):
        identity = run_id + index * 10
        github.api[
            f"{_REPO}/actions/workflows/{workflow}.yml/runs?head_sha={scratch['head']}"
            "&event=workflow_dispatch&per_page=100"
        ] = {
            "workflow_runs": [
                {
                    "id": identity,
                    "head_sha": scratch["head"],
                    "head_branch": scratch["branch"],
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": conclusion,
                }
            ]
        }
        if workflow == "e2e":
            github.e2e(
                scratch["head"],
                tree,
                run_id=identity,
                conclusion=conclusion,
                branch=scratch["branch"],
            )
            continue
        plan = "CI: what to run" if workflow == "ci" else "Docker images: what to run"
        github.api[f"{_REPO}/actions/runs/{identity}/jobs?per_page=100"] = {
            "jobs": [
                {"id": identity + 1, "name": plan, "status": "completed", "conclusion": "success"},
                {
                    "id": identity + 2,
                    "name": "CI result" if workflow == "ci" else "Docker images: image tests",
                    "status": "completed",
                    "conclusion": conclusion,
                },
                *(
                    [
                        {
                            "id": identity + 3,
                            "name": "CI: server tests",
                            "status": "completed",
                            # A failed run fails in "CI result", as the tests of it read.
                            "conclusion": "success",
                        }
                    ]
                    if workflow == "ci"
                    else []
                ),
            ]
        }
        github.api[f"{_REPO}/check-runs/{identity + 1}/annotations?per_page=100"] = (
            [
                {"title": "Merge tested", "message": scratch["head"]},
                {"title": "Server tests", "message": "whole"},
            ]
            if workflow == "ci"
            else [{"title": "Files tested", "message": tree}]
        )


def _finish_batch(github: GitHub, *, conclusion: str = "success", run_id: int = 100) -> None:
    """Every prefix's runs end with `conclusion`: the ith member's are `run_id + 1000i` on."""
    for index, member in enumerate(_prefixes(github)):
        if member["scratch"]:
            _finish_prefix(
                github, member["number"], conclusion=conclusion, run_id=run_id + index * 1000
            )


def _scratch_branches(github: GitHub) -> list[str]:
    listed = github.workflow.git("ls-remote", "origin", "refs/heads/workflow/merge-batch-*")
    return [line.split()[1].removeprefix("refs/heads/") for line in listed.splitlines()]


SELECTED = "selected: 4 of 374 files"
# What one stale member whose own runs were selections costs on the single path, merging alone:
# a refresh starts its CI and E2E again, then the whole suites of both run on its tested merge.
ONE_AT_A_TIME_RUNS = 4


def _merges(github: GitHub) -> list[str]:
    return [call.split()[3] for call in github.workflow.calls("gh pr merge")]


def _parents(github: GitHub, commit: str) -> list[str]:
    return github.workflow.git("show", "-s", "--format=%P", commit).split()


def _branch_runs(github: GitHub, branch: str, *runs: tuple[Any, ...]) -> None:
    """The runs GitHub lists for `branch`, each `(id, status)` of CI dispatched on it, or
    `(id, status, its branch, its event)`, or `(id, status, its branch, its event, its
    workflow)`; a completed one passed."""
    github.api[f"{_REPO}/actions/runs?branch={branch}&per_page=100"] = {
        "workflow_runs": [
            {
                "id": run[0],
                "head_sha": "0" * 40,
                "head_branch": run[2] if len(run) > 2 else branch,
                "event": run[3] if len(run) > 2 else "workflow_dispatch",
                "path": f".github/workflows/{run[4] if len(run) > 4 else 'ci'}.yml",
                "status": run[1],
                "conclusion": "success" if run[1] == "completed" else None,
            }
            for run in runs
        ]
    }


def _cancelled(github: GitHub) -> dict[str, list[str]]:
    """Each run the gate asked GitHub to cancel, with the scratch branches there at that time."""
    log = github.workflow.state / "cancelled.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return {line.split()[0]: line.split()[1:] for line in lines}


def _dropped_nine(github: GitHub) -> dict[str, Any]:
    """Members 7 to 9 tested at once; 9's prefix is dropped at the next call."""
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    dropped = _scratch(github, "9")
    assert dropped is not None
    github.pulls["9"]["state"] = "CLOSED"
    return dropped


def _waiting_drops(github: GitHub) -> list[dict[str, Any]]:
    return _batch_state(github).get("dropped", [])


def _restored_before_eight(
    github: GitHub, *, passed: bool, reconciled: bool = False, count: int = 3, late: bool = False
) -> dict[str, Any]:
    """Restore 7 on unchanged main, with a proof shortfall and 8 already tested alone."""
    _batch_setup(github, count=count, images=True, images_member=8)
    if late:
        # 9 has not joined the ready line. A one-slot window starts 8 alone after 7 fails.
        github.pulls["9"]["isDraft"] = True
        saved = _batch_state(github)
        saved["window"] = 2
        (github.workflow.repo / ".local/workflow/merge-train.json").write_text(json.dumps(saved))
    assert _batch_call(github).returncode == 3
    isolated = _prefixes(github)[0]
    _finish_prefix(github, "7", conclusion="failure")
    assert _batch_call(github).returncode == 1
    kept = next(member for member in _prefixes(github) if member["number"] == "8")
    _finish_prefix(github, "8", run_id=1100)
    if not passed:
        for workflow in kept["scratch"]["workflows"]:
            key = (
                f"{_REPO}/actions/workflows/{workflow}.yml/runs"
                f"?head_sha={kept['scratch']['head']}&event=workflow_dispatch&per_page=100"
            )
            github.api[key]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    # Exercise a state written before kept prefixes and their dependencies were recorded.
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    saved = _batch_state(github)
    saved.pop("restored", None)
    for member in saved["prefixes"]["members"]:
        member.pop("ahead", None)
        member.pop("kept", None)
    path.write_text(json.dumps(saved))
    _finish_prefix(
        github, "7", scratch=isolated["scratch"], tree=isolated["tree"], run_id=100
    )
    for workflow in isolated["scratch"]["workflows"]:
        key = (
            f"{_REPO}/actions/workflows/{workflow}.yml/runs"
            f"?head_sha={isolated['scratch']['head']}&event=workflow_dispatch&per_page=100"
        )
        github.api[key]["workflow_runs"][0]["run_attempt"] = 2
    github.api[f"{_REPO}/check-runs/101/annotations?per_page=100"][1]["message"] = SELECTED
    if reconciled:
        github.save()
        assert _train(github)["first"] == "7"
        saved = _batch_state(github)
        saved.pop("restored", None)
        for member in saved["prefixes"]["members"]:
            member.pop("ahead", None)
        path.write_text(json.dumps(saved))
    return kept


# The two ways the pre-push security scan reports the machine's locked-memory shortage.
MEMORY_SHORTAGES = [
    "Unix_error: Cannot allocate memory io_uring_queue_init",
    "Semgrep could not lock memory for 1 jobs.",
]


def short_of_memory(workflow: Workflow, message: str, times: int, stream: int = 2) -> None:
    """A real pre-push hook that fails `times` pushes with the scan's memory message.

    It logs each run with its EIO_BACKEND, so a test sees the hook ran and how it was retried.
    Like the real hook, it first prints the scan's command line, which is not the error.
    `stream` 1 prints the message on standard output, where git passes a pre-push hook's own.
    The hook lives in `workflow.bin`, not the repository: CI runs pytest in /dev/shm, where git
    finds a hook it cannot run and skips it.
    """
    (workflow.state / "memory-shortage").write_text(f"{times}\n")
    hooks = workflow.bin / "hooks"
    path = hooks / "pre-push"
    hooks.mkdir(exist_ok=True)
    workflow.git("config", "core.hooksPath", str(hooks))
    path.write_text(
        "#!/bin/bash\n"
        f'state="{workflow.state}"\n'
        'echo "pre-push EIO_BACKEND=${EIO_BACKEND:-}" >> "$state/calls.log"\n'
        "echo '$ sh scripts/security-scan.sh all' >&2\n"
        'left=$(cat "$state/memory-shortage")\n'
        'if [ "$left" -gt 0 ]; then\n'
        '  echo $((left - 1)) > "$state/memory-shortage"\n'
        f"  echo {json.dumps(message)}{' >&2' if stream == 2 else ''}\n"
        "  exit 1\n"
        "fi\n"
    )
    path.chmod(0o755)


def hook_runs(workflow: Workflow) -> list[str]:
    return [call.removeprefix("pre-push ") for call in workflow.calls("pre-push")]


def hook_runs_and_waits(workflow: Workflow) -> list[str]:
    """The hook runs and the gate's sleeps, in the order they happened."""
    calls = (workflow.state / "calls.log").read_text().splitlines()
    return [call for call in calls if call.startswith(("pre-push ", "sleep "))]


def _stale(github: GitHub) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old)


def _refusal(done: subprocess.CompletedProcess[str]) -> str:
    """The first line of the gate's message, which the wake and the hand-back show."""
    return next(line for line in done.stderr.splitlines() if line.startswith("merge "))


# The package audit's own last lines (scripts/audit-against-base.py): a service it could not
# reach after its tries, and an advisory the branch brings.
UNREACHABLE = (
    "Refused because npm's advisory service could not be reached; "
    "nothing was found wrong with this branch."
)
ADVISORY = "Refused: this branch brings advisories that main does not have:\n  npm: GHSA-1 in vite"


def _dependencies_red(github: GitHub, *, annotation: str | None) -> dict[str, Any]:
    """Main's CI run 10 failed only in "CI: dependencies", on a home runner, retried once."""
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 2
    parts = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]
    parts[1]["name"] = "CI: dependencies"
    for job in parts:
        job["labels"] = ["example-app-home-main"]
    github.api[f"{_REPO}/check-runs/12/annotations?per_page=100"] = (
        [{"title": "Advisory service unreachable", "message": annotation}] if annotation else []
    )
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main-rerun-10").write_text("automatically re-ran failed jobs of main run 10 once\n")
    github.save()
    return run


def advisory_service_answers(workflow: Workflow, *, answers: bool) -> None:
    """A stand-in for curl: the advisory service answers `{}`, or the request fails."""
    workflow.fake(
        "curl",
        "#!/bin/sh\n"
        f'echo "curl $*" >> "{workflow.state}/calls.log"\n'
        + ("echo '{}'\n" if answers else "echo 'curl: (22) 503' >&2; exit 22\n"),
    )


def rerun_calls(workflow: Workflow) -> list[str]:
    return [call for call in workflow.calls("gh") if "run rerun" in call]


_MAIN_UNREACHABLE = (
    "Refused because npm's advisory service could not be reached; "
    "nothing was found wrong with these packages."
)


def pull_dependencies_red(github: GitHub, *, annotation: str | None) -> dict[str, Any]:
    """Pull request 7's CI run 20 failed only in "CI: dependencies", on a home runner."""
    run = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    run.update(conclusion="failure", run_attempt=1)
    parts = github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"]
    parts[1]["conclusion"] = "failure"
    parts.append(
        {"id": 23, "name": "CI: dependencies", "status": "completed", "conclusion": "failure"}
    )
    for job in parts:
        job["labels"] = ["example-app-home"]
    github.api[f"{_REPO}/check-runs/23/annotations?per_page=100"] = (
        [{"title": "Advisory service unreachable", "message": annotation}] if annotation else []
    )
    rollup = github.pulls["7"]["statusCheckRollup"]
    rollup[0]["conclusion"] = "FAILURE"
    rollup.append({"name": "CI: dependencies", "status": "COMPLETED", "conclusion": "FAILURE"})
    github.save()
    return run


def _changes(github: GitHub, path: str = "web/src/page.tsx") -> str:
    """Pull request 7 changes a file and has green CI on current main; its tested tree."""
    workflow = github.workflow
    (workflow.repo / path).parent.mkdir(parents=True, exist_ok=True)
    (workflow.repo / path).write_text("changed\n")
    workflow.git("add", path)
    workflow.git("commit", "-m", "fix: seven changes a file")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    # The gate pushes and dispatches a scratch run as it does for a batch.
    github.batch_mode = True
    return workflow.git("rev-parse", f"{github.head}^{{tree}}")


def _suite(github: GitHub) -> dict[str, Any]:
    return _batch_state(github)["suites"]["7"]


def _prefix_only(github: GitHub, groups: list[str]) -> None:
    # Keep the repository's commands: its browser plan decides whether a change needs E2E.
    config = github.workflow.repo / "shipline.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    data.setdefault("checks", {})["prefix_only"] = groups
    config.write_text(json.dumps(data))


def _green_second(github: GitHub) -> Path:
    """Pull request 8 changes a file, has green checks on current main and a whole suite."""
    workflow = github.workflow
    second = workflow.root / "second"
    workflow.running("review-8")
    workflow.git("worktree", "add", "-b", "ticket/eight", str(second), github.base)
    (second / "web/src").mkdir(parents=True, exist_ok=True)
    (second / "web/src/other.tsx").write_text("eight\n")
    workflow.git("add", ".", cwd=second)
    workflow.git("commit", "-m", "fix: eight", cwd=second)
    head = workflow.git("rev-parse", "HEAD", cwd=second)
    workflow.git("push", "origin", "HEAD:ticket/eight", cwd=second)
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefOid": head,
        "headRefName": "ticket/eight",
    }
    github.ci(github.base, head, run_id=30)
    github.save()
    return second


def _selected_server_tests(github: GitHub, server: str | None = "selected: 4 of 374 files") -> str:
    """Pull request 7's own CI ran only the server tests its change reaches; its tested tree."""
    tree = _changes(github, "src/example_app/reader.py")
    github.ci(github.base, github.head, server=server)
    # A server change no spec reaches: its own E2E run selected nothing.
    github.e2e(github.head, tree, suite="selected: 0 of 48 spec files", run_id=900_020)
    return tree


def _server_suite_dependencies_red(github: GitHub, *, annotation: str | None) -> dict[str, Any]:
    """The run of every server test failed only in "CI: dependencies", on home runners."""
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000, python="success")
    run = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={suite['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0]
    run.update(conclusion="failure", run_attempt=1)
    parts = github.api[f"{_REPO}/actions/runs/960000/jobs?per_page=100"]["jobs"]
    parts[2]["conclusion"] = "failure"  # CI result
    parts.append(
        {"id": 960_004, "name": "CI: dependencies", "status": "completed", "conclusion": "failure"}
    )
    for job in parts:
        job["labels"] = ["example-app-home"]
    github.api[f"{_REPO}/check-runs/960004/annotations?per_page=100"] = (
        [{"title": "Advisory service unreachable", "message": annotation}] if annotation else []
    )
    github.save()
    return suite


def batch_dependencies_red(
    github: GitHub, *, annotation: str | None, reason: str = "", run_id: int = 100
) -> None:
    """The first prefix's CI run failed only in "CI: dependencies", on a home runner, at
    attempt 1: after `_finish_batch`, which gives every other job a pass."""
    batch = _scratch(github)
    assert batch is not None and batch["workflows"][0] == "ci"
    run = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={batch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0]
    run.update(conclusion="failure", run_attempt=1)
    parts = github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"]["jobs"]
    parts[1]["conclusion"] = "failure"
    parts.append(
        {
            "id": run_id + 3,
            "name": "CI: dependencies",
            "status": "completed",
            "conclusion": "failure",
        }
    )
    if reason == "another job failed too":
        parts.append(
            {"id": run_id + 4, "name": "CI: web", "status": "completed", "conclusion": "failure"}
        )
    for job in parts:
        job["labels"] = ["ubuntu-latest" if reason == "hosted" else "example-app-home"]
    github.api[f"{_REPO}/check-runs/{run_id + 3}/annotations?per_page=100"] = (
        [{"title": "Advisory service unreachable", "message": annotation}] if annotation else []
    )
    github.save()


# A reviewer's handoff checks its own pull request, then holds the merge lock only to join the
# line and save its note: the members already in the line are the tick's to read.


def _handing_off(github: GitHub) -> Path:
    """Pull request 7's reviewer in its worktree, with a passed round published on its head."""
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("worktree", "add", str(workflow.worktree(7)), "ticket/seven")
    github.api[f"{_REPO}/pulls/7/reviews"] = [
        {
            "body": "<!-- workflow-review round=1 result=passed -->\nReviewed.",
            "commit_id": github.head,
        }
    ]
    github.lock_probe = str(workflow.repo / ".local/workflow/merge.lock")
    github.save()
    return workflow.worktree(7)


def _hand_off(github: GitHub) -> subprocess.CompletedProcess[str]:
    github.save()
    return github.workflow.run_script(
        "merge-when-current", "--handoff", "7", cwd=github.workflow.worktree(7)
    )


def _locked_calls(workflow: Workflow) -> list[str]:
    log = workflow.state / "locked-calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _held(workflow: Workflow) -> list[float]:
    """How long each of pull request 7's handoffs held the merge lock, by the wake log."""
    text = workflow.wake_log.read_text() if workflow.wake_log.exists() else ""
    return [float(s) for s in re.findall(r"handoff 7 held the merge lock for ([0-9.]+) s", text)]


def _after_the_checks(workflow: Workflow, script: str) -> None:
    """Run `script` as the pre-train checks: after the handoff's own checks, before the lock."""
    workflow.fake("pre-train-checks", "#!/bin/bash\n" + script + "\nexit 0\n")


def _waiting(workflow: Workflow) -> list[str]:
    path = workflow.repo / ".local/workflow/merge-train.json"
    return json.loads(path.read_text())["waiting"] if path.exists() else []


def _red_ci(github: GitHub) -> None:
    """Its CI run failed, though GitHub still shows its checks green."""
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]["conclusion"] = "failure"
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"


def _budget(workflow: Workflow) -> Path:
    folder = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# Queued scratch jobs use their own timestamps, never the age of their workflow run.
def _queued_prefix_job(
    github: GitHub, *, minutes: int = 21, workflow: str = "ci", attempt: int = 1,
    label: str = "example-app-home-main-batch", number: str = "7",
) -> dict[str, Any]:
    member = next(member for member in _prefixes(github) if member["number"] == number)
    scratch = member["scratch"]
    identity = 100 if workflow == "ci" else 110
    run = {
        "id": identity, "head_sha": scratch["head"], "head_branch": scratch["branch"],
        "event": "workflow_dispatch", "status": "in_progress", "conclusion": None,
        "run_attempt": attempt,
    }
    github.api[
        f"{_REPO}/actions/workflows/{workflow}.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ] = {"workflow_runs": [run]}
    github.api[f"{_REPO}/actions/runs/{identity}/jobs?per_page=100"] = {"jobs": [{
        "id": identity + 2, "name": "CI: server tests (2 of 3)", "status": "queued",
        "conclusion": None, "labels": [label],
        "created_at": datetime.fromtimestamp(time.time() - minutes * 60, UTC).isoformat(),
    }]}
    return run


def _queue_config(github: GitHub, limit: int | None = None) -> None:
    path = github.workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config.setdefault("runner", {})["label"] = "example-app-home"
    if limit is not None:
        config["merge_train"] = {"queued_limit_minutes": limit}
    path.write_text(json.dumps(config))
    github.workflow.git("update-index", "--skip-worktree", "shipline.json")
