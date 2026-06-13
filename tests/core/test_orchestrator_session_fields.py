"""B3 Task b — deploy_session writes session_start / session_end via Ledger.touch."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import kinoforge.engines.fake  # noqa: F401 — registers fake engine
import kinoforge.providers.local  # noqa: F401 — registers local provider
import kinoforge.sources.http  # noqa: F401 — registers https:// source
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


class _IDCaptureProvider(LocalProvider):
    """LocalProvider that records every created instance to a list."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[Instance] = []

    def create_instance(self, spec: InstanceSpec) -> Instance:
        inst = super().create_instance(spec)
        self.created.append(inst)
        return inst


# ---------------------------------------------------------------------------
# session_start writes
# ---------------------------------------------------------------------------


def test_deploy_session_writes_session_start_after_hb_start(tmp_path: Path) -> None:
    """Bug: missing session_start write would leave busy-detection blind.

    Cross-CLI scanners would never see this CLI's claim and could
    double-attach.
    """
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = _IDCaptureProvider()
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
        # Spy.start() pre-records the instance; B3 then touches session_start.
        entry = Ledger(store=store).read(provider.created[0].id)
        assert entry is not None
        assert entry.get("session_start") is not None


def test_deploy_session_session_start_absent_when_hb_disabled(tmp_path: Path) -> None:
    """Bug: writing session_start without HB would create permanently-busy
    entries (no freshness gate → never clears)."""
    cfg = _compute_cfg()  # heartbeat_interval_s default None
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    # Pre-record so we can detect any spurious session_start write.
    ledger = Ledger(store=store)

    class _RecordOnCreate(_IDCaptureProvider):
        def create_instance(self, spec: InstanceSpec) -> Instance:
            inst = super().create_instance(spec)
            ledger.record(inst)
            return inst

    provider2 = _RecordOnCreate()
    with deploy_session(
        cfg,
        store=store,
        provider=provider2,
        engine=engine,
    ):
        pass
    entry = ledger.read(provider2.created[0].id)
    assert entry is not None
    assert entry.get("session_start") is None


def test_deploy_session_session_start_absent_on_hosted_engine_path(
    tmp_path: Path,
) -> None:
    """Bug: writing session_start on hosted path crashes (no instance.id)."""
    cfg = _compute_cfg()
    engine = HostedFakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
    )
    store = LocalArtifactStore(tmp_path)
    with deploy_session(cfg, store=store, engine=engine, run_id="r") as session:
        assert session.instance is None
    # No ledger entry, no crash, no session_start anywhere.
    assert Ledger(store=store).entries() == []


# ---------------------------------------------------------------------------
# session_end writes
# ---------------------------------------------------------------------------


def test_deploy_session_writes_session_end_in_finally(tmp_path: Path) -> None:
    """Bug: missing session_end write would leave entries marked busy forever."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = _IDCaptureProvider()
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
    entry = Ledger(store=store).read(provider.created[0].id)
    assert entry is not None
    assert entry.get("session_end") is not None
    assert entry["session_end"] >= entry["session_start"]


def test_session_end_written_even_on_exception_in_yielded_block(
    tmp_path: Path,
) -> None:
    """Bug: yielded-body exception bypassing session_end leaves entries busy-pinned."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = _IDCaptureProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            heartbeat_loop_factory=factory,
        ):
            raise _Boom("inject")
    entry = Ledger(store=store).read(provider.created[0].id)
    assert entry is not None
    assert entry.get("session_end") is not None


def test_session_end_touch_failure_logs_warning_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug: ledger.touch raising at __exit__ would abort the finally block,
    leaking pool / hb_loop resources."""
    cfg = _compute_cfg_hb(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = _IDCaptureProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    original_touch = Ledger.touch

    def flaky_touch(self: Ledger, instance_id: str, **kwargs: Any) -> bool:
        if "session_end" in kwargs:
            raise OSError("simulated cloud-store transient")
        return original_touch(self, instance_id, **kwargs)

    monkeypatch.setattr(Ledger, "touch", flaky_touch)
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.orchestrator"):
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            heartbeat_loop_factory=factory,
        ):
            pass
    assert any(
        "session_end" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )
