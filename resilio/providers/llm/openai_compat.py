"""OpenAI-compatible chat completions: OpenAI, DeepSeek, Ollama and vLLM.

All four speak ``POST {base_url}/chat/completions``. The subclasses only set
defaults, which keeps air-gapped deployments (Ollama/vLLM on a private
network) on exactly the same code path as the commercial APIs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

import httpx

from resilio.providers.base import BaseLLMProvider, LLMRequest, LLMResponse
from resilio.providers.http import ProviderError, build_client, request_json

if TYPE_CHECKING:
    from resilio.config import Settings


class OpenAICompatibleProvider(BaseLLMProvider):
    """Generic client for any ``/v1/chat/completions`` endpoint."""

    name = "openai-compatible"
    default_base_url: ClassVar[str] = "http://localhost:8000/v1"
    default_model: ClassVar[str | None] = None
    api_key_setting: ClassVar[str | None] = None
    requires_api_key: ClassVar[bool] = False
    supports_json_mode: ClassVar[bool] = True
    max_tokens_field: ClassVar[str] = "max_tokens"

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or build_client(
            (base_url or self.default_base_url).rstrip("/"), headers=headers, timeout=timeout
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        api_key = settings.llm_api_key
        if api_key is None and cls.api_key_setting:
            api_key = getattr(settings, cls.api_key_setting)
        if cls.requires_api_key and api_key is None:
            env = (cls.api_key_setting or "llm_api_key").upper()
            raise ValueError(f"{cls.__name__} requires {env} (or LLM_API_KEY)")
        model = settings.llm_model or cls.default_model
        if not model:
            raise ValueError(f"{cls.__name__} requires LLM_MODEL")
        return cls(
            model=model,
            base_url=settings.llm_base_url,
            api_key=api_key.get_secret_value() if api_key else None,
            timeout=settings.llm_timeout_seconds,
        )

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
            self.max_tokens_field: request.max_tokens,
        }
        if request.json_mode and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        data = await request_json(
            self._client,
            "POST",
            "/chat/completions",
            provider=self.name,
            json=self.build_payload(request),
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, f"unexpected response shape: {data!r:.300}") from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=str(data.get("model", self.model)),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    async def healthcheck(self) -> bool:
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    api_key_setting = "openai_api_key"
    requires_api_key = True
    max_tokens_field = "max_completion_tokens"


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"
    default_base_url = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"
    api_key_setting = "deepseek_api_key"
    requires_api_key = True


class OllamaProvider(OpenAICompatibleProvider):
    """Local, air-gapped models served by Ollama (``ollama serve``)."""

    name = "ollama"
    default_base_url = "http://localhost:11434/v1"
    default_model = "llama3.1:8b"


class VLLMProvider(OpenAICompatibleProvider):
    """Self-hosted models served by ``vllm serve <model>``. Set LLM_MODEL."""

    name = "vllm"
    default_base_url = "http://localhost:8001/v1"
