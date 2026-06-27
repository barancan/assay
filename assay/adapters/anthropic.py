"""Anthropic Messages adapter (target or judge). Requires `pip install anthropic`."""
from __future__ import annotations
import json
import time
from .base import ModelRequest, ModelResponse


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, *, model: str = "claude-opus-4-8", **_: object) -> None:
        self.model = model

    def _client(self):
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("pip install anthropic to use the anthropic adapter") from e
        return anthropic.Anthropic()

    def describe(self) -> dict:
        return {"adapter": self.name, "model": self.model}

    def ping(self) -> dict:
        import time
        t0 = time.perf_counter()
        try:
            self._client().models.list()
            return {"ok": True, "latency_ms": (time.perf_counter() - t0) * 1000, "error": None}
        except Exception as exc:
            # Authentication errors still mean the service is reachable.
            err = str(exc)
            reachable = any(k in err.lower() for k in ("auth", "api_key", "permission", "status"))
            return {
                "ok": reachable,
                "latency_ms": (time.perf_counter() - t0) * 1000,
                "error": None if reachable else err,
            }

    def _messages(self, req: ModelRequest) -> list[dict]:
        if "messages" in req.input:
            return req.input["messages"]
        prompt = req.input.get("prompt") or json.dumps(req.input)
        return [{"role": "user", "content": prompt}]

    def invoke(self, req: ModelRequest) -> ModelResponse:
        client = self._client()
        t0 = time.perf_counter()
        msg = client.messages.create(
            model=self.model, max_tokens=req.params.get("max_tokens", 1024),
            temperature=req.params.get("temperature", 0.0), messages=self._messages(req))
        latency = (time.perf_counter() - t0) * 1000
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        parsed = _maybe_json(text)
        return ModelResponse(text=text, raw=msg.model_dump(), json=parsed, latency_ms=latency,
                             usage={"input_tokens": msg.usage.input_tokens,
                                    "output_tokens": msg.usage.output_tokens})

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        return self.invoke(ModelRequest(input={"messages": messages}, params=params or {}))


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
