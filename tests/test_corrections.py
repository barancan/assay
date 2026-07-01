"""Corrections: dark default, clickable rows, project detail, model selectors."""
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


def _make_report(project: str = "corr-test"):
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Report

    p = create_pipeline(project=project, name=f"{project}-pipe")
    cfg = {
        "version": 1, "project": project,
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
        return run_id, rep.id, p.id, pv.id


# ── 1. Dark theme default ──────────────────────────────────────────────────

def test_dark_theme_default_on_html_element(client):
    """<html> element has data-theme='dark' as the pre-JS initial value."""
    resp = client.get("/")
    assert 'data-theme="dark"' in resp.text


def test_fouc_script_in_head_before_stylesheets(client):
    """Inline theme script appears in <head> before the first stylesheet link."""
    resp = client.get("/")
    html = resp.text
    script_pos = html.find("localStorage.getItem('assay-theme')")
    css_pos = html.find("/static/tokens.css")
    assert script_pos != -1, "pre-paint script not found"
    assert css_pos != -1, "tokens.css link not found"
    assert script_pos < css_pos, "pre-paint script must appear before stylesheets"


def test_theme_toggle_shows_sun_or_moon(client):
    """Theme toggle uses ti-sun / ti-moon, not ti-wand."""
    resp = client.get("/")
    assert ("ti-sun" in resp.text or "ti-moon" in resp.text)
    assert "ti-wand" not in resp.text


# ── 2. Clickable rows ─────────────────────────────────────────────────────

def test_report_rows_are_anchor_links(client):
    """Each report row in the reports list is an <a href='/reports/{id}'>."""
    _, rep_id, _, _ = _make_report("row-link-test")
    resp = client.get("/reports", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert f'href="/reports/{rep_id}"' in resp.text


def test_report_rows_use_row_link_class(client):
    """Report rows use the .row-link CSS class for hover/focus styling."""
    _make_report("row-cls-test")
    resp = client.get("/reports", headers={"Accept": "text/html"})
    assert "row-link" in resp.text


def test_project_cards_are_anchor_links(client):
    """Project cards are wrapped in <a href='/projects/{name}'>."""
    _make_report("card-link-test")
    resp = client.get("/projects")
    assert resp.status_code == 200
    assert 'href="/projects/card-link-test"' in resp.text


# ── 3. Project detail page ────────────────────────────────────────────────

def test_project_detail_returns_200(client):
    """GET /projects/{name} returns 200."""
    _make_report("detail-test")
    resp = client.get("/projects/detail-test")
    assert resp.status_code == 200


def test_project_detail_has_four_sections(client):
    """Project detail contains the four expected sections."""
    _make_report("sections-test")
    resp = client.get("/projects/sections-test")
    html = resp.text
    assert "Approved baseline" in html
    assert "Pipelines" in html
    assert "Reports" in html


def test_project_detail_baseline_with_approved_report(client):
    """An approved pass report surfaces as the approved baseline."""
    from assay.engine import submit_for_review
    from assay.engine.review import set_verdict
    _, rep_id, _, _ = _make_report("baseline-test")
    submit_for_review(rep_id, actor="solo-dev")
    set_verdict(rep_id, "pass", "lgtm", "solo-dev")

    resp = client.get("/projects/baseline-test")
    assert resp.status_code == 200
    html = resp.text
    assert "Approved baseline" in html
    assert f"Report #{rep_id}" in html


def test_project_detail_draft_has_resume_link(client):
    """A draft pipeline shows a Resume link inside the project detail."""
    from assay.pipeline import create_pipeline, create_version
    p = create_pipeline(project="draft-detail-test", name="draft-pipe")
    cfg = {"version": 1, "project": "draft-detail-test", "target": {"adapter": "mock"},
           "judges": {}, "suites": [], "gating": {}}
    pv = create_version(p.id, cfg, {}, {})

    resp = client.get("/projects/draft-detail-test")
    assert resp.status_code == 200
    assert "Resume" in resp.text
    assert f"resume={pv.id}" in resp.text


def test_nav_has_pipelines_link(client):
    """Navigation has a Pipelines top-level link."""
    resp = client.get("/")
    html = resp.text
    nav_start = html.find('<nav')
    nav_end = html.find('</nav>', nav_start)
    nav_html = html[nav_start:nav_end]
    assert 'href="/pipelines"' in nav_html


# ── 4. Model selectors ────────────────────────────────────────────────────

def test_model_selector_has_custom_option(client):
    """/pipelines/new contains a 'Custom…' option in the model selector."""
    resp = client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "Custom" in resp.text


def test_model_selector_has_anthropic_models(client):
    """/pipelines/new lists known Anthropic models in the macro output."""
    resp = client.get("/pipelines/new")
    assert "claude-opus-4-8" in resp.text
    assert "claude-sonnet-4-6" in resp.text


def test_settings_judge_uses_select_not_text_input(client):
    """Settings page uses a <select> for judge adapter, not a free-text input."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.text
    # The adapter field must be a <select> with known values
    assert '<select' in html
    assert 'anthropic' in html


def test_settings_judge_roundtrip(client):
    """POST /settings/judge persists new values; GET /settings/judge returns them."""
    resp = client.post(
        "/settings/judge",
        json={"judge_adapter": "ollama", "judge_model": "mistral"},
    )
    assert resp.status_code == 200
    data = client.get("/settings/judge").json()
    assert data["judge_adapter"] == "ollama"
    assert data["judge_model"] == "mistral"
