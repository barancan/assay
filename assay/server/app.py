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
from ..store.models import Report, Run, User, Pipeline, PipelineVersion, WorkspaceSetting
from ..engine import approve_report, submit_for_review
from ..reporting import export_report
from .. import config as _config

_config.enforce_posture_or_raise()

app = FastAPI(title="Assay")
init_db()

_DIR = os.path.dirname(__file__)
_TEMPLATES_DIR = os.path.join(_DIR, "templates")
_STATIC_DIR = os.path.join(_DIR, "static")

from urllib.parse import quote as _urlquote

templates = Jinja2Templates(directory=_TEMPLATES_DIR)
templates.env.filters["tojson"] = lambda v, indent=None: json.dumps(v, indent=indent)
templates.env.filters["clean_request"] = lambda d: {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}
templates.env.filters["urlencode"] = lambda s: _urlquote(str(s), safe="")

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _identity(request: Request) -> str:
    """Resolve identity for display purposes. Always returns a string."""
    from itsdangerous import URLSafeSerializer, BadSignature
    cookie = request.cookies.get("assay_user")
    if cookie:
        try:
            return URLSafeSerializer(_config.secret_key()).loads(cookie)
        except (BadSignature, Exception):
            pass
    header = request.headers.get("x-assay-user")
    if header:
        return header
    return "anonymous"


def _require_identity(request: Request, x_assay_user: str | None = None) -> str:
    """Resolve identity for privileged actions; 401 in enforced mode if not authenticated."""
    actor = x_assay_user or _identity(request)
    if _config.auth_mode() == "enforced" and actor == "anonymous":
        raise HTTPException(
            401,
            detail="authentication required: log in at /login or set X-Assay-User header",
        )
    return actor


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
    from ..engine.review import compute_suggested_verdict
    with session_scope() as s:
        r = s.get(Report, report_id)
        if not r:
            raise HTTPException(404, "report not found")
        run = s.get(Run, r.run_id)
        target = s.get(TargetModel, run.target_id)
        pv = s.get(PipelineVersion, run.pipeline_version_id) if run.pipeline_version_id else None
        cases = s.query(CaseResult).filter_by(run_id=run.id).all()
        reviewers = s.query(User).filter(User.role.in_(["reviewer", "admin"])).all()
        suggested_verdict = compute_suggested_verdict(r.id, s)
        return {
            "report_id": r.id,
            "report_state": r.state,
            "report_summary": dict(r.summary),
            "report_approved_by": r.approved_by,
            "report_locked": r.locked,
            "assigned_reviewer": r.assigned_reviewer,
            "suggested_verdict": suggested_verdict,
            "verdict": r.verdict,
            "verdict_reason": r.verdict_reason,
            "verdict_set_by": r.verdict_set_by,
            "verdict_set_at": str(r.verdict_set_at)[:19] if r.verdict_set_at else None,
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
    from ..store.models import TargetModel
    with session_scope() as s:
        rows = s.query(Report).order_by(Report.created_at.desc()).all()
        report_rows = []
        for r in rows:
            run = s.get(Run, r.run_id)
            if state and r.state != state:
                continue
            if project and run.project != project:
                continue
            target = s.get(TargetModel, run.target_id) if run.target_id else None
            pv = s.get(PipelineVersion, run.pipeline_version_id) if run.pipeline_version_id else None
            report_rows.append({
                "id": r.id,
                "state": r.state,
                "verdict": r.verdict,
                "project": run.project,
                "summary": dict(r.summary) if r.summary else {},
                "assigned_reviewer": r.assigned_reviewer,
                "triggered_by": run.triggered_by,
                "target_adapter": target.adapter if target else None,
                "target_model": target.model if target else None,
                "pipeline_version": pv.version_number if pv else None,
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
    signed = URLSafeSerializer(_config.secret_key()).dumps(username)
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
def list_pipelines(request: Request):
    if "text/html" in request.headers.get("accept", ""):
        # Drafts now live inside Project detail — redirect browsers to /projects.
        return RedirectResponse("/projects", status_code=302)
    with session_scope() as s:
        return [
            {"id": p.id, "project": p.project, "name": p.name,
             "created_at": str(p.created_at), "created_by": p.created_by}
            for p in s.query(Pipeline).all()
        ]


_STARTER_TEMPLATES = [
    {"id": "valid_json",        "label": "Valid JSON",        "text": "The response must be valid JSON."},
    {"id": "no_pii",            "label": "No PII",            "text": "No personally identifiable information in the output."},
    {"id": "citation_present",  "label": "Citation present",  "text": "Every finding cites a source article or document."},
    {"id": "refusal_handling",  "label": "Refusal handling",  "text": "The model should decline or express uncertainty rather than hallucinate."},
    {"id": "latency_budget",    "label": "Latency budget",    "text": "Response in under 2 000 ms (p95)."},
    {"id": "rag_faithfulness",  "label": "RAG faithfulness",  "text": "Answer only from the provided context—no fabrication."},
    {"id": "toxicity_free",     "label": "Toxicity-free",     "text": "Output must be free from toxic or harmful language."},
]
_ADAPTER_NAMES = ["mock", "anthropic", "openai_compat", "ollama", "rest"]


@app.get("/pipelines/new", response_class=HTMLResponse)
def pipeline_new_page(request: Request, resume: int | None = None, project: str | None = None):
    with session_scope() as s:
        ja = s.get(WorkspaceSetting, "judge_adapter")
        jm = s.get(WorkspaceSetting, "judge_model")
        judge_adapter = ja.value if ja else "anthropic"
        judge_model = jm.value if jm else "claude-haiku-4-5-20251001"
        resume_data = None
        if resume:
            pv = s.get(PipelineVersion, resume)
            if pv and pv.status == "draft":
                from ..store.models import Pipeline as _P
                pipe = s.get(_P, pv.pipeline_id)
                resume_data = {
                    "version_id": pv.id,
                    "requirements": pv.config.get("requirements", "") if isinstance(pv.config, dict) else "",
                    "step_reached": pv.step_reached or "define",
                    "project": pipe.project if pipe else "",
                    "name": pipe.name if pipe else "",
                }
    return templates.TemplateResponse(request, "pipeline_new.html", {
        "starter_templates": _STARTER_TEMPLATES,
        "adapter_names": _ADAPTER_NAMES,
        "judge_adapter": judge_adapter,
        "judge_model": judge_model,
        "resume_data": resume_data,
        "project_default": project or "",
        "identity": _identity(request),
    })


class ConnectionTestBody(BaseModel):
    adapter: str
    model: str | None = None
    endpoint: str | None = None
    key_env: str | None = None


@app.post("/connection-test", response_class=HTMLResponse)
def connection_test_route(body: ConnectionTestBody):
    from ..engine.connection import test_connection
    spec = {"adapter": body.adapter}
    if body.model:
        spec["model"] = body.model
    if body.endpoint:
        spec["endpoint"] = body.endpoint
    result = test_connection(spec)
    if result.get("ok"):
        ms = result.get("latency_ms") or 0
        html = (
            f'<span id="connection-result" class="badge badge-pass">'
            f'<i class="ti ti-circle-check" aria-hidden="true"></i>'
            f" Connected {ms:.0f} ms</span>"
        )
    else:
        err = (result.get("error") or "connection failed")[:120]
        html = (
            f'<span id="connection-result" class="badge badge-fail">'
            f'<i class="ti ti-circle-x" aria-hidden="true"></i>'
            f" {err}</span>"
        )
    return HTMLResponse(html)


class PreviewBody(BaseModel):
    requirements: str
    adapter: str | None = None


@app.post("/pipelines/preview")
def pipeline_preview(body: PreviewBody):
    from ..generator.build import derive_intents
    intents = derive_intents(body.requirements, judge=None)
    checks = [
        {"id": it["id"], "type": it.get("how", "template"), "assertion": it.get("assertion", "")}
        for it in intents
    ]
    return {"checks": checks, "estimated_minutes": max(2, len(checks))}


class GenerateBody(BaseModel):
    project: str
    name: str
    requirements: str
    adapter_spec: dict
    judge_spec: dict | None = None


@app.post("/pipelines/generate")
def pipeline_generate(
    body: GenerateBody,
    request: Request,
    x_assay_user: str | None = Header(default=None),
):
    from ..generator.build import derive_intents, intents_to_spec
    from ..pipeline import create_version
    from ..pipeline.service import update_step_reached
    actor = _require_identity(request, x_assay_user)
    intents = derive_intents(body.requirements, judge=None)
    judges = {"primary": body.judge_spec} if body.judge_spec else {}
    spec_dict = intents_to_spec(body.project, intents, body.adapter_spec, judges)
    with session_scope() as s:
        pipe = s.query(Pipeline).filter_by(project=body.project, name=body.name).one_or_none()
        if pipe is None:
            pipe = Pipeline(project=body.project, name=body.name, created_by=actor)
            s.add(pipe)
            s.flush()
        pid = pipe.id
    pv = create_version(pid, spec_dict, {}, {}, actor)
    update_step_reached(pv.id, "review")
    if _is_htmx(request):
        return Response(headers={"HX-Redirect": f"/projects/{_urlquote(body.project, safe='')}"})
    return {"pipeline_version_id": pv.id}


class SaveDraftBody(BaseModel):
    project: str
    name: str
    requirements: str
    step: str = "define"


@app.post("/pipelines/save-draft")
def save_pipeline_draft(
    body: SaveDraftBody,
    request: Request,
    x_assay_user: str | None = Header(default=None),
):
    from ..pipeline.service import save_draft_from_requirements, update_step_reached
    actor = _require_identity(request, x_assay_user)
    version_id = save_draft_from_requirements(body.project, body.name, body.requirements, actor)
    if body.step != "define":
        update_step_reached(version_id, body.step)
    return {"pipeline_version_id": version_id}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with session_scope() as s:
        users = s.query(User).order_by(User.role, User.name).all()
        user_list = [{"name": u.name, "role": u.role} for u in users]
        ja = s.get(WorkspaceSetting, "judge_adapter")
        jm = s.get(WorkspaceSetting, "judge_model")
        judge_adapter = ja.value if ja else "anthropic"
        judge_model = jm.value if jm else "claude-haiku-4-5-20251001"
    return templates.TemplateResponse(request, "settings.html", {
        "users": user_list,
        "judge_adapter": judge_adapter,
        "judge_model": judge_model,
        "auth_mode": _config.auth_mode(),
        "identity": _identity(request),
    })


@app.get("/settings/judge")
def settings_judge():
    with session_scope() as s:
        ja = s.get(WorkspaceSetting, "judge_adapter")
        jm = s.get(WorkspaceSetting, "judge_model")
    return {
        "judge_adapter": ja.value if ja else "anthropic",
        "judge_model": jm.value if jm else "claude-haiku-4-5-20251001",
    }


class JudgeSettingsBody(BaseModel):
    judge_adapter: str
    judge_model: str


@app.post("/settings/judge")
def update_judge_settings(body: JudgeSettingsBody):
    with session_scope() as s:
        s.merge(WorkspaceSetting(key="judge_adapter", value=body.judge_adapter))
        s.merge(WorkspaceSetting(key="judge_model", value=body.judge_model))
    return {"ok": True}


@app.get("/projects", response_class=HTMLResponse)
def list_projects(request: Request):
    from ..store.models import Run
    with session_scope() as s:
        project_names = [row[0] for row in s.query(Pipeline.project).distinct().order_by(Pipeline.project).all()]
        project_list = []
        for name in project_names:
            pipeline_count = s.query(Pipeline).filter_by(project=name).count()
            run_ids = [r.id for r in s.query(Run).filter_by(project=name).all()]
            report_count = 0
            latest_verdict = None
            pass_rate = None
            last_run_at = None
            if run_ids:
                reports_q = s.query(Report).filter(Report.run_id.in_(run_ids))
                report_count = reports_q.count()
                latest_done = (
                    reports_q.filter(Report.state == "done")
                    .order_by(Report.created_at.desc()).first()
                )
                if latest_done:
                    latest_verdict = latest_done.verdict
                all_rep = reports_q.all()
                total_cases = sum(r.summary.get("cases", 0) for r in all_rep)
                total_passed = sum(r.summary.get("passed", 0) for r in all_rep)
                pass_rate = (total_passed / total_cases * 100) if total_cases > 0 else None
                last_run = (
                    s.query(Run).filter_by(project=name)
                    .order_by(Run.started_at.desc()).first()
                )
                if last_run:
                    last_run_at = str(last_run.started_at)[:10]
            project_list.append({
                "name": name,
                "pipeline_count": pipeline_count,
                "report_count": report_count,
                "latest_verdict": latest_verdict,
                "pass_rate": pass_rate,
                "last_run_at": last_run_at,
            })
    return templates.TemplateResponse(request, "projects.html", {
        "projects": project_list,
        "identity": _identity(request),
    })


@app.post("/projects")
def create_project(request: Request, name: str = Form(...)):
    return RedirectResponse("/projects", status_code=303)


@app.get("/projects/{project_name}", response_class=HTMLResponse)
def project_detail(request: Request, project_name: str):
    from ..store.models import Run as _Run
    from urllib.parse import unquote as _unquote
    project_name = _unquote(project_name)
    with session_scope() as s:
        # Stats
        pipeline_count = s.query(Pipeline).filter_by(project=project_name).count()
        run_ids = [r.id for r in s.query(_Run).filter_by(project=project_name).all()]
        report_count = 0
        pass_rate = None
        last_run_at = None
        baseline = None
        if run_ids:
            reports_q = s.query(Report).filter(Report.run_id.in_(run_ids))
            report_count = reports_q.count()
            all_rep = reports_q.all()
            total_cases = sum(r.summary.get("cases", 0) for r in all_rep)
            total_passed = sum(r.summary.get("passed", 0) for r in all_rep)
            pass_rate = (total_passed / total_cases * 100) if total_cases > 0 else None
            last_run = (
                s.query(_Run).filter_by(project=project_name)
                .order_by(_Run.started_at.desc()).first()
            )
            if last_run:
                last_run_at = str(last_run.started_at)[:10]
            # Approved baseline: most recent done+pass report
            bl = (
                reports_q.filter(Report.state == "done", Report.verdict == "pass")
                .order_by(Report.created_at.desc()).first()
            )
            if bl:
                bl_run = s.get(_Run, bl.run_id)
                bl_pv = s.get(PipelineVersion, bl_run.pipeline_version_id) if bl_run and bl_run.pipeline_version_id else None
                baseline = {
                    "id": bl.id,
                    "pipeline_version": bl_pv.version_number if bl_pv else None,
                    "verdict_set_by": bl.verdict_set_by,
                    "created_at": str(bl.created_at)[:10],
                }
        # Pipelines with versions
        pipes_raw = s.query(Pipeline).filter_by(project=project_name).order_by(Pipeline.name).all()
        pipelines = []
        for pipe in pipes_raw:
            active_pv = (
                s.query(PipelineVersion)
                .filter_by(pipeline_id=pipe.id, status="active")
                .order_by(PipelineVersion.version_number.desc()).first()
            )
            drafts_pv = (
                s.query(PipelineVersion)
                .filter_by(pipeline_id=pipe.id, status="draft")
                .order_by(PipelineVersion.id.desc()).all()
            )
            pipe_run_ids = [r.id for r in s.query(_Run).filter_by(project=project_name).all()]
            pipe_report_count = 0
            last_verdict = None
            last_pipe_run = None
            if pipe_run_ids and active_pv:
                # narrow to runs for this pipeline version
                pv_runs = s.query(_Run).filter(
                    _Run.pipeline_version_id == active_pv.id
                ).all()
                pv_run_ids = [r.id for r in pv_runs]
                if pv_run_ids:
                    pipe_report_count = s.query(Report).filter(Report.run_id.in_(pv_run_ids)).count()
                    latest_done = (
                        s.query(Report)
                        .filter(Report.run_id.in_(pv_run_ids), Report.state == "done")
                        .order_by(Report.created_at.desc()).first()
                    )
                    if latest_done:
                        last_verdict = latest_done.verdict
                    last_pv_run = (
                        s.query(_Run).filter(_Run.pipeline_version_id == active_pv.id)
                        .order_by(_Run.started_at.desc()).first()
                    )
                    if last_pv_run:
                        last_pipe_run = str(last_pv_run.started_at)[:10]
            pipelines.append({
                "id": pipe.id,
                "name": pipe.name,
                "active_version": active_pv.version_number if active_pv else None,
                "last_verdict": last_verdict,
                "report_count": pipe_report_count,
                "last_run_at": last_pipe_run,
                "drafts": [
                    {
                        "id": d.id,
                        "version_number": d.version_number,
                        "step_reached": d.step_reached or "define",
                    }
                    for d in drafts_pv
                ],
            })
        # Reports for this project
        report_rows = []
        if run_ids:
            reps = (
                s.query(Report).filter(Report.run_id.in_(run_ids))
                .order_by(Report.created_at.desc()).limit(50).all()
            )
            for r in reps:
                report_rows.append({
                    "id": r.id,
                    "state": r.state,
                    "verdict": r.verdict,
                    "summary": dict(r.summary) if r.summary else {},
                    "created_at": str(r.created_at)[:10],
                })
    return templates.TemplateResponse(request, "project_detail.html", {
        "project_name": project_name,
        "stats": {
            "pipeline_count": pipeline_count,
            "report_count": report_count,
            "pass_rate": pass_rate,
            "last_run_at": last_run_at,
        },
        "baseline": baseline,
        "pipelines": pipelines,
        "reports": report_rows,
        "identity": _identity(request),
    })


@app.delete("/pipelines/versions/{version_id}")
def delete_draft_version(version_id: int, request: Request):
    with session_scope() as s:
        pv = s.get(PipelineVersion, version_id)
        if not pv:
            raise HTTPException(404, "version not found")
        if pv.status != "draft":
            raise HTTPException(409, f"only draft versions can be deleted (status: {pv.status})")
        s.delete(pv)
    if _is_htmx(request):
        return HTMLResponse("")
    return {"ok": True}


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

_PAGE_SIZE = 50


@app.get("/reports")
def list_reports(
    request: Request,
    q: str | None = None,
    project: str | None = None,
    state: str | None = None,
    verdict: str | None = None,
    reviewer: str | None = None,
    page: int = 0,
):
    if "text/html" in request.headers.get("accept", ""):
        from ..store.models import TargetModel, Run as _Run
        with session_scope() as s:
            query = s.query(Report).join(_Run, Report.run_id == _Run.id)
            if state:
                query = query.filter(Report.state == state)
            if project:
                query = query.filter(_Run.project == project)
            if verdict:
                query = query.filter(Report.verdict == verdict)
            if reviewer:
                query = query.filter(Report.assigned_reviewer == reviewer)
            if q:
                query = query.filter(_Run.project.ilike(f"%{q}%"))
            total = query.count()
            rows = (
                query.order_by(Report.created_at.desc())
                .offset(page * _PAGE_SIZE).limit(_PAGE_SIZE).all()
            )
            report_rows = []
            for r in rows:
                run = s.get(_Run, r.run_id)
                target = s.get(TargetModel, run.target_id) if run.target_id else None
                pv = s.get(PipelineVersion, run.pipeline_version_id) if run.pipeline_version_id else None
                report_rows.append({
                    "id": r.id,
                    "state": r.state,
                    "verdict": r.verdict,
                    "project": run.project,
                    "summary": dict(r.summary) if r.summary else {},
                    "target_adapter": target.adapter if target else None,
                    "target_model": target.model if target else None,
                    "pipeline_version": pv.version_number if pv else None,
                    "triggered_by": run.triggered_by,
                    "assigned_reviewer": r.assigned_reviewer,
                    "created_at": str(r.created_at)[:19],
                })
        params = "&".join(
            f"{k}={v}" for k, v in [("q", q), ("project", project), ("state", state),
                                      ("verdict", verdict), ("reviewer", reviewer)] if v
        )
        return templates.TemplateResponse(request, "reports.html", {
            "reports": report_rows,
            "total": total,
            "page": page,
            "page_size": _PAGE_SIZE,
            "current_params": params,
            "q_filter": q or "",
            "project_filter": project or "",
            "state_filter": state or "",
            "verdict_filter": verdict or "",
            "reviewer_filter": reviewer or "",
            "identity": _identity(request),
        })
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
    actor = _require_identity(request, x_assay_user)
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
    actor = _require_identity(request, x_assay_user)
    verdict = body.verdict if body.verdict else None   # normalise "" → None
    try:
        adjudicate_case(report_id, case_result_id, verdict, actor, body.reason)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
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


# ── Transcript (lazy-loaded case detail) ───────────────────────────────────

@app.get("/reports/{report_id}/cases/{case_result_id}/transcript", response_class=HTMLResponse)
def case_transcript(report_id: int, case_result_id: int, request: Request):
    from ..store.models import CaseResult as CR
    with session_scope() as s:
        cr = s.get(CR, case_result_id)
        if not cr:
            raise HTTPException(404, "case result not found")
        rep = s.get(Report, report_id)
        if not rep or cr.run_id != rep.run_id:
            raise HTTPException(404, "case result not in report")
        ctx = {
            "cr_request": dict(cr.request or {}),
            "cr_response": dict(cr.response or {}),
            "checks": list(cr.checks or []),
        }
    return templates.TemplateResponse(request, "_transcript.html", ctx)


# ── Set verdict ─────────────────────────────────────────────────────────────

class SetVerdictBody(BaseModel):
    verdict: str
    reason: str


@app.post("/reports/{report_id}/set-verdict")
def set_verdict_route(
    report_id: int,
    body: SetVerdictBody,
    request: Request,
    x_assay_user: str | None = Header(default=None),
):
    from ..engine.review import set_verdict as _set_verdict
    actor = _require_identity(request, x_assay_user)
    try:
        _set_verdict(report_id, body.verdict, body.reason, actor)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    export_report(_run_for(report_id))
    if _is_htmx(request):
        return Response(headers={"HX-Redirect": f"/reports/{report_id}"})
    return {"ok": True, "verdict": body.verdict, "verdict_set_by": actor}


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
    actor = _require_identity(request, x_assay_user)
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
