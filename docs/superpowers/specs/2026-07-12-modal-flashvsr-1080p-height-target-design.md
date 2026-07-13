# Modal FlashVSR 1080p height-target upscale — design

**Date:** 2026-07-12
**Status:** validated (brainstorm approved)
**Spec type:** capability parity + config + live proof (no production code change)

## Problem

RunPod exposes a **height-target** ("1080p") upscale — `upscale.scale: 1080p` — as an
alternative to a raw factor (`2x` / `4x`). Two RunPod configs ship it:

- `examples/configs/runpod-diffusers-flashvsr-1080p-upscale.yaml` (upscale-only)
- `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale.yaml` (render+upscale)

Modal has only a **factor** upscale config (`modal-diffusers-flashvsr-x4-upscale.yaml`,
`scale: 4x`). No height-target config exists for Modal.

## Key finding — the capability is already present in Modal

Height-target is **provider-agnostic controller logic**, not a per-provider server feature:

- `pipeline/upscale.py:_run_height` resolves a `ScaleTarget(kind="height")` via
  `core/scale_resolver.py:resolve_height_target(source_h, factors, requested_h)` to the
  engine's native factor (FlashVSR = 4x), then records an optional `downscale_to`.
- The remote GPU server (`wan_t2v_server`) only ever receives `kind="factor"`.
- The materialize boundary (`pipeline/materialize.py:finalize_upscaled_bytes` →
  `pipeline/downscale.py:downscale_video_bytes`) lanczos-downscales the returned bytes
  (1920 → 1080) on the **controller**, after fetching from the remote — identical path
  regardless of provider.
- No validation check gates height-target on provider or engine
  (`validation/checks/upscale.py` gates only `seedvr2`).

Therefore Modal already runs the full height-target path. Modal's x4 upscale is live-green
(`successful-generations.md` §24); height-target is live-proven on RunPod. The delta a Modal
1080p run exercises is a controller-side lanczos downscale that cannot fail per-provider.

**Consequence:** no production code changes. The gap is only the missing Modal config, plus
a live proof to close the parity claim end-to-end on Modal.

## Scope

1. New config `examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml`.
2. Offline test asserting the cfg parses and its scale resolves to a **height** ScaleTarget.
3. RED live scaffold committed BEFORE any spend (durability rule).
4. Live Modal upscale smoke (480² → 1920² → 1080²) with util-poll, dims + frame-QA,
   teardown verification, and a `successful-generations.md` entry.

Out of scope (explicitly, per YAGNI): a Wan-render+FlashVSR-1080p Modal cfg (the
render+upscale variant). Only the upscale-only 1080p config ships here.

## Components

### 1. Config — `modal-diffusers-flashvsr-1080p-upscale.yaml`

A clone of `modal-diffusers-flashvsr-x4-upscale.yaml` with exactly these changes:

- `upscale.scale: 4x` → `upscale.scale: 1080p`.
- Header comment rewritten to the height-target explainer, mirroring the RunPod 1080p
  config's preamble: 1080p on a 480² source resolves to FlashVSR native 4x (→1920²),
  then the orchestrator materialize boundary lanczos-downscales 1920→1080 (aspect
  preserved → 1080×1080 square). Cross-reference
  `docs/superpowers/specs/2026-07-05-height-target-upscale-design.md`.
- Usage example updated to name the new config filename.

Everything else is byte-identical to the Modal x4 cfg: `engine.kind: diffusers`,
`python:3.13-slim` image, torch 2.6.0+cu124 pip trio, cp313 BSA wheel URL, `upscale_only:
true`, `models: []`, `compute.provider: modal`, 80GB A100/H100 preference, the M3 lifecycle
timings, `spec.model: flashvsr-wan21-bfloat16`, and the `upscale.flashvsr` block
(`weights_bundle`, `precision: bfloat16`, `window_size: 24`, `tile_size: 512`,
`long_video_mode: false`).

**Interface / dependency:** consumed by `kinoforge upscale --config`. Depends on the
existing height-target pipeline (already shipped) and the Modal provider (live-green).

### 2. Offline test

A test in the existing config-parsing test surface that:

- Loads the new YAML through the normal Config loader.
- Asserts `ScaleTarget.parse(cfg.upscale.scale)` returns `kind == "height"` and
  `value == 1080` (i.e. NOT `kind="factor"`).
- Asserts `cfg.upscale.engine == "flashvsr"` and `cfg.compute.provider == "modal"`, so a
  future edit that accidentally reverts the scale or provider fails loudly.

Per the test-design skill: the behavior under test is "the Modal 1080p cfg is a
height-target flashvsr/modal cfg"; the bug it catches is a copy-paste that leaves
`scale: 4x` (factor) or the wrong provider, silently shipping a non-height config.

### 3. RED live scaffold — `tests/live/test_modal_flashvsr_1080p.py`

Mirrors the M3 x4 live scaffold (`tests/live/test_modal_flashvsr_x4.py`): gated
(skip/xfail unless a live-run env marker is set) so it is RED/inert in CI. Its purpose is
to satisfy the durability rule — the scaffold that drives live spend must be committed
before the spend fires. Committed atomically with (or immediately after) the config +
offline test, verified present before the live invocation.

### 4. Live Modal smoke

- **Preflight:** `pixi run preflight` (creds present, zero pods, clean tree, exit 0).
- **Fixture:** the 480²/81f clip named in CLAUDE.md
  (`output/20260630-221857_..._Photorealistic-cinem.mp4`); confirm it exists first, else
  pick another 480² fixture from `output/`.
- **Invoke:**
  ```
  pixi run -e live-modal kinoforge upscale \
    --config examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml \
    --video <fixture> \
    --no-reuse
  ```
- **Util-poll (mandatory):** every 60–90 s call `ModalUtilEndpoint.read_util` (ledger-
  resolved). GPU 0% for ≥3 consecutive probes while a generation is in flight ⇒ dead/
  stalled container: capture the boot log, destroy, fail fast. Do NOT poll spend as the
  health signal.
- **Dims check:** ffprobe the output — MUST be **1080×1080**. 1920×1920 would mean the
  materialize downscale did not run (the whole point of the height-target path).
- **Frame-QA (mandatory before green):** extract ~5 frames via
  `kinoforge.core.frames.ffmpeg_frames_by_count`, eyeball for artifacts, temporal
  coherence, prompt adherence, and fidelity vs the 1920² x4 sibling. Record the verdict;
  flag ⚠️ if anything is not clearly high quality.
- **Teardown:** `--no-reuse` auto-destroys; then **verify** with `kinoforge list` —
  expect both `No running instances.` AND `No instances recorded in ledger.`. Destroy
  explicitly if either shows a pod.
- **Log:** new `successful-generations.md` section — new YAML shape (Modal + height-target)
  is a new reproduction recipe ⇒ a full new entry, not a "See also".

## Data flow

```
kinoforge upscale --config <modal-1080p> --video <480² clip>
  → Config load: upscale.scale="1080p" → ScaleTarget(kind="height", value=1080)
  → Modal provider provisions FlashVSR server (fast-boot image bake, §24)
  → UpscaleStage._run_height:
       resolve_height_target(source_h=480, factors=[4], requested_h=1080)
         → upscale_factor=4, downscale_to=1080
       → remote /upscale with ScaleTarget(kind="factor", value=4) → 1920² bytes
  → materialize boundary: downscale_video_bytes(bytes, target_h=1080) → 1080² bytes
  → artifact written; --no-reuse destroys the Modal app
```

## Error handling

- Fixture missing → fail before spend (preflight-style check), pick an alternate 480² clip.
- Container preemption / 0% GPU → util-poll catches it; capture boot log, destroy, fail.
- Output dims 1920² not 1080² → height-target downscale regression; fail the smoke, do not
  log green.
- Frame-QA garbage → ⚠️ flag; do not report green on dims alone (2026-07-03 FlashVSR lesson).

## Testing

- **Offline:** the config-parse test above (green in CI, no spend).
- **Live:** the RED scaffold + the manual smoke above (one-shot, `--no-reuse`).

## Durability / ordering

1. Write config + offline test; run offline test green.
2. Commit config + offline test + RED live scaffold (scaffold committed BEFORE spend).
3. `pixi run preflight` → run the live smoke.
4. Frame-QA, dims, teardown verify.
5. Commit the `successful-generations.md` entry + PROGRESS update.
