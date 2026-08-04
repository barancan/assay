"""Adapter contracts shared by every target and judge."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_DECODER = json.JSONDecoder()


@dataclass
class ModelRequest:
    input: dict[str, Any]                      # {messages|prompt|http_body|fields}
    params: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)   # full payload, always captured
    json: dict[str, Any] | None = None                  # parsed body if JSON
    tool_calls: list[Any] | None = None
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    status: str = "ok"                                  # ok | error | timeout
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict view handed to (untrusted) checks. Data only, no methods."""
        return {
            "text": self.text, "raw": self.raw, "json": self.json,
            "tool_calls": self.tool_calls, "latency_ms": self.latency_ms,
            "usage": self.usage, "cost_usd": self.cost_usd,
            "status": self.status, "error": self.error,
        }


@runtime_checkable
class TargetAdapter(Protocol):
    name: str
    def describe(self) -> dict[str, Any]: ...
    def invoke(self, req: ModelRequest) -> ModelResponse: ...
    def ping(self) -> dict[str, Any]:
        """Report reachability and, where it can be determined, credential validity.

        Returns {"ok": bool, "reachable": bool, "authenticated": bool | None,
                 "latency_ms": float | None, "error": str | None, "env_var": str | None}.
        `authenticated` is None when the probe cannot tell (no credential is needed,
        or the server never answered). `env_var` is a variable NAME, never a value.
        """
        ...


@runtime_checkable
class JudgeProvider(Protocol):
    name: str
    def complete(self, messages: list[dict], *, schema: dict | None = None,
                 tools: list | None = None, params: dict | None = None) -> ModelResponse:
        """Complete a chat turn, optionally forcing the reply into a JSON schema.

        Passing `schema` puts the provider into structured mode -- forced tool use on
        Anthropic, a json_schema response format on OpenAI-compatible servers, the
        `format` field on ollama -- and guarantees one of two outcomes: either
        `status == "ok"` and `ModelResponse.json` is a dict that validates against the
        schema (when `jsonschema` is installed), or `status == "error"` with `error`
        naming the provider and `json` left as None. A caller that asked for a schema
        never receives prose, partial JSON, or an unvalidated object in `json`. Without
        `schema` nothing changes: `json` is a best-effort parse of the reply text.
        """
        ...


def parse_structured(value: Any, schema: dict | None = None, *,
                     provider: str = "model") -> tuple[dict | None, str | None]:
    """Coerce a provider's structured reply into (object, error) -- never both.

    `value` may already be a dict (a tool-use input), a JSON string, or prose with
    JSON buried in it. The error message names `provider` so a failure points at the
    model that produced it rather than at the adapter that asked.
    """
    obj = _loads_lenient(value) if isinstance(value, str) else value
    if not isinstance(obj, dict):
        return None, f"{provider} did not return a JSON object for the requested schema"
    if schema:
        problem = _schema_error(obj, schema)
        if problem:
            return None, f"{provider} returned JSON that does not match the schema: {problem}"
    return obj, None


def _loads_lenient(text: str):
    """Plain JSON first, then the first balanced object inside a fence or prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        try:
            return _DECODER.raw_decode(text[start:])[0]
        except ValueError:
            start = text.find("{", start + 1)
    return None


def _schema_error(obj: dict, schema: dict) -> str | None:
    try:
        import jsonschema
    except ImportError:          # optional at runtime; validation is best effort
        return None
    try:
        jsonschema.validate(obj, schema)
    except jsonschema.ValidationError as exc:
        return exc.message
    except jsonschema.SchemaError:
        return None              # our own schema is malformed -- not the provider's fault
    return None
