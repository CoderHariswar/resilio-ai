# Contributing to ResilioAI

Thanks for helping make on-call less miserable. This guide gets you from
clone to merged PR, with a focus on the most common contribution: **a new
provider**.

- [Development setup](#development-setup)
- [Build a provider in under 50 lines](#build-a-provider-in-under-50-lines)
- [Remediation, notifier and LLM providers](#remediation-notifier-and-llm-providers)
- [Shipping a provider as a separate package](#shipping-a-provider-as-a-separate-package)
- [Testing](#testing)
- [Good first issues](#good-first-issues)
- [Pull requests](#pull-requests)
- [Architecture Decision Records](#architecture-decision-records)

## Development setup

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). Docker is optional.

```bash
git clone https://github.com/CoderHariswar/resilio-ai.git
cd resilio-ai
make install        # uv sync --extra dev
make hooks          # pre-commit: ruff, mypy --strict, unit tests

make dev            # API on :8000 with hot reload, Mock Mode, in-memory storage
uv run resilio dev simulate --type deadlock --local   # or run a whole incident in-process
make check          # what CI runs: lint + typecheck + tests
```

Prefer containers? `docker compose watch` syncs `./resilio` into the running
container and restarts it on every save.

Code style is enforced, not debated: `ruff format`, `ruff check`, and
`mypy --strict` on the `resilio` package. Public functions get docstrings and
full type hints.

## Build a provider in under 50 lines

A telemetry provider answers one question: *what errors has this service
emitted recently?* Here is a complete New Relic provider. It is also
committed as [`examples/newrelic_telemetry.py`](../examples/newrelic_telemetry.py)
and covered by `tests/unit/test_scripts_and_examples.py`, so it is known to work.

```python
from __future__ import annotations

import os
from typing import Any, Self

import httpx

from resilio.config import Settings
from resilio.providers import BaseTelemetryProvider, register_telemetry
from resilio.providers.http import build_client, request_json

NRQL = (
    "SELECT timestamp, error.class, error.message, trace.id FROM TransactionError "
    "WHERE appName = '{service}' SINCE {minutes} minutes ago LIMIT 200"
)
GRAPHQL = '{{ actor {{ account(id: {account}) {{ nrql(query: "{nrql}") {{ results }} }} }} }}'


@register_telemetry("newrelic")
class NewRelicTelemetryProvider(BaseTelemetryProvider):
    name = "newrelic"

    def __init__(self, api_key: str, account_id: int, client: httpx.AsyncClient | None = None):
        self.account_id = account_id
        self._client = client or build_client(
            "https://api.newrelic.com", headers={"API-Key": api_key}
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(os.environ["NEW_RELIC_API_KEY"], int(os.environ["NEW_RELIC_ACCOUNT_ID"]))

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        nrql = NRQL.format(service=service_name, minutes=lookback_minutes)
        query = GRAPHQL.format(account=self.account_id, nrql=nrql)
        data = await request_json(
            self._client, "POST", "/graphql", provider=self.name, json={"query": query}
        )
        return [
            {
                "timestamp": str(row.get("timestamp", "")),
                "message": row.get("error.message", ""),
                "error_type": row.get("error.class", "error"),
                "trace_id": row.get("trace.id", ""),
            }
            for row in data["data"]["actor"]["account"]["nrql"]["results"]
        ]
```

What each piece does:

| Piece | Why |
|---|---|
| `BaseTelemetryProvider` | The contract. Only `fetch_traces` is abstract. |
| `@register_telemetry("newrelic")` | Makes `TELEMETRY_PROVIDER=newrelic` work. |
| `from_settings` | Builds the provider from configuration. Raise a clear `ValueError` when something is missing; it surfaces at startup, not mid-incident. |
| `client: httpx.AsyncClient \| None` | Lets tests inject `httpx.MockTransport`. |
| `request_json` | Shared retry/backoff policy and consistent `ProviderError`s. |
| returned dict keys | Follow [`TraceDict`](../resilio/models.py): `timestamp`, `message`, `error_type`, `trace_id`, optionally `level`, `span_id`, `attributes`. |

You do **not** need to redact, deduplicate or truncate: the telemetry node
does that for every provider. Optionally implement
`get_error_rate(service) -> float | None` so remediation providers can confirm
recovery from your backend.

To ship it in-tree:

1. Save it as `resilio/providers/telemetry/newrelic.py` (drop the decorator).
2. Add `"newrelic": "resilio.providers.telemetry.newrelic:NewRelicTelemetryProvider"`
   to `_BUILTINS["telemetry"]` in [`registry.py`](../resilio/providers/registry.py).
3. Add its settings to [`config.py`](../resilio/config.py) and `.env.example`
   (use `SecretStr` for credentials) and read them in `from_settings`.
4. Add a test in `tests/unit/test_telemetry_providers.py`.
5. Add a row to the provider table in the README.

## Remediation, notifier and LLM providers

Same pattern, different contract:

```python
class BaseRemediationProvider:
    async def execute_rollback(self, service_name: str, target_sha: str) -> bool: ...  # required
    async def verify_health(self, service_name: str, timeout_seconds: int) -> bool: ...  # required
    async def recent_deployments(self, service_name: str, limit: int = 10) -> list[Deployment]: ...
    async def restart_service(self, service_name: str) -> bool: ...  # optional
    async def scale_service(self, service_name: str, replicas_delta: int) -> bool: ...  # optional


class BaseNotifier:
    async def send(self, notification: Notification) -> bool: ...  # required


class BaseLLMProvider:
    async def complete(self, request: LLMRequest) -> LLMResponse: ...  # required
```

Guidelines specific to remediation providers, because they change production:

- **Implement `recent_deployments`.** Without history the VCS node cannot
  choose a rollback target, and the policy engine will refuse rollbacks.
- **`verify_health` must actually verify.** Combine your platform's rollout
  status with `telemetry_recovered(self.telemetry, ...)` from
  `providers/remediation/_health.py`, and poll with `wait_until`.
- Leave optional actions unimplemented rather than faking them; the
  remediation node escalates cleanly on `NotImplementedError`.
- Document the minimum permissions in the module docstring.

LLM providers that speak the OpenAI chat-completions protocol are a
three-line subclass of `OpenAICompatibleProvider` (see `OllamaProvider`).

## Shipping a provider as a separate package

You don't need to land code here to integrate. Publish a package that declares
an entry point:

```toml
# your package's pyproject.toml
[project.entry-points."resilio.telemetry"]
newrelic = "resilio_newrelic:NewRelicTelemetryProvider"
```

`pip install resilio-newrelic`, set `TELEMETRY_PROVIDER=newrelic`, and the
registry discovers it. The groups are `resilio.telemetry`,
`resilio.remediation`, `resilio.notifiers` and `resilio.llm`.
`resilio providers` lists everything that was discovered.

## Testing

```bash
uv run pytest                    # everything (Postgres tests skip without TEST_DATABASE_URL)
uv run pytest tests/unit -q      # fast loop
make test-pg                     # spins up pgvector in Docker and runs the integration suite
uv run resilio eval run          # benchmark; add --llm <name> to score a real model
```

- Provider tests use `tests.helpers.mock_client(handler)`, an `AsyncClient` on
  `httpx.MockTransport`. Assert on the request you send (path, params, body)
  and return a recorded response shape from the vendor's documentation.
- Workflow tests use the `container` fixture: real graph, mock providers.
- Never call real external APIs in tests.

Coverage is enforced at 80% and the suite is currently around 95%. Keep it there.

## Good first issues

Each of these is a single file plus a test, following the guide above:

| Kind | Idea | Notes |
|---|---|---|
| Telemetry | **New Relic** | Start from `examples/newrelic_telemetry.py`; add `get_error_rate` |
| Telemetry | **Dynatrace** | Problems API v2 + Logs |
| Telemetry | **Grafana Tempo / Loki** | TraceQL `{ status = error }` / LogQL |
| Telemetry | **Elastic / OpenSearch** | `_search` on `log.level:error` |
| Remediation | **GitLab CI** | Trigger a pipeline with `TARGET_SHA` variable |
| Remediation | **Spinnaker / Argo Rollouts** | Abort + rollback a canary |
| Remediation | **AWS ECS** | `UpdateService` to a previous task definition |
| Notifications | **Opsgenie**, **Discord**, **Google Chat** | Webhook payloads like `teams.py` |
| LLM | **Google Gemini**, **Azure OpenAI**, **AWS Bedrock** | Azure is an `OpenAICompatibleProvider` subclass |
| Ingest | **Grafana**, **New Relic**, **Opsgenie** webhooks | Add a normalizer in `ingest.py` |

Comment on the issue to claim it; we will help you land it.

## Pull requests

- One logical change per PR; link the issue.
- Fill in the [PR template](../.github/pull_request_template.md). `make check` must pass.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat(telemetry): add New Relic provider`).
- Anything that lets ResilioAI do *more* to production without a human
  (new automated actions, policy changes) needs an explicit safety discussion
  in the PR and usually an ADR.

## Architecture Decision Records

Significant decisions live in [`docs/ADR/`](ADR/). To propose one, copy the
template in [`ADR/README.md`](ADR/README.md), number it sequentially, and open
a PR with status `Proposed`. Disagreements are resolved in the PR thread, and
the ADR is merged as `Accepted` or `Rejected`, so the reasoning is kept either way.

By contributing you agree that your contributions are licensed under the
Apache License 2.0 and that you will follow the
[Code of Conduct](../CODE_OF_CONDUCT.md).
