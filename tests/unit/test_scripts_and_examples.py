from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from examples.newrelic_telemetry import NewRelicTelemetryProvider
from resilio.ingest import normalize
from resilio.providers import registry
from scripts import mock_outage_simulator, seed_eval_benchmark
from tests.helpers import mock_client


@pytest.mark.parametrize("source", ["sentry", "datadog", "alertmanager"])
def test_simulator_payloads_normalize_as_synthetic(source: str) -> None:
    import random

    builder = mock_outage_simulator.BUILDERS[source]
    template = mock_outage_simulator.get_scenario("deadlock")
    payload = builder(template, "orders", "production", random.Random(1))
    [event] = normalize(source, payload)
    assert event.service == "orders"
    assert event.synthetic is True
    assert event.incident_type == "deadlock"


def test_simulator_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        mock_outage_simulator.main(["--type", "disk-full", "--source", "datadog", "--dry-run"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["alert_transition"] == "Triggered"


def test_simulator_reports_unreachable_server(capsys: pytest.CaptureFixture[str]) -> None:
    assert mock_outage_simulator.main(["--url", "http://127.0.0.1:9", "--secret", "x"]) == 2


def test_seed_script_writes_benchmark(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "s.json"
    assert seed_eval_benchmark.main(["-o", str(output), "-n", "10"]) == 0
    assert len(json.loads(output.read_text())) == 10
    assert "wrote 10 scenarios" in capsys.readouterr().out


async def test_example_plugin_registers_and_fetches() -> None:
    assert registry.resolve("telemetry", "newrelic") is NewRelicTelemetryProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert "appName = 'checkout'" in json.loads(request.content)["query"]
        rows = [
            {
                "timestamp": 1,
                "error.class": "TimeoutError",
                "error.message": "slow",
                "trace.id": "t",
            }
        ]
        return httpx.Response(
            200, json={"data": {"actor": {"account": {"nrql": {"results": rows}}}}}
        )

    provider = NewRelicTelemetryProvider("key", 42, client=mock_client(handler))
    [trace] = await provider.fetch_traces("checkout", 15)
    assert trace == {
        "timestamp": "1",
        "message": "slow",
        "error_type": "TimeoutError",
        "trace_id": "t",
    }
