"""Auth posture tests (ASSAY_AUTH=open vs enforced)."""
from __future__ import annotations
import importlib
import pytest
from itsdangerous import URLSafeSerializer

_DEV_SECRET = "assay-dev-secret"
_REAL_SECRET = "test-only-real-secret-not-the-dev-default"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.delenv("ASSAY_LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("ASSAY_AUTH", raising=False)
    monkeypatch.delenv("ASSAY_SECRET_KEY", raising=False)
    import assay.config
    import assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db
    init_db()
    yield


def _make_run_and_report():
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.engine import execute_run, submit_for_review
    from assay.store import session_scope
    from assay.store.models import Report
    p = create_pipeline(project="auth-test", name="auth-test")
    cfg = {
        "version": 1, "project": "auth-test",
        "target": {"adapter": "mock"}, "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [{"type": "template", "uses": "valid_json"}]}
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
    submit_for_review(rep_id, actor="tester")
    return run_id, rep_id


def _fresh_client(monkeypatch, *, auth: str | None = None, secret: str | None = None):
    """Set env vars, reload the app stack, return a fresh TestClient."""
    if auth is not None:
        monkeypatch.setenv("ASSAY_AUTH", auth)
    if secret is not None:
        monkeypatch.setenv("ASSAY_SECRET_KEY", secret)
    import assay.config
    importlib.reload(assay.config)
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


# ── 1. open mode: existing behaviour unchanged ─────────────────────────────

def test_open_mode_unchanged(monkeypatch):
    """Regression: open mode + empty User table -> approve still succeeds."""
    _, rep_id = _make_run_and_report()
    client = _fresh_client(monkeypatch)  # ASSAY_AUTH unset = open
    resp = client.post(f"/reports/{rep_id}/approve",
                       headers={"X-Assay-User": "solo-dev"})
    assert resp.status_code == 303


# ── 2. enforced mode refuses without a real secret ─────────────────────────

def test_enforced_requires_real_secret(monkeypatch):
    """enforce_posture_or_raise() raises RuntimeError when enforced + dev secret."""
    monkeypatch.setenv("ASSAY_AUTH", "enforced")
    monkeypatch.delenv("ASSAY_SECRET_KEY", raising=False)
    import assay.config
    importlib.reload(assay.config)
    with pytest.raises(RuntimeError, match="ASSAY_SECRET_KEY"):
        assay.config.enforce_posture_or_raise()


# ── 3. enforced mode rejects a cookie forged with the dev secret ───────────

def test_enforced_rejects_forged_default_cookie(monkeypatch):
    """A cookie signed with the public dev secret is rejected in enforced mode."""
    _, rep_id = _make_run_and_report()
    client = _fresh_client(monkeypatch, auth="enforced", secret=_REAL_SECRET)
    forged = URLSafeSerializer(_DEV_SECRET).dumps("admin")
    resp = client.post(f"/reports/{rep_id}/approve",
                       cookies={"assay_user": forged})
    assert resp.status_code == 401


# ── 4. enforced mode refuses when no reviewer accounts are seeded ──────────

def test_enforced_refuses_when_unseeded(monkeypatch):
    """enforced + zero reviewer/admin users -> 403 on approve and assign."""
    run_id, rep_id = _make_run_and_report()
    client = _fresh_client(monkeypatch, auth="enforced", secret=_REAL_SECRET)
    valid_cookie = URLSafeSerializer(_REAL_SECRET).dumps("alice")
    # approve
    resp = client.post(f"/reports/{rep_id}/approve",
                       cookies={"assay_user": valid_cookie})
    assert resp.status_code == 403
    # assign
    resp = client.post(f"/reports/{rep_id}/assign",
                       json={"reviewer": "alice"},
                       cookies={"assay_user": valid_cookie})
    assert resp.status_code == 403
    # adjudicate
    from assay.store import session_scope
    from assay.store.models import CaseResult
    with session_scope() as s:
        cr = s.query(CaseResult).filter_by(run_id=run_id).first()
        cr_id = cr.id
    resp = client.post(f"/reports/{rep_id}/cases/{cr_id}/adjudicate",
                       json={"verdict": "pass"},
                       cookies={"assay_user": valid_cookie})
    assert resp.status_code == 403


# ── 5. enforced mode allows a valid reviewer with a correctly-signed cookie ─

def test_enforced_allows_valid_reviewer(monkeypatch):
    """enforced + real secret + seeded reviewer + valid cookie -> approve OK."""
    from assay.store import session_scope
    from assay.store.models import User
    # Create run before seeding users so solo-dev activation path still works.
    _, rep_id = _make_run_and_report()
    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))
    client = _fresh_client(monkeypatch, auth="enforced", secret=_REAL_SECRET)
    valid_cookie = URLSafeSerializer(_REAL_SECRET).dumps("alice")
    resp = client.post(f"/reports/{rep_id}/approve",
                       cookies={"assay_user": valid_cookie})
    assert resp.status_code == 303


# ── 6. serve warns on non-loopback in open mode ────────────────────────────

def test_serve_warns_on_nonloopback_open(monkeypatch, capsys):
    """_nonloopback_warning prints to stderr when host is not loopback in open mode."""
    monkeypatch.delenv("ASSAY_AUTH", raising=False)
    import assay.config
    importlib.reload(assay.config)
    import assay.cli
    importlib.reload(assay.cli)
    assay.cli._nonloopback_warning("0.0.0.0")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
