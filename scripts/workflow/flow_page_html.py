"""The flow page's HTML, from the numbers `flow_page.py` computes: static, no JavaScript, read on a
phone. Every word that came from GitHub, the journal or the wake log is escaped here."""

from html import escape
from typing import Any

# The page reloads itself this often, in seconds.
REFRESH_SECONDS = 300

STYLE = """
:root { color-scheme: light dark; --text: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7;
  --bar: #3b6fd8; --blocked: #b3261e; --back: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --text: #f2f2f7; --muted: #a1a1a6; --line: #3a3a3c; --bar: #6f9bff;
    --blocked: #ff8a80; --back: #121214; }
}
body { margin: 0 auto; max-width: 52rem; padding: 0 16px 32px; background: var(--back);
  color: var(--text); font: 15px/1.45 system-ui, -apple-system, sans-serif; }
h1 { font-size: 1.1rem; margin: 16px 0 4px; }
h2 { font-size: 1rem; margin: 24px 0 4px; border-top: 1px solid var(--line); padding-top: 12px; }
h3 { font-size: .95rem; margin: 12px 0 4px; }
.now { font-size: 1.15rem; font-weight: 600; margin: 4px 0 8px; }
.source, .muted { color: var(--muted); font-size: .85rem; margin: 0 0 6px; }
.error { color: var(--blocked); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: 3px 6px 3px 0; border-bottom: 1px solid var(--line);
  vertical-align: top; }
td.n, th.n { text-align: right; white-space: nowrap; }
.blocked { color: var(--blocked); }
.bar { display: inline-block; height: .7em; background: var(--bar); vertical-align: middle; }
ul { margin: 4px 0; padding-left: 18px; font-size: .85rem; }
"""


def _text(value: Any) -> str:
    return escape(str(value), quote=True)


def _clock(moment: str | None) -> str:
    return "–" if not moment else f"{moment[11:16]} UTC"


def _when(moment: str) -> str:
    """A day and a time: a gap can cross midnight."""
    return f"{moment[5:10]} {moment[11:16]}"


def _hours(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{round(value * 60)} min" if value < 1 else f"{value:.1f} h"


def _number(value: float | None, form: str = "{:.1f}") -> str:
    return "–" if value is None else form.format(value)


def _bar(percent: int) -> str:
    return f'<span class="bar" style="width:{percent}%"></span>'


def _source(numbers: dict[str, Any], *names: str) -> str:
    said = []
    for name in names:
        read = numbers["sources"].get(name)
        if not read:
            continue
        line = _text(f"{read['source']}, read {_clock(read['read_at'])}")
        if read["error"]:
            line += f' — <span class="error">not read: {_text(read["error"])}</span>'
        said.append(line)
    return f'<p class="source">Source: {"; ".join(said)}</p>'


def _table(head: list[str], rows: list[list[str]], numeric: set[int]) -> str:
    cells = "".join(
        f'<th class="n">{_text(name)}</th>' if index in numeric else f"<th>{_text(name)}</th>"
        for index, name in enumerate(head)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="n">{cell}</td>' if index in numeric else f"<td>{cell}</td>"
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f'<div class="scroll"><table><tr>{cells}</tr>{body}</table></div>'


def _work(numbers: dict[str, Any]) -> str:
    parts = ["<h2>Work now</h2>", _source(numbers, "open pull requests", "merge train")]
    for stage, found in numbers["stages"].items():
        items = found["items"]
        parts.append(f"<h3>{_text(stage)}: {len(items)}</h3>")
        if not items:
            continue
        rows = [
            [
                f"#{_text(item['number'])}",
                _text(item["title"] or ""),
                _hours(item["hours"]),
                _text(item["priority"]),
                f'<span class="blocked">{_text(item["blocked"])}</span>' if item["blocked"] else "",
            ]
            for item in items
        ]
        parts.append(_table(["", "title", "in stage", "priority", "blocked"], rows, {2}))
    return "".join(parts)


def _merged(numbers: dict[str, Any]) -> str:
    parts = ["<h2>Merged in 24 hours</h2>", _source(numbers, "merged pull requests")]
    merged = numbers["merged"]
    if merged is None:
        return "".join(parts)
    rows = [
        [
            _text(stage),
            _text(found["count"]),
            _number(found["median"]),
            _number(found["p90"]),
        ]
        for stage, found in merged["stages"].items()
    ]
    parts.append(
        f'<p class="muted">{len(merged["numbers"])} merged; sent back at least once:'
        f" {_number(merged['sent_back_share'], '{:.0%}')}</p>"
    )
    parts.append(_table(["stage", "pull requests", "median h", "90th h"], rows, {1, 2, 3}))
    return "".join(parts)


def _messages(title: str, said: dict[str, int] | None) -> str:
    if said is None:
        return f"<li>{_text(title)}: not read</li>"
    if not said:
        return f"<li>{_text(title)}: nothing</li>"
    lines = "".join(f"<li>{count} × {_text(message)}</li>" for message, count in said.items())
    return f"<li>{_text(title)}:<ul>{lines}</ul></li>"


def _line(numbers: dict[str, Any]) -> str:
    parts = [
        "<h2>Merge line</h2>",
        _source(numbers, "merged pull requests", "journal", "wake log"),
    ]
    line = numbers["line"]
    if line["per_hour"] is None:
        return "".join(parts)
    most = max([hour["merges"] for hour in line["per_hour"]] + [1])
    rows = [
        [
            _text(_when(hour["from"])),
            _text(hour["merges"]),
            _bar(hour["merges"] * 100 // most),
        ]
        for hour in line["per_hour"]
    ]
    parts.append("<h3>Merges an hour</h3>")
    parts.append(_table(["hour from", "merges", ""], rows, {1}))
    parts.append(f"<h3>Gaps of over an hour: {len(line['gaps'])}</h3>")
    for gap in line["gaps"]:
        start = "the window's start" if gap["edge"] else _when(gap["from"])
        end = "now" if gap["open"] else _when(gap["until"])
        check_ins = gap["check_ins"] if gap["check_ins"] is not None else "not read"
        parts.append(
            f"<p><strong>{_text(start)} to {_text(end)}: {_hours(gap['hours'])}</strong></p><ul>"
            f"<li>check-ins: {_text(check_ins)}</li>"
            + _messages("wake log", gap["wake"])
            + _messages("journal", gap["journal"])
            + "</ul>"
        )
    return "".join(parts)


def _ci(numbers: dict[str, Any]) -> str:
    parts = ["<h2>CI</h2>", _source(numbers, "runs")]
    ci = numbers["ci"]
    if ci is None:
        return "".join(parts)
    if ci["unread_jobs"]:
        parts.append(
            f'<p class="muted">The jobs of {ci["unread_jobs"]} runs are not read yet: at most 40'
            " a pass.</p>"
        )
    if ci["capped"]:
        parts.append('<p class="muted">GitHub lists at most 1,000 runs: some are left out.</p>')
    head = ["", "runs", "median min", "wait median", "wait 90th", "failed", "cancelled"]
    for workflow in ci["workflows"]:
        running = f", {workflow['running']} running" if workflow["running"] else ""
        parts.append(f"<h3>{_text(workflow['name'])}: {workflow['runs']} runs{running}</h3>")
        rows = [
            [
                f"<strong>{_text(row['name'])}</strong>" if row is workflow else _text(row["name"]),
                _text(row["runs"]),
                _number(row["median_minutes"]),
                _number(row["wait_median"]),
                _number(row["wait_p90"]),
                _text(row["failures"]),
                _text(row["cancelled"]),
            ]
            for row in (workflow, *workflow["jobs"])
        ]
        parts.append(_table(head, rows, {1, 2, 3, 4, 5, 6}))
    return "".join(parts)


def _machines(numbers: dict[str, Any]) -> str:
    parts = ["<h2>Machines</h2>", _source(numbers, "machines")]
    for machine in numbers["machines"]:
        if machine["at"] is None:
            parts.append(f'<h3>{_text(machine["name"])}</h3><p class="muted">No answer yet.</p>')
            continue
        when = "" if machine["fresh"] else f", last answer {_clock(machine['at'])}"
        share = machine["load"] / machine["processors"] if machine["processors"] else None
        parts.append(
            f"<h3>{_text(machine['name'])}{_text(when)}</h3>"
            f'<p class="muted">load {_number(machine["load"], "{:.2f}")} on'
            f" {_text(machine['processors'])} processors ({_number(share, '{:.0%}')});"
            f" memory available {_number(machine['available_gb'])} of"
            f" {_number(machine['total_gb'])} GB</p>"
        )
        rows = [
            [
                _text(_when(hour["from"])),
                _number(hour["load_share"], "{:.0%}"),
                _bar(min(100, round((hour["load_share"] or 0) * 100))),
                _number(hour["available_gb"]),
            ]
            for hour in machine["hours"]
            if hour["load_share"] is not None or hour["available_gb"] is not None
        ]
        if rows:
            parts.append(
                _table(["hour from", "load", "of processors", "available GB, lowest"], rows, {1, 3})
            )
    return "".join(parts)


def render(numbers: dict[str, Any]) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        f"<title>Workflow flow</title><style>{STYLE}</style></head><body>"
        f"<h1>Workflow flow, {_text(_when(numbers['now']))} UTC</h1>"
        f'<p class="now">{_text(numbers["bottleneck"])}</p>'
        + _work(numbers)
        + _merged(numbers)
        + _line(numbers)
        + _ci(numbers)
        + _machines(numbers)
        + "</body></html>\n"
    )
