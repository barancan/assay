def check(response: dict, context: dict) -> dict:
    """R3: blocked findings must carry high|critical severity."""
    body = response.get("json") or {}
    findings = body.get("findings", [])
    sev = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    bad = [f for f in findings
           if f.get("status") == "blocked" and sev.get(f.get("severity"), 0) < 2]
    return {
        "passed": not bad,
        "score": None,
        "severity": "fail" if bad else "info",
        "message": (f"{len(bad)} blocked finding(s) below high severity"
                    if bad else "severity monotonic with status"),
        "evidence": {"violations": bad[:5]},
    }
