"""Generic REST target with Postman-collection or OpenAPI import.

The file is read by `generator/interface.py`, which is also what the builder grounds on:
one parser, so the request the adapter sends and the request the builder reasons about
cannot drift apart. Format is decided by content, not by extension.
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Any
import requests
from ..llm.provider import LLMConfigError
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
            imported = self._import(import_, request)
            # Collection variables are defaults; what the spec declares wins.
            self.variables = {**imported.pop("variables", {}), **self.variables}
            imported_auth = imported.pop("auth", {})
            self.template = imported
            url = str(self.template.get("url") or "")
            if endpoint and not url.startswith(("http://", "https://")):
                # An OpenAPI document with no `servers` yields a bare path; the spec's
                # endpoint is the server it belongs to.
                self.template["url"] = endpoint.rstrip("/") + url
            self._adopt_auth(imported_auth)

    def _import(self, path: str, request_name: str | None) -> dict:
        # Imported lazily: the generator is a layer above adapters, and importing it at
        # module scope would make adapter imports depend on the builder's import graph.
        from ..generator.interface import (
            detect_format, load_document, openapi_request, postman_request,
        )
        doc = load_document(Path(path).read_text())
        kind = detect_format(doc)
        if kind == "postman":
            return postman_request(doc, request_name)
        if kind == "openapi":
            return openapi_request(doc, request_name)
        raise ValueError(f"{path}: not a Postman collection or an OpenAPI document")

    def _adopt_auth(self, imported: dict) -> None:
        """Carry a collection's declared auth over, without inventing credentials.

        Spec auth always wins -- it is the one that knows which env var holds the token.
        A collection's bearer token is usually a `{{variable}}`, so it goes in as a header
        and is filled by the same substitution as everything else.
        """
        if self.auth or not imported:
            return
        token = (imported.get("params") or {}).get("token")
        if imported.get("type") == "bearer" and token:
            self.template.setdefault("headers", {})
            self.template["headers"]["Authorization"] = f"Bearer {token}"

    def describe(self) -> dict:
        return {"adapter": self.name, "endpoint": self.template.get("url")}

    def _token_env(self) -> str | None:
        """Name of the variable holding this target's bearer token, if it uses one."""
        if self.auth.get("type") != "bearer":
            return None
        return self.auth.get("token_env") or None

    def ping(self) -> dict:
        import os
        import time
        from urllib.parse import urlparse
        env_var = self._token_env()
        configured = bool(os.environ.get(env_var)) if env_var else True
        url = self.template.get("url")
        if not url:
            return {"ok": True, "reachable": True, "authenticated": None,
                    "latency_ms": 0.0, "error": None, "env_var": env_var}
        concrete = _subst(url, self.variables)
        parsed = urlparse(concrete)
        # Ping the server root; any HTTP response means the server is reachable.
        base = f"{parsed.scheme}://{parsed.netloc}"
        t0 = time.perf_counter()
        try:
            requests.head(base, timeout=5, allow_redirects=True)
        except requests.RequestException as exc:
            return {
                "ok": False,
                "reachable": False,
                "authenticated": None,
                "latency_ms": (time.perf_counter() - t0) * 1000,
                "error": f"{base}: {exc}",
                "env_var": env_var,
            }
        latency = (time.perf_counter() - t0) * 1000
        if env_var and not configured:
            return {"ok": False, "reachable": True, "authenticated": False,
                    "latency_ms": latency, "error": f"{env_var} is not set",
                    "env_var": env_var}
        # The root probe is unauthenticated, so it says nothing about the token.
        return {"ok": True, "reachable": True, "authenticated": None,
                "latency_ms": latency, "error": None, "env_var": env_var}

    def _headers(self) -> dict:
        h = dict(self.template.get("headers", {}))
        env_var = self._token_env()
        if self.auth.get("type") == "bearer":
            import os
            if not env_var:
                raise LLMConfigError("bearer auth needs auth.token_env (the variable name)",
                                     adapter=self.name)
            token = os.environ.get(env_var, "")
            if not token:
                raise LLMConfigError(f"{env_var} is not set", adapter=self.name,
                                     env_var=env_var)
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
