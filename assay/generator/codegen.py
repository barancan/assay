"""Intent -> a Python check the sandbox can run.

This is the part of the product that was only ever a claim: when no vetted template
fits an assertion, Assay is supposed to *write* the check. Until now `generated`
intents produced a spec entry pointing at a file nobody wrote, and the run died in the
sandbox with "generated check not found".

The loop is generate -> validate -> dry-run -> repair:

  * `validate_source` is a static gate. It runs before anything executes, so a reply
    that imports `os` or crawls `__globals__` is rejected on the AST rather than
    discovered by the sandbox.
  * the dry-run then executes the candidate for real, via `run_generated_source`, which
    takes source text -- nothing is written or persisted until it has run.
  * a dry-run is not enough on its own. A check that returns `{"passed": True}`
    unconditionally passes every dry-run and tests nothing, so the candidate is also
    run against deliberately degraded responses and must reject at least one.
  * every rejection is fed back verbatim as a repair prompt, and every attempt's errors
    are recorded on the result so a failure is explainable rather than mysterious.

When the loop still cannot produce a valid check, the caller degrades the intent to a
judge check (see `generator.build.generated_sources_for`). The assertion still gets
tested, semantically instead of mechanically, and the reason is recorded.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field

from ..sandbox import run_generated_source
from ..sandbox.runner import _ALLOWED
from .interface import describe_for_prompt, sample_response

CHECK_CONTRACT = "def check(response: dict, context: dict) -> dict"

# Names that do not exist in the sandbox, or exist only as an escape hatch. `open`,
# `exec`, `eval` and `compile` are actually removed from builtins by the runner; the
# rest are here because a check has no legitimate use for them.
_BLOCKED_NAMES = {"__import__", "eval", "exec", "compile", "open", "globals", "locals",
                  "vars", "breakpoint", "input"}

# Dunder attributes that walk from a plain object back to the interpreter. Blocked as
# attribute access *and* as string constants, so `getattr(x, "__globals__")` is caught
# too.
_BLOCKED_ATTRS = {"__globals__", "__subclasses__", "__builtins__", "__class__",
                  "__bases__", "__mro__", "__code__", "__dict__", "__getattribute__",
                  "__reduce__", "__init_subclass__", "__loader__", "__spec__",
                  "__import__"}

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
_MAX_DEGRADE_DEPTH = 6

# A real check from examples/compliance-copilot. Shown to the model because the shape of
# a good check -- defensive reads, a bounded evidence dict, a message that says what was
# wrong -- is far easier to copy than to describe.
_EXAMPLE = '''def check(response: dict, context: dict) -> dict:
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
'''

_PROMPT = (
    "You write one deterministic Python check for an LLM evaluation harness.\n"
    "Reply with ONLY Python source. No prose, no explanation, no markdown fence.\n"
    f"\nThe module must define exactly this function, at module level:\n"
    f"    {CHECK_CONTRACT}\n"
    "\n`response` is the captured target response: "
    "{'text': str, 'json': parsed body or None, 'raw': ..., 'tool_calls': list, "
    "'latency_ms': float, 'usage': dict, 'cost_usd': float, 'status': str, "
    "'error': str|None}. The parsed body is `response['json']`.\n"
    "`context` is {'input': the case input dict, 'suite': str, 'case': str}.\n"
    "\nReturn a dict: {'passed': bool, 'score': float|None, "
    "'severity': 'info'|'warn'|'fail', 'message': str, 'evidence': dict}.\n"
    "\nThe check runs in a sandbox with no filesystem, no network and no subprocesses. "
    f"The only importable modules are: {', '.join(_ALLOWED)}. Nothing else resolves. "
    "Do not use __import__, eval, exec, compile, open, or dunder attribute access.\n"
    "\nRules:\n"
    "- Never raise. Read defensively with .get(), and treat a missing, empty or "
    "wrongly-typed field as passed=False with a message saying which field.\n"
    "- The check must actually discriminate: passed=True for a response that satisfies "
    "the assertion, passed=False for one that does not. A check that returns the same "
    "verdict for every response is rejected.\n"
    "- Put the offending values in `evidence`, truncated to a few entries.\n"
)


@dataclass
class GeneratedCheck:
    """One codegen attempt sequence, successful or not."""

    path: str
    source: str
    attempts: int
    dry_run: dict
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_source(source: str) -> list[str]:
    """Static problems with a candidate check, empty when it is clean.

    Runs before execution, so nothing here relies on the sandbox catching it. Every
    message is written to be handed straight back to the model as a repair instruction.
    """
    if not (source or "").strip():
        return ["the reply contained no Python source"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"the source does not parse: line {exc.lineno}: {exc.msg}"]

    problems: list[str] = []
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    top = [n for n in tree.body if isinstance(n, functions) and n.name == "check"]
    anywhere = [n for n in ast.walk(tree) if isinstance(n, functions) and n.name == "check"]

    if not top and anywhere:
        problems.append(
            "check() is nested inside another function or block; it must be defined at "
            "module level so the sandbox can find it")
    elif not top:
        problems.append(f"the module defines no check(); the contract is `{CHECK_CONTRACT}`")
    else:
        fn = top[-1]
        if isinstance(fn, ast.AsyncFunctionDef):
            problems.append("check() must be a plain function, not async")
        args = fn.args
        names = [a.arg for a in (*args.posonlyargs, *args.args)]
        if names != ["response", "context"]:
            problems.append(
                f"check() takes {names or 'no arguments'}; the contract is exactly two "
                "positional parameters named (response, context)")
        if args.vararg or args.kwarg or args.kwonlyargs:
            problems.append(
                "check() must not take *args, **kwargs or keyword-only arguments")

    allowed = ", ".join(_ALLOWED)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _ALLOWED:
                    problems.append(
                        f"import of '{alias.name}' is blocked in the sandbox; the only "
                        f"importable modules are: {allowed}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append("relative imports do not resolve in the sandbox")
            elif (node.module or "").split(".")[0] not in _ALLOWED:
                problems.append(
                    f"import of '{node.module}' is blocked in the sandbox; the only "
                    f"importable modules are: {allowed}")
        elif isinstance(node, ast.Name) and node.id in _BLOCKED_NAMES:
            problems.append(f"`{node.id}` is not available in the sandbox")
        elif isinstance(node, ast.Attribute) and node.attr in _BLOCKED_ATTRS:
            problems.append(
                f"attribute `{node.attr}` is a sandbox-escape vector and is not allowed")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and node.value in _BLOCKED_ATTRS):
            problems.append(
                f"the string {node.value!r} reaches a blocked attribute by name and is "
                "not allowed")

    # Order-preserving dedupe: one import of `os` in two places is one problem.
    return list(dict.fromkeys(problems))


def generate_check(intent: dict, iface, llm, *, max_repairs: int = 2,
                   sample: dict | None = None) -> GeneratedCheck:
    """Ask `llm` for a check for `intent`, validating and dry-running every candidate.

    Never raises: the caller decides what a failure means (build degrades the intent to
    a judge check, the review screen surfaces the errors). `attempts` is 1 + the number
    of repairs actually used, and `errors` carries every attempt's problems in order.
    """
    ident = str(intent.get("id") or "check")
    path = f"generated/checks/{ident}.py"
    good = _response_for(iface, sample)
    context = _context_for(iface)
    base = _prompt_for(intent, iface, good)

    errors: list[str] = []
    source, outcome = "", {}
    prompt = base
    for attempt in range(1, max_repairs + 2):
        try:
            source = _extract_source(_ask(llm, prompt))
        except Exception as exc:
            # The provider itself failed. There is no reply to repair, so stop here.
            errors.append(f"attempt {attempt}: the builder model failed: "
                          f"{type(exc).__name__}: {exc}")
            return GeneratedCheck(path, source, attempt, outcome, False, errors)

        problems = validate_source(source)
        outcome = {}
        if not problems:
            outcome, problems = _dry_run(source, good, context, path, llm)
        if not problems:
            return GeneratedCheck(path, source, attempt, outcome, True, errors)

        errors.extend(f"attempt {attempt}: {p}" for p in problems)
        prompt = _repair_prompt(base, source, problems)

    return GeneratedCheck(path, source, max_repairs + 1, outcome, False, errors)


def _dry_run(source: str, good: dict, context: dict, path: str,
             llm=None) -> tuple[dict, list[str]]:
    """Execute the candidate, then prove it is testing something.

    Passing the nominal sample only shows the module loads and returns a verdict; a
    `return {"passed": True}` does that perfectly. So the check is also run against
    responses it ought to reject, and has to reject at least one.

    Those come from two places, cheap first:

      * degradations of the nominal response -- content ruined, then the body emptied.
        These catch anything that reads a field and expects it to be present or sane.
      * failing that, a counter-example asked of the model that wrote the check. Generic
        degradation cannot express a *conditional* assertion: "blocked findings must be
        high severity" is perfectly satisfied by a response with no blocked findings, so
        a correct check would look vacuous. Its author knows what a violation looks like.
        A check that cannot reject even its own counter-example is not checking anything,
        so the gate still closes on the degenerate case.

    Each sample is a sandbox subprocess, so we stop at the first rejection.
    """
    raw = run_generated_source(source, good, context, origin=path)
    if not isinstance(raw, dict) or "passed" not in raw:
        return {}, ["check() did not return a dict containing a 'passed' key"]
    if not raw.get("passed"):
        return _outcome(raw, None), [
            "the check rejected the nominal sample response it was shown; it must "
            f"return passed=True for that response. It said: "
            f"{raw.get('message') or '(no message)'}"]

    def rejects(candidate: dict) -> bool:
        out = run_generated_source(source, candidate, context, origin=path)
        return isinstance(out, dict) and not out.get("passed")

    for bad in _bad_samples(good):
        if rejects(bad):
            return _outcome(raw, True), []

    if llm is not None:
        counter = _counterexample(llm, source, good)
        if counter is not None and rejects(counter):
            return _outcome(raw, True), []

    return _outcome(raw, False), [
        "the check passed every response it should have rejected -- the nominal one, a "
        "degraded one, an empty one, and a counter-example -- so it is not testing the "
        "assertion. Read the specific fields the assertion is about and return "
        "passed=False when they are missing, empty or violate it"]


def _counterexample(llm, source: str, good: dict) -> dict | None:
    """Ask the check's author for a response the check should reject. None if unusable."""
    prompt = (
        "Here is a check you wrote:\n\n"
        f"{source}\n"
        "Here is a response it correctly passes:\n"
        f"{_as_text(good.get('json') if good.get('json') is not None else good)[:1200]}\n\n"
        "Reply with ONLY a JSON object: a response body of the SAME SHAPE that this "
        "check must REJECT (passed=False). Change the values the check actually reads. "
        "No prose, no markdown fence."
    )
    try:
        text = _ask(llm, prompt)
        body = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        return None
    if not isinstance(body, (dict, list)):
        return None
    return _response_for(None, sample=body)


def _outcome(raw: dict, discriminates: bool | None) -> dict:
    """The dry-run record persisted in build_meta and shown on the review screen.

    Deliberately trimmed: this lands in the pipeline config JSON, so an evidence dict of
    unbounded size does not belong in it.
    """
    return {
        "passed": bool(raw.get("passed")),
        "severity": str(raw.get("severity") or "info"),
        "message": str(raw.get("message") or "")[:300],
        "discriminates": discriminates,
    }


def _bad_samples(good: dict) -> list[dict]:
    """Responses a check worth keeping should reject, derived from the nominal one.

    Two degradations: the body with its shape intact but its content ruined, then the
    body emptied entirely. The first probes content ("this field must match X"), the
    second probes presence and cardinality ("there must be at least one citation").
    """
    body = good.get("json")
    if body is None:
        return [{**good, "text": ""},
                {**good, "text": "", "status": "error", "error": "target returned nothing"}]
    ruined = _degrade(body)
    return [{**good, "json": ruined, "text": _as_text(ruined)},
            {**good, "json": {} if isinstance(body, dict) else [], "text": ""}]


def _degrade(value, depth: int = 0):
    """Same shape, worthless content.

    Keys and list lengths survive, so a check that iterates still has something to
    iterate over and fails on the values rather than simply finding nothing.
    """
    if depth > _MAX_DEGRADE_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _degrade(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_degrade(v, depth + 1) for v in value]
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return -abs(value) - 1
    if isinstance(value, str):
        return ""
    return value


def _response_for(iface, sample: dict | None = None) -> dict:
    """The response dict the dry-run hands the check.

    `sample_response` yields the response *body* when the interface carries a schema, but
    a check is handed the adapter envelope (`ModelResponse.as_dict()`) with the body under
    `json`. Wrap unless what we were given is already an envelope.
    """
    body = sample if sample is not None else sample_response(iface)
    if isinstance(body, dict) and "text" in body and "json" in body:
        return dict(body)
    return {"text": _as_text(body), "raw": body, "json": body, "tool_calls": [],
            "latency_ms": 120.0, "usage": {}, "cost_usd": 0.0, "status": "ok",
            "error": None}


def _context_for(iface) -> dict:
    fields = getattr(iface, "input_fields", None) or []
    return {"input": {f: "sample" for f in fields}, "suite": "dry-run", "case": "dry-run"}


def _as_text(body) -> str:
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, default=str)
    except (TypeError, ValueError):
        return str(body)


def _prompt_for(intent: dict, iface, good: dict) -> str:
    parts = [_PROMPT, f"\nASSERTION TO CHECK:\n{intent.get('assertion') or intent.get('id')}"]
    if intent.get("category"):
        parts.append(f"CATEGORY: {intent['category']}")
    if intent.get("rationale"):
        parts.append(f"WHY IT MATTERS: {intent['rationale']}")
    described = describe_for_prompt(iface) if iface is not None else ""
    if described:
        parts.append(f"\n{described}")
    parts.append(
        "\nThis exact response will be used to dry-run your check, and your check MUST "
        f"return passed=True for it:\n{_as_text(good)[:1500]}")
    parts.append(f"\nA REAL CHECK, FOR SHAPE:\n{_EXAMPLE}")
    return "\n".join(parts)


def _repair_prompt(base: str, source: str, problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (f"{base}\n\nYour previous attempt was REJECTED for these reasons:\n{listed}\n"
            f"\nThat attempt was:\n{source}\n"
            "\nReply again with ONLY the corrected Python source.")


def _ask(llm, prompt: str) -> str:
    out = llm.complete([{"role": "user", "content": prompt}],
                       params={"temperature": 0.0, "max_tokens": 1500})
    return getattr(out, "text", "") or ""


def _extract_source(text: str) -> str:
    """Pull Python out of a reply that may or may not have obeyed 'no markdown fence'."""
    match = _FENCE.search(text or "")
    body = match.group(1) if match else (text or "")
    return body.strip() + "\n"
