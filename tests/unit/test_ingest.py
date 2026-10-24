from __future__ import annotations

from datetime import UTC, datetime

import pytest

from resilio.ingest import UnsupportedSourceError, normalize, to_severity
from resilio.models import Severity


def test_sentry_issue_alert() -> None:
    payload = {
        "action": "triggered",
        "data": {
            "event": {
                "title": "TypeError: Cannot read properties of undefined",
                "level": "fatal",
                "environment": "production",
                "datetime": "2026-01-01T10:00:00Z",
                "tags": [["service", "checkout"], ["release", "v2"]],
                "exception": {"values": [{"type": "TypeError", "value": "x is undefined"}]},
            },
            "triggered_rule": "new issue",
        },
    }
    [event] = normalize("sentry", payload)
    assert event.service == "checkout"
    assert event.severity == Severity.CRITICAL
    assert event.sample_logs == ["TypeError: x is undefined"]
    assert event.labels["release"] == "v2"
    assert event.occurred_at == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_datadog_monitor_and_recovery() -> None:
    payload = {
        "title": "[Triggered] High error rate on checkout",
        "body": "error rate 34% > 5%",
        "priority": "P1",
        "tags": "service:checkout,env:staging,team:payments",
        "alert_transition": "Triggered",
        "date": 1767261600000,
    }
    [event] = normalize("datadog", payload)
    assert (event.service, event.environment, event.severity) == (
        "checkout",
        "staging",
        Severity.CRITICAL,
    )
    assert event.sample_logs == ["error rate 34% > 5%"]
    assert event.occurred_at.year == 2026
    assert normalize("datadog", {**payload, "alert_transition": "Recovered"}) == []


def test_datadog_list_tags_and_defaults() -> None:
    [event] = normalize("datadog", {"tags": ["service:api", "bare"]})
    assert event.service == "api"
    assert event.title == "Datadog monitor alert"


def test_alertmanager_fans_out_firing_alerts() -> None:
    payload = {
        "commonLabels": {"env": "production"},
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighLatency", "service": "search", "severity": "warning"},
                "annotations": {"summary": "p99 > 2s", "description": "latency spike"},
                "startsAt": "2026-01-01T00:00:00Z",
            },
            {"status": "resolved", "labels": {"alertname": "Old"}},
            {"status": "firing", "labels": {"alertname": "Disk", "job": "node"}},
        ],
    }
    events = normalize("alertmanager", payload)
    assert [e.service for e in events] == ["search", "node"]
    assert events[0].title == "p99 > 2s"
    assert events[0].severity == Severity.MEDIUM
    assert events[1].title == "Disk"
    assert normalize("prometheus", payload)[0].source == "alertmanager"


def test_generic_and_unsupported() -> None:
    [event] = normalize("generic", {"service": "x", "title": "y", "severity": "low"})
    assert event.source == "generic" and event.severity == Severity.LOW
    with pytest.raises(UnsupportedSourceError, match="sentry"):
        normalize("nagios", {})
    with pytest.raises(ValueError, match="service"):
        normalize("generic", {"title": "missing service"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("P2", Severity.HIGH),
        ("warn", Severity.MEDIUM),
        (None, Severity.HIGH),
        ("??", Severity.HIGH),
    ],
)
def test_severity_aliases(raw: str | None, expected: Severity) -> None:
    assert to_severity(raw) == expected


def test_invalid_timestamp_is_ignored() -> None:
    [event] = normalize("datadog", {"date": "not a date", "tags": ""})
    assert event.occurred_at.tzinfo is not None
