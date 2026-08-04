"""Ollama native adapter for local models."""
from __future__ import annotations
import json
import time
import requests
from ..llm.provider import key_env_for
from .base import ModelRequest, ModelResponse, parse_structured


class OllamaAdapter:
    name = "ollama"

    def __init__(self, *, model: str = "llama3", endpoint: str = "http://localhost:11434",
                 key_env: str | None = None, params: dict | None = None,
                 **_: object) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.key_env = key_env          # ollama needs no key, but a proxy in front might
        self.params = params or {}

    def describe(self) -> dict:
        return {"adapter": self.name, "model": self.model, "endpoint": self.endpoint}

    def ping(self) -> dict:
        env_var = key_env_for(self.name, self.key_env)
        t0 = time.perf_counter()
        try:
            requests.get(f"{self.endpoint}/api/tags", timeout=5)
            # Local models need no credential, so authentication is not a question here.
            return {"ok": True, "reachable": True, "authenticated": None,
                    "latency_ms": (time.perf_counter() - t0) * 1000,
                    "error": None, "env_var": env_var}
        except requests.RequestException as exc:
            return {
                "ok": False,
                "reachable": False,
                "authenticated": None,
                "latency_ms": (time.perf_counter() - t0) * 1000,
                "error": f"{self.endpoint}: {exc}",
                "env_var": env_var,
            }

    def invoke(self, req: ModelRequest) -> ModelResponse:
        prompt = req.input.get("prompt") or json.dumps(req.input)
        params = {**self.params, **req.params}
        schema = req.metadata.get("schema")
        timeout = params.get("timeout", 120)
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        # The judge sends its instructions as params["system"]; ollama has a field for it.
        if params.get("system"):
            payload["system"] = params["system"]
        if schema:
            payload["format"] = schema
        t0 = time.perf_counter()
        r = requests.post(f"{self.endpoint}/api/generate", json=payload, timeout=timeout)
        mode = "schema" if schema else None
        # Schema-valued `format` landed in ollama 0.5; before that the field only
        # accepted the string "json" and anything else is rejected outright.
        if schema and r.status_code == 400:
            retry = {**payload, "format": "json"}
            r = requests.post(f"{self.endpoint}/api/generate", json=retry, timeout=timeout)
            mode = "json"
        latency = (time.perf_counter() - t0) * 1000
        data = r.json()
        raw = dict(data) if isinstance(data, dict) else {"body": data}
        if mode:
            raw["_assay_structured_mode"] = mode
        text = raw.get("response")
        if not schema:
            return ModelResponse(text=text, raw=raw, json=_maybe_json(text or ""),
                                 latency_ms=latency, status="ok" if r.ok else "error")
        if not r.ok:
            return ModelResponse(text=text, raw=raw, latency_ms=latency, status="error",
                                 error=f"ollama request failed (HTTP {r.status_code})")
        parsed, err = parse_structured(text, schema, provider="ollama")
        return ModelResponse(text=text, raw=raw, json=parsed, latency_ms=latency,
                             status="error" if err else "ok", error=err)

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        prompt = "\n".join(m.get("content", "") for m in messages)
        return self.invoke(ModelRequest(input={"prompt": prompt}, params=params or {},
                                        metadata={"schema": schema, "tools": tools}))


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
