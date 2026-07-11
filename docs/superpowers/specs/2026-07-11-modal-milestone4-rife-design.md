# Modal Milestone 4 — RIFE frame interpolation via fast-boot image bake (design)

**Date:** 2026-07-11
**Status:** Validated (brainstorm approved 2026-07-11)
**Roadmap:** `docs/superpowers/briefs/2026-07-08-modal-provider-roadmap.md` (milestone 4 — the last engine in the Modal set: t2v ✓ §22/§23, upscale ✓ §24, **interpolate ← this**).

## Goal

Run RIFE v4.26 frame interpolation (e.g. 16 fps → 60 fps) on the Modal
serverless-GPU provider, reusing the M3 image-bake fast-boot path. Produce a
live-green 60 fps clip, frame-QA it, tear down clean, and log
`successful-generations.md` §25.

## Approach — pure config (no engine/provider changes)

The fast-boot work already shipped everything the mechanism needs:

- `DiffusersEngine.render_provision` composes `cfg.interpolate.engine`'s
  provision into the **build phase** (`_add("build", ...)`,
  `src/kinoforge/engines/diffusers/__init__.py` ~line 1101) exactly as it does
  the FlashVSR upscaler.
- `ModalProvider` bakes `image_build_script` into the image
  (`echo <b64> | base64 -d | bash`) and boots with the runtime script only.
- The provider's bake apt-installs `curl git build-essential cmake pkg-config`
  before the bake.

So **RIFE-on-Modal bakes automatically**. Milestone 4 is a NEW config file that
mirrors `examples/configs/modal-flashvsr-x4.yaml` (Modal transport shape) and
`examples/configs/interpolate-rife-60fps.yaml` (RIFE block), plus a live proof.

**Decision (2026-07-11):** the provider's always-on apt list (which includes the
FlashVSR-only `build-essential/cmake/pkg-config`) is left AS-IS. RIFE's image
carries the unused toolchain (~200 MB, one-time cached build). Making the apt
list config-driven is deferred — not worth the provider churn for M4. (As it
happens, `build-essential` is likely load-bearing for RIFE too — see the numpy
risk below — so removing it now would be premature.)

## The config: `examples/configs/modal-rife-60fps.yaml`

Mirrors the Modal FlashVSR cfg's transport fields and the RunPod RIFE cfg's
interpolate block. Concrete shape (values may be tuned during implementation):

```yaml
engine:
  kind: diffusers
  precision: fp16
  diffusers:
    # Modal's serialized web_server fn requires image-Python == controller (3.13).
    image: "python:3.13-slim"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      # torch is ADDED vs the RunPod RIFE cfg: python:3.13-slim ships no torch,
      # whereas runpod/pytorch did. No ABI lock (RIFE has no compiled ext like
      # BSA) — torch 2.6.0 cu124 cp313 chosen for stack consistency with M3.
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    embed_modules:
      - "kinoforge.engines.diffusers.servers"
      - "kinoforge.interpolators.rife"
    embed_files:
      - "kinoforge.core.errors"
      - "kinoforge.core.fps_resolver"
    # Skip eager WanPipeline load — pod runs only the on-demand RIFE runtime.
    upscale_only: true

models: []  # no base model; RIFE weights fetched at provision time

compute:
  provider: modal
  image: "python:3.13-slim"   # NO `cloud:` key (SkyPilot-only, fails validation)
  mode: pod
  requirements:
    min_vram_gb: 16           # RIFE runtime VRAM estimate is ~2 GB; 16 is ample
    min_cuda: "12.4"
    max_usd_per_hr: 1.00
    disk_gb: 40
    gpu_preference:
      - "T4"                  # cheapest Modal card (~$0.59/hr, 16 GB, SM75) —
      - "L4"                  # RIFE has no SM80 requirement, so T4 runs it.
      - "A10G"                # capacity fallback
  lifecycle:
    boot_timeout: 30m         # generous; the bake is one-time, boot is seconds
    idle_timeout: 20m
    job_timeout: 12m
    time_buffer: 3m
    max_lifetime: 45m
    budget: 0.5

spec:
  model: "rife-rife49"        # mirror the working RunPod cfg's slug

interpolate:
  engine: rife
  fps: 60.0
  rife:
    weights_ref: "hf:hzwer/RIFE"   # HF repo holding RIFEv4.26_0921.zip (~22 MB)
    model: rife426
    precision: fp16
```

What the RIFE provision emits (composed into `build_script`, verified via the
existing `RifeEngine.render_provision`,
`src/kinoforge/interpolators/rife/_engine.py`):
`apt-get install -y ffmpeg unzip` → `git clone` Practical-RIFE @ pinned commit →
`pip install "numpy<2" opencv-python-headless imageio[ffmpeg]` → `curl` the HF
zip → `unzip` → stage weights into `train_log/` + `/workspace/models/rife/`.
`git`/`curl` come from the provider apt list; `ffmpeg`/`unzip` RIFE self-installs
at bake time (persists in the image, so the runtime server has ffmpeg for mux).

## Data flow (unchanged from M3)

1. `kinoforge interpolate` → orchestrator renders provision → `build_script`
   (RIFE apt/git/pip/curl/weights, composed) + `runtime_script` (embed + server
   exec), threaded onto `InstanceSpec`.
2. `ModalProvider.create_instance` → `build_modal_app` apt-installs the toolchain,
   bakes `build_script` into the image (one base64 RUN), boots the container with
   `runtime_script` → `.modal.run` URL as `endpoints["8000"]`.
3. Container boots fast (deps baked); `wan_t2v_server` binds; the CLI POSTs
   `/interpolate`, polls `/interpolate/status/{job_id}`, materializes the 60 fps
   artifact; `--no-reuse` destroys the app.

## Known risks (the live proof exists to surface these)

1. **`numpy<2` on Python 3.13 (the #1 risk).** RIFE pins `numpy<2` (Practical-RIFE
   breaks on numpy 2), but numpy 1.x has no cp313 wheel → pip builds it from
   source. The always-on `build-essential` should let it compile (a few min).
   If it fails or stalls: revisit — either (a) confirm whether the pinned RIFE
   commit actually breaks on numpy 2 and relax the pin, or (b) pin a
   py3.13-buildable numpy. **Accepted as discover-in-live** (brainstorm decision
   2026-07-11); do NOT pre-solve in the cfg.
2. **torch pin.** No ABI lock (unlike BSA); torch 2.6.0 cu124 cp313 for stack
   consistency. T4 is SM75 — fine, RIFE has no SM80 guard. torchvision included
   defensively; drop if unused.
3. **ffmpeg at runtime.** Baked via RIFE's in-provision `apt-get`; the server
   needs it for the final mux. Confirm it survives into the runtime container
   (it should — apt writes persist in the image layer).
4. **T4 capacity.** Cheapest pool can be busy; `gpu_preference: [T4, L4, A10G]`
   falls back. Fast boot keeps the preemption window tiny regardless.

## Testing

- **Offline (no spend):** the cfg loads via `load_config`; `render_provision`
  splits correctly — `build_script` contains the RIFE `git clone` +
  `curl`/weights + `pip install "numpy<2"`; `runtime_script` contains the
  `wan_t2v_server` exec and NOT the installs. Reuse the split-test pattern from
  `tests/engines/diffusers/test_render_provision_split.py`; extend the golden if
  warranted.
- **RED live scaffold:** `tests/live/test_modal_rife_60fps.py` (`pytest.mark.live`),
  committed BEFORE any spend (durability rule — RED is fine).
- **Live proof (USER-GATE):**
  ```bash
  pixi run -e live-modal kinoforge interpolate \
    --config examples/configs/modal-rife-60fps.yaml \
    --video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 \
    --fps 60 --no-reuse
  ```
  Acceptance: container binds fast (bake once, boot seconds); output ~60 fps
  (ffprobe `r_frame_rate=60/1`, ~304 frames for the 81f/16fps 480² source),
  dims unchanged (480×480, temporal-only); frame-QA on ~5 frames shows smooth
  pose deltas (genuine arbitrary-timestep synth, no ghosting/warping);
  `kinoforge list` + `modal app list` clean after exit; `successful-generations.md`
  §25 written.

## Non-goals

- No provider or engine code changes (pure cfg). If the numpy risk forces a code
  change, that's an in-plan deviation, not a planned task.
- No config-driven apt list (deferred).
- No warm-reuse / HF_HOME caching on Modal (separate roadmap items).
- No new interpolation model (RIFE v4.26 only; the axis is Modal-transport, not a
  new interpolator).

## Reproduction lineage

- Modal transport + fast-boot bake: `successful-generations.md` §22 (M1),
  §23 (M2), §24 (M3); memory `reference_modal_provider_gotchas`.
- RIFE recipe: `successful-generations.md` §20 (RunPod A4000); plan
  `docs/superpowers/plans/2026-07-05-frame-interpolation.md`;
  cfg `examples/configs/interpolate-rife-60fps.yaml`.
