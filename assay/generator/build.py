"""Requirements -> pipeline.

A real model does the derivation. The offline keyword heuristic is still here, but it is
opt-in (`allow_heuristic=True`, `assay generate --offline`) rather than a silent fallback:
a pipeline built from keywords looks identical to one built from comprehension, and that
is exactly the confusion this module used to create.

The full LLM generator (route decision, codegen, rubric gen) is documented in the design;
this module implements intent derivation and spec assembly.
"""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path

from ..checks.library import REGISTRY as _TEMPLATES
from ..llm.provider import LLMConfigError
from .casegen import dataset_to_cases, deterministic_cases, generate_cases, load_dataset
from .ingest import format_for_prompt, split_requirements
from .interface import Interface, parse_interface

_INTENT_PROMPT = (
    "You convert software/model assessment requirements into a test pipeline.\n"
    "For EACH atomic requirement, output one or more test intents as JSON list. "
    "Each intent: {id, requirement_ref, category, assertion, how: 'template'|'generated'|'judge', "
    "template?: name, params?: object, threshold?: float, rationale}. "
    "`requirement_ref` MUST be exactly one of the requirement ids listed below — never "
    "invent an id and never write 'auto'. `id` must be unique and use only letters, "
    "digits, dots, dashes and underscores. "
    "Use a deterministic template when the assertion is mechanically checkable "
    "(valid_json, json_schema, contains, not_contains, regex_match, numeric_bound, "
    "latency_bound, field_present, citation_present, refusal_detector, pii_absent). "
    "Use 'judge' with a threshold (0-1) for graded semantic metrics: "
    "faithfulness 0.80, answer_relevance 0.80, context_precision 0.75, context_recall 0.75, "
    "hallucination 0.85, toxicity_free 0.90, task_completion 0.80. "
    "Use 'generated' only when no template or catalogue metric fits. "
    "Reply with ONLY the JSON list."
)

_HOWS = ("template", "generated", "judge")
_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "must", "should", "be", "is", "are", "of", "to", "in", "and", "or",
    "not", "no", "for", "that", "this", "it", "its", "with", "at", "on", "any", "each",
    "every", "response", "responses", "model", "output", "outputs",
}

# Keyword → deterministic template intents, applied per requirement.
_TEMPLATE_KEYWORDS: list[tuple[str, str, str, str, dict]] = [
    # pattern, id-suffix, assertion, template, params
    (r"\bjson\b|schema",                "json",    "response is valid JSON",
     "valid_json", {}),
    (r"latenc|second|\bms\b|p9\d|response time|under \d",  "latency",
     "responds within the latency budget", "latency_bound", {"max_ms": 5000}),
    (r"cit(e|es|ed|ation)|article|source",  "cite",   "findings cite an article",
     "citation_present", {"field": "$.findings[*].article", "min": 1}),
    (r"\bpii\b|personal.*info|identif",  "pii",     "no PII in output",
     "pii_absent", {}),
]

# Keyword → (assertion, threshold) for graded judge metrics
_JUDGE_KEYWORDS: list[tuple[str, str, str, float]] = [
    # pattern, id-suffix, assertion, threshold
    (r"hallucin|fabricat|invent",  "hallucination", "response contains no hallucinated facts", 0.85),
    (r"toxic|harm|offensive",      "toxicity",      "output is free from toxic or harmful language", 0.90),
    (r"faithful|rag|retriev",      "faithfulness",  "answer is faithful to the provided context", 0.80),
    (r"relevance|relevant",        "relevance",     "answer directly addresses the question", 0.80),
    (r"complete[sd]? the .*task|task completion|finish.*task", "completion",
     "response fully completes the requested task", 0.80),
    (r"refus|uncertain|decline",   "refusal",       "flags uncertainty rather than over-asserting", 0.85),
]


class IntentDerivationError(ValueError):
    """The model replied, but not with intents this pipeline can be built from."""


def _heuristic_intents(requirements: list[dict]) -> list[dict]:
    """Opt-in offline path: baseline checks + keyword-driven catalogue metrics.

    Each intent is tagged with the id of the requirement whose text triggered it, so the
    coverage matrix is real even without a model.
    """
    intents: list[dict] = []
    seen: set[str] = set()

    def add(intent: dict) -> None:
        if intent["id"] not in seen:
            seen.add(intent["id"])
            intents.append(intent)

    for req in requirements:
        text = req["text"]
        for pattern, suffix, assertion, template, params in _TEMPLATE_KEYWORDS:
            if re.search(pattern, text, re.I):
                add({"id": f"H-{req['id']}-{suffix}", "requirement_ref": req["id"],
                     "category": "auto", "assertion": assertion, "how": "template",
                     "template": template, "params": dict(params)})
        for pattern, suffix, assertion, threshold in _JUDGE_KEYWORDS:
            if re.search(pattern, text, re.I):
                add({"id": f"H-{req['id']}-{suffix}", "requirement_ref": req["id"],
                     "category": "quality", "assertion": assertion, "how": "judge",
                     "threshold": threshold})

    if not intents and requirements:
        # Nothing matched. Still emit the two baselines so the offline path always
        # produces a runnable pipeline rather than an empty one.
        ref = requirements[0]["id"]
        intents = [
            {"id": f"H-{ref}-json", "requirement_ref": ref, "category": "format",
             "assertion": "response is valid JSON", "how": "template",
             "template": "valid_json", "params": {}},
            {"id": f"H-{ref}-latency", "requirement_ref": ref, "category": "latency",
             "assertion": "responds under 5s", "how": "template",
             "template": "latency_bound", "params": {"max_ms": 5000}},
        ]
    return intents


def _parse_intent_json(text: str | None) -> list:
    """Pull the JSON list out of a model reply. Raises IntentDerivationError."""
    body = text or ""
    try:
        body = body[body.index("["): body.rindex("]") + 1]
    except ValueError:
        raise IntentDerivationError(
            "the builder model did not return a JSON list of intents") from None
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError) as e:
        raise IntentDerivationError(f"the builder model returned invalid JSON: {e}") from None
    if not isinstance(parsed, list):
        raise IntentDerivationError("the builder model returned JSON, but not a list")
    return parsed


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _resolve_ref(raw, intent: dict, requirements: list[dict]) -> str:
    """Map whatever the model wrote in requirement_ref onto a real requirement id.

    Repairs the near-misses (case, "Req 2", "R2 — valid JSON") and falls back to the
    best textual match. Raises when nothing plausible fits, because persisting a
    dangling ref is what made the coverage matrix meaningless in the first place.
    """
    by_id = {r["id"]: r for r in requirements}
    candidate = str(raw or "").strip()
    if candidate in by_id:
        return candidate
    upper = candidate.upper().replace(" ", "")
    if upper in by_id:
        return upper
    match = re.search(r"R?(\d+)", upper)
    if match and f"R{int(match.group(1))}" in by_id:
        return f"R{int(match.group(1))}"
    # Best-effort: whichever requirement shares the most words with the assertion.
    wanted = _tokens(f"{candidate} {intent.get('assertion', '')}")
    if wanted:
        best, best_id = 0, None
        for r in requirements:
            overlap = len(wanted & _tokens(r["text"]))
            if overlap > best:
                best, best_id = overlap, r["id"]
        if best_id:
            return best_id
    if len(requirements) == 1:
        return requirements[0]["id"]
    raise IntentDerivationError(
        f"intent {intent.get('id') or '?'} references unknown requirement "
        f"{candidate!r}; known ids: {', '.join(by_id)}")


def _validate_intents(parsed: list, requirements: list[dict]) -> list[dict]:
    """Reject or repair model output before it becomes a persisted pipeline."""
    if not parsed:
        raise IntentDerivationError("the builder model returned no intents")
    intents: list[dict] = []
    used_ids: set[str] = set()
    for i, raw in enumerate(parsed, start=1):
        if not isinstance(raw, dict):
            raise IntentDerivationError(f"intent {i} is not an object")
        how = str(raw.get("how") or "").strip().lower()
        if how not in _HOWS:
            raise IntentDerivationError(
                f"intent {i} has how={raw.get('how')!r}; expected one of {', '.join(_HOWS)}")

        # Ids land in file paths (generated/rubrics/<id>.yaml), so they are sanitised
        # rather than trusted.
        ident = _ID_SAFE.sub("-", str(raw.get("id") or "").strip()).strip("-.") or f"I{i}"
        while ident in used_ids:
            ident = f"{ident}-{i}"
        used_ids.add(ident)

        intent = {
            "id": ident,
            "requirement_ref": None,
            "category": str(raw.get("category") or "auto"),
            "assertion": str(raw.get("assertion") or "").strip() or f"intent {ident}",
            "how": how,
        }
        intent["requirement_ref"] = _resolve_ref(raw.get("requirement_ref"), intent, requirements)

        if how == "template":
            template = str(raw.get("template") or raw.get("uses") or "").strip()
            if template not in _TEMPLATES:
                raise IntentDerivationError(
                    f"intent {ident} asks for unknown template {template!r}; "
                    f"available: {', '.join(sorted(_TEMPLATES))}")
            intent["template"] = template
            params = raw.get("params")
            intent["params"] = params if isinstance(params, dict) else {}
        elif how == "judge":
            try:
                threshold = float(raw["threshold"])
            except (KeyError, TypeError, ValueError):
                threshold = None
            if threshold is not None:
                intent["threshold"] = min(max(threshold, 0.0), 1.0)
        if raw.get("rationale"):
            intent["rationale"] = str(raw["rationale"])
        intents.append(intent)
    return intents


def derive_intents(requirements: str, judge=None, *, allow_heuristic: bool = False) -> list[dict]:
    """Turn requirements prose into validated test intents.

    `judge` is the builder model. Without one this raises unless the caller has
    explicitly asked for the offline heuristic -- silently degrading to keyword matching
    is what made every UI-built pipeline indistinguishable from a real one.
    """
    reqs = split_requirements(requirements)
    if judge is None:
        if not allow_heuristic:
            raise LLMConfigError(
                "no builder model available: configure one in Settings, or use the "
                "offline heuristic explicitly (assay generate --offline)",
                adapter="unconfigured",
            )
        return _heuristic_intents(reqs)
    if not reqs:
        raise IntentDerivationError("no requirements to derive intents from")
    prompt = (
        f"{_INTENT_PROMPT}\n\nREQUIREMENTS (cite these ids in requirement_ref):\n"
        f"{format_for_prompt(reqs)}"
    )
    out = judge.complete([{"role": "user", "content": prompt}],
                         params={"temperature": 0.0, "max_tokens": 2000})
    return _validate_intents(_parse_intent_json(getattr(out, "text", None)), reqs)


def _cases_for(intent: dict, iface, cases_by_intent: dict | None) -> list[dict]:
    """The cases to emit for one intent, guaranteed non-empty and with real inputs.

    This is the last gate before a case is persisted: a case with an empty input runs
    the target against nothing, which is the bug this phase exists to close.
    """
    supplied = (cases_by_intent or {}).get(intent["id"]) or []
    cases = [c for c in supplied if isinstance(c.get("input"), dict) and c["input"]]
    return cases or deterministic_cases(intent, iface, n=1)


def _judges_block(judges: dict | None, has_judge_check: bool) -> dict:
    """The `judges:` block for a generated spec.

    This used to substitute {"primary": {"provider": "mock", ...}} whenever none was
    configured, which is how an unconfigured workspace produced a pipeline whose graded
    checks all passed for free. The stand-in now survives only where mocks are allowed at
    all (see adapters.registry.mock_allowed); anywhere else, a judge check with no judge
    is an error that names what to set.
    """
    from ..adapters.registry import mock_allowed
    if judges:
        return judges
    if mock_allowed():
        return {"primary": {"provider": "mock", "model": "mock"}}
    if has_judge_check:
        raise LLMConfigError(
            "this pipeline has graded (judge) checks but no judge model is configured, "
            "and Assay will not stand in a mock one: a mock judge passes everything, so "
            "the report would be green and mean nothing.\n"
            "Configure a judge under Settings > Providers (set $ANTHROPIC_API_KEY or "
            "$OPENAI_API_KEY first), or pass --judge provider:model to `assay generate`.",
            adapter="unconfigured",
        )
    return {}


def intents_to_spec(project: str, intents: list[dict], target: dict,
                    judges: dict, iface=None,
                    cases_by_intent: dict | None = None) -> dict:
    judges = _judges_block(judges, any(it["how"] == "judge" for it in intents))
    iface = iface if iface is not None else Interface()
    suites: dict[str, list] = {}
    used_ids: set[str] = set()
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
        ref = it.get("requirement_ref") or "unmapped"
        for case in _cases_for(it, iface, cases_by_intent):
            # Case ids are a persisted column (CaseResult.case_id, 120 chars), so the
            # composed id is bounded rather than however long the model felt like.
            case_id = _ID_SAFE.sub("-", f"{it['id']}-{case['id']}").strip("-.")[:100]
            while case_id in used_ids:
                case_id = f"{case_id}-{len(used_ids)}"
            used_ids.add(case_id)
            # A copy per case: sharing one dict makes yaml.safe_dump emit anchors and
            # aliases, and a spec humans are meant to review should read plainly.
            emitted = {"id": case_id, "input": case["input"],
                       "checks": [copy.deepcopy(check)]}
            if isinstance(case.get("context"), dict) and case["context"]:
                emitted["context"] = case["context"]
            suites.setdefault(ref, []).append(emitted)
    return {
        "version": 1, "project": project, "target": target,
        "judges": judges,
        # One suite per requirement: the coverage matrix keys off suite.requirement_ref.
        "suites": [{"id": ref, "requirement_ref": ref, "cases": cases}
                   for ref, cases in suites.items()],
        "gating": {"fail_run_if": "any required check fails"},
    }


def rubric_for(intent: dict, llm=None, *, interface=None) -> dict:
    """Rubric for a judge intent: model-authored when a builder LLM is available.

    Without one -- the `--offline` path, and any caller that has no model -- this is the
    deterministic fallback rather than a silent one-dimension stub.
    """
    from .rubricgen import fallback_rubric, generate_rubric
    if llm is None:
        return fallback_rubric(intent)
    return generate_rubric(intent, llm, interface=interface)


def cases_for_intents(intents: list[dict], iface, judge=None, *,
                      dataset: str | None = None, n: int = 3) -> dict[str, list[dict]]:
    """Inputs for every intent: bound to a dataset when one is given, generated otherwise.

    A golden dataset is the alternative to generation, not a supplement to it -- when the
    user supplies real cases, inventing more of them is noise.
    """
    if dataset:
        bound = dataset_to_cases(load_dataset(dataset))
        return {it["id"]: bound for it in intents}
    return {it["id"]: generate_cases(it, iface, judge, n=n) for it in intents}


def resolve_interface(interface_path: str | None, target: dict):
    """Parse the interface the pipeline is grounded on, falling back to the target's own.

    `parse_interface` is deliberately forgiving -- a document it cannot make sense of
    yields an ungrounded Interface rather than taking the build down. A path the user
    explicitly supplied and that does not exist is a different thing: silently building
    an ungrounded pipeline would give them exactly what they asked not to have, with no
    signal. So existence is checked here, at the boundary, and content is not.
    """
    path = interface_path or (target or {}).get("import")
    if path and not Path(path).exists():
        raise FileNotFoundError(f"interface file not found: {path}")
    return parse_interface(path)


def build_pipeline_to_db(
    requirements_path: str,
    target: dict,
    out_dir: str = ".",       # kept for API parity with build_pipeline; unused
    judge=None,
    judges: dict | None = None,
    project: str = "project",
    created_by: str | None = None,
    allow_heuristic: bool = False,
    interface_path: str | None = None,
    dataset: str | None = None,
) -> int:
    """Generate pipeline from requirements and persist as a draft PipelineVersion in DB.

    Returns the new pipeline_version_id.
    """
    import yaml
    from ..pipeline import create_version
    from ..store import session_scope
    from ..store.models import Pipeline

    requirements = Path(requirements_path).read_text()
    intents = derive_intents(requirements, judge, allow_heuristic=allow_heuristic)
    iface = resolve_interface(interface_path, target)
    cases = cases_for_intents(intents, iface, judge, dataset=dataset)
    spec_dict = intents_to_spec(project, intents, target, judges or {},
                                iface=iface, cases_by_intent=cases)

    rubrics: dict[str, str] = {}
    for it in intents:
        if it["how"] == "judge":
            path = f"generated/rubrics/{it['id']}.yaml"
            rubrics[path] = yaml.safe_dump(rubric_for(it, judge), sort_keys=False)

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
                   judge=None, judges: dict | None = None, project: str = "project",
                   allow_heuristic: bool = False, interface_path: str | None = None,
                   dataset: str | None = None) -> str:
    requirements = Path(requirements_path).read_text()
    intents = derive_intents(requirements, judge, allow_heuristic=allow_heuristic)
    iface = resolve_interface(interface_path, target)
    cases = cases_for_intents(intents, iface, judge, dataset=dataset)
    spec = intents_to_spec(project, intents, target, judges or {},
                           iface=iface, cases_by_intent=cases)
    out = Path(out_dir)
    (out / "generated" / "rubrics").mkdir(parents=True, exist_ok=True)
    (out / "generated" / "checks").mkdir(parents=True, exist_ok=True)
    import yaml
    spec_path = out / "assay.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
    # write rubric stubs for judge intents
    for it in intents:
        if it["how"] == "judge":
            (out / "generated" / "rubrics" / f"{it['id']}.yaml").write_text(
                yaml.safe_dump(rubric_for(it, judge), sort_keys=False))
    return str(spec_path)
