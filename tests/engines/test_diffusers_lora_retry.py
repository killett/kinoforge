"""Tests for DiffusersBackend.set_lora_stack() retry-wrapping (Task 22).

The retry wrap MUST NOT swallow the _raise_lora_swap_error semantic-error
path: a non-transient exception carrying `.status` + `.body` dict attributes
must still be routed to _raise_lora_swap_error and produce a typed
LoraSwap* exception.  Only transient codes and transport errors retry.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from kinoforge.core.errors import (
    LoraSwapDownloadError,
    LoraSwapPodUnreachableError,
)
from kinoforge.core.interfaces import ModelProfile
from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers import DiffusersBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _HTTPErrorWithBody(Exception):
    """Test double for the diffusers HTTP client's wrapped response error.

    The real diffusers HTTP client attaches `.status` (int) and `.body`
    (dict) to response errors.  Tests synthesise these here so that
    _raise_lora_swap_error's ``getattr(e, 'status')`` + ``getattr(e,
    'body')`` inspection finds them.

    This class intentionally does NOT inherit from URLError/OSError so
    that retry_proxy_call's ``catch_classes`` tuple does NOT absorb it —
    it propagates immediately on the first attempt and lands in
    set_lora_stack's except block.
    """

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def _make_backend(
    http_post: Any,
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
    return DiffusersBackend(
        http_post=http_post,
        http_get=lambda _u: {},
        base_url="http://pod.example",
        probe_profile=profile,
        sleep=lambda _s: None,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lora_recovers_from_transient_502() -> None:
    """set_lora_stack() retries and returns the response after one transient 502.

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
        return {"inventory": [], "free_bytes": 4096, "swap_rejected": None}

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

    Mechanism: retry_proxy_call re-raises the last HTTPError; vanilla
    HTTPError lacks `.status`/`.body`, so set_lora_stack's except block
    falls through to `raise LoraSwapPodUnreachableError(...)`.
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
    """set_lora_stack() retries and succeeds after one TLS connection reset.

    Bug caught: if the URLError catch_classes guard is absent, a TLS reset
    propagates on the first attempt; because ConnectionResetError lacks
    .status/.body it becomes LoraSwapPodUnreachableError — a false failure.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset"))
        return {"inventory": [], "free_bytes": 4096, "swap_rejected": None}

    backend = _make_backend(http_post)
    resp = backend.set_lora_stack(
        pod_id="pod-abc",
        active_stack=[LoraEntry(ref="civitai:Z@1", strength=0.9)],
        download_specs={},
    )
    assert resp["free_bytes"] == 4096
    assert attempts["n"] == 2


def test_lora_non_transient_with_body_routes_to_raise_lora_swap_error() -> None:
    """Non-transient exception with .status+.body reaches _raise_lora_swap_error.

    Bug caught: if the retry wrap absorbs or re-wraps the exception before
    set_lora_stack's except block inspects it, the `.status`/`.body`
    attributes are lost and _raise_lora_swap_error is never called,
    returning LoraSwapPodUnreachableError instead of the typed error.

    Mechanism: _HTTPErrorWithBody does not inherit from URLError/OSError,
    so retry_proxy_call does not catch it — it propagates on attempt 1.
    set_lora_stack's except block sees .status=502 and .body with
    error='lora_download_failed', so _raise_lora_swap_error fires and
    raises LoraSwapDownloadError.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise _HTTPErrorWithBody(
            status=502,
            body={
                "error": "lora_download_failed",
                "evict_completed": [],
                "download_failed": "civitai:A@1",
                "underlying": "timeout",
            },
        )

    backend = _make_backend(http_post)
    with pytest.raises(LoraSwapDownloadError) as exc_info:
        backend.set_lora_stack(
            pod_id="pod-abc",
            active_stack=[LoraEntry(ref="civitai:A@1")],
            download_specs={},
        )
    # Typed error carries the pod_id and failing ref — not a generic unreachable.
    assert exc_info.value.pod_id == "pod-abc"
    assert exc_info.value.ref == "civitai:A@1"
    # _HTTPErrorWithBody is not in catch_classes → never retried.
    assert attempts["n"] == 1
