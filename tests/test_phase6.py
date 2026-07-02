"""Phase 6: metric catalogue + threshold scoring."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.delenv("ASSAY_LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("ASSAY_AUTH", raising=False)
    monkeypatch.delenv("ASSAY_SECRET_KEY", raising=False)
    import assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db
    init_db()
    yield


@pytest.fixture
def client(_tmp_db):
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


# ── 1. CheckResult threshold logic ───────────────────────────────────────────

def test_graded_metric_passes_by_threshold():
    from assay.checks.base import from_raw
    result = from_raw({"passed": False, "score": 0.85}, "judge:primary", True,
                      type="judge", threshold=0.80)
    assert result.passed is True
    assert result.threshold == 0.80
    assert result.score == 0.85


def test_graded_metric_fails_by_threshold():
    from assay.checks.base import from_raw
    result = from_raw({"passed": True, "score": 0.72}, "judge:primary", True,
                      type="judge", threshold=0.80)
    assert result.passed is False
    assert result.threshold == 0.80


def test_from_raw_no_threshold_uses_raw_passed():
    """Without a threshold, the raw passed flag is honoured as-is."""
    from assay.checks.base import from_raw
    result = from_raw({"passed": True, "score": 0.30}, "judge:primary", True, type="judge")
    assert result.passed is True
    assert result.threshold is None


def test_threshold_stored_in_case_result():
    """After a run, check dicts in CaseResult carry threshold when set."""
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import CaseResult
    pipe = create_pipeline(project="p", name="thresh-pipe")
    config = {
        "version": 1, "project": "thresh-pipe", "target": {"adapter": "mock"},
        "judges": {"primary": {"provider": "mock", "model": "mock"}},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [
                {"type": "template", "uses": "valid_json", "with": {"threshold": 0.75}}
            ]}
        ]}],
        "gating": {},
    }
    pv = create_version(pipe.id, config, {}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).first()
        assert cr.checks[0]["threshold"] == 0.75


# ── 2. Metric catalogue in the UI ────────────────────────────────────────────

def test_catalogue_renders_define_step(client):
    """GET /pipelines/new shows catalogue labels and threshold hints."""
    resp = client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "RAG faithfulness" in resp.text
    assert "Toxicity-free" in resp.text
    assert "Hallucination-free" in resp.text
    assert "Task completion" in resp.text
    # Stochastic chips must carry threshold hint
    assert "≥0.9" in resp.text or "≥0.90" in resp.text
    assert "≥0.8" in resp.text or "≥0.80" in resp.text


def test_preview_returns_threshold_for_judge_intents(client):
    """Preview endpoint includes threshold on judge intents (heuristic path)."""
    resp = client.post("/pipelines/preview", json={
        "requirements": "Output must be free from toxic or harmful language."
    })
    assert resp.status_code == 200
    data = resp.json()
    judge_checks = [c for c in data["checks"] if c.get("type") == "judge"]
    assert judge_checks, "expected at least one judge check for toxicity requirement"
    assert any(c.get("threshold") is not None for c in judge_checks)
