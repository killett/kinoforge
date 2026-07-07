# Dead-Pod Ledger-Ghost Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the top-of-every-command instance overview from printing confident dollar `est_spend` for pods that are already dead, by reconciling suspect ledger rows against the provider on read, forgetting pod-gone rows mid-run from the heartbeat loop, and relabelling the estimate honestly.

**Architecture:** Three independent changes over the existing ledger/overview/heartbeat code. (1) Extract the existing `_reconcile_dead_ledger_entries` into a shared module so both `kinoforge list` and the overview call one implementation. (2) `_print_instance_overview` age-gates entries (`age > max_age_s`) and reconciles only the suspect subset — young/live rows are never probed, preserving the offline-fast hot path. (3) The in-run `HeartbeatLoop` gains pod-gone detection via the C26 util `probe()` existence flag, forgetting + stopping the moment a host-reclaim is observed. Ledger is treated as a cache; provider is the source of truth for liveness.

**Tech Stack:** Python 3.13, pytest, existing kinoforge `cli/_commands.py`, `cli/_main.py`, `core/heartbeat_loop.py`, `core/reaper.py`, RunPod provider.

**User decisions (already made):**
- "Both reconcile + source" — read-side overview reconcile AND source-side heartbeat forget, shaped so the overview stays offline-fast (age-gated, best-effort). Quoted from brainstorm.
- Best foundation = ledger-as-cache validated against provider; reuse existing reconcile primitive (no duplicated logic); no new global config knob (reuse per-entry lifetime field); no real billing-API integration (estimate stays an honestly-labelled estimate).
- Suspect gate = `age > max_age_s` (per-entry ledger field), default fallback for legacy rows.

---

## File Structure

- **New:** `src/kinoforge/cli/_reconcile.py` — the shared reconcile primitive (`_reconcile_dead_ledger_entries`, `_RECONCILABLE_PROVIDERS`, `_ForgetLedger`) moved out of `_commands.py`. One responsibility: forget ledger rows the provider confirms gone.
- **Modify:** `src/kinoforge/cli/_commands.py` — delete the moved defs; import from `_reconcile`; `_cmd_list` uses the shared function (behavior unchanged).
- **Modify:** `src/kinoforge/cli/_main.py:908` — `_print_instance_overview`: age-gate + suspect reconcile + honest label + `⚠ unverified` marker.
- **Modify:** `src/kinoforge/core/reaper.py:26` — add `POD_GONE` to the `Verdict` enum (logging label).
- **Modify:** `src/kinoforge/core/heartbeat_loop.py` — factor the destroy+forget+cancel+stop tail of `_maybe_fire_reap` into `_reap_and_stop`; add pod-gone detection in `_tick_once` via util `probe()`.
- **Tests:** `tests/cli/test_reconcile_ledger.py` (import-path update), `tests/cli/test_instance_overview.py` (new), `tests/core/test_heartbeat_pod_gone.py` (new).

---

## Task 0: Extract shared reconcile primitive

**Goal:** Move `_reconcile_dead_ledger_entries` and its helpers to a new shared module so the overview (Task 1) and `_cmd_list` call one implementation — no duplicated logic.

**Files:**
- Create: `src/kinoforge/cli/_reconcile.py`
- Modify: `src/kinoforge/cli/_commands.py` (delete moved defs at ~1120-1185, add import, `_cmd_list` unchanged in behavior)
- Test: `tests/cli/test_reconcile_ledger.py` (update import path)

**Acceptance Criteria:**
- [ ] `_reconcile_dead_ledger_entries`, `_RECONCILABLE_PROVIDERS`, `_ForgetLedger` live in `cli/_reconcile.py`.
- [ ] `_commands.py` imports them; `_cmd_list` output is byte-identical to before.
- [ ] Existing `tests/cli/test_reconcile_ledger.py` passes unchanged except the import line.

**Verify:** `pixi run pytest tests/cli/test_reconcile_ledger.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Confirm what moves.** Read the current block in `src/kinoforge/cli/_commands.py` (the `_ForgetLedger` class ~1118, `_RECONCILABLE_PROVIDERS` ~1130, `_reconcile_dead_ledger_entries` ~1133-1185). Note the imports it needs: `argparse`/`Any`/`Callable` typing and `from kinoforge.core import registry` (imported lazily inside the function today).

- [ ] **Step 2: Create `src/kinoforge/cli/_reconcile.py`** with the moved code verbatim:

```python
"""Shared ledger reconciliation — forget rows the provider confirms gone.

Used by both ``kinoforge list`` (`_cmd_list`) and the top-of-command
instance overview (`_print_instance_overview`). One implementation, two
callers — a dead pod's ``est_spend`` (age×rate) must not inflate forever.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Providers whose ``get_instance(id)`` is authoritative ACROSS processes — a
# KeyError reliably means "this pod no longer exists". Only these are auto-
# reconciled. ``local`` is excluded: its instance table is in-process, so a
# fresh CLI invocation always KeyErrors on a valid pod.
_RECONCILABLE_PROVIDERS: frozenset[str] = frozenset({"runpod"})


class _ForgetLedger:
    """Minimal ledger surface the reconciler needs: just ``forget``."""

    def forget(self, instance_id: str) -> None:  # noqa: D102
        raise NotImplementedError


def _reconcile_dead_ledger_entries(
    ledger: _ForgetLedger,
    entries: list[dict[str, Any]],
    *,
    get_provider: Callable[[str], Callable[[], Any]] | None = None,
) -> list[str]:
    """Forget ledger entries whose pod the provider confirms is gone.

    For each entry, resolve its provider and call ``get_instance(id)``. A
    ``KeyError`` means the pod definitively does not exist provider-side, so the
    stale ledger entry is forgotten — otherwise its ``est_spend`` (age×rate) goes
    on inflating forever (2026-07-06: two 7-day-old dead pods showed ~$225 each).
    ANY other outcome (unknown provider, auth/transport error, live pod) is
    treated as uncertain and the entry is left untouched. Best-effort: never
    raises, so it can run inline on ``kinoforge list`` and the overview without a
    creds/network dependency becoming fatal.

    Args:
        ledger: Object exposing ``forget(instance_id)``.
        entries: Ledger entry dicts (each may carry ``id`` + ``provider``).
        get_provider: Injectable provider-factory resolver (test seam); defaults
            to :func:`kinoforge.core.registry.get_provider`.

    Returns:
        The ids that were confirmed gone and forgotten.
    """
    from kinoforge.core import registry

    resolve = get_provider if get_provider is not None else registry.get_provider
    forgotten: list[str] = []
    for entry in entries:
        pid = str(entry.get("id") or "")
        pname = str(entry.get("provider") or "")
        if not pid or pname not in _RECONCILABLE_PROVIDERS:
            continue
        try:
            provider = resolve(pname)()
        except Exception as exc:  # noqa: BLE001 — unknown/unresolvable provider
            logger.debug("reconcile: skip %s (provider %s: %s)", pid, pname, exc)
            continue
        try:
            provider.get_instance(pid)
        except KeyError:
            try:
                ledger.forget(pid)
                forgotten.append(pid)
            except Exception as exc:  # noqa: BLE001 — forget best-effort
                logger.debug("reconcile: forget %s failed: %s", pid, exc)
                continue
        except Exception as exc:  # noqa: BLE001 — auth/transport → uncertain, keep
            logger.debug("reconcile: probe %s uncertain, keeping: %s", pid, exc)
            continue
    return forgotten
```

- [ ] **Step 3: Delete the moved block from `_commands.py`** and add near the top imports:

```python
from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries
```

Leave `_cmd_list` calling `_reconcile_dead_ledger_entries(ledger, entries)` exactly as before. If any other symbol in `_commands.py` referenced `_RECONCILABLE_PROVIDERS` or `_ForgetLedger`, import those too; otherwise do not.

- [ ] **Step 4: Update the test import** in `tests/cli/test_reconcile_ledger.py`. Change any `from kinoforge.cli._commands import _reconcile_dead_ledger_entries` (and helper imports) to `from kinoforge.cli._reconcile import ...`.

- [ ] **Step 5: Run tests** — Run: `pixi run pytest tests/cli/test_reconcile_ledger.py -v` → Expected: all pass (byte-identical logic, only location changed).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/cli/_reconcile.py src/kinoforge/cli/_commands.py tests/cli/test_reconcile_ledger.py
git commit -m "refactor(cli): extract _reconcile_dead_ledger_entries to shared _reconcile module"
```

---

## Task 1: Age-gated suspect reconcile in the instance overview

**Goal:** `_print_instance_overview` reconciles only suspect rows (`age > max_age_s`) against the provider before printing, forgetting confirmed-gone pods so no command's startup shows a dead-pod ghost. Young/live rows are never probed.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (`_print_instance_overview`, ~908-943; add module const)
- Test: `tests/cli/test_instance_overview.py` (new)

**Acceptance Criteria:**
- [ ] An entry with `age <= max_age_s` (per-entry field; fallback `_OVERVIEW_STALE_AFTER_S` when absent) is NOT probed (provider resolver never called for it).
- [ ] A suspect entry (`age > max_age_s`) the provider confirms gone is forgotten and absent from printed output.
- [ ] A provider/network failure during reconcile does not raise; the overview still prints.
- [ ] The reconcile reuses the shared `_reconcile_dead_ledger_entries` from Task 0 (no new reconcile logic).

**Verify:** `pixi run pytest tests/cli/test_instance_overview.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** in `tests/cli/test_instance_overview.py`. These use a fake ledger + injected reconcile so no network. Behavior under test + concrete bug each catches is stated in the docstring per the `test-design` skill.

```python
"""Tests for _print_instance_overview reconcile + honest labelling."""

from __future__ import annotations

import io
import time

from kinoforge.cli import _main


class _FakeLedger:
    """In-memory ledger stub exposing entries()/forget()."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = list(rows)

    def entries(self) -> list[dict]:
        return list(self._rows)

    def forget(self, iid: str) -> None:
        self._rows = [r for r in self._rows if str(r.get("id")) != iid]


class _Ctx:
    def __init__(self, ledger: _FakeLedger) -> None:
        self._ledger = ledger

    def ledger_safe(self):  # noqa: ANN201
        return self._ledger, None


def _run(ctx, monkeypatch, resolver) -> str:
    """Invoke the overview with an injected provider resolver, capture stdout."""
    monkeypatch.setattr(_main, "_overview_get_provider", resolver, raising=False)
    buf = io.StringIO()
    _main._print_instance_overview(ctx, file=buf)
    return buf.getvalue()


def test_young_entry_is_not_probed(monkeypatch):
    """A row younger than max_age_s must never hit the provider.

    Bug caught: an unconditional reconcile that probes every row would add a
    network round-trip to the hot warm-reuse path and call the resolver here.
    """
    now = time.time()
    ledger = _FakeLedger(
        [{"id": "young1", "provider": "runpod",
          "created_at": now - 60, "max_age_s": 3600,
          "cost_rate_usd_per_hr": 1.0}]
    )
    called: list[str] = []

    def resolver(name):
        called.append(name)
        raise AssertionError("young row must not be probed")

    out = _run(_Ctx(ledger), monkeypatch, resolver)
    assert called == []
    assert "young1" in out


def test_suspect_gone_entry_is_forgotten_and_dropped(monkeypatch):
    """A suspect row the provider 404s on is forgotten and not printed.

    Bug caught: overview prints a dead pod's inflating est_spend forever.
    """
    now = time.time()
    ledger = _FakeLedger(
        [{"id": "ghost1", "provider": "runpod",
          "created_at": now - 200 * 3600, "max_age_s": 3600,
          "cost_rate_usd_per_hr": 1.19}]
    )

    class _GoneProvider:
        def get_instance(self, iid):
            raise KeyError(iid)

    out = _run(_Ctx(ledger), monkeypatch, lambda name: (lambda: _GoneProvider()))
    assert "ghost1" not in out
    assert "No running instances" in out


def test_reconcile_failure_does_not_raise(monkeypatch):
    """A resolver/network explosion must not crash the overview.

    Bug caught: a bare provider error at the top of every command aborts the
    whole CLI invocation instead of degrading to a printed row.
    """
    now = time.time()
    ledger = _FakeLedger(
        [{"id": "ghost2", "provider": "runpod",
          "created_at": now - 200 * 3600, "max_age_s": 3600,
          "cost_rate_usd_per_hr": 1.0}]
    )

    def boom(name):
        raise RuntimeError("network down")

    out = _run(_Ctx(ledger), monkeypatch, boom)
    assert "ghost2" in out  # kept, printed, no crash
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/cli/test_instance_overview.py -v` → Expected: FAIL (`_overview_get_provider` / reconcile wiring not present; young-probe + drop behavior absent).

- [ ] **Step 3: Implement** in `src/kinoforge/cli/_main.py`. Add the import and module const near the top:

```python
from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

#: Fallback suspect-age threshold when a ledger row lacks ``max_age_s``
#: (legacy rows). A row older than this is probed against the provider before
#: its est_spend is printed. Probing a still-live long-lived pod merely
#: confirms it alive (get_instance succeeds → kept), so this is a
#: performance knob, never a correctness one.
_OVERVIEW_STALE_AFTER_S: float = 6 * 3600.0


def _overview_get_provider(name: str):  # noqa: ANN201
    """Resolve a provider factory by name (test seam; patched in unit tests).

    Ensures the runpod provider is registered before resolving, so the
    top-of-command overview reconcile works even when no subcommand has
    imported the provider yet.
    """
    import kinoforge.providers.runpod  # noqa: F401 — self-registers
    from kinoforge.core import registry

    return registry.get_provider(name)
```

Then rewrite the body of `_print_instance_overview` between fetching `entries` and the print loop so it reconciles the suspect subset:

```python
    now = time.time()
    # Read-side self-heal: reconcile only SUSPECT rows (older than their own
    # max_age_s reap deadline, or a default for legacy rows). Young/live rows
    # are never probed, so the warm-reuse hot path stays zero-network. Any
    # failure degrades gracefully — the overview must never raise.
    suspect = [
        e for e in entries
        if now - float(e.get("created_at", now))
        > float(e.get("max_age_s", _OVERVIEW_STALE_AFTER_S))
    ]
    if suspect:
        try:
            gone = _reconcile_dead_ledger_entries(
                ledger, suspect, get_provider=_overview_get_provider
            )
            if gone:
                gone_set = set(gone)
                entries = [
                    e for e in entries if str(e.get("id") or "") not in gone_set
                ]
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            print(
                f"[instance overview] reconcile skipped: "
                f"{type(exc).__name__}: {exc}",
                file=out,
            )
    if not entries:
        print("[instance overview] No running instances.", file=out)
        return
    print("[instance overview]", file=out)
    for entry in entries:
        ...  # existing per-entry print loop (label change lands in Task 2)
```

Note: `_reconcile_dead_ledger_entries` is already best-effort internally, but the surrounding `try/except` guards the `_overview_get_provider` import/registration path too. Keep the existing `ledger_safe()` guard above unchanged.

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/cli/test_instance_overview.py -v` → Expected: PASS.

- [ ] **Step 5: Guard the reconcile-move regression** — Run: `pixi run pytest tests/cli/test_reconcile_ledger.py tests/cli/ -v` → Expected: PASS (overview change did not disturb `list`).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/cli/_main.py tests/cli/test_instance_overview.py
git commit -m "fix(cli): reconcile suspect ledger rows in instance overview (age-gated, best-effort) — kills dead-pod est_spend ghosts on every command"
```

---

## Task 2: Honest est_spend label + offline unverified marker

**Goal:** The overview never again presents `age × rate` as a confident bill. Relabel it as an upper-bound estimate, and flag suspect rows that survived reconcile (e.g. provider unreachable — could not confirm gone) with `⚠ unverified`.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (per-entry print loop in `_print_instance_overview`)
- Test: `tests/cli/test_instance_overview.py` (extend)

**Acceptance Criteria:**
- [ ] Each printed entry line labels spend as an estimate/upper-bound, not a bare `est_spend=$X`.
- [ ] A suspect row still present after reconcile (not confirmed gone) carries `⚠ unverified — run 'kinoforge list'`.
- [ ] A young/live row does NOT carry the `⚠ unverified` marker.

**Verify:** `pixi run pytest tests/cli/test_instance_overview.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests** — extend `tests/cli/test_instance_overview.py`:

```python
def test_est_spend_is_labelled_as_estimate(monkeypatch):
    """The spend figure prints as an explicit upper-bound estimate.

    Bug caught: a bare 'est_spend=$225' reads as a real charge and alarms the
    operator, which is the whole defect this feature fixes.
    """
    now = time.time()
    ledger = _FakeLedger(
        [{"id": "live1", "provider": "runpod",
          "created_at": now - 60, "max_age_s": 3600,
          "cost_rate_usd_per_hr": 1.0}]
    )
    out = _run(_Ctx(ledger), monkeypatch, lambda name: (_ for _ in ()).throw(AssertionError))
    assert "est" in out
    assert "$0 if pod" in out  # honest caveat present


def test_offline_suspect_row_marked_unverified(monkeypatch):
    """A suspect row that reconcile could not confirm gone is flagged.

    Bug caught: with the provider unreachable, a real ghost is printed with a
    confident number and no hint that it may be dead.
    """
    now = time.time()
    ledger = _FakeLedger(
        [{"id": "maybe_ghost", "provider": "runpod",
          "created_at": now - 200 * 3600, "max_age_s": 3600,
          "cost_rate_usd_per_hr": 1.0}]
    )

    def boom(name):
        raise RuntimeError("network down")

    out = _run(_Ctx(ledger), monkeypatch, boom)
    assert "maybe_ghost" in out
    assert "unverified" in out
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/cli/test_instance_overview.py::test_est_spend_is_labelled_as_estimate tests/cli/test_instance_overview.py::test_offline_suspect_row_marked_unverified -v` → Expected: FAIL.

- [ ] **Step 3: Implement** the per-entry print loop in `_print_instance_overview`. Compute the suspect flag per row and change the printed line:

```python
    print("[instance overview]", file=out)
    for entry in entries:
        iid = entry.get("id", "?")
        created_at = float(entry.get("created_at", now))
        age_s = now - created_at
        age_h = age_s / 3600.0
        rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
        spend = age_h * rate
        max_age_s = float(entry.get("max_age_s", _OVERVIEW_STALE_AFTER_S))
        suspect = age_s > max_age_s
        marker = "  ⚠ unverified — run 'kinoforge list'" if suspect else ""
        print(
            f"  {iid}  age={age_h:.1f}h  "
            f"est≤${spend:.4f} (age×rate; $0 if pod already dead)"
            f"{marker}",
            file=out,
        )
```

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/cli/test_instance_overview.py -v` → Expected: PASS (all, including Task 1's three).

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/_main.py tests/cli/test_instance_overview.py
git commit -m "fix(cli): label overview spend as upper-bound estimate + mark offline-unverified suspect rows"
```

---

## Task 3: Source fix — heartbeat forgets on observed pod-death

**Goal:** The in-run `HeartbeatLoop` forgets the ledger entry + stops the moment its util probe confirms the pod no longer exists (host-reclaim), so a mid-run death is cleaned before the operator kills a hung driver — instead of orphaning a row that inflates forever.

**Files:**
- Modify: `src/kinoforge/core/reaper.py:26` (add `POD_GONE` to `Verdict`)
- Modify: `src/kinoforge/core/heartbeat_loop.py` (factor `_reap_and_stop`; add pod-gone detection in `_tick_once`)
- Test: `tests/core/test_heartbeat_pod_gone.py` (new)

**Acceptance Criteria:**
- [ ] When the util endpoint's `probe()` returns `exists is False`, the loop forgets the entry, sets the cancel token, and stops.
- [ ] When `probe()` returns `exists is True` (or `(True, None)` null-runtime), no reap fires.
- [ ] When `_util_endpoint is None`, no existence probe and no reap (heartbeat_mode:none path unchanged, zero added network).
- [ ] `Verdict.POD_GONE` exists and is used as the log label.

**Verify:** `pixi run pytest tests/core/test_heartbeat_pod_gone.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Add the verdict.** In `src/kinoforge/core/reaper.py`, inside `class Verdict(StrEnum)` (after `RESTART_LOOP_REAP` ~line 42):

```python
    POD_GONE = "POD_GONE"  # 2026-07-06 — provider confirmed pod absent mid-run
```

- [ ] **Step 2: Write the failing tests** in `tests/core/test_heartbeat_pod_gone.py`. Build a `HeartbeatLoop` with fakes and drive one `_tick_once`.

```python
"""Heartbeat pod-gone detection: forget + stop when probe() says not-found."""

from __future__ import annotations

import threading

from kinoforge.core.heartbeat_loop import HeartbeatLoop


class _FakeProvider:
    def heartbeat(self, iid): ...
    def last_heartbeat(self, iid): return None
    def destroy_instance(self, iid): ...


class _RecordingLedger:
    def __init__(self): self.forgotten = []
    def touch(self, iid, **kw): ...
    def forget(self, iid): self.forgotten.append(iid)


class _Probe:
    """UtilSnapshotEndpoint stub with a settable probe() return."""

    def __init__(self, exists: bool):
        self._exists = exists
    def read_util(self, iid): return None
    def probe(self, iid): return (self._exists, None)


def _make_loop(*, exists: bool, ledger, cancel):
    return HeartbeatLoop(
        instance_id="pod1",
        provider=_FakeProvider(),
        ledger=ledger,
        interval_s=1.0,
        util_endpoint=_Probe(exists),
        cancel_token=cancel,
        provider_kind="runpod",
        # STALL/RESTART windows off — isolate pod-gone behavior:
        stall_window_s=None,
        restart_loop_window_s=None,
    )


def test_probe_not_found_forgets_and_stops():
    """probe(exists=False) → forget + cancel set + loop stop.

    Bug caught: a host-reclaimed pod is swallowed by _tick_once's broad
    except and its ledger row orphans, inflating est_spend forever.
    """
    ledger = _RecordingLedger()
    cancel = threading.Event()
    loop = _make_loop(exists=False, ledger=ledger, cancel=cancel)
    loop._tick_once()
    assert ledger.forgotten == ["pod1"]
    assert cancel.is_set()
    assert loop._stop.is_set()


def test_probe_exists_does_not_reap():
    """probe(exists=True) → no forget, loop keeps running.

    Bug caught: a live pod wrongly forgotten mid-run kills a good session.
    """
    ledger = _RecordingLedger()
    cancel = threading.Event()
    loop = _make_loop(exists=True, ledger=ledger, cancel=cancel)
    loop._tick_once()
    assert ledger.forgotten == []
    assert not cancel.is_set()
    assert not loop._stop.is_set()


def test_no_util_endpoint_no_probe():
    """util_endpoint=None → no existence probe, no reap.

    Bug caught: adding an unconditional probe would break the tuned
    heartbeat_mode:none path and add a network call every tick.
    """
    ledger = _RecordingLedger()
    cancel = threading.Event()
    loop = HeartbeatLoop(
        instance_id="pod1", provider=_FakeProvider(), ledger=ledger,
        interval_s=1.0, util_endpoint=None, cancel_token=cancel,
        provider_kind="runpod", stall_window_s=None, restart_loop_window_s=None,
    )
    loop._tick_once()
    assert ledger.forgotten == []
    assert not loop._stop.is_set()
```

Adjust the `HeartbeatLoop(...)` kwargs in `_make_loop` to match the real constructor signature (read `heartbeat_loop.py:121-188`); the fields referenced above (`instance_id`, `provider`, `ledger`, `interval_s`, `util_endpoint`, `cancel_token`, `provider_kind`, `stall_window_s`, `restart_loop_window_s`) are the ones under test.

- [ ] **Step 3: Run to confirm failure** — Run: `pixi run pytest tests/core/test_heartbeat_pod_gone.py -v` → Expected: FAIL (no pod-gone detection; probe() not consulted for existence).

- [ ] **Step 4: Factor `_reap_and_stop`.** In `heartbeat_loop.py`, extract the destroy+forget+cancel+stop tail of `_maybe_fire_reap` (lines ~381-401) into a reusable method, and have `_maybe_fire_reap` call it:

```python
    def _reap_and_stop(self, verdict: "Verdict") -> None:
        """Destroy (best-effort) + forget the entry + signal cancel + stop.

        Shared by STALL/RESTART_LOOP reaps and POD_GONE detection.
        """
        destroy = getattr(self._provider, "destroy_instance", None)
        if destroy is not None:
            try:
                destroy(self._instance_id)
            except Exception:  # noqa: BLE001 — best-effort destroy
                self._logger.exception(
                    "%s destroy failed for %s", verdict.value, self._instance_id
                )
        forget = getattr(self._ledger, "forget", None)
        if forget is not None:
            try:
                forget(self._instance_id)
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "%s ledger.forget failed for %s", verdict.value, self._instance_id
                )
        if self._cancel_token is not None:
            self._cancel_token.set()
        self._stop.set()
```

Replace lines ~381-401 in `_maybe_fire_reap` with `self._reap_and_stop(verdict)` (keep the warning log above it).

- [ ] **Step 5: Add pod-gone detection** in `_tick_once`. Add a helper that probes existence via the util endpoint's `probe()` (only when the endpoint exposes it), and call it inside the `try` block after `self._ledger.touch(...)`:

```python
    def _pod_confirmed_gone(self) -> bool:
        """True iff the util endpoint's probe() reports the pod not-found.

        Uses probe() (returns (exists, snap)) rather than read_util() (which
        conflates 'gone' and 'null runtime' as None). Only runs when the
        endpoint exposes probe(); returns False on any error (uncertain →
        keep, never a false reap).
        """
        probe = getattr(self._util_endpoint, "probe", None)
        if self._util_endpoint is None or probe is None:
            return False
        try:
            exists, _snap = probe(self._instance_id)
        except Exception:  # noqa: BLE001 — transport/auth uncertain → keep
            return False
        return exists is False
```

In `_tick_once`, after the `self._ledger.touch(...)` call and before/alongside the `_maybe_fire_reap` call, add:

```python
            if self._pod_confirmed_gone():
                from kinoforge.core.reaper import Verdict  # noqa: PLC0415
                self._logger.warning(
                    "POD_GONE fired for %s (provider confirmed absent)",
                    self._instance_id,
                )
                self._reap_and_stop(Verdict.POD_GONE)
                return
            if self._util_endpoint is not None:
                self._maybe_fire_reap(now=now)
```

(The `return` prevents a STALL check after we've already stopped.)

- [ ] **Step 6: Run tests** — Run: `pixi run pytest tests/core/test_heartbeat_pod_gone.py -v` → Expected: PASS.

- [ ] **Step 7: Guard heartbeat regressions** — Run: `pixi run pytest tests/core/ -k heartbeat -v` → Expected: PASS (existing STALL/RESTART tests still green after the `_reap_and_stop` extraction).

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/reaper.py src/kinoforge/core/heartbeat_loop.py tests/core/test_heartbeat_pod_gone.py
git commit -m "fix(heartbeat): POD_GONE — forget + stop when util probe confirms pod absent mid-run (source fix for orphan ledger rows)"
```

---

## Task 4: Full-suite + lint/type green, update PROGRESS

**Goal:** Confirm nothing regressed across the whole suite, lint/type clean, and record the fix in PROGRESS.

**Files:**
- Modify: `PROGRESS.md` (RESUME SNAPSHOT — note the ghost fix shipped)

**Acceptance Criteria:**
- [ ] `pixi run test` green (no new failures vs. the 3882-passed baseline plus the new tests).
- [ ] `pixi run lint` and `pixi run typecheck` clean on the touched files.
- [ ] `pixi run pre-commit run --all-files` passes.
- [ ] PROGRESS RESUME SNAPSHOT mentions the dead-pod ghost fix + spec/plan paths.

**Verify:** `pixi run pre-commit run --all-files` → all hooks pass; `pixi run test` → green.

**Steps:**

- [ ] **Step 1: Full suite** — Run: `pixi run test` → Expected: green; note the passed count (was 3882 + the new tests).
- [ ] **Step 2: Lint + type** — Run: `pixi run lint` then `pixi run typecheck` → Expected: clean.
- [ ] **Step 3: Update PROGRESS.md** RESUME SNAPSHOT with a short entry: dead-pod ledger-ghost fix shipped (read-side overview reconcile + heartbeat POD_GONE + honest est label); point to `docs/superpowers/specs/2026-07-06-dead-pod-ledger-ghosts-design.md` and this plan.
- [ ] **Step 4: pre-commit + commit**

```bash
pixi run pre-commit run --all-files
git add PROGRESS.md
git commit -m "docs(progress): dead-pod ledger-ghost fix shipped (overview reconcile + heartbeat POD_GONE + honest est label)"
```

---

## Self-Review

**Spec coverage:**
- Spec Part 1 (read-side self-heal, age-gated, shared primitive) → Task 0 (extract) + Task 1 (age-gate + suspect reconcile). ✓
- Spec Part 2 (write-side POD_GONE in heartbeat) → Task 3. ✓
- Spec Part 3 (honest label + offline ⚠ marker) → Task 2. ✓
- Spec Testing section → tests in Tasks 1/2/3; move-regression in Task 0/1; full suite in Task 4. ✓
- Spec Non-goals (no billing API, no new global config, no teardown change, runpod-only) → honored: reuse `max_age_s` + `_OVERVIEW_STALE_AFTER_S` default (no config knob), `_RECONCILABLE_PROVIDERS` unchanged, teardown untouched. ✓

**Placeholder scan:** No TBD/TODO; every code step shows the code. The one deferred detail (exact `_make_loop` kwargs) instructs the engineer to match the real constructor signature at `heartbeat_loop.py:121-188` — grounded, not vague.

**Type consistency:** `_reconcile_dead_ledger_entries(ledger, entries, *, get_provider=...)` signature identical across Task 0 def, Task 1 call. `_reap_and_stop(verdict)` defined and called with `Verdict` in Task 3. `max_age_s` / `_OVERVIEW_STALE_AFTER_S` used consistently in Tasks 1 and 2. `probe()` returns `(exists, snap)` per RunPod `util.py:173`.

**Note for implementer:** the ledger persists the lifetime field as `max_age_s` (see `_CFG_LIFECYCLE_ATTR` at `_commands.py:1230`, which maps `max_age_s → max_lifetime_s`). Read the field as `max_age_s`; do not use `max_lifetime_s` as an entry key.
