"""OpenAI-compatible Chat Completions adapter (OpenAI, vLLM, LM Studio, OpenRouter...)."""
from __future__ import annotations
import json
import os
import time
import requests
from .base import ModelRequest, ModelResponse


class OpenAICompatAdapter:
    name = "openai_compat"

    def __init__(self, *, model: str = "gpt-4o-mini",
                 endpoint: str = "https://api.openai.com/v1", **_: object) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")

    def describe(self) -> dict:
        return {"adapter": self.name, "model": self.model, "endpoint": self.endpoint}

    def ping(self) -> dict:
        import time
        t0 = time.perf_counter()
        try:
            key = os.environ.get("OPENAI_API_KEY", "")
            r = requests.get(f"{self.endpoint}/models",
                             headers={"Authorization": f"Bearer {key}"},
                             timeout=5)
            # Any HTTP response (even 401) means the server is reachable.
            return {"ok": True, "latency_ms": (time.perf_counter() - t0) * 1000, "error": None}
        except requests.RequestException as exc:
            return {
                "ok": False,
                "latency_ms": (time.perf_counter() - t0) * 1000,
                "error": f"{self.endpoint}: {exc}",
            }

    def _messages(self, req: ModelRequest):
        if "messages" in req.input:
            return req.input["messages"]
        return [{"role": "user", "content": req.input.get("prompt") or json.dumps(req.input)}]

    def invoke(self, req: ModelRequest) -> ModelResponse:
        key = os.environ.get("OPENAI_API_KEY", "")
        t0 = time.perf_counter()
        r = requests.post(f"{self.endpoint}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": self.model, "messages": self._messages(req),
                                "temperature": req.params.get("temperature", 0.0)},
                          timeout=req.params.get("timeout", 60))
        latency = (time.perf_counter() - t0) * 1000
        data = r.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content")
        return ModelResponse(text=text, raw=data, json=_maybe_json(text or ""),
                             latency_ms=latency, usage=data.get("usage", {}),
                             status="ok" if r.ok else "error")

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        return self.invoke(ModelRequest(input={"messages": messages}, params=params or {}))


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
