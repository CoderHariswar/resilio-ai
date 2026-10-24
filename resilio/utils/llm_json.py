"""Tolerant JSON extraction from LLM output."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMOutputError(ValueError):
    """The model did not return a parseable JSON object."""


def parse_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object in ``text``.

    Handles bare JSON, Markdown code fences, and prose before/after the object.
    """
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            if start == -1:
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    raise LLMOutputError(f"no JSON object found in model output: {text[:200]!r}")
