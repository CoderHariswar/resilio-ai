"""Prometheus telemetry.

Prometheus has no traces, so ``fetch_traces`` turns firing alerts and the
current error ratio into evidence records the rest of the pipeline can reason
about. Pair it with a log/trace provider for richer RCA.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.providers.base import BaseTelemetryProvider
from resilio.providers.http import build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings


class PrometheusTelemetryProvider(BaseTelemetryProvider):
    name = "prometheus"

    def __init__(
        self,
        base_url: str,
        error_rate_query: str,
        error_rate_threshold: float = 0.05,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.error_rate_query = error_rate_query
        self.error_rate_threshold = error_rate_threshold
        self._client = client or build_client(base_url.rstrip("/"))

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            base_url=settings.prometheus_url,
            error_rate_query=settings.prometheus_error_rate_query,
            error_rate_threshold=settings.prometheus_error_rate_threshold,
        )

    async def _instant(self, query: str) -> list[dict[str, Any]]:
        data = await request_json(
            self._client, "GET", "/api/v1/query", provider=self.name, params={"query": query}
        )
        result: list[dict[str, Any]] = (data or {}).get("data", {}).get("result", [])
        return result

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        traces: list[dict[str, Any]] = []
        alerts = await self._instant(f'ALERTS{{alertstate="firing",service="{service_name}"}}')
        for alert in alerts:
            labels = alert.get("metric", {})
            name = labels.get("alertname", "UnknownAlert")
            details = ", ".join(
                f"{k}={v}" for k, v in sorted(labels.items()) if k not in {"__name__", "alertname"}
            )
            traces.append(
                {
                    "timestamp": now,
                    "service": service_name,
                    "level": labels.get("severity", "error"),
                    "message": f"alert {name} firing ({details})",
                    "error_type": name,
                    "attributes": labels,
                }
            )
        rate = await self.get_error_rate(service_name)
        if rate is not None and rate >= self.error_rate_threshold:
            traces.append(
                {
                    "timestamp": now,
                    "service": service_name,
                    "level": "error",
                    "message": (
                        f"HTTP 5xx ratio {rate:.3f} exceeds threshold {self.error_rate_threshold}"
                    ),
                    "error_type": "ErrorRateBreach",
                    "attributes": {"error_rate": rate},
                }
            )
        return traces

    async def get_error_rate(self, service_name: str) -> float | None:
        result = await self._instant(self.error_rate_query.format(service=service_name))
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, ValueError):
            return None

    async def healthcheck(self) -> bool:
        try:
            response = await self._client.get("/-/ready")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()
