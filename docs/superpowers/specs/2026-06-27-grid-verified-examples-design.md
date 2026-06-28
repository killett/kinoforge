# Grid verified examples — design

**Date:** 2026-06-27
**Status:** validated, awaiting user spec review before plan phase
**Author:** brainstorm session 2026-06-27
**Predecessors:**
- `docs/superpowers/specs/2026-06-25-kinoforge-grid-design.md` (base grid v1)
- `docs/superpowers/specs/2026-06-26-grid-lora-swap-design.md` (lora_swap variant)

This spec adds 9 verified, commitable example grid YAMLs covering every
capability the `kinoforge grid` verb exposes — strength sweep, LoRA
stack swap, prompt sweep, mixed `path:` + `generate:`, and model sweep
— for both the Wan 2.1 and Wan 2.2 family. Each example ships with a
live-run cost ledger + composed mp4 sha + a README sub-section showing
the exact subshell + heredoc command to reproduce it.

---

## 1. Motivation

`kinoforge grid` v1 + the `lora_swap:` variant shipped 2026-06-25
through 2026-06-27 (commits `b21f252..c9832cb`). The docs at
`docs/batch-and-grid.md` describe the schema. The only in-repo example
is `examples/grids/illustrative-strength-sweep.yaml` — explicitly
marked NOT-loadable because `GridSpec.load()` rejects under-repo paths
to protect user-authored specs from accidental commit of vault tokens
or raw LoRA names.

This leaves a documentation gap: a new operator who reads
`docs/batch-and-grid.md` and wants to actually run a grid has to
hand-roll a spec OUTSIDE the repo, with no working reference to copy
from. The illustrative sample isn't runnable; the live smoke configs
under `tests/smoke/` are wired to pytest harnesses rather than the
plain CLI.

This spec closes the gap by shipping 9 example specs in
`examples/configs/grids/` that:

- use only official, repo-verified LoRA refs and prompts (no vault
  tokens / no private material — safe to commit);
- carry a per-spec opt-in `allow_in_repo: true` field so the
  under-repo guard stays strict for user-authored specs;
- have committed live evidence (cost ledger + composed mp4 sha)
  proving each runs end-to-end on RunPod against real Wan weights;
- get a verbatim subshell + heredoc command in `README.md` so
  operators can copy-paste a known-working invocation.

The 9 examples enumerate every grid capability the schema can express:

1. LoRA strength sweep (`generate:` cells, override
   `loras[N].strength`).
2. LoRA stack swap (`lora_swap:` cells, different stacks per cell on
   a single warm pod via `/lora/set_stack`).
3. Prompt sweep (`generate:` cells override `prompt:`).
4. Mixed `path:` + `generate:` (reuses prior cells' mp4s as cheap
   composition fodder + 1 fresh gen).
5. Model sweep (different `config:` per cell, multiple pods, parallel).

Caps 1-4 ship in both Wan 2.1 1.3B and Wan 2.2 14B variants. Cap 5
ships only for Wan 2.1 (intra-family 1.3B / 5B / 14B); Wan 2.2 has
only one size so a same-family model-sweep is degenerate and skipped.

---

## 2. Scope

### In scope

- ~5 LOC change in `src/kinoforge/core/grid/spec.py`: add
  `allow_in_repo: bool = False` to `GridSpec`; bypass
  `GridSpecUnderRepoError` when set.
- 2 unit tests in `tests/core/grid/test_spec.py` locking the opt-in
  semantics.
- New directory `examples/configs/grids/` with:
  - 5 base cfgs (carry top-level `prompt:`; usable as
    `generate.config` from any cell).
  - 9 grid spec YAMLs (each `allow_in_repo: true`).
  - 1 short `README.md` index pointing back to the main README.
  - 1 `_fixtures/` subdir holding mp4s produced by earlier grid runs
    that later mixed-path grids reference.
- 9 live grid runs on RunPod (Wan 2.1 family + Wan 2.2 family).
- 9 evidence files under `tests/live/_grid_examples/<name>.json`
  capturing per-grid cost ledger + composed mp4 sha + post-run probes.
- README "Grid examples — verified" H2 section with 9 H3
  sub-sections + 9 indented TOC entries with anchor links.
- Per-example sub-section ≤ 15 lines: paragraph + subshell + heredoc
  command + evidence file link.

### Out of scope

- New grid executor / compose / cost-sidecar logic. Behaviour of
  every existing grid path stays bit-identical.
- Hosted-Bearer engines (Replicate / Runway / Luma) as grid cells.
- Audio mixing across cells.
- Cross-family model-sweep (e.g. Wan 2.1 14B vs Wan 2.2 14B); single
  example would add ~$1 for marginal pedagogical value.
- New grid capabilities (e.g. cross-engine, cross-provider). Each
  capability listed in §1 already has a code path.

---

## 3. Code change — `allow_in_repo` opt-in

### 3.1 Surface

```python
# src/kinoforge/core/grid/spec.py
class GridSpec(BaseModel):
    # ...existing fields...
    allow_in_repo: bool = False
```

### 3.2 Loader gate

```python
# In GridSpec.load() — wrap the existing under-repo block:
repo_root = _git_repo_root()
if repo_root is not None:
    try:
        p.relative_to(repo_root)
    except ValueError:
        pass
    else:
        # NEW: peek the YAML for the opt-in BEFORE raising. Raise the
        # existing GridSpecUnderRepoError when opt-in absent / false.
        if not _yaml_opts_in_repo(p):
            raise GridSpecUnderRepoError(...)
```

`_yaml_opts_in_repo(p)` reads the YAML once and returns
`bool(data.get("allow_in_repo", False))`. Lives in the same module.
The double-parse cost is one extra `yaml.safe_load` on a sub-KB file —
trivial.

Alternative considered: model-level validation post-parse. Rejected
because the under-repo check happens BEFORE pydantic parse (the spec
file might be malformed for unrelated reasons; we want the under-repo
error to dominate when applicable).

### 3.3 Tests

- `test_allow_in_repo_true_bypasses_under_repo_guard`: write a spec
  with `allow_in_repo: true` to a path under the repo; `GridSpec.load`
  succeeds.
  - Bug: forgetting the opt-in means every spec we ship gets
    rejected by `GridSpec.load`, breaking the entire example pipeline.
- `test_default_under_repo_guard_still_rejects`: write a spec WITHOUT
  the field (default False) under the repo; `GridSpec.load` raises
  `GridSpecUnderRepoError`.
  - Bug: gate flips polarity → user-authored specs get committed by
    accident.

---

## 4. The 9 examples

| # | Spec file | Cap | Cells | Pods | Est cost |
|---|---|---|---|---|---|
| 1 | `wan21-1_3b-strength-sweep.grid.yaml` | strength sweep | 3 × `generate:` override `loras[0].strength` + `loras[1].strength` to `{0.5, 1.0, 1.5}` | 1 | ~$0.05 |
| 2 | `wan21-1_3b-loras-swap.grid.yaml` | lora_swap | 3 × `lora_swap:`: (a) Static Rotation only, (b) Pokemon only, (c) both stacked | 1 | ~$0.05 |
| 3 | `wan21-1_3b-prompt-sweep.grid.yaml` | prompt sweep | 3 × `generate:` override `prompt:` to field-realistic / field-dreamlike / forest | 1 | ~$0.05 |
| 4 | `wan21-mixed-path-plus-generate.grid.yaml` | mixed | 2 × `path:` from #1 + #3 fixtures + 1 × new `generate:` | 1 | ~$0.02 |
| 5 | `wan21-model-sweep.grid.yaml` | model sweep | 3 × `generate:` cfgs `wan21-1_3b-base-no-loras.yaml`, `wan21-5b-base-no-loras.yaml`, `wan21-14b-base-no-loras.yaml`; no LoRAs | 3 | ~$1.20 |
| 6 | `wan22-14b-strength-sweep.grid.yaml` | strength sweep | 3 × `generate:` Arcane high+low strength `{0.5, 1.0, 1.5}` | 1 | ~$1.00 |
| 7 | `wan22-14b-loras-swap.grid.yaml` | lora_swap | 3 × `lora_swap:`: (a) high only, (b) low only, (c) both | 1 | ~$1.10 |
| 8 | `wan22-14b-prompt-sweep.grid.yaml` | prompt sweep | 3 × `generate:` override `prompt:` to field-realistic / field-dreamlike / dawn-flight | 1 | ~$1.00 |
| 9 | `wan22-14b-mixed-path-plus-generate.grid.yaml` | mixed | 2 × `path:` from #6 + #8 fixtures + 1 × new `generate:` | 1 | ~$1.00 |

**Total live spend estimate: ~$5.50.** Kill-switch at $10 cumulative.

### 4.1 Standard refs (verified, official)

**Prompts** (from `examples/configs/prompts/`):
- `field-realistic.txt` — default for single-prompt caps (per memory
  `feedback_standard_test_prompt`).
- `field-dreamlike.txt`, `forest.txt`, `dawn-flight.md` — variants
  for prompt-sweep caps.

**LoRAs** (from `docs/warm-reuse.md`):
- Wan 2.1 1.3B: `civitai:1479320@1673265` (Static Rotation),
  `civitai:1595383@1805395` (Pokemon Sprite Animation).
- Wan 2.2 14B Arcane Style pair: `civitai:2197303@2474081`
  (high_noise), `civitai:2197303@2474073` (low_noise).

### 4.2 Base cfg pattern

Each base cfg in `examples/configs/grids/` MUST include a top-level
`prompt:` field (read by the executor at
`src/kinoforge/core/grid/executor.py:_build_generate_cmd`). Default
prompt = `examples/configs/prompts/field-realistic.txt` content.

Other cfg fields (engine, models, compute, lifecycle) clone the
proven shapes from the existing per-family cfgs:

- Wan 2.1 1.3B: clone `examples/configs/runpod-comfyui-wan-t2v-1_3b.yaml`.
- Wan 2.1 5B: clone `examples/configs/runpod-comfyui-wan-t2v-5b.yaml`.
- Wan 2.1 14B: clone `examples/configs/runpod-comfyui-wan-t2v.yaml`.
- Wan 2.2 14B: clone `examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml`
  (the green path per memory `project_phase52_*` and Task 8 attempt
  #7 fix).

### 4.3 Mixed-path fixtures

Caps #4 and #9 need pre-existing mp4s. Plan stages produce them
naturally from earlier examples:

- After cap #1 + #3 complete: copy each grid's
  `_grid_<id>/cell_0_out/*.mp4` to
  `examples/configs/grids/_fixtures/wan21_strength_cell0.mp4` and
  `_fixtures/wan21_prompt_cell0.mp4` respectively. Then cap #4 runs
  with `path:` cells pointing at those committed files.
- Same for #6 + #8 → `_fixtures/wan22_*_cell0.mp4` → cap #9.

Fixture mp4s are small (~1-3 MB at Wan 2.1 1.3B, ~2-5 MB at Wan 2.2
14B 480×480×81f) so the binary cost in-repo is tolerable. Each lives
under `_fixtures/` so the existing pre-commit large-files allowlist
can scope an exemption if needed.

---

## 5. Stage flow

### Stage 1 — code + scaffold (no spend)

1. Add `allow_in_repo` field + loader gate to `spec.py`.
2. Add 2 unit tests in `tests/core/grid/test_spec.py`.
3. Write all 14 example files (5 base cfgs + 9 grid specs).
4. `pre-commit run --files <changed>` clean.
5. Commit: `feat(grid): allow_in_repo opt-in + 9 verified-example grid specs (scaffold; live evidence pending)`.

### Stage 2 — dry-run gate (no spend)

Per spec, run:

```bash
for s in examples/configs/grids/*.grid.yaml; do
    pixi run kinoforge grid --spec "$s" --dry-run || echo "FAIL: $s"
done
```

Expected: every spec exits 0 with the planned cell list. Fix any
errors inline. Re-stage. Commit only if fixes shipped (likely
none if Stage 1 was careful):
`fix(grid): spec/cfg corrections from dry-run sweep`.

### Stage 3 — live Wan 2.1 family (~$1.30 budget)

USER-ORDERED GATE per `feedback_autonomous_no_gates` —
pre-authorized up to $20 session budget; mechanical preflight +
post-run verify only.

1. `pixi run preflight` → PASS.
2. Run cap #1 (strength) → capture sidecar + composed mp4 sha →
   evidence JSON → commit:
   `test(live): wan21 1.3b strength-sweep grid GREEN — evidence`.
3. Run cap #2 (loras-swap) → evidence → commit.
4. Run cap #3 (prompt-sweep) → evidence → commit.
5. Copy #1 + #3 outputs to `_fixtures/`.
6. Run cap #4 (mixed-path) → evidence → commit
   (includes the fixture mp4s force-added with `git add -f`).
7. Run cap #5 (model-sweep, 3 pods parallel) → evidence → commit.
8. Post-run probe: `pixi run kinoforge list` + RunPod GraphQL → both
   empty.

Live polling rule per CLAUDE.md `Live smoke monitoring` applies to
every step.

### Stage 4 — live Wan 2.2 family (~$4-5 budget)

USER-ORDERED GATE.

1. Preflight PASS.
2. Run cap #6 (strength) → evidence → commit.
3. Run cap #7 (loras-swap) → evidence → commit.
4. Run cap #8 (prompt) → evidence → commit.
5. Copy #6 + #8 outputs to `_fixtures/`.
6. Run cap #9 (mixed-path) → evidence → commit.
7. Post-run probe clean.

### Stage 5 — README integration

For each successfully-verified example (i.e. whose evidence JSON is
committed), add one H3 sub-section to a new `## Grid examples —
verified` section AND one indented TOC entry. Commit:
`docs(readme): verified grid examples for {N} capabilities across Wan 2.1 + 2.2`.

All 9 sub-sections required. Per-cap failure in Stage 3/4 is a
STOP-and-surface event per §7 — Stage 5 is not entered until every
example has a committed evidence file.

---

## 6. README integration shape

**Insertion point:** new H2 `## Grid examples — verified` after the
existing `## Subcommands` table, before `## Configuration`.

**TOC fragment:**

```markdown
- [Grid examples — verified](#grid-examples--verified)
  - [Wan 2.1 1.3B — strength sweep](#wan-21-13b--strength-sweep)
  - [Wan 2.1 1.3B — LoRA stack swap](#wan-21-13b--lora-stack-swap)
  - [Wan 2.1 1.3B — prompt sweep](#wan-21-13b--prompt-sweep)
  - [Wan 2.1 mixed path + generate](#wan-21-mixed-path--generate)
  - [Wan 2.1 model sweep — 1.3B vs 5B vs 14B](#wan-21-model-sweep--13b-vs-5b-vs-14b)
  - [Wan 2.2 14B — strength sweep](#wan-22-14b--strength-sweep)
  - [Wan 2.2 14B — LoRA stack swap](#wan-22-14b--lora-stack-swap)
  - [Wan 2.2 14B — prompt sweep](#wan-22-14b--prompt-sweep)
  - [Wan 2.2 14B mixed path + generate](#wan-22-14b-mixed-path--generate)
```

**Per-sub-section template (≤ 15 lines):**

```markdown
### Wan 2.1 1.3B — strength sweep

Sweep LoRA strength {0.5, 1.0, 1.5} on Static Rotation + Pokemon
Sprite. One pod (CapabilityKey strength-invariant per P1). ~$0.05.

(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml \
        --out output/wan21-1_3b-strength.mp4
)

Evidence: tests/live/_grid_examples/wan21_strength.json.
```

(Code block fenced as `bash` in the actual README.)

---

## 7. Failure handling

### Per-stage

- **Stage 1:** pydantic ValidationError on a cfg → fix inline; not
  committed yet.
- **Stage 2:** any spec fails dry-run → fix inline; re-run loop;
  BLOCK Stage 3 until clean. (No live spend until all 9 dry-runs
  exit 0.)
- **Stage 3/4 grid exit codes** (live):
  | code | meaning | action |
  |---|---|---|
  | 0 | full | capture evidence, commit, next |
  | 1 | spec | impossible if Stage 2 clean → treat as Stage 2 regression |
  | 2 | partial | inspect `_grid_<id>/cell_<i>.stderr.txt`; classify; if transient (proxy 502 / model-DL) re-run; if cfg-shape, fix + re-dry-run |
  | 3 | budget | bump that grid's `budget_cap_usd` + re-run only that grid |
  | 4 | ffmpeg | inspect stderr; investigate before next grid |
  | 5 | teardown — residual pod | STOP; `kinoforge destroy --id <id>` manually; capture pod ID; investigate before next |
- Repeated failure on one grid → STOP, surface to user with
  reproducer (spec path + stderr + cost so far). User decides whether
  to debug + re-fire or descope. Do NOT silently drop a cap; user
  asked for ALL 9 verified.

### Cross-stage budget kill-switch

After every grid, sum `cost_usd` across cost.json sidecars. If
cumulative > $10 → halt remaining grids, commit verified state,
surface to user.

### Live polling per CLAUDE.md

Every 60-90s during live grid: probe RunPod GPU util / CPU / mem /
costPerHr via the kinoforge provider's GraphQL surface. 3 consecutive
idle probes during a should-be-busy phase → kill that grid's pod +
fail-fast.

---

## 8. Testing surface

### Unit (Stage 1)

- `tests/core/grid/test_spec.py::test_allow_in_repo_true_bypasses_under_repo_guard`
- `tests/core/grid/test_spec.py::test_default_under_repo_guard_still_rejects`

### Dry-run integration (Stage 2)

Mechanical bash loop in Stage 2 task; no new pytest file.

### Live evidence schema (Stages 3+4)

`tests/live/_grid_examples/<name>.json` mirrors the predecessor
warm-reuse + warm-attach evidence shape:

```json
{
  "test": "kinoforge grid example: <cap name>",
  "spec_path": "examples/configs/grids/<name>.grid.yaml",
  "result": "PASS",
  "wall_clock_seconds": <n>,
  "grid_id": "grid_YYYYMMDD-HHMMSS_<hex>",
  "composed_mp4_sha256": "<hex>",
  "cells": [
    {"idx": 0, "mp4_sha256": "<hex>", "status": "success", "size_bytes": <n>},
    ...
  ],
  "cost_sidecar": "<sidecar contents>",
  "total_cost_usd": <n>,
  "post_run_runpod_pods": [],
  "post_run_kinoforge_list_clean": true,
  "preflight_pre_spend": "PASS",
  "acceptance_criteria": [
    {"name": "...", "status": "PASS", "evidence": "..."}
  ]
}
```

### README anchor invariant (Stage 5)

Ad-hoc shell check:

```bash
grep '^### ' README.md | <slugify> | diff <TOC anchor list>
```

Promote to a real pytest in `tests/test_readme_anchors.py` ONLY if
this design ships another regression in the area; v1 plan keeps it
inline.

---

## 9. Success criteria

This spec is GREEN when:

1. `allow_in_repo: true` ships as an opt-in field; default-False
   preserves the guard.
2. 5 base cfgs + 9 grid spec YAMLs + 1 grids `README.md` ship in
   `examples/configs/grids/`.
3. Every spec passes `--dry-run` (Stage 2 gate).
4. All 9 grids land live evidence (per user requirement: ALL
   capabilities verified). On per-grid failure, surface to user with
   reproducer and pause; do not silently drop a cap.
5. README has new `## Grid examples — verified` H2 with all 9 H3
   sub-sections, each with verbatim subshell + heredoc command +
   evidence link.
6. README TOC has all 9 matching indented entries with anchor links
   that resolve to the H3 headings.
7. Total live spend ≤ $10.
8. Post-run state: zero residual RunPod pods, zero residual ledger
   entries, no orphan _fixtures/ that aren't referenced by a committed
   spec.

---

## 10. Open questions

None at design time. Plan-time questions deferred to the
implementation plan writer.
