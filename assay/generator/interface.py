"""The target's interface, parsed into something the builder can ground on.

Without this the builder is guessing. It knows a target exists but not what a request
looks like, what fields come back, or what a plausible input is -- so generated checks
cannot reference real response paths and generated cases have empty inputs.

`Interface` is the contract between parsing (which format did the user give us) and
consumption (case generation, codegen, and the prompts that carry response shape).

This module is also the single Postman/OpenAPI reader: `adapters/rest.py` builds its
request template from the same functions, so the adapter and the builder cannot drift
into disagreeing about what a collection says.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml

_VAR = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")
_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
# Schemas can be recursive ($ref back to an ancestor); every walk is depth-bounded.
_MAX_DEPTH = 8
_FORMAT_SAMPLES = {
    "date-time": "2024-01-01T00:00:00Z",
    "date": "2024-01-01",
    "time": "00:00:00",
    "email": "user@example.com",
    "uri": "https://example.com",
    "uuid": "00000000-0000-4000-8000-000000000000",
}


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
    name: str = ""                              # the request/operation/tool we parsed

    def is_grounded(self) -> bool:
        """True when there is enough here to reference real fields."""
        return bool(self.input_fields or self.response_paths or self.response_schema)


def interface_hash(payload: Any) -> str:
    """Stable short hash of an interface description, for TargetModel.interface_hash."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


# ── loading and format detection ────────────────────────────────────────────

def load_document(raw: str) -> Any:
    """Parse JSON or YAML. Returns None when the text is neither.

    JSON first because it is the common case and the stricter parser; YAML second so an
    OpenAPI `.yaml` works. Scalars (a plain sentence parses as a YAML string) are not
    documents, so they come back as None.
    """
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, (dict, list)) else None


def detect_format(doc: Any) -> str:
    """postman | openapi | mcp | unknown, decided by content -- users mislabel files."""
    if isinstance(doc, dict):
        if doc.get("openapi") or doc.get("swagger"):
            return "openapi"
        if isinstance(doc.get("item"), list) and ("info" in doc or _postman_entries(doc)):
            return "postman"
    if mcp_tools(doc):
        return "mcp"
    return "unknown"


def parse_interface(path: str | None, *, request: str | None = None) -> Interface:
    """Parse a Postman collection, OpenAPI document, or MCP descriptor.

    Returns an empty (ungrounded) Interface when `path` is None, so callers can treat
    "no interface supplied" as an ordinary case rather than an error. Anything that fails
    to parse is ungrounded too: a bad file must not take the build down.
    """
    if not path:
        return Interface()
    try:
        raw = Path(path).read_text()
    except OSError:
        return Interface()
    doc = load_document(raw)
    if doc is None:
        return Interface(kind="unknown", hash=interface_hash(raw))
    kind = detect_format(doc)
    builders = {"postman": _from_postman, "openapi": _from_openapi, "mcp": _from_mcp}
    build = builders.get(kind)
    if build is None:
        return Interface(kind="unknown", hash=interface_hash(doc))
    try:
        return build(doc, request)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        # We recognised the format but could not read this document. Say which format it
        # was and stay ungrounded rather than failing the build.
        return Interface(kind=kind, hash=interface_hash(doc))


def interface_from_target(target: Any) -> Interface:
    """Parse the interface a target spec (or its dict form) points at."""
    if target is None:
        return Interface()
    get = target.get if isinstance(target, dict) else (lambda k, d=None: getattr(target, k, d))
    path = get("import") or get("import_")
    return parse_interface(path, request=get("request"))


# ── Postman ─────────────────────────────────────────────────────────────────

def _postman_entries(doc: dict) -> list[dict]:
    """Flatten a collection's nested `item` folders into leaf requests."""
    entries: list[dict] = []

    def walk(items: Any, trail: list[str]) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if isinstance(item.get("item"), list):
                walk(item["item"], trail + [name])
            elif isinstance(item.get("request"), (dict, str)):
                req = item["request"]
                req = {"url": req, "method": "GET"} if isinstance(req, str) else req
                entries.append({"name": name, "path": " / ".join(trail + [name]),
                                "request": req})

    walk(doc.get("item"), [])
    return entries


def _select(entries: list[dict], name: str | None,
            kind: str = "request", where: str = "collection",
            prefer: Any = None) -> dict:
    """Pick a named entry, or the best default. Matches leaf name then folder path."""
    if not entries:
        raise ValueError(f"{where} contains no {kind}s")
    if name is None:
        return next((e for e in entries if prefer and prefer(e)), entries[0])
    for key in ("name", "path"):
        for entry in entries:
            if entry.get(key) == name:
                return entry
        for entry in entries:
            if str(entry.get(key, "")).lower() == name.lower():
                return entry
    raise ValueError(f"{kind} '{name}' not found in {where}")


def _postman_url(url: Any) -> str:
    if isinstance(url, str):
        return url
    if not isinstance(url, dict):
        return ""
    if url.get("raw"):
        return str(url["raw"])
    host = url.get("host")
    host = ".".join(str(h) for h in host) if isinstance(host, list) else str(host or "")
    segments = url.get("path")
    tail = "/".join(str(p) for p in segments) if isinstance(segments, list) else str(segments or "")
    out = f"{url.get('protocol') or 'https'}://{host}"
    if tail:
        out += "/" + tail.lstrip("/")
    query = [f"{q.get('key')}={q.get('value', '')}" for q in url.get("query") or []
             if isinstance(q, dict) and q.get("key") and not q.get("disabled")]
    return out + ("?" + "&".join(query) if query else "")


def _query_keys(url: str) -> list[str]:
    return [k for k, _ in parse_qsl(urlsplit(url).query)]


def _postman_headers(request: dict) -> dict:
    headers = request.get("header") or []
    if isinstance(headers, str):
        return {}
    return {h["key"]: h.get("value", "") for h in headers
            if isinstance(h, dict) and h.get("key") and not h.get("disabled")}


def _postman_auth(block: Any) -> dict:
    """Normalise Postman's `{"type": "bearer", "bearer": [{key, value}]}` shape."""
    if not isinstance(block, dict) or not block.get("type"):
        return {}
    kind = str(block["type"])
    params = block.get(kind)
    values: dict = {}
    if isinstance(params, list):
        values = {p["key"]: p.get("value") for p in params
                  if isinstance(p, dict) and p.get("key")}
    elif isinstance(params, dict):
        values = dict(params)
    return {"type": kind, **({"params": values} if values else {})}


def _postman_body(request: dict) -> tuple[str | None, list[str]]:
    """The raw body to send, and the field names it carries."""
    body = request.get("body") or {}
    if not isinstance(body, dict):
        return None, []
    mode = body.get("mode")
    if mode == "raw":
        raw = body.get("raw")
        if not isinstance(raw, str):
            return None, []
        try:
            parsed = json.loads(raw)
        except ValueError:
            # A templated body ({{payload}}) is still a body; its variables are inputs.
            return raw, sorted(set(_VAR.findall(raw)))
        return raw, sorted(parsed.keys()) if isinstance(parsed, dict) else []
    if mode in ("urlencoded", "formdata"):
        items = [i for i in body.get(mode) or []
                 if isinstance(i, dict) and i.get("key") and not i.get("disabled")]
        fields = sorted({str(i["key"]) for i in items})
        if mode == "urlencoded":
            return "&".join(f"{i['key']}={i.get('value', '')}" for i in items), fields
        return None, fields
    return None, []


def _sends_a_body(entry: dict) -> bool:
    return bool((entry["request"].get("body") or {}).get("mode"))


def _pick_postman(doc: dict, name: str | None) -> dict:
    # Unnamed: a collection usually opens with a health check, but the request worth
    # binding a target to is the one that sends a body.
    return _select(_postman_entries(doc), name, prefer=_sends_a_body)


def _postman_template(doc: dict, entry: dict) -> dict:
    request = entry["request"]
    url = _postman_url(request.get("url"))
    body, _ = _postman_body(request)
    auth = _postman_auth(request.get("auth")) or _postman_auth(doc.get("auth"))
    variables = {str(v["key"]): v.get("value") for v in doc.get("variable") or []
                 if isinstance(v, dict) and v.get("key")}
    return {
        "method": str(request.get("method") or "POST").upper(),
        "url": url,
        "headers": _postman_headers(request),
        "body": body,
        "auth": auth,
        "variables": variables,
        "name": entry["name"],
    }


def postman_request(doc: dict, name: str | None = None) -> dict:
    """The request template `adapters/rest.py` sends, read from a collection."""
    return _postman_template(doc, _pick_postman(doc, name))


def _from_postman(doc: dict, name: str | None = None) -> Interface:
    entry = _pick_postman(doc, name)
    template = _postman_template(doc, entry)
    _, body_fields = _postman_body(entry["request"])
    fields = set(body_fields) | set(_query_keys(template["url"]))
    # Collection variables are configuration ({{base_url}}), not per-case inputs.
    fields |= set(_VAR.findall(template["url"])) - set(template["variables"])
    return Interface(
        kind="postman",
        request_template={k: v for k, v in template.items() if k != "auth"},
        input_fields=sorted(fields),
        auth=template["auth"],
        hash=interface_hash(doc),
        name=template["name"],
    )


# ── OpenAPI ─────────────────────────────────────────────────────────────────

def _pointer(root: Any, ref: str) -> Any:
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, list) and part.isdigit():
            node = node[int(part)]
        else:
            return None
    return node


def resolve_refs(node: Any, root: Any, _seen: tuple = ()) -> Any:
    """Inline local `$ref`s. External refs are left alone rather than fetched."""
    if isinstance(node, list):
        return [resolve_refs(v, root, _seen) for v in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/") or ref in _seen:
            return node          # external, or a cycle -- neither is worth chasing
        target = _pointer(root, ref)
        if target is None:
            return node
        resolved = resolve_refs(target, root, _seen + (ref,))
        siblings = {k: resolve_refs(v, root, _seen) for k, v in node.items() if k != "$ref"}
        return {**resolved, **siblings} if isinstance(resolved, dict) else resolved
    return {k: resolve_refs(v, root, _seen) for k, v in node.items()}


def openapi_operations(doc: dict) -> list[dict]:
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return []
    out: list[dict] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            out.append({
                "path": path,
                "method": method.upper(),
                "operation": operation,
                "parameters": list(shared) + list(operation.get("parameters") or []),
                "name": operation.get("operationId") or f"{method.upper()} {path}",
                "id": f"{method.upper()} {path}",
            })
    return out


def _select_operation(operations: list[dict], name: str | None) -> dict:
    if not operations:
        raise ValueError("document declares no operations")
    if name is None:
        # A POST is what an eval target almost always is; fall back to whatever exists.
        return next((o for o in operations if o["method"] == "POST"), operations[0])
    for entry in operations:
        if name in (entry["name"], entry["id"], entry["path"]):
            return entry
    lowered = name.lower()
    for entry in operations:
        if lowered in (entry["name"].lower(), entry["id"].lower()):
            return entry
    raise ValueError(f"operation '{name}' not found in document")


def _server_url(doc: dict) -> str:
    servers = doc.get("servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], dict):
        return ""
    url = str(servers[0].get("url") or "")
    for key, spec in (servers[0].get("variables") or {}).items():
        if isinstance(spec, dict) and spec.get("default") is not None:
            url = url.replace("{" + key + "}", str(spec["default"]))
    return url.rstrip("/")


def _json_content(container: Any) -> dict | None:
    """The JSON media-type schema out of a requestBody/response `content` map."""
    content = (container or {}).get("content") if isinstance(container, dict) else None
    if not isinstance(content, dict) or not content:
        return None
    key = next((k for k in content if k == "application/json"), None)
    key = key or next((k for k in content if "json" in k), None) or next(iter(content))
    media = content.get(key)
    schema = media.get("schema") if isinstance(media, dict) else None
    return schema if isinstance(schema, dict) else None


def _success_response(operation: dict) -> Any:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    codes = sorted(k for k in responses if str(k).isdigit() and 200 <= int(k) < 300)
    for key in codes + ["2XX", "default"]:
        if key in responses:
            return responses[key]
    return None


def openapi_request(doc: dict, name: str | None = None) -> dict:
    """The request template `adapters/rest.py` sends, read from an OpenAPI document."""
    return _openapi_template(doc, _select_operation(openapi_operations(doc), name))


def _openapi_template(doc: dict, entry: dict) -> dict:
    headers = {}
    if _json_content(entry["operation"].get("requestBody")):
        headers["Content-Type"] = "application/json"
    return {
        "method": entry["method"],
        "url": _server_url(doc) + entry["path"],
        "headers": headers,
        # No body template: the adapter serialises the case input, which is exactly the
        # request body schema. A synthesised template would only fight it.
        "body": None,
        "auth": _openapi_auth(doc, entry["operation"]),
        "variables": {},
        "name": entry["name"],
    }


def _openapi_auth(doc: dict, operation: dict) -> dict:
    security = operation.get("security")
    if not isinstance(security, list):
        security = doc.get("security")
    if not isinstance(security, list) or not security:
        return {}
    schemes = ((doc.get("components") or {}).get("securitySchemes") or {})
    for requirement in security:
        for scheme_name in (requirement or {}):
            scheme = schemes.get(scheme_name)
            if not isinstance(scheme, dict):
                continue
            kind = str(scheme.get("type") or "")
            if kind == "http":
                return {"type": str(scheme.get("scheme") or "http").lower(),
                        "scheme": scheme_name}
            if kind == "apiKey":
                return {"type": "apikey", "in": scheme.get("in"),
                        "name": scheme.get("name"), "scheme": scheme_name}
            return {"type": kind, "scheme": scheme_name}
    return {}


def _from_openapi(doc: dict, name: str | None = None) -> Interface:
    entry = _select_operation(openapi_operations(doc), name)
    operation = entry["operation"]
    body_schema = _json_content(operation.get("requestBody"))
    body_schema = resolve_refs(body_schema, doc) if body_schema else None
    fields = set(_flatten(body_schema or {}).get("properties") or {})
    for param in resolve_refs(entry["parameters"], doc):
        if isinstance(param, dict) and param.get("name") and param.get("in") != "header":
            fields.add(str(param["name"]))

    response = resolve_refs(_success_response(operation), doc)
    response_schema = _json_content(response)
    return Interface(
        kind="openapi",
        request_template={k: v for k, v in _openapi_template(doc, entry).items()
                          if k != "auth"},
        input_fields=sorted(fields),
        response_schema=response_schema,
        response_paths=schema_paths(response_schema) if response_schema else [],
        auth=_openapi_auth(doc, operation),
        hash=interface_hash(doc),
        name=entry["name"],
    )


# ── MCP ─────────────────────────────────────────────────────────────────────

def mcp_tools(doc: Any) -> list[dict]:
    """Tools out of a descriptor, a `tools/list` result, or a bare tool object.

    Only shapes that unambiguously carry an input schema count; anything else stays
    ungrounded rather than being guessed at.
    """
    def has_schema(tool: Any) -> bool:
        return isinstance(tool, dict) and bool(tool.get("name")) and isinstance(
            tool.get("inputSchema") or tool.get("input_schema"), dict)

    candidates: list[Any] = []
    if isinstance(doc, list):
        candidates = doc
    elif isinstance(doc, dict):
        for holder in (doc, doc.get("result"), doc.get("params")):
            tools = holder.get("tools") if isinstance(holder, dict) else None
            if isinstance(tools, list):
                candidates = tools
                break
        else:
            candidates = [doc]          # a bare tool object is its own descriptor
    return [t for t in candidates if has_schema(t)]


def _from_mcp(doc: Any, name: str | None = None) -> Interface:
    tools = mcp_tools(doc)
    entry = _select([{"name": t.get("name"), "tool": t} for t in tools], name,
                    kind="tool", where="descriptor")
    tool = entry["tool"]
    schema = resolve_refs(tool.get("inputSchema") or tool.get("input_schema") or {}, tool)
    fields = sorted(_flatten(schema).get("properties") or {})
    output = tool.get("outputSchema") or tool.get("output_schema")
    output = resolve_refs(output, tool) if isinstance(output, dict) else None
    return Interface(
        kind="mcp",
        request_template={"transport": "mcp", "method": "tools/call",
                          "tool": entry["name"],
                          "arguments": {f: "{{%s}}" % f for f in fields}},
        input_fields=fields,
        response_schema=output,
        response_paths=schema_paths(output) if output else [],
        hash=interface_hash(doc),
        name=str(entry["name"]),
    )


# ── schemas: paths and samples ──────────────────────────────────────────────

def _flatten(schema: Any) -> dict:
    """One object view of a schema, collapsing allOf/oneOf/anyOf.

    A union has no single shape, so the first branch is the one we describe and sample --
    a plausible sample beats no sample at all.
    """
    if not isinstance(schema, dict):
        return {}
    for key in ("oneOf", "anyOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            rest = {k: v for k, v in schema.items() if k != key}
            return {**_flatten(options[0]), **rest}
    parts = schema.get("allOf")
    if isinstance(parts, list) and parts:
        merged = {k: v for k, v in schema.items() if k != "allOf"}
        properties = dict(merged.get("properties") or {})
        required = list(merged.get("required") or [])
        for part in parts:
            part = _flatten(part)
            properties.update(part.get("properties") or {})
            required += list(part.get("required") or [])
            if part.get("type") and not merged.get("type"):
                merged["type"] = part["type"]
        if properties:
            merged["properties"] = properties
            merged.setdefault("type", "object")
        if required:
            merged["required"] = sorted(set(required))
        return merged
    return schema


def schema_paths(schema: Any, prefix: str = "$", depth: int = 0) -> list[str]:
    """JSONPath-ish paths into a response, e.g. `$.findings[*].article`."""
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return []
    schema = _flatten(schema)
    out: list[str] = []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, sub in properties.items():
            path = f"{prefix}.{key}"
            out.append(path)
            out.extend(schema_paths(sub, path, depth + 1))
    elif schema.get("type") == "array" or isinstance(schema.get("items"), dict):
        path = f"{prefix}[*]"
        items = schema.get("items")
        nested = schema_paths(items, path, depth + 1) if isinstance(items, dict) else []
        out.extend(nested or [path])
    # Sorted, so the same API described in JSON and in YAML grounds identically.
    return sorted(set(out))


def sample_response(iface: Interface) -> dict:
    """A plausible response for `iface`, for dry-running a generated check.

    Never a network call. Shape only -- values are placeholders.
    """
    if iface.response_schema:
        sample = _sample_from_schema(iface.response_schema)
        if isinstance(sample, dict):
            return sample
        return {"text": json.dumps(sample), "json": sample}
    return {"text": "sample response", "json": None}


def _sample_from_schema(schema: Any, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return "sample"
    schema = _flatten(schema)
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind is None:
        kind = "object" if schema.get("properties") else "array" if schema.get("items") else None
    if kind == "object":
        return {k: _sample_from_schema(v, depth + 1)
                for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [_sample_from_schema(schema.get("items") or {}, depth + 1)]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return _FORMAT_SAMPLES.get(str(schema.get("format")), "sample")


def describe_for_prompt(iface: Interface) -> str:
    """Render the interface for an LLM prompt, or "" when there is nothing to say."""
    if not iface.is_grounded():
        return ""
    lines = [f"TARGET INTERFACE ({iface.kind}):"]
    template = iface.request_template or {}
    if template.get("url"):
        lines.append(f"Request: {template.get('method', 'POST')} {template['url']}")
    elif template.get("tool"):
        lines.append(f"Tool: {template['tool']}")
    if iface.input_fields:
        lines.append(f"Request fields: {', '.join(iface.input_fields)}")
    if iface.response_paths:
        lines.append(f"Response paths: {', '.join(iface.response_paths)}")
    if iface.response_schema:
        lines.append(f"Response schema: {json.dumps(iface.response_schema)[:1500]}")
    return "\n".join(lines)
