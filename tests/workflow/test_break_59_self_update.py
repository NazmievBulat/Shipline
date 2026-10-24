"""Breaking test for pull request 59: the update guard follows a home-folder installation.

The installation's file is found in the home config folder, named after the repository the
runner settings name; its state folder holds the gate's lock. While the gate holds it, no update
may move Shipline.
"""

import fcntl
import json
from pathlib import Path

from test_self_update import _MOVED, Shipline, shipline  # noqa: F401  (the fixture)


def test_a_held_gate_lock_in_the_home_file_s_state_folder_stops_an_update(
    shipline: Shipline,  # noqa: F811
) -> None:
    settings = shipline.repo / ".github/runner/settings.sh"
    settings.parent.mkdir(parents=True)
    settings.write_text("repository=example-app/example-app\n")
    folder = Path(shipline.env["HOME"]) / ".config/shipline/example-app-example-app"
    folder.mkdir(parents=True)
    (folder / "shipline.json").write_text(json.dumps({"state_folder": ".local/example-state"}))
    shipline.good()
    landed = shipline.land()
    shipline.later(10)
    state = shipline.repo / ".local/example-state"
    state.mkdir(parents=True)
    with (state / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert shipline.tick() == 0
        assert shipline.head == shipline.first
    shipline.later(10)
    assert shipline.tick() == _MOVED
    assert shipline.head == landed
