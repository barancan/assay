"""Phase 6: notification dispatch on state transitions."""
from __future__ import annotations
import pytest
from assay.notifications.base import NoOpNotifier
from assay.notifications.factory import get_notifier
from assay.pipeline import create_pipeline, create_version, activate_version
from assay.engine import execute_run, submit_for_review, approve_report
from assay.store import session_scope
from assay.store.models import Report


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    # Remove any Linear env vars so get_notifier() returns NoOpNotifier.
    monkeypatch.delenv("ASSAY_LINEAR_API_KEY", raising=False)
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


def _make_pending_report() -> tuple[int, int]:
    """Create an active pipeline, run it, return (run_id, report_id)."""
    p = create_pipeline(project="notify-test", name="notify-test")
    cfg = {
        "version": 1, "project": "notify-test",
        "target": {"adapter": "mock"},
        "judges": {},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": "c1", "input": {}, "checks": [
                {"type": "template", "uses": "valid_json"}
            ]}
        ]}],
        "gating": {},
    }
    pv = create_version(p.id, cfg, {}, {})
    activate_version(pv.id, "solo-dev")
    run_id = execute_run(pipeline_version_id=pv.id, triggered_by="tester")
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        return run_id, rep.id


class _RecordingNotifier:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def notify(self, event: str, payload: dict) -> None:
        self.calls.append((event, payload))


# ── basic protocol ────────────────────────────────────────────────────────────

def test_noop_notifier_fires_without_error():
    NoOpNotifier().notify("ready_for_review", {})


def test_unconfigured_is_noop():
    assert isinstance(get_notifier(), NoOpNotifier)


# ── submit_for_review fires ready_for_review ─────────────────────────────────

def test_mock_notifier_receives_ready_for_review(monkeypatch):
    import assay.engine.review as _review
    notifier = _RecordingNotifier()
    monkeypatch.setattr(_review, "get_notifier", lambda: notifier)

    _, rep_id = _make_pending_report()
    submit_for_review(rep_id, actor="tester")

    assert len(notifier.calls) == 1
    event, payload = notifier.calls[0]
    assert event == "ready_for_review"
    assert payload["report_id"] == rep_id
    assert "project" in payload
    assert "summary" in payload


# ── approve_report fires approved ─────────────────────────────────────────────

def test_mock_notifier_receives_approved(monkeypatch):
    import assay.engine.review as _review
    notifier = _RecordingNotifier()
    monkeypatch.setattr(_review, "get_notifier", lambda: notifier)

    _, rep_id = _make_pending_report()
    submit_for_review(rep_id, actor="tester")
    approve_report(rep_id, "solo-dev")

    events = [e for e, _ in notifier.calls]
    assert "ready_for_review" in events
    assert "approved" in events

    approved_payload = next(p for e, p in notifier.calls if e == "approved")
    assert approved_payload["approved_by"] == "solo-dev"
    assert "summary" in approved_payload


# ── notifier errors are swallowed ────────────────────────────────────────────

def test_notifier_error_does_not_abort_transition(monkeypatch):
    import assay.engine.review as _review

    class _BrokenNotifier:
        def notify(self, event, payload):
            raise RuntimeError("notification service down")

    monkeypatch.setattr(_review, "get_notifier", lambda: _BrokenNotifier())

    _, rep_id = _make_pending_report()
    submit_for_review(rep_id, actor="tester")   # must not raise

    with session_scope() as s:
        rep = s.get(Report, rep_id)
        assert rep.state == "ready_for_review"
