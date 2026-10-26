"""``resilio`` - the developer CLI.

Examples::

    resilio dev simulate --type memory-leak          # fire into a running server
    resilio dev simulate --type disk-full --local    # run the whole graph in-process
    resilio status                                   # pending human approvals
    resilio approve <incident-id>
    resilio eval run                                 # 50-scenario offline benchmark
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from resilio import __version__
from resilio.config import Settings, get_settings
from resilio.models import (
    ApprovalDecision,
    IncidentEvent,
    IncidentRecord,
    IncidentStatus,
    Severity,
)
from resilio.scenarios import SCENARIO_TYPES, SCENARIOS, get_scenario

console = Console()

app = typer.Typer(
    name="resilio",
    help="ResilioAI developer CLI: simulate outages, review approvals, benchmark the judge.",
    no_args_is_help=True,
    add_completion=False,
)
dev_app = typer.Typer(help="Local development helpers.", no_args_is_help=True)
eval_app = typer.Typer(help="Offline evaluation of the synthesizer and LLM-as-a-judge.")
app.add_typer(dev_app, name="dev")
app.add_typer(eval_app, name="eval")

ApiUrl = Annotated[
    str, typer.Option("--api-url", envvar="RESILIO_API_URL", help="ResilioAI server URL.")
]
ApiToken = Annotated[
    str | None, typer.Option("--api-token", envvar="RESILIO_API_TOKEN", help="Bearer token.")
]
DEFAULT_API = "http://localhost:8000"

STATUS_STYLE = {
    "received": "dim",
    "investigating": "cyan",
    "awaiting_approval": "yellow",
    "resolved": "green",
    "escalated": "red",
    "failed": "red",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _client(api_url: str, token: str | None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=api_url.rstrip("/"), headers=headers, timeout=15.0)


def _request(api_url: str, token: str | None, method: str, path: str, **kwargs: Any) -> Any:
    try:
        with _client(api_url, token) as client:
            response = client.request(method, path, **kwargs)
    except httpx.ConnectError:
        console.print(
            f"[red]Cannot reach ResilioAI at {api_url}.[/] Start it with "
            "[bold]docker compose up -d[/] or [bold]resilio serve[/], or use [bold]--local[/]."
        )
        raise typer.Exit(2) from None
    if response.status_code >= 400:
        console.print(f"[red]HTTP {response.status_code}[/] {response.text}")
        raise typer.Exit(1)
    return response.json()


def _age(ts: datetime) -> str:
    seconds = int((datetime.now(UTC) - ts).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _status(value: str) -> str:
    return f"[{STATUS_STYLE.get(value, 'white')}]{value}[/]"


def _print_timeline(entries: list[dict[str, Any]]) -> None:
    table = Table(title="Timeline", show_lines=False, expand=False)
    table.add_column("node", style="bold cyan")
    table.add_column("event")
    for entry in entries:
        table.add_row(entry.get("node", ""), entry.get("message", ""))
    console.print(table)


def _print_summary(record: IncidentRecord) -> None:
    hypothesis = record.summary.get("hypothesis") or {}
    stats = record.summary.get("token_stats") or {}
    console.print(
        f"\n[bold]Incident[/] {record.id}  {_status(record.status.value)}\n"
        f"[bold]Root cause[/] {hypothesis.get('root_cause', '-')}  "
        f"[bold]action[/] {hypothesis.get('action', '-')}  "
        f"[bold]confidence[/] {hypothesis.get('confidence', '-')}\n"
        f"[bold]Token savings[/] {stats.get('savings_percent', 0)}% "
        f"({stats.get('raw_tokens', 0)} -> {stats.get('compact_tokens', 0)} tokens)  "
        f"[bold]evaluator attempts[/] {record.summary.get('evaluator_attempts', 0)}"
    )
    if record.summary.get("escalation_reason"):
        console.print(f"[bold red]Escalation[/] {record.summary['escalation_reason']}")


def _synthetic_event(
    incident_type: str, service: str, severity: Severity | None, environment: str
) -> IncidentEvent:
    template = get_scenario(incident_type)
    return IncidentEvent(
        source="simulator",
        service=service,
        title=template.title,
        severity=severity or template.severity,
        environment=environment,
        incident_type=incident_type,
        synthetic=True,
        labels={"simulated": "true"},
    )


# --------------------------------------------------------------------------- #
# dev
# --------------------------------------------------------------------------- #
@dev_app.command("scenarios")
def dev_scenarios() -> None:
    """List the synthetic outage types available to ``simulate``."""
    table = Table(title="Synthetic outage library")
    for column in ("type", "severity", "ground-truth root cause", "expected action"):
        table.add_column(column)
    for name, template in SCENARIOS.items():
        table.add_row(
            name, template.severity.value, template.root_cause, template.recommended_action.value
        )
    console.print(table)


@dev_app.command("simulate")
def dev_simulate(
    incident_type: Annotated[
        str, typer.Option("--type", "-t", help=f"One of: {', '.join(SCENARIO_TYPES)}")
    ] = "memory-leak",
    service: Annotated[str, typer.Option("--service", "-s")] = "checkout",
    severity: Annotated[Severity | None, typer.Option(help="Override template severity.")] = None,
    environment: Annotated[str, typer.Option("--env")] = "production",
    local: Annotated[
        bool, typer.Option("--local", help="Run the graph in-process; no server needed.")
    ] = False,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="With --local, approve without prompting.")
    ] = False,
    follow: Annotated[bool, typer.Option(help="Wait and print the outcome.")] = True,
    timeout: Annotated[float, typer.Option(help="Seconds to wait when following.")] = 60.0,
    api_url: ApiUrl = DEFAULT_API,
    api_token: ApiToken = None,
) -> None:
    """Fire a synthetic incident into the local setup."""
    try:
        event = _synthetic_event(incident_type, service, severity, environment)
    except KeyError as exc:
        console.print(f"[red]{exc.args[0]}[/]")
        raise typer.Exit(2) from None

    if local:
        asyncio.run(_run_local(event, auto_approve=auto_approve))
        return

    body = event.model_dump(
        mode="json",
        include={
            "service",
            "title",
            "severity",
            "environment",
            "incident_type",
            "labels",
            "synthetic",
        },
    )
    result = _request(api_url, api_token, "POST", "/api/v1/incidents", json=body)
    if not result["accepted"]:
        console.print("[yellow]Deduplicated:[/] an identical alert is already open.")
        return
    incident_id = result["accepted"][0]
    console.print(
        f"[green]Fired[/] {incident_type} on [bold]{service}[/] -> incident {incident_id}"
    )
    if follow:
        _follow(api_url, api_token, incident_id, timeout)


def _follow(api_url: str, token: str | None, incident_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    with console.status("investigating..."):
        while True:
            detail = _request(api_url, token, "GET", f"/api/v1/incidents/{incident_id}")
            record = IncidentRecord.model_validate(detail["incident"])
            if record.status.is_terminal or record.status == IncidentStatus.AWAITING_APPROVAL:
                break
            if time.monotonic() > deadline:
                console.print(f"[yellow]Still {record.status.value} after {timeout:.0f}s.[/]")
                return
            time.sleep(1.0)
    _print_timeline(record.summary.get("timeline", []))
    _print_summary(record)
    if record.status == IncidentStatus.AWAITING_APPROVAL:
        console.print(
            f"\n[yellow]Waiting for a human.[/] Run [bold]resilio approve {incident_id}[/] "
            f"or [bold]resilio reject {incident_id}[/]."
        )


async def _run_local(event: IncidentEvent, *, auto_approve: bool) -> None:
    from resilio.container import build_container

    settings = get_settings().model_copy(
        update={"database_url": None, "redis_url": None, "health_check_timeout_seconds": 5}
    )
    container = await build_container(settings)
    try:
        repository = container.deps.repository
        await repository.create(event)
        console.rule(f"[bold]{event.title}[/] ({event.incident_type} on {event.service})")
        await container.runner.run(event)
        record = await repository.get(event.id)
        while record is not None and record.status == IncidentStatus.AWAITING_APPROVAL:
            hypothesis = record.summary.get("hypothesis") or {}
            question = (
                f"Approve {hypothesis.get('action')} "
                f"{hypothesis.get('target_sha') or ''} for {event.service}?"
            )
            approved = auto_approve or typer.confirm(question, default=False)
            await container.runner.resume(
                event.id,
                ApprovalDecision(approved=approved, approver=getpass.getuser(), comment="cli"),
            )
            record = await repository.get(event.id)
        if record is not None:
            _print_timeline(record.summary.get("timeline", []))
            _print_summary(record)
    finally:
        await container.aclose()


@dev_app.command("replay")
def dev_replay(
    target: Annotated[str, typer.Argument(help="Incident id, or path to a JSON payload file.")],
    source: Annotated[
        str, typer.Option(help="Webhook source for file payloads (sentry, datadog, ...).")
    ] = "generic",
    api_url: ApiUrl = DEFAULT_API,
    api_token: ApiToken = None,
) -> None:
    """Replay a past incident (by id) or a captured webhook payload (by file)."""
    path = Path(target)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = _request(api_url, api_token, "POST", f"/api/v1/webhooks/{source}", json=payload)
    else:
        detail = _request(api_url, api_token, "GET", f"/api/v1/incidents/{target}")
        event = detail["incident"]["event"]
        body = {
            key: event.get(key)
            for key in (
                "service",
                "severity",
                "environment",
                "incident_type",
                "sample_logs",
                "labels",
            )
        }
        body["title"] = f"{event['title']} [replay {datetime.now(UTC):%H:%M:%S}]"
        body["synthetic"] = event.get("synthetic", False)
        result = _request(api_url, api_token, "POST", "/api/v1/incidents", json=body)
    console.print_json(data=result)


# --------------------------------------------------------------------------- #
# status / approvals
# --------------------------------------------------------------------------- #
async def _pending_from_db(dsn: str) -> list[tuple[IncidentRecord, int]]:
    from resilio.core.database import PostgresIncidentRepository, create_pool

    pool = await create_pool(dsn, 1, 2)
    try:
        repo = PostgresIncidentRepository(pool)
        records = await repo.list_incidents(IncidentStatus.AWAITING_APPROVAL, limit=200)
        counts: dict[str, int] = {}
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT thread_id, count(*) AS n FROM checkpoints "
                    "WHERE thread_id = ANY(%s) GROUP BY thread_id",
                    ([r.id for r in records],),
                )
                counts = {row["thread_id"]: int(row["n"]) for row in await cur.fetchall()}
        except Exception:  # checkpoint tables absent (e.g. never ran with Postgres)
            counts = {}
        return [(r, counts.get(r.id, 0)) for r in records]
    finally:
        await pool.close()


@app.command("status")
def status_cmd(
    show_all: Annotated[
        bool, typer.Option("--all", help="Show recent incidents of any status.")
    ] = False,
    source: Annotated[
        str, typer.Option(help="auto | api | db. 'auto' reads PostgreSQL when DATABASE_URL is set.")
    ] = "auto",
    database_url: Annotated[str | None, typer.Option(envvar="DATABASE_URL")] = None,
    api_url: ApiUrl = DEFAULT_API,
    api_token: ApiToken = None,
) -> None:
    """Display pending Human-in-the-Loop checkpoints (or all recent incidents)."""
    use_db = source == "db" or (source == "auto" and database_url and not show_all)
    rows: list[tuple[IncidentRecord, int | None]]
    if use_db:
        if not database_url:
            console.print("[red]--source db requires DATABASE_URL[/]")
            raise typer.Exit(2)
        rows = list(asyncio.run(_pending_from_db(database_url)))
        origin = "postgresql"
    else:
        params: dict[str, Any] = {"limit": 50}
        if not show_all:
            params["status"] = IncidentStatus.AWAITING_APPROVAL.value
        data = _request(api_url, api_token, "GET", "/api/v1/incidents", params=params)
        rows = [(IncidentRecord.model_validate(item), None) for item in data]
        origin = api_url

    title = "Incidents" if show_all else "Pending human approvals"
    table = Table(title=f"{title} ({origin})")
    table.add_column("incident", no_wrap=True)
    for column in ("service", "status", "action", "risk", "confidence", "age"):
        table.add_column(column)
    if use_db:
        table.add_column("checkpoints", justify="right")
    for record, checkpoints in rows:
        hypothesis = record.summary.get("hypothesis") or {}
        policy = record.summary.get("policy") or {}
        action = hypothesis.get("action", "-")
        if hypothesis.get("target_sha"):
            action += f" -> {str(hypothesis['target_sha'])[:8]}"
        cells = [
            record.id,
            record.service,
            _status(record.status.value),
            action,
            policy.get("risk", "-"),
            str(hypothesis.get("confidence", "-")),
            _age(record.updated_at),
        ]
        if use_db:
            cells.append(str(checkpoints))
        table.add_row(*cells)
    console.print(table)
    if not rows:
        console.print("[dim]Nothing here.[/]")


def _decide(
    incident_id: str, approved: bool, approver: str, comment: str, api_url: str, token: str | None
) -> None:
    verb = "approve" if approved else "reject"
    result = _request(
        api_url,
        token,
        "POST",
        f"/api/v1/incidents/{incident_id}/{verb}",
        json={"approver": approver, "comment": comment},
    )
    past = "approved" if approved else "rejected"
    console.print(f"[green]{past}[/] {incident_id}: {result['status']}")


@app.command("approve")
def approve_cmd(
    incident_id: str,
    approver: Annotated[str, typer.Option()] = os.environ.get("USER", "cli"),
    comment: Annotated[str, typer.Option()] = "",
    api_url: ApiUrl = DEFAULT_API,
    api_token: ApiToken = None,
) -> None:
    """Approve the remediation proposed for a paused incident."""
    _decide(incident_id, True, approver, comment, api_url, api_token)


@app.command("reject")
def reject_cmd(
    incident_id: str,
    approver: Annotated[str, typer.Option()] = os.environ.get("USER", "cli"),
    comment: Annotated[str, typer.Option()] = "",
    api_url: ApiUrl = DEFAULT_API,
    api_token: ApiToken = None,
) -> None:
    """Reject the proposed remediation; the incident escalates to humans."""
    _decide(incident_id, False, approver, comment, api_url, api_token)


@app.command("show")
def show_cmd(incident_id: str, api_url: ApiUrl = DEFAULT_API, api_token: ApiToken = None) -> None:
    """Show an incident's timeline and outcome."""
    detail = _request(api_url, api_token, "GET", f"/api/v1/incidents/{incident_id}")
    record = IncidentRecord.model_validate(detail["incident"])
    _print_timeline(record.summary.get("timeline", []))
    _print_summary(record)


# --------------------------------------------------------------------------- #
# eval
# --------------------------------------------------------------------------- #
def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


@eval_app.command("seed")
def eval_seed(
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("benchmarks/scenarios.json"),
    count: int = 50,
    seed: int = 42,
) -> None:
    """Write the benchmark scenarios to a JSON file you can inspect or edit."""
    from resilio.utils.evaluator_benchmark import generate_scenarios, save_scenarios

    scenarios = generate_scenarios(count, seed)
    save_scenarios(scenarios, output)
    console.print(f"[green]Wrote {len(scenarios)} scenarios[/] to {output}")


@eval_app.command("run")
def eval_run(
    scenarios_path: Annotated[
        Path | None, typer.Option("--scenarios", help="JSON from `resilio eval seed`.")
    ] = None,
    count: int = 50,
    seed: int = 42,
    llm: Annotated[
        str | None, typer.Option(help="Override LLM_PROVIDER (e.g. openai, ollama).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
    min_precision: Annotated[
        float, typer.Option(help="Exit non-zero if judge precision is below this (CI gate).")
    ] = 0.0,
    max_hallucination_escape: Annotated[
        float, typer.Option(help="Exit non-zero if the hallucination escape rate exceeds this.")
    ] = 1.0,
) -> None:
    """Execute the offline benchmark and print judge precision and hallucination rates."""
    from resilio.providers.registry import build_llm_provider
    from resilio.utils.evaluator_benchmark import generate_scenarios, load_scenarios, run_benchmark

    settings: Settings = get_settings()
    if llm:
        settings = settings.model_copy(update={"llm_provider": llm})
    scenarios = (
        load_scenarios(scenarios_path) if scenarios_path else generate_scenarios(count, seed)
    )

    async def _run() -> Any:
        provider = build_llm_provider(settings)
        try:
            return await run_benchmark(scenarios, provider, settings)
        finally:
            await provider.aclose()

    with console.status(f"running {len(scenarios)} scenarios with {settings.llm_provider}..."):
        report = asyncio.run(_run())

    if as_json:
        console.print_json(data=report.to_dict())
    else:
        judge = Table(title=f"LLM-as-a-judge ({report.llm}, {report.scenarios} scenarios)")
        judge.add_column("metric")
        judge.add_column("value", justify="right")
        for name, value in (
            ("precision (accepted RCAs that were correct)", _pct(report.judge_precision)),
            ("recall (correct RCAs accepted)", _pct(report.judge_recall)),
            ("F1", f"{report.judge_f1:.3f}"),
            ("accuracy", _pct(report.judge_accuracy)),
            (
                "hallucination escape rate (fabricated SHA/evidence accepted)",
                _pct(report.hallucination_escape_rate),
            ),
            (
                "misattribution escape rate (wrong root cause accepted)",
                _pct(report.misattribution_escape_rate),
            ),
        ):
            judge.add_row(name, value)
        console.print(judge)

        pipeline = Table(title="End-to-end RCA pipeline (synthesize -> judge -> revise)")
        pipeline.add_column("metric")
        pipeline.add_column("value", justify="right")
        for name, value in (
            ("root-cause accuracy", _pct(report.rca_accuracy)),
            ("action accuracy", _pct(report.action_accuracy)),
            ("first-pass hallucination rate", _pct(report.first_pass_hallucination_rate)),
            ("accepted-but-wrong rate", _pct(report.final_hallucination_rate)),
            ("incidents needing evaluator correction", _pct(report.correction_rate)),
            ("escalated (no accepted RCA)", _pct(report.escalation_rate)),
            ("tokens", f"{report.tokens:,}"),
            ("wall time", f"{report.duration_seconds:.2f}s"),
        ):
            pipeline.add_row(name, value)
        console.print(pipeline)

        per_type = Table(title="By incident type")
        for column in ("type", "n", "judge accuracy", "rca accuracy", "corrections"):
            per_type.add_column(column, justify="right" if column != "type" else "left")
        for incident_type, row in report.by_type().items():
            per_type.add_row(
                incident_type,
                str(int(row["n"])),
                _pct(row["judge_accuracy"]),
                _pct(row["rca_accuracy"]),
                str(int(row["corrections"])),
            )
        console.print(per_type)

    failed = (
        report.judge_precision < min_precision
        or report.hallucination_escape_rate > max_hallucination_escape
    )
    if failed:
        console.print("[red]Benchmark gate failed.[/]")
        raise typer.Exit(1)


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
@app.command("providers")
def providers_cmd() -> None:
    """List available providers (built-in, registered and installed plugins)."""
    from resilio.providers.registry import registry
    from resilio.scoring.confidence import load_confidence_config

    settings = get_settings()
    active = {
        "telemetry": settings.telemetry_provider,
        "remediation": settings.remediation_provider,
        "notifiers": ",".join(settings.notifiers),
        "llm": settings.llm_provider,
        "confidence": load_confidence_config(settings.confidence_config_path).confidence.scorer,
    }
    table = Table(title="Providers")
    table.add_column("kind", style="bold")
    table.add_column("active", style="green")
    table.add_column("available")
    for kind in ("telemetry", "remediation", "notifiers", "llm", "confidence"):
        table.add_row(kind, active[kind], ", ".join(registry.available(kind)))
    console.print(table)


@app.command("serve")
def serve_cmd(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Hot-reload on code changes.")] = False,
) -> None:
    """Run the API server (with the embedded worker)."""
    import uvicorn

    uvicorn.run("resilio.main:app", host=host, port=port, reload=reload, log_config=None)


@app.command("version")
def version_cmd() -> None:
    """Print the ResilioAI version."""
    console.print(__version__)


if __name__ == "__main__":
    app()
