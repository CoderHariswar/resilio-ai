#!/usr/bin/env python3
"""Fire realistic Sentry / Datadog / Alertmanager webhooks at a ResilioAI server.

Unlike ``resilio dev simulate`` (which calls the internal API), this exercises
the real ingestion path: vendor payload -> HMAC check -> normalizer -> queue.

Examples::

    python scripts/mock_outage_simulator.py --type memory-leak --source sentry
    python scripts/mock_outage_simulator.py --type traffic-spike --source datadog --storm 20
    python scripts/mock_outage_simulator.py --type deadlock --source alertmanager --dry-run
    WEBHOOK_SECRET=s3cret python scripts/mock_outage_simulator.py --type disk-full

Payloads are tagged ``resilio-simulated:<type>`` so Mock Mode fabricates matching
telemetry; real deployments never see that tag.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from resilio.core.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, compute_signature
from resilio.ingest import SIMULATION_LABEL
from resilio.scenarios import SCENARIO_TYPES, ScenarioTemplate, get_scenario

SENTRY_LEVEL = {
    "critical": "fatal",
    "high": "error",
    "medium": "warning",
    "low": "info",
    "info": "info",
}

PayloadBuilder = Callable[[ScenarioTemplate, str, str, random.Random], dict[str, Any]]


def _sample(template: ScenarioTemplate, rng: random.Random) -> tuple[str, str]:
    error_type, message = rng.choice(template.error_messages)
    return error_type, message.format(
        n=rng.randint(2, 999),
        ms=rng.randint(250, 30_000),
        id=uuid.UUID(int=rng.getrandbits(128)),
        pod=f"pod-{rng.randrange(16**5):05x}",
    )


def sentry_payload(
    template: ScenarioTemplate, service: str, env: str, rng: random.Random
) -> dict[str, Any]:
    """Shape of a Sentry issue-alert webhook (``data.event``)."""
    exceptions = [
        {"type": error_type, "value": message}
        for error_type, message in (_sample(template, rng) for _ in range(2))
    ]
    return {
        "action": "triggered",
        "actor": {"type": "application", "name": "Sentry"},
        "data": {
            "triggered_rule": f"{template.incident_type} spike",
            "event": {
                "event_id": uuid.UUID(int=rng.getrandbits(128)).hex,
                "title": f"{exceptions[0]['type']}: {exceptions[0]['value']}"[:120],
                "level": SENTRY_LEVEL[template.severity.value],
                "environment": env,
                "datetime": datetime.now(UTC).isoformat(),
                "project": service,
                "tags": [
                    ["service", service],
                    ["environment", env],
                    [SIMULATION_LABEL, template.incident_type],
                ],
                "exception": {"values": exceptions},
            },
        },
    }


def datadog_payload(
    template: ScenarioTemplate, service: str, env: str, rng: random.Random
) -> dict[str, Any]:
    """Shape of a Datadog monitor webhook using the default JSON template."""
    _, message = _sample(template, rng)
    return {
        "id": str(rng.getrandbits(48)),
        "title": f"[Triggered] {template.title}",
        "body": message,
        "alert_type": "error",
        "alert_transition": "Triggered",
        "priority": "P1" if template.severity.value == "critical" else "P2",
        "date": int(time.time() * 1000),
        "tags": f"service:{service},env:{env},{SIMULATION_LABEL}:{template.incident_type}",
    }


def alertmanager_payload(
    template: ScenarioTemplate, service: str, env: str, rng: random.Random
) -> dict[str, Any]:
    """Shape of a Prometheus Alertmanager v4 webhook."""
    _, message = _sample(template, rng)
    return {
        "version": "4",
        "status": "firing",
        "receiver": "resilio",
        "commonLabels": {"env": env},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": template.incident_type.title().replace("-", ""),
                    "service": service,
                    "severity": template.severity.value,
                    SIMULATION_LABEL: template.incident_type,
                },
                "annotations": {"summary": template.title, "description": message},
                "startsAt": datetime.now(UTC).isoformat(),
            }
        ],
    }


BUILDERS: dict[str, PayloadBuilder] = {
    "sentry": sentry_payload,
    "datadog": datadog_payload,
    "alertmanager": alertmanager_payload,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--type", "-t", default="memory-leak", choices=SCENARIO_TYPES)
    parser.add_argument("--source", "-s", default="sentry", choices=sorted(BUILDERS))
    parser.add_argument("--service", default="checkout")
    parser.add_argument("--env", default="production")
    parser.add_argument("--storm", type=int, default=1, help="Send N copies to test dedupe.")
    parser.add_argument("--url", default=os.environ.get("RESILIO_API_URL", "http://localhost:8000"))
    parser.add_argument("--secret", default=os.environ.get("WEBHOOK_SECRET"), help="HMAC secret.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Print the payload, do not send.")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    template = get_scenario(args.type)
    payload = BUILDERS[args.source](template, args.service, args.env, rng)
    body = json.dumps(payload).encode()

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    headers = {"Content-Type": "application/json"}
    if args.secret:
        timestamp = str(int(time.time()))
        headers[TIMESTAMP_HEADER] = timestamp
        headers[SIGNATURE_HEADER] = compute_signature(args.secret, body, timestamp)

    accepted = deduplicated = 0
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=10) as client:
        for _ in range(args.storm):
            try:
                response = client.post(
                    f"/api/v1/webhooks/{args.source}", content=body, headers=headers
                )
            except httpx.ConnectError:
                print(f"cannot reach {args.url} - is `docker compose up` running?", file=sys.stderr)
                return 2
            if response.status_code >= 400:
                print(f"HTTP {response.status_code}: {response.text}", file=sys.stderr)
                return 1
            result = response.json()
            accepted += len(result["accepted"])
            deduplicated += result["deduplicated"]
            for incident_id in result["accepted"]:
                print(f"incident {incident_id} opened ({args.type} via {args.source})")
    print(f"sent {args.storm} webhook(s): {accepted} accepted, {deduplicated} deduplicated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
