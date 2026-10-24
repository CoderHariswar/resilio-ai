from __future__ import annotations

from resilio.core.database import InMemoryIncidentRepository, _vector_literal
from resilio.core.metrics import render_metrics
from resilio.models import IncidentStatus
from resilio.utils.embeddings import HashingEmbedder, cosine_similarity
from tests.helpers import make_event


async def test_in_memory_repository_lifecycle() -> None:
    repo = InMemoryIncidentRepository()
    event = make_event()
    record = await repo.create(event)
    assert record.status == IncidentStatus.RECEIVED
    assert (await repo.create(event)).created_at == record.created_at, "create is idempotent"

    updated = await repo.update_status(event.id, IncidentStatus.AWAITING_APPROVAL, {"a": 1})
    assert updated is not None and updated.resolved_at is None
    done = await repo.update_status(event.id, IncidentStatus.RESOLVED, {"b": 2})
    assert done is not None and done.resolved_at is not None
    assert done.summary == {"a": 1, "b": 2}
    assert await repo.update_status("missing", IncidentStatus.RESOLVED) is None

    other = make_event("disk-full", service="api")
    await repo.create(other)
    assert [r.id for r in await repo.list_incidents()] == [other.id, event.id]
    assert [r.id for r in await repo.list_incidents(IncidentStatus.RESOLVED)] == [event.id]
    assert len(await repo.list_incidents(limit=1)) == 1
    assert await repo.get("missing") is None
    await repo.initialize()
    await repo.aclose()


async def test_in_memory_similarity_search() -> None:
    repo = InMemoryIncidentRepository()
    embedder = HashingEmbedder()
    await repo.remember(
        "leak", "a", embedder.embed("OOMKilled heap space"), {"root_cause": "memory_leak"}
    )
    await repo.remember(
        "disk", "b", embedder.embed("no space left on device"), {"root_cause": "disk"}
    )
    results = await repo.find_similar(embedder.embed("OOMKilled heap space again"), k=2)
    assert results[0]["incident_id"] == "leak"
    assert results[0]["similarity"] > results[1]["similarity"]
    excluded = await repo.find_similar(embedder.embed("x"), k=5, exclude_id="leak")
    assert [r["incident_id"] for r in excluded] == ["disk"]


def test_embedder_properties() -> None:
    embedder = HashingEmbedder(dim=64)
    a = embedder.embed("Connection pool exhausted")
    assert len(a) == 64
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
    assert a == embedder.embed("connection POOL exhausted")
    assert embedder.embed("") == [0.0] * 64
    assert cosine_similarity(a, a) > 0.999
    assert cosine_similarity(a, [0.0] * 64) == 0.0


def test_vector_literal() -> None:
    assert _vector_literal([0.5, -1.0]) == "[0.500000,-1.000000]"


def test_metrics_exposition_contains_headline_series() -> None:
    body, content_type = render_metrics()
    text = body.decode()
    assert content_type.startswith("text/plain")
    for name in (
        'resilio_incidents_total{status="resolved"}',
        'resilio_incidents_total{status="escalated"}',
        "resilio_token_savings_percent",
        "resilio_evaluator_corrections_total",
        "resilio_resolution_duration_seconds_bucket",
    ):
        assert name in text
