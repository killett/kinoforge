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

import base64
import gzip
import json
import logging
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from kinoforge.core import registry
from kinoforge.core.boot_liveness import BootVerdict, classify_boot_liveness
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import CapacityError, TeardownError, TransportError
from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint
from kinoforge.core.interfaces import (
    ComputeProvider,
    CredentialProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.offers import filter_offers
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.providers.runpod import selfterm
from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

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

#: C28 A3: path to the A0 empirical-probe sidecar that records whether
#: PodFindAndDeployOnDemandInput accepts ``restartPolicy``. Read at
#: _create_pod time so a future RunPod schema change picks up automatically
#: once the probe is re-run; absent or unsupported → conservative skip with
#: a warning. Patchable via unittest.mock for unit tests.
_RUNPOD_SCHEMA_SIDECAR: Path = Path(
    "tests/live/_c28_runpod_input_schema_probe.json",
)


def _restart_policy_supported() -> bool:
    """Read the A0 sidecar to decide whether to emit ``restartPolicy``."""
    try:
        payload = json.loads(_RUNPOD_SCHEMA_SIDECAR.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return bool(payload.get("restart_policy_supported"))


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


class RunPodGraphQLError(TransportError):
    """GraphQL endpoint returned a non-empty ``errors`` field.

    Distinguishes a server-acknowledged failure (HTTP 200 but
    ``{"errors": [...]}`` body) from a transport-level error.  Extends
    :class:`~kinoforge.core.errors.TransportError` so existing broad-catch
    sites (e.g. :class:`~kinoforge.core.heartbeat.HeartbeatLoop`) keep
    catching the new failure without code changes.
    """


def _unwrap_graphql_response(resp: dict[str, Any], *, context: str) -> dict[str, Any]:
    """Return ``resp["data"]`` or raise :class:`RunPodGraphQLError`.

    RunPod's legacy GraphQL endpoint returns HTTP 200 even when the
    request semantically fails — the failure is signalled by a
    non-empty ``errors`` array in the response body.  Treating
    ``data: null`` as success in that case is the load-bearing bug
    behind the 2026-06-23 destroy-on-teardown money leak: a failed
    terminate mutation is followed by a get-pod query whose
    errors-only response is misread as "pod confirmed gone".

    An empty ``errors`` list is treated as success (RunPod sometimes
    returns the key with an empty list even on a clean response).

    Args:
        resp: The decoded JSON body of a RunPod GraphQL response.
        context: A short operator-facing string describing the call
            site (e.g. ``"terminate pod-X"``, ``"get pod-X"``).  Joined
            into the exception message so a failed call surfaces a
            useful breadcrumb.

    Returns:
        The ``resp["data"]`` payload (``{}`` if the key is absent).

    Raises:
        RunPodGraphQLError: ``resp["errors"]`` is a non-empty list.
    """
    errors = resp.get("errors")
    if errors:
        raise RunPodGraphQLError(f"RunPod GraphQL {context} failed: {errors}")
    data = resp.get("data") or {}
    if not isinstance(data, dict):
        return {}
    return data


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

    # RunPod's GraphQL edge intermittently returns 502 / 503 / 504 under
    # load (observed 2026-06-27 Tier-4 fire `94344920`: a single 503 mid
    # `wait_for_ready` poll loop aborted a 5-min cold-boot). Wrap every
    # request in a small retry budget so transient gateway failures
    # don't kill long orchestrator sequences.
    _RETRY_STATUS = {502, 503, 504}
    _RETRY_ATTEMPTS = 3
    _RETRY_BACKOFF_S = (2.0, 5.0, 10.0)

    def _request(
        req: urllib.request.Request,
    ) -> dict[str, Any]:
        last_exc: urllib.error.HTTPError | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                with urllib.request.urlopen(req) as resp:  # noqa: S310
                    return dict(json.loads(resp.read().decode("utf-8")))
            except urllib.error.HTTPError as exc:
                # Surface RunPod's GraphQL error body to stderr so 500/4xx
                # don't masquerade as opaque HTTP errors. URL api_key is
                # redacted; request body is NOT echoed (would leak HF_TOKEN /
                # RUNPOD_TERMINATE_KEY from the env injection list).
                try:
                    body_dump = exc.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    body_dump = "<unreadable response body>"
                _redacted_url = req.full_url.split("?")[0] + "?api_key=<redacted>"
                sys.stderr.write(
                    f"[runpod-http] {exc.code} {exc.reason} from {_redacted_url}: "
                    f"{body_dump[:1500]}\n"
                )
                if exc.code not in _RETRY_STATUS or attempt == _RETRY_ATTEMPTS - 1:
                    raise
                last_exc = exc
                time.sleep(_RETRY_BACKOFF_S[attempt])
        # Defensive: loop guarantees a return or raise; satisfy type checker.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable")

    def authed_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            _append_api_key(url),
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": _UA},
            method="POST",
        )
        return _request(req)

    def authed_get(url: str) -> dict[str, Any]:
        # Content-Type bypasses RunPod GraphQL's CSRF protection — without
        # it, GETs return HTTP 400 "potential Cross-Site Request Forgery".
        # User-Agent bypasses the Python-urllib block (HTTP 403).
        req = urllib.request.Request(  # noqa: S310
            _append_api_key(url),
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        return _request(req)

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
        heartbeat_endpoint: HeartbeatEndpoint | None = None,
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
            heartbeat_endpoint: Optional B5a heartbeat substrate endpoint.
                When provided, ``heartbeat()`` delegates writes and
                ``last_heartbeat()`` delegates reads to it.  ``None``
                (the default) preserves the pre-B5a no-op behaviour.
        """
        self._creds = creds
        api_key = creds.get("RUNPOD_API_KEY") if creds is not None else None
        default_post, default_get = _make_default_http_seams(api_key)
        self._http_post = http_post if http_post is not None else default_post
        self._http_get = http_get if http_get is not None else default_get
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")
        self._heartbeat_endpoint: HeartbeatEndpoint | None = heartbeat_endpoint
        # Sweeper-side ephemeral reap (spec 2026-06-28): GraphQL probe endpoint
        # for probe_runtime. Test seam: tests reassign this attribute to a stub
        # endpoint. None when no api_key → probe_runtime returns None
        # (substrate-missing signal).
        self._api_key: str = api_key or ""
        self._util_endpoint: RunPodGraphQLUtilEndpoint | None = (
            RunPodGraphQLUtilEndpoint(api_key=self._api_key) if api_key else None
        )
        # Process-local registry of pods we created in this process: pod_id
        # -> Instance. RunPod's GraphQL pod schema does not surface
        # user-defined tags on list/get responses (only id, desiredStatus,
        # imageName), so _pod_to_instance produces tag-less Instances. The
        # only place a pod's full tag set survives is the in-memory Instance
        # returned by _create_pod / _create_serverless. Without this
        # registry, find_instance_by_tag misses every pod we just created
        # — silently no-op'ing the test destroy block and orphaning pods
        # until selfterm reaps them. Populated by create_instance,
        # consulted first by find_instance_by_tag, pruned by
        # destroy_instance once a teardown is confirmed.
        self._created_instances: dict[str, Instance] = {}

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
            instance = self._create_serverless(spec)
        else:
            instance = self._create_pod(spec)
        self._created_instances[instance.id] = instance
        return instance

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

    def find_instance_by_tag(self, key: str, value: str) -> Instance | None:
        """Return the first 'ready' instance whose tags[key] == value, else None.

        Used by long-running test loops (Layer P live smoke) to discover and
        reuse warm pods across iterations, avoiding repeated cold-start costs,
        AND by the smoke test's destroy block to recover the pod_id of a pod
        the orchestrator created inside :meth:`create_instance` whose Instance
        was never surfaced back to the test.

        Search order:
        1. Process-local create registry (``self._created_instances``) — the
           only source where user-defined tags (e.g. ``kinoforge.layer``)
           survive, since RunPod's pod schema does not expose tags on
           list/get responses.
        2. Remote :meth:`list_instances` — falls back here for tags that
           ``_pod_to_instance`` actually populates (currently only ``mode``).
           Cross-process warm-reuse via user tags is not supported on this
           path; selfterm and ``cli reap`` are the supported cleanup levers.

        Args:
            key: Tag dict key to match (e.g. ``"kinoforge.layer"``).
            value: Required value at that key (e.g. ``"layer-p-smoke"``).

        Returns:
            The first ``Instance`` with ``tags.get(key) == value`` (registry
            entries match regardless of ``status`` since the test layer
            transitions through ``starting → ready`` while still owning the
            pod); for the ``list_instances`` fallback path, ``status ==
            "ready"`` is still required.  ``None`` if no such instance
            exists.
        """
        for inst in self._created_instances.values():
            if inst.tags.get(key) == value:
                return inst
        for inst in self.list_instances():
            if inst.status == "ready" and inst.tags.get(key) == value:
                return inst
        return None

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
            RunPodGraphQLError: The terminate mutation or any poll
                returned a non-empty ``errors`` field.  This must
                surface to callers instead of being misread as
                ``data.pod is None`` → "confirmed gone" — the
                load-bearing case for the 2026-06-23 destroy-on-teardown
                money leak.
            TeardownError: The pod was not confirmed gone within
                :data:`_MAX_DESTROY_POLLS` attempts (terminate +
                polls all returned a populated pod).
        """
        # Terminate — if the GraphQL endpoint replies with errors, raise
        # immediately; do NOT enter the poll loop where errors-only responses
        # are indistinguishable from "pod gone".
        terminate_resp = self._http_post(
            self._base_url,
            {"query": _terminate_pod_mutation(instance_id)},
        )
        _unwrap_graphql_response(terminate_resp, context=f"terminate {instance_id}")
        # Poll until gone or cap exceeded.
        for _ in range(_MAX_DESTROY_POLLS):
            resp = self._http_post(
                self._base_url, {"query": _get_pod_query(instance_id)}
            )
            data = _unwrap_graphql_response(resp, context=f"get pod {instance_id}")
            pod = data.get("pod")
            if not pod:
                self._created_instances.pop(instance_id, None)
                return  # confirmed gone
            self._sleep(_DESTROY_POLL_INTERVAL)
        raise TeardownError(
            f"RunPod pod {instance_id!r} not confirmed destroyed after "
            f"{_MAX_DESTROY_POLLS} polls"
        )

    def heartbeat(self, instance_id: str) -> None:
        """Stamp a heartbeat for ``instance_id`` via the configured endpoint.

        No-op when no :class:`HeartbeatEndpoint` has been wired (operator
        opted out via ``compute.heartbeat_mode = "none"``). Otherwise
        delegates to ``endpoint.write(instance_id, datetime.now().astimezone())``
        — TZ-aware local time per project memory
        ``feedback_local_timezone_only``.

        Args:
            instance_id: Pod id whose heartbeat to stamp.

        Raises:
            TransportError: Propagated from the endpoint's wire layer
                (HTTP non-2xx, GraphQL errors). The Layer U
                HeartbeatLoop wraps this in its broad try/except so a
                single bad tick never kills the loop.
        """
        if self._heartbeat_endpoint is None:
            return
        self._heartbeat_endpoint.write(instance_id, datetime.now().astimezone())

    def last_heartbeat(self, instance_id: str) -> float | None:
        """Return the most-recent heartbeat for ``instance_id`` as POSIX epoch.

        The datetime/float seam: :class:`HeartbeatEndpoint` returns
        TZ-aware datetime; ``HeartbeatLoop`` and ``Ledger`` consume float
        POSIX epoch. ``datetime.timestamp()`` is TZ-correct on
        local-aware datetimes (converts to UTC POSIX under the hood;
        ``datetime.fromtimestamp`` reverses).

        Args:
            instance_id: Pod id whose heartbeat to read.

        Returns:
            POSIX-epoch float when the endpoint returned a datetime,
            ``None`` when the endpoint returned None or no endpoint is
            wired.

        Raises:
            TransportError: Propagated from the endpoint's wire layer
                (same envelope as :meth:`heartbeat`).
        """
        if self._heartbeat_endpoint is None:
            return None
        dt = self._heartbeat_endpoint.read(instance_id)
        return dt.timestamp() if dt is not None else None

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        """Live runtime probe via the GraphQL ``pod{runtime{...}}`` query.

        Distinguishes three outcomes:
            * ``data.pod = null`` (404) → ``RuntimeProbe(found=False, ...)``
            * ``runtime = null`` (early boot) → ``RuntimeProbe(found=True,
              container_uptime_s=None, gpu_util_pct=None, ...)``
            * ``runtime`` populated → fully populated ``RuntimeProbe``

        Returns:
            A :class:`RuntimeProbe`, or ``None`` when no endpoint is
            configured (e.g. provider constructed without credentials —
            substrate-missing signal for sweeper-ephemeral-reap).

        Raises:
            TransportError: GraphQL transport fault. Sweeper's
                ``_probe_with_cache`` catches and classifies as
                ``PROBE_FAILED``.
        """
        endpoint = self._util_endpoint
        if endpoint is None:
            return None
        found, snapshot = endpoint.probe(pod_id)
        now_local = datetime.now().isoformat()
        if not found:
            return RuntimeProbe(
                pod_id=pod_id,
                found=False,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
            )
        if snapshot is None:
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
            )
        return RuntimeProbe(
            pod_id=pod_id,
            found=True,
            container_uptime_s=float(snapshot.uptime_seconds)
            if snapshot.uptime_seconds is not None
            else None,
            gpu_util_pct=snapshot.gpu_util_percent,
            cpu_pct=snapshot.cpu_percent,
            cost_per_hr=None,
            probed_at_local=now_local,
        )

    def make_boot_liveness_probe(
        self, instance: Instance
    ) -> RunPodBootLivenessProbe | None:
        """Fresh boot-liveness probe for this pod, or None if no util endpoint."""
        if self._util_endpoint is None:
            return None
        return RunPodBootLivenessProbe(
            instance_id=instance.id,
            util_endpoint=self._util_endpoint,
            fetch_bootstrap_log=lambda _iid: _fetch_bootstrap_log_tail(instance),
        )

    def set_heartbeat_endpoint(
        self,
        endpoint: object | None,
    ) -> None:
        """Install or clear the heartbeat endpoint post-construction.

        Used by :func:`kinoforge.core.orchestrator._resolve_provider` to
        inject the dispatched endpoint after the registry's zero-arg
        factory built the provider.

        Args:
            endpoint: A :class:`HeartbeatEndpoint`-satisfying object, or
                ``None`` to clear. Typed as ``object | None`` to match the
                ABC signature (core-import-ban: interfaces.py cannot import
                HeartbeatEndpoint).
        """
        self._heartbeat_endpoint = endpoint  # type: ignore[assignment]

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

        When ``spec.provision_script`` is set, it is base64-encoded into the
        ``KINOFORGE_PROVISION_SCRIPT`` env var and ``dockerArgs`` is set to the
        literal ``bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh
        && chmod +x /tmp/p.sh && bash /tmp/p.sh"`` so the pod decodes + runs the
        script at boot. When ``spec.provision_script`` is None, ``dockerArgs == ""``
        (pre-Layer-Q default).

        Args:
            spec: Instance specification.

        Returns:
            Instance with ``status="starting"``.
        """
        env = self._assemble_create_env(spec)
        docker_args = self._encode_provision_script(env, spec.provision_script)

        gpu_type_id = spec.offer.gpu_type if spec.offer else ""
        # Under ephemeral mode, suppress the alias-laden run_id from the
        # provider-visible pod name and stamp ``kinoforge-ephemeral=true``
        # on the Instance tags. Default mode is unchanged.
        _eph = EphemeralSession.current()
        if _eph is not None and not _eph.policy.pod_name_includes_alias:
            pod_name = f"kinoforge-{secrets.token_hex(4)}"
        else:
            pod_name = spec.run_id or "kinoforge-pod"
        body = self._build_create_pod_body(
            spec,
            gpu_type_id=gpu_type_id,
            pod_name=pod_name,
            docker_args=docker_args,
            env=env,
        )

        resp = self._http_post(self._base_url, body)
        self._classify_capacity_error(resp, gpu_type_id=gpu_type_id)
        return self._instance_from_create_response(spec, resp, _eph)

    def _assemble_create_env(self, spec: InstanceSpec) -> dict[str, str]:
        """Assemble the pod env payload for the create-pod mutation.

        Combines user-supplied vars, the C28 diagnostic overlay, the scoped
        terminate-only key, and the rendered self-terminator script. The main
        ``RUNPOD_API_KEY`` is never included.

        Args:
            spec: Instance specification.

        Returns:
            The env dict to serialize into the mutation's ``env`` field
            (insertion order is preserved on the wire).
        """
        # Build env dict: user-supplied vars + self-terminator key + script.
        env: dict[str, str] = dict(spec.env)

        # C28 A1.5: overlay diagnostic env (S3 bucket/prefix + AWS keys for the
        # in-pod EXIT trap) without clobbering any explicit user env. The
        # diagnostic overlay is opt-in via cfg.diagnostic_mode → orchestrator
        # populates spec.diagnostic_env; outside that path the dict is empty
        # and this loop is a no-op.
        for diag_key, diag_value in spec.diagnostic_env.items():
            env.setdefault(diag_key, diag_value)

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
        return env

    @staticmethod
    def _encode_provision_script(
        env: dict[str, str], provision_script: str | None
    ) -> str:
        """Encode ``provision_script`` into ``env`` and return ``dockerArgs``.

        Args:
            env: Env dict to receive ``KINOFORGE_PROVISION_SCRIPT`` (mutated
                in place when a script is present).
            provision_script: The raw provision script, or None.

        Returns:
            The ``dockerArgs`` string that decodes + runs the script at pod
            boot, or ``""`` when no script is set (pre-Layer-Q default).
        """
        if provision_script is not None:
            # Gzip BEFORE base64: RunPod's podFindAndDeployOnDemand mutation
            # returns a raw HTTP 500 (not a GraphQL error) once the total env
            # payload exceeds ~101 KB. The wan+flashvsr bootstrap is ~74 KB raw
            # → 98.8 KB plain base64, which alone pushed total env to 101,971
            # bytes and 500'd every create (root-caused live 2026-07-05). Gzip
            # cuts it to ~72 KB base64 (~4× headroom for future script growth).
            compressed = gzip.compress(provision_script.encode("utf-8"))
            encoded = base64.b64encode(compressed).decode("ascii")
            env["KINOFORGE_PROVISION_SCRIPT"] = encoded
            docker_args = (
                'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d | gzip -d '
                '> /tmp/p.sh && chmod +x /tmp/p.sh && bash /tmp/p.sh"'
            )
        else:
            docker_args = ""
        return docker_args

    @staticmethod
    def _build_create_pod_body(
        spec: InstanceSpec,
        *,
        gpu_type_id: str,
        pod_name: str,
        docker_args: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Build the ``podFindAndDeployOnDemand`` GraphQL mutation body.

        Field ordering is preserved exactly — the wire shape is
        payload-size-sensitive (see the gzip note in
        :meth:`_encode_provision_script`).

        Args:
            spec: Instance specification.
            gpu_type_id: GPU type ID from the selected offer (may be ``""``).
            pod_name: Provider-visible pod name.
            docker_args: ``dockerArgs`` string (see
                :meth:`_encode_provision_script`).
            env: Fully assembled pod env dict.

        Returns:
            The GraphQL request body dict.
        """
        # spec.cloud_type pins the host pool — "secure" for long-running
        # one-shot workloads (community interruption deletes zero-volume
        # pods outright; 3 BSA builds lost 2026-07-03).
        cloud_type = {"any": "ALL", "secure": "SECURE", "community": "COMMUNITY"}[
            spec.cloud_type
        ]
        body: dict[str, Any] = {
            "query": _CREATE_POD_MUTATION,
            "variables": {
                "input": {
                    "cloudType": cloud_type,
                    "gpuCount": 1,
                    "volumeInGb": spec.volume_gb,
                    # Container disk sized to fit large-model downloads
                    # (Wan 2.2 14B diffusers shards = ~70 GB + cache
                    # overhead). Was hardcoded 50 GB — caused
                    # `Not enough free disk space` warnings on Task 8
                    # attempt #10 once shards started landing. TODO:
                    # thread `cfg.compute.requirements.disk_gb` through
                    # InstanceSpec.container_disk_gb instead of this
                    # blanket bump.
                    "containerDiskInGb": 250,
                    "minVcpuCount": 2,
                    # Was 15 GB — Task 8 attempt #17 OOM-killed the
                    # diffusers Wan 2.2 14B loader at rc=137 with CPU
                    # mem 99%. Task 8 attempt #18 then hit a different
                    # wall: 64 GB filtered out all A40 offers ("no
                    # instances available with the requested
                    # specifications"). 32 GB is the sweet spot — most
                    # A40 / A6000 / L40S RunPod machines ship with at
                    # least 32 GB CPU RAM, and the marginal headroom
                    # vs 15 GB is enough to clear shard-load. TODO:
                    # thread cfg.compute.requirements.min_ram_gb
                    # through InstanceSpec.min_memory_gb (sibling of
                    # the containerDiskInGb TODO above).
                    "minMemoryInGb": 32,
                    "gpuTypeId": gpu_type_id,
                    "name": pod_name,
                    "imageName": spec.image,
                    "dockerArgs": docker_args,
                    # RunPod's HTTP reverse proxy
                    # (https://{pod_id}-{port}.proxy.runpod.net) only exposes
                    # ports whose declaration carries an explicit protocol
                    # suffix. Bare ports like "8188" allocate the port but the
                    # proxy returns 404 for every request — wait_for_ready
                    # polls the URL forever, never sees a 200. Diagnostic
                    # 2026-06-02: comparing `ports: "8188"` vs
                    # `ports: "8188/http"` showed the former 404s, the latter
                    # serves correctly. Default to /http for any port without
                    # an explicit suffix so callers who don't know to add it
                    # still work. SSH or TCP-only callers pass "22/tcp" etc.
                    "ports": ",".join(
                        p if "/" in p else f"{p}/http" for p in spec.ports
                    )
                    if spec.ports
                    else "",
                    "volumeMountPath": spec.volume_mount or "/workspace",
                    "env": [{"key": k, "value": v} for k, v in env.items()],
                }
            },
        }

        # C28 A3: opt-out of RunPod's auto-restart-on-every-exit when caller
        # asks AND the input schema actually exposes the field. The A0 probe
        # (2026-06-13) confirmed `restartPolicy` is NOT in
        # PodFindAndDeployOnDemandInput at the time of this commit, so the
        # `restart_policy="never"` path always warns + skips today. If RunPod
        # ever exposes the field, re-running the A0 probe flips the sidecar
        # and this branch starts emitting `restartPolicy: NEVER` with no code
        # change required.
        #
        # RunPod's default (no `restartPolicy` field emitted) is
        # `RestartPolicy.ALWAYS` — the container is re-started on EVERY exit,
        # success and failure alike. The C33 (f) warning rewrite makes this
        # explicit so operators reading the log do not mis-read it as
        # "restart-on-failure" (which would imply clean exits stay terminated).
        if spec.restart_policy == "never":
            if _restart_policy_supported():
                body["variables"]["input"]["restartPolicy"] = "NEVER"
            else:
                logging.getLogger(__name__).warning(
                    "spec.restart_policy='never' requested but RunPod schema "
                    "does not expose restartPolicy (per %s); falling back to "
                    "RunPod's default always-restart-on-every-container-exit "
                    "behaviour (success and failure alike)",
                    _RUNPOD_SCHEMA_SIDECAR,
                )
        return body

    @staticmethod
    def _classify_capacity_error(resp: dict[str, Any], *, gpu_type_id: str) -> None:
        """Classify GraphQL ``errors`` in a create-pod response and raise.

        No-op when ``resp`` carries no ``errors`` key.

        Args:
            resp: Decoded GraphQL response of the create-pod mutation.
            gpu_type_id: GPU type ID, for the capacity-error message.

        Raises:
            CapacityError: A known transient capacity-exhaustion phrasing was
                matched (retryable by ``_create_with_offer_retry`` + the
                capacity-wait loop).
            ValueError: Any other GraphQL error.
        """
        if "errors" not in resp:
            return
        error_msgs = [str(e.get("message", e)) for e in resp.get("errors", [])]
        assembled = "RunPod create-pod mutation returned errors:\n" + "\n".join(
            f"  - {m}" for m in error_msgs
        )
        value_error = ValueError(assembled)
        joined_lower = "\n".join(error_msgs).lower()
        # Capacity exhaustion has three observed phrasings; all mean "the
        # offer find_offers listed is gone by create time" and are transient
        # (2026-07-07). Classify them so _create_with_offer_retry + the
        # capacity-wait loop retry instead of failing the whole run.
        _CAPACITY_MARKERS = (
            "resources to deploy",
            "no longer any instances available",
        )
        if any(marker in joined_lower for marker in _CAPACITY_MARKERS):
            raise CapacityError(
                f"RunPod has no current capacity for {gpu_type_id!r}: {assembled}"
            ) from value_error
        raise value_error

    def _instance_from_create_response(
        self,
        spec: InstanceSpec,
        resp: dict[str, Any],
        eph: EphemeralSession | None,
    ) -> Instance:
        """Assemble the ``Instance`` from a create-pod GraphQL response.

        Args:
            spec: Instance specification.
            resp: Decoded GraphQL response of the create-pod mutation.
            eph: Active ephemeral session, if any (stamps
                ``kinoforge-ephemeral=true`` on the Instance tags).

        Returns:
            Instance with ``status="starting"``.

        Raises:
            ValueError: The response carried no pod id.
        """
        pod_data: dict[str, Any] = resp.get("data", {}).get(
            "podFindAndDeployOnDemand", {}
        )
        pod_id: str = str(pod_data.get("id", ""))
        if not pod_id:
            raise ValueError(
                f"RunPod create-pod returned no pod id; full response: {resp!r}"
            )

        # Populate endpoints eagerly: RunPod assigns proxy URLs synchronously
        # on pod creation via the deterministic pattern
        # https://{pod_id}-{port}.proxy.runpod.net. The URLs return 5xx until
        # the in-pod listener binds, but the URLs themselves are stable from
        # creation time — wait_for_ready can poll them immediately.
        #
        # Resolved-ports lookup: prefer ``spec.ports`` (the Layer Q +
        # orchestrator path: render_provision.ports → InstanceSpec.ports);
        # fall back to ``spec.tags["ports"]`` (legacy callers + the existing
        # ``provider.endpoints()`` method's source-of-truth) so both paths
        # populate the same Instance.endpoints dict.
        resolved_ports: list[str] = list(spec.ports)
        if not resolved_ports:
            raw_tag_ports = spec.tags.get("ports", "")
            resolved_ports = [p.strip() for p in raw_tag_ports.split(",") if p.strip()]
        instance_endpoints: dict[str, str] = {
            p: _PROXY_URL_PATTERN.format(pod_id=pod_id, port=p) for p in resolved_ports
        }
        instance_tags: dict[str, str] = {k: v for k, v in spec.tags.items()}
        if resolved_ports and "ports" not in instance_tags:
            instance_tags["ports"] = ",".join(resolved_ports)
        if eph is not None and not eph.policy.pod_name_includes_alias:
            instance_tags["kinoforge-ephemeral"] = "true"
        return Instance(
            id=pod_id,
            provider=self.name,
            status="starting",
            created_at=time.time(),
            endpoints=instance_endpoints,
            tags=instance_tags,
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
        _eph = EphemeralSession.current()
        if _eph is not None and not _eph.policy.pod_name_includes_alias:
            endpoint_name = f"kinoforge-{secrets.token_hex(4)}"
        else:
            endpoint_name = spec.run_id or "kinoforge-serverless"
        body: dict[str, Any] = {
            "query": _CREATE_SERVERLESS_MUTATION,
            "variables": {
                "input": {
                    "name": endpoint_name,
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

        serverless_tags: dict[str, str] = {k: v for k, v in spec.tags.items()}
        if _eph is not None and not _eph.policy.pod_name_includes_alias:
            serverless_tags["kinoforge-ephemeral"] = "true"
        return Instance(
            id=endpoint_id,
            provider=self.name,
            status="ready",
            created_at=time.time(),
            tags=serverless_tags,
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

_LIST_PODS_QUERY: str = "{ myself { pods { id desiredStatus imageName costPerHr } } }"

_CREATE_POD_MUTATION: str = (
    "mutation($input: PodFindAndDeployOnDemandInput!) "
    "{ podFindAndDeployOnDemand(input: $input) { id desiredStatus imageName } }"
)

_CREATE_SERVERLESS_MUTATION: str = (
    "mutation($input: EndpointInput!) { saveTemplate(input: $input) { id } }"
)


class _UtilProbeEndpoint(Protocol):
    """Duck type for the util endpoint's existence+snapshot probe."""

    def probe(self, instance_id: str) -> tuple[bool, UtilSnapshot | None]:  # noqa: D102
        ...


class RunPodBootLivenessProbe:
    """Stateful boot-liveness probe for one RunPod pod (2026-07-07).

    Each check() reads the util snapshot + bootstrap.log tail and feeds them to
    classify_boot_liveness, tracking the prior snapshot + flatline counter. Boot
    start time is captured at construction for the grace/elapsed window. Any
    fetch/read error degrades to UNKNOWN/ALIVE — never a false STALLED.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        util_endpoint: _UtilProbeEndpoint,
        fetch_bootstrap_log: Callable[[str], str | None],
        grace_s: float = 90.0,
        consecutive_needed: int = 3,
        clock: Clock | None = None,
    ) -> None:
        """Capture boot-start time + wire the util/log/clock seams."""
        self._id = instance_id
        self._util = util_endpoint
        self._fetch_log = fetch_bootstrap_log
        self._grace_s = grace_s
        self._needed = consecutive_needed
        self._clock = clock if clock is not None else RealClock()
        self._start = self._clock.now()
        self._prev_snap: UtilSnapshot | None = None
        self._consecutive_flat = 0

    def check(self, instance_id: str) -> BootVerdict:  # noqa: D102
        try:
            exists, snap = self._util.probe(instance_id)
        except Exception:  # noqa: BLE001 — transport uncertain → keep waiting
            return BootVerdict.UNKNOWN
        try:
            log_tail = self._fetch_log(instance_id)
        except Exception:  # noqa: BLE001 — log fetch best-effort
            log_tail = None
        result = classify_boot_liveness(
            exists=exists,
            log_tail=log_tail,
            snap=snap,
            prev_snap=self._prev_snap,
            consecutive_flat=self._consecutive_flat,
            elapsed_s=self._clock.now() - self._start,
            grace_s=self._grace_s,
            consecutive_needed=self._needed,
        )
        self._consecutive_flat = result.consecutive_flat
        if snap is not None:
            self._prev_snap = snap
        return result.verdict


def _fetch_bootstrap_log_tail(instance: Instance, *, lines: int = 40) -> str | None:
    """Best-effort GET of the pod's :8001/bootstrap.log tail (or None)."""
    base = instance.endpoints.get("8001")
    if not base:
        # Derive from the 8000 proxy host if only that is present.
        base = next(iter(instance.endpoints.values()), "")
        if base:
            base = base.replace("-8000.", "-8001.")
    if not base:
        return None
    url = f"{base.rstrip('/')}/bootstrap.log"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    return "\n".join(text.splitlines()[-lines:])


def _get_pod_query(pod_id: str) -> str:
    """Return a GraphQL query string for a single pod by ID.

    Args:
        pod_id: The RunPod pod ID.

    Returns:
        A GraphQL query string.
    """
    return (
        f'{{ pod(input: {{ podId: "{pod_id}" }}) '
        "{ id desiredStatus imageName costPerHr } }"
    )


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

    ``cost_rate_usd_per_hr`` is populated from the live ``pod.costPerHr``
    field so :func:`kinoforge.cli._commands._cmd_status` can refresh the
    ledger and reflect post-substitution / spot-price rates accurately.
    Missing or ``None`` values (early-boot responses, partial GraphQL
    selection sets) fall back to ``0.0`` rather than raising so the
    status surface stays observable while a pod is still spinning up.

    Args:
        pod: A dict with ``id``, ``desiredStatus``, ``imageName`` and
            (optionally) ``costPerHr`` keys.

    Returns:
        An :class:`~kinoforge.core.interfaces.Instance` representing the pod.
    """
    pod_id: str = str(pod.get("id", ""))
    desired_status: str = str(pod.get("desiredStatus", ""))
    raw_cost = pod.get("costPerHr")
    cost_rate: float = float(raw_cost) if raw_cost is not None else 0.0
    return Instance(
        id=pod_id,
        provider="runpod",
        status=_runpod_status_to_kinoforge(desired_status),
        created_at=0.0,  # RunPod list API does not return creation time
        tags={"mode": "pod"},
        cost_rate_usd_per_hr=cost_rate,
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


# ---------------------------------------------------------------------------
# Validation Check — co-located with provider per the kinoforge.validation
# Check Registry pattern.
# ---------------------------------------------------------------------------

from kinoforge.validation.protocol import (  # noqa: E402
    CheckCategory as _CC,
)
from kinoforge.validation.protocol import (  # noqa: E402
    CheckResult as _CR,
)
from kinoforge.validation.protocol import (  # noqa: E402
    Severity as _SEV,
)
from kinoforge.validation.registry import register as _register  # noqa: E402

# 2026-07-03 RunPod schema migration: GpuTypesInput → GpuTypeFilter
# (list field `ids`, was `gpuTypes`); GpuType.availableCount REMOVED.
# Capacity signal is now lowestPrice(input:{gpuCount:1}).stockStatus —
# observed "Low"/"High", null when no stock. The old query 400'd with
# GRAPHQL_VALIDATION_FAILED on every generate.
_GPU_AVAILABILITY_QUERY = """
query GpuAvailability($input: GpuTypeFilter) {
  gpuTypes(input: $input) {
    id
    lowestPrice(input: {gpuCount: 1}) { stockStatus }
  }
}
""".strip()


def _default_capacity_http_post() -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Return an authenticated http_post for capacity probes."""
    from kinoforge.core.credentials import EnvCredentialProvider

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    post, _ = _make_default_http_seams(api_key)
    return post


class RunPodCapacityHintCheck:
    """PREFLIGHT WARN — at least one preferred GPU must have RunPod capacity."""

    name: str = "runpod_capacity_hint"
    category: _CC = _CC.PREFLIGHT
    severity: _SEV = _SEV.WARN

    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        graphql_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        """Wire injectable POST seam + GraphQL URL."""
        self._http_post = (
            http_post if http_post is not None else _default_capacity_http_post()
        )
        self._graphql_url = graphql_url

    def applies_to(self, cfg: Any) -> bool:  # noqa: ANN401 — Check Protocol
        """Apply iff provider is runpod and gpu_preference is non-empty."""
        if cfg.compute is None or cfg.compute.provider != "runpod":
            return False
        reqs = cfg.compute.requirements
        return bool(reqs and reqs.gpu_preference)

    def run(self, cfg: Any) -> _CR:  # noqa: ANN401 — Check Protocol
        """Query RunPod gpuTypes for current capacity on preferred GPUs."""
        prefs = list(cfg.compute.requirements.gpu_preference)
        try:
            resp = self._http_post(
                self._graphql_url,
                {
                    "query": _GPU_AVAILABILITY_QUERY,
                    "variables": {"input": {"ids": prefs}},
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _CR(
                name=self.name,
                passed=True,
                severity=_SEV.WARN,
                message=f"capacity probe inconclusive: {exc}; not blocking",
            )
        types = (resp.get("data") or {}).get("gpuTypes", [])
        # stockStatus null (or lowestPrice missing entirely — delisted
        # SKUs) means no stock; any non-null value ("Low"/"High") counts.
        any_available = any(
            (t.get("lowestPrice") or {}).get("stockStatus") for t in types
        )
        if any_available:
            return _CR(
                name=self.name,
                passed=True,
                severity=self.severity,
                message="at least one preferred GPU has capacity",
            )
        return _CR(
            name=self.name,
            passed=False,
            severity=self.severity,
            message=(
                "no current capacity on any preferred GPU "
                f"({', '.join(prefs)}); offer-retry will exhaust"
            ),
            fix_suggestion=(
                "either wait, add more entries to gpu_preference, "
                "or raise max_usd_per_hr to admit more SKUs"
            ),
        )

    def auto_fix(self, cfg: Any) -> Any | None:  # noqa: ANN401 — Check Protocol
        """No safe auto-fix — capacity is provider-side state."""
        return None


_register(RunPodCapacityHintCheck())
