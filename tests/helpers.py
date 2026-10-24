"""Shared test helpers (importable as ``tests.helpers``)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
from prometheus_client import REGISTRY

from resilio.models import IncidentEvent
from resilio.scenarios import get_scenario

Handler = Callable[[httpx.Request], httpx.Response]


def mock_client(handler: Handler, base_url: str = "https://test.local") -> httpx.AsyncClient:
    """An AsyncClient whose requests are answered by ``handler``."""
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


def make_event(
    incident_type: str = "memory-leak",
    service: str = "checkout",
    **overrides: object,
) -> IncidentEvent:
    template = get_scenario(incident_type)
    fields: dict[str, object] = {
        "source": "test",
        "service": service,
        "title": template.title,
        "severity": template.severity,
        "incident_type": incident_type,
        "synthetic": True,
    }
    fields.update(overrides)
    return IncidentEvent.model_validate(fields)


def metric(name: str, labels: dict[str, str] | None = None) -> float:
    """Current value of a Prometheus sample (0.0 if absent)."""
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0
