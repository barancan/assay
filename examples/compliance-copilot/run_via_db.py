#!/usr/bin/env python3
"""End-to-end DB flow for the compliance-copilot example.

Steps
-----
1. Import assay.yaml into the DB as a PipelineVersion (draft).
2. Activate the version (solo-dev path -- any actor is trusted when the
   User table is empty).
3. Execute a run against the mock target.
4. Submit the report for review.
5. Print the review URL so you can open it in a browser.

Usage
-----
Run with the same Python interpreter that has assay-eval installed:

    cd examples/compliance-copilot
    python3 run_via_db.py          # if assay-eval is in the active env
    python3.11 run_via_db.py       # or specify the version explicitly

You can also find the right interpreter from the assay entry point:
    head -1 $(which assay)         # prints the interpreter path

Set ASSAY_DB_URL to point at a Postgres instance; defaults to SQLite at
~/.assay/assay.db via the normal config resolution.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

# Resolve the spec path relative to this script so the script can be run
# from any working directory.
_HERE = Path(__file__).parent
_SPEC = str(_HERE / "assay.yaml")


def main() -> None:
    try:
        import assay  # noqa: F401
    except ImportError:
        print(
            "Error: assay not found.\n"
            "Run this script with the Python that has assay-eval installed, e.g.:\n"
            f"    python3.11 {__file__}\n"
            "Or install it first:  pip install assay-eval"
        )
        sys.exit(1)

    # Initialise config + DB before importing engine modules so that
    # environment variables are picked up first.
    from assay.store.db import init_db
    init_db()

    from assay.pipeline import import_from_yaml, activate_version
    from assay.engine import execute_run, submit_for_review
    from assay.store import session_scope
    from assay.store.models import Report

    print("→ Importing assay.yaml …")
    # import_from_yaml must run with cwd = spec dir so that relative paths
    # inside the spec (JSON schemas, generated checks) resolve correctly.
    cwd = os.getcwd()
    os.chdir(_HERE)
    try:
        pv = import_from_yaml(_SPEC, project="compliance-copilot", created_by="run_via_db")
    finally:
        os.chdir(cwd)

    print(f"  PipelineVersion #{pv.id} created (status: {pv.status}, "
          f"hash: {pv.content_hash[:12]}…)")

    print("→ Activating version …")
    activate_version(pv.id, actor="solo-dev")
    print("  Activated.")

    print("→ Executing run …")
    os.chdir(_HERE)
    try:
        run_id = execute_run(pipeline_version_id=pv.id, triggered_by="run_via_db")
    finally:
        os.chdir(cwd)

    with session_scope() as s:
        rep = s.query(Report).filter_by(run_id=run_id).one()
        rep_id = rep.id
        summary = dict(rep.summary)

    print(f"  Run #{run_id} complete — {summary}")

    print("→ Submitting for review …")
    submit_for_review(rep_id, actor="run_via_db")
    print(f"  Report #{rep_id} is now ready_for_review.")

    host = os.environ.get("ASSAY_SERVE_URL", "http://localhost:8000")
    print(f"\nOpen the review UI: {host}/reports/{rep_id}")
    print("Run `assay serve` (or `docker compose up`) to start the server.")


if __name__ == "__main__":
    main()
