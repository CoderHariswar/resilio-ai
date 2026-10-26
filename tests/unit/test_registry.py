from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from resilio.config import RiskLevel, Settings
from resilio.providers import BaseTelemetryProvider, register_telemetry, registry
from resilio.providers.notifications.composite import CompositeNotifier
from resilio.providers.notifications.terminal import TerminalNotifier
from resilio.providers.registry import (
    UnknownProviderError,
    build_confidence_scorer,
    build_llm_provider,
    build_notifier,
    build_remediation_provider,
    build_telemetry_provider,
    register_confidence_scorer,
    register_llm,
    register_notifier,
    register_remediation,
)
from resilio.providers.remediation.mock import MockRemediationProvider
from resilio.providers.telemetry.datadog import DatadogTelemetryProvider
from resilio.providers.telemetry.mock import MockTelemetryProvider
from resilio.scoring.confidence import (
    BaseConfidenceScorer,
    ConfidenceResult,
    ConfidenceSignals,
    WeightedRuleScorer,
)


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_defaults_are_mock_mode() -> None:
    settings = _settings()
    assert settings.is_mock_mode
    assert settings.notifiers == ["terminal"]
    assert settings.auto_approve_max_risk == RiskLevel.LOW


def test_csv_settings_are_split() -> None:
    settings = _settings(notifiers="terminal, slack ,", critical_services="a,b")
    assert settings.notifiers == ["terminal", "slack"]
    assert settings.critical_services == ["a", "b"]


def test_csv_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFIERS", "terminal,pagerduty")
    monkeypatch.setenv("TELEMETRY_PROVIDER", "datadog")
    settings = _settings()
    assert settings.notifiers == ["terminal", "pagerduty"]
    assert settings.telemetry_provider == "datadog"
    assert not settings.is_mock_mode


def test_risk_level_rank_is_ordered() -> None:
    ranks = [level.rank for level in RiskLevel]
    assert ranks == sorted(ranks)


def test_factories_build_mock_stack() -> None:
    settings = _settings()
    telemetry = build_telemetry_provider(settings)
    assert isinstance(telemetry, MockTelemetryProvider)
    remediation = build_remediation_provider(settings, telemetry)
    assert isinstance(remediation, MockRemediationProvider)
    assert remediation.telemetry is telemetry
    notifier = build_notifier(settings)
    assert isinstance(notifier, CompositeNotifier)
    assert isinstance(notifier.notifiers[0], TerminalNotifier)
    assert build_llm_provider(settings).name == "mock"


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("telemetry", "opentelemetry"),
        ("telemetry", "otel"),
        ("telemetry", "sentry"),
        ("telemetry", "prometheus"),
        ("remediation", "github_actions"),
        ("remediation", "argocd"),
        ("remediation", "k8s"),
        ("notifiers", "slack"),
        ("notifiers", "pagerduty"),
        ("notifiers", "msteams"),
        ("llm", "openai"),
        ("llm", "anthropic"),
        ("llm", "deepseek"),
        ("llm", "ollama"),
        ("llm", "vllm"),
    ],
)
def test_builtins_resolve(kind: Any, name: str) -> None:
    assert registry.resolve(kind, name.upper()).name


def test_unknown_provider_lists_choices() -> None:
    with pytest.raises(UnknownProviderError, match="mock"):
        registry.resolve("telemetry", "nope")


def test_provider_requiring_credentials_fails_fast() -> None:
    with pytest.raises(ValueError, match="DATADOG_API_KEY"):
        build_telemetry_provider(_settings(telemetry_provider="datadog"))


def test_provider_from_settings_with_credentials() -> None:
    provider = build_telemetry_provider(
        _settings(
            telemetry_provider="datadog",
            datadog_api_key=SecretStr("a"),
            datadog_app_key=SecretStr("b"),
        )
    )
    assert isinstance(provider, DatadogTelemetryProvider)


def test_runtime_registration_and_type_check() -> None:
    @register_telemetry("unit-test-provider")
    class Custom(BaseTelemetryProvider):
        name = "unit-test-provider"

        async def fetch_traces(
            self, service_name: str, lookback_minutes: int
        ) -> list[dict[str, Any]]:
            return []

    try:
        assert registry.resolve("telemetry", "Unit-Test-Provider") is Custom
        assert "unit-test-provider" in registry.available("telemetry")
        settings = _settings(telemetry_provider="unit-test-provider")
        assert isinstance(build_telemetry_provider(settings), Custom)
    finally:
        registry.unregister("telemetry", "unit-test-provider")

    with pytest.raises(TypeError, match="BaseTelemetryProvider"):
        registry.register("telemetry", "bad", dict)


def test_custom_confidence_scorer_is_picked_by_name(tmp_path: Path) -> None:
    @register_confidence_scorer("unit-test-scorer")
    class Constant(BaseConfidenceScorer):
        name = "unit-test-scorer"

        def score(self, signals: ConfidenceSignals) -> ConfidenceResult:
            return ConfidenceResult(value=0.5, breakdown=())

    config = tmp_path / "confidence.yaml"
    config.write_text("confidence:\n  scorer: unit-test-scorer\n")
    try:
        assert "unit-test-scorer" in registry.available("confidence")
        scorer = build_confidence_scorer(_settings(confidence_config_path=str(config)))
        assert isinstance(scorer, Constant)
    finally:
        registry.unregister("confidence", "unit-test-scorer")


def test_confidence_scorer_defaults_and_unknown_name(tmp_path: Path) -> None:
    default = build_confidence_scorer(_settings())
    assert isinstance(default, WeightedRuleScorer)
    config = tmp_path / "confidence.yaml"
    config.write_text("confidence:\n  scorer: nope\n")
    with pytest.raises(UnknownProviderError, match="weighted_rules"):
        build_confidence_scorer(_settings(confidence_config_path=str(config)))


def test_other_decorators_validate_base_class() -> None:
    for decorator in (
        register_remediation,
        register_notifier,
        register_llm,
        register_confidence_scorer,
    ):
        with pytest.raises(TypeError):
            decorator("bad")(dict)
