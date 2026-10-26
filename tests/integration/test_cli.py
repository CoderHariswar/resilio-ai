"""CLI commands, exercised against a live in-process API where needed."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from resilio import cli
from tests.integration.test_api import _client, _settings, wait_for

pytestmark = pytest.mark.integration

runner = CliRunner()


class _Borrowed:
    """Lend an already-running TestClient to code that uses ``with client:``."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def __enter__(self) -> TestClient:
        return self.client

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(_settings()) as client:
        monkeypatch.setattr(cli, "_client", lambda url, token: _Borrowed(client))
        yield client


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MOCK_LLM_FAULT_RATE", "0")
    monkeypatch.setenv("NOTIFIERS", "mock")
    monkeypatch.setattr(cli.console, "width", 200)
    cli.get_settings.cache_clear()


def invoke(*args: str) -> Any:
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result


def test_scenarios_providers_version() -> None:
    assert "memory-leak" in invoke("dev", "scenarios").output
    assert "datadog" in invoke("providers").output
    assert invoke("version").output.strip()


def test_simulate_local_auto_approve() -> None:
    output = invoke("dev", "simulate", "--type", "memory-leak", "--local", "--auto-approve").output
    assert "resolved" in output
    assert "approval_gate" in output


def test_simulate_local_prompt_rejected() -> None:
    result = runner.invoke(cli.app, ["dev", "simulate", "-t", "bad-deploy", "--local"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "escalated" in result.output


def test_simulate_unknown_type() -> None:
    result = runner.invoke(cli.app, ["dev", "simulate", "--type", "meteor"])
    assert result.exit_code == 2
    assert "unknown incident type" in result.output


def test_simulate_remote_status_approve_show(api: TestClient) -> None:
    output = invoke("dev", "simulate", "-t", "config-drift", "-s", "web", "--timeout", "10").output
    assert "Waiting for a human" in output
    incident_id = api.get("/api/v1/incidents").json()[0]["id"]

    assert incident_id in invoke("status", "--source", "api").output
    assert "awaiting_approval" in invoke("status", "--all").output
    assert "approved" in invoke("approve", incident_id, "--approver", "me").output
    wait_for(api, incident_id, "resolved")
    shown = invoke("show", incident_id).output
    assert "resolved" in shown
    assert "Confidence" in shown and "deploy_timing" in shown and "model said" in shown

    duplicate = invoke("dev", "simulate", "-t", "config-drift", "-s", "web", "--no-follow").output
    assert "Deduplicated" not in duplicate, "resolved incidents release their dedupe key"


def test_reject_and_replay(api: TestClient, tmp_path: Path) -> None:
    invoke("dev", "simulate", "-t", "deadlock", "-s", "orders", "--no-follow")
    incident_id = api.get("/api/v1/incidents").json()[0]["id"]
    wait_for(api, incident_id, "awaiting_approval")
    assert "rejected" in invoke("reject", incident_id).output

    replayed = json.loads(invoke("dev", "replay", incident_id).output)
    assert len(replayed["accepted"]) == 1

    payload = tmp_path / "sentry.json"
    payload.write_text(
        json.dumps({"data": {"event": {"title": "boom", "tags": [["service", "x"]]}}})
    )
    from_file = json.loads(invoke("dev", "replay", str(payload), "--source", "sentry").output)
    assert len(from_file["accepted"]) == 1


def test_api_errors_are_reported(api: TestClient) -> None:
    result = runner.invoke(cli.app, ["approve", "does-not-exist"])
    assert result.exit_code == 1
    assert "404" in result.output


def test_unreachable_server_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["status", "--api-url", "http://127.0.0.1:9"])
    assert result.exit_code == 2
    assert "Cannot reach ResilioAI" in result.output
    assert runner.invoke(cli.app, ["status", "--source", "db"]).exit_code == 2


def test_eval_seed_and_run(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.json"
    invoke("eval", "seed", "--output", str(path), "--count", "20")
    assert len(json.loads(path.read_text())) == 20

    table = invoke("eval", "run", "--scenarios", str(path)).output
    assert "hallucination escape rate" in table
    assert "By incident type" in table

    report = json.loads(invoke("eval", "run", "--count", "10", "--json").output)
    assert report["scenarios"] == 10

    gated = runner.invoke(cli.app, ["eval", "run", "--count", "10", "--min-precision", "1.01"])
    assert gated.exit_code == 1
