"""Abstract base classes for every pluggable provider.

A provider is a small class that adapts one external system (Datadog, ArgoCD,
Slack, an LLM API...) to a narrow async interface. To add a new integration,
subclass one of these ABCs, implement the abstract methods, and register it
(see ``docs/CONTRIBUTING.md``). The graph never imports a concrete provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Self

if TYPE_CHECKING:
    from collections.abc import Mapping

    from resilio.config import Settings
    from resilio.models import Deployment, Notification


class _Lifecycle:
    """Shared lifecycle hooks; override when the provider holds resources."""

    #: Registry name, e.g. ``"datadog"``. Set by subclasses.
    name: ClassVar[str] = "unnamed"

    async def aclose(self) -> None:
        """Release network clients / pools. Called on application shutdown."""

    async def healthcheck(self) -> bool:
        """Return True if the backing system is reachable. Used by ``/readyz``."""
        return True


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
class BaseTelemetryProvider(_Lifecycle, ABC):
    """Reads error evidence (traces, logs, metrics) from an observability backend."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the provider from application settings. Override as needed."""
        return cls()

    @abstractmethod
    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        """Fetch error traces and log snippets.

        Returns a list of dicts following :class:`resilio.models.TraceDict`
        (``timestamp``, ``level``, ``message``, ``error_type``, ``trace_id``...).
        """

    async def get_error_rate(self, service_name: str) -> float | None:
        """Return the current error ratio in ``[0, 1]``, or None if unsupported."""
        return None


# --------------------------------------------------------------------------- #
# Remediation
# --------------------------------------------------------------------------- #
class BaseRemediationProvider(_Lifecycle, ABC):
    """Changes the running system: rollbacks, restarts, scaling."""

    def __init__(self, telemetry: BaseTelemetryProvider | None = None) -> None:
        self.telemetry = telemetry

    @classmethod
    def from_settings(
        cls, settings: Settings, telemetry: BaseTelemetryProvider | None = None
    ) -> Self:
        """Build the provider from application settings. Override as needed."""
        return cls(telemetry=telemetry)

    @abstractmethod
    async def execute_rollback(self, service_name: str, target_sha: str) -> bool:
        """Execute deployment rollback."""

    @abstractmethod
    async def verify_health(self, service_name: str, timeout_seconds: int) -> bool:
        """Poll telemetry to verify error rate reduction."""

    async def recent_deployments(self, service_name: str, limit: int = 10) -> list[Deployment]:
        """Return deployment history, newest first. Used to pick a rollback target."""
        return []

    async def restart_service(self, service_name: str) -> bool:
        """Restart workloads in place (e.g. rolling restart). Optional."""
        raise NotImplementedError(f"{self.name} does not support restart")

    async def scale_service(self, service_name: str, replicas_delta: int) -> bool:
        """Scale horizontally by ``replicas_delta`` replicas. Optional."""
        raise NotImplementedError(f"{self.name} does not support scaling")


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
class BaseNotifier(_Lifecycle, ABC):
    """Delivers human-facing messages (status updates, approval requests)."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the notifier from application settings. Override as needed."""
        return cls()

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Deliver ``notification``. Return False on a non-fatal delivery failure."""


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LLMRequest:
    """A single completion request.

    ``system`` and ``prompt`` are what real models see. ``task`` and
    ``context`` carry the same information in structured form, which lets the
    deterministic :class:`MockLLMProvider` work without parsing prose.
    """

    system: str
    prompt: str
    task: str = "generic"
    context: Mapping[str, Any] = field(default_factory=dict)
    max_tokens: int = 1024
    temperature: float = 0.0
    json_mode: bool = True


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class BaseLLMProvider(_Lifecycle, ABC):
    """Chat-completion style LLM backend (commercial API or local runtime)."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the LLM client from application settings. Override as needed."""
        return cls()

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion and return the raw text plus token accounting."""
