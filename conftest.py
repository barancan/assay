"""Session-wide test environment.

Mock adapters are refused unless a caller opts in (see assay.adapters.registry), and the
suite is built almost entirely on them: they are the reason `pytest` needs no API keys
and makes no network calls. So the opt-in is declared once, here, at the root — before
any test module imports anything.

`setdefault`, not assignment: a test that wants to prove the wall is there deletes the
variable with monkeypatch, and an operator running the suite against a real provider can
export ASSAY_ALLOW_MOCK=0 without editing this file.
"""
import os

os.environ.setdefault("ASSAY_ALLOW_MOCK", "1")
