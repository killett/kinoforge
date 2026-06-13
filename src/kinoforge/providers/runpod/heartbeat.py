"""RunPod GraphQL-tag heartbeat satisfier (B5a Tasks b + f).

Implements :class:`~kinoforge.core.heartbeat_endpoints.HeartbeatEndpoint`
by writing a compact JSON heartbeat marker (``{"_kinoforge_hb": "<ISO>"}``)
into the pod's ``dockerArgs`` field via the GraphQL ``podEditJob`` mutation
and reading it back via the ``pod`` query. Both methods go through an
injected ``http_post`` seam so tests can spy the precise wire payload
without a real RunPod account.

The wire-discovery on 2026-06-12 (Task f live smoke) showed that
``PodEditJobInput`` has NO ``tags`` field; the B5a spec design assumed
it did. ``dockerArgs`` is the only free-form string field that survives
both write and read on the live API. The heartbeat marker survives
across orchestrator process lifetimes, which is what makes the
cross-session warm-reuse path (B3) workable on a fresh shell.

**Production-safety constraint:** every heartbeat write OVERWRITES the
pod's ``dockerArgs`` field, which is the same field Phase 24's
:meth:`RunPodProvider._create_pod` injects the kinoforge in-pod selfterm
script into at pod creation. This satisfier is therefore SAFE only on
pods created with ``provision_script=None`` (bare heartbeat-mode pods).
Enabling ``compute.heartbeat_mode = "graphql-tag"`` on a real workload
pod will silently overwrite the selfterm script — see PROGRESS.md §C25
for the follow-up tracking entry and fix candidates. Construction of
this class is gated by ``_adapters.build_heartbeat_endpoint_for``; that
function raises ``ValidationError`` for unsafe ``(engine.kind, mode)``
combinations before a pod is ever created — see PROGRESS §C25.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.errors import TransportError

__all__ = ["RunPodGraphQLHeartbeatEndpoint"]

#: Historical name reserved for reference. Pre-Task-f the heartbeat was
#: going to land as a RunPod tag with this key; the live API does not
#: support a ``tags`` field on ``PodEditJobInput``, so the wire-level
#: storage moved to ``dockerArgs`` and the JSON key in
#: ``_HEARTBEAT_JSON_KEY`` below. Kept as a module-private constant for
#: documentation cross-reference only.
_HEARTBEAT_TAG_KEY_LEGACY: str = "_kinoforge_last_heartbeat"

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"

_POD_EDIT_JOB_MUTATION: str = """
mutation PodEditJob($input: PodEditJobInput!) {
  podEditJob(input: $input) { id }
}
""".strip()

# RunPod's PodEditJobInput does NOT have a ``tags`` field — ``tags`` is a
# design assumption from the B5a spec that proved incorrect when tested
# against the live API (2026-06-12 live smoke, Task f).  The ``dockerArgs``
# field IS present in both ``PodEditJobInput`` (write) and the ``pod`` query
# (read), making it the only available free-form string carrier for
# cross-session heartbeat state.  Using it for heartbeat timestamps does
# not break any kinoforge workload because the heartbeat-mode pods have
# ``provision_script=None`` and do not rely on ``dockerArgs`` for their
# boot command.
_POD_QUERY: str = """
query GetPod($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    dockerArgs
  }
}
""".strip()

#: JSON key embedded in the ``dockerArgs`` string to distinguish the
#: heartbeat payload from a legitimate docker command string.
_HEARTBEAT_JSON_KEY: str = "_kinoforge_hb"


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build a stdlib-urllib POST callable with Bearer auth.

    Phase 24 pattern: HTTP via stdlib urllib by default, replaceable via
    the constructor's ``http_post`` kwarg in tests and production opt-in.
    """

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


class RunPodGraphQLHeartbeatEndpoint:
    """GraphQL-tag satisfier: ``podEditJob`` (write) + ``pod`` (read).

    Both methods raise :class:`TransportError` on HTTP non-2xx, GraphQL
    ``errors`` arrays, JSON parse failures, and corrupted tag values.
    Pod-gone (``data.pod == null``) and tag-absent are valid ``None``
    returns, not transport failures.

    Args:
        api_key: RunPod API key with write scope (the main
            ``RUNPOD_API_KEY``, NOT the scoped ``RUNPOD_TERMINATE_KEY``
            which is delete-only).
        graphql_url: RunPod GraphQL endpoint URL. Defaults to the
            production URL.
        http_post: Optional injected POST callable. ``None`` builds a
            stdlib-urllib callable with Bearer auth.
    """

    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str = _DEFAULT_GRAPHQL_URL,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """Initialise the satisfier with credentials and an optional seam."""
        self._api_key = api_key
        self._graphql_url = graphql_url
        self._http_post = (
            http_post if http_post is not None else _default_http_post(api_key)
        )

    def write(self, instance_id: str, ts_local: datetime) -> None:
        """Stamp the heartbeat on ``instance_id`` via the pod's ``dockerArgs`` field.

        RunPod's ``PodEditJobInput`` does NOT have a ``tags`` field — the
        live-API discovery on 2026-06-12 (B5a Task f) showed only
        ``podId``, ``env``, ``dockerArgs``, ``imageName``, ``volumeInGb``,
        and ``containerDiskInGb`` are accepted.  ``dockerArgs`` is the only
        free-form string that round-trips through both ``podEditJob`` (write)
        and ``pod { dockerArgs }`` (read).

        The value written is a compact JSON string:
        ``{"_kinoforge_hb": "<ISO8601 timestamp>"}``
        so ``read`` can distinguish a kinoforge heartbeat write from a
        genuine docker start command that happens to be present on the pod.

        Idempotent: writing the same value twice overwrites the same field.

        Args:
            instance_id: Pod id whose heartbeat to stamp.
            ts_local: Timezone-aware datetime in local TZ.

        Raises:
            TransportError: HTTP non-2xx, GraphQL ``errors`` array, or
                any other transport-layer failure.
        """
        docker_args_value = json.dumps(
            {_HEARTBEAT_JSON_KEY: ts_local.isoformat()}, separators=(",", ":")
        )
        payload = {
            "query": _POD_EDIT_JOB_MUTATION,
            "variables": {
                "input": {
                    "podId": instance_id,
                    "dockerArgs": docker_args_value,
                }
            },
        }
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface ANY transport flake as TransportError
            raise TransportError(f"RunPod podEditJob transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod podEditJob failed: {resp['errors']}")

    def read(self, instance_id: str) -> datetime | None:
        """Read the heartbeat from ``instance_id``'s ``dockerArgs`` field.

        The value written by :meth:`write` is a compact JSON string of the
        form ``{"_kinoforge_hb": "<ISO8601>"}`` stored in the pod's
        ``dockerArgs`` field.  ``read`` retrieves that field and parses the
        embedded timestamp.

        Args:
            instance_id: Pod id whose heartbeat to read.

        Returns:
            The parsed timestamp, or ``None`` when the pod is gone or
            ``dockerArgs`` has not been written by kinoforge yet.

        Raises:
            TransportError: HTTP failure, GraphQL ``errors`` array, JSON
                parse failure, or a ``dockerArgs`` value that appears to
                be a kinoforge heartbeat but cannot be parsed as ISO-8601.
        """
        payload = {
            "query": _POD_QUERY,
            "variables": {"podId": instance_id},
        }
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod pod query transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod pod query failed: {resp['errors']}")
        pod = resp.get("data", {}).get("pod")
        if pod is None:
            return None  # instance gone — valid None
        raw = pod.get("dockerArgs")
        if not isinstance(raw, str) or raw == "":
            return None  # never written by kinoforge — valid None
        # Attempt to parse as kinoforge heartbeat JSON; ignore if it looks
        # like a genuine docker command string.
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None  # not a kinoforge heartbeat payload — valid None
        if not isinstance(parsed, dict) or _HEARTBEAT_JSON_KEY not in parsed:
            return None  # kinoforge key absent — valid None
        value = parsed[_HEARTBEAT_JSON_KEY]
        if not isinstance(value, str):
            raise TransportError(
                f"corrupted heartbeat dockerArgs for {instance_id}: "
                f"key present but value not a string"
            )
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise TransportError(
                f"corrupted heartbeat dockerArgs for {instance_id}: {value!r}"
            ) from exc
