# Assay — build status

What is actually built, what is partial, and what is still planned. This file is the
source of truth for capability claims. If the README, the design doc, or a docstring
disagrees with this page, this page is right and the other is a bug.

**Last verified:** 2026-08-04 against `main`. 162 tests passing.

| Marker | Meaning |
|---|---|
| **Built** | Works end to end, covered by tests |
| **Partial** | Exists but incomplete, or works only in the offline path |
| **Planned** | Designed, not implemented |

> **The headline:** the *runner* — execute, review, adjudicate, approve, audit — is
> production-shaped. The *builder* — requirements → intents → checks — is a v0 heuristic.
> The web UI never calls an LLM, and LLM codegen does not exist. Assay is not yet
> market-ready; see [Roadmap](#roadmap) for the phases that close the gap.

## Builder (requirements → pipeline)

| Capability | Status | Note |
|---|---|---|
| Parse requirements into intents | **Partial** | Real LLM path exists in the CLI via `--judge`. The web UI hardcodes `judge=None` (`server/app.py:344`, `:377`), so every UI build uses the offline keyword heuristic |
| Requirement traceability (`R1…Rn`) | **Planned** | Heuristic intents are all stamped `requirement_ref: "auto"` (`generator/build.py:41-62`) |
| Route deterministic vs. judge | **Partial** | Decided inside the single intent call; no rationale is persisted, no per-intent override |
| Bind to a template check | **Built** | 11 primitives (`checks/library.py`) |
| Generate a Python check (codegen) | **Planned** | `generator/build.py:144` hardcodes `generated_sources = {}`. A `generated` intent produces a spec pointing at a file that is never written |
| Generate a judge rubric | **Partial** | A fixed one-dimension YAML synthesised from the assertion string; no anchored dimensions, no output schema, no evidence requirement |
| Generate test cases | **Planned** | Every case is `"input": {}` (`generator/build.py:98`) |
| Ground on the target interface | **Planned** | The Postman/OpenAPI file is never parsed at build time, so checks cannot reference real response fields |
| Golden dataset binding | **Planned** | `datasets/` is scaffolded by `cli.py:25` and never read |
| Regenerate a single check | **Partial** | Emits a contract-correct scaffold that fails explicitly until codegen lands |

## Target adapters

| Adapter | Status | Note |
|---|---|---|
| `mock` | **Built** | For tests and the offline example only |
| `rest` | **Partial** | Postman collection import, variable substitution, bearer auth. **OpenAPI import is not implemented** — an OpenAPI file fails in `json.loads` (`adapters/rest.py:37`) |
| `anthropic` | **Built** | |
| `openai_compat` | **Partial** | Works, but the API key env var is hardcoded to `OPENAI_API_KEY` (`adapters/openai_compat.py:44`); per-target `key_env` is ignored |
| `ollama` | **Built** | |
| `mcp` | **Planned** | Not implemented |
| `custom` | **Planned** | Not implemented; the adapter registry is a closed dict (`adapters/registry.py:11-18`) |

## Judges

| Capability | Status | Note |
|---|---|---|
| Rubric-driven scoring, temperature 0 | **Built** | |
| Rubrics resolve for DB pipelines | **Built** | Fixed 2026-08-04; previously aborted the run with `FileNotFoundError` |
| Structured output (schema-forced) | **Planned** | `schema=`/`tools=` are accepted by `complete()` and ignored by every adapter |
| Evidence enforcement | **Planned** | Quotes are stored but never verified against the response |
| Self-consistency (n>1) | **Planned** | |

## Checks and sandbox

| Capability | Status | Note |
|---|---|---|
| Template library | **Partial** | 11 of the 16 designed primitives. Missing: `equals`, `cost_bound`, `citation_resolves`, `enum_value`, `length_bound` |
| Isolated subprocess, CPU/memory rlimits, wall-clock timeout | **Built** | POSIX only; rlimits no-op elsewhere |
| Import allowlist, `open`/`exec`/`eval`/`compile` removed | **Built** | Installed before the module body executes (fixed 2026-08-04) |
| Socket factories patched | **Built** | Defence in depth, not an egress block |
| Filesystem isolation | **Planned** | The subprocess inherits the engine's working directory. There is no chroot, namespace, or temp-dir jail |
| OS-level network deny-all | **Planned** | |
| Hardened tier (gVisor / Firecracker / WASM) | **Planned** | |

## Engine and runs

| Capability | Status | Note |
|---|---|---|
| Execute a run, capture full request + response per case | **Built** | |
| Case-level gating (all required checks pass) | **Built** | |
| Run-level / suite-level gating | **Planned** | `Spec.gating` is parsed (`spec/models.py:57`) and never read; `engine/gating.py:10` is dead code; suite-level `gate:` is silently discarded |
| Token and cost capture | **Planned** | No columns on `CaseResult`; `Run.total_cost_usd` is always 0 |
| Regression vs. the last approved run | **Planned** | |
| Run progress / streaming / cancel | **Planned** | The run request blocks until every case completes |
| Scheduler / `assay watch` | **Planned** | |

## Review, approval, audit

| Capability | Status | Note |
|---|---|---|
| `pending → ready_for_review → done` state machine | **Built** | Including the back-edge to `pending` |
| Automation may reach `ready_for_review`, never `done` | **Built** | Structurally enforced (`engine/review.py`) |
| Per-case adjudication with reviewer override | **Built** | |
| Reviewer assignment | **Built** | |
| Approval locks the report, records the approver | **Built** | |
| Append-only state transition log | **Built** | |
| Auth postures (open / enforced), RBAC, signed sessions | **Built** | |
| Seeding the first reviewer from the UI | **Planned** | Enforced mode requires seeding via `assay users --add` before the UI is usable |

## Server and UI

| Capability | Status | Note |
|---|---|---|
| Review queue, report detail, transcripts, exports | **Built** | |
| Projects list and project detail | **Built** | Projects are a string column on `Pipeline`, not a first-class entity |
| 3-step pipeline wizard with draft resume | **Built** | |
| Live connection test | **Built** | |
| Pipeline review screen with activation gate | **Built** | |
| Per-check params editing, inline source editing | **Built** | Draft versions only; 409 on active |
| Determinism classification, metric catalogue + thresholds | **Built** | |
| Provider credential status | **Planned** | Nothing in the UI reports a missing API key |
| Run history, pass-rate trend, regression banner | **Planned** | |
| Account management from Settings | **Planned** | The accounts table is read-only |
| Linear integration UI | **Planned** | The notification backend exists; there is no UI |

## Exporters

| Format | Status | Note |
|---|---|---|
| JSON | **Built** | |
| Markdown | **Built** | |
| HTML | **Partial** | The Markdown escaped inside a `<pre>`, not a styled document |
| PDF | **Planned** | |
| Requirement coverage matrix | **Partial** | One-directional (pass counts by `requirement_ref`). Uncovered requirements and orphan tests are never computed |

## CLI

**Built:** `init`, `generate`, `run`, `users`, `report`, `approve`, `serve`, `target ping`,
`pipeline import`, `pipeline list`, `pipeline activate`, `pipeline show`.

**Planned:** `watch`, `export`. Report export is currently HTTP-only, plus an implicit export
at the end of `assay run`.

## Storage

| Capability | Status | Note |
|---|---|---|
| SQLite default | **Built** | |
| Postgres via `ASSAY_DB_URL` | **Partial** | No code change is needed, but `store/db.py:13-34` emits SQLite-only DDL (`DATETIME`) in its hand-rolled migrations |
| Schema migrations | **Partial** | Hand-rolled `ALTER TABLE`; no Alembic |
| Cases as first-class rows | **Planned** | Cases live as JSON inside `PipelineVersion.config` |
| `TargetModel.interface_hash` | **Planned** | The column exists and is never populated |

## Roadmap

Phases referenced from [`user-journeys.md`](user-journeys.md). Market-ready minimum is
**P0, P1, P2, P4, P6**.

| Phase | Objective |
|---|---|
| **P0** | Credential and provider foundation: one resolution path for which model, which key, and whether it is configured |
| **P1** | A real LLM in every build path, including the web UI. No silent heuristic fallback |
| **P2** | Real judging: structured output, evidence enforcement, self-consistency, real rubric generation |
| **P3** | Interface grounding and case generation, including dataset binding |
| **P4** | Real codegen with validation, sandbox dry-run, and a repair loop |
| **P5** | Token and cost capture |
| **P6** | Retire the mock default — a fresh install with no keys must not be able to produce a green report |
| **P7** | Test tiering: offline, recorded-cassette, and live-provider tiers |

## Keeping this file true

`tests/test_docs_truth.py` asserts that the adapter table here and in the README matches
`adapters/registry.py`, and that every CLI command named in the docs exists in `cli.py`.
It fails the build on drift. It cannot police prose — that is on us, in review.
