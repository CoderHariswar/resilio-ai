"""Dependencies injected into every node (constructor injection, no globals)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from resilio.utils.embeddings import Embedder, HashingEmbedder

if TYPE_CHECKING:
    from resilio.config import Settings
    from resilio.core.database import IncidentRepository
    from resilio.providers.base import (
        BaseLLMProvider,
        BaseNotifier,
        BaseRemediationProvider,
        BaseTelemetryProvider,
    )


@dataclass(slots=True)
class WorkflowDeps:
    settings: Settings
    telemetry: BaseTelemetryProvider
    remediation: BaseRemediationProvider
    notifier: BaseNotifier
    llm: BaseLLMProvider
    repository: IncidentRepository
    embedder: Embedder = field(default_factory=HashingEmbedder)
