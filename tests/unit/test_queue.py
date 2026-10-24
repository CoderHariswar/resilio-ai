from __future__ import annotations

from typing import Any

from resilio.config import Settings
from resilio.core.queue import (
    InMemoryIncidentQueue,
    RedisIncidentQueue,
    build_queue,
)
from tests.helpers import make_event


async def test_in_memory_queue_dedupes_until_released() -> None:
    queue = InMemoryIncidentQueue(dedupe_window_seconds=60)
    first, duplicate = make_event(), make_event()
    assert await queue.enqueue(first)
    assert not await queue.enqueue(duplicate)
    assert await queue.size() == 1
    assert (await queue.dequeue(0.1)) == first
    assert await queue.dequeue(0.01) is None
    await queue.release(first)
    assert await queue.enqueue(duplicate)
    await queue.aclose()


async def test_in_memory_dedupe_window_expires() -> None:
    queue = InMemoryIncidentQueue(dedupe_window_seconds=0)
    assert await queue.enqueue(make_event())
    assert await queue.enqueue(make_event())


class FakeRedis:
    """The handful of redis.asyncio commands the queue uses."""

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.lists: dict[str, list[str]] = {}
        self.closed = False

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def rpush(self, name: str, value: str) -> int:
        self.lists.setdefault(name, []).append(value)
        return len(self.lists[name])

    async def blpop(self, names: list[str], timeout: int = 0) -> tuple[str, str] | None:
        items = self.lists.get(names[0]) or []
        return (names[0], items.pop(0)) if items else None

    async def llen(self, name: str) -> int:
        return len(self.lists.get(name, []))

    async def delete(self, key: str) -> int:
        return 1 if self.kv.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        self.closed = True


async def test_redis_queue_roundtrip_and_dedupe() -> None:
    redis = FakeRedis()
    queue = RedisIncidentQueue(redis, "q", 30)  # type: ignore[arg-type]
    event = make_event()
    assert await queue.enqueue(event)
    assert not await queue.enqueue(make_event())
    assert await queue.size() == 1
    popped = await queue.dequeue(1)
    assert popped is not None and popped.id == event.id
    assert await queue.dequeue(1) is None
    await queue.release(event)
    assert await queue.enqueue(make_event())
    await queue.aclose()
    assert redis.closed


def test_build_queue_selects_backend() -> None:
    assert isinstance(build_queue(Settings(_env_file=None)), InMemoryIncidentQueue)  # type: ignore[call-arg]
    redis_queue = build_queue(Settings(_env_file=None, redis_url="redis://localhost:6390/0"))  # type: ignore[call-arg]
    assert isinstance(redis_queue, RedisIncidentQueue)
