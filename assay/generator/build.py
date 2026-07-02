"""Requirements -> pipeline. LLM-assisted when a judge provider is given;
otherwise a deterministic offline heuristic so the tool works with no keys.

The full LLM generator (intent derivation, route decision, codegen, rubric gen)
is documented in the design; this module implements a working v0 of the loop.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

_INTENT_PROMPT = (
    "You convert software/model assessment requirements into a test pipeline.\n"
    "For EACH atomic requirement, output one or more test intents as JSON list. "
    "Each intent: {id, requirement_ref, category, assertion, how: 'template'|'generated'|'judge', "
    "template?: name, params?: object, threshold?: float, rationale}. "
    "Use a deterministic template when the assertion is mechanically checkable "
    "(valid_json, json_schema, contains, not_contains, regex_match, numeric_bound, "
    "latency_bound, field_present, citation_present, refusal_detector, pii_absent). "
    "Use 'judge' with a threshold (0-1) for graded semantic metrics: "
    "faithfulness 0.80, answer_relevance 0.80, context_precision 0.75, context_recall 0.75, "
    "hallucination 0.85, toxicity_free 0.90, task_completion 0.80. "
    "Use 'generated' only when no template or catalogue metric fits. "
    "Reply with ONLY the JSON list."
)

# Keyword → (assertion, threshold) for graded judge metrics
_JUDGE_KEYWORDS: list[tuple[str, str, str, float]] = [
    # pattern, id-suffix, assertion, threshold
    (r"hallucin|fabricat|invent",  "H-hallucination", "response contains no hallucinated facts", 0.85),
    (r"toxic|harm|offensive",      "H-toxicity",      "output is free from toxic or harmful language", 0.90),
    (r"faithful|rag|retriev",      "H-faithfulness",  "answer is faithful to the provided context", 0.80),
    (r"relevance|relevant",        "H-relevance",     "answer directly addresses the question", 0.80),
    (r"complet|finish.*task",      "H-completion",    "response fully completes the requested task", 0.80),
    (r"refus|uncertain|decline",   "H-refusal",       "flags uncertainty rather than over-asserting", 0.85),
]


def _heuristic_intents(requirements: str) -> list[dict]:
    """No-LLM fallback: baseline checks + keyword-driven catalogue metrics."""
    intents = [
        {"id": "H-json", "requirement_ref": "auto", "category": "format",
         "assertion": "response is valid JSON", "how": "template",
         "template": "valid_json", "params": {}},
        {"id": "H-latency", "requirement_ref": "auto", "category": "latency",
         "assertion": "responds under 5s", "how": "template",
         "template": "latency_bound", "params": {"max_ms": 5000}},
    ]
    if re.search(r"cit(e|ation)|article", requirements, re.I):
        intents.append({"id": "H-cite", "requirement_ref": "auto", "category": "correctness",
                        "assertion": "findings cite an article", "how": "template",
                        "template": "citation_present",
                        "params": {"field": "$.findings[*].article", "min": 1}})
    if re.search(r"pii|personal.*info|identif", requirements, re.I):
        intents.append({"id": "H-pii", "requirement_ref": "auto", "category": "safety",
                        "assertion": "no PII in output", "how": "template",
                        "template": "pii_absent", "params": {}})
    seen_ids: set[str] = set()
    for pattern, sid, assertion, threshold in _JUDGE_KEYWORDS:
        if sid not in seen_ids and re.search(pattern, requirements, re.I):
            intents.append({"id": sid, "requirement_ref": "auto", "category": "quality",
                            "assertion": assertion, "how": "judge", "threshold": threshold})
            seen_ids.add(sid)
    return intents


def derive_intents(requirements: str, judge=None) -> list[dict]:
    if judge is None:
        return _heuristic_intents(requirements)
    out = judge.complete(
        [{"role": "user", "content": f"{_INTENT_PROMPT}\n\nREQUIREMENTS:\n{requirements}"}],
        params={"temperature": 0.0, "max_tokens": 2000})
    try:
        text = out.text or ""
        text = text[text.index("["): text.rindex("]") + 1]
        return json.loads(text)
    except (ValueError, TypeError):
        return _heuristic_intents(requirements)


def intents_to_spec(project: str, intents: list[dict], target: dict,
                    judges: dict) -> dict:
    cases = []
    for it in intents:
        check = {"type": it["how"]}
        if it["how"] == "template":
            check["uses"] = it.get("template")
            check["with"] = it.get("params", {})
        elif it["how"] == "judge":
            check["judge"] = "primary"
            check["rubric"] = f"generated/rubrics/{it['id']}.yaml"
            with_dict: dict = {}
            if it.get("threshold") is not None:
                with_dict["threshold"] = it["threshold"]
            check["with"] = with_dict
        else:  # generated
            check["uses"] = f"generated/checks/{it['id']}.py"
        cases.append({"id": it["id"], "input": {}, "checks": [check]})
    return {
        "version": 1, "project": project, "target": target,
        "judges": judges or {"primary": {"provider": "mock", "model": "mock"}},
        "suites": [{"id": "generated", "requirement_ref": "requirements.md", "cases": cases}],
        "gating": {"fail_run_if": "any required check fails"},
    }


def build_pipeline_to_db(
    requirements_path: str,
    target: dict,
    out_dir: str = ".",       # kept for API parity with build_pipeline; unused
    judge=None,
    judges: dict | None = None,
    project: str = "project",
    created_by: str | None = None,
) -> int:
    """Generate pipeline from requirements and persist as a draft PipelineVersion in DB.

    Returns the new pipeline_version_id.
    """
    import yaml
    from ..pipeline import create_version
    from ..store import session_scope
    from ..store.models import Pipeline

    requirements = Path(requirements_path).read_text()
    intents = derive_intents(requirements, judge)
    spec_dict = intents_to_spec(project, intents, target, judges or {})

    rubrics: dict[str, str] = {}
    for it in intents:
        if it["how"] == "judge":
            rubric = {
                "judge": "primary",
                "dimensions": [{
                    "id": "judgment",
                    "question": it["assertion"],
                    "scale": {0: "fails", 1: "partial", 2: "meets"},
                    "min_score": 2,
                }],
            }
            path = f"generated/rubrics/{it['id']}.yaml"
            rubrics[path] = yaml.safe_dump(rubric, sort_keys=False)

    # generated_sources is empty in v0 — codegen not yet implemented.
    generated_sources: dict[str, str] = {}

    with session_scope() as s:
        pipeline = s.query(Pipeline).filter_by(project=project, name=project).one_or_none()
        if pipeline is None:
            pipeline = Pipeline(project=project, name=project, created_by=created_by)
            s.add(pipeline)
            s.flush()
        pid = pipeline.id

    pv = create_version(pid, spec_dict, generated_sources, rubrics, created_by)
    return pv.id


def build_pipeline(requirements_path: str, target: dict, out_dir: str,
                   judge=None, judges: dict | None = None, project: str = "project") -> str:
    requirements = Path(requirements_path).read_text()
    intents = derive_intents(requirements, judge)
    spec = intents_to_spec(project, intents, target, judges or {})
    out = Path(out_dir)
    (out / "generated" / "rubrics").mkdir(parents=True, exist_ok=True)
    (out / "generated" / "checks").mkdir(parents=True, exist_ok=True)
    import yaml
    spec_path = out / "assay.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
    # write rubric stubs for judge intents
    for it in intents:
        if it["how"] == "judge":
            rubric = {"judge": "primary",
                      "dimensions": [{"id": "judgment", "question": it["assertion"],
                                      "scale": {0: "fails", 1: "partial", 2: "meets"},
                                      "min_score": 2}]}
            (out / "generated" / "rubrics" / f"{it['id']}.yaml").write_text(
                yaml.safe_dump(rubric, sort_keys=False))
    return str(spec_path)
