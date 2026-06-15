"""C29 — Cancelled raised mid-boot destroys the pod (reap or Ctrl-C path).

Pins the new ``except Cancelled`` clause in
``_provision_instance_and_build_backend``:

1. ``cancel_token`` set during ``engine.provision`` → ``Cancelled`` raised
   out of the helper → pod ``destroy_instance`` is called even though
   ``Cancelled`` isn't a ``ProvisionFailed``/``ProvisionTimeout``.
2. The pre-existing ``except (ProvisionFailed, ...)`` clause also stops the
   boot-phase ``hb_loop`` before destroying the pod.
3. The boot-phase reap path (``hb_loop`` destroys the pod, then sets the
   token) re-destroys idempotently — RunPod's repeated-destroy is a logged
   no-op, so the second ``destroy_instance`` does not crash the helper.
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


def test_cancelled_during_engine_provision_destroys_pod_and_stops_hb_loop(
    tmp_path: Path,
) -> None:
    """A Cancelled raised out of engine.provision is caught + the pod is destroyed.

    Bug catch: the pre-C29 except clause only listed ProvisionFailed,
    ProvisionTimeout, CapabilityMismatch, ValidationError. A Cancelled
    raised from a boot-phase reap (or operator Ctrl-C) would have leaked
    past — leaving a paid pod alive forever.
    """
    engine = _fake_engine()
    provider = _fake_provider()
    loop = _FakeLoop()

    def _broken_provision(
        instance: Instance | None, cfg: Any, *, cancel_token: object | None = None
    ) -> None:
        del cancel_token
        raise Cancelled("simulated boot-phase reap")

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
            start_heartbeat=lambda _inst: loop,
        )

    # hb_loop was stopped before destroy
    assert loop.stopped is True
    # Pod was destroyed
    provider.destroy_instance.assert_called_once_with("inst-1")


# ---------------------------------------------------------------------------
# Test 2: ProvisionFailed path also stops hb_loop before destroy
# ---------------------------------------------------------------------------


def test_provision_failed_path_stops_hb_loop_before_destroy(tmp_path: Path) -> None:
    """ProvisionFailed during engine.provision → stop loop → destroy pod → re-raise."""
    engine = _fake_engine()
    provider = _fake_provider()
    loop = _FakeLoop()

    def _broken_provision(
        instance: Instance | None, cfg: Any, *, cancel_token: object | None = None
    ) -> None:
        del cancel_token
        raise ProvisionFailed("simulated boot crash")

    engine.provision.side_effect = _broken_provision

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
            start_heartbeat=lambda _inst: loop,
        )

    assert loop.stopped is True
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
