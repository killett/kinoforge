"""RunPod GraphQL-tag heartbeat satisfier (B5a Task b).

Implements :class:`~kinoforge.core.heartbeat_endpoints.HeartbeatEndpoint`
by reading/writing a well-known tag (``_kinoforge_last_heartbeat``) on
the RunPod pod resource via the GraphQL ``podEditJob`` mutation and the
``pod`` query. Both methods go through an injected ``http_post`` seam so
tests can spy the precise wire payload without a real RunPod account.

The tag survives across orchestrator process lifetimes; this is what
makes the cross-session warm-reuse path (B3) workable on a fresh shell
— the previous orchestrator's last write persists in RunPod's tag store
and the next orchestrator reads it back without any local state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.errors import TransportError

__all__ = ["HEARTBEAT_TAG_KEY", "RunPodGraphQLHeartbeatEndpoint"]

#: Tag key written to RunPod pods. Underscore prefix marks kinoforge-internal
#: so operators reading tags in the RunPod console can recognise the
#: namespace.
HEARTBEAT_TAG_KEY: str = "_kinoforge_last_heartbeat"

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"

_POD_EDIT_JOB_MUTATION: str = """
mutation PodEditJob($input: PodEditJobInput!) {
  podEditJob(input: $input) { id }
}
""".strip()

_POD_QUERY: str = """
query GetPod($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    tags { key value }
  }
}
""".strip()


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
        """Stamp the heartbeat tag on ``instance_id`` with ``ts_local``.

        Idempotent: writing the same value twice rewrites the same slot.

        Args:
            instance_id: Pod id whose heartbeat to stamp.
            ts_local: Timezone-aware datetime in local TZ.

        Raises:
            TransportError: HTTP non-2xx, GraphQL ``errors`` array, or
                any other transport-layer failure.
        """
        payload = {
            "query": _POD_EDIT_JOB_MUTATION,
            "variables": {
                "input": {
                    "podId": instance_id,
                    "tags": [{"key": HEARTBEAT_TAG_KEY, "value": ts_local.isoformat()}],
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
        """Read the heartbeat tag on ``instance_id``.

        Args:
            instance_id: Pod id whose heartbeat to read.

        Returns:
            The parsed timestamp, or ``None`` when the pod is gone or the
            tag has never been written.

        Raises:
            TransportError: HTTP failure, GraphQL ``errors`` array, JSON
                parse failure, or a tag value that is present but not
                parseable as an ISO-8601 datetime.
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
        for tag in pod.get("tags") or []:
            if tag.get("key") == HEARTBEAT_TAG_KEY:
                value = tag.get("value")
                if not isinstance(value, str):
                    raise TransportError(
                        f"corrupted heartbeat tag for {instance_id}: value not a string"
                    )
                try:
                    return datetime.fromisoformat(value)
                except ValueError as exc:
                    raise TransportError(
                        f"corrupted heartbeat tag for {instance_id}: {value!r}"
                    ) from exc
        return None  # never written — valid None
