"""Build and save downloadable reports (json / md / html)."""
from __future__ import annotations
import json
from pathlib import Path
from .. import config
from ..store import session_scope
from ..store.models import Run, CaseResult, Report


def _gather(run_id: int) -> dict:
    with session_scope() as s:
        run = s.get(Run, run_id)
        report = s.query(Report).filter_by(run_id=run_id).one()
        results = s.query(CaseResult).filter_by(run_id=run_id).all()
        target = run.target
        data = {
            "project": run.project, "run_id": run.id, "spec_hash": run.spec_hash,
            "git_commit": run.git_commit, "trigger": run.trigger,
            "triggered_by": run.triggered_by, "state": report.state,
            "approved_by": report.approved_by,
            "approved_at": str(report.approved_at) if report.approved_at else None,
            "target": {"adapter": target.adapter, "model": target.model,
                       "endpoint": target.endpoint, "params": target.params},
            "summary": report.summary,
            "cost_usd": run.total_cost_usd,
            "cases": [{"suite": r.suite_id, "case": r.case_id,
                       "requirement_ref": r.requirement_ref, "passed": r.passed,
                       "latency_ms": r.latency_ms, "checks": r.checks,
                       "response": r.response} for r in results],
        }
        report_pk = report.id
    return data, report_pk


def _coverage(data: dict) -> dict:
    refs = {}
    for c in data["cases"]:
        ref = c.get("requirement_ref") or "(unmapped)"
        refs.setdefault(ref, {"total": 0, "passed": 0})
        refs[ref]["total"] += 1
        refs[ref]["passed"] += int(c["passed"])
    return refs


def _md(data: dict) -> str:
    cov = _coverage(data)
    lines = [f"# Assay report — {data['project']} (run {data['run_id']})", "",
             f"- **State:** {data['state']}",
             f"- **Approved by:** {data['approved_by'] or '—'} ({data['approved_at'] or '—'})",
             f"- **Target:** `{data['target']['adapter']}` "
             f"{data['target']['model'] or ''} {data['target']['endpoint'] or ''}".strip(),
             f"- **Spec hash:** `{data['spec_hash']}`  •  **Commit:** `{data['git_commit'] or '—'}`",
             f"- **Trigger:** {data['trigger']} by {data['triggered_by']}",
             f"- **Summary:** {data['summary']}  •  **Cost:** ${data['cost_usd']:.4f}", "",
             "## Requirement coverage", ""]
    for ref, c in cov.items():
        lines.append(f"- `{ref}` — {c['passed']}/{c['total']} passed")
    lines += ["", "## Cases", ""]
    for c in data["cases"]:
        flag = "PASS" if c["passed"] else "FAIL"
        lines.append(f"### [{flag}] {c['suite']} / {c['case']}  ({c['latency_ms']:.0f} ms)")
        for chk in c["checks"]:
            mark = "ok" if chk["passed"] else "X"
            lines.append(f"  - [{mark}] {chk['check_id']}: {chk['message']}")
        lines.append("")
    return "\n".join(lines)


def _html(data: dict) -> str:
    import html
    body = _md(data)
    return ("<!doctype html><meta charset=utf-8>"
            "<title>Assay report</title>"
            "<style>body{font:15px/1.5 system-ui;max-width:60rem;margin:2rem auto;padding:0 1rem}"
            "pre{white-space:pre-wrap}</style>"
            f"<pre>{html.escape(body)}</pre>")


def export_report(run_id: int, formats: list[str] | None = None) -> dict:
    formats = formats or ["json", "md", "html"]
    data, report_pk = _gather(run_id)
    out_dir = Path(config.REPORTS_DIR) / f"run_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    if "json" in formats:
        p = out_dir / "report.json"; p.write_text(json.dumps(data, indent=2, default=str)); paths["json"] = str(p)
    if "md" in formats:
        p = out_dir / "report.md"; p.write_text(_md(data)); paths["md"] = str(p)
    if "html" in formats:
        p = out_dir / "report.html"; p.write_text(_html(data)); paths["html"] = str(p)
    with session_scope() as s:
        rep = s.get(Report, report_pk)
        rep.export_paths = paths
    return paths
