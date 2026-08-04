"""Offline adapter: deterministic synthetic responses so the tool runs with no keys.

It echoes the input and, if the case input carries a `_mock_response` field,
returns it verbatim. Used by the example suite and the test harness.
"""
from __future__ import annotations
import json
import time
from ..pricing import estimate_cost, normalise_usage
from .base import ModelRequest, ModelResponse

# In-process and therefore genuinely free -- 0.0, never None. `estimate_cost` owns that
# call so "free" and "unpriced" cannot drift apart between adapters.
_USAGE = {"input_tokens": 0, "output_tokens": 0}


class MockAdapter:
    name = "mock"

    def __init__(self, **_: object) -> None:
        pass

    def describe(self) -> dict:
        return {"adapter": self.name, "capabilities": ["text", "json"]}

    def ping(self) -> dict:
        # In-process: always reachable, never has a credential to authenticate.
        return {"ok": True, "reachable": True, "authenticated": None,
                "latency_ms": 0.0, "error": None, "env_var": None}

    def invoke(self, req: ModelRequest) -> ModelResponse:
        t0 = time.perf_counter()
        canned = req.input.get("_mock_response")
        if canned is not None:
            body = canned
        else:
            body = {"echo": req.input}
        text = json.dumps(body)
        latency = (time.perf_counter() - t0) * 1000 + req.params.get("_mock_latency_ms", 5.0)
        return ModelResponse(
            text=text, raw=body, json=body if isinstance(body, dict) else None,
            latency_ms=latency, usage=normalise_usage(self.name, _USAGE),
            cost_usd=estimate_cost(self.name, None, _USAGE), status="ok",
        )


class MockJudge:
    name = "mock-judge"

    def __init__(self, **_: object) -> None:
        pass

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        # Deterministic neutral verdict so judge-typed checks run offline. With a
        # schema the shape follows the schema instead, so the structured path is
        # exercised without a provider.
        if schema:
            verdict = _from_schema(schema)
        else:
            verdict = {"scores": {}, "rationale": "mock judge: no model configured",
                       "evidence_quotes": []}
        return ModelResponse(text=json.dumps(verdict), raw=verdict, json=verdict, status="ok",
                             usage=normalise_usage(self.name, _USAGE),
                             cost_usd=estimate_cost(self.name, None, _USAGE))


def _from_schema(schema: dict) -> dict:
    """Smallest object that satisfies `schema`: every declared property, filled in."""
    props = schema.get("properties") or {}
    obj = {name: _placeholder(name, spec) for name, spec in props.items()}
    for name in schema.get("required") or []:
        obj.setdefault(name, f"mock {name}")
    return obj


def _placeholder(name: str, spec: dict):
    spec = spec if isinstance(spec, dict) else {}
    if spec.get("enum"):
        return spec["enum"][0]
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else "string"
    if kind == "object":
        return _from_schema(spec)
    if kind == "array":
        item = spec.get("items")
        return [_placeholder(name, item)] if isinstance(item, dict) else []
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return f"mock {name}"
