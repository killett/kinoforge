"""Tests that ``deploy()`` preserves create-time Instance fields across the
get_instance polling loop.

Regression context (Stage E live smoke 2026-06-18):
    First real SkyPilot ledger row landed as
    ``skypilot-cluster age=494954.6h est_spend=$0.0000 capability_key=<unknown>``.
    Root cause: orchestrator.deploy()'s polling loop reassigns ``instance =
    provider.get_instance(...)`` until status=="ready". The SkyPilotProvider's
    ``_cluster_record_to_instance`` returns an impoverished Instance with
    ``created_at=0.0``, empty ``tags``, and ``cost_rate_usd_per_hr=0.0``
    because ``sky.status()`` does not expose creation time, tags, or rate.
    The clobber surfaces as the three-bug Stage-E symptom set when
    ``Ledger.record(instance)`` later reads those fields.

These tests fence the fix: rich fields from ``create_instance()`` must
survive the polling loop AND polling must continue to drive status to
``"ready"`` before deploy() returns.
"""

from __future__ import annotations

import time

import kinoforge.engines.fake  # noqa: F401  — self-register
import kinoforge.providers.local  # noqa: F401  — self-register
import kinoforge.sources.http  # noqa: F401  — self-register https:// source
from kinoforge.core.config import Config, load_config
from kinoforge.core.interfaces import Instance, InstanceSpec, ModelProfile
from kinoforge.core.orchestrator import deploy
from kinoforge.engines.fake import FakeEngine
from kinoforge.providers.local import LocalProvider

_COMPUTE_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
"""


def _cfg() -> Config:
    return load_config(_COMPUTE_YAML)


def _engine() -> FakeEngine:
    return FakeEngine(
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


class _SkyShapeProvider(LocalProvider):
    """LocalProvider subclass that reproduces the SkyPilotProvider polling shape.

    ``create_instance`` returns a *rich* Instance (status=starting, real
    created_at, populated tags, non-zero cost rate). ``get_instance`` returns
    an *impoverished* Instance (status=ready, created_at=0.0, no tags,
    cost_rate=0.0) — matching what ``_cluster_record_to_instance`` produces
    against ``sky.status()`` output.
    """

    _POD_ID = "fence-deploy-poll-instance"
    _RICH_RATE = 1.29
    _RICH_TAGS = {
        "kinoforge_engine": "fake",
        "kinoforge_key": "abcdef012345",
    }

    def __init__(self) -> None:
        super().__init__()
        self.get_instance_calls = 0
        self.create_at: float = 0.0  # populated by create_instance

    def create_instance(self, spec: InstanceSpec) -> Instance:
        self.create_at = time.time()
        return Instance(
            id=self._POD_ID,
            provider="local",
            status="starting",  # forces orchestrator into the polling loop
            created_at=self.create_at,
            tags=dict(self._RICH_TAGS),
            cost_rate_usd_per_hr=self._RICH_RATE,
        )

    def get_instance(self, instance_id: str) -> Instance:
        self.get_instance_calls += 1
        return Instance(
            id=self._POD_ID,
            provider="local",
            status="ready",
            created_at=0.0,
            tags={},
            cost_rate_usd_per_hr=0.0,
        )

    def destroy_instance(self, instance_id: str) -> None:
        # No-op — the fence test does not exercise teardown semantics.
        pass


def test_deploy_preserves_create_instance_fields_when_get_instance_drops_them() -> None:
    """deploy() must NOT let the polling loop clobber created_at/tags/cost_rate."""
    provider = _SkyShapeProvider()

    t_before = time.time()
    result = deploy(_cfg(), provider=provider, engine=_engine())
    t_after = time.time()

    assert result.instance is not None, "compute deploy must return an Instance"
    inst = result.instance

    assert provider.get_instance_calls >= 1, (
        "fence guard: provider.get_instance must have been polled at least once "
        "(otherwise test 2's status assertion is moot)"
    )

    assert inst.created_at == provider.create_at, (
        f"created_at clobbered by polling loop: expected create-time "
        f"{provider.create_at!r}, got {inst.created_at!r}. Ledger.record() "
        f"would persist this as ~56-year age."
    )
    assert t_before <= inst.created_at <= t_after, (
        f"created_at outside the bounded window: {t_before!r} <= "
        f"{inst.created_at!r} <= {t_after!r}"
    )
    assert inst.tags == _SkyShapeProvider._RICH_TAGS, (
        f"tags clobbered by polling loop: expected "
        f"{_SkyShapeProvider._RICH_TAGS!r}, got {inst.tags!r}. Ledger would "
        f"render this as capability_key=<unknown>."
    )
    assert inst.cost_rate_usd_per_hr == _SkyShapeProvider._RICH_RATE, (
        f"cost_rate clobbered by polling loop: expected "
        f"{_SkyShapeProvider._RICH_RATE!r}, got {inst.cost_rate_usd_per_hr!r}. "
        f"Ledger would render this as est_spend=$0.0000."
    )


def test_deploy_polls_get_instance_until_status_ready() -> None:
    """Fix MUST still poll for status transition — a no-poll fix would leave
    DeployResult.instance.status == 'starting' and break every downstream
    consumer that expects ready."""
    provider = _SkyShapeProvider()

    result = deploy(_cfg(), provider=provider, engine=_engine())

    assert result.instance is not None
    assert result.instance.status == "ready", (
        f"deploy() returned before polling drove status to 'ready'; got "
        f"status={result.instance.status!r}"
    )
    assert provider.get_instance_calls >= 1, (
        "polling loop never ran; create_instance returned status=starting and "
        "deploy() must call get_instance at least once to drive status to ready"
    )
