from __future__ import annotations

import io
import json

import httpx
import pytest
from pydantic import SecretStr
from rich.console import Console

from resilio.config import Settings
from resilio.models import Notification, NotificationKind, Severity
from resilio.providers.base import BaseNotifier
from resilio.providers.notifications.composite import CompositeNotifier
from resilio.providers.notifications.mock import MockNotifier
from resilio.providers.notifications.pagerduty import PagerDutyNotifier
from resilio.providers.notifications.slack import SlackNotifier
from resilio.providers.notifications.teams import TeamsNotifier
from resilio.providers.notifications.terminal import TerminalNotifier
from tests.helpers import mock_client


def _note(kind: NotificationKind = NotificationKind.APPROVAL_REQUEST) -> Notification:
    return Notification(
        incident_id="inc-1",
        kind=kind,
        title="Approval needed: rollback checkout",
        body="Memory leak after deploy",
        severity=Severity.CRITICAL,
        service="checkout",
        fields={"risk": "high"},
        links={"Review": "https://resilio/ui/incidents/inc-1", "cli": "resilio approve inc-1"},
    )


class Recorder:
    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})


async def test_slack_block_kit_payload() -> None:
    rec = Recorder()
    notifier = SlackNotifier("https://hooks.slack.test/x", client=mock_client(rec))
    assert await notifier.send(_note())
    payload = rec.bodies[0]
    assert ":raising_hand:" in str(payload["text"])
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    actions = blocks[-1]
    assert actions["type"] == "actions"
    assert [e["url"] for e in actions["elements"]] == ["https://resilio/ui/incidents/inc-1"]
    await notifier.aclose()


async def test_pagerduty_trigger_resolve_and_skip_info() -> None:
    rec = Recorder()
    notifier = PagerDutyNotifier("rk", client=mock_client(rec))
    assert await notifier.send(_note(NotificationKind.ESCALATED))
    assert await notifier.send(_note(NotificationKind.RESOLVED))
    assert await notifier.send(_note(NotificationKind.INFO))
    trigger, resolve = rec.bodies
    assert trigger["event_action"] == "trigger"
    assert trigger["dedup_key"] == "inc-1"
    assert trigger["payload"]["severity"] == "critical"  # type: ignore[index]
    assert resolve == {"routing_key": "rk", "event_action": "resolve", "dedup_key": "inc-1"}


async def test_teams_adaptive_card() -> None:
    rec = Recorder()
    notifier = TeamsNotifier("https://teams.test/hook", client=mock_client(rec))
    assert await notifier.send(_note(NotificationKind.RESOLVED))
    card = rec.bodies[0]["attachments"][0]["content"]  # type: ignore[index]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["color"] == "Good"
    assert {"title": "risk", "value": "high"} in card["body"][2]["facts"]


async def test_terminal_renders_panel() -> None:
    buffer = io.StringIO()
    notifier = TerminalNotifier(Console(file=buffer, width=120, color_system=None))
    assert await notifier.send(_note())
    output = buffer.getvalue()
    assert "Approval needed" in output
    assert "inc-1" in output
    assert "Memory leak after deploy" in output


class Exploding(BaseNotifier):
    name = "exploding"

    async def send(self, notification: Notification) -> bool:
        raise RuntimeError("boom")


class Declining(BaseNotifier):
    name = "declining"

    async def send(self, notification: Notification) -> bool:
        return False


async def test_composite_isolates_failures() -> None:
    mock = MockNotifier()
    composite = CompositeNotifier([Exploding(), mock])
    assert await composite.send(_note()) is False
    assert mock.kinds() == [NotificationKind.APPROVAL_REQUEST]
    assert await CompositeNotifier([Declining()]).send(_note()) is False
    assert await CompositeNotifier([mock]).send(_note()) is True
    await composite.aclose()


@pytest.mark.parametrize(
    ("cls", "field", "env"),
    [
        (SlackNotifier, "slack_webhook_url", "SLACK_WEBHOOK_URL"),
        (PagerDutyNotifier, "pagerduty_routing_key", "PAGERDUTY_ROUTING_KEY"),
        (TeamsNotifier, "teams_webhook_url", "TEAMS_WEBHOOK_URL"),
    ],
)
def test_notifiers_require_configuration(cls: type, field: str, env: str) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match=env):
        cls.from_settings(settings)
    assert cls.from_settings(settings.model_copy(update={field: SecretStr("https://x")}))
