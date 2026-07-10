# Design — Modal Milestone 3: FlashVSR 4x full-res (480²→1920²) on Modal 80GB

> **Milestone 3** of the Modal provider roadmap
> (`docs/superpowers/briefs/2026-07-08-modal-provider-roadmap.md`). Milestones 1
> (Wan 2.1 T2V-1.3B / A10, `successful-generations.md` §22) and 2 (Wan 2.2
> T2V-A14B / A100-80GB, §23) are LIVE-GREEN. This milestone proves the
> **upscale axis** — full-resolution FlashVSR 4x super-resolution — on the SAME
> Modal transport.

## Goal

Run a FlashVSR v1.1 4x upscale of a 480×480×81f clip → **1920×1920** on a Modal
**80GB** GPU (A100-80GB / H100), frame-QA it against the source, and log it.
Full native resolution — unlike the SkyPilot/Lambda 40GB proof (§21) that had to
downscale the source to 288² and trim to ~41 frames to fit. An 80GB card runs the
full 480→1920 at reference quality (BSA peak ~42–46 GB with `tile_size=512`).

## The wall the roadmap did not foresee — BSA cp311 vs Modal py3.13

The roadmap assumed FlashVSR "reuses the diffusers server upscale path" as cleanly
as M2 reused the gen path. It does not, for one reason:

- Modal's **serialized** web-server function (`providers/modal/_app.py:89`,
  `serialized=True`) cloudpickles the boot closure on the controller and unpickles
  it in the container, so the **image Python must equal the controller Python
  (3.13)** — every Modal image is `python:3.13-slim`. There is no non-serialized
  path in the provider.
- FlashVSR's Block-Sparse-Attention (BSA) kernel ships as a **prebuilt cp311**
  wheel (`block_sparse_attn-...-cp311-cp311-...whl`, default in `core/config.py`).
  pip **rejects a cp311 wheel on a py3.13 interpreter** → provision fails.
- The runtime's dense-attention fallback (`upscalers/flashvsr/_runtime.py:44`,
  `_dense_masked_attention`) is **debug-only by design** — the docstring says
  "Slow and memory-hungry by design"; the comment records an 83 GiB bool-mask OOM
  at 1920² (pod `0t7wo4sthf1o70`). Not a production path. **Ruled out.**

**Decision (user-selected fork, 2026-07-09):** build a **cp313 BSA wheel** and
point the Modal cfg's `bsa_wheel_url` at it. Full BSA speed/quality; a one-time
build artifact; the cleanest analog to how M1/M2 pip-install their torch cp313
stack on `python:3.13-slim`.

## Component 1 — the cp313 BSA wheel (prerequisite)

Generalize the existing one-shot builder `tools/build_bsa_wheel.py` (which produced
the cp311 `bsa-cu124-torch2.6-v1` wheel) to build a **cp313** wheel:

- **Same** CUDA-devel base image (`runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`
  — nvcc 12.4 matches the cu124 torch wheels, pulls fast on RunPod) but the build
  runs under a **py3.13** interpreter installed into that image (uv or
  deadsnakes/pyenv), so `pip wheel` emits a `cp313-cp313` tag.
- **Same** BSA commit `3453bbb1`, **same** torch pins (`torch==2.6.0
  torchvision==0.21.0 torchaudio==2.6.0` + cu124 index), **same**
  `TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0"`. The wheel links against the build-time
  torch, so pinning stays identical (T7.5 lesson: never trust the image tag).
- **New** GH release tag **`bsa-cu124-torch2.6-cp313-v1`** on
  `killett/kinoforge-artifacts`. The release must be **created first** (empty tag)
  via the GH API with `GH_TOKEN` — the builder's `_get_release_id` requires the tag
  to exist. Same upload-and-poll-from-outside driver; pod destroyed on every exit.
- **Refactor shape:** parameterize the module's `_GH_TAG`, `_build_provision_script`
  (add the py3.13-install prelude), and the build-python selection. Keep the cp311
  path reproducible (don't delete it — parameterize). Budget: ~$1, ceiling $2 via
  `Lifecycle.budget_usd`. Runs on **RunPod credit, not Modal credit.**

**Build verification (no Modal spend):** the driver already asserts a `.whl` asset
lands on the release. Additionally assert the uploaded filename contains
`cp313-cp313` before declaring the wheel good (a cp311 upload would silently break
the Modal install). The wheel is not import-tested locally (no py3.13+CUDA locally);
its real proof is the M3 live upscale succeeding.

## Component 2 — the Modal cfg `examples/configs/modal-flashvsr-x4.yaml`

Merge of the M2 Modal transport (`modal-wan-t2v-14b-2_2.yaml`) and the RunPod
torch2.6 FlashVSR cfg (`upscale-flashvsr-x4-torch26.yaml`, model + upscale block):

| Field | Source | M3 value | Why |
|---|---|---|---|
| `compute.provider` | — | `modal` | this milestone |
| `compute` `cloud:` key | — | **omitted** | Modal is non-sky (fails validation) |
| `engine.diffusers.image` | M2 | `python:3.13-slim` | Modal py-match invariant |
| `compute.image` | M2 | `python:3.13-slim` | must match engine image |
| `engine.precision` | torch26 cfg | `bfloat16` | FlashVSR upstream default |
| pip list | M2 + FlashVSR | torch2.6 cu124 trio + fastapi + uvicorn + imageio[ffmpeg] + **modelscope** | modelscope: diffsynth module-top import |
| `embed_modules` | torch26 cfg | `...servers`, `...flashvsr` | FlashVSR runtime dispatch |
| `embed_files` | torch26 cfg | `core.errors`, `core.scale_target` | runtime imports at first /upscale |
| `upscale_only` | torch26 cfg | `true` | skip eager Wan load → ~5min boot |
| `models` | torch26 cfg | `[]` | upscale-only, no eager model |
| `upscale.engine` | torch26 cfg | `flashvsr` | |
| `upscale.scale` | x4 cfg | `4x` | **full** 480→1920 (not 1080p, not downscaled) |
| `upscale.flashvsr.bsa_wheel_url` | — | **cp313 wheel URL** | the Component-1 artifact |
| `upscale.flashvsr.weights_bundle` | torch26 cfg | `hf:JunhaoZhuang/FlashVSR-v1.1` | |
| `upscale.flashvsr.tile_size` | torch26 cfg | `512` | reference-quality tiling; caps VRAM |
| `upscale.flashvsr.window_size` | torch26 cfg | `24` | API-compat (FullPipeline ignores) |
| `compute.requirements.min_vram_gb` | x4 cfg | `80` | 480→1920 4x peaks ~42–46 GB |
| `gpu_preference` | M2 | `[A100-80GB, H100]` | Modal 80GB catalog strings (`_catalog.py`) |
| `max_usd_per_hr` | — | `4.00` | A100-80GB $2.50, H100 $3.95 (Modal catalog) |
| `disk_gb` | — | `60` | FlashVSR weights small vs Wan; BSA wheel + lite bundle |
| `lifecycle.boot_timeout` | — | `45m` | ≥ M2; BSA install + FlashVSR fetch + first-call compile |
| `lifecycle.budget` | — | `2.0` | headroom under $30 ceiling |

`spec.model`: `flashvsr-wan21-bfloat16` (informational; the integration-lock test
asserts every example resolves a non-empty `model_identity`; no `/generate` fires).

**BSA ABI note:** the cp313 wheel is built against `torch==2.6.0+cu124`. The pip
list pins exactly `torch==2.6.0` so the runtime torch matches the wheel's link
target (the cp311 `bsa-cu124-torch2.6-v1` history proves torch-ABI lockstep is
load-bearing — a mismatch surfaces as a `c10` undefined-symbol at inference, not at
install). `python:3.13-slim` + pip torch2.6cu124 provides libcudart/libgomp that
the BSA wheel needs at runtime (M1/M2 proved this base carries the diffusers stack;
BSA is `pip install --no-deps`, so it only needs importable torch + its bundled
CUDA runtime).

## Component 3 — HF_HOME=/cache/hf wiring (provider hardening)

The Modal provider mounts a Volume at `/cache/hf` (`_app.py:36,100`) but never sets
`HF_HOME`, so any HF download re-fetches from scratch on a preempted/cold container
(roadmap flag; M2 §23 follow-up). Wire `HF_HOME=/cache/hf` (the Volume mount) into
the container env so HF-cached weights persist across container starts.

- **Scope:** set `env["HF_HOME"] = req.volume_mount` where the `ModalAppRequest`
  env is assembled in `ModalProvider.create_instance` (do not override an
  operator-supplied `HF_HOME`). Small, standalone, benefits every future Modal run.
- **M3 value is incidental** (upscale-only `models: []` → no HF snapshot; FlashVSR
  weights use a custom fetch to `/workspace/models/flashvsr`, not HF_HOME). Included
  because it is cheap, roadmap-flagged, and unblocks caching for later HF-based
  Modal milestones. **Must not gate the FlashVSR proof** — separable task, its own
  unit test (env dict carries `HF_HOME=/cache/hf`).

## Testing

- **Offline characterization (green offline, no spend)** — mirrors
  `tests/test_modal_config.py`: the M3 config resolves to a `ModalProvider`,
  `cloud is None`, `min_vram_gb == 80`, `upscale.engine == "flashvsr"`,
  `upscale.scale` resolves to 4x, `bsa_wheel_url` contains `cp313`, `models == []`
  / `upscale_only is True`, and `modal_offers(reqs)` returns an 80GB card first
  (A100-80GB ahead of H100 per `gpu_preference`).
- **Build-tool test** — characterize the generalized `build_bsa_wheel.py`:
  `--dry-run` renders a provision script that (a) installs py3.13, (b) pins
  torch==2.6.0, (c) targets the `cp313` release tag. No live build in the unit
  suite.
- **HF_HOME unit test** — `ModalProvider.create_instance` (or the request builder)
  populates `env["HF_HOME"] == "/cache/hf"`, and does not clobber an
  operator-supplied value.
- **Live-smoke scaffold (RED, committed BEFORE any spend)** — mirrors
  `tests/live/test_modal_wan_t2v_1_3b.py`; drives the real `kinoforge upscale`
  against the fixture, asserts a 1920×1920 output + non-corrupt frames.

## Live-run protocol (autonomous, per brief)

1. **Wheel first.** Create the `bsa-cu124-torch2.6-cp313-v1` release, run the
   generalized builder (RunPod), confirm a `cp313-cp313 .whl` asset landed.
2. Commit offline config + tests + RED live scaffold **before** the Modal spend
   (durability rule). `pixi run preflight` (clean tree, creds, zero RunPod pods).
3. Fixture: `output/20260630-221857_..._Photorealistic-cinem.mp4` (480²/81f — the
   CLAUDE.md upscaler-validation fixture). Full clip, no trim (80GB fits 81f).
   ```
   pixi run -e live-modal kinoforge upscale \
     --config examples/configs/modal-flashvsr-x4.yaml \
     --video output/20260630-221857_..._Photorealistic-cinem.mp4 \
     --no-reuse
   ```
4. **Monitor:** Modal has **no util probe** → app-state (`modal app list`) +
   orchestrator-log only (bootstrap.log not proxied; port 8001 unexposed on Modal).
   Abort if no 80GB container starts within a couple minutes, or the boot stalls
   past `boot_timeout`.
5. **Frame-QA (mandatory):** extract ~5 frames of the 1920² output, eyeball for
   corruption / temporal coherence / **fidelity vs the 480² source sibling** (the
   FlashVSR corruption history — §13/§14 psychedelic garbage, root-caused §e82b0d1 —
   is exactly why dims alone are never "green"). ⚠️-flag anything not clearly HQ.
6. **Verify teardown:** `kinoforge list` → no instances **and** `modal app list`
   clean (`--no-reuse` should stop the app; verify AFTER the orchestrator exits).
7. Log to `successful-generations.md` §24 (new axis: FlashVSR upscale on Modal
   80GB). Update PROGRESS + the roadmap (M4 RIFE remains).

## Cost & budget

- A100-80GB $2.50/hr, H100 $3.95/hr (Modal catalog). Upscale-only boot ~5–8 min +
  inference a few min → ~15 min → **~$0.65** Modal credit.
- Wheel build ~$1 on **RunPod** credit (separate pool).
- Cumulative Modal spend after M3 ≈ $2.25 of the $30 ceiling.

## Non-goals (deferred)

- Full HF-weight-caching validation on Modal (M3 exercises the wiring but upscale-
  only doesn't download an HF snapshot; a later HF-based milestone proves the cache
  hit).
- Warm-reuse on Modal; i2v/flf2v; RIFE interpolation (Milestone 4).
- Rebuilding the cp311 wheel path (kept parameterized/reproducible, not exercised).
