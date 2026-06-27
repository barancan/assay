"""Minimal FastAPI surface for the review/approval workflow + auto-trigger hook.

Identity is passed via the `X-Assay-User` header (lightweight, self-hosted).
The approval endpoint enforces reviewer/admin authority — automation can trigger
runs and submit for review, but never self-promote to `done`.
"""
from __future__ import annotations
import json
import os
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from ..store import session_scope, init_db
from ..store.models import Report, Run, User, Pipeline, PipelineVersion
from ..engine import approve_report, submit_for_review
from ..reporting import export_report

app = FastAPI(title="Assay")
init_db()

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.filters["tojson"] = lambda v, indent=None: json.dumps(v, indent=indent)


@app.get("/pipelines")
def list_pipelines():
    with session_scope() as s:
        return [
            {"id": p.id, "project": p.project, "name": p.name,
             "created_at": str(p.created_at), "created_by": p.created_by}
            for p in s.query(Pipeline).all()
        ]


@app.get("/pipelines/{pipeline_id}/versions")
def get_pipeline_versions(pipeline_id: int):
    with session_scope() as s:
        if not s.get(Pipeline, pipeline_id):
            raise HTTPException(404, "pipeline not found")
        rows = (
            s.query(PipelineVersion)
            .filter_by(pipeline_id=pipeline_id)
            .order_by(PipelineVersion.version_number)
            .all()
        )
        return [
            {"id": v.id, "version_number": v.version_number, "status": v.status,
             "content_hash": v.content_hash, "created_at": str(v.created_at),
             "created_by": v.created_by}
            for v in rows
        ]


class ImportBody(BaseModel):
    spec_yaml: str
    project: str
    created_by: str | None = None


@app.post("/pipelines/import")
def import_pipeline(body: ImportBody):
    """Import a pipeline spec from YAML text. Generated sources and rubrics are not
    resolved from disk — use `assay pipeline import --spec FILE` for the full import."""
    import yaml
    from ..spec.models import Spec
    from ..pipeline import create_pipeline, create_version, content_hash
    try:
        raw = yaml.safe_load(body.spec_yaml)
        spec = Spec.model_validate(raw)
    except Exception as exc:
        raise HTTPException(400, f"invalid spec: {exc}")
    config_dict = spec.model_dump(by_alias=True)
    with session_scope() as s:
        pipeline = s.query(Pipeline).filter_by(project=body.project, name=spec.project).one_or_none()
        if pipeline is None:
            pipeline = Pipeline(project=body.project, name=spec.project, created_by=body.created_by)
            s.add(pipeline)
            s.flush()
        pid = pipeline.id
    pv = create_version(pid, config_dict, {}, {}, body.created_by)
    return {"pipeline_version_id": pv.id, "status": pv.status, "content_hash": pv.content_hash}


@app.post("/pipelines/versions/{version_id}/activate")
def activate_pipeline_version(
    version_id: int, x_assay_user: str = Header(...),
):
    from ..pipeline import activate_version
    try:
        activate_version(version_id, x_assay_user)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "version_id": version_id, "activated_by": x_assay_user}


@app.get("/reports")
def list_reports():
    with session_scope() as s:
        return [{"id": r.id, "run_id": r.run_id, "state": r.state,
                 "approved_by": r.approved_by, "summary": r.summary}
                for r in s.query(Report).all()]


@app.get("/reports/{report_id}")
def get_report(report_id: int):
    with session_scope() as s:
        r = s.get(Report, report_id)
        if not r:
            raise HTTPException(404, "report not found")
        run = s.get(Run, r.run_id)
        return {"id": r.id, "state": r.state, "approved_by": r.approved_by,
                "summary": r.summary, "export_paths": r.export_paths,
                "target": {"adapter": run.target.adapter, "model": run.target.model},
                "transitions": [{"from": t.from_state, "to": t.to_state,
                                 "actor": t.actor, "at": str(t.at)}
                                for t in r.transitions]}


@app.get("/reports/{report_id}/view", response_class=HTMLResponse)
def view_report(request: Request, report_id: int):
    from ..store.models import TargetModel, CaseResult
    with session_scope() as s:
        r = s.get(Report, report_id)
        if not r:
            raise HTTPException(404, "report not found")
        run = s.get(Run, r.run_id)
        target = s.get(TargetModel, run.target_id)
        pv = s.get(PipelineVersion, run.pipeline_version_id) if run.pipeline_version_id else None
        cases = s.query(CaseResult).filter_by(run_id=run.id).all()

        ctx = {
            "report_id": r.id,
            "report_state": r.state,
            "report_summary": r.summary,
            "report_approved_by": r.approved_by,
            "run_id": run.id,
            "run_project": run.project,
            "run_triggered_by": run.triggered_by,
            "target_adapter": target.adapter,
            "target_model": target.model,
            "target_endpoint": target.endpoint,
            "pipeline_version": {
                "id": pv.id,
                "version_number": pv.version_number,
                "content_hash": pv.content_hash,
            } if pv else None,
            "case_results": [
                {
                    "id": cr.id,
                    "suite_id": cr.suite_id,
                    "case_id": cr.case_id,
                    "requirement_ref": cr.requirement_ref,
                    "passed": cr.passed,
                    "latency_ms": cr.latency_ms,
                    "checks": cr.checks,
                }
                for cr in cases
            ],
        }

    return templates.TemplateResponse(request, "report_detail.html", ctx)


class AssignBody(BaseModel):
    reviewer: str


@app.post("/reports/{report_id}/assign")
def assign(report_id: int, body: AssignBody, x_assay_user: str = Header(...)):
    from ..engine import assign_reviewer
    try:
        assign_reviewer(report_id, body.reviewer, x_assay_user)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "report_id": report_id, "assigned_reviewer": body.reviewer}


class AdjudicateBody(BaseModel):
    verdict: str | None = None   # "pass" | "fail" | None to clear
    reason: str | None = None


@app.post("/reports/{report_id}/cases/{case_result_id}/adjudicate")
def adjudicate(
    report_id: int,
    case_result_id: int,
    body: AdjudicateBody,
    x_assay_user: str = Header(...),
):
    from ..engine import adjudicate_case
    try:
        adjudicate_case(report_id, case_result_id, body.verdict, x_assay_user, body.reason)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "report_id": report_id, "case_result_id": case_result_id,
            "verdict": body.verdict}


@app.get("/reports/{report_id}/effective-summary")
def effective_summary(report_id: int):
    from ..engine.review import _recompute_summary
    with session_scope() as s:
        r = s.get(Report, report_id)
        if not r:
            raise HTTPException(404, "report not found")
        _recompute_summary(report_id, s)
        summary = dict(r.summary)
    return summary


@app.get("/reports/{report_id}/reviewer-options")
def reviewer_options(report_id: int):
    with session_scope() as s:
        rows = s.query(User).filter(User.role.in_(["reviewer", "admin"])).all()
        return [{"name": u.name, "role": u.role} for u in rows]


class ApproveBody(BaseModel):
    note: str | None = None


@app.post("/reports/{report_id}/submit")
def submit(report_id: int, x_assay_user: str = Header("anonymous")):
    submit_for_review(report_id, actor=x_assay_user)
    return {"ok": True, "state": "ready_for_review"}


@app.post("/reports/{report_id}/approve")
def approve(report_id: int, body: ApproveBody, x_assay_user: str = Header(...)):
    try:
        approve_report(report_id, x_assay_user, body.note)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    export_report(_run_for(report_id))
    return {"ok": True, "state": "done", "approved_by": x_assay_user}


def _run_for(report_id: int) -> int:
    with session_scope() as s:
        return s.get(Report, report_id).run_id


class HookBody(BaseModel):
    spec: str = "assay.yaml"
    by: str = "webhook"


@app.post("/hooks/run")
def hook_run(body: HookBody):
    """Auto-trigger a run on model/prompt update. Lands at ready_for_review."""
    from ..spec.loader import load_spec
    from ..engine import execute_run
    run_id = execute_run(load_spec(body.spec), trigger="auto", triggered_by=body.by)
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
    submit_for_review(rep_id, actor=body.by, note="auto after webhook")
    export_report(run_id)
    return {"ok": True, "run_id": run_id, "report_id": rep_id, "state": "ready_for_review"}
