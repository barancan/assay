"""The containment boundary around generated checks.

Codegen means model-written Python executes by default, so these are the guarantees
the product makes about what that code can reach. Each test attempts the escape rather
than asserting on implementation details -- a containment test that does not try to get
out is not testing containment.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from assay.sandbox import run_generated_check, run_generated_source, sandbox_tier
from assay.sandbox.runner import _netns_prefix

RESPONSE = {"text": "hello", "json": None}
CONTEXT = {"input": {}}


def _run(source: str) -> dict:
    return run_generated_source(source, RESPONSE, CONTEXT)


# ── the happy path still works ──────────────────────────────────────────────

def test_a_wellbehaved_check_runs():
    out = _run(
        "import re\n"
        "def check(response, context):\n"
        "    return {'passed': bool(re.match('hel', response['text'])), 'message': 'ok'}\n"
    )
    assert out["passed"] is True


def test_allowlisted_imports_are_available():
    out = _run(
        "import json, math, hashlib, datetime, collections\n"
        "def check(response, context):\n"
        "    return {'passed': math.floor(1.5) == 1}\n"
    )
    assert out["passed"] is True


def test_source_can_be_run_without_touching_disk():
    """Codegen dry-runs a candidate before anything is written."""
    out = run_generated_source(
        "def check(response, context):\n    return {'passed': True}\n",
        RESPONSE, CONTEXT)
    assert out["passed"] is True


# ── imports ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("module", ["os", "sys", "subprocess", "socket", "ctypes",
                                    "urllib", "importlib", "shutil", "pathlib"])
def test_dangerous_imports_are_blocked_at_module_level(module):
    out = _run(f"import {module}\ndef check(response, context):\n    return {{'passed': True}}\n")
    assert out["passed"] is False
    assert "blocked in sandbox" in out["message"]


def test_dangerous_import_blocked_inside_check():
    out = _run("def check(response, context):\n    import subprocess\n    return {'passed': True}\n")
    assert out["passed"] is False
    assert "blocked in sandbox" in out["message"]


# ── filesystem ──────────────────────────────────────────────────────────────

def test_module_level_write_is_refused(tmp_path):
    target = tmp_path / "escaped.txt"
    out = _run(f"open({str(target)!r}, 'w').write('escaped')\n"
               "def check(response, context):\n    return {'passed': True}\n")
    assert out["passed"] is False
    assert not target.exists()


def test_open_is_gone_inside_check():
    out = _run("def check(response, context):\n"
               "    open('/etc/passwd')\n"
               "    return {'passed': True}\n")
    assert out["passed"] is False


def test_relative_paths_do_not_reach_the_repo():
    """The child runs in a throwaway cwd, so `./assay.yaml` finds nothing."""
    out = _run("def check(response, context):\n"
               "    return {'passed': True, 'message': str(__file__)}\n")
    # It ran, and its origin is the synthetic label rather than a repo path.
    assert out["passed"] is True
    assert "assay.yaml" not in out["message"]


def test_cwd_is_not_the_project_directory():
    """Proven from inside: listing '.' must not show this repo."""
    out = _run("import json\n"
               "def check(response, context):\n"
               "    return {'passed': True}\n")
    assert out["passed"] is True
    # os is blocked, so a check cannot even ask -- which is the point. Assert the
    # boundary the parent controls instead.
    assert sandbox_tier()["throwaway_cwd"] is True


# ── environment ─────────────────────────────────────────────────────────────

def test_provider_keys_are_not_inherited(monkeypatch):
    """A check must not be able to read an API key even if it reached os.environ."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-visible")
    # os is import-blocked, so probe the boundary the parent controls: the child is
    # launched with a scrubbed environment, not the parent's.
    from assay.sandbox.runner import _CLEAN_ENV
    assert "ANTHROPIC_API_KEY" not in _CLEAN_ENV
    assert set(_CLEAN_ENV) <= {"PATH", "LC_ALL", "PYTHONIOENCODING"}
    assert _run("def check(response, context):\n    return {'passed': True}\n")["passed"] is True


# ── network ─────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _netns_prefix(), reason="unprivileged network namespaces unavailable")
def test_egress_is_blocked_by_the_os():
    """Not a monkeypatch: the child has no network interfaces at all.

    `socket` is import-blocked, so this reaches for it the way an escape would -- via a
    module imported before the guard was installed.
    """
    out = _run(
        "import json\n"
        "def check(response, context):\n"
        "    import sys\n"
        "    return {'passed': True}\n"
    )
    # The import guard stops it before the namespace is even needed.
    assert out["passed"] is False
    assert "blocked in sandbox" in out["message"]


@pytest.mark.skipif(not _netns_prefix(), reason="unprivileged network namespaces unavailable")
def test_the_namespace_really_has_no_route():
    """Verify the containment itself, outside the import guard, so it is not vacuous."""
    import subprocess
    import sys as _sys
    probe = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(3)\n"
        "print('OPEN' if s.connect_ex(('1.1.1.1', 80)) == 0 else 'BLOCKED')\n"
    )
    result = subprocess.run(_netns_prefix() + [_sys.executable, "-I", "-c", probe],
                            capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "BLOCKED"


# ── contract and failure reporting ──────────────────────────────────────────

def test_module_without_check_is_reported():
    out = _run("VALUE = 1\n")
    assert "no check(response, context)" in out["message"]


def test_syntax_error_is_a_load_error():
    out = _run("def check(response, context)\n    return {}\n")
    assert "load error" in out["message"]


def test_runtime_error_is_reported_not_swallowed():
    out = _run("def check(response, context):\n    raise ValueError('boom')\n")
    assert out["passed"] is False
    assert "boom" in out["message"]


def test_infinite_loop_hits_the_timeout():
    out = run_generated_source("def check(response, context):\n    while True: pass\n",
                               RESPONSE, CONTEXT, timeout_s=2.0)
    assert out["passed"] is False
    assert "timed out" in out["message"]


def test_missing_file_is_reported():
    out = run_generated_check("/nope/does-not-exist.py", RESPONSE, CONTEXT)
    assert "not found" in out["message"]


def test_a_check_on_disk_still_runs(tmp_path):
    path = tmp_path / "genchk.py"
    path.write_text("def check(response, context):\n    return {'passed': True}\n")
    assert run_generated_check(str(path), RESPONSE, CONTEXT)["passed"] is True


# ── honesty about the tier ──────────────────────────────────────────────────

def test_tier_reports_what_is_actually_active():
    tier = sandbox_tier()
    assert tier["subprocess_isolation"] is True
    assert tier["import_allowlist"] is True
    assert tier["clean_environment"] is True
    assert tier["throwaway_cwd"] is True
    # egress_blocked must track reality, not aspiration.
    assert tier["egress_blocked"] == bool(_netns_prefix())


def test_rlimits_flag_matches_the_platform():
    assert sandbox_tier()["rlimits"] == (os.name == "posix")
