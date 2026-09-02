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
    Instance,
    InstanceSpec,
    Launch,
    ModelProfile,
    Offer,
    Placement,
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
        minimal_skypilot_cfg, launch=None, logger=logging.getLogger("t")
    )
    assert all(g.field != "compute.lifecycle.idle_timeout" for g in gaps)


def test_server_spec_still_reports_the_idle_gap(minimal_skypilot_cfg: Config) -> None:
    gaps = assert_launch_capabilities(
        minimal_skypilot_cfg,
        launch=Launch(("python", "-m", "server")),
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
    SERVER (Task 4), and ``launch=None`` here means the authoritative shape
    is BATCH, so the rendered message must name SERVER as the load-time
    guess and BATCH as what ``spec.launch`` says — in that order.
    """
    with caplog.at_level(logging.WARNING):
        assert_launch_capabilities(
            minimal_skypilot_cfg, launch=None, logger=logging.getLogger("kinoforge")
        )
    messages = [r.getMessage() for r in caplog.records]
    matches = [m for m in messages if "shape inference miss" in m]
    assert len(matches) == 1
    msg = matches[0]
    assert "load-time inferred server" in msg
    assert "spec.launch says batch" in msg


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
            launch=Launch(("python", "-m", "server")),
            logger=logging.getLogger("t"),
        )


# ---------------------------------------------------------------------------
# Integration coverage: the ERROR-gap-raises-before-create guarantee is an
# acceptance criterion about the real provisioning wiring, not about
# assert_launch_capabilities in isolation. A unit test calling it directly
# (above) cannot tell whether the check is reached on the real path at all,
# nor whether it aborts ahead of offer discovery and instance creation —
# only driving that path with a call-recording provider can.
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
        self.find_offers_calls = 0

    def find_offers(self, reqs: Placement) -> list[Offer]:
        """Return one fixed offer regardless of ``reqs``, counting the call.

        Args:
            reqs: Ignored; the caller's Placement.

        Returns:
            A single-element offer list, enough to drive one retry iteration.
        """
        del reqs
        self.find_offers_calls += 1
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
    """Drives the real ``_provision_instance_and_build_backend`` and proves an
    ERROR gap aborts before ANY provider call — not just that
    ``assert_launch_capabilities`` raises when called in isolation.

    What this pins, precisely: the launch-time re-check is reached on the
    real provisioning path (after ``render_provision``, since it needs the
    authoritative ``rendered.run_cmd``) and it raises *ahead of* offer
    discovery and instance creation.

    Bugs this catches:

    * The re-check being dropped from, or moved below, the provisioning path
      — ``pytest.raises`` fails outright.
    * The re-check being demoted to a log line, or re-ordered below
      ``_find_offers`` / ``_create_with_capacity_wait``: ``find_offers_calls
      == 0`` and ``create_calls == []`` are the assertions that fail, and a
      bare ``pytest.raises`` on its own would not.

    Deliberately NOT claimed: this does not catch a widening of
    ``except CapacityError``. The re-check now runs above the retry loops
    entirely (and even before the hoist, ``_build_spec`` was invoked outside
    the guarded ``try``), so a broadened ``except`` never sees this raise.
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
    assert provider.find_offers_calls == 0


class _FlakyProvider(LocalProvider):
    """Two offers; the create aborts immediately.

    compute-seam S4 moved offer-retry into the provider, so the orchestrator
    calls create_instance exactly once. The abort keeps the run out of
    wait_for_ready / engine.provision, which is all this module needs — the
    claim under test is how many times the capability check runs BEFORE the
    create, and the two offers still exist so a per-offer regression would
    show up as two check calls.
    """

    def __init__(self) -> None:
        """Initialise with an empty call log."""
        super().__init__()
        self.create_calls: list[InstanceSpec] = []

    def find_offers(self, reqs: Placement) -> list[Offer]:
        """Return two offers so the retry loop has somewhere to go.

        Args:
            reqs: Ignored; the caller's Placement.

        Returns:
            Two offers differing only in id.
        """
        del reqs
        return [
            Offer(
                id=f"offer-{i}",
                gpu_type="GPU_0",
                vram_gb=24,
                cuda="12.0",
                cost_rate_usd_per_hr=0.10,
                mode="pod",
            )
            for i in range(2)
        ]

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Abort the run before any downstream provisioning.

        Args:
            spec: The InstanceSpec built for the chosen offer.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always, to end the run without exercising the whole
                downstream provisioning path.
        """
        self.create_calls.append(spec)
        raise RuntimeError("stop here")


def test_launch_capability_check_runs_once_not_once_per_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-check depends only on (cfg, rendered.run_cmd) — so it runs once.

    Bug caught: the check sitting inside ``_build_spec``, which is called per
    offer AND again on every capacity-wait retry. Its verdict cannot change
    between those calls, so every extra call re-emitted the same WARN lines,
    which an operator reads as several distinct guardrail problems. On this
    cfg skypilot earns real WARNs (idle_timeout / job_timeout /
    heartbeat_interval_s are covered by ON_INSTANCE_DEADLINE, not enforced),
    so the duplication was operator-visible, not theoretical.
    """
    from kinoforge.core import orchestrator as orch

    cfg = load_config(_INTEGRATION_CFG_YAML)
    real = orch.assert_launch_capabilities
    calls: list[object] = []

    def _counting(cfg_arg: Config, **kwargs: object) -> object:
        calls.append(kwargs.get("run_cmd"))
        return real(cfg_arg, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(orch, "assert_launch_capabilities", _counting)

    provider = _FlakyProvider()
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

    with pytest.raises(RuntimeError, match="stop here"):
        _provision_instance_and_build_backend(
            resolved_engine=engine,
            resolved_provider=provider,
            cfg=cfg,
            run_id="t",
            key=cfg.capability_key(),
            creds=None,
            store=LocalArtifactStore(tmp_path),
            state_dir=tmp_path,
            for_discovery=False,
        )

    # S4: the orchestrator hands the provider one spec and does not iterate ...
    assert len(provider.create_calls) == 1
    # ... and the capability re-check ran exactly once, hoisted above the
    # capacity-wait loop rather than sitting inside _build_spec.
    assert len(calls) == 1
