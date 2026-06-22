"""Tests for the shared proxy-retry helper."""

from __future__ import annotations


def test_module_loads_and_exposes_public_surface() -> None:
    from kinoforge.engines._proxy_retry import (
        RUNPOD_PROXY_POLICY,
        RetryPolicy,
        interpoll_wait,
        retry_proxy_call,
    )

    assert isinstance(RUNPOD_PROXY_POLICY, RetryPolicy)
    assert callable(retry_proxy_call)
    assert callable(interpoll_wait)
