"""Engine + session helpers. SQLite by default; Postgres via ASSAY_DB_URL."""
from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .. import config
from .models import Base

_engine = None
_Session = None


def init_db():
    global _engine, _Session
    config.ensure_dirs()
    _engine = create_engine(config.DB_URL, future=True)
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
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
