"""SQLAlchemy ORM — every hard-requirement record is first-class and queryable."""
from __future__ import annotations
import datetime as dt
from sqlalchemy import (String, Integer, Float, Text, DateTime, ForeignKey, JSON, Boolean)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Pipeline(Base):
    """Durable, reusable pipeline record — the what/how of an eval."""
    __tablename__ = "pipelines"
    id: Mapped[int] = mapped_column(primary_key=True)
    project: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    versions: Mapped[list["PipelineVersion"]] = relationship(
        back_populates="pipeline", order_by="PipelineVersion.version_number"
    )


class PipelineVersion(Base):
    """Immutable snapshot: spec + generated source + rubrics + content hash."""
    __tablename__ = "pipeline_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"))
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON, default=dict)          # full spec dict
    generated_sources: Mapped[dict] = mapped_column(JSON, default=dict)  # {path: source}
    rubrics: Mapped[dict] = mapped_column(JSON, default=dict)          # {path: yaml_text}
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="draft")   # draft|active|archived
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pipeline: Mapped["Pipeline"] = relationship(back_populates="versions")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(String(20), default="runner")  # runner|reviewer|admin


class TargetModel(Base):
    """Snapshot of the model under test — req: tested model + details recorded."""
    __tablename__ = "target_models"
    id: Mapped[int] = mapped_column(primary_key=True)
    project: Mapped[str] = mapped_column(String(120))
    adapter: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(400), nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    interface_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project: Mapped[str] = mapped_column(String(120))
    spec_hash: Mapped[str] = mapped_column(String(32))
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target_models.id"))
    pipeline_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_versions.id"), nullable=True
    )
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual|auto|ci
    triggered_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|complete|error
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    results: Mapped[list["CaseResult"]] = relationship(back_populates="run")
    report: Mapped["Report"] = relationship(back_populates="run", uselist=False)
    target: Mapped["TargetModel"] = relationship()
    pipeline_version: Mapped["PipelineVersion | None"] = relationship(
        foreign_keys=[pipeline_version_id]
    )


class CaseResult(Base):
    """Per case per run — full request/response + every check captured for replay."""
    __tablename__ = "case_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    suite_id: Mapped[str] = mapped_column(String(120))
    case_id: Mapped[str] = mapped_column(String(120))
    requirement_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    # Adjudication columns (Phase 5)
    human_verdict: Mapped[str | None] = mapped_column(String(10), nullable=True)
    overridden_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    overridden_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    run: Mapped["Run"] = relationship(back_populates="results")

    @hybrid_property
    def effective_passed(self) -> bool:
        if self.human_verdict is not None:
            return self.human_verdict == "pass"
        return self.passed


class Report(Base):
    """Reviewable artifact with the required state machine + approver."""
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    state: Mapped[str] = mapped_column(String(20), default="pending")  # pending|ready_for_review|done
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    export_paths: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    # Reviewer assignment columns (Phase 5)
    assigned_reviewer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assigned_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assigned_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    run: Mapped["Run"] = relationship(back_populates="report")
    transitions: Mapped[list["StateTransition"]] = relationship(back_populates="report")


class StateTransition(Base):
    """Append-only audit log — who moved a report and when."""
    __tablename__ = "state_transitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"))
    from_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_state: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    report: Mapped["Report"] = relationship(back_populates="transitions")


class CaseAdjudication(Base):
    """Append-only audit log of every human verdict override on a case."""
    __tablename__ = "case_adjudications"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_result_id: Mapped[int] = mapped_column(ForeignKey("case_results.id"))
    action: Mapped[str] = mapped_column(String(10))     # set | clear
    verdict: Mapped[str | None] = mapped_column(String(10), nullable=True)
    actor: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class NotificationRecord(Base):
    """Stores the external ID (e.g. Linear issue id) created per report per channel."""
    __tablename__ = "notification_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"))
    channel: Mapped[str] = mapped_column(String(40))        # "linear"
    external_id: Mapped[str] = mapped_column(String(200))   # Linear issue UUID
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
