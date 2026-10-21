"""P4: the model writes the check.

`generated` intents used to produce a spec entry pointing at a file nobody wrote, and
the run died in the sandbox with "generated check not found". These tests cover the
loop that closes that: static validation, a real dry-run, repair, and -- when the model
still cannot get there -- an honest degradation to a judge check with the reason kept.

Every LLM here is a scripted double (same shape as the doubles in conftest.py). No
network, no keys.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assay.adapters.base import ModelResponse

# ── doubles ─────────────────────────────────────────────────────────────────


class ScriptedLLM:
    """Replays `replies` one per call. The last reply repeats once exhausted."""

    name = "fake-codegen"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or [""]
        self.prompts: list[str] = []

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        return ModelResponse(text=reply, status="ok")


class AuthorLLM:
    """Writes `source`, and answers the counter-example request with `counterexample`.

    `counterexample=None` means "reply with something unusable", which is how a model
    that cannot name a violation of its own check behaves.
    """

    name = "fake-author"

    def __init__(self, source: str, counterexample=None) -> None:
        self.source = source
        self.counterexample = counterexample
        self.prompts: list[str] = []

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        self.prompts.append(prompt)
        if "must REJECT" in prompt:
            body = ("no idea" if self.counterexample is None
                    else json.dumps(self.counterexample))
            return ModelResponse(text=body, status="ok")
        return ModelResponse(text=self.source, status="ok")


class BuildLLM:
    """A whole builder: routes on the prompt so one double serves a full build.

    `derive_intents`, `generate_cases`, `generate_check` and `generate_rubric` each send
    a distinctly-worded prompt. Anything unrecognised gets an empty reply, which those
    modules treat as an unusable reply and fall back from deterministically.
    """

    name = "fake-builder"

    def __init__(self, intents: list[dict], check_source: str) -> None:
        self.intents = intents
        self.check_source = check_source
        self.prompts: list[str] = []

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        self.prompts.append(prompt)
        if prompt.startswith("You convert software/model assessment requirements"):
            return ModelResponse(text=json.dumps(self.intents), status="ok")
        if prompt.startswith("You write one deterministic Python check"):
            return ModelResponse(text=self.check_source, status="ok")
        return ModelResponse(text="", status="ok")


# ── candidate sources ───────────────────────────────────────────────────────

# Passes the nominal sample (non-empty text) and rejects the degraded ones (blank text).
GOOD = '''def check(response: dict, context: dict) -> dict:
    """The target must actually say something."""
    text = response.get("text") or ""
    ok = len(text.strip()) > 0
    return {
        "passed": ok,
        "score": None,
        "severity": "info" if ok else "fail",
        "message": "response carries text" if ok else "response text is empty",
        "evidence": {"length": len(text)},
    }
'''

ALWAYS_PASSES = (
    "def check(response: dict, context: dict) -> dict:\n"
    '    return {"passed": True, "severity": "info", "message": "fine", "evidence": {}}\n'
)

WRONG_SIGNATURE = (
    "def check(response):\n"
    '    return {"passed": True}\n'
)

NESTED = (
    "def build():\n"
    "    def check(response: dict, context: dict) -> dict:\n"
    '        return {"passed": True}\n'
    "    return check\n"
)

BLOCKED_IMPORT = (
    "import os\n\n"
    "def check(response: dict, context: dict) -> dict:\n"
    '    return {"passed": bool(os.environ)}\n'
)


@pytest.fixture
def iface():
    from assay.generator.interface import Interface
    return Interface()


# ── validate_source ─────────────────────────────────────────────────────────

def test_validate_accepts_a_clean_check():
    from assay.generator.codegen import validate_source
    assert validate_source(GOOD) == []


def test_validate_rejects_a_wrong_signature():
    from assay.generator.codegen import validate_source
    problems = validate_source(WRONG_SIGNATURE)
    assert problems
    assert any("response, context" in p for p in problems)


def test_validate_rejects_a_check_that_is_not_module_level():
    from assay.generator.codegen import validate_source
    problems = validate_source(NESTED)
    assert any("module level" in p for p in problems)


def test_validate_rejects_a_blocked_import():
    from assay.generator.codegen import validate_source
    problems = validate_source(BLOCKED_IMPORT)
    assert any("'os'" in p and "blocked" in p for p in problems)


def test_validate_rejects_a_blocked_from_import():
    from assay.generator.codegen import validate_source
    src = ("from subprocess import run\n\n"
           "def check(response: dict, context: dict) -> dict:\n"
           '    return {"passed": True}\n')
    assert any("subprocess" in p for p in validate_source(src))


@pytest.mark.parametrize("body,needle", [
    ('    return {"passed": bool(__import__("os"))}', "__import__"),
    ('    return {"passed": bool(eval("1"))}', "eval"),
    ('    return {"passed": bool(().__class__.__bases__)}', "__class__"),
    ('    return {"passed": bool(getattr(response, "__globals__", None))}', "__globals__"),
    ('    return {"passed": bool(open("/etc/passwd"))}', "open"),
])
def test_validate_rejects_dangerous_constructs(body, needle):
    from assay.generator.codegen import validate_source
    src = f"def check(response: dict, context: dict) -> dict:\n{body}\n"
    problems = validate_source(src)
    assert problems, f"{needle} should have been rejected"
    assert any(needle in p for p in problems)


def test_validate_rejects_source_that_does_not_parse():
    from assay.generator.codegen import validate_source
    assert any("does not parse" in p for p in validate_source("def check(:\n"))


def test_validate_rejects_an_empty_reply():
    from assay.generator.codegen import validate_source
    assert validate_source("   ") == ["the reply contained no Python source"]


# ── the generate → validate → dry-run → repair loop ─────────────────────────

def test_first_reply_good_is_accepted(iface):
    from assay.generator.codegen import generate_check
    llm = ScriptedLLM(GOOD)
    out = generate_check({"id": "not-empty", "assertion": "the response is not empty"},
                         iface, llm)
    assert out.ok
    assert out.attempts == 1
    assert out.errors == []
    assert out.path == "generated/checks/not-empty.py"
    assert out.dry_run["passed"] is True
    assert out.dry_run["discriminates"] is True


def test_the_repair_loop_converges(iface):
    """First reply is unusable, second is good: one repair, and the errors are kept."""
    from assay.generator.codegen import generate_check
    llm = ScriptedLLM(WRONG_SIGNATURE, GOOD)
    out = generate_check({"id": "not-empty", "assertion": "the response is not empty"},
                         iface, llm)
    assert out.ok
    assert out.attempts == 2
    assert out.source == GOOD
    assert any("attempt 1" in e and "response, context" in e for e in out.errors)


def test_the_repair_prompt_carries_the_exact_problem(iface):
    from assay.generator.codegen import generate_check
    llm = ScriptedLLM(WRONG_SIGNATURE, GOOD)
    generate_check({"id": "not-empty", "assertion": "the response is not empty"},
                   iface, llm)
    assert len(llm.prompts) == 2
    repair = llm.prompts[1]
    assert "REJECTED" in repair
    assert "positional parameters named (response, context)" in repair
    assert WRONG_SIGNATURE.strip() in repair, "the rejected attempt is shown back"


def test_the_prompt_states_the_contract_and_the_allowlist(iface):
    from assay.generator.codegen import CHECK_CONTRACT, generate_check
    from assay.sandbox.runner import _ALLOWED
    llm = ScriptedLLM(GOOD)
    generate_check({"id": "x", "assertion": "the response is not empty"}, iface, llm)
    prompt = llm.prompts[0]
    assert CHECK_CONTRACT in prompt
    for module in _ALLOWED:
        assert module in prompt
    assert "no filesystem, no network" in prompt


def test_a_check_that_passes_everything_is_rejected(iface):
    """Passing the good sample only proves it runs. It has to reject a bad one too."""
    from assay.generator.codegen import generate_check
    llm = ScriptedLLM(ALWAYS_PASSES)
    out = generate_check({"id": "vacuous", "assertion": "the response is not empty"},
                         iface, llm, max_repairs=1)
    assert not out.ok
    assert out.dry_run["passed"] is True, "it ran fine — that was never the problem"
    assert out.dry_run["discriminates"] is False
    assert any("not testing the assertion" in e for e in out.errors)


SEVERITY_MONOTONIC = (
    Path(__file__).resolve().parent.parent
    / "examples/compliance-copilot/generated/checks/severity_monotonic.py"
).read_text()


@pytest.fixture
def findings_iface():
    """A grounded interface whose sample response has one allowed, low-severity finding."""
    from assay.generator.interface import Interface
    schema = {"type": "object", "properties": {"findings": {"type": "array", "items": {
        "type": "object", "properties": {
            "article": {"type": "string"},
            "severity": {"enum": ["low", "medium", "high", "critical"]},
            "status": {"enum": ["allowed", "blocked"]}}}}}}
    return Interface(kind="openapi", response_schema=schema,
                     response_paths=["$.findings[*]"])


def test_a_conditional_check_discriminates_via_a_counterexample(findings_iface):
    """A real check whose assertion only bites in one case must not look vacuous.

    "blocked findings must be high severity" is satisfied by any response with no
    blocked findings, so no generic degradation can trigger it. The author is asked for
    a violation instead.
    """
    from assay.generator.codegen import generate_check
    llm = AuthorLLM(SEVERITY_MONOTONIC,
                    {"findings": [{"article": "a", "severity": "low", "status": "blocked"}]})
    out = generate_check({"id": "sev", "assertion": "blocked findings carry high severity"},
                         findings_iface, llm, max_repairs=0)
    assert out.ok, out.errors
    assert out.dry_run["discriminates"] is True
    assert any("must REJECT" in p for p in llm.prompts)


def test_a_vacuous_check_is_not_saved_by_a_counterexample(findings_iface):
    """The escape hatch does not open for a check that rejects nothing at all."""
    from assay.generator.codegen import generate_check
    llm = AuthorLLM(ALWAYS_PASSES,
                    {"findings": [{"article": "a", "severity": "low", "status": "blocked"}]})
    out = generate_check({"id": "sev", "assertion": "blocked findings carry high severity"},
                         findings_iface, llm, max_repairs=0)
    assert not out.ok
    assert out.dry_run["discriminates"] is False


def test_degradation_keeps_list_elements_so_per_item_checks_can_fail(findings_iface):
    """Emptying a list would make "every finding cites an article" vacuously true."""
    from assay.generator.codegen import generate_check
    every_finding_cites = (
        "def check(response: dict, context: dict) -> dict:\n"
        '    findings = (response.get("json") or {}).get("findings") or []\n'
        '    bad = [f for f in findings if not (f.get("article") or "").strip()]\n'
        '    return {"passed": not bad, "severity": "fail" if bad else "info",\n'
        '            "message": f"{len(bad)} finding(s) without an article",\n'
        '            "evidence": {"violations": bad[:5]}}\n'
    )
    # No counter-example available: the degraded sample alone has to carry this.
    llm = AuthorLLM(every_finding_cites, None)
    out = generate_check({"id": "cites", "assertion": "every finding cites an article"},
                         findings_iface, llm, max_repairs=0)
    assert out.ok, out.errors
    assert out.dry_run["discriminates"] is True


def test_exhausting_the_repairs_records_every_attempt(iface):
    from assay.generator.codegen import generate_check
    llm = ScriptedLLM(WRONG_SIGNATURE)
    out = generate_check({"id": "x", "assertion": "the response is not empty"},
                         iface, llm, max_repairs=2)
    assert not out.ok
    assert out.attempts == 3
    assert len(llm.prompts) == 3
    assert [e.split(":")[0] for e in out.errors] == ["attempt 1", "attempt 2", "attempt 3"]


def test_a_provider_failure_stops_the_loop(iface):
    from assay.generator.codegen import generate_check

    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("upstream 503")

    out = generate_check({"id": "x", "assertion": "a"}, iface, Broken())
    assert not out.ok
    assert out.attempts == 1
    assert any("upstream 503" in e for e in out.errors)


def test_a_check_that_crashes_is_rejected_with_the_traceback_text(iface):
    from assay.generator.codegen import generate_check
    boom = ("def check(response: dict, context: dict) -> dict:\n"
            "    return response['definitely-missing']\n")
    out = generate_check({"id": "x", "assertion": "a"}, iface, ScriptedLLM(boom),
                         max_repairs=0)
    assert not out.ok
    assert any("KeyError" in e for e in out.errors)


def test_a_fenced_reply_is_unwrapped(iface):
    from assay.generator.codegen import generate_check
    llm = ScriptedLLM(f"Here you go:\n```python\n{GOOD}```\n")
    out = generate_check({"id": "x", "assertion": "a"}, iface, llm)
    assert out.ok
    assert out.source.startswith("def check(")


# ── degradation to a judge check ────────────────────────────────────────────

def _generated_intent():
    return {"id": "G1", "requirement_ref": "R1", "category": "auto",
            "assertion": "the response is not empty", "how": "generated"}


def test_a_failed_intent_degrades_to_judge_and_records_the_reason(iface):
    from assay.generator.build import generated_sources_for
    intents = [_generated_intent()]
    sources, meta = generated_sources_for(intents, iface, ScriptedLLM(WRONG_SIGNATURE))

    assert sources == {}
    assert intents[0]["how"] == "judge", "the assertion still gets tested"
    assert intents[0]["degraded_from"] == "generated"

    failure = meta["codegen_failures"][0]
    assert failure["intent_id"] == "G1"
    assert failure["assertion"] == "the response is not empty"
    assert failure["attempts"] == 3
    assert failure["errors"]


def test_the_offline_path_degrades_with_no_model(iface):
    """`assay generate --offline` has no model, so it can write no checks."""
    from assay.generator.build import generated_sources_for
    intents = [_generated_intent()]
    sources, meta = generated_sources_for(intents, iface, None)

    assert sources == {}
    assert intents[0]["how"] == "judge"
    failure = meta["codegen_failures"][0]
    assert failure["attempts"] == 0
    assert any("no builder model" in e for e in failure["errors"])


def test_a_successful_intent_stays_generated(iface):
    from assay.generator.build import generated_sources_for
    intents = [_generated_intent()]
    sources, meta = generated_sources_for(intents, iface, ScriptedLLM(GOOD))

    assert intents[0]["how"] == "generated"
    assert sources == {"generated/checks/G1.py": GOOD}
    assert meta["codegen"]["generated/checks/G1.py"]["discriminates"] is True
    assert "codegen_failures" not in meta


# ── the build, end to end ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_HOME", str(tmp_path / ".assay"))
    monkeypatch.setenv("ASSAY_DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    import importlib, assay.config, assay.store.db
    importlib.reload(assay.config)
    importlib.reload(assay.store.db)
    from assay.store.db import init_db as _init
    _init()
    yield


def _build(tmp_path, check_source: str) -> int:
    from assay.generator.build import build_pipeline_to_db
    reqs = tmp_path / "reqs.md"
    reqs.write_text("R1. The response must not be empty.\n")
    llm = BuildLLM([_generated_intent()], check_source)
    return build_pipeline_to_db(str(reqs), {"adapter": "mock"}, judge=llm,
                                project="p4", created_by="tester")


def test_a_build_persists_the_generated_source(tmp_path):
    from assay.pipeline import get_version
    pv = get_version(_build(tmp_path, GOOD))

    assert "generated/checks/G1.py" in pv.generated_sources
    assert pv.generated_sources["generated/checks/G1.py"] == GOOD
    # The spec points at the file that now exists, rather than at one nobody wrote.
    uses = {c["uses"] for s in pv.config["suites"] for case in s["cases"]
            for c in case["checks"]}
    assert uses == {"generated/checks/G1.py"}
    assert pv.config["build_meta"]["codegen"]["generated/checks/G1.py"]["passed"] is True


def test_a_failed_build_degrades_the_spec_to_a_judge_check(tmp_path):
    from assay.pipeline import get_version
    pv = get_version(_build(tmp_path, WRONG_SIGNATURE))

    assert pv.generated_sources == {}
    checks = [c for s in pv.config["suites"] for case in s["cases"] for c in case["checks"]]
    assert checks and {c["type"] for c in checks} == {"judge"}
    # A judge check needs a rubric, and the degraded intent got one.
    assert {c["rubric"] for c in checks} == {"generated/rubrics/G1.yaml"}
    assert "generated/rubrics/G1.yaml" in pv.rubrics
    assert pv.config["build_meta"]["codegen_failures"][0]["intent_id"] == "G1"


def test_a_generated_check_executes_in_a_real_run(tmp_path):
    """The whole point: build it, activate it, run it, and get a real verdict."""
    from assay.engine import execute_run
    from assay.pipeline import activate_version
    from assay.store import session_scope
    from assay.store.models import CaseResult

    version_id = _build(tmp_path, GOOD)
    activate_version(version_id, "solo-dev")
    run_id = execute_run(pipeline_version_id=version_id, triggered_by="tester")

    with session_scope() as s:
        results = s.query(CaseResult).filter_by(run_id=run_id).all()
        assert results
        checks = [c for r in results for c in r.checks]

    assert checks, "the run produced no check results at all"
    # The engine materialises generated_sources into a run tmpdir, so the check id is
    # that path — what matters is that it is the G1 module and that it actually ran.
    assert all(c["check_id"].endswith("checks/G1.py") for c in checks)
    assert {c["type"] for c in checks} == {"generated"}
    assert all(c["passed"] is True for c in checks)
    assert all(c["message"] == "response carries text" for c in checks)
    assert not any("generated check not found" in c["message"] for c in checks)


# ── regenerate_check ────────────────────────────────────────────────────────

CHECK_PATH = "generated/checks/not_empty.py"


def _draft_with_generated_check():
    from assay.pipeline import create_pipeline, create_version
    config = {
        "version": 1, "project": "regen", "target": {"adapter": "mock"},
        "judges": {}, "gating": {},
        "suites": [{"id": "R1", "requirement_ref": "R1", "cases": [
            {"id": "not_empty-1", "input": {"q": "hi"},
             "checks": [{"type": "generated", "uses": CHECK_PATH,
                         "assertion": "the response is not empty"}]},
        ]}],
    }
    pipe = create_pipeline(project="regen", name="regen")
    return create_version(pipe.id, config, {CHECK_PATH: "def check(r, c):\n    pass\n"}, {})


def test_regenerate_produces_a_working_check_not_a_scaffold():
    from assay.pipeline import get_version
    from assay.pipeline.service import regenerate_check
    from assay.sandbox import run_generated_source

    draft = _draft_with_generated_check()
    new_id = regenerate_check(draft.id, CHECK_PATH, "alice", llm=ScriptedLLM(GOOD))
    source = get_version(new_id).generated_sources[CHECK_PATH]

    assert "scaffold" not in source
    assert source == GOOD
    out = run_generated_source(source, {"text": "a real answer", "json": None},
                               {"input": {}, "suite": "s", "case": "c"})
    assert out["passed"] is True


def test_regenerate_applies_the_same_dry_run_gate():
    """A candidate that never fails anything must not be persisted."""
    from assay.pipeline.service import CodegenError, regenerate_check
    draft = _draft_with_generated_check()
    with pytest.raises(CodegenError) as excinfo:
        regenerate_check(draft.id, CHECK_PATH, "alice", llm=ScriptedLLM(ALWAYS_PASSES))
    assert "not testing the assertion" in str(excinfo.value)


def test_regenerate_surfaces_repair_failures():
    from assay.pipeline.service import CodegenError, regenerate_check
    draft = _draft_with_generated_check()
    with pytest.raises(CodegenError) as excinfo:
        regenerate_check(draft.id, CHECK_PATH, "alice", llm=ScriptedLLM(WRONG_SIGNATURE))
    err = excinfo.value
    assert err.attempts == 3
    assert len(err.errors) == 3
    assert "response, context" in str(err)


def test_regenerate_records_the_new_dry_run():
    from assay.pipeline import get_version
    from assay.pipeline.service import regenerate_check
    draft = _draft_with_generated_check()
    new_id = regenerate_check(draft.id, CHECK_PATH, "alice", llm=ScriptedLLM(GOOD))
    run = get_version(new_id).config["build_meta"]["codegen"][CHECK_PATH]
    assert run["passed"] is True
    assert run["discriminates"] is True


def test_editing_a_check_by_hand_drops_the_stale_dry_run(tmp_path):
    """The review screen must not vouch for source nobody ran."""
    from assay.pipeline import get_version
    from assay.pipeline.service import update_check_source

    version_id = _build(tmp_path, GOOD)
    path = "generated/checks/G1.py"
    assert path in get_version(version_id).config["build_meta"]["codegen"]

    update_check_source(version_id, path, "def check(response, context):\n    pass\n")
    meta = get_version(version_id).config.get("build_meta", {})
    assert path not in meta.get("codegen", {})


def test_a_failed_regeneration_persists_nothing():
    from assay.pipeline import list_versions
    from assay.pipeline.service import CodegenError, regenerate_check
    draft = _draft_with_generated_check()
    before = len(list_versions(draft.pipeline_id))
    with pytest.raises(CodegenError):
        regenerate_check(draft.id, CHECK_PATH, "alice", llm=ScriptedLLM(WRONG_SIGNATURE))
    assert len(list_versions(draft.pipeline_id)) == before


# ── the review screen ───────────────────────────────────────────────────────

@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("jinja2")
    import importlib, assay.server.app
    importlib.reload(assay.server.app)
    from starlette.testclient import TestClient
    return TestClient(assay.server.app.app)


def test_the_review_screen_shows_the_dry_run(client, tmp_path):
    from assay.pipeline import get_version
    version_id = _build(tmp_path, GOOD)
    pv = get_version(version_id)

    page = client.get(f"/pipelines/{pv.pipeline_id}/versions/{version_id}/review").text
    assert "dry run passed" in page
    assert "rejects a bad response" in page
    assert "response carries text" in page
    assert "def check(response: dict, context: dict)" in page


def test_the_review_screen_badges_a_degraded_intent(client, tmp_path):
    """A reviewer has to be able to see that a mechanical check was wanted and missed."""
    from assay.pipeline import get_version
    version_id = _build(tmp_path, WRONG_SIGNATURE)
    pv = get_version(version_id)

    page = client.get(f"/pipelines/{pv.pipeline_id}/versions/{version_id}/review").text
    assert "fell back from generated" in page
    assert "Codegen could not produce one after" in page
    assert "Why it failed" in page
    assert "response, context" in page, "the actual reason, not just that it failed"
