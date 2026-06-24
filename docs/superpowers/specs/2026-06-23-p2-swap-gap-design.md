# P2 swap-gap fix — design (2026-06-23)

Follow-up to `docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md`.
Closes 2 server-side gaps in `wan_t2v_server` `/lora/set_stack` exposed
by Tier-4 release-gate fires on 2026-06-23 (HEAD `2a7d6f0`, PROGRESS.md
line 189-215).

## 1. Symptoms

Tier-4 7-case matrix
(`tests/smoke/release_wan22/test_dual_transformer_routing.py`),
3rd fire on pod `ee38uxn9rs444b` (A100-SXM4-80GB), 5/7 PASS:

- `case_5 wrong_routing_h_into_low_and_l_into_high` → HTTP 500
  (`'Internal Server Error'`). Posts the canonical Arcane pair with
  swapped branches AFTER case_4 left the canonical pair loaded.
- `case_7 same_ref_in_both_branches_composite_key` → HTTP 500. Posts
  ARCANE_HIGH under both `high_noise` and `low_noise` branches.

Both 500s mean an unmapped exception escaped the
`/lora/set_stack` handler (the 4xx catch blocks for `Branch*` and the
`(RuntimeError, ValueError)` rollback path would have caught any of
their members and returned 200 / 4xx).

## 2. Root cause

### 2.1 case_5 — same-ref branch swap

`/lora/set_stack` handler (`wan_t2v_server.py:967-1222`) for case_5:

```
current_keys  = {(HIGH, "high_noise"), (LOW, "low_noise")}        # from case_4
target_keys   = {(HIGH, "low_noise"),  (LOW, "high_noise")}        # swapped
mandatory_evict = current_keys - target_keys = both current keys
already_downloaded_refs = {HIGH, LOW}     # computed PRE-eviction
to_download_refs = []                     # both refs already on disk
```

Two compounded bugs:

1. **`_evict_one` (lines 574-602) unconditionally unlinks the file.**
   For `(HIGH, "high_noise")` eviction, the on-disk ARCANE_HIGH file
   gets deleted — even though target needs the same file under
   `(HIGH, "low_noise")`.

2. **Pending inventory entries for the new (ref, branch) keys are
   never created.** The nested inventory-write loop
   (lines 1117-1135) is gated on `for ref in to_download_refs:` — empty
   when no download is needed. So `_inventory` never receives
   `(HIGH, "low_noise")` or `(LOW, "high_noise")` entries.

`_replace_adapter_stack(target)` (line 605) then runs:

- Pre-load validation gate passes (both branches are valid on MoE).
- `pipe.unload_lora_weights()` wipes the case_4 canonical pair.
- Loop body: `entry = _inventory[(t.ref, t.branch)]` → **`KeyError`**.
  The `(HIGH, "low_noise")` key was never created.

`KeyError` is not in the handler's `except (RuntimeError, ValueError)`
list, so it propagates. FastAPI converts to HTTP 500 with body
`'Internal Server Error'`.

### 2.2 case_7 — likely cascade from case_5

Tier-4 uses a module-scoped `_warm_wan22_pod` fixture; case_7 runs
against pod state left behind by case_5's mid-flight crash:

- `_inventory` was wiped of `(HIGH, "high_noise")` and `(LOW, "low_noise")`
  by `_evict_one` calls before the `KeyError`.
- ARCANE_HIGH + ARCANE_LOW files were unlinked from disk by the same
  calls.
- `pipe.unload_lora_weights()` ran, so pipeline has zero adapters.
- No rollback ran (KeyError outside the catch list).

case_7 with fresh-ish state should succeed against the fixed handler:

- `current_keys = {}`, `to_download_refs = [HIGH]` (one download for
  both target branches via the `_seen_dl` dedup at lines 1027-1031).
- Download succeeds; nested loop creates pending entries for both
  `(HIGH, "high_noise")` and `(HIGH, "low_noise")`.
- `_replace_adapter_stack` loads `lora_0_h` into `transformer` and
  `lora_1_l` into `transformer_2`.

Residual risk: diffusers' `WanLoraLoaderMixin.load_lora_weights` may
raise when loading a high-noise-only state-dict with
`load_into_transformer_2=True` (no matching tensor keys). If so, the
raise will be a `ValueError` / `RuntimeError` — caught by the existing
rollback path and surfaced as HTTP 200 with
`swap_rejected.reason="set_adapters_value_error"`, not 500.

If the live re-fire reveals a non-(ValueError, RuntimeError) raise
class from peft / diffusers, capture the traceback and patch the catch
list as a follow-up. Out of scope for this design.

## 3. Fix

### 3.1 `_evict_one` — file-aware deletion

After popping the `(ref, branch)` inventory entry, only unlink
`loras_dir_path` if no other `(ref, *)` entry survives in
`_inventory`. Pseudocode:

```python
file_path = entry["loras_dir_path"]
_inventory.pop(key, None)
if not any(other_ref == ref for other_ref, _ in _inventory):
    Path(file_path).unlink(missing_ok=True)   # original best-effort
```

This honors Q6 Option 1 composite identity — the file is one physical
artifact shared across `(ref, *)` rows.

### 3.2 `/lora/set_stack` — guarantee inventory coverage of every target key

After the download step (where pending entries for newly downloaded
refs are created), run a second pass that ensures every key in
`target_keys_list` has an inventory entry. For keys still absent
(branch-swap-of-already-downloaded-ref case), seed a pending entry by
copying `loras_dir_path` + `filename` + `size_bytes` from any
surviving `(same_ref, other_branch)` entry. Adapter name +
`last_strength` are left to `_replace_adapter_stack` to populate.

```python
# After download loop, BEFORE _replace_adapter_stack.
on_disk_by_ref: dict[str, dict[str, Any]] = {}
for (ref, _br), entry in _inventory.items():
    on_disk_by_ref.setdefault(ref, entry)
for tref, tbranch in target_keys_list:
    if (tref, tbranch) in _inventory:
        continue
    source = on_disk_by_ref.get(tref)
    if source is None:
        # Defensive: should never trip — to_download_refs would have
        # caught a wholly-absent ref. Raise loud rather than silently
        # short-circuit.
        raise RuntimeError(
            f"set_stack post-download inventory gap for ref={tref} "
            f"branch={tbranch} — no source row found"
        )
    now = datetime.now().isoformat()
    _inventory[(tref, tbranch)] = {
        "ref": tref,
        "filename": source["filename"],
        "size_bytes": source["size_bytes"],
        "loras_dir_path": source["loras_dir_path"],
        "downloaded_at_local": source["downloaded_at_local"],
        "last_used_at_local": now,
        "adapter_name": f"lora_pending_{tref}_{_BRANCH_SHORT[tbranch]}",
        "branch": tbranch,
    }
```

Combined with the §3.1 fix, the case_5 flow is:

1. Eviction pops `(HIGH, "high_noise")` and `(LOW, "low_noise")` from
   inventory. Inventory is now empty. Files are unlinked because no
   surviving `(HIGH, *)` or `(LOW, *)` entry remains at that instant.

Wait — that re-introduces the bug. The §3.1 check happens at eviction
time and the surviving-entry check fails because at THAT moment the
target's new entries don't exist yet.

Revised approach: **seed target inventory entries BEFORE eviction**.

### 3.3 Revised fix — seed target entries first, then evict, then download

Reorder the handler's first half:

```python
target_keys_list = [(t.ref, t.branch) for t in req.target]
target_keys = set(target_keys_list)
current_keys = set(_inventory.keys())

# (NEW) Seed pending target entries from on-disk siblings BEFORE
# eviction. After this, any (ref, *) inventory entry — old OR newly
# seeded — anchors the file on disk for the §3.1 file-aware unlink
# check.
on_disk_by_ref: dict[str, dict[str, Any]] = {}
for (ref, _br), entry in _inventory.items():
    on_disk_by_ref.setdefault(ref, entry)
for tref, tbranch in target_keys_list:
    if (tref, tbranch) in _inventory:
        continue
    source = on_disk_by_ref.get(tref)
    if source is None:
        continue  # genuine download case — handled by to_download_refs
    now = datetime.now().isoformat()
    _inventory[(tref, tbranch)] = {
        "ref": tref,
        "filename": source["filename"],
        "size_bytes": source["size_bytes"],
        "loras_dir_path": source["loras_dir_path"],
        "downloaded_at_local": source["downloaded_at_local"],
        "last_used_at_local": now,
        "adapter_name": f"lora_pending_{tref}_{_BRANCH_SHORT[tbranch]}",
        "branch": tbranch,
    }

# mandatory_evict computed AFTER target seeding so the surviving-entry
# check in _evict_one sees the freshly-seeded sibling.
current_keys = set(_inventory.keys())
mandatory_evict = current_keys - target_keys
```

`mandatory_evict` still excludes the freshly-seeded target keys
(they're in `target_keys`). For case_5: after seeding,
`_inventory` has 4 entries `{(HIGH, h), (HIGH, l), (LOW, l), (LOW, h)}`.
`mandatory_evict = {(HIGH, h), (LOW, l)}`. `_evict_one("HIGH", "h")`
pops the entry, sees `(HIGH, "l")` still in inventory, skips the
unlink. Same for `(LOW, "l")`. Files survive.

`to_download_refs` computed AFTER seeding remains empty (both refs
already represented). `_replace_adapter_stack(target)` succeeds:
`_inventory[(HIGH, "low_noise")]` and `_inventory[(LOW, "high_noise")]`
both exist; their `loras_dir_path` points at on-disk files.

### 3.4 Disk accounting under same-ref multi-branch

`mandatory_freed` (current line 1018) sums `_inventory[k]["size_bytes"]`
for evicted keys. After §3.1, evicting `(HIGH, "high_noise")` while
`(HIGH, "low_noise")` survives does NOT free any disk — the file is
still there. Update:

```python
mandatory_freed = sum(
    _inventory[k]["size_bytes"]
    for k in mandatory_evict
    if not any(
        other_ref == k[0] and other_br != k[1]
        for other_ref, other_br in (target_keys | (current_keys - mandatory_evict))
    )
)
```

Simpler: pre-evict scan — for each `mandatory_evict` key, count its
size only when no other `(ref, *)` key remains in
`(_inventory.keys() - mandatory_evict) ∪ target_keys` after eviction.

For Tier-1 stub tests this nuance doesn't fire (`size_hint=0` default).
Cover with a Tier-1 unit test asserting `mandatory_freed=0` when
mandatory_evict shares its ref with a target key.

## 4. Scope discipline

In scope:

- `_evict_one` file-aware unlink.
- `/lora/set_stack` target-key seeding + post-seed `mandatory_evict`
  + `mandatory_freed` recomputation.
- Tier-1 unit tests against the existing MoE stub
  (`KINOFORGE_STUB_MOE=1`).
- Live Tier-4 re-fire validating both gaps closed.

Out of scope (defer):

- Catch-list extension for peft/diffusers exceptions outside
  `(RuntimeError, ValueError)`. Only address if the live re-fire
  exposes one with a clear traceback.
- Stub mirroring peft's actual state-dict-key filtering. The stub
  records load calls; it does not simulate state-dict shape. The
  Tier-4 live fire is the fidelity check.
- Tier-3 (Wan 2.1) regression coverage — single-transformer rejects
  `branch ∈ {high_noise, low_noise}` at the pre-load gate (already
  green in PROGRESS.md line 159-167).

## 5. Test invariants (for stub-first TDD)

Three Tier-1 unit tests (against `KINOFORGE_STUB_MOE=1`):

| # | Scenario | Invariant |
|---|---|---|
| T-A | Same-ref branch swap | After `set_stack`, inventory keys match swapped target; pre-swap files survived unlink. |
| T-B | Same-ref two branches composite | After `set_stack`, inventory has 2 rows under composite `(ref, branch)`; both rows carry distinct adapter names. |
| T-C | Disk accounting under branch swap | `mandatory_freed` accounts for 0 bytes when every mandatory_evict ref still appears in target. |

T-A is case_5's Tier-1 analog. T-B is case_7's. T-C is the
size-bookkeeping fence that prevents accidental re-introduction of the
"file-was-deleted-but-counted-as-freed" arithmetic bug.

## 6. Live re-fire envelope

- Cap: $4 (user override 2026-06-23).
- Pod: A100 80GB SXM per `gpu_preference` order in
  `examples/configs/release_wan22_diffusers.yaml` after commit
  `2a7d6f0`.
- `--no-reuse` to honor session memory `feedback_use_no_reuse_for_one_shots`.
- Poll cadence per CLAUDE.md "Live smoke monitoring": every 60-90s
  (`gpuUtilPercent`, `cpuPercent`, `memoryPercent`, `costPerHr`).
- Post-run `pixi run kinoforge list` to verify pod gone per CLAUDE.md
  "Live smoke teardown".
- Full 7-case matrix re-fires (module-scoped fixture sequences state;
  isolating to cases 5+7 only would lose case_4's canonical-pair
  preconditioning that case_5 depends on).

## 7. Open follow-ups (carried forward, not in scope)

- C25 RunPod heartbeat substitute (B5b non-mutating heartbeat).
- C26 util-aware stall classify (extends RunPod heartbeat tick).
- Tier-3 fixture-teardown bug (`destroy_all_active_pods` did not reap
  2 Tier-3 pods on 2026-06-23 — PROGRESS.md line 234-237).
