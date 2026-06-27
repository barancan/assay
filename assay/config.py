"""Process-wide configuration resolved from env vars."""
from __future__ import annotations
import os
from pathlib import Path

ASSAY_DIR = Path(os.environ.get("ASSAY_HOME", ".assay"))
DB_URL = os.environ.get("ASSAY_DB_URL", f"sqlite:///{ASSAY_DIR / 'assay.db'}")
REPORTS_DIR = ASSAY_DIR / "reports"

_DEV_SECRET = "assay-dev-secret"


def ensure_dirs() -> None:
    ASSAY_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def auth_mode() -> str:
    """Return 'open' (default) or 'enforced' based on ASSAY_AUTH env var."""
    return os.environ.get("ASSAY_AUTH", "open")


def secret_key() -> str:
    """Return the active signing key for session cookies."""
    return os.environ.get("ASSAY_SECRET_KEY", _DEV_SECRET)


def secret_is_default() -> bool:
    """True when ASSAY_SECRET_KEY is unset or equals the public dev default."""
    return secret_key() == _DEV_SECRET


def enforce_posture_or_raise() -> None:
    """In enforced mode, raise RuntimeError if the signing key is the public dev default."""
    if auth_mode() == "enforced" and secret_is_default():
        raise RuntimeError(
            "ASSAY_AUTH=enforced requires a real ASSAY_SECRET_KEY.\n"
            "Generate one:  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "Then set:      ASSAY_SECRET_KEY=<value>"
        )
