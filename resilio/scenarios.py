"""Synthetic outage library.

One source of truth for the mock telemetry provider, the mock remediation
provider, ``scripts/mock_outage_simulator.py`` and the offline benchmark.
Each template describes *what an outage looks like from the outside* (log
lines, error types) plus ground truth used only for scoring - the mock LLM
never sees the ``root_cause`` label; it has to infer it from evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from resilio.models import RemediationAction, Severity


@dataclass(frozen=True, slots=True)
class ScenarioTemplate:
    incident_type: str
    title: str
    severity: Severity
    root_cause: str
    recommended_action: RemediationAction
    deploy_related: bool
    error_messages: tuple[tuple[str, str], ...]
    deploy_message: str = "chore: routine dependency bumps"
    fixed_by: frozenset[RemediationAction] = field(default_factory=frozenset)
    description: str = ""


NOISE_MESSAGES: tuple[tuple[str, str], ...] = (
    ("Warning", "slow query detected on orders_by_customer took {ms}ms"),
    ("Warning", "retrying request to feature-flags (attempt {n})"),
    ("Info", "health probe /livez returned 200 in {ms}ms"),
)

SCENARIOS: dict[str, ScenarioTemplate] = {
    t.incident_type: t
    for t in (
        ScenarioTemplate(
            incident_type="memory-leak",
            title="Pods OOMKilled - memory climbing since last release",
            severity=Severity.CRITICAL,
            root_cause="memory_leak",
            recommended_action=RemediationAction.ROLLBACK,
            deploy_related=True,
            deploy_message="feat(catalog): warm in-process product cache on startup",
            error_messages=(
                ("OutOfMemoryError", "java.lang.OutOfMemoryError: Java heap space at CacheWarmer"),
                ("OOMKilled", "container {pod} OOMKilled: exceeded memory limit 512Mi (rss={n}Mi)"),
                ("GCOverhead", "GC overhead limit exceeded, heap usage {n}% after full GC"),
            ),
            fixed_by=frozenset({RemediationAction.ROLLBACK}),
            description="Unbounded cache introduced in the latest deploy exhausts heap.",
        ),
        ScenarioTemplate(
            incident_type="bad-deploy",
            title="Spike of 500s on /checkout after deploy",
            severity=Severity.HIGH,
            root_cause="code_regression",
            recommended_action=RemediationAction.ROLLBACK,
            deploy_related=True,
            deploy_message="refactor(pricing): inline discount calculation",
            error_messages=(
                ("TypeError", "TypeError: Cannot read properties of undefined (reading 'price')"),
                ("NullPointerException", "NullPointerException in PricingService.applyDiscount"),
                ("HTTP500", "POST /checkout returned 500 for order {id}"),
            ),
            fixed_by=frozenset({RemediationAction.ROLLBACK}),
            description="Regression in pricing refactor dereferences a missing field.",
        ),
        ScenarioTemplate(
            incident_type="db-connection-exhaustion",
            title="Database connection pool exhausted",
            severity=Severity.HIGH,
            root_cause="connection_pool_exhaustion",
            recommended_action=RemediationAction.RESTART,
            deploy_related=False,
            error_messages=(
                ("PoolExhausted", "connection pool exhausted (max=20), waited {ms}ms for a lease"),
                (
                    "OperationalError",
                    "FATAL: remaining connection slots are reserved for superuser connections",
                ),
                ("Timeout", "acquire connection timed out after {ms}ms"),
            ),
            fixed_by=frozenset({RemediationAction.RESTART}),
            description="Leaked connections pile up until the pool is exhausted.",
        ),
        ScenarioTemplate(
            incident_type="dependency-timeout",
            title="Upstream payments-api timing out",
            severity=Severity.HIGH,
            root_cause="dependency_failure",
            recommended_action=RemediationAction.NONE,
            deploy_related=False,
            error_messages=(
                ("UpstreamTimeout", "upstream payments-api timed out after {ms}ms"),
                ("CircuitOpen", "circuit breaker OPEN for payments-api after {n} failures"),
                ("HTTP504", "GET /v1/charges returned 504 Gateway Timeout"),
            ),
            description="A third-party dependency is degraded; nothing to roll back.",
        ),
        ScenarioTemplate(
            incident_type="traffic-spike",
            title="Latency SLO burn - request queue saturated",
            severity=Severity.HIGH,
            root_cause="capacity_saturation",
            recommended_action=RemediationAction.SCALE_UP,
            deploy_related=False,
            error_messages=(
                ("QueueSaturated", "request queue depth {n} exceeds threshold 500"),
                ("CPUThrottling", "CPU throttling detected in {n}% of CFS periods"),
                ("HTTP503", "503 Service Unavailable: upstream overloaded, shedding load"),
            ),
            fixed_by=frozenset({RemediationAction.SCALE_UP}),
            description="Organic traffic spike exceeds provisioned capacity.",
        ),
        ScenarioTemplate(
            incident_type="cert-expiry",
            title="TLS handshake failures to identity provider",
            severity=Severity.CRITICAL,
            root_cause="certificate_expiry",
            recommended_action=RemediationAction.NONE,
            deploy_related=False,
            error_messages=(
                ("x509Error", "x509: certificate has expired or is not yet valid"),
                ("SSLError", "SSL handshake failed with idp.internal: certificate expired"),
                ("AuthFailure", "token introspection failed for request {id}"),
            ),
            description="A certificate expired; needs a human to rotate it.",
        ),
        ScenarioTemplate(
            incident_type="config-drift",
            title="Service crash-looping on startup",
            severity=Severity.HIGH,
            root_cause="configuration_error",
            recommended_action=RemediationAction.ROLLBACK,
            deploy_related=True,
            deploy_message="chore(config): migrate env vars to new secret store layout",
            error_messages=(
                (
                    "KeyError",
                    "KeyError: 'PAYMENT_GATEWAY_URL' missing required environment variable",
                ),
                ("ConfigError", "invalid configuration: feature_flags.url must not be empty"),
                ("CrashLoopBackOff", "back-off restarting failed container {pod}"),
            ),
            fixed_by=frozenset({RemediationAction.ROLLBACK}),
            description="A config change shipped with the deploy removed a required variable.",
        ),
        ScenarioTemplate(
            incident_type="disk-full",
            title="Writes failing - no space left on device",
            severity=Severity.MEDIUM,
            root_cause="disk_exhaustion",
            recommended_action=RemediationAction.RESTART,
            deploy_related=False,
            error_messages=(
                ("ENOSPC", "write /var/log/app/app.log: no space left on device"),
                ("IOError", "IOError: [Errno 28] No space left on device"),
                ("EvictionWarning", "ephemeral-storage usage {n}% on pod {pod}"),
            ),
            fixed_by=frozenset({RemediationAction.RESTART}),
            description="Debug logging fills the ephemeral volume; restart reclaims it.",
        ),
        ScenarioTemplate(
            incident_type="rate-limit",
            title="Third-party API rate limiting",
            severity=Severity.MEDIUM,
            root_cause="rate_limited",
            recommended_action=RemediationAction.NONE,
            deploy_related=False,
            error_messages=(
                ("HTTP429", "429 Too Many Requests from api.stripe.com, retry after {n}s"),
                ("RateLimitError", "rate limit exceeded for key ending ...{n}"),
                ("RetryExhausted", "retries exhausted for webhook delivery {id}"),
            ),
            description="Provider quota exceeded; needs a quota bump or backoff change.",
        ),
        ScenarioTemplate(
            incident_type="deadlock",
            title="Order writes stalling on lock waits",
            severity=Severity.HIGH,
            root_cause="database_deadlock",
            recommended_action=RemediationAction.ROLLBACK,
            deploy_related=True,
            deploy_message="perf(orders): batch inventory updates in a single transaction",
            error_messages=(
                ("DeadlockDetected", "ERROR: deadlock detected while updating inventory rows"),
                (
                    "LockWaitTimeout",
                    "Lock wait timeout exceeded; try restarting transaction after {ms}ms",
                ),
                (
                    "TransactionRollback",
                    "transaction {id} rolled back due to serialization failure",
                ),
            ),
            fixed_by=frozenset({RemediationAction.ROLLBACK}),
            description="A new batched transaction acquires locks in conflicting order.",
        ),
    )
}

SCENARIO_TYPES: tuple[str, ...] = tuple(SCENARIOS)


def get_scenario(incident_type: str) -> ScenarioTemplate:
    """Return a template, raising ``KeyError`` with the valid choices."""
    try:
        return SCENARIOS[incident_type]
    except KeyError:
        raise KeyError(
            f"unknown incident type '{incident_type}'. Choose from: {', '.join(SCENARIO_TYPES)}"
        ) from None
