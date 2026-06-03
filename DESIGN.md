# kinoforge — design

Vendor-agnostic video-generation provisioning & orchestration. Foundation layer of a future
long-form pipeline. Three independent swappable axes — **compute**, **model source**, **generation
engine** — each a registry-discovered plugin the core never hard-codes.

Source of truth for *what*: `SPEC.md`. Source of truth for *how* (process/durability):
`CLAUDE.md` + Superpowers. This doc is the validated design synthesizing both plus the resolved
open questions. Recovery index: `PROGRESS.md`.

## 1. Resolved open questions (locked)

| Topic | Decision |
|---|---|
| **Submit/result vs Pool/Future** | `GenerationBackend.submit(job)->job_id` dispatches; `result(job_id)` blocks/polls to completion. `BackendPool` owns Futures; `SequentialPool` runs jobs inline and returns an already-resolved Future. A synchronous in-process backend (Diffusers) and an async remote one (hosted) satisfy the same contract with no special-casing. Concurrency lives only in the (future) pool. |
| **`models:` per engine** | Refs are **always parsed for identity** (the base ref feeds the `CapabilityKey`). The core provisioner **downloads only when the engine declares it needs local weights** (`GenerationEngine.requires_local_weights`). A hosted engine derives its key-base from its own `hosted.model` field; its `models:` block, if present, is validation-only and never fetched. |
| **`params` vs `spec`** | `params` = engine-neutral knobs every engine honors identically (fps, resolution, steps, seed, cfg/guidance, num_frames). `spec` = engine-only payload (ComfyUI graph template + node overrides; Diffusers pipeline class/scheduler; hosted model id + extra body). Rule: shared meaning → `params`; one-engine-only → `spec`. `GenerationEngine.validate_spec` guards `spec` before dispatch. |
| **Profile cache location** | Profiles persisted as JSON keyed by `CapabilityKey.derive()` **through the `ArtifactStore`**, local-filesystem default (`.kinoforge/profiles/`), path config-overridable. A shared store (S3/GCS/network volume) is a drop-in later via the same registry pattern. Single-flight discovery uses an **in-process lock** now; cross-process coordination deferred. |
| **Serverless runaway caps** | Proactive: `max_workers` (autoscale ceiling), per-request deadline (effective-deadline math), `max_in_flight` concurrent requests. `budget` remains the reactive backstop. |
| **ArtifactStore GC** | Run-scoped `run_id` namespaces + explicit `kinoforge gc [--run ID] [--older-than DUR]`. No background daemon. |
| **Conditioning role vocab** | `init_image` (i2v), `first_frame` + `last_frame` (flf2v), `drive_audio`, `source_video`. `kind` is an open enum (image/audio/video/…). `MODE_ROLE_REQUIREMENTS` maps mode → required roles in ONE place. Audio/video roles exist as names but are only consumed by an engine that declares the `kind`. |
| **Under-use warning** | Undeclared strategy flags → `False` + a `WARNING` naming the `CapabilityKey` and stating a declaration would unlock native extension / joint audio. **Declared-only — no inference** from probed modes (an inferred flag could be wrong and `verify` cannot catch it; engine declaration stays the single source of truth, conservative and never wrong). |

## 2. Architecture — three axes, a core that imports neither vendor nor engine

Core depends only on `core/interfaces.py`. Adapters self-register into `core/registry.py`
(explicit `register()` calls; entry-point discovery is a later option). Core resolves
implementations by **name** (providers, engines) or **scheme** (sources) only. Core must never
`import providers.runpod` / `import engines.comfyui` / `import skypilot`. Reviewer checks this
invariant.

### Repo layout
```
src/kinoforge/
  core/
    interfaces.py     all ABCs / dataclasses the spec defines (the only thing core depends on)
    registry.py       name+scheme -> implementation; adapters self-register
    config.py         pydantic v2 models, YAML load, cross-field validation
    credentials.py    CredentialProvider (env-backed default)
    downloader.py     parallel, resumable, checksum-verifying; stdlib backend, optional aria2c
    profiles.py       CapabilityKey, ModelProfile, ModelProfileProvider (self-populating cache)
    strategy.py       long-video strategy decision point (pure function)
    pool.py           BackendPool ABC + SequentialPool
    orchestrator.py   deploy flow
    provisioner.py    on-instance flow (shared steps + delegate to engine)
    lifecycle.py      cost-safety: timers, effective-deadline, sweeper, ledger, teardown, budget
    logging.py        stdlib structured logging
    errors.py         typed errors (ProfileNotCached, ConfigError, AuthError, CapacityError, ...)
  providers/   runpod/ (pod+serverless), local/, skypilot/
  sources/     civitai/, huggingface/, http/
  engines/     comfyui/ (+ node installer), diffusers/, hosted/, fake/
  pipeline/    stage.py (Stage protocol) + generate_clip.py (the one concrete stage)
  stores/      base + local/  (s3/, gcs/ later)
  cli.py       deploy | provision | generate | list | status | stop | destroy | reap | gc
  __main__.py  python -m kinoforge
examples/configs/   wan.yaml, diffusers.yaml, hosted.yaml, local-fake.yaml
hooks/
tests/
.github/workflows/ci.yml
```

## 3. Interfaces (from SPEC.md §"Architecture", with reconciliations)

Implement exactly the ABCs/dataclasses in SPEC.md lines 216–407. Reconciliations layered on top:

- **`GenerationBackend`**: `submit -> job_id`, `result(job_id) -> Artifact` (blocks/polls). Sync
  backends compute in `submit` and return immediately on `result`; async backends poll a remote.
- **`BackendPool`**: `submit(job) -> Future[Artifact]`, `map(jobs) -> list[Artifact]`, `add/remove`.
  `SequentialPool` runs each job inline through its backend(s), returns a resolved Future.
- **`GenerationEngine`**: add `requires_local_weights: bool` (drives whether the provisioner
  downloads `models:`). Keep `requires_compute`, `provision`, `backend`, `profile_for`,
  `declared_flags`, `validate_spec`.
- **`GenerationJob`**: `spec` (engine-interpreted) + engine-neutral `params` + ordered `segments`
  (len 1 = single-clip/stitching, N = native-extension stream). `Segment.params` override
  `GenerationJob.params` (segment-wins merge).
- **`CapabilityKey`**: `base_model`, ordered `loras` tuple, `engine`, `precision`;
  `derive()` = stable order-sensitive hash over all fields. VAE excluded (decode-only).
- **`ModelProfileProvider`**: `resolve(key)` plan-time cache read (raises `ProfileNotCached` on
  miss, no backend, no loaded model); `discover(key, engine, backend)` single-flight probe via
  `backend.inspect_capabilities()` + merge `engine.declared_flags(key)` → persist → return;
  `verify(profile, backend)` re-probes probeable fields only, raises on drift.
- **`MODE_ROLE_REQUIREMENTS`**: `{"t2v": set(), "i2v": {"init_image"}, "flf2v": {"first_frame","last_frame"}}`.

`profile_for(key)` on the engine **delegates to the one registered `ModelProfileProvider.resolve`**
— single source of truth, no drift. Three accessors kept distinct & documented: `resolve`/
`profile_for` = plan-time cache read; `GenerationBackend.capabilities()` = in-force profile;
`inspect_capabilities()` = raw live probe (only this touches the running model).

## 4. Data flows

**deploy (control plane):** load+validate config → derive `CapabilityKey` → if
`engine.requires_compute`: `find_offers(reqs)` (exclude < `min_vram_gb`, < `min_cuda`, pod offers >
`max_cost_rate`; keep serverless on cost; preserve `gpu_preference` order) → `create_instance(spec
with guardrails + injected least-privilege self-termination cred)` → poll ready → report
`endpoints`. Hosted (`requires_compute=False`): skip compute entirely. `--dry-run` prints the
vendor/engine-neutral plan, makes zero network calls.

**provision (data plane):** core orchestrates shared steps — parallel resumable checksum downloads
(only if `engine.requires_local_weights`), run optional `post_provision` hook — then delegates
engine-specific setup to `engine.provision(instance, cfg)`. ComfyUI: install pinned ComfyUI +
custom nodes (git + requirements/install.py) + route files to subdirs + launch with flags.
Diffusers: pip deps + weights cache + headless job server. Hosted: validate creds/endpoint only.
Persist heavy assets to mounted volume when available.

**capability discovery — guaranteed ordering (explicit in code):**
1. `resolve(key)` cache read → hit: continue; miss: `discover` ONCE under single-flight lock →
   provision smallest viable backend (cheapest mode: serverless/scale-to-zero where offered) →
   `inspect_capabilities()` (read config/metadata; at most a trivial 1-step/1-frame pass; never a
   full clip) → merge `engine.declared_flags(key)` → persist under key → tear probe backend down.
2. **validate `GenerationRequest`** vs now-known profile: `mode ∈ supported_modes`; role-authoritative
   asset check from `MODE_ROLE_REQUIREMENTS[mode]` (each required role present exactly once with
   right `kind`; missing/duplicated = hard error). Lone image in a single-asset mode may default to
   `init_image`; multi-asset modes require explicit roles.
3. splitter consumes `max_segment_seconds` and chops the prompt. **Deferred** — stub returns one
   segment (request assets copied in).
4. provision the generation backend.
5. `verify(profile, backend)` re-runs `inspect_capabilities()` and compares probeable fields. On
   contradiction: **raise, abort the run, and tear down the compute** (destroy pod / stop
   serverless worker via `ComputeProvider`) so a misconfigured instance can't keep billing or
   generate against a wrong split.

**generate (single-clip happy path):** `GenerateClipStage` takes a `GenerationRequest` → builds
`GenerationJob` → **strategy decision point** packages segments by flags → submit via
`GenerationBackend` (through `BackendPool`) → `result` → `Artifact` written to `ArtifactStore`.

**strategy decision point** (`core/strategy.py`): pure, testable
`decide(profile, segments, params, spec) -> list[GenerationJob]`. `supports_native_extension` True →
ONE job carrying all N segments. False → N single-segment jobs (continuity/stitching fallback
**stubbed** behind it). `supports_joint_audio` chooses joint vs separate audio stage (separate
stage **stubbed**). Both flag branches tested. No premature pipeline structure leaks in.

## 5. Cost-safety (layers 1–7)

Separate invariant (universal, non-negotiable: bounded lifetime, idle reap, hung-job abort,
confirmed teardown, no orphans) from mechanism (provider-specific). Three timers, each one concern:
**idle window** (warm reuse) / per-clip **`job_timeout`** / **`max_lifetime`** graceful drain.
`effective_deadline ≈ segments × job_timeout + time_buffer`. Worst-case wall-clock =
`max_lifetime + longest-in-flight effective_deadline`.

- **RunPodProvider (pod):** in-pod self-terminator installed at provision — (a) `max_lifetime`
  graceful drain, (b) local enforcement of the job's effective deadline injected at dispatch, (c)
  heartbeat dead-man's switch. Liveness = in-flight job under its deadline OR `heartbeat()` within
  ~2× idle window. Dedicated **least-privilege terminate-only** credential (ideally pod-scoped via
  `RUNPOD_POD_ID`), injected at `create_instance` as an env secret via `CredentialProvider`, never
  baked into image/config, lifetime-bound to the pod, rotatable. **Serverless mode** exempt from
  in-pod timers (scales to zero) but gets proactive caps (`max_workers`, per-request deadline,
  `max_in_flight`).
- **SkyPilotProvider:** satisfies the invariant via SkyPilot native autostop; maps `idle_timeout` →
  autostop; documents that the exact custom timer model is not replicated (cost of multi-cloud
  reach). `import skypilot` lives only here.
- **LocalProvider:** in-process timer simulation with an **injectable clock** so cost-safety is
  fully testable offline.
- **Universal (all providers):** instance **tags** + persistent **ledger** of everything launched;
  independent **sweeper** (`reap`) that is **provider-aware** (reconcile through `sky status` for
  SkyPilot, not the raw cloud API); **confirmed teardown** (`destroy_instance` polls until gone,
  retries + alerts loudly — fire-and-forget forbidden); **budget ceiling**; CLI surfaces running
  instances + age + estimated spend on every invocation and refuses a duplicate pod for the same job.

## 6. Config model

Pydantic v2, loaded from YAML, secrets excluded (safe to commit). Sub-blocks read by `engine.kind`
/ `compute.provider`. The orchestrator derives `CapabilityKey` from config: base ref + ordered LoRA
stack (`models[].kind`) + `engine.kind` + `engine.precision`. `models[].kind` (base|lora|vae) drives
key IDENTITY; `models[].target` drives FILE PLACEMENT.

**Validation rejects:** `idle_timeout >= max_lifetime`; `job_timeout > max_lifetime`; inconsistent
`kind`/`target` pairings; `compute:` present when `engine.kind == hosted`; unknown engine/provider/
scheme names (clear error). **Defaults:** `min_vram_gb=48`, `min_cuda="12.8"`,
`max_usd_per_hr=2.20`, `disk_gb=100`, `idle_timeout=2h`, `job_timeout=30m`,
`time_buffer=30m`, `max_lifetime=5h`; `budget` required. Durations parse `2h`/`30m`/`90s`.

## 7. Testing (offline — hard constraint)

No test may require real cloud creds, network, GPUs, or model weights. Fakes: `LocalProvider` +
`FakeProvider` (synthetic offers for `find_offers`), in-memory `FakeSource`, `FakeEngine`
(deterministic artifacts, declarable flags, fake `inspect_capabilities`), injectable clock for
lifecycle. Red-first per CLAUDE.md + `test-design` skill (state behavior-under-test + a concrete
failing bug per test; no weak assertions, no implementation mirroring, no over-mocking).

**Behaviors covered (red-first):** registry routing; config validation (incl. nonsensical combos);
scheme dispatch; downloader resume + checksum; provider lifecycle; all cost-safety items a–i; engine
selection; discovery (miss self-heal, hit no-compute, single-flight, key distinctness, fail-hard on
contradiction + teardown invoked); `find_offers` filtering + preference order + defaults; mode +
role-authoritative validation; engine `validate_spec`; strategy both flag branches; segment-stream
packaging (native vs non-native); pool submit/map contract + pool-swap; end-to-end Local+Fake
producing a clip Artifact; `--dry-run` no-network.

## 8. Deferred seams (interface + one real path built; layers NOT built)

Marked explicitly in code (`# DEFERRED:`) and here: prompt splitter (stub → 1 segment),
continuity/stitching fallback, audio-sync stage, concurrent/distributed pool, image/keyframe
upstream `Stage`, S3/GCS artifact stores, cross-process discovery lock. YAGNI reconciliation: build
each interface + exactly one real path now; do not build the named future layers.

## 9. Dependencies (stdlib-first, each justified)

- **pydantic** v2 — typed config validation (spec-suggested). conda-forge.
- **PyYAML** — YAML config load. conda-forge.
- **skypilot** — optional extra, lazy-imported only in `providers/skypilot/`.
- Downloader — **stdlib** `urllib` threaded ranged-GET; optional external `aria2c` binary if present
  (not a Python dep).
- HF / CivitAI / RunPod adapters — **stdlib** `urllib` against their HTTP APIs; avoid
  `huggingface_hub` / `requests` to keep deps minimal (revisit if HF resolution proves brittle).
- Logging — **stdlib** `logging` with a structured formatter; no `structlog`.
- Dev (already in pixi): pytest, pytest-cov, mypy, ruff, pre-commit.

## 10. Build order (phases → implementation plan)

1. interfaces + registry + config model + tests
2. downloader + HTTP source + tests
3. `GenerationEngine` iface + `FakeEngine` + provisioner + `LocalProvider` → e2e vs fake
4. profiles (`CapabilityKey` + cache + provider + discovery) + strategy decision point +
   pool/`SequentialPool` + `GenerateClipStage` + local `ArtifactStore`
5. cost-safety (timers, effective-deadline, sweeper, ledger, confirmed teardown, budget) vs
   `LocalProvider` + injectable clock
6. CivitAI + HuggingFace sources
7. ComfyUI engine (+ node installer) + RunPodProvider (pod + serverless)
8. DiffusersEngine + HostedAPIEngine (no-compute path) + SkyPilotProvider
9. CLI + examples/configs + README + CI (lint + types + tests on Linux/macOS/Windows)

Acceptance criteria = SPEC.md §"Definition of done" (each written as a failing test first).
