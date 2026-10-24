from __future__ import annotations

import pytest

from resilio.utils.log_sanitizer import (
    compact_traces,
    estimate_tokens,
    fingerprint,
    normalize_message,
    redact,
    redact_mapping,
)


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
        ("Authorization: Bearer abc.def.ghijklmnop", "abc.def.ghijklmnop"),
        ("contact jane.doe@example.com now", "jane.doe@example.com"),
        ("postgres://admin:hunter2@db:5432/app", "hunter2"),
        ("password=s3cr3t!& next", "s3cr3t!"),
        ('{"api_key": "sk-live-123456"}', "sk-live-123456"),
        ("token eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl", "eyJzdWIiOiIx"),
        ("client 10.42.7.19 refused", "10.42.7.19"),
        ("card 4111 1111 1111 1111 declined", "4111 1111 1111 1111"),
        ("slack xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
        ("gh ghp_" + "a" * 36, "ghp_" + "a" * 36),
        ("llm sk-ant-" + "b" * 30, "sk-ant-" + "b" * 30),
    ],
)
def test_redact_removes_secrets_and_pii(raw: str, leaked: str) -> None:
    cleaned = redact(raw)
    assert leaked not in cleaned
    assert "REDACTED" in cleaned


def test_redact_private_key_block() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert redact(f"oops {pem}") == "oops [REDACTED:private_key]"


def test_redact_leaves_ordinary_text_alone() -> None:
    text = "GET /checkout returned 500 for order"
    assert redact(text) == text


def test_redact_mapping_recurses() -> None:
    data = {"a": "mail bob@example.com", "b": [{"c": "AKIAIOSFODNN7EXAMPLE"}], "n": 3}
    out = redact_mapping(data)
    assert "bob@example.com" not in str(out)
    assert "AKIA" not in str(out)
    assert out["n"] == 3


def test_normalize_collapses_volatile_tokens() -> None:
    a = normalize_message("user 4812 timed out after 3001ms at 2026-01-01T10:00:00Z")
    b = normalize_message("user 77 timed out after 12ms at 2026-03-09T23:59:59Z")
    assert a == b
    assert "<n>" in a and "<ts>" in a


def test_normalize_uuid_hex_and_strings() -> None:
    msg = "order 123e4567-e89b-12d3-a456-426614174000 ptr 0xdeadbeef sha 9f86d081884c7d65 'x'"
    assert normalize_message(msg) == "order <uuid> ptr <hex> sha <hex> <str>"


def test_fingerprint_is_stable_and_type_sensitive() -> None:
    assert fingerprint("Timeout", "took 10ms") == fingerprint("Timeout", "took 99ms")
    assert fingerprint("Timeout", "x") != fingerprint("OOM", "x")
    assert len(fingerprint("a", "b")) == 12


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_compact_traces_groups_and_saves_tokens() -> None:
    traces = [
        {
            "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
            "message": f"connection pool exhausted, waited {1000 + i}ms",
            "error_type": "PoolExhausted",
            "trace_id": f"t{i}",
        }
        for i in range(50)
    ] + [{"timestamp": "2026-01-01T00:01:00+00:00", "message": "disk full", "level": "error"}]
    result = compact_traces(traces)
    assert [g.count for g in result.groups] == [50, 1]
    top = result.groups[0]
    assert top.first_seen == "2026-01-01T00:00:00+00:00"
    assert top.last_seen == "2026-01-01T00:00:49+00:00"
    assert top.trace_ids == ["t0", "t1", "t2"]
    assert result.groups[1].error_type == "error"
    assert result.savings_percent > 80


def test_compact_traces_redacts_samples_and_limits_groups() -> None:
    traces = [
        {"message": f"distinct failure kind {chr(97 + i)} for bob@example.com"} for i in range(26)
    ]
    result = compact_traces(traces, max_groups=5)
    assert len(result.groups) == 5
    assert all("bob@example.com" not in g.sample for g in result.groups)
    assert result.groups[0].as_dict()["fingerprint"] == result.groups[0].fingerprint


def test_compact_traces_empty() -> None:
    result = compact_traces([])
    assert result.groups == []
    assert result.savings_percent == 0.0
