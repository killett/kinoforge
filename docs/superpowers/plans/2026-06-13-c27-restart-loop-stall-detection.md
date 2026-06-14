# C27 — Restart-loop stall detection: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers-extended-cc:subagent-driven-development` (recommended) or `superpowers-extended-cc:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a sibling predicate on the C26 util substrate that classifies chronic container-restart loops as `RESTART_LOOP_REAP` and tears the pod down, closing the deferred C25 Task 4 / C26 Task 14 gate.

**Architecture:** Pure-additive extension of C26. One new pure counter (`_update_uptime_counter`), one new pure predicate (`_restart_loop_reap_predicate`), one new `Verdict` (`RESTART_LOOP_REAP`), three new `LifecycleConfig` knobs, one new ledger field (`consecutive_low_uptime_count`), one new per-entry override key (`restart_loop_window_s`), one new CLI flag (`--restart-loop-window-override`). No new wire path. No new provider code. No ledger schema migration.

**Tech Stack:** Python 3.13, pydantic v2, pytest, ruff, mypy, RunPod GraphQL (already integrated in C26).

**Spec:** `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md`

---

## File Structure

### New files

| Path | Responsibility |
| ---- | -------------- |
| `tests/live/_c27_fake_util_endpoint.py` | `FakeUtilEndpoint` test helper returning a fixed `UtilSnapshot` (used by Phase A1). |
| `tests/live/test_c27_phase_a1_uptime_streak_live.py` | Phase A1 live smoke — FakeUtilEndpoint forces `uptime_seconds=1` to drive predicate fire. |
| `tests/live/test_c27_phase_a2_alpine_restart_loop_live.py` | Phase A2 live smoke — alpine pod with `sh -c 'sleep 5; exit 1'` to force real RunPod restart churn. |
| `tests/live/test_c27_phase_b_wan_warm_reuse_live.py` | Phase B live smoke — Wan + ComfyUI re-fire of deferred C25/C26 gate. |
| `tests/live/_c27_phase_a1_evidence.json` | Sidecar evidence (committed after Phase A1 spend). |
| `tests/live/_c27_phase_a2_evidence.json` | Sidecar evidence (committed after Phase A2 spend). |
| `tests/live/_c27_phase_b_evidence.json` | Sidecar evidence (committed after Phase B spend). |

### Modified files

| Path | Change |
| ---- | ------ |
| `src/kinoforge/core/util_counter.py` | Add `_update_uptime_counter` pure function. |
| `src/kinoforge/core/reaper.py` | Append `Verdict.RESTART_LOOP_REAP`; add to `DEFAULT_APPLY_POLICY`; add `_restart_loop_reap_predicate`; widen `classify()` signature; insert row 3'' branch. |
| `src/kinoforge/core/heartbeat_loop.py` | Add three new `__init__` kwargs; add `_uptime_counter` instance state; thread counter through `_tick_once` + `_build_util_extra`; rename `_maybe_fire_stall_reap` → `_maybe_fire_reap` and route both predicates. |
| `src/kinoforge/core/config.py` | Add three new `LifecycleConfig` fields + two validators; extend `Config.lifecycle()` collapse. |
| `src/kinoforge/core/interfaces.py` | Add `restart_loop_window_s: float \| None` + `restart_loop_uptime_threshold_s: float` to `Lifecycle`. |
| `src/kinoforge/_adapters.py` | Update `build_util_endpoint_for` kill-switch check to gate on (`stall_reap_enabled` AND `restart_loop_reap_enabled`) both being False. |
| `src/kinoforge/cli/_commands.py` | Add `--restart-loop-window-override` to `deploy`; ledger.touch persist; thread new kwargs through HeartbeatLoop construction sites (lines 736-738, 1020-1022, 1354-1356, 1683-1713). |
| `src/kinoforge/cli/_main.py` | Wire new CLI flag through `deploy` argparser. |
| `tests/test_util_counter.py` | New TestClass for `_update_uptime_counter`. |
| `tests/test_reaper.py` | New TestClass for `_restart_loop_reap_predicate`; extend classify TestClass with row 3'' cases. |
| `tests/test_heartbeat_loop.py` | Extend existing TestClasses with both-routes wiring + counter persistence cases. |
| `tests/test_config.py` | Extend `LifecycleConfig` tests with three new fields + two validators; extend `Config.lifecycle()` collapse tests. |
| `tests/test_cli.py` | Extend deploy-CLI tests with `--restart-loop-window-override` persist case. |
| `PROGRESS.md` | Append §C C27 closeout line. |
| `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md` | Fill §13 closeout after Phase B closes. |
| `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` | Append §17 cross-reference pointer. |

---

## Task 1: `Verdict.RESTART_LOOP_REAP` + `DEFAULT_APPLY_POLICY`

**Goal:** Append `RESTART_LOOP_REAP` to the `Verdict` StrEnum and add it to `DEFAULT_APPLY_POLICY`. Pure additive, no behaviour change yet — predicate not wired.

**Files:**
- Modify: `src/kinoforge/core/reaper.py` (enum + policy)
- Test: `tests/test_reaper.py` (TestVerdictEnum + TestDefaultApplyPolicy)

**Acceptance Criteria:**
- [ ] `Verdict.RESTART_LOOP_REAP` exists with string value `"RESTART_LOOP_REAP"`.
- [ ] Member appears AFTER `STALL_REAP` in enum declaration order (insertion contract).
- [ ] `DEFAULT_APPLY_POLICY.act_verdicts` contains the new Verdict.
- [ ] Existing C26 members untouched; existing tests pass.

**Verify:** `pixi run -- pytest tests/test_reaper.py -v -k 'TestVerdict or TestDefault'` → all pass.

**Steps:**

- [ ] **Step 1: Add failing test for the enum member**

Edit `tests/test_reaper.py` — append inside the existing TestVerdictEnum class (or create one if absent):

```python
def test_restart_loop_reap_verdict_exists_after_stall_reap(self) -> None:
    """C27: RESTART_LOOP_REAP appended after STALL_REAP, honouring insertion order."""
    from kinoforge.core.reaper import Verdict

    assert Verdict.RESTART_LOOP_REAP.value == "RESTART_LOOP_REAP"
    members = list(Verdict)
    assert members.index(Verdict.RESTART_LOOP_REAP) > members.index(Verdict.STALL_REAP)
```

- [ ] **Step 2: Add failing test for the DEFAULT_APPLY_POLICY inclusion**

```python
def test_default_apply_policy_contains_restart_loop_reap(self) -> None:
    """C27: DEFAULT_APPLY_POLICY acts on RESTART_LOOP_REAP."""
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict

    assert Verdict.RESTART_LOOP_REAP in DEFAULT_APPLY_POLICY.act_verdicts
```

- [ ] **Step 3: Run tests — confirm RED**

Run: `pixi run -- pytest tests/test_reaper.py::TestVerdictEnum::test_restart_loop_reap_verdict_exists_after_stall_reap tests/test_reaper.py::TestDefaultApplyPolicy::test_default_apply_policy_contains_restart_loop_reap -v`
Expected: FAIL — `AttributeError: RESTART_LOOP_REAP` and/or assertion error.

- [ ] **Step 4: Add the enum member + policy entry**

Edit `src/kinoforge/core/reaper.py`:

In the `Verdict` StrEnum, after `STALL_REAP = "STALL_REAP"  # C26`, append:

```python
    RESTART_LOOP_REAP = "RESTART_LOOP_REAP"  # C27
```

In the `DEFAULT_APPLY_POLICY = Policy(...)` block, after `Verdict.STALL_REAP,  # C26`, append:

```python
            Verdict.RESTART_LOOP_REAP,  # C27
```

- [ ] **Step 5: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_reaper.py -v`
Expected: PASS, including all pre-existing tests.

- [ ] **Step 6: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/reaper.py tests/test_reaper.py
git add src/kinoforge/core/reaper.py tests/test_reaper.py
git commit -m "feat(c27): add Verdict.RESTART_LOOP_REAP + DEFAULT_APPLY_POLICY entry

Pure additive. Member appended after STALL_REAP per insertion-order
contract. Predicate + classify wiring follow in Task 4.

Spec docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md §4.3.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `_update_uptime_counter` pure function

**Goal:** Add the sibling pure counter to `util_counter.py`. Table-driven tests cover all branches.

**Files:**
- Modify: `src/kinoforge/core/util_counter.py` (add `_update_uptime_counter`)
- Test: `tests/test_util_counter.py` (new TestClass)

**Acceptance Criteria:**
- [ ] `_update_uptime_counter` exported from `util_counter.py` (added to `__all__`).
- [ ] Pure function — no I/O, no module-level state.
- [ ] All 8 table-driven cases pass per spec §7.1.
- [ ] Existing `_update_counter` behaviour and tests untouched.

**Verify:** `pixi run -- pytest tests/test_util_counter.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing table-driven test**

Append to `tests/test_util_counter.py` (create file imports if needed at the top):

```python
import pytest


class TestUpdateUptimeCounter:
    """C27: consecutive-low-uptime counter pure state machine."""

    @pytest.mark.parametrize(
        "name, prev, snap, uptime_threshold, expected",
        [
            ("transport hiccup preserves at high counter", 9, None, 90.0, 9),
            ("snap with uptime_seconds=None resets", 5, "SNAP_UPTIME_NONE", 90.0, 0),
            ("uptime strictly < threshold increments", 3, "SNAP_UPTIME_89", 90.0, 4),
            ("uptime == threshold resets (strict <)", 3, "SNAP_UPTIME_90", 90.0, 0),
            ("uptime > threshold resets", 7, "SNAP_UPTIME_200", 90.0, 0),
            ("float-equal threshold edge", 0, "SNAP_UPTIME_89_9999", 90.0, 1),
            ("fresh tick uptime=1 always counts", 0, "SNAP_UPTIME_1", 90.0, 1),
            ("extreme threshold (0) blocks all", 5, "SNAP_UPTIME_1", 0.0, 0),
        ],
    )
    def test_counter_table(
        self, name: str, prev: int, snap: object, uptime_threshold: float, expected: int
    ) -> None:
        """Each row asserts the state machine returns the expected counter value."""
        from kinoforge.core.util_counter import _update_uptime_counter
        from kinoforge.core.util_endpoints import UtilSnapshot

        snap_map: dict[str, UtilSnapshot | None] = {
            "SNAP_UPTIME_NONE": UtilSnapshot(None, None, None, None, None),
            "SNAP_UPTIME_89": UtilSnapshot(None, None, None, None, 89),
            "SNAP_UPTIME_90": UtilSnapshot(None, None, None, None, 90),
            "SNAP_UPTIME_200": UtilSnapshot(None, None, None, None, 200),
            "SNAP_UPTIME_89_9999": UtilSnapshot(None, None, None, None, 89),  # int rounds down
            "SNAP_UPTIME_1": UtilSnapshot(None, None, None, None, 1),
        }
        resolved_snap = None if snap is None else snap_map[snap]  # type: ignore[index]
        result = _update_uptime_counter(
            prev, snap=resolved_snap, uptime_threshold_s=uptime_threshold
        )
        assert result == expected, f"case={name!r} got {result} want {expected}"
```

Note: `UtilSnapshot.uptime_seconds` is `int | None`. The `89_9999` row is mechanically `89` after coercion — it tests "very close to threshold from below stays below". A true float edge would need refactoring the dataclass; out of scope for C27.

- [ ] **Step 2: Run test — confirm RED**

Run: `pixi run -- pytest tests/test_util_counter.py::TestUpdateUptimeCounter -v`
Expected: FAIL — `ImportError: cannot import name '_update_uptime_counter'`.

- [ ] **Step 3: Add the function**

Edit `src/kinoforge/core/util_counter.py`. Update `__all__` and append the new function below `_update_counter`:

```python
__all__ = ["_update_counter", "_update_uptime_counter"]


# ... existing _update_counter unchanged ...


def _update_uptime_counter(
    prev_counter: int,
    *,
    snap: UtilSnapshot | None,
    uptime_threshold_s: float,
) -> int:
    """Tick the consecutive-low-uptime counter (C27).

    Pure function. No I/O, no side effects. Called from
    HeartbeatLoop._tick_once each tick alongside _update_counter.

    Semantics:
      - snap is None (transport hiccup): preserve prev_counter.
      - snap.uptime_seconds is None: reset to 0 (provider not surfacing).
      - snap.uptime_seconds < uptime_threshold_s: increment.
      - else: reset to 0.

    Differs from _update_counter (C26):
      - No prev_uptime_s parameter — chronic restart loop IS the signal,
        not a restart-blip the predicate is trying to filter out.
      - Single-axis read of uptime_seconds (no gpu/cpu AND-clause).
      - uptime_seconds=None resets (silence the predicate if the provider
        stops surfacing uptime mid-loop) rather than preserve.

    Args:
        prev_counter: The previous tick's counter value.
        snap: This tick's util snapshot, or None on transport failure.
        uptime_threshold_s: Strictly-< threshold below which the tick
            counts as 'low uptime'.

    Returns:
        The new counter value.
    """
    if snap is None:
        return prev_counter
    if snap.uptime_seconds is None:
        return 0
    if snap.uptime_seconds < uptime_threshold_s:
        return prev_counter + 1
    return 0
```

- [ ] **Step 4: Run test — confirm GREEN**

Run: `pixi run -- pytest tests/test_util_counter.py -v`
Expected: PASS — all 8 rows + all pre-existing `_update_counter` tests.

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/util_counter.py tests/test_util_counter.py
git add src/kinoforge/core/util_counter.py tests/test_util_counter.py
git commit -m "feat(c27): add _update_uptime_counter pure state machine

Twin of _update_counter for the C27 low-uptime streak predicate.
Single-axis (uptime_seconds), no restart-blip filter, snap=None
preserves, uptime_seconds=None resets.

Spec §4.1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `_restart_loop_reap_predicate` pure function

**Goal:** Add the sibling predicate to `reaper.py`. Same defensive shape as `_stall_reap_predicate`.

**Files:**
- Modify: `src/kinoforge/core/reaper.py` (add predicate)
- Test: `tests/test_reaper.py` (new TestClass)

**Acceptance Criteria:**
- [ ] `_restart_loop_reap_predicate` defined module-level in `reaper.py`.
- [ ] All 12 table cases from spec §7.1 pass.
- [ ] Existing `_stall_reap_predicate` behaviour untouched.

**Verify:** `pixi run -- pytest tests/test_reaper.py::TestRestartLoopReapPredicate -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing table-driven test**

Append to `tests/test_reaper.py`:

```python
import pytest


class TestRestartLoopReapPredicate:
    """C27: _restart_loop_reap_predicate decision table."""

    NOW = 1_000_000.0
    INTERVAL = 30.0
    SENTINEL_WINDOW = 3.0 * INTERVAL  # 90s

    @pytest.mark.parametrize(
        "name, entry, restart_loop_window_s, expected",
        [
            (
                "feature off via None window",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 999,
                 "util_thread_tick": 1_000_000.0},
                None,
                False,
            ),
            (
                "substrate-unsupported provider 'fal'",
                {"id": "x", "provider": "fal",
                 "consecutive_low_uptime_count": 999,
                 "util_thread_tick": 1_000_000.0},
                10.0,
                False,
            ),
            (
                "substrate-unknown provider (legacy entry, no provider key)",
                {"id": "x",
                 "consecutive_low_uptime_count": 20,
                 "util_thread_tick": 1_000_000.0},
                10.0,
                True,
            ),
            (
                "legacy entry no counter",
                {"id": "x", "provider": "runpod",
                 "util_thread_tick": 1_000_000.0},
                10.0,
                False,
            ),
            (
                "legacy entry no util_tick",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 20},
                10.0,
                False,
            ),
            (
                "stale util_tick (age > sentinel_window)",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 999,
                 "util_thread_tick": 1_000_000.0 - 200.0},  # 200s old > 90s
                10.0,
                False,
            ),
            (
                "just-under window: counter*interval = window-1",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 1,
                 "util_thread_tick": 1_000_000.0},
                31.0,  # 1*30=30, window=31 → 30 < 31 → False
                False,
            ),
            (
                "exactly at window: counter*interval = window (>= not >)",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 1,
                 "util_thread_tick": 1_000_000.0},
                30.0,  # 1*30=30, window=30 → 30 >= 30 → True
                30.0,
            ),  # placeholder — real expected handled below
            (
                "per-entry override beats cfg default",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 1,
                 "util_thread_tick": 1_000_000.0,
                 "restart_loop_window_s": 10.0},
                999.0,  # cfg ignored; per-entry 10s; 1*30=30 >= 10 → True
                True,
            ),
            (
                "corrupt per-entry override falls through to cfg",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 1,
                 "util_thread_tick": 1_000_000.0,
                 "restart_loop_window_s": "abc"},
                10.0,  # cfg used; 1*30=30 >= 10 → True
                True,
            ),
            (
                "corrupt counter type",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": "abc",
                 "util_thread_tick": 1_000_000.0},
                10.0,
                False,
            ),
            (
                "corrupt util_tick type",
                {"id": "x", "provider": "runpod",
                 "consecutive_low_uptime_count": 5,
                 "util_thread_tick": "bad"},
                10.0,
                False,
            ),
        ],
    )
    def test_predicate_table(
        self, name: str, entry: dict, restart_loop_window_s: float | None, expected: object
    ) -> None:
        from kinoforge.core.reaper import _restart_loop_reap_predicate

        result = _restart_loop_reap_predicate(
            entry,
            now=self.NOW,
            sentinel_window=self.SENTINEL_WINDOW,
            heartbeat_interval_s=self.INTERVAL,
            restart_loop_window_s=restart_loop_window_s,
        )
        # Normalise the placeholder row's expected to True.
        want = True if expected == 30.0 else expected
        assert result == want, f"case={name!r} got {result} want {want}"
```

(Placeholder row "exactly at window" uses the `30.0` sentinel to flag "expected True" — the test normaliser converts. Cleaner: rewrite that row with `expected=True` and drop the sentinel; included as a one-row clarity exception so the reader sees the boundary math up front. Implementer may inline the cleanup if desired.)

- [ ] **Step 2: Run test — confirm RED**

Run: `pixi run -- pytest tests/test_reaper.py::TestRestartLoopReapPredicate -v`
Expected: FAIL — `ImportError: cannot import name '_restart_loop_reap_predicate'`.

- [ ] **Step 3: Add the predicate**

Edit `src/kinoforge/core/reaper.py`. After `_stall_reap_predicate` (around line 180), append:

```python
def _restart_loop_reap_predicate(
    entry: Mapping[str, Any],
    *,
    now: float,
    sentinel_window: float,
    heartbeat_interval_s: float,
    restart_loop_window_s: float | None,
) -> bool:
    """Return True iff the entry should fire RESTART_LOOP_REAP (C27 row 3'').

    Same defensive shape as _stall_reap_predicate: bad types fall through
    to default rather than raising, because raising inside ``classify``
    would abort the whole sweep on one bad entry.

    Args:
        entry: The ledger entry being classified.
        now: Wall-clock seconds.
        sentinel_window: ``3 * heartbeat_interval_s`` — used as the
            util-tick freshness ceiling.
        heartbeat_interval_s: Cfg heartbeat cadence; counter × interval
            gives cumulative low-uptime duration in seconds.
        restart_loop_window_s: Cfg-level threshold; None = kill switch.

    Returns:
        True when:
          1. Feature on (effective window is not None), AND
          2. Provider has a util substrate (or provider unknown), AND
          3. consecutive_low_uptime_count and util_thread_tick both present, AND
          4. util_thread_tick fresh (age <= sentinel_window), AND
          5. counter × heartbeat_interval_s >= effective window.
        Per-entry ``restart_loop_window_s`` override beats the default.
    """
    override = entry.get("restart_loop_window_s")
    if override is not None:
        try:
            effective_window: float | None = float(override)
        except (TypeError, ValueError):
            effective_window = restart_loop_window_s
    else:
        effective_window = restart_loop_window_s
    if effective_window is None:
        return False
    provider_kind = entry.get("provider_kind") or entry.get("provider")
    if provider_kind is not None and not provider_util_supported(str(provider_kind)):
        return False
    counter = entry.get("consecutive_low_uptime_count")
    util_tick = entry.get("util_thread_tick")
    if counter is None or util_tick is None:
        return False
    try:
        counter_i = int(counter)
        util_age = now - float(util_tick)
    except (TypeError, ValueError):
        return False
    if util_age > sentinel_window:
        return False
    return counter_i * heartbeat_interval_s >= effective_window
```

`provider_util_supported` is already imported at module top (C26).

- [ ] **Step 4: Run test — confirm GREEN**

Run: `pixi run -- pytest tests/test_reaper.py::TestRestartLoopReapPredicate -v`
Expected: PASS — all 12 cases.

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/reaper.py tests/test_reaper.py
git add src/kinoforge/core/reaper.py tests/test_reaper.py
git commit -m "feat(c27): add _restart_loop_reap_predicate pure function

Twin of _stall_reap_predicate. Same defensive shape: bad types fall
through to default. Per-entry restart_loop_window_s override beats cfg.

Spec §4.2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `classify()` row 3'' wiring

**Goal:** Wire `_restart_loop_reap_predicate` into `classify()` at row 3'', behind row 3' (`STALL_REAP` tie-breaker wins).

**Files:**
- Modify: `src/kinoforge/core/reaper.py` (`classify` signature + body)
- Test: `tests/test_reaper.py` (extend TestClassify)

**Acceptance Criteria:**
- [ ] `classify()` signature gains `restart_loop_window_s: float | None = None` and `restart_loop_uptime_threshold_s: float = 90.0`.
- [ ] Row 3'' branch returns `Verdict.RESTART_LOOP_REAP` only when predicate fires AND `_stall_reap_predicate` did not.
- [ ] Tie-breaker: both predicates fire → `STALL_REAP` returned.
- [ ] Kill-switch isolation: row 3'' doesn't fire when `restart_loop_window_s=None`.
- [ ] All existing classify tests still pass (no regression).

**Verify:** `pixi run -- pytest tests/test_reaper.py -v -k 'classify or row3' ` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests for the four wiring cases**

Append to `tests/test_reaper.py` inside the existing `TestClassify` class (or a new `TestClassifyC27` if cleaner):

```python
def test_classify_only_restart_loop_predicate_fires_returns_restart_loop_reap(
    self,
) -> None:
    """C27: row 3'' fires when only restart-loop predicate matches."""
    from kinoforge.core.reaper import Verdict, classify

    now = 1_000_000.0
    entry = {
        "id": "p1",
        "provider": "runpod",
        "created_at": now - 100.0,
        "heartbeat_thread_tick": now - 5.0,
        "last_heartbeat": now - 5.0,
        "util_thread_tick": now,
        "consecutive_low_util_count": 0,           # stall predicate False
        "consecutive_low_uptime_count": 10,         # restart-loop fires
    }
    verdict = classify(
        entry,
        live_pod_ids=frozenset({"p1"}),
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=3600.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=120.0,
        stall_window_s=600.0,
        restart_loop_window_s=180.0,
    )
    assert verdict == Verdict.RESTART_LOOP_REAP


def test_classify_only_stall_predicate_fires_returns_stall_reap(self) -> None:
    """C26 still works: row 3' fires when only stall predicate matches."""
    from kinoforge.core.reaper import Verdict, classify

    now = 1_000_000.0
    entry = {
        "id": "p1",
        "provider": "runpod",
        "created_at": now - 100.0,
        "heartbeat_thread_tick": now - 5.0,
        "last_heartbeat": now - 5.0,
        "util_thread_tick": now,
        "consecutive_low_util_count": 100,          # stall fires
        "consecutive_low_uptime_count": 0,           # restart-loop False
    }
    verdict = classify(
        entry,
        live_pod_ids=frozenset({"p1"}),
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=3600.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=120.0,
        stall_window_s=60.0,
        restart_loop_window_s=180.0,
    )
    assert verdict == Verdict.STALL_REAP


def test_classify_both_predicates_fire_stall_reap_wins_tiebreaker(self) -> None:
    """C27 tie-breaker: stall checked first, wins when both true."""
    from kinoforge.core.reaper import Verdict, classify

    now = 1_000_000.0
    entry = {
        "id": "p1",
        "provider": "runpod",
        "created_at": now - 100.0,
        "heartbeat_thread_tick": now - 5.0,
        "last_heartbeat": now - 5.0,
        "util_thread_tick": now,
        "consecutive_low_util_count": 100,
        "consecutive_low_uptime_count": 100,
    }
    verdict = classify(
        entry,
        live_pod_ids=frozenset({"p1"}),
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=3600.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=120.0,
        stall_window_s=60.0,
        restart_loop_window_s=60.0,
    )
    assert verdict == Verdict.STALL_REAP


def test_classify_restart_loop_kill_switch_returns_live(self) -> None:
    """C27 kill-switch: restart_loop_window_s=None → row 3'' never fires."""
    from kinoforge.core.reaper import Verdict, classify

    now = 1_000_000.0
    entry = {
        "id": "p1",
        "provider": "runpod",
        "created_at": now - 100.0,
        "heartbeat_thread_tick": now - 5.0,
        "last_heartbeat": now - 5.0,
        "util_thread_tick": now,
        "consecutive_low_util_count": 0,
        "consecutive_low_uptime_count": 10,
    }
    verdict = classify(
        entry,
        live_pod_ids=frozenset({"p1"}),
        now=now,
        idle_timeout_s=600.0,
        max_lifetime_s=3600.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=120.0,
        stall_window_s=None,
        restart_loop_window_s=None,  # kill switch
    )
    assert verdict == Verdict.LIVE
```

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run -- pytest tests/test_reaper.py::TestClassify -v -k restart_loop`
Expected: FAIL — `classify() got an unexpected keyword argument 'restart_loop_window_s'`.

- [ ] **Step 3: Widen `classify()` signature + add row 3''**

Edit `src/kinoforge/core/reaper.py`. Update the `classify()` signature and docstring:

```python
def classify(
    entry: Mapping[str, Any],
    live_pod_ids: frozenset[str] | set[str],
    now: float,
    *,
    idle_timeout_s: float,
    max_lifetime_s: float,
    heartbeat_interval_s: float | None,
    grace_after_session_s: float,
    stall_window_s: float | None = None,
    stall_gpu_threshold: float = 5.0,
    stall_cpu_threshold: float = 20.0,
    restart_loop_window_s: float | None = None,
    restart_loop_uptime_threshold_s: float = 90.0,
) -> Verdict:
```

Augment docstring Args block, adding right after the C26 `stall_cpu_threshold` paragraph:

```
        restart_loop_window_s: C27 cfg threshold for util-aware restart-
            loop reaping. ``None`` (default) = kill switch, no
            RESTART_LOOP_REAP fires. Per-entry ``restart_loop_window_s``
            key overrides at row 3''.
        restart_loop_uptime_threshold_s: C27 cfg uptime % strict-<
            threshold for ``_update_uptime_counter``. Carried for
            HeartbeatLoop and unused inside classify itself.
```

In the body, find the existing row 3' branch:

```python
        if _stall_reap_predicate(
            entry,
            now=now,
            sentinel_window=sentinel_window,
            heartbeat_interval_s=heartbeat_interval_s,
            stall_window_s=stall_window_s,
        ):
            return Verdict.STALL_REAP
        return Verdict.LIVE
```

Insert the row 3'' branch between `STALL_REAP` and `return Verdict.LIVE`:

```python
        if _stall_reap_predicate(
            entry,
            now=now,
            sentinel_window=sentinel_window,
            heartbeat_interval_s=heartbeat_interval_s,
            stall_window_s=stall_window_s,
        ):
            return Verdict.STALL_REAP
        # Row 3'' (C27): restart-loop reap interception.
        if _restart_loop_reap_predicate(
            entry,
            now=now,
            sentinel_window=sentinel_window,
            heartbeat_interval_s=heartbeat_interval_s,
            restart_loop_window_s=restart_loop_window_s,
        ):
            return Verdict.RESTART_LOOP_REAP
        return Verdict.LIVE
```

- [ ] **Step 4: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_reaper.py -v`
Expected: PASS — all four new cases + all pre-existing classify tests.

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/reaper.py tests/test_reaper.py
git add src/kinoforge/core/reaper.py tests/test_reaper.py
git commit -m "feat(c27): wire row 3'' into classify with STALL_REAP tie-breaker

Predicate checked after _stall_reap_predicate so simultaneous fires
return STALL_REAP. Kill-switch via restart_loop_window_s=None.

Spec §4.4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `LifecycleConfig` restart_loop_* fields + validators

**Goal:** Add three new fields to `LifecycleConfig` with two validators, matching the C26 stall_* pattern exactly.

**Files:**
- Modify: `src/kinoforge/core/config.py` (`LifecycleConfig` class)
- Test: `tests/test_config.py` (extend `LifecycleConfig` TestClass)

**Acceptance Criteria:**
- [ ] `LifecycleConfig.restart_loop_reap_enabled: bool = True` exists.
- [ ] `LifecycleConfig.restart_loop_window_s: float = 180.0` exists.
- [ ] `LifecycleConfig.restart_loop_uptime_threshold_s: float = 90.0` exists.
- [ ] `restart_loop_window_s = -1` rejected at load with ValueError.
- [ ] `restart_loop_uptime_threshold_s = -1` rejected at load with ValueError.
- [ ] YAML round-trip preserves all three fields.

**Verify:** `pixi run -- pytest tests/test_config.py -v -k 'LifecycleConfig and restart_loop'` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests for defaults + validators**

Append to `tests/test_config.py` inside the existing LifecycleConfig TestClass:

```python
def test_lifecycle_config_restart_loop_defaults(self) -> None:
    """C27: defaults are restart_loop_reap_enabled=True, window=180, threshold=90."""
    from kinoforge.core.config import LifecycleConfig

    lc = LifecycleConfig(budget=10.0)
    assert lc.restart_loop_reap_enabled is True
    assert lc.restart_loop_window_s == 180.0
    assert lc.restart_loop_uptime_threshold_s == 90.0


def test_lifecycle_config_restart_loop_window_negative_rejected(self) -> None:
    """C27: negative window rejected at load."""
    import pytest
    from kinoforge.core.config import LifecycleConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="restart_loop_window_s must be >= 0"):
        LifecycleConfig(budget=10.0, restart_loop_window_s=-1.0)


def test_lifecycle_config_restart_loop_uptime_threshold_negative_rejected(self) -> None:
    """C27: negative threshold rejected at load."""
    import pytest
    from kinoforge.core.config import LifecycleConfig
    from pydantic import ValidationError

    with pytest.raises(
        ValidationError, match="restart_loop_uptime_threshold_s must be >= 0"
    ):
        LifecycleConfig(budget=10.0, restart_loop_uptime_threshold_s=-1.0)


def test_lifecycle_config_restart_loop_yaml_roundtrip(self) -> None:
    """C27: YAML roundtrips all three new fields."""
    from kinoforge.core.config import LifecycleConfig

    lc = LifecycleConfig(
        budget=10.0,
        restart_loop_reap_enabled=False,
        restart_loop_window_s=240.0,
        restart_loop_uptime_threshold_s=120.0,
    )
    dumped = lc.model_dump()
    assert dumped["restart_loop_reap_enabled"] is False
    assert dumped["restart_loop_window_s"] == 240.0
    assert dumped["restart_loop_uptime_threshold_s"] == 120.0
```

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run -- pytest tests/test_config.py -v -k restart_loop`
Expected: FAIL — `LifecycleConfig() got an unexpected keyword argument 'restart_loop_window_s'`.

- [ ] **Step 3: Add fields + validators**

Edit `src/kinoforge/core/config.py`. In the `LifecycleConfig` class, after the C26 `stall_cpu_threshold: float = 20.0` line, append:

```python
    restart_loop_reap_enabled: bool = True
    restart_loop_window_s: float = 180.0
    restart_loop_uptime_threshold_s: float = 90.0
```

After the C26 `_validate_stall_threshold_range` validator, append:

```python
    @field_validator("restart_loop_window_s")
    @classmethod
    def _validate_restart_loop_window_non_negative(cls, v: float) -> float:
        """Reject negative restart_loop_window_s at load time (C27)."""
        if v < 0:
            raise ValueError(f"restart_loop_window_s must be >= 0; got {v}")
        return v

    @field_validator("restart_loop_uptime_threshold_s")
    @classmethod
    def _validate_restart_loop_uptime_threshold_non_negative(cls, v: float) -> float:
        """Reject negative restart_loop_uptime_threshold_s at load time (C27)."""
        if v < 0:
            raise ValueError(f"restart_loop_uptime_threshold_s must be >= 0; got {v}")
        return v
```

- [ ] **Step 4: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_config.py -v -k LifecycleConfig`
Expected: PASS — four new cases + all pre-existing.

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/config.py tests/test_config.py
git add src/kinoforge/core/config.py tests/test_config.py
git commit -m "feat(c27): add LifecycleConfig restart_loop_* fields + validators

Three new fields (restart_loop_reap_enabled=True, restart_loop_window_s
=180.0, restart_loop_uptime_threshold_s=90.0). Two validators reject
negatives at load. Mirrors the C26 stall_* pattern.

Spec §5.1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `interfaces.Lifecycle` extension + `Config.lifecycle()` wiring

**Goal:** Add two new fields to the `Lifecycle` dataclass; wire the `Config.lifecycle()` collapse so `restart_loop_reap_enabled=False` produces `restart_loop_window_s=None` in the `InterfaceLifecycle`.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`Lifecycle` dataclass)
- Modify: `src/kinoforge/core/config.py` (`Config.lifecycle()` method)
- Test: `tests/test_config.py` (collapse cases) + `tests/test_interfaces.py` if exists

**Acceptance Criteria:**
- [ ] `Lifecycle.restart_loop_window_s: float | None = None` exists.
- [ ] `Lifecycle.restart_loop_uptime_threshold_s: float = 90.0` exists.
- [ ] `Config.lifecycle()` returns `restart_loop_window_s=None` when `restart_loop_reap_enabled=False`.
- [ ] `Config.lifecycle()` returns `restart_loop_window_s=180.0` when `restart_loop_reap_enabled=True`.
- [ ] `restart_loop_uptime_threshold_s` always passes through.

**Verify:** `pixi run -- pytest tests/test_config.py -v -k lifecycle_collapse` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests for the collapse**

Append to `tests/test_config.py`:

```python
def test_config_lifecycle_collapses_restart_loop_to_none_when_disabled(self) -> None:
    """C27: restart_loop_reap_enabled=False → InterfaceLifecycle.restart_loop_window_s=None."""
    from kinoforge.core.config import LifecycleConfig

    lc_cfg = LifecycleConfig(
        budget=10.0,
        restart_loop_reap_enabled=False,
        restart_loop_window_s=180.0,
    )
    cfg = _make_config_with_lifecycle(lc_cfg)  # helper builds a Config
    lc = cfg.lifecycle()
    assert lc.restart_loop_window_s is None
    assert lc.restart_loop_uptime_threshold_s == 90.0  # threshold always passes


def test_config_lifecycle_collapses_restart_loop_window_when_enabled(self) -> None:
    """C27: restart_loop_reap_enabled=True → window value passes through."""
    from kinoforge.core.config import LifecycleConfig

    lc_cfg = LifecycleConfig(
        budget=10.0,
        restart_loop_reap_enabled=True,
        restart_loop_window_s=240.0,
        restart_loop_uptime_threshold_s=120.0,
    )
    cfg = _make_config_with_lifecycle(lc_cfg)
    lc = cfg.lifecycle()
    assert lc.restart_loop_window_s == 240.0
    assert lc.restart_loop_uptime_threshold_s == 120.0
```

If `_make_config_with_lifecycle` does not already exist, add it at top of test module (or inline a minimal `Config(engine=…, models=[ModelEntry(…)], compute=ComputeConfig(…, lifecycle=lc_cfg))` builder mirroring the C26 stall_* collapse test).

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run -- pytest tests/test_config.py -v -k lifecycle_collapses`
Expected: FAIL — `AttributeError: 'Lifecycle' object has no attribute 'restart_loop_window_s'` or similar.

- [ ] **Step 3: Extend `interfaces.Lifecycle`**

Edit `src/kinoforge/core/interfaces.py`. In the `Lifecycle` dataclass (find the C26 `stall_*` block), after the `stall_cpu_threshold: float = 20.0` line, append:

```python
    restart_loop_window_s: float | None = None
    restart_loop_uptime_threshold_s: float = 90.0
```

- [ ] **Step 4: Wire `Config.lifecycle()` collapse**

Edit `src/kinoforge/core/config.py` in the `Config.lifecycle()` method. After the existing `stall_window_s=…`, `stall_gpu_threshold=…`, `stall_cpu_threshold=…` lines in the `return InterfaceLifecycle(...)` call, append:

```python
            restart_loop_window_s=(
                lc.restart_loop_window_s if lc.restart_loop_reap_enabled else None
            ),
            restart_loop_uptime_threshold_s=lc.restart_loop_uptime_threshold_s,
```

- [ ] **Step 5: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_config.py tests/test_interfaces.py -v 2>&1 | tail -50`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/config.py tests/test_config.py
git add src/kinoforge/core/interfaces.py src/kinoforge/core/config.py tests/test_config.py
git commit -m "feat(c27): wire restart_loop_* through interfaces.Lifecycle

Config.lifecycle() collapses restart_loop_reap_enabled to
restart_loop_window_s=None when disabled. Mirrors C26 stall_reap_enabled
collapse.

Spec §5.2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `HeartbeatLoop` kwargs + state + ledger touch extensions

**Goal:** Extend `HeartbeatLoop.__init__` with three new kwargs, add `_uptime_counter` instance state, persist `consecutive_low_uptime_count` via `_build_util_extra`, run `_update_uptime_counter` each tick. Predicate routing comes in Task 8.

**Files:**
- Modify: `src/kinoforge/core/heartbeat_loop.py`
- Test: `tests/test_heartbeat_loop.py` (extend existing TestClasses)

**Acceptance Criteria:**
- [ ] Three new `__init__` kwargs: `restart_loop_window_s: float | None = None`, `restart_loop_uptime_threshold_s: float = 90.0` (third kwarg `_uptime_counter` is instance state, not a kwarg).
- [ ] `self._uptime_counter` initialised to 0.
- [ ] `_tick_once` calls `_update_uptime_counter` after `_update_counter` when `util_endpoint is not None`.
- [ ] `_build_util_extra` includes `consecutive_low_uptime_count` in both branches (snap=None + snap-present).
- [ ] Backward-compat: callers not passing new kwargs still work — feature dormant.

**Verify:** `pixi run -- pytest tests/test_heartbeat_loop.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests for state + ledger persistence**

Append to `tests/test_heartbeat_loop.py`:

```python
def test_heartbeat_loop_persists_uptime_counter_on_tick(self) -> None:
    """C27: ledger.touch receives consecutive_low_uptime_count each tick."""
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.core.util_endpoints import UtilSnapshot

    touches: list[dict] = []

    class _SpyLedger:
        def touch(self, instance_id, **extra):
            touches.append({"id": instance_id, **extra})
            return True

    class _SpyProvider:
        def heartbeat(self, _id): pass
        def last_heartbeat(self, _id): return 1_000.0
        def destroy_instance(self, _id): pass

    class _StaticUtilEndpoint:
        def read_util(self, _id):
            return UtilSnapshot(0.0, 13.0, 0.0, None, 1)  # uptime=1 < 90

    loop = HeartbeatLoop(
        ledger=_SpyLedger(),
        provider=_SpyProvider(),
        instance_id="p1",
        interval_s=0.01,  # tight so the test runs fast
        util_endpoint=_StaticUtilEndpoint(),
        restart_loop_uptime_threshold_s=90.0,
        # restart_loop_window_s left None → no fire (Task 8 wires firing)
    )
    loop._tick_once()
    assert touches, "no touch recorded"
    assert "consecutive_low_uptime_count" in touches[0]
    assert touches[0]["consecutive_low_uptime_count"] == 1  # first tick increment


def test_heartbeat_loop_uptime_counter_increments_across_ticks(self) -> None:
    """C27: counter accumulates while uptime stays below threshold."""
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.core.util_endpoints import UtilSnapshot

    touches: list[dict] = []

    class _SpyLedger:
        def touch(self, instance_id, **extra):
            touches.append({"id": instance_id, **extra})
            return True

    class _SpyProvider:
        def heartbeat(self, _id): pass
        def last_heartbeat(self, _id): return 1_000.0
        def destroy_instance(self, _id): pass

    class _StaticUtilEndpoint:
        def read_util(self, _id):
            return UtilSnapshot(0.0, 13.0, 0.0, None, 1)

    loop = HeartbeatLoop(
        ledger=_SpyLedger(),
        provider=_SpyProvider(),
        instance_id="p1",
        interval_s=0.01,
        util_endpoint=_StaticUtilEndpoint(),
        restart_loop_uptime_threshold_s=90.0,
    )
    for _ in range(5):
        loop._tick_once()
    assert [t["consecutive_low_uptime_count"] for t in touches] == [1, 2, 3, 4, 5]
```

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run -- pytest tests/test_heartbeat_loop.py -v -k uptime_counter`
Expected: FAIL — kwargs unknown or counter field missing in touches.

- [ ] **Step 3: Extend `__init__` signature**

Edit `src/kinoforge/core/heartbeat_loop.py`. In `HeartbeatLoop.__init__`, after the existing C26 kwargs (`stall_window_s`, `stall_gpu_threshold`, `stall_cpu_threshold`), append:

```python
        restart_loop_window_s: float | None = None,
        restart_loop_uptime_threshold_s: float = 90.0,
```

Add to the existing docstring Args block:

```
            restart_loop_window_s: C27 cfg threshold (effective window in
                seconds). ``None`` = kill switch — no RESTART_LOOP fires.
            restart_loop_uptime_threshold_s: uptime strict-< threshold
                for ``_update_uptime_counter``.
```

In `__init__` body, after `self._stall_cpu_threshold = stall_cpu_threshold`, append:

```python
        self._restart_loop_window_s = restart_loop_window_s
        self._restart_loop_uptime_threshold_s = restart_loop_uptime_threshold_s
        self._uptime_counter = 0
```

- [ ] **Step 4: Update imports**

At the top of `src/kinoforge/core/heartbeat_loop.py`, alongside `from kinoforge.core.util_counter import _update_counter`, change to:

```python
from kinoforge.core.util_counter import _update_counter, _update_uptime_counter
```

- [ ] **Step 5: Extend `_tick_once` counter update**

In `_tick_once`, in the `if self._util_endpoint is not None:` block, after the existing `_update_counter` call, append:

```python
                self._uptime_counter = _update_uptime_counter(
                    self._uptime_counter,
                    snap=snap,
                    uptime_threshold_s=self._restart_loop_uptime_threshold_s,
                )
```

In the same block, update the `extra.update(...)` line to pass the new counter to `_build_util_extra`:

```python
                extra.update(
                    self._build_util_extra(
                        now=now,
                        snap=snap,
                        counter=self._counter,
                        uptime_counter=self._uptime_counter,
                    )
                )
```

- [ ] **Step 6: Extend `_build_util_extra` signature + both branches**

Replace the existing `_build_util_extra` with:

```python
    @staticmethod
    def _build_util_extra(
        *,
        now: float,
        snap: UtilSnapshot | None,
        counter: int,
        uptime_counter: int,
    ) -> dict[str, float | int | str | None]:
        """Build the util-related ledger fields plus the tick timestamp.

        C27 adds consecutive_low_uptime_count alongside C26's
        consecutive_low_util_count in both branches.
        """
        base: dict[str, float | int | str | None] = {
            "util_thread_tick": now,
            "consecutive_low_util_count": counter,
            "consecutive_low_uptime_count": uptime_counter,
        }
        if snap is None:
            return base
        return {
            **base,
            "last_gpu_util_percent": snap.gpu_util_percent,
            "last_cpu_percent": snap.cpu_percent,
            "last_memory_percent": snap.memory_percent,
            "last_disk_percent": snap.disk_percent,
            "last_uptime_seconds": snap.uptime_seconds,
        }
```

- [ ] **Step 7: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_heartbeat_loop.py -v 2>&1 | tail -40`
Expected: PASS — new tests + all pre-existing.

- [ ] **Step 8: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/heartbeat_loop.py tests/test_heartbeat_loop.py
git add src/kinoforge/core/heartbeat_loop.py tests/test_heartbeat_loop.py
git commit -m "feat(c27): wire _update_uptime_counter + ledger persist into HeartbeatLoop

New kwargs restart_loop_window_s + restart_loop_uptime_threshold_s.
New instance state _uptime_counter. _tick_once runs both counters;
_build_util_extra persists consecutive_low_uptime_count in both branches.
Predicate routing (rename + both-paths) follows in Task 8.

Spec §4.5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: HeartbeatLoop `_maybe_fire_reap` rename + both-routes wiring

**Goal:** Rename `_maybe_fire_stall_reap` → `_maybe_fire_reap`; check both predicates first-match-wins; logging names which Verdict fired.

**Files:**
- Modify: `src/kinoforge/core/heartbeat_loop.py`
- Test: `tests/test_heartbeat_loop.py`

**Acceptance Criteria:**
- [ ] Method renamed; old name removed.
- [ ] Six new tests pass (per spec §7.1 "both routes" matrix).
- [ ] STALL_REAP fires when only stall predicate matches → destroy + cancel + stop.
- [ ] RESTART_LOOP_REAP fires when only restart-loop predicate matches → destroy + cancel + stop.
- [ ] Both fire → STALL_REAP wins (tie-breaker matches classify).
- [ ] Neither set → no fire even at counter=999.
- [ ] Log line names the Verdict + both counter values.
- [ ] `_tick_once` calls `_maybe_fire_reap` (the renamed method).

**Verify:** `pixi run -- pytest tests/test_heartbeat_loop.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests for both-routes matrix**

Append to `tests/test_heartbeat_loop.py`:

```python
class TestHeartbeatLoopBothRoutes:
    """C27: _maybe_fire_reap routes STALL_REAP and RESTART_LOOP_REAP."""

    def _spies(self):
        touches: list[dict] = []
        destroys: list[str] = []
        forgets: list[str] = []
        cancel_set = [False]

        class _SpyLedger:
            def touch(self, instance_id, **extra):
                touches.append({"id": instance_id, **extra})
                return True
            def forget(self, instance_id):
                forgets.append(instance_id)

        class _SpyProvider:
            def heartbeat(self, _id): pass
            def last_heartbeat(self, _id): return 1_000.0
            def destroy_instance(self, instance_id):
                destroys.append(instance_id)

        class _CancelToken:
            def set(self):
                cancel_set[0] = True

        return touches, destroys, forgets, cancel_set, _SpyLedger(), _SpyProvider(), _CancelToken()

    def test_only_stall_window_set_fires_stall_reap_path(self) -> None:
        from kinoforge.core.heartbeat_loop import HeartbeatLoop
        from kinoforge.core.util_endpoints import UtilSnapshot
        touches, destroys, forgets, cancel_set, led, prov, tok = self._spies()

        class _Util:
            def read_util(self, _id):
                # gpu+cpu both LOW so stall fires; uptime LOW too but
                # restart-loop disabled via window=None.
                return UtilSnapshot(0.0, 0.0, 0.0, None, 1)

        loop = HeartbeatLoop(
            ledger=led, provider=prov, instance_id="p1",
            interval_s=0.01,
            util_endpoint=_Util(),
            cancel_token=tok,
            provider_kind="runpod",
            stall_window_s=0.005,                  # fire on first tick
            restart_loop_window_s=None,             # kill
        )
        loop._tick_once()
        assert destroys == ["p1"]
        assert forgets == ["p1"]
        assert cancel_set[0] is True

    def test_only_restart_loop_window_set_fires_restart_loop_path(self) -> None:
        from kinoforge.core.heartbeat_loop import HeartbeatLoop
        from kinoforge.core.util_endpoints import UtilSnapshot
        touches, destroys, forgets, cancel_set, led, prov, tok = self._spies()

        class _Util:
            def read_util(self, _id):
                # uptime LOW → restart-loop fires; stall window=None kills C26 path.
                return UtilSnapshot(50.0, 50.0, 50.0, None, 1)  # gpu/cpu HIGH

        loop = HeartbeatLoop(
            ledger=led, provider=prov, instance_id="p1",
            interval_s=0.01,
            util_endpoint=_Util(),
            cancel_token=tok,
            provider_kind="runpod",
            stall_window_s=None,
            restart_loop_window_s=0.005,
            restart_loop_uptime_threshold_s=90.0,
        )
        loop._tick_once()
        assert destroys == ["p1"]
        assert forgets == ["p1"]
        assert cancel_set[0] is True

    def test_both_predicates_at_threshold_stall_wins_tiebreaker(self) -> None:
        from kinoforge.core.heartbeat_loop import HeartbeatLoop
        from kinoforge.core.util_endpoints import UtilSnapshot
        touches, destroys, forgets, cancel_set, led, prov, tok = self._spies()

        class _Util:
            def read_util(self, _id):
                return UtilSnapshot(0.0, 0.0, 0.0, None, 1)

        loop = HeartbeatLoop(
            ledger=led, provider=prov, instance_id="p1",
            interval_s=0.01,
            util_endpoint=_Util(),
            cancel_token=tok,
            provider_kind="runpod",
            stall_window_s=0.005,
            restart_loop_window_s=0.005,
            restart_loop_uptime_threshold_s=90.0,
        )
        with self._capture_logs() as logs:
            loop._tick_once()
        # Verify it was the STALL_REAP path that fired (log line names it).
        assert any("STALL_REAP" in m for m in logs), logs
        assert not any("RESTART_LOOP_REAP" in m for m in logs), logs

    def test_neither_set_no_fire_even_at_high_counter(self) -> None:
        from kinoforge.core.heartbeat_loop import HeartbeatLoop
        from kinoforge.core.util_endpoints import UtilSnapshot
        touches, destroys, forgets, cancel_set, led, prov, tok = self._spies()

        class _Util:
            def read_util(self, _id):
                return UtilSnapshot(0.0, 0.0, 0.0, None, 1)

        loop = HeartbeatLoop(
            ledger=led, provider=prov, instance_id="p1",
            interval_s=0.01,
            util_endpoint=_Util(),
            cancel_token=tok,
            provider_kind="runpod",
            stall_window_s=None,
            restart_loop_window_s=None,
        )
        for _ in range(50):
            loop._tick_once()
        assert destroys == []
        assert forgets == []
        assert cancel_set[0] is False

    @staticmethod
    def _capture_logs():
        """Context manager yielding a list of warning records from this module."""
        import contextlib
        import logging
        import io

        @contextlib.contextmanager
        def _cm():
            logger = logging.getLogger("kinoforge.core.heartbeat_loop")
            buf = io.StringIO()
            handler = logging.StreamHandler(buf)
            handler.setLevel(logging.WARNING)
            logger.addHandler(handler)
            prev_level = logger.level
            logger.setLevel(logging.WARNING)
            try:
                yield buf.getvalue().splitlines() if False else _LineList(buf)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(prev_level)

        class _LineList(list):
            def __init__(self, buf):
                super().__init__()
                self._buf = buf
            def __iter__(self):
                return iter(self._buf.getvalue().splitlines())
            def __contains__(self, item):
                return item in self._buf.getvalue()
            def __bool__(self):
                return bool(self._buf.getvalue())
            def __repr__(self):
                return repr(self._buf.getvalue())

        return _cm()
```

Note: log capture is fiddly with the existing test style. If the existing repo uses `caplog`, simplify the tie-breaker test to:

```python
def test_both_predicates_at_threshold_stall_wins_tiebreaker(self, caplog) -> None:
    import logging
    caplog.set_level(logging.WARNING, logger="kinoforge.core.heartbeat_loop")
    # ... setup as above ...
    loop._tick_once()
    assert any("STALL_REAP" in r.message for r in caplog.records)
    assert not any("RESTART_LOOP_REAP" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run -- pytest tests/test_heartbeat_loop.py::TestHeartbeatLoopBothRoutes -v`
Expected: FAIL — `_maybe_fire_stall_reap` only handles one route; RESTART_LOOP path missing.

- [ ] **Step 3: Rename method + add both-routes body**

Edit `src/kinoforge/core/heartbeat_loop.py`. Update import to include the new predicate:

```python
from kinoforge.core.reaper import _restart_loop_reap_predicate, _stall_reap_predicate
```

Replace the entire `_maybe_fire_stall_reap` method with the renamed `_maybe_fire_reap`:

```python
    def _maybe_fire_reap(self, *, now: float) -> None:
        """Self-classify; on STALL_REAP or RESTART_LOOP_REAP destroy + cancel + stop."""
        sentinel_window = 3.0 * self._interval_s
        entry: dict[str, float | int | str | None] = {
            "id": self._instance_id,
            "consecutive_low_util_count": self._counter,
            "consecutive_low_uptime_count": self._uptime_counter,
            "util_thread_tick": now,
        }
        if self._provider_kind is not None:
            entry["provider"] = self._provider_kind

        fired_verdict: str | None = None
        fired_window: float | None = None
        if self._stall_window_s is not None and _stall_reap_predicate(
            entry,
            now=now,
            sentinel_window=sentinel_window,
            heartbeat_interval_s=self._interval_s,
            stall_window_s=self._stall_window_s,
        ):
            fired_verdict = "STALL_REAP"
            fired_window = self._stall_window_s
        elif self._restart_loop_window_s is not None and _restart_loop_reap_predicate(
            entry,
            now=now,
            sentinel_window=sentinel_window,
            heartbeat_interval_s=self._interval_s,
            restart_loop_window_s=self._restart_loop_window_s,
        ):
            fired_verdict = "RESTART_LOOP_REAP"
            fired_window = self._restart_loop_window_s
        if fired_verdict is None:
            return

        self._logger.warning(
            "%s fired for %s (low_util_counter=%d, low_uptime_counter=%d, window=%.0fs)",
            fired_verdict,
            self._instance_id,
            self._counter,
            self._uptime_counter,
            fired_window,
        )
        destroy = getattr(self._provider, "destroy_instance", None)
        if destroy is not None:
            try:
                destroy(self._instance_id)
            except Exception:  # noqa: BLE001 — best-effort destroy
                self._logger.exception(
                    "%s destroy failed for %s", fired_verdict, self._instance_id
                )
        forget = getattr(self._ledger, "forget", None)
        if forget is not None:
            try:
                forget(self._instance_id)
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "%s ledger.forget failed for %s", fired_verdict, self._instance_id
                )
        if self._cancel_token is not None:
            self._cancel_token.set()
        self._stop.set()
```

In `_tick_once`, replace the existing call:

```python
            if self._util_endpoint is not None:
                self._maybe_fire_stall_reap(now=now)
```

with:

```python
            if self._util_endpoint is not None:
                self._maybe_fire_reap(now=now)
```

- [ ] **Step 4: Update existing C26 tests that referenced the old method name**

Run: `rg -l '_maybe_fire_stall_reap' tests/`
For each match, replace `_maybe_fire_stall_reap` → `_maybe_fire_reap`. Run the file's tests after each edit to confirm no regression.

- [ ] **Step 5: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_heartbeat_loop.py -v 2>&1 | tail -40`
Expected: PASS — new TestHeartbeatLoopBothRoutes + all pre-existing tests including renamed-method references.

- [ ] **Step 6: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/heartbeat_loop.py tests/test_heartbeat_loop.py
git add src/kinoforge/core/heartbeat_loop.py tests/test_heartbeat_loop.py
git commit -m "feat(c27): _maybe_fire_reap routes both STALL_REAP and RESTART_LOOP_REAP

Rename _maybe_fire_stall_reap → _maybe_fire_reap. First-match-wins on
the two predicates, STALL_REAP checked first to honour the classify
tie-breaker. Log line names the fired Verdict + both counter values.

Spec §4.5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: CLI `--restart-loop-window-override` flag

**Goal:** Add the CLI flag to `kinoforge deploy`; persist override into the ledger entry's `restart_loop_window_s` key.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (argparser)
- Modify: `src/kinoforge/cli/_commands.py` (`deploy` handler around line 228)
- Test: `tests/test_cli.py`

**Acceptance Criteria:**
- [ ] `kinoforge deploy --restart-loop-window-override 90` accepted.
- [ ] Ledger entry gains key `restart_loop_window_s: 90.0`.
- [ ] Negative values rejected at argparse level.
- [ ] Existing `--stall-window-override` behaviour untouched.

**Verify:** `pixi run -- pytest tests/test_cli.py -v -k restart_loop_window` → all pass.

**Steps:**

- [ ] **Step 1: Write failing test for the CLI flag persistence**

Append to `tests/test_cli.py` (or wherever deploy-CLI tests live). Pattern matches the C26 `--stall-window-override` test if it exists:

```python
def test_deploy_persists_restart_loop_window_override(self) -> None:
    """C27: kinoforge deploy --restart-loop-window-override persists ledger field."""
    # Pattern: mirror the C26 stall_window override test exactly.
    # Use the same fixture for ledger + cli runner; only the flag differs.
    from kinoforge.cli._commands import _deploy_cmd  # or whatever entrypoint exists

    # ... mirror C26 test setup ...
    args = _parse_deploy_args([
        "--config", str(cfg_path),
        "--state-dir", str(state_dir),
        "--restart-loop-window-override", "90",
    ])
    result = _deploy_cmd(args)
    entry = ledger.get(result.instance.id)
    assert entry["restart_loop_window_s"] == 90.0
```

(Implementer should locate the C26 stall_window override test and copy its shape verbatim, only swapping the flag name and ledger key.)

- [ ] **Step 2: Run test — confirm RED**

Expected: FAIL — `argparse: unrecognized argument`.

- [ ] **Step 3: Add CLI flag in argparser**

Edit `src/kinoforge/cli/_main.py`. Around line 352 (where `--stall-window-override` is defined for `deploy`), append a symmetric flag:

```python
        deploy_parser.add_argument(
            "--restart-loop-window-override",
            type=float,
            default=None,
            help=(
                "C27: persist a per-entry restart_loop_window_s override into the "
                "deployed ledger entry. Tunes per-deploy; cfg-level kill switch "
                "remains restart_loop_reap_enabled."
            ),
        )
```

- [ ] **Step 4: Wire override into ledger.touch**

Edit `src/kinoforge/cli/_commands.py`. Around line 228 (where `stall_window_s` override is persisted), append a sibling:

```python
            if override := getattr(args, "restart_loop_window_override", None):
                ledger.touch(result.instance.id, restart_loop_window_s=float(override))
```

If the existing C26 override uses a positional `override` variable name, choose a non-colliding name (e.g. `restart_loop_override`). Confirm both override paths can coexist (independent reads of args).

- [ ] **Step 5: Reject negatives at argparse level**

Wrap `type=float` with a custom validator at top of `_main.py`:

```python
def _non_negative_float(s: str) -> float:
    v = float(s)
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0; got {v}")
    return v
```

Use it for both `--stall-window-override` and `--restart-loop-window-override`.

- [ ] **Step 6: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_cli.py -v 2>&1 | tail -30`
Expected: PASS — new flag test + all pre-existing.

- [ ] **Step 7: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/test_cli.py
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/test_cli.py
git commit -m "feat(c27): add --restart-loop-window-override CLI flag to deploy

Persists per-entry restart_loop_window_s into the deployed ledger entry.
Symmetric to C26 --stall-window-override. Negative values rejected at
argparse.

Spec §5.4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Cross-process callsite threading

**Goal:** Thread `restart_loop_window_s` + `restart_loop_uptime_threshold_s` through every site that constructs `HeartbeatLoop` or calls `classify`. Update `_adapters.build_util_endpoint_for` kill-switch gate to consider both `stall_reap_enabled` AND `restart_loop_reap_enabled`.

**Files:**
- Modify: `src/kinoforge/_adapters.py` (gate)
- Modify: `src/kinoforge/cli/_commands.py` (5 callsites: lines 736-738, 1020-1022, 1354-1356, 1683-1693, 1711-1713 per pre-task `rg`)
- Test: `tests/test_adapters.py` + `tests/test_cli.py` updates

**Acceptance Criteria:**
- [ ] `build_util_endpoint_for` returns None only when BOTH `stall_reap_enabled` AND `restart_loop_reap_enabled` are False (or other gate conditions fire).
- [ ] All `HeartbeatLoop(...)` construction sites pass `restart_loop_window_s` + `restart_loop_uptime_threshold_s` from the resolved `Lifecycle`.
- [ ] All `classify(...)` call sites pass `restart_loop_window_s` + `restart_loop_uptime_threshold_s`.
- [ ] One dedicated test per modified callsite verifying the kwargs are wired.

**Verify:** `pixi run -- pytest tests/test_adapters.py tests/test_cli.py -v` → all pass; `rg 'restart_loop_window_s' src/kinoforge/cli/_commands.py | wc -l` reports at least 5 hits.

**Steps:**

- [ ] **Step 1: Update `_adapters.build_util_endpoint_for` gate test**

Append to `tests/test_adapters.py`:

```python
def test_build_util_endpoint_returns_none_only_when_both_features_disabled() -> None:
    """C27: kill-switch requires BOTH stall_reap_enabled=False AND restart_loop_reap_enabled=False."""
    from kinoforge._adapters import build_util_endpoint_for
    # Build cfg fixtures with each combination.
    # Case: both False → None.
    cfg_both_off = _cfg_runpod(stall_reap_enabled=False, restart_loop_reap_enabled=False)
    assert build_util_endpoint_for(cfg_both_off, _Creds()) is None

    # Case: stall off, restart_loop on → endpoint built.
    cfg_restart_only = _cfg_runpod(stall_reap_enabled=False, restart_loop_reap_enabled=True)
    assert build_util_endpoint_for(cfg_restart_only, _Creds()) is not None

    # Case: stall on, restart_loop off → endpoint built (C26 behaviour preserved).
    cfg_stall_only = _cfg_runpod(stall_reap_enabled=True, restart_loop_reap_enabled=False)
    assert build_util_endpoint_for(cfg_stall_only, _Creds()) is not None
```

(`_cfg_runpod` is the existing C26 test factory; if it doesn't accept the new kwarg, extend it in this task.)

- [ ] **Step 2: Run test — confirm RED**

Expected: FAIL — second case (`stall=False, restart_loop=True`) returns None because the current gate only checks `stall_reap_enabled`.

- [ ] **Step 3: Update the gate**

Edit `src/kinoforge/_adapters.py` line 198 area. Replace:

```python
    if lifecycle is not None and not lifecycle.stall_reap_enabled:
        return None
```

with:

```python
    if lifecycle is not None and not (
        lifecycle.stall_reap_enabled or lifecycle.restart_loop_reap_enabled
    ):
        return None
```

Update the docstring to reflect the new condition:

```
    Returns None when:
      - cfg.compute is None (hosted-only path), OR
      - both cfg.compute.lifecycle.stall_reap_enabled AND
        cfg.compute.lifecycle.restart_loop_reap_enabled are False
        (kill switch), OR
      - provider_util_supported(cfg.compute.provider) is False.
```

And the `AuthError` message:

```python
            raise AuthError(
                "RUNPOD_API_KEY must be set when stall_reap_enabled "
                "or restart_loop_reap_enabled is true on runpod"
            )
```

- [ ] **Step 4: Thread kwargs through HeartbeatLoop construction sites in `_commands.py`**

For each of these line ranges (from pre-task `rg`):
- `_commands.py:736-738` (one HeartbeatLoop construction)
- `_commands.py:1020-1022` (second)
- `_commands.py:1354-1356` (third)
- `_commands.py:1683-1693` (fourth — extract lifecycle pattern)
- `_commands.py:1711-1713` (fifth — pass to HeartbeatLoop)

At each site, after the existing `stall_window_s=...`, `stall_gpu_threshold=...`, `stall_cpu_threshold=...` lines, append:

```python
            restart_loop_window_s=lifecycle.restart_loop_window_s,
            restart_loop_uptime_threshold_s=lifecycle.restart_loop_uptime_threshold_s,
```

For the 1683-1693 block (the "no lifecycle, use defaults" branch around `stall_window_s = None`), append:

```python
        restart_loop_window_s = None
        restart_loop_uptime_threshold_s = 90.0
```

And at the use site (1711-1713), thread:

```python
                restart_loop_window_s=restart_loop_window_s,
                restart_loop_uptime_threshold_s=restart_loop_uptime_threshold_s,
```

- [ ] **Step 5: Find any `classify(...)` callsites and thread C27 kwargs**

Run: `rg 'classify\(' src/kinoforge/cli/_commands.py`
For each callsite, after `stall_window_s=...`, append:

```python
        restart_loop_window_s=lifecycle.restart_loop_window_s,
        restart_loop_uptime_threshold_s=lifecycle.restart_loop_uptime_threshold_s,
```

(Same for any Layer W sweeper call into classify, if discovered during the audit.)

- [ ] **Step 6: Run tests — confirm GREEN**

Run: `pixi run -- pytest tests/test_adapters.py tests/test_cli.py -v 2>&1 | tail -30`
Expected: PASS.

- [ ] **Step 7: Audit: at least 5 hits**

Run: `rg 'restart_loop_window_s' src/kinoforge/cli/_commands.py | wc -l`
Expected: ≥ 5 (one per identified callsite + any classify thread sites).

- [ ] **Step 8: Lint + commit**

```bash
pixi run pre-commit run --files src/kinoforge/_adapters.py src/kinoforge/cli/_commands.py tests/test_adapters.py tests/test_cli.py
git add src/kinoforge/_adapters.py src/kinoforge/cli/_commands.py tests/test_adapters.py tests/test_cli.py
git commit -m "feat(c27): thread restart_loop_* kwargs through callsites

build_util_endpoint_for gates on BOTH stall_reap_enabled OR
restart_loop_reap_enabled. Five HeartbeatLoop construction sites in
cli/_commands.py thread the two new kwargs.

Spec §5.5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: `FakeUtilEndpoint` test helper

**Goal:** Add a tiny test helper that produces a fixed `UtilSnapshot` per read — Phase A1 uses this to force `uptime_seconds=1` indefinitely.

**Files:**
- Create: `tests/live/_c27_fake_util_endpoint.py`
- Test: `tests/test_fake_util_endpoint.py` (one happy-path test) — optional but cheap.

**Acceptance Criteria:**
- [ ] `FakeUtilEndpoint(snap: UtilSnapshot)` constructible.
- [ ] `read_util(any_id)` returns the configured snap.
- [ ] Satisfies `UtilSnapshotEndpoint` Protocol (passes runtime `isinstance` check).

**Verify:** `pixi run -- pytest tests/test_fake_util_endpoint.py -v` → pass.

**Steps:**

- [ ] **Step 1: Write the test**

Create `tests/test_fake_util_endpoint.py`:

```python
"""Smoke test for the C27 FakeUtilEndpoint helper."""

from __future__ import annotations


def test_fake_util_endpoint_returns_configured_snap() -> None:
    """FakeUtilEndpoint replays the snap it was constructed with."""
    from kinoforge.core.util_endpoints import (
        UtilSnapshot,
        UtilSnapshotEndpoint,
    )
    # The helper lives under tests/live/ — import path via conftest
    # adjustment if needed. Implementer may relocate to tests/_helpers/
    # if tests/live/ is excluded from default collection.
    from tests.live._c27_fake_util_endpoint import FakeUtilEndpoint

    snap = UtilSnapshot(None, None, None, None, 1)
    ep = FakeUtilEndpoint(snap)
    assert isinstance(ep, UtilSnapshotEndpoint)  # runtime_checkable Protocol
    assert ep.read_util("any-id") is snap
    assert ep.read_util("other-id") is snap  # idempotent per instance
```

- [ ] **Step 2: Run test — confirm RED**

Expected: FAIL — module not found.

- [ ] **Step 3: Create the helper**

Create `tests/live/_c27_fake_util_endpoint.py`:

```python
"""C27 test helper: a FakeUtilEndpoint that replays a fixed UtilSnapshot.

Used by Phase A1 (`test_c27_phase_a1_uptime_streak_live.py`) to drive
the predicate from the orchestrator side without depending on real
RunPod restart behaviour.
"""

from __future__ import annotations

from kinoforge.core.util_endpoints import UtilSnapshot


class FakeUtilEndpoint:
    """A UtilSnapshotEndpoint satisfier that returns a fixed snap per read.

    Constructor takes the snap to replay; ``read_util`` is pure and
    idempotent. Satisfies the runtime-checkable
    :class:`UtilSnapshotEndpoint` Protocol structurally.
    """

    def __init__(self, snap: UtilSnapshot) -> None:
        """Store the snap to be replayed on every read.

        Args:
            snap: The fixed snapshot. May carry None fields. Phase A1
                uses ``UtilSnapshot(None, None, None, None, 1)``.
        """
        self._snap = snap

    def read_util(self, instance_id: str) -> UtilSnapshot:
        """Return the configured snap. Argument ignored.

        Args:
            instance_id: Ignored — same snap returned for every id.

        Returns:
            The snap supplied at construction.
        """
        return self._snap
```

- [ ] **Step 4: Run test — confirm GREEN**

Run: `pixi run -- pytest tests/test_fake_util_endpoint.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files tests/live/_c27_fake_util_endpoint.py tests/test_fake_util_endpoint.py
git add tests/live/_c27_fake_util_endpoint.py tests/test_fake_util_endpoint.py
git commit -m "test(c27): add FakeUtilEndpoint test helper for Phase A1 smoke

Replays a fixed UtilSnapshot per read. Satisfies UtilSnapshotEndpoint
Protocol structurally.

Spec §7.3 Phase A1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Phase A1 live smoke — FakeUtilEndpoint forcing uptime=1

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Cheapest-pod RunPod offer + FakeUtilEndpoint forcing `uptime_seconds=1` drives the C27 predicate end-to-end. Counter trail `[1,2,3,4,5,6]`, `RESTART_LOOP_REAP` fires at counter=6, pod destroyed, ledger forgotten, cancel-token set, thread stopped. Sidecar `_c27_phase_a1_evidence.json` committed post-spend.

**Files:**
- Create: `tests/live/test_c27_phase_a1_uptime_streak_live.py`
- Create (post-spend): `tests/live/_c27_phase_a1_evidence.json`

**Acceptance Criteria:**
- [ ] RED scaffold committed BEFORE live spend (durability rule).
- [ ] `pixi run preflight` clean before invocation.
- [ ] Cheapest-offer pod provisioned ($0.13/hr range).
- [ ] Counter trail observed: `[1,2,3,4,5,6]` over 6+ ticks at `interval_s=10`.
- [ ] `RESTART_LOOP_REAP` log line observed in orchestrator output.
- [ ] Pod destroyed (verified via post-fire `provider.list_instances` returning empty for the id).
- [ ] Ledger entry forgotten (verified via `ledger.get(id) is None`).
- [ ] Cancel-token set (test-injected token's flag observed True).
- [ ] Total live spend ≤ $0.05.
- [ ] Sidecar `_c27_phase_a1_evidence.json` committed with: pod_id, offer_rate, counter_trail, fire_timestamp, ledger snapshot at fire, cleanup verification.

**Verify:** `pixi run -- pytest tests/live/test_c27_phase_a1_uptime_streak_live.py -v` → PASS; sidecar exists and contains the required keys.

**Steps:**

- [ ] **Step 1: Write the RED live-smoke scaffold (committed BEFORE spend)**

Create `tests/live/test_c27_phase_a1_uptime_streak_live.py`:

```python
"""C27 Phase A1: FakeUtilEndpoint drives RESTART_LOOP_REAP end-to-end on a real RunPod pod.

Pattern mirrors `test_c26_phase_a_stall_detection_live.py`. Replaces the
C26 low-util FakeUtilEndpoint with one forcing uptime_seconds=1 to fire
the C27 predicate path.

Budget cap: $0.05 (cheapest offer ~$0.13/hr × 23 min).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.core.util_endpoints import UtilSnapshot

_LIVE = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE") != "1",
    reason="Live RunPod smoke; set KINOFORGE_LIVE=1 to enable.",
)

_EVIDENCE = Path(__file__).parent / "_c27_phase_a1_evidence.json"


@_LIVE
def test_c27_phase_a1_uptime_streak_fires_restart_loop_reap() -> None:
    """RED scaffold: written before spend, intentionally fails until evidence committed."""
    from kinoforge.providers.runpod import RunPodProvider
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.core.lifecycle import Ledger
    from tests.live._c27_fake_util_endpoint import FakeUtilEndpoint

    # Cheapest offer construction (mirror C26 Phase A pattern verbatim).
    # ... pod provisioning, ledger.record, loop construction, run for 8 ticks,
    # capture trail, destroy, write evidence ...

    pytest.fail("RED scaffold — evidence sidecar not yet committed.")
```

(Implementer expands the body to mirror `test_c26_phase_a_stall_detection_live.py` verbatim, swapping the C26 low-util Fake for `FakeUtilEndpoint(UtilSnapshot(None, None, None, None, 1))`.)

- [ ] **Step 2: Commit the RED scaffold BEFORE invoking live spend**

```bash
git add tests/live/test_c27_phase_a1_uptime_streak_live.py
git commit -m "test(c27): Phase A1 RED scaffold — FakeUtilEndpoint uptime=1 smoke

Pre-spend commit per durability rule. Fails intentionally until
evidence sidecar lands.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: Preflight clean**

Run: `pixi run preflight`
Expected: exit 0.

- [ ] **Step 4: Run the live smoke**

Run: `KINOFORGE_LIVE=1 pixi run -- pytest tests/live/test_c27_phase_a1_uptime_streak_live.py -v -s`
Expected after implementing the body in Step 1: PASS with `RESTART_LOOP_REAP` log line; sidecar `_c27_phase_a1_evidence.json` written by the test body.

- [ ] **Step 5: Verify cleanup**

Confirm no leaked pods:
```bash
pixi run -e live-runpod -- runpod pod list 2>&1 | grep -i 'id\|status' | head
```
Expected: no pods owned by this smoke remain.

- [ ] **Step 6: Commit the sidecar evidence**

```bash
git add tests/live/_c27_phase_a1_evidence.json
git commit -m "live(c27): Phase A1 PROVEN — RESTART_LOOP_REAP fires on FakeUtilEndpoint uptime=1

Counter trail [1,2,3,4,5,6] over 6 ticks at interval=10s; RESTART_LOOP_REAP
fired at counter=6 (60s elapsed); pod destroyed; ledger forgotten;
cancel-token set. Total spend ~\$0.02 (cap \$0.05).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

```json:metadata
{"files": ["tests/live/test_c27_phase_a1_uptime_streak_live.py", "tests/live/_c27_phase_a1_evidence.json"], "verifyCommand": "KINOFORGE_LIVE=1 pixi run -- pytest tests/live/test_c27_phase_a1_uptime_streak_live.py -v -s && test -f tests/live/_c27_phase_a1_evidence.json", "acceptanceCriteria": ["RED scaffold committed BEFORE live spend", "preflight clean", "cheapest-offer pod provisioned", "counter trail [1,2,3,4,5,6] observed", "RESTART_LOOP_REAP log line observed", "pod destroyed post-fire", "ledger entry forgotten", "cancel-token set", "spend <= $0.05", "sidecar evidence committed"], "userGate": true, "tags": ["user-gate", "live-spend"], "requireEvidenceTokens": [["RED-scaffold", "pre-spend-commit"], ["PROVEN", "evidence-sidecar"]]}
```

---

## Task 13: Phase A2 live smoke — alpine restart loop

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Real `RunPodUtilEndpoint` against a real RunPod restart loop on alpine. Verifies the wire path end-to-end against RunPod's actual restart behaviour at minimum cost.

**Files:**
- Create: `tests/live/test_c27_phase_a2_alpine_restart_loop_live.py`
- Create (post-spend): `tests/live/_c27_phase_a2_evidence.json`

**Acceptance Criteria:**
- [ ] RED scaffold committed BEFORE live spend.
- [ ] `pixi run preflight` clean before invocation.
- [ ] Alpine pod (`alpine:latest`, ~5 MB pull) provisioned with `dockerArgs: "sh -c 'sleep 5; exit 1'"`.
- [ ] Live `runtime.container.uptimeInSeconds < 90` observed for ≥ 8 consecutive ticks at `interval_s=15`.
- [ ] `RESTART_LOOP_REAP` log line observed.
- [ ] Pod destroyed post-fire.
- [ ] Total live spend ≤ $0.15.
- [ ] Sidecar `_c27_phase_a2_evidence.json` includes raw GraphQL `runtime{}` responses at each tick for forensic record.

**Verify:** `pixi run -- pytest tests/live/test_c27_phase_a2_alpine_restart_loop_live.py -v` → PASS; sidecar exists.

**Steps:**

- [ ] **Step 1: Write the RED live-smoke scaffold**

Create `tests/live/test_c27_phase_a2_alpine_restart_loop_live.py`:

```python
"""C27 Phase A2: alpine restart-loop drives RESTART_LOOP_REAP via real RunPodUtilEndpoint.

Pattern mirrors `test_c27_phase_a1_*` but uses the real
`RunPodGraphQLUtilEndpoint` (no Fake) and provisions an alpine pod with
a self-restarting entrypoint to produce actual container churn.

Budget cap: $0.15 (cheapest offer ~$0.13/hr × ~70 min, expect ~5 min actual).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

_LIVE = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE") != "1",
    reason="Live RunPod smoke; set KINOFORGE_LIVE=1 to enable.",
)
_EVIDENCE = Path(__file__).parent / "_c27_phase_a2_evidence.json"


@_LIVE
def test_c27_phase_a2_alpine_restart_loop_fires_restart_loop_reap() -> None:
    """RED scaffold."""
    # Provision an alpine pod with dockerArgs: sh -c 'sleep 5; exit 1'.
    # Construct HeartbeatLoop with the real RunPodGraphQLUtilEndpoint,
    # interval_s=15, restart_loop_window_s=120, restart_loop_uptime_threshold_s=90.
    # Run until RESTART_LOOP_REAP fires (or timeout).
    # Capture every util tick's raw GraphQL runtime{} response.
    # Write evidence sidecar.
    pytest.fail("RED scaffold — evidence sidecar not yet committed.")
```

- [ ] **Step 2: Commit RED scaffold BEFORE spend**

```bash
git add tests/live/test_c27_phase_a2_alpine_restart_loop_live.py
git commit -m "test(c27): Phase A2 RED scaffold — alpine restart-loop smoke

Pre-spend commit. Fails until evidence lands.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: Preflight clean**

Run: `pixi run preflight` → exit 0.

- [ ] **Step 4: Run live smoke**

Run: `KINOFORGE_LIVE=1 pixi run -- pytest tests/live/test_c27_phase_a2_alpine_restart_loop_live.py -v -s`
Expected: PASS after body implementation; sidecar written.

- [ ] **Step 5: Verify cleanup + commit evidence**

```bash
git add tests/live/_c27_phase_a2_evidence.json
git commit -m "live(c27): Phase A2 PROVEN — alpine restart-loop fires RESTART_LOOP_REAP

Live runtime.container.uptimeInSeconds stayed < 90 for ≥ 8 consecutive
ticks at interval=15s. RESTART_LOOP_REAP fired; pod destroyed.
Total spend ~\$0.05 (cap \$0.15).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

```json:metadata
{"files": ["tests/live/test_c27_phase_a2_alpine_restart_loop_live.py", "tests/live/_c27_phase_a2_evidence.json"], "verifyCommand": "KINOFORGE_LIVE=1 pixi run -- pytest tests/live/test_c27_phase_a2_alpine_restart_loop_live.py -v -s && test -f tests/live/_c27_phase_a2_evidence.json", "acceptanceCriteria": ["RED scaffold committed BEFORE live spend", "preflight clean", "alpine pod with dockerArgs sleep 5 exit 1 provisioned", "uptime < 90 for >= 8 consecutive ticks at interval 15s", "RESTART_LOOP_REAP log line observed", "pod destroyed post-fire", "spend <= $0.15", "sidecar evidence includes raw GraphQL runtime{} responses"], "userGate": true, "tags": ["user-gate", "live-spend"], "requireEvidenceTokens": [["RED-scaffold", "pre-spend-commit"], ["PROVEN", "evidence-sidecar"]]}
```

---

## Task 14: Phase B live smoke — Wan + ComfyUI re-fire (closes deferred gate)

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Re-fire the deferred C25 Task 4 / C26 Task 14 smoke with C27 cfg knobs active. Two acceptance paths: CLEAN-PASS (Wan runs through, warm-reuse ratio < 0.7) OR PROVEN-PROTECTION (`RESTART_LOOP_REAP` fires within 180 s of chronic-restart symptom).

**Files:**
- Create: `tests/live/test_c27_phase_b_wan_warm_reuse_live.py`
- Create (post-spend): `tests/live/_c27_phase_b_evidence.json`

**Acceptance Criteria:**
- [ ] RED scaffold committed BEFORE live spend.
- [ ] `pixi run preflight` clean before invocation.
- [ ] Wan + ComfyUI pod provisioned per deferred-gate config.
- [ ] Either CLEAN-PASS: both CLI invocations complete, `gen2_elapsed / gen1_elapsed < 0.7`, video logged to `successful-generations.md`.
- [ ] Or PROVEN-PROTECTION: `RESTART_LOOP_REAP` fires within 180 s of symptom; pod destroyed; cancel-token propagates; outer CLI exits with cancel reason (not hung at 22 min).
- [ ] Sidecar evidence records counter trails, raw GraphQL responses at threshold crossing, ledger snapshots, CLI exit reason, which acceptance path closed.
- [ ] Total live spend ≤ $0.60.

**Verify:** `KINOFORGE_LIVE=1 pixi run -- pytest tests/live/test_c27_phase_b_wan_warm_reuse_live.py -v -s` → PASS; sidecar exists with `acceptance_path` ∈ {`"CLEAN-PASS"`, `"PROVEN-PROTECTION"`}.

**Steps:**

- [ ] **Step 1: Copy the C26 Phase B file as the C27 Phase B starting point**

```bash
cp tests/live/test_c26_phase_b_wan_warm_reuse_live.py tests/live/test_c27_phase_b_wan_warm_reuse_live.py
```

- [ ] **Step 2: Update the new file to assert C27 acceptance shape**

Edit `tests/live/test_c27_phase_b_wan_warm_reuse_live.py`:

- Rename top-line docstring + test name to `test_c27_phase_b_wan_restart_loop_protected`.
- Enable C27 cfg in the LifecycleConfig used by the smoke:
  ```python
  lifecycle = LifecycleConfig(
      budget=0.60,
      # ... existing fields ...
      restart_loop_reap_enabled=True,
      restart_loop_window_s=180.0,
      restart_loop_uptime_threshold_s=90.0,
  )
  ```
- Add two-path acceptance handling at the assertion site:
  ```python
  if outcome == "CLEAN-PASS":
      assert gen2_elapsed / gen1_elapsed < 0.7
      _log_to_successful_generations(...)
      acceptance_path = "CLEAN-PASS"
  elif outcome == "PROVEN-PROTECTION":
      assert any("RESTART_LOOP_REAP" in line for line in captured_logs)
      assert cli_exit_reason == "cancel-token"
      acceptance_path = "PROVEN-PROTECTION"
  else:
      pytest.fail(f"Phase B did not close: outcome={outcome!r}")
  ```
- Write the evidence sidecar with `acceptance_path` recorded.

- [ ] **Step 3: Commit RED scaffold BEFORE spend**

```bash
git add tests/live/test_c27_phase_b_wan_warm_reuse_live.py
git commit -m "test(c27): Phase B RED scaffold — Wan re-fire with C27 cfg active

Pre-spend commit. Two-path acceptance: CLEAN-PASS or
PROVEN-PROTECTION. Closes the deferred C25 Task 4 / C26 Task 14 gate
either way.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: Preflight clean**

Run: `pixi run preflight` → exit 0.

- [ ] **Step 5: Run live smoke**

Run: `KINOFORGE_LIVE=1 pixi run -- pytest tests/live/test_c27_phase_b_wan_warm_reuse_live.py -v -s`
Expected: PASS via one of the two acceptance paths; sidecar written.

- [ ] **Step 6: If CLEAN-PASS — log to `successful-generations.md` per CLAUDE.md**

Per CLAUDE.md durability rule: "Any kinoforge generation that produces a video … MUST get a new detailed section in `/workspace/successful-generations.md` per the schema in that file's preamble."

If outcome was CLEAN-PASS and a new capability axis is introduced (e.g. first Wan + ComfyUI t2v with C27 protections active), add a new section per schema. If `(runpod, comfyui, wan-14b-fp16, t2v)` tuple already exists, append a "See also" line under the existing TOC entry.

- [ ] **Step 7: Commit sidecar evidence (+ successful-generations.md if updated)**

```bash
git add tests/live/_c27_phase_b_evidence.json
[ -n "$(git status --porcelain successful-generations.md)" ] && git add successful-generations.md
git commit -m "live(c27): Phase B PROVEN — closes deferred C25 Task 4 / C26 Task 14 gate

Acceptance via <CLEAN-PASS|PROVEN-PROTECTION>. Sidecar records
acceptance_path + counter trails + ledger snapshots.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

```json:metadata
{"files": ["tests/live/test_c27_phase_b_wan_warm_reuse_live.py", "tests/live/_c27_phase_b_evidence.json", "successful-generations.md"], "verifyCommand": "KINOFORGE_LIVE=1 pixi run -- pytest tests/live/test_c27_phase_b_wan_warm_reuse_live.py -v -s && grep -q 'acceptance_path' tests/live/_c27_phase_b_evidence.json", "acceptanceCriteria": ["RED scaffold committed BEFORE live spend", "preflight clean", "Wan + ComfyUI pod provisioned", "CLEAN-PASS OR PROVEN-PROTECTION reached", "sidecar records acceptance_path", "spend <= $0.60", "if CLEAN-PASS: successful-generations.md updated per CLAUDE.md schema"], "userGate": true, "tags": ["user-gate", "live-spend", "deferred-gate-closer"], "requireEvidenceTokens": [["RED-scaffold", "pre-spend-commit"], ["CLEAN-PASS", "PROVEN-PROTECTION"]]}
```

---

## Task 15: Closeout — PROGRESS.md + spec §13 + C26 §17 pointer

**Goal:** Append C27 CLOSED line to PROGRESS.md §C; fill spec §13 closeout section with Phase A1/A2/B outcomes; append C26 spec §17 cross-reference pointer.

**Files:**
- Modify: `PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md` (§13)
- Modify: `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md` (§17)

**Acceptance Criteria:**
- [ ] PROGRESS.md §C has the C27 CLOSED line with spec path + Phase A1/A2/B closure status.
- [ ] C27 spec §13 filled with: Phase A1 SHA, Phase A2 SHA, Phase B SHA + acceptance_path, deferred-gate closure note.
- [ ] C26 spec §17 has a single appended pointer paragraph referencing the C27 spec §13.

**Verify:** `git log --oneline -5` shows the closeout commit on HEAD.

**Steps:**

- [ ] **Step 1: Append C27 line to PROGRESS.md §C**

Edit `PROGRESS.md`. Under §C, append:

```markdown
- **C27 — Restart-loop stall detection.** CLOSED. Adds `_update_uptime_counter` + `_restart_loop_reap_predicate` + `Verdict.RESTART_LOOP_REAP` on the C26 substrate. Phase A1 PROVEN (sidecar `_c27_phase_a1_evidence.json`), Phase A2 PROVEN (sidecar `_c27_phase_a2_evidence.json`), Phase B closed via <PATH> (sidecar `_c27_phase_b_evidence.json`). Spec `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md`. Closes deferred C25 Task 4 / C26 Task 14 gate.
```

(`<PATH>` = `CLEAN-PASS` or `PROVEN-PROTECTION` per Task 14 outcome.)

- [ ] **Step 2: Fill C27 spec §13 closeout**

Edit `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md`. Replace the §13 stub with:

```markdown
## 13. Closeout (2026-06-13)

- **Phase A1 outcome:** PROVEN. Sidecar
  `tests/live/_c27_phase_a1_evidence.json` (commit `<SHA>`).
  Counter trail `[1,2,3,4,5,6]`; RESTART_LOOP_REAP fired at counter=6.
- **Phase A2 outcome:** PROVEN. Sidecar
  `tests/live/_c27_phase_a2_evidence.json` (commit `<SHA>`).
  Live `runtime.container.uptimeInSeconds < 90` for ≥ 8 ticks; predicate
  fired on real `RunPodUtilEndpoint` wire path.
- **Phase B outcome:** <CLEAN-PASS|PROVEN-PROTECTION>. Sidecar
  `tests/live/_c27_phase_b_evidence.json` (commit `<SHA>`).
- **C25 Task 4 / C26 Task 14 deferred-gate closure:** Closed via Phase B
  outcome above.
- **PROGRESS.md §C C27 status line:** CLOSED.
- **C26 spec §17 cross-reference:** Pointer added (commit `<SHA>`).
```

(Implementer substitutes `<SHA>` values from `git log --oneline` after each commit.)

- [ ] **Step 3: Append C26 spec §17 cross-reference**

Edit `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md`. Append to §17:

```markdown
### C27 — restart-loop stall detection (2026-06-13)

C26 shipped PARTIAL — Phase A (steady-low-util stall) PROVEN; Phase B
(Wan + ComfyUI restart-loop class) FAILED-DESIGN-HOLE. The restart-loop
gap is closed by C27 — sibling `_update_uptime_counter` +
`_restart_loop_reap_predicate` + `Verdict.RESTART_LOOP_REAP` on the C26
substrate. See
`docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md`
§13 for the Phase A1 / A2 / B outcomes.
```

- [ ] **Step 4: Commit closeout**

```bash
git add PROGRESS.md docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md
git commit -m "docs(c27): closeout — PROGRESS §C C27 CLOSED + spec §13 + C26 §17 pointer

Records Phase A1 PROVEN, Phase A2 PROVEN, Phase B
<CLEAN-PASS|PROVEN-PROTECTION>. Closes deferred C25 Task 4 / C26
Task 14 gate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task ordering + dependencies

Sequential where required, parallel-safe where independent.

```
Task 1 (Verdict)                 ─┐
Task 2 (_update_uptime_counter)  ─┤── independent
Task 5 (LifecycleConfig)         ─┘
Task 3 (predicate)               ──── depends on Task 1 (uses Verdict for callsite)
Task 4 (classify row 3'')        ──── depends on Tasks 1, 3
Task 6 (interfaces + Config)     ──── depends on Task 5
Task 7 (HeartbeatLoop kwargs)    ──── depends on Task 2
Task 8 (_maybe_fire_reap)        ──── depends on Tasks 7, 3
Task 9 (CLI flag)                ──── depends on Task 6 (lifecycle fields exist)
Task 10 (callsite threading)     ──── depends on Tasks 6, 7, 8
Task 11 (FakeUtilEndpoint helper) ─── independent (could land anytime after Task 2)
Task 12 (Phase A1 live smoke)    ──── depends on Tasks 4, 8, 10, 11
Task 13 (Phase A2 live smoke)    ──── depends on Tasks 4, 8, 10
Task 14 (Phase B live re-fire)   ──── depends on Tasks 4, 8, 10, 12, 13 (de-risk ladder)
Task 15 (closeout)               ──── depends on Tasks 12, 13, 14
```

Recommended execution order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15. This sequence minimises cross-task rework and keeps each commit reviewable.

---

## Spec acceptance criteria → task mapping

| Spec §8 criterion | Implemented by |
| ----------------- | -------------- |
| 1. New unit tests green | Tasks 1-9 each ship their own unit tests RED→GREEN. |
| 2. Pre-existing C26 tests untouched | Verified at end of each task (`pixi run -- pytest tests/ -v`); Task 8 rename audit catches any C26 test that mentioned the old `_maybe_fire_stall_reap`. |
| 3. Core-import-ban invariant | No new provider import from `core/` — predicate uses C26's existing `provider_util_supported`. Verified by `tests/test_core_invariant.py` running in standard suite. |
| 4. Phase A1 PROVEN | Task 12. |
| 5. Phase A2 PROVEN | Task 13. |
| 6. Phase B closed | Task 14. |
| 7. `pre-commit run --all-files` clean | Every task ends with `pixi run pre-commit run --files …`; final task closes by re-running over the full diff if needed. |
| 8. `pixi run preflight` clean before each live smoke | Tasks 12, 13, 14 each include a preflight step. |

---

## Self-review

**1. Spec coverage:**

- §1 Purpose → no task (narrative).
- §2 Decisions locked → no task (narrative).
- §3 Architecture → Tasks 2, 3, 4, 7, 8 (the wire diagram concretised in code).
- §4.1 `_update_uptime_counter` → Task 2.
- §4.2 `_restart_loop_reap_predicate` → Task 3.
- §4.3 Verdict + DEFAULT_APPLY_POLICY → Task 1.
- §4.4 classify row 3'' wiring → Task 4.
- §4.5 HeartbeatLoop self-classify → Tasks 7 + 8.
- §5.1 LifecycleConfig extensions → Task 5.
- §5.2 interfaces.Lifecycle + Config.lifecycle() wiring → Task 6.
- §5.3 Per-entry ledger override → covered by Task 3 (predicate reads the key) + Task 9 (CLI persists it).
- §5.4 CLI flag → Task 9.
- §5.5 Five-callsite audit → Task 10.
- §6 Ledger schema delta → Task 7 (touch persistence) + Task 9 (CLI override).
- §7.1 Unit tests → Tasks 2, 3, 4, 5, 7, 8 each ship their slice.
- §7.2 Core-import-ban → covered by acceptance criterion 3 (no new task; ban is enforced by existing test).
- §7.3 Live smoke ladder → Tasks 11, 12, 13, 14.
- §8 Acceptance criteria → covered by all tasks (see mapping table above).
- §9 Invariants preserved → verified inside Task acceptance criteria (tie-breaker, kill-switch, backward-compat).
- §10 Out of scope → no task.
- §11 Risk register → no task (mitigation lives in cfg + spec narrative).
- §12 Task preview → THIS PLAN.
- §13 Closeout → Task 15.
- §14 Wire-discovery notes → no task (already-known from C26).
- §15 PROGRESS.md updates → Task 15.

No gaps detected.

**2. Placeholder scan:**

- "TBD" / "TODO" / "implement later" — none.
- "Similar to Task N" — none (every task carries its own code blocks).
- "Add appropriate error handling" — Tasks 7, 8 reuse the existing C26 `try/except` pattern around `destroy_instance` / `forget`; not vague hand-waving.
- "Write tests for the above" — every task lists the test names + expected output.
- One soft spot: Task 9 CLI test ("Implementer should locate the C26 stall_window override test and copy its shape verbatim, only swapping the flag name and ledger key") — acceptable because the C26 test exists in the same file and the pattern is mechanical. If unclear, the implementer should grep `rg 'stall_window_override' tests/test_cli.py` and copy the matching test bodily.

**3. Type consistency:**

- `_update_uptime_counter` signature: `(prev_counter: int, *, snap: UtilSnapshot | None, uptime_threshold_s: float) -> int` — matches usage in Tasks 7, 8, 12.
- `_restart_loop_reap_predicate` signature: matches usage in classify (Task 4) and `_maybe_fire_reap` (Task 8).
- `LifecycleConfig.restart_loop_*` field names match across Tasks 5, 6, 9, 10.
- `interfaces.Lifecycle.restart_loop_window_s` vs `LifecycleConfig.restart_loop_window_s` — same name; collapse to `None` in Task 6 is the only divergence. Documented in spec §5.2.
- `HeartbeatLoop.__init__` kwargs `restart_loop_window_s` + `restart_loop_uptime_threshold_s` match callsite threading in Task 10.
- `classify()` kwargs `restart_loop_window_s` + `restart_loop_uptime_threshold_s` match callsite threading in Task 10.
- Ledger field `consecutive_low_uptime_count` used identically in Tasks 7 (write) and 3 (read) and 4 (classify).
- Ledger field `restart_loop_window_s` (per-entry override) used identically in Tasks 9 (write) and 3 (read).
- Verdict identifier `RESTART_LOOP_REAP` consistent in Tasks 1, 4, 8.
- Log line format string in Task 8 names `low_util_counter`/`low_uptime_counter` — distinct from the ledger keys `consecutive_low_util_count`/`consecutive_low_uptime_count` (log is human-facing; ledger is machine-facing). Intentional.

No type drift detected.
