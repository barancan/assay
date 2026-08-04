"""P2b: judging that can be trusted, and rubrics worth judging against.

Three failure modes are pinned here, all of which used to pass silently:

  * a judge invents a supporting quote and the check passes anyway;
  * a judge disagrees with itself and only one roll of the dice is recorded;
  * a judge omits a dimension and the missing score reads as a genuine 0.

Plus the rubric side: generation now produces anchored dimensions and is validated
before anything is written, because dimension ids become YAML keys and path components.

No network. The judge and the builder are doubles.
"""
from __future__ import annotations

import json

import pytest
import yaml

from assay.adapters.base import ModelResponse
from assay.generator.rubricgen import (
    RubricGenerationError,
    fallback_rubric,
    generate_rubric,
)
from assay.judges import VERDICT_SCHEMA, run_judge_check, verify_quotes

RESPONSE = {
    "text": "Article 17 requires erasure on request.\nThe controller must act "
            "without undue delay.",
    "json": None,
}
CONTEXT = {"input": {"prompt": "what does article 17 say?"}}


def rubric(**over) -> dict:
    """A two-dimension rubric; keyword args override any top-level key."""
    doc = {
        "judge": "primary",
        "dimensions": [
            {"id": "accuracy", "question": "Is the answer accurate?",
             "scale": {0: "contradicts the source", 1: "partly supported",
                       2: "fully supported by the source"},
             "min_score": 2},
        ],
        "output_schema": VERDICT_SCHEMA,
    }
    doc.update(over)
    return doc


class FakeJudge:
    """Replays verdicts in order, repeating the last one. Records every call."""

    name = "fake-judge"

    def __init__(self, *verdicts, structured: bool = True) -> None:
        self.verdicts = list(verdicts)
        self.structured = structured
        self.calls: list[dict] = []

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        verdict = self.verdicts[min(len(self.calls), len(self.verdicts) - 1)]
        self.calls.append({"messages": messages, "schema": schema, "params": params or {}})
        return ModelResponse(text=json.dumps(verdict),
                             json=verdict if self.structured else None,
                             status="ok")


def verdict(scores: dict, rationale: str = "because", quotes=()) -> dict:
    return {"scores": scores, "rationale": rationale, "evidence_quotes": list(quotes)}


# ── evidence enforcement ────────────────────────────────────────────────────

def test_genuine_quote_passes():
    judge = FakeJudge(verdict({"accuracy": 2}, quotes=["Article 17 requires erasure"]))
    out = run_judge_check(judge, rubric(require_evidence=True), RESPONSE, CONTEXT)
    assert out["passed"] is True
    assert out["evidence"]["verified_quotes"] == ["Article 17 requires erasure"]


def test_fabricated_quote_fails_even_with_a_top_score():
    judge = FakeJudge(verdict({"accuracy": 2},
                              quotes=["Article 99 abolishes the right to erasure"]))
    out = run_judge_check(judge, rubric(require_evidence=True), RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "do not appear in the response" in out["message"]
    assert out["evidence"]["unverified_quotes"]


def test_empty_evidence_fails_when_the_rubric_requires_it():
    judge = FakeJudge(verdict({"accuracy": 2}, quotes=[]))
    out = run_judge_check(judge, rubric(require_evidence=True), RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "quoted nothing" in out["message"]


def test_whitespace_only_quote_is_not_evidence():
    judge = FakeJudge(verdict({"accuracy": 2}, quotes=["   ", "..."]))
    out = run_judge_check(judge, rubric(require_evidence=True), RESPONSE, CONTEXT)
    assert out["passed"] is False


def test_evidence_is_not_required_unless_the_rubric_asks():
    judge = FakeJudge(verdict({"accuracy": 2}, quotes=["invented"]))
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT)
    assert out["passed"] is True
    assert "verified_quotes" not in out["evidence"]


@pytest.mark.parametrize("quote", [
    "article 17 REQUIRES erasure",              # re-cased
    "Article 17 requires\n  erasure on request",  # re-wrapped
    "Article 17 requires ... without undue delay",  # elided span
    "“Article 17 requires erasure”",   # curly quotes
])
def test_trivially_reformatted_quotes_still_count(quote):
    assert verify_quotes([quote], RESPONSE) == ([quote], [])


@pytest.mark.parametrize("quote", [
    "Article 18 requires erasure",
    "without undue delay ... Article 17 requires",   # right words, wrong order
    "the controller may ignore the request",
])
def test_quotes_that_are_not_in_the_response_are_rejected(quote):
    assert verify_quotes([quote], RESPONSE) == ([], [quote])


def test_quotes_may_come_from_a_structured_response():
    response = {"text": None, "json": {"finding": "erasure is mandatory"}}
    assert verify_quotes(["erasure is mandatory"], response) == (["erasure is mandatory"], [])


# ── self-consistency ────────────────────────────────────────────────────────

def test_median_across_disagreeing_samples_and_recorded_spread():
    judge = FakeJudge(
        verdict({"accuracy": 0}, quotes=["Article 17 requires erasure"]),
        verdict({"accuracy": 2}, quotes=["Article 17 requires erasure"]),
        verdict({"accuracy": 2}, quotes=["Article 17 requires erasure"]),
    )
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT, samples=3)
    assert len(judge.calls) == 3
    assert out["evidence"]["scores"]["accuracy"] == 2
    consistency = out["evidence"]["consistency"]
    assert consistency["samples"] == 3
    assert consistency["per_dimension"]["accuracy"]["samples"] == [0, 2, 2]
    assert consistency["per_dimension"]["accuracy"]["spread"] == 2
    assert consistency["agreed"] is False
    assert out["passed"] is True


def test_a_split_judge_does_not_get_the_benefit_of_the_doubt():
    """Even sample count: median 1.5 floors to 1, which is below min_score 2."""
    judge = FakeJudge(verdict({"accuracy": 1}), verdict({"accuracy": 2}))
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT, samples=2)
    assert out["evidence"]["scores"]["accuracy"] == 1
    assert out["passed"] is False


def test_agreement_is_recorded_when_the_samples_match():
    judge = FakeJudge(verdict({"accuracy": 2}))
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT, samples=3)
    assert out["evidence"]["consistency"]["agreed"] is True
    assert out["evidence"]["consistency"]["max_spread"] == 0


def test_single_sample_records_no_consistency_block():
    judge = FakeJudge(verdict({"accuracy": 2}))
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT)
    assert len(judge.calls) == 1
    assert "consistency" not in out["evidence"]


def test_samples_come_from_the_rubric():
    """Self-consistency is a per-rubric property, not a global setting."""
    judge = FakeJudge(verdict({"accuracy": 2}))
    run_judge_check(judge, rubric(samples=3), RESPONSE, CONTEXT)
    assert len(judge.calls) == 3


def test_every_sample_is_asked_at_temperature_zero():
    judge = FakeJudge(verdict({"accuracy": 2}))
    run_judge_check(judge, rubric(samples=3), RESPONSE, CONTEXT)
    assert all(c["params"]["temperature"] == 0.0 for c in judge.calls)


# ── missing and malformed scores ────────────────────────────────────────────

def test_missing_dimension_is_named_not_silently_zero():
    doc = rubric()
    doc["dimensions"].append(
        {"id": "completeness", "question": "Is it complete?",
         "scale": {0: "nothing", 1: "some", 2: "all"}, "min_score": 1})
    judge = FakeJudge(verdict({"accuracy": 2}))
    out = run_judge_check(judge, doc, RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "did not score dimension 'completeness'" in out["message"]
    assert "completeness" not in out["evidence"]["scores"]


def test_a_below_threshold_score_says_which_dimension_and_why():
    judge = FakeJudge(verdict({"accuracy": 1}))
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT)
    assert "accuracy=1 < min_score 2" in out["message"]


def test_a_verdict_with_no_scores_at_all_fails_by_name():
    judge = FakeJudge({"scores": {}, "rationale": "no model configured"})
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "accuracy" in out["message"]


# ── rubric plumbing ─────────────────────────────────────────────────────────

def test_inline_dict_rubric_and_path_rubric_agree(tmp_path):
    doc = rubric()
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))

    inline = run_judge_check(FakeJudge(verdict({"accuracy": 2})), doc, RESPONSE, CONTEXT)
    from_disk = run_judge_check(FakeJudge(verdict({"accuracy": 2})), str(path),
                                RESPONSE, CONTEXT)
    assert inline == from_disk
    assert inline["passed"] is True


def test_the_verdict_schema_is_sent_to_the_provider():
    judge = FakeJudge(verdict({"accuracy": 2}))
    run_judge_check(judge, rubric(), RESPONSE, CONTEXT)
    assert judge.calls[0]["schema"] == VERDICT_SCHEMA


def test_an_adapter_that_only_returns_text_still_works():
    """Fallback for adapters not yet returning a parsed object in ModelResponse.json."""
    judge = FakeJudge(verdict({"accuracy": 2}), structured=False)
    out = run_judge_check(judge, rubric(), RESPONSE, CONTEXT)
    assert out["passed"] is True


# ── rubric generation ───────────────────────────────────────────────────────

INTENT = {"id": "I1", "assertion": "the answer is faithful to the retrieved context",
          "how": "judge", "category": "quality"}

GOOD_RUBRIC = {
    "dimensions": [
        {"id": "faithfulness", "question": "Is every claim supported by the context?",
         "scale": {"0": "at least one claim contradicts the context",
                   "1": "one or more claims are absent from the context",
                   "2": "every claim restates something present in the context"},
         "min_score": 2},
        {"id": "no_omission", "question": "Does the answer omit contradicting context?",
         "scale": {"0": "context that contradicts the answer is ignored entirely",
                   "1": "contradicting context is mentioned but not reconciled",
                   "2": "all relevant context is addressed, including what disagrees"},
         "min_score": 1},
    ],
    "require_evidence": True,
    "samples": 3,
}


class ScriptedLLM:
    """Replays replies in order. `None` means "raise", to stand in for a dead provider."""

    name = "scripted"

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        if reply is None:
            raise RuntimeError("provider is down")
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return ModelResponse(text=text, status="ok")


def assert_anchored(doc: dict) -> None:
    assert len(doc["dimensions"]) >= 2
    for dim in doc["dimensions"]:
        assert set(dim["scale"]) == {0, 1, 2}
        for text in dim["scale"].values():
            assert len(text) >= 12, text
            assert text.lower() not in {"good", "bad", "partial", "meets"}
        assert 0 <= dim["min_score"] <= 2
    assert doc["output_schema"] == VERDICT_SCHEMA
    assert isinstance(doc["require_evidence"], bool)


def test_generated_rubric_is_anchored_and_carries_the_schema():
    doc = generate_rubric(INTENT, ScriptedLLM(GOOD_RUBRIC))
    assert_anchored(doc)
    assert [d["id"] for d in doc["dimensions"]] == ["faithfulness", "no_omission"]
    assert doc["samples"] == 3


def test_the_fallback_rubric_is_anchored_too():
    assert_anchored(fallback_rubric(INTENT))
    assert INTENT["assertion"] in fallback_rubric(INTENT)["dimensions"][0]["question"]


def test_the_fallback_rubric_satisfies_the_generators_own_validator():
    """The floor has to clear the bar it holds model output to."""
    from assay.generator.rubricgen import _validate
    doc = fallback_rubric(INTENT)
    assert _validate(doc, INTENT) == doc


def test_a_generated_rubric_is_usable_by_the_judge():
    doc = generate_rubric(INTENT, ScriptedLLM(GOOD_RUBRIC))
    judge = FakeJudge(verdict({"faithfulness": 2, "no_omission": 2},
                              quotes=["Article 17 requires erasure"]))
    out = run_judge_check(judge, doc, RESPONSE, CONTEXT)
    assert out["passed"] is True
    assert len(judge.calls) == 3   # the rubric asked for three samples


def test_malformed_output_is_repaired_on_the_second_attempt():
    llm = ScriptedLLM("not json at all", GOOD_RUBRIC)
    doc = generate_rubric(INTENT, llm)
    assert [d["id"] for d in doc["dimensions"]] == ["faithfulness", "no_omission"]
    assert len(llm.prompts) == 2
    assert "rejected" in llm.prompts[1]


def test_persistently_malformed_output_falls_back():
    llm = ScriptedLLM("nope", "still nope")
    doc = generate_rubric(INTENT, llm)
    assert doc == fallback_rubric(INTENT)
    assert len(llm.prompts) == 2   # one repair attempt, then it stops asking


def test_a_dead_provider_falls_back_without_retrying():
    llm = ScriptedLLM(None)
    assert generate_rubric(INTENT, llm) == fallback_rubric(INTENT)
    assert len(llm.prompts) == 1


def test_no_llm_means_the_deterministic_rubric():
    assert generate_rubric(INTENT, None) == fallback_rubric(INTENT)


@pytest.mark.parametrize("bad_id", [
    "../../etc/passwd", "../escape", "a/b", "generated/rubrics/x", "..", "a b", "",
])
def test_a_dimension_id_that_would_escape_its_directory_is_rejected(bad_id):
    reply = json.loads(json.dumps(GOOD_RUBRIC))
    reply["dimensions"][0]["id"] = bad_id
    with pytest.raises(RubricGenerationError):
        from assay.generator.rubricgen import _validate
        _validate(reply, INTENT)
    # And end to end: nothing traversal-shaped survives into a persisted rubric.
    doc = generate_rubric(INTENT, ScriptedLLM(reply, reply))
    assert doc == fallback_rubric(INTENT)
    assert all("/" not in d["id"] and ".." not in d["id"] for d in doc["dimensions"])


@pytest.mark.parametrize("mutate, why", [
    (lambda r: r["dimensions"].pop(), "fewer than two dimensions"),
    (lambda r: r["dimensions"][1].__setitem__("id", "faithfulness"), "duplicate ids"),
    (lambda r: r["dimensions"][0]["scale"].pop("1"), "incomplete scale"),
    (lambda r: r["dimensions"][0]["scale"].__setitem__("2", "good"), "one-word anchor"),
    (lambda r: r["dimensions"][0]["scale"].__setitem__("2", "the response is very good"),
     "long but still only a grade"),
    (lambda r: r["dimensions"][0]["scale"].__setitem__("2", "  "), "empty anchor"),
    (lambda r: r["dimensions"][0].__setitem__("min_score", 5), "min_score out of range"),
    (lambda r: r["dimensions"][0].__setitem__("min_score", -1), "negative min_score"),
    (lambda r: r["dimensions"][0].__setitem__("question", "eh"), "no real question"),
])
def test_invalid_rubrics_are_rejected(mutate, why):
    from assay.generator.rubricgen import _validate
    reply = json.loads(json.dumps(GOOD_RUBRIC))
    mutate(reply)
    with pytest.raises(RubricGenerationError):
        _validate(reply, INTENT)


# ── build wiring ────────────────────────────────────────────────────────────

def test_rubric_for_offline_is_the_deterministic_fallback():
    from assay.generator.build import rubric_for
    assert rubric_for(INTENT) == fallback_rubric(INTENT)


def test_rubric_for_uses_the_builder_model_when_there_is_one():
    from assay.generator.build import rubric_for
    doc = rubric_for(INTENT, ScriptedLLM(GOOD_RUBRIC))
    assert [d["id"] for d in doc["dimensions"]] == ["faithfulness", "no_omission"]


def test_generated_rubrics_round_trip_through_yaml():
    """They are persisted as YAML text on the PipelineVersion, then read back."""
    doc = generate_rubric(INTENT, ScriptedLLM(GOOD_RUBRIC))
    assert yaml.safe_load(yaml.safe_dump(doc, sort_keys=False)) == doc


def test_generation_is_grounded_by_the_interface_when_there_is_one():
    from assay.generator.interface import Interface
    llm = ScriptedLLM(GOOD_RUBRIC)
    generate_rubric(INTENT, llm, interface=Interface(kind="postman",
                                                    input_fields=["text"],
                                                    response_paths=["$.findings[*]"]))
    assert "$.findings[*]" in llm.prompts[0]


# ── the web path must not be weaker than the CLI ────────────────────────────

def test_web_generate_uses_the_builder_model_for_rubrics(tmp_path, monkeypatch):
    """The UI passed no model, so every rubric was the deterministic fallback."""
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 'web.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db
    init_db()

    import assay.generator.build as build
    seen = {}
    original = build.rubric_for

    def spy(intent, llm=None, *, interface=None):
        seen["llm"] = llm
        seen["interface"] = interface
        return original(intent, llm, interface=interface)

    monkeypatch.setattr(build, "rubric_for", spy)

    from fastapi.testclient import TestClient
    from assay.server.app import app
    from tests.conftest import HeuristicBuilderLLM

    fake = HeuristicBuilderLLM()
    monkeypatch.setattr("assay.llm.provider.resolve_builder_llm", lambda project=None: fake)

    resp = TestClient(app).post("/pipelines/generate", json={
        "project": "rub", "name": "rub",
        "requirements": "- The model must decline to answer when it is uncertain.",
        "adapter_spec": {"adapter": "mock"},
    }, headers={"X-Assay-User": "alice"})

    assert resp.status_code == 200, resp.text
    assert seen, "rubric_for was never called for a judge intent"
    assert seen["llm"] is fake, "the web path must hand the builder model to rubric generation"
