"""Phase 5: adjudication, reviewer assignment, and effective-summary recompute."""
from __future__ import annotations
import pytest
from assay.pipeline import create_pipeline, create_version
from assay.engine import execute_run, submit_for_review, approve_report
from assay.engine.review import assign_reviewer, adjudicate_case
from assay.store import session_scope
from assay.store.models import Report, CaseResult, CaseAdjudication, User, StateTransition


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


def _make_run_with_failure():
    """Pipeline with one passing and one failing case; report left at ready_for_review.

    Returns (run_id, report_id, failing_case_result_id).
    Bypasses activate_version role checks so it works regardless of User table state.
    """
    import datetime as dt
    from assay.store.models import PipelineVersion
    p = create_pipeline(project="adj-test", name="adj-test")
    cfg = {
        "version": 1, "project": "adj-test",
        "target": {"adapter": "mock"},
        "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "pass-case", "input": {}, "checks": [
                {"type": "template", "uses": "valid_json"}
            ]},
            {"id": "fail-case", "input": {}, "checks": [
                {"type": "template", "uses": "contains", "with": {"value": "NEVER_PRESENT"}}
            ]},
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    # Directly activate to avoid role-check interference with User table state.
    with session_scope() as s:
        obj = s.get(PipelineVersion, pv.id)
        obj.status = "active"
        obj.activated_by = "test-setup"
        obj.activated_at = dt.datetime.now(dt.timezone.utc)
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")

    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
        cr = s.query(CaseResult).filter_by(run_id=run_id, case_id="fail-case").one()
        cr_id = cr.id

    submit_for_review(rep_id, actor="tester")
    return run_id, rep_id, cr_id


def test_override_flips_effective_summary():
    _, rep_id, cr_id = _make_run_with_failure()

    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.summary["failed"] == 1

    adjudicate_case(rep_id, cr_id, "pass", "solo-dev", "looks fine on review")

    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.summary["failed"] == 0
        assert rep.summary["passed"] == 2


def test_override_blocked_after_done():
    _, rep_id, cr_id = _make_run_with_failure()
    # Solo-dev path: User table empty so any actor can approve.
    approve_report(rep_id, "solo-dev")
    with pytest.raises(PermissionError, match="locked"):
        adjudicate_case(rep_id, cr_id, "pass", "solo-dev")


def test_runner_cannot_adjudicate():
    with session_scope() as s:
        s.add(User(name="runner1", role="runner"))
    _, rep_id, cr_id = _make_run_with_failure()
    with pytest.raises(PermissionError):
        adjudicate_case(rep_id, cr_id, "pass", "runner1")


def test_runner_cannot_assign():
    with session_scope() as s:
        s.add(User(name="runner1", role="runner"))
    _, rep_id, _ = _make_run_with_failure()
    with pytest.raises(PermissionError):
        assign_reviewer(rep_id, "anyone", "runner1")


def test_assignment_recorded_and_audited():
    _, rep_id, _ = _make_run_with_failure()
    assign_reviewer(rep_id, "alice", "solo-dev")

    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.assigned_reviewer == "alice"
        assert rep.assigned_by == "solo-dev"
        assert rep.assigned_at is not None
        # A StateTransition note records the assignment.
        notes = [t.note for t in s.query(StateTransition).filter_by(report_id=rep_id).all()]
        assert any("alice" in (n or "") for n in notes)


def test_approval_uses_effective_verdicts():
    _, rep_id, cr_id = _make_run_with_failure()
    adjudicate_case(rep_id, cr_id, "pass", "solo-dev", "manual override")
    approve_report(rep_id, "solo-dev", note="all good")

    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.summary["failed"] == 0
        assert rep.approved_by == "solo-dev"
        assert rep.locked is True


def test_adjudication_audit_log():
    _, rep_id, cr_id = _make_run_with_failure()
    adjudicate_case(rep_id, cr_id, "pass", "solo-dev", "first")
    adjudicate_case(rep_id, cr_id, None, "solo-dev", "cleared")

    with session_scope() as s:
        rows = (
            s.query(CaseAdjudication)
            .filter_by(case_result_id=cr_id)
            .order_by(CaseAdjudication.at)
            .all()
        )
        assert len(rows) == 2
        assert rows[0].action == "set" and rows[0].verdict == "pass"
        assert rows[1].action == "clear" and rows[1].verdict is None
