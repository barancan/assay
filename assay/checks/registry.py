"""Dispatch a single CheckSpec to template / generated / judge execution."""
from __future__ import annotations
from typing import Any
from ..spec.models import CheckSpec
from ..sandbox import run_generated_check
from ..judges import run_judge_check
from .base import CheckResult, from_raw
from .library import REGISTRY as TEMPLATES


def run_check(spec: CheckSpec, response: dict, context: dict,
              judges: dict[str, Any]) -> CheckResult:
    cid = spec.uses or spec.type
    if spec.type == "template":
        fn = TEMPLATES.get(spec.uses)
        if fn is None:
            return CheckResult(cid, False, severity="fail",
                               message=f"unknown template: {spec.uses}", required=spec.required)
        raw = fn(response, spec.with_)
        return from_raw(raw, f"template:{spec.uses}", spec.required)
    if spec.type == "generated":
        raw = run_generated_check(spec.uses, response, context)
        return from_raw(raw, f"generated:{spec.uses}", spec.required)
    if spec.type == "judge":
        provider = judges.get(spec.judge)
        if provider is None:
            return CheckResult(cid, False, severity="fail",
                               message=f"unknown judge: {spec.judge}", required=spec.required)
        raw = run_judge_check(provider, spec.rubric, response, context)
        return from_raw(raw, f"judge:{spec.judge}", spec.required)
    return CheckResult(cid, False, severity="fail",
                       message=f"unknown check type: {spec.type}", required=spec.required)
