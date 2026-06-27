"""Decide whether a case / run passes based on its check results."""
from __future__ import annotations
from ..checks.base import CheckResult


def case_passed(results: list[CheckResult]) -> bool:
    return all(r.passed for r in results if r.required)


def run_passed(case_flags: list[bool]) -> bool:
    return all(case_flags)
