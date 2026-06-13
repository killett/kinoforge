"""RunPod GraphQL runtime{} util-snapshot satisfier (C26 Task 3).

Implements :class:`~kinoforge.core.util_endpoints.UtilSnapshotEndpoint`
by querying ``pod{runtime{uptimeInSeconds, gpus{...}, container{...}}}``
and aggregating MAX across the gpus array for ``gpu_util_percent``.

Single GraphQL round-trip per ``read_util`` call. Bearer auth + inlined
podId (matching the proven shape from the Task 1 disk-field probe).

Per Task 1 outcome (sidecar tests/live/_runpod_util_disk_probe.json),
RunPod's ``PodRuntime`` and ``PodRuntimeContainer`` types expose NO
disk-percent field, so ``disk_percent`` is always ``None``.

Spec: docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md §4.2 + §8.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from kinoforge.core.errors import TransportError
from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["RunPodGraphQLUtilEndpoint"]

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"


def _build_runtime_query(pod_id: str) -> str:
    """Build the per-tick GraphQL query for a given pod.

    Args:
        pod_id: The RunPod pod ID to query.

    Returns:
        A GraphQL query string with the podId inlined.
    """
    return (
        "{\n"
        f'  pod(input: {{ podId: "{pod_id}" }}) {{\n'
        "    id\n"
        "    runtime {\n"
        "      uptimeInSeconds\n"
        "      gpus { id gpuUtilPercent memoryUtilPercent }\n"
        "      container { cpuPercent memoryPercent }\n"
        "    }\n"
        "  }\n"
        "}"
    )


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """stdlib-urllib POST with Bearer auth; sister of C25 heartbeat closure."""

    def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "kinoforge-util/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                data: bytes = resp.read()
        except urllib.error.HTTPError as exc:
            raise TransportError(
                f"RunPod GraphQL HTTP {exc.code}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(
                f"RunPod GraphQL transport error: {exc.reason}"
            ) from exc
        try:
            decoded: dict[str, Any] = json.loads(data)
        except json.JSONDecodeError as exc:
            raise TransportError(f"RunPod GraphQL non-JSON response: {exc}") from exc
        return decoded

    return _post


class RunPodGraphQLUtilEndpoint:
    """RunPod GraphQL runtime{} satisfier.

    Single ``pod{runtime{...}}`` query per :meth:`read_util` call.
    ``gpu_util_percent`` = MAX across ``runtime.gpus``; empty array → None.
    ``data.pod = null`` → ``read_util`` returns None.
    ``runtime = null`` (early boot, confirmed Task 1) → returns None.

    Attributes:
        api_key: RunPod API key (Bearer-auth header value).
        graphql_url: RunPod GraphQL endpoint.
        http_post: Injectable POST closure (test seam).
    """

    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str = _DEFAULT_GRAPHQL_URL,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """Build the satisfier.

        Args:
            api_key: RunPod API key for Bearer auth.
            graphql_url: GraphQL endpoint URL.
            http_post: Test seam; defaults to a Bearer-auth urllib closure.
        """
        self._api_key = api_key
        self._graphql_url = graphql_url
        self._http_post = (
            http_post if http_post is not None else _default_http_post(api_key)
        )

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        """Return a :class:`UtilSnapshot` for ``instance_id``, or None.

        Args:
            instance_id: The RunPod pod ID.

        Returns:
            A UtilSnapshot, or None when the pod is gone or runtime is null.

        Raises:
            TransportError: GraphQL ``errors`` or HTTP / JSON transport fault.
        """
        payload = {"query": _build_runtime_query(instance_id)}
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod runtime query failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod runtime query failed: {resp['errors']}")
        pod = (resp.get("data") or {}).get("pod")
        if pod is None:
            return None
        runtime = pod.get("runtime")
        if runtime is None:
            return None
        gpus = runtime.get("gpus") or []
        gpu_util = max(
            (
                float(g["gpuUtilPercent"])
                for g in gpus
                if g.get("gpuUtilPercent") is not None
            ),
            default=None,
        )
        container = runtime.get("container") or {}
        cpu = container.get("cpuPercent")
        mem = container.get("memoryPercent")
        uptime = runtime.get("uptimeInSeconds")
        return UtilSnapshot(
            gpu_util_percent=gpu_util,
            cpu_percent=float(cpu) if cpu is not None else None,
            memory_percent=float(mem) if mem is not None else None,
            disk_percent=None,
            uptime_seconds=int(uptime) if uptime is not None else None,
        )
