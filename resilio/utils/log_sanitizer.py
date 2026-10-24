"""PII/secret redaction and trace fingerprinting.

Two jobs, both run before any telemetry reaches an LLM:

1. **Redaction** - strip credentials and PII so they never leave the cluster.
2. **Compaction** - collapse thousands of near-identical error lines into a
   handful of fingerprinted groups. This is where most of the token savings
   reported by ``resilio_token_savings_percent`` come from.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

REDACTED = "[REDACTED]"

# Order matters: specific credential formats first, broad PII last.
_REDACTION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        "[REDACTED:private_key]",
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws_key]"),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{40,})\b"),
        "[REDACTED:github_token]",
    ),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack_token]"),
    ("llm_api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"), "[REDACTED:api_key]"),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "[REDACTED:jwt]",
    ),
    ("bearer", re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (
        "dsn_credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
        r"\1[REDACTED]@",
    ),
    (
        "kv_secret",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"
            r"(\"?\s*[:=]\s*\"?)([^\s\"',;&]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED:email]",
    ),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED:card]"),
    (
        "ipv4",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        "[REDACTED:ip]",
    ),
)

# Normalization for fingerprinting: replace volatile tokens with placeholders
# so "user 4812 timed out after 3001ms" and "user 77 timed out after 2990ms"
# collapse into the same template.
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<ts>",
    ),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{12,}\b"), "<hex>"),
    (re.compile(r"\[REDACTED[^\]]*\]"), "<redacted>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|kb|mb|gb|mi|gi|b|%)?\b", re.IGNORECASE), "<n>"),
    (re.compile(r"\s+"), " "),
)


def redact(text: str) -> str:
    """Remove secrets and PII from ``text``."""
    for _, pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


def normalize_message(text: str) -> str:
    """Return a stable template for ``text`` suitable for grouping."""
    for pattern, replacement in _NORMALIZERS:
        text = pattern.sub(replacement, text)
    return text.strip()


def fingerprint(error_type: str, message: str) -> str:
    """Stable 12-char fingerprint of an error type + normalized message."""
    digest = hashlib.sha256(f"{error_type}|{normalize_message(message)}".encode()).hexdigest()
    return digest[:12]


def estimate_tokens(text: str) -> int:
    """Cheap, provider-agnostic token estimate (~4 chars per token)."""
    return math.ceil(len(text) / 4) if text else 0


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact every string inside a JSON-like mapping."""

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, Mapping):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [_walk(v) for v in value]
        return value

    return {k: _walk(v) for k, v in data.items()}


@dataclass(slots=True)
class TraceGroup:
    """All traces sharing one fingerprint."""

    fingerprint: str
    error_type: str
    template: str
    sample: str
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    trace_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "error_type": self.error_type,
            "template": self.template,
            "sample": self.sample,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "trace_ids": list(self.trace_ids),
        }


@dataclass(slots=True)
class CompactionResult:
    groups: list[TraceGroup]
    raw_tokens: int
    compact_tokens: int

    @property
    def savings_percent(self) -> float:
        if self.raw_tokens <= 0:
            return 0.0
        return round(max(0.0, 1 - self.compact_tokens / self.raw_tokens) * 100, 2)


def render_group(group: TraceGroup) -> str:
    """The exact text a group contributes to an LLM prompt."""
    return (
        f"[{group.fingerprint}] x{group.count} {group.error_type}: {group.sample} "
        f"(first={group.first_seen} last={group.last_seen})"
    )


def compact_traces(traces: Iterable[Mapping[str, Any]], max_groups: int = 20) -> CompactionResult:
    """Redact, fingerprint and group ``traces``; most frequent groups first."""
    groups: dict[str, TraceGroup] = {}
    raw_tokens = 0
    for trace in traces:
        message = str(trace.get("message", ""))
        error_type = str(trace.get("error_type") or trace.get("level") or "error")
        timestamp = str(trace.get("timestamp", ""))
        raw_tokens += estimate_tokens(
            f"{timestamp} {error_type} {message} {trace.get('trace_id', '')}"
        )
        clean = redact(message)
        fp = fingerprint(error_type, clean)
        group = groups.get(fp)
        if group is None:
            group = groups[fp] = TraceGroup(
                fingerprint=fp,
                error_type=error_type,
                template=normalize_message(clean),
                sample=clean[:500],
                first_seen=timestamp,
            )
        group.count += 1
        if timestamp:
            group.first_seen = min(group.first_seen or timestamp, timestamp)
            group.last_seen = max(group.last_seen, timestamp)
        trace_id = trace.get("trace_id")
        if trace_id and len(group.trace_ids) < 3:
            group.trace_ids.append(str(trace_id))

    ordered = sorted(groups.values(), key=lambda g: g.count, reverse=True)[:max_groups]
    compact_tokens = sum(estimate_tokens(render_group(g)) for g in ordered)
    return CompactionResult(groups=ordered, raw_tokens=raw_tokens, compact_tokens=compact_tokens)
