"""runpod_lifecycle helpers: proxy URL, leak sweep, stat poller."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests._smoke_harness import runpod_lifecycle


def test_resolve_proxy_url_uses_well_known_pattern() -> None:
    """Bug: helper drifts from {pod_id}-{port}.proxy.runpod.net pattern."""
    assert (
        runpod_lifecycle.resolve_proxy_url("abc123")
        == "https://abc123-8000.proxy.runpod.net"
    )
    assert (
        runpod_lifecycle.resolve_proxy_url("xyz", port=9000)
        == "https://xyz-9000.proxy.runpod.net"
    )


class _FakeInstance:
    def __init__(self, id: str, tags: dict[str, str] | None = None) -> None:  # noqa: A002
        self.id = id
        self.tags = tags or {}


def test_destroy_all_active_pods_reaps_everything_when_no_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper requires tag filter → untagged leaks linger."""
    destroyed: list[str] = []

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return [_FakeInstance("a"), _FakeInstance("b")]

        def destroy_instance(self, pod_id: str) -> None:
            destroyed.append(pod_id)

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())
    out = runpod_lifecycle.destroy_all_active_pods()
    assert sorted(out.destroyed) == ["a", "b"]
    assert out.failures == {}
    assert sorted(destroyed) == ["a", "b"]


def test_destroy_all_active_pods_honors_tag_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: tier-3 sweep reaps a tier-4 pod sharing the workspace."""
    destroyed: list[str] = []

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return [
                _FakeInstance("t3", {"smoke_tier": "kinoforge-smoke-tier-3"}),
                _FakeInstance("t4", {"smoke_tier": "kinoforge-smoke-tier-4"}),
                _FakeInstance("none"),
            ]

        def destroy_instance(self, pod_id: str) -> None:
            destroyed.append(pod_id)

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())
    out = runpod_lifecycle.destroy_all_active_pods(tag_filter="kinoforge-smoke-tier-3")
    assert out.destroyed == ["t3"]
    assert out.failures == {}
    assert destroyed == ["t3"]


def test_destroy_all_active_pods_reports_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: one destroy failure was logged at WARNING and dropped from
    the return — no programmatic channel for the caller to detect a
    leak. This is the 2026-06-23 money-leak vector.

    Verifies the new SweepResult contract: ``destroyed`` carries the
    pods that left cleanly, ``failures`` maps every pod_id whose
    destroy raised to the actual exception so the caller can react.
    """
    destroyed: list[str] = []
    bad_exc = RuntimeError("transient RunPod 502")

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return [
                _FakeInstance("good-1"),
                _FakeInstance("bad"),
                _FakeInstance("good-2"),
            ]

        def destroy_instance(self, pod_id: str) -> None:
            if pod_id == "bad":
                raise bad_exc
            destroyed.append(pod_id)

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())
    out = runpod_lifecycle.destroy_all_active_pods()
    assert sorted(out.destroyed) == ["good-1", "good-2"]
    assert list(out.failures.keys()) == ["bad"]
    # Exception object reference preserved so callers can re-raise or log
    # the original cause, not a lossy str() of it.
    assert out.failures["bad"] is bad_exc


def test_sweepresult_contains_delegates_to_destroyed() -> None:
    """Bug: a SweepResult that loses ``__contains__`` would silently
    flip the smoke fixtures' ``if pod_id not in reaped:`` to
    always-true, firing the subprocess fallback on every run and
    masking the real failure mode the fallback is supposed to expose.
    """
    result = runpod_lifecycle.SweepResult(
        destroyed=["a", "b"], failures={"c": RuntimeError()}
    )
    assert "a" in result
    assert "b" in result
    # Pods in the failures dict are NOT "in" the result — the smoke
    # fixture must treat them as not-yet-reaped.
    assert "c" not in result
    assert "missing" not in result


def test_stat_poller_writes_per_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug: poller swallows snapshot or crashes on None."""

    class _Snap:
        gpu_util_percent = 42.0
        cpu_percent = 11.0
        memory_percent = 33.0

    class _Endpoint:
        def __init__(self, *_args: Any, **_kw: Any) -> None: ...

        def read_util(self, _pod_id: str) -> Any:
            return _Snap()

    monkeypatch.setattr(runpod_lifecycle, "_build_util_endpoint", lambda: _Endpoint())
    log = tmp_path / "stats.log"
    poller = runpod_lifecycle.PodStatPoller("pod-x", log, interval_s=0.05)
    poller.start()
    time.sleep(0.2)
    poller.stop()
    poller.join(timeout=1.0)
    body = log.read_text()
    assert "gpu_util=42.0" in body
    assert "cpu=11.0" in body


def test_stat_poller_handles_none_snapshot_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug: early-boot returns None → poller raises AttributeError."""

    class _Endpoint:
        def __init__(self, *_args: Any, **_kw: Any) -> None: ...

        def read_util(self, _pod_id: str) -> Any:
            return None

    monkeypatch.setattr(runpod_lifecycle, "_build_util_endpoint", lambda: _Endpoint())
    log = tmp_path / "stats.log"
    poller = runpod_lifecycle.PodStatPoller("pod-x", log, interval_s=0.05)
    poller.start()
    time.sleep(0.15)
    poller.stop()
    poller.join(timeout=1.0)
    assert "runtime not yet visible" in log.read_text()
