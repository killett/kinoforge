# Text generation on reserved compute: `kinoforge text`

**Status:** design approved 2026-10-04, not yet planned.
**Scope:** a terminal text-generation command. One prompt and zero or more images in, one
completion out, on a pod kinoforge books, boots and tears down. A new engine seam
(`TextEngine`), one engine implementation (`transformers`), one pod-side server, three
example configs, two live smokes.
**Non-scope (§13):** the two pipeline hooks (prompt enhancement, frame QA) — a follow-on
spec that depends on this one; Modal and SkyPilot text configs; vLLM and the quantised 24
and 48 GB tiers; video input to vision models; multi-turn chat, streaming, batch prompts,
logprobs; migrating the Wan server onto the shared upload module.
**Research input:** `docs/superpowers/research/2026-10-04-open-weight-llm-survey.md`.

## 1. The question, and the answer

*kinoforge can book a GPU, boot an open-weight model behind an HTTP server on it, send a
prompt plus conditioning images, poll a job to completion and publish the result. Why can
it not do that for a language model?*

Because nothing in the tree speaks text. `rg -i 'vllm|AutoModelForCausalLM|AutoTokenizer'`
across `src/`, `examples/` and `pyproject.toml` returns zero hits; `transformers` appears
once, as a pip string in the MiniMax-H3 config. Every pod-side server renders frames and
every artifact is an `.mp4` or a `.png`.

But every piece *around* the model already exists and is live-proven: the diffusers engine
provisions an arbitrary `server_cmd` (`engines/diffusers/__init__.py:1156`; the H3 config
runs a server that is not the Wan server), `deploy_session` books and tears down the pod,
the ledger and heartbeat watch it, warm-attach matches it by capability key, the pod HTTP
mixin uploads a PNG with a sha256 cross-check (`engines/_pod_http.py:197`), and the
orchestrator already runs a pipeline whose head is not a text-to-video call
(`skip_clip_stage`, `core/orchestrator.py:2609`).

**The answer is a new command, a new config block, a new engine seam shaped like
`UpscalerEngine`, a new stage shaped like `UpscaleStage`, and a lean pod-side server
built on the H3 skeleton.** Everything between them is reused.

### 1.1 Why `upscale` is the sibling and `image` is not

`kinoforge image` (`docs/superpowers/specs/2026-10-03-standalone-image-generation-design.md`)
is built on one premise: every image engine declares `requires_compute = False`, so there is
no pod, no ledger row, no heartbeat, no warm reuse, and the config allowlist *forbids*
`engine:` and `compute:`. A text command that reserves compute inverts every one of those.

`kinoforge upscale` is the command that takes a local input file, reserves a pod through
the orchestrator, honours `--no-reuse` and `--attach-pod`, uploads the input to the pod,
and publishes through the shared sink (`cli/_commands.py:1095`). That is the shape.

### 1.2 Two approaches rejected

**Text through the clip path as a `GenerationBackend`.** The text server would speak the
existing `/generate` + `/status` + `/artifacts` contract and `text` would be a thin wrapper
over the video path. Least code — but the clip path has no upload step (it writes asset
URIs into the request body and expects the pod to fetch them, which a controller-local
`file://` cannot satisfy on RunPod with a local store), and the splitter, continuity
chaining and `.mp4` publish are all video assumptions to route around. The hooks (§13.1)
would still need a separate client and provision fragment.

**A standalone run function with its own `deploy_session` call.** Conceptually clean, but it
re-implements the warm-reuse, ledger and reaper interplay that lives in
`orchestrator.generate` — the highest-defect-density code in the repo, by the URGENT
ACTION ITEMS history.

## 2. Surface

```
kinoforge text -c <cfg> [--prompt TEXT] [--image PATH ...]
               [--no-reuse | --attach-pod ID]
               [--output-dir PATH | --no-output-dir]
               [--run-id ID] [--dry-run]
```

### 2.1 The mode is derived, never typed

No `--mode`. Zero `--image` flags means mode `t2t`; one or more means `it2t`. The names are
the Hugging Face task names (`text-generation`, `image-text-to-text`) in the repo's
`x2y` spelling, and they sit beside `t2v` / `i2v` / `flf2v` in `MODE_ROLE_REQUIREMENTS`
(`core/interfaces.py:801`) as `"t2t": {}` and `"it2t": {}` — no *required* role, because
the image count is open-ended. Image assets carry roles `image_1 … image_N` in flag
order.

`--image` accepts `.png`, `.jpg`, `.jpeg` only — the set `_IMAGE_CONTENT_TYPES` already
accepts (`engines/_pod_http.py:39`). Anything else, or a path that does not exist, is a
precondition fault.

### 2.2 Prompt precedence: CLI wins

```
--prompt  >  cfg.text.prompt  >  cfg.prompt  >  refuse
```

Identical to `image` (§3.2 of that design), for the same reasons: a config is runnable
bare, and CLI-over-config is the established direction.

### 2.3 Flags, and their absence

`--no-reuse`, `--attach-pod`, `--run-id`, `--output-dir`, `--no-output-dir`, `--dry-run`
mirror `upscale` exactly, including the `--no-reuse`/`--attach-pod` mutual exclusion
checked before any config load. `--dry-run` prints the resolved plan (mode, prompt source,
image count, engine, model, declared modes, reuse flags) and exits 0 with zero HTTP.

No `--system`, `--max-tokens`, `--temperature`: generation parameters live in
`text.params` (§3) so a run is reproducible from its config and its `.json` sidecar, and
the CLI surface stays one flag per *input*, not one per knob.

`--ephemeral` needs nothing new. A text config carries `engine.kind: diffusers` and a
`compute.provider`, so `_preflight_ephemeral`'s normal `(engine_kind, provider)` lookup in
`EPHEMERAL_CAPABILITIES` (`core/ephemeral.py:99`) decides — the `image` carve-out is not
involved.

### 2.4 Wiring that is easy to forget

`text` MUST be added to `_DISPATCH` (`cli/_main.py:154`), to `_INTERRUPTIBLE_CMDS`
(`:97`, so a SIGINT drains the poll through `ctx.cancel_token`), and to the
`_propagate_session_globals` node list (`:349`). The last is the U27/U29 defect shape: a
subcommand that re-declares a session global instead of inheriting it ran non-ephemerally
while reporting success. A test pins all three.

### 2.5 Ordering of refusals

Following `_cmd_upscale`: flag conflicts first (no config needed), then config presence,
then every config-fact refusal (§4.1) **before** the `--dry-run` block, so a dry run
surfaces them too — and long before any ledger row, warm scan or pod create.

### 2.6 Exit codes

**2** for config and precondition faults (no `text:` block, no prompt resolvable, missing
or non-image `--image`, images against a model whose declared modes lack `it2t`, unknown
text engine). **1** for operational failure (boot timeout, pod-side error, poll timeout,
upload integrity, cancelled, pod-reported mode mismatch). **0** on success and dry run.

## 3. Config: `text:` coexists with `engine:`, `models:`, `compute:`

```yaml
engine:
  kind: diffusers
  precision: bf16
  diffusers:
    image: "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
    server_cmd: ["python", "-m", "kinoforge.engines.diffusers.servers.text_server"]
    pip:
      - "transformers>=5.10"
      - "accelerate>=1.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "pillow>=10"
      - "psutil>=5.9"
      - "nvidia-ml-py>=12"
    embed_files:
      - "kinoforge.engines.diffusers.servers.text_server"
      - "kinoforge.engines.diffusers.servers._util_stats"
      - "kinoforge.engines.diffusers.servers._upload"
    capability:
      supported_modes: ["t2t"]            # ["t2t", "it2t"] for a vision-language model

models:
  - ref: "hf:Qwen/Qwen3-0.6B"
    kind: base

text:
  engine: transformers                   # TextEngine registry key
  prompt: "..."                          # optional default
  system: "..."                          # optional system turn
  params:                                # opaque pass-through to the server (§6.3)
    max_new_tokens: 256
    temperature: 0.7
    chat_template_kwargs:
      enable_thinking: false

compute: { ... }                         # unchanged RunPod / Modal / SkyPilot block
output: { dir: output }
store: { kind: local }
```

### 3.1 `TextConfig`

```python
class TextConfig(BaseModel):
    engine: str
    prompt: str | None = None
    system: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    port: int = 8000
    model_config = ConfigDict(extra="forbid")
```

`port` is the pod port the text server listens on and the client reads; only the default
is exercised in this spec. It exists because §5.2 and §13.1 need the seam, and a field a
sidecar launch will set is cheaper to add now than to retrofit under `extra="forbid"`.

`Config.text: TextConfig | None = None`. Shaped like `UpscaleConfig` (`config.py:798`),
not like `ImageConfig`: it is one more block a compute-bearing config may carry.

### 3.2 The model ref stays in `models[kind: base]`

Deliberately not `text.model`. With the ref where every other pod config keeps it:

- `Config.capability_key()` derives `base_model` from it unchanged (`config.py:1722`), so
  two text configs for different checkpoints never warm-attach to each other's pod;
- the diffusers engine's existing `WAN_MODEL_ID` export (`engines/diffusers/__init__.py:1394`)
  fires harmlessly, and the text fragment (§5.2) exports the same ref under its own name;
- the `test_no_unknown_slug_for_example_configs` regression lock sees a model identity.

### 3.3 Load-time validator

When `text:` is present, a `model_validator` refuses, naming the offending key or rule:

| rule | why |
|---|---|
| `engine.kind` must be `diffusers` | the only engine that provisions an arbitrary `server_cmd`; `comfyui` / `hosted` / `bedrock_video` have no text path |
| `engine.diffusers.capability.supported_modes` must be present, non-empty, and a subset of `{t2t, it2t}` | §4.1's pre-spend gate reads it; absent, the shared `_DEFAULT_PROBE` (`engines/diffusers/__init__.py:330`) says `{t2v}` and every text run would be refused for the wrong reason |
| top-level `mode` must be **absent** | the mode is derived from `--image`; a `mode:` here would be documentation that can lie, the anti-pattern the image design rejected (§4.1 there) |
| `upscale`, `interpolate`, `keyframe`, `loras` must be absent | each would be silently inert on a text pod — U51/U56's defect class. The hooks spec (§13.1) decides how a *video* config refers to a text model; this validator does not pre-empt it, because that spec will name its own block |
| `models` must contain exactly one `kind: base` entry | the server loads one checkpoint; a VAE or LoRA entry is inert |

The existing `base_count == 0` rule (`config.py:1685`) is untouched — text configs have a
base model.

### 3.4 Capability key

`Config.capability_key()` appends `"text"` to `stages` when `self.text is not None`
(beside the `upscale` / `interpolate` branches at `config.py:1798-1816`). `derive()`'s
conditional-extend keeps every legacy key byte-identical. Consequences in §9.

## 4. The multimodal gate, in two halves

The operator asked for "a warning at minimum, or a fast error" when images go to a
text-only model. This design does the fast error, twice, because the two failure shapes
are different.

### 4.1 Pre-spend: the config's declaration

In `_cmd_text`, before any pod work and before `--dry-run` prints:

```python
mode = "it2t" if image_paths else "t2t"
declared = set(cfg.engine.diffusers.capability.supported_modes)   # §3.3 guarantees present
if mode not in declared:
    -> exit 2: "model <ref> declares modes <declared>; --image needs it2t.
                Either drop --image or use a vision-language config such as
                examples/configs/runpod-diffusers-smolvlm-256m-it2t.yaml"
```

A test asserts the compute provider's `create_instance` was **never called** and no ledger
row was written. The weak version — assert-raises on exit code alone — passes against an
implementation that books the pod first and refuses after.

### 4.2 On the pod: the server's truth

A declaration can lie — the operator pastes a text-only checkpoint into a VLM config. So:

- `text_server.py` derives its real modes at load time (§6.2) and reports them in
  `GET /health` as `supported_modes`.
- `TextStage.run` (§7) reads `/health` **first** and raises `ValidationError` naming both
  the declared and the actual modes when `state.request.mode` is not in the actual set,
  before any upload and before `POST /text`. `orchestrator.generate` already tears an
  owned instance down on a stage `ValidationError` (its docstring, step 7), so a lying
  config costs one boot and nothing more.
- The server itself returns **400** on a `POST /text` carrying `images` when it has no
  image path — a third belt, for a client that is not kinoforge.

A warning-and-strip variant was rejected: it spends the pod's time on a request the
operator did not make, and the silently-changed request is exactly the shape this repo
files as a defect.

### 4.3 Why not `JsonProfileCache.verify`

The diffusers engine's "live probe" is `_DEFAULT_PROBE` plus the config's own override
(`engines/diffusers/__init__.py:378`) — it never reads the pod. `verify` therefore
compares the cache against the declaration, and a lying declaration verifies cleanly.
The stage-level `/health` check is the only place the pod's truth is consulted, which is
why it is explicit rather than delegated.

## 5. Engine seam: `TextEngine`

New ABC in `core/interfaces.py`, beside `UpscalerEngine` (`:1372`) and shaped like it:

```python
@dataclass(frozen=True)
class TextJob:
    prompt: str
    system: str | None
    images: tuple[str, ...]        # pod-side paths returned by upload_image
    params: dict[str, Any]

@dataclass(frozen=True)
class TextResult:
    text: str
    finish_reason: str             # "stop" | "length" | engine-specific
    usage: dict[str, int]          # prompt_tokens, completion_tokens
    model: str
    elapsed_s: float

@dataclass(frozen=True)
class TextHealth:
    ready: bool
    model: str
    supported_modes: frozenset[str]

class TextEngine(ABC):
    name: str
    requires_compute: bool

    def render_provision(self, cfg) -> RenderedProvision: ...   # default raises, like UpscalerEngine
    @abstractmethod
    def health(self, instance, cfg) -> TextHealth: ...
    @abstractmethod
    def upload_image(self, instance, local_path: Path) -> str: ...
    @abstractmethod
    def complete(self, instance, job: TextJob, cfg, *, cancel_token=None) -> TextResult: ...
    @abstractmethod
    def validate_spec(self, job: TextJob) -> None: ...
    @abstractmethod
    def model_identity(self, cfg) -> str: ...
    def attach_get_instance(...) / attach_boot_liveness_probe(...)   # mirrors UpscalerEngine
```

### 5.1 Registry and composition root

`core/registry.py` gains `register_text_engine` / `get_text_engine` / `text_engine_names`,
copied from the upscaler trio (`:280-330`) **including the duplicate-registration raise** —
not the overwrite semantics of `register_engine`. The single concrete import lands in
`_adapters.py` (`:24-66`), the only module permitted to import adapters;
`test_core_invariant` keeps `core/` clean.

### 5.2 The `transformers` engine

`src/kinoforge/text_engines/transformers/__init__.py`, a `PodHTTPClientMixin` subclass:

- `render_provision(cfg)` emits the fragment: `export KINOFORGE_TEXT_MODEL_ID=<base ref minus "hf:">`
  and `export KINOFORGE_TEXT_PORT=<port>` (default `"8000"`). No pip, no embeds — those
  are explicit in `engine.diffusers.pip` / `embed_files` like every other config, and the
  embed-closure test (§11) enforces the set. `ports=["8000"]`.
- `health` → `GET {base}/health`, parsed into `TextHealth`.
- `upload_image` → `self._upload_source(instance, path, media="image")`, returns the
  pod-side path (the `file://` prefix stripped; the server wants a path).
- `complete` → `submit_and_poll(label_prefix="text", base_url, endpoint="/text", payload=…)`
  (`engines/_pod_http.py:85`), then `TextResult(**result)`.
- `model_identity` → the base ref's final path component (`Qwen3-0.6B`); never raises.

The base URL comes from `instance.endpoints[port]`. Today `_base_url` hard-codes
`_DEFAULT_SERVER_PORT` (`_pod_http.py:276`); the text engine reads the port from
`cfg["text"]["port"]` (§3.1, default 8000). **This parameterised port, and the fragment that
exports it, are the two seams the hooks spec needs** to run the text server as a sidecar
beside a video server on one pod. They cost nothing here.

### 5.3 Composition into the diffusers provision

`DiffusersEngine.render_provision` gains a third composition block beside the upscaler
and interpolator ones (`engines/diffusers/__init__.py:1352-1393`): when `cfg["text"]` is a
dict, resolve `registry.get_text_engine(name)()`, call its `render_provision(cfg)`, and
append the fragment's lines. **Phase `"runtime"`, not `"build"`** — the two existing
blocks compose weight fetches, which are bakeable into a Modal image; an `export` baked
into an image layer is lost by the time the container runs. The RunPod path concatenates
both phases, so this is invisible there, and correct on Modal when a Modal text config
arrives (§13.2).

## 6. Pod-side server: `servers/text_server.py`

Copies the skeleton of `minimax_h3_server.py` (`:803-863`: module-level `app`, `ready`
event, `jobs` dict, `_q` queue, `_worker_loop`, the startup ordering that sets `ready`
last) and none of its body — no `_lora`, no `_av_io`, no numpy, no geometry validators.

### 6.1 Routes

| route | contract |
|---|---|
| `GET /health` | `{"ready", "model", "supported_modes": [...], "capabilities": ["text", "upload"], "torch": {...}}`. 200 even while loading, `ready: false` — `wait_for_ready` polls it. |
| `GET /util` | the five `UtilSnapshot` fields via `_util_stats.read_gpu_stats`; sync `def` so the blocking NVML read runs in the threadpool. **Mandatory**: CLAUDE.md's live-smoke rule polls it. |
| `PUT /upload` | the Wan server's contract (`wan_t2v_server.py:2722`): `Content-Type` in `{image/png, image/jpeg}` (415 otherwise — no `video/mp4` here), `X-Filename` sanitised to a basename in `[A-Za-z0-9._-]`, stream to a tempfile, `KINOFORGE_MAX_UPLOAD_MB` → 413 with cleanup, atomic `os.replace`, return `{"path", "size", "sha256"}`. Implemented in **`servers/_upload.py`** (§6.4). |
| `POST /text` | `{"prompt", "system"?, "images": [pod paths], "params": {...}}` → `{"job_id"}`. 503 while not ready. **400** when `images` is non-empty and the model has no image path (§4.2). 400 on a path outside the upload dir (same `relative_to` guard the H3 `/artifacts` route uses). |
| `GET /text/status/{job_id}` | `{"state": "queued"|"running"|"done"|"error", "result": {...}, "error": "..."}`. |

**The status schema is `submit_and_poll`'s (`state` / `result`), not `/generate`'s
(`status` / `filename`).** The repo has two polling contracts today; this server joins the
one its client uses and does not invent a third. The completion travels **inline** in
`result` — it is kilobytes, so there is no artifact file, no `/artifacts` route and no
post-walk materialisation from a pod URL.

### 6.2 Loading, and how the modes are derived

```
model_id = os.environ["KINOFORGE_TEXT_MODEL_ID"]            # no default; a missing export is a boot fault
processor = AutoProcessor.from_pretrained(model_id)
if processor has an image processor:
    model = AutoModelForImageTextToText.from_pretrained(model_id, dtype=bf16, device_map="cuda")
    supported_modes = ["t2t", "it2t"]
else:
    model = AutoModelForCausalLM.from_pretrained(model_id, dtype=bf16, device_map="cuda")
    supported_modes = ["t2t"]
```

The loader is injectable (a module-level callable the tests replace), so the derivation,
the 400 path and the chat assembly are unit-tested with a fake model and no weights.
`ready.set()` happens after the worker thread starts, as in H3.

### 6.3 One request → one chat

```
messages = []
if system: messages.append({"role": "system", "content": system})
messages.append({"role": "user", "content": [*({"type": "image", "image": p} for p in images),
                                             {"type": "text", "text": prompt}]})
inputs = processor.apply_chat_template(messages, add_generation_prompt=True,
                                       tokenize=True, return_dict=True, return_tensors="pt",
                                       **params.pop("chat_template_kwargs", {}))
out = model.generate(**inputs, **params)
```

Everything in `params` except `chat_template_kwargs` goes to `generate()` verbatim — the
same opaque pass-through contract `image.params` has (image design §8). `usage` is the
prompt and completion token counts from the tensors; `finish_reason` is `"length"` when
the completion hit `max_new_tokens`, else `"stop"`. A missing `max_new_tokens` gets the
server default **256**, stated in `/health` so a run is reproducible without reading the
server source.

### 6.4 `_upload.py`: new, shared by one server — deliberately

The handler in `wan_t2v_server.py:2700-2782` is extracted into `servers/_upload.py` as a
function the text server mounts, with the content-type set as a parameter. **The Wan
server keeps its inline copy in this spec.** Migrating it would move sixteen configs'
`embed_files`, their launch goldens and their payload baselines in a change whose only
witness is a Wan pod — a 25-minute boot to prove a refactor that changes no behaviour.
The copy is named here and in §13.5 so it is a decision, not drift. The existing Wan
upload tests (`tests/engines/diffusers/test_server_upload*.py`) are the regression net
when that migration happens; the same tests, pointed at the text server, are the
acceptance tests for `_upload.py` now.

### 6.5 What the server imports

stdlib, `torch`, `transformers`, `PIL`, `fastapi`, `uvicorn`, and the two embedded helpers.
Nothing from `kinoforge.*` outside the `servers/` package — U66 killed every RunPod
diffusers pod for three hours with one module-level `kinoforge.core` import that the
controller's 6348 green tests could not see. `test_pod_embed_closure.py` enforces the
embed set in both directions and `test_boot_import_closure` walks the transitive imports.

## 7. Stage and orchestrator

### 7.1 `pipeline/text.py::TextStage`

```python
@dataclass
class TextStage:
    engine: TextEngine
    instance: Instance | None
    cfg: dict[str, Any]
    store: ArtifactStore
    sink: OutputSink | None
    run_id: str
    cancel_token: CancelToken | None = None

    def run(self, state: PipelineState) -> PipelineState:
        request = state.request                                   # prompt, mode, assets
        health = self.engine.health(self.instance, self.cfg)
        if request.mode not in health.supported_modes:            # §4.2
            raise ValidationError(...)
        images = tuple(self.engine.upload_image(self.instance, _local_path(a.ref))
                       for a in request.assets)
        job = TextJob(prompt=request.prompt, system=self.cfg["text"].get("system"),
                      images=images, params=dict(self.cfg["text"].get("params", {})))
        self.engine.validate_spec(job)
        result = self.engine.complete(self.instance, job, self.cfg, cancel_token=self.cancel_token)
        artifact = _store_and_publish(result, ...)                # §8
        return replace(state, artifacts={**state.artifacts, "text": artifact})
```

Ordering is the contract: **health, then upload, then submit.** A test with a counting
fake asserts a mode mismatch leaves the upload and complete counters at zero.

### 7.2 Orchestrator changes — two branches

1. Stage assembly (`core/orchestrator.py:2918`, beside the `UpscaleStage` block): when
   `cfg.text is not None`, resolve `registry.get_text_engine(cfg.text.engine)()` and
   append `TextStage(engine, session.instance, cfg_dict, store, sink, run_id, cancel_token)`.
2. Return selection (`:3014-3019`): `elif skip_clip_stage and cfg.text is not None:
   artifact_key = "text"`.

Nothing else. The materialise blocks for `upscaled` and `interpolated` key on those
artifact names and do not fire; the stage has already published (§8), and the orchestrator
returns `state.artifacts["text"]` without re-publishing — the interpolate local-decimate
branch is the precedent for a stage that owns its own publish.

### 7.3 The CLI handler

`_cmd_text` mirrors `_cmd_upscale` after its refusals: `_resolve_input_as_artifact`
(`cli/_commands.py:1477`) for each `--image` with `media="image"`, wrapped as
`ConditioningAsset(kind="image", role=f"image_{i}", ref=...)`; `_build_sink`;
`_resolve_run_id(args, "text")`; `_resolve_attach_pod` or `_scan_warm_candidates`;
`_ephemeral_launch_row_reserve`; then

```python
orchestrator.generate(cfg, request=GenerationRequest(prompt, mode, assets),
                      store=..., sink=..., run_id=..., instance=..., cancel_token=...,
                      skip_clip_stage=True)
```

`request` is real, not the `GenerationRequest(prompt="", mode="upscale")` placeholder at
`:2790` — the stage reads it. On return the handler reads the text back from the stored
artifact and prints it to stdout.

## 8. Output: stdout, `.txt`, `.json`

**stdout carries the completion text and nothing else**, so `kinoforge text … | pbcopy`
and `$(kinoforge text …)` work. Run chatter (instance id, timings, filenames) goes to the
log on stderr, as every other command's does.

**Store:** `store.put_bytes(run_id, "response.txt", …)` and `(run_id, "response.json", …)`,
fixed names with the `# kinoforge:public-name` pragma the keyframe and image paths use.

**Sink:** two `publish` calls with the same `prompt`, `provider=cfg.text.engine`,
`model=engine.model_identity(cfg)`, `kind="text"`, differing only in `extension`:

```
output/20261004-153000_text_transformers_Qwen3-0.6B_Describe-the-image.txt
output/20261004-153000_text_transformers_Qwen3-0.6B_Describe-the-image.json
```

The `.json` holds: `prompt`, `system`, `mode`, `images` (local path, sha256, pod path),
`model` (the full ref), `params` as sent, `text`, `usage`, `finish_reason`, `elapsed_s`,
`run_id`, `kinoforge_version`, `instance_id`. It is the reproduction recipe
`successful-generations.md` asks for, in machine-readable form.

**`core/media.py` is not touched.** Its `Media = Literal["video", "image"]` is scoped to
the upscale path, and a third member would claim text is an upscalable kind. The
extension is passed explicitly, as `image_run.py:131` does.

The collision ladder (`outputs/local.py:138`) is per filename, so if a run in the same
clock second already published the same stem, the `.txt` and `.json` may receive different
`_2` suffixes. Accepted: the `.json` names its run id, which resolves any pairing doubt.

## 9. Warm reuse and the stage vocabulary

With `stages=("text",)` (§3.4), `_cfg_want_stages` (`cli/_commands.py:3657`) yields
`("text",)` and the `/health` gate (`:3690`) attaches only to a pod whose
`capabilities` include `"text"` **and** whose capability key — base model, engine,
precision — matches. Two text configs for different checkpoints never share a pod; the
same config twice does, which is the whole point of warm reuse for a 10-minute boot.

`tests/cli/test_shipped_cfg_want_stages_sweep.py::ADVERTISABLE_STAGES` is a
hand-transcribed set — by design, so the server and the test cannot silently agree on a
new term. It gains `"text"` in the same commit that teaches the server to advertise it.
`KNOWN_IMAGE_CFGS` there is untouched: text configs load a capability key and flow through
the sweep like every video config.

## 10. Example configs and models

Three configs, named per `docs/superpowers/specs/2026-07-12-config-filename-scheme-design.md`
(`<provider>-<engine>-<subject>-<qualifier>-<operation>`, dots as underscores):

| file | model | modes | role |
|---|---|---|---|
| `runpod-diffusers-qwen3-0_6b-t2t.yaml` | `hf:Qwen/Qwen3-0.6B` (0.75 B, 1.5 GB bf16, Apache 2.0) | `t2t` | smoke, **live-fired** |
| `runpod-diffusers-smolvlm-256m-it2t.yaml` | `hf:HuggingFaceTB/SmolVLM-256M-Instruct` (0.26 B, 0.5 GB, Apache 2.0, Idefics3) | `t2t`, `it2t` | smoke, **live-fired** |
| `runpod-diffusers-qwen3_8-27b-it2t.yaml` | `hf:Qwen/Qwen3.8-27B` (27.8 B, 55.6 GB bf16, Apache 2.0) | `t2t`, `it2t` | quality, **offline-validated only** |

Shared shape: the `runpod/pytorch:2.8.0` image (torch 2.8 pre-installed, so
`transformers>=5.10` installs without a torch pull — thirteen shipped configs already use
it); `cloud_type: secure` (U65: seventeen long-boot configs were being reclaimed by the
community pool mid-fetch); explicit NVIDIA accelerator allowlists (the AMD-offer HTTP 500);
`boot_timeout` sized to pip plus the weights download.

**Why these models.** The survey (`docs/superpowers/research/…`) found Qwen3.8-27B first
in its class on the Artificial Analysis index and the only single-card model above
Gemma-4-31B. For the smoke pair, Qwen3-0.6B has **no image processor**, so the §4
negative path is a real refusal, not a mock; SmolVLM-256M is the smallest credible VLM and
its Idefics3 architecture has been stable in `transformers` since 4.46, which matters
more for a smoke than quality. Qwen3.5-0.8B is the better tiny VLM and the documented
alternative, held back only because a months-old architecture needs a very recent
`transformers` and the smoke should fail on kinoforge, not on a library version.

**The quality config is not live-fired in this spec.** Its header says so. Replicate's
image engine set the precedent: shipping a config implies a proof, so the header must
state which proof is missing (a bf16 27 B load on an 80 GB card, ~56 GB download) and what
it would cost to close (one run, ~$1.50-2.50 on an A100-80GB). Its `params` set
`chat_template_kwargs.enable_thinking: false` and `max_new_tokens: 1024`.

Two prompt files under `examples/configs/prompts/`, read verbatim by the smokes and never
paraphrased (the standing rule for every live smoke): `text-smoke-t2t.txt` (a two-sentence
summarisation task over the `field-realistic.txt` shot description, so the t2t smoke
previews the prompt-enhancement hook) and `text-smoke-it2t.txt` ("Describe this image in
two sentences. Name the dominant colours.").

### 10.1 Sweeps the new configs fall under, automatically

Each is a reason the configs must be right, not a chore:

- `test_launch_payload_goldens.py` — each needs a committed golden (`tools/snapshot_launch_payloads.py`, run **after** `pre-commit --all-files`).
- `test_env_payload_ceiling.py` — a `_BASELINE_BYTES` row per RunPod diffusers config and the 101 000 B ceiling. The text server is new source embedded as base64 (~1.33 B on the wire per source byte); keeping it lean is a payload budget, not taste. Measure with `_rendered_env_bytes` before any pod is booked.
- `test_pod_embed_closure.py` — the embed set equals the import closure, both directions.
- `test_runpod_long_boot_needs_secure_pool.py`, `test_bakeable_steps_set_u_safe.py`, `test_diffusers_optional_cfg_defaults.py`, `test_shipped_configs_capabilities.py`, `test_examples.py`'s rglob loaders, `test_doctor_examples_live.py`.

`tests/test_examples.py::EXAMPLE_CONFIGS` is a hand list of nine names; the three text
configs are added to it so they get the AC1 load test rather than silently missing it.

## 11. Testing

Offline work rides a `FakeTextEngine` (registered under `fake`, counting calls to `health`,
`upload_image`, `complete`) and the server's injectable loader (§6.2) with a fake model
whose `generate` returns fixed ids.

### 11.1 The assertions that carry weight

| behaviour | assertion | bug it catches |
|---|---|---|
| pre-spend gate | provider `create_instance` never called; no ledger row; exit 2; message names the model and `it2t` | an implementation that books the pod and refuses after |
| pod-side gate ordering | fake counters: `health == 1`, `upload_image == 0`, `complete == 0` on mismatch | a stage that uploads first and gates after |
| server mode derivation | loader returns a processor with / without an image processor → `/health.supported_modes` is `[t2t, it2t]` / `[t2t]` | a server that echoes the config's declaration instead of deriving |
| server 400 | `POST /text` with `images` on a `[t2t]` server → 400 before the job is queued | a 400 raised from the worker thread, after the queue |
| chat assembly | messages captured by the fake processor: system turn present only when set; images precede the text in the user turn; `chat_template_kwargs` reach `apply_chat_template` and **not** `generate` | the H3-style "params leak into the wrong call" |
| upload contract | `_upload.py` via the text server passes the Wan upload tests re-pointed at it, plus a `video/mp4` body → 415 | a copy that drifted from the contract `_upload_source` cross-checks |
| cancellation | fake poll counts stop early when the token trips mid-poll | a loop that runs to completion and raises at the end |
| `--dry-run` | injected HTTP seam recorded zero calls; mode and image count printed | a dry run that probes a pod |
| validator | one case per §3.3 rule, each asserting the message names that key | a validator that refuses for the wrong reason |
| capability key | `stages == ("text",)`; a video cfg's key is byte-identical before and after | the U14/U19 derivation drift |
| want-stages sweep | `ADVERTISABLE_STAGES` contains `"text"` and `_cfg_want_stages(text_cfg) == ("text",)` | a pod structurally unattachable |
| output | `.txt` bytes equal `result.text`; `.json` round-trips and names `run_id`; stdout equals the text exactly, no trailing chatter | a handler that prints the log line to stdout |
| session globals | `text` in `_DISPATCH`, `_INTERRUPTIBLE_CMDS`, `_propagate_session_globals` node list | U27/U29 |
| guard the guard | the config-discovery helper found exactly three text configs | a sweep matching nothing passes everything |
| embed / boot closure | `text_server` imports resolve to exactly `{_util_stats, _upload}` within `servers/`; nothing from `kinoforge.core` | U66 |

Per the `test-design` skill each test states its behaviour and the concrete bug that would
fail it; the table is the subset where assertion *strength* is the risk.

### 11.2 Live fire

Two runs, both through the real CLI as subprocesses with `--no-reuse`, following
`tests/live/test_spandrel_image_upscale_smoke.py`: RED scaffold committed before any spend;
evidence (`stdout.txt`, `stderr.txt`, the `.txt` and `.json`) written to
`tests/live/evidence/<run date>-text-command/` **before** the assertions; the util probe
polled every 60-90 s during the run and the pod destroyed on three consecutive 0 % GPU
reads; `kinoforge list` afterwards asserting both `No running instances.` and `No
instances recorded in ledger.`.

1. `t2t` on Qwen3-0.6B with `text-smoke-t2t.txt`. Asserts: exit 0; stdout non-empty and
   equal to the `.txt`; `.json` `finish_reason` present and `usage.completion_tokens > 0`.
2. `it2t` on SmolVLM-256M with `text-smoke-it2t.txt` and the §35 `kinoforge image` PNG as
   `--image` (skip with the regeneration command when absent, as the spandrel smoke does).
   Asserts the same, plus `.json.images[0].sha256` equals the local file's.

The negative path — `--image` against the `t2t`-only config — is asserted **offline**
(§11.1 row 1); it must never need a pod to prove.

**Output QA is mandatory and is read, not measured.** CLAUDE.md's visual-QA rule exists
because dims and duration cannot see pixels; token counts cannot see nonsense. Each
completion is read for coherence and prompt adherence and the verdict recorded in the
`successful-generations.md` entry. Two new sections, not "See also" lines: a new command,
a new engine and two new modes are each a qualifying axis.

Expected spend: two boots of a 16 GB-class secure-pool card at ~$0.20-0.35/hr for ~5-8
minutes each — well under $0.50 total.

## 12. Docs

`docs/configuration.md` (the `text:` block, the validator rules, the mode derivation);
`docs/engines.md` (text engines as a fourth engine family; the `/health` stage term);
`README.md` quick usage; `successful-generations.md` preamble widened from "video
generation" to "generation" (its §35 and §36 already stretched the wording);
`PROGRESS.md` design path, plan path, checklist and next action per the durability rules.

## 13. Non-scope, and why each is separate

**13.1 The two pipeline hooks.** Prompt enhancement (a `Stage` before the clip stage that
rewrites the prompt through a text model) and frame QA (a `Stage` after upscale or
interpolate that extracts frames with `core/frames.py`, sends them to a vision model with
a rubric, and attaches the verdict). Both are `Stage`s that call a `TextEngine`; both need
to decide where the text model runs during a video run — a second pod, or a sidecar on the
video pod on another port. §5.2's parameterised port and §5.3's runtime-phase fragment
are the seams that decision needs, and nothing else in this spec moves for it. Separate
because the compute-topology decision, the verbatim-prompt baseline every cross-model
comparison relies on, and the QA rubric are each a design of their own.

**13.2 Modal and SkyPilot text configs.** Modal needs image-Python equal to the controller's
(3.13, `python:3.13-slim`) and its own live proof; nothing here blocks it, and §5.3's
phase choice is already Modal-correct.

**13.3 vLLM and the quantised tiers.** vLLM pins its own torch and adds a multi-GB install
at boot; its value is throughput and the AWQ/FP8 kernels the 24 and 48 GB Qwen3.8-27B
variants need. A second `TextEngine` implementation, when wanted.

**13.4 Video input, multi-turn, streaming, batch, logprobs.** Each is a surface change
with its own contract; none is needed to prove the seam.

**13.5 Migrating the Wan server onto `_upload.py`.** §6.4. Sixteen configs, their goldens
and baselines, and a Wan-pod witness.

## 14. Module-by-module summary

| file | change |
|---|---|
| `core/interfaces.py` | `TextJob`, `TextResult`, `TextHealth`, `TextEngine`; `MODE_ROLE_REQUIREMENTS` += `t2t`, `it2t` |
| `core/registry.py` | `register_text_engine` / `get_text_engine` / `text_engine_names` (duplicate-rejecting) |
| `core/config.py` | `TextConfig`; `Config.text`; §3.3 validator; `capability_key` stages += `"text"` |
| `core/orchestrator.py` | append `TextStage`; `artifact_key = "text"` |
| `pipeline/text.py` | **new** — `TextStage` |
| `text_engines/transformers/__init__.py` | **new** — `TransformersTextEngine` (client + fragment) |
| `engines/diffusers/__init__.py` | third composition block, runtime phase |
| `engines/diffusers/servers/text_server.py` | **new** — the pod server |
| `engines/diffusers/servers/_upload.py` | **new** — upload handler, extracted from the Wan server's shape |
| `_adapters.py` | one import |
| `cli/_main.py` | `text` parser; `_DISPATCH`; `_INTERRUPTIBLE_CMDS`; session-globals node list |
| `cli/_commands.py` | `_cmd_text` |
| `examples/configs/` | three configs; two prompt files |
| `tests/cli/test_shipped_cfg_want_stages_sweep.py` | `ADVERTISABLE_STAGES` += `"text"` |
| `tests/test_examples.py` | `EXAMPLE_CONFIGS` += three |
| `tests/providers/test_env_payload_ceiling.py` | `_BASELINE_BYTES` += three |
| `tests/live/test_text_command_smoke.py` | **new** — two smokes |
| docs | `configuration.md`, `engines.md`, `README.md`, `successful-generations.md` preamble, `PROGRESS.md` |

Four new modules, one new ABC, two orchestrator branches, one deliberate duplicate (§6.4).
