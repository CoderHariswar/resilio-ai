"""REST routes (``/api/v1``) and a minimal approval review page."""

from __future__ import annotations

import html
import json
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from resilio.container import Container
from resilio.core.metrics import INCIDENTS_DEDUPLICATED_TOTAL, INCIDENTS_RECEIVED_TOTAL
from resilio.core.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    verify_signature,
)
from resilio.ingest import UnsupportedSourceError, normalize
from resilio.models import (
    ApprovalDecision,
    IncidentEvent,
    IncidentRecord,
    IncidentStatus,
    Severity,
)
from resilio.providers.registry import registry

router = APIRouter(prefix="/api/v1")
ui_router = APIRouter(include_in_schema=False)


# --------------------------------------------------------------------------- #
# Schemas & dependencies
# --------------------------------------------------------------------------- #
class IncidentCreate(BaseModel):
    service: str
    title: str
    severity: Severity = Severity.HIGH
    environment: str = "production"
    incident_type: str | None = None
    sample_logs: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    synthetic: bool = False


class IngestResponse(BaseModel):
    accepted: list[str]
    deduplicated: int = 0


class ApprovalRequest(BaseModel):
    approver: str = "api"
    comment: str = ""


class IncidentDetail(BaseModel):
    incident: IncidentRecord
    state: dict[str, Any] | None = None


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def require_api_token(
    container: ContainerDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Enforce ``Authorization: Bearer <API_TOKEN>`` when ``API_TOKEN`` is configured."""
    expected = container.settings.api_token
    if expected is None:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API token")


async def _accept(container: Container, events: list[IncidentEvent]) -> IngestResponse:
    accepted: list[str] = []
    duplicates = 0
    for event in events:
        if await container.queue.enqueue(event):
            await container.deps.repository.create(event)
            INCIDENTS_RECEIVED_TOTAL.labels(source=event.source).inc()
            accepted.append(event.id)
        else:
            INCIDENTS_DEDUPLICATED_TOTAL.inc()
            duplicates += 1
    return IngestResponse(accepted=accepted, deduplicated=duplicates)


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
@router.post(
    "/webhooks/{source}", status_code=status.HTTP_202_ACCEPTED, response_model=IngestResponse
)
async def ingest_webhook(
    source: str,
    request: Request,
    container: ContainerDep,
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
    timestamp: Annotated[str | None, Header(alias=TIMESTAMP_HEADER)] = None,
) -> IngestResponse:
    """Receive an alert from Sentry, Datadog, Alertmanager or a generic source."""
    body = await request.body()
    secret = container.settings.webhook_secret
    if secret is not None:
        try:
            verify_signature(secret.get_secret_value(), body, signature, timestamp)
        except SignatureError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid signature: {exc}") from exc
    try:
        payload = json.loads(body or b"{}")
        events = normalize(source, payload)
    except UnsupportedSourceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"bad payload: {exc}") from exc
    return await _accept(container, events)


@router.post(
    "/incidents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestResponse,
    dependencies=[Depends(require_api_token)],
)
async def create_incident(body: IncidentCreate, container: ContainerDep) -> IngestResponse:
    """Open an incident directly (used by ``resilio dev simulate``)."""
    event = IncidentEvent(source="api", **body.model_dump())
    return await _accept(container, [event])


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
@router.get("/incidents", response_model=list[IncidentRecord])
async def list_incidents(
    container: ContainerDep,
    status_filter: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[IncidentRecord]:
    records: list[IncidentRecord] = await container.deps.repository.list_incidents(
        status_filter, limit
    )
    return records


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def get_incident(incident_id: str, container: ContainerDep) -> IncidentDetail:
    record = await container.deps.repository.get(incident_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    state = await container.runner.get_state(incident_id)
    if state is not None:
        state = {k: v for k, v in state.items() if k not in {"event"}}
    return IncidentDetail(incident=record, state=state)


@router.get("/providers")
async def list_providers(container: ContainerDep) -> dict[str, Any]:
    deps = container.deps
    return {
        "active": {
            "telemetry": deps.telemetry.name,
            "remediation": deps.remediation.name,
            "notifiers": container.settings.notifiers,
            "llm": deps.llm.name,
            "confidence": deps.confidence_scorer.name,
        },
        "available": {
            kind: registry.available(kind)
            for kind in ("telemetry", "remediation", "notifiers", "llm", "confidence")
        },
        "mock_mode": container.settings.is_mock_mode,
    }


# --------------------------------------------------------------------------- #
# Human-in-the-loop
# --------------------------------------------------------------------------- #
async def _decide(
    incident_id: str, container: Container, decision: ApprovalDecision
) -> dict[str, str]:
    record = await container.deps.repository.get(incident_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    if record.status != IncidentStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"incident is {record.status.value}, not awaiting approval"
        )
    container.runner.resume_in_background(incident_id, decision)
    return {"incident_id": incident_id, "status": "resuming", "approved": str(decision.approved)}


@router.post(
    "/incidents/{incident_id}/approve",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_token)],
)
async def approve_incident(
    incident_id: str, container: ContainerDep, body: ApprovalRequest | None = None
) -> dict[str, str]:
    body = body or ApprovalRequest()
    return await _decide(
        incident_id,
        container,
        ApprovalDecision(approved=True, approver=body.approver, comment=body.comment),
    )


@router.post(
    "/incidents/{incident_id}/reject",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_token)],
)
async def reject_incident(
    incident_id: str, container: ContainerDep, body: ApprovalRequest | None = None
) -> dict[str, str]:
    body = body or ApprovalRequest()
    return await _decide(
        incident_id,
        container,
        ApprovalDecision(approved=False, approver=body.approver, comment=body.comment),
    )


_REVIEW_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ResilioAI review</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;
color:#1b1f24;background:#fff}}@media(prefers-color-scheme:dark){{body{{color:#e6edf3;background:#0d1117}}}}
dt{{font-weight:600;margin-top:.6rem}}button{{font:inherit;padding:.5rem 1.2rem;margin-right:.6rem;
border-radius:6px;border:0;cursor:pointer}}.ok{{background:#1f883d;color:#fff}}.no{{background:#cf222e;color:#fff}}
pre{{white-space:pre-wrap}}</style></head><body>
<h1>{title}</h1><p>Status: <strong>{status}</strong></p><dl>{rows}</dl>
<p><input id="who" placeholder="your name" value="web"> <input id="tok" type="password"
placeholder="API token (if required)"></p>
<button class="ok" onclick="act('approve')">Approve</button>
<button class="no" onclick="act('reject')">Reject</button><pre id="out"></pre>
<script>async function act(kind){{const t=document.getElementById('tok').value;
const r=await fetch('/api/v1/incidents/{incident_id}/'+kind,{{method:'POST',
headers:Object.assign({{'Content-Type':'application/json'}},t?{{Authorization:'Bearer '+t}}:{{}}),
body:JSON.stringify({{approver:document.getElementById('who').value}})}});
document.getElementById('out').textContent=r.status+' '+await r.text();}}</script></body></html>"""


@ui_router.get("/ui/incidents/{incident_id}", response_class=HTMLResponse)
async def review_page(incident_id: str, container: ContainerDep) -> HTMLResponse:
    record = await container.deps.repository.get(incident_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    hypothesis = record.summary.get("hypothesis") or {}
    policy = record.summary.get("policy") or {}
    rows = {
        "Service": record.service,
        "Root cause": hypothesis.get("root_cause", "-"),
        "Summary": hypothesis.get("summary", "-"),
        "Action": f"{hypothesis.get('action', '-')} {hypothesis.get('target_sha') or ''}",
        "Confidence": str(hypothesis.get("confidence", "-")),
        "Risk": f"{policy.get('risk', '-')} ({'; '.join(policy.get('reasons', []))})",
    }
    return HTMLResponse(
        _REVIEW_PAGE.format(
            title=html.escape(record.title),
            status=html.escape(record.status.value),
            incident_id=html.escape(incident_id),
            rows="".join(
                f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>" for k, v in rows.items()
            ),
        )
    )
