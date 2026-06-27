"""LLM-judge check: load a rubric, prompt the judge provider, score the response."""
from __future__ import annotations
import json
from pathlib import Path
import yaml
from ..adapters.base import JudgeProvider, ModelResponse

_SYS = (
    "You are a strict evaluation judge. Score the MODEL RESPONSE against each rubric "
    "dimension. Reply with ONLY JSON: "
    '{"scores": {dim_id: int}, "rationale": str, "evidence_quotes": [str]}. '
    "Quote spans from the response as evidence. Do not reward verbosity."
)


def run_judge_check(provider: JudgeProvider, rubric_path: str, response: dict,
                    context: dict) -> dict:
    rubric = yaml.safe_load(Path(rubric_path).read_text())
    dims = rubric.get("dimensions", [])
    dim_text = "\n".join(
        f"- {d['id']}: {d['question']} (scale: {d.get('scale')})" for d in dims)
    user = (
        f"RUBRIC DIMENSIONS:\n{dim_text}\n\n"
        f"MODEL RESPONSE:\n{json.dumps(response.get('json') or response.get('text'))[:6000]}\n\n"
        f"CASE INPUT:\n{json.dumps(context.get('input'))[:2000]}"
    )
    out: ModelResponse = provider.complete(
        [{"role": "user", "content": user}],
        params={"system": _SYS, "temperature": 0.0})
    verdict = out.json or _safe(out.text)
    scores = (verdict or {}).get("scores", {})
    # Pass if every dimension meets its min (default: top of scale or >=1)
    passed = True
    for d in dims:
        scale = d.get("scale") or {}
        max_score = max((int(k) for k in scale), default=1)
        threshold = d.get("min_score", max_score)
        if int(scores.get(d["id"], 0)) < threshold:
            passed = False
    norm = None
    if dims:
        total = sum(int(scores.get(d["id"], 0)) for d in dims)
        maxtot = sum(max((int(k) for k in (d.get("scale") or {1: 1})), default=1) for d in dims)
        norm = round(total / maxtot, 3) if maxtot else None
    return {"passed": passed, "score": norm,
            "severity": "info" if passed else "warn",
            "message": (verdict or {}).get("rationale", "")[:300],
            "evidence": {"scores": scores,
                         "quotes": (verdict or {}).get("evidence_quotes", [])[:5]}}


def _safe(text: str | None):
    try:
        return json.loads(text or "")
    except (ValueError, TypeError):
        return None
