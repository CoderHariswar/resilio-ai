"""Rich terminal notifier - the default channel in Mock Mode."""

from __future__ import annotations

from typing import ClassVar

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from resilio.models import Notification, NotificationKind
from resilio.providers.base import BaseNotifier


class TerminalNotifier(BaseNotifier):
    name = "terminal"

    _STYLES: ClassVar[dict[NotificationKind, tuple[str, str]]] = {
        NotificationKind.INFO: ("cyan", "i"),
        NotificationKind.APPROVAL_REQUEST: ("yellow", "?"),
        NotificationKind.RESOLVED: ("green", "+"),
        NotificationKind.ESCALATED: ("red", "!"),
    }

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(highlight=False)

    async def send(self, notification: Notification) -> bool:
        color, icon = self._STYLES[notification.kind]
        body = Table.grid(padding=(0, 2))
        body.add_column(style="bold")
        body.add_column()
        body.add_row("incident", notification.incident_id)
        if notification.service:
            body.add_row("service", notification.service)
        if notification.body:
            body.add_row("summary", notification.body)
        for key, value in notification.fields.items():
            body.add_row(key, value)
        for label, url in notification.links.items():
            body.add_row(label, url)
        self.console.print(
            Panel(
                body,
                title=f"[{color}][{icon}] {notification.title}[/]",
                border_style=color,
                expand=False,
            )
        )
        return True
