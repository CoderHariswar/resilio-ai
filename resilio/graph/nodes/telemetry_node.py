"""Telemetry node: gather evidence, redact it, and compress it into fingerprints."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from resilio.core.metrics import PROVIDER_ERRORS_TOTAL, TOKEN_SAVINGS_PERCENT
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, instrumented
from resilio.graph.state import IncidentState, timeline
from resilio.models import IncidentEvent
from resilio.providers.telemetry.mock import SupportsFaultInjection
from resilio.utils.log_sanitizer import compact_traces

logger = logging.getLogger(__name__)


def memory_text(title: str, groups: Sequence[Mapping[str, Any]]) -> str:
    """Text embedded for incident-memory similarity search."""
    return " ".join([title, *(f"{g['error_type']} {g['template']}" for g in groups[:5])])


def make_telemetry_node(deps: WorkflowDeps) -> NodeFn:
    settings = deps.settings

    @instrumented("telemetry")
    async def telemetry_node(state: IncidentState) -> dict[str, Any]:
        event = IncidentEvent.model_validate(state["event"])
        provider = deps.telemetry
        if event.synthetic and event.incident_type and isinstance(provider, SupportsFaultInjection):
            provider.inject_outage(event.service, event.incident_type)

        notes: list[str] = []
        try:
            traces = await provider.fetch_traces(event.service, settings.telemetry_lookback_minutes)
        except Exception as exc:
            PROVIDER_ERRORS_TOTAL.labels(kind="telemetry", provider=provider.name).inc()
            logger.warning("telemetry fetch failed for %s: %s", event.service, exc)
            traces = []
            notes.append(f"telemetry provider error: {exc}")

        traces = list(traces) + [
            {
                "timestamp": event.occurred_at.isoformat(),
                "level": "error",
                "message": line,
                "error_type": "AlertSample",
            }
            for line in event.sample_logs
        ]
        if not traces:
            traces = [
                {
                    "timestamp": event.occurred_at.isoformat(),
                    "level": "error",
                    "message": event.title,
                    "error_type": "Alert",
                }
            ]

        compacted = compact_traces(traces)
        TOKEN_SAVINGS_PERCENT.set(compacted.savings_percent)
        groups = [g.as_dict() for g in compacted.groups]

        embedding = deps.embedder.embed(memory_text(event.title, groups))
        try:
            similar = await deps.repository.find_similar(
                embedding, k=settings.similar_incidents_k, exclude_id=event.id
            )
        except Exception as exc:
            logger.warning("similarity search failed: %s", exc)
            similar = []

        message = (
            f"{len(traces)} traces -> {len(groups)} fingerprints "
            f"({compacted.savings_percent:.1f}% token savings); {len(similar)} similar incidents"
        )
        if notes:
            message += f"; {'; '.join(notes)}"
        return {
            "status": "investigating",
            "trace_groups": groups,
            "token_stats": {
                "raw_traces": len(traces),
                "raw_tokens": compacted.raw_tokens,
                "compact_tokens": compacted.compact_tokens,
                "savings_percent": compacted.savings_percent,
            },
            "similar_incidents": similar,
            "timeline": timeline("telemetry", message),
        }

    return telemetry_node
