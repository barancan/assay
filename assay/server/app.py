"""FastAPI review server for the Assay workflow.

Identity resolution order:
  1. Signed session cookie 'assay_user' (set by POST /login)
  2. X-Assay-User request header (API / CLI / CI callers)
  3. 'anonymous' fallback

Reviewer/admin authority is checked by the engine layer; the server just
resolves and forwards the actor name.
"""
from __future__ import annotations
import json
import os
from fastapi import FastAPI, Header, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from ..store import session_scope, init_db
from ..store.models import Report, Run, User, Pipeline, PipelineVersion
from ..engine import approve_report, submit_for_review
from ..reporting import export_report

app = FastAPI(title="Assay")
init_db()

_DIR = os.path.dirname(__file__)
_TEMPLATES_DIR = os.path.join(_DIR, "templates")
_STATIC_DIR = os.path.join(_DIR, "static")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.filters["tojson"] = lambda v, indent=None: json.dumps(v, indent=indent)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

_SECRET = os.environ.get("ASSAY_SECRET_KEY", "assay-dev-secret")


def _identity(request: Request) -> str:
    from itsdangerous import URLSafeSerializer, BadSignature
    cookie = request.cookies.get("assay_user")
    if cookie:
        try:
            return URLSafeSerializer(_SECRET).loads(cookie)
        except (BadSignature, Exception):
            pass
    header = request.headers.get("x-assay-user")
    if header:
        return header
    return "anonymous"


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _run_for(report_id: int) -> int:
    with session_scope() as s:
        r = s.get(Report, report_id)
        if r is None:
            raise HTTPException(404, "report not found")
        return r.run_id


def _report_ctx(report_id: int, request: Request) -> dict:
    from ..store.models import TargetModel, CaseResult
    with session_scope() as s:
        r = s.get(Report, report_id)
        if not r:
            raise HTTPException(404, "report not found")
        run = s.get(Run, r.run_id)
        target = s.get(TargetModel, run.target_id)
        pv = s.get(PipelineVersion, run.pipeline_version_id) if run.pipeline_version_id else None
        cases = s.query(CaseResult).filter_by(run_id=run.id).all()
        reviewers = s.query(User).filter(User.role.in_(["reviewer", "admin"])).all()
        return {
            "report_id": r.id,
            "report_state": r.state,
            "report_summary": dict(r.summary),
            "report_approved_by": r.approved_by,
            "report_locked": r.locked,
            "assigned_reviewer": r.assigned_reviewer,
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
            "generated_sources": dict(pv.generated_sources or {}) if pv else {},
            "case_results": [
                {
                    "id": cr.id,
                    "suite_id": cr.suite_id,
                    "case_id": cr.case_id,
                    "requirement_ref": cr.requirement_ref,
                    "passed": cr.passed,
                    "latency_ms": cr.latency_ms,
                    "checks": cr.checks,
                    "human_verdict": cr.human_verdict,
                    "overridden_by": cr.overridden_by,
                    "effective_passed": cr.effective_passed,
                }
                for cr in cases
            ],
            "reviewer_options": [{"name": u.name, "role": u.role} for u in reviewers],
            "identity": _identity(request),
        }


# ── Queue ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def queue(request: Request, state: str | None = None, project: str | None = None):
    with session_scope() as s:
        rows = s.query(Report).order_by(Report.created_at.desc()).all()
        report_rows = []
        for r in rows:
            run = s.get(Run, r.run_id)
            if state and r.state != state:
                continue
            if project and run.project != project:
                continue
            report_rows.append({
                "id": r.id,
                "state": r.state,
                "project": run.project,
                "summary": dict(r.summary) if r.summary else {},
                "assigned_reviewer": r.assigned_reviewer,
                "pipeline_version_id": run.pipeline_version_id,
                "created_at": str(r.created_at)[:19],
            })
    return templates.TemplateResponse(request, "queue.html", {
        "reports": report_rows,
        "state_filter": state or "",
        "project_filter": project or "",
        "identity": _identity(request),
    })


# ── Login / Logout ─────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    with session_scope() as s:
        users = s.query(User).filter(User.role.in_(["reviewer", "admin"])).all()
        user_list = [{"name": u.name, "role": u.role} for u in users]
    return templates.TemplateResponse(request, "login.html", {
        "users": user_list,
        "identity": _identity(request),
    })


@app.post("/login")
def login_submit(username: str = Form(...)):
    from itsdangerous import URLSafeSerializer
    signed = URLSafeSerializer(_SECRET).dumps(username)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("assay_user", signed, httponly=True, samesite="strict")
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("assay_user")
    return response


# ── Pipelines ──────────────────────────────────────────────────────────────

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
    """Import a pipeline spec from YAML text."""
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
def activate_pipeline_version(version_id: int, x_assay_user: str = Header(...)):
    from ..pipeline import activate_version
    try:
        activate_version(version_id, x_assay_user)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "version_id": version_id, "activated_by": x_assay_user}


# ── Reports ────────────────────────────────────────────────────────────────

@app.get("/reports")
def list_reports():
    with session_scope() as s:
        return [{"id": r.id, "run_id": r.run_id, "state": r.state,
                 "approved_by": r.approved_by, "summary": r.summary}
                for r in s.query(Report).all()]


@app.get("/reports/{report_id}")
def get_report(report_id: int, request: Request):
    """Content-negotiate: JSON if Accept: application/json, HTML otherwise."""
    if "application/json" in request.headers.get("accept", ""):
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
    ctx = _report_ctx(report_id, request)
    return templates.TemplateResponse(request, "report_detail.html", ctx)


@app.get("/reports/{report_id}/view", response_class=HTMLResponse)
def view_report(request: Request, report_id: int):
    ctx = _report_ctx(report_id, request)
    return templates.TemplateResponse(request, "report_detail.html", ctx)


# ── Assignment ─────────────────────────────────────────────────────────────

class AssignBody(BaseModel):
    reviewer: str


@app.post("/reports/{report_id}/assign")
def assign(
    report_id: int,
    body: AssignBody,
    request: Request,
    x_assay_user: str | None = Header(default=None),
):
    from ..engine import assign_reviewer
    actor = x_assay_user or _identity(request)
    try:
        assign_reviewer(report_id, body.reviewer, actor)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    if _is_htmx(request):
        return HTMLResponse(
            f'<div id="assigned-reviewer-section">'
            f"<strong>Assigned to:</strong> {body.reviewer}"
            f' <small style="color:#666">(by {actor})</small>'
            f"</div>"
        )
    return {"ok": True, "report_id": report_id, "assigned_reviewer": body.reviewer}


# ── Adjudication ───────────────────────────────────────────────────────────

class AdjudicateBody(BaseModel):
    verdict: str | None = None
    reason: str | None = None


@app.post("/reports/{report_id}/cases/{case_result_id}/adjudicate")
def adjudicate(
    report_id: int,
    case_result_id: int,
    body: AdjudicateBody,
    request: Request,
    x_assay_user: str | None = Header(default=None),
):
    from ..engine import adjudicate_case
    from ..store.models import CaseResult
    actor = x_assay_user or _identity(request)
    verdict = body.verdict if body.verdict else None   # normalise "" → None
    try:
        adjudicate_case(report_id, case_result_id, verdict, actor, body.reason)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    if _is_htmx(request):
        with session_scope() as s:
            cr = s.get(CaseResult, case_result_id)
            rep = s.get(Report, report_id)
            cr_dict = {
                "id": cr.id,
                "suite_id": cr.suite_id,
                "case_id": cr.case_id,
                "requirement_ref": cr.requirement_ref,
                "passed": cr.passed,
                "latency_ms": cr.latency_ms,
                "checks": cr.checks,
                "human_verdict": cr.human_verdict,
                "overridden_by": cr.overridden_by,
                "effective_passed": cr.effective_passed,
            }
            locked = rep.locked
        resp = templates.TemplateResponse(request, "_case_row.html", {
            "cr": cr_dict,
            "report_id": report_id,
            "report_locked": locked,
            "identity": actor,
        })
        resp.headers["HX-Trigger"] = "summaryChanged"
        return resp
    return {"ok": True, "report_id": report_id, "case_result_id": case_result_id,
            "verdict": verdict}


# ── Summary / reviewer options ─────────────────────────────────────────────

@app.get("/reports/{report_id}/effective-summary")
def effective_summary(report_id: int, request: Request):
    from ..engine.review import _recompute_summary
    with session_scope() as s:
        r = s.get(Report, report_id)
        if not r:
            raise HTTPException(404, "report not found")
        _recompute_summary(report_id, s)
        summary = dict(r.summary)
    if _is_htmx(request):
        return templates.TemplateResponse(request, "_summary_bar.html", {
            "report_id": report_id,
            "summary": summary,
        })
    return summary


@app.get("/reports/{report_id}/reviewer-options")
def reviewer_options(report_id: int):
    with session_scope() as s:
        rows = s.query(User).filter(User.role.in_(["reviewer", "admin"])).all()
        return [{"name": u.name, "role": u.role} for u in rows]


# ── Submit / Approve ───────────────────────────────────────────────────────

@app.post("/reports/{report_id}/submit")
def submit(report_id: int, x_assay_user: str = Header("anonymous")):
    submit_for_review(report_id, actor=x_assay_user)
    return {"ok": True, "state": "ready_for_review"}


@app.post("/reports/{report_id}/approve")
def approve(
    report_id: int,
    request: Request,
    x_assay_user: str | None = Header(default=None),
):
    actor = x_assay_user or _identity(request)
    try:
        approve_report(report_id, actor)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    export_report(_run_for(report_id))
    if _is_htmx(request):
        return Response(headers={"HX-Redirect": f"/reports/{report_id}"})
    return RedirectResponse(f"/reports/{report_id}", status_code=303)


# ── Export download ─────────────────────────────────────────────────────────

@app.get("/reports/{report_id}/export/{fmt}")
def export_download(report_id: int, fmt: str):
    if fmt not in ("json", "md", "html"):
        raise HTTPException(400, f"unknown format: {fmt}")
    run_id = _run_for(report_id)
    paths = export_report(run_id, formats=[fmt])
    path = paths.get(fmt)
    if not path:
        raise HTTPException(500, "export failed")
    media_types = {"json": "application/json", "md": "text/markdown", "html": "text/html"}
    return FileResponse(path, media_type=media_types[fmt],
                        filename=f"report_{report_id}.{fmt}")


# ── Webhook ────────────────────────────────────────────────────────────────

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
