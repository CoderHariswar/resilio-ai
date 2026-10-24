"""Composition root: builds providers, storage, queue, graph and runner from settings."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from resilio.core.database import (
    IncidentRepository,
    InMemoryIncidentRepository,
    PostgresIncidentRepository,
    create_pool,
)
from resilio.core.metrics import HITL_PENDING
from resilio.core.queue import IncidentQueue, build_queue
from resilio.graph.deps import WorkflowDeps
from resilio.graph.workflow import build_checkpointer, build_workflow
from resilio.models import IncidentStatus
from resilio.providers.registry import (
    build_llm_provider,
    build_notifier,
    build_remediation_provider,
    build_telemetry_provider,
)
from resilio.worker import IncidentRunner

if TYPE_CHECKING:
    from resilio.config import Settings
    from resilio.core.database import DictPool
    from resilio.providers.base import (
        BaseLLMProvider,
        BaseNotifier,
        BaseRemediationProvider,
        BaseTelemetryProvider,
    )

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    deps: WorkflowDeps
    queue: IncidentQueue
    graph: Any
    runner: IncidentRunner
    pool: DictPool | None = None

    async def aclose(self) -> None:
        providers = (
            self.deps.telemetry,
            self.deps.remediation,
            self.deps.notifier,
            self.deps.llm,
        )
        await asyncio.gather(*(p.aclose() for p in providers), return_exceptions=True)
        await self.deps.repository.aclose()
        await self.queue.aclose()
        if self.pool is not None:
            await self.pool.close()


async def build_container(
    settings: Settings,
    *,
    telemetry: BaseTelemetryProvider | None = None,
    remediation: BaseRemediationProvider | None = None,
    notifier: BaseNotifier | None = None,
    llm: BaseLLMProvider | None = None,
    repository: IncidentRepository | None = None,
    queue: IncidentQueue | None = None,
) -> Container:
    """Wire the application. Any component can be overridden (tests, embedding)."""
    telemetry = telemetry or build_telemetry_provider(settings)
    remediation = remediation or build_remediation_provider(settings, telemetry)
    notifier = notifier or build_notifier(settings)
    llm = llm or build_llm_provider(settings)

    pool: DictPool | None = None
    if repository is None:
        if settings.database_url:
            pool = await create_pool(
                settings.database_url, settings.database_pool_min, settings.database_pool_max
            )
            repository = PostgresIncidentRepository(pool)
            logger.info("storage: postgresql")
        else:
            repository = InMemoryIncidentRepository()
            logger.info("storage: in-memory (set DATABASE_URL for durable HITL checkpoints)")
    await repository.initialize()
    checkpointer = await build_checkpointer(pool)

    deps = WorkflowDeps(
        settings=settings,
        telemetry=telemetry,
        remediation=remediation,
        notifier=notifier,
        llm=llm,
        repository=repository,
    )
    queue = queue or build_queue(settings)
    graph = build_workflow(deps, checkpointer)
    HITL_PENDING.set(
        len(await repository.list_incidents(IncidentStatus.AWAITING_APPROVAL, limit=1000))
    )
    return Container(
        settings=settings,
        deps=deps,
        queue=queue,
        graph=graph,
        runner=IncidentRunner(graph, deps, queue),
        pool=pool,
    )
