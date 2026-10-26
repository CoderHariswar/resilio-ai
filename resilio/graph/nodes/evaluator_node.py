"""Evaluator node: deterministic grounding checks + LLM-as-a-judge.

A hypothesis is accepted only if (1) every grounding check passes - cited
fingerprints exist, a rollback SHA is real and older than the current deploy -
and (2) the judge scores it at or above ``EVALUATOR_MIN_SCORE``. Rejections
are fed back to the synthesizer as concrete revision instructions.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from resilio.core.metrics import EVALUATOR_CORRECTIONS_TOTAL
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, call_llm_json, instrumented
from resilio.graph.prompts import JUDGE_SYSTEM, render_judge_prompt
from resilio.graph.state import IncidentState, timeline
from resilio.models import RemediationAction
from resilio.providers.base import LLMRequest

logger = logging.getLogger(__name__)

MIN_SHA_PREFIX = 7


def _sha_matches(candidate: str, known: str) -> bool:
    return len(candidate) >= MIN_SHA_PREFIX and known.startswith(candidate.lower())


def check_grounding(
    hypothesis: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    deployments: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Mechanical hallucination checks. Any finding is fatal."""
    issues: list[str] = []
    valid_actions = {a.value for a in RemediationAction}
    action = hypothesis.get("action")
    if action not in valid_actions:
        issues.append(f"invalid action '{action}'")

    confidence = hypothesis.get("confidence")
    if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
        issues.append(f"confidence {confidence!r} outside [0, 1]")

    known = {str(g["fingerprint"]) for g in groups}
    evidence = [str(e) for e in hypothesis.get("evidence", [])]
    fabricated = [e for e in evidence if e not in known]
    if fabricated:
        issues.append(f"cites fingerprints not present in evidence: {', '.join(fabricated)}")
    if not evidence and hypothesis.get("root_cause") != "unknown":
        issues.append("no evidence cited")

    if action == RemediationAction.ROLLBACK.value:
        target = str(hypothesis.get("target_sha") or "")
        shas = [str(d["sha"]).lower() for d in deployments]
        if not target:
            issues.append("rollback requires target_sha")
        elif not any(_sha_matches(target, sha) for sha in shas):
            issues.append(f"target_sha {target} not found in deployment history")
        elif shas and _sha_matches(target, shas[0]):
            issues.append(f"target_sha {target} is the currently deployed version")
    return issues


async def evaluate_hypothesis(
    deps: WorkflowDeps,
    hypothesis: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    deployments: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Judge one hypothesis. Returns ``(evaluation, tokens_used)``."""
    grounding = check_grounding(hypothesis, groups, deployments)
    request = LLMRequest(
        system=JUDGE_SYSTEM,
        prompt=render_judge_prompt(
            hypothesis=hypothesis,
            groups=groups,
            deployments=deployments,
            grounding_issues=grounding,
        ),
        task="evaluate",
        context={
            "hypothesis": dict(hypothesis),
            "trace_groups": list(groups),
            "deployments": list(deployments),
            "grounding_issues": grounding,
        },
        max_tokens=deps.settings.llm_max_tokens,
        temperature=0.0,
    )
    tokens = 0
    judge_available = True
    try:
        verdict, response = await call_llm_json(deps.llm, request)
        tokens = response.prompt_tokens + response.completion_tokens
        score = float(verdict.get("score", 0.0))
        judge_issues = [str(i) for i in verdict.get("issues", [])]
        judge_accepts = str(verdict.get("verdict", "reject")).lower() == "accept"
        feedback = str(verdict.get("feedback", ""))
    except Exception as exc:
        logger.warning("judge unavailable, falling back to grounding checks: %s", exc)
        judge_available = False
        score = 1.0 if not grounding else 0.0
        judge_issues, judge_accepts, feedback = [], not grounding, ""

    issues = list(dict.fromkeys([*grounding, *judge_issues]))
    accepted = not grounding and judge_accepts and score >= deps.settings.evaluator_min_score
    if not feedback:
        feedback = "; ".join(issues) or "Hypothesis is grounded in the evidence."
    return (
        {
            "accepted": accepted,
            "score": round(score, 3),
            "issues": issues,
            "grounding_issues": grounding,
            "feedback": feedback,
            "judge_available": judge_available,
        },
        tokens,
    )


def make_evaluator_node(deps: WorkflowDeps) -> NodeFn:
    max_retries = deps.settings.max_evaluator_retries

    @instrumented("evaluator")
    async def evaluator_node(state: IncidentState) -> dict[str, Any]:
        hypothesis = state.get("hypothesis") or {}
        evaluation, tokens = await evaluate_hypothesis(
            deps, hypothesis, state.get("trace_groups", []), state.get("deployments", [])
        )
        attempts = state.get("evaluator_attempts", 0) + 1
        if evaluation["accepted"]:
            message = f"accepted (score {evaluation['score']:.2f})"
        elif attempts <= max_retries:
            EVALUATOR_CORRECTIONS_TOTAL.inc()
            message = (
                f"rejected, requesting revision {attempts}/{max_retries}: {evaluation['feedback']}"
            )
        else:
            message = f"rejected after {attempts} attempts: {evaluation['feedback']}"
        return {
            "evaluation": evaluation,
            "evaluator_attempts": attempts,
            "llm_tokens": tokens,
            "timeline": timeline("evaluator", message),
        }

    return evaluator_node


def make_evaluation_router(
    deps: WorkflowDeps,
) -> Any:
    max_retries = deps.settings.max_evaluator_retries

    def route_after_evaluation(
        state: IncidentState,
    ) -> Literal["confidence", "synthesizer", "escalate"]:
        evaluation = state.get("evaluation") or {}
        if evaluation.get("accepted"):
            return "confidence"
        if state.get("evaluator_attempts", 0) <= max_retries:
            return "synthesizer"
        return "escalate"

    return route_after_evaluation
