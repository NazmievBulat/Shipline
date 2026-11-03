"""Audit main in isolation and open only missing dependency tickets through the API."""

import fcntl
import json
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline

MARKER = "<!-- dependency-advisory-watch -->"

_FAKE_GH = '''#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

state = Path("@STATE@")
args = sys.argv[1:]
assert args[0] == "api", args
path = urlsplit(args[1]).path
method = args[args.index("--method") + 1]
payload = json.load(sys.stdin) if "--input" in args else None
with (state / "api.jsonl").open("a") as log:
    log.write(json.dumps([method, path, payload]) + "\\n")
prs = json.loads((state / "prs.json").read_text())
if method == "GET":
    if path.endswith("/pulls"):
        answer = prs
    elif path.endswith("/check-runs"):
        answer = {"check_runs": json.loads((state / "checks.json").read_text())}
    elif path.endswith("/annotations"):
        answer = [{"title": "Advisory", "message": '["GHSA-aaaa-bbbb-cccc in example-lib"]'}]
    elif "/git/commits/" in path:
        answer = {"sha": path.rsplit("/", 1)[1], "tree": {"sha": "main-tree"}}
    else:
        raise AssertionError(args)
else:
    if path.endswith("/git/commits"):
        assert payload["tree"] == "main-tree"
        assert len(payload["parents"]) == 1
        answer = {"sha": "empty-commit"}
    elif path.endswith("/git/refs"):
        assert payload["ref"].startswith("refs/heads/ticket/dependency-watch-")
        assert payload["sha"] == "empty-commit"
        answer = {}
    elif path.endswith("/pulls"):
        assert payload["draft"] is True and payload["base"] == "main"
        assert payload["head"].startswith("ticket/dependency-watch-")
        answer = {**payload, "number": 19, "labels": []}
        prs.append(answer)
        (state / "prs.json").write_text(json.dumps(prs))
    elif path.endswith("/labels"):
        if (state / "fail-labels").exists():
            print("HTTP 503: Service Unavailable", file=sys.stderr)
            sys.exit(1)
        number = int(path.split("/")[-2])
        for pr in prs:
            if pr["number"] == number:
                pr["labels"] = [{"name": name} for name in payload["labels"]]
        (state / "prs.json").write_text(json.dumps(prs))
        answer = []
    else:
        raise AssertionError(args)
print(json.dumps(answer))
'''


@pytest.fixture
def watch(workflow: Workflow) -> Workflow:
    install_shipline(workflow)
    workflow.fake("gh", _FAKE_GH)
    for name, value in (("prs", []), ("checks", [])):
        (workflow.state / f"{name}.json").write_text(json.dumps(value))
    (workflow.repo / "bun.lock").write_text("main lock\n")
    (workflow.repo / "backend").mkdir(exist_ok=True)
    (workflow.repo / "backend/uv.lock").write_text("python lock\n")
    workflow.git("add", "bun.lock", "backend/uv.lock")
    workflow.git("commit", "--quiet", "-m", "chore: example locks")
    workflow.git("push", "--quiet", "origin", "main")
    workflow.fake("prepare-watch", '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path
assert sys.argv[1] == os.getcwd()
assert Path("bun.lock").read_text() == "main lock\\n"
Path("prepared").touch()
''')
    workflow.fake("audit-watch", '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
state = Path("@STATE@")
with (state / "audit-folders").open("a") as log:
    log.write(os.getcwd() + "\\n")
root = Path.cwd() if sys.argv[1] == "bun" else Path.cwd().parent
assert (root / "prepared").exists()
print((state / (sys.argv[1] + ".json")).read_text())
sys.exit(1)
''')
    _report(workflow)
    (workflow.state / "pip.json").write_text('{"dependencies": []}')
    _config(workflow)
    return workflow


def _config(workflow: Workflow, **changes: object) -> None:
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["checks"]["advisory_annotation"] = "Advisory"
    config["dependency_watch"] = {
        "prepare": ["prepare-watch {root}"],
        "audits": [
            {"run": "audit-watch bun", "format": "bun-audit", "lock_files": ["bun.lock"]},
            {"run": "audit-watch pip", "format": "pip-audit", "cwd": "backend",
             "lock_files": ["backend/uv.lock"]},
        ],
        **changes,
    }
    path.write_text(json.dumps(config))


def _report(workflow: Workflow, package: str = "example-lib", fixed: str = ">=2.0.1",
            severity: str = "high") -> None:
    (workflow.state / "bun.json").write_text(json.dumps({"advisories": {"123": {
        "module_name": package, "severity": severity, "github_advisory_id": "GHSA-aaaa-bbbb-cccc",
        "cves": ["CVE-2026-1234"], "patched_versions": fixed,
    }}}))


def _prs(workflow: Workflow, *prs: dict) -> None:
    (workflow.state / "prs.json").write_text(json.dumps(list(prs)))


def _calls(workflow: Workflow, method: str = "POST", suffix: str = "") -> list:
    path = workflow.state / "api.jsonl"
    calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [call for call in calls if call[0] == method and call[1].endswith(suffix)]


def test_uncovered_finding_opens_one_empty_draft_with_blocker_labels(watch: Workflow) -> None:
    (watch.state / "checks.json").write_text(json.dumps([
        {"id": 8, "name": "CI: dependencies", "conclusion": "failure", "status": "completed"},
    ]))
    (watch.repo / "bun.lock").write_text("uncommitted work\n")
    before = watch.git("rev-parse", "HEAD").stdout

    done = watch.shipline("dependency-watch")

    assert done.returncode == 0, done.stderr
    created = _calls(watch, suffix="/pulls")
    assert len(created) == 1
    body = created[0][2]["body"]
    assert MARKER in body and "Worker: Codex (a bug fix with no new UI)" in body
    for text in ("npm", "example-lib", "GHSA-aaaa-bbbb-cccc", "CVE-2026-1234", ">=2.0.1", "bun.lock"):
        assert text in body
    assert created[0][2]["title"].startswith("fix(deps): example-lib >=2.0.1")
    assert created[0][2]["title"].endswith("(ticket, not started)")
    assert _calls(watch, suffix="/labels")[0][2]["labels"] == ["workflow", "priority: blocker"]
    assert (watch.repo / "bun.lock").read_text() == "uncommitted work\n"
    assert watch.git("rev-parse", "HEAD").stdout == before
    assert not (watch.repo / "prepared").exists()
    folders = (watch.state / "audit-folders").read_text().splitlines()
    assert len(folders) == 2 and all(not Path(folder).exists() for folder in folders)


@pytest.mark.parametrize("title,body", [
    ("fix(deps): update example-lib", ""),
    ("CHORE(deps-dev): update example-lib", ""),
    ("bump example-lib to 2.0.1", ""),
    ("fix dependencies", MARKER + "\nexample-lib"),
])
def test_a_dependency_pr_covers_the_package(watch: Workflow, title: str, body: str) -> None:
    _prs(watch, {"number": 3, "title": title, "body": body, "draft": False})
    done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert not _calls(watch)
    assert "covered" in done.stdout


@pytest.mark.parametrize("title,body", [
    ("fix: example-lib is mentioned in passing", "example-lib"),
    ("fix(deps): example-lib-extra", ""),
    ("bump @scope/example-lib", ""),
    ("bump other-example-lib", ""),
])
def test_an_unrelated_or_partial_name_does_not_cover(watch: Workflow, title: str, body: str) -> None:
    _prs(watch, {"number": 3, "title": title, "body": body, "draft": False})
    done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert len(_calls(watch, suffix="/pulls")) == 1
    assert _calls(watch, suffix="/labels")[0][2]["labels"] == ["workflow", "priority: high"]


def test_label_failure_is_retried_without_another_ticket(watch: Workflow) -> None:
    (watch.state / "fail-labels").touch()
    first = watch.shipline("dependency-watch")
    assert first.returncode != 0
    (watch.state / "fail-labels").unlink()
    second = watch.shipline("dependency-watch")
    assert second.returncode == 0, second.stderr
    assert len(_calls(watch, suffix="/pulls")) == 1
    assert len(_calls(watch, suffix="/labels")) == 2


def test_no_fix_is_reported_without_opening_a_ticket(watch: Workflow) -> None:
    _report(watch, fixed="<0.0.0")
    done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert "no fix" in done.stdout and "example-lib" in done.stdout
    assert not _calls(watch)


def test_dry_run_prints_ticket_and_never_mutates_github(watch: Workflow) -> None:
    done = watch.shipline("dependency-watch", "--dry-run")
    assert done.returncode == 0, done.stderr
    assert MARKER in done.stdout and "example-lib" in done.stdout
    assert not _calls(watch)


def test_a_long_title_falls_back_without_losing_packages(watch: Workflow) -> None:
    package = "example-" + "package-" * 40
    _report(watch, package=package)
    done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    payload = _calls(watch, suffix="/pulls")[0][2]
    assert len(payload["title"]) <= 256
    assert package in payload["body"]


@pytest.mark.parametrize("spelling", ["example_lib", "example.lib", "example-lib", "EXAMPLE.LIB"])
def test_python_name_separators_are_interchangeable(watch: Workflow, spelling: str) -> None:
    (watch.state / "bun.json").write_text('{"advisories": {}}')
    (watch.state / "pip.json").write_text(json.dumps({"dependencies": [{
        "name": "example-lib", "version": "1.0", "vulns": [{
            "id": "PYSEC-2026-123", "aliases": ["CVE-2026-1234"], "fix_versions": ["2.0.1"],
        }],
    }]}))
    _prs(watch, {"number": 3, "title": f"bump {spelling}", "body": "", "draft": False})
    done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert not _calls(watch)


def test_without_configuration_there_is_nothing_to_watch(workflow: Workflow) -> None:
    install_shipline(workflow)
    done = workflow.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert "nothing to watch" in done.stdout


def test_severity_threshold_ignores_lower_findings(watch: Workflow) -> None:
    _report(watch, severity="moderate")
    done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert not _calls(watch)
    _config(watch, minimum_severity="moderate")
    done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert len(_calls(watch, suffix="/pulls")) == 1


def test_an_invalid_audit_report_opens_nothing(watch: Workflow) -> None:
    (watch.state / "pip.json").write_text('{"error": "audit unavailable"}')
    done = watch.shipline("dependency-watch")
    assert done.returncode != 0
    assert not _calls(watch)


def test_a_second_watch_does_not_run_under_the_lock(watch: Workflow) -> None:
    folder = watch.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "dependency-watch.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = watch.shipline("dependency-watch")
    assert done.returncode == 0, done.stderr
    assert "already running" in done.stdout
    assert not (watch.state / "audit-folders").exists()


def test_timer_and_uninstall_keep_the_installing_environment(watch: Workflow) -> None:
    done = watch.shipline("install-dependency-timer", env={"GH_CONFIG_DIR": str(watch.state / "gh")})
    assert done.returncode == 0, done.stderr
    units = Path(watch.env["HOME"]) / ".config/systemd/user"
    service = (units / "dependency-watch.service").read_text()
    timer = (units / "dependency-watch.timer").read_text()
    for text in ("Type=oneshot", "TimeoutStartSec=15min", '" dependency-watch',
                 f'Environment="PATH={watch.env["PATH"]}"',
                 f'Environment="GH_CONFIG_DIR={watch.state / "gh"}"'):
        assert text in service
    assert "OnCalendar=*-*-* 03,15:17:00 UTC" in timer and "Persistent=true" in timer
    assert watch.shipline("uninstall").returncode == 0
    assert not (units / "dependency-watch.service").exists()
    assert not (units / "dependency-watch.timer").exists()
