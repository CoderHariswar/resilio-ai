"""Runtime configuration.

Every knob is an environment variable (or a line in ``.env``). Defaults boot the
platform in **Mock Mode**: no API keys, no external services, no surprises.

Provider-specific settings are flat (``DATADOG_API_KEY`` rather than nested
models) so they map 1:1 onto container env vars, Helm values and CI secrets.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CommaList = Annotated[list[str], NoDecode]


class RiskLevel(StrEnum):
    """Ordered risk tiers used by the policy engine."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(RiskLevel).index(self)


class Settings(BaseSettings):
    """Application settings loaded from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"  # noqa: S104 - container default; override in prod
    api_port: int = 8000
    public_url: str = "http://localhost:8000"
    api_token: SecretStr | None = Field(
        default=None, description="Bearer token required for approve/reject endpoints."
    )
    webhook_secret: SecretStr | None = Field(
        default=None, description="Shared secret for HMAC-SHA256 webhook signatures."
    )

    # ------------------------------------------------------------ providers
    telemetry_provider: str = "mock"
    remediation_provider: str = "mock"
    notifiers: CommaList = Field(default_factory=lambda: ["terminal"])
    llm_provider: str = "mock"

    # ------------------------------------------------------------------ llm
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = 60.0
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    mock_llm_fault_rate: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Fraction of incidents where the mock LLM's first answer hallucinates, "
        "so the evaluator's self-correction loop is visible in demos.",
    )

    # --------------------------------------------------------- infrastructure
    database_url: str | None = None
    database_pool_min: int = 1
    database_pool_max: int = 10
    redis_url: str | None = None
    queue_name: str = "resilio:incidents"

    # ------------------------------------------------------- workflow/policy
    telemetry_lookback_minutes: int = 15
    max_evaluator_retries: int = 2
    evaluator_min_score: float = 0.7
    auto_approve_max_risk: RiskLevel = RiskLevel.LOW
    min_auto_confidence: float = 0.8
    confidence_config_path: str | None = Field(
        default=None,
        description="Confidence weights and rules (YAML). Default: ./confidence.yaml if present, "
        "else built-in defaults (see confidence.example.yaml).",
    )
    critical_services: CommaList = Field(default_factory=list)
    health_check_timeout_seconds: int = 120
    health_check_interval_seconds: float = 5.0
    dedupe_window_seconds: int = 300
    worker_concurrency: int = 4
    similar_incidents_k: int = 3

    # ------------------------------------------------------ telemetry: datadog
    datadog_api_key: SecretStr | None = None
    datadog_app_key: SecretStr | None = None
    datadog_site: str = "datadoghq.com"
    datadog_error_rate_query: str = (
        "sum:trace.http.request.errors{{service:{service}}}.as_count() / "
        "sum:trace.http.request.hits{{service:{service}}}.as_count()"
    )

    # ------------------------------------------------------- telemetry: sentry
    sentry_auth_token: SecretStr | None = None
    sentry_org: str | None = None
    sentry_project: str | None = None
    sentry_base_url: str = "https://sentry.io"
    sentry_service_tag: str = "service"

    # --------------------------------------------------- telemetry: prometheus
    prometheus_url: str = "http://localhost:9090"
    prometheus_error_rate_query: str = (
        'sum(rate(http_requests_total{{service="{service}",code=~"5.."}}[5m]))'
        ' / clamp_min(sum(rate(http_requests_total{{service="{service}"}}[5m])), 1e-9)'
    )
    prometheus_error_rate_threshold: float = 0.05

    # ------------------------------------------------ telemetry: opentelemetry
    otel_query_url: str = "http://localhost:16686"
    otel_query_token: SecretStr | None = None

    # ------------------------------------------------- remediation: github
    github_token: SecretStr | None = None
    github_repository: str | None = Field(default=None, description="owner/repo")
    github_rollback_workflow: str = "rollback.yml"
    github_ref: str = "main"
    github_environment: str = "production"
    github_api_url: str = "https://api.github.com"

    # ------------------------------------------------- remediation: argocd
    argocd_url: str | None = None
    argocd_token: SecretStr | None = None
    argocd_app_template: str = "{service}"
    argocd_verify_tls: bool = True

    # --------------------------------------------- remediation: kubernetes
    k8s_api_url: str = "https://kubernetes.default.svc"
    k8s_token: SecretStr | None = None
    k8s_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105
    k8s_ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    k8s_namespace: str = "default"
    k8s_sha_label: str = "resilio.dev/git-sha"

    # --------------------------------------------------------- notifications
    slack_webhook_url: SecretStr | None = None
    pagerduty_routing_key: SecretStr | None = None
    teams_webhook_url: SecretStr | None = None

    @field_validator("notifiers", "critical_services", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_mock_mode(self) -> bool:
        """True when every pluggable dependency is a zero-dependency mock."""
        return (
            self.telemetry_provider == "mock"
            and self.remediation_provider == "mock"
            and self.llm_provider == "mock"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
