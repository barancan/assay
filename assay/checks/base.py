"""Check result contract. A check is a pure function (response_dict, context) -> CheckResult."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class CheckResult:
    check_id: str
    passed: bool
    score: float | None = None          # 0..1 for graded checks
    threshold: float | None = None      # min passing score; pass/fail = score >= threshold
    severity: str = "info"              # info | warn | fail
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    required: bool = True
    type: str = ""                      # template | generated | judge (for display)

    def to_dict(self) -> dict:
        return asdict(self)


def from_raw(raw: dict, check_id: str, required: bool,
             type: str = "", threshold: float | None = None) -> CheckResult:
    """Build a CheckResult from the plain dict a generated function returns."""
    score = raw.get("score")
    passed = bool(raw.get("passed", False))
    if threshold is not None and score is not None:
        passed = score >= threshold
    return CheckResult(
        check_id=check_id,
        passed=passed,
        score=score,
        threshold=threshold,
        severity=raw.get("severity", "info"),
        message=raw.get("message", ""),
        evidence=raw.get("evidence", {}) or {},
        required=required,
        type=type,
    )
