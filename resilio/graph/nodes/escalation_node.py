"""Escalation node: hand the incident to humans with everything learned so far."""

from __future__ import annotations

from typing import Any

from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, instrumented
from resilio.graph.state import IncidentState, timeline
from resilio.models import Notification, NotificationKind, Severity


def make_escalation_node(deps: WorkflowDeps) -> NodeFn:
    @instrumented("escalate")
    async def escalation_node(state: IncidentState) -> dict[str, Any]:
        event, hypothesis = state["event"], state.get("hypothesis") or {}
        reason = state.get("escalation_reason") or (
            "evaluator could not validate a hypothesis: "
            + str((state.get("evaluation") or {}).get("feedback", "unknown"))
        )
        await deps.notifier.send(
            Notification(
                incident_id=state["incident_id"],
                kind=NotificationKind.ESCALATED,
                title=f"Escalated: {event['title']}",
                body=reason,
                severity=Severity(event.get("severity", "high")),
                service=event["service"],
                fields={
                    "best hypothesis": hypothesis.get("root_cause", "unknown"),
                    "summary": hypothesis.get("summary", "")[:200],
                    "evaluator attempts": str(state.get("evaluator_attempts", 0)),
                },
            )
        )
        return {
            "status": "escalated",
            "escalation_reason": reason,
            "timeline": timeline("escalate", reason),
        }

    return escalation_node
