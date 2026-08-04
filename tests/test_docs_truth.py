"""Fail the build when the docs drift from the code.

These tests cannot police prose -- that is what review is for. What they can police is
the small set of claims that are mechanically checkable: which adapters exist, which CLI
commands exist, and which check templates exist. Those are exactly the claims that went
stale last time.
"""
from __future__ import annotations
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
STATUS = ROOT / "docs" / "STATUS.md"
JOURNEYS = ROOT / "docs" / "user-journeys.md"


def _adapter_table_row(doc: str, kind: str) -> str:
    """Return the `| Target | ... |` row from the README adapters table."""
    match = re.search(rf"^\|\s*{kind}\s*\|(.+)\|\s*$", doc, re.MULTILINE | re.IGNORECASE)
    assert match, f"no {kind} row found in the adapters table"
    return match.group(1)


def _backticked(text: str) -> set[str]:
    return set(re.findall(r"`([a-z_]+)`", text))


# ── adapters ────────────────────────────────────────────────────────────────

def test_readme_target_adapters_match_the_registry():
    from assay.adapters.registry import _TARGETS
    claimed = _backticked(_adapter_table_row(README.read_text(), "Target"))
    assert claimed == set(_TARGETS), (
        f"README target adapters {sorted(claimed)} != registry {sorted(_TARGETS)}. "
        "Update the adapters table in README.md and docs/STATUS.md."
    )


def test_readme_judge_adapters_match_the_registry():
    from assay.adapters.registry import _JUDGES
    claimed = _backticked(_adapter_table_row(README.read_text(), "Judge"))
    assert claimed == set(_JUDGES), (
        f"README judge adapters {sorted(claimed)} != registry {sorted(_JUDGES)}."
    )


def test_status_page_lists_every_registered_target_adapter():
    from assay.adapters.registry import _TARGETS
    status = STATUS.read_text()
    for name in _TARGETS:
        assert f"| `{name}` |" in status, (
            f"adapter '{name}' is registered but has no row in docs/STATUS.md"
        )


# ── CLI ─────────────────────────────────────────────────────────────────────

def _documented_cli_commands() -> set[str]:
    """Commands the docs mention as `assay <verb>`."""
    found: set[str] = set()
    for doc in (README, STATUS):
        found |= set(re.findall(r"`?assay ([a-z]+)", doc.read_text()))
    return found - {"serve"}  # serve is a server entrypoint, checked separately


def _planned_cli_commands() -> set[str]:
    """Verbs docs/STATUS.md explicitly declares unimplemented."""
    match = re.search(r"\*\*Planned:\*\*(.+?)(?:\n\n|\Z)", STATUS.read_text(), re.DOTALL)
    return _backticked(match.group(1)) if match else set()


def _implemented_cli_commands() -> set[str]:
    import assay.cli as cli
    from typer.main import get_command
    return set(get_command(cli.app).commands)


def test_every_documented_cli_command_exists_or_is_declared_planned():
    """A doc may name a command that doesn't exist, but only if STATUS says it's planned."""
    missing = _documented_cli_commands() - _implemented_cli_commands() - _planned_cli_commands()
    assert not missing, (
        f"docs reference CLI commands that do not exist: {sorted(missing)}. "
        "Either implement them or list them under **Planned:** in docs/STATUS.md."
    )


def test_planned_list_does_not_claim_an_implemented_command_is_missing():
    """The inverse drift: a command gets built but STATUS still calls it planned."""
    stale = _planned_cli_commands() & _implemented_cli_commands()
    assert not stale, (
        f"docs/STATUS.md lists {sorted(stale)} as Planned, but they are implemented."
    )


def test_serve_command_exists():
    assert "serve" in _implemented_cli_commands()


def test_planned_cli_commands_are_not_presented_as_available():
    """`watch` and `export` are designed but unimplemented -- they must stay Planned."""
    implemented = _implemented_cli_commands()
    status = STATUS.read_text()
    for verb in ("watch", "export"):
        if verb not in implemented:
            assert re.search(rf"\*\*Planned:\*\*[^\n]*\b{verb}\b", status), (
                f"'assay {verb}' is not implemented and is not listed as Planned "
                "in the CLI section of docs/STATUS.md"
            )


# ── check templates ─────────────────────────────────────────────────────────

def test_template_count_claim_matches_the_registry():
    from assay.checks.library import REGISTRY
    status = STATUS.read_text()
    match = re.search(r"(\d+) (?:of the \d+ designed )?primitives", status)
    assert match, "docs/STATUS.md no longer states a template primitive count"
    assert int(match.group(1)) == len(REGISTRY), (
        f"docs/STATUS.md claims {match.group(1)} template primitives, "
        f"registry has {len(REGISTRY)}"
    )


# ── structural ──────────────────────────────────────────────────────────────

def test_status_and_journeys_are_cross_linked():
    assert "STATUS.md" in JOURNEYS.read_text()
    assert "user-journeys.md" in STATUS.read_text()


def test_readme_points_at_the_status_page():
    assert "docs/STATUS.md" in README.read_text(), (
        "README must link to docs/STATUS.md so capability claims stay traceable"
    )


@pytest.mark.parametrize("marker", ["**Built**", "**Partial**", "**Planned**"])
def test_status_page_uses_its_own_markers(marker):
    assert marker in STATUS.read_text()


def test_stale_sprint_plan_is_archived():
    assert not (ROOT / "assay-ui-sprint-plan-prompt.md").exists(), (
        "the stale UI sprint plan must stay in docs/archive/"
    )
    archived = ROOT / "docs" / "archive" / "assay-ui-sprint-plan-prompt.md"
    assert archived.exists()
    assert "ARCHIVED" in archived.read_text().split("\n")[0]
