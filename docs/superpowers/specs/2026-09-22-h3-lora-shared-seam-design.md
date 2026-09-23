# MiniMax-H3 LoRA: the shared seam

**Status:** design approved 2026-09-22, not yet planned.
**Scope:** give MiniMax-H3 a working LoRA path, on a seam a third model can reuse.
**Non-scope:** the Wan server is not modified. See §10.

## 1. The question, and the answer

*Does the LoRA approach built for Wan 2.1 / Wan 2.2 work for MiniMax-H3?* No — but
the obstacle is entirely on our side. The model supports LoRA well.

**Upstream is ready.** `diffusers==0.40.0` ships `MiniMaxH3LoraLoaderMixin`
(`loaders/lora_pipeline.py:6743`), and `MiniMaxH3ModularPipeline` inherits it
(`modular_pipelines/minimax_h3/modular_pipeline.py:150`). The object
`minimax_h3_server._load` already builds therefore carries `load_lora_weights` and
`set_adapters` — the same calls the Wan path makes.

The offload hazard that looked fatal is not one. `ComponentsManager.enable_auto_cpu_offload`
installs a `CustomOffloadHook` (`modular_pipelines/components_manager.py:44`), not
accelerate's `CpuOffload` or `AlignDevicesHook`. diffusers'
`_func_optionally_disable_offloading` (`loaders/lora_base.py:442`) keys on exactly those
two classes, so its hook-strip-and-reapply branch stays dormant and never reaches the
`enable_model_cpu_offload()` that would `AttributeError` on a `ModularPipeline`.
`ModularPipeline` also defines `hf_device_map = None` and a `components` property, both
of which that helper reads.

**Three defects on our side, in order of severity:**

1. **`loras:` in an H3 config fails silently.** Nothing validates a LoRA stack against
   the server module. The stack feeds `CapabilityKey` and the matcher, and reaches a pod
   only through `POST /lora/set_stack` — which H3 does not serve. Cold boot is quieter
   still: `KINOFORGE_INITIAL_LORA_STACK_JSON` is read by `wan_t2v_server.py:1474` and
   written by nothing in the repo, so an H3 pod would generate without the LoRA and
   report success. This is the FlashVSR `LQ_proj_in` failure shape: a silent quality bug
   behind a green exit code.
2. **No LoRA surface on the H3 server.** `minimax_h3_server.py` serves `/health`,
   `/util`, `/generate`, `/status/{job_id}`, `/artifacts/{filename}` and nothing else. The
   subsystem lives inside `wan_t2v_server.py`, where roughly 1,200–1,600 of 2,621 lines
   touch it, shared with nothing.
3. **The routing vocabulary is Wan-shaped.** `LoraEntry.branch` is
   `Literal["high_noise","low_noise","auto"]` (`core/lora.py:138`) — Wan 2.2's MoE noise
   split. H3's second denoiser is a *workflow partition* (`transformer` for t2va/fl2va,
   `transformer_ref` for ref2va), reached with `load_into_transformer_ref=True`. diffusers
   warns the two partitions share module names, so a misrouted LoRA loads successfully and
   degrades output. Our `_detect_moe_arity` regex `^transformer(?:_\d+)?$`
   (`wan_t2v_server.py:613`) does not match `transformer_ref` at all.

**Two ecosystem facts that change what we build:**

- The turbo LoRA named in the H3 t2va design doc — `Comfy-Org/…minimax_h3_fl2v_turbo_8step…`,
  1.96 GB — **cannot be loaded by diffusers**: it is trained against the pruned checkpoint
  and fails with a size mismatch. The diffusers-compatible 8-step files are
  `lightx2v/Minimax-h3-Turbo` (records its training alpha in `__metadata__`) and
  `larryvrh/MiniMax-H3-Turbo-Lora` (alpha-less, loads at `alpha == rank`).
- DiffSynth-Studio H3 LoRAs carry fp32 factors that push the unfused LoRA path into fp32
  compute. diffusers' stated remedy is `.to(torch.bfloat16)` on the model after loading.
  The Wan path has no such step, so it is not inherited for free.

## 2. Decisions

| # | Decision | Rejected alternative |
|---|---|---|
| D1 | Build a shared seam (`servers/_lora.py`) and mount it in H3 only | A bespoke H3 loader — a second implementation is how we got a 2,621-line server |
| D2 | Per-model differences are **data** (`LoraProfile`), not subclasses | A `LoraAdapter` Protocol per model: more surface, invites drift |
| D3 | The stack reaches a pod over **HTTP after ready**, never via pod env | The dormant env-file path: refs are sensitive under vault mode, and RunPod's REST pod-detail echoes env cleartext |
| D4 | Capability truth = client registry **and** server `/health`, parity-tested | Server-only (a bad cfg costs a 45-min H200 boot); cfg-declared (a declaration can lie) |
| D5 | Reuse the existing `/lora/set_stack` async contract, minus eviction | A new `/lora/apply`: same job, divergent semantics, duplicate client code |
| D6 | `branch` generalises to `target`, `branch` kept as a deprecated alias | Widening the `branch` Literal per model; deferring until ref2va lands |
| D7 | A failed apply **fails the run** | Logging and generating anyway — the defect this design exists to remove |
| D8 | Wan is untouched this increment | Extracting from Wan now: entangled with the `/upscale` + `/interpolate` co-residency LRU |

## 3. Schema — `branch` becomes `target`

Add `target: str | None = None` to `LoraEntry` (`core/lora.py`) and `LoraTarget`
(`wan_t2v_server.py`). `VaultLoRA` subclasses `LoraEntry` and inherits it.

`target` names a routing target drawn from the **server profile's** vocabulary, not from a
global enum:

| Profile | Legal targets | Omitted `target` resolves to |
|---|---|---|
| Wan 2.1 (single denoiser) | — | the sole denoiser |
| Wan 2.2 (MoE) | `high_noise`, `low_noise` | ERROR, listing both |
| H3 `t2va` | `transformer` | `transformer` |
| H3 `ref2va` (future) | `transformer_ref` | `transformer_ref` |
| H3, both partitions loaded | `transformer`, `transformer_ref` | ERROR, listing both |

`target: null` therefore means "the profile's default target", reproducing today's `"auto"`
semantics — including the MoE rejection — without `"auto"` being a token every model must
understand. `load_into_transformer_ref` becomes a kwarg the profile derives from the target,
not a concept the schema knows.

`branch` is accepted, normalized (`h` / `l` aliases unchanged), and mapped onto `target` at
validation time with one WARNING naming the **count** of entries that used it, never the
refs (privacy invariant, spec §4 P3-Privacy-4). Setting both to disagreeing values is a hard
error, not a precedence rule. Removal is recorded in `docs/breaking-changes.md`. Both models
are `extra="forbid"`, so shipped configs and vaults keep loading unchanged.

`target` is a low-entropy enum: NON-SENSITIVE, same posture as `strength`. Only `ref` is
sensitive under vault mode.

`_adapters.build_set_stack_request` (`_adapters.py:341`) drops `branch` today. It is not a
live defect — the production path builds its own payload at
`engines/diffusers/__init__.py:718` and threads `branch` correctly — but it is a second,
lossy conversion of the same thing. **Delete it**, and move its tests onto the backend
payload builder.

## 4. The profile and the capability registry

### 4.1 Pod side — `LoraProfile`

A frozen dataclass in `servers/_lora.py`, constructed by each server **after** its pipeline
loads, because part of it is only knowable then:

| Field | Meaning | H3 `t2va` |
|---|---|---|
| `name` | echoed in `/health` and error bodies | `"minimax-h3-t2va"` |
| `targets` | legal targets for the pipeline actually loaded | `("transformer",)` |
| `default_target` | resolved when `target` is omitted | `"transformer"` |
| `load(pipe, path, adapter_name, target)` | the model-specific load call | `pipe.load_lora_weights(path, adapter_name=…, load_into_transformer_ref=(target == "transformer_ref"))` |
| `module_for(pipe, target)` | module whose `set_adapters` applies strengths | `pipe.transformer` |
| `after_load(pipe)` | post-load fixup | `.to(torch.bfloat16)` dtype restore |
| `explain_load_failure(exc)` | operator-facing hint, or `None` | pruned-checkpoint hint (§4.3) |

Targets are read off the loaded pipeline — `getattr(pipe, "transformer", None)` and
`getattr(pipe, "transformer_ref", None)`, the same way diffusers' own `patch_size` property
reads them — never from a constant.

**No pre-flight state-dict validator.** Predicting a size mismatch from safetensors headers
duplicates the model's dims in our code, and a wrong pre-check rejects valid LoRAs. Error
mapping keeps that knowledge where it lives. A pre-check remains a separable later addition
if the hint proves insufficient.

### 4.2 Client side — `core/lora_profiles.py`

A registry keyed by the dotted server module named in `engine.diffusers.server_cmd` — the
same signal the provision renderer keys off. Each entry answers what the client can know
statically: does this server serve LoRAs, and what is the target universe for this model
family.

**The split is deliberate and bounded.** The client cannot know which partitions a given pod
loaded. So config load catches *wrong family, no support, typo'd target*; the pod catches
*not loaded in this workflow*. Both refuse; neither proceeds silently.

The client check lives in `validation/checks/loras.py`, beside the existing `capabilities.py`
/ `models.py` / `upscale.py` checks, emitting `Severity.ERROR` gaps through the machinery
`kinoforge doctor` already surfaces.

`/health` gains:

```json
"lora": {"supported": true, "profile": "minimax-h3-t2va",
         "targets": ["transformer"], "default_target": "transformer"}
```

`tests/test_lora_profile_parity.py` imports each registry-named server module and asserts the
declared family vocabulary matches — the discipline that already keeps
`tests/test_lora_schema_parity.py` honest.

LoRA capability is never declared in cfg. The existing `capability:` block stays about modes.

### 4.3 The pruned-checkpoint hint

`explain_load_failure` turns a size mismatch into: *this looks like a LoRA trained against a
pruned checkpoint (the Comfy-Org `*_pruned_*` files; `joyfox/MiniMax-H3-Turbo` is one).
diffusers cannot load those — use `lightx2v/Minimax-h3-Turbo` or
`larryvrh/MiniMax-H3-Turbo-Lora`.* It surfaces as `lora_format_unsupported`, HTTP 400,
non-retryable.

## 5. The apply protocol

### 5.1 Wire contract

H3 mounts the **existing** endpoints, from a shared router:

- `GET /lora/inventory`
- `POST /lora/set_stack` → `202 {"job_id": …}`
- `GET /lora/set_stack/status/{job_id}`

`DiffusersBackend.set_lora_stack`, `_poll_set_stack`, the error mapping and the proxy-retry
wrapper are reused unchanged. Async is not optional: a 1.96 GB download through a provider
proxy is why that contract was made async (`2026-07-13-lora-set-stack-async-job-design.md`).

H3's version is **set_stack minus eviction**: same request, same job polling, same inventory
response including disk accounting. Deferred: the LRU eviction policy and matcher
integration — not the contract.

`servers/_lora.py` exports `build_lora_router(get_pipe, get_profile, loras_dir)` returning an
`APIRouter`. Accessors, not objects, so the router is honest about the pipe not existing
until startup completes, and so the later Wan migration does not care how that server holds
its pipeline.

### 5.2 Controller sequencing

`core/lora_apply.py`, called by the orchestrator once the backend is ready and before the
first `submit`, for cold pods and caller-supplied warm pods alike:

1. Resolve the stack via `resolve_active_lora_stack(cfg, vault, cli_loras=…)` — CLI > vault >
   cfg, unchanged.
2. Empty stack → no HTTP call; generate as today.
3. Non-empty → resolve download specs client-side (CivitAI credentials never reach the pod),
   POST, poll to terminal.
4. Success → submit `/generate`.
5. Failure → **fail the run.** Never generate bare. Teardown follows the usual `--no-reuse`
   path.

`try_warm_attach_with_swap` stays as-is and stays unwired; it is the matcher's swap planner,
and wiring it would pull the LRU forward.

### 5.3 Failure taxonomy

Reuses `core/errors.py`: `LoraSwapDownloadError` (pod unchanged, safe to retry),
`LoraSwapDiskFullError`, `LoraSwapVramOomError` (rolled back, pod healthy),
`LoraSwapPodUnreachableError`. One addition: `lora_format_unsupported` (§4.3).

**Rollback.** If loading entry *k* of *n* raises, the pod unloads everything and reports an
empty inventory, so the pod is never left half-applied for the controller to guess about.
This mirrors the Wan VRAM-OOM rollback and is the reason sequencing belongs in shared code.

**Replace.** A second `set_stack` unloads all adapters, then loads the new target set —
`unload_lora_weights` → `load_lora_weights` → `set_adapters`, as the Wan server does.
Downloaded files stay on disk.

## 6. The H3 mount

Startup gains one step between `_load()` and `ready.set()`: build the profile from the loaded
pipeline, then include the router. The pod is never ready with `/lora/set_stack` present and
no profile behind it. `/generate`, the job worker and the audio mux are untouched, and the
`KINOFORGE_H3_LOAD_STUB` seam keeps all of it testable without torch.

**New config** `examples/configs/modal-diffusers-minimax-h3-t2va-lora-turbo.yaml`: sibling of
the 640x352 long config, `num_inference_steps: 8`, plus a `loras:` block. H3 is
guidance-distilled and the turbo LoRA is a *step* distillation, so they compose — no guider,
no `guidance_scale`, one forward pass per step either way.

**The LoRA ref is a plan task, not a design assumption.** Our format is
`hf:<org>/<repo>:<file>` and `lightx2v/Minimax-h3-Turbo`'s filename is unknown here.
Inventing it is the U48 mistake. The plan pins it with a $0 HF API probe, which also confirms
the file is a single safetensors and reads its `__metadata__` alpha — the profile branch this
LoRA exists to prove.

## 7. Blast radius outside H3

`_render_embed_lines` (`engines/diffusers/__init__.py:138`) walks the whole `servers/`
package, so `_lora.py` ships in **every** diffusers config's boot script, Wan included.

- **Golden churn.** All ~20 launch-payload goldens plus `_golden_provision.json` move, with
  no behaviour change. Regeneration runs **after** `pre-commit --all-files`, never before, or
  the goldens bake against unformatted bytes and move twice.
- **RunPod env ceiling.** We are near 72 KB of the ~101 KB that produced the raw-HTTP-500
  hunt; a ~1,000-line module adds roughly 10 KB base64. Comfortable, but nothing asserts an
  absolute ceiling today — `tests/providers/test_runpod_provision_script.py` only asserts
  gzip beats plain base64 by 2x. This design adds the ceiling guard (§8, test 13) at a
  **90 KB total-env budget**: below the ~101 KB failure point with ~11 KB of headroom, and
  above today's ~72 KB by enough that the guard fires on a real regression rather than on
  ordinary growth.

If keeping H3-only bytes out of Wan pods ever matters, `embed_files` already supports
per-module embedding. Not done preemptively; the guard will say if it is needed.

## 8. Test plan

Each test names the bug it catches. Tests that could not name one were dropped.

**Schema**

1. `branch: "h"` normalizes to `target: "high_noise"` identically on `LoraEntry` and
   `LoraTarget` — catches the alias taught to one side only, yielding a cfg value the pod
   rejects.
2. `branch` and `target` both set and disagreeing raises — catches a precedence rule being
   implemented instead, silently ignoring a stale `branch` and misrouting a MoE stack.
3. An omitted target against a two-target profile is refused, naming both legal values —
   catches a default that picks the first target, which is diffusers' documented H3 hazard:
   loads fine, degrades output, reports nothing.

**Profile and capability**

4. A config whose `server_cmd` names a module absent from the registry, carrying `loras:`,
   produces a `Severity.ERROR` gap — catches a future server inheriting today's silent
   no-op by omission.
5. `target: "high_noise"` in an H3 config errors at load, naming H3's legal targets —
   catches a globally-keyed vocabulary check, where Wan tokens pass config load and fail
   only after a 45-minute H200 boot.
6. For every registry entry, the server module's declared vocabulary equals the client's —
   catches a one-sided edit; the dangerous direction is a client more permissive than the
   pod.

**Apply protocol** (load-stub seams, no torch)

7. A failed apply fails the run and `/generate` is never called — zero calls on the generate
   seam, plus the exact error type. If this goes green while generation proceeds, the
   feature is worse than not having it.
8. A failure loading entry *k* of *n* leaves inventory empty, not partial — catches a
   mid-loop break leaving adapters `0..k-1` live while the controller believes otherwise.
9. A size-mismatch exception surfaces as `lora_format_unsupported` 400 with the hint, and
   the client raises non-retryably — catches it falling into the generic 502 path and
   burning the retry budget on a file that can never load.
10. Applying [A] then [B] leaves exactly [B] active with both files on disk — catches a
    skipped unload, where A and B blend silently and the inventory still reads [B].
11. An empty stack makes no HTTP call — catches a `set_stack` POST on every run, which
    against a no-LoRA server is a 404 breaking runs that work today.

**Mount and payload**

12. `/health` reports one target under a stub holding only `transformer`, both under a stub
    holding both — catches a profile built from a constant rather than the loaded pipeline.
13. Every shipped RunPod diffusers config renders a total env payload under **90 KB** —
    catches the next embed addition crossing ~101 KB, whose only symptom is a raw HTTP 500
    with no GraphQL error body.

## 9. Live proof

One cold H200, two applies, `--no-reuse`. `pixi run preflight` first; the scaffold committed
RED **before** any spend; util polled every 60–90 s on `gpuUtilPercent` / `cpuPercent` (0%
GPU for three consecutive probes → pull the boot log, destroy, fail fast); teardown verified
by `kinoforge list` from a fresh process **after** the orchestrator exits, never from a
mid-run log line.

- **Run 1** — turbo LoRA, 8 steps, 640x352 / 124 frames.
- **Run 2** — second apply, style LoRA (`DiffSynth-Studio/MiniMax-H3-LoRA-LineartAnime`),
  normal step count, same geometry.

Frame QA on both, per the mandatory rule: `ffmpeg_frames_by_count`, contact sheets read for
artifacts and coherence, run 1 compared against the 50-step no-LoRA clip from 2026-09-18.

**The engagement control.** Run 2's verdict is binary — lineart or not. Run 1's is a contrast
argument: 8 steps *without* a step-distillation LoRA should be visibly incoherent. If run 1
looks merely mediocre, one more 8-step render with an empty stack (~$0.10, pod already warm)
settles it rather than leaving a maybe in the log.

Both runs get a `successful-generations.md` entry — new capability axis: first LoRA on H3,
first LoRA on a t2va model. Budget ~$1.0–1.5.

**Modal's LoRA dir persists.** `pod_path_env` puts `KINOFORGE_LORAS_DIR` at
`<volume>/loras`, and Modal's mount is `/cache/hf`, so downloads land on the shared Volume
and a repeat run skips the fetch.

## 10. Known debt this increment creates

Both belong in `PROGRESS.md`, not in a comment.

1. **Two LoRA implementations coexist.** Wan keeps its own until a separately planned
   migration whose whole job is disentangling the registry from the `/upscale` +
   `/interpolate` co-residency LRU.
2. **No eviction on H3.** `<volume>/loras` only grows — the turbo and style LoRAs are ~3 GB
   on a Volume already holding a 144 GiB fetch. Acceptable now; the LRU increment pays it
   off.

Unchanged and still true: `KINOFORGE_INITIAL_LORA_STACK_JSON` remains dormant on the Wan
side, so cold-boot `cfg.loras` is still a no-op there. This design does not fix it; it makes
the H3 path structurally incapable of the same failure.

## 11. References

- `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py` — the server being extended
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:538-1340` — the LoRA subsystem
  being generalised from
- `src/kinoforge/core/lora.py`, `src/kinoforge/core/vault.py:45` — the schema
- `src/kinoforge/engines/diffusers/__init__.py:658-930` — the client contract
- `docs/superpowers/specs/2026-09-17-minimax-h3-t2va-design.md` — H3 engine route and memory
  math
- `docs/superpowers/specs/2026-07-13-lora-set-stack-async-job-design.md` — why apply is async
- `docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md` — the
  `branch` vocabulary being generalised
- diffusers v0.40.0: `loaders/lora_pipeline.py:6743`, `loaders/lora_base.py:442`,
  `modular_pipelines/minimax_h3/modular_pipeline.py:150`,
  `modular_pipelines/components_manager.py:44`
