"""Breaking tests for pull request 69: conflict resolutions on Codex with independent reviewers.

Each test replays a daily use with neutral names: `shipline.json` sets
`"conflict_review": {"program": "codex"}` and nothing else, one large merge to main leaves
several handed-off pull requests in conflict at once, and each gets a Codex resolver whose
panel must be reviewed by Claude and Cursor.
"""

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from workflow.test_conflict_review import (  # pyright: ignore[reportPrivateUsage]
    _FILE,
    _PREFIX,
    _SUFFIX,
    _conflict,
    _panel_conflict,
    _program,
    _record_handback,
    _resolve,
    _write,
)
from workflow.test_merge_driver import _folder, _publish  # pyright: ignore[reportPrivateUsage]
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow, instruction, session_arguments

handed = handed_fixture
github = github_fixture

_PULLS = (7, 8, 9, 10)


def _conflicts_after_one_merge(workflow: Workflow) -> dict[int, str]:
    """Four ready pull requests change one line; one merge to main changes it too."""
    heads = {}
    for number in _PULLS:
        branch = workflow.add_worktree(number, draft=False)
        _write(branch, _FILE, _PREFIX + f"result = 'branch {number}'\n" + _SUFFIX)
        workflow.git("add", ".", cwd=branch)
        workflow.git("commit", "--quiet", "-m", f"feat: {number}", cwd=branch)
        workflow.git("push", "--quiet", "origin", "HEAD", cwd=branch)
        heads[number] = workflow.git("rev-parse", "HEAD", cwd=branch)
        workflow.set_pull_request(number, "OPEN", heads[number])
    _write(workflow.repo, _FILE, _PREFIX + "result = 'one large merge'\n" + _SUFFIX)
    workflow.git("add", ".")
    workflow.git("commit", "--quiet", "-m", "feat: one large merge")
    workflow.git("push", "--quiet", "origin", "main")
    main = workflow.git("rev-parse", "HEAD")
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    for number, head in heads.items():
        merged = subprocess.run(
            ["git", "merge", "--no-ff", "--no-commit", main],
            cwd=workflow.worktree(number), env=workflow.env,
            text=True, capture_output=True, check=False,
        )
        assert merged.returncode == 1, merged.stdout + merged.stderr
        (folder / f"review-{number}.conflict.json").write_text(
            json.dumps({"head": head, "main": main, "files": [_FILE]})
        )
        (folder / f"merge-review-{number}").write_text(
            json.dumps({"head": head, "event": "conflict-event",
                        "reason": "non-mechanical conflict in " + _FILE})
        )
    _program(workflow)
    return heads


def test_four_conflicts_after_one_merge_start_four_codex_resolvers(workflow: Workflow) -> None:
    """The daily case: each conflict gets its own Codex pair and sentence, none on Claude."""
    heads = _conflicts_after_one_merge(workflow)
    for number in _PULLS:
        done = workflow.run("start", "review", str(number))
        assert done.returncode == 0, done.stdout + done.stderr
    for number, head in heads.items():
        assert workflow.is_running(f"codex-{number}") and workflow.is_running(f"review-{number}")
        args = shlex.split(session_arguments(workflow.state, f"codex-{number}")[-1])
        assert args[-1] == instruction(
            f"Review conflict resolution for pull request {number} extending passed head "
            f"{head} in files {_FILE}"
        )
        assert "--model" not in args, "program alone uses Codex's configured model"
        assert "model_reasoning_effort=high" in args
        config = (workflow.notes(number) / f"review-{number}.codex-config.json").read_text()
        assert f"review-{number}" in config
        assert (workflow.notes(number) / f"review-{number}.codex").exists()
        record = json.loads((_folder(workflow) / f"review-{number}.conflict-reviewer").read_text())
        assert record["program"] == "codex"
    # A quota stop on one resolver leaves the other three as they are.
    workflow.screen("■ You've hit your usage limit. Try again at 4:00 PM.\n› \n", "codex-9")
    listed = workflow.run("list")
    assert listed.returncode == 0, listed.stderr
    states = {line.split()[0]: line.split()[1] for line in listed.stdout.splitlines() if line.strip()}
    assert states["review-9"] == "limit"
    assert all(states[f"review-{number}"] != "limit" for number in (7, 8, 10)), listed.stdout
    # Stopping one pair stops only that pair.
    assert workflow.run("stop", "review-8").returncode == 0
    assert not workflow.is_running("codex-8") and not workflow.is_running("review-8")
    for number in (7, 9, 10):
        assert workflow.is_running(f"codex-{number}") and workflow.is_running(f"review-{number}")


def test_two_starts_of_one_conflict_at_once_make_one_codex_pair(workflow: Workflow) -> None:
    """Two ticks (or a tick and the orchestrator) start the same conflict reviewer together."""
    _conflicts_after_one_merge(workflow)
    script = workflow.script
    calls = [
        subprocess.Popen(
            ["bash", str(script), "start", "review", "7"], cwd=workflow.repo, env=workflow.env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [(call.wait(timeout=120), *call.communicate()) for call in calls]
    assert sorted(code == 0 for code, _, _ in results) == [False, True], results
    assert workflow.is_running("codex-7") and workflow.is_running("review-7")
    launches = [call for call in workflow.calls("tmux codex-7 new-session")]
    assert len(launches) == 1, launches


def test_program_alone_survives_the_exported_configuration(workflow: Workflow) -> None:
    """Scripts re-read the configuration from the exported JSON; Codex stays the resolver."""
    _conflicts_after_one_merge(workflow)
    shell = subprocess.run(
        ["python3", "-B", str(workflow.script.parent / "workflow_config.py"), "shell"],
        cwd=workflow.repo, env=workflow.env, text=True, capture_output=True, check=False,
    )
    assert shell.returncode == 0, shell.stderr
    exported = subprocess.run(
        ["bash", "-c", shell.stdout + '\nprintf %s "$SHIPLINE_CONFIG_JSON"'],
        cwd=workflow.repo, env=workflow.env, text=True, capture_output=True, check=False,
    )
    assert exported.returncode == 0, exported.stderr
    conflict = json.loads(exported.stdout)["conflict_review"]
    assert conflict["program"] == "codex"
    assert "model" not in conflict and "max_changed_lines" not in conflict
    assert workflow.run("start", "review", "7").returncode == 0
    program = subprocess.run(
        ["bash", "-c", shell.stdout + "\npython3 -B \"$1\" program \"$2\" 7", "bash",
         str(workflow.script.parent / "conflict_review.py"), str(_folder(workflow))],
        cwd=workflow.worktree(7), env=workflow.env, text=True, capture_output=True, check=False,
    )
    assert program.returncode == 0, program.stderr
    assert program.stdout.split() == ["codex"]


def test_codex_resolution_hands_off_with_program_alone(handed: GitHub) -> None:
    """The tick starts Codex; its conflict round on the resolved head is accepted by the gate."""
    workflow = handed.workflow
    head, main = _conflict(workflow)
    _program(workflow)
    handed.head, handed.base = head, main
    handed.pulls["7"]["headRefOid"] = head
    handed.pulls["7"]["mergeable"] = "CONFLICTING"
    handed.main()
    handed.ci(main, head, tested_base=workflow.git("merge-base", main, head))
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    note = _folder(workflow) / "merge-wanted-7"
    note.write_text(json.dumps({**json.loads(note.read_text()), "head": head}))
    _publish(handed, "passed", head)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.is_running("codex-7")
    resolved = _resolve(workflow)
    handed.pulls["7"]["headRefOid"] = resolved
    handed.pulls["7"]["mergeable"] = "MERGEABLE"
    handed.api["repos/example-org/example-app/pulls/7/reviews"] = [
        {"body": "<!-- workflow-review round=1 result=passed -->", "commit_id": head},
        {
            "body": (
                "<!-- workflow-review round=2 result=passed -->\n"
                f"<!-- workflow-conflict extends={head} -->"
            ),
            "commit_id": resolved,
        },
    ]
    handed.save()
    (_folder(workflow) / "merge-wanted-7").unlink(missing_ok=True)
    done = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))
    assert done.returncode == 0, done.stdout + done.stderr
    assert (_folder(workflow) / "merge-wanted-7").exists()


def test_late_github_head_after_push_still_returns_the_fallback_to_claude(
    workflow: Workflow,
) -> None:
    """Codex pushes and runs the panel at once, while GitHub still lists the old head."""
    head, _ = _panel_conflict(workflow)
    _program(workflow)
    _record_handback(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    resolved = _resolve(workflow)
    workflow.set_pull_request(7, "OPEN", head)  # GitHub is late
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 2, done.stdout + done.stderr
    workflow.set_pull_request(7, "OPEN", resolved)  # GitHub caught up
    assert workflow.run("stop", "review-7").returncode == 0
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    # The rules: after a fallback the tick starts the usual Claude reviewer on the pushed head.
    assert not workflow.is_running("codex-7"), "a second Codex resolver started on the same head"


class _Recorder(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        type(self).bodies.append(self.rfile.read(length))
        self.send_response(500)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_: object) -> None:
        pass


@contextmanager
def _model_api() -> Generator[tuple[str, list[bytes]]]:
    bodies: list[bytes] = []
    handler = type("Handler", (_Recorder,), {"bodies": bodies})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", bodies
    finally:
        server.shutdown()


def test_codex_panel_claude_reviewer_receives_its_prompt(workflow: Workflow, tmp_path: Path) -> None:
    """The panel's exact Claude command line, replayed on the real Claude CLI: the review
    prompt must reach the model. A local stand-in API records the request; no sign-in."""
    real = shutil.which("claude", path=os.environ.get("PATH", ""))
    if real is None or Path(real).parent == workflow.bin:
        pytest.skip("the real Claude CLI is not installed")
    _panel_conflict(workflow)
    _program(workflow)
    _record_handback(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    resolved = _resolve(workflow)
    workflow.set_pull_request(7, "OPEN", resolved)
    workflow.fake("agent", '#!/bin/bash\necho "Cursor report"\n')
    workflow.fake("claude", "#!/bin/bash\nprintf '%s\\0' \"$@\" > \"@STATE@/claude-argv\"\necho claude report\n")
    done = workflow.run_script("review-panel", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    argv = (workflow.state / "claude-argv").read_text().split("\0")[:-1]
    prompt = argv[-1]
    assert prompt.strip(), argv
    config = tmp_path / "replay-mcp.json"
    config.write_text('{"mcpServers":{}}\n')
    argv[argv.index("--mcp-config") + 1] = str(config)
    marker = json.dumps(prompt.splitlines()[0][:40])[1:-1].encode()
    # Control: the same words with each option's value attached reach the stand-in API.
    attached = argv[:argv.index("--mcp-config")] + [
        f"--mcp-config={config}", "--tools=Read,Grep,Glob", prompt
    ]
    assert attached[-4:-3] == ["--strict-mcp-config"] and argv[-3:-1] == ["--tools", "Read,Grep,Glob"]
    reached, output = _replay(real, attached, tmp_path / "control", marker)
    assert reached, f"the stand-in API is not reachable here; claude said: {output}"
    reached, output = _replay(real, argv, tmp_path / "panel", marker)
    assert reached, f"the review prompt never reached the model; claude said: {output}"


def _replay(real: str, argv: list[str], home: Path, marker: bytes) -> tuple[bool, str]:
    home.mkdir()
    with _model_api() as (url, bodies):
        process = subprocess.Popen(
            [real, *argv],
            cwd=home, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={"PATH": os.environ["PATH"], "HOME": str(home), "ANTHROPIC_API_KEY": "sk-example",
                 "ANTHROPIC_BASE_URL": url},
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and process.poll() is None and not bodies:
            time.sleep(0.2)
        if process.poll() is None:
            process.kill()
        output = process.communicate()[0].decode(errors="replace").strip()[-300:]
        return any(marker in body for body in bodies), output


@pytest.mark.parametrize("inside", [False, True], ids=["control-outside-codex", "inside-codex"])
def test_codex_resolver_runs_its_panel_inside_its_own_sandbox(
    workflow: Workflow, tmp_path: Path, inside: bool
) -> None:
    """The resolver runs `review-panel N` itself, inside Codex's workspace-write sandbox with
    the grants `session` gives it. The panel's read-only sandbox must still start there, or
    neither Claude nor Cursor reviews the resolution. Real Codex and bwrap, stand-in reviewers."""
    found = {tool: shutil.which(tool, path=os.environ.get("PATH", "")) for tool in ("codex", "bwrap")}
    if any(path is None or Path(path).parent == workflow.bin for path in found.values()):
        pytest.skip("the real Codex CLI or bwrap is not installed")
    _panel_conflict(workflow)
    _program(workflow)
    _record_handback(workflow)
    assert workflow.run("start", "review", "7").returncode == 0
    args = shlex.split(session_arguments(workflow.state, "codex-7")[-1])
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    grants = [args[index + 1] for index, word in enumerate(args) if word == "--add-dir"]
    resolved = _resolve(workflow)
    workflow.set_pull_request(7, "OPEN", resolved)
    (workflow.bin / "bwrap").unlink()  # the real read-only sandbox, as installed
    workflow.fake("agent", '#!/bin/bash\necho "Cursor report"\n')
    workflow.fake("claude", '#!/bin/bash\necho "Claude report"\n')
    home = tmp_path / "codex-home"
    home.mkdir()
    panel = ["bash", str(workflow.script.with_name("review-panel")), "7"]
    sandbox = [
        str(found["codex"]), "sandbox",
        "-c", 'sandbox_mode="workspace-write"',
        "-c", "sandbox_workspace_write.network_access=true",
        "-c", f"sandbox_workspace_write.writable_roots={json.dumps(grants)}",
        "--",
    ]
    done = subprocess.run(
        [*(sandbox if inside else []), *panel],
        cwd=workflow.worktree(7), env={**workflow.env, "CODEX_HOME": str(home)},
        text=True, capture_output=True, check=False, timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    reports = _folder(workflow) / "reviews/7" / resolved
    claude, cursor = ((reports / name).read_text() for name in ("claude.md", "cursor.md"))
    assert "Claude report" in claude and "Cursor report" in cursor, claude + cursor
