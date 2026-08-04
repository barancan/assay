"""Run progress feedback (journey J10.7).

A run used to block the HTTP request until every case finished. That is invisible
against mock adapters and unacceptable against real models, where a run takes minutes.
Browsers now get an immediate redirect to a progress view; programmatic callers keep
synchronous semantics, because CI depends on the response carrying report_id.
"""
from __future__ import annotations
import time

import pytest
from fastapi.testclient import TestClient


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


def _spec(n_cases: int = 3) -> dict:
    return {
        "version": 1, "project": "prog", "target": {"adapter": "mock"},
        "judges": {}, "gating": {},
        "suites": [{"id": "s1", "requirement_ref": "R1", "cases": [
            {"id": f"c{i}", "input": {"prompt": "hi"},
             "checks": [{"type": "template", "uses": "valid_json"}]}
            for i in range(n_cases)
        ]}],
    }


def _active_version(n_cases: int = 3) -> int:
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.store.db import session_scope
    from assay.store.models import User
    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))
    pipe = create_pipeline(project="prog", name="prog")
    version = create_version(pipe.id, _spec(n_cases), {}, {})
    activate_version(version.id, "alice")
    return version.id


def _client():
    from assay.server.app import app
    return TestClient(app)


def _wait_for(predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── the run row knows its size up front ─────────────────────────────────────

def test_run_records_total_cases():
    from assay.engine import execute_run, run_progress
    run_id = execute_run(pipeline_version_id=_active_version(4))
    progress = run_progress(run_id)
    assert progress["total"] == 4
    assert progress["done"] == 4
    assert progress["status"] == "complete"


def test_progress_of_unknown_run_raises():
    from assay.engine import run_progress
    with pytest.raises(ValueError):
        run_progress(9999)


# ── synchronous path is unchanged ───────────────────────────────────────────

def test_json_api_still_runs_synchronously():
    """CI and the webhook depend on report_id being present in the response."""
    version_id = _active_version()
    resp = _client().post(f"/pipelines/versions/{version_id}/run",
                          headers={"X-Assay-User": "alice"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["report_id"] is not None

    from assay.engine import run_progress
    assert run_progress(body["run_id"])["status"] == "complete"


# ── async path ──────────────────────────────────────────────────────────────

def test_browser_run_redirects_to_progress_view():
    version_id = _active_version()
    resp = _client().post(f"/pipelines/versions/{version_id}/run",
                          headers={"X-Assay-User": "alice", "hx-request": "true"})
    assert resp.status_code == 200
    location = resp.headers["HX-Redirect"]
    assert location.startswith("/runs/")
    run_id = int(location.rsplit("/", 1)[1])

    from assay.engine import run_progress
    assert _wait_for(lambda: run_progress(run_id)["status"] == "complete")


def test_background_run_submits_and_exports():
    """The async path must reach ready_for_review, like the synchronous one does."""
    from assay.engine import start_run, run_progress
    from assay.store.db import session_scope
    from assay.store.models import Report

    run_id = start_run(pipeline_version_id=_active_version(), triggered_by="alice")
    assert _wait_for(lambda: run_progress(run_id)["status"] == "complete")
    assert _wait_for(lambda: run_progress(run_id)["report_id"] is not None)

    report_id = run_progress(run_id)["report_id"]
    with session_scope() as s:
        assert s.get(Report, report_id).state == "ready_for_review"


def test_setup_failure_surfaces_synchronously():
    """An inactive version must raise on the calling thread, not vanish into one."""
    from assay.pipeline import create_pipeline, create_version
    from assay.engine import start_run

    pipe = create_pipeline(project="prog", name="prog")
    draft = create_version(pipe.id, _spec(), {}, {})
    with pytest.raises(PermissionError):
        start_run(pipeline_version_id=draft.id)


def test_failed_run_records_the_error():
    from assay.engine import start_run, run_progress
    from assay.adapters.mock import MockAdapter

    def _boom(self, req):
        raise RuntimeError("target exploded")

    original = MockAdapter.invoke
    MockAdapter.invoke = _boom
    try:
        run_id = start_run(pipeline_version_id=_active_version(), triggered_by="alice")
        assert _wait_for(lambda: run_progress(run_id)["status"] == "error")
        assert "target exploded" in run_progress(run_id)["error"]
    finally:
        MockAdapter.invoke = original


# ── progress views ──────────────────────────────────────────────────────────

def test_progress_page_renders():
    from assay.engine import execute_run
    run_id = execute_run(pipeline_version_id=_active_version())
    resp = _client().get(f"/runs/{run_id}")
    assert resp.status_code == 200
    assert "Progress" in resp.text


def test_progress_page_404s_for_unknown_run():
    assert _client().get("/runs/9999").status_code == 404


def test_progress_fragment_redirects_when_complete():
    from assay.engine import start_run, run_progress
    run_id = start_run(pipeline_version_id=_active_version(), triggered_by="alice")
    assert _wait_for(lambda: run_progress(run_id)["report_id"] is not None)

    resp = _client().get(f"/runs/{run_id}/progress")
    assert resp.headers["HX-Redirect"] == f"/reports/{run_progress(run_id)['report_id']}"


def test_progress_fragment_polls_while_running():
    """While a run is in flight the fragment re-arms its own poll."""
    from assay.store.db import session_scope
    from assay.store.models import Run, TargetModel

    with session_scope() as s:
        tm = TargetModel(project="prog", adapter="mock")
        s.add(tm)
        s.flush()
        run = Run(project="prog", spec_hash="x", target_id=tm.id,
                  status="running", cases_total=4)
        s.add(run)
        s.flush()
        run_id = run.id

    resp = _client().get(f"/runs/{run_id}/progress")
    assert resp.status_code == 200
    assert 'hx-trigger="every 2s"' in resp.text
    assert "Case 0 of 4" in resp.text
    assert 'role="progressbar"' in resp.text


# ── per-case commits are what make progress observable ──────────────────────

def test_case_results_are_committed_as_they_complete():
    from assay.engine.runner import _setup_run, _execute_cases
    from assay.store.db import session_scope
    from assay.store.models import CaseResult

    ctx = _setup_run(None, _active_version(3), "manual", "alice")
    seen: list[int] = []

    from assay.adapters.mock import MockAdapter
    original = MockAdapter.invoke

    def _counting(self, req):
        with session_scope() as s:
            seen.append(s.query(CaseResult).filter_by(run_id=ctx.run_id).count())
        return original(self, req)

    MockAdapter.invoke = _counting
    try:
        _execute_cases(ctx)
    finally:
        MockAdapter.invoke = original

    # Each invocation sees the results of every case before it -- proof the rows land
    # incrementally rather than all at once on a single final commit.
    assert seen == [0, 1, 2]
