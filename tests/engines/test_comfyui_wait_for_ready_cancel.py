"""C29 — ComfyUIEngine.wait_for_ready honors CancelToken (boot-phase reap path)."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled
from kinoforge.core.interfaces import Instance
from kinoforge.engines.comfyui import ComfyUIEngine


def _instance(status: str = "ready") -> Instance:
    return Instance(
        id="pod-c29",
        provider="runpod",
        status=status,
        created_at=0.0,
        endpoints={"8188": "https://pod-c29-8188.proxy.runpod.net"},
    )


def test_wait_for_ready_raises_cancelled_when_token_set_before_poll() -> None:
    """Token already set → Cancelled raised before any http_get call."""
    token = CancelToken()
    token.set()
    http_get_calls: list[str] = []

    def http_get(url: str) -> dict[str, Any]:
        http_get_calls.append(url)
        return {}

    with pytest.raises(Cancelled):
        ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            _instance(),
            http_get=http_get,
            sleep=lambda _s: None,
            get_instance=lambda _id: _instance(),
            timeout_s=5.0,
            cancel_token=token,
        )
    assert http_get_calls == [], "http_get must not be called when token is pre-set"


def test_wait_for_ready_raises_cancelled_when_token_set_mid_poll() -> None:
    """Token set during sleep on iter 2 → next iter (3) observes it and raises."""
    token = CancelToken()
    poll_count = {"n": 0}

    def http_get(url: str) -> dict[str, Any]:
        raise RuntimeError("not yet")

    def sleep(_s: float) -> None:
        poll_count["n"] += 1
        if poll_count["n"] == 2:
            token.set()

    with pytest.raises(Cancelled):
        ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            _instance(),
            http_get=http_get,
            sleep=sleep,
            get_instance=lambda _id: _instance(),
            timeout_s=60.0,
            cancel_token=token,
        )
    assert poll_count["n"] == 2, (
        "expected the token to be observed at the top of iter 3 (set during iter 2 sleep)"
    )


def test_wait_for_ready_no_cancel_token_preserves_today_behavior() -> None:
    """cancel_token=None (default) keeps pre-C29 behaviour byte-identical."""
    calls = {"n": 0}

    def http_get(url: str) -> dict[str, Any]:
        calls["n"] += 1
        return {"system_stats": True}

    ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        _instance(),
        http_get=http_get,
        sleep=lambda _s: None,
        get_instance=lambda _id: _instance(),
        timeout_s=5.0,
    )
    assert calls["n"] == 1
