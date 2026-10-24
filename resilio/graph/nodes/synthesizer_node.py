"""Synthesizer node: produce a root-cause hypothesis and remediation plan."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, call_llm_json, instrumented
from resilio.graph.prompts import SYNTHESIZER_SYSTEM, render_synthesizer_prompt
from resilio.graph.state import IncidentState, timeline
from resilio.models import RemediationAction
from resilio.providers.base import LLMRequest

logger = logging.getLogger(__name__)


def normalize_hypothesis(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce model output into the canonical hypothesis shape.

    Values are *not* validated against the evidence here - that is the
    evaluator's job - but types are fixed so downstream code can rely on them.
    """
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    action = str(raw.get("action", "none")).lower().strip()
    evidence = raw.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    target = raw.get("target_sha")
    return {
        "root_cause": str(raw.get("root_cause", "unknown")),
        "summary": str(raw.get("summary", "")),
        "confidence": confidence,
        "evidence": [str(e).strip("[] ") for e in evidence],
        "action": action,
        "target_sha": str(target) if target else None,
        "reasoning": str(raw.get("reasoning", "")),
    }


async def synthesize(
    deps: WorkflowDeps,
    *,
    incident_id: str,
    event: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    deployments: Sequence[Mapping[str, Any]],
    suspect: Mapping[str, Any] | None,
    rollback_target: str | None,
    similar: Sequence[Mapping[str, Any]] = (),
    feedback: str | None = None,
    attempt: int = 0,
) -> tuple[dict[str, Any], int]:
    """Run one synthesis call. Returns ``(hypothesis, tokens_used)``."""
    request = LLMRequest(
        system=SYNTHESIZER_SYSTEM,
        prompt=render_synthesizer_prompt(
            event=event,
            groups=groups,
            deployments=deployments,
            suspect=suspect,
            rollback_target=rollback_target,
            similar=similar,
            feedback=feedback,
        ),
        task="synthesize",
        context={
            "incident_id": incident_id,
            "trace_groups": list(groups),
            "deployments": list(deployments),
            "suspect_deployment": suspect,
            "rollback_target": rollback_target,
            "similar_incidents": list(similar),
            "feedback": feedback,
            "attempt": attempt,
        },
        max_tokens=deps.settings.llm_max_tokens,
        temperature=deps.settings.llm_temperature,
    )
    try:
        raw, response = await call_llm_json(deps.llm, request)
    except Exception as exc:
        logger.warning("synthesis failed: %s", exc)
        return (
            {
                "root_cause": "unknown",
                "summary": "LLM synthesis failed; manual investigation required.",
                "confidence": 0.0,
                "evidence": [],
                "action": RemediationAction.NONE.value,
                "target_sha": None,
                "reasoning": f"synthesis error: {exc}",
                "llm_error": True,
            },
            0,
        )
    return normalize_hypothesis(raw), response.prompt_tokens + response.completion_tokens


def make_synthesizer_node(deps: WorkflowDeps) -> NodeFn:
    @instrumented("synthesizer")
    async def synthesizer_node(state: IncidentState) -> dict[str, Any]:
        attempt = state.get("evaluator_attempts", 0)
        evaluation = state.get("evaluation")
        feedback = evaluation.get("feedback") if attempt and evaluation else None
        hypothesis, tokens = await synthesize(
            deps,
            incident_id=state["incident_id"],
            event=state["event"],
            groups=state.get("trace_groups", []),
            deployments=state.get("deployments", []),
            suspect=state.get("suspect_deployment"),
            rollback_target=state.get("rollback_target"),
            similar=state.get("similar_incidents", []),
            feedback=feedback,
            attempt=attempt,
        )
        label = "revised hypothesis" if attempt else "hypothesis"
        return {
            "hypothesis": hypothesis,
            "llm_tokens": tokens,
            "timeline": timeline(
                "synthesizer",
                f"{label}: {hypothesis['root_cause']} -> {hypothesis['action']} "
                f"(confidence {hypothesis['confidence']:.2f})",
            ),
        }

    return synthesizer_node
