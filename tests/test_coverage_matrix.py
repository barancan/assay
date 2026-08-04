"""Requirement coverage, in both directions.

Counting cases per requirement only describes requirements that were tested. The
question a reviewer actually needs answered before signing off is the inverse: what
has *nothing* testing it? A requirement with no case is invisible in a one-directional
matrix. The mirror case matters too -- a test citing a requirement that no longer
exists has quietly stopped meaning anything.
"""
from __future__ import annotations

import pytest

from assay.reporting.exporters import _coverage_lines, coverage

REQUIREMENTS = [
    {"id": "R1", "text": "Responses must be valid JSON.", "section": None},
    {"id": "R2", "text": "Responses must arrive under 5 seconds.", "section": None},
    {"id": "R3", "text": "Responses must not contain PII.", "section": None},
]


def _data(cases, requirements=REQUIREMENTS):
    return {"requirements": requirements, "cases": cases}


def _case(ref, passed=True):
    return {"suite": ref or "s", "case": f"c-{ref}", "requirement_ref": ref,
            "passed": passed, "latency_ms": 1.0, "checks": [], "response": {}}


def test_uncovered_requirements_are_named():
    cov = coverage(_data([_case("R1")]))
    assert cov["uncovered"] == ["R2", "R3"]
    assert cov["requirements_covered"] == 1
    assert cov["requirements_total"] == 3


def test_full_coverage_leaves_nothing_uncovered():
    cov = coverage(_data([_case("R1"), _case("R2"), _case("R3")]))
    assert cov["uncovered"] == []
    assert cov["covered_pct"] == 100.0


def test_orphan_test_is_flagged():
    """A case citing a requirement that no longer exists."""
    cov = coverage(_data([_case("R1"), _case("R9")]))
    assert cov["orphans"] == ["R9"]


def test_case_with_no_ref_is_an_orphan():
    cov = coverage(_data([{"suite": "s", "case": "c", "requirement_ref": None,
                           "passed": True, "latency_ms": 1.0, "checks": [], "response": {}}]))
    assert "(unmapped)" in cov["orphans"]


def test_failing_case_still_counts_as_covered():
    """Covered means tested, not passing -- they are different questions."""
    cov = coverage(_data([_case("R1", passed=False)]))
    entry = next(r for r in cov["by_requirement"] if r["id"] == "R1")
    assert entry["covered"] is True
    assert entry["passed"] == 0
    assert entry["total"] == 1


def test_percentage_is_rounded_sensibly():
    cov = coverage(_data([_case("R1"), _case("R2")]))
    assert cov["covered_pct"] == 66.7


def test_multiple_cases_per_requirement_aggregate():
    cov = coverage(_data([_case("R1"), _case("R1", passed=False), _case("R1")]))
    entry = next(r for r in cov["by_requirement"] if r["id"] == "R1")
    assert (entry["total"], entry["passed"]) == (3, 2)


# ── the degenerate case ─────────────────────────────────────────────────────

def test_missing_requirement_list_is_reported_honestly():
    """Without the requirements we cannot know what is uncovered -- and must say so."""
    cov = coverage(_data([_case("R1")], requirements=[]))
    assert cov["known_requirements"] is False
    assert cov["covered_pct"] is None

    rendered = "\n".join(_coverage_lines(_data([_case("R1")], requirements=[])))
    assert "unavailable" in rendered
    # Must NOT imply full coverage.
    assert "100" not in rendered
    assert "NOT COVERED" not in rendered


def test_no_cases_at_all_means_nothing_is_covered():
    cov = coverage(_data([]))
    assert cov["uncovered"] == ["R1", "R2", "R3"]
    assert cov["covered_pct"] == 0.0


# ── rendering ───────────────────────────────────────────────────────────────

def test_markdown_names_the_uncovered_requirement():
    rendered = "\n".join(_coverage_lines(_data([_case("R1"), _case("R2")])))
    assert "NOT COVERED" in rendered
    assert "Responses must not contain PII." in rendered
    assert "2/3 requirements covered" in rendered


def test_markdown_flags_orphans():
    rendered = "\n".join(_coverage_lines(_data([_case("R9")])))
    assert "Orphan tests" in rendered
    assert "`R9`" in rendered


# ── end to end through a real export ────────────────────────────────────────

@pytest.fixture
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


def test_export_reads_requirements_from_the_pipeline_version(_tmp_db):
    """The matrix needs the requirement text, which lives on the version config."""
    from assay.engine import execute_run
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.reporting.exporters import _gather
    from assay.store.db import session_scope
    from assay.store.models import User

    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))

    config = {
        "version": 1, "project": "cov", "target": {"adapter": "mock"},
        "judges": {}, "gating": {},
        "requirements": "- Responses must be valid JSON.\n- Responses must not contain PII.\n",
        "suites": [{"id": "R1", "requirement_ref": "R1", "cases": [
            {"id": "c1", "input": {"prompt": "hi"},
             "checks": [{"type": "template", "uses": "valid_json"}]}]}],
    }
    pipe = create_pipeline(project="cov", name="cov")
    version = create_version(pipe.id, config, {}, {})
    activate_version(version.id, "alice")

    run_id = execute_run(pipeline_version_id=version.id)
    data, _ = _gather(run_id)

    assert [r["id"] for r in data["requirements"]] == ["R1", "R2"]
    cov = coverage(data)
    assert cov["uncovered"] == ["R2"], "the untested requirement must surface"


# ── run provenance ──────────────────────────────────────────────────────────

def test_run_records_the_interface_hash(_tmp_db, tmp_path):
    """A report must be able to say what interface it was tested against."""
    import json
    from assay.engine import execute_run
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.store.db import session_scope
    from assay.store.models import Run, TargetModel, User

    collection = tmp_path / "c.postman_collection.json"
    collection.write_text(json.dumps({
        "info": {"name": "c"},
        "item": [{"name": "a", "request": {
            "method": "POST", "url": {"raw": "https://x/a"},
            "body": {"mode": "raw", "raw": json.dumps({"text": "x"})}}}],
    }))

    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))
    config = {
        "version": 1, "project": "iface", "judges": {}, "gating": {},
        "target": {"adapter": "mock", "import": str(collection)},
        "suites": [{"id": "R1", "requirement_ref": "R1", "cases": [
            {"id": "c1", "input": {"prompt": "hi"},
             "checks": [{"type": "template", "uses": "valid_json"}]}]}],
    }
    pipe = create_pipeline(project="iface", name="iface")
    version = create_version(pipe.id, config, {}, {})
    activate_version(version.id, "alice")

    run_id = execute_run(pipeline_version_id=version.id)
    with session_scope() as s:
        run = s.get(Run, run_id)
        assert s.get(TargetModel, run.target_id).interface_hash


def test_no_interface_leaves_the_hash_null(_tmp_db):
    from assay.engine import execute_run
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.store.db import session_scope
    from assay.store.models import Run, TargetModel, User

    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))
    config = {
        "version": 1, "project": "noiface", "judges": {}, "gating": {},
        "target": {"adapter": "mock"},
        "suites": [{"id": "R1", "requirement_ref": "R1", "cases": [
            {"id": "c1", "input": {}, "checks": [{"type": "template", "uses": "valid_json"}]}]}],
    }
    pipe = create_pipeline(project="noiface", name="noiface")
    version = create_version(pipe.id, config, {}, {})
    activate_version(version.id, "alice")

    run_id = execute_run(pipeline_version_id=version.id)
    with session_scope() as s:
        run = s.get(Run, run_id)
        assert s.get(TargetModel, run.target_id).interface_hash is None
