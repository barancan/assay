"""P1: a real LLM in every build path.

The web UI used to hardcode `judge=None`, so every pipeline built through the product's
primary surface came from an offline keyword heuristic that looked identical to a real
one once persisted. These tests pin the replacement: the builder model is resolved and
called, its output is validated against the requirements it was given, and any failure
surfaces as an actionable 422 instead of a silent downgrade.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "assay" / "server" / "templates" / "pipeline_new.html"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.delenv("ASSAY_LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("ASSAY_AUTH", raising=False)
    monkeypatch.delenv("ASSAY_SECRET_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db
    init_db()
    yield


@pytest.fixture
def client(_tmp_db):
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


REQUIREMENTS = (
    "R1. Every response must be valid JSON.\n"
    "R2. Responses must not leak PII.\n"
)

GOOD_INTENTS = [
    {"id": "json-shape", "requirement_ref": "R1", "category": "format",
     "assertion": "response is valid JSON", "how": "template",
     "template": "valid_json", "params": {}},
    {"id": "no-pii", "requirement_ref": "R2", "category": "safety",
     "assertion": "no personal data in the output", "how": "template",
     "template": "pii_absent", "params": {}},
    {"id": "not-fabricated", "requirement_ref": "R2", "category": "quality",
     "assertion": "no fabricated personal details", "how": "judge", "threshold": 0.85},
]


# ── requirement ingestion ────────────────────────────────────────────────────

def test_numbered_markdown_requirements_get_stable_ids():
    from assay.generator.ingest import split_requirements
    reqs = split_requirements(
        "# Assessment requirements\n\n"
        "R1. The model must return valid JSON.\n"
        "R2. Responses must complete within 5 seconds.\n"
    )
    assert [r["id"] for r in reqs] == ["R1", "R2"]
    assert reqs[0]["text"] == "The model must return valid JSON."
    assert reqs[0]["section"] == "Assessment requirements"


def test_bullets_and_plain_lines_are_separate_requirements():
    from assay.generator.ingest import split_requirements
    bullets = split_requirements("- Must be valid JSON.\n* No PII in the output.\n")
    assert [r["id"] for r in bullets] == ["R1", "R2"]
    # The wizard textarea: one sentence per line, no markers at all.
    lines = split_requirements("The response must be valid JSON.\nNo PII in the output.")
    assert [r["text"] for r in lines] == [
        "The response must be valid JSON.", "No PII in the output."]


def test_wrapped_lines_stay_with_their_requirement():
    from assay.generator.ingest import split_requirements
    reqs = split_requirements(
        "R4. When the feature description is ambiguous, the copilot must flag uncertainty\n"
        "    rather than asserting compliance.\n"
    )
    assert len(reqs) == 1
    assert reqs[0]["text"].endswith("rather than asserting compliance.")


def test_unnumbered_prose_still_yields_one_requirement():
    from assay.generator.ingest import split_requirements
    assert [r["id"] for r in split_requirements("Be helpful")] == ["R1"]
    assert split_requirements("   \n\n") == []


# ── derive_intents: the model is actually used ───────────────────────────────

def test_prompt_carries_the_requirements_and_their_ids(canned_llm):
    from assay.generator.build import derive_intents
    llm = canned_llm(GOOD_INTENTS)
    derive_intents(REQUIREMENTS, judge=llm)
    prompt = llm.last_prompt
    assert "R1: Every response must be valid JSON." in prompt
    assert "R2: Responses must not leak PII." in prompt
    assert "requirement_ref" in prompt


def test_refs_round_trip_from_the_model_to_the_spec(canned_llm):
    from assay.generator.build import derive_intents, intents_to_spec
    intents = derive_intents(REQUIREMENTS, judge=canned_llm(GOOD_INTENTS))
    assert [it["requirement_ref"] for it in intents] == ["R1", "R2", "R2"]
    spec = intents_to_spec("p", intents, {"adapter": "mock"}, {})
    # One suite per requirement, so the coverage matrix has real buckets.
    assert {s["requirement_ref"] for s in spec["suites"]} == {"R1", "R2"}
    assert {s["id"] for s in spec["suites"]} == {"R1", "R2"}
    assert "auto" not in json.dumps(spec)


def test_a_near_miss_ref_is_repaired_not_persisted(canned_llm):
    from assay.generator.build import derive_intents
    llm = canned_llm([dict(GOOD_INTENTS[0], requirement_ref="req 1")])
    assert derive_intents(REQUIREMENTS, judge=llm)[0]["requirement_ref"] == "R1"


def test_an_unresolvable_ref_is_rejected(canned_llm):
    from assay.generator.build import IntentDerivationError, derive_intents
    llm = canned_llm([{"id": "x", "requirement_ref": "auto", "how": "template",
                       "template": "valid_json", "assertion": "zzz qqq"}])
    with pytest.raises(IntentDerivationError):
        derive_intents(REQUIREMENTS, judge=llm)


# ── derive_intents: malformed output is rejected, never degraded ─────────────

@pytest.mark.parametrize("reply", [
    "I'm sorry, I can't help with that.",          # no JSON at all
    "[{'id': 'x'},]",                              # not valid JSON
    '{"intents": []}',                             # JSON, but not a list
    "[]",                                          # empty
    '[{"id": "x", "requirement_ref": "R1"}]',      # no `how`
    '[{"id": "x", "requirement_ref": "R1", "how": "sorcery"}]',
    '[{"id": "x", "requirement_ref": "R1", "how": "template", "template": "nope"}]',
])
def test_malformed_model_output_is_rejected(reply, canned_llm):
    from assay.generator.build import IntentDerivationError, derive_intents
    with pytest.raises(IntentDerivationError):
        derive_intents(REQUIREMENTS, judge=canned_llm(text=reply))


def test_intent_ids_are_sanitised_before_they_become_paths(canned_llm):
    from assay.generator.build import derive_intents, intents_to_spec
    llm = canned_llm([{"id": "../../etc/passwd", "requirement_ref": "R1",
                       "how": "judge", "assertion": "a", "threshold": 0.8}])
    intents = derive_intents(REQUIREMENTS, judge=llm)
    assert ".." not in intents[0]["id"] and "/" not in intents[0]["id"]
    spec = intents_to_spec("p", intents, {"adapter": "mock"}, {})
    rubric = spec["suites"][0]["cases"][0]["checks"][0]["rubric"]
    assert rubric.startswith("generated/rubrics/") and ".." not in rubric


def test_no_judge_and_no_opt_in_raises():
    from assay.generator.build import derive_intents
    from assay.llm import LLMConfigError
    with pytest.raises(LLMConfigError):
        derive_intents(REQUIREMENTS)


def test_heuristic_is_opt_in_and_emits_real_refs():
    from assay.generator.build import derive_intents
    intents = derive_intents(REQUIREMENTS, allow_heuristic=True)
    assert intents
    assert {it["requirement_ref"] for it in intents} <= {"R1", "R2"}
    assert all(it["requirement_ref"] != "auto" for it in intents)


# ── the routes resolve and use the builder model ─────────────────────────────

def test_preview_calls_the_builder_model(client, canned_llm, install_builder_llm):
    llm = install_builder_llm(canned_llm(GOOD_INTENTS))
    resp = client.post("/pipelines/preview", json={"requirements": REQUIREMENTS})
    assert resp.status_code == 200
    assert llm.prompts, "the preview route must actually call the model"
    assert [c["id"] for c in resp.json()["checks"]] == ["json-shape", "no-pii", "not-fabricated"]


def test_generate_calls_the_builder_model_and_stores_refs(client, canned_llm, install_builder_llm):
    llm = install_builder_llm(canned_llm(GOOD_INTENTS))
    resp = client.post("/pipelines/generate", json={
        "project": "p1", "name": "p1-pipe", "requirements": REQUIREMENTS,
        "adapter_spec": {"adapter": "mock"},
    }, headers={"X-Assay-User": "tester"})
    assert resp.status_code == 200
    assert llm.prompts
    from assay.store import session_scope
    from assay.store.models import PipelineVersion
    with session_scope() as s:
        pv = s.get(PipelineVersion, resp.json()["pipeline_version_id"])
        assert {su["requirement_ref"] for su in pv.config["suites"]} == {"R1", "R2"}
        # A judge check is useless without its rubric materialised alongside it.
        assert "generated/rubrics/not-fabricated.yaml" in pv.rubrics


@pytest.mark.parametrize("route,payload", [
    ("/pipelines/preview", {"requirements": REQUIREMENTS}),
    ("/pipelines/generate", {"project": "p", "name": "n", "requirements": REQUIREMENTS,
                             "adapter_spec": {"adapter": "mock"}}),
])
def test_unconfigured_builder_returns_422_naming_the_env_var(client, route, payload):
    """No key anywhere: both routes must say which variable to set, not fall back."""
    from assay.store import session_scope
    from assay.store.models import WorkspaceSetting
    with session_scope() as s:
        for key, value in [("judge_adapter", "anthropic"),
                           ("judge_model", "claude-haiku-4-5-20251001")]:
            row = s.get(WorkspaceSetting, key)
            if row:
                row.value = value
            else:
                s.add(WorkspaceSetting(key=key, value=value))
    resp = client.post(route, json=payload, headers={"X-Assay-User": "tester"})
    assert resp.status_code == 422
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_malformed_model_output_surfaces_as_422(client, canned_llm, install_builder_llm):
    install_builder_llm(canned_llm(text="sorry, no."))
    resp = client.post("/pipelines/preview", json={"requirements": REQUIREMENTS})
    assert resp.status_code == 422
    assert resp.json()["detail"]


def test_provider_transport_failure_surfaces_as_422(client, canned_llm, install_builder_llm):
    class Exploding:
        name = "boom"

        def complete(self, messages, *, schema=None, tools=None, params=None):
            raise ConnectionError("connection reset by peer")

    install_builder_llm(Exploding())
    resp = client.post("/pipelines/preview", json={"requirements": REQUIREMENTS})
    assert resp.status_code == 422
    assert "connection reset" in resp.json()["detail"]


def test_no_pipeline_is_persisted_when_the_build_fails(client, canned_llm, install_builder_llm):
    install_builder_llm(canned_llm(text="nope"))
    resp = client.post("/pipelines/generate", json={
        "project": "ghost", "name": "ghost-pipe", "requirements": REQUIREMENTS,
        "adapter_spec": {"adapter": "mock"},
    }, headers={"X-Assay-User": "tester"})
    assert resp.status_code == 422
    from assay.store import session_scope
    from assay.store.models import Pipeline
    with session_scope() as s:
        assert s.query(Pipeline).filter_by(name="ghost-pipe").one_or_none() is None


# ── the wizard can show what went wrong ──────────────────────────────────────

def test_wizard_renders_a_danger_banner_for_build_failures():
    html = TEMPLATE.read_text()
    assert 'class="banner banner-danger"' in html
    assert 'x-show="buildError"' in html
    assert 'x-text="buildError"' in html


def test_wizard_checks_response_status_before_using_the_body():
    html = TEMPLATE.read_text()
    assert html.count("if (!r.ok)") >= 2, "preview and generate must both check r.ok"
    assert "errorText" in html


def test_wizard_stays_on_step_2_when_generate_fails():
    """The failure branch must return before `step = 3`, so the user can fix and retry."""
    html = TEMPLATE.read_text()
    generate = html.split("async generate()", 1)[1].split("async saveCheckParams", 1)[0]
    assert generate.index("this.buildError = await this.errorText(r); return;") < \
        generate.index("this.step = 3")
