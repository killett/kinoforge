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
Execution started. Tasks 1–15 complete. Phases 1–3 complete. Phase 4 in progress (Task 15 done, Task 16 next).

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
- [ ] Phase 5: cost-safety (timers, sweeper, ledger, teardown, budget) vs LocalProvider+clock
- [ ] Phase 6: CivitAI + HuggingFace sources
- [ ] Phase 7: ComfyUI engine (+node installer) + RunPodProvider (pod+serverless)
- [ ] Phase 8: DiffusersEngine + HostedAPIEngine (no-compute) + SkyPilotProvider
- [ ] Phase 9: CLI + examples + README + CI (3-OS)

## Key decisions & gotchas
- Core NEVER imports a concrete provider/source/engine — registry-mediated by name/scheme. Reviewer enforces.
- 8 open questions resolved in DESIGN.md §1 (submit/result+Pool, models-per-engine, params-vs-spec, profile-cache location, serverless caps, artifact GC, role vocab, under-use warning).
- Discovery ordering is explicit & guaranteed (resolve→validate→split→provision→verify); fail-hard on drift tears down compute.
- Cost-safety: invariant universal, mechanism provider-specific. RunPod in-pod self-terminator + least-privilege terminate-only cred; SkyPilot native autostop; LocalProvider injectable clock for tests.
- Deferred (interface + 1 path only, layers NOT built): splitter, stitching, audio, concurrent pool, keyframe stage, S3/GCS, cross-process discovery lock.
- Deps stdlib-first: pydantic + PyYAML runtime; skypilot optional/lazy; urllib for all HTTP; stdlib logging.
- TDD red-first, fully offline (LocalProvider/FakeProvider/FakeSource/FakeEngine + injectable clock). No real cloud/net/GPU/weights in any test.

## Single next action
Task 16: Orchestrator (deploy + generate flows, discovery ordering, fail-hard teardown, dry-run).
