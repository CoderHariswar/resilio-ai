"""In-memory remediation provider with a plausible deployment history.

Works with :class:`MockTelemetryProvider`: an action listed in the active
scenario's ``fixed_by`` set heals the service, anything else leaves the error
rate high so ``verify_health`` fails and the incident escalates - exactly the
behaviour you want to see when exercising the escalation path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta

from resilio.models import Deployment, RemediationAction
from resilio.providers.base import BaseRemediationProvider, BaseTelemetryProvider
from resilio.providers.telemetry.mock import MockTelemetryProvider

logger = logging.getLogger(__name__)

_HISTORY_MESSAGES = (
    "fix(api): tighten request validation",
    "feat(search): add typo tolerance",
    "chore(deps): bump httpx to 0.27",
    "docs: update runbook links",
    "perf(db): add covering index on orders(customer_id)",
)


def mock_sha(service_name: str, index: int) -> str:
    """Deterministic fake 40-char git SHA."""
    return hashlib.sha1(f"{service_name}:{index}".encode(), usedforsecurity=False).hexdigest()


def mock_deployments(
    service_name: str,
    *,
    latest_message: str | None = None,
    latest_at: datetime | None = None,
    count: int = 5,
) -> list[Deployment]:
    """Build a newest-first deployment history for ``service_name``."""
    now = datetime.now(UTC)
    latest_at = latest_at or now - timedelta(days=2)
    history = [
        Deployment(
            sha=mock_sha(service_name, 0),
            deployed_at=latest_at,
            author="dev-bot",
            message=latest_message or _HISTORY_MESSAGES[0],
            version="v1.42.0",
        )
    ]
    for i in range(1, count):
        history.append(
            Deployment(
                sha=mock_sha(service_name, i),
                deployed_at=latest_at - timedelta(days=i, hours=3 * i),
                author=("alice", "bob", "carol", "dave")[i % 4],
                message=_HISTORY_MESSAGES[i % len(_HISTORY_MESSAGES)],
                version=f"v1.{42 - i}.0",
            )
        )
    return history


class MockRemediationProvider(BaseRemediationProvider):
    name = "mock"

    def __init__(
        self,
        telemetry: BaseTelemetryProvider | None = None,
        action_delay_seconds: float = 0.0,
    ) -> None:
        super().__init__(telemetry)
        self.action_delay_seconds = action_delay_seconds
        self.actions: list[tuple[str, str, str]] = []

    def _mock_telemetry(self) -> MockTelemetryProvider | None:
        return self.telemetry if isinstance(self.telemetry, MockTelemetryProvider) else None

    async def recent_deployments(self, service_name: str, limit: int = 10) -> list[Deployment]:
        telemetry = self._mock_telemetry()
        outage = telemetry.active_outage(service_name) if telemetry else None
        if outage and outage.scenario.deploy_related:
            history = mock_deployments(
                service_name,
                latest_message=outage.scenario.deploy_message,
                latest_at=outage.started_at - timedelta(minutes=4),
            )
        else:
            history = mock_deployments(service_name)
        return history[:limit]

    async def _apply(self, service_name: str, action: RemediationAction, detail: str) -> bool:
        await asyncio.sleep(self.action_delay_seconds)
        self.actions.append((service_name, action.value, detail))
        logger.info("[mock] %s %s %s", action.value, service_name, detail)
        telemetry = self._mock_telemetry()
        outage = telemetry.active_outage(service_name) if telemetry else None
        if telemetry and outage and action in outage.scenario.fixed_by:
            telemetry.heal(service_name)
        return True

    async def execute_rollback(self, service_name: str, target_sha: str) -> bool:
        return await self._apply(service_name, RemediationAction.ROLLBACK, target_sha)

    async def restart_service(self, service_name: str) -> bool:
        return await self._apply(service_name, RemediationAction.RESTART, "rolling restart")

    async def scale_service(self, service_name: str, replicas_delta: int) -> bool:
        return await self._apply(service_name, RemediationAction.SCALE_UP, f"+{replicas_delta}")

    async def verify_health(self, service_name: str, timeout_seconds: int) -> bool:
        if self.telemetry is None:
            return True
        rate = await self.telemetry.get_error_rate(service_name)
        return rate is None or rate < 0.05
