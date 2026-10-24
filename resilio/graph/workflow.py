"""The incident-response state machine.

::

    START -> telemetry -> vcs -> synthesizer -> evaluator --accepted--> policy
                                    ^              |                     |
                                    +--rejected----+ (<= MAX_RETRIES)    |
                                                   +--exhausted--> escalate
    policy --auto--> remediation --healthy--> END
    policy --approval--> request_approval -> approval_gate (interrupt) --approved--> remediation
    policy/approval_gate/remediation --otherwise--> escalate -> END

``approval_gate`` calls :func:`langgraph.types.interrupt`, a dynamic breakpoint.
State is checkpointed per incident (``thread_id = incident_id``), so a human
can approve hours later and any worker can resume it with
``Command(resume={"approved": True, ...})``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from resilio.graph.nodes.approval_node import (
    make_approval_gate_node,
    make_request_approval_node,
    route_after_approval,
)
from resilio.graph.nodes.escalation_node import make_escalation_node
from resilio.graph.nodes.evaluator_node import make_evaluation_router, make_evaluator_node
from resilio.graph.nodes.policy_node import make_policy_node, route_after_policy
from resilio.graph.nodes.remediation_node import make_remediation_node, route_after_remediation
from resilio.graph.nodes.synthesizer_node import make_synthesizer_node
from resilio.graph.nodes.telemetry_node import make_telemetry_node
from resilio.graph.nodes.vcs_node import make_vcs_node
from resilio.graph.state import IncidentState

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from resilio.core.database import DictPool
    from resilio.graph.deps import WorkflowDeps

APPROVAL_NODE = "approval_gate"


def build_workflow(deps: WorkflowDeps, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    """Compile the incident graph with the given dependencies and checkpointer."""
    graph = StateGraph(IncidentState)
    nodes: dict[str, Any] = {
        "telemetry": make_telemetry_node(deps),
        "vcs": make_vcs_node(deps),
        "synthesizer": make_synthesizer_node(deps),
        "evaluator": make_evaluator_node(deps),
        "policy": make_policy_node(deps),
        "request_approval": make_request_approval_node(deps),
        APPROVAL_NODE: make_approval_gate_node(deps),
        "remediation": make_remediation_node(deps),
        "escalate": make_escalation_node(deps),
    }
    for name, node in nodes.items():
        graph.add_node(name, node)

    graph.add_edge(START, "telemetry")
    graph.add_edge("telemetry", "vcs")
    graph.add_edge("vcs", "synthesizer")
    graph.add_edge("synthesizer", "evaluator")
    graph.add_conditional_edges(
        "evaluator",
        make_evaluation_router(deps),
        {"policy": "policy", "synthesizer": "synthesizer", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "policy",
        route_after_policy,
        {
            "remediation": "remediation",
            "request_approval": "request_approval",
            "escalate": "escalate",
        },
    )
    graph.add_edge("request_approval", APPROVAL_NODE)
    graph.add_conditional_edges(
        APPROVAL_NODE, route_after_approval, {"remediation": "remediation", "escalate": "escalate"}
    )
    graph.add_conditional_edges(
        "remediation", route_after_remediation, {"resolved": END, "escalate": "escalate"}
    )
    graph.add_edge("escalate", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


async def build_checkpointer(pool: DictPool | None) -> BaseCheckpointSaver[Any]:
    """PostgreSQL checkpointer when a pool is available, otherwise in-memory."""
    if pool is None:
        return InMemorySaver()
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    return saver


def thread_config(incident_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": incident_id}}
