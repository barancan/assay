"""Resolve adapter/judge names from a spec into instances."""
from __future__ import annotations
from typing import Any
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


def get_target_adapter(target: TargetSpec) -> Any:
    cls = _TARGETS.get(target.adapter)
    if cls is None:
        raise ValueError(f"unknown target adapter: {target.adapter}")
    kwargs = target.model_dump(by_alias=False)
    kwargs.pop("adapter", None)
    # the spec field is `import_`; constructors accept it too
    return cls(**{k: v for k, v in kwargs.items() if v is not None})


def get_judge_provider(judge: JudgeSpec) -> Any:
    cls = _JUDGES.get(judge.provider)
    if cls is None:
        raise ValueError(f"unknown judge provider: {judge.provider}")
    return cls(model=judge.model)


def test_connection(adapter: Any) -> None:
    """Ping the target; raise ConnectionError with the endpoint in the message on failure."""
    result = adapter.ping()
    if not result["ok"]:
        desc = adapter.describe()
        endpoint = desc.get("endpoint") or desc.get("adapter", "unknown")
        raise ConnectionError(
            f"Cannot reach target at {endpoint}: {result.get('error', 'unknown error')}"
        )
