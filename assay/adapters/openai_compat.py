"""OpenAI-compatible Chat Completions adapter (OpenAI, vLLM, LM Studio, OpenRouter...)."""
from __future__ import annotations
import json
import os
import time
import requests
from ..llm.provider import key_env_for, read_key
from ..pricing import estimate_cost, normalise_usage
from .base import ModelRequest, ModelResponse, parse_structured


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
        # key_env="" means a keyless local server, so no Authorization header at all.
        key = read_key(self.name, self.key_env)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        params = {**self.params, **req.params}
        schema = req.metadata.get("schema")
        timeout = params.get("timeout", 60)
        payload = {"model": self.model, "messages": self._messages(req),
                   "temperature": params.get("temperature", 0.0)}
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": schema, "strict": True},
            }
        elif req.metadata.get("tools"):
            payload["tools"] = req.metadata["tools"]

        t0 = time.perf_counter()
        r = requests.post(f"{self.endpoint}/chat/completions", headers=headers,
                          json=payload, timeout=timeout)
        mode = "json_schema" if schema else None
        fallback_from = None
        # vLLM and older gateways answer 400 to a json_schema response_format they do
        # not implement. json_object is the widely supported weaker form; retry once.
        if schema and r.status_code == 400:
            fallback_from = _error_text(r)
            retry = {**payload, "response_format": {"type": "json_object"}}
            r = requests.post(f"{self.endpoint}/chat/completions", headers=headers,
                              json=retry, timeout=timeout)
            mode = "json_object"
        latency = (time.perf_counter() - t0) * 1000

        data = r.json()
        raw = dict(data) if isinstance(data, dict) else {"body": data}
        if mode:
            # Left in raw so a human debugging a weird verdict can see which form
            # of structured output the server actually accepted.
            raw["_assay_structured_mode"] = mode
            if fallback_from:
                raw["_assay_structured_fallback_from"] = fallback_from[:300]
        text = _content(raw)
        reported = raw.get("usage") or {}
        # Normalised here so every caller reads one shape; the server's own payload
        # survives untouched in `raw`. cost_usd stays None for a model we cannot price,
        # which is the common case behind a self-hosted or gateway endpoint.
        usage = normalise_usage(self.name, reported)
        cost = estimate_cost(self.name, self.model, reported)
        if not schema:
            return ModelResponse(text=text, raw=raw, json=_maybe_json(text or ""),
                                 latency_ms=latency, usage=usage, cost_usd=cost,
                                 status="ok" if r.ok else "error")
        if not r.ok:
            return ModelResponse(text=text, raw=raw, latency_ms=latency,
                                 usage=usage, cost_usd=cost, status="error",
                                 error=f"openai_compat request failed (HTTP {r.status_code})")
        parsed, err = parse_structured(text, schema, provider="openai_compat")
        return ModelResponse(text=text, raw=raw, json=parsed, latency_ms=latency,
                             usage=usage, cost_usd=cost,
                             status="error" if err else "ok", error=err)

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        return self.invoke(ModelRequest(input={"messages": messages}, params=params or {},
                                        metadata={"schema": schema, "tools": tools}))


def _content(data: dict):
    choices = data.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") or {}
    return message.get("content") if isinstance(message, dict) else None


def _error_text(r) -> str:
    try:
        body = r.json()
    except ValueError:
        return str(getattr(r, "text", ""))[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err or body)


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
