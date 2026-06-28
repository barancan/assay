"""Phase 2: reports list, projects grid, pipelines/drafts, step_reached."""
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


def _make_report(project: str = "p2-test"):
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Report

    p = create_pipeline(project=project, name=f"{project}-pipe")
    cfg = {
        "version": 1, "project": project,
        "target": {"adapter": "mock"}, "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [
                {"type": "template", "uses": "valid_json"}
            ]}
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        return run_id, rep.id, p.id, pv.id


# ── Reports list HTML ──────────────────────────────────────────────────────

def test_reports_list_html(client):
    """GET /reports with Accept: text/html returns 200 with the list page."""
    _, rep_id, _, _ = _make_report()
    resp = client.get("/reports", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert str(rep_id) in resp.text
    assert "Filter" in resp.text


def test_reports_filter_by_state(client):
    """GET /reports?state=done shows only done reports."""
    _, rep_pending, _, _ = _make_report("proj-pending")
    _, rep_done, _, _ = _make_report("proj-done")

    from assay.engine import submit_for_review
    from assay.engine.review import set_verdict
    submit_for_review(rep_done, actor="tester")
    set_verdict(rep_done, "pass", "lgtm", "solo-dev")

    resp = client.get("/reports?state=done", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert f"/reports/{rep_done}" in resp.text
    assert f"/reports/{rep_pending}" not in resp.text


def test_reports_filter_by_project(client):
    """GET /reports?project=proj-a shows only that project's reports."""
    _, rep_a, _, _ = _make_report("proj-a")
    _, rep_b, _, _ = _make_report("proj-b")

    resp = client.get("/reports?project=proj-a", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert f"/reports/{rep_a}" in resp.text
    assert f"/reports/{rep_b}" not in resp.text


# ── Projects page ──────────────────────────────────────────────────────────

def test_projects_page(client):
    """GET /projects returns 200 with a project card showing pipeline/report counts."""
    _make_report("myproject")
    resp = client.get("/projects")
    assert resp.status_code == 200
    assert "myproject" in resp.text
    # Card shows pipeline and report counts
    assert "Pipelines" in resp.text
    assert "Reports" in resp.text


# ── Pipelines drafts page ──────────────────────────────────────────────────

def test_pipelines_page_drafts(client):
    """GET /pipelines with Accept: text/html lists draft versions with a Resume link."""
    from assay.pipeline import create_pipeline, create_version
    from assay.store import session_scope
    from assay.store.models import PipelineVersion

    p = create_pipeline(project="drafts-test", name="my-draft-pipe")
    cfg = {
        "version": 1, "project": "drafts-test",
        "target": {"adapter": "mock"}, "judges": {},
        "suites": [], "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    # Leave as draft (don't activate)

    resp = client.get("/pipelines", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "my-draft-pipe" in resp.text
    assert "Resume" in resp.text


# ── step_reached ───────────────────────────────────────────────────────────

def test_draft_step_reached():
    """update_step_reached persists the step to the DB."""
    from assay.pipeline import create_pipeline, create_version, update_step_reached
    from assay.store import session_scope
    from assay.store.models import PipelineVersion

    p = create_pipeline(project="step-test", name="step-pipe")
    cfg = {
        "version": 1, "project": "step-test",
        "target": {"adapter": "mock"}, "judges": {},
        "suites": [], "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    update_step_reached(pv.id, "connect")

    with session_scope() as s:
        obj = s.get(PipelineVersion, pv.id)
        assert obj.step_reached == "connect"


# ── Draft delete ───────────────────────────────────────────────────────────

def test_draft_delete(client):
    """DELETE /pipelines/versions/{id} for a draft version succeeds and removes it."""
    from assay.pipeline import create_pipeline, create_version
    from assay.store import session_scope
    from assay.store.models import PipelineVersion

    p = create_pipeline(project="del-test", name="del-pipe")
    cfg = {
        "version": 1, "project": "del-test",
        "target": {"adapter": "mock"}, "judges": {},
        "suites": [], "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})

    resp = client.delete(f"/pipelines/versions/{pv.id}")
    assert resp.status_code == 200

    with session_scope() as s:
        assert s.get(PipelineVersion, pv.id) is None


def test_draft_delete_rejects_active(client):
    """DELETE /pipelines/versions/{id} for an active version returns 409."""
    _, _, _, pv_id = _make_report("active-del-test")

    resp = client.delete(f"/pipelines/versions/{pv_id}")
    assert resp.status_code == 409
