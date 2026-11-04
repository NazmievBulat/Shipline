"""Independent breaker cases for dependency watch; all external services are stand-ins.

Run only this file with pytest. Git repositories, programs and state belong to tmp_path.
No existing dependency-watch test or fixture is used.
"""

import copy
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"
sys.path.insert(0, str(SCRIPTS))
import dependency_watch as watch
import workflow_config


GHSA = "GHSA-aaaa-bbbb-cccc"
PYSEC = "PYSEC-2026-123"


def npm_report(severity="high", fixed=">=2.0.1"):
    return {"advisories": {"123": {
        "module_name": "example-lib", "severity": severity,
        "github_advisory_id": GHSA, "patched_versions": fixed,
    }}}


def python_report(fixes=None):
    return {"dependencies": [{"name": "example-python", "version": "1.0.0", "vulns": [{
        "id": PYSEC, "aliases": ["CVE-2026-1234"],
        "fix_versions": ["2.0.0", "1.0.1"] if fixes is None else fixes,
    }]}]}


class Installation:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.repo = root / "example-app"
        self.repo.mkdir()
        self.programs = root / "programs"
        self.programs.mkdir()
        self.home = root / "user-config"
        self.home.mkdir()
        self.tmp = root / "snapshots"
        self.tmp.mkdir()
        self.calls = []
        self.prs = []
        self.checks = []
        self.annotations = []
        self.fail_labels = False
        self.lost_label_reply = False
        self.bun = npm_report()
        self.pip = python_report()
        self.mutations = []
        for key in ("SHIPLINE_REPO", "SHIPLINE_CONFIG", "SHIPLINE_CONFIG_JSON", "VIRTUAL_ENV"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(self.home / "config"))
        monkeypatch.setenv("TMPDIR", str(self.tmp))
        monkeypatch.setenv("PATH", str(self.programs) + os.pathsep + os.environ["PATH"])
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Bulat Nazmiev")
        self.git("config", "user.email", "82903523+NazmievBulat@users.noreply.github.com")
        (self.repo / "backend").mkdir()
        (self.repo / "bun.lock").write_text("locked web\n")
        (self.repo / "backend/uv.lock").write_text("locked server\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "test: neutral dependency locks")
        self.git("init", "--quiet", "--bare", str(root / "remote.git"))
        self.git("remote", "add", "origin", str(root / "remote.git"))
        self.git("push", "--quiet", "origin", "main")
        self.settings = workflow_config._dependency_watch({
            "prepare": ["bun install --frozen-lockfile --ignore-scripts",
                        "uv sync --project {root}/backend --locked"],
            "audits": [{"run": "bun audit --audit-level=high --json", "format": "bun-audit"},
                       {"run": "pip-audit --skip-editable --format json", "format": "pip-audit",
                        "cwd": "backend"}],
        }, "dependency_watch")
        self.config = SimpleNamespace(
            main=self.repo, repository="example-org/example-app", api="repos/example-org/example-app",
            dependency_watch=self.settings, state=lambda: self.repo / ".local/workflow",
            labels=SimpleNamespace(workflow="workflow", priority={"blocker": "priority: blocker",
                                                                   "high": "priority: high"}),
            checks=SimpleNamespace(dependency_job="CI: dependencies", advisory_annotation="Advisory"),
        )
        monkeypatch.setattr(watch.workflow_config, "current", lambda: self.config)
        monkeypatch.setattr(watch, "install_signal_handlers", lambda: None)
        monkeypatch.setattr(watch, "github", self.github)
        monkeypatch.setattr(watch, "run", self.run)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()

    def run(self, *args, timeout, input=None):
        cwd = Path.cwd()
        self.calls.append((args, cwd, timeout))
        if args[0] == "bun":
            assert cwd != self.repo and (cwd / "bun.lock").read_text() == "locked web\n"
            if args[1] == "install":
                assert args[2:] == ("--frozen-lockfile", "--ignore-scripts")
                (cwd / "prepared-web").touch()
                output, code = "", 0
            else:
                assert args[1:] == ("audit", "--audit-level=high", "--json")
                assert (cwd / "prepared-web").exists()
                output, code = json.dumps(self.bun), 1
            return subprocess.CompletedProcess(args, code, output, "")
        if args[0] == "uv":
            assert args[1:] == ("sync", "--project", str(cwd / "backend"), "--locked")
            (cwd / "backend/prepared-server").touch()
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "pip-audit":
            assert cwd.name == "backend" and (cwd / "prepared-server").exists()
            assert args[1:] == ("--skip-editable", "--format", "json")
            return subprocess.CompletedProcess(args, 1, json.dumps(self.pip), "")
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input)

    def github(self, path, payload=None):
        if payload is not None:
            self.mutations.append((path, copy.deepcopy(payload)))
            if path.endswith("/git/commits"):
                return {"sha": "empty-ticket-commit"}
            if path.endswith("/git/refs"):
                return {}
            if path.endswith("/pulls"):
                pr = {**payload, "number": 19, "labels": [], "head": {
                    "ref": payload["head"], "repo": {"full_name": self.config.repository}}}
                self.prs.append(pr)
                return pr
            if path.endswith("/labels"):
                if self.fail_labels:
                    raise watch.WatchError("GitHub label request failed (exit 1)")
                existing = [entry["name"] for entry in self.prs[0]["labels"]]
                self.prs[0]["labels"] = [{"name": name} for name in dict.fromkeys(existing + payload["labels"])]
                if self.lost_label_reply:
                    raise watch.CommandTimeout("stand-in response timed out")
                return []
            raise AssertionError("unexpected mutation")
        if path == "user":
            return {"name": "Example Maintainer", "login": "example-owner", "id": 123}
        if "/git/commits/" in path:
            return {"tree": {"sha": "main-tree"}}
        page = int(path.rsplit("page=", 1)[-1]) if "page=" in path else 1
        if "/pulls?" in path:
            values = self.prs
        elif "/annotations?" in path:
            values = self.annotations
        elif "/check-runs?" in path:
            return {"check_runs": self.checks[(page - 1) * 100:page * 100]}
        else:
            raise AssertionError("unexpected read")
        return values[(page - 1) * 100:page * 100]

    def main(self, monkeypatch, dry=False):
        monkeypatch.setattr(sys, "argv", ["dependency-watch"] + (["--dry-run"] if dry else []))
        return watch.main()

    def posts(self, suffix):
        return [payload for path, payload in self.mutations if path.endswith(suffix)]


@pytest.fixture
def installation(tmp_path, monkeypatch):
    return Installation(tmp_path, monkeypatch)


def test_snapshot_commands_title_labels_and_dirty_checkout(installation, monkeypatch):
    app = installation
    before = app.git("rev-parse", "HEAD")
    (app.repo / "bun.lock").write_text("unfinished checkout edit\n")
    assert app.main(monkeypatch) == 0
    assert len(app.posts("/pulls")) == 1
    draft = app.posts("/pulls")[0]
    assert draft["title"] == ("fix(deps): example-lib >=2.0.1; example-python 1.0.1 / 2.0.0, "
                              "clearing 123, CVE-2026-1234, GHSA-aaaa-bbbb-cccc, PYSEC-2026-123 "
                              "(ticket, not started)")
    assert draft["draft"] is True and draft["base"] == "main"
    assert "backend/uv.lock" in draft["body"] and "bun.lock" in draft["body"]
    assert app.posts("/labels") == [{"labels": ["workflow", "priority: high"]}]
    commit = app.posts("/git/commits")[0]
    assert commit["parents"] == [before] and commit["tree"] == "main-tree"
    assert commit["author"]["email"].endswith("@users.noreply.github.com")
    assert (app.repo / "bun.lock").read_text() == "unfinished checkout edit\n"
    assert app.git("rev-parse", "HEAD") == before
    assert not list(app.tmp.iterdir())


def test_fetched_remote_head_is_used_even_when_checkout_is_older(installation, monkeypatch):
    app = installation
    old = app.git("rev-parse", "HEAD")
    (app.repo / "new-main-file").touch()
    app.git("add", "new-main-file")
    app.git("commit", "--quiet", "-m", "test: advance neutral main")
    new = app.git("rev-parse", "HEAD")
    app.git("push", "--quiet", "origin", "main")
    app.git("checkout", "--quiet", "--detach", old)
    assert app.main(monkeypatch) == 0
    assert app.posts("/git/commits")[0]["parents"] == [new]
    assert app.git("rev-parse", "HEAD") == old


def test_bare_pip_audit_reads_prepared_snapshot_environment(installation, monkeypatch):
    app = installation
    app.bun = {}
    unrelated = app.programs / "pip-audit"
    unrelated.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote('{"dependencies": []}') + "\n")
    unrelated.chmod(0o700)
    real = app.run
    def prepare_and_run(*args, timeout, input=None):
        if args[0] == "pip-audit":
            # Resolve the actual command through PATH; no emulated audit selection.
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input)
        done = real(*args, timeout=timeout, input=input)
        if args[0] == "uv":
            programs = Path(args[3]) / ".venv/bin"
            programs.mkdir(parents=True)
            snapshot_auditor = programs / "pip-audit"
            snapshot_auditor.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(json.dumps(app.pip)) + "\n")
            snapshot_auditor.chmod(0o700)
            expected = subprocess.run([str(snapshot_auditor), "--skip-editable", "--format", "json"],
                                      capture_output=True, text=True, timeout=5)
            assert PYSEC in expected.stdout
        return done
    monkeypatch.setattr(watch, "run", prepare_and_run)
    assert app.main(monkeypatch) == 0
    assert len(app.posts("/pulls")) == 1, \
        "snapshot Python advisory was missed: PATH selected the installing environment's auditor"
    assert PYSEC in app.posts("/pulls")[0]["body"]


def test_bulk_bun_advisory_uses_published_package_fix(installation, monkeypatch):
    app = installation
    app.bun = {"example-lib": [{"id": 123, "severity": "critical",
                                "url": "https://github.com/advisories/" + GHSA,
                                "vulnerable_versions": "<2.0.1"}]}
    real = app.github
    def advisory(path, payload=None):
        if path == "advisories/" + GHSA:
            return {"ghsa_id": GHSA, "cve_id": "CVE-2026-1234", "vulnerabilities": [
                {"package": {"ecosystem": "npm", "name": "example-lib"},
                 "first_patched_version": "2.0.1"},
                {"package": {"ecosystem": "npm", "name": "example-other"},
                 "first_patched_version": "9.0.0"}]}
        return real(path, payload)
    monkeypatch.setattr(watch, "github", advisory)
    assert app.main(monkeypatch) == 0
    body = app.posts("/pulls")[0]["body"]
    assert "fixed versions: 2.0.1" in body and "9.0.0" not in body


def test_title_fallback_preserves_every_finding():
    items = [watch.Finding("npm", f"example-lib-{n}", (GHSA,), ("2.0.1",), ("bun.lock",))
             for n in range(30)]
    title, body = watch.ticket(items)
    assert len(title) <= 256
    assert all(item.package in body for item in items)


@pytest.mark.parametrize("severity,expected", [("low", 0), ("moderate", 0), ("high", 1), ("critical", 1)])
def test_bun_severity_floor(installation, monkeypatch, severity, expected):
    app = installation
    app.bun = npm_report(severity)
    app.pip = {"dependencies": []}
    assert app.main(monkeypatch) == 0
    assert len(app.posts("/pulls")) == expected


@pytest.mark.parametrize("title,body,covered", [
    ("CHORE(deps-dev): example-lib", "", True),
    ("BUMP example-lib", "", True),
    ("fix: example-lib", "", False),
    ("bump other-example-lib", "", False),
    ("bump @scope/example-lib", "", False),
    ("bump example-lib/extra", "", False),
    ("bump example-lib.extra", "", False),
    ("bump example-lib@2.0.1", "", False),
    ("fix: update dependencies", watch.MARKER + "\nexample-lib", True),
])
def test_dependency_title_and_whole_package_boundaries(title, body, covered):
    item = watch.Finding("npm", "example-lib", (GHSA,), ("2.0.1",), ("bun.lock",))
    assert watch.covered(item, {"title": title, "body": body}) is covered


@pytest.mark.parametrize("name", ["example_python", "EXAMPLE.PYTHON", "example---python"])
def test_python_name_equivalence(name):
    item = watch.Finding("python", "example-python", (PYSEC,), ("2.0",), ("backend/uv.lock",))
    assert watch.covered(item, {"title": "bump " + name})


def test_no_fix_in_both_ecosystems_opens_nothing(installation, monkeypatch, capsys):
    app = installation
    app.bun = npm_report(fixed="<0.0.0")
    app.pip = python_report([])
    assert app.main(monkeypatch) == 0
    assert "2 with no fix" in capsys.readouterr().out
    assert not app.mutations


def test_check_and_annotation_pagination_with_matching_alias(installation, monkeypatch):
    app = installation
    app.checks = [{"name": "other check", "conclusion": "success", "id": n} for n in range(100)]
    app.checks += [{"name": "CI: dependencies", "conclusion": "failure", "id": 101}]
    app.annotations = [{"title": "Advisory", "message": "unrelated"} for _ in range(100)]
    app.annotations += [{"title": "Advisory", "message": "CVE-2026-1234"}]
    assert app.main(monkeypatch) == 0
    assert app.posts("/labels")[0]["labels"] == ["workflow", "priority: blocker"]


@pytest.mark.parametrize("checks", [[], [{"name": "CI: dependencies", "conclusion": None, "id": 1}],
                                    [{"name": "CI: dependencies", "conclusion": "success", "id": 1}]])
def test_lost_pending_or_rerun_successful_check_keeps_high_priority(installation, monkeypatch, checks):
    app = installation
    app.checks = checks
    app.annotations = [{"title": "Advisory", "message": GHSA}]
    assert app.main(monkeypatch) == 0
    assert app.posts("/labels")[0]["labels"] == ["workflow", "priority: high"]


@pytest.mark.parametrize("lost_reply", [False, True])
def test_failed_or_lost_label_response_recovers_without_duplicate(installation, monkeypatch, lost_reply):
    app = installation
    app.fail_labels = not lost_reply
    app.lost_label_reply = lost_reply
    assert app.main(monkeypatch) == 1
    app.fail_labels = app.lost_label_reply = False
    assert app.main(monkeypatch) == 0
    assert len(app.posts("/pulls")) == 1
    assert [label["name"] for label in app.prs[0]["labels"]] == ["workflow", "priority: high"]


def test_failed_labels_are_retried_after_main_is_fixed(installation, monkeypatch):
    app = installation
    app.fail_labels = True
    assert app.main(monkeypatch) == 1
    app.fail_labels = False
    app.bun, app.pip = {}, {"dependencies": []}
    assert app.main(monkeypatch) == 0
    assert len(app.posts("/pulls")) == 1
    assert [label["name"] for label in app.prs[0]["labels"]] == ["workflow", "priority: high"], \
        "marked draft remains unlabelled after findings disappear"


def test_held_lock_exits_promptly_and_releases_after_holder_finishes(installation, monkeypatch, capsys):
    app = installation
    state = app.config.state()
    state.mkdir(parents=True)
    with (state / "dependency-watch.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        assert app.main(monkeypatch) == 0
        assert time.monotonic() - started < 1
        assert "already running" in capsys.readouterr().out
        assert not app.calls
    assert app.main(monkeypatch) == 0
    assert len(app.posts("/pulls")) == 1


def test_two_live_calls_run_only_one_audit(installation, monkeypatch, capsys):
    app = installation
    helper = app.root / "hold-watch.py"
    helper.write_text('''import sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv.pop(1))
import dependency_watch as watch
root = Path(sys.argv.pop(1))
watch.workflow_config.current = lambda: SimpleNamespace(
    dependency_watch={"audits": [1]}, state=lambda: root / "example-app/.local/workflow")
def hold(*args):
    (root / "started").touch()
    stop = time.monotonic() + 10
    while not (root / "release").exists():
        if time.monotonic() > stop:
            raise RuntimeError("test holder deadline")
        time.sleep(0.01)
watch.audit = hold
raise SystemExit(watch.main())
''')
    child = subprocess.Popen([sys.executable, str(helper), str(SCRIPTS), str(app.root)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not (app.root / "started").exists() and child.poll() is None:
            assert time.monotonic() < deadline, "holder never acquired lock"
            time.sleep(0.01)
        assert child.poll() is None
        assert app.main(monkeypatch) == 0
        assert "already running" in capsys.readouterr().out
        assert not app.calls
        (app.root / "release").touch()
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


@pytest.mark.parametrize("failure", ["timeout", "exit", "json"])
def test_audit_failure_redacts_diagnostics_and_releases_lock(installation, monkeypatch, capsys, failure):
    app = installation
    real = app.run
    def failing(*args, timeout, input=None):
        if args[0] == "pip-audit":
            if failure == "timeout":
                raise watch.CommandTimeout("PRIVATE_DIAGNOSTIC", "PRIVATE_DIAGNOSTIC")
            return subprocess.CompletedProcess(args, 2 if failure == "exit" else 1,
                                               "PRIVATE_DIAGNOSTIC", "PRIVATE_DIAGNOSTIC")
        return real(*args, timeout=timeout, input=input)
    monkeypatch.setattr(watch, "run", failing)
    assert app.main(monkeypatch) == 1
    assert "PRIVATE_DIAGNOSTIC" not in str(capsys.readouterr())
    assert not app.mutations and not list(app.tmp.iterdir())
    monkeypatch.setattr(watch, "run", real)
    assert app.main(monkeypatch) == 0


def test_missing_lock_file_stops_before_ticket_creation(installation, monkeypatch):
    app = installation
    app.settings["audits"][1]["lock_files"] = ["backend/missing.lock"]
    assert app.main(monkeypatch) == 1
    assert not app.mutations


def test_absent_settings_does_not_fetch_or_write(installation, monkeypatch):
    app = installation
    app.config.dependency_watch = None
    assert app.main(monkeypatch) == 0
    assert not app.calls and not app.mutations and not app.config.state().exists()


def test_dry_run_does_not_retry_labels_or_create_tickets(installation, monkeypatch):
    app = installation
    app.fail_labels = True
    assert app.main(monkeypatch) == 1
    before = copy.deepcopy(app.mutations)
    app.fail_labels = False
    assert app.main(monkeypatch, dry=True) == 0
    assert app.mutations == before


def test_fork_marker_never_receives_labels(installation, monkeypatch):
    app = installation
    app.prs = [{"number": 3, "draft": True, "title": "fix(deps): example-lib example-python",
                "body": watch.MARKER, "labels": [], "head": {
                    "ref": "ticket/dependency-watch-example", "repo": {"full_name": "contributor/example-app"}}}]
    assert app.main(monkeypatch) == 0
    assert not app.mutations


def test_ticket_ignores_advisory_prose_and_rejects_command_text(installation, monkeypatch):
    app = installation
    app.bun["advisories"]["123"]["title"] = "PRIVATE_DIAGNOSTIC"
    assert app.main(monkeypatch) == 0
    assert "PRIVATE_DIAGNOSTIC" not in json.dumps(app.mutations)
    with pytest.raises(watch.WatchError):
        watch.finding("npm", "example-lib;touch injected", (GHSA,), "2.0", ("bun.lock",))
    with pytest.raises(watch.WatchError):
        watch.versions("2.0 $(touch injected)")
    assert not (app.repo / "injected").exists()


def test_state_and_lock_are_private(installation, monkeypatch):
    app = installation
    assert app.main(monkeypatch) == 0
    assert app.config.state().stat().st_mode & 0o777 == 0o700
    assert (app.config.state() / "dependency-watch.lock").stat().st_mode & 0o777 == 0o600


def test_timer_schedule_environment_permissions_and_uninstall(installation):
    app = installation
    config = app.repo / "shipline.json"
    config.write_text(json.dumps({"repository": "example-org/example-app",
                                  "dependency_watch": app.settings}))
    program = app.programs / "systemctl"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o700)
    env = {**os.environ, "SHIPLINE_CONFIG": str(config), "GH_CONFIG_DIR": str(app.home / "gh")}
    launcher = SCRIPTS.parents[1] / "bin/shipline"
    done = subprocess.run([str(launcher), "--repo", str(app.repo), "install-dependency-timer"],
                          env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, "timer installation failed"
    units = Path(env["XDG_CONFIG_HOME"]) / "systemd/user"
    service = (units / "dependency-watch.service").read_text()
    timer = (units / "dependency-watch.timer").read_text()
    assert "OnCalendar=*-*-* 03,15:17:00 UTC" in timer and "Persistent=true" in timer
    assert "TimeoutStartSec=15min" in service and "Type=oneshot" in service
    assert f'Environment="GH_CONFIG_DIR={env["GH_CONFIG_DIR"]}"' in service
    assert f'Environment="PATH={env["PATH"]}"' in service
    assert (units / "dependency-watch.service").stat().st_mode & 0o777 == 0o600
    done = subprocess.run([str(launcher), "--repo", str(app.repo), "uninstall"],
                          env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0
    assert not (units / "dependency-watch.service").exists()
    assert not (units / "dependency-watch.timer").exists()
