"""Breaking tests for pull request 36: a cloud worker's push runs the tests CI will run.

Each test builds an `example-app` origin and a cloud checkout of it whose pre-push hook ends
with `shipline ci-tests-for-changes --run --extra-only`, as RULES.md says, and pushes through
real `git push`. The suites are small shell stand-ins: a server suite chosen by the files a
change reaches, and a browser suite whose browser cannot start in the cloud.

Run with Python 3.14 (Shipline's own version) as `python3` on the PATH:

    python3 -m pytest -q tests/break/test_pr36_cloud_pre_push.py

The tests that need root and an unprivileged account (`SHIPLINE_CLOUD_TEST_USER`) create the
account `example-tester` when they run as root, and are skipped otherwise.
"""

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

SHIPLINE = Path(__file__).resolve().parents[2]
TESTER = "example-tester"

_SERVER_SELECT = r"""#!/usr/bin/env bash
# <select> <base> --files-from <list>: the server tests a change reaches, as CI chooses them.
set -euo pipefail
list=$3
total=$(ls tests/server | wc -l)
if grep -qxE 'shipline.json|server/settings.sh' "$list"; then
  printf '{"whole": true, "tests": [], "total": %s, "reasons": ["a broad change"]}\n' "$total"
  exit 0
fi
picked=()
while IFS= read -r path; do
  case $path in
    server/migrations/*) picked+=(tests/server/test_migrations.sh) ;;
    server/*.sh) name=${path#server/}; [[ ! -f tests/server/test_$name ]] || picked+=("tests/server/test_$name") ;;
    tests/server/test_*.sh) picked+=("$path") ;;
  esac
done <"$list"
if ((${#picked[@]} == 0)); then
  printf '{"whole": false, "tests": [], "total": %s, "reasons": []}\n' "$total"
  exit 0
fi
mapfile -t picked < <(printf '%s\n' "${picked[@]}" | sort -u)
json=$(printf '"%s",' "${picked[@]}")
printf '{"whole": false, "tests": [%s], "total": %s, "reasons": ["reached by imports"]}\n' \
  "${json%,}" "$total"
"""

_SERVER_RUN = r"""#!/usr/bin/env bash
# server-tests --workers N [--shard K/N] [files]: runs each test; the whole suite without files.
set -uo pipefail
echo "server-tests $* as $(id -un)" >>"$RUNS_LOG"
shift 2
shard=""
if [[ ${1:-} == --shard ]]; then shard=$2; shift 2; fi
files=("$@")
((${#files[@]})) || mapfile -t files < <(ls tests/server/*.sh)
if [[ -n $shard ]]; then
  k=${shard%/*}; n=${shard#*/}; mine=()
  for i in "${!files[@]}"; do (( i % n == k - 1 )) && mine+=("${files[$i]}"); done
  files=("${mine[@]}")
fi
failed=0
for test in "${files[@]}"; do
  if bash "$test"; then echo "ok $test"; else echo "FAIL $test"; failed=1; fi
done
exit $failed
"""

_BROWSER_SELECT = r"""#!/usr/bin/env bash
# browser-tests-for-changes --ci <base> --files-from <list>: every spec of a changed page.
set -euo pipefail
list=$4
total=$(ls tests/browser | wc -l)
picked=()
while IFS= read -r path; do
  case $path in web/*.html) name=${path#web/}; picked+=("tests/browser/${name%.html}.spec.sh") ;; esac
done <"$list"
json=""
((${#picked[@]} == 0)) || json=$(printf '"%s",' "${picked[@]}")
printf '{"whole": false, "specs": [%s], "total": %s, "reasons": []}\n' "${json%,}" "$total"
"""

_BROWSER_RUN = r"""#!/usr/bin/env bash
echo "browser-tests $* as $(id -un)" >>"$RUNS_LOG"
shift 2
for spec in "$@"; do bash "$spec" || exit 1; done
"""

_GIT_ENV = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Example Worker",
    "GIT_AUTHOR_EMAIL": "owner@example.com",
    "GIT_COMMITTER_NAME": "Example Worker",
    "GIT_COMMITTER_EMAIL": "owner@example.com",
}


def _python_bin(root: Path, python: str) -> Path:
    """A folder whose `python3` is the one named, first on the PATH of every push."""
    folder = root / f"bin-{Path(python).name}"
    folder.mkdir(exist_ok=True)
    link = folder / "python3"
    if not link.exists():
        link.symlink_to(python)
    return folder


def _python314() -> str:
    for name in ("python3.14", "python3"):
        found = shutil.which(name)
        if found and subprocess.run(
            [found, "-c", "import sys; assert sys.version_info >= (3, 14)"], check=False
        ).returncode == 0:
            return found
    pytest.skip("Shipline needs Python 3.14")


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


class App:
    """An `example-app` origin and a cloud checkout of it, on a branch from main."""

    def __init__(self, root: Path, suites: dict, python: str | None = None) -> None:
        self.root = root
        root.mkdir(exist_ok=True)
        root.chmod(0o755)
        self.origin = root / "origin.git"
        self.checkout = root / "cloud"
        self.runs = root / "runs"
        self.runs.mkdir(mode=0o777)
        self.runs.chmod(0o777)
        self.log = self.runs / "runs.log"
        python_bin = _python_bin(root, python or _python314())
        self.env = {
            **_GIT_ENV,
            "PATH": f"{python_bin}:{SHIPLINE / 'bin'}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "RUNS_LOG": str(self.log),
            # Not root unless a test says so: the suites run as the calling user.
            "WORKFLOW_EUID": "1000",
        }
        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin)],
                       env=self.env, check=True)
        self.git_in(root, "clone", "--quiet", str(self.origin), str(self.checkout))
        app = self.checkout
        _write(app / "shipline.json", json.dumps(
            {"repository": "example-owner/example-app", "cloud_pre_push": {"suites": suites}},
            indent=2))
        _write(app / "tools/shipline/server-tests-for-changes", _SERVER_SELECT, 0o755)
        _write(app / "tools/shipline/server-tests", _SERVER_RUN, 0o755)
        _write(app / "tools/shipline/browser-tests-for-changes", _BROWSER_SELECT, 0o755)
        _write(app / "tools/shipline/browser-tests", _BROWSER_RUN, 0o755)
        # No browser can start in the cloud.
        _write(app / "tools/shipline/browser-starts",
               "#!/usr/bin/env bash\necho 'chromium: cannot open display' >&2\nexit 1\n", 0o755)
        _write(app / "server/settings.sh", "PORT=8000\n")
        _write(app / "server/orders.sh", "orders() { echo 3; }\n")
        _write(app / "server/users.sh", "users() { echo 2; }\n")
        _write(app / "server/migrations/0001_start.sql", "-- start\n")
        _write(app / "tests/server/test_orders.sh",
               'source server/orders.sh; [[ $(orders) == 3 ]]\n')
        _write(app / "tests/server/test_users.sh", 'source server/users.sh; [[ $(users) == 2 ]]\n')
        _write(app / "tests/server/test_migrations.sh", textwrap.dedent("""\
            # Two migrations may not share one number.
            dupes=$(ls server/migrations | cut -d_ -f1 | sort | uniq -d)
            [[ -z $dupes ]] || { echo "two migrations numbered $dupes"; exit 1; }
            """))
        for page in ("home", "cart", "login", "search", "account"):
            _write(app / f"web/{page}.html", f"<h1>{page}</h1>\n")
            _write(app / f"tests/browser/{page}.spec.sh", f"grep -q {page} web/{page}.html\n")
        # The repository's pre-push hook: its fast checks, then the cloud worker's suites.
        _write(app / ".githooks/pre-push", textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "fast checks: lint passed"
            shipline ci-tests-for-changes --run --extra-only
            """), 0o755)
        self.git("config", "core.hooksPath", ".githooks")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "feat: example-app")
        self.git("push", "--quiet", "--no-verify", "origin", "main")
        self.git("checkout", "--quiet", "-b", "ticket/example")

    def git_in(self, where: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=where, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def git(self, *args: str) -> str:
        return self.git_in(self.checkout, *args)

    def change(self, path: str, text: str, message: str = "feat: a change") -> None:
        _write(self.checkout / path, text)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)

    def push(self, cloud: bool = True, **env: str) -> subprocess.CompletedProcess[str]:
        full = {**self.env, **env}
        if cloud:
            full["SHIPLINE_CLOUD_WORKER"] = "1"
        return subprocess.run(["git", "push", "origin", "ticket/example"], cwd=self.checkout,
                              env=full, capture_output=True, text=True, timeout=300, check=False)

    def pushed(self) -> bool:
        return subprocess.run(["git", "rev-parse", "--verify", "--quiet", "ticket/example"],
                              cwd=self.origin, env=self.env, capture_output=True,
                              check=False).returncode == 0

    def runs_said(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def main_moves(self, path: str, text: str) -> None:
        """Another pull request lands on main after this branch left it."""
        other = self.root / "other"
        if not other.exists():
            self.git_in(self.root, "clone", "--quiet", str(self.origin), str(other))
        _write(other / path, text)
        self.git_in(other, "add", "-A")
        self.git_in(other, "commit", "--quiet", "-m", "feat: another change on main")
        self.git_in(other, "push", "--quiet", "--no-verify", "origin", "main")


SUITES = {
    "server": {
        "select": "tools/shipline/server-tests-for-changes",
        "run": "tools/shipline/server-tests --workers {workers}",
        "part": "--shard {shard}/{shards}",
    },
    "browser": {
        "select": "tools/shipline/browser-tests-for-changes --ci",
        "run": "tools/shipline/browser-tests --workers {workers}",
        "probe": "tools/shipline/browser-starts",
    },
}


@pytest.fixture
def root():
    # Readable by the unprivileged account, as a cloud checkout is.
    folder = Path(tempfile.mkdtemp(prefix="break36-", dir="/tmp"))
    folder.chmod(0o755)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def app(root: Path) -> App:
    return App(root, SUITES)


def _tester() -> str:
    if os.geteuid() != 0:
        pytest.skip("needs root, as the cloud container runs")
    try:
        pwd.getpwnam(TESTER)
    except KeyError:
        subprocess.run(["useradd", "--create-home", TESTER], check=True)
    return TESTER


# Daily flows.


def test_a_server_change_runs_the_server_tests_it_reaches_and_the_push_goes(app: App) -> None:
    app.change("server/orders.sh", "orders() { echo 3; }\n# faster\n")

    done = app.push()

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.pushed()
    assert app.runs_said() == ["server-tests --workers 4 tests/server/test_orders.sh as root"]
    assert "server: 1 of 3 files: tests/server/test_orders.sh" in done.stdout


def test_a_server_change_that_breaks_a_reached_test_refuses_the_push(app: App) -> None:
    app.change("server/orders.sh", "orders() { echo 4; }\n")

    done = app.push()

    assert done.returncode != 0
    assert not app.pushed()
    assert "the push is refused: server failed" in done.stdout


def test_a_local_push_runs_no_extra_suite_even_when_one_would_fail(app: App) -> None:
    app.change("server/orders.sh", "orders() { echo 4; }\n")

    done = app.push(cloud=False)

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.pushed()
    assert app.runs_said() == []
    assert "fast checks: lint passed" in done.stdout


def test_a_broad_change_runs_the_whole_server_suite(app: App) -> None:
    app.change("server/settings.sh", "PORT=8001\n")

    done = app.push()

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.runs_said() == ["server-tests --workers 4 as root"]
    assert "the whole suite (3 files)" in done.stdout


def test_a_working_change_not_yet_committed_is_in_the_selection(app: App) -> None:
    app.change("server/users.sh", "users() { echo 2; }\n# a\n")
    _write(app.checkout / "server/orders.sh", "orders() { echo 3; }\n# b\n")

    done = app.push()

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.runs_said() == [
        "server-tests --workers 4 tests/server/test_orders.sh tests/server/test_users.sh as root"
    ]


def test_the_browser_cannot_start_in_the_cloud_so_its_specs_are_left_to_ci(app: App) -> None:
    for page in ("home", "cart", "login", "search", "account"):
        _write(app.checkout / f"web/{page}.html", f"<h1>{page}</h1>\n<p>new</p>\n")
    app.change("web/home.html", "<h1>home</h1>\n<p>new</p>\n")

    done = app.push()

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.pushed()
    # All five are chosen, past the local cap of three, and none runs here.
    assert "browser: 5 of 5 files" in done.stdout
    assert "browser: left to CI: no browser can start as this user: chromium: cannot open display" in done.stdout
    assert app.runs_said() == []


def test_a_browser_probe_that_hangs_leaves_the_browser_to_ci_in_time(app: App) -> None:
    _write(app.checkout / "tools/shipline/browser-starts", "#!/usr/bin/env bash\nsleep 60\n", 0o755)
    app.change("web/cart.html", "<h1>cart</h1>\n<p>new</p>\n")
    started = time.monotonic()

    done = app.push(WORKFLOW_CLOUD_PROBE_SECONDS="2")

    assert done.returncode == 0, done.stdout + done.stderr
    assert time.monotonic() - started < 30
    assert "browser: left to CI" in done.stdout


# Failures met in use.


def test_a_suite_held_past_its_limit_refuses_with_its_time_and_leaves_nothing_behind(
    app: App,
) -> None:
    _write(app.checkout / "tests/server/test_orders.sh",
           "trap '' TERM\n( trap '' TERM; exec sleep 321 ) &\nsleep 322\n")
    app.change("server/orders.sh", "orders() { echo 3; }\n# slow\n")
    started = time.monotonic()

    done = app.push(WORKFLOW_CLOUD_SUITE_SECONDS="2", WORKFLOW_CLOUD_SUITE_GRACE="1")

    assert done.returncode != 0
    assert not app.pushed()
    assert time.monotonic() - started < 30
    assert "server: timed out after 0m 2s, the limit is 0m 2s" in done.stdout
    assert "the push is refused: server timed out" in done.stdout
    left = subprocess.run(["pgrep", "-f", "sleep 32[12]"], capture_output=True, text=True)
    assert left.stdout == ""


def test_after_the_whole_suite_timed_out_a_part_runs_and_ci_runs_the_rest(app: App) -> None:
    app.change("server/settings.sh", "PORT=8001\n")

    done = app.push(SHIPLINE_CLOUD_SERVER_PART="1 of 2")

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.runs_said() == ["server-tests --workers 4 --shard 1/2 as root"]
    assert "part 1 of 2 passed" in done.stdout and "CI runs the rest" in done.stdout


def test_a_failing_part_still_refuses(app: App) -> None:
    # The first part of three holds test_migrations.sh (sorted: migrations, orders, users).
    _write(app.checkout / "tests/server/test_migrations.sh", "exit 1\n")
    app.change("server/settings.sh", "PORT=8001\n")

    done = app.push(SHIPLINE_CLOUD_SERVER_PART="1 of 3")

    assert done.returncode != 0
    assert "the push is refused: server failed" in done.stdout


def test_a_change_reaching_most_of_the_server_suite_has_a_way_past_the_time_limit(
    app: App,
) -> None:
    """A change to a module most tests import is reached by them, so its selection is most of
    the suite but not the whole: it takes as long as the whole suite. After its timeout the
    rules give the worker one way on, a part, and the part must then run."""
    for name in ("orders", "users"):
        _write(app.checkout / f"tests/server/test_{name}.sh", f"source server/{name}.sh; sleep 3\n")
        _write(app.checkout / f"server/{name}.sh", f"{name}() {{ echo 1; }}\n# core\n")
    app.change("server/orders.sh", "orders() { echo 1; }\n# core\n")

    first = app.push(WORKFLOW_CLOUD_SUITE_SECONDS="4", WORKFLOW_CLOUD_SUITE_GRACE="1")
    assert "server: timed out" in first.stdout
    # The worker does as the cloud sentence says after a timeout.
    second = app.push(
        WORKFLOW_CLOUD_SUITE_SECONDS="4", WORKFLOW_CLOUD_SUITE_GRACE="1",
        SHIPLINE_CLOUD_SERVER_PART="1 of 2",
    )

    assert second.returncode == 0, second.stdout[-600:]


def test_two_pushes_at_once_each_run_their_own_selection(app: App, root: Path) -> None:
    second = App(root / "second", SUITES)
    app.change("server/orders.sh", "orders() { echo 3; }\n# a\n")
    second.change("server/users.sh", "users() { echo 2; }\n# b\n")
    _write(app.checkout / "tests/server/test_orders.sh", 'source server/orders.sh; sleep 2; [[ $(orders) == 3 ]]\n')
    app.git("commit", "--quiet", "-am", "test: slower")

    with subprocess.Popen(["git", "push", "origin", "ticket/example"], cwd=app.checkout,
                          env={**app.env, "SHIPLINE_CLOUD_WORKER": "1"},
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as one:
        other = second.push()
        out, _ = one.communicate(timeout=120)

    assert one.returncode == 0, out
    assert other.returncode == 0, other.stdout + other.stderr
    assert app.runs_said() == ["server-tests --workers 4 tests/server/test_orders.sh as root"]
    assert second.runs_said() == ["server-tests --workers 4 tests/server/test_users.sh as root"]


def test_a_missing_selector_refuses_the_push_by_its_name(app: App) -> None:
    (app.checkout / "tools/shipline/server-tests-for-changes").unlink()
    app.change("server/orders.sh", "orders() { echo 3; }\n# x\n")

    done = app.push()

    assert done.returncode != 0
    assert "server: the selection failed" in done.stdout


def test_a_repository_without_cloud_suites_pushes_as_before(root: Path) -> None:
    app = App(root, {})
    app.change("server/orders.sh", "orders() { echo 4; }\n")

    done = app.push()

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.pushed()


def test_main_moved_on_since_the_branch_left_it_and_the_merge_ci_tests_fails(app: App) -> None:
    """CI tests the merge of the branch into main. Another pull request landed migration 0002
    on main after this branch left it, and this branch adds its own 0002: the typical miss the
    ticket names. The cloud push runs test_migrations on the branch alone, where it passes."""
    app.main_moves("server/migrations/0002_orders_index.sql", "-- index\n")
    app.change("server/migrations/0002_users_email.sql", "-- email\n")

    done = app.push()

    # What CI will see: the merge of the branch into the current main.
    merged = app.root / "merged"
    app.git_in(app.root, "clone", "--quiet", str(app.origin), str(merged))
    app.git_in(merged, "fetch", "--quiet", str(app.checkout), "ticket/example")
    app.git_in(merged, "merge", "--quiet", "--no-edit", "FETCH_HEAD")
    ci = subprocess.run(["bash", "tests/server/test_migrations.sh"], cwd=merged,
                        capture_output=True, text=True)
    assert ci.returncode != 0, "the merge CI tests should fail"
    assert "server: 1 of 3 files: tests/server/test_migrations.sh" in done.stdout
    # The worker needs this push refused, since CI will come back red on it.
    assert done.returncode != 0, done.stdout[-500:]


def test_a_cloud_container_whose_python3_is_older_says_what_it_needs(root: Path) -> None:
    older = shutil.which("python3.13") or shutil.which("python3.12")
    if not older:
        pytest.skip("no older Python here")
    app = App(root, SUITES)
    app.env["PATH"] = app.env["PATH"].replace(str(_python_bin(root, _python314())),
                                              str(_python_bin(root, older)))
    app.change("server/orders.sh", "orders() { echo 3; }\n# y\n")

    done = app.push()

    out = done.stdout + done.stderr
    assert done.returncode == 0 or "3.14" in out, out[-500:]


# As root, with the unprivileged account.


def test_as_root_every_suite_runs_as_the_test_account(app: App) -> None:
    tester = _tester()
    app.change("server/orders.sh", "orders() { echo 3; }\n# z\n")

    done = app.push(WORKFLOW_EUID="0", SHIPLINE_CLOUD_TEST_USER=tester)

    assert done.returncode == 0, done.stdout + done.stderr
    assert app.runs_said() == [f"server-tests --workers 4 tests/server/test_orders.sh as {tester}"]


def test_as_root_without_the_account_nothing_runs(app: App) -> None:
    _tester()
    app.change("server/orders.sh", "orders() { echo 3; }\n# z\n")

    done = app.push(WORKFLOW_EUID="0")

    assert done.returncode != 0
    assert "SHIPLINE_CLOUD_TEST_USER" in done.stdout
    assert app.runs_said() == []


def test_as_root_a_suite_held_past_its_limit_as_the_account_is_stopped(app: App) -> None:
    tester = _tester()
    _write(app.checkout / "tests/server/test_orders.sh",
           "trap '' TERM\n( trap '' TERM; exec setsid sleep 323 ) &\nsleep 324\n")
    app.change("server/orders.sh", "orders() { echo 3; }\n# slow\n")

    done = app.push(WORKFLOW_EUID="0", SHIPLINE_CLOUD_TEST_USER=tester,
                    WORKFLOW_CLOUD_SUITE_SECONDS="2", WORKFLOW_CLOUD_SUITE_GRACE="1")

    assert "server: timed out" in done.stdout
    left = subprocess.run(["pgrep", "-f", "sleep 32[34]"], capture_output=True, text=True)
    assert left.stdout == ""


def test_as_root_a_suite_that_writes_in_its_checkout_can_as_the_account(app: App) -> None:
    """The cloud checkout is root's. A server suite that keeps a cache or results in the
    checkout, or asks git where the checkout is, runs as the account the worker named."""
    tester = _tester()
    _write(app.checkout / "tests/server/test_orders.sh", textwrap.dedent("""\
        cd "$(git rev-parse --show-toplevel)" || exit 1
        mkdir -p .cache && echo ok >.cache/orders || exit 1
        """))
    app.change("server/orders.sh", "orders() { echo 3; }\n# w\n")

    done = app.push(WORKFLOW_EUID="0", SHIPLINE_CLOUD_TEST_USER=tester)

    assert done.returncode == 0, done.stdout[-600:]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
