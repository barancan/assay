"""CRUD operations for Pipeline and PipelineVersion records."""
from __future__ import annotations
import datetime as dt
from pathlib import Path  # used by regenerate_check stub source naming
from sqlalchemy import func
from ..store import session_scope
from ..store.models import Pipeline, PipelineVersion, User
from .hash import content_hash


def create_pipeline(
    project: str,
    name: str,
    created_by: str | None = None,
    description: str | None = None,
) -> Pipeline:
    with session_scope() as s:
        p = Pipeline(project=project, name=name, created_by=created_by, description=description)
        s.add(p)
        s.flush()
        pid = p.id
    with session_scope() as s:
        return s.get(Pipeline, pid)


def create_version(
    pipeline_id: int,
    config: dict,
    generated_sources: dict | None = None,
    rubrics: dict | None = None,
    created_by: str | None = None,
) -> PipelineVersion:
    sources = generated_sources or {}
    rubs = rubrics or {}
    ch = content_hash(config, sources, rubs)
    with session_scope() as s:
        max_ver = (
            s.query(func.max(PipelineVersion.version_number))
            .filter_by(pipeline_id=pipeline_id)
            .scalar()
        ) or 0
        pv = PipelineVersion(
            pipeline_id=pipeline_id,
            version_number=max_ver + 1,
            config=config,
            generated_sources=sources,
            rubrics=rubs,
            content_hash=ch,
            status="draft",
            created_by=created_by,
        )
        s.add(pv)
        s.flush()
        pv_id = pv.id
    with session_scope() as s:
        return s.get(PipelineVersion, pv_id)


def import_from_yaml(
    spec_path: str,
    project: str,
    created_by: str | None = None,
) -> PipelineVersion:
    """Read assay.yaml from disk, inline referenced generated sources and rubrics, persist."""
    from ..spec.loader import load_spec
    spec = load_spec(spec_path)
    spec_dir = Path(spec_path).parent

    config_dict = spec.model_dump(by_alias=True)

    generated_sources: dict[str, str] = {}
    rubrics: dict[str, str] = {}
    for suite in spec.suites:
        for case in suite.cases:
            for check in case.checks:
                if check.type == "generated" and check.uses:
                    p = spec_dir / check.uses
                    if p.exists():
                        generated_sources[check.uses] = p.read_text()
                elif check.type == "judge" and check.rubric:
                    p = spec_dir / check.rubric
                    if p.exists():
                        rubrics[check.rubric] = p.read_text()

    with session_scope() as s:
        pipeline = s.query(Pipeline).filter_by(project=project, name=spec.project).one_or_none()
        if pipeline is None:
            pipeline = Pipeline(project=project, name=spec.project, created_by=created_by)
            s.add(pipeline)
            s.flush()
        pid = pipeline.id

    return create_version(pid, config_dict, generated_sources, rubrics, created_by)


def activate_version(version_id: int, actor: str) -> None:
    """Promote a draft PipelineVersion to active.

    Requires actor to be reviewer/admin (unless the User table is empty,
    in which case any named actor is trusted — solo-dev path).
    Archives any currently active version for the same pipeline.
    """
    with session_scope() as s:
        pv = s.get(PipelineVersion, version_id)
        if pv is None:
            raise ValueError(f"PipelineVersion {version_id} not found")

        users_exist = s.query(User).first() is not None
        if users_exist:
            user = s.query(User).filter_by(name=actor).one_or_none()
            if user is None or user.role not in ("reviewer", "admin"):
                raise PermissionError(
                    f"'{actor}' lacks reviewer authority to activate a pipeline version"
                )

        # Archive any currently active version for this pipeline.
        (
            s.query(PipelineVersion)
            .filter_by(pipeline_id=pv.pipeline_id, status="active")
            .update({"status": "archived"})
        )

        pv.status = "active"
        pv.activated_by = actor
        pv.activated_at = dt.datetime.now(dt.timezone.utc)


def save_draft_from_requirements(
    project: str,
    name: str,
    requirements: str,
    created_by: str | None = None,
) -> int:
    """Create a draft PipelineVersion from raw requirements text. Returns version_id."""
    config_dict = {
        "version": 1,
        "project": project,
        "requirements": requirements,
        "target": {"adapter": "mock"},
        "judges": {},
        "suites": [],
        "gating": {},
    }
    with session_scope() as s:
        pipeline = s.query(Pipeline).filter_by(project=project, name=name).one_or_none()
        if pipeline is None:
            pipeline = Pipeline(project=project, name=name, created_by=created_by)
            s.add(pipeline)
            s.flush()
        pid = pipeline.id
    pv = create_version(pid, config_dict, {}, {}, created_by)
    update_step_reached(pv.id, "define")
    return pv.id


def update_step_reached(version_id: int, step: str) -> None:
    """Record the draft step reached (define | connect | review)."""
    with session_scope() as s:
        pv = s.get(PipelineVersion, version_id)
        if pv is None:
            raise ValueError(f"PipelineVersion {version_id} not found")
        pv.step_reached = step


def regenerate_check(version_id: int, check_path: str, actor: str) -> int:
    """Clone a draft PipelineVersion with one generated-check source regenerated.

    Only works on draft versions. Returns the new PipelineVersion id.
    """
    with session_scope() as s:
        pv = s.get(PipelineVersion, version_id)
        if pv is None:
            raise ValueError(f"PipelineVersion {version_id} not found")
        if pv.status != "draft":
            raise ValueError(
                f"can only regenerate checks on a draft version (status: {pv.status})"
            )
        existing = pv.generated_sources or {}
        if check_path not in existing:
            raise ValueError(f"generated check '{check_path}' not found in this version")

        # Find the assertion for this check in config so the stub is meaningful.
        assertion = ""
        for suite in (pv.config or {}).get("suites", []):
            for case in suite.get("cases", []):
                for chk in case.get("checks", []):
                    if chk.get("uses") == check_path:
                        assertion = case.get("id", check_path)

        fn_name = Path(check_path).stem.replace("-", "_")
        new_source = (
            f"# Regenerated check: {assertion}\n"
            f"def {fn_name}(response, **kwargs):\n"
            f"    # TODO: implement check logic\n"
            f"    return True\n"
        )
        new_sources = {**existing, check_path: new_source}
        pid = pv.pipeline_id
        config = dict(pv.config or {})
        rubrics = dict(pv.rubrics or {})

    return create_version(pid, config, new_sources, rubrics, actor).id


def update_check_source(version_id: int, check_path: str, source: str) -> None:
    """Inline-edit the source for a generated check in a draft PipelineVersion."""
    with session_scope() as s:
        pv = s.get(PipelineVersion, version_id)
        if pv is None:
            raise ValueError(f"PipelineVersion {version_id} not found")
        if pv.status != "draft":
            raise ValueError(
                f"can only edit checks on a draft version (status: {pv.status})"
            )
        sources = dict(pv.generated_sources or {})
        sources[check_path] = source
        pv.generated_sources = sources


def get_version(version_id: int) -> PipelineVersion | None:
    with session_scope() as s:
        return s.get(PipelineVersion, version_id)


def list_versions(pipeline_id: int) -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(PipelineVersion)
            .filter_by(pipeline_id=pipeline_id)
            .order_by(PipelineVersion.version_number)
            .all()
        )
        return [
            {
                "id": v.id,
                "version_number": v.version_number,
                "status": v.status,
                "content_hash": v.content_hash,
                "created_at": str(v.created_at),
                "created_by": v.created_by,
            }
            for v in rows
        ]
