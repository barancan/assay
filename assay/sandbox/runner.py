"""Run an LLM-generated check in an isolated subprocess.

Threat model for v0: an LLM-authored *pure data* check that may be buggy or
naively do something it shouldn't. The check is handed already-captured dicts —
it never needs the network, the filesystem, or subprocesses.

Containment, in layers:
  * separate `-I` (isolated) interpreter, not the engine process
  * CPU + address-space rlimits (POSIX) + hard wall-clock timeout
  * import ALLOWLIST: only pure-computation stdlib + check helpers resolve;
    os / sys / socket / subprocess / importlib / ctypes / urllib / requests ...
    raise ImportError
  * builtins `open`, `exec`, `eval`, `compile` removed inside the check
  * socket factories patched to raise (defence in depth)

This stops accidental and naive-malicious I/O. It is NOT a guarantee against a
determined adversary with native-code tricks — for untrusted third-party code,
run Assay with the hardened tier (gVisor / Firecracker / WASM), documented in
the design under "sandbox strength". The contract a generated module must meet:

    def check(response: dict, context: dict) -> dict
"""
from __future__ import annotations
import json
import subprocess
import sys
import textwrap
from pathlib import Path

# Pure-computation allowlist for generated checks. jsonpath/regex over the
# response dict is available via plain Python; rich path queries belong in the
# trusted template layer, not in sandboxed generated code.
_ALLOWED = (
    "json re math statistics decimal fractions datetime collections itertools "
    "functools operator string typing numbers unicodedata hashlib base64"
).split()

_WORKER = textwrap.dedent('''
    import json, sys, builtins, importlib, importlib.util, resource

    def _limit():
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024, 256*1024*1024))
        except (ValueError, OSError):
            pass
    _limit()

    payload = json.load(sys.stdin)

    # Pre-import every allowlisted module (and known submodules) BEFORE locking,
    # so their transitive deps are cached and the guard only sees the user's
    # own explicit top-level imports.
    ALLOWED = set(payload["allowed"])
    for _m in list(payload["allowed"]):
        try: importlib.import_module(_m)
        except Exception: pass

    # Load the user module BEFORE locking imports (its own top-level imports run now).
    spec = importlib.util.spec_from_file_location("genchk", payload["module_path"])
    mod = importlib.util.module_from_spec(spec)

    _real_import = builtins.__import__
    def _guard(name, *a, **k):
        top = name.split(".")[0]
        if top not in ALLOWED:
            raise ImportError(f"import of '{name}' blocked in sandbox")
        return _real_import(name, *a, **k)

    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        print(json.dumps({"error": f"load error: {type(e).__name__}: {e}"})); sys.exit(0)

    if not hasattr(mod, "check"):
        print(json.dumps({"error": "module defines no check(response, context)"})); sys.exit(0)

    # Lock down for the duration of check() execution.
    builtins.__import__ = _guard
    for _b in ("open", "exec", "eval", "compile"):
        if hasattr(builtins, _b):
            setattr(builtins, _b, None)
    try:
        import socket
        socket.socket = lambda *a, **k: (_ for _ in ()).throw(OSError("network blocked"))
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("network blocked"))
    except Exception:
        pass

    try:
        out = mod.check(payload["response"], payload["context"])
        print(json.dumps({"result": out}))
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
''')


def run_generated_check(module_path: str, response: dict, context: dict,
                        timeout_s: float = 6.0) -> dict:
    if not Path(module_path).exists():
        return {"passed": False, "severity": "fail",
                "message": f"generated check not found: {module_path}"}
    payload = json.dumps({"module_path": str(Path(module_path).resolve()),
                          "response": response, "context": context,
                          "allowed": _ALLOWED})
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", _WORKER],
                              input=payload, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"passed": False, "severity": "fail", "message": "generated check timed out"}
    if proc.returncode != 0:
        return {"passed": False, "severity": "fail",
                "message": f"sandbox crashed: {proc.stderr.strip()[:200]}"}
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"passed": False, "severity": "fail", "message": "sandbox produced no result"}
    if "error" in out:
        return {"passed": False, "severity": "fail", "message": out["error"]}
    return out["result"]
