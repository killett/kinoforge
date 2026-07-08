"""ComfyUIBackend honors a CancelToken passed via .result()."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from kinoforge.core import Cancelled, CancelToken
from kinoforge.core.interfaces import ModelProfile
from kinoforge.engines.comfyui import ComfyUIBackend

_PROBE = ModelProfile(
    name="comfyui",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _unused_post(_url: str, _body: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("submit should not POST in result()-only tests")


def test_result_raises_cancelled_on_preset_token() -> None:
    """Pre-set token short-circuits before the first http_get call.

    Bug: today ComfyUIBackend.result polls forever with no cancellation
    mechanism — the reason `kinoforge generate` hangs on Ctrl-C.
    """
    calls: list[str] = []

    def _http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"outputs": {}}

    backend = ComfyUIBackend(
        http_post=_unused_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.01,
        poll_timeout_s=1.0,
    )
    token = CancelToken()
    token.set()

    with pytest.raises(Cancelled):
        backend.result("job-123", cancel_token=token)

    assert calls == [], f"expected no http_get calls, got {calls}"


def test_result_cancel_during_transient_retry_aborts_fast() -> None:
    """A token fired mid-retry-storm aborts inside the inner retry, not after it.

    Bug caught (2026-07-07 reclaim): the pod dies mid-job and the poll's inner
    retry_proxy_call burned its full backoff (~7 attempts) against the dead pod
    before the outer loop could honor the cancel. Threading the token into the
    inner retry aborts on the first failed attempt (calls == 1, not 7).
    """
    import urllib.error

    calls = {"n": 0}
    token = CancelToken()

    def _http_get(url: str) -> dict[str, Any]:
        calls["n"] += 1
        token.set()  # pod reclaimed mid-job → POD_GONE sets the token
        raise urllib.error.HTTPError(url=url, code=404, msg="gone", hdrs=None, fp=None)  # type: ignore[arg-type]

    backend = ComfyUIBackend(
        http_post=_unused_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.0,
        poll_timeout_s=60.0,
        sleep=lambda _s: None,
    )
    with pytest.raises(Cancelled):
        backend.result("job-789", cancel_token=token)
    assert calls["n"] == 1


def test_result_honors_token_set_during_wait() -> None:
    """Token set after one poll tick raises Cancelled within ~poll_interval_s."""
    tick_count = [0]

    def _http_get(url: str) -> dict[str, Any]:
        tick_count[0] += 1
        return {}  # never complete

    backend = ComfyUIBackend(
        http_post=_unused_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.05,
        poll_timeout_s=60.0,
    )
    token = CancelToken()

    def _setter() -> None:
        time.sleep(0.1)
        token.set()

    threading.Thread(target=_setter, daemon=True).start()
    with pytest.raises(Cancelled):
        backend.result("job-456", cancel_token=token)

    # Should have ticked 1-10 times before cancellation, not hundreds.
    assert tick_count[0] < 10, (
        f"too many ticks ({tick_count[0]}) — wait not interruptible"
    )
