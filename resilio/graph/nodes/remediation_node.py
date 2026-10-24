"""Remediation node: execute the approved action and verify recovery."""

from __future__ import annotations

import logging
from typing import Any, Literal

from resilio.core.metrics import PROVIDER_ERRORS_TOTAL
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, instrumented
from resilio.graph.state import IncidentState, timeline
from resilio.models import Notification, NotificationKind, RemediationAction, Severity

logger = logging.getLogger(__name__)

SCALE_STEP = 2


def make_remediation_node(deps: WorkflowDeps) -> NodeFn:
    settings = deps.settings

    @instrumented("remediation")
    async def remediation_node(state: IncidentState) -> dict[str, Any]:
        event, hypothesis = state["event"], state.get("hypothesis") or {}
        service = event["service"]
        action = RemediationAction(hypothesis.get("action", "none"))
        provider = deps.remediation
        executed, healthy, detail = False, False, ""
        try:
            if action == RemediationAction.ROLLBACK:
                executed = await provider.execute_rollback(service, str(hypothesis["target_sha"]))
                detail = f"rolled back to {hypothesis['target_sha']}"
            elif action == RemediationAction.RESTART:
                executed = await provider.restart_service(service)
                detail = "rolling restart"
            elif action == RemediationAction.SCALE_UP:
                executed = await provider.scale_service(service, SCALE_STEP)
                detail = f"scaled up by {SCALE_STEP} replicas"
            if executed:
                healthy = await provider.verify_health(
                    service, settings.health_check_timeout_seconds
                )
        except NotImplementedError as exc:
            detail = str(exc)
        except Exception as exc:
            PROVIDER_ERRORS_TOTAL.labels(kind="remediation", provider=provider.name).inc()
            logger.exception("remediation failed for %s", service)
            detail = f"{action.value} failed: {exc}"

        result = {
            "action": action.value,
            "executed": executed,
            "healthy": healthy,
            "detail": detail,
        }
        if healthy:
            await deps.notifier.send(
                Notification(
                    incident_id=state["incident_id"],
                    kind=NotificationKind.RESOLVED,
                    title=f"Resolved: {service}",
                    body=f"{detail}; health checks passing.",
                    severity=Severity(event.get("severity", "high")),
                    service=service,
                    fields={
                        "root cause": hypothesis.get("root_cause", "unknown"),
                        "evaluator attempts": str(state.get("evaluator_attempts", 0)),
                    },
                )
            )
            return {
                "status": "resolved",
                "remediation": result,
                "timeline": timeline("remediation", f"{detail}; service healthy"),
            }

        reason = detail if not executed else f"{detail}, but health did not recover"
        return {
            "remediation": result,
            "escalation_reason": reason,
            "timeline": timeline("remediation", reason),
        }

    return remediation_node


def route_after_remediation(state: IncidentState) -> Literal["resolved", "escalate"]:
    return "resolved" if state.get("status") == "resolved" else "escalate"
