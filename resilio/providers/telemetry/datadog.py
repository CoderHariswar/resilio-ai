"""Datadog telemetry: Logs Search API v2 for evidence, Metrics API for error rate."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.providers.base import BaseTelemetryProvider
from resilio.providers.http import build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings


class DatadogTelemetryProvider(BaseTelemetryProvider):
    name = "datadog"

    def __init__(
        self,
        api_key: str,
        app_key: str,
        site: str = "datadoghq.com",
        error_rate_query: str = "",
        page_limit: int = 500,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.error_rate_query = error_rate_query
        self.page_limit = page_limit
        self._client = client or build_client(
            f"https://api.{site}",
            headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.datadog_api_key is None or settings.datadog_app_key is None:
            raise ValueError(
                "DatadogTelemetryProvider requires DATADOG_API_KEY and DATADOG_APP_KEY"
            )
        return cls(
            api_key=settings.datadog_api_key.get_secret_value(),
            app_key=settings.datadog_app_key.get_secret_value(),
            site=settings.datadog_site,
            error_rate_query=settings.datadog_error_rate_query,
        )

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        body = {
            "filter": {
                "query": f"service:{service_name} status:error",
                "from": f"now-{lookback_minutes}m",
                "to": "now",
            },
            "sort": "-timestamp",
            "page": {"limit": self.page_limit},
        }
        data = await request_json(
            self._client, "POST", "/api/v2/logs/events/search", provider=self.name, json=body
        )
        traces: list[dict[str, Any]] = []
        for item in (data or {}).get("data", []):
            attrs = item.get("attributes", {})
            extra = attrs.get("attributes", {})
            error = extra.get("error", {}) if isinstance(extra.get("error"), dict) else {}
            traces.append(
                {
                    "timestamp": attrs.get("timestamp", ""),
                    "service": attrs.get("service", service_name),
                    "level": attrs.get("status", "error"),
                    "message": error.get("message") or attrs.get("message", ""),
                    "error_type": error.get("kind") or attrs.get("status", "error"),
                    "trace_id": str(extra.get("dd", {}).get("trace_id", "") or ""),
                    "attributes": {"host": attrs.get("host"), "tags": attrs.get("tags", [])[:10]},
                }
            )
        return traces

    async def get_error_rate(self, service_name: str) -> float | None:
        if not self.error_rate_query:
            return None
        now = int(time.time())
        data = await request_json(
            self._client,
            "GET",
            "/api/v1/query",
            provider=self.name,
            params={
                "from": now - 300,
                "to": now,
                "query": self.error_rate_query.format(service=service_name),
            },
        )
        for series in (data or {}).get("series", []):
            points = [p for p in series.get("pointlist", []) if p and p[1] is not None]
            if points:
                return float(points[-1][1])
        return None

    async def healthcheck(self) -> bool:
        try:
            response = await self._client.get("/api/v1/validate")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()
