"""Process-wide configuration resolved from env vars."""
from __future__ import annotations
import os
from pathlib import Path

ASSAY_DIR = Path(os.environ.get("ASSAY_HOME", ".assay"))
DB_URL = os.environ.get("ASSAY_DB_URL", f"sqlite:///{ASSAY_DIR / 'assay.db'}")
REPORTS_DIR = ASSAY_DIR / "reports"


def ensure_dirs() -> None:
    ASSAY_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
