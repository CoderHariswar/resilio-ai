from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from resilio.config import Settings
from resilio.container import Container, build_container
from resilio.core.database import InMemoryIncidentRepository
from resilio.core.queue import InMemoryIncidentQueue
from resilio.graph.deps import WorkflowDeps
from resilio.providers.llm.mock import MockLLMProvider
from resilio.providers.notifications.mock import MockNotifier
from resilio.providers.remediation.mock import MockRemediationProvider
from resilio.providers.telemetry.mock import MockTelemetryProvider


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mock_llm_fault_rate=0.0,
        health_check_timeout_seconds=1,
        health_check_interval_seconds=0.01,
        notifiers=["mock"],
    )


@pytest.fixture
def telemetry() -> MockTelemetryProvider:
    return MockTelemetryProvider(traces_per_fetch=120)


@pytest.fixture
def remediation(telemetry: MockTelemetryProvider) -> MockRemediationProvider:
    return MockRemediationProvider(telemetry)


@pytest.fixture
def notifier() -> MockNotifier:
    return MockNotifier()


@pytest.fixture
def llm() -> MockLLMProvider:
    return MockLLMProvider()


@pytest.fixture
def repository() -> InMemoryIncidentRepository:
    return InMemoryIncidentRepository()


@pytest.fixture
def deps(
    settings: Settings,
    telemetry: MockTelemetryProvider,
    remediation: MockRemediationProvider,
    notifier: MockNotifier,
    llm: MockLLMProvider,
    repository: InMemoryIncidentRepository,
) -> WorkflowDeps:
    return WorkflowDeps(
        settings=settings,
        telemetry=telemetry,
        remediation=remediation,
        notifier=notifier,
        llm=llm,
        repository=repository,
    )


@pytest.fixture
async def container(
    settings: Settings,
    telemetry: MockTelemetryProvider,
    remediation: MockRemediationProvider,
    notifier: MockNotifier,
    llm: MockLLMProvider,
    repository: InMemoryIncidentRepository,
) -> AsyncIterator[Container]:
    built = await build_container(
        settings,
        telemetry=telemetry,
        remediation=remediation,
        notifier=notifier,
        llm=llm,
        repository=repository,
        queue=InMemoryIncidentQueue(),
    )
    yield built
    await built.aclose()
