"""Linear outbound notifier.

Creates a Linear issue when a report reaches ready_for_review.
Posts a comment on that issue when the report is approved or rejected.

Required env vars:  ASSAY_LINEAR_API_KEY, ASSAY_LINEAR_TEAM_ID
Optional env var:   ASSAY_LINEAR_PROJECT_ID
"""
from __future__ import annotations
import os
import requests

_GQL_URL = "https://api.linear.app/graphql"

_CREATE_ISSUE = """
mutation CreateIssue($title: String!, $description: String!, $teamId: String!, $projectId: String) {
  issueCreate(input: {title: $title, description: $description,
                      teamId: $teamId, projectId: $projectId}) {
    success
    issue { id }
  }
}
"""

_CREATE_COMMENT = """
mutation CreateComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
  }
}
"""


class LinearNotifier:
    def __init__(self) -> None:
        self.api_key = os.environ["ASSAY_LINEAR_API_KEY"]
        self.team_id = os.environ["ASSAY_LINEAR_TEAM_ID"]
        self.project_id = os.environ.get("ASSAY_LINEAR_PROJECT_ID")

    def _gql(self, query: str, variables: dict) -> dict:
        resp = requests.post(
            _GQL_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def notify(self, event: str, payload: dict) -> None:
        if event == "ready_for_review":
            self._on_ready(payload)
        elif event in ("approved", "rejected"):
            self._on_conclusion(event, payload)

    def _on_ready(self, payload: dict) -> None:
        from ..store import session_scope
        from ..store.models import NotificationRecord

        project = payload.get("project", "unknown")
        report_id = payload["report_id"]
        summary = payload.get("summary", {})
        pv = payload.get("pipeline_version")
        target = payload.get("target", {})
        assigned = payload.get("assigned_reviewer")

        lines = [
            f"**Project**: {project}",
            f"**Cases**: {summary.get('cases', 0)} | "
            f"**Passed**: {summary.get('passed', 0)} | "
            f"**Failed**: {summary.get('failed', 0)}",
        ]
        if pv:
            lines.append(
                f"**Pipeline version**: #{pv.get('id')} "
                f"hash `{str(pv.get('content_hash', ''))[:12]}…`"
            )
        if target:
            lines.append(f"**Target**: {target.get('adapter')} / {target.get('model') or 'n/a'}")
            if target.get("endpoint"):
                lines.append(f"**Endpoint**: {target['endpoint']}")
        if assigned:
            lines.append(f"**Assigned reviewer**: {assigned}")

        data = self._gql(_CREATE_ISSUE, {
            "title": f"[Assay] {project} report #{report_id} ready for review",
            "description": "\n".join(lines),
            "teamId": self.team_id,
            "projectId": self.project_id,
        })
        issue_id = data["data"]["issueCreate"]["issue"]["id"]

        with session_scope() as s:
            s.add(NotificationRecord(report_id=report_id, channel="linear",
                                     external_id=issue_id))

    def _on_conclusion(self, event: str, payload: dict) -> None:
        from ..store import session_scope
        from ..store.models import NotificationRecord

        report_id = payload.get("report_id")
        with session_scope() as s:
            rec = (
                s.query(NotificationRecord)
                .filter_by(report_id=report_id, channel="linear")
                .order_by(NotificationRecord.at.desc())
                .first()
            )
            if rec is None:
                return
            issue_id = rec.external_id

        lines = [f"**Status**: {event.upper()}"]
        if event == "approved":
            lines.append(f"**Approved by**: {payload.get('approved_by')}")
        summary = payload.get("summary", {})
        lines.append(
            f"**Final summary**: {summary.get('cases', 0)} cases, "
            f"{summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed"
        )

        self._gql(_CREATE_COMMENT, {"issueId": issue_id, "body": "\n".join(lines)})
