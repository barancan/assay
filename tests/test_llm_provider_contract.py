"""The provider-resolution contract that the builder and the UI both depend on.

These tests pin the shared interface. P0 (credential plumbing) and P1 (real LLM in the
build paths) are developed against it in parallel, so it must not drift.
"""
from __future__ import annotations

import pytest

from assay.llm import (
    DEFAULT_KEY_ENV,
    LLMConfigError,
    credential_status,
    key_env_for,
    resolve_llm,
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


# ── key_env_for ─────────────────────────────────────────────────────────────

def test_default_key_env_per_adapter():
    assert key_env_for("anthropic") == "ANTHROPIC_API_KEY"
    assert key_env_for("openai_compat") == "OPENAI_API_KEY"


def test_local_adapters_need_no_key():
    assert key_env_for("ollama") is None
    assert key_env_for("mock") is None


def test_explicit_key_env_wins():
    assert key_env_for("openai_compat", "MY_OTHER_KEY") == "MY_OTHER_KEY"


# ── credential_status ───────────────────────────────────────────────────────

def test_status_reports_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status = credential_status("anthropic")
    assert status["configured"] is False
    assert status["requires_key"] is True
    assert status["env_var"] == "ANTHROPIC_API_KEY"


def test_status_reports_present_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert credential_status("anthropic")["configured"] is True


def test_status_never_leaks_the_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    assert "sk-secret-value" not in repr(credential_status("anthropic"))


def test_status_flags_unknown_adapter():
    status = credential_status("not-a-provider")
    assert status["known"] is False


def test_keyless_adapter_is_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert credential_status("ollama")["configured"] is True


# ── resolve_llm ─────────────────────────────────────────────────────────────

def test_resolve_raises_named_error_when_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMConfigError) as excinfo:
        resolve_llm("anthropic", "claude-haiku-4-5-20251001")
    assert excinfo.value.env_var == "ANTHROPIC_API_KEY"
    assert excinfo.value.adapter == "anthropic"
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_resolve_raises_on_unknown_adapter():
    with pytest.raises(LLMConfigError) as excinfo:
        resolve_llm("not-a-provider", "x")
    assert excinfo.value.adapter == "not-a-provider"


def test_resolve_returns_a_provider_when_configured(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    provider = resolve_llm("anthropic", "claude-haiku-4-5-20251001")
    assert hasattr(provider, "complete")
    assert provider.model == "claude-haiku-4-5-20251001"


def test_resolve_never_falls_back_to_mock(monkeypatch):
    """The whole point of P0: an unconfigured provider fails loudly."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        resolve_llm("anthropic")


# ── builder resolution ──────────────────────────────────────────────────────

def _set(key: str, value: str) -> None:
    from assay.store.db import session_scope
    from assay.store.models import WorkspaceSetting
    with session_scope() as s:
        row = s.get(WorkspaceSetting, key)
        if row:
            row.value = value
        else:
            s.add(WorkspaceSetting(key=key, value=value))


def test_builder_falls_back_to_judge_settings():
    from assay.llm.provider import builder_choice
    _set("judge_adapter", "ollama")
    _set("judge_model", "llama3")
    assert builder_choice() == ("ollama", "llama3")


def test_explicit_builder_settings_win():
    from assay.llm.provider import builder_choice
    _set("judge_adapter", "ollama")
    _set("judge_model", "llama3")
    _set("builder_adapter", "anthropic")
    _set("builder_model", "claude-opus-4-8")
    assert builder_choice() == ("anthropic", "claude-opus-4-8")


def test_resolve_builder_llm_raises_when_unconfigured(monkeypatch):
    from assay.llm import resolve_builder_llm
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _set("judge_adapter", "anthropic")
    _set("judge_model", "claude-haiku-4-5-20251001")
    with pytest.raises(LLMConfigError) as excinfo:
        resolve_builder_llm()
    assert excinfo.value.env_var == "ANTHROPIC_API_KEY"


def test_every_registered_judge_adapter_has_a_key_env_entry():
    """A new adapter must declare its credential requirement here."""
    from assay.adapters.registry import _JUDGES
    for name in _JUDGES:
        assert name in DEFAULT_KEY_ENV, (
            f"judge adapter '{name}' has no DEFAULT_KEY_ENV entry"
        )


# ── keyless local endpoints ─────────────────────────────────────────────────

def test_empty_key_env_means_no_credential():
    """An explicit "" opts a local OpenAI-compatible server out of auth entirely."""
    assert key_env_for("openai_compat", "") is None
    status = credential_status("openai_compat", "")
    assert status["requires_key"] is False
    assert status["configured"] is True


def test_empty_key_env_reaches_the_adapter(monkeypatch):
    """The "" must survive resolution -- dropping it silently restores the default."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = resolve_llm("openai_compat", "local-model", key_env="")
    assert provider.key_env == ""
    assert provider.describe()["key_env"] is None


def test_keyless_local_endpoint_sends_no_auth_header(monkeypatch):
    """A local vLLM must not be forced to invent an API key."""
    import assay.adapters.openai_compat as oc
    from assay.adapters.base import ModelRequest

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured = {}

    class _Resp:
        ok = True

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(oc.requests, "post", _fake_post)
    adapter = oc.OpenAICompatAdapter(model="local-model",
                                     endpoint="http://localhost:8000/v1", key_env="")
    adapter.invoke(ModelRequest(input={"prompt": "hi"}))
    assert "Authorization" not in captured["headers"]


def test_none_key_env_still_uses_the_adapter_default():
    assert key_env_for("openai_compat", None) == "OPENAI_API_KEY"
