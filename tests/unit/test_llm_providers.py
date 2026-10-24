from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from resilio.config import Settings
from resilio.providers.base import LLMRequest
from resilio.providers.http import ProviderError
from resilio.providers.llm.anthropic import AnthropicProvider
from resilio.providers.llm.mock import FABRICATED_FINGERPRINT, FABRICATED_SHA, MockLLMProvider
from resilio.providers.llm.openai_compat import (
    DeepSeekProvider,
    OllamaProvider,
    OpenAIProvider,
    VLLMProvider,
)
from resilio.providers.llm.rules import score_groups
from resilio.utils.llm_json import LLMOutputError, parse_json_object
from tests.helpers import mock_client

REQUEST = LLMRequest(system="sys", prompt="hello", max_tokens=64)


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------ json parser
@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        'Sure! ```json\n{"a": 1}\n``` hope that helps',
        'prefix {"a": 1} suffix',
        '```\n{"a": 1}\n```',
    ],
)
def test_parse_json_object_variants(text: str) -> None:
    assert parse_json_object(text) == {"a": 1}


@pytest.mark.parametrize("text", ["no json here", "[1, 2]", "{broken"])
def test_parse_json_object_rejects(text: str) -> None:
    with pytest.raises(LLMOutputError):
        parse_json_object(text)


# ------------------------------------------------------- openai-compatible
async def test_openai_payload_and_parsing() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-x",
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    provider = OpenAIProvider(model="gpt-x", client=mock_client(handler))
    response = await provider.complete(REQUEST)
    assert response.text == '{"ok": true}'
    assert (response.prompt_tokens, response.completion_tokens) == (11, 3)
    body = captured[0]
    assert body["max_completion_tokens"] == 64
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": "sys"}  # type: ignore[index]


async def test_ollama_uses_max_tokens_and_no_auth() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = OllamaProvider(model="llama3.1:8b", client=mock_client(handler))
    await provider.complete(LLMRequest(system="s", prompt="p", json_mode=False))
    body = json.loads(captured[0].content)
    assert body["max_tokens"] == 1024
    assert "response_format" not in body
    assert "Authorization" not in captured[0].headers


async def test_openai_compatible_bad_shape_raises() -> None:
    provider = VLLMProvider(
        model="m", client=mock_client(lambda r: httpx.Response(200, json={"oops": 1}))
    )
    with pytest.raises(ProviderError, match="unexpected response"):
        await provider.complete(REQUEST)


async def test_openai_compatible_healthcheck() -> None:
    ok = OllamaProvider(model="m", client=mock_client(lambda r: httpx.Response(200)))
    assert await ok.healthcheck()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x", request=request)

    assert not await OllamaProvider(model="m", client=mock_client(down)).healthcheck()


def test_from_settings_key_and_model_resolution() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIProvider.from_settings(_settings())
    provider = OpenAIProvider.from_settings(_settings(openai_api_key=SecretStr("k")))
    assert provider.model == "gpt-4o-mini"
    deepseek = DeepSeekProvider.from_settings(_settings(llm_api_key=SecretStr("k")))
    assert deepseek.model == "deepseek-chat"
    ollama = OllamaProvider.from_settings(_settings(llm_model="qwen2.5:14b"))
    assert ollama.model == "qwen2.5:14b"
    with pytest.raises(ValueError, match="LLM_MODEL"):
        VLLMProvider.from_settings(_settings())


# --------------------------------------------------------------- anthropic
async def test_anthropic_messages_api() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-5-5",
                "content": [
                    {"type": "text", "text": '{"a":'},
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": " 1}"},
                ],
                "usage": {"input_tokens": 20, "output_tokens": 5},
            },
        )

    provider = AnthropicProvider(api_key="k", client=mock_client(handler))
    response = await provider.complete(REQUEST)
    assert response.text == '{"a": 1}'
    assert response.prompt_tokens == 20
    body = json.loads(captured[0].content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"].startswith("sys") and "JSON" in body["system"]
    assert body["messages"] == [{"role": "user", "content": "hello"}]


async def test_anthropic_errors_and_settings() -> None:
    bad = AnthropicProvider(api_key="k", client=mock_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ProviderError):
        await bad.complete(REQUEST)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider.from_settings(_settings())
    provider = AnthropicProvider.from_settings(
        _settings(anthropic_api_key=SecretStr("k"), llm_model="claude-opus-5-5")
    )
    assert provider.model == "claude-opus-5-5"
    await provider.aclose()


# -------------------------------------------------------------------- mock
GROUPS = [
    {
        "fingerprint": "aaaaaaaaaaaa",
        "error_type": "OOMKilled",
        "sample": "OOMKilled pod",
        "count": 40,
    },
    {"fingerprint": "bbbbbbbbbbbb", "error_type": "Warning", "sample": "slow query", "count": 5},
]
DEPLOYS = [
    {"sha": "1" * 40, "deployed_at": "2026-01-01T00:00:00+00:00", "message": "cache"},
    {"sha": "2" * 40, "deployed_at": "2025-12-30T00:00:00+00:00", "message": "older"},
]


def _synth_request(**context: object) -> LLMRequest:
    base: dict[str, object] = {
        "incident_id": "inc",
        "trace_groups": GROUPS,
        "deployments": DEPLOYS,
        "suspect_deployment": DEPLOYS[0],
        "rollback_target": DEPLOYS[1]["sha"],
        "attempt": 0,
    }
    base.update(context)
    return LLMRequest(system="", prompt="", task="synthesize", context=base)


async def test_mock_synthesizes_grounded_rollback() -> None:
    response = await MockLLMProvider().complete(_synth_request())
    rca = json.loads(response.text)
    assert rca["root_cause"] == "memory_leak"
    assert rca["action"] == "rollback"
    assert rca["target_sha"] == "2" * 40
    assert rca["evidence"] == ["aaaaaaaaaaaa"]
    assert rca["confidence"] > 0.8
    assert response.prompt_tokens >= 0


async def test_mock_refuses_rollback_without_correlated_deploy() -> None:
    response = await MockLLMProvider().complete(
        _synth_request(suspect_deployment=None, rollback_target=None)
    )
    rca = json.loads(response.text)
    assert rca["action"] == "none"
    assert "unsafe" in rca["reasoning"]


async def test_mock_unknown_evidence() -> None:
    groups = [{"fingerprint": "cccccccccccc", "error_type": "X", "sample": "weird", "count": 1}]
    rca = json.loads((await MockLLMProvider().complete(_synth_request(trace_groups=groups))).text)
    assert rca["root_cause"] == "unknown"
    assert rca["action"] == "none"


async def test_mock_uses_similar_incidents() -> None:
    similar = [{"incident_id": "old", "similarity": 0.93, "root_cause": "memory_leak"}]
    rca = json.loads(
        (await MockLLMProvider().complete(_synth_request(similar_incidents=similar))).text
    )
    assert "old" in rca["reasoning"]


async def test_mock_fault_injection_only_on_first_attempt() -> None:
    llm = MockLLMProvider(fault_rate=1.0)
    first = json.loads((await llm.complete(_synth_request())).text)
    assert first["target_sha"] == FABRICATED_SHA
    retry = json.loads((await llm.complete(_synth_request(attempt=1))).text)
    assert retry["target_sha"] == "2" * 40

    restart_groups = [
        {
            "fingerprint": "dddddddddddd",
            "error_type": "ENOSPC",
            "sample": "no space left",
            "count": 9,
        }
    ]
    faulty = json.loads((await llm.complete(_synth_request(trace_groups=restart_groups))).text)
    assert FABRICATED_FINGERPRINT in faulty["evidence"]


async def test_mock_judge_flags_misattribution_and_bad_action() -> None:
    def judge(hypothesis: dict[str, object], issues: list[str] | None = None) -> dict[str, object]:
        return {
            "hypothesis": hypothesis,
            "trace_groups": GROUPS,
            "grounding_issues": issues or [],
        }

    llm = MockLLMProvider()
    good = {"root_cause": "memory_leak", "evidence": ["aaaaaaaaaaaa"], "action": "rollback"}
    verdict = json.loads(
        (
            await llm.complete(
                LLMRequest(system="", prompt="", task="evaluate", context=judge(good))
            )
        ).text
    )
    assert verdict["verdict"] == "accept"

    wrong = {"root_cause": "disk_exhaustion", "evidence": ["aaaaaaaaaaaa"], "action": "scale_up"}
    verdict = json.loads(
        (
            await llm.complete(
                LLMRequest(system="", prompt="", task="evaluate", context=judge(wrong))
            )
        ).text
    )
    assert verdict["verdict"] == "reject"
    assert any("indicates memory_leak" in i for i in verdict["issues"])

    bad_action = {"root_cause": "memory_leak", "evidence": ["aaaaaaaaaaaa"], "action": "scale_up"}
    verdict = json.loads(
        (
            await llm.complete(
                LLMRequest(system="", prompt="", task="evaluate", context=judge(bad_action, ["x"]))
            )
        ).text
    )
    assert any("does not address" in i for i in verdict["issues"])
    assert verdict["score"] < 0.5

    unsupported = {"root_cause": "memory_leak", "evidence": ["bbbbbbbbbbbb"], "action": "invalid"}
    ctx = judge(unsupported)
    ctx["trace_groups"] = [GROUPS[1]]
    verdict = json.loads(
        (await llm.complete(LLMRequest(system="", prompt="", task="evaluate", context=ctx))).text
    )
    assert any("no cited evidence supports" in i for i in verdict["issues"])


async def test_mock_generic_task() -> None:
    response = await MockLLMProvider().complete(LLMRequest(system="", prompt="", task="chat"))
    assert json.loads(response.text) == {"text": "mock response"}


def test_rules_rank_by_trace_volume() -> None:
    matches = score_groups(
        [
            {
                "fingerprint": "a",
                "error_type": "HTTP429",
                "sample": "429 Too Many Requests",
                "count": 3,
            },
            {"fingerprint": "b", "error_type": "x", "sample": "deadlock detected", "count": 30},
        ]
    )
    assert [m.rule.root_cause for m in matches] == ["database_deadlock", "rate_limited"]
    assert matches[0].fingerprints == ("b",)
