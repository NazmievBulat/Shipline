"""Breaking tests for pull request 71: a transient failure of the tick's refresh push waits.

Each test replays the daily flow with neutral names: pull request 7 is handed off green, main
moves, the tick merges main into 7's kept worktree and pushes, and that push meets what a real
network or host says. Pull request 8 waits behind 7 throughout.
"""

import json
import stat

import pytest
from workflow.test_merge_driver import (
    GitHub,
    _folder,
    _note,
    _stale_refresh,
    _started,
    _train,
)
from workflow.test_merge_driver import (
    github as github_fixture,
)
from workflow.test_merge_driver import (
    handed as handed_fixture,
)

github = github_fixture
handed = handed_fixture


def _fail_pushes(workflow, failures: list[str], *, accepted: bool = False) -> None:
    """The next refresh pushes fail with these whole outputs, one per push, in order.

    `accepted`: the host took the push before the connection dropped."""
    queue = workflow.state / "push-failures"
    queue.mkdir(exist_ok=True)
    for index, failure in enumerate(failures):
        (queue / f"{index:03}").write_text(failure + "\n")
    if accepted:
        (workflow.state / "push-accepted").touch()
    workflow.fake(
        "git",
        """#!/bin/bash
if [[ $1 == push ]]; then
  next=$(ls "@STATE@/push-failures" 2>/dev/null | head -n 1)
  if [[ -n $next ]]; then
    if [[ -e "@STATE@/push-accepted" ]]; then /usr/bin/git "$@" >/dev/null 2>&1 || exit; fi
    cat "@STATE@/push-failures/$next" >&2
    rm "@STATE@/push-failures/$next"
    exit 1
  fi
fi
exec /usr/bin/git "$@"
""",
    )


def _remote(workflow) -> str:
    return workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]


def _local(workflow) -> str:
    return workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))


def _move_main(github: GitHub, message: str) -> str:
    """Another pull request merged: main moves on the origin. Returns the old main."""
    workflow = github.workflow
    old = github.base
    workflow.git("fetch", "origin", "main")
    workflow.git("merge", "--ff-only", "origin/main")
    workflow.git("commit", "--allow-empty", "-m", message)
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main()
    github.save()
    return old


def _publish_head(github: GitHub, head: str) -> None:
    github.head = head
    github.pulls["7"]["headRefOid"] = head
    (github.workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")
    github.save()


def _assert_waited(workflow, tick) -> None:
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert "merge hand-back 7" not in tick.stdout, tick.stdout + tick.stderr
    assert _note(workflow).exists()
    assert not list(_folder(workflow).glob("merge-review-*"))
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert _started(workflow) == []


# What git prints when a push meets a network or host failure on a real machine. Each is a
# whole push output, as the push helper collects it.
TRANSIENT_OUTPUTS = {
    # The host dropped the connection while the pack was sent, over HTTP/2.
    "http2-stream-cancel": (
        "error: RPC failed; curl 92 HTTP/2 stream 7 was not closed cleanly: CANCEL (err 8)\n"
        "send-pack: unexpected disconnect while reading sideband packet\n"
        "fatal: the remote end hung up unexpectedly\n"
        "Everything up-to-date"
    ),
    # A TLS reset in the middle of the push.
    "tls-reset": (
        "error: RPC failed; curl 56 OpenSSL SSL_read: SSL_ERROR_SYSCALL, errno 104\n"
        "send-pack: unexpected disconnect while reading sideband packet\n"
        "fatal: the remote end hung up unexpectedly"
    ),
    "server-error": (
        "remote: Internal Server Error\n"
        "error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
        "send-pack: unexpected disconnect while reading sideband packet\n"
        "fatal: the remote end hung up unexpectedly"
    ),
    # The machine lost its network for a moment: the name did not resolve.
    "dns": (
        "fatal: unable to access 'https://github.com/example-org/example-app.git/': "
        "Could not resolve host: github.com"
    ),
    # The machine had no route for a moment.
    "connect-refused": (
        "fatal: unable to access 'https://github.com/example-org/example-app.git/': "
        "Failed to connect to github.com port 443 after 21 ms: Couldn't connect to server"
    ),
    # A TLS stream cut short with nothing else said.
    "gnutls-cut": (
        "error: RPC failed; curl 56 GnuTLS recv error (-110): "
        "The TLS connection was non-properly terminated.\n"
        "send-pack: unexpected disconnect while reading sideband packet\n"
        "fatal: the remote end hung up unexpectedly"
    ),
}


# A machine that loses its network for a moment says only this: no hung-up line follows.
BROKEN = {"dns", "connect-refused"}


@pytest.mark.parametrize("kind", [
    pytest.param(
        kind,
        marks=pytest.mark.xfail(
            strict=True, reason="broke: handed back at once, and a fresh reviewer starts"
        ),
    )
    if kind in BROKEN
    else kind
    for kind in sorted(TRANSIENT_OUTPUTS)
])
def test_a_real_network_failure_of_the_refresh_push_waits_one_tick(
    handed: GitHub, kind: str
) -> None:
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, [TRANSIENT_OUTPUTS[kind]])

    first = workflow.run_script("tick")

    _assert_waited(workflow, first)
    refreshed = _local(workflow)
    assert refreshed != handed.head
    assert _remote(workflow) == handed.head

    second = workflow.run_script("tick")

    _assert_waited(workflow, second)
    assert _remote(workflow) == refreshed
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    assert not (_folder(workflow) / "merge-refresh-7.json").exists()


def test_the_saved_refresh_is_private_to_the_machine_user(handed: GitHub) -> None:
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, ["fatal: the remote end hung up unexpectedly"])

    first = workflow.run_script("tick")

    _assert_waited(workflow, first)
    saved = _folder(workflow) / "merge-refresh-7.json"
    assert stat.S_IMODE(saved.stat().st_mode) & 0o077 == 0
    record = json.loads(saved.read_text())
    assert set(record) == {"head", "branch", "round", "commit", "base", "failures"}
    assert record["failures"] == 1
    assert record["commit"] == _local(workflow)


def test_github_still_shows_the_old_head_after_an_accepted_push(handed: GitHub) -> None:
    """The host took the push, the response was lost, and GitHub's pull request still reads the
    old head on the next tick (its head sync lags during the same incident)."""
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, ["fatal: the remote end hung up unexpectedly"], accepted=True)

    first = workflow.run_script("tick")

    _assert_waited(workflow, first)
    refreshed = _local(workflow)
    assert _remote(workflow) == refreshed

    second = workflow.run_script("tick")

    _assert_waited(workflow, second)
    assert _remote(workflow) == refreshed
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    assert _train(workflow)["approved"]["7"] == refreshed


@pytest.mark.xfail(
    strict=True,
    reason="broke: the saved merge is reset and remade on the new main; its push is "
    "rejected as non-fast-forward over the published saved commit and handed back",
)
def test_main_moves_while_github_still_shows_the_old_head_after_an_accepted_push(
    handed: GitHub,
) -> None:
    """As above, and before the next tick a member behind merged, so main moved."""
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, ["fatal: the remote end hung up unexpectedly"], accepted=True)

    first = workflow.run_script("tick")

    _assert_waited(workflow, first)
    published = _local(workflow)
    assert _remote(workflow) == published
    _move_main(handed, "fix: main advanced again")

    second = workflow.run_script("tick")

    _assert_waited(workflow, second)
    # Whatever the tick did, the branch carries no unreviewed work and the handoff follows it.
    head = _remote(workflow)
    assert json.loads(_note(workflow).read_text())["head"] == head


def test_main_moves_after_an_accepted_push_github_reads_the_new_head(handed: GitHub) -> None:
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, ["fatal: the remote end hung up unexpectedly"], accepted=True)

    first = workflow.run_script("tick")

    _assert_waited(workflow, first)
    published = _local(workflow)
    _move_main(handed, "fix: main advanced again")
    _publish_head(handed, published)

    second = workflow.run_script("tick")

    _assert_waited(workflow, second)
    assert "the refreshed branch is published; waiting for its CI" in second.stdout + second.stderr
    assert _remote(workflow) == published
    assert json.loads(_note(workflow).read_text())["head"] == published
    assert not (_folder(workflow) / "merge-refresh-7.json").exists()


def test_a_flapping_host_two_failures_then_success_then_a_new_refresh_counts_afresh(
    handed: GitHub,
) -> None:
    """Many refreshes a day: two failures of one refresh do not count against the next."""
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, ["fatal: the remote end hung up unexpectedly"] * 2)
    for _ in range(2):
        _assert_waited(workflow, workflow.run_script("tick"))
    third = workflow.run_script("tick")
    _assert_waited(workflow, third)
    refreshed = _local(workflow)
    assert _remote(workflow) == refreshed
    _publish_head(handed, refreshed)
    # Its new CI passed, but on a main already old: the next refresh.
    tested = _move_main(handed, "fix: main advanced again")
    handed.ci(handed.base, refreshed, tested_base=tested, run_id=60)
    handed.save()
    _fail_pushes(workflow, ["fatal: read error: Connection reset by peer"] * 2)

    for _ in range(2):
        _assert_waited(workflow, workflow.run_script("tick"))
    last = workflow.run_script("tick")

    _assert_waited(workflow, last)
    again = _local(workflow)
    assert again != refreshed
    assert _remote(workflow) == again


@pytest.mark.xfail(
    strict=True,
    reason="low: the hook's log words make it transient; it waits two ticks, then hands back",
)
def test_a_local_hook_failure_mentioning_a_reset_connection_is_handed_back_at_once(
    handed: GitHub,
) -> None:
    """The branch's own pre-push hook ran its tests on the merge and one failed: its log names
    a reset connection. Nothing about the push was transient."""
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, [(
        "pre-push: running the server tests\n"
        "FAILED tests/test_client.py::test_upload - ConnectionResetError: "
        "[Errno 104] Connection reset by peer\n"
        "1 failed, 120 passed\n"
        "husky - pre-push script failed (code 1)\n"
        "error: failed to push some refs to 'https://github.com/example-org/example-app.git'"
    )])

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge hand-back 7" in done.stdout


@pytest.mark.xfail(
    strict=True, reason="low: a host-side failure worded as a remote rejection hands back at once"
)
def test_the_host_fails_to_write_the_ref(handed: GitHub) -> None:
    """The host's own internal failure while it wrote the branch, reported as a rejection."""
    workflow = handed.workflow
    _stale_refresh(handed)
    _fail_pushes(workflow, [(
        "remote: fatal error in commit_refs\n"
        "To https://github.com/example-org/example-app.git\n"
        " ! [remote rejected] HEAD -> ticket/seven (failure)\n"
        "error: failed to push some refs to 'https://github.com/example-org/example-app.git'"
    )])

    _assert_waited(workflow, workflow.run_script("tick"))
