"""Rule-based confidence inside the compiled workflow, and its safety invariants."""

from __future__ import annotations

import json
from typing import Any

import pytest

from resilio.container import Container
from resilio.models import IncidentStatus
from resilio.providers.base import BaseLLMProvider, LLMRequest, LLMResponse
from resilio.providers.remediation.mock import MockRemediationProvider
from resilio.scoring.confidence import BaseConfidenceScorer, ConfidenceResult, ConfidenceSignals
from tests.helpers import make_event

pytestmark = pytest.mark.integration


class ConstantScorer(BaseConfidenceScorer):
    """Always maximally confident: proves confidence alone cannot force an action."""

    name = "constant"

    def score(self, signals: ConfidenceSignals) -> ConfidenceResult:
        return ConfidenceResult(value=1.0, breakdown=())


class ScriptedLLM(BaseLLMProvider):
    """Synthesizer answers come from ``answer(context)``; the judge always accepts."""

    name = "scripted"

    def __init__(self, answer: Any, judge_score: float = 0.95) -> None:
        self.answer = answer
        self.judge_score = judge_score

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.task == "synthesize":
            body: dict[str, Any] = self.answer(request.context)
        else:
            body = {"score": self.judge_score, "verdict": "accept", "issues": []}
        return LLMResponse(text=json.dumps(body), model="scripted")


def _nodes(state: dict[str, Any]) -> list[str]:
    return [entry["node"] for entry in state["timeline"]]


async def test_llm_claims_099_but_rule_based_confidence_raises_risk(
    container: Container, remediation: MockRemediationProvider
) -> None:
    def overconfident(context: dict[str, Any]) -> dict[str, Any]:
        smallest = min(context["trace_groups"], key=lambda g: g["count"])
        return {
            "root_cause": "disk_exhaustion",
            "summary": "disk full",
            "confidence": 0.99,
            "evidence": [smallest["fingerprint"]],
            "action": "restart",
        }

    container.deps.llm = ScriptedLLM(overconfident, judge_score=0.75)
    event = make_event("disk-full", service="api")
    state = await container.runner.run(event)

    record = await container.deps.repository.get(event.id)
    assert record is not None
    assert record.status == IncidentStatus.AWAITING_APPROVAL
    assert state["llm_confidence"] == 0.99
    assert state["confidence"] < container.settings.min_auto_confidence
    policy = record.summary["policy"]
    assert (policy["risk"], policy["decision"]) == ("medium", "approval")
    assert any(r.startswith("confidence ") and "below 0.8" in r for r in policy["reasons"])
    assert record.summary["confidence_breakdown"]["breakdown"]
    assert remediation.actions == []


async def test_grounding_failure_rejects_regardless_of_confidence(
    container: Container, remediation: MockRemediationProvider
) -> None:
    def fabricated(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "root_cause": "memory_leak",
            "confidence": 1.0,
            "evidence": [context["trace_groups"][0]["fingerprint"]],
            "action": "rollback",
            "target_sha": "deadbeef" * 5,
        }

    container.deps.llm = ScriptedLLM(fabricated, judge_score=1.0)
    container.deps.confidence_scorer = ConstantScorer()
    state = await container.runner.run(make_event("memory-leak"))

    assert state["status"] == "escalated"
    assert "confidence" not in _nodes(state)
    assert remediation.actions == []


async def test_action_none_escalates_even_at_full_confidence(container: Container) -> None:
    container.deps.confidence_scorer = ConstantScorer()
    state = await container.runner.run(make_event("cert-expiry"))
    assert state["confidence"] == 1.0
    assert state["policy"]["decision"] == "escalate"
    assert state["status"] == "escalated"


async def test_full_confidence_cannot_bypass_the_approval_gate(
    container: Container, remediation: MockRemediationProvider
) -> None:
    container.deps.confidence_scorer = ConstantScorer()
    event = make_event("memory-leak")
    state = await container.runner.run(event)
    assert state["confidence"] == 1.0
    assert state["policy"]["decision"] == "approval"
    assert await container.runner.get_state(event.id) is not None
    record = await container.deps.repository.get(event.id)
    assert record is not None
    assert record.status == IncidentStatus.AWAITING_APPROVAL
    assert remediation.actions == []
