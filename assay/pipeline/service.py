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


def update_version_config(
    version_id: int,
    config: dict,
    generated_sources: dict | None = None,
    rubrics: dict | None = None,
) -> None:
    """Replace config of an existing draft PipelineVersion in place."""
    sources = generated_sources or {}
    rubs = rubrics or {}
    ch = content_hash(config, sources, rubs)
    with session_scope() as s:
        pv = s.get(PipelineVersion, version_id)
        if pv is None:
            raise ValueError(f"PipelineVersion {version_id} not found")
        if pv.status != "draft":
            raise ValueError(f"Can only update draft versions (status: {pv.status})")
        pv.config = config
        pv.generated_sources = sources
        pv.rubrics = rubs
        pv.content_hash = ch


def update_check_params(
    version_id: int,
    suite_id: str,
    case_id: str,
    check_index: int,
    params: dict,
) -> None:
    """Replace the `with` params of a single check in a draft PipelineVersion.

    Locates config.suites[suite_id].cases[case_id].checks[check_index] and swaps
    its `with` dict, then recomputes the content hash. Draft only.
    """
    import copy
    with session_scope() as s:
        pv = s.get(PipelineVersion, version_id)
        if pv is None:
            raise ValueError(f"PipelineVersion {version_id} not found")
        if pv.status != "draft":
            raise ValueError(f"Can only update draft versions (status: {pv.status})")
        # Deep-copy so the reassignment is a genuinely distinct object from the
        # loaded value — otherwise SQLAlchemy's JSON column won't flag the change.
        config = copy.deepcopy(dict(pv.config or {}))
        suites = config.get("suites", [])
        target = None
        for suite in suites:
            if suite.get("id") != suite_id:
                continue
            for case in suite.get("cases", []):
                if case.get("id") != case_id:
                    continue
                checks = case.get("checks", [])
                if check_index < 0 or check_index >= len(checks):
                    raise ValueError(
                        f"check_index {check_index} out of range for {suite_id}/{case_id}"
                    )
                target = checks[check_index]
                break
            if target is not None:
                break
        if target is None:
            raise ValueError(f"check not found: {suite_id}/{case_id}[{check_index}]")
        target["with"] = params
        pv.config = config
        pv.content_hash = content_hash(config, pv.generated_sources or {}, pv.rubrics or {})


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


class CodegenError(ValueError):
    """Codegen ran and could not produce a usable check.

    Carries the attempt count and every attempt's errors, so the caller can tell the
    reviewer what the model actually got wrong rather than "regeneration failed".
    """

    def __init__(self, message: str, *, attempts: int = 0,
                 errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.errors = errors or []


def _codegen_context(config: dict, check_path: str) -> tuple[dict, object]:
    """The intent and interface to regenerate `check_path` against.

    The spec is what survives a build, so the intent is reconstructed from it: the
    assertion recorded on the check, falling back to the case id. The interface is
    best-effort -- the file the target was imported from may not exist on this host, and
    an ungrounded interface still produces a runnable dry-run.
    """
    from ..generator.interface import Interface, parse_interface

    intent_id = Path(check_path).stem
    assertion = ""
    for suite in (config or {}).get("suites", []):
        for case in suite.get("cases", []):
            for chk in case.get("checks", []):
                if chk.get("uses") == check_path:
                    assertion = (chk.get("assertion") or case.get("id")
                                 or assertion or check_path)

    try:
        iface = parse_interface((config or {}).get("target", {}).get("import"))
    except Exception:
        iface = Interface()
    return {"id": intent_id, "assertion": assertion or intent_id,
            "how": "generated", "category": "auto"}, iface


def _unavailable_scaffold(check_path: str, assertion: str, reason: str) -> str:
    """What to store when there is no model to generate with at all.

    Still meets the sandbox contract -- a module-level check(response, context) -> dict --
    so it fails as an honest check verdict rather than as a contract violation, and says
    why in the message.
    """
    return (
        f"# Check for: {assertion}\n"
        f"# Not generated: {reason}\n"
        f"# Configure a builder model in Settings and regenerate, or write the body\n"
        f"# here via the inline editor on the pipeline review screen.\n"
        f"def check(response: dict, context: dict) -> dict:\n"
        f"    return {{\n"
        f'        "passed": False,\n'
        f'        "severity": "fail",\n'
        f'        "message": "check {check_path} was not generated: {reason}",\n'
        f"    }}\n"
    )


def regenerate_check(version_id: int, check_path: str, actor: str, llm=None) -> int:
    """Clone a draft PipelineVersion with one generated-check source regenerated.

    The source is written by the builder model and put through the same gate as a build:
    static validation, then a real dry-run in the sandbox against a sample response and
    against degraded ones, so what lands is known to load, to return a verdict, and to
    discriminate. A model that cannot get there after its repairs raises CodegenError
    with the attempts rather than persisting something broken.

    With no builder model configured there is nothing to generate with, so the stored
    source is a scaffold that fails loudly and names the reason.

    Only works on draft versions. Returns the new PipelineVersion id.
    """
    from ..generator.codegen import generate_check

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
        pid = pv.pipeline_id
        config = dict(pv.config or {})
        rubrics = dict(pv.rubrics or {})

    intent, iface = _codegen_context(config, check_path)

    if llm is None:
        from ..llm.provider import LLMConfigError, resolve_builder_llm
        try:
            llm = resolve_builder_llm(config.get("project"))
        except LLMConfigError:
            llm = None

    if llm is None:
        new_source = _unavailable_scaffold(
            check_path, intent["assertion"], "no builder model is configured")
        dry_run = None
    else:
        result = generate_check(intent, iface, llm)
        if not result.ok:
            raise CodegenError(
                f"could not generate a working check for '{check_path}' after "
                f"{result.attempts} attempt(s): "
                + "; ".join(result.errors[-3:] or ["no reason recorded"]),
                attempts=result.attempts, errors=result.errors)
        new_source = result.source
        dry_run = result.dry_run

    config = _record_dry_run(config, check_path, dry_run)
    new_sources = {**existing, check_path: new_source}
    return create_version(pid, config, new_sources, rubrics, actor).id


def _record_dry_run(config: dict, check_path: str, dry_run: dict | None) -> dict:
    """Attach this source's dry-run to build_meta, dropping the previous source's.

    The outcome shown on the review screen has to describe the source that is actually
    stored. Carrying the old one forward would tell a reviewer that a check they have
    never seen run had passed.
    """
    import copy
    config = copy.deepcopy(config)
    meta = config.setdefault("build_meta", {})
    runs = meta.setdefault("codegen", {})
    if dry_run is None:
        runs.pop(check_path, None)
    else:
        runs[check_path] = dry_run
    if not runs:
        meta.pop("codegen", None)
    if not meta:
        config.pop("build_meta", None)
    return config


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
        # The recorded dry-run described the source that was just replaced. Drop it
        # rather than let the review screen vouch for code nobody ran.
        pv.config = _record_dry_run(dict(pv.config or {}), check_path, None)


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
