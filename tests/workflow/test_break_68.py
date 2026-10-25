"""Breaking tests for pull request 68: prefixes run images only for their own image change.

Each test replays a daily flow of the merge line with neutral names: most members change no
image input, a few change one, and main's own image run on a push selects image tests only
when the push changes an image input or does not inherit them. Gate calls repeat as the tick
makes them.
"""

import json
import threading
from typing import Any

from workflow.merge_gate_fixtures import _MAIN_IMAGES, _main_image_run
from workflow.test_merge_when_current import (
    _REPO,
    GitHub,
    _batch_call,
    _batch_setup,
    _finish_batch,
    _merges,
    _prefixes,
    _receipt,
    _scratch,
    _train,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture


def _image_dispatches(github: GitHub) -> int:
    return len(github.workflow.calls("gh workflow run images.yml"))


def _main_push_images(github: GitHub, run_id: int, *, tests: str) -> str:
    """Main's own image run for its current head, as a push to main starts it.

    The planner notes the tested tree; `tests` is the image test job's conclusion, `skipped`
    when the push changed no image input or main inherited the image tests."""
    main = github.workflow.git("rev-parse", "origin/main")
    tree = github.workflow.git("rev-parse", f"{main}^{{tree}}")
    _main_image_run(github, run_id, main)
    jobs = github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"]["jobs"]
    jobs[1].update(name="Docker images: image tests", conclusion=tests)
    github.api[f"{_REPO}/check-runs/{run_id + 1}/annotations?per_page=100"] = [
        {"title": "Files tested", "message": tree}
    ]
    listed = github.api[_MAIN_IMAGES]["workflow_runs"][0]
    github.api[f"{_REPO}/actions/runs/{run_id}"] = listed
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={main}&per_page=100"] = {
        "workflow_runs": [listed]
    }
    return main


def _drive(github: GitHub, numbers: list[str], rounds: int = 10) -> None:
    """Gate calls as the reviewers make them, every prefix finishing green, until each merges."""
    for turn in range(rounds):
        waiting = [n for n in numbers if github.pulls[n]["state"] != "MERGED"]
        if not waiting:
            return
        _finish_batch(github, run_id=10_000 * (turn + 1))
        done = _batch_call(github, waiting[0])
        print(f"gate call {turn}: exit {done.returncode}: {done.stdout.strip().splitlines()[-1:]}")
        main = github.workflow.git("rev-parse", "origin/main")
        if not github.api.get(
            f"{_REPO}/actions/workflows/images.yml/runs?head_sha={main}&per_page=100", {}
        ).get("workflow_runs"):
            # Each squash's push starts main's image run; a neutral squash selects no tests.
            _main_push_images(github, 500_000 + turn * 100, tests="skipped")


def _new_window(github: GitHub) -> None:
    """The line emptied after a merge; the members waiting are tested again on main as it is."""
    folder = github.workflow.repo / ".local/workflow"
    state = json.loads((folder / "merge-train.json").read_text())
    state["prefixes"] = None
    (folder / "merge-train.json").write_text(json.dumps(state) + "\n")


def _neutral_front_merges_alone(github: GitHub, *, push_run: bool) -> None:
    """7 changes no image input and, fresh on main, merges by its own checks. With `push_run`,
    main's image workflow starts a run on that push, whose planner selects no image test."""
    _batch_setup(github)
    github.ci(github.base, github.head, run_id=7000)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7"]
    if push_run:
        _main_push_images(github, 8100, tests="skipped")
    _new_window(github)


def test_neutral_members_after_a_neutral_own_check_merge_still_merge(github: GitHub) -> None:
    """Most days: the front member merges alone by its own checks and changes no image input.
    Main's push image run selects no image tests. The neutral members tested next, on that
    main, must merge as they did before the change, not wait for an image result forever."""
    _neutral_front_merges_alone(github, push_run=True)
    _drive(github, ["8", "9"])
    status = _train(github)
    assert _merges(github) == ["7", "8", "9"], json.dumps(
        [{"number": p["number"], "state": p["state"], "image_source": p.get("image_source")}
         for p in status.get("prefixes") or []]
    )


def test_neutral_members_after_a_neutral_own_check_merge_with_no_push_run(github: GitHub) -> None:
    """The same day where the image workflow starts no run on a neutral push: main's latest
    image run is an older one that tested its tree. Control for the test above."""
    _neutral_front_merges_alone(github, push_run=False)
    for turn in range(6):
        waiting = [n for n in ("8", "9") if github.pulls[n]["state"] != "MERGED"]
        if not waiting:
            break
        _finish_batch(github, run_id=10_000 * (turn + 1))
        _batch_call(github, waiting[0])
    assert _merges(github) == ["7", "8", "9"]
    assert _image_dispatches(github) == 0


def _with_image_inputs(github: GitHub) -> None:
    """An image filter as installations write it: images, the lockfile, compose tests and
    release scripts, set on main before the members branch from it."""
    workflow = github.workflow
    path = workflow.repo / ".github/workflows/images.yml"
    path.write_text(
        'on:\n  pull_request:\n    paths:\n      - "docker/**"\n      - "package-lock.json"\n'
        '      - "tests/compose/**"\n      - "scripts/release/**"\n  push:\n'
    )
    workflow.git("checkout", "main")
    workflow.git("add", ".github")
    workflow.git("commit", "-m", "ci: image inputs")
    workflow.git("push", "origin", "main")
    github.base = workflow.git("rev-parse", "HEAD")
    github.main()
    workflow.git("checkout", "ticket/seven")
    workflow.git("merge", "--no-edit", "main")
    github.pulls["7"]["headRefOid"] = github.head = workflow.git("rev-parse", "HEAD")


def test_a_mixed_line_runs_images_once_for_its_one_image_change(github: GitHub) -> None:
    """[neutral, image change, neutral, neutral]: only the image change's prefix runs images,
    the first leans on main's proof, the last two on the image change's run, and all merge."""
    _with_image_inputs(github)
    _batch_setup(github, count=4, images=True, images_member=8)
    assert _batch_call(github).returncode == 3
    members = _prefixes(github)
    workflows = {m["number"]: m["scratch"]["workflows"] for m in members if m["scratch"]}
    assert all(("images" in w) == (n == "8") for n, w in workflows.items()), workflows
    for turn in range(8):
        waiting = [n for n in ("7", "8", "9", "10") if github.pulls[n]["state"] != "MERGED"]
        if not waiting:
            break
        _finish_batch(github, run_id=10_000 * (turn + 1))
        _batch_call(github, waiting[0])
    assert _merges(github) == ["7", "8", "9", "10"]
    assert _image_dispatches(github) == 1
    receipt = _receipt(github)
    assert receipt["images_complete"] is True
    assert "docker/change.txt" not in receipt["images_differing_files"]
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout + status.stderr


def test_a_failed_image_member_sent_back_frees_the_neutral_ones_behind_it(github: GitHub) -> None:
    """[image change, neutral, neutral]: the image change's prefix fails its image tests. Its
    member leaves the line; the neutral members behind it must not merge on that failed run,
    and must merge on main's own proof once rebuilt without it."""
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    first = _scratch(github, "7")
    path = (f"{_REPO}/actions/workflows/images.yml/runs?head_sha={first['head']}"
            "&event=workflow_dispatch&per_page=100")
    failed = github.api[path]["workflow_runs"][0]
    failed["conclusion"] = "failure"
    github.api[f"{_REPO}/actions/runs/{failed['id']}"] = failed
    github.api[f"{_REPO}/actions/runs/{failed['id']}/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    assert _batch_call(github).returncode == 1
    assert _merges(github) == []
    for number in ("8", "9"):
        _batch_call(github, number)
        assert "8" not in _merges(github) or "7" in _merges(github)
    # 7 is sent back: its pull request returns to draft and leaves the line.
    github.pulls["7"]["isDraft"] = True
    for turn in range(8):
        waiting = [n for n in ("8", "9") if github.pulls[n]["state"] != "MERGED"]
        if not waiting:
            break
        _finish_batch(github, run_id=10_000 * (turn + 1))
        _batch_call(github, waiting[0])
    assert _merges(github) == ["8", "9"]
    assert "docker/change.txt" not in github.workflow.git("ls-tree", "-r", "--name-only", "origin/main")


def test_a_rerun_of_the_shared_image_run_holds_the_members_leaning_on_it(github: GitHub) -> None:
    """[image change, neutral, neutral]: every prefix is green, then the shared image run is
    re-run in part before the front merges. While it runs, and after it fails, nobody leaning
    on it merges."""
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    first = _scratch(github, "7")
    path = (f"{_REPO}/actions/workflows/images.yml/runs?head_sha={first['head']}"
            "&event=workflow_dispatch&per_page=100")
    run = github.api[path]["workflow_runs"][0]
    run.update(status="in_progress", conclusion=None, run_attempt=2)
    github.api[f"{_REPO}/actions/runs/{run['id']}"] = run
    jobs = github.api[f"{_REPO}/actions/runs/{run['id']}/jobs?per_page=100"]["jobs"]
    jobs[1].update(status="in_progress", conclusion=None)
    for number in ("7", "8", "9"):
        _batch_call(github, number)
    assert _merges(github) == []
    run.update(status="completed", conclusion="failure")
    jobs[1].update(status="completed", conclusion="failure")
    for number in ("7", "8", "9"):
        _batch_call(github, number)
    assert _merges(github) == []


def test_three_reviewers_calling_at_once_start_one_image_run(github: GitHub) -> None:
    """[image change, neutral, neutral]: the three reviewers call the gate at the same moment.
    The line is built once, with one image run, and every member still merges."""
    _batch_setup(github, images=True)
    github.save()
    results: list[Any] = []

    def call(number: str) -> None:
        results.append(github.workflow.run_script("merge-when-current", number))

    threads = [threading.Thread(target=call, args=(n,)) for n in ("7", "8", "9")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for done in results:
        assert "Traceback" not in done.stderr, done.stderr
    saved = json.loads((github.workflow.state / "github.json").read_text())
    github.api, github.pulls = saved["api"], saved["pulls"]
    assert _image_dispatches(github) == 1
    for turn in range(6):
        waiting = [n for n in ("7", "8", "9") if github.pulls[n]["state"] != "MERGED"]
        if not waiting:
            break
        _finish_batch(github, run_id=10_000 * (turn + 1))
        _batch_call(github, waiting[0])
    assert _merges(github) == ["7", "8", "9"]
    assert _image_dispatches(github) == 1


def test_the_nightly_full_image_run_does_not_release_the_held_members(github: GitHub) -> None:
    """After the hold above, the nightly scheduled run of main passes every image test on that
    very main. The neutral members still wait: nothing but a later main push could free them,
    and no push lands while the line's front waits."""
    _neutral_front_merges_alone(github, push_run=True)
    _drive(github, ["8", "9"], rounds=3)
    assert _merges(github) == ["7"]
    main = github.workflow.git("rev-parse", "origin/main")
    _main_image_run(github, 9100, main, event="schedule")
    github.api[f"{_REPO}/actions/runs/9100/jobs?per_page=100"]["jobs"][1]["name"] = "Docker images: image tests"
    github.api[f"{_REPO}/check-runs/9101/annotations?per_page=100"] = [
        {"title": "Files tested", "message": github.workflow.git("rev-parse", f"{main}^{{tree}}")}
    ]
    _drive(github, ["8", "9"], rounds=3)
    assert _merges(github) == ["7", "8", "9"]
