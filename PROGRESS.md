# PROGRESS — kinoforge

Recovery index. A fresh/resumed session reads THIS first (see `CLAUDE.md` → Session resume
protocol), then the design + plan it points to, then `git log --oneline -20`, then resumes from the
first unchecked task without redoing committed work.

## Pointers
- **Spec (the *what*):** `SPEC.md`
- **Design (validated):** `DESIGN.md`
- **Implementation plan:** `docs/superpowers/plans/2026-05-29-kinoforge.md`
- **Native task snapshot:** `docs/superpowers/plans/2026-05-29-kinoforge.md.tasks.json` (28 tasks, IDs 1–28, dependencies set)

## Phase
ALL 28 tasks complete. All 9 phases complete.

## Task checklist (high-level; plan refines into 28 bite-sized tasks)
- [x] Read SPEC.md, explore project context
- [x] Resolve open design questions (8 decisions locked — see DESIGN.md §1)
- [x] Write + commit DESIGN.md
- [x] Design review gate — approved
- [x] Write + commit implementation plan + native tasks + tasks.json
- [x] Phase 1: interfaces + registry + config model + tests (Tasks 1–4)
  - [x] Task 1: Core interfaces, errors, structured logging (`src/kinoforge/core/{__init__,errors,interfaces,logging}.py`, `tests/core/test_interfaces.py`) — commit e636df4
  - [x] Task 2: Adapter registry (`src/kinoforge/core/registry.py`, `tests/core/test_registry.py`) — commit f33ec13. API: register_provider/engine/source + get_provider/engine/source_for_ref via handles(). Sources dispatch by handles() not scheme equality; re-registration overwrites. pyproject.toml: added ignore_errors=true to tests.* mypy override to allow duck-typed fakes.
  - [x] Task 3: Env-backed credential provider (`src/kinoforge/core/credentials.py`, `tests/core/test_credentials.py`) — commit 85699ee. `EnvCredentialProvider.get(key)` reads from `os.environ`; returns `None` when unset. Subclasses `CredentialProvider` ABC.
  - [x] Task 4: Config model (`src/kinoforge/core/config.py`, `tests/core/test_config.py`) — commit 36e7e1a. `load_config()`/`parse_duration()`; pydantic v2 `Config` with `LifecycleConfig`, `EngineConfig`, `ModelEntry`, `ComputeConfig`, `RequirementsConfig`; cross-field validators; `capability_key()`, `lifecycle()`, `hardware_requirements()`. types-pyyaml added for mypy stubs. 11/11 AC tests pass.
- [x] Phase 2: Tasks 5–7 complete.
  - [x] Task 5: `filter_offers` pure helper (`src/kinoforge/core/offers.py`, `tests/core/test_offers.py`) — commit 57e04ca. Semantic CUDA compare via `_cuda_tuple()`; pod-only cost filter; stable `gpu_preference` sort. 6/6 AC tests pass.
  - [x] Task 6: Downloader (`src/kinoforge/core/downloader.py`, `tests/core/test_downloader.py`, `tests/conftest.py`) — commit 566d9d9. stdlib ThreadPool downloader: skip (sha256 or filename), resume via Range header, sha256 verify, corrupt-.part detect-and-raise, concurrent download_all. Range-aware loopback HTTP fixture in conftest.py. 8/8 tests pass. Corrupt-.part strategy: append + sha verify; mismatch → delete .part + raise; next call retries from scratch.
  - [x] Task 7: HTTPSource (`src/kinoforge/sources/__init__.py`, `src/kinoforge/sources/http/__init__.py`, `tests/sources/test_http.py`) — commit 37db66f. `HTTPSource.handles()` dispatches http/https only; `resolve()` strips query strings; self-registers on import. 5/5 AC tests pass.
- [x] Phase 3: Tasks 8–10 complete.
  - [x] Task 8: FakeEngine + FakeBackend (`src/kinoforge/engines/__init__.py`, `src/kinoforge/engines/fake/__init__.py`, `tests/engines/test_fake.py`) — commit dfdb9cf. Deterministic GPU-free engine/backend: sha256-derived `Artifact.filename`, injectable probe profile, `declared_flags_map`, `required_spec_keys`-gated `validate_spec`, `profile_for` deferred to Task 12, self-registers under `"fake"` on import. 17/17 tests pass.
  - [x] Task 9: LocalProvider + injectable clock (`src/kinoforge/core/clock.py`, `src/kinoforge/providers/__init__.py`, `src/kinoforge/providers/local/__init__.py`, `tests/core/test_clock.py`, `tests/providers/__init__.py`, `tests/providers/test_local.py`) — commit 5c8bbbb. Clock protocol (runtime_checkable) + RealClock + FakeClock(start, advance, ValueError on negative); LocalProvider(ComputeProvider) with synthetic offers (2 LOCAL offers), filter_offers delegation, full lifecycle (create/get/list/stop/destroy/heartbeat), idempotent destroy, last_heartbeat accessor, endpoints returning local://id, self-registration under "local". 18/18 tests pass.
  - [x] Task 10: Provisioner (`src/kinoforge/core/provisioner.py`, `tests/core/test_provisioner.py`) — commit fb53c46. `provision()` function with `_ProvisionConfig`/`_ModelEntryLike` structural Protocols; walks model entries, resolves via registry, merges sha256+target onto artifacts with dataclasses.replace; calls downloader only when `requires_local_weights=True`; runs `post_provision_hook(instance)` before delegating `engine.provision()` last. 7/7 tests pass (5 ACs fully covered).
- [x] Phase 3 (remaining): provisioner + e2e vs fake
- [x] Phase 4: profiles + strategy decision point + pool/SequentialPool + GenerateClipStage + local ArtifactStore
  - [x] Task 11: ArtifactStore ABC + LocalArtifactStore + store registry (`src/kinoforge/stores/base.py`, `src/kinoforge/stores/local.py`, `src/kinoforge/core/registry.py`, `tests/stores/test_local.py`) — commit 55e8668. `put_bytes`/`get_bytes`/`put_json`/`get_json`/`list`/`delete`; run_id-namespaced layout `<root>/<run_id>/<name>`; resolved absolute URIs; `list()` returns relative names, empty list for unknown run_ids; `delete()` raises `FileNotFoundError`; self-registers under `"local"`; `register_store`/`get_store` in registry raising `UnknownAdapter`. 22/22 tests pass.
  - [x] Task 12: ModelProfileProvider — JsonProfileCache (`src/kinoforge/core/profiles.py`, `tests/core/test_profiles.py`) — commit 9ad354f. `resolve/discover/verify/resolve_or_discover`; per-key single-flight via threading.Event + inflight dict; JSON serialisation (set→sorted-list, tuple→list round-trips); URI index populated by `_persist`, fallback to `_reconstruct_uri` via `LocalArtifactStore._path` for cross-restart reads; `declared_flags` merged onto probe (only the two flag fields); WARNING emitted when both flags absent; `verify` compares only probeable fields (max_frames, fps, max_resolution, supported_modes). 13/13 tests pass.
  - [x] Task 13: Request validation (`src/kinoforge/core/validation.py`, `tests/core/test_validation.py`) — commit 8c352e9. Pure `validate_request(profile, request, *, accepted_kinds)`: mode gate, kind gate, single-asset-mode lone-image default (i2v only; flf2v requires explicit roles), role contract (required role present exactly once with kind=="image"). Returns new `GenerationRequest` via `dataclasses.replace`; never mutates input. 9/9 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 14: Strategy decision point (`src/kinoforge/core/strategy.py`, `tests/core/test_strategy.py`) — commit 4c2fe8e. Pure `decide(profile, segments, params, spec) -> list[GenerationJob]`: native branch → 1 job with all N segments; fallback branch → N single-segment jobs; segment-wins merge on Segment.params; job-level params is unchanged base; `spec["_audio_mode"]` set from `supports_joint_audio`. 18/18 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 15: SequentialPool + Stage re-export + GenerateClipStage (`src/kinoforge/core/pool.py`, `src/kinoforge/pipeline/__init__.py`, `src/kinoforge/pipeline/stage.py`, `src/kinoforge/pipeline/generate_clip.py`, `tests/core/test_pool.py`, `tests/pipeline/__init__.py`, `tests/pipeline/test_generate_clip.py`) — commit 4088b19. `SequentialPool.submit` wraps backend.submit+result in a pre-resolved Future; `map` preserves input order; `_ListPool` pool-swap AC verified; `add` increments `_backends` list; `Stage` Protocol re-exported from pipeline layer; `GenerateClipStage.run(request, *, segments_override)` validates → decide → pool.map → store.put_bytes; deterministic bytes from `filename+meta`; `CountingBackend` tests branching at N=3 segments (native=1 job, fallback=3 jobs). 11/11 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 16: Orchestrator (`src/kinoforge/core/orchestrator.py`, `tests/core/test_orchestrator.py`) — commit 0f3d0f6. `deploy()`: hosted path (requires_compute=False) skips provider; dry-run prints vendor/engine-neutral plan without calling create_instance; live path polls until ready. `generate()`: guaranteed ordering — discover on cache miss (verify skipped on fresh profile, trivially consistent); verify on cache hit with fail-hard teardown (destroy_instance called before re-raising CapabilityMismatch); 1-segment splitter stub (DEFERRED). Key design decision: verify is skipped when _just_discovered=True to avoid double inspect_capabilities (AC4 requires exactly 1 call on first generate; AC5 requires verify triggers on second generate/cache-hit). 12/12 AC tests pass; mypy + ruff + pre-commit clean.
- [x] Phase 5: cost-safety complete (Tasks 17–18)
  - [x] Task 17: LifecycleManager + effective_deadline + warm_reuse_or_create (`src/kinoforge/core/lifecycle.py`, `tests/core/test_lifecycle.py`) — commit 353eacd. `effective_deadline` pure function; `LifecycleManager` with per-instance state (created_at, idle_since, in_flight_job, accepting_new_jobs); `start_job`/`finish_job`/`should_reap`/`should_drain`/`is_liveness_OK`/`accepting_new_jobs`/`in_flight_job`; `warm_reuse_or_create` destroys + creates on reap; dead-man window = 2×idle_timeout; `last_signal = max(heartbeat or 0, created_at)` avoids killing brand-new instances. 9/9 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 18: Ledger + destroy_confirmed + reap + BudgetTracker (`src/kinoforge/core/lifecycle.py`, `tests/core/test_lifecycle_sweeper.py`) — commit 5a9a5e3. `Ledger` persists instance records to ArtifactStore as ledger.json; `destroy_confirmed` polls until gone with injectable sleep and raises TeardownError+logs ERROR on failure; `reap` sweeps over-age and idle instances via destroy_confirmed; `BudgetTracker.enforce` destroys before raising BudgetExceeded. 9/9 AC tests pass; mypy + ruff + pre-commit clean.
- [x] Phase 6: CivitAI + HuggingFace sources
  - [x] Task 19a: CivitAISource (`src/kinoforge/sources/civitai/__init__.py`, `tests/sources/test_civitai.py`) — commit f786de1. `CivitAISource` resolves `civitai:<modelId>[@<versionId>]` refs via CivitAI REST API; injectable `fetch` transport; `CIVITAI_TOKEN` attached to HTTP request + Artifact headers; model-only path hits `/models/{id}` then `/model-versions/{vid}`; `AuthError` re-raised. 14/14 tests pass.
  - [x] Task 19b: HuggingFaceSource (`src/kinoforge/sources/huggingface/__init__.py`, `tests/sources/test_huggingface.py`) — commit dc8715e. `HuggingFaceSource` resolves `hf:<repo>:<path>` refs to canonical HF resolve URLs (no HTTP calls); `HF_TOKEN` attached to Artifact headers; bare repo ref raises `ValidationError` with "specify a file path" message (directory listing DEFERRED); self-registers under `"hf"`. 11/11 tests pass; mypy + ruff + pre-commit clean.
- [x] Phase 7: ComfyUI engine (+node installer) + RunPodProvider — Tasks 20a+20b complete
  - [x] Task 20a: ComfyUIEngine + ComfyUIBackend + git node installer (`src/kinoforge/engines/comfyui/__init__.py`, `src/kinoforge/engines/comfyui/nodes.py`, `tests/engines/test_comfyui.py`) — commit 3e9c223. `provision` clones nodes via injected `run_cmd`, installs `requirements.txt` via `file_exists` spy, routes models via `TARGET_TO_SUBDIR` + injected `route_file`, launches ComfyUI with `launch_args`. `submit` deep-merges `node_overrides` onto `graph` and POSTs to `/prompt`; `result` polls `/history/{id}` until outputs present. All I/O seams injected; self-registers under `"comfyui"`. 23/23 AC tests pass.
  - [x] Task 20b: RunPodProvider (pod+serverless) (`src/kinoforge/providers/runpod/__init__.py`, `src/kinoforge/providers/runpod/selfterm.py`, `tests/providers/test_runpod.py`) — commit 1be572d. Pod mode: `find_offers` (http_get→filter_offers), `create_instance` injects `RUNPOD_TERMINATE_KEY` (scoped, not main key) + `KINOFORGE_SELFTERM_SCRIPT` via `selfterm.RENDER(...)`. Serverless mode: concurrency caps from Lifecycle, `status="ready"` immediately. `endpoints` uses `https://{id}-{port}.proxy.runpod.net` (pod) / `/v2/{id}/run` (serverless). `destroy_instance` polls+raises TeardownError, idempotent. All HTTP via injected seams; self-registers under `"runpod"`. 24/24 tests pass.
- [x] Phase 8 (partial): Tasks 21a–21b complete
  - [x] Task 21a: DiffusersEngine + DiffusersBackend (`src/kinoforge/engines/diffusers/__init__.py`, `tests/engines/test_diffusers.py`) — commit 157325b. `provision` runs pip install + server_cmd via injected `run_cmd`; `backend` constructs `DiffusersBackend` with cfg base_url; `submit` POSTs to `/generate`; `result` polls `/status/{job_id}` until done; `validate_spec` requires `pipeline` + `scheduler`; `declared_flags` returns copy from map; self-registers under `"diffusers"`. 25/25 tests pass.
  - [x] Task 21b: HostedAPIEngine + HostedAPIBackend (`src/kinoforge/engines/hosted/__init__.py`, `tests/engines/test_hosted.py`) — commit ad5c726. `requires_compute=False`, `requires_local_weights=False`; `provision(None, cfg)` validates cred via injected `CredentialProvider` + pings health URL via injected `http_get`; raises `AuthError` on missing cred, `KinoforgeError("hosted endpoint unreachable: …")` on ping failure, `KinoforgeError` if non-None instance passed; `backend(None, cfg)` returns `HostedAPIBackend`; `submit` POSTs to endpoint; `result` polls `/status/{job_id}`; `validate_spec` requires `model`+`params`; `key_base(cfg)` returns hosted model ID; `declared_flags` returns copy from map; self-registers under `"hosted"`. 25/25 tests pass; mypy/ruff/pre-commit clean.
  - [x] Task 21c: SkyPilotProvider (lazy import) — commit e069dfe. `SkyPilotProvider(ComputeProvider)` with `_get_sky()` lazy import (only inside function body, never at module top level); injectable `sky_client` seam so tests run without skypilot installed; `idle_timeout_s → autostop` (minutes) mapping via `sky_client.launch(task_config, autostop=...)`;  `list_instances()` via `sky_client.status()`; `destroy_instance()` calls `sky_client.down()` then polls until gone; `get_instance()` raises `KeyError` when absent; `endpoints()` returns `{"ssh": "ssh://<id>"}`. 16/16 AC tests pass; mypy/ruff/pre-commit clean.
- [x] Phase 9 (partial): CLI — Task 22 complete
  - [x] Task 22: CLI + `__main__` — `_adapters.py` (sole concrete-import hub), `cli.py` (deploy/provision/generate/list/status/stop/destroy/reap/gc), `__main__.py` wired. Duplicate-pod guard, UnknownAdapter catch, instance overview header, 8/8 ACs pass. — commit 4b4e31e
- [x] Phase 9 (complete): Examples, README, CI — Task 23 complete
  - [x] Task 23: `examples/configs/{wan,diffusers,hosted,local-fake}.yaml`, `README.md` (6 required headings), `.github/workflows/ci.yml` (3-OS matrix), `tests/test_examples.py` (21 tests). All 6 ACs pass. — commit 1b7f662
  - [x] Task 24: `tests/test_core_invariant.py` — 3-AC lockdown: subprocess isolation (no adapter modules in sys.modules after core import), vendor-SDK confinement scan (sky/skypilot→providers/skypilot, runpod→providers/runpod), core-import ban scan (no kinoforge.providers/sources/engines in core/). All 3 tests pass; mypy/ruff/pre-commit clean. — commit e2f9b37

## Key decisions & gotchas
- Core NEVER imports a concrete provider/source/engine — registry-mediated by name/scheme. Reviewer enforces.
- 8 open questions resolved in DESIGN.md §1 (submit/result+Pool, models-per-engine, params-vs-spec, profile-cache location, serverless caps, artifact GC, role vocab, under-use warning).
- Discovery ordering is explicit & guaranteed (resolve→validate→split→provision→verify); fail-hard on drift tears down compute.
- Cost-safety: invariant universal, mechanism provider-specific. RunPod in-pod self-terminator + least-privilege terminate-only cred; SkyPilot native autostop; LocalProvider injectable clock for tests.
- `CapabilityKey.derive()` uses `json.dumps`, not separator scheme — JSON escaping guarantees distinct tuples never collide (caught in commit `7e70a57`).
- Config requires exactly one `kind: base` model entry — zero or many rejected at load time (commit `94afa3e`).
- Splitter is pluggable ABC+registry, not a single function — future LLM/scene-detect strategies slot in as adapters. `HeuristicSplitter` uses blank-line markers.
- `validate_request` called exactly once per `generate()` — orchestrator calls it; `GenerateClipStage` `segments_override` branch skips re-validation.
- Asset attachment is an orchestrator concern, not a splitter concern — splitter returns segments with empty assets; orchestrator attaches to seg-0 via `dataclasses.replace`.
- Continuity dispatch via `MODE_ROLE_REQUIREMENTS` — injects only when `"init_image"` in role contract (i2v today; t2v/flf2v skip); future modes automatic.
- `ArtifactStore.uri_for(run_id, name)` is pure, no I/O — returns URI it *would* address; invariant: `uri_for == put_*.uri`. Unblocks S3/GCS.
- Concrete ABC defaults are a legitimate extension pattern — `GenerationEngine.extract_last_frame` is a concrete default that raises; engines opt in by override.
- S3/GCS shipped as two independent siblings, no shared cloud-base — ~30 LOC duplication acceptable; avoids locking guesses about future stores (Azure, B2, R2). Factor when third cloud lands.
- SDK credential discovery uses default chains, not kinoforge plumbing — boto3 walks AWS env → `~/.aws/credentials` → IMDS → IAM role; GCS walks `GOOGLE_APPLICATION_CREDENTIALS` → gcloud ADC → GCE metadata. Routing through `EnvCredentialProvider` would defeat IMDS/IAM-role auto-discovery.
- `.env` loader is a transparent shim at CLI entry — populates `os.environ` once; every downstream consumer (EnvCredentialProvider, boto3, GCS default chains) reads unchanged. `override=True` is library-only, no CLI flag.
- Deferred (interface + 1 path only, layers NOT built): stitching, audio, keyframe stage, cross-process discovery lock. (Splitter, uri_for, continuity, S3/GCS, .env loader, concurrent pool now built.)
- Deps stdlib-first: pydantic + PyYAML + python-dotenv runtime; boto3 + google-cloud-storage lazy-import-gated; skypilot optional/lazy; urllib for all HTTP; stdlib logging.
- TDD red-first, fully offline (LocalProvider/FakeProvider/FakeSource/FakeEngine + injectable clock + Fake cloud clients). No real cloud/net/GPU/weights in any test.

## Established patterns for layer development

Patterns proven across MVP + Layers A–D. New layers should follow them by default; deviation needs justification.

- Injected I/O seams on every adapter — HTTP/subprocess/filesystem as constructor params with stdlib defaults; tests pass spies; no real network/subprocess/git/GPU in tests.
- Self-registration on import — zero-arg factories for engines/providers/stores; instances for sources (dispatch by `handles(ref)`).
- Source dispatch by behaviour, not key equality — `source_for_ref(ref)` asks each registered source `handles(ref)`, returns first match.
- Stage protocol + pool-swap — stages talk only to `BackendPool`/`ArtifactStore`/`ModelProfile`; `SequentialPool` is default, `ConcurrentPool` drops in via same ABC; future distributed variants (Ray, cross-process) follow the same pattern.
- `ConcurrentPool` dispatch pattern — `_Slot(backend, executor, cap, in_flight)` per backend; `submit` picks least-loaded slot by `in_flight / cap` ratio under a per-pool lock (ties broken by registration order via `min`'s left-bias), then dispatches to that slot's `ThreadPoolExecutor`; `_run_one` releases the counter via `try/finally` so backend failures don't leak slots; `map` submits all eagerly, iterates futures in input order (preserves result ordering), on first exception cancels queued + drains in-flight + re-raises; `close` flips closed flag under lock then calls `executor.shutdown(wait=True)` per slot outside the lock for deterministic shutdown.
- Strategy / validation / continuity / splitter helpers are pure functions — `decide`, `validate_request`, `inject_tail_frame`, `split()` all return new objects, never mutate input.
- `dataclasses.replace` for every immutable update — no mutation paths; tests verify with `is`-identity on unchanged fields.
- TDD red-first, every task — write failing test first, confirm FAIL, then implement; `test-design` skill (bug-catch comments, no implementation mirroring, no over-mocking).
- In-core defaults wire themselves via `core/__init__.py` — `HeuristicSplitter` self-registers here, not in `_adapters.py` (preserves the core-import-ban invariant).
- `Field(default_factory=NestedModel)` for optional pydantic nested blocks — `Config.splitter`, `Config.store` both use this pattern.
- Brainstorm → spec → plan → execute → ship — superpowers workflow with brainstorming skill, spec doc committed, plan with full HEREDOC code blocks, subagent per task, two-stage review, whole-branch review, `--no-ff` merge.
- Spec self-review before commit — strip "implementer must grep" footnotes; provide exact line numbers, variable names, diff snippets. Saves round-trips.
- ABC change → pre-implementation grep for construction sites — adding a required dataclass field breaks every site; plan must enumerate them.
- Cloud SDK lazy-import gate — `__init__(client=None)` with `if client is None: import <sdk>` inside; tests inject fake. Dual-gate for GCS (client + exception module).
- Shared test conftest for sibling adapters — `tests/stores/conftest.py` holds both `FakeS3Client` + `FakeGCSClient` even when only one is used per task.
- CLI dispatcher with lazy SDK imports per branch — `_build_store(cfg, state_dir)` dispatches by `cfg.store.kind`, imports heavy SDKs only on relevant branches; keeps CLI startup fast.
- Two-stage review (spec compliance first, then code quality) — spec reviewer catches contract mismatches; quality reviewer focuses on placement, imports, mutations, tests. Different classes of issue.
- Quality reviewer can escalate NICE → FIX-REQUIRED — if an unraised case is reachable by test or supported caller pattern, it's a gap not a polish nit.
- `--no-ff` merge pattern with substantive body — merge commit references layer name, AC state, per-task commits, GitHub issue via `Closes #N` trailer. Natural layer boundary.
- Builder subagent (caveman:cavecrew-builder) has no Bash — use for surgical 1-2 line edits; controller handles verify-and-commit.
- `import X.Y.Z as alias` for lazy SDK imports — ruff-format wraps `from X.Y import Z` onto multiple lines and splits type-ignore comments off; the alias form keeps comments attached.

## Known limitations & follow-ups

Carry-forward gaps + post-Layer-D housekeeping. Each is a candidate for a future layer.

**Real-cloud verification gaps (offline-tested only):**
- ~~`RunPodProvider.find_offers` REST shape is a stub~~ — **CLOSED** by Phase 24 (Layer N). Real-cloud verified end-to-end; 10 production bugs fixed.
- `SkyPilotProvider._get_sky()` lazy path wired but unexercised against real `sky` SDK.
- `S3ArtifactStore` + `GCSArtifactStore` never hit real cloud — fake clients don't simulate multipart edge cases, transient retries, SSE/KMS, signed URLs.

**Architectural follow-ups:**
- ~~**Layer F: engine `submit()` ignores seg-0 assets.**~~ Closed by Phase 16 (see below).
- `cli._cmd_status` queries in-process provider state only, not the ledger.
- `provisioner.provision` typed as `_ProvisionConfig` Protocol — `# type: ignore[arg-type]` at call site for mypy generic variance.
- `GenerateClipStage` persists only final artifact (intermediates in-memory) — stitching, when shipped, must refactor persistence model or stitching read path.
- `flf2v + N > 1 + non-native` is a pre-existing gap (no continuity for two-image-bookend non-native).
- `test_core_invariant.py` allowlist does not yet include a `splitters/` directory — first adapter splitter (LLM, scene-detect) must extend the allowlist.

**Layer C / D residuals:**
- Ledger remains local-only by CLI wiring — `cli._ledger(state_dir)` always constructs a `LocalArtifactStore(state_dir)` even when `store.kind` is `s3`/`gcs`. Cross-process safety for cloud-backed ledger is now available via Layer H (`store.acquire_lock`), but routing the CLI through the configured cloud store still needs a follow-up.
- Default zero-arg store factories require env vars set — `register_store("s3", _default_factory)` reads `KINOFORGE_S3_BUCKET`; raises with helpful message when unset. The CLI doesn't use this path (constructs directly via `_build_store`).
- No multipart threshold knob on cloud stores — SDK defaults (boto3 ~8 MiB) cover the common case; if real workloads need custom control, kwargs are a future layer.

**CI / platform:**
- Windows CI declined — see `windows-migration-cancelled.md`. Linux + macOS only.

**Breaking changes already shipped:**
- `kinoforge gc` requires `--config PATH` (since Layer C) — anyone resuming with old shell scripts must update.

## GitHub issues status

| # | Title | Status |
|---|---|---|
| #1 | Continuity / stitching fallback | CLOSED (Layer B) |
| #2 | Audio sync stage | Open |
| #3 | Concurrent / distributed backend scheduler | CLOSED (Layer G) |
| #4 | Keyframe / image-generation upstream Stage | Open |
| #5 | S3 / GCS artifact stores | CLOSED (Layer C) |
| #6 | `ArtifactStore.uri_for(run_id, name)` ABC | CLOSED (Layer A) |
| #7 | Cross-process discovery lock | CLOSED (Layer H) |
| #8 | HuggingFaceSource bare-repo listing | Open |
| #9 | aria2c fast-path | Open |

## Single next action
**Layer O nearly complete on `build/layer-o`** (Tasks 1-9 landed at commits
`3f621e9` → Task 9 commit). RunPod-pivoted Layer O = user-facing output
directory. Final task (Task 10) is the full gate + `--no-ff` merge to `main`
once Task 9's docs commit settles. Once merged, next layer candidate:
**Layer P** — engine integration on RunPod (ComfyUI + Wan i2v producing a
real MP4 artifact, originally slotted as Layer O before the output-dir
operator pain became more pressing).

**Pending follow-ups:**
- ~~`GenerateClipStage._artifact_bytes` HTTP seam normalization (Phase 19 follow-up; needs Authorization-header support for RunwayML/Pika).~~ — **CLOSED** by Phase 23 (Layer M).
- ~~`engine.hosted.model` ↔ `spec.model` duplication collapse (Layer K hosted YAML ambiguity).~~ — **CLOSED** by Phase 23 (Layer M).
- ~~`kinoforge batch` CLI subcommand~~ — **CLOSED** by Phase 22 (Layer L), see below.

**Layer L Task 4 — streaming per-entry log lines (DEFERRED, ships in a later follow-up):**
- Layer L spec §5 and the plan show streaming per-entry markers during the run
  (`[batch-...] waves start`, `[batch-...] waves ok 14.2s ...`), but the CLI as
  shipped at `c940da9` only prints the initial `manifest loaded` header and the
  final per-entry summary table. The final table already shows everything users
  need post-run, and none of the 6 batch-CLI tests assert mid-run output, so the
  visible UAT contract is met — but the spec/plan and the implementation now
  disagree on intra-run progress. Closing this gap requires a callback hook into
  `batch_generate` (since `core/` cannot print directly without breaking the
  core-import-ban invariant); deferring keeps Task 4 focused and lets a future
  contributor add the seam + a streaming-output test in one self-contained
  change. Owner: whoever picks up Layer L Task 5 or a follow-up polish phase.

## Post-MVP

### Phase 10 — prompt splitter (deferred layer #1 from handoff §7)
- [x] Task 1: Splitter ABC + register/get registry helpers — commit 231fcc4
- [x] Task 2: HeuristicSplitter + core self-registration trigger — commit f522e2b
- [x] Task 3: SplitterConfig optional block (defaults to heuristic) — commit fd0978a
- [x] Task 4: Orchestrator step-6 wiring + stage validate-once + README/PROGRESS — commit d1828b7

### Phase 11 — uri_for ABC (deferred layer A, GitHub issue #6)
- [x] Task 1: Add `ArtifactStore.uri_for` ABC method + LocalArtifactStore impl + tests — commit `a6f8950`
- [x] Task 2: Refactor JsonProfileCache to use `store.uri_for`; delete `_uri_index`, `_uri_for`, `_reconstruct_uri` — commit `dd08f0c` (closes #6)

### Phase 12 — continuity fallback (deferred layer B, GitHub issue #1)
- [x] Task 1: Add `inject_tail_frame` helper + `extract_last_frame` ABC default + FakeEngine impl — commit `b9cb44b`
- [x] Task 2: Wire continuity into GenerateClipStage non-native branch — commit `270accd` (closes #1)

### Phase 13 — S3 / GCS artifact stores (deferred layer C, GitHub issue #5)
- [x] Task 1: S3ArtifactStore + deps + invariant patterns + adapters wire + 17 tests — commit `424c7c9`
- [x] Task 2: GCSArtifactStore + adapters wire + 17 tests — commit `057caaf`
- [x] Task 3: StoreConfig pydantic block + 6 tests + YAML example — commit `41cc75d`
- [x] Task 4: CLI _build_store + 3 call-site swaps + 3 tests + Layer-A _path peek fix — commit `1cd1f15` (+ docstring polish at `b661576`) (closes #5)

**CLI breaking change (Task 4):** `kinoforge gc` subcommand gained a required `--config PATH` argument so it can read the optional `store:` block; anyone resuming the project must update existing `gc` invocations accordingly.

### Phase 14 — .env secrets loader (post-MVP Layer D)
- [x] Task 1: python-dotenv dep + .gitignore .env + .env.example — commit `59f732e`
- [x] Task 2: dotenv_loader module + 8 unit tests — commit `0dc4714` (+ polish at `366ce5d`)
- [x] Task 3: CLI --env-file flag + 2 integration tests — commit `727ee2f` (+ polish at `b9056cf`)
- [x] Task 4: README Credentials section + PROGRESS Phase 14 entry — commit `d4be826`

### Phase 15 — per-engine extract_last_frame (post-MVP Layer E)
- [x] Task 1: `FrameExtractionError` + `core/frames.ffmpeg_last_frame` helper + injectable subprocess seam — commit `ba265bb` (+ missing-ffmpeg wrap + test strengthening at `ec04976`)
- [x] Task 2: ABC contract change (`extract_last_frame -> bytes`) + `inject_tail_frame` simplification + FakeEngine bytes return — commit `b6fca7a` (+ docstring polish at `d150613`)
- [x] Task 3: `GenerateClipStage` non-native rewiring (extract → put_bytes → wrap → inject) — commit `0c2c7a0` (+ filename-population + chain-test strengthening at `f41f3c4`)
- [x] Task 4: ComfyUI `result()` /view URL backfill + `extract_last_frame` + 2 seams — commit `50a08bb` (+ filename URL-encoding at `e4151ff`)
- [x] Task 5: Diffusers `result()` URL passthrough + `extract_last_frame` + 2 seams + server contract doc — commit `9df1dfd` (+ url-shadowing rename at `3d6ce7a`)
- [x] Task 6: Hosted `url_path` cfg + dot-walker + `result()` backfill + `extract_last_frame` + 2 seams — commit `c10b111`
- [x] Cross-engine fetch-error wrap (Task 4/5/6 retrofit) — commit `0d2d2c3`. All three engines now wrap `http_get_bytes` exceptions as `FrameExtractionError` per spec §4.3.

### Phase 16 — per-engine asset wiring (post-MVP Layer F)
- [x] Task 1: `AssetFetchError` + `core/assets.py` (find_asset, asset_bytes, set_by_dot_path) + 10 tests — commit `8335ff9`
- [x] Task 2: Diffusers backend `asset_paths` + submit + validate_spec + 4 tests — commit `a62d110`
- [x] Task 3: Hosted backend `asset_paths` + submit + validate_spec + 4 tests — commit `d25c5c8`
- [x] Task 4: ComfyUI backend `http_get_bytes` + `http_post_file` seams + `asset_node_ids` + 8 tests — commit `40dfaec`
- [x] Task 4 (review fix): random multipart boundary + filename escape + AssetFetchError wrapping + 8 tests — commit `e6826c6`
- [x] Task 5: GenerateClipStage post-chain `validate_spec` + 3 tests — commit `22269ed`
- [x] Task 6: README + PROGRESS + final gate + merge — commit `a271a03` (+ Phase 16 SHA backfill at `cb94413`; merge commit `3037bde`)
- [x] Post-merge fix: pydantic cfg strip closed for Layer E `url_path` + Layer F `asset_paths`. `HostedEngineConfig` gains `url_path`/`asset_paths`/`api_key_env`/`health_url`; new `DiffusersEngineConfig` registered on `EngineConfig.diffusers`. 7 cfg round-trip tests + 2 YAML→engine.backend E2E tests close the silent-strip defect that bypassed both Layer F unit tests and Layer E tests — commit `484e368`. Post-Layer-F count: 524 tests.

### Phase 17 — concurrent backend scheduler (post-MVP Layer G, GitHub issue #3)
- [x] Task 1: `BackendPool.close()` ABC method + context-manager (`__enter__`/`__exit__`) + `SequentialPool` no-op impl + 4 parity tests — commit `a344bc8`
- [x] Task 1 cleanup: drop `func-returns-value` suppression in close-noop test — commit `f770a8b`
- [x] Task 2: `ConcurrentPool` core dispatch: `_Slot` (backend + `ThreadPoolExecutor` + cap + lock-protected `in_flight` counter), `submit` (least-loaded-by-utilization pick under lock, executor dispatch outside lock), `close` shutdown — commit `a6f504a`
- [x] Task 2 fix: release slot `in_flight` counter when `executor.submit` raises to prevent slot leak — commit `0725457`
- [x] Task 3: `ConcurrentPool.map` with fail-fast cancellation: eager submits all futures, iterates in input order (preserves result ordering), cancels remaining queued futures on first exception, drains in-flight, re-raises first exception — commit `a4d4421`
- [x] Task 4: `GenerateClipStage` branches on `should_chain` (i2v non-native → serial loop; t2v non-native → `pool.map` fan-out) — commit `7ba9974`
- [x] Task 4 hardening: spy on `pool.map` in 1-job test for discriminating assertion — commit `24356cc`
- [x] Task 5: `orchestrator.generate()` wraps stage inside `with ConcurrentPool() as pool: pool.add(backend, max_in_flight=cfg.lifecycle().max_in_flight)`; `SequentialPool` import removed — commit `c90b046`
- [x] Task 6: `LifecycleConfig.max_in_flight` field + wire through `lifecycle()` method; README Concurrency section; PROGRESS Phase 17 — commit `b7e57fc` (Phase 17 Task 6 SHA backfill at `eed9706`)
- [x] Task 6 regression test: lock down YAML→`Lifecycle.max_in_flight` wiring so a future drop of the `lc.max_in_flight=` line in `Config.lifecycle()` fails fast instead of silently defaulting to cap=1 — commit `bab8d64`
- [x] Task 6 doc corrections: fix Phase 17 Task 2/3 inaccuracies (semaphore → lock-protected counter; `as_completed` → input-order iteration); refresh test count — commits `4622083` + `08eb48b`
- [x] Merge to main via `--no-ff` — merge commit `9e02e15` (closes #3)

### Phase 18 — cross-process discovery lock (post-MVP Layer H, GitHub issue #7)
- [x] Task 1: `core/locks.py` — `Lock` Protocol + `LockToken` + `InMemoryLock` + `LockError`/`LockTimeout` in `core/errors.py` — commit `a1802d3` (+ fix `81052a8`)
- [x] Task 2: `ArtifactStore.acquire_lock` abstract method + temporary `NotImplementedError` stubs on 3 stores — commit `6a4d8dc` (+ test gap fix `15742f0`)
- [x] Task 3: `FileLock` (fcntl) + `LocalArtifactStore.acquire_lock`; subprocess integration test — commit `0ac9d90` (+ fix `98bc569`)
- [x] Task 4: `S3CloudLock` (`IfNoneMatch="*"`) + `S3ArtifactStore.acquire_lock` + `FakeS3Client` precondition support — commit `b26c6fd`
- [x] Task 5: `GCSCloudLock` (`if_generation_match=0`) + `GCSArtifactStore.acquire_lock` + `FakeGCSClient` generation tracking — commit `9ac0abd`
- [x] Task 6: `JsonProfileCache.resolve_or_discover` outer-lock wrap; cache-hit fast path preserved; `discover_ttl_s` kwarg — commit `e03d28a` (+ import cleanup `8c2d175`)
- [x] Task 7: `Ledger.record`/`forget` outer-lock wrap; `mutate_ttl_s` kwarg; `entries()` stays lock-free — commit `c8372f6`
- [x] Task 8: README "Multi-node coordination" section + PROGRESS Phase 18 — commit `351d691`
- [x] Merge to main via `--no-ff` — merge commit `4672735` (closes #7)

### Phase 19 — Layer I (fal.ai adapter + UX A + hosted hardening)

- [x] Hot-fix: provisioner cfg-dict — commit `e78cafc` on `main`
- [x] Task 1: Diffusers + ComfyUI provisioner-cfg regression — commit `78a09e1`
- [x] Task 2: declared_flags WARNING → DEBUG — commits `46653ec` + `b1d8b1b`
- [x] Task 3: FakeEngine declared_flags_map default — commit `c586f01`
- [x] Task 4: HostedEngineConfig validators — commit `c1a1c85`
- [x] Task 5: HostedAPIEngine AuthError + declared_flags_map default — commit `d7460f8`
- [x] Task 6: Rewrite hosted.yaml + shim contract docs — commit `bd35810`
- [x] Task 7: core/provision_state.py — commit `a285c36`
- [x] Task 8: UX A hosted preflight — commit `9d5bcd8`
- [x] Task 9: UX A compute preflight + marker — commit `4d573b5`
- [x] Task 10: FalEngineConfig pydantic block — commits `96d45a8` + `2680b22`
- [x] Task 11: FalEngine + FalBackend + wire — commits `7e3327a` + `0d324dc`
- [x] Task 12: _adapters + fal.yaml + invariant + tooling — commit `9be6e67`
- [x] Task 13: Live opt-in test + manual smoke — commit `bf3841f`
- [x] Merge to main via `--no-ff` — merge commit `0b2a8d7`

**First real artifact:** `/tmp/kinoforge-fal-smoke/smoke-i-1/n9TG4YoyIIkzR1rouhQCw_tmpykhkugmc.mp4` — 3,073,440 bytes, MP4 (`ftyp isom`), produced by `fal-ai/wan-t2v` via `examples/configs/fal.yaml` (capability_key `2820ed10e74fbea4bb4ab8e3d338f716db8d86383869ebf793bed423f507caaa`, git SHA `9be6e67` at smoke time).

**Live-smoke bug catches integrated into Task 13:**
- `examples/configs/fal.yaml` endpoint changed `fal-ai/wan/v2.2/t2v` (404 on result URL — fal.ai rewrites the family path back to `fal-ai/wan/...` which 404s on GET) → `fal-ai/wan-t2v` (queue family matches; status/response URLs round-trip cleanly).
- `FalBackend.submit` now falls back to `segments[0].prompt` when `job.spec` lacks `"prompt"` — the orchestrator places the user prompt on the Segment, not in the engine spec, so without this the fal POST body contained only `_audio_mode` and fal silently completed a no-op job that 422'd on result fetch.
- `FalEngine.validate_spec` widened to accept a non-empty prompt on `segments[0]` as well as `job.spec` (mirrors the new submit fallback).
- `GenerateClipStage._artifact_bytes` now resolves `uri` → local file read → `url` → HTTP download → synthetic-fallback (FakeEngine path).  Hosted/queue engines that return `Artifact(url="https://...mp4")` previously had their bytes silently replaced with debug-stub bytes.
- CLI `provision` and `generate` accept `-c` as a short alias for `--config` so the documented quickstart works verbatim.
- README "Real providers — fal.ai" quickstart added.

### Phase 20 — Layer J (cross-engine prompt fallback)

- [x] Task 1: `core/prompt_routing.py` + 8 helper tests — commit `ba078ec`
- [x] Task 2: `prompt_body_key` on hosted + diffusers configs + 4 round-trip tests — commit `4c87e27`
- [x] Task 3: HostedAPIBackend + Engine wire + 6 tests (5 routing + 1 E2E YAML) — commit `cc7b3dd`
- [x] Task 4: DiffusersBackend + Engine wire + 6 tests — commit `e3e4244`
- [x] Task 5: ComfyUIBackend + Engine wire (spec-level `prompt_node_ids`) + 6 tests — commit `acf93c2`
- [x] Task 6: FalBackend retrofit (drop inline fallback, use helper) — commit `36cdc5c`
- [x] Task 7: Examples + README + PROGRESS — commit `ec65c01`

**Key design decisions:**
- Shared helper in `core/prompt_routing.py` (Q1=B): single `resolve_prompt(job)` consumed by all 4 engines.
- Hosted/Diffusers default `prompt_body_key="prompt"` (Q4=A) with opt-out via `null`.
- ComfyUI `prompt_node_ids` lives in `job.spec`, not cfg (Q6=A) — mirrors `asset_node_ids` symmetry.
- Opt-in `validate_spec` raise (Q3=A): legacy configs untouched.
- Fal retrofit (Q5=A): behavior preserved.

**Known follow-up (necessary but out of scope):** `Orchestrator.generate` hardcodes `base_spec={}` (`src/kinoforge/core/orchestrator.py:605`). Routing YAML-supplied spec into the orchestrator (model/params for hosted, pipeline/scheduler for diffusers, graph/node_overrides for comfyui) is a separate Layer K candidate. Hosted/Diffusers/ComfyUI orchestrator-driven runs remain blocked on missing required spec keys until that work lands.

### Phase 21 — Layer K (spec & params routing)

- [x] Task 1: Config.spec + Config.params pydantic fields + 4 round-trip tests — commit `638937e`
- [x] Task 2: Orchestrator routes cfg.spec/cfg.params + validate_spec moved into stage + ValidationError teardown + 4 tests — commit `3606527`
- [x] Task 3: Strategy precedence regression locks (segment-wins + _audio_mode authority) — commit `8b81eb2`
- [x] Task 4: e2e YAML round-trip via Orchestrator — commit `2b5fa25`
- [x] Task 5: hosted/diffusers/wan/fal example YAMLs + 4 extended example-load tests — commit `0d3c514`
- [x] Task 6: README + PROGRESS + full suite gate — commit `23ca0e0`
- [x] Merge to main via `--no-ff` — merge commit `13fc395`

**Key design decisions:**
- Permissive `dict[str, Any]` (Q3=A): Config stays engine-agnostic, preserves the core-import-ban invariant. `engine.validate_spec` is the sole gate.
- Top-level YAML siblings (Q2=A): `spec:` and `params:` live alongside `engine:` / `models:` / `lifecycle:`, not nested per-engine.
- Teardown on `ValidationError` (Q5=A): orchestrator mirrors the existing `CapabilityMismatch` branch; a config typo does not leak compute.
- `dict(...)` copy at stage construction: defends against any future engine that mutates `job.spec`.
- `validate_spec` moved into `GenerateClipStage.run` (after `decide`, before any dispatch): closes a pre-existing gap where `validate_spec` only ran for chained tail-frame jobs.

**Hosted YAML ambiguity (carried forward):** `engine.hosted.model` (cache identity, fed to `key_base(cfg)`) and `spec.model` (wire body) coincide today but are read by different callers. Documented in `examples/configs/hosted.yaml` comment block; collapsing them is a Layer-L+ candidate.

**Test count:** 708 tests passed + 1 skipped (was 693 + 1 skipped before Layer K, +15 net).

### Phase 22 — Layer L (`kinoforge batch` CLI)

- [x] Task 1: deploy_session context manager extraction — commit `f971c4c` (+ polish `25b4dc7`)
- [x] Task 2: core/batch.py manifest models + load_manifest — commit `def94dc` (+ polish `ac06873`)
- [x] Task 3: batch_generate() core function — commit `f06fa3b` (+ polish `6122215`)
- [x] Task 4: kinoforge batch CLI subcommand — commit `4e8a564` (+ polish `c940da9` + streaming-log deferral note `38d5394`)
- [x] Task 5: examples + README + PROGRESS + full gate — commit `cc50ba8`
- [x] Merge to main via `--no-ff` — merge commit `da072a3` (closes PROGRESS:155 follow-up #3)

**Key design decisions:**
- Shared deploy across N entries (Q1=A): one `create_instance`, ConcurrentPool fans entries; `deploy_session` is the reusable seam.
- YAML manifest with per-entry `prompt`/`prompt_file` (Q2=A): pydantic `extra="forbid"` + exactly-one-of validator; `prompt_file` paths resolve relative to the manifest's parent dir; auto-indexed `run_id` when omitted.
- `batch_id` default `batch-YYYYMMDD-HHMMSS` in LOCAL timezone (Q3 clarification): override with `--batch-id`.
- Continue-on-error per entry; batch-fatal on `BudgetExceeded` / `CapabilityMismatch` / `TeardownError` (Q4=A) → cancel queued + exit code 2.
- `deploy_session` extraction (Q5=B refactor): both `generate()` and `batch_generate()` consume it; zero behavior change to `generate()` — all 708 pre-Layer-L tests pass unmodified.
- `_batch_summary.json` written in a `finally` clause regardless of exit path; in-flight entries at fatal-abort time are recorded as `interrupted`.
- Per-entry param/spec overrides are shallow-merged onto `cfg.params` / `cfg.spec` (entry wins per key) via a fresh `dict(...)` copy at stage construction — no mutation leaks to siblings or to `cfg`.

**Streaming per-entry log lines (DEFERRED):** the CLI prints the initial `manifest loaded` header and the final per-entry summary table but no mid-run markers — see the "Layer L Task 4" note in the Single-next-action block above (committed at `38d5394`). Closing the gap requires a callback hook into `batch_generate` so `core/` does not print directly. Future contributor picks this up as a self-contained polish phase.

**Test count:** 741 tests passed + 1 skipped before Task 5 → 743 tests passed + 1 skipped after Task 5 (+35 net across Layer L; pre-Layer-L baseline was 708 + 1).

### Phase 23 — Layer M (hosted-YAML collapse + Authorization-header passthrough)

- [x] Task 1: HostedEngineConfig.model dropped + model_validator migration + tests — commit `e63cf61` (+ fix `c50b701`)
- [x] Task 2: HostedAPIEngine.key_base reads cfg["spec"]["model"] + retrofit _BASE_CFG + new tests — commit `d4d583f` (+ fix `5f4f11b`)
- [x] Task 3: examples/configs/hosted.yaml cleaned + test_hosted_yaml smell-lock rewritten — commit `5ab4493` (+ plan-sync `986a64a`)
- [x] Task 4: GenerateClipStage gains http_get_bytes seam; _artifact_bytes threads Artifact.headers — commit `c482a05` (+ fix `9b3df5e`)
- [x] Task 5: HostedAPIBackend.result populates Authorization: Bearer header — commit `67e3236` (+ docstring `9ef0efe`)
- [x] Task 6: E2E test + README + PROGRESS + full gate — commit `3ea5cfa`
- [x] Merge to main via `--no-ff` — merge commit `862e2d5` (closes PROGRESS:155 follow-ups #1 + #2)

**Key design decisions:**
- spec.model is the single source of truth for hosted model identity (Q2=A): cache identity and wire body cannot meaningfully diverge for hosted engines.
- Hard-cut migration with a guiding `model_validator` (Q4=A): matches the `kinoforge gc --config` precedent; deprecation cycles would drag the smell through one more layer for zero functional gain.
- Authorization passthrough via `Artifact.headers` + injectable `http_get_bytes` seam (Q3=A): mirrors the PROGRESS:87 "injected I/O seam" pattern; no new ABC.
- HostedAPIEngine retrofitted as the in-tree consumer of the seam (Q5=A): exercises the auth path end-to-end without waiting for a future RunwayML/Pika adapter.
- Out of scope (Layer N candidate): real-cloud verification gaps (RunPod find_offers shape, SkyPilot SDK smoke, S3/GCS medium-fidelity tests).

**Test count:** 743 passed + 1 skipped pre-Layer-M → ~755 passed + 1 skipped post-Layer-M (+12 net new; +2 retrofits on AC1/AC6).

### Phase 24 — Layer N (RunPod cloud-fidelity hardening)

Verification-only layer that closes PROGRESS:113 carry-forward #1 (`RunPodProvider`
real-cloud shape). What was planned as a fixture-capture pass against the
existing offline tests became, on the first live run, the discovery that the
production code had NEVER successfully talked to a real RunPod API — the
offline tests passed against fictional shape because fake `http_get`/`http_post`
seams bypass URL validation, headers, and CSRF. Ten distinct bugs were caught
and fixed on this branch, every one with a regression test against the captured
shape. Layer N's net contribution is therefore far larger than the spec
projected: the provider works against real RunPod for the first time.

- [x] Task 1: Recording HTTP seam + `_load_fixture` + redaction — commits `0dace7a`, `85e7877`, `561e63d`
- [x] Task 2: Placeholder fixture commits + offline-load smoke — commit `059c6ab`
- [x] Task 3: Live smoke YAML + skeleton test + sample init frame — commits `e7ddc20`, `915ab1c`, `66446fc`
- [x] Task 4: USER-GATE live smoke + real fixture capture — commits `8d71eed`, `ff97bb8` + 8 bug-fix commits between
- [x] Task 5: Refactor `test_runpod.py` to load fixtures — commit `198faf4`
- [x] Task 6: Real-shape required-keys + status-mapping lockdown — commit `8be0930`
- [x] Task 7: README + PROGRESS + final gate + merge — commit `a594346`
- [x] Merge to main via `--no-ff` — merge commit `454e514` (closes PROGRESS:114 carry-forward #1)

**First real artifact (RunPod):** pod `ia66l3rlto5x66` on NVIDIA A40 @ $0.35/hr,
ready at T+5s, destroyed at T+10s. Captured fixtures committed at
`tests/providers/fixtures/runpod/*.json` (5 GraphQL responses) +
`last_smoke.json` (artifact metadata). Smoke captured 2026-05-31T20:53:21-0700
at git SHA `7a85d62`. Total cost ≈ $0.001.

**Live-smoke bug catches integrated (10 production fixes):**

1. `83605b8` — URL-encode GraphQL queries; Python 3.13's urllib rejects raw spaces (`InvalidURL`)
2. `7edb10f`+`5c085d7` — Auth header (`Bearer` 403) → query param (`?api_key=` 200)
3. `f026133` — `Content-Type: application/json` required on GETs to bypass RunPod's CSRF block (HTTP 400)
4. `d22f25b` — User-Agent override; RunPod's edge layer blocks the `Python-urllib/*` default (HTTP 403)
5. `45b4a91` — Tolerate `lowestPrice=null` in `find_offers` (was `AttributeError`)
6. `9f63e6b` (part) — GraphQL `env` is an array of `{key, value}` pairs, not a plain dict
7. `9f63e6b` (part) — Detect mutation `errors` block + raise on empty pod id; previously returned `Instance(id="")` leaking paid pods
8. `b694c0b` — Switch ALL GraphQL ops to POST (RunPod GET broken for parameterised queries) + orchestrator `destroy_instance` wraps post-create block (would otherwise leak on any error after create_instance returns)
9. `7a85d62` (part) — Recording seam carries `git_sha` in `_meta` so fixture provenance survives reviewer scrutiny
10. `7a85d62` (part) — `lowestPrice` resolver requires `(input: { gpuCount: 1 })` to return prices; `find_offers` now drops null-priced (unavailable) entries instead of surfacing them as $0 offers

**Key design decisions / deviations from spec:**

- **Smoke pivoted to bare pod lifecycle** (find_offers → create alpine pod → poll ready → destroy) instead of ComfyUI + Wan i2v. The spec called for an MP4 artifact; that was deferred because the original architecture (kinoforge CLI subprocess + in-pytest recording seam) cannot capture fixtures across the process boundary. The bare lifecycle exercises the same 10 production-code paths at $0.001/run vs ~$2/run for engine integration. Engine smoke is a Layer O candidate.
- **Spec convention deviation:** `KINOFORGE_LIVE_RUNPOD` (spec §3) → `KINOFORGE_LIVE_TESTS=1` + per-provider creds (existing fal-live convention).
- **`RUNPOD_TERMINATE_KEY` reuses `RUNPOD_API_KEY`** via `${...}` interpolation in `.env` because RunPod's scoped-key UX has no terminate-only tier. Privilege separation is lost but selfterm fallback still works.

**Test count:** ~756 pre-Layer-N → 778 post-Layer-N (+22 net; mostly +regression tests on the 10 bug catches, +2 lockdown tests in Task 6).

**Out of scope (Layer O candidates):**

- Engine-integration live smoke (ComfyUI/Diffusers/Hosted deployed on a real RunPod pod producing a real MP4)
- Serverless mode read-paths + live smoke (Q3 from Layer N brainstorm was pod-only)
- SkyPilot SDK smoke (PROGRESS:113 carry-forward #2)
- S3/GCS medium-fidelity tests (PROGRESS:113 carry-forward #3)
- Streaming per-entry log lines in `kinoforge batch` (PROGRESS:158 deferred from Layer L Task 4)

### Phase 25 — Layer O (user-facing output directory)

UX-only layer that closes the operator findability + persistence gap
identified during the Layer-N retro: final clips were buried under
`.kinoforge/<run_id>/<engine-derived-name>` with names that mean nothing
at a glance, and the default `--run-id="run"` silently overwrote prior
runs.

- [x] Task 1: `outputs/base.py` (Protocol + slugify + format_filename) + `outputs/__init__.py` (registry) + 12 slugify tests — commit `3f621e9`
- [x] Task 2: `outputs/local.py` (LocalOutputSink with atomic write + collision suffix + self-register) + 10 tests — commit `a58df43`
- [x] Task 3: `OutputConfig` pydantic block + `Config.output` field + 3 round-trip tests — commit `3af17d8`
- [x] Task 4: `GenerateClipStage` sink + namespace integration + 4 stage tests — commit `9d22694`
- [x] Task 5: `orchestrator.generate()` sink threading + 2 tests — commit `e845443`
- [x] Task 6: `batch.batch_generate()` sink + batch_id namespace + 2 tests — commit `3e66a72`
- [x] Task 7: CLI `--output-dir`/`--no-output-dir` mutex group + `_build_sink` + `--run-id` uniquification + 5 tests — commit `0f135de`
- [x] Task 8: `.gitignore` `output/` + commented `output:` block on every example YAML + 6 round-trip tests — commit `503b3a8`
- [x] Task 9: README "Output directory" section + this PROGRESS entry + invariant verification — commit `646adf7`
- [ ] Task 10: Full gate + `--no-ff` merge to main — commit _PENDING_

**Key design decisions:**
- Publish step layered on top of ArtifactStore (Q2=A): zero behavior change to existing call sites; store/ledger/uri_for/gc untouched.
- ASCII-conservative slug (Q3=A): emoji/CJK/accents dropped, not transliterated; cross-platform safe, shell-friendly.
- Flat single + batch-nested layout (Q4=A): single-clip runs land directly in `output/`; batch runs nest under `output/<batch_id>/`.
- `--run-id` default uniquification folded into Layer O: one-line CLI change closes the silent-overwrite foot-gun on the internal store side too.
- Bytes-only v1: hardlink optimization (`ArtifactStore.local_path_for`) deferred; sub-GB mp4 disk doubling is negligible.

**Breaking changes:**
- `kinoforge generate` default `--run-id` flipped from `"run"` to `f"run-{ts}"`. Scripts that grep `.kinoforge/run/` no longer find clips; pass `--run-id run` to restore prior behavior. Second breaking change after the Layer C `kinoforge gc --config PATH` precedent.

**Test count:** ~778 pre-Layer-O → ~823 post-Layer-O (+45 net: 12 slugify + 11 local incl. hash-exhausted regression + 3 cfg + 4 stage + 2 orch + 3 batch + 6 CLI + 6 examples). Numbers will be backfilled with the actual delta in Task 10.

**Out of scope (Layer P+ candidates):**
- Hardlink / zero-copy via `ArtifactStore.local_path_for`.
- Cloud-native sinks (S3 mirror, webhook POST).
- Filename template customization.
- Migration of existing `.kinoforge/<run_id>/*.mp4` into `output/`.
- `Artifact.published_path` field for CLI status / batch summary.
- Engine integration on real RunPod (original Layer-O candidate; now reslotted as Layer P).
