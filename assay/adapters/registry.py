"""Resolve adapter/judge names from a spec into instances.

`mock` is a test fixture, not a product option. It answers instantly and passes
everything, so a run against it yields a fully green report that is evidence of nothing
at all -- which is worse than no report. It therefore resolves only where somebody has
said out loud that they want offline behaviour: `ASSAY_ALLOW_MOCK=1` in the environment
(the test suite, the offline example) or an explicit `allow_mock=True` from the caller.
Everywhere else, asking for it is an error that names what to configure instead.
"""
from __future__ import annotations
import os
from typing import Any
from ..llm.provider import LLMConfigError
from ..spec.models import TargetSpec, JudgeSpec
from .mock import MockAdapter, MockJudge
from .rest import RestAdapter
from .anthropic import AnthropicAdapter
from .openai_compat import OpenAICompatAdapter
from .ollama import OllamaAdapter

_TARGETS = {
    "mock": MockAdapter, "rest": RestAdapter, "anthropic": AnthropicAdapter,
    "openai_compat": OpenAICompatAdapter, "ollama": OllamaAdapter,
}
_JUDGES = {
    "mock": MockJudge, "anthropic": AnthropicAdapter,
    "openai_compat": OpenAICompatAdapter, "ollama": OllamaAdapter,
}

ALLOW_MOCK_ENV = "ASSAY_ALLOW_MOCK"
_OFF = {"", "0", "false", "no", "off"}

# Printed verbatim in the refusal: whoever hits this wall needs the next step, not a
# pointer to the docs.
_REAL_PROVIDERS = (
    "  anthropic      set $ANTHROPIC_API_KEY\n"
    "  openai_compat  set $OPENAI_API_KEY and endpoint (or key_env: \"\" for a keyless "
    "local server)\n"
    "  ollama         set endpoint, e.g. http://localhost:11434 (no key needed)\n"
    "  rest           set endpoint to your own service"
)


def mock_allowed(explicit: bool = False) -> bool:
    """Whether mock adapters may be resolved right now.

    `explicit` is the caller saying "I am the offline path" -- `assay generate --offline`
    and the offline example. Otherwise it takes the environment opt-in.
    """
    if explicit:
        return True
    return os.environ.get(ALLOW_MOCK_ENV, "").strip().lower() not in _OFF


def _refuse_mock(role: str, field: str) -> None:
    # Front-loaded on purpose: callers that surface this in a UI truncate it, so the
    # reason and the fix have to land before the detail does.
    raise LLMConfigError(
        f"the mock {role} is a test fixture: it passes every check, so the report comes "
        f"out green and means nothing. Set {field} to a real provider:\n"
        f"{_REAL_PROVIDERS}\n"
        f"Running the offline example, or the test suite? Set {ALLOW_MOCK_ENV}=1.",
        adapter="mock",
    )


def get_target_adapter(target: TargetSpec, *, allow_mock: bool = False) -> Any:
    cls = _TARGETS.get(target.adapter)
    if cls is None:
        raise ValueError(f"unknown target adapter: {target.adapter}")
    if target.adapter == "mock" and not mock_allowed(allow_mock):
        _refuse_mock("target", "target.adapter in assay.yaml")
    kwargs = target.model_dump(by_alias=False)
    kwargs.pop("adapter", None)
    # the spec field is `import_`; constructors accept it too
    return cls(**{k: v for k, v in kwargs.items() if v is not None})


def get_judge_provider(judge: JudgeSpec, *, allow_mock: bool = False) -> Any:
    cls = _JUDGES.get(judge.provider)
    if cls is None:
        raise ValueError(f"unknown judge provider: {judge.provider}")
    if judge.provider == "mock" and not mock_allowed(allow_mock):
        _refuse_mock("judge", "judges.<name>.provider in assay.yaml")
    # endpoint/key_env/params are what let a judge point at a local vLLM or a
    # second key; they used to be declared and dropped on the floor here.
    kwargs = {"model": judge.model, "endpoint": judge.endpoint,
              "key_env": judge.key_env, "params": judge.params}
    # `is not None`, not truthiness: key_env="" is meaningful -- it opts a keyless
    # local server out of auth, and dropping it would silently restore the default.
    return cls(**{k: v for k, v in kwargs.items() if v is not None})


def test_connection(adapter: Any) -> None:
    """Ping the target; raise ConnectionError naming the endpoint or the missing key."""
    result = adapter.ping()
    if result["ok"]:
        return
    desc = adapter.describe()
    endpoint = desc.get("endpoint") or desc.get("adapter", "unknown")
    error = result.get("error") or "unknown error"
    if result.get("authenticated") is False:
        # Reached (or could not even try) but has no usable credential -- say so
        # rather than blaming the network.
        raise ConnectionError(f"Cannot authenticate to {endpoint}: {error}")
    raise ConnectionError(f"Cannot reach target at {endpoint}: {error}")
