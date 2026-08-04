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
    # endpoint/key_env/params are what let a judge point at a local vLLM or a
    # second key; they used to be declared and dropped on the floor here.
    kwargs = {"model": judge.model, "endpoint": judge.endpoint,
              "key_env": judge.key_env, "params": judge.params}
    return cls(**{k: v for k, v in kwargs.items() if v})


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
