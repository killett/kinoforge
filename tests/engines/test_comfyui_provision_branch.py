"""Tests for ComfyUIEngine.provision's local-vs-remote branch."""

from __future__ import annotations

from typing import Any

from kinoforge.core.interfaces import Instance
from kinoforge.engines.comfyui import ComfyUIEngine


def test_provision_with_none_instance_runs_local_body() -> None:
    """instance=None → original local code path (run_cmd called)."""
    calls: list[tuple[list[str], str | None]] = []

    def _run_cmd(argv: list[str], cwd: str | None) -> None:
        calls.append((argv, cwd))

    engine = ComfyUIEngine(
        run_cmd=_run_cmd,
        probe_profile=None,  # type: ignore[arg-type]
    )
    engine.provision(
        None,
        {"engine": {"comfyui": {"custom_nodes": [], "launch_args": []}}, "models": []},
    )
    assert len(calls) > 0  # launch step always runs in local body


def test_provision_with_local_provider_runs_local_body() -> None:
    """instance.provider == 'local' → original local code path."""
    calls: list[tuple[list[str], str | None]] = []

    def _run_cmd(argv: list[str], cwd: str | None) -> None:
        calls.append((argv, cwd))

    engine = ComfyUIEngine(
        run_cmd=_run_cmd,
        probe_profile=None,  # type: ignore[arg-type]
    )
    inst = Instance(id="local-1", provider="local", status="ready", created_at=0.0)
    engine.provision(
        inst,
        {"engine": {"comfyui": {"custom_nodes": [], "launch_args": []}}, "models": []},
    )
    assert len(calls) > 0


def test_provision_with_remote_provider_calls_wait_for_ready_not_local_body() -> None:
    """instance.provider == 'runpod' → wait_for_ready, NO subprocess calls."""
    run_cmd_calls: list[Any] = []
    http_get_calls: list[str] = []

    def _run_cmd(argv: list[str], cwd: str | None) -> None:
        run_cmd_calls.append((argv, cwd))

    def _http_get(url: str) -> dict[str, Any]:
        http_get_calls.append(url)
        return {"ok": True}

    inst = Instance(
        id="pod-x",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8188": "https://pod-x-8188.proxy.runpod.net"},
    )
    engine = ComfyUIEngine(
        run_cmd=_run_cmd,
        http_get=_http_get,
        sleep=lambda _: None,
        probe_profile=None,  # type: ignore[arg-type]
        get_instance=lambda _: inst,
    )
    cfg = {"lifecycle": {"boot_timeout_s": 30.0}}
    engine.provision(inst, cfg)
    assert run_cmd_calls == []  # remote branch: no subprocess
    assert http_get_calls == ["https://pod-x-8188.proxy.runpod.net/system_stats"]


def test_provision_remote_uses_boot_timeout_from_cfg_lifecycle() -> None:
    """cfg.lifecycle.boot_timeout_s flows through to wait_for_ready's timeout_s."""
    seen_timeout: list[float] = []

    class _SpyEngine(ComfyUIEngine):
        def wait_for_ready(self, instance, *, http_get, sleep, get_instance, timeout_s):
            seen_timeout.append(timeout_s)

    engine = _SpyEngine(probe_profile=None)  # type: ignore[arg-type]
    inst = Instance(
        id="pod-x",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8188": "https://x"},
    )
    engine.provision(inst, {"lifecycle": {"boot_timeout_s": 1234.0}})
    assert seen_timeout == [1234.0]


def test_provision_remote_default_boot_timeout_when_cfg_absent() -> None:
    """No cfg.lifecycle → default 900.0."""
    seen_timeout: list[float] = []

    class _SpyEngine(ComfyUIEngine):
        def wait_for_ready(self, instance, *, http_get, sleep, get_instance, timeout_s):
            seen_timeout.append(timeout_s)

    engine = _SpyEngine(probe_profile=None)  # type: ignore[arg-type]
    inst = Instance(
        id="pod-x",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8188": "https://x"},
    )
    engine.provision(inst, {})
    assert seen_timeout == [900.0]
