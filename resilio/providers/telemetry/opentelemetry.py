"""OpenTelemetry traces through the Jaeger Query API.

OTLP is a write protocol; reading traces back requires a storage backend.
Jaeger's ``/api/traces`` is the most widely deployed query surface for OTel
pipelines (Jaeger v2 is itself built on the OTel Collector), so that is what
this provider speaks. Error spans are those with ``error=true`` or
``otel.status_code=ERROR``; exception details come from span events/logs.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.providers.base import BaseTelemetryProvider
from resilio.providers.http import build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings


def _tags(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {t.get("key", ""): t.get("value") for t in items or []}


class OpenTelemetryProvider(BaseTelemetryProvider):
    name = "opentelemetry"

    def __init__(
        self,
        query_url: str,
        token: str | None = None,
        limit: int = 100,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.limit = limit
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or build_client(query_url.rstrip("/"), headers=headers)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        token = settings.otel_query_token.get_secret_value() if settings.otel_query_token else None
        return cls(query_url=settings.otel_query_url, token=token)

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        end_us = int(time.time() * 1_000_000)
        start_us = end_us - lookback_minutes * 60 * 1_000_000
        data = await request_json(
            self._client,
            "GET",
            "/api/traces",
            provider=self.name,
            params={
                "service": service_name,
                "start": start_us,
                "end": end_us,
                "limit": self.limit,
                "tags": json.dumps({"error": "true"}),
            },
        )
        records: list[dict[str, Any]] = []
        for trace in (data or {}).get("data", []):
            processes = trace.get("processes", {})
            for span in trace.get("spans", []):
                tags = _tags(span.get("tags", []))
                if not (
                    tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"
                ):
                    continue
                exception: dict[str, Any] = {}
                for log in span.get("logs", []):
                    fields = _tags(log.get("fields", []))
                    if fields.get("event") == "exception" or "exception.type" in fields:
                        exception = fields
                        break
                process = processes.get(span.get("processID", ""), {})
                records.append(
                    {
                        "timestamp": datetime.fromtimestamp(
                            span.get("startTime", 0) / 1_000_000, tz=UTC
                        ).isoformat(),
                        "service": process.get("serviceName", service_name),
                        "level": "error",
                        "message": str(
                            exception.get("exception.message")
                            or tags.get("otel.status_description")
                            or span.get("operationName", "error span")
                        ),
                        "error_type": str(exception.get("exception.type") or "SpanError"),
                        "trace_id": span.get("traceID", ""),
                        "span_id": span.get("spanID", ""),
                        "attributes": {
                            "operation": span.get("operationName"),
                            "duration_us": span.get("duration"),
                        },
                    }
                )
        return records

    async def healthcheck(self) -> bool:
        try:
            response = await self._client.get("/api/services")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()
