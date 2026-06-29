"""Phase 1: report verdict, suggested verdict, adjudication reason enforcement."""
from __future__ import annotations
import datetime as dt
import pytest


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.delenv("ASSAY_LINEAR_API_KEY", raising=False)
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db
    init_db()
    yield


@pytest.fixture
def client(_tmp_db):
    import importlib
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


def _make_run_and_report():
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Report

    p = create_pipeline(project="p1-test", name="p1-test")
    cfg = {
        "version": 1, "project": "p1-test",
        "target": {"adapter": "mock"}, "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [{"type": "template", "uses": "valid_json"}]}
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        return run_id, rep.id


def _make_run_with_failure():
    """Pipeline with one passing and one failing case; report submitted for review."""
    from assay.pipeline import create_pipeline, create_version
    from assay.engine import execute_run, submit_for_review
    from assay.store import session_scope
    from assay.store.models import Report, CaseResult, PipelineVersion

    p = create_pipeline(project="p1-fail", name="p1-fail")
    cfg = {
        "version": 1, "project": "p1-fail",
        "target": {"adapter": "mock"}, "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "pass-case", "input": {}, "checks": [
                {"type": "template", "uses": "valid_json"}
            ]},
            {"id": "fail-case", "input": {}, "checks": [
                {"type": "template", "uses": "contains", "with": {"value": "NEVER_PRESENT"}}
            ]},
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    with session_scope() as s:
        obj = s.get(PipelineVersion, pv.id)
        obj.status = "active"
        obj.activated_by = "test-setup"
        obj.activated_at = dt.datetime.now(dt.timezone.utc)
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
        cr = s.query(CaseResult).filter_by(run_id=run_id, case_id="fail-case").one()
        cr_id = cr.id
    submit_for_review(rep_id, actor="tester")
    return run_id, rep_id, cr_id


# ── Suggested verdict ──────────────────────────────────────────────────────

def test_suggested_verdict_all_required():
    """all_required policy: one failing case → suggested verdict is 'fail'."""
    from assay.engine.review import compute_suggested_verdict

    _, rep_id, _ = _make_run_with_failure()
    assert compute_suggested_verdict(rep_id) == "fail"


def test_suggested_verdict_threshold():
    """threshold policy value=0.5: 1/2 pass (50%) → at threshold → 'pass'."""
    from assay.pipeline import create_pipeline, create_version
    from assay.engine import execute_run
    from assay.engine.review import compute_suggested_verdict
    from assay.store import session_scope
    from assay.store.models import Report, PipelineVersion

    p = create_pipeline(project="thresh-test", name="thresh-test")
    cfg = {
        "version": 1, "project": "thresh-test",
        "target": {"adapter": "mock"}, "judges": {},
        "pass_policy": {"type": "threshold", "value": 0.5},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "pass-case", "input": {}, "checks": [
                {"type": "template", "uses": "valid_json"}
            ]},
            {"id": "fail-case", "input": {}, "checks": [
                {"type": "template", "uses": "contains", "with": {"value": "NEVER"}}
            ]},
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    with session_scope() as s:
        obj = s.get(PipelineVersion, pv.id)
        obj.status = "active"
        obj.activated_by = "test-setup"
        obj.activated_at = dt.datetime.now(dt.timezone.utc)
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
    assert compute_suggested_verdict(rep_id) == "pass"  # 1/2 = 0.5 >= 0.5


# ── set_verdict ────────────────────────────────────────────────────────────

def test_set_verdict_locks_report():
    """set_verdict sets verdict columns and locks the report."""
    from assay.engine.review import set_verdict
    from assay.engine import submit_for_review
    from assay.store import session_scope
    from assay.store.models import Report

    _, rep_id = _make_run_and_report()
    submit_for_review(rep_id, actor="tester")
    set_verdict(rep_id, "pass", "lgtm", "solo-dev")

    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.locked is True
        assert rep.verdict == "pass"
        assert rep.verdict_reason == "lgtm"
        assert rep.verdict_set_by == "solo-dev"
        assert rep.verdict_set_at is not None


def test_set_verdict_requires_reason():
    """set_verdict with empty reason raises ValueError."""
    from assay.engine.review import set_verdict
    from assay.engine import submit_for_review

    _, rep_id = _make_run_and_report()
    submit_for_review(rep_id, actor="tester")
    with pytest.raises(ValueError, match="reason"):
        set_verdict(rep_id, "pass", "", "solo-dev")


def test_set_verdict_requires_reviewer():
    """set_verdict by a runner-role actor raises PermissionError."""
    from assay.engine.review import set_verdict
    from assay.engine import submit_for_review
    from assay.store import session_scope
    from assay.store.models import User

    # Create run/report while the user table is empty (solo-dev trusted).
    _, rep_id = _make_run_and_report()
    submit_for_review(rep_id, actor="tester")
    # Now seed a runner-only user so _check_reviewer enforces roles.
    with session_scope() as s:
        s.add(User(name="runner1", role="runner"))
    with pytest.raises(PermissionError):
        set_verdict(rep_id, "pass", "lgtm", "runner1")


# ── adjudicate_case reason enforcement ────────────────────────────────────

def test_adjudicate_requires_reason():
    """adjudicate_case with verdict and no reason raises ValueError."""
    from assay.engine.review import adjudicate_case

    _, rep_id, cr_id = _make_run_with_failure()
    with pytest.raises(ValueError, match="reason"):
        adjudicate_case(rep_id, cr_id, "fail", "solo-dev", None)


def test_adjudicate_overturn_requires_reason():
    """Overturning machine fail to human pass also requires a reason."""
    from assay.engine.review import adjudicate_case

    _, rep_id, cr_id = _make_run_with_failure()
    with pytest.raises(ValueError, match="reason"):
        adjudicate_case(rep_id, cr_id, "pass", "solo-dev")  # no reason arg


# ── Routes ─────────────────────────────────────────────────────────────────

def test_set_verdict_route_htmx(client):
    """POST /reports/{id}/set-verdict with HTMX header returns HX-Redirect."""
    _, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})
    resp = client.post(
        f"/reports/{rep_id}/set-verdict",
        json={"verdict": "pass", "reason": "all good"},
        headers={"X-Assay-User": "solo-dev", "HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "HX-Redirect" in resp.headers


def test_adjudicate_route_rejects_empty_reason(client):
    """POST adjudicate with verdict but empty reason returns 422."""
    from assay.store import session_scope
    from assay.store.models import CaseResult

    run_id, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})
    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).first()
        cr_id = cr.id
    resp = client.post(
        f"/reports/{rep_id}/cases/{cr_id}/adjudicate",
        json={"verdict": "fail", "reason": ""},
        headers={"X-Assay-User": "solo-dev"},
    )
    assert resp.status_code == 422


def test_report_detail_shows_suggested_verdict(client):
    """GET report detail HTML shows suggested verdict chip and 'suggested' text."""
    _, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})

    resp = client.get(f"/reports/{rep_id}", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "suggested" in resp.text.lower()
    assert "badge-pass" in resp.text or "badge-fail" in resp.text
