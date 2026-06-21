"""Shared HTTP client for smoke tests.

Wraps urllib with the four kinoforge-internal patterns the live tier
needs. Every smoke tier MUST call into ``post_json``/``get_json``
instead of raw urllib so the patterns stay in one place.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_PROXY_UA = "kinoforge-smoke/0.1"
_RETRY_BACKOFFS_S: tuple[float, ...] = (0.5, 1.5, 4.5)


def _sleep(seconds: float) -> None:
    """Sleep seam — monkeypatched in tests to keep the suite fast."""
    time.sleep(seconds)


def _append_api_key(url: str) -> str:
    """Append ``?api_key=<RUNPOD_API_KEY>`` (or ``&api_key=...``) when env set."""
    key = os.environ.get("RUNPOD_API_KEY", "")
    if not key:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}api_key={urllib.parse.quote(key, safe='')}"


def _open_with_retry(req: urllib.request.Request, timeout: int) -> bytes:
    """urlopen with URLError retry budget; HTTPError propagates immediately.

    Before re-raising an ``HTTPError`` the response body is read into the
    exception's ``response_body`` attribute AND appended to its message
    so post-mortem doesn't require a live pod. urllib's default
    ``str(HTTPError)`` is ``"HTTP Error <code>: <reason>"`` which throws
    away the JSON body that the wan_t2v_server uses to carry the actual
    cause (e.g. ``{"error":"lora_download_failed","underlying":"..."}``).
    """
    last_exc: urllib.error.URLError | None = None
    for backoff in (*_RETRY_BACKOFFS_S, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return bytes(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body_bytes = exc.read() if exc.fp is not None else b""
            except Exception:  # noqa: BLE001
                body_bytes = b""
            body_text = body_bytes.decode("utf-8", errors="replace")[:2000]
            exc.response_body = body_text  # type: ignore[attr-defined]
            # Rebuild the args so str(exc) carries the body.
            if body_text:
                exc.msg = f"{exc.reason} :: body={body_text!r}"
            raise
        except urllib.error.URLError as exc:
            last_exc = exc
            if backoff is None:
                break
            _sleep(backoff)
    assert last_exc is not None
    raise last_exc


def post_json(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    """POST ``body`` as JSON; return parsed JSON response."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        _append_api_key(url),
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _PROXY_UA,
        },
        method="POST",
    )
    raw = _open_with_retry(req, timeout)
    return dict(json.loads(raw))


def get_json(url: str, *, timeout: int) -> dict[str, Any]:
    """GET; return parsed JSON response."""
    req = urllib.request.Request(  # noqa: S310
        _append_api_key(url),
        headers={
            "Accept": "application/json",
            "User-Agent": _PROXY_UA,
        },
    )
    raw = _open_with_retry(req, timeout)
    return dict(json.loads(raw))
