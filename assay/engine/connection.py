"""Adapter connection testing — instantiates from a plain dict spec, calls ping()."""
from __future__ import annotations
import importlib

_ADAPTER_MAP = {
    "mock":         ("assay.adapters.mock",         "MockAdapter"),
    "anthropic":    ("assay.adapters.anthropic",    "AnthropicAdapter"),
    "openai_compat":("assay.adapters.openai_compat","OpenAICompatAdapter"),
    "ollama":       ("assay.adapters.ollama",       "OllamaAdapter"),
    "rest":         ("assay.adapters.rest",         "RestAdapter"),
}

# Every result carries the full ping contract, so callers can read any key.
_BLANK = {"ok": False, "reachable": False, "authenticated": None,
          "latency_ms": None, "error": None, "env_var": None}


def _result(**fields) -> dict:
    return {**_BLANK, **fields}


def test_connection(adapter_spec: dict) -> dict:
    """Instantiate adapter from a plain dict and ping it. Never raises; always returns a dict."""
    adapter_name = adapter_spec.get("adapter", "mock")
    entry = _ADAPTER_MAP.get(adapter_name)
    if entry is None:
        return _result(error=f"unknown adapter: {adapter_name!r}")

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        kwargs = {k: v for k, v in adapter_spec.items() if k != "adapter" and v is not None}
        adapter = cls(**kwargs)
    except Exception as exc:
        return _result(error=f"init failed: {exc}")

    try:
        return _result(**adapter.ping())
    except Exception as exc:
        env_var = getattr(exc, "env_var", None)
        return _result(error=str(exc), env_var=env_var,
                       authenticated=False if env_var else None)
