"""Prometheus self-observability metrics for the agent itself.

Exposed on ``GET /metrics``. The four headline series:

* ``resilio_incidents_total{status="resolved|escalated|failed"}``
* ``resilio_token_savings_percent``
* ``resilio_evaluator_corrections_total``
* ``resilio_resolution_duration_seconds{status=...}``
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

INCIDENTS_TOTAL = Counter(
    "resilio_incidents_total",
    "Incidents that reached a terminal state, by outcome.",
    ["status"],
)
TOKEN_SAVINGS_PERCENT = Gauge(
    "resilio_token_savings_percent",
    "Prompt-token reduction from trace fingerprinting on the most recent incident (0-100).",
)
EVALUATOR_CORRECTIONS_TOTAL = Counter(
    "resilio_evaluator_corrections_total",
    "RCA hypotheses rejected by the LLM-as-a-judge and sent back for revision.",
)
RESOLUTION_DURATION_SECONDS = Histogram(
    "resilio_resolution_duration_seconds",
    "Wall-clock time from alert ingestion to terminal state.",
    ["status"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)

INCIDENTS_RECEIVED_TOTAL = Counter(
    "resilio_incidents_received_total", "Alerts accepted for processing.", ["source"]
)
INCIDENTS_DEDUPLICATED_TOTAL = Counter(
    "resilio_incidents_deduplicated_total", "Alerts dropped as duplicates of an open incident."
)
HITL_PENDING = Gauge(
    "resilio_hitl_pending", "Incidents currently paused at the human approval gate."
)
LLM_TOKENS_TOTAL = Counter("resilio_llm_tokens_total", "LLM tokens consumed.", ["provider", "kind"])
NODE_DURATION_SECONDS = Histogram(
    "resilio_node_duration_seconds",
    "Execution time of each LangGraph node.",
    ["node"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
CONFIDENCE_SCORE = Histogram(
    "resilio_confidence_score",
    "Rule-based confidence of accepted hypotheses (what the policy acts on).",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
LLM_CONFIDENCE_DELTA = Histogram(
    "resilio_llm_confidence_delta",
    "Rule-based confidence minus the LLM's self-reported confidence.",
    buckets=(-0.75, -0.5, -0.25, -0.1, 0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
)
PROVIDER_ERRORS_TOTAL = Counter(
    "resilio_provider_errors_total", "Errors raised by external providers.", ["kind", "provider"]
)

for _status in ("resolved", "escalated", "failed"):
    INCIDENTS_TOTAL.labels(status=_status)
    RESOLUTION_DURATION_SECONDS.labels(status=_status)


def record_terminal(status: str, duration_seconds: float) -> None:
    """Record an incident reaching ``status`` after ``duration_seconds``."""
    INCIDENTS_TOTAL.labels(status=status).inc()
    RESOLUTION_DURATION_SECONDS.labels(status=status).observe(max(duration_seconds, 0.0))


def render_metrics(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    """Return ``(body, content_type)`` for the ``/metrics`` endpoint."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
