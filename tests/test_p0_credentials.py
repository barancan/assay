"""P0: credentials reach the adapters, and the UI tells the truth about them.

Nothing here touches the network. `requests` is monkeypatched and the Anthropic
SDK is replaced with a fake module, so every assertion is made against the
outbound payload we would have sent.
"""
from __future__ import annotations
import sys
import types
import pytest
import requests

from assay.adapters.anthropic import AnthropicAdapter
from assay.adapters.mock import MockAdapter
from assay.adapters.ollama import OllamaAdapter
from assay.adapters.openai_compat import OpenAICompatAdapter
from assay.adapters.rest import RestAdapter
from assay.llm.provider import LLMConfigError
from assay.spec.models import JudgeSpec, TargetSpec

SYSTEM_PROMPT = "You are a strict evaluation judge."
SECRET = "sk-do-not-leak-me"


# ── fakes ───────────────────────────────────────────────────────────────────

class _Sink:
    """Records what an adapter tried to send."""

    def __init__(self):
        self.client_kwargs = []
        self.calls = []


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "{}"}}], "response": "{}", "usage": {},
        }

    def json(self):
        return self._payload


@pytest.fixture
def sink():
    return _Sink()


@pytest.fixture
def fake_anthropic(monkeypatch, sink):
    """Install a stand-in `anthropic` module; the real SDK is not a test dependency."""
    state = {"error": None}

    class _Messages:
        def create(self, **kwargs):
            sink.calls.append(kwargs)
            msg = types.SimpleNamespace()
            msg.content = [types.SimpleNamespace(type="text", text="{}")]
            msg.usage = types.SimpleNamespace(input_tokens=1, output_tokens=2)
            msg.model_dump = lambda: {}
            return msg

    class _Models:
        def list(self):
            if state["error"] is not None:
                raise state["error"]
            return []

    class _Anthropic:
        def __init__(self, **kwargs):
            sink.client_kwargs.append(kwargs)
            self.messages = _Messages()
            self.models = _Models()

    module = types.ModuleType("anthropic")
    module.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return state


@pytest.fixture
def capture_post(monkeypatch, sink):
    def _post(url, **kwargs):
        sink.calls.append({"url": url, **kwargs})
        return _FakeResponse()
    monkeypatch.setattr(requests, "post", _post)
    return sink


@pytest.fixture
def no_network(monkeypatch):
    """Any HTTP call at all is a test failure."""
    def _boom(*a, **k):
        raise AssertionError(f"network call attempted: {a} {k}")
    for verb in ("get", "post", "head", "request"):
        monkeypatch.setattr(requests, verb, _boom)


# ── key_env survives the spec and reaches the adapter ───────────────────────

def test_target_spec_keeps_key_env():
    assert TargetSpec(adapter="openai_compat", key_env="MY_KEY").key_env == "MY_KEY"


def test_judge_spec_keeps_endpoint_and_key_env():
    j = JudgeSpec(provider="openai_compat", model="m",
                  endpoint="http://vllm:8000/v1", key_env="VLLM_KEY")
    assert (j.endpoint, j.key_env) == ("http://vllm:8000/v1", "VLLM_KEY")


def test_spec_rejects_an_undeclared_field():
    """The failure mode that lost key_env in the first place is now loud."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TargetSpec(adapter="mock", keyenv="TYPO")


def test_judge_provider_receives_endpoint_key_env_and_params():
    from assay.adapters.registry import get_judge_provider
    provider = get_judge_provider(JudgeSpec(
        provider="openai_compat", model="local", endpoint="http://vllm:8000/v1",
        key_env="VLLM_KEY", params={"temperature": 0.3}))
    assert provider.endpoint == "http://vllm:8000/v1"
    assert provider.key_env == "VLLM_KEY"
    assert provider.params == {"temperature": 0.3}


def test_target_adapter_receives_key_env():
    from assay.adapters.registry import get_target_adapter
    adapter = get_target_adapter(TargetSpec(adapter="anthropic", model="m",
                                            key_env="SECOND_ANTHROPIC_KEY"))
    assert adapter.key_env == "SECOND_ANTHROPIC_KEY"


def test_anthropic_reads_the_named_variable(monkeypatch, fake_anthropic, sink):
    monkeypatch.setenv("SECOND_ANTHROPIC_KEY", SECRET)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    AnthropicAdapter(model="m", key_env="SECOND_ANTHROPIC_KEY").complete(
        [{"role": "user", "content": "hi"}])
    assert sink.client_kwargs == [{"api_key": SECRET}]


def test_anthropic_never_stores_the_key_value(monkeypatch, fake_anthropic):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    adapter = AnthropicAdapter(model="m")
    adapter.complete([{"role": "user", "content": "hi"}])
    assert SECRET not in repr(vars(adapter))
    assert SECRET not in repr(adapter.describe())


def test_openai_compat_reads_the_named_variable(monkeypatch, capture_post):
    monkeypatch.setenv("VLLM_KEY", SECRET)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    OpenAICompatAdapter(model="local", endpoint="http://vllm:8000/v1",
                        key_env="VLLM_KEY").complete([{"role": "user", "content": "hi"}])
    assert capture_post.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert capture_post.calls[0]["url"].startswith("http://vllm:8000/v1")


# ── a missing key fails loudly instead of sending an empty bearer ───────────

def test_openai_compat_missing_key_raises_and_sends_nothing(monkeypatch, no_network):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMConfigError) as exc:
        OpenAICompatAdapter(model="gpt-4o-mini").complete([{"role": "user", "content": "hi"}])
    assert exc.value.env_var == "OPENAI_API_KEY"
    assert "OPENAI_API_KEY" in str(exc.value)


def test_openai_compat_missing_custom_key_names_that_variable(monkeypatch, no_network):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)   # the default is set; the override is not
    monkeypatch.delenv("VLLM_KEY", raising=False)
    with pytest.raises(LLMConfigError) as exc:
        OpenAICompatAdapter(model="local", key_env="VLLM_KEY").complete(
            [{"role": "user", "content": "hi"}])
    assert exc.value.env_var == "VLLM_KEY"


def test_anthropic_missing_key_raises_before_constructing_a_client(
        monkeypatch, fake_anthropic, sink):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMConfigError) as exc:
        AnthropicAdapter(model="m").complete([{"role": "user", "content": "hi"}])
    assert exc.value.env_var == "ANTHROPIC_API_KEY"
    assert sink.client_kwargs == []


def test_rest_missing_bearer_token_raises_and_sends_nothing(monkeypatch, no_network):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    adapter = RestAdapter(endpoint="http://example.invalid/v1",
                          auth={"type": "bearer", "token_env": "MY_TOKEN"})
    from assay.adapters.base import ModelRequest
    with pytest.raises(LLMConfigError) as exc:
        adapter.invoke(ModelRequest(input={"prompt": "hi"}))
    assert exc.value.env_var == "MY_TOKEN"


# ── ping() distinguishes unauthenticated from unreachable ───────────────────

def _ping_with_get(monkeypatch, response_or_error):
    def _get(url, **kwargs):
        if isinstance(response_or_error, Exception):
            raise response_or_error
        return response_or_error
    monkeypatch.setattr(requests, "get", _get)


def test_ping_contract_keys_present():
    for key in ("ok", "reachable", "authenticated", "latency_ms", "error", "env_var"):
        assert key in MockAdapter().ping()


def test_openai_compat_ping_unauthorised_is_not_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _ping_with_get(monkeypatch, _FakeResponse(status_code=401))
    result = OpenAICompatAdapter().ping()
    assert result["ok"] is False
    assert result["reachable"] is True
    assert result["authenticated"] is False
    assert "OPENAI_API_KEY" in result["error"]


def test_openai_compat_ping_missing_key_names_the_variable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _ping_with_get(monkeypatch, _FakeResponse(status_code=200))
    result = OpenAICompatAdapter().ping()
    assert result["ok"] is False
    assert result["authenticated"] is False
    assert result["env_var"] == "OPENAI_API_KEY"
    assert SECRET not in str(result)


def test_openai_compat_ping_unreachable_cannot_judge_authentication(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _ping_with_get(monkeypatch, requests.RequestException("connection refused"))
    result = OpenAICompatAdapter(endpoint="http://localhost:39999/v1").ping()
    assert result["ok"] is False
    assert result["reachable"] is False
    assert result["authenticated"] is None       # never answered — not determinable
    assert "39999" in result["error"]


def test_openai_compat_ping_ok_when_authenticated(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _ping_with_get(monkeypatch, _FakeResponse(status_code=200))
    result = OpenAICompatAdapter().ping()
    assert result["ok"] is True
    assert result["authenticated"] is True


def test_anthropic_ping_without_a_key_is_not_connected(monkeypatch, fake_anthropic, sink):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = AnthropicAdapter().ping()
    assert result["ok"] is False
    assert result["authenticated"] is False
    assert result["env_var"] == "ANTHROPIC_API_KEY"
    assert sink.client_kwargs == []


def test_anthropic_ping_rejected_credential_is_reachable(monkeypatch, fake_anthropic):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    err = RuntimeError("authentication_error")
    err.status_code = 401
    fake_anthropic["error"] = err
    result = AnthropicAdapter().ping()
    assert result["ok"] is False
    assert result["reachable"] is True
    assert result["authenticated"] is False
    assert SECRET not in str(result)


def test_anthropic_ping_ok_with_a_key(monkeypatch, fake_anthropic):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    result = AnthropicAdapter().ping()
    assert result["ok"] is True
    assert result["authenticated"] is True


def test_engine_connection_always_returns_the_full_contract():
    from assay.engine.connection import test_connection
    result = test_connection({"adapter": "does_not_exist"})
    assert result["ok"] is False
    for key in ("reachable", "authenticated", "latency_ms", "error", "env_var"):
        assert key in result


def test_run_gate_blames_the_credential_not_the_network(monkeypatch, fake_anthropic):
    """execute_run's pre-flight must name the variable, not report 'unreachable'."""
    from assay.adapters.registry import test_connection as _tc
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConnectionError) as exc:
        _tc(AnthropicAdapter())
    assert "ANTHROPIC_API_KEY" in str(exc.value)


# ── the judge's system prompt actually goes out ─────────────────────────────

def test_anthropic_sends_the_system_prompt(monkeypatch, fake_anthropic, sink):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    AnthropicAdapter(model="m").complete([{"role": "user", "content": "hi"}],
                                         params={"system": SYSTEM_PROMPT})
    assert sink.calls[0]["system"] == SYSTEM_PROMPT


def test_openai_compat_sends_the_system_prompt(monkeypatch, capture_post):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    OpenAICompatAdapter(model="m").complete([{"role": "user", "content": "hi"}],
                                            params={"system": SYSTEM_PROMPT})
    messages = capture_post.calls[0]["json"]["messages"]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1]["role"] == "user"


def test_ollama_sends_the_system_prompt(capture_post):
    OllamaAdapter(model="llama3").complete([{"role": "user", "content": "hi"}],
                                           params={"system": SYSTEM_PROMPT})
    assert capture_post.calls[0]["json"]["system"] == SYSTEM_PROMPT


def test_judge_check_reaches_the_provider_with_its_instructions(monkeypatch, capture_post, tmp_path):
    """End to end through run_judge_check, the caller that was losing the prompt."""
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    from assay.judges.judge import run_judge_check
    rubric = tmp_path / "r.yaml"
    rubric.write_text('dimensions:\n  - {id: d1, question: "good?", scale: {1: bad, 2: good}}\n')
    run_judge_check(OpenAICompatAdapter(model="m"), str(rubric),
                    {"text": "answer"}, {"input": {}})
    assert capture_post.calls[0]["json"]["messages"][0]["role"] == "system"


# ── Settings → Providers card and builder settings ──────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    assay.store.db.init_db()
    import assay.server.app as _mod
    importlib.reload(_mod)
    from fastapi.testclient import TestClient
    return TestClient(_mod.app, follow_redirects=False)


def test_providers_card_shows_configured(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    body = client.get("/settings").text
    assert "Providers" in body
    assert "ANTHROPIC_API_KEY" in body
    assert "Configured" in body


def test_providers_card_shows_not_configured(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = client.get("/settings").text
    assert "Not configured" in body
    assert "OPENAI_API_KEY" in body


def test_providers_card_never_renders_a_key_value(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    assert SECRET not in client.get("/settings").text


def test_builder_settings_round_trip(client):
    resp = client.post("/settings/builder",
                       json={"builder_adapter": "ollama", "builder_model": "llama3"})
    assert resp.status_code == 200
    assert client.get("/settings/builder").json() == {
        "builder_adapter": "ollama", "builder_model": "llama3"}
    assert "llama3" in client.get("/settings").text


def test_builder_defaults_to_the_judge_model_without_a_seed_row(client):
    client.post("/settings/judge",
                json={"judge_adapter": "ollama", "judge_model": "mistral"})
    assert client.get("/settings/builder").json()["builder_model"] == "mistral"


def test_default_judge_model_is_offered_by_the_model_selector(client):
    """The seeded default must not render as 'Custom…'."""
    assert "claude-haiku-4-5-20251001" in client.get("/settings").text


def test_connection_test_badge_warns_when_the_key_is_missing(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.post("/connection-test", json={"adapter": "anthropic"})
    assert resp.status_code == 200
    assert "badge-pass" not in resp.text          # never a green "Connected"
    assert "badge-warning" in resp.text
    assert "ANTHROPIC_API_KEY" in resp.text
