"""One resolution path for "which model, which key, and is it configured".

Every part of Assay that needs to talk to an LLM -- the builder, the judges, the
connection tester -- goes through here. Nothing else should read a provider API key
from the environment directly.

Keys live in the environment only. They are referenced by variable *name* (`key_env`),
never persisted to the database and never rendered back to the browser.

Two distinct roles, deliberately separate:

  * the **judge** model, used at eval time to score responses
  * the **builder** model, used at build time to turn requirements into a pipeline

They may be different models, and a workspace may have one configured but not the other.
"""
from __future__ import annotations

import os
from typing import Any

# Which environment variable each adapter reads by default. None means the adapter
# needs no credential (a local or in-process provider).
DEFAULT_KEY_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai_compat": "OPENAI_API_KEY",
    "ollama": None,
    "mock": None,
    "rest": None,
}

# Used when a workspace has expressed no preference at all.
_FALLBACK_ADAPTER = "anthropic"
_FALLBACK_MODEL = "claude-haiku-4-5-20251001"


class LLMConfigError(RuntimeError):
    """A provider was requested but cannot be used.

    Carries the adapter and the environment variable the caller needs to set, so the
    CLI and the web UI can both tell the user exactly what is missing rather than
    failing silently or falling back to an offline heuristic.
    """

    def __init__(self, message: str, *, adapter: str, env_var: str | None = None) -> None:
        super().__init__(message)
        self.adapter = adapter
        self.env_var = env_var


def key_env_for(adapter: str, key_env: str | None = None) -> str | None:
    """Name of the env var holding this adapter's credential, or None if it needs none.

    An explicit `key_env` always wins, so a spec can point a second OpenAI-compatible
    target at a different key than the workspace default.

    An explicit empty string means "this target takes no credential" -- the escape
    hatch for a local OpenAI-compatible server (vLLM, LM Studio, llama.cpp) that
    would otherwise be forced to invent an API key it does not want.
    """
    if key_env is not None:
        return key_env or None
    return DEFAULT_KEY_ENV.get(adapter)


def credential_status(adapter: str, key_env: str | None = None) -> dict:
    """Report whether `adapter` is usable right now, without constructing a client.

    Returns {"adapter", "env_var", "requires_key", "configured", "known"}.
    Safe to render in a UI: it reports the variable *name*, never its value.
    """
    known = adapter in DEFAULT_KEY_ENV
    env_var = key_env_for(adapter, key_env)
    requires_key = env_var is not None
    configured = bool(os.environ.get(env_var)) if requires_key else known
    return {
        "adapter": adapter,
        "env_var": env_var,
        "requires_key": requires_key,
        "configured": configured,
        "known": known,
    }


def credential_overview() -> list[dict]:
    """credential_status() for every adapter Assay knows about, in declaration order.

    What the Settings > Providers card renders. Names only -- no values.
    """
    return [credential_status(name) for name in DEFAULT_KEY_ENV]


def read_key(adapter: str, key_env: str | None = None) -> str | None:
    """Return the credential *value* for `adapter`, or None when it needs none.

    The only place a key value is read. Callers use it to construct a client and
    must not store it on an instance, log it, or return it. Raises LLMConfigError
    when a required variable is unset or empty.
    """
    env_var = key_env_for(adapter, key_env)
    if env_var is None:
        return None
    value = os.environ.get(env_var, "")
    if not value:
        raise LLMConfigError(
            f"{adapter} is not configured: set ${env_var} in the environment",
            adapter=adapter,
            env_var=env_var,
        )
    return value


def require_credential(adapter: str, key_env: str | None = None) -> None:
    """Raise LLMConfigError unless `adapter` is known and its credential is present."""
    status = credential_status(adapter, key_env)
    if not status["known"]:
        raise LLMConfigError(
            f"unknown adapter: {adapter}. Known adapters: "
            f"{', '.join(sorted(DEFAULT_KEY_ENV))}",
            adapter=adapter,
        )
    if status["requires_key"] and not status["configured"]:
        raise LLMConfigError(
            f"{adapter} is not configured: set ${status['env_var']} in the environment",
            adapter=adapter,
            env_var=status["env_var"],
        )


def resolve_llm(
    adapter: str | None = None,
    model: str | None = None,
    *,
    key_env: str | None = None,
) -> Any:
    """Return a ready-to-use judge provider for `adapter`/`model`.

    Raises LLMConfigError when the adapter is unknown or its credential is missing --
    never returns a silently degraded stand-in.
    """
    adapter = adapter or _FALLBACK_ADAPTER
    model = model or _FALLBACK_MODEL
    require_credential(adapter, key_env)

    from ..adapters.registry import get_judge_provider
    from ..spec.models import JudgeSpec

    spec = JudgeSpec(provider=adapter, model=model)
    if key_env is not None:
        spec = spec.model_copy(update={"key_env": key_env})
    return get_judge_provider(spec)


def _setting(key: str) -> str | None:
    from ..store.db import session_scope
    from ..store.models import WorkspaceSetting

    with session_scope() as s:
        row = s.get(WorkspaceSetting, key)
        return row.value if row else None


def builder_choice(project: str | None = None) -> tuple[str, str]:
    """Return the (adapter, model) that should build pipelines for `project`.

    Precedence: explicit builder settings -> judge settings -> built-in fallback.
    A workspace that has only ever configured a judge therefore builds with that same
    model, which is the least surprising default.
    """
    adapter = _setting("builder_adapter") or _setting("judge_adapter") or _FALLBACK_ADAPTER
    model = _setting("builder_model") or _setting("judge_model") or _FALLBACK_MODEL
    return adapter, model


def resolve_builder_llm(project: str | None = None) -> Any:
    """Return the LLM used to build pipelines. Raises LLMConfigError when unconfigured."""
    adapter, model = builder_choice(project)
    return resolve_llm(adapter, model)
