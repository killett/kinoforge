"""B3 Task d — --no-reuse semantics: cold create + ephemeral destroy at __exit__."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import TeardownError
from kinoforge.core.interfaces import Instance, InstanceSpec
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import deploy_session
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore
from tests.core.test_orchestrator import (
    HostedFakeEngine,
    _compute_cfg,
    _make_engine,
    _probe_profile,
)
from tests.core.test_orchestrator_heartbeat import (
    _compute_cfg as _compute_cfg_hb,
)
from tests.core.test_orchestrator_heartbeat import (
    _seed_profile_cache,
    _SpyFactory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DestroyTrackingProvider(LocalProvider):
    """LocalProvider subclass that records destroyed instance ids + created."""

    def __init__(self, *, fail_destroy_with: Exception | None = None) -> None:
        super().__init__()
        self.created: list[Instance] = []
        self.destroyed_ids: list[str] = []
        self._fail_destroy_with = fail_destroy_with

    def create_instance(self, spec: InstanceSpec) -> Instance:
        inst = super().create_instance(spec)
        self.created.append(inst)
        return inst

    def destroy_instance(self, instance_id: str) -> None:
        self.destroyed_ids.append(instance_id)
        if self._fail_destroy_with is not None:
            raise self._fail_destroy_with
        super().destroy_instance(instance_id)


# ---------------------------------------------------------------------------
# AC: destroy fires at exit + ledger forgotten
# ---------------------------------------------------------------------------


def test_no_reuse_destroys_pod_at_exit(tmp_path: Path) -> None:
    """Bug: not destroying on --no-reuse would leak ephemeral pods forever."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = _DestroyTrackingProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
        single=True,
    ):
        pass
    assert provider.destroyed_ids == [provider.created[0].id]


def test_no_reuse_forgets_ledger_after_destroy(tmp_path: Path) -> None:
    """Bug: ledger entry surviving destroy would mislead next B3 scan."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = _DestroyTrackingProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
        single=True,
    ):
        pass
    assert Ledger(store=store).read(provider.created[0].id) is None


# ---------------------------------------------------------------------------
# AC: reaper lock held during destroy
# ---------------------------------------------------------------------------


def test_no_reuse_acquires_reaper_lock_during_destroy(tmp_path: Path) -> None:
    """Bug: not holding reaper:<id> would let concurrent B3 scans attach
    mid-destroy."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()
    held_during_destroy: list[bool] = []

    class _ProbeProvider(_DestroyTrackingProvider):
        def destroy_instance(self, instance_id: str) -> None:
            # Probe reaper:<id> from a separate fd in the same process.
            # fcntl is per open file description, so a separate open()
            # raises BlockingIOError when LOCK_EX is already held → probe
            # returns True ("held").
            from kinoforge.cli._commands import _probe_lock_held

            held_during_destroy.append(_probe_lock_held(store, f"reaper/{instance_id}"))
            super().destroy_instance(instance_id)

    provider = _ProbeProvider()
    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
        single=True,
    ):
        pass
    assert held_during_destroy == [True]


# ---------------------------------------------------------------------------
# AC: D7 — destroy fires even for caller-supplied instance
# ---------------------------------------------------------------------------


def test_no_reuse_destroys_even_when_caller_supplied_instance(tmp_path: Path) -> None:
    """Bug: respecting _caller_supplied_instance on --no-reuse would defeat D7
    composition (operator wants attach + destroy)."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    # Caller pre-creates the instance via the provider.
    provider = _DestroyTrackingProvider()
    caller_instance = provider.create_instance(
        InstanceSpec(image="x", run_id="r", tags={"kinoforge_engine": "fake"})
    )

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        instance=caller_instance,
        heartbeat_loop_factory=factory,
        single=True,
    ):
        pass
    assert caller_instance.id in provider.destroyed_ids


# ---------------------------------------------------------------------------
# AC: TeardownError → log ERROR, ledger preserved
# ---------------------------------------------------------------------------


def test_no_reuse_destroy_failure_logs_error_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bug: raising on destroy failure would break clean shutdown of pool / hb."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()
    provider = _DestroyTrackingProvider(fail_destroy_with=TeardownError("transient"))

    with caplog.at_level(logging.ERROR, logger="kinoforge.core.orchestrator"):
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            heartbeat_loop_factory=factory,
            single=True,
        ):
            pass
    assert any("--no-reuse destroy failed" in r.getMessage() for r in caplog.records)


def test_no_reuse_TeardownError_preserves_ledger_entry_for_reap_recovery(
    tmp_path: Path,
) -> None:
    """Bug: forgetting ledger after destroy failure would lose recovery handle."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()
    provider = _DestroyTrackingProvider(fail_destroy_with=TeardownError("transient"))

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
        single=True,
    ):
        pass
    # Spy ledger-record happens in _SpyHeartbeatLoop.start(); destroy failure
    # short-circuits before ledger.forget → entry survives.
    assert Ledger(store=store).read(provider.created[0].id) is not None


# ---------------------------------------------------------------------------
# AC: hosted path is no-op
# ---------------------------------------------------------------------------


def test_no_reuse_skips_destroy_on_hosted_engine_path(tmp_path: Path) -> None:
    """Bug: attempting destroy on hosted (None instance + None provider) would crash."""
    cfg = _compute_cfg()
    engine = HostedFakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
    )
    store = LocalArtifactStore(tmp_path)
    with deploy_session(cfg, store=store, engine=engine, single=True):
        pass  # no crash


# ---------------------------------------------------------------------------
# AC: session_end written before destroy
# ---------------------------------------------------------------------------


def test_no_reuse_writes_session_end_before_destroy(tmp_path: Path) -> None:
    """Bug: writing session_end after destroy would race a concurrent classify
    that sees STALE_LEDGER on a still-busy entry."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()
    seen_session_end: list[bool] = []

    class _SessionEndProbeProvider(_DestroyTrackingProvider):
        def destroy_instance(self, instance_id: str) -> None:
            entry = Ledger(store=store).read(instance_id)
            seen_session_end.append(
                entry is not None and entry.get("session_end") is not None
            )
            super().destroy_instance(instance_id)

    provider = _SessionEndProbeProvider()
    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
        single=True,
    ):
        pass
    assert seen_session_end == [True]


# ---------------------------------------------------------------------------
# AC: single=False (default) → no destroy
# ---------------------------------------------------------------------------


def test_single_false_default_does_not_destroy(tmp_path: Path) -> None:
    """Bug: defaulting single=True would break warm-reuse for every existing caller."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = _DestroyTrackingProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
    ):
        pass
    assert provider.destroyed_ids == []


# ---------------------------------------------------------------------------
# Spec A2 fix round 1: an unconfirmed destroy must be visible to the CLI.
# ---------------------------------------------------------------------------


def test_failed_no_reuse_destroy_is_recorded_on_the_ephemeral_session(
    tmp_path: Path,
) -> None:
    """The swallowed ``TeardownError`` must leave a trace the caller can read.

    Bug caught: the teardown logs "use `kinoforge reap --apply` to recover",
    swallows the error and returns normally, and the return tuple is already
    fixed by then — so ``_cmd_generate`` cannot tell a clean ``--no-reuse``
    teardown from a failed one. It then releases the ephemeral index row, which
    is the last durable trace of a pod that is still billing. The mark on the
    session is the only channel between the two, and it must be set for exactly
    the pod that survived.
    """
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()
    provider = _DestroyTrackingProvider(fail_destroy_with=TeardownError("transient"))

    with EphemeralSession(enabled=True) as session:
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            heartbeat_loop_factory=factory,
            single=True,
        ):
            pass
        pod_id = provider.created[0].id
        assert session.destroy_was_confirmed(pod_id) is False, (
            f"the destroy of {pod_id} raised TeardownError and was swallowed, "
            "but the session still reports it confirmed — the CLI will delete "
            "the last record of a live pod"
        )
        assert session.destroy_was_confirmed("some-other-pod") is True


def test_successful_no_reuse_destroy_leaves_the_session_clean(tmp_path: Path) -> None:
    """Bug caught: marking every ``--no-reuse`` teardown unconfirmed, which
    would keep an index row for a pod that is definitively gone and send the
    next ``--ephemeral`` run's matcher at a corpse."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()
    provider = _DestroyTrackingProvider()

    with EphemeralSession(enabled=True) as session:
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            heartbeat_loop_factory=factory,
            single=True,
        ):
            pass
        assert session.destroy_was_confirmed(provider.created[0].id) is True
