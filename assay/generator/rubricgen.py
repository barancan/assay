"""Judge intent -> rubric.

The old rubric "generator" emitted the same one-dimension template for every intent, so
every judge check asked the same vague question and no rubric carried an evidence
requirement or an output schema. This module asks the builder model for a real rubric —
anchored dimensions whose 0/1/2 levels describe observable properties of a response —
and validates the reply before it is written anywhere.

Validation is not politeness. Dimension ids become YAML keys and, downstream, path
components; an id like `../../etc/passwd` is a traversal vector. Anything that fails
validation gets one repair attempt and then falls back to `fallback_rubric`, which is
deterministic and is also the offline (`assay generate --offline`) path.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..judges import VERDICT_SCHEMA
from .interface import describe_for_prompt

# Slug-safe: what may appear in a YAML key and a file path component. Deliberately does
# not include '.' or '/', so no id can traverse out of generated/rubrics/.
_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,47}$")

# Words that grade rather than describe. A level built only from these ("mostly good",
# "partially meets") restates the verdict instead of saying what a grader would see —
# exactly the unanchored rubric this module exists to replace.
_VACUOUS = {"good", "bad", "ok", "okay", "fine", "poor", "great", "excellent", "yes",
            "no", "pass", "fail", "passing", "failing", "partial", "partially", "meets",
            "met", "unmet", "none", "acceptable", "unacceptable", "correct", "incorrect",
            "not", "very", "somewhat", "mostly", "fully", "overall", "quite", "the", "a",
            "an", "is", "are", "response", "answer", "output", "at", "all", "level"}

_MIN_LEVEL_CHARS = 12
_WORDS = re.compile(r"[a-z]+")
_LEVELS = ("0", "1", "2")

_PROMPT = (
    "You write evaluation rubrics for an LLM judge.\n"
    "Given ONE assertion about a system's responses, produce a rubric with 2 to 4 "
    "dimensions that together decide whether the assertion holds.\n"
    "Reply with ONLY JSON: {\"dimensions\": [{\"id\": str, \"question\": str, "
    "\"scale\": {\"0\": str, \"1\": str, \"2\": str}, \"min_score\": int}], "
    "\"require_evidence\": bool, \"samples\": int}.\n"
    "Rules:\n"
    "- `id` is a slug: letters, digits, underscore or dash only, 2-48 characters. "
    "No dots, no slashes, no spaces. Ids must be unique.\n"
    "- `question` is answerable by reading one response.\n"
    "- Each scale level describes what a grader would OBSERVE in the response at that "
    "level — concrete and checkable. Never write 'good', 'bad', 'partial' or similar.\n"
    "- `min_score` is the lowest level that still counts as passing, 0, 1 or 2.\n"
    "- `require_evidence` is true whenever the judgement can be supported by quoting "
    "the response.\n"
    "- `samples` is 1, or 3 when the judgement is subjective enough that one sample "
    "would be unreliable."
)


class RubricGenerationError(ValueError):
    """The model replied, but not with a rubric that can be used."""


def generate_rubric(intent: dict, llm, *, interface=None) -> dict:
    """Ask `llm` for a rubric for `intent`, repairing once, then falling back.

    Never raises: a build that reaches here already has a judge intent, and refusing to
    emit any rubric would leave a spec pointing at a file that does not exist.
    """
    if llm is None:
        return fallback_rubric(intent)
    prompt = _prompt_for(intent, interface)
    try:
        return _validate(_ask(llm, prompt), intent)
    except RubricGenerationError as e:
        problem = str(e)
    except Exception:
        return fallback_rubric(intent)   # the provider failed; there is nothing to repair
    try:
        repair = (
            f"{prompt}\n\nYour previous reply was rejected: {problem}\n"
            "Reply again with ONLY the corrected JSON object."
        )
        return _validate(_ask(llm, repair), intent)
    except Exception:
        return fallback_rubric(intent)


def fallback_rubric(intent: dict) -> dict:
    """Deterministic two-dimension rubric. Used offline and whenever the model fails.

    Still anchored: the levels describe what is on the page, not how good it felt.
    """
    assertion = str(intent.get("assertion") or "the requirement is met").strip()
    return _rubric({
        "dimensions": [
            {
                "id": "requirement_met",
                "question": f"Does the response satisfy: {assertion}?",
                "scale": {
                    0: "the response contradicts the assertion or does not address it",
                    1: "the response addresses the assertion but leaves part of it "
                       "unmet or ambiguous",
                    2: "the response fully satisfies the assertion, with nothing "
                       "missing or contradicted",
                },
                "min_score": 2,
            },
            {
                "id": "grounded_in_input",
                "question": "Is every factual claim in the response traceable to the "
                            "case input or to the response's own cited sources?",
                "scale": {
                    0: "the response states facts that appear in neither the input nor "
                       "any source it cites",
                    1: "most claims are traceable, but at least one is unsupported or "
                       "cites nothing",
                    2: "every claim is traceable to the input or to a source the "
                       "response names",
                },
                "min_score": 1,
            },
        ],
        "require_evidence": True,
        "samples": 1,
    })


def _prompt_for(intent: dict, interface) -> str:
    described = describe_for_prompt(interface) if interface is not None else ""
    parts = [_PROMPT, f"\nASSERTION:\n{intent.get('assertion') or intent.get('id')}"]
    if intent.get("category"):
        parts.append(f"CATEGORY: {intent['category']}")
    if described:
        parts.append(f"\n{described}")
    return "\n".join(parts)


def _ask(llm, prompt: str) -> Any:
    out = llm.complete([{"role": "user", "content": prompt}],
                       params={"temperature": 0.0, "max_tokens": 1200})
    body = getattr(out, "json", None)
    if isinstance(body, dict):
        return body
    return _parse(getattr(out, "text", None))


def _parse(text: str | None) -> Any:
    body = text or ""
    try:
        body = body[body.index("{"): body.rindex("}") + 1]
    except ValueError:
        raise RubricGenerationError("the model did not return a JSON object") from None
    try:
        return json.loads(body)
    except (ValueError, TypeError) as e:
        raise RubricGenerationError(f"invalid JSON: {e}") from None


def _validate(reply: Any, intent: dict) -> dict:
    if not isinstance(reply, dict):
        raise RubricGenerationError("the rubric must be a JSON object")
    raw_dims = reply.get("dimensions")
    if not isinstance(raw_dims, list) or len(raw_dims) < 2:
        raise RubricGenerationError("a rubric needs at least 2 dimensions")

    dims: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_dims, start=1):
        if not isinstance(raw, dict):
            raise RubricGenerationError(f"dimension {i} is not an object")
        ident = str(raw.get("id") or "").strip()
        if not _ID_OK.match(ident):
            raise RubricGenerationError(
                f"dimension id {ident!r} is not a slug (letters, digits, _ and - only)")
        if ident in seen:
            raise RubricGenerationError(f"duplicate dimension id {ident!r}")
        seen.add(ident)

        question = str(raw.get("question") or "").strip()
        if len(question) < 10:
            raise RubricGenerationError(f"dimension {ident!r} has no usable question")

        scale = _scale(ident, raw.get("scale"))
        top = max(scale)
        min_score = raw.get("min_score", reply.get("min_score", top))
        try:
            min_score = int(min_score)
        except (TypeError, ValueError):
            raise RubricGenerationError(
                f"dimension {ident!r} has a non-numeric min_score") from None
        if not 0 <= min_score <= top:
            raise RubricGenerationError(
                f"dimension {ident!r} has min_score {min_score} outside 0..{top}")
        dims.append({"id": ident, "question": question, "scale": scale,
                     "min_score": min_score})

    samples = reply.get("samples", 1)
    try:
        samples = max(1, min(int(samples), 5))
    except (TypeError, ValueError):
        samples = 1
    return _rubric({"dimensions": dims,
                    "require_evidence": bool(reply.get("require_evidence", True)),
                    "samples": samples})


def _scale(ident: str, raw: Any) -> dict[int, str]:
    if not isinstance(raw, dict):
        raise RubricGenerationError(f"dimension {ident!r} has no scale")
    by_level = {str(k).strip(): v for k, v in raw.items()}
    missing = [lvl for lvl in _LEVELS if lvl not in by_level]
    if missing:
        raise RubricGenerationError(
            f"dimension {ident!r} is missing scale level(s) {', '.join(missing)}")
    scale: dict[int, str] = {}
    for level in _LEVELS:
        text = str(by_level[level] or "").strip()
        # Anchors have to say what is observable: long enough to describe something, and
        # made of more than grading words.
        words = _WORDS.findall(text.lower())
        if len(text) < _MIN_LEVEL_CHARS or not words or all(w in _VACUOUS for w in words):
            raise RubricGenerationError(
                f"dimension {ident!r} level {level} is not an observable anchor: {text!r}")
        scale[int(level)] = text
    return scale


def _rubric(body: dict) -> dict:
    """Assemble the persisted shape. `output_schema` is what the judge asks for."""
    return {
        "judge": "primary",
        "dimensions": body["dimensions"],
        "require_evidence": body.get("require_evidence", True),
        "samples": body.get("samples", 1),
        "output_schema": VERDICT_SCHEMA,
    }
