from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from resilio.config import RiskLevel, Settings
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes.approval_node import make_request_approval_node, route_after_approval
from resilio.graph.nodes.escalation_node import make_escalation_node
from resilio.graph.nodes.evaluator_node import (
    check_grounding,
    evaluate_hypothesis,
    make_evaluation_router,
    make_evaluator_node,
)
from resilio.graph.nodes.policy_node import assess_risk, make_policy_node, route_after_policy
from resilio.graph.nodes.remediation_node import make_remediation_node, route_after_remediation
from resilio.graph.nodes.synthesizer_node import (
    make_synthesizer_node,
    normalize_hypothesis,
    synthesize,
)
from resilio.graph.nodes.telemetry_node import make_telemetry_node
from resilio.graph.nodes.vcs_node import correlate, error_onset, make_vcs_node
from resilio.graph.prompts import render_judge_prompt, render_synthesizer_prompt
from resilio.graph.state import IncidentState
from resilio.models import Deployment, NotificationKind
from resilio.providers.base import BaseLLMProvider, BaseRemediationProvider, LLMRequest, LLMResponse
from resilio.providers.notifications.mock import MockNotifier
from tests.helpers import make_event, metric

GROUPS = [
    {"fingerprint": "aaaaaaaaaaaa", "error_type": "OOMKilled", "sample": "OOMKilled", "count": 9}
]
DEPLOYS = [{"sha": "1" * 40, "deployed_at": "x"}, {"sha": "2" * 40, "deployed_at": "y"}]


def _hyp(**overrides: Any) -> dict[str, Any]:
    base = {
        "root_cause": "memory_leak",
        "summary": "leak",
        "confidence": 0.9,
        "evidence": ["aaaaaaaaaaaa"],
        "action": "rollback",
        "target_sha": "2" * 40,
    }
    base.update(overrides)
    return base


class BrokenLLM(BaseLLMProvider):
    name = "broken"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("LLM down")


class TextLLM(BaseLLMProvider):
    name = "text"

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.text, model="t", prompt_tokens=1, completion_tokens=1)


# ------------------------------------------------------------- telemetry
async def test_telemetry_node_compacts_and_records_savings(deps: WorkflowDeps) -> None:
    event = make_event("memory-leak", sample_logs=["OOMKilled from alert body"])
    update = await make_telemetry_node(deps)({"event": event.model_dump(mode="json")})
    assert update["token_stats"]["raw_traces"] == 121
    assert update["token_stats"]["savings_percent"] > 80
    assert metric("resilio_token_savings_percent") == update["token_stats"]["savings_percent"]
    assert any(g["error_type"] == "AlertSample" for g in update["trace_groups"])
    assert "fingerprints" in update["timeline"][0]["message"]


async def test_telemetry_node_survives_provider_failure(deps: WorkflowDeps) -> None:
    async def boom(service: str, minutes: int) -> list[dict[str, Any]]:
        raise RuntimeError("datadog 500")

    deps.telemetry.fetch_traces = boom  # type: ignore[method-assign]
    event = make_event(synthetic=False)
    update = await make_telemetry_node(deps)({"event": event.model_dump(mode="json")})
    assert update["trace_groups"][0]["error_type"] == "Alert"
    assert "datadog 500" in update["timeline"][0]["message"]


async def test_telemetry_node_finds_similar_incidents(deps: WorkflowDeps) -> None:
    await deps.repository.remember(
        "old",
        "checkout",
        deps.embedder.embed("OOMKilled memory limit"),
        {"root_cause": "memory_leak"},
    )
    event = make_event("memory-leak")
    update = await make_telemetry_node(deps)({"event": event.model_dump(mode="json")})
    assert update["similar_incidents"][0]["incident_id"] == "old"


# ------------------------------------------------------------------- vcs
def test_correlate_and_onset() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    history = [
        Deployment(sha="new", deployed_at=now - timedelta(minutes=10)),
        Deployment(sha="old", deployed_at=now - timedelta(days=1)),
    ]
    assert correlate(history, now) == (history[0], "old")
    assert correlate(history, now - timedelta(hours=1)) == (None, None)
    assert correlate(history, now + timedelta(days=1)) == (None, None)
    assert correlate([], now) == (None, None)
    assert correlate(history[:1], now) == (history[0], None)

    event = make_event(occurred_at=now)
    groups = [{"first_seen": "garbage"}, {"first_seen": (now - timedelta(minutes=3)).isoformat()}]
    assert error_onset(event, groups) == now - timedelta(minutes=3)
    assert error_onset(event, [{"first_seen": "2026-01-01T00:00:00"}]) == now


async def test_vcs_node_identifies_suspect(deps: WorkflowDeps) -> None:
    event = make_event("bad-deploy")
    state: IncidentState = {"event": event.model_dump(mode="json")}
    state.update(await make_telemetry_node(deps)(state))  # type: ignore[typeddict-item]
    update = await make_vcs_node(deps)(state)
    assert update["suspect_deployment"]["message"].startswith("refactor(pricing)")
    assert update["rollback_target"] == update["deployments"][1]["sha"]


async def test_vcs_node_handles_provider_error(deps: WorkflowDeps) -> None:
    async def boom(service: str, limit: int = 10) -> list[Deployment]:
        raise RuntimeError("github down")

    deps.remediation.recent_deployments = boom  # type: ignore[method-assign]
    update = await make_vcs_node(deps)({"event": make_event().model_dump(mode="json")})
    assert update["deployments"] == []
    assert update["rollback_target"] is None


# ----------------------------------------------------------- synthesizer
def test_normalize_hypothesis_coerces_types() -> None:
    out = normalize_hypothesis(
        {"confidence": "high", "evidence": "[abc]", "action": " ROLLBACK ", "target_sha": ""}
    )
    assert out["confidence"] == 0.0
    assert out["evidence"] == ["abc"]
    assert out["action"] == "rollback"
    assert out["target_sha"] is None
    assert out["root_cause"] == "unknown"


async def test_synthesize_falls_back_when_llm_fails(deps: WorkflowDeps) -> None:
    deps.llm = BrokenLLM()
    hypothesis, tokens = await synthesize(
        deps,
        incident_id="i",
        event={"service": "s", "title": "t"},
        groups=GROUPS,
        deployments=DEPLOYS,
        suspect=None,
        rollback_target=None,
    )
    assert hypothesis["llm_error"] is True
    assert hypothesis["action"] == "none"
    assert tokens == 0


async def test_synthesizer_node_passes_feedback_on_retry(deps: WorkflowDeps) -> None:
    seen: list[LLMRequest] = []

    class Spy(TextLLM):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            seen.append(request)
            return await super().complete(request)

    deps.llm = Spy('{"root_cause": "memory_leak", "action": "none", "confidence": 0.5}')
    state: IncidentState = {
        "incident_id": "i",
        "event": make_event().model_dump(mode="json"),
        "trace_groups": GROUPS,
        "evaluator_attempts": 1,
        "evaluation": {"feedback": "cite real fingerprints"},
    }
    update = await make_synthesizer_node(deps)(state)
    assert "revised hypothesis" in update["timeline"][0]["message"]
    assert seen[0].context["feedback"] == "cite real fingerprints"
    assert "Reviewer feedback" in seen[0].prompt
    assert update["llm_tokens"] == 2


def test_prompts_render_all_sections() -> None:
    prompt = render_synthesizer_prompt(
        event={"service": "s", "title": "t", "environment": "production", "severity": "high"},
        groups=GROUPS,
        deployments=[{"sha": "1" * 40, "deployed_at": "now", "message": "m"}],
        suspect={"sha": "1" * 40, "message": "m"},
        rollback_target=None,
        similar=[{"incident_id": "x", "similarity": 0.9, "root_cause": "r", "action": "a"}],
        feedback="fix it",
    )
    for section in ("## Alert", "CURRENT", "## Correlation", "## Similar", "Reviewer feedback"):
        assert section in prompt
    empty = render_synthesizer_prompt(
        event={"service": "s", "title": "t"},
        groups=[],
        deployments=[],
        suspect=None,
        rollback_target=None,
        similar=[],
        feedback=None,
    )
    assert "(no evidence)" in empty and "(no deployment history)" in empty
    judge = render_judge_prompt(
        hypothesis=_hyp(), groups=GROUPS, deployments=[], grounding_issues=[]
    )
    assert "- none" in judge


# ------------------------------------------------------------- evaluator
@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"action": "nuke"}, "invalid action"),
        ({"confidence": 1.7}, "outside [0, 1]"),
        ({"evidence": ["zzzzzzzzzzzz"]}, "not present in evidence"),
        ({"evidence": []}, "no evidence cited"),
        ({"target_sha": None}, "requires target_sha"),
        ({"target_sha": "f" * 40}, "not found in deployment history"),
        ({"target_sha": "1" * 40}, "currently deployed"),
        ({"target_sha": "22"}, "not found"),
    ],
)
def test_grounding_detects_hallucinations(overrides: dict[str, Any], fragment: str) -> None:
    issues = check_grounding(_hyp(**overrides), GROUPS, DEPLOYS)
    assert any(fragment in issue for issue in issues), issues


def test_grounding_accepts_valid_and_sha_prefix() -> None:
    assert check_grounding(_hyp(), GROUPS, DEPLOYS) == []
    assert check_grounding(_hyp(target_sha="2222222"), GROUPS, DEPLOYS) == []
    assert check_grounding(_hyp(root_cause="unknown", evidence=[], action="none"), GROUPS, []) == []


async def test_evaluate_falls_back_to_grounding_when_judge_down(deps: WorkflowDeps) -> None:
    deps.llm = BrokenLLM()
    evaluation, _ = await evaluate_hypothesis(deps, _hyp(), GROUPS, DEPLOYS)
    assert evaluation["accepted"] is True
    assert evaluation["judge_available"] is False
    rejected, _ = await evaluate_hypothesis(deps, _hyp(target_sha=None), GROUPS, DEPLOYS)
    assert rejected["accepted"] is False


async def test_evaluate_respects_min_score(deps: WorkflowDeps) -> None:
    deps.llm = TextLLM('{"score": 0.5, "verdict": "accept", "issues": []}')
    evaluation, _ = await evaluate_hypothesis(deps, _hyp(), GROUPS, DEPLOYS)
    assert evaluation["accepted"] is False
    assert evaluation["feedback"]


async def test_evaluator_node_counts_corrections_and_routes(deps: WorkflowDeps) -> None:
    before = metric("resilio_evaluator_corrections_total")
    node = make_evaluator_node(deps)
    router = make_evaluation_router(deps)
    state: IncidentState = {
        "hypothesis": _hyp(target_sha="f" * 40),
        "trace_groups": GROUPS,
        "deployments": DEPLOYS,
        "evaluator_attempts": 0,
    }
    update = await node(state)
    assert update["evaluator_attempts"] == 1
    assert metric("resilio_evaluator_corrections_total") == before + 1
    assert router({**state, **update}) == "synthesizer"  # type: ignore[typeddict-item]

    exhausted = await node({**state, "evaluator_attempts": deps.settings.max_evaluator_retries})
    assert "rejected after" in exhausted["timeline"][0]["message"]
    assert metric("resilio_evaluator_corrections_total") == before + 1
    assert router({**state, **exhausted}) == "escalate"  # type: ignore[typeddict-item]

    accepted = await node({**state, "hypothesis": _hyp()})
    assert router({**state, **accepted}) == "policy"  # type: ignore[typeddict-item]


# ---------------------------------------------------------------- policy
def _event(**kw: Any) -> dict[str, Any]:
    return {"service": "checkout", "severity": "high", "environment": "production", **kw}


@pytest.mark.parametrize(
    ("hypothesis", "event", "settings_kw", "risk", "decision"),
    [
        (_hyp(action="restart"), _event(), {}, "low", "auto"),
        (_hyp(action="scale_up"), _event(), {}, "low", "auto"),
        (_hyp(), _event(), {}, "medium", "approval"),
        (_hyp(), _event(severity="critical"), {}, "high", "approval"),
        (_hyp(), _event(environment="staging"), {}, "low", "auto"),
        (
            _hyp(action="restart"),
            _event(),
            {"critical_services": ["checkout"]},
            "medium",
            "approval",
        ),
        (_hyp(action="restart", confidence=0.5), _event(), {}, "medium", "approval"),
        (_hyp(), _event(), {"auto_approve_max_risk": RiskLevel.MEDIUM}, "medium", "auto"),
        (
            _hyp(action="restart"),
            _event(),
            {"auto_approve_max_risk": RiskLevel.NONE},
            "low",
            "approval",
        ),
        (_hyp(action="none"), _event(), {}, "none", "escalate"),
        (
            _hyp(confidence=0.1),
            _event(severity="critical"),
            {"critical_services": ["checkout"]},
            "critical",
            "approval",
        ),
    ],
)
def test_assess_risk_matrix(
    hypothesis: dict[str, Any],
    event: dict[str, Any],
    settings_kw: dict[str, Any],
    risk: str,
    decision: str,
) -> None:
    settings = Settings(_env_file=None, **settings_kw)  # type: ignore[call-arg]
    result = assess_risk(settings, event, hypothesis, {"judge_available": True})
    assert (result["risk"], result["decision"]) == (risk, decision), result["reasons"]


def test_assess_risk_penalizes_missing_judge() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    result = assess_risk(settings, _event(), _hyp(action="restart"), {"judge_available": False})
    assert result["risk"] == "medium"
    assert "LLM judge unavailable" in result["reasons"]


async def test_policy_node_notifies_on_auto(deps: WorkflowDeps, notifier: MockNotifier) -> None:
    state: IncidentState = {
        "incident_id": "i",
        "event": _event(),
        "hypothesis": _hyp(action="restart"),
        "evaluation": {},
    }
    update = await make_policy_node(deps)(state)
    assert update["policy"]["decision"] == "auto"
    assert notifier.kinds() == [NotificationKind.INFO]
    assert route_after_policy({**state, **update}) == "remediation"  # type: ignore[typeddict-item]

    escalate = await make_policy_node(deps)({**state, "hypothesis": _hyp(action="none")})
    assert escalate["escalation_reason"]
    assert route_after_policy({**state, **escalate}) == "escalate"  # type: ignore[typeddict-item]
    assert route_after_policy({"policy": {"decision": "approval"}}) == "request_approval"


async def test_request_approval_sends_links(deps: WorkflowDeps, notifier: MockNotifier) -> None:
    state: IncidentState = {
        "incident_id": "inc-9",
        "event": _event(),
        "hypothesis": _hyp(),
        "policy": {"risk": "medium", "reasons": ["rollback base risk medium"]},
    }
    update = await make_request_approval_node(deps)(state)
    assert update["status"] == "awaiting_approval"
    sent = notifier.sent[0]
    assert sent.kind == NotificationKind.APPROVAL_REQUEST
    assert sent.links["Review"].endswith("/ui/incidents/inc-9")
    assert sent.fields["target sha"] == "2" * 40
    assert route_after_approval({"approval": {"approved": True}}) == "remediation"
    assert route_after_approval({"approval": {"approved": False}}) == "escalate"


# ----------------------------------------------------------- remediation
class FlakyRemediation(BaseRemediationProvider):
    name = "flaky"

    def __init__(self, healthy: bool = True, fail: bool = False) -> None:
        super().__init__()
        self.healthy, self.fail = healthy, fail

    async def execute_rollback(self, service_name: str, target_sha: str) -> bool:
        if self.fail:
            raise RuntimeError("argo exploded")
        return True

    async def verify_health(self, service_name: str, timeout_seconds: int) -> bool:
        return self.healthy


@pytest.mark.parametrize(
    ("provider", "action", "fragment", "route"),
    [
        (FlakyRemediation(), "rollback", "service healthy", "resolved"),
        (FlakyRemediation(healthy=False), "rollback", "did not recover", "escalate"),
        (FlakyRemediation(fail=True), "rollback", "argo exploded", "escalate"),
        (FlakyRemediation(), "restart", "does not support restart", "escalate"),
        (FlakyRemediation(), "scale_up", "does not support scaling", "escalate"),
    ],
)
async def test_remediation_outcomes(
    deps: WorkflowDeps, provider: BaseRemediationProvider, action: str, fragment: str, route: str
) -> None:
    deps.remediation = provider
    state: IncidentState = {
        "incident_id": "i",
        "event": _event(),
        "hypothesis": _hyp(action=action),
    }
    update = await make_remediation_node(deps)(state)
    assert fragment in update["timeline"][0]["message"]
    assert route_after_remediation({**state, **update}) == route  # type: ignore[typeddict-item]


async def test_escalation_node_uses_evaluator_feedback(
    deps: WorkflowDeps, notifier: MockNotifier
) -> None:
    update = await make_escalation_node(deps)(
        {
            "incident_id": "i",
            "event": {**_event(), "title": "t"},
            "evaluation": {"feedback": "bad sha"},
        }
    )
    assert update["status"] == "escalated"
    assert "bad sha" in update["escalation_reason"]
    assert notifier.kinds() == [NotificationKind.ESCALATED]
