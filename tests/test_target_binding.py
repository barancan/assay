"""Phase 2: ping() on every adapter + connection test gate in execute_run."""
import os
import pytest
from assay.adapters.mock import MockAdapter
from assay.adapters.rest import RestAdapter
from assay.adapters.ollama import OllamaAdapter
from assay.adapters.openai_compat import OpenAICompatAdapter
from assay.spec.models import Spec, TargetSpec
from assay.store import session_scope
from assay.store.models import Run

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "compliance-copilot")


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


# ── ping() returns the right shape ──────────────────────────────────────────

def test_mock_ping_ok():
    result = MockAdapter().ping()
    assert result["ok"] is True
    assert result["error"] is None
    assert result["latency_ms"] == 0.0


def test_rest_ping_no_url_is_ok():
    # RestAdapter with no endpoint can't fail — nothing to connect to.
    result = RestAdapter().ping()
    assert result["ok"] is True


def test_rest_ping_unreachable_returns_not_ok():
    adapter = RestAdapter(endpoint="http://localhost:19999")
    result = adapter.ping()
    assert result["ok"] is False
    assert result["error"] is not None
    assert "19999" in result["error"]


def test_ollama_ping_unreachable_returns_not_ok():
    adapter = OllamaAdapter(endpoint="http://localhost:29999")
    result = adapter.ping()
    assert result["ok"] is False
    assert "29999" in result["error"]


def test_openai_compat_ping_unreachable_returns_not_ok():
    adapter = OpenAICompatAdapter(endpoint="http://localhost:39999/v1")
    result = adapter.ping()
    assert result["ok"] is False
    assert "39999" in result["error"]


# ── test_connection raises on failure ────────────────────────────────────────

def test_test_connection_passes_for_mock():
    from assay.adapters.registry import test_connection as _tc
    _tc(MockAdapter())  # must not raise


def test_test_connection_raises_for_unreachable():
    from assay.adapters.registry import test_connection as _tc
    adapter = RestAdapter(endpoint="http://localhost:19999")
    with pytest.raises(ConnectionError) as exc_info:
        _tc(adapter)
    assert "19999" in str(exc_info.value)


# ── execute_run aborts before DB write on bad connection ─────────────────────

def test_failing_adapter_aborts_run_before_db_write():
    from assay.engine import execute_run
    spec = Spec(project="test", target=TargetSpec(adapter="rest", endpoint="http://localhost:19999"))
    with pytest.raises(ConnectionError) as exc_info:
        execute_run(spec, triggered_by="tester")
    # Endpoint URL appears in the error.
    assert "19999" in str(exc_info.value)
    # No Run row was created.
    with session_scope() as s:
        assert s.query(Run).count() == 0


def test_successful_run_still_works():
    """Smoke: mock adapter passes ping and run completes normally."""
    from assay.spec.loader import load_spec
    from assay.engine import execute_run
    from assay.store.models import Report
    cwd = os.getcwd()
    os.chdir(EXAMPLE)
    try:
        run_id = execute_run(load_spec("assay.yaml"), triggered_by="tester")
    finally:
        os.chdir(cwd)
    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        assert rep.state == "pending"
