"""Phase 8: full DB-pipeline end-to-end flow using the compliance-copilot example."""
from __future__ import annotations
import os
from pathlib import Path
import pytest

EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "compliance-copilot"


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


def test_compliance_copilot_db_flow():
    """import → activate → run → submit → assign → adjudicate → approve → export."""
    from assay.pipeline import import_from_yaml, activate_version
    from assay.engine import execute_run, submit_for_review, assign_reviewer, adjudicate_case, approve_report
    from assay.reporting import export_report
    from assay.store import session_scope
    from assay.store.models import Report, CaseResult

    # 1. Import assay.yaml — must run from the spec dir for relative paths.
    cwd = os.getcwd()
    os.chdir(EXAMPLE_DIR)
    try:
        pv = import_from_yaml("assay.yaml", project="compliance-copilot", created_by="ci")
        assert pv.status == "draft"
        assert len(pv.content_hash) == 64

        # 2. Activate (solo-dev path: User table empty → any actor trusted).
        activate_version(pv.id, actor="ci")

        # 3. Execute run.
        run_id = execute_run(pipeline_version_id=pv.id, triggered_by="ci")
    finally:
        os.chdir(cwd)

    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
        assert rep.state == "pending"
        assert rep.summary["cases"] == 4
        assert rep.summary["failed"] == 1   # R3-bad-severity must still fail

    # 4. Submit for review.
    submit_for_review(rep_id, actor="ci")
    with session_scope() as s:
        assert s.get(Report, rep_id).state == "ready_for_review"

    # 5. Assign a reviewer (solo-dev path).
    assign_reviewer(rep_id, reviewer="alice", actor="ci")
    with session_scope() as s:
        assert s.get(Report, rep_id).assigned_reviewer == "alice"

    # 6. Adjudicate the one failing case to pass.
    with session_scope() as s:
        failing_cr = (
            s.query(CaseResult)
            .filter_by(run_id=run_id, passed=False)
            .first()
        )
        assert failing_cr is not None
        cr_id = failing_cr.id

    adjudicate_case(rep_id, cr_id, verdict="pass", actor="ci",
                    reason="accepted for this release")

    with session_scope() as s:
        cr = s.get(CaseResult, cr_id)
        assert cr.human_verdict == "pass"
        assert cr.effective_passed is True

    # 7. Approve — uses effective verdicts (all pass after adjudication).
    approve_report(rep_id, approver="ci")

    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.state == "done"
        assert rep.locked is True
        assert rep.summary["failed"] == 0

    # 8. Export — all three formats written to disk.
    paths = export_report(run_id)
    assert "json" in paths
    assert Path(paths["json"]).exists()
    assert Path(paths["md"]).exists()
    assert Path(paths["html"]).exists()
