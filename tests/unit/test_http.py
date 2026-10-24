from __future__ import annotations

import httpx
import pytest

from resilio.providers.http import ProviderError, build_client, request_json
from tests.helpers import mock_client


async def test_retries_transient_status_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    async with mock_client(handler) as client:
        data = await request_json(client, "GET", "/x", provider="t", backoff_seconds=0)
    assert data == {"ok": True}
    assert calls["n"] == 3


async def test_non_retryable_error_raises_immediately() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    async with mock_client(handler) as client:
        with pytest.raises(ProviderError) as info:
            await request_json(client, "GET", "/x", provider="t", backoff_seconds=0)
    assert info.value.status_code == 403
    assert "forbidden" in str(info.value)
    assert calls["n"] == 1


async def test_retries_exhausted_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with mock_client(handler) as client:
        with pytest.raises(ProviderError, match="down"):
            await request_json(client, "GET", "/x", provider="t", retries=1, backoff_seconds=0)


async def test_empty_body_decodes_to_none() -> None:
    async with mock_client(lambda r: httpx.Response(204)) as client:
        assert await request_json(client, "POST", "/x", provider="t") is None


async def test_build_client_sets_user_agent() -> None:
    client = build_client("https://example.com", headers={"X": "1"})
    assert client.headers["User-Agent"].startswith("resilio-ai/")
    assert client.headers["X"] == "1"
    await client.aclose()
