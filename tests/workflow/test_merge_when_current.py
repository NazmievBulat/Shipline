"""Merge safety with real Git histories and a stand-in for GitHub."""

import errno
import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from stand_in_server import StandInServer
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
    elif args[:2] == ["run", "rerun"]:
        if (state / "fail-rerun").exists():
            sys.exit("rerun refused")
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
        if path not in data["api"]:
            if "event=workflow_dispatch" in path or "/actions/runs?branch=" in path:
                print('{"workflow_runs": []}')
                sys.exit(0)
            sys.exit("unexpected API request: " + path)
        reply = data["api"][path]
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


def test_a_pull_request_tested_against_older_main_is_refreshed_not_merged(github: GitHub) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert not (workflow.state / "merged").exists()
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[
        0
    ] == workflow.git("rev-parse", "HEAD")
    assert "CI requested" in done.stdout


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


def test_only_the_front_of_the_train_refreshes_and_the_next_runs_after_merge(
    github: GitHub,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    before = workflow.git("rev-parse", "HEAD", cwd=second)
    waiting = workflow.run_script("merge-when-current", "8", cwd=second)
    assert waiting.returncode == 3, waiting.stdout + waiting.stderr
    assert "notify waiting" in waiting.stdout
    assert workflow.git("rev-parse", "HEAD", cwd=second) == before
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0] == before
    _refreshed_first(github)
    assert github.run().returncode == 0
    github.pulls["7"]["state"] = "MERGED"
    github.save()
    next_pull = workflow.run_script("merge-when-current", "8", cwd=second)
    assert next_pull.returncode == 2, next_pull.stdout + next_pull.stderr
    assert workflow.git("rev-parse", "HEAD^2", cwd=second) == github.base


def test_a_failed_front_leaves_the_train_and_rejoins_at_the_back_after_a_fix(
    github: GitHub,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.save()
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 2
    assert github.run().returncode == 1
    workflow.git("commit", "--allow-empty", "-m", "fix: failed check")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"].update(headRefOid=github.head, statusCheckRollup=[])
    assert github.run().returncode == 3


def test_the_train_survives_restarting_the_tick(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    status = workflow.run_script("merge-when-current", "--train-status")
    assert status.returncode == 0, status.stderr
    train = json.loads(status.stdout)
    assert train["first"] == "7"
    assert train["waiting"] == ["7", "8"]
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3


def test_the_tick_reads_the_saved_train_while_a_gate_holds_the_lock(github: GitHub) -> None:
    """A refresh pushes under the lock, and the push hooks take minutes."""
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    views = len(workflow.calls("gh pr view"))
    with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = workflow.run_script("merge-when-current", "--train-status")
    assert status.returncode == 0, status.stderr
    train = json.loads(status.stdout)
    assert train["first"] == "7"
    assert train["waiting"] == ["7", "8"]
    # Nothing was read from GitHub: reconciling waits for a tick that gets the lock.
    assert len(workflow.calls("gh pr view")) == views


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


@pytest.mark.parametrize("outcome", ["merged", "failed"])
def test_the_tick_wakes_the_next_turn_once_even_if_its_checks_already_woke_it(
    github: GitHub,
    outcome: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    done = workflow.run_script(
        "merge-when-current", "8", cwd=second, env={"WORKFLOW_SESSION": "review-8"}
    )
    assert done.returncode == 3, done.stderr
    assert (workflow.notes(8) / "review-8.waiting").exists()
    # The previous checks nudge sent the reviewer to the gate, where it had to wait.
    head = github.pulls["8"]["headRefOid"]
    (workflow.repo / f".local/workflow/review-8.checks-nudged-{head}").touch()
    assert workflow.run_script("tick").returncode == 0
    assert workflow.calls("tmux review-8 send-keys") == []
    if outcome == "merged":
        github.pulls["7"]["state"] = "MERGED"
    else:
        github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.save()
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    second_tick = workflow.run_script("tick")
    assert second_tick.returncode == 0, second_tick.stdout + second_tick.stderr
    assert len(workflow.calls("tmux review-8 send-keys")) == 2  # literal text and Enter


def test_the_front_is_woken_again_when_main_finishes_its_checks(github: GitHub) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    main_run = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"][0]
    main_run.update(status="in_progress", conclusion=None)
    assert github.run().returncode == 2
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-7 send-keys")) == 2
    main_run.update(status="completed", conclusion="success")
    github.save()
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-7 send-keys")) == 4


@pytest.mark.parametrize("change", ["closed", "draft", "stacked", "unreadable"])
def test_train_reconciliation_releases_ineligible_pulls_but_fails_closed_on_read_errors(
    github: GitHub,
    change: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    if change == "closed":
        github.pulls["7"]["state"] = "CLOSED"
    elif change == "draft":
        github.pulls["7"]["isDraft"] = True
    elif change == "stacked":
        github.pulls["7"]["baseRefName"] = "ticket/other"
    else:
        del github.pulls["7"]
    github.save()
    before = workflow.git("rev-parse", "HEAD", cwd=second)
    done = workflow.run_script("merge-when-current", "8", cwd=second)
    if change == "unreadable":
        assert done.returncode == 1
        assert workflow.git("rev-parse", "HEAD", cwd=second) == before
    else:
        assert done.returncode == 2, done.stderr


def test_a_new_reviewer_does_not_inherit_the_previous_reviewers_train_nudge(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7, draft=False)
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    mark = folder / "review-7.train-nudged-1-main-health-head"
    mark.touch()
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    assert not mark.exists()


@pytest.mark.parametrize("main_red", [True, False])
@pytest.mark.parametrize("proof", ["fresh", "partial", "skipped", "stale", "unreadable"])
def test_a_proven_fix_at_the_back_takes_the_red_main_turn(
    github: GitHub, main_red: bool, proof: str
) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    if proof == "stale":
        github.ci(github.base, head, tested_base=workflow.git("rev-parse", "main^"), run_id=30)
    if proof != "partial":
        github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
            {
                "id": 39,
                "name": "CI: web",
                "status": "completed",
                "conclusion": "skipped" if proof == "skipped" else "success",
            }
        )
    if proof == "unreadable":
        github.api[f"{_REPO}/check-runs/31/annotations?per_page=100"] = None
    github.main("failure" if main_red else "success")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert workflow.run_script("main-status").returncode == 0
    train = _train(github)
    fixed = main_red and proof == "fresh"
    assert train["first"] == ("8" if fixed else "7")
    # The waiting are listed in the order served, the fix first.
    assert train["waiting"] == (["8", "7"] if fixed else ["7", "8"])
    assert _train(github)["turn"] == train["turn"]
    # A separate status process also recovers the original order after main goes green.
    github.main()
    github.save()
    assert workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "7"
    assert _train(github)["waiting"] == ["7", "8"]


@pytest.mark.parametrize("warned", [True, False])
def test_a_pass_only_over_mains_advisory_does_not_take_the_red_main_turn(
    github: GitHub, warned: bool
) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": _DEPENDENCIES, "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = (
        [{"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}]
        if warned
        else []
    )
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": _DEPENDENCIES, "status": "completed", "conclusion": "failure"},
    ]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    # The gate would not merge a pass over main's own advisory as the fix, so it is not first.
    assert _train(github)["first"] == ("7" if warned else "8")


def test_a_red_main_judges_each_pull_request_once_until_its_checks_change(
    github: GitHub,
) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    # Pull request 7's own CI has not passed: nothing about it can prove a fix.
    github.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    github.main("failure")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))

    def reads(run: int) -> int:
        # The proof reads which merge a run tested. The first member's turn reads it too.
        return len(workflow.calls(f"gh api --paginate {_REPO}/check-runs/{run + 1}/annotations"))

    def first_and_reads(run: int) -> tuple[str, int]:
        before = reads(run)
        first = _train(github)["first"]
        return first, reads(run) - before

    assert workflow.run_script("main-status").returncode == 0
    assert reads(20) == 0
    first, judged = first_and_reads(30)
    assert first == "8"
    # The tick reads the train every 3 minutes; GitHub's shared budget pays for each read.
    assert first_and_reads(30) == ("8", judged - 1)
    assert reads(20) == 0
    # A changed check on the pull request is new evidence, judged again.
    github.pulls["8"]["statusCheckRollup"].append(
        {"name": "CI: web", "status": "COMPLETED", "conclusion": "SUCCESS"}
    )
    github.save()
    assert first_and_reads(30) == ("8", judged)


def _labelled(github: GitHub, labels: dict[str, list[str]]) -> None:
    for number, names in labels.items():
        github.pulls[number]["labels"] = [{"name": name} for name in names]
    github.save()


def _line(github: GitHub, waiting: list[str]) -> None:
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": waiting}))


@pytest.mark.parametrize(
    "labels", [{}, {"7": ["priority: low"], "8": ["priority: high"]}], ids=["none", "labelled"]
)
def test_multiple_red_main_fixes_keep_their_original_queue_order(
    github: GitHub, labels: dict[str, list[str]]
) -> None:
    """Every proven fix counts as the first blocker, whatever its label."""
    _two_stale_pulls(github)
    _labelled(github, labels)
    github.ci(github.base, github.head)
    _pr_job(github, "CI: web", "success")
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    github.main("failure")
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "7"
    assert json.loads((folder / "merge-train.json").read_text())["fixes"] == ["7", "8"]


@pytest.mark.parametrize(
    "labels,served,priorities",
    [
        # A later high goes before an earlier medium.
        ({"8": ["workflow", "priority: high"]}, ["8", "7"], {"7": "medium", "8": "high"}),
        # Within one priority, the order of joining.
        (
            {"7": ["priority: high"], "8": ["priority: high"]},
            ["7", "8"],
            {"7": "high", "8": "high"},
        ),
        # No label counts as medium: before a low, after a high.
        ({"7": ["priority: low"]}, ["8", "7"], {"7": "low", "8": "medium"}),
        ({"7": ["priority: medium"], "8": []}, ["7", "8"], {"7": "medium", "8": "medium"}),
        # A blocker before a high, whichever joined first.
        (
            {"7": ["priority: high"], "8": ["priority: blocker"]},
            ["8", "7"],
            {"7": "high", "8": "blocker"},
        ),
    ],
)
def test_the_train_serves_by_priority_then_by_the_order_of_joining(
    github: GitHub, labels: dict[str, list[str]], served: list[str], priorities: dict[str, str]
) -> None:
    _two_stale_pulls(github)
    _labelled(github, labels)
    _line(github, ["7", "8"])

    train = _train(github)

    assert train["first"] == served[0]
    assert train["waiting"] == served
    assert train["priorities"] == priorities
    # The order of joining is kept, so a priority that changes back restores it.
    saved = json.loads((github.workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert saved["waiting"] == ["7", "8"]


def test_a_label_changed_while_waiting_moves_the_pull_request_at_the_next_turn(
    github: GitHub,
) -> None:
    _two_stale_pulls(github)
    _labelled(github, {"8": ["priority: high"]})
    _line(github, ["7", "8"])
    before = _train(github)
    assert before["first"] == "8"

    _labelled(github, {"8": ["priority: low"]})
    after = _train(github)

    assert after["first"] == "7"
    assert after["waiting"] == ["7", "8"]
    assert after["priorities"]["8"] == "low"
    assert after["turn"] == before["turn"] + 1


def test_the_fix_of_a_red_main_is_first_whatever_its_label(github: GitHub) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    github.main("failure")
    _labelled(github, {"7": ["priority: blocker"], "8": ["priority: low"]})
    _line(github, ["7", "8"])
    assert github.workflow.run_script("main-status").returncode == 0

    train = _train(github)

    assert train["first"] == "8"
    assert train["waiting"] == ["8", "7"]
    # The status shows the priority it is served at, not its label.
    assert train["priorities"] == {"8": "blocker", "7": "blocker"}


def test_a_green_member_behind_a_pending_more_urgent_one_takes_the_turn(github: GitHub) -> None:
    """Behind a pending member means behind it in the order served, not in the order joined."""
    _two_stale_pulls(github)
    github.ci(github.base, github.head)  # 7: green, tested on current main.
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    runs = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
    github.api[runs]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    _labelled(github, {"8": ["priority: high"]})
    _line(github, ["7", "8"])

    train = _train(github)

    assert train["first"] == "7"
    # 8 keeps its place at the front of the line while its checks run.
    assert train["waiting"] == ["8", "7"]


def test_a_later_high_pull_request_is_served_before_an_earlier_medium_one(
    github: GitHub,
) -> None:
    """The gate itself: the earlier medium one waits while the later high one refreshes."""
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _labelled(github, {"8": ["workflow", "priority: high"]})
    assert github.run().returncode == 2  # 7 is alone in the line and refreshes.
    _refreshed_first(github)
    refreshed = workflow.git("rev-parse", "HEAD")

    joined = workflow.run_script("merge-when-current", "8", cwd=second)
    waiting = github.run()

    assert joined.returncode == 2, joined.stdout + joined.stderr
    assert "CI requested" in joined.stdout
    assert waiting.returncode == 3, waiting.stdout + waiting.stderr
    assert "waits behind 8" in waiting.stdout
    assert not (workflow.state / "merged").exists()
    assert workflow.git("rev-parse", "HEAD") == refreshed


@pytest.mark.parametrize("proof", ["fresh", "wrong-tree", "skipped"])
def test_an_image_fix_at_the_back_takes_the_red_main_turn(github: GitHub, proof: str) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    _main_image_run(github, 60, github.base, conclusion="failure")
    original_head = github.head
    github.head = head
    _images(github, tree="f" * 40 if proof == "wrong-tree" else None)
    github.head = original_head
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"].append(
        {
            "id": 44,
            "name": "Docker images: browser tests (1 of 2)",
            "status": "completed",
            "conclusion": "skipped" if proof == "skipped" else "success",
        }
    )
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == ("8" if proof == "fresh" else "7")


def test_the_tick_wakes_a_red_main_fix_ahead_of_the_queue(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    github.main("failure")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    # Both reviewers already received their checks wake; only the new turn should wake one.
    for number in ("7", "8"):
        (folder / f"review-{number}.checks-nudged-{github.pulls[number]['headRefOid']}").touch()
        (workflow.notes(int(number)) / f"review-{number}.waiting").touch()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _train(github)["first"] == "8"
    assert len(workflow.calls("tmux review-8 send-keys")) == 2
    assert workflow.calls("tmux review-7 send-keys") == []
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-8 send-keys")) == 2
    merged = workflow.run_script("merge-when-current", "8", cwd=second)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert (workflow.state / "merged").read_text() == "8"


def test_a_front_that_cannot_fix_red_main_lets_the_next_candidate_try(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.main("failure")
    refused = github.run()
    assert refused.returncode == 1
    assert "merging stopped: main: red" in refused.stderr
    assert not (workflow.state / "merged").exists()
    next_pull = workflow.run_script("merge-when-current", "8", cwd=second)
    assert next_pull.returncode == 2, next_pull.stdout + next_pull.stderr


def test_a_red_main_candidate_that_joins_later_can_take_the_turn(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    assert github.run().returncode == 2
    _refreshed_first(github)
    github.main("failure")
    assert github.run().returncode == 1
    next_pull = github.workflow.run_script("merge-when-current", "8", cwd=second)
    assert next_pull.returncode == 2, next_pull.stdout + next_pull.stderr


def test_red_main_candidates_sleep_until_main_or_their_head_changes(github: GitHub) -> None:
    _two_stale_pulls(github)
    assert github.run().returncode == 2
    _refreshed_first(github)
    github.main("failure")
    assert github.run().returncode == 1
    workflow = github.workflow
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] is None
    github.main()
    github.save()
    assert workflow.run_script("main-status").returncode == 0
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "7"
    assert github.run().returncode == 0


@pytest.mark.parametrize("outcome", ["merged", "failed", "self-failed"])
def test_an_unlabelled_audit_is_woken_when_its_train_turn_arrives(
    github: GitHub,
    outcome: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    workflow.list_pull_request(8, draft=False, labels=())
    workflow.running("audit-8")
    (workflow.state / "tmux/review-8").unlink()
    workflow.screen("● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "audit-8")
    assert github.run().returncode == 2
    waiting = workflow.run_script(
        "merge-when-current", "8", cwd=second, env={"WORKFLOW_SESSION": "audit-8"}
    )
    assert waiting.returncode == 3
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Waiting."}, "audit-8")
    assert (workflow.notes(8) / "audit-8.checks-wait").exists()
    assert workflow.run_script("tick").returncode == 0
    assert workflow.calls("tmux audit-8 send-keys") == []
    if outcome == "merged":
        github.pulls["7"]["state"] = "MERGED"
    elif outcome == "failed":
        github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    else:
        github.pulls["8"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        rollup = workflow.state / "rollup-8.json"
        checks = json.loads(rollup.read_text())
        checks[0]["conclusion"] = "FAILURE"
        rollup.write_text(json.dumps(checks))
    github.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux audit-8 send-keys")) == 2
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux audit-8 send-keys")) == 2


def test_a_stale_pull_request_is_refreshed_while_main_ci_is_still_running(
    github: GitHub,
) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    [main_run] = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"]
    main_run.update(status="in_progress", conclusion=None)
    github.ci(github.base, github.head, tested_base=old)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert "main: pending" in done.stdout
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    assert not (workflow.state / "merged").exists()


def test_a_fresh_pull_request_waits_for_main_ci_that_is_still_running(github: GitHub) -> None:
    [main_run] = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"]
    main_run.update(status="in_progress", conclusion=None)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: pending" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_fresh_pull_request_merges_with_its_head_pinned(github: GitHub) -> None:
    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert any(
        f"--squash --match-head-commit {github.head}" in call
        for call in github.workflow.calls("gh pr merge")
    )


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_a_red_main_refuses_to_merge_and_reports_why(github: GitHub, conclusion: str) -> None:
    github.main(conclusion)

    done = github.run()

    assert done.returncode == 1
    assert "main: red since 2026-10-16T09:00:00Z, CI result, CI: web" in done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert "main: red" in (github.workflow.repo / ".local/workflow/main-status").read_text()


def _pr_job(github: GitHub, name: str, conclusion: str) -> None:
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"].append(
        {"id": 29, "name": name, "status": "completed", "conclusion": conclusion}
    )


def test_a_fix_that_passed_every_check_failing_on_red_main_merges(github: GitHub) -> None:
    github.main("failure")
    _pr_job(github, "CI: web", "success")

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert "passed every check failing on main: CI result, CI: web" in done.stdout


def _rerun_failed_jobs(
    github: GitHub,
    run_id: int,
    head: str,
    *carried: str,
    earlier_head: str | None = None,
    attempts: int = 2,
) -> None:
    """Run `run_id` failed a job and re-ran its failed jobs alone until its last attempt,
    `attempts`, passed. Each re-run carried the `carried` jobs from the first attempt under a
    new job ID, without notes. The first attempt ran on `earlier_head`, the head of the run
    unless a test says otherwise."""
    for reply in github.api.values():
        for run in reply.get("workflow_runs", []) if isinstance(reply, dict) else []:
            if run["id"] == run_id:
                run["run_attempt"] = attempts
    latest = github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"]["jobs"]
    for attempt in range(1, attempts + 1):
        listed = deepcopy(latest) if attempt < attempts else latest
        for job in listed:
            job.update(
                run_id=run_id,
                run_attempt=attempt,
                head_sha=earlier_head or head if attempt == 1 else head,
            )
            if job["name"] not in carried and attempt < attempts:
                job["conclusion"] = "failure"
            if job["name"] in carried and attempt > 1:
                job["id"] = run_id + 500 * (attempt - 1) + job["id"] % 100
                github.api[f"{_REPO}/check-runs/{job['id']}/annotations?per_page=100"] = []
        if attempt < attempts:
            github.api[f"{_REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"] = {
                "jobs": listed
            }


@pytest.mark.parametrize("earlier", ["same", "same, re-run twice", "other main", "other head"])
def test_a_fix_whose_failed_jobs_alone_were_rerun_merges_on_red_main(
    github: GitHub, earlier: str
) -> None:
    workflow = github.workflow
    github.main("failure")
    if earlier == "other main":
        github.ci(github.base, github.head, tested_base=workflow.git("rev-parse", "main^"))
    _pr_job(github, "CI: web", "success")
    _rerun_failed_jobs(
        github,
        20,
        github.head,
        "CI: what to run",
        earlier_head=f"{9:040x}" if earlier == "other head" else None,
        attempts=3 if earlier == "same, re-run twice" else 2,
    )

    done = github.run()

    if earlier.startswith("same"):
        assert done.returncode == 0, done.stdout + done.stderr
        assert (workflow.state / "merged").read_text() == "7"
        assert "passed every check failing on main: CI result, CI: web" in done.stdout
    else:
        # The first attempt tested another merge: the pull request is refreshed, not merged.
        assert done.returncode == 2, done.stdout + done.stderr
        assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("earlier", ["same", "other main", "other head"])
def test_a_fix_whose_failed_jobs_alone_were_rerun_takes_the_red_main_turn(
    github: GitHub, earlier: str
) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    head = github.pulls["8"]["headRefOid"]
    github.ci(
        github.base,
        head,
        run_id=30,
        tested_base=workflow.git("rev-parse", "main^") if earlier == "other main" else None,
    )
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    _rerun_failed_jobs(
        github,
        30,
        head,
        "CI: what to run",
        earlier_head=f"{9:040x}" if earlier == "other head" else None,
    )
    github.main("failure")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == ("8" if earlier == "same" else "7")


def test_a_pass_over_mains_advisory_carried_into_a_rerun_is_not_the_fix(
    github: GitHub,
) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: dependencies", "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = [
        {"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}
    ]
    # Its warning is on the first attempt; the re-run carried the job without it.
    _rerun_failed_jobs(github, 30, head, "CI: what to run", "CI: dependencies")
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": "CI: dependencies", "status": "completed", "conclusion": "failure"},
    ]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "7"


@pytest.mark.parametrize("conclusion", ["skipped", None])
def test_a_pull_request_that_did_not_pass_main_failing_check_stays_blocked(
    github: GitHub, conclusion: str | None
) -> None:
    github.main("failure")
    if conclusion:
        _pr_job(github, "CI: web", conclusion)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert "CI: web" in done.stderr
    assert not (github.workflow.state / "merged").exists()


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


def test_a_pull_request_that_passed_only_over_mains_advisory_is_not_its_fix(
    github: GitHub,
) -> None:
    _main_red_on_an_advisory(github, warned=True)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_pull_request_whose_audit_is_clean_on_a_red_main_is_its_fix(github: GitHub) -> None:
    _main_red_on_an_advisory(github, warned=False)

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"passed every check failing on main: CI result, {_DEPENDENCIES}" in done.stdout


def test_an_audit_passed_over_mains_advisory_is_not_inherited_by_main(github: GitHub) -> None:
    _pr_job(github, _DEPENDENCIES, "success")
    github.api[f"{_REPO}/check-runs/29/annotations?per_page=100"] = [
        {"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}
    ]

    _squash_with_pending_main(github)

    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    assert _DEPENDENCIES not in receipt["ci_passed"]
    assert receipt["ci_unproven"] == [_DEPENDENCIES]
    _main_jobs(github, (_DEPENDENCIES, "completed", "failure"))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout


def test_a_clean_audit_is_inherited_by_main(github: GitHub) -> None:
    _pr_job(github, _DEPENDENCIES, "success")
    github.api[f"{_REPO}/check-runs/29/annotations?per_page=100"] = []

    _squash_with_pending_main(github)

    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    assert _DEPENDENCIES in receipt["ci_passed"]
    assert receipt["ci_unproven"] == []


def test_a_stale_pull_request_is_refreshed_on_a_red_main(github: GitHub) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main("failure")
    github.ci(github.base, github.head, tested_base=old)
    _pr_job(github, "CI: web", "success")

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    assert not (workflow.state / "merged").exists()


def test_merging_goes_on_after_main_is_green_again(github: GitHub) -> None:
    github.main("failure")
    assert github.run().returncode == 1
    github.main()

    done = github.run()

    assert done.returncode == 0, done.stderr
    assert (github.workflow.state / "merged").exists()


def test_missing_ci_on_main_is_never_read_as_green(github: GitHub) -> None:
    github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": []
    }
    done = github.run()
    assert done.returncode == 1
    assert "main: pending" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_failed_github_read_never_permits_merging(github: GitHub) -> None:
    github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"] = None
    done = github.run()
    assert done.returncode == 1
    assert "main: unknown" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_missing_provenance_requests_new_ci_even_when_main_is_already_in_the_branch(
    github: GitHub,
) -> None:
    github.api[f"{_REPO}/check-runs/21/annotations?per_page=100"] = []
    done = github.run()
    assert done.returncode == 2, done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert github.workflow.git("rev-parse", "HEAD") != github.head
    assert "CI requested" in done.stdout


def test_a_check_of_a_different_pr_head_cannot_prove_freshness(github: GitHub) -> None:
    github.ci(github.base, github.base)
    path = (
        f"{_REPO}/actions/workflows/ci.yml/runs"
        f"?head_sha={github.head}&event=pull_request&per_page=100"
    )
    cast_runs: dict[str, Any] = github.api[path]
    cast_runs["workflow_runs"] = []
    done = github.run()
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_a_pending_or_failed_check_stops_even_a_fresh_pr(github: GitHub) -> None:
    pull: dict[str, Any] = github.pulls["7"]
    pull["statusCheckRollup"].append(
        {"name": "playwright", "status": "IN_PROGRESS", "conclusion": None}
    )
    done = github.run()
    assert done.returncode == 1
    assert "playwright" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_red_main_report_names_commits_since_last_green(github: GitHub) -> None:
    workflow = github.workflow
    green = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: first incompatible change")
    workflow.git("commit", "--allow-empty", "-m", "fix: second incompatible change")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main("failure")
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=100"
    ] = {"workflow_runs": [{"id": 5, "head_sha": green}]}
    github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={green}&per_page=100"] = {
        "workflow_runs": [
            {
                "id": 5,
                "head_sha": green,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    github.api[f"{_REPO}/actions/runs/5/jobs?per_page=100"] = {
        "jobs": [{"id": 6, "name": "CI result", "status": "completed", "conclusion": "success"}]
    }
    github.save()

    done = workflow.run_script("main-status")

    assert done.returncode == 0
    assert f"commits since last green main {green[:12]}" in done.stdout
    assert "fix: first incompatible change;" in done.stdout
    assert "fix: second incompatible change" in done.stdout


def test_the_tick_reports_a_red_main_through_the_wake_during_the_day(github: GitHub) -> None:
    workflow = github.workflow
    github.main("failure")
    github.save()
    workflow.orchestrator()

    done = workflow.run_script("tick", env={"TZ": "UTC", "WORKFLOW_NOW": "1792152000"})

    assert done.returncode == 0, done.stderr
    assert "main: red" in done.stdout
    [message] = workflow.sent()
    assert "CI: web" in message and "main: red since" in message
    assert "last green main" in message
    assert not (workflow.repo / ".local/workflow/main-status.pending").exists()


def test_only_a_red_or_unreadable_main_and_its_recovery_wake_the_orchestrator(
    github: GitHub,
) -> None:
    folder = github.workflow.repo / ".local/workflow"
    pending = folder / "main-status.pending"
    gate = github.workflow.repo / "scripts/workflow/merge_gate.py"
    published: dict[str, bool] = {}
    for line in [
        "main: green (aaaaaaaaaaaa)",
        "main: pending, CI result has not started (bbbbbbbbbbbb)",
        "main: green (bbbbbbbbbbbb)",
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc)",
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc); history read",
        "main: unknown, gh failed",
        "main: pending, CI result (dddddddddddd)",
        "main: green (dddddddddddd)",
    ]:
        pending.unlink(missing_ok=True)
        state = line.split(",")[0].split(" ")[1]
        subprocess.run(
            [
                "python3",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
                "from pathlib import Path; "
                "g.publish(Path(sys.argv[2]), g.Health(sys.argv[3], sys.argv[4]))",
                str(gate.parent),
                str(folder),
                state,
                line,
            ],
            check=True,
        )
        published[line] = pending.exists()

    assert published == {
        "main: green (aaaaaaaaaaaa)": False,
        "main: pending, CI result has not started (bbbbbbbbbbbb)": False,
        "main: green (bbbbbbbbbbbb)": False,
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc)": True,
        "main: red since 2026-10-16T09:00:00Z, CI: web (cccccccccccc); history read": False,
        "main: unknown, gh failed": True,
        "main: pending, CI result (dddddddddddd)": True,
        "main: green (dddddddddddd)": False,
    }


def test_a_conflict_keeps_the_required_merge_message_until_the_session_resolves_it(
    github: GitHub,
) -> None:
    workflow = github.workflow
    shared = workflow.repo / "shared.txt"
    shared.write_text("the pull request\n")
    workflow.git("commit", "-am", "fix: branch edit")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    workflow.git("checkout", "main")
    shared.write_text("main changed it\n")
    workflow.git("commit", "-am", "fix: main edit")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.pulls["7"].update(
        headRefOid=github.head, mergeable="CONFLICTING", mergeStateStatus="DIRTY"
    )
    github.main()

    refused = github.run()

    assert refused.returncode == 1
    assert "resolve the conflicts in shared.txt" in refused.stderr
    assert not (workflow.state / "merged").exists()
    shared.write_text("the resolved edit\n")
    workflow.git("add", "shared.txt")
    workflow.git("commit", "--no-edit")
    assert workflow.git("log", "-1", "--format=%s") == "merge main: conflict in shared.txt"
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"].update(
        headRefOid=github.head, mergeable="MERGEABLE", mergeStateStatus="CLEAN"
    )
    github.ci(github.base, github.head)
    assert github.run().returncode == 0


def test_two_racing_merges_wait_and_the_second_rechecks_main(github: GitHub) -> None:
    workflow = github.workflow
    workflow.running("review-8")
    second_worktree = workflow.root / "second"
    workflow.git("worktree", "add", "-b", "ticket/eight", str(second_worktree), github.base)
    workflow.git("commit", "--allow-empty", "-m", "fix: eight", cwd=second_worktree)
    second_head = workflow.git("rev-parse", "HEAD", cwd=second_worktree)
    workflow.git("push", "origin", "HEAD:ticket/eight", cwd=second_worktree)
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefOid": second_head,
        "headRefName": "ticket/eight",
    }
    github.ci(github.base, second_head, run_id=30)
    old = github.base
    github.base = workflow.git(
        "commit-tree",
        workflow.git("rev-parse", f"{old}^{{tree}}"),
        "-p",
        old,
        "-m",
        "fix: seven merged",
    )
    github.next_main = github.base
    workflow.git("push", "origin", f"{github.base}:refs/heads/prepared-merge")
    github.main()
    github.save()
    entered = workflow.state / "merge-entered"
    release = workflow.state / "merge-continue"
    os.mkfifo(entered)
    os.mkfifo(release)
    (workflow.state / "block-first-merge").touch()
    entered_fd = os.open(entered, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    processes: list[subprocess.Popen[str]] = []
    try:
        first = subprocess.Popen(
            ["bash", str(workflow.script.with_name("merge-when-current")), "7"],
            cwd=workflow.repo,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(first)
        assert select.select([entered_fd], [], [], 15)[0], "first merge never reached GitHub"
        assert os.read(entered_fd, 100) == b"entered\n"
        second = subprocess.Popen(
            ["bash", str(workflow.script.with_name("merge-when-current")), "8"],
            cwd=second_worktree,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(second)
        assert second.stdout is not None
        assert select.select([second.stdout], [], [], 15)[0]
        assert second.stdout.readline().strip() == "waiting for the merge lock"
        assert not workflow.calls("gh pr view 8")
        os.write(release_fd, b"x")
        first_output = first.communicate(timeout=15)
        second_output = second.communicate(timeout=15)
        assert first.returncode == 0, first_output
        assert second.returncode == 2, second_output
        assert (workflow.state / "merged").read_text() == "7"
        assert workflow.git("rev-parse", "HEAD^2", cwd=second_worktree) == github.base
        assert "CI requested" in second_output[0]
        assert len(workflow.calls("gh pr merge")) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        os.close(entered_fd)
        os.close(release_fd)


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


def test_a_failed_image_check_on_main_stops_merging(github: GitHub) -> None:
    _images(github, pull=False, conclusion="failure")
    done = github.run()
    assert done.returncode == 1
    assert "main: red since" in done.stderr and "Docker images: image tests" in done.stderr


def test_images_passed_on_the_tested_tree_allow_merging_with_unused_jobs_skipped(
    github: GitHub,
) -> None:
    _images(github)
    done = github.run()
    assert done.returncode == 0, done.stderr


def test_images_whose_failed_jobs_alone_were_rerun_still_prove_their_tree(
    github: GitHub,
) -> None:
    _images(github)
    _rerun_failed_jobs(github, 40, github.head, "Docker images: what to run")
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"


def test_images_of_an_older_merge_tree_request_new_ci(github: GitHub) -> None:
    _images(github, tree="old-tree")
    done = github.run()
    assert done.returncode == 2, done.stderr
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize(
    "filename", ["Dockerfile", "é.txt", "tab\tname.txt", "line\nname.txt", "raw\udcff.txt"]
)
def test_a_required_image_run_that_has_not_started_is_waited_for(
    github: GitHub, filename: str
) -> None:
    workflow = github.workflow
    workflow.git("config", "core.quotePath", "true")
    image_input = workflow.repo / "docker" / filename
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    done = github.run()
    assert done.returncode == 1
    assert "Docker images checks have not started" in done.stderr
    assert not (workflow.state / "merged").exists()


def test_a_failure_on_the_second_page_of_jobs_stops_merging(github: GitHub) -> None:
    endpoint = f"{_REPO}/actions/runs/20/jobs?per_page=100"
    first: dict[str, Any] = github.api[endpoint]
    github.api[endpoint] = {
        "_pages": [
            first,
            {
                "jobs": [
                    {"id": 24, "name": "CI: web", "status": "completed", "conclusion": "failure"}
                ]
            },
        ]
    }
    done = github.run()
    assert done.returncode == 1
    assert "latest CI run" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_merge_provenance_on_the_second_page_is_read(github: GitHub) -> None:
    endpoint = f"{_REPO}/check-runs/21/annotations?per_page=100"
    original = github.api[endpoint]
    github.api[endpoint] = {
        "_pages": [[{"title": "other notice", "message": "unrelated"}], original]
    }
    done = github.run()
    assert done.returncode == 0, done.stderr


def test_main_turning_red_while_pr_checks_are_read_stops_the_merge(github: GitHub) -> None:
    github.main("failure")
    github.save()
    after = github.workflow.state / "github-after-annotations.json"
    after.write_text((github.workflow.state / "github.json").read_text())
    github.main()
    done = github.run()
    assert done.returncode == 1
    assert "main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


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


def test_a_failed_image_run_keeps_main_red_after_a_push_without_images(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: red since 2026-10-18T07:30:23Z"), status.stdout
    assert "Docker images: browser tests (1 of 2)" in status.stdout
    assert f"Docker images run 60 of {earlier[:12]}" in status.stdout
    assert done.returncode == 1
    assert "merging stopped: main: red" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_later_passing_image_run_on_main_turns_it_green_again(github: GitHub) -> None:
    failed = _push_without_images(github)
    _main_image_run(github, 60, failed, conclusion="failure")
    passed = _push_without_images(github)
    _main_image_run(github, 70, passed)
    _push_without_images(github)
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: green"), status.stdout
    assert done.returncode == 0, done.stderr
    assert (github.workflow.state / "merged").exists()


def test_a_green_rerun_of_the_failed_image_run_turns_main_green(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    github.save()
    assert github.workflow.run_script("main-status").stdout.startswith("main: red")
    # A re-run keeps its run id and replaces its conclusion.
    _main_image_run(github, 60, earlier)
    del github.api[_MAIN_IMAGES]["workflow_runs"][1]
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert github.run().returncode == 0


def test_an_image_run_in_progress_on_an_earlier_push_holds_main_pending(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier)
    _main_image_run(
        github, 60, earlier, status="in_progress", conclusion=None, event="workflow_dispatch"
    )
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: pending"), status.stdout
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_a_failed_image_run_behind_one_in_progress_keeps_main_red(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, conclusion="failure")
    _main_image_run(
        github, 60, earlier, status="in_progress", conclusion=None, event="workflow_dispatch"
    )
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: red")
    assert github.run().returncode == 1
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("receipt", [False, True])
def test_superseded_image_cancellation_waits_or_inherits(github: GitHub, receipt: bool) -> None:
    if receipt:
        _images(github, release_conclusion="success")
        earlier = github.base
        _squash_with_pending_main(github)
    else:
        earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, conclusion="cancelled")
    _main_image_run(github, 60, github.base, status="in_progress", conclusion=None)
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [github.api[_MAIN_IMAGES]["workflow_runs"][0]]
    }
    github.save()

    status = github.workflow.run_script("main-status")

    expected = "green" if receipt else "pending"
    assert status.stdout.startswith(f"main: {expected}"), status.stdout + status.stderr
    if receipt:
        assert "Docker images run 40" in status.stdout
    else:
        assert github.run().returncode == 1
        assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("newer", ["none", "push", "schedule", "pull_request", "unrelated"])
@pytest.mark.parametrize("job_conclusion", ["cancelled", "failure"])
def test_only_superseded_image_cancellations_are_pending_and_never_retried(
    github: GitHub, newer: str, job_conclusion: str
) -> None:
    # Keep the cancelled run on current main so the existing main-moved retry guard
    # cannot make this test pass by itself.
    _main_image_run(github, 50, github.base, conclusion="cancelled")
    cancelled = github.api[_MAIN_IMAGES]["workflow_runs"][0]
    cancelled["run_attempt"] = 1
    parts = github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"]["jobs"]
    parts[1].update(conclusion=job_conclusion, labels=["example-app-home-docker"])
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [cancelled]
    }
    if newer != "none":
        _main_image_run(
            github,
            60,
            "f" * 40 if newer == "unrelated" else github.base,
            status="in_progress",
            conclusion=None,
            event=newer if newer in ("schedule", "pull_request") else "push",
        )
        if newer == "push":
            github.api[
                f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
            ]["workflow_runs"].insert(0, github.api[_MAIN_IMAGES]["workflow_runs"][0])
    github.save()

    status = github.workflow.run_script("main-status")
    expected = "pending" if newer == "push" and job_conclusion == "cancelled" else "red"
    assert status.stdout.startswith(f"main: {expected}"), status.stdout + status.stderr
    github.workflow.run_script("tick")
    rerun_calls = [call for call in github.workflow.calls("gh") if "run rerun" in call]
    if newer == "push":
        assert not rerun_calls
        assert not (github.workflow.repo / ".local/workflow/main-rerun-50").exists()
    else:
        assert rerun_calls == ["gh run rerun 50 -R example-org/example-app --failed"]


def test_an_earlier_green_image_run_does_not_stand_in_for_the_heads_own(github: GitHub) -> None:
    workflow = github.workflow
    earlier = github.base
    workflow.git("checkout", "main")
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: main image input")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head)
    _main_image_run(github, 50, earlier)
    github.save()

    assert "main: pending, Docker images have not started" in (
        workflow.run_script("main-status").stdout
    )
    assert github.run().returncode == 1


def test_scheduled_and_unrelated_image_runs_do_not_decide_main(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier)
    _main_image_run(github, 60, earlier, conclusion="failure", event="schedule")
    # A run of a commit that is not in main's history (an old force-push) proves nothing.
    _main_image_run(github, 70, "f" * 40, conclusion="failure")
    _main_image_run(github, 80, earlier, conclusion="failure", event="pull_request")
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert github.run().returncode == 0


def test_a_failed_image_run_on_a_later_page_still_keeps_main_red(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    first = github.api[_MAIN_IMAGES]
    github.api[_MAIN_IMAGES.replace("&page=1", "&page=2")] = first
    # A hundred nightly runs fill the first page and push the failure onto the second.
    github.api[_MAIN_IMAGES] = {
        "workflow_runs": [
            {
                "id": 1000 - night,
                "head_sha": github.base,
                "head_branch": "main",
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
            }
            for night in range(100)
        ]
    }
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: red")
    assert github.run().returncode == 1


def test_a_fix_whose_images_passed_the_check_failing_on_main_merges(github: GitHub) -> None:
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    _images(github)
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"].append(
        {
            "id": 44,
            "name": "Docker images: browser tests (1 of 2)",
            "status": "completed",
            "conclusion": "success",
        }
    )

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        "passed every check failing on main: Docker images: browser tests (1 of 2)" in done.stdout
    )


def test_a_rerun_in_progress_of_mains_latest_image_run_holds_main_pending(
    github: GitHub,
) -> None:
    # The re-run's first attempt was main's latest completed image run: the older failure it
    # replaced must not come back while it runs again, nor let a "fix" merge meanwhile.
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, conclusion="failure")
    _main_image_run(github, 60, github.base, status="in_progress", conclusion=None)
    head_run = github.api[_MAIN_IMAGES]["workflow_runs"][0]
    head_run["run_attempt"] = 2
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [head_run]
    }
    _images(github)
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"].append(
        {
            "id": 44,
            "name": "Docker images: browser tests (1 of 2)",
            "status": "completed",
            "conclusion": "success",
        }
    )
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: pending"), status.stdout
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_an_older_image_run_running_again_behind_a_newer_completed_one_does_not_hold_main(
    github: GitHub,
) -> None:
    # The newer completed run decides main; whatever the older one finds, it cannot change that.
    earlier = _push_without_images(github)
    _main_image_run(github, 50, earlier, status="in_progress", conclusion=None)
    github.api[_MAIN_IMAGES]["workflow_runs"][0]["run_attempt"] = 2
    _main_image_run(github, 60, earlier)
    github.save()

    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    assert github.run().returncode == 0


def test_the_tick_reports_a_failed_image_run_after_a_push_without_images(github: GitHub) -> None:
    workflow = github.workflow
    earlier = _push_without_images(github)
    _main_image_run(github, 60, earlier, conclusion="failure")
    github.save()
    workflow.orchestrator()

    done = workflow.run_script("tick", env={"TZ": "UTC", "WORKFLOW_NOW": "1792152000"})

    assert done.returncode == 0, done.stderr
    [message] = workflow.sent()
    assert "main: red since" in message
    assert "Docker images: browser tests (1 of 2)" in message


@pytest.mark.parametrize(
    "filename", ["Dockerfile", "é.txt", "tab\tname.txt", "line\nname.txt", "raw\udcff.txt"]
)
@pytest.mark.parametrize("initial_push", [False, True])
def test_main_waits_for_required_images_of_the_whole_push(
    github: GitHub, filename: str, initial_push: bool
) -> None:
    workflow = github.workflow
    before = "0" * 40 if initial_push else github.base
    workflow.git("config", "core.quotePath", "true")
    workflow.git("checkout", "main")
    image_input = workflow.repo / "docker" / filename
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: main image input")
    # A push can contain more than one commit. Its last commit alone misses this image input.
    workflow.git("commit", "--allow-empty", "-m", "chore: next commit in the same push")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main()
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"].append(
        {"id": 13, "name": "CI: what to run", "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/13/annotations?per_page=100"] = [
        {"title": "Push base", "message": before}
    ]
    github.save()

    status = workflow.run_script("main-status")
    refused = github.run()

    assert "main: pending, Docker images" in status.stdout
    assert refused.returncode == 1
    assert not (workflow.state / "merged").exists()
    _images(github, pull=False)
    github.save()
    assert "main: green" in workflow.run_script("main-status").stdout


@pytest.mark.parametrize(
    "block", ["owner-blocked", "interaction-blocked", "pushed", "stopped", "limit"]
)
def test_an_unavailable_front_yields_and_recovers_its_old_place(
    github: GitHub,
    block: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    if block == "stopped":
        marker = workflow.state / "tmux/review-7"
        marker.unlink()
        github.api[f"{_REPO}/pulls/7/reviews"] = []
        github.save()
    elif block == "limit":
        # Listed as `limit` by its own line on the screen; the default screen comes back after.
        marker = workflow.state / "screen/review-7"
        workflow.screen(
            "  ⎿  You've hit your session limit\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n",
            "review-7",
        )
    else:
        marker = workflow.notes(7) / f"review-7.{block}"
        marker.touch()
    if block != "stopped":
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert len(workflow.calls("tmux review-8 send-keys")) == 2
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "8"
    refreshed = workflow.run_script("merge-when-current", "8", cwd=second)
    assert refreshed.returncode == 2, refreshed.stderr
    if block == "stopped":
        marker.touch()
    else:
        marker.unlink()
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "7"
    assert json.loads(status.stdout)["waiting"] == ["7", "8"]


def test_a_conflicted_front_lets_the_next_refresh_and_returns_after_resolution(
    github: GitHub,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    shared = workflow.repo / "shared.txt"
    shared.write_text("branch edit\n")
    workflow.git("commit", "-am", "fix: branch edit")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("checkout", "main")
    shared.write_text("main edit\n")
    workflow.git("commit", "-am", "fix: main edit")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.pulls["7"].update(headRefOid=github.head, mergeable="CONFLICTING")
    github.main()
    refused = github.run()
    assert refused.returncode == 1
    assert "resolve the conflicts" in refused.stderr
    refreshed = workflow.run_script("merge-when-current", "8", cwd=second)
    assert refreshed.returncode == 2, refreshed.stdout + refreshed.stderr
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "8"
    shared.write_text("resolved\n")
    workflow.git("add", "shared.txt")
    workflow.git("commit", "--no-edit")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.pulls["7"].update(mergeable="MERGEABLE")
    _refreshed_first(github)
    assert github.run().returncode == 0


@pytest.mark.parametrize("block", ["owner-blocked", "interaction-blocked", "pushed"])
def test_the_session_calling_the_gate_is_not_waiting_for_the_owner(
    github: GitHub,
    block: str,
) -> None:
    # A permission dialog answered at the machine, or a wake by the session's own job, leaves
    # a note behind while the session runs the gate itself.
    notes = github.workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"review-7.{block}").touch()

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").exists()


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


def test_a_scheduled_image_run_in_progress_does_not_hold_main(github: GitHub) -> None:
    _images(github, pull=False)
    _scheduled_images(github)
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert status.stdout.startswith("main: green"), status.stdout
    assert done.returncode == 0, done.stderr


def test_a_failed_scheduled_image_run_is_reported_once_and_does_not_close_the_gate(
    github: GitHub,
) -> None:
    folder = github.workflow.repo / ".local/workflow"
    _images(github, pull=False)
    _scheduled_images(github, status="completed", conclusion="failure")
    github.save()

    status = github.workflow.run_script("main-status")

    assert status.stdout.startswith("main: green"), status.stdout
    assert "scheduled Docker images run 50 failed" in status.stdout
    assert "Docker images: browser tests (1 of 2)" in status.stdout
    report = (folder / "main-status.pending").read_text()
    assert "scheduled Docker images run 50 failed" in report
    (folder / "main-status.pending").unlink()
    github.workflow.run_script("main-status")
    assert not (folder / "main-status.pending").exists()
    assert github.run().returncode == 0


def test_main_still_waits_for_the_image_run_of_its_push(github: GitHub) -> None:
    _images(github, pull=False)
    pushed = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"][0]
    pushed.update(status="in_progress", conclusion=None)
    _scheduled_images(github, status="completed", conclusion="success")
    github.save()

    status = github.workflow.run_script("main-status")
    done = github.run()

    assert "main: pending" in status.stdout, status.stdout
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


def test_a_manual_rerun_of_main_ci_counts_and_a_scheduled_one_does_not(github: GitHub) -> None:
    endpoint = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    pushed = github.api[endpoint]["workflow_runs"][0]
    pushed["conclusion"] = "failure"
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    for run_id, event in ((14, "workflow_dispatch"), (16, "schedule")):
        conclusion = "success" if event == "workflow_dispatch" else "failure"
        github.api[endpoint]["workflow_runs"].append(
            {**pushed, "id": run_id, "event": event, "conclusion": conclusion}
        )
        github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {
            "jobs": [
                {
                    "id": run_id + 1,
                    "name": "CI result",
                    "status": "completed",
                    "conclusion": conclusion,
                }
            ]
        }
    github.save()

    status = github.workflow.run_script("main-status")

    assert status.stdout.startswith("main: green"), status.stdout


def test_each_new_scheduled_failure_is_reported_and_an_older_one_is_not(github: GitHub) -> None:
    pending = github.workflow.repo / ".local/workflow/main-status.pending"
    _images(github, pull=False)
    _scheduled_images(github, status="completed", conclusion="failure")
    github.save()
    github.workflow.run_script("main-status")
    pending.unlink()

    runs = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"]
    runs.append({**runs[-1], "id": 60})
    github.api[f"{_REPO}/actions/runs/60/jobs?per_page=100"] = github.api[
        f"{_REPO}/actions/runs/50/jobs?per_page=100"
    ]
    github.save()
    github.workflow.run_script("main-status")
    assert "scheduled Docker images run 60 failed" in pending.read_text()
    pending.unlink()

    runs.append({**runs[-1], "id": 70, "conclusion": "success"})
    github.api[f"{_REPO}/actions/runs/70/jobs?per_page=100"] = {
        "jobs": [
            {
                "id": 71,
                "name": "Docker images: what to run",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    }
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green") and "scheduled" not in status.stdout
    assert not pending.exists()


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


def test_main_inherits_the_exact_ci_run_that_tested_its_squashed_tree(github: GitHub) -> None:
    # An older run exists too: only the latest, validated run may be credited.
    endpoint = (
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    )
    older = deepcopy(github.api[endpoint]["workflow_runs"][0])
    github.ci(github.base, github.head, run_id=70)
    github.api[endpoint]["workflow_runs"].append(older)
    _squash_with_pending_main(github)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "CI run 70" in status.stdout
    assert "CI run 20" not in status.stdout


def test_a_different_squash_tree_waits_for_main_ci(github: GitHub) -> None:
    _squash_with_pending_main(github, different=True)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout


def test_main_failure_overrides_inherited_green_and_stops_the_next_merge(github: GitHub) -> None:
    _squash_with_pending_main(github)
    assert github.workflow.run_script("main-status").stdout.startswith("main: green")
    github.main("failure")
    github.ci(github.base, github.head, run_id=80)
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "merging stopped: main: red" in done.stderr
    assert len(github.workflow.calls("gh pr merge")) == 1


@pytest.mark.parametrize("image_state", ["missing", "pending", "failed"])
def test_main_reuses_images_on_the_same_tree_but_its_own_failure_wins(
    github: GitHub, image_state: str
) -> None:
    _images(github, release_conclusion="success")
    _squash_with_pending_main(github)
    if image_state != "missing":
        _images(github, pull=False, conclusion="failure" if image_state == "failed" else "success")
        if image_state == "pending":
            endpoint = (
                f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
            )
            github.api[endpoint]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()
    status = github.workflow.run_script("main-status")
    if image_state == "failed":
        assert status.stdout.startswith("main: red"), status.stdout
    else:
        assert status.stdout.startswith("main: green"), status.stdout
        assert "Docker images run 40" in status.stdout


def test_inheritance_does_not_cover_a_later_untested_main_commit(github: GitHub) -> None:
    squash = _squash_with_pending_main(github)
    later = github.workflow.git(
        "commit-tree",
        github.workflow.git("rev-parse", f"{squash}^{{tree}}"),
        "-p",
        squash,
        "-m",
        "chore: unrelated hand push",
    )
    github.workflow.git("push", "origin", f"{later}:main")
    github.base = later
    github.main()
    endpoint = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={later}&per_page=100"
    github.api[endpoint]["workflow_runs"] = []
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout


def test_the_next_gate_can_merge_before_main_ci_has_even_started(github: GitHub) -> None:
    _squash_with_pending_main(github)
    endpoint = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    github.api[endpoint]["workflow_runs"] = []
    github.ci(github.base, github.head, run_id=80)
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "inherited from CI run 20" in done.stdout
    assert len(github.workflow.calls("gh pr merge")) == 2


def test_inherited_ci_cannot_replace_required_images_without_image_evidence(github: GitHub) -> None:
    _squash_with_pending_main(github)
    # A push's image workflow can have started even though no PR image result was available.
    _images(github, pull=False)
    endpoint = f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
    github.api[endpoint]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout
    assert "Docker images" in status.stdout


def test_scheduled_failures_are_still_reported_when_main_inherits_green(github: GitHub) -> None:
    _squash_with_pending_main(github)
    _scheduled_images(github, status="completed", conclusion="failure")
    github.save()
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "scheduled Docker images run 50 failed" in status.stdout


@pytest.mark.parametrize(
    "receipt", ["missing", "invalid", "missing-fields", "wrong-tree", "without-unproven-jobs"]
)
def test_unusable_inheritance_evidence_waits_for_main(github: GitHub, receipt: str) -> None:
    _squash_with_pending_main(github)
    path = github.workflow.repo / ".local/workflow/main-tree-green.json"
    if receipt == "missing":
        path.unlink()
    elif receipt == "invalid":
        path.write_text("{interrupted")
    elif receipt == "missing-fields":
        path.write_text("{}")
    elif receipt == "without-unproven-jobs":
        # A receipt from before it named the jobs the pull request skipped.
        data = json.loads(path.read_text())
        del data["ci_unproven"]
        path.write_text(json.dumps(data))
    else:
        data = json.loads(path.read_text())
        data["tree"] = "0" * 40
        path.write_text(json.dumps(data))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout


def test_identical_tree_with_an_unexpected_squash_parent_gets_no_receipt(github: GitHub) -> None:
    _squash_with_pending_main(github, wrong_parent=True)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout
    assert not (github.workflow.repo / ".local/workflow/main-tree-green.json").exists()


def test_inherited_ci_waits_for_required_images_when_its_receipt_lacks_them(github: GitHub) -> None:
    workflow = github.workflow
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    _images(github)
    _squash_with_pending_main(github)
    path = workflow.repo / ".local/workflow/main-tree-green.json"
    receipt = json.loads(path.read_text())
    receipt["images_run"] = None
    path.write_text(json.dumps(receipt))
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending, Docker images"), status.stdout
    _images(github, pull=False)
    github.save()
    assert workflow.run_script("main-status").stdout.startswith("main: green")


@pytest.mark.parametrize("main_images", ["missing", "pending"])
def test_skipped_pr_release_checks_cannot_cover_main_release_tests(
    github: GitHub, main_images: str
) -> None:
    workflow = github.workflow
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    _images(github)  # The PR skipped release tests; main's push will run them.
    _squash_with_pending_main(github)
    if main_images == "pending":
        _images(github, pull=False)
        endpoint = f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"
        github.api[endpoint]["workflow_runs"][0].update(status="in_progress", conclusion=None)
        release = github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"][-1]
        release.update(status="in_progress", conclusion=None)
    github.save()
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending"), status.stdout
    assert "Docker images" in status.stdout


def test_an_earlier_passed_image_run_does_not_stand_in_for_main_release_tests(
    github: GitHub,
) -> None:
    workflow = github.workflow
    # The image run of an earlier push of main passed; it is in main's list of image runs.
    _main_image_run(github, 90, github.base)
    image_input = workflow.repo / "docker/Dockerfile"
    image_input.parent.mkdir()
    image_input.write_text("FROM scratch\n")
    workflow.git("add", "docker")
    workflow.git("commit", "-m", "fix: image input")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    _images(github)  # The PR skipped release tests; main's push will run them.
    _squash_with_pending_main(github)
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: pending, Docker images have not started"), status.stdout
    _images(github, pull=False, release_conclusion="success")
    github.save()
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout


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


def test_a_ci_job_the_pull_request_skipped_waits_for_mains_own_success(github: GitHub) -> None:
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    assert receipt["ci_passed"] == ["CI result", "CI: what to run"]
    assert receipt["ci_unproven"] == [_OLDER]
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: pending, {_OLDER}"), status.stdout
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "in_progress", None))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: pending, {_OLDER}"), status.stdout
    # Main's own result for the skipped job is enough; the jobs the pull request passed are
    # inherited while main still runs them.
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "completed", "success"))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "inherited from CI run 20" in status.stdout


def test_mains_failure_of_a_job_the_pull_request_skipped_turns_main_red(github: GitHub) -> None:
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "completed", "failure"))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout
    assert _OLDER in status.stdout


def test_no_merge_while_main_waits_for_a_job_the_pull_request_skipped(github: GitHub) -> None:
    # The next merge would push main and cancel the very run the gate waits for.
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    _main_jobs(github, (_OLDER, "in_progress", None))
    github.ci(github.base, github.head, run_id=80)
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"merging stopped: main: pending, {_OLDER}" in done.stderr
    assert len(github.workflow.calls("gh pr merge")) == 1


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


def test_main_waits_for_every_part_of_a_check_the_pull_request_skipped(github: GitHub) -> None:
    # Skipped, the job has its name alone; on main it runs in parts, named with their part.
    _pr_job(github, _PYTHON, "skipped")
    _squash_with_pending_main(github)
    assert _receipt(github)["ci_unproven"] == [_PYTHON]

    _main_jobs(
        github,
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "in_progress", None),
    )
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: pending, {_PYTHON} ("), status.stdout

    _main_jobs(
        github,
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "completed", "success"),
    )
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "inherited from CI run 20" in status.stdout


def test_one_failed_part_on_main_makes_the_whole_check_red(github: GitHub) -> None:
    _pr_job(github, _PYTHON, "skipped")
    _squash_with_pending_main(github)
    _main_jobs(
        github,
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "completed", "failure"),
    )
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith(f"main: red since 2026-10-16T09:00:00Z, {_PYTHON} ("), (
        status.stdout
    )
    assert "of 2)" not in status.stdout


def test_main_inherits_a_check_whose_every_part_passed(github: GitHub) -> None:
    _pr_parts(github, "success", "success")
    _squash_with_pending_main(github)

    receipt = _receipt(github)
    assert _PYTHON in receipt["ci_passed"]
    assert receipt["ci_unproven"] == []


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


def test_an_inherited_main_run_still_running_is_cancelled_and_main_stays_green(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    assert workflow.calls("gh run cancel") == ["gh run cancel 10 -R example-org/example-app"]
    receipt = _receipt(github)
    assert receipt["cancelled_inherited"] == [{"run": 10, "attempt": 1, "tree": receipt["tree"]}]
    _cancelled_by_github(github)
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout + status.stderr
    assert "inherited from CI run 20" in status.stdout
    assert workflow.run_script("main-status", "--tick-main-status").stdout.startswith("main: green")
    assert len(workflow.calls("gh run cancel")) == 1


def test_a_main_run_cancelled_otherwise_is_still_red(github: GitHub) -> None:
    _squash_with_pending_main(github)
    _cancelled_by_github(github)
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr
    # Listed for another tree, it is not this main's.
    path = github.workflow.repo / ".local/workflow/main-tree-green.json"
    elsewhere = [{"run": 10, "attempt": 1, "tree": "0" * 40}]
    path.write_text(json.dumps({**_receipt(github), "cancelled_inherited": elsewhere}))
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr


def test_a_listed_run_that_failed_before_its_cancel_is_still_red(github: GitHub) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)
    assert workflow.run_script("main-status", "--tick-main-status").stdout.startswith("main: green")
    _main_ci_run(github).update(status="completed", conclusion="cancelled")
    _main_jobs(github, ("CI result", "completed", "cancelled"), ("CI: web", "completed", "failure"))
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr
    assert "CI: web" in status.stdout


def test_a_main_run_not_yet_inherited_is_never_cancelled(github: GitHub) -> None:
    workflow = github.workflow
    _pull_skipped_older(github)
    _squash_with_pending_main(github)
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "in_progress", None))

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith(f"main: pending, {_OLDER}"), done.stdout + done.stderr
    assert workflow.calls("gh run cancel") == []
    assert "cancelled_inherited" not in _receipt(github)
    # The plain status, which holds no lock, cancels nothing either.
    _main_jobs(github, ("CI: server tests", "in_progress", None), (_OLDER, "completed", "success"))
    assert workflow.run_script("main-status").stdout.startswith("main: green")
    assert workflow.calls("gh run cancel") == []


def test_the_cancelled_runs_of_another_tree_are_pruned(github: GitHub) -> None:
    _squash_with_pending_main(github)
    path = github.workflow.repo / ".local/workflow/main-tree-green.json"
    older = {"run": 5, "attempt": 1, "tree": "0" * 40}
    path.write_text(json.dumps({**_receipt(github), "cancelled_inherited": [older]}))

    done = github.workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    receipt = _receipt(github)
    assert receipt["cancelled_inherited"] == [{"run": 10, "attempt": 1, "tree": receipt["tree"]}]


def test_a_refused_cancel_of_main_is_logged_once(github: GitHub) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)
    github.refuse_cancel = [10]
    github.save()

    for _ in range(2):
        done = workflow.run_script("main-status", "--tick-main-status")
        assert done.stdout.startswith("main: green"), done.stdout + done.stderr

    assert len(workflow.calls("gh run cancel")) == 1
    notes = [line for line in workflow.wake_log.read_text().splitlines() if "run 10" in line]
    assert len(notes) == 1 and "Cannot cancel a workflow run that is completed." in notes[0]


def test_an_inherited_image_run_of_main_is_left_to_finish(github: GitHub) -> None:
    # Main is judged by its latest image run on any earlier push too: cancelled, this one
    # would decide main once a later push replaced its receipt.
    workflow = github.workflow
    _images(github, release_conclusion="success")
    _squash_with_pending_main(github)
    _main_image_run(github, 60, github.base, status="in_progress", conclusion=None)
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.base}&per_page=100"] = {
        "workflow_runs": [github.api[_MAIN_IMAGES]["workflow_runs"][0]]
    }
    github.save()

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    assert "Docker images run 40" in done.stdout
    assert workflow.calls("gh run cancel") == ["gh run cancel 10 -R example-org/example-app"]


def test_a_cancelled_inherited_run_whose_summary_job_failed_after_the_cancel_stays_green(
    github: GitHub,
) -> None:
    # "CI result" runs even in a cancelled run, and fails for the jobs cancelled under it.
    workflow = github.workflow
    _squash_with_pending_main(github)
    assert workflow.run_script("main-status", "--tick-main-status").stdout.startswith("main: green")
    _main_ci_run(github).update(status="completed", conclusion="cancelled")
    _main_jobs(github, ("CI result", "completed", "failure"), ("CI: web", "completed", "cancelled"))
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout + status.stderr


def test_a_re_run_of_main_is_never_cancelled(github: GitHub) -> None:
    # A second attempt, such as the tick's own retry of a failed main, decides main itself.
    workflow = github.workflow
    _squash_with_pending_main(github)
    _main_ci_run(github)["run_attempt"] = 2
    github.save()

    done = workflow.run_script("main-status", "--tick-main-status")

    assert done.stdout.startswith("main: green"), done.stdout + done.stderr
    assert workflow.calls("gh run cancel") == []
    # Cancelled by someone else, it is red as any other cancelled run.
    _cancelled_by_github(github)
    status = workflow.run_script("main-status")
    assert status.stdout.startswith("main: red"), status.stdout + status.stderr


def test_a_cancel_lost_to_a_github_outage_is_tried_again(github: GitHub) -> None:
    workflow = github.workflow
    _squash_with_pending_main(github)
    github.cancel_unavailable = [10]
    github.save()

    for _ in range(2):
        done = workflow.run_script("main-status", "--tick-main-status")
        assert done.stdout.startswith("main: green"), done.stdout + done.stderr

    assert len(workflow.calls("gh run cancel 10 ")) == 2
    assert not workflow.wake_log.exists() or "run 10" not in workflow.wake_log.read_text()
    assert len(workflow.calls("gh run cancel")) == 2
    # Accepted, it is not asked again while GitHub still lists the run as finishing.
    workflow.run_script("main-status", "--tick-main-status")
    assert len(workflow.calls("gh run cancel")) == 2


def _main_red_in_one_part(github: GitHub) -> None:
    github.main("failure")
    _main_jobs(
        github,
        ("CI result", "completed", "failure"),
        (f"{_PYTHON} (1 of 2)", "completed", "success"),
        (f"{_PYTHON} (2 of 2)", "completed", "failure"),
    )


def test_a_fix_that_passed_every_part_of_mains_failed_check_merges(github: GitHub) -> None:
    _main_red_in_one_part(github)
    # Dealt to parts otherwise than main's run was: the check is what has to pass.
    _pr_parts(github, "success")

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert f"passed every check failing on main: CI result, {_PYTHON}" in done.stdout


def test_a_pull_request_with_a_failed_part_is_not_the_fix(github: GitHub) -> None:
    _main_red_in_one_part(github)
    _pr_parts(github, "failure", "success")

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert not (github.workflow.state / "merged").exists()


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


def test_an_audit_with_a_new_fix_still_gets_its_checks_finished_wake(github: GitHub) -> None:
    _blocked_first(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    workflow.list_pull_request(7, draft=False, labels=())
    workflow.running("audit-7")
    (workflow.state / "tmux/review-7").unlink()
    workflow.screen("● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "audit-7")
    (workflow.notes(7) / "audit-7.checks-wait").touch()
    workflow.git("commit", "--allow-empty", "-m", "fix: failed check")
    _refreshed_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    workflow.set_pull_request(7, "OPEN", github.head)
    github.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux audit-7 send-keys")) == 2
    # Its new fix still rejoins at the back when the audit calls the gate.
    assert github.run().returncode == 3


def test_a_green_rerun_recovers_the_blocked_heads_old_place(github: GitHub) -> None:
    _blocked_first(github)
    assert _train(github)["first"] == "8"
    # A rerun updates the same run id and head, rather than requesting a new commit.
    github.ci(github.base, github.head, run_id=50)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    github.save()
    train = _train(github)
    assert train["blocked"] == []
    assert train["waiting"] == ["7", "8"]
    assert train["first"] == "7"
    before = github.workflow.git("rev-parse", "HEAD")
    assert github.run().returncode == 0
    assert github.workflow.git("rev-parse", "HEAD") == before


def test_a_green_rerun_on_old_main_stays_blocked_until_refreshed(github: GitHub) -> None:
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
    stale = _train(github)
    assert stale["blocked"] == ["7"]
    assert _train(github)["turn"] == stale["turn"]
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert not (workflow.state / "merged").exists()
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    # While the refreshed head's CI runs, it holds its place like any other refresh.
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ] = {"workflow_runs": []}
    github.save()
    assert _train(github)["first"] == "7"
    second = workflow.root / "second"
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    again = github.run()
    assert again.returncode == 1
    assert "has not started" in again.stderr and "failed checks" not in again.stderr
    _refreshed_first(github)
    refreshed = _train(github)
    assert refreshed["blocked"] == []
    assert refreshed["waiting"] == ["7", "8"]
    assert refreshed["first"] == "7"


@pytest.mark.parametrize("result", ["failure", "pending", "unreadable", "new-failure"])
def test_a_blocked_head_needs_a_readable_green_latest_run(github: GitHub, result: str) -> None:
    _blocked_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    if result == "unreadable":
        github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"] = None
    elif result == "pending":
        github.ci(github.base, github.head, run_id=50)
        github.api[
            f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
            "&event=pull_request&per_page=100"
        ]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    elif result == "new-failure":
        github.ci(github.base, github.head, run_id=50)
        github.save()
        assert _train(github)["first"] == "7"
        endpoint = (
            f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
            "&event=pull_request&per_page=100"
        )
        old_run = github.api[endpoint]["workflow_runs"][0]
        github.api[endpoint]["workflow_runs"].append({**old_run, "id": 60, "conclusion": "failure"})
        github.api[f"{_REPO}/actions/runs/60/jobs?per_page=100"] = github.api[
            f"{_REPO}/actions/runs/50/jobs?per_page=100"
        ]
    github.save()
    train = _train(github)
    assert train["blocked"] == ["7"]
    assert train["first"] == "8"
    assert train["waiting"] == ["8"]  # The tick can still deliver the failure's checks wake.
    # A session that calls the gate meanwhile is told a green re-run also lifts the block.
    refused = github.run()
    assert refused.returncode == 1
    assert "once a re-run is green" in refused.stderr


def test_a_green_rerun_whose_refresh_conflicts_keeps_its_place(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    shared = workflow.repo / "shared.txt"
    shared.write_text("branch edit\n")
    workflow.git("commit", "-am", "fix: branch edit")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    _refreshed_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    assert github.run().returncode == 1
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 2
    old = github.base
    workflow.git("checkout", "main")
    shared.write_text("main edit\n")
    workflow.git("commit", "-am", "fix: main edit")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old, run_id=50)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    github.pulls["7"].update(mergeable="CONFLICTING", mergeStateStatus="DIRTY")
    refused = github.run()
    assert refused.returncode == 1
    assert "resolve the conflicts in shared.txt" in refused.stderr
    shared.write_text("resolved\n")
    workflow.git("add", "shared.txt")
    workflow.git("commit", "--no-edit")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.pulls["7"].update(mergeable="MERGEABLE", mergeStateStatus="CLEAN")
    # The resolution is the refresh, not a new fix: it keeps the place ahead of 8.
    _refreshed_first(github)
    train = _train(github)
    assert train["blocked"] == []
    assert train["first"] == "7"
    assert github.run().returncode == 0


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure"])
def test_tick_reruns_failed_home_jobs_once_and_reports_it(github: GitHub, conclusion: str) -> None:
    workflow = github.workflow
    github.main(conclusion)
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["self-hosted", "example-app-home-main"]
    github.save()

    done = workflow.run_script("tick")

    rerun_calls = [call for call in workflow.calls("gh") if "run rerun" in call]
    assert rerun_calls == ["gh run rerun 10 -R example-org/example-app --failed"], done.stderr
    note = "automatically re-ran failed jobs of main run 10 once"
    assert note in workflow.wake_log.read_text()
    assert note in (workflow.repo / ".local/workflow/main-status").read_text()
    assert "main: red" in (workflow.repo / ".local/workflow/main-status").read_text()
    # A merge-gate health refresh must preserve the note without another retry.
    assert note in workflow.run_script("main-status").stdout
    # GitHub can still return attempt 1 for a tick after accepting the request.
    workflow.run_script("tick")
    assert len([call for call in workflow.calls("gh") if "run rerun" in call]) == 1
    run["run_attempt"] = 2
    github.save()
    workflow.run_script("tick")
    assert len([call for call in workflow.calls("gh") if "run rerun" in call]) == 1


@pytest.mark.parametrize("first", ["example-app-home-main", "ubuntu-latest"])
def test_tick_reruns_a_failed_part_only_when_every_part_ran_at_home(
    github: GitHub, first: str
) -> None:
    workflow = github.workflow
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": "CI: server tests (1 of 2)", "status": "completed", "conclusion": "success"},
        {"id": 13, "name": "CI: server tests (2 of 2)", "status": "completed", "conclusion": "failure"},
    ]
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["self-hosted", "example-app-home-main"]
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["labels"] = [first]
    github.save()

    done = workflow.run_script("tick")

    reruns = [call for call in workflow.calls("gh") if "run rerun" in call]
    expected = ["gh run rerun 10 -R example-org/example-app --failed"]
    assert reruns == (expected if first == "example-app-home-main" else []), done.stderr
    assert "main: red" in (workflow.repo / ".local/workflow/main-status").read_text()


@pytest.mark.parametrize(
    "reason",
    [
        "second attempt",
        "in progress",
        "hosted",
        "unknown runner",
        "unknown attempt",
        "pull request",
    ],
)
def test_tick_does_not_rerun_ineligible_main_jobs(github: GitHub, reason: str) -> None:
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 2 if reason == "second attempt" else 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home-main"]
    if reason == "in progress":
        run["status"] = "in_progress"
    elif reason == "hosted":
        github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["labels"] = [
            "ubuntu-latest"
        ]
    elif reason == "unknown runner":
        github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1].pop("labels")
    elif reason == "unknown attempt":
        run.pop("run_attempt")
    elif reason == "pull request":
        run["event"] = "pull_request"
    github.save()
    github.workflow.run_script("tick")
    assert not [call for call in github.workflow.calls("gh") if "run rerun" in call]


@pytest.mark.parametrize("mode", ["read only", "request refused", "images"])
def test_main_retry_keeps_its_scope_and_reports_refusals(github: GitHub, mode: str) -> None:
    workflow = github.workflow
    github.main("failure")
    run_path = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    run = github.api[run_path]["workflow_runs"][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home-docker"]
    if mode == "images":
        github.api[run_path.replace("ci.yml", "images.yml")] = {"workflow_runs": [deepcopy(run)]}
        github.api[run_path] = {"workflow_runs": []}
    elif mode == "request refused":
        (workflow.state / "fail-rerun").touch()
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main-status").write_text("main: red since earlier\n")

    if mode == "read only":
        workflow.run_script("main-status")
        assert not [call for call in workflow.calls("gh") if "run rerun" in call]
        return
    workflow.run_script("tick")
    pending = (folder / "main-status.pending").read_text()
    assert "main run 10" in pending
    assert "main run 10" in workflow.wake_log.read_text()
    if mode == "request refused":
        assert "refused" in pending
        workflow.run_script("tick")
    assert len([call for call in workflow.calls("gh") if "run rerun" in call]) == 1


def test_a_failed_home_planner_cannot_authorize_hosted_dependents(github: GitHub) -> None:
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    run["event"] = "workflow_dispatch"
    parts = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]
    parts[0].update(name="CI: what to run", labels=["home-planner"])
    parts[1].update(name="CI result", labels=["example-app-home"])
    # A failed planner has not chosen where its skipped dependents will run. A manual
    # run may request machine=github; retrying that home planner would spend minutes.
    parts.append({"id": 13, "name": "CI: web", "status": "completed", "conclusion": "skipped"})
    github.save()
    github.workflow.run_script("tick")
    assert not [call for call in github.workflow.calls("gh") if "run rerun" in call]


def test_tick_does_not_retry_while_a_merge_gate_holds_its_lock(github: GitHub) -> None:
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home"]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        github.workflow.run_script("tick")
    assert not [call for call in github.workflow.calls("gh") if "run rerun" in call]


def test_tick_does_not_retry_a_head_that_left_main_during_the_github_read(github: GitHub) -> None:
    workflow = github.workflow
    github.main("failure")
    run = github.api[f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"][
        "workflow_runs"
    ][0]
    run["run_attempt"] = 1
    for job in github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]:
        job["labels"] = ["example-app-home"]
    github.next_main = workflow.git(
        "commit-tree",
        workflow.git("rev-parse", f"{github.base}^{{tree}}"),
        "-p",
        github.base,
        "-m",
        "fix: main moved",
    )
    workflow.git("push", "origin", f"{github.next_main}:refs/heads/future-main")
    workflow.fake(
        "gh.py",
        _GH.replace(
            '        reply = data["api"][path]',
            """        if path.endswith("/actions/runs/10/jobs?per_page=100"):
            run(["git", "--git-dir", data["origin"], "update-ref",
                 "refs/heads/main", data["next_main"]], check=True)
        reply = data["api"][path]""",
        ),
    )
    github.save()
    workflow.run_script("tick")
    assert workflow.git("ls-remote", "origin", "refs/heads/main").split()[0] == github.next_main
    assert not [call for call in workflow.calls("gh") if "run rerun" in call]


def test_a_red_line_that_only_mentions_automatic_is_not_a_retry_report(github: GitHub) -> None:
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"][1]["name"] = (
        "CI: automatic checks"
    )
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main-status").write_text("main: red since earlier, CI result (abc)\n")

    github.workflow.run_script("tick")

    assert "main: red" in (folder / "main-status").read_text()
    # Red to red is not news; only a retry note of this tick's own is.
    assert not (folder / "main-status.pending").exists()


@pytest.mark.parametrize(
    ("mergeable", "merge_state"), [("CONFLICTING", "UNKNOWN"), ("UNKNOWN", "DIRTY")]
)
def test_github_conflict_yields_before_a_gate_attempt_and_wakes_once_per_head(
    github: GitHub, mergeable: str, merge_state: str
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.pulls["7"].update(mergeable=mergeable, mergeStateStatus=merge_state)
    # Conflicts can prevent CI from starting at all.
    github.pulls["7"]["statusCheckRollup"] = []
    github.save()
    (workflow.state / "rollup-7.json").write_text("[]")
    (workflow.state / "mergeability-7").write_text(f"{mergeable} {merge_state}")
    workflow.set_pull_request(7, "OPEN", github.head)

    assert _train(github)["first"] == "8"
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    wakes = workflow.calls("tmux review-7 send-keys")
    assert len(wakes) == 2
    assert f"GitHub reports a merge conflict on {github.head}" in wakes[0]
    assert "non-mechanical conflict" in wakes[0]
    assert len(workflow.calls("tmux review-8 send-keys")) == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 2

    # A newly conflicting head receives a fresh wake, even before it finishes CI.
    workflow.git("commit", "--allow-empty", "-m", "fix: conflict still present")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    workflow.set_pull_request(7, "OPEN", github.head)
    github.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux review-7 send-keys")) == 4

    github.pulls["7"].update(mergeable="MERGEABLE", mergeStateStatus="CLEAN")
    github.ci(github.base, github.head, run_id=50)
    github.save()
    assert _train(github)["first"] == "7"


def test_unknown_mergeability_keeps_its_train_place_without_a_conflict_wake(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.pulls["7"].update(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN", statusCheckRollup=[])
    github.save()
    (workflow.state / "rollup-7.json").write_text("[]")
    (workflow.state / "mergeability-7").write_text("UNKNOWN UNKNOWN")
    assert _train(github)["first"] == "7"
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert workflow.calls("tmux review-8 send-keys") == []
    assert "not determined" in github.run().stderr


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
        if images and number == 7:
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
        _images(github)
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


def test_a_stale_member_merging_alone_starts_a_refresh_and_its_whole_suites(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    old = github.base
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old, server=SELECTED)
    github.e2e(github.head, tree, suite="selected: 0 of 48 spec files", run_id=900_020)
    published = {github.head}

    assert _batch_call(github).returncode == 2  # The refresh: its CI and E2E start again.
    github.head = workflow.git("rev-parse", "HEAD")
    published.add(workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0])
    github.pulls["7"]["headRefOid"] = github.head
    tree = workflow.git("rev-parse", f"{github.head}^{{tree}}")
    github.ci(github.base, github.head, run_id=70, server=SELECTED)
    github.e2e(github.head, tree, suite="selected: 0 of 48 spec files", run_id=900_070)
    assert _batch_call(github).returncode == 3  # Every server test and the browser suite.
    suite = _suite(github)
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    refreshes = len(published) - 1
    assert 2 * refreshes + len(workflow.calls("gh workflow run")) == ONE_AT_A_TIME_RUNS


def test_a_line_starts_no_more_runs_per_merged_member_than_merging_one_at_a_time(
    github: GitHub,
) -> None:
    _batch_setup(github, count=4, selected=True)
    heads = {number: pull["headRefOid"] for number, pull in github.pulls.items()}
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9", "10"]
    # No pull request branch was pushed: nothing ran on one again.
    for number, head in heads.items():
        branch = github.pulls[number]["headRefName"]
        assert github.workflow.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0] == head
    runs = len(github.workflow.calls("gh workflow run"))
    # A prefix's CI and E2E for each member.
    assert runs == 8
    assert runs / 4 <= ONE_AT_A_TIME_RUNS


def _merges(github: GitHub) -> list[str]:
    return [call.split()[3] for call in github.workflow.calls("gh pr merge")]


def _parents(github: GitHub, commit: str) -> list[str]:
    return github.workflow.git("show", "-s", "--format=%P", commit).split()


def test_three_members_each_tested_on_the_ones_ahead_merge_in_order(github: GitHub) -> None:
    _batch_setup(github)
    heads = {number: pull["headRefOid"] for number, pull in github.pulls.items()}
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    members = _prefixes(github)
    assert _numbers(github) == ["7", "8", "9"]
    # Each prefix is main and the members ahead of it, merged with its own member.
    for index, member in enumerate(members):
        ahead = members[index - 1]["commit"] if index else github.base
        assert _parents(github, member["commit"]) == [ahead, member["head"]]
        assert member["scratch"]["head"] == member["commit"]
    assert sorted(_scratch_branches(github)) == sorted(m["scratch"]["branch"] for m in members)
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3
    assert github.workflow.git("rev-parse", "HEAD") == heads["7"]
    for number, head in heads.items():
        branch = github.pulls[number]["headRefName"]
        assert github.workflow.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0] == head
    assert _batch_call(github).returncode == 3  # A restart does not dispatch twice.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    final = github.workflow.git("rev-parse", "origin/main^{tree}")
    assert final == members[-1]["tree"]
    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    # Main inherits the run of the prefix that tested exactly its tree: the third's.
    assert receipt["tree"] == final and receipt["ci_run"] == 2100
    assert _batch_state(github)["prefixes"] is None
    assert _scratch_branches(github) == []
    assert "merged in this call: 7, 8, 9" in done.stdout
    assert "the tick tells the sessions of 8, 9" in done.stdout


def test_four_members_merge_in_order_as_their_prefix_runs_pass(github: GitHub) -> None:
    _batch_setup(github, count=4)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8", "9", "10"]
    trees = {member["number"]: member["tree"] for member in _prefixes(github)}
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 4
    # A passed prefix behind one still running waits for the member ahead of it.
    _finish_prefix(github, "8", run_id=1100)
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _merges(github) == []
    _finish_prefix(github, "7")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "merged in this call: 7, 8" in done.stdout
    assert _merges(github) == ["7", "8"]
    assert _numbers(github) == ["9", "10"]
    github.workflow.git("checkout", "ticket/9")
    _finish_prefix(github, "10", run_id=3100)
    assert _batch_call(github, "9").returncode == 3
    assert _merges(github) == ["7", "8"]
    _finish_prefix(github, "9", run_id=2100)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9", "10"]
    assert github.workflow.git("rev-parse", "origin/main^{tree}") == trees["10"]
    # Nothing was tested twice.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 4
    assert _batch_state(github)["prefixes"] is None


def test_a_merge_pulls_the_next_member_into_the_window_on_the_last_prefix(
    github: GitHub,
) -> None:
    _batch_setup(github, count=5)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8", "9", "10"]
    _finish_prefix(github, "7")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7"]
    members = _prefixes(github)
    assert [member["number"] for member in members] == ["8", "9", "10", "11"]
    # 11 is merged onto the prefix of 10, and its runs start in the same call.
    assert _parents(github, members[3]["commit"]) == [
        members[2]["commit"],
        github.pulls["11"]["headRefOid"],
    ]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 5


def test_a_member_leaving_the_line_keeps_the_prefixes_ahead_of_it(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_prefix(github, "7")
    _finish_prefix(github, "8", run_id=1100)
    github.pulls["9"]["state"] = "CLOSED"
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3


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


def test_a_dropped_prefix_has_its_unfinished_runs_cancelled_before_its_branch_goes(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    dropped, kept = _scratch(github, "9"), _scratch(github, "7")
    assert dropped is not None and kept is not None
    _branch_runs(
        github,
        dropped["branch"],
        (901, "queued"),
        (902, "in_progress"),
        (903, "completed"),
        # Listed for its branch by mistake, these are never the gate's to cancel.
        (904, "in_progress", "ticket/9", "pull_request"),
        (905, "in_progress", "main", "push"),
    )
    _branch_runs(github, kept["branch"], (701, "in_progress"))
    github.pulls["9"]["state"] = "CLOSED"

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert _numbers(github) == ["7", "8"]
    cancelled = _cancelled(github)
    assert list(cancelled) == ["901", "902"]
    assert all(dropped["branch"] in branches for branches in cancelled.values())
    first = github.workflow.calls("gh run cancel")[0]
    assert first == "gh run cancel 901 -R example-org/example-app"
    assert dropped["branch"] not in _scratch_branches(github)
    assert kept["branch"] in _scratch_branches(github)


def test_every_prefix_dropped_at_once_has_its_runs_cancelled(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    old: dict[str, str] = {}
    for index, member in enumerate(_prefixes(github)):
        first = 800 + index * 10
        _branch_runs(
            github,
            member["scratch"]["branch"],
            (first, "queued"),
            (first + 1, "in_progress"),
            (first + 2, "waiting"),
            (first + 3, "completed"),
        )
        old |= {str(first + offset): member["scratch"]["branch"] for offset in range(3)}
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "chore: move main again")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert "main moved outside the merge train" in done.stdout
    cancelled = _cancelled(github)
    assert sorted(cancelled) == sorted(old)
    assert all(old[run] in branches for run, branches in cancelled.items())
    remaining = _scratch_branches(github)
    assert not set(old.values()) & set(remaining)
    assert sorted(remaining) == sorted(m["scratch"]["branch"] for m in _prefixes(github))


def _dropped_nine(github: GitHub) -> dict[str, Any]:
    """Members 7 to 9 tested at once; 9's prefix is dropped at the next call."""
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    dropped = _scratch(github, "9")
    assert dropped is not None
    github.pulls["9"]["state"] = "CLOSED"
    return dropped


def test_a_scratch_cancel_lost_to_an_outage_is_tried_again(github: GitHub) -> None:
    dropped = _dropped_nine(github)
    _branch_runs(github, dropped["branch"], (901, "in_progress"))
    github.cancel_unavailable = [901]

    assert _batch_call(github).returncode == 3
    # The branch outlives the failed cancel, so the next sweep finds its run again.
    assert dropped["branch"] in _scratch_branches(github)
    log = github.workflow.wake_log
    assert not log.exists() or "901" not in log.read_text()
    assert _batch_call(github).returncode == 3

    assert len(github.workflow.calls("gh run cancel 901 ")) == 2
    assert dropped["branch"] not in _scratch_branches(github)


def test_a_run_github_lists_only_after_the_drop_is_still_cancelled(github: GitHub) -> None:
    # GitHub accepts a dispatch before it lists the run: a prefix dropped in that window has
    # its branch deleted while its run is still to come.
    dropped = _dropped_nine(github)
    assert "ci" in dropped["requested"], dropped
    branch = dropped["branch"]

    assert _batch_call(github).returncode == 3
    assert branch not in _scratch_branches(github)
    _branch_runs(github, branch, (901, "queued"))
    assert _batch_call(github).returncode == 3

    assert github.workflow.calls("gh run cancel 901 ")


def _waiting_drops(github: GitHub) -> list[dict[str, Any]]:
    return _batch_state(github).get("dropped", [])


def test_a_dropped_prefix_is_watched_until_each_dispatched_run_was_seen(github: GitHub) -> None:
    dropped = _dropped_nine(github)
    branch = dropped["branch"]
    assert _batch_call(github).returncode == 3
    assert [entry["branch"] for entry in _waiting_drops(github)] == [branch]
    # A run of each workflow it dispatched shows up: each is cancelled, or had finished.
    runs = [
        (900 + index, "completed" if index else "queued", branch, "workflow_dispatch", workflow)
        for index, workflow in enumerate(dropped["requested"])
    ]
    _branch_runs(github, branch, *runs)
    assert _batch_call(github).returncode == 3
    assert list(_cancelled(github)) == ["900"]
    assert _waiting_drops(github) == []
    assert _batch_call(github).returncode == 3
    assert list(_cancelled(github)) == ["900"]


def test_a_dropped_prefix_whose_runs_never_show_is_given_up_after_the_deadline(
    github: GitHub,
) -> None:
    dropped = _dropped_nine(github)
    branch = dropped["branch"]
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    # Still within its deadline, it stays watched.
    train["dropped"][0]["dropped"] -= 29 * 60
    path.write_text(json.dumps(train))
    assert _batch_call(github).returncode == 3
    assert [entry["branch"] for entry in _waiting_drops(github)] == [branch]
    train = _batch_state(github)
    train["dropped"][0]["dropped"] -= 2 * 60
    path.write_text(json.dumps(train))

    assert _batch_call(github).returncode == 3

    assert _waiting_drops(github) == []
    log = github.workflow.wake_log.read_text()
    notes = [line for line in log.splitlines() if branch in line]
    assert len(notes) == 1 and "gave up" in notes[0], log
    assert _batch_call(github).returncode == 3
    assert github.workflow.wake_log.read_text().count(branch) == 1
    assert github.workflow.calls("gh run cancel") == []


def test_past_the_deadline_the_gate_looks_once_more_before_it_gives_up(github: GitHub) -> None:
    # The next sweep may come long after the drop: by then GitHub lists the late runs.
    dropped = _dropped_nine(github)
    branch = dropped["branch"]
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    train["dropped"][0]["dropped"] -= 31 * 60
    path.write_text(json.dumps(train))
    runs = [
        (900 + index, "completed" if index else "queued", branch, "workflow_dispatch", workflow)
        for index, workflow in enumerate(dropped["requested"])
    ]
    _branch_runs(github, branch, *runs)

    assert _batch_call(github).returncode == 3

    assert list(_cancelled(github)) == ["900"]
    assert _waiting_drops(github) == []
    log = github.workflow.wake_log
    assert not log.exists() or "gave up" not in log.read_text()


def test_a_lasting_error_on_one_dropped_branch_does_not_hold_the_others(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    eight, nine = _scratch(github, "8"), _scratch(github, "9")
    assert eight is not None and nine is not None
    # 8 leaves the line: its prefix and 9's behind it are dropped.
    github.pulls["8"]["state"] = "CLOSED"
    unreadable = f"{_REPO}/actions/runs?branch={eight['branch']}&per_page=100"
    github.api[unreadable] = "HTTP 404: Not Found"
    _branch_runs(github, nine["branch"], (901, "queued"))

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert list(_cancelled(github)) == ["901"]
    assert nine["branch"] not in _scratch_branches(github)
    assert eight["branch"] in _scratch_branches(github)


def test_a_malformed_watch_list_never_changes_the_gates_result(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    train["dropped"] = [
        None,
        {"branch": 5},
        {"branch": "workflow/merge-batch-x", "requested": None},
    ]
    path.write_text(json.dumps(train))

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert _waiting_drops(github) == []


def test_a_dispatch_whose_answer_was_lost_is_watched_too(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    during = json.loads((github.workflow.state / "batch-state-during-dispatch").read_text())
    # Sent but not yet answered: it may have reached GitHub.
    assert during["prefixes"]["members"][0]["scratch"]["dispatching"] == ["ci"]
    assert _scratch(github, "7")["dispatching"] == []
    # 9's dispatch was sent, but the gate stopped before GitHub answered.
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    nine = train["prefixes"]["members"][2]["scratch"]
    nine.update(requested=[], dispatching=["ci"])
    path.write_text(json.dumps(train))
    github.pulls["9"]["state"] = "CLOSED"

    assert _batch_call(github).returncode == 3

    assert [(e["branch"], e["requested"]) for e in _waiting_drops(github)] == [
        (nine["branch"], ["ci"])
    ]


def test_a_rejected_dispatch_is_not_watched(github: GitHub) -> None:
    _batch_setup(github)
    rejected = github.workflow.state / "reject-batch-dispatch"
    rejected.touch()
    assert _batch_call(github).returncode == 1
    scratch = _scratch(github)
    assert scratch is not None and scratch["requested"] == [] and scratch["dispatching"] == []


def test_a_refused_cancel_is_logged_once_and_the_branch_still_goes(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    dropped = _scratch(github, "9")
    assert dropped is not None
    _branch_runs(github, dropped["branch"], (901, "in_progress"), (902, "in_progress"))
    github.refuse_cancel = [901]
    github.pulls["9"]["state"] = "CLOSED"

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert list(_cancelled(github)) == ["901", "902"]
    assert dropped["branch"] not in _scratch_branches(github)
    log = github.workflow.wake_log.read_text()
    notes = [line for line in log.splitlines() if "run 901" in line]
    assert len(notes) == 1 and "Cannot cancel a workflow run that is completed." in notes[0]
    assert "run 902" not in log
    assert _batch_call(github).returncode == 3
    assert github.workflow.wake_log.read_text().count("run 901") == 1


def test_a_later_prefix_that_cannot_start_never_holds_a_ready_front(github: GitHub) -> None:
    _batch_setup(github)
    # 7's own CI tested its merge with this main: it needs no prefix of its own.
    github.ci(github.base, github.head, run_id=7100)
    # The push of 8's prefix is short of locked memory, twice: it waits for the next call.
    short_of_memory(github.workflow, MEMORY_SHORTAGES[1], times=2)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "the prefix of pull request 8 starts later" in done.stdout
    assert _merges(github) == ["7"]


def test_after_a_single_merge_the_next_prefix_waits_for_a_job_main_runs_itself(
    github: GitHub,
) -> None:
    _batch_setup(github)
    # 7 merges by its own checks, which skipped a job that main always runs.
    github.ci(github.base, github.head, run_id=7100)
    github.api[f"{_REPO}/actions/runs/7100/jobs?per_page=100"]["jobs"].append(
        {"id": 7103, "name": _OLDER, "status": "completed", "conclusion": "skipped"}
    )
    assert _batch_call(github).returncode == 0
    assert _merges(github) == ["7"]
    assert _numbers(github) == ["8", "9"]
    _finish_batch(github)
    github.workflow.git("checkout", "ticket/8")
    done = _batch_call(github, "8")
    # The next squash would cancel main's own run of that job before its result is known.
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"merging stopped: main: pending, {_OLDER}" in done.stderr
    assert _merges(github) == ["7"]


def test_a_member_whose_prefix_failed_is_blocked_and_the_ones_behind_are_rebuilt(
    github: GitHub,
) -> None:
    _batch_setup(github, count=4)
    assert _batch_call(github).returncode == 3
    before = {member["number"]: member for member in _prefixes(github)}
    _finish_prefix(github, "7")
    _finish_prefix(github, "8", conclusion="failure", run_id=1100)
    # The prefix behind it holds its change and fails with it.
    _finish_prefix(github, "9", conclusion="failure", run_id=2100)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "merged in this call: 7" in done.stdout
    assert (
        "ci run 1100 of pull request 8's prefix failed (CI result) on main and the members "
        "ahead of it; it is blocked as a failed check"
    ) in done.stdout
    train = _batch_state(github)
    # Only the first failure is the member's own: 9 failed because 8 was in its prefix.
    assert train["blocked"] == {"8": github.pulls["8"]["headRefOid"]}
    assert train["window"] == 2
    rebuilt = _prefixes(github)
    assert [member["number"] for member in rebuilt] == ["9", "10"]
    main = github.workflow.git("rev-parse", "origin/main")
    assert _parents(github, rebuilt[0]["commit"]) == [main, github.pulls["9"]["headRefOid"]]
    assert rebuilt[0]["scratch"]["branch"] != before["9"]["scratch"]["branch"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 6
    # The failed run's branch stays for a rerun; the dropped prefixes' branches are gone.
    branches = _scratch_branches(github)
    assert before["8"]["scratch"]["branch"] in branches
    assert before["9"]["scratch"]["branch"] not in branches
    assert before["10"]["scratch"]["branch"] not in branches
    github.save()
    assert _train(github)["first"] == "9"
    github.workflow.git("checkout", "ticket/9")
    _finish_batch(github, run_id=5000)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "9", "10"]
    train = _batch_state(github)
    assert train["blocked"] == {"8": github.pulls["8"]["headRefOid"]}
    assert train["window"] == 4


def test_the_window_halves_on_each_failure_and_grows_by_one_per_merge(github: GitHub) -> None:
    _batch_setup(github, count=4)
    assert _batch_call(github).returncode == 3
    _finish_prefix(github, "7", conclusion="failure")
    assert _batch_call(github).returncode == 1
    assert _batch_state(github)["window"] == 2
    assert _numbers(github) == ["8", "9"]
    github.workflow.git("checkout", "ticket/8")
    _finish_prefix(github, "8", conclusion="failure", run_id=1100)
    assert _batch_call(github, "8").returncode == 1
    assert _batch_state(github)["window"] == 1
    # A window of one still tests a stale first member alone, on a scratch branch.
    assert _numbers(github) == ["9"]
    github.workflow.git("checkout", "ticket/9")
    _finish_prefix(github, "9", run_id=2100)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _batch_state(github)["window"] == 2
    assert _merges(github) == ["9"]


def test_a_batch_takes_its_members_by_priority(github: GitHub) -> None:
    _batch_setup(github)
    github.pulls["9"]["labels"] = [{"name": "priority: high"}]
    github.pulls["7"]["labels"] = [{"name": "priority: low"}]
    github.workflow.git("checkout", "ticket/9")

    done = _batch_call(github, "9")

    assert done.returncode == 3, done.stdout + done.stderr
    assert _numbers(github) == ["9", "8", "7"]
    _finish_batch(github)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["9", "8", "7"]


def test_a_priority_change_drops_the_prefixes_served_out_of_order(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    kept = _prefixes(github)[0]
    github.pulls["8"]["labels"] = [{"name": "priority: low"}]
    assert _batch_call(github).returncode == 3
    # 9 is now served before 8: the prefix of 8 is tested again behind 9.
    assert _numbers(github) == ["7", "9", "8"]
    assert _prefixes(github)[0] == kept


def test_a_member_conflicting_with_the_batch_waits_for_its_own_turn(github: GitHub) -> None:
    _batch_setup(github, conflict=True)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8"]
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _batch_state(github)["waiting"] == ["9"]
    assert github.pulls["9"]["state"] == "OPEN"
    assert _merges(github) == ["7", "8"]


def test_main_moved_from_outside_invalidates_every_prefix(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    previous = _prefixes(github)
    _finish_batch(github)
    workflow = github.workflow
    workflow.git("checkout", "main")
    (workflow.repo / "new-main.txt").write_text("a concurrent push\n")
    workflow.git("add", "new-main.txt")
    workflow.git("commit", "-m", "fix: concurrent main")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _batch_state(github)["prefixes"]["base"] == github.base
    current = _prefixes(github)
    assert [m["number"] for m in current] == ["7", "8", "9"]
    for old, new in zip(previous, current, strict=True):
        assert new["commit"] != old["commit"]
        assert new["scratch"]["branch"] != old["scratch"]["branch"]
    assert github.workflow.calls("gh pr merge") == []
    _finish_batch(github, run_id=200)
    assert _batch_call(github).returncode == 0
    assert len(workflow.calls("gh workflow run ci.yml")) == 6


def test_batch_completion_changes_the_train_wakeup_once(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    github.save()
    before = _train(github)["turn"]
    # Only the first member's prefix decides its turn.
    _finish_prefix(github, "8", run_id=1100)
    github.save()
    assert _train(github)["turn"] == before
    _finish_prefix(github, "7")
    github.save()
    after = _train(github)["turn"]
    assert after > before
    assert _train(github)["turn"] == after


def test_train_status_shows_each_members_prefix_and_its_state(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    members = _prefixes(github)
    _finish_prefix(github, "7")
    _finish_prefix(github, "8", conclusion="failure", run_id=1100)
    github.save()
    status = _train(github)
    assert status["window"] == 4
    assert [
        (prefix["number"], prefix["branch"], prefix["state"]) for prefix in status["prefixes"]
    ] == [
        ("7", members[0]["scratch"]["branch"], "passed"),
        ("8", members[1]["scratch"]["branch"], "failed"),
        ("9", members[2]["scratch"]["branch"], "starting"),
    ]
    assert {run["workflow"]: run["id"] for run in status["prefixes"][1]["runs"]} == {
        "ci": 1100,
        "e2e": 1110,
    }


def test_the_line_never_tests_more_members_than_its_ceiling(github: GitHub) -> None:
    _batch_setup(github, count=5)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8", "9", "10"]
    _finish_batch(github)
    assert _batch_call(github).returncode == 0
    assert _batch_state(github)["waiting"] == ["11"]


@pytest.mark.parametrize("state", ["missing", "pending", "red", "other-tree"])
def test_batch_images_must_pass_on_the_scratch_tree(github: GitHub, state: str) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    # Each prefix holds the image change of 7.
    assert all(m["scratch"]["workflows"] == ["ci", "images", "e2e"] for m in _prefixes(github))
    _finish_batch(github)
    image_path = (
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    if state == "missing":
        github.api.pop(image_path)
    elif state == "pending":
        github.api[image_path]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    elif state == "red":
        github.api[image_path]["workflow_runs"][0]["conclusion"] = "failure"
    else:
        github.api[f"{_REPO}/check-runs/111/annotations?per_page=100"][0]["message"] = "old-tree"
    done = _batch_call(github)
    assert done.returncode == (1 if state in ("other-tree", "red") else 3), (
        done.stdout + done.stderr
    )
    assert github.workflow.calls("gh pr merge") == []
    if state == "other-tree":
        # The same evidence would refuse every later call: the prefix is retested instead.
        assert (
            "merge waiting: pull request 7's prefix images did not prove its tested files"
            in done.stderr
        )
        retested = _scratch(github)
        assert retested is not None and retested["branch"] != scratch["branch"]
        assert retested["head"] == scratch["head"]
        assert _batch_call(github).returncode == 3
        assert retested["branch"] in _scratch_branches(github)
        assert scratch["branch"] not in _scratch_branches(github)
    if state != "red":
        _finish_batch(github)
        done = _batch_call(github)
        assert done.returncode == 0, done.stdout + done.stderr
        receipt = json.loads(
            (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
        )
        assert receipt["images_run"] == 2110 and receipt["images_complete"] is True


@pytest.mark.parametrize("change", ["unapproved-queue-entry", "changed-head"])
def test_a_batch_cannot_merge_a_member_without_approval_of_its_current_head(
    github: GitHub, change: str
) -> None:
    _batch_setup(github)
    if change == "changed-head":
        assert _batch_call(github).returncode == 3
        previous = _prefixes(github)
        _finish_batch(github)
        workflow = github.workflow
        workflow.git("checkout", "ticket/9")
        (workflow.repo / "change-9.txt").write_text("an unreviewed new change\n")
        workflow.git("add", "change-9.txt")
        workflow.git("commit", "-m", "fix: new head for nine")
        head = workflow.git("rev-parse", "HEAD")
        workflow.git("push", "origin", "ticket/9")
        workflow.git("checkout", "ticket/seven")
        github.pulls["9"]["headRefOid"] = head
        github.ci(github.base, head, run_id=90)
    else:
        path = github.workflow.repo / ".local/workflow/merge-train.json"
        train = _batch_state(github)
        train["approved"].pop("9")
        path.write_text(json.dumps(train))
        previous = None
    done = _batch_call(github)
    assert done.returncode == (0 if previous else 3), done.stdout + done.stderr
    if previous is None:
        assert _numbers(github) == ["7", "8"]
        assert github.workflow.calls("gh pr merge") == []
        _finish_batch(github, run_id=200)
        assert _batch_call(github).returncode == 0
    else:
        # The prefixes ahead of the changed member still stand; its own is gone.
        assert "merged in this call: 7, 8" in done.stdout
    assert github.pulls["9"]["state"] == "OPEN"
    assert _merges(github) == ["7", "8"]


def test_rejected_batch_dispatch_can_be_retried(github: GitHub) -> None:
    _batch_setup(github)
    rejected = github.workflow.state / "reject-batch-dispatch"
    rejected.touch()
    done = _batch_call(github)
    assert done.returncode == 1 and "dispatch failed" in done.stderr
    scratch = _scratch(github)
    assert scratch is not None and scratch["requested"] == []
    assert github.workflow.calls("gh pr merge") == []
    rejected.unlink()
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    assert _batch_call(github).returncode == 0
    # The rejected one, then one for each prefix.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 4


def test_restart_recovers_an_accepted_dispatch_before_its_state_was_saved(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    during = json.loads((github.workflow.state / "batch-state-during-dispatch").read_text())
    # The crash window before a request starts cannot claim an unsent dispatch succeeded.
    assert during["prefixes"]["members"][0]["scratch"]["requested"] == []
    _finish_batch(github)
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    path.write_text(json.dumps(during))
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3


@pytest.mark.parametrize("member", [0, 1])
def test_a_different_prefix_tree_returns_remaining_members_to_the_single_path(
    github: GitHub, member: int
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    train = _batch_state(github)
    train["prefixes"]["members"][member]["tree"] = github.workflow.git(
        "rev-parse", f"{github.base}^{{tree}}"
    )
    (github.workflow.repo / ".local/workflow/merge-train.json").write_text(json.dumps(train))
    _finish_batch(github)
    done = _batch_call(github)
    # Once the caller's own pull request merged, the call reports that merge, never a wait.
    assert done.returncode == (2 if member == 0 else 0), done.stdout + done.stderr
    assert _merges(github) == ([] if member == 0 else ["7"])
    if member == 1:
        assert "pull request 7 merged in a batch (merged in this call: 7)" in done.stdout
        assert "still in the line: 8, 9" in done.stdout
    assert _batch_state(github)["prefixes"] is None
    receipt = github.workflow.repo / ".local/workflow/main-tree-green.json"
    # Main inherits only the prefix run that tested exactly its tree: 7's.
    assert receipt.exists() is (member == 1)


@pytest.mark.parametrize("fixes", [False, True])
def test_main_turning_red_during_a_batch_uses_the_existing_single_fix_rule(
    github: GitHub, fixes: bool
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.main("failure")
    github.ci(github.base, github.head, run_id=3000)
    if fixes:
        github.api[f"{_REPO}/actions/runs/3000/jobs?per_page=100"]["jobs"].append(
            {"id": 3003, "name": "CI: web", "status": "completed", "conclusion": "success"}
        )
    done = _batch_call(github)
    assert done.returncode == (0 if fixes else 1), done.stdout + done.stderr
    train = _batch_state(github)
    # The fix merges alone; main inherits green from its run, and the others' prefixes start.
    assert _numbers(github) == (["8", "9"] if fixes else [])
    assert ("7" in train["suspended"]) is not fixes
    github.save()
    assert _train(github)["first"] == "8"
    assert len(github.workflow.calls("gh pr merge")) == int(fixes)


def test_an_image_rerun_can_restore_an_isolated_batch_failure_to_its_place(github: GitHub) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    _finish_batch(github)
    path = (
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[path]["workflow_runs"][0]["conclusion"] = "failure"
    assert _batch_call(github).returncode == 1
    github.save()
    assert _train(github)["first"] == "8"
    image = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    image["run_attempt"] = 2
    github.save()
    assert _train(github)["first"] == "7"
    assert _batch_call(github).returncode == 2  # Refresh its stale green CI in its saved place.


def test_a_batch_saved_before_prefixes_is_dropped(github: GitHub) -> None:
    _batch_setup(github, count=4)
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    branch = "workflow/merge-batch-" + "0" * 32
    github.workflow.git("push", "origin", f"{github.base}:refs/heads/{branch}")
    train["batch"] = {
        "base": github.base,
        "head": github.base,
        "branch": branch,
        "members": [{"number": "7", "head": github.head, "tree": "0" * 40}],
        "cursor": 1,
        "workflows": ["ci"],
        "requested": ["ci"],
        "pushed": True,
    }
    train["probes"] = [["8"]]
    path.write_text(json.dumps(train))
    github.workflow.git("checkout", "ticket/10")
    done = _batch_call(github, "10")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "batch" not in _batch_state(github) and "probes" not in _batch_state(github)
    assert branch not in _scratch_branches(github)
    assert github.workflow.calls("gh pr merge") == []


@pytest.mark.parametrize("moved", [False, True])
def test_a_green_scratch_rerun_restores_the_isolated_members_earlier_place(
    github: GitHub, moved: bool
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    isolated = _scratch(github)
    assert isolated is not None
    _finish_batch(github)
    # Only CI failed for the first member; its whole browser suite passed.
    _finish_prefix(github, "7", conclusion="failure")
    github.e2e(
        isolated["head"], _prefixes(github)[0]["tree"], run_id=110, branch=isolated["branch"]
    )
    assert _batch_call(github).returncode == 1
    github.save()
    assert _train(github)["first"] == "8"
    if moved:
        # Main's files changed: the rerun no longer tests 7 on what main is.
        github.workflow.git("checkout", "main")
        (github.workflow.repo / "new-main.txt").write_text("a concurrent push\n")
        github.workflow.git("add", "new-main.txt")
        github.workflow.git("commit", "-m", "chore: main moved again")
        github.base = github.workflow.git("rev-parse", "HEAD")
        github.workflow.git("push", "origin", "main")
        github.workflow.git("checkout", "ticket/seven")
        github.main()
    path = (
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={isolated['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[path]["workflow_runs"][0].update(conclusion="success", run_attempt=2)
    for job in github.api[f"{_REPO}/actions/runs/100/jobs?per_page=100"]["jobs"]:
        job["conclusion"] = "success"
    github.save()
    assert _train(github)["first"] == "7"
    done = _batch_call(github)
    # On the same main, the passed rerun is its prefix: it merges without another run.
    assert done.returncode == (2 if moved else 0), done.stdout + done.stderr
    assert _merges(github) == ([] if moved else ["7"])
    # 7, 8 and 9; 8 and 9 again behind the new front after the failure; and, once 7 is back in
    # front of them, 8 and 9 once more on top of it. A refresh dispatches nothing.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == (5 if moved else 7)


@pytest.mark.parametrize("number", ["7", "9"])
def test_a_member_requiring_migration_compatibility_uses_the_single_path(
    github: GitHub, number: str
) -> None:
    _batch_setup(github, compatibility=number)
    job = {
        "id": 30000,
        "name": "CI: older version on new tables",
        "status": "completed",
        "conclusion": "success",
    }
    github.api[f"{_REPO}/actions/runs/{int(number) * 1000}/jobs?per_page=100"]["jobs"].append(job)
    done = _batch_call(github)
    assert done.returncode == (2 if number == "7" else 3), done.stdout + done.stderr
    if number == "7":
        assert _batch_state(github)["prefixes"] is None
        assert github.workflow.calls("gh workflow run") == []
    else:
        assert _numbers(github) == ["7", "8"]
        _finish_batch(github)
        assert _batch_call(github).returncode == 0
        assert github.pulls["9"]["state"] == "OPEN"
        github.workflow.git("checkout", "ticket/9")
        assert _batch_call(github, "9").returncode == 2
        head = github.workflow.git("rev-parse", "HEAD")
        base = github.workflow.git("rev-parse", "origin/main")
        github.pulls["9"]["headRefOid"] = head
        github.ci(base, head, run_id=3000)
        github.api[f"{_REPO}/actions/runs/3000/jobs?per_page=100"]["jobs"].append(job)
        done = _batch_call(github, "9")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _merges(github) == ["7", "8", "9"]


def test_the_acting_reviewer_keeps_its_availability_through_batch_squashes(github: GitHub) -> None:
    _batch_setup(github)
    notes = github.workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "review-7.owner-blocked").touch()
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


def test_the_scratch_branch_changing_drops_the_batch_for_a_retest(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    _finish_batch(github)
    github.workflow.git(
        "push", "--force", "origin", f"{github.base}:refs/heads/{scratch['branch']}"
    )
    done = _batch_call(github)
    assert done.returncode == 1
    assert "merge waiting: the gate's scratch branch changed; retesting next call" in done.stderr
    retested = _scratch(github)
    assert retested is not None and retested["branch"] != scratch["branch"]
    # The prefixes behind it are built on the same commit and stand.
    assert _numbers(github) == ["7", "8", "9"]
    assert scratch["branch"] not in _scratch_branches(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github, run_id=200)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    # A scratch run that proved the tree forgets the shortfall: a later one is a wait again.
    assert _batch_state(github)["retest"] is None


def test_a_different_shortfall_on_the_same_tree_is_still_a_wait(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    _finish_batch(github)
    github.workflow.git(
        "push", "--force", "origin", f"{github.base}:refs/heads/{scratch['branch']}"
    )
    assert "merge waiting: the gate's scratch branch changed" in _batch_call(github).stderr
    assert _batch_call(github).returncode == 3
    retested = _scratch(github)
    assert retested is not None
    _finish_batch(github, run_id=200)
    github.e2e(
        retested["head"],
        _prefixes(github)[0]["tree"],
        suite="selected: 3 of 48 spec files",
        run_id=210,
        branch=retested["branch"],
    )

    done = _batch_call(github)

    # Not the same shortfall twice in a row: one more retest, not a refusal.
    assert done.returncode == 1
    assert (
        "merge waiting: pull request 7's prefix e2e did not prove the whole browser suite"
        in done.stderr
    )
    assert github.workflow.calls("gh pr merge") == []


def test_a_member_no_longer_ready_after_the_callers_squash_reports_that_merge(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_merge = {"7": {"8": {"isDraft": True}}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "prefix member 8 is no longer ready" in done.stdout
    assert "pull request 7 merged in a batch (merged in this call: 7)" in done.stdout
    assert _merges(github) == ["7"]
    assert _batch_state(github)["prefixes"] is None


def test_unknown_mergeability_right_after_a_squash_keeps_the_batch(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    unknown = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
    github.after_merge = {"7": {"8": unknown, "9": unknown}, "8": {"9": unknown}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3


def test_a_conflict_shown_after_a_squash_still_stops_the_batch(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_merge = {"7": {"8": {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "prefix member 8 is no longer ready" in done.stdout
    assert _merges(github) == ["7"]


@pytest.mark.parametrize("refusals", [1, 3])
def test_a_refused_member_squash_is_retried_a_bounded_number_of_times(
    github: GitHub, refusals: int
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.refuse_merge = {"8": refusals}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    merges = _merges(github)
    if refusals == 1:
        assert merges == ["7", "8", "8", "9"]
        assert len(github.workflow.calls("sleep 10")) == 1
    else:
        assert merges == ["7", "8", "8", "8"]
        assert len(github.workflow.calls("sleep 10")) == 2
        assert "the squash of batch member 8 failed" in done.stdout
        assert github.pulls["8"]["state"] == "OPEN" and github.pulls["9"]["state"] == "OPEN"
        assert _batch_state(github)["prefixes"] is None
        assert _batch_state(github)["waiting"] == ["8", "9"]


def test_members_merged_by_another_call_are_noted_for_their_own_sessions(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    assert _batch_call(github).returncode == 0
    folder = github.workflow.repo / ".local/workflow"
    squashes = github.workflow.git("log", "--format=%H", "-3", "origin/main").splitlines()
    for number, squash in (("7", squashes[2]), ("8", squashes[1]), ("9", squashes[0])):
        note = json.loads((folder / f"batch-merged-{number}").read_text())
        assert note == {"by": "7", "head": github.pulls[number]["headRefOid"], "squash": squash}
    github.workflow.git("checkout", "ticket/8")
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already merged in a batch, through the gate call of pull request 7" in done.stdout
    assert _merges(github) == ["7", "8", "9"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("red", "main is red; remaining members use the single fix rule"),
        ("pushed-over", "main did not match the prefix squash"),
        ("on-view-of-8", "main moved during the merge train"),
    ],
)
def test_main_changing_after_the_callers_squash_reports_that_merge(
    github: GitHub, change: str, message: str
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_main = {"7": change}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pull request 7 merged in a batch (merged in this call: 7)" in done.stdout
    assert message in done.stdout
    assert _merges(github) == ["7"]
    train = _batch_state(github)
    assert train["waiting"] == ["8", "9"]
    # Rebuilt on the main the gate cannot vouch for only once it is green again.
    assert train["prefixes"] is None or change == "on-view-of-8"


def test_unknown_mergeability_keeps_a_member_out_of_a_new_batch(github: GitHub) -> None:
    _batch_setup(github)
    github.pulls["8"].update(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    done = _batch_call(github)
    # Only a squash this gate just made explains UNKNOWN; a new line waits for GitHub.
    assert done.returncode == 2, done.stdout + done.stderr
    assert _batch_state(github)["prefixes"] is None
    assert github.workflow.calls("gh workflow run") == []


def test_main_moving_while_a_squash_is_refused_stops_without_retrying(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.refuse_merge = {"8": 1}
    github.refusal_moves_main = True
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "main moved before batch member 8 merged; it needs fresh tests" in done.stdout
    assert github.workflow.calls("sleep 10") == []
    assert _merges(github) == ["7", "8"]
    assert _batch_state(github)["prefixes"] is None


def test_an_error_after_the_callers_squash_still_reports_and_keeps_that_merge(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_merge = {"7": {"8": {"fail_view": True}}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pull request 7 merged in a batch (merged in this call: 7); the merge train stopped" in (
        done.stdout
    )
    note = json.loads((github.workflow.repo / ".local/workflow/batch-merged-7").read_text())
    assert note["by"] == "7"
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already merged in a batch, through an earlier gate call of its own" in done.stdout
    assert _merges(github) == ["7"]


def test_an_isolated_failure_is_reported_to_its_own_caller(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github, conclusion="failure")
    done = _batch_call(github)
    # Its own PR checks stay green, so a wait would never be woken: it is a finding now.
    assert done.returncode == 1, done.stdout + done.stderr
    assert (
        "ci run 100 of pull request 7's prefix failed (CI result) on main and the members ahead "
        "of it; treat it as a failed check"
    ) in done.stderr
    assert _batch_state(github)["blocked"] == {"7": github.head}
    # The prefixes behind it are built again without it, at once.
    assert _numbers(github) == ["8", "9"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 5
    done = _batch_call(github)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "the ci run of this pull request's prefix" in done.stderr


@pytest.mark.parametrize("departure", ["closed", "new-head"])
def test_a_departed_isolated_failure_no_longer_keeps_its_branch(
    github: GitHub, departure: str
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    isolated = _scratch(github)
    assert isolated is not None
    _finish_batch(github, conclusion="failure")
    assert _batch_call(github).returncode == 1
    assert isolated["branch"] in _scratch_branches(github)
    if departure == "closed":
        github.pulls["7"]["state"] = "CLOSED"
    else:
        github.pulls["7"]["headRefOid"] = github.base  # Any other head: its fix.
    github.workflow.git("checkout", "ticket/8")
    assert _batch_call(github, "8").returncode == 3
    train = _batch_state(github)
    assert train["batch_failed"] == {}
    assert isolated["branch"] not in _scratch_branches(github)


def test_a_failed_fetch_after_the_callers_squash_still_records_that_merge(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_main = {"7": "unreachable"}
    done = _batch_call(github)
    origin = github.workflow.root / "origin.git"
    (github.workflow.root / "origin.git.away").rename(origin)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pull request 7 merged in a batch (merged in this call: 7); the merge train stopped" in (
        done.stdout
    )
    note = json.loads((github.workflow.repo / ".local/workflow/batch-merged-7").read_text())
    assert note == {"by": "7", "head": github.head}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already merged in a batch, through an earlier gate call of its own" in done.stdout


def test_main_moving_during_the_retry_wait_stops_before_the_next_squash(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.refuse_merge = {"8": 1}
    origin = github.workflow.root / "origin.git"
    # Another push lands while the gate waits to retry.
    (github.workflow.state / "on-sleep").write_text(
        f'moved=$(git --git-dir "{origin}" commit-tree "main^{{tree}}" -p main -m push)\n'
        f'git --git-dir "{origin}" update-ref refs/heads/main "$moved"\n'
        f'rm "{github.workflow.state}/on-sleep"\n'
    )
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "main moved before batch member 8 merged; it needs fresh tests" in done.stdout
    assert len(github.workflow.calls("sleep 10")) == 1
    assert _merges(github) == ["7", "8"]


@pytest.mark.parametrize(
    "hang, interruption",
    [
        ("read", None),
        ("push", None),
        ("merge", None),
        ("read-exited", None),
        ("push", signal.SIGINT),
        ("merge", signal.SIGTERM),
        ("push", signal.SIGHUP),
    ],
)
def test_stalled_commands_release_lock_and_stop_descendants(
    github: GitHub,
    hang: str,
    interruption: int | None,
) -> None:
    workflow = github.workflow
    if hang == "push":
        # Force the refresh path through a real git push and its pre-push hook.
        github.ci(github.base, github.head, tested_base=workflow.git("rev-parse", "main^"))
    github.save()
    # Programs live beside the stand-ins: CI's temporary folder does not allow running them.
    release = workflow.state / "release-descendant"
    os.mkfifo(release)
    stall = workflow.bin / "stall"
    stall.write_text(
        "#!/bin/bash\n"
        "trap '' TERM\n"
        # Open both ends so the child blocks until the test releases it, even without
        # another writer. Publish readiness only once the descendant holds the FIFO.
        f"( exec 3<> '{release}'; echo entered > '{workflow.state / 'entered'}'; "
        f"read -r <&3; echo escaped > '{workflow.state / 'escaped'}' ) &\n"
        + ("exit 0\n" if hang == "read-exited" else "wait\n/bin/sleep 600\n")
    )
    stall.chmod(0o755)
    if hang == "push":
        hooks = workflow.bin / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text(f"#!/bin/bash\nexec '{stall}'\n")
        hook.chmod(0o755)
        workflow.git("config", "core.hooksPath", str(hooks))
    else:
        original = (workflow.bin / "gh").read_text()
        workflow.fake("gh-real", original)
        match = "pr view" if hang.startswith("read") else "pr merge"
        workflow.fake(
            "gh",
            f'#!/bin/bash\nif [[ "$1 $2" == "{match}" ]]; then exec "{stall}"; fi\n'
            f'exec "{workflow.bin / "gh-real"}" "$@"\n',
        )
    # Short deadlines only in this test's own gate; production has no test timing switch.
    command_timeout = 10
    entry = workflow.root / "gate.sh"
    entry.write_text(
        "#!/bin/bash\nexec python3 -B -c 'import sys; "
        f'sys.path.insert(0, "{workflow.script.parent}"); '
        "import merge_gate as g; "
        f"g.COMMAND_TIMEOUT = {command_timeout}; "
        "g.READ_TIMEOUT = "
        f"{1 if hang.startswith('read') and not interruption else command_timeout}; "
        "g.READ_RETRY_DELAYS = (); runner = g.run_command; "
        "g.run_command = lambda *a, **kw: runner(*a, **dict(kw, timeout="
        f"{command_timeout if interruption else 1} if "
        f'(a[:2] == ("git", "push") or a[:3] == ("gh", "pr", "merge")) else {command_timeout})); '
        'sys.exit(g.main())\' "$@"\n'
    )
    process = subprocess.Popen(
        ["bash", str(entry), "7"],
        cwd=workflow.repo,
        env=workflow.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        if interruption:
            # Allow the test's gate its command budget to reach the stall under load.
            deadline = time.monotonic() + command_timeout
            while not (workflow.state / "entered").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (workflow.state / "entered").exists()
            process.send_signal(interruption)
        output, error = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    assert (workflow.state / "entered").exists(), output + error
    if interruption:
        assert process.returncode != 0, output + error
    else:
        assert process.returncode == 1, output + error
        assert "timed out" in error
    with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # No test scheduling delay can release the child before cleanup. A FIFO with
        # no readers rejects this open immediately; a surviving child gets permission
        # to mutate state while we hold the released lock, and fails the test.
        try:
            writer = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
        else:
            with os.fdopen(writer, "w") as pipe:
                pipe.write("go\n")
            pytest.fail("descendant ignoring TERM survived cleanup after lock release")
        assert not (workflow.state / "escaped").exists()
    assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("read", ["api", "pull"])
@pytest.mark.parametrize(
    "status, attempts",
    [(502, 3), (503, 4), (404, 1), ("timeout", 3), ("timeout-exhausted", 4), ("client-timeout", 3)],
)
def test_github_reads_retry_server_errors_only(
    github: GitHub,
    read: str,
    status: int,
    attempts: int,
) -> None:
    workflow = github.workflow
    original = (workflow.bin / "gh").read_text()
    workflow.fake("gh-real", original)
    match = '[[ "$1" == api ]]' if read == "api" else '[[ "$1 $2" == "pr view" ]]'
    counter = workflow.state / "read-attempts"
    if isinstance(status, str):
        entry = workflow.script.parent / "merge-when-current"
        entry.write_text(
            "#!/bin/bash\nexec python3 -B -c 'import sys; "
            f'sys.path.insert(0, "{entry.parent}"); '
            "import merge_gate as g; g.READ_TIMEOUT = 0.3; "
            "g.READ_RETRY_DELAYS = (0.01, 0.02, 0.04); "
            'sys.exit(g.main())\' "$@"\n'
        )
    failure = (
        "exec /bin/sleep 600"
        if status in ("timeout", "timeout-exhausted")
        else "echo 'context deadline exceeded' >&2; exit 1"
        if status == "client-timeout"
        else f"echo 'gh: Server Error (HTTP {status})' >&2; exit 1"
    )
    workflow.fake(
        "gh",
        f"""#!/bin/bash
if {match} && [[ ! -e "{workflow.state / "read-succeeded"}" ]]; then
  count=0
  [[ ! -f "{counter}" ]] || read -r count < "{counter}"
  count=$((count + 1))
  echo "$count" > "{counter}"
  if (( count <= {2 if status in (502, "timeout", "client-timeout") else 9} )); then
    {failure}
  fi
  touch "{workflow.state / "read-succeeded"}"
fi
exec "{workflow.bin / "gh-real"}" "$@"
""",
    )
    done = github.run()
    assert int(counter.read_text()) == attempts
    if status in (502, "timeout", "client-timeout"):
        assert done.returncode == 0, done.stderr
        assert (workflow.state / "merged").read_text() == "7"
    else:
        assert done.returncode == 1
        assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("interruption", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
@pytest.mark.parametrize("starved_cleanup", [False, True], ids=["normal", "starved-cleanup"])
def test_interrupted_outer_gate_stops_inner_gate_command_before_releasing_lock(
    github: GitHub,
    interruption: int,
    starved_cleanup: bool,
) -> None:
    workflow = github.workflow
    release = workflow.state / "release-inner-command"
    os.mkfifo(release)
    workflow.fake("gh-real", (workflow.bin / "gh").read_text())
    workflow.fake(
        "gh",
        f'''#!/bin/bash
if [[ "$1 $2" == "pr view" ]]; then
  trap '' TERM
  {'kill -STOP "$PPID"' if starved_cleanup else ":"}
  ( exec 3<> "{release}"; echo entered > "{workflow.state / "entered"}";
    read -r <&3; echo escaped > "{workflow.state / "escaped"}" ) &
  wait
fi
exec "{workflow.bin / "gh-real"}" "$@"
''',
    )
    github.save()
    outer = workflow.bin / "outer.py"
    outer.write_text(
        f"""import os
import signal
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, {str(workflow.script.parent)!r})
import merge_gate as g
g.install_signal_handlers()
communicate = subprocess.Popen.communicate
def scheduled(self, *args, **kwargs):
    if {starved_cleanup!r} and Path({str(workflow.state / "entered")!r}).exists():
        # A stopped gate cannot clean up until scheduled. Expire any bounded
        # cleanup wait immediately; an unbounded wait lets its handler run.
        if kwargs.get("timeout") is not None:
            raise subprocess.TimeoutExpired(self.args, kwargs["timeout"])
        try:
            os.kill(self.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    return communicate(self, *args, **kwargs)
subprocess.Popen.communicate = scheduled
g.command("bash", {str(workflow.script.parent / "merge-when-current")!r}, "7")
"""
    )
    process = subprocess.Popen(
        ["python3", "-B", str(outer)],
        cwd=workflow.repo,
        env=workflow.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not (workflow.state / "entered").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (workflow.state / "entered").exists()
        process.send_signal(interruption)
        output, error = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    assert (workflow.state / "entered").exists(), output + error
    assert process.returncode != 0, output + error
    with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Readiness is published only after the descendant holds the FIFO. A live
        # reader after outer-gate exit proves cleanup released the lock too early.
        try:
            writer = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
        else:
            with os.fdopen(writer, "w") as pipe:
                pipe.write("go\n")
            pytest.fail("inner command survived cleanup after lock release")
        assert not (workflow.state / "escaped").exists()
    assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize(
    "interruption", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT]
)
def test_interrupt_during_spawn_stops_command_before_releasing_lock(
    workflow: Workflow, interruption: int
) -> None:
    # Reproduce the spawn window deterministically: Popen has created a live child,
    # but has not returned the handle to _run when the signal arrives.
    entered = workflow.state / "entered"
    release = workflow.state / "release-spawned-command"
    os.mkfifo(release)
    workflow.fake(
        "stall",
        "#!/bin/bash\ntrap '' TERM\n"
        f'exec 3<> "{release}"\n'
        f'echo entered > "{entered}"\nread -r <&3\n'
        f'echo escaped > "{workflow.state / "escaped"}"\n',
    )
    runner = workflow.bin / "interrupt-spawn.py"
    runner.write_text(
        f"""import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, {str(workflow.script.parent)!r})
import workflow_commands as w
w.install_signal_handlers()
original = subprocess.Popen.__init__
children = []
def interrupted_spawn(self, *args, **kwargs):
    original(self, *args, **kwargs)
    children.append(self)
    deadline = time.monotonic() + 5
    while not Path({str(entered)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert Path({str(entered)!r}).exists()
    os.kill(os.getpid(), {int(interruption)})
subprocess.Popen.__init__ = interrupted_spawn
try:
    with open({str(workflow.state / "lock")!r}, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            w.run("stall")
        except (SystemExit, KeyboardInterrupt):
            pass
        else:
            raise AssertionError("signal was swallowed")
    assert children[0].poll() is not None, "spawned command survived lock release"
    assert not Path({str(workflow.state / "escaped")!r}).exists()
finally:
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=5)
"""
    )
    done = subprocess.run(
        ["python3", "-B", str(runner)],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_nested_gate_is_not_cut_off_by_the_outer_command_deadline(github: GitHub) -> None:
    # The tick's `--drive` gate first waits for the lock another gate holds through its
    # push, so its whole run may outlast the deadline of any one command.
    workflow = github.workflow
    workflow.fake("gh-real", (workflow.bin / "gh").read_text())
    workflow.fake(
        "gh",
        f'''#!/bin/bash
if [[ "$1 $2" == "pr view" ]]; then /bin/sleep 1.5; fi
exec "{workflow.bin / "gh-real"}" "$@"
''',
    )
    github.save()
    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
            "g.COMMAND_TIMEOUT = 1; "
            'done = g.command("bash", sys.argv[1] + "/merge-when-current", "7", check=False); '
            "print(done.stdout + done.stderr); sys.exit(done.returncode)",
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"


@pytest.mark.parametrize("status, attempts", [(502, 3), (403, 1)])
def test_git_reads_of_github_retry_server_errors_only(
    github: GitHub, status: int, attempts: int
) -> None:
    workflow = github.workflow
    real = subprocess.check_output(["bash", "-c", "command -v git"], text=True).strip()
    counter = workflow.state / "git-read-attempts"
    url = "https://github.com/example-org/example-app.git/"
    workflow.fake(
        "git",
        f"""#!/bin/bash
if [[ "$1" == fetch || "$1" == ls-remote ]]; then
  count=0
  [[ ! -f "{counter}" ]] || read -r count < "{counter}"
  count=$((count + 1))
  echo "$count" > "{counter}"
  echo "$*" >> "{workflow.state / "git-reads"}"
  if (( count <= 2 )); then
    echo "fatal: unable to access '{url}': The requested URL returned error: {status}" >&2
    exit 128
  fi
fi
exec "{real}" "$@"
""",
    )
    done = github.run()
    assert int(counter.read_text()) >= attempts
    if status == 502:
        assert done.returncode == 0, done.stderr
        assert (workflow.state / "merged").read_text() == "7"
    else:
        # Each read is tried once: the refused fetch and the later batch-branch listing.
        reads = (workflow.state / "git-reads").read_text().splitlines()
        assert len(reads) == len(set(reads)), reads
        assert done.returncode == 1
        assert "returned error: 403" in done.stderr
        assert not (workflow.state / "merged").exists()


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


@pytest.mark.parametrize("message", MEMORY_SHORTAGES)
def test_a_batch_push_short_of_memory_is_retried_a_minute_later(
    github: GitHub, message: str
) -> None:
    _batch_setup(github)
    short_of_memory(github.workflow, message, times=1)

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert all(member["scratch"]["pushed"] for member in _prefixes(github))
    assert sorted(_scratch_branches(github)) == sorted(
        member["scratch"]["branch"] for member in _prefixes(github)
    )
    # The first prefix's push twice, a minute apart, then the two behind it.
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 4
    assert len(github.workflow.calls("sleep 60")) == 1


def test_a_batch_push_that_fails_twice_keeps_the_batch_for_the_next_call(github: GitHub) -> None:
    _batch_setup(github)
    short_of_memory(github.workflow, MEMORY_SHORTAGES[1], times=2)

    done = _batch_call(github)

    # A wait, not a refusal: nothing about the members changed.
    assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    assert "could not lock memory" in _refusal(done)
    train = _batch_state(github)
    assert _numbers(github) == ["7", "8", "9"]
    assert not any(member["scratch"]["pushed"] for member in _prefixes(github))
    assert sorted(train["approved"]) == ["7", "8", "9"]
    assert _scratch_branches(github) == []
    assert github.workflow.calls("gh workflow run") == []
    branches = [member["scratch"]["branch"] for member in _prefixes(github)]

    assert _batch_call(github).returncode == 3
    assert [member["scratch"]["branch"] for member in _prefixes(github)] == branches
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert [call.split()[3] for call in github.workflow.calls("gh pr merge")] == ["7", "8", "9"]


def test_a_batch_push_short_of_memory_on_standard_output_is_never_counted(
    github: GitHub,
) -> None:
    _batch_setup(github)
    # The scan prints Semgrep's report, io_uring error and all, on standard output; git's own
    # "failed to push some refs" is all that reaches standard error.
    short_of_memory(github.workflow, MEMORY_SHORTAGES[0], times=6, stream=1)

    for _ in range(3):
        done = _batch_call(github)
        assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    batch = _scratch(github)
    assert batch is not None
    assert batch["pushed"] is False and "push_failures" not in batch
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 6
    assert len(github.workflow.calls("sleep 60")) == 3
    assert _batch_call(github).returncode == 3
    assert all(member["scratch"]["pushed"] for member in _prefixes(github))


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


def test_every_push_of_the_gate_runs_its_hook_with_the_posix_backend(github: GitHub) -> None:
    _stale(github)
    short_of_memory(github.workflow, MEMORY_SHORTAGES[0], times=0)

    # Even a caller whose own environment names another backend.
    github.save()
    done = github.workflow.run_script("merge-when-current", "7", env={"EIO_BACKEND": "io_uring"})

    assert done.returncode == 2, done.stdout + done.stderr
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"]
    assert github.workflow.calls("sleep 60") == []


@pytest.mark.parametrize("message", MEMORY_SHORTAGES)
def test_a_refresh_push_short_of_memory_is_retried_a_minute_later(
    github: GitHub, message: str
) -> None:
    _stale(github)
    short_of_memory(github.workflow, message, times=1)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    workflow = github.workflow
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[
        0
    ] == workflow.git("rev-parse", "HEAD")
    # The minute is waited between the two pushes, not after them.
    assert hook_runs_and_waits(workflow) == [
        "pre-push EIO_BACKEND=posix",
        "sleep 60",
        "pre-push EIO_BACKEND=posix",
    ]


@pytest.mark.parametrize("stream", [1, 2])
@pytest.mark.parametrize("message", MEMORY_SHORTAGES)
def test_a_refresh_push_that_fails_twice_is_refused_naming_the_error(
    github: GitHub, message: str, stream: int
) -> None:
    _stale(github)
    short_of_memory(github.workflow, message, times=2, stream=stream)

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    # The scan's own words on the line the wake shows, not "$ sh scripts/security-scan.sh all".
    refusal = _refusal(done)
    assert refusal.startswith("merge refused: could not push the refreshed branch: "), refusal
    assert message in refusal and "a minute apart" in refusal, refusal
    assert len(github.workflow.calls("sleep 60")) == 1
    assert (
        github.workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]
        == github.head
    )
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 2


# Any other program out of memory (lint, the web tests) is not the scan's locked memory: a
# retry would not help, and a batch would wait on it for ever.
@pytest.mark.parametrize(
    "message", ["secret found in config.py", "bash: fork: Cannot allocate memory"]
)
def test_a_push_failing_for_another_reason_is_not_retried(github: GitHub, message: str) -> None:
    _stale(github)
    short_of_memory(github.workflow, message, times=1)

    done = github.run()

    assert done.returncode == 1 and message in done.stderr, done.stdout + done.stderr
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"]
    assert github.workflow.calls("sleep 60") == []


def test_a_batch_push_the_hook_keeps_refusing_is_refused_on_the_third_call(
    github: GitHub,
) -> None:
    _batch_setup(github)
    # Not a memory shortage: no retry within a call, and it never clears.
    short_of_memory(github.workflow, "secret found in config.py", times=99)

    for _ in range(2):
        done = _batch_call(github)
        assert done.returncode == 1 and "merge waiting" in done.stderr, done.stderr
        assert _numbers(github) == ["7", "8", "9"]
    done = _batch_call(github)

    assert done.returncode == 1 and "merge refused" in done.stderr, done.stderr
    assert "secret found" in done.stderr and "3 calls in a row" in done.stderr
    # The first prefix could not be pushed, and every prefix behind it holds it.
    assert _batch_state(github)["prefixes"] is None
    assert hook_runs(github.workflow) == ["EIO_BACKEND=posix"] * 3
    assert _scratch_branches(github) == []
    assert github.workflow.calls("gh workflow run") == []


def test_a_batch_push_that_stalls_past_its_deadline_waits_for_the_next_call(
    github: GitHub,
) -> None:
    _batch_setup(github)
    workflow = github.workflow
    hooks = workflow.bin / "hooks"
    stalled = workflow.state / "stalled"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-push").write_text(
        f'#!/bin/bash\n[ -e "{stalled}" ] && exit 0\ntouch "{stalled}"\nexec /bin/sleep 30\n'
    )
    (hooks / "pre-push").chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))
    github.save()

    done = subprocess.run(
        [
            "python3",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
            "g.COMMAND_TIMEOUT = 5; sys.argv = ['merge-when-current', '7']; "
            "sys.exit(g.main())",
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    batch = _scratch(github)
    assert batch is not None
    assert batch["pushed"] is False and batch["push_failures"] == 1
    assert _batch_call(github).returncode == 3
    assert all(member["scratch"]["pushed"] for member in _prefixes(github))


# The package audit's own last lines (scripts/audit-against-base.py): a service it could not
# reach after its tries, and an advisory the branch brings.
UNREACHABLE = (
    "Refused because npm's advisory service could not be reached; "
    "nothing was found wrong with this branch."
)
ADVISORY = "Refused: this branch brings advisories that main does not have:\n  npm: GHSA-1 in vite"


def test_a_refresh_push_refused_for_an_advisory_is_still_refused(github: GitHub) -> None:
    _stale(github)
    short_of_memory(github.workflow, ADVISORY, times=1)

    done = github.run()

    assert done.returncode == 1 and "merge refused" in done.stderr, done.stdout + done.stderr
    assert "GHSA-1" in done.stderr
    assert not (github.workflow.repo / ".local/workflow/advisory-service-down").exists()


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


def test_main_red_for_the_audit_service_and_its_images_does_not_say_nothing_was_wrong(
    github: GitHub,
) -> None:
    _dependencies_red(github, annotation=_MAIN_UNREACHABLE)
    _images(github, pull=False, conclusion="failure")
    github.save()
    advisory_service_answers(github.workflow, answers=False)

    line = github.workflow.run_script("main-status").stdout

    assert "main: red" in line and "Docker images: image tests" in line
    assert "nothing was found wrong with main" not in line


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


@pytest.mark.parametrize("reason", ["no annotation", "another check failed too"])
def test_a_pull_request_red_for_more_than_the_audit_service_is_refused(
    github: GitHub, reason: str
) -> None:
    pull_dependencies_red(github, annotation=None if reason == "no annotation" else UNREACHABLE)
    if reason == "another check failed too":
        github.pulls["7"]["statusCheckRollup"].append(
            {"name": "CI: web", "status": "COMPLETED", "conclusion": "FAILURE"}
        )
        github.save()

    done = github.run()

    assert done.returncode == 1 and "merge refused" in done.stderr, done.stdout + done.stderr
    assert "failed checks" in done.stderr


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


@pytest.mark.parametrize(
    "own",
    [
        "selected: 3 of 48 spec files",
        # Before the planner noted the kind of run, nothing says every spec ran.
        None,
        # The whole suite, but of another tree than the merge CI tested.
        "whole on another tree",
    ],
)
def test_a_pull_request_merges_only_after_the_whole_browser_suite_on_its_tested_merge(
    github: GitHub, own: str | None
) -> None:
    tree = _changes(github)
    run = 900_020
    if own == "whole on another tree":
        github.e2e(github.head, github.base, run_id=run)
    else:
        github.e2e(github.head, tree, suite=own or "", run_id=run)
        if own is None:
            github.api[f"{_REPO}/check-runs/{run + 1}/annotations?per_page=100"] = []

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert "waiting for the whole browser suite to start" in done.stdout
    assert not (github.workflow.state / "merged").exists()
    suite = _suite(github)
    assert _scratch_branches(github) == [suite["branch"]]
    # The scratch commit is the tested merge: main and the pull request, with CI's files.
    assert github.workflow.git("rev-parse", f"{suite['head']}^{{tree}}") == tree
    assert github.workflow.git("show", "-s", "--format=%P", suite["head"]).split() == [
        github.base,
        github.head,
    ]
    assert len(github.workflow.calls(f"gh workflow run e2e.yml -R {_REPO[6:]}")) == 1
    assert _batch_call(github).returncode == 3  # No second dispatch while it starts.
    assert len(github.workflow.calls("gh workflow run")) == 1

    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert "suites" not in _batch_state(github) or _batch_state(github)["suites"] == {}
    assert _scratch_branches(github) == []


def test_a_dispatched_run_that_was_not_the_whole_suite_never_merges(github: GitHub) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(
        suite["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_000,
        branch=suite["branch"],
    )

    done = _batch_call(github)

    assert done.returncode == 1
    assert "merge waiting: the run of the whole browser suite did not prove its tested files" in (
        done.stderr
    )
    assert not (github.workflow.state / "merged").exists()
    assert _batch_state(github)["suites"] == {}
    # The retest dispatches a new scratch run of the same tested merge; the same shortfall
    # again is a refusal for a reviewer.
    assert _batch_call(github).returncode == 3
    retested = _suite(github)
    assert retested["branch"] != suite["branch"] and retested["tree"] == tree
    github.e2e(
        retested["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_100,
        branch=retested["branch"],
    )

    done = _batch_call(github)

    assert done.returncode == 1
    assert (
        "merge refused: the run of the whole browser suite did not prove its tested files, "
        "twice in a row on the same tested files"
    ) in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_whole_suite_retest_that_passes_merges_and_forgets_the_shortfall(
    github: GitHub,
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(
        suite["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_000,
        branch=suite["branch"],
    )
    assert "merge waiting" in _batch_call(github).stderr
    assert _batch_state(github)["retest"]["tree"] == tree
    assert _batch_call(github).returncode == 3
    retested = _suite(github)
    github.e2e(retested["head"], tree, run_id=950_100, branch=retested["branch"])

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert _batch_state(github)["retest"] is None


def test_the_same_shortfall_on_other_tested_files_is_still_a_wait(github: GitHub) -> None:
    reason = "the run of the whole browser suite did not prove its tested files"
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    # The last shortfall had the same reason on the tree of an older main.
    train = _batch_state(github)
    other = github.workflow.git("rev-parse", f"{github.base}^{{tree}}")
    assert other != tree
    train["retest"] = {"tree": other, "reason": reason}
    (github.workflow.repo / ".local/workflow/merge-train.json").write_text(json.dumps(train))
    github.e2e(
        suite["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_000,
        branch=suite["branch"],
    )

    done = _batch_call(github)

    assert done.returncode == 1
    assert f"merge waiting: {reason}; retesting next call" in done.stderr
    assert _batch_state(github)["retest"] == {"tree": tree, "reason": reason}


def test_a_failed_whole_suite_refuses_until_its_rerun_passes(github: GitHub) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"], conclusion="failure")

    done = _batch_call(github)

    assert done.returncode == 1
    assert "run 950000, failed on this pull request's tested merge (playwright)" in done.stderr
    # Blocked from then on, as a failed check is, and told so again while it stays red.
    assert _train(github)["blocked"] == ["7"]
    again = _batch_call(github)
    assert again.returncode == 1
    assert "a whole suite run on this pull request's tested merge failed" in again.stderr
    assert not (github.workflow.state / "merged").exists()
    # The same run, rerun and green: no new dispatch, and the merge goes ahead.
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(github.workflow.calls("gh workflow run")) == 1


def test_a_pull_request_whose_own_run_was_the_whole_suite_merges_at_once(github: GitHub) -> None:
    _changes(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []


def test_an_own_whole_suite_whose_failed_jobs_alone_were_rerun_merges_at_once(
    github: GitHub,
) -> None:
    _changes(github)
    _rerun_failed_jobs(github, 900_020, github.head, "E2E: what to run")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []


def test_a_workflow_only_change_needs_no_browser_run(github: GitHub) -> None:
    _changes(github, "scripts/workflow/notes.md")
    del github.api[
        f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ]
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []
    assert not [call for call in github.workflow.calls("gh api") if "e2e.yml" in call]


def test_a_batch_runs_the_whole_browser_suite_on_its_tested_merge(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    assert all(m["scratch"]["workflows"] == ["ci", "e2e"] for m in _prefixes(github))
    assert len(github.workflow.calls("gh workflow run e2e.yml")) == 3
    _finish_batch(github)
    batch = _scratch(github)
    assert batch is not None
    github.e2e(
        batch["head"],
        _prefixes(github)[0]["tree"],
        suite="selected: 3 of 48 spec files",
        run_id=110,
        branch=batch["branch"],
    )
    github.save()
    before = _train(github)["turn"]

    done = _batch_call(github)

    assert done.returncode == 1
    assert (
        "merge waiting: pull request 7's prefix e2e did not prove the whole browser suite"
        in done.stderr
    )
    assert github.workflow.calls("gh pr merge") == []
    # Its reviewer, which waits in the line, is woken once more for the retest.
    assert _train(github)["turn"] > before


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_a_finished_whole_suite_run_changes_the_train_wakeup_once(
    github: GitHub, conclusion: str
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    github.save()
    before = _train(github)["turn"]
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"], conclusion=conclusion)
    github.save()
    train = _train(github)
    assert train["turn"] > before
    # A failed run leaves it first and waiting, so the tick wakes its reviewer to be told.
    assert train["first"] == "7" and train["waiting"] == ["7"] and train["blocked"] == []
    assert _train(github)["turn"] == train["turn"]


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


@pytest.mark.parametrize("suite", ["failure", "running"])
def test_a_whole_suite_that_failed_or_still_runs_lets_a_green_pull_request_go_first(
    github: GitHub, suite: str
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    second = _green_second(github)
    record = _suite(github)
    github.e2e(
        record["head"],
        tree,
        run_id=950_000,
        branch=record["branch"],
        conclusion="failure" if suite == "failure" else "success",
    )
    if suite == "running":
        path = (
            f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={record['head']}"
            "&event=workflow_dispatch&per_page=100"
        )
        github.api[path]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    else:
        # Its reviewer, woken by the finished run, is told and the pull request is blocked.
        assert _batch_call(github).returncode == 1
    github.save()

    # Without the whole suite counted, 8 would wait behind 7.
    done = github.workflow.run_script("merge-when-current", "8", cwd=second)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "8"
    train = _train(github)
    # A blocked 7 is first again only once a rerun of its suite passes.
    assert train["first"] == (None if suite == "failure" else "7")
    assert train["blocked"] == (["7"] if suite == "failure" else [])


def test_a_local_merge_unlike_the_tested_one_is_refused_before_any_run(github: GitHub) -> None:
    _changes(github)
    merge = github.api[f"{_REPO}/check-runs/21/annotations?per_page=100"][0]["message"]
    other = github.workflow.git("rev-parse", f"{github.base}^{{tree}}")
    github.api[f"{_REPO}/git/commits/{merge}"]["tree"]["sha"] = other

    done = _batch_call(github)

    assert done.returncode == 1
    assert "a local merge of this pull request differs from the merge CI tested" in done.stderr
    assert github.workflow.calls("gh workflow run") == []
    assert not (github.workflow.state / "merged").exists()


def test_a_merge_that_changes_nothing_needs_no_browser_run(github: GitHub) -> None:
    # Pull request 7 of the fixture is an empty commit: its merge is main's own tree.
    del github.api[
        f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ]
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [call for call in github.workflow.calls("gh") if "e2e.yml" in call]


def _selected_server_tests(github: GitHub, server: str | None = "selected: 4 of 374 files") -> str:
    """Pull request 7's own CI ran only the server tests its change reaches; its tested tree."""
    tree = _changes(github, "src/example_app/reader.py")
    github.ci(github.base, github.head, server=server)
    # A server change no spec reaches: its own E2E run selected nothing.
    github.e2e(github.head, tree, suite="selected: 0 of 48 spec files", run_id=900_020)
    return tree


@pytest.mark.parametrize(
    "own",
    [
        "selected: 4 of 374 files",
        # Before the planner noted the kind of run, nothing says every server test ran.
        None,
    ],
)
def test_a_pull_request_merges_only_after_every_server_test_on_its_tested_merge(
    github: GitHub, own: str | None
) -> None:
    tree = _selected_server_tests(github, own)

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert "waiting for every server test to start" in done.stdout
    assert not (github.workflow.state / "merged").exists()
    suite = _suite(github)
    assert suite["workflows"] == ["ci", "e2e"]
    assert github.workflow.git("rev-parse", f"{suite['head']}^{{tree}}") == tree
    assert len(github.workflow.calls(f"gh workflow run ci.yml -R {_REPO[6:]}")) == 1
    assert _batch_call(github).returncode == 3  # No second dispatch while it starts.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1

    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert _scratch_branches(github) == []


@pytest.mark.parametrize(
    "proof",
    [
        {"server": "selected: 4 of 374 files"},
        # Green, but of another commit than the gate's tested merge.
        {"merge": "0" * 40},
        # Green, with no server test run at all.
        {"python": "skipped"},
    ],
)
def test_a_dispatched_ci_run_that_was_not_every_server_test_never_merges(
    github: GitHub, proof: dict[str, str]
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000, **proof)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])

    done = _batch_call(github)

    assert done.returncode == 1
    assert "the run of every server test did not prove its tested files" in done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert _batch_state(github)["suites"] == {}


def test_a_failed_run_of_every_server_test_refuses_until_its_rerun_passes(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000, conclusion="failure")

    done = _batch_call(github)

    assert done.returncode == 1
    assert (
        "every server test, run 960000, failed on this pull request's tested merge "
        "(CI: server tests, CI result)"
    ) in done.stderr
    assert _train(github)["blocked"] == ["7"]
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1


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


def test_a_run_of_every_server_test_red_for_dependencies_without_the_service_note_refuses(
    github: GitHub,
) -> None:
    _server_suite_dependencies_red(github, annotation=None)
    advisory_service_answers(github.workflow, answers=True)

    done = _batch_call(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "every server test, run 960000, failed" in done.stderr
    assert _train(github)["blocked"] == ["7"]
    assert rerun_calls(github.workflow) == []


def test_the_fix_of_a_red_python_check_merges_only_after_every_server_test_passed(
    github: GitHub,
) -> None:
    github.main("failure")
    main_jobs = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]
    main_jobs[1]["name"] = "CI: server tests"
    tree = _selected_server_tests(github)
    # Its own "CI: server tests" passed, on the test files its change reaches.
    _pr_job(github, "CI: server tests", "success")

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1
    suite = _suite(github)
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert "passed every check failing on main: CI result, CI: server tests" in done.stdout


def test_a_browser_suite_record_saved_before_server_tests_keeps_its_run(github: GitHub) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    state = json.loads(path.read_text())
    record = state["suites"]["7"]
    assert record.pop("workflows") == ["e2e"]
    path.write_text(json.dumps(state))
    # Its CI is then known to have run a selection of the server tests.
    github.ci(github.base, github.head, server="selected: 4 of 374 files")
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    suite = _suite(github)
    assert [suite["branch"], suite["head"], suite["workflows"]] == [
        record["branch"],
        record["head"],
        ["ci", "e2e"],
    ]
    assert len(github.workflow.calls("gh workflow run e2e.yml")) == 1
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    assert _batch_call(github).returncode == 3  # Every server test has not run yet.
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"


def test_a_browser_suite_saved_before_server_tests_still_runs_while_others_go_first(
    github: GitHub,
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    state = json.loads(path.read_text())
    del state["suites"]["7"]["workflows"]
    path.write_text(json.dumps(state))
    second = _green_second(github)
    record = _suite(github)
    github.e2e(record["head"], tree, run_id=950_000, branch=record["branch"])
    runs = (
        f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={record['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[runs]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()

    # Read as the browser suite's record, its run still going lets 8 merge first.
    done = github.workflow.run_script("merge-when-current", "8", cwd=second)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "8"


def test_a_finished_run_of_every_server_test_changes_the_train_wakeup_once(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    github.save()
    before = _train(github)["turn"]
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.save()
    train = _train(github)
    assert train["turn"] > before
    assert _train(github)["turn"] == train["turn"]


def test_a_run_of_every_server_test_still_going_lets_a_green_pull_request_go_first(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    second = _green_second(github)
    record = _suite(github)
    github.e2e(record["head"], tree, run_id=950_000, branch=record["branch"])
    github.ci_dispatched(record["head"], record["branch"], run_id=960_000)
    path = (
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={record['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[path]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()

    done = github.workflow.run_script("merge-when-current", "8", cwd=second)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "8"
    assert _train(github)["first"] == "7"


@pytest.mark.parametrize("own", ["whole", "none"])
def test_a_pull_request_whose_ci_ran_every_server_test_or_needed_none_merges_at_once(
    github: GitHub, own: str
) -> None:
    _changes(github)
    github.ci(github.base, github.head, server=own)

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []


def test_a_batch_whose_ci_ran_a_selection_never_merges(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    batch = _scratch(github)
    assert batch is not None
    notes = f"{_REPO}/check-runs/101/annotations?per_page=100"
    assert github.api[notes][1] == {"title": "Server tests", "message": "whole"}
    github.api[notes][1]["message"] = "selected: 4 of 374 files"

    done = _batch_call(github)

    assert done.returncode == 1
    assert "pull request 7's prefix ci did not prove its tested files" in done.stderr
    assert github.workflow.calls("gh pr merge") == []
    assert batch["workflows"][0] == "ci"


@pytest.mark.parametrize("python", ["skipped", None])
def test_a_batch_whose_ci_did_not_pass_its_python_job_never_merges(
    github: GitHub, python: str | None
) -> None:
    # The same proof as a pull request merging alone (whole_server_tests): the note and a
    # passed "CI: server tests", here skipped or missing.
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    found = github.api[f"{_REPO}/actions/runs/100/jobs?per_page=100"]["jobs"]
    python_job = next(job for job in found if job["name"] == "CI: server tests")
    if python is None:
        found.remove(python_job)
    else:
        python_job["conclusion"] = python

    done = _batch_call(github)

    assert done.returncode == 1
    assert "pull request 7's prefix ci did not prove its tested files" in done.stderr
    assert github.workflow.calls("gh pr merge") == []


def test_a_prefix_whose_whole_suite_fails_blocks_its_member(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    batch = _scratch(github)
    assert batch is not None
    github.e2e(
        batch["head"],
        _prefixes(github)[0]["tree"],
        run_id=110,
        branch=batch["branch"],
        conclusion="failure",
    )

    done = _batch_call(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "e2e run 110 of pull request 7's prefix failed (playwright)" in done.stderr
    assert github.workflow.calls("gh pr merge") == []
    assert _batch_state(github)["blocked"] == {"7": github.head}
    assert _numbers(github) == ["8", "9"]


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


@pytest.mark.parametrize(
    "reason", ["no annotation", "another job failed too", "hosted", "unreadable annotation"]
)
def test_a_batch_red_for_more_than_the_audit_service_is_isolated(
    github: GitHub, reason: str
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    batch_dependencies_red(
        github, annotation=None if reason == "no annotation" else UNREACHABLE, reason=reason
    )
    if reason == "unreadable annotation":
        del github.api[f"{_REPO}/check-runs/103/annotations?per_page=100"]
    advisory_service_answers(github.workflow, answers=True)

    done = _batch_call(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "ci run 100 of pull request 7's prefix failed" in done.stderr
    assert _batch_state(github)["blocked"] == {"7": github.head}
    assert _numbers(github) == ["8", "9"]
    assert rerun_calls(github.workflow) == []


@pytest.mark.parametrize("count", [2, 3])
def test_a_complete_receipt_inherits_every_job_with_python_shards(
    github: GitHub, count: int
) -> None:
    _pr_parts(github, *(["success"] * count))
    jobs = ("CI: older version on new tables", "CI: web", "CI: lint")
    for index, name in enumerate(jobs):
        github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"].append(
            {"id": 60 + index, "name": name, "status": "completed", "conclusion": "success"}
        )
    _squash_with_pending_main(github)
    receipt = _receipt(github)
    assert receipt["ci_unproven"] == []
    assert receipt["ci_passed"] == sorted(["CI result", "CI: what to run", _PYTHON, *jobs])
    for index, name in enumerate(receipt["ci_passed"]):
        if name == "CI: what to run":
            github.api[f"{_REPO}/check-runs/{100 + index}/annotations?per_page=100"] = []
    _main_jobs(github, *[(name, "in_progress", None) for name in receipt["ci_passed"]])
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "inherited from CI run 20" in status.stdout


def test_successful_older_version_noop_keeps_prefix_testing(github: GitHub) -> None:
    _batch_setup(github)
    for number in (7, 8, 9):
        github.api[f"{_REPO}/actions/runs/{number * 1000}/jobs?per_page=100"]["jobs"].append(
            {"id": 30000 + number, "name": _OLDER, "status": "completed", "conclusion": "success"}
        )
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _numbers(github) == ["7", "8", "9"]
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


@pytest.mark.parametrize("broken", ["", "tree", "branch", "matrix"])
def test_gate_verifies_reused_image_evidence_before_merging(github: GitHub, broken: str) -> None:
    _images(github)
    current = github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"]
    current[1]["conclusion"] = "skipped"
    current.append(
        {
            "id": 44,
            "name": "Docker images: browser tests",
            "status": "completed",
            "conclusion": "skipped",
        }
    )
    github.api[f"{_REPO}/check-runs/41/annotations?per_page=100"].append(
        {"title": "Inherited image run", "message": "80"}
    )
    github.api[f"{_REPO}/actions/runs/80"] = {
        "id": 80,
        "event": "pull_request",
        "path": ".github/workflows/images.yml",
        "head_branch": "other" if broken == "branch" else "ticket/seven",
        "status": "completed",
        "conclusion": "success",
    }
    original = [
        {
            "id": 81,
            "name": "Docker images: what to run",
            "status": "completed",
            "conclusion": "success",
        },
        {
            "id": 82,
            "name": "Docker images: image tests",
            "status": "completed",
            "conclusion": "success",
        },
        *[
            {
                "id": 82 + i,
                "name": f"Docker images: browser tests ({i} of 3)",
                "status": "completed",
                "conclusion": "success",
            }
            for i in range(1, 3 if broken == "matrix" else 4)
        ],
    ]
    github.api[f"{_REPO}/actions/runs/80/jobs?per_page=100"] = {"jobs": original}
    github.api[f"{_REPO}/check-runs/81/annotations?per_page=100"] = [
        {
            "title": "Files tested",
            "message": "other"
            if broken == "tree"
            else github.workflow.git("rev-parse", f"{github.head}^{{tree}}"),
        }
    ]
    done = github.run()
    assert done.returncode == (1 if broken else 0), done.stdout + done.stderr
    assert (github.workflow.state / "merged").exists() is (not broken)


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
    checks = workflow.script.with_name("pre-train-checks")
    checks.write_text("#!/bin/bash\n" + script + "\nexit 0\n")
    checks.chmod(0o755)


def _waiting(workflow: Workflow) -> list[str]:
    path = workflow.repo / ".local/workflow/merge-train.json"
    return json.loads(path.read_text())["waiting"] if path.exists() else []


def test_a_handoff_into_a_long_line_reads_no_other_member_under_the_merge_lock(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _handing_off(github)
    line = [str(number) for number in range(8, 58)]
    for number in line:
        github.pulls[number] = {
            **deepcopy(github.pulls["7"]),
            "headRefOid": f"{int(number):040x}",
            "headRefName": f"ticket/{number}",
        }
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": line}))

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (folder / "merge-wanted-7").exists()
    assert _waiting(workflow) == [*line, "7"]
    # Nothing is asked of GitHub's API under the lock, and the line's members are not read at all.
    assert _locked_calls(workflow) == []
    assert workflow.calls("gh pr view 7 ")[-1:] and len(_held(workflow)) == 1
    others = [github.pulls[number]["headRefOid"] for number in line]
    assert not [
        call
        for call in workflow.calls("gh")
        if call.startswith(tuple(f"gh pr view {number} " for number in line))
        or any(head in call for head in others)
    ]
    assert re.search(
        r"^[0-9]+ handoff 7 held the merge lock for [0-9]+\.[0-9] s$",
        workflow.wake_log.read_text(),
        re.MULTILINE,
    )


def test_another_gate_call_gets_the_merge_lock_while_a_handoff_reads_github(
    github: GitHub,
) -> None:
    workflow = github.workflow
    worktree = _handing_off(github)
    entered = workflow.state / "call-entered"
    release = workflow.state / "call-continue"
    os.mkfifo(entered)
    os.mkfifo(release)
    # GitHub is slow to answer the handoff's read of its reviews.
    (workflow.state / "pause-call").write_text("pulls/7/reviews")
    entered_fd = os.open(entered, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    handoff = subprocess.Popen(
        ["bash", str(workflow.script.with_name("merge-when-current")), "--handoff", "7"],
        cwd=worktree,
        env=workflow.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([entered_fd], [], [], 15)[0], "the handoff never read its reviews"
        assert os.read(entered_fd, 100) == b"entered\n"
        # Another gate call waiting for the lock gets it while the handoff waits on GitHub.
        got = None
        started = time.monotonic()
        with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
            while got is None and time.monotonic() - started < 5:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = time.monotonic() - started
                except BlockingIOError:
                    time.sleep(0.05)
        os.write(release_fd, b"x")
        out, err = handoff.communicate(timeout=60)
    finally:
        if handoff.poll() is None:
            handoff.kill()
            handoff.communicate()
        os.close(entered_fd)
        os.close(release_fd)
    assert got is not None, "the handoff held the merge lock while it read GitHub"
    assert handoff.returncode == 0, out + err
    assert (workflow.repo / ".local/workflow/merge-wanted-7").exists()


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"headRefOid": "f" * 40}, "the pull request head changed during the handoff"),
        ({"headRefName": "ticket/other"}, "the pull request head changed during the handoff"),
        ({"isDraft": True}, "only a ready, non-conflicting pull request can be handed off"),
        ({"mergeable": "CONFLICTING"}, "only a ready, non-conflicting pull request"),
        (
            {
                "statusCheckRollup": [
                    {"name": "CI result", "status": "COMPLETED", "conclusion": "FAILURE"}
                ]
            },
            "only a ready, non-conflicting pull request",
        ),
    ],
)
def test_a_pull_request_that_changed_before_the_lock_was_taken_is_refused(
    github: GitHub, change: dict[str, Any], refusal: str
) -> None:
    workflow = github.workflow
    _handing_off(github)
    github.after_view = {"7": change}

    done = _hand_off(github)

    assert done.returncode == 1 and refusal in done.stderr, done.stdout + done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert "7" not in _waiting(workflow)
    # The refusal comes from the read just before the lock: the lock is never taken.
    assert _locked_calls(workflow) == [] and _held(workflow) == []


def _red_ci(github: GitHub) -> None:
    """Its CI run failed, though GitHub still shows its checks green."""
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]["conclusion"] = "failure"
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"


@pytest.mark.parametrize(
    ("case", "refusal"),
    [
        ("draft", "only a ready, non-conflicting pull request can be handed off"),
        ("conflicting", "only a ready, non-conflicting pull request can be handed off"),
        ("failed", "only a ready, non-conflicting pull request can be handed off"),
        ("unreviewed", "the latest independent round must have passed on the current head"),
        ("sent back", "the latest independent round must have passed on the current head"),
        ("unclean", "handoff requires the clean published pull request worktree"),
        ("branch", "handoff requires the pull request branch"),
        ("pre-train checks", "pre-train checks failed"),
        ("blocked", "this pull request failed checks; fix it before rejoining the merge train"),
        ("whole suite", "a whole suite run on this pull request's tested merge failed"),
        ("prefix", "the ci run of this pull request's prefix (workflow/merge-batch-b) failed"),
    ],
)
def test_each_handoff_refusal_still_fires(github: GitHub, case: str, refusal: str) -> None:
    workflow = github.workflow
    worktree = _handing_off(github)
    folder = workflow.repo / ".local/workflow"
    pull = github.pulls["7"]
    if case == "draft":
        pull["isDraft"] = True
    elif case == "conflicting":
        pull["mergeable"] = "CONFLICTING"
    elif case == "failed":
        pull["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    elif case == "unreviewed":
        github.api[f"{_REPO}/pulls/7/reviews"] = []
    elif case == "sent back":
        github.api[f"{_REPO}/pulls/7/reviews"][0]["body"] = (
            "<!-- workflow-review round=1 result=sent-back -->\nNot yet."
        )
    elif case == "unclean":
        (worktree / "uncommitted.txt").write_text("not published\n")
    elif case == "branch":
        workflow.git("checkout", "--detach", cwd=worktree)
    elif case == "pre-train checks":
        checks = workflow.script.with_name("pre-train-checks")
        checks.write_text("#!/bin/bash\necho 'inventory is stale' >&2\nexit 1\n")
        # An earlier handoff of it waits in the line: it leaves the line, and its note goes.
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7"]}))
        (folder / "merge-wanted-7").write_text(
            json.dumps({"head": github.head, "round": 1, "branch": "ticket/seven"})
        )
    elif case == "blocked":
        # It failed in the line before, on this same head, and its run is still red.
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(
            json.dumps({"waiting": ["7"], "blocked": {"7": github.head}})
        )
        _red_ci(github)
    elif case == "whole suite":
        # The whole suite of its tested merge failed, on this same head.
        suite = {
            "member": github.head,
            "head": "a" * 40,
            "branch": "workflow/merge-batch-a",
            "workflows": ["ci"],
            "requested": ["ci"],
        }
        github.ci_dispatched("a" * 40, suite["branch"], run_id=40, conclusion="failure")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(
            json.dumps(
                {"waiting": ["7"], "blocked": {"7": github.head}, "suites": {"7": suite}}
            )
        )
    else:
        # Its prefix's CI run failed on this same head, and no re-run of it passed since.
        batch = {"head": "b" * 40, "branch": "workflow/merge-batch-b", "workflows": ["ci"]}
        github.ci_dispatched("b" * 40, batch["branch"], run_id=41, conclusion="failure")
        failure = {"pull": "20:1", "batch": batch, "workflow": "ci", "run": "41:1"}
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(
            json.dumps(
                {
                    "waiting": ["7"],
                    "blocked": {"7": github.head},
                    "batch_failed": {"7": failure},
                }
            )
        )

    done = _hand_off(github)

    assert done.returncode == 1 and refusal in done.stderr, done.stdout + done.stderr
    assert not (folder / "merge-wanted-7").exists()
    if case in ("blocked", "whole suite", "prefix"):
        # It keeps its place, passed over until a green re-run, as before.
        train = json.loads((folder / "merge-train.json").read_text())
        assert train["waiting"] == ["7"] and train["blocked"] == {"7": github.head}
    else:
        assert "7" not in _waiting(workflow)


def test_a_handoff_whose_place_the_tick_changed_meanwhile_is_judged_again(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _handing_off(github)
    _red_ci(github)
    # While the handoff judged its place, the tick blocked it for its red run.
    github.line_after_view = {"7": [None, {"waiting": ["7"], "blocked": {"7": github.head}}]}

    done = _hand_off(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "this pull request failed checks" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    train = json.loads((workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert train["waiting"] == ["7"] and train["blocked"] == {"7": github.head}
    assert _locked_calls(workflow) == [] and len(_held(workflow)) == 2


def test_a_handoff_whose_place_keeps_changing_waits(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    github.line_after_view = {
        "7": [None, *({"waiting": [], "approved": {"7": f"{n:040x}"}} for n in range(3))]
    }

    done = _hand_off(github)

    assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    assert "kept changing" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert _locked_calls(workflow) == [] and len(_held(workflow)) == 3


def test_a_newly_joined_member_waits_for_the_tick_to_read_its_run(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    github.pulls["8"] = {**deepcopy(github.pulls["7"]), "headRefOid": "8" * 40}
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    # 8 waits, green on current main; 7 joins with a higher priority.
    (folder / "merge-train.json").write_text(
        json.dumps({"waiting": ["8"], "passing": ["8"], "priorities": {"8": "low"}})
    )
    github.pulls["7"]["labels"] = [{"name": "priority: high"}]

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    train = json.loads((folder / "merge-train.json").read_text())
    assert train["waiting"] == ["8", "7"] and train["pending"] == ["7"]
    assert train["turn"] == 0


def test_a_re_run_another_call_claimed_is_not_requested_again(github: GitHub) -> None:
    workflow = github.workflow
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "pull-rerun-20-1"
    # Another call claimed this re-run a moment ago.
    marker.write_text("automatic retry requested for failed jobs of run 20 of pull request 7\n")
    before = marker.read_text()

    done = subprocess.run(
        [
            "python3",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
            "from pathlib import Path; "
            "print(g._rerun({'id': 20}, Path(sys.argv[2]), Path(sys.argv[3]), 'now', "
            "name='run 20 of pull request 7'))",
            str(workflow.script.parent),
            str(folder),
            str(marker),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "re-run of run 20 of pull request 7 is requested" in done.stdout
    assert rerun_calls(workflow) == []
    assert marker.read_text() == before


def test_a_head_pushed_after_the_last_read_is_refused_under_the_lock(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    # Another push lands right after the read just before the lock.
    github.push_after_view = {"7": [False, True]}

    done = _hand_off(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "the pull request head changed during the handoff; review it again" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert "7" not in _waiting(workflow)
    assert _locked_calls(workflow) == [] and len(_held(workflow)) == 1


def test_a_stalled_head_check_lets_the_merge_lock_go_within_seconds(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    real = shutil.which("git", path=workflow.env["PATH"])
    # GitHub stops answering git after the checks: its branch's head cannot be read.
    workflow.fake(
        "git",
        "#!/bin/bash\n"
        f'if [ -e "{workflow.state}/stall" ] && [ "$1" = ls-remote ] '
        '&& [[ " $* " == *" refs/heads/ticket/seven "* ]]; then exec /bin/sleep 600; fi\n'
        f'exec "{real}" "$@"\n',
    )
    _after_the_checks(workflow, f": > '{workflow.state}/stall'")

    done = _hand_off(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "could not confirm the head; try again" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert "7" not in _waiting(workflow)
    # It waited out the check's own time limit, and no more.
    held = _held(workflow)
    assert len(held) == 1 and 9 < held[0] < 15, held


def _budget(workflow: Workflow) -> Path:
    folder = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_a_budget_spent_after_the_checks_does_not_hold_the_merge_lock(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    # The line's other work spends GitHub's budget while the handoff runs its checks; it resets
    # 10 s later.
    budget = _budget(workflow)
    _after_the_checks(
        workflow,
        f"""echo '{{"core": {{"remaining": 0, "reset": '$(( $(date +%s) + 10 ))'}}}}' """
        f"""> '{budget}/budget.json'""",
    )
    started = time.monotonic()

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    # It waited for the budget, before the lock.
    assert "GitHub budget low" in done.stderr and time.monotonic() - started > 8
    held = _held(workflow)
    assert len(held) == 1 and held[0] < 5, held


def test_busy_gh_call_slots_do_not_hold_the_merge_lock(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    budget = _budget(workflow)
    holder = workflow.state / "slot-holder.py"
    holder.write_text(
        "import fcntl, sys, time\n"
        "from pathlib import Path\n"
        "folder = Path(sys.argv[1])\n"
        "locks = [(folder / f'call-{n}.lock').open('a') for n in range(2)]\n"
        "for lock in locks:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "Path(sys.argv[2]).touch()\n"
        "time.sleep(10)\n"
    )
    ready = workflow.state / "slots-taken"
    # Two other calls, the tick reading the line's members, hold both gh slots for 10 s.
    _after_the_checks(
        workflow,
        f"python3 '{holder}' '{budget}' '{ready}' </dev/null >/dev/null 2>&1 &\n"
        f"while [ ! -e '{ready}' ]; do /bin/sleep 0.05; done",
    )
    started = time.monotonic()

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    # It waited for a slot, before the lock.
    assert time.monotonic() - started > 8
    held = _held(workflow)
    assert len(held) == 1 and held[0] < 5, held


@pytest.mark.parametrize("meanwhile", ["nothing", "draft"])
def test_a_read_made_old_by_a_long_wait_for_the_lock_is_made_again(
    github: GitHub, meanwhile: str
) -> None:
    workflow = github.workflow
    _handing_off(github)
    holder = workflow.state / "lock-holder.py"
    # Another gate holds the merge lock for 7 s, from just after the handoff's checks. Once the
    # handoff has read its pull request before the lock, it may turn draft on GitHub.
    holder.write_text(
        "import fcntl, json, sys, time\n"
        "from pathlib import Path\n"
        "state, lock, draft = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == 'draft'\n"
        "held = lock.open('w')\n"
        "fcntl.flock(held, fcntl.LOCK_EX)\n"
        "(state / 'lock-taken').touch()\n"
        "started = time.monotonic()\n"
        "while draft and (state / 'calls.log').read_text().count('gh pr view 7 ') < 2:\n"
        "    time.sleep(0.05)\n"
        "if draft:\n"
        "    data = json.loads((state / 'github.json').read_text())\n"
        "    data['pulls']['7']['isDraft'] = True\n"
        "    (state / 'github.json').write_text(json.dumps(data))\n"
        "time.sleep(max(0, 7 - (time.monotonic() - started)))\n"
    )
    lock = workflow.repo / ".local/workflow/merge.lock"
    _after_the_checks(
        workflow,
        f"python3 '{holder}' '{workflow.state}' '{lock}' '{meanwhile}' "
        "</dev/null >/dev/null 2>&1 &\n"
        f"while [ ! -e '{workflow.state}/lock-taken' ]; do /bin/sleep 0.05; done",
    )

    done = _hand_off(github)

    # Its pull request is read again once the lock is free, and judged by that read.
    assert len(workflow.calls("gh pr view 7 ")) == 3
    if meanwhile == "draft":
        assert done.returncode == 1, done.stdout + done.stderr
        assert "only a ready, non-conflicting pull request can be handed off" in done.stderr
        assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
        assert "7" not in _waiting(workflow)
    else:
        assert done.returncode == 0, done.stdout + done.stderr
        assert (workflow.repo / ".local/workflow/merge-wanted-7").exists()
        assert _waiting(workflow) == ["7"]
    assert all(held < 5 for held in _held(workflow))
