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
- `RunPodProvider.find_offers` REST shape is a stub — GPU-types JSON shape needs real-key validation.
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
Layer H (cross-process discovery lock) complete. All 8 commits + docs shipped (555 → ~595 tests).
**Next: choose from open GitHub issues #2 (audio sync), #4 (keyframe stage), #8 (HF bare-repo listing), #9 (aria2c fast-path).**

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
