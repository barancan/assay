"""Identity must be proven, not asserted.

`X-Assay-User` is a header anyone who can reach the port can send. In enforced mode
that made the approval gate decorative: name a seeded reviewer and approve a report.
Assay's central claim is that automation produces evidence and only a human produces a
decision, so this is the claim's load-bearing test.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer

SECRET = "test-secret-not-the-dev-default"
TOKEN = "test-api-token"


@pytest.fixture
def enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("ASSAY_AUTH", "enforced")
    monkeypatch.setenv("ASSAY_SECRET_KEY", SECRET)
    monkeypatch.delenv("ASSAY_API_TOKEN", raising=False)
    import assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db, session_scope
    from assay.store.models import User
    init_db()
    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))
    from assay.server.app import app
    return TestClient(app)


@pytest.fixture
def open_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 'o.db'}")
    monkeypatch.setenv("ASSAY_AUTH", "open")
    import assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db
    init_db()
    from assay.server.app import app
    return TestClient(app)


def _cookie() -> dict:
    return {"assay_user": URLSafeSerializer(SECRET).dumps("alice")}


# ── the hole ────────────────────────────────────────────────────────────────

def test_bare_header_is_refused_in_enforced_mode(enforced):
    """The whole point: asserting a reviewer's name must not grant their authority."""
    resp = enforced.post("/settings/judge",
                         json={"judge_adapter": "anthropic", "judge_model": "m"},
                         headers={"X-Assay-User": "alice"})
    assert resp.status_code == 401
    assert "ASSAY_API_TOKEN" in resp.json()["detail"]


def test_header_with_token_is_accepted(enforced, monkeypatch):
    monkeypatch.setenv("ASSAY_API_TOKEN", TOKEN)
    import assay.config
    importlib.reload(assay.config)
    resp = enforced.post("/settings/judge",
                         json={"judge_adapter": "anthropic", "judge_model": "m"},
                         headers={"X-Assay-User": "alice", "X-Assay-Token": TOKEN})
    assert resp.status_code == 200


def test_wrong_token_is_refused(enforced, monkeypatch):
    monkeypatch.setenv("ASSAY_API_TOKEN", TOKEN)
    import assay.config
    importlib.reload(assay.config)
    resp = enforced.post("/settings/judge",
                         json={"judge_adapter": "anthropic", "judge_model": "m"},
                         headers={"X-Assay-User": "alice", "X-Assay-Token": "wrong"})
    assert resp.status_code == 401


def test_signed_cookie_still_works_without_a_token(enforced):
    """A browser session is proof on its own; the token is only for header callers."""
    resp = enforced.post("/settings/judge",
                         json={"judge_adapter": "anthropic", "judge_model": "m"},
                         cookies=_cookie())
    assert resp.status_code == 200


def test_open_mode_is_unchanged(open_mode):
    """A solo developer on localhost keeps the frictionless path."""
    resp = open_mode.post("/settings/judge",
                          json={"judge_adapter": "anthropic", "judge_model": "m"},
                          headers={"X-Assay-User": "whoever"})
    assert resp.status_code == 200


# ── the routes that had no gate at all ──────────────────────────────────────

def test_settings_builder_requires_identity(enforced):
    resp = enforced.post("/settings/builder",
                         json={"builder_adapter": "anthropic", "builder_model": "m"})
    assert resp.status_code == 401


def test_webhook_requires_identity(enforced):
    resp = enforced.post("/hooks/run", json={"spec": "assay.yaml", "by": "ci"})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd",
                                  "../../../root/.ssh/id_rsa"])
def test_webhook_cannot_read_outside_the_working_directory(open_mode, path):
    """body.spec went straight to load_spec -- an unauthenticated arbitrary file read."""
    resp = open_mode.post("/hooks/run", json={"spec": path, "by": "ci"},
                          headers={"X-Assay-User": "alice"})
    assert resp.status_code in (400, 404)
    assert "passwd" not in resp.text or resp.status_code == 400


def test_webhook_reports_a_missing_spec_cleanly(open_mode):
    resp = open_mode.post("/hooks/run", json={"spec": "nope.yaml", "by": "ci"},
                          headers={"X-Assay-User": "alice"})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_webhook_records_the_authenticated_actor_not_the_claim(open_mode, tmp_path,
                                                               monkeypatch):
    """`by` was written into the audit trail unverified."""
    import yaml
    monkeypatch.chdir(tmp_path)
    spec = {
        "version": 1, "project": "hook", "target": {"adapter": "mock"},
        "judges": {}, "gating": {},
        "suites": [{"id": "R1", "requirement_ref": "R1", "cases": [
            {"id": "c1", "input": {"prompt": "hi"},
             "checks": [{"type": "template", "uses": "valid_json"}]}]}],
    }
    (tmp_path / "assay.yaml").write_text(yaml.safe_dump(spec))

    resp = open_mode.post("/hooks/run", json={"spec": "assay.yaml", "by": "i-am-alice"},
                          headers={"X-Assay-User": "realcaller"})
    assert resp.status_code == 200

    from assay.store.db import session_scope
    from assay.store.models import Run
    with session_scope() as s:
        run = s.get(Run, resp.json()["run_id"])
        assert run.triggered_by == "realcaller"
        assert run.triggered_by != "i-am-alice"


# ── the invariant this protects ─────────────────────────────────────────────

def test_automation_cannot_approve_by_naming_a_reviewer(enforced):
    """Assay's central claim, stated as a test."""
    resp = enforced.post("/reports/1/approve", headers={"X-Assay-User": "alice"})
    # 401 before authority is even considered -- the claim is never reached.
    assert resp.status_code == 401
