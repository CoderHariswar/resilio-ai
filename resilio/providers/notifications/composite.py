"""Fan-out notifier: one failing channel never blocks the others."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from resilio.core.metrics import PROVIDER_ERRORS_TOTAL
from resilio.models import Notification
from resilio.providers.base import BaseNotifier

logger = logging.getLogger(__name__)


class CompositeNotifier(BaseNotifier):
    name = "composite"

    def __init__(self, notifiers: Sequence[BaseNotifier]) -> None:
        self.notifiers = list(notifiers)

    async def send(self, notification: Notification) -> bool:
        results = await asyncio.gather(
            *(n.send(notification) for n in self.notifiers), return_exceptions=True
        )
        ok = True
        for notifier, result in zip(self.notifiers, results, strict=True):
            if isinstance(result, BaseException):
                PROVIDER_ERRORS_TOTAL.labels(kind="notifier", provider=notifier.name).inc()
                logger.warning("notifier %s failed: %s", notifier.name, result)
                ok = False
            elif result is False:
                ok = False
        return ok

    async def aclose(self) -> None:
        await asyncio.gather(*(n.aclose() for n in self.notifiers), return_exceptions=True)
