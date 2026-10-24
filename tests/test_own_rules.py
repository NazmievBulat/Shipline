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
        "single source of truth",
        # 3. The builder.
        "one cloud session per ticket",
        "it never opens another",
        # 4. CI.
        "GitHub-hosted runners only",
        # 5. Codex.
        "`@codex review`",
        # 6. The breaker.
        "`break/<N>`",
        "`<!-- shipline-breaker -->`",
        "`verdict: survived`",
        "`verdict: broke`",
        # 7. The reviewer.
        "never reads the builder's transcript",
        "Nothing merges with a finding open.",
        "`<!-- shipline-review result=sent-back -->`",
        "`<!-- shipline-review result=passed -->`",
        "squash-merges",
        # 8. Authorship.
        f'"Bulat Nazmiev <{NO_REPLY}>"',
        "`git config user.name`",
        "`git config user.email`",
        # 9. The owner's word.
        "a change to text that is sent to a model",
        # 10. Personal data.
        "`example-app`",
        # 11. Outside contributions.
        "issues are open now.",
        "after the first release",
    ],
)
def test_agents_md_states_how_shipline_is_developed(said: str) -> None:
    assert said in _words(_read("AGENTS.md"))


def test_agents_md_never_allows_a_self_hosted_runner() -> None:
    assert "self-hosted runner is never used" in _words(_read("AGENTS.md"))


@pytest.mark.parametrize(
    "said",
    [
        "`<!-- shipline-worker --> question`",
        "`<!-- shipline-orchestrator -->`",
        "never asks the owner",
        '"Choices"',
        "a failing test",
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
        "personal data",
        "IP addresses",
        "home paths",
        "session or chat links",
        "shell quoting",
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
    assert "AGENTS.md" in first


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "README.md"])
def test_the_only_address_in_the_rules_is_the_no_reply_one(name: str) -> None:
    addresses = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", _read(name)))

    assert addresses <= {NO_REPLY}
