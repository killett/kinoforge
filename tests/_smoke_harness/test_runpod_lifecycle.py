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


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_instance_class(status: str) -> Any:
    class _I:
        def __init__(self, id: str) -> None:  # noqa: A002
            self.id = id
            self.status = status

    return _I


def test_teardown_pod_or_raise_raises_when_post_condition_pod_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: subprocess fallback ``check=False`` + no stderr inspection
    swallows non-zero exit silently. The 2026-06-23 money-leak vector.

    The new helper performs a post-condition probe via
    ``provider.get_instance(pod_id)``; if the pod is still alive,
    raise with the full breadcrumb (sweep failures + fallback output)
    embedded in the message.
    """
    pod_id = "leaked-pod-1"

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return [_FakeInstance(pod_id)]

        def destroy_instance(self, _pod_id: str) -> None:
            raise RuntimeError("graphql 502")

        def get_instance(self, queried_id: str) -> Any:
            assert queried_id == pod_id
            return _fake_instance_class("ready")(pod_id)

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())
    monkeypatch.setattr(
        runpod_lifecycle,
        "_kinoforge_destroy_subprocess",
        lambda _pid, _repo: _FakeCompletedProcess(
            returncode=1, stderr="instance 'leaked-pod-1' not found in ledger\n"
        ),
    )
    with pytest.raises(AssertionError) as excinfo:
        runpod_lifecycle.teardown_pod_or_raise(pod_id)
    msg = str(excinfo.value)
    # Pin three breadcrumb fragments: pod id, sweep failure cause, and
    # fallback exit code — each must reach the operator on failure.
    assert pod_id in msg
    assert "graphql 502" in msg
    assert "exit=1" in msg or "returncode=1" in msg


def test_teardown_pod_or_raise_no_raise_when_pod_confirmed_gone_via_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: an over-eager helper that raises on the happy path would
    fail every green smoke run and obscure real failures."""
    pod_id = "gone-pod"

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return []

        def destroy_instance(self, _pod_id: str) -> None: ...

        def get_instance(self, _pid: str) -> Any:
            raise KeyError(_pid)

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())
    runpod_lifecycle.teardown_pod_or_raise(pod_id)


def test_teardown_pod_or_raise_runs_subprocess_fallback_when_sweep_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a sweep that reports the pod in ``failures`` but no
    subprocess fallback fires leaves the pod alive silently.

    Pins that the fallback is invoked exactly once with the requested
    pod id, and post-condition KeyError → no raise.
    """
    pod_id = "transient-fail-pod"
    fallback_calls: list[tuple[str, Path]] = []

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return [_FakeInstance(pod_id)]

        def destroy_instance(self, _pid: str) -> None:
            raise RuntimeError("transient")

        def get_instance(self, _pid: str) -> Any:
            raise KeyError(_pid)  # pod gone after fallback

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())

    def _spy_subprocess(pid: str, repo: Path) -> Any:
        fallback_calls.append((pid, repo))
        return _FakeCompletedProcess(returncode=0, stdout="destroyed: " + pid)

    monkeypatch.setattr(
        runpod_lifecycle, "_kinoforge_destroy_subprocess", _spy_subprocess
    )
    runpod_lifecycle.teardown_pod_or_raise(pod_id)
    assert len(fallback_calls) == 1
    assert fallback_calls[0][0] == pod_id


def test_teardown_pod_or_raise_skips_fallback_when_sweep_reaped_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: an always-fire fallback wastes a 60-120 s subprocess call
    on every clean smoke (doubles the per-test budget cap)."""
    pod_id = "clean-pod"
    fallback_calls: list[Any] = []

    class _Provider:
        def list_instances(self) -> list[_FakeInstance]:
            return [_FakeInstance(pod_id)]

        def destroy_instance(self, _pid: str) -> None: ...

        def get_instance(self, _pid: str) -> Any:
            raise KeyError(_pid)

    monkeypatch.setattr(runpod_lifecycle, "_get_runpod_provider", lambda: _Provider())

    def _spy_subprocess(pid: str, repo: Path) -> Any:
        fallback_calls.append((pid, repo))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(
        runpod_lifecycle, "_kinoforge_destroy_subprocess", _spy_subprocess
    )
    runpod_lifecycle.teardown_pod_or_raise(pod_id)
    assert fallback_calls == []


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
