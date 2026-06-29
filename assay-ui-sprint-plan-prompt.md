# Assay UI Sprint Plan

> **Living document.** Updated to reflect design decisions made during implementation.
> Phases 0–3 are complete. This document governs Phases 4–6.

---

## Locked design decisions (all phases must respect these)

| Decision | Detail |
|---|---|
| **Default theme** | Dark. `data-theme="dark"` on `<html>` before stylesheets paint (FOUC prevention). User can toggle to light; preference saved in `localStorage('assay-theme')`. Toggle button uses `ti-sun` (dark→light) / `ti-moon` (light→dark). |
| **Navigation** | Projects · Runs · Reports · Settings — **no Pipelines tab**. |
| **Project-centric IA** | Drafts, active pipelines, baseline, and reports all live inside `/projects/{name}`. The standalone `/pipelines` HTML route redirects to `/projects`. |
| **Clickable rows** | Every list row (`<a class="row-link">`) and project card (`<a class="project-card-link">`) is a full-width anchor. Keyboard-navigable (Tab + Enter). Hover: `background: var(--surface-1)`. |
| **Model selector** | All model fields use the `model_selector(model_var, provider_var)` Jinja2 macro from `_macros.html`. Renders a `<select>` of known models + `Custom…` option that reveals a text input. Seed lists: Anthropic → `claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5`; OpenAI → `gpt-4o, gpt-4o-mini, o3`; Ollama → `llama3, mistral, qwen2.5`; REST/other → Custom… only. |
| **No hardcoded hex** | All color values are CSS custom properties from `tokens.css`. `style.css` and `components.css` may not contain hex literals. |
| **Tabler outline icons** | `ti-*` prefix, no `-filled` variants, `aria-hidden="true"` on decorative icons. |
| **Stack** | FastAPI + Jinja2 + htmx + Alpine.js. No Node/React/build step. |

---

## Project detail page (`/projects/{name}`) — canonical structure

Implemented in Phase 0–3 corrections. Every project-scoped view anchors here.

1. **Header** — project name, aggregated stats (`dl.meta`), "New pipeline" button scoped to `?project={name}`.
2. **Approved baseline card** — most recent `Report` with `state=done, verdict=pass`. Shows report id, pipeline version, approver, date. Empty state when none.
3. **Pipelines section** — each `Pipeline` row: name, active version badge, last verdict badge, report count, last run date. Draft versions appear inline with a step badge (Define/Connect/Review) and a Resume link to `/pipelines/new?resume={id}`.
4. **Reports section** — clickable `.row-link` rows ordered by `created_at desc`, max 50.

---

## Phase 4 — Review and activate

**Objective**: Pipeline review screen (`/pipelines/{pid}/versions/{vid}/review`): generated-check list with collapsible source/rubric, per-check regenerate, requirement-coverage meter, activation gate card.

### Files to add
- `assay/server/templates/pipeline_review.html` — check rows (template/generated/judge badges), coverage line, activation gate

### Files to modify
- `assay/pipeline/service.py` — `regenerate_check(version_id, check_id, actor)`
- `assay/server/app.py`:
  - `GET /pipelines/{pid}/versions/{vid}/review` → `pipeline_review.html`
  - `POST /pipelines/versions/{vid}/checks/{cid}/regenerate`
  - `PATCH /pipelines/versions/{vid}/checks/{cid}` (inline edit, draft only)
  - `POST /pipelines/versions/{vid}/activate` — HTMX path returns `HX-Redirect` to project detail

### Post-activate redirect
After activation, redirect to `/projects/{project_name}` (not `/pipelines`).

### Tests
- `test_pipeline_review_page` — 200 with check rows and activation gate
- `test_activate_requires_reviewer` — enforced mode, runner → 403
- `test_activate_promotes_version` — version.status == "active"
- `test_activate_archives_previous` — old active → archived
- `test_activate_redirects_to_project_detail` — HTMX response has `HX-Redirect` pointing to `/projects/…`
- `test_regenerate_check_creates_draft` — new draft PipelineVersion
- `test_inline_edit_updates_source` — PATCH on draft updates `generated_sources`
- `test_inline_edit_rejects_active` — PATCH on active → 409

---

## Phase 5 — Runs / trends

**Objective**: Per-pipeline run history at `/pipelines/{pid}/runs`, pass-rate trend chart (Chart.js CDN), regression flag, trigger-run action.

### Files to add
- `assay/server/templates/runs.html` — run table, pass-rate trend, regression banner, trigger-run button
- `assay/server/static/chart-init.js` — Chart.js init (loaded only on runs page)

### Nav note
The "Runs" nav link (`/runs`) lists all recent runs across projects. The per-pipeline run history is at `/pipelines/{pid}/runs`, reachable from Project detail.

### Files to modify
- `assay/server/app.py`:
  - `GET /pipelines/{pid}/runs` → `runs.html`
  - `POST /pipelines/{pid}/run` — triggers execute_run + submit_for_review
  - `GET /runs` → recent runs across all projects (simple list, reuses `.row-link`)

### Tests
- `test_runs_page` — 200 with run rows
- `test_trigger_run_creates_report` — new Run + Report in DB
- `test_regression_flag_set` — 5/5 then 3/5 pass → regression alert in HTML
- `test_regression_flag_absent` — both 5/5 → no regression
- `test_pass_rate_history` — context includes `pass_rates` list

---

## Phase 6 — Settings + polish

**Objective**: Full Settings page (accounts, judge defaults, Linear integration, auth posture display), empty states on all list pages, dark-mode final audit.

### Files to add / modify
- `assay/server/templates/settings.html` — expand existing stub:
  - Accounts: user table + "Add reviewer" form (POST `/settings/users`) + delete button
  - Default judge: already done (Phase 3 corrections). The `model_selector` macro is already wired.
  - Linear integration: `ASSAY_LINEAR_TOKEN` env-var status badge, team URL field (WorkspaceSetting), test-connection button
  - Auth posture: read-only display of `ASSAY_AUTH`
- `assay/server/templates/_empty_state.html` — icon + heading + subtext + optional CTA; used by all list pages
- `assay/server/app.py`:
  - `POST /settings/users` — create User; HTMX returns updated row
  - `DELETE /settings/users/{name}` — delete user; HTMX removes row
  - `POST /settings/judge` — **already implemented in corrections**
  - `POST /settings/linear` — update `WorkspaceSetting(key="linear_team_url", ...)`

### Empty-state pages
Add `{% include "_empty_state.html" %}` branches to: `reports.html`, `projects.html`, `project_detail.html` (reports/pipelines sections), `runs.html`.

### Tests
- `test_settings_page` — 200 with accounts section
- `test_add_reviewer_via_settings` — POST `/settings/users` → User in DB
- `test_delete_user_via_settings` — DELETE → user removed
- `test_judge_default_persists` — **already covered by `test_corrections.py::test_settings_judge_roundtrip`**
- `test_auth_posture_shown` — `ASSAY_AUTH=enforced` → "enforced" in HTML
- `test_empty_state_reports` — GET `/reports` with no data → empty-state markup
- `test_empty_state_projects` — GET `/projects` with no data → empty-state markup
