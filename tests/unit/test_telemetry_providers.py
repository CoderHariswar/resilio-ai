from __future__ import annotations

import json

import httpx
import pytest

from resilio.providers.telemetry.datadog import DatadogTelemetryProvider
from resilio.providers.telemetry.mock import (
    HEALTHY_ERROR_RATE,
    UNHEALTHY_ERROR_RATE,
    MockTelemetryProvider,
    SupportsFaultInjection,
)
from resilio.providers.telemetry.opentelemetry import OpenTelemetryProvider
from resilio.providers.telemetry.prometheus import PrometheusTelemetryProvider
from resilio.providers.telemetry.sentry import SentryTelemetryProvider
from tests.helpers import mock_client


# ----------------------------------------------------------------- mock
async def test_mock_provider_injects_and_heals() -> None:
    provider = MockTelemetryProvider(traces_per_fetch=50)
    assert isinstance(provider, SupportsFaultInjection)
    assert await provider.fetch_traces("svc", 15) == []
    assert await provider.get_error_rate("svc") == HEALTHY_ERROR_RATE

    provider.inject_outage("svc", "disk-full")
    traces = await provider.fetch_traces("svc", 15)
    assert len(traces) == 50
    assert traces == sorted(traces, key=lambda t: t["timestamp"])
    assert any("no space left" in t["message"].lower() for t in traces)
    assert await provider.get_error_rate("svc") == UNHEALTHY_ERROR_RATE

    again = await provider.fetch_traces("svc", 15)
    assert [t["message"] for t in again] == [t["message"] for t in traces], "deterministic"

    provider.heal("svc")
    assert await provider.fetch_traces("svc", 15) == []
    assert await provider.get_error_rate("svc") == HEALTHY_ERROR_RATE


def test_mock_provider_rejects_unknown_type() -> None:
    with pytest.raises(KeyError, match="memory-leak"):
        MockTelemetryProvider().inject_outage("svc", "alien-invasion")


# -------------------------------------------------------------- datadog
async def test_datadog_fetch_and_error_rate() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v2/logs/events/search":
            body = json.loads(request.content)
            assert body["filter"]["query"] == "service:checkout status:error"
            assert body["filter"]["from"] == "now-10m"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "timestamp": "2026-01-01T00:00:00Z",
                                "service": "checkout",
                                "status": "error",
                                "message": "fallback message",
                                "host": "h1",
                                "tags": ["env:prod"],
                                "attributes": {
                                    "error": {"kind": "TypeError", "message": "boom"},
                                    "dd": {"trace_id": 42},
                                },
                            }
                        },
                        {"attributes": {"message": "plain", "attributes": {}}},
                    ]
                },
            )
        if request.url.path == "/api/v1/query":
            assert "service:checkout" in request.url.params["query"]
            return httpx.Response(
                200, json={"series": [{"pointlist": [[1, 0.5], [2, None], [3, 0.12]]}]}
            )
        return httpx.Response(200)

    provider = DatadogTelemetryProvider(
        "k",
        "a",
        error_rate_query="sum:errors{{service:{service}}}",
        client=mock_client(handler),
    )
    traces = await provider.fetch_traces("checkout", 10)
    assert traces[0]["error_type"] == "TypeError"
    assert traces[0]["message"] == "boom"
    assert traces[0]["trace_id"] == "42"
    assert traces[1]["message"] == "plain"
    assert await provider.get_error_rate("checkout") == 0.12
    assert await provider.healthcheck()
    await provider.aclose()


async def test_datadog_error_rate_none_without_query_or_data() -> None:
    provider = DatadogTelemetryProvider(
        "k", "a", error_rate_query="", client=mock_client(lambda r: httpx.Response(200))
    )
    assert await provider.get_error_rate("x") is None
    provider.error_rate_query = "q"
    assert await provider.get_error_rate("x") is None


# --------------------------------------------------------------- sentry
async def test_sentry_discover_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/0/organizations/acme/events/"
        assert request.url.params["query"] == "event.type:error service:checkout project:web"
        assert request.url.params["statsPeriod"] == "15m"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "e1",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "title": "TypeError: x",
                        "error.type": ["ValueError", "TypeError"],
                        "error.value": ["inner", "Cannot read properties"],
                        "trace": "abc",
                    },
                    {"title": "only title", "error.type": [], "error.value": []},
                ]
            },
        )

    client = httpx.AsyncClient(
        base_url="https://sentry.test",
        headers={"Authorization": "Bearer tok"},
        transport=httpx.MockTransport(handler),
    )
    provider = SentryTelemetryProvider("tok", "acme", project="web", client=client)
    traces = await provider.fetch_traces("checkout", 15)
    assert traces[0]["error_type"] == "TypeError"
    assert traces[0]["message"] == "Cannot read properties"
    assert traces[0]["trace_id"] == "abc"
    assert traces[1]["message"] == "only title"
    assert traces[1]["error_type"] == "error"
    await provider.aclose()


# ----------------------------------------------------------- prometheus
async def test_prometheus_alerts_and_error_rate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if request.url.path == "/-/ready":
            return httpx.Response(200)
        if query.startswith("ALERTS"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "result": [
                            {
                                "metric": {
                                    "alertname": "HighLatency",
                                    "severity": "page",
                                    "pod": "p1",
                                }
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"data": {"result": [{"value": [0, "0.31"]}]}})

    provider = PrometheusTelemetryProvider(
        "http://prom", 'rate{{service="{service}"}}', 0.05, client=mock_client(handler)
    )
    traces = await provider.fetch_traces("checkout", 15)
    assert traces[0]["error_type"] == "HighLatency"
    assert "pod=p1" in traces[0]["message"]
    assert traces[1]["error_type"] == "ErrorRateBreach"
    assert await provider.get_error_rate("checkout") == pytest.approx(0.31)
    assert await provider.healthcheck()


async def test_prometheus_no_data() -> None:
    provider = PrometheusTelemetryProvider(
        "http://prom",
        "q",
        client=mock_client(lambda r: httpx.Response(200, json={"data": {"result": []}})),
    )
    assert await provider.fetch_traces("x", 5) == []
    assert await provider.get_error_rate("x") is None


async def test_prometheus_malformed_value() -> None:
    provider = PrometheusTelemetryProvider(
        "http://prom",
        "q",
        client=mock_client(
            lambda r: httpx.Response(200, json={"data": {"result": [{"value": [0, "NaN?"]}]}})
        ),
    )
    assert await provider.get_error_rate("x") is None


# -------------------------------------------------------- opentelemetry
async def test_otel_jaeger_error_spans() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/services":
            return httpx.Response(200, json={"data": []})
        assert request.url.params["service"] == "checkout"
        assert json.loads(request.url.params["tags"]) == {"error": "true"}
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "processes": {"p1": {"serviceName": "checkout"}},
                        "spans": [
                            {
                                "traceID": "t1",
                                "spanID": "s1",
                                "processID": "p1",
                                "operationName": "POST /pay",
                                "startTime": 1_700_000_000_000_000,
                                "duration": 1200,
                                "tags": [{"key": "error", "value": True}],
                                "logs": [
                                    {
                                        "fields": [
                                            {"key": "event", "value": "exception"},
                                            {"key": "exception.type", "value": "TimeoutError"},
                                            {
                                                "key": "exception.message",
                                                "value": "upstream x timed out",
                                            },
                                        ]
                                    }
                                ],
                            },
                            {
                                "traceID": "t1",
                                "spanID": "s2",
                                "processID": "p1",
                                "operationName": "GET /ok",
                                "tags": [{"key": "otel.status_code", "value": "OK"}],
                            },
                            {
                                "traceID": "t1",
                                "spanID": "s3",
                                "processID": "p1",
                                "operationName": "db.query",
                                "tags": [
                                    {"key": "otel.status_code", "value": "ERROR"},
                                    {
                                        "key": "otel.status_description",
                                        "value": "deadlock detected",
                                    },
                                ],
                            },
                        ],
                    }
                ]
            },
        )

    provider = OpenTelemetryProvider("http://jaeger", client=mock_client(handler))
    records = await provider.fetch_traces("checkout", 15)
    assert len(records) == 2
    assert records[0]["error_type"] == "TimeoutError"
    assert records[0]["message"] == "upstream x timed out"
    assert records[0]["timestamp"].startswith("2023-11-14")
    assert records[1]["message"] == "deadlock detected"
    assert records[1]["error_type"] == "SpanError"
    assert await provider.healthcheck()


async def test_healthchecks_report_unreachable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    for provider in (
        OpenTelemetryProvider("http://x", client=mock_client(down)),
        PrometheusTelemetryProvider("http://x", "q", client=mock_client(down)),
        DatadogTelemetryProvider("k", "a", client=mock_client(down)),
    ):
        assert await provider.healthcheck() is False
