"""Feedback-1 fixes: card CSS, new project, trigger run, drafts page."""
from __future__ import annotations
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


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_active_version():
    """Create a Pipeline with an active PipelineVersion (no users → solo-dev path)."""
    from assay.pipeline import create_pipeline, create_version, activate_version
    pipe = create_pipeline(project="test-proj", name="test-pipe")
    config = {
        "version": 1, "project": "test-pipe",
        "target": {"adapter": "mock"},
        "judges": {}, "suites": [
            {"id": "s1", "requirement_ref": None, "cases": [
                {"id": "c1", "input": {}, "checks": [
                    {"type": "template", "uses": "valid_json", "with": {}}
                ]}
            ]}
        ], "gating": {},
    }
    pv = create_version(pipe.id, config, {}, {})
    activate_version(pv.id, "solo-dev")
    return pipe, pv


def _make_draft_version():
    """Create a Pipeline with a draft PipelineVersion."""
    from assay.pipeline import create_pipeline, create_version
    pipe = create_pipeline(project="draft-proj", name="draft-pipe")
    config = {
        "version": 1, "project": "draft-pipe",
        "target": {"adapter": "mock"},
        "judges": {}, "suites": [], "gating": {},
    }
    pv = create_version(pipe.id, config, {}, {})
    return pipe, pv


# ── Fix 2: New project / POST /projects ──────────────────────────────────────

def test_projects_get_200(client):
    """GET /projects returns 200."""
    resp = client.get("/projects", headers={"Accept": "text/html"})
    assert resp.status_code == 200


def test_projects_page_has_new_project_button(client):
    """GET /projects includes 'New project' button."""
    resp = client.get("/projects", headers={"Accept": "text/html"})
    assert "New project" in resp.text


def test_create_project_redirects_to_wizard(client):
    """POST /projects name=foo → 303 to /pipelines/new?project=foo."""
    resp = client.post("/projects", data={"name": "my-new-project"})
    assert resp.status_code == 303
    assert "/pipelines/new" in resp.headers["location"]
    assert "my-new-project" in resp.headers["location"]


# ── Fix 4: Drafts page ───────────────────────────────────────────────────────

def test_drafts_page_empty_state(client):
    """GET /pipelines (HTML) with no drafts returns 200 with empty-state markup."""
    resp = client.get("/pipelines", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "No draft pipelines" in resp.text or "draft" in resp.text.lower()


def test_drafts_page_lists_draft(client):
    """GET /pipelines (HTML) shows existing draft versions."""
    _make_draft_version()
    resp = client.get("/pipelines", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "draft-pipe" in resp.text
    assert "draft-proj" in resp.text


def test_drafts_page_excludes_active(client):
    """GET /pipelines (HTML) does NOT list active pipeline versions."""
    _make_active_version()
    resp = client.get("/pipelines", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    # The active version's pipeline name should not appear in the drafts table
    assert "test-pipe" not in resp.text


# ── Fix 3: Trigger run ───────────────────────────────────────────────────────

def test_trigger_run_on_active_version(client):
    """POST /pipelines/versions/{vid}/run on active version → 200 with report_id."""
    _, pv = _make_active_version()
    resp = client.post(
        f"/pipelines/versions/{pv.id}/run",
        headers={"X-Assay-User": "solo-dev"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "run_id" in data
    assert "report_id" in data
    assert data["ok"] is True


def test_trigger_run_creates_ready_for_review_report(client):
    """After triggering a run, the report is ready_for_review (auto-submitted)."""
    from assay.store import session_scope
    from assay.store.models import Report
    _, pv = _make_active_version()
    resp = client.post(
        f"/pipelines/versions/{pv.id}/run",
        headers={"X-Assay-User": "solo-dev"},
    )
    rep_id = resp.json()["report_id"]
    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.state == "ready_for_review"


def test_trigger_run_on_draft_returns_error(client):
    """POST /pipelines/versions/{vid}/run on draft → 4xx (not active)."""
    _, pv = _make_draft_version()
    resp = client.post(
        f"/pipelines/versions/{pv.id}/run",
        headers={"X-Assay-User": "solo-dev"},
    )
    assert resp.status_code >= 400
