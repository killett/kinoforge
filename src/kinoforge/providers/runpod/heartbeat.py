"""RunPod dockerArgs preserve-and-merge heartbeat satisfier (C25 Task 2, Branch B).

Implements :class:`~kinoforge.core.heartbeat_endpoints.HeartbeatEndpoint`
by appending a trailing bash comment ``# _kinoforge_hb:<ISO>`` to the
pod's ``dockerArgs`` field. The Phase 24 selfterm boot bash (set at pod
creation by :meth:`RunPodProvider._create_pod`) is preserved verbatim
because bash treats ``#`` as start-of-comment; pod restart re-runs the
preserved boot bash and the in-pod selfterm survives.

Single-writer invariant: B7's ``provision:<id>`` cooperative lock
guarantees only the holding orchestrator writes a pod's wire state
during a session; intra-orchestrator HeartbeatLoop is single-threaded.

Spec: docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.errors import TransportError

__all__ = ["RunPodGraphQLHeartbeatEndpoint"]

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"

_HEARTBEAT_MARKER_KEY: str = "_kinoforge_hb"

_POD_EDIT_JOB_MUTATION: str = """
mutation PodEditJob($input: PodEditJobInput!) {
  podEditJob(input: $input) { id }
}
""".strip()

_POD_QUERY: str = """
query GetPod($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    dockerArgs
  }
}
""".strip()

# Strip stale ` # _kinoforge_hb:<ISO>` trailer from prior tick before
# re-appending. The marker value is a single ISO 8601 timestamp emitted
# by ``datetime.isoformat()`` — guaranteed no embedded whitespace — so
# ``\S+`` after the key isolates the trailer without consuming any
# upstream bash content (the Phase 24 decoder string is the typical
# upstream content, and it has no `#` mid-string in the real path).
_STRIP_RE: re.Pattern[str] = re.compile(
    r"\s*#\s*" + re.escape(_HEARTBEAT_MARKER_KEY) + r":\S+\s*$"
)

# Read-side extractor. ``\S+`` rejects mid-string ``# _kinoforge_hb:``
# occurrences whose tail contains whitespace (e.g. ``# _kinoforge_hb:foo &&``
# inside an ``echo`` argument): such a string ends in ``"`` not ISO chars
# so the anchored ``\s*$`` can't be reached without consuming whitespace
# the capture forbids → no match → read returns ``None``.
_READ_RE: re.Pattern[str] = re.compile(
    r"#\s*" + re.escape(_HEARTBEAT_MARKER_KEY) + r":(\S+)\s*$"
)


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build a stdlib-urllib POST callable with Bearer auth."""

    def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "kinoforge-heartbeat/0.1",
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


def _merge_marker(base: str, ts_local: datetime) -> str:
    """Strip any stale heartbeat marker and append a fresh one."""
    stripped = _STRIP_RE.sub("", base)
    if stripped.strip() == "":
        return f": # {_HEARTBEAT_MARKER_KEY}:{ts_local.isoformat()}"
    return f"{stripped} # {_HEARTBEAT_MARKER_KEY}:{ts_local.isoformat()}"


class RunPodGraphQLHeartbeatEndpoint:
    """dockerArgs preserve-and-merge satisfier.

    Write path: read current dockerArgs → strip any prior heartbeat
    marker → append fresh ``# _kinoforge_hb:<ISO>`` trailer → mutate.
    Two GraphQL round-trips per tick.

    Read path: query dockerArgs → regex-extract trailing marker.
    """

    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str = _DEFAULT_GRAPHQL_URL,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """Construct the endpoint with an injectable HTTP seam.

        Args:
            api_key: RunPod API key used for Bearer auth on every call.
            graphql_url: RunPod GraphQL endpoint URL. Defaults to the
                production URL; tests can point at a fixture server.
            http_post: Optional injectable ``(url, body) -> dict`` POST.
                Defaults to a stdlib-urllib closure that wraps HTTP /
                JSON errors in :class:`TransportError`.
        """
        self._api_key = api_key
        self._graphql_url = graphql_url
        self._http_post = (
            http_post if http_post is not None else _default_http_post(api_key)
        )

    def _read_dockerargs(self, instance_id: str) -> str | None:
        """Return current dockerArgs string, or None if pod gone."""
        payload = {"query": _POD_QUERY, "variables": {"podId": instance_id}}
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod pod query transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod pod query failed: {resp['errors']}")
        pod = (resp.get("data") or {}).get("pod")
        if pod is None:
            return None
        raw = pod.get("dockerArgs")
        return raw if isinstance(raw, str) else ""

    def write(self, instance_id: str, ts_local: datetime) -> None:
        """Append the heartbeat marker to the pod's dockerArgs.

        Reads current ``dockerArgs``, strips any prior heartbeat trailer,
        appends ``# _kinoforge_hb:<ts_local.isoformat()>`` and writes
        back via ``podEditJob``. No-op when the pod is gone.

        Args:
            instance_id: RunPod pod ID.
            ts_local: Heartbeat timestamp (tz-aware; local-TZ ISO emitted).

        Raises:
            TransportError: GraphQL ``errors``, HTTP non-2xx, JSON parse
                failure, or any other transport-layer fault.
        """
        base = self._read_dockerargs(instance_id)
        if base is None:
            # Pod gone; no-op write. Next tick or classify will surface it.
            return
        merged = _merge_marker(base, ts_local)
        payload = {
            "query": _POD_EDIT_JOB_MUTATION,
            "variables": {"input": {"podId": instance_id, "dockerArgs": merged}},
        }
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod podEditJob transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod podEditJob failed: {resp['errors']}")

    def read(self, instance_id: str) -> datetime | None:
        """Return the heartbeat timestamp from the pod's dockerArgs.

        Args:
            instance_id: RunPod pod ID.

        Returns:
            Tz-aware :class:`datetime` parsed from the trailing
            ``# _kinoforge_hb:<ISO>`` marker, or ``None`` when the pod
            is gone, dockerArgs is empty, or no marker is present.

        Raises:
            TransportError: Marker present but value is not a valid ISO
                8601 timestamp, or any GraphQL / HTTP transport failure.
        """
        raw = self._read_dockerargs(instance_id)
        if raw is None or raw == "":
            return None
        m = _READ_RE.search(raw)
        if m is None:
            return None
        value = m.group(1).strip()
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise TransportError(
                f"corrupted heartbeat marker for {instance_id}: {value!r}"
            ) from exc
