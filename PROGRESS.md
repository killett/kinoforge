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
- Deferred (interface + 1 path only, layers NOT built): splitter, stitching, audio, concurrent pool, keyframe stage, S3/GCS, cross-process discovery lock.
- Deps stdlib-first: pydantic + PyYAML runtime; skypilot optional/lazy; urllib for all HTTP; stdlib logging.
- TDD red-first, fully offline (LocalProvider/FakeProvider/FakeSource/FakeEngine + injectable clock). No real cloud/net/GPU/weights in any test.

## Single next action
**Layer D (.env secrets loader) complete.** All acceptance criteria met:
`pixi run pre-commit run --all-files` clean; `pixi run test` reports
450 passed (440 prior + 8 dotenv unit tests + 2 CLI integration tests);
mypy strict clean across 89 source files; `kinoforge --env-file PATH ...`
flag works; default `./.env` auto-loads at CLI startup; shell values
always win over `.env`. `.env` finally added to `.gitignore` (was missing).
.env.example checked in with the 4 documented kinoforge credential
variables (FAL_KEY, CIVITAI_TOKEN, HF_TOKEN, RUNPOD_API_KEY).

**Next: pick from the layered roadmap.** Three plausible next layers:

1. **ComfyUI / Diffusers / Hosted `extract_last_frame` implementations**
   (no GitHub issue yet; smaller per-engine follow-ups). Worth doing
   before the first real-cloud user trips the post-Layer-B
   `NotImplementedError` on a multi-segment non-native run. Requires
   per-engine decisions on extraction mechanism (PIL? ffmpeg via
   engine's own runtime? hosted-API endpoint?).

2. **Layer #4 — Concurrent backend scheduler (GitHub issue #3).**
   Drop-in `ConcurrentPool` behind the existing `BackendPool` ABC. Pure
   dispatch concern; no other modules touched.

3. **Layer #5 — Keyframe / image-generation upstream Stage (GitHub
   issue #4).** Composable with the splitter via `segments_override`.
   Forces the engine-kind ADR (image-generation engines vs
   video-generation engines on the same `kind` axis vs split axes).

Begin the chosen layer with the
`superpowers-extended-cc:brainstorming` skill.

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
