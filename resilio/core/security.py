"""Webhook HMAC verification and secret scrubbing.

Signature scheme (compatible with GitHub-style ``sha256=<hex>`` headers)::

    X-Resilio-Timestamp: 1735689600              # optional, enables replay protection
    X-Resilio-Signature: sha256=<hex(HMAC_SHA256(secret, "<ts>." + body))>

Without a timestamp header the MAC is computed over the raw body only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from typing import Any

from resilio.utils.log_sanitizer import REDACTED, redact

SIGNATURE_HEADER = "X-Resilio-Signature"
TIMESTAMP_HEADER = "X-Resilio-Timestamp"

_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "private_key",
    "routing_key",
    "webhook_url",
)


class SignatureError(ValueError):
    """Raised when a webhook signature is missing, malformed, stale or wrong."""


def compute_signature(secret: str, body: bytes, timestamp: str | None = None) -> str:
    """Return the ``sha256=<hex>`` signature for ``body``."""
    message = f"{timestamp}.".encode() + body if timestamp else body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None = None,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> None:
    """Validate a webhook signature in constant time.

    Raises:
        SignatureError: if the signature is absent, malformed, stale or invalid.
    """
    if not signature:
        raise SignatureError("missing signature")
    if not signature.startswith("sha256="):
        raise SignatureError("unsupported signature scheme")
    if timestamp is not None:
        try:
            sent_at = int(timestamp)
        except ValueError as exc:
            raise SignatureError("malformed timestamp") from exc
        current = time.time() if now is None else now
        if abs(current - sent_at) > tolerance_seconds:
            raise SignatureError("stale timestamp")
    expected = compute_signature(secret, body, timestamp)
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("signature mismatch")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SENSITIVE_KEYS)


def scrub_secrets(data: Any) -> Any:
    """Recursively mask sensitive keys and redact secrets embedded in strings."""
    if isinstance(data, Mapping):
        return {k: REDACTED if _is_sensitive(str(k)) else scrub_secrets(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [scrub_secrets(v) for v in data]
    if isinstance(data, str):
        return redact(data)
    return data


class SecretScrubbingFilter(logging.Filter):
    """Logging filter that redacts secrets from every record before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record args
            return True
        record.msg = redact(message)
        record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    """Install a root handler whose output is always secret-scrubbed."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s - %(message)s"))
    handler.addFilter(SecretScrubbingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
