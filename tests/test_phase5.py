"""Phase 5: determinism classification, per-check param config, activation authority."""
from __future__ import annotations
import importlib
import pytest
from itsdangerous import URLSafeSerializer

_REAL_SECRET = "test-only-real-secret-not-the-dev-default"


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


def _fresh_client(monkeypatch, *, auth=None, secret=None):
    if auth is not None:
        monkeypatch.setenv("ASSAY_AUTH", auth)
    if secret is not None:
        monkeypatch.setenv("ASSAY_SECRET_KEY", secret)
    import assay.config
    importlib.reload(assay.config)
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


def _make_draft_mixed_checks():
    """Pipeline + draft version with template, generated, and judge checks."""
    from assay.pipeline import create_pipeline, create_version
    pipe = create_pipeline(project="proj", name="mixed-pipe")
    config = {
        "version": 1, "project": "mixed-pipe",
        "target": {"adapter": "mock"}, "judges": {"primary": {"provider": "mock", "model": "mock"}},
        "suites": [
            {"id": "s1", "requirement_ref": "R1", "cases": [
                {"id": "c1", "input": {}, "checks": [
                    {"type": "template", "uses": "latency_bound", "with": {"max_ms": 5000}},
                    {"type": "generated", "uses": "generated/checks/g1.py"},
                    {"type": "judge", "judge": "primary", "rubric": "generated/rubrics/r1.yaml"},
                ]},
            ]},
        ],
        "gating": {},
    }
    sources = {"generated/checks/g1.py": "def g1(r, **kw):\n    return True\n"}
    rubrics = {"generated/rubrics/r1.yaml": "judge: primary\n"}
    pv = create_version(pipe.id, config, sources, rubrics)
    return pipe, pv


# ── 1. determinism derivation ────────────────────────────────────────────────

def test_determinism_classification():
    """_build_check_list derives deterministic/stochastic from check type."""
    import assay.server.app as app
    _, pv = _make_draft_mixed_checks()
    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    with session_scope() as s:
        pv_obj = s.get(PipelineVersion, pv.id)
        checks = app._build_check_list(pv_obj)
    by_type = {c["type"]: c["determinism"] for c in checks}
    assert by_type["template"] == "deterministic"
    assert by_type["generated"] == "deterministic"
    assert by_type["judge"] == "stochastic"


def test_determinism_badge_in_review_page(client):
    """Review page HTML shows both Deterministic and Stochastic badges."""
    pipe, pv = _make_draft_mixed_checks()
    resp = client.get(f"/pipelines/{pipe.id}/versions/{pv.id}/review")
    assert resp.status_code == 200
    assert "Deterministic" in resp.text
    assert "Stochastic" in resp.text


def test_get_version_checks_json(client):
    """GET /pipelines/versions/{vid}/checks returns the flattened list with determinism."""
    _, pv = _make_draft_mixed_checks()
    resp = client.get(f"/pipelines/versions/{pv.id}/checks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert {c["determinism"] for c in data} == {"deterministic", "stochastic"}
    # check_index is populated per case
    assert [c["check_index"] for c in data] == [0, 1, 2]


# ── 2. per-check param config ─────────────────────────────────────────────────

def test_check_params_update_draft(client):
    """PATCH check-params on a draft updates the config in place."""
    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    _, pv = _make_draft_mixed_checks()
    resp = client.patch(
        f"/pipelines/versions/{pv.id}/check-params",
        json={"suite_id": "s1", "case_id": "c1", "check_index": 0,
              "params": {"max_ms": 1200}},
        headers={"X-Assay-User": "dev"},
    )
    assert resp.status_code == 200
    with session_scope() as s:
        updated = s.get(PipelineVersion, pv.id)
        chk = updated.config["suites"][0]["cases"][0]["checks"][0]
        assert chk["with"] == {"max_ms": 1200}


def test_check_params_update_active_rejected(client):
    """PATCH check-params on an active version → 409."""
    from assay.store import session_scope
    from assay.store.models import User
    from assay.pipeline import activate_version
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    _, pv = _make_draft_mixed_checks()
    activate_version(pv.id, "rev1")
    resp = client.patch(
        f"/pipelines/versions/{pv.id}/check-params",
        json={"suite_id": "s1", "case_id": "c1", "check_index": 0,
              "params": {"max_ms": 1200}},
        headers={"X-Assay-User": "rev1"},
    )
    assert resp.status_code == 409


def test_check_params_bad_index(client):
    """PATCH check-params with an out-of-range index → 404."""
    _, pv = _make_draft_mixed_checks()
    resp = client.patch(
        f"/pipelines/versions/{pv.id}/check-params",
        json={"suite_id": "s1", "case_id": "c1", "check_index": 99,
              "params": {}},
        headers={"X-Assay-User": "dev"},
    )
    assert resp.status_code == 404


# ── 3. stored CheckResult carries type ────────────────────────────────────────

def test_case_result_checks_carry_type():
    """After a run, each stored check dict includes its type for display."""
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import CaseResult
    pipe = create_pipeline(project="p", name="typed")
    config = {
        "version": 1, "project": "typed", "target": {"adapter": "mock"},
        "judges": {}, "suites": [
            {"id": "s1", "requirement_ref": None, "cases": [
                {"id": "c1", "input": {}, "checks": [
                    {"type": "template", "uses": "valid_json"}]}
            ]}
        ], "gating": {},
    }
    pv = create_version(pipe.id, config, {}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).first()
        assert cr.checks[0]["type"] == "template"


# ── 4. activation authority preserved in enforced mode ────────────────────────

def test_enforced_mode_generate_returns_permission_error(monkeypatch):
    """Enforced mode + non-reviewer POST generate → draft, activated:false, error surfaced."""
    from assay.store import session_scope
    from assay.store.models import User, Pipeline, PipelineVersion
    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))   # seed so table is non-empty
        s.add(User(name="bob", role="runner"))
    client = _fresh_client(monkeypatch, auth="enforced", secret=_REAL_SECRET)
    bob_cookie = URLSafeSerializer(_REAL_SECRET).dumps("bob")
    resp = client.post(
        "/pipelines/generate",
        json={"project": "e", "name": "e-pipe",
              "requirements": "The response must be valid JSON.",
              "adapter_spec": {"adapter": "mock"}},
        cookies={"assay_user": bob_cookie},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["activated"] is False
    assert data["permission_error"]
    with session_scope() as s:
        pv = s.get(PipelineVersion, data["pipeline_version_id"])
        assert pv.status == "draft"


def test_open_mode_generate_activates(client):
    """Open mode + empty User table → generate auto-activates."""
    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    resp = client.post(
        "/pipelines/generate",
        json={"project": "o", "name": "o-pipe",
              "requirements": "The response must be valid JSON.",
              "adapter_spec": {"adapter": "mock"}},
        headers={"X-Assay-User": "solo-dev"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["activated"] is True
    with session_scope() as s:
        pv = s.get(PipelineVersion, data["pipeline_version_id"])
        assert pv.status == "active"


# ── 5. edit → new active version → archive-old flow still works ───────────────

def test_edit_new_version_archive_flow(client):
    """Reviewer generates then edits: new version activates, prior one archived."""
    from assay.store import session_scope
    from assay.store.models import User, PipelineVersion
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    # First generate (as reviewer) → active v1
    r1 = client.post(
        "/pipelines/generate",
        json={"project": "ed", "name": "ed-pipe",
              "requirements": "The response must be valid JSON.",
              "adapter_spec": {"adapter": "mock"}},
        headers={"X-Assay-User": "rev1"},
    )
    d1 = r1.json()
    assert d1["activated"] is True
    v1_id = d1["pipeline_version_id"]
    pipe_id = d1["pipeline_id"]
    # Edit: generate again on the same pipeline → new active version, v1 archived
    r2 = client.post(
        "/pipelines/generate",
        json={"project": "ed", "name": "ed-pipe",
              "requirements": "The response must be valid JSON.\nNo PII in the output.",
              "adapter_spec": {"adapter": "mock"}, "pipeline_id": pipe_id},
        headers={"X-Assay-User": "rev1"},
    )
    d2 = r2.json()
    v2_id = d2["pipeline_version_id"]
    assert v2_id != v1_id
    assert d2["activated"] is True
    with session_scope() as s:
        assert s.get(PipelineVersion, v1_id).status == "archived"
        assert s.get(PipelineVersion, v2_id).status == "active"
