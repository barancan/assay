"""Phase 3: generator → DB draft + activation gate."""
import os
import pytest
from assay.pipeline import create_pipeline, create_version, activate_version, get_version
from assay.store import init_db, session_scope
from assay.store.models import User, PipelineVersion

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "compliance-copilot")
REQUIREMENTS = os.path.join(EXAMPLE, "requirements.md")


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


def _make_draft() -> PipelineVersion:
    """Create a minimal draft PipelineVersion for testing the gate."""
    p = create_pipeline(project="test", name="test-pipe")
    config = {
        "version": 1, "project": "test-pipe",
        "target": {"adapter": "mock"},
        "judges": {"primary": {"provider": "mock", "model": "mock"}},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [{"type": "template", "uses": "valid_json"}]}
        ]}],
        "gating": {},
    }
    return create_version(p.id, config, {}, {})


# ── generator → DB ───────────────────────────────────────────────────────────

def test_generate_creates_draft():
    from assay.generator.build import build_pipeline_to_db
    pv_id = build_pipeline_to_db(
        REQUIREMENTS,
        {"adapter": "mock"},
        project="gen-test",
        created_by="tester",
    )
    pv = get_version(pv_id)
    assert pv.status == "draft"
    assert pv.config["project"] == "gen-test"
    assert pv.content_hash  # non-empty


def test_generate_to_db_includes_rubric_for_judge_intent():
    """When requirements mention refusal/uncertainty, a judge intent and rubric appear."""
    from assay.generator.build import build_pipeline_to_db
    # The heuristic adds a judge intent when requirements mention 'refus' or 'uncertain'.
    req_text = "R1. The model must flag uncertainty rather than over-asserting."
    req_path = os.path.join(os.path.dirname(__file__), "tmp_req.md")
    with open(req_path, "w") as f:
        f.write(req_text)
    try:
        pv_id = build_pipeline_to_db(req_path, {"adapter": "mock"}, project="judge-test")
    finally:
        os.unlink(req_path)
    pv = get_version(pv_id)
    # At least one rubric should be present.
    assert len(pv.rubrics) >= 1
    # All rubrics should be valid YAML strings.
    import yaml
    for content in pv.rubrics.values():
        parsed = yaml.safe_load(content)
        assert "dimensions" in parsed


# ── activation gate ──────────────────────────────────────────────────────────

def test_run_blocked_on_draft():
    from assay.engine import execute_run
    pv = _make_draft()
    with pytest.raises(PermissionError, match="not active"):
        execute_run(pipeline_version_id=pv.id, triggered_by="tester")


def test_activation_records_actor_and_timestamp():
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    pv = _make_draft()
    activate_version(pv.id, "rev1")
    activated = get_version(pv.id)
    assert activated.status == "active"
    assert activated.activated_by == "rev1"
    assert activated.activated_at is not None


def test_runner_role_cannot_activate():
    with session_scope() as s:
        s.add(User(name="runner1", role="runner"))
    pv = _make_draft()
    with pytest.raises(PermissionError):
        activate_version(pv.id, "runner1")


def test_unknown_actor_blocked_when_users_exist():
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    pv = _make_draft()
    with pytest.raises(PermissionError):
        activate_version(pv.id, "ghost")


def test_solo_dev_path_any_actor_allowed_when_no_users():
    """If the User table is empty, any actor can activate (solo-dev path)."""
    pv = _make_draft()
    activate_version(pv.id, "anyone")   # must not raise
    assert get_version(pv.id).status == "active"


def test_previously_active_version_archived():
    with session_scope() as s:
        s.add(User(name="rev1", role="reviewer"))
    p = create_pipeline(project="proj", name="pipe")
    config = {
        "version": 1, "project": "pipe",
        "target": {"adapter": "mock"},
        "judges": {}, "suites": [], "gating": {},
    }
    v1 = create_version(p.id, config, {}, {})
    activate_version(v1.id, "rev1")
    assert get_version(v1.id).status == "active"

    v2 = create_version(p.id, config, {}, {})
    activate_version(v2.id, "rev1")
    assert get_version(v1.id).status == "archived"
    assert get_version(v2.id).status == "active"


# ── active pipeline runs successfully ────────────────────────────────────────

def test_active_pipeline_version_runs():
    """Activate a simple (template-only) DB pipeline and run it end to end."""
    from assay.engine import execute_run
    from assay.store.models import Report, Run
    pv = _make_draft()
    activate_version(pv.id, "solo-dev")  # no users in DB → allowed

    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")

    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run.pipeline_version_id == pv.id
        rep = s.query(Report).filter_by(run_id=run_id).one()
        assert rep.state == "pending"
        assert rep.summary["cases"] == 1
