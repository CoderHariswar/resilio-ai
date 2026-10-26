"""Graph state schema.

Everything in the state is JSON-serializable (dicts, lists, strings, numbers)
so it can be checkpointed to PostgreSQL and resumed by any worker after a
human approves - see ``docs/ADR/0002-langgraph-for-hitl-state.md``.
"""

from __future__ import annotations

import operator
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict


class TimelineEntry(TypedDict):
    at: str
    node: str
    message: str


class IncidentState(TypedDict, total=False):
    # --- input
    incident_id: str
    event: dict[str, Any]
    started_at: float
    status: str

    # --- telemetry_node
    trace_groups: list[dict[str, Any]]
    token_stats: dict[str, Any]
    similar_incidents: list[dict[str, Any]]

    # --- vcs_node
    deployments: list[dict[str, Any]]
    suspect_deployment: dict[str, Any] | None
    rollback_target: str | None
    deploy_onset_gap_minutes: float | None

    # --- synthesizer_node / evaluator_node
    hypothesis: dict[str, Any] | None
    evaluation: dict[str, Any] | None
    evaluator_attempts: int

    # --- confidence_node (rule-based; the policy uses `confidence`)
    confidence: float | None
    llm_confidence: float | None
    confidence_breakdown: dict[str, Any] | None

    # --- policy / approval / remediation
    policy: dict[str, Any] | None
    approval: dict[str, Any] | None
    remediation: dict[str, Any] | None
    escalation_reason: str | None

    # --- accumulated across nodes
    timeline: Annotated[list[TimelineEntry], operator.add]
    llm_tokens: Annotated[int, operator.add]


def timeline(node: str, message: str) -> list[TimelineEntry]:
    """Build a single-entry timeline update (appended by the reducer)."""
    return [{"at": datetime.now(UTC).isoformat(), "node": node, "message": message}]
