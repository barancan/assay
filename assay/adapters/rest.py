"""Generic REST target with optional Postman-collection / OpenAPI import."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Any
import requests
from .base import ModelRequest, ModelResponse

_VAR = re.compile(r"\{\{(\w+)\}\}")


def _subst(value: Any, variables: dict) -> Any:
    if isinstance(value, str):
        return _VAR.sub(lambda m: str(variables.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _subst(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, variables) for v in value]
    return value


class RestAdapter:
    name = "rest"

    def __init__(self, *, import_: str | None = None, request: str | None = None,
                 endpoint: str | None = None, variables: dict | None = None,
                 auth: dict | None = None, **_: object) -> None:
        self.variables = variables or {}
        self.auth = auth or {}
        self.template = {"method": "POST", "url": endpoint, "headers": {}, "body": None}
        if import_:
            self.template = self._from_postman(import_, request)

    def _from_postman(self, path: str, request_name: str | None) -> dict:
        col = json.loads(Path(path).read_text())
        items = col.get("item", [])
        chosen = None
        for it in items:
            if request_name is None or it.get("name") == request_name:
                chosen = it
                break
        if chosen is None:
            raise ValueError(f"request '{request_name}' not found in collection")
        r = chosen["request"]
        url = r["url"]["raw"] if isinstance(r["url"], dict) else r["url"]
        headers = {h["key"]: h["value"] for h in r.get("header", [])}
        body = None
        if r.get("body", {}).get("mode") == "raw":
            body = r["body"]["raw"]
        return {"method": r.get("method", "POST"), "url": url, "headers": headers, "body": body}

    def describe(self) -> dict:
        return {"adapter": self.name, "endpoint": self.template.get("url")}

    def ping(self) -> dict:
        import time
        from urllib.parse import urlparse
        url = self.template.get("url")
        if not url:
            return {"ok": True, "latency_ms": 0.0, "error": None}
        concrete = _subst(url, self.variables)
        parsed = urlparse(concrete)
        # Ping the server root; any HTTP response means the server is reachable.
        base = f"{parsed.scheme}://{parsed.netloc}"
        t0 = time.perf_counter()
        try:
            requests.head(base, timeout=5, allow_redirects=True)
            return {"ok": True, "latency_ms": (time.perf_counter() - t0) * 1000, "error": None}
        except requests.RequestException as exc:
            return {
                "ok": False,
                "latency_ms": (time.perf_counter() - t0) * 1000,
                "error": f"{base}: {exc}",
            }

    def _headers(self) -> dict:
        h = dict(self.template.get("headers", {}))
        if self.auth.get("type") == "bearer":
            import os
            token = os.environ.get(self.auth.get("token_env", ""), "")
            h["Authorization"] = f"Bearer {token}"
        return h

    def invoke(self, req: ModelRequest) -> ModelResponse:
        variables = {**self.variables, **req.input}
        url = _subst(self.template["url"], variables)
        headers = _subst(self._headers(), variables)
        body_t = self.template.get("body")
        if body_t:
            body = _subst(body_t, variables)
        else:
            body = json.dumps(req.input)
        t0 = time.perf_counter()
        try:
            resp = requests.request(self.template["method"], url, headers=headers,
                                    data=body, timeout=req.params.get("timeout", 30))
            latency = (time.perf_counter() - t0) * 1000
            try:
                parsed = resp.json()
            except ValueError:
                parsed = None
            return ModelResponse(text=resp.text, raw=parsed if parsed is not None else {"text": resp.text},
                                 json=parsed, latency_ms=latency,
                                 status="ok" if resp.ok else "error",
                                 error=None if resp.ok else f"HTTP {resp.status_code}")
        except requests.RequestException as e:
            return ModelResponse(status="error", error=str(e),
                                 latency_ms=(time.perf_counter() - t0) * 1000)
