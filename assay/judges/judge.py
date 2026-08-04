"""LLM-judge check: load a rubric, prompt the judge provider, score the response.

Three things separate this from "ask a model for JSON and hope":

  * the verdict is requested with a **schema**, so the adapter parses it and the judge
    reads `ModelResponse.json` instead of scraping prose;
  * quoted evidence is **verified against the response text**, so a judge that invents a
    supporting quote fails the check rather than passing it silently;
  * a dimension the judge did not score is an **explicit failure naming the dimension**,
    not a silent 0 that reads as "the model did badly".
"""
from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path
from typing import Any

import yaml

from ..adapters.base import JudgeProvider, ModelResponse

# The verdict contract, frozen in tests/test_phase_contracts.py. Rubric generation
# emits this as each rubric's `output_schema`; a rubric may override it.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {"type": "object"},
        "rationale": {"type": "string"},
        "evidence_quotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "rationale"],
}

_SYS = (
    "You are a strict evaluation judge. Score the MODEL RESPONSE against each rubric "
    "dimension, using the scale anchors verbatim. Reply with ONLY JSON: "
    '{"scores": {dim_id: int}, "rationale": str, "evidence_quotes": [str]}. '
    "Score EVERY dimension id listed, even when the response is empty. "
    "Every quote in evidence_quotes must be copied verbatim from the MODEL RESPONSE — "
    "quotes that do not appear in it invalidate the verdict. Do not reward verbosity."
)

_WS = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\.{3,}|…")
# Typographic variants models routinely substitute when they "quote" a span.
_LOOKALIKES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-", " ": " ",
})


class RubricError(ValueError):
    """The rubric itself is unusable — not a verdict this judge can score against."""


def run_judge_check(provider: JudgeProvider, rubric: str | Path | dict, response: dict,
                    context: dict, samples: int = 1) -> dict:
    """Score `response` against `rubric` using `provider`.

    `rubric` is a path to a YAML file or the rubric dict itself — the engine
    materialises DB-stored rubrics to disk and passes paths, callers that already hold
    the rubric do not have to round-trip through a file.
    """
    doc = _load_rubric(rubric)
    dims = doc.get("dimensions") or []
    if not isinstance(dims, list):
        raise RubricError("rubric 'dimensions' must be a list")

    # An explicit `samples` argument wins; otherwise the rubric decides, so
    # self-consistency is a property of the rubric rather than a global setting.
    n = samples if samples != 1 else _as_int(doc.get("samples"), 1)
    n = max(1, min(n, 9))

    schema = doc.get("output_schema") or VERDICT_SCHEMA
    user = _build_prompt(dims, response, context)
    verdicts = [_ask(provider, user, schema) for _ in range(n)]

    per_dim = _collect_scores(dims, verdicts)
    scores, consistency = _reduce(per_dim)

    failures: list[str] = []
    for d in dims:
        did = str(d.get("id"))
        if did not in scores:
            # Honest missing-score handling: name the dimension. A silent 0 here is
            # indistinguishable from a genuine zero score.
            failures.append(f"judge did not score dimension '{did}'")
            continue
        threshold = _threshold(d, doc)
        if scores[did] < threshold:
            failures.append(f"{did}={scores[did]} < min_score {threshold}")

    quotes = _unique_quotes(verdicts)
    evidence: dict[str, Any] = {"scores": scores, "quotes": quotes[:5]}
    if doc.get("require_evidence"):
        verified, fabricated = verify_quotes(quotes, response)
        evidence["verified_quotes"] = verified[:5]
        if fabricated:
            evidence["unverified_quotes"] = fabricated[:5]
        if not quotes:
            failures.append("rubric requires evidence, but the judge quoted nothing")
        elif fabricated:
            failures.append(
                "evidence quotes do not appear in the response: "
                + "; ".join(repr(q[:80]) for q in fabricated[:3]))
    if consistency is not None:
        evidence["consistency"] = consistency

    passed = not failures
    return {
        "passed": passed,
        "score": _normalised(dims, scores),
        "severity": "info" if passed else "warn",
        "message": _message(verdicts, failures),
        "evidence": evidence,
    }


def verify_quotes(quotes: list[str], response: dict) -> tuple[list[str], list[str]]:
    """Split `quotes` into (present in the response, not present).

    Matching rule, deliberately forgiving about form and strict about substance:
    both sides are lowercased, stripped of surrounding quote marks, have typographic
    look-alikes (curly quotes, en/em dashes, NBSP) folded to ASCII, and have all runs of
    whitespace collapsed to one space. A quote containing an ellipsis is split on it and
    each segment must appear **in order**, which is how a model elides a long span. So a
    genuine span survives re-casing, re-wrapping and elision, while a fabricated one —
    which shares no contiguous wording with the response — still fails.
    """
    hay = _normalise(_response_text(response))
    present: list[str] = []
    missing: list[str] = []
    for quote in quotes:
        target = present if _quote_in(quote, hay) else missing
        target.append(quote)
    return present, missing


def _quote_in(quote: str, hay: str) -> bool:
    segments = [s for s in (_normalise(p) for p in _ELLIPSIS.split(quote)) if s]
    if not segments:
        return False   # whitespace or bare "..." is not evidence
    at = 0
    for seg in segments:
        found = hay.find(seg, at)
        if found < 0:
            return False
        at = found + len(seg)
    return True


def _normalise(text: str) -> str:
    return _WS.sub(" ", str(text).translate(_LOOKALIKES).lower()).strip().strip('"\'')


def _response_text(response: dict) -> str:
    """Everything the judge could legitimately have quoted from."""
    parts = [str(response.get("text") or "")]
    body = response.get("json")
    if body is not None:
        # Serialised without escaping, so a quote of a JSON string value matches.
        parts.append(_flatten(body))
    return "\n".join(p for p in parts if p)


def _flatten(node: Any) -> str:
    if isinstance(node, dict):
        return "\n".join(f"{k} {_flatten(v)}" for k, v in node.items())
    if isinstance(node, list):
        return "\n".join(_flatten(v) for v in node)
    return str(node)


def _load_rubric(rubric: str | Path | dict) -> dict:
    if isinstance(rubric, dict):
        return rubric
    doc = yaml.safe_load(Path(rubric).read_text())
    if not isinstance(doc, dict):
        raise RubricError(f"rubric {rubric} is not a YAML mapping")
    return doc


def _build_prompt(dims: list[dict], response: dict, context: dict) -> str:
    dim_text = "\n".join(
        f"- {d.get('id')}: {d.get('question')} (scale: {d.get('scale')})" for d in dims)
    return (
        f"RUBRIC DIMENSIONS:\n{dim_text}\n\n"
        f"MODEL RESPONSE:\n{json.dumps(response.get('json') or response.get('text'))[:6000]}\n\n"
        f"CASE INPUT:\n{json.dumps(context.get('input'))[:2000]}"
    )


def _ask(provider: JudgeProvider, user: str, schema: dict) -> dict:
    out: ModelResponse = provider.complete(
        [{"role": "user", "content": user}],
        schema=schema,
        params={"system": _SYS, "temperature": 0.0})
    # Schema path first; the text fallback is for adapters not yet returning `json`.
    verdict = out.json if isinstance(out.json, dict) else _safe(out.text)
    return verdict if isinstance(verdict, dict) else {}


def _collect_scores(dims: list[dict], verdicts: list[dict]) -> dict[str, list[int]]:
    """{dim_id: [score from each sample that scored it]}. Unscored dims stay absent."""
    collected: dict[str, list[int]] = {}
    for verdict in verdicts:
        raw = verdict.get("scores")
        raw = raw if isinstance(raw, dict) else {}
        by_id = {str(k): v for k, v in raw.items()}
        for d in dims:
            did = str(d.get("id"))
            value = _as_int(by_id.get(did), None)
            if value is not None:
                collected.setdefault(did, []).append(value)
    return collected


def _reduce(per_dim: dict[str, list[int]]) -> tuple[dict[str, int], dict | None]:
    """Median score per dimension, plus the spread when there was more than one sample."""
    scores: dict[str, int] = {}
    spread: dict[str, dict] = {}
    multi = any(len(v) > 1 for v in per_dim.values())
    for did, values in per_dim.items():
        # Floor rather than round: a judge split between 1 and 2 has not agreed that the
        # dimension is met, so it does not get the benefit of the doubt.
        scores[did] = math.floor(statistics.median(values))
        if multi:
            spread[did] = {"samples": sorted(values), "median": scores[did],
                           "spread": max(values) - min(values)}
    if not multi:
        return scores, None
    consistency = {
        "samples": max((len(v) for v in per_dim.values()), default=0),
        "per_dimension": spread,
        "max_spread": max((v["spread"] for v in spread.values()), default=0),
    }
    consistency["agreed"] = consistency["max_spread"] == 0
    return scores, consistency


def _unique_quotes(verdicts: list[dict]) -> list[str]:
    quotes: list[str] = []
    seen: set[str] = set()
    for verdict in verdicts:
        raw = verdict.get("evidence_quotes")
        for quote in raw if isinstance(raw, list) else []:
            text = str(quote)
            if text not in seen:
                seen.add(text)
                quotes.append(text)
    return quotes


def _threshold(dim: dict, doc: dict) -> int:
    scale = dim.get("scale") or {}
    top = max((int(k) for k in scale), default=1)
    value = dim.get("min_score", doc.get("min_score", top))
    return _as_int(value, top)


def _normalised(dims: list[dict], scores: dict[str, int]) -> float | None:
    if not dims:
        return None
    total = sum(scores.get(str(d.get("id")), 0) for d in dims)
    maxtot = sum(max((int(k) for k in (d.get("scale") or {1: 1})), default=1) for d in dims)
    return round(total / maxtot, 3) if maxtot else None


def _message(verdicts: list[dict], failures: list[str]) -> str:
    rationale = next((str(v.get("rationale") or "") for v in verdicts if v.get("rationale")), "")
    if failures:
        return "; ".join(failures + ([rationale] if rationale else []))[:300]
    return rationale[:300]


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe(text: str | None):
    try:
        return json.loads(text or "")
    except (ValueError, TypeError):
        return None
