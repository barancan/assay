"""CRUD operations for Pipeline and PipelineVersion records."""
from __future__ import annotations
import datetime as dt
from pathlib import Path
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
