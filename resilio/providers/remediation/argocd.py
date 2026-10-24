"""ArgoCD remediation: roll an Application back to a previous history entry.

Note: ArgoCD refuses rollbacks while automated sync is enabled for the app.
Either disable auto-sync for services ResilioAI may roll back, or use the
GitHub Actions provider to revert the GitOps repo instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.models import Deployment
from resilio.providers.base import BaseRemediationProvider, BaseTelemetryProvider
from resilio.providers.http import ProviderError, build_client, request_json
from resilio.providers.remediation._health import telemetry_recovered, wait_until

if TYPE_CHECKING:
    from resilio.config import Settings


class ArgoCDRemediationProvider(BaseRemediationProvider):
    name = "argocd"

    def __init__(
        self,
        base_url: str,
        token: str,
        app_template: str = "{service}",
        verify_tls: bool = True,
        telemetry: BaseTelemetryProvider | None = None,
        error_rate_threshold: float = 0.05,
        poll_interval_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(telemetry)
        self.app_template = app_template
        self.error_rate_threshold = error_rate_threshold
        self.poll_interval_seconds = poll_interval_seconds
        self._client = client or build_client(
            base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            verify=verify_tls,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, telemetry: BaseTelemetryProvider | None = None
    ) -> Self:
        if not settings.argocd_url or settings.argocd_token is None:
            raise ValueError("ArgoCD remediation requires ARGOCD_URL and ARGOCD_TOKEN")
        return cls(
            base_url=settings.argocd_url,
            token=settings.argocd_token.get_secret_value(),
            app_template=settings.argocd_app_template,
            verify_tls=settings.argocd_verify_tls,
            telemetry=telemetry,
            error_rate_threshold=settings.prometheus_error_rate_threshold,
            poll_interval_seconds=settings.health_check_interval_seconds,
        )

    def _app(self, service_name: str) -> str:
        return self.app_template.format(service=service_name)

    async def _get_app(self, service_name: str) -> dict[str, Any]:
        data: dict[str, Any] = await request_json(
            self._client,
            "GET",
            f"/api/v1/applications/{self._app(service_name)}",
            provider=self.name,
        )
        return data

    async def recent_deployments(self, service_name: str, limit: int = 10) -> list[Deployment]:
        app = await self._get_app(service_name)
        history = app.get("status", {}).get("history", []) or []
        deployments = [
            Deployment(
                sha=entry["revision"],
                deployed_at=datetime.fromisoformat(entry["deployedAt"].replace("Z", "+00:00")),
                author=(entry.get("initiatedBy") or {}).get("username", "argocd"),
                message=f"argocd history id {entry.get('id')}",
                version=str(entry.get("id")),
            )
            for entry in history
            if entry.get("revision") and entry.get("deployedAt")
        ]
        deployments.sort(key=lambda d: d.deployed_at, reverse=True)
        return deployments[:limit]

    async def execute_rollback(self, service_name: str, target_sha: str) -> bool:
        app = await self._get_app(service_name)
        history = app.get("status", {}).get("history", []) or []
        entry = next(
            (h for h in reversed(history) if str(h.get("revision", "")).startswith(target_sha)),
            None,
        )
        if entry is None:
            raise ProviderError(self.name, f"revision {target_sha} not found in app history")
        await request_json(
            self._client,
            "POST",
            f"/api/v1/applications/{self._app(service_name)}/rollback",
            provider=self.name,
            json={"id": entry["id"], "prune": False},
        )
        return True

    async def verify_health(self, service_name: str, timeout_seconds: int) -> bool:
        async def healthy() -> bool:
            app = await self._get_app(service_name)
            if app.get("status", {}).get("health", {}).get("status") != "Healthy":
                return False
            recovered = await telemetry_recovered(
                self.telemetry, service_name, self.error_rate_threshold
            )
            return recovered is not False

        return await wait_until(healthy, timeout_seconds, self.poll_interval_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()
