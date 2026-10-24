"""The scripts run in a repository that holds only them, and use what a repository adds beside
them: the gate's run-proof helper, and the runner's settings and `gh` installer. The commands a
repository names in `shipline.json` are tried in `test_commands.py`."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline

_HELPERS = ("run_proof",)
# What a scan that could not reach an advisory service says, as an outage would read it.
_UNREACHED = "npm's advisory service could not be reached"


def _gate(workflow: Workflow, code: str) -> subprocess.CompletedProcess[str]:
    """Runs `code` with the repository's own gate imported as `g`; it prints what it found."""
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g\n"
            + textwrap.dedent(code),
            str(workflow.script.parent),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _failing_hook(workflow: Workflow, lines: list[str], *, stall: bool = False) -> None:
    """A pre-push hook that prints `lines` and refuses, or with `stall` sleeps past any deadline.
    It lives in `workflow.bin`: CI runs pytest in /dev/shm, where git finds a hook it cannot run
    and skips it."""
    hooks = workflow.bin / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    printed = "".join(f"echo {json.dumps(line)}\n" for line in lines)
    end = "exec /bin/sleep 30" if stall else "exit 1"
    hook.write_text(f'#!/bin/bash\necho pre-push >> "{workflow.state}/calls.log"\n{printed}{end}\n')
    hook.chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))


def _push(workflow: Workflow, deadline: int | None = None) -> subprocess.CompletedProcess[str]:
    """The gate pushes a new branch; it prints what came back, or the wait it raised. A
    `deadline` is the gate's own for this test, shorter than its commands'."""
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "feat: pushed by the gate")
    return _gate(
        workflow,
        (f"g.COMMAND_TIMEOUT = {deadline}\n" if deadline else "")
        + textwrap.dedent(
            """
        import json
        try:
            pushed = g.push("origin", "HEAD:refs/heads/ticket/pushed")
        except g.Waiting as error:
            print(json.dumps({"waiting": type(error).__name__, "reason": str(error)}))
        else:
            print(json.dumps({"returncode": pushed.returncode, "refusal": pushed.refusal}))
        """
        ),
    )


def _helpers_beside_the_scripts(workflow: Workflow) -> None:
    """The small helper of a repository that holds it."""
    scripts = workflow.script.parent
    (scripts / "run_proof.py").write_text(
        textwrap.dedent(
            """\
            RECEIPT = "Workflow-Proof: "
            WHOLE = "Workflow-Whole-Suites: true"
            def passed_jobs(parts):
                return {job["name"] for job in parts if job.get("conclusion") == "success"}
            """
        )
    )


def test_the_gate_holds_none_of_the_helpers_a_repository_may_add() -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "workflow"

    assert [name for name in _HELPERS if (scripts / f"{name}.py").exists()] == []


def test_the_gate_starts_without_the_helpers(workflow: Workflow) -> None:
    done = _gate(workflow, 'print("started")')

    assert done.stdout == "started\n", done.stderr


def test_a_failed_push_is_refused_with_its_last_lines(workflow: Workflow) -> None:
    lines = [f"step {number}" for number in range(1, 16)] + [_UNREACHED]
    _failing_hook(workflow, lines)

    done = _push(workflow)

    assert done.returncode == 0, done.stderr
    pushed = json.loads(done.stdout)
    assert pushed["returncode"] != 0
    # Ten lines, the hook's last and then git's own, which says the push failed.
    refused = pushed["refusal"].splitlines()
    assert len(refused) == 10, refused
    assert _UNREACHED in refused and "step 1" not in refused
    assert "failed to push" in refused[-1]


def test_a_push_that_times_out_is_refused_with_its_last_twenty_lines(workflow: Workflow) -> None:
    lines = [f"step {number}" for number in range(1, 26)] + [_UNREACHED]
    _failing_hook(workflow, lines, stall=True)

    done = _push(workflow, deadline=5)

    assert done.returncode == 0, done.stderr
    pushed = json.loads(done.stdout)
    assert pushed["returncode"] != 0
    # The deadline on the first line, then the hook's last twenty, which git never followed.
    assert pushed["refusal"].splitlines() == [
        "git push timed out after 5s",
        *lines[-20:],
    ], pushed["refusal"]


def test_a_failed_push_is_never_read_as_an_outage_of_an_advisory_service(
    workflow: Workflow,
) -> None:
    _failing_hook(workflow, [_UNREACHED])

    done = _push(workflow)

    assert "waiting" not in json.loads(done.stdout), done.stdout
    assert not (workflow.repo / ".local/workflow/advisory-service-down").exists()


def test_nothing_is_installed_before_a_push(workflow: Workflow) -> None:
    for tool in ("npm", "pnpm", "uv", "pip"):
        workflow.fake(tool, f'#!/bin/bash\necho "{tool} $*" >> "@STATE@/calls.log"\n')

    done = _push(workflow)

    assert json.loads(done.stdout) == {"returncode": 0, "refusal": ""}, done.stderr
    assert workflow.calls() == []


def test_without_a_proof_helper_the_proof_is_shipline_s_own(workflow: Workflow) -> None:
    """ci_proof.py's, which its example CI reads: a job counts as passed only where it passed
    itself, a job in parts once every part did."""
    done = _gate(
        workflow,
        """
        parts = [
            {"name": "playwright", "conclusion": "success"},
            {"name": "E2E: lint", "conclusion": "skipped"},
            {"name": "Docker images: browser tests (1 of 2)", "conclusion": "success"},
        ]
        run = {"id": 10}
        print(sorted(g.passed_jobs(parts)))
        print(g.squash_proof("base", "tree", {"e2e": (run, parts)}))
        """,
    )

    assert done.stdout.splitlines() == [
        "['playwright']",
        'Workflow-Proof: {"base":"base","tree":"tree","sources":'
        '[{"workflow":"e2e","run":10,"jobs":["playwright"]}]}',
    ], done.stderr


@pytest.mark.parametrize("helper", _HELPERS)
def test_a_helper_that_cannot_start_stops_the_gate(workflow: Workflow, helper: str) -> None:
    """Only a helper the repository does not hold is left out; a broken one is never so."""
    (workflow.script.parent / f"{helper}.py").write_text("import example_app_missing_package\n")

    done = _gate(workflow, 'print("started")')

    assert done.returncode == 1
    assert "No module named 'example_app_missing_package'" in done.stderr


def test_a_squash_carries_the_commits_then_the_proof(workflow: Workflow) -> None:
    done = _gate(
        workflow,
        """
        g.commit_list = lambda number: "* feat: seven"
        with g.squash_body("7", g.squash_proof("base", "tree", {})) as body:
            print(open(body).read(), end="")
        """,
    )

    assert done.stdout == (
        '* feat: seven\n\nWorkflow-Proof: {"base":"base","tree":"tree","sources":[]}\n'
    ), done.stderr


def test_a_repository_s_proof_is_read_and_written_when_it_holds_it(workflow: Workflow) -> None:
    _helpers_beside_the_scripts(workflow)

    done = _gate(
        workflow,
        """
        parts = [{"name": "playwright", "conclusion": "success"}]
        print(g.squash_proof("base", "tree", {"e2e": ({"id": 10}, parts)}))
        """,
    )

    assert done.stdout.startswith("Workflow-Proof: {"), done.stderr
    assert json.loads(done.stdout.removeprefix("Workflow-Proof: "))["sources"] == [
        {"workflow": "e2e", "run": 10, "jobs": ["playwright"]}
    ]


def _settings(workflow: Workflow) -> Path:
    return workflow.repo / ".github/runner/settings.sh"


def _origin(workflow: Workflow, url: str) -> None:
    """The main checkout's `origin` says `url`, and git still fetches from the test's own."""
    local = workflow.git("remote", "get-url", "origin")
    workflow.git("remote", "set-url", "origin", url)
    workflow.git("config", f"url.{local}.insteadOf", url)


def _reviews_asked(workflow: Workflow) -> list[str]:
    return [call for call in workflow.calls("gh api --paginate") if call.endswith("/reviews")]


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example-org/other-app.git",
        "https://github.com/example-org/other-app",
        "https://github.com/example-org/other-app.git/",
        # `git clone https://name@github.com/...` keeps the user name in the URL.
        "https://example-user@github.com/example-org/other-app.git",
        "git@github.com:example-org/other-app.git",
        "ssh://git@github.com/example-org/other-app.git",
    ],
)
def test_the_tick_takes_the_repository_from_origin_without_the_runner_settings(
    workflow: Workflow, url: str
) -> None:
    _settings(workflow).unlink()
    _origin(workflow, url)
    workflow.add_pull_request(7, draft=False)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    asked = _reviews_asked(workflow)
    assert asked and all("repos/example-org/other-app/pulls/" in call for call in asked), asked


def test_the_tick_takes_the_repository_from_the_runner_settings_when_they_are_there(
    workflow: Workflow,
) -> None:
    _settings(workflow).write_text("repository=example-org/settings-app\n")
    _origin(workflow, "https://github.com/example-org/other-app.git")
    workflow.add_pull_request(7, draft=False)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    asked = _reviews_asked(workflow)
    assert asked and all("repos/example-org/settings-app/pulls/" in call for call in asked), asked


def test_the_tick_stops_when_it_cannot_tell_the_repository(workflow: Workflow) -> None:
    _settings(workflow).unlink()

    done = workflow.run_script("tick")

    assert done.returncode == 1
    assert "cannot tell which repository" in done.stderr
    assert workflow.calls("gh") == []


def _codex_gh(workflow: Workflow, *args: str) -> subprocess.CompletedProcess[str]:
    """The Codex worker's `gh`, as worker 7 calls it, in front of a `gh` that allows all."""
    workflow.fake("gh", '#!/bin/bash\necho "allowed $*"\n')
    return subprocess.run(
        ["bash", str(workflow.script.parent / "codex-bin" / "gh"), *args],
        cwd=workflow.repo,
        env={
            **workflow.env,
            "WORKFLOW_SESSION": "ticket-7",
            "WORKFLOW_GH": str(workflow.bin / "gh"),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _own_body(repository: str) -> list[str]:
    return ["api", "-X", "PATCH", f"repos/{repository}/pulls/7", "-F", "body=@/tmp/task.md"]


def test_the_codex_gh_takes_the_repository_from_origin_without_the_runner_settings(
    workflow: Workflow,
) -> None:
    _settings(workflow).unlink()
    _origin(workflow, "git@github.com:example-org/other-app.git")

    own = _codex_gh(workflow, *_own_body("example-org/other-app"))
    other = _codex_gh(workflow, *_own_body("example-org/example-app"))

    assert own.returncode == 0, own.stderr
    assert own.stdout.strip() == "allowed " + " ".join(_own_body("example-org/other-app"))
    assert other.returncode == 1 and "Codex worker" in other.stderr


def test_the_codex_gh_reads_a_user_name_in_origin(workflow: Workflow) -> None:
    _settings(workflow).unlink()
    _origin(workflow, "https://example-user@github.com/example-org/other-app.git")

    own = _codex_gh(workflow, *_own_body("example-org/other-app"))

    assert own.returncode == 0, own.stderr


def test_the_codex_gh_takes_the_repository_from_the_runner_settings_when_they_are_there(
    workflow: Workflow,
) -> None:
    _settings(workflow).write_text("repository=example-org/settings-app\n")
    _origin(workflow, "git@github.com:example-org/other-app.git")

    own = _codex_gh(workflow, *_own_body("example-org/settings-app"))
    origin = _codex_gh(workflow, *_own_body("example-org/other-app"))

    assert own.returncode == 0, own.stderr
    assert origin.returncode == 1 and "Codex worker" in origin.stderr


def test_the_codex_gh_without_a_repository_allows_reads_and_refuses_the_body(
    workflow: Workflow,
) -> None:
    _settings(workflow).unlink()

    read = _codex_gh(workflow, "pr", "view", "7")
    body = _codex_gh(workflow, *_own_body("example-org/example-app"))

    assert read.returncode == 0, read.stderr
    assert body.returncode == 1 and "Codex worker" in body.stderr


def _installer(workflow: Workflow) -> Path:
    return workflow.repo / ".github/runner/install-github-budget.sh"


def test_install_timer_runs_without_the_gh_installer(workflow: Workflow) -> None:
    # `install-timer` runs from a Shipline checkout of its own.
    install_shipline(workflow)
    assert not _installer(workflow).exists()

    done = workflow.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("systemctl") == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now workflow-tick.timer",
    ]


def test_install_timer_runs_the_gh_installer_first_when_it_is_there(workflow: Workflow) -> None:
    # `install-timer` runs from a Shipline checkout of its own.
    install_shipline(workflow)
    _installer(workflow).write_text(f'echo "install-github-budget $*" >> "{workflow.state}/calls.log"\n')

    done = workflow.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    assert workflow.calls()[0] == "install-github-budget "


def test_install_timer_stops_when_the_gh_installer_fails(workflow: Workflow) -> None:
    # `install-timer` runs from a Shipline checkout of its own.
    install_shipline(workflow)
    _installer(workflow).write_text("exit 3\n")

    done = workflow.run_script("install-timer")

    assert done.returncode == 3
    assert workflow.calls("systemctl") == []
