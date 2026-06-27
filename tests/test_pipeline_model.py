"""Phase 1: Pipeline and PipelineVersion DB model."""
import os
import pytest
from assay.pipeline import (
    create_pipeline,
    create_version,
    import_from_yaml,
    get_version,
    list_versions,
    content_hash,
)
from assay.store import init_db, session_scope
from assay.store.models import Pipeline, PipelineVersion, Run

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "compliance-copilot")
SPEC_PATH = os.path.join(EXAMPLE, "assay.yaml")


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


def test_create_and_fetch_pipeline_version():
    p = create_pipeline(project="test-proj", name="my-pipeline", created_by="tester")
    assert p.id is not None

    config = {"version": 1, "project": "my-pipeline"}
    sources = {"generated/checks/foo.py": "def check(r, c): return {'passed': True}"}
    rubrics = {"generated/rubrics/bar.yaml": "judge: primary\ndimensions: []"}

    pv = create_version(p.id, config, sources, rubrics, created_by="tester")

    assert pv.id is not None
    assert pv.status == "draft"
    assert pv.version_number == 1
    assert len(pv.content_hash) == 64
    assert pv.content_hash == content_hash(config, sources, rubrics)

    fetched = get_version(pv.id)
    assert fetched.id == pv.id
    assert fetched.content_hash == pv.content_hash


def test_version_number_increments_per_pipeline():
    p = create_pipeline(project="proj", name="pipe")
    v1 = create_version(p.id, {"v": 1}, {}, {})
    v2 = create_version(p.id, {"v": 2}, {}, {})
    assert v1.version_number == 1
    assert v2.version_number == 2


def test_import_from_yaml():
    cwd = os.getcwd()
    os.chdir(EXAMPLE)
    try:
        pv = import_from_yaml(SPEC_PATH, project="test-proj", created_by="tester")
    finally:
        os.chdir(cwd)

    assert pv.id is not None
    assert pv.status == "draft"
    assert len(pv.content_hash) == 64

    # generated source was inlined
    assert "generated/checks/severity_monotonic.py" in pv.generated_sources
    src = pv.generated_sources["generated/checks/severity_monotonic.py"]
    assert "def check" in src

    # this spec has no judge checks, so rubrics dict is empty
    assert pv.rubrics == {}

    # hash matches a recompute from the stored data
    recomputed = content_hash(pv.config, pv.generated_sources, pv.rubrics)
    assert pv.content_hash == recomputed


def test_import_creates_pipeline_record():
    cwd = os.getcwd()
    os.chdir(EXAMPLE)
    try:
        pv = import_from_yaml(SPEC_PATH, project="proj2")
    finally:
        os.chdir(cwd)

    with session_scope() as s:
        pipeline = s.get(Pipeline, pv.pipeline_id)
        assert pipeline is not None
        assert pipeline.project == "proj2"
        assert pipeline.name == "compliance-copilot"


def test_import_reuses_existing_pipeline():
    """Importing the same spec twice reuses the Pipeline row."""
    cwd = os.getcwd()
    os.chdir(EXAMPLE)
    try:
        pv1 = import_from_yaml(SPEC_PATH, project="proj3")
        pv2 = import_from_yaml(SPEC_PATH, project="proj3")
    finally:
        os.chdir(cwd)

    assert pv1.pipeline_id == pv2.pipeline_id
    assert pv2.version_number == 2

    with session_scope() as s:
        count = s.query(Pipeline).filter_by(project="proj3").count()
        assert count == 1


def test_file_loading_still_works():
    """load_spec + execute_run(spec=...) works; pipeline_version_id stays None."""
    from assay.spec.loader import load_spec
    from assay.engine import execute_run
    from assay.store.models import Run

    cwd = os.getcwd()
    os.chdir(EXAMPLE)
    try:
        spec = load_spec("assay.yaml")
        run_id = execute_run(spec, triggered_by="tester")
    finally:
        os.chdir(cwd)

    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.pipeline_version_id is None
