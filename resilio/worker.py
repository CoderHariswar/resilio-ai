"""Incident runner: drives the graph, syncs status to the repository, records metrics.

The runner is the only component that talks to both the graph and the
repository. Nodes stay pure (state in, state update out) and the API stays
thin (validate, enqueue, read).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

from langgraph.types import Command

from resilio.core.metrics import HITL_PENDING, record_terminal
from resilio.core.queue import IncidentQueue
from resilio.graph.deps import WorkflowDeps
from resilio.graph.nodes.telemetry_node import memory_text
from resilio.graph.state import IncidentState
from resilio.graph.workflow import thread_config
from resilio.models import ApprovalDecision, IncidentEvent, IncidentStatus

logger = logging.getLogger(__name__)

SUMMARY_KEYS = (
    "hypothesis",
    "evaluation",
    "confidence",
    "llm_confidence",
    "confidence_breakdown",
    "policy",
    "approval",
    "remediation",
    "escalation_reason",
    "token_stats",
    "evaluator_attempts",
    "llm_tokens",
    "rollback_target",
)


class InvalidIncidentStateError(RuntimeError):
    """The requested transition is not valid for the incident's current status."""


def summarize(values: dict[str, Any]) -> dict[str, Any]:
    """The subset of graph state persisted on the incident row."""
    summary = {key: values.get(key) for key in SUMMARY_KEYS if key in values}
    summary["timeline"] = list(values.get("timeline", []))[-50:]
    return summary


class IncidentRunner:
    def __init__(self, graph: Any, deps: WorkflowDeps, queue: IncidentQueue) -> None:
        self.graph = graph
        self.deps = deps
        self.queue = queue
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def repository(self) -> Any:
        return self.deps.repository

    # ----------------------------------------------------------- lifecycle
    async def run(self, event: IncidentEvent) -> IncidentState:
        """Run a new incident until it terminates or pauses for approval."""
        if await self.repository.get(event.id) is None:
            await self.repository.create(event)
        await self.repository.update_status(event.id, IncidentStatus.INVESTIGATING)
        initial: IncidentState = {
            "incident_id": event.id,
            "event": event.model_dump(mode="json"),
            "started_at": time.time(),
            "status": IncidentStatus.INVESTIGATING.value,
            "evaluator_attempts": 0,
            "timeline": [],
            "llm_tokens": 0,
        }
        async with self._locks[event.id]:
            try:
                await self.graph.ainvoke(initial, thread_config(event.id))
            except Exception as exc:
                return await self._fail(event, exc)
            return await self._sync(event.id)

    async def resume(self, incident_id: str, decision: ApprovalDecision) -> IncidentState:
        """Resume an incident paused at the approval gate."""
        async with self._locks[incident_id]:
            record = await self.repository.get(incident_id)
            if record is None:
                raise KeyError(incident_id)
            if record.status != IncidentStatus.AWAITING_APPROVAL:
                raise InvalidIncidentStateError(
                    f"incident {incident_id} is {record.status.value}, not awaiting approval"
                )
            HITL_PENDING.dec()
            await self.repository.update_status(
                incident_id, IncidentStatus.INVESTIGATING, {"approval": decision.model_dump()}
            )
            try:
                await self.graph.ainvoke(
                    Command(resume=decision.model_dump()), thread_config(incident_id)
                )
            except Exception as exc:
                return await self._fail(IncidentEvent.model_validate(record.event), exc)
            return await self._sync(incident_id)

    def resume_in_background(self, incident_id: str, decision: ApprovalDecision) -> None:
        self._spawn(self._guarded(self.resume(incident_id, decision), incident_id))

    async def get_state(self, incident_id: str) -> dict[str, Any] | None:
        snapshot = await self.graph.aget_state(thread_config(incident_id))
        return dict(snapshot.values) if snapshot and snapshot.values else None

    # ------------------------------------------------------------ internals
    async def _sync(self, incident_id: str) -> IncidentState:
        snapshot = await self.graph.aget_state(thread_config(incident_id))
        values: dict[str, Any] = dict(snapshot.values)
        event = IncidentEvent.model_validate(values["event"])
        summary = summarize(values)

        if snapshot.next:
            interrupts = getattr(snapshot, "interrupts", ()) or ()
            summary["pending"] = [getattr(i, "value", None) for i in interrupts]
            status = IncidentStatus.AWAITING_APPROVAL
            HITL_PENDING.inc()
        else:
            try:
                status = IncidentStatus(values.get("status", "failed"))
            except ValueError:
                status = IncidentStatus.FAILED
            if not status.is_terminal:
                status = IncidentStatus.FAILED
            record_terminal(
                status.value, time.time() - float(values.get("started_at", time.time()))
            )
            # Escalated incidents keep their dedupe marker for the full window:
            # humans now own them, and a still-firing alert must not page again.
            if status == IncidentStatus.RESOLVED:
                await self._remember(event, values)
                await self.queue.release(event)

        await self.repository.update_status(incident_id, status, summary)
        logger.info("incident %s -> %s", incident_id, status.value)
        return values  # type: ignore[return-value]

    async def _remember(self, event: IncidentEvent, values: dict[str, Any]) -> None:
        hypothesis = values.get("hypothesis") or {}
        groups = values.get("trace_groups", [])
        try:
            await self.repository.remember(
                event.id,
                event.service,
                self.deps.embedder.embed(memory_text(event.title, groups)),
                {
                    "title": event.title,
                    "root_cause": hypothesis.get("root_cause"),
                    "action": hypothesis.get("action"),
                    "summary": hypothesis.get("summary"),
                },
            )
        except Exception as exc:
            logger.warning("failed to store incident memory for %s: %s", event.id, exc)

    async def _fail(self, event: IncidentEvent, exc: BaseException) -> IncidentState:
        logger.exception("incident %s failed", event.id, exc_info=exc)
        record_terminal(IncidentStatus.FAILED.value, 0.0)
        await self.queue.release(event)
        await self.repository.update_status(
            event.id, IncidentStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
        )
        return {"incident_id": event.id, "status": IncidentStatus.FAILED.value}

    async def _guarded(self, coro: Any, incident_id: str) -> None:
        try:
            await coro
        except InvalidIncidentStateError as exc:
            logger.warning("%s", exc)
        except Exception:
            logger.exception("background task for incident %s crashed", incident_id)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def serve(self, stop: asyncio.Event) -> None:
        """Consume the queue until ``stop`` is set, running up to N incidents at once."""
        semaphore = asyncio.Semaphore(self.deps.settings.worker_concurrency)

        async def handle(event: IncidentEvent) -> None:
            try:
                await self._guarded(self.run(event), event.id)
            finally:
                semaphore.release()

        logger.info("worker started (concurrency=%d)", self.deps.settings.worker_concurrency)
        while not stop.is_set():
            try:
                event = await self.queue.dequeue(timeout=1.0)
            except Exception as exc:
                logger.warning("queue unavailable: %s", exc)
                await asyncio.sleep(1.0)
                continue
            if event is None:
                continue
            await semaphore.acquire()
            self._spawn(handle(event))
        await self.drain()

    async def drain(self, timeout: float = 30.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)
