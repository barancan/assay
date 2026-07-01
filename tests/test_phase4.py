"""Phase 4: pipeline review screen — check list, regenerate, inline edit, activation gate."""
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

def _make_draft_with_checks(with_generated=False):
    """Create a Pipeline + draft PipelineVersion with a generated check source."""
    from assay.pipeline import create_pipeline, create_version
    pipe = create_pipeline(project="proj", name="test-pipe")
    check = {"type": "template", "uses": "valid_json", "with": {}}
    if with_generated:
        check = {"type": "generated", "uses": "generated/checks/my_check.py"}
    config = {
        "version": 1, "project": "test-pipe",
        "target": {"adapter": "mock"},
        "judges": {}, "suites": [
            {"id": "s1", "requirement_ref": "R1", "cases": [
                {"id": "c1", "input": {}, "checks": [check]}
            ]}
        ], "gating": {},
    }
    sources = {"generated/checks/my_check.py": "def my_check(r, **kw):\n    return True\n"} if with_generated else {}
    pv = create_version(pipe.id, config, sources, {})
    return pipe, pv


# ── GET /pipelines/{pid}/versions/{vid}/review ────────────────────────────────

def test_pipeline_review_page(client):
    """GET /pipelines/{pid}/versions/{vid}/review returns 200 with check rows and activation gate."""
    pipe, pv = _make_draft_with_checks()
    resp = client.get(
        f"/pipelines/{pipe.id}/versions/{pv.id}/review",
        headers={"Accept": "text/html"},
    )
    assert resp.status_code == 200
    assert "valid_json" in resp.text
    assert "activation-gate" in resp.text
    assert "Activate v" in resp.text


def test_pipeline_review_page_shows_check_count(client):
    """Review page header shows '1 check'."""
    pipe, pv = _make_draft_with_checks()
    resp = client.get(f"/pipelines/{pipe.id}/versions/{pv.id}/review")
    assert resp.status_code == 200
    assert "1 check" in resp.text


# ── POST /pipelines/versions/{vid}/activate ───────────────────────────────────

def test_activate_requires_reviewer(client):
    """Activate with a runner-role actor → 403."""
    from assay.store import session_scope
    from assay.store.models import User
    with session_scope() as s:
        s.add(User(name="runner1", role="runner"))
    _, pv = _make_draft_with_checks()
    resp = client.post(
        f"/pipelines/versions/{pv.id}/activate",
        headers={"X-Assay-User": "runner1"},
    )
    assert resp.status_code == 403


def test_activate_promotes_version(client):
    """Activate with a reviewer-role actor → version status becomes active."""
    from assay.store import session_scope
    from assay.store.models import User, PipelineVersion
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    _, pv = _make_draft_with_checks()
    resp = client.post(
        f"/pipelines/versions/{pv.id}/activate",
        headers={"X-Assay-User": "rev1"},
    )
    assert resp.status_code == 200
    with session_scope() as s:
        updated = s.get(PipelineVersion, pv.id)
        assert updated.status == "active"
        assert updated.activated_by == "rev1"


def test_activate_archives_previous(client):
    """Activating a second version archives the first."""
    from assay.store import session_scope
    from assay.store.models import User, PipelineVersion
    from assay.pipeline import create_version
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    pipe, pv1 = _make_draft_with_checks()
    client.post(f"/pipelines/versions/{pv1.id}/activate",
                headers={"X-Assay-User": "rev1"})
    # Create a second draft
    config = {
        "version": 1, "project": "test-pipe", "target": {"adapter": "mock"},
        "judges": {}, "suites": [], "gating": {},
    }
    pv2 = create_version(pipe.id, config, {}, {})
    client.post(f"/pipelines/versions/{pv2.id}/activate",
                headers={"X-Assay-User": "rev1"})
    with session_scope() as s:
        assert s.get(PipelineVersion, pv1.id).status == "archived"
        assert s.get(PipelineVersion, pv2.id).status == "active"


def test_activate_htmx_returns_redirect(client):
    """HTMX activation → response has HX-Redirect pointing to /projects/…"""
    _, pv = _make_draft_with_checks()
    resp = client.post(
        f"/pipelines/versions/{pv.id}/activate",
        headers={"X-Assay-User": "solo-dev", "HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "HX-Redirect" in resp.headers
    assert "/projects/" in resp.headers["HX-Redirect"]


# ── POST /pipelines/versions/{vid}/checks/{cid}/regenerate ───────────────────

def test_regenerate_check_creates_draft(client):
    """POST regenerate → new draft PipelineVersion with updated generated_sources."""
    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    _, pv = _make_draft_with_checks(with_generated=True)
    original_id = pv.id
    check_path = "generated/checks/my_check.py"
    resp = client.post(
        f"/pipelines/versions/{pv.id}/checks/{check_path}/regenerate",
        headers={"X-Assay-User": "dev"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "new_version_id" in data
    new_id = data["new_version_id"]
    assert new_id != original_id
    with session_scope() as s:
        new_pv = s.get(PipelineVersion, new_id)
        assert new_pv.status == "draft"
        assert check_path in new_pv.generated_sources
        # Source should have been regenerated (non-empty)
        assert new_pv.generated_sources[check_path]


# ── PATCH /pipelines/versions/{vid}/checks/{cid} ─────────────────────────────

def test_inline_edit_updates_source(client):
    """PATCH on a draft version updates generated_sources for that check."""
    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    _, pv = _make_draft_with_checks(with_generated=True)
    check_path = "generated/checks/my_check.py"
    new_source = "def my_check(r, **kw):\n    return r.get('ok') is True\n"
    resp = client.patch(
        f"/pipelines/versions/{pv.id}/checks/{check_path}",
        json={"source": new_source},
        headers={"X-Assay-User": "dev"},
    )
    assert resp.status_code == 200
    with session_scope() as s:
        updated = s.get(PipelineVersion, pv.id)
        assert updated.generated_sources[check_path] == new_source


def test_inline_edit_rejects_active(client):
    """PATCH on an active version → 409."""
    from assay.store import session_scope
    from assay.store.models import User
    from assay.pipeline import activate_version
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    _, pv = _make_draft_with_checks(with_generated=True)
    activate_version(pv.id, "rev1")
    check_path = "generated/checks/my_check.py"
    resp = client.patch(
        f"/pipelines/versions/{pv.id}/checks/{check_path}",
        json={"source": "def my_check(r, **kw): return True"},
        headers={"X-Assay-User": "rev1"},
    )
    assert resp.status_code == 409
