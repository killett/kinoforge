"""RunPod GraphQL ``clientBalance`` reader for B2 / Layer X.

One method. Mirrors :class:`kinoforge.providers.runpod.heartbeat.RunPodGraphQLHeartbeatEndpoint`
constructor + injected-seam shape. Hits the same
``https://api.runpod.io/graphql`` endpoint as the heartbeat satisfier;
distinct query, distinct concern, no shared transport.

Auth: Bearer header + ``User-Agent`` are BOTH required by the live
RunPod GraphQL gateway. Bearer-only returns HTTP 403; query-param
``?api_key=`` also returns 403 against this query path. Confirmed
during B2 t2 fixture capture (2026-06-12).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.balance_endpoints import ProviderBalance, TransportError

_QUERY = "{ myself { clientBalance } }"
_URL = "https://api.runpod.io/graphql"
_UA = "kinoforge-cost/0.1"


def _default_http_post(
    api_key: str,
) -> Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]:
    """Build the default POST closure with the operator's key baked in.

    Separate factory because the balance signature also takes a
    ``headers`` dict so tests can spy the exact wire shape.
    """

    def _post(
        url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        req = urllib.request.Request(  # noqa: S310
            url,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise TransportError(f"runpod-balance transport: {exc}") from exc

    return _post


class RunPodBalanceEndpoint:
    """Read the operator's RunPod account balance via GraphQL.

    Construction binds the API key; ``read()`` takes no arguments.
    Failure contract per :class:`kinoforge.core.balance_endpoints.BalanceEndpoint`:
        * Transport / shape drift -> raise :class:`TransportError`.
        * Missing credential (``api_key`` is ``None`` or empty) -> return
          ``None`` without making any wire call.
        * Negative balance -> return verbatim.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
        | None = None,
    ) -> None:
        """Bind credentials and inject the transport seam.

        Args:
            api_key: RunPod API key (any scope; read-only suffices).
            http_post: Optional seam. ``None`` builds the default urllib closure.
        """
        self._api_key = api_key
        self._http_post = (
            http_post if http_post is not None else _default_http_post(api_key or "")
        )

    def read(self) -> ProviderBalance | None:
        """Read the account balance.

        Returns:
            A fresh :class:`ProviderBalance`, or ``None`` when the API key
            is missing.

        Raises:
            TransportError: Transport failure or response-shape drift.
        """
        if not self._api_key:
            return None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        }
        body = {"query": _QUERY}
        resp = self._http_post(_URL, body, headers)
        try:
            raw = resp["data"]["myself"]["clientBalance"]
            usd = float(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise TransportError(f"runpod-balance schema drift: {exc}") from exc
        return ProviderBalance(
            usd=usd,
            as_of=datetime.now(),
            source="runpod-graphql-clientBalance",
        )
