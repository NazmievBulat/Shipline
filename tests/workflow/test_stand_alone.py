"""The scripts run in a repository that holds only them, and use what a repository adds beside
them: the gate's advisory-service, pushing-checkout and run-proof helpers, and the runner's
settings and `gh` installer."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from workflow_harness import Workflow

_HELPERS = ("advisory_service", "pushing_checkout", "run_proof")
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
    """Small helpers of a repository that holds them; each writes down that it was asked."""
    scripts = workflow.script.parent
    calls = workflow.state / "calls.log"
    (scripts / "advisory_service.py").write_text(
        textwrap.dedent(
            f"""\
            ANNOTATION = "Advisory service unreachable"
            def unreachable(output):
                return "npm" if {_UNREACHED!r} in output else None
            def note_down(folder, service):
                (folder / "advisory-service-down").write_text(service)
            def note_up(folder):
                (folder / "advisory-service-down").unlink(missing_ok=True)
            def down(folder):
                return None
            def answers(service):
                return False
            """
        )
    )
    (scripts / "pushing_checkout.py").write_text(
        textwrap.dedent(
            f"""\
            from pathlib import Path
            def top(where):
                return where or Path.cwd()
            def install_if_changed(checkout):
                with open({str(calls)!r}, "a") as log:
                    log.write("install " + str(checkout) + "\\n")
                return ""
            def refusal(output, checkout):
                with open({str(calls)!r}, "a") as log:
                    log.write("refusal " + output.replace("\\n", " | ") + "\\n")
                return "the hook's step failed"
            """
        )
    )
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


@pytest.mark.parametrize("helper", [*_HELPERS, "mechanical_conflicts"])
def test_a_helper_that_cannot_start_stops_the_gate(workflow: Workflow, helper: str) -> None:
    """Only a helper the repository does not hold is left out; a broken one is never so."""
    (workflow.script.parent / f"{helper}.py").write_text("import example_app_missing_package\n")

    done = _gate(workflow, "g.mechanical_conflicts()")

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


def test_a_repository_s_helpers_are_used_when_it_holds_them(workflow: Workflow) -> None:
    _helpers_beside_the_scripts(workflow)
    _failing_hook(workflow, [_UNREACHED])

    done = _push(workflow)

    assert json.loads(done.stdout)["waiting"] == "ServiceWaiting", done.stdout + done.stderr
    assert (workflow.repo / ".local/workflow/advisory-service-down").read_text() == "npm"
    assert workflow.calls("install ") == [f"install {workflow.repo}"]


def test_a_repository_s_refusal_of_a_failed_push_is_used_when_it_holds_it(
    workflow: Workflow,
) -> None:
    _helpers_beside_the_scripts(workflow)
    _failing_hook(workflow, ["secret found in config.py"])

    done = _push(workflow)

    assert json.loads(done.stdout)["refusal"] == "the hook's step failed", done.stderr


def test_a_repository_s_refusal_of_a_timed_out_push_follows_the_deadline(
    workflow: Workflow,
) -> None:
    _helpers_beside_the_scripts(workflow)
    _failing_hook(workflow, ["$ sh scripts/slow-check.sh", _UNREACHED], stall=True)

    done = _push(workflow, deadline=5)

    # The output read before the stop is worded by the repository; no outage is noted.
    pushed = json.loads(done.stdout)
    assert pushed["refusal"] == "git push timed out after 5s\nthe hook's step failed", done.stderr
    assert workflow.calls("refusal ") == [
        f"refusal $ sh scripts/slow-check.sh | {_UNREACHED} | "
    ]
    assert not (workflow.repo / ".local/workflow/advisory-service-down").exists()


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
    assert not _installer(workflow).exists()

    done = workflow.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("systemctl") == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now workflow-tick.timer",
    ]


def test_install_timer_runs_the_gh_installer_first_when_it_is_there(workflow: Workflow) -> None:
    _installer(workflow).write_text(f'echo "install-github-budget $*" >> "{workflow.state}/calls.log"\n')

    done = workflow.run_script("install-timer")

    assert done.returncode == 0, done.stderr
    assert workflow.calls()[0] == "install-github-budget "


def test_install_timer_stops_when_the_gh_installer_fails(workflow: Workflow) -> None:
    _installer(workflow).write_text("exit 3\n")

    done = workflow.run_script("install-timer")

    assert done.returncode == 3
    assert workflow.calls("systemctl") == []


def _pre_train_checks(workflow: Workflow) -> subprocess.CompletedProcess[str]:
    """The gate's checks before pull request 7 joins the train; it prints how they ended."""
    return _gate(
        workflow,
        """
        from pathlib import Path
        folder = Path(".local/workflow")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            g.pre_train_checks("7", folder)
        except g.Refused as error:
            print("refused:", error)
        else:
            print("passed")
        """,
    )


def test_a_repository_without_pre_train_checks_has_them_passed(workflow: Workflow) -> None:
    (workflow.script.parent / "pre-train-checks").unlink()

    done = _pre_train_checks(workflow)

    assert done.stdout == "passed\n", done.stderr
    assert done.stderr == ""


def test_a_repository_s_pre_train_checks_are_run_when_it_holds_them(workflow: Workflow) -> None:
    (workflow.script.parent / "pre-train-checks").write_text(
        f'echo "pre-train-checks" >> "{workflow.state}/calls.log"\necho "a check failed"\nexit 1\n'
    )

    done = _pre_train_checks(workflow)

    assert workflow.calls() == ["pre-train-checks"]
    assert done.stdout == "a check failed\nrefused: pre-train checks failed; " \
        "fix the inventory before joining the merge train\n", done.stderr


def test_without_conflict_rules_no_migration_is_taken(workflow: Workflow) -> None:
    done = _gate(workflow, 'print(g.mechanical_conflicts(), g._taken_migrations("HEAD", "HEAD"))')

    assert done.stdout == "None []\n", done.stderr


def test_a_repository_s_conflict_rules_are_used_when_it_holds_them(workflow: Workflow) -> None:
    (workflow.script.parent / "mechanical_conflicts.py").write_text(
        textwrap.dedent(
            """\
            class MechanicalConflict(Exception):
                pass
            def taken_migrations(head, base):
                return ["migrations/0002_taken.py"]
            """
        )
    )

    done = _gate(workflow, 'print(g._taken_migrations("HEAD", "HEAD"))')

    assert done.stdout == "['migrations/0002_taken.py']\n", done.stderr


def _an_app_change(workflow: Workflow) -> None:
    (workflow.repo / "app.txt").write_text("changed\n")
    workflow.git("add", "app.txt")
    workflow.git("commit", "--quiet", "-m", "feat: an app change")


def _browser_needed(workflow: Workflow) -> subprocess.CompletedProcess[str]:
    return _gate(workflow, 'print("needed" if g.browser_needed("HEAD~1", "HEAD") else "none")')


def test_without_an_e2e_planner_no_change_needs_browser_tests(workflow: Workflow) -> None:
    (workflow.script.parents[1] / "e2e-plan.py").unlink()
    _an_app_change(workflow)

    done = _browser_needed(workflow)

    assert done.stdout == "none\n", done.stderr


def test_a_repository_s_e2e_planner_decides_when_it_holds_one(workflow: Workflow) -> None:
    _an_app_change(workflow)

    done = _browser_needed(workflow)

    assert done.stdout == "needed\n", done.stderr
