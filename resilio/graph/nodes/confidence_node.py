"""Confidence node: score an accepted hypothesis from verified signals.

Runs between the evaluator (on accept) and the policy engine. The policy acts
on the rule-based ``confidence``; the model's self-reported number is kept as
``llm_confidence`` for comparison only. See :mod:`resilio.scoring.confidence`.
"""

from __future__ import annotations

from typing import Any

from resilio.core.metrics import CONFIDENCE_SCORE, LLM_CONFIDENCE_DELTA
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, instrumented
from resilio.graph.state import IncidentState, timeline
from resilio.scoring.confidence import signals_from_evidence


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def make_confidence_node(deps: WorkflowDeps) -> NodeFn:
    @instrumented("confidence")
    async def confidence_node(state: IncidentState) -> dict[str, Any]:
        hypothesis = state.get("hypothesis") or {}
        signals = signals_from_evidence(
            hypothesis=hypothesis,
            evaluation=state.get("evaluation") or {},
            trace_groups=state.get("trace_groups", []),
            total_traces=(state.get("token_stats") or {}).get("raw_traces"),
            deploy_onset_gap_minutes=state.get("deploy_onset_gap_minutes"),
            similar_incidents=state.get("similar_incidents", []),
            revision_count=max(state.get("evaluator_attempts", 1) - 1, 0),
        )
        result = deps.confidence_scorer.score(signals)
        # Plugins may misbehave; the policy only ever sees a value in [0, 1].
        value = min(max(result.value, 0.0), 1.0)
        CONFIDENCE_SCORE.observe(value)
        llm_confidence = _as_float(hypothesis.get("confidence"))
        message = result.summary()
        if llm_confidence is not None:
            LLM_CONFIDENCE_DELTA.observe(value - llm_confidence)
            message += f"; model said {llm_confidence:.2f}"
        return {
            "confidence": value,
            "llm_confidence": llm_confidence,
            "confidence_breakdown": result.as_dict(),
            "timeline": timeline("confidence", message),
        }

    return confidence_node
