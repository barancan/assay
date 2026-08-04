# Assay: an open-source eval-pipeline builder

Point Assay at a deployed model (HTTP endpoint or SDK), hand it your assessment
requirements, and it **builds the eval pipeline for you**: it decides what to
test, routes each test to the right approach (deterministic template, sandboxed
generated function, or LLM judge), runs it, and produces a saved, reviewable
report that a named human must sign off before it is considered production ready.

> **Status: pre-1.0, and the builder half is not finished.** The runner — execute,
> review, adjudicate, approve, audit — works end to end. The builder derives test
> intents with a real model on every path (`assay generate --offline` is the only
> way to get the old keyword heuristic), but **LLM codegen is not implemented yet**. Read
> [`docs/STATUS.md`](docs/STATUS.md) before you rely on anything below; every
> capability is marked built, partial, or planned.

## Why it exists

- **Eval-as-code.** The pipeline (`assay.yaml` + `generated/`) lives in your repo, diffable and version-pinned.
- **Three ways to test.** Vetted templates where a mechanical check fits; LLM-generated Python (sandboxed) where it does not; LLM judges for semantic calls. *Templates and judges work today; generated-function codegen is [planned](docs/STATUS.md#builder-requirements--pipeline).*
- **Provider-agnostic.** Targets and judges: Anthropic, OpenAI / OpenAI-compatible, Ollama, and generic REST with Postman import.
- **Auditable and gated.** Every run records the tested model, test cases, full responses, and the approver. Reports move `pending → ready_for_review → done`; automation can trigger runs but only a reviewer can promote to `done`.

## Install

```bash
pipx install assay-eval          # or: pip install -e .
# zero-install:  uvx --from assay-eval assay --help
```

## DB pipeline quickstart (recommended)

Store the pipeline in the database to get version history, activation gates, and
a full review UI.

```bash
pip install 'assay-eval[server]'

# Import a spec from YAML into the DB and activate it
assay pipeline import --spec assay.yaml --project my-project
assay pipeline activate 1 --by you

# Run against the active version
assay run --pipeline-version 1

# Start the review UI
assay serve            # http://localhost:8000
```

Open `http://localhost:8000` to see the review queue. From there you can
assign reviewers, override individual case verdicts, and approve reports to
lock them at `done`.

Set `ASSAY_DB_URL=postgresql+psycopg://...` to switch from SQLite to Postgres
with no code change.

## File-based quickstart (backward-compatible)

The original file-based path still works. `assay.yaml` and `generated/` live
in your repo, diffable and version-pinned:

```bash
assay init my-evals && cd my-evals          # scaffold + requirements.md stub
assay generate --requirements requirements.md --adapter mock   # build the pipeline
#   add --judge anthropic:claude-opus-4-8 for LLM-assisted generation
assay run                                    # execute -> report (ready_for_review)
assay users --add you --role reviewer        # create a reviewer identity
assay report                                 # list reports + states
assay approve 1 --approver you               # promote to done (records approver)
```

Reports are written to `.assay/reports/run_<id>/` as JSON, Markdown, and HTML.

## Try the worked example (offline, no API keys)

```bash
cd examples/compliance-copilot
python3 run_via_db.py          # import, activate, run, submit for review
assay serve                   # open http://localhost:8000/reports/1
```

Or the classic file-based path:

```bash
cd examples/compliance-copilot
assay run --by alice
cat .assay/reports/run_1/report.md
```

Four cases run against a mock target; one deliberately fails via a sandboxed
generated check so you can exercise the adjudication and approval flow. See
[`examples/compliance-copilot/README.md`](examples/compliance-copilot/README.md)
for the full walkthrough.

## Run it for a team (enforced auth)

By default Assay runs in open mode -- frictionless for a single developer.
For a shared deployment, switch to enforced mode before exposing the port:

```bash
# 1. Generate a signing key
export ASSAY_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# 2. Seed at least one reviewer (required before any approval in enforced mode)
assay users --add alice --role reviewer

# 3. Start with enforced auth
ASSAY_AUTH=enforced assay serve --host 0.0.0.0
# or via Docker Compose (ASSAY_SECRET_KEY must be set in your shell first):
docker compose up
```

In enforced mode the server refuses to start with the built-in dev secret, all
privileged actions require a valid session or `X-Assay-User` header, and at
least one reviewer account must exist before any approval goes through.

## How the build works

`requirements.md` + target interface: derive test intents, route deterministic
vs. judge, materialise (template | generated function | rubric), generate
cases, emit `assay.yaml` + `generated/` for **your review** before anything
runs in production. See `assay-design.md` for the full design.

What that looks like in the current build:

| Stage | Today |
|---|---|
| Derive intents | Real LLM on every path — the web UI and `assay generate` both resolve the configured build model, and fail with the missing variable's name rather than degrading. `assay generate --offline` opts back into the keyword heuristic |
| Requirement traceability | Requirements are split into `R1…Rn` (`generator/ingest.py`); every intent must cite one, and each becomes its own suite so the coverage matrix has real buckets |
| Route deterministic vs. judge | Decided by the same call; no rationale is recorded |
| Template checks | Working — 11 primitives |
| Generated functions | **Not implemented.** Routing an intent to `generated` produces a spec entry with no source behind it |
| Judge rubrics | A fixed single-dimension rubric, not the anchored multi-dimension rubric the design describes |
| Test cases | Emitted with empty inputs; the target interface is not parsed at build time |

Closing this gap is the current priority — see the roadmap in
[`docs/STATUS.md`](docs/STATUS.md).

## Sandbox honesty

Generated checks are **pure functions of captured data** -- they receive dicts,
never a model client. They run in an isolated subprocess with CPU/memory
rlimits, a wall-clock timeout, an import allowlist (no `os`/`socket`/`subprocess`/...),
and `open`/`exec`/`eval`/`compile` removed. The lockdown is installed before the
module body executes, so it covers a check's top-level statements as well as the
body of `check()`.

What this does **not** do, despite what earlier versions of this file claimed:
the subprocess inherits the engine's working directory (there is no filesystem
jail) and there is no OS-level egress block — only the `socket` factories are
patched, as defence in depth. This contains buggy and naive-malicious checks; it
is not a boundary for genuinely untrusted third-party code. A hardened tier
(gVisor / Firecracker / WASM) is designed but **not implemented**.

## Adapters

| Kind | Built-in |
|---|---|
| Target | `mock` (tests only), `rest` (Postman import), `anthropic`, `openai_compat`, `ollama` |
| Judge  | `anthropic`, `openai_compat`, `ollama`, `mock` (tests only) |

Planned, not yet implemented: `mcp` and `custom` target adapters, and OpenAPI
import for `rest` (only Postman collections parse today).

## License

Apache-2.0.
