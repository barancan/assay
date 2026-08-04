"""Engine + session helpers. SQLite by default; Postgres via ASSAY_DB_URL."""
from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .. import config
from .models import Base

_engine = None
_Session = None


# Portable column types. The hand-rolled migrations below previously emitted SQLite
# spellings unconditionally, which would fail against the Postgres path the README
# documents. Keyed by dialect name, with a fallback for anything unlisted.
_TYPE_MAP = {
    "timestamp": {"sqlite": "DATETIME", "postgresql": "TIMESTAMP"},
    "text": {"sqlite": "TEXT", "postgresql": "TEXT"},
    "int": {"sqlite": "INTEGER", "postgresql": "INTEGER"},
}


def _ddl_type(logical: str) -> str:
    """Map a logical column type to this dialect's spelling."""
    if logical.startswith("varchar"):
        return logical.upper()
    mapping = _TYPE_MAP.get(logical, {})
    return mapping.get(_engine.dialect.name, mapping.get("sqlite", logical.upper()))


def _add_columns(table: str, columns: list[tuple[str, str]]) -> None:
    """Add any of `columns` (name, logical_type) that `table` does not already have.

    Additive and nullable only -- existing rows read NULL. Safe to run on every start.
    """
    from sqlalchemy import text, inspect as sa_inspect
    existing = {col["name"] for col in sa_inspect(_engine).get_columns(table)}
    missing = [(name, t) for name, t in columns if name not in existing]
    if not missing:
        return
    with _engine.begin() as conn:
        for name, logical in missing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {_ddl_type(logical)}"))


def _migrate():
    """Add columns introduced after initial schema creation."""
    _add_columns("reports", [
        ("verdict", "varchar(20)"),
        ("verdict_reason", "text"),
        ("verdict_set_by", "varchar(120)"),
        ("verdict_set_at", "timestamp"),
    ])
    _add_columns("pipeline_versions", [("step_reached", "varchar(20)")])
    _add_columns("runs", [
        ("cases_total", "int"),
        ("error", "text"),
    ])


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
    connect_args = {}
    if config.DB_URL.startswith("sqlite"):
        # Runs execute on a background thread so the browser can watch progress;
        # SQLite's default same-thread guard would reject those connections.
        connect_args["check_same_thread"] = False
    _engine = create_engine(config.DB_URL, future=True, connect_args=connect_args)
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
