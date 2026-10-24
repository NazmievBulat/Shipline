"""Shipline's own development rules: AGENTS.md, with CLAUDE.md, README.md and RULES.md pointing to it."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NO_REPLY = "82903523+NazmievBulat@users.noreply.github.com"


def _read(name: str) -> str:
    return (ROOT / name).read_text()


def _words(text: str) -> str:
    """The text with its line wrapping undone, so a phrase may break across lines."""
    return " ".join(text.split())


def _rules(text: str) -> str:
    """The numbered rules: the text before the first section."""
    return text.split("\n## ", 1)[0]


def _section(text: str, title: str) -> str:
    found = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert found is not None, f"no section {title!r}"
    return _words(found.group(1))


def test_claude_md_only_imports_agents_md() -> None:
    assert _read("CLAUDE.md") == "@AGENTS.md\n"


@pytest.mark.parametrize(
    "said",
    [
        # 1. Pull requests only.
        "Every change is a pull request to `main`.",
        "Nothing is pushed to `main` directly.",
        "Never force-push.",
        # 2. Tickets.
        "Every ticket is an empty draft pull request.",
        "`ticket/<slug>`",
        "with one empty commit",
        "single source of truth",
        "A job larger than the ticket becomes a new empty draft pull request",
        # 3. The builder.
        "one cloud session per ticket",
        "it never opens another",
        "It adds its choices and runs below the ticket text.",
        # 4. CI.
        "GitHub-hosted runners only",
        # 5. Codex.
        "`@codex review`",
        "It runs no tests and nothing from the pull request.",
        # 6. The breaker.
        "a lock held too long, a lost GitHub check, two calls at once, a partial re-run",
        "`break/<N>`",
        "`<!-- shipline-breaker -->`",
        "`verdict: survived`",
        "`verdict: broke`",
        "Re-running the suite is CI's job.",
        # 7. The reviewer.
        "never reads the builder's transcript",
        "Nothing merges with a finding open.",
        "`<!-- shipline-review result=sent-back -->`, with the pull request back to draft",
        "`<!-- shipline-review result=passed -->`",
        "squash-merges the pull request with GitHub's own merge",
        "Shipline uses no merge gate or merge line of its own for itself.",
        # 8. Authorship.
        f'"Bulat Nazmiev <{NO_REPLY}>"',
        "`git config user.name`",
        "`git config user.email`",
        # 9. The owner's word.
        "Never done without the owner's word: a force-push, a change to text that is sent to a model, "
        "and any product decision.",
        # 10. Personal data.
        "`example-app`",
        # 11. Outside contributions.
        "issues are open now.",
        "Pull requests from others are taken after the first release",
        "the owner approves a newcomer's CI before it runs",
    ],
)
def test_agents_md_states_how_shipline_is_developed(said: str) -> None:
    assert said in _words(_rules(_read("AGENTS.md")))


def test_agents_md_numbers_eleven_rules() -> None:
    numbers = re.findall(r"^(\d+)\. ", _rules(_read("AGENTS.md")), re.M)

    assert numbers == [str(n) for n in range(1, 12)]


def test_agents_md_never_allows_a_self_hosted_runner() -> None:
    assert "self-hosted runner is never used" in _words(_rules(_read("AGENTS.md")))


@pytest.mark.parametrize(
    "said",
    [
        "`<!-- shipline-worker --> question`",
        "Carry on with what does not depend on it.",
        "`<!-- shipline-orchestrator -->`",
        "never asks the owner",
        '"Choices"',
        "a failing test",
        "(the command and the last lines) go in the description",
        "the whole suite passes",
        "a reviewer subagent",
        "`<!-- shipline-worker --> ready`",
        "`<!-- shipline-worker --> Blocked`",
    ],
)
def test_agents_md_says_how_a_ticket_is_built(said: str) -> None:
    assert said in _section(_read("AGENTS.md"), "Building a ticket")


@pytest.mark.parametrize(
    "said",
    [
        "Codex included",
        "Besides correctness",
        "personal data",
        "secrets or tokens",
        "host or machine names",
        "e-mail addresses other than a GitHub no-reply address",
        "IP addresses",
        "home paths",
        "session or chat links",
        "injection through commands, paths or shell quoting",
        "secrets reaching logs or comments",
        "unsafe permissions on files the scripts write",
        "timeouts",
        "a fork's pull request",
    ],
)
def test_agents_md_holds_the_review_guidelines(said: str) -> None:
    assert said in _section(_read("AGENTS.md"), "Review guidelines")


def test_readme_says_how_it_is_built_and_points_to_agents_md() -> None:
    assert "[`AGENTS.md`](AGENTS.md)" in _section(_read("README.md"), "How it is built")


def test_rules_md_says_at_its_top_whose_rules_it_holds() -> None:
    first = _read("scripts/workflow/RULES.md").splitlines()[0]

    assert "repositories that use Shipline" in first
    assert "`AGENTS.md` in the Shipline repository" in first
