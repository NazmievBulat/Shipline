"""Keep one hunt's app settings, separate from the hunter's environment."""

import json
import os
import sys
from pathlib import Path

from workflow_config import ConfigError, _settings, load


def header(report: Path, area: str, settings: dict[str, str]) -> str:
    # Escape backticks so a value cannot close the report's JSON fence.
    recorded = json.dumps(settings, indent=2).replace("`", "\\u0060")
    return (f"# Hunt {report.name}\n\nArea: {area or 'every screen'}\n\n"
            f"## Settings\n\n```json\n{recorded}\n```\n\n")


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
    except OSError:
        # Exclusive creation succeeded: only this operation's partial file is removed.
        path.unlink()
        raise


def prepare(worktree: Path, report: Path, area: str) -> None:
    config = load()
    settings = {**config.hunts.settings, **config.hunts.area_settings.get(area, {})}
    snapshot = config.state(worktree) / "hunt-settings.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    # The snapshot is data, never shell source. Keep installation values private.
    write_private(snapshot, json.dumps(settings) + "\n")
    try:
        write_private(report / "report.md", header(report, area, settings))
    except OSError:
        snapshot.unlink()
        raise


def discard(worktree: Path, report: Path, area: str) -> None:
    snapshot = load().state(worktree) / "hunt-settings.json"
    settings = json.loads(snapshot.read_text())
    path = report / "report.md"
    # A hunter may have written findings before its launch failed. Preserve those.
    if path.exists() and path.read_text() == header(report, area, settings):
        path.unlink()


def run(snapshot: Path, words: list[str]) -> None:
    settings = _settings(json.loads(snapshot.read_text()), "hunt settings") if snapshot.exists() else {}
    # Only the app receives these variables, including on fresh and restart calls.
    os.execvpe(words[0], words, {**os.environ, **settings})


def main() -> int:
    try:
        if sys.argv[1] == "prepare":
            prepare(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        elif sys.argv[1] == "discard":
            discard(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        elif sys.argv[1] == "run":
            run(Path(sys.argv[2]), sys.argv[3:])
        else:
            raise ValueError("unknown hunt settings command")
    except (ConfigError, OSError, ValueError) as error:
        # Never echo an application's environment or an invalid snapshot's contents.
        print(str(error) if isinstance(error, ConfigError) else "could not use hunt settings", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
