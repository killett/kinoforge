# P2 — Wan 2.2 dual-transformer h/l routing — design

**Anchor:** PROGRESS.md "CLI `--loras` arg — sub-project decomposition" (2026-06-21).
**Predecessor:** P1 server per-LoRA strength weights — CODE-COMPLETE (commits `c96078e..eeecf84`).
**Successor:** P3 CLI `--loras` arg surface — DEFERRED, HIGH.
**Status:** brainstorm complete 2026-06-22; awaiting plan-writing.

---

## 1. Goal & scope

**Goal.** Wan 2.2 14B is a two-transformer MoE (`transformer` = high-noise stage;
`transformer_2` = low-noise stage). Today's pod-side `_replace_adapter_stack`
calls `pipe.load_lora_weights(path, adapter_name=name)` with no per-transformer
target — the LoRA lands in whichever transformer diffusers' default loader
picks. For Wan 2.2 that means roughly half the canonical recipes are running
with their low-noise LoRA patched into the wrong transformer. P2 makes routing
explicit: each LoRA reaches the stage it was trained against.

### In scope

- Add `branch: Literal["high_noise", "low_noise", "auto"]` to `LoraEntry`
  (canonical schema; cfg + vault + server `LoraTarget` all inherit via
  parity lock at `tests/test_lora_schema_parity.py`).
- Server-side pipeline introspection — detect MoE vs single-transformer.
- Per-entry routing in `_replace_adapter_stack`: dispatch to the resolved
  transformer at load time. Primary path uses diffusers' native
  per-transformer kwarg (Approach 1); fallback uses temporary
  `pipe.transformer` rebind (Approach 3). Task 0 of the plan pins which
  path before execution.
- Validation: reject `auto` on MoE; reject explicit `high_noise`/`low_noise`
  on single-transformer.
- Inventory keyed by composite `(ref, branch)` so the same ref can co-exist
  in two branches.
- `is_stack_match` extension: warm-reuse equality compares
  `(ref, strength, branch)` tuples in order.
- Pydantic alias normalization: `h` → `high_noise`, `l` → `low_noise`.
- Tier-3 (Wan 2.1 1.3B) + Tier-4 (Wan 2.2 14B) RED scaffolds proving the
  routing produces the expected style separation.
- Cold-boot env shape (`KINOFORGE_INITIAL_LORA_STACK_JSON`) carries
  `branch` per entry.
- VRAM-OOM rollback snapshot includes `branch` (restores exact prior
  `(ref, strength, branch)` triples).

### Out of scope (P3)

- `--loras` CLI surface.
- Heredoc parser (one LoRA per line, columns = strength/ref/branch).
- Override-vs-append vs `cfg.loras` block.
- CLI alias expansion (`<modelId>:<versionId>` →
  `civitai:<modelId>@<versionId>`).

### Out of scope (separate future spec)

- N-expert routing beyond `{high_noise, low_noise}`. Schema accepts two
  branch values + `auto`; adding `mid_noise` for Wan 2.3 3-expert is a
  separate spec.
- Per-stage strength splits (one LoRA, different strengths in two
  transformers). Operators use the (ref, branch) composite path with two
  LoRA entries.
- LoRA filename-based auto-detection. Branch is always explicit on MoE.

### Pre-conditions inherited from P1

- `LoraEntry` exists with `ref + strength + sha256`.
- `VaultLoRA(LoraEntry)` extends with `label`.
- `LoraTarget` wire shape with `ref + strength`.
- `set_adapters(adapter_weights=...)` server-side wiring.
- `is_stack_match(refs + math.isclose strength)` matcher.
- VRAM-OOM rollback restores refs + strengths.
- `resolve_active_lora_stack` cfg-vs-vault precedence.

---

## 2. Schema changes (`LoraEntry` + parity)

### 2.1 `LoraEntry` extension

**File:** `src/kinoforge/core/lora.py`

```python
class LoraEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$|^$")
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch_alias(cls, v: Any) -> Any:
        if v == "h":
            return "high_noise"
        if v == "l":
            return "low_noise"
        return v
```

### 2.2 Defaults + back-compat

- `branch` defaults to `"auto"`. Existing cfg + vault files without the
  field load unchanged.
- Alias normalization runs `mode="before"` so the `Literal` constraint sees
  the canonical form. Pydantic emits storage form on `.model_dump()`.
- `extra="forbid"` already in place — typos like `branche:` raise
  ValidationError.

### 2.3 Parity propagation

`tests/test_lora_schema_parity.py` enforces field-set equality between
`LoraEntry` and server-side `LoraTarget` (in `wan_t2v_server.py`). After
adding `branch` to `LoraEntry`, `LoraTarget` MUST get the matching field.
Same `Literal` + same alias validator (server copies the validator inline
— no cross-module import per existing parity invariant).

**File:** `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`

```python
class LoraTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch_alias(cls, v: Any) -> Any:
        # MUST stay in lockstep with LoraEntry._normalize_branch_alias
        # — parity locked by tests/test_lora_schema_parity.py.
        if v == "h":
            return "high_noise"
        if v == "l":
            return "low_noise"
        return v
```

### 2.4 Parity test update

Extend `tests/test_lora_schema_parity.py` to verify:
1. `branch` present on both with same `Literal` annotation.
2. Both `_normalize_branch_alias` validators map `h→high_noise` and
   `l→low_noise` identically. Verify via dispatching
   `model_validate({"ref": "x", "branch": "h"})` against both classes
   and comparing `.branch`.

### 2.5 Vault — no migration

`VaultLoRA(LoraEntry)` inherits `branch` automatically. Existing vault
YAML files lack the field → load with `branch="auto"`. MoE recipes
surface a server-side `auto`-not-allowed error on the next
`/lora/set_stack` — actionable, not silent.

### 2.6 Privacy classification

Append to `LoraEntry` docstring's privacy classification:
- `branch` — NON-SENSITIVE (low-entropy enum; same posture as `strength`).

### 2.7 Schema-edge cases (must be tested)

1. Both `branch: h` and `branch: high_noise` round-trip through
   `model_dump()` to `"high_noise"`.
2. Unknown values (`branch: medium`, `branch: m`, `branch: ""`) raise
   ValidationError.
3. Missing field defaults to `"auto"`.
4. `extra="forbid"` rejects `branche:` (typo).
5. `LoraEntry(branch="h").branch == LoraTarget(ref="x", branch="h").branch`
   — cross-class normalization symmetry.

---

## 3. Wire / server changes

### 3.1 `SetStackRequest` — no new top-level field

`SetStackRequest.target: list[LoraTarget]` already carries per-entry data.
Adding `branch` to `LoraTarget` (Section 2) propagates without changing
`SetStackRequest`'s shape. Legacy `target_refs` migrator (P1) defaults
`branch="auto"` on auto-promoted entries — same posture as P1's
`strength=1.0` default.

### 3.2 Inventory — composite (ref, branch) key

**Today:** `_inventory: dict[str, dict[str, Any]]` keyed by `ref` alone.

**P2:**

```python
_inventory: dict[tuple[str, str], dict[str, Any]] = {}
```

Each entry's interior dict gains a `branch` field for round-trip clarity
in `/lora/inventory` HTTP response (`LoraInventoryEntry` Pydantic also
gets `branch`).

**Migration of in-memory state on rolling deploy:** N/A — inventory is
in-process only; pod restart wipes it. Cold-boot rebuilds keyed by
composite from the start.

### 3.3 `_replace_adapter_stack` — per-entry transformer dispatch

Today's body (`wan_t2v_server.py:397-428`) unloads, re-loads each entry
as `lora_{i}`, calls `set_adapters(names, weights)`. P2 changes:

1. **Resolve transformer per entry** via `_resolve_transformer(pipe, branch)`
   (Section 5.2). Raises if mismatch.
2. **Adapter naming.** Was `lora_{i}`. P2: `lora_{i}_{branch_short}`
   (`h` / `l` / `a`). Position-prefix preserves activation order; branch
   suffix avoids collisions across transformers and clarifies logs.
3. **Load dispatch.** Primary path (Approach 1, pending Task 0):

   ```python
   pipe.load_lora_weights(
       entry["loras_dir_path"],
       adapter_name=adapter_name,
       load_into_transformer=target_transformer,  # exact kwarg pending Task 0
   )
   ```

   Fallback path (Approach 3, if Task 0 confirms no kwarg):

   ```python
   original = pipe.transformer
   try:
       pipe.transformer = target_transformer
       pipe.load_lora_weights(entry["loras_dir_path"], adapter_name=adapter_name)
   finally:
       pipe.transformer = original
   ```

   Plan's Task 0 picks one. Spec ships both code blocks behind a
   `# PRIMARY (Task 0)` / `# FALLBACK (Task 0)` switch so the executing
   developer can't pick the wrong one.

4. **`set_adapters` after load.** Activation strategy resolved by Task 0
   (Section 8.3 Question 2). Either single end-of-load
   `set_adapters([all_names], [all_weights])` OR per-transformer
   activation loop (Section 5.4).

### 3.4 Cold-boot `_load_pipeline` — initial stack carries branch

`KINOFORGE_INITIAL_LORA_STACK_JSON` env today (`wan_t2v_server.py:432-475`):
list of `(ref, ArtifactDownloadSpec)` tuples. P2 extension: list of dicts:

```json
[
  {"ref": "...", "download_spec": {...}, "strength": 1.0, "branch": "high_noise"},
  {"ref": "...", "download_spec": {...}, "strength": 0.8, "branch": "low_noise"}
]
```

P1 deferred adding `strength` to env shape; P2 lands strength + branch
together. Legacy `(ref, ArtifactDownloadSpec)` tuple form auto-promoted
with `strength=1.0, branch="auto"`. Orchestrator-side serialization in
`src/kinoforge/engines/diffusers/__init__.py` emits the new dict shape.

### 3.5 `/lora/inventory` response — composite shape

`LoraInventoryEntry` Pydantic gains:

```python
branch: Literal["high_noise", "low_noise", "auto"]
```

Inventory listing returns the new field per row. Matcher-side
(`is_stack_match`) reads it directly.

### 3.6 New error responses

Server-defined exceptions surfaced via HTTP:

- `BranchAutoNotAllowedOnMoE` → 400
  `{"reason": "branch_auto_disallowed_on_moe", "detail": ..., "target_refs_dropped": []}`.
- `BranchUnsupportedOnSingleTransformer` → 400
  `{"reason": "branch_unsupported_single_transformer", "detail": ..., "target_refs_dropped": []}`.

Errors raised before any `load_lora_weights` call → no partial state;
inventory unchanged on rejection.

### 3.7 MoE detection

New module-level helper:

```python
def _detect_moe_arity(pipe: Any) -> int:
    """Return count of transformer* attrs on the pipeline.

    1 for non-MoE, 2 for Wan 2.2, N for future N-expert.
    """
    return sum(
        1 for attr in dir(pipe)
        if attr == "transformer" or attr.startswith("transformer_")
    )

_pipe_arity: int = 1  # set during _load_pipeline; gated by ready event.
```

Hard-coded list (`["transformer", "transformer_2"]`) is the alternative.
Helper version generalizes to Wan 2.3+ N-expert without code edit. Watch
for diffusers adding non-routing-relevant `transformer_*` attrs
(none today; future risk).

`_resolve_transformer` consults `_pipe_arity` for branch-routing decisions.

---

## 4. Matcher changes (`is_stack_match`)

### 4.1 Current shape (P1)

`is_stack_match(desired: list[LoraEntry], inventory: list[LoraInventoryEntry]) -> bool`
compares ordered list of `(ref, strength)` pairs. Strength via
`math.isclose` (rel_tol 1e-9, abs_tol 1e-12 per P1 convention).

### 4.2 P2 shape

```python
def is_stack_match(
    desired: list[LoraEntry],
    inventory: list[LoraInventoryEntry],
) -> bool:
    if len(desired) != len(inventory):
        return False
    for d, i in zip(desired, inventory):
        if d.ref != i.ref:
            return False
        if d.branch != i.branch:
            return False
        if not math.isclose(d.strength, i.last_strength or 1.0,
                            rel_tol=1e-9, abs_tol=1e-12):
            return False
    return True
```

### 4.3 Inventory ingestion

`LoraInventoryEntry` already has `last_strength`. P2 adds `branch`.
Matcher reads both fields directly — no derivation from `adapter_name`
substring tricks.

### 4.4 Ordering preserved from P1

Matcher walks ordered tuples in lockstep. Two stacks differing only in
ordering of identical entries → NOT a match. Diffusers' adapter activation
order affects composition.

### 4.5 Composite-key inventory + matcher

Inventory is dict keyed by `(ref, branch)` server-side, but
`/lora/inventory` HTTP response returns an ORDERED list (load order =
activation order). Matcher consumes the list as-is — does not re-key
locally.

### 4.6 `capability_key` unchanged

Per Q4 (brainstorm): `branch` IS NOT in `capability_key`. Warm pod
identity stays `(base_model, engine, precision)`. Same warm pod serves
any branch combination via `/lora/set_stack` swap. P1 spec §6.2's
strength-invariant test extended to also assert branch-invariance.

### 4.7 New test cases

`is_stack_match`:

1. Same refs + strengths + branches → match.
2. Same refs + strengths + different branches → NO match.
3. Same refs + branches transposed (high_noise/low_noise positions
   swapped) → NO match.
4. Duplicate ref allowed: `[("X", 1.0, h), ("X", 0.8, l)]` matches itself.
5. Duplicate ref, branches transposed:
   `[("X", 1.0, h), ("X", 0.8, l)]` does NOT match
   `[("X", 0.8, l), ("X", 1.0, h)]`.

`capability_key`:

6. Two stacks differing only in branch → SAME `capability_key`.
7. Two stacks differing in base or engine → DIFFERENT `capability_key`
   (P1 regression preserve).

---

## 5. Pipeline detection + routing dispatch

### 5.1 MoE detection (recap)

Section 3.7. Called once during `_load_pipeline` after `_diffusers_load()`
returns. Result cached in module-level `_pipe_arity`. `ready` event set
only after detection completes.

**Test-seam contract.** `KINOFORGE_DIFFUSERS_LOAD_STUB` stub pipelines
(Tier-1 local CPU) must expose `transformer` always and optionally
`transformer_2` when emulating Wan 2.2. Stub fixture in
`tests/_smoke_harness/` gains `moe: bool = False` knob.

### 5.2 `_resolve_transformer` — single dispatch point

```python
def _resolve_transformer(pipe: Any, branch: str) -> Any:
    arity = _pipe_arity
    if arity == 1:
        if branch == "auto":
            return pipe.transformer
        raise BranchUnsupportedOnSingleTransformer(branch=branch, arity=arity)
    # arity >= 2 (MoE)
    if branch == "auto":
        raise BranchAutoNotAllowedOnMoE(arity=arity)
    if branch == "high_noise":
        return pipe.transformer
    if branch == "low_noise":
        return pipe.transformer_2
    raise BranchUnknown(branch=branch)  # Defensive, unreachable under Literal.
```

### 5.3 Routing dispatch (recap)

Section 3.3 primary + fallback paths. Task 0 selects.

### 5.4 `set_adapters` activation under split-transformer LoRAs

**Open risk** — diffusers' `set_adapters([names], adapter_weights=[weights])`
may walk `pipe.transformer.peft_config` only. If LoRAs split across
transformers, activation could be partial.

Task 0 verifies. If activation needs per-transformer dispatch:

```python
high_adapters = [(n, w) for (n, w, b) in zipped if b == "high_noise"]
low_adapters = [(n, w) for (n, w, b) in zipped if b == "low_noise"]
auto_adapters = [(n, w) for (n, w, b) in zipped if b == "auto"]

if auto_adapters:
    pipe.transformer.set_adapters(
        [n for n, _ in auto_adapters], [w for _, w in auto_adapters]
    )
if high_adapters:
    pipe.transformer.set_adapters(
        [n for n, _ in high_adapters], [w for _, w in high_adapters]
    )
if low_adapters:
    pipe.transformer_2.set_adapters(
        [n for n, _ in low_adapters], [w for _, w in low_adapters]
    )
```

Task 0's research dictates the exact form.

### 5.5 Adapter-name uniqueness under composite key

Scheme: `lora_{i}_{branch_short}` where `branch_short ∈ {h, l, a}`.
Position-prefix preserves activation order; branch suffix avoids
collisions across transformers.

Canonical Arcane pair:
- `lora_0_h` (Arcane high-noise → `transformer`)
- `lora_1_l` (Arcane low-noise → `transformer_2`)

Same ref in both branches (Q6 Option 1 allowed):
- `lora_0_h`
- `lora_1_l`

(Position 0 and 1 distinct — name uniqueness preserved.)

### 5.6 `delete_adapters` on eviction — branch-aware

`_evict_one(ref)` today (`wan_t2v_server.py:380-394`) keyed by `ref`.
P2 takes `(ref, branch)`:

```python
def _evict_one(ref: str, branch: str) -> None:
    entry = _inventory.get((ref, branch))
    if entry is None:
        return
    adapter = entry["adapter_name"]
    if hasattr(pipe, "delete_adapters"):
        # delete_adapters may need per-transformer dispatch — Task 0 Question 3.
        pipe.delete_adapters([adapter])
    try:
        Path(entry["loras_dir_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    _inventory.pop((ref, branch), None)
```

Task 0 Question 3 verifies whether per-transformer delete dispatch needed.

---

## 6. Error semantics + VRAM-OOM rollback

### 6.1 Error taxonomy

| Exception | HTTP | Body `reason` | When raised |
|---|---|---|---|
| `BranchAutoNotAllowedOnMoE` | 400 | `branch_auto_disallowed_on_moe` | MoE pipeline + `branch="auto"` |
| `BranchUnsupportedOnSingleTransformer` | 400 | `branch_unsupported_single_transformer` | non-MoE + `branch ∈ {high_noise, low_noise}` |
| `BranchUnknown` | 400 | `branch_unknown` | Defensive — never fires under Pydantic Literal |
| `VRAMRollbackFailure` | 500 | `vram_rollback_failed` | P1's existing exception, branch-aware after P2 |

### 6.2 Response body shape

```json
{
  "reason": "branch_auto_disallowed_on_moe",
  "detail": "pipeline has 2 transformers; entry 0 (ref=civitai:1234@5678) requires explicit branch=high_noise or low_noise",
  "target_refs_dropped": []
}
```

`target_refs_dropped: []` — pre-load reject means no inventory mutation.
Same `SwapRejectedDetails` shape P1 already ships.

### 6.3 Pre-load validation gate

`_replace_adapter_stack` walks `target` BEFORE any
`pipe.unload_lora_weights()` call. First pass: validate every entry's
branch against `_pipe_arity`. If any raises → propagate as 400, no state
mutation. Inventory + pipeline unchanged.

```python
def _replace_adapter_stack(target: list[LoraTarget]) -> None:
    # Pre-load validation — fail before mutating any state.
    resolved: list[tuple[LoraTarget, Any]] = []
    for t in target:
        target_transformer = _resolve_transformer(pipe, t.branch)
        resolved.append((t, target_transformer))

    # Past validation — safe to mutate.
    pipe.unload_lora_weights()
    # ... load each (t, target_transformer) pair ...
```

### 6.4 VRAM-OOM rollback (P1 extension)

P1's rollback (`wan_t2v_server.py:836`, `reason="vram_oom"`) restores
refs + strengths from a pre-call snapshot. P2 extends snapshot to
include branch.

**Snapshot shape (P1 today):**

```python
snapshot = [
    (entry["ref"], entry["last_strength"]) for entry in _inventory.values()
]
```

**P2:**

```python
snapshot = [
    (entry["ref"], entry["last_strength"], entry["branch"])
    for entry in _inventory.values()
]
```

**Rollback path (P1 today):** unload, re-load by ref, restore strengths
via `set_adapters(adapter_weights=...)`.

**P2 rollback:** unload, re-load by `(ref, branch)` via the same
`_resolve_transformer` dispatch, restore strengths via the activation
strategy from Section 5.4. Rollback uses the SAME routing helper —
single source of truth for branch dispatch.

**Rollback-itself-fails contract preserved (P1):** if rollback re-load
raises (VRAM still constrained, file evicted, etc.) → HTTP 500 +
`reason="vram_rollback_failed"`. Inventory state undefined; pod
self-marks unhealthy; orchestrator destroys.

### 6.5 Cold-boot validation (`_load_pipeline`)

Cold-boot env (`KINOFORGE_INITIAL_LORA_STACK_JSON`) parsed at startup.
Same validation pass as `/lora/set_stack`:

- MoE pipeline + any initial entry with `branch="auto"` → `_load_pipeline`
  raises before serving any request. `ready` event NEVER set. Pod exits
  with logged error.
- Single-transformer pipeline + any entry with explicit
  `high_noise`/`low_noise` → same exit.

Operator sees failure in pod boot logs immediately, not after first
generation attempt. Orchestrator's `wait_for_ready` times out → cold-boot
failure surfaces in normal failure path.

### 6.6 Error messages — actionable

Spec mandates error `detail` includes:

- Entry index in `target` (`entry 0`, `entry 1`, ...).
- `ref` of the offending entry.
- Current `_pipe_arity`.
- Suggested fix.

Example:

```
"entry 1 (ref=civitai:1234@5678): pipeline has 1 transformer; branch=low_noise not applicable. Set branch=auto for portable single-transformer cfgs."
```

### 6.7 Test cases

`_replace_adapter_stack`:

1. MoE + all entries `auto` → all rejected, no state mutation.
2. MoE + mixed (one `auto`, one `h`) → entire request rejected (atomic),
   no state mutation.
3. Single-transformer + entry `high_noise` → rejected, no state mutation.
4. MoE + valid `(h, l)` pair → loads both, set_adapters dispatched per
   Section 5.4.
5. VRAM OOM during second entry load → rollback restores pre-call state
   (refs + strengths + branches).
6. VRAM OOM during rollback re-load → HTTP 500, inventory marked dirty,
   pod unhealthy.

Cold-boot:

7. MoE pipe + initial stack with auto entry → `_load_pipeline` raises,
   ready never set.
8. Single-transformer + initial stack with `h` entry → `_load_pipeline`
   raises, ready never set.

---

## 7. Testing strategy

### 7.1 Test pyramid

Inherits `tests/_smoke_harness/` foundation (lora-smoke-pyramid SHIPPED
2026-06-21). Three tiers, each scoped.

**Tier 1 — local CPU, every PR via `pixi run smoke-local`.**

CPU stub via `KINOFORGE_DIFFUSERS_LOAD_STUB`. Stub gains `moe: bool`
fixture knob — when True, stub exposes both `transformer` +
`transformer_2` (no actual weights; routing observable via stub
recording the kwarg or the rebind).

Tests:
- All Section 6.7 unit tests (schema + matcher + routing + rollback +
  cold-boot).
- All Section 4.7 matcher tests.
- Parity test (Section 2.4 extension).
- AC8 AST scan extended: no top-level `pipe.transformer` reads outside
  `_resolve_transformer`.

**Tier 3 — Wan 2.1 1.3B weekly cron, ~$0.20/fire.**

Wan 2.1 is single-transformer. Tier 3 verifies:

- `branch=auto` continues to work (load lands in single transformer).
- Explicit `branch=high_noise` rejected with proper error body
  (regression: P2 doesn't break Wan 2.1 portability when operator
  accidentally tries MoE-shaped cfg).

New file: `tests/smoke/live_wan21/test_branch_routing.py`. Two cases:

1. Cfg with `branch=auto` → generation succeeds; output sha matches
   existing Tier-3 baseline.
2. Cfg with `branch=high_noise` → server returns 400 +
   `branch_unsupported_single_transformer`; pod state untouched.

**Tier 4 — Wan 2.2 14B release-gate, ~$1-2/fire.**

Wan 2.2 is dual-transformer. Tier 4 verifies routing works end-to-end.

New file: `tests/smoke/release_wan22/test_dual_transformer_routing.py`.
Matrix:

| Case | Stack | Expectation |
|---|---|---|
| Baseline (no LoRA) | `[]` | Reference output |
| Arcane high-noise only | `[(arcane_h, 1.0, high_noise)]` | Style diff vs baseline; effect concentrated in early-step features |
| Arcane low-noise only | `[(arcane_l, 1.0, low_noise)]` | Style diff vs baseline; effect concentrated in late-step features |
| Arcane pair (canonical) | `[(arcane_h, 1.0, h), (arcane_l, 1.0, l)]` | Both effects present; sha matches existing Tier-4 baseline |
| Wrong routing | `[(arcane_h, 1.0, l), (arcane_l, 1.0, h)]` | Generation succeeds but output is perceptibly off — known-bad recipe; capture sha as "proof routing matters" |
| MoE + auto reject | `[(arcane_h, 1.0, auto)]` | 400 + `branch_auto_disallowed_on_moe` |
| Same ref in both branches | `[(arcane_h, 1.0, h), (arcane_h, 0.8, l)]` | Loads both (composite key); generation succeeds |

Spend cap: $2 (matches P1's Tier-4 budget). All 7 cases share a warm pod
via `--reuse` to amortize cold-boot.

**Watchdog (existing).** `tools/smoke_leak_sweep.py` already caps Tier-4
at 90 min. P2 stays under.

### 7.2 RED scaffolds first

Both Tier 3 + Tier 4 ship as RED scaffolds (`pytest.xfail("RED scaffold")`)
BEFORE any live spend. Pattern matches P1 Tasks 12 + 14.

### 7.3 test-design skill compliance

For every unit test in the plan:
- State the behavior under test.
- State the concrete bug it would catch.
- Assertions hit observable state (HTTP body, inventory dict, pipeline
  attribute), not internal control flow.
- Mocks at the HTTP boundary; no mocks against `_resolve_transformer`
  itself (mocks would mirror the function-under-test).
- Sleep schedules / activation order asserted with EXACT values.

### 7.4 New conftest fixtures

- `moe_stub_pipe` — `KINOFORGE_DIFFUSERS_LOAD_STUB` target exposing
  `transformer` + `transformer_2` + `load_lora_weights` + `set_adapters`
  recording.
- `single_transformer_stub_pipe` — same shape minus `transformer_2`.
- `branch_recorder` — captures `(adapter_name, target_transformer_attr,
  weight)` triples per `load_lora_weights` call.

### 7.5 Cross-cutting regression

P1's existing tests touching `LoraEntry` / `LoraTarget` / `is_stack_match`
/ `_replace_adapter_stack` all get updated for the new field. NO existing
unit test gets `@pytest.mark.skip` or deleted as P2 cost. If a test breaks
because branch defaults to `"auto"` and the pipeline is now stricter, the
test gets the explicit branch added — same posture as P1's `LoraEntry`
introduction.

### 7.6 Coverage gates

- ruff + mypy clean across touched files.
- pytest coverage ≥ existing baseline on `src/kinoforge/core/lora.py` +
  server file.
- AST scan in `tests/test_no_unredacted_writes.py` extended: branch field
  flagged the same as P1's `LoraInventoryEntry.last_strength`
  (AC8/AC9 invariant).

---

## 8. End-to-end data flow + Task 0 research scope

### 8.1 Data flow — `kinoforge generate` with Wan 2.2 MoE recipe

```
[cfg.loras: [
  {ref: civitai:1234@high, strength: 1.0, branch: high_noise},
  {ref: civitai:5678@low,  strength: 0.8, branch: low_noise},
]]
        ↓
Config.load → LoraEntry × 2 (canonical, branch normalized)
        ↓
resolve_active_lora_stack(cfg, vault) → list[LoraEntry]
        ↓
build_set_stack_request(stack) → SetStackRequest{target: [LoraTarget × 2], download_specs}
        ↓
[orchestrator picks warm pod via capability_key]
        ↓
capability_key derived from (base_model, engine, precision) — branch NOT factored
        ↓
[matcher: is_stack_match(desired_stack, /lora/inventory)]
   ├── exact match (refs + strengths + branches in order) → no swap, run /generate
   └── mismatch → POST /lora/set_stack with request body
        ↓
[server: /lora/set_stack handler]
        ↓
_replace_adapter_stack(target)
   1. pre-load validation: _resolve_transformer per entry
      ├── any raise → atomic reject, HTTP 400, inventory untouched
      └── all pass → continue
   2. snapshot inventory (refs + strengths + branches)
   3. pipe.unload_lora_weights()
   4. per entry: load_lora_weights + per-transformer dispatch
      └── VRAM OOM → rollback to snapshot (Section 6.4)
   5. set_adapters per Section 5.4 strategy
   6. update _inventory[(ref, branch)] with last_strength + adapter_name
        ↓
HTTP 200 + new /lora/inventory state
        ↓
POST /generate with prompt + seed
        ↓
generation pipeline activates loaded adapters per transformer
        ↓
HTTP poll /status/{job_id} → done → GET /artifacts/{filename}
```

### 8.2 Cold-boot data flow (initial stack via env)

```
[orchestrator: kinoforge generate, no warm pod]
        ↓
provision pod with KINOFORGE_INITIAL_LORA_STACK_JSON=[
  {ref, download_spec, strength: 1.0, branch: high_noise},
  ...
]
        ↓
pod boot → _load_pipeline()
   1. _diffusers_load() → pipe
   2. _detect_moe_arity(pipe) → _pipe_arity
   3. validate every initial entry's branch against arity
      └── any invalid → log + exit (ready never set)
   4. _download_one per entry
   5. load_lora_weights per entry with _resolve_transformer dispatch
   6. set_adapters per Section 5.4 strategy
   7. populate _inventory[(ref, branch)]
        ↓
ready.set() → /health 200 → orchestrator's wait_for_ready returns
        ↓
[normal /generate flow]
```

### 8.3 Task 0 research scope (spec mandates Task 0 as plan's first task)

**Question 1: per-transformer LoRA loading.**

Does `diffusers.WanPipeline.load_lora_weights` accept a per-transformer
routing argument?

- Probe: read diffusers source for current `load_lora_weights` signature
  on `WanPipeline` (or its parent loader mixin). Confirm or deny presence
  of kwarg like `load_into_transformer`, `target_module`, `transformer`,
  or equivalent.
- If kwarg exists: write 20-line script that loads a known LoRA into
  `transformer_2` via the kwarg; assert
  `pipe.transformer_2.peft_config[adapter_name]` is non-empty AND
  `pipe.transformer.peft_config.get(adapter_name)` is None or missing.
- If no kwarg exists: confirm absence and write a 20-line script that
  uses Approach 3 rebind to achieve the same outcome; same assertions.

**Question 2: split-transformer activation.**

Does single `pipe.set_adapters([a, b], adapter_weights=[w_a, w_b])`
correctly activate adapters split across `transformer` + `transformer_2`?

- Probe: after loading one adapter into each transformer, call
  `set_adapters` with both names. Inspect
  `transformer.peft_config[a].is_active` (or equivalent active marker)
  AND `transformer_2.peft_config[b].is_active`.
- If both active: single end-of-load `set_adapters` call is correct.
  Section 3.3 stays as-is.
- If only one transformer's adapter active: spec mandates the
  per-transformer activation loop in Section 5.4.

**Question 3: per-transformer eviction.**

Does `pipe.delete_adapters([name])` need per-transformer dispatch?

- Probe: load adapter into `transformer_2`. Call
  `pipe.delete_adapters([name])`. Inspect both transformers' `peft_config`
  for residue.
- If delete cleans both transformers: `_evict_one` stays as Section 5.6.
- If delete only touches `pipe.transformer`: `_evict_one` needs
  per-transformer dispatch (expanded Section 5.6 shape).

**Question 4: peft availability + version compatibility.**

- Probe: `pip show peft` in pod env (confirm `peft` version ≥ minimum
  that supports `set_adapters` per-module).
- Document floor version in spec §1 pre-conditions.

**Task 0 deliverable:** 1-page research note at
`docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md`
summarizing all four questions' answers with concrete code citations
(diffusers source file:line) + probe script outputs. Plan bakes the
right primary/fallback choice into implementation tasks before any code
is written.

**Task 0 budget:** ~30 min wall-clock, $0.00 (no live pod — probes use
Tier 1 stub OR `python -c` against locally-installed diffusers).

### 8.4 Plan-writing follow-ups

Sections 1-8 are spec-level. Plan-writing phase derives:
- 12-16 implementation tasks with file paths + acceptance criteria per
  writing-plans skill convention.
- Task 0 (research) as unconditional first task.
- RED scaffolds (Tier 3 + Tier 4) committed BEFORE any live spend
  (durability rule).
- Per-task pre-commit + ruff + mypy + pytest verify commands.

---

## Appendix A — settled brainstorm decisions

| # | Decision | Pick |
|---|---|---|
| Q1 | `auto` semantics | D — auto on single-transformer; MoE requires h/l |
| Q2 | Schema home for `branch` | LoraEntry (canonical, all consumers via parity lock) |
| Q3 | Vocabulary | Verbose canonical `{high_noise, low_noise, auto}` + short aliases `{h, l}` accepted |
| Q4 | Matcher participation | IN `is_stack_match`, OUT of `capability_key` |
| Q5 | Branch mismatch | Strict reject; `auto` is the explicit-portability value |
| Q6 | Same ref in multiple branches | YES — `(ref, branch)` composite identity |
| Q7 | Routing implementation | Approach 1 primary + Approach 3 fallback + Task 0 selects |
