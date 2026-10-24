"""PagerDuty Events API v2 notifier.

Pages on approval requests and escalations, auto-resolves on recovery, and
ignores informational updates (nobody wants to be woken up for those). The
incident id is the ``dedup_key`` so all updates land on one PagerDuty incident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

import httpx

from resilio.models import Notification, NotificationKind, Severity
from resilio.providers.base import BaseNotifier
from resilio.providers.http import build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings

EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"


class PagerDutyNotifier(BaseNotifier):
    name = "pagerduty"

    _SEVERITY: ClassVar[dict[Severity, str]] = {
        Severity.CRITICAL: "critical",
        Severity.HIGH: "error",
        Severity.MEDIUM: "warning",
        Severity.LOW: "info",
        Severity.INFO: "info",
    }

    def __init__(
        self,
        routing_key: str,
        client: httpx.AsyncClient | None = None,
        events_url: str = EVENTS_URL,
    ) -> None:
        self.routing_key = routing_key
        self.events_url = events_url
        self._client = client or build_client()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.pagerduty_routing_key is None:
            raise ValueError("PagerDutyNotifier requires PAGERDUTY_ROUTING_KEY")
        return cls(settings.pagerduty_routing_key.get_secret_value())

    def build_payload(self, notification: Notification) -> dict[str, Any] | None:
        if notification.kind == NotificationKind.INFO:
            return None
        if notification.kind == NotificationKind.RESOLVED:
            return {
                "routing_key": self.routing_key,
                "event_action": "resolve",
                "dedup_key": notification.incident_id,
            }
        return {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "dedup_key": notification.incident_id,
            "payload": {
                "summary": f"{notification.title}: {notification.body}"[:1024],
                "source": notification.service or "resilio-ai",
                "severity": self._SEVERITY[notification.severity],
                "component": notification.service,
                "group": "resilio-ai",
                "class": notification.kind.value,
                "custom_details": notification.fields,
            },
            "links": [
                {"href": url, "text": label}
                for label, url in notification.links.items()
                if url.startswith("http")
            ],
        }

    async def send(self, notification: Notification) -> bool:
        payload = self.build_payload(notification)
        if payload is None:
            return True
        await request_json(self._client, "POST", self.events_url, provider=self.name, json=payload)
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
