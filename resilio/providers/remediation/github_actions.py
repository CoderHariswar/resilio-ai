"""GitHub Actions remediation: dispatch a rollback workflow.

Your repository provides ``.github/workflows/rollback.yml`` (name configurable)
with a ``workflow_dispatch`` trigger accepting ``service`` and ``target_sha``
inputs. ResilioAI dispatches it and then watches the run and/or telemetry.
Deployment history comes from the GitHub Deployments API.

``GITHUB_REPOSITORY`` may contain ``{service}`` for repo-per-service layouts,
e.g. ``acme/{service}``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.models import Deployment
from resilio.providers.base import BaseRemediationProvider, BaseTelemetryProvider
from resilio.providers.http import build_client, request_json
from resilio.providers.remediation._health import telemetry_recovered, wait_until

if TYPE_CHECKING:
    from resilio.config import Settings


class GitHubActionsRemediationProvider(BaseRemediationProvider):
    name = "github_actions"

    def __init__(
        self,
        token: str,
        repository: str,
        workflow: str = "rollback.yml",
        ref: str = "main",
        environment: str = "production",
        api_url: str = "https://api.github.com",
        telemetry: BaseTelemetryProvider | None = None,
        error_rate_threshold: float = 0.05,
        poll_interval_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(telemetry)
        self.repository = repository
        self.workflow = workflow
        self.ref = ref
        self.environment = environment
        self.error_rate_threshold = error_rate_threshold
        self.poll_interval_seconds = poll_interval_seconds
        self._client = client or build_client(
            api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, telemetry: BaseTelemetryProvider | None = None
    ) -> Self:
        if settings.github_token is None or not settings.github_repository:
            raise ValueError("GitHub remediation requires GITHUB_TOKEN and GITHUB_REPOSITORY")
        return cls(
            token=settings.github_token.get_secret_value(),
            repository=settings.github_repository,
            workflow=settings.github_rollback_workflow,
            ref=settings.github_ref,
            environment=settings.github_environment,
            api_url=settings.github_api_url,
            telemetry=telemetry,
            error_rate_threshold=settings.prometheus_error_rate_threshold,
            poll_interval_seconds=settings.health_check_interval_seconds,
        )

    def _repo(self, service_name: str) -> str:
        return self.repository.format(service=service_name)

    async def recent_deployments(self, service_name: str, limit: int = 10) -> list[Deployment]:
        data = await request_json(
            self._client,
            "GET",
            f"/repos/{self._repo(service_name)}/deployments",
            provider=self.name,
            params={"environment": self.environment, "per_page": limit},
        )
        deployments: list[Deployment] = []
        for item in data or []:
            payload: dict[str, Any] = item.get("payload") or {}
            if isinstance(payload, dict) and payload.get("service") not in (None, service_name):
                continue
            deployments.append(
                Deployment(
                    sha=item["sha"],
                    deployed_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
                    author=(item.get("creator") or {}).get("login", "unknown"),
                    message=item.get("description") or "",
                    version=item.get("ref"),
                )
            )
        return deployments

    async def execute_rollback(self, service_name: str, target_sha: str) -> bool:
        await request_json(
            self._client,
            "POST",
            f"/repos/{self._repo(service_name)}/actions/workflows/{self.workflow}/dispatches",
            provider=self.name,
            json={"ref": self.ref, "inputs": {"service": service_name, "target_sha": target_sha}},
        )
        return True

    async def _latest_run_succeeded(self, service_name: str) -> bool:
        data = await request_json(
            self._client,
            "GET",
            f"/repos/{self._repo(service_name)}/actions/workflows/{self.workflow}/runs",
            provider=self.name,
            params={"event": "workflow_dispatch", "per_page": 1},
        )
        runs = (data or {}).get("workflow_runs", [])
        if not runs:
            return False
        run = runs[0]
        if run.get("status") != "completed":
            return False
        return bool(run.get("conclusion") == "success")

    async def verify_health(self, service_name: str, timeout_seconds: int) -> bool:
        async def healthy() -> bool:
            if not await self._latest_run_succeeded(service_name):
                return False
            recovered = await telemetry_recovered(
                self.telemetry, service_name, self.error_rate_threshold
            )
            return recovered is not False

        return await wait_until(healthy, timeout_seconds, self.poll_interval_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()
