"""Shared test doubles for the build path.

Every pipeline build now goes through a real LLM, so the suite injects a fake one. The
hard rule is that `pytest` passes with zero API keys set and makes no network calls.

Two doubles, for two jobs:

  * `CannedBuilderLLM` replays a fixed reply -- used to test parsing and validation.
  * `HeuristicBuilderLLM` reads the requirement ids out of the prompt and answers with
    the offline keyword intents. It stands in for a competent model, so the pre-existing
    route tests keep asserting product behaviour rather than a hardcoded fixture.
"""
from __future__ import annotations

import json
import re

import pytest

from assay.adapters.base import ModelResponse

_REQ_LINE = re.compile(r"^(R\d+):\s*(.*)$", re.MULTILINE)


def prompt_requirements(prompt: str) -> list[dict]:
    """Recover the id-prefixed requirement block the builder sends to the model."""
    return [{"id": rid, "text": text.strip()} for rid, text in _REQ_LINE.findall(prompt)]


class _RecordingLLM:
    name = "fake-builder"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def _reply(self, prompt: str) -> str:
        raise NotImplementedError

    def complete(self, messages, *, schema=None, tools=None, params=None) -> ModelResponse:
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        self.prompts.append(prompt)
        return ModelResponse(text=self._reply(prompt), status="ok")

    @property
    def last_prompt(self) -> str:
        return self.prompts[-1]


class CannedBuilderLLM(_RecordingLLM):
    """Replays `text`, or `intents` serialised as JSON."""

    def __init__(self, intents=None, text=None) -> None:
        super().__init__()
        self.intents = intents
        self.text = text

    def _reply(self, prompt: str) -> str:
        if self.text is not None:
            return self.text
        return json.dumps(self.intents or [])


class HeuristicBuilderLLM(_RecordingLLM):
    """Answers with the offline keyword intents for the requirements in the prompt."""

    def _reply(self, prompt: str) -> str:
        from assay.generator.build import _heuristic_intents
        reqs = prompt_requirements(prompt)
        return json.dumps(_heuristic_intents(reqs))


@pytest.fixture
def builder_llm(monkeypatch):
    """Make `resolve_builder_llm` hand back a fake, so no credential is needed."""
    fake = HeuristicBuilderLLM()
    monkeypatch.setattr("assay.llm.provider.resolve_builder_llm", lambda project=None: fake)
    return fake


@pytest.fixture
def canned_llm():
    """Factory: build a CannedBuilderLLM without installing it."""
    return CannedBuilderLLM


@pytest.fixture
def install_builder_llm(monkeypatch):
    """Factory: make `resolve_builder_llm` hand back the given double."""
    def install(double):
        monkeypatch.setattr("assay.llm.provider.resolve_builder_llm",
                            lambda project=None: double)
        return double
    return install
