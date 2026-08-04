"""P6: the mock adapter is a test fixture, not a product default.

The single most misleading thing Assay could do is hand a fresh install with no API keys
a fully green report. That is exactly what it did: `mock` was the wizard's first adapter,
the CLI's default `--adapter`, and the judge the builder quietly substituted when none
was configured. Mocks pass everything, so the report was green and meant nothing.

These tests pin the wall. The suite as a whole runs with ASSAY_ALLOW_MOCK=1 (see the root
conftest.py) because it is built on mocks; every test here first takes that away, so it
sees what a real user with no keys sees.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assay.adapters.registry import ALLOW_MOCK_ENV, get_judge_provider, get_target_adapter
from assay.llm.provider import LLMConfigError
from assay.spec.models import JudgeSpec, TargetSpec

_TEMPLATES = Path(__file__).parent.parent / "assay" / "server" / "templates"
ADAPTER_FIELDS = _TEMPLATES / "_adapter_fields.html"
WIZARD = _TEMPLATES / "pipeline_new.html"

REQUIREMENTS = (
    "R1. Every response must be valid JSON.\n"
    "R2. Responses must not contain hallucinated facts.\n"
)

JUDGE_INTENT = {"id": "J1", "requirement_ref": "R2", "category": "quality",
                "assertion": "response contains no hallucinated facts", "how": "judge",
                "threshold": 0.85}
TEMPLATE_INTENT = {"id": "T1", "requirement_ref": "R1", "category": "format",
                   "assertion": "response is valid JSON", "how": "template",
                   "template": "valid_json", "params": {}}

MOCK_SPEC = {
    "version": 1, "project": "unconfigured",
    "target": {"adapter": "mock"},
    "judges": {"primary": {"provider": "mock", "model": "mock"}},
    "suites": [{"id": "s1", "requirement_ref": "R1", "cases": [
        {"id": "c1", "input": {"prompt": "anything"},
         "checks": [{"type": "template", "uses": "valid_json"}]},
    ]}],
    "gating": {},
}


@pytest.fixture
def fresh_install(monkeypatch):
    """What a new user has: no provider keys, and no opt-in to mocks."""
    for var in (ALLOW_MOCK_ENV, "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 'p6.db'}")
    import importlib

    import assay.config
    import assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db
    init_db()
    yield


# ── the headline: no keys, no opt-in, no green report ───────────────────────


def test_a_fresh_install_cannot_produce_a_green_report(fresh_install, tmp_db):
    """The whole point of P6. With nothing configured, the run must not happen."""
    from assay.engine import execute_run
    from assay.spec.models import Spec
    from assay.store import session_scope
    from assay.store.models import Report, Run

    with pytest.raises(LLMConfigError):
        execute_run(Spec.model_validate(MOCK_SPEC), triggered_by="tester")

    # Not merely "no green report" -- no report at all, and no run pretending to be one.
    with session_scope() as s:
        assert s.query(Run).count() == 0
        assert s.query(Report).count() == 0


def test_the_offline_example_is_the_only_thing_that_unlocks_it(fresh_install, tmp_db,
                                                              monkeypatch):
    """Same spec, same absence of keys -- the opt-in is what makes it run."""
    from assay.engine import execute_run
    from assay.spec.models import Spec
    monkeypatch.setenv(ALLOW_MOCK_ENV, "1")
    run_id = execute_run(Spec.model_validate(MOCK_SPEC), triggered_by="tester")
    assert run_id


# ── resolving a mock target/judge ────────────────────────────────────────────


def test_resolving_a_mock_target_raises(fresh_install):
    with pytest.raises(LLMConfigError):
        get_target_adapter(TargetSpec(adapter="mock"))


def test_resolving_a_mock_judge_raises(fresh_install):
    with pytest.raises(LLMConfigError):
        get_judge_provider(JudgeSpec(provider="mock", model="mock"))


def test_the_refusal_says_what_to_do_next(fresh_install):
    """The message is the deliverable: a dead end with no next step is a bug."""
    with pytest.raises(LLMConfigError) as exc:
        get_target_adapter(TargetSpec(adapter="mock"))
    message = str(exc.value)
    # Why it is refused, not just that it is.
    assert "passes every check" in message
    # How to configure a real provider -- every option, with the variable to set.
    for expected in ("anthropic", "ANTHROPIC_API_KEY", "openai_compat", "OPENAI_API_KEY",
                     "ollama", "rest", "endpoint"):
        assert expected in message
    # And the escape hatch, named, for the offline demo and the test suite.
    assert ALLOW_MOCK_ENV in message


def test_a_real_adapter_is_untouched_by_the_gate(fresh_install):
    """The gate is about mocks only; an unconfigured real adapter fails on its key."""
    result = get_target_adapter(TargetSpec(adapter="anthropic", model="m")).ping()
    assert not result["ok"]
    assert result["env_var"] == "ANTHROPIC_API_KEY"


def test_an_unknown_adapter_is_still_a_plain_value_error(fresh_install):
    with pytest.raises(ValueError):
        get_target_adapter(TargetSpec(adapter="telepathy"))


def test_the_opt_in_restores_mocks_for_tests(monkeypatch):
    monkeypatch.setenv(ALLOW_MOCK_ENV, "1")
    assert get_target_adapter(TargetSpec(adapter="mock")).ping()["ok"]
    assert get_judge_provider(JudgeSpec(provider="mock", model="mock"))


def test_an_explicit_caller_opt_in_also_works(fresh_install):
    """`allow_mock=True` is the in-process equivalent for the offline path."""
    assert get_target_adapter(TargetSpec(adapter="mock"), allow_mock=True)
    assert get_judge_provider(JudgeSpec(provider="mock", model="mock"), allow_mock=True)


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_a_falsy_opt_in_is_not_an_opt_in(fresh_install, monkeypatch, value):
    monkeypatch.setenv(ALLOW_MOCK_ENV, value)
    with pytest.raises(LLMConfigError):
        get_target_adapter(TargetSpec(adapter="mock"))


# ── no silent mock judge ─────────────────────────────────────────────────────


def test_a_judge_check_with_no_judge_is_an_error(fresh_install):
    """The builder used to substitute {"primary": {"provider": "mock"}} here."""
    from assay.generator.build import intents_to_spec
    with pytest.raises(LLMConfigError) as exc:
        intents_to_spec("p", [JUDGE_INTENT], {"adapter": "anthropic"}, {})
    message = str(exc.value)
    assert "mock" in message
    assert "ANTHROPIC_API_KEY" in message or "Settings" in message


def test_a_template_only_pipeline_needs_no_judge(fresh_install):
    """Deterministic checks score themselves; demanding a judge would be noise."""
    from assay.generator.build import intents_to_spec
    spec = intents_to_spec("p", [TEMPLATE_INTENT], {"adapter": "anthropic"}, {})
    assert spec["judges"] == {}


def test_a_configured_judge_is_carried_through_untouched(fresh_install):
    from assay.generator.build import intents_to_spec
    judges = {"primary": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}}
    spec = intents_to_spec("p", [JUDGE_INTENT], {"adapter": "anthropic"}, judges)
    assert spec["judges"] == judges
    assert "mock" not in json.dumps(spec)


# ── the wizard does not offer mock ───────────────────────────────────────────


def test_mock_is_not_in_the_wizards_adapter_list():
    from assay.server.app import _ADAPTER_NAMES
    assert "mock" not in _ADAPTER_NAMES
    assert _ADAPTER_NAMES[0] != "mock", "the default choice must be a real provider"


def test_mock_is_not_an_option_in_the_adapter_template():
    html = ADAPTER_FIELDS.read_text()
    assert 'value="mock"' not in html
    assert "adapter === 'mock'" not in html


def test_the_wizard_sends_the_judge_it_collects():
    """It collected judgeAdapter/judgeModel and never sent them, so every graded check
    the UI built fell through to the substituted mock judge."""
    html = WIZARD.read_text()
    assert "payload.judge_spec" in html


@pytest.fixture
def client(tmp_db, monkeypatch):
    import importlib

    pytest.importorskip("fastapi")
    for var in ("ASSAY_AUTH", "ASSAY_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    import assay.server.app as mod
    importlib.reload(mod)
    from starlette.testclient import TestClient
    return TestClient(mod.app)


def test_the_ui_gets_an_actionable_422_not_a_500(fresh_install, client,
                                                 canned_llm, install_builder_llm):
    """An API caller that omits judge_spec used to get a mock judge; now it gets told."""
    install_builder_llm(canned_llm([JUDGE_INTENT]))
    resp = client.post("/pipelines/generate", json={
        "project": "p6", "name": "p6-pipe", "requirements": REQUIREMENTS,
        "adapter_spec": {"adapter": "anthropic", "model": "m"},
    }, headers={"X-Assay-User": "tester"})
    assert resp.status_code == 422
    assert "judge" in resp.json()["detail"].lower()


# ── the CLI ──────────────────────────────────────────────────────────────────


def _cli(*args):
    from typer.testing import CliRunner

    from assay.cli import app
    return CliRunner().invoke(app, list(args))


@pytest.fixture
def project(tmp_path):
    (tmp_path / "requirements.md").write_text(REQUIREMENTS)
    return tmp_path


def test_generate_refuses_an_explicit_mock_adapter(fresh_install, project):
    result = _cli("generate", "--adapter", "mock", "--project", "p",
                  "--requirements", str(project / "requirements.md"),
                  "--out", str(project))
    assert result.exit_code == 1
    assert "--offline" in result.output
    assert not (project / "assay.yaml").exists()


def test_generate_no_longer_defaults_to_mock(fresh_install, project):
    """Omitting --adapter used to silently build a pipeline against a mock."""
    result = _cli("generate", "--project", "p",
                  "--requirements", str(project / "requirements.md"),
                  "--out", str(project))
    assert result.exit_code == 1
    assert "--adapter is required" in result.output
    assert not (project / "assay.yaml").exists()


def test_target_ping_will_not_greenlight_a_mock(fresh_install):
    result = _cli("target", "ping", "--adapter", "mock")
    assert result.exit_code == 1
    assert "ok" not in result.output.lower().split("\n")[0]


def test_run_reports_the_refusal_rather_than_a_traceback(fresh_install, tmp_db, tmp_path):
    """The message is the deliverable; a stack trace buries it."""
    import yaml
    spec_path = tmp_path / "assay.yaml"
    spec_path.write_text(yaml.safe_dump(MOCK_SPEC))
    result = _cli("run", "--spec", str(spec_path))
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "ANTHROPIC_API_KEY" in result.output


def test_the_offline_path_still_works_end_to_end(fresh_install, tmp_db, project,
                                                 monkeypatch):
    """--offline is the documented no-keys path: it may use mocks, and it must run."""
    from assay.engine import execute_run
    from assay.spec.loader import load_spec

    # Rubric paths in the spec are relative to the project directory.
    monkeypatch.chdir(project)
    result = _cli("generate", "--offline", "--project", "p",
                  "--requirements", str(project / "requirements.md"),
                  "--out", str(project))
    assert result.exit_code == 0, result.output
    # The user is told, in the same breath, that the result is not evidence.
    assert ALLOW_MOCK_ENV in result.output

    spec = load_spec(str(project / "assay.yaml"))
    assert spec.target.adapter == "mock"
    # The mock judge is named in the file the user is asked to review, not substituted
    # somewhere they cannot see it.
    assert spec.judges["primary"].provider == "mock"
    cases = [case for _, case in spec.all_cases()]
    assert cases and all(case.input for case in cases)

    # Refuses to execute until the same explicit opt-in the message named.
    with pytest.raises(LLMConfigError):
        execute_run(spec, triggered_by="tester")
    monkeypatch.setenv(ALLOW_MOCK_ENV, "1")
    assert execute_run(spec, triggered_by="tester")


def test_offline_and_mock_together_are_allowed(fresh_install, project):
    result = _cli("generate", "--offline", "--adapter", "mock", "--project", "p",
                  "--requirements", str(project / "requirements.md"),
                  "--out", str(project))
    assert result.exit_code == 0, result.output
