# `kinoforge grid` — design spec

**Date:** 2026-06-25
**Author:** brainstorm session (Dr. Twinklebrane + Claude)
**Issue:** none yet (recommend opening one before plan phase)
**Status:** validated, awaiting user spec review before plan phase

This spec introduces a reusable `kinoforge grid` CLI verb that composes N
generation outputs into one side-by-side mp4 with per-cell captions, then
applies it to close P1 (server per-LoRA strength weights) by greening the
two outstanding RED smoke scaffolds via the new command.

---

## 1. Motivation

P1 (server per-LoRA strength weights) shipped code-complete on
2026-06-21 and merged to `main` (commits `5eec358..639fd0f`). Two
deferred tasks remain:

- **Task 13** — Tier-3 live smoke: Wan 2.1 1.3B strength variation
  (`tests/smoke/live_wan21/test_lora_strength_variation.py`,
  RED-scaffolded, $0.30 budget).
- **Task 15** — Tier-4 release-gate live smoke: Wan 2.2 14B MoE pair
  strength variation
  (`tests/smoke/release_wan22/test_lora_strength_variation.py`,
  RED-scaffolded, $1.50 budget).

Both scaffolds declare a perceptual success criterion: generate the
same `(prompt, seed, LoRA-stack)` tuple at strength values
`{0.5, 1.0, 1.5}` and assert the outputs are pairwise distinct AND
visually different. SHA-distinctness is necessary-but-not-sufficient;
the load-bearing check is human visual diff.

Closing P1 requires a way to **view N generations side-by-side with
labelled captions** so the visual diff is one-click. A one-off
testing harness for this would violate the "real kinoforge command
that users can use" principle the user named explicitly. This spec
therefore designs a generic grid composer first, then wires the two
P1 smokes through it.

The user's stated additional use cases for grid:

- Same LoRA × {strength values} (P1's case).
- {LoRA stacks} ∪ {no-LoRA reference} on the same model.
- {Different prompts} on the same model, no LoRAs.
- Same prompt × {Wan 2.1 vs Wan 2.2} (cross-model).

The command must serve all four with one syntax.

---

## 2. Scope

### In scope

- New CLI verb `kinoforge grid --spec <path> [--out <path>]`.
- Grid spec YAML schema living **outside the repo** (vault-style
  guard via new `GridSpecUnderRepoError`), `RedactionRegistry`-aware
  on load.
- Hybrid cell types: `generate:` (orchestrated) or `path:`
  (pre-existing mp4).
- Option A dotted-path overrides with `.field` and `[N]` indexing;
  no `[*]` wildcard in v1.
- Smart `capability_key` grouping: warm-reuse intra-group, groups
  run in parallel (cap `--max-parallel-groups`, default 2).
- Save-partial failure semantics + distinct non-zero exit codes.
- Single grid mp4 output per run (no png strip, no manifest sidecar
  in v1).
- ffmpeg subprocess shell-out (no Python ffmpeg bindings).
- `_escape_drawtext()` helper with its own unit tests.
- New `tests/_smoke_harness/grid.py` shared helper for Tier-3 +
  Tier-4 strength smokes.
- One `successful-generations.md` § entry per grid run, schema-
  extended with `grid:` block listing per-cell shas.
- Tier-3 + Tier-4 smoke RED → GREEN.
- `PROGRESS.md` close-out for P1 (also corrects stale "13 commits
  ahead, NOT merged" line).

### Out of scope (this spec)

- `kinoforge sweep` Option-D matrix-helper command.
- Hosted-Bearer engines in grid cells (Replicate / Runway / Luma).
- Audio mixing across cells (kinoforge mp4s are silent).
- Animated transitions / fades between cells.
- Multi-segment vault composition in grid spec.
- `[*]` wildcard in dotted-path overrides.
- Override values that are lists or dicts (scalars only in v1; use
  separate base cfg for whole-subtree swaps).
- `--ephemeral` interaction beyond pass-through (full ephemeral
  audit of grid path deferred).
- Cross-spec composition (one grid spec referencing another).

---

## 3. Grid spec schema

Outside-repo YAML, loaded once at `kinoforge grid` entry. Top-level
fields:

```yaml
# /home/<user>/.kinoforge/grids/arcane-strength.yaml
title: 'Arcane LoRA strength sweep'    # optional banner above grid
layout: '1x3'                          # 'RxC' explicit | 'auto' (sqrt+ceil)
budget_cap_usd: 1.50                   # REQUIRED — no default
caption_style:                         # optional, defaults below
  position: top-center                 # top-center | bottom-center | top-left | none
  font_size_pct: 5                     # % of cell height
  bg_alpha: 0.5                        # 0.0..1.0 opaque-black bg
cells:                                 # ordered, fills layout left→right, top→bottom
  - generate:
      config: examples/configs/wan22-14b-arcane.yaml   # repo-relative
      overrides:
        loras[0].strength: 0.5
        loras[1].strength: 0.5
    caption: 'strength=0.5'
  - generate: { config: ..., overrides: { ... } }
    caption: 'strength=1.0'
  - generate: { config: ..., overrides: { ... } }
    caption: 'strength=1.5'
```

### 3.1 Cell variants (mutually exclusive per cell)

- `generate: { config: <repo-relative-path>, overrides: { <dotted-key>: <scalar>, ... } }`
- `path: <absolute-or-repo-relative-mp4-path>`

Each cell also has optional `caption: <str>`. Omit → no caption.

### 3.2 Override semantics

- Key is dotted path: `.field` walks objects, `[N]` walks lists by
  0-indexed position. Chain freely: `loras[0].strength`,
  `compute.lifecycle.lora_swap_re_probe_after_s`.
- Value is a YAML scalar (int, float, str, bool, null).
- **Lists and dicts as override values are rejected** with a clear
  error pointing the user at the `path:` escape hatch or a separate
  base cfg.
- Override applied to in-memory cfg object, then **full pydantic
  re-validation**. Typo'd paths and invalid values fail loud.

### 3.3 Pydantic surface

New models in `src/kinoforge/core/grid/spec.py`:

- `GridSpec` — top-level spec model; `model_config = ConfigDict(extra="forbid")`.
- `GenerateCell` — `{ config: Path, overrides: dict[str, ScalarValue] }`.
- `PathCell` — `{ path: Path }`.
- `GridCell` — discriminated union of `GenerateCell` and `PathCell`
  (one of `generate:` or `path:` required, both forbidden).
- `CaptionStyle` — defaults from §3 if omitted.

### 3.4 Vault interaction

- `Vault.load(...)` already runs at CLI entry. Grid spec MAY contain
  raw LoRA names or prompt strings in `caption:` values.
- On spec load: walk every string field (title, captions), register
  any value matching a known vault token with `RedactionRegistry`
  (no-op for fresh strings; the vault is the sole authoritative
  source of secrets per the 2026-06-08 ephemeral spec).
- Captions render into the final mp4 (output dir = exempt zone).
- The SAME caption string appearing in any log line gets redacted to
  the existing `<lora:abc123>` placeholder shape. Operator viewing
  the mp4 sees plaintext; operator reading the log sees redacted.

### 3.5 Path guard

New exception `GridSpecUnderRepoError` mirrors `VaultUnderRepoError`.
Refuses any `--spec` path under repo root at load. The
`examples/grids/` dir in the repo holds **illustrative only** spec
samples that reference vault aliases (not raw refs); these never
load via `kinoforge grid` — they're docs.

---

## 4. Execution model

### 4.1 Cell resolution pass (synchronous, no compute)

1. Load + validate spec; raise on any error.
2. For each `generate:` cell, apply dotted-path overrides to the
   base cfg, run full pydantic re-validation. Cell now carries a
   fully resolved cfg object.
3. For each `path:` cell, `os.stat` the file; raise
   `GridCellPathMissing(idx, path)` if absent.
4. Compute `CapabilityKey.derive()` for each `generate:` cell.
   Strength and prompt are branch-invariant per P1 + warm-reuse
   spec, so a strength sweep across one base cfg collapses to ONE
   capability key.
5. Group cells by capability key. `path:` cells form a degenerate
   group (no compute, no provisioning).

### 4.2 Per-group asyncio worker

- One task per group; concurrent groups capped at
  `--max-parallel-groups` (default 2).
- Worker provisions ONE pod for the group via the same surface
  `kinoforge generate` uses today, runs cells sequentially through
  that pod via the existing warm-reuse `set_stack` path, destroys
  the pod when the group completes (success OR partial failure)
  via the `teardown_pod_or_raise` helper from the 2026-06-24
  destroy-on-teardown fix.
- Each completed cell writes its mp4 to a temp dir IMMEDIATELY at
  `<output_dir>/_grid_<grid_id>/cell_<idx>.mp4`. Surviving partial
  failures means already-paid-for compute never gets discarded.
- Each cell's mp4 path + sha256 captured in an in-memory
  `GridCellResult` list keyed by cell index.

### 4.3 Failure semantics

- Cell raises → catch, record
  `GridCellFailure(idx, cfg_repr, exception_chain)`, do NOT abort
  siblings in the same group, do NOT abort other groups.
- Group worker keeps running until all its cells are attempted.
- Pod for group destroyed unconditionally on group exit via
  `teardown_pod_or_raise`. If teardown itself raises, propagate as
  the dominant exit reason (the destroy-on-teardown fix's
  post-condition probe must not be silently swallowed).

### 4.4 Composition pass (after all groups settle)

- IF any cell failed → SKIP composition. Print summary (cell idx |
  caption | mp4 path | status). Move temp mp4s to
  `<output_dir>/_grid_<grid_id>_partial/` with rename
  `cell_<idx>_<caption-slug>.mp4`. Exit code 2.
- IF all cells succeeded → compose grid mp4, save to `--out` path
  (default `<output_dir>/grid_{ts}_{title-slug}.mp4`, local TZ per
  CLAUDE.md memory), delete temp dir, exit 0.

### 4.5 Budget accounting

- Existing `BudgetTracker` wraps the N generate calls under ONE
  scope rooted at the grid run. Sum ceiling pulled from spec
  `budget_cap_usd:` (required, no default — forces author to think
  about spend).
- Exceeding the cap mid-grid → abort in-flight cells, destroy pods
  via `teardown_pod_or_raise`, save partial mp4s under
  `_grid_<grid_id>_partial/`, exit code 3 with breadcrumb.

### 4.6 `successful-generations.md` integration

- ONE § entry per fully-successful grid run.
- Schema-extended `grid:` block:
  - `spec_path` (redacted via `RedactionRegistry`)
  - `composed_mp4_sha`
  - `cells:` list of `{idx, caption, cfg_path, overrides, cell_mp4_sha, cost_usd}`
- `kinoforge grid` is itself a NEW capability axis per the
  CLAUDE.md durability rule ("new kinoforge command, etc."). The
  FIRST grid run lands a brand-new section regardless of the
  underlying `(provider, engine, model, mode)` tuple of its cells.
  SUBSEQUENT grid runs on the SAME underlying tuple get "See also"
  lines under the existing GRID section (not under the legacy
  tuple-section, to keep the grid axis discoverable in its own
  TOC entry).

---

## 5. Composition (ffmpeg layer)

### 5.1 Dependency

`ffmpeg` binary on PATH. Pre-flight at `kinoforge grid` entry;
missing → loud error with install hint
(`apt-get install ffmpeg` / `brew install ffmpeg` /
`pixi add ffmpeg` if the repo doesn't already have it).

**No Python ffmpeg bindings.** All invocation via
`subprocess.run([...])` with arg-list (no `shell=True`). Matches
kinoforge's existing pattern (zero current ffmpeg dependency).

### 5.2 Layout resolver

- `layout: 'RxC'` explicit → use as-is. Validate `R*C >= len(cells)`;
  error if mismatch.
- `layout: 'auto'` (default) → `C = ceil(sqrt(N))`,
  `R = ceil(N / C)`. Examples:
  - N=3 → 2x2 with 1 empty slot
  - N=4 → 2x2
  - N=6 → 3x2
  - N=9 → 3x3
- Empty slots in the last row render as black frames matching cell
  dimensions.

### 5.3 Dimension / fps / duration normalization

- Probe each input via
  `ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate,duration -of json`.
- **Target cell dims** = `(min(width), min(height))` across inputs.
  Preserves aspect; scales bigger inputs down rather than upscaling
  smaller ones.
- **Target fps** = `max(fps)` across inputs. Lower-fps inputs
  resample up via `fps=` filter; higher fps never down-sampled (avoids
  visible jitter on shorter outputs).
- **Target duration per cell** = `max(duration)` across inputs.
  Shorter inputs padded with last-frame freeze via
  `tpad=stop_mode=clone:stop_duration=...`.
- Each input filter chain:
  `[N:v] scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=F,tpad=stop_mode=clone:stop_duration=...[vN]`.

### 5.4 Caption rendering

ffmpeg `drawtext` filter per captioned cell, applied to the
SCALED+NORMALIZED stream BEFORE xstack.

Defaults (overridable via spec `caption_style:`):

- Font: `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`
  (DejaVu Sans Bold — present in conda + standard linux distros;
  ttf path validated at startup, fallback to `monospace` fontconfig
  name).
- Size: `5%` of cell height.
- Color: `white`.
- Background: semi-transparent black box
  (`box=1:boxcolor=black@0.5:boxborderw=8`).
- Position: top-center (`x=(w-text_w)/2:y=20`).

**`drawtext` arg-quoting:** percent-encode user-supplied caption text
via `_escape_drawtext()` helper. Special chars: `'`, `:`, `\`, `%`.
Helper has its own unit tests in `test_grid_compose.py` (drawtext
silent mis-parses on un-escaped `:` are easy to ship by accident).

### 5.5 xstack composition

For RxC grid:

```
xstack=inputs=N:layout=0_0|w0_0|0_h0|w0_h0:fill=black
```

(layout string composed programmatically from R, C, per-cell W, H).

Output codec:
`-c:v libx264 -pix_fmt yuv420p -preset medium -crf 18` (high-quality
default; not user-tunable in v1).

No audio: `-an`.

### 5.6 Title banner (optional)

IF `title:` set → render via
`color=c=black:s=GxH:d=D` + `drawtext` → `vstack` above the grid,
where `G` = composed grid width, `H` = banner height, `D` = max cell
duration (so the banner matches the grid's runtime). Banner height
= `font_size_pct * 1.5` of cell height (slightly taller than cell
captions).

### 5.7 ffmpeg failure

Capture stdout + stderr to
`<output_dir>/_grid_<grid_id>_partial/ffmpeg_stderr.txt`. Exit code
4. Per-cell mp4s already saved by execution layer remain intact.

---

## 6. CLI surface

### 6.1 Flags

- `--spec PATH` (required) — path to grid spec YAML (outside-repo).
- `--out PATH` — composed grid mp4 destination. Default:
  `<output_dir>/grid_{ts}_{title-slug}.mp4`.
- `--max-parallel-groups N` — concurrent groups. Default 2.
- `--dry-run` — resolve spec + show cell list + grouping + cost
  estimate, NO compute.
- `--ephemeral` — pass-through to each underlying generate call.

### 6.2 Exit codes

| Code | Meaning |
|---|---|
| 0 | Full success (grid mp4 written) |
| 1 | Spec validation / CLI usage error (no compute happened) |
| 2 | Partial: ≥1 cell failed; per-cell mp4s saved; no grid composed |
| 3 | Budget cap exceeded mid-grid; partial mp4s saved; pods destroyed |
| 4 | ffmpeg invocation failed; per-cell mp4s intact; no grid composed |
| 5 | Pod teardown failure on group exit (the destroy-on-teardown post-condition probe raised) |

### 6.3 Worked example (P1 close-out, Tier-4)

```bash
$ cat ~/.kinoforge/grids/p1-tier4-strength.yaml
title: 'Wan 2.2 14B Arcane strength sweep'
layout: '1x3'
budget_cap_usd: 1.50
cells:
  - generate:
      config: examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml
      overrides:
        loras[0].strength: 0.5
        loras[1].strength: 0.5
    caption: 'strength=0.5'
  - generate:
      config: examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml
      overrides:
        loras[0].strength: 1.0
        loras[1].strength: 1.0
    caption: 'strength=1.0'
  - generate:
      config: examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml
      overrides:
        loras[0].strength: 1.5
        loras[1].strength: 1.5
    caption: 'strength=1.5'

$ pixi run preflight
$ pixi run kinoforge grid --spec ~/.kinoforge/grids/p1-tier4-strength.yaml --dry-run
[grid resolve] 3 cells, 1 capability_key group (warm-reuse intra-group)
[grid plan]    group 0 (3 cells): wan22-14b-arcane @ A100 80GB ~ $1.20 est
[grid plan]    composed mp4 → output/grid_20260625-143012_wan-2-2-14b-arcane-strengt.mp4
[grid plan]    budget_cap=$1.50, est=$1.20, headroom=$0.30

$ pixi run kinoforge grid --spec ~/.kinoforge/grids/p1-tier4-strength.yaml
[grid run] group 0 starting: 3 cells, provider=runpod, gpu=A100-80GB
[grid run] cell 0/3 generating: strength=0.5 ...
[grid run] cell 0/3 done: <output>/_grid_a3b9/cell_0.mp4 (sha=4f2a...e8c1, $0.41)
[grid run] cell 1/3 warm-reusing pod ... strength=1.0 ...
[grid run] cell 1/3 done: <output>/_grid_a3b9/cell_1.mp4 (sha=8b1c...90d7, $0.38)
[grid run] cell 2/3 warm-reusing pod ... strength=1.5 ...
[grid run] cell 2/3 done: <output>/_grid_a3b9/cell_2.mp4 (sha=c5e0...23a4, $0.39)
[grid run] group 0 destroying pod ... teardown OK
[grid compose] ffmpeg 3-cell xstack 1x3 with captions ...
[grid compose] grid mp4 → output/grid_20260625-143012_wan-2-2-14b-arcane-strengt.mp4 (sha=11ab...ff03)
[grid summary] 3/3 cells succeeded, spent $1.18 of $1.50 cap, exit 0

$ kinoforge list
[instance overview] No running instances.
No instances recorded in ledger.
```

---

## 7. File layout + module boundaries

### 7.1 New files

```
src/kinoforge/core/grid/
  __init__.py              # public re-exports: GridSpec, run_grid, GridResult
  spec.py                  # GridSpec / GridCell / GenerateCell / PathCell / CaptionStyle
                           # + GridSpecUnderRepoError + spec loader (mirrors vault.Vault.load)
  dotted_path.py           # set_path(obj, path_str, value) — list/dict walker w/ [N] indexing
  grouping.py              # group_cells_by_capability_key(cells) -> dict[CapKey, list[CellIdx]]
  executor.py              # run_grid_groups(spec, provider_factory) -> list[GridCellResult]
  compose.py               # probe_inputs, build_filter_graph, _escape_drawtext,
                           # layout resolver, subprocess shell-out
  errors.py                # GridSpecUnderRepoError, GridCellPathMissing, GridCellFailure,
                           # GridBudgetExceeded, FfmpegInvocationError, FfmpegNotFoundError
```

### 7.2 Touched files

```
src/kinoforge/cli/_commands.py      # +_cmd_grid (CLI entry)
src/kinoforge/cli/_main.py          # +grid subparser wiring
README                              # +`kinoforge grid` section w/ minimal example
examples/grids/                     # NEW dir w/ ONE illustrative spec (alias-only, in-repo)
tests/smoke/live_wan21/test_lora_strength_variation.py   # rewire RED → GREEN
tests/smoke/release_wan22/test_lora_strength_variation.py # rewire RED → GREEN
tests/_smoke_harness/grid.py        # NEW shared helper for both tier smokes
```

### 7.3 Public API (anything outside `core/grid/` imports)

- `from kinoforge.core.grid import GridSpec, run_grid, GridResult`
- `run_grid(spec: GridSpec, *, provider_factory, output_dir: Path, budget_tracker: BudgetTracker) -> GridResult`
  — async; returns full per-cell result list + composed mp4 path on
  success.
- Internal modules (`dotted_path`, `grouping`, `executor`,
  `compose`) NOT re-exported. Tests import via fully-qualified paths.

### 7.4 Module dependency direction (no cycles)

```
cli/_commands.py
    ↓
core/grid/__init__.py
    ↓ ↓ ↓ ↓
spec.py  grouping.py  executor.py  compose.py
   ↓         ↓             ↓            ↓
dotted_path  CapabilityKey  provider/  (subprocess only)
              (existing)    lifecycle
                            (existing)
```

---

## 8. Testing strategy

Follows `test-design` skill (user-scope). Every test states behavior +
concrete bug that breaks it. No weak assertions, no implementation
mirroring.

### 8.1 Unit tests (no live spend, all green before any live fire)

`tests/core/test_grid_spec.py`
- `GridSpec` parses minimal valid spec → roundtrips through pydantic.
  Bug: `extra="forbid"` drift swallows typos.
- Spec under repo root → `GridSpecUnderRepoError`. Bug: mirror of
  `VaultUnderRepoError` regresses, leaks LoRA-named specs into git.
- Cell with BOTH `generate:` and `path:` → ValueError. Bug:
  ambiguous cell silently picks one.
- Cell with NEITHER → ValueError. Bug: empty cell composes as
  black-frame undetected.
- Override value that's a list or dict → ValueError. Bug: deep-merge
  sneaks in via "user expected it to work".
- Spec missing `budget_cap_usd:` → ValueError. Bug: unbounded grid
  spend.
- Caption containing vault token → after
  `RedactionRegistry.register_vault(...)`, formatted spec log line
  redacts the token. Bug: caption strings bypass the existing log
  filter.

`tests/core/test_dotted_path.py`
- `set_path(cfg, 'loras[0].strength', 0.5)` mutates exactly that
  location, no neighbor drift. Bug: index off-by-one or aliasing.
- `set_path(cfg, 'loras[99].strength', 0.5)` raises IndexError. Bug:
  silent list extension creates phantom LoRA.
- `set_path(cfg, 'no.such.field', 0.5)` raises KeyError. Bug: typo'd
  path adds spurious attr that pydantic re-validation also misses.
- `[*]` wildcard rejected with explicit "v1 doesn't support wildcards"
  error. Bug: ambiguous semantics ship by accident.
- Post-mutation cfg fails pydantic re-validation (e.g. strength=-1) →
  loud error. Bug: cell ships invalid cfg to provider, fails opaquely
  deep in engine.

`tests/core/test_grid_grouping.py`
- 3 cells, same base cfg, only `loras[*].strength` differs → ALL one
  `capability_key` group. Bug: strength stops being branch-invariant
  in matcher.
- 3 cells, different `config:` per cell → 3 groups. Bug: groups
  collapse, wrong pod reused.
- Mixed: 2 cells same group + 1 `path:` cell → 2 groups (1 compute,
  1 degenerate). Bug: `path:` cells trigger phantom pod provisioning.

`tests/core/test_grid_executor.py` (mocked provider)
- Cell 1 raises mid-grid → cells 2+3 still attempted, all 3 results
  recorded with `status` tag. Bug: fail-fast abort wastes spent
  compute on cell 1's pod.
- Group worker destroys pod on partial failure too. Bug: pod leaks
  $/hr after grid crashes (the exact failure mode the
  destroy-on-teardown fix just shipped to prevent — must extend
  coverage to grid path).
- `BudgetTracker` cap exceeded mid-grid → in-flight cells cancelled,
  pods destroyed via `teardown_pod_or_raise` helper. Bug: budget cap
  silently overrun.

`tests/core/test_grid_compose.py` (ffmpeg path mocked — assert arg
lists, not actual mp4)
- `layout: 'auto'` for N=3 → 2x2 with 1 black-frame slot. Bug: 3x1
  layout makes cells too narrow.
- `layout: '2x3'` for N=5 → 2x3 with 1 black slot at (1,2). Bug:
  cell index → grid-position mapping rotates by mistake.
- `_escape_drawtext("a:b'c%d\\e")` → all 4 special chars escaped.
  Bug: caption with `:` truncates at the colon (drawtext silent
  mis-parse).
- Cell heights differ → all scaled to `min(h)`, all padded to
  preserve aspect. Bug: stretched cells distort the comparison.
- Cell durations differ → shorter cells padded via `tpad`. Bug:
  shorter cell black-frame-tails for last N seconds; viewer reads it
  as "model failed".
- Title banner present → vstack above grid. Bug: title overlays
  cells.

### 8.2 Integration tests (FakeEngine, real spec parse → real ffmpeg, no compute)

`tests/integration/test_grid_end_to_end.py`
- Spec with 3 `path:` cells pointing at fixture mp4s → composes to
  grid mp4 with N=3 cells + captions. Decode result, assert frame at
  t=0 shows 3 distinct regions of expected color. Bug: arg-builder
  regression composes wrong cells.
- Spec with 1 `generate:` cell using FakeEngine → end-to-end
  orchestration runs, mp4 lands, sha256 stable. Bug: orchestration
  path drift breaks for real engines.

### 8.3 Live smoke tests (Tier-3 + Tier-4 — the P1 close-out)

`tests/smoke/live_wan21/test_lora_strength_variation.py` — rewire
existing RED scaffold:
- Drop `pytest.xfail(strict=True)` decorator.
- Write a Tier-3 grid spec to a tmp dir OUTSIDE repo (vault-style)
  via `tests/_smoke_harness/grid.py::write_strength_grid_spec`,
  3 cells = strength={0.5, 1.0, 1.5} via dotted-path overrides on
  the canonical Wan 2.1 1.3B Pokemon+static-rotation cfg.
- Invoke `pixi run kinoforge grid --spec <tmp-spec>` subprocess.
- Assert: exit 0; composed grid mp4 exists; per-cell mp4 shas
  (parsed from CLI stdout summary) pairwise-distinct; budget cap
  $0.30 not exceeded.
- Post-condition: `kinoforge list` → no residual pods (via
  `teardown_pod_or_raise` helper).
- Perceptual eval = operator-loop (view grid mp4 in `output/`); not
  encoded as an assertion in the smoke.

`tests/smoke/release_wan22/test_lora_strength_variation.py` — same
pattern, Wan 2.2 14B MoE pair, budget cap $1.50.
- Bug coverage: `set_adapters(adapter_weights=)` reaches BOTH
  transformers — if only one is wired, strength=0.5 vs 1.5 look
  identical (or only half-different).
- Per-cell sha distinctness + post-condition pod-teardown probe.

### 8.4 AC8 / AST-scan invariants

Existing `tests/test_no_unredacted_writes.py` extended: any new write
seam in `core/grid/` (e.g. partial-failure summary logger,
`ffmpeg_stderr.txt` save, `successful-generations.md` § entry writer)
must route through `RedactionRegistry`-aware sinks. Bug: grid summary
leaks captioned LoRA names to log file.

### 8.5 Live spend discipline

- NO live smoke runs DURING design / implementation iteration.
- Live fire ONLY after all unit + integration + AC8 tests green AND
  RED scaffolds rewired GREEN against mocked engine first.
- `pixi run preflight` clean immediately before each tier fire.
- Live-smoke polling rule (CLAUDE.md): every 60–90s during the run,
  probe RunPod runtime utilization; kill on 3 consecutive idle
  probes.

---

## 9. Deferred / open questions

- **Hosted-Bearer engines as grid cells.** Replicate / Runway / Luma
  responses don't have `capability_key` parity with the diffusers
  path; bolt-on later when Layer 5 (Bearer per-prediction cost
  capture, the post-P1 single-next-action) is shipped.
- **`kinoforge sweep` Option-D matrix helper.** If the Option-A
  verbosity for strength sweeps actually bites in practice, ship a
  small `kinoforge sweep --spec <matrix.yaml> --out-grid <out.yaml>`
  that expands a matrix into an Option-A grid spec. Keeps `kinoforge
  grid` itself focused on one job.
- **Two-axis sweep ergonomics.** Cross-products like
  `{model} × {strength}` currently require explicit cells. If common
  in practice, consider Option D's matrix expansion natively.
- **Animated transitions / fades.** Out of scope; user can compose a
  second `kinoforge` invocation OR run ffmpeg manually on the grid
  output. Reconsider if requested.
- **`--ephemeral` end-to-end audit on grid.** Pass-through is safe
  on paper but the grid summary log + partial-failure breadcrumb
  surfaces need explicit ephemeral coverage. Track as a follow-up.

---

## 10. Success criteria

This spec is GREEN when:

1. `kinoforge grid --spec <outside-repo-path>` runs end-to-end on
   a 3-cell strength sweep without operator intervention.
2. Tier-3 smoke `test_lora_strength_variation_wan21` passes live;
   3 distinct mp4 shas, grid mp4 lands, pod cleanly destroyed,
   budget under $0.30.
3. Tier-4 smoke `test_lora_strength_variation_wan22` passes live;
   3 distinct mp4 shas on Wan 2.2 14B MoE pair, grid mp4 lands,
   pod cleanly destroyed, budget under $1.50.
4. Composed grid mp4 visually confirms strength variation reaches
   both transformers (operator-loop perceptual eval — load-bearing
   check that ships P1 GREEN end-to-end, not just code-complete).
5. `successful-generations.md` records the two grid runs with full
   recipe + per-cell + composed shas.
6. `PROGRESS.md` updated: P1 → CLOSED FULL_GREEN, stale "13 commits
   ahead, NOT merged" line corrected, P1 entry archived below the
   active-workstream section.
