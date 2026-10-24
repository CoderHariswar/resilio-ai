"""PostgreSQL + pgvector + LangGraph checkpoint durability.

Skipped unless ``TEST_DATABASE_URL`` points at a disposable database, e.g.::

    docker run -d --rm -p 5433:5432 -e POSTGRES_PASSWORD=resilio pgvector/pgvector:pg16
    TEST_DATABASE_URL=postgresql://postgres:resilio@localhost:5433/postgres pytest -m integration
"""

from __future__ import annotations

import os
import uuid

import pytest

from resilio.config import Settings
from resilio.container import build_container
from resilio.core.database import PostgresIncidentRepository, create_pool
from resilio.core.queue import InMemoryIncidentQueue
from resilio.models import ApprovalDecision, IncidentStatus
from resilio.providers.notifications.mock import MockNotifier
from resilio.utils.embeddings import HashingEmbedder
from tests.helpers import make_event

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set"),
]


async def test_repository_and_vector_search() -> None:
    assert DSN
    pool = await create_pool(DSN, 1, 2)
    try:
        repo = PostgresIncidentRepository(pool)
        await repo.initialize()
        assert repo.vector_enabled, "use the pgvector/pgvector image"
        event = make_event(service=f"svc-{uuid.uuid4().hex[:6]}")
        record = await repo.create(event)
        assert record.status == IncidentStatus.RECEIVED
        assert (await repo.create(event)).id == event.id

        updated = await repo.update_status(event.id, IncidentStatus.RESOLVED, {"k": 1})
        assert updated is not None and updated.resolved_at is not None
        assert updated.summary == {"k": 1}
        assert (await repo.get(event.id)) == updated
        assert event.id in [r.id for r in await repo.list_incidents(IncidentStatus.RESOLVED)]
        assert await repo.list_incidents(limit=1)
        assert await repo.update_status("missing", IncidentStatus.FAILED) is None

        embedder = HashingEmbedder()
        await repo.remember(
            event.id, event.service, embedder.embed("OOMKilled heap"), {"root_cause": "memory_leak"}
        )
        similar = await repo.find_similar(embedder.embed("OOMKilled heap space"), k=5)
        assert any(s["incident_id"] == event.id and s["similarity"] > 0.5 for s in similar)
        assert all(
            s["incident_id"] != event.id
            for s in await repo.find_similar(embedder.embed("x"), exclude_id=event.id)
        )
    finally:
        await pool.close()


async def test_hitl_checkpoint_survives_process_restart() -> None:
    """Pause in one container, resume from a brand-new one (new graph, new pool)."""
    assert DSN
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=DSN,
        mock_llm_fault_rate=0.0,
        health_check_timeout_seconds=1,
    )
    event = make_event("bad-deploy", service=f"svc-{uuid.uuid4().hex[:6]}")

    first = await build_container(settings, notifier=MockNotifier(), queue=InMemoryIncidentQueue())
    try:
        await first.runner.run(event)
        record = await first.deps.repository.get(event.id)
        assert record is not None and record.status == IncidentStatus.AWAITING_APPROVAL
        target = record.summary["hypothesis"]["target_sha"]
    finally:
        await first.aclose()

    second = await build_container(settings, notifier=MockNotifier(), queue=InMemoryIncidentQueue())
    try:
        # The new process has a fresh mock world; re-inject the outage so the rollback can heal it.
        second.deps.telemetry.inject_outage(event.service, "bad-deploy")  # type: ignore[attr-defined]
        state = await second.runner.resume(
            event.id, ApprovalDecision(approved=True, approver="next-shift")
        )
        assert state["status"] == "resolved"
        assert state["hypothesis"]["target_sha"] == target
        assert state["approval"]["approver"] == "next-shift"
    finally:
        await second.aclose()
