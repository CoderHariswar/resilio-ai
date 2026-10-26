"""Policy node: risk-tiered approval engine.

Risk starts from the action's blast radius and is adjusted by context:

=========================  =====================
signal                     effect
=========================  =====================
restart / scale_up         base LOW
rollback                   base MEDIUM
service in CRITICAL_SERVICES  +1 tier
severity == critical       +1 tier
confidence < MIN_AUTO_CONFIDENCE  +1 tier (rule-based, see confidence_node)
judge unavailable          +1 tier
non-production environment -1 tier (floor LOW)
=========================  =====================

Risk <= ``AUTO_APPROVE_MAX_RISK`` executes automatically; anything higher
pauses at the human approval gate. ``action == none`` always escalates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from resilio.config import RiskLevel, Settings
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, instrumented
from resilio.graph.state import IncidentState, timeline
from resilio.models import Notification, NotificationKind, RemediationAction, Severity

Decision = Literal["auto", "approval", "escalate"]

_BASE_RISK = {
    RemediationAction.RESTART: RiskLevel.LOW,
    RemediationAction.SCALE_UP: RiskLevel.LOW,
    RemediationAction.ROLLBACK: RiskLevel.MEDIUM,
}
_TIERS = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]


def assess_risk(
    settings: Settings,
    event: Mapping[str, Any],
    hypothesis: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    confidence: float,
) -> dict[str, Any]:
    """Return ``{"risk", "decision", "reasons"}`` for an accepted hypothesis.

    ``confidence`` is the rule-based score, never the model's self-report.
    """
    action = RemediationAction(hypothesis.get("action", "none"))
    if action == RemediationAction.NONE:
        return {
            "risk": RiskLevel.NONE.value,
            "decision": "escalate",
            "reasons": ["no safe automated remediation for this root cause"],
        }

    tier = _TIERS.index(_BASE_RISK[action])
    reasons = [f"{action.value} base risk {_BASE_RISK[action].value}"]
    if event.get("service") in settings.critical_services:
        tier += 1
        reasons.append("service is marked critical")
    if event.get("severity") == Severity.CRITICAL.value:
        tier += 1
        reasons.append("alert severity is critical")
    if confidence < settings.min_auto_confidence:
        tier += 1
        reasons.append(f"confidence {confidence:.2f} below {settings.min_auto_confidence}")
    if not evaluation.get("judge_available", True):
        tier += 1
        reasons.append("LLM judge unavailable")
    if event.get("environment", "production") != "production":
        tier -= 1
        reasons.append("non-production environment")

    risk = _TIERS[min(max(tier, 0), len(_TIERS) - 1)]
    auto_ok = (
        settings.auto_approve_max_risk != RiskLevel.NONE
        and risk.rank <= settings.auto_approve_max_risk.rank
    )
    decision: Decision = "auto" if auto_ok else "approval"
    reasons.append(
        f"risk {risk.value} {'<=' if auto_ok else '>'} auto-approve threshold "
        f"{settings.auto_approve_max_risk.value}"
    )
    return {"risk": risk.value, "decision": decision, "reasons": reasons}


def confidence_line(state: IncidentState) -> str:
    """``0.82 (deploy timing +0.30, judge +0.25, ...)`` for human-facing messages."""
    breakdown = state.get("confidence_breakdown") or {}
    return str(breakdown.get("summary") or f"{float(state.get('confidence') or 0.0):.2f}")


def make_policy_node(deps: WorkflowDeps) -> NodeFn:
    @instrumented("policy")
    async def policy_node(state: IncidentState) -> dict[str, Any]:
        hypothesis = state.get("hypothesis") or {}
        # Missing confidence (should not happen after confidence_node) counts as 0.
        policy = assess_risk(
            deps.settings,
            state["event"],
            hypothesis,
            state.get("evaluation") or {},
            confidence=float(state.get("confidence") or 0.0),
        )
        update: dict[str, Any] = {
            "policy": policy,
            "timeline": timeline("policy", f"risk={policy['risk']} decision={policy['decision']}"),
        }
        if policy["decision"] == "escalate":
            update["escalation_reason"] = policy["reasons"][0]
        elif policy["decision"] == "auto":
            event = state["event"]
            await deps.notifier.send(
                Notification(
                    incident_id=state["incident_id"],
                    kind=NotificationKind.INFO,
                    title=f"Auto-remediating {event['service']}: {hypothesis['action']}",
                    body=hypothesis.get("summary", ""),
                    severity=Severity(event.get("severity", "high")),
                    service=event["service"],
                    fields={
                        "root cause": hypothesis["root_cause"],
                        "risk": policy["risk"],
                        "confidence": confidence_line(state),
                    },
                )
            )
        return update

    return policy_node


def route_after_policy(
    state: IncidentState,
) -> Literal["remediation", "request_approval", "escalate"]:
    decision = (state.get("policy") or {}).get("decision")
    if decision == "auto":
        return "remediation"
    if decision == "approval":
        return "request_approval"
    return "escalate"
