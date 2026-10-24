"""Sentry telemetry via the Discover (organization events) API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.providers.base import BaseTelemetryProvider
from resilio.providers.http import build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings

_FIELDS = ("id", "timestamp", "title", "message", "error.type", "error.value", "trace", "level")


class SentryTelemetryProvider(BaseTelemetryProvider):
    name = "sentry"

    def __init__(
        self,
        auth_token: str,
        org: str,
        project: str | None = None,
        base_url: str = "https://sentry.io",
        service_tag: str = "service",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.org = org
        self.project = project
        self.service_tag = service_tag
        self._client = client or build_client(
            base_url.rstrip("/"), headers={"Authorization": f"Bearer {auth_token}"}
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.sentry_auth_token is None or not settings.sentry_org:
            raise ValueError("SentryTelemetryProvider requires SENTRY_AUTH_TOKEN and SENTRY_ORG")
        return cls(
            auth_token=settings.sentry_auth_token.get_secret_value(),
            org=settings.sentry_org,
            project=settings.sentry_project,
            base_url=settings.sentry_base_url,
            service_tag=settings.sentry_service_tag,
        )

    def _query(self, service_name: str) -> str:
        parts = ["event.type:error", f"{self.service_tag}:{service_name}"]
        if self.project:
            parts.append(f"project:{self.project}")
        return " ".join(parts)

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        data = await request_json(
            self._client,
            "GET",
            f"/api/0/organizations/{self.org}/events/",
            provider=self.name,
            params={
                "field": list(_FIELDS),
                "query": self._query(service_name),
                "statsPeriod": f"{lookback_minutes}m",
                "sort": "-timestamp",
                "per_page": 100,
            },
        )
        traces: list[dict[str, Any]] = []
        for row in (data or {}).get("data", []):
            error_type = row.get("error.type")
            error_value = row.get("error.value")
            if isinstance(error_type, list):
                error_type = error_type[-1] if error_type else None
            if isinstance(error_value, list):
                error_value = error_value[-1] if error_value else None
            traces.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "service": service_name,
                    "level": row.get("level", "error"),
                    "message": error_value or row.get("message") or row.get("title", ""),
                    "error_type": error_type or "error",
                    "trace_id": row.get("trace") or "",
                    "attributes": {"sentry_event_id": row.get("id")},
                }
            )
        return traces

    async def aclose(self) -> None:
        await self._client.aclose()
