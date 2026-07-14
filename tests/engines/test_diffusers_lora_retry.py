"""Tests for DiffusersBackend.set_lora_stack() retry-wrapping (Task 22).

The retry wrap MUST NOT swallow the _raise_lora_swap_error semantic-error
path: a non-transient urllib.error.HTTPError whose body decodes to a
structured FastAPI detail dict must still be routed to _raise_lora_swap_error
and produce a typed LoraSwap* exception.  Only transient codes and transport
errors retry.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from kinoforge.core.errors import (
    LoraSwapDiskFullError,
    LoraSwapPodUnreachableError,
)
from kinoforge.core.interfaces import ModelProfile
from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers import DiffusersBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(
    http_post: Any,
    http_get: Any = None,
) -> DiffusersBackend:
    profile = ModelProfile(
        name="wan-2.2",
        max_frames=81,
        fps=24,
        supported_modes={"t2v"},
        max_resolution=(1024, 1024),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    if http_get is None:
        http_get = lambda _u: {  # noqa: E731
            "state": "done",
            "inventory": [],
            "free_bytes": 4096,
            "swap_rejected": None,
            "error": None,
        }
    return DiffusersBackend(
        http_post=http_post,
        http_get=http_get,
        base_url="http://pod.example",
        probe_profile=profile,
        sleep=lambda _s: None,
        poll_interval_s=0.0,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


def _http_error_with_body(code: int, detail: dict[str, Any]) -> urllib.error.HTTPError:
    """Return a real urllib.error.HTTPError with a FastAPI-shaped JSON body.

    FastAPI serialises HTTPException(status_code=N, detail=<dict>) as
    {"detail": <dict>}.  Using a real HTTPError (not a fabricated class)
    ensures the decode path in set_lora_stack's except block is exercised
    faithfully — a fabricated class with .body would bypass e.read() entirely.
    """
    body = json.dumps({"detail": detail}).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lora_recovers_from_transient_502() -> None:
    """set_lora_stack() retries submit and polls to done after one transient 502.

    Bug caught: if set_lora_stack() calls _http_post bare (no retry wrap),
    the first 502 reaches the except block, which (lacking .status/.body)
    raises LoraSwapPodUnreachableError — a false failure for a transient
    proxy hiccup.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _http_error(502)
        return {"job_id": "s-retry-ok"}

    backend = _make_backend(http_post)
    resp = backend.set_lora_stack(
        pod_id="pod-abc",
        active_stack=[],
        download_specs={},
    )
    assert resp["swap_rejected"] is None
    assert attempts["n"] == 2


def test_lora_exhaustion_raises_pod_unreachable() -> None:
    """Backoff exhaustion on transient 503s raises LoraSwapPodUnreachableError.

    Bug caught: if the finally-exhausted URLError/HTTPError leaks directly
    as an untyped exception, callers' `except LoraSwapPodUnreachableError`
    blocks miss it and the error propagates silently.

    Mechanism: retry_proxy_call re-raises the last HTTPError(503); 503 has
    no FastAPI body (fp=None), so e.read() fails silently and detail stays
    None; set_lora_stack's except block falls through to
    `raise LoraSwapPodUnreachableError(...)`.
    """

    attempts = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise _http_error(503)

    backend = _make_backend(http_post)
    with pytest.raises(LoraSwapPodUnreachableError) as exc_info:
        backend.set_lora_stack(
            pod_id="pod-abc",
            active_stack=[],
            download_specs={},
        )
    assert exc_info.value.pod_id == "pod-abc"
    # 7 attempts = 1 initial + 6 retries (RUNPOD_PROXY_POLICY.backoffs).
    assert attempts["n"] == 7


def test_lora_tls_reset_recovers() -> None:
    """set_lora_stack() retries submit after TLS reset, then polls to done.

    Bug caught: if the URLError catch_classes guard is absent, a TLS reset
    propagates on the first attempt; because ConnectionResetError is not a
    urllib.error.HTTPError it falls into the bare except-Exception branch and
    becomes LoraSwapPodUnreachableError — a false failure on a transient reset.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset"))
        return {"job_id": "s-tls-ok"}

    backend = _make_backend(http_post)
    resp = backend.set_lora_stack(
        pod_id="pod-abc",
        active_stack=[LoraEntry(ref="civitai:Z@1", strength=0.9)],
        download_specs={},
    )
    assert resp["free_bytes"] == 4096
    assert attempts["n"] == 2


def test_lora_non_transient_with_body_routes_to_raise_lora_swap_error() -> None:
    """Non-transient HTTPError(507) with FastAPI body reaches _raise_lora_swap_error.

    Bug caught: the old except block used getattr(e, 'body', None) which only
    worked for fabricated test doubles — a real urllib.error.HTTPError has no
    .body attribute, so a server 507 silently degraded to LoraSwapPodUnreachableError.
    The fix decodes e.read() to extract {"detail": <dict>} (FastAPI wire shape)
    and routes through _raise_lora_swap_error, raising LoraSwapDiskFullError.

    Mechanism: HTTPError(507) is non-transient (507 not in transient_codes={404,
    502,503,504}) so retry_proxy_call re-raises immediately on attempt 1.
    set_lora_stack's except urllib.error.HTTPError branch decodes the body,
    extracts detail["error"]="disk_full", and _raise_lora_swap_error raises
    LoraSwapDiskFullError.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise _http_error_with_body(
            507,
            {
                "error": "disk_full",
                "phase": "plan",
                "evict_completed": [],
                "download_failed": "civitai:A@1",
            },
        )

    backend = _make_backend(http_post)
    with pytest.raises(LoraSwapDiskFullError) as exc_info:
        backend.set_lora_stack(
            pod_id="pod-abc",
            active_stack=[LoraEntry(ref="civitai:A@1")],
            download_specs={},
        )
    # Typed error carries pod_id — not a generic unreachable.
    assert exc_info.value.pod_id == "pod-abc"
    # HTTPError(507) is non-transient → never retried; fires on attempt 1.
    assert attempts["n"] == 1
