"""Tests for UX A compute-path preflight in ``orchestrator.generate()`` (Layer I Task 9).

Acceptance Criteria:
  AC1: First generate() against a fresh instance writes the provision marker
       AND calls ``provisioner.provision`` exactly once.
  AC2: Second generate() against the same instance + same capability_key
       reads the marker and does NOT re-call ``provisioner.provision``.
  AC3: Second generate() against the same instance with a DIFFERENT
       capability_key re-calls ``provisioner.provision`` and rewrites the marker.
  AC4: Two concurrent generate() calls against the same instance + key serialize
       via ``store.acquire_lock``; ``provisioner.provision`` is called exactly
       once across both threads.

Local fakes are defined inline below to keep ``test_orchestrator.py`` untouched.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

# Import providers/engines so they self-register
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 -- registers https:// source
from kinoforge.core.config import Config, load_config
from kinoforge.core.interfaces import (
    GenerationRequest,
    Instance,
    InstanceSpec,
    ModelProfile,
)
from kinoforge.core.locks import InMemoryLock, Lock
from kinoforge.core.orchestrator import generate
from kinoforge.core.provision_state import marker_path, read_marker
from kinoforge.engines.fake import FakeBackend, FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Test config helpers
# ---------------------------------------------------------------------------

# fp16 precision — used as the "current" key in AC1, AC2, AC4
_COMPUTE_YAML_FP16 = """\
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

# fp32 precision — derives a DIFFERENT capability_key for the AC3 stale-key path
_COMPUTE_YAML_FP32 = """\
engine:
  kind: fake
  precision: fp32
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


def _cfg_fp16() -> Config:
    return load_config(_COMPUTE_YAML_FP16)


def _cfg_fp32() -> Config:
    return load_config(_COMPUTE_YAML_FP32)


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


# ---------------------------------------------------------------------------
# Local fakes
# ---------------------------------------------------------------------------


class CountingFakeEngine(FakeEngine):
    """FakeEngine that records every ``engine.provision`` call.

    ``provisioner.provision`` (the core helper) calls ``engine.provision`` last;
    counting it here lets us verify the preflight gate without monkey-patching.

    ``requires_local_weights`` stays False so the inner downloader is never
    invoked (the test config's ``https://`` ref is parsed but not downloaded).
    """

    requires_compute: bool = True
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        probe_profile: ModelProfile,
        provision_delay_s: float = 0.0,
    ) -> None:
        super().__init__(
            probe_profile=probe_profile,
            declared_flags_map={},
            required_spec_keys=set(),
        )
        self.provision_call_count: int = 0
        self._provision_delay_s = provision_delay_s
        # Share one backend across discover-path and pool-path calls.
        self._shared_backend = FakeBackend(probe=probe_profile)

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        # Sleep first, then increment, so two threads racing into provision
        # before either finishes are observable via the count.
        if self._provision_delay_s > 0:
            time.sleep(self._provision_delay_s)
        self.provision_call_count += 1

    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> FakeBackend:
        return self._shared_backend


class StableInstanceProvider(LocalProvider):
    """LocalProvider that returns the SAME instance ID on every create_instance.

    Real ``LocalProvider`` mints a fresh UUID each call; for AC2/AC3/AC4 the
    second call must reuse the first instance so the marker keys line up.
    """

    def __init__(self, *, instance_id: str = "local-stable-0001") -> None:
        super().__init__()
        self._stable_id = instance_id

    def create_instance(self, spec: InstanceSpec) -> Instance:
        # Return the same instance every time so AC2/AC3/AC4 share a marker.
        existing = self._instances.get(self._stable_id)
        if existing is not None:
            return existing
        instance = Instance(
            id=self._stable_id,
            provider=self.name,
            status="ready",
            created_at=self._clock.now(),
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=0.0,
        )
        self._instances[self._stable_id] = instance
        return instance


class _LockingStore(LocalArtifactStore):
    """LocalArtifactStore whose acquire_lock uses an in-process Lock.

    The default ``FileLock`` is backed by ``fcntl.flock`` which is per-process,
    not per-thread — two threads in the same process can both acquire it
    simultaneously, defeating the AC4 contention check. Swap to an
    ``InMemoryLock`` keyed by name so threads serialise correctly.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        # Shared registry of active leases keyed by lock name.
        self._lock_registry: dict[str, dict[str, float | str]] = {}
        # Count acquire_lock invocations so tests can sanity-check the lock path.
        self.acquire_lock_call_count: int = 0

    def acquire_lock(self, key: str, *, ttl_s: float) -> Lock:
        self.acquire_lock_call_count += 1
        return InMemoryLock(
            key=key,
            ttl_s=ttl_s,
            registry=self._lock_registry,
            poll_interval_s=0.01,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputePreflight:
    def test_first_generate_writes_marker_and_calls_provisioner(
        self, tmp_path: Path
    ) -> None:
        """AC1: First generate() against a fresh instance writes the marker and
        calls ``provisioner.provision`` exactly once.

        Bug catch: pre-Task-9 the compute path never called
        ``provisioner.provision`` from ``generate()``, so the marker file would
        not exist after a successful generate.
        """
        cfg = _cfg_fp16()
        engine = CountingFakeEngine(probe_profile=_probe_profile())
        provider = StableInstanceProvider()
        store = _LockingStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
            state_dir=tmp_path,
        )

        assert engine.provision_call_count == 1, (
            f"expected exactly one engine.provision call after the first "
            f"generate, got {engine.provision_call_count}"
        )
        marker = marker_path(tmp_path, provider._stable_id)
        assert marker.exists(), (
            f"provision marker {marker!s} was not written by generate()"
        )
        record = read_marker(marker)
        assert record is not None, "marker file present but unreadable"
        assert record["instance_id"] == provider._stable_id
        assert record["capability_key"] == cfg.capability_key().derive()
        assert record["engine"] == engine.name

    def test_second_generate_same_key_skips_provision(self, tmp_path: Path) -> None:
        """AC2: Second generate() against the same instance + same key reads
        the marker and does NOT re-call ``provisioner.provision``.

        Bug catch: a stateless preflight that re-provisions on every generate
        would wastefully re-run engine.provision (and, for real engines, hit
        the network) on every clip in a long run.
        """
        cfg = _cfg_fp16()
        engine = CountingFakeEngine(probe_profile=_probe_profile())
        provider = StableInstanceProvider()
        store = _LockingStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
            state_dir=tmp_path,
        )
        assert engine.provision_call_count == 1

        generate(
            cfg,
            request,
            store=store,
            provider=provider,
            engine=engine,
            state_dir=tmp_path,
        )
        assert engine.provision_call_count == 1, (
            f"second generate() with the same capability_key must NOT re-invoke "
            f"engine.provision; got {engine.provision_call_count} total calls"
        )

    def test_second_generate_stale_key_reprovisions(self, tmp_path: Path) -> None:
        """AC3: Second generate() with a DIFFERENT capability_key re-provisions
        and rewrites the marker with the new key.

        Bug catch: a marker keyed by instance_id alone would never invalidate
        when the user edits precision/model set, leaving stale weights on a
        long-lived box.
        """
        cfg_a = _cfg_fp16()
        cfg_b = _cfg_fp32()
        # Sanity: the two configs MUST derive distinct capability keys for the
        # test to be meaningful.
        assert cfg_a.capability_key().derive() != cfg_b.capability_key().derive()

        engine = CountingFakeEngine(probe_profile=_probe_profile())
        provider = StableInstanceProvider()
        store = _LockingStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        generate(
            cfg_a,
            request,
            store=store,
            provider=provider,
            engine=engine,
            state_dir=tmp_path,
        )
        assert engine.provision_call_count == 1

        generate(
            cfg_b,
            request,
            store=store,
            provider=provider,
            engine=engine,
            state_dir=tmp_path,
        )
        assert engine.provision_call_count == 2, (
            f"second generate() with a NEW capability_key must re-invoke "
            f"engine.provision; got {engine.provision_call_count} total calls"
        )

        record = read_marker(marker_path(tmp_path, provider._stable_id))
        assert record is not None
        assert record["capability_key"] == cfg_b.capability_key().derive(), (
            "marker was not rewritten with the new capability_key after "
            "stale-key re-provision"
        )

    def test_concurrent_generates_serialize_via_lock(self, tmp_path: Path) -> None:
        """AC4: Two concurrent generate() threads serialize via
        ``store.acquire_lock("provision:<instance_id>", ...)``; the provisioner
        runs exactly once across both threads.

        Bug catch: without acquire_lock, both threads would see "no marker"
        simultaneously, both enter provision, and ``provision_call_count``
        would be 2.  The injected ``provision_delay_s=0.1`` widens the race
        window so an unlocked path is reliably observable.
        """
        cfg = _cfg_fp16()
        engine = CountingFakeEngine(
            probe_profile=_probe_profile(),
            provision_delay_s=0.1,
        )
        provider = StableInstanceProvider()
        store = _LockingStore(tmp_path)
        request = GenerationRequest(prompt="a sunset", mode="t2v")

        errors: list[BaseException] = []

        def run_one() -> None:
            try:
                generate(
                    cfg,
                    request,
                    store=store,
                    provider=provider,
                    engine=engine,
                    state_dir=tmp_path,
                )
            except BaseException as exc:  # pragma: no cover -- surfaced by assert
                errors.append(exc)

        t1 = threading.Thread(target=run_one)
        t2 = threading.Thread(target=run_one)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert not t1.is_alive() and not t2.is_alive(), (
            "concurrent generate() threads did not finish within 10s — "
            "likely a deadlock in the preflight lock path"
        )
        assert not errors, f"concurrent generate raised: {errors!r}"
        assert engine.provision_call_count == 1, (
            f"concurrent generate() with the same key must lock and run "
            f"engine.provision exactly once; got "
            f"{engine.provision_call_count} calls"
        )
        # And the lock acquisition path must have been exercised at least twice
        # (once per thread) — if the helper bypassed acquire_lock entirely, the
        # provision_call_count check above would still pass on a cooperative
        # scheduler so we pin the lock path explicitly here.
        assert store.acquire_lock_call_count >= 2, (
            f"expected acquire_lock to be called at least twice across two "
            f"threads, got {store.acquire_lock_call_count}"
        )
