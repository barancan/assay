# Assay — user journeys

**Actors:**

| Actor | Who they are | Journeys |
|---|---|---|
| **Builder / integrator** | The engineer who defines requirements, connects a target, generates and reviews the pipeline, and iterates on it | [J1–J12](#j1--land-and-orient-zero-state) |
| **Reviewer / approver** | The human who is accountable for the verdict. The only actor that can move a report to `done` | [J13–J15](#j13--find-work-in-the-review-queue) |
| **CI / automation** | A non-human caller — a merge hook, a scheduled job, a webhook. Can produce evidence, never a decision | [J16–J17](#j16--trigger-an-eval-from-ci-on-merge) |
| **Admin / operator** | Whoever deploys Assay for a team and owns its posture, its credentials, its database and its spend | [J18–J20](#j18--deploy-assay-for-a-team) |

**Surface:** web UI first; the CLI is noted as the secondary path where it exists.
**Framing:** each journey describes the **target state** — how it must work for Assay to be
market-ready — with every step marked against what is actually built today.

> The builder hands off at [J11.6](#j11--triage-results-and-hand-off-for-approval), which is
> where the reviewer journey picks up. [J12.8](#j12--iterate-track-regressions-wire-into-ci)
> is the seam the CI journey attaches to.

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

Journeys are numbered `J1…J20`; failure branches are lettered (`J6-F1`).

---

## Journey index

### Builder / integrator

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

### Reviewer / approver

| # | Journey | Trigger | Success criterion |
|---|---|---|---|
| [J13](#j13--find-work-in-the-review-queue) | Find work in the review queue | Report at `ready_for_review` | Reviewer reaches the report they are accountable for |
| [J14](#j14--read-a-report-and-understand-what-failed) | Read a report and understand what failed | Report open | Reviewer can say *why* each failing case failed, from evidence |
| [J15](#j15--adjudicate-set-a-verdict-and-approve) | Adjudicate, set a verdict, approve | Reviewer has read the report | Report at `done`, locked, verdict and approver recorded |

### CI / automation

| # | Journey | Trigger | Success criterion |
|---|---|---|---|
| [J16](#j16--trigger-an-eval-from-ci-on-merge) | Trigger an eval from CI on merge | Merge to the default branch | A run executes and its report reaches the team's queue |
| [J17](#j17--notify-hand-off-and-stop) | Notify, hand off, and stop | Run finished | Humans are told; automation goes no further than `ready_for_review` |

### Admin / operator

| # | Journey | Trigger | Success criterion |
|---|---|---|---|
| [J18](#j18--deploy-assay-for-a-team) | Deploy Assay for a team | More than one person needs it | A durable, enforced-auth instance nobody can impersonate into |
| [J19](#j19--configure-providers-accounts-and-roles) | Configure providers, accounts, roles | Instance is up | Providers reachable; at least one reviewer exists |
| [J20](#j20--operate-storage-cost-and-budget) | Operate: storage, cost, budget | Instance in daily use | Operator knows what it costs and can bound it |

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
| 1 | Choose adapter + model | `_adapter_fields.html`, `model_selector` | — | `_ADAPTER_NAMES` | — | **BUILT** — `mock` is no longer offered; it resolves only under `ASSAY_ALLOW_MOCK=1` or an explicit `--offline` |
| 2 | Test the connection | "Test connection", `pipeline_new.html:227` | `POST /connection-test` | `engine.connection.test_connection` | — | **BUILT** — a live badge with latency that now distinguishes unreachable from unauthenticated, naming the missing variable instead of showing a green "Connected" with no key |
| 3 | Supply the interface file (Postman / OpenAPI / MCP) | step-2 interface-file field, `_adapter_fields.html` | `POST /pipelines/generate` | `generator.interface.parse_interface` | `TargetSpec.import_`, `TargetModel.interface_hash` | **PARTIAL** — the field takes a server-side path (matching `assay generate --interface`) and the interface is parsed at build time, so cases use real request fields and the run records what it was tested against. A true browser file *upload* is not built |
| 4 | Point at a golden dataset | — (CLI only) | — | `generator.casegen.load_dataset` | — | **PARTIAL** — `assay generate --dataset` binds cases to a `datasets/*.jsonl` file, and a malformed row names file and line. Not yet exposed in the wizard |

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
| 5 | Materialise **generated** checks | — | — | `generator.codegen.generate_check` | `PipelineVersion.generated_sources` | **BUILT** — model-written source, gated by a static AST check, a sandboxed dry-run and a discrimination test, with up to two repairs. On failure the intent degrades to a judge check and the reason is recorded in `build_meta.codegen_failures` |
| 6 | Materialise **judge** rubrics | — | — | `generator.rubricgen.generate_rubric` | `PipelineVersion.rubrics` | **BUILT** — an LLM-authored rubric with at least two anchored dimensions, each anchor observable rather than a grading word, plus `min_score`, `require_evidence` and the verdict schema. Invalid output is repaired once, then falls back to a deterministic rubric that clears the same validator |
| 7 | Generate concrete test cases | — | — | `generator.casegen.generate_cases` | config | **BUILT** — concrete inputs per intent, grounded on the interface's real request fields, with nominal, empty, boundary and hostile variants. A single gate means no path can emit a case with an empty input |
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
| 4 | See requirement coverage in both directions | coverage meter, `pipeline_review.html` | — | `reporting.exporters.coverage` | — | **PARTIAL** — the exported report names uncovered requirements and orphan tests in both directions; the review screen's bar is still a check-*type* breakdown and does not yet show it |
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
| 6 | Record tokens and cost per case | — | — | `pricing.estimate_cost` | `CaseResult.{input,output,judge}_tokens`, `cost_usd` | **BUILT** — target and judge spend per case, judge samples included. An unpriced model reads "unknown" rather than $0.00 |
| 7 | See progress during a long run | `run_progress.html` + polled `_run_progress.html` | `GET /runs/{run_id}`, `GET /runs/{run_id}/progress` | `engine.runner.start_run`, `run_progress` | `Run.cases_total`, per-case commits | **PARTIAL** — a browser run now returns immediately and polls a done/total progress bar, redirecting to the report when it lands. Setup failures still raise synchronously. No cancel yet, and no queue: runs execute on a background thread in-process |
| 8 | Land at `ready_for_review` | `HX-Redirect` to the report | — | `engine.review.submit_for_review:140` | `Report.state` transition + `StateTransition` row | **BUILT** |

---

## J11 — Triage results and hand off for approval

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Read the report | `report_detail.html` | `GET /reports/{id}/view` | `server/app.py:1062` | — | **BUILT** |
| 2 | Open a failing case transcript | `_transcript.html` (htmx) | `GET …/cases/{cid}/transcript` | `server/app.py:1181` | — | **BUILT** |
| 3 | See *why* a check failed — evidence and quotes | verdict block | — | `judges.judge.run_judge_check` | — | **BUILT** — with `require_evidence`, quotes are verified against the response: a fabricated quote fails the check, and re-cased or re-wrapped spans still pass. Verified and unverified quotes are recorded separately, and a self-consistency spread is stored when the rubric asks for multiple samples |
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
| 8 | Run in CI on merge | `.github/workflows/eval.yml` | `POST /hooks/run` | `server/app.py:1433` | report at `ready_for_review`, never `done` | **PARTIAL** — the webhook works; the shipped workflow targets a file-based pipeline, not the DB one. Continues as [J16](#j16--trigger-an-eval-from-ci-on-merge) |

---

# Reviewer / approver

The governance story. Everything else in Assay produces evidence; this actor produces the
decision, and the product's whole claim is that the decision is made by a named human and
recorded so it can be audited later.

---

## J13 — Find work in the review queue

**Goal:** a reviewer with no prior context opens Assay and finds the reports waiting on them.
**Precondition:** at least one report at `ready_for_review`.
**Success:** the reviewer reaches a report they are accountable for without asking anyone.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Land on the queue | `queue.html` | `GET /` | `server/app.py:143` | — | **BUILT** — every report newest-first, with state, verdict, pass/fail counts, target, pipeline version, who triggered it and who it is assigned to |
| 2 | Be recognised as a reviewer | nav identity, `_nav.html:35-43`; `login.html` | `GET /login`, `POST /login` | `server/app.py:180`, `server/app.py:191` → `config.secret_key:23` | signed `assay_user` cookie | **PARTIAL** — the cookie is signed with `ASSAY_SECRET_KEY` and the login page only lists seeded reviewer/admin accounts (`server/app.py:182`), but it is a name picker with no password, no SSO and no session expiry; and `_require_identity` takes an unverified `X-Assay-User` header *ahead* of the cookie (`server/app.py:60-68`), so the cookie is not the only way in |
| 3 | Filter to what needs review | state dropdown, `queue.html:11-16` | `GET /?state=ready_for_review` | `server/app.py:143-155` | — | **BUILT** |
| 4 | Filter to what is assigned to me | reviewer field, `reports.html:26` | `GET /reports?reviewer=` | `server/app.py:1151` | — | **PARTIAL** — a free-text reviewer filter exists on `/reports`, not on the landing queue, and nothing binds it to the logged-in identity. There is no "assigned to me" view |
| 5 | Be told a report is waiting, without watching the app | — | — | `notifications.factory.get_notifier:6` | `NotificationRecord` row | **PARTIAL** — a Linear issue is created on `ready_for_review` when `ASSAY_LINEAR_API_KEY` is set (`notifications/linear.py:56-95`). No email, no Slack, no in-app inbox, and no UI to configure any of it (see [J17.2](#j17--notify-hand-off-and-stop)) |
| 6 | Open the report | row link, `queue.html:28` | `GET /reports/{id}` | `server/app.py:1201` → `server.app._report_ctx:83` | — | **BUILT** — content-negotiated: JSON for `Accept: application/json`, otherwise `report_detail.html` |

**J13-F1 — enforced mode with no accounts seeded.** The login page lists nobody
(`server/app.py:182` filters to `reviewer`/`admin`) and every privileged action returns 403
from `engine.review._check_reviewer:38`. There is no way out from the browser; someone needs
shell access to the host to run `assay users --add <name> --role reviewer` (`cli.py:186`).
**MISSING** — see [J19.3](#j19--configure-providers-accounts-and-roles).

**J13-F2 — a report the reviewer does not have context for.** The queue row names the target
adapter/model and pipeline version, and the report header adds the content hash and the
interface hash's provenance. There is no link back to the pipeline's requirements text from
the report. **PARTIAL.**

---

## J14 — Read a report and understand what failed

**Goal:** the reviewer can state, for every failing case, what was sent, what came back,
which check objected, and on what evidence — without reading the codebase.
**Success:** no failure is a black box.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | See the shape of the run at a glance | summary bar, `_summary_bar.html:1-14` | `GET /reports/{id}` | `engine.review._recompute_summary:49` | — | **BUILT** — counts are recomputed from `CaseResult.effective_passed`, so they follow human overrides rather than the machine result |
| 2 | See what was under test | meta list, `report_detail.html:27-39` | `GET /reports/{id}` | `server/app.py:_report_ctx:83` | — | **BUILT** — project, run, trigger actor, target adapter/model/endpoint, pipeline version number and content hash |
| 3 | Scan the case table for failures | cases table, `report_detail.html:92-114`, rows from `_case_row.html` | `GET /reports/{id}` | — | — | **BUILT** — failing rows are tinted, and machine / human / effective verdicts are three distinct columns so an override never hides what the machine said |
| 4 | See which requirement a case belongs to | Req ref column, `_case_row.html:13` | — | `CaseResult.requirement_ref` | — | **BUILT** — populated from `Suite.requirement_ref` at run time (`engine/runner.py:219`) |
| 5 | Open a case transcript | `<details>` row, htmx-loaded on first open, `_case_row.html:4-11` | `GET /reports/{id}/cases/{cid}/transcript` | `server/app.py:1339` | — | **BUILT** — `_transcript.html` renders the input, the full response, and every check with its score, threshold and message. Lazy-loaded, so a 200-case report does not ship 200 transcripts |
| 6 | See a judge's evidence and quotes | Evidence `<details>`, `_transcript.html:32-37` | same | `judges.judge.run_judge_check` | — | **BUILT** — verified and unverified quotes are recorded separately, and the self-consistency spread appears under `evidence.consistency` when the rubric asked for multiple samples |
| 7 | See the generated source a check ran | Generated check sources card, `report_detail.html:117-129` | `GET /reports/{id}` | `PipelineVersion.generated_sources` | — | **PARTIAL** — the card renders, but `generated_sources` is always `{}` (`generator/build.py:144`), so the card never appears (P4) |
| 8 | See what the run cost | Cost line in the Markdown export, `reporting/exporters.py:155` | `GET /reports/{id}/export/md` | `Run.total_cost_usd` | — | **BROKEN** — `engine/runner.py:208` sums `resp.cost_usd`, and only `adapters/mock.py:38` ever sets it (to `0.0`). Judge spend is not counted at all. A real run therefore exports `**Cost:** $0.0000`, which is a false number rendered as fact in the artifact a reviewer signs off on (P5) |
| 9 | See why the machine says pass or fail overall | Suggested verdict, `_verdict_block.html:32-42` | `GET /reports/{id}` | `engine.review.compute_suggested_verdict:70` | — | **PARTIAL** — the suggestion is computed and shown, but `pass_policy` is read from `PipelineVersion.config` (`engine/review.py:79`) and no code path ever writes it, so every report in practice falls to the `all_required` branch (`engine/review.py:94`). The reviewer is never told which policy produced the suggestion |

**J14-F1 — a case whose run died half way.** `_execute_cases` commits per case
(`engine/runner.py:215-221`), so the results that landed are kept and `Run.error` records the
exception (`engine/runner.py:243`). The report page does not surface `Run.error` at all — the
reviewer sees a short case list with no explanation. **PARTIAL.**

**J14-F2 — the reviewer wants the raw artifact.** JSON and Markdown export cleanly;
"HTML" is the Markdown escaped inside a `<pre>` (`reporting/exporters.py:168`). No PDF.
**PARTIAL** — as [J11.5](#j11--triage-results-and-hand-off-for-approval).

---

## J15 — Adjudicate, set a verdict, and approve

**Goal:** the reviewer disagrees with the machine where they must, records why, and then
takes personal responsibility for a verdict that is preserved unaltered afterwards.
**Precondition:** report at `ready_for_review`, not locked, actor has reviewer/admin authority.
**Success:** `state=done`, `locked=True`, verdict + reason + approver + timestamp recorded, and
an append-only trail explaining every override that fed it.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Take ownership — assign a reviewer | Reviewer card, `report_detail.html:60-86` | `POST /reports/{id}/assign` | `engine.review.assign_reviewer:174` | `Report.assigned_reviewer` / `assigned_by` / `assigned_at`, plus a `StateTransition` audit row | **BUILT** — the dropdown is populated from seeded reviewer/admin accounts and degrades to a free-text field when there are none (`report_detail.html:73-82`) |
| 2 | Override one case's verdict | Override form per row, `_case_row.html:46-65` | `POST /reports/{id}/cases/{cid}/adjudicate` | `engine.review.adjudicate_case:192` | `CaseResult.human_verdict` / `overridden_by` / `overridden_at` / `override_reason`, plus a `CaseAdjudication` row | **BUILT** — a reason is required to *set* a verdict and optional to *clear* one (`engine/review.py:214`), enforced server-side and mirrored in the disabled-submit binding at `_case_row.html:61` |
| 3 | See the summary move as overrides land | summary bar | `GET /reports/{id}/effective-summary` | `engine.review._recompute_summary:49` | `Report.summary` rewritten | **BUILT** — adjudicate returns the re-rendered row with `HX-Trigger: summaryChanged` (`server/app.py:1304`), which the summary bar listens for (`_summary_bar.html:3`) |
| 4 | Set the report verdict and lock it | Set verdict & lock card, `_verdict_block.html:43-68` | `POST /reports/{id}/set-verdict` | `engine.review.set_verdict:131` → `_apply_verdict:102` | `state=done`, `locked=True`, `verdict`, `verdict_reason`, `verdict_set_by`, `verdict_set_at`, `approved_by`, `approved_at`, and a `StateTransition` row | **BUILT** — verdict must be `pass` or `fail` and the reason must be non-empty (`engine/review.py:133-136`); the report is re-exported afterwards so the on-disk artifact carries the verdict (`server/app.py:1378`) |
| 5 | Send a report back to `pending` for a re-run | — | — | `engine.review.VALID:16` | `ready_for_review → pending` | **MISSING** — the transition is declared legal in `VALID` and no function anywhere calls `_transition(rep, "pending", …)`. Grepping `assay/` finds `_transition` invoked at only two sites, to `done` (`engine/review.py:109`) and to `ready_for_review` (`engine/review.py:144`). The back-edge is a documented capability with no implementation behind it |
| 6 | Be the only actor who can reach `done` | — | `POST /reports/{id}/set-verdict`, `POST /reports/{id}/approve` | `engine.review._check_reviewer:28` | — | **PARTIAL** — *structurally* correct: `_transition` is the only writer of `Report.state` (`engine/review.py:20-25`), the only path to `done` is `_apply_verdict`, and its first statement is `_check_reviewer` (`engine/review.py:104`), while `submit_for_review` deliberately has no such guard so automation can reach `ready_for_review` freely (`engine/review.py:140`). What is *not* enforced is that the actor is a human: the name is taken from an unverified `X-Assay-User` header before the signed cookie is consulted, in enforced mode as much as in open mode (`server/app.py:60-68`). A CI job that sends the header of a seeded reviewer approves the report. `tests/test_auth_posture.py` covers the forged-cookie and unseeded cases; the header path is untested and unblocked |
| 7 | Have the decision preserved | locked banner, `report_detail.html:6-11`; locked verdict card, `_verdict_block.html:1-25` | — | `Report.locked` | — | **BUILT** — locking is enforced at three layers: `VALID["done"] = set()` makes any further transition illegal (`engine/review.py:17`), `adjudicate_case` refuses on a locked report (`engine/review.py:209`), and the templates replace every control with a lock badge (`_case_row.html:66-70`, `report_detail.html:66`) |
| 8 | Leave an audit trail someone else can read later | — | `GET /reports/{id}` with `Accept: application/json` | `StateTransition`, `CaseAdjudication` | — | **PARTIAL** — both tables are append-only and complete, and the JSON representation returns the transition list (`server/app.py:1212-1214`). Neither is rendered anywhere in the UI, so from the browser the history is invisible |

**What a reviewer cannot do — verified, not assumed:**

- **Approve twice, or edit an approved report.** `VALID["done"]` is empty (`engine/review.py:17`),
  so `_transition` raises `ValueError` on any second attempt, and `adjudicate_case` raises
  `PermissionError("report is locked")` first (`engine/review.py:209-210`).
- **Adjudicate a report that is still `pending`.** `adjudicate_case` requires
  `state == "ready_for_review"` (`engine/review.py:211-212`).
- **Adjudicate a case belonging to a different report.** Checked by `run_id`
  (`engine/review.py:218`).
- **Approve without a reason** — via `POST /reports/{id}/set-verdict`, which validates it.
  But `POST /reports/{id}/approve` (`server/app.py:1393`) calls `approve_report`
  (`engine/review.py:169`), the back-compat alias that routes to `_apply_verdict` with **no**
  reason validation and forces `verdict="pass"`. That route is live, unauthenticated beyond the
  identity check, and reachable by anyone who can `curl`. Nothing in the templates links to it —
  grepping the template directory for `approve` finds only prose — so the UI is safe and the
  API is not. **PARTIAL.**
- **Approve in enforced mode before accounts are seeded.** `_check_reviewer` never
  trusts-when-empty in enforced mode (`engine/review.py:36-42`).

**J15-F1 — non-reviewer attempts to approve.** `_check_reviewer` raises `PermissionError`,
the route maps it to 403 (`server/app.py:1401-1402`). **BUILT.**
**J15-F2 — override with no reason.** `adjudicate_case` raises `ValueError`, mapped to 422
(`server/app.py:1279-1280`); the submit button is disabled client-side too. **BUILT.**
**J15-F3 — two reviewers adjudicate the same case concurrently.** Last write wins on
`CaseResult`, and both attempts are preserved in `CaseAdjudication`. There is no optimistic
concurrency check. **PARTIAL** — nothing is lost from the audit log, but the effective verdict
is racy.

---

# CI / automation

A non-human actor. It may generate evidence and it may ask for attention. It may not decide.
Everything below is honest about how far it actually gets today, which is not far.

---

## J16 — Trigger an eval from CI on merge

**Goal:** merging to the default branch runs the pipeline and puts a report in front of the
team.
**Success:** a report exists on the *team's* Assay instance at `ready_for_review`.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Fire on a change to prompts, model config or the spec | — | — | `.github/workflows/eval.yml:3-4` | — | **BUILT** — the shipped workflow triggers on `prompts/**`, `model.config.*`, `assay.yaml`, `generated/**` |
| 2 | Run the pipeline in CI | — | — | `cli.run:159` → `engine.runner.execute_run:248` | `Run`, `CaseResult`s, `Report` at `ready_for_review` | **PARTIAL** — `assay run --trigger ci --by github-actions` (`eval.yml:13`) executes against `assay.yaml` in the checkout and writes to a **SQLite file inside the ephemeral runner** (`config.DB_URL:7`). There is no `ASSAY_DB_URL`, no server URL, and no artifact upload step in the workflow. The report is created and then discarded with the runner. Nothing reaches the queue a reviewer watches |
| 3 | Fail the build when the eval fails | — | — | `cli.run:159-172` | — | **MISSING** — `assay run` prints the report path and always exits 0. There is no `--fail-on`, no non-zero exit on a failing verdict, and run-level gating is parsed and never read (`spec/models.py:64`, `engine/gating.py:10`). CI cannot block a merge on eval results |
| 4 | Trigger a run on a live server instead | — | `POST /hooks/run` | `server/app.py:1433` → `spec.loader.load_spec`, `engine.runner.execute_run:248` | `Run` + `Report` at `ready_for_review` | **PARTIAL** — the route works and correctly lands at `ready_for_review` via `submit_for_review` (`server/app.py:1441`), and exports. But: it takes no `Request`, so it is the **only** mutating route with no identity resolution and no `_require_identity` — unauthenticated in enforced mode as much as in open mode; `body.spec` is an arbitrary server-side path fed straight to `load_spec` with no containment; it can only run a **file-based** spec, with no `pipeline_version_id` parameter, so the DB pipelines the whole wizard produces are unreachable from it; it is fully synchronous, so a real-model run holds the HTTP request open for minutes; and it has no exception handling, so a bad path is a 500 traceback rather than a 4xx |
| 5 | Authenticate the caller | — | — | `server/app.py:60-68` | — | **MISSING** for the webhook (no identity at all) and **PARTIAL** for `POST /pipelines/versions/{vid}/run`, which resolves an actor but accepts an unverified `X-Assay-User` header as proof of it |
| 6 | Get a machine-readable result back | — | `POST /pipelines/versions/{vid}/run` | `server/app.py:813` | — | **BUILT** — the htmx/browser path returns immediately with `HX-Redirect` to a progress page via `start_run:268`, and every non-htmx caller keeps synchronous semantics and gets `{run_id, report_id}` from `execute_run:248` (`server/app.py:826-854`). The split is deliberate and documented in the route |

**J16-F1 — target unreachable from the runner.** `_setup_run` calls `test_connection` before
creating the `Run` row (`engine/runner.py:167`), so the failure is loud and no orphan run is
persisted. **BUILT.**
**J16-F2 — no API key in CI.** The adapter raises `LLMConfigError` naming the variable
(`llm/provider.py:105`). The workflow sets no secrets at all, so this is what a fresh copy of
`eval.yml` actually does on first run unless the user adds them. **BUILT** (the error), **MISSING**
(the workflow does not document or wire the secret).

**Gap, stated plainly:** the shipped CI story is a demonstration, not an integration. It runs
an eval and throws the result away. Making it real needs three things that do not exist:
`ASSAY_DB_URL` (or a server URL) in the workflow, a webhook that can address a DB pipeline
version, and an authentication mechanism for machine callers that is not a spoofable header.

---

## J17 — Notify, hand off, and stop

**Goal:** the automation tells a human and then stops, by design.
**Success:** the report is visible and someone knows about it; nothing automated advances it.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Land at `ready_for_review` and go no further | — | — | `engine.review.submit_for_review:140` | `pending → ready_for_review` + `StateTransition` | **BUILT** — `submit_for_review` has no `_check_reviewer` guard by design, and there is no code path from `ready_for_review` to `done` that is not guarded by one. This is the product's central structural claim and it holds at the engine layer (see [J15.6](#j15--adjudicate-set-a-verdict-and-approve) for what it does *not* claim) |
| 2 | Notify a human | — | — | `engine.review._fire:61` → `notifications.factory.get_notifier:6` | `NotificationRecord` | **PARTIAL** — one channel, Linear, selected purely by the presence of `ASSAY_LINEAR_API_KEY` (`notifications/factory.py:7`). An issue is created on `ready_for_review` and a comment posted on `approved` (`notifications/linear.py:50-54`). No email, no Slack, no generic webhook, and no UI to configure any of it — the backend exists and the settings screen does not mention it |
| 3 | Never let a failed notification break a transition | — | — | `engine.review._fire:61-67` | — | **BUILT** — `_fire` swallows and logs; `tests/test_notifications.py:110-113` covers a deliberately broken notifier |
| 4 | Alert on a regression against the approved baseline | — | — | — | — | **MISSING** — the baseline itself is computed and shown on the project page (`server/app.py:678-690`), but nothing compares a new run to it, so "we got worse" is silent in the queue and in the notification alike ([J12.5](#j12--iterate-track-regressions-wire-into-ci)) |
| 5 | Report on a run that errored | — | — | `engine.runner._mark_run_failed:239` | `Run.status="error"`, `Run.error` | **PARTIAL** — a background run that dies records the exception on the row, and the progress fragment carries it (`engine/runner.py:358`). No notification fires on failure — `_fire` is only called for `ready_for_review` and `approved` — so a crashed nightly run is silent |
| 6 | Survive the process it started in | — | — | `engine.runner.start_run:268`, `_RUN_THREADS:309` | — | **PARTIAL** — background runs are in-process daemon threads. There is no queue, no persistence of pending work, and no cancel. A restart mid-run leaves a `Run` row stuck at `status="running"` forever, with no reaper |

---

# Admin / operator

Whoever runs Assay for other people. Their journey is mostly environment variables, which is
a design choice worth stating: nothing security-relevant is configurable from the browser.

---

## J18 — Deploy Assay for a team

**Goal:** a durable instance more than one person can trust.
**Success:** enforced auth, a real signing key, Postgres, and no way to impersonate a reviewer
by typing a name.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Get an image | — | — | `Dockerfile:8` | — | **BUILT** — installs `.[server,anthropic,openai]` plus `psycopg[binary]`, serves on `0.0.0.0:8000` (`Dockerfile:11`) |
| 2 | Bring up app + database together | — | — | `docker-compose.yml` | — | **BUILT** — Postgres 16 with a `pg_isready` healthcheck and a `service_healthy` dependency (`docker-compose.yml:20-36`), so the app never races an unready database |
| 3 | Turn on enforced auth | Settings → Auth posture (read-only badge), `settings.html:128-147` | `GET /settings` | `config.auth_mode:18` | — | **BUILT** — `ASSAY_AUTH=enforced` is set in the compose file (`docker-compose.yml:9`); the setting is env-only and correctly *not* editable from the UI |
| 4 | Be prevented from deploying insecurely | — | — | `config.enforce_posture_or_raise:33` | — | **BUILT** — enforced mode plus the public dev signing key is a `RuntimeError` at import time (`server/app.py:25`) and at `assay serve` (`cli.py:345`), with the exact command to generate a key. The compose file makes the same failure a shell error before the container starts (`docker-compose.yml:13`). Open mode on a non-loopback bind prints a warning naming the risk (`cli.py:325-338`). What none of this covers is the `X-Assay-User` header, which stays trusted in enforced mode ([J15.6](#j15--adjudicate-set-a-verdict-and-approve)) |
| 5 | Keep exported reports across restarts | — | — | `config.ASSAY_DIR:6`, `config.REPORTS_DIR:8` | — | **BROKEN** — the compose file mounts a volume at `/root/.assay` (`docker-compose.yml:19`) but never sets `ASSAY_HOME`, and the image's `WORKDIR` is `/app` (`Dockerfile:2`). `ASSAY_DIR` therefore resolves to the relative path `.assay` → `/app/.assay`, outside the volume. Every exported JSON/Markdown/HTML report is lost on `docker compose down`. The database survives, so the evidence survives; the signed artifacts do not |
| 6 | Terminate TLS | — | — | — | — | **MISSING** — no reverse proxy, no TLS guidance, no `secure` flag on the session cookie (`server/app.py:195` sets `httponly` and `samesite="strict"` only). A signed identity cookie crosses the wire in the clear unless the operator puts something in front of it themselves |

---

## J19 — Configure providers, accounts, and roles

**Goal:** the instance can reach a model, and at least one human can approve.
**Success:** Settings shows a configured provider, and `assay users` lists a reviewer.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | See which providers are usable | Providers card, `settings.html:56-97` | `GET /settings` | `llm.provider.credential_overview:85` → `credential_status:66` | — | **BUILT** — one row per adapter in `DEFAULT_KEY_ENV` (`llm/provider.py:24-30`) with the variable *name* and a configured/not-configured badge. Never a value, and no client is constructed to find out |
| 2 | Choose the judge and builder models separately | Settings cards, `settings.html:11-53` and `99-125` | `POST /settings/judge`, `POST /settings/builder` | `server/app.py:568`, `server/app.py:590` | `WorkspaceSetting` rows | **PARTIAL** — both persist correctly, and `builder_choice:163` resolves builder → judge → built-in default at read time so no seed row is needed. But neither route calls `_require_identity`: in enforced mode any unauthenticated caller can repoint the workspace's models. Every other mutating route on the server is guarded |
| 3 | Add a reviewer account | Accounts card, `settings.html:150-179` | — | — | — | **MISSING** — the card is a read-only table with no form. Its own empty-state prose says "add reviewers **here**" (`settings.html:176`), which is false: the only way to create a `User` is `assay users --add <name> --role reviewer` on the host (`cli.py:186-195`). Nothing can change a role, rename an account, or remove one — there is no update or delete path in the CLI either |
| 4 | Bootstrap the very first reviewer in enforced mode | — | — | `engine.review._check_reviewer:36-42` | — | **MISSING** — the chicken-and-egg case. Enforced mode never trusts-when-empty, so a freshly deployed instance with no seeded users cannot approve anything, cannot assign anyone, and cannot adjudicate. The container has no interactive path to fix it; the operator must `docker compose exec` into it. This is the single gap that makes a first enforced deployment feel broken |
| 5 | Rotate a provider key | — | — | `llm.provider.read_key:93` | — | **BUILT** by construction — keys are read from the environment at client-construction time and never persisted (`llm/provider.py:8`, `llm/provider.py:99-110`), so rotation is a restart with a new value and nothing in the database goes stale |
| 6 | Point one target at a different key than the workspace default | wizard step 2 `key_env` field | `POST /pipelines/generate` | `llm.provider.key_env_for:51` | `PipelineVersion.config.target.key_env` | **BUILT** — an explicit `key_env` always wins, and `key_env: ""` means "this target takes no credential" |

---

## J20 — Operate: storage, cost, and budget

**Goal:** the operator knows where the data lives, what the instance spends, and how to bound it.
**Success:** none of those three are guesses.

| # | Action | UI touchpoint | Route | Business logic | State effect | Status |
|---|---|---|---|---|---|---|
| 1 | Move from SQLite to Postgres | — | — | `config.DB_URL:7` → `store.db.init_db:73` | — | **PARTIAL** — no code change is needed, and the hand-rolled migrations are dialect-aware (`store/db.py:16-28`), so `ALTER TABLE` emits `TIMESTAMP` on Postgres rather than SQLite's `DATETIME`. But `_add_columns` is additive-and-nullable only (`store/db.py:31-44`), there is no Alembic and no down-path, and `.github/workflows/ci.yml` runs the suite against SQLite only — the Postgres path this deployment depends on is never exercised in CI |
| 2 | See what a run cost | — | — | `Run.total_cost_usd` | — | **MISSING** — the column exists (`store/models.py:85`) and `engine/runner.py:208` sums `resp.cost_usd`, but only the mock adapter ever populates it, and always with `0.0` (`adapters/mock.py:38`). There are no per-case token columns on `CaseResult` at all, and judge calls — often the majority of an eval's spend — are never counted (P5) |
| 3 | Cap spend before it happens | — | — | — | — | **MISSING** — grepping `assay/` for `budget` finds one thing: the `latency_bound` metric label. There is no per-run cap, no per-project cap, no daily ceiling, no cost estimate before Generate or Run, and no confirmation step. A 500-case pipeline against a frontier model is one unguarded button click, and `POST /hooks/run` will do it unauthenticated |
| 4 | Stop a run in flight | — | — | `engine.runner.start_run:268` | — | **MISSING** — no cancel route and no cancel control. Runs are daemon threads (`engine/runner.py:299`) with no cooperative stop flag; the only way to halt one is to kill the process, which strands the `Run` row at `status="running"` |
| 5 | Bound the blast radius of generated code | — | — | `sandbox/runner.py` | — | **PARTIAL** — subprocess isolation, CPU/memory rlimits, a wall-clock timeout, an import allowlist installed before the module body runs, an empty environment and a throwaway cwd. A network namespace with no interfaces where unprivileged `unshare` works, falling back to patched socket factories elsewhere; `sandbox_tier()` reports which applies. Not a chroot and not a VM boundary — see the hardened-tier row in [`STATUS.md`](STATUS.md) |
| 6 | Wait for in-flight work before shutting down | — | — | `engine.runner.wait_for_runs:319` | — | **PARTIAL** — the helper exists and is honest about why (`engine/runner.py:305-311`), but nothing in `cli.serve` or the app's lifespan calls it. A `docker compose down` kills mid-run threads |
| 7 | Prune old runs and reports | — | `DELETE /pipelines/{pid}` | `server/app.py:792` | — | **PARTIAL** — a pipeline with run history refuses to delete with a 409 telling the operator to "delete the runs first" (`server/app.py:800-805`), and no route or CLI command to delete a run exists. The instruction cannot be followed. There is no retention policy and no archival |

---

## Ranked gaps

Ordered by what most blocks a credible market-ready claim. Closed items are struck from
the list as their journey steps flip to BUILT; the roadmap phase that closed each one is
named so this table stays auditable.

| # | Gap | Journey | Phase | State |
|---|---|---|---|---|
| 1 | No codegen — the stated differentiator does not exist | J6.5 | P4 | **Open** |
| 2 | No token or cost capture, so a real-model run reports zero spend — and exports `$0.0000` as fact | J10.6, J14.8, J20.2 | P5 | **Open** |
| 3 | Mock adapters are still selectable as ordinary targets | J5.1 | P6 | **Open** |
| 4 | Reviewer authority rests on an unverified `X-Assay-User` header, in enforced mode too | J13.2, J15.6, J18.4 | security | **Open** |
| 5 | `POST /hooks/run` has no identity check at all and loads an arbitrary server path | J16.4-5 | security | **Open** |
| 6 | No account management from the UI — a first enforced deployment cannot seed its own reviewer | J19.3-4 | post-P6 | **Open** |
| 7 | CI runs execute against a throwaway SQLite file and never reach the team's queue | J16.2, J17.1 | post-P6 | **Open** |
| 8 | No CI gate — `assay run` always exits 0, so a merge cannot be blocked on an eval | J16.3 | post-P6 | **Open** |
| 9 | Run-level and suite-level gating are parsed and never enforced; `pass_policy` is read and never written | J12.6, J14.9 | post-P6 | **Open** |
| 10 | The `ready_for_review → pending` back-edge is declared legal and never implemented | J15.5 | post-P6 | **Open** |
| 11 | No run history, trends, regression detection, or failure notification | J12.3-5, J17.4-5 | post-P6 | **Open** |
| 12 | No spec export — the eval-as-code thesis is unreachable from the UI | J12.7 | post-P6 | **Open** |
| 13 | No budget cap, no cost estimate, and no way to cancel a run in flight | J20.3-4 | post-P6 | **Open** |
| 14 | Compose writes exported reports outside the mounted volume — artifacts die with the container | J18.5 | bug | **Open** |
| 15 | The audit trail is complete in the database and rendered nowhere in the UI | J15.8 | UI polish | **Open** |
| 16 | Zero state and no Project entity | J1, J3 | UI polish | **Open** |
| — | Judge rubrics never materialised — broke every run containing a judge check | J10.5 | hotfix | Closed |
| — | "Regenerate" wrote code the sandbox could never load | J8.3 | hotfix | Closed |
| — | Sandbox locked down after module load — the containment claim was false | J10.4 | hotfix | Closed |
| — | The UI never called an LLM | J6.2, J4.4 | P1 | Closed |
| — | No credential journey — nothing told you a key was missing | J2 | P0 | Closed |
| — | A long run gave no progress feedback | J10.7 | P1 | Closed |
| — | Judge quotes were stored but never verified | J11.3 | P2 | Closed |
| — | Rubrics were a fixed single dimension | J6.6 | P2 | Closed |
| — | Empty case inputs, no interface grounding | J6.7, J5.3 | P3 | Closed |

## Keeping this document true

A phase is not done until the journey steps it claims to close have flipped to **BUILT** in the
same pull request. This file is the acceptance checklist for the roadmap in
[`STATUS.md`](STATUS.md), not a snapshot to be written once and left behind.
