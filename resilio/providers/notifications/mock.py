"""Notifier that records messages in memory - handy for tests and demos."""

from __future__ import annotations

from resilio.models import Notification, NotificationKind
from resilio.providers.base import BaseNotifier


class MockNotifier(BaseNotifier):
    name = "mock"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True

    def kinds(self) -> list[NotificationKind]:
        return [n.kind for n in self.sent]
