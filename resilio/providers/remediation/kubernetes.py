"""Kubernetes API remediation for ``apps/v1`` Deployments.

Talks to the API server directly over HTTPS (no kubectl, no client library):

* **rollback** - find the ReplicaSet whose pod template carries the target git
  SHA (label ``resilio.dev/git-sha`` or an image tag) and patch the Deployment
  template back to it; equivalent to ``kubectl rollout undo --to-revision``.
* **restart** - bump ``kubectl.kubernetes.io/restartedAt`` like ``rollout restart``.
* **scale** - patch the ``/scale`` subresource.

The Deployment name is the service name. Required RBAC: get/list/patch on
``deployments``, ``deployments/scale`` and get/list on ``replicasets``.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

import httpx

from resilio.models import Deployment
from resilio.providers.base import BaseRemediationProvider, BaseTelemetryProvider
from resilio.providers.http import ProviderError, build_client, request_json
from resilio.providers.remediation._health import telemetry_recovered, wait_until

if TYPE_CHECKING:
    from resilio.config import Settings

STRATEGIC_MERGE = {"Content-Type": "application/strategic-merge-patch+json"}
MERGE_PATCH = {"Content-Type": "application/merge-patch+json"}
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


class KubernetesRemediationProvider(BaseRemediationProvider):
    name = "kubernetes"

    def __init__(
        self,
        api_url: str,
        token: str,
        namespace: str = "default",
        sha_label: str = "resilio.dev/git-sha",
        verify: bool | str = True,
        telemetry: BaseTelemetryProvider | None = None,
        error_rate_threshold: float = 0.05,
        poll_interval_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(telemetry)
        self.namespace = namespace
        self.sha_label = sha_label
        self.error_rate_threshold = error_rate_threshold
        self.poll_interval_seconds = poll_interval_seconds
        self._client = client or build_client(
            api_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, verify=verify
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, telemetry: BaseTelemetryProvider | None = None
    ) -> Self:
        if settings.k8s_token is not None:
            token = settings.k8s_token.get_secret_value()
        elif os.path.exists(settings.k8s_token_path):
            with open(settings.k8s_token_path, encoding="utf-8") as fh:
                token = fh.read().strip()
        else:
            raise ValueError("Kubernetes remediation requires K8S_TOKEN or an in-cluster token")
        verify: bool | str = settings.k8s_ca_path if os.path.exists(settings.k8s_ca_path) else True
        return cls(
            api_url=settings.k8s_api_url,
            token=token,
            namespace=settings.k8s_namespace,
            sha_label=settings.k8s_sha_label,
            verify=verify,
            telemetry=telemetry,
            error_rate_threshold=settings.prometheus_error_rate_threshold,
            poll_interval_seconds=settings.health_check_interval_seconds,
        )

    # ------------------------------------------------------------- helpers
    def _deployment_path(self, service_name: str) -> str:
        return f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{service_name}"

    async def _get_deployment(self, service_name: str) -> dict[str, Any]:
        data: dict[str, Any] = await request_json(
            self._client, "GET", self._deployment_path(service_name), provider=self.name
        )
        return data

    async def _replica_sets(self, service_name: str) -> list[dict[str, Any]]:
        deployment = await self._get_deployment(service_name)
        labels = deployment["spec"]["selector"].get("matchLabels", {})
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        data = await request_json(
            self._client,
            "GET",
            f"/apis/apps/v1/namespaces/{self.namespace}/replicasets",
            provider=self.name,
            params={"labelSelector": selector},
        )
        owned = [
            rs
            for rs in (data or {}).get("items", [])
            if any(
                ref.get("kind") == "Deployment" and ref.get("name") == service_name
                for ref in rs["metadata"].get("ownerReferences", [])
            )
        ]
        owned.sort(
            key=lambda rs: int(rs["metadata"].get("annotations", {}).get(REVISION_ANNOTATION, 0)),
            reverse=True,
        )
        return owned

    def _sha_of(self, replica_set: dict[str, Any]) -> str | None:
        template = replica_set["spec"]["template"]
        sha = template.get("metadata", {}).get("labels", {}).get(self.sha_label)
        if sha:
            return str(sha)
        for container in template["spec"].get("containers", []):
            image = container.get("image", "")
            if ":" in image:
                return str(image.rsplit(":", 1)[1])
        return None

    # ---------------------------------------------------------- interface
    async def recent_deployments(self, service_name: str, limit: int = 10) -> list[Deployment]:
        deployments: list[Deployment] = []
        for rs in await self._replica_sets(service_name):
            sha = self._sha_of(rs)
            if not sha:
                continue
            meta = rs["metadata"]
            deployments.append(
                Deployment(
                    sha=sha,
                    deployed_at=datetime.fromisoformat(
                        meta["creationTimestamp"].replace("Z", "+00:00")
                    ),
                    message=f"replicaset {meta['name']}",
                    version=meta.get("annotations", {}).get(REVISION_ANNOTATION),
                )
            )
        return deployments[:limit]

    async def execute_rollback(self, service_name: str, target_sha: str) -> bool:
        target = next(
            (
                rs
                for rs in await self._replica_sets(service_name)
                if (self._sha_of(rs) or "").startswith(target_sha)
                or target_sha.startswith(self._sha_of(rs) or "\0")
            ),
            None,
        )
        if target is None:
            raise ProviderError(self.name, f"no ReplicaSet found for sha {target_sha}")
        template = copy.deepcopy(target["spec"]["template"])
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        await request_json(
            self._client,
            "PATCH",
            self._deployment_path(service_name),
            provider=self.name,
            headers=STRATEGIC_MERGE,
            json={"spec": {"template": template}},
        )
        return True

    async def restart_service(self, service_name: str) -> bool:
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": datetime.now(UTC).isoformat()
                        }
                    }
                }
            }
        }
        await request_json(
            self._client,
            "PATCH",
            self._deployment_path(service_name),
            provider=self.name,
            headers=STRATEGIC_MERGE,
            json=patch,
        )
        return True

    async def scale_service(self, service_name: str, replicas_delta: int) -> bool:
        path = f"{self._deployment_path(service_name)}/scale"
        scale = await request_json(self._client, "GET", path, provider=self.name)
        current = int(scale.get("spec", {}).get("replicas", 0))
        await request_json(
            self._client,
            "PATCH",
            path,
            provider=self.name,
            headers=MERGE_PATCH,
            json={"spec": {"replicas": max(current + replicas_delta, 1)}},
        )
        return True

    async def verify_health(self, service_name: str, timeout_seconds: int) -> bool:
        async def healthy() -> bool:
            deployment = await self._get_deployment(service_name)
            spec, status = deployment.get("spec", {}), deployment.get("status", {})
            desired = int(spec.get("replicas", 1))
            rolled_out = (
                status.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 0)
                and status.get("updatedReplicas", 0) == desired
                and status.get("availableReplicas", 0) == desired
                and status.get("replicas", 0) == desired
            )
            if not rolled_out:
                return False
            recovered = await telemetry_recovered(
                self.telemetry, service_name, self.error_rate_threshold
            )
            return recovered is not False

        return await wait_until(healthy, timeout_seconds, self.poll_interval_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()
