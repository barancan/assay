"""Assay CLI:  init · generate · run · report · review · approve · users · serve · pipeline"""
from __future__ import annotations
from pathlib import Path
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, help="Assay — eval-pipeline builder.")
console = Console()

pipeline_app = typer.Typer(help="Manage DB pipeline records.")
app.add_typer(pipeline_app, name="pipeline")

target_app = typer.Typer(help="Target adapter utilities.")
app.add_typer(target_app, name="target")


@app.command()
def init(path: str = typer.Argument(".", help="project directory")):
    """Scaffold a project directory."""
    from . import config
    p = Path(path)
    (p / "generated" / "checks").mkdir(parents=True, exist_ok=True)
    (p / "generated" / "rubrics").mkdir(parents=True, exist_ok=True)
    (p / "datasets").mkdir(parents=True, exist_ok=True)
    req = p / "requirements.md"
    if not req.exists():
        req.write_text("# Assessment requirements\n\n"
                       "R1. The model must return valid JSON.\n"
                       "R2. Responses must complete within 5 seconds.\n")
    console.print(f"[green]initialised[/] {p.resolve()}")


@app.command()
def generate(
    requirements: str = typer.Option("requirements.md"),
    target_adapter: str = typer.Option("mock", "--adapter"),
    target_import: str = typer.Option(None, "--import"),
    request: str = typer.Option(None),
    out: str = typer.Option("."),
    judge: str = typer.Option(None, help="provider:model for LLM-assisted build"),
    project: str = typer.Option("project"),
    to_db: bool = typer.Option(False, "--to-db",
                               help="store pipeline in DB instead of writing files to disk"),
    by: str = typer.Option(None, "--by", help="creator identity (DB path only)"),
):
    """Build the pipeline from requirements + target interface.

    Default: emit assay.yaml + generated/ to disk (eval-as-code, commit to git).
    Use --to-db to store as a draft PipelineVersion in the DB instead.
    """
    from .adapters import get_judge_provider
    from .spec.models import JudgeSpec
    target = {"adapter": target_adapter}
    if target_import:
        target["import"] = target_import
    if request:
        target["request"] = request
    judge_obj, judges = None, None
    if judge:
        prov, model = judge.split(":", 1)
        judge_obj = get_judge_provider(JudgeSpec(provider=prov, model=model))
        judges = {"primary": {"provider": prov, "model": model}}

    if to_db:
        from .store.db import init_db
        from .generator.build import build_pipeline_to_db
        init_db()
        pv_id = build_pipeline_to_db(requirements, target, out, judge=judge_obj,
                                     judges=judges, project=project, created_by=by)
        console.print(f"[green]pipeline version {pv_id} created (draft)[/]")
        console.print("[yellow]activate it before running: assay pipeline activate "
                      f"{pv_id} --by REVIEWER[/]")
    else:
        from .generator import build_pipeline
        path = build_pipeline(requirements, target, out, judge=judge_obj,
                              judges=judges, project=project)
        console.print(f"[green]pipeline written[/] {path}")
        console.print("[yellow]review generated/ before running in production[/]")


@app.command()
def run(spec: str = typer.Option("assay.yaml"),
        trigger: str = typer.Option("manual"),
        by: str = typer.Option("cli", help="who triggered this")):
    """Execute the pipeline and store a report (state: pending)."""
    from .spec.loader import load_spec
    from .engine import execute_run, submit_for_review
    from .reporting import export_report
    sp = load_spec(spec)
    run_id = execute_run(sp, trigger=trigger, triggered_by=by)
    submit_for_review_id(run_id, by)
    paths = export_report(run_id)
    console.print(f"[green]run {run_id} complete[/] -> report state: ready_for_review")
    for fmt, p in paths.items():
        console.print(f"  {fmt}: {p}")


def submit_for_review_id(run_id: int, actor: str):
    from .engine import submit_for_review
    from .store import session_scope
    from .store.models import Report
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rid = rep.id
    submit_for_review(rid, actor=actor, note="auto-submitted after run")


@app.command()
def users(add: str = typer.Option(None, help="name to add"),
          role: str = typer.Option("runner", help="runner|reviewer|admin")):
    """List or add users (reviewer/admin can approve reports)."""
    from .store import session_scope
    from .store.models import User
    if add:
        with session_scope() as s:
            if not s.query(User).filter_by(name=add).one_or_none():
                s.add(User(name=add, role=role))
        console.print(f"[green]user added[/] {add} ({role})")
        return
    with session_scope() as s:
        rows = s.query(User).all()
        t = Table("id", "name", "role")
        for u in rows:
            t.add_row(str(u.id), u.name, u.role)
    console.print(t)


@app.command()
def report():
    """List reports and their states."""
    from .store import session_scope
    from .store.models import Report, Run
    with session_scope() as s:
        rows = s.query(Report).all()
        t = Table("report", "run", "project", "state", "approved_by", "summary")
        for r in rows:
            run = s.get(Run, r.run_id)
            t.add_row(str(r.id), str(r.run_id), run.project, r.state,
                      r.approved_by or "—", str(r.summary))
    console.print(t)


@app.command()
def approve(report_id: int, approver: str = typer.Option(...),
            note: str = typer.Option(None)):
    """Mark a report done (requires reviewer/admin authority)."""
    from .engine import approve_report
    try:
        approve_report(report_id, approver, note)
        console.print(f"[green]report {report_id} -> done[/] by {approver}")
    except PermissionError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)


@target_app.command("ping")
def target_ping(
    adapter: str = typer.Option("mock", "--adapter", help="mock|rest|anthropic|openai_compat|ollama"),
    endpoint: str = typer.Option(None, "--endpoint", help="target endpoint URL"),
    model: str = typer.Option(None, "--model", help="model name"),
):
    """Test connectivity to a target adapter."""
    from .spec.models import TargetSpec
    from .adapters import get_target_adapter
    from .adapters.registry import test_connection
    spec = TargetSpec(adapter=adapter, endpoint=endpoint, model=model)
    tgt = get_target_adapter(spec)
    result = tgt.ping()
    if result["ok"]:
        console.print(f"[green]ok[/] latency {result['latency_ms']:.1f} ms")
    else:
        console.print(f"[red]unreachable[/] {result['error']}")
        raise typer.Exit(1)


@pipeline_app.command("import")
def pipeline_import(
    spec: str = typer.Option("assay.yaml", "--spec", help="path to assay.yaml"),
    project: str = typer.Option("default", "--project", help="project name"),
    by: str = typer.Option(None, "--by", help="creator identity"),
):
    """Import assay.yaml into the DB as a draft PipelineVersion."""
    from .store.db import init_db
    from .pipeline import import_from_yaml
    init_db()
    pv = import_from_yaml(spec, project, by)
    console.print(f"[green]imported[/] pipeline version {pv.id}  "
                  f"hash: {pv.content_hash[:12]}…  status: {pv.status}")


@pipeline_app.command("list")
def pipeline_list():
    """List all pipelines in the DB."""
    from .store.db import init_db
    from .store import session_scope
    from .store.models import Pipeline, PipelineVersion
    init_db()
    with session_scope() as s:
        rows = s.query(Pipeline).all()
        t = Table("id", "project", "name", "versions", "created_by")
        for p in rows:
            n = s.query(PipelineVersion).filter_by(pipeline_id=p.id).count()
            t.add_row(str(p.id), p.project, p.name, str(n), p.created_by or "—")
    console.print(t)


@pipeline_app.command("activate")
def pipeline_activate(
    version_id: int = typer.Argument(..., help="PipelineVersion id to activate"),
    by: str = typer.Option(..., "--by", help="actor name (must be reviewer/admin)"),
):
    """Activate a draft pipeline version so it can be used in runs."""
    from .store.db import init_db
    from .pipeline import activate_version
    init_db()
    try:
        activate_version(version_id, by)
        console.print(f"[green]version {version_id} activated[/] by {by}")
    except (PermissionError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)


@pipeline_app.command("show")
def pipeline_show(version_id: int = typer.Argument(..., help="PipelineVersion id")):
    """Show details of a specific pipeline version."""
    from .store.db import init_db
    from .pipeline import get_version
    init_db()
    pv = get_version(version_id)
    if not pv:
        console.print(f"[red]version {version_id} not found[/]")
        raise typer.Exit(1)
    console.print(f"id:               {pv.id}")
    console.print(f"pipeline_id:      {pv.pipeline_id}")
    console.print(f"version_number:   {pv.version_number}")
    console.print(f"status:           {pv.status}")
    console.print(f"content_hash:     {pv.content_hash}")
    console.print(f"created_at:       {pv.created_at}")
    console.print(f"created_by:       {pv.created_by or '—'}")
    console.print(f"generated_sources: {list(pv.generated_sources.keys())}")
    console.print(f"rubrics:          {list(pv.rubrics.keys())}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8080):
    """Run the review/approval web API."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]pip install 'assay-eval[server]' to use serve[/]")
        raise typer.Exit(1)
    uvicorn.run("assay.server.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
