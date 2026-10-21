"""Keep docs/user-journeys.md from citing things that do not exist.

The journey tables are only useful if they can be trusted. A route or a
`module.function` that reads plausibly but is not real is worse than no
documentation at all, because a reader will act on it.

So: every route the document cites must be mounted on the FastAPI app, and every
`assay.<module>.<symbol>` it cites must be importable.

Rows marked **MISSING** or **BROKEN** are exempt from the *existence* check --
naming the thing that ought to exist is exactly what those markers are for
(`generator.codegen.generate_check`, `pricing.estimate_cost`,
`GET /pipelines/{pid}/runs`). Their prose is policed by review, not by pytest.

Like tests/test_docs_truth.py, this cannot police claims. It polices names.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JOURNEYS = ROOT / "docs" / "user-journeys.md"

# Top-level modules under `assay/`. A citation must start with one of these for us
# to treat it as a code reference rather than prose like `Report.state` or
# `model.config.*`.
_PACKAGES = {
    "adapters", "checks", "cli", "config", "engine", "generator", "judges",
    "llm", "notifications", "pipeline", "reporting", "sandbox", "server",
    "spec", "store",
}

# `GET /reports/{id}/view`, `POST /pipelines/generate`. Requires a literal "/" after
# the method, so the document's abbreviated `POST …/adjudicate` form is skipped
# rather than mis-parsed.
_ROUTE = re.compile(r"`(GET|POST|PATCH|PUT|DELETE) (/[^`\s]*)`")

# `engine.review.adjudicate_case:192`, `llm.provider.read_key`, `config.DB_URL:7`.
# The trailing line reference is optional and may be a range.
_SYMBOL = re.compile(r"`([a-z_]+(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?::\d+(?:-\d+)?)?`")

# The document also cites bare files (`cli.py:186`, `sandbox/runner.py`). Slashes
# already exclude most of them; a trailing extension catches the rest.
_FILE_SUFFIXES = (".py", ".html", ".yml", ".yaml", ".json", ".md", ".jsonl", ".txt")

_EXEMPT = ("**MISSING**", "**BROKEN**")


def _citable_lines() -> list[str]:
    """Document lines whose citations must resolve."""
    return [ln for ln in JOURNEYS.read_text().splitlines()
            if not any(marker in ln for marker in _EXEMPT)]


def _cited_routes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for line in _citable_lines():
        for method, path in _ROUTE.findall(line):
            found.add((method, path.split("?", 1)[0]))
    return found


def _cited_symbols() -> set[str]:
    found: set[str] = set()
    for line in _citable_lines():
        for dotted in _SYMBOL.findall(line):
            head = dotted.split(".", 1)[0]
            if head not in _PACKAGES or dotted.endswith(_FILE_SUFFIXES):
                continue
            found.add(dotted)
    return found


def _app_routes() -> list[tuple[set[str], list[str]]]:
    """(methods, path segments) for every route mounted on the app."""
    from assay.server.app import app
    out = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            out.append((set(methods), path.strip("/").split("/")))
    return out


def _segments_match(cited: list[str], actual: list[str]) -> bool:
    """Path templates match when literals are equal and placeholders line up.

    The document names its parameters for readability (`{pid}`, `{vid}`, `{cid}`);
    the app names them for the handler signature (`{pipeline_id}`, `{version_id}`,
    `{check_path:path}`). Only the shape is load-bearing.
    """
    if len(cited) != len(actual):
        return False
    return all(
        (c.startswith("{") and a.startswith("{")) or c == a
        for c, a in zip(cited, actual)
    )


def _resolve(dotted: str) -> object:
    """Import `assay.<dotted>`, walking attributes once the module path runs out."""
    parts = dotted.split(".")
    module = None
    consumed = 0
    for i in range(len(parts), 0, -1):
        try:
            module = importlib.import_module("assay." + ".".join(parts[:i]))
            consumed = i
            break
        except ImportError:
            continue
    if module is None:
        raise ImportError(f"no importable module prefix in assay.{dotted}")
    obj = module
    for attr in parts[consumed:]:
        obj = getattr(obj, attr)          # AttributeError is the failure we want
    return obj


# ── the checks ──────────────────────────────────────────────────────────────

def test_the_document_actually_cites_things():
    """Guard the regexes: a silent parse failure would make every test below vacuous."""
    assert len(_cited_routes()) >= 25, "route extraction found suspiciously little"
    assert len(_cited_symbols()) >= 25, "symbol extraction found suspiciously little"


@pytest.mark.parametrize("method,path", sorted(_cited_routes()))
def test_every_cited_route_exists_on_the_app(method, path):
    cited = path.strip("/").split("/")
    matches = [
        True for methods, actual in _app_routes()
        if method in methods and _segments_match(cited, actual)
    ]
    assert matches, (
        f"docs/user-journeys.md cites `{method} {path}`, which is not mounted on "
        "the FastAPI app. Either fix the citation or mark the row MISSING."
    )


@pytest.mark.parametrize("dotted", sorted(_cited_symbols()))
def test_every_cited_symbol_is_importable(dotted):
    try:
        _resolve(dotted)
    except (ImportError, AttributeError) as exc:
        pytest.fail(
            f"docs/user-journeys.md cites `{dotted}`, which does not resolve: {exc}. "
            "Either fix the citation or mark the row MISSING."
        )


def test_every_journey_in_the_index_has_a_section():
    """The index and the body must not drift apart as actors are added."""
    text = JOURNEYS.read_text()
    indexed = set(re.findall(r"^\| \[(J\d+)\]", text, re.MULTILINE))
    sectioned = set(re.findall(r"^## (J\d+) — ", text, re.MULTILINE))
    assert indexed == sectioned, (
        f"journey index and sections disagree: "
        f"indexed-only {sorted(indexed - sectioned)}, "
        f"sectioned-only {sorted(sectioned - indexed)}"
    )


def test_every_ranked_gap_names_a_real_journey_step():
    """A gap row must point at a journey that exists, so the table stays auditable."""
    text = JOURNEYS.read_text()
    sectioned = set(re.findall(r"^## (J\d+) — ", text, re.MULTILINE))
    table = text.split("## Ranked gaps", 1)[1]
    for row in re.findall(r"^\| (?:\d+|—) \| .+$", table, re.MULTILINE):
        for journey in re.findall(r"\bJ(\d+)(?:\.[\d\-]+)?", row):
            assert f"J{journey}" in sectioned, (
                f"ranked-gaps row cites J{journey}, which has no section: {row}"
            )
