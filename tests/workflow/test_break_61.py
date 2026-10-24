"""Breaking tests for pull request 61: the example E2E and image workflows, copied as they are.

A new repository set up from the guide copies `examples/e2e.yml` and `examples/images.yml`,
holds no `scripts/workflow/` and runs on GitHub's own runners. Each test replays the copied
planner job step by step in such a repository (`example-app`): its own `uses:` steps stand in
for the checkout, every `run:` step runs in bash as a hosted runner runs it, with its `if:` and
`env:` read as GitHub reads them, and the clone of Shipline is served from this checkout. The
notes the job prints are the annotations the merge gate reads.
"""

import json
import os
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SHIPLINE_URL = "https://github.com/NazmievBulat/Shipline.git"
_REPOSITORY = "example-owner/example-app"


# ---- a small reader of GitHub's expressions, enough for the examples' conditions ----

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+)|(?P<op>==|!=|&&|\|\||!|\(|\)|,)"
    r"|(?P<name>[A-Za-z_][\w.-]*))"
)


def _tokens(text: str) -> list[tuple[str, str]]:
    out, at = [], 0
    while at < len(text):
        if text[at:].strip() == "":
            break
        found = _TOKEN.match(text, at)
        assert found, f"cannot read {text[at:]!r}"
        kind = found.lastgroup
        assert kind
        out.append((kind, found[kind]))
        at = found.end()
    return out


class _Expr:
    def __init__(self, text: str, context: dict[str, Any]) -> None:
        self.tokens, self.at, self.context = _tokens(text), 0, context

    def value(self) -> Any:
        result = self._or()
        assert self.at == len(self.tokens), self.tokens[self.at:]
        return result

    def _peek(self) -> str | None:
        return self.tokens[self.at][1] if self.at < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        self.at += 1
        return self.tokens[self.at - 1]

    def _or(self) -> Any:
        left = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            left = left if _truthy(left) else right
        return left

    def _and(self) -> Any:
        left = self._eq()
        while self._peek() == "&&":
            self._take()
            right = self._eq()
            left = right if _truthy(left) else left
        return left

    def _eq(self) -> Any:
        left = self._not()
        while self._peek() in ("==", "!="):
            op = self._take()[1]
            right = self._not()
            same = _loose(left) == _loose(right)
            left = same if op == "==" else not same
        return left

    def _not(self) -> Any:
        if self._peek() == "!":
            self._take()
            return not _truthy(self._not())
        return self._atom()

    def _atom(self) -> Any:
        kind, text = self._take()
        if text == "(":
            inner = self._or()
            assert self._take()[1] == ")"
            return inner
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return int(text)
        if text in ("true", "false"):
            return text == "true"
        if self._peek() == "(":
            self._take()
            args = []
            while self._peek() != ")":
                args.append(self._or())
                if self._peek() == ",":
                    self._take()
            self._take()
            return _call(text, args)
        value: Any = self.context
        for part in text.split("."):
            value = value.get(part, "") if isinstance(value, dict) else ""
        return value


def _truthy(value: Any) -> bool:
    return value not in ("", None, False, 0)


def _loose(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _call(name: str, args: list[Any]) -> Any:
    if name == "fromJSON":
        return json.loads(args[0])
    if name == "contains":
        haystack, needle = args
        if isinstance(haystack, list):
            return any(_loose(item) == _loose(needle) for item in haystack)
        return _loose(needle) in _loose(haystack)
    if name in ("always", "success"):
        return True
    raise AssertionError(f"unknown function {name}")


def _evaluate(text: str, context: dict[str, Any]) -> Any:
    text = text.strip()
    found = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    return _Expr(found[1] if found else text, context).value()


def _substitute(text: str, context: dict[str, Any]) -> str:
    def one(found: re.Match[str]) -> str:
        value = _Expr(found[1], context).value()
        return value if isinstance(value, str) else json.dumps(value)

    return re.sub(r"\$\{\{(.*?)\}\}", one, text)


def test_the_expression_reader_reads_github_s_forms() -> None:
    context = {"steps": {"a": {"outputs": {"x": '["images"]'}}}, "github": {"event_name": "push"}}
    assert _evaluate("github.event_name == 'push'", context) is True
    assert _evaluate("${{ steps.b.outputs.y || '[]' }}", context) == "[]"
    assert _evaluate("contains(fromJSON(steps.a.outputs.x), 'images')", context) is True
    assert _evaluate("!contains(fromJSON(steps.a.outputs.x), 'images')", context) is False


# ---- a repository set up from the guide, and the run of one copied planner ----


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


_FAKE_GH = """\
#!/usr/bin/env python3
import json, os, sys, time
args = [a for a in sys.argv[1:] if a != '--paginate']
with open(os.environ['FAKE_GH_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
mode = os.environ.get('FAKE_GH_MODE', 'answers')
if mode == 'down':
    print('HTTP 502: Bad Gateway', file=sys.stderr); sys.exit(1)
if mode == 'hang':
    time.sleep(120)
answers = json.load(open(os.environ['FAKE_GH_ANSWERS']))
path = args[-1]
if path not in answers:
    print('gh: Not Found (HTTP 404)', file=sys.stderr); sys.exit(1)
print(json.dumps(answers[path]))
"""


class App:
    """A repository set up from the guide: its own files, the copied workflows, no Shipline."""

    def __init__(self, tmp: Path, config: dict[str, Any] | None, plan: dict[str, Any]) -> None:
        self.tmp = tmp
        self.root = tmp / "example-app"
        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.name", "Example")
        _git(self.root, "config", "user.email", "owner@example.com")
        (self.root / ".github/workflows").mkdir(parents=True)
        for name in ("e2e.yml", "images.yml"):
            shutil.copy(_ROOT / "examples" / name, self.root / ".github/workflows" / name)
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "ci-plan").write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(plan)}\nEOF\n")
        (tools / "ci-plan").chmod(0o755)
        if config is not None:
            (self.root / "shipline.json").write_text(json.dumps(config))
        (self.root / "app.txt").write_text("example\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "start")
        # As actions/checkout leaves it with persist-credentials: false.
        _git(self.root, "remote", "add", "origin", f"https://github.com/{_REPOSITORY}")
        assert not (self.root / "scripts").exists()
        # Shipline's GitHub is served from this checkout's committed head.
        self.upstream = tmp / "shipline-upstream"
        subprocess.run(
            ["git", "clone", "-q", "--no-local", str(_ROOT), str(self.upstream)], check=True
        )
        # The working tree as it is here, the pull request's files, not only what is committed.
        for name in ("scripts", "examples"):
            shutil.rmtree(self.upstream / name)
            shutil.copytree(_ROOT / name, self.upstream / name)
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "-c", "user.name=t", "-c", "user.email=t@example.com",
             "commit", "-q", "--allow-empty", "-m", "as here")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(_FAKE_GH)
        (self.bin / "gh").chmod(0o755)
        self.gh_log = tmp / "gh.log"
        self.gh_log.touch()
        self.answers = tmp / "gh.json"
        self.answers.write_text("{}")
        self.home = tmp / "home"
        self.home.mkdir()

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def tree(self) -> str:
        return _git(self.root, "rev-parse", "HEAD^{tree}")

    def run_job(
        self, workflow: str, job: str, event: str, *, gh_mode: str = "answers",
        clone_fails: bool = False, needs: dict[str, Any] | None = None,
    ) -> "JobRun":
        spec = yaml.safe_load((self.root / ".github/workflows" / workflow).read_text())
        definition = spec["jobs"][job]
        runner_temp = self.tmp / f"runner-temp-{workflow}-{job}-{event}-{time.monotonic_ns()}"
        runner_temp.mkdir()
        context: dict[str, Any] = {
            "github": {
                "event_name": event,
                "event": {"pull_request": {"draft": False}} if event == "pull_request" else {},
                "token": "example-token",
                "sha": self.head(),
            },
            "steps": {},
            "needs": needs or {},
        }
        run = JobRun(definition, context)
        if "if" in definition and not _truthy(_evaluate(str(definition["if"]), context)):
            run.skipped = True
            return run
        upstream = f"file://{self.upstream}" if not clone_fails else "file:///nonexistent/shipline"
        base_env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_EVENT_NAME": event,
            "GITHUB_SHA": self.head(),
            "GITHUB_WORKSPACE": str(self.root),
            "CI": "true",
            "FAKE_GH_LOG": str(self.gh_log),
            "FAKE_GH_ANSWERS": str(self.answers),
            "FAKE_GH_MODE": gh_mode,
            # git fetches github.com's Shipline from the local stand-in.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{upstream}.insteadOf",
            "GIT_CONFIG_VALUE_0": _SHIPLINE_URL,
        }
        for index, step in enumerate(definition["steps"]):
            if "run" not in step:
                continue
            if "if" in step and not _truthy(_evaluate(str(step["if"]), context)):
                run.ran.append((step.get("name", str(index)), "skipped"))
                continue
            output = runner_temp / f"output-{index}"
            output.touch()
            env = dict(base_env, GITHUB_OUTPUT=str(output))
            for key, value in (step.get("env") or {}).items():
                env[key] = _substitute(str(value), context)
            script = runner_temp / f"step-{index}.sh"
            script.write_text(step["run"])
            done = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                cwd=self.root, env=env, capture_output=True, text=True, timeout=90,
            )
            run.stdout += done.stdout
            run.stderr += done.stderr
            outputs = dict(
                line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
            )
            if "id" in step:
                context["steps"][step["id"]] = {"outputs": outputs}
            run.ran.append((step.get("name", str(index)), "failed" if done.returncode else "ok"))
            if done.returncode:
                run.failed = True
                return run
        for key, value in (definition.get("outputs") or {}).items():
            run.outputs[key] = _substitute(str(value), context)
        return run


class JobRun:
    def __init__(self, definition: dict[str, Any], context: dict[str, Any]) -> None:
        self.definition, self.context = definition, context
        self.ran: list[tuple[str, str]] = []
        self.stdout = self.stderr = ""
        self.failed = self.skipped = False
        self.outputs: dict[str, str] = {}

    def notes(self) -> dict[str, list[str]]:
        """The job's notices as the gate reads them: title to messages."""
        found: dict[str, list[str]] = {}
        for line in self.stdout.splitlines():
            if hit := re.fullmatch(r"::notice title=([^:]*)::(.*)", line):
                title = hit[1].replace("%3A", ":").replace("%2C", ",").replace("%25", "%")
                found.setdefault(title, []).append(hit[2].replace("%25", "%"))
        return found


def _needs(plan: JobRun) -> dict[str, Any]:
    return {"plan": {"outputs": plan.outputs, "result": "failure" if plan.failed else "success"}}


def _app(tmp_path: Path, config: dict[str, Any] | None = None, **plan: Any) -> App:
    selection = {"needed": [], "server_files": [], "server_total": 0,
                 "browser_files": [], "browser_total": 0}
    selection.update(plan)
    return App(tmp_path, config, selection)


# ---- E2E ----


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_e2e_with_no_browser_change_notes_none(tmp_path: Path, event: str) -> None:
    app = _app(tmp_path)
    plan = app.run_job("e2e.yml", "plan", event, gh_mode="down")

    assert not plan.failed, plan.stderr
    notes = plan.notes()
    assert notes["Merge tested"] == [app.head()]
    assert notes["Files tested"] == [app.tree()]
    assert notes["Browser suite"] == ["none"]
    assert "can't open file" not in plan.stderr
    browser = app.run_job("e2e.yml", "browser", event, needs=_needs(plan))
    assert browser.skipped


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_e2e_with_a_browser_change_and_no_deferral_runs_the_whole_suite(
    tmp_path: Path, event: str
) -> None:
    app = _app(tmp_path, None, needed=["browser_whole"], browser_files=["e2e/a.spec.ts"],
               browser_total=12)
    plan = app.run_job("e2e.yml", "plan", event, gh_mode="down")

    assert not plan.failed, plan.stderr
    assert plan.notes()["Browser suite"] == ["whole"]
    assert plan.notes()["Prefix-only groups"] == ["[]"]
    assert plan.outputs["prefix_only"] == "[]"
    assert plan.outputs["needed"] == "true"
    browser = app.run_job("e2e.yml", "browser", event, needs=_needs(plan))
    assert not browser.skipped


def test_e2e_deferred_on_a_pull_request_notes_the_selection(tmp_path: Path) -> None:
    app = _app(tmp_path, {"checks": {"prefix_only": ["browser_whole"]}},
               needed=["browser_whole"], browser_files=["e2e/a.spec.ts", "e2e/b.spec.ts"],
               browser_total=12)
    plan = app.run_job("e2e.yml", "plan", "pull_request")

    assert not plan.failed, plan.stderr
    assert plan.notes()["Prefix-only groups"] == ['["browser_whole"]']
    assert plan.notes()["Browser suite"] == ["selected: 2 of 12 files"]
    assert json.loads(plan.outputs["prefix_only"]) == ["browser_whole"]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_e2e_deferral_is_never_taken_on_main_or_a_scratch_run(tmp_path: Path, event: str) -> None:
    app = _app(tmp_path, {"checks": {"prefix_only": ["browser_whole"]}},
               needed=["browser_whole"], browser_files=["e2e/a.spec.ts"], browser_total=12)
    plan = app.run_job("e2e.yml", "plan", event, gh_mode="down")

    assert not plan.failed, plan.stderr
    assert plan.notes()["Browser suite"] == ["whole"]


def test_e2e_reads_the_repository_s_own_annotation_titles_not_shipline_s(tmp_path: Path) -> None:
    """The clone holds Shipline's own shipline.json; the notes must follow example-app's."""
    app = _app(tmp_path, {"annotations": {"merge_tested": "Example merge",
                                          "files_tested": "Example tree",
                                          "browser_suite": "Example browsers"}})
    plan = app.run_job("e2e.yml", "plan", "pull_request")

    assert not plan.failed, plan.stderr
    assert plan.notes()["Example merge"] == [app.head()]
    assert plan.notes()["Example tree"] == [app.tree()]
    assert plan.notes()["Example browsers"] == ["none"]
    assert "Merge tested" not in plan.notes()


# ---- Images ----


@pytest.mark.parametrize("needed", [[], ["images"]])
@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_images_planner_notes_the_tested_tree(tmp_path: Path, event: str, needed: list[str]) -> None:
    app = _app(tmp_path, None, needed=needed)
    plan = app.run_job("images.yml", "plan", event, gh_mode="down")

    assert not plan.failed, plan.stderr
    assert plan.notes()["Merge tested"] == [app.head()]
    assert plan.notes()["Files tested"] == [app.tree()]
    images = app.run_job("images.yml", "images", event, needs=_needs(plan))
    assert images.skipped is (not needed)


def test_images_deferred_on_a_pull_request_skips_the_image_job(tmp_path: Path) -> None:
    app = _app(tmp_path, {"checks": {"prefix_only": ["images"]}}, needed=["images"])
    plan = app.run_job("images.yml", "plan", "pull_request")

    assert not plan.failed, plan.stderr
    assert json.loads(plan.outputs["prefix_only"]) == ["images"]
    assert app.run_job("images.yml", "images", "pull_request", needs=_needs(plan)).skipped


# ---- main's inheritance through the clone, with GitHub answering, lost, or hanging ----


def _squash_with_proof(app: App, workflow: str, jobs: list[str], run: int) -> None:
    base = app.head()
    (app.root / "app.txt").write_text("changed\n")
    _git(app.root, "add", "-A")
    tree = _git(app.root, "write-tree")
    proof = {"base": base, "tree": tree,
             "sources": [{"workflow": workflow, "run": run, "jobs": jobs}]}
    message = f"feat: example\n\nWorkflow-Proof: {json.dumps(proof)}\n"
    _git(app.root, "commit", "-q", "-m", message)
    head = app.head()
    api = f"repos/{_REPOSITORY}"
    plan_name = {"e2e": "E2E: what to run", "images": "Docker images: what to run"}[workflow]
    job_names = {"e2e": ["playwright"], "images": ["Docker images: image tests"]}[workflow]
    notes = [{"title": "Files tested", "message": tree}]
    if workflow == "e2e":
        notes.append({"title": "Browser suite", "message": "whole"})
    answers = {
        f"{api}/git/commits/{head}": {
            "message": message, "tree": {"sha": tree}, "parents": [{"sha": base}]},
        f"{api}/actions/runs/{run}": {
            "status": "completed", "conclusion": "success",
            "path": f".github/workflows/{workflow}.yml", "head_sha": "a" * 40,
            "head_repository": {"full_name": _REPOSITORY}},
        f"{api}/actions/runs/{run}/jobs?per_page=100": {"jobs": [
            {"id": 11, "name": plan_name, "conclusion": "success"},
            *({"id": 12 + i, "name": name, "conclusion": "success"}
              for i, name in enumerate(job_names)),
        ]},
        f"{api}/check-runs/11/annotations?per_page=100": notes,
    }
    app.answers.write_text(json.dumps(answers))


def test_main_inherits_the_browser_job_from_its_squash_s_proof(tmp_path: Path) -> None:
    app = _app(tmp_path, None, needed=["browser_whole"], browser_files=["e2e/a.spec.ts"],
               browser_total=12)
    _squash_with_proof(app, "e2e", ["playwright"], 4242)
    plan = app.run_job("e2e.yml", "plan", "push")

    assert not plan.failed, plan.stderr
    assert json.loads(plan.outputs["inherited"]) == ["playwright"]
    assert plan.notes()["Inherited job"] == ["playwright: run 4242"]
    assert app.run_job("e2e.yml", "browser", "push", needs=_needs(plan)).skipped
    # It asked GitHub about example-app, the copying repository, not Shipline.
    asked = [json.loads(line) for line in app.gh_log.read_text().splitlines()]
    assert asked and all(f"repos/{_REPOSITORY}/" in call[-1] for call in asked)


def test_main_inherits_the_image_job_from_its_squash_s_proof(tmp_path: Path) -> None:
    app = _app(tmp_path, None, needed=["images"])
    _squash_with_proof(app, "images", ["Docker images: image tests"], 4343)
    plan = app.run_job("images.yml", "plan", "push")

    assert not plan.failed, plan.stderr
    assert json.loads(plan.outputs["inherited"]) == ["Docker images: image tests"]
    assert app.run_job("images.yml", "images", "push", needs=_needs(plan)).skipped


@pytest.mark.parametrize("workflow,job", [("e2e.yml", "browser"), ("images.yml", "images")])
def test_github_lost_on_main_means_every_job_runs(tmp_path: Path, workflow: str, job: str) -> None:
    app = _app(tmp_path, None, needed=["browser_whole", "images"], browser_files=[],
               browser_total=3)
    _squash_with_proof(app, workflow.split(".")[0], ["playwright"], 7)
    plan = app.run_job(workflow, "plan", "push", gh_mode="down")

    assert not plan.failed, plan.stderr
    assert plan.outputs["inherited"] == "[]"
    assert "nothing is inherited, every job runs" in plan.stderr
    assert not app.run_job(workflow, job, "push", needs=_needs(plan)).skipped


@pytest.mark.timeout(100)
def test_github_hanging_on_main_is_bounded_and_every_job_runs(tmp_path: Path) -> None:
    app = _app(tmp_path, None, needed=["browser_whole"], browser_total=3)
    _squash_with_proof(app, "e2e", ["playwright"], 7)
    started = time.monotonic()
    plan = app.run_job("e2e.yml", "plan", "push", gh_mode="hang")

    assert not plan.failed, plan.stderr
    assert time.monotonic() - started < 80
    assert plan.outputs["inherited"] == "[]"


# ---- the clone itself ----


@pytest.mark.parametrize("workflow", ["e2e.yml", "images.yml"])
def test_a_clone_that_fails_fails_the_planner_before_any_note(tmp_path: Path, workflow: str) -> None:
    """GitHub unreachable for the clone: the planner fails, so the gate never reads a
    half-written set of notes as a pass."""
    app = _app(tmp_path, None, needed=["browser_whole", "images"])
    plan = app.run_job(workflow, "plan", "pull_request", clone_fails=True)

    assert plan.failed
    assert plan.ran[-1] == ("Shipline", "failed")
    assert plan.notes() == {}


@pytest.mark.parametrize("workflow", ["e2e.yml", "images.yml"])
def test_a_rerun_of_the_planner_starts_from_a_fresh_runner_temp(tmp_path: Path, workflow: str) -> None:
    """A re-run attempt on a hosted runner gets an empty RUNNER_TEMP: the clone runs again."""
    app = _app(tmp_path, None, needed=["images"])
    first = app.run_job(workflow, "plan", "pull_request")
    second = app.run_job(workflow, "plan", "pull_request")

    assert not first.failed and not second.failed, second.stderr
    assert first.notes()["Files tested"] == second.notes()["Files tested"]


def test_a_repository_whose_path_has_spaces_and_quotes_still_runs(tmp_path: Path) -> None:
    """RUNNER_TEMP is quoted wherever the clone is named."""
    odd = tmp_path / "a dir with 'quotes' and $dollar"
    odd.mkdir()
    app = _app(odd, None, needed=["browser_whole"], browser_files=["e2e/a.spec.ts"],
               browser_total=4)
    plan = app.run_job("e2e.yml", "plan", "pull_request")

    assert not plan.failed, plan.stderr
    assert plan.notes()["Browser suite"] == ["whole"]


@pytest.mark.parametrize("workflow", ["e2e.yml", "images.yml"])
def test_the_copied_workflow_carries_no_secret_and_no_write_permission(workflow: str) -> None:
    text = (_ROOT / "examples" / workflow).read_text()
    spec = yaml.safe_load(text)

    assert "secrets." not in text
    assert "pull_request_target" not in text
    assert spec["permissions"] == {"contents": "read"}
    for job in spec["jobs"].values():
        assert all(level in ("read", "none") for level in (job.get("permissions") or {}).values())
        assert job["runs-on"] == "ubuntu-latest"
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False
            # GH_TOKEN reaches only main's push.
            if "GH_TOKEN" in (step.get("env") or {}):
                assert step["if"] == "github.event_name == 'push'"


def test_the_clone_step_in_all_three_examples_is_the_same() -> None:
    clones = set()
    for name in ("ci.yml", "e2e.yml", "images.yml"):
        spec = yaml.safe_load((_ROOT / "examples" / name).read_text())
        for job in spec["jobs"].values():
            for step in job["steps"]:
                if step.get("name") == "Shipline":
                    clones.add(step["run"])
    assert clones == {textwrap.dedent(f"""\
        git clone --quiet --depth 1 {_SHIPLINE_URL} "$RUNNER_TEMP/shipline\"""")}
