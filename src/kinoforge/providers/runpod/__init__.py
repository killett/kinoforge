"""RunPodProvider — supports both pod and serverless modes via ComputeProvider.

All HTTP traffic is routed through injected ``http_post`` / ``http_get``
callables so tests can spy without any real network calls.

Pod mode:
  - Creates a pod via the RunPod GraphQL API.
  - Installs the in-pod self-terminator (see :mod:`kinoforge.providers.runpod.selfterm`)
    and injects a scoped terminate-only credential.
  - Polls until the pod is gone on :meth:`destroy_instance`.

Serverless mode:
  - Creates a serverless endpoint via the RunPod GraphQL API.
  - Sets concurrency caps (``max_workers``, ``max_in_flight``) and a
    per-request deadline from the ``Lifecycle``.
  - Reports ``status="ready"`` immediately (serverless is ready on creation).

Mode dispatch:
  Determined by ``spec.tags.get("mode", "pod")`` at :meth:`create_instance`
  time.  Pass ``tags={"mode": "serverless"}`` for serverless behaviour.

Self-registers under ``"runpod"`` when this module is imported.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from kinoforge.core import registry
from kinoforge.core.errors import TeardownError
from kinoforge.core.interfaces import (
    ComputeProvider,
    CredentialProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.offers import filter_offers
from kinoforge.providers.runpod import selfterm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default RunPod GraphQL endpoint.
_DEFAULT_BASE_URL: str = "https://api.runpod.io/graphql"

#: Maximum poll iterations in :meth:`RunPodProvider.destroy_instance`.
_MAX_DESTROY_POLLS: int = 10

#: How long to sleep between destroy polls (seconds).
_DESTROY_POLL_INTERVAL: float = 3.0

#: RunPod proxy URL pattern for exposed ports.
_PROXY_URL_PATTERN: str = "https://{pod_id}-{port}.proxy.runpod.net"

#: RunPod serverless run URL pattern.
_SERVERLESS_RUN_URL: str = "https://api.runpod.ai/v2/{endpoint_id}/run"


# ---------------------------------------------------------------------------
# Real HTTP helpers (default seam implementations)
# ---------------------------------------------------------------------------


def _urllib_post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST *body* as JSON to *url* and return the decoded response dict.

    Args:
        url: Endpoint URL.
        body: JSON-serialisable request body.

    Returns:
        Decoded JSON response as a Python dict.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


def _urllib_get_json(url: str) -> dict[str, Any]:
    """GET *url* and return the decoded JSON response dict.

    Args:
        url: Endpoint URL.

    Returns:
        Decoded JSON response as a Python dict.
    """
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


def _make_default_http_seams(
    api_key: str | None,
) -> tuple[
    Callable[[str, dict[str, Any]], dict[str, Any]],
    Callable[[str], dict[str, Any]],
]:
    """Return urllib http_post / http_get callables with api_key appended.

    RunPod's legacy GraphQL endpoint authenticates via the ``api_key`` query
    parameter, NOT an ``Authorization`` header.  Bearer-token auth returns
    HTTP 403 against ``https://api.runpod.io/graphql``.  This helper appends
    ``?api_key=<key>`` (or ``&api_key=<key>`` if the URL already has a query
    string) to every request URL before dispatching to ``urllib``.

    When ``api_key`` is empty or ``None``, returns the bare unauthenticated
    callables — callers that have no credentials still get something callable
    and the real API will respond with 401/403 on its own.

    Args:
        api_key: The RUNPOD_API_KEY string, or None if no credential is set.

    Returns:
        ``(http_post, http_get)`` tuple matching the existing seam contract.
    """
    if not api_key:
        return _urllib_post_json, _urllib_get_json

    from urllib.parse import quote

    encoded_key = quote(api_key, safe="")

    # RunPod's edge layer rejects requests whose User-Agent matches
    # ``Python-urllib/*`` (the stdlib default) with HTTP 403.  Any non-default
    # UA — including a kinoforge-identifying one — passes the filter.
    _UA = "kinoforge/0.1 (+https://github.com/dr-twinklebrane/kinoforge)"

    def _append_api_key(url: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}api_key={encoded_key}"

    def authed_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            _append_api_key(url),
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": _UA},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return dict(json.loads(resp.read().decode("utf-8")))

    def authed_get(url: str) -> dict[str, Any]:
        # Content-Type bypasses RunPod GraphQL's CSRF protection — without
        # it, GETs return HTTP 400 "potential Cross-Site Request Forgery".
        # User-Agent bypasses the Python-urllib block (HTTP 403).
        req = urllib.request.Request(  # noqa: S310
            _append_api_key(url),
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return dict(json.loads(resp.read().decode("utf-8")))

    return authed_post, authed_get


# ---------------------------------------------------------------------------
# RunPodProvider
# ---------------------------------------------------------------------------


class RunPodProvider(ComputeProvider):
    """ComputeProvider for RunPod — pod and serverless modes.

    All I/O is routed through injected callables for testability.

    Args:
        creds: Credential provider.  Must supply ``RUNPOD_API_KEY`` (used for
            orchestration and to authenticate all default HTTP calls) and
            ``RUNPOD_TERMINATE_KEY`` (injected into pod env for least-privilege
            self-termination).  ``None`` is safe when only
            :meth:`find_offers`, :meth:`list_instances`, or :meth:`endpoints`
            are called (though the real RunPod API will return 401/403 without
            a valid key).
        http_post: Callable ``(url, body) -> dict`` for POST requests.
            Defaults to an authenticated ``urllib`` implementation that appends
            ``?api_key=<RUNPOD_API_KEY>`` to every request URL.
        http_get: Callable ``(url) -> dict`` for GET requests.
            Defaults to an authenticated ``urllib`` implementation that appends
            ``?api_key=<RUNPOD_API_KEY>`` to every request URL.
        sleep: Callable invoked between destroy-poll iterations.
            Defaults to :func:`time.sleep`.
        base_url: RunPod GraphQL base URL.

    Example:
        >>> from kinoforge.providers.runpod import RunPodProvider
        >>> p = RunPodProvider()
        >>> p.name
        'runpod'
    """

    name: str = "runpod"

    def __init__(
        self,
        creds: CredentialProvider | None = None,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        http_get: Callable[[str], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        """Initialise the provider with injectable seams.

        Args:
            creds: Credential provider for ``RUNPOD_API_KEY`` (HTTP auth) and
                ``RUNPOD_TERMINATE_KEY`` (pod env injection).
            http_post: Injectable POST transport.  When ``None``, resolves to
                an authenticated closure that appends ``?api_key=<RUNPOD_API_KEY>``
                to every request URL (or the bare urllib callable when no key
                is available).
            http_get: Injectable GET transport.  Same auth behaviour as
                ``http_post``.
            sleep: Injectable sleep used between destroy polls.
            base_url: RunPod GraphQL endpoint URL.
        """
        self._creds = creds
        api_key = creds.get("RUNPOD_API_KEY") if creds is not None else None
        default_post, default_get = _make_default_http_seams(api_key)
        self._http_post = http_post if http_post is not None else default_post
        self._http_get = http_get if http_get is not None else default_get
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # ComputeProvider interface
    # ------------------------------------------------------------------

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        """Return RunPod GPU offers that satisfy ``reqs``.

        Calls the RunPod GraphQL API once to fetch available GPU types, converts
        them to :class:`~kinoforge.core.interfaces.Offer` objects, then delegates
        filtering and sorting to :func:`~kinoforge.core.offers.filter_offers`.

        Args:
            reqs: Hardware requirements to filter against.

        Returns:
            Filtered and sorted list of :class:`~kinoforge.core.interfaces.Offer`
            objects.
        """
        response = self._http_post(self._base_url, {"query": _GPU_TYPES_QUERY})
        gpu_types: list[dict[str, Any]] = response.get("data", {}).get("gpuTypes", [])
        raw_offers: list[Offer] = []
        for gpu in gpu_types:
            gpu_id: str = str(gpu.get("id", ""))
            vram_gb: int = int(gpu.get("memoryInGb", 0))
            # RunPod's lowestPrice resolver returns null for both fields when
            # a GPU type has no currently-available instances.  Skip those —
            # otherwise we would happily return them as $0 offers and the
            # caller would attempt a create that fails with "no instances
            # available".
            pricing: dict[str, Any] | None = gpu.get("lowestPrice")
            if not pricing:
                continue
            uninterruptable = pricing.get("uninterruptablePrice")
            min_bid = pricing.get("minimumBidPrice")
            price = uninterruptable if uninterruptable is not None else min_bid
            if price is None:
                continue
            cost: float = float(price)
            raw_offers.append(
                Offer(
                    id=gpu_id,
                    gpu_type=gpu_id,
                    vram_gb=vram_gb,
                    cuda="12.8",  # RunPod standard image baseline (CUDA 12.8+)
                    cost_rate_usd_per_hr=cost,
                    mode="pod",
                )
            )
        return filter_offers(raw_offers, reqs)

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Create a RunPod pod or serverless endpoint from ``spec``.

        Mode is controlled by ``spec.tags.get("mode", "pod")``:

        - ``"pod"``: Creates a on-demand pod via GraphQL.  Injects the
          ``RUNPOD_TERMINATE_KEY`` and the rendered self-terminator script
          into the pod environment.  Returns ``status="starting"``.
        - ``"serverless"``: Creates a serverless template/endpoint via GraphQL.
          Sets concurrency caps from ``spec.lifecycle``.  No self-terminator
          script is injected.  Returns ``status="ready"``.

        Cred safety: ``RUNPOD_API_KEY`` is NEVER placed in the pod environment
        or on the returned ``Instance``.

        Args:
            spec: The instance specification.

        Returns:
            A new :class:`~kinoforge.core.interfaces.Instance`.
        """
        mode = spec.tags.get("mode", "pod")
        if mode == "serverless":
            return self._create_serverless(spec)
        return self._create_pod(spec)

    def get_instance(self, instance_id: str) -> Instance:
        """Return an :class:`~kinoforge.core.interfaces.Instance` by ID.

        Queries the RunPod GraphQL API for the pod.

        Args:
            instance_id: The RunPod pod/endpoint ID.

        Returns:
            The matching ``Instance``.

        Raises:
            KeyError: No pod found for ``instance_id``.
        """
        resp = self._http_post(self._base_url, {"query": _get_pod_query(instance_id)})
        pod: dict[str, Any] = resp.get("data", {}).get("pod", {})
        if not pod:
            raise KeyError(f"no RunPod pod found: {instance_id!r}")
        return _pod_to_instance(pod)

    def list_instances(self) -> list[Instance]:
        """Return all active RunPod pods for the authenticated account.

        Returns:
            A (possibly empty) list of :class:`~kinoforge.core.interfaces.Instance`.
        """
        resp = self._http_post(self._base_url, {"query": _LIST_PODS_QUERY})
        pods: list[dict[str, Any]] = (
            resp.get("data", {}).get("myself", {}).get("pods", [])
        )
        return [_pod_to_instance(p) for p in pods]

    def stop_instance(self, instance_id: str) -> None:
        """Stop a running RunPod pod (pause billing).

        Args:
            instance_id: The pod ID to stop.
        """
        self._http_post(
            self._base_url,
            {"query": _stop_pod_mutation(instance_id)},
        )

    def destroy_instance(self, instance_id: str) -> None:
        """Terminate a pod and poll until it is confirmed gone.

        Posts the terminate mutation once, then polls with :data:`_MAX_DESTROY_POLLS`
        GET requests to confirm deletion.  Idempotent: if the pod is already
        gone (empty response) the call succeeds immediately.

        Args:
            instance_id: The pod ID to destroy.

        Raises:
            TeardownError: The pod was not confirmed gone within
                :data:`_MAX_DESTROY_POLLS` attempts.
        """
        # Terminate (best-effort; pod might already be gone)
        self._http_post(
            self._base_url,
            {"query": _terminate_pod_mutation(instance_id)},
        )
        # Poll until gone or cap exceeded
        for _ in range(_MAX_DESTROY_POLLS):
            resp = self._http_post(
                self._base_url, {"query": _get_pod_query(instance_id)}
            )
            pod = resp.get("data", {}).get("pod")
            if not pod:
                return  # confirmed gone
            self._sleep(_DESTROY_POLL_INTERVAL)
        raise TeardownError(
            f"RunPod pod {instance_id!r} not confirmed destroyed after "
            f"{_MAX_DESTROY_POLLS} polls"
        )

    def heartbeat(self, instance_id: str) -> None:
        """No-op for RunPod: liveness is managed by the in-pod self-terminator.

        Args:
            instance_id: Unused.
        """
        # The in-pod self-terminator handles heartbeat / dead-man logic.
        # The orchestrator should call the pod's own heartbeat endpoint instead.

    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Return the endpoint map for ``instance``.

        For pod mode:
            ``{"<port>": "https://{id}-{port}.proxy.runpod.net"}`` for each
            port listed in ``instance.tags["ports"]``.

        For serverless mode:
            ``{"run": "https://api.runpod.ai/v2/{id}/run"}``.

        Args:
            instance: The instance whose endpoints to return.

        Returns:
            A dict mapping logical endpoint names / port strings to URLs.
        """
        mode = instance.tags.get("mode", "pod")
        if mode == "serverless":
            return {
                "run": _SERVERLESS_RUN_URL.format(endpoint_id=instance.id),
            }
        # Pod mode — parse port list
        ports_raw = instance.tags.get("ports", "")
        ports: list[str] = [p.strip() for p in ports_raw.split(",") if p.strip()]
        return {
            port: _PROXY_URL_PATTERN.format(pod_id=instance.id, port=port)
            for port in ports
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _create_pod(self, spec: InstanceSpec) -> Instance:
        """Create a RunPod on-demand pod and return an Instance.

        Args:
            spec: Instance specification.

        Returns:
            Instance with ``status="starting"``.
        """
        # Build env dict: user-supplied vars + self-terminator key + script.
        env: dict[str, str] = dict(spec.env)

        # Inject terminate-only key (scoped; NOT the main API key)
        if self._creds is not None:
            terminate_key = self._creds.get("RUNPOD_TERMINATE_KEY")
            if terminate_key:
                env["RUNPOD_TERMINATE_KEY"] = terminate_key

        # Embed self-terminator script
        env["KINOFORGE_SELFTERM_SCRIPT"] = selfterm.RENDER(
            idle_timeout=spec.lifecycle.idle_timeout_s,
            max_lifetime=spec.lifecycle.max_lifetime_s,
            job_timeout=spec.lifecycle.job_timeout_s,
            time_buffer=spec.lifecycle.time_buffer_s,
        )

        # Safety: NEVER put the main API key in the pod env
        env.pop("RUNPOD_API_KEY", None)

        gpu_type_id = spec.offer.gpu_type if spec.offer else ""
        body: dict[str, Any] = {
            "query": _CREATE_POD_MUTATION,
            "variables": {
                "input": {
                    "cloudType": "ALL",
                    "gpuCount": 1,
                    "volumeInGb": spec.volume_gb,
                    "containerDiskInGb": 50,
                    "minVcpuCount": 2,
                    "minMemoryInGb": 15,
                    "gpuTypeId": gpu_type_id,
                    "name": spec.run_id or "kinoforge-pod",
                    "imageName": spec.image,
                    "dockerArgs": "",
                    "ports": ",".join(spec.ports) if spec.ports else "",
                    "volumeMountPath": spec.volume_mount or "/workspace",
                    "env": [{"key": k, "value": v} for k, v in env.items()],
                }
            },
        }

        resp = self._http_post(self._base_url, body)
        if "errors" in resp:
            error_msgs = [str(e.get("message", e)) for e in resp.get("errors", [])]
            raise ValueError(
                "RunPod create-pod mutation returned errors:\n"
                + "\n".join(f"  - {m}" for m in error_msgs)
            )
        pod_data: dict[str, Any] = resp.get("data", {}).get(
            "podFindAndDeployOnDemand", {}
        )
        pod_id: str = str(pod_data.get("id", ""))
        if not pod_id:
            raise ValueError(
                f"RunPod create-pod returned no pod id; full response: {resp!r}"
            )

        return Instance(
            id=pod_id,
            provider=self.name,
            status="starting",
            created_at=time.time(),
            tags={k: v for k, v in spec.tags.items()},
            cost_rate_usd_per_hr=(
                spec.offer.cost_rate_usd_per_hr if spec.offer else 0.0
            ),
        )

    def _create_serverless(self, spec: InstanceSpec) -> Instance:
        """Create a RunPod serverless endpoint and return an Instance.

        Args:
            spec: Instance specification.

        Returns:
            Instance with ``status="ready"`` (serverless is ready immediately).
        """
        body: dict[str, Any] = {
            "query": _CREATE_SERVERLESS_MUTATION,
            "variables": {
                "input": {
                    "name": spec.run_id or "kinoforge-serverless",
                    "imageName": spec.image,
                    "gpuIds": "ADA_24",
                    "workersMin": 0,
                    "workersMax": spec.lifecycle.max_workers,
                    "idleTimeout": int(spec.lifecycle.idle_timeout_s),
                    "maxJobsPerWorker": spec.lifecycle.max_in_flight,
                    "executionTimeoutMs": int(spec.lifecycle.job_timeout_s * 1000),
                }
            },
        }

        resp = self._http_post(self._base_url, body)
        if "errors" in resp:
            error_msgs = [str(e.get("message", e)) for e in resp.get("errors", [])]
            raise ValueError(
                "RunPod create-serverless mutation returned errors:\n"
                + "\n".join(f"  - {m}" for m in error_msgs)
            )
        endpoint_data: dict[str, Any] = resp.get("data", {}).get("saveTemplate", {})
        endpoint_id: str = str(endpoint_data.get("id", ""))
        if not endpoint_id:
            raise ValueError(
                f"RunPod create-serverless returned no endpoint id; full response: {resp!r}"
            )

        return Instance(
            id=endpoint_id,
            provider=self.name,
            status="ready",
            created_at=time.time(),
            tags={k: v for k, v in spec.tags.items()},
            cost_rate_usd_per_hr=0.0,
        )


# ---------------------------------------------------------------------------
# GraphQL query / mutation strings
# ---------------------------------------------------------------------------

# RunPod's `lowestPrice` resolver returns null for ALL fields without an
# input argument.  Passing `(input: { gpuCount: 1 })` causes the resolver
# to return real prices for currently-available GPU types and null for
# unavailable ones — `find_offers` filters out null-priced offers so the
# caller never tries to create a pod on a GPU that has no live capacity.
_GPU_TYPES_QUERY: str = (
    "{ gpuTypes { id displayName memoryInGb secureCloud communityCloud "
    "lowestPrice(input: { gpuCount: 1 }) "
    "{ minimumBidPrice uninterruptablePrice } } }"
)

_LIST_PODS_QUERY: str = "{ myself { pods { id desiredStatus imageName } } }"

_CREATE_POD_MUTATION: str = (
    "mutation($input: PodFindAndDeployOnDemandInput!) "
    "{ podFindAndDeployOnDemand(input: $input) { id desiredStatus imageName } }"
)

_CREATE_SERVERLESS_MUTATION: str = (
    "mutation($input: EndpointInput!) { saveTemplate(input: $input) { id } }"
)


def _get_pod_query(pod_id: str) -> str:
    """Return a GraphQL query string for a single pod by ID.

    Args:
        pod_id: The RunPod pod ID.

    Returns:
        A GraphQL query string.
    """
    return f'{{ pod(input: {{ podId: "{pod_id}" }}) {{ id desiredStatus imageName }} }}'


def _stop_pod_mutation(pod_id: str) -> str:
    """Return a GraphQL mutation string to stop a pod.

    Args:
        pod_id: The RunPod pod ID.

    Returns:
        A GraphQL mutation string.
    """
    return (
        f'mutation {{ podStop(input: {{ podId: "{pod_id}" }}) {{ id desiredStatus }} }}'
    )


def _terminate_pod_mutation(pod_id: str) -> str:
    """Return a GraphQL mutation string to terminate (destroy) a pod.

    Args:
        pod_id: The RunPod pod ID.

    Returns:
        A GraphQL mutation string.
    """
    return f'mutation {{ podTerminate(input: {{ podId: "{pod_id}" }}) }}'


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _runpod_status_to_kinoforge(desired_status: str) -> str:
    """Map a RunPod ``desiredStatus`` string to a kinoforge status string.

    Args:
        desired_status: The ``desiredStatus`` field from the RunPod API.

    Returns:
        One of ``"starting"``, ``"ready"``, ``"stopped"``, ``"terminated"``.
    """
    mapping: dict[str, str] = {
        "RUNNING": "ready",
        "EXITED": "stopped",
        "DEAD": "terminated",
    }
    return mapping.get(desired_status.upper(), "starting")


def _pod_to_instance(pod: dict[str, Any]) -> Instance:
    """Convert a RunPod pod dict from the API to an :class:`~kinoforge.core.interfaces.Instance`.

    Args:
        pod: A dict with ``id``, ``desiredStatus``, and ``imageName`` keys.

    Returns:
        An :class:`~kinoforge.core.interfaces.Instance` representing the pod.
    """
    pod_id: str = str(pod.get("id", ""))
    desired_status: str = str(pod.get("desiredStatus", ""))
    return Instance(
        id=pod_id,
        provider="runpod",
        status=_runpod_status_to_kinoforge(desired_status),
        created_at=0.0,  # RunPod list API does not return creation time
        tags={"mode": "pod"},
    )


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _default_factory() -> RunPodProvider:
    """Default factory: build a RunPodProvider with env-backed credentials.

    Reads ``RUNPOD_API_KEY`` (and ``RUNPOD_TERMINATE_KEY``) from the process
    environment so the orchestrator's resolved provider authenticates against
    the real RunPod API.
    """
    from kinoforge.core.credentials import EnvCredentialProvider

    return RunPodProvider(creds=EnvCredentialProvider())


registry.register_provider("runpod", _default_factory)
