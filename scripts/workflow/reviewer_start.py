"""Start reviewers without losing local work or repeating an unchanged refusal."""

import json
import sys
import uuid
from pathlib import Path

import merge_gate as gate
from merge_handoff import write_json


def _clear(folder: Path, path: Path) -> None:
    if path.exists():
        event = json.loads(path.read_text())["event"]
        (folder / "merge-handbacks.pending" / event).unlink(missing_ok=True)
        path.unlink()


def _notice(
    folder: Path,
    path: Path,
    identity: list[str],
    number: str,
    reason: str,
    blocked: bool = True,
) -> None:
    if path.exists() and (
        json.loads(path.read_text())["identity"] == identity
        and json.loads(path.read_text())["blocked"] == blocked
    ):
        data = json.loads(path.read_text())
        if not (folder / "merge-handbacks.sent" / data["event"]).exists():
            write_json(
                folder / "merge-handbacks.pending" / data["event"],
                {"number": number, "reason": reason},
            )
        return
    _clear(folder, path)
    event = uuid.uuid4().hex
    write_json(path, {"identity": identity, "event": event, "reason": reason, "blocked": blocked})
    write_json(folder / "merge-handbacks.pending" / event, {"number": number, "reason": reason})
    print(f"pull request {number} {reason}", flush=True)


def reconcile(folder: Path) -> None:
    """Withdraw obsolete notices even when a running session bypasses start checks."""
    for suffix in ("allowance-notice", "start-refused"):
        for path in folder.glob(f"review-*.{suffix}"):
            number = path.name.removeprefix("review-").removesuffix(f".{suffix}")
            data = json.loads(path.read_text())
            if suffix == "allowance-notice":
                allowance = folder / f"review-{number}.allow-round"
                if allowance.exists() and allowance.read_text().strip() == data["identity"][1]:
                    _clear(folder, path)
                    continue
            done = gate.command(
                "gh",
                "pr",
                "view",
                number,
                "--json",
                "state,headRefName,headRefOid",
                "--jq",
                '"\\(.state) \\(.headRefName) \\(.headRefOid)"',
                check=False,
            )
            fields = done.stdout.split()
            if (
                done.returncode == 0
                and len(fields) == 3
                and (fields[0] != "OPEN" or fields[2] != data["identity"][0])
            ):
                _clear(folder, path)


def _identity(worktree: Path, head: str, branch_name: str) -> list[str]:
    local = gate.command("git", "-C", str(worktree), "rev-parse", "HEAD", check=False)
    branch = gate.command("git", "-C", str(worktree), "branch", "--show-current", check=False)
    status = gate.command("git", "-C", str(worktree), "status", "--porcelain", check=False)
    checkouts = gate.command(
        "git", "-C", str(worktree.parents[2]), "worktree", "list", "--porcelain", check=False
    )
    competing = "\n\n".join(
        block
        for block in checkouts.stdout.split("\n\n")
        if f"branch refs/heads/{branch_name}" in block.splitlines()
    )
    return [
        head,
        local.stdout.strip() if local.returncode == 0 else "",
        branch.stdout,
        status.stdout,
        competing,
    ]


def start(
    folder: Path, number: str, head: str, next_round: int, sent_back: int, branch_name: str
) -> int:
    """Return 0 for started, 2 for an unchanged block, 1 for a new failed start, and 75 when
    memory is short and the start waits for the next tick."""
    allowance = folder / f"review-{number}.allow-round"
    blocked = folder / f"review-{number}.allowance-notice"
    if sent_back >= 2 and (
        not allowance.exists() or allowance.read_text().strip() != str(next_round)
    ):
        _notice(
            folder,
            blocked,
            [head, str(next_round)],
            number,
            f"needs the owner's allowance for round {next_round}",
        )
        return 2
    _clear(folder, blocked)
    worktree = gate.CONFIG.checkout_of(folder) / ".claude" / "worktrees" / f"ticket-{number}"
    identity = _identity(worktree, head, branch_name)
    refused = folder / f"review-{number}.start-refused"
    if refused.exists() and json.loads(refused.read_text())["identity"] == identity:
        data = json.loads(refused.read_text())
        _notice(folder, refused, identity, number, data["reason"], data["blocked"])
        if data["blocked"]:
            return 2
    else:
        _clear(folder, refused)
    done = gate.command("bash", str(gate.HERE / "session"), "start", "review", number, check=False)
    if done.returncode == 0:
        _clear(folder, refused)
        return 0
    if done.returncode == 75:
        print(done.stderr or done.stdout, flush=True)
        return 75
    # Preparation can create/advance the local branch before refusing it.
    identity = _identity(worktree, head, branch_name)
    _notice(
        folder,
        refused,
        identity,
        number,
        f"reviewer start refused: {(done.stderr or done.stdout).strip()}",
        blocked=done.returncode == 3,
    )
    return 1


def would_start(
    folder: Path, number: str, head: str, next_round: int, sent_back: int, branch_name: str
) -> int:
    """What `start` would do, for a shadow run, which writes nothing: 2 for a block it keeps
    unchanged, 0 when it would start the reviewer."""
    allowance = folder / f"review-{number}.allow-round"
    if sent_back >= 2 and (
        not allowance.exists() or allowance.read_text().strip() != str(next_round)
    ):
        return 2
    refused = folder / f"review-{number}.start-refused"
    if refused.exists():
        worktree = gate.CONFIG.checkout_of(folder) / ".claude" / "worktrees" / f"ticket-{number}"
        data = json.loads(refused.read_text())
        if data["blocked"] and data["identity"] == _identity(worktree, head, branch_name):
            return 2
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--shadow":
        arguments = sys.argv[2:]
        sys.exit(
            would_start(
                Path(arguments[0]),
                arguments[1],
                arguments[2],
                int(arguments[3]),
                int(arguments[4]),
                arguments[5],
            )
        )
    if sys.argv[1] == "--started":
        _clear(Path(sys.argv[2]), Path(sys.argv[2]) / f"review-{sys.argv[3]}.start-refused")
        sys.exit(0)
    if sys.argv[1] == "--reconcile":
        reconcile(Path(sys.argv[2]))
        sys.exit(0)
    sys.exit(
        start(
            Path(sys.argv[1]),
            sys.argv[2],
            sys.argv[3],
            int(sys.argv[4]),
            int(sys.argv[5]),
            sys.argv[6],
        )
    )
