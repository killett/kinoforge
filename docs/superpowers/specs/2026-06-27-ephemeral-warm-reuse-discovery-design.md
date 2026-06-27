# Ephemeral warm-reuse discovery — design spec

**Date:** 2026-06-27
**Status:** Approved (brainstormed 2026-06-27)
**Supersedes:** none (additive to
`2026-06-08-ephemeral-workspaces-design.md` + `2026-06-20-lora-flexible-warm-reuse-design.md`)
**Owner:** kinoforge

## 1. Problem

Two back-to-back `kinoforge --ephemeral generate` invocations with the
same capability key cold-boot two separate pods. The second invocation
prints `[instance overview] No running instances.` and provisions a
fresh pod despite the first invocation's pod being alive and
reusable.

### 1.1 Reproduction (2026-06-27)

```
$ python -m kinoforge --ephemeral generate --config runpod-comfyui-wan-t2v.yaml --mode t2v --prompt "cat"
14:18:09 INFO running provisioner.provision for instance 5ntmvs1ryqvodf
14:24:36 INFO generate completed — artifact uri=...
$ python -m kinoforge --ephemeral generate --config runpod-comfyui-wan-t2v.yaml --mode t2v --prompt "mickey"
14:32:11 [instance overview] No running instances.   ← cold-boots again
```

Wall-clock cost: ~6 minutes wasted per redundant cold-boot. RunPod
cost: ~$0.20/run wasted (Wan T2V on RTX 4090).

### 1.2 Spec intent (locked-in before this gap was found)

`2026-06-08-ephemeral-workspaces-design.md` §10.4:
> `EphemeralSession.__exit__` is therefore scoped to **records, not
> compute** — it deletes the `ArtifactStore` run directory, leaves the
> pod alone, and lets the warm-reuse machinery (current + future)
> decide when the pod actually dies.

Revision banner on the same spec, 2026-06-10:
> `EphemeralSession.__exit__` no longer destroys the compute instance.
> Original wording would have forced cold-boot on every `--ephemeral`
> run, defeating the upcoming warm-reuse roadmap.

`2026-06-20-lora-flexible-warm-reuse-design.md` §10.3:
> Pod-side files survive across ephemeral sessions so
> warm-reuse-with-different-LoRAs works under `--ephemeral`.

Both specs assume cross-session warm-reuse works. It does not.

### 1.3 Root cause

`EphemeralSession.__exit__` correctly leaves the pod alive — verified
in `core/ephemeral.py:159`. But `STRICT_POLICY` sets
`ledger_record=False`, which routes all ledger writes to a per-process
`in_memory_ledger` dict (`core/lifecycle.py:528`). The dict dies with
the process. The disk ledger never sees the entry.

Process #2's matcher reads the disk ledger (empty), gets `[]` from
`find_pods_by_warm_attach_key`, falls through to cold-boot.

The discovery channel between the surviving pod and the next CLI
process is missing.

### 1.4 Non-goals

- Pod-side disk scrub on session exit (already deferred in
  `2026-06-20-lora-flexible-warm-reuse-design.md` §15).
- Cross-machine pod sharing (single-workspace assumption).
- Provider-side enumeration (rejected during brainstorm — see §7).

## 2. Solution overview

A thin disk file alongside `ledger.json` that records the bare
minimum needed for cross-process pod discovery under `--ephemeral`.
Written only by ephemeral runs; read by everyone; cleaned up by
three paths (sweeper, explicit destroy, matcher 404).

```
<artifact_store>/_lifecycle/
├── ledger.json              ← unchanged; non-ephemeral path
└── ephemeral-index.json     ← NEW; written only under --ephemeral
```

## 3. Schema + file format

### 3.1 Path + format

`<artifact_store>/_lifecycle/ephemeral-index.json`. Same dir as
`ledger.json`. Same `ArtifactStore` instance.

```json
{
  "rows": [
    {
      "id": "5ntmvs1ryqvodf",
      "warm_attach_key": "a1b2c3d4e5f6...",
      "endpoint_url": "https://5ntmvs1ryqvodf-8188.proxy.runpod.net",
      "provider": "runpod",
      "created_at_local": "2026-06-27T14:18:09"
    }
  ]
}
```

### 3.2 Field rationale

| Field | Why |
|---|---|
| `id` | Matcher key + cleanup target |
| `warm_attach_key` | Same name as ledger entry field so reader code generalizes; matcher filters on hash equality |
| `endpoint_url` | Lets matcher attach without re-provisioning |
| `provider` | Disambiguates which provider backend to instantiate |
| `created_at_local` | Debugging + sweeper TTL backstop; local TZ per project rule |

Deliberately **not** stored: `status`, `lora_inventory`,
`loras_dir_free_bytes`, `loras_dir_free_bytes_observed_at_local`,
`capability_key_hex`, `last_used_at_local`. Matcher always re-probes
under `--ephemeral` (`core/warm_reuse/matcher.py:161`), so these go
stale instantly.

### 3.3 Concurrency

Read-modify-write under `store.acquire_lock(
"ephemeral-index/_lifecycle", ttl_s=30.0)` — exact pattern Ledger
uses (`core/lifecycle.py:656`). Reads stay lock-free for matcher hot
path.

### 3.4 Visibility

| Mode | Writes? | Reads? |
|---|---|---|
| `--ephemeral` (STRICT) | ✓ | ✓ |
| default (non-ephemeral) | ✗ | ✓ |

Reason: an ephemeral pod is just a pod. Same WAK = same
compatibility, regardless of who provisioned it. Non-ephemeral runs
benefit from discovering ephemeral pods (and vice versa via the
normal ledger).

### 3.5 Privacy posture

The index leaks `(pod_id, warm_attach_key, endpoint_url, provider,
created_at)` to disk. Operator explicitly accepted this trade-off
during brainstorm (2026-06-27). Threat model implications:

- An adversary reading `ephemeral-index.json` learns "kinoforge
  provisioned pod X with capability fingerprint Y at time Z".
- WAK does not directly contain the prompt or LoRA refs, but it
  fingerprints the runtime config and can be linked back to a Config
  file in the repo.
- Original strict-no-disk-trace contract from
  `2026-06-08-ephemeral-workspaces-design.md` §3 is relaxed for this
  one file. Captured here as a deliberate scope change.

## 4. Lifecycle

### 4.1 Write trigger

One write site: `cli/_commands.py::_resolve_warm_instance` cold-create
branch, right after `Ledger.record(instance, warm_attach_key=cfg_wak)`
(~line 570):

```python
ledger.touch(returned_instance.id, warm_attach_key=cfg_wak)
if EphemeralSession.current() is not None:
    ephemeral_index.add(EphemeralIndexRow(
        id=returned_instance.id,
        warm_attach_key=cfg_wak,
        endpoint_url=returned_instance.endpoint_url,
        provider=returned_instance.provider,
        created_at_local=datetime.now().isoformat(),
    ))
```

Symmetric with existing `Ledger.record` timing. If the run crashes
mid-generation the pod is still discoverable next call — which is
correct (pod survived, money was already spent).

### 4.2 Cleanup paths — defense-in-depth

| Path | Trigger | Implementation |
|---|---|---|
| 1 — sweeper reap | `kinoforge sweeper` destroys idle pod | One-line `ephemeral_index.remove(pod_id)` after successful `provider.destroy_instance` in `core/sweeper.py` |
| 2 — explicit destroy | `kinoforge destroy --id <pod>` | One-line `ephemeral_index.remove(pod_id)` in `cli/_commands.py::_cmd_destroy` after `Ledger.forget` |
| 3 — matcher 404 | `re_probe(pod_id)` raises `LoraSwapPodUnreachableError` | `try_warm_attach_with_swap` catches the error (already does); add `ephemeral_index.remove(match.pod_id)` before re-raise |

### 4.3 Failure-mode coverage

| Pod death path | Caught by | Latency |
|---|---|---|
| `kinoforge sweeper` reaps idle | Path 1 | sync |
| `kinoforge destroy --id` explicit | Path 2 | sync |
| In-pod selfterm watchdog (`idle_timeout`) | Path 3 | next probe (~1-2s) |
| `max_lifetime` wall cap (selfterm) | Path 3 | next probe |
| RunPod-side spot interrupt | Path 3 | next probe |
| `BudgetTracker` mid-run kill | Path 2 (it shells to destroy) | sync |

Worst-case staleness: one matcher probe round-trip per dead pod, then
row vanishes.

## 5. Module + integration

### 5.1 New module

`src/kinoforge/core/warm_reuse/ephemeral_index.py`:

```python
@dataclass(frozen=True)
class EphemeralIndexRow:
    id: str
    warm_attach_key: str
    endpoint_url: str
    provider: str
    created_at_local: str

    def to_entry_dict(self) -> dict:
        """Ledger-shaped sparse entry for matcher consumption."""
        return {
            "id": self.id,
            "warm_attach_key": self.warm_attach_key,
            "endpoint_url": self.endpoint_url,
            "provider": self.provider,
        }


class EphemeralIndex:
    _INDEX_NAME = "ephemeral-index.json"
    _NAMESPACE = "_lifecycle"

    def __init__(self, store: ArtifactStore, *, mutate_ttl_s: float = 30.0) -> None: ...
    def add(self, row: EphemeralIndexRow) -> None: ...      # locked RMW; idempotent on id
    def remove(self, pod_id: str) -> None: ...              # locked RMW; no-op if missing
    def rows(self) -> list[EphemeralIndexRow]: ...          # lock-free
    def rows_by_wak(self, wak_hex: str) -> list[EphemeralIndexRow]: ...  # lock-free
```

`add` replaces on `id` collision (idempotent). `remove` is no-op on
missing id. Reads tolerate `FileNotFoundError` → `[]` and malformed
JSON → `[]` + warning log.

### 5.2 Matcher signature change

`src/kinoforge/core/warm_reuse/matcher.py`:

```python
def find_warm_attach_candidate(
    cfg,
    ledger,
    *,
    pod_lock_registry,
    re_probe=None,
    re_probe_threshold_s=300.0,
    download_specs=None,
    ephemeral_index: EphemeralIndex | None = None,  # NEW
) -> WarmAttachMatch | None:
```

After `candidates = ledger.find_pods_by_warm_attach_key(wak_hex)`:

```python
if ephemeral_index is not None:
    ledger_ids = {e["id"] for e in candidates}
    for row in ephemeral_index.rows_by_wak(wak_hex):
        if row.id not in ledger_ids:           # ledger wins on overlap
            candidates.append(row.to_entry_dict())
```

Sparse entries (no `loras_dir_free_bytes`, no `lora_inventory`)
already trigger `re_probe` via the existing `_snapshot_stale(None,
...) -> True` + `free_bytes is None` paths. No new branches in the
eligibility loop.

### 5.3 Call-site wiring

Three call sites consume the matcher today:

1. `core/warm_reuse/integration.py::try_warm_attach_with_swap` — accept + forward kwarg.
2. `cli/_commands.py::_scan_warm_candidates` — construct `EphemeralIndex(store=ledger._store)` + pass.
3. `cli/_commands.py::_dry_run_swap_show` (`--dry-run-swap` flag) — same.

`EphemeralIndex` is constructed unconditionally (non-ephemeral runs
still benefit from finding ephemeral pods).

### 5.4 Cleanup hook in integration

`core/warm_reuse/integration.py::try_warm_attach_with_swap` already
catches the relevant exceptions:

```python
except (LoraSwapDegradedPodError, LoraSwapPodUnreachableError, LoraSwapDiskFullError):
    ledger.touch(match.pod_id, status="degraded")
    if ephemeral_index is not None:
        ephemeral_index.remove(match.pod_id)   # NEW — Path 3
    pod_lock_registry.release(match.pod_id)
    raise
```

Matcher stays exception-clean. Cleanup lives at the integration
layer where the lock-release already lives.

## 6. Tests

### 6.1 Unit — `tests/core/warm_reuse/test_ephemeral_index.py`

| Test | Bug it catches |
|---|---|
| `add` writes row to disk | Silent write-skip regression |
| `add` is idempotent on same `id` (replaces) | Duplicate rows confuse matcher |
| `remove` is no-op when pod_id absent | Crash on double-destroy |
| `rows_by_wak` filters correctly + `[]` when no match | `None` vs `[]` shape drift |
| Read returns `[]` when file absent | First-run crash |
| Read tolerates malformed JSON → `[]` + warn-log | Corrupted index halts matcher |
| Concurrent `add` from two threads under lock → both rows, no torn write | Lost-update from RMW race |
| `to_entry_dict()` shape matches matcher's sparse-entry expectations | KeyError on sparse field |

### 6.2 Unit — `tests/core/warm_reuse/test_matcher_ephemeral_index.py`

| Test | Bug |
|---|---|
| Matcher with `ephemeral_index=None` behaves identically to today (regression guard) | Default-arg change leaks new behavior into non-ephemeral runs |
| Matcher unions ledger + index candidates; ledger wins on `id` collision | Sparse row clobbers richer ledger entry |
| Sparse row triggers `re_probe` | Matcher attaches to ghost without probing |
| Ledger empty + index has one matching row → matcher returns `WarmAttachMatch` | Cross-session ephemeral discovery silently broken |

### 6.3 Cleanup paths — `tests/core/warm_reuse/test_ephemeral_index_cleanup.py`

| Path | Test |
|---|---|
| 1 — sweeper | Stub `provider.destroy_instance` → assert `ephemeral_index.remove(pod_id)` called after success; NOT on failure |
| 2 — `kinoforge destroy` | Same assertion against `_cmd_destroy` handler |
| 3 — matcher 404 | `re_probe` raises `LoraSwapPodUnreachableError` → assert row removed before exception propagates |

### 6.4 Cross-session integration

`tests/integration/test_ephemeral_cross_session_warm_reuse.py`:

```python
def test_two_ephemeral_sessions_share_pod(tmp_path, fake_provider, fake_engine):
    """Process #1 (--ephemeral) provisions pod; process #2 (--ephemeral) attaches.

    Bug: today process #2 cold-boots a second pod because the discovery
    channel is missing.
    """
    with EphemeralSession(enabled=True):
        instance1, _ = _resolve_warm_instance(ctx, cfg, ...)

    # Simulate process boundary: drop the session + its in-memory ledger.

    with EphemeralSession(enabled=True):
        instance2, report = _scan_warm_candidates(ctx, cfg)

    assert instance2.id == instance1.id, (
        f"Expected warm-attach to pod {instance1.id}; "
        f"got fresh pod {instance2.id} (cold-boot regression)"
    )
    assert report.attached == instance1.id
```

Strong assertion: identity check on `pod_id`, not just "found
something". Fake provider with deterministic id.

### 6.5 Visibility tests

- `tests/integration/test_non_ephemeral_consumes_index.py` —
  non-ephemeral process #2 finds ephemeral process #1's pod.
- `tests/core/test_non_ephemeral_does_not_write_index.py` —
  `EphemeralIndex.add` not called when `EphemeralSession.current() is
  None`.

### 6.6 AST-scan invariant

`tests/test_ephemeral_index_write_gated.py` — walks `src/`, asserts
every `ephemeral_index.add(...)` call site is inside a branch
checking `EphemeralSession.current()`. Same pattern as
`tests/test_no_unredacted_writes.py`. Prevents future ungated writes
from leaking the index into non-ephemeral runs.

### 6.7 Live smoke (last; RED scaffold committed first per project rule)

`tests/live/test_runpod_ephemeral_warm_reuse_smoke.py` — two
`kinoforge --ephemeral generate` invocations against RunPod, same
config, different prompts read from
`/workspace/examples/configs/prompts/field-realistic.txt` (per
standard-test-prompt project rule).

Assertions:
- 1st run cold-boots (~3-15 min).
- 2nd run attaches within ~30s of generation start (matcher emits
  `warm-reuse: attached to <id>` to stderr).
- `pixi run kinoforge list` after both runs shows the SAME pod id
  once (not two).
- Final `kinoforge destroy --id <pod>` succeeds;
  `ephemeral-index.json` no longer contains the row (cleanup path 2
  verification).

Cost budget: ~$0.30 (Wan T2V ~$0.40/run cold + ~$0.10 warm + 1
destroy). Within session budget.

`pixi run preflight` must pass before invocation.

## 7. Rejected alternatives

### 7.1 Option A — Provider-side enumeration

Query RunPod (`list pods` filtered by `kinoforge-ephemeral=true` tag)
on cold path, recover WAK from pod metadata. No disk writes; full
privacy.

**Rejected:** operator explicitly accepted disk-leak trade-off
(2026-06-27). Provider-enumeration would require per-provider
`list_ephemeral_pods` implementations + WAK round-trip via pod
metadata — additional complexity for a privacy guarantee the operator
isn't asking for.

Kept here as a fallback if the threat model tightens in future.

### 7.2 Option C — Scrub index row at session exit

Write index row; delete it in `EphemeralSession.__exit__`.

**Rejected:** does not solve the problem. Row gets scrubbed at exactly
the moment the pod transitions to "available for warm-reuse by next
call", so next call still finds nothing. Only useful if discovery is
tied to pod lifecycle, not session lifecycle — which is Option B
(this spec) with extra coordination machinery.

## 8. Open questions

None. All design decisions locked during 2026-06-27 brainstorm.

## 9. References

- `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md`
  — `EphemeralSession`, `EphemeralPolicy`, threat model.
- `docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md`
  — matcher contract, re-probe policy, WAK semantics.
- `docs/superpowers/specs/2026-06-13-b3-warm-reuse-retrofit-design.md`
  — `_scan_warm_candidates`, `warm_reuse_auto_attach` config, default
  warm-reuse behavior.
- `src/kinoforge/core/warm_reuse/matcher.py:113` —
  `find_warm_attach_candidate`.
- `src/kinoforge/core/warm_reuse/integration.py:42` —
  `try_warm_attach_with_swap`.
- `src/kinoforge/core/lifecycle.py:407` — `Ledger`.
- `src/kinoforge/core/ephemeral.py:67` — `STRICT_POLICY`.
