"""Run an LLM-generated check in a contained subprocess.

Threat model: an LLM-authored *pure data* check that may be buggy or naively do
something it shouldn't. The check is handed already-captured dicts -- it never needs
the network, the filesystem, or subprocesses.

Containment, in layers:

  * a separate `-I` (isolated) interpreter, never the engine process
  * an **empty environment** -- the parent's variables, including provider API keys,
    are not inherited
  * a **throwaway working directory**, so relative paths reach nothing; the repo and
    `.assay/` are not the cwd
  * a **network namespace** with no interfaces, where the platform supports
    unprivileged `unshare` -- a real egress block, not a monkeypatch
  * CPU + address-space rlimits (POSIX) and a hard wall-clock timeout
  * an import ALLOWLIST: only pure-computation stdlib resolves; os / sys / socket /
    subprocess / importlib / ctypes / urllib / requests raise ImportError
  * builtins `open`, `exec`, `eval`, `compile` removed
  * socket factories patched to raise, as defence in depth behind the namespace

The allowlist and the builtins removal are installed *before* the module body runs, so
they cover top-level statements as well as `check()`. The source is read by the trusted
parent and passed in, so the child never needs `open` at all.

`sandbox_tier()` reports which of these are actually active on this host, because the
network namespace is unavailable on some platforms and a containment claim that is not
true is worse than one that is merely modest. It is still NOT a guarantee against a
determined adversary with native-code tricks; for genuinely untrusted third-party code,
run Assay inside a VM or container boundary you control.

The contract a generated module must meet:

    def check(response: dict, context: dict) -> dict
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# Pure-computation allowlist for generated checks. jsonpath/regex over the
# response dict is available via plain Python; rich path queries belong in the
# trusted template layer, not in sandboxed generated code.
_ALLOWED = (
    "json re math statistics decimal fractions datetime collections itertools "
    "functools operator string typing numbers unicodedata hashlib base64"
).split()

# A minimal environment. Nothing from the parent is inherited, so a check cannot read
# an API key even if it found a way to reach os.environ.
_CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}

_WORKER = textwrap.dedent('''
    import json, sys, builtins, importlib, resource

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

    # Pre-import every allowlisted module BEFORE locking, so their transitive deps are
    # cached and the guard only sees the check's own explicit imports.
    ALLOWED = set(payload["allowed"])
    for _m in list(payload["allowed"]):
        try: importlib.import_module(_m)
        except Exception: pass

    # The parent read the source, so the child never opens a file. compile() only
    # parses -- nothing from the module has executed yet, so the lockdown below covers
    # the module body as well as check().
    try:
        _code = compile(payload["source"], payload["origin"], "exec")
    except Exception as e:
        print(json.dumps({"error": f"load error: {type(e).__name__}: {e}"})); sys.exit(0)

    _real_import = builtins.__import__
    _real_exec = exec

    # Patch socket BEFORE the import guard: afterwards `socket` is not allowlisted, so
    # importing it here would raise and the patch would silently be skipped.
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

    builtins.__import__ = _guard
    for _b in ("open", "exec", "eval", "compile"):
        if hasattr(builtins, _b):
            setattr(builtins, _b, None)

    _ns = {"__name__": "genchk", "__file__": payload["origin"], "__builtins__": builtins}
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


def _netns_prefix() -> list[str]:
    """Command prefix that puts the child in an empty network namespace, if we can.

    Unprivileged `unshare --net` needs user namespaces, which some kernels and most
    non-Linux hosts do not offer. Probed once; when it is unavailable we fall back to
    the in-process socket patch and say so via sandbox_tier().
    """
    if _netns_prefix._cached is not None:
        return _netns_prefix._cached

    prefix: list[str] = []
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        # --map-current-user keeps the child as this user; --map-root-user is the
        # older spelling and only fakes root inside the namespace.
        for mapping in ("--map-current-user", "--map-root-user"):
            candidate = ["unshare", "--net", mapping]
            try:
                probe = subprocess.run(candidate + ["true"], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            if probe.returncode == 0:
                prefix = candidate
                break
    _netns_prefix._cached = prefix
    return prefix


_netns_prefix._cached = None


def sandbox_tier() -> dict:
    """What containment is actually active here, so docs and UI need not guess."""
    return {
        "subprocess_isolation": True,
        "clean_environment": True,
        "throwaway_cwd": True,
        "import_allowlist": True,
        "builtins_removed": True,
        "rlimits": os.name == "posix",
        "network_namespace": bool(_netns_prefix()),
        # True only when egress is blocked by the OS rather than by monkeypatching.
        "egress_blocked": bool(_netns_prefix()),
    }


def _fail(message: str) -> dict:
    return {"passed": False, "severity": "fail", "message": message}


def run_generated_source(source: str, response: dict, context: dict,
                         timeout_s: float = 6.0, origin: str = "<generated>") -> dict:
    """Execute generated check source against captured data.

    Takes the source text rather than a path so codegen can dry-run a candidate before
    anything is written or persisted.
    """
    payload = json.dumps({"source": source, "origin": origin,
                          "response": response, "context": context,
                          "allowed": _ALLOWED})
    # A throwaway cwd: the child inherits a directory that contains nothing, instead of
    # the repo it was launched from.
    workdir = tempfile.mkdtemp(prefix="assay-sbx-")
    try:
        cmd = _netns_prefix() + [sys.executable, "-I", "-c", _WORKER]
        try:
            proc = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                                  timeout=timeout_s, cwd=workdir, env=dict(_CLEAN_ENV))
        except subprocess.TimeoutExpired:
            return _fail("generated check timed out")
        except OSError as exc:
            return _fail(f"sandbox could not start: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if proc.returncode != 0:
        return _fail(f"sandbox crashed: {proc.stderr.strip()[:200]}")
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _fail("sandbox produced no result")
    if "error" in out:
        return _fail(out["error"])
    return out["result"]


def run_generated_check(module_path: str, response: dict, context: dict,
                        timeout_s: float = 6.0) -> dict:
    """Execute a generated check that lives on disk.

    The source is read here, in the trusted parent, so the sandboxed child never needs
    filesystem access at all.
    """
    path = Path(module_path)
    if not path.exists():
        return _fail(f"generated check not found: {module_path}")
    try:
        source = path.read_text()
    except OSError as exc:
        return _fail(f"generated check unreadable: {exc}")
    return run_generated_source(source, response, context, timeout_s=timeout_s,
                                origin=str(path.resolve()))
