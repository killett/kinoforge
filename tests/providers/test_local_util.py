"""Unit tests for LocalUtilEndpoint + _adapters.build_util_endpoint_for (C26)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kinoforge.core.errors import AuthError
from kinoforge.core.util_endpoints import UtilSnapshot, UtilSnapshotEndpoint
from kinoforge.providers.local.util import LocalUtilEndpoint


class _FakeCreds:
    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._m = mapping

    def get(self, key: str) -> str | None:
        return self._m.get(key)


def _snap(
    gpu: float | None = 0.0, cpu: float | None = 0.0, uptime: int | None = 100
) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=gpu,
        cpu_percent=cpu,
        memory_percent=None,
        disk_percent=None,
        uptime_seconds=uptime,
    )


def _build_runpod_cfg(
    *,
    stall_reap_enabled: bool,
    restart_loop_reap_enabled: bool = True,
    provider: str = "runpod",
) -> SimpleNamespace:
    lifecycle = SimpleNamespace(
        stall_reap_enabled=stall_reap_enabled,
        restart_loop_reap_enabled=restart_loop_reap_enabled,
    )
    compute = SimpleNamespace(provider=provider, lifecycle=lifecycle)
    return SimpleNamespace(compute=compute)


def _build_hosted_only_cfg() -> SimpleNamespace:
    return SimpleNamespace(compute=None)


def test_local_util_endpoint_implements_protocol() -> None:
    assert isinstance(LocalUtilEndpoint(), UtilSnapshotEndpoint)


def test_local_util_endpoint_returns_scripted_snapshots() -> None:
    snaps = [_snap(gpu=1.0), _snap(gpu=2.0), _snap(gpu=3.0)]
    ep = LocalUtilEndpoint(script=snaps)
    assert ep.read_util("i1") is not None
    assert ep.read_util("i1").gpu_util_percent == 2.0  # type: ignore[union-attr]
    assert ep.read_util("i1").gpu_util_percent == 3.0  # type: ignore[union-attr]


def test_local_util_endpoint_returns_none_when_script_exhausted() -> None:
    ep = LocalUtilEndpoint(script=[_snap()])
    assert ep.read_util("i1") is not None
    assert ep.read_util("i1") is None


def test_build_util_endpoint_for_returns_none_when_compute_is_none() -> None:
    from kinoforge._adapters import build_util_endpoint_for

    cfg = _build_hosted_only_cfg()
    assert build_util_endpoint_for(cfg, _FakeCreds({})) is None  # type: ignore[arg-type]


def test_build_util_endpoint_for_returns_none_when_both_features_disabled() -> None:
    """C27: kill switch trips only when BOTH stall and restart-loop are off."""
    from kinoforge._adapters import build_util_endpoint_for

    cfg = _build_runpod_cfg(stall_reap_enabled=False, restart_loop_reap_enabled=False)
    assert (
        build_util_endpoint_for(cfg, _FakeCreds({"RUNPOD_API_KEY": "k"}))  # type: ignore[arg-type]
        is None
    )


def test_build_util_endpoint_for_builds_endpoint_when_only_stall_disabled() -> None:
    """C27: stall disabled but restart-loop enabled → endpoint still built.

    The util sampler powers BOTH predicates; turning off only one leaves
    the other consumer needing the sampler.
    """
    from kinoforge._adapters import build_util_endpoint_for

    cfg = _build_runpod_cfg(stall_reap_enabled=False, restart_loop_reap_enabled=True)
    ep = build_util_endpoint_for(cfg, _FakeCreds({"RUNPOD_API_KEY": "k"}))  # type: ignore[arg-type]
    assert ep is not None


def test_build_util_endpoint_for_builds_endpoint_when_only_restart_loop_disabled() -> (
    None
):
    """C27: restart-loop disabled but stall enabled → endpoint still built (C26 path)."""
    from kinoforge._adapters import build_util_endpoint_for

    cfg = _build_runpod_cfg(stall_reap_enabled=True, restart_loop_reap_enabled=False)
    ep = build_util_endpoint_for(cfg, _FakeCreds({"RUNPOD_API_KEY": "k"}))  # type: ignore[arg-type]
    assert ep is not None


def test_build_util_endpoint_for_returns_none_for_unsupported_provider() -> None:
    """SkyPilot/Bedrock have no util substrate ⇒ None."""
    from kinoforge._adapters import build_util_endpoint_for

    cfg = _build_runpod_cfg(stall_reap_enabled=True, provider="skypilot")
    assert build_util_endpoint_for(cfg, _FakeCreds({})) is None  # type: ignore[arg-type]


def test_build_util_endpoint_for_runpod_branch() -> None:
    from kinoforge._adapters import build_util_endpoint_for
    from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

    cfg = _build_runpod_cfg(stall_reap_enabled=True)
    ep = build_util_endpoint_for(cfg, _FakeCreds({"RUNPOD_API_KEY": "k"}))  # type: ignore[arg-type]
    assert isinstance(ep, RunPodGraphQLUtilEndpoint)


def test_build_util_endpoint_for_local_branch_returns_local_endpoint() -> None:
    from kinoforge._adapters import build_util_endpoint_for

    cfg = _build_runpod_cfg(stall_reap_enabled=True, provider="local")
    ep = build_util_endpoint_for(cfg, _FakeCreds({}))  # type: ignore[arg-type]
    assert isinstance(ep, LocalUtilEndpoint)


def test_build_util_endpoint_for_runpod_branch_raises_when_missing_key() -> None:
    from kinoforge._adapters import build_util_endpoint_for

    cfg = _build_runpod_cfg(stall_reap_enabled=True)
    with pytest.raises(AuthError, match="RUNPOD_API_KEY"):
        build_util_endpoint_for(cfg, _FakeCreds({}))  # type: ignore[arg-type]
