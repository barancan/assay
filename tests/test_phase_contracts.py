"""Frozen contracts for the judging and grounding work.

Four workstreams build against these in parallel:

  * structured output (adapters)  ->  judging (judges/rubrics)
  * interface grounding (parsing) ->  case generation

Each pair meets exactly here. These tests pin the seams; the implementations on either
side may change freely as long as these keep passing.
"""
from __future__ import annotations

import json

import pytest

from assay.adapters.base import ModelResponse
from assay.generator.interface import (
    Interface,
    describe_for_prompt,
    interface_hash,
    parse_interface,
    sample_response,
)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "object"},
        "rationale": {"type": "string"},
        "evidence_quotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "rationale"],
}


# ── Seam 1: structured output ───────────────────────────────────────────────
#
# When a caller passes `schema=`, an adapter must return the parsed object in
# ModelResponse.json. The judge relies on this instead of scraping prose for JSON.

def test_model_response_carries_a_parsed_object():
    resp = ModelResponse(text='{"scores": {"a": 2}}', json={"scores": {"a": 2}})
    assert resp.json["scores"]["a"] == 2
    assert resp.as_dict()["json"] == {"scores": {"a": 2}}


def test_structured_response_survives_the_dict_view():
    """Checks receive as_dict(); a structured verdict must not be lost on the way."""
    resp = ModelResponse(text=None, json={"scores": {}, "rationale": "ok"})
    assert resp.as_dict()["json"]["rationale"] == "ok"


@pytest.mark.parametrize("adapter_name", ["anthropic", "openai_compat", "ollama"])
def test_every_real_judge_accepts_the_schema_keyword(adapter_name):
    """complete() must accept schema=/tools=/params= -- the judge always passes them."""
    import inspect
    from assay.adapters.registry import _JUDGES

    sig = inspect.signature(_JUDGES[adapter_name].complete)
    for name in ("schema", "tools", "params"):
        assert name in sig.parameters, f"{adapter_name}.complete has no '{name}'"
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_verdict_schema_shape_is_stable():
    """The judge's output contract. Rubric generation targets these keys."""
    assert set(VERDICT_SCHEMA["properties"]) == {"scores", "rationale", "evidence_quotes"}
    assert VERDICT_SCHEMA["required"] == ["scores", "rationale"]


# ── Seam 2: the parsed interface ────────────────────────────────────────────

def test_no_interface_is_an_ordinary_case():
    iface = parse_interface(None)
    assert isinstance(iface, Interface)
    assert iface.is_grounded() is False
    assert describe_for_prompt(iface) == ""


def test_postman_collection_yields_request_shape(tmp_path):
    collection = {
        "info": {"name": "c"},
        "item": [{
            "name": "analyse",
            "request": {
                "method": "POST",
                "url": {"raw": "https://api.example.com/analyse"},
                "body": {"mode": "raw", "raw": json.dumps({"text": "x", "locale": "en"})},
            },
        }],
    }
    path = tmp_path / "c.postman_collection.json"
    path.write_text(json.dumps(collection))

    iface = parse_interface(str(path))
    assert iface.kind == "postman"
    assert iface.input_fields == ["locale", "text"]
    assert iface.request_template["method"] == "POST"
    assert iface.is_grounded() is True
    assert iface.hash


def test_interface_hash_is_stable_and_order_independent():
    assert interface_hash({"a": 1, "b": 2}) == interface_hash({"b": 2, "a": 1})
    assert interface_hash({"a": 1}) != interface_hash({"a": 2})


def test_sample_response_follows_the_schema():
    """Codegen dry-runs a generated check against this -- never a network call."""
    iface = Interface(response_schema={
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": {
                "type": "object",
                "properties": {"article": {"type": "string"}, "severity": {"type": "integer"}},
            }},
            "ok": {"type": "boolean"},
        },
    })
    sample = sample_response(iface)
    assert isinstance(sample["findings"], list)
    assert set(sample["findings"][0]) == {"article", "severity"}
    assert sample["ok"] is True


def test_sample_response_without_a_schema_is_still_usable():
    sample = sample_response(Interface())
    assert "text" in sample


def test_describe_for_prompt_mentions_real_fields():
    iface = Interface(kind="postman", input_fields=["text"], response_paths=["$.findings[*]"])
    described = describe_for_prompt(iface)
    assert "text" in described
    assert "$.findings[*]" in described
