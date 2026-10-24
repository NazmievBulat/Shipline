"""The tick starts the next draft ticket itself, by priority and age, apart from running files."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from workflow_harness import Workflow

_WORKFLOW = Path(__file__).resolve().parents[2] / "scripts/workflow"

# The model stand-in answers a ticket's question with `answer-<n>` when its description names
# `TICKET-<n>`, and counts the questions in `model-calls`. Other `claude` calls keep the usual
# stand-in.
_MODEL = """#!/bin/bash
if [[ $1 == -p && " $* " == *" --tools  "* ]]; then
  number=$(grep -o 'TICKET-[0-9]*' <<<"${@: -1}" | head -n 1)
  number=${number#TICKET-}
  echo "$number" >> "@STATE@/model-calls"
  [ -f "@STATE@/answer-$number" ] || exit 1
  cat "@STATE@/answer-$number"
  exit 0
fi
exec bash "@STATE@/claude-usual" "$@"
"""


def _start_now() -> int:
    """A moment near the real clock, so the usage figures a test writes are fresh."""
    return int(time.time())


def _draft(
    workflow: Workflow,
    number: int,
    answer: str,
    *,
    labels: tuple[str, ...] = (),
    body: str | None = None,
) -> None:
    """A draft ticket without the label, whose description the model answers with `answer`."""
    workflow.add_pull_request(number, labels=labels)
    (workflow.state / f"body-{number}").write_text(body or f"Change TICKET-{number}.")
    (workflow.state / f"answer-{number}").write_text(answer + "\n")


def _model(workflow: Workflow) -> None:
    usual = workflow.bin / "claude"
    (workflow.state / "claude-usual").write_text(usual.read_text())
    workflow.fake("claude", _MODEL)


def _notes(workflow: Workflow) -> Path:
    folder = workflow.repo / ".local" / "workflow"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _room_since(workflow: Workflow, moment: int, minutes: int = 10) -> None:
    """The ticks before this one found room for `minutes`, the last one 3 minutes ago."""
    (_notes(workflow) / "room").write_text(f"{moment - minutes * 60} {moment - 180}\n")


_MACHINE = (
    "waits: the machine (a start needs 10 minutes of room, 5 minutes since the last start and "
    "the usage window)"
)


def _tick(workflow: Workflow, moment: int) -> str:
    """What the tick printed."""
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(moment)})
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _labelled(workflow: Workflow) -> list[str]:
    path = workflow.state / "labelled"
    return path.read_text().split() if path.exists() else []


def _told(workflow: Workflow) -> list[str]:
    """The lines noted for the wake, in order."""
    pending = _notes(workflow) / "ticket-starts.pending"
    if not pending.exists():
        return []
    return [
        line
        for event in sorted(pending.glob("*.json"))
        for line in json.loads(event.read_text())["lines"]
    ]


def _model_calls(workflow: Workflow) -> list[str]:
    path = workflow.state / "model-calls"
    return path.read_text().split() if path.exists() else []


def _push_change(workflow: Workflow, number: int, path: str) -> None:
    """The ticket's branch changes one file, as after a worker's push."""
    clone = workflow.root / f"clone-{number}"
    workflow.git(
        "clone",
        "--quiet",
        "--branch",
        workflow.branch(number),
        workflow.git("remote", "get-url", "origin"),
        str(clone),
    )
    (clone / path).parent.mkdir(parents=True, exist_ok=True)
    (clone / path).write_text("changed\n")
    workflow.git("add", path, cwd=clone)
    workflow.git("commit", "--quiet", "-m", "feat: a change", cwd=clone)
    workflow.git("push", "--quiet", "origin", workflow.branch(number), cwd=clone)
    workflow.set_pull_request(number, "OPEN", workflow.git("rev-parse", "HEAD", cwd=clone))


def test_two_drafts_that_share_a_file_start_one_after_the_other(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "scripts/workflow/tick")
    _draft(workflow, 8, "scripts/workflow")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == ["7"]
    assert "started 7 (priority medium)" in _told(workflow)

    # The next tick starts the worker of the labelled draft. 8 needs a directory 7 changes: it
    # waits however long the machine has room.
    for later in (moment + 180, moment + 360, moment + 540):
        _tick(workflow, later)
    assert workflow.is_running("ticket-7")
    assert _labelled(workflow) == ["7"]
    assert "8 waits: shares scripts/workflow/tick with 7" in _told(workflow)

    workflow.set_pull_request(7, "MERGED", workflow.git("rev-parse", "origin/main"))
    _tick(workflow, moment + 720)
    _tick(workflow, moment + 900)

    assert _labelled(workflow) == ["7", "8"]
    assert workflow.is_running("ticket-8")


def test_no_ticket_is_labelled_in_a_tick_that_started_another_session(
    workflow: Workflow,
) -> None:
    """The tick's own start counts: the 5 minutes after it let its memory and load show."""
    _model(workflow)
    workflow.add_pull_request(5)
    (workflow.state / "body-5").write_text("Change TICKET-5.")
    (workflow.state / "answer-5").write_text("web\n")
    _draft(workflow, 8, "src")
    moment = _start_now()
    _room_since(workflow, moment)

    printed = _tick(workflow, moment)

    assert workflow.is_running("ticket-5")
    assert _labelled(workflow) == []
    assert "8 waits: a session started in the last 5 minutes" in printed


def test_a_wait_is_told_once_until_it_changes(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "src", labels=("workflow",))
    _draft(workflow, 8, "src/app.py")
    moment = _start_now()
    _room_since(workflow, moment)

    for later in (moment, moment + 180, moment + 360):
        _tick(workflow, later)

    assert _told(workflow) == ["8 waits: shares src/app.py with 7"]


def test_the_highest_priority_goes_first_then_the_older_ticket(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    _draft(workflow, 8, "src")
    _draft(workflow, 9, "docs", labels=("priority:+high",))
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == ["9"]
    assert "started 9 (priority high)" in _told(workflow)


def test_a_start_plan_line_is_obeyed(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    _draft(workflow, 8, "src")
    _draft(workflow, 9, "docs", labels=("workflow",))
    workflow.running("ticket-9")
    (_notes(workflow) / "start-plan").write_text("# the owner's order\n7 after 9  # 9 first\n")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == ["8"]
    assert "7 waits: 7 after 9 in start-plan" in _told(workflow)

    # Once 9 is merged, 7 waits for nothing.
    workflow.set_pull_request(9, "MERGED", workflow.git("rev-parse", "origin/main"))
    workflow.set_pull_request(8, "MERGED", workflow.git("rev-parse", "origin/main"))
    _tick(workflow, moment + 360)

    assert _labelled(workflow) == ["8", "7"]


def test_a_start_plan_line_not_understood_holds_every_start(workflow: Workflow) -> None:
    """A mistyped hold must not let its ticket start."""
    _model(workflow)
    _draft(workflow, 7, "web")
    (_notes(workflow) / "start-plan").write_text("Hold 7\n")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert _told(workflow) == ["automatic starts are held: start-plan line not understood: Hold 7"]


def test_a_hold_is_obeyed(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    _draft(workflow, 8, "src")
    (_notes(workflow) / "start-plan").write_text("hold 7\n")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == ["8"]
    assert "7 waits: hold 7 in start-plan" in _told(workflow)


def test_hold_all_stops_every_automatic_start(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    (_notes(workflow) / "start-plan").write_text("hold all\n")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert _told(workflow) == ["automatic starts are held: hold all in start-plan"]


def test_nothing_starts_while_the_load_is_over_the_ceiling(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)
    # Four processors: the ceiling is a 5-minute load of 6.
    workflow.loadavg_file.write_text("6.50 6.40 6.30 1/100 1234\n")

    printed = _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert "7 waits: memory or load is over the ceilings" in printed
    assert f"7 {_MACHINE}" in _told(workflow)
    # The minutes of room count again from the next tick that finds room.
    assert not (_notes(workflow) / "room").exists()


def test_memory_and_load_must_have_had_room_for_ten_minutes(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    moment = _start_now()

    for later in (moment, moment + 180, moment + 360, moment + 540):
        printed = _tick(workflow, later)
        assert (
            "7 waits: memory and load have been under the ceilings for less than 10 minutes"
            in printed
        )
    assert _labelled(workflow) == []
    # Told once: the wait did not change while the minutes went by.
    assert _told(workflow) == [f"7 {_MACHINE}"]

    _tick(workflow, moment + 600)

    assert _labelled(workflow) == ["7"]


def test_a_gap_between_two_looks_starts_the_count_again(workflow: Workflow) -> None:
    """A tick that did not run for a while does not know whether the machine had room."""
    _model(workflow)
    _draft(workflow, 7, "web")
    moment = _start_now()
    (_notes(workflow) / "room").write_text(f"{moment - 3600} {moment - 420}\n")

    _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert (_notes(workflow) / "room").read_text().split() == [str(moment), str(moment)]


def test_nothing_starts_within_five_minutes_of_the_last_start(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)
    (_notes(workflow) / "last-start").write_text(f"{moment - 299}\n")

    printed = _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert "7 waits: a session started in the last 5 minutes" in printed

    _tick(workflow, moment + 180)

    assert _labelled(workflow) == ["7"]


def test_a_start_by_session_start_counts_as_the_last_start(workflow: Workflow) -> None:
    workflow.add_pull_request(5, labels=("workflow",))
    moment = _start_now()

    _tick(workflow, moment)

    assert workflow.is_running("ticket-5")
    assert (_notes(workflow) / "last-start").read_text().split() == [str(moment)]


def test_nothing_starts_above_85_percent_of_the_usage_window(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)
    workflow.usage(90, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(moment + 7200)))

    printed = _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert "7 waits: the 5-hour usage window is 90% used" in printed


def test_a_ticket_with_a_diff_uses_the_diff_without_a_model_call(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    _push_change(workflow, 7, "src/example_app/app.py")

    _tick(workflow, _start_now())

    assert _model_calls(workflow) == []
    notes = _notes(workflow)
    assert (notes / "files-7").read_text() == "src/example_app/app.py\n"
    assert (notes / "files-7.source").read_text().split()[0] == "diff"


def test_the_files_heading_of_a_description_is_used_without_a_model_call(
    workflow: Workflow,
) -> None:
    _model(workflow)
    body = "Change TICKET-7.\n\n## Files\n\n- `web/src/app.tsx`\n- scripts/workflow/tick\n"
    _draft(workflow, 7, "docs", body=body + "\n## Out of scope\n\n- `docs/`\n")

    _tick(workflow, _start_now())

    assert _model_calls(workflow) == []
    files = (_notes(workflow) / "files-7").read_text().split()
    assert files == ["web/src/app.tsx", "scripts/workflow/tick"]


def test_the_model_is_asked_once_per_ticket_until_the_description_changes(
    workflow: Workflow,
) -> None:
    _model(workflow)
    _draft(workflow, 7, "web/src\n- `../outside`\n/etc/passwd")
    moment = _start_now()

    _tick(workflow, moment)
    _tick(workflow, moment + 180)

    assert _model_calls(workflow) == ["7"]
    assert (_notes(workflow) / "files-7").read_text() == "web/src\n"

    (workflow.state / "body-7").write_text("Change TICKET-7, and the settings page too.")
    _tick(workflow, moment + 360)

    assert _model_calls(workflow) == ["7", "7"]


def test_a_failed_answer_holds_the_ticket_and_is_asked_again_after_half_an_hour(
    workflow: Workflow,
) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    (workflow.state / "answer-7").unlink()
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)
    _tick(workflow, moment + 180)

    assert _model_calls(workflow) == ["7"]
    assert _labelled(workflow) == []
    assert "7 waits: its files are not known yet" in _told(workflow)

    _tick(workflow, moment + 1800)

    assert _model_calls(workflow) == ["7", "7"]


def test_tickets_without_an_answer_leave_the_model_calls_to_later_tickets(
    workflow: Workflow,
) -> None:
    _model(workflow)
    for number in (7, 8, 9):
        _draft(workflow, number, "web")
        (workflow.state / f"answer-{number}").unlink()
    _draft(workflow, 10, "src")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)
    assert _model_calls(workflow) == ["7", "8", "9"]
    _tick(workflow, moment + 180)

    assert _model_calls(workflow) == ["7", "8", "9", "10"]
    assert _labelled(workflow) == ["10"]


def test_cloud_and_owner_only_tickets_are_not_started(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web", body="Change TICKET-7.\n\nWorker: cloud\n")
    _draft(workflow, 8, "backend", body="Change TICKET-8.\n\nStart: owner only\n")
    _draft(workflow, 9, "docs")
    (_notes(workflow) / "cloud-9").write_text("{}\n")
    _draft(workflow, 10, "scripts")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == ["10"]


def test_a_draft_built_on_another_pull_request_waits_for_its_base(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    _draft(workflow, 8, "backend")
    (workflow.state / "base-branch-7").write_text(workflow.branch(8))
    moment = _start_now()
    _room_since(workflow, moment)
    (_notes(workflow) / "start-plan").write_text("hold 8\n")

    _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert "7 waits: built on 8, not on main" in _told(workflow)


def test_an_audited_pull_request_holds_the_files_it_changes(workflow: Workflow) -> None:
    """An audit's pull request has no label, and its session still runs."""
    _model(workflow)
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, "web/src/app.tsx")
    workflow.running("audit-6")
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert "7 waits: shares web/src/app.tsx with 6" in _told(workflow)


def test_a_running_ticket_whose_new_head_cannot_be_fetched_holds_every_draft(
    workflow: Workflow,
) -> None:
    """The files kept for an older head may miss the file the new head shares with a draft."""
    _model(workflow)
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, "docs/notes.md")
    workflow.running("audit-6")
    _draft(workflow, 7, "web")
    moment = _start_now()
    # No room yet: this tick only finds the files of 6 at its first head.
    _tick(workflow, moment)
    assert (_notes(workflow) / "files-6").read_text() == "docs/notes.md\n"

    # 6 pushes a change to the folder 7 touches, then its branch cannot be fetched.
    clone = workflow.root / "clone-6"
    (clone / "web/src").mkdir(parents=True)
    (clone / "web/src/app.tsx").write_text("changed\n")
    workflow.git("add", "web/src/app.tsx", cwd=clone)
    workflow.git("commit", "--quiet", "-m", "feat: another change", cwd=clone)
    workflow.set_pull_request(6, "OPEN", workflow.git("rev-parse", "HEAD", cwd=clone))
    workflow.git("push", "--quiet", "origin", "--delete", workflow.branch(6), cwd=clone)
    _room_since(workflow, moment + 180)

    _tick(workflow, moment + 180)

    assert _labelled(workflow) == []
    assert "7 waits: the files of 6 are not known yet" in _told(workflow)


def test_a_running_ticket_whose_head_was_never_fetched_holds_every_draft(
    workflow: Workflow,
) -> None:
    """Its description may name less than its diff changes: the diff must be read first."""
    _model(workflow)
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, "web/src/app.tsx")
    (workflow.state / "body-6").write_text("Change TICKET-6.")
    (workflow.state / "answer-6").write_text("docs\n")
    workflow.git("push", "--quiet", "origin", "--delete", workflow.branch(6))
    workflow.running("audit-6")
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert "7 waits: the files of 6 are not known yet" in _told(workflow)
    assert "6" not in _model_calls(workflow)


def test_the_machine_is_looked_at_again_after_finding_the_files(workflow: Workflow) -> None:
    """The model calls take minutes: a load that rose meanwhile still holds the start."""
    _model(workflow)
    model = (workflow.bin / "claude").read_text()
    workflow.fake(
        "claude",
        model.replace(
            "#!/bin/bash\n",
            f"#!/bin/bash\necho '6.50 6.40 6.30 1/100 1234' > {workflow.loadavg_file}\n",
            1,
        ),
    )
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)

    printed = _tick(workflow, moment)

    assert _model_calls(workflow) == ["7"]
    assert _labelled(workflow) == []
    assert "7 waits: memory or load is over the ceilings" in printed
    assert not (_notes(workflow) / "room").exists()


def _train_numbers(notes: Path) -> str:
    """What the starter reads of the merge train, run as the tick runs it: from its folder."""
    code = (
        f"import sys; sys.path.insert(0, {str(_WORKFLOW)!r}); import ticket_starts, pathlib; "
        f"print(sorted(ticket_starts.train_numbers(pathlib.Path({str(notes)!r}))))"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_the_waiting_members_of_the_merge_train_are_read(tmp_path: Path) -> None:
    (tmp_path / "merge-train.json").write_text(json.dumps({"waiting": ["6", 9], "fixes": []}))

    assert _train_numbers(tmp_path) == "['6', '9']"
    (tmp_path / "merge-train.json").write_text("not json")
    assert _train_numbers(tmp_path) == "[]"


def test_ticket_files_says_what_the_tick_found(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web/src/pages")
    _tick(workflow, _start_now())

    done = workflow.run_script("ticket-files", "7")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "web/src/pages\n"
    assert _model_calls(workflow) == ["7"]


def test_ticket_files_finds_the_files_of_a_ticket_the_tick_has_not_seen(
    workflow: Workflow,
) -> None:
    _model(workflow)
    _draft(workflow, 7, "web/src/pages")

    done = workflow.run_script("ticket-files", "7")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "web/src/pages\n"
    assert _model_calls(workflow) == ["7"]


def test_a_draft_starts_beside_a_ready_train_member_without_a_session(
    workflow: Workflow,
) -> None:
    """The tick can start work beside a reviewed pull request waiting only for its merge."""
    _model(workflow)
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, "web/src/app.tsx")
    (_notes(workflow) / "merge-train.json").write_text(json.dumps({"waiting": ["6"]}))
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)

    # The stand-in cannot answer what the merge gate asks of the train, so the tick reports a
    # failure there; the pick runs all the same.
    workflow.run_script("tick", env={"WORKFLOW_NOW": str(moment)})

    assert _labelled(workflow) == ["7"]
    assert _told(workflow) == ["started 7 (priority medium)"]


@pytest.mark.parametrize(
    ("in_train", "draft", "labelled", "session", "cloud", "starts"),
    [
        (True, False, True, None, False, True),
        (True, False, True, "review", False, False),
        (True, False, True, "ticket", False, False),
        (True, False, True, "codex", False, False),
        (True, False, True, "audit", False, False),
        (True, False, False, None, True, False),
        (True, True, True, None, False, False),
        (True, True, False, None, False, False),
        (False, False, False, "ticket", False, False),
        (False, False, True, None, False, False),
    ],
)
def test_only_ready_train_members_without_sessions_release_their_files(
    workflow: Workflow,
    in_train: bool,
    draft: bool,
    labelled: bool,
    session: str | None,
    cloud: bool,
    starts: bool,
) -> None:
    """Exercise pick without the tick starting a new reviewer before it reads the sessions."""
    _model(workflow)
    workflow.add_pull_request(6, draft=draft, labels=("workflow",) if labelled else ())
    _push_change(workflow, 6, "web/src/app.tsx")
    notes = _notes(workflow)
    if in_train:
        (notes / "merge-train.json").write_text(json.dumps({"waiting": ["6"]}))
    if session:
        workflow.running(f"{session}-6")
    if cloud:
        (notes / "cloud-6").write_text("{}\n")
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)
    snapshot = workflow.root / "snapshot.json"
    for arguments in (
        ["tick_snapshot.py", "create", str(snapshot)],
        ["ticket_starts.py", "pick", str(workflow.repo), str(snapshot)],
    ):
        done = subprocess.run(
            [sys.executable, "-B", str(_WORKFLOW / arguments[0]), *arguments[1:]],
            cwd=workflow.repo,
            env={**workflow.env, "WORKFLOW_NOW": str(moment)},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr

    assert _labelled(workflow) == (["7"] if starts else [])
    assert _told(workflow) == (
        ["started 7 (priority medium)"]
        if starts
        else ["7 waits: shares web/src/app.tsx with 6"]
    )


def test_a_running_ticket_whose_files_are_not_known_holds_every_draft(
    workflow: Workflow,
) -> None:
    _model(workflow)
    _draft(workflow, 6, "docs", labels=("workflow",))
    (workflow.state / "answer-6").unlink()
    workflow.running("ticket-6")
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)

    _tick(workflow, moment)

    assert _labelled(workflow) == []
    assert "7 waits: the files of 6 are not known yet" in _told(workflow)


def test_a_route_folder_in_brackets_is_one_of_the_files(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "docs")
    _push_change(workflow, 7, "web/src/app/orders/[orderId]/page.tsx")

    _tick(workflow, _start_now())

    files = (_notes(workflow) / "files-7").read_text()
    assert files == "web/src/app/orders/[orderId]/page.tsx\n"


def test_a_failed_label_is_not_a_start_and_is_tried_again(workflow: Workflow) -> None:
    _model(workflow)
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)
    (workflow.state / "fail-gh-label").touch()

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(moment)})

    assert done.returncode != 0
    assert "cannot add the label workflow to 7" in done.stderr
    assert not (_notes(workflow) / "last-start").exists()
    assert not any(line.startswith("started") for line in _told(workflow))

    (workflow.state / "fail-gh-label").unlink()
    _tick(workflow, moment + 180)

    assert _labelled(workflow) == ["7"]


def _helper(expression: str) -> str:
    """A helper of the starter, run as the tick runs it: from its folder."""
    code = (
        f"import sys; sys.path.insert(0, {str(_WORKFLOW)!r}); import ticket_starts; "
        f"print(repr(ticket_starts.{expression}))"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_a_path_outside_the_repository_or_a_sentence_is_not_a_file() -> None:
    assert _helper("clean_path('../outside')") == "None"
    assert _helper("clean_path('apps/../../etc')") == "None"
    assert _helper("clean_path('/etc/passwd')") == "None"
    assert _helper("clean_path('the web app')") == "None"
    assert _helper("clean_path('- `web/src/app/(public)/page.tsx`')") == (
        "'web/src/app/(public)/page.tsx'"
    )
    assert _helper("clean_path('1. scripts/workflow/tick:42')") == "'scripts/workflow/tick'"
    assert _helper("clean_path('src/**')") == "'src'"


def test_a_folder_holds_only_the_paths_inside_it() -> None:
    assert _helper("overlap('web', 'webapp')") == "None"
    assert _helper("overlap('web', 'web/src/a.ts')") == "'web/src/a.ts'"
    assert _helper("overlap('web/src/a.ts', 'web')") == "'web/src/a.ts'"
    # The repository itself, as a ticket that changes everything names it.
    assert _helper("clean_path('./')") == "'.'"
    assert _helper("overlap('.', 'web/src/a.ts')") == "'web/src/a.ts'"
    assert _helper("overlap('web', '.')") == "'web'"


def test_the_wake_tells_the_start_lines_once(workflow: Workflow) -> None:
    pending = _notes(workflow) / "ticket-starts.pending"
    pending.mkdir()
    (pending / "1-1.json").write_text(
        json.dumps({"lines": ["started 9 (priority high)", "8 waits: shares docs with 9"]})
    )
    workflow.orchestrator("idle")
    night = {"WORKFLOW_NOW": "1791932400", "TZ": "UTC"}  # 13 October, 23:00

    done = workflow.run_script("wake", env=night)

    assert done.returncode == 0, done.stderr
    [prompt] = workflow.sent()
    assert (
        "Ticket starts by the tick: started 9 (priority high); 8 waits: shares docs with 9."
        in prompt
    )
    assert not list(pending.glob("*.json"))


def test_a_start_line_alone_is_a_change_for_the_check_in(workflow: Workflow) -> None:
    """Without it, a wait told when nothing else changed would wait for some other change."""
    workflow.orchestrator("idle")
    first = {"WORKFLOW_NOW": "1791932400", "TZ": "UTC"}  # 13 October, 23:00
    later = {"WORKFLOW_NOW": "1791934200", "TZ": "UTC"}  # 23:30
    assert workflow.run_script("wake", env=first).returncode == 0
    assert workflow.run_script("wake", env=later).returncode == 0
    assert len(workflow.sent()) == 1  # nothing changed, nothing told

    pending = _notes(workflow) / "ticket-starts.pending"
    pending.mkdir()
    (pending / "1-1.json").write_text(json.dumps({"lines": ["started 9 (priority high)"]}))
    at_midnight = {"WORKFLOW_NOW": "1791936000", "TZ": "UTC"}

    done = workflow.run_script("wake", env=at_midnight)

    assert done.returncode == 0, done.stderr
    assert len(workflow.sent()) == 2
    assert "Ticket starts by the tick: started 9 (priority high)." in workflow.sent()[-1]


def _hot(workflow: Workflow, *paths: str) -> None:
    """The repository's `shipline.json` names these hot paths, beside what it named."""
    config = workflow.repo / "shipline.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "hot_paths": list(paths)}))


def _pick(workflow: Workflow, moment: int) -> None:
    """One pick, as the tick runs it, without the tick's other starts."""
    snapshot = workflow.root / "snapshot.json"
    scripts = workflow.script.parent
    for arguments in (
        ["tick_snapshot.py", "create", str(snapshot)],
        ["ticket_starts.py", "pick", str(workflow.repo), str(snapshot)],
    ):
        done = subprocess.run(
            [sys.executable, "-B", str(scripts / arguments[0]), *arguments[1:]],
            cwd=workflow.repo,
            env={**workflow.env, "WORKFLOW_NOW": str(moment)},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("hot", [True, False], ids=["hot path", "no hot paths"])
def test_two_tickets_on_one_hot_path_never_run_at_once(workflow: Workflow, hot: bool) -> None:
    _model(workflow)
    if hot:
        _hot(workflow, "docs/*.md", "web/src/pages/**")
    _draft(workflow, 6, "web/src/pages/menu/a.tsx", labels=("workflow",))
    workflow.running("ticket-6")
    # Another file than 6's: only the hot path holds it.
    _draft(workflow, 7, "web/src/pages/settings/b.tsx")
    moment = _start_now()
    _room_since(workflow, moment)

    _pick(workflow, moment)

    assert _labelled(workflow) == ([] if hot else ["7"])
    assert _told(workflow) == (
        ["7 waits: hot path web/src/pages/** with 6"] if hot else ["started 7 (priority medium)"]
    )


@pytest.mark.parametrize("hot", [True, False], ids=["hot path", "no hot paths"])
def test_a_pull_request_only_waiting_in_the_merge_line_holds_its_hot_path(
    workflow: Workflow, hot: bool
) -> None:
    """A ready member without a session still runs for a hot path: its squash is to come."""
    _model(workflow)
    if hot:
        _hot(workflow, "web/src/pages/**")
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, "web/src/pages/menu/a.tsx")
    (_notes(workflow) / "merge-train.json").write_text(json.dumps({"waiting": ["6"]}))
    _draft(workflow, 7, "web/src/pages/settings/b.tsx")
    _draft(workflow, 8, "src/app.py")
    moment = _start_now()
    _room_since(workflow, moment)

    _pick(workflow, moment)

    # A draft outside the hot path starts beside it, as before.
    assert _labelled(workflow) == (["8"] if hot else ["7"])
    assert _told(workflow) == (
        ["started 8 (priority medium)", "7 waits: hot path web/src/pages/** with 6"]
        if hot
        else ["started 7 (priority medium)"]
    )


def test_a_folder_a_ticket_names_holds_the_hot_paths_of_the_files_in_it(
    workflow: Workflow,
) -> None:
    _model(workflow)
    _hot(workflow, "web/src/pages/*.tsx")
    (workflow.repo / "web/src/pages").mkdir(parents=True)
    (workflow.repo / "web/src/pages/menu.tsx").write_text("menu\n")
    workflow.git("add", "web")
    workflow.git("commit", "--quiet", "-m", "feat: the menu")
    workflow.git("push", "--quiet", "origin", "main")
    workflow.add_pull_request(6, draft=False, labels=())
    _push_change(workflow, 6, "web/src/pages/menu.tsx")
    (_notes(workflow) / "merge-train.json").write_text(json.dumps({"waiting": ["6"]}))
    _draft(workflow, 7, "web")
    moment = _start_now()
    _room_since(workflow, moment)

    _pick(workflow, moment)

    assert _labelled(workflow) == []
    assert _told(workflow) == ["7 waits: hot path web/src/pages/*.tsx with 6"]


def test_the_files_of_a_merge_line_member_must_be_known_for_a_hot_path_only(
    workflow: Workflow,
) -> None:
    _model(workflow)
    _hot(workflow, "web/src/pages/**")
    # A ready member whose branch was never pushed: its files cannot be read.
    workflow.add_pull_request(6, draft=False, labels=())
    (workflow.state / "pr/6").write_text(f"OPEN ticket/gone {'0' * 40}\n")
    (_notes(workflow) / "merge-train.json").write_text(json.dumps({"waiting": ["6"]}))
    _draft(workflow, 7, "web/src/pages/settings/b.tsx")
    _draft(workflow, 8, "src/app.py")
    moment = _start_now()
    _room_since(workflow, moment)

    _pick(workflow, moment)

    assert _labelled(workflow) == ["8"]
    assert "7 waits: the files of 6 are not known yet" in _told(workflow)


def test_a_hot_path_wait_is_told_once_until_it_changes(workflow: Workflow) -> None:
    _model(workflow)
    _hot(workflow, "web/src/pages/**")
    _draft(workflow, 6, "web/src/pages/a.tsx", labels=("workflow",))
    workflow.running("ticket-6")
    _draft(workflow, 7, "web/src/pages/b.tsx")
    moment = _start_now()
    _room_since(workflow, moment)

    for later in (moment, moment + 180, moment + 360):
        _pick(workflow, later)

    assert _told(workflow) == ["7 waits: hot path web/src/pages/** with 6"]
