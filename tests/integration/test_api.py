"""HTTP API with the embedded worker running, exactly as in production."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from resilio.config import Settings
from resilio.container import Container, build_container
from resilio.core.queue import InMemoryIncidentQueue
from resilio.core.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, compute_signature
from resilio.main import create_app
from resilio.providers.notifications.mock import MockNotifier

pytestmark = pytest.mark.integration

TOKEN = "test-token"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mock_llm_fault_rate=0.0,
        health_check_timeout_seconds=1,
        log_level="WARNING",
        **overrides,
    )


def _client(settings: Settings) -> TestClient:
    async def factory(s: Settings) -> Container:
        return await build_container(s, notifier=MockNotifier(), queue=InMemoryIncidentQueue())

    return TestClient(create_app(settings, container_factory=factory))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with _client(_settings()) as test_client:
        yield test_client


def wait_for(
    client: TestClient, incident_id: str, *statuses: str, timeout: float = 10
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail: dict[str, Any] = client.get(f"/api/v1/incidents/{incident_id}").json()
        if detail["incident"]["status"] in statuses:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"incident {incident_id} never reached {statuses}")


def simulate(client: TestClient, incident_type: str, service: str, **headers: str) -> str:
    response = client.post(
        "/api/v1/incidents",
        json={
            "service": service,
            "title": f"{incident_type} on {service}",
            "incident_type": incident_type,
            "synthetic": True,
            "severity": "high",
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return str(response.json()["accepted"][0])


def test_ops_endpoints(client: TestClient) -> None:
    assert client.get("/").json()["name"] == "ResilioAI"
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "resilio_incidents_total" in metrics.text
    providers = client.get("/api/v1/providers").json()
    assert providers["active"]["telemetry"] == "mock"
    assert "datadog" in providers["available"]["telemetry"]
    assert providers["mock_mode"] is True


def test_full_hitl_lifecycle_over_http(client: TestClient) -> None:
    incident_id = simulate(client, "memory-leak", "checkout")
    detail = wait_for(client, incident_id, "awaiting_approval")
    assert detail["incident"]["summary"]["hypothesis"]["action"] == "rollback"
    assert any(e["node"] == "request_approval" for e in detail["state"]["timeline"])

    pending = client.get("/api/v1/incidents", params={"status": "awaiting_approval"}).json()
    assert [p["id"] for p in pending] == [incident_id]

    page = client.get(f"/ui/incidents/{incident_id}")
    assert page.status_code == 200 and "memory_leak" in page.text

    response = client.post(f"/api/v1/incidents/{incident_id}/approve", json={"approver": "sre"})
    assert response.status_code == 202
    detail = wait_for(client, incident_id, "resolved")
    assert detail["incident"]["resolved_at"] is not None

    conflict = client.post(f"/api/v1/incidents/{incident_id}/approve")
    assert conflict.status_code == 409

    metrics = client.get("/metrics").text
    assert 'resilio_incidents_total{status="resolved"}' in metrics


def test_reject_and_not_found(client: TestClient) -> None:
    incident_id = simulate(client, "deadlock", "orders")
    wait_for(client, incident_id, "awaiting_approval")
    assert client.post(f"/api/v1/incidents/{incident_id}/reject").status_code == 202
    wait_for(client, incident_id, "escalated")
    assert client.get("/api/v1/incidents/nope").status_code == 404
    assert client.post("/api/v1/incidents/nope/approve").status_code == 404
    assert client.get("/ui/incidents/nope").status_code == 404


def test_duplicate_alerts_are_deduplicated(client: TestClient) -> None:
    body = {
        "service": "search",
        "title": "same alert",
        "incident_type": "rate-limit",
        "synthetic": True,
    }
    first = client.post("/api/v1/incidents", json=body).json()
    second = client.post("/api/v1/incidents", json=body).json()
    assert len(first["accepted"]) == 1
    assert second == {"accepted": [], "deduplicated": 1}


def test_webhook_normalization(client: TestClient) -> None:
    payload = {
        "alerts": [
            {"status": "firing", "labels": {"alertname": "A", "service": "s1"}},
            {"status": "firing", "labels": {"alertname": "B", "service": "s2"}},
        ]
    }
    response = client.post("/api/v1/webhooks/alertmanager", json=payload)
    assert response.status_code == 202
    assert len(response.json()["accepted"]) == 2
    assert client.post("/api/v1/webhooks/nagios", json={}).status_code == 404
    bad = client.post("/api/v1/webhooks/generic", content=b"{not json")
    assert bad.status_code == 422


def test_webhook_hmac_enforced() -> None:
    with _client(_settings(webhook_secret=SecretStr("s3cret"))) as client:
        body = json.dumps({"service": "x", "title": "signed"}).encode()
        unsigned = client.post("/api/v1/webhooks/generic", content=body)
        assert unsigned.status_code == 401
        ts = str(int(time.time()))
        signed = client.post(
            "/api/v1/webhooks/generic",
            content=body,
            headers={
                SIGNATURE_HEADER: compute_signature("s3cret", body, ts),
                TIMESTAMP_HEADER: ts,
            },
        )
        assert signed.status_code == 202, signed.text


def test_api_token_protects_mutations() -> None:
    with _client(_settings(api_token=SecretStr(TOKEN))) as client:
        body = {"service": "x", "title": "t"}
        assert client.post("/api/v1/incidents", json=body).status_code == 401
        wrong = client.post("/api/v1/incidents", json=body, headers={"Authorization": "Bearer no"})
        assert wrong.status_code == 401
        ok = client.post(
            "/api/v1/incidents", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert ok.status_code == 202
        assert client.get("/api/v1/incidents").status_code == 200, "reads stay open"
