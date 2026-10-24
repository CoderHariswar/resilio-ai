from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from resilio.config import Settings
from resilio.providers.base import BaseTelemetryProvider
from resilio.providers.http import ProviderError
from resilio.providers.remediation._health import telemetry_recovered, wait_until
from resilio.providers.remediation.argocd import ArgoCDRemediationProvider
from resilio.providers.remediation.github_actions import GitHubActionsRemediationProvider
from resilio.providers.remediation.kubernetes import KubernetesRemediationProvider
from resilio.providers.remediation.mock import MockRemediationProvider, mock_sha
from resilio.providers.telemetry.mock import MockTelemetryProvider
from tests.helpers import mock_client


class FixedRate(BaseTelemetryProvider):
    def __init__(self, rate: float | None) -> None:
        self.rate = rate

    async def fetch_traces(self, service_name: str, lookback_minutes: int) -> list[dict[str, Any]]:
        return []

    async def get_error_rate(self, service_name: str) -> float | None:
        return self.rate


# --------------------------------------------------------------- helpers
async def test_wait_until_succeeds_and_times_out() -> None:
    calls = {"n": 0}

    async def flips() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return calls["n"] >= 3

    assert await wait_until(flips, timeout_seconds=1, interval_seconds=0.001)

    async def never() -> bool:
        return False

    assert not await wait_until(never, timeout_seconds=0.01, interval_seconds=0.005)


async def test_telemetry_recovered() -> None:
    assert await telemetry_recovered(None, "s", 0.05) is None
    assert await telemetry_recovered(FixedRate(None), "s", 0.05) is None
    assert await telemetry_recovered(FixedRate(0.01), "s", 0.05) is True
    assert await telemetry_recovered(FixedRate(0.5), "s", 0.05) is False


# ------------------------------------------------------------------ mock
async def test_mock_heals_only_with_correct_action() -> None:
    telemetry = MockTelemetryProvider()
    provider = MockRemediationProvider(telemetry)
    telemetry.inject_outage("svc", "memory-leak")

    history = await provider.recent_deployments("svc")
    assert history[0].message.startswith("feat(catalog)")
    assert history[0].sha == mock_sha("svc", 0)

    await provider.restart_service("svc")
    assert not await provider.verify_health("svc", 1)
    await provider.execute_rollback("svc", history[1].sha)
    assert await provider.verify_health("svc", 1)
    assert [a[1] for a in provider.actions] == ["restart", "rollback"]


async def test_mock_without_telemetry_is_always_healthy() -> None:
    provider = MockRemediationProvider()
    assert await provider.scale_service("svc", 2)
    assert await provider.verify_health("svc", 1)
    assert len(await provider.recent_deployments("svc", limit=3)) == 3


# ---------------------------------------------------------------- github
def _github(handler: Any, telemetry: BaseTelemetryProvider | None = None) -> Any:
    return GitHubActionsRemediationProvider(
        "tok",
        "acme/{service}",
        telemetry=telemetry,
        poll_interval_seconds=0.001,
        client=mock_client(handler, "https://api.github.test"),
    )


async def test_github_dispatch_history_and_health() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/repos/acme/checkout/deployments":
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "a" * 40,
                        "created_at": "2026-01-02T00:00:00Z",
                        "creator": {"login": "alice"},
                        "description": "deploy v2",
                        "ref": "v2",
                        "payload": {"service": "checkout"},
                    },
                    {
                        "sha": "c" * 40,
                        "created_at": "2026-01-01T12:00:00Z",
                        "payload": {"service": "other"},
                    },
                    {"sha": "b" * 40, "created_at": "2026-01-01T00:00:00Z", "payload": ""},
                ],
            )
        if path.endswith("/dispatches"):
            body = json.loads(request.content)
            assert body == {
                "ref": "main",
                "inputs": {"service": "checkout", "target_sha": "b" * 40},
            }
            return httpx.Response(204)
        if path.endswith("/runs"):
            return httpx.Response(
                200, json={"workflow_runs": [{"status": "completed", "conclusion": "success"}]}
            )
        return httpx.Response(404)

    provider = _github(handler, telemetry=FixedRate(0.01))
    history = await provider.recent_deployments("checkout")
    assert [d.sha[0] for d in history] == ["a", "b"]
    assert history[0].author == "alice"
    assert await provider.execute_rollback("checkout", "b" * 40)
    assert await provider.verify_health("checkout", 1)
    await provider.aclose()


async def test_github_health_fails_on_failed_run_or_bad_telemetry() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"workflow_runs": [{"status": "completed", "conclusion": "failure"}]}
        )

    assert not await _github(failing).verify_health("checkout", 0)

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow_runs": []})

    assert not await _github(empty).verify_health("checkout", 0)

    def running(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow_runs": [{"status": "in_progress"}]})

    assert not await _github(running).verify_health("checkout", 0)

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"workflow_runs": [{"status": "completed", "conclusion": "success"}]}
        )

    assert not await _github(ok, telemetry=FixedRate(0.9)).verify_health("checkout", 0)


def test_github_from_settings_requires_credentials() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        GitHubActionsRemediationProvider.from_settings(settings)
    configured = settings.model_copy(
        update={"github_token": SecretStr("t"), "github_repository": "a/b"}
    )
    assert GitHubActionsRemediationProvider.from_settings(configured).repository == "a/b"


# ---------------------------------------------------------------- argocd
ARGO_APP = {
    "status": {
        "health": {"status": "Healthy"},
        "history": [
            {"id": 7, "revision": "1" * 40, "deployedAt": "2026-01-01T00:00:00Z"},
            {
                "id": 8,
                "revision": "2" * 40,
                "deployedAt": "2026-01-02T00:00:00Z",
                "initiatedBy": {"username": "bob"},
            },
        ],
    }
}


async def test_argocd_history_rollback_and_health() -> None:
    rollbacks: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v1/applications/prod-checkout/rollback"
            rollbacks.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(200, json=ARGO_APP)

    provider = ArgoCDRemediationProvider(
        "https://argo",
        "tok",
        app_template="prod-{service}",
        poll_interval_seconds=0.001,
        client=mock_client(handler),
    )
    history = await provider.recent_deployments("checkout")
    assert [d.version for d in history] == ["8", "7"]
    assert history[0].author == "bob"
    assert await provider.execute_rollback("checkout", "1111111")
    assert rollbacks == [{"id": 7, "prune": False}]
    assert await provider.verify_health("checkout", 1)
    with pytest.raises(ProviderError, match="not found"):
        await provider.execute_rollback("checkout", "deadbeef")


async def test_argocd_unhealthy_app() -> None:
    app = {"status": {"health": {"status": "Degraded"}, "history": []}}
    provider = ArgoCDRemediationProvider(
        "https://argo", "tok", client=mock_client(lambda r: httpx.Response(200, json=app))
    )
    assert not await provider.verify_health("checkout", 0)


# ------------------------------------------------------------ kubernetes
def _rs(name: str, revision: int, sha: str | None, image: str = "app:latest") -> dict[str, Any]:
    labels = {"app": "checkout", "pod-template-hash": name[-5:]}
    if sha:
        labels["resilio.dev/git-sha"] = sha
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": f"2026-01-0{revision}T00:00:00Z",
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "ownerReferences": [{"kind": "Deployment", "name": "checkout"}],
        },
        "spec": {
            "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{"name": "app", "image": image}]},
            }
        },
    }


class FakeK8s:
    def __init__(self) -> None:
        self.patches: list[tuple[str, str, dict[str, Any]]] = []
        self.ready = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        base = "/apis/apps/v1/namespaces/shop/deployments/checkout"
        if request.method == "PATCH":
            self.patches.append(
                (path, request.headers["Content-Type"], json.loads(request.content))
            )
            return httpx.Response(200, json={})
        if path == base:
            replicas = 3 if self.ready else 1
            return httpx.Response(
                200,
                json={
                    "metadata": {"generation": 5},
                    "spec": {"replicas": 3, "selector": {"matchLabels": {"app": "checkout"}}},
                    "status": {
                        "observedGeneration": 5,
                        "updatedReplicas": replicas,
                        "availableReplicas": replicas,
                        "replicas": 3,
                    },
                },
            )
        if path == f"{base}/scale":
            return httpx.Response(200, json={"spec": {"replicas": 3}})
        if path == "/apis/apps/v1/namespaces/shop/replicasets":
            assert request.url.params["labelSelector"] == "app=checkout"
            orphan = _rs("other-aaaaa", 9, "f" * 40)
            orphan["metadata"]["ownerReferences"] = [{"kind": "Deployment", "name": "other"}]
            return httpx.Response(
                200,
                json={
                    "items": [
                        _rs("checkout-11111", 1, "a" * 40),
                        _rs("checkout-33333", 3, None, image="registry/app:" + "c" * 12),
                        _rs("checkout-22222", 2, "b" * 40),
                        orphan,
                    ]
                },
            )
        return httpx.Response(404)


def _k8s(fake: FakeK8s) -> KubernetesRemediationProvider:
    return KubernetesRemediationProvider(
        "https://k8s",
        "tok",
        namespace="shop",
        poll_interval_seconds=0.001,
        client=mock_client(fake),
    )


async def test_k8s_history_from_replicasets() -> None:
    history = await _k8s(FakeK8s()).recent_deployments("checkout")
    assert [d.version for d in history] == ["3", "2", "1"]
    assert history[0].sha == "c" * 12
    assert history[1].sha == "b" * 40


async def test_k8s_rollback_patches_template_without_hash() -> None:
    fake = FakeK8s()
    assert await _k8s(fake).execute_rollback("checkout", "a" * 10)
    _, content_type, body = fake.patches[0]
    assert content_type == "application/strategic-merge-patch+json"
    labels = body["spec"]["template"]["metadata"]["labels"]
    assert labels["resilio.dev/git-sha"] == "a" * 40
    assert "pod-template-hash" not in labels
    with pytest.raises(ProviderError):
        await _k8s(fake).execute_rollback("checkout", "0" * 40)


async def test_k8s_restart_scale_and_health() -> None:
    fake = FakeK8s()
    provider = _k8s(fake)
    assert await provider.restart_service("checkout")
    annotations = fake.patches[0][2]["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in annotations
    assert await provider.scale_service("checkout", 2)
    assert fake.patches[1][2] == {"spec": {"replicas": 5}}
    assert fake.patches[1][1] == "application/merge-patch+json"
    assert await provider.verify_health("checkout", 1)
    fake.ready = False
    assert not await provider.verify_health("checkout", 0)


def test_k8s_from_settings(tmp_path: Any) -> None:
    settings = Settings(_env_file=None, k8s_token_path=str(tmp_path / "missing"))  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="K8S_TOKEN"):
        KubernetesRemediationProvider.from_settings(settings)
    token = tmp_path / "token"
    token.write_text("abc\n")
    provider = KubernetesRemediationProvider.from_settings(
        settings.model_copy(update={"k8s_token_path": str(token), "k8s_namespace": "ns"})
    )
    assert provider.namespace == "ns"
