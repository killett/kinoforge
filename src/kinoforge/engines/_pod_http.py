"""Shared pod-HTTP client machinery for job-server engine adapters.

Extracted from the byte-identical triplicate in
:mod:`kinoforge.upscalers.spandrel._engine`,
:mod:`kinoforge.upscalers.flashvsr._engine`, and
:mod:`kinoforge.interpolators.rife._engine`, so the 502-warmup retry,
sha256 cross-check, Cloudflare UA gate, and timeout policies are one
decision instead of three. Per-engine seams stay in the engine modules:
each keeps a module-level ``_http_json`` (tests monkeypatch that name)
that delegates to :func:`http_json` with its own User-Agent, and all
error wording is parameterized so every message stays byte-identical
per engine.
"""

from __future__ import annotations

import hashlib
import json as _json
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast
from urllib.error import HTTPError

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import UploadIntegrityError
from kinoforge.core.interfaces import Instance
from kinoforge.engines._proxy_retry import interpoll_wait, retry_proxy_call

_DEFAULT_SERVER_PORT = "8000"

_HTTP_TIMEOUT_S = 60
"""Per-request timeout for plain JSON calls (submit / status polls)."""

_UPLOAD_TIMEOUT_S = 600
"""Per-request timeout for the PUT /upload body stream."""

_POLL_INTERVAL_S = 2.0
"""Inter-poll cadence for the job status loop."""


def http_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    user_agent: str,
) -> dict[str, Any]:
    """Send a JSON request to a pod endpoint and decode the JSON response.

    Args:
        method: HTTP method (``"GET"`` / ``"POST"``).
        url: Full endpoint URL (pod proxy, http/https only).
        payload: JSON body to send; ``None`` sends no body.
        user_agent: Engine-specific User-Agent string. Cloudflare
            (RunPod's proxy edge) returns 403 to the default
            Python-urllib User-Agent — a plain kinoforge UA clears the
            gate (same fix as DiffusersEngine).

    Returns:
        Decoded JSON response object.
    """
    data = _json.dumps(payload).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {"User-Agent": user_agent}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
        url,
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
        body = resp.read()
    return cast(dict[str, Any], _json.loads(body))


def submit_and_poll(
    *,
    label_prefix: str,
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    http_json: Callable[..., dict[str, Any]],
    make_error: Callable[[str, Any], Exception],
    cancel_token: CancelToken | None = None,
) -> tuple[dict[str, Any], float]:
    """Submit a pod job via POST, poll its status endpoint to completion.

    POSTs *payload* to ``{base_url}{endpoint}``, then polls
    ``{base_url}{endpoint}/status/{job_id}`` every
    :data:`_POLL_INTERVAL_S` seconds until the job reports ``done`` or
    ``error``. Both calls go through
    :func:`kinoforge.engines._proxy_retry.retry_proxy_call` for RunPod
    proxy startup-window 404/502 tolerance.

    Args:
        label_prefix: Engine tag for retry log labels
            (``"<prefix>.submit"`` / ``"<prefix>.status"``).
        base_url: Pod server base URL (no trailing slash).
        endpoint: Job endpoint path (e.g. ``"/upscale"`` or
            ``"/interpolate"``); the status URL is derived as
            ``{endpoint}/status/{job_id}``.
        payload: JSON submit body.
        http_json: JSON-call seam; the engine passes its module-level
            ``_http_json`` so tests keep patching one module attribute.
        make_error: ``(job_id, server_error) -> Exception`` factory
            raised when the server reports ``state == "error"``; keeps
            the engine-specific exception type and wording engine-local.
        cancel_token: Optional cooperative-cancellation token, honored
            before every poll and across every wait.

    Returns:
        Tuple of the server's ``result`` object for the completed job
        and the elapsed seconds from submit-acknowledged to done.

    Raises:
        Exception: Whatever *make_error* builds, when the job errors.
    """
    submit_url = f"{base_url}{endpoint}"
    submit_resp = retry_proxy_call(
        label=f"{label_prefix}.submit",
        url=submit_url,
        fn=lambda: http_json(method="POST", url=submit_url, payload=payload),
        sleep=time.sleep,
        cancel_token=cancel_token,
    )
    job_id: str = submit_resp["job_id"]

    status_url = f"{base_url}{endpoint}/status/{job_id}"
    t0 = time.monotonic()
    while True:
        if cancel_token is not None:
            cancel_token.raise_if_set()
        status = retry_proxy_call(
            label=f"{label_prefix}.status",
            url=status_url,
            fn=lambda: http_json(method="GET", url=status_url),
            sleep=time.sleep,
            cancel_token=cancel_token,
        )
        state = status["state"]
        if state == "done":
            return cast(dict[str, Any], status["result"]), time.monotonic() - t0
        if state == "error":
            raise make_error(job_id, status.get("error", ""))
        interpoll_wait(_POLL_INTERVAL_S, cancel_token, time.sleep)


class PodHTTPClientMixin:
    """Upload + base-URL helpers shared by pod-backed engine adapters.

    Subclasses must set :attr:`_pod_user_agent`. ``_put_upload`` and
    ``_upload_source`` keep their historical names and signatures
    because engine tests patch them on the engine instance
    (``patch.object`` / ``monkeypatch.setattr``).
    """

    _pod_user_agent: str
    """Engine-specific User-Agent sent on every pod HTTP call."""

    def _put_upload(
        self,
        url: str,
        data: IO[bytes],
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        """Single PUT /upload request — streams ``data``, parses JSON response.

        Split out so tests can patch HTTP without monkeypatching urllib
        globally, and so the retry loop in :meth:`_upload_source` can
        swap a fresh file handle on each attempt.

        Args:
            url: Full ``/upload`` endpoint URL.
            data: Readable binary body (fresh file handle per attempt).
            headers: Request headers, including the integrity metadata.
            timeout: Socket timeout in seconds.

        Returns:
            Decoded JSON response object.
        """
        req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
            url, data=data, method="PUT", headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return cast(dict[str, Any], _json.loads(resp.read().decode("utf-8")))

    def _upload_source(self, instance: Instance, local_path: Path) -> str:
        """Upload ``local_path`` mp4 to the pod via PUT /upload; return file:// URL.

        Computes sha256 locally, streams the file body as the PUT
        payload, and cross-checks the server's reported sha256 before
        returning. Recovers once from a proxy cold-warmup 502;
        subsequent failures bubble.

        Args:
            instance: Compute instance exposing the pod server endpoint.
            local_path: Local mp4 to upload.

        Returns:
            ``file://`` URL of the uploaded file on the pod.

        Raises:
            UploadIntegrityError: Server-reported sha256 does not match
                the locally computed one.
        """
        body = local_path.read_bytes()
        local_sha = hashlib.sha256(body).hexdigest()
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": "video/mp4",
            "X-Filename": f"{short}.mp4",
            "Content-Length": str(len(body)),
            "User-Agent": self._pod_user_agent,
        }

        last_error: HTTPError | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(2):
            with local_path.open("rb") as fobj:
                try:
                    payload = self._put_upload(
                        url, fobj, headers, timeout=_UPLOAD_TIMEOUT_S
                    )
                    last_error = None
                    break
                except HTTPError as exc:
                    last_error = exc
                    if exc.code == 502 and attempt == 0:
                        continue
                    raise
        if payload is None:
            raise RuntimeError(
                f"_upload_source loop completed without payload "
                f"(last_error={last_error!r})"
            )

        server_sha = str(payload.get("sha256", ""))
        if server_sha != local_sha:
            raise UploadIntegrityError(
                local_sha256=local_sha,
                server_sha256=server_sha,
                bytes_sent=len(body),
            )
        return f"file://{payload['path']}"

    def _base_url(self, instance: Instance) -> str:
        """Resolve the pod server base URL from ``instance.endpoints``.

        Args:
            instance: Compute instance whose endpoints map to probe.

        Returns:
            Base URL for port :data:`_DEFAULT_SERVER_PORT` (or the first
            endpoint as fallback), without a trailing slash.

        Raises:
            ValueError: No endpoint is available on the instance.
        """
        endpoints = instance.endpoints or {}
        url = endpoints.get(_DEFAULT_SERVER_PORT) or next(iter(endpoints.values()), "")
        if not url:
            raise ValueError(
                f"{type(self).__name__}: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")
