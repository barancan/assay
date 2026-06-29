"""Engine + session helpers. SQLite by default; Postgres via ASSAY_DB_URL."""
from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .. import config
from .models import Base

_engine = None
_Session = None


def _migrate():
    """Add columns introduced after initial schema creation."""
    from sqlalchemy import text, inspect as sa_inspect
    insp = sa_inspect(_engine)
    existing = {col["name"] for col in insp.get_columns("reports")}
    new_cols = [
        ("verdict", "VARCHAR(20)"),
        ("verdict_reason", "TEXT"),
        ("verdict_set_by", "VARCHAR(120)"),
        ("verdict_set_at", "DATETIME"),
    ]
    with _engine.begin() as conn:
        for col, col_type in new_cols:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE reports ADD COLUMN {col} {col_type}"))

    pv_existing = {col["name"] for col in insp.get_columns("pipeline_versions")}
    pv_new_cols = [("step_reached", "VARCHAR(20)")]
    with _engine.begin() as conn:
        for col, col_type in pv_new_cols:
            if col not in pv_existing:
                conn.execute(text(f"ALTER TABLE pipeline_versions ADD COLUMN {col} {col_type}"))


def _seed_settings():
    from .models import WorkspaceSetting
    s = _Session()
    try:
        if s.query(WorkspaceSetting).count() == 0:
            s.add(WorkspaceSetting(key="judge_adapter", value="anthropic"))
            s.add(WorkspaceSetting(key="judge_model", value="claude-haiku-4-5-20251001"))
            s.commit()
    finally:
        s.close()


def init_db():
    global _engine, _Session
    config.ensure_dirs()
    _engine = create_engine(config.DB_URL, future=True)
    Base.metadata.create_all(_engine)
    _migrate()
    _Session = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    _seed_settings()
    return _engine


@contextmanager
def session_scope():
    if _Session is None:
        init_db()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
