# Wan 2.2 native T2V-A14B via DiffusersEngine — design

**Status:** spec draft, awaiting user review
**Date:** 2026-06-19
**Phase:** New — Wan 2.2 14B T2V first-class engine support
**Tier:** 1 (durable, low tech-debt)

## Problem

Wan 2.2 14B T2V has been unreachable through kinoforge's existing
ComfyUIEngine path. Kijai's `WanVideo_comfy` HuggingFace repo only
publishes the I2V-A14B HIGH+LOW pair, not a T2V single-file variant.
Running I2V-A14B weights in T2V mode via `WanVideoEmptyEmbeds` fails
at the patch_embedding layer with a runtime channel-count mismatch
(model expects 36 channels = noise + image latent + mask; T2V provides
only 16 noise channels). The I2V weights have image-conditioning
baked into the convolution shape; no workflow-level rewiring can
substitute for it.

The actual T2V model — `Wan-AI/Wan2.2-T2V-A14B` on HuggingFace — is
published in **sharded** form: 6 safetensors files for the high-noise
model, 6 for the low-noise model, each ~10 GB, plus an index JSON per
side. Kijai's `WanVideoModelLoader` reads single files only. Native
ComfyUI loaders have the same limitation. No bootstrap-script merge
hack is acceptable as a first-class solution: it duplicates work that
HuggingFace's official Python libraries solve correctly upstream, and
the disk profile (114 GB merged + ~228 GB transient peak) is hostile
to any operator with a sane pod budget.

## Goal

Add a durable, low-tech-debt path for Wan 2.2 native T2V-A14B
generation through kinoforge. Single live `kinoforge generate` against
a new YAML cfg must produce a valid h264/MP4 from a text prompt, with
the same warm-reuse and capability-key semantics as existing
Wan 2.1 + Wan 2.2 5B configs. First-phase scope is **T2V-A14B only**;
I2V-A14B, TI2V-5B native, and future Wan releases ride on follow-up
work but reuse the same engine seam.

## Non-goals

- Multi-worker concurrency on the inference server.
- Synchronous (non-polled) HTTP generation contract.
- Bidirectional cancellation (Ctrl-C kills in-flight pipeline call).
- Background sweeper for stale artifacts.
- Generic "DiffusersEngine server-script abstraction" (deferred to
  later spec; pattern documented but not codified).
- Wan I2V-A14B / TI2V-5B native via diffusers (separate phase).
- Replacing the existing Kijai-based Wan 2.1 + Wan 2.2 5B Comfy configs.

## Approach

Extend the existing `DiffusersEngine`. Ship a kinoforge-maintained
inference server script that wraps `diffusers.WanPipeline.from_pretrained`
and exposes the standard `DiffusersBackend` HTTP contract (
`POST /generate`, `GET /status/{id}`, `GET /artifacts/{file}`,
`GET /health`).

Why this layer:

- DiffusersEngine already wires HTTP polling, render_provision,
  wait_for_ready, heartbeat, selfterm, env-var lifting. No
  duplication.
- diffusers' `WanPipeline.from_pretrained` calls
  `huggingface_hub.snapshot_download()` under the hood, which handles
  sharded safetensors correctly via the `model.safetensors.index.json`
  manifest. We outsource the shard-handling problem to upstream.
- The engine's `server_cmd` field was designed for exactly this
  extension pattern: each future model family (Mochi, CogVideoX,
  Hunyuan, Wan 3.x) becomes a new server script under
  `kinoforge/engines/diffusers/servers/` without engine-layer changes.

Why not a new engine kind:

- Duplicating the ~600 LOC DiffusersEngine infrastructure per model
  family invites drift between parallel engines. Bug fixes land on
  one, not the other.

Why not the Comfy + shard-merge alternative:

- Shard-merge bootstrap reinvents lazy sharded loading that diffusers
  solves natively. Disk profile (~228 GB transient peak) blocks
  reasonable pod budgets. Every new sharded model family needs the
  same hack rerun.

## Architecture

```
Laptop (kinoforge generate)
  └─ DiffusersEngine (existing)
       render_provision()  → bootstrap script
       wait_for_ready()    → polls GET /health
       submit()            → POST /generate
       result()            → polls GET /status/{id}
       http_get_bytes()    → GET /artifacts/{file}

         │ HTTPS via RunPod proxy
         ▼

RunPod pod (FastAPI + uvicorn, single process)
  wan_t2v_server.py
    Startup (run once):
      pipe = WanPipeline.from_pretrained(
        "Wan-AI/Wan2.2-T2V-A14B",
        torch_dtype=torch.bfloat16,
      ).to("cuda")
      ready.set()
      spawn worker thread
    Endpoints:
      GET  /health           → {ready, model}
      POST /generate         → {job_id}
      GET  /status/{job_id}  → {status, progress, filename, url}
      GET  /artifacts/{name} → MP4 bytes (FileResponse)
    Worker thread:
      while True:
        job = q.get()
        frames = pipe(prompt, **params).frames[0]
        write_mp4(frames, fps, artifact_dir / f"{job_id}.mp4")
        state.status = "done"

HuggingFace cache (~/.cache/huggingface):
  Wan-AI--Wan2.2-T2V-A14B/
    high_noise_model/
      diffusion_pytorch_model-00001-of-00006.safetensors  (~10 GB)
      ... 5 more shards
      model.safetensors.index.json
    low_noise_model/
      diffusion_pytorch_model-00001-of-00006.safetensors  (~10 GB)
      ... 5 more shards
      model.safetensors.index.json
    Wan2.1_VAE.pth                                        (~0.5 GB)
    text_encoder/                                         (umt5-xxl)
    tokenizer/
    scheduler/
```

## Components

### A. `wan_t2v_server.py` (NEW, ~250 LOC)

Path: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`

FastAPI single-process app, single worker thread, in-process job
state dict. Pipeline loaded at startup, persisted across requests.
Default model id `Wan-AI/Wan2.2-T2V-A14B` overridable via
`WAN_MODEL_ID` env var (room for the Wan 2.3 / future case without
code change).

Public surface:

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | `{ready: bool, model: str}` |
| `/generate` | POST | Body `{prompt, negative_prompt?, width, height, num_frames, fps, num_inference_steps?, guidance_scale?, seed?}` → `{job_id}`. 503 if pipe not loaded. |
| `/status/{job_id}` | GET | `{status: queued|running|done|error, progress, filename?, url?, error?}`. 404 if unknown id. |
| `/artifacts/{filename}` | GET | `FileResponse`, `media_type: video/mp4`. 404 if missing. |

In-process state (set at startup, mutated only by worker thread):

```python
pipe: WanPipeline | None = None
artifact_dir: Path                     # /workspace/artifacts
jobs: dict[str, JobState] = {}
q: queue.Queue[str] = queue.Queue()
ready: threading.Event = threading.Event()
```

Server-side defaults (cfg-knob baseline for thin-cfg pattern):

| Knob | Default | Source |
|---|---|---|
| `num_inference_steps` | 20 | Wan 2.2 14B reference workflow |
| `guidance_scale` | 6.0 | Kijai 14B workflow widget |
| `negative_prompt` | (Wan canonical bilingual neg) | Kijai workflow widget |
| `seed` | random per request | safer than fixed default |
| `width`, `height`, `num_frames`, `fps` | 480, 480, 81, 16 | matches Wan 2.1 1.3B + Wan 2.2 5B output shape |

### B. `_video_io.py` (NEW, ~50 LOC)

Path: `src/kinoforge/engines/diffusers/servers/_video_io.py`

Single function `write_mp4(frames: np.ndarray, fps: int, path: Path) -> None`.
Wraps `imageio` with `imageio-ffmpeg` extra. Produces h264 yuv420p at
crf 19 to match the encoding profile of existing Wan 2.1 outputs.
Pure function, testable independently.

### C. `runpod-diffusers-wan-t2v-14b-2_2.yaml` (NEW, ~60 LOC)

Path: `examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml`

```yaml
engine:
  kind: diffusers
  precision: bf16
  diffusers:
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    base_url: "http://localhost:8000"
    prompt_body_key: "prompt"

models:
  # Bare-repo ref — HuggingFaceSource enumerates files at resolve time
  # so the cfg passes load_config validation and contributes its repo
  # path to the capability key. The ACTUAL download is handled at
  # server startup by diffusers.WanPipeline.from_pretrained(), which
  # calls huggingface_hub.snapshot_download() into the standard
  # ~/.cache/huggingface tree. The bootstrap's _kinoforge_download
  # path is NOT used for this ref — see "Bootstrap interaction" below.
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B"
    kind: base
    target: checkpoints  # any valid target; nothing lands here at runtime

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  warm_reuse_auto_attach: true
  requirements:
    min_vram_gb: 48
    min_cuda: "12.4"
    max_usd_per_hr: 1.00
    gpu_preference:
      - "NVIDIA A40"           # ~$0.39/hr
      - "NVIDIA RTX A6000"     # ~$0.79/hr
      - "NVIDIA L40S"
    disk_gb: 150
  lifecycle:
    idle_timeout: 25m
    job_timeout: 15m
    time_buffer: 3m
    max_lifetime: 90m
    boot_timeout: 25m
    budget: 3.0
    heartbeat_interval_s: 30

spec:
  width: 480
  height: 480
  num_frames: 81
  fps: 16
```

### D. Bootstrap interaction (engine-layer behavior)

The existing `DiffusersEngine.render_provision()` is minimal — it
pip-installs deps and execs `server_cmd`. It does NOT loop over
`cfg.models[]` to emit per-file download lines the way ComfyUIEngine
does. So a bare-repo ref like `hf:Wan-AI/Wan2.2-T2V-A14B` in this cfg
does NOT trigger any kinoforge-side download. The model is fetched
at server startup by `diffusers.WanPipeline.from_pretrained()`
calling `huggingface_hub.snapshot_download()` into
`~/.cache/huggingface/`.

The `models[]` block in the cfg serves two purposes for this engine:

1. **Capability-key derivation** — the base ref is part of the key,
   so warm-reuse correctly distinguishes this cfg from other diffusers
   cfgs that load different model IDs.
2. **load_config validation** — exactly-one or two base entries is
   the schema invariant.

The `target` field has no runtime effect under DiffusersEngine — it's
required by schema but ignored by render_provision. We pick
`checkpoints` (the most generic valid target for base) and document
this in the cfg's inline YAML comments.

**No schema change** is required by this spec. The behavior is
already implicit in the existing engine layering; this section just
documents it for future readers.

### E. Engine-layer patches (audit gaps)

1. **`DiffusersBackend.result()` — short-circuit on `status == "error"`.**
   Current implementation polls until `done` and otherwise loops to
   timeout. Add: when `status == "error"`, raise `GenerationError`
   with the server-supplied error message. ~3 LOC + 1 unit test.

2. **`DiffusersBackend.wait_for_ready()` — proxy 404 retry semantics.**
   Audit whether the existing wait_for_ready already wraps with
   `_retry_proxy_call`-equivalent. If not, add same shape as
   Phase 47's ComfyUI fix. ~5 LOC + 1 unit test.

Both patches ship with `wan_t2v_server.py` since they are exercised
by it in production. Single commit; small.

### F. Tests

Five test layers, each shipping with the code it covers:

| Layer | File | Cost | Bugs caught |
|---|---|---|---|
| A. Video I/O unit | `tests/engines/diffusers/test_video_io.py` | <1s offline | Codec/dimensions drift |
| B. Server unit (FastAPI TestClient + fake pipe) | `tests/engines/diffusers/test_wan_t2v_server.py` | <2s offline | Endpoint contract violations, worker error handling, ready-flag race |
| C. Backend patch unit | `tests/engines/diffusers/test_backend_error_handling.py` | <1s offline | Loop-forever on error, proxy 404 race |
| D. Cfg lockdown | extend `tests/test_examples.py` | <1s offline | Cfg drift, capability_key non-determinism |
| E. Live smoke | `tests/live/test_diffusers_wan_t2v_live.py` | $0.30-0.80 live | End-to-end correctness |

Live smoke gated by `KINOFORGE_LIVE_TESTS=1`. Manually invoked once
before declaring spec complete; not run in CI.

## Data flow

(See full sequence diagram in the brainstorming session — abbreviated
here.)

1. `kinoforge generate` derives capability_key, ledger scan misses,
   cold-create fires.
2. DiffusersEngine.render_provision emits a script: selfterm + pip
   install + exec server.
3. Pod boots, pip installs ~30 s, FastAPI binds :8000.
4. Startup event fires: `WanPipeline.from_pretrained` downloads
   ~63 GB via huggingface_hub (~5-15 min), `pipe.to("cuda")`
   (~2-3 min), `ready.set()`, worker thread spawn.
5. Orchestrator's wait_for_ready polls /health, sees `ready=true`.
6. submit POSTs /generate, server enqueues, returns job_id.
7. Worker thread runs `pipe(...)` (~5-10 min on A40), writes MP4,
   marks done.
8. Orchestrator polls /status, sees `done`, fetches /artifacts/<id>.mp4
   bytes via http_get_bytes.
9. LocalOutputSink writes to `/workspace/output/<timestamp>_diffusers_
   Wan2.2-T2V-A14B_<slug>.mp4`. Ledger records the run. Heartbeat loop
   continues.
10. Second `kinoforge generate` with a different prompt: ledger scan
    hits, log `warm-reuse: attached to <pod>`, skip to step 6. MP4
    lands ~5-10 min later (no model reload).

## Error handling

Five failure surfaces, each with explicit policy. See brainstorming
session Section 4 — summary:

- **Model download fails at startup:** uvicorn exits non-zero,
  pod terminal, orchestrator's `boot_timeout` surfaces
  `ProvisionTimeout`. No background retry — startup failures should
  surface, not hide.
- **Generation fails mid-sample (CUDA OOM, NaN, scheduler):** worker
  thread's `try/except` records `state.status = "error"`,
  `state.error = "<exc type>: <message>"`. Backend's `result()` raises
  `GenerationError` (requires audit-gap patch E.1).
- **Worker thread dies:** belt-and-braces `try/except Exception` in
  the loop; logs at ERROR level; thread stays alive and processes the
  next job. Single-thread design means worker death blocks everything
  — explicit logging is the diagnostic.
- **Artifact disk fills up:** caught by worker's exception handler;
  job marked error with "disk full" in message. Cfg specifies
  `disk_gb: 150` (sufficient for HF cache + a few artifacts).
- **Pod proxy 404 startup race:** `wait_for_ready` retries via
  `_retry_proxy_call`-equivalent (audit-gap patch E.2).
- **Orchestrator cancellation:** cancel token exits poll loop; pod
  left running for selfterm or explicit `kinoforge destroy`. No
  bidirectional cancel (Phase 1 non-goal).

## Invariants

The design relies on three invariants that should remain true through
the implementation and beyond:

1. **`huggingface_hub.snapshot_download()` handles sharded weights
   correctly.** It reads `model.safetensors.index.json`, downloads
   all shards in parallel with ETag verification + retry. This is
   the load-bearing upstream contract. If diffusers ever drops this,
   we hand-roll snapshot_download — same effort class as a Comfy
   custom node.
2. **Pipeline lives in-process and persists across requests.** Warm
   reuse on the kinoforge side depends on this. Server must NOT
   reload the model between `/generate` calls.
3. **Job state is in-process dict.** No durable persistence. Pod
   restart loses all in-flight jobs; orchestrator's existing
   404/timeout handling surfaces this. Acceptable for the smoke
   target.

## Acceptance criteria

The implementation is complete when ALL of these hold:

1. Unit tests A, B, C, D all pass under `pixi run pytest`.
2. ruff + mypy clean across all new files.
3. `pixi run kinoforge doctor -c examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml`
   reports all green.
4. Live smoke E with `KINOFORGE_LIVE_TESTS=1` produces a valid
   h264/MP4 at 480×480 / 81 frames / 16 fps from
   `examples/configs/prompts/field-realistic.txt`.
5. A second `kinoforge generate` invocation against the same cfg
   with `examples/configs/prompts/field-dreamlike.txt` triggers
   warm-reuse — `warm-reuse: attached to <pod_id>` log line fires,
   second MP4 lands without re-provision.
6. Both successful generations produce distinct MP4 bytes (sha256
   differs) — proves prompt actually drives output.
7. New entry added to `/workspace/successful-generations.md` per
   the durability rule.
8. The 14B `runpod-comfyui-wan-t2v-14b-2_2.yaml` Kijai-path cfg is
   either fixed (if a path is found) or explicitly marked DEAD in
   PROGRESS.md.

## Out of scope (follow-up phases)

- Wan 2.2 I2V-A14B via the same engine pattern.
- Wan 2.2 TI2V-5B native via the same engine pattern (replaces
  current Kijai 5B cfg if successful).
- Other model families: Mochi, CogVideoX, Hunyuan (each ships a new
  server.py + cfg).
- Bidirectional cancellation (`DELETE /jobs/{id}` + diffusers
  interrupt callback).
- Background sweeper for stale artifacts on long-lived pods.
- Server-side metrics endpoint (`/metrics` for Prometheus scrape).
- Generic "server-script contract" abstraction codified as a separate
  spec — pattern documented in this file's Approach section but not
  enforced via base class.

## Open questions resolved during brainstorming

- **Scope:** First-class engine support (not smoke-only PoC).
- **Engine layer:** Extend DiffusersEngine (not new WanNativeEngine,
  not Comfy + shard-merge).
- **Variant scope (Phase 1):** T2V-A14B only.
- **Server architecture:** FastAPI single worker, model loaded at
  startup.
- **VRAM strategy:** Target 48 GB GPU (no offload).
- **Cfg surface:** Thin cfg, fat server (defaults baked in).

## References

- Brainstorming session: this conversation, 2026-06-19.
- `examples/configs/runpod-comfyui-wan-t2v-14b-2_2.yaml` — the failed
  Kijai-based Phase 1 attempt that motivated this design.
- `src/kinoforge/engines/diffusers/__init__.py` — DiffusersEngine
  existing implementation; defines the seam this spec extends.
- `src/kinoforge/engines/comfyui/__init__.py:1245-1300` — Phase 47
  `_retry_proxy_call` helper; pattern referenced by audit-gap E.2.
- `successful-generations.md` entries #5 + #7 — Wan 2.1 14B + 1.3B
  T2V Kijai-based reference for output-shape parity.
