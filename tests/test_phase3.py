"""Phase 3: setup flow — define/connect/judge/preview, WorkspaceSetting, connection test."""
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


@pytest.fixture(autouse=True)
def _builder(builder_llm):
    """Every build path needs a model; inject the fake so no API key is required."""
    return builder_llm


@pytest.fixture
def client(_tmp_db):
    import importlib
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


# ── GET /pipelines/new ──────────────────────────────────────────────────────

def test_pipeline_new_page(client):
    """GET /pipelines/new returns 200 with the stepper markup."""
    resp = client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "Define" in resp.text
    assert "Connect" in resp.text


# ── POST /connection-test ───────────────────────────────────────────────────

def test_connection_test_mock(client):
    """POST /connection-test with adapter=mock returns HTML badge containing 'Connected'."""
    resp = client.post("/connection-test", json={"adapter": "mock"})
    assert resp.status_code == 200
    assert "Connected" in resp.text


def test_connection_test_bad_adapter(client):
    """POST /connection-test with unknown adapter returns error badge, not a 500."""
    resp = client.post("/connection-test", json={"adapter": "does_not_exist"})
    assert resp.status_code == 200
    # Must NOT raise a 500 — always returns HTML badge
    assert "unknown adapter" in resp.text or "error" in resp.text.lower()


# ── POST /pipelines/preview ─────────────────────────────────────────────────

def test_pipeline_preview(client):
    """POST /pipelines/preview with citation requirement returns citation_present check."""
    resp = client.post(
        "/pipelines/preview",
        json={"requirements": "Every finding cites a source article or document."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "checks" in data
    assert "estimated_minutes" in data
    types = [c["type"] for c in data["checks"]]
    assertions = " ".join(c["assertion"] for c in data["checks"])
    assert "template" in types
    assert "cit" in assertions.lower() or any("cite" in c["assertion"].lower() for c in data["checks"])


def test_judge_section_hidden(client):
    """POST /pipelines/preview with only latency+JSON requirements returns no judge checks."""
    resp = client.post(
        "/pipelines/preview",
        json={"requirements": "Response must be valid JSON. Latency under 2000 ms."},
    )
    assert resp.status_code == 200
    data = resp.json()
    types = [c["type"] for c in data["checks"]]
    assert "judge" not in types


def test_judge_section_shown(client):
    """POST /pipelines/preview with refusal requirement returns at least one judge check."""
    resp = client.post(
        "/pipelines/preview",
        json={"requirements": "The model should refuse or express uncertainty rather than hallucinate."},
    )
    assert resp.status_code == 200
    data = resp.json()
    types = [c["type"] for c in data["checks"]]
    assert "judge" in types


# ── POST /pipelines/generate ─────────────────────────────────────────────────

def test_pipeline_generate_creates_active(client):
    """POST /pipelines/generate auto-activates the version so it is immediately runnable."""
    resp = client.post(
        "/pipelines/generate",
        json={
            "project": "gen-test",
            "name": "gen-pipe",
            "requirements": "Response must be valid JSON.",
            "adapter_spec": {"adapter": "mock"},
        },
        headers={"X-Assay-User": "tester"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "pipeline_version_id" in data

    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    with session_scope() as s:
        pv = s.get(PipelineVersion, data["pipeline_version_id"])
        assert pv is not None
        assert pv.status == "active"
        assert pv.step_reached == "review"


# ── POST /pipelines/save-draft ───────────────────────────────────────────────

def test_save_draft_step(client):
    """POST /pipelines/save-draft with step=define persists step_reached=define."""
    resp = client.post(
        "/pipelines/save-draft",
        json={
            "project": "draft-proj",
            "name": "draft-pipe",
            "requirements": "Must be valid JSON.",
            "step": "define",
        },
        headers={"X-Assay-User": "dev"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "pipeline_version_id" in data

    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    with session_scope() as s:
        pv = s.get(PipelineVersion, data["pipeline_version_id"])
        assert pv.step_reached == "define"


# ── GET /settings/judge ───────────────────────────────────────────────────────

def test_workspace_judge_default(client):
    """GET /settings/judge returns the seeded judge_adapter='anthropic'."""
    resp = client.get("/settings/judge")
    assert resp.status_code == 200
    data = resp.json()
    assert data["judge_adapter"] == "anthropic"
    assert "judge_model" in data


# ── GET /pipelines/new adapter choices ────────────────────────────────────────

def test_the_wizard_does_not_offer_the_mock_adapter(client):
    """The mock adapter used to be the wizard's first and default choice.

    It passes every check, so picking it produces a green report about nothing. This
    test replaces one that asserted the mock block was present.
    """
    resp = client.get("/pipelines/new")
    assert resp.status_code == 200
    html = resp.text
    assert 'value="mock"' not in html
    assert 'adapter-mock-fields' not in html
    assert 'value="anthropic"' in html
