"""Anthropic Messages adapter (target or judge). Requires `pip install anthropic`."""
from __future__ import annotations
import json
import time
from ..llm.provider import LLMConfigError, credential_status, key_env_for, read_key
from .base import ModelRequest, ModelResponse, parse_structured

# Anthropic has no response-format switch: the way to force a shape is to declare a
# single tool and require it. The name is arbitrary but must match in tool_choice.
STRUCTURED_TOOL = "emit_verdict"


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, *, model: str = "claude-opus-4-8", key_env: str | None = None,
                 params: dict | None = None, **_: object) -> None:
        self.model = model
        self.key_env = key_env          # variable NAME; the value is never stored
        self.params = params or {}

    def _client(self):
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("pip install anthropic to use the anthropic adapter") from e
        # Read the value here and nowhere else, and pass it explicitly -- the SDK's
        # implicit ANTHROPIC_API_KEY read is what makes per-target keys impossible.
        key = read_key(self.name, self.key_env)
        return anthropic.Anthropic(api_key=key)

    def describe(self) -> dict:
        return {"adapter": self.name, "model": self.model,
                "key_env": key_env_for(self.name, self.key_env)}

    def ping(self) -> dict:
        env_var = key_env_for(self.name, self.key_env)
        status = credential_status(self.name, self.key_env)
        if status["requires_key"] and not status["configured"]:
            # No key at all: nothing was contacted, so reachability is unknown.
            # Checked before the SDK import so the message names the variable.
            return {"ok": False, "reachable": False, "authenticated": False,
                    "latency_ms": None, "error": f"{env_var} is not set",
                    "env_var": env_var}
        t0 = time.perf_counter()
        try:
            client = self._client()
        except LLMConfigError as exc:
            return {"ok": False, "reachable": False, "authenticated": False,
                    "latency_ms": None, "error": str(exc), "env_var": env_var}
        except RuntimeError as exc:
            # SDK not installed -- a local problem, not an unreachable service.
            return {"ok": False, "reachable": False, "authenticated": None,
                    "latency_ms": None, "error": str(exc), "env_var": env_var}
        try:
            client.models.list()
            return {"ok": True, "reachable": True, "authenticated": True,
                    "latency_ms": (time.perf_counter() - t0) * 1000,
                    "error": None, "env_var": env_var}
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000
            if _is_auth_error(exc):
                # Reached the service; it rejected the credential.
                return {"ok": False, "reachable": True, "authenticated": False,
                        "latency_ms": latency,
                        "error": f"credential rejected: check ${env_var}",
                        "env_var": env_var}
            return {"ok": False, "reachable": False, "authenticated": None,
                    "latency_ms": latency, "error": str(exc), "env_var": env_var}

    def _messages(self, req: ModelRequest) -> list[dict]:
        if "messages" in req.input:
            return req.input["messages"]
        prompt = req.input.get("prompt") or json.dumps(req.input)
        return [{"role": "user", "content": prompt}]

    def invoke(self, req: ModelRequest) -> ModelResponse:
        client = self._client()
        params = {**self.params, **req.params}
        schema = req.metadata.get("schema")
        kwargs = {
            "model": self.model,
            "max_tokens": params.get("max_tokens", 1024),
            "temperature": params.get("temperature", 0.0),
            "messages": self._messages(req),
        }
        # The judge sends its instructions as params["system"]; Anthropic takes it
        # top-level, not as a message.
        if params.get("system"):
            kwargs["system"] = params["system"]
        if schema:
            kwargs["tools"] = [{"name": STRUCTURED_TOOL,
                                "description": "Return the result as structured data.",
                                "input_schema": schema}]
            kwargs["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL}
        elif req.metadata.get("tools"):
            kwargs["tools"] = req.metadata["tools"]
        t0 = time.perf_counter()
        msg = client.messages.create(**kwargs)
        latency = (time.perf_counter() - t0) * 1000
        blocks = list(getattr(msg, "content", None) or [])
        text = "".join(b.text for b in blocks if getattr(b, "type", "") == "text")
        tool_calls = [{"id": getattr(b, "id", None), "name": getattr(b, "name", None),
                       "input": getattr(b, "input", None)}
                      for b in blocks if getattr(b, "type", "") == "tool_use"] or None
        usage = {"input_tokens": msg.usage.input_tokens,
                 "output_tokens": msg.usage.output_tokens}
        raw = msg.model_dump()
        if not schema:
            return ModelResponse(text=text, raw=raw, json=_maybe_json(text),
                                 tool_calls=tool_calls, latency_ms=latency, usage=usage)
        # Structured mode: the tool input is the answer. Falling back to the text
        # block would defeat the point of forcing the tool in the first place.
        if not tool_calls:
            return ModelResponse(
                text=text, raw=raw, latency_ms=latency, usage=usage, status="error",
                error="anthropic answered with text, not the forced structured tool call")
        parsed, err = parse_structured(tool_calls[0]["input"], schema, provider="anthropic")
        if err:
            return ModelResponse(text=text, raw=raw, tool_calls=tool_calls, latency_ms=latency,
                                 usage=usage, status="error", error=err)
        return ModelResponse(text=text or json.dumps(parsed), raw=raw, json=parsed,
                             tool_calls=tool_calls, latency_ms=latency, usage=usage)

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        return self.invoke(ModelRequest(input={"messages": messages}, params=params or {},
                                        metadata={"schema": schema, "tools": tools}))


def _is_auth_error(exc: Exception) -> bool:
    """Did the service answer and reject us, rather than never answer at all?"""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return True
    err = str(exc).lower()
    return any(k in err for k in ("auth", "api_key", "api key", "permission", "credit"))


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
