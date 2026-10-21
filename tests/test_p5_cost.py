"""P5: token and cost capture.

Everything here runs offline with no keys. The recurring theme is that "free" and
"unknown" must never collapse into each other -- a run that spent money and reports
$0.00 is the bug this phase exists to remove.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from assay.pricing import PricingError, estimate_cost, normalise_usage, rate_for


@pytest.fixture
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


# ── usage normalisation ────────────────────────────────────────────────────

def test_normalise_anthropic_shape():
    usage = {"input_tokens": 120, "output_tokens": 45}
    assert normalise_usage("anthropic", usage) == {"input_tokens": 120, "output_tokens": 45}


def test_normalise_openai_shape():
    usage = {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}
    assert normalise_usage("openai_compat", usage) == {"input_tokens": 120,
                                                      "output_tokens": 45}


def test_normalise_ollama_shape():
    usage = {"prompt_eval_count": 120, "eval_count": 45, "total_duration": 999}
    assert normalise_usage("ollama", usage) == {"input_tokens": 120, "output_tokens": 45}


def test_normalise_missing_usage_is_zero_not_error():
    assert normalise_usage("anthropic", {}) == {"input_tokens": 0, "output_tokens": 0}
    assert normalise_usage("anthropic", None) == {"input_tokens": 0, "output_tokens": 0}


def test_normalise_unknown_provider_falls_back_to_any_known_shape():
    """A new adapter reporting a familiar shape still gets counted."""
    assert normalise_usage("brand-new", {"prompt_tokens": 7, "completion_tokens": 3}) == {
        "input_tokens": 7, "output_tokens": 3}


# ── cost maths ─────────────────────────────────────────────────────────────

def test_cost_prices_input_and_output_separately():
    """claude-sonnet-4-5 is $3/Mtok in, $15/Mtok out."""
    cost = estimate_cost("anthropic", "claude-sonnet-4-5",
                         {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert cost == pytest.approx(18.0)


def test_cost_scales_with_tokens():
    cost = estimate_cost("anthropic", "claude-sonnet-4-5",
                         {"input_tokens": 1000, "output_tokens": 500})
    # 1000 * 3/1e6 + 500 * 15/1e6
    assert cost == pytest.approx(0.003 + 0.0075)


def test_cost_uses_openai_key_names():
    cost = estimate_cost("openai_compat", "gpt-4o-mini",
                         {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert cost == pytest.approx(0.15)


# ── unknown vs free ────────────────────────────────────────────────────────

def test_unknown_model_returns_none_not_zero():
    """The whole point: an unpriced model is absent, never a guess and never free."""
    cost = estimate_cost("anthropic", "claude-something-unreleased",
                         {"input_tokens": 1000, "output_tokens": 1000})
    assert cost is None


def test_priced_model_with_no_reported_usage_is_unknown():
    """Provider said nothing about tokens: that is unknown, not a free call."""
    assert estimate_cost("anthropic", "claude-sonnet-4-5", {}) is None


def test_local_providers_are_free_not_unknown():
    assert estimate_cost("ollama", "llama3", {"prompt_eval_count": 900,
                                              "eval_count": 100}) == 0.0
    assert estimate_cost("mock", None, {"input_tokens": 0, "output_tokens": 0}) == 0.0


def test_free_and_unknown_are_distinguishable():
    free = estimate_cost("ollama", "llama3", {"prompt_eval_count": 5, "eval_count": 5})
    unknown = estimate_cost("openai_compat", "some-private-deployment",
                            {"prompt_tokens": 5, "completion_tokens": 5})
    assert free == 0.0 and unknown is None
    assert free is not None                 # 0.0 is falsy; identity must still differ


def test_mock_adapter_records_zero_not_none():
    from assay.adapters.mock import MockAdapter, MockJudge
    from assay.adapters.base import ModelRequest
    resp = MockAdapter().invoke(ModelRequest(input={"q": "hi"}))
    assert resp.cost_usd == 0.0 and resp.cost_usd is not None
    assert resp.usage == {"input_tokens": 0, "output_tokens": 0}
    assert MockJudge().complete([{"role": "user", "content": "x"}]).cost_usd == 0.0


# ── model id matching ──────────────────────────────────────────────────────

def test_version_suffixed_id_matches_base_entry():
    """Providers version model ids; a dated id is the same model."""
    dated = rate_for("anthropic", "claude-haiku-4-5-20251001")
    assert dated is not None and dated == rate_for("anthropic", "claude-haiku-4-5")


def test_dashed_date_and_vendor_prefix_also_match():
    assert rate_for("openai_compat", "gpt-4o-mini-2024-07-18") == \
        rate_for("openai_compat", "gpt-4o-mini")
    assert rate_for("anthropic", "anthropic/claude-sonnet-4-5") == \
        rate_for("anthropic", "claude-sonnet-4-5")


def test_matching_never_crosses_model_families():
    """A version suffix may be tolerated; a different model name may not."""
    haiku = rate_for("anthropic", "claude-haiku-4-5-20251001")
    assert haiku != rate_for("anthropic", "claude-sonnet-4-5")
    assert haiku != rate_for("anthropic", "claude-opus-4-1")
    # `gpt-4o-mini` is a tenth the price of `gpt-4o`; it must never inherit its rate.
    assert rate_for("openai_compat", "gpt-4o-mini") != rate_for("openai_compat", "gpt-4o")


def test_unrelated_suffix_does_not_match_a_shorter_entry():
    """`-turbo` is a different model, not a version tag, so this stays unknown."""
    assert rate_for("openai_compat", "gpt-4o-turbo-supreme") is None


def test_family_names_are_not_matched_across_providers():
    assert rate_for("openai_compat", "claude-sonnet-4-5") is None


# ── ASSAY_PRICING_FILE override ────────────────────────────────────────────

def test_pricing_file_overrides_a_listed_model(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(
        {"anthropic": {"claude-sonnet-4-5": {"input": 1.0, "output": 2.0}}}))
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(path))
    cost = estimate_cost("anthropic", "claude-sonnet-4-5",
                         {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert cost == pytest.approx(3.0)


def test_pricing_file_adds_a_model_the_table_does_not_know(tmp_path, monkeypatch):
    """A user on a new or private model is not blocked on an assay release."""
    assert estimate_cost("openai_compat", "my-finetune",
                         {"prompt_tokens": 1_000_000, "completion_tokens": 0}) is None
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"openai_compat": {"my-finetune": {"input": 9.0,
                                                                 "output": 9.0}}}))
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(path))
    assert estimate_cost("openai_compat", "my-finetune",
                         {"prompt_tokens": 1_000_000, "completion_tokens": 0}) == \
        pytest.approx(9.0)


def test_pricing_file_leaves_unmentioned_models_alone(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"anthropic": {"claude-haiku-4-5": {"input": 0.1,
                                                                  "output": 0.2}}}))
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(path))
    assert rate_for("anthropic", "claude-sonnet-4-5") == {"input": 3.0, "output": 15.0}


def test_pricing_file_override_survives_version_suffix_matching(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"anthropic": {"claude-haiku-4-5": {"input": 0.5,
                                                                  "output": 1.0}}}))
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(path))
    assert rate_for("anthropic", "claude-haiku-4-5-20251001") == {"input": 0.5,
                                                                 "output": 1.0}


def test_malformed_pricing_file_raises_rather_than_being_ignored(tmp_path, monkeypatch):
    """Silently ignoring it would bill the user at list rates they thought they replaced."""
    path = tmp_path / "prices.json"
    path.write_text("{not json")
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(path))
    with pytest.raises(PricingError):
        estimate_cost("anthropic", "claude-sonnet-4-5", {"input_tokens": 1})


def test_missing_pricing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(tmp_path / "nope.json"))
    with pytest.raises(PricingError):
        estimate_cost("anthropic", "claude-sonnet-4-5", {"input_tokens": 1})


def test_pricing_file_with_bad_rate_shape_raises(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"anthropic": {"claude-sonnet-4-5": {"input": 1.0}}}))
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(path))
    with pytest.raises(PricingError):
        estimate_cost("anthropic", "claude-sonnet-4-5", {"input_tokens": 1})


def test_pricing_file_is_reread_when_it_changes(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"anthropic": {"m-1": {"input": 1.0, "output": 1.0}}}))
    monkeypatch.setenv("ASSAY_PRICING_FILE", str(path))
    assert rate_for("anthropic", "m-1") == {"input": 1.0, "output": 1.0}
    path.write_text(json.dumps({"anthropic": {"m-1": {"input": 4.0, "output": 4.0}}}))
    import os
    os.utime(path, (0, 0))          # force a different mtime, not just a different size
    assert rate_for("anthropic", "m-1") == {"input": 4.0, "output": 4.0}


# ── adapters set cost on every response ────────────────────────────────────

class _FakeAnthropicMessage:
    def __init__(self, tokens_in, tokens_out):
        import types
        self.usage = types.SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out)
        self.content = [types.SimpleNamespace(type="text", text="hello")]

    def model_dump(self):
        return {"content": [{"type": "text", "text": "hello"}]}


def _anthropic_adapter(monkeypatch, model, tokens_in=1000, tokens_out=500):
    from assay.adapters.anthropic import AnthropicAdapter
    a = AnthropicAdapter(model=model)

    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _FakeAnthropicMessage(tokens_in, tokens_out)

    monkeypatch.setattr(a, "_client", lambda: _Client())
    return a


def test_anthropic_adapter_normalises_usage_and_prices(monkeypatch):
    from assay.adapters.base import ModelRequest
    a = _anthropic_adapter(monkeypatch, "claude-sonnet-4-5")
    resp = a.invoke(ModelRequest(input={"prompt": "hi"}))
    assert resp.usage == {"input_tokens": 1000, "output_tokens": 500}
    assert resp.cost_usd == pytest.approx(1000 * 3 / 1e6 + 500 * 15 / 1e6)


def test_anthropic_adapter_unknown_model_reports_none(monkeypatch):
    from assay.adapters.base import ModelRequest
    a = _anthropic_adapter(monkeypatch, "claude-not-a-real-model")
    resp = a.invoke(ModelRequest(input={"prompt": "hi"}))
    assert resp.usage == {"input_tokens": 1000, "output_tokens": 500}   # tokens still counted
    assert resp.cost_usd is None


def test_openai_adapter_prices_from_usage_block(monkeypatch):
    import assay.adapters.openai_compat as mod
    from assay.adapters.base import ModelRequest

    class _R:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                              "total_tokens": 1_000_000}}

    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _R())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    adapter = mod.OpenAICompatAdapter(model="gpt-4o-mini")
    resp = adapter.invoke(ModelRequest(input={"prompt": "hi"}))
    assert resp.usage == {"input_tokens": 1_000_000, "output_tokens": 0}
    assert resp.cost_usd == pytest.approx(0.15)


def test_ollama_adapter_counts_tokens_and_is_free(monkeypatch):
    import assay.adapters.ollama as mod
    from assay.adapters.base import ModelRequest

    class _R:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"response": "hi", "prompt_eval_count": 900, "eval_count": 100}

    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _R())
    resp = mod.OllamaAdapter(model="llama3").invoke(ModelRequest(input={"prompt": "hi"}))
    assert resp.usage == {"input_tokens": 900, "output_tokens": 100}
    assert resp.cost_usd == 0.0


# ── judge cost attribution ─────────────────────────────────────────────────

RUBRIC = {
    "dimensions": [{"id": "accuracy", "question": "Is it right?",
                    "scale": {"0": "no", "1": "yes"}, "min_score": 0}],
}


@pytest.fixture
def rubric_path(tmp_path):
    """RUBRIC on disk -- a spec's `rubric` field is a path, not an inline dict."""
    import yaml
    p = tmp_path / "rubric.yaml"
    p.write_text(yaml.safe_dump(RUBRIC))
    return str(p)


JUDGE_SPEC = {"provider": "mock", "model": "mock-judge"}


class _CostedJudge:
    """A judge whose every call costs a fixed, known amount."""
    name = "costed-judge"

    def __init__(self, cost=0.01, tokens_in=100, tokens_out=20):
        self.cost, self.tokens_in, self.tokens_out = cost, tokens_in, tokens_out
        self.calls = 0

    def complete(self, messages, *, schema=None, tools=None, params=None):
        from assay.adapters.base import ModelResponse
        self.calls += 1
        verdict = {"scores": {"accuracy": 1}, "rationale": "fine", "evidence_quotes": []}
        return ModelResponse(text=json.dumps(verdict), json=verdict, status="ok",
                             usage={"input_tokens": self.tokens_in,
                                    "output_tokens": self.tokens_out},
                             cost_usd=self.cost)


def test_judge_returns_its_own_cost_alongside_the_verdict():
    from assay.judges import run_judge_check
    judge = _CostedJudge(cost=0.01)
    out = run_judge_check(judge, RUBRIC, {"text": "hi"}, {"input": {}})
    assert out["passed"] is True
    assert out["cost_usd"] == pytest.approx(0.01)
    assert out["usage"] == {"input_tokens": 100, "output_tokens": 20}


def test_judge_cost_counts_every_self_consistency_sample():
    """samples > 1 is N billed calls; charging for one of them is the silent-drop bug."""
    from assay.judges import run_judge_check
    judge = _CostedJudge(cost=0.01)
    out = run_judge_check(judge, RUBRIC, {"text": "hi"}, {"input": {}}, samples=3)
    assert judge.calls == 3
    assert out["cost_usd"] == pytest.approx(0.03)
    assert out["usage"] == {"input_tokens": 300, "output_tokens": 60}
    assert out["evidence"]["cost"]["samples"] == 3


def test_judge_samples_from_the_rubric_are_also_counted():
    from assay.judges import run_judge_check
    judge = _CostedJudge(cost=0.01)
    out = run_judge_check(judge, {**RUBRIC, "samples": 4}, {"text": "hi"}, {"input": {}})
    assert judge.calls == 4
    assert out["cost_usd"] == pytest.approx(0.04)


def test_judge_cost_is_unknown_when_a_sample_is_unpriced():
    """A partial sum would read as a complete one and understate the bill."""
    from assay.judges import run_judge_check
    judge = _CostedJudge(cost=None)
    out = run_judge_check(judge, RUBRIC, {"text": "hi"}, {"input": {}}, samples=2)
    assert out["cost_usd"] is None
    assert out["usage"] == {"input_tokens": 200, "output_tokens": 40}   # tokens still known


def test_judge_cost_survives_onto_the_check_result(rubric_path):
    """`evidence` is the only field from_raw carries, so cost must ride along in it."""
    from assay.checks.registry import run_check
    from assay.spec.models import CheckSpec
    judge = _CostedJudge(cost=0.02)
    spec = CheckSpec(type="judge", judge="j", rubric=rubric_path)
    result = run_check(spec, {"text": "hi"}, {"input": {}}, {"j": judge})
    assert result.evidence["cost"]["usd"] == pytest.approx(0.02)


# ── roll-up through a real run ─────────────────────────────────────────────

def _spec_with_judge(rubric_path, n_cases=2):
    return {
        "version": 1, "project": "cost-test",
        "target": {"adapter": "mock"},
        "judges": {"j": JUDGE_SPEC},
        "suites": [{"id": "s1", "requirement_ref": None, "cases": [
            {"id": f"c{i}", "input": {}, "checks": [
                {"type": "judge", "judge": "j", "rubric": rubric_path},
            ]} for i in range(n_cases)
        ]}],
        "gating": {},
    }


def test_run_total_is_the_sum_of_case_costs_including_judges(_tmp_db, monkeypatch,
                                                             rubric_path):
    from assay.engine import execute_run
    from assay.spec.models import Spec
    from assay.store import session_scope
    from assay.store.models import Run, CaseResult
    import assay.engine.runner as runner

    judge = _CostedJudge(cost=0.005, tokens_in=100, tokens_out=20)
    monkeypatch.setattr(runner, "get_judge_provider", lambda cfg: judge)
    spec = Spec.model_validate(_spec_with_judge(rubric_path, 2))

    run_id = execute_run(spec, triggered_by="tester")
    with session_scope() as s:
        run = s.get(Run, run_id)
        cases = s.query(CaseResult).filter_by(run_id=run_id).all()
        assert len(cases) == 2
        for c in cases:
            # mock target is free (0.0); the judge adds its own spend on top.
            assert c.cost_usd == pytest.approx(0.005)
            assert c.judge_tokens == 120
            assert c.input_tokens == 0 and c.output_tokens == 0
        assert run.total_cost_usd == pytest.approx(sum(c.cost_usd for c in cases))
        assert run.total_cost_usd == pytest.approx(0.01)


def test_run_total_is_no_longer_always_zero(_tmp_db, monkeypatch, rubric_path):
    """The bug this phase fixes: judge spend used to be invisible to the roll-up."""
    from assay.engine import execute_run
    from assay.spec.models import Spec
    from assay.store import session_scope
    from assay.store.models import Run
    import assay.engine.runner as runner

    monkeypatch.setattr(runner, "get_judge_provider", lambda cfg: _CostedJudge(cost=0.25))
    spec = Spec.model_validate(_spec_with_judge(rubric_path, 1))
    run_id = execute_run(spec, triggered_by="tester")
    with session_scope() as s:
        assert s.get(Run, run_id).total_cost_usd == pytest.approx(0.25)


def test_case_cost_is_unknown_when_the_target_is_unpriced(_tmp_db, monkeypatch):
    from assay.engine import execute_run
    from assay.spec.models import Spec
    from assay.store import session_scope
    from assay.store.models import CaseResult
    import assay.engine.runner as runner
    from assay.adapters.base import ModelResponse

    class _UnpricedTarget:
        name = "unpriced"

        def describe(self):
            return {"adapter": self.name}

        def ping(self):
            return {"ok": True, "reachable": True, "authenticated": None,
                    "latency_ms": 0.0, "error": None, "env_var": None}

        def invoke(self, req):
            return ModelResponse(text="hi", usage={"input_tokens": 10, "output_tokens": 5},
                                 cost_usd=None, status="ok")

    monkeypatch.setattr(runner, "get_target_adapter", lambda cfg: _UnpricedTarget())
    spec = Spec.model_validate({
        "version": 1, "project": "p", "target": {"adapter": "mock"}, "judges": {},
        "suites": [{"id": "s1", "cases": [
            {"id": "c1", "input": {}, "checks": [{"type": "template",
                                                  "uses": "valid_json"}]}]}],
        "gating": {}})
    run_id = execute_run(spec, triggered_by="tester")
    with session_scope() as s:
        c = s.query(CaseResult).filter_by(run_id=run_id).one()
        assert c.cost_usd is None                 # unknown, not 0.0
        assert c.input_tokens == 10 and c.output_tokens == 5


# ── reporting ──────────────────────────────────────────────────────────────

def test_exported_report_shows_per_case_and_total_spend(_tmp_db, monkeypatch, rubric_path):
    from assay.engine import execute_run
    from assay.reporting import export_report
    from assay.spec.models import Spec
    import assay.engine.runner as runner

    monkeypatch.setattr(runner, "get_judge_provider", lambda cfg: _CostedJudge(cost=0.005))
    spec = Spec.model_validate(_spec_with_judge(rubric_path, 2))
    run_id = execute_run(spec, triggered_by="tester")
    paths = export_report(run_id)

    data = json.loads(open(paths["json"]).read())
    assert data["spend"]["total_usd"] == pytest.approx(0.01)
    assert data["spend"]["complete"] is True
    assert data["spend"]["unknown_cases"] == 0
    assert all(c["cost_usd"] is not None for c in data["cases"])

    md = open(paths["md"]).read()
    assert "## Spend" in md
    assert "$0.0100" in md
    assert "judge tokens" in md


def test_unknown_cost_renders_as_unknown_not_zero():
    from assay.reporting.exporters import money, spend
    assert money(None) == "unknown"
    assert money(0.0) == "$0.0000"

    data = {"cases": [{"cost_usd": None, "input_tokens": 5, "output_tokens": 5,
                       "judge_tokens": 0},
                      {"cost_usd": 0.25, "input_tokens": 1, "output_tokens": 1,
                       "judge_tokens": 2}]}
    s = spend(data)
    assert s["unknown_cases"] == 1
    assert s["complete"] is False
    assert s["total_usd"] == pytest.approx(0.25)     # the unknown case is NOT added as 0


def test_markdown_flags_unpriced_cases_instead_of_implying_a_complete_total():
    from assay.reporting.exporters import _md
    data = {"project": "p", "run_id": 1, "spec_hash": "h", "git_commit": None,
            "trigger": "manual", "triggered_by": "t", "state": "pending",
            "approved_by": None, "approved_at": None, "requirements": [],
            "target": {"adapter": "a", "model": "m", "endpoint": None, "params": {}},
            "summary": {}, "cost_usd": 0.0,
            "cases": [{"suite": "s", "case": "c", "requirement_ref": None,
                       "passed": True, "latency_ms": 1.0, "checks": [],
                       "cost_usd": None, "input_tokens": None,
                       "output_tokens": None, "judge_tokens": None}]}
    out = _md(data)
    assert "unknown" in out
    assert "could not" in out and "priced" in out
    assert "lower bound" in out


def test_report_page_renders_unknown_distinctly_from_zero(_tmp_db, monkeypatch):
    """The HTML view must not print $0.00 for a case whose cost is not known."""
    pytest.importorskip("fastapi")
    pytest.importorskip("jinja2")
    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path
    import assay.server

    tpl_dir = Path(assay.server.__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(tpl_dir)))
    # Render the spend fragment alone; the surrounding page needs the full app context.
    source = (tpl_dir / "report_detail.html").read_text()
    start = source.index("{% set sp = namespace")
    end = source.index("<!-- Reviewer assignment -->")
    fragment = env.from_string(source[start:end])

    html = fragment.render(case_results=[
        {"id": 1, "suite_id": "s", "case_id": "known", "cost_usd": 0.0,
         "input_tokens": 3, "output_tokens": 4, "judge_tokens": 0},
        {"id": 2, "suite_id": "s", "case_id": "unpriced", "cost_usd": None,
         "input_tokens": 5, "output_tokens": 6, "judge_tokens": 0},
    ])
    assert "unknown" in html          # the unpriced case says so
    assert "$0.0000" in html          # the genuinely-free case shows a real zero
    assert "1 of 2 case(s) could not be priced" in html


def test_a_generated_check_cannot_inject_cost_into_the_run_total():
    """Generated checks are model-written code; their evidence must not move the bill."""
    from assay.checks.base import CheckResult
    from assay.engine.runner import _judge_spend
    forged = CheckResult("generated:x", True, type="generated",
                         evidence={"cost": {"usd": 99.0, "input_tokens": 10 ** 9,
                                            "output_tokens": 0}})
    assert _judge_spend([forged]) == {"tokens": 0, "usd": 0.0}


def test_report_page_shows_real_per_case_cost_end_to_end(_tmp_db):
    """The rendered page should show the cost that is actually in the database."""
    pytest.importorskip("fastapi")
    pytest.importorskip("jinja2")
    import importlib
    from starlette.testclient import TestClient
    from assay.engine import execute_run
    from assay.spec.models import Spec
    from assay.store import session_scope
    from assay.store.models import Report, CaseResult
    import assay.server.app

    spec = Spec.model_validate({
        "version": 1, "project": "p", "target": {"adapter": "mock"}, "judges": {},
        "suites": [{"id": "s1", "cases": [
            {"id": "c1", "input": {},
             "checks": [{"type": "template", "uses": "valid_json"}]}]}],
        "gating": {}})
    run_id = execute_run(spec, triggered_by="tester")
    with session_scope() as s:
        rep_id = s.query(Report).filter_by(run_id=run_id).one().id
        # The mock target is free, so this is a real 0.0 -- not an unknown.
        assert s.query(CaseResult).filter_by(run_id=run_id).one().cost_usd == 0.0

    importlib.reload(assay.server.app)
    resp = TestClient(assay.server.app.app).get(f"/reports/{rep_id}/view")
    assert resp.status_code == 200
    assert "$0.0000" in resp.text
    assert "could not be priced" not in resp.text


# ── migration ──────────────────────────────────────────────────────────────

def test_migration_adds_columns_to_a_preexisting_database(tmp_path, monkeypatch):
    """A DB created before P5 must still open, and gain the new columns."""
    db_path = tmp_path / "old.db"

    # Build the pre-P5 shape of case_results by hand: no token or cost columns.
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE case_results (
            id INTEGER NOT NULL PRIMARY KEY,
            run_id INTEGER NOT NULL,
            suite_id VARCHAR(120) NOT NULL,
            case_id VARCHAR(120) NOT NULL,
            requirement_ref VARCHAR(200),
            request JSON,
            response JSON,
            checks JSON,
            passed BOOLEAN,
            latency_ms FLOAT,
            human_verdict VARCHAR(10),
            overridden_by VARCHAR(120),
            overridden_at DATETIME,
            override_reason TEXT
        );
        INSERT INTO case_results (id, run_id, suite_id, case_id, passed, latency_ms)
        VALUES (1, 1, 's1', 'c1', 1, 12.5);
    """)
    con.commit()
    con.close()

    before = {r[1] for r in sqlite3.connect(db_path).execute(
        "PRAGMA table_info(case_results)")}
    assert "cost_usd" not in before

    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{db_path}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    assay.store.db.init_db()

    after = {r[1] for r in sqlite3.connect(db_path).execute(
        "PRAGMA table_info(case_results)")}
    assert {"input_tokens", "output_tokens", "judge_tokens", "cost_usd"} <= after

    # The pre-existing row still reads, with NULL (unknown) for the new columns.
    from assay.store import session_scope
    from assay.store.models import CaseResult
    with session_scope() as s:
        row = s.get(CaseResult, 1)
        assert row.case_id == "c1" and row.latency_ms == 12.5
        assert row.cost_usd is None and row.input_tokens is None


def test_migration_is_idempotent(_tmp_db):
    """init_db runs on every start; running it twice must not fail."""
    import assay.store.db
    assay.store.db.init_db()
    assay.store.db.init_db()
