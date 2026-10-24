"""Example plugin: a New Relic telemetry provider in under 50 lines.

This is the file from docs/CONTRIBUTING.md, kept runnable and covered by tests.
Copy it to resilio/providers/telemetry/newrelic.py (or ship it as your own
package with an entry point) and set ``TELEMETRY_PROVIDER=newrelic``.
"""

from __future__ import annotations

import os
from typing import Any, Self

import httpx

from resilio.config import Settings
from resilio.providers import BaseTelemetryProvider, register_telemetry
from resilio.providers.http import build_client, request_json

NRQL = (
    "SELECT timestamp, error.class, error.message, trace.id FROM TransactionError "
    "WHERE appName = '{service}' SINCE {minutes} minutes ago LIMIT 200"
)
GRAPHQL = '{{ actor {{ account(id: {account}) {{ nrql(query: "{nrql}") {{ results }} }} }} }}'


@register_telemetry("newrelic")
class NewRelicTelemetryProvider(BaseTelemetryProvider):
    name = "newrelic"

    def __init__(self, api_key: str, account_id: int, client: httpx.AsyncClient | None = None):
        self.account_id = account_id
        self._client = client or build_client(
            "https://api.newrelic.com", headers={"API-Key": api_key}
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(os.environ["NEW_RELIC_API_KEY"], int(os.environ["NEW_RELIC_ACCOUNT_ID"]))

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        nrql = NRQL.format(service=service_name, minutes=lookback_minutes)
        query = GRAPHQL.format(account=self.account_id, nrql=nrql)
        data = await request_json(
            self._client, "POST", "/graphql", provider=self.name, json={"query": query}
        )
        return [
            {
                "timestamp": str(row.get("timestamp", "")),
                "message": row.get("error.message", ""),
                "error_type": row.get("error.class", "error"),
                "trace_id": row.get("trace.id", ""),
            }
            for row in data["data"]["actor"]["account"]["nrql"]["results"]
        ]
