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
Execution started. Task 1 complete. Continuing Phase 1.

## Task checklist (high-level; plan refines into 28 bite-sized tasks)
- [x] Read SPEC.md, explore project context
- [x] Resolve open design questions (8 decisions locked — see DESIGN.md §1)
- [x] Write + commit DESIGN.md
- [x] Design review gate — approved
- [x] Write + commit implementation plan + native tasks + tasks.json
- [ ] Phase 1: interfaces + registry + config model + tests (Tasks 1–4)
  - [x] Task 1: Core interfaces, errors, structured logging (`src/kinoforge/core/{__init__,errors,interfaces,logging}.py`, `tests/core/test_interfaces.py`) — commit e636df4
- [ ] Phase 2: downloader + HTTP source
- [ ] Phase 3: GenerationEngine iface + FakeEngine + provisioner + LocalProvider (e2e vs fake)
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
Task 2: Registry (name + scheme routing). Create `src/kinoforge/core/registry.py` with
`register_provider(name, cls)`, `register_source(scheme, cls)`, `register_engine(name, cls)`,
`get_provider(name)`, `get_source(scheme)`, `get_engine(name)` — all typed, raising `UnknownAdapter`
on miss. Tests first in `tests/core/test_registry.py`.
