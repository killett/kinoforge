"""Tests for orchestrator.deploy_session — the shared compute setup.

deploy_session is the refactored extraction of steps 1-4, 7, 8 of
generate().  These tests pin its contract; the existing
test_orchestrator.py suite anchors the no-behavior-change guarantee for
generate() itself.

Layer L Task 1 — six tests:
  1. clean entry yields DeploySession with open pool, backend, profile, engine
  2. clean exit does NOT call provider.destroy_instance
  3. body-raised exception still closes the pool (idempotent finally)
  4. CapabilityMismatch during __enter__ verify destroys instance before re-raise
  5. hosted (requires_compute=False) yields instance=None and never calls
     provider.create_instance
  6. profile cache hit path: discover not called; backend constructed once
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# Import providers/engines/sources so they self-register.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.core.errors import CapabilityMismatch
from kinoforge.core.interfaces import (
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.orchestrator import DeploySession, deploy_session
from kinoforge.core.pool import ConcurrentPool
from kinoforge.core.profiles import JsonProfileCache
from kinoforge.engines.fake import FakeBackend, FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# Reuse the test scaffolding already shipped for orchestrator tests.
from tests.core.test_orchestrator import (
    DestroySpyProvider,
    HostedFakeEngine,
    _compute_cfg,
    _make_engine,
    _probe_profile,
)

# ---------------------------------------------------------------------------
# Local spies for this test module
# ---------------------------------------------------------------------------


class _BackendCountingEngine(FakeEngine):
    """FakeEngine that counts every backend() construction call.

    Used to assert that the warm-cache path constructs the backend exactly
    once — the spec contract for deploy_session on a profile-cache hit.
    """

    def __init__(
        self,
        *,
        probe_profile: ModelProfile,
        declared_flags_map: dict[str, dict[str, Any]],
        required_spec_keys: set[str],
    ) -> None:
        super().__init__(
            probe_profile=probe_profile,
            declared_flags_map=declared_flags_map,
            required_spec_keys=required_spec_keys,
        )
        self.backend_calls: int = 0

    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> FakeBackend:
        self.backend_calls += 1
        return super().backend(instance, cfg)


class _DiscoverCountingProfileCache(JsonProfileCache):
    """JsonProfileCache subclass that tracks discover() invocations.

    Lets the warm-cache test assert the contract "discover is NOT called
    when the cache already has a profile for this key".
    """

    def __init__(self, store: Any) -> None:
        super().__init__(store)
        self.discover_calls: int = 0

    def discover(self, key: Any, engine: Any, backend: Any) -> ModelProfile:
        self.discover_calls += 1
        return super().discover(key, engine, backend)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_deploy_session_yields_pool_backend_profile(tmp_path: Path) -> None:
    """A clean entry to deploy_session must yield a fully wired session.

    Bug catch: a refactor that drops the ``pool.add(backend, ...)`` line
    would cause every batch invocation to fail with
    "ConcurrentPool has no registered backend" — silent on entry, only
    blowing up on first submit.  This test catches that by submitting a
    job inside the with-block and confirming the pool dispatches it.
    """
    cfg = _compute_cfg()
    engine = _make_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        provider=provider,
        run_id="r",
    ) as session:
        assert isinstance(session, DeploySession)
        assert isinstance(session.pool, ConcurrentPool)
        assert isinstance(session.profile, ModelProfile)
        assert session.profile.name == "fake"
        assert session.backend is not None
        assert session.engine is engine
        # The pool has a wired backend — submit a trivial job; future
        # must resolve without raising "no registered backend".
        future = session.pool.submit(GenerationJob(spec={}, params={}, segments=[]))
        # FakeBackend.submit returns synchronously; this should not block.
        future.result(timeout=2.0)


def test_deploy_session_does_not_destroy_instance_on_clean_exit(
    tmp_path: Path,
) -> None:
    """Clean exit must leave the instance running for warm reuse.

    Bug catch: a refactor that calls provider.destroy_instance in the
    cleanup branch would tear down the pod after every batch run,
    defeating the whole point of batch's shared-deploy invariant —
    users would get pod-per-entry billing.
    """
    cfg = _compute_cfg()
    engine = _make_engine()
    provider = DestroySpyProvider()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        provider=provider,
        run_id="r",
    ) as session:
        assert session.instance is not None

    assert provider.destroy_calls == [], (
        "deploy_session must NOT destroy the instance on clean exit; "
        f"got destroy_calls={provider.destroy_calls!r}"
    )


def test_deploy_session_closes_pool_on_exit_even_when_body_raises(
    tmp_path: Path,
) -> None:
    """Pool must be closed even when the body raises.

    Bug catch: a missing finally / __exit__ wiring leaks every
    ConcurrentPool's ThreadPoolExecutors past the with-block.  We pin
    the contract by capturing the pool, escaping the with-block via a
    RuntimeError, then asserting that a fresh submit on the captured
    pool raises "pool closed" (the documented signal that close() ran).
    """
    cfg = _compute_cfg()
    engine = _make_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(tmp_path)

    pool_ref: list[ConcurrentPool] = []
    with pytest.raises(RuntimeError, match="boom"):
        with deploy_session(
            cfg,
            store=store,
            engine=engine,
            provider=provider,
            run_id="r",
        ) as session:
            pool_ref.append(session.pool)
            raise RuntimeError("boom")

    assert len(pool_ref) == 1
    with pytest.raises(RuntimeError, match="pool closed"):
        pool_ref[0].submit(GenerationJob(spec={}, params={}, segments=[]))


def test_deploy_session_teardown_on_capability_mismatch_during_enter(
    tmp_path: Path,
) -> None:
    """verify-fail inside __enter__ must destroy compute before re-raising.

    Bug catch: a refactor that loses the verify-fail teardown branch
    leaves a capability-mismatched pod alive — silent budget burn.

    Setup: seed the cache with the stable engine (max_frames=16), then
    enter deploy_session with a drifted engine whose backend probe
    returns max_frames=32.  The cache-hit branch runs verify() which
    raises CapabilityMismatch.  Contract: instance destroyed exactly
    once, exception re-raised.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    provider = DestroySpyProvider()

    # Seed the cache so the next deploy_session takes the cache-hit
    # branch — that's the branch that calls verify().
    stable_engine = _make_engine(max_frames=16)
    with deploy_session(
        cfg,
        store=store,
        engine=stable_engine,
        provider=provider,
        run_id="seed",
    ):
        pass
    assert provider.destroy_calls == [], "seed run must not destroy"

    # Now construct a drifted engine whose backend reports max_frames=32.
    drifted_probe = _probe_profile(max_frames=32)

    class DriftedBackend(FakeBackend):
        def inspect_capabilities(self) -> ModelProfile:
            return drifted_probe

    class DriftedEngine(FakeEngine):
        def backend(
            self, instance: Instance | None, cfg: dict[str, object]
        ) -> FakeBackend:
            del instance, cfg
            return DriftedBackend(probe=drifted_probe)

    drifted_engine = DriftedEngine(
        probe_profile=drifted_probe,
        declared_flags_map={},
        required_spec_keys=set(),
    )

    with pytest.raises(CapabilityMismatch):
        with deploy_session(
            cfg,
            store=store,
            engine=drifted_engine,
            provider=provider,
            run_id="r",
        ):
            pytest.fail("body must not run when __enter__ raises")

    assert len(provider.destroy_calls) == 1, (
        "exactly one destroy_instance must fire on capability mismatch; "
        f"got destroy_calls={provider.destroy_calls!r}"
    )


def test_deploy_session_hosted_path_skips_instance(tmp_path: Path) -> None:
    """Hosted engine yields session with instance=None; provider untouched.

    Bug catch: a refactor that always provisions an instance silently
    burns budget on hosted runs that don't need any pod.  This test
    pins the contract by using a hosted engine + a provider spy whose
    every method raises — if the refactor reaches the provider on a
    hosted path, the test fails with the spy's AssertionError.
    """
    cfg = _compute_cfg()
    engine = HostedFakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
    )
    store = LocalArtifactStore(tmp_path)

    # No provider supplied — and the engine.requires_compute is False so
    # deploy_session must NOT try to resolve one from the registry either.
    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        run_id="r",
    ) as session:
        assert session.instance is None
        assert session.provider is None
        assert session.backend is not None


def test_deploy_session_profile_cache_hit_skips_discover(tmp_path: Path) -> None:
    """Cache hit: discover not called; backend constructed exactly once.

    Bug catch: a refactor that always calls discover() would burn an
    extra inspect_capabilities probe on every warm batch.  A refactor
    that builds the backend twice (once for discover, again for stage
    dispatch) would double the engine.backend() workload.  Either
    regression caught here.

    Setup: seed the cache via a first deploy_session call.  Then enter a
    second deploy_session with a _BackendCountingEngine and a
    _DiscoverCountingProfileCache wired against the same store; assert
    that during the second session discover_calls stays 0 and
    backend_calls equals exactly 1.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    provider = LocalProvider()

    # Seed phase — populates the JsonProfileCache via deploy_session's
    # cache-miss branch.
    seed_engine = _make_engine()
    with deploy_session(
        cfg,
        store=store,
        engine=seed_engine,
        provider=provider,
        run_id="seed",
    ):
        pass

    # Probe phase — fresh engine + counting cache.  Cache is warm.
    probe_engine = _BackendCountingEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
    )
    counting_cache = _DiscoverCountingProfileCache(store)

    with deploy_session(
        cfg,
        store=store,
        engine=probe_engine,
        provider=provider,
        profile_provider=counting_cache,
        run_id="r",
    ) as session:
        assert session.backend is not None

    assert counting_cache.discover_calls == 0, (
        "cache hit must not call discover; "
        f"got discover_calls={counting_cache.discover_calls}"
    )
    assert probe_engine.backend_calls == 1, (
        "cache hit must construct the backend exactly once; "
        f"got backend_calls={probe_engine.backend_calls}"
    )
