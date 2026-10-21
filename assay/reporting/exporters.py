"""Build and save downloadable reports (json / md / html)."""
from __future__ import annotations
import json
from pathlib import Path
from .. import config
from ..store import session_scope
from ..store.models import Run, CaseResult, Report


def _requirements_for(run) -> list[dict]:
    """The requirement list this run was built from, for the coverage matrix.

    Requirements live as raw text on the pipeline version, so they are re-split with
    the same function the builder used -- ids are deterministic, so R3 here is the
    same R3 the intents cite.
    """
    pv = getattr(run, "pipeline_version", None)
    config = getattr(pv, "config", None) or {}
    text = config.get("requirements")
    if not text:
        return []
    try:
        from ..generator.ingest import split_requirements
        return split_requirements(text)
    except Exception:
        # Coverage is a reporting nicety; never let it break an export.
        return []


def _gather(run_id: int) -> dict:
    with session_scope() as s:
        run = s.get(Run, run_id)
        report = s.query(Report).filter_by(run_id=run_id).one()
        results = s.query(CaseResult).filter_by(run_id=run_id).all()
        target = run.target
        requirements = _requirements_for(run)
        data = {
            "requirements": requirements,
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
                       "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                       "judge_tokens": r.judge_tokens, "cost_usd": r.cost_usd,
                       "response": r.response} for r in results],
        }
        data["spend"] = spend(data)
        report_pk = report.id
    return data, report_pk


def spend(data: dict) -> dict:
    """Token and dollar totals for a run, and how much of it is unaccounted for.

    `unknown_cases` is the honest part: `total_usd` only covers cases we could price,
    so a report that reads "$0.31" while three cases went unpriced has to say so
    instead of presenting a partial figure as the bill.
    """
    cases = data.get("cases") or []
    priced = [c for c in cases if c.get("cost_usd") is not None]
    unknown = [c for c in cases if c.get("cost_usd") is None]
    return {
        "total_usd": round(sum(c["cost_usd"] for c in priced), 10),
        "input_tokens": sum(int(c.get("input_tokens") or 0) for c in cases),
        "output_tokens": sum(int(c.get("output_tokens") or 0) for c in cases),
        "judge_tokens": sum(int(c.get("judge_tokens") or 0) for c in cases),
        "unknown_cases": len(unknown),
        "priced_cases": len(priced),
        # True only when every case carries a price, so a reader knows whether
        # total_usd is the bill or a lower bound on it.
        "complete": not unknown,
    }


def money(value: float | None) -> str:
    """Dollars, or the word unknown. Never renders a missing price as $0.0000."""
    return "unknown" if value is None else f"${value:.4f}"


def _coverage(data: dict) -> dict:
    """Pass counts keyed by requirement ref. One direction only -- see coverage()."""
    refs = {}
    for c in data["cases"]:
        ref = c.get("requirement_ref") or "(unmapped)"
        refs.setdefault(ref, {"total": 0, "passed": 0})
        refs[ref]["total"] += 1
        refs[ref]["passed"] += int(c["passed"])
    return refs


def coverage(data: dict) -> dict:
    """Requirement coverage in both directions.

    Counting cases per requirement only tells you about requirements that were tested.
    The interesting question is the other one: which requirements has nothing tested?
    A requirement with no case is invisible in a one-directional matrix, and it is
    exactly the thing a reviewer needs to see before signing a report off.

    The mirror image matters too: a case citing a requirement that no longer exists is
    an orphan, and it silently stops meaning anything when requirements are edited.
    """
    tested = _coverage(data)
    requirements = data.get("requirements") or []
    known = {r["id"] for r in requirements}

    by_requirement = []
    for r in requirements:
        counts = tested.get(r["id"], {"total": 0, "passed": 0})
        by_requirement.append({
            "id": r["id"],
            "text": r.get("text", ""),
            "section": r.get("section"),
            "total": counts["total"],
            "passed": counts["passed"],
            "covered": counts["total"] > 0,
        })

    uncovered = [r["id"] for r in by_requirement if not r["covered"]]
    # A ref pointing at nothing, plus cases that carry no ref at all.
    orphans = sorted(ref for ref in tested if ref not in known)

    covered_count = sum(1 for r in by_requirement if r["covered"])
    pct = round(covered_count / len(by_requirement) * 100, 1) if by_requirement else None

    return {
        "by_requirement": by_requirement,
        "tested": tested,
        "uncovered": uncovered,
        "orphans": orphans,
        "requirements_total": len(by_requirement),
        "requirements_covered": covered_count,
        "covered_pct": pct,
        # Without the requirements text we can only report one direction, and saying so
        # is better than rendering "0 uncovered" and implying full coverage.
        "known_requirements": bool(requirements),
    }


def _coverage_lines(data: dict) -> list[str]:
    cov = coverage(data)
    lines = ["## Requirement coverage", ""]

    if not cov["known_requirements"]:
        # Say so rather than showing "0 uncovered", which would read as full coverage.
        lines.append("_Requirement list unavailable for this run; "
                     "showing tested requirements only._")
        lines.append("")
        for ref, c in cov["tested"].items():
            lines.append(f"- `{ref}` — {c['passed']}/{c['total']} passed")
        return lines + [""]

    lines.append(f"**{cov['requirements_covered']}/{cov['requirements_total']} "
                 f"requirements covered ({cov['covered_pct']}%)**")
    lines.append("")
    for r in cov["by_requirement"]:
        if r["covered"]:
            lines.append(f"- `{r['id']}` — {r['passed']}/{r['total']} passed — {r['text']}")
        else:
            lines.append(f"- `{r['id']}` — **NOT COVERED** — {r['text']}")

    if cov["uncovered"]:
        lines += ["", f"> {len(cov['uncovered'])} requirement(s) have no test: "
                      + ", ".join(f"`{r}`" for r in cov["uncovered"])]
    if cov["orphans"]:
        lines += ["", "> Orphan tests, citing a requirement that no longer exists: "
                      + ", ".join(f"`{r}`" for r in cov["orphans"])]
    return lines + [""]


def _md(data: dict) -> str:
    lines = [f"# Assay report — {data['project']} (run {data['run_id']})", "",
             f"- **State:** {data['state']}",
             f"- **Approved by:** {data['approved_by'] or '—'} ({data['approved_at'] or '—'})",
             f"- **Target:** `{data['target']['adapter']}` "
             f"{data['target']['model'] or ''} {data['target']['endpoint'] or ''}".strip(),
             f"- **Spec hash:** `{data['spec_hash']}`  •  **Commit:** `{data['git_commit'] or '—'}`",
             f"- **Trigger:** {data['trigger']} by {data['triggered_by']}",
             f"- **Summary:** {data['summary']}", ""]
    lines += _spend_lines(data)
    lines += _coverage_lines(data)
    lines += ["## Cases", ""]
    for c in data["cases"]:
        flag = "PASS" if c["passed"] else "FAIL"
        lines.append(f"### [{flag}] {c['suite']} / {c['case']}  ({c['latency_ms']:.0f} ms)")
        lines.append(f"  - Spend: {money(c.get('cost_usd'))} — "
                     f"{_tokens(c.get('input_tokens'))} in / "
                     f"{_tokens(c.get('output_tokens'))} out / "
                     f"{_tokens(c.get('judge_tokens'))} judge tokens")
        for chk in c["checks"]:
            mark = "ok" if chk["passed"] else "X"
            lines.append(f"  - [{mark}] {chk['check_id']}: {chk['message']}")
        lines.append("")
    return "\n".join(lines)


def _tokens(value) -> str:
    return "unknown" if value is None else str(int(value))


def _spend_lines(data: dict) -> list[str]:
    s = data.get("spend") or spend(data)
    lines = ["## Spend", "",
             f"- **Total:** {money(s['total_usd'])}"
             + ("" if s["complete"] else "  (lower bound)"),
             f"- **Tokens:** {s['input_tokens']} in / {s['output_tokens']} out / "
             f"{s['judge_tokens']} judge"]
    if not s["complete"]:
        # Naming the count stops the total reading as the whole bill.
        lines.append(f"- **{s['unknown_cases']} of {len(data['cases'])} case(s) could not "
                     "be priced** — the model is not in the price table. Set "
                     "ASSAY_PRICING_FILE to add it.")
    return lines + [""]


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
