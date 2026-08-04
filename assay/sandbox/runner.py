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
  * builtins `open`, `exec`, `eval`, `compile` removed
  * socket factories patched to raise (defence in depth)

The allowlist and the builtins removal are installed *before* the module body is
executed, so they cover the module's top-level statements as well as check().

This stops accidental and naive-malicious I/O. It is NOT a guarantee against a
determined adversary with native-code tricks, and it does NOT isolate the
filesystem or block egress at the OS level — the subprocess inherits the engine's
working directory. For untrusted third-party code, run Assay with a hardened tier
(gVisor / Firecracker / WASM), which is not yet implemented. The contract a
generated module must meet:

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

    # Read and compile the user module while the builtins we are about to remove are
    # still available. Nothing from the module has executed at this point -- compile()
    # only parses. The lockdown below therefore covers the module's top-level
    # statements and imports, not just the body of check().
    try:
        with open(payload["module_path"], "r") as _fh:
            _source = _fh.read()
        _code = compile(_source, payload["module_path"], "exec")
    except Exception as e:
        print(json.dumps({"error": f"load error: {type(e).__name__}: {e}"})); sys.exit(0)

    _real_import = builtins.__import__
    _real_exec = exec

    # Patch socket BEFORE the import guard is installed: afterwards `socket` is not
    # allowlisted, so importing it here would raise and the patch would be skipped.
    try:
        import socket
        socket.socket = lambda *a, **k: (_ for _ in ()).throw(OSError("network blocked"))
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("network blocked"))
    except Exception:
        pass

    def _guard(name, *a, **k):
        top = name.split(".")[0]
        if top not in ALLOWED:
            raise ImportError(f"import of '{name}' blocked in sandbox")
        return _real_import(name, *a, **k)

    # Lock down for module execution AND check() execution.
    builtins.__import__ = _guard
    for _b in ("open", "exec", "eval", "compile"):
        if hasattr(builtins, _b):
            setattr(builtins, _b, None)

    _ns = {"__name__": "genchk", "__file__": payload["module_path"], "__builtins__": builtins}
    try:
        _real_exec(_code, _ns)
    except Exception as e:
        print(json.dumps({"error": f"load error: {type(e).__name__}: {e}"})); sys.exit(0)

    _check = _ns.get("check")
    if _check is None:
        print(json.dumps({"error": "module defines no check(response, context)"})); sys.exit(0)

    try:
        out = _check(payload["response"], payload["context"])
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
