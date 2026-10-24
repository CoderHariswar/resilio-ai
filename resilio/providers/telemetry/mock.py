"""Zero-dependency telemetry provider that fabricates realistic outages.

Call :meth:`inject_outage` (the graph does this for ``synthetic`` events) and
``fetch_traces`` returns hundreds of noisy, timestamped log lines for that
scenario. Remediation providers can :meth:`heal` the service, which drops the
reported error rate so ``verify_health`` succeeds.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from resilio.providers.base import BaseTelemetryProvider
from resilio.scenarios import NOISE_MESSAGES, ScenarioTemplate, get_scenario

UNHEALTHY_ERROR_RATE = 0.34
HEALTHY_ERROR_RATE = 0.004


@runtime_checkable
class SupportsFaultInjection(Protocol):
    """Telemetry providers that can simulate outages (used in Mock Mode)."""

    def inject_outage(
        self, service_name: str, incident_type: str, seed: int | None = None
    ) -> None: ...


@dataclass(slots=True)
class ActiveOutage:
    scenario: ScenarioTemplate
    started_at: datetime
    seed: int
    healed: bool = False


def _stable_seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:4], "big")


class MockTelemetryProvider(BaseTelemetryProvider):
    name = "mock"

    def __init__(self, traces_per_fetch: int = 240) -> None:
        self.traces_per_fetch = traces_per_fetch
        self._outages: dict[str, ActiveOutage] = {}

    def inject_outage(self, service_name: str, incident_type: str, seed: int | None = None) -> None:
        """Start (or replace) a synthetic outage on ``service_name``."""
        scenario = get_scenario(incident_type)
        self._outages[service_name] = ActiveOutage(
            scenario=scenario,
            started_at=datetime.now(UTC) - timedelta(minutes=6),
            seed=seed if seed is not None else _stable_seed(service_name, incident_type),
        )

    def active_outage(self, service_name: str) -> ActiveOutage | None:
        return self._outages.get(service_name)

    def heal(self, service_name: str) -> None:
        outage = self._outages.get(service_name)
        if outage:
            outage.healed = True

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        outage = self._outages.get(service_name)
        if outage is None or outage.healed:
            return []
        return generate_traces(
            outage.scenario,
            service_name,
            count=self.traces_per_fetch,
            seed=outage.seed,
            start=outage.started_at,
            end=min(datetime.now(UTC), outage.started_at + timedelta(minutes=lookback_minutes)),
        )

    async def get_error_rate(self, service_name: str) -> float | None:
        outage = self._outages.get(service_name)
        if outage is None or outage.healed:
            return HEALTHY_ERROR_RATE
        return UNHEALTHY_ERROR_RATE


def generate_traces(
    scenario: ScenarioTemplate,
    service_name: str,
    *,
    count: int,
    seed: int,
    start: datetime,
    end: datetime,
    noise_ratio: float = 0.15,
    extra_messages: tuple[tuple[str, str], ...] = (),
) -> list[dict[str, Any]]:
    """Deterministically generate ``count`` trace dicts for ``scenario``."""
    rng = random.Random(seed)
    span = max((end - start).total_seconds(), 1.0)
    pods = [
        f"{service_name}-{rng.randrange(16**5):05x}-{rng.randrange(16**5):05x}" for _ in range(3)
    ]
    signal = scenario.error_messages + extra_messages
    traces: list[dict[str, Any]] = []
    for _ in range(count):
        is_noise = rng.random() < noise_ratio
        error_type, template = rng.choice(NOISE_MESSAGES if is_noise else signal)
        message = template.format(
            n=rng.randint(2, 999),
            ms=rng.randint(250, 30_000),
            id=uuid.UUID(int=rng.getrandbits(128)),
            pod=rng.choice(pods),
        )
        timestamp = start + timedelta(seconds=rng.uniform(0, span))
        traces.append(
            {
                "timestamp": timestamp.isoformat(),
                "service": service_name,
                "level": "warning" if is_noise else "error",
                "message": message,
                "error_type": error_type,
                "trace_id": f"{rng.getrandbits(64):016x}",
                "span_id": f"{rng.getrandbits(32):08x}",
                "attributes": {"pod": rng.choice(pods)},
            }
        )
    traces.sort(key=lambda t: t["timestamp"])
    return traces
