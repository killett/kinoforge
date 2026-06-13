# B4 — Cross-CLI Warm-Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the Layer P warm-supplied `instance=` kwarg at the CLI as `kinoforge generate --instance-id <id>` / `kinoforge batch --instance-id <id>`, with classify-gated `--force-attach`, capability_key precheck, and a `kinoforge list` cap_key column.

**Architecture:** Shared helper `_resolve_warm_instance` in `cli/_commands.py` runs cheap-first validation (ledger → provider-kind → cap_key → classify) and returns `(Instance | None, exit_code | None)`. Both `_cmd_generate` and `_cmd_batch` call it before delegating to the existing orchestrator entry points with the resolved `Instance` threaded through the `instance=` kwarg. `_cmd_list` appends a `capability_key=<hash>` column. Orchestrator unchanged; B7's `provision:<id>` lock reused unchanged.

**Tech Stack:** Python 3.13, argparse, pytest, FakeProvider (registry adapter). No new modules; all changes confined to `cli/_main.py` + `cli/_commands.py` + tests.

**Spec:** `docs/superpowers/specs/2026-06-12-b4-cross-cli-warm-reuse-design.md` — decisions D1–D8 locked, AC1–AC12 enumerated, F1–F12 failure modes mapped.

**Scope guardrails honoured:**
- Manual escape hatch only. No auto-discovery.
- No orchestrator changes (`core/orchestrator.py` / `core/lifecycle.py` diffs are empty).
- No new ABCs, substrate, specs, or YAML fields.
- B7's `provision:<id>` lock reused unchanged. No CLI-side acquire.
- Live spend $0; FakeProvider smoke covers the full CLI path.

---

## File Structure

**Modify:**
- `src/kinoforge/cli/_main.py` — argparse: add `--instance-id` + `--force-attach` to `p_generate` (lines 304–320) and `p_batch` (lines 400–423). ~12 LOC.
- `src/kinoforge/cli/_commands.py` — new `_resolve_warm_instance` helper (~80 LOC including the per-verdict refuse-text blocks); wire `instance=` through `_cmd_generate` (line 278) and `_cmd_batch` (line 329); append cap_key column to `_cmd_list` (line 459). ~50 LOC net.

**Tests new:**
- `tests/cli/test_resolve_warm_instance.py` — ~17 unit cases per verdict / refusal path.
- `tests/live/test_warm_attach_dry_run.py` — 1 FakeProvider end-to-end smoke.

**Tests delta:**
- `tests/cli/test_main_flow.py` — `_cmd_list` cap_key column (~2 cases). End-to-end `--instance-id` smoke through `cli.main([...])` (~4 cases for generate, ~3 for batch).

**Docs:**
- `README.md` — new "Operator warm-reuse" section.
- `PROGRESS.md` — strike B4, point at this spec.
- `warm-reuse-tasks.txt` — replace B4 starter (lines 367–455) with closeout pointer.

---

## Task 0: `_resolve_warm_instance` helper + unit tests

**Goal:** Implement the shared validation helper end-to-end with full verdict-gate coverage. This is the foundation every other task depends on.

**Files:**
- Create: `tests/cli/test_resolve_warm_instance.py`
- Modify: `src/kinoforge/cli/_commands.py` — add `_resolve_warm_instance` helper after existing `_classify_for_status` (~line 617).

**Acceptance Criteria:**
- [ ] Helper signature: `_resolve_warm_instance(ctx, cfg, instance_id, *, force_attach, clock=None) -> tuple[Instance | None, int | None]`.
- [ ] Validation order matches spec §3.3: ledger.read → provider-kind → cap_key (via `entry["tags"]["kinoforge_key"]`) → provider construction → classify → get_instance.
- [ ] Verdict gate per D3: LIVE passes; HEARTBEAT_UNKNOWN / IDLE_REAP / ORPHAN_REAP pass only with `force_attach=True`; STALE_LEDGER / OVERAGE_REAP / UNROUTABLE refuse always.
- [ ] Exit codes per D2: `(None, 1)` for ledger-absent; `(None, 2)` for every precondition refusal.
- [ ] All 17 unit cases pass.

**Verify:** `pixi run pytest tests/cli/test_resolve_warm_instance.py -v` → 17 passed.

**Steps:**

- [ ] **Step 1: Write failing test file**

Create `tests/cli/test_resolve_warm_instance.py`:

```python
"""B4 — unit tests for `_resolve_warm_instance` helper."""

from __future__ import annotations

import time
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
    provider: str = "fake",
    cap_key: str | None = "ab12cd34ef56",
    created_at: float = _NOW - 60.0,
    last_heartbeat: float | None = _NOW - 5.0,
    heartbeat_thread_tick: float | None = _NOW - 5.0,
    cost_rate: float = 1.0,
) -> dict[str, Any]:
    """Build a ledger entry shaped like the orchestrator persists today.

    cap_key lives at entry['tags']['kinoforge_key'] (orchestrator.py:492,1015).
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


class _FakeCfg:
    """Minimal Config stand-in: capability_key().derive() returns a fixed hash."""

    def __init__(
        self,
        *,
        provider: str = "fake",
        cap_hash: str = "ab12cd34ef56XX",
    ) -> None:
        self._provider = provider
        self._cap_hash = cap_hash

        class _Compute:
            pass

        self.compute = _Compute()
        self.compute.provider = provider

    def capability_key(self) -> Any:
        cap_hash = self._cap_hash

        class _CapKey:
            def derive(self) -> str:
                return cap_hash

        return _CapKey()

    def lifecycle(self) -> Any:
        from kinoforge.core.interfaces import Lifecycle

        return Lifecycle()


class _FakeCtx:
    """SessionContext stand-in returning a MagicMock ledger with Ledger.read."""

    def __init__(self, entry: dict[str, Any] | None, cfg: Any) -> None:
        self.cfg = cfg
        self._ledger = MagicMock()
        self._ledger.read = MagicMock(return_value=entry)
        # entries() also needed for some classify paths
        self._ledger.entries = MagicMock(
            return_value=([entry] if entry is not None else [])
        )

    def ledger(self) -> MagicMock:
        return self._ledger


def _ctx(entry: dict[str, Any] | None, cfg: Any) -> SessionContext:
    return cast("SessionContext", _FakeCtx(entry, cfg))


class _FakeProvider:
    """Provider stand-in supporting list_instances + get_instance.

    Behaviour is configured per-test via the kwargs.
    """

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
            status="ready",
            endpoints={},
            offer=None,
            tags={},
            created_at=_NOW - 60.0,
        )

    def list_instances(self) -> list[Instance]:
        if self._list_raises is not None:
            raise self._list_raises
        return [
            Instance(
                id=i,
                status="ready",
                endpoints={},
                offer=None,
                tags={},
                created_at=_NOW - 60.0,
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

    monkeypatch.setattr("kinoforge.cli._commands.registry.get_provider", _factory)
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
    cfg = _FakeCfg(cap_hash="ab12cd34ef56XX")
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
    cfg = _FakeCfg()
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
    cfg = _FakeCfg(provider="runpod")
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
    cfg = _FakeCfg(cap_hash="aaaaaaaaaaaaXX")
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
    cfg = _FakeCfg(cap_hash="ab12cd34ef56XX")
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

    monkeypatch.setattr(
        "kinoforge.cli._commands.registry.get_provider", _bad_factory
    )
    cfg = _FakeCfg()
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
    cfg = _FakeCfg()
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
    patched_registry["provider"] = _FakeProvider(live_ids=set())  # no live pods
    cfg = _FakeCfg()
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
    """Idle for longer than Lifecycle().idle_timeout_s (default 1800s)."""
    cfg = _FakeCfg()
    # last_heartbeat ancient → hb_age > idle_timeout
    entry = _entry(last_heartbeat=_NOW - 9999.0, heartbeat_thread_tick=_NOW - 5.0)
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
    cfg = _FakeCfg()
    entry = _entry(last_heartbeat=_NOW - 9999.0, heartbeat_thread_tick=_NOW - 5.0)
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=True)
    assert rc is None and inst is not None


def test_returns_2_on_ORPHAN_REAP_without_force(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sentinel-stale beyond grace_after_session_s (default 300s)."""
    cfg = _FakeCfg()
    entry = _entry(
        created_at=_NOW - 9999.0,  # past grace window
        heartbeat_thread_tick=_NOW - 9999.0,  # sentinel ancient
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
    cfg = _FakeCfg()
    entry = _entry(
        created_at=_NOW - 9999.0, heartbeat_thread_tick=_NOW - 9999.0
    )
    ctx = _ctx(entry, cfg)
    inst, rc = _resolve_warm_instance(ctx, cfg, "i-1", force_attach=True)
    assert rc is None and inst is not None


def test_returns_2_on_HEARTBEAT_UNKNOWN_without_force(
    patched_registry: dict[str, Any],
    fixed_clock: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No heartbeat_thread_tick field at all → HEARTBEAT_UNKNOWN."""
    cfg = _FakeCfg()
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
    cfg = _FakeCfg()
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
    cfg = _FakeCfg()
    entry = _entry(created_at=_NOW - 999999.0)  # very old
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
    cfg = _FakeCfg()
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
    patched_registry["provider"].list_instances = spy  # type: ignore[method-assign]
    cfg = _FakeCfg(cap_hash="aaaaaaaaaaaaXX")
    entry = _entry(cap_key="bbbbbbbbbbbb")
    ctx = _ctx(entry, cfg)
    _resolve_warm_instance(ctx, cfg, "i-1", force_attach=False)
    assert spy.call_count == 0
```

- [ ] **Step 2: Run failing tests**

```bash
pixi run pytest tests/cli/test_resolve_warm_instance.py -v
```

Expected: ImportError or AttributeError — `_resolve_warm_instance` not defined.

- [ ] **Step 3: Implement helper in `_commands.py`**

Add after `_classify_for_status` (around line 617), before `_cmd_status`:

```python
# ---------------------------------------------------------------------------
# B4 — `--instance-id` warm-attach helper
# ---------------------------------------------------------------------------


_FORCE_BYPASSABLE_VERDICTS: frozenset[str] = frozenset(
    {"HEARTBEAT_UNKNOWN", "IDLE_REAP", "ORPHAN_REAP"}
)


def _resolve_warm_instance(
    ctx: SessionContext,
    cfg: Config,
    instance_id: str,
    *,
    force_attach: bool,
    clock: Clock | None = None,
) -> tuple["Instance | None", int | None]:
    """Validate operator-supplied --instance-id; return Instance or exit code.

    Order (D1 cheap-first):
      1. Ledger.read(instance_id) — missing → (None, 1).
      2. Provider-kind: entry["provider"] vs cfg.compute.provider → (None, 2).
      3. capability_key: cfg.capability_key().derive()[:12] vs
         entry["tags"]["kinoforge_key"] → (None, 2).
      4. Provider construction → (None, 2) on UnknownAdapter / other.
      5. list_instances() → (None, 2) on raise.
      6. classify(entry, live_ids, now, ...) verdict gate per D3:
           LIVE: pass.
           HEARTBEAT_UNKNOWN / IDLE_REAP / ORPHAN_REAP: pass IFF force_attach.
           STALE_LEDGER / OVERAGE_REAP / UNROUTABLE: refuse always.
      7. provider.get_instance(instance_id) → (None, 2) on KeyError.
    """
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter
    from kinoforge.core.interfaces import Instance, Lifecycle
    from kinoforge.core.reaper import classify

    # Read time through the module attribute so tests can pin it via monkeypatch.
    now = time.time()

    # 1. Ledger lookup.
    ledger = ctx.ledger()
    entry = ledger.read(instance_id)
    if entry is None:
        print(
            f"instance not found in ledger: {instance_id}. "
            f"Run 'kinoforge list' to see available ids.",
            file=sys.stderr,
        )
        return (None, 1)

    # 2. Provider-kind.
    entry_provider = str(entry.get("provider", ""))
    cfg_provider = cfg.compute.provider if cfg.compute is not None else ""
    if entry_provider != cfg_provider:
        print(
            f"provider mismatch: cfg={cfg_provider}, ledger says "
            f"provider={entry_provider} for {instance_id}. "
            f"Use a cfg matching the pod's provider.",
            file=sys.stderr,
        )
        return (None, 2)

    # 3. capability_key.
    cfg_hash = cfg.capability_key().derive()[:12]
    entry_hash_raw = entry.get("tags", {}).get("kinoforge_key")
    entry_hash = str(entry_hash_raw) if entry_hash_raw is not None else "<unknown>"
    if cfg_hash != entry_hash:
        print(
            f"capability_key mismatch: cfg={cfg_hash}, ledger entry "
            f"{instance_id}={entry_hash}. Either use a cfg matching this pod "
            f"or 'kinoforge destroy --id {instance_id}' first.",
            file=sys.stderr,
        )
        return (None, 2)

    # 4. Provider construction.
    try:
        provider = registry.get_provider(entry_provider)()
    except UnknownAdapter as exc:
        print(
            f"provider {entry_provider} unconstructable: "
            f"{type(exc).__name__}: {exc}. Check provider credentials.",
            file=sys.stderr,
        )
        return (None, 2)
    except Exception as exc:  # noqa: BLE001
        print(
            f"provider {entry_provider} unconstructable: "
            f"{type(exc).__name__}: {exc}.",
            file=sys.stderr,
        )
        return (None, 2)

    # 5. list_instances RPC for classify's live_pod_ids.
    try:
        live_ids = {i.id for i in provider.list_instances()}
    except Exception as exc:  # noqa: BLE001
        print(
            f"provider {entry_provider} list_instances failed: "
            f"{type(exc).__name__}: {exc}.",
            file=sys.stderr,
        )
        return (None, 2)

    # 6. classify verdict gate.
    lifecycle = cfg.lifecycle() if cfg is not None else Lifecycle()
    verdict = classify(
        entry,
        live_ids,
        now,
        idle_timeout_s=lifecycle.idle_timeout_s,
        max_lifetime_s=lifecycle.max_lifetime_s,
        heartbeat_interval_s=lifecycle.heartbeat_interval_s,
        grace_after_session_s=lifecycle.grace_after_session_s,
    )
    v_name = verdict.value
    if v_name == "LIVE":
        pass
    elif v_name in _FORCE_BYPASSABLE_VERDICTS:
        if not force_attach:
            reason = _refuse_reason_for_verdict(v_name, entry, lifecycle, now)
            print(
                f"classify verdict {v_name} blocks attach for {instance_id}: "
                f"{reason}. Pass --force-attach to override, or "
                f"'kinoforge reap --apply' to clean up.",
                file=sys.stderr,
            )
            return (None, 2)
    elif v_name == "STALE_LEDGER":
        print(
            f"instance {instance_id} is stale: provider no longer has this "
            f"pod. Run 'kinoforge forget --id {instance_id}' and provision "
            f"a fresh one.",
            file=sys.stderr,
        )
        return (None, 2)
    elif v_name == "OVERAGE_REAP":
        print(
            f"instance {instance_id} exceeded max_lifetime_s (cfg policy). "
            f"Destroy it with 'kinoforge destroy --id {instance_id}' before "
            f"reusing the slot.",
            file=sys.stderr,
        )
        return (None, 2)
    else:  # UNROUTABLE or unknown
        print(
            f"classify verdict {v_name} blocks attach for {instance_id}.",
            file=sys.stderr,
        )
        return (None, 2)

    # 7. Provider get_instance.
    try:
        instance = provider.get_instance(instance_id)
    except KeyError:
        print(
            f"instance {instance_id} disappeared between classify and "
            f"lookup; a concurrent reaper may have destroyed it. "
            f"Re-run after 'kinoforge list'.",
            file=sys.stderr,
        )
        return (None, 2)

    return (instance, None)


def _refuse_reason_for_verdict(
    verdict: str,
    entry: dict,  # type: ignore[type-arg]
    lifecycle: Any,
    now: float,
) -> str:
    """One-line human-readable reason for a refused verdict."""
    if verdict == "IDLE_REAP":
        hb = float(entry.get("last_heartbeat", now))
        return (
            f"hb_age={now - hb:.0f}s > "
            f"idle_timeout={lifecycle.idle_timeout_s:.0f}s"
        )
    if verdict == "ORPHAN_REAP":
        tick = float(entry.get("heartbeat_thread_tick", now))
        return (
            f"sentinel_age={now - tick:.0f}s past "
            f"grace_after_session_s={lifecycle.grace_after_session_s:.0f}s"
        )
    if verdict == "HEARTBEAT_UNKNOWN":
        return "no sentinel data in ledger entry"
    return verdict
```

Add `Instance` to the imports at the top of `_commands.py` (it's already imported indirectly via `kinoforge.core.interfaces`; ensure module-level import for the type annotation):

```python
# in the TYPE_CHECKING block at the top:
if TYPE_CHECKING:
    from kinoforge.core.interfaces import Instance
    from kinoforge.core.reaper_actor import SweepReport
```

And add `Any` to the typing imports if not already present:

```python
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
```

- [ ] **Step 4: Run tests to GREEN**

```bash
pixi run pytest tests/cli/test_resolve_warm_instance.py -v
```

Expected: 17 passed.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/cli/test_resolve_warm_instance.py src/kinoforge/cli/_commands.py
git add tests/cli/test_resolve_warm_instance.py src/kinoforge/cli/_commands.py
git commit -m "$(cat <<'EOF'
feat(b4): add _resolve_warm_instance helper + verdict-gate tests

Implements the shared validation helper for --instance-id warm-attach
per design spec §3.3. Cheap-first order (ledger → provider-kind →
capability_key → classify) with --force-attach bypassing the
salvageable trio (HEARTBEAT_UNKNOWN / IDLE_REAP / ORPHAN_REAP) per D3.

Cap_key sourced from entry["tags"]["kinoforge_key"] matching the
orchestrator's Phase 18 tagging contract (orchestrator.py:492,1015).

17 unit cases cover happy path + each refusal step + every verdict gate
combination. No live spend; FakeProvider drives all RPCs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/cli/_commands.py", "tests/cli/test_resolve_warm_instance.py"], "verifyCommand": "pixi run pytest tests/cli/test_resolve_warm_instance.py -v", "acceptanceCriteria": ["17 unit tests pass", "_resolve_warm_instance signature matches spec §3.3", "verdict gate honours D3 (HEARTBEAT_UNKNOWN+IDLE_REAP+ORPHAN_REAP bypassable; STALE_LEDGER+OVERAGE_REAP+UNROUTABLE never bypassable)", "exit codes per D2 (1 ledger-absent / 2 precondition-refused)"]}
```

---

## Task 1: `_cmd_generate` wiring + argparse flags + dispatch tests

**Goal:** Wire `--instance-id` and `--force-attach` flags through `p_generate`, thread the resolved `Instance` into `generate(...)`, and verify dispatch end-to-end.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` lines 304–320 (add two flags to `p_generate`).
- Modify: `src/kinoforge/cli/_commands.py` line 278 (`_cmd_generate` body).
- Modify: `tests/cli/test_main_flow.py` (append new test cases).

**Acceptance Criteria:**
- [ ] `kinoforge generate --instance-id <id>` passes a resolved `Instance` into `generate()` via the `instance=` kwarg.
- [ ] `--force-attach` without `--instance-id` exits 2 with a one-line stderr message.
- [ ] Unknown id exits 1.
- [ ] `--force-attach` + IDLE_REAP entry: dispatches normally (passes through warm-attach).
- [ ] All four new test cases pass; existing `test_main_flow.py` cases still pass.

**Verify:** `pixi run pytest tests/cli/test_main_flow.py -v` → all green.

**Steps:**

- [ ] **Step 1: Write failing test cases**

Append to `tests/cli/test_main_flow.py`:

```python
# ---------------------------------------------------------------------------
# B4 — `kinoforge generate --instance-id` warm-attach
# ---------------------------------------------------------------------------


def _b4_local_cfg(p: Path) -> Path:
    """Local FakeProvider cfg suitable for warm-attach tests."""
    return _write_local_cfg(p)


def _seed_ledger_entry(state_dir: Path, *, eid: str, cap_hash: str) -> None:
    """Pre-populate ledger.json with a LIVE-shaped entry for `eid`."""
    import json
    import time as _time

    state_dir.mkdir(parents=True, exist_ok=True)
    now = _time.time()
    entry = {
        "id": eid,
        "provider": "local",
        "created_at": now - 60.0,
        "cost_rate_usd_per_hr": 1.0,
        "last_heartbeat": now - 5.0,
        "heartbeat_thread_tick": now - 5.0,
        "tags": {"kinoforge_key": cap_hash},
    }
    (state_dir / "ledger.json").write_text(json.dumps([entry]))


def test_generate_warm_attach_passes_instance_kwarg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--instance-id <id>` resolves to an Instance and is forwarded."""
    from unittest.mock import MagicMock
    from kinoforge.cli import main
    from kinoforge.core.interfaces import Artifact

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    # Derive the cap_hash that the cfg will produce.
    from kinoforge.cli.context import SessionContext

    sctx = SessionContext.from_args(state_dir=state, cfg_path=cfg)
    cap_hash = sctx.cfg.capability_key().derive()[:12]  # type: ignore[union-attr]
    _seed_ledger_entry(state, eid="i-warm", cap_hash=cap_hash)

    # Spy on cli.generate; assert instance= kwarg present.
    spy = MagicMock(
        return_value=(Artifact(uri="file:///tmp/x.mp4", run_id="r1"), None)
    )
    monkeypatch.setattr("kinoforge.cli.generate", spy)

    rc = main(
        [
            "--state-dir", str(state),
            "generate", "-c", str(cfg),
            "--prompt", "test prompt", "--mode", "t2v",
            "--instance-id", "i-warm",
        ]
    )
    assert rc == 0
    assert spy.call_count == 1
    kwargs = spy.call_args.kwargs
    assert kwargs.get("instance") is not None
    assert kwargs["instance"].id == "i-warm"


def test_generate_refuses_unknown_instance_id_exit_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "ledger.json").write_text("[]")

    rc = main(
        [
            "--state-dir", str(state),
            "generate", "-c", str(cfg),
            "--prompt", "p", "--mode", "t2v",
            "--instance-id", "i-nope",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found in ledger" in err


def test_generate_force_attach_without_instance_id_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    rc = main(
        [
            "--state-dir", str(state),
            "generate", "-c", str(cfg),
            "--prompt", "p", "--mode", "t2v",
            "--force-attach",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--force-attach" in err
    assert "--instance-id" in err


def test_generate_force_attach_passes_through_idle_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IDLE_REAP entry + --force-attach → dispatches to generate normally."""
    import json
    import time as _time
    from unittest.mock import MagicMock

    from kinoforge.cli import main
    from kinoforge.core.interfaces import Artifact

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    from kinoforge.cli.context import SessionContext

    sctx = SessionContext.from_args(state_dir=state, cfg_path=cfg)
    cap_hash = sctx.cfg.capability_key().derive()[:12]  # type: ignore[union-attr]
    now = _time.time()
    entry = {
        "id": "i-idle",
        "provider": "local",
        "created_at": now - 60.0,
        "cost_rate_usd_per_hr": 1.0,
        "last_heartbeat": now - 9999.0,
        "heartbeat_thread_tick": now - 5.0,
        "tags": {"kinoforge_key": cap_hash},
    }
    (state / "ledger.json").write_text(json.dumps([entry]))

    spy = MagicMock(
        return_value=(Artifact(uri="file:///tmp/x.mp4", run_id="r1"), None)
    )
    monkeypatch.setattr("kinoforge.cli.generate", spy)

    rc = main(
        [
            "--state-dir", str(state),
            "generate", "-c", str(cfg),
            "--prompt", "p", "--mode", "t2v",
            "--instance-id", "i-idle",
            "--force-attach",
        ]
    )
    assert rc == 0
    assert spy.call_args.kwargs.get("instance") is not None
```

- [ ] **Step 2: Run tests, confirm RED**

```bash
pixi run pytest tests/cli/test_main_flow.py -v -k generate
```

Expected: 4 new failures (`unrecognized arguments: --instance-id` / `--force-attach`).

- [ ] **Step 3: Add argparse flags to `p_generate` in `_main.py`**

After line 320 (the existing `--no-output-dir` argument), append before `# list`:

```python
    p_generate.add_argument(
        "--instance-id",
        default=None,
        metavar="ID",
        help=(
            "reuse an existing pod from the local ledger instead of cold-"
            "creating (skip ComfyUI + Wan spin-up). Use `kinoforge list` to "
            "find candidate ids."
        ),
    )
    p_generate.add_argument(
        "--force-attach",
        action="store_true",
        help=(
            "override classify verdicts HEARTBEAT_UNKNOWN, IDLE_REAP, "
            "ORPHAN_REAP for the supplied --instance-id. Has no effect "
            "without --instance-id. Never bypasses STALE_LEDGER, "
            "OVERAGE_REAP, UNROUTABLE, or capability_key mismatch."
        ),
    )
```

- [ ] **Step 4: Wire `_cmd_generate` body**

In `_commands.py`, modify `_cmd_generate` (line 278). Before the existing `try: _generate(...)` block, add the warm-attach precheck. Replace the existing `try: artifact, _ = _generate(...)` call with a version that threads `instance=`:

```python
def _cmd_generate(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``generate`` subcommand."""
    if ctx.cfg is None:
        raise RuntimeError("_cmd_generate requires --config")
    cfg = ctx.cfg
    store = ctx.store()
    sink = _build_sink(cfg, args)
    request = GenerationRequest(prompt=args.prompt, mode=args.mode)

    _cli_mod = sys.modules.get("kinoforge.cli")
    _clock = getattr(_cli_mod, "_cli_clock", _cli_clock)
    _generate = getattr(_cli_mod, "generate", generate)

    if args.run_id is not None:
        run_id: str = args.run_id
    else:
        ts = datetime.fromtimestamp(_clock.now()).strftime("%Y%m%d-%H%M%S")
        run_id = f"run-{ts}"

    # B4 — warm-attach precheck (D5: --dry-run not on this command).
    instance: Instance | None = None
    if getattr(args, "instance_id", None) is not None:
        instance, rc = _resolve_warm_instance(
            ctx, cfg, args.instance_id,
            force_attach=bool(getattr(args, "force_attach", False)),
        )
        if rc is not None:
            return rc
    elif getattr(args, "force_attach", False):
        print(
            "error: --force-attach has no effect without --instance-id",
            file=sys.stderr,
        )
        return 2

    try:
        artifact, _ = _generate(
            cfg,
            request,
            store=store,
            sink=sink,
            run_id=run_id,
            state_dir=ctx.state_dir,
            cancel_token=ctx.cancel_token,
            instance=instance,
        )
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    print(f"generated: uri={artifact.uri!r}")
    return 0
```

Add the `Instance` import at the top of `_commands.py` (move it out of TYPE_CHECKING since it's now needed at runtime for the annotation):

```python
from kinoforge.core.interfaces import GenerationRequest, Instance
```

- [ ] **Step 5: Run tests to GREEN**

```bash
pixi run pytest tests/cli/test_main_flow.py -v -k generate
```

Expected: previously failing 4 cases now pass; existing cases unaffected.

Full module:

```bash
pixi run pytest tests/cli/test_main_flow.py -v
```

Expected: all green.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_main_flow.py
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_main_flow.py
git commit -m "$(cat <<'EOF'
feat(b4): wire --instance-id + --force-attach into `kinoforge generate`

Adds the two flags to p_generate's argparse subparser and threads the
resolved Instance through to generate() via the existing instance=
kwarg (Layer P). --force-attach without --instance-id exits 2 with a
one-line stderr message. Unknown id exits 1.

Four new end-to-end cases in test_main_flow.py cover the happy warm-
attach path, the no-id refusal, the orphaned --force-attach guard, and
the IDLE_REAP bypass behaviour.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/cli/_main.py", "src/kinoforge/cli/_commands.py", "tests/cli/test_main_flow.py"], "verifyCommand": "pixi run pytest tests/cli/test_main_flow.py -v", "acceptanceCriteria": ["kinoforge generate --instance-id <id> threads instance= into generate()", "--force-attach without --instance-id exits 2", "unknown id exits 1", "--force-attach + IDLE_REAP entry dispatches normally"]}
```

---

## Task 2: `_cmd_batch` wiring + dispatch tests

**Goal:** Same wiring shape as Task 1 but for `kinoforge batch --instance-id`. Single pod reused across every manifest row (D7).

**Files:**
- Modify: `src/kinoforge/cli/_main.py` lines 400–423 (add two flags to `p_batch`).
- Modify: `src/kinoforge/cli/_commands.py` line 329 (`_cmd_batch` body).
- Modify: `tests/cli/test_main_flow.py` (append batch cases).

**Acceptance Criteria:**
- [ ] `kinoforge batch --instance-id <id> --manifest <m>` passes resolved Instance into `batch_generate()` via `instance=` kwarg.
- [ ] Single Instance shared across all manifest rows.
- [ ] Capability_key mismatch refuses at CLI before batch_generate runs.
- [ ] `--force-attach` without `--instance-id` exits 2.

**Verify:** `pixi run pytest tests/cli/test_main_flow.py -v -k batch` → all green.

**Steps:**

- [ ] **Step 1: Add manifest helper to test_main_flow.py**

Append (above the new batch cases):

```python
def _b4_manifest(p: Path, *, rows: int = 3) -> Path:
    """Write a minimal batch manifest with N rows varying only by prompt."""
    import yaml

    entries = [
        {"prompt": f"prompt-{i}", "mode": "t2v", "run_id": f"r-{i}"}
        for i in range(rows)
    ]
    m = p / "manifest.yaml"
    m.write_text(yaml.safe_dump({"entries": entries}))
    return m
```

(If `_b4_manifest` shape diverges from real `BatchManifest` schema, adapt to match `kinoforge.core.batch.load_manifest`'s expected input.)

- [ ] **Step 2: Write failing batch dispatch tests**

```python
def test_batch_warm_attach_single_pod_for_all_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All manifest rows share one Instance via the warm-attach path."""
    import json
    from unittest.mock import MagicMock

    from kinoforge.cli import main
    from kinoforge.core.batch import BatchResult

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    from kinoforge.cli.context import SessionContext

    sctx = SessionContext.from_args(state_dir=state, cfg_path=cfg)
    cap_hash = sctx.cfg.capability_key().derive()[:12]  # type: ignore[union-attr]
    _seed_ledger_entry(state, eid="i-warm-batch", cap_hash=cap_hash)
    manifest = _b4_manifest(tmp_path, rows=3)

    spy = MagicMock(return_value=BatchResult(outcomes=[]))
    monkeypatch.setattr("kinoforge.core.batch.batch_generate", spy)

    rc = main(
        [
            "--state-dir", str(state),
            "batch", "-c", str(cfg),
            "--manifest", str(manifest),
            "--instance-id", "i-warm-batch",
            "--stream-format", "none",
        ]
    )
    assert rc == 0
    assert spy.call_count == 1
    kwargs = spy.call_args.kwargs
    assert kwargs.get("instance") is not None
    assert kwargs["instance"].id == "i-warm-batch"


def test_batch_refuses_capability_key_mismatch_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    import time as _time

    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    now = _time.time()
    bad_entry = {
        "id": "i-mismatch",
        "provider": "local",
        "created_at": now - 60.0,
        "cost_rate_usd_per_hr": 1.0,
        "last_heartbeat": now - 5.0,
        "heartbeat_thread_tick": now - 5.0,
        "tags": {"kinoforge_key": "zzzzzzzzzzzz"},  # never matches cfg
    }
    (state / "ledger.json").write_text(json.dumps([bad_entry]))
    manifest = _b4_manifest(tmp_path)

    rc = main(
        [
            "--state-dir", str(state),
            "batch", "-c", str(cfg),
            "--manifest", str(manifest),
            "--instance-id", "i-mismatch",
            "--stream-format", "none",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "capability_key mismatch" in err


def test_batch_force_attach_without_instance_id_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    manifest = _b4_manifest(tmp_path)

    rc = main(
        [
            "--state-dir", str(state),
            "batch", "-c", str(cfg),
            "--manifest", str(manifest),
            "--force-attach",
            "--stream-format", "none",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--force-attach" in err
    assert "--instance-id" in err
```

- [ ] **Step 3: Confirm RED**

```bash
pixi run pytest tests/cli/test_main_flow.py -v -k batch
```

Expected: 3 new cases fail with `unrecognized arguments: --instance-id` / `--force-attach`.

- [ ] **Step 4: Add argparse flags to `p_batch` in `_main.py`**

After line 423 (the `--no-output-dir` argument in the `p_batch_output` group), append outside the mutex group, before `return parser`:

```python
    p_batch.add_argument(
        "--instance-id",
        default=None,
        metavar="ID",
        help=(
            "reuse an existing pod across every manifest row instead of "
            "cold-creating. Use `kinoforge list` to find candidate ids."
        ),
    )
    p_batch.add_argument(
        "--force-attach",
        action="store_true",
        help=(
            "override classify verdicts HEARTBEAT_UNKNOWN, IDLE_REAP, "
            "ORPHAN_REAP for the supplied --instance-id."
        ),
    )
```

- [ ] **Step 5: Wire `_cmd_batch` body**

In `_commands.py`, modify `_cmd_batch` (line 329). Before the existing `try: result = batch_generate(...)` block (around line 423), add the warm-attach precheck. Thread `instance=` through the `batch_generate` call.

Add after the `existing = store.list(batch_id)` block (around line 411) and before the `header = ...` line:

```python
    # B4 — warm-attach precheck.
    instance: Instance | None = None
    if getattr(args, "instance_id", None) is not None:
        instance, rc = _resolve_warm_instance(
            ctx, cfg, args.instance_id,
            force_attach=bool(getattr(args, "force_attach", False)),
        )
        if rc is not None:
            return rc
    elif getattr(args, "force_attach", False):
        print(
            "error: --force-attach has no effect without --instance-id",
            file=sys.stderr,
        )
        return 2
```

Update the `batch_generate(...)` call (around line 423) to add `instance=instance`:

```python
        result = batch_generate(
            cfg,
            manifest,
            store=store,
            sink=sink,
            batch_id=batch_id,
            concurrent=args.concurrent,
            state_dir=ctx.state_dir,
            on_event=formatter.emit,
            cancel_token=ctx.cancel_token,
            instance=instance,
        )
```

- [ ] **Step 6: Run tests to GREEN**

```bash
pixi run pytest tests/cli/test_main_flow.py -v
```

Expected: all green.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_main_flow.py
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_main_flow.py
git commit -m "$(cat <<'EOF'
feat(b4): wire --instance-id + --force-attach into `kinoforge batch`

Adds the two flags to p_batch's argparse subparser and threads the
resolved Instance through to batch_generate() via the existing
instance= kwarg (Layer P). Single Instance shared across every
manifest row per D7. Capability_key mismatch refuses at CLI before
batch_generate runs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/cli/_main.py", "src/kinoforge/cli/_commands.py", "tests/cli/test_main_flow.py"], "verifyCommand": "pixi run pytest tests/cli/test_main_flow.py -v -k batch", "acceptanceCriteria": ["kinoforge batch --instance-id threads instance= into batch_generate()", "single Instance shared across all manifest rows", "cap_key mismatch exits 2 before batch_generate runs", "--force-attach without --instance-id exits 2"]}
```

---

## Task 3: `_cmd_list` capability_key column

**Goal:** Append `capability_key=<12-char hash>` to every `kinoforge list` row. Pure ledger read; no new RPC.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` line 459 (`_cmd_list` body).
- Modify: `tests/cli/test_commands_routing.py` (extend `test_cmd_list_*` cases).

**Acceptance Criteria:**
- [ ] `kinoforge list` output appends `capability_key=<hash>` per row.
- [ ] Hash sourced from `entry["tags"]["kinoforge_key"]`.
- [ ] Legacy entry lacking `tags.kinoforge_key` renders `capability_key=<unknown>`.
- [ ] Existing `test_commands_routing.py::test_cmd_list_*` cases still pass.

**Verify:** `pixi run pytest tests/cli/test_commands_routing.py -v -k list` → all green.

**Steps:**

- [ ] **Step 1: Write failing test cases**

Append to `tests/cli/test_commands_routing.py`:

```python
def test_cmd_list_includes_capability_key_column(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each list row shows `capability_key=<12-char hash>`."""
    import json

    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)  # reuse existing helper
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": "i-1",
        "provider": "local",
        "created_at": 1.0,
        "tags": {"kinoforge_key": "ab12cd34ef56"},
    }
    (state / "ledger.json").write_text(json.dumps([entry]))
    rc = main(
        ["--state-dir", str(state), "list"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "capability_key=ab12cd34ef56" in out


def test_cmd_list_prints_unknown_for_legacy_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Legacy entry without tags.kinoforge_key → capability_key=<unknown>."""
    import json

    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    legacy = {
        "id": "i-legacy",
        "provider": "local",
        "created_at": 1.0,
        "tags": {},
    }
    (state / "ledger.json").write_text(json.dumps([legacy]))
    rc = main(
        ["--state-dir", str(state), "list"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "capability_key=<unknown>" in out
```

(If `_write_local_cfg` doesn't exist in `test_commands_routing.py`, copy the helper from `test_main_flow.py` or import it via a shared conftest.)

- [ ] **Step 2: Confirm RED**

```bash
pixi run pytest tests/cli/test_commands_routing.py -v -k list
```

Expected: 2 new cases fail (column substring not present).

- [ ] **Step 3: Update `_cmd_list` print format**

In `_commands.py`, replace the existing `_cmd_list` body (around line 470):

```python
def _cmd_list(args: argparse.Namespace, ctx: SessionContext) -> int:  # noqa: ARG001
    """Handle ``list`` subcommand — prints ledger entries with cap_key column.

    B4: appends `capability_key=<12-char hash>` sourced from
    entry["tags"]["kinoforge_key"]. Legacy entries lacking that field
    render `capability_key=<unknown>`.
    """
    ledger = ctx.ledger()
    entries = ledger.entries()
    if not entries:
        print("No instances recorded in ledger.")
        return 0
    for entry in entries:
        cap_key = str(entry.get("tags", {}).get("kinoforge_key", "<unknown>"))
        print(
            f"  {entry.get('id', '?')}  "
            f"provider={entry.get('provider', '?')}  "
            f"capability_key={cap_key}"
        )
    return 0
```

- [ ] **Step 4: Run tests to GREEN**

```bash
pixi run pytest tests/cli/test_commands_routing.py -v
pixi run pytest tests/cli/test_main_flow.py -v -k list
```

Expected: all green.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_commands.py tests/cli/test_commands_routing.py
git add src/kinoforge/cli/_commands.py tests/cli/test_commands_routing.py
git commit -m "$(cat <<'EOF'
feat(b4): add capability_key column to `kinoforge list`

Each list row now shows `capability_key=<12-char hash>` sourced from
entry["tags"]["kinoforge_key"]. Legacy entries lacking the field render
`capability_key=<unknown>`. Pure ledger read; zero new RPC. Discovery
loop: `kinoforge list` → match cap_key vs cfg → `kinoforge generate
--instance-id <id>`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/cli/_commands.py", "tests/cli/test_commands_routing.py"], "verifyCommand": "pixi run pytest tests/cli/test_commands_routing.py -v -k list", "acceptanceCriteria": ["list shows capability_key=<hash>", "legacy entry shows capability_key=<unknown>", "hash sourced from entry.tags.kinoforge_key"]}
```

---

## Task 4: FakeProvider end-to-end smoke

**Goal:** Drive the full CLI through `cli.main([...])` against `FakeProvider` and assert the warm-attach path skips `create_instance`.

**Files:**
- Create: `tests/live/test_warm_attach_dry_run.py`

**Acceptance Criteria:**
- [ ] `kinoforge generate -c fake.yaml --prompt P --mode t2v --instance-id <id>` exits 0.
- [ ] Stdout contains `generated: uri=`.
- [ ] `FakeProvider.create_instance` is NEVER called during the warm-attach path.
- [ ] `FakeProvider.get_instance` is called at least once with the supplied id.

**Verify:** `pixi run pytest tests/live/test_warm_attach_dry_run.py -v` → 1 passed.

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/live/test_warm_attach_dry_run.py`:

```python
"""B4 — FakeProvider end-to-end smoke for `kinoforge generate --instance-id`.

Verifies the warm-attach path skips create_instance and reuses the
operator-supplied pod. Pure offline; no cloud spend. Lives under
tests/live/ because it exercises the full CLI through cli.main([...])
not just one handler.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_FAKE_YAML = (
    "engine:\n  kind: fake\n  precision: fp16\n"
    "models:\n  - kind: base\n    name: m\n    ref: fake://m\n"
    "    target: checkpoints\n"
    "compute:\n  provider: local\n  image: kinoforge/local:latest\n"
    "  lifecycle:\n    idle_timeout: 1h\n    job_timeout: 30m\n"
    "    time_buffer: 30m\n    max_lifetime: 3h\n    budget: 10.0\n"
)


def test_full_cli_warm_attach_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm-attach path skips create_instance; reuses operator-supplied pod."""
    from kinoforge.cli import main
    from kinoforge.cli.context import SessionContext

    cfg_path = tmp_path / "fake.yaml"
    cfg_path.write_text(_FAKE_YAML)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)

    # Seed ledger with an entry whose cap_key matches what the cfg produces.
    sctx = SessionContext.from_args(state_dir=state, cfg_path=cfg_path)
    cap_hash = sctx.cfg.capability_key().derive()[:12]  # type: ignore[union-attr]
    now = time.time()
    entry = {
        "id": "i-warm-smoke",
        "provider": "local",
        "created_at": now - 60.0,
        "cost_rate_usd_per_hr": 0.0,
        "last_heartbeat": now - 5.0,
        "heartbeat_thread_tick": now - 5.0,
        "tags": {"kinoforge_key": cap_hash},
    }
    (state / "ledger.json").write_text(json.dumps([entry]))

    # Spy on LocalProvider.create_instance and .get_instance.
    from kinoforge.providers import local as local_provider_mod

    create_spy = MagicMock(wraps=local_provider_mod.LocalProvider.create_instance)
    get_spy = MagicMock(wraps=local_provider_mod.LocalProvider.get_instance)
    monkeypatch.setattr(
        local_provider_mod.LocalProvider, "create_instance", create_spy
    )
    monkeypatch.setattr(
        local_provider_mod.LocalProvider, "get_instance", get_spy
    )

    rc = main(
        [
            "--state-dir", str(state),
            "generate", "-c", str(cfg_path),
            "--prompt", "smoke test prompt",
            "--mode", "t2v",
            "--instance-id", "i-warm-smoke",
        ]
    )

    assert rc == 0, "warm-attach smoke should exit 0"
    assert create_spy.call_count == 0, (
        "warm-attach must NOT call create_instance"
    )
    # get_instance is called at least once during _resolve_warm_instance.
    called_ids = [
        call.args[0] if call.args else call.kwargs.get("instance_id")
        for call in get_spy.call_args_list
    ]
    assert "i-warm-smoke" in called_ids
```

(If `LocalProvider` is not the right FakeProvider to use — e.g. the engine 'fake' uses a different compute substrate — adjust the spy targets to whichever provider class the local-store cfg resolves to in `registry.get_provider("local")`.)

- [ ] **Step 2: Confirm RED**

```bash
pixi run pytest tests/live/test_warm_attach_dry_run.py -v
```

Expected: AttributeError on `--instance-id` if Task 1 hasn't landed (this task depends on Task 1+2). If Task 1+2 are in, the test exercises the full chain.

- [ ] **Step 3: Investigate failures + adjust**

If the test fails with a real assertion (e.g. `create_instance` was called once), inspect why the warm-attach path leaked. Likely causes:
- `_resolve_warm_instance` returned `(None, rc)` because the verdict gate triggered (cfg's `capability_key().derive()` rotates between runs — re-seed with a fresh hash).
- The orchestrator path with `instance=` is unwiring back to `create_instance` for the engine 'fake' (Layer P invariant — should never happen, but worth confirming).

If the test passes, proceed to commit.

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/live/test_warm_attach_dry_run.py
git add tests/live/test_warm_attach_dry_run.py
git commit -m "$(cat <<'EOF'
test(b4): FakeProvider end-to-end smoke for --instance-id warm-attach

Drives `kinoforge generate --instance-id <id>` through cli.main([...])
against a pre-seeded ledger entry. Spies on LocalProvider.create_instance
to assert the warm-attach path NEVER cold-creates, and on get_instance
to confirm the supplied id is the one resolved. No cloud spend; pure
offline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/test_warm_attach_dry_run.py"], "verifyCommand": "pixi run pytest tests/live/test_warm_attach_dry_run.py -v", "acceptanceCriteria": ["full CLI exits 0", "stdout contains 'generated: uri='", "create_instance call_count == 0", "get_instance called with the supplied id"]}
```

---

## Task 5: README + PROGRESS + warm-reuse-tasks closeout

**Goal:** Document the operator workflow; mark B4 done in the project trackers.

**Files:**
- Modify: `README.md` — append "Operator warm-reuse" section.
- Modify: `PROGRESS.md` — strike B4 entry; add Phase entry pointing at the spec.
- Modify: `warm-reuse-tasks.txt` — replace B4 starter (lines 367–455) with closeout line.

**Acceptance Criteria:**
- [ ] README documents the `list → match cap_key → generate --instance-id` discovery loop.
- [ ] README documents the bypassable-verdict matrix (which verdicts `--force-attach` overrides; which it doesn't).
- [ ] PROGRESS.md B4 entry strikethrough'd, with a one-line pointer to the spec + closeout commit.
- [ ] warm-reuse-tasks.txt B4 entry replaced with closeout pointer.

**Verify:** `git diff HEAD~1 README.md PROGRESS.md warm-reuse-tasks.txt` shows the three docs updated coherently.

**Steps:**

- [ ] **Step 1: Add README section**

Append a new "## Operator warm-reuse" section under the existing Operator Guide (or whatever the README's nearest neighbour is). Show the discovery loop + the matrix:

```markdown
## Operator warm-reuse (B4)

After a successful `kinoforge generate` or `kinoforge deploy`, the
provisioned pod stays in the local ledger. A second invocation can
reuse it without paying the 1–5 minute ComfyUI + Wan cold-start cost.

### Discovery loop

```bash
kinoforge list                       # shows id + provider + capability_key
# Match the printed capability_key against your cfg:
#   python -c "from kinoforge.core.config import Config; \
#              print(Config.from_yaml('cfg.yaml').capability_key().derive()[:12])"
kinoforge status --id <id>           # confirm the verdict is LIVE
kinoforge generate -c cfg.yaml --prompt P --mode t2v --instance-id <id>
```

`kinoforge batch -c cfg.yaml --manifest m.yaml --instance-id <id>` reuses
the same pod across every manifest row.

### `--force-attach` matrix

When the classify verdict is not LIVE, `kinoforge generate` refuses.
Pass `--force-attach` to override the salvageable verdicts:

| Verdict | Default | `--force-attach` |
|---|---|---|
| LIVE | attach | attach |
| HEARTBEAT_UNKNOWN | refuse | attach |
| IDLE_REAP | refuse | attach |
| ORPHAN_REAP | refuse | attach |
| STALE_LEDGER | refuse | refuse (pod is gone) |
| OVERAGE_REAP | refuse | refuse (max_lifetime policy) |
| UNROUTABLE | refuse | refuse (provider unreachable) |

Capability_key mismatch is never bypassable — use a cfg matching the
pod or `kinoforge destroy --id <id>` to free the slot.

Exit codes:
- `0` warm-attach succeeded.
- `1` instance id not in ledger.
- `2` precondition refused (provider mismatch / cap_key mismatch /
  classify verdict non-LIVE without `--force-attach` / pod raced
  destroyed between classify and attach).
```

- [ ] **Step 2: Update PROGRESS.md**

Locate the B4 entry in PROGRESS.md (line 147 per the spec sanity-check). Apply a strikethrough wrapper around the entry's title line and append a closeout pointer:

```markdown
- **B4. Cross-CLI warm-reuse CLI exposure.** ~~Layer P Task 7 item #2…~~
  CLOSED — see `docs/superpowers/specs/2026-06-12-b4-cross-cli-warm-reuse-design.md`.
  CLI now exposes `--instance-id` + `--force-attach` on `generate` + `batch`,
  `kinoforge list` shows `capability_key=<hash>`, helper at
  `cli/_commands.py:_resolve_warm_instance`.
```

(Match the strikethrough convention used by other closed entries in PROGRESS.md.)

- [ ] **Step 3: Update warm-reuse-tasks.txt**

Replace lines 367–455 (the B4 starter spec block) with a single closeout line:

```text
- **B4. Cross-CLI warm-reuse CLI exposure.** CLOSED — see
  `docs/superpowers/specs/2026-06-12-b4-cross-cli-warm-reuse-design.md`
  and `docs/superpowers/plans/2026-06-12-b4-cross-cli-warm-reuse-plan.md`.
```

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files README.md PROGRESS.md warm-reuse-tasks.txt
git add README.md PROGRESS.md warm-reuse-tasks.txt
git commit -m "$(cat <<'EOF'
docs(b4): operator warm-reuse README + PROGRESS + tasks closeout

Adds README "Operator warm-reuse" section documenting the
list → match cap_key → generate --instance-id discovery loop and the
--force-attach bypassable-verdict matrix. Strikes B4 in PROGRESS.md
and replaces the warm-reuse-tasks.txt starter with a closeout pointer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["README.md", "PROGRESS.md", "warm-reuse-tasks.txt"], "verifyCommand": "git diff HEAD~1 README.md PROGRESS.md warm-reuse-tasks.txt", "acceptanceCriteria": ["README has discovery loop + bypassable-verdict matrix", "PROGRESS B4 entry struck through with closeout pointer", "warm-reuse-tasks.txt B4 starter replaced with closeout line"]}
```

---

## Task 6: Full-suite green gate

**Goal:** Confirm the existing test suite remains green and the spec's AC1–AC12 are all satisfied.

**Files:** No source edits. Diagnostic-only.

**Acceptance Criteria:**
- [ ] `pixi run pytest` exits 0 with no new failures.
- [ ] `pixi run lint` exits 0.
- [ ] `pixi run typecheck` exits 0.
- [ ] Each spec AC1–AC12 maps to at least one passing test (manual checklist).

**Verify:** `pixi run pytest && pixi run lint && pixi run typecheck` → exit 0.

**Steps:**

- [ ] **Step 1: Full test suite**

```bash
pixi run pytest -x
```

Expected: exit 0. Any failure in non-B4 files indicates a regression; investigate before proceeding.

- [ ] **Step 2: Lint + typecheck**

```bash
pixi run lint
pixi run typecheck
```

Expected: exit 0 from each.

- [ ] **Step 3: AC walkthrough**

For each AC1–AC12 in `docs/superpowers/specs/2026-06-12-b4-cross-cli-warm-reuse-design.md` §6, identify the covering test:

| AC | Covering test |
|---|---|
| AC1 | `test_generate_warm_attach_passes_instance_kwarg`, `test_batch_warm_attach_single_pod_for_all_rows` |
| AC2 | `test_resolve_warm_instance.py::test_short_circuits_on_first_failure` + ordered per-step refusal tests |
| AC3 | All exit-code asserts in `test_resolve_warm_instance.py` |
| AC4 | `test_passes_on_*_with_force_attach` + `test_returns_2_on_STALE_LEDGER_even_with_force`, `test_returns_2_on_OVERAGE_REAP_even_with_force` |
| AC5 | `test_generate_force_attach_without_instance_id_exit_2`, `test_batch_force_attach_without_instance_id_exit_2` |
| AC6 | `test_cmd_list_includes_capability_key_column`, `test_cmd_list_prints_unknown_for_legacy_entry` |
| AC7 | No new lock acquire — verified by `git diff` of `core/orchestrator.py` (empty) |
| AC8 | `git diff src/kinoforge/core/` shows zero changes |
| AC9 | `test_batch_warm_attach_single_pod_for_all_rows` |
| AC10 | All stderr assertions in `test_resolve_warm_instance.py` |
| AC11 | `test_full_cli_warm_attach_smoke` |
| AC12 | README diff in Task 5 |

- [ ] **Step 4: No commit needed**

This task is a green gate, not a code change. If any AC fails, return to the relevant task to address.

```json:metadata
{"files": [], "verifyCommand": "pixi run pytest && pixi run lint && pixi run typecheck", "acceptanceCriteria": ["full pytest exits 0", "lint exits 0", "typecheck exits 0", "every AC1-AC12 maps to a passing test"]}
```

---

## Self-Review Notes

- **Spec coverage:** AC1–AC12 each mapped to a task (see Task 6 table). F1–F12 failure modes each covered by a test case in Task 0 or Task 1/2. Scope guardrails (no orchestrator changes, no lock acquire, $0 live spend) enforced by zero-diff requirement on `core/` in Task 6.
- **Placeholder scan:** No `TBD`/`TODO`/`implement later`. Every code block contains the exact text to write.
- **Type consistency:** `_resolve_warm_instance` signature stable across Tasks 0/1/2. `Instance` import added in Task 0 and reused in Task 1 (modify the `Instance` import location at module-top once, not redundantly).
- **Order dependency:** Task 0 is the foundation; Tasks 1+2+3 depend on it; Task 4 depends on 1+2; Task 5 is doc-only and can land any time after the implementation tasks; Task 6 is the green gate at the end.
- **Cap_key persist path:** Spec §3.3 step 3 was corrected at self-review time to read `entry["tags"]["kinoforge_key"]` (matches orchestrator.py:492,1015). Plan reflects the corrected schema everywhere.
