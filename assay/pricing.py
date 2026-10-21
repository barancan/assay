"""Token accounting: normalise each provider's usage shape, then price it.

Two jobs, deliberately separated. Providers report usage under different key names,
so `normalise_usage` gives the rest of the codebase one shape to read. `estimate_cost`
turns that into dollars using a per-model table of USD per million tokens.

An unknown model prices as **None**, never as a guess and never as zero. A run that
reports $0.00 when it actually spent money is worse than one that admits it does not
know, so "free" (a local model) and "unknown" (a model missing from the table) stay
distinguishable all the way to the report.

PRICES GO STALE. The table below was last checked against each provider's public
pricing page on 2026-08-04. Rather than wait for a release when a rate changes or a
new model ships, point ASSAY_PRICING_FILE at a JSON file:

    ASSAY_PRICING_FILE=/etc/assay/prices.json

    {"anthropic": {"claude-sonnet-4-5": {"input": 2.4, "output": 12.0}},
     "openai_compat": {"my-finetune": {"input": 0.5, "output": 1.5}}}

Entries merge over the built-ins per provider, so overriding one model leaves the
rest of the table intact.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# USD per million tokens, input and output priced separately.
# Last checked: 2026-08-04. Override with ASSAY_PRICING_FILE (see module docstring).
PRICES: dict[str, dict[str, dict[str, float]]] = {
    "anthropic": {
        "claude-opus-4-5":    {"input": 5.00,  "output": 25.00},
        "claude-opus-4-1":    {"input": 15.00, "output": 75.00},
        "claude-opus-4":      {"input": 15.00, "output": 75.00},
        "claude-sonnet-4-5":  {"input": 3.00,  "output": 15.00},
        "claude-sonnet-4":    {"input": 3.00,  "output": 15.00},
        "claude-haiku-4-5":   {"input": 1.00,  "output": 5.00},
        "claude-3-7-sonnet":  {"input": 3.00,  "output": 15.00},
        "claude-3-5-sonnet":  {"input": 3.00,  "output": 15.00},
        "claude-3-5-haiku":   {"input": 0.80,  "output": 4.00},
        "claude-3-opus":      {"input": 15.00, "output": 75.00},
        "claude-3-haiku":     {"input": 0.25,  "output": 1.25},
    },
    "openai_compat": {
        "gpt-5":         {"input": 1.25, "output": 10.00},
        "gpt-5-mini":    {"input": 0.25, "output": 2.00},
        "gpt-5-nano":    {"input": 0.05, "output": 0.40},
        "gpt-4.1":       {"input": 2.00, "output": 8.00},
        "gpt-4.1-mini":  {"input": 0.40, "output": 1.60},
        "gpt-4.1-nano":  {"input": 0.10, "output": 0.40},
        "gpt-4o":        {"input": 2.50, "output": 10.00},
        "gpt-4o-mini":   {"input": 0.15, "output": 0.60},
        "o3":            {"input": 2.00, "output": 8.00},
        "o3-mini":       {"input": 1.10, "output": 4.40},
        "o4-mini":       {"input": 1.10, "output": 4.40},
    },
}

# Runs on hardware the user already pays for: genuinely 0.0, not unknown. The
# distinction matters -- see the module docstring.
FREE_PROVIDERS = frozenset({"ollama", "mock", "mock-judge"})

# Provider-preferred usage keys. Anything unlisted falls back to trying all of them,
# so a new adapter reporting a familiar shape still gets counted.
_INPUT_KEYS = {
    "anthropic": ("input_tokens",),
    "openai_compat": ("prompt_tokens",),
    "ollama": ("prompt_eval_count",),
}
_OUTPUT_KEYS = {
    "anthropic": ("output_tokens",),
    "openai_compat": ("completion_tokens",),
    "ollama": ("eval_count",),
}
_ANY_INPUT = ("input_tokens", "prompt_tokens", "prompt_eval_count")
_ANY_OUTPUT = ("output_tokens", "completion_tokens", "eval_count")

# What may follow a base model id and still mean the same model: a release date, a
# version tag. Anything else is a different model -- `gpt-4o-mini` is not `gpt-4o`.
_VERSION_SUFFIX = re.compile(r"^(\d{8}|\d{4}-\d{2}-\d{2}|v\d+|latest|preview)$")

_CACHE: dict[tuple, dict] = {}


class PricingError(ValueError):
    """ASSAY_PRICING_FILE is set but unusable. Raised loudly: a pricing file that is
    silently ignored bills the user at list rates they thought they had overridden."""


def normalise_usage(provider: str, usage: dict) -> dict:
    """Reduce any provider's usage dict to {"input_tokens", "output_tokens"}."""
    usage = usage if isinstance(usage, dict) else {}
    return {
        "input_tokens": _first_int(usage, _INPUT_KEYS.get(provider, ()), _ANY_INPUT),
        "output_tokens": _first_int(usage, _OUTPUT_KEYS.get(provider, ()), _ANY_OUTPUT),
    }


def estimate_cost(provider: str, model: str | None, usage: dict) -> float | None:
    """Dollars for one call, or None when the model is not in the price table.

    None means "not known", which the caller must not collapse into 0.0.
    """
    if provider in FREE_PROVIDERS:
        return 0.0
    rate = rate_for(provider, model)
    if rate is None:
        return None
    if not _reports_usage(usage):
        # Priced model, but the provider told us nothing about tokens. Unknown, not free.
        return None
    tokens = normalise_usage(provider, usage)
    cost = (tokens["input_tokens"] * rate["input"]
            + tokens["output_tokens"] * rate["output"]) / 1_000_000
    return round(cost, 10)


def rate_for(provider: str, model: str | None) -> dict | None:
    """The {"input", "output"} per-million rate for `model`, or None if unlisted.

    Matching tolerates the version suffixes providers bolt on -- `claude-haiku-4-5-20251001`
    finds `claude-haiku-4-5` -- but never crosses to a neighbouring model.
    """
    table = _table().get(provider) or {}
    if not model:
        return None
    name = _canonical(model)
    if name in table:
        return table[name]
    # Longest first, so a table holding both `gpt-4.1` and `gpt-4.1-mini` matches the
    # more specific one rather than whichever happens to be iterated first.
    for key in sorted(table, key=len, reverse=True):
        if name.startswith(key + "-") and _VERSION_SUFFIX.match(name[len(key) + 1:]):
            return table[key]
    return None


def _canonical(model: str) -> str:
    """Strip the decorations that route a model without changing which model it is."""
    name = str(model).strip().lower()
    name = name.rsplit("/", 1)[-1]      # openrouter-style "anthropic/claude-sonnet-4-5"
    name = name.split(":", 1)[0]        # ollama-style "llama3:8b"
    return name


def _table() -> dict:
    """Built-in prices, with ASSAY_PRICING_FILE merged over them per provider."""
    path = os.environ.get("ASSAY_PRICING_FILE")
    if not path:
        return PRICES
    try:
        stat = Path(path).stat()
    except OSError as exc:
        raise PricingError(f"ASSAY_PRICING_FILE {path!r} cannot be read: {exc}") from exc
    key = (path, stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is None:
        _CACHE.clear()          # only ever one file in play; do not grow without bound
        cached = _CACHE[key] = _merge(PRICES, _load(path))
    return cached


def _load(path: str) -> dict:
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise PricingError(f"ASSAY_PRICING_FILE {path!r} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise PricingError(f"ASSAY_PRICING_FILE {path!r} must be a JSON object "
                           "of {provider: {model: {input, output}}}")
    return doc


def _merge(base: dict, override: dict) -> dict:
    merged = {provider: dict(models) for provider, models in base.items()}
    for provider, models in override.items():
        if not isinstance(models, dict):
            raise PricingError(f"pricing for provider {provider!r} must be an object")
        target = merged.setdefault(provider, {})
        for model, rate in models.items():
            target[_canonical(model)] = _rate(provider, model, rate)
    return merged


def _rate(provider: str, model: str, rate) -> dict:
    if not isinstance(rate, dict) or "input" not in rate or "output" not in rate:
        raise PricingError(f"pricing for {provider}/{model} needs "
                           '{"input": <usd per Mtok>, "output": <usd per Mtok>}')
    try:
        return {"input": float(rate["input"]), "output": float(rate["output"])}
    except (TypeError, ValueError) as exc:
        raise PricingError(f"pricing for {provider}/{model} is not numeric: {exc}") from exc


def _reports_usage(usage: dict) -> bool:
    return isinstance(usage, dict) and any(
        usage.get(k) is not None for k in _ANY_INPUT + _ANY_OUTPUT)


def _first_int(usage: dict, preferred: tuple, fallback: tuple) -> int:
    for key in preferred + fallback:
        value = usage.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0
