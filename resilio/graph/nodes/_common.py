"""Helpers shared by nodes: timing instrumentation and LLM calls."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

from resilio.core.metrics import LLM_TOKENS_TOTAL, NODE_DURATION_SECONDS
from resilio.graph.state import IncidentState
from resilio.providers.base import BaseLLMProvider, LLMRequest, LLMResponse
from resilio.utils.llm_json import parse_json_object

NodeFn = Callable[[IncidentState], Awaitable[dict[str, Any]]]


def instrumented(name: str) -> Callable[[NodeFn], NodeFn]:
    """Record ``resilio_node_duration_seconds{node=name}`` for a node."""

    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(state: IncidentState) -> dict[str, Any]:
            start = time.perf_counter()
            try:
                return await fn(state)
            finally:
                NODE_DURATION_SECONDS.labels(node=name).observe(time.perf_counter() - start)

        return wrapper

    return decorate


async def call_llm_json(
    llm: BaseLLMProvider, request: LLMRequest
) -> tuple[dict[str, Any], LLMResponse]:
    """Complete ``request``, account tokens, and parse a JSON object."""
    response = await llm.complete(request)
    LLM_TOKENS_TOTAL.labels(provider=llm.name, kind="prompt").inc(response.prompt_tokens)
    LLM_TOKENS_TOTAL.labels(provider=llm.name, kind="completion").inc(response.completion_tokens)
    return parse_json_object(response.text), response
