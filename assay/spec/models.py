"""Pydantic models for the pipeline spec (assay.yaml)."""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field


class TargetSpec(BaseModel):
    adapter: str                              # mock | openai_compat | anthropic | ollama | rest
    model: str | None = None
    endpoint: str | None = None
    key_env: str | None = None               # env var holding the API key (name, never value)
    import_: str | None = Field(default=None, alias="import")   # postman/openapi file
    request: str | None = None               # named request inside a collection
    params: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)

    # forbid: a field the UI collects but the model does not declare used to vanish
    # silently (that is how key_env was lost). Now it is a loud validation error.
    model_config = {"populate_by_name": True, "extra": "forbid"}


class JudgeSpec(BaseModel):
    provider: str                            # anthropic | openai | ollama | openai_compat
    model: str
    endpoint: str | None = None              # so a judge can point at a local vLLM
    key_env: str | None = None               # env var holding the API key (name, never value)
    params: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class CheckSpec(BaseModel):
    type: Literal["template", "generated", "judge"]
    uses: str | None = None                  # template name OR path to generated/*.py
    judge: str | None = None                 # judge key (for type=judge)
    rubric: str | None = None                # path to rubric yaml (for type=judge)
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    required: bool = True
    threshold: float | None = None           # min passing score (0-1) for graded checks

    model_config = {"populate_by_name": True}


class Case(BaseModel):
    id: str
    input: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckSpec] = Field(default_factory=list)
    context: dict[str, Any] | None = None   # RAG retrieved passages or extra eval context


class Suite(BaseModel):
    id: str
    requirement_ref: str | None = None
    cases: list[Case] = Field(default_factory=list)


class Spec(BaseModel):
    version: int = 1
    project: str
    target: TargetSpec
    judges: dict[str, JudgeSpec] = Field(default_factory=dict)
    suites: list[Suite] = Field(default_factory=list)
    gating: dict[str, Any] = Field(default_factory=dict)

    def all_cases(self) -> list[tuple[str, Case]]:
        return [(s.id, c) for s in self.suites for c in s.cases]
