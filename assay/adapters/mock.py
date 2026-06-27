"""Offline adapter: deterministic synthetic responses so the tool runs with no keys.

It echoes the input and, if the case input carries a `_mock_response` field,
returns it verbatim. Used by the example suite and the test harness.
"""
from __future__ import annotations
import json
import time
from .base import ModelRequest, ModelResponse


class MockAdapter:
    name = "mock"

    def __init__(self, **_: object) -> None:
        pass

    def describe(self) -> dict:
        return {"adapter": self.name, "capabilities": ["text", "json"]}

    def ping(self) -> dict:
        return {"ok": True, "latency_ms": 0.0, "error": None}

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
            latency_ms=latency, usage={"input_tokens": 0, "output_tokens": 0},
            cost_usd=0.0, status="ok",
        )


class MockJudge:
    name = "mock-judge"

    def __init__(self, **_: object) -> None:
        pass

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        # Deterministic neutral verdict so judge-typed checks run offline.
        verdict = {"scores": {}, "rationale": "mock judge: no model configured",
                   "evidence_quotes": []}
        return ModelResponse(text=json.dumps(verdict), json=verdict, status="ok",
                             usage={"input_tokens": 0, "output_tokens": 0}, cost_usd=0.0)
