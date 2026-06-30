"""Phase 0: design system + app shell smoke tests."""
from __future__ import annotations
import os
import re
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


def _make_run_and_report():
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Report

    p = create_pipeline(project="p0-test", name="p0-test")
    cfg = {
        "version": 1, "project": "p0-test",
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


# ── Nav ────────────────────────────────────────────────────────────────────

def test_shell_nav_present(client):
    """GET / returns HTML with top nav including Pipelines link (ti-git-branch icon)."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<nav" in resp.text
    assert "ti-git-branch" in resp.text


def test_shell_nav_has_all_sections(client):
    """Top nav contains the four IA sections: Projects, Pipelines, Reports, Settings."""
    resp = client.get("/")
    html = resp.text
    for label in ("Projects", "Pipelines", "Reports", "Settings"):
        assert label in html, f"Nav missing: {label}"


def test_dark_theme_default(client):
    """<html> element has data-theme='dark' as its initial (pre-JS) value."""
    resp = client.get("/")
    assert 'data-theme="dark"' in resp.text


def test_theme_toggle_uses_sun_moon_icons(client):
    """Theme toggle button references ti-sun or ti-moon (not ti-wand)."""
    resp = client.get("/")
    assert "ti-sun" in resp.text or "ti-moon" in resp.text
    assert "ti-wand" not in resp.text


# ── Dark mode ──────────────────────────────────────────────────────────────

def test_dark_mode_attr_on_html_element(client):
    """<html> element carries data-theme attribute for light/dark toggle."""
    resp = client.get("/")
    assert "data-theme" in resp.text


def test_dark_mode_toggle_button_present(client):
    """A theme-toggle button exists in the nav."""
    resp = client.get("/")
    assert "nav-theme-btn" in resp.text


# ── No hardcoded hex in non-token CSS ─────────────────────────────────────

def test_no_hardcoded_hex_in_style_css():
    """style.css must not contain hardcoded hex color values."""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "assay", "server", "static")
    path = os.path.join(static_dir, "style.css")
    content = open(path).read()
    # Strip comments
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    # Hex color pattern: colon then optional whitespace then hex value
    matches = re.findall(r":\s*(#[0-9a-fA-F]{3,8})\b", content)
    assert not matches, f"style.css contains hardcoded hex: {matches}"


def test_no_hardcoded_hex_in_components_css():
    """components.css must not contain hardcoded hex color values."""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "assay", "server", "static")
    path = os.path.join(static_dir, "components.css")
    content = open(path).read()
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    matches = re.findall(r":\s*(#[0-9a-fA-F]{3,8})\b", content)
    assert not matches, f"components.css contains hardcoded hex: {matches}"


# ── Semantic badge classes ─────────────────────────────────────────────────

def test_queue_uses_badge_accent_for_ready_for_review(client):
    """Queue page uses badge-accent class for ready_for_review state."""
    _, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})

    resp = client.get("/")
    assert resp.status_code == 200
    assert "badge-accent" in resp.text


def test_report_detail_uses_badge_accent_for_ready_for_review(client):
    """Report detail uses badge-accent class when state is ready_for_review."""
    _, rep_id = _make_run_and_report()
    client.post(f"/reports/{rep_id}/submit", headers={"X-Assay-User": "solo-dev"})

    resp = client.get(f"/reports/{rep_id}", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "badge-accent" in resp.text


# ── Tabler icons ───────────────────────────────────────────────────────────

def test_tabler_icons_cdn_loaded(client):
    """base.html loads the Tabler Icons stylesheet."""
    resp = client.get("/")
    assert "tabler-icons" in resp.text


def test_inter_font_loaded(client):
    """base.html loads the Inter font from Google Fonts."""
    resp = client.get("/")
    assert "Inter" in resp.text
