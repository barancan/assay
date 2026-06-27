"""State-machine transitions. Automation may submit_for_review; only an
authorised reviewer/admin may approve to `done`, assign a reviewer, or adjudicate cases."""
from __future__ import annotations
import datetime as dt
import logging
from ..store import session_scope
from ..store.models import (
    Report, Run, StateTransition, User, CaseResult, CaseAdjudication,
    TargetModel, PipelineVersion,
)
from ..notifications import get_notifier

logger = logging.getLogger(__name__)

VALID = {"pending": {"ready_for_review"},
         "ready_for_review": {"done", "pending"},
         "done": set()}


def _transition(report: Report, to_state: str, actor: str, note: str | None, s) -> None:
    if to_state not in VALID[report.state]:
        raise ValueError(f"illegal transition {report.state} -> {to_state}")
    s.add(StateTransition(report_id=report.id, from_state=report.state,
                          to_state=to_state, actor=actor, note=note))
    report.state = to_state


def _check_reviewer(actor: str, s) -> None:
    """Raise PermissionError unless actor has reviewer/admin role.

    Open mode: if the User table is empty, any named actor is trusted (solo-dev path).
    Enforced mode: never trust-when-empty; requires seeded reviewer accounts.
    """
    from ..config import auth_mode
    total = s.query(User).count()
    if total == 0:
        if auth_mode() == "enforced":
            raise PermissionError(
                "no reviewer accounts configured; "
                "seed a reviewer before approving in enforced mode: "
                "assay users --add <name> --role reviewer"
            )
        return
    user = s.query(User).filter_by(name=actor).one_or_none()
    if user is None or user.role not in ("reviewer", "admin"):
        raise PermissionError(f"'{actor}' lacks reviewer authority")


def _recompute_summary(report_id: int, s) -> None:
    """Recompute Report.summary using effective_passed on all CaseResults (in-session)."""
    rep = s.get(Report, report_id)
    cases = s.query(CaseResult).filter_by(run_id=rep.run_id).all()
    flags = [cr.effective_passed for cr in cases]
    rep.summary = {
        "cases": len(flags),
        "passed": sum(flags),
        "failed": len(flags) - sum(flags),
    }


def _fire(event: str, payload: dict) -> None:
    """Call the configured notifier; swallow exceptions so transitions are never aborted."""
    try:
        get_notifier().notify(event, payload)
    except Exception:
        logger.exception("notification failed for event=%s report_id=%s",
                         event, payload.get("report_id"))


def submit_for_review(report_id: int, actor: str = "cli", note: str | None = None) -> None:
    payload: dict = {}
    with session_scope() as s:
        rep = s.get(Report, report_id)
        _transition(rep, "ready_for_review", actor, note, s)
        run = s.get(Run, rep.run_id)
        target = s.get(TargetModel, run.target_id)
        pv = s.get(PipelineVersion, run.pipeline_version_id) if run.pipeline_version_id else None
        payload = {
            "event": "ready_for_review",
            "report_id": rep.id,
            "run_id": run.id,
            "project": run.project,
            "summary": dict(rep.summary),
            "assigned_reviewer": rep.assigned_reviewer,
            "target": {
                "adapter": target.adapter,
                "model": target.model,
                "endpoint": target.endpoint,
            },
            "pipeline_version": {
                "id": pv.id,
                "version_number": pv.version_number,
                "content_hash": pv.content_hash,
            } if pv else None,
        }
    _fire("ready_for_review", payload)


def approve_report(report_id: int, approver: str, note: str | None = None) -> None:
    """Promote to `done`. Requires reviewer/admin authority (solo-dev: any actor)."""
    payload: dict = {}
    with session_scope() as s:
        _check_reviewer(approver, s)
        rep = s.get(Report, report_id)
        _recompute_summary(report_id, s)
        _transition(rep, "done", approver, note, s)
        rep.approved_by = approver
        rep.approved_at = dt.datetime.now(dt.timezone.utc)
        rep.locked = True
        run = s.get(Run, rep.run_id)
        payload = {
            "event": "approved",
            "report_id": rep.id,
            "run_id": run.id,
            "project": run.project,
            "approved_by": approver,
            "summary": dict(rep.summary),
        }
    _fire("approved", payload)


def assign_reviewer(report_id: int, reviewer: str, actor: str) -> None:
    """Set Report.assigned_reviewer. Actor must have reviewer/admin authority."""
    with session_scope() as s:
        _check_reviewer(actor, s)
        rep = s.get(Report, report_id)
        if rep is None:
            raise ValueError(f"Report {report_id} not found")
        rep.assigned_reviewer = reviewer
        rep.assigned_by = actor
        rep.assigned_at = dt.datetime.now(dt.timezone.utc)
        s.add(StateTransition(
            report_id=report_id,
            from_state=rep.state, to_state=rep.state,
            actor=actor,
            note=f"assigned reviewer: {reviewer}",
        ))


def adjudicate_case(
    report_id: int,
    case_result_id: int,
    verdict: str | None,
    actor: str,
    reason: str | None = None,
) -> None:
    """Override the machine verdict on a single case.

    verdict: "pass" | "fail" to set, None to clear.
    Actor must be reviewer/admin. Report must be ready_for_review and not locked.
    """
    with session_scope() as s:
        _check_reviewer(actor, s)
        rep = s.get(Report, report_id)
        if rep is None:
            raise ValueError(f"Report {report_id} not found")
        if rep.locked:
            raise PermissionError("report is locked")
        if rep.state != "ready_for_review":
            raise ValueError(f"report is not ready_for_review (state: {rep.state})")

        cr = s.get(CaseResult, case_result_id)
        if cr is None or cr.run_id != rep.run_id:
            raise ValueError(f"CaseResult {case_result_id} not in report {report_id}")

        cr.human_verdict = verdict
        cr.overridden_by = actor
        cr.overridden_at = dt.datetime.now(dt.timezone.utc)
        cr.override_reason = reason

        s.add(CaseAdjudication(
            case_result_id=case_result_id,
            action="set" if verdict is not None else "clear",
            verdict=verdict,
            actor=actor,
            reason=reason,
        ))

        _recompute_summary(report_id, s)
