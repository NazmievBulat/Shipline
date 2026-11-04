"""A provider stop must retain its identity when a terminal viewport changes."""

from break_77_support import CONTINUE, ERROR, STOPPED, enters, stopped_session, tick
from workflow_harness import Workflow


def test_resize_after_successful_retry_does_not_retry_the_same_stop(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    # Attaching from a smaller terminal removes old rows above the same error.
    history = "• Read example-app\n• Checked configuration\n"
    workflow.screen(history + STOPPED, target)
    assert tick(workflow, 0).returncode == 0
    assert tick(workflow, 300).returncode == 0
    assert enters(workflow, target) == 1
    workflow.screen(STOPPED, target)
    assert tick(workflow, 301).returncode == 0
    assert tick(workflow, 1201).returncode == 0
    actual = enters(workflow, target)
    assert actual == 1, f"same stop was submitted {actual} times after viewport resize"


def test_resize_during_wait_does_not_restart_the_five_minute_timer(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    workflow.screen("• Read example-app\n" + STOPPED, target)
    assert tick(workflow, 0).returncode == 0
    workflow.screen(STOPPED, target)
    assert tick(workflow, 299).returncode == 0
    assert tick(workflow, 300).returncode == 0
    actual = enters(workflow, target)
    assert actual == 1, f"first stop reached five minutes, but retries = {actual}"


def test_identical_full_viewport_after_another_capacity_stop_retries_again(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    # The generic input stand-in removes historical prompt rows. Preserve those
    # rows here, as real tmux does, so this test models a full transcript.
    tmux = workflow.bin / "tmux"
    source = tmux.read_text()
    old = (
        "    at = boxes[-1] - len(boxes) + 1 if boxes else len(lines)\n"
        "    lines = [line for line in lines if not line.startswith(prompts)]\n"
    )
    assert old in source
    tmux.write_text(source.replace(old,
        "    at = boxes[-1] if boxes else len(lines)\n"
        "    if boxes: lines.pop(at)\n"
    ))
    # Once repeated retries fill a pane, appending the same prompt/error pair and
    # scrolling one pair off the top can leave the visible text exactly unchanged.
    block = f"› {CONTINUE}\n\n{ERROR}\n\n"
    transcript = block * 20
    def viewport(text: str) -> str:
        return "\n".join(text.splitlines()[-44:]) + "\n› \n  ? for shortcuts  90% context left\n"

    first = viewport(transcript)
    second = viewport(transcript + block)
    assert first == second
    workflow.screen(first, target)
    assert tick(workflow, 0).returncode == 0
    # The accepted retry immediately reaches a new capacity error before another tick.
    (workflow.state / f"screen-after-enter-{target}").write_text(second)
    assert tick(workflow, 300).returncode == 0
    assert enters(workflow, target) == 1
    assert tick(workflow, 301).returncode == 0
    assert tick(workflow, 1201).returncode == 0
    actual = enters(workflow, target)
    assert actual == 2, f"new capacity stop received no second retry; retries = {actual}"
