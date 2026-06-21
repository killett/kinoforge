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
    assert sorted(out) == ["a", "b"]
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
    assert out == ["t3"]
    assert destroyed == ["t3"]


def test_destroy_swallows_per_pod_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: one destroy failure aborts sweep → other pods leak."""
    destroyed: list[str] = []

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return [
                _FakeInstance("good-1"),
                _FakeInstance("bad"),
                _FakeInstance("good-2"),
            ]

        def destroy_instance(self, pod_id: str) -> None:
            if pod_id == "bad":
                raise RuntimeError("transient")
            destroyed.append(pod_id)

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())
    out = runpod_lifecycle.destroy_all_active_pods()
    assert sorted(destroyed) == ["good-1", "good-2"]
    assert "bad" not in out


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
