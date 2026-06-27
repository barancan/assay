# compliance-copilot example

A worked example that validates a JSON API against three made-up compliance
requirements (R1 output format, R2 data types, R3 severity ordering).
One case is deliberately crafted to fail R3 so you can exercise the
adjudication + approval flow.

## File-based path (backward-compatible, no server needed)

```bash
cd examples/compliance-copilot
assay run --by alice
cat .assay/reports/run_1/report.md
assay approve 1 --approver alice
```

The pipeline (`assay.yaml` + `generated/`) lives on disk and can be committed
to version control.

## DB pipeline path (recommended for teams)

This path stores the pipeline in the database, unlocks the review UI, and
supports version history + activation gates.

### Prerequisites

```bash
pip install 'assay-eval[server]'
# Optional: export ASSAY_DB_URL=postgresql+psycopg://user:pass@host/db
```

### 1. Run the full flow in one command

```bash
cd examples/compliance-copilot
python3 run_via_db.py
```

This imports `assay.yaml`, activates the version, executes a run against the
mock target, and submits the report for review. It prints the URL to open in a
browser.

### 2. Open the review UI

```bash
assay serve          # starts at http://localhost:8000
```

Then open the URL printed by `run_via_db.py` — e.g.
`http://localhost:8000/reports/1`.

From the UI you can:
- Override individual case verdicts (adjudicate)
- Assign a reviewer
- Approve the report to lock it at `done`
- Download the report as JSON / Markdown / HTML

### Step-by-step (manual)

```bash
# Import & activate
assay pipeline import --spec assay.yaml --project compliance-copilot
assay pipeline activate 1 --by solo-dev

# Run
assay run --pipeline-version 1

# Review
assay serve &
open http://localhost:8000
```
