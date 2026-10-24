"""Microsoft Teams notifier (Workflows / incoming webhook, Adaptive Card payload)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

import httpx

from resilio.models import Notification, NotificationKind
from resilio.providers.base import BaseNotifier
from resilio.providers.http import build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings


class TeamsNotifier(BaseNotifier):
    name = "teams"

    _COLOR: ClassVar[dict[NotificationKind, str]] = {
        NotificationKind.INFO: "Accent",
        NotificationKind.APPROVAL_REQUEST: "Warning",
        NotificationKind.RESOLVED: "Good",
        NotificationKind.ESCALATED: "Attention",
    }

    def __init__(self, webhook_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.webhook_url = webhook_url
        self._client = client or build_client()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.teams_webhook_url is None:
            raise ValueError("TeamsNotifier requires TEAMS_WEBHOOK_URL")
        return cls(settings.teams_webhook_url.get_secret_value())

    def build_payload(self, notification: Notification) -> dict[str, Any]:
        facts = [
            {"title": key, "value": value}
            for key, value in {
                "incident": notification.incident_id,
                "service": notification.service or "-",
                **notification.fields,
            }.items()
        ]
        card: dict[str, Any] = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": notification.title,
                    "weight": "Bolder",
                    "size": "Medium",
                    "color": self._COLOR[notification.kind],
                    "wrap": True,
                },
                {"type": "TextBlock", "text": notification.body, "wrap": True},
                {"type": "FactSet", "facts": facts},
            ],
            "actions": [
                {"type": "Action.OpenUrl", "title": label, "url": url}
                for label, url in notification.links.items()
                if url.startswith("http")
            ],
        }
        return {
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ],
        }

    async def send(self, notification: Notification) -> bool:
        await request_json(
            self._client,
            "POST",
            self.webhook_url,
            provider=self.name,
            json=self.build_payload(notification),
        )
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
