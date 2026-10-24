"""Breaking tests for pull request 54: a refresh clears configured generated output first.

A web app's build leaves generated route types in an ignored `apps/web/.next/types` inside each
kept pull request worktree, and the pre-push typecheck reads them. When main moves a route, a
refresh that merges main and pushes is refused on stale types nobody changed. These tests
replay that with a stand-in typecheck, through the real gate and the real tick, and then try
the failures around it: other sessions' worktrees, a real type error, two clears at once, a
file written during a clear, a git timeout and a large build cache.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from workflow.test_merge_driver import _handoff, _publish
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture

github = github_fixture

WORKFLOW = Path(__file__).resolve().parents[2] / "scripts" / "workflow"
GENERATED = ["apps/web/.next/types", "apps/web/.next/cache"]
TYPECHECK = textwrap.dedent(
    """\
    #!/bin/bash
    # Stands in for the web typecheck, whose program includes apps/web/.next/types.
    cd "$(git rev-parse --show-toplevel)" || exit 1
    shopt -s nullglob globstar
    if [[ -e apps/web/TYPE_ERROR ]]; then
      echo "apps/web/app/broken.ts(1,7): error TS2322: Type 'string' is not assignable"
      exit 1
    fi
    for file in apps/web/.next/types/app/**/page.ts; do
      route=${file#apps/web/.next/types/app/}
      route=${route%/page.ts}
      if [[ ! -e apps/web/app/$route/page.tsx ]]; then
        echo "$file(2,24): error TS2307: Cannot find module '../../../../app/$route/page.js'"
        exit 1
      fi
    done
    """
)


def _configure(github: GitHub, paths: list[str] | None) -> None:
    workflow = github.workflow
    config = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    given = json.loads(config.read_text())
    if paths is not None:
        given["refresh"] = {"clear_generated": paths}
    config.write_text(json.dumps(given))


def _web_app(github: GitHub) -> Path:
    """Main gains the web app with its own tracked `.gitignore`; pull request 7 has it too."""
    workflow = github.workflow
    branch = workflow.worktree(7)
    workflow.git("checkout", "main")
    workflow.git("worktree", "add", str(branch), "ticket/seven")
    web = workflow.repo / "apps/web"
    (web / "app/dashboard").mkdir(parents=True)
    (web / ".gitignore").write_text("/.next/\n/node_modules\n")
    (web / "app/dashboard/page.tsx").write_text("export default function Page() {}\n")
    workflow.git("add", "apps")
    workflow.git("commit", "-m", "feat: web app")
    workflow.git("push", "origin", "main")
    workflow.git("merge", "--no-edit", "main", cwd=branch)
    workflow.git("push", "origin", "HEAD:ticket/seven", cwd=branch)
    github.head = workflow.git("rev-parse", "HEAD", cwd=branch)
    github.pulls["7"]["headRefOid"] = github.head
    github.base = workflow.git("rev-parse", "main")
    github.main()
    github.ci(github.base, github.head)
    return branch


def _main_moves_the_route(github: GitHub) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("mv", "apps/web/app/dashboard", "apps/web/app/overview")
    workflow.git("commit", "-m", "refactor: dashboard is now overview")
    workflow.git("push", "origin", "main")
    github.base = workflow.git("rev-parse", "HEAD")
    github.main()
    github.ci(github.base, github.head, tested_base=old)
    github.save()


def _built(checkout: Path, note: str) -> None:
    """What a build and a typecheck leave: route types, a cache and the server output."""
    next_folder = checkout / "apps/web/.next"
    types = next_folder / "types/app/dashboard"
    types.mkdir(parents=True)
    (types / "page.ts").write_text(
        f"// {note}\nimport * as entry from '../../../../app/dashboard/page.js'\n"
    )
    (next_folder / "types/routes.d.ts").write_text(f"// {note}\n")
    (next_folder / "types/package.json").write_text('{"type": "module"}\n')
    pack = next_folder / "cache/webpack/client-production"
    pack.mkdir(parents=True)
    (pack / "0.pack").write_text(note)
    (next_folder / "cache/.tsbuildinfo").write_text(note)
    (next_folder / "server/app").mkdir(parents=True)
    (next_folder / "server/app/dashboard.html").write_text(note)
    (next_folder / "BUILD_ID").write_text(note)


def _typecheck_on_push(github: GitHub) -> None:
    workflow = github.workflow
    hooks = workflow.bin / "web-hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text(TYPECHECK)
    hook.chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))


def _other_session(github: GitHub) -> Path:
    """Another session's worktree, busy at the same time, with its own stale output."""
    workflow = github.workflow
    other = workflow.worktree(8)
    workflow.git("worktree", "add", "-b", "ticket/eight", str(other), github.base)
    _built(other, "session eight")
    return other


def _untouched(checkout: Path, note: str) -> None:
    next_folder = checkout / "apps/web/.next"
    assert (next_folder / "types/app/dashboard/page.ts").read_text().startswith(f"// {note}")
    assert (next_folder / "cache/webpack/client-production/0.pack").read_text() == note


def _remote(github: GitHub, branch: str = "ticket/seven") -> str:
    return github.workflow.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0]


@pytest.mark.parametrize("paths", [None, GENERATED], ids=["today", "configured"])
def test_gate_refresh_in_a_kept_worktree_clears_stale_route_types(
    github: GitHub, paths: list[str] | None
) -> None:
    workflow = github.workflow
    branch = _web_app(github)
    _main_moves_the_route(github)
    _configure(github, paths)
    _built(branch, "pull request seven")
    _built(workflow.repo, "main checkout")
    other = _other_session(github)
    _typecheck_on_push(github)

    done = workflow.run_script("merge-when-current", "7", cwd=branch)

    if paths is None:
        # Today's failure, reproduced: the push is refused on types nobody changed.
        assert done.returncode == 1, done.stdout + done.stderr
        assert "TS2307" in done.stderr
        assert _remote(github) == github.head
        return
    assert done.returncode == 2, done.stdout + done.stderr
    assert "CI requested" in done.stdout
    assert _remote(github) == workflow.git("rev-parse", "HEAD", cwd=branch)
    assert workflow.git("rev-parse", "HEAD^2", cwd=branch) == github.base
    next_folder = branch / "apps/web/.next"
    assert not (next_folder / "types").exists()
    assert not (next_folder / "cache").exists()
    assert (next_folder / "server/app/dashboard.html").read_text() == "pull request seven"
    assert (next_folder / "BUILD_ID").read_text() == "pull request seven"
    assert (branch / "apps/web/app/overview/page.tsx").exists()
    assert (branch / "apps/web/.gitignore").exists()
    assert not workflow.git("status", "--porcelain", cwd=branch)
    _untouched(workflow.repo, "main checkout")
    _untouched(other, "session eight")


def test_tick_refresh_of_a_handed_off_pull_request_clears_stale_route_types(
    github: GitHub,
) -> None:
    workflow = github.workflow
    branch = _web_app(github)
    workflow.list_pull_request(7, draft=False)
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {github.head}\n")
    github.pulls["7"]["labels"] = [{"name": "workflow"}]
    _publish(github)
    _handoff(github)
    workflow.run("stop", "review-7")
    github.batch_mode = True
    github.driver_mode = True
    _main_moves_the_route(github)
    _configure(github, GENERATED)
    _built(branch, "pull request seven")
    _built(workflow.repo, "main checkout")
    other = _other_session(github)
    _typecheck_on_push(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "TS2307" not in done.stdout + done.stderr
    assert _remote(github) != github.head
    assert _remote(github) == workflow.git("rev-parse", "HEAD", cwd=branch)
    assert not (branch / "apps/web/.next/types").exists()
    assert (branch / "apps/web/.next/BUILD_ID").read_text() == "pull request seven"
    _untouched(workflow.repo, "main checkout")
    _untouched(other, "session eight")


@pytest.mark.parametrize("paths", [None, GENERATED], ids=["today", "configured"])
def test_a_real_type_error_is_still_refused_as_today(
    github: GitHub, paths: list[str] | None
) -> None:
    workflow = github.workflow
    branch = _web_app(github)
    (branch / "apps/web/TYPE_ERROR").write_text("x\n")
    workflow.git("add", "apps/web/TYPE_ERROR", cwd=branch)
    workflow.git("commit", "-m", "feat: a broken page", cwd=branch)
    workflow.git("push", "origin", "HEAD:ticket/seven", cwd=branch)
    github.head = workflow.git("rev-parse", "HEAD", cwd=branch)
    github.pulls["7"]["headRefOid"] = github.head
    github.ci(github.base, github.head)
    _main_moves_the_route(github)
    _configure(github, paths)
    _built(branch, "pull request seven")
    _typecheck_on_push(github)

    done = workflow.run_script("merge-when-current", "7", cwd=branch)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "could not push the refreshed branch" in done.stderr
    assert "TS2322" in done.stderr
    assert _remote(github) == github.head
    # The refused merge stays as a refused hook push leaves it today.
    assert workflow.git("rev-parse", "HEAD^1", cwd=branch) == github.head
    assert (branch / "apps/web/.next/types").exists() == (paths is None)


def test_a_kept_worktree_never_built_refreshes(github: GitHub) -> None:
    workflow = github.workflow
    branch = _web_app(github)
    _main_moves_the_route(github)
    _configure(github, GENERATED)
    _typecheck_on_push(github)

    done = workflow.run_script("merge-when-current", "7", cwd=branch)

    assert done.returncode == 2, done.stdout + done.stderr
    assert _remote(github) == workflow.git("rev-parse", "HEAD", cwd=branch)


def test_an_empty_refresh_object_is_todays_behaviour(github: GitHub) -> None:
    workflow = github.workflow
    branch = _web_app(github)
    _main_moves_the_route(github)
    config = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    given = json.loads(config.read_text())
    given["refresh"] = {}
    config.write_text(json.dumps(given))
    _built(branch, "pull request seven")

    done = workflow.run_script("merge-when-current", "7", cwd=branch)

    assert done.returncode == 2, done.stdout + done.stderr
    _untouched(branch, "pull request seven")


# Direct calls of the cleanup, each in its own interpreter as the scripts run it.


def _checkout(tmp_path: Path, name: str = "example-app") -> Path:
    root = tmp_path / name
    (root / "apps/web/app").mkdir(parents=True)
    (root / "apps/web/.gitignore").write_text("/.next/\n")
    (root / "apps/web/app/page.tsx").write_text("x\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "example", "GIT_AUTHOR_EMAIL": "owner@example.com",
           "GIT_COMMITTER_NAME": "example", "GIT_COMMITTER_EMAIL": "owner@example.com"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)
    return root


def _fill(root: Path, files: int, folder: str = "cache/images") -> None:
    target = root / "apps/web/.next" / folder
    for index in range(files):
        part = target / f"{index % 100:02d}"
        part.mkdir(parents=True, exist_ok=True)
        (part / f"{index}.webp").write_text("x")


def _python(code: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    program = f"import sys\nsys.path.insert(0, {str(WORKFLOW)!r})\n" + textwrap.dedent(code)
    return subprocess.run(
        [sys.executable, "-c", program, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def test_two_clears_at_once_on_one_worktree_both_finish(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    _fill(root, 4000, "types/app")
    _fill(root, 4000)
    (root / "apps/web/.next/server").mkdir()
    (root / "apps/web/.next/server/keep.js").write_text("keep")
    code = """
        from pathlib import Path
        import workflow_config as wc
        wc.Refresh(("apps/web/.next/types", "apps/web/.next/cache")).clear(Path(sys.argv[1]))
        """
    started = [
        subprocess.Popen(
            [sys.executable, "-c", f"import sys\nsys.path.insert(0, {str(WORKFLOW)!r})\n"
             + textwrap.dedent(code), str(root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    results = [(process.wait(timeout=120), process.stderr.read()) for process in started]

    assert [code for code, _ in results] == [0, 0], results
    assert not (root / "apps/web/.next/types").exists()
    assert not (root / "apps/web/.next/cache").exists()
    assert (root / "apps/web/.next/server/keep.js").read_text() == "keep"


def test_a_file_written_during_the_clear_is_kept(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    _built(root, "stale")
    done = _python(
        """
        from pathlib import Path
        import workflow_config as wc
        root = Path(sys.argv[1])
        listed = wc._generated_git
        def git(checkout, *args):
            done = listed(checkout, *args)
            if "--ignored" in args:
                # A build writes a new route type just after git listed the old ones.
                fresh = root / "apps/web/.next/types/app/overview"
                fresh.mkdir(parents=True, exist_ok=True)
                (fresh / "page.ts").write_text("fresh")
            return done
        wc._generated_git = git
        wc.Refresh(("apps/web/.next/types",)).clear(root)
        """,
        str(root),
    )

    assert done.returncode == 0, done.stderr
    assert not (root / "apps/web/.next/types/app/dashboard/page.ts").exists()
    assert (root / "apps/web/.next/types/app/overview/page.ts").read_text() == "fresh"


def test_a_git_timeout_refuses_without_deleting(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    _built(root, "stale")
    done = _python(
        """
        from pathlib import Path
        import subprocess
        import workflow_config as wc
        real = subprocess.run
        def run(command, *args, **kwargs):
            if "--ignored" in command:
                assert kwargs.get("timeout"), "git runs without a deadline"
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return real(command, *args, **kwargs)
        wc.subprocess.run = run
        try:
            wc.Refresh(("apps/web/.next/types", "apps/web/.next/cache")).clear(Path(sys.argv[1]))
        except wc.ConfigError as error:
            print("refused:", error)
        """,
        str(root),
    )

    assert done.returncode == 0, done.stderr
    assert "refused: shipline.json: refresh.clear_generated could not inspect" in done.stdout
    _untouched(root, "stale")


def test_reading_the_config_with_a_large_build_cache_stays_quick(tmp_path: Path) -> None:
    """Every command reads the config, checking the main checkout and its own worktree."""
    root = _checkout(tmp_path)
    (root / "shipline.json").write_text(json.dumps({"refresh": {"clear_generated": GENERATED}}))
    _built(root, "stale")
    _fill(root, 30000)
    started = time.monotonic()
    done = _python(
        """
        from pathlib import Path
        import workflow_config as wc
        wc.load(Path(sys.argv[1]))
        """,
        str(root),
    )
    took = time.monotonic() - started

    assert done.returncode == 0, done.stderr
    print(f"config read with 30000 cached files: {took:.2f}s")
    assert took < 10


def test_a_symbolic_link_in_the_main_checkouts_cache_stops_every_command(
    tmp_path: Path,
) -> None:
    """Low, not reached in use: a build cache holding a link. Recorded, not counted."""
    root = _checkout(tmp_path)
    (root / "shipline.json").write_text(json.dumps({"refresh": {"clear_generated": GENERATED}}))
    _built(root, "stale")
    (root / "apps/web/.next/cache/link").symlink_to(root / "apps/web/app")
    done = _python(
        """
        from pathlib import Path
        import workflow_config as wc
        try:
            wc.load(Path(sys.argv[1]))
        except wc.ConfigError as error:
            print("refused:", error)
        """,
        str(root),
    )

    assert "refused:" in done.stdout
    assert (root / "apps/web/app/page.tsx").exists()


def test_a_dev_server_writing_the_cache_does_not_stop_reading_or_clearing(
    tmp_path: Path,
) -> None:
    """The main checkout's dev server rewrites its cache while commands read the config."""
    root = _checkout(tmp_path)
    (root / "shipline.json").write_text(json.dumps({"refresh": {"clear_generated": GENERATED}}))
    _built(root, "stale")
    _fill(root, 3000)
    churn = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(
            """
            import os, shutil, sys, time
            from pathlib import Path
            cache = Path(sys.argv[1]) / "apps/web/.next/cache/webpack"
            end = time.monotonic() + 20
            index = 0
            while time.monotonic() < end:
                folder = cache / f"pack-{index % 7}"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{index}.pack").write_text("x")
                if index % 5 == 0:
                    shutil.rmtree(cache / f"pack-{(index + 3) % 7}", ignore_errors=True)
                index += 1
            """
        ), str(root)],
    )
    try:
        errors = []
        for _ in range(8):
            done = _python(
                """
                from pathlib import Path
                import workflow_config as wc
                config = wc.load(Path(sys.argv[1]))
                config.refresh.clear(Path(sys.argv[1]))
                """,
                str(root),
            )
            if done.returncode:
                errors.append(done.stderr.strip().splitlines()[-1])
    finally:
        churn.kill()
        churn.wait()

    assert errors == []
    assert (root / "apps/web/.next/server/app/dashboard.html").read_text() == "stale"
    assert not (root / "apps/web/.next/types").exists()
