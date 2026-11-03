"""Audit a snapshot of main and open one empty draft for uncovered, fixable advisories."""

import argparse
import fcntl
import json
import os
import re
import secrets
import shlex
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import workflow_config
from workflow_commands import CommandTimeout, github_read, install_signal_handlers, run

MARKER = "<!-- dependency-advisory-watch -->"
DEPENDENCY_TITLE = re.compile(r"^(?:\w+\(deps[\w-]*\)|bump\s)", re.IGNORECASE)
SEVERITIES = ("info", "low", "moderate", "high", "critical")
# Only package names, identifiers and version ranges enter generated tickets, never advisory
# prose, command output or URLs. Nothing read from GitHub becomes a command or file name.
PACKAGE = re.compile(r"[\w@/.-]+", re.ASCII)
IDENTIFIER = re.compile(r"(?:GHSA-[A-Za-z0-9-]+|CVE-\d{4}-\d+|PYSEC-\d{4}-\d+|\d+)", re.IGNORECASE)
VERSION = re.compile(r"[A-Za-z0-9.*+!<>=~^, |()/:-]+")


class WatchError(Exception):
    """No ticket can safely be inferred from the available evidence."""


@dataclass(frozen=True)
class Finding:
    ecosystem: str
    package: str
    ids: tuple[str, ...]
    fixes: tuple[str, ...]
    locks: tuple[str, ...]

    def line(self) -> str:
        return (f"- {self.ecosystem}: `{self.package}`; advisories and aliases: "
                f"{', '.join(self.ids)}; fixed versions: {' / '.join(self.fixes) or 'no fix'}; "
                f"lock files: {', '.join('`' + lock + '`' for lock in self.locks)}")


def identifiers(*values: Any) -> tuple[str, ...]:
    found = set()
    for value in values:
        for word in value if isinstance(value, list) else [value]:
            if isinstance(word, (str, int)) and IDENTIFIER.fullmatch(str(word)):
                found.add(str(word))
    if not found:
        raise WatchError("an audit finding has no advisory identifier")
    return tuple(sorted(found))


def versions(value: Any) -> tuple[str, ...]:
    if value is None or value is False:
        return ()
    values = value if isinstance(value, list) else [value]
    found = set()
    for item in values:
        if item in (None, "", "<0.0.0", "<=0.0.0", "*", "No fix available"):
            continue
        if not isinstance(item, str) or not VERSION.fullmatch(item) or not re.search(r"\d", item):
            raise WatchError("an audit finding has invalid fixed versions")
        found.add(item)
    return tuple(sorted(found))


def finding(ecosystem: str, package: Any, ids: tuple[str, ...], fixes: Any,
            locks: tuple[str, ...]) -> Finding:
    if not isinstance(package, str) or not PACKAGE.fullmatch(package):
        raise WatchError("an audit finding has an invalid package name")
    return Finding(ecosystem, package, ids, versions(fixes), locks)


def bun_findings(report: Any, minimum: str, locks: tuple[str, ...]) -> list[Finding]:
    if not isinstance(report, dict):
        raise WatchError("bun-audit did not return an audit object")
    result = []
    if isinstance(report.get("advisories"), dict):
        for key, advisory in report["advisories"].items():
            if not isinstance(advisory, dict) or advisory.get("severity") not in SEVERITIES:
                raise WatchError("bun-audit returned an invalid advisory")
            if SEVERITIES.index(advisory["severity"]) < SEVERITIES.index(minimum):
                continue
            result.append(finding(
                "npm", advisory.get("module_name"),
                identifiers(advisory.get("github_advisory_id"), advisory.get("id", key),
                            advisory.get("cves", [])),
                advisory.get("patched_versions"), locks,
            ))
    elif isinstance(report.get("vulnerabilities"), dict):
        # npm's newer audit shape, also accepted by tools emitting npm-style JSON.
        for package, entry in report["vulnerabilities"].items():
            if not isinstance(entry, dict) or not isinstance(entry.get("via"), list):
                raise WatchError("bun-audit returned an invalid vulnerability")
            for advisory in entry["via"]:
                if isinstance(advisory, str):
                    continue  # A reference to another package's entry.
                if not isinstance(advisory, dict) or advisory.get("severity") not in SEVERITIES:
                    raise WatchError("bun-audit returned an invalid advisory")
                if SEVERITIES.index(advisory["severity"]) < SEVERITIES.index(minimum):
                    continue
                fixed = entry.get("fixAvailable")
                fixed = fixed.get("version") if isinstance(fixed, dict) else advisory.get("patched_versions")
                url_id = str(advisory.get("url", "")).rsplit("/", 1)[-1]
                result.append(finding("npm", package,
                                      identifiers(advisory.get("source"), url_id), fixed, locks))
    elif all(isinstance(entries, list) for entries in report.values()):
        # Bun also prints the registry's bulk response: package -> advisories. This names
        # vulnerable ranges, not fixed releases. Ask the named GHSA for published fixes;
        # complementing a vulnerable range would invent versions that may not exist.
        for package, entries in report.items():
            for advisory in entries:
                if not isinstance(advisory, dict) or advisory.get("severity") not in SEVERITIES:
                    raise WatchError("bun-audit returned an invalid bulk advisory")
                if SEVERITIES.index(advisory["severity"]) < SEVERITIES.index(minimum):
                    continue
                url_id = str(advisory.get("url", "")).rsplit("/", 1)[-1]
                ids = identifiers(advisory.get("id"), advisory.get("github_advisory_id"), url_id,
                                  advisory.get("cves", []))
                fixed = advisory.get("patched_versions")
                if "patched_versions" not in advisory:
                    ghsa = next((identifier for identifier in ids if identifier.startswith("GHSA-")), None)
                    if ghsa is None:
                        raise WatchError("a bulk advisory has no GHSA for finding fixed versions")
                    detail = github("advisories/" + ghsa)
                    if not isinstance(detail, dict) or not isinstance(detail.get("vulnerabilities"), list):
                        raise WatchError("GitHub returned an invalid advisory")
                    fixed = []
                    matched = False
                    for vulnerability in detail["vulnerabilities"]:
                        named = vulnerability.get("package", {})
                        if named.get("ecosystem") != "npm" or named.get("name") != package:
                            continue
                        matched = True
                        if patch := vulnerability.get("first_patched_version"):
                            fixed.append(patch)
                    if not matched:
                        raise WatchError("GitHub's advisory does not name the audited package")
                    ids = identifiers(list(ids), detail.get("cve_id"), detail.get("ghsa_id"))
                result.append(finding("npm", package, ids, fixed, locks))
    else:
        raise WatchError("bun-audit returned no advisories or vulnerabilities object")
    return result


def pip_findings(report: Any, locks: tuple[str, ...]) -> list[Finding]:
    dependencies = report.get("dependencies") if isinstance(report, dict) else report
    if not isinstance(dependencies, list):
        raise WatchError("pip-audit returned no dependencies list")
    result = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise WatchError("pip-audit returned an invalid dependency")
        # --skip-editable deliberately leaves editable packages out of this audit.
        if isinstance(dependency.get("skip_reason"), str):
            continue
        if not isinstance(dependency.get("vulns"), list):
            raise WatchError("pip-audit could not audit every dependency")
        for advisory in dependency["vulns"]:
            if not isinstance(advisory, dict):
                raise WatchError("pip-audit returned an invalid advisory")
            result.append(finding("python", dependency.get("name"),
                                  identifiers(advisory.get("id"), advisory.get("aliases", [])),
                                  advisory.get("fix_versions"), locks))
    return result


def covered(item: Finding, pr: dict[str, Any]) -> bool:
    title, body = pr.get("title") or "", pr.get("body") or ""
    if not DEPENDENCY_TITLE.match(title) and MARKER not in body:
        return False
    package = re.escape(item.package)
    if item.ecosystem == "python":
        package = r"[-_.]+".join(re.escape(part) for part in re.split(r"[-_.]+", item.package))
    return re.search(r"(?<![\w@/.-])" + package + r"(?![\w@/.-])",
                     title + "\n" + body, re.IGNORECASE) is not None


@contextmanager
def directory(folder: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(folder)
    try:
        yield
    finally:
        os.chdir(previous)


def checked(*args: str, seconds: int = 60) -> str:
    done = run(*args, timeout=seconds, input="")
    if done.returncode:
        # Raw tool diagnostics can contain credentials and installation details.
        raise WatchError(f"{args[0]} failed (exit {done.returncode}); no new ticket was opened")
    return done.stdout.strip()


def github(path: str, payload: dict | None = None) -> Any:
    method = "GET" if payload is None else "POST"
    args = ["api", path, "--method", method]
    if payload is None:
        done = github_read(*args)
    else:
        done = run("gh", *args, "--input", "-", input=json.dumps(payload), timeout=30)
    if done.returncode:
        raise WatchError(f"GitHub {method} {path.split('?')[0]} failed (exit {done.returncode})")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise WatchError("GitHub returned invalid JSON") from None


def api(config: workflow_config.Config, path: str, payload: dict | None = None) -> Any:
    return github(config.api + "/" + path, payload)


def pages(config: workflow_config.Config, path: str, key: str | None = None) -> list[dict]:
    result = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 1001):
        answer = api(config, f"{path}{separator}per_page=100&page={page}")
        batch = answer.get(key) if key and isinstance(answer, dict) else answer
        if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
            raise WatchError("GitHub returned an invalid list")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise WatchError("GitHub pagination exceeded the dependency watch limit")


def priority(config: workflow_config.Config, head: str, findings: list[Finding]) -> str:
    checks = pages(config, f"commits/{head}/check-runs?filter=latest", "check_runs")
    ids = {identifier for item in findings for identifier in item.ids}
    for check in checks:
        if check.get("name") != config.checks.dependency_job or check.get("conclusion") != "failure":
            continue
        notes = pages(config, f"check-runs/{int(check['id'])}/annotations")
        for note in notes:
            if config.checks.advisory_annotation and note.get("title") != config.checks.advisory_annotation:
                continue
            text = "\n".join(str(note.get(key) or "") for key in ("message", "raw_details"))
            if any(re.search(r"(?<![\w-])" + re.escape(identifier) + r"(?![\w-])", text)
                   for identifier in ids):
                return config.labels.priority["blocker"]
    return config.labels.priority["high"]


def ticket(findings: list[Finding]) -> tuple[str, str]:
    packages: dict[str, set[str]] = {}
    for item in findings:
        packages.setdefault(item.package, set()).update(item.fixes)
    named = "; ".join(f"{name} {' / '.join(sorted(fixes))}" for name, fixes in sorted(packages.items()))
    ids = sorted({identifier for item in findings for identifier in item.ids})
    title = f"fix(deps): {named}, clearing {', '.join(ids)} (ticket, not started)"
    if len(title) > 256:
        title = "fix(deps): clear dependency advisories (ticket, not started)"
    body = (MARKER + "\n\n## Problem\n\nThe locked dependencies on main have fixable advisories:\n\n"
            + "\n".join(item.line() for item in findings)
            + "\n\n## What done means\n\nUpdate the affected dependencies and lock files to fixed versions. "
              "The configured dependency audits and CI pass, with these advisories cleared.\n\n"
              "## Out of scope\n\nUnrelated dependency updates and product changes.\n\n## Files\n\n"
            + "\n".join("- `" + lock + "`" for lock in sorted({lock for item in findings for lock in item.locks}))
            + "\n\nWorker: Codex (a bug fix with no new UI)\n")
    return title, body


def label(config: workflow_config.Config, number: int, labels: list[str]) -> None:
    api(config, f"issues/{number}/labels", {"labels": labels})


def reconcile(config: workflow_config.Config, head: str, findings: list[Finding], dry: bool) -> None:
    findings = sorted(set(findings), key=lambda item: (item.ecosystem, item.package, item.ids, item.fixes, item.locks))
    fixable = [item for item in findings if item.fixes]
    no_fix = [item for item in findings if not item.fixes]
    for item in no_fix:
        print(f"no fix: {item.ecosystem} {item.package} ({', '.join(item.ids)})")
    prs = pages(config, "pulls?state=open&base=main") if fixable else []
    missing = [item for item in fixable if not any(covered(item, pr) for pr in prs)]
    print(f"dependency watch: {len(fixable)} fixable, {len(fixable) - len(missing)} covered, "
          f"{len(no_fix)} with no fix")
    # A PR exists before its labels are requested. Retry that last step from the marker and
    # branch rather than keeping local success state that could be lost after an interruption.
    for pr in prs:
        if not (pr.get("draft") and MARKER in (pr.get("body") or "")):
            continue
        branch = pr.get("head", {})
        branch = branch.get("ref", "") if isinstance(branch, dict) else branch
        if not branch.startswith("ticket/dependency-watch-"):
            continue
        matching = [item for item in fixable if covered(item, pr)]
        if not matching:
            continue
        existing = {entry["name"] for entry in pr.get("labels", [])}
        required = [config.labels.workflow, priority(config, head, matching)]
        if not set(required) <= existing:
            if dry:
                print(f"would label draft #{pr['number']}: {', '.join(required)}")
            else:
                label(config, int(pr["number"]), required)
                print(f"labelled draft #{pr['number']}")
    if not missing:
        return
    labels = [config.labels.workflow, priority(config, head, missing)]
    title, body = ticket(missing)
    if dry:
        print(f"would open draft: {title}\nlabels: {', '.join(labels)}\n\n{body}")
        return
    parent = api(config, f"git/commits/{head}")
    user = github("user")
    author = {"name": user.get("name") or user["login"],
              "email": f"{int(user['id'])}+{user['login']}@users.noreply.github.com"}
    commit = api(config, "git/commits", {
        "message": "chore: open dependency advisory ticket", "tree": parent["tree"]["sha"], "parents": [head],
        "author": author, "committer": author,
    })
    branch = "ticket/dependency-watch-" + secrets.token_hex(6)
    api(config, "git/refs", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
    pr = api(config, "pulls", {"title": title, "body": body, "head": branch, "base": "main", "draft": True})
    print(f"opened draft #{pr['number']}: {title}")
    label(config, int(pr["number"]), labels)


def inside(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise WatchError("an audit path leaves the snapshot")
    return target


def audit(config: workflow_config.Config, settings: dict, dry: bool) -> None:
    with directory(config.main):
        checked("git", "fetch", "origin", "main")
        head = checked("git", "rev-parse", "origin/main")
        with tempfile.TemporaryDirectory(prefix="shipline-dependency-") as temporary:
            folder = Path(temporary)
            archive, root = folder / "main.tar", folder / "snapshot"
            root.mkdir(mode=0o700)
            checked("git", "archive", "--format=tar", "--output=" + str(archive), head)
            with tarfile.open(archive) as source:
                source.extractall(root, filter="data")
            with directory(root):
                for line in settings["prepare"]:
                    checked(*(word.replace("{root}", str(root)) for word in shlex.split(line)), seconds=600)
            findings = []
            for entry in settings["audits"]:
                cwd = inside(root, entry["cwd"])
                locks = tuple(path for path in entry["lock_files"] if inside(root, path).is_file())
                if not locks:
                    raise WatchError("an audit has no configured lock file in the snapshot")
                with directory(cwd):
                    done = run(*(word.replace("{root}", str(root)) for word in shlex.split(entry["run"])),
                               timeout=180, input="")
                if done.returncode not in (0, 1):
                    raise WatchError(f"{entry['format']} failed (exit {done.returncode})")
                try:
                    report = json.loads(done.stdout)
                except ValueError:
                    raise WatchError(f"{entry['format']} returned invalid JSON") from None
                findings.extend(bun_findings(report, settings["minimum_severity"], locks)
                                if entry["format"] == "bun-audit" else pip_findings(report, locks))
            reconcile(config, head, findings, dry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    install_signal_handlers()
    try:
        config = workflow_config.current()
        settings = config.dependency_watch
        if settings is None or not settings["audits"]:
            print("dependency watch: nothing to watch")
            return 0
        state = config.state()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(state / "dependency-watch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as held:
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("dependency watch: already running")
                return 0
            audit(config, settings, args.dry_run)
    except (WatchError, workflow_config.ConfigError) as error:
        print(f"dependency watch: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, CommandTimeout, tarfile.TarError):
        print("dependency watch: audit or GitHub operation failed; no further writes attempted",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
