"""Anthropic Messages API provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.providers.base import BaseLLMProvider, LLMRequest, LLMResponse
from resilio.providers.http import ProviderError, build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings

ANTHROPIC_VERSION = "2023-06-01"
JSON_INSTRUCTION = "\n\nRespond with a single JSON object and nothing else."


class AnthropicProvider(BaseLLMProvider):
    name = "anthropic"
    default_model = "claude-sonnet-5-5"

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None = None,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model or self.default_model
        self._client = client or build_client(
            base_url.rstrip("/"),
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
            timeout=timeout,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        api_key = settings.llm_api_key or settings.anthropic_api_key
        if api_key is None:
            raise ValueError("AnthropicProvider requires ANTHROPIC_API_KEY (or LLM_API_KEY)")
        return cls(
            api_key=api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.llm_base_url or "https://api.anthropic.com",
            timeout=settings.llm_timeout_seconds,
        )

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        system = request.system + (JSON_INSTRUCTION if request.json_mode else "")
        return {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": system,
            "messages": [{"role": "user", "content": request.prompt}],
        }

    async def complete(self, request: LLMRequest) -> LLMResponse:
        data = await request_json(
            self._client,
            "POST",
            "/v1/messages",
            provider=self.name,
            json=self.build_payload(request),
        )
        try:
            text = "".join(
                block.get("text", "") for block in data["content"] if block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise ProviderError(self.name, f"unexpected response shape: {data!r:.300}") from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=str(data.get("model", self.model)),
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
