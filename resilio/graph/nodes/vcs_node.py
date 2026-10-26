"""VCS node: correlate error onset with the deployment history.

If the newest deploy landed shortly before the first error, it becomes the
*suspect* and the deploy before it becomes the *rollback target*. The
synthesizer may only roll back to a SHA chosen here - never one it invents.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from resilio.core.metrics import PROVIDER_ERRORS_TOTAL
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes._common import NodeFn, instrumented
from resilio.graph.state import IncidentState, timeline
from resilio.models import Deployment, IncidentEvent

logger = logging.getLogger(__name__)

SUSPECT_WINDOW = timedelta(hours=6)


def onset_from_groups(groups: Sequence[Mapping[str, Any]]) -> datetime | None:
    """Earliest timezone-aware ``first_seen`` across evidence groups."""
    onsets: list[datetime] = []
    for group in groups:
        try:
            onsets.append(datetime.fromisoformat(str(group.get("first_seen"))))
        except ValueError:
            continue
    aware = [o for o in onsets if o.tzinfo is not None]
    return min(aware) if aware else None


def error_onset(event: IncidentEvent, groups: list[dict[str, Any]]) -> datetime:
    """Earliest ``first_seen`` across evidence groups, else the alert time."""
    return onset_from_groups(groups) or event.occurred_at


def correlate(
    deployments: list[Deployment], onset: datetime
) -> tuple[Deployment | None, str | None]:
    """Return ``(suspect, rollback_target_sha)`` for a newest-first history."""
    if not deployments:
        return None, None
    latest = deployments[0]
    if latest.deployed_at <= onset and onset - latest.deployed_at <= SUSPECT_WINDOW:
        target = deployments[1].sha if len(deployments) > 1 else None
        return latest, target
    return None, None


def make_vcs_node(deps: WorkflowDeps) -> NodeFn:
    @instrumented("vcs")
    async def vcs_node(state: IncidentState) -> dict[str, Any]:
        event = IncidentEvent.model_validate(state["event"])
        provider = deps.remediation
        try:
            deployments = await provider.recent_deployments(event.service, limit=10)
        except Exception as exc:
            PROVIDER_ERRORS_TOTAL.labels(kind="remediation", provider=provider.name).inc()
            logger.warning("deployment history unavailable for %s: %s", event.service, exc)
            deployments = []

        onset = error_onset(event, state.get("trace_groups", []))
        suspect, target = correlate(deployments, onset)
        gap = round((onset - suspect.deployed_at).total_seconds() / 60, 2) if suspect else None
        if suspect:
            message = (
                f"onset follows deploy {suspect.sha[:8]} ({suspect.message!r}); "
                f"rollback target {target[:8] if target else 'none'}"
            )
        else:
            message = f"{len(deployments)} deployments, none correlated with onset"
        return {
            "deployments": [d.model_dump(mode="json") for d in deployments],
            "suspect_deployment": suspect.model_dump(mode="json") if suspect else None,
            "rollback_target": target,
            "deploy_onset_gap_minutes": gap,
            "timeline": timeline("vcs", message),
        }

    return vcs_node
