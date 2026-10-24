"""Breaking test for pull request 59: a runner pool of a home-folder installation.

The installation keeps its file in the home config folder and adds none to its repository; the
repository's runner settings name it. `runner/install.sh` is run from its main checkout.
"""

import json
import subprocess
from pathlib import Path

from test_install import _RUNNER, _env, _git, _install, _settings, machine  # noqa: F401


def test_a_pool_installs_from_the_home_file(machine: Path) -> None:  # noqa: F811
    checkout = machine / "checkout"
    (checkout / ".github/runner").mkdir(parents=True)
    (checkout / ".github/runner/settings.sh").write_text("repository=example-app/example-app\n")
    _git(machine, "add", "--all")
    _git(machine, "commit", "--quiet", "-m", "main")
    _git(machine, "update-ref", "refs/remotes/origin/main", "HEAD")
    folder = machine / "home/.config/shipline/example-app-example-app"
    folder.mkdir(parents=True)
    (folder / "shipline.json").write_text(json.dumps({"runner": _RUNNER}))
    _settings(machine)
    read = subprocess.run(
        ["python3", str(Path(__file__).resolve().parents[2] / "scripts/workflow/workflow_config.py"),
         "sh"], cwd=checkout, env=_env(machine), capture_output=True, text=True, timeout=30,
    )
    assert read.returncode == 0, read.stderr
    assert "SHIPLINE_RUNNER_PREFIX=example-app\n" in read.stdout

    done = _install(machine)

    assert done.returncode == 0, done.stderr
