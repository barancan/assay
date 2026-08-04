# Assay — build status

What is actually built, what is partial, and what is still planned. This file is the
source of truth for capability claims. If the README, the design doc, or a docstring
disagrees with this page, this page is right and the other is a bug.

**Last verified:** 2026-08-04 against `claude/real-judging-and-grounding`. 471 tests passing, with zero API keys set.

| Marker | Meaning |
|---|---|
| **Built** | Works end to end, covered by tests |
| **Partial** | Exists but incomplete, or works only in the offline path |
| **Planned** | Designed, not implemented |

> **The headline:** the *runner* — execute, review, adjudicate, approve, audit — is
> production-shaped. The *builder* now calls a real model on every path, grounds on the
> target's interface, and emits cases with real inputs and judges with verified evidence.
> **LLM codegen still does not exist**, so an intent routed to `generated` produces a
> spec entry with no source behind it — the one remaining piece of the product's core
> claim. See [Roadmap](#roadmap).

## Builder (requirements → pipeline)

| Capability | Status | Note |
|---|---|---|
| Parse requirements into intents | **Built** | Both the web UI and the CLI resolve the configured build model (`llm.provider.resolve_builder_llm`) and call it. Malformed or unconfigured means a 422 naming the variable to set, never a silent fallback. `assay generate --offline` is the explicit opt-in to the keyword heuristic |
| Requirement traceability (`R1…Rn`) | **Built** | `generator/ingest.py` splits requirements into `R1…Rn`; the prompt carries those ids, every returned `requirement_ref` is resolved against them, and `intents_to_spec` emits one suite per requirement |
| Route deterministic vs. judge | **Partial** | Decided inside the single intent call; no rationale is persisted, no per-intent override |
| Bind to a template check | **Built** | 11 primitives (`checks/library.py`) |
| Generate a Python check (codegen) | **Planned** | `generator/build.py:144` hardcodes `generated_sources = {}`. A `generated` intent produces a spec pointing at a file that is never written |
| Generate a judge rubric | **Built** | `generator/rubricgen.py` asks the builder model for ≥2 anchored dimensions (0/1/2 levels describing observable properties), plus `min_score`, `require_evidence`, `samples` and the verdict `output_schema`. Output is validated — slug-safe unique ids, complete scales, `min_score` in range — then repaired once, then falls back to the deterministic `fallback_rubric` (also the `--offline` path). The web wizard and the CLI both hand it the configured builder model, so neither is quietly weaker than the other |
| Generate test cases | **Built** | `generator/casegen.py` produces concrete inputs per intent, grounded on the interface's real request fields, with nominal, empty, boundary and hostile variants. Ids are validated rather than sanitised, and a single gate in `build.py` means no path can emit a case with an empty input |
| Ground on the target interface | **Built** | `generator/interface.py` parses Postman, OpenAPI 3 (JSON or YAML, local `$ref`s resolved) and MCP tool schemas into request fields, a response schema and JSONPath response paths; `adapters/rest.py` uses the same reader so adapter and builder cannot drift. Case generation consumes it. Detection is by content, not extension. An unreadable *document* stays ungrounded; a path the user explicitly supplied that does not exist is an error. Codegen will consume `sample_response` when it lands (P4) |
| Golden dataset binding | **Built** | `assay generate --dataset` binds cases to a `datasets/*.jsonl` file instead of generating them; malformed rows name file and line. `assay init` scaffolds an example so the format is discoverable |
| Regenerate a single check | **Partial** | Emits a contract-correct scaffold that fails explicitly until codegen lands |

## Target adapters

| Adapter | Status | Note |
|---|---|---|
| `mock` | **Built** | For tests and the offline example only |
| `rest` | **Built** | Imports Postman collections (nested folders, named requests, disabled headers dropped, collection variables and auth) and OpenAPI 3 documents in JSON or YAML, through the same parser the builder grounds on (`generator/interface.py`). Variable substitution, bearer auth |
| `anthropic` | **Built** | |
| `openai_compat` | **Built** | Honours a per-target `key_env`; `key_env: ""` opts a keyless local server (vLLM, LM Studio) out of auth entirely |
| `ollama` | **Built** | |
| `mcp` | **Planned** | Not implemented |
| `custom` | **Planned** | Not implemented; the adapter registry is a closed dict (`adapters/registry.py:11-18`) |

## Judges

| Capability | Status | Note |
|---|---|---|
| Rubric-driven scoring, temperature 0 | **Built** | |
| Rubrics resolve for DB pipelines | **Built** | Fixed 2026-08-04; previously aborted the run with `FileNotFoundError` |
| Structured output (schema-forced) | **Built** | Forced natively per provider: Anthropic via a required `emit_verdict` tool, openai_compat via `json_schema` (retrying as `json_object` on a 400), Ollama via `format`. Validated with `jsonschema`; a reply that does not conform is an error, never unvalidated data passed downstream |
| Evidence enforcement | **Built** | With `require_evidence: true`, a verdict with no quotes — or with a quote that does not appear in the response — fails and says so. Matching folds case, whitespace and typographic look-alikes, and honours `...` elision, so only fabrication fails |
| Self-consistency (n>1) | **Built** | `samples:` on the rubric (or `samples=` on `run_judge_check`) takes the median score per dimension across N temperature-0 calls and records the per-dimension spread under `evidence.consistency` |
| Missing dimension scores | **Built** | An unscored dimension fails by name instead of silently reading 0 |

## Checks and sandbox

| Capability | Status | Note |
|---|---|---|
| Template library | **Partial** | 11 of the 16 designed primitives. Missing: `equals`, `cost_bound`, `citation_resolves`, `enum_value`, `length_bound` |
| Isolated subprocess, CPU/memory rlimits, wall-clock timeout | **Built** | POSIX only; rlimits no-op elsewhere |
| Import allowlist, `open`/`exec`/`eval`/`compile` removed | **Built** | Installed before the module body executes (fixed 2026-08-04) |
| Socket factories patched | **Built** | Defence in depth, not an egress block |
| Filesystem isolation | **Built** | The child runs in a throwaway cwd with an empty environment, and the source is read by the parent so the child never needs `open`. Not a chroot: a check that defeated the import allowlist could still address absolute paths |
| OS-level network deny-all | **Partial** | A network namespace with no interfaces where unprivileged `unshare` works (Linux with user namespaces). Elsewhere it falls back to patched socket factories behind the import allowlist. `sandbox_tier()` reports which applies |
| Hardened tier (gVisor / Firecracker / WASM) | **Planned** | For genuinely untrusted third-party code; run Assay inside your own VM or container boundary until then |

## Engine and runs

| Capability | Status | Note |
|---|---|---|
| Execute a run, capture full request + response per case | **Built** | |
| Case-level gating (all required checks pass) | **Built** | |
| Run-level / suite-level gating | **Planned** | `Spec.gating` is parsed (`spec/models.py:57`) and never read; `engine/gating.py:10` is dead code; suite-level `gate:` is silently discarded |
| Token and cost capture | **Planned** | No columns on `CaseResult`; `Run.total_cost_usd` is always 0 |
| Regression vs. the last approved run | **Planned** | |
| Run progress | **Partial** | A browser run returns immediately and polls a done/total progress view; no cancel, and runs execute on an in-process background thread rather than a queue |
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
| Provider credential status | **Built** | Settings > Providers lists each adapter's env var and whether it is set. Names only, never values |
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
| Requirement coverage matrix | **Built** | Both directions: pass counts per requirement, requirements with no test at all, and orphan tests citing a requirement that no longer exists. Needs the requirement list, which DB pipelines store; a file-based run says so rather than implying full coverage |

## CLI

**Built:** `init`, `generate`, `run`, `users`, `report`, `approve`, `serve`, `target ping`,
`pipeline import`, `pipeline list`, `pipeline activate`, `pipeline show`.

**Planned:** `watch`, `export`. Report export is currently HTTP-only, plus an implicit export
at the end of `assay run`.

## Storage

| Capability | Status | Note |
|---|---|---|
| SQLite default | **Built** | |
| Postgres via `ASSAY_DB_URL` | **Partial** | No code change is needed, and migrations are now dialect-aware, but the Postgres path is not exercised in CI |
| Schema migrations | **Partial** | Hand-rolled additive `ALTER TABLE` via `store/db.py:_add_columns`; no Alembic |
| Cases as first-class rows | **Planned** | Cases live as JSON inside `PipelineVersion.config` |
| `TargetModel.interface_hash` | **Built** | Written on every run from the target's interface file, so a report records what it was tested against. Null when no interface is supplied |

## Roadmap

Phases referenced from [`user-journeys.md`](user-journeys.md). Market-ready minimum is
**P0, P1, P2, P4, P6**.

| Phase | Objective |
|---|---|
| **P0** | Credential and provider foundation: one resolution path for which model, which key, and whether it is configured |
| **P1** | A real LLM in every build path, including the web UI. No silent heuristic fallback |
| **P2** | Real judging: structured output, evidence enforcement, self-consistency, real rubric generation |
| **P3** | Interface grounding and case generation, including dataset binding. Parsing is in (`generator/interface.py`); the builder does not consume it yet |
| **P4** | Real codegen with validation, sandbox dry-run, and a repair loop |
| **P5** | Token and cost capture |
| **P6** | Retire the mock default — a fresh install with no keys must not be able to produce a green report |
| **P7** | Test tiering: offline, recorded-cassette, and live-provider tiers |

## Keeping this file true

`tests/test_docs_truth.py` asserts that the adapter table here and in the README matches
`adapters/registry.py`, and that every CLI command named in the docs exists in `cli.py`.
It fails the build on drift. It cannot police prose — that is on us, in review.
