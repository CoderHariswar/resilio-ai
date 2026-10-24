"""Domain models shared by the API, the graph and the providers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class IncidentStatus(StrEnum):
    RECEIVED = "received"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {IncidentStatus.RESOLVED, IncidentStatus.ESCALATED, IncidentStatus.FAILED}


class RemediationAction(StrEnum):
    ROLLBACK = "rollback"
    RESTART = "restart"
    SCALE_UP = "scale_up"
    NONE = "none"


class TraceDict(TypedDict, total=False):
    """Canonical shape of one element returned by ``fetch_traces``.

    Providers return plain dicts (so plugins need no imports from us), but
    should populate these keys where the backend has them.
    """

    timestamp: str
    service: str
    level: str
    message: str
    error_type: str
    trace_id: str
    span_id: str
    attributes: dict[str, Any]


class IncidentEvent(BaseModel):
    """A normalized alert, independent of the source (Sentry, Datadog, ...)."""

    id: str = Field(default_factory=new_id)
    source: str = "generic"
    service: str
    title: str
    severity: Severity = Severity.HIGH
    environment: str = "production"
    incident_type: str | None = Field(
        default=None, description="Optional classifier hint, e.g. 'memory-leak'."
    )
    occurred_at: datetime = Field(default_factory=utcnow)
    sample_logs: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool = False

    @property
    def dedupe_key(self) -> str:
        return f"{self.environment}:{self.service}:{self.title.lower().strip()}"


class Deployment(BaseModel):
    """One entry from a service's deployment history (newest first)."""

    sha: str
    deployed_at: datetime
    author: str = "unknown"
    message: str = ""
    version: str | None = None


class NotificationKind(StrEnum):
    INFO = "info"
    APPROVAL_REQUEST = "approval_request"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class Notification(BaseModel):
    """A provider-agnostic message rendered by each notifier."""

    incident_id: str
    kind: NotificationKind
    title: str
    body: str
    severity: Severity = Severity.HIGH
    service: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    links: dict[str, str] = Field(default_factory=dict)


class IncidentRecord(BaseModel):
    """Persisted view of an incident (one row in ``incidents``)."""

    id: str
    service: str
    title: str
    severity: Severity
    source: str
    status: IncidentStatus
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    event: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    """Payload used to resume a workflow paused at the human approval gate."""

    approved: bool
    approver: str = "anonymous"
    comment: str = ""


ApprovalOutcome = Literal["approved", "rejected"]
