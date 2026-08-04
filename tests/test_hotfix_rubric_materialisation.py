"""Regression tests for run-time materialisation of DB-stored pipeline artifacts.

A pipeline stored in the DB keeps its generated check sources *and* its judge rubrics as
text on the PipelineVersion. At run time both must be written to disk before the spec's
paths are dereferenced -- rubrics were previously omitted, so any DB pipeline containing
a judge check died with FileNotFoundError inside run_judge_check.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

import pytest
import yaml

RUBRIC = yaml.safe_dump({
    "judge": "primary",
    "dimensions": [
        {"id": "clarity", "question": "Is the answer clear?",
         "scale": {0: "no", 1: "partly", 2: "yes"}, "min_score": 1},
    ],
})

GENERATED_CHECK = (
    "def check(response, context):\n"
    "    return {'passed': bool(response.get('text')), 'message': 'has text'}\n"
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


def _spec(checks: list[dict]) -> dict:
    return {
        "version": 1,
        "project": "rubric-fix",
        "target": {"adapter": "mock", "model": "mock"},
        "judges": {"primary": {"provider": "mock", "model": "mock"}},
        "suites": [{
            "id": "s1",
            "requirement_ref": "R1",
            "cases": [{"id": "c1", "input": {"prompt": "hello"}, "checks": checks}],
        }],
        "gating": {},
    }


def _activate(config: dict, sources: dict, rubrics: dict) -> int:
    from assay.pipeline import create_pipeline, create_version, activate_version
    from assay.store.db import session_scope
    from assay.store.models import User

    with session_scope() as s:
        s.add(User(name="alice", role="reviewer"))
    pipe = create_pipeline(project="rubric-fix", name="rubric-fix")
    version = create_version(pipe.id, config, sources, rubrics)
    activate_version(version.id, "alice")
    return version.id


def _case_checks(run_id: int) -> list[dict]:
    from assay.store.db import session_scope
    from assay.store.models import CaseResult
    with session_scope() as s:
        results = s.query(CaseResult).filter_by(run_id=run_id).all()
        assert len(results) == 1
        return results[0].checks


def test_db_pipeline_with_judge_check_runs():
    """A judge check on a DB pipeline must execute, not raise FileNotFoundError."""
    from assay.engine.runner import execute_run

    rubric_path = "generated/rubrics/clarity.yaml"
    version_id = _activate(
        _spec([{"type": "judge", "judge": "primary", "rubric": rubric_path}]),
        {},
        {rubric_path: RUBRIC},
    )

    run_id = execute_run(pipeline_version_id=version_id)

    checks = _case_checks(run_id)
    assert len(checks) == 1
    assert checks[0]["type"] == "judge"
    # The rubric was found and scored. The failure mode under test is an unhandled
    # FileNotFoundError that aborts the whole run, not any particular verdict.
    assert "evidence" in checks[0]


def test_db_pipeline_with_generated_and_judge_checks():
    """Both artifact kinds materialise in the same run without colliding."""
    from assay.engine.runner import execute_run

    check_path = "generated/checks/has_text.py"
    rubric_path = "generated/rubrics/clarity.yaml"
    version_id = _activate(
        _spec([
            {"type": "generated", "uses": check_path},
            {"type": "judge", "judge": "primary", "rubric": rubric_path},
        ]),
        {check_path: GENERATED_CHECK},
        {rubric_path: RUBRIC},
    )

    run_id = execute_run(pipeline_version_id=version_id)

    checks = _case_checks(run_id)
    assert {c["type"] for c in checks} == {"generated", "judge"}
    generated = next(c for c in checks if c["type"] == "generated")
    assert generated["passed"] is True


def test_materialised_artifacts_are_cleaned_up():
    """Temp dirs are removed even when there are no generated sources, only rubrics."""
    from assay.engine.runner import execute_run

    rubric_path = "generated/rubrics/clarity.yaml"
    version_id = _activate(
        _spec([{"type": "judge", "judge": "primary", "rubric": rubric_path}]),
        {},
        {rubric_path: RUBRIC},
    )

    before = set(Path(tempfile.gettempdir()).glob("assay-run-*"))
    execute_run(pipeline_version_id=version_id)
    after = set(Path(tempfile.gettempdir()).glob("assay-run-*"))
    assert after == before
