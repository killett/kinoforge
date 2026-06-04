# kinoforge

kinoforge is a configuration-driven video-generation orchestrator. It abstracts over GPU compute providers (RunPod, SkyPilot, local), generation engines (ComfyUI, Diffusers, hosted APIs), and model sources (HuggingFace, CivitAI, plain HTTPS) behind a single YAML config file and a small CLI. Swapping providers, engines, or model sources requires only a config edit — no code changes, no branching on provider names in core logic.

## Quickstart

```bash
# Install dependencies
pixi install

# Dry-run: print the deployment plan without touching any cloud resources
pixi run python -m kinoforge --state-dir ~/.kinoforge \
  deploy --config examples/configs/local-fake.yaml --dry-run

# Generate a clip offline (FakeEngine + LocalProvider, no GPU required)
pixi run python -m kinoforge --state-dir ~/.kinoforge \
  generate --config examples/configs/local-fake.yaml \
           --prompt "ocean waves at sunset" --mode t2v --run-id run01
```

Expected output sketch for the dry-run:

```
[dry-run] engine=fake  provider=local
  capability_key: <sha256-prefix>
  offers available: 2
  lifecycle: idle_timeout=3600s  max_lifetime=10800s  budget=$10.00
  models: 1 entry (1 base, 0 lora, 0 vae)
```

## Batch generation

Render N clips on one shared deployed instance with continue-on-error
semantics:

```bash
kinoforge batch -c examples/configs/wan.yaml \
                --manifest examples/configs/batch-prompts.yaml
```

The manifest is a YAML list. Each entry sets exactly one of `prompt`
(inline text) or `prompt_file` (path resolved relative to the manifest's
parent directory). Optional per-entry overrides: `params`, `spec`,
`assets`, `run_id`.

```yaml
# examples/configs/batch-prompts.yaml
- prompt: "waves crashing on basalt cliffs at dusk"
  mode: t2v
  run_id: waves

- prompt_file: prompts/forest.txt
  mode: t2v
  run_id: forest
  params: { seed: 42 }
```

**Outputs.** Each entry's artifact lands at
`<store>/<batch_id>/<run_id>/<name>`. Default `batch_id` is
`batch-YYYYMMDD-HHMMSS` in **local timezone**; override with
`--batch-id ID` for a memorable name. A machine-readable summary is
written to `<batch_id>/_batch_summary.json` on every exit path (success,
per-entry failures, batch-fatal abort).

**Concurrency.** `--concurrent N` overrides `cfg.lifecycle.max_in_flight`.
Both layers (outer entry executor and `ConcurrentPool` slot cap) share
the same value. After the run, the CLI prints a per-entry summary table;
intra-run streaming progress is a deferred follow-up — see PROGRESS.md
Phase 22.

**Failure semantics.** Per-entry exceptions become `FAIL` outcomes; the
batch keeps going. Batch-fatal exceptions (`BudgetExceeded`,
`CapabilityMismatch`, `TeardownError`) cancel queued entries and exit
with code 2. The summary JSON is written before the exit in every case.

**Cleanup.** `kinoforge gc --run <batch_id> -c <config>` walks the entire
batch namespace at once.

## Breaking changes

### Layer M — `engine.hosted.model` removed; use top-level `spec.model`

Hosted configs that previously declared the model identifier under
`engine.hosted.model` must move the value to top-level `spec.model`. The
two locations carried the same string in every shipped config, with a
"keep these in sync" comment block as the only safeguard. Layer M
collapses them: `spec.model` is now the single source of truth, read both
by `HostedAPIBackend.submit` (wire body) and by
`HostedAPIEngine.key_base` (cache identity).

Migration:

```diff
 engine:
   kind: hosted
   hosted:
     provider: my-shim
     endpoint: "https://shim/inference"
-    model: "wan-ai/Wan2.2-T2V-A14B"
     api_key_env: "MY_SHIM_KEY"
     health_url: "https://shim/health"
     url_path: video.url

 spec:
   model: "wan-ai/Wan2.2-T2V-A14B"
```

Failure mode: configs still carrying `engine.hosted.model` raise a
load-time `ValidationError` with the message
`"engine.hosted.model is no longer supported; move the value to
top-level spec.model"`.

## Configuration

Each kinoforge run is described by a single YAML file with three top-level blocks:

```yaml
engine:      # which generation backend to use + precision
models:      # ordered list of model refs (base + optional loras/vae)
compute:     # where to run (provider + image + hardware + lifecycle/budget)
```

For hosted engines (e.g. fal.ai) the `compute:` block is omitted and a top-level `lifecycle: {budget: N}` carries the spend guard instead.

Browse ready-to-use examples in [`examples/configs/`](examples/configs/):

| File | Engine | Provider | Use case |
|------|--------|----------|----------|
| [`wan.yaml`](examples/configs/wan.yaml) | ComfyUI | RunPod pod | Production Wan2.2 + CivitAI LoRA |
| [`diffusers.yaml`](examples/configs/diffusers.yaml) | Diffusers | RunPod serverless | SVD serverless |
| [`hosted.yaml`](examples/configs/hosted.yaml) | Hosted API | fal.ai | Zero-infra hosted |
| [`local-fake.yaml`](examples/configs/local-fake.yaml) | Fake | Local | Offline / CI smoke test |

### Concurrency

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

### Multi-node coordination

Multi-node deployments where several `kinoforge` workers point at one shared
artifact store (S3 or GCS) are coordinated by a lease-based mutex returned
from `ArtifactStore.acquire_lock(key, *, ttl_s)`. Local-disk stores use
`fcntl.flock`; S3 uses conditional PUT (`If-None-Match: *`); GCS uses native
`if_generation_match=0`.

Two surfaces use the lock automatically:

1. **Profile discovery** (`JsonProfileCache.resolve_or_discover`) — only one
   worker probes the live model for a given `CapabilityKey`; followers read
   the cached profile.
2. **Ledger mutations** (`Ledger.record`, `Ledger.forget`) — read-modify-write
   stays atomic across workers; entries cannot be lost to concurrent updates.

Semantics are best-effort: a holder that dies mid-hold has its lease expire
after `ttl_s`, at which point another acquirer can steal. There are no
fencing tokens. Sized TTLs absorb modest clock skew.

Tune via constructor kwargs (no YAML surface):

```python
JsonProfileCache(store, discover_ttl_s=600.0)
Ledger(store, mutate_ttl_s=60.0)
```

### Remote provisioning

Engines that talk to a remote pod (ComfyUI, Diffusers on RunPod / SkyPilot)
bootstrap via `engine.render_provision(cfg)`. The engine emits a self-
contained bash script that clones its repo, installs dependencies, downloads
weights, and launches the inference HTTP server. The orchestrator validates
declared credential env vars, attaches the rendered payload to
`InstanceSpec`, and the provider injects it via its native boot-script
mechanism (RunPod base64-encoded env var + `dockerArgs`; SkyPilot
`Task.setup` / `Task.run`).

After the pod boots, `engine.provision(instance, cfg)` polls an engine-
specific ready endpoint (ComfyUI: `/system_stats`; Diffusers: `/health`)
until HTTP-200, the pod status flips terminal, or
`cfg.lifecycle.boot_timeout` (default 900s) elapses. Failures raise
`ProvisionFailed` (terminal status) or `ProvisionTimeout` (deadline).

No SSH required. Local users see zero behavioural change — engines branch
on `instance.provider == "local"` and run the existing local bootstrap.

Credentials referenced by the script (e.g. `$HF_TOKEN`) are lifted from
the configured `CredentialProvider` onto `spec.env` by the orchestrator.
The script string never carries plaintext token values.

### HuggingFace ref grammar

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

## Credentials

Kinoforge reads its API credentials from environment variables. To avoid
exporting them in `~/.bashrc`, copy the checked-in template:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and fill in the keys you need.
```

The CLI auto-loads `./.env` from whatever directory you invoke `kinoforge`
in (typically the project root). Absent file is a silent no-op — you'll
get an `AuthError` on first secret use instead. To load a different file
explicitly:

```bash
kinoforge --env-file /path/to/other.env generate --config ...
```

### Precedence

Shell-set values **always win** over `.env` values. CI/prod exports always
take precedence over a stale dev `.env`. To override this in your own
Python scripts, call
`kinoforge.core.dotenv_loader.load_env_file(path, override=True)`.

### Known keys

| Variable | Used by | Required when |
|---|---|---|
| `FAL_KEY` | `HostedAPIEngine` (fal.ai) | Hosted engine path against fal.ai |
| `CIVITAI_TOKEN` | `CivitAISource` | Downloading gated/private CivitAI models |
| `HF_TOKEN` | `HuggingFaceSource` | Downloading gated/private HF repos |
| `RUNPOD_API_KEY` | `RunPodProvider` | Provisioning RunPod compute |

AWS / GCP credentials are NOT managed by kinoforge — the `boto3` and
`google-cloud-storage` SDKs walk their own default credential chains
(env → `~/.aws/credentials` → IMDS → IAM role / ADC → gcloud config →
GCE metadata) unchanged. You may put `AWS_ACCESS_KEY_ID` +
`AWS_SECRET_ACCESS_KEY` (boto3 needs both), `GOOGLE_APPLICATION_CREDENTIALS`,
etc. into your `.env` if you prefer a single file; the SDK chains pick
them up via `os.environ`.

### Never commit `.env`

`.env` is in `.gitignore`. Only commit `.env.example` (no values).

### Credential safety in tests

Secrets enter kinoforge tests via `.env` only — never via test code, fixtures, example YAML, or
commit messages. The `_RecordingHTTPSeam` in `tests/providers/conftest_runpod.py` runs a layered
redaction pipeline over every captured payload and refuses (via `CredentialLeakError`) to write a
fixture that still contains a credential pattern. See [`AGENTS.md`](AGENTS.md) for the contributor
guide, the pattern table, and the procedure for adding a new credential format.

### Faster downloads (aria2c)

kinoforge auto-detects `aria2c` on `PATH` and uses it as a transparent
multi-connection fast-path for every model fetch. With aria2c installed
on a typical residential link, the Wan 2.1 weight set (~9 GiB total)
downloads in roughly one-tenth the wall-clock time it takes via the
stdlib transport.

Install:
- Debian / Ubuntu: `sudo apt install aria2`
- macOS (Homebrew): `brew install aria2`
- Windows (Chocolatey): `choco install aria2`

No configuration is required. If aria2c is absent, or if the subprocess
fails for any reason (CDN rate-limit, transient network error,
unexpected flag deprecation in a future aria2c release), the failure is
logged at `WARNING` level and the stdlib single-connection path is used
as a fallback — operators always get the file.

## Real providers — fal.ai

kinoforge ships with a fal.ai sibling engine (`FalEngine`) for video generation
via fal's queue API.

**Setup:**

1. Put your fal.ai key in `.env` at the repo root:
   ```
   FAL_KEY=fal-XXXXXXXX
   ```
2. Pick a model — `examples/configs/fal.yaml` defaults to Wan2.2 T2V.
3. Run:
   ```bash
   pixi run python -m kinoforge --env-file .env generate \
     -c examples/configs/fal.yaml \
     --prompt "a cat sitting on a fence" --mode t2v
   ```
4. Artifact lands under `.kinoforge/run/<run-id>/`.

To run the live test suite (`pixi run test-live`), set `KINOFORGE_LIVE_TESTS=1`
alongside `FAL_KEY` in your environment.

### Real providers — RunPod

kinoforge ships an opt-in live smoke against the real RunPod GraphQL API
that validates the provider's pod lifecycle end-to-end. It is skipped by
default and never runs in CI.

```bash
export RUNPOD_API_KEY=...
export RUNPOD_TERMINATE_KEY=$RUNPOD_API_KEY    # see .env.example

KINOFORGE_LIVE_TESTS=1 \
pixi run pytest tests/live/test_runpod_live.py -v
```

To refresh the committed GraphQL response fixtures (e.g. after a RunPod
schema upgrade), add the capture flag:

```bash
KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 \
pixi run pytest tests/live/test_runpod_live.py -v
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
`examples/configs/runpod-comfyui-wan*.yaml` are committed as forward
scaffolding for that work.

**Note on RUNPOD_TERMINATE_KEY:** the selfterm.py design predates RunPod's
scoped-key feature; RunPod's current scoped-key UX is two-level (GraphQL
read or read+write, OR per-endpoint serverless) with no native
terminate-only scope. Until that ships, reusing the main key via
`${RUNPOD_API_KEY}` interpolation is the documented pattern; the
selfterm fallback still works, only the privilege separation is lost.

#### Engine integration (ComfyUI + Wan i2v)

End-to-end RunPod → ComfyUI → Wan 2.1 i2v generation. Drives a real RunPod pod that boots ComfyUI with the kijai WanVideoWrapper graph and produces an MP4.

**Required env vars:**
- `RUNPOD_API_KEY` — RunPod REST API key (least-privilege; see "Credential safety in tests")
- `HF_TOKEN` — Hugging Face token (for on-pod model downloads)

**Optional env vars (live-test runner only):**
- `KINOFORGE_LIVE_KEEP_POD=1` — read by `tests/live/test_comfyui_wan_live.py`; when set, the live test skips the destroy step so re-runs reuse the same pod via tag lookup. Not consumed by the `kinoforge generate` CLI.

**Quickstart:**

```bash
pixi run kinoforge generate \
  --config examples/configs/wan.yaml \
  --prompt "a cat turns into a woman" \
  --init-image tests/providers/fixtures/runpod/sample_init_frame.png
```

**Dev loop via the live test runner:**

```bash
KINOFORGE_LIVE_KEEP_POD=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v
# iterate: tweak graph JSON / fixture / prompt, re-run with the same KINOFORGE_LIVE_KEEP_POD=1
# pod stays warm and auto-reaps after idle_timeout (configured at 2h in examples/configs/wan.yaml)
# manual reap:
pixi run kinoforge destroy <pod_id>
```

**Cost shape:**
- Pod: NVIDIA RTX 3090 @ ~$0.27/hr (varies by region/availability)
- Cold boot (first run; downloads model weights): ~12–20 min wall-clock, ~$0.05–0.09
- Warm reuse: ~5 min, ~$0.025
- Always run `pixi run preflight` before live spend (checks zero active pods, clean tree, creds present)

**Configuration files:**
- `examples/configs/wan.yaml` — Wan 2.1 i2v engine config (lifecycle, params, model entries)
- `examples/configs/runpod-comfyui-wan.graph.json` — kijai WanVideoWrapper API-format graph

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

See `examples/configs/hosted.yaml`, `diffusers.yaml`, `wan.yaml`, and `fal.yaml`
for working `spec:` + `params:` shapes per engine.

## Extending: add a provider/source/engine

kinoforge's registry lets you add a new adapter in a single file without touching core. Each pattern follows the same three steps: subclass the ABC, implement the required methods, and call the register function once at module import.

### New ComputeProvider

```python
# src/kinoforge/providers/myprovider/__init__.py
from kinoforge.core.interfaces import (
    ComputeProvider, GpuOffer, InstanceSpec, Instance, Lifecycle,
)
from kinoforge.core.registry import register_provider

class MyProvider(ComputeProvider):
    def find_offers(self, requirements, lifecycle) -> list[GpuOffer]: ...
    def create_instance(self, spec: InstanceSpec) -> Instance: ...
    def get_instance(self, instance_id: str) -> Instance: ...
    def list_instances(self) -> list[Instance]: ...
    def stop_instance(self, instance_id: str) -> None: ...
    def destroy_instance(self, instance_id: str) -> None: ...
    def heartbeat(self, instance_id: str) -> None: ...
    def endpoints(self, instance: Instance) -> dict[str, str]: ...

register_provider("myprovider", MyProvider)
```

Set `compute.provider: myprovider` in your YAML — no other changes.

### New ModelSource

```python
# src/kinoforge/sources/mystore/__init__.py
from kinoforge.core.interfaces import ModelSource, Artifact
from kinoforge.core.registry import register_source

class MyStoreSource(ModelSource):
    def handles(self, ref: str) -> bool:
        return ref.startswith("mystore:")

    def resolve(self, ref: str) -> Artifact:
        # return an Artifact with url + headers
        ...

register_source(MyStoreSource())
```

Use `ref: "mystore:org/model:file.safetensors"` in the `models:` list.

### New GenerationEngine

```python
# src/kinoforge/engines/myengine/__init__.py
from kinoforge.core.interfaces import GenerationEngine, GenerationBackend
from kinoforge.core.registry import register_engine

class MyEngine(GenerationEngine):
    requires_compute: bool = True
    requires_local_weights: bool = True

    def provision(self, instance, cfg) -> None: ...
    def backend(self, instance, cfg) -> GenerationBackend: ...
    def validate_spec(self, spec: dict) -> None: ...

register_engine("myengine", MyEngine)
```

Set `engine.kind: myengine` in your YAML.

### Diffusers inference-server response contract

`DiffusersBackend.result()` polls `GET /status/{job_id}` and reads two
fields from a successful (`status: done`) response:

- `filename` — display name for the produced clip.
- `url` — HTTP-fetchable location for the produced clip (e.g.
  `http://127.0.0.1:8000/file/clip.mp4`). Required for non-native
  multi-segment runs (`extract_last_frame` GETs this URL to decode the
  tail frame). Servers that omit it leave `Artifact.url == ""`; calling
  `extract_last_frame` then raises `FrameExtractionError` with a clear
  message instead of attempting a corrupt fetch.

### Hosted response URL — `url_path`

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

### Cross-engine prompt routing

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

### Engine asset wiring — non-native multi-segment continuity

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

### New Splitter

```python
# src/kinoforge/splitters/mysplitter/__init__.py
from kinoforge.core.interfaces import ModelProfile, Segment, Splitter
from kinoforge.core.registry import register_splitter

class MySplitter(Splitter):
    name = "mysplitter"

    def split(
        self, prompt: str, profile: ModelProfile, params: dict
    ) -> list[Segment]:
        # Return ordered segments derived from prompt + profile + params.
        ...

register_splitter("mysplitter", lambda: MySplitter())
```

Set `splitter.kind: mysplitter` in your YAML. The default `"heuristic"` splitter (`core/splitter.py`) splits on blank lines; plug an LLM-semantic or scene-detect strategy here.

### New ArtifactStore

Three stores ship in-tree: `LocalArtifactStore` (filesystem, default), `S3ArtifactStore` (`s3://` URIs, registered as `"s3"`), and `GCSArtifactStore` (`gs://` URIs, registered as `"gcs"`). Add a fourth backend by subclassing the ABC and self-registering:

```python
# src/kinoforge/stores/mystore/__init__.py
from kinoforge.core.interfaces import Artifact
from kinoforge.core.registry import register_store
from kinoforge.stores.base import ArtifactStore

class MyArtifactStore(ArtifactStore):
    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact: ...
    def get_bytes(self, uri: str) -> bytes: ...
    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact: ...
    def get_json(self, uri: str) -> dict: ...
    def list(self, run_id: str) -> list[str]: ...
    def delete(self, uri: str) -> None: ...
    def uri_for(self, run_id: str, name: str) -> str: ...

register_store("mystore", lambda: MyArtifactStore(...))
```

Set `store.kind: mystore` in your YAML.

## Output directory

Final clips publish to a flat user-visible directory (default `output/` at
the repo root) with filenames of the form:

    YYYYMMDD-HHMMSS_<prompt-slug>.<ext>

* The timestamp is local-TZ at the moment the clip finishes.
* The slug is the first 20 ASCII-safe characters of the prompt; emoji,
  CJK, accented characters, and punctuation are dropped (the slug
  pipeline is ASCII-conservative for cross-platform safety and
  grep/tab-complete ergonomics).
* Collisions in the same second resolve as `_2`, `_3`, … `_99`, then a
  6-character sha256 hash.
* Batch entries nest under `output/<batch_id>/` for grouping.

The internal artifact store (profile cache, ledger, weights cache,
intermediate segment artifacts) is unchanged — it still lives under
`--state-dir` (default `.kinoforge/`) and is operator-facing, not
user-facing. The output dir is a *publish* target, not a replacement
for the store.

### Configuring it

YAML block (optional; absent block uses the defaults below):

```yaml
output:
  kind: local            # only "local" ships in v1
  dir: output            # relative-to-cwd, or absolute
  enabled: true          # set false to skip publishing
```

CLI flags (overrides YAML):

* `--output-dir PATH` — publish here instead of the YAML default.
* `--no-output-dir` — skip publishing for this invocation.
* Flags are mutually exclusive.

### `--run-id` change

The `kinoforge generate --run-id` default changed from the literal
string `"run"` to `f"run-{YYYYMMDD-HHMMSS}"` (local TZ at invocation
time). This closes a silent-overwrite foot-gun where two successive
`kinoforge generate` calls without explicit `--run-id` would overwrite
each other's internal artifact + ledger entry. Pass `--run-id run` to
restore the prior behavior verbatim. Batch runs are unaffected — each
manifest entry already names its own `run_id`.

## Roadmap (deferred layers and their seams)

Each item below names the deferred layer and the exact seam it plugs into when built:

- **Continuity / stitching fallback** — `strategy.decide` non-native branch; the fallback path currently issues N single-segment jobs; stitching post-processing slots in between `pool.map` and `store.put_bytes` in `GenerateClipStage`.
- **Audio sync layer** — `strategy.decide` sets `spec["_audio_mode"] = "separate"` as a marker; a downstream audio-sync stage reads this key and schedules audio generation after the video clip is stored.
- **Distributed / cross-process backend scheduler** — `ConcurrentPool` (Layer G) handles in-process thread-level concurrency; a future `RayPool` or cross-process variant would slot into the same `BackendPool` ABC without touching the stage or orchestrator.
- **Keyframe / image-generation upstream Stage** — `Stage` Protocol + `ConditioningAsset` with `kind="image"`; add an `ImageGenStage` that satisfies `Stage` and feeds its output into the video generation stage's `segments_override`.
- **Cross-process discovery lock** — `ModelProfileProvider` currently uses an in-process threading.Event for single-flight; replace with a file-lock or Redis-backed lock for multi-process / distributed workers.

## Design references

The `providers/skypilot/` adapter wraps [SkyPilot](https://github.com/skypilot-org/skypilot) (Apache 2.0, UC Berkeley Sky Computing Lab). SkyPilot was a major influence on kinoforge's `ComputeProvider` abstraction, particularly the autostop mapping (`idle_timeout_s → autostop minutes`), the cost-aware GPU offer selection model, and the principle that cloud portability should be configuration-level rather than code-level. We credit the SkyPilot authors and recommend their work for anyone building on cloud-portable ML infrastructure.
