"""Provider registry and factories.

Providers are resolved by name from three places, in order:

1. Runtime registrations (``@register_telemetry("newrelic")``).
2. Built-ins, imported lazily so unused integrations cost nothing.
3. Installed plugins exposing entry points in the ``resilio.<kind>`` group.

Factories read the active names from :class:`~resilio.config.Settings`, e.g.
``TELEMETRY_PROVIDER=datadog`` -> :class:`DatadogTelemetryProvider`.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Literal, TypeVar

from resilio.providers.base import (
    BaseLLMProvider,
    BaseNotifier,
    BaseRemediationProvider,
    BaseTelemetryProvider,
)
from resilio.scoring.confidence import BaseConfidenceScorer, load_confidence_config

if TYPE_CHECKING:
    from resilio.config import Settings

logger = logging.getLogger(__name__)

ProviderKind = Literal["telemetry", "remediation", "notifiers", "llm", "confidence"]
T = TypeVar("T")

_BASES: dict[ProviderKind, type] = {
    "telemetry": BaseTelemetryProvider,
    "remediation": BaseRemediationProvider,
    "notifiers": BaseNotifier,
    "llm": BaseLLMProvider,
    "confidence": BaseConfidenceScorer,
}

_BUILTINS: dict[ProviderKind, dict[str, str]] = {
    "telemetry": {
        "mock": "resilio.providers.telemetry.mock:MockTelemetryProvider",
        "opentelemetry": "resilio.providers.telemetry.opentelemetry:OpenTelemetryProvider",
        "otel": "resilio.providers.telemetry.opentelemetry:OpenTelemetryProvider",
        "datadog": "resilio.providers.telemetry.datadog:DatadogTelemetryProvider",
        "sentry": "resilio.providers.telemetry.sentry:SentryTelemetryProvider",
        "prometheus": "resilio.providers.telemetry.prometheus:PrometheusTelemetryProvider",
    },
    "remediation": {
        "mock": "resilio.providers.remediation.mock:MockRemediationProvider",
        "github": "resilio.providers.remediation.github_actions:GitHubActionsRemediationProvider",
        "github_actions": (
            "resilio.providers.remediation.github_actions:GitHubActionsRemediationProvider"
        ),
        "argocd": "resilio.providers.remediation.argocd:ArgoCDRemediationProvider",
        "kubernetes": "resilio.providers.remediation.kubernetes:KubernetesRemediationProvider",
        "k8s": "resilio.providers.remediation.kubernetes:KubernetesRemediationProvider",
    },
    "notifiers": {
        "terminal": "resilio.providers.notifications.terminal:TerminalNotifier",
        "mock": "resilio.providers.notifications.mock:MockNotifier",
        "slack": "resilio.providers.notifications.slack:SlackNotifier",
        "pagerduty": "resilio.providers.notifications.pagerduty:PagerDutyNotifier",
        "teams": "resilio.providers.notifications.teams:TeamsNotifier",
        "msteams": "resilio.providers.notifications.teams:TeamsNotifier",
    },
    "llm": {
        "mock": "resilio.providers.llm.mock:MockLLMProvider",
        "openai": "resilio.providers.llm.openai_compat:OpenAIProvider",
        "deepseek": "resilio.providers.llm.openai_compat:DeepSeekProvider",
        "ollama": "resilio.providers.llm.openai_compat:OllamaProvider",
        "vllm": "resilio.providers.llm.openai_compat:VLLMProvider",
        "anthropic": "resilio.providers.llm.anthropic:AnthropicProvider",
    },
    "confidence": {
        "weighted_rules": "resilio.scoring.confidence:WeightedRuleScorer",
    },
}


class UnknownProviderError(LookupError):
    """Raised when a provider name cannot be resolved."""


class ProviderRegistry:
    """Name -> class lookup for every provider kind."""

    def __init__(self) -> None:
        self._runtime: dict[ProviderKind, dict[str, type]] = {kind: {} for kind in _BASES}

    def register(self, kind: ProviderKind, name: str, cls: type) -> None:
        base = _BASES[kind]
        if not issubclass(cls, base):
            raise TypeError(f"{cls.__name__} must subclass {base.__name__}")
        self._runtime[kind][name.lower()] = cls

    def unregister(self, kind: ProviderKind, name: str) -> None:
        self._runtime[kind].pop(name.lower(), None)

    def resolve(self, kind: ProviderKind, name: str) -> type:
        key = name.lower().strip()
        if key in self._runtime[kind]:
            return self._runtime[kind][key]
        if key in _BUILTINS[kind]:
            return _import(_BUILTINS[kind][key])
        for ep in entry_points(group=f"resilio.{kind}"):
            if ep.name.lower() == key:
                cls = ep.load()
                self.register(kind, key, cls)
                return cls  # type: ignore[no-any-return]
        raise UnknownProviderError(
            f"Unknown {kind} provider '{name}'. Available: {', '.join(self.available(kind))}"
        )

    def available(self, kind: ProviderKind) -> list[str]:
        names = set(_BUILTINS[kind]) | set(self._runtime[kind])
        names |= {ep.name for ep in entry_points(group=f"resilio.{kind}")}
        return sorted(names)


def _import(path: str) -> type:
    module_name, _, attr = path.partition(":")
    return getattr(importlib.import_module(module_name), attr)  # type: ignore[no-any-return]


registry = ProviderRegistry()


def _decorator(kind: ProviderKind, name: str) -> Callable[[type[T]], type[T]]:
    def wrap(cls: type[T]) -> type[T]:
        registry.register(kind, name, cls)
        return cls

    return wrap


def register_telemetry(name: str) -> Callable[[type[T]], type[T]]:
    """Class decorator registering a :class:`BaseTelemetryProvider`."""
    return _decorator("telemetry", name)


def register_remediation(name: str) -> Callable[[type[T]], type[T]]:
    """Class decorator registering a :class:`BaseRemediationProvider`."""
    return _decorator("remediation", name)


def register_notifier(name: str) -> Callable[[type[T]], type[T]]:
    """Class decorator registering a :class:`BaseNotifier`."""
    return _decorator("notifiers", name)


def register_llm(name: str) -> Callable[[type[T]], type[T]]:
    """Class decorator registering a :class:`BaseLLMProvider`."""
    return _decorator("llm", name)


def register_confidence_scorer(name: str) -> Callable[[type[T]], type[T]]:
    """Class decorator registering a :class:`BaseConfidenceScorer`."""
    return _decorator("confidence", name)


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
def build_telemetry_provider(settings: Settings) -> BaseTelemetryProvider:
    cls: type[BaseTelemetryProvider] = registry.resolve("telemetry", settings.telemetry_provider)
    logger.info("telemetry provider: %s", cls.__name__)
    return cls.from_settings(settings)


def build_remediation_provider(
    settings: Settings, telemetry: BaseTelemetryProvider | None = None
) -> BaseRemediationProvider:
    cls: type[BaseRemediationProvider] = registry.resolve(
        "remediation", settings.remediation_provider
    )
    logger.info("remediation provider: %s", cls.__name__)
    return cls.from_settings(settings, telemetry=telemetry)


def build_notifier(settings: Settings) -> BaseNotifier:
    """Build every configured notifier and fan out through one composite."""
    from resilio.providers.notifications.composite import CompositeNotifier

    notifiers: list[BaseNotifier] = []
    for name in settings.notifiers or ["terminal"]:
        cls: type[BaseNotifier] = registry.resolve("notifiers", name)
        notifiers.append(cls.from_settings(settings))
    logger.info("notifiers: %s", ", ".join(type(n).__name__ for n in notifiers))
    return CompositeNotifier(notifiers)


def build_llm_provider(settings: Settings) -> BaseLLMProvider:
    cls: type[BaseLLMProvider] = registry.resolve("llm", settings.llm_provider)
    logger.info("llm provider: %s", cls.__name__)
    return cls.from_settings(settings)


def build_confidence_scorer(settings: Settings) -> BaseConfidenceScorer:
    """Load ``confidence.yaml`` (or ``CONFIDENCE_CONFIG_PATH``) and build its scorer."""
    section = load_confidence_config(settings.confidence_config_path).confidence
    cls: type[BaseConfidenceScorer] = registry.resolve("confidence", section.scorer)
    logger.info("confidence scorer: %s", cls.__name__)
    return cls.from_config(section)
