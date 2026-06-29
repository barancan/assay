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


def test_connection(adapter_spec: dict) -> dict:
    """Instantiate adapter from a plain dict and ping it. Never raises; always returns a dict."""
    adapter_name = adapter_spec.get("adapter", "mock")
    entry = _ADAPTER_MAP.get(adapter_name)
    if entry is None:
        return {"ok": False, "latency_ms": None, "error": f"unknown adapter: {adapter_name!r}"}

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        kwargs = {k: v for k, v in adapter_spec.items() if k != "adapter" and v is not None}
        adapter = cls(**kwargs)
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "error": f"init failed: {exc}"}

    try:
        return adapter.ping()
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "error": str(exc)}
