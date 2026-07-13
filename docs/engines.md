# Engines and providers

(Moved from README §Real providers — fal.ai, §Hosted Bearer providers (Replicate / Runway), §Bedrock Video, §Keyframe stage, §Real providers — RunPod, §Diffusers inference-server response contract, §Hosted response URL — url_path, §Cross-engine prompt routing, §Engine asset wiring on 2026-06-27. See [../README.md](../README.md).)

## Real providers — fal.ai

kinoforge ships with a fal.ai sibling engine (`FalEngine`) for video generation
via fal's queue API.

**Setup:**

1. Put your fal.ai key in `.env` at the repo root:
   ```
   FAL_KEY=fal-XXXXXXXX
   ```
2. Pick a model — `../examples/configs/fal.yaml` defaults to Wan2.2 T2V.
3. Run:
   ```bash
   pixi run python -m kinoforge --env-file .env generate \
     -c ../examples/configs/fal.yaml \
     --prompt "a cat sitting on a fence" --mode t2v
   ```
4. Artifact lands under `.kinoforge/run/<run-id>/`.

To run the live test suite (`pixi run test-live`), set `KINOFORGE_LIVE_TESTS=1`
alongside `FAL_KEY` in your environment.

## Hosted Bearer providers (Replicate / Runway)

Layer 4 ships two hosted video adapters that share a single foundation —
`RemoteSubmitPollBackend` in `kinoforge.core.remote_backend`. Each adapter
lazy-imports the official provider SDK inside method bodies (preserving the
core-import-ban invariant) and implements 5 wire-shape hooks:

| Provider | Engine kind | Env var | Status field | Output shape |
|---|---|---|---|---|
| Replicate | `replicate` | `REPLICATE_API_TOKEN` | `status` (lowercase) | `output: str \| list[str]` |
| Runway | `runway` | `RUNWAYML_API_SECRET` | `status` (UPPERCASE) | `output: list[str]` |

> **Luma direct video API retired 2026.** The legacy
> `api.lumalabs.ai/dream-machine/...` endpoint was retired by the
> provider and now 308-redirects to the consumer dashboard. Reach Luma
> video models via AWS Bedrock (`luma.ray-v2:0`, see the Bedrock Video
> section below) or Replicate (`luma/ray-flash-2`, see the Replicate
> row above). UNI-1 image-keyframe support via `LumaAgentsImageEngine`
> is planned in Layer 5b — track the `LUMAAI_API_KEY` env var, which
> is reserved for that engine.

Each engine's `provision()` validates the Bearer credential via Layer-1
`Bearer` strategy. Compute is `requires_compute=False` — no GPU instance
required. `validate_spec` requires `spec.model`; `key_base` returns it.

### Comparison-batch quickstart

```bash
# 1. Wire credentials (any subset; missing ones skip silently)
echo 'REPLICATE_API_TOKEN=r8_xxxxx' >> .env
echo 'RUNWAYML_API_SECRET=key_yyyyy' >> .env
# LUMAAI_API_KEY (reserved for Layer 5b UNI-1 keyframe engine; direct video API retired)
# echo 'LUMAAI_API_KEY=luma-zzzzz' >> .env

# 2. Verify creds present (Layer-4 gate added to preflight)
pixi run preflight --check-hosted

# 3. Run a single t2v smoke per provider
pixi run -e live-hosted python -m kinoforge \
    --state-dir /tmp/kf-runway generate \
    -c ../examples/configs/comparison/runway-t2v.yaml \
    --prompt "$(cat ../examples/configs/prompts/field-realistic.txt)" \
    --mode t2v --run-id live-runway
```

### Filename schema

`LocalOutputSink` filenames embed the provider + model so side-by-side
comparison outputs are easy to grep:

```
{ts}_{provider}_{model-slug}_{prompt-slug}.{ext}
20260607-194858_replicate_bytedance-seedance-1-lit_Cinematic-shot-of-a.mp4
```

`provider` and `model` flow from `engine.kind` + `spec.model` through the
orchestrator → `GenerateClipStage` → `OutputSink.publish` Protocol. Configs
that don't supply both substitute the literal `"unknown"` so the schema is
stable.

Filename slugs now reflect engine-native model identity: hosted engines use
`spec.model`, fal uses `engine.fal.endpoint`, ComfyUI uses the filename stem
of the base model entry, and Bedrock uses the model id. Engines that cannot
surface a real identity log a WARNING and the slug falls back to `unknown`.

### Live-smoke prompt-size + model-entitlement caveats

- **Runway** caps `prompt_text` at 1000 characters. The standard kinoforge
  comparison prompt is ~1267 chars; for Runway smokes either truncate the
  prompt or pre-summarise. The kinoforge layer does **not** truncate.
- Runway model variants are gated per-account. `gen3a_turbo` may return 403
  "Model variant ... is not available"; `gen4.5` is generally available.
  The engine narrows on `runwayml.AuthenticationError` (not raw HTTP 403)
  so model-access failures surface as `KinoforgeError`, not `AuthError`.
- **Replicate** uses `predictions.create(model="owner/name")` (the slug),
  not `version=` (a 64-char hash). Pass the operator-friendly slug in
  `spec.model`. Throttling kicks in when account credit drops below $5
  (6 req/min burst-of-1).

## Bedrock Video (AWS Bedrock — Nova Reel, Luma Ray v2, etc.)

Generic engine for any Bedrock async-invoke video model. YAML supplies a
`model_input_template` dict where `"${PROMPT}"` is substituted at submit
time. New Bedrock video models (Nova Reel, Luma Ray, future additions)
drop in config-only.

Auth: AWS SigV4 via Layer 1 `AWSSigV4` strategy. No Bearer key.

Live smoke: `KINOFORGE_LIVE_TESTS=1 pixi run pytest ../tests/live/test_luma_ray_live.py -v`

NOTE: AWS gates new third-party Bedrock models behind a one-time per-
account authorization. As of 2026-06 the gate requires an AWS Support
case — the console "Model access" page is retired for first-party
models but the authorization step remains for third-party models.
Open a case via the AWS Support Center for the target model + region.

### Bedrock Video probe

Before spending on a live smoke, verify catalog + invocation access in one shot:

```bash
pixi run probe-hosted -- --config ../examples/configs/luma-ray.yaml \
    --check-bedrock-model-access luma.ray-v2:0
```

This runs a two-stage check: (1) `list_foundation_models` for catalog
presence, then (2) a deliberately-malformed `StartAsyncInvoke` that returns
a body-format `ValidationException` if access is granted, or `"Operation not
allowed"` if the account-level authorization gate is still active.

## Keyframe stage

The keyframe stage runs an image-generation model **before** the video-generation
step and injects the result as a conditioning asset. Add a `keyframe:` block to
any config to opt in — configs without the block are unaffected.

### When to use it

| Scenario | Without keyframe | With keyframe |
|---|---|---|
| i2v (image-to-video) | Supply your own init image via `--init-image` | Let kinoforge generate the init frame from a tailored prompt |
| flf2v (first-last-frame-to-video) | Supply both bookend frames manually | Let kinoforge generate each bookend independently, with per-role prompts and seeds |
| t2v | Not applicable | Not applicable |

### i2v — generate the init frame automatically

```yaml
mode: i2v
prompt: "a cat walking through a sunlit meadow, soft motion"

engine:
  kind: fal
  fal:
    endpoint: "fal-ai/wan-i2v"
    queue_base: "https://queue.fal.run"
    api_key_env: "FAL_KEY"
    url_path: "video.url"

spec:
  model: "fal-ai/wan-i2v"

keyframe:
  engine: fal
  prompt: "photorealistic cat in a sunlit meadow, shot on 35mm film, shallow depth of field"
  spec:
    model: "fal-ai/flux/schnell"
```

`keyframe.prompt` is the image-generation prompt (usually more precise than the
video prompt). `keyframe.spec.model` is the image model slug. The generated image
is injected automatically as the `init_image` conditioning asset — you do not
supply `--init-image` at the CLI.

### flf2v — differentiated bookend frames

flf2v requires one image per bookend role. The `roles:` map lets each bookend
carry an independent prompt and `spec` overrides while sharing the same image
model:

```yaml
mode: flf2v
prompt: "a cat morphing into a tiger, smooth transition"

engine:
  kind: fal
  fal:
    endpoint: "fal-ai/wan-flf2v"
    queue_base: "https://queue.fal.run"
    api_key_env: "FAL_KEY"
    url_path: "video.url"

spec:
  model: "fal-ai/wan-flf2v"

keyframe:
  engine: fal
  spec:
    model: "fal-ai/flux/schnell"
  roles:
    first_frame:
      prompt: "photorealistic cat sitting in meadow, centered, soft daylight"
      spec:
        seed: 42
    last_frame:
      prompt: "photorealistic tiger sitting in meadow, centered, same composition, same lighting"
      spec:
        seed: 43
```

Each role entry can override `prompt` and any `spec` keys. A top-level
`keyframe.prompt` can be set as a shared default for roles that omit their own
`prompt`.

### Implementation note

`KeyframeStage` runs as a **pre-phase** before `validate_request` + splitter +
`GenerateClipStage`. This ordering is necessary because `validate_request`
rejects `mode=i2v` with empty assets — the keyframe image must exist before
validation runs. Future stages (audio, upscale) may face the same pre/post
choice; a future layer may promote `validate_request` itself into a Stage to make
the ordering explicit.

See ready-to-run examples:
- [`../examples/configs/keyframe-fal-i2v.yaml`](../examples/configs/keyframe-fal-i2v.yaml)
- [`../examples/configs/keyframe-fal-flf2v.yaml`](../examples/configs/keyframe-fal-flf2v.yaml)

## Real providers — RunPod

kinoforge ships an opt-in live smoke against the real RunPod GraphQL API
that validates the provider's pod lifecycle end-to-end. It is skipped by
default and never runs in CI.

```bash
export RUNPOD_API_KEY=...
export RUNPOD_TERMINATE_KEY=$RUNPOD_API_KEY    # see ../.env.example

KINOFORGE_LIVE_TESTS=1 \
pixi run pytest ../tests/live/test_runpod_live.py -v
```

To refresh the committed GraphQL response fixtures (e.g. after a RunPod
schema upgrade), add the capture flag:

```bash
KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 \
pixi run pytest ../tests/live/test_runpod_live.py -v
```

The smoke is intentionally minimal: it calls `find_offers`, creates a
real pod on the cheapest viable GPU, polls until ready, lists, then
destroys. No engine, no model download, no generation. Cost per run
is ≈$0.001 (single-digit pennies × seconds at ~$0.35/hr).

Cost guards (triple-locked):
1. Smoke YAML pins `max_usd_per_hr=0.50` — `filter_offers` excludes anything more expensive
2. `finally:` block always calls `destroy_instance`
3. Selfterm script + `idle_timeout_s=600` provides a 10-minute fallback if the test process is killed mid-run

Engine-integration smoke (ComfyUI + Wan i2v producing a real MP4) is
deferred to a future Layer O — the YAML and manifest at
`../examples/configs/runpod-comfyui-wan*.yaml` are committed as forward
scaffolding for that work.

**Note on RUNPOD_TERMINATE_KEY:** the selfterm.py design predates RunPod's
scoped-key feature; RunPod's current scoped-key UX is two-level (GraphQL
read or read+write, OR per-endpoint serverless) with no native
terminate-only scope. Until that ships, reusing the main key via
`${RUNPOD_API_KEY}` interpolation is the documented pattern; the
selfterm fallback still works, only the privilege separation is lost.

### Engine integration (ComfyUI + Wan i2v)

End-to-end RunPod → ComfyUI → Wan 2.1 i2v generation. Drives a real RunPod pod that boots ComfyUI with the kijai WanVideoWrapper graph and produces an MP4.

**Required env vars:**
- `RUNPOD_API_KEY` — RunPod REST API key (least-privilege; see "Credential safety in tests")
- `HF_TOKEN` — Hugging Face token (for on-pod model downloads)

**Optional env vars (live-test runner only):**
- `KINOFORGE_LIVE_KEEP_POD=1` — read by `../tests/live/test_comfyui_wan_live.py`; when set, the live test skips the destroy step so re-runs reuse the same pod via tag lookup. Not consumed by the `kinoforge generate` CLI.

**Quickstart:**

```bash
pixi run kinoforge generate \
  --config ../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml \
  --prompt "a cat turns into a woman" \
  --init-image ../tests/providers/fixtures/runpod/sample_init_frame.png
```

**Dev loop via the live test runner:**

```bash
KINOFORGE_LIVE_KEEP_POD=1 pixi run pytest ../tests/live/test_comfyui_wan_live.py -v
# iterate: tweak graph JSON / fixture / prompt, re-run with the same KINOFORGE_LIVE_KEEP_POD=1
# pod stays warm and auto-reaps after idle_timeout (configured at 2h in ../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml)
# manual reap:
pixi run kinoforge destroy <pod_id>
```

**Cost shape:**
- Pod: NVIDIA RTX 3090 @ ~$0.27/hr (varies by region/availability)
- Cold boot (first run; downloads model weights): ~12–20 min wall-clock, ~$0.05–0.09
- Warm reuse: ~5 min, ~$0.025
- Always run `pixi run preflight` before live spend (checks zero active pods, clean tree, creds present)

**Configuration files:**
- `../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml` — Wan 2.1 i2v engine config (lifecycle, params, model entries)
- `../examples/configs/runpod-comfyui-wan.graph.json` — kijai WanVideoWrapper API-format graph

## Diffusers inference-server response contract

`DiffusersBackend.result()` polls `GET /status/{job_id}` and reads two
fields from a successful (`status: done`) response:

- `filename` — display name for the produced clip.
- `url` — HTTP-fetchable location for the produced clip (e.g.
  `http://127.0.0.1:8000/file/clip.mp4`). Required for non-native
  multi-segment runs (`extract_last_frame` GETs this URL to decode the
  tail frame). Servers that omit it leave `Artifact.url == ""`; calling
  `extract_last_frame` then raises `FrameExtractionError` with a clear
  message instead of attempting a corrupt fetch.

## Hosted response URL — `url_path`

Hosted providers vary on response body shape. Configure
`engine.hosted.url_path` as a dot-separated path into the
`/status/{job_id}` response body where the rendered video's URL lives.

Examples:

| Provider response | `url_path` |
|---|---|
| `{"video": {"url": "..."}}` | `video.url` |
| `{"output_url": "..."}` | `output_url` |

The walker returns `""` for missing paths or non-string terminals; the
engine then raises `FrameExtractionError` rather than fetching a bogus
URL. Array indexing (e.g. `results[0].url`) is not supported.

## Cross-engine prompt routing

The user prompt supplied at the CLI (or via `GenerationRequest.prompt`)
is placed on `Segment.prompt` by the orchestrator. `HostedAPIBackend`,
`DiffusersBackend`, `ComfyUIBackend`, and `FalBackend` all route it
into their request body via `kinoforge.core.prompt_routing.resolve_prompt`.

- Hosted / Diffusers / Fal: top-level `body["prompt"]` (configurable
  on hosted/diffusers via `engine.<name>.prompt_body_key`; set to
  `null` to disable).
- ComfyUI: into `node_overrides[node_id]["inputs"]["text"]` for each
  entry in `spec["prompt_node_ids"]` (declare in spec alongside
  `asset_node_ids`).

An explicit `spec["prompt"]` always wins over the segment-supplied prompt.

## Engine asset wiring — non-native multi-segment continuity

Non-native multi-segment runs (engines whose `ModelProfile` reports
`supports_native_extension=False`, chained over N > 1 segments) extract
and persist the tail frame of each segment as a PNG in the `ArtifactStore`
under the run's namespace, inject a `ConditioningAsset(role="init_image")`
into the next job's `segments[0].assets`, and each engine's `submit()`
folds that asset's URI into the request body or graph it sends to the
backend. End-to-end the chain now produces visually continuous output on
non-native engines. ffmpeg must be on `PATH` on whichever host runs the
engine.

Each engine declares *how* to wire each role through a small config
contract. Today only the `init_image` role is wired; other roles
(`first_frame`, `last_frame`, `drive_audio`, `source_video`) are deferred
— no engine declares support yet.

**Diffusers** — `engine.diffusers.asset_paths` maps each supported role
to a dot-separated path inside the POST `/generate` request body. At
submit time the backend resolves the seg-0 asset of that role and writes
its URI at the path (passthrough — the inference server is responsible
for fetching the URI):

```yaml
engine:
  kind: diffusers
  diffusers:
    base_url: http://127.0.0.1:8000
    asset_paths:
      init_image: init_image
```

**Hosted** — `engine.hosted.asset_paths` is the same pattern, addressing
the provider-specific request body. The dot-path can be nested to reach
into provider-specific shapes:

```yaml
engine:
  kind: hosted
  hosted:
    url_path: video.url
    asset_paths:
      init_image: "input.image_url"
spec:
  model: "fal-ai/some-i2v-model"
```

**ComfyUI** — `spec.asset_node_ids` maps each supported role to the
`LoadImage` (or equivalent) node ID in the workflow graph. At submit
time the backend fetches the asset bytes, uploads them to ComfyUI's
`/upload/image` endpoint (with a hardened multipart body — random
boundary, escaped filename, `AssetFetchError` wrapping for missing
`name` / malformed JSON), and patches the named node's `inputs.image`
field with the uploaded filename. Graph authors set this in the job
spec:

```yaml
spec:
  graph:
    "12":
      class_type: LoadImage
      inputs:
        image: placeholder.png
  asset_node_ids:
    init_image: "12"
```

Failures across all three engines surface as `AssetFetchError`
(a `KinoforgeError` subclass, symmetric with `FrameExtractionError`):
missing role, empty `ref.uri`, HTTP fetch failure, ComfyUI upload
failure, malformed `/upload/image` response.

Native multi-segment engines (those declaring
`supports_native_extension=True` in their `ModelProfile`) are unaffected —
they receive all segments in a single job and handle continuity internally.


## Upscalers

Upscalers are a parallel registry to the generation engines. They are
keyed by `cfg.upscale.engine` and resolved via
`kinoforge.core.registry.get_upscaler`.

| Name | Class | Status | Provision surface |
|------|-------|--------|-------------------|
| `spandrel` | `kinoforge.upscalers.spandrel.SpandrelEngine` | v1 default | HTTP via the diffusers server's `/upscale` + `/upscale/status/{id}` |
| `seedvr2`  | `kinoforge.upscalers.seedvr2.SeedVR2Engine`   | `kinoforge[seedvr]` extras (Phase 2) | same — HTTP via the diffusers server |

### `spandrel` (v1 default)

Per-frame super-resolution via the
[spandrel](https://github.com/chaiNNer-org/spandrel) library — the SR
runtime that backs chaiNNer and ComfyUI custom nodes. `spandrel`'s
`ModelLoader` auto-detects the underlying architecture (RealESRGAN,
ESRGAN, SwinIR, OmniSR, ...) from a `.pth` or `.safetensors` weights
file, so a single engine surface supports the entire ecosystem of
published SR weights.

**Quality tradeoff (v1).** Per-frame inference has no temporal model:
adjacent frames are upscaled independently, which can introduce subtle
flicker on high-frequency texture (foliage, hair). The tradeoff was
made deliberately to ship a packaged default in P2 while video-coherent
upscaling (SeedVR2) waits on Phase 2 vendoring of an unpackaged
upstream. Operators who need temporal coherence should hold for the
`[seedvr]` extras path.

**Cfg surface.** See `examples/configs/upscale-spandrel-x2.yaml`
(upscale-only, for `kinoforge upscale`) and
`examples/configs/wan-with-upscale-spandrel.yaml` (Wan T2V → spandrel
multi-stage warm-reuse, for `kinoforge generate`). Per-engine knobs
live under `cfg.upscale.spandrel`:

- `model_url` — source ref for the weights (`hf:`, `civitai:`,
  `civarchive:`, plain http(s)). Fetched at provision time via
  `python -m kinoforge.upscalers.spandrel._fetch_weights`.
- `arch` — architecture token surfaced in the model-identity slug
  (informational; the runtime auto-detects from the weights file).
- `precision` — `"fp16"` (default) or `"fp32"`.
- `tile_size` / `batch_size` — VRAM-vs-throughput knobs.

### `seedvr2` (extras-gated, Phase 2)

Video-coherent diffusion upscaling via ByteDance-Seed/SeedVR2. The
upstream repository ships as research scripts with no `setup.py` /
`pyproject.toml` (verified 2026-06-29), so `pip install seedvr @
git+...` is not feasible. The engine self-registers under `"seedvr2"`
but its four heavyweight ABC methods (`render_provision`, `provision`,
`upscale`, `validate_spec`) raise `ExtrasNotInstalled` until the Phase
2 workstream vendors `projects/inference_seedvr2_*.py` + `common/` +
`models/` into `src/kinoforge/upscalers/seedvr2/_vendored/`.

A PREFLIGHT validation check (`seedvr2_extras_pending`) refuses
`cfg.upscale.engine == "seedvr2"` at `kinoforge generate` pre-flight
so operators see the structured remediation hint BEFORE any pod is
created. The example cfgs are kept under `examples/configs/extras/` as
forward-compatible references.
