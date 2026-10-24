# 0002. LangGraph checkpoints for human-in-the-loop state

- Status: Accepted
- Date: 2026-10-24
- Deciders: ResilioAI maintainers

## Context

An incident investigation is a multi-step, branching, partly
non-deterministic process: gather evidence → hypothesize → critique → maybe
revise (a loop) → decide risk → maybe **wait for a human** → act → verify →
resolve or escalate.

The wait is the hard part. An approval for a production rollback can take
seconds or hours. During that time:

- the API process may be redeployed, crash, or be scaled to zero;
- the approval may land on a different replica than the one that paused;
- the on-call engineer needs to see *exactly* what the agent saw and proposed;
- nothing must execute twice. A rollback replayed after a restart is itself
  an incident.

We need durable, resumable, inspectable execution state, ideally without
introducing a workflow-engine cluster that is harder to run than the thing
it is protecting.

## Decision

Model the investigation as a **LangGraph `StateGraph`** with an explicit,
JSON-serializable `TypedDict` state ([`graph/state.py`](../../resilio/graph/state.py)),
and persist it with **LangGraph's PostgreSQL checkpointer**
(`AsyncPostgresSaver`) on the same database as everything else
([ADR 0001](0001-postgresql-over-vectordb.md)).

- Each incident is one thread: `thread_id = incident_id`.
- The approval gate calls `langgraph.types.interrupt(payload)`, a dynamic
  breakpoint. The graph checkpoints and returns; no coroutine, thread or
  process waits for the human.
- Approval resumes the thread with `Command(resume={"approved": ..., "approver": ...})`
  from whichever process receives the API call.
- **Side effects never share a node with `interrupt()`.** On resume,
  LangGraph re-executes the interrupted node from the top. So sending the
  approval request (`request_approval`) and pausing (`approval_gate`) are
  separate nodes, and the notification is sent exactly once.
- Without `DATABASE_URL`, an `InMemorySaver` provides identical semantics for
  Mock Mode and tests (paused incidents simply don't survive a restart).

This is verified end-to-end by
`tests/integration/test_postgres.py::test_hitl_checkpoint_survives_process_restart`,
which pauses an incident in one container, discards it, and resumes it from a
brand-new container with a new graph and connection pool.

## Alternatives considered

**Hand-rolled state machine + status column.**
Simple at first. But the loop (evaluator → synthesizer), conditional
branches, and "resume mid-graph" logic recreate a workflow engine badly, with
hand-written serialization of every intermediate field and no step history.

**Temporal / Cadence.**
The gold standard for durable execution, with excellent retry semantics and
visibility. It requires running a Temporal cluster (or buying the cloud
service), and workflow code must obey determinism constraints that sit
awkwardly with LLM calls. Too heavy for a project whose first requirement is
`docker compose up` with zero accounts. We would reconsider it for
multi-day, multi-team remediation sagas.

**Celery / RQ task chains.**
Good for fire-and-forget jobs; poor fit for long human pauses, branching, and
inspecting intermediate state. Pausing means parking a task or polling.

**Cloud workflow services (AWS Step Functions, GCP Workflows).**
Durable and managed, but cloud-specific, awkward to run locally, and they put
the control flow of an on-prem incident responder in a vendor's console.

**Agent frameworks without explicit graphs (free-form tool-calling loops).**
Flexible, but the control flow is decided by the model. For an agent that
can modify production we want the *possible* transitions to be code-reviewed
and auditable; the LLM fills in content, not control flow.

## Consequences

**Positive**

- Durable HITL with no extra infrastructure: checkpoints are rows in Postgres.
- Control flow is explicit code, reviewable in one file
  ([`workflow.py`](../../resilio/graph/workflow.py)); the LLM cannot invent a path
  that skips the evaluator or the policy engine.
- Every step is checkpointed, so `aget_state_history` gives a full audit trail
  and time-travel debugging of an investigation.
- Nodes are plain async functions taking state and returning partial updates,
  so they are easy to unit test (see `tests/unit/test_nodes.py`).

**Negative / risks**

- State must stay JSON-serializable. We store plain dicts, not Pydantic
  models, and re-validate at node boundaries. Renaming a state key is a schema
  migration for in-flight incidents. Prefer additive changes.
- Checkpoint tables grow with every step (~10 rows per incident). Add a
  retention job (delete threads for incidents resolved > N days ago) before
  this matters.
- We depend on LangGraph's interrupt/resume semantics, which have changed
  between major versions. The integration tests pin the behaviour we rely on.
- Exactly-once execution of external side effects is still the provider's
  responsibility. Rollback providers should be idempotent (dispatching the
  same rollback twice must be harmless), because a crash *after* the API call
  but *before* the checkpoint can replay the node.

**Revisit when** remediations need multi-day sagas with compensation steps,
cross-team approvals with escalation timers, or execution volumes where a
dedicated durable-execution engine pays for itself.
