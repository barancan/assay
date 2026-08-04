"""Orchestrate one run: for each case -> invoke target -> run checks -> persist."""
from __future__ import annotations
import datetime as dt
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
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


def _interface_hash(spec: Spec) -> str | None:
    """Hash of the target's interface description, so a run records what it tested against.

    The column has existed since the first schema and was never populated, which meant a
    report could not tell you whether the interface had changed underneath it.
    """
    try:
        from ..generator.interface import interface_from_target
        return interface_from_target(spec.target).hash or None
    except Exception:
        # Provenance is worth recording but never worth failing a run over.
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


@dataclass
class _RunContext:
    """Everything a run needs after setup, so the case loop can run anywhere."""
    run_id: int
    spec: Spec
    target: object
    judges: dict
    tmpdir: str | None
    triggered_by: str


def _setup_run(
    spec: Spec | None,
    pipeline_version_id: int | None,
    trigger: str,
    triggered_by: str,
) -> _RunContext:
    """Resolve the spec, materialise artifacts, reach the target, create the Run row.

    Everything here is fast and fails loudly, so a caller that dispatches the case
    loop to a thread still surfaces "unreachable target" synchronously.
    """
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

        total_cases = sum(len(suite.cases) for suite in spec.suites)
        with session_scope() as s:
            tm = TargetModel(project=spec.project, adapter=spec.target.adapter,
                             model=spec.target.model, endpoint=spec.target.endpoint,
                             params=spec.target.params,
                             interface_hash=_interface_hash(spec))
            s.add(tm)
            s.flush()
            run = Run(project=spec.project, spec_hash=spec_hash(spec),
                      git_commit=_git_commit(), target_id=tm.id,
                      pipeline_version_id=pv_id,
                      trigger=trigger, triggered_by=triggered_by, status="running",
                      cases_total=total_cases)
            s.add(run)
            s.flush()
            run_id = run.id
    except Exception:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    return _RunContext(run_id=run_id, spec=spec, target=target, judges=judges,
                       tmpdir=tmpdir, triggered_by=triggered_by)


def _execute_cases(ctx: _RunContext) -> None:
    """Run every case, committing after each one so progress is observable.

    Each case is its own transaction. A run that dies half way therefore keeps the
    results it did produce, and the progress view can count them as they land.
    """
    case_flags: list[bool] = []
    total_cost = 0.0
    spec, target, judges = ctx.spec, ctx.target, ctx.judges

    for suite in spec.suites:
        for case in suite.cases:
            req = ModelRequest(input=case.input, params=spec.target.params)
            resp = target.invoke(req)
            total_cost += resp.cost_usd or 0.0
            rdict = resp.as_dict()
            cctx = {"input": case.input, "suite": suite.id, "case": case.id}
            results: list[CheckResult] = [
                run_check(c, rdict, cctx, judges) for c in case.checks]
            ok = case_passed(results)
            case_flags.append(ok)
            with session_scope() as s:
                s.add(CaseResult(
                    run_id=ctx.run_id, suite_id=suite.id, case_id=case.id,
                    requirement_ref=suite.requirement_ref,
                    request={"input": case.input}, response=rdict,
                    checks=[r.to_dict() for r in results], passed=ok,
                    latency_ms=resp.latency_ms))

    with session_scope() as s:
        run = s.get(Run, ctx.run_id)
        run.status = "complete"
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        run.total_cost_usd = total_cost

        summary = {"cases": len(case_flags), "passed": sum(case_flags),
                   "failed": len(case_flags) - sum(case_flags)}
        report = Report(run_id=ctx.run_id, state="pending", summary=summary)
        s.add(report)
        s.flush()
        s.add(StateTransition(report_id=report.id, from_state=None,
                              to_state="pending", actor=ctx.triggered_by,
                              note="run created"))


def _mark_run_failed(run_id: int, exc: Exception) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is not None:
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"[:500]
            run.finished_at = dt.datetime.now(dt.timezone.utc)


def execute_run(
    spec: Spec | None = None,
    *,
    pipeline_version_id: int | None = None,
    trigger: str = "manual",
    triggered_by: str = "cli",
) -> int:
    """Run a pipeline to completion synchronously and return the run id."""
    ctx = _setup_run(spec, pipeline_version_id, trigger, triggered_by)
    try:
        _execute_cases(ctx)
        return ctx.run_id
    except Exception as exc:
        _mark_run_failed(ctx.run_id, exc)
        raise
    finally:
        if ctx.tmpdir:
            shutil.rmtree(ctx.tmpdir, ignore_errors=True)


def start_run(
    spec: Spec | None = None,
    *,
    pipeline_version_id: int | None = None,
    trigger: str = "manual",
    triggered_by: str = "cli",
    submit: bool = True,
) -> int:
    """Begin a run on a background thread and return its id immediately.

    Setup -- resolving the spec and reaching the target -- still happens on the
    calling thread, so an unreachable target or an inactive version raises here
    rather than disappearing into the background. Only the case loop is deferred,
    which is the part that takes real time once real models are involved.

    With `submit`, the finished run is moved to `ready_for_review` and exported, the
    same way the synchronous HTTP path does it.
    """
    ctx = _setup_run(spec, pipeline_version_id, trigger, triggered_by)

    def _worker() -> None:
        try:
            _execute_cases(ctx)
            if submit:
                _submit_and_export(ctx.run_id, ctx.triggered_by)
        except Exception as exc:                      # noqa: BLE001 - recorded on the run
            _mark_run_failed(ctx.run_id, exc)
        finally:
            if ctx.tmpdir:
                shutil.rmtree(ctx.tmpdir, ignore_errors=True)

    thread = threading.Thread(target=_worker, name=f"assay-run-{ctx.run_id}", daemon=True)
    _track(thread)
    thread.start()
    return ctx.run_id


# Background runs resolve the session factory from module globals every time they touch
# the DB, so a run still in flight when the process reconfigures its store will write
# somewhere unexpected. Keeping handles lets callers wait for quiescence -- which tests
# need between cases, and a graceful shutdown wants before exiting.
_RUN_THREADS: list[threading.Thread] = []
_RUN_THREADS_LOCK = threading.Lock()


def _track(thread: threading.Thread) -> None:
    with _RUN_THREADS_LOCK:
        _RUN_THREADS[:] = [t for t in _RUN_THREADS if t.is_alive()]
        _RUN_THREADS.append(thread)


def wait_for_runs(timeout: float = 30.0) -> bool:
    """Block until every background run finishes. True if all completed in time."""
    with _RUN_THREADS_LOCK:
        pending = list(_RUN_THREADS)
    deadline = time.monotonic() + timeout
    for thread in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in pending)


def _submit_and_export(run_id: int, actor: str) -> None:
    from .review import submit_for_review
    from ..reporting import export_report

    with session_scope() as s:
        report = s.query(Report).filter_by(run_id=run_id).one_or_none()
        report_id = report.id if report else None
    if report_id is not None:
        submit_for_review(report_id, actor=actor)
    export_report(run_id)


def run_progress(run_id: int) -> dict:
    """Current state of a run, for the progress view.

    `done` counts persisted case results, which is why _execute_cases commits per
    case rather than once at the end.
    """
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        done = s.query(CaseResult).filter_by(run_id=run_id).count()
        report = s.query(Report).filter_by(run_id=run_id).one_or_none()
        return {
            "run_id": run_id,
            "project": run.project,
            "status": run.status,
            "done": done,
            "total": run.cases_total,
            "report_id": report.id if report else None,
            "error": run.error,
            "started_at": run.started_at,
        }
