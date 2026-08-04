# Assay — user journeys

**Actor:** builder / integrator (the engineer who defines requirements, connects a target,
generates and reviews the pipeline, and iterates on it).
**Surface:** web UI first; the CLI is noted as the secondary path where it exists.
**Framing:** each journey describes the **target state** — how it must work for Assay to be
market-ready — with every step marked against what is actually built today.

> Reviewer/approver, CI/automation, and admin/operator journeys are not yet written.
> J11.6 and J12.8 are the seams where they attach.

## Conventions

| Marker | Meaning |
|---|---|
| **BUILT** | Works today, end to end |
| **PARTIAL** | Exists but incomplete, or works only in the offline/mock path |
| **MISSING** | Not implemented |
| **BROKEN** | Shipped and reachable in the UI, but fails at run time |

Every step names a **UI touchpoint** (screen + control), a **route**, the **business logic**
(`module.function`), and the **state effect** (what row, column, or transition changes).
Gap notes cite `file:line` and the roadmap phase that closes them — see
[`STATUS.md`](STATUS.md) for the capability matrix and the phase definitions.

Journeys are numbered `J1…J12`; failure branches are lettered (`J6-F1`).

---

## Journey index

| # | Journey | Trigger | Success criterion |
|---|---|---|---|
| [J1](#j1--land-and-orient-zero-state) | Land and orient (zero state) | First `assay serve` | Builder knows the next action within one screen |
| [J2](#j2--configure-a-model-provider) | Configure a model provider | No API key configured | A real provider is reachable and shown as configured |
| [J3](#j3--create-a-project) | Create a project | "New project" | Project exists, wizard opens scoped to it |
| [J4](#j4--define-requirements-wizard-step-1) | Define requirements | Wizard step 1 | Requirements captured, intents previewed |
| [J5](#j5--connect-the-target-and-import-its-interface-wizard-step-2) | Connect the target + import its interface | Wizard step 2 | Live connection confirmed; interface parsed |
| [J6](#j6--generate-the-pipeline) | Generate the pipeline | "Generate" | Draft version with real checks, cases, rubrics |
| [J7](#j7--review-the-generated-pipeline) | Review the generated pipeline | Draft exists | Builder has read every check and understands coverage |
| [J8](#j8--fix-a-check) | Fix a check | Bad check in review | Corrected check persisted to a draft version |
| [J9](#j9--activate-a-version) | Activate a version | Reviewed draft | Exactly one active version; previous archived |
| [J10](#j10--run-against-the-target) | Run against the target | Active version | Report at `ready_for_review` with full evidence |
| [J11](#j11--triage-results-and-hand-off-for-approval) | Triage results and hand off | Report exists | Reviewer assigned; builder knows what failed and why |
| [J12](#j12--iterate-track-regressions-wire-into-ci) | Iterate, track regressions, wire into CI | Report reviewed | Next version measured against the approved baseline |

---

## J1 — Land and orient (zero state)

**Goal:** a builder who just ran `assay serve` knows what to do next.
**Precondition:** empty database. **Success:** one obvious primary action.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Open the app | Reports queue (`queue.html`) | `GET /` | `server/app.py:141` | — | **BUILT** |
| 2 | See an empty state that names the next step | inline empty state, `queue.html:95` | — | — | — | **PARTIAL** — six duplicated hand-rolled empty states, no shared partial; the zero-state CTA points at reports rather than "create a project" |
| 3 | See whether a model provider is configured | global banner | — | `llm.provider.credential_status` | — | **PARTIAL** — Settings > Providers reports it; there is still no zero-state banner on the landing screen |

**Gap:** the landing screen is a report queue, which on day one is empty and meaningless.
Target: zero state routes into [J3](#j3--create-a-project) and surfaces
[J2](#j2--configure-a-model-provider)'s credential status.

---

## J2 — Configure a model provider

> New journey. The product cannot be real without it.

**Goal:** connect real Anthropic / OpenAI / Ollama credentials, for **building** and for
**judging** — these are separate concerns and may use different models.
**Success:** Settings shows at least one provider as configured, and Generate is enabled.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Open Settings | `settings.html` | `GET /settings` | `server/app.py:446` | — | **BUILT** |
| 2 | Pick default judge adapter + model | `model_selector` macro, `settings.html:11-53` | `POST /settings/judge` | `server/app.py:480` | `WorkspaceSetting` `judge_adapter` / `judge_model` | **BUILT** |
| 3 | See per-provider credential status — e.g. "`ANTHROPIC_API_KEY` not set" | Settings → Providers card | `GET /settings/builder` | `llm.provider.credential_overview` | — | **BUILT** — env var name plus a configured/not-configured badge per adapter. Names only, never values |
| 4 | Set the **builder** model separately from the judge model | Settings → Providers card | `POST /settings/builder` | `llm.provider.builder_choice` | `WorkspaceSetting` `builder_adapter` / `builder_model` | **BUILT** — falls back to the judge setting, then to a built-in default, resolved at read time |
| 5 | Name a per-target key env var | wizard step 2 `key_env` field, `_adapter_fields.html:36` | `POST /pipelines/generate` | `TargetSpec.key_env` → `llm.provider.read_key` | persisted in `PipelineVersion.config.target` | **BUILT** — the name reaches the adapter and the value is read from the environment at client-construction time. `key_env: ""` means the target takes no credential. The spec models are now `extra="forbid"`, so the next dropped field is a loud validation error rather than silent loss |

**J2-F1 — no key set, builder clicks Generate.** Returns HTTP 422 naming the exact env var,
rendered in the step-2 `.banner-danger` next to the fields that need fixing. The wizard stays
on step 2 and nothing is persisted.

**Credential policy:** API keys live in the environment only, referenced by name (`key_env`).
They are never written to the database, never rendered back to the browser, and never included
in an exported report.

---

## J3 — Create a project

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Browse projects | `projects.html` | `GET /projects` | `server/app.py:488` | — | **BUILT** |
| 2 | "New project" → name it | Alpine modal, `projects.html:7,19` | `POST /projects` | `server/app.py:534` | none — a project is a string column on `Pipeline`, not a row | **PARTIAL** — no first-class Project entity, so a project cannot be renamed, described, archived, or deleted |
| 3 | Land in the wizard scoped to the project | — | `303 → /pipelines/new?project=` | `server/app.py:534` | — | **BUILT** |

---

## J4 — Define requirements (wizard step 1)

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Open the wizard | `pipeline_new.html`, Alpine `step: 1` | `GET /pipelines/new` | `server/app.py:265` | — | **BUILT** |
| 2 | Write requirements freehand | step-1 textarea | — | — | localStorage draft (survives refresh) | **BUILT** |
| 3 | Add a metric from the catalogue | metric chips, `pipeline_new.html:171` | — | `_METRIC_CATALOGUE`, `server/app.py:~240` | — | **BUILT** |
| 4 | See intents previewed from the requirements | step-1 preview list | `POST /pipelines/preview` | `generator.build.derive_intents` | — | **BUILT** — the route resolves the workspace build model; an unconfigured or failing model becomes a 422 rendered in the step-2 danger banner |
| 5 | See each intent traced to a numbered requirement | `R1…Rn` in the review page and coverage matrix | `POST /pipelines/preview` | `generator.ingest.split_requirements` | — | **PARTIAL** — refs are real end to end (one suite per requirement), but the step-1 preview list does not yet show the ref per check |
| 6 | Save and resume later | "Save draft" | `POST /pipelines/save-draft` | `pipeline.service.save_draft_from_requirements:196` | draft `PipelineVersion`, `step_reached` | **BUILT** |

---

## J5 — Connect the target and import its interface (wizard step 2)

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Choose adapter + model | `_adapter_fields.html`, `model_selector` | — | `_ADAPTER_NAMES`, `server/app.py:261` | — | **PARTIAL** — `mock` is offered as an ordinary choice; it must become test-only (P6) |
| 2 | Test the connection | "Test connection", `pipeline_new.html:227` | `POST /connection-test` | `engine.connection.test_connection` | — | **BUILT** — a live badge with latency that now distinguishes unreachable from unauthenticated, naming the missing variable instead of showing a green "Connected" with no key |
| 3 | Upload the interface file (Postman / OpenAPI / MCP) | step-2 file field | `POST /pipelines/generate` | `generator.ingest.parse_interface` | `TargetModel.interface_hash` | **MISSING** — no upload control, and the interface is never parsed at build time, so generated checks cannot reference real response fields (P3) |
| 4 | Point at a golden dataset | step-2 dataset field | — | `generator.casegen.load_dataset` | — | **MISSING** — `datasets/` is scaffolded by `cli.py:25` and never read (P3) |

**J5-F1 — endpoint unreachable:** the badge shows the error. **BUILT.**
**J5-F2 — reachable but unauthorised (401):** names the missing key env var. **BUILT.**

---

## J6 — Generate the pipeline

The product's core moment. **Success:** a draft version whose checks a builder would trust.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Click Generate | step-2 primary button, `pipeline_new.html:310` | `POST /pipelines/generate` | `server/app.py:367` | — | **BUILT** |
| 2 | Derive intents with a real LLM | progress state | — | `generator.build.derive_intents` | — | **BUILT** — `_derive_intents_or_422` resolves the build model; nothing is persisted when the build fails |
| 3 | Route each intent → template / generated / judge | — | — | `generator.build.intents_to_spec:81` | `PipelineVersion.config` | **PARTIAL** — routing is a field on the single LLM call; no rationale captured, no per-intent override surface |
| 4 | Materialise **template** checks | — | — | `checks/library.py:125` | config | **BUILT** — 11 of the design's 16 primitives |
| 5 | Materialise **generated** checks | — | — | `generator.codegen.generate_check` | `PipelineVersion.generated_sources` | **MISSING** — `generator/build.py:144` hardcodes `generated_sources = {}`, so a `generated` intent produces a spec pointing at a file that is never written and the run dies at `sandbox/runner.py:103` (P4) |
| 6 | Materialise **judge** rubrics | — | — | `generator.rubricgen.generate_rubric` | `PipelineVersion.rubrics` | **PARTIAL** — a fixed one-dimension YAML synthesised from the assertion string (`generator/build.py:131-142`); no anchors, no output schema, no evidence requirement (P2) |
| 7 | Generate concrete test cases | — | — | `generator.casegen.generate_cases` | config | **MISSING** — every case is `"input": {}` (`generator/build.py:98`) (P3) |
| 8 | Auto-activate and land on Review | step 3 / `HX-Redirect` | — | `pipeline.service.activate_version:164` | version `status=active` | **BUILT** |

**J6-F1 — no credentials:** 422 naming the env var, never a silent heuristic fallback — **BUILT**.
**J6-F2 — codegen fails validation after retries:** the intent degrades to a judge check, with
the reason recorded in `config["build_meta"]` and shown in review (P4).
**J6-F3 — provider 429 / timeout:** the partial draft is saved and resumable; never a lost
wizard (P0/P7).

---

## J7 — Review the generated pipeline

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Open the review screen | `pipeline_review.html` | `GET /pipelines/{pid}/versions/{vid}/review` | `server/app.py:813` | — | **BUILT** |
| 2 | See every check with type + determinism badges | check rows, `pipeline_review.html:95` | `GET /pipelines/versions/{vid}/checks` | `server/app.py:847` | — | **BUILT** |
| 3 | Expand a check's source or rubric | collapsible row | — | `generated_sources` / `rubrics` | — | **PARTIAL** — the panel renders, but `generated_sources` is always empty (P4) |
| 4 | See requirement coverage in both directions | coverage meter, `pipeline_review.html:64-76` | — | `reporting.exporters._coverage:35` | — | **PARTIAL** — the bar is a check-*type* breakdown, not requirement coverage; uncovered requirements and orphan tests are never computed (P1/P3) |
| 5 | See what the build decided and why | rationale column | — | `config["build_meta"]` | — | **MISSING** — routing rationale is never persisted (P1) |

---

## J8 — Fix a check

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Retune a template check's params | inline params form, `pipeline_review.html:166` | `PATCH /pipelines/versions/{vid}/check-params` | `pipeline.service.update_check_params:81` | draft config | **BUILT** — 409 on active versions |
| 2 | Edit generated source by hand | source editor | `PATCH /pipelines/versions/{vid}/checks/{path}` | `pipeline.service.update_check_source:273` | `generated_sources` | **BUILT** (409 on active) — but there is never any source to edit (P4) |
| 3 | Regenerate a single check | "Regenerate", `pipeline_review.html:142` | `POST …/checks/{path}/regenerate` | `pipeline.service.regenerate_check:233` | new draft `PipelineVersion` | **BROKEN** — writes a `# TODO: implement check logic` stub with the wrong signature (`def <stem>(response, **kwargs)`), where the sandbox requires a module-level `check(response, context) -> dict` (`sandbox/runner.py:78-79`). Every regenerated check fails at run time (P4) |
| 4 | See the regenerated check dry-run before accepting it | diff + dry-run panel | — | `generator.codegen.generate_check` dry-run | — | **MISSING** (P4) |

---

## J9 — Activate a version

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Read the activation gate card | `pipeline_review.html:202-223` | — | — | — | **BUILT** |
| 2 | Activate | primary button, `pipeline_review.html:215` | `POST /pipelines/versions/{vid}/activate` | `pipeline.service.activate_version:164` | `status=active`; previous active → `archived`; actor + timestamp recorded | **BUILT** |
| 3 | Be blocked when not authorised | 403 | — | `engine.review._check_reviewer:28` | — | **BUILT** — enforced mode requires a reviewer identity |
| 4 | Land back on the project | — | `HX-Redirect → /projects/{name}` | `server/app.py:963` | — | **BUILT** |

---

## J10 — Run against the target

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Trigger a run | "Run" on project detail (`project_detail.html:107`) or review (`pipeline_review.html:241`) | `POST /pipelines/versions/{vid}/run` | `server/app.py:703` → `engine.runner.execute_run:86` | `Run` + `CaseResult`s + `Report` | **BUILT** |
| 2 | Call the target once per case | — | — | adapter `.complete()` | full request + response captured (`engine/runner.py:135-140`) | **BUILT** |
| 3 | Execute template checks | — | — | `checks/registry.py:11` | `CheckResult` | **BUILT** |
| 4 | Execute generated checks in the sandbox | — | — | `sandbox/runner.py` | `CheckResult` | **PARTIAL** — the sandbox runs, but the import allowlist and the removal of `open`/`exec`/`eval`/`compile` install **after** `exec_module` (`sandbox/runner.py:62-76`), so a check's top-level code runs unsandboxed |
| 5 | Execute judge checks | — | — | `judges/judge.py` | `CheckResult` | **BROKEN** — rubrics are never materialised (`engine/runner.py:51-67` handles only `generated_sources`), so `judges/judge.py:18` raises `FileNotFoundError` and kills the entire run (P2) |
| 6 | Record tokens and cost per case | — | — | `pricing.estimate_cost` | `CaseResult` cost columns | **MISSING** — the columns do not exist; `Run.total_cost_usd` is always 0 (P5) |
| 7 | See progress during a long run | `run_progress.html` + polled `_run_progress.html` | `GET /runs/{run_id}`, `GET /runs/{run_id}/progress` | `engine.runner.start_run`, `run_progress` | `Run.cases_total`, per-case commits | **PARTIAL** — a browser run now returns immediately and polls a done/total progress bar, redirecting to the report when it lands. Setup failures still raise synchronously. No cancel yet, and no queue: runs execute on a background thread in-process |
| 8 | Land at `ready_for_review` | `HX-Redirect` to the report | — | `engine.review.submit_for_review:140` | `Report.state` transition + `StateTransition` row | **BUILT** |

---

## J11 — Triage results and hand off for approval

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Read the report | `report_detail.html` | `GET /reports/{id}/view` | `server/app.py:1062` | — | **BUILT** |
| 2 | Open a failing case transcript | `_transcript.html` (htmx) | `GET …/cases/{cid}/transcript` | `server/app.py:1181` | — | **BUILT** |
| 3 | See *why* a check failed — evidence and quotes | verdict block | — | `judges/judge.py` evidence | — | **PARTIAL** — evidence is stored but never enforced; judge quotes are unverified against the response (P2) |
| 4 | Assign a reviewer | `report_detail.html:68` | `POST /reports/{id}/assign` | `engine.review.assign_reviewer:174` | `Report.reviewer` | **BUILT** |
| 5 | Export the report | `report_detail.html:43-49` | `GET /reports/{id}/export/{fmt}` | `reporting.exporters.export_report:79` | — | **PARTIAL** — JSON/MD/HTML only; the "HTML" export is Markdown escaped inside a `<pre>`; no PDF |
| 6 | Hand off — reviewer adjudicates and approves | *(crosses into the reviewer journey)* | `POST …/adjudicate`, `POST …/approve` | `engine.review.adjudicate_case:192`, `approve_report:169` | `state=done`, report locked, approver recorded | **BUILT** |

---

## J12 — Iterate, track regressions, wire into CI

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Edit an active pipeline → new draft version | "Edit", `project_detail.html:103` | `GET /pipelines/new?resume=` | `server/app.py:265` | new draft version | **BUILT** |
| 2 | See the approved baseline for the project | baseline card, `project_detail.html:29` | `GET /projects/{name}` | `server/app.py:542` | — | **BUILT** |
| 3 | See run history for a pipeline | `runs.html` | `GET /pipelines/{pid}/runs` | — | — | **MISSING** — never built |
| 4 | See a pass-rate trend | trend chart | — | — | — | **MISSING** — no charting library is loaded at all |
| 5 | Be told when a run regresses against the baseline | regression banner | — | `regression_against` | — | **MISSING** — no comparison logic exists, so "we got worse" is silent |
| 6 | See run-level gating enforced | verdict block | — | `engine/gating.py` | — | **PARTIAL** — `Spec.gating` is parsed (`spec/models.py:57`) and never read; `engine/gating.py:10 run_passed` is dead code; suite-level `gate:` is silently discarded by pydantic |
| 7 | Export the spec to the repo for eval-as-code | "Download assay.yaml" | `GET /pipelines/versions/{vid}/export` | — | — | **MISSING** — DB pipelines cannot round-trip to a committable file, and `assay export` does not exist |
| 8 | Run in CI on merge | `.github/workflows/eval.yml` | `POST /hooks/run` | `server/app.py:1275` | report at `ready_for_review`, never `done` | **PARTIAL** — the webhook works; the shipped workflow targets a file-based pipeline, not the DB one |

---

## Ranked gaps

Ordered by what most blocks a credible market-ready claim. Closed items are struck from
the list as their journey steps flip to BUILT; the roadmap phase that closed each one is
named so this table stays auditable.

| # | Gap | Journey | Phase | State |
|---|---|---|---|---|
| 1 | No codegen — the stated differentiator does not exist | J6.5 | P4 | **Open** |
| 2 | Empty cases, no interface grounding — generated checks cannot reference real fields | J6.7, J5.3 | P3 | **Open** |
| 3 | Judge rubrics are a fixed single dimension, and evidence quotes are never verified | J6.6, J11.3 | P2 | **Open** |
| 4 | No token or cost capture, so a real-model run reports zero spend | J10.6 | P5 | **Open** |
| 5 | Mock adapters are still selectable as ordinary targets | J5.1 | P6 | **Open** |
| 6 | No run history, trends, or regression detection | J12.3-5 | post-P6 | **Open** |
| 7 | No spec export — the eval-as-code thesis is unreachable from the UI | J12.7 | post-P6 | **Open** |
| 8 | Zero state and no Project entity | J1, J3 | UI polish | **Open** |
| — | Judge rubrics never materialised — broke every run containing a judge check | J10.5 | hotfix | Closed |
| — | "Regenerate" wrote code the sandbox could never load | J8.3 | hotfix | Closed |
| — | Sandbox locked down after module load — the containment claim was false | J10.4 | hotfix | Closed |
| — | The UI never called an LLM — the builder promise was unmet on the primary surface | J6.2, J4.4 | P1 | Closed |
| — | No credential journey — nothing told you a key was missing | J2 | P0 | Closed |
| — | A long run gave no progress feedback | J10.7 | P1 | Closed |

## Keeping this document true

A phase is not done until the journey steps it claims to close have flipped to **BUILT** in the
same pull request. This file is the acceptance checklist for the roadmap in
[`STATUS.md`](STATUS.md), not a snapshot to be written once and left behind.
