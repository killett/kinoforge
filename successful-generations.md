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
10. `2026-06-21 05:37:14` — [Diffusers WanPipeline Wan 2.2 T2V-A14B + Arcane LoRA pair warm-reuse matrix on RunPod (A100 80GB) — t2v](#10-2026-06-21-053714--diffusers-wanpipeline-wan-22-t2v-a14b--arcane-lora-pair-warm-reuse-matrix-on-runpod-a100-80gb--t2v)
   - See also: `2026-06-20 23:33:36` — LoRA-flexible warm-reuse smoke step-1 (cold-boot, 0 LoRAs) at HEAD `7ce3a09` (test `tests/live/test_wan22_lora_warm_reuse.py`, cfg `examples/configs/wan22-lora-flexible-warm-reuse-smoke.yaml`). 5 attempts across 22:01-23:34 PT validated cold-boot + plain Wan 2.2 T2V generation 3 times; published artifacts `output/20260620-221751_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` (attempt 1, pod `tu1rlnrksgs6sd`), `output/20260620-231141_…` (attempt 4, pod `grvq7smmd7r5g0`), `output/20260620-233336_…` (attempt 5, pod `62zmz86zmmjjg1`). Steps 2-4 (warm-attach with `[high+low]` / `[low]` / `[]` Arcane LoRA stacks via `POST /lora/set_stack`) NOT executed live — every attempt blocked on a different smoke-harness bug (proxy URL pattern, mid-flight pod-leak when cold-boot crashes pre-`_extract_pod_id`, missing `?api_key=…`, missing `User-Agent` to clear Cloudflare). All 4 fixes committed (`dc018a3`, `f7677b2`, `7e55036`, `7ce3a09`); harness is now ready for an operator-fire to drive the LoRA-swap matrix. Cumulative T22 spend $2.15. Same tuple `(runpod, DiffusersEngine, Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)`; the LoRA-swap path will graduate to its own section once steps 2-4 land on real hardware.
   - See also: `2026-06-20 12:24:49` — 4-prompt warm-reuse re-fire at HEAD `085781e` (test `tests/live/test_diffusers_wan_t2v_4prompt_live.py`): 1 cold + 3 warm-reuse on the SAME pod, one per file in `examples/configs/prompts/`. Pod `87geau1jcpxr0z` (NVIDIA A100 80GB PCIe), total wall-clock 33 m 19 s, spend ~$0.66. All 4 MP4s ffprobe-verified h264 / yuv420p / 480×480 / 81 frames / 16 fps; 4 distinct sha256s; legs 2/3/4 all `warm-reuse: attached to 87geau1jcpxr0z`. Same tuple `(runpod, DiffusersEngine, Wan-AI/Wan2.2-T2V-A14B-Diffusers, t2v)`. Stable evidence copies at `.kinoforge/wan22_4prompt_evidence/`. Per-leg table:
     | # | Prompt file | Wall-clock from cold start | Published path | Size | SHA-256 |
     |---|---|---|---|---|---|
     | 0 (cold)  | `field-realistic.txt` | 23 m 02 s | `output/20260620-121432_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` | 1,367,301 B (1.30 MiB) | `50ac05975a13702633bcc35f7012bfee66788c9bdf9d556f9120e3448acb8d40` |
     | 1 (warm1) | `field-dreamlike.txt` | +3 m 26 s | `output/20260620-121758_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-yet-d.mp4` | 1,736,159 B (1.66 MiB) | `36d34431276713e0d20f069759fbffc26aebf0def396674c746d586b43c57a1b` |
     | 2 (warm2) | `forest.txt`          | +3 m 26 s | `output/20260620-122124_diffusers_Wan2.2-T2V-A14B-Diffuser_A-dense-old-growth-f.mp4`   | 798,293 B (0.76 MiB)   | `7b2836285ebd0b64c8a6662fea13ae21e5bac2349b81c3de5c705b309b5b6a94` |
     | 3 (warm3) | `dawn-flight.md`      | +3 m 25 s | `output/20260620-122449_diffusers_Wan2.2-T2V-A14B-Diffuser_Aerial-drone-shot-at.mp4`   | 403,909 B (0.39 MiB)   | `d11c1c194d47a70399838b095f63ea4a3d4dc2e24a99ef2279df8146af46f4f5` |

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
  --config examples/configs/fal.yaml \
  --prompt "$(cat prompt-field-realistic.txt)" \
  --mode t2v
```

### YAML config(s)

**`examples/configs/fal.yaml`** at SHA `f6045ab1293e92e43f514fb1bbd660285afc5115`:

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

(End-to-end smoke harness because the orchestrator path requires a pre-warmed `JsonProfileCache` for the kijai workflow's i2v probe shape; the test sets it up around `orchestrator.generate()` rather than invoking the CLI. Equivalent CLI command would be `pixi run kinoforge generate --config examples/configs/runpod-comfyui-wan.yaml --mode i2v --prompt "..." --init-image tests/providers/fixtures/runpod/sample_init_frame.png` but the CLI doesn't yet expose `--init-image`; tracked as a separate UX follow-up.)

### YAML config(s)

**`examples/configs/runpod-comfyui-wan.yaml`** at SHA `8aa7ae92d3d447598c476d977bf4fb0e835cc102` — see file at that ref for the kijai custom-node pins, Wan 2.1 14B / VAE / T5 / CLIP-vision models, RunPod compute block (`max_usd_per_hr: 0.50`, RTX 4090 → A5000 → 3090 preference), and 25/15/5/50/30-min lifecycle.

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

**`examples/configs/runpod-comfyui-wan-t2v.yaml`** at SHA `4c6ea68` — derived from `runpod-comfyui-wan.yaml` (i2v) with: I2V → T2V diffusion checkpoint (`Wan2_1-T2V-14B_fp8_e4m3fn.safetensors`), no CLIP-vision model entry, no `init_image` asset wiring, lifecycle budget doubled (2.0 → 4.0) and `max_lifetime` extended (50 m → 90 m) to cover two consecutive generations.

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
  --config examples/configs/runpod-comfyui-wan-t2v-1_3b.yaml \
  --prompt "$(cat examples/configs/prompts/forest.txt)" \
  --mode t2v
```

```bash
pixi run kinoforge generate \
  --config examples/configs/runpod-comfyui-wan-t2v-1_3b.yaml \
  --prompt "$(cat examples/configs/prompts/dawn-flight.md)" \
  --mode t2v
```

The CLI auto-loads `/workspace/.env` for `RUNPOD_API_KEY`,
`RUNPOD_TERMINATE_KEY`, and `HF_TOKEN`; no shell exports needed.

### YAML config

**`examples/configs/runpod-comfyui-wan-t2v-1_3b.yaml`** at blob SHA
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

**`examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml`** at SHA `365ab00`:

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
(`runpod-comfyui-wan-t2v-5b.yaml`) is the Kijai/ComfyUI path
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

First green Tier-3 fire of the LoRA smoke-test pyramid
(`docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md`).
Drives the 4-step single-LoRA swap matrix on Wan 2.1 1.3B via the
shared `tests/_smoke_harness/` module — proves the warm-reuse
`POST /lora/set_stack` path against a real-diffusers backend at
~$0.10 / fire (vs ~$2 / fire for the Tier-4 Wan 2.2 14B counterpart).

### Recipe

- **Model:** `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` (3GB; single transformer).
- **Engine / cfg:** Diffusers / `examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`.
- **LoRAs:** repo-canonical pair for Wan 2.1 1.3B (see README "Default test LoRAs (Wan 2.1 1.3B T2V)"):
  - `civitai:1479320@1673265` — wan2.1 1.3b static rotation
    (trigger `sttcrttn`, base "Wan Video 1.3B t2v", 350MB)
  - `civitai:1595383@1805395` — Pokemon Sprite Animation Video LoRA
    (no trigger word, base "Wan Video 1.3B t2v", 88MB)
- **Provider:** RunPod pod, NVIDIA RTX A5000 24GB (~$0.34/hr).
- **Test:** `tests/smoke/live_wan21/test_lora_swap_matrix.py`,
  `pixi run smoke-21b-live`.
- **Wall-clock:** 15 m 48 s.
- **Spend:** ~$0.10 (pod `2nik609jv7smsj`).

### 4-step matrix outcome

| Step | Target stack | Artifact | Size | SHA-256 |
|---|---|---|---|---|
| 1 (cold-boot, 0 LoRAs) | `[]` | `output/20260621-051118_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 347,293 B | `a6f810c4193d7c045fb9f92c2c9eda3cd51732458949d87d4ca39a7141f90dd7` |
| 2 (warm-attach [A]) | `[sttcrttn]` | `output/20260621-051515_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 662,031 B | `4024eeec5b072a314d93ffa357bc81957539ea8df0d0c209646688eeeae90280` |
| 3 (swap to [B]) | `[pokemon]` | `output/20260621-051604_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 215,183 B | `6788e2175f928830b11490a110c309d4977cb65d25e54df386eff2f1eb982d70` |
| 4 (clear all) | `[]` | `output/20260621-051637_diffusers_Wan2.1-T2V-1.3B-Diffuser_Photorealistic-cinem.mp4` | 369,137 B | `bcf9fcd2f5d16ac0b5bf9d4e88319ae8c356ec7b7d4ef4993fb9d5892529b9f6` |

All 4 mp4 sha256s distinct → LoRA swap demonstrably affects output
(matrix runner's `sha_distinct_required=True` post-condition green).

### What this surfaced (8 fires + fixes)

The smoke harness landed in 16 plan tasks (`docs/superpowers/plans/2026-06-21-lora-smoke-pyramid.md`)
but the first 7 Tier-3 fires each exposed a different bug that the
existing Tier-1 stub didn't catch. Each bug was root-caused, fixed
with a focused commit, and pinned by a unit test:

- `0f8bec8` — `WAN_MODEL_ID` propagation: `wan_t2v_server.py` defaults
  to Wan 2.2 14B; cfg.models[0].ref must inject into the server env
  via `DiffusersEngine.render_provision`. Otherwise a Wan 2.1 1.3B
  cfg silently OOM-attempts 63GB on a 24GB A5000.
- `d27429f` — `kinoforge-pod-download/0.1` UA in pod-side
  `_download_one` (Civitai Cloudflare 403s the default Python UA;
  same class of fix as commit `53a1e6e` on the orchestrator-side
  CivitAISource).
- `7242739` — `peft>=0.13` in Wan cfg pip lists (diffusers'
  `unload_lora_weights` / `set_adapters` raise `ValueError(PEFT
  backend required)` without it).
- `7242739` — harness `http.py` captures HTTPError response body
  into `exc.response_body` + appends to `exc.msg` so 5xx failures
  carry the underlying cause instead of "HTTP Error 502".
- `810f2f4` — wan-server async-blocking fix: `_download_one` + the
  diffusers ops in `_reload_pipeline_loras` were called inline from
  `async def set_stack`, blocking the FastAPI event loop. `/health`
  hung, RunPod proxy 502'd. Wrapped both in `asyncio.to_thread`.
- `5659f82` — harness 502-recovery: catch proxy 502s on
  `/lora/set_stack`, poll `/lora/inventory` for up to 600s; if
  inventory converges, the server-side download eventually succeeded
  despite the proxy give-up.
- `5b07afd` — `logging.basicConfig` at wan_t2v_server module init
  (module `_log.warning` calls were silently dropped because uvicorn
  wires its own logger but not the module's).
- `53d5777` — harness `_warmup_proxy()`: GET /health before the
  first /lora/set_stack POST. The RunPod edge proxy 502s the first
  POST after a freshly-created pod until a probing GET warms the
  upstream connection. Diagnostic 2026-06-21 confirmed set_stack
  succeeds in 8s when preceded by /health.

### Notes for future agents

- The harness fix landscape generalises: the four patterns
  (kinoforge-smoke UA, ?api_key=, URLError retry, leak-sweep) plus
  the new four (WAN_MODEL_ID inject, asyncio.to_thread,
  502-recovery via inventory poll, /health warmup) are now all in
  `tests/_smoke_harness/`. Future engine smokes (C23 ComfyUI, Wan
  3.0, Flux) inherit them by import — none of them should ever
  rediscover any of these.
- The repo-canonical Wan 2.1 1.3B LoRA pair lives in
  `examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`
  + the README "Default test LoRAs (Wan 2.1 1.3B T2V)" section. The
  pair is operator-specified (2026-06-21) to give cross-style mp4
  shas in the matrix.
- `pixi run smoke-21b-live` is the operator entrypoint; the weekly
  GH Actions cron at `.github/workflows/smoke-wan21-weekly.yml`
  (Mon 04:00 PT) automates it. The leak-sweep cron
  (`.github/workflows/leak-sweep.yml`, every 30 min) backstops any
  tier-3 pod older than 45 min.

---

## 10. `2026-06-21 05:37:14` — Diffusers WanPipeline Wan 2.2 T2V-A14B + Arcane LoRA pair warm-reuse matrix on RunPod (A100 80GB) — t2v

**T22 ARCANE LoRA-SWAP MATRIX FINALLY GREEN.** Picks up exactly where
the 2026-06-20 attempt sequence stopped (entry #8 "See also" of
2026-06-20 23:33:36): the full 4-step warm-reuse matrix on Wan 2.2 14B
+ the canonical Arcane Style LoRA pair (`civitai:2197303@2474081` high
+ `civitai:2197303@2474073` low). Same engine + model + provider as
entry #8, but now exercises the LoRA-swap path that was deferred —
graduating it from "See also" to its own capability axis.

### Recipe

- **Model:** `Wan-AI/Wan2.2-T2V-A14B-Diffusers` (MoE, 63GB; two
  transformers high_noise + low_noise).
- **Engine / cfg:** Diffusers / `examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml`.
- **LoRAs:** Arcane Style v1.0 pair (repo canonical for Wan 2.2 14B;
  see README "Default test LoRA (Wan 2.2 T2V)"):
  - `civitai:2197303@2474081` — high-noise tensor (~720MB)
  - `civitai:2197303@2474073` — low-noise tensor (~720MB)
  - Trigger: `ArcaneStyle` (prepended to step-2/3 prompts).
- **Provider:** RunPod pod, NVIDIA A100 80GB (~$1.79/hr).
- **Test:** `tests/smoke/release_wan22/test_lora_swap_matrix.py`,
  `pixi run smoke-wan22-live`.
- **Wall-clock:** 34 m 13 s.
- **Spend:** ~$0.86 (pod `eoisesybsq5wbg`), `BudgetTracker(cap_usd=2.00)` passed.

### 5-mp4 outcome

| Step | Target stack | Artifact | Size | SHA-256 |
|---|---|---|---|---|
| 1 (cold-boot, 0 LoRAs) | `[]` | `output/20260621-053714_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` | 1,299,080 B | `2b8eef0644ee49f31463741088790ab9e81fc7a32346b097a32afe64882fb29f` |
| 2 (warm-attach [high+low]) | `[high, low]` | `output/20260621-054108_diffusers_Wan2.2-T2V-A14B-Diffuser_ArcaneStyle-Photorea.mp4` | 1,173,069 B | `9774c9c0ab03aeb396f706bd361de8138b71b9d3c50ea123700558c8904736c3` |
| 3 (swap to [low] only) | `[low]` | `output/20260621-054450_diffusers_Wan2.2-T2V-A14B-Diffuser_ArcaneStyle-Photorea.mp4` | 1,628,999 B | `13a351bc0d358fef2cd6e05f2ec88bdabb5749923af6f73e55c2d2fb9535b552` |
| 4-matrix (clear via set_stack []) | `[]` | `output/20260621-054827_diffusers_Wan2.2-T2V-A14B-Diffuser_ArcaneStyle-Photorea.mp4` | 1,851,359 B | `3faec26d4a1b27f16fc09074703a94a8b645a62abc424a573963880a7d1526fa` |
| 4-bare (`_run_cli` plain regen post-swap) | `[]` | `output/20260621-055203_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` | 1,864,878 B | `6875284cb464ed87943ed57a530db1b28236768981e9327d9de196ab3f0c8581` |

All 5 mp4 shas distinct → LoRA-swap demonstrably changes output.
Step-2 + step-3 use the `ArcaneStyle` trigger; step-4-bare uses the
plain prompt to confirm the empty target_refs swap actually cleared
adapters server-side. `swap_rejected` is `null` for every transition
(no VRAM-OOM rollback needed at 80 GB).

### Why this entry exists separately from #8

Entry #8 records the Wan 2.2 14B Diffusers cold-boot path that was
green 2026-06-20 but only generated PLAIN (no-LoRA) mp4s — the 2026-06-20
T22 attempts hit a different smoke-harness bug on every fire and never
executed steps 2-4. The 2026-06-21 smoke-pyramid workstream landed 8
focused commits (`0f8bec8`, `d27429f`, `7242739`, `810f2f4`, `5659f82`,
`5b07afd`, `53d5777`, plus `53a1e6e` on the source side) that closed
each of those gaps. The capability axis introduced HERE is:
- The orchestrator-driven `POST /lora/set_stack` warm-reuse path
  (not just cold-boot generates), exercised end-to-end against a
  real-diffusers Wan 2.2 MoE on real Cloudflare-fronted RunPod
  proxy, with the Arcane high+low pair.

### Notes for future agents

- The full 8-commit fix sequence is documented inline in entry #9
  (the Tier-3 Wan 2.1 1.3B counterpart, which surfaced + fixed every
  bug at $0.10/fire before any of them touched A100 prices).
- The wan_t2v_server's `async def set_stack` MUST keep wrapping
  `_download_one` and `_reload_pipeline_loras` in `asyncio.to_thread`.
  Without that, the event loop blocks while a 720MB Arcane tensor
  downloads, /health stops responding, and RunPod's edge proxy
  returns its "Waiting for service to respond" page. The Tier-1
  CPU smoke doesn't catch this because the stub pipe is
  near-instant.
- The harness's `_warmup_proxy` is necessary even on a warm pod —
  the FIRST POST against a freshly-created pod 502s consistently
  until a probing GET completes. Don't remove it.
- 720MB tensor downloads from Civitai (per Wan 2.2 LoRA half) push
  RunPod's edge proxy timeout. The 502-recovery via inventory poll
  is what carries the harness across when the proxy gives up — the
  server-side `_download_one` keeps streaming for up to its 600s
  internal urlopen timeout and the harness eventually sees the
  inventory converge.
- The release-gate workflow is `pixi run smoke-wan22-live` (manual;
  no cron — too expensive to fire weekly). It is now a documented
  pre-tag item in `docs/RELEASE-CHECKLIST.md`.

