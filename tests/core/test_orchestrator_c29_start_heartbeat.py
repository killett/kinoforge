"""C29 — start_heartbeat closure fires after RunPod status-ready, before engine.provision.

These tests pin the new ``_provision_instance_and_build_backend`` contract:

1. Returns a :class:`ProvisionResult` NamedTuple with
   ``(instance, backend, hb_loop)``.
2. When a ``start_heartbeat`` closure is supplied, invokes it exactly once with
   the polled-ready instance, BEFORE ``engine.provision`` runs.
3. When the closure is ``None``, ``hb_loop`` in the result is ``None`` and no
   heartbeat artefacts are created.
4. Closure failures fall through to ``hb_loop=None`` and log; the helper does
   NOT crash boot for an HB construction error (the late-start path in
   ``deploy_session`` handles the caller-supplied recovery instead).
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
    """Minimal HeartbeatLoopProtocol stand-in for tests."""

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
    # provisioner gates downloads on this flag — False keeps the downloader inert.
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
    """Provider whose get_instance walks the supplied status sequence.

    The orchestrator polls until status == "ready"; ``initial_statuses`` lets
    us simulate a cold-boot pod that flips from "creating" → "ready" mid-wait.
    """
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
    # provisioner iterates cfg.models — a real empty list keeps it inert.
    cfg.models = []
    # requires_local_weights=False so downloader is not called.
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


# ---------------------------------------------------------------------------
# Test 1: NamedTuple shape
# ---------------------------------------------------------------------------


def test_provision_result_namedtuple_shape() -> None:
    """ProvisionResult is a NamedTuple with three fields and is unpackable."""
    inst = MagicMock(spec=Instance)
    backend = MagicMock()
    loop = _FakeLoop("inst-1")
    result = orchestrator.ProvisionResult(instance=inst, backend=backend, hb_loop=loop)

    # Field access
    assert result.instance is inst
    assert result.backend is backend
    assert result.hb_loop is loop

    # Positional unpacking
    a, b, c = result
    assert (a, b, c) == (inst, backend, loop)

    # hb_loop None is also a valid value
    result_no_hb = orchestrator.ProvisionResult(
        instance=inst, backend=backend, hb_loop=None
    )
    assert result_no_hb.hb_loop is None


# ---------------------------------------------------------------------------
# Test 2: helper returns ProvisionResult
# ---------------------------------------------------------------------------


def test_provision_instance_and_build_backend_returns_provision_result(
    tmp_path: Path,
) -> None:
    """The helper's return value is a :class:`ProvisionResult`."""
    engine = _fake_engine()
    provider = _fake_provider(["ready"])
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
    )
    assert isinstance(result, orchestrator.ProvisionResult)
    assert result.hb_loop is None  # no closure supplied


# ---------------------------------------------------------------------------
# Test 3: closure invoked after status=ready, BEFORE engine.provision
# ---------------------------------------------------------------------------


def test_start_heartbeat_closure_invoked_after_status_ready_before_engine_provision(
    tmp_path: Path,
) -> None:
    """Closure fires AFTER the RunPod status-poll loop returns ready, and
    BEFORE engine.provision runs.

    Bug catch: if the closure fires before status flips to ready, the
    heartbeat ticks against a half-booted pod; if it fires after
    engine.provision returns, the boot-phase reap window stays unprotected.
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

    # Closure ran exactly once
    starts = [c for c in call_order if c.startswith("start_heartbeat")]
    assert len(starts) == 1, call_order
    # Closure ran BEFORE engine.provision
    assert call_order.index(starts[0]) < call_order.index("engine.provision"), (
        call_order
    )
    # Instance passed in was status=ready (post-poll)
    assert starts[0] == "start_heartbeat(inst-1,status=ready)"
    # Returned hb_loop is the one the closure created
    assert result.hb_loop is created_loop
    assert created_loop.started is True


# ---------------------------------------------------------------------------
# Test 4: closure None → hb_loop is None
# ---------------------------------------------------------------------------


def test_start_heartbeat_none_yields_hb_loop_none(tmp_path: Path) -> None:
    """When the caller passes no closure, the helper returns hb_loop=None."""
    engine = _fake_engine()
    provider = _fake_provider(["ready"])
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
        start_heartbeat=None,
    )
    assert result.hb_loop is None


# ---------------------------------------------------------------------------
# Test 5: closure failure falls through to hb_loop=None (logged, no crash)
# ---------------------------------------------------------------------------


def test_start_heartbeat_closure_failure_falls_through_to_none(
    tmp_path: Path,
) -> None:
    """A raise inside the closure does NOT crash the helper.

    Bug catch: a bug in the heartbeat ledger or util-endpoint adapter would
    have killed every cold-boot before C29 unless the closure failure is
    swallowed. The late-start path in deploy_session handles the recovery
    pattern for caller-supplied warm pods.
    """
    engine = _fake_engine()
    provider = _fake_provider(["ready"])

    def _broken_closure(inst: Instance) -> _FakeLoop:
        raise RuntimeError("closure-deliberately-broken")

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
        start_heartbeat=_broken_closure,
    )
    assert result.hb_loop is None
    # engine.provision still ran — the broken closure does NOT block boot
    engine.provision.assert_called_once()
