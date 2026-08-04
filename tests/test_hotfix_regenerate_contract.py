"""A regenerated check must satisfy the sandbox contract.

regenerate_check previously emitted `def <stem>(response, **kwargs): return True`, but
the sandbox requires a module-level `check(response, context) -> dict`. Every regenerated
check therefore died at run time with "module defines no check(response, context)".
Codegen is still unimplemented, so the scaffold fails loudly -- but it must fail as a
check verdict, not as a contract violation.
"""
from __future__ import annotations

import pytest

GENERATED_CHECK = (
    "def check(response, context):\n"
    "    return {'passed': True, 'message': 'original'}\n"
)


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


CHECK_PATH = "generated/checks/severity_monotonic.py"


def _draft_with_generated_check():
    from assay.pipeline import create_pipeline, create_version
    config = {
        "version": 1, "project": "regen", "target": {"adapter": "mock"},
        "judges": {}, "gating": {},
        "suites": [{"id": "s1", "requirement_ref": "R1", "cases": [
            {"id": "severity-monotonic", "input": {},
             "checks": [{"type": "generated", "uses": CHECK_PATH}]},
        ]}],
    }
    pipe = create_pipeline(project="regen", name="regen")
    return create_version(pipe.id, config, {CHECK_PATH: GENERATED_CHECK}, {})


def _regenerated_source() -> str:
    from assay.pipeline.service import regenerate_check, get_version
    draft = _draft_with_generated_check()
    new_id = regenerate_check(draft.id, CHECK_PATH, "alice")
    return get_version(new_id).generated_sources[CHECK_PATH]


def test_regenerated_source_defines_the_contract_function():
    source = _regenerated_source()
    ns: dict = {}
    exec(compile(source, CHECK_PATH, "exec"), ns)
    assert callable(ns.get("check")), "must define a module-level check()"

    import inspect
    params = list(inspect.signature(ns["check"]).parameters)
    assert params == ["response", "context"]


def test_regenerated_check_runs_in_the_sandbox(tmp_path):
    """The scaffold must produce a check verdict, not a contract violation."""
    from assay.sandbox import run_generated_check

    path = tmp_path / "genchk.py"
    path.write_text(_regenerated_source())

    out = run_generated_check(str(path), {"text": "hi"}, {"input": {}})
    assert out["passed"] is False
    assert "unimplemented scaffold" in out["message"]
    assert "no check(response, context)" not in out["message"]


def test_regenerate_creates_a_new_draft_version():
    from assay.pipeline.service import regenerate_check, get_version
    draft = _draft_with_generated_check()
    new_id = regenerate_check(draft.id, CHECK_PATH, "alice")
    assert new_id != draft.id
    assert get_version(new_id).status == "draft"
    # The original source is untouched on the old version.
    assert get_version(draft.id).generated_sources[CHECK_PATH] == GENERATED_CHECK
