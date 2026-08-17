# tests/core/test_capability_shape_launch.py
"""Launch-time shape re-check (Brief 2, Task 5)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

# Import providers/engines so they self-register with kinoforge.core.registry.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
from kinoforge.core.capabilities import WorkloadShape
from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    HardwareRequirements,
    Instance,
    InstanceSpec,
    ModelProfile,
    Offer,
)
from kinoforge.core.orchestrator import (
    _provision_instance_and_build_backend,
    assert_launch_capabilities,
)
from kinoforge.engines.fake import FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.skypilot import SkyPilotProvider
from kinoforge.stores.local import LocalArtifactStore


def test_shape_comes_from_the_spec_not_the_cfg(minimal_skypilot_cfg: Config) -> None:
    """Catches a re-check that re-reads cfg and therefore can never catch an
    inference miss: cfg infers SERVER, the spec says batch."""
    gaps = assert_launch_capabilities(
        minimal_skypilot_cfg, run_cmd=[], logger=logging.getLogger("t")
    )
    assert all(g.field != "compute.lifecycle.idle_timeout" for g in gaps)


def test_server_spec_still_reports_the_idle_gap(minimal_skypilot_cfg: Config) -> None:
    gaps = assert_launch_capabilities(
        minimal_skypilot_cfg,
        run_cmd=["python", "-m", "server"],
        logger=logging.getLogger("t"),
    )
    assert any(g.field == "compute.lifecycle.idle_timeout" for g in gaps)


def test_inference_miss_is_logged_not_swallowed(
    minimal_skypilot_cfg: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catches a silent correction — the derivation must get fixed, not drift.

    Also catches the message dropping its ``%s, %s`` interpolation, or
    swapping the argument order so it reports "inferred batch, spec says
    server" when the truth is the reverse: ``infer_shape`` always guesses
    SERVER (Task 4), and ``run_cmd=[]`` here means the authoritative shape
    is BATCH, so the rendered message must name SERVER as the load-time
    guess and BATCH as what ``spec.run_cmd`` says — in that order.
    """
    with caplog.at_level(logging.WARNING):
        assert_launch_capabilities(
            minimal_skypilot_cfg, run_cmd=[], logger=logging.getLogger("kinoforge")
        )
    messages = [r.getMessage() for r in caplog.records]
    matches = [m for m in messages if "shape inference miss" in m]
    assert len(matches) == 1
    msg = matches[0]
    assert "load-time inferred server" in msg
    assert "spec.run_cmd says batch" in msg


def test_error_gap_raises_before_create(
    minimal_skypilot_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an ERROR gap being logged instead of aborting the launch."""
    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    with pytest.raises(ValidationError, match="ON_INSTANCE_DEADLINE"):
        assert_launch_capabilities(
            minimal_skypilot_cfg,
            run_cmd=["python", "-m", "server"],
            logger=logging.getLogger("t"),
        )


# ---------------------------------------------------------------------------
# Integration coverage: the ERROR-gap-raises-before-create guarantee is an
# acceptance criterion about the real _build_spec -> _create_with_offer_retry
# wiring, not about assert_launch_capabilities in isolation. A unit test
# calling assert_launch_capabilities directly (above) cannot catch a future
# widening of `except CapacityError` in `_create_with_offer_retry` or
# `_create_with_capacity_wait` to also swallow ValidationError — only driving
# the real provisioning path with a call-recording provider can.
# ---------------------------------------------------------------------------

_INTEGRATION_CFG_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: skypilot
  image: fake:latest
  mode: pod
  lifecycle:
    max_lifetime: 10800
    budget: 1.0
"""


class _RecordingProvider(LocalProvider):
    """LocalProvider whose create_instance records every call it receives.

    Used to prove a launch-time ERROR gap aborts before any provider call —
    an empty ``create_calls`` list after the raise is the assertion that
    actually distinguishes "aborted before create" from "create ran and
    something else raised".
    """

    def __init__(self) -> None:
        """Initialise with an empty call log."""
        super().__init__()
        self.create_calls: list[InstanceSpec] = []

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        """Return one fixed offer regardless of ``reqs``.

        Args:
            reqs: Ignored; the caller's HardwareRequirements.

        Returns:
            A single-element offer list, enough to drive one retry iteration.
        """
        del reqs
        return [
            Offer(
                id="offer-0",
                gpu_type="GPU_0",
                vram_gb=24,
                cuda="12.0",
                cost_rate_usd_per_hr=0.10,
                mode="pod",
            )
        ]

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Record ``spec`` then delegate to LocalProvider's real creation.

        Args:
            spec: The InstanceSpec _build_spec constructed for the offer.

        Returns:
            The Instance LocalProvider.create_instance would normally return.
        """
        self.create_calls.append(spec)
        return super().create_instance(spec)


def test_error_gap_raises_before_create_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the real _provision_instance_and_build_backend -> _build_spec ->
    _create_with_offer_retry path and proves an ERROR gap aborts before
    create_instance is ever called — not just that assert_launch_capabilities
    raises in isolation.

    Bug this catches: a future change that widens
    `except CapacityError` in `_create_with_offer_retry` (or the
    `except CapacityError` in `_create_with_capacity_wait`) to also catch
    `ValidationError` would silently retry past the guardrail-gap abort and
    eventually call create_instance anyway. `create_calls == []` is the
    assertion that would fail if that regression landed; a bare
    `pytest.raises` on its own would still pass.
    """
    # Load with SkyPilotProvider's real declaration in effect (ON_INSTANCE_DEADLINE
    # covers max_lifetime), so load-time validation passes; only THEN monkeypatch
    # capabilities to frozenset() so the launch-time re-check is what raises.
    cfg = load_config(_INTEGRATION_CFG_YAML)
    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    provider = _RecordingProvider()
    store = LocalArtifactStore(tmp_path)
    engine = FakeEngine(
        probe_profile=ModelProfile(
            name="fake",
            max_frames=16,
            fps=8,
            supported_modes={"t2v"},
            max_resolution=(512, 512),
            supports_native_extension=False,
            supports_joint_audio=False,
        ),
        declared_flags_map={},
        required_spec_keys=set(),
    )

    with pytest.raises(ValidationError, match="ON_INSTANCE_DEADLINE"):
        _provision_instance_and_build_backend(
            resolved_engine=engine,
            resolved_provider=provider,
            cfg=cfg,
            run_id="t",
            key=cfg.capability_key(),
            creds=None,
            store=store,
            state_dir=tmp_path,
            for_discovery=False,
        )

    assert provider.create_calls == []
