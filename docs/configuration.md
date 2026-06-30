# Configuration

(Moved from README §Configuration, §Concurrency, §HuggingFace ref grammar, §Per-job spec & params on 2026-06-27. See [../README.md](../README.md).)

## Configuration

Each kinoforge run is described by a single YAML file with three top-level blocks:

```yaml
engine:      # which generation backend to use + precision
models:      # ordered list of model refs (base + optional loras/vae)
compute:     # where to run (provider + image + hardware + lifecycle/budget)
```

For hosted engines (e.g. fal.ai) the `compute:` block is omitted and a top-level `lifecycle: {budget: N}` carries the spend guard instead.

Browse ready-to-use examples in [`../examples/configs/`](../examples/configs/):

| File | Engine | Provider | Use case |
|------|--------|----------|----------|
| [`wan.yaml`](../examples/configs/wan.yaml) | ComfyUI | RunPod pod | Production Wan2.2 + CivitAI LoRA |
| [`diffusers.yaml`](../examples/configs/diffusers.yaml) | Diffusers | RunPod serverless | SVD serverless |
| [`hosted.yaml`](../examples/configs/hosted.yaml) | Hosted API | fal.ai | Zero-infra hosted |
| [`local-fake.yaml`](../examples/configs/local-fake.yaml) | Fake | Local | Offline / CI smoke test |

## Concurrency

By default kinoforge runs one generation job at a time (sequential). Add
`max_in_flight` to your `lifecycle:` block to enable concurrent dispatch:

```yaml
compute:
  ...
  lifecycle:
    idle_timeout: 2h
    max_lifetime: 6h
    budget: 50.0
    max_in_flight: 4   # send up to 4 jobs to the backend in parallel
```

Three behaviours determined by `max_in_flight` and the model's generation
mode:

- **t2v fan-out** — text-to-video segments have no temporal dependency, so
  `GenerateClipStage` submits all N segments concurrently (up to
  `max_in_flight` at a time). First failure cancels in-flight jobs and
  re-raises immediately.
- **i2v serial** — image-to-video segments must be chained (each segment's
  tail frame seeds the next), so they are dispatched one-at-a-time
  regardless of `max_in_flight`.
- **multi-request** — a backend running on multi-GPU hardware (e.g. a
  ComfyUI server with 4 GPUs) can process multiple independent requests
  simultaneously; set `max_in_flight` to match its actual parallelism.

`max_in_flight: 1` (the default) preserves the original sequential behaviour.

## HuggingFace ref grammar

Four ref shapes are recognised:

| Ref | Meaning |
|---|---|
| `hf:<repo>` | Bare repo at `main` — every file enumerated via the HF tree API. |
| `hf:<repo>@<rev>` | Bare repo at a pinned branch / tag / commit SHA. |
| `hf:<repo>:<path>` | Single file at `main`. |
| `hf:<repo>@<rev>:<path>` | Single file at a pinned revision. |

Bare-repo resolves auto-populate per-file SHA256 from LFS metadata when
present (every weights file ships LFS-tracked, so integrity verification
runs without the operator setting `sha256:` per entry). Setting
`sha256:` on a bare-repo entry raises `ValidationError` at provision
time — use a pinned `@<commit-sha>` for tree-level reproducibility, or
split into per-file refs for per-file pinning.

## Per-job spec & params

Two top-level YAML blocks supply per-job payload to the engine:

| block | flows into | who reads it | scope |
|---|---|---|---|
| `spec:` | `GenerationJob.spec` | `engine.validate_spec(job)` + `backend.submit(job)` | engine-interpreted (engine-specific shape) |
| `params:` | `GenerationJob.params` | every engine + every `Segment.params` (segment-wins merge) | engine-neutral knobs (fps, num_frames, steps, seed, ...) |

### Required `spec.*` keys per engine

| engine | required `spec.*` keys | notes |
|---|---|---|
| `hosted` | `model`, `params` | `spec.model` is the single source of truth for model identity (Layer M: `engine.hosted.model` removed) |
| `diffusers` | `pipeline`, `scheduler` | |
| `comfyui` | `graph`, `node_overrides` | optional: `asset_node_ids`, `prompt_node_ids` |
| `fal` | — | prompt comes from `Segment.prompt` via Layer J's `resolve_prompt` |

### Top-level `params:` vs nested `spec.params:` (gotcha)

Hosted requires a `params` key **inside** `spec:` as a wire body field. This is
structurally distinct from top-level `params:` (engine-neutral knobs that flow
into `GenerationJob.params`). There is **no merging** between the two
namespaces.

```yaml
params:                 # -> GenerationJob.params (engine-neutral, segment-wins)
  fps: 24
spec:
  model: "wan-..."
  params:               # -> GenerationJob.spec["params"] (hosted wire body)
    guidance_scale: 5.0
```

Reader takeaway: if a key matters to every engine, put it under top-level
`params:`. If it is engine-specific, put it under `spec:`.

### On `validate_spec` failure

When the orchestrator detects a `spec:` key missing for the configured engine,
it raises `ValidationError` and tears down any provisioned compute before
re-raising (mirroring the existing `CapabilityMismatch` branch). A typo in
your config will not cost idle pod time.

See `../examples/configs/hosted.yaml`, `../examples/configs/diffusers.yaml`, `../examples/configs/wan.yaml`, and `../examples/configs/fal.yaml`
for working `spec:` + `params:` shapes per engine.


## `upscale:` (optional, video upscaling)

Activates the in-pipeline `UpscaleStage` after `GenerateClipStage` for
`kinoforge generate`, or stands alone for `kinoforge upscale`. The CLI
flag `--scale` overrides `upscale.scale` for one-off runs.

| Key | Type | Default | Notes |
|---|---|---|---|
| `engine` | `"seedvr2"` | — | Required. v1 supports SeedVR2; FlashVSR drop-in is a future session. |
| `scale` | string | — | `"Nx"` for factor (works in v1); `"Np"` parses but raises `NotYetImplementedError` (deferred). |
| `seedvr2.variant` | `"3B"` \| `"7B"` | `"3B"` | Required when `engine == "seedvr2"`. |
| `seedvr2.precision` | `"fp8"` \| `"fp16"` | `"fp8"` | |
| `seedvr2.tile_size` | int \| null | `null` | engine default |
| `seedvr2.steps` | int \| null | `null` | engine default |

See `../examples/configs/upscale-seedvr2-3b.yaml` (standalone) and
`../examples/configs/wan-with-upscale.yaml` (multi-stage).
