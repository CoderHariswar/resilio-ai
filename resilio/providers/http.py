"""Shared HTTP plumbing for providers: one client factory, one retry policy."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class ProviderError(RuntimeError):
    """Raised when an external system returns an unrecoverable error."""

    def __init__(self, provider: str, message: str, status_code: int | None = None) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status_code = status_code


def build_client(
    base_url: str = "",
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    verify: bool | str = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` with sane defaults for provider calls."""
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"User-Agent": "resilio-ai/0.1", **(headers or {})},
        timeout=httpx.Timeout(timeout),
        verify=verify,
        transport=transport,
    )


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    retries: int = 2,
    backoff_seconds: float = 0.5,
    **kwargs: Any,
) -> Any:
    """Send a request and decode JSON, retrying transient failures.

    Retries on network errors and on :data:`RETRYABLE_STATUS` with exponential
    backoff. Other 4xx/5xx responses raise :class:`ProviderError` immediately.
    Empty bodies (e.g. ``204 No Content``) decode to ``None``.
    """
    attempt = 0
    while True:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= retries:
                raise ProviderError(provider, f"{method} {url} failed: {exc}") from exc
        else:
            if response.status_code < 400:
                return response.json() if response.content else None
            if response.status_code not in RETRYABLE_STATUS or attempt >= retries:
                raise ProviderError(
                    provider,
                    f"{method} {url} -> HTTP {response.status_code}: {response.text[:300]}",
                    status_code=response.status_code,
                )
        delay = backoff_seconds * (2**attempt)
        logger.debug("%s: retrying %s %s in %.2fs", provider, method, url, delay)
        await asyncio.sleep(delay)
        attempt += 1
