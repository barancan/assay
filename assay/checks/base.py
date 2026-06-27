"""Check result contract. A check is a pure function (response_dict, context) -> CheckResult."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class CheckResult:
    check_id: str
    passed: bool
    score: float | None = None          # 0..1 for graded checks
    severity: str = "info"              # info | warn | fail
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def from_raw(raw: dict, check_id: str, required: bool) -> CheckResult:
    """Build a CheckResult from the plain dict a generated function returns."""
    return CheckResult(
        check_id=check_id,
        passed=bool(raw.get("passed", False)),
        score=raw.get("score"),
        severity=raw.get("severity", "info"),
        message=raw.get("message", ""),
        evidence=raw.get("evidence", {}) or {},
        required=required,
    )
