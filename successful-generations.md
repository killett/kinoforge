# Successful generations — kinoforge

This file records every qualifying successful kinoforge video generation.
A run qualifies if it introduces a new capability axis — a new mode
(t2v, i2v, flf2v, keyframe, ...), a new provider, engine, or model, or
materially changes the reproduction recipe. Same-tuple repeats get a
"See also" line under the existing TOC entry, not a new section.

Generations run with the `--ephemeral` flag (Layer 5b) MUST NOT appear
in this file under any circumstance — that flag's whole purpose is to
leave no record.

Future agents: see the **Durability rules** section of `/workspace/CLAUDE.md`
for the enforcement policy. The full schema and capture mechanics live
in `docs/superpowers/specs/2026-06-08-successful-generations-log-design.md`.

## Table of Contents

1. `2026-06-08 21:17:16` — [fal-ai/wan-t2v — t2v](#1-2026-06-08-211716--fal-aiwan-t2v--t2v)
2. `2026-06-08 21:26:01` — [Replicate bytedance/seedance-1-lite — t2v](#2-2026-06-08-212601--replicate-bytedanceseedance-1-lite--t2v)
3. `2026-06-08 21:26:59` — [Runway gen4.5 — t2v](#3-2026-06-08-212659--runway-gen45--t2v)
4. `2026-06-08 22:28:40` — [ComfyUI Wan 2.1 14B i2v on RunPod — i2v](#4-2026-06-08-222840--comfyui-wan-21-14b-i2v-on-runpod--i2v)
5. `2026-06-09 21:19:45` — [ComfyUI Wan 2.1 14B t2v on RunPod (in-process warm-reuse, 2 prompts) — t2v](#5-2026-06-09-211945--comfyui-wan-21-14b-t2v-on-runpod-in-process-warm-reuse-2-prompts--t2v)
6. `2026-06-13 11:16:26` — [FakeEngine on RunPod (B3 cross-CLI auto-discovery warm-reuse) — t2v](#6-2026-06-13-111626--fakeengine-on-runpod-b3-cross-cli-auto-discovery-warm-reuse--t2v)
   - See also: `2026-06-13 12:44:24` — B3 smoke re-fire post-closeout at HEAD `8bf51d6`: gen 1 6.3 s / gen 2 2.6 s (ratio 0.41, 59 % cold-skip), pod `k838y2t6mpq91s` (RTX A5000), spend ~$0.0016. Same tuple `(runpod, FakeEngine, fake-model, t2v)`; confirms B3 mechanics still green after Phase 52 BQ-export plumbing diff.
7. `2026-06-18 22:05:08` — [ComfyUI Wan 2.1 1.3B t2v on RunPod (CLI cross-invocation warm-reuse, real engine) — t2v](#7-2026-06-18-220508--comfyui-wan-21-13b-t2v-on-runpod-cli-cross-invocation-warm-reuse-real-engine--t2v)
8. `2026-06-20 05:58:23` — [Diffusers WanPipeline Wan 2.2 T2V-A14B on RunPod (A100 80GB) — t2v](#8-2026-06-20-055823--diffusers-wanpipeline-wan-22-t2v-a14b-on-runpod-a100-80gb--t2v)
9. `2026-06-21 05:11:18` — [Diffusers WanPipeline Wan 2.1 T2V-1.3B + single-LoRA matrix on RunPod (RTX A5000 24GB) — t2v](#9-2026-06-21-051118--diffusers-wanpipeline-wan-21-t2v-13b--single-lora-matrix-on-runpod-rtx-a5000-24gb--t2v)
   - See also: `2026-06-23 17:24:19` — P2 Tier-3 branch-routing live fire at HEAD `14ed527` against fresh pod `44xs7kgyz1nxhy` (RTX A5000 24GB). Cold-boot generated `de430bd6c7cfebd002917cc589b5a2276e1286dbb88ae1f6d58a9b7150aaffe8.mp4`; two `/lora/set_stack` POSTs pinned the new branch invariants: `{"branch":"auto"}` → 200 + inventory carries `branch="auto"` (single-transformer path), `{"branch":"high_noise"}` → HTTP 400 with `{"error":"branch_routing","reason":"branch_unsupported_single_transformer","branch":"high_noise","arity":1}` (Q5 strict-reject). Tests: `tests/smoke/live_wan21/test_branch_routing.py` (2/2 PASSED in 352.9 s wall). Two iterations debugging surfaced `_detect_moe_arity` over-count bugs (commits `66a158c` + `14ed527`); cumulative live spend $0.13 across three pods (`lzzv2jccv6fchg`, `ndbvogufj1qhuq`, `44xs7kgyz1nxhy`), all destroyed via `kinoforge destroy --id`. Same tuple `(runpod, DiffusersEngine, Wan-AI/Wan2.1-T2V-1.3B-Diffusers, t2v)`; covers the P2 branch-routing capability axis without reaching the Tier-4 Wan 2.2 MoE matrix.
10. `2026-06-21 05:37:14` — [Diffusers WanPipeline Wan 2.2 T2V-A14B + Arcane LoRA pair warm-reuse matrix on RunPod (A100 80GB) — t2v](#10-2026-06-21-053714--diffusers-wanpipeline-wan-22-t2v-a14b--arcane-lora-pair-warm-reuse-matrix-on-runpod-a100-80gb--t2v)
11. `2026-06-23 21:35:52` — [Diffusers WanPipeline Wan 2.2 T2V-A14B + per-transformer branch routing on RunPod (A100 80GB SXM) — t2v](#11-2026-06-23-213552--diffusers-wanpipeline-wan-22-t2v-a14b--per-transformer-branch-routing-on-runpod-a100-80gb-sxm--t2v)
   - See also: `2026-06-23 23:07:30` — Tier-4 7/7 **FULL_GREEN** re-fire on pod `2k0gonzmeqw7xj` (NVIDIA A100-SXM4-80GB) at HEAD `9799657` after swap-gap fix (commits `0dec40d` + `305b832` + `fdac5ab`). All 6 mp4 shas distinct; case_5 `wrong_routing` sha `2b68…af65` ≠ case_4 canonical-pair sha `2b49…4945` confirms per-transformer routing actually routes (the prior PARTIAL_GREEN entry could not validate this since case_5 never produced an mp4). Wall clock 32:26; spend $0.80; same tuple `(runpod, DiffusersEngine, Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)`.
   - See also: `2026-06-20 23:33:36` — LoRA-flexible warm-reuse smoke step-1 (cold-boot, 0 LoRAs) at HEAD `7ce3a09` (test `tests/live/test_wan22_lora_warm_reuse.py`, cfg `examples/configs/wan22-lora-flexible-warm-reuse-smoke.yaml`). 5 attempts across 22:01-23:34 PT validated cold-boot + plain Wan 2.2 T2V generation 3 times; published artifacts `output/20260620-221751_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` (attempt 1, pod `tu1rlnrksgs6sd`), `output/20260620-231141_…` (attempt 4, pod `grvq7smmd7r5g0`), `output/20260620-233336_…` (attempt 5, pod `62zmz86zmmjjg1`). Steps 2-4 (warm-attach with `[high+low]` / `[low]` / `[]` Arcane LoRA stacks via `POST /lora/set_stack`) NOT executed live — every attempt blocked on a different smoke-harness bug (proxy URL pattern, mid-flight pod-leak when cold-boot crashes pre-`_extract_pod_id`, missing `?api_key=…`, missing `User-Agent` to clear Cloudflare). All 4 fixes committed (`dc018a3`, `f7677b2`, `7e55036`, `7ce3a09`); harness is now ready for an operator-fire to drive the LoRA-swap matrix. Cumulative T22 spend $2.15. Same tuple `(runpod, DiffusersEngine, Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)`; the LoRA-swap path will graduate to its own section once steps 2-4 land on real hardware.
   - See also: `2026-06-20 12:24:49` — 4-prompt warm-reuse re-fire at HEAD `085781e` (test `tests/live/test_diffusers_wan_t2v_4prompt_live.py`): 1 cold + 3 warm-reuse on the SAME pod, one per file in `examples/configs/prompts/`. Pod `87geau1jcpxr0z` (NVIDIA A100 80GB PCIe), total wall-clock 33 m 19 s, spend ~$0.66. All 4 MP4s ffprobe-verified h264 / yuv420p / 480×480 / 81 frames / 16 fps; 4 distinct sha256s; legs 2/3/4 all `warm-reuse: attached to 87geau1jcpxr0z`. Same tuple `(runpod, DiffusersEngine, Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)`. Stable evidence copies at `.kinoforge/wan22_4prompt_evidence/`. Per-leg table:
     | # | Prompt file | Wall-clock from cold start | Published path | Size | SHA-256 |
     |---|---|---|---|---|---|
     | 0 (cold)  | `field-realistic.txt` | 23 m 02 s | `output/20260620-121432_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` | 1,367,301 B (1.30 MiB) | `50ac05975a13702633bcc35f7012bfee66788c9bdf9d556f9120e3448acb8d40` |
     | 1 (warm1) | `field-dreamlike.txt` | +3 m 26 s | `output/20260620-121758_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-yet-d.mp4` | 1,736,159 B (1.66 MiB) | `36d34431276713e0d20f069759fbffc26aebf0def396674c746d586b43c57a1b` |
     | 2 (warm2) | `forest.txt`          | +3 m 26 s | `output/20260620-122124_diffusers_Wan2.2-T2V-A14B-Diffuser_A-dense-old-growth-f.mp4`   | 798,293 B (0.76 MiB)   | `7b2836285ebd0b64c8a6662fea13ae21e5bac2349b81c3de5c705b309b5b6a94` |
     | 3 (warm3) | `dawn-flight.md`      | +3 m 25 s | `output/20260620-122449_diffusers_Wan2.2-T2V-A14B-Diffuser_Aerial-drone-shot-at.mp4`   | 403,909 B (0.39 MiB)   | `d11c1c194d47a70399838b095f63ea4a3d4dc2e24a99ef2279df8146af46f4f5` |
12. `2026-06-30 21:19:07` — [SpandrelEngine RealESRGAN-x2 upscale on RunPod (wan_t2v_server multi-engine) — upscale](#12-2026-06-30-211907--spandrelengine-realesrgan-x2-upscale-on-runpod-wan_t2v_server-multi-engine--upscale)
    - See also: `2026-06-30 22:19:07` — T16 multi-stage warm-reuse on pod `4ju5e4ae9jnx6e`: Wan 2.2 T2V-A14B stage-1 (480×480×81) → SpandrelEngine stage-2 (960×960×81) on the same pod, spend $0.25, 908.82 s wall. Same tuple `(runpod, spandrel, RealESRGAN, upscale)` chained after `(runpod, DiffusersEngine, Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)`.
13. `2026-07-03 12:13:45` — [FlashVSR v1.1 diffusion upscaler (4x native) on RunPod A100 80GB — upscale](#13-2026-07-03-121345--flashvsr-v11-diffusion-upscaler-4x-native-on-runpod-a100-80gb--upscale)
14. `2026-07-03 22:10:05` — [Wan 2.2 T2V-A14B + FlashVSR 4x co-resident multi-stage + warm-reuse re-generate on RunPod A100 80GB — t2v+upscale](#14-2026-07-03-221005--wan-22-t2v-a14b--flashvsr-4x-co-resident-multi-stage--warm-reuse-re-generate-on-runpod-a100-80gb--t2vupscale)
15. `2026-07-04 00:02:32` — [Luma UNI-1 image keyframe via agents API (LumaAgentsImageEngine) — t2i](#15-2026-07-04-000232--luma-uni-1-image-keyframe-via-agents-api-lumaagentsimageengine--t2i)
    - See also: `2026-07-04 03:26` — uni-1 4-prompt standard matrix (field-realistic / field-dreamlike / forest / dawn-flight, 16:9): 4/4 green, all 2784×1504, latency 102-194 s (median 118 s), median 6.9 MB. Same tuple `(luma_agents, LumaAgentsImageEngine, uni-1, t2i)`. Manifest: `tests/live/evidence/2026-07-04_luma_matrix_manifest_run1.json`; PNGs under `output/luma-keyframe-matrix/`.
16. `2026-07-04 00:50:21` — [Keyframe→video pipeline: Luma UNI-1 keyframe → fal wan-i2v (E21 data-URI hand-off) — i2v](#16-2026-07-04-005021--keyframevideo-pipeline-luma-uni-1-keyframe--fal-wan-i2v-e21-data-uri-hand-off--i2v)
17. `2026-07-04 01:29:58` — [flf2v pipeline: dual fal flux-schnell keyframes → fal wan-flf2v — flf2v](#17-2026-07-04-012958--flf2v-pipeline-dual-fal-flux-schnell-keyframes--fal-wan-flf2v--flf2v)
18. `2026-07-04 03:56:00` — [Luma UNI-1-MAX keyframes — 4-prompt matrix vs uni-1 — t2i](#18-2026-07-04-035600--luma-uni-1-max-keyframes--4-prompt-matrix-vs-uni-1--t2i)
19. `2026-07-05 04:16:18` — [FlashVSR height-target upscale (scale=1080p → 4x+downscale) on RunPod A100 80GB — upscale](#19-2026-07-05-041618--flashvsr-height-target-upscale-scale1080p--4xdownscale-on-runpod-a100-80gb--upscale)
20. `2026-07-05 22:24:29` — [RIFE v4.26 frame interpolation (16fps→60fps) on RunPod RTX A4000 — interpolate](#20-2026-07-05-222429--rife-v426-frame-interpolation-16fps60fps-on-runpod-rtx-a4000--interpolate)
21. `2026-07-08 01:33:54` — [FlashVSR upscale on Lambda A100 via SkyPilot ssh-tunnel (provider-internal HTTP seam) — upscale](#21-2026-07-08-013354--flashvsr-upscale-on-lambda-a100-via-skypilot-ssh-tunnel-provider-internal-http-seam--upscale)
22. `2026-07-08 22:12:07` — [Diffusers WanPipeline Wan 2.1 T2V-1.3B on Modal serverless GPU (A10) — t2v](#22-2026-07-08-221207--diffusers-wanpipeline-wan-21-t2v-13b-on-modal-serverless-gpu-a10--t2v)
23. `2026-07-08 23:55:31` — [Diffusers WanPipeline Wan 2.2 T2V-A14B on Modal serverless GPU (A100-80GB) — t2v](#23-2026-07-08-235531--diffusers-wanpipeline-wan-22-t2v-a14b-on-modal-serverless-gpu-a100-80gb--t2v)
24. `2026-07-10 23:52:23` — [FlashVSR v1.1 4x upscale on Modal A100-80GB via image-bake fast boot (Milestone 3) — upscale](#24-2026-07-10-235223--flashvsr-v11-4x-upscale-on-modal-a100-80gb-via-image-bake-fast-boot-milestone-3--upscale)
25. `2026-07-11 17:59:51` — [RIFE v4.26 frame interpolation (16fps→60fps) on Modal T4 via image-bake fast boot (Milestone 4) — interpolate](#25-2026-07-11-175951--rife-v426-frame-interpolation-16fps60fps-on-modal-t4-via-image-bake-fast-boot-milestone-4--interpolate)
26. `2026-07-12 01:08:08` — [Cross-CLI warm-reuse + HF Volume weight-cache on Modal (Wan 2.1 1.3B / A10, Milestone 5) — t2v](#26-2026-07-12-010808--cross-cli-warm-reuse--hf-volume-weight-cache-on-modal-wan-21-13b--a10-milestone-5--t2v)

---

## 1. `2026-06-08 21:17:16` — fal-ai/wan-t2v — t2v

| Field | Value |
|---|---|
| **Stack triple** | `fal.ai / FalEngine / fal-ai/wan-t2v` |
| **Mode** | t2v |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `fe75583e190196558103bdb40c00f49b3ee971be` |
| **Date (local TZ)** | 2026-06-08 21:17:16 -0700 (PDT) |
| **Layer / phase** | [Phase 46 (Layer 6)](PROGRESS.md#phase-46--successful-generations-log-scaffold) — first qualifying entry; original first-success trail in [Phase 19 (Layer I)](PROGRESS.md#phase-19--layer-i-falai-adapter--ux-a--hosted-hardening) |

### Exact command

```bash
pixi run kinoforge generate \
  --config examples/configs/fal-t2v.yaml \
  --prompt "$(cat prompt-field-realistic.txt)" \
  --mode t2v
```

### YAML config(s)

**`examples/configs/fal-t2v.yaml`** at SHA `f6045ab1293e92e43f514fb1bbd660285afc5115`:

```yaml
# kinoforge example: FalEngine (fal.ai queue API)
#
# Uses fal.ai's queue API for asynchronous video generation.
# Set FAL_KEY in your .env file (or pass via --env-file).
#
# HostedAPIEngine is NOT used here — fal.ai's wire shape differs from
# HostedAPIEngine's synthetic shim contract.  See examples/configs/hosted.yaml
# for the shim path.

engine:
  kind: fal
  precision: ""
  fal:
    endpoint: "fal-ai/wan-t2v"
    queue_base: "https://queue.fal.run"
    api_key_env: "FAL_KEY"
    url_path: video.url

models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors"
    kind: base
    target: checkpoints

lifecycle:
  budget: 5.0
  # Layer U — heartbeat persistence. Uncomment to enable a background
  # thread that pings provider.heartbeat() and persists the timestamp
  # to the ledger every N seconds. Operator-visible via
  # `kinoforge status --id <id>` ("last_heartbeat=<ISO>"). Default null
  # (disabled). Recommended >= 10s to avoid lock contention at scale.
  # heartbeat_interval_s: 30
  # Layer V — post-session warm-reuse grace window. Sentinel-stale
  # entries within this window are classified LIVE (not ORPHAN_REAP).
  # grace_after_session_s: 300

# --- Layer K: per-job spec & params (optional for fal.ai) -------------------
# FalEngine.validate_spec does not require any spec keys (prompt comes from
# Segment.prompt via Layer J's resolve_prompt helper).  Uncomment to add
# engine-specific knobs to the queue submission body.
#
# spec:
#   guidance_scale: 5.0
#
# params:
#   fps: 24
#   num_frames: 81

# Layer O — user-facing output directory.  Uncomment to override defaults.
# Final clips are published to <dir>/<batch_id>?/{YYYYMMDD-HHMMSS}_{prompt-slug}{ext}.
# Internal artifacts (profile cache, ledger, weights) stay under --state-dir
# regardless of this block.
# output:
#   kind: local        # only "local" ships in v1
#   dir: output        # relative-to-cwd or absolute
#   enabled: true      # set false to skip publishing for this config
```

### Prompt

- **Source:** `prompt-field-realistic.txt` (committed file, last touched at SHA `a4ba38894af816d594ad9cd833b44bfaf239a791`) — referenced by filename only per project policy.

### Env vars / secret names (names only — never values)

- `FAL_KEY` — fal.ai API token (Bearer-style; `engine.fal.api_key_env` in YAML points at this var name).

### Region

fal.ai default (provider chooses region; no operator-side knob in YAML).

### Capability key

`2820ed10e74fbea4bb4ab8e3d338f716db8d86383869ebf793bed423f507caaa` (cached profile at `.kinoforge/_profiles/profiles/2820ed10e74fbea4bb4ab8e3d338f716db8d86383869ebf793bed423f507caaa.json`)

### Output artifact

- **Path (published):** `/workspace/output/20260608-211716_fal_unknown_Photorealistic-cinem.mp4`
- **Path (internal cache):** `/workspace/.kinoforge/run-20260608-211624/c370b7148dc566ce.mp4`
- **File size:** 5,362,487 bytes (5.12 MiB)
- **Container / codec:** `mov,mp4,m4a,3gp,3g2,mj2` (MP4 / ISO BMFF) / `h264`
- **Resolution:** 1280×720
- **Duration:** 5.0625 s
- **Frame count:** 81
- **Average frame rate:** 16/1 (16 fps)
- **Bit rate:** 8,474,053 bit/s (~8.47 Mbit/s)

### Cost

- **Total:** ~$0.05 estimated (fal-ai/wan-t2v at 720p / 5 s clip per fal's published rate card; fal does not surface per-prediction cost in the queue API response. Charged against the fal-credit balance attached to `FAL_KEY`.)

### Success criterion

Pending operator visual confirmation. Artifact is valid h264/MP4 at the requested 1280×720 and the 81-frame / ~5 s duration matches the prompt's "5-second shot" intent; ffprobe metadata is internally consistent.

### Failure modes encountered before success

None this run. (Historical Layer-I/Layer-K bugs are documented in PROGRESS.md Phase 19 Task 13.)

### Notes

- Published filename's `model` slug surfaced as `unknown` rather than `wan-t2v`. The OutputSink couldn't resolve the model from `engine.fal.endpoint`; this is a small `LocalOutputSink` provenance gap, not a generation defect. Carry-forward for a future polish layer.

---

## 2. `2026-06-08 21:26:01` — Replicate bytedance/seedance-1-lite — t2v

| Field | Value |
|---|---|
| **Stack triple** | `Replicate / ReplicateEngine / bytedance/seedance-1-lite` |
| **Mode** | t2v |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `5a6b34c2e9ff7638effb0e79d71eff769df1b8df` |
| **Date (local TZ)** | 2026-06-08 21:26:01 -0700 (PDT) |
| **Layer / phase** | [Phase 46 (Layer 6)](PROGRESS.md#phase-46--successful-generations-log-scaffold) — first qualifying entry under the C-rule; historical first-success trail in [Phase 43 (Layer 4)](PROGRESS.md#phase-43--layer-4-bearer-provider-comparison-smokes) Task 11 |

### Exact command

```bash
# Requires the `live-hosted` pixi feature env (which ships `replicate`
# and `runwayml` SDKs); the default env does not have them.
pixi run -e live-hosted kinoforge generate \
  --config examples/configs/comparison/replicate-t2v.yaml \
  --prompt "$(cat prompt-field-realistic.txt)" \
  --mode t2v
```

(The CLI auto-loads `/workspace/.env` for `REPLICATE_API_TOKEN`; no need to export.)

### YAML config(s)

**`examples/configs/comparison/replicate-t2v.yaml`** at SHA `671499decc90ace71acc981281aeca7da28a3130`:

```yaml
# kinoforge example: Replicate budget-tier t2v
#
# Sign up:  https://replicate.com/signin
# Get key:  https://replicate.com/account/api-tokens
# Set REPLICATE_API_TOKEN in your .env file (auto-loaded by the CLI).

engine:
  kind: replicate
  precision: ""

spec:
  model: "bytedance/seedance-1-lite"
  mode: t2v

params:
  duration: 5
  resolution: "480p"
  aspect_ratio: "16:9"

# `models:` is required by Config validation but unused for hosted-Bearer engines
# (RemoteSubmitPollEngine.requires_local_weights == False). Synthetic entry only.
models:
  - ref: "synthetic:replicate-hosted"
    kind: base
    target: checkpoints

lifecycle:
  budget: 1.50

output:
  kind: local
  dir: output
  enabled: true
```

### Prompt

- **Source:** `prompt-field-realistic.txt` (committed file, last touched at SHA `a4ba38894af816d594ad9cd833b44bfaf239a791`) — referenced by filename only per project policy.

### Env vars / secret names (names only — never values)

- `REPLICATE_API_TOKEN` — Replicate Bearer token; ReplicateEngine reads it via the Layer-1 `Bearer` AuthStrategy.

### Region

Replicate routes internally; no operator-side region knob in the YAML.

### Capability key

`19e83b51ea131441f2f558f09084381014ddf3e584ee00f9e8b739570f26e9e2` (cached profile at `.kinoforge/_profiles/profiles/19e83b51ea131441f2f558f09084381014ddf3e584ee00f9e8b739570f26e9e2.json`)

### Output artifact

- **Path (published):** `/workspace/output/20260608-212601_replicate_bytedance-seedance-1-lit_Photorealistic-cinem.mp4`
- **Path (internal cache):** `/workspace/.kinoforge/run-20260608-212536/8c6154840b77f277.mp4`
- **File size:** 4,479,160 bytes (4.27 MiB)
- **Container / codec:** `mov,mp4,m4a,3gp,3g2,mj2` (MP4 / ISO BMFF) / `h264`
- **Resolution:** 864×480 (480p at 16:9)
- **Duration:** 5.041667 s
- **Frame count:** 121
- **Average frame rate:** 24/1 (24 fps)
- **Bit rate:** 7,107,426 bit/s (~7.11 Mbit/s)

### Cost

- **Total:** ~$0.10 estimated.
- **Formula:** Replicate's per-prediction billing for `bytedance/seedance-1-lite` at 480p / 5 s. Replicate's response object would carry `metrics.predict_time` (model-execution seconds × rate-card) — the Layer-4 carry-forward "Hosted-engine per-prediction cost capture" notes this is not yet lifted onto `Artifact.meta["cost_usd"]`. Manual estimate from Replicate's published `seedance-1-lite` rate (~$0.02/s of output × 5 s ≈ $0.10).
- **Wall-clock end-to-end:** 26 s (submit → artifact saved). Model predict time is shorter (Replicate adds queue + transport latency).

### Success criterion

Pending operator visual confirmation. Artifact is valid h264/MP4 at the requested 16:9 480p (864×480) and 5 s / 121-frame duration is consistent with `params.duration: 5` + `params.aspect_ratio: "16:9"`. Bitrate 7.1 Mbit/s.

### Failure modes encountered before success

None this run. (Historical Layer-4 bugs are documented in PROGRESS.md Phase 43 carry-forward block, commit `f20a70d`.)

### Notes

- The `live-hosted` pixi feature env was a non-obvious prerequisite: the default env raised `ModuleNotFoundError: No module named 'replicate'`. Documented in the "Exact command" block above so it's discoverable from this entry alone.

---

## 3. `2026-06-08 21:26:59` — Runway gen4.5 — t2v

| Field | Value |
|---|---|
| **Stack triple** | `Runway / RunwayEngine / gen4.5` |
| **Mode** | t2v |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `5a6b34c2e9ff7638effb0e79d71eff769df1b8df` |
| **Date (local TZ)** | 2026-06-08 21:26:59 -0700 (PDT) |
| **Layer / phase** | [Phase 46 (Layer 6)](PROGRESS.md#phase-46--successful-generations-log-scaffold) — first qualifying entry under the C-rule; historical first-success trail in [Phase 43 (Layer 4)](PROGRESS.md#phase-43--layer-4-bearer-provider-comparison-smokes) Task 12 |

### Exact command

```bash
# Requires the `live-hosted` pixi feature env (which ships `runwayml`
# SDK); the default env does not have it.
pixi run -e live-hosted kinoforge generate \
  --config examples/configs/comparison/runway-t2v.yaml \
  --prompt "$(cat prompt-field-realistic.txt)" \
  --mode t2v
```

(The CLI auto-loads `/workspace/.env` for `RUNWAYML_API_SECRET`; no need to export.)

### YAML config(s)

**`examples/configs/comparison/runway-t2v.yaml`** at SHA `671499decc90ace71acc981281aeca7da28a3130`:

```yaml
# kinoforge example: Runway budget-tier t2v
#
# Sign up:  https://runwayml.com/signup
# Get key:  https://dev.runwayml.com/
# Set RUNWAYML_API_SECRET in your .env file (auto-loaded by the CLI).

engine:
  kind: runway
  precision: ""

spec:
  model: "gen4.5"
  mode: t2v

params:
  ratio: "1280:720"
  duration: 5

models:
  - ref: "synthetic:runway-hosted"
    kind: base
    target: checkpoints

lifecycle:
  budget: 1.50

output:
  kind: local
  dir: output
  enabled: true
```

### Prompt

- **Source:** `prompt-field-realistic.txt` (committed file, last touched at SHA `a4ba38894af816d594ad9cd833b44bfaf239a791`) — referenced by filename only per project policy.

### Env vars / secret names (names only — never values)

- `RUNWAYML_API_SECRET` — Runway Bearer token; RunwayEngine reads it via the Layer-1 `Bearer` AuthStrategy.

### Region

Runway routes internally; no operator-side region knob in the YAML.

### Capability key

`aa4e492fcccf2e189a0fb6838e6e1b1f2721b7db78764fc78d77a0bfe527f39f` (cached profile at `.kinoforge/_profiles/profiles/aa4e492fcccf2e189a0fb6838e6e1b1f2721b7db78764fc78d77a0bfe527f39f.json`)

### Output artifact

- **Path (published):** `/workspace/output/20260608-212659_runway_gen4.5_Photorealistic-cinem.mp4`
- **Path (internal cache):** `/workspace/.kinoforge/run-20260608-212521/ffd59e8abc2d1c56.mp4`
- **File size:** 3,472,252 bytes (3.31 MiB)
- **Container / codec:** `mov,mp4,m4a,3gp,3g2,mj2` (MP4 / ISO BMFF) / `h264`
- **Resolution:** 1280×720
- **Duration:** 5.041667 s
- **Frame count:** 121
- **Average frame rate:** 24/1 (24 fps)
- **Bit rate:** 5,509,688 bit/s (~5.51 Mbit/s)

### Cost

- **Total:** ~$1.25 estimated.
- **Formula:** Runway's per-prediction billing for `gen4.5` at 1280×720 / 5 s. Rate ≈ $0.25/s × 5 s = $1.25, matching the per-clip cost observed during Phase 43 Task 12's first live run (`f20a70d` bug trail).
- **Wall-clock end-to-end:** 100 s (submit → artifact saved). Notably faster than Phase 43's ~2 m 40 s — likely a quieter queue at this time of day rather than an engine speed-up.

### Success criterion

Pending operator visual confirmation. Artifact is valid h264/MP4 at the requested 1280×720 and 5 s / 121-frame duration is consistent with `params.duration: 5` + `params.ratio: "1280:720"`. ISO-BMFF magic confirms it's MP4 (resolves the Layer-4 `.bin` extension quirk by virtue of the OutputSink now picking the right extension from the URL path).

### Failure modes encountered before success

None this run. (Historical Layer-4 bugs documented in PROGRESS.md Phase 43, commit `f20a70d` — Runway's 403-for-everything error mode, missing-prompt segment fallback, `.bin` extension on artifact, etc.)

### Notes

- Same `live-hosted` pixi feature env requirement as the Replicate entry above.
- Artifact filename's `.mp4` extension (vs the Phase 43 `.bin`) confirms the Phase 43 `f20a70d` fix is still in effect — the OutputSink derives the extension from `urlparse(url).path` basename.

---

## 4. `2026-06-08 22:28:40` — ComfyUI Wan 2.1 14B i2v on RunPod — i2v

| Field | Value |
|---|---|
| **Stack triple** | `RunPod / ComfyUIEngine / Kijai Wan2_1-I2V-14B-480P_fp8_e4m3fn` |
| **Mode** | i2v |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `5fcfb9cf2810e3eb039e1fee94f5dbde025eb488` |
| **Date (local TZ)** | 2026-06-08 22:28:40 -0700 (PDT) |
| **Layer / phase** | Phase 47 (Layer 7) — ComfyUI RunPod proxy 404 fix; supersedes Phase 46 Task 7 carry-forward (deferred) |

### Exact command

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v -s
```

(End-to-end smoke harness because the orchestrator path requires a pre-warmed `JsonProfileCache` for the kijai workflow's i2v probe shape; the test sets it up around `orchestrator.generate()` rather than invoking the CLI. Equivalent CLI command would be `pixi run kinoforge generate --config examples/configs/runpod-comfyui-wan-2_1-14b-i2v.yaml --mode i2v --prompt "..." --init-image tests/providers/fixtures/runpod/sample_init_frame.png` but the CLI doesn't yet expose `--init-image`; tracked as a separate UX follow-up.)

### YAML config(s)

**`examples/configs/runpod-comfyui-wan-2_1-14b-i2v.yaml`** at SHA `8aa7ae92d3d447598c476d977bf4fb0e835cc102` — see file at that ref for the kijai custom-node pins, Wan 2.1 14B / VAE / T5 / CLIP-vision models, RunPod compute block (`max_usd_per_hr: 0.50`, RTX 4090 → A5000 → 3090 preference), and 25/15/5/50/30-min lifecycle.

### Prompt

- **Source:** Inlined in the test (`tests/live/test_comfyui_wan_live.py`) — a short init-frame-aligned cat description rather than `prompt-field-realistic.txt`. Rationale captured in commit `5a6b34c2`: the canonical long-form alpine-meadow prompt fights the white-cat init image (re-introduces the cat-turns-into-woman morph). Same-tuple repeats with the canonical prompt land here as "See also" lines.

### Env vars / secret names (names only — never values)

- `RUNPOD_API_KEY` — RunPod GraphQL main key; reads pod offers, creates / lists / destroys pods.
- `RUNPOD_TERMINATE_KEY` — Scoped self-terminate Bearer key; embedded in the pod's `KINOFORGE_SELFTERM_SCRIPT` dead-man watchdog.
- `HF_TOKEN` — Hugging Face gated-repo Bearer token; passed via `Authorization: Bearer $HF_TOKEN` into the pod's curl-bootstrap weight downloader for `Kijai/WanVideo_comfy` (gated).

### Region

RunPod cloud assigned, `cloudType: ALL` in the create-pod mutation; no operator-side knob. Pod `7tfkwgtyf83gr2` landed on RTX A5000 (4090 capacity unavailable at submit time, fell back per the `gpu_preference` list).

### Capability key

`a771bb678238aba6cd650c7af96924cceb248980bc3ce9c43ba861e08ba1d84b` (in-memory pre-warmed by the test via `JsonProfileCache.warm`; persistence to `.kinoforge/_profiles/` is bypassed because the test injects its own cache instance and never calls `_persist`).

### Output artifact

- **Path (published):** `/workspace/output/20260608-222840_comfyui_unknown_A-photorealistic-clo.mp4`
- **Path (internal cache):** `/workspace/.kinoforge/artifacts/run/47b3eb01950ff084.mp4`
- **File size:** 964,470 bytes (942 KiB)
- **SHA-256:** `47b3eb01950ff0842b7f451e564e573e50f96a8c76e6e13b78f431cf69d01e35`
- **Container / codec:** `mov,mp4,m4a,3gp,3g2,mj2` (MP4 / ISO BMFF) / `h264`
- **Resolution:** 624×624 (Wan VAE upscaled the 480×480 request — kijai workflow's `ImageResizeKJv2` node 68 picks a divisible-by-16 target larger than `params.width × params.height`)
- **Duration:** 5.0625 s
- **Frame count:** 81
- **Average frame rate:** 16/1 (16 fps)
- **Bit rate:** 1,512,996 bit/s (~1.51 Mbit/s)

### Cost

- **Total:** ~$0.29 estimated.
- **Formula:** RunPod RTX A5000 spot at $0.16/hr × 1,523.85 s wall (≈25 m 24 s) ≈ $0.068 in compute; plus the 4090-capacity offer-retry round-trip and a small idle window during selfterm; rounded to ~$0.30 for safety.
- **Wall-clock end-to-end:** 25 m 24 s (provision + weight download + ComfyUI cold-start + Wan 14B inference + VAE decode + result fetch).

### Success criterion

Pending operator visual confirmation. Artifact is valid h264/MP4 at 624×624 with 81 frames @ 16 fps, matching the kijai workflow contract (`params.num_frames: 81`, `params.fps: 16`). ISO-BMFF magic confirmed. SHA-256 verified.

### Failure modes encountered before success

The Phase 46 Task 7 carry-forward (`/history/{id}` 404) and the immediately-prior attempt's `/upload/image` 404 (pod `xawdweboxapubz`) both surfaced as bare `urllib.error.HTTPError: HTTP Error 404` with no URL, no poll index, no body. Root-cause investigation (Phase 47, commit `5fcfb9cf`):

- ComfyUI 0.3.10's `/history/{prompt_id}` and `/upload/image` routes never return 404 themselves (verified in upstream `server.py`).
- Live probe of pod `xawdweboxapubz` while still warm: 50/50 sequential POSTs to `/upload/image` returned 200; the earlier 404 was a transient RunPod-proxy startup-window failure mode.
- Fix shipped: `_retry_proxy_call` helper wraps both submit POSTs (1+2+4+8+16+16 s backoff = ~47 s); `result()` poll-body continues on transient 404 and surfaces the last transient on `_MAX_POLL` exhaustion instead of swallowing it as `TimeoutError`.

No 404 retries fired during this green run — pod warmed past the proxy window before submit attempted; the fix is defensive coverage for the race rather than a bug-trigger in the smoke itself.

### Notes

- `LocalOutputSink` renders the `model` slug as `unknown` in the published filename (`comfyui_unknown_...`) because `cfg.engine.comfyui` doesn't surface a `model` field to the sink. Same provenance defect noted on the Phase 43 fal.ai entry; small Layer-O follow-up, not a generation defect.
- The published filename ends in `.mp4` (vs `.bin`) — ComfyUI engine writes the artifact bytes locally so the sink uses the source file's extension directly.

---

## 5. `2026-06-09 21:19:45` — ComfyUI Wan 2.1 14B t2v on RunPod (in-process warm-reuse, 2 prompts) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `RunPod / ComfyUIEngine / Kijai Wan2_1-T2V-14B_fp8_e4m3fn` |
| **Mode** | t2v |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `36b65caada5d0af21f6ff78acab377d80aec9b7b` |
| **Date (local TZ)** | 2026-06-09 21:19:45 -0700 (PDT) |
| **Layer / phase** | Phase 49 (Wan t2v on RunPod + in-process warm-reuse smoke); sibling of Phase 47's i2v config |

New capability axes vs entry #4:

- **t2v** instead of i2v — text-only conditioning, no init image, no CLIP-vision.
- **Two back-to-back generations on the same pod** — the cold `generate()` returns the orchestrator-created `Instance`; the warm `generate()` receives it back via the `instance=` kwarg and skips `create_instance` + boot poll. Demonstrates the warm-reuse path that PROGRESS B3/B4 (CLI exposure of `LifecycleManager.warm_reuse_or_create`) does not yet provide at the `kinoforge` CLI surface.

### Exact command

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_comfyui_wan_t2v_live.py -v -s
```

(Same harness rationale as entry #4 — orchestrator-level test rather than two CLI invocations, because the warm-reuse path is only reachable in-process today and the JsonProfileCache needs pre-warming with the workflow-specific probe shape. A `kinoforge generate` CLI form would still create a fresh pod on each invocation.)

### YAML config(s)

**`examples/configs/runpod-comfyui-wan-2_1-14b-t2v.yaml`** at SHA `4c6ea68` — derived from `runpod-comfyui-wan-2_1-14b-i2v.yaml` (i2v) with: I2V → T2V diffusion checkpoint (`Wan2_1-T2V-14B_fp8_e4m3fn.safetensors`), no CLIP-vision model entry, no `init_image` asset wiring, lifecycle budget doubled (2.0 → 4.0) and `max_lifetime` extended (50 m → 90 m) to cover two consecutive generations.

**`examples/configs/runpod-comfyui-wan-t2v.graph.json`** — hand-authored from the i2v graph: nodes 58/59/63/65/68 (`LoadImage` / `CLIPVisionLoader` / `WanVideoImageToVideoEncode` / `WanVideoClipVisionEncode` / `ImageResizeKJv2`) dropped; node 80 (`WanVideoEmptyEmbeds` with `width=480`, `height=480`, `num_frames=81`) added to feed `WanVideoSampler.image_embeds`. Graph-shape lockdown lives in `tests/engines/test_comfyui_wan_t2v_graph_shape.py` (6 offline assertions that trip before any RunPod spend).

### Prompts

Read verbatim from the two prompt files at the repo root (per `feedback_standard_test_prompt` — same prompt-substrate the upcoming Bearer-comparison smokes use):

- **Realistic:** `/workspace/prompt-field-realistic.txt` — long-form alpine-meadow cinematic with anamorphic lenses, golden-hour rake light, fae creatures.
- **Dreamlike:** `/workspace/prompt-field-dreamlike.txt` — same scene rendered with jewel-tone bloom, opalescent sky, stained-glass dragonflies — written specifically to exercise prompt-driven style divergence on identical compute.

### Env vars / secret names (names only — never values)

- `RUNPOD_API_KEY` — RunPod GraphQL main key.
- `RUNPOD_TERMINATE_KEY` — Scoped self-terminate Bearer key for the in-pod selfterm watchdog.
- `HF_TOKEN` — Hugging Face gated-repo Bearer token for `Kijai/WanVideo_comfy` weight pulls.
- `KINOFORGE_LIVE_TESTS=1` — global live-mode gate (test skips silently when unset).

### Region

RunPod cloud assigned, `cloudType: ALL`. Pod `1cyd9v4e17ufvc`; GPU type not captured into the smoke fixture (`Instance.tags["gpu_type"]` is set by the orchestrator's tag policy; absent here). Cost arithmetic below assumes the worst-case RTX 4090 to keep the upper bound honest.

### Capability key

`db992da0b751f8e7e76119fd62f5fd5710644facb9fc17fd06ea4536b438367b` (pre-warmed via `JsonProfileCache.warm` in the test, mirroring entry #4). Distinct from entry #4's i2v key because `spec.graph_file` and the asset / model identifiers differ.

### Output artifacts

| # | Prompt | Path (published) | Path (internal cache) | Size | SHA-256 |
|---|---|---|---|---|---|
| 1 | realistic | `/workspace/output/20260609-212621_comfyui_Wan2_1-T2V-14B_fp8_e4m3f_Photorealistic-cinem.mp4` | `/workspace/.kinoforge/artifacts/t2v-realistic/d1f8cfef54439b2c.mp4` | 1,277,945 B (1.22 MiB) | `d1f8cfef54439b2c9695ba464e387b1a419f5808017a6e5c4e8d5f5b0c7eb513` |
| 2 | dreamlike | `/workspace/output/20260609-213053_comfyui_Wan2_1-T2V-14B_fp8_e4m3f_Photorealistic-yet-d.mp4` | `/workspace/.kinoforge/artifacts/t2v-dreamlike/139a7d9c91557efe.mp4` | 1,703,715 B (1.62 MiB) | `139a7d9c91557efea1b8dbd34306ec862977c10b88af09830c773556c667d1d1` |

Common to both:

- **Container / codec:** MP4 / ISO BMFF / h264 High, yuv420p (bt709)
- **Resolution:** 480×480 (matches `params.width`/`height`; no upscale because the t2v graph has no `ImageResizeKJv2` node)
- **Duration:** 5.0625 s
- **Frame count:** 81
- **Average frame rate:** 16/1 (16 fps)
- **Bit rate:** 2,019 kb/s (realistic) / 2,681 kb/s (dreamlike) — content-driven delta, not a pipeline difference

### Cost

- **Total:** ~$0.10 estimated.
- **Wall-clock:**
  - Cold path (provision + ~24 GB weight download + ComfyUI cold-start + gen 1): 402.4 s (≈ 6 m 42 s)
  - Warm path (gen 2 on the same pod, no re-provision, no boot): 271.7 s (≈ 4 m 32 s)
  - Total pod-up time: ≈ 11 m 17 s end-to-end
- **Formula:** worst-case RunPod RTX 4090 spot at ~$0.40/hr × 677 s ≈ $0.075; A5000 fallback at $0.16/hr × 677 s ≈ $0.030. Rounded up to ~$0.10 for safety.
- **Warm-reuse savings:** the second generation would otherwise have paid ~140 s of provision + boot (the cold delta above gen-1 inference), or roughly half its own runtime — proves the path's value even on a single repeat.

### Success criteria

Pending operator visual confirmation. Programmatic checks all pass:

- Both artifacts are valid h264/MP4 at 480×480, 81 frames @ 16 fps (ISO-BMFF `ftyp*` magic verified).
- SHA-256 of the two clips differ — distinct prompts produced distinct bytes, ruling out the "second generation got the first's cached output" failure mode.
- `warm_elapsed (271.7 s) < cold_elapsed (402.4 s)` — proves the warm path skipped re-provision (a regression that re-provisioned would land warm ≥ cold).
- Single `provider.destroy_instance(pod_id)` at the end; finally-arm verifies the pod is gone.

### Failure modes encountered before success

- First run skipped silently because the live-test module-import-time env-var check fired before pixi's `[activation.env]` had any chance to inject the credentials into `os.environ`. Root cause: live tests previously assumed the operator had sourced `.env` in the shell; the loader (`kinoforge.core.dotenv_loader.load_env_file`) was only invoked from `tools/preflight.py`, not from pytest. Fixed by adding `tests/live/conftest.py` (commit `36b65ca`) — a session-scoped, silent, override-`False` dotenv loader. Benefits every live-test module.
- No t2v-specific runtime regressions — the hand-authored graph + `WanVideoEmptyEmbeds` rewiring landed on the first live attempt. The offline `tests/engines/test_comfyui_wan_t2v_graph_shape.py` regression lock catches the structural diff before the next person touches the YAML.

### Notes

- Published filename now reads `comfyui_Wan2_1-T2V-14B_fp8_e4m3f_...` (not `unknown` like entry #4) because Phase 48 / Layer 8 shipped the `model_identity(cfg)` ABC; `ComfyUIEngine.model_identity` returns the `kind: base` model's filename stem. Same fix that closed the entry-#4 carry-forward.
- This entry's "warm-reuse" still goes through the in-process orchestrator harness — no Layer Y CLI exposure yet (PROGRESS B3/B4 remain). A future `kinoforge generate` invocation against the same pod would re-create-and-destroy because the CLI does not consult the ledger for matching live pods.

---

## 6. `2026-06-13 11:16:26` — FakeEngine on RunPod (B3 cross-CLI auto-discovery warm-reuse) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `runpod / FakeEngine / fake-model` |
| **Mode** | t2v |
| **kinoforge version** | `v0.5.0` |
| **First-success SHA** | `3bdec1c` |
| **Date (local TZ)** | 2026-06-13 11:16:26 -0700 (PDT) |
| **Layer / phase** | [B3 — in-session orchestrator warm-reuse retrofit](PROGRESS.md#b3-layer-y--in-session-orchestrator-warm-reuse-retrofit) |
| **New axis** | Cross-CLI auto-discovery warm-reuse via `_scan_warm_candidates` (B3). Gen 2 attaches to Gen 1's pod through the ledger scan + B5a heartbeat substrate, with no operator-supplied `--instance-id`. |

### Exact command

```bash
KINOFORGE_LIVE_RUNPOD=1 KINOFORGE_LIVE_TESTS=1 \
  pixi run pytest tests/live/test_b3_warm_attach_live.py -v -s
```

The smoke runs `pixi run kinoforge --state-dir <tmp>/state generate
-c tests/live/cfg_b3_warm_attach.yaml --prompt
"$(cat prompt-field-realistic.txt)" --mode t2v --run-id b3-smoke-1`
twice, 30 s apart, in separate subprocess CLIs.

### YAML config

**`tests/live/cfg_b3_warm_attach.yaml`** at SHA `3bdec1c`:

```yaml
engine:
  kind: fake
  precision: fp16

models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  warm_reuse_auto_attach: true
  heartbeat_mode: graphql-tag
  requirements:
    min_vram_gb: 8
    min_cuda: "12.0"
    max_usd_per_hr: 0.50
    gpu_preference:
      - "NVIDIA GeForce RTX 4090"
      - "NVIDIA RTX A5000"
      - "NVIDIA GeForce RTX 3090"
    disk_gb: 20
  lifecycle:
    idle_timeout: 5m
    job_timeout: 5m
    time_buffer: 2m
    max_lifetime: 30m
    boot_timeout: 10m
    budget: 1.0
    heartbeat_interval_s: 30
```

### Measured outcome

- **Pod id**: `7bvm89fywnr05r` (RTX A5000 — GeForce 4090 had no current
  capacity → offer-retry to A5000).
- **Gen 1 (cold create + first-tick poll + fake gen + ledger.record)**:
  11.3 s wall.
- **Gen 2 (B3 auto-discovery scan hit + caller-supplied attach + fake
  gen)**: 2.9 s wall.
- **Cold-skip benefit**: 8.4 s (74 % wall-time reduction; ratio 0.26
  well under the 0.7 pass-threshold).
- **Total live spend**: $0.0040 RunPod (per `kinoforge destroy`
  `est_spend` readout).
- **Pod-id continuity**: `pod_id_1 == pod_id_2` (B3 scan attached
  cleanly).
- **Log line confirmed**: `warm-reuse: attached to 7bvm89fywnr05r`.
- **Cleanup**: explicit `kinoforge destroy --id 7bvm89fywnr05r`
  succeeded; pod gone from RunPod GraphQL `myself.pods`.

### Mid-task production fixes folded back

Two gaps surfaced during the live smoke iteration, both committed
before the closeout:

- **`3454b48`** — `_cmd_generate` ledger-record. Prior to this fix,
  cold-created instances were never recorded by the generate path
  (only `_cmd_deploy` recorded), so the cross-CLI scan saw an empty
  ledger and every fresh-shell invocation cold-created a new pod.
- **`3bdec1c`** — `_record_then_install` callback. Prior to this fix,
  `HeartbeatLoop.touch` no-oped (strict update) for unrecorded
  instances, so `heartbeat_thread_tick` never landed and
  `hold_until_first_tick` polled forever to `FirstTickTimeout`. Wired
  ledger.record into the existing `on_instance_created` callback so it
  runs BEFORE the holder enters first-tick polling.

Both gaps were invisible to unit tests because B7's spy HB loop
pre-records the instance in `spy.start()`, masking the real-world
strict-update behavior.

### Production limitation (C25)

The B3 production path for `ComfyUI + Wan` on RunPod remains gated by
C25 (heartbeat carrier `dockerArgs` collides with selfterm injection).
This smoke uses FakeEngine (the only HB-safe engine on RunPod per
`_RUNPOD_HEARTBEAT_SAFE_ENGINES`) to validate B3 mechanics end-to-end
on real cloud without exercising the workload — the workload itself
is independently tested via the existing Wan 2.1 14B t2v live suite
(entry #5 above).

Operators wanting B3 warm-reuse with ComfyUI today must use
`--force-attach` on `kinoforge generate --instance-id <id>` to bypass
HEARTBEAT_UNKNOWN classification, or wait for C25's preserve-and-merge
wire path to land.

---

## 7. `2026-06-18 22:05:08` — ComfyUI Wan 2.1 1.3B t2v on RunPod (CLI cross-invocation warm-reuse, real engine) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `RunPod / ComfyUIEngine / Kijai Wan2_1-T2V-1_3B_fp8_e4m3fn` |
| **Mode** | t2v |
| **kinoforge version** | `v0.5.0` |
| **First-success SHA** | `7050ffc` |
| **Date (local TZ)** | 2026-06-18 22:05:08 -0700 (PDT) |
| **Layer / phase** | B3 + B4 cross-CLI auto-discovery warm-reuse on a **real video engine** (ComfyUI + Wan), 4 mid-task production fixes folded in |
| **New axes vs prior entries** | (a) Wan 2.1 **1.3B** variant (prior Wan entries were 14B); (b) **CLI cross-invocation warm-reuse with a real video engine** — entry #6 demonstrated B3 mechanics with FakeEngine only because ComfyUI + Wan was gated by C25; entry #5 demonstrated Wan warm-reuse in-process only. This run is the first time `kinoforge generate ... && kinoforge generate ...` (two CLI invocations, same cfg, different prompts, no `--instance-id`) actually attaches the second invocation to the first invocation's pod with a real video workload. |

### Exact commands

The two commands were identical except for the prompt file. The
operator pastes both, one after the other; no `--instance-id`
lookup, no `--force-attach`, no manual ledger inspection between.

```bash
pixi run kinoforge generate \
  --config examples/configs/runpod-comfyui-wan-2_1-1_3b-t2v.yaml \
  --prompt "$(cat examples/configs/prompts/forest.txt)" \
  --mode t2v
```

```bash
pixi run kinoforge generate \
  --config examples/configs/runpod-comfyui-wan-2_1-1_3b-t2v.yaml \
  --prompt "$(cat examples/configs/prompts/dawn-flight.md)" \
  --mode t2v
```

The CLI auto-loads `/workspace/.env` for `RUNPOD_API_KEY`,
`RUNPOD_TERMINATE_KEY`, and `HF_TOKEN`; no shell exports needed.

### YAML config

**`examples/configs/runpod-comfyui-wan-2_1-1_3b-t2v.yaml`** at blob SHA
`61f158cc30fee00d7c234eca5037a5733e28d8e7` — sibling of the 14B t2v
cfg with the diffusion checkpoint swapped to
`Wan2_1-T2V-1_3B_fp8_e4m3fn.safetensors` (1.47 GB vs 17 GB), VAE +
T5 unchanged, `min_vram_gb: 16` (vs 24), `disk_gb: 40` (vs 80),
`max_lifetime: 60m`. Includes the load-bearing
`lifecycle.heartbeat_interval_s: 30` — without it the
`HeartbeatLoop` never starts, no `heartbeat_thread_tick` lands in
the ledger row, and the second invocation's classify chain returns
`HEARTBEAT_UNKNOWN` → conservative-on-ignorance → cold create
(empirically observed during this smoke's first attempt, pods
`f2w4sqghw5udio` + `p3oj1qjmjioae1` ran simultaneously until manual
destroy at ~$0.04 combined spend; cfg comment added in `7b93725`).

**`examples/configs/runpod-comfyui-wan-t2v-1_3b.graph.json`** —
hand-derived from the 14B t2v graph by swapping only node 22's
`model` field; ComfyUI workflow topology unchanged.

### Prompts

Both read verbatim from committed prompt files at
`examples/configs/prompts/` per the operator's standing
"never paraphrase live-smoke prompts" rule:

- **Forest (cmd 1, cold path):** `examples/configs/prompts/forest.txt`
  — *"A dense old-growth forest at first light. Mist coils between
  the trunks, backlit by a low golden sun. Camera drifts slowly
  forward through the underbrush; ferns brush the lens; a single
  shaft of light pierces the canopy."*
- **Dawn flight (cmd 2, warm path):** `examples/configs/prompts/dawn-flight.md`
  — *"Aerial drone shot at dawn. The camera lifts off the surface of
  a still lake, water beading on the lens, then climbs above a
  ridge line as the first sun strikes the peaks. The horizon glows
  orange-pink. Slow forward motion. Cinematic, photoreal."*

### Env vars / secret names (names only — never values)

- `RUNPOD_API_KEY` — RunPod GraphQL main key.
- `RUNPOD_TERMINATE_KEY` — Scoped self-terminate Bearer for the in-pod selfterm.
- `HF_TOKEN` — Hugging Face gated-repo Bearer for the `Kijai/WanVideo_comfy` pull.

### Region

RunPod cloud assigned, `cloudType: ALL`. Pod `qswjrs5ehzkwr8`
landed on RTX A5000 (4090 capacity unavailable at submit time; offer
retry fell back per the `gpu_preference` list). Ledger
`cost_rate_usd_per_hr: 0.22`.

### Capability key

`cdcd2a98cbf2` (12-char hash; full sha256 derived from
`[base_model, loras, engine, precision]` per
`src/kinoforge/core/interfaces.py:CapabilityKey.derive`). Both
invocations produced the same key because the cfg is byte-identical
between calls — that's the load-bearing invariant that makes
`_scan_warm_candidates` find the pod.

### Output artifacts

| # | Prompt | Path (published) | Path (internal cache) | Size | SHA-256 |
|---|---|---|---|---|---|
| 1 | forest (cold) | `/workspace/output/20260618-220508_comfyui_Wan2_1-T2V-1_3B_fp8_e4m3_A-dense-old-growth-f.mp4` | `/workspace/.kinoforge/run-20260618-220208/1b6f9cd040c9065f.mp4` | 861,361 B (841 KiB) | `1b6f9cd040c9065fec44edf7deca7f1d335e86ecfe4bbc5272d40500863ac7be` |
| 2 | dawn-flight (warm) | `/workspace/output/20260618-220704_comfyui_Wan2_1-T2V-1_3B_fp8_e4m3_Aerial-drone-shot-at.mp4` | `/workspace/.kinoforge/run-20260618-220525/7fa95bd1ed3dac10.mp4` | 264,600 B (258 KiB) | `7fa95bd1ed3dac104ce80d63d70c9c38396eb282122b152e6db024c6fc77ecf4` |

Common to both (from cfg `params:` block — `ffprobe` is not
installed in this env so dimensions are reported per the workflow
contract rather than re-read from the file headers; the same kijai
T2V graph drives both runs, so the contract values apply):

- **Container / codec:** MP4 / ISO BMFF / h264 (kijai VHS_VideoCombine output node).
- **Resolution:** 480×480 (matches `params.width` / `params.height`).
- **Duration:** 5.0625 s.
- **Frame count:** 81 (`params.num_frames`).
- **Average frame rate:** 16/1 fps (`params.fps`).

The two clips' SHA-256 hashes differ — distinct prompts produced
distinct bytes. Rules out the "second generation got the first's
cached output" failure mode.

### Cost

- **Total:** ~$0.03 estimated.
- **Wall-clock:**
  - **Gen 1 (cold path: provision + ~9 GB weight download + ComfyUI
    cold-start + Wan 1.3B inference + VAE decode + result fetch):**
    181 s wall (3 m 01 s).
  - **Gen 2 (warm path: ledger scan + classify LIVE + attach +
    inference + VAE decode + result fetch):** 101 s wall (1 m 41 s).
  - **Total pod-up time:** ~5 m 22 s end-to-end on RunPod side.
- **Formula:** RTX A5000 at $0.22/hr × ~5 m 22 s ≈ $0.020 in
  compute, plus the 4090-capacity offer-retry round-trip and a
  small idle window. Ledger `est_spend` readout at destroy time:
  $0.0186.
- **Warm-reuse savings:** Gen 2 saved 80 s of cold-boot wall and
  ~$0.005 of compute that would otherwise have re-provisioned a
  fresh pod. Cold-skip ratio: `101 / 181 = 0.56` (well under the
  0.7 pass threshold the B3 smoke suite uses to assert real
  attach-vs-recreate).

### Success criteria

Operator visual confirmation pending. Programmatic checks all pass:

- Both artifacts are valid MP4 files written by the kijai
  VHS_VideoCombine output node (the workflow has no other
  artifact-producing terminator).
- SHA-256 of the two clips differ — distinct prompts produced
  distinct bytes.
- `warm_elapsed (101 s) < cold_elapsed (181 s)` — proves the warm
  path skipped re-provision.
- **Log line `INFO kinoforge.cli._commands warm-reuse: attached to
  qswjrs5ehzkwr8`** fires on cmd 2 immediately after the ledger
  scan, before any engine call. This is the canonical "warm-reuse
  actually happened" assertion.
- `pod_id_1 == pod_id_2 == qswjrs5ehzkwr8` (B4 scan attached
  cleanly to the cold-create pod).
- Single `kinoforge destroy --id qswjrs5ehzkwr8` at the end;
  `pixi run preflight` confirms `pods: 0 active`.

### Failure modes encountered before success (mid-task production fixes folded in)

This smoke surfaced FOUR independent production bugs that had to
ship before the warm-reuse path completed end-to-end. All four are
captured in separate atomic commits BEFORE the warm-reuse smoke
re-fired; the smoke is the verification artefact, not the
discovery one.

1. **`6f008b8` cfg scaffold (Wan 1.3B sibling).** No bug per se;
   the RED scaffold required by the CLAUDE.md durability rule. The
   1.3B variant didn't exist as a published cfg yet.

2. **`05fc93d` Stage E image-placeholder fix (carried over).**
   Pre-existing from earlier in the session — not a fresh bug.

3. **`7b93725` cfg fix: add `heartbeat_interval_s: 30` to the 1.3B
   cfg.** First smoke attempt cold-created TWO pods running
   simultaneously (`f2w4sqghw5udio` + `p3oj1qjmjioae1`) because
   the cfg lacked `heartbeat_interval_s`. Without it the
   `HeartbeatLoop` never starts, no `heartbeat_thread_tick` lands
   in the ledger row, and cmd 2's classify chain returns
   `HEARTBEAT_UNKNOWN` → conservative-on-ignorance → cold create.
   UX gap captured separately: CLI should warn when
   `warm_reuse_auto_attach: true` is set without
   `heartbeat_interval_s`. Filed as a PROGRESS follow-up.

4. **`be33a67` HeartbeatLoop last_heartbeat fallback.** Second
   attempt still failed `HEARTBEAT_UNKNOWN` even with the
   `heartbeat_interval_s` cfg in place. Root cause: post-C33,
   `RunPodGraphQLHeartbeatEndpoint.write()` is permanently
   disabled, so the wire-level read endpoint returns `None` on
   every tick. `HeartbeatLoop._tick_once` was sourcing
   `last_heartbeat` from that broken wire path, writing `None`
   to the ledger, which `Ledger.touch` SKIPS. Ledger row never
   gets `last_heartbeat`. Reaper `classify` returns
   `HEARTBEAT_UNKNOWN`. Fix: fall back to `self._clock.now()`
   when the provider returns `None`, per the B5b deferral spec's
   "local ledger is the same-host substrate" conclusion. Also
   filled a documented hole in the B5b deferral spec.

5. **`7050ffc` `_resolve_warm_instance` endpoint rehydration.**
   Third attempt finally activated warm-reuse (cmd 2 logged
   `warm-reuse: attached to di506yuuczuhht`) but immediately
   aborted with `ProvisionFailed: pod 'di506yuuczuhht' has no
   endpoints — cannot construct ready URL`. Root cause:
   `_resolve_warm_instance` returned the impoverished Instance
   from `provider.get_instance` verbatim (RunPod's
   `_pod_to_instance` strips endpoints + sparse tags). Same
   Instance-impoverishment family as the earlier `e33d564`
   orchestrator polling-loop fix. Patch merges ledger tags onto
   the provider-fresh instance and calls `provider.endpoints` to
   reconstruct the proxy URL dict (network-free, deterministic).
   This was the closing patch — fourth-attempt smoke fired green.

Total spend across all four attempts: ~$0.12 (well under the $20
session budget envelope). Three pods were destroyed mid-debug
during the diagnostic trail (`f2w4sqghw5udio`, `p3oj1qjmjioae1`,
`252wqr84clzhlg`, `di506yuuczuhht`) before the green smoke landed
on `qswjrs5ehzkwr8`.

### Notes

- This is the FIRST time the operator's "two identical commands
  trigger warm-reuse" UX guarantee actually works end-to-end on a
  real video workload. Prior to this entry, entry #5 demonstrated
  Wan warm-reuse in-process only (single Python process), and
  entry #6 demonstrated CLI cross-invocation warm-reuse with
  FakeEngine only (ComfyUI + Wan was gated by C25 at the time).
- Two of the four bugs (`be33a67`, `7050ffc`) only ship test
  fences against the same root-cause family identified in
  `e33d564` — the gap where provider `get_instance` returns an
  impoverished Instance and consumers expect the rich
  create-time fields. The next provider added to the registry
  should pre-emptively audit this surface.
- The `warm-reuse: attached to <pod_id>` log line is the
  canonical operator-facing signal. Future smokes (and any
  CI-style verification) should grep for that exact string
  rather than wall-time deltas, which are noise-prone on
  fluctuating RunPod GPU loads.
- 🎉 **Warm reuse actually works.**

---

## 8. `2026-06-20 05:58:23` — Diffusers WanPipeline Wan 2.2 T2V-A14B on RunPod (A100 80GB) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `runpod / DiffusersEngine / Wan-AI/Wan2.2-T2V-A14B-Diffusers` |
| **Mode** | t2v |
| **kinoforge version** | branch `worktree-wan22-native-t2v-a14b` |
| **First-success SHA** | `365ab00ad1f3e10c80d52e7ae4d3793116c1ed94` |
| **Date (local TZ)** | 2026-06-20 05:58:23 -0700 (PDT) |
| **Layer / phase** | Wan 2.2 native T2V-A14B (plan: `docs/superpowers/plans/2026-06-19-wan22-native-t2v-a14b.md`, Task 8) |

### Exact command

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest \
  tests/live/test_diffusers_wan_t2v_live.py -v -s
```

The pytest harness runs three sequential `pixi run kinoforge generate`
invocations (14B cold, 14B warm reuse, 5B cross-cap-key) — the first
two land entry #8; the 5B leg is the Kijai ComfyUI 5B already
documented under entry #5 lineage.

### YAML config

**`examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml`** at SHA `365ab00`:

```yaml
engine:
  kind: diffusers
  precision: bf16
  diffusers:
    image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    base_url: "http://localhost:8000"
    prompt_body_key: "prompt"
    embed_modules:
      - "kinoforge.engines.diffusers.servers"

models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers"
    kind: base
    target: checkpoints

compute:
  provider: runpod
  mode: pod
  warm_reuse_auto_attach: true
  requirements:
    min_vram_gb: 80
    min_cuda: "12.4"
    max_usd_per_hr: 3.00
    gpu_preference:
      - "NVIDIA A100 80GB PCIe"
      - "NVIDIA A100-SXM4-80GB"
      - "NVIDIA H100 80GB HBM3"
      - "NVIDIA H100 PCIe"
    disk_gb: 150
  lifecycle:
    idle_timeout: 60m
    job_timeout: 15m
    time_buffer: 3m
    max_lifetime: 180m
    boot_timeout: 60m
    budget: 5.0
    heartbeat_interval_s: 30

spec:
  pipeline: "WanPipeline"
  scheduler: "UniPCMultistepScheduler"
  width: 480
  height: 480
  num_frames: 81
  fps: 16
```

### Prompts

- Cold (Leg 1): `examples/configs/prompts/field-realistic.txt`
- Warm reuse (Leg 2): `examples/configs/prompts/field-dreamlike.txt`

### Env vars

`HF_TOKEN` required (declared in `DiffusersEngine.render_provision`'s
`env_required` so the orchestrator lifts it from `.env` and the
RunPod provider injects it into the pod's env). Without it, the
unauthenticated HF Hub rate-limit stalled downloads at 3/41 files in
attempt #9.

### Region

RunPod auto-select (no explicit cloud-region pin in the cfg). Both
green pods (`7o0p1pyvbfpbr8` for 14B, `ldcejjob13kh9z` for 5B) were
A100 80GB PCIe machines.

### Capability key

`5dff86b4f44e` (14B, derived from
`hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers` + DiffusersEngine + t2v mode).
Different from the Kijai 5B comfyui cfg's cap_key `c72657314e92`,
which is what the cross-cap-key leg 3 verifies — the orchestrator
cold-creates a fresh pod for the 5B cfg rather than warm-reusing the
14B pod.

### Output artifact

**Original 28-attempt smoke (commit `365ab00`, 2026-06-20 05:58:23):**
all three artifacts were DELETED immediately after the merge — the
operator's `git worktree remove` (Step 6 of
`finishing-a-development-branch`) wiped the worktree's `output/` dir
before the bytes were copied to a stable path. Programmatic
assertions on those bytes (h264 ftyp magic, size ≥ 100 KB, pairwise
sha256 distinctness) passed at smoke time per the pytest log; bytes
themselves are unrecoverable. The deleted paths were:

- `/workspace/.claude/worktrees/wan22-native-t2v-a14b/output/20260620-055823_diffusers_unknown_Photorealistic-cinem.mp4` (14B cold, field-realistic, 1.1 MB)
- `/workspace/.claude/worktrees/wan22-native-t2v-a14b/output/20260620-060158_diffusers_unknown_Photorealistic-yet-d.mp4` (14B warm reuse, field-dreamlike, 1.9 MB)
- `/workspace/.claude/worktrees/wan22-native-t2v-a14b/output/20260620-060729_comfyui_Wan2_2-TI2V-5B-FastWanFu_Photorealistic-cinem.mp4` (5B cross-cap-key cold, field-realistic, 1.3 MB)

The slug `_unknown_` on the two 14B clips comes from a missing
`spec.model` key in the cfg at smoke time (fixed in commit
`57c5f3b` — the cfg now sets `model: "Wan2.2-T2V-A14B-Diffusers"`).

**Surviving evidence for the same `(runpod, DiffusersEngine, Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)` tuple
comes from the 2026-06-20 12:24:49 4-prompt warm-reuse re-fire** at
HEAD `085781e` (commit `90e588f` "See also" entry above). Four MP4s
land in `/workspace/output/` AND in `/workspace/.kinoforge/wan22_4prompt_evidence/`
(stable evidence dir, copied by the test harness before teardown so
the bytes survive any future `git clean` / worktree removal). Each
ffprobe-verified h264 / yuv420p / 480×480 / 81 frames / 16 fps /
5.0625 s. Per-prompt sha256 + size in the See-also table above.

**5B cross-cap-key leg from the original smoke is NOT re-verified
visually.** The pytest assertion that the 5B leg cold-created pod
`ldcejjob13kh9z` with cap_key `c72657314e92` (distinct from the 14B
cap_key `5dff86b4f44e`) and that no warm-reuse log fired on the 5B
invocation both passed — proving the cross-cap-key isolation
mechanism — but the 5B MP4 bytes themselves are gone. The 5B cfg
(`runpod-comfyui-wan-2_2-5b-t2v.yaml`) is the Kijai/ComfyUI path
(separate engine from this entry's DiffusersEngine path) and any
future re-fire would land under its own entry.

### Cost

Green-attempt pod spend:
- 14B pod `7o0p1pyvbfpbr8` (A100 80GB PCIe, ~$1.39/hr): $0.47 (~20 min cold boot + ~3.5 min × 2 generations).
- 5B pod `ldcejjob13kh9z`: $0.02 (~1 min — already-cached image + tiny model).

Total green run: **~$0.49**.

Debug trail (28 attempts before green): ~$10 across pods burned on
the bugs enumerated in the Notes section. All within the user's
pre-authorized $20 session ceiling.

### Success criterion

`pytest` exit 0 — all three legs (14B cold, 14B warm reuse, 5B
cross-cap-key) produced MP4s; the warm-reuse leg's pod_id matched
the cold leg's; the 5B leg's pod_id differed; the 14B cold/warm
output sha256s differ (distinct prompts → distinct bytes).

### Failure modes

Each of these is hardwired into a commit on the worktree branch.
Future regression of any of them would fail tests on the same line.

- **Local-download waste** (refs_to_stage refactor, commit `ecbaa5b`).
  DiffusersEngine's `requires_local_weights = True` triggered a
  ~70 GB workspace-side aria2c download for the bare-repo HF ref —
  but `render_provision` never shipped those bytes to the pod, so
  the pod's `wan_t2v_server` re-downloaded everything. Net waste:
  ~$0.50-0.85 of pod-idle/leg. Fixed by adding
  `engine.refs_to_stage(merged) -> list[Artifact]` to both ABCs,
  defaulting to honor the legacy boolean. DiffusersEngine overrides
  to return `[]`.
- **pip arg redirect bug** (commit `6ad3bfa`). The bootstrap rendered
  `pip install -q diffusers>=0.32 transformers>=4.45 ...` unquoted.
  Bash with `set -euo pipefail` parsed each `>=` as a stdout redirect
  to a file named `=0.32` etc, silently stripping every version pin.
  Fixed by `shlex.quote`-ing each dep.
- **Log surface absence** (commits `2eeb1d4` + `b86bf4f`). On a boot
  failure the container died before any error message left the pod.
  Fixed by (1) redirecting bootstrap stdout/stderr to
  `/tmp/bootstrap.log`, (2) backgrounding `python3 -m http.server
  8001 --directory /tmp` so the file is reachable via the RunPod
  port-8001 proxy, (3) installing an EXIT trap that runs `sleep
  infinity` so PID 1 (bash) stays alive on failure — selfterm and
  the `max_lifetime` cap still fire.
- **HF repo layout** (commit `34ef018`). Pointed at the bare
  `Wan-AI/Wan2.2-T2V-A14B` repo — native Wan-AI checkpoint layout,
  no `model_index.json` at root, `from_pretrained` 404s. Switched
  to the `-Diffusers` variant.
- **HF xet transport bug** (commit `299b587`). xet's "Background
  writer channel closed" error during the 70 GB shard download.
  Fixed by `HF_HUB_DISABLE_XET=1` + `HF_HOME=/workspace/.hf_cache`
  set at the top of `wan_t2v_server.py` before any HF import.
- **HF anonymous rate-limit** (commit `7d1d271`). Without
  `HF_TOKEN`, the download stalled at 3/41 files. Fixed by adding
  `HF_TOKEN` to `env_required` so the orchestrator lifts it from
  creds and the RunPod provider injects it into the pod env.
- **Container disk too small** (commit `898e4ad`). 50 GB hardcoded
  `containerDiskInGb` ran out at the 18th shard. Bumped to 250 GB.
- **Boot timeout too short** (commit `ab96ecc`). 25m
  `boot_timeout` truncated the ~30-minute download. Bumped to 60m.
- **Subprocess timeout < boot_timeout** (commit `21bcd7a`). Test's
  2400s subprocess SIGKILL fired before the 60m orchestrator
  timeout could surface a real failure. Bumped to 3900s.
- **GPU too small** (commit `f54c64d`). Wan 2.2 MoE has TWO 14B
  transformers (high_noise + low_noise) + 11 GB UMT5-XXL. Total
  ~70 GB which doesn't fit on a 48 GB A40/A6000/L40S. Switched
  gpu_preference to A100 80GB / H100 80GB.
- **CPU RAM OOM at shard load** (commit `d0c1cd1`). Even with 80 GB
  GPU, diffusers' default loader staged all shards in CPU RAM
  before moving to GPU — pod CPU RAM (variable 15-50 GB per
  machine) OOM-killed at 8/12 shards. Fixed by
  `WanPipeline.from_pretrained(..., device_map="cuda")` so weights
  stream directly to GPU and skip CPU.
- **Cloudflare 403 against urllib UA** (commit `2e07fe5`). RunPod's
  Cloudflare edge returned 403 to `Python-urllib/3.13`. Fixed by
  setting `User-Agent: kinoforge-diffusers/0.1` on every
  `urllib.request.Request` in the engine.
- **validate_spec required-keys gap** (commit `19e08ca`).
  DiffusersEngine.validate_spec required `pipeline` and `scheduler`
  on `job.spec` but the cfg's spec block didn't set them. Added as
  placeholder strings.
- **Backend base_url localhost** (commit `4d6d3df`).
  `DiffusersEngine.backend` discarded its `instance` argument and
  always used `cfg.engine.diffusers.base_url`
  (`http://localhost:8000`). For remote pods, that pointed at the
  workspace container. Fixed by deriving base_url from
  `instance.endpoints[port]` when remote.
- **_MAX_POLL too small for video** (commit `7bee919`). 60 polls × 1 s
  = 60 seconds of patience, ~5-10 min Wan generation. Bumped to
  1800.
- **Artifact URL hardcoded localhost** (commit `a8841d5`). Server's
  `/status` returned `url: http://localhost:8000/artifacts/X.mp4`.
  Fixed by ignoring the server-supplied URL and rebuilding from
  `self._base_url` in `DiffusersBackend.result`.
- **Teardown skipped on failure** (commit `365ab00`). `_run_generate`
  raised on subprocess rc != 0 before the test's `pod_14b` /
  `pod_5b` assignments ran, leaking pods to the next preflight.
  Fixed by re-extracting pod_ids from per-leg log files in
  `finally`.

### Notes

- This is the first kinoforge integration with the `diffusers`
  Python library directly — prior diffusers-shaped work all ran
  through `ComfyUIEngine` (entries #4, #5, #7) which talks to the
  ComfyUI HTTP API. This entry uses
  `diffusers.WanPipeline.from_pretrained` inline inside a
  custom `wan_t2v_server.py` FastAPI app embedded into the pod's
  bootstrap.
- Two transformers (high_noise_model, low_noise_model) loaded by
  `WanPipeline` form the MoE that gives Wan 2.2 14B its quality
  bump over Wan 2.1 14B at the same parameter count per expert.
  This is why the 80 GB GPU requirement is non-negotiable in bf16.
- Switching the cfg to `image: runpod/pytorch:2.4` + pip-installing
  `torch==2.6.0` (cu124 wheels) is the recommended pattern for any
  future kinoforge cfg that needs a torch version newer than what
  RunPod's stock images ship — RunPod's newer-torch images (e.g.
  `2.8.0-cudnn-devel`) have unreliable distribution across the
  machine pool (3 consecutive image-pull stalls during this debug).
- The `device_map="cuda"` knob is the canonical mechanism for
  streaming weights to GPU without staging through CPU RAM. Future
  large-model diffusers cfgs (Cosmos, HunyuanVideo, etc.) should
  use the same pattern.
- The `--extra-index-url https://download.pytorch.org/whl/cu124`
  pattern in `DiffusersEngine.render_provision` is generic: any
  diffusers cfg that needs a specific torch version can pin it in
  `engine.diffusers.pip` and the bootstrap will resolve from the
  PyTorch wheel index.
- The `kinoforge logs --id <pod>` CLI command is deferred — for now
  the manual incantation
  `curl https://<pod_id>-8001.proxy.runpod.net/bootstrap.log` works
  and turned the smoke debug from a guessing game into a tractable
  fix sequence.
- 28 attempts to reach this entry. Each surfaced exactly one new
  bug; each bug was fixed with a focused commit + at least one
  test. The Wan 2.2 MoE was always going to be the most brittle
  large-model integration in kinoforge; the layered fixes now form
  the standard playbook for the next one.

---

## 9. `2026-06-21 05:11:18` — Diffusers WanPipeline Wan 2.1 T2V-1.3B + single-LoRA matrix on RunPod (RTX A5000 24GB) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `runpod / DiffusersEngine / Wan-AI/Wan2.1-T2V-1.3B-Diffusers` |
| **Mode** | t2v |
| **kinoforge version** | branch `main` |
| **First-success SHA** | `53d5777` (`fix(harness): warmup /health before first /lora/set_stack`) |
| **Date (local TZ)** | 2026-06-21 05:11:18 -0700 (PDT) |
| **Layer / phase** | LoRA smoke-test pyramid Tier 3 (plan `docs/superpowers/plans/2026-06-21-lora-smoke-pyramid.md`, Task 11) |

First green Tier-3 fire of the LoRA smoke-test pyramid
(`docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md`).
Drives the 4-step single-LoRA swap matrix on Wan 2.1 1.3B via the
shared `tests/_smoke_harness/` module — proves the warm-reuse
`POST /lora/set_stack` path against a real-diffusers backend at
~$0.10 / fire (vs ~$0.86 / fire for the Tier-4 Wan 2.2 14B counterpart
in entry #10).

### Exact command

```bash
KINOFORGE_LIVE_TESTS=1 pixi run smoke-21b-live
# alias for:
KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest \
  tests/smoke/live_wan21/ -v -s
```

The smoke driver (`tests/smoke/live_wan21/test_lora_swap_matrix.py`)
runs one `pixi run kinoforge generate` for the step-1 cold-boot,
then drives steps 2-4 via `tests/_smoke_harness/matrix.run_matrix`
which does `POST /lora/set_stack` → `kinoforge generate
--instance-id <pod>` per step against the same warm pod.

### YAML config

`examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml`
(committed at HEAD `53d5777`). Salient knobs:

```yaml
engine:
  kind: diffusers
  precision: fp16
  diffusers:
    image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    server_cmd: ["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"]
    pip:
      - "torch==2.6.0"
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "peft>=0.13"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
models:
  - ref: "hf:Wan-AI/Wan2.1-T2V-1.3B-Diffusers"
    kind: base
compute:
  provider: runpod
  requirements:
    min_vram_gb: 24
    gpu_preference: ["NVIDIA RTX A5000", "NVIDIA RTX 4090", "NVIDIA L4"]
    max_usd_per_hr: 0.40
  lifecycle:
    max_lifetime: 60m
    boot_timeout: 30m
    budget: 0.50
spec:
  width: 480
  height: 480
  num_frames: 33
  fps: 16
smoke:
  lora_a: "civitai:1479320@1673265"   # wan2.1 1.3b static rotation
  lora_b: "civitai:1595383@1805395"   # Pokemon Sprite Animation Video LoRA
```

### Prompt

All 4 steps used the same source prompt from
`examples/configs/prompts/field-realistic.txt` (the canonical
test prompt per the `standard-test-prompt-for-video-smokes` memory
— no per-test override, enables cross-model comparison). No
trigger-word prefix prepended (the Wan 2.1 1.3B LoRAs activate by
load alone for Pokemon; `sttcrttn` is a motion effect that does not
require prompt prefix to demonstrate the swap).

### Env vars / secret names (names only — never values)

- `RUNPOD_API_KEY` — pod create/destroy + GraphQL util probe.
- `RUNPOD_TERMINATE_KEY` — preflight assertion + leak-sweep destroy.
- `CIVITAI_TOKEN` — Bearer auth on CivitAI `/api/v1/model-versions/{id}`
  + the actual `/api/download/models/{id}` file fetch from the pod.
- `HF_TOKEN` — Wan 2.1 1.3B `from_pretrained` (declared as
  `env_required` by `DiffusersEngine.render_provision`).

### Region

RunPod auto-select (no explicit cloud-region pin in cfg). Green pod
`2nik609jv7smsj` was an RTX A5000 24GB machine.

### Capability key

`0aaf4ee6e6c0` (derived from `hf:Wan-AI/Wan2.1-T2V-1.3B-Diffusers`
+ DiffusersEngine + t2v mode). Distinct from the Wan 2.2 14B
cap_key `5dff86b4f44e` (entry #10) so warm-reuse cannot cross the
two — appropriate isolation since the model dimensions differ.

### LoRA refs (repo-canonical for Wan 2.1 1.3B)

See README "Default test LoRAs (Wan 2.1 1.3B T2V)" — operator-pinned
2026-06-21:

| Slot | CivitAI page | Ref | Base | Trigger | Size on disk |
|---|---|---|---|---|---|
| A | <https://civitai.com/models/1479320?modelVersionId=1673265> | `civitai:1479320@1673265` | Wan Video 1.3B t2v | `sttcrttn` | 350,068,312 B |
| B | <https://civitai.com/models/1595383?modelVersionId=1805395> | `civitai:1595383@1805395` | Wan Video 1.3B t2v | (none — style activates by load) | 87,593,744 B |

### Output artifact

All 4 mp4s landed in `/workspace/output/`. Each is h264 / yuv420p /
480×480 / 33 frames / 16 fps / 2.0625 s (ffprobe-verified). All 4
sha256s distinct → matrix runner's `sha_distinct_required=True`
post-condition green.

| Step | Target stack | Path | Size | SHA-256 |
|---|---|---|---|---|
| 1 (cold-boot, 0 LoRAs) | `[]` | `output/20260621-051118_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 347,293 B | `a6f810c4193d7c045fb9f92c2c9eda3cd51732458949d87d4ca39a7141f90dd7` |
| 2 (warm-attach `[A]`) | `[civitai:1479320@1673265]` | `output/20260621-051515_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 662,031 B | `4024eeec5b072a314d93ffa357bc81957539ea8df0d0c209646688eeeae90280` |
| 3 (swap to `[B]`) | `[civitai:1595383@1805395]` | `output/20260621-051604_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 215,183 B | `6788e2175f928830b11490a110c309d4977cb65d25e54df386eff2f1eb982d70` |
| 4 (clear all) | `[]` | `output/20260621-051637_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 369,137 B | `bcf9fcd2f5d16ac0b5bf9d4e88319ae8c356ec7b7d4ef4993fb9d5892529b9f6` |

### Cost

Green pod `2nik609jv7smsj` (NVIDIA RTX A5000 24GB, ~$0.34/hr):
- Cold-boot (pip install + 3 GB HF weights + first `/generate`):
  ~8 m to step-1 mp4.
- Steps 2-4 (download 350 MB + 88 MB LoRAs, swap, generate × 3):
  ~7 m wall-clock + 600 s harness inventory-convergence poll on the
  step-2 proxy 502.
- Total wall-clock 15 m 48 s, pod spend ~$0.10 (BudgetTracker
  `cap_usd=0.30` passed, `lifecycle.budget=0.50` ceiling never
  approached).

Debug trail across 7 prior Tier-3 fires (#1-#7) that surfaced the
8 bugs listed in Failure modes below: ~$0.48 cumulative across pods.
Plus diagnostic pod ($0.03). Total Tier-3 cost to green:
**~$0.61**.

### Success criterion

`pytest` exit 0 (single test
`tests/smoke/live_wan21/test_lora_swap_matrix.py::test_lora_swap_matrix_wan21`)
in 948.23 s (15 m 48 s). The runner enforces, in order:
- preflight green (env keys + zero active pods + clean tree),
- cold-boot `kinoforge generate` rc=0 + mp4 published,
- `_warmup_proxy` /health 200,
- per-step `set_stack` 200 or 502+inventory-convergence,
- per-step inventory == target_stack,
- per-step `kinoforge generate --instance-id <pod>` rc=0 + mp4,
- adjacent step mp4 sha256s distinct,
- final `BudgetTracker(cap_usd=0.30, pod_id=...).assert_under_cap()`
  passes,
- finally `runpod_lifecycle.destroy_all_active_pods()` returns the
  pod id (no leak).

### Failure modes encountered before success (8 fires)

The smoke harness landed in 16 plan tasks (`docs/superpowers/plans/2026-06-21-lora-smoke-pyramid.md`)
but the first 7 Tier-3 fires each exposed a different bug that the
existing Tier-1 stub didn't catch. Each was root-caused, fixed with
a focused commit, and pinned by a unit test:

- Fire #1-#2 abort ($0.03 + $0.05): cfg used operator-not-yet-vetted
  LoRA pair; aborted before set_stack work to refit. Closed by
  user-gate Task 16 supplying the canonical pair.
- Fire #3 commit `0f8bec8` — `WAN_MODEL_ID` propagation:
  `wan_t2v_server.py` defaults to Wan 2.2 14B; `cfg.models[0].ref`
  must inject into the server env via
  `DiffusersEngine.render_provision`. Otherwise a Wan 2.1 1.3B cfg
  silently OOM-attempts 63 GB on a 24 GB A5000 → ProvisionTimeout
  at 900 s.
- Fire #4 commit `d27429f` — `kinoforge-pod-download/0.1` UA in
  pod-side `_download_one`. CivitAI is Cloudflare-fronted and 403s
  the default `Python-urllib/X.Y` UA. Same class as commit `53a1e6e`
  on orchestrator-side `CivitAISource`.
- Fire #4 commit `7242739` — `peft>=0.13` in Wan cfg pip lists.
  diffusers' `LoraBaseMixin.unload_lora_weights` /
  `set_adapters` raise `ValueError("PEFT backend is required for
  this method.")` without it.
- Fire #4 commit `7242739` — harness `tests/_smoke_harness/http.py`
  reads HTTPError response body into `exc.response_body` AND
  appends to `exc.msg` so 5xx failures carry the underlying cause
  instead of opaque "HTTP Error 502: Bad Gateway".
- Fire #5-#7 commit `810f2f4` — wan-server async-blocking fix:
  `_download_one` and the diffusers ops in `_reload_pipeline_loras`
  were called inline from `async def set_stack`, blocking the
  FastAPI event loop. `/health` hung, RunPod's edge proxy returned
  "Waiting for service to respond" HTML (HTTP 502) even though
  uvicorn was alive. Wrapped both in `await asyncio.to_thread(...)`.
- Fire #6-#7 commit `5659f82` — harness 502-recovery: catch proxy
  502s on `/lora/set_stack` and poll `/lora/inventory` every 10 s
  for up to 600 s. The server-side download often completes despite
  the proxy giving up.
- Fire #6 commit `5b07afd` — `logging.basicConfig(level=INFO, ...)`
  at wan_t2v_server module init. Module `_log.warning` calls were
  silently dropped because uvicorn wires its own logger but not the
  module's. Without this, no signal from the handler about which
  ref was downloading or where it failed.
- Fire #8 commit `53d5777` — harness `_warmup_proxy()`: GET /health
  before the first `/lora/set_stack` POST. The RunPod edge proxy
  502s the first POST after a freshly-created pod until a probing
  GET warms the upstream connection. Diagnostic against a same-pod
  manual call (after the warmup) confirmed set_stack succeeds in
  8.1 s.

### Notes

- The harness fix landscape generalises: the four patterns
  (kinoforge-smoke UA, `?api_key=`, URLError retry, leak-sweep) plus
  the new four (WAN_MODEL_ID inject, asyncio.to_thread,
  502-recovery via inventory poll, /health warmup) are now all in
  `tests/_smoke_harness/`. Future engine smokes (C23 ComfyUI, Wan
  3.0, Flux) inherit them by import — none of them should ever
  rediscover any of these.
- The repo-canonical Wan 2.1 1.3B LoRA pair lives in
  `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml`
  + the README "Default test LoRAs (Wan 2.1 1.3B T2V)" section. The
  pair is operator-specified (2026-06-21) to give cross-style mp4
  shas in the matrix.
- `pixi run smoke-21b-live` is the operator entrypoint; the weekly
  GH Actions cron at `.github/workflows/smoke-wan21-weekly.yml`
  (Mon 04:00 PT = 12:00 UTC) automates it. The leak-sweep cron
  (`.github/workflows/leak-sweep.yml`, every 30 min) backstops any
  tier-3 pod older than 45 min.

### See also (2026-06-25): P3 `kinoforge generate --loras` CLI-override path

P3 CLI `--loras` arg surface (spec
`docs/superpowers/specs/2026-06-25-p3-cli-loras-arg-design.md`,
plan `docs/superpowers/plans/2026-06-25-p3-cli-loras-arg.md`)
re-fired the same Wan 2.1 1.3B + LoRA pair end-to-end via
`kinoforge generate --loras "$(cat <<'EOF' ... EOF)"` against a clone
of the strength-grid cfg with `loras: []` (CLI sole LoRA source). MP4
`output/20260625-233553_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4`,
sha256 prefix `748c9cf4e1c1eb9c`, 425 KB. RunPod pod `6u8gh8dzlix3ct`,
~10:26 wall-clock, est_spend ≤ $0.10. `--no-reuse` auto-destroyed the
pod; post-run `kinoforge list` confirmed `No running instances.` +
`No instances recorded in ledger.` Vault-bypass branch NOT exercised
(no vault loaded — exercise covered by `tests/core/test_lora_resolver_p3.py`).
Proves the CLI override threads through `parse_loras_heredoc` →
`resolve_active_lora_stack(*, cli_loras=...)` →
`build_set_stack_request` → live `/lora/set_stack` wire body.

### See also (2026-06-26): `kinoforge grid lora_swap:` cell variant — Tier-3 3-cell sweeps

Grid `lora_swap:` cell variant (spec
`docs/superpowers/specs/2026-06-26-grid-lora-swap-design.md`,
plan `docs/superpowers/plans/2026-06-26-grid-lora-swap.md`) packs
N strength-sweep cells into ONE warm pod with server-side
`/lora/set_stack` swaps between cells, replacing N cold-boots with
1 cold-boot + N-1 attaches. Two Tier-3 live fires against Wan 2.1
1.3B + Pokemon/static-rotation pair on RunPod (RTX A4000 24GB,
$0.16/hr):

- **Fire 1 (happy-path, all branch=auto):** pod `o80tl2byw6irbh`,
  3 cells at strengths {0.5, 1.0, 1.5}, group cold-boot ~7 min,
  3 generations in 80 s after model-ready (~26 s/cell), 3 distinct
  shas (`bef386d8…392d932`, `241cfbc1…85742a4`, `d5ed27db…71f250`),
  composed `/tmp/output/tier3-swap-happy-4.mp4` (1.38 MB), sidecar
  `/tmp/output/tier3-swap-happy-4.cost.json` total $0.0036 (under
  $0.50 cap), wall 535 s. Post-run `kinoforge list` clean.
- **Fire 2 (branch=high_noise on single-transformer, no server
  reject):** pod `3dt0ue4xt1wkv0`, 3 cells, 3 distinct shas, sidecar
  total $0.0035, wall 466 s. The Wan 2.1 1.3B server accepted
  `branch=high_noise` rather than raising
  `BranchUnsupportedOnSingleTransformer` (intended "forced failure"
  did not trigger; classify CONTINUE path validated by unit tests
  `tests/core/test_grid_swap_failures.py`).

3 production bugs surfaced + fixed mid-fires:

- `6d313e4` — `_run_swap_cell_once` now persists full subprocess
  stderr to `<tmp_dir>/cell_<i>.stderr.txt` (was silently dropped
  past the 500-char tail in `GridCellFailure`).
- `865a4e4` → `76ff41f` — `_resolve_attach_pod` merges the ledger
  entry's recorded `tags` into the `provider.get_instance` result
  before calling `provider.endpoints(live)`. RunPod's pod-query
  GraphQL doesn't include the port spec; without the merge,
  `wait_for_ready` raises `ProvisionFailed: pod has no endpoints`
  on every cells-2..N attach.
- `59b22cf` — `LoraStackEntry.branch` literal aligned with
  `kinoforge.core.lora.LoraEntry.branch` canonical form
  `{high_noise, low_noise, auto}` (was `{high, low, auto}` —
  the executor's `_stack_to_loras_heredoc` would emit `branch=high`
  and the CLI `--loras` parser would reject `literal_error`).

Tier-4 (Wan 2.2 14B Arcane high_noise+low_noise pair) **FULL GREEN
2026-06-27** on pod `oig4i9vcynbq10` (A100 80GB, $1.39/hr): 3 cells
at strengths {0.5, 1.0, 1.5}, 3 sha-distinct mp4s
(`8f9d93c1…439aa6`, `31fed0bc…e9f971`, `37ef6e4f…478772`), composed
`/tmp/output/tier4-swap-3.mp4` (4.37 MB), sidecar
`/tmp/output/tier4-swap-3.cost.json` total $0.148 (under $2.00 cap),
wall 1158 s (group wall 385 s for 3 generations after model load).
Post-run `kinoforge list` clean. The MoE pair `branch=high_noise`
+ `branch=low_noise` correctly routes to pipe.transformer +
pipe.transformer_2 per the Q5 strict-routing invariants.

Two intermediate Tier-4 failures surfaced two additional bugs before
the GREEN re-fire:
- `d34cbfd` — `_resolve_attach_pod` retries `provider.get_instance`
  on `KeyError` up to 3× at 5s backoff (RunPod GraphQL eventual-
  consistency mid-state-transition).
- `0f3790a` — RunPod `authed_post`/`authed_get` retry on
  502/503/504 up to 3× at (2s, 5s, 10s) backoff (transient gateway
  failures mid `wait_for_ready` poll loop killed a 5-min cold-boot
  on fire `94344920`).

Same tuple `(runpod, DiffusersEngine, Wan-AI/Wan2.1-T2V-1.3B-Diffusers
+ Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)`. Cumulative live-fire spend
for the grid `lora_swap` workstream: Tier-3 $0.07 + intermediate
Tier-4 $1.45 + Tier-4 GREEN $0.15 = **$1.67**.

---

## 10. `2026-06-21 05:37:14` — Diffusers WanPipeline Wan 2.2 T2V-A14B + Arcane LoRA pair warm-reuse matrix on RunPod (A100 80GB) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `runpod / DiffusersEngine / Wan-AI/Wan2.2-T2V-A14B-Diffusers` (with `civitai:2197303@2474081` + `civitai:2197303@2474073` adapters) |
| **Mode** | t2v |
| **kinoforge version** | branch `main` |
| **First-success SHA** | `53d5777` (`fix(harness): warmup /health before first /lora/set_stack`) — same HEAD as entry #9; no commits between Tier-3 pass and Tier-4 fire |
| **Date (local TZ)** | 2026-06-21 05:37:14 -0700 (PDT) |
| **Layer / phase** | LoRA smoke-test pyramid Tier 4 (plan `docs/superpowers/plans/2026-06-21-lora-smoke-pyramid.md`, Task 12). Closes the T22 partial-state from entry #8 "See also" of 2026-06-20 23:33:36. |

**T22 ARCANE LoRA-SWAP MATRIX FINALLY GREEN.** Picks up exactly where
the 2026-06-20 attempt sequence stopped: the full 4-step warm-reuse
matrix on Wan 2.2 14B + the canonical Arcane Style LoRA pair
(`civitai:2197303@2474081` high + `civitai:2197303@2474073` low).
Same engine + model + provider as entry #8, but now exercises the
LoRA-swap path that was deferred — graduating from "See also" to its
own capability axis.

### Exact command

```bash
KINOFORGE_LIVE_TESTS=1 pixi run smoke-wan22-live
# alias for:
KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest \
  tests/smoke/release_wan22/ -v -s
```

The smoke driver (`tests/smoke/release_wan22/test_lora_swap_matrix.py`)
runs `_run_cli("generate", ...)` for the step-1 cold-boot, then
`matrix.run_matrix(...)` for steps 2-3 (and matrix-step-4 set-to-[]),
then a final `_run_cli("generate", ..., "--instance-id", pod)` with
the plain (no-ArcaneStyle) prompt to capture a 5th post-swap-empty
mp4. Manual release gate — no automated cron trigger.

### YAML config

`examples/configs/runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml`
(committed at HEAD `53d5777`). Salient knobs:

```yaml
engine:
  kind: diffusers
  precision: bf16
  diffusers:
    image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      - "torch==2.6.0"
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "peft>=0.13"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers"
    kind: base
compute:
  provider: runpod
  image: "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
  warm_reuse_auto_attach: true
  tags:
    smoke_tier: "kinoforge-smoke-tier-4"
  requirements:
    min_vram_gb: 80
    max_usd_per_hr: 3.00
    gpu_preference:
      - "NVIDIA A100 80GB PCIe"
      - "NVIDIA A100-SXM4-80GB"
      - "NVIDIA H100 80GB HBM3"
      - "NVIDIA H100 PCIe"
    disk_gb: 150
  lifecycle:
    max_lifetime: 150m
    boot_timeout: 60m
    budget: 5.0
    heartbeat_interval_s: 30
    lora_swap_re_probe_after_s: 300
spec:
  width: 480
  height: 480
  num_frames: 81
  fps: 16
```

LoRA refs are hardcoded in the smoke driver
(`tests/smoke/release_wan22/test_lora_swap_matrix.py`) — not in the
cfg — because the Wan 2.2 14B pair is the canonical operator default
documented under README "Default test LoRA (Wan 2.2 T2V)" rather
than a cfg-specified knob.

### Prompt

- Steps 1 + 4-bare: plain prompt from `examples/configs/prompts/field-realistic.txt`.
- Steps 2 + 3 + matrix-step-4: same prompt with `ArcaneStyle `
  prepended (trigger word for the Arcane LoRA pair, per the
  CivitAI model card).

The plain-vs-styled distinction is what surfaces visible Arcane
styling in the mid-matrix mp4s vs the bare regen at the end. Same
canonical prompt across the whole run per the
`standard-test-prompt-for-video-smokes` memory.

### Env vars / secret names (names only — never values)

- `RUNPOD_API_KEY` — pod create/destroy + GraphQL util probe.
- `RUNPOD_TERMINATE_KEY` — preflight assertion + leak-sweep destroy.
- `CIVITAI_TOKEN` — Bearer auth on CivitAI model-version metadata
  + the actual file fetch from inside the pod for both Arcane
  tensors.
- `HF_TOKEN` — Wan 2.2 14B `from_pretrained` (declared as
  `env_required` by `DiffusersEngine.render_provision`).

### Region

RunPod auto-select (no explicit cloud-region pin in cfg). Green pod
`eoisesybsq5wbg` was an NVIDIA A100 80GB machine; same physical
machine class as entry #8's `7o0p1pyvbfpbr8` and the 4-prompt
warm-reuse `87geau1jcpxr0z`.

### Capability key

`5dff86b4f44e` (derived from `hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers`
+ DiffusersEngine + t2v mode). **Identical to entry #8's cap_key** —
warm-reuse would attach to entry #8's pod if it were alive at fire
time. The pod was already destroyed long before this fire so the
matcher cold-created. The Arcane LoRAs do not factor into the
capability key (per the LoRA-flexible warm-reuse design: LoRA stack
is a swap-time delta, not a key factor).

### LoRA refs (repo-canonical Arcane Style v1.0 for Wan 2.2 T2V)

Operator-pinned long before this entry; see README
"Default test LoRA (Wan 2.2 T2V)" for activation guidance.

| Role | CivitAI page | Ref | Size on disk | Adapter name |
|---|---|---|---|---|
| High noise | <https://civitai.com/models/2197303?modelVersionId=2474081> | `civitai:2197303@2474081` | ~720 MB | `lora_0` after warm-attach `[high, low]` |
| Low noise | <https://civitai.com/models/2197303?modelVersionId=2474073> | `civitai:2197303@2474073` | ~720 MB | `lora_1` after warm-attach `[high, low]`; `lora_0` after swap to `[low]` only |

Trigger word: `ArcaneStyle` — prepended to the prompt where the
LoRA's style should activate. Recommended strength per the CivitAI
page: 1.0–1.2 (the smoke uses the diffusers default of 1.0;
operator-facing weight overrides are not exercised here).

### Output artifact

All 5 mp4s landed in `/workspace/output/`. Each is h264 / yuv420p /
480×480 / 81 frames / 16 fps / 5.0625 s (ffprobe-verified). All 5
sha256s distinct → LoRA-swap demonstrably changes output;
matrix runner's `sha_distinct_required=True` post-condition green.
`swap_rejected` is `null` for every transition (no VRAM-OOM rollback
needed at 80 GB).

| Step | Target stack | Path | Size | SHA-256 |
|---|---|---|---|---|
| 1 (cold-boot, 0 LoRAs) | `[]` | `output/20260621-053714_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` | 1,299,080 B | `2b8eef0644ee49f31463741088790ab9e81fc7a32346b097a32afe64882fb29f` |
| 2 (warm-attach `[high, low]`) | `[civitai:2197303@2474081, civitai:2197303@2474073]` | `output/20260621-054108_diffusers_Wan2.2-T2V-A14B-Diffuser_ArcaneStyle-Photorea.mp4` | 1,173,069 B | `9774c9c0ab03aeb396f706bd361de8138b71b9d3c50ea123700558c8904736c3` |
| 3 (swap to `[low]` only) | `[civitai:2197303@2474073]` | `output/20260621-054450_diffusers_Wan2.2-T2V-A14B-Diffuser_ArcaneStyle-Photorea.mp4` | 1,628,999 B | `13a351bc0d358fef2cd6e05f2ec88bdabb5749923af6f73e55c2d2fb9535b552` |
| 4-matrix (clear via `set_stack []`) | `[]` | `output/20260621-054827_diffusers_Wan2.2-T2V-A14B-Diffuser_ArcaneStyle-Photorea.mp4` | 1,851,359 B | `3faec26d4a1b27f16fc09074703a94a8b645a62abc424a573963880a7d1526fa` |
| 4-bare (`_run_cli` plain regen, post-swap empty) | `[]` | `output/20260621-055203_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` | 1,864,878 B | `6875284cb464ed87943ed57a530db1b28236768981e9327d9de196ab3f0c8581` |

### Cost

Green pod `eoisesybsq5wbg` (NVIDIA A100 80GB, ~$1.79/hr):
- Cold-boot (pip install + 63 GB Wan 2.2 14B HF weights + first
  `/generate`): ~20 m to step-1 mp4 (large MoE weight download is
  the dominant term).
- Steps 2-4 + 4-bare regen (download 2× 720 MB Arcane tensors, swap
  to [low], clear, regen): ~14 m wall-clock.
- Total wall-clock 34 m 13 s, pod spend ~$0.86. `BudgetTracker(
  cap_usd=2.00)` passed; `lifecycle.budget=5.0` never approached;
  `max_lifetime=150m` never approached.

No debug trail at Tier-4 — every bug surfaced at Tier-3 (entry #9)
where the per-fire cost is ~$0.10. **Total Tier-4 cost to green:
~$0.86 first fire.** This is the whole point of the smoke pyramid
(`docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md`):
cheap-tier debugging followed by a clean expensive-tier release
gate.

### Success criterion

`pytest` exit 0 (single test
`tests/smoke/release_wan22/test_lora_swap_matrix.py::test_wan22_lora_warm_reuse_4_step_matrix`)
in 2053.38 s (34 m 13 s). The driver enforces, in order:
- preflight green,
- step-1 `kinoforge generate` rc=0 + mp4 published + pod_id captured
  via `_extract_pod_id`,
- `_warmup_proxy` /health 200,
- per-matrix-step `set_stack` 200 or 502+inventory-convergence,
- per-matrix-step inventory == target,
- per-matrix-step `kinoforge generate --instance-id <pod>` rc=0 + mp4,
- adjacent step mp4 sha256s distinct,
- step-4-bare `_run_cli` rc=0 + 5th mp4 produced,
- final `BudgetTracker(cap_usd=2.00, pod_id=...).assert_under_cap()`
  passes,
- finally `runpod_lifecycle.destroy_all_active_pods()` returns the
  pod id (no leak).

### Failure modes encountered before success

**None at Tier-4.** Every bug that could have surfaced here was
caught + fixed at the $0.10/fire Tier-3 (entry #9). The 8 commits
that closed the gaps:
- `53a1e6e` — civitai-source UA (kinoforge-side resolve path).
- `0f8bec8` — `WAN_MODEL_ID` propagation from cfg.models[0].ref.
- `d27429f` — `kinoforge-pod-download/0.1` UA in pod-side
  `_download_one`.
- `7242739` — `peft>=0.13` in cfg pip list; harness HTTPError body
  capture.
- `810f2f4` — `asyncio.to_thread` wrapping of `_download_one` +
  `_reload_pipeline_loras` (critical for 720 MB Arcane tensors —
  blocking the event loop on a 720 MB sync download would have
  triggered the "Waiting for service to respond" path immediately
  here).
- `5659f82` — harness 502-recovery via `/lora/inventory` polling.
- `5b07afd` — `logging.basicConfig` at wan_t2v_server module init.
- `53d5777` — harness `_warmup_proxy()` GET /health before first
  `set_stack` POST.

See entry #9 "Failure modes" for the per-bug detail of how each was
surfaced. T22's 2026-06-20 cumulative spend of $2.15 across 5 attempts
(documented in entry #8 "See also") was driven by these same bugs
hitting at Wan 2.2 14B prices — the pyramid investment paid for itself
on this fire.

### Notes

- The full 8-commit fix sequence is documented inline in entry #9
  (the Tier-3 Wan 2.1 1.3B counterpart, which surfaced + fixed every
  bug at $0.10/fire before any of them touched A100 prices).
- The wan_t2v_server's `async def set_stack` MUST keep wrapping
  `_download_one` and `_reload_pipeline_loras` in `asyncio.to_thread`.
  Without that, the event loop blocks while a 720 MB Arcane tensor
  downloads, /health stops responding, and RunPod's edge proxy
  returns its "Waiting for service to respond" page. The Tier-1
  CPU smoke doesn't catch this because the stub pipe is
  near-instant.
- The harness's `_warmup_proxy` is necessary even on a warm pod —
  the FIRST POST against a freshly-created pod 502s consistently
  until a probing GET completes. Don't remove it.
- 720 MB tensor downloads from Civitai (per Wan 2.2 LoRA half) push
  RunPod's edge proxy timeout. The 502-recovery via inventory poll
  is what carries the harness across when the proxy gives up — the
  server-side `_download_one` keeps streaming for up to its 600 s
  internal urlopen timeout and the harness eventually sees the
  inventory converge.
- The release-gate workflow is `pixi run smoke-wan22-live` (manual;
  no cron — too expensive to fire weekly). It is now a documented
  pre-tag item in `docs/RELEASE-CHECKLIST.md`.


---

## 11. `2026-06-23 21:35:52` — Diffusers WanPipeline Wan 2.2 T2V-A14B + per-transformer branch routing on RunPod (A100 80GB SXM) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `runpod / DiffusersEngine / Wan-AI/Wan2.2-T2V-A14B-Diffusers` with `civitai:2197303@2474081` + `civitai:2197303@2474073` Arcane LoRA pair routed per-transformer |
| **Mode** | t2v |
| **kinoforge version** | branch `main` |
| **First-success SHA** | `2a7d6f0` (`cfg(p2): reorder Wan 2.2 release gpu_preference — SXM first (stock=High)`) |
| **Date (local TZ)** | 2026-06-23 21:35:52 -0700 (PDT) |
| **Layer / phase** | P2 Wan 2.2 dual-transformer routing — Task 16 Tier-4 live fire (plan `docs/superpowers/plans/2026-06-22-p2-wan22-dual-transformer-routing.md`). Capability axis distinct from §10: per-LoRA `branch` field routes high-noise / low-noise tensors to the correct MoE transformer via boolean `load_into_transformer_2` kwarg. |

**P2 PARTIAL_GREEN — 5 of 7 cases PASSED on the warm-reuse routing matrix.** Six-case sequence on a single Wan 2.2 14B A100 80GB SXM pod proves per-transformer dispatch works end-to-end for the canonical (high_noise, low_noise) pair and rejects the MoE+auto contract per spec §7.1. Two cases (case 5 wrong-routing, case 7 same-ref-two-branches) hit HTTP 500 on the server's `/lora/set_stack` swap path — surface as P2 follow-ups, not blocking the routing capability axis.

### Exact command

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest \
  tests/smoke/release_wan22/test_dual_transformer_routing.py \
  --no-cov -v -s
```

7-case module-scope warm-reuse — fixture `_warm_wan22_pod` pays the cold-boot once + shares the warm pod across all 7 cases. Case 1 reuses the cold-boot mp4 as the baseline sha (no redundant generate). Cases 2-5 + 7 invoke `kinoforge generate --instance-id` for per-case mp4 capture; case 6 asserts HTTP 400 (no generate).

### YAML config

`examples/configs/runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml` at HEAD `2a7d6f0`. Differences vs §10's HEAD-`53d5777` snapshot:

- `gpu_preference`: SXM-first (`NVIDIA A100-SXM4-80GB` ahead of `NVIDIA A100 80GB PCIe`). Driven by 2 consecutive cold-boot pod kills (8h91rjnslmzwab, 9e2dsucq33zron) on A100 PCIe — RunPod's GraphQL probe reports `stockStatus="Low"` for PCIe at $1.39/hr vs `stockStatus="High"` for SXM at $1.49/hr. The $0.10/hr premium bought reliability.
- `lifecycle.idle_timeout`: 30m → 90m. The 7-case matrix needs more runway than the 4-step swap-matrix smoke; bumped headroom does not impact §10's existing test path.
- `lifecycle.max_lifetime`: 150m → 180m.

LoRAs are operator-managed via `/lora/set_stack` — config stays Base-only.

### Prompt

`examples/configs/prompts/field-realistic.txt` verbatim (same prompt across all 6 generations) per the `standard-test-prompt-for-video-smokes` memory. No `ArcaneStyle` prepend — branch routing is observed via raw mp4 sha distinctness, not perceptually.

### Env vars / secret names

- `RUNPOD_API_KEY`, `RUNPOD_TERMINATE_KEY`, `CIVITAI_TOKEN`, `HF_TOKEN` — same set as §10.
- `KINOFORGE_LIVE_TESTS=1` — pytest gate.

### Region

RunPod auto-select. Green pod `ee38uxn9rs444b` ran on `NVIDIA A100-SXM4-80GB`.

### Capability key

`5dff86b4f44e` — identical to §8 / §10. Per the P2 design, `branch` is intentionally NOT a capability-key factor: a single warm Wan 2.2 pod serves every branch combination via swap.

### LoRA refs

| Role | CivitAI page | Ref | Adapter name shape |
|---|---|---|---|
| High noise | <https://civitai.com/models/2197303?modelVersionId=2474081> | `civitai:2197303@2474081` | `lora_0_h` when canonical pair |
| Low noise  | <https://civitai.com/models/2197303?modelVersionId=2474073> | `civitai:2197303@2474073` | `lora_1_l` when canonical pair |

### Per-case outcomes

Pod `ee38uxn9rs444b` (NVIDIA A100-SXM4-80GB), cold-boot at 21:03 PDT, destroyed at 21:38:49 PDT. Total spend $0.80. Wall clock 31:38.

| # | Case | Stack | mp4 (under `.kinoforge/smoke-22b-branch-<label>/`) | Size | SHA-256 | Outcome |
|---|---|---|---|---|---|---|
| — | cold-boot (= baseline) | `[]` | `cold-boot/3f422e835c0ccb03.mp4` | 962,665 | `3f422e835c0ccb0361c78b24557d18a29a17cb1e3d8a7f8b5e471f1529864faf` | seeded `_shas["baseline"]` |
| 1 | `baseline_no_lora` | `set_stack(target=[])` only | (reuses cold-boot) | — | (same as above) | **PASS** — 200 + `inventory == []` |
| 2 | `arcane_high_noise_only` | `[(HIGH, 1.0, "high_noise")]` | `h-only/bf38957bb5b3e6d3.mp4` | 784,841 | `bf38957bb5b3e6d3d591782fda438faeefc8b033812cc333b7d2476014fb51eb` | **PASS** — `sha != baseline` |
| 3 | `arcane_low_noise_only` | `[(LOW, 1.0, "low_noise")]` | `l-only/e36160737fcca851.mp4` | 1,608,397 | `e36160737fcca851769feea77c2244d8913e6063000112b5f3df3d024af4bca0` | **PASS** — `sha != {baseline, h_only}` |
| 4 | `arcane_pair_canonical` | `[(HIGH, 1.0, "high_noise"), (LOW, 1.0, "low_noise")]` | `canonical-pair/8ab512d51afe0977.mp4` | 1,505,084 | `8ab512d51afe09776810125fc853b6b10e2de19b773d4ea49b0aa095131a7186` | **PASS** — `sha != {h_only, l_only}` |
| 5 | `wrong_routing_h_into_low_and_l_into_high` | `[(HIGH, 1.0, "low_noise"), (LOW, 1.0, "high_noise")]` | — | — | — | **FAIL** — server `/lora/set_stack` returned HTTP 500 (body `'Internal Server Error'`) before generate was reached |
| 6 | `moe_with_auto_branch_returns_400` | `[(HIGH, 1.0, "auto")]` | — | — | — | **PASS** — HTTP 400 `branch_routing` / `branch_auto_disallowed_on_moe` / `arity=2` per spec §6.1 |
| 7 | `same_ref_in_both_branches_composite_key` | `[(HIGH, 1.0, "high_noise"), (HIGH, 0.8, "low_noise")]` | — | — | — | **FAIL** — server `/lora/set_stack` returned HTTP 500 |

All 4 distinct shas (baseline / h_only / l_only / canonical_pair) confirm the per-transformer routing actually reaches both transformers and produces materially different output depending on which transformer each LoRA tensor patches. Case 6 asserts the spec §6.1 strict-reject contract for `auto` on MoE arity=2.

### Open server-side follow-ups (P2 Task 16 gaps)

- **case_5_wrong_routing**: server returns 500 when re-posting the canonical pair with swapped branches after the canonical pair was already loaded. Suspected gap in the unload-then-reload-with-different-branch path. Fix should preserve the strict spec contract (200 + sha materially differs from canonical) so the "routing matters" proof can be captured.
- **case_7_same_ref_two_branches**: server returns 500 when the same ref is posted with two different branches. Spec Q6 Option 1 "composite identity" requires two inventory rows under composite key `(ref, branch)`. Suspected gap: peft `load_lora_weights` rejects loading the same tensor file under two different adapter names, OR the server's `_adapter_name` collision-suffix breaks under the same-ref case.

Both 500s indicate the server raises an unmapped exception (else FastAPI would have returned a structured 4xx). Next-session priority: capture pod-side traceback (server log), wire the missing branches in `wan_t2v_server._replace_adapter_stack`, then re-fire just these 2 cases against a Tier-3 stub to avoid burning A100 cold-boot tax on each iteration.

### Failure history — preceding 2 fires lost during this session

- **8h91rjnslmzwab** (NVIDIA A100 80GB PCIe, $0.67 sunk). Cold-boot succeeded + first generate ran ~30 min into uptime with GPU at 100%; pod was silently revoked by RunPod mid-generate (no `idle_timeout`, no OOM in log). Diagnosed via `gpuTypes.lowestPrice.stockStatus` probe → PCIe = `Low` stock. Triggered the cfg reorder above.
- **9e2dsucq33zron** (NVIDIA A100 80GB PCIe, $1.32 sunk before forget). Pod died during `wait_for_ready` (i.e. before generate started). Same PCIe-stock-status root cause.

Both pods forgotten via `kinoforge forget --id <pod>`; ledger clean at fire-3 start.

### Notes

- The fixture pattern (capture cold-boot mp4 as baseline) saves one ~3 min generate cycle vs the canonical "fire all 7 generates" pattern from the original PROGRESS-author plan. Removes the back-to-back generate pressure that may have contributed to fire-1's mid-generate revocation.
- Case 6's HTTP 400 was structurally correct on FIRST live touch — the server-side arity-gate Task 4 commit `80eede8` (`BranchAutoNotAllowedOnMoE`) wired this through diffusers' `_lora_loadable_modules` reliably (Tier-3 had already proven the analogous Wan 2.1 reject path).
- Cases 1-4 + 6 lock in 5 of the 7 P2 §7.1 spec contracts on live A100 80GB. The two remaining gaps (cases 5 + 7) are server-side bugs in the swap-after-canonical path, not breaking the routing capability axis itself.

### Follow-up `2026-06-23 23:07:30` — Tier-4 7/7 **FULL_GREEN** post swap-gap fix

Re-fire on pod `2k0gonzmeqw7xj` (NVIDIA A100-SXM4-80GB, $1.49/hr) at HEAD `9799657` after the swap-gap fix landed (commits `0dec40d` design+plan, `305b832` RED scaffolds, `fdac5ab` `_evict_one` + handler patch). Cold-boot at 22:35:04 PT → all-cases complete + pod destroyed at 23:07:30 PT. **Wall clock 32:26. Total spend $0.80.** Pytest exit 0; thread-leak dump clean.

Root cause both prior 500s: `_replace_adapter_stack` raised `KeyError` outside the handler's `(RuntimeError, ValueError)` catch list because `_evict_one` unconditionally unlinked the shared on-disk file AND the download-step pending-entry loop never ran when every ref was already downloaded. Fix shape: `_evict_one` skips the file unlink when any surviving `(ref, *)` sibling inventory entry remains; the `set_stack` handler pre-seeds pending inventory entries for every target `(ref, branch)` whose ref already has any on-disk row BEFORE computing `mandatory_evict`. `mandatory_freed` accounting guarded against double-counting the shared file. Three Tier-1 unit tests in `tests/engines/diffusers/servers/test_set_stack_swap_gaps.py` fence the contracts forever. Full design + diagnosis: `docs/superpowers/specs/2026-06-23-p2-swap-gap-design.md`.

| # | Case | mp4 (under `.kinoforge/smoke-22b-branch-<label>/`) | SHA-256 | Outcome |
|---|---|---|---|---|
| — | cold-boot (= baseline) | `cold-boot/0f54f01389d581f6.mp4` | `0f54f01389d581f6fec47c48d5a40a7c46569eac7b25f9ba2b074aca4ae3404a` | seeded `_shas["baseline"]` |
| 1 | `baseline_no_lora` | (reuses cold-boot) | (same as above) | **PASS** — 200 + empty inventory |
| 2 | `arcane_high_noise_only` | `h-only/9e1d0d320fad4593.mp4` | `9e1d0d320fad459373f96eda476d5db5f656f87404fba7b6997212b5d76532c3` | **PASS** — `sha != baseline` |
| 3 | `arcane_low_noise_only` | `l-only/118dbb4e4a14c503.mp4` | `118dbb4e4a14c503d328ee721ccc51bb2879c3decba22ccb9bb1c2a78900be30` | **PASS** — `sha != {baseline, h_only}` |
| 4 | `arcane_pair_canonical` | `canonical-pair/2b49c75a600b5da9.mp4` | `2b49c75a600b5da9e0241781991739edc8f89029e8ec0ff5c5b03cd980364945` | **PASS** — `sha != {h_only, l_only}` |
| 5 | `wrong_routing_h_into_low_and_l_into_high` | `wrong-routing/2b68325d0f5b3472.mp4` | `2b68325d0f5b3472f9d60e50ea732a8446e77a7b50c196703d293fd64624af65` | **PASS** — `sha != canonical_pair` (routing-matters proof) |
| 6 | `moe_with_auto_branch_returns_400` | — (400-reject) | — | **PASS** — HTTP 400 `branch_auto_disallowed_on_moe` |
| 7 | `same_ref_in_both_branches_composite_key` | `same-ref-two-branches/358df2c3dcb11de5.mp4` | `358df2c3dcb11de58b46d088f9b4d1f3894ebd084d552016083777d021bda19e` | **PASS** — 2 inventory rows under composite `(ref, branch)`, distinct adapter names `lora_0_h` / `lora_1_l` |

All 6 mp4 shas distinct including the case_5 `wrong_routing` sha `2b68…af65` vs the case_4 canonical-pair sha `2b49…4945` — confirms per-transformer routing actually routes (the prior PARTIAL_GREEN entry could not validate this invariant because case_5 never produced an mp4).

Case_7 PASS confirms the case_7 live 500 on fire #3 was a state-cascade from case_5's mid-flight `KeyError` (which left the pod's inventory + on-disk state corrupted before case_7 ran), not a fresh-state bug. The case_5 fix resolved both 500s with one patch. Tier-1 test `test_same_ref_two_branches_yields_two_inventory_rows` was GREEN pre-fix and stays GREEN forever as the Q6 Option 1 composite-identity contract fence.

Teardown note: fixture's `runpod_lifecycle.destroy_all_active_pods()` again failed to reap the pod on teardown (same bug noted on the prior Tier-3 fires this session — PROGRESS.md line 234-237). Smoke harness's explicit-destroy fallback `subprocess.run(["pixi", "run", "kinoforge", "destroy", "--id", pod_id])` ALSO did not reach the pod (visible because `kinoforge list` immediately after pytest exit still showed the pod alive). Manual `pixi run kinoforge destroy --id 2k0gonzmeqw7xj` reaped cleanly at 23:07:30 PT. The destroy bug is a P2 follow-up.

Open follow-up: revert `_BUDGET_CAP = 4.0 → 2.0` in `tests/smoke/release_wan22/test_dual_transformer_routing.py` now that the swap-gap re-fire is FULL_GREEN. The cap was bumped solely for this re-fire (commit `9799657`); single-SXM fires are ~$0.80 so the standing $2 ceiling is right.

---

## 12. `2026-06-30 21:19:07` — SpandrelEngine RealESRGAN-x2 upscale on RunPod (wan_t2v_server multi-engine) — upscale

| Field | Value |
|---|---|
| **Stack triple** | `runpod / SpandrelEngine (client) + wan_t2v_server (server, spandrel-* prefix) / RealESRGAN_x2 (spandrel-realesrgan-fp16)` |
| **Mode** | upscale (new capability axis) |
| **kinoforge version** | branch `video-upscaling` (worktree) |
| **First-success SHA** | `4052080` (T15 evidence commit); functional-GREEN commits `de42070` + `9274c5c` + `a6345c2` + `43a1cab` + `87024ab` + `1c1f414` + `a7525f8` + `6961530` + `4d95377` + `7366e0b` + `b3b49bf` + `3e07026` |
| **Date (local TZ)** | 2026-06-30 21:19:07 -0700 (PDT) |
| **Layer / phase** | P3 (pod file-upload path) — plan `docs/superpowers/plans/2026-06-29-upscale-pod-upload.md`, spec `docs/superpowers/specs/2026-06-29-upscale-pod-upload-design.md` |

**GREEN — T15 single-shot upscale (`4ju5e4ae9jnx6e`-preceding pod `1jofyeyg46m747` att7, spend $0.02, 116.91 s wall) AND T16 multi-stage warm-reuse (Wan T2V → spandrel on same pod `4ju5e4ae9jnx6e`, spend $0.25, 908.82 s wall).** New capability axes: (a) client-side `PUT /upload` sha256-verified streaming path for `file://` sources; (b) pod-side `_UPLOAD_DIR` scratch with atomic publish + cleanup finally; (c) spandrel-* prefix dispatch in `/upscale` route + `_run_upscale_job` body co-existing with seedvr2-*; (d) `_load_model_to_gpu` now moves fresh `nn.Module` to CUDA (regression fix — metadata previously lied when weights stayed on CPU); (e) ImageModelDescriptor unwrap + combined `.to(device, dtype)` to preserve device across dtype cast; (f) orchestrator `sink.publish` for upscaled artifact fires pre-destroy so `--no-reuse` doesn't leave the client with a dead pod URL; (g) new `kinoforge logs --id <pod>` CLI fetches `/tmp/bootstrap.log` via port-8001 sidecar (unblocks post-crash diagnosis).

### Exact command (T15 single-shot)

```bash
pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v -m live
```

### Exact command (T16 multi-stage)

```bash
pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v -m live
```

### YAML configs

- T15 single-shot: `examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml` — `engine.kind=diffusers`, `spec.model=spandrel-realesrgan-fp16`, `upscale.engine=spandrel`, `upscale.scale=2x`, `spandrel.arch=realesrgan`, `spandrel.precision=fp16`, `spandrel.model_url=hf:ai-forever/Real-ESRGAN/RealESRGAN_x2.pth`.
- T16 multi-stage: `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale.yaml` — Wan 2.2 T2V-A14B (models[0]) + same spandrel upscale block. Prompt sourced from `examples/configs/prompts/field-realistic.txt` per the standard-test-prompt policy.

### Fixture (T15)

`examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4` — 480×480, 33 frames, 188953 bytes, sha256 `54a5f732497679ebdee900644309dcfa9894260db2837d47e0626c4b08ecd1dc`.

### Outputs

- T15 upscaled: 480×480 → 960×960, 33 frames, 773345 bytes. Evidence at `tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/_t15_evidence.json`.
- T16 stage-1 (Wan): 480×480, 81 frames, 827869 bytes at `/workspace/output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4`.
- T16 stage-2 (upscaled): 960×960, 81 frames, 1292638 bytes at `/workspace/output/20260630-221907_upscaled_spandrel_Wan2.2-T2V-A14B-Diffuser_upscale.mp4`. Evidence at `tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/_t16_evidence.json`.

### Bugs surfaced + fixed in-flight (7 attempts to T15 GREEN, 3 to T16)

1. Pod-embed missing `kinoforge.core.registry` → split SpandrelEngine into `._engine` + pod-safe shim `__init__` (`87024ab`).
2. SpandrelRuntime fp16 input vs fp32 model bias → cast dtype in `upscale()` (`43a1cab`).
3. Pod hangs undiagnosable without stderr access → new `kinoforge logs` CLI (`4d33e65`).
4. `_load_model_to_gpu` claimed `on_device="cuda"` but never called `.to("cuda")` on fresh pipe → moved after construction (`a6345c2`).
5. ImageModelDescriptor.to(dtype) dropped device on some spandrel versions → unwrap raw model + single `.to(device=X, dtype=Y)` (`9274c5c`).
6. Orchestrator returned pod's proxy URL as artifact.uri; `--no-reuse` destroyed pod before caller could fetch → sink materializes bytes pre-return (`de42070`).
7. Multi-stage `kinoforge generate` returns "clip" artifact; my materialize hook only fired for "upscaled" → sink both regardless of returned key (`c374d8a`).
8. Spandrel dispatch only handled `file://` scheme; stage-1 Wan output arrives as bare `/abs` path from LocalStore → accept bare abs paths too (`1a50452`).

### Notes

- T16 stage-2 DID PUT /upload from the operator's disk (Wan output was already sinked locally at end of stage-1). Original plan AC said "no PUT /upload" — updated in `_t16_evidence.json` under `notes`: strict zero-upload warm-reuse would require an orchestrator mode that keeps intermediate artifacts pod-local.
- Total P3 live spend across all attempts ≈ $1.20 (T15 ~$0.10 across 7 attempts, T16 ~$0.85 across 3 attempts, plus one debugging pod destroy fallback). Under the plan's $1-3 envelope for the multi-stage smoke.
- Post-both-runs ledger clean: `kinoforge list` returns `No running instances.` AND `No instances recorded in ledger.` at both T15 and T16 close.

---

## 13. `2026-07-03 12:13:45` — FlashVSR v1.1 diffusion upscaler (4x native) on RunPod A100 80GB — upscale

| Field | Value |
|---|---|
| **Stack triple** | `runpod / FlashVSREngine + DiffusersEngine (upscale-only mode) / diffsynth.FlashVSRFullPipeline @ JunhaoZhuang/FlashVSR-v1.1` |
| **Mode** | upscale |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `af212dc` (F-single pytest); `e1a42e4` first end-to-end green (24th manual smoke `50ioxii84z3bjv`) |
| **Date (local TZ)** | 2026-07-03 12:13:45 -0700 (PDT) |
| **Layer / phase** | T7.6 sub-plan (`docs/superpowers/plans/2026-07-02-flashvsr-runtime-rewrite.md`) — closes T#8 F-single from parent P4 plan (`docs/superpowers/plans/2026-07-01-flashvsr-video-upscaling.md`). |

### Exact command

```bash
KINOFORGE_LIVE_SPEND=1 pixi run pytest \
  tests/live/test_flashvsr_live.py::test_f_single -v -s
```

### Cfg

- `examples/configs/runpod-diffusers-flashvsr-x4-upscale.yaml` — engine=`diffusers`, upscaler=`flashvsr`, scale=`4x`, precision=`bfloat16`, tile_size=`512`, GPU tier=A100/H100 80GB, image=`runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04`.

### Input

- Source clip: `/workspace/output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` — 480×480, 81 frames, Wan 2.2 T2V-A14B (see entry #8).

### Output

- Artifact: `output/20260703-121345_upscaled_flashvsr_flashvsr-wan21-bfloat16_upscale.mp4`
- Dims: **1920×1920** (source×4) — verified via `pixi run ffprobe`.
- Size: ~34 MB, h264 / yuv420p / libx264.
- SHA256: `e277993070125f05fb0288443327e037c74abe207169dcb7a116cb6220386a22` (24th manual smoke output at `20260703-120312_...mp4` was the first artifact; pytest re-fire produced `20260703-121345_...`).

### Pod

- Provider: RunPod (GraphQL `podFindAndDeployOnDemand`).
- GPU: NVIDIA A100 80GB PCIe.
- Cost rate: ~$1.19/hr.
- Pytest wall-clock: **4 m 35 s** (`1 passed in 275.68s`).
- Manual smoke wall-clock (24th): ~5 m 30 s (provision + fetch + upscale + destroy).
- Ledger post-run: clean (`No running instances.` + `No instances recorded in ledger.`).

### Reproduction recipe deltas vs. #12 (SpandrelEngine)

- **Engine change**: swap SpandrelEngine per-frame architecture-agnostic SR for FlashVSREngine streaming diffusion (Wan 2.1 1.3B backbone with Block-Sparse-Attention).
- **Native scale**: fixed at 4× by upstream `Causal_LQ4x_Proj` weight shape (spandrel was 2× via RealESRGAN).
- **VRAM tier**: 80GB required (spandrel fit A6000 48GB). FlashVSR at 480×480→1920×1920 peaks ~42 GB PyTorch alloc; A6000 OOMs even with tile_size=512.
- **Precision**: bfloat16 (spandrel was fp16).
- **Weights bundle**: `hf:JunhaoZhuang/FlashVSR-v1.1` — 2-file lite bundle (StreamingDMD + Wan2.1_VAE) + `posi_prompt.pth` + `LQ_proj_in.ckpt` + upstream `utils.py` fetched at provision.
- **Runtime deps pinned** to diffsynth 1.1.7's `install_requires` (transformers==4.46.2, safetensors==0.5.3, accelerate==1.8.1, peft==0.16.0, einops==0.8.1, ftfy==6.3.1, sentencepiece==0.2.0).

### Live-smoke debugging chronology (T7.6 sub-plan Task 6)

24 total live smokes across 2026-07-02 and 2026-07-03; 15+ discrete on-pod infra bugs surfaced and fixed. Cumulative spend ~$1.60 (all pods destroyed via `kinoforge destroy` or `--no-reuse`). Full chronology in `PROGRESS.md` § **FlashVSR T7.6 sub-plan** table.

Key learnings for future FlashVSR / diffsynth-based upscalers:

1. **`--no-deps` is mandatory** on FlashVSR git-install — upstream `requirements.txt` pins `torch==2.6.0+cu124` (local +cu124 suffix) which pip cannot resolve.
2. **modelscope is a required runtime dep** even when weights come from HF Hub — diffsynth's downloader.py has a module-top `from modelscope import snapshot_download`.
3. **BSA prebuilt wheel tag is misleading** — `bsa-cu128-torch2.8-v1` was actually linked against `runpod/pytorch:2.8.0-...cuda12.8.1`'s preinstalled torch (2.4.1+cu124 in reality). Reinstalling torch (any version) breaks BSA ABI.
4. **`init_cross_kv()` needs `posi_prompt.pth`** at a hardcoded relative path — pass `context_tensor=` kwarg to bypass.
5. **`Causal_LQ4x_Proj`** must be loaded from upstream `utils.py` (fetched at provision) — the vendored stub in `_input_prep.py` lacks `.clear_cache()` / `.stream_forward()`.
6. **`imwrite` via pyav plugin** needs explicit `codec="libx264"` — imageio v3 defaults to `codec=None` which crashes PyAV.
7. **Server-side traceback logging** in `_run_upscale_job` catch block is essential — client-side `str(exc)` alone strands debugging.
8. **`reader.metadata` is a method** in imageio.v3, not an attribute.
9. **80GB VRAM required** for 480×480 → 1920×1920 4× with `num_persistent_param_in_dit=None` — A6000 48GB OOMs even with tiling.
10. **`DiffusersEngineConfig` schema needs `image` + `pytorch_extra_index_url` fields** — pydantic silently strips undeclared cfg keys.

### Notes

- Test fixture source clip is the 480×480 Wan 2.2 output from entry #8. F-multi (Wan generate → FlashVSR upscale on same pod) and F-warm (LRU-hit second generate) live smokes are still xfail-gated (`KINOFORGE_LIVE_SPEND` env var); firing them is deferred to a follow-up session.
- The 24th manual smoke output at `20260703-120312_upscaled_flashvsr_flashvsr-wan21-bfloat16_upscale.mp4` (35 MB, same tuple) is the FIRST-EVER green FlashVSR generation kinoforge produced; the pytest re-fire at `20260703-121345_...` is the receipt for Task 6 AC.
- **⚠️ QUALITY FLAG (2026-07-04 frame-extraction QA):** every FlashVSR output still on
  disk (all 7 upscales from the 2026-07-03 evening session, entries #13/#14 stack) is
  **visually corrupted** — psychedelic false-color noise with only scene structure
  preserved; unusable as video. This entry's own artifacts (`20260703-1203*/1213*`) were
  deleted before any frame-level inspection, so the original torch-2.4.1/cu128-wheel
  output is unverifiable. Green here means orchestration + dims only, NOT visual output.
- **✅ RESOLVED (2026-07-04 root-cause session, `e82b0d1`):** `_fetch_weights` classified
  `LQ_proj_in.ckpt` as long-video-only, so every `long_video_mode: false` pod (i.e. all
  of them) ran the LQ conditioning projection with RANDOM weights — the runtime's
  upstream-copied `if lq_ckpt.exists()` guard silently skipped the load. Corruption was
  stack-independent (old-stack repro `20260704-220357` equally corrupt). Fix: ckpt moved
  to the base bundle + runtime hard-fails on a missing ckpt. Verified clean:
  `output/20260704-222558_upscaled_flashvsr_...mp4` (1920², 4x of `20260703-220726`,
  frame-QA'd — sharp, color-correct, temporally coherent). See also: 2026-07-04 22:25
  re-fire of this tuple, pod `thtta0gl4zuyo0`, ~3.5 min, destroyed clean.


---

## 14. `2026-07-03 22:10:05` — Wan 2.2 T2V-A14B + FlashVSR 4x co-resident multi-stage + warm-reuse re-generate on RunPod A100 80GB — t2v+upscale

| Field | Value |
|---|---|
| **Stack triple** | `runpod / DiffusersEngine + FlashVSREngine (co-resident, one wan_t2v_server process) / diffusers.WanPipeline @ Wan-AI/Wan2.2-T2V-A14B-Diffusers + diffsynth.FlashVSRFullPipeline @ JunhaoZhuang/FlashVSR-v1.1` |
| **Mode** | t2v + upscale (multi-stage pipeline), then warm-reuse t2v + upscale on the same pod |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `a9f187e` (attempt 13, 2 passed); first pipeline-green run was attempt 9 at `3d15fac` (failed only a test assert) |
| **Date (local TZ)** | 2026-07-03 22:10:05 -0700 (PDT) |
| **Layer / phase** | Parent P4 plan T9 (`docs/superpowers/plans/2026-07-01-flashvsr-video-upscaling.md`) — closes F-multi + F-warm. |

### Exact command

```bash
KINOFORGE_LIVE_SPEND=1 pixi run pytest \
  tests/live/test_flashvsr_live.py::test_f_multi \
  tests/live/test_flashvsr_live.py::test_f_warm \
  -v --tb=long
```

### Cfg

- `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale.yaml` — engine=`diffusers` (Wan 2.2 A14B eager), upscale block engine=`flashvsr`, scale=`4x`, precision=`bfloat16`, tile_size=`512`, `bsa_wheel_url`=`bsa-cu124-torch2.6-v1` wheel, torch trio pinned `2.6.0` (cu124), image `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` on BOTH `engine.diffusers.image` and `compute.image`, `compute.cloud_type: secure`.

### Input

- Standard prompt verbatim from `examples/configs/prompts/field-realistic.txt`; F-warm appends " variant B".

### Output

- F-multi Wan stage: `output/20260703-220726_diffusers_...mp4` — 480×480, sha `29040cb375e4b950…`.
- F-multi FlashVSR stage: `output/20260703-221005_upscaled_flashvsr_...mp4` — **1920×1920** (4×), 37.9 MB, sha `89cb29cadfae67c5…`.
- F-warm Wan stage: `output/20260703-221416_diffusers_...mp4` — 480×480, sha `a638daf6816246e3…`.
- F-warm FlashVSR stage: `output/20260703-221714_upscaled_flashvsr_...mp4` — **1920×1920**, 43.7 MB, sha `c2afd8e6031dbc00…`.

### Pod

- Single pod `cb25udex7bvoq6` for BOTH tests (warm-reuse attach confirmed: `warm-reuse: attached to cb25udex7bvoq6`).
- GPU: NVIDIA A100 80GB (secure cloud, $1.39/hr).
- Wall-clock: `2 passed in 1223.57s` (20 m 23 s) — F-multi cold ~13 min, F-warm warm leg ~7 min.
- Spend ≈ $0.47. Post-run: `kinoforge list` clean + RunPod `myself.pods` empty (explicit `kinoforge destroy` in the test's finally).

### The co-residency VRAM swap (the new capability axis)

One 80 GB card cannot hold Wan 2.2 A14B (~74 GiB resident) and FlashVSR (~9 GiB + inference) together. The T11 LRU registry now swaps them:

1. Startup registers the eager Wan pipe in the registry (`_register_eager_wan`) — previously a module global invisible to eviction.
2. `/upscale` frees headroom BEFORE FlashVSR's constructor runs (`_free_headroom_for`, prefix-sized) — post-load eviction OOM'd.
3. Wan eviction is a **disk-drop** (registry + module-global refs cleared), never `.to("cpu")` — pods pin only 32 GiB min RAM, and `device_map="cuda"` pipes raise on `.to()`.
4. F-warm's generate re-promotes Wan (`_promote_wan_if_evicted` in the sync worker): drops FlashVSR entirely (CPU-parking left ~2-3 GiB CUDA residue that OOM'd the reload at the margin), then reloads Wan from the pod-local HF cache (~4 min).

### Reproduction recipe deltas vs. #13 (F-single)

- **New BSA wheel**: `bsa-cu124-torch2.6-v1` (torch pinned at build time with version assert) — Wan 2.2 needs torch>=2.6 (infer_schema), the old wheel linked the image's torch 2.4.1.
- **peft 0.17.0** (was 0.16.0) — diffusers-latest import floor.
- **transformers window `>=4.48,<5`** (was ==4.46.2) — diffusers needs Dinov2WithRegistersConfig (4.48+), diffsynth needs PretrainedConfig (<5).
- **No `HF_HUB_OFFLINE=1`** in the provision tail on co-resident pods (it put the whole server env offline and killed the Wan eager load); upscale-only cfgs keep it.
- **`compute.cloud_type: secure`** — new cfg surface; community-pool pods were deleted by RunPod minutes into runs all day (schema-migration day), and even secure pods vanished twice (attempts 7, 12) — retry-to-green.

### Notes

- 13 attempts total for T9; every pipeline component was green by attempt 9 — attempts 9-12 burned on two test-harness assert bugs (slug printed only by the upscale subcommand, not generate) and two RunPod mid-run pod deletions.
- Known follow-up: Wan reload on promotion drops any live LoRA adapters (stack-replay needed if LoRA + upscale ever compose); RunPod `GpuAvailability` probe still 400s on the new `GpuTypeFilter` schema (nonfatal, logged).
- **⚠️ QUALITY FLAG (2026-07-04 frame-extraction QA):** both FlashVSR stage outputs
  (`20260703-221005`, `20260703-221714`) — and all 5 other FlashVSR upscales from this
  session — are **visually corrupted**: severe false-color/channel-scramble noise over a
  preserved scene silhouette. The Wan stage outputs (`220726`, `221416`) are clean, and a
  spandrel upscale (`20260630-221907`) through the same encode path is clean, so the
  corruption is FlashVSR-stage specific. The multi-stage + warm-reuse ORCHESTRATION this
  entry records is real; the upscaled pixels are not usable.
- **✅ RESOLVED (2026-07-04, `e82b0d1`):** root cause was a missing `LQ_proj_in.ckpt`
  (mis-gated behind `long_video_mode` in `_fetch_weights`) leaving the LQ conditioning
  projection random-init — NOT the BSA wheel or torch 2.6 (old stack reproduced the same
  corruption). Fixed + verified clean on this entry's own F-multi source clip:
  `output/20260704-222558_upscaled_flashvsr_...mp4` (frame-QA'd sharp/color-correct).
- **See also (re-fire with fix, 2026-07-04 23:15 PDT):** full F-multi + F-warm pytest
  pair re-run green post-fix — `2 passed in 1172.22s`, pod `utbf9k7bp2khuo`, ~$0.40,
  destroyed clean. All four artifacts frame-QA'd CLEAN: `20260704-230446` (Wan) +
  `20260704-230735` (1920² upscale), `20260704-231130` (F-warm Wan) + `20260704-231525`
  (1920² upscale). Evidence:
  `tests/live/evidence/2026-07-04_flashvsr_fix_refire_f_multi_warm_stdout.txt`. This is
  the first visually-verified green for the co-resident multi-stage tuple.

---

## 15. `2026-07-04 00:02:32` — Luma UNI-1 image keyframe via agents API (LumaAgentsImageEngine) — t2i

| Field | Value |
|---|---|
| **Stack triple** | `lumalabs.ai (agents API) / LumaAgentsImageEngine / uni-1` |
| **Mode** | t2i (image keyframe — first image-mode entry in this log) |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `6f933c8` (agents retarget; engine first landed at `857e2f6`) |
| **Date (local TZ)** | 2026-07-04 00:02:32 -0700 (PDT) |
| **Layer / phase** | Layer 5b (Luma keyframe pivot; closes the build half of memory `project_luma_video_retirement_2026`; deletion half was Phase 44). Plugs into Layer R (`keyframe:` block, Phase 32). |

### Exact command

```bash
KINOFORGE_LIVE_SPEND=1 pixi run pytest \
  tests/live/test_luma_keyframe_live.py -v -s
```

### Cfg

- Engine driven directly (registry `"luma_agents"`); example cfg `examples/configs/fal-luma-keyframe-i2v.yaml` pins the same block: `keyframe.engine=luma_agents`, `spec.model=uni-1`, `params.aspect_ratio="16:9"`.
- Wire: `POST https://agents.lumalabs.ai/v1/generations` body `{"prompt", "model": "uni-1", "type": "image", "aspect_ratio": "16:9"}`; poll `GET /v1/generations/{id}`; image at `output[0].url` (pre-signed S3, ~1 h expiry).

### Input

- Standard prompt verbatim from `examples/configs/prompts/field-realistic.txt`.

### Output

- Generation id `a08e47af-e1dc-41bd-9f5b-453911fb4d34`; PNG **2784×1504** (16:9 honoured), 5,923,004 B.
- SHA256 `a672c733f1b629e25b33e418dc711fcdbed84f8545f092688fae1218aaee40aa`.
- Wall-clock `1 passed in 125.60s` (~2 min generation — UNI-1 is autoregressive; slower than the ~31 s the marketing page quotes).
- Evidence: `tests/live/evidence/2026-07-04_luma_keyframe_stdout.txt` (pre-signed query string scrubbed — it carries short-lived AWS tokens).

### Reproduction recipe notes

- Auth: `Authorization: Bearer $LUMAAI_API_KEY` (platform `luma-api-…` key, already in `.env` since 2026-06-07 — still valid).
- The RETIRED `api.lumalabs.ai/dream-machine/v1` surface returns `403 {"detail":"Not authenticated"}` for platform keys — if you see that 403, you are on the wrong host, not holding a bad key.
- No DELETE endpoint on the agents API; records purge via the dashboard (`manual_cleanup_url`).
- Spend: single generation from the $20 platform credit (~$0.01-0.05 range; Luma does not return per-generation cost on the wire).

### Notes

- First `(luma_agents, t2i)` tuple and the first image-keyframe capability in the log; unlocks Layer-R `keyframe:` flows (i2v/flf2v openers) with a second hosted image provider next to fal + replicate.
- E21 (keyframe → hosted i2v end-to-end upload) remains the known gap before the full `fal-luma-keyframe-i2v.yaml` i2v pipeline runs unattended.

---

## 16. `2026-07-04 00:50:21` — Keyframe→video pipeline: Luma UNI-1 keyframe → fal wan-i2v (E21 data-URI hand-off) — i2v

| Field | Value |
|---|---|
| **Stack triple** | `lumalabs.ai agents API (keyframe) + fal.ai queue (video) / LumaAgentsImageEngine → KeyframeStage → FalEngine / uni-1 → fal-ai/wan-i2v` |
| **Mode** | i2v with keyframe pre-stage — FIRST full keyframe→video pipeline in the log; also the first live fal i2v |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `7ea8e96` (E21 data-URI inlining + asset_paths cfg fix) |
| **Date (local TZ)** | 2026-07-04 00:50:21 -0700 (PDT) |
| **Layer / phase** | Closes Phase 32 deferred E21; first live exercise of the Layer R `keyframe:` → `ConditioningAsset` → hosted-engine flow. Phase 43 T14's fal-i2v leg is now live-proven. |

### Exact command

```bash
pixi run -e live-hosted kinoforge generate \
  --config <scratch copy of examples/configs/fal-luma-keyframe-i2v.yaml \
            with keyframe.prompt = the standard prompt> \
  --mode i2v \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)"
```

### Cfg

- `examples/configs/fal-luma-keyframe-i2v.yaml` (scratch copy differed ONLY in prompts — both video + keyframe slots set to the standard prompt for coherence). Key blocks: `keyframe.engine=luma_agents` / `spec.model=uni-1` / `aspect_ratio 16:9`; `engine.fal.endpoint=fal-ai/wan-i2v` with `asset_paths: {init_image: image_url}` (the E21 fix — without it the keyframe never reached the video request).
- Hand-off mechanics: KeyframeStage stores the PNG locally → FalBackend inlines it as a `data:image/png;base64,…` (~7.9 MB body) at submit — accepted by fal first try.

### Input

- Standard prompt verbatim from `examples/configs/prompts/field-realistic.txt` (both stages).

### Output

- Keyframe: `output/20260704-004927_keyframe-init_luma_agents_uni-1_Photorealistic-cinem.png` — 2784×1504, 5,936,684 B, sha256 `f09218af24bf0fa0…`.
- Video: `output/20260704-005021_fal_fal-ai-wan-i2v_Photorealistic-cinem.mp4` — **1280×720, 161 frames**, 3,151,314 B, sha256 `e43a6d1e2baed198…`.
- Wall-clock: 2 m 13 s total (keyframe ~79 s incl. UNI-1 generation, fal i2v ~54 s).
- Evidence: `tests/live/evidence/2026-07-04_keyframe_luma_fal_i2v_stdout.txt`.

### Notes

- Spend: one Luma generation (~cents) + one fal wan-i2v run (fal does not return cost on the wire; list price ~$0.2-0.4 for a 5 s 720p clip).
- flf2v role→field mapping in `fal-keyframe-flf2v.yaml` remains docs-derived and NOT live-verified — only the i2v leg is proven here.
- The published keyframe filename carries the `keyframe-init_luma_agents_uni-1` token chain — Layer 4 sink naming worked unmodified with the new image engine.

---

## 17. `2026-07-04 01:29:58` — flf2v pipeline: dual fal flux-schnell keyframes → fal wan-flf2v — flf2v

| Field | Value |
|---|---|
| **Stack triple** | `fal.ai / FalImageEngine (x2 roles) → KeyframeStage → FalEngine / fal-ai/flux/schnell → fal-ai/wan-flf2v` |
| **Mode** | flf2v with dual-keyframe pre-stage — first flf2v entry; closes the Phase 43 T14 flf2v leg |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `7add3d8` (tree state; E21 seam from `7ea8e96`) |
| **Date (local TZ)** | 2026-07-04 01:29:58 -0700 (PDT) |
| **Layer / phase** | Validates the docs-derived `first_frame→start_image_url` / `last_frame→end_image_url` mapping flagged UNVERIFIED in the E21 close-out. Layer R per-role overrides (distinct prompt+seed per frame) exercised live for the first time. |

### Exact command

```bash
pixi run -e live-hosted kinoforge generate \
  --config examples/configs/fal-keyframe-flf2v.yaml \
  --mode flf2v \
  --prompt "a cat morphing into a tiger, smooth transition"
```

### Cfg

- `examples/configs/fal-keyframe-flf2v.yaml` verbatim (committed shape). Per-role keyframe overrides: first_frame = cat (seed 42), last_frame = tiger (seed 43), both `fal-ai/flux/schnell`.
- Standard-prompt deviation, deliberate: flf2v needs a COHERENT first/last pair; the single-scene standard prompt cannot decompose into two frame prompts. The cfg's purpose-built cat→tiger pair is the test.

### Output

- first_frame: `output/20260704-012858_keyframe-first_fal_unknown_photorealistic-cat-s.png` — 248,223 B, sha `b47910a8fcfeb273…`.
- last_frame: `output/20260704-012900_keyframe-last_fal_unknown_photorealistic-tiger.png` — 307,859 B, sha `5ce83b82420b829d…`.
- Video: `output/20260704-012958_fal_fal-ai-wan-flf2v_a-cat-morphing-into.mp4` — **1280×720, 81 frames**, 1,260,495 B, sha `335aa5cc2daccad8…`.
- Wall-clock: 63 s total (two flux keyframes ~5 s, wan-flf2v ~58 s).
- Evidence: `tests/live/evidence/2026-07-04_keyframe_fal_flf2v_stdout.txt`.

### Notes

- Spend: 2× flux-schnell (~$0.01) + wan-flf2v (~$0.3 list).
- The `_fal_unknown_` token in the keyframe filenames is a bug this run surfaced: `FalImageEngine.model_identity` only read `engine.fal.endpoint` and returned `""` for keyframe sub-cfgs (which carry `spec.model`). Fixed same commit — future runs render `fal-ai-flux-schnell`.
- Both keyframes hand off through the E21 data-URI seam (two ~250-300 KB inline bodies).

---

## 18. `2026-07-04 03:56:00` — Luma UNI-1-MAX keyframes — 4-prompt matrix vs uni-1 — t2i

| Field | Value |
|---|---|
| **Stack triple** | `lumalabs.ai (agents API) / LumaAgentsImageEngine / uni-1-max` |
| **Mode** | t2i (keyframe matrix) |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `60cc9a2` |
| **Date (local TZ)** | 2026-07-04 03:56 -0700 (PDT) |
| **Layer / phase** | Keyframe quality matrix (spends the $20 Luma platform credit per its earmark). New model id in the tuple → new section per the log schema. |

### Exact command

One-off matrix runner (scratchpad ops script) driving
`registry.get_image_engine("luma_agents")` directly: models × the four
standard prompt files in `examples/configs/prompts/`, `aspect_ratio 16:9`.
Manifests: `tests/live/evidence/2026-07-04_luma_matrix_manifest_run{1,2}.json`.

### Results

| model | cells | dims | latency (s, median / range) | size (median) |
|---|---|---|---|---|
| `uni-1` | 4/4 | 2784×1504 | 118 / 102–194 | 6.9 MB |
| `uni-1-max` | 4/4 | 2784×1504 | 137 / 115–186 | 6.6 MB |
| `photon-1` | rejected | — | — | — |

- **photon-1 is DEAD on the agents API**: `HTTP 400 {"detail":"Unknown model: photon-1"}` — the photon family did not carry over from dream-machine. Only the UNI family exists here.
- Objective metrics are near-identical between uni-1 and uni-1-max (same dims, ±15 % latency, comparable size). Visual-quality ranking needs operator eyes: PNG pairs live side-by-side under `output/luma-keyframe-matrix/`.
- Run 1 died mid-uni-1-max on a transient SSL EOF — root-caused and fixed same session (`60cc9a2`: GET polls retry once + map to KinoforgeError; POSTs never retry to avoid double-generation). One orphaned generation likely completed unclaimed server-side (~cents).

### Visual review (2026-07-04, in-session, all 4 pairs)

| prompt | verdict | detail |
|---|---|---|
| field-realistic | style trade-off | uni-1: heavier cinematic grade, denser magical elements, softer subject. uni-1-max: more photoreal (prompt says "Photorealistic"), clearer face, natural waterfall. |
| field-dreamlike | **uni-1-max** | uni-1 shows hair/antler blending artifact on the subject + blown highlights; max is structurally clean with crisper florals and coherent spectral deer. |
| forest | **uni-1** | More dramatic god-ray shaft + sun star; max reads flatter/duskier. |
| dawn-flight | **uni-1-max** | uni-1 has destructive frame-wide out-of-focus blob artifacts (reads as sensor dirt — would propagate into any i2v clip); max's droplet sparkle is confined to the bottom edge. |

### Notes

- Spend: 9 generations total (~$0.3-0.7 of the $20 credit; Luma does not return per-generation cost on the wire).
- **Revised recommendation** (metrics said tie; eyes disagree): `uni-1-max` is the safer keyframe default — fewer destructive artifacts and better subject clarity at +15 % latency. Cfg stays on `uni-1` for now ONLY because max-tier per-image pricing is unverifiable on the wire; flip `fal-luma-keyframe-i2v.yaml` to `uni-1-max` once the dashboard confirms the price delta is acceptable.
- Artifact caution for keyframe→i2v flows: inspect keyframes before spending on the video leg — uni-1's dawn-flight blobs would have seeded a ruined clip.

---

## 19. `2026-07-05 04:16:18` — FlashVSR height-target upscale (scale=1080p → 4x+downscale) on RunPod A100 80GB — upscale

| Field | Value |
|---|---|
| **Stack triple** | `runpod / FlashVSREngine + DiffusersEngine (upscale-only mode) / diffsynth.FlashVSRFullPipeline @ JunhaoZhuang/FlashVSR-v1.1` |
| **Mode** | upscale |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `42c4451` (upscale-only smoke green); feature commits `65120de`..`8438a8b` |
| **Date (local TZ)** | 2026-07-05 04:16:18 -0700 (PDT) |
| **Layer / phase** | Height-target upscaling — spec `docs/superpowers/specs/2026-07-05-height-target-upscale-design.md`, plan `docs/superpowers/plans/2026-07-05-height-target-upscale.md` (Task 6). |

### Exact command

```bash
KINOFORGE_LIVE_SPEND=1 pixi run pytest \
  tests/live/test_flashvsr_height_target_live.py -v -s
```

### Cfg

- `examples/configs/runpod-diffusers-flashvsr-1080p-upscale.yaml` — engine=`diffusers` (upscale_only), upscaler=`flashvsr`, **scale=`1080p`** (height target, NOT a raw factor), precision=`bfloat16`, tile_size=`512`, cloud_type=`secure`, GPU tier=A100/H100 80GB, image=`runpod/pytorch:2.8.0-...cuda12.8.1`.

### Input

- Source clip: `/workspace/output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` — 480×480, 81 frames (Wan 2.2 T2V-A14B, entry #8). Same fixture as #13's F-single.

### Output

- Artifact: `output/20260705-041618_upscaled_flashvsr_flashvsr-wan21-bfloat16_upscale.mp4`
- Dims: **1080×1080** (height-target 1080p; source 480² → FlashVSR 4×=1920² → lanczos downscale → 1080²). Verified via `pixi run ffprobe`.
- Duration 4.81 s, ~2.25 MB, h264 / yuv420p.
- SHA256: `5c5b6c13b086491bf061bb456d7b81f3a67e983f058d519e1ec3a25363f99f9f`

### Frame-QA verdict (5-frame contact sheet)

**PASS — high quality.** Sharp, color-correct golden-hour grade (NOT the pre-`e82b0d1` psychedelic corruption). Prompt-adherent: woman in wildflower meadow, tall waterfall on mossy cliffs, backlit glow, luminous butterflies + light-wisps, colorful dress, glancing over shoulder. Temporally coherent across frames (subject turns, camera glides, no flicker/warp). Crisp 4× detail retained through the downscale.

### Pod

- Provider: RunPod; GPU: A100 80GB; cost rate ~$1.19/hr.
- Pytest wall-clock: **4 m 04 s** (`1 passed in 243.99s`) — vs ~30 min for the render+upscale multi-stage path (no 70 GB Wan A14B download).
- Spend: ~$0.08. Ledger post-run: clean (`--no-reuse` auto-destroy verified via `kinoforge list`).

### Reproduction recipe deltas vs. #13 (FlashVSR 4x)

- **New scale grammar**: `scale: 1080p` (height target) instead of `4x`. `UpscaleStage` resolves it against the engine's `supported_scales=(4x,)` to a concrete 4× factor + a `downscale_to=1080` stashed on the artifact meta; the orchestrator materialize boundary lanczos-downscales the fetched bytes before publishing. Engine-agnostic pure resolver `kinoforge.core.scale_resolver.resolve_height_target`.
- **Delivered size capped**: 1080² deliverable (2.25 MB) vs #13's raw 1920² (34 MB) — the whole point of the height target.

### Infra bugs root-caused + fixed en route (4 live runs, ~$1.6 total)

1. **RunPod create HTTP 500** — total `env` payload >~101 KB (base64 provision script alone 98,848 B → total 101,971). Fixed `5418c35`: gzip the script before base64 (→72 KB), `dockerArgs` decodes `base64 -d | gzip -d`. Hardens every RunPod create.
2. **`ValueError: supported_factors must be non-empty`** — FlashVSR declared the empty accept-any `supported_scales` sentinel; the height resolver needs a factor menu. Fixed `e3c3065`: declare `supported_scales=(4x,)`.
3. **ffmpeg exit 183 on the downscale** — a large mp4 piped via `pipe:0` fails demux (moov atom needs seeking on a non-seekable pipe). Fixed `8438a8b`: write bytes to a seekable temp file, `ffmpeg -i <file>`.
4. **RunPod pod-death mid-run** (run 2, secure host, ~30 min in) — infra, not code. Motivated the pivot to the upscale-only fixture path (`42c4451`): ~4 min / ~$0.08, far shorter pod-death window than render+upscale.

## 20. `2026-07-05 22:24:29` — RIFE v4.26 frame interpolation (16fps→60fps) on RunPod RTX A4000 — interpolate

| Field | Value |
|---|---|
| **Stack triple** | `runpod / RifeEngine + DiffusersEngine (upscale_only mode) / Practical-RIFE RIFE_HDv3 @ hzwer/RIFE RIFEv4.26_0921` |
| **Mode** | interpolate |
| **kinoforge version** | `v0.1.0` |
| **First-success SHA** | `13ec94d` (padding fix — the green run); feature commits `4e5e201`..`13ec94d` |
| **Date (local TZ)** | 2026-07-05 22:24:29 -0700 (PDT) |
| **Layer / phase** | Frame-interpolation stage (RIFE v4) — spec `docs/superpowers/specs/2026-07-05-frame-interpolation-design.md`, plan `docs/superpowers/plans/2026-07-05-frame-interpolation.md` (Task 12, USER-GATE). First interpolate-mode generation. |

### Exact command

```bash
KINOFORGE_LIVE_SPEND=1 pixi run pytest \
  tests/live/test_rife_interpolate_live.py -v -s
```

Equivalent standalone CLI (what the smoke drives):

```bash
pixi run kinoforge interpolate \
  --config examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml \
  --video output/20260630-221857_..._Photorealistic-cinem.mp4 \
  --fps 60 --no-reuse
```

### Cfg

- `examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml` — engine=`diffusers` (`upscale_only: true` = skip eager Wan load), interpolator=`rife`, **fps=`60.0`**, `rife.weights_ref=hf:hzwer/RIFE`, `model=rife426`, precision=`fp16`, `compute.cloud_type=secure`, GPU tier RTX A4000/4090/A5000, image=`runpod/pytorch:2.4.0-...cuda12.4.1`.
- `embed_modules` includes `kinoforge.interpolators.rife`; `embed_files` add `kinoforge.core.fps_resolver` + `kinoforge.core.frames`.

### Input

- Source clip: `/workspace/output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` — 480×480, **81 frames, 16 fps, 5.062 s** (Wan 2.2 T2V-A14B, entry #8). Same fixture as #13/#19.

### Output

- Artifact: `output/20260705-222429_interpolated_rife_interp_interpolate.mp4`
- Dims **480×480** (unchanged — interpolation is temporal only), **60.000 fps**, **304 frames**, 5.067 s, ~0.84 MB, h264 / yuv420p.
- Frame count = `round(5.0625 s × 60) = 304`; duration matches the source within 0.005 s (≈0.08 frame). Verified via `ffprobe_fps` + `nb_read_packets`.
- SHA256: `bbb6968c27035851481bdc709ac93c6707442bf886ac58b353c6a2cec563cc73`

### Frame-QA verdict (contact sheet + adjacent-frame check)

**PASS — high quality, no ⚠️.** 6-frame sweep: sharp, temporally coherent (woman turning in a wildflower meadow, tall backlit waterfall, luminous butterflies + light-wisps), prompt-adherent, no warping/ghosting/false-color. **Interp-specific check:** two *consecutive* 60 fps frames (n=150, 151) show a small, smooth pose delta — a genuine synthesized midpoint, NOT a duplicated/held frame and NOT a ghosted double-exposure. Confirms real 3.75× arbitrary-timestep synthesis (16→60), not frame-repeat padding.

### Pod

- Provider: RunPod; GPU: RTX A4000; `cloud_type: secure`.
- Pytest wall-clock: **1 m 10 s** (`1 passed in 69.62s`) — fast: no torch reinstall (base image), ~22 MB model bundle, 81→304-frame interp is seconds on the GPU.
- Spend: ~$0.02 (this run). Ledger post-run: clean (`--no-reuse` auto-destroy verified via `kinoforge list` → no instances + empty ledger).
- Evidence: `tests/live/evidence/2026-07-05_rife_interpolate_stdout.txt`.

### Reproduction recipe (new capability axis: interpolate mode + RifeEngine)

- **Engine-agnostic resolver** `kinoforge.core.fps_resolver.resolve_fps_target` maps `(source_fps, target_fps, ARBITRARY_TIMESTEP)` → a per-output-frame `(source_index, timestep)` schedule (t=0 copies a source frame, else synthesize). 16→60 over 304 frames.
- **Standalone command** `kinoforge interpolate --video … --fps …` reuses `generate(skip_clip_stage=True, initial_clip=…)` — its own RIFE pod, structurally identical to `kinoforge upscale --video`. Upscale→interpolate ordering (when both wanted) is realized by chaining the two commands, NOT single-pass (see plan Planning-time correction).
- **On-pod**: `RifeRuntime` decodes frames (imageio/FFMPEG), pads each to a multiple of 64 for IFNet, runs `Model.inference(f0, f1, t)`, crops back, muxes at 60 fps. Server `/interpolate` + `/interpolate/status/{id}` mirror `/upscale`.

### Infra bugs root-caused + fixed en route (6 live boots, ~$0.11 total)

1. **RunPod host reclaim mid-provision** (boot 1) — pod terminated during `wait_for_ready` → `KeyError: no RunPod pod found`. Transient, not code; `kinoforge forget` + retry.
2. **`huggingface-cli download` deprecated → exit 1** (boot 2) — killed the bootstrap under `set -euo pipefail` (`[bootstrap-trap] rc=1`, pod idle at 0% GPU/CPU). Fixed → later dropped for the zip path.
3. **`ffprobe not found on PATH`** (boot 3) — base `runpod/pytorch` image ships no system ffmpeg; the runtime shells out to `ffprobe`. Fixed `a75622f`: `apt-get install -y ffmpeg unzip`.
4. **`No module named 'train_log.RIFE_HDv3'`** (boot 4) — the RIFE v4 arch (`RIFE_HDv3.py`, `IFNet_HDv3.py`, `refine.py`) ships INSIDE the model release zip, NOT the git repo; cloning alone leaves `train_log/` empty. Fixed `9810052`: `curl` the `RIFEv4.26_0921.zip` bundle + unzip its contents into `<repo>/train_log/`; load `flownet.pkl` from there; also fixed a `.to(model.device)` bug (device is a *method*).
5. **`Expected size 512 but got size 480`** (boot 5) — RIFE v4's IFNet flow pyramid needs H/W padded up to a multiple of 64 (480→512). Fixed `13ec94d`: pad before `inference`, crop back after → **GREEN** (boot 6).
- **Monitoring lesson**: boots 1–2 were watched with a monitor that polled only `est_spend` (wall-clock, GPU-blind) — it missed the 0%-GPU stall for ~12 min until the operator asked. CLAUDE.md "Live smoke monitoring" now hard-bans spend-as-health and mandates the `gpuUtilPercent/cpuPercent/memoryPercent` probe.

---

## 21. `2026-07-08 01:33:54` — FlashVSR upscale on Lambda A100 via SkyPilot ssh-tunnel (provider-internal HTTP seam) — upscale

| Field | Value |
|---|---|
| **Stack triple** | `skypilot(lambda) / DiffusersEngine(FlashVSR) / flashvsr-wan21-bfloat16` |
| **Mode** | upscale (FlashVSR 4x-native → 1080p height-target) |
| **New capability axis** | First kinoforge generation on a **SkyPilot-provisioned** GPU, driven over the **provider-internal `ssh -L` tunnel HTTP seam** (`SkyPilotProvider.create_instance` opens `ssh -L <local>:localhost:8000` and returns `endpoints={"8000":"http://127.0.0.1:<port>"}`, exactly where RunPod returns a proxy URL — the diffusers engine's `wait_for_ready`/`http_get`/`generate` run unchanged). Slice-1 of the SkyPilot vast video-gen plan. |
| **First-success SHA** | `78279b0047f3c6c38113ac317537dc5dfccfd873` |
| **Date (local TZ)** | 2026-07-08 01:33:54 -0700 (PDT) |
| **Plan / spec** | `docs/superpowers/plans/2026-07-07-skypilot-vast-video-gen.md`, `docs/superpowers/specs/2026-07-07-skypilot-vast-video-gen-design.md` |

### Exact command

```bash
pixi run -e live-skypilot kinoforge upscale \
  --config examples/configs/skypilot-lambda-diffusers-flashvsr-upscale.yaml \
  --video output/flashvsr-fixture-41f-288sq.mp4 \
  --no-reuse
```

### Output

- Published: `output/20260708-013354_upscaled_flashvsr_flashvsr-wan21-bfloat16_upscale.mp4`
- ffprobe: **1080×1080**, 16 fps, ~2.3 s, h264/yuv420p — decode rc 0.
- SHA-256: `59654c56d566cf3e009eca75b5c102adea18cd9037f078522d595bd7ec525937`
- **Frame-QA (5 frames): PASS** — coherent photorealistic meadow/waterfall scene with a figure + glowing butterflies; sharp grass/flower/hair detail, temporally consistent across frames, no false-color/psychedelic artifacts, no visible tile seams.

### GPU / cost

- Lambda `gpu_1x_a100_sxm4` (A100 40GB, us-east-1), $1.99/hr. Session live spend ≈ $3 across the slice-1 bring-up (many capacity-miss/OOM iterations, each torn down).
- GPU polled during the run; 0% seen only during the FlashVSR weight-fetch (network-bound, expected — not a stall). The successful 1080p output confirms real GPU inference ran (not a 0%-GPU stall).

### Reproduction notes / deviations (read before re-firing)

- **Lambda, not vast (approved pivot).** The plan targeted vast.ai, but vast's instance-**list** API (`/api/v0/instances`) returns **410 Gone** and vastai-sdk 0.2.5's `show_instances()` returns empty, so sky's vast readiness poll (`list_instances`) never sees the launched instance and waits forever — a *second* incompatibility beyond the `VastAI().client.api_key` shim (Task 0). The vast shim + `sitecustomize` server-process shim + launch-cloud pin are all committed and did get vast to actually **launch** a real A100 (instance ran) before the list-API wall. User approved pivoting the live proof to Lambda (sky-native); vast stays deferred pending an upstream sky/SDK fix.
- **Source downscaled to 288² to fit A100 40GB.** FlashVSR's 4x path peaks >40GB at a 1920² output (the reference RunPod cfg pins 80GB); the FullPipeline **ignores `window_size` and treats `tile_size` as on/off only**, and the peak is resolution-bound (a ~5.27GB 1920²-sized buffer), not frame-bound — so trimming frames or shrinking tiles did **not** help. Lambda's 48GB A6000 was capacity-dry all session. The fit lever is output resolution: a 288² source → 4x = 1152² on the GPU (fits 40GB) → downscaled to the cfg's 1080p target. `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (via an `env` argv prefix in `server_cmd`) reclaims fragmentation and is kept in the cfg.
- **Teardown bug (`--no-reuse`):** after `generate completed`, the driver hung instead of destroying the pod — `HeartbeatLoop._tick_once` throws `AttributeError: 'SkyPilotProvider' object has no attribute 'last_heartbeat'` (the B5a heartbeat substrate expects a method SkyPilotProvider doesn't implement; triggered because validation auto-set `heartbeat_interval_s=30`). Pod + tunnel were destroyed manually (`sky down` + `kill <ssh -L pid>` + `kinoforge forget`); ledger verified clean. **Follow-up:** implement `SkyPilotProvider.last_heartbeat` (or gate the heartbeat loop off for providers lacking it) so `--no-reuse` tears down cleanly.

---

## 22. `2026-07-08 22:12:07` — Diffusers WanPipeline Wan 2.1 T2V-1.3B on Modal serverless GPU (A10) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `Modal / DiffusersEngine / Wan-AI/Wan2.1-T2V-1.3B-Diffusers` |
| **Mode** | t2v |
| **New capability axis** | **New provider — Modal serverless GPU** (first non-RunPod/non-sky/non-hosted compute provider; the diffusers `wan_t2v_server` runs on a Modal `@modal.web_server(8000)` via the SAME `provision_script; exec run_cmd` bundle as RunPod — Option-A generic reuse) |
| **First-success SHA** | `1126f93b9c2f9596c110a61e3782b6b704f7f0a3` |
| **Date (local TZ)** | 2026-07-08 22:12:07 -0700 (PDT) |
| **GPU** | Modal `A10` (24 GB), serverless; preference-first offer from the Modal catalog |
| **Wall clock** | ~10 m 31 s cold (deploy 22:01:41 → provision/pip-install torch+diffusers → weight download → eager `WanPipeline.from_pretrained` → generate → destroy 22:12:12) |
| **Est. spend** | ~$0.19 (A10 @ $1.10/hr × ~0.175 h; Modal serverless per-second) — within the $30 Modal credit |
| **Layer / phase** | Modal-provider spec 1 (Milestone 1) — `docs/superpowers/plans/2026-07-08-modal-provider.md` Task 8 |

### Exact command

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml \
  --mode t2v \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --no-reuse
```

### Artifact

| Field | Value |
|---|---|
| **Published path** | `output/20260708-221207_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` |
| **Internal uri** | `.kinoforge/run-20260708-220141/743cb2c5e3620187.mp4` |
| **Dimensions** | 480×480, 33 frames, 16 fps (ffprobe-equivalent via imageio: shape `(33, 480, 480, 3)`) |
| **Size / SHA-256** | 305,079 B / `743cb2c5e3620187e08bfe1210dc3532025ac1926692a50f1dc3afbb0b7713ae` |

### Frame-QA verdict (mandatory visual review)

**PASS — clearly high quality.** 5 frames extracted (`ffmpeg_frames_by_count`, indices 0/2/4 eyeballed). Coherent alpine meadow of wildflowers, tall waterfall down mossy cliffs, golden-hour backlight + sun flare; a woman (back to camera) standing in the meadow facing the waterfall; glowing butterflies / light-wisps drifting. Strong prompt adherence; no corruption, no false-color, no visible artifacts. Temporal coherence good for a ~2 s clip (slow push-in — mid/late frames near-identical, expected). Frames at `/tmp/.../scratchpad/modal_m1_frame{0..4}.png`.

### Reproduction notes / deviations (read before re-firing)

- **Image MUST be Python-3.13** (`python:3.13-slim`, NOT the RunPod `runpod/pytorch:...py3.11`). Modal's `serialized=True` web-server function (required so the container never has to import kinoforge) rejects a serialized-fn/image **Python-minor mismatch** — the controller (`live-modal` env) is 3.13 (`requires-python >=3.12,<3.14` forbids dropping to 3.11), so the image minor must match. The provision then pip-installs torch 2.6.0 (cu124 cp313 wheels bundle CUDA + libgomp) + diffusers; `imageio[ffmpeg]` bundles ffmpeg. No CUDA base image needed — Modal supplies the driver, torch wheels supply the runtime.
- **`add_python` must be OMITTED.** `Image.from_registry(tag, add_python=...)` runs `ln -s .../python3 /usr/local/bin/python`, which fails (`File exists`) on any image that already ships Python. `ModalAppRequest.add_python` defaults `None` (omit).
- **Boot payload is gzip-chunked across Secret keys.** Modal caps a Secret value at 32768 bytes; the embedded-server provision payload is ~50 KB base64. It is gzipped then split across `KINOFORGE_PROVISION_B64_<i>` keys (+ `NCHUNKS`), reassembled + gunzipped in the container. Wan 1.3B → 2 chunks (30000 + 7468).
- **`modal app list --json` names the app under `description`, not `name`,** and keeps stopped apps listed — `list_instances`/`destroy` read `description`(-or-`name`) and treat "gone" as "no longer `deployed`/`running`".
- **Teardown verified** post-run: `kinoforge list` → `No running instances` + `No instances recorded in ledger`; `modal app list` active `[]`.

---

## 23. `2026-07-08 23:55:31` — Diffusers WanPipeline Wan 2.2 T2V-A14B on Modal serverless GPU (A100-80GB) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `Modal / DiffusersEngine / Wan-AI/Wan2.2-T2V-A14B-Diffusers` |
| **Mode** | t2v |
| **New capability axis** | **Big-model gen on Modal 80GB** (Milestone 2) — dual-14B MoE (`transformer/` high-noise + `transformer_2/` low-noise, ~56 GB bf16) on a Modal A100-80GB via the SAME Milestone-1 transport (serialized `@modal.web_server(8000)`, gzip-chunked Secret boot payload, `python:3.13-slim`). First model on Modal needing an 80 GB card — the axis M1's 24 GB A10 could not reach. |
| **First-success SHA** | `7b820a1c2b80501c5b7707ba9c4d4294bbb1cad1` |
| **Date (local TZ)** | 2026-07-08 23:55:31 -0700 (PDT) |
| **GPU** | Modal `A100-80GB` (80 GB), serverless; preference-first offer from the Modal catalog ($2.50/hr snapshot) |
| **Wall clock** | ~27 m 02 s cold (deploy 23:28:34 → provision/pip torch+diffusers → ~63 GB HF snapshot download → `WanPipeline.from_pretrained` MoE load → generate → destroy 23:55:36). Survived one mid-download Modal worker **preemption** (auto-restarted, recovered). |
| **Est. spend** | ~$1.13 this run (A100-80GB @ $2.50/hr × ~0.45 h) + ~$0.46 on the first (startup-timeout-killed) attempt + ~$0 CPU probes ≈ **~$1.60 cumulative** — within the $30 Modal credit |
| **Layer / phase** | Modal-provider Milestone 2 — `docs/superpowers/plans/2026-07-08-modal-milestone2-wan22-a14b.md` Task 2 (spec `docs/superpowers/specs/2026-07-08-modal-milestone2-wan22-a14b-design.md`) |

### Exact command

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-diffusers-wan-2_2-14b-t2v.yaml \
  --mode t2v \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --no-reuse
```

### Artifact

| Field | Value |
|---|---|
| **Published path** | `output/20260708-235531_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` |
| **Internal uri** | `.kinoforge/run-20260708-232834/ba16e42341e88ef7.mp4` |
| **Dimensions** | 480×480, 81 frames, 16 fps (ffprobe: width=480 height=480 nb_frames=81 r_frame_rate=16/1) |
| **Size / SHA-256** | 1,656,317 B / `ba16e42341e88ef7aa8a33994a9ffdde1800a04d12f5e1f33025f7f159cdfb99` |

### Frame-QA verdict (mandatory visual review)

**PASS — clearly high quality.** 5 frames extracted (`ffmpeg_frames_by_count`, indices 0/2/4 eyeballed). Alpine meadow of red/yellow wildflowers, tall waterfall down moss-covered cliffs into a misting pool, golden-hour backlight + anamorphic lens flare, volumetric god rays; a young woman in a vivid blue-and-red dress. Narrative arc holds across the clip: frame 0 back-to-camera facing the waterfall → frame 2 turning → frame 4 turned toward camera (the prompt's "glance over her shoulder → close-up"). Glowing butterflies + light-wisp ribbons drifting. Strong prompt adherence; no corruption, no false-color, no visible artifacts; temporal coherence good. Frames at `/tmp/.../scratchpad/m2qa_{0..4}.png`.

### Reproduction notes / deviations (read before re-firing)

- **THE FIX that made this work (`7b820a1`): container-init `startup_timeout` on `@app.function`, not `@web_server`.** With `serialized=True`, Modal **drops** the `@modal.web_server(8000, startup_timeout=...)` value and governs the container-init window by the *function's* `startup_timeout`, which itself defaults to `timeout` (300 s). A ~63 GB A14B download takes ~30 min, so the first attempt was killed at exactly 300 s (`Runner has been initializing for too long: 300 seconds`). Milestone 1's 1.3B downloaded **under** 300 s, which is why this only surfaced at A14B. `build_modal_app` now sets `startup_timeout=req.startup_timeout_s` AND `timeout=req.startup_timeout_s` on `@app.function`, mapped from `lifecycle.boot_timeout` (45 m → 2700 s). Verified with two CPU probes (bind-after-320 s): timeout on `@web_server` alone → killed at 300 s; timeout on `@app.function` → survived to 320 s bind.
- **Modal pooled GPUs can preempt mid-boot.** This run took one `Runner interrupted due to worker preemption. Your Function will be restarted with the same input` during the download; it auto-restarted and recovered. **No HF weight caching is wired yet** — the Volume `kinoforge-hf-cache` is mounted at `/cache/hf` but nothing sets `HF_HOME` there, so a preemption restart re-downloads from scratch. A repeated-preemption run could exhaust `boot_timeout` (45 m). **Follow-up:** set `HF_HOME=/cache/hf` so downloads persist across restarts AND future boots are fast (deferred as a non-goal in the M2 spec; the preemption risk argues for promoting it).
- **Config = M1 transport + RunPod A14B model/hardware.** `bf16`, model `Wan-AI/Wan2.2-T2V-A14B-Diffusers` (the `-Diffusers` variant — sharded `from_pretrained`; bare repo 404s), `min_vram_gb: 80`, `gpu_preference: [A100-80GB, H100]`, `disk_gb: 150`, 81 frames, `boot_timeout: 45m`. Same `python:3.13-slim` image + torch 2.6 cu124 pip stack + gzip-chunked Secret boot payload as M1 (carry all four M1 gotchas).
- **Teardown verified** post-run: `kinoforge list` → `No running instances` + `No instances recorded in ledger`; `modal app list` shows `kinoforge-run-20260708-232834` = `stopped`.

---

## 24. `2026-07-10 23:52:23` — FlashVSR v1.1 4x upscale on Modal A100-80GB via image-bake fast boot (Milestone 3) — upscale

| Field | Value |
|---|---|
| **Stack triple** | `Modal / DiffusersEngine (FlashVSR upscaler, composed) / JunhaoZhuang/FlashVSR-v1.1` |
| **Mode** | upscale (480×480 → 1920×1920, native 4x) |
| **New capability axis** | **Modal fast-boot via image-bake** (Milestone 3). The heavy provision steps — pip torch 2.6 cu124, the 526 MB **cp313** Block-Sparse-Attention wheel, and the FlashVSR weights — are BAKED into the Modal image at BUILD time (`Image.run_commands`), so the container boots in seconds instead of running a ~15 min runtime provision. That closes the preemption window that killed the 2026-07-09 attempt (Modal preempted the pooled A100 repeatedly mid-boot → `/health` never bound → 10-container pile-up, never converged). First kinoforge run to split `render_provision` into a bakeable `build_script` + a fast `runtime_script` and have a provider bake the former. Also the first FlashVSR success on Modal (M1/M2 were Wan t2v; this is the FlashVSR/BSA/cp313 axis on Modal). |
| **First-success SHA** | `22793a668da9c8a2e8d931e9a875c311ec6fbb1d` |
| **Date (local TZ)** | 2026-07-10 23:52:23 -0700 (PDT) |
| **GPU** | Modal `A100-80GB` (80 GB), serverless; preference-first offer from the Modal catalog |
| **Wall clock** | Image bake (one-time, cached, CPU builder): 350 s for the deps/wheel/weights layer + 29 s for the apt layer. Then **fast boot**: provision→result **~97 s** on the GPU (provision 23:50:46 → artifact 23:52:23). NO preemption loop, NO container pile-up (contrast 2026-07-09: 10 containers, never converged). |
| **Est. spend** | ~$0.15–0.20 GPU (A100-80GB, ~2–3 min live) + ~8 CPU-only failed image builds (near-zero) while clearing the slim-image gaps below. Cumulative well under $1 — within the Modal credit. |
| **Layer / phase** | Modal fast-boot image-bake — `docs/superpowers/plans/2026-07-10-modal-fast-boot-image-bake.md` Task 10 (spec `docs/superpowers/specs/2026-07-10-modal-fast-boot-image-bake.md`). Unblocks M3 Task 5. |

### Exact command

```bash
pixi run -e live-modal kinoforge upscale \
  --config examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml \
  --video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 \
  --no-reuse
```

### Artifact

| Field | Value |
|---|---|
| **Published path** | `output/20260710-235223_upscaled_flashvsr_flashvsr-wan21-bfloat16_upscale.mp4` |
| **Source (480² sibling)** | `output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` (from §23-lineage Wan 2.2 A14B) |
| **Dimensions** | 1920×1920, 77 frames, 4.81 s (ffprobe: width=1920 height=1920 nb_frames=77) |
| **Size / SHA-256** | 6,419,912 B / `179875588b067e270797b9defe9d03e50f0d7d092add23783284e1b056047352` |

### Frame-QA verdict (mandatory visual review)

**PASS — clearly high quality.** 5 frames extracted per clip (output + 480² source); output frames 1/3/5 + source frame 1 eyeballed. Alpine meadow of yellow/white wildflowers, tall waterfall down moss cliffs, golden-hour backlight, glowing butterflies + light-wisp ribbons; young woman in a red/blue dress. Faithful 4x upscale of the 480² source: same composition, sharpened flower/hair/dress detail, NO invented content, NO color shift. Temporal coherence good (woman turns from back-to-camera → toward camera across the clip). **No corruption, no false-color** — a clean contrast with the §13/§14 RunPod FlashVSR psychedelic-garbage failures (root-caused later to the `LQ_proj_in.ckpt` gating bug, fixed `e82b0d1`). Frames at `/tmp/.../scratchpad/qa_{out,src}_*.png`.

### Reproduction recipe / deviations (read before re-firing)

The fast-boot mechanism + every slim-image gap it exposed (one failed CPU build per gap; the build fails FAST + cheap, unlike a silent boot hang):

- **The bake mechanism (`ModalProvider`).** `build_modal_app` bakes `image_build_script` into the image and the container boots with `runtime_provision_script` only. Modal emits each `run_commands` arg as a Dockerfile `RUN`, and the Dockerfile parser terminates a RUN at any bare newline BEFORE a shell sees it — so a raw multi-line string (`'mkdir' Dockerfile command is not supported`) and a quoted `bash -c '<multi-line>'` (`Unterminated quoted string`) BOTH fail. Fix: encode the whole build script to one newline-free base64 blob and `echo <b64> | base64 -d | bash` in a single RUN (same trick the module-embed lines use); bash then runs it with `set -e`/exports/PYTHONPATH intact.
- **`python:3.13-slim` is a bare Debian** — the composed FlashVSR bake needs tools it lacks. `ModalProvider` now `apt_install`s them before the bake: **curl** (BSA wheel + weights fetch), **git** (FlashVSR `pip install git+https`), **build-essential + cmake + pkg-config** (`sentencepiece==0.2.0` has no cp313 wheel → source build via `build_bundled.sh`). RunPod's pytorch base (py3.11) ships all of these AND the wheels, so this is a slim-only gap — RunPod behavior is unchanged.
- **setuptools:** slim's pip ships none, and modern setuptools (**≥81**) REMOVED `pkg_resources` which FlashVSR's `setup.py` imports. Cfg pins `setuptools<81` + `wheel`, and the FlashVSR git install uses **`--no-build-isolation`** so its wheel build reuses that main-env setuptools (pip's default isolation would build in a fresh env lacking it).
- **The embed must be in BOTH build and runtime phases.** The composed weights fetch runs `python -m kinoforge.upscalers.flashvsr._fetch_weights`, which resolves only against the embedded `/tmp/kfsrv` tree + `PYTHONPATH` — so the embed lands in `build_script` (before the fetch) AND `runtime_script` (for the server). The combined `script` is byte-identical (embed appears once), so RunPod + the golden are unaffected.
- **cp313 BSA wheel.** Modal's serialized web-server fn forces `python:3.13-slim` (image-Python == controller 3.13), but BSA's prebuilt wheels are cp311; this cfg points `bsa_wheel_url` at the cp313 wheel built on Modal (`tools/build_bsa_wheel_modal.py`), torch 2.6.0+cu124. The BSA SM80 guard now no-ops when `torch.cuda.is_available()` is False so the wheel bakes on the CPU image builder; it still enforces SM80+ at runtime.
- **`--extra-index-url None` is harmless** (the cfg leaves `pytorch_extra_index_url` unset → pydantic default None → literal `None`): pip logs `Location 'None/...' is ignored` and resolves torch 2.6.0 from PyPI (cu124 default on linux). Same as M1/M2.
- **Teardown verified** post-run: log `--no-reuse: destroyed + forgot pod upscale-20260710-234415`; then `kinoforge list` → `No running instances` + `No instances recorded in ledger`; `modal app list` → no running kinoforge app.

---

## 25. `2026-07-11 17:59:51` — RIFE v4.26 frame interpolation (16fps→60fps) on Modal T4 via image-bake fast boot (Milestone 4) — interpolate

| Field | Value |
|---|---|
| **Stack triple** | `Modal / DiffusersEngine (RIFE interpolator, composed) / hzwer/RIFE RIFEv4.26` |
| **Mode** | interpolate (480×480, 16 fps 81f → 60 fps 304f; temporal-only, dims unchanged) |
| **New capability axis** | **RIFE interpolation on Modal via the M3 image-bake fast boot** (Milestone 4) — closes the Modal engine matrix (t2v §22/§23 · upscale §24 · **interpolate** here). Pure-config: the Task-6 provision split already routes the composed `interpolate.engine` (RIFE) provision into `build_script`, and `ModalProvider` already bakes it — so RIFE-on-Modal fast-boots with NO provider/engine change. First interpolate-mode generation on Modal, and first RIFE bake on the slim py3.13 image. |
| **First-success SHA** | `e819248` (the embed fix — the green run). Feature commits: `c01a515` (cfg + offline split test), `cdbc317` (RED live scaffold, pre-spend), `e819248` (embed `kinoforge.core.frames` + regression assertion). |
| **Date (local TZ)** | 2026-07-11 17:59:51 -0700 (PDT) |
| **GPU** | Modal **T4** (16 GB), serverless. T4-first `gpu_preference: [T4, L4, A10G]` + `min_vram_gb: 16` → catalog picks T4 (`$0.59/hr` snapshot). RIFE needs ~2 GB VRAM, no SM80 — T4 is ample and cheapest. |
| **Wall clock** | This run **rebuilt the image** (the embed-list change busts the single base64 bake RUN → full apt+pip+numpy-source-build re-ran, ~3.5 min CPU builder). Then **fast boot**: provision→result **~42 s** on the T4 (provision 17:59:02 → artifact 17:59:44). NO preemption loop. A same-embed re-fire would skip the rebuild (cached layer) and boot in seconds. |
| **Est. spend** | ~$0.01 GPU (T4 ~53 s live, provision→destroy) + a first failed T4 attempt (~20 s to the `core.frames` import error, torn down) + near-zero CPU image builds. Total well under $0.05 — within the Modal credit. |
| **Layer / phase** | Modal Milestone 4 — plan `docs/superpowers/plans/2026-07-11-modal-milestone4-rife.md` Task 2 (spec `docs/superpowers/specs/2026-07-11-modal-milestone4-rife-design.md`). Rides the M3 fast-boot bake (§24). USER-GATE. |

### Exact command

```bash
pixi run -e live-modal kinoforge interpolate \
  --config examples/configs/modal-diffusers-rife-60fps-interpolate.yaml \
  --video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 \
  --fps 60 --no-reuse
```

### Cfg

- `examples/configs/modal-diffusers-rife-60fps-interpolate.yaml` — `compute.provider=modal` (NO `cloud:` key — SkyPilot-only), `image=python:3.13-slim`, interpolator=`rife`, **fps=`60.0`**, `rife.weights_ref=hf:hzwer/RIFE`, `model=rife426`, precision=`fp16`, `upscale_only: true` (skip eager Wan load — pod runs only the on-demand RIFE runtime), `models: []`.
- **torch is ADDED vs the RunPod RIFE cfg** (§20): slim ships no torch, so `pip:` carries `torch==2.6.0` + `torchvision==0.21.0` (cu124 cp313, stack-consistent with M3). No ABI lock (RIFE has no compiled ext like BSA).
- **`embed_files` must include `kinoforge.core.frames`** — see the deviation below; the RIFE runtime imports `ffprobe_fps` from it. Also embeds `kinoforge.core.errors` + `kinoforge.core.fps_resolver`; `embed_modules` = `kinoforge.engines.diffusers.servers` + `kinoforge.interpolators.rife`.

### Input

- Source clip: `/workspace/output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` — 480×480, **81 frames, 16 fps, 5.062 s** (Wan 2.2 T2V-A14B, §23 lineage). Same fixture as §20/§24.

### Output

| Field | Value |
|---|---|
| **Published path** | `output/20260711-175951_interpolated_rife_interp_interpolate.mp4` |
| **Dimensions** | **480×480** (unchanged — interpolation is temporal only), **r_frame_rate=60/1**, **304 frames** (ffprobe: width=480 height=480 r_frame_rate=60/1 nb_frames=304) |
| **Frame math** | `round(5.0625 s × 60) = 304` = 3.75× the 81-frame source; matches §20's RunPod RIFE result exactly. |
| **Size / SHA-256** | 879,994 B / `fed84f7751f07ecd19f9d194914fe29efd9823a9b4ecc86508145fd4a8ed9ff6` |

### Frame-QA verdict (spread sweep + adjacent-frame check)

**PASS — clearly high quality, no ⚠️.** Spread sweep (5 frames, every 60th) + 6 consecutive mid-clip frames (n=150–155) eyeballed. Woman in a red/blue dress turning in a backlit wildflower meadow, tall waterfall down moss cliffs, golden-hour light, glowing butterflies + light-wisps — sharp, prompt-adherent, no warping/ghosting/false-color. **Interp-specific check:** consecutive 60 fps frames (150/152/154) show small, smooth pose deltas (torso/head rotation, hair) — genuine synthesized midpoints, NOT duplicated/held frames and NOT ghosted double-exposures. Confirms real 3.75× arbitrary-timestep synthesis (16→60), not frame-repeat padding. Frames at `/tmp/.../scratchpad/rifeqa_{spread,consec}_*.png`.

### Reproduction recipe / deviations (read before re-firing)

- **numpy<2 source-build on py3.13 — SUCCEEDED, no cfg change (the #1 pre-run risk).** RIFE pins `numpy<2`; py3.13 has no cp313 wheel for numpy 1.26.4, so pip built it from source (`numpy-1.26.4.tar.gz` → `numpy-1.26.4-cp313-cp313-linux_x86_64.whl`). The `build-essential` that `ModalProvider` already `apt_install`s (for the M3 FlashVSR bake — carried here as harmless ~200 MB waste per the pure-cfg decision) supplied the toolchain, so it "just built." The build fails FAST + cheap at IMAGE-BUILD time (no GPU spend) if it ever can't compile — it didn't.
- **Embed gap — the one live-caught bug (fixed `e819248`).** The first live attempt failed on the GPU with `InterpolationError: ... No module named 'kinoforge.core.frames'`: the RIFE runtime (`interpolators/rife/_runtime.py`) imports `ffprobe_fps` from `kinoforge.core.frames`, but the M4 cfg (mirroring §24's FlashVSR embed list) OMITTED it. The working RunPod RIFE cfg (§20) DOES embed it. Fix: add `kinoforge.core.frames` to `embed_files` + an offline regression assertion (`"core/frames.py" in build_script`) that would have caught it pre-spend. `core.frames` only needs `core.errors` (already embedded) + stdlib — no transitive gap. **Lesson:** when cloning a Modal cfg's embed list across engines, diff it against that engine's own working (RunPod) cfg — the embed set is engine-specific.
- **The bake mechanism is unchanged from §24** — `ModalProvider` bakes `build_script` (RIFE `git clone` Practical-RIFE + `numpy<2` + `RIFEv4.26` weights + torch) into the image via one base64 `RUN`, boots with `runtime_script` (the `wan_t2v_server` exec) only. The single-RUN base64 encoding means ANY build-phase change (like the embed-list edit) busts the whole layer → full rebuild; a same-content re-fire is cached.
- **`--extra-index-url None` is harmless** (same as §22–§24): `pytorch_extra_index_url` unset → literal `None`, pip logs `Location 'None/...' is ignored`, resolves torch 2.6.0 from PyPI (cu124 default on linux).
- **Teardown verified** post-run: log `--no-reuse: destroyed + forgot pod interpolate-20260711-175532`; then `kinoforge list` → `No running instances` + `No instances recorded in ledger`; `modal app list` → both kinoforge apps `stopped` (0 GPUs), no running app.

---

## 26. `2026-07-12 01:08:08` — Cross-CLI warm-reuse + HF Volume weight-cache on Modal (Wan 2.1 1.3B / A10, Milestone 5) — t2v

| Field | Value |
|---|---|
| **Stack triple** | `Modal / DiffusersEngine (Wan 2.1 T2V-1.3B) / Wan-AI/Wan2.1-T2V-1.3B-Diffusers` |
| **Mode** | t2v (480×480, 33 frames, 16 fps) |
| **New capability axis** | **Cross-CLI warm-reuse + HF Volume weight-cache on Modal** (Milestone 5). Two firsts, both proven live in separate `kinoforge generate` processes: (1) a second CLI invocation **warm-attaches** to the first run's still-alive Modal container (no redeploy, no re-boot); (2) a fresh Modal container **reuses Volume-cached weights** (`HF_HOME=/cache/hf` on the named `kinoforge-hf-cache` Volume) instead of re-downloading. Closes the last two open Modal threads (the §24/§25 gotcha-memory "STILL OPEN" items). |
| **First-success SHA** | `1cb4299` — the enabling fix (`fix(warm-reuse): persist instance.endpoints in ledger.record`). Milestone commits: `15fe799` (offline round-trip characterization), `bb29fbc` (RED live scaffold, pre-spend), `1cb4299` (the ledger-endpoints fix that made Modal warm-attach work). |
| **Date (local TZ)** | 2026-07-12 01:08:08 -0700 (PDT) |
| **GPU** | Modal **A10** (24 GB), serverless. Cfg `min_vram_gb: 24` (T4/16 GB excluded); catalog picks A10. |
| **Runs (3, each a separate CLI process)** | See the evidence table below. RUN 1 = cold + weight-download baseline; RUN 2 = cold + **cache-hit** (download skipped); RUN 3 = **warm-attach** to RUN 2 (no redeploy). |
| **Est. spend** | ~$0.14 total: RUN 1 cold ~$0.10, RUN 2 cold-cache ~$0.02 (75 s), RUN 3 warm ~$0.01 (39 s), + a first failed warm-attach (pre-fix, ~$0, died fast on `has no endpoints`). Within the Modal credit. |
| **Layer / phase** | Modal Milestone 5 — plan `docs/superpowers/plans/2026-07-12-modal-milestone5-warm-reuse-hf-cache.md` (spec `docs/superpowers/specs/2026-07-12-modal-milestone5-warm-reuse-hf-cache-design.md`). USER-GATE. |

### Exact commands (all separate CLI invocations; default warm-reuse, NO `--no-reuse` until teardown)

```bash
# RUN 2 — cold boot, weights served from the /cache/hf Volume (cache hit)
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml --mode t2v \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)"

# RUN 3 — a fresh process warm-attaches to RUN 2's live container
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml --mode t2v \
  --prompt "$(cat examples/configs/prompts/field-dreamlike.txt)"
```

(There is no `--prompt-file` flag; `--prompt TEXT` + `--mode t2v` are both required — discovered while building the M5 RED scaffold.)

### Evidence — the two proofs

| Run | Instance | Prompt | Boot/attach → gen-complete | What it proves |
|---|---|---|---|---|
| RUN 1 (cold+download, baseline) | `run-20260712-004352` | field-realistic | provision 00:45:39 → gen 00:49:22 = **223 s** | cold boot WITH the first-ever 1.3B weight download to the Volume |
| RUN 2 (cold, **cache-hit**) | `run-20260712-010548` | field-realistic | provision 01:05:53 → gen 01:07:08 = **75 s** | **HF cache hit** — download skipped (weights persisted on the Volume across RUN 1's destroy) → 3× faster than RUN 1 (148 s saved) |
| RUN 3 (**warm-attach**) | attached to `run-20260712-010548` | field-dreamlike | attach 01:07:29 → gen 01:08:08 = **39 s**; log `warm-reuse: attached to run-20260712-010548`; **zero** `Building image` / deploy lines | **cross-CLI warm-reuse** — a separate process reused the live container, no redeploy, no boot |

### Artifacts

| Field | RUN 2 (field-realistic) | RUN 3 (field-dreamlike) |
|---|---|---|
| **Path** | `.kinoforge/run-20260712-010548/1435ee833b214ac5.mp4` | `.kinoforge/run-20260712-010727/6d5582a3567c15c0.mp4` |
| **Dims** | 480×480, 16 fps, 33 frames | 480×480, 16 fps, 33 frames |
| **Size / SHA-256 (head)** | 299,321 B / `1435ee833b214ac5…` | 451,363 B / `6d5582a3567c15c0…` |

### Frame-QA verdict (mandatory visual review)

**PASS — clearly high quality, no ⚠️.** 3–4 frames per clip eyeballed. RUN 2 (field-realistic): a young woman in a blue dress in a golden-hour wildflower meadow, tall backlit waterfall, glowing butterflies — coherent, sharp, prompt-adherent, real temporal motion (she turns from back-to-camera toward camera across the clip), no warping/ghosting. RUN 3 (field-dreamlike): visibly DISTINCT output (distinct SHA) — an enchanted glowing meadow with prismatic rainbow tones, violet/rose/gold flowers, blooming halos; the heavier chromatic glow is prompt-driven ("dreamlike, prismatic rainbow, blooming highlights into halos"), not corruption. Both prompt-adherent and artifact-free. (Wan 2.1 1.3B renders slightly more illustrative than the A14B, expected for the small model.)

### Reproduction recipe / deviations (read before re-firing)

- **The enabling fix (`1cb4299`) — the reason this milestone needed code.** Modal warm-attach initially died `ProvisionFailed: pod '<id>' has no endpoints — cannot construct ready URL` even though discovery + attach succeeded (`warm-reuse: attached to <id>`). Root cause: `Ledger.record` (`core/lifecycle.py`) persisted only `id/provider/tags/created_at/cost_rate` — NOT `instance.endpoints`. The warm-attach reconstructor `_resolve_warm_instance` replays `entry.get("endpoints")` and, only if empty, falls back to `provider.endpoints()` — a port-based rebuild that works for RunPod/SkyPilot but returns empty for Modal (its `.modal.run` URL carries a non-deterministic `build-<hash>` suffix, not rebuildable from ports). **Modal is the first provider that is BOTH non-rebuildable AND used with non-ephemeral warm-reuse**, so it's the first to trip the gap. Fix: persist `instance.endpoints` in the ledger entry (provider-agnostic, one line); the existing replay then serves Modal's URL. Offline coverage added: `tests/core/test_ledger_record_endpoints.py` + a Modal-shape replay test in `tests/cli/test_resolve_warm_instance_endpoints.py`.
- **The warm-reuse discovery channel already worked** for Modal — cross-CLI discovery is via the disk **ledger** (non-ephemeral) matched on `capability_key` (provider-agnostic, `_scan_warm_candidates` → `_resolve_warm_instance`). The modal provider is registered on the attach path via `_commands.py:27` → `kinoforge._adapters` (verified offline in Task 0). Only the endpoint *replay* was broken.
- **The HF cache persisted across an app destroy.** RUN 1's downloaded weights survived on the named `kinoforge-hf-cache` Volume even after its app was destroyed (Modal committed the Volume), so RUN 2's fresh container served them from `/cache/hf` — the Volume-commit uncertainty flagged in the spec resolved in favour of persistence for a clean full-download (RUN 1 ran to completion before destroy).
- **Warm window = `lifecycle.idle_timeout` (5 min here).** RUN 3 must fire within the idle window of RUN 2's last generation or the container scales down (Modal `scaledown_window = idle_timeout_s`). Fire the second process promptly.
- **Artifacts land in `.kinoforge/<run-id>/` under default warm-reuse**, not `output/` (contrast the `--no-reuse` §24/§25 runs which publish to `output/`).
- **Teardown verified** post-run: `kinoforge destroy --id run-20260712-010548` → `destroyed`; then `kinoforge list` → `No running instances` + `No instances recorded in ledger`; `modal app list` → no RUNNING kinoforge app. (Destroy MUST run under `-e live-modal` — the `modal` binary is absent in the default env; a `default`-env destroy fails `FileNotFoundError: 'modal'`.)
