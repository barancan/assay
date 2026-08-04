"""The target's interface, parsed into something the builder can ground on.

Without this the builder is guessing. It knows a target exists but not what a request
looks like, what fields come back, or what a plausible input is -- so generated checks
cannot reference real response paths and generated cases have empty inputs.

`Interface` is the contract between parsing (which format did the user give us) and
consumption (case generation, codegen, and the prompts that carry response shape).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Interface:
    """A normalised view of whatever interface description the user supplied."""

    kind: str = "unknown"                       # postman | openapi | mcp | unknown
    request_template: dict = field(default_factory=dict)
    input_fields: list[str] = field(default_factory=list)
    response_schema: dict | None = None
    response_paths: list[str] = field(default_factory=list)
    auth: dict = field(default_factory=dict)
    hash: str = ""

    def is_grounded(self) -> bool:
        """True when there is enough here to reference real fields."""
        return bool(self.input_fields or self.response_paths or self.response_schema)


def interface_hash(payload: Any) -> str:
    """Stable short hash of an interface description, for TargetModel.interface_hash."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def parse_interface(path: str | None) -> Interface:
    """Parse a Postman collection, OpenAPI document, or MCP descriptor.

    Returns an empty (ungrounded) Interface when `path` is None, so callers can treat
    "no interface supplied" as an ordinary case rather than an error.
    """
    if not path:
        return Interface()
    raw = Path(path).read_text()
    try:
        doc = json.loads(raw)
    except ValueError:
        return Interface(kind="unknown", hash=interface_hash(raw))
    if "item" in doc and "info" in doc:
        return _from_postman(doc)
    return Interface(kind="unknown", hash=interface_hash(doc))


def _from_postman(doc: dict) -> Interface:
    """Minimal Postman read: first request's shape. Extended by the grounding work."""
    items = doc.get("item") or []
    request = (items[0].get("request") or {}) if items else {}
    url = request.get("url")
    url = url.get("raw") if isinstance(url, dict) else url
    body = (request.get("body") or {}).get("raw")
    input_fields: list[str] = []
    if body:
        try:
            input_fields = sorted(json.loads(body).keys())
        except (ValueError, AttributeError):
            input_fields = []
    return Interface(
        kind="postman",
        request_template={"method": request.get("method", "POST"), "url": url},
        input_fields=input_fields,
        auth=request.get("auth") or {},
        hash=interface_hash(doc),
    )


def sample_response(iface: Interface) -> dict:
    """A plausible response for `iface`, for dry-running a generated check.

    Never a network call. Shape only -- values are placeholders.
    """
    if iface.response_schema:
        return _sample_from_schema(iface.response_schema)
    return {"text": "sample response", "json": None}


def _sample_from_schema(schema: dict) -> Any:
    kind = schema.get("type")
    if kind == "object":
        return {k: _sample_from_schema(v) for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [_sample_from_schema(schema.get("items") or {})]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    return "sample"


def describe_for_prompt(iface: Interface) -> str:
    """Render the interface for an LLM prompt, or "" when there is nothing to say."""
    if not iface.is_grounded():
        return ""
    lines = [f"TARGET INTERFACE ({iface.kind}):"]
    if iface.input_fields:
        lines.append(f"Request fields: {', '.join(iface.input_fields)}")
    if iface.response_paths:
        lines.append(f"Response paths: {', '.join(iface.response_paths)}")
    if iface.response_schema:
        lines.append(f"Response schema: {json.dumps(iface.response_schema)[:1500]}")
    return "\n".join(lines)
