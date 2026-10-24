"""Human-in-the-loop: request approval, then pause at a LangGraph breakpoint.

Split into two nodes on purpose. ``interrupt()`` re-executes its node from the
top when the graph resumes, so the side effect (sending the approval request)
lives in ``request_approval`` and the pause lives in ``approval_gate``.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.types import interrupt

from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, instrumented
from resilio.graph.state import IncidentState, timeline
from resilio.models import ApprovalDecision, Notification, NotificationKind, Severity


def make_request_approval_node(deps: WorkflowDeps) -> NodeFn:
    base_url = deps.settings.public_url.rstrip("/")

    @instrumented("request_approval")
    async def request_approval_node(state: IncidentState) -> dict[str, Any]:
        event, hypothesis = state["event"], state.get("hypothesis") or {}
        policy = state.get("policy") or {}
        incident_id = state["incident_id"]
        fields = {
            "root cause": hypothesis.get("root_cause", "unknown"),
            "action": hypothesis.get("action", "none"),
            "confidence": f"{float(hypothesis.get('confidence', 0)):.2f}",
            "risk": policy.get("risk", "unknown"),
            "why approval": "; ".join(policy.get("reasons", [])),
            "approve via CLI": f"resilio approve {incident_id}",
        }
        if hypothesis.get("target_sha"):
            fields["target sha"] = str(hypothesis["target_sha"])
        await deps.notifier.send(
            Notification(
                incident_id=incident_id,
                kind=NotificationKind.APPROVAL_REQUEST,
                title=f"Approval needed: {hypothesis.get('action')} {event['service']}",
                body=hypothesis.get("summary", ""),
                severity=Severity(event.get("severity", "high")),
                service=event["service"],
                fields=fields,
                links={"Review": f"{base_url}/ui/incidents/{incident_id}"},
            )
        )
        return {
            "status": "awaiting_approval",
            "timeline": timeline("request_approval", "approval requested from on-call"),
        }

    return request_approval_node


def make_approval_gate_node(deps: WorkflowDeps) -> NodeFn:
    @instrumented("approval_gate")
    async def approval_gate_node(state: IncidentState) -> dict[str, Any]:
        hypothesis = state.get("hypothesis") or {}
        raw = interrupt(
            {
                "incident_id": state["incident_id"],
                "action": hypothesis.get("action"),
                "target_sha": hypothesis.get("target_sha"),
                "risk": (state.get("policy") or {}).get("risk"),
                "question": f"Approve {hypothesis.get('action')} for {state['event']['service']}?",
            }
        )
        decision = ApprovalDecision.model_validate(raw)
        verdict = "approved" if decision.approved else "rejected"
        update: dict[str, Any] = {
            "status": "investigating",
            "approval": decision.model_dump(),
            "timeline": timeline(
                "approval_gate",
                f"{verdict} by {decision.approver}"
                + (f": {decision.comment}" if decision.comment else ""),
            ),
        }
        if not decision.approved:
            update["escalation_reason"] = f"remediation rejected by {decision.approver}"
        return update

    return approval_gate_node


def route_after_approval(state: IncidentState) -> Literal["remediation", "escalate"]:
    return "remediation" if (state.get("approval") or {}).get("approved") else "escalate"
