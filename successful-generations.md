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
