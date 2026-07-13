# Design: Uniform config filename scheme for `examples/configs/`

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan
**Plan:** _(to be created by writing-plans)_

## Problem

`examples/configs/` filenames are inconsistent and ambiguous:

- Axis ordering varies: `runpod-comfyui-wan-t2v-14b-2_2.yaml` puts size+version
  *after* mode, in an order the user finds hard to read.
- Model identity is under-specified: `runpod-comfyui-wan-t2v.yaml` and
  `runpod-comfyui-wan.yaml` do not state Wan version (2.1 vs 2.2) or size
  (1.3B / 5B / 14B) — you must open the file to know what it is.
- The `mode:` field inside configs conflates **compute mode** (`pod` /
  `serverless`) with **generation mode** (`t2v` / `i2v` / `flf2v`), so the
  filename is the only reliable place to encode generation mode — and today it
  often does not.

Goal: one uniform, self-describing naming scheme across **every** config, with
all functional and living-doc references updated in lockstep.

## Naming scheme

### Slot template

Fixed slot order; **omit any slot that does not apply**; operation/mode is
**always last**:

```
<provider>-<engine>-<subject>-<qualifier>-<operation>.yaml
```

### Slot vocabulary

| Slot | Values |
|------|--------|
| provider | `runpod` · `modal` · `skypilot-lambda` · `skypilot-vast` · `skypilot` · `bedrock` · `fal` · `replicate` · `runway` · `local` |
| engine | `comfyui` · `diffusers` — omit when the provider implies it (bedrock/fal/replicate/runway) |
| subject | `wan-2_1-14b` · `wan-2_2-14b` · `wan-2_1-1_3b` · `wan-2_2-5b` · `flashvsr` · `spandrel` · `seedvr2` · `rife` · `luma-ray` · `nova-reel` |
| qualifier | `x4` · `x2` · `1080p` · `60fps` · `torch26` · `base` · `no-loras` · `strength-grid` · `lora-flexible-warm-reuse-smoke` · … |
| operation | `t2v` · `i2v` · `flf2v` · `upscale` · `interpolate` · `keyframe` |

### Token rules

- Wan version: `wan-2_1` / `wan-2_2` (underscore decimal — matches existing `2_2`).
- Size: `1_3b` · `5b` · `14b` (lowercase `b`, underscore decimal).
- All-lowercase, hyphen-separated slots.
- Two-stage pipelines chain both operations, generation op first:
  `…-wan-2_2-14b-t2v-flashvsr-upscale.yaml`.

### Forced carve-outs (design decisions)

1. **Grid sweep specs (`.grid.yaml`)** have no `engine:` field and a single
   spec can span multiple sizes (e.g. `model-sweep` spans 1.3B/5B/14B), so they
   cannot take provider/engine/size slots. They are **version-normalized only**
   (`wan21` → `wan-2_1`), keeping their model-family-first identity.
2. **Grid base cells** (the per-cell configs the sweep specs reference) DO take
   the full prefix — they have a concrete engine and size.
3. **Pure tool/demo configs with no model** (`cost.yaml`, `sweeper.yaml`,
   `hosted.yaml`, `local-fake.yaml`) are left unchanged — the scheme has no
   model/mode to fill. Configs that DO carry an implied provider/engine are
   normalized (`diffusers.yaml` → `runpod-diffusers-serverless.yaml`).

## Rename map (59 configs: 50 renamed, 8 unchanged, 1 deleted)

### Delete (1)

- `runpod-comfyui-wan-t2v-14b-2_2.yaml` — marked `DEAD — DO NOT USE`; no
  functional references; deleting frees the name collision with
  `wan.yaml`'s new name (both are Wan 2.2 14B t2v on ComfyUI/RunPod).

### ComfyUI / RunPod generation

| old | new |
|---|---|
| `wan.yaml` | `runpod-comfyui-wan-2_2-14b-t2v.yaml` |
| `runpod-comfyui-wan-t2v.yaml` | `runpod-comfyui-wan-2_1-14b-t2v.yaml` |
| `runpod-comfyui-wan.yaml` | `runpod-comfyui-wan-2_1-14b-i2v.yaml` |
| `runpod-comfyui-wan-t2v-1_3b.yaml` | `runpod-comfyui-wan-2_1-1_3b-t2v.yaml` |
| `runpod-comfyui-wan-t2v-5b.yaml` | `runpod-comfyui-wan-2_2-5b-t2v.yaml` |

### Diffusers / RunPod generation

| old | new |
|---|---|
| `runpod-diffusers-wan-t2v-14b-2_2.yaml` | `runpod-diffusers-wan-2_2-14b-t2v.yaml` |
| `wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml` | `runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml` |
| `wan21-1_3b-strength-grid.yaml` | `runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml` |
| `wan22-14b-lora-flexible-warm-reuse-release.yaml` | `runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml` |
| `wan22-14b-strength-grid.yaml` | `runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml` |

### Modal (diffusers)

| old | new |
|---|---|
| `modal-wan-t2v-1_3b.yaml` | `modal-diffusers-wan-2_1-1_3b-t2v.yaml` |
| `modal-wan-t2v-14b-2_2.yaml` | `modal-diffusers-wan-2_2-14b-t2v.yaml` |
| `modal-flashvsr-x4.yaml` | `modal-diffusers-flashvsr-x4-upscale.yaml` |
| `modal-rife-60fps.yaml` | `modal-diffusers-rife-60fps-interpolate.yaml` |

### Upscale / interpolate (diffusers / RunPod)

| old | new |
|---|---|
| `upscale-flashvsr-x4.yaml` | `runpod-diffusers-flashvsr-x4-upscale.yaml` |
| `upscale-flashvsr-x4-torch26.yaml` | `runpod-diffusers-flashvsr-x4-torch26-upscale.yaml` |
| `upscale-flashvsr-1080p.yaml` | `runpod-diffusers-flashvsr-1080p-upscale.yaml` |
| `upscale-spandrel-x2.yaml` | `runpod-diffusers-spandrel-x2-upscale.yaml` |
| `interpolate-rife-60fps.yaml` | `runpod-diffusers-rife-60fps-interpolate.yaml` |
| `extras/upscale-seedvr2-3b.yaml` | `extras/runpod-diffusers-seedvr2-3b-upscale.yaml` |

### Two-stage pipelines (chain both ops; Wan 2.2 14B t2v; diffusers / RunPod)

| old | new |
|---|---|
| `wan-with-upscale-flashvsr.yaml` | `runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale.yaml` |
| `wan-with-upscale-flashvsr-1080p.yaml` | `runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale.yaml` |
| `wan-with-upscale-spandrel.yaml` | `runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale.yaml` |
| `extras/wan-with-upscale-seedvr2.yaml` | `extras/runpod-diffusers-wan-2_2-14b-t2v-seedvr2-upscale.yaml` |

### Bedrock / hosted-bearer

| old | new |
|---|---|
| `luma-ray.yaml` | `bedrock-luma-ray-t2v.yaml` |
| `nova-reel.yaml` | `bedrock-nova-reel-t2v.yaml` |
| `fal.yaml` | `fal-t2v.yaml` |
| `keyframe-fal-i2v.yaml` | `fal-keyframe-i2v.yaml` |
| `keyframe-fal-flf2v.yaml` | `fal-keyframe-flf2v.yaml` |
| `keyframe-luma.yaml` | `fal-luma-keyframe-i2v.yaml` |
| `comparison/replicate-t2v.yaml` | _unchanged_ (already conformant) |
| `comparison/runway-t2v.yaml` | _unchanged_ (already conformant) |

### SkyPilot

| old | new |
|---|---|
| `skypilot.yaml` | `skypilot-cpu.yaml` |
| `skypilot-gpu.yaml` | _unchanged_ |
| `skypilot-lambda.yaml` | `skypilot-lambda-comfyui.yaml` |
| `skypilot-lambda-flashvsr.yaml` | `skypilot-lambda-diffusers-flashvsr-upscale.yaml` |
| `skypilot-vast-flashvsr.yaml` | `skypilot-vast-diffusers-flashvsr-upscale.yaml` |

### Tool / demo (normalize-what-fits)

| old | new |
|---|---|
| `diffusers.yaml` | `runpod-diffusers-serverless.yaml` |
| `hosted.yaml` | _unchanged_ (engine demo, no model) |
| `local-fake.yaml` | _unchanged_ (engine demo, no model) |
| `cost.yaml` | _unchanged_ (tool config) |
| `sweeper.yaml` | _unchanged_ (tool config) |

### Grid cells (full prefix)

| old | new |
|---|---|
| `grids/wan21-14b-base-no-loras.yaml` | `grids/runpod-comfyui-wan-2_1-14b-base-no-loras.yaml` |
| `grids/wan21-1_3b-base-no-loras.yaml` | `grids/runpod-diffusers-wan-2_1-1_3b-base-no-loras.yaml` |
| `grids/wan21-1_3b-base.yaml` | `grids/runpod-diffusers-wan-2_1-1_3b-base.yaml` |
| `grids/wan21-5b-base-no-loras.yaml` | `grids/runpod-comfyui-wan-2_1-5b-base-no-loras.yaml` |
| `grids/wan22-14b-base.yaml` | `grids/runpod-diffusers-wan-2_2-14b-base.yaml` |

### Grid sweep specs (`.grid.yaml`; version-normalize only — carve-out)

| old | new |
|---|---|
| `grids/wan21-1_3b-loras-swap.grid.yaml` | `grids/wan-2_1-1_3b-loras-swap.grid.yaml` |
| `grids/wan21-1_3b-prompt-sweep.grid.yaml` | `grids/wan-2_1-1_3b-prompt-sweep.grid.yaml` |
| `grids/wan21-1_3b-strength-sweep.grid.yaml` | `grids/wan-2_1-1_3b-strength-sweep.grid.yaml` |
| `grids/wan21-mixed-path-plus-generate.grid.yaml` | `grids/wan-2_1-mixed-path-plus-generate.grid.yaml` |
| `grids/wan21-model-sweep.grid.yaml` | `grids/wan-2_1-model-sweep.grid.yaml` |
| `grids/wan22-14b-loras-swap.grid.yaml` | `grids/wan-2_2-14b-loras-swap.grid.yaml` |
| `grids/wan22-14b-mixed-path-plus-generate.grid.yaml` | `grids/wan-2_2-14b-mixed-path-plus-generate.grid.yaml` |
| `grids/wan22-14b-prompt-sweep.grid.yaml` | `grids/wan-2_2-14b-prompt-sweep.grid.yaml` |
| `grids/wan22-14b-strength-sweep.grid.yaml` | `grids/wan-2_2-14b-strength-sweep.grid.yaml` |

### Manifests

| old | new |
|---|---|
| `manifests/runpod-comfyui-wan-manifest.yaml` | `manifests/runpod-comfyui-wan-2_1-14b-i2v-manifest.yaml` |
| `manifests/batch-prompts.yaml` | _unchanged_ (multi-model batch demo) |

## Reference-update policy

- **Mechanism:** `git mv` for every rename (preserves file history).
- **Update — functional (must, or things break):**
  - `tests/` + `src/` hardcoded config paths (~40 sites).
  - Grid sweep-spec `config:` paths pointing at renamed base cells.
  - `tests/live/_grid_examples/*.json` `spec:` paths (loaded by tests).
  - `tests/engines/diffusers/_golden_provision.json` (compared in tests).
  - `manifests/batch-prompts.yaml` header comment referencing `wan.yaml`.
- **Update — living docs:** `README.md`, `PROGRESS.md`,
  `successful-generations.md`, `docs/*.md` (`engines.md`, `warm-reuse.md`,
  `CLOUD-CREDS.md`, etc.).
- **Frozen — do NOT edit:**
  - `docs/superpowers/plans/` + `docs/superpowers/specs/` archives — dated
    point-in-time records; the old filenames were accurate when written.
  - `tests/live/evidence/*.txt` stdout logs — historical run evidence.

## Verification

- **Every renamed OLD basename returns zero hits** outside frozen archives:
  `rg '<old-basename>' --glob '!docs/superpowers/**' --glob '!tests/live/evidence/**'`.
- **`pixi run test`** green (config-loading + example + grid + modal tests all
  resolve the new paths).
- **`tests/test_examples.py`** (walks `examples/configs/`) passes — proves no
  config is left dangling and all still parse.
- **`pixi run pre-commit run --all-files`** clean.

## Out of scope

- No change to config file *contents* beyond the intra-`configs/` reference
  edits above (grid `config:` paths). Header comments that mention a sibling by
  old name inside a renamed/living file are updated opportunistically; frozen
  archives are not.
- No change to the `mode:` field semantics (the compute-vs-generation conflation
  is noted as motivation but not fixed here).
