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
