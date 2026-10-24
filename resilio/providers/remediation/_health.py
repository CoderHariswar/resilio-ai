"""Polling helpers shared by remediation providers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from resilio.providers.base import BaseTelemetryProvider

logger = logging.getLogger(__name__)


async def wait_until(
    predicate: Callable[[], Awaitable[bool]],
    timeout_seconds: float,
    interval_seconds: float = 5.0,
) -> bool:
    """Poll ``predicate`` until it returns True or ``timeout_seconds`` elapse.

    Exceptions raised by the predicate count as "not yet" and are logged.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if await predicate():
                return True
        except Exception as exc:
            logger.debug("health predicate raised: %s", exc)
        if time.monotonic() + interval_seconds > deadline:
            return False
        await asyncio.sleep(interval_seconds)


async def telemetry_recovered(
    telemetry: BaseTelemetryProvider | None, service_name: str, threshold: float
) -> bool | None:
    """True/False if telemetry reports an error rate; None if it cannot tell."""
    if telemetry is None:
        return None
    rate = await telemetry.get_error_rate(service_name)
    if rate is None:
        return None
    return rate < threshold
