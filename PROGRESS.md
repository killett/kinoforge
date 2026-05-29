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
Execution started. Tasks 1–9 complete. Phases 1–2 complete. Phase 3 in progress (Task 10 next).

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
- [x] Phase 3 (partial): Tasks 8–9 complete.
  - [x] Task 8: FakeEngine + FakeBackend (`src/kinoforge/engines/__init__.py`, `src/kinoforge/engines/fake/__init__.py`, `tests/engines/test_fake.py`) — commit dfdb9cf. Deterministic GPU-free engine/backend: sha256-derived `Artifact.filename`, injectable probe profile, `declared_flags_map`, `required_spec_keys`-gated `validate_spec`, `profile_for` deferred to Task 12, self-registers under `"fake"` on import. 17/17 tests pass.
  - [x] Task 9: LocalProvider + injectable clock (`src/kinoforge/core/clock.py`, `src/kinoforge/providers/__init__.py`, `src/kinoforge/providers/local/__init__.py`, `tests/core/test_clock.py`, `tests/providers/__init__.py`, `tests/providers/test_local.py`) — commit 5c8bbbb. Clock protocol (runtime_checkable) + RealClock + FakeClock(start, advance, ValueError on negative); LocalProvider(ComputeProvider) with synthetic offers (2 LOCAL offers), filter_offers delegation, full lifecycle (create/get/list/stop/destroy/heartbeat), idempotent destroy, last_heartbeat accessor, endpoints returning local://id, self-registration under "local". 18/18 tests pass.
- [ ] Phase 3 (remaining): provisioner + e2e vs fake
- [ ] Phase 4: profiles + strategy decision point + pool/SequentialPool + GenerateClipStage + local ArtifactStore
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
Task 10: Provisioner — shared deploy/teardown steps + engine delegation; wires LocalProvider
and FakeEngine together for an end-to-end offline test.
