"""Shared Bearer-auth GraphQL POST closure for RunPod satellite modules.

One decision — the RunPod gateway auth / timeout / error-mapping policy —
previously copy-pasted byte-identically (modulo User-Agent) between the
C25 heartbeat and C26 util endpoints. ``balance.py`` keeps its own third
variant deliberately (documented no-shared-transport intent there).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from kinoforge.core.errors import TransportError

__all__ = ["bearer_graphql_post"]


def bearer_graphql_post(
    api_key: str, user_agent: str
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build a stdlib-urllib POST callable with Bearer auth.

    Args:
        api_key: RunPod API key (Bearer-auth header value).
        user_agent: Per-module User-Agent string (e.g.
            ``"kinoforge-heartbeat/0.1"``).

    Returns:
        A callable ``post(url, payload) -> decoded_json`` that raises
        :class:`~kinoforge.core.errors.TransportError` on HTTP, transport,
        or JSON-decode failure.
    """

    def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": user_agent,
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
