"""Ollama native adapter for local models."""
from __future__ import annotations
import json
import time
import requests
from .base import ModelRequest, ModelResponse


class OllamaAdapter:
    name = "ollama"

    def __init__(self, *, model: str = "llama3", endpoint: str = "http://localhost:11434",
                 **_: object) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")

    def describe(self) -> dict:
        return {"adapter": self.name, "model": self.model, "endpoint": self.endpoint}

    def ping(self) -> dict:
        import time
        t0 = time.perf_counter()
        try:
            r = requests.get(f"{self.endpoint}/api/tags", timeout=5)
            return {"ok": True, "latency_ms": (time.perf_counter() - t0) * 1000, "error": None}
        except requests.RequestException as exc:
            return {
                "ok": False,
                "latency_ms": (time.perf_counter() - t0) * 1000,
                "error": f"{self.endpoint}: {exc}",
            }

    def invoke(self, req: ModelRequest) -> ModelResponse:
        prompt = req.input.get("prompt") or json.dumps(req.input)
        t0 = time.perf_counter()
        r = requests.post(f"{self.endpoint}/api/generate",
                          json={"model": self.model, "prompt": prompt, "stream": False},
                          timeout=req.params.get("timeout", 120))
        latency = (time.perf_counter() - t0) * 1000
        data = r.json()
        text = data.get("response")
        return ModelResponse(text=text, raw=data, json=_maybe_json(text or ""),
                             latency_ms=latency, status="ok" if r.ok else "error")

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        prompt = "\n".join(m.get("content", "") for m in messages)
        return self.invoke(ModelRequest(input={"prompt": prompt}, params=params or {}))


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
