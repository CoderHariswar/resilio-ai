"""End-to-end incident execution through the compiled LangGraph workflow."""

from __future__ import annotations

import pytest

from resilio.container import Container
from resilio.models import ApprovalDecision, IncidentStatus, NotificationKind
from resilio.providers.llm.mock import MockLLMProvider
from resilio.providers.notifications.mock import MockNotifier
from resilio.providers.remediation.mock import MockRemediationProvider
from resilio.worker import InvalidIncidentStateError
from tests.helpers import make_event, metric

pytestmark = pytest.mark.integration


async def _status(container: Container, incident_id: str) -> IncidentStatus:
    record = await container.deps.repository.get(incident_id)
    assert record is not None
    return record.status


async def test_low_risk_incident_auto_resolves(
    container: Container, notifier: MockNotifier, remediation: MockRemediationProvider
) -> None:
    before = metric("resilio_incidents_total", {"status": "resolved"})
    event = make_event("disk-full", service="api")
    state = await container.runner.run(event)

    assert state["status"] == "resolved"
    assert await _status(container, event.id) == IncidentStatus.RESOLVED
    assert remediation.actions == [("api", "restart", "rolling restart")]
    assert notifier.kinds() == [NotificationKind.INFO, NotificationKind.RESOLVED]
    assert metric("resilio_incidents_total", {"status": "resolved"}) == before + 1
    assert metric("resilio_resolution_duration_seconds_count", {"status": "resolved"}) >= 1
    nodes = [entry["node"] for entry in state["timeline"]]
    assert nodes == [
        "telemetry",
        "vcs",
        "synthesizer",
        "evaluator",
        "confidence",
        "policy",
        "remediation",
    ]
    assert state["confidence"] >= container.settings.min_auto_confidence


async def test_rollback_pauses_for_human_then_resolves(
    container: Container, notifier: MockNotifier, remediation: MockRemediationProvider
) -> None:
    event = make_event("memory-leak")
    pending_before = metric("resilio_hitl_pending")
    await container.runner.run(event)

    record = await container.deps.repository.get(event.id)
    assert record is not None
    assert record.status == IncidentStatus.AWAITING_APPROVAL
    assert record.summary["policy"]["decision"] == "approval"
    assert record.summary["pending"][0]["action"] == "rollback"
    assert metric("resilio_hitl_pending") == pending_before + 1
    assert remediation.actions == [], "nothing executed before approval"
    assert notifier.kinds() == [NotificationKind.APPROVAL_REQUEST]

    target = record.summary["hypothesis"]["target_sha"]
    state = await container.runner.resume(event.id, ApprovalDecision(approved=True, approver="sre"))
    assert state["status"] == "resolved"
    assert state["approval"]["approver"] == "sre"
    assert remediation.actions == [("checkout", "rollback", target)]
    assert metric("resilio_hitl_pending") == pending_before
    assert notifier.kinds()[-1] == NotificationKind.RESOLVED

    with pytest.raises(InvalidIncidentStateError):
        await container.runner.resume(event.id, ApprovalDecision(approved=True))


async def test_rejection_escalates_without_touching_production(
    container: Container, remediation: MockRemediationProvider, notifier: MockNotifier
) -> None:
    event = make_event("bad-deploy")
    await container.runner.run(event)
    state = await container.runner.resume(
        event.id, ApprovalDecision(approved=False, approver="lead", comment="freeze")
    )
    assert state["status"] == "escalated"
    assert "rejected by lead" in state["escalation_reason"]
    assert remediation.actions == []
    assert notifier.kinds()[-1] == NotificationKind.ESCALATED


async def test_third_party_outage_escalates_with_rca(container: Container) -> None:
    before = metric("resilio_incidents_total", {"status": "escalated"})
    event = make_event("cert-expiry")
    state = await container.runner.run(event)
    assert state["status"] == "escalated"
    assert state["hypothesis"]["root_cause"] == "certificate_expiry"
    assert state["policy"]["decision"] == "escalate"
    assert metric("resilio_incidents_total", {"status": "escalated"}) == before + 1


async def test_evaluator_corrects_hallucinated_rollback(
    container: Container, llm: MockLLMProvider
) -> None:
    llm.fault_rate = 1.0
    before = metric("resilio_evaluator_corrections_total")
    event = make_event("config-drift", service="billing")
    state = await container.runner.run(event)

    assert state["evaluator_attempts"] == 2
    assert metric("resilio_evaluator_corrections_total") == before + 1
    evaluator_msgs = [e["message"] for e in state["timeline"] if e["node"] == "evaluator"]
    assert "not found in deployment history" in evaluator_msgs[0]
    assert evaluator_msgs[1].startswith("accepted")
    assert state["hypothesis"]["target_sha"] == state["rollback_target"]


async def test_wrong_remediation_escalates_after_failed_health_check(
    container: Container,
) -> None:
    # Restarting a memory leak does not heal it in the mock world.
    container.settings.auto_approve_max_risk = container.settings.auto_approve_max_risk.HIGH
    event = make_event("memory-leak", service="ledger")

    async def restart_instead(service: str, target_sha: str) -> bool:
        return await container.deps.remediation.restart_service(service)

    container.deps.remediation.execute_rollback = restart_instead  # type: ignore[method-assign]
    state = await container.runner.run(event)
    assert state["status"] == "escalated"
    assert "health did not recover" in state["escalation_reason"]


async def test_resolved_incidents_feed_similarity_memory(container: Container) -> None:
    first = make_event("traffic-spike", service="search")
    await container.runner.run(first)
    second = make_event("traffic-spike", service="search", title="Latency SLO burn again")
    state = await container.runner.run(second)
    assert state["similar_incidents"][0]["incident_id"] == first.id
    assert state["similar_incidents"][0]["root_cause"] == "capacity_saturation"


async def test_graph_crash_marks_incident_failed(container: Container) -> None:
    before = metric("resilio_incidents_total", {"status": "failed"})

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("checkpoint store unavailable")

    container.runner.graph.ainvoke = explode
    event = make_event("disk-full", service="fragile")
    state = await container.runner.run(event)
    assert state["status"] == "failed"
    record = await container.deps.repository.get(event.id)
    assert record is not None and "checkpoint store unavailable" in record.summary["error"]
    assert metric("resilio_incidents_total", {"status": "failed"}) == before + 1
