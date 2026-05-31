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

# Import providers/engines so they self-register
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
from kinoforge.core.config import Config
from kinoforge.core.errors import CapabilityMismatch
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationJob,
    GenerationRequest,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    ModelProfile,
    Offer,
    Segment,
)
from kinoforge.core.orchestrator import DeployResult, deploy, generate
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
  - ref: "fake://base"
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
  - ref: "fake://base"
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
