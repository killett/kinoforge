"""Tests for DiffusersEngine.provision local-vs-remote branch."""

from __future__ import annotations

from typing import Any

from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import DiffusersEngine


def test_provision_with_none_instance_runs_local_body() -> None:
    calls: list[Any] = []
    engine = DiffusersEngine(
        run_cmd=lambda argv, cwd: calls.append((argv, cwd)),
        probe_profile=None,  # type: ignore[arg-type]
    )
    cfg = {
        "engine": {
            "diffusers": {"pip": ["diffusers"], "server_cmd": ["python", "-m", "x"]}
        }
    }
    engine.provision(None, cfg)
    # local body runs pip + server_cmd
    assert len(calls) == 2


def test_provision_with_local_provider_runs_local_body() -> None:
    calls: list[Any] = []
    engine = DiffusersEngine(
        run_cmd=lambda argv, cwd: calls.append((argv, cwd)),
        probe_profile=None,  # type: ignore[arg-type]
    )
    inst = Instance(id="local-1", provider="local", status="ready", created_at=0.0)
    cfg = {"engine": {"diffusers": {"pip": [], "server_cmd": ["python", "-m", "x"]}}}
    engine.provision(inst, cfg)
    assert len(calls) == 1


def test_provision_with_remote_provider_calls_wait_for_ready_not_local_body() -> None:
    run_cmd_calls: list[Any] = []
    http_get_calls: list[str] = []
    inst = Instance(
        id="pod-d",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod-d-8000.proxy.runpod.net"},
    )

    def _http_get(url: str) -> dict[str, Any]:
        http_get_calls.append(url)
        return {"ok": True}

    engine = DiffusersEngine(
        run_cmd=lambda argv, cwd: run_cmd_calls.append((argv, cwd)),
        http_get=_http_get,
        sleep=lambda _: None,
        get_instance=lambda _: inst,
        probe_profile=None,  # type: ignore[arg-type]
    )
    engine.provision(inst, {"lifecycle": {"boot_timeout": 30.0}})
    assert run_cmd_calls == []
    assert http_get_calls == ["https://pod-d-8000.proxy.runpod.net/health"]
