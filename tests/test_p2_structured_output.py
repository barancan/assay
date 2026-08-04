"""P2a: `schema=` is real -- the adapters force structure and parse it back.

Nothing here touches the network. `requests.post` is monkeypatched and the Anthropic
SDK is replaced with a fake module, so every assertion is made against the outbound
payload we would have sent and the reply we pretend to have received.
"""
from __future__ import annotations
import json
import sys
import types

import pytest
import requests

from assay.adapters.anthropic import STRUCTURED_TOOL, AnthropicAdapter
from assay.adapters.mock import MockJudge
from assay.adapters.ollama import OllamaAdapter
from assay.adapters.openai_compat import OpenAICompatAdapter

KEY = "sk-not-a-real-key"
SYSTEM_PROMPT = "You are a strict evaluation judge."
MESSAGES = [{"role": "user", "content": "score this"}]

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "object"},
        "rationale": {"type": "string"},
        "evidence_quotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "rationale"],
}

GOOD_VERDICT = {"scores": {"d1": 2}, "rationale": "clear and grounded",
                "evidence_quotes": ["a quote"]}


# ── fakes ───────────────────────────────────────────────────────────────────

class _FakeHTTP:
    """Records outbound POSTs and replays a queue of scripted replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.replies.pop(0) if self.replies else _Reply(200, {})

    @property
    def payloads(self):
        return [c["json"] for c in self.calls]


class _Reply:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = {} if payload is None else payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _chat(content):
    return {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 3}}


@pytest.fixture
def http(monkeypatch):
    def _make(*replies):
        fake = _FakeHTTP(replies)
        monkeypatch.setattr(requests, "post", fake.post)
        return fake
    return _make


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Install a stand-in `anthropic` module; the real SDK is not a test dependency."""
    state = {"blocks": [], "calls": []}

    class _Messages:
        def create(self, **kwargs):
            state["calls"].append(kwargs)
            msg = types.SimpleNamespace()
            msg.content = state["blocks"]
            msg.usage = types.SimpleNamespace(input_tokens=1, output_tokens=2)
            msg.model_dump = lambda: {"id": "msg_1"}
            return msg

    class _Anthropic:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    return state


def _tool_use(tool_input, name=STRUCTURED_TOOL):
    return types.SimpleNamespace(type="tool_use", id="tu_1", name=name, input=tool_input)


def _text(value):
    return types.SimpleNamespace(type="text", text=value)


# ── anthropic: forced tool use ──────────────────────────────────────────────

def test_anthropic_sends_the_schema_as_a_forced_tool(fake_anthropic):
    fake_anthropic["blocks"] = [_tool_use(GOOD_VERDICT)]
    AnthropicAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    sent = fake_anthropic["calls"][0]
    assert sent["tools"] == [{"name": STRUCTURED_TOOL,
                              "description": "Return the result as structured data.",
                              "input_schema": VERDICT_SCHEMA}]
    assert sent["tool_choice"] == {"type": "tool", "name": STRUCTURED_TOOL}


def test_anthropic_returns_the_tool_input_as_json(fake_anthropic):
    fake_anthropic["blocks"] = [_tool_use(GOOD_VERDICT)]
    out = AnthropicAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert out.status == "ok"
    assert out.json == GOOD_VERDICT
    assert out.tool_calls[0]["name"] == STRUCTURED_TOOL
    assert out.tool_calls[0]["input"] == GOOD_VERDICT
    assert out.raw == {"id": "msg_1"}


def test_anthropic_keeps_sending_the_system_prompt_with_a_schema(fake_anthropic):
    fake_anthropic["blocks"] = [_tool_use(GOOD_VERDICT)]
    AnthropicAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA,
                                         params={"system": SYSTEM_PROMPT})
    assert fake_anthropic["calls"][0]["system"] == SYSTEM_PROMPT


def test_anthropic_without_a_schema_is_unchanged(fake_anthropic):
    fake_anthropic["blocks"] = [_text('{"a": 1}')]
    out = AnthropicAdapter(model="m").complete(MESSAGES)

    sent = fake_anthropic["calls"][0]
    assert "tools" not in sent and "tool_choice" not in sent
    assert out.status == "ok"
    assert out.text == '{"a": 1}'
    assert out.json == {"a": 1}


def test_anthropic_passes_bare_tools_through_without_forcing(fake_anthropic):
    fake_anthropic["blocks"] = [_text("hi")]
    tools = [{"name": "lookup", "input_schema": {"type": "object"}}]
    AnthropicAdapter(model="m").complete(MESSAGES, tools=tools)

    sent = fake_anthropic["calls"][0]
    assert sent["tools"] == tools
    assert "tool_choice" not in sent


def test_anthropic_prose_instead_of_a_tool_call_is_an_error(fake_anthropic):
    """Forced tool use can still come back as text; that is a failure, not a verdict."""
    fake_anthropic["blocks"] = [_text("Sure! The score is probably a 2.")]
    out = AnthropicAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert out.status == "error"
    assert out.json is None
    assert "anthropic" in out.error


def test_anthropic_tool_input_that_misses_the_schema_is_an_error(fake_anthropic):
    fake_anthropic["blocks"] = [_tool_use({"rationale": "no scores key"})]
    out = AnthropicAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert out.status == "error"
    assert out.json is None
    assert "anthropic" in out.error
    assert out.tool_calls                       # kept for debugging


# ── openai_compat: json_schema response format ──────────────────────────────

def test_openai_compat_sends_a_json_schema_response_format(monkeypatch, http):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    fake = http(_Reply(200, _chat(json.dumps(GOOD_VERDICT))))
    out = OpenAICompatAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert fake.payloads[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "verdict", "schema": VERDICT_SCHEMA, "strict": True},
    }
    assert out.status == "ok"
    assert out.json == GOOD_VERDICT
    assert out.raw["_assay_structured_mode"] == "json_schema"


def test_openai_compat_still_sends_the_system_prompt_with_a_schema(monkeypatch, http):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    fake = http(_Reply(200, _chat(json.dumps(GOOD_VERDICT))))
    OpenAICompatAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA,
                                            params={"system": SYSTEM_PROMPT})
    assert fake.payloads[0]["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_openai_compat_falls_back_to_json_object_on_400(monkeypatch, http):
    """vLLM and older gateways 400 on json_schema; one retry in the weaker mode."""
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    rejection = _Reply(400, {"error": {"message": "response_format json_schema unsupported"}})
    fake = http(rejection, _Reply(200, _chat(json.dumps(GOOD_VERDICT))))
    out = OpenAICompatAdapter(model="m", endpoint="http://vllm:8000/v1").complete(
        MESSAGES, schema=VERDICT_SCHEMA)

    assert len(fake.calls) == 2
    assert fake.payloads[0]["response_format"]["type"] == "json_schema"
    assert fake.payloads[1]["response_format"] == {"type": "json_object"}
    assert out.status == "ok"
    assert out.json == GOOD_VERDICT
    assert out.raw["_assay_structured_mode"] == "json_object"
    assert "unsupported" in out.raw["_assay_structured_fallback_from"]


def test_openai_compat_retries_only_once(monkeypatch, http):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    fake = http(_Reply(400, {"error": {"message": "nope"}}),
                _Reply(400, {"error": {"message": "still nope"}}))
    out = OpenAICompatAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert len(fake.calls) == 2
    assert out.status == "error"
    assert out.json is None
    assert "400" in out.error


def test_openai_compat_does_not_retry_a_500(monkeypatch, http):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    fake = http(_Reply(500, {"error": "boom"}))
    out = OpenAICompatAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert len(fake.calls) == 1
    assert out.status == "error"


def test_openai_compat_prose_reply_under_a_schema_is_an_error(monkeypatch, http):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    http(_Reply(200, _chat("I think it deserves a 2 out of 3.")))
    out = OpenAICompatAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert out.status == "error"
    assert out.json is None
    assert "openai_compat" in out.error


def test_openai_compat_non_conforming_json_is_an_error(monkeypatch, http):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    http(_Reply(200, _chat(json.dumps({"rationale": "missing scores"}))))
    out = OpenAICompatAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert out.status == "error"
    assert out.json is None
    assert "openai_compat" in out.error
    assert out.text                              # the raw reply is still available


def test_openai_compat_fenced_json_is_recovered(monkeypatch, http):
    """Weaker json_object servers often wrap the object in a markdown fence."""
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    fenced = "```json\n" + json.dumps(GOOD_VERDICT) + "\n```"
    http(_Reply(200, _chat(fenced)))
    out = OpenAICompatAdapter(model="m").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert out.status == "ok"
    assert out.json == GOOD_VERDICT


def test_openai_compat_without_a_schema_is_unchanged(monkeypatch, http):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    fake = http(_Reply(200, _chat("just prose")))
    out = OpenAICompatAdapter(model="m").complete(MESSAGES)

    assert "response_format" not in fake.payloads[0]
    assert out.status == "ok"
    assert out.text == "just prose"
    assert out.json is None
    assert "_assay_structured_mode" not in out.raw


# ── ollama: the format field ────────────────────────────────────────────────

def test_ollama_sends_the_schema_as_format(http):
    fake = http(_Reply(200, {"response": json.dumps(GOOD_VERDICT)}))
    out = OllamaAdapter(model="llama3").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert fake.payloads[0]["format"] == VERDICT_SCHEMA
    assert out.status == "ok"
    assert out.json == GOOD_VERDICT
    assert out.raw["_assay_structured_mode"] == "schema"


def test_ollama_falls_back_to_plain_json_format(http):
    """Before ollama 0.5 `format` only accepted the string "json"."""
    fake = http(_Reply(400, {"error": "invalid format"}),
                _Reply(200, {"response": json.dumps(GOOD_VERDICT)}))
    out = OllamaAdapter(model="llama3").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert len(fake.calls) == 2
    assert fake.payloads[0]["format"] == VERDICT_SCHEMA     # the schema went out first
    assert fake.payloads[1]["format"] == "json"
    assert out.status == "ok"
    assert out.json == GOOD_VERDICT
    assert out.raw["_assay_structured_mode"] == "json"


def test_ollama_keeps_the_system_prompt_with_a_schema(http):
    fake = http(_Reply(200, {"response": json.dumps(GOOD_VERDICT)}))
    OllamaAdapter(model="llama3").complete(MESSAGES, schema=VERDICT_SCHEMA,
                                           params={"system": SYSTEM_PROMPT})
    assert fake.payloads[0]["system"] == SYSTEM_PROMPT


def test_ollama_non_conforming_output_is_an_error(http):
    http(_Reply(200, {"response": json.dumps(["not", "an", "object"])}))
    out = OllamaAdapter(model="llama3").complete(MESSAGES, schema=VERDICT_SCHEMA)

    assert out.status == "error"
    assert out.json is None
    assert "ollama" in out.error


def test_ollama_without_a_schema_is_unchanged(http):
    fake = http(_Reply(200, {"response": "just prose"}))
    out = OllamaAdapter(model="llama3").complete(MESSAGES)

    assert "format" not in fake.payloads[0]
    assert out.status == "ok"
    assert out.text == "just prose"
    assert out.json is None


# ── mock: the offline structured path ───────────────────────────────────────

def test_mock_judge_honours_the_schema():
    out = MockJudge().complete(MESSAGES, schema=VERDICT_SCHEMA)
    assert out.status == "ok"
    assert set(VERDICT_SCHEMA["required"]) <= set(out.json)
    assert isinstance(out.json["scores"], dict)
    assert isinstance(out.json["rationale"], str)


def test_mock_judge_output_validates_against_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "confidence": {"type": "number"},
            "hits": {"type": "integer"},
            "flagged": {"type": "boolean"},
            "spans": {"type": "array", "items": {"type": "string"}},
            "detail": {"type": "object", "properties": {"note": {"type": "string"}}},
        },
        "required": ["verdict", "confidence"],
    }
    out = MockJudge().complete(MESSAGES, schema=schema)
    jsonschema.validate(out.json, schema)
    assert out.json["verdict"] == "pass"          # first enum value, deterministically


def test_mock_judge_without_a_schema_is_unchanged():
    out = MockJudge().complete(MESSAGES)
    assert out.json == {"scores": {}, "rationale": "mock judge: no model configured",
                        "evidence_quotes": []}


# ── the shared parser ───────────────────────────────────────────────────────

def test_parse_structured_never_returns_both_an_object_and_an_error():
    from assay.adapters.base import parse_structured

    for value in (GOOD_VERDICT, json.dumps(GOOD_VERDICT), "prose", None, ["a"], 3):
        obj, err = parse_structured(value, VERDICT_SCHEMA, provider="p")
        assert (obj is None) != (err is None)
        assert obj is None or isinstance(obj, dict)


def test_parse_structured_names_the_provider():
    from assay.adapters.base import parse_structured

    _, err = parse_structured("not json at all", VERDICT_SCHEMA, provider="some_provider")
    assert "some_provider" in err


def test_parse_structured_accepts_anything_without_a_schema():
    from assay.adapters.base import parse_structured

    obj, err = parse_structured('{"anything": true}')
    assert err is None
    assert obj == {"anything": True}
