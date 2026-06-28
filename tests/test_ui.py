"""Phase 7: full review UI endpoints."""
from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.delenv("ASSAY_LINEAR_API_KEY", raising=False)
    import importlib
    import assay.config
    import assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


@pytest.fixture
def client(_tmp_db):
    import importlib
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


def _make_run_and_report() -> tuple[int, int]:
    """Create an active pipeline version, run it; return (run_id, report_id)."""
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Report

    p = create_pipeline(project="ui-test", name="ui-test")
    cfg = {
        "version": 1,
        "project": "ui-test",
        "target": {"adapter": "mock"},
        "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [{"type": "template", "uses": "valid_json"}]}
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    # Solo-dev path: User table is empty, so any named actor is trusted.
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        return run_id, rep.id


# ── Queue ──────────────────────────────────────────────────────────────────

def test_queue_shows_reports(client):
    _, rep_id = _make_run_and_report()
    resp = client.get("/")
    assert resp.status_code == 200
    assert str(rep_id) in resp.text


def test_queue_filter_by_state(client):
    _, rep_pending = _make_run_and_report()
    _, rep_done = _make_run_and_report()
    client.post(f"/reports/{rep_done}/submit",
                headers={"X-Assay-User": "solo-dev"})
    client.post(f"/reports/{rep_done}/approve",
                headers={"X-Assay-User": "solo-dev"})

    resp = client.get("/?state=pending")
    assert resp.status_code == 200
    assert f"/reports/{rep_pending}" in resp.text
    assert f"/reports/{rep_done}" not in resp.text


# ── Report detail ──────────────────────────────────────────────────────────

def test_report_detail_shows_cases(client):
    _, rep_id = _make_run_and_report()
    resp = client.get(f"/reports/{rep_id}", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "c1" in resp.text   # case id from fixture config


# ── Adjudication htmx partial ──────────────────────────────────────────────

def test_adjudication_htmx_partial(client):
    from assay.store import session_scope
    from assay.store.models import CaseResult

    run_id, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})

    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).first()
        cr_id = cr.id

    resp = client.post(
        f"/reports/{rep_id}/cases/{cr_id}/adjudicate",
        json={"verdict": "pass", "reason": "looks fine"},
        headers={"X-Assay-User": "solo-dev", "HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    assert f"case-row-{cr_id}" in resp.text


# ── Assign reviewer htmx ───────────────────────────────────────────────────

def test_assign_reviewer_htmx(client):
    _, rep_id = _make_run_and_report()
    resp = client.post(
        f"/reports/{rep_id}/assign",
        json={"reviewer": "alice"},
        headers={"X-Assay-User": "solo-dev", "HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "alice" in resp.text


# ── Approve via UI ─────────────────────────────────────────────────────────

def test_approve_via_ui(client):
    _, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})

    resp = client.post(f"/reports/{rep_id}/approve",
                       headers={"X-Assay-User": "solo-dev"})
    assert resp.status_code == 303
    location = resp.headers["location"]

    resp2 = client.get(location)
    assert resp2.status_code == 200
    # New UI uses sentence case ("Done") and a locked banner
    assert "Done" in resp2.text or "locked" in resp2.text


# ── Locked report rejects adjudication ────────────────────────────────────

def test_locked_report_rejects_adjudication_via_api(client):
    from assay.store import session_scope
    from assay.store.models import CaseResult

    run_id, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})
    client.post(f"/reports/{rep_id}/approve", headers={"X-Assay-User": "solo-dev"})

    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).first()
        cr_id = cr.id

    resp = client.post(
        f"/reports/{rep_id}/cases/{cr_id}/adjudicate",
        json={"verdict": "fail"},
        headers={"X-Assay-User": "solo-dev"},
    )
    assert resp.status_code == 403


# ── Export download ────────────────────────────────────────────────────────

def test_export_download(client):
    _, rep_id = _make_run_and_report()
    resp = client.get(f"/reports/{rep_id}/export/json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]


# ── JSON API still works ───────────────────────────────────────────────────

def test_api_json_still_works(client):
    _, rep_id = _make_run_and_report()
    resp = client.get(f"/reports/{rep_id}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == rep_id
    assert "state" in data
