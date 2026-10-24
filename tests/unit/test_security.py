from __future__ import annotations

import logging

import pytest

from resilio.core.security import (
    SecretScrubbingFilter,
    SignatureError,
    compute_signature,
    configure_logging,
    scrub_secrets,
    verify_signature,
)

SECRET = "topsecret"
BODY = b'{"hello": "world"}'


def test_signature_roundtrip() -> None:
    verify_signature(SECRET, BODY, compute_signature(SECRET, BODY))


def test_signature_with_timestamp() -> None:
    sig = compute_signature(SECRET, BODY, "1000")
    verify_signature(SECRET, BODY, sig, "1000", now=1100)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature(SECRET, BODY, compute_signature(SECRET, BODY), "1000", now=1100)


@pytest.mark.parametrize(
    ("signature", "timestamp", "message"),
    [
        (None, None, "missing"),
        ("md5=abc", None, "unsupported"),
        ("sha256=deadbeef", None, "mismatch"),
        ("sha256=deadbeef", "not-a-number", "malformed"),
        ("sha256=deadbeef", "1", "stale"),
    ],
)
def test_signature_failures(signature: str | None, timestamp: str | None, message: str) -> None:
    with pytest.raises(SignatureError, match=message):
        verify_signature(SECRET, BODY, signature, timestamp, now=10_000)


def test_tampered_body_rejected() -> None:
    sig = compute_signature(SECRET, BODY)
    with pytest.raises(SignatureError):
        verify_signature(SECRET, BODY + b" ", sig)


def test_scrub_secrets_masks_keys_and_values() -> None:
    data = {
        "Authorization": "Bearer abc",
        "nested": {"db_password": "x", "note": "mail me at a@b.io"},
        "items": ["AKIAIOSFODNN7EXAMPLE", 5],
        "X-Api-Key": "k",
    }
    out = scrub_secrets(data)
    assert out["Authorization"] == "[REDACTED]"
    assert out["X-Api-Key"] == "[REDACTED]"
    assert out["nested"]["db_password"] == "[REDACTED]"
    assert "a@b.io" not in out["nested"]["note"]
    assert out["items"][1] == 5
    assert "AKIA" not in out["items"][0]


def test_logging_filter_redacts_formatted_message() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token=%s", ("abc123",), None)
    assert SecretScrubbingFilter().filter(record)
    assert record.getMessage() == "token=[REDACTED]"


def test_configure_logging_installs_filter() -> None:
    configure_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert any(isinstance(f, SecretScrubbingFilter) for f in root.handlers[0].filters)
    configure_logging("WARNING")
