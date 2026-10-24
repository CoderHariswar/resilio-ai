"""Incident ingestion queue with alert-storm deduplication.

``FastAPI -> queue -> worker`` decouples webhook latency from graph execution.
Redis gives durability across restarts and lets several API replicas feed one
worker pool; the in-memory queue keeps Mock Mode dependency-free.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from resilio.models import IncidentEvent

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from resilio.config import Settings

logger = logging.getLogger(__name__)


class IncidentQueue(ABC):
    """FIFO of :class:`IncidentEvent` with a dedupe window."""

    def __init__(self, dedupe_window_seconds: int = 300) -> None:
        self.dedupe_window_seconds = dedupe_window_seconds

    @abstractmethod
    async def enqueue(self, event: IncidentEvent) -> bool:
        """Queue ``event``. Return False if it duplicates a recent alert."""

    @abstractmethod
    async def dequeue(self, timeout: float = 1.0) -> IncidentEvent | None:
        """Pop the next event, or None after ``timeout`` seconds."""

    @abstractmethod
    async def size(self) -> int: ...

    @abstractmethod
    async def release(self, event: IncidentEvent) -> None:
        """Clear the dedupe marker so a recurrence opens a new incident."""

    async def aclose(self) -> None:
        return None


class InMemoryIncidentQueue(IncidentQueue):
    def __init__(self, dedupe_window_seconds: int = 300) -> None:
        super().__init__(dedupe_window_seconds)
        self._queue: asyncio.Queue[IncidentEvent] = asyncio.Queue()
        self._seen: dict[str, float] = {}

    async def enqueue(self, event: IncidentEvent) -> bool:
        now = time.monotonic()
        expires = self._seen.get(event.dedupe_key)
        if expires is not None and expires > now:
            return False
        self._seen[event.dedupe_key] = now + self.dedupe_window_seconds
        await self._queue.put(event)
        return True

    async def dequeue(self, timeout: float = 1.0) -> IncidentEvent | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def size(self) -> int:
        return self._queue.qsize()

    async def release(self, event: IncidentEvent) -> None:
        self._seen.pop(event.dedupe_key, None)


class RedisIncidentQueue(IncidentQueue):
    """Redis list + ``SET NX EX`` dedupe markers."""

    def __init__(self, client: Redis, name: str, dedupe_window_seconds: int = 300) -> None:
        super().__init__(dedupe_window_seconds)
        self._redis = client
        self._name = name

    def _dedupe_key(self, event: IncidentEvent) -> str:
        return f"{self._name}:dedupe:{event.dedupe_key}"

    async def enqueue(self, event: IncidentEvent) -> bool:
        fresh = await self._redis.set(
            self._dedupe_key(event), event.id, nx=True, ex=self.dedupe_window_seconds
        )
        if not fresh:
            return False
        await self._redis.rpush(self._name, event.model_dump_json())
        return True

    async def dequeue(self, timeout: float = 1.0) -> IncidentEvent | None:
        item = await self._redis.blpop([self._name], timeout=max(1, int(timeout)))
        if item is None:
            return None
        _, payload = item
        return IncidentEvent.model_validate_json(payload)

    async def size(self) -> int:
        return int(await self._redis.llen(self._name))

    async def release(self, event: IncidentEvent) -> None:
        await self._redis.delete(self._dedupe_key(event))

    async def aclose(self) -> None:
        await self._redis.aclose()


def build_queue(settings: Settings) -> IncidentQueue:
    """Redis queue when ``REDIS_URL`` is set, otherwise in-process."""
    if settings.redis_url:
        from redis.asyncio import Redis

        logger.info("incident queue: redis")
        return RedisIncidentQueue(
            Redis.from_url(settings.redis_url, decode_responses=True),
            settings.queue_name,
            settings.dedupe_window_seconds,
        )
    logger.info("incident queue: in-memory")
    return InMemoryIncidentQueue(settings.dedupe_window_seconds)
