# `kinoforge grid` — `lora_swap:` cell variant (server-side warm-attach swap)

**Status:** validated design, ready for plan
**Date:** 2026-06-26
**Author:** brainstorm session 2026-06-26 (transcript-sourced deferred-(2) work from 2026-06-26T00:17Z `5baa5857-a489-4741-820d-6b6843f4d086.jsonl`)
**Supersedes deferred path:** "Option 2" presented at P1 strength-variation gate close-out; "Option 1" shipped as `examples/configs/wan{21-1_3b,22-14b}-strength-grid.yaml` per-cell cold-boot path.

## Motivation

`kinoforge grid` v1 (shipped 2026-06-25) cold-boots a fresh pod per cell. For a 3-cell strength sweep on Wan 2.2 14B that means three 25 min cold-boots at $1.39/hr each — Tier-4 live fire 2026-06-25 cost ~$1.80 for 2-of-3 cells before budget kill. The LoRA-flexible warm-reuse work (P1-P3, 2026-06-20 through 2026-06-25) shipped a server-side `/lora/set_stack` endpoint that lets a single warm Wan 2.2 pod serve many LoRA stacks via swap — but `kinoforge grid` did not learn to drive that endpoint. This spec closes the gap: ONE cold-boot per group, swap stack per cell.

Expected savings: Tier-4 3-cell strength sweep drops from ~$2.10 (3 × 25-min cold-boot + 3 × 3-min gen) to ~$1.05 (1 × 25-min cold-boot + 2 × 30s swaps + 3 × 3-min gen). ~50% pod-time reduction, larger as N grows.

## Non-goals (v1)

- Mixing `path:` and `lora_swap:` cells under a single shared group. Different `capability_key` derivations route them to different groups already; that's enough.
- Cross-grid pod sharing. Each `kinoforge grid` run owns its own pod lifecycle.
- Per-cell `--no-reuse` override inside a swap group. By definition a swap group shares one pod.
- Per-prediction cost capture for hosted Bearer providers (Layer 5 candidate, B10/B13). Sidecar schema reserves the shape; values are not populated for Bearer engines here.
- In-process executor. Subprocess shell-out matches the v1 grid executor pattern; in-process is a future refactor.
- Spec lint warning on identical-stack swap cells. Repeatability testing is a legitimate use; not our job to second-guess.

## Cross-cutting policies

- **Subprocess shell-out (not in-process).** Grid executor invokes `kinoforge` subprocesses (matches existing `_run_one_cell` pattern shipped 2026-06-25). Cell-1 cold-boots via `kinoforge generate --config <c> --loras <heredoc>` (no `--attach-pod`, no `--no-reuse`); cells 2..N attach via `kinoforge generate --config <c> --attach-pod <id> --loras <heredoc>`; group exit destroys via `kinoforge destroy --id <id>`. (Q2 = A.)
- **Cell-1 cold-boots with its own stack via P3 `--loras`.** Cells 2..N take the warm-attach path. (Q3 = revised-B; single new flag covers both.)
- **One new CLI flag total:** `--attach-pod <id>` on existing `generate` verb. No new verbs. (Q6 = A.)
- **Group key = `WarmAttachKey(base, engine, precision)` only.** LoRA stack and strength are OUT of the group key, IN of the per-cell payload. Maximizes pod sharing. (Q5 = A.)
- **Whole-grid failure policy.** `on_swap_failure: strict|continue|classify` lives on `GridSpec` (one knob per grid). Default `classify`. (Q4 = C, Q9 = A.)
- **Whole-group cost accounting + structured sidecar.** Polled `cost_per_hr` × wall-time per group; per-cell attribution emitted to `<grid_out>.cost.json` for operator post-processing. (Q8 = C.)
- **Ledger is source of truth for attach.** `--attach-pod <id>` validates against ledger entry's `status=running` AND `WarmAttachKey` match before skipping provision. No "any pod" escape hatch in v1. (Q7 = A.)

---

## § 1 — Spec surface

### `GridSpec` extension (`src/kinoforge/core/grid/spec.py`)

New optional top-level field:

```python
class GridSpec(BaseModel):
    # ...existing fields...
    on_swap_failure: Literal["strict", "continue", "classify"] = "classify"
```

### New cell variant — `LoraSwapCell`

```python
class LoraStackEntry(BaseModel):
    """One LoRA reference in a swap cell's stack.

    Schema mirrors the P3 CLI heredoc shape so the executor can serialize
    a `LoraStackEntry` list directly to a `--loras` heredoc payload.
    """
    model_config = ConfigDict(extra="forbid")

    ref: str  # civitai:<modelId>@<versionId> | hf:Org/Repo[:filename]
    strength: float = Field(default=1.0, ge=-1.0, le=2.0)
    branch: Literal["high", "low", "auto"] = "auto"


class LoraSwapCell(BaseModel):
    """Cell that drives a warm-attach LoRA-stack swap on a shared pod.

    Cells with the same `WarmAttachKey(base, engine, precision)` (derived
    from `config`) pack into one group; group cold-boots one pod for
    cell-1 and attaches via `--attach-pod` for cells 2..N. Each cell's
    `stack` flows through the existing P3 `--loras` CLI surface and
    routes to `POST /lora/set_stack` on the warm pod.
    """
    model_config = ConfigDict(extra="forbid")

    config: Path                                   # base cfg; provides base/engine/precision
    stack: list[LoraStackEntry] = Field(min_length=0)  # empty stack legal (P3 D9)
```

### `GridCell` variant mutex extension

```python
class GridCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generate: GenerateCell | None = None
    path: Path | None = None
    lora_swap: LoraSwapCell | None = None   # NEW
    caption: str | None = None

    @model_validator(mode="after")
    def _check_variant(self) -> GridCell:
        variants = [self.generate, self.path, self.lora_swap]
        n_set = sum(v is not None for v in variants)
        if n_set != 1:
            raise ValueError(
                "cell must declare exactly one of `generate:` / `path:` / "
                "`lora_swap:`"
            )
        return self
```

### YAML shape — Tier-4 strength sweep example

```yaml
title: "Arcane strength sweep — Wan 2.2 14B"
budget_cap_usd: 5.00
on_swap_failure: classify          # default; omit-OK
layout: "1x3"
cells:
  - lora_swap:
      config: ~/grids/wan22-base.yaml
      stack:
        - ref: "civitai:1709622@1935544"
          strength: 0.5
          branch: high
        - ref: "civitai:1709622@1935551"
          strength: 0.5
          branch: low
    caption: "strength=0.5"
  - lora_swap:
      config: ~/grids/wan22-base.yaml
      stack:
        - ref: "civitai:1709622@1935544"
          strength: 1.0
          branch: high
        - ref: "civitai:1709622@1935551"
          strength: 1.0
          branch: low
    caption: "strength=1.0"
  - lora_swap:
      config: ~/grids/wan22-base.yaml
      stack:
        - ref: "civitai:1709622@1935544"
          strength: 1.5
          branch: high
        - ref: "civitai:1709622@1935551"
          strength: 1.5
          branch: low
    caption: "strength=1.5"
```

### Redaction

`_register_caption_tokens` (spec.py:201) extended to also register every `LoraSwapCell.stack[*].ref` with `RedactionRegistry` (kind `grid:lora_ref`). Refs appear redacted in logs / `successful-generations.md` while still rendering plain into the output mp4 (output dir is the universal exempt zone per Layer 5b ephemeral-workspaces design).

---

## § 2 — Grouping

`_cell_capability_key()` in `src/kinoforge/core/grid/executor.py:98` extended:

| Cell type      | Key shape                                                     |
| -------------- | ------------------------------------------------------------- |
| `generate:`    | `CapabilityKey.derive(cfg).key_str` (full key; includes LoRAs)|
| `path:`        | `None` → `_PATH_GROUP_KEY` sentinel                           |
| `lora_swap:`   | `WarmAttachKey.derive(cfg).key_str` (base + engine + precision; LoRAs OUT) |

All `lora_swap:` cells sharing the same `WarmAttachKey` pack into one group. `generate:` cells with the same `(base, engine, precision)` go to a DIFFERENT group (their key includes LoRAs, so it never collides with a `WarmAttachKey`-derived key — by construction of the two key types in P1).

Group execution preserves spec order (insertion-preserved `dict` in `grouping.py:42`).

---

## § 3 — CLI surface

### New flag 1: `--attach-pod <id>` on existing `generate`

Argparse addition in `src/kinoforge/cli/_main.py` (`p_generate`):

```python
p_generate.add_argument(
    "--attach-pod",
    type=str,
    default=None,
    metavar="POD_ID",
    help="attach to an existing warm pod from the ledger; skip provision; "
         "pod survives at end (mutually exclusive with --no-reuse). "
         "Pod must be ledger status=running AND match cfg's WarmAttachKey.",
)
```

### New flag 2: `--emit-provision-record <path>` on existing `generate`

```python
p_generate.add_argument(
    "--emit-provision-record",
    type=Path,
    default=None,
    metavar="PATH",
    help="on successful provision, write a JSON record "
         "{pod_id, endpoint_url, provider, warm_attach_key, provision_ts} "
         "to PATH. Used by `kinoforge grid` swap-mode and by operator "
         "scripting. Not written on provision failure.",
)
```

Mutually exclusive with `--attach-pod` (attach doesn't provision; nothing to record).

### Validation (in `cmd_generate` before orchestrator call)

| Condition                                           | Behavior                                                                                  |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `--attach-pod` + `--no-reuse`                       | Exit 1: `--attach-pod and --no-reuse are mutually exclusive: --attach-pod implies pod survival` |
| `--attach-pod` + `--emit-provision-record`          | Exit 1: `--attach-pod and --emit-provision-record are mutually exclusive: attach does not provision` |
| `--attach-pod <id>`, pod missing from ledger        | Exit 1: `pod <id> not in ledger; cannot attach (ledger path: <path>)`                     |
| `--attach-pod <id>`, ledger `status != running`     | Exit 1: `pod <id> has ledger status=<X>; --attach-pod requires running`                   |
| `--attach-pod <id>`, ledger `WarmAttachKey` ≠ cfg's | Exit 1: `pod <id> is base=X engine=Y precision=Z but cfg requires base=A engine=B precision=C` |

### Orchestrator integration

When `--attach-pod` passes validation:
- `deploy_session` (or the equivalent batch path) skips `engine.provision`
- Engine's HTTP backend is constructed pointed at the ledger entry's recorded `endpoint_url`
- `--loras <heredoc>` flows through existing `resolve_active_lora_stack(*, cli_loras=)` → `set_stack` payload → POST to the warm pod (existing P3 path)
- Generation proceeds as today
- At end: skip destroy; pod survives; ledger entry updated only via `Ledger.touch` (last-seen timestamp; existing behavior)

Net new CLI surface: one flag. No new verbs.

---

## § 4 — Executor

### New async function `_run_swap_group`

Lives in `src/kinoforge/core/grid/executor.py` alongside existing `_run_group`. Signature mirrors:

```python
async def _run_swap_group(
    group_cells: list[_ResolvedCell],          # all LoraSwapCell, same WarmAttachKey
    *,
    on_swap_failure: Literal["strict", "continue", "classify"],
    output_dir: Path,
    grid_id: str,
    budget_cap_usd: float,
    budget_state: _BudgetState,                # shared mutable; abort on cap-trip
    timeout_per_cell_s: float,
) -> list[GridCellResult]:
    """Cold-boot one pod for cell-1, attach for cells 2..N, destroy on exit."""
```

### Per-cell flow

1. **Cell-1** (`i == 0`): subprocess
   ```
   kinoforge generate
     --config <cell.config>
     --loras <heredoc-from-cell.stack>
     --output-dir <per-cell-dir>
     --emit-provision-record <per-cell-dir>/.provision.json
   ```
   (No `--attach-pod`, no `--no-reuse`.) New flag `--emit-provision-record <path>` writes a machine-readable record on successful provision:
   ```json
   {"pod_id": "<runpod-id>", "endpoint_url": "https://...", "provider": "runpod",
    "warm_attach_key": "<base>__<engine>__<precision>", "provision_ts": "2026-06-26T15:23:30Z"}
   ```
   Grid executor reads this file after subprocess exit to obtain `pod_id` for cells 2..N. The flag is independently useful for any operator scripting the CLI (not grid-specific). On provision failure: file is NOT written; subprocess exits non-zero; grid executor classifies as `ABORT` (no pod to swap on).

2. **Cells 2..N**: subprocess
   ```
   kinoforge generate
     --config <cell.config>
     --attach-pod <pod_id>
     --loras <heredoc-from-cell.stack>
     --output-dir <per-cell-dir>
   ```

3. **After each cell**: post-condition that `output_dir/*.mp4` exists; sha256 it; record `GridCellResult(idx, mp4_path, sha256, wall_time_s, swap_wall_time_s)`.

4. **After each cell**: budget cap check (see § 5).

5. **On subprocess failure**: invoke `_classify_swap_failure(stderr, exit_code, on_swap_failure)` → returns `SwapFailureAction.RETRY | CONTINUE | ABORT`.

6. **Group exit (success OR abort)**: `try/finally` calls
   ```
   kinoforge destroy --id <pod_id>
   ```
   followed by post-run `kinoforge list` probe (extends destroy-on-teardown protection from `_check_no_residual_pods`). On residual-pod detection, surface as `GridResult.status = "teardown"` (exit code 5).

### `run_grid` dispatch

For each group key, switch on cell type:
- `_PATH_GROUP_KEY` → existing path-cell handler (unchanged)
- All cells are `GenerateCell` → existing `_run_group` (unchanged)
- All cells are `LoraSwapCell` → new `_run_swap_group`
- Mixed `GenerateCell` + `LoraSwapCell` in one group is structurally impossible (their `capability_key` derivations produce disjoint key spaces by P1 construction)

Parallelism: same `--max-parallel-groups` semaphore. Swap groups run in parallel with each other, sequentially within.

### `--dry-run` interaction

`kinoforge grid --dry-run` prints the planned subprocess invocations + group structure + estimated wall time + estimated cost (using `cost_per_hr` from cfg's provider rate-card if available). No live spend.

### `--ephemeral` interaction

`--ephemeral` propagates to the per-cell subprocess invocations as a parent-process env flag (`KINOFORGE_EPHEMERAL=1`). Same semantics as today: do NOT amend `successful-generations.md`; do NOT persist artifacts to the ledger long-term. Sidecar `.cost.json` IS still written (it lives next to the output mp4 which itself is ephemeral when the flag is set).

---

## § 5 — Failure taxonomy

### New module `src/kinoforge/core/grid/swap_failures.py`

```python
class SwapFailureAction(Enum):
    RETRY = "retry"        # transient — retry within cell, max 3× at 5s backoff
    CONTINUE = "continue"  # recoverable — cell fails, pod state known-good, next cell proceeds
    ABORT = "abort"        # unrecoverable — destroy pod, fail remaining cells in group

def _classify_swap_failure(
    stderr: str,
    exit_code: int,
    policy: Literal["strict", "continue", "classify"],
) -> SwapFailureAction:
    if policy == "strict":
        return SwapFailureAction.ABORT
    if _is_unrecoverable(stderr):
        return SwapFailureAction.ABORT  # always abort on truly unrecoverable
    if policy == "continue":
        return SwapFailureAction.CONTINUE
    # classify (default):
    if _is_transient(stderr):
        return SwapFailureAction.RETRY
    if _is_recoverable(stderr):
        return SwapFailureAction.CONTINUE
    return SwapFailureAction.ABORT
```

### Classification table

| Pattern in stderr / exit code                                          | `classify` action | Reason                                                |
| ---------------------------------------------------------------------- | ----------------- | ----------------------------------------------------- |
| HTTP 502, `ProxyWarmupTimeout`, `ConnectionError`                      | `RETRY`           | Transient; harness pattern recovers per `wan_server_set_stack_proxy_warmup` |
| `SwapRejectedDetails` (P2 structured 4xx)                              | `CONTINUE`        | Pod state known-good; server rejected before mutating |
| `BranchUnsupportedOnSingleTransformer`, `BranchAutoNotAllowedOnMoE`    | `CONTINUE`        | Recoverable contract violation                        |
| `BranchUnknown`                                                        | `CONTINUE`        | Operator typo in `branch:` field                      |
| `VRAMRollbackFailure`                                                  | `ABORT`           | Pod state corrupted                                   |
| `RunPodGraphQLError`                                                   | `ABORT`           | Provider failure; pod likely gone                     |
| HTTP 5xx after `RETRY` budget exhausted                                | `ABORT`           | Server-side cascading failure                         |
| Subprocess exit code 137 (OOM kill)                                    | `ABORT`           | Pod under-resourced                                   |
| Any other non-zero exit code                                           | `ABORT`           | Unknown; fail safe                                    |

### Retry budget

`RETRY` actions: 3 attempts at 5s backoff (matches `tests/_smoke_harness/` pattern). After 3rd `RETRY`, re-classifies to `ABORT`.

### Policy override semantics

- `strict`: ABORT on any non-zero exit. Operator opt-in for money-sensitive runs.
- `continue`: Recoverable AND ambiguous errors continue; only truly-unrecoverable (`VRAMRollbackFailure`, `RunPodGraphQLError`) abort. Useful for "I want as many cells as possible, even if some fail weirdly."
- `classify`: default; uses the table above.

---

## § 6 — Cost sidecar

### Polling

Grid executor polls `provider.status_instance(pod_id).cost_per_hr` every 30s during group run (existing `cost_rate_usd_per_hr` field per the 2026-06-20 accuracy fix). Group's accrued cost computed as:

```
group_cost_usd = (now - group_provision_ts) * cost_per_hr_usd / 3600
```

Cap check happens at every poll AND between cells. Mid-cell trip: let in-flight cell finish (no kill mid-generation); subsequent cells in the group marked `budget_killed`.

### Sidecar file path

`<grid_out>.cost.json` (sibling of output mp4 in the grid's `--out` path).

### Sidecar schema

```json
{
  "grid_id": "grid-20260626-152330-abc",
  "spec_path": "<redacted-via-RedactionRegistry>",
  "out_mp4": "output/sweep-arcane-strength.mp4",
  "total_cost_usd": 1.42,
  "budget_cap_usd": 5.00,
  "wall_time_s": 1834.5,
  "groups": [
    {
      "key": "wan22-a14b__diffusers__bf16",
      "pod_id": "<runpod-id>",
      "provider": "runpod",
      "cost_per_hr_usd": 1.39,
      "wall_time_s": 1820.1,
      "cost_usd": 0.703,
      "cell_indices": [0, 1, 2],
      "cells": [
        {
          "idx": 0,
          "gen_wall_time_s": 198.2,
          "swap_wall_time_s": 0.0,
          "status": "ok",
          "mp4_sha256": "4d7b1e6f03825baa...",
          "size_bytes": 884736
        },
        {
          "idx": 1,
          "gen_wall_time_s": 192.5,
          "swap_wall_time_s": 28.7,
          "status": "ok",
          "mp4_sha256": "96f954a494bcf7ef...",
          "size_bytes": 1234567
        },
        {
          "idx": 2,
          "gen_wall_time_s": 0.0,
          "swap_wall_time_s": 4.1,
          "status": "budget_killed",
          "error": "budget cap $5.00 reached at cell 2 start"
        }
      ]
    }
  ]
}
```

### Behavior

- Sidecar always written, even on partial / aborted runs.
- Sets the file-shape convention for Layer 5 Bearer per-prediction cost work (B10 / B13 from PROGRESS.md "Known limitations & follow-ups").
- Exit code 3 on budget cap trip (existing mapping).

---

## § 7 — Testing surface

### Unit tests (`tests/core/grid/`)

| File                              | Approx tests | Coverage                                                                 |
| --------------------------------- | ------------ | ------------------------------------------------------------------------ |
| `test_spec_lora_swap.py`          | ~6           | 3-way variant mutex, `LoraSwapCell` / `LoraStackEntry` validators, empty-stack legal, ref redaction registered, `on_swap_failure` defaults + literals |
| `test_grouping_lora_swap.py`      | ~4           | Same `WarmAttachKey` packs into one group; different precision splits; mixed `generate:` + `lora_swap:` go to different groups; `path:` cells still land in `_PATH_GROUP_KEY` |
| `test_swap_failures.py`           | ~9           | Each `_classify_swap_failure` branch — transient (502, proxy_warmup, ConnectionError), recoverable (`SwapRejectedDetails`, `BranchUnsupportedOnSingleTransformer`, `BranchAutoNotAllowedOnMoE`), unrecoverable (`VRAMRollbackFailure`, `RunPodGraphQLError`, 500 after retry), strict-mode override, continue-mode override |
| `test_executor_swap_group.py`     | ~6           | Cell-1 subprocess shape (no `--attach-pod`, `--loras` present), cells 2..N include `--attach-pod`, group teardown in finally on success, teardown on abort, post-condition `kinoforge list` probe fires, budget cap-trip aborts group with code 3 |

### CLI tests (`tests/cli/`)

| File                              | Approx tests | Coverage                                                                 |
| --------------------------------- | ------------ | ------------------------------------------------------------------------ |
| `test_generate_attach_pod.py`     | ~8           | `--attach-pod` + `--no-reuse` → exit 1; `--attach-pod` + `--emit-provision-record` → exit 1; ledger-missing pod → exit 1; ledger status ≠ running → exit 1; WarmAttachKey mismatch → exit 1; happy-path attach skips provision; `--loras` flows through `resolve_active_lora_stack`; `--emit-provision-record` writes well-formed JSON on happy-path provision; not written on provision failure |

### Integration tests (`tests/integration/grid/`)

| File                                       | Approx tests | Coverage                                                                 |
| ------------------------------------------ | ------------ | ------------------------------------------------------------------------ |
| `test_lora_swap_executor_integration.py`   | ~2           | End-to-end 3-cell swap group against stubbed kinoforge subprocesses + stubbed `/lora/set_stack` + stubbed mp4 download; per-cell shas distinct; sidecar JSON shape matches schema |

### AST scan extension (`tests/test_no_unredacted_writes.py`)

- New AC10: every code path writing `LoraSwapCell.stack[*].ref` to a file/log must go through `RedactionRegistry.redact()`. Mirrors existing AC8/AC9 for `InventorySnapshot` + `Ledger.touch`.

### Shared smoke harness (`tests/_smoke_harness/`)

- New helper `write_lora_swap_grid_spec(*, tier: Literal["tier3", "tier4"], strengths: list[float], out_path: Path) -> None` covering Tier-3 single-LoRA-swap (Wan 2.1 1.3B Pokemon + static-rotation) and Tier-4 MoE-pair-swap (Wan 2.2 14B Arcane high+low) shapes.

### Net test count

~33 new tests across 5 files + 1 new AST AC + 1 new harness helper.

---

## § 8 — Live-fire plan

### Tier-3 (Wan 2.1 1.3B, ~$0.20-0.40 RunPod)

1. **Happy-path 3-cell sweep.** Strength {0.5, 1.0, 1.5} on Pokemon + static-rotation pair. Single pod, 3 generations. Expected: ~6 min wall (1 cold-boot + 2 swaps × ~30s + 3 × ~90s gens). Cost target ≤ $0.30. Sha-distinct mp4s. Composed grid + sidecar.json.
2. **Forced recoverable failure.** Pass deliberately invalid `branch: bogus` on cell-2. Verify `classify` action = `CONTINUE` → cell-2 fails, cell-3 proceeds, group exits partial (exit code 2), sidecar records cell-2's `error` field.

### Tier-4 (Wan 2.2 14B MoE, ~$1-1.5 RunPod)

1. **Happy-path 3-cell sweep.** Strength {0.5, 1.0, 1.5} on Arcane high+low pair. Expected: ~35 min wall (1 × 25-min cold-boot + 2 × ~30s swaps + 3 × ~3-min gens). Cost target ≤ $1.50.
2. Composed grid mp4 + sidecar.json + amend `successful-generations.md §11` (new "See also" line under `kinoforge grid` capability axis from current Task 18 stub).

### Total live-fire budget

~$2 cumulative across both tiers. Well under $20 session cap. Comparable to Option-1's $1.80 for an INCOMPLETE 2-cell Tier-4 — Option-2 delivers 3 cells for similar money.

### Operator-perceptual eval

Same deferred outcome as the current Tier-4 grid eval: does strength variation look correct on the composed mp4? Now WITH the cost saving + the full 3-cell sweep instead of the budget-killed 2-cell partial.

---

## Acceptance criteria

- AC1: `lora_swap:` cells with the same `WarmAttachKey` pack into one group; group cold-boots one pod, swaps stack for cells 2..N, destroys at group exit.
- AC2: Two new CLI flags on `generate` (`--attach-pod <id>`, `--emit-provision-record <path>`); no new verbs. Mutually exclusive with each other.
- AC3: `--attach-pod` validates against ledger (status, kind); rejects mismatch with structured error + exit 1.
- AC4: `--attach-pod` + `--no-reuse` rejected at argparse-validation time with structured error.
- AC5: `LoraSwapCell.stack[*].ref` registered with `RedactionRegistry`; AC10 AST scan locks the invariant.
- AC6: `on_swap_failure: strict|continue|classify` (default `classify`) drives `_classify_swap_failure`; transient retries 3× at 5s; recoverable continues group; unrecoverable aborts group + destroys pod.
- AC7: `<grid_out>.cost.json` sidecar always written with per-group + per-cell schema as defined in § 6.
- AC8: Budget cap trip during a swap group destroys pod + marks remaining cells `budget_killed` + exits with code 3.
- AC9: Group teardown runs in `try/finally`; post-run `kinoforge list` probe surfaces residual pods as `status="teardown"` (exit code 5) extending the destroy-on-teardown protection.
- AC10: `--dry-run` prints the planned subprocess invocations + group structure + estimated cost; no live spend.
- AC11: `--ephemeral` propagates to subprocesses via `KINOFORGE_EPHEMERAL=1` env; same semantics as today's grid behavior.
- AC-Live-T3: Tier-3 happy-path 3-cell sweep green at ≤ $0.30 with sha-distinct mp4s.
- AC-Live-T3-fail: Tier-3 forced-failure run produces partial result + recoverable cell logged with structured error.
- AC-Live-T4: Tier-4 happy-path 3-cell sweep green at ≤ $1.50 with sha-distinct mp4s + amended `successful-generations.md §11`.

## Out of scope (explicit)

See "Non-goals (v1)" above.

## Open questions

None at design time. Plan-time questions deferred to the implementation plan writer.
