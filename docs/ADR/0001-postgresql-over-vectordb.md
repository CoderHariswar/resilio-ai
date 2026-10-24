# 0001. PostgreSQL + pgvector instead of a dedicated vector database

- Status: Accepted
- Date: 2026-10-24
- Deciders: ResilioAI maintainers

## Context

ResilioAI stores three kinds of state:

1. **Incident lifecycle rows**: status, hypothesis, policy decision, timeline.
   Queried by the API and `resilio status`.
2. **LangGraph checkpoints**: the full graph state at every step, needed to
   resume an incident after a human approves it (see [ADR 0002](0002-langgraph-for-hitl-state.md)).
3. **Incident memory**: an embedding per resolved incident, so the
   synthesizer can be told "this looks like INC-1234, which was fixed by
   rolling back".

Only (3) is a vector workload, and it is small. A busy organization opens
thousands of incidents a year, not millions; the corpus is the organization's
own incident history. Each retrieval happens once per investigation, next to
LLM calls that take seconds.

The usual reflex for "the agent needs memory" is to add a dedicated vector
database (Pinecone, Weaviate, Qdrant, Milvus, Chroma). This project's
constraints argue against it:

- **Zero-friction self-hosting is a product requirement.** `docker compose up`
  must work with no accounts, and every extra stateful service is another
  thing to back up, secure, upgrade and monitor, in the one system that has
  to work *during* an outage.
- **Correctness across stores matters.** An incident's status, its checkpoint
  and its memory entry should never disagree. Two databases mean two failure
  domains and no shared transaction.
- **Queries are hybrid.** "Similar incidents *for this service* in the last
  90 days that were *resolved* by *rollback*" is a relational filter plus a
  vector ranking plus a JSON projection.

## Decision

Use **one PostgreSQL database with the pgvector extension** for incident rows,
LangGraph checkpoints and incident-memory embeddings. Similarity search is a
plain SQL query over an HNSW index (`vector_cosine_ops`):

```sql
SELECT incident_id, service, document, 1 - (embedding <=> $1) AS similarity
  FROM incident_memory
 WHERE incident_id IS DISTINCT FROM $2
 ORDER BY embedding <=> $1
 LIMIT $3;
```

Embeddings are 256-dimensional and produced by a deterministic
feature-hashing embedder by default, so Mock Mode needs no model; a
model-backed `Embedder` can replace it without schema changes beyond the
dimension.

If pgvector is not installed, the repository logs a warning and disables
similarity search; everything else keeps working. Without `DATABASE_URL` at
all, an in-memory implementation with the same interface is used.

## Measurements

Measured with the exact index type and dimensionality used in
[`core/database.py`](../../resilio/core/database.py), stock
`pgvector/pgvector:pg16` container with default settings, on a laptop (AMD
Ryzen 7 5800H). Latency is measured from the Python client and includes the
network round-trip, i.e. what the graph actually waits for. 300 queries each:

| Corpus | Query | p50 | p95 | p99 |
|---|---|---|---|---|
| 100,000 × 256-d | top-3 cosine (HNSW) | 1.19 ms | 1.58 ms | 1.80 ms |
| 100,000 × 256-d | top-3 + `WHERE service = …` + JSONB projection | 1.44 ms | 1.89 ms | 2.22 ms |

Loading 100k rows with `COPY` took 12.5 s; building the HNSW index took 56 s
(one-time, and roughly 10–100× more incidents than most organizations will
ever accumulate). For comparison, a single synthesizer call to a hosted LLM
is typically 1–10 s, so retrieval is well under 0.1% of the investigation
loop. A dedicated vector database could only save part of 1–2 ms that nobody
can observe, and it would add a network hop to a second service.

Reproduce with any pgvector instance: create the `incident_memory` schema,
`COPY` random unit vectors, and time the query above.

## Alternatives considered

**Dedicated vector database (Pinecone, Qdrant, Weaviate, Milvus).**
Excellent at billion-scale ANN, filtering at scale, and managed operations.
None of that applies at our corpus size, and the costs do: a second stateful
service in `docker compose`, a second set of credentials, cross-store
consistency handled by hand, and for SaaS options, sending incident
descriptions to a third party. That last one is unacceptable for many of the
regulated, air-gapped teams this project targets.

**Embedded vector library (FAISS, hnswlib, Chroma in-process).**
No extra service, but the index lives in one process's memory. That conflicts
with horizontally scaled API replicas and with resuming incidents on a
different worker. Persistence would have to be built by hand.

**No vector search; keyword/trigram search (`pg_trgm`, full-text).**
Simpler, and it remains a reasonable fallback. It handles paraphrase poorly
("OOMKilled" vs "out of memory") and was rejected as the primary mechanism.
Postgres makes it cheap to add later as a hybrid signal.

**Redis (RediSearch vectors).**
Redis is already in the stack as a queue, but we treat it as disposable;
making it the system of record for memory would change its durability
requirements and still split state across two stores.

## Consequences

**Positive**

- One stateful dependency to operate, back up (`pg_dump`) and secure.
- Incident status, checkpoints and memory share a database; hybrid
  relational + vector queries are one SQL statement.
- Works fully offline and on-prem; managed Postgres offerings (RDS, Cloud SQL,
  Azure, Supabase, Neon) ship pgvector.
- Mock Mode and tests use an in-memory implementation of the same interface.

**Negative / risks**

- HNSW with a selective `WHERE` filter can return fewer than *k* rows,
  because filtering happens after the index scan (`hnsw.ef_search` candidates,
  default 40). At our scale the planner can fall back to the B-tree on
  `service`, which is exact. If filters become very selective on a large
  corpus, enable pgvector ≥ 0.8 iterative scans
  (`SET hnsw.iterative_scan = relaxed_order`) or raise `hnsw.ef_search`.
- Index builds are CPU-heavy and slow down as the corpus grows; schedule
  `REINDEX` off-peak if embeddings are ever re-generated in bulk.
- Changing embedding dimension requires a migration of `incident_memory`.

**Revisit when** the memory corpus exceeds roughly 10 million vectors,
p99 retrieval latency becomes a visible share of investigation time, or
multi-tenant isolation requirements outgrow row-level security.
