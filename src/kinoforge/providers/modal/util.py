"""Modal satisfier of the core UtilSnapshotEndpoint contract.

Resolves instance_id -> .modal.run URL (injected resolver, ledger-backed at
wire time), GETs the server's /util route, and maps the JSON to a UtilSnapshot.
Mirrors RunPodGraphQLUtilEndpoint's injected-http-seam shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from kinoforge.core.errors import TransportError
from kinoforge.core.util_endpoints import UtilSnapshot

__all__ = ["ModalUtilEndpoint"]


class _HttpResponse(Protocol):
    """Injected-seam response shape — a status code plus a JSON body accessor."""

    status_code: int

    def json(self) -> dict[str, object]:
        """Return the decoded JSON body as a mapping."""
        ...


def _default_http_get(url: str) -> _HttpResponse:
    """Default GET seam — urllib-based, returns an object with status_code + json()."""
    import json
    import urllib.error
    import urllib.request

    class _R:
        def __init__(self, status: int, body: dict[str, object]) -> None:
            self.status_code = status
            self._body = body

        def json(self) -> dict[str, object]:
            return self._body

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — https only
            return _R(resp.status, json.loads(resp.read().decode()))
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        return _R(exc.code, {})


class ModalUtilEndpoint:
    """Read GPU/CPU/mem util from a Modal pod's /util route."""

    def __init__(
        self,
        resolve_endpoint: Callable[[str], str | None],
        http_get: Callable[[str], _HttpResponse] | None = None,
    ) -> None:
        """Wire the id->URL resolver + the HTTP GET seam (test-injectable)."""
        self._resolve = resolve_endpoint
        self._http_get = http_get if http_get is not None else _default_http_get

    def read_util(self, instance_id: str) -> UtilSnapshot | None:
        """Return a :class:`UtilSnapshot` for ``instance_id``, or None.

        Args:
            instance_id: The Modal instance ID (resolved to a .modal.run URL
                via the injected resolver).

        Returns:
            A UtilSnapshot, or None when the endpoint is unresolved (resolver
            yields None) or the /util route returns HTTP 404 (pod gone).

        Raises:
            TransportError: The /util route returned an HTTP 5xx (or any other
                non-2xx that is not 404) — surfaced so consumers can tolerate.
        """
        base = self._resolve(instance_id)
        if not base:
            return None
        resp = self._http_get(base.rstrip("/") + "/util")
        status = getattr(resp, "status_code", 0)
        if status == 404:
            return None
        if status < 200 or status >= 300:
            raise TransportError(
                f"modal /util for {instance_id} returned HTTP {status}"
            )
        body = resp.json() or {}

        def _f(key: str) -> float | None:
            v = body.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        up = body.get("uptime_seconds")
        return UtilSnapshot(
            gpu_util_percent=_f("gpu_util_percent"),
            cpu_percent=_f("cpu_percent"),
            memory_percent=_f("memory_percent"),
            disk_percent=_f("disk_percent"),
            uptime_seconds=int(up) if isinstance(up, (int, float)) else None,
        )
