"""The sandbox lockdown must cover a generated check's module body, not just check().

Previously the import allowlist and the builtins removal were installed *after*
spec.loader.exec_module(), so a generated check could do anything it liked at module
top level. These tests pin the containment boundary at module load.
"""
from __future__ import annotations

from assay.sandbox import run_generated_check

RESPONSE = {"text": "hello", "json": None}
CONTEXT = {"input": {}}


def _write(tmp_path, source: str) -> str:
    path = tmp_path / "genchk.py"
    path.write_text(source)
    return str(path)


def test_wellbehaved_check_still_runs(tmp_path):
    path = _write(tmp_path, (
        "import json, re\n"
        "def check(response, context):\n"
        "    return {'passed': bool(re.match('hel', response['text'])), 'message': 'ok'}\n"
    ))
    assert run_generated_check(path, RESPONSE, CONTEXT)["passed"] is True


def test_top_level_blocked_import_is_refused(tmp_path):
    """A blocked import at MODULE level must fail -- this is the regression."""
    path = _write(tmp_path, (
        "import os\n"
        "def check(response, context):\n"
        "    return {'passed': True}\n"
    ))
    out = run_generated_check(path, RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "blocked in sandbox" in out["message"]


def test_top_level_side_effect_cannot_touch_the_filesystem(tmp_path):
    """Module-level code must not be able to write files."""
    target = tmp_path / "escaped.txt"
    path = _write(tmp_path, (
        f"open({str(target)!r}, 'w').write('escaped')\n"
        "def check(response, context):\n"
        "    return {'passed': True}\n"
    ))
    out = run_generated_check(path, RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert not target.exists()


def test_blocked_import_inside_check_is_refused(tmp_path):
    path = _write(tmp_path, (
        "def check(response, context):\n"
        "    import subprocess\n"
        "    return {'passed': True}\n"
    ))
    out = run_generated_check(path, RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "blocked in sandbox" in out["message"]


def test_socket_factories_are_patched(tmp_path):
    """socket is not allowlisted, so a check cannot even reach the patched factories."""
    path = _write(tmp_path, (
        "def check(response, context):\n"
        "    import socket\n"
        "    socket.create_connection(('example.com', 80))\n"
        "    return {'passed': True}\n"
    ))
    out = run_generated_check(path, RESPONSE, CONTEXT)
    assert out["passed"] is False


def test_module_without_check_is_reported(tmp_path):
    path = _write(tmp_path, "VALUE = 1\n")
    out = run_generated_check(path, RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "no check(response, context)" in out["message"]


def test_syntax_error_is_reported_as_load_error(tmp_path):
    path = _write(tmp_path, "def check(response, context)\n    return {}\n")
    out = run_generated_check(path, RESPONSE, CONTEXT)
    assert out["passed"] is False
    assert "load error" in out["message"]
