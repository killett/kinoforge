"""C29 — Cancelled raised mid-boot destroys the pod (operator Ctrl-C path).

Pins the ``except Cancelled`` clause in
``_provision_instance_and_build_backend``:

1. ``cancel_token`` set during ``engine.provision`` → ``Cancelled`` raised
   out of the helper → pod ``destroy_instance`` is called even though
   ``Cancelled`` isn't a ``ProvisionFailed``/``ProvisionTimeout``.
2. The pre-existing ``except (ProvisionFailed, ...)`` clause also destroys
   the pod before re-raising.

C33-m (2026-06-17, ``tests/live/_c33_probe_m_evidence.json``) moved the
``start_heartbeat`` call to AFTER ``_provision_compute_once`` returns. As
a result:
   - During the engine.provision window, no heartbeat is running.
   - Cancelled during engine.provision can only originate from operator
     Ctrl-C; there is no boot-phase reap predicate to fire.
   - The teardown paths for ProvisionFailed / Cancelled no longer touch
     ``hb_loop`` (it's None at that point).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled, ProvisionFailed
from kinoforge.core.interfaces import (
    Instance,
    Lifecycle,
    Offer,
    RenderedProvision,
)


class _FakeLoop:
    """Minimal HeartbeatLoopProtocol stand-in."""

    def __init__(self) -> None:
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


def _fake_provider() -> MagicMock:
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
    provider.create_instance.return_value = Instance(
        id="inst-1",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    provider.get_instance.return_value = Instance(
        id="inst-1",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
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


# ---------------------------------------------------------------------------
# Test 1: Cancelled raised out of engine.provision triggers destroy
# ---------------------------------------------------------------------------


def test_cancelled_during_engine_provision_destroys_pod(
    tmp_path: Path,
) -> None:
    """A Cancelled raised out of engine.provision is caught + the pod is destroyed.

    Bug catch: the pre-C29 except clause only listed ProvisionFailed,
    ProvisionTimeout, CapabilityMismatch, ValidationError. A Cancelled
    raised from operator Ctrl-C would have leaked past — leaving a paid
    pod alive forever.

    C33-m: heartbeat is NOT running during engine.provision (was C29's
    BEFORE-provision ordering, reverted). The start_heartbeat closure must
    NOT have been called when Cancelled raises out of provision.
    """
    engine = _fake_engine()
    provider = _fake_provider()
    loop = _FakeLoop()
    hb_calls: list[str] = []

    def _broken_provision(
        instance: Instance | None, cfg: Any, *, cancel_token: object | None = None
    ) -> None:
        del cancel_token
        raise Cancelled("simulated operator Ctrl-C")

    engine.provision.side_effect = _broken_provision

    def _start_heartbeat(_inst: Instance) -> _FakeLoop:
        hb_calls.append("start_heartbeat")
        return loop

    with pytest.raises(Cancelled):
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
            cancel_token=CancelToken(),
            start_heartbeat=_start_heartbeat,
        )

    # C33-m: start_heartbeat was NEVER invoked — it's now after provision.
    assert hb_calls == [], hb_calls
    assert loop.started is False
    assert loop.stopped is False
    # Pod was destroyed
    provider.destroy_instance.assert_called_once_with("inst-1")


# ---------------------------------------------------------------------------
# Test 2: ProvisionFailed path also stops hb_loop before destroy
# ---------------------------------------------------------------------------


def test_provision_failed_path_destroys_pod_without_starting_heartbeat(
    tmp_path: Path,
) -> None:
    """ProvisionFailed during engine.provision → destroy pod → re-raise.

    C33-m: heartbeat is not started during provision. The start_heartbeat
    closure must NOT have been called by the time ProvisionFailed
    propagates.
    """
    engine = _fake_engine()
    provider = _fake_provider()
    loop = _FakeLoop()
    hb_calls: list[str] = []

    def _broken_provision(
        instance: Instance | None, cfg: Any, *, cancel_token: object | None = None
    ) -> None:
        del cancel_token
        raise ProvisionFailed("simulated boot crash")

    engine.provision.side_effect = _broken_provision

    def _start_heartbeat(_inst: Instance) -> _FakeLoop:
        hb_calls.append("start_heartbeat")
        return loop

    with pytest.raises(ProvisionFailed):
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

    # C33-m: start_heartbeat was NEVER invoked.
    assert hb_calls == [], hb_calls
    assert loop.started is False
    assert loop.stopped is False
    provider.destroy_instance.assert_called_once_with("inst-1")


# ---------------------------------------------------------------------------
# Test 3: idempotent re-destroy under Cancelled (reap path)
# ---------------------------------------------------------------------------


def test_cancelled_re_destroy_swallows_provider_error(tmp_path: Path) -> None:
    """When the heartbeat already destroyed the pod, the helper's idempotent
    re-destroy must NOT crash if provider.destroy_instance raises.

    RunPod's destroy_instance is 404-idempotent on repeat — re-destroy is
    a log-noise event, not a fatal error. The helper swallows + logs so
    the Cancelled keeps propagating to the operator.
    """
    engine = _fake_engine()
    provider = _fake_provider()

    def _broken_destroy(_id: str) -> None:
        raise RuntimeError("simulated 404 on already-destroyed pod")

    provider.destroy_instance.side_effect = _broken_destroy

    def _broken_provision(
        instance: Instance | None, cfg: Any, *, cancel_token: object | None = None
    ) -> None:
        del cancel_token
        raise Cancelled("simulated mid-boot reap")

    engine.provision.side_effect = _broken_provision

    with pytest.raises(Cancelled):
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
            cancel_token=CancelToken(),
        )

    # destroy_instance was attempted even though it raised
    provider.destroy_instance.assert_called_once_with("inst-1")
