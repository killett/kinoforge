# Batch and grid

(Moved from README §Batch generation (incl. Streaming output), §kinoforge grid (top-level generate: / path: cells; the lora_swap: cells variant lives in [warm-reuse.md](warm-reuse.md)) on 2026-06-27. See [../README.md](../README.md).)

## Batch generation

Render N clips on one shared deployed instance with continue-on-error
semantics:

```bash
kinoforge batch -c ../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml \
                --manifest ../examples/configs/manifests/batch-prompts.yaml
```

The manifest is a YAML list. Each entry sets exactly one of `prompt`
(inline text) or `prompt_file` (path resolved relative to the manifest's
parent directory). Optional per-entry overrides: `params`, `spec`,
`assets`, `run_id`.

```yaml
# ../examples/configs/manifests/batch-prompts.yaml
- prompt: "waves crashing on basalt cliffs at dusk"
  mode: t2v
  run_id: waves

- prompt_file: ../prompts/forest.txt
  mode: t2v
  run_id: forest
  params: { seed: 42 }
```

**Outputs.** Each entry's artifact lands at
`<store>/<batch_id>/<run_id>/<name>`. Default `batch_id` is
`batch-YYYYMMDD-HHMMSS` in **local timezone**; override with
`--batch-id ID` for a memorable name. A machine-readable summary is
written to `<batch_id>/_batch_summary.json` on every exit path (success,
per-entry failures, batch-fatal abort).

**Concurrency.** `--concurrent N` overrides `cfg.lifecycle.max_in_flight`.
Both layers (outer entry executor and `ConcurrentPool` slot cap) share
the same value. After the run, the CLI prints a per-entry summary table;
intra-run streaming progress is a deferred follow-up — see PROGRESS.md
Phase 22.

**Failure semantics.** Per-entry exceptions become `FAIL` outcomes; the
batch keeps going. Batch-fatal exceptions (`BudgetExceeded`,
`CapabilityMismatch`, `TeardownError`) cancel queued entries and exit
with code 2. The summary JSON is written before the exit in every case.

**Cleanup.** `kinoforge gc --run <batch_id> -c <config>` walks the entire
batch namespace at once.

### Streaming output

`kinoforge batch` emits per-entry progress lines as the run proceeds.
The output shape is controlled by `--stream-format`:

- `--stream-format=human` (default) — operator-readable lines on stdout:

  ```
  [batch-20260605-103000] [1/dawn] START mode=t2v prompt='a sunrise over the cliffs'
  [batch-20260605-103000] [1/dawn] OK 1.5s local://.kinoforge/batch-20260605-103000/dawn/clip.mp4
  ```

  Lines are paired (one `START` per entry, one terminal status per
  entry — `OK` / `FAIL` / `INTERRUPTED` / `ABORTED`). The final summary
  table is printed after the batch completes.

- `--stream-format=jsonl` — one JSON event per stdout line, terminated
  by a `{"kind": "batch_summary", ...}` object. The `manifest loaded`
  and `[instance overview]` headers are routed to stderr so stdout
  stays pure JSONL for piping:

  ```bash
  kinoforge batch --config c.yaml --manifest m.yaml --stream-format=jsonl | jq .
  ```

- `--stream-format=none` — suppress mid-run lines; the final summary
  table is still printed. Matches pre-Layer-L-T4 behaviour for
  operators who prefer the original quieter output.

Library users of `batch_generate()` can plug their own consumer by
passing `on_event=<callable>` directly. The callback receives a
`BatchEvent` (frozen pydantic model defined in
`kinoforge.core.batch_events`); calls are serialized via an internal
`threading.Lock` so multi-line output never interleaves.


## `kinoforge grid` — composed side-by-side mp4 from N generations

> The `lora_swap:` cell variant lives in [warm-reuse.md](warm-reuse.md#lora_swap-grid-cells).

Compose N generations into one side-by-side mp4 with per-cell captions.
Use it for strength sweeps, prompt comparisons, A/B tests across
providers / engines / LoRA stacks.

```bash
pixi run kinoforge grid \
    --spec ~/grids/wan22-arcane-strength.yaml \
    --out output/wan22-strength-grid.mp4
```

**Spec lives OUTSIDE the repo.** `GridSpec.load` raises
`GridSpecUnderRepoError` for any path resolving under the active git
repo root — captions and overrides can carry LoRA refs or prompt
fragments, so accidental commits must be impossible. The
`../examples/grids/` directory contains an illustrative spec marked
NOT-loadable for documentation only.

Each cell declares EITHER `generate:` (orchestrates a real generation
via subprocess `kinoforge generate`) OR `path:` (points at a pre-existing
mp4). Same-`CapabilityKey` cells share a warm pod (matcher reuse); the
last cell of each group passes `--no-reuse` so the pod auto-destroys.
Cross-group cells run in parallel under `--max-parallel-groups` (default 2).

**Exit codes:**

| status | exit | meaning |
|---|---|---|
| `full` | 0 | every cell GREEN + ffmpeg compose succeeded |
| (spec err) | 1 | YAML / pydantic / under-repo path |
| `partial` | 2 | one or more cells failed; per-cell mp4s preserved in `_grid_<id>_partial/` |
| `budget` | 3 | cumulative spend crossed `budget_cap_usd` mid-run |
| `ffmpeg` | 4 | every cell succeeded but final compose subprocess failed |
| `teardown` | 5 | post-condition `kinoforge list` detected a residual pod |

**Override syntax (dotted path).** A cell's `overrides:` map keys are
dotted paths into the base cfg: `loras[0].strength`, `prompt`,
`compute.lifecycle.lora_swap_re_probe_after_s`. Values MUST be scalars
(int/float/str/bool/null); list/dict overrides are rejected — declare a
separate base cfg instead. `[*]` wildcards rejected in v1.

See `../examples/grids/illustrative-strength-sweep.yaml` for a worked
strength-sweep spec.
