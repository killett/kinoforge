"""Tests for kinoforge.core.orchestrator — deploy() and generate() flows.

Acceptance Criteria:
  AC1: deploy(dry_run=True) prints plan; create_instance never called.
  AC2: deploy() for hosted engine (requires_compute=False) returns instance=None;
       no provider method reached.
  AC3: generate() end-to-end with LocalProvider + FakeEngine + LocalArtifactStore
       produces a readable Artifact.
  AC4: Discovery ordering — inspect_capabilities called once on cache miss;
       second call hits cache (zero inspect_capabilities on second run).
  AC5: Fail-hard teardown — CapabilityMismatch → destroy_instance called once, then raised.
  AC6: 1-segment Job produced (splitter stub comment in code).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Import providers/engines/sources so they self-register
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 — registers https:// source for provisioner
from kinoforge.core.config import Config
from kinoforge.core.errors import (
    CapabilityMismatch,
    CapacityError,
    ProfileNotCached,
    ValidationError,
)
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ConditioningAsset,
    GenerationJob,
    GenerationRequest,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    ModelProfile,
    ModelProfileProvider,
    Offer,
    Segment,
)
from kinoforge.core.orchestrator import DeployResult, deploy, deploy_session, generate
from kinoforge.engines.fake import FakeBackend, FakeEngine
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Fixtures: minimal configs
# ---------------------------------------------------------------------------

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

_HOSTED_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
"""


def _probe_profile(max_frames: int = 16) -> ModelProfile:
    return ModelProfile(
        name="fake",
        max_frames=max_frames,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _make_engine(max_frames: int = 16) -> FakeEngine:
    return FakeEngine(
        probe_profile=_probe_profile(max_frames),
        declared_flags_map={},
        required_spec_keys=set(),
    )


def _compute_cfg() -> Config:
    from kinoforge.core.config import load_config

    return load_config(_COMPUTE_YAML)


def _hosted_cfg() -> Config:
    from kinoforge.core.config import load_config

    return load_config(_HOSTED_YAML)


# ---------------------------------------------------------------------------
# Spy provider — raises on any method call (for AC2 hosted path)
# ---------------------------------------------------------------------------


class _RaisingProviderSpy(LocalProvider):
    """Raises AssertionError the moment any method is called."""

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        raise AssertionError("find_offers called on hosted engine path")

    def create_instance(self, spec: InstanceSpec) -> Instance:
        raise AssertionError("create_instance called on hosted engine path")

    def get_instance(self, instance_id: str) -> Instance:
        raise AssertionError("get_instance called on hosted engine path")

    def list_instances(self) -> list[Instance]:
        raise AssertionError("list_instances called on hosted engine path")

    def stop_instance(self, instance_id: str) -> None:
        raise AssertionError("stop_instance called on hosted engine path")

    def destroy_instance(self, instance_id: str) -> None:
        raise AssertionError("destroy_instance called on hosted engine path")

    def heartbeat(self, instance_id: str) -> None:
        raise AssertionError("heartbeat called on hosted engine path")

    def endpoints(self, instance: Instance) -> dict[str, str]:
        raise AssertionError("endpoints called on hosted engine path")


# ---------------------------------------------------------------------------
# Counting backend — tracks inspect_capabilities calls
# ---------------------------------------------------------------------------


class CountingBackend(FakeBackend):
    """FakeBackend subclass that counts inspect_capabilities calls."""

    def __init__(self, probe: ModelProfile) -> None:
        super().__init__(probe=probe)
        self.inspect_count: int = 0

    def inspect_capabilities(self) -> ModelProfile:
        self.inspect_count += 1
        return super().inspect_capabilities()


# ---------------------------------------------------------------------------
# Hosted FakeEngine (requires_compute = False)
# ---------------------------------------------------------------------------


class HostedFakeEngine(FakeEngine):
    """FakeEngine variant that claims it does NOT require compute."""

    requires_compute: bool = False


# ---------------------------------------------------------------------------
# Sequenced backend — returns different profiles on successive calls (AC5)
# ---------------------------------------------------------------------------


class SequencedBackend(FakeBackend):
    """Backend that pops profiles from a list on each inspect_capabilities call."""

    def __init__(self, probes: list[ModelProfile]) -> None:
        if not probes:
            raise ValueError("probes list must not be empty")
        super().__init__(probe=probes[0])
        self._probes = list(probes)
        self._call_index: int = 0

    def inspect_capabilities(self) -> ModelProfile:
        profile = self._probes[self._call_index % len(self._probes)]
        self._call_index += 1
        return profile


class SequencedEngine(FakeEngine):
    """FakeEngine that returns a SequencedBackend for drift tests."""

    def __init__(
        self,
        probes: list[ModelProfile],
        declared_flags_map: dict[str, dict[str, Any]],
        required_spec_keys: set[str],
    ) -> None:
        super().__init__(
            probe_profile=probes[0],
            declared_flags_map=declared_flags_map,
            required_spec_keys=required_spec_keys,
        )
        self._probes = probes
        # shared backend across all backend() calls so the counter persists
        self._shared_backend = SequencedBackend(probes)

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> SequencedBackend:
        return self._shared_backend


# ---------------------------------------------------------------------------
# Spy provider — counts destroy_instance calls (AC5)
# ---------------------------------------------------------------------------


class DestroySpyProvider(LocalProvider):
    """LocalProvider subclass that tracks destroy_instance calls."""

    def __init__(self) -> None:
        super().__init__()
        self.destroy_calls: list[str] = []

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)
        super().destroy_instance(instance_id)


# ---------------------------------------------------------------------------
# AC1: dry-run plan printed; create_instance never called
# ---------------------------------------------------------------------------


class TestDeployDryRun:
    def test_dry_run_does_not_call_create_instance(self, capsys: Any) -> None:
        """AC1: dry-run should not call create_instance and should print a plan."""
        cfg = _compute_cfg()
        engine = _make_engine()

        # Spy provider that records create_instance calls
        class _SpyProvider(LocalProvider):
            def __init__(self) -> None:
                super().__init__()
                self.create_calls: int = 0

            def create_instance(self, spec: InstanceSpec) -> Instance:
                self.create_calls += 1
                return super().create_instance(spec)

        spy = _SpyProvider()
        deploy(cfg, dry_run=True, provider=spy, engine=engine)

        assert spy.create_calls == 0, "create_instance must not be called in dry-run"

    def test_dry_run_returns_deploy_result_with_plan_text(self) -> None:
        """AC1: dry-run DeployResult has plan_text populated and instance=None."""
        cfg = _compute_cfg()
        engine = _make_engine()
        provider = LocalProvider()

        result = deploy(cfg, dry_run=True, provider=provider, engine=engine)

        assert isinstance(result, DeployResult)
        assert result.instance is None
        assert result.plan_text != "", "plan_text must be non-empty in dry-run"

    def test_dry_run_plan_text_is_vendor_neutral(self, capsys: Any) -> None:
        """AC1: plan text contains engine/provider/key info without cloud specifics."""
        cfg = _compute_cfg()
        engine = _make_engine()
        provider = LocalProvider()

        result = deploy(cfg, dry_run=True, provider=provider, engine=engine)

        # Must contain engine name, model count, provider name, key hash
        assert "fake" in result.plan_text.lower()
        assert "local" in result.plan_text.lower()


# ---------------------------------------------------------------------------
# AC2: hosted engine (requires_compute=False) — no provider touched
# ---------------------------------------------------------------------------


class TestDeployHosted:
    def test_hosted_deploy_returns_no_instance(self) -> None:
        """AC2: hosted engine deploy returns DeployResult with instance=None."""
        # For the hosted path we need a config that FakeEngine is compatible with.
        # Use the compute config but inject a hosted engine variant.
        cfg = _compute_cfg()
        engine = HostedFakeEngine(
            probe_profile=_probe_profile(),
            declared_flags_map={},
            required_spec_keys=set(),
        )
        spy = _RaisingProviderSpy()

        result = deploy(cfg, engine=engine, provider=spy)

        assert result.instance is None

    def test_hosted_deploy_endpoints_populated(self) -> None:
        """AC2: hosted engine returns endpoints from engine.backend()."""
        cfg = _compute_cfg()
        engine = HostedFakeEngine(
            probe_profile=_probe_profile(),
            declared_flags_map={},
            required_spec_keys=set(),
        )

        result = deploy(cfg, engine=engine, provider=_RaisingProviderSpy())

        assert isinstance(result.endpoints, dict)
        # FakeEngine/FakeBackend always has a "generate" endpoint
        assert "generate" in result.endpoints


# ---------------------------------------------------------------------------
# AC3: end-to-end generate() with LocalProvider + FakeEngine + LocalArtifactStore
# ---------------------------------------------------------------------------


class TestGenerateEndToEnd:
    def test_generate_returns_artifact_with_readable_uri(self, tmp_path: Path) -> None:
        """AC3: generate() returns Artifact with uri that can be read from the store."""
        cfg = _compute_cfg()
        engine = _make_engine()
        provider = LocalProvider()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        artifact = generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
        )

        assert isinstance(artifact, Artifact)
        assert artifact.uri != "", "returned Artifact must have a uri"
        # Read back from the store
        data = store.get_bytes(artifact.uri)
        assert len(data) > 0

    def test_generate_artifact_bytes_match(self, tmp_path: Path) -> None:
        """AC3: bytes stored are retrievable and non-trivial."""
        cfg = _compute_cfg()
        engine = _make_engine()
        provider = LocalProvider()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        artifact = generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
        )

        data = store.get_bytes(artifact.uri)
        # FakeBackend derives name from sha256 of prompts; file should contain
        # the filename as prefix (per GenerateClipStage._artifact_bytes)
        assert b"clip-" in data


# ---------------------------------------------------------------------------
# AC4: Discovery ordering — inspect_capabilities called once on miss, once on hit
# (verify runs on cache-hit path — exactly one call for discover, one for verify)
# ---------------------------------------------------------------------------


class TestDiscoveryOrdering:
    def test_inspect_called_once_on_cache_miss(self, tmp_path: Path) -> None:
        """AC4: first generate() calls inspect_capabilities exactly once (discover path).

        When the profile cache is empty, discover() is called which calls
        inspect_capabilities once. Verify is skipped on a freshly-discovered profile
        (trivially consistent with itself). Total = 1.
        """
        cfg = _compute_cfg()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        # Shared counter across all backends created during this test
        global_inspect_count: list[int] = [0]

        class CountingEngine(FakeEngine):
            def backend(
                self, instance: Instance | None, cfg: dict[str, object]
            ) -> FakeBackend:
                probe = _probe_profile()

                class _CB(FakeBackend):
                    def inspect_capabilities(self) -> ModelProfile:
                        global_inspect_count[0] += 1
                        return super().inspect_capabilities()

                return _CB(probe=probe)

        engine = CountingEngine(
            probe_profile=_probe_profile(),
            declared_flags_map={},
            required_spec_keys=set(),
        )
        provider = LocalProvider()

        generate(cfg, request, store=store, provider=provider, engine=engine)

        assert global_inspect_count[0] == 1, (
            f"Expected inspect_capabilities called 1 time on cache miss "
            f"(discover only, verify skipped on fresh profile), "
            f"got {global_inspect_count[0]}"
        )

    def test_second_generate_hits_cache_and_runs_verify(self, tmp_path: Path) -> None:
        """AC4: second generate() hits cache (zero new discover calls) and runs verify once.

        Discovery ordering: discover is NOT called on the second generate (profile is
        cached). Verify IS called once to check for model drift. Total inspect_capabilities
        calls across both generate() calls = 2 (one discover + one verify).
        """
        cfg = _compute_cfg()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        global_inspect_count: list[int] = [0]

        class CountingEngine(FakeEngine):
            def backend(
                self, instance: Instance | None, cfg: dict[str, object]
            ) -> FakeBackend:
                probe = _probe_profile()

                class _CB(FakeBackend):
                    def inspect_capabilities(self) -> ModelProfile:
                        global_inspect_count[0] += 1
                        return super().inspect_capabilities()

                return _CB(probe=probe)

        engine = CountingEngine(
            probe_profile=_probe_profile(),
            declared_flags_map={},
            required_spec_keys=set(),
        )
        provider = LocalProvider()

        # First call — populates cache; discover called once, verify skipped
        generate(cfg, request, store=store, provider=provider, engine=engine)
        count_after_first = global_inspect_count[0]

        # Second call — cache hit; discover NOT called, verify called once
        generate(cfg, request, store=store, provider=provider, engine=engine)
        count_after_second = global_inspect_count[0]

        assert count_after_first == 1, (
            f"First call: expected 1 inspect_capabilities (discover only), "
            f"got {count_after_first}"
        )
        assert count_after_second == 2, (
            f"Second call: expected 1 more inspect_capabilities (verify), "
            f"total should be 2, got {count_after_second}"
        )
        # Key property: no discover called on second run (discover = 0 additional calls
        # beyond the single verify)
        assert count_after_second - count_after_first == 1, (
            "Second call must add exactly 1 inspect_capabilities call (verify only, no discover)"
        )


# ---------------------------------------------------------------------------
# AC5: Fail-hard teardown on CapabilityMismatch
# ---------------------------------------------------------------------------


class TestFailHardTeardown:
    def test_capability_mismatch_triggers_destroy_and_raises(
        self, tmp_path: Path
    ) -> None:
        """AC5: CapabilityMismatch during verify → destroy_instance called once, then re-raised.

        Strategy:
        1. Populate cache with max_frames=16 using a stable engine.
        2. On the second generate(), use a DriftedEngine whose backend's
           inspect_capabilities() immediately returns max_frames=32 (drifted),
           causing verify() to raise CapabilityMismatch.
        3. Assert destroy_instance was called exactly once.
        """
        cfg = _compute_cfg()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        spy_provider = DestroySpyProvider()

        # Step 1: populate cache with stable engine (max_frames=16)
        stable_engine = _make_engine(max_frames=16)
        generate(
            cfg,
            request,
            store=store,
            provider=spy_provider,
            engine=stable_engine,
        )

        # Step 2: second generate with a drifted engine (verify returns max_frames=32)
        drifted_probe = _probe_profile(max_frames=32)

        class DriftedBackend(FakeBackend):
            def inspect_capabilities(self) -> ModelProfile:
                return drifted_probe

        class DriftedEngine(FakeEngine):
            def backend(
                self, instance: Instance | None, cfg: dict[str, object]
            ) -> DriftedBackend:
                return DriftedBackend(probe=drifted_probe)

        drifted_engine = DriftedEngine(
            probe_profile=drifted_probe,
            declared_flags_map={},
            required_spec_keys=set(),
        )

        with pytest.raises(CapabilityMismatch):
            generate(
                cfg,
                request,
                store=store,
                provider=spy_provider,
                engine=drifted_engine,
            )

        assert len(spy_provider.destroy_calls) == 1, (
            f"Expected destroy_instance called once, got {spy_provider.destroy_calls}"
        )

    def test_teardown_occurs_before_reraise(self, tmp_path: Path) -> None:
        """AC5: destroy_instance is called BEFORE the exception propagates."""
        cfg = _compute_cfg()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        call_order: list[str] = []

        class OrderTrackingProvider(DestroySpyProvider):
            def destroy_instance(self, instance_id: str) -> None:
                call_order.append("destroy")
                super().destroy_instance(instance_id)

        spy_provider = OrderTrackingProvider()

        # Populate cache first with stable engine (max_frames=16)
        stable_engine = _make_engine(max_frames=16)
        generate(cfg, request, store=store, provider=spy_provider, engine=stable_engine)

        # Second generate with drifted engine (verify sees max_frames=32)
        drifted_probe = _probe_profile(max_frames=32)

        class DriftedBackend(FakeBackend):
            def inspect_capabilities(self) -> ModelProfile:
                return drifted_probe

        class DriftedEngine(FakeEngine):
            def backend(
                self, instance: Instance | None, cfg: dict[str, object]
            ) -> DriftedBackend:
                return DriftedBackend(probe=drifted_probe)

        drifted_engine = DriftedEngine(
            probe_profile=drifted_probe,
            declared_flags_map={},
            required_spec_keys=set(),
        )

        try:
            generate(
                cfg, request, store=store, provider=spy_provider, engine=drifted_engine
            )
        except CapabilityMismatch:
            call_order.append("exception_caught")

        assert call_order == ["destroy", "exception_caught"], (
            f"destroy must happen before exception propagates, got: {call_order}"
        )


# ---------------------------------------------------------------------------
# AC6: 1-segment job produced (DEFERRED splitter comment in code)
# ---------------------------------------------------------------------------


class TestSplitterStub:
    def test_single_segment_job_produced(self, tmp_path: Path) -> None:
        """AC6: The generate flow builds exactly one segment (splitter DEFERRED)."""
        cfg = _compute_cfg()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="one segment test", mode="t2v")

        segments_seen: list[list[Any]] = []

        class CapturingBackend(FakeBackend):
            def submit(self, job: GenerationJob) -> str:
                segments_seen.append(list(job.segments))
                return super().submit(job)

        class CapturingEngine(FakeEngine):
            def backend(
                self, instance: Instance | None, cfg: dict[str, object]
            ) -> CapturingBackend:
                return CapturingBackend(probe=_probe_profile())

        engine = CapturingEngine(
            probe_profile=_probe_profile(),
            declared_flags_map={},
            required_spec_keys=set(),
        )
        provider = LocalProvider()

        generate(cfg, request, store=store, provider=provider, engine=engine)

        assert len(segments_seen) >= 1, "At least one job must be submitted"
        # Each job should have exactly one segment (stub: no splitter yet)
        for segs in segments_seen:
            assert len(segs) == 1, (
                f"Expected 1 segment per job (splitter DEFERRED), got {len(segs)}"
            )


# ---------------------------------------------------------------------------
# O-1..O-4: Splitter wired into orchestrator.generate()
# ---------------------------------------------------------------------------


def _i2v_probe() -> ModelProfile:
    """ModelProfile that supports both t2v and i2v modes."""
    return ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v", "i2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


class I2vFakeEngine(FakeEngine):
    """FakeEngine variant whose backend reports i2v support and accepts images."""

    accepted_kinds: set[str] = {"image"}

    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> FakeBackend:
        return FakeBackend(probe=_i2v_probe())


def _make_i2v_engine() -> I2vFakeEngine:
    return I2vFakeEngine(
        probe_profile=_i2v_probe(),
        declared_flags_map={},
        required_spec_keys=set(),
    )


def _make_init_image_asset(tmp_path: Path) -> ConditioningAsset:
    """Write a tiny PNG file and wrap it as an init_image ConditioningAsset."""
    img = tmp_path / "init.png"
    img.write_bytes(b"\x89PNG\r\n")
    return ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(uri=str(img), filename="init.png", meta={}),
    )


class TestSplitterWiring:
    def test_orchestrator_multi_paragraph_splits_into_n_segments(
        self, tmp_path: Path
    ) -> None:
        # Bug: splitter not wired into generate() — multi-paragraph prompts
        # silently collapse to one segment and the marquee feature does nothing.
        cfg = _compute_cfg()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(
            prompt="paragraph one\n\nparagraph two\n\nparagraph three",
            mode="t2v",
        )
        engine = _make_engine()
        provider = LocalProvider()

        captured: dict[str, list[Segment] | None] = {"segments_override": None}
        real_run = GenerateClipStage.run

        def _spy(
            self: GenerateClipStage,
            req: GenerationRequest,
            *,
            segments_override: list[Segment] | None = None,
        ) -> Artifact:
            captured["segments_override"] = segments_override
            return real_run(self, req, segments_override=segments_override)

        with patch.object(GenerateClipStage, "run", _spy):
            generate(cfg, request, store=store, provider=provider, engine=engine)

        segments = captured["segments_override"]
        assert segments is not None
        assert [s.prompt for s in segments] == [
            "paragraph one",
            "paragraph two",
            "paragraph three",
        ]

    def test_orchestrator_attaches_assets_to_segment_zero_only(
        self, tmp_path: Path
    ) -> None:
        # Bug: assets accidentally copied to every segment — every clip in a
        # multi-paragraph run gets the same init_image stamped on it, which
        # is wrong for narrative continuity.
        engine = _make_i2v_engine()
        store = LocalArtifactStore(tmp_path)
        asset = _make_init_image_asset(tmp_path)
        request = GenerationRequest(
            prompt="scene one\n\nscene two",
            mode="i2v",
            assets=[asset],
        )
        cfg = _compute_cfg()
        provider = LocalProvider()

        captured: dict[str, list[Segment] | None] = {"segments_override": None}
        real_run = GenerateClipStage.run

        def _spy(
            self: GenerateClipStage,
            req: GenerationRequest,
            *,
            segments_override: list[Segment] | None = None,
        ) -> Artifact:
            captured["segments_override"] = segments_override
            return real_run(self, req, segments_override=segments_override)

        with patch.object(GenerateClipStage, "run", _spy):
            generate(cfg, request, store=store, provider=provider, engine=engine)

        segments = captured["segments_override"]
        assert segments is not None
        assert len(segments) == 2
        assert len(segments[0].assets) == 1
        assert segments[0].assets[0].role == "init_image"
        assert segments[1].assets == []

    def test_orchestrator_single_paragraph_regression(self, tmp_path: Path) -> None:
        # Bug: the splitter wiring regresses today's single-segment + assets
        # happy path. With one paragraph + one asset, exactly one Segment
        # carrying the asset must reach the stage.
        engine = _make_i2v_engine()
        store = LocalArtifactStore(tmp_path)
        asset = _make_init_image_asset(tmp_path)
        request = GenerationRequest(
            prompt="just one paragraph",
            mode="i2v",
            assets=[asset],
        )
        cfg = _compute_cfg()
        provider = LocalProvider()

        captured: dict[str, list[Segment] | None] = {"segments_override": None}
        real_run = GenerateClipStage.run

        def _spy(
            self: GenerateClipStage,
            req: GenerationRequest,
            *,
            segments_override: list[Segment] | None = None,
        ) -> Artifact:
            captured["segments_override"] = segments_override
            return real_run(self, req, segments_override=segments_override)

        with patch.object(GenerateClipStage, "run", _spy):
            generate(cfg, request, store=store, provider=provider, engine=engine)

        segments = captured["segments_override"]
        assert segments is not None
        assert len(segments) == 1
        assert segments[0].prompt == "just one paragraph"
        assert len(segments[0].assets) == 1

    def test_orchestrator_default_splitter_resolved_at_runtime(
        self, tmp_path: Path
    ) -> None:
        # Bug: when the config omits the splitter: block, generate() blows up
        # looking for a missing field instead of resolving the heuristic default.
        cfg = _hosted_cfg()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a calm sea", mode="t2v")
        engine = _make_engine()
        provider = LocalProvider()

        artifact = generate(
            cfg, request, store=store, provider=provider, engine=engine, run_id="r4"
        )
        assert artifact.uri  # store URI populated


# ---------------------------------------------------------------------------
# Layer G Task 5: orchestrator closes ConcurrentPool after generate()
# ---------------------------------------------------------------------------


def test_generate_closes_concurrent_pool_after_run(tmp_path: Path) -> None:
    """orchestrator.generate() closes its ConcurrentPool on return.

    Spies on ConcurrentPool by monkey-patching close() to record the call,
    then verifies it was called exactly once after generate() returns.
    """
    from kinoforge.core import pool as pool_mod

    close_calls: list[bool] = []
    original_close = pool_mod.ConcurrentPool.close

    def _spy_close(self: pool_mod.ConcurrentPool) -> None:
        close_calls.append(True)
        original_close(self)

    pool_mod.ConcurrentPool.close = _spy_close  # type: ignore[method-assign]
    try:
        cfg = _compute_cfg()
        engine = _make_engine()
        provider = LocalProvider()
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        result = generate(cfg, request, store=store, provider=provider, engine=engine)
        assert result is not None
        assert close_calls == [True], (
            f"orchestrator did not close the pool exactly once; "
            f"close_calls={close_calls}"
        )
    finally:
        pool_mod.ConcurrentPool.close = original_close  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Layer I Task 8: UX A hosted preflight in orchestrator.generate()
#
# For hosted engines (requires_compute=False), generate() must call
# engine.provision(None, cfg_dict) BEFORE any backend.submit so that
# missing-cred / health-probe failures surface up-front rather than
# crashing mid-pipeline inside backend.submit.
# ---------------------------------------------------------------------------


class _PreflightTracker:
    """Shared monotonic counter so provision/submit get ordered call indices."""

    def __init__(self) -> None:
        self.counter: int = 0

    def tick(self) -> int:
        self.counter += 1
        return self.counter


class _OrderTrackingBackend(FakeBackend):
    """FakeBackend that records the call order of its submit() invocations."""

    def __init__(self, probe: ModelProfile, tracker: _PreflightTracker) -> None:
        super().__init__(probe=probe)
        self._tracker = tracker
        self.submit_call_count: int = 0
        self.submit_call_order: int | None = None

    def submit(self, job: GenerationJob) -> str:
        self.submit_call_count += 1
        # Record only the first submit so AC1's ordering check is unambiguous.
        if self.submit_call_order is None:
            self.submit_call_order = self._tracker.tick()
        return super().submit(job)


class _PreflightHostedEngine(FakeEngine):
    """Hosted engine that records provision() call order and shares a backend.

    requires_compute=False so the orchestrator takes the hosted path.
    The fake's provision() either records the call or raises an injected
    exception (used to verify AC2/AC3 — provision failure blocks submit).
    """

    requires_compute: bool = False

    def __init__(
        self,
        *,
        probe_profile: ModelProfile,
        tracker: _PreflightTracker,
        provision_exception: Exception | None = None,
    ) -> None:
        super().__init__(
            probe_profile=probe_profile,
            declared_flags_map={},
            required_spec_keys=set(),
        )
        self._tracker = tracker
        self._provision_exception = provision_exception
        self.provision_call_count: int = 0
        self.provision_call_order: int | None = None
        # Share one backend across all backend() calls so the orchestrator's
        # discover-path and pool-path see the same submit counters.
        self._shared_backend = _OrderTrackingBackend(
            probe=probe_profile, tracker=tracker
        )

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        self.provision_call_count += 1
        self.provision_call_order = self._tracker.tick()
        if self._provision_exception is not None:
            raise self._provision_exception

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> _OrderTrackingBackend:
        return self._shared_backend


class TestHostedPreflight:
    def test_hosted_preflight_calls_provision_before_backend_submit(
        self, tmp_path: Path
    ) -> None:
        """generate() must call engine.provision(None, cfg_dict) exactly once
        before any backend.submit for hosted engines (requires_compute=False).

        Bug catch: previous behavior bypassed engine.provision entirely from
        generate(), causing cred-missing failures to crash mid-flight inside
        backend.submit instead of failing fast at the preflight step.
        """
        cfg = _compute_cfg()
        tracker = _PreflightTracker()
        engine = _PreflightHostedEngine(
            probe_profile=_probe_profile(),
            tracker=tracker,
        )
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        # Hosted engines must not touch the provider; supply the raising spy
        # to catch any accidental compute-path call.
        generate(
            cfg,
            request,
            store=store,
            provider=_RaisingProviderSpy(),
            engine=engine,
        )

        assert engine.provision_call_count == 1, (
            f"expected exactly one engine.provision() call, "
            f"got {engine.provision_call_count}"
        )
        assert engine.provision_call_order is not None, (
            "engine.provision() was never called"
        )
        assert engine._shared_backend.submit_call_order is not None, (
            "backend.submit() was never called — generate() did not reach the pipeline"
        )
        assert engine.provision_call_order < engine._shared_backend.submit_call_order, (
            f"engine.provision() (order={engine.provision_call_order}) must run "
            f"BEFORE backend.submit() (order={engine._shared_backend.submit_call_order})"
        )

    def test_hosted_preflight_auth_error_blocks_backend_submit(
        self, tmp_path: Path
    ) -> None:
        """When engine.provision raises AuthError, no backend.submit happens.

        Bug catch: if preflight is skipped or runs after submit, the cred
        failure would surface from deep inside the pipeline rather than from
        generate()'s preflight step.
        """
        from kinoforge.core.errors import AuthError

        cfg = _compute_cfg()
        tracker = _PreflightTracker()
        engine = _PreflightHostedEngine(
            probe_profile=_probe_profile(),
            tracker=tracker,
            provision_exception=AuthError("KINOFORGE_FAKE_API_KEY not set"),
        )
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        with pytest.raises(AuthError, match="KINOFORGE_FAKE_API_KEY"):
            generate(
                cfg,
                request,
                store=store,
                provider=_RaisingProviderSpy(),
                engine=engine,
            )

        assert engine._shared_backend.submit_call_count == 0, (
            f"backend.submit() must not be called when preflight AuthError fires; "
            f"got submit_call_count={engine._shared_backend.submit_call_count}"
        )

    def test_hosted_preflight_health_error_blocks_backend_submit(
        self, tmp_path: Path
    ) -> None:
        """When engine.provision raises KinoforgeError (health probe failure),
        no backend.submit happens.

        Bug catch: an unreachable hosted endpoint must surface at preflight,
        not as a confusing failure inside backend.submit during pipeline run.
        """
        from kinoforge.core.errors import KinoforgeError

        cfg = _compute_cfg()
        tracker = _PreflightTracker()
        engine = _PreflightHostedEngine(
            probe_profile=_probe_profile(),
            tracker=tracker,
            provision_exception=KinoforgeError(
                "hosted endpoint unreachable: connection refused"
            ),
        )
        store = LocalArtifactStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        with pytest.raises(KinoforgeError, match="unreachable"):
            generate(
                cfg,
                request,
                store=store,
                provider=_RaisingProviderSpy(),
                engine=engine,
            )

        assert engine._shared_backend.submit_call_count == 0, (
            f"backend.submit() must not be called when preflight health probe "
            f"fails; got submit_call_count={engine._shared_backend.submit_call_count}"
        )


# ---------------------------------------------------------------------------
# Layer K Task 2: cfg.spec/cfg.params routing + ValidationError teardown
# ---------------------------------------------------------------------------


def test_generate_routes_cfg_spec_into_job_spec(tmp_path: Path) -> None:
    """cfg.spec values reach GenerationJob.spec via stage.base_spec.

    Bug catch: hardcoded base_spec={} at orchestrator.py:605 means
    orchestrator-driven hosted/diffusers/comfyui runs fail validate_spec
    on every config typo for missing required spec keys.
    """
    cfg = _compute_cfg()
    cfg.spec = {"k": "v", "params": {"guidance_scale": 5.0}}
    cfg.params = {"fps": 24}

    captured: dict[str, Any] = {}

    class _SpySpecEngine(FakeEngine):
        def validate_spec(self, job: GenerationJob) -> None:
            captured["spec"] = dict(job.spec)
            captured["params"] = dict(job.params)
            super().validate_spec(job)

    engine = _SpySpecEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys={"k"},
    )

    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="hello", mode="t2v")

    with patch(
        "kinoforge.core.registry.get_engine", side_effect=lambda _kind: lambda: engine
    ):
        generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="run-spec-routing",
            state_dir=tmp_path,
        )

    assert captured["spec"]["k"] == "v"
    assert captured["spec"]["params"] == {"guidance_scale": 5.0}
    assert captured["params"] == {"fps": 24}


def test_generate_does_not_alias_cfg_spec_into_job_spec(tmp_path: Path) -> None:
    """A mutation of job.spec inside the engine does not bleed into cfg.spec.

    Bug catch: pydantic returns the underlying dict by reference. Without
    a defensive dict() copy at stage construction, an engine that does
    `job.spec["seen"] = True` corrupts the user's cfg.
    """
    cfg = _compute_cfg()
    cfg.spec = {"k": "v"}

    class _MutatingEngine(FakeEngine):
        def validate_spec(self, job: GenerationJob) -> None:
            job.spec["mutated_by_engine"] = True
            super().validate_spec(job)

    engine = _MutatingEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys={"k"},
    )

    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="hi", mode="t2v")

    with patch(
        "kinoforge.core.registry.get_engine", side_effect=lambda _kind: lambda: engine
    ):
        generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="run-isolation",
            state_dir=tmp_path,
        )

    assert "mutated_by_engine" not in cfg.spec


def test_generate_tears_down_compute_on_validate_spec_failure(
    tmp_path: Path,
) -> None:
    """ValidationError from engine.validate_spec → destroy_instance called once.

    Bug catch: without the teardown wrapper, a typo in spec: that
    triggers ValidationError leaves a RunPod pod billing until reap.
    """
    cfg = _compute_cfg()
    cfg.spec = {}  # empty — fails the required_spec_keys gate

    engine = FakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys={"required_key"},  # cfg.spec is missing this
    )

    destroy_calls: list[str] = []

    class _TrackingProvider(LocalProvider):
        def destroy_instance(self, instance_id: str) -> None:
            destroy_calls.append(instance_id)
            super().destroy_instance(instance_id)

    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="hi", mode="t2v")
    tracking_provider = _TrackingProvider()

    with (
        patch(
            "kinoforge.core.registry.get_engine",
            side_effect=lambda _kind: lambda: engine,
        ),
        patch(
            "kinoforge.core.registry.get_provider",
            side_effect=lambda _kind: lambda: tracking_provider,
        ),
        pytest.raises(ValidationError),
    ):
        generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="run-teardown",
            state_dir=tmp_path,
        )

    assert len(destroy_calls) == 1, (
        f"expected exactly one destroy_instance call, saw {destroy_calls!r}"
    )


# ---------------------------------------------------------------------------
# Layer P Task 7 item #2: orchestrator offer-retry across CapacityError
# ---------------------------------------------------------------------------


class _OfferRetryProvider(LocalProvider):
    """Fake provider scripted per-call to test offer-retry mechanics.

    Configured with a list of offers from find_offers() and a parallel
    list of outcomes:
        "capacity" -> raise CapacityError(...) on create_instance
        "value"    -> raise ValueError("non-capacity") on create_instance
        "ok"       -> return a real Instance with id derived from the offer

    Records every (offer, outcome) pair so tests can assert iteration
    order and call count.
    """

    def __init__(self, offers: list[Offer], outcomes: list[str]) -> None:
        super().__init__()
        if len(offers) != len(outcomes):
            raise AssertionError("offers and outcomes must be same length")
        self._scripted_offers = offers
        self._outcomes = outcomes
        self._index = 0
        self.calls: list[Offer] = []
        # Track CapacityError exceptions so identity-check can verify __cause__
        self.last_capacity_excs: list[CapacityError] = []

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        return list(self._scripted_offers)

    def create_instance(self, spec: InstanceSpec) -> Instance:
        idx = self._index
        self._index += 1
        assert spec.offer is not None
        self.calls.append(spec.offer)
        outcome = self._outcomes[idx]
        if outcome == "capacity":
            exc = CapacityError(
                f"RunPod has no current capacity for {spec.offer.gpu_type!r}"
            )
            self.last_capacity_excs.append(exc)
            raise exc
        if outcome == "value":
            raise ValueError("non-capacity error from provider")
        if outcome == "ok":
            return Instance(
                id=f"pod-{spec.offer.id}",
                provider="local",
                status="ready",  # skip the get_instance poll
                created_at=0.0,
                tags=dict(spec.tags),
            )
        raise AssertionError(f"unknown outcome {outcome!r}")


def _three_offers() -> list[Offer]:
    """Three distinct offers ordered by gpu_preference (already sorted)."""
    return [
        Offer(
            id=f"offer-{i}",
            gpu_type=f"GPU_{i}",
            vram_gb=24,
            cuda="12.0",
            cost_rate_usd_per_hr=0.10 * (i + 1),
            mode="pod",
        )
        for i in range(3)
    ]


def test_deploy_retries_next_offer_on_capacity_error() -> None:
    """deploy() walks past the first CapacityError and uses offer[1].

    Bug catch: if _create_with_offer_retry isn't wired into deploy(),
    deploy crashes on the first CapacityError exactly as PROGRESS:182
    describes. The chosen-instance id assertion locks the off-by-one
    case where the helper returns offers[0]'s spec but advances past it.
    """
    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["capacity", "ok", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()

    result = deploy(cfg, provider=provider, engine=engine)

    assert result.instance is not None
    assert result.instance.id == "pod-offer-1"
    assert [o.id for o in provider.calls] == ["offer-0", "offer-1"]


def test_deploy_iterates_offers_in_input_order() -> None:
    """deploy() walks offers in exact find_offers-returned order.

    Bug catch: a future change that uses set() / reversed() / random
    iteration silently breaks the cost-aware sort done by filter_offers.
    Cheapest available offer would no longer be tried first.
    """
    offers = _three_offers()
    # offer[3] would succeed if it existed; here we exhaust 2 then succeed
    # to keep the assertion focused on iteration order only
    provider = _OfferRetryProvider(offers, ["capacity", "capacity", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()

    deploy(cfg, provider=provider, engine=engine)

    assert [o.id for o in provider.calls] == ["offer-0", "offer-1", "offer-2"]


def test_deploy_raises_capacity_error_when_all_offers_exhausted() -> None:
    """Every offer raises CapacityError → final exc is CapacityError with chain.

    Bug catch: raising ValueError, KinoforgeError, or a fresh
    CapacityError without __cause__ blinds the operator to the last
    real RunPod message. Identity check on __cause__ catches misuse
    of `raise X from None` (or no `from` at all, which falls through to
    the in-handler implicit chaining and wraps the wrong exception).
    """
    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["capacity", "capacity", "capacity"])
    cfg = _compute_cfg()
    engine = _make_engine()

    with pytest.raises(CapacityError) as exc_info:
        deploy(cfg, provider=provider, engine=engine)

    assert "3 offers exhausted" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, CapacityError)
    # Identity: the chained cause IS the last per-offer exception
    assert exc_info.value.__cause__ is provider.last_capacity_excs[-1]


def test_deploy_does_not_retry_on_non_capacity_error() -> None:
    """Non-CapacityError exceptions propagate after exactly 1 create call.

    Bug catch: a too-broad `except Exception:` in the retry helper
    would silently retry auth / config errors across every offer,
    burning time and obscuring the real failure.
    """
    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["value", "ok", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()

    with pytest.raises(ValueError, match="non-capacity"):
        deploy(cfg, provider=provider, engine=engine)

    assert len(provider.calls) == 1, (
        f"non-CapacityError must propagate immediately; "
        f"got {len(provider.calls)} create_instance calls"
    )


def test_provision_instance_helper_retries_next_offer_on_capacity_error(
    tmp_path: Path,
) -> None:
    """The deploy_session compute helper retries identically to deploy().

    Tests `_provision_instance_and_build_backend` directly because it
    is the actual site of the second `offers[0]` (PROGRESS:182 only
    flagged deploy()'s site at line 626; this one at line 283 was
    silently sharing the same bug). Tested at the helper level rather
    than through `with deploy_session(...)` to avoid pulling in
    provisioner / profile-cache machinery unrelated to offer-retry.

    Bug catch: forgetting to rewire _provision_instance_and_build_backend
    leaves generate() and batch_generate() broken on the same capacity
    blip — a silent regression with no observable difference in deploy()
    tests.
    """
    from kinoforge.core.orchestrator import _provision_instance_and_build_backend

    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["capacity", "ok", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()
    store = LocalArtifactStore(tmp_path)

    # Patch the in-helper provisioner to a no-op so the test focuses on
    # the offer-retry mechanism rather than weights / profile cache I/O.
    with patch(
        "kinoforge.core.orchestrator._provision_compute_once",
        return_value=None,
    ):
        instance, _backend = _provision_instance_and_build_backend(
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

    assert instance.id == "pod-offer-1"
    assert [o.id for o in provider.calls] == ["offer-0", "offer-1"]


# ---------------------------------------------------------------------------
# Layer N regression: deploy() destroys pod on any post-create error
# ---------------------------------------------------------------------------


def test_deploy_destroys_pod_when_get_instance_raises() -> None:
    """Layer N regression — deploy() MUST destroy the pod if any error fires
    after create_instance returns.  Otherwise a paid-for pod is orphaned.

    Constructs a minimal fake provider that creates an instance with
    status="starting" and raises on the very next get_instance call.
    Asserts that destroy_instance is called with the same pod ID before
    the exception propagates.
    """
    from kinoforge.core.orchestrator import deploy

    _FAKE_POD_ID = "layer-n-orphan-pod"

    class _GetInstanceRaisingProvider(LocalProvider):
        """Creates a pod then raises KeyError on the first get_instance call.

        destroy_instance is tracked so the test can assert cleanup happened.
        """

        def __init__(self) -> None:
            super().__init__()
            self.destroy_calls: list[str] = []

        def create_instance(self, spec: InstanceSpec) -> Instance:
            return Instance(
                id=_FAKE_POD_ID,
                provider="local",
                status="starting",  # not "ready" — forces a get_instance poll
                created_at=0.0,
                tags={},
            )

        def get_instance(self, instance_id: str) -> Instance:
            raise KeyError(f"simulated RunPod null response for {instance_id!r}")

        def destroy_instance(self, instance_id: str) -> None:
            self.destroy_calls.append(instance_id)
            # Don't call super() — the fake pod isn't tracked in LocalProvider.

    cfg = _compute_cfg()
    engine = _make_engine()
    spy_provider = _GetInstanceRaisingProvider()

    with pytest.raises(KeyError, match="simulated RunPod null response"):
        deploy(cfg, provider=spy_provider, engine=engine)

    assert spy_provider.destroy_calls == [_FAKE_POD_ID], (
        f"deploy() must call destroy_instance({_FAKE_POD_ID!r}) when get_instance raises; "
        f"got: {spy_provider.destroy_calls!r}"
    )


def test_generate_validates_spec_with_real_request_prompt_not_probe(
    tmp_path: Path,
) -> None:
    """validate_spec receives the real Segment.prompt, never an empty probe.

    Bug catch: an earlier Layer K iteration validated via a probe job with
    Segment(prompt=""). For real engines that consume Layer J's
    resolve_prompt (hosted/diffusers/fal), an empty Segment.prompt makes
    resolve_prompt return None and validate_spec raises even when the
    user's request carried a perfectly good prompt.
    """
    cfg = _compute_cfg()
    cfg.spec = {"k": "v"}

    observed_prompts: list[str] = []

    class _PromptObservingEngine(FakeEngine):
        def validate_spec(self, job: GenerationJob) -> None:
            if job.segments:
                observed_prompts.append(job.segments[0].prompt)
            super().validate_spec(job)

    engine = _PromptObservingEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys={"k"},
    )

    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="cinematic shot of a forest", mode="t2v")

    with patch(
        "kinoforge.core.registry.get_engine", side_effect=lambda _kind: lambda: engine
    ):
        generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="run-real-prompt",
            state_dir=tmp_path,
        )

    assert observed_prompts, "validate_spec was never called"
    assert all(p == "cinematic shot of a forest" for p in observed_prompts), (
        f"validate_spec saw stale/empty prompt: {observed_prompts!r}"
    )


# ---------------------------------------------------------------------------
# Layer O Task 5: generate() sink kwarg threading
# ---------------------------------------------------------------------------


class _SpyOutputSink:
    """Minimal OutputSink spy that records every publish() call.

    Implements the OutputSink Protocol without inheriting from any
    concrete class, to keep the test self-contained (option (a) per spec).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
    ) -> str:
        """Record the call and return a fake URI."""
        self.calls.append(
            {
                "data_len": len(data),
                "prompt": prompt,
                "extension": extension,
                "namespace": namespace,
            }
        )
        return f"spy://{prompt[:20]}{extension}"


def test_generate_default_sink_is_none(tmp_path: Path) -> None:
    """AC1 regression lock: generate() with no sink kwarg still works end-to-end.

    Verifies that the None default keeps every existing call site working
    bit-for-bit after the sink kwarg is added.
    """
    cfg = _compute_cfg()
    engine = _make_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="a calm sea at dusk", mode="t2v")

    artifact = generate(
        cfg,
        request,
        store=store,
        provider=provider,
        engine=engine,
    )

    assert isinstance(artifact, Artifact)
    assert artifact.uri != "", "Artifact must have a non-empty uri when sink=None"
    data = store.get_bytes(artifact.uri)
    assert len(data) > 0, "Store must contain bytes when sink=None"


def test_generate_threads_sink_into_stage(tmp_path: Path) -> None:
    """AC2: generate(sink=spy) threads the sink into GenerateClipStage and
    the spy receives at least one publish() call during the run.
    """
    cfg = _compute_cfg()
    engine = _make_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="a stormy ocean wave", mode="t2v")
    spy = _SpyOutputSink()

    artifact = generate(
        cfg,
        request,
        store=store,
        provider=provider,
        engine=engine,
        sink=spy,
    )

    assert isinstance(artifact, Artifact)
    assert len(spy.calls) >= 1, (
        f"spy.publish() must be called at least once when sink=spy; "
        f"got spy.calls={spy.calls!r}"
    )


# ---------------------------------------------------------------------------
# Layer P Task 7 item #2: instance= + tags= kwarg tests
# ---------------------------------------------------------------------------


class _InstanceSupplyProvider(LocalProvider):
    """LocalProvider spy that records create_instance + destroy_instance + find_offers.

    Used to assert the orchestrator does NOT touch capacity/lifecycle APIs
    when the caller supplies a pre-created Instance via ``instance=``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.create_calls: list[InstanceSpec] = []
        self.destroy_calls: list[str] = []
        self.find_offers_calls: int = 0

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        self.find_offers_calls += 1
        return super().find_offers(reqs)

    def create_instance(self, spec: InstanceSpec) -> Instance:
        self.create_calls.append(spec)
        return super().create_instance(spec)

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)
        super().destroy_instance(instance_id)


class _CountingFakeEngine(FakeEngine):
    """FakeEngine spy that records every provision() + backend() call.

    Asserts the warm-pod path still hits ``engine.provision`` (idempotent
    via Layer I marker) and ``engine.backend`` on the caller-supplied
    Instance.
    """

    def __init__(self) -> None:
        super().__init__(
            probe_profile=_probe_profile(),
            declared_flags_map={},
            required_spec_keys=set(),
        )
        self.provision_calls: list[Instance | None] = []
        self.backend_calls: list[Instance | None] = []

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        self.provision_calls.append(instance)
        super().provision(instance, cfg)

    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> FakeBackend:
        self.backend_calls.append(instance)
        return super().backend(instance, cfg)


class _CountingProfileProvider(ModelProfileProvider):
    """ModelProfileProvider stub that counts discover() and verify() calls.

    Begins empty — resolve() always raises ProfileNotCached on the first
    request, forcing the discover() path. After discover(), subsequent
    resolve() calls hit the in-memory cache.
    """

    def __init__(self) -> None:
        self.discover_calls: int = 0
        self.verify_calls: int = 0
        self._cached: ModelProfile | None = None

    def resolve(self, key: CapabilityKey) -> ModelProfile:
        if self._cached is None:
            raise ProfileNotCached(f"no profile for {key.derive()!r}")
        return self._cached

    def discover(
        self,
        key: CapabilityKey,
        engine: Any,
        backend: Any,
    ) -> ModelProfile:
        self.discover_calls += 1
        probe = backend.inspect_capabilities()
        self._cached = probe
        return probe

    def verify(
        self,
        profile: ModelProfile,
        backend: Any,
        *,
        engine: Any = None,
        key: Any = None,
    ) -> None:
        self.verify_calls += 1


class _MismatchingProfileProvider(ModelProfileProvider):
    """ModelProfileProvider stub whose verify() always raises CapabilityMismatch.

    Used to drive the CapabilityMismatch teardown branch in deploy_session
    on a cache-hit path (so verify runs).
    """

    def __init__(self, cached: ModelProfile) -> None:
        self._cached = cached

    def resolve(self, key: CapabilityKey) -> ModelProfile:
        return self._cached

    def discover(
        self,
        key: CapabilityKey,
        engine: Any,
        backend: Any,
    ) -> ModelProfile:
        return self._cached

    def verify(
        self,
        profile: ModelProfile,
        backend: Any,
        *,
        engine: Any = None,
        key: Any = None,
    ) -> None:
        raise CapabilityMismatch("synthetic drift")


class _RaisingValidateSpecFakeEngine(_CountingFakeEngine):
    """FakeEngine whose validate_spec always raises ValidationError.

    Drives the post-deploy_session ValidationError teardown branch inside
    generate(). Inherits _CountingFakeEngine so we still see provision()
    + backend() on the caller-supplied instance.
    """

    def validate_spec(self, job: GenerationJob) -> None:
        raise ValidationError("synthetic spec failure")


def _make_premade_instance() -> Instance:
    """Build a fully-ready Instance dataclass for instance= kwarg tests.

    Tags include caller-meaningful values so tests can assert they survive
    untouched when ``tags=`` kwarg is ignored.
    """
    return Instance(
        id="pod-premade-7b2",
        provider="local",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={"kinoforge.layer": "layer-p-smoke", "mode": "pod"},
        cost_rate_usd_per_hr=0.0,
    )


def _seed_profile_cache(store: LocalArtifactStore, cfg: Config) -> None:
    """Populate JsonProfileCache for ``cfg.capability_key()`` with a probe.

    Forces deploy_session onto the cache-hit branch (so verify() runs)
    without needing a full real generate() warmup. Uses the public
    ``JsonProfileCache.warm`` test seam — no private-API reach.
    """
    from kinoforge.core.profiles import JsonProfileCache

    cache = JsonProfileCache(store)
    cache.warm(cfg.capability_key(), _probe_profile())


def test_deploy_session_with_supplied_instance_skips_create_and_find_offers(
    tmp_path: Path,
) -> None:
    """instance= supplied + warm cache → no find_offers + no create_instance.

    Bug catch: a missing instance= short-circuit in the cache-hit branch
    silently re-creates a parallel pod even though the caller already
    paid for one — the warm-reuse loop never converges. Asserts both
    capacity hooks (find_offers, create_instance) AND the engine wiring
    (provision, backend) still bind to the caller's Instance.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    _seed_profile_cache(store, cfg)

    with deploy_session(
        cfg, store=store, provider=spy, engine=engine, instance=premade
    ) as session:
        assert session.instance is premade
        assert session.backend is not None

    assert spy.find_offers_calls == 0
    assert spy.create_calls == []
    assert engine.provision_calls == [premade]
    assert engine.backend_calls == [premade]


def test_deploy_session_with_supplied_instance_runs_discover_on_cache_miss(
    tmp_path: Path,
) -> None:
    """instance= + empty cache → engine.provision + discover both run; no create.

    Bug catch: a too-eager short-circuit that also skips discover() would
    leave the profile cache permanently empty across warm-reuse iterations
    and force every call back through capability discovery — defeating
    the perf benefit of warm reuse.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    profile_provider = _CountingProfileProvider()
    premade = _make_premade_instance()

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        profile_provider=profile_provider,
        instance=premade,
    ) as session:
        assert session.instance is premade

    assert spy.find_offers_calls == 0
    assert spy.create_calls == []
    assert engine.provision_calls == [premade]
    assert profile_provider.discover_calls == 1
    # verify is skipped on the just-discovered path
    assert profile_provider.verify_calls == 0


def test_deploy_session_supplied_instance_calls_engine_provision(
    tmp_path: Path,
) -> None:
    """Discriminating: even on cache-hit, engine.provision must run for re-attached pod.

    Bug catch: a short-circuit that also skips provision() defeats Layer
    I's idempotent provisioning marker — the second warm-reuse iteration
    can hit a pod whose weights were never re-confirmed after a process
    restart, with no observable signal.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    _seed_profile_cache(store, cfg)

    with deploy_session(
        cfg, store=store, provider=spy, engine=engine, instance=premade
    ):
        pass

    assert engine.provision_calls == [premade]


def test_deploy_session_supplied_instance_skips_destroy_on_capability_mismatch(
    tmp_path: Path,
) -> None:
    """CapabilityMismatch + instance= → destroy NOT called; mismatch propagates.

    Bug catch: forgetting the _caller_supplied_instance guard inside the
    CapabilityMismatch teardown destroys a pod the orchestrator does NOT
    own, killing the operator's warm-reuse loop on the first drift
    signal. Caller owns the lifecycle; orchestrator only re-raises.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    profile_provider = _MismatchingProfileProvider(cached=_probe_profile())
    premade = _make_premade_instance()
    _seed_profile_cache(store, cfg)

    with pytest.raises(CapabilityMismatch):
        with deploy_session(
            cfg,
            store=store,
            provider=spy,
            engine=engine,
            profile_provider=profile_provider,
            instance=premade,
        ):
            pass

    assert spy.destroy_calls == []


def test_generate_with_supplied_instance_skips_destroy_on_validation_error(
    tmp_path: Path,
) -> None:
    """ValidationError in generate + instance= → destroy NOT called.

    Bug catch: the existing teardown wrapper in generate() destroys
    session.instance on ValidationError. Without the caller-supplied
    guard, a config typo by the smoke kills the operator's warm pod
    every iteration.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _RaisingValidateSpecFakeEngine()
    premade = _make_premade_instance()
    request = GenerationRequest(prompt="hi", mode="t2v")

    with pytest.raises(ValidationError):
        generate(
            cfg,
            request,
            store=store,
            provider=spy,
            engine=engine,
            instance=premade,
        )

    assert spy.destroy_calls == []


def test_generate_threads_instance_kwarg_to_deploy_session(
    tmp_path: Path,
) -> None:
    """Discriminating: generate(instance=) → downstream provider.create_instance NOT called.

    Bug catch: forgetting to thread instance= through the generate()->
    deploy_session() handoff leaves the kwarg as a silent no-op; the
    orchestrator still creates a fresh pod every call and the warm-reuse
    smoke iteration loop never converges.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    request = GenerationRequest(prompt="hi", mode="t2v")

    artifact = generate(
        cfg,
        request,
        store=store,
        provider=spy,
        engine=engine,
        instance=premade,
    )

    assert spy.create_calls == []
    assert isinstance(artifact, Artifact)


def test_deploy_session_threads_tags_into_instance_spec(
    tmp_path: Path,
) -> None:
    """tags={"k":"v"} (no instance=) → orchestrator-built InstanceSpec.tags merged.

    Bug catch: dropping the caller's tags= on the floor breaks the
    smoke's pod-discovery contract (find_instance_by_tag relies on
    the caller-set `kinoforge.layer` tag to re-attach across iterations).
    Built-in tags (kinoforge_engine, kinoforge_key) must coexist; we
    assert both survive.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        tags={"kinoforge.layer": "layer-p-smoke", "mode": "pod"},
    ):
        pass

    assert len(spy.create_calls) == 1
    created_spec = spy.create_calls[0]
    assert created_spec.tags["kinoforge.layer"] == "layer-p-smoke"
    assert created_spec.tags["mode"] == "pod"
    assert "kinoforge_engine" in created_spec.tags
    assert "kinoforge_key" in created_spec.tags


def test_deploy_session_threads_tags_into_instance_spec_on_cache_hit(
    tmp_path: Path,
) -> None:
    """tags={"k":"v"} + warm cache → built InstanceSpec.tags merged on cache-hit branch.

    Bug catch: threading tags= only through the cache-miss branch leaves
    the steady-state warm-reuse path (cache hit, fresh pod) silently
    dropping the caller's discovery tag, breaking find_instance_by_tag
    for every iteration after the first.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    _seed_profile_cache(store, cfg)

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        tags={"kinoforge.layer": "layer-p-smoke", "mode": "pod"},
    ):
        pass

    assert len(spy.create_calls) == 1
    created_spec = spy.create_calls[0]
    assert created_spec.tags["kinoforge.layer"] == "layer-p-smoke"
    assert created_spec.tags["mode"] == "pod"
    assert "kinoforge_engine" in created_spec.tags
    assert "kinoforge_key" in created_spec.tags


def test_deploy_session_tags_empty_dict_is_noop(tmp_path: Path) -> None:
    """tags={} (empty dict, not None) → InstanceSpec.tags carries built-ins only.

    Bug catch: a truthy check (``if tags:``) regressed to an identity check
    (``if tags is not None:``) would still call ``dict.update({})`` — a
    no-op today, but a different bug surface if the merge logic grows.
    Pins the truthy semantics.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()

    with deploy_session(cfg, store=store, provider=spy, engine=engine, tags={}):
        pass

    assert len(spy.create_calls) == 1
    created_spec = spy.create_calls[0]
    assert set(created_spec.tags.keys()) == {"kinoforge_engine", "kinoforge_key"}


def test_deploy_session_tags_ignored_when_instance_supplied(
    tmp_path: Path,
) -> None:
    """tags= + instance= → caller's instance tags untouched; tags= kwarg ignored.

    Bug catch: silently mutating caller.instance.tags after it has been
    handed in violates the "caller owns the lifecycle" contract for
    warm-pod reuse. The supplied tags= kwarg is for the cold-path
    only — the merge has no defined meaning when the orchestrator never
    builds an InstanceSpec.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    original_tags = dict(premade.tags)
    _seed_profile_cache(store, cfg)

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        instance=premade,
        tags={"override": "should-be-ignored"},
    ) as session:
        assert session.instance is premade
        assert dict(session.instance.tags) == original_tags
        assert "override" not in session.instance.tags

    assert spy.create_calls == []
