"""End-to-end smoke test: run the example, check states, enforce the approval gate."""
import os
import tempfile
import pytest
from assay.spec.loader import load_spec
from assay.engine import execute_run, approve_report
from assay.store import init_db, session_scope
from assay.store.models import User, Report
from assay.sandbox import run_generated_check

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "compliance-copilot")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path/'t.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


def _run_example():
    cwd = os.getcwd()
    os.chdir(EXAMPLE)
    try:
        spec = load_spec("assay.yaml")
        return execute_run(spec, triggered_by="tester")
    finally:
        os.chdir(cwd)


def test_run_produces_report_with_expected_pass_fail():
    run_id = _run_example()
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        assert rep.state == "pending"
        assert rep.summary["cases"] == 4
        assert rep.summary["failed"] == 1   # R3-bad-severity must fail


def test_approval_requires_reviewer_authority():
    from assay.store.db import init_db
    init_db()
    with session_scope() as s:
        s.add(User(name="runner1", role="runner"))
        s.add(User(name="rev1", role="reviewer"))
    run_id = _run_example()
    with session_scope() as s:
        rep_id = s.query(Report).filter_by(run_id=run_id).one().id
    from assay.engine import submit_for_review
    submit_for_review(rep_id, actor="runner1")
    with pytest.raises(PermissionError):
        approve_report(rep_id, "runner1")
    approve_report(rep_id, "rev1", note="ok")
    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.state == "done" and rep.approved_by == "rev1" and rep.locked


def test_sandbox_blocks_network_and_filesystem():
    src = 'def check(r,c):\n import socket; return {"passed":True}\n'
    p = tempfile.mktemp(suffix=".py"); open(p, "w").write(src)
    out = run_generated_check(p, {"json": {}}, {})
    assert out["passed"] is False and "blocked" in out["message"]
