"""Adapter contracts shared by every target and judge."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


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
        """Return {"ok": bool, "latency_ms": float | None, "error": str | None}."""
        ...


@runtime_checkable
class JudgeProvider(Protocol):
    name: str
    def complete(self, messages: list[dict], *, schema: dict | None = None,
                 tools: list | None = None, params: dict | None = None) -> ModelResponse: ...
