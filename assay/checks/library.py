"""Vetted template checks. Each is a pure function of (response_dict, params)."""
from __future__ import annotations
import re
from typing import Any
from jsonpath_ng.ext import parse as jp_parse
import jsonschema


def _jsonpath(obj: Any, expr: str) -> list:
    try:
        return [m.value for m in jp_parse(expr).find(obj)]
    except Exception:
        return []


def valid_json(resp: dict, p: dict) -> dict:
    ok = resp.get("json") is not None
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": "response body is valid JSON" if ok else "response is not valid JSON"}


def json_schema(resp: dict, p: dict) -> dict:
    import json
    from pathlib import Path
    schema = p.get("schema")
    if schema is None and p.get("schema_ref"):
        schema = json.loads(Path(p["schema_ref"]).read_text())
    body = resp.get("json")
    if body is None:
        return {"passed": False, "severity": "fail", "message": "no JSON body to validate"}
    try:
        jsonschema.validate(body, schema)
        return {"passed": True, "message": "matches schema"}
    except jsonschema.ValidationError as e:
        return {"passed": False, "severity": "fail", "message": str(e.message),
                "evidence": {"path": list(e.absolute_path)}}


def contains(resp: dict, p: dict) -> dict:
    needle = p["value"]
    hay = resp.get("text") or ""
    ok = needle in hay
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"text {'contains' if ok else 'missing'}: {needle!r}"}


def not_contains(resp: dict, p: dict) -> dict:
    needle = p["value"]
    hay = resp.get("text") or ""
    ok = needle not in hay
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"forbidden string {'absent' if ok else 'present'}: {needle!r}"}


def regex_match(resp: dict, p: dict) -> dict:
    pat = re.compile(p["pattern"], re.MULTILINE | re.DOTALL)
    ok = bool(pat.search(resp.get("text") or ""))
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"pattern {'matched' if ok else 'no match'}: {p['pattern']!r}"}


def numeric_bound(resp: dict, p: dict) -> dict:
    vals = _jsonpath(resp.get("json"), p["field"])
    if not vals:
        return {"passed": False, "severity": "fail", "message": f"field not found: {p['field']}"}
    v = vals[0]
    lo, hi = p.get("min", float("-inf")), p.get("max", float("inf"))
    ok = lo <= v <= hi
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"{p['field']}={v} within [{lo},{hi}]" if ok else f"{p['field']}={v} out of bounds"}


def latency_bound(resp: dict, p: dict) -> dict:
    v = resp.get("latency_ms", 0.0)
    ok = v <= p["max_ms"]
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"latency {v:.0f}ms {'<=' if ok else '>'} {p['max_ms']}ms",
            "evidence": {"latency_ms": v}}


def field_present(resp: dict, p: dict) -> dict:
    vals = _jsonpath(resp.get("json"), p["field"])
    ok = len(vals) >= p.get("min", 1)
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"{len(vals)} match(es) for {p['field']}"}


def citation_present(resp: dict, p: dict) -> dict:
    vals = _jsonpath(resp.get("json"), p["field"])
    non_empty = [v for v in vals if v not in (None, "", [])]
    ok = len(non_empty) >= p.get("min", 1)
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"{len(non_empty)} citation(s) found at {p['field']}",
            "evidence": {"citations": non_empty[:5]}}


def refusal_detector(resp: dict, p: dict) -> dict:
    text = (resp.get("text") or "").lower()
    markers = p.get("markers", ["i can't", "i cannot", "i'm unable", "cannot help",
                                "not able to", "i won't"])
    refused = any(m in text for m in markers)
    want = p.get("expect_refusal", True)
    ok = refused == want
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": f"refusal={refused}, expected={want}"}


def pii_absent(resp: dict, p: dict) -> dict:
    text = resp.get("text") or ""
    patterns = {
        "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
        "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b",
        "card": r"\b(?:\d[ -]*?){13,16}\b",
    }
    hits = {k: re.findall(v, text) for k, v in patterns.items()}
    found = {k: v for k, v in hits.items() if v}
    ok = not found
    return {"passed": ok, "severity": "info" if ok else "fail",
            "message": "no PII detected" if ok else f"PII detected: {list(found)}",
            "evidence": found}


REGISTRY = {
    "valid_json": valid_json, "json_schema": json_schema, "contains": contains,
    "not_contains": not_contains, "regex_match": regex_match, "numeric_bound": numeric_bound,
    "latency_bound": latency_bound, "field_present": field_present,
    "citation_present": citation_present, "refusal_detector": refusal_detector,
    "pii_absent": pii_absent,
}
