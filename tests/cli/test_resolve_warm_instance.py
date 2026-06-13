"""B4 — unit tests for `_resolve_warm_instance` helper."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from kinoforge.cli._commands import _resolve_warm_instance
from kinoforge.cli.context import SessionContext
from kinoforge.core.interfaces import Instance

_NOW = 1_700_000_000.0


def _entry(
    *,
    eid: str = "i-1",
    provider: str = "local",
    cap_key: str | None = "ab12cd34ef56",
    created_at: float = _NOW - 60.0,
    last_heartbeat: float | None = _NOW - 5.0,
    heartbeat_thread_tick: float | None = _NOW - 5.0,
    cost_rate: float = 1.0,
) -> dict[str, Any]:
    """Build a ledger entry shaped like the orchestrator persists today.

    cap_key lives at entry['tags']['kinoforge_key'] (orchestrator.py:492,1015).
    Default provider="local" so classify() Row-7 substrate gate resolves to
    HEARTBEAT_UNKNOWN (local is in _HEARTBEAT_SUPPORTED), not
    HEARTBEAT_SUBSTRATE_MISSING.
    """
    e: dict[str, Any] = {
        "id": eid,
        "provider": provider,
        "created_at": created_at,
        "cost_rate_usd_per_hr": cost_rate,
        "tags": {},
    }
    if cap_key is not None:
        e["tags"]["kinoforge_key"] = cap_key
    if last_heartbeat is not None:
        e["last_heartbeat"] = last_heartbeat
    if heartbeat_thread_tick is not None:
        e["heartbeat_thread_tick"] = heartbeat_thread_tick
    return e


class _Compute:
    """ComputeConfig stand-in carrying just the provider attr the helper reads."""

    def __init__(self, provider: str) -> None:
        self.provider = provider


class _FakeCfg:
    """Minimal Config stand-in: capability_key().derive() returns a fixed hash.

    Lifecycle() carries heartbeat_interval_s=30.0 so classify() can reach
    the IDLE_REAP / ORPHAN_REAP / LIVE rows; the production default of
    None short-circuits to HEARTBEAT_UNKNOWN and would mask those paths
    from these tests.
    """

    def __init__(
        self,
        *,
        provider: str = "local",
        cap_hash: str = "ab12cd34ef56XX",
    ) -> None:
        self._provider = provider
        self._cap_hash = cap_hash
        self.compute = _Compute(provider)

    def capability_key(self) -> Any:
        cap_hash = self._cap_hash

        class _CapKey:
            def derive(self) -> str:
                return cap_hash

        return _CapKey()

    def lifecycle(self) -> Any:
        from kinoforge.core.interfaces import Lifecycle

        return Lifecycle(heartbeat_interval_s=30.0)


def _fake_cfg(**kwargs: Any) -> Any:
    """Typed-as-Any factory so call sites don't trigger mypy arg-type errors."""
    return _FakeCfg(**kwargs)


class _FakeCtx:
    """SessionContext stand-in returning a MagicMock ledger with Ledger.read."""

    def __init__(self, entry: dict[str, Any] | None, cfg: Any) -> None:
        self.cfg = cfg
        self._ledger = MagicMock()
        self._ledger.read = MagicMock(return_value=entry)
        self._ledger.entries = MagicMock(
            return_value=([entry] if entry is not None else [])
        )

    def ledger(self) -> MagicMock:
        return self._ledger


def _ctx(entry: dict[str, Any] | None, cfg: Any) -> SessionContext:
    return cast("SessionContext", _FakeCtx(entry, cfg))


class _FakeProvider:
    """Provider stand-in supporting list_instances + get_instance."""

    def __init__(
        self,
        *,
        live_ids: set[str] | None = None,
        get_raises: Exception | None = None,
        list_raises: Exception | None = None,
        instance_obj: Instance | None = None,
    ) -> None:
        self._live_ids = live_ids if live_ids is not None else {"i-1"}
        self._get_raises = get_raises
        self._list_raises = list_raises
        self._instance_obj = instance_obj or Instance(
            id="i-1",
            provider="local",
            status="ready",
            created_at=_NOW - 60.0,
            endpoints={},
            tags={},
        )

    def list_instances(self) -> list[Instance]:
        if self._list_raises is not None:
            raise self._list_raises
        return [
            Instance(
                id=i,
                provider="local",
                status="ready",
                created_at=_NOW - 60.0,
                endpoints={},
                tags={},
            )
            for i in self._live_ids
        ]

    def get_instance(self, iid: str) -> Instance:
        if self._get_raises is not None:
            raise self._get_raises
        return self._instance_obj


@pytest.fixture
def patched_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch registry.get_provider to return a controllable factory.

    Tests mutate the returned dict's 'provider' key to swap behaviour.
    """
    state: dict[str, Any] = {"provider": _FakeProvider()}

    def _factory(name: str) -> Any:
        def _ctor() -> _FakeProvider:
            return state["provider"]

        return _ctor

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _factory)
    return state


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin time.time() inside _commands to a known wall clock."""
    monkeypatch.setattr("kinoforge.cli._commands.time.time", lambda: _NOW)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_returns_instance_on_happy_path(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ledger ok + cap_key match + LIVE verdict → returns the Instance."""
    cfg = _fake_cfg(cap_hash="ab12cd34ef56XX")
    entry = _entry(cap_key="ab12cd34ef56")
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert rc is None
    assert inst is not None
    assert inst.id == "i-1"


# ---------------------------------------------------------------------------
# Refusals (each step in order)
# ---------------------------------------------------------------------------


def test_returns_1_when_id_not_in_ledger(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg()
    ctx = _ctx(None, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-missing", force_attach=False)
    assert (inst, rc) == (None, 1)
    err = capsys.readouterr().err
    assert "not found in ledger" in err
    assert "kinoforge list" in err


def test_returns_2_on_provider_kind_mismatch(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg(provider="runpod")
    entry = _entry(provider="skypilot")
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "provider mismatch" in err
    assert "cfg=runpod" in err
    assert "skypilot" in err


def test_returns_2_on_capability_key_mismatch(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg(cap_hash="aaaaaaaaaaaaXX")
    entry = _entry(cap_key="bbbbbbbbbbbb")
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "capability_key mismatch" in err
    assert "aaaaaaaaaaaa" in err
    assert "bbbbbbbbbbbb" in err


def test_returns_2_when_entry_missing_capability_key_field(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Legacy ledger row lacking tags.kinoforge_key → mismatch with <unknown>."""
    cfg = _fake_cfg(cap_hash="ab12cd34ef56XX")
    entry = _entry(cap_key=None)
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "capability_key mismatch" in err
    assert "<unknown>" in err


def test_returns_2_on_provider_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kinoforge.core.errors import UnknownAdapter

    def _bad_factory(name: str) -> Any:
        def _ctor() -> Any:
            raise UnknownAdapter(f"unknown provider: {name}")

        return _ctor

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _bad_factory)
    cfg = _fake_cfg()
    entry = _entry()
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "unconstructable" in err


def test_returns_2_on_list_instances_failure(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patched_registry["provider"] = _FakeProvider(list_raises=RuntimeError("HTTP 500"))
    cfg = _fake_cfg()
    entry = _entry()
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "list_instances failed" in err


# ---------------------------------------------------------------------------
# Verdict gate (D3)
# ---------------------------------------------------------------------------


def test_returns_2_on_STALE_LEDGER_even_with_force(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pod not in live_ids → STALE_LEDGER → refuse even with --force-attach."""
    patched_registry["provider"] = _FakeProvider(live_ids=set())
    cfg = _fake_cfg()
    entry = _entry()
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=True)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "stale" in err.lower()


def test_returns_2_on_IDLE_REAP_without_force(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """last_heartbeat older than idle_timeout (default 7200s) → IDLE_REAP.

    Sentinel kept fresh so classify reaches the idle branch (Rows 3 & 4);
    sentinel-stale would route to ORPHAN_REAP instead.
    """
    cfg = _fake_cfg()
    entry = _entry(last_heartbeat=_NOW - 99999.0, heartbeat_thread_tick=_NOW - 5.0)
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "IDLE_REAP" in err
    assert "--force-attach" in err


def test_passes_on_IDLE_REAP_with_force_attach(
    patched_registry: dict[str, Any],
    fixed_clock: None,
) -> None:
    cfg = _fake_cfg()
    entry = _entry(last_heartbeat=_NOW - 99999.0, heartbeat_thread_tick=_NOW - 5.0)
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=True)
    assert rc is None and inst is not None


def test_returns_2_on_ORPHAN_REAP_without_force(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sentinel-stale beyond grace_after_session_s (default 300s) → ORPHAN_REAP."""
    cfg = _fake_cfg()
    entry = _entry(
        created_at=_NOW - 9999.0,
        last_heartbeat=_NOW - 9999.0,
        heartbeat_thread_tick=_NOW - 9999.0,
    )
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "ORPHAN_REAP" in err


def test_passes_on_ORPHAN_REAP_with_force_attach(
    patched_registry: dict[str, Any],
    fixed_clock: None,
) -> None:
    cfg = _fake_cfg()
    entry = _entry(
        created_at=_NOW - 9999.0,
        last_heartbeat=_NOW - 9999.0,
        heartbeat_thread_tick=_NOW - 9999.0,
    )
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=True)
    assert rc is None and inst is not None


def test_returns_2_on_HEARTBEAT_UNKNOWN_without_force(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No heartbeat_thread_tick → Row 7 → HEARTBEAT_UNKNOWN (provider=local OK)."""
    cfg = _fake_cfg()
    entry = _entry(heartbeat_thread_tick=None)
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "HEARTBEAT_UNKNOWN" in err


def test_passes_on_HEARTBEAT_UNKNOWN_with_force_attach(
    patched_registry: dict[str, Any],
    fixed_clock: None,
) -> None:
    cfg = _fake_cfg()
    entry = _entry(heartbeat_thread_tick=None)
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=True)
    assert rc is None and inst is not None


def test_returns_2_on_OVERAGE_REAP_even_with_force(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """pod_age > max_lifetime_s — never bypassable."""
    cfg = _fake_cfg()
    entry = _entry(created_at=_NOW - 999999.0)
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=True)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "OVERAGE_REAP" in err
    assert "max_lifetime" in err


def test_returns_2_when_get_instance_raises_keyerror(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LIVE classify but get_instance KeyError → raced concurrent destroy."""
    patched_registry["provider"] = _FakeProvider(get_raises=KeyError("i-1"))
    cfg = _fake_cfg()
    entry = _entry()
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert (inst, rc) == (None, 2)
    err = capsys.readouterr().err
    assert "disappeared" in err.lower() or "concurrent" in err.lower()


def test_short_circuits_on_first_failure(
    patched_registry: dict[str, Any],
    fixed_clock: None,
) -> None:
    """Cap_key mismatch → no list_instances RPC fires."""
    patched_registry["provider"] = _FakeProvider()
    spy = MagicMock(wraps=patched_registry["provider"].list_instances)
    patched_registry["provider"].list_instances = spy
    cfg = _fake_cfg(cap_hash="aaaaaaaaaaaaXX")
    entry = _entry(cap_key="bbbbbbbbbbbb")
    ctx = _ctx(entry, cfg)
    _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert spy.call_count == 0
