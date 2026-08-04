"""Orchestrate one run: for each case -> invoke target -> run checks -> persist."""
from __future__ import annotations
import datetime as dt
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..spec.models import Spec
from ..spec.loader import spec_hash
from ..adapters import get_target_adapter, get_judge_provider
from ..adapters.registry import test_connection
from ..adapters.base import ModelRequest
from ..checks.registry import run_check
from ..checks.base import CheckResult
from .gating import case_passed
from ..store import session_scope
from ..store.models import Run, CaseResult, TargetModel, Report, StateTransition, PipelineVersion


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _resolve_spec(
    spec: Spec | None,
    pipeline_version_id: int | None,
) -> tuple[Spec, int | None, dict, dict]:
    """Return (spec, pv_id, generated_sources, rubrics).

    Exactly one of spec/pv_id must be given. A file-based spec references artifacts that
    already exist on disk, so both artifact dicts come back empty.
    """
    if pipeline_version_id is not None:
        with session_scope() as s:
            pv = s.get(PipelineVersion, pipeline_version_id)
        if pv is None:
            raise ValueError(f"PipelineVersion {pipeline_version_id} not found")
        if pv.status != "active":
            raise PermissionError(
                f"Pipeline version {pipeline_version_id} is not active (status: {pv.status})"
            )
        return (Spec.model_validate(pv.config), pipeline_version_id,
                dict(pv.generated_sources or {}), dict(pv.rubrics or {}))

    if spec is not None:
        return spec, None, {}, {}

    raise ValueError("Either spec or pipeline_version_id must be provided")


def _materialise_sources(
    generated_sources: dict,
    rubrics: dict | None = None,
) -> tuple[str | None, dict[str, str], dict[str, str]]:
    """Write DB-stored check sources and judge rubrics to a fresh temp dir.

    Returns (tmpdir, check_map, rubric_map) where each map is {orig_path: abs_tmp_path},
    or (None, {}, {}) when there is nothing to write. Checks and rubrics get their own
    subdirectories so identically-named artifacts cannot collide.
    The caller is responsible for shutil.rmtree(tmpdir) when done.
    """
    rubrics = rubrics or {}
    if not generated_sources and not rubrics:
        return None, {}, {}

    tmpdir = tempfile.mkdtemp(prefix="assay-run-")

    def _write(artifacts: dict, subdir: str) -> dict[str, str]:
        if not artifacts:
            return {}
        dest_dir = Path(tmpdir) / subdir
        dest_dir.mkdir(exist_ok=True)
        written: dict[str, str] = {}
        for orig_path, source in artifacts.items():
            dest = dest_dir / Path(orig_path).name
            dest.write_text(source)
            written[orig_path] = str(dest)
        return written

    return tmpdir, _write(generated_sources, "checks"), _write(rubrics, "rubrics")


def _patch_spec_paths(
    spec: Spec,
    check_map: dict[str, str],
    rubric_map: dict[str, str] | None = None,
) -> Spec:
    """Return a copy of spec with artifact paths rewritten to materialised temp paths."""
    rubric_map = rubric_map or {}

    def _patch(c):
        if c.type == "generated" and c.uses in check_map:
            return c.model_copy(update={"uses": check_map[c.uses]})
        if c.type == "judge" and c.rubric in rubric_map:
            return c.model_copy(update={"rubric": rubric_map[c.rubric]})
        return c

    new_suites = []
    for suite in spec.suites:
        new_cases = []
        for case in suite.cases:
            new_checks = [_patch(c) for c in case.checks]
            new_cases.append(case.model_copy(update={"checks": new_checks}))
        new_suites.append(suite.model_copy(update={"cases": new_cases}))
    return spec.model_copy(update={"suites": new_suites})


def execute_run(
    spec: Spec | None = None,
    *,
    pipeline_version_id: int | None = None,
    trigger: str = "manual",
    triggered_by: str = "cli",
) -> int:
    spec, pv_id, generated_sources, rubrics = _resolve_spec(spec, pipeline_version_id)

    tmpdir = None
    try:
        if generated_sources or rubrics:
            tmpdir, check_map, rubric_map = _materialise_sources(generated_sources, rubrics)
            if check_map or rubric_map:
                spec = _patch_spec_paths(spec, check_map, rubric_map)

        target = get_target_adapter(spec.target)
        judges = {k: get_judge_provider(v) for k, v in spec.judges.items()}

        # Fail fast before touching the DB if the target is unreachable.
        test_connection(target)

        with session_scope() as s:
            tm = TargetModel(project=spec.project, adapter=spec.target.adapter,
                             model=spec.target.model, endpoint=spec.target.endpoint,
                             params=spec.target.params)
            s.add(tm)
            s.flush()
            run = Run(project=spec.project, spec_hash=spec_hash(spec),
                      git_commit=_git_commit(), target_id=tm.id,
                      pipeline_version_id=pv_id,
                      trigger=trigger, triggered_by=triggered_by, status="running")
            s.add(run)
            s.flush()
            run_id = run.id

            case_flags: list[bool] = []
            total_cost = 0.0
            for suite in spec.suites:
                for case in suite.cases:
                    req = ModelRequest(input=case.input, params=spec.target.params)
                    resp = target.invoke(req)
                    total_cost += resp.cost_usd or 0.0
                    rdict = resp.as_dict()
                    ctx = {"input": case.input, "suite": suite.id, "case": case.id}
                    results: list[CheckResult] = [
                        run_check(c, rdict, ctx, judges) for c in case.checks]
                    ok = case_passed(results)
                    case_flags.append(ok)
                    s.add(CaseResult(
                        run_id=run_id, suite_id=suite.id, case_id=case.id,
                        requirement_ref=suite.requirement_ref,
                        request={"input": case.input}, response=rdict,
                        checks=[r.to_dict() for r in results], passed=ok,
                        latency_ms=resp.latency_ms))

            run.status = "complete"
            run.finished_at = dt.datetime.now(dt.timezone.utc)
            run.total_cost_usd = total_cost

            summary = {"cases": len(case_flags), "passed": sum(case_flags),
                       "failed": len(case_flags) - sum(case_flags)}
            report = Report(run_id=run_id, state="pending", summary=summary)
            s.add(report)
            s.flush()
            s.add(StateTransition(report_id=report.id, from_state=None,
                                  to_state="pending", actor=triggered_by,
                                  note="run created"))
        return run_id

    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
