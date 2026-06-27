"""Phase 4: run on DB pipeline + sandbox materialisation + read-only view."""
from __future__ import annotations
import os
import textwrap
import pytest

EXAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "examples", "compliance-copilot")
EXAMPLE_YAML = os.path.join(EXAMPLE_DIR, "assay.yaml")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


@pytest.fixture
def client(_tmp_db):
    pytest.importorskip("fastapi")
    pytest.importorskip("jinja2")
    import importlib, assay.server.app
    importlib.reload(assay.server.app)
    from assay.server.app import app as _app
    from starlette.testclient import TestClient
    return TestClient(_app)


def _make_active_pv(config: dict | None = None, generated_sources: dict | None = None) -> int:
    """Create and activate a minimal PipelineVersion. Returns version id."""
    from assay.pipeline import create_pipeline, create_version, activate_version
    p = create_pipeline(project="test", name="test-pipe")
    cfg = config or {
        "version": 1, "project": "test-pipe",
        "target": {"adapter": "mock"},
        "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [
                {"type": "template", "uses": "valid_json"}
            ]}
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, generated_sources or {}, {})
    activate_version(pv.id, "solo-dev")
    return pv.id


def test_run_from_db_pipeline():
    """execute_run via pipeline_version_id stores the FK on the Run row."""
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Run, Report
    pv_id = _make_active_pv()
    run_id = execute_run(pipeline_version_id=pv_id, triggered_by="tester")
    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run.pipeline_version_id == pv_id
        rep = s.query(Report).filter_by(run_id=run_id).one()
        assert rep.state == "pending"
        assert rep.summary["cases"] == 1


def test_sandbox_materialises_generated_check():
    """Check source stored in generated_sources is written to disk and executed."""
    source = textwrap.dedent("""\
        def check(response, context):
            ok = response.get("json", {}).get("ok") is True
            return {"passed": ok, "message": "ok-field check"}
    """)
    cfg = {
        "version": 1, "project": "gen-test",
        "target": {"adapter": "mock"},
        "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [
                {"type": "generated", "uses": "generated/checks/ok_check.py"}
            ]}
        ]}],
        "gating": {},
    }
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import CaseResult
    p = create_pipeline(project="gen-test", name="gen-test")
    pv = create_version(p.id, cfg, {"generated/checks/ok_check.py": source}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).one()
        # Verify the check ran through the sandbox (check_id starts with "generated:")
        assert any(c["check_id"].startswith("generated:") for c in cr.checks)


def test_sandbox_still_blocks_network():
    """Generated check that imports socket inside check() is blocked by the import guard."""
    source = textwrap.dedent("""\
        def check(response, context):
            import socket  # blocked by sandbox import guard after module load
            return {"passed": True}
    """)
    cfg = {
        "version": 1, "project": "net-test",
        "target": {"adapter": "mock"},
        "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [
                {"type": "generated", "uses": "generated/checks/net_check.py"}
            ]}
        ]}],
        "gating": {},
    }
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import CaseResult
    p = create_pipeline(project="net-test", name="net-test")
    pv = create_version(p.id, cfg, {"generated/checks/net_check.py": source}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).one()
        assert not cr.passed
        assert any("blocked" in (c.get("message") or "") for c in cr.checks)


def test_read_only_view_returns_200(client):
    """GET /reports/{id}/view returns 200 HTML containing the case id."""
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Report
    pv_id = _make_active_pv()
    run_id = execute_run(pipeline_version_id=pv_id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
    resp = client.get(f"/reports/{rep_id}/view")
    assert resp.status_code == 200
    assert "c1" in resp.text  # case id from _make_active_pv


def test_spec_path_still_works():
    """execute_run(spec=...) keeps pipeline_version_id=None on the run."""
    import assay.spec.loader as _loader
    from assay.engine import execute_run
    from assay.store import session_scope
    from assay.store.models import Run
    cwd = os.getcwd()
    os.chdir(EXAMPLE_DIR)
    try:
        spec = _loader.load_spec("assay.yaml")
        run_id = execute_run(spec, triggered_by="tester")
    finally:
        os.chdir(cwd)
    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run.pipeline_version_id is None
