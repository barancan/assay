# Assay: an open-source eval-pipeline builder

Point Assay at a deployed model (HTTP endpoint, MCP, or SDK), hand it your
assessment requirements, and it **builds the eval pipeline for you**: it decides
what to test, routes each test to the right approach (deterministic template,
sandboxed generated function, or LLM judge), runs it, and produces a saved,
reviewable report that a named human must sign off before it is considered
production ready.

## Why it exists

- **Eval-as-code.** The pipeline (`assay.yaml` + `generated/`) lives in your repo, diffable and version-pinned.
- **Three ways to test.** Vetted templates where a mechanical check fits; LLM-generated Python (sandboxed) where it does not; LLM judges for semantic calls.
- **Provider-agnostic.** Targets and judges: Anthropic, OpenAI / OpenAI-compatible, Ollama, generic REST (Postman/OpenAPI import), and custom adapters.
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

## Run it for a team

```bash
docker compose up        # Postgres + Assay server at http://localhost:8000
# or just the server:
pip install 'assay-eval[server]' && assay serve
```

## How the build works

`requirements.md` + target interface: derive test intents, route deterministic
vs. judge, materialise (template | generated function | rubric), generate
cases, emit `assay.yaml` + `generated/` for **your review** before anything
runs in production. See `assay-design.md` for the full design.

## Sandbox honesty

Generated checks are **pure functions of captured data** -- they receive dicts,
never a model client. They run in an isolated subprocess with CPU/memory
rlimits, a wall-clock timeout, an import allowlist (no `os`/`socket`/`subprocess`/...),
and `open` disabled. This contains buggy and naive-malicious checks. For
genuinely untrusted third-party code, enable the hardened tier
(gVisor / Firecracker / WASM) -- see the design doc.

## Adapters

| Kind | Built-in |
|---|---|
| Target | `mock`, `rest` (+Postman/OpenAPI import), `anthropic`, `openai_compat`, `ollama`, `custom` |
| Judge  | `anthropic`, `openai_compat`, `ollama`, `mock` |

## License

Apache-2.0.
