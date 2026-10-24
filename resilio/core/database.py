"""Incident persistence: an async Postgres pool plus an in-memory fallback.

PostgreSQL holds three things (see ``docs/ADR/0001``):

* ``incidents`` - lifecycle/status rows queried by the API and ``resilio status``.
* ``incident_memory`` - pgvector embeddings of resolved incidents, used to show
  the synthesizer how similar outages were fixed before.
* LangGraph checkpoint tables - created by ``AsyncPostgresSaver.setup()``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from resilio.models import IncidentEvent, IncidentRecord, IncidentStatus, utcnow
from resilio.utils.embeddings import cosine_similarity

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg.rows import DictRow
    from psycopg_pool import AsyncConnectionPool

    #: Connection pool whose cursors return dict rows (required by AsyncPostgresSaver).
    DictPool = AsyncConnectionPool[AsyncConnection[DictRow]]

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 256

# One statement per entry: the pool prepares every statement (prepare_threshold=0,
# as LangGraph's checkpointer requires) and prepared statements cannot batch.
SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS incidents (
        id          TEXT PRIMARY KEY,
        service     TEXT        NOT NULL,
        title       TEXT        NOT NULL,
        severity    TEXT        NOT NULL,
        source      TEXT        NOT NULL,
        status      TEXT        NOT NULL,
        event       JSONB       NOT NULL DEFAULT '{}'::jsonb,
        summary     JSONB       NOT NULL DEFAULT '{}'::jsonb,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolved_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS incidents_status_idx ON incidents (status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS incidents_service_idx ON incidents (service, created_at DESC)",
)

VECTOR_SCHEMA_SQL: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""
    CREATE TABLE IF NOT EXISTS incident_memory (
        incident_id TEXT PRIMARY KEY REFERENCES incidents(id) ON DELETE CASCADE,
        service     TEXT        NOT NULL,
        embedding   vector({EMBEDDING_DIM}) NOT NULL,
        document    JSONB       NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS incident_memory_hnsw
        ON incident_memory USING hnsw (embedding vector_cosine_ops)
    """,
)


class IncidentRepository(ABC):
    """Storage port for incidents and incident memory."""

    async def initialize(self) -> None:
        """Create schema. Idempotent."""

    async def aclose(self) -> None:
        """Release connections."""

    @abstractmethod
    async def create(self, event: IncidentEvent) -> IncidentRecord: ...

    @abstractmethod
    async def get(self, incident_id: str) -> IncidentRecord | None: ...

    @abstractmethod
    async def update_status(
        self,
        incident_id: str,
        status: IncidentStatus,
        summary: dict[str, Any] | None = None,
    ) -> IncidentRecord | None: ...

    @abstractmethod
    async def list_incidents(
        self, status: IncidentStatus | None = None, limit: int = 50
    ) -> list[IncidentRecord]: ...

    @abstractmethod
    async def remember(
        self, incident_id: str, service: str, embedding: list[float], document: dict[str, Any]
    ) -> None:
        """Store a resolved incident's embedding for future similarity search."""

    @abstractmethod
    async def find_similar(
        self, embedding: list[float], k: int = 3, exclude_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return up to ``k`` past incidents: ``{incident_id, similarity, **document}``."""


class InMemoryIncidentRepository(IncidentRepository):
    """Dict-backed repository for Mock Mode and tests."""

    def __init__(self) -> None:
        self._rows: dict[str, IncidentRecord] = {}
        self._memory: dict[str, tuple[list[float], dict[str, Any]]] = {}

    async def create(self, event: IncidentEvent) -> IncidentRecord:
        existing = self._rows.get(event.id)
        if existing is not None:
            return existing
        now = utcnow()
        record = IncidentRecord(
            id=event.id,
            service=event.service,
            title=event.title,
            severity=event.severity,
            source=event.source,
            status=IncidentStatus.RECEIVED,
            created_at=now,
            updated_at=now,
            event=event.model_dump(mode="json"),
        )
        self._rows[event.id] = record
        return record

    async def get(self, incident_id: str) -> IncidentRecord | None:
        return self._rows.get(incident_id)

    async def update_status(
        self,
        incident_id: str,
        status: IncidentStatus,
        summary: dict[str, Any] | None = None,
    ) -> IncidentRecord | None:
        record = self._rows.get(incident_id)
        if record is None:
            return None
        now = utcnow()
        updated = record.model_copy(
            update={
                "status": status,
                "updated_at": now,
                "summary": {**record.summary, **(summary or {})},
                "resolved_at": now if status.is_terminal else record.resolved_at,
            }
        )
        self._rows[incident_id] = updated
        return updated

    async def list_incidents(
        self, status: IncidentStatus | None = None, limit: int = 50
    ) -> list[IncidentRecord]:
        rows = [r for r in self._rows.values() if status is None or r.status == status]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    async def remember(
        self, incident_id: str, service: str, embedding: list[float], document: dict[str, Any]
    ) -> None:
        self._memory[incident_id] = (embedding, {"service": service, **document})

    async def find_similar(
        self, embedding: list[float], k: int = 3, exclude_id: str | None = None
    ) -> list[dict[str, Any]]:
        scored = [
            {"incident_id": iid, "similarity": round(cosine_similarity(embedding, vec), 4), **doc}
            for iid, (vec, doc) in self._memory.items()
            if iid != exclude_id
        ]
        scored.sort(key=lambda item: item["similarity"], reverse=True)
        return scored[:k]


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"


class PostgresIncidentRepository(IncidentRepository):
    """psycopg3 async implementation. The pool must use ``dict_row``.

    The pool is owned by the caller (it is shared with the LangGraph
    checkpointer), so :meth:`aclose` does not close it.
    """

    _COLUMNS = (
        "id, service, title, severity, source, status, event, summary, "
        "created_at, updated_at, resolved_at"
    )

    def __init__(self, pool: DictPool) -> None:
        self._pool = pool
        self.vector_enabled = False

    async def initialize(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA_SQL:
                await conn.execute(statement)
        try:
            async with self._pool.connection() as conn:
                for statement in VECTOR_SCHEMA_SQL:
                    await conn.execute(statement)
            self.vector_enabled = True
        except Exception as exc:
            logger.warning("pgvector unavailable, similarity search disabled: %s", exc)

    @staticmethod
    def _to_record(row: dict[str, Any]) -> IncidentRecord:
        return IncidentRecord.model_validate(row)

    async def create(self, event: IncidentEvent) -> IncidentRecord:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""
                INSERT INTO incidents (id, service, title, severity, source, status, event)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET updated_at = now()
                RETURNING {self._COLUMNS}
                """,  # noqa: S608 - column list is a constant
                (
                    event.id,
                    event.service,
                    event.title,
                    event.severity.value,
                    event.source,
                    IncidentStatus.RECEIVED.value,
                    event.model_dump_json(),
                ),
            )
            row = await cur.fetchone()
        assert row is not None
        return self._to_record(row)

    async def get(self, incident_id: str) -> IncidentRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {self._COLUMNS} FROM incidents WHERE id = %s",  # noqa: S608
                (incident_id,),
            )
            row = await cur.fetchone()
        return self._to_record(row) if row else None

    async def update_status(
        self,
        incident_id: str,
        status: IncidentStatus,
        summary: dict[str, Any] | None = None,
    ) -> IncidentRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""
                UPDATE incidents
                   SET status = %s,
                       summary = summary || %s::jsonb,
                       updated_at = now(),
                       resolved_at = CASE WHEN %s THEN now() ELSE resolved_at END
                 WHERE id = %s
             RETURNING {self._COLUMNS}
                """,  # noqa: S608
                (status.value, json.dumps(summary or {}), status.is_terminal, incident_id),
            )
            row = await cur.fetchone()
        return self._to_record(row) if row else None

    async def list_incidents(
        self, status: IncidentStatus | None = None, limit: int = 50
    ) -> list[IncidentRecord]:
        async with self._pool.connection() as conn:
            if status is None:
                cur = await conn.execute(
                    f"SELECT {self._COLUMNS} FROM incidents "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            else:
                cur = await conn.execute(
                    f"SELECT {self._COLUMNS} FROM incidents WHERE status = %s "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT %s",
                    (status.value, limit),
                )
            rows = await cur.fetchall()
        return [self._to_record(r) for r in rows]

    async def remember(
        self, incident_id: str, service: str, embedding: list[float], document: dict[str, Any]
    ) -> None:
        if not self.vector_enabled:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO incident_memory (incident_id, service, embedding, document)
                VALUES (%s, %s, %s::vector, %s::jsonb)
                ON CONFLICT (incident_id) DO UPDATE
                   SET embedding = EXCLUDED.embedding, document = EXCLUDED.document
                """,
                (incident_id, service, _vector_literal(embedding), json.dumps(document)),
            )

    async def find_similar(
        self, embedding: list[float], k: int = 3, exclude_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not self.vector_enabled:
            return []
        vector = _vector_literal(embedding)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT incident_id, service, document,
                       1 - (embedding <=> %s::vector) AS similarity
                  FROM incident_memory
                 WHERE incident_id IS DISTINCT FROM %s
              ORDER BY embedding <=> %s::vector
                 LIMIT %s
                """,
                (vector, exclude_id, vector, k),
            )
            rows = await cur.fetchall()
        return [
            {
                "incident_id": r["incident_id"],
                "similarity": round(float(r["similarity"]), 4),
                "service": r["service"],
                **r["document"],
            }
            for r in rows
        ]


async def create_pool(dsn: str, min_size: int = 1, max_size: int = 10) -> DictPool:
    """Open a psycopg async pool compatible with LangGraph's ``AsyncPostgresSaver``."""
    from psycopg import AsyncConnection
    from psycopg.rows import DictRow, dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=dsn,
        connection_class=AsyncConnection[DictRow],
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=30)
    return pool
