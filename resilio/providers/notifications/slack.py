"""Slack incoming-webhook notifier using Block Kit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

import httpx

from resilio.models import Notification, NotificationKind
from resilio.providers.base import BaseNotifier
from resilio.providers.http import build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings


class SlackNotifier(BaseNotifier):
    name = "slack"

    _EMOJI: ClassVar[dict[NotificationKind, str]] = {
        NotificationKind.INFO: ":mag:",
        NotificationKind.APPROVAL_REQUEST: ":raising_hand:",
        NotificationKind.RESOLVED: ":white_check_mark:",
        NotificationKind.ESCALATED: ":rotating_light:",
    }

    def __init__(self, webhook_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.webhook_url = webhook_url
        self._client = client or build_client()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.slack_webhook_url is None:
            raise ValueError("SlackNotifier requires SLACK_WEBHOOK_URL")
        return cls(settings.slack_webhook_url.get_secret_value())

    def build_payload(self, notification: Notification) -> dict[str, Any]:
        emoji = self._EMOJI[notification.kind]
        fields = [
            {"type": "mrkdwn", "text": f"*{key}*\n{value}"}
            for key, value in {
                "service": notification.service or "-",
                **notification.fields,
            }.items()
        ][:10]
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{notification.title}"[:150]},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": notification.body[:2900]}},
            {"type": "section", "fields": fields},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"incident `{notification.incident_id}`"}],
            },
        ]
        if notification.links:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": label},
                            "url": url,
                            **({"style": "primary"} if label.lower() == "approve" else {}),
                            **({"style": "danger"} if label.lower() == "reject" else {}),
                        }
                        for label, url in notification.links.items()
                        if url.startswith("http")
                    ][:5],
                }
            )
        return {"text": f"{emoji} {notification.title}", "blocks": blocks}

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
