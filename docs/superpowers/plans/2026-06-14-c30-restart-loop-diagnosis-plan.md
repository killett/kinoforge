# C30 — RunPod restart-loop diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the C30 fault-isolation infrastructure (diagnostics module + spend ledger + 9 live-probe scaffolds + 5 cfgs) and run the live decision tree until a single hypothesis is confirmed decisively + inverse-control flipped. Diagnose-only — zero production-code mutation.

**Architecture:** New additive subtree `src/kinoforge/diagnostics/c30_probe.py` exposes `create_probe_pod`, `PodStatusPoller`, `count_trap_fires`, `classify_run`, `assert_under_cap`, `destroy_with_retry`. All A1/A0' live tests use direct GraphQL (bypassing `kinoforge deploy`). A2–A6 live tests use `kinoforge deploy` with per-phase cfg files that disable both reap predicates and enable diagnostic_mode. Predecessor-sidecar gating ensures cheap RED-scaffold commits before any spend, so the durability rule holds.

**Tech Stack:** Python 3.13, pixi, pytest, RunPod GraphQL (existing `RunPodGraphQLClient`), boto3 (S3 trap-fire counting), `kinoforge.cli` for A2–A6 deploys.

---

## Open question resolutions (from spec §8)

These were investigated during plan writing — locking them in here so the implementing agent does not re-derive.

1. **`cfg.provider.runpod.selfterm_enabled` knob — does NOT exist.** `RunPodProvider.create_instance` unconditionally injects `KINOFORGE_SELFTERM_SCRIPT` (src/kinoforge/providers/runpod/__init__.py:606). The A2 inverse therefore runs via `c30_probe.create_probe_pod` (direct GraphQL, no kinoforge deploy, no selfterm env), with `provision_script="echo c30-a2-inverse-no-selfterm && sleep 600"`.
2. **`runtime.uptimeSeconds` field path — actual field is `pod.runtime.uptimeInSeconds`** (CamelCase with "In"). Source: src/kinoforge/providers/runpod/util.py:47 and :164. `PodStatusPoller` GraphQL query template: `pod(input: {podId: "<id>"}) { id desiredStatus runtime { uptimeInSeconds } }`. Python-side accessor: `runtime["uptimeInSeconds"]`.
3. **C28 trap pre-amble — INLINE in `c30_probe.py` as private constant `_C28_TRAP_PREAMBLE_LINES`.** The pre-amble currently lives in `src/kinoforge/engines/comfyui/__init__.py` lines 1284–1330. Extracting a shared helper would touch `engines/` and violate the C30 non-goal "zero production-code mutation". Inlining costs ~50 lines of duplicated bash but keeps C30 read-only over production. Add a code comment citing the source location so future drift is detectable.
4. **`src/kinoforge/diagnostics/` — no collision.** No existing subtree of that name; no existing module imports `kinoforge.diagnostics`. Clear to land.

---

## File structure

### New files

```
src/kinoforge/diagnostics/
  __init__.py                         # empty, marks package
  c30_probe.py                        # all 6 helpers + _C28_TRAP_PREAMBLE_LINES

src/kinoforge/cfg/
  c30_phase_a2.yaml                   # empty provision, diag_mode=True, reaps off
  c30_phase_a3.yaml                   # + git clone ComfyUI
  c30_phase_a4.yaml                   # + pip install requirements
  c30_phase_a5.yaml                   # + Wan custom-node clones + import smoke
  c30_phase_a6.yaml                   # full Wan; mirrors C28 Phase A v5 cfg

tests/diagnostics/
  __init__.py
  test_c30_classify_run.py            # SURVIVED / RESTARTED_N / AMBIGUOUS rules
  test_c30_count_trap_fires.py        # S3 lister edge cases
  test_c30_spend_ledger.py            # assert_under_cap + ledger schema
  test_c30_create_probe_pod_wire_shape.py  # mocked GraphQL payload assertions
  test_c30_pod_status_poller.py       # uptimeInSeconds polling behaviour
  test_c30_destroy_with_retry.py      # retry-until-pod-gone behaviour

tests/live/
  test_c30_phase_a1a_sleep_no_port_live.py
  test_c30_phase_a1b_sleep_port_declared_live.py
  test_c30_phase_a1c_sleep_port_listener_live.py
  test_c30_phase_a0prime_alt_image_live.py
  test_c30_phase_a2_empty_provision_live.py
  test_c30_phase_a3_clone_only_live.py
  test_c30_phase_a4_clone_pip_live.py
  test_c30_phase_a5_custom_nodes_live.py
  test_c30_phase_a6_full_wan_control_live.py
  _c30_spend_ledger.json              # ledger; tracked in git
  # _c30_phase_*_evidence.json sidecars committed per live run
```

### Files NOT touched

`src/kinoforge/orchestrator/`, `src/kinoforge/engines/`, `src/kinoforge/providers/`, `src/kinoforge/core/` — entirely read-only.

---

## Task list

Tasks 0–7 are pure-code, no live spend. Task 8 commits the RED live scaffold (durability rule: scaffold ships before spend). Tasks 9–12 walk the live decision tree; total expected spend ≈ $0.10, hard cap $2.00 enforced by ledger.

### Task 0: Module scaffold + package marker

**Goal:** Create `src/kinoforge/diagnostics/` package and empty `c30_probe.py` so subsequent tasks have an import target.

**Files:**
- Create: `src/kinoforge/diagnostics/__init__.py`
- Create: `src/kinoforge/diagnostics/c30_probe.py`
- Create: `tests/diagnostics/__init__.py`

**Acceptance Criteria:**
- [ ] `from kinoforge.diagnostics import c30_probe` succeeds in a fresh Python.
- [ ] No other file in the tree is touched.
- [ ] Pre-commit clean.

**Verify:** `pixi run -- python -c "from kinoforge.diagnostics import c30_probe; print(c30_probe.__name__)"` → prints `kinoforge.diagnostics.c30_probe`.

**Steps:**

- [ ] **Step 1: Create empty package files.**

`src/kinoforge/diagnostics/__init__.py`:
```python
"""C30 fault-isolation diagnostics — read-only over production code."""
```

`src/kinoforge/diagnostics/c30_probe.py`:
```python
"""C30 probe helpers — direct-GraphQL pod probes, S3 trap-fire counting,
verdict classification, spend-ledger enforcement, and verify-and-retry
destroy. All public helpers are documented in
``docs/superpowers/specs/2026-06-14-c30-restart-loop-diagnosis-design.md``.
"""

from __future__ import annotations
```

`tests/diagnostics/__init__.py`:
```python
```

- [ ] **Step 2: Verify import.**

Run: `pixi run -- python -c "from kinoforge.diagnostics import c30_probe; print(c30_probe.__name__)"`
Expected: `kinoforge.diagnostics.c30_probe`

- [ ] **Step 3: Commit.**

```bash
git add src/kinoforge/diagnostics/ tests/diagnostics/
git commit -m "feat(c30): scaffold diagnostics package for fault-isolation probes"
```

---

### Task 1: Implement and test `classify_run`

**Goal:** Verdict classifier maps `(poll_trail, fire_count)` to one of `SURVIVED / RESTARTED_N / AMBIGUOUS` per spec §3 rules.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py`
- Create: `tests/diagnostics/test_c30_classify_run.py`

**Acceptance Criteria:**
- [ ] `Verdict` is an `Enum` with members `SURVIVED`, `RESTARTED`, `AMBIGUOUS`.
- [ ] `classify_run([(0.0, 1), (30.0, 31), (60.0, 61)], fire_count=0)` returns `Verdict.SURVIVED`.
- [ ] `classify_run([(0.0, 1), (30.0, 1), (60.0, 1)], fire_count=4)` returns `Verdict.RESTARTED` (uptime resets + ≥3 fires).
- [ ] `classify_run([(0.0, 100), (30.0, 130)], fire_count=2)` returns `Verdict.AMBIGUOUS` (1–2 fires).
- [ ] `classify_run([], fire_count=0)` returns `Verdict.AMBIGUOUS` (no evidence).
- [ ] `classify_run([(0.0, None)], fire_count=0)` returns `Verdict.AMBIGUOUS` (uptime unobservable).
- [ ] Single-sample trail with fire_count=0 returns `Verdict.AMBIGUOUS` (not enough samples to assert monotonicity).
- [ ] 100% line coverage on `classify_run`.

**Verify:** `pixi run -- pytest tests/diagnostics/test_c30_classify_run.py -v` → 7 passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

`tests/diagnostics/test_c30_classify_run.py`:
```python
"""Unit tests for ``c30_probe.classify_run`` verdict classifier."""

from __future__ import annotations

import pytest

from kinoforge.diagnostics.c30_probe import Verdict, classify_run


def test_survived_when_monotonic_and_no_fires() -> None:
    """0 fires + uptime increases monotonically across ≥2 samples → SURVIVED."""
    trail = [(0.0, 1), (30.0, 31), (60.0, 61)]
    assert classify_run(trail, fire_count=0) is Verdict.SURVIVED


def test_restarted_when_uptime_resets_and_fires_present() -> None:
    """Uptime drops to a smaller value AND ≥3 trap fires → RESTARTED."""
    trail = [(0.0, 1), (30.0, 1), (60.0, 1), (90.0, 1)]
    assert classify_run(trail, fire_count=4) is Verdict.RESTARTED


def test_ambiguous_when_one_or_two_fires() -> None:
    """1–2 fires is below the ≥3 threshold and above 0 → AMBIGUOUS."""
    trail = [(0.0, 100), (30.0, 130)]
    assert classify_run(trail, fire_count=2) is Verdict.AMBIGUOUS


def test_ambiguous_when_trail_empty() -> None:
    """No samples at all → cannot decide → AMBIGUOUS."""
    assert classify_run([], fire_count=0) is Verdict.AMBIGUOUS


def test_ambiguous_when_uptime_none() -> None:
    """Poll returned None for uptime (RunPod transient) → AMBIGUOUS."""
    assert classify_run([(0.0, None)], fire_count=0) is Verdict.AMBIGUOUS


def test_ambiguous_when_single_sample_and_no_fires() -> None:
    """Single sample cannot establish monotonicity → AMBIGUOUS."""
    assert classify_run([(0.0, 30)], fire_count=0) is Verdict.AMBIGUOUS


def test_restarted_takes_precedence_over_monotonic_appearance() -> None:
    """Even if uptime later climbs again, ≥3 fires means RESTARTED."""
    trail = [(0.0, 60), (30.0, 90), (60.0, 1), (90.0, 31)]
    assert classify_run(trail, fire_count=5) is Verdict.RESTARTED
```

- [ ] **Step 2: Run tests, confirm they fail.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_classify_run.py -v`
Expected: ImportError on `Verdict` and `classify_run` — RED.

- [ ] **Step 3: Implement.**

Append to `src/kinoforge/diagnostics/c30_probe.py`:
```python
from collections.abc import Sequence
from enum import Enum


class Verdict(Enum):
    """Outcome classes for a 10-minute probe window.

    SURVIVED  — pod stayed up the whole window; no trap fires; uptime
                monotonically increased across all samples.
    RESTARTED — pod cycled ≥3 times within the window (trap-fire count
                is the authoritative signal; uptime drops corroborate).
    AMBIGUOUS — evidence cannot distinguish the two; rerun the probe
                or treat as RESTARTED conservatively per spec §3.
    """

    SURVIVED = "survived"
    RESTARTED = "restarted"
    AMBIGUOUS = "ambiguous"


def classify_run(
    poll_trail: Sequence[tuple[float, int | None]],
    fire_count: int,
) -> Verdict:
    """Classify a probe run from its poll trail and S3 trap-fire count.

    Args:
        poll_trail: ``(elapsed_seconds, uptime_in_seconds)`` per sample.
            ``uptime_in_seconds`` may be ``None`` when the GraphQL
            ``pod(podId)`` response lacked a ``runtime`` block (transient).
        fire_count: Number of ``diag-*.txt`` objects under the run's
            S3 prefix.

    Returns:
        Verdict per spec §3 rules.
    """
    if fire_count >= 3:
        return Verdict.RESTARTED
    if fire_count >= 1:
        return Verdict.AMBIGUOUS
    # fire_count == 0 here.
    if len(poll_trail) < 2:
        return Verdict.AMBIGUOUS
    uptimes = [u for _, u in poll_trail]
    if any(u is None for u in uptimes):
        return Verdict.AMBIGUOUS
    # All non-None at this point; check strict monotonic increase.
    for prev, curr in zip(uptimes, uptimes[1:], strict=True):
        assert prev is not None and curr is not None  # narrowed by check above
        if curr <= prev:
            return Verdict.AMBIGUOUS
    return Verdict.SURVIVED
```

- [ ] **Step 4: Run tests, confirm green.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_classify_run.py -v`
Expected: 7 passed.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_classify_run.py
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_classify_run.py
git commit -m "feat(c30): classify_run verdict classifier with monotonic-uptime rule"
```

---

### Task 2: Implement and test `count_trap_fires`

**Goal:** S3 list helper returns the number of `diag-*.txt` objects under a given `bucket/prefix`. Treats `NoSuchKey` / empty as 0.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py`
- Create: `tests/diagnostics/test_c30_count_trap_fires.py`

**Acceptance Criteria:**
- [ ] `count_trap_fires(s3_client, "bkt", "prefix/")` returns the number of objects whose `Key` matches `prefix/diag-*.txt`.
- [ ] Ignores objects whose `Key` doesn't match the `diag-*.txt` pattern (e.g. a stray `notes.txt`).
- [ ] Handles paginated `ListObjectsV2` responses (continuation token).
- [ ] Treats absent prefix (empty `Contents`) as 0.
- [ ] Treats `botocore.exceptions.ClientError` with code `NoSuchKey` as 0.
- [ ] 100% line coverage.

**Verify:** `pixi run -- pytest tests/diagnostics/test_c30_count_trap_fires.py -v` → 5 passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

`tests/diagnostics/test_c30_count_trap_fires.py`:
```python
"""Unit tests for ``c30_probe.count_trap_fires``."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from kinoforge.diagnostics.c30_probe import count_trap_fires


class _StubS3:
    """Minimal stub for the boto3 S3 client surface ``count_trap_fires`` uses."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        idx = len(self.calls) - 1
        if idx >= len(self._pages):
            return {}
        return self._pages[idx]


def test_returns_count_of_diag_files() -> None:
    s3 = _StubS3(
        [
            {
                "Contents": [
                    {"Key": "p/diag-20260614T000000Z.txt"},
                    {"Key": "p/diag-20260614T000030Z.txt"},
                ],
            }
        ]
    )
    assert count_trap_fires(s3, "bkt", "p/") == 2


def test_ignores_non_diag_keys() -> None:
    s3 = _StubS3(
        [
            {
                "Contents": [
                    {"Key": "p/diag-20260614T000000Z.txt"},
                    {"Key": "p/notes.txt"},
                    {"Key": "p/diag-20260614T000030Z.txt.bak"},
                ],
            }
        ]
    )
    assert count_trap_fires(s3, "bkt", "p/") == 1


def test_paginates_correctly() -> None:
    s3 = _StubS3(
        [
            {
                "Contents": [{"Key": "p/diag-1.txt"}, {"Key": "p/diag-2.txt"}],
                "IsTruncated": True,
                "NextContinuationToken": "tok",
            },
            {"Contents": [{"Key": "p/diag-3.txt"}]},
        ]
    )
    assert count_trap_fires(s3, "bkt", "p/") == 3
    assert s3.calls[1]["ContinuationToken"] == "tok"


def test_empty_prefix_returns_zero() -> None:
    s3 = _StubS3([{}])
    assert count_trap_fires(s3, "bkt", "p/") == 0


def test_no_such_key_returns_zero() -> None:
    class _NoSuchKey:
        def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "absent"}},
                "ListObjectsV2",
            )

    assert count_trap_fires(_NoSuchKey(), "bkt", "p/") == 0
```

- [ ] **Step 2: Confirm RED.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_count_trap_fires.py -v`
Expected: ImportError on `count_trap_fires`.

- [ ] **Step 3: Implement.**

Append to `src/kinoforge/diagnostics/c30_probe.py`:
```python
import re
from typing import Any

from botocore.exceptions import ClientError

_DIAG_KEY_PATTERN = re.compile(r"/diag-\d{8}T\d{6}Z\.txt$")


def count_trap_fires(s3_client: Any, bucket: str, prefix: str) -> int:
    """Count ``diag-YYYYMMDDTHHMMSSZ.txt`` objects under ``bucket/prefix``.

    Args:
        s3_client: A boto3 S3 client (or anything with a compatible
            ``list_objects_v2`` method).
        bucket: S3 bucket name (no scheme).
        prefix: Key prefix. Must include the trailing slash if the
            prefix is a directory.

    Returns:
        Number of diag-pattern objects. Returns 0 on ``NoSuchKey``.
    """
    total = 0
    continuation: str | None = None
    try:
        while True:
            kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation is not None:
                kw["ContinuationToken"] = continuation
            page = s3_client.list_objects_v2(**kw)
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "")
                if _DIAG_KEY_PATTERN.search(key):
                    total += 1
            if not page.get("IsTruncated"):
                return total
            continuation = page.get("NextContinuationToken")
            if continuation is None:
                return total
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            return 0
        raise
```

- [ ] **Step 4: Confirm GREEN.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_count_trap_fires.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit.**

```bash
pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_count_trap_fires.py
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_count_trap_fires.py
git commit -m "feat(c30): count_trap_fires S3 lister with diag-key pattern"
```

---

### Task 3: Implement and test `assert_under_cap` + spend ledger

**Goal:** Cumulative-spend hard cap enforcement before every live probe. Ledger file is tracked from the start so spend across phases stays visible in git.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py`
- Create: `tests/diagnostics/test_c30_spend_ledger.py`
- Create: `tests/live/_c30_spend_ledger.json`

**Acceptance Criteria:**
- [ ] `assert_under_cap(path, hard_cap_usd=1.50)` raises `BudgetCapExceeded` when cumulative >= cap.
- [ ] Missing file is treated as zero cumulative (no raise).
- [ ] Malformed JSON raises a clear `ValueError` (not a silent zero).
- [ ] `append_spend_entry(path, entry)` accumulates `cumulative_usd` correctly and persists.
- [ ] Entry timestamps must be monotonic; non-monotonic appends raise.
- [ ] Initial committed ledger has `{"cumulative_usd": 0.0, "entries": []}`.
- [ ] 100% coverage on both functions.

**Verify:** `pixi run -- pytest tests/diagnostics/test_c30_spend_ledger.py -v` → 6 passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

`tests/diagnostics/test_c30_spend_ledger.py`:
```python
"""Unit tests for ``c30_probe.assert_under_cap`` + ``append_spend_entry``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kinoforge.diagnostics.c30_probe import (
    BudgetCapExceeded,
    append_spend_entry,
    assert_under_cap,
)


def _seed(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_missing_file_is_zero(tmp_path: Path) -> None:
    assert_under_cap(tmp_path / "absent.json", hard_cap_usd=1.50)


def test_under_cap_does_not_raise(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(p, {"cumulative_usd": 0.30, "entries": []})
    assert_under_cap(p, hard_cap_usd=1.50)


def test_at_or_above_cap_raises(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(p, {"cumulative_usd": 1.50, "entries": []})
    with pytest.raises(BudgetCapExceeded):
        assert_under_cap(p, hard_cap_usd=1.50)


def test_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    p.write_text("{this is not json")
    with pytest.raises(ValueError):
        assert_under_cap(p, hard_cap_usd=1.50)


def test_append_accumulates(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(p, {"cumulative_usd": 0.10, "entries": []})
    append_spend_entry(
        p,
        {
            "phase": "a1a",
            "pod_id": "pod-1",
            "gpu_type_id": "RTXA2000",
            "cents_per_hr": 10,
            "start_ts": "2026-06-14T10:00:00-07:00",
            "end_ts": "2026-06-14T10:10:00-07:00",
            "est_spend_usd": 0.017,
        },
    )
    payload = json.loads(p.read_text())
    assert payload["cumulative_usd"] == pytest.approx(0.117)
    assert payload["entries"][-1]["phase"] == "a1a"


def test_append_refuses_non_monotonic(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(
        p,
        {
            "cumulative_usd": 0.0,
            "entries": [
                {
                    "phase": "x",
                    "pod_id": "p",
                    "gpu_type_id": "g",
                    "cents_per_hr": 1,
                    "start_ts": "2026-06-14T10:00:00-07:00",
                    "end_ts": "2026-06-14T10:10:00-07:00",
                    "est_spend_usd": 0.0,
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="monotonic"):
        append_spend_entry(
            p,
            {
                "phase": "y",
                "pod_id": "p2",
                "gpu_type_id": "g",
                "cents_per_hr": 1,
                "start_ts": "2026-06-14T09:00:00-07:00",  # earlier!
                "end_ts": "2026-06-14T09:10:00-07:00",
                "est_spend_usd": 0.0,
            },
        )
```

- [ ] **Step 2: Confirm RED.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_spend_ledger.py -v`
Expected: ImportError on `BudgetCapExceeded`, `append_spend_entry`, `assert_under_cap`.

- [ ] **Step 3: Implement.**

Append to `src/kinoforge/diagnostics/c30_probe.py`:
```python
import json
from datetime import datetime
from pathlib import Path


class BudgetCapExceeded(RuntimeError):
    """Raised when cumulative spend would meet or exceed the hard cap."""


def _read_ledger(path: Path) -> dict:
    if not path.exists():
        return {"cumulative_usd": 0.0, "entries": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed C30 spend ledger at {path}: {exc}") from exc


def assert_under_cap(path: Path, hard_cap_usd: float) -> None:
    """Raise ``BudgetCapExceeded`` if cumulative spend in ``path`` >= cap."""
    payload = _read_ledger(path)
    cumulative = float(payload.get("cumulative_usd", 0.0))
    if cumulative >= hard_cap_usd:
        raise BudgetCapExceeded(
            f"Cumulative C30 spend ${cumulative:.4f} >= cap ${hard_cap_usd:.2f}"
        )


def append_spend_entry(path: Path, entry: dict) -> None:
    """Append a spend entry and rewrite the ledger.

    Args:
        path: Ledger JSON path.
        entry: Dict with keys ``phase``, ``pod_id``, ``gpu_type_id``,
            ``cents_per_hr``, ``start_ts``, ``end_ts``, ``est_spend_usd``.
            Timestamps must be ISO-8601 with offset.

    Raises:
        ValueError: If ``start_ts`` precedes the last existing entry's
            ``end_ts``.
    """
    payload = _read_ledger(path)
    entries = list(payload.get("entries", []))
    if entries:
        last_end = datetime.fromisoformat(str(entries[-1]["end_ts"]))
        new_start = datetime.fromisoformat(str(entry["start_ts"]))
        if new_start < last_end:
            raise ValueError(
                f"Entry start_ts {entry['start_ts']} is not monotonic vs "
                f"prior entry end_ts {entries[-1]['end_ts']}"
            )
    entries.append(entry)
    cumulative = float(payload.get("cumulative_usd", 0.0)) + float(
        entry["est_spend_usd"]
    )
    path.write_text(
        json.dumps(
            {"cumulative_usd": round(cumulative, 6), "entries": entries},
            indent=2,
        )
        + "\n"
    )
```

- [ ] **Step 4: Confirm GREEN.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_spend_ledger.py -v`
Expected: 6 passed.

- [ ] **Step 5: Create initial ledger.**

`tests/live/_c30_spend_ledger.json`:
```json
{
  "cumulative_usd": 0.0,
  "entries": []
}
```

- [ ] **Step 6: Commit.**

```bash
pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_spend_ledger.py tests/live/_c30_spend_ledger.json
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_spend_ledger.py tests/live/_c30_spend_ledger.json
git commit -m "feat(c30): spend ledger with hard-cap enforcement and monotonic-time guard"
```

---

### Task 4: Implement and test `_C28_TRAP_PREAMBLE_LINES` + `create_probe_pod`

**Goal:** Direct GraphQL `podFindAndDeployOnDemand` wrapper. Inlines the C28 trap pre-amble bash so the live probes capture death state to S3. Wire-shape assertions cover the exact `dockerArgs` strings for A1a/A1b/A1c/A0'a.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py`
- Create: `tests/diagnostics/test_c30_create_probe_pod_wire_shape.py`

**Acceptance Criteria:**
- [ ] `_C28_TRAP_PREAMBLE_LINES` is a `list[str]` matching the bash block currently in `src/kinoforge/engines/comfyui/__init__.py` lines 1284–1330 (function body of the diagnostic-mode pre-amble).
- [ ] `create_probe_pod(client, image, ports, provision_script, env, gpu_type_id, run_id, diag_bucket)` issues exactly one GraphQL mutation matching `podFindAndDeployOnDemand` with the expected input shape.
- [ ] `env` MUST include `KINOFORGE_DIAG_BUCKET` and `KINOFORGE_DIAG_PREFIX` (set from `run_id`) so the trap pre-amble can upload.
- [ ] When `ports` is `None`, the mutation input omits the `ports` field entirely (does NOT send `ports: null`).
- [ ] The `dockerArgs` is `bash -c "<trap-preamble>\n<provision_script>"` — newline-joined.
- [ ] Returns the new `pod_id`.

**Verify:** `pixi run -- pytest tests/diagnostics/test_c30_create_probe_pod_wire_shape.py -v` → 5 passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

`tests/diagnostics/test_c30_create_probe_pod_wire_shape.py`:
```python
"""Wire-shape assertions for ``c30_probe.create_probe_pod``."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import (
    _C28_TRAP_PREAMBLE_LINES,
    create_probe_pod,
)


class _CapturingClient:
    """Capture the GraphQL mutation payload(s) and return a canned response."""

    def __init__(self, pod_id: str = "pod-abc") -> None:
        self.payloads: list[tuple[str, dict[str, Any]]] = []
        self._pod_id = pod_id

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append((query, variables))
        return {"data": {"podFindAndDeployOnDemand": {"id": self._pod_id}}}


def test_preamble_contains_trap_function() -> None:
    text = "\n".join(_C28_TRAP_PREAMBLE_LINES)
    assert "_kinoforge_diag_capture()" in text
    assert "trap '_kinoforge_diag_capture $?' EXIT" in text
    assert "aws s3 cp /tmp/diag.txt" in text


def test_a1a_no_port_payload() -> None:
    client = _CapturingClient()
    pod_id = create_probe_pod(
        client,
        image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        ports=None,
        provision_script='echo a1a && sleep 600',
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a1a-20260614T120000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    assert pod_id == "pod-abc"
    assert len(client.payloads) == 1
    _, vars_ = client.payloads[0]
    assert "ports" not in vars_["input"]
    docker_args = vars_["input"]["dockerArgs"]
    assert docker_args.startswith('bash -c "')
    assert "_kinoforge_diag_capture()" in docker_args
    assert "echo a1a && sleep 600" in docker_args


def test_a1b_port_declared_payload() -> None:
    client = _CapturingClient()
    create_probe_pod(
        client,
        image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        ports="8188/http",
        provision_script='echo a1b && sleep 600',
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a1b-20260614T130000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    _, vars_ = client.payloads[0]
    assert vars_["input"]["ports"] == "8188/http"


def test_a1c_listener_payload_has_http_server_in_args() -> None:
    client = _CapturingClient()
    create_probe_pod(
        client,
        image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        ports="8188/http",
        provision_script='python3 -m http.server 8188 & sleep 600',
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a1c-20260614T140000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    _, vars_ = client.payloads[0]
    assert "python3 -m http.server 8188" in vars_["input"]["dockerArgs"]


def test_diag_env_propagated_to_input() -> None:
    client = _CapturingClient()
    create_probe_pod(
        client,
        image="ubuntu:22.04",
        ports=None,
        provision_script="apt-get install -y awscli && sleep 600",
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics", "EXTRA": "ok"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a0prime-20260614T150000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    _, vars_ = client.payloads[0]
    env_list = vars_["input"]["env"]
    keys = {e["key"]: e["value"] for e in env_list}
    assert keys["KINOFORGE_DIAG_BUCKET"] == "kinoforge-pod-diagnostics"
    assert keys["KINOFORGE_DIAG_PREFIX"] == "boot-logs/c30-a0prime-20260614T150000"
    assert keys["EXTRA"] == "ok"
```

- [ ] **Step 2: Confirm RED.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_create_probe_pod_wire_shape.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement.**

Append to `src/kinoforge/diagnostics/c30_probe.py`:
```python
# Inlined verbatim from src/kinoforge/engines/comfyui/__init__.py lines
# 1284-1330 (the diagnostic_mode pre-amble in ComfyUIEngine.render_provision).
# Inlined rather than imported because C30 must not touch production code
# (spec §2 non-goal). If the source diverges, sync this constant.
_C28_TRAP_PREAMBLE_LINES: list[str] = [
    "set -euo pipefail",
    "command -v aws >/dev/null 2>&1 || "
    "pip install -q awscli >/dev/null 2>&1 || true",
    "command -v aria2c >/dev/null 2>&1 || "
    "(apt-get update -qq && apt-get install -y -qq aria2 "
    ">/dev/null 2>&1) || true",
    "exec > >(tee -a /tmp/boot.log) 2>&1",
    "trap '_kinoforge_diag_capture $?' EXIT",
    "_kinoforge_diag_capture() {",
    "  local rc=$1",
    "  local last_line",
    "  last_line=$(tail -1 /tmp/boot.log 2>/dev/null || true)",
    "  {",
    "    echo '===== rc ====='; echo \"$rc\";",
    "    echo '===== last_line ====='; echo \"$last_line\";",
    "    echo '===== nvidia-smi ====='; nvidia-smi || true;",
    "    echo '===== df -h ====='; df -h || true;",
    "    echo '===== free -m ====='; free -m || true;",
    "    echo '===== ls -la models/diffusion_models ====='; "
    "ls -la /workspace/ComfyUI/models/diffusion_models 2>/dev/null"
    " || true;",
    "    echo '===== dpkg -l torch ====='; "
    "dpkg -l 2>/dev/null | grep -iE 'torch|cuda' || true;",
    "    echo '===== boot.log ====='; "
    "tail -500 /tmp/boot.log 2>/dev/null || true;",
    "  } > /tmp/diag.txt",
    '  if [ -n "${KINOFORGE_DIAG_BUCKET:-}" ]; then',
    "    aws s3 cp /tmp/diag.txt "
    '"s3://${KINOFORGE_DIAG_BUCKET}/${KINOFORGE_DIAG_PREFIX}/'
    'diag-$(date -u +%Y%m%dT%H%M%SZ).txt" || true',
    "  fi",
    "}",
]


_CREATE_POD_MUTATION = """
mutation podFindAndDeployOnDemand($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) {
    id
    desiredStatus
    imageName
  }
}
""".strip()


def create_probe_pod(
    client: Any,
    *,
    image: str,
    ports: str | None,
    provision_script: str,
    env: dict[str, str],
    gpu_type_id: str,
    run_id: str,
    diag_bucket: str,
) -> str:
    """Create a stock RunPod pod via direct GraphQL with the C28 trap.

    Args:
        client: Object with ``execute(query, variables) -> dict``.
        image: Docker image reference.
        ports: RunPod ``ports`` string (e.g. ``"8188/http"``) or ``None``
            to omit declaration entirely.
        provision_script: Bash to run AFTER the trap pre-amble — the
            actual probe payload (e.g. ``"sleep 600"``).
        env: Additional pod env vars. ``KINOFORGE_DIAG_BUCKET`` and
            ``KINOFORGE_DIAG_PREFIX`` are added/overwritten here.
        gpu_type_id: RunPod GPU type ID string.
        run_id: Per-probe identifier; becomes the S3 prefix suffix.
        diag_bucket: Diagnostics S3 bucket name.

    Returns:
        Newly created pod ID.
    """
    merged_env = dict(env)
    merged_env["KINOFORGE_DIAG_BUCKET"] = diag_bucket
    merged_env["KINOFORGE_DIAG_PREFIX"] = f"boot-logs/{run_id}"

    full_script = "\n".join([*_C28_TRAP_PREAMBLE_LINES, provision_script])
    docker_args = f'bash -c "{full_script}"'

    input_obj: dict[str, Any] = {
        "imageName": image,
        "gpuTypeId": gpu_type_id,
        "dockerArgs": docker_args,
        "env": [{"key": k, "value": v} for k, v in merged_env.items()],
    }
    if ports is not None:
        input_obj["ports"] = ports

    result = client.execute(_CREATE_POD_MUTATION, {"input": input_obj})
    return str(result["data"]["podFindAndDeployOnDemand"]["id"])
```

- [ ] **Step 4: Confirm GREEN.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_create_probe_pod_wire_shape.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit.**

```bash
pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_create_probe_pod_wire_shape.py
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_create_probe_pod_wire_shape.py
git commit -m "feat(c30): create_probe_pod direct-GraphQL wrapper with inlined C28 trap pre-amble"
```

---

### Task 5: Implement and test `PodStatusPoller`

**Goal:** Poll `pod(podId)` every `interval_s` for `runtime.uptimeInSeconds`. Return a trail of `(elapsed_seconds, uptime_in_seconds)` over the window. Handle `None` runtime gracefully.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py`
- Create: `tests/diagnostics/test_c30_pod_status_poller.py`

**Acceptance Criteria:**
- [ ] `PodStatusPoller(client, pod_id, window_s, interval_s, sleep=...).poll()` returns a list of `(elapsed_seconds, uptime_in_seconds)`.
- [ ] Exactly `floor(window_s / interval_s) + 1` samples are emitted (initial + at each interval).
- [ ] Each sample's `elapsed_seconds` matches the monotonic clock used.
- [ ] If `pod(podId)` returns `{ "data": { "pod": None } }` mid-window, `uptime_in_seconds` is `None` for that sample and polling continues.
- [ ] If runtime is null but pod is present, `uptime_in_seconds` is `None` (graceful).
- [ ] The injected `sleep` callable is invoked exactly `samples - 1` times.

**Verify:** `pixi run -- pytest tests/diagnostics/test_c30_pod_status_poller.py -v` → 5 passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

`tests/diagnostics/test_c30_pod_status_poller.py`:
```python
"""Unit tests for ``c30_probe.PodStatusPoller``."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import PodStatusPoller


class _ClockedClient:
    """Returns scripted GraphQL responses in order; advances a clock per call."""

    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = list(scripted)
        self.queries: list[str] = []
        self.calls = 0

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        self.calls += 1
        if not self._scripted:
            return {"data": {"pod": None}}
        return self._scripted.pop(0)


def _ok(uptime: int) -> dict[str, Any]:
    return {
        "data": {
            "pod": {
                "id": "p",
                "desiredStatus": "RUNNING",
                "runtime": {"uptimeInSeconds": uptime},
            }
        }
    }


def test_emits_expected_sample_count() -> None:
    sleeps: list[float] = []
    elapsed = [0.0, 30.0, 60.0, 90.0]
    client = _ClockedClient([_ok(1), _ok(31), _ok(61), _ok(91)])
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=90,
        interval_s=30,
        sleep=lambda s: sleeps.append(s),
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert len(trail) == 4
    assert sleeps == [30, 30, 30]


def test_uptime_propagates_when_runtime_missing() -> None:
    client = _ClockedClient(
        [
            _ok(1),
            {"data": {"pod": {"id": "p", "desiredStatus": "RUNNING", "runtime": None}}},
            _ok(61),
        ]
    )
    elapsed = [0.0, 30.0, 60.0]
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=60,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert [u for _, u in trail] == [1, None, 61]


def test_pod_null_yields_none_uptime() -> None:
    client = _ClockedClient(
        [
            {"data": {"pod": None}},
            _ok(31),
            _ok(61),
        ]
    )
    elapsed = [0.0, 30.0, 60.0]
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=60,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert trail[0][1] is None
    assert trail[1][1] == 31


def test_query_references_uptime_in_seconds() -> None:
    client = _ClockedClient([_ok(0)])
    elapsed = [0.0]
    PodStatusPoller(
        client,
        pod_id="p",
        window_s=0,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert "uptimeInSeconds" in client.queries[0]


def test_elapsed_seconds_match_clock() -> None:
    client = _ClockedClient([_ok(1), _ok(31)])
    elapsed = [100.0, 130.0]
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=30,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert [round(t) for t, _ in trail] == [0, 30]
```

- [ ] **Step 2: Confirm RED.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_pod_status_poller.py -v`
Expected: ImportError on `PodStatusPoller`.

- [ ] **Step 3: Implement.**

Append to `src/kinoforge/diagnostics/c30_probe.py`:
```python
import time
from collections.abc import Callable
from dataclasses import dataclass

_POD_STATUS_QUERY = (
    'query {{ pod(input: {{ podId: "{pod_id}" }}) '
    "{{ id desiredStatus runtime {{ uptimeInSeconds }} }} }}"
)


@dataclass
class PodStatusPoller:
    """Poll ``pod(podId)`` for ``runtime.uptimeInSeconds`` over a window.

    Args:
        client: Object with ``execute(query, variables) -> dict``.
        pod_id: Pod ID to probe.
        window_s: Total polling duration.
        interval_s: Sleep between samples.
        sleep: Injectable sleep (default ``time.sleep``) — enables fast tests.
        clock: Injectable monotonic clock returning seconds (default
            ``time.monotonic``).
    """

    client: Any
    pod_id: str
    window_s: float
    interval_s: float
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def poll(self) -> list[tuple[float, int | None]]:
        """Run the poll loop. Returns trail of ``(elapsed_seconds, uptime)``."""
        trail: list[tuple[float, int | None]] = []
        n_intervals = int(self.window_s // self.interval_s)
        n_samples = n_intervals + 1
        start = self.clock()
        for i in range(n_samples):
            now = self.clock()
            uptime = self._read_uptime()
            trail.append((now - start, uptime))
            if i < n_samples - 1:
                self.sleep(self.interval_s)
        return trail

    def _read_uptime(self) -> int | None:
        q = _POD_STATUS_QUERY.format(pod_id=self.pod_id)
        result = self.client.execute(q, {})
        pod = (result.get("data") or {}).get("pod")
        if pod is None:
            return None
        runtime = pod.get("runtime")
        if runtime is None:
            return None
        val = runtime.get("uptimeInSeconds")
        return int(val) if val is not None else None
```

- [ ] **Step 4: Confirm GREEN.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_pod_status_poller.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit.**

```bash
pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_pod_status_poller.py
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_pod_status_poller.py
git commit -m "feat(c30): PodStatusPoller using pod.runtime.uptimeInSeconds"
```

---

### Task 6: Implement and test `destroy_with_retry`

**Goal:** After `podTerminate`, poll `myself.pods` 3-5× at 3 s; re-issue terminate if `pod_id` still present. Inlines C31 pattern so C30 doesn't leak pods.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py`
- Create: `tests/diagnostics/test_c30_destroy_with_retry.py`

**Acceptance Criteria:**
- [ ] `destroy_with_retry(client, pod_id, attempts=5, sleep_s=3, sleep=...)` issues one terminate then polls.
- [ ] If pod absent from `myself.pods` after first poll, returns immediately without retry.
- [ ] If pod still present, re-issues terminate, sleeps, polls again.
- [ ] After `attempts` total terminate calls without absence, returns and logs a warning (does NOT raise — operator's external guardian is the last line of defense).
- [ ] Returns the number of terminate mutations issued.

**Verify:** `pixi run -- pytest tests/diagnostics/test_c30_destroy_with_retry.py -v` → 4 passed.

**Steps:**

- [ ] **Step 1: Write failing tests.**

`tests/diagnostics/test_c30_destroy_with_retry.py`:
```python
"""Unit tests for ``c30_probe.destroy_with_retry``."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import destroy_with_retry


class _FakeClient:
    """Scripted client returning queued ``myself.pods`` results in order."""

    def __init__(self, list_results: list[list[str]]) -> None:
        self._results = list(list_results)
        self.terminates: list[str] = []
        self.lists = 0

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "podTerminate" in query:
            self.terminates.append(variables.get("podId", ""))
            return {"data": {"podTerminate": None}}
        if "myself" in query:
            ids = self._results[self.lists] if self.lists < len(self._results) else []
            self.lists += 1
            return {"data": {"myself": {"pods": [{"id": pid} for pid in ids]}}}
        raise AssertionError(f"unexpected query: {query[:80]}")


def test_returns_after_first_terminate_when_absent() -> None:
    client = _FakeClient([[]])
    n = destroy_with_retry(client, pod_id="p", attempts=5, sleep_s=0, sleep=lambda _: None)
    assert n == 1
    assert client.terminates == ["p"]


def test_retries_when_pod_still_present() -> None:
    client = _FakeClient([["p"], ["p"], []])
    n = destroy_with_retry(client, pod_id="p", attempts=5, sleep_s=0, sleep=lambda _: None)
    assert n == 3
    assert client.terminates == ["p", "p", "p"]


def test_gives_up_after_max_attempts_without_raising() -> None:
    client = _FakeClient([["p"]] * 10)
    n = destroy_with_retry(client, pod_id="p", attempts=4, sleep_s=0, sleep=lambda _: None)
    assert n == 4


def test_does_not_terminate_unrelated_pods() -> None:
    client = _FakeClient([["other-pod"]])
    n = destroy_with_retry(client, pod_id="p", attempts=3, sleep_s=0, sleep=lambda _: None)
    assert n == 1
    assert client.terminates == ["p"]
```

- [ ] **Step 2: Confirm RED.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_destroy_with_retry.py -v`
Expected: ImportError on `destroy_with_retry`.

- [ ] **Step 3: Implement.**

Append to `src/kinoforge/diagnostics/c30_probe.py`:
```python
import logging

_LOG = logging.getLogger(__name__)

_TERMINATE_MUTATION = (
    "mutation podTerminate($podId: String!) { podTerminate(input: { podId: $podId }) }"
)
_LIST_PODS_QUERY = "query { myself { pods { id } } }"


def destroy_with_retry(
    client: Any,
    *,
    pod_id: str,
    attempts: int = 5,
    sleep_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Issue ``podTerminate`` and verify the pod has actually left ``myself.pods``.

    Args:
        client: GraphQL client.
        pod_id: Pod to terminate.
        attempts: Maximum terminate calls before giving up. Default 5.
        sleep_s: Sleep between polls. Default 3 s.
        sleep: Injectable sleep callable.

    Returns:
        Number of terminate mutations issued.
    """
    n = 0
    for _ in range(attempts):
        n += 1
        client.execute(_TERMINATE_MUTATION, {"podId": pod_id})
        sleep(sleep_s)
        listing = client.execute(_LIST_PODS_QUERY, {})
        pods = (
            (listing.get("data") or {})
            .get("myself", {})
            .get("pods")
            or []
        )
        if not any(p.get("id") == pod_id for p in pods):
            return n
    _LOG.warning(
        "c30 destroy_with_retry: pod %s still present after %d attempts",
        pod_id,
        attempts,
    )
    return n
```

- [ ] **Step 4: Confirm GREEN.**

Run: `pixi run -- pytest tests/diagnostics/test_c30_destroy_with_retry.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit.**

```bash
pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_destroy_with_retry.py
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_destroy_with_retry.py
git commit -m "feat(c30): destroy_with_retry inlining C31 verify-and-retry pattern"
```

---

### Task 7: A2–A6 provision-script constants (PIVOT — direct-GraphQL)

**Pivot rationale (2026-06-14):** Original Task 7 assumed a flat cfg schema (`engine.kind` + `engine.provision_script`, top-level `lifecycle`) and a `Config.load_yaml` loader. Real schema is `compute.lifecycle.*` + `engine.comfyui.{repo, branch, custom_nodes[], image, launch_args}` + `models[]`, loaded via `load_config()` (src/kinoforge/core/config.py:1048). `ComfyUIEngine.render_provision` unconditionally renders the full clone→pip→nodes→download→exec pipeline; the only knob (`slim_mode`) is auto-derived from image prefix and not configurable. There is no `kinoforge deploy --diagnostic-mode` flag.

**Pivot:** Drop the YAML approach. Author A2–A6 as `list[str]` bash constants in `c30_probe.py` that mirror successive rungs of `ComfyUIEngine.render_provision`. Each rung extends the previous by exactly the lines the engine would emit for that step. All live probes use `create_probe_pod` uniformly (Task 4) and pass these constants verbatim as `provision_script` (joined with `\n`). Rigorously honors spec §2 "zero production-code mutation" and gives crisper isolation than re-using the orchestrator pipeline. Lose the side-property that A2 tests selfterm — acceptable because selfterm is a *watchdog* not a *restarter*; the C30 bug is platform-side restart loop, not selfterm action.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py` (append constants + helper)
- Create: `tests/diagnostics/test_c30_provision_walk_down.py`

**Constants to define:**
- `_KINOFORGE_DOWNLOAD_HELPER_LINES: list[str]` — verbatim inline of `kinoforge_download_helper` from `src/kinoforge/engines/comfyui/__init__.py:1226-1274`.
- `_C28_PHASE_A_CUSTOM_NODES: tuple[tuple[str, str], ...]` — `(repo_url, ref_sha)` triples for Kijai/WanVideoWrapper, Kijai/KJNodes, Kosinkadink/VideoHelperSuite (from `tests/live/cfg_c28_phase_a_diagnostic.yaml`).
- `_C28_PHASE_A_MODELS: tuple[tuple[str, str, str], ...]` — `(hf_path, target_subdir, filename)` triples for Wan2_1-T2V-1_3B_fp8, Wan2_1_VAE_bf16, umt5-xxl-enc-fp8.
- `PROVISION_A2_LINES`, `PROVISION_A3_LINES`, `PROVISION_A4_LINES`, `PROVISION_A5_LINES`, `PROVISION_A6_LINES`.
- `PROVISION_A2_LINES` = `["cd /workspace", "sleep 600"]` — stock pod, no work.
- `PROVISION_A3_LINES` adds `[ ! -d ComfyUI ] && git clone --depth 1 --branch master https://github.com/comfyanonymous/ComfyUI ComfyUI`.
- `PROVISION_A4_LINES` adds `cd ComfyUI && pip install -q -r requirements.txt` then `cd /workspace`.
- `PROVISION_A5_LINES` adds `cd /workspace/ComfyUI` then the custom-node clone+pip lines for the three C28 Phase A nodes (mirrors engine line-shape at __init__.py:1369-1389).
- `PROVISION_A6_LINES` adds the download helper, three model downloads using `_kinoforge_download '<url>' '<subdir>/<filename>' '' 'HF_TOKEN'`, and `exec python main.py --listen 0.0.0.0 --port 8188` (replacing the trailing `sleep 600`).

**Acceptance Criteria:**
- [ ] Each `PROVISION_AN_LINES` is `list[str]` and non-empty.
- [ ] A3 contains every line in A2 except the trailing `sleep 600` (monotonic walk-down).
- [ ] A4 contains every non-`sleep` line in A3.
- [ ] A5 contains every non-`sleep` line in A4 + Kijai + KJNodes + VideoHelperSuite clone lines.
- [ ] A6 contains the `_kinoforge_download` helper definition and three model-download invocations and a final `exec python main.py` (no `sleep 600`).
- [ ] A6 does NOT contain `sleep 600` as its terminal line.
- [ ] `HF_TOKEN` appears in A6 (as the token-env-name arg to `_kinoforge_download`) so the live scaffold knows to inject the env var.

**Verify:** `pixi run -- pytest tests/diagnostics/test_c30_provision_walk_down.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Confirm reference cfg.**

Run: `cat tests/live/cfg_c28_phase_a_diagnostic.yaml` (existing real C28 Phase A cfg — provides custom-node refs + model HF refs).

- [ ] **Step 2: Inline `_KINOFORGE_DOWNLOAD_HELPER_LINES` verbatim from src/kinoforge/engines/comfyui/__init__.py:1226-1274.**

Add a header comment citing the source line range so future drift is detectable. The plan §"Open question resolutions" item 3 already covers the inlining rationale.

- [ ] **Step 3: Define `_C28_PHASE_A_CUSTOM_NODES` and `_C28_PHASE_A_MODELS` tuples.**

Model HF URL pattern: `https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/<filename>`.
Target subdirs (from cfg + comfyui/__init__.py TARGET_TO_SUBDIR): diffusion_models → `models/diffusion_models`, vae → `models/vae`, text_encoder → `models/text_encoders`.

- [ ] **Step 4: Define A2–A6 constants with a small helper.**

```python
def _provision_with_sleep(*pre_lines: str) -> list[str]:
    return [*pre_lines, "sleep 600"]
```

A2 = `_provision_with_sleep("cd /workspace")`
A3 = `_provision_with_sleep("cd /workspace", "[ ! -d ComfyUI ] && git clone --depth 1 --branch master https://github.com/comfyanonymous/ComfyUI ComfyUI")`
A4 = A3 minus final sleep + `["cd ComfyUI && pip install -q -r requirements.txt", "cd /workspace"]` + `sleep 600`
A5 = A4 minus final sleep + `cd /workspace/ComfyUI` + for each `(url, ref)` in custom-nodes append the two clone+pip lines + `sleep 600`
A6 = the helper + A5 minus final sleep + per-model `mkdir -p <subdir>` + `[ ! -f <subdir>/<filename> ] && _kinoforge_download '<url>' '<subdir>/<filename>' '' 'HF_TOKEN'` + `cd /workspace/ComfyUI && exec python main.py --listen 0.0.0.0 --port 8188`

- [ ] **Step 5: Write unit tests.**

`tests/diagnostics/test_c30_provision_walk_down.py` — assertions per Acceptance Criteria. Use set-difference / `in` checks to verify cumulative property without over-specifying line order.

- [ ] **Step 6: Run + commit.**

```bash
pixi run -- pytest tests/diagnostics/test_c30_provision_walk_down.py -v
pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_provision_walk_down.py
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c30_provision_walk_down.py
git commit -m "feat(c30): A2-A6 provision-line constants (direct-GraphQL walk-down pivot)"
```

---

### Task 8: RED scaffold for all 9 live tests

**Goal:** Commit all 9 `tests/live/test_c30_phase_*.py` scaffolds before any live spend (durability rule). Each scaffold implements the 8-step flow (preflight → cap-check → predecessor-sidecar gate → create_probe_pod or kinoforge deploy → poll → count fires → classify → sidecar → destroy_with_retry). Without sidecars, tests skip; once sidecars arrive they execute and write their own.

**Files:**
- Create: `tests/live/test_c30_phase_a1a_sleep_no_port_live.py`
- Create: `tests/live/test_c30_phase_a1b_sleep_port_declared_live.py`
- Create: `tests/live/test_c30_phase_a1c_sleep_port_listener_live.py`
- Create: `tests/live/test_c30_phase_a0prime_alt_image_live.py`
- Create: `tests/live/test_c30_phase_a2_empty_provision_live.py`
- Create: `tests/live/test_c30_phase_a3_clone_only_live.py`
- Create: `tests/live/test_c30_phase_a4_clone_pip_live.py`
- Create: `tests/live/test_c30_phase_a5_custom_nodes_live.py`
- Create: `tests/live/test_c30_phase_a6_full_wan_control_live.py`
- Create: `tests/live/conftest_c30.py` (shared fixtures: preflight, ledger path, GPU type)

**Acceptance Criteria:**
- [ ] Every live test file imports `c30_probe`.
- [ ] Every test reads predecessor sidecar (where applicable); calls `pytest.skip(...)` with a clear gate-mismatch message when the predecessor verdict doesn't match expectations.
- [ ] A1a is the root: it skips only if `_c30_phase_a1a_evidence.json` already exists (idempotent re-run).
- [ ] Each test computes `est_spend_usd = (elapsed_s / 3600) * (cents_per_hr / 100)` from the poll trail's start/end.
- [ ] Each test writes its sidecar via `append_spend_entry` + a per-phase JSON file.
- [ ] Each test calls `destroy_with_retry` in an `atexit`-registered finalizer.
- [ ] `pixi run -- pytest tests/live/test_c30_phase_*.py -v --co` collects all 9 tests without errors.
- [ ] `pixi run -- pytest tests/live/test_c30_phase_a1b_sleep_port_declared_live.py -v` skips with "predecessor sidecar a1a missing" message (when A1a sidecar absent).

**Verify:** `pixi run -- pytest tests/live/test_c30_phase_*.py -v --co` → 9 tests collected, no errors.

**Steps:**

- [ ] **Step 1: Write shared conftest.**

`tests/live/conftest_c30.py`:
```python
"""Shared fixtures + helpers for C30 live tests."""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import pytest

from kinoforge.diagnostics.c30_probe import (
    PodStatusPoller,
    Verdict,
    append_spend_entry,
    assert_under_cap,
    classify_run,
    count_trap_fires,
    create_probe_pod,
    destroy_with_retry,
)
from kinoforge.providers.runpod.graphql import RunPodGraphQLClient  # adjust import path if different

C30_LEDGER = Path(__file__).parent / "_c30_spend_ledger.json"
C30_DIAG_BUCKET = "kinoforge-pod-diagnostics"
C30_HARD_CAP_USD = 1.50
C30_PER_PROBE_CAP_USD = 0.10
C30_GPU_TYPE_ID = "NVIDIA RTX A2000"  # cheapest community-cloud baseline
C30_GPU_CENTS_PER_HR = 10  # update if pricing drifts


@pytest.fixture(scope="session", autouse=True)
def c30_preflight() -> None:
    """Run `pixi run preflight` once per session — no spend if it fails."""
    result = subprocess.run(
        ["pixi", "run", "preflight"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"preflight failed: {result.stdout}\n{result.stderr}")


@pytest.fixture
def c30_client() -> RunPodGraphQLClient:
    api_key = os.environ["RUNPOD_API_KEY"]
    return RunPodGraphQLClient(api_key=api_key)


@pytest.fixture
def c30_s3():
    return boto3.client("s3")


def c30_run_id(phase: str) -> str:
    return f"c30-{phase}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def c30_estimate_spend(elapsed_s: float, cents_per_hr: int) -> float:
    return (elapsed_s / 3600.0) * (cents_per_hr / 100.0)


def c30_sidecar_path(phase: str) -> Path:
    return Path(__file__).parent / f"_c30_phase_{phase}_evidence.json"


def c30_read_predecessor(phase: str) -> dict | None:
    p = c30_sidecar_path(phase)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def c30_write_sidecar(phase: str, payload: dict) -> None:
    p = c30_sidecar_path(phase)
    p.write_text(json.dumps(payload, indent=2) + "\n")
```

- [ ] **Step 2: Write A1a scaffold (root — no predecessor gate).**

`tests/live/test_c30_phase_a1a_sleep_no_port_live.py`:
```python
"""C30 Phase A1a — stock pod, no ports declared, `sleep 600`.

Tests whether the platform restarts a pod with NO declared port. If the
pod survives, the port-healthcheck hypothesis (H1) is at least
plausible; we proceed to A1b. If the pod restarts, fork to A0' image
isolation.
"""

from __future__ import annotations

import atexit
import json
from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.diagnostics.c30_probe import (
    Verdict,
    append_spend_entry,
    assert_under_cap,
    classify_run,
    count_trap_fires,
    create_probe_pod,
    destroy_with_retry,
    PodStatusPoller,
)

from .conftest_c30 import (
    C30_DIAG_BUCKET,
    C30_GPU_CENTS_PER_HR,
    C30_GPU_TYPE_ID,
    C30_HARD_CAP_USD,
    C30_LEDGER,
    c30_estimate_spend,
    c30_run_id,
    c30_sidecar_path,
    c30_write_sidecar,
)

PHASE = "a1a"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_a1a_stock_pod_no_port_sleep(c30_client, c30_s3) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    assert_under_cap(C30_LEDGER, hard_cap_usd=C30_HARD_CAP_USD)

    run_id = c30_run_id(PHASE)
    pod_id = create_probe_pod(
        c30_client,
        image=IMAGE,
        ports=None,
        provision_script="sleep 600",
        env={},
        gpu_type_id=C30_GPU_TYPE_ID,
        run_id=run_id,
        diag_bucket=C30_DIAG_BUCKET,
    )
    atexit.register(
        destroy_with_retry, c30_client, pod_id=pod_id, attempts=5, sleep_s=3
    )

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()
    poller = PodStatusPoller(
        c30_client,
        pod_id=pod_id,
        window_s=600,
        interval_s=30,
    )
    trail = poller.poll()
    end_t = datetime.now().timestamp()
    end_iso = datetime.now().astimezone().isoformat()

    fire_count = count_trap_fires(c30_s3, C30_DIAG_BUCKET, f"boot-logs/{run_id}/")
    verdict = classify_run(trail, fire_count)

    elapsed = end_t - start_t
    spend = c30_estimate_spend(elapsed, C30_GPU_CENTS_PER_HR)

    append_spend_entry(
        C30_LEDGER,
        {
            "phase": PHASE,
            "pod_id": pod_id,
            "gpu_type_id": C30_GPU_TYPE_ID,
            "cents_per_hr": C30_GPU_CENTS_PER_HR,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": round(spend, 6),
        },
    )
    c30_write_sidecar(
        PHASE,
        {
            "phase": PHASE,
            "verdict": verdict.value,
            "run_id": run_id,
            "pod_id": pod_id,
            "s3_prefix": f"boot-logs/{run_id}/",
            "fire_count": fire_count,
            "poll_trail": trail,
            "est_spend_usd": round(spend, 6),
            "captured_at": end_iso,
        },
    )

    destroy_with_retry(c30_client, pod_id=pod_id, attempts=5, sleep_s=3)

    # Sidecar written regardless of verdict — the next test reads it.
    # Do not assert SURVIVED here; A1a accepts any verdict and routes
    # the next probe accordingly.
    assert verdict in {Verdict.SURVIVED, Verdict.RESTARTED, Verdict.AMBIGUOUS}
```

- [ ] **Step 3: Write A1b scaffold (gated on A1a SURVIVED).**

`tests/live/test_c30_phase_a1b_sleep_port_declared_live.py`:
```python
"""C30 Phase A1b — stock pod, ports=8188/http declared, `sleep 600`.

Tests the port-healthcheck hypothesis. Skip unless A1a SURVIVED.
"""

from __future__ import annotations

import atexit
from datetime import datetime

import pytest

from kinoforge.diagnostics.c30_probe import (
    Verdict,
    append_spend_entry,
    assert_under_cap,
    classify_run,
    count_trap_fires,
    create_probe_pod,
    destroy_with_retry,
    PodStatusPoller,
)

from .conftest_c30 import (
    C30_DIAG_BUCKET,
    C30_GPU_CENTS_PER_HR,
    C30_GPU_TYPE_ID,
    C30_HARD_CAP_USD,
    C30_LEDGER,
    c30_estimate_spend,
    c30_read_predecessor,
    c30_run_id,
    c30_sidecar_path,
    c30_write_sidecar,
)

PHASE = "a1b"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_a1b_stock_pod_port_declared_sleep(c30_client, c30_s3) -> None:
    if c30_sidecar_path(PHASE).exists():
        pytest.skip(f"{PHASE} sidecar already present; idempotent skip")

    predecessor = c30_read_predecessor("a1a")
    if predecessor is None:
        pytest.skip("A1b gated on A1a sidecar; A1a not yet committed")
    if predecessor["verdict"] != Verdict.SURVIVED.value:
        pytest.skip(
            f"A1b gated on A1a=SURVIVED; found A1a={predecessor['verdict']}"
        )

    assert_under_cap(C30_LEDGER, hard_cap_usd=C30_HARD_CAP_USD)

    run_id = c30_run_id(PHASE)
    pod_id = create_probe_pod(
        c30_client,
        image=IMAGE,
        ports="8188/http",
        provision_script="sleep 600",
        env={},
        gpu_type_id=C30_GPU_TYPE_ID,
        run_id=run_id,
        diag_bucket=C30_DIAG_BUCKET,
    )
    atexit.register(
        destroy_with_retry, c30_client, pod_id=pod_id, attempts=5, sleep_s=3
    )

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()
    poller = PodStatusPoller(
        c30_client, pod_id=pod_id, window_s=600, interval_s=30
    )
    trail = poller.poll()
    end_t = datetime.now().timestamp()
    end_iso = datetime.now().astimezone().isoformat()

    fire_count = count_trap_fires(c30_s3, C30_DIAG_BUCKET, f"boot-logs/{run_id}/")
    verdict = classify_run(trail, fire_count)

    elapsed = end_t - start_t
    spend = c30_estimate_spend(elapsed, C30_GPU_CENTS_PER_HR)
    append_spend_entry(
        C30_LEDGER,
        {
            "phase": PHASE,
            "pod_id": pod_id,
            "gpu_type_id": C30_GPU_TYPE_ID,
            "cents_per_hr": C30_GPU_CENTS_PER_HR,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": round(spend, 6),
        },
    )
    c30_write_sidecar(
        PHASE,
        {
            "phase": PHASE,
            "verdict": verdict.value,
            "run_id": run_id,
            "pod_id": pod_id,
            "s3_prefix": f"boot-logs/{run_id}/",
            "fire_count": fire_count,
            "poll_trail": trail,
            "est_spend_usd": round(spend, 6),
            "captured_at": end_iso,
        },
    )

    destroy_with_retry(c30_client, pod_id=pod_id, attempts=5, sleep_s=3)
    assert verdict in {Verdict.SURVIVED, Verdict.RESTARTED, Verdict.AMBIGUOUS}
```

- [ ] **Step 4: Write A1c, A0', A2-A6 scaffolds — all follow the same template with phase-specific gating.**

**PIVOT (2026-06-14):** All A2-A6 scaffolds use `create_probe_pod` uniformly (was `kinoforge deploy --cfg ... --diagnostic-mode`, which doesn't exist). For each phase, copy the A1b template and change:
- `PHASE` constant.
- Predecessor sidecar name + expected verdict (gates below).
- `image`, `ports`, `env` for A1c/A0'.
- For A2-A6: pass `provision_script="\n".join(PROVISION_AN_LINES)` from the Task-7 constants. A6 additionally injects `env={"HF_TOKEN": os.environ["HF_TOKEN"]}` so `_kinoforge_download` resolves the bearer header. A6 sets `ports="8188/http"` so the final `exec python main.py --listen 0.0.0.0 --port 8188` has a port to bind. A2-A5 keep `ports=None`.

Image: A2-A5 use stock `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` (matches A1*). A6 uses the same — slim_mode pre-baked image would short-circuit the walk-down's clone/pip rungs and defeat the experiment.

Gates:
- A1c skipped unless A1b RESTARTED.
- A0' skipped unless A1a RESTARTED.
- A2 skipped unless A1a SURVIVED AND A1b SURVIVED.
- A3 skipped unless A2 SURVIVED.
- A4 skipped unless A3 SURVIVED.
- A5 skipped unless A4 SURVIVED.
- A6 skipped unless A5 SURVIVED.

The full template is identical to A1b's body except for those substitutions; do not abbreviate — copy the full body into each file so each test stands alone.

**Conftest placement (PIVOT):** Original plan named the shared fixtures file `tests/live/conftest_c30.py` — but pytest only auto-loads `conftest.py`, so fixtures defined elsewhere never register. Append the C30 fixtures (`c30_preflight`, `c30_client`, `c30_s3`) and helper functions/constants directly to the existing `tests/live/conftest.py` (currently a 32-line env loader). Tests import constants and helper functions via `from .conftest import ...`; fixtures auto-resolve via pytest plugin discovery.

- [ ] **Step 5: Confirm all 9 collect.**

Run: `pixi run -- pytest tests/live/test_c30_phase_*.py -v --co`
Expected: 9 tests collected, no errors.

- [ ] **Step 6: Confirm A1b skips cleanly when A1a sidecar absent.**

Run: `pixi run -- pytest tests/live/test_c30_phase_a1b_sleep_port_declared_live.py -v`
Expected: 1 skipped with reason mentioning predecessor sidecar.

- [ ] **Step 7: Commit RED scaffold.**

```bash
pixi run pre-commit run --files tests/live/conftest.py tests/live/test_c30_phase_*.py
git add tests/live/conftest.py tests/live/test_c30_phase_*.py
git commit -m "test(c30): RED scaffold for 9-probe fault-isolation tree (gated on predecessor sidecars)"
```

---

### Task 9: Live A1a — root probe

**Goal:** Run the A1a live probe. Sidecar `_c30_phase_a1a_evidence.json` committed regardless of verdict.

**Files:**
- Modify (created by test): `tests/live/_c30_phase_a1a_evidence.json`
- Modify (updated by test): `tests/live/_c30_spend_ledger.json`

**Acceptance Criteria:**
- [ ] `_c30_phase_a1a_evidence.json` exists with one of `verdict ∈ {survived, restarted, ambiguous}`.
- [ ] `_c30_spend_ledger.json` `cumulative_usd` increases by `est_spend_usd` of the A1a entry.
- [ ] No leaked pods after the test exits (verify via direct `myself.pods` GraphQL call after pytest).
- [ ] Test passed (the test asserts only that verdict is one of the three; not asserting SURVIVED).

**Verify:** `pixi run -- pytest tests/live/test_c30_phase_a1a_sleep_no_port_live.py -v` → 1 passed; sidecar present.

**Steps:**

- [ ] **Step 1: Confirm A1a sidecar is absent before run.**

Run: `ls tests/live/_c30_phase_a1a_evidence.json 2>/dev/null || echo absent`
Expected: `absent`.

- [ ] **Step 2: Run A1a.**

Run: `pixi run -- pytest tests/live/test_c30_phase_a1a_sleep_no_port_live.py -v -s`
Expected: 1 passed in ~10 min.

- [ ] **Step 3: Inspect sidecar and ledger.**

Run: `cat tests/live/_c30_phase_a1a_evidence.json` and `cat tests/live/_c30_spend_ledger.json`
Expected: sidecar with verdict; ledger cumulative_usd increased.

- [ ] **Step 4: Verify no leaked pods.**

Run a quick GraphQL probe (a one-line script using `RunPodGraphQLClient`) to list `myself.pods`. Expected: empty or unrelated only.

- [ ] **Step 5: Commit sidecar + ledger.**

```bash
git add tests/live/_c30_phase_a1a_evidence.json tests/live/_c30_spend_ledger.json
git commit -m "test(c30): Phase A1a evidence — verdict=<actual>"
```

---

### Task 10: Live A1b OR A0' — first branch

**Goal:** Run the appropriate next probe based on A1a's verdict.

- If A1a SURVIVED → run A1b.
- If A1a RESTARTED → run A0'.
- If A1a AMBIGUOUS → re-run A1a once (delete sidecar, repeat Task 9); if still ambiguous, treat as RESTARTED.

**Files:**
- Modify (created by test): `tests/live/_c30_phase_a1b_evidence.json` OR `tests/live/_c30_phase_a0prime_evidence.json`
- Modify (updated by test): `tests/live/_c30_spend_ledger.json`

**Acceptance Criteria:**
- [ ] Exactly one of `_c30_phase_a1b_evidence.json` or `_c30_phase_a0prime_evidence.json` is created.
- [ ] The other probe's test skips with a gate message.
- [ ] Ledger cumulative_usd increases by the probe's spend.
- [ ] No leaked pods after the test exits.

**Verify:** `pixi run -- pytest tests/live/test_c30_phase_a1b_sleep_port_declared_live.py tests/live/test_c30_phase_a0prime_alt_image_live.py -v` → 1 passed + 1 skipped, sidecar present.

**Steps:**

- [ ] **Step 1: Read A1a sidecar verdict.**

Run: `pixi run -- python -c "import json; print(json.load(open('tests/live/_c30_phase_a1a_evidence.json'))['verdict'])"`
Note the verdict.

- [ ] **Step 2: Decide branch.**

- SURVIVED → A1b will run, A0' will skip.
- RESTARTED → A0' will run, A1b will skip.
- AMBIGUOUS → delete A1a sidecar and re-run Task 9. If still ambiguous after the rerun, manually edit the A1a sidecar's `verdict` field to `restarted` (record the override in the sidecar's `notes` field) and proceed to A0'.

- [ ] **Step 3: Run both tests; the correct one executes.**

Run: `pixi run -- pytest tests/live/test_c30_phase_a1b_sleep_port_declared_live.py tests/live/test_c30_phase_a0prime_alt_image_live.py -v -s`
Expected: 1 passed (the gated branch) + 1 skipped.

- [ ] **Step 4: Commit sidecar + ledger.**

```bash
git add tests/live/_c30_phase_a1b_evidence.json tests/live/_c30_spend_ledger.json
# OR:
git add tests/live/_c30_phase_a0prime_evidence.json tests/live/_c30_spend_ledger.json
git commit -m "test(c30): Phase <a1b|a0prime> evidence — verdict=<actual>"
```

---

### Task 11: Live inverse-control OR walk-down

**Goal:** Run the inverse control (if a hypothesis was matched) or the next walk-down rung. Spec §3 stop-rule applies: at the first RESTARTED in the walk-down, run that node's inverse immediately.

**Files:**
- Modify (created by test): the next phase's sidecar.
- Modify (updated by test): ledger.

**Acceptance Criteria:**
- [ ] Either an inverse-control sidecar (A1c or equivalent) shows the predicted opposite verdict, OR the walk-down continues with the next rung's sidecar.
- [ ] When inverse flips correctly, the spec exit criterion is satisfied: decisive + inverse evidence both committed.
- [ ] Cumulative spend ≤ $2.00.

**Verify:** `pixi run -- pytest tests/live/test_c30_phase_*.py -v` → all uncalled probes still skip; the chosen probe passed; sidecar committed.

**Steps:**

- [ ] **Step 1: Read the most recent sidecar (A1b or A0' or last walk-down rung).**

If verdict is RESTARTED and a hypothesis maps cleanly (e.g. A1b RESTARTED → H1), proceed to inverse (A1c).
If verdict is SURVIVED, advance the walk-down (A2, then A3, then A4, etc.).

- [ ] **Step 2: Run the chosen probe.**

Run: `pixi run -- pytest tests/live/test_c30_phase_<next>_live.py -v -s`

- [ ] **Step 3: Commit sidecar + ledger.**

```bash
git add tests/live/_c30_phase_<next>_evidence.json tests/live/_c30_spend_ledger.json
git commit -m "test(c30): Phase <next> evidence — verdict=<actual>"
```

- [ ] **Step 4: Repeat until exit criterion holds OR cap reached OR all phases exhausted.**

If the spec's exit criterion (decisive + inverse) is satisfied → proceed to Task 12.
If `cumulative_usd >= $1.50` → halt, proceed to Task 12 with partial-close.
If all 9 phases ran without decisive verdict → partial-close per spec §6 failure-mode rules.

---

### Task 12: Close C30 — PROGRESS.md entry + summary commit

**Goal:** Write the C30 closeout entry to `PROGRESS.md` per the established C-series shape. Cite both sidecars, both S3 prefixes, total spend, named RCA, and the follow-up-phase identifier from spec §6's table.

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] `PROGRESS.md` C30 entry rewritten from stub to CLOSED (or CLOSED PARTIAL).
- [ ] Entry includes: named hypothesis (H1/H2/H4/H5/H_clone/H_pip/NO_REPRODUCTION_BUG_FLED), both sidecar paths, both S3 prefixes, total cumulative spend, and the follow-up phase identifier (e.g. `Follow-up: C32 — kinoforge port-listener preamble`).
- [ ] Entry references the spec + plan files explicitly.
- [ ] No leaked pods after final commit (re-verify via direct GraphQL).
- [ ] Full regression: `pixi run test` shows no new failures vs baseline (allowance: pre-existing concurrency flakes named in C29 closeout).

**Verify:** `git -C . log -1 --format=%s PROGRESS.md` shows the C30 closeout subject; `pixi run -- pytest tests/diagnostics/ -v` → all pass.

**Steps:**

- [ ] **Step 1: Compose the C30 closeout text.**

Template (adjust fields):
```markdown
- ~~**C30. Investigate why RunPod containers restart every ~30s during clone phase.**~~ — **CLOSED** YYYY-MM-DD. Spec: `docs/superpowers/specs/2026-06-14-c30-restart-loop-diagnosis-design.md`. Plan: `docs/superpowers/plans/2026-06-14-c30-restart-loop-diagnosis-plan.md`. **RCA: <H1|H2|H4|H5|H_clone|H_pip|NO_REPRODUCTION_BUG_FLED — full sentence>.** Decisive evidence: `tests/live/_c30_phase_<decisive>_evidence.json` (S3 `boot-logs/<decisive-run-id>/`). Inverse control: `tests/live/_c30_phase_<inverse>_evidence.json` (S3 `boot-logs/<inverse-run-id>/`). Total live spend: ~$<sum>. **Follow-up:** <C32|C28-Phase-B-push|C33|C34|C35|none> per spec §6 mapping.
```

- [ ] **Step 2: Apply the edit to `PROGRESS.md` C30 line (around line 202).**

Use the Edit tool to replace the existing C30 stub with the closeout text.

- [ ] **Step 3: Re-run full regression.**

Run: `pixi run test 2>&1 | tail -20`
Expected: no new failures vs baseline (~2470 passed).

- [ ] **Step 4: Final no-leaked-pod check.**

Issue a one-line script that calls `RunPodGraphQLClient.execute(_LIST_PODS_QUERY, {})` and prints the pod IDs.
Expected: empty or unrelated only.

- [ ] **Step 5: Commit closeout.**

```bash
git add PROGRESS.md
git commit -m "docs(c30): CLOSED — RCA confirmed (<hypothesis>); follow-up <C32|C28PhaseB|...>"
```

---

## Self-review

**Spec coverage:**
- Spec §1 Goal → Task 12 acceptance + spec named-hypothesis output.
- Spec §2 Non-goals → enforced by file list (no orchestrator/engines/providers/core mutation).
- Spec §3 Fault tree → Tasks 8-11 implement the gated 9-probe chain.
- Spec §4 Spend budget → Task 3 ledger + Task 8 per-test cap-check + ledger commit per live task.
- Spec §5 Files to commit → Tasks 0-8 produce every listed file.
- Spec §6 Exit criterion + post-RCA branches → Task 11 stops at decisive+inverse; Task 12 maps RCA to follow-up.
- Spec §7 Testing strategy → Tasks 1-6 unit tests, Task 8 live scaffold, Tasks 9-11 live runs.
- Spec §8 Open questions → Resolved in the "Open question resolutions" section at the top of this plan.

**Placeholder scan:** No "TBD", "TODO", or "implement later". Tasks 7 step 3 and Task 8 step 4 require the implementing agent to look up specific values (GPU type ID, custom-node URLs, kinoforge-deploy invocation pattern) from existing C28 cfgs and tests — these are *references* to live source, not placeholders.

**Type consistency:**
- `Verdict.SURVIVED`/`RESTARTED`/`AMBIGUOUS` used identically across all tasks.
- `BudgetCapExceeded` raised only in Task 3, caught nowhere (it's a hard fail — operator must reset budget).
- `create_probe_pod` signature stable across Task 4 implementation and Task 8 callers.
- `PodStatusPoller` `poll()` returns `list[tuple[float, int | None]]` — consumed identically by `classify_run` and by sidecar writers.

**Self-review fixes inline:** none required after this pass.

---

## Task persistence

After all tasks are created via `TaskCreate`, write the task list to `docs/superpowers/plans/2026-06-14-c30-restart-loop-diagnosis-plan.md.tasks.json`. The executing-plans / subagent-driven-development skill will read it on resume.
