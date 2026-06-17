"""C33 — start_heartbeat closure fires AFTER engine.provision returns.

Supersedes the C29 contract (which had the closure fire BEFORE engine.provision
so STALL/RESTART_LOOP predicates could tick throughout the boot phase). The
C33 (m) live probe (2026-06-17, tests/live/_c33_probe_m_evidence.json) proved
that the RunPod GraphQL ``podEditJob`` mutation issued by the C25 B5a
heartbeat satisfier every 30 s during active provision triggers a
container-level restart on the RunPod side, which makes provision INFINITE.

A heartbeat that prevents provision from ever completing cannot help with
stall detection during it. Trade-off accepted: STALL_REAP / RESTART_LOOP_REAP
predicates can no longer fire during provision; they still fire post-boot.

Bug catch: if the closure ever fires before ``_provision_compute_once``
returns, the cycle returns and Wan / ComfyUI cold-boots will loop at ~31 s
cadence (30 s heartbeat interval + 1 s GraphQL roundtrip) until kinoforge
times out, producing no artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from kinoforge.core import orchestrator
from kinoforge.core.interfaces import (
    Instance,
    Lifecycle,
    Offer,
    RenderedProvision,
)


class _FakeLoop:
    """Minimal HeartbeatLoopProtocol stand-in."""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _fake_engine() -> MagicMock:
    engine = MagicMock()
    engine.name = "fakeengine"
    engine.requires_local_weights = False
    engine.render_provision.return_value = RenderedProvision(
        script="echo hi",
        run_cmd=["python", "-m", "x"],
        image="fake:latest",
        ports=["8000"],
        env_required=["HF_TOKEN"],
    )
    return engine


def _fake_provider(initial_statuses: list[str]) -> MagicMock:
    provider = MagicMock()
    provider.name = "fakeprovider"
    provider.find_offers.return_value = [
        Offer(
            id="X1",
            gpu_type="X1",
            vram_gb=24,
            cuda="12.8",
            cost_rate_usd_per_hr=0.30,
        )
    ]
    final_status = "ready"
    provider.create_instance.return_value = Instance(
        id="inst-1",
        provider="fakeprovider",
        status=initial_statuses[0] if initial_statuses else final_status,
        created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    statuses = list(initial_statuses[1:]) + [final_status]
    seq_iter = iter(statuses)

    def _get_instance(_id: str) -> Instance:
        try:
            status = next(seq_iter)
        except StopIteration:
            status = final_status
        return Instance(
            id="inst-1",
            provider="fakeprovider",
            status=status,
            created_at=0.0,
            endpoints={"8000": "https://inst-1-8000"},
        )

    provider.get_instance.side_effect = _get_instance
    return provider


def _fake_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.lifecycle.return_value = Lifecycle(boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="should-be-overridden")
    cfg.models = []
    cfg.model_dump.return_value = {"engine": {}, "models": []}
    return cfg


def _fake_creds() -> MagicMock:
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    return creds


def _fake_key() -> MagicMock:
    key = MagicMock()
    key.derive.return_value = "deadbeef"
    return key


def test_start_heartbeat_closure_invoked_after_engine_provision(
    tmp_path: Path,
) -> None:
    """C33 contract: closure fires AFTER engine.provision returns.

    Bug catch: if the closure fires before engine.provision, a RunPod
    podEditJob heartbeat mutation runs while bash is mid-provision, the
    container restarts (server-side), bash dies (rc=0 in trap because $? is
    the last completed command, not bash's actual SIGTERM exit), the restart
    loop never terminates, and no artifact is ever produced.

    Pinned in src/kinoforge/core/orchestrator.py:_provision_instance_and_build_backend
    by reading the call order through fake hooks.
    """
    call_order: list[str] = []

    engine = _fake_engine()

    def _engine_provision(
        instance: Instance | None, cfg: Any, *, cancel_token: object | None = None
    ) -> None:
        del cancel_token
        call_order.append("engine.provision")

    engine.provision.side_effect = _engine_provision
    provider = _fake_provider(["creating", "creating", "ready"])

    created_loop = _FakeLoop("inst-1")

    def _start_heartbeat(inst: Instance) -> _FakeLoop:
        call_order.append(f"start_heartbeat({inst.id},status={inst.status})")
        created_loop.instance_id = inst.id
        created_loop.start()
        return created_loop

    result = orchestrator._provision_instance_and_build_backend(
        resolved_engine=engine,
        resolved_provider=provider,
        cfg=_fake_cfg(),
        run_id="run-1",
        key=_fake_key(),
        creds=_fake_creds(),
        store=MagicMock(),
        state_dir=tmp_path,
        for_discovery=False,
        start_heartbeat=_start_heartbeat,
    )

    # Closure ran exactly once.
    starts = [c for c in call_order if c.startswith("start_heartbeat")]
    assert len(starts) == 1, call_order
    # C33 contract: closure ran AFTER engine.provision (was BEFORE under C29).
    assert call_order.index(starts[0]) > call_order.index("engine.provision"), (
        f"C33 contract violated — heartbeat fired before provision: {call_order}"
    )
    # Instance passed in still had status=ready (post-poll).
    assert starts[0] == "start_heartbeat(inst-1,status=ready)"
    # Returned hb_loop is the one the closure created.
    assert result.hb_loop is created_loop
    assert created_loop.started is True


def test_start_heartbeat_not_invoked_when_engine_provision_raises(
    tmp_path: Path,
) -> None:
    """If engine.provision raises before completing, heartbeat MUST NOT start.

    Bug catch: starting heartbeat during the destroy-after-provision-failure
    cleanup would issue podEditJob against a being-destroyed pod, surfacing
    a spurious GraphQL error instead of the underlying provision failure.
    """
    from kinoforge.core.errors import ProvisionFailed

    call_order: list[str] = []

    engine = _fake_engine()

    def _engine_provision(
        instance: Instance | None, cfg: Any, *, cancel_token: object | None = None
    ) -> None:
        del cancel_token
        call_order.append("engine.provision")
        raise ProvisionFailed("simulated boot script crash")

    engine.provision.side_effect = _engine_provision
    provider = _fake_provider(["ready"])

    def _start_heartbeat(inst: Instance) -> _FakeLoop:
        call_order.append(f"start_heartbeat({inst.id})")
        return _FakeLoop(inst.id)

    try:
        orchestrator._provision_instance_and_build_backend(
            resolved_engine=engine,
            resolved_provider=provider,
            cfg=_fake_cfg(),
            run_id="run-1",
            key=_fake_key(),
            creds=_fake_creds(),
            store=MagicMock(),
            state_dir=tmp_path,
            for_discovery=False,
            start_heartbeat=_start_heartbeat,
        )
    except ProvisionFailed:
        pass

    assert "engine.provision" in call_order
    # No heartbeat call at all when provision raises.
    assert not any(c.startswith("start_heartbeat") for c in call_order), call_order
    # Provider.destroy_instance was called on cleanup.
    provider.destroy_instance.assert_called_once_with("inst-1")
