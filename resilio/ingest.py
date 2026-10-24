"""Webhook normalizers: vendor payload -> :class:`IncidentEvent`.

Supported sources:

* ``sentry``       - Sentry issue-alert webhooks (``data.event``).
* ``datadog``      - Datadog monitor webhooks using the default JSON template.
* ``alertmanager`` - Prometheus Alertmanager (one event per firing alert).
* ``generic``      - a JSON body matching :class:`IncidentEvent`.

Payloads carrying the tag/label ``resilio-simulated=<incident-type>`` (as sent by
``scripts/mock_outage_simulator.py``) are flagged ``synthetic`` so Mock Mode can
fabricate telemetry that matches the simulated outage.

Add a source by writing a function ``(payload) -> list[IncidentEvent]`` and
registering it in :data:`NORMALIZERS`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from resilio.models import IncidentEvent, Severity
from resilio.scenarios import SCENARIOS

SIMULATION_LABEL = "resilio-simulated"


class UnsupportedSourceError(ValueError):
    pass


_SEVERITY_ALIASES = {
    "fatal": Severity.CRITICAL,
    "critical": Severity.CRITICAL,
    "p1": Severity.CRITICAL,
    "error": Severity.HIGH,
    "high": Severity.HIGH,
    "p2": Severity.HIGH,
    "warning": Severity.MEDIUM,
    "warn": Severity.MEDIUM,
    "medium": Severity.MEDIUM,
    "p3": Severity.MEDIUM,
    "low": Severity.LOW,
    "p4": Severity.LOW,
    "info": Severity.INFO,
    "p5": Severity.INFO,
}


def to_severity(value: Any, default: Severity = Severity.HIGH) -> Severity:
    return _SEVERITY_ALIASES.get(str(value or "").lower().strip(), default)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, int | float):
        from datetime import UTC

        return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _with_time(fields: dict[str, Any], value: Any) -> dict[str, Any]:
    parsed = _parse_time(value)
    if parsed is not None:
        fields["occurred_at"] = parsed
    return fields


def normalize_sentry(payload: dict[str, Any]) -> list[IncidentEvent]:
    data = payload.get("data", {})
    event = data.get("event") or data.get("issue") or {}
    tags = {str(k): str(v) for k, v in (event.get("tags") or []) if k}
    service = tags.get("service") or event.get("project") or payload.get("project") or "unknown"
    exceptions = (event.get("exception") or {}).get("values") or []
    sample = [f"{e.get('type', 'Error')}: {e.get('value', '')}" for e in exceptions][:5]
    fields: dict[str, Any] = {
        "source": "sentry",
        "service": str(service),
        "title": event.get("title") or data.get("triggered_rule") or "Sentry alert",
        "severity": to_severity(event.get("level"), Severity.HIGH),
        "environment": event.get("environment") or tags.get("environment") or "production",
        "sample_logs": sample,
        "labels": tags,
        "raw": payload,
    }
    return [IncidentEvent(**_with_time(fields, event.get("datetime") or event.get("timestamp")))]


def normalize_datadog(payload: dict[str, Any]) -> list[IncidentEvent]:
    transition = str(payload.get("alert_transition", "Triggered")).lower()
    if transition in {"recovered", "resolved"}:
        return []
    raw_tags = payload.get("tags") or ""
    tag_list = raw_tags.split(",") if isinstance(raw_tags, str) else list(raw_tags)
    tags = dict(t.strip().split(":", 1) for t in tag_list if ":" in t)
    fields: dict[str, Any] = {
        "source": "datadog",
        "service": tags.get("service", payload.get("service", "unknown")),
        "title": payload.get("title") or payload.get("event_title") or "Datadog monitor alert",
        "severity": to_severity(payload.get("priority") or payload.get("alert_type")),
        "environment": tags.get("env", "production"),
        "sample_logs": [str(payload["body"])[:1000]] if payload.get("body") else [],
        "labels": tags,
        "raw": payload,
    }
    return [IncidentEvent(**_with_time(fields, payload.get("date") or payload.get("last_updated")))]


def normalize_alertmanager(payload: dict[str, Any]) -> list[IncidentEvent]:
    events: list[IncidentEvent] = []
    common = payload.get("commonLabels", {})
    for alert in payload.get("alerts", []):
        if alert.get("status", "firing") != "firing":
            continue
        labels = {**common, **alert.get("labels", {})}
        annotations = alert.get("annotations", {})
        fields: dict[str, Any] = {
            "source": "alertmanager",
            "service": labels.get("service") or labels.get("job") or "unknown",
            "title": annotations.get("summary") or labels.get("alertname", "Prometheus alert"),
            "severity": to_severity(labels.get("severity")),
            "environment": labels.get("env") or labels.get("environment") or "production",
            "sample_logs": [annotations["description"]] if annotations.get("description") else [],
            "labels": {str(k): str(v) for k, v in labels.items()},
            "raw": alert,
        }
        events.append(IncidentEvent(**_with_time(fields, alert.get("startsAt"))))
    return events


def normalize_generic(payload: dict[str, Any]) -> list[IncidentEvent]:
    return [IncidentEvent.model_validate({"source": "generic", **payload})]


NORMALIZERS: dict[str, Callable[[dict[str, Any]], list[IncidentEvent]]] = {
    "sentry": normalize_sentry,
    "datadog": normalize_datadog,
    "alertmanager": normalize_alertmanager,
    "prometheus": normalize_alertmanager,
    "generic": normalize_generic,
}


def normalize(source: str, payload: dict[str, Any]) -> list[IncidentEvent]:
    """Convert a webhook payload from ``source`` into incident events."""
    try:
        normalizer = NORMALIZERS[source.lower()]
    except KeyError:
        raise UnsupportedSourceError(
            f"unsupported source '{source}'. Supported: {', '.join(sorted(NORMALIZERS))}"
        ) from None
    events = normalizer(payload)
    for event in events:
        simulated = event.labels.get(SIMULATION_LABEL)
        if simulated in SCENARIOS:
            event.synthetic = True
            event.incident_type = simulated
    return events
