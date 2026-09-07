"""B3 Task c — `_scan_warm_candidates` auto-discovery scan."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from kinoforge.cli._commands import (
    _probe_lock_held,
    _scan_warm_candidates,
    _ScanReport,
)
from kinoforge.cli.context import SessionContext
from kinoforge.core.clock import FakeClock
from kinoforge.core.interfaces import Instance
from kinoforge.stores.local import LocalArtifactStore

_NOW = 1_700_000_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    *,
    eid: str,
    provider: str = "runpod",
    cap_key: str = "abc123abc123",
    created_at: float = _NOW - 60.0,
    last_heartbeat: float | None = _NOW - 5.0,
    heartbeat_thread_tick: float | None = _NOW - 5.0,
    session_start: float | None = None,
    session_end: float | None = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": eid,
        "provider": provider,
        "created_at": created_at,
        "cost_rate_usd_per_hr": 1.0,
        "tags": {"kinoforge_key": cap_key},
    }
    if last_heartbeat is not None:
        e["last_heartbeat"] = last_heartbeat
    if heartbeat_thread_tick is not None:
        e["heartbeat_thread_tick"] = heartbeat_thread_tick
    if session_start is not None:
        e["session_start"] = session_start
    if session_end is not None:
        e["session_end"] = session_end
    return e


class _Compute:
    def __init__(self, provider: str) -> None:
        self.provider = provider


class _FakeCfg:
    def __init__(
        self,
        *,
        provider: str = "runpod",
        cap_hash: str = "abc123abc123XX",
    ) -> None:
        self._cap_hash = cap_hash
        self.compute = _Compute(provider)

    def capability_key(self) -> Any:
        cap_hash = self._cap_hash

        class _CapKey:
            # Mirrors the real CapabilityKey: `_cfg_want_stages` reads
            # `.stages` off it, and a double without the field would make
            # the /health stage gate untestable here.
            stages: tuple[str, ...] = ()

            def derive(self) -> str:
                return cap_hash

        return _CapKey()

    def lifecycle(self) -> Any:
        from kinoforge.core.interfaces import Lifecycle

        return Lifecycle(heartbeat_interval_s=30.0)


def _make_cfg(**kwargs: Any) -> Any:
    return _FakeCfg(**kwargs)


class _FakeCtx:
    """SessionContext stand-in with real store (for lock probes) and seedable ledger."""

    def __init__(self, store: LocalArtifactStore) -> None:
        self._store = store
        self._entries: list[dict[str, Any]] = []
        self._ledger = MagicMock()
        self._ledger.entries = MagicMock(return_value=self._entries)

    def store(self) -> LocalArtifactStore:
        return self._store

    def ledger(self) -> MagicMock:
        return self._ledger

    def seed(self, entry: dict[str, Any]) -> None:
        self._entries.append(entry)


def _make_ctx(tmp_path: Any) -> SessionContext:
    return cast("SessionContext", _FakeCtx(LocalArtifactStore(tmp_path)))


def _seed_entry(ctx: Any, eid: str, **kwargs: Any) -> None:
    cast(_FakeCtx, ctx).seed(_make_entry(eid=eid, **kwargs))


# Live ids for FakeProvider — used for classify gate via _resolve_warm_instance.
class _FakeProvider:
    def __init__(
        self,
        *,
        live_ids: set[str] | None = None,
        get_raises: Exception | None = None,
        list_raises: Exception | None = None,
    ) -> None:
        self._live_ids = (
            live_ids
            if live_ids is not None
            else {"pod-1", "pod-2", "pod-new", "pod-old"}
        )
        self._get_raises = get_raises
        self._list_raises = list_raises

    def list_instances(self) -> list[Instance]:
        if self._list_raises is not None:
            raise self._list_raises
        return [
            Instance(
                id=i,
                provider="runpod",
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
        return Instance(
            id=iid,
            provider="runpod",
            status="ready",
            created_at=_NOW - 60.0,
            endpoints={},
            tags={},
        )


@pytest.fixture
def patched_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"provider": _FakeProvider()}

    def _factory(name: str) -> Any:
        def _ctor() -> _FakeProvider:
            return state["provider"]

        return _ctor

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _factory)
    return state


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kinoforge.cli._commands.time.time", lambda: _NOW)


# ---------------------------------------------------------------------------
# Coarse filter
# ---------------------------------------------------------------------------


def test_empty_ledger_returns_none(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: returning a phantom Instance on empty ledger would crash deploy_session."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    assert report.skipped == []
    assert report.attached is None


def test_returns_none_when_no_cap_key_match(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: attaching to mismatched-cap_key pod would run wrong engine config."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg(cap_hash="aaa111ZZZZZZ")
    _seed_entry(ctx, "pod-1", cap_key="bbb222")
    instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    # Coarse-filter drops must be recorded like any other skip, or a live
    # pod dropped here reads identically to an empty ledger.
    assert report.skipped == [("pod-1", "cap-key-mismatch")]


def test_returns_none_when_provider_mismatch(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: cross-provider attach would call wrong vendor SDK."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg(provider="runpod")
    _seed_entry(ctx, "pod-1", provider="skypilot")
    instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    # Coarse-filter drops must be recorded like any other skip, or a live
    # pod dropped here reads identically to an empty ledger.
    assert report.skipped == [("pod-1", "provider-mismatch")]


def test_filters_busy_entries_via_is_session_busy(
    tmp_path: Any, patched_registry: dict[str, Any]
) -> None:
    """Bug: attaching to a busy pod would queue serially behind another CLI's lock,
    appearing wedged for minutes."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(
        ctx,
        "pod-1",
        session_start=100.0,
        heartbeat_thread_tick=100.0,
    )
    instance, report = _scan_warm_candidates(ctx, cfg, clock=FakeClock(100.0))
    assert instance is None
    # Busy entries are coarse-filtered but must still be recorded, or a
    # live-but-busy pod reads identically to an empty ledger.
    assert report.skipped == [("pod-1", "session-busy")]


def test_filters_classify_non_live_entries(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: attaching to IDLE_REAP / ORPHAN_REAP entries would race the reaper."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    # Old created_at + stale last_heartbeat → IDLE_REAP per Layer V classify.
    _seed_entry(
        ctx,
        "pod-1",
        created_at=_NOW - 99999.0,
        last_heartbeat=_NOW - 99999.0,
        heartbeat_thread_tick=_NOW - 5.0,
    )
    instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    # Verdict gate refusal records skip.
    assert any(r == "classify-not-live" for _, r in report.skipped)


def test_sorts_candidates_by_newest_heartbeat_thread_tick(
    tmp_path: Any,
    patched_registry: dict[str, Any],
    fixed_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: stable but non-fresh-first sort would attach to the least-recently-used pod,
    losing warm-cache benefit."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-old", heartbeat_thread_tick=_NOW - 60.0)
    _seed_entry(ctx, "pod-new", heartbeat_thread_tick=_NOW - 5.0)

    seen: list[str] = []

    def spy(store: Any, key: str) -> bool:
        seen.append(key)
        return True  # force-skip both → see ordering

    monkeypatch.setattr("kinoforge.cli._commands._probe_lock_held", spy)
    _scan_warm_candidates(ctx, cfg)
    reaper_keys = [k for k in seen if k.startswith("reaper/")]
    assert reaper_keys[0] == "reaper/pod-new"
    assert reaper_keys[1] == "reaper/pod-old"


def test_skips_reaper_lock_held_candidate(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: attaching to a pod B1 is mid-destroying would HTTP-fail at first submit."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1")
    with ctx.store().acquire_lock("reaper/pod-1", ttl_s=30.0):
        instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    assert ("pod-1", "reaper-held") in report.skipped


def test_skips_provision_lock_held_candidate(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: attaching mid-cold-boot would serialise behind B7 blocking-acquire."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1")
    with ctx.store().acquire_lock("provision/pod-1", ttl_s=300.0):
        instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    assert ("pod-1", "provision-held") in report.skipped


def test_returns_first_valid_candidate(
    tmp_path: Any,
    patched_registry: dict[str, Any],
    fixed_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: not returning on first success would over-validate and slow scan."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1", heartbeat_thread_tick=_NOW - 5.0)
    fake_instance = Instance(
        id="pod-1",
        provider="runpod",
        tags={},
        created_at=_NOW - 60.0,
        cost_rate_usd_per_hr=0.0,
        status="ready",
        endpoints={},
    )
    monkeypatch.setattr(
        "kinoforge.cli._commands._resolve_warm_instance",
        lambda *a, **kw: (fake_instance, None),
    )
    instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is fake_instance
    assert report.attached == "pod-1"


def test_record_includes_skipped_reasons_with_stable_codes(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: drifting reason vocabulary would break B2 dashboard ingestion."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1")
    with ctx.store().acquire_lock("reaper/pod-1", ttl_s=30.0):
        _, report = _scan_warm_candidates(ctx, cfg)
    valid_codes = {
        "reaper-held",
        "provision-held",
        "cap-key-drift",
        "cap-key-mismatch",
        "session-busy",
        "provider-mismatch",
        "provider-unconstructable",
        "list-instances-failed",
        "classify-not-live",
        "get-instance-keyerror",
    }
    for _, reason in report.skipped:
        assert reason in valid_codes


def test_scan_report_summarize_attached_case() -> None:
    """Bug: hit-case formatting drift would mislead operators reading logs."""
    r = _ScanReport(attached="pod-1", skipped=[("pod-2", "reaper-held")])
    msg = r.summarize()
    assert "attached to pod-1" in msg
    assert "skipped" in msg


def test_scan_report_summarize_miss_case() -> None:
    """Bug: miss-case formatting drift would hide cold-create-reason from operators."""
    r = _ScanReport(
        attached=None,
        skipped=[("pod-1", "reaper-held"), ("pod-2", "classify-not-live")],
    )
    msg = r.summarize()
    assert "cold create" in msg
    assert "reaper-held" in msg
    assert "classify-not-live" in msg


def test_summarize_names_a_cold_create_when_no_candidates_were_found() -> None:
    """An empty candidate list must not be silent.

    Bug caught: a cold create with zero candidates logged nothing at all,
    so an operator could not tell "no pod was running" from "a pod was
    running and never entered the candidate list" — which is exactly the
    ambiguity that made U14 undiagnosable without spending money again.
    """
    report = _ScanReport(attached=None, skipped=[])
    summary = report.summarize()
    # Pinned exact wording, not a substring: another task greps this exact
    # line, and a substring check would pass for a materially worse message
    # (e.g. one that dropped the "0" or reworded "cold create").
    assert summary == "warm-reuse: scanned 0 candidates — cold create"


def test_coarse_filtered_entry_summary_differs_from_empty_ledger(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """A live pod dropped by the coarse filter must not read like an empty ledger.

    Bug caught: provider mismatch, cap-key mismatch, and session-busy all
    short-circuited before `skipped` was populated, so `summarize()` printed
    the identical "scanned 0 candidates — cold create" line whether the
    ledger was genuinely empty or held a live pod dropped before validation
    ever ran. That is exactly the ambiguity a later task needs to resolve
    from this line alone, on hardware billed at $2.50/hr — reading it wrong
    costs a paid boot.
    """
    ctx_empty = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _, empty_report = _scan_warm_candidates(ctx_empty, cfg)

    ctx_busy = _make_ctx(tmp_path)
    _seed_entry(ctx_busy, "pod-1", session_start=100.0, heartbeat_thread_tick=100.0)
    _, busy_report = _scan_warm_candidates(ctx_busy, cfg, clock=FakeClock(100.0))

    assert empty_report.summarize() == "warm-reuse: scanned 0 candidates — cold create"
    assert busy_report.summarize() != empty_report.summarize()
    assert busy_report.summarize() == (
        "warm-reuse: scanned 1, 0 attachable (reasons: 1 session-busy) — cold create"
    )


def test_force_attach_param_is_false_always(
    tmp_path: Any,
    patched_registry: dict[str, Any],
    fixed_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: auto-discovery bypassing verdicts would attach to non-LIVE pods,
    defeating the conservative-on-ignorance contract."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1")
    captured: dict[str, Any] = {}

    def spy_resolve(
        ctx_: Any,
        cfg_: Any,
        iid: str,
        *,
        force_attach: bool,
        entry: Any = None,
    ) -> tuple[Any, int]:
        captured["force_attach"] = force_attach
        return (None, 2)

    monkeypatch.setattr("kinoforge.cli._commands._resolve_warm_instance", spy_resolve)
    _scan_warm_candidates(ctx, cfg)
    assert captured["force_attach"] is False


def test_list_instances_failure_records_skip(
    tmp_path: Any,
    patched_registry: dict[str, Any],
    fixed_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: continuing after RPC failure without recording would lose audit signal."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1")

    def fail_resolve(*a: Any, **kw: Any) -> tuple[Any, int]:
        return (None, 2)

    monkeypatch.setattr("kinoforge.cli._commands._resolve_warm_instance", fail_resolve)
    instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    assert len(report.skipped) == 1


def test_uses_injected_clock_for_is_session_busy(
    tmp_path: Any, patched_registry: dict[str, Any], fixed_clock: None
) -> None:
    """Bug: using time.time() instead of injected clock breaks deterministic tests."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1", session_start=100.0, heartbeat_thread_tick=100.0)
    instance, report = _scan_warm_candidates(ctx, cfg, clock=FakeClock(100.0))
    assert instance is None
    # busy → coarse-filtered, but still recorded (see coarse-filter tests above)
    assert report.skipped == [("pod-1", "session-busy")]


def test_skips_candidate_on_resolve_warm_instance_failure(
    tmp_path: Any,
    patched_registry: dict[str, Any],
    fixed_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: not skipping on rc=1/2 would attach to ledger-stale entries."""
    ctx = _make_ctx(tmp_path)
    cfg = _make_cfg()
    _seed_entry(ctx, "pod-1")
    monkeypatch.setattr(
        "kinoforge.cli._commands._resolve_warm_instance",
        lambda *a, **kw: (None, 2),
    )
    instance, report = _scan_warm_candidates(ctx, cfg)
    assert instance is None
    assert len(report.skipped) == 1


def test_probe_lock_held_returns_True_when_held(tmp_path: Any) -> None:
    """Bug: probe returning False on held lock would skip the D5 race protection."""
    store = LocalArtifactStore(tmp_path)
    with store.acquire_lock("test-key", ttl_s=30.0):
        assert _probe_lock_held(store, "test-key") is True


def test_probe_lock_held_returns_False_when_unheld(tmp_path: Any) -> None:
    """Bug: probe returning True on unheld lock would skip every candidate."""
    store = LocalArtifactStore(tmp_path)
    assert _probe_lock_held(store, "test-key") is False


# ---------------------------------------------------------------------------
# T14 /health stage gate — U14 regression
# ---------------------------------------------------------------------------


def _upscale_only_cfg() -> Any:
    """A real Config shaped like modal-diffusers-flashvsr-*-upscale.yaml."""
    from kinoforge.core.config import Config

    return Config.model_validate(
        {
            "engine": {
                "kind": "diffusers",
                "precision": "bfloat16",
                "diffusers": {"image": "python:3.13-slim", "upscale_only": True},
            },
            "models": [],
            "compute": {
                "provider": "modal",
                "image": "python:3.13-slim",
                "lifecycle": {"heartbeat_interval_s": 30, "budget": 2.0},
            },
            "upscale": {
                "engine": "flashvsr",
                "scale": "4x",
                "flashvsr": {
                    "weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1",
                    "precision": "bfloat16",
                },
            },
        }
    )


def _t2v_plus_upscale_cfg() -> Any:
    """A real Config that genuinely needs BOTH stages on the pod."""
    from kinoforge.core.config import Config

    return Config.model_validate(
        {
            "engine": {
                "kind": "diffusers",
                "precision": "bfloat16",
                "diffusers": {"image": "python:3.13-slim"},
            },
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
                    "target": "diffusion_models",
                }
            ],
            "compute": {
                "provider": "modal",
                "image": "python:3.13-slim",
                "lifecycle": {"heartbeat_interval_s": 30, "budget": 2.0},
            },
            "upscale": {
                "engine": "flashvsr",
                "scale": "4x",
                "flashvsr": {
                    "weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1",
                    "precision": "bfloat16",
                },
            },
        }
    )


def _seed_live_upscale_pod(ctx: Any, cfg: Any) -> str:
    """Seed a LIVE ledger row for a warm pod matching ``cfg``'s key."""
    eid = "pod-1"
    entry = _make_entry(
        eid=eid,
        provider="modal",
        cap_key=cfg.capability_key().derive()[:12],
    )
    entry["endpoints"] = {"8000": "https://pod-1.example.invalid"}
    fake = cast(_FakeCtx, ctx)
    fake.seed(entry)
    # _resolve_warm_instance re-reads the row by id; the shared fake's
    # ledger is a MagicMock whose default read() would shadow the seed.
    fake.ledger().read = lambda iid: entry if iid == eid else None
    return eid


def test_upscale_only_cfg_attaches_to_pod_advertising_only_upscale(
    tmp_path: Any,
    patched_registry: dict[str, Any],
    fixed_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U14: an upscale-only cfg must attach to an upscale-only pod.

    Bug caught: ``_cfg_want_stages`` demanded ``("t2v", "upscale")`` from
    every cfg carrying an upscale block, so a pod booted from an
    ``upscale_only: true`` cfg — whose /health legitimately advertises
    ``["upload", "upscale"]`` because no Wan pipeline is ever loaded — was
    rejected with ``stage-mismatch`` and a second $2.50/hr A100 was
    cold-booted beside the idle one. Observed live 2026-09-07:
    ``warm-reuse: scanned 1, 0 attachable (reasons: 1 stage-mismatch)``.
    """
    ctx = _make_ctx(tmp_path)
    cfg = _upscale_only_cfg()
    eid = _seed_live_upscale_pod(ctx, cfg)
    monkeypatch.setattr(
        "kinoforge.cli._commands._http_get_json",
        lambda url: {"capabilities": ["upload", "upscale"]},
    )

    instance, report = _scan_warm_candidates(ctx, cfg)

    assert report.skipped == []
    assert report.attached == eid
    assert instance is not None
    assert instance.id == eid


def test_t2v_plus_upscale_cfg_still_refuses_an_upscale_only_pod(
    tmp_path: Any,
    patched_registry: dict[str, Any],
    fixed_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must survive the U14 fix, not be deleted by it.

    Bug caught: relaxing ``_cfg_want_stages`` to ``("upscale",)`` for
    every upscale cfg would let a cfg that really does run t2v attach to
    a pod with no Wan pipeline loaded, and the generate stage would fail
    on the pod after the attach instead of cold-booting a correct one.
    """
    ctx = _make_ctx(tmp_path)
    cfg = _t2v_plus_upscale_cfg()
    eid = _seed_live_upscale_pod(ctx, cfg)
    monkeypatch.setattr(
        "kinoforge.cli._commands._http_get_json",
        lambda url: {"capabilities": ["upload", "upscale"]},
    )

    instance, report = _scan_warm_candidates(ctx, cfg)

    assert instance is None
    assert report.attached is None
    assert report.skipped == [(eid, "stage-mismatch")]
