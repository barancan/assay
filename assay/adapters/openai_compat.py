"""OpenAI-compatible Chat Completions adapter (OpenAI, vLLM, LM Studio, OpenRouter...)."""
from __future__ import annotations
import json
import os
import time
import requests
from ..llm.provider import key_env_for, read_key
from .base import ModelRequest, ModelResponse


class OpenAICompatAdapter:
    name = "openai_compat"

    def __init__(self, *, model: str = "gpt-4o-mini",
                 endpoint: str = "https://api.openai.com/v1",
                 key_env: str | None = None, params: dict | None = None,
                 **_: object) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.key_env = key_env          # variable NAME; the value is never stored
        self.params = params or {}

    def describe(self) -> dict:
        return {"adapter": self.name, "model": self.model, "endpoint": self.endpoint,
                "key_env": key_env_for(self.name, self.key_env)}

    def ping(self) -> dict:
        env_var = key_env_for(self.name, self.key_env)
        key = os.environ.get(env_var, "") if env_var else ""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        t0 = time.perf_counter()
        try:
            r = requests.get(f"{self.endpoint}/models", headers=headers, timeout=5)
        except requests.RequestException as exc:
            # Never answered: whether the credential works is not determinable.
            return {"ok": False, "reachable": False, "authenticated": None,
                    "latency_ms": (time.perf_counter() - t0) * 1000,
                    "error": f"{self.endpoint}: {exc}", "env_var": env_var}
        latency = (time.perf_counter() - t0) * 1000
        if env_var and not key:
            return {"ok": False, "reachable": True, "authenticated": False,
                    "latency_ms": latency,
                    "error": f"{env_var} is not set", "env_var": env_var}
        if r.status_code in (401, 403):
            return {"ok": False, "reachable": True, "authenticated": False,
                    "latency_ms": latency,
                    "error": f"credential rejected (HTTP {r.status_code}): check ${env_var}",
                    "env_var": env_var}
        # Any other HTTP response means the server is reachable; some compatible
        # servers do not implement /models, so a non-2xx there proves nothing.
        return {"ok": True, "reachable": True,
                "authenticated": True if r.ok else None,
                "latency_ms": latency, "error": None, "env_var": env_var}

    def _messages(self, req: ModelRequest):
        if "messages" in req.input:
            messages = list(req.input["messages"])
        else:
            messages = [{"role": "user",
                         "content": req.input.get("prompt") or json.dumps(req.input)}]
        params = {**self.params, **req.params}
        # The judge sends its instructions as params["system"]; this dialect wants
        # them as a leading system message.
        system = params.get("system")
        if system and not (messages and messages[0].get("role") == "system"):
            messages = [{"role": "system", "content": system}] + messages
        return messages

    def invoke(self, req: ModelRequest) -> ModelResponse:
        # Raises LLMConfigError naming the variable rather than sending an empty bearer.
        key = read_key(self.name, self.key_env)
        params = {**self.params, **req.params}
        t0 = time.perf_counter()
        r = requests.post(f"{self.endpoint}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": self.model, "messages": self._messages(req),
                                "temperature": params.get("temperature", 0.0)},
                          timeout=params.get("timeout", 60))
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
