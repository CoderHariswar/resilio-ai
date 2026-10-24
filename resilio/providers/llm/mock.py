"""Deterministic, offline stand-in for an LLM.

Implements the two tasks the graph needs - ``synthesize`` (root-cause
hypothesis) and ``evaluate`` (LLM-as-a-judge) - with the transparent rules in
:mod:`resilio.providers.llm.rules`.

To make the self-correction loop observable in demos, ``fault_rate`` makes the
*first* synthesis attempt for a fraction of incidents hallucinate (a fabricated
commit SHA or evidence fingerprint). The evaluator should catch it and the
retry - which receives the judge's feedback - comes back grounded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Self

from resilio.models import RemediationAction
from resilio.providers.base import BaseLLMProvider, LLMRequest, LLMResponse
from resilio.providers.llm.rules import RULES_BY_CAUSE, score_groups
from resilio.utils.log_sanitizer import estimate_tokens

if TYPE_CHECKING:
    from resilio.config import Settings

FABRICATED_SHA = "deadbeefcafe0000000000000000000000000000"
FABRICATED_FINGERPRINT = "f00dfeedbeef"


def _bucket(key: str) -> float:
    """Map ``key`` to a stable float in ``[0, 1)``."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0x1_0000_0000


class MockLLMProvider(BaseLLMProvider):
    name = "mock"

    def __init__(self, fault_rate: float = 0.0, model: str = "resilio-rules-v1") -> None:
        self.fault_rate = fault_rate
        self.model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(fault_rate=settings.mock_llm_fault_rate)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.task == "synthesize":
            payload = self._synthesize(request.context)
        elif request.task == "evaluate":
            payload = self._evaluate(request.context)
        else:
            payload = {"text": "mock response"}
        text = json.dumps(payload)
        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=estimate_tokens(request.system + request.prompt),
            completion_tokens=estimate_tokens(text),
        )

    # ------------------------------------------------------------ synthesize
    def _synthesize(self, ctx: Mapping[str, Any]) -> dict[str, Any]:
        groups = list(ctx.get("trace_groups", []))
        suspect = ctx.get("suspect_deployment")
        rollback_target = ctx.get("rollback_target")
        attempt = int(ctx.get("attempt", 0))
        matches = score_groups(groups)

        if not matches:
            return {
                "root_cause": "unknown",
                "summary": "Evidence does not match any known failure pattern.",
                "confidence": 0.2,
                "evidence": [g["fingerprint"] for g in groups[:2]],
                "action": RemediationAction.NONE.value,
                "target_sha": None,
                "reasoning": "No rule matched; a human should investigate.",
            }

        top = matches[0]
        total = sum(m.score for m in matches)
        confidence = 0.55 + 0.35 * (top.score / total)
        action = top.rule.action
        reasoning = [top.rule.explanation]
        if top.rule.deploy_related:
            if suspect and rollback_target:
                confidence += 0.08
                reasoning.append(
                    f"Errors began after deploy {str(suspect.get('sha', ''))[:8]} "
                    f"('{suspect.get('message', '')}')."
                )
            else:
                confidence -= 0.2
                action = RemediationAction.NONE
                reasoning.append("No deploy correlates with onset, so rollback is unsafe.")
        similar = [
            s
            for s in ctx.get("similar_incidents", [])
            if s.get("root_cause") == top.rule.root_cause
        ]
        if similar and similar[0].get("similarity", 0) >= 0.8:
            confidence += 0.05
            reasoning.append(f"Matches past incident {similar[0].get('incident_id')}.")

        hypothesis: dict[str, Any] = {
            "root_cause": top.rule.root_cause,
            "summary": f"{top.rule.root_cause.replace('_', ' ').capitalize()}: "
            f"{top.rule.explanation}",
            "confidence": round(min(confidence, 0.99), 2),
            "evidence": list(top.fingerprints[:5]),
            "action": action.value,
            "target_sha": rollback_target if action == RemediationAction.ROLLBACK else None,
            "reasoning": " ".join(reasoning),
        }

        incident_id = str(ctx.get("incident_id", ""))
        if attempt == 0 and self.fault_rate > 0 and _bucket(incident_id) < self.fault_rate:
            if action == RemediationAction.ROLLBACK:
                hypothesis["target_sha"] = FABRICATED_SHA
            else:
                hypothesis["evidence"] = [*hypothesis["evidence"], FABRICATED_FINGERPRINT]
        return hypothesis

    # -------------------------------------------------------------- evaluate
    def _evaluate(self, ctx: Mapping[str, Any]) -> dict[str, Any]:
        hypothesis = ctx.get("hypothesis", {})
        groups = list(ctx.get("trace_groups", []))
        issues = list(ctx.get("grounding_issues", []))
        root_cause = str(hypothesis.get("root_cause", "unknown"))
        cited = set(hypothesis.get("evidence", []))
        cited_groups = [g for g in groups if g.get("fingerprint") in cited] or groups

        matches = score_groups(cited_groups)
        if matches and matches[0].rule.root_cause != root_cause:
            issues.append(
                f"cited evidence indicates {matches[0].rule.root_cause}, not {root_cause}"
            )
        elif not matches and root_cause != "unknown":
            issues.append(f"no cited evidence supports {root_cause}")

        rule = RULES_BY_CAUSE.get(root_cause)
        try:
            action = RemediationAction(hypothesis.get("action", "none"))
        except ValueError:
            action = RemediationAction.NONE
        if rule and action not in rule.allowed_actions:
            issues.append(f"action '{action.value}' does not address {root_cause}")

        score = round(max(0.0, 1.0 - 0.35 * len(issues)), 2)
        return {
            "score": score,
            "verdict": "accept" if not issues else "reject",
            "issues": issues,
            "feedback": "; ".join(issues) or "Hypothesis is grounded in the evidence.",
        }
