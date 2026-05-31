# Layer I — fal.ai adapter + UX A + hosted hardening (Design Spec)

**Status:** Draft (brainstorm-validated 2026-05-31)
**Branch:** `build/layer-i`
**Predecessors:** PROGRESS.md Phase 18 (Layer H, merged 2026-05-31)
**Closes:** First "real-public-provider" run gap surfaced during the Option-B empirical smoke-test sprint.

---

## 1. Motivation

The Option-B 10-minute smoke-test sprint surfaced four confirmed bugs and one architectural gap that block any real first-run:

| # | Bug / gap | State |
|---|---|---|
| 1 | `provisioner.provision()` forwarded pydantic `Config` to `engine.provision(cfg: dict)` → `AttributeError: 'Config' object has no attribute 'get'`. | **Fixed at `e78cafc` on `main`.** Diffusers + ComfyUI provision paths still need regression coverage. |
| 2 | `examples/configs/hosted.yaml` uses relative endpoint `/fal-ai/...` — `urllib` rejects. | Open. |
| 3 | `examples/configs/hosted.yaml` missing `api_key_env` + `health_url`. | Open. |
| 4 | `orchestrator.generate()` bypasses `engine.provision()` entirely — no cred or health preflight. | Open. UX A chosen. |
| 5 | `HostedAPIEngine` server contract matches NO real public provider (fal, Replicate, HF). The engine is a "user-deployed shim" path only. | Open. Addressed by introducing per-provider sibling engines, starting with `FalEngine`. |
| 6 | `declared_flags_map` empty for fake + hosted default keys → noisy WARNING on every fresh-cache generate. | Open. |
| 7 | `AuthError(f"missing {key_name}")` prints `missing ` (empty key_name) when `api_key_env` is unset. | Open. |

Layer I closes Bugs 1 (carry-forward coverage), 2, 3, 4, 6, 7 and ships the first per-provider sibling engine (`FalEngine`) to address gap 5.

## 2. Scope

### In scope

- Diffusers + ComfyUI provisioner-cfg regression tests (lock down Bug 1's blast radius).
- Pydantic validators on `HostedEngineConfig.api_key_env` (non-empty), `HostedEngineConfig.endpoint` (must be absolute `http://` or `https://` URL).
- `examples/configs/hosted.yaml` rewrite as the documented "user-deployed shim" example.
- New `FalEngineConfig` pydantic block with validators.
- New `FalEngine` + `FalBackend` sibling engine targeting fal.ai's queue API.
- New `examples/configs/fal.yaml`.
- UX A: `orchestrator.generate()` calls `engine.provision()` lazily on first use.
  - Hosted path: rerun every generate (cheap; ~100–500 ms one HTTP GET).
  - Compute path: persistent per-instance marker at `<state-dir>/instances/<instance_id>/.provisioned`; staleness keyed by `capability_key`.
  - RMW safety via `store.acquire_lock("provision:<instance_id>")` (Layer H primitive).
- `declared_flags_map` defaults populated for `FakeEngine` and `HostedAPIEngine`; profile-cache fresh-discovery WARNING downgraded to DEBUG.
- `AuthError` runtime message clarity (defense-in-depth) — primary fix is the pydantic validator.
- `_adapters.py` registers the new fal engine; `test_core_invariant.py` allowlist extends.
- Opt-in live test `tests/live/test_fal_live.py` gated by `KINOFORGE_LIVE_TESTS=1` + `FAL_KEY`.
- README "Real providers" / Credentials section update.
- PROGRESS.md Phase 19 entry.
- One manual end-to-end smoke run producing a real video artifact from fal.ai; first-real-artifact filename + capability_key recorded in PROGRESS.

### Out of scope (carry forward)

- Replicate, HF Inference, and other public-provider sibling adapters (each is its own future layer).
- `RunPodProvider.find_offers` real REST shape validation (pre-existing PROGRESS-flagged gap).
- SkyPilot live `sky` SDK exercise; S3 / GCS real-cloud verification.
- ComfyUI / Diffusers real-server bring-up (image build, node compat sweep).
- Audio sync stage (#2), keyframe stage (#4), HF bare-repo listing (#8), aria2c fast-path (#9).

## 3. Architecture

### 3.1 `FalEngine` — sibling, not subclass

Per brainstorming decision Q2, the fal.ai adapter is a standalone engine that **composes** shared helpers (`set_by_dot_path`, `find_asset`, dot-walker) rather than inheriting from `HostedAPIEngine`. This isolates fal-specific wire shapes and avoids coupling future per-provider adapters (Replicate, HF) to `HostedAPIEngine`'s shim contract.

Module layout:

```
src/kinoforge/engines/fal/
  __init__.py       # FalEngine + FalBackend, self-registers under "fal"
  wire.py           # internal: pure HTTP-shape helpers (status URL builder, body builder,
                    #           result URL extraction, status-string interpretation)
tests/engines/test_fal.py
tests/engines/test_fal_wire.py
```

`FalEngine`:
- `name = "fal"`
- `requires_compute = False`
- `requires_local_weights = False`
- `provision(instance=None, cfg: dict) -> None`:
  1. `instance` must be `None` (raise `KinoforgeError` otherwise — mirrors `HostedAPIEngine`).
  2. Resolve `engine.fal.api_key_env` (default `"FAL_KEY"`) via injected `CredentialProvider`. Missing → `AuthError("engine.fal.api_key_env is missing in env")`.
  3. Optional health probe: if `engine.fal.health_url` is non-empty, `http_get(health_url)`; on failure raise `KinoforgeError(f"fal endpoint unreachable: {exc}")`. Empty → skip (fal has no documented health endpoint).
- `backend(instance=None, cfg: dict) -> FalBackend`: build the backend with injected seams.
- `validate_spec(job)`: require `prompt` non-empty in `job.spec`. Other fal model params are model-specific, not enforced here.
- `declared_flags(key)`: copy from `declared_flags_map`. The map is populated with an entry for the shipped `fal.yaml` capability key (see §3.4). For unmapped keys, return `{}` (matches existing engine convention — `JsonProfileCache.discover` uses the empty map; the WARNING-vs-DEBUG fix in §3.4 keeps unmapped-key noise out of the fresh-discovery path).

`FalBackend`:
- Constructor injects: `endpoint: str` (path, e.g. `"fal-ai/wan/v2.2/t2v"`), `queue_base: str` (default `"https://queue.fal.run"`), `api_key: str`, `url_path: str`, `asset_paths: dict[str, str]`, `http_post`, `http_get`, `sleep`, `clock`, `max_poll: int = 600`.
- `submit(job) -> str`:
  - Build body: copy `job.spec`, then for each `(role, dot_path)` in `asset_paths`, look up `find_asset(job, role)` and `set_by_dot_path(body, dot_path, asset.ref.uri)`. Silently skip absent roles.
  - `POST {queue_base}/{endpoint}` with `Authorization: Key {api_key}` header.
  - Response: `{"request_id": "...", "status_url": "...", "response_url": "..."}`.
  - The Pool contract requires `submit -> str`, so the backend keeps a private `self._jobs: dict[str, dict[str, str]]` map keyed by `request_id` that stores `{"status_url": ..., "response_url": ...}` for `result()` to consult. The string returned to the pool is the `request_id`.
- `result(job_id) -> Artifact`:
  - Poll `status_url` (or `f"{queue_base}/{endpoint}/requests/{job_id}/status"` as fallback) with `Authorization: Key {api_key}`.
  - Status interpretation:
    - `"IN_QUEUE"` or `"IN_PROGRESS"` → `self.sleep(1.0)`, loop.
    - `"COMPLETED"` → break.
    - `"FAILED"` → raise `KinoforgeError(f"fal job {job_id} failed: {logs}")`.
    - Any other → raise `KinoforgeError(f"fal job {job_id} unknown status: {status}")`.
  - After `max_poll` iterations without `"COMPLETED"`, raise `TimeoutError`.
  - On `"COMPLETED"`: `GET response_url` (or fallback construction). Extract result URL via `_walk_dot_path(data, url_path)`.
  - Return `Artifact(filename=basename(url), url=url, meta={"request_id": job_id})`.
- `endpoints() -> {"queue": f"{queue_base}/{endpoint}"}`.
- `extract_last_frame(artifact)`: delegate to `ffmpeg_last_frame` (Layer E shared helper), with `http_get_bytes` seam for fetching `artifact.url` first.

### 3.2 UX A — provision self-check inside `generate()`

`orchestrator.generate()` gains two preflight steps:

**Step 2.5 — Hosted preflight (every call):**
```python
if not resolved_engine.requires_compute:
    resolved_engine.provision(None, cfg_dict)
```
Runs unconditionally on every `generate` call when `requires_compute=False`. Latency: one HTTP GET when `health_url` set, else just env lookup (~µs). No persisted state.

**Step 4.5 / 7.5 — Compute preflight (per-instance marker):**

After `create_instance` returns a ready instance:

```python
marker = state_dir / "instances" / instance.id / ".provisioned"
current_key = key.derive()
with store.acquire_lock(f"provision:{instance.id}", ttl_s=300):
    record = read_marker(marker)
    if record is None or not is_marker_current(record, current_key):
        provisioner.provision(
            engine=resolved_engine,
            cfg=cfg,
            instance=instance,
            creds=creds or EnvCredentialProvider(),
            download_dir=state_dir / "weights",
        )
        write_marker(marker, instance.id, current_key, resolved_engine.name, time.time())
```

`creds` is the optional argument already on `generate()`; falls back to the `EnvCredentialProvider()` the CLI uses today.

**Marker shape** (JSON):
```json
{
  "instance_id": "i-abc123",
  "capability_key": "<64-hex>",
  "engine": "comfyui",
  "timestamp": 1717200000.0
}
```

**Staleness rule:** marker is stale iff its `capability_key` ≠ current `cfg.capability_key().derive()`. Catches "user edited the model set or config and reused the instance" → forces re-provision (re-clone nodes, re-route weights).

**Concurrency:** the outer `store.acquire_lock("provision:<instance_id>", ...)` wraps the read-then-write so two concurrent generates on the same instance lock-serialize. Reuses Layer H primitives.

**State-dir layout addition:**
```
.kinoforge/
  ledger.json
  weights/
  instances/                    # NEW
    <instance_id>/
      .provisioned              # NEW marker
  run/
```

**New helpers** (`src/kinoforge/core/provision_state.py`):
- `marker_path(state_dir: Path, instance_id: str) -> Path`
- `read_marker(path: Path) -> dict | None` (None if absent OR corrupt JSON OR missing required keys)
- `write_marker(path: Path, instance_id: str, capability_key: str, engine_name: str, timestamp: float) -> None`
- `is_marker_current(marker: dict, capability_key: str) -> bool`

### 3.3 Pydantic validator hardening

`HostedEngineConfig` (existing):
- `api_key_env: str` — `@field_validator` rejects empty string.
- `endpoint: str` — `@field_validator` requires `startswith(("http://", "https://"))`.
- `health_url: str = ""` — optional; empty signals "skip health probe".

`FalEngineConfig` (new):
- `endpoint: str` — required non-empty.
- `queue_base: str = "https://queue.fal.run"` — must start with `http://` or `https://`.
- `api_key_env: str = "FAL_KEY"` — must be non-empty (default satisfies validator).
- `url_path: str` — required non-empty.
- `asset_paths: dict[str, str] = {}` — optional.
- `health_url: str = ""` — optional.

`EngineConfig`:
- Add `fal: FalEngineConfig | None = None`.
- Extend the existing `kind`-vs-block model-validator to require `engine.fal` when `engine.kind == "fal"`.

### 3.4 Cosmetics

**Bug 6 — `declared_flags` WARNING:**
- In `JsonProfileCache.discover()`, log at `DEBUG` when both flags absent on the fresh-discovery path (the probe is the source of truth, declared flags are a cross-check that doesn't yet apply).
- In `JsonProfileCache.verify()`, retain the `WARNING` log when declared flags disappear (signals stale cache vs. live engine drift).
- Populate `FakeEngine.declared_flags_map[("fake-base", "fp16")] = {"supports_native_extension": False, "supports_joint_audio": False}`.
- Populate `HostedAPIEngine.declared_flags_map[("hosted", "")] = {"supports_native_extension": False, "supports_joint_audio": False}`.

**Bug 7 — `AuthError` message clarity:**
- Primary: pydantic validator catches empty `api_key_env` at config load with `ValidationError`.
- Defense-in-depth: `HostedAPIEngine.provision()` and `FalEngine.provision()` check `if not key_name: raise AuthError(f"engine.{kind}.api_key_env is empty — set the env var name in your config")` instead of the f-string with an empty value.

## 4. Data flow

### 4.1 First-time hosted generate (UX A)

```
$ kinoforge --env-file .env generate -c examples/configs/fal.yaml --prompt "a cat" --mode t2v

orchestrator.generate
  ├─ resolve_engine                            -> FalEngine
  ├─ engine.requires_compute is False
  ├─ STEP 2.5: engine.provision(None, cfg_dict)
  │     ├─ creds.get("FAL_KEY")                -> "abc123..."
  │     └─ skip health probe (empty health_url)
  ├─ profile_provider.resolve(key)             -> ProfileNotCached
  │     └─ backend = engine.backend(None, cfg_dict)
  │     └─ profile_provider.discover(...)      -> writes profile.json
  ├─ STEP 5: validate_request(...)
  ├─ STEP 6: splitter.split(...)
  ├─ STEP 7: backend already exists
  ├─ STEP 8: skip verify (just-discovered)
  └─ STEP 9: stage.run(request)
        └─ FalBackend.submit(job)
              ├─ POST queue.fal.run/fal-ai/wan/v2.2/t2v
              └─ -> request_id="r-xyz"
        └─ FalBackend.result("r-xyz")
              ├─ poll status_url -> IN_QUEUE -> IN_PROGRESS -> COMPLETED
              ├─ GET response_url
              └─ -> Artifact(url="https://v3.fal.media/...mp4")
        └─ stage persists to store
```

### 4.2 First-time compute generate (UX A compute path)

```
orchestrator.generate
  ├─ resolve_engine                            -> ComfyUIEngine
  ├─ engine.requires_compute is True           -> resolve_provider
  ├─ profile_provider.resolve(key)             -> ProfileNotCached
  │     ├─ provider.create_instance(spec)      -> instance i-abc123 (ready)
  │     ├─ STEP 4.5: provision-once
  │     │     ├─ acquire_lock "provision:i-abc123"
  │     │     ├─ read_marker -> None
  │     │     ├─ provisioner.provision(engine, cfg, instance, ...)
  │     │     │     ├─ resolve refs via sources
  │     │     │     ├─ downloader(artifacts, ...)
  │     │     │     ├─ post_provision_hook
  │     │     │     └─ engine.provision(instance, cfg_dict)
  │     │     │           ├─ git-clone custom nodes
  │     │     │           ├─ pip install requirements
  │     │     │           ├─ route weights
  │     │     │           └─ launch ComfyUI
  │     │     ├─ write_marker(path, instance.id, key_hex, "comfyui", now)
  │     │     └─ release_lock
  │     └─ backend = engine.backend(instance, cfg_dict)
  ├─ ...
```

### 4.3 Second-call compute generate (cached + provisioned)

```
orchestrator.generate
  ├─ profile_provider.resolve(key)             -> profile hit
  ├─ STEP 7: backend is None
  │     ├─ provider.create_instance(spec)      -> instance i-abc123 (reused)
  │     ├─ STEP 7.5: provision-once
  │     │     ├─ acquire_lock "provision:i-abc123"
  │     │     ├─ read_marker -> {"capability_key": "<same hex>", ...}
  │     │     ├─ is_marker_current -> True
  │     │     ├─ SKIP provisioner.provision
  │     │     └─ release_lock
  │     └─ backend = engine.backend(instance, cfg_dict)
  ├─ STEP 8: verify (cache-hit)
  └─ STEP 9: stage.run
```

## 5. Error handling

| Error | Source | Raised when | Caught at |
|---|---|---|---|
| `pydantic.ValidationError` | `Config.load_config` | empty `api_key_env`, relative `endpoint`, missing required fal field | CLI `main()` — exit 1 with the validator message |
| `AuthError` | `engine.provision()` | env var absent or named-empty | CLI `main()` — exit 1 with clear "set FAL_KEY in .env" |
| `KinoforgeError("fal endpoint unreachable: …")` | `FalEngine.provision()` health probe | `health_url` HTTP failure | CLI `main()` — exit 1 |
| `KinoforgeError("fal job … failed: …")` | `FalBackend.result()` | fal returns `status: FAILED` | propagates pool → stage → orchestrator → CLI exit 1 |
| `TimeoutError` | `FalBackend.result()` | poll loop hits `max_poll` without `COMPLETED` | CLI `main()` — exit 1 |
| `LockTimeout` | `store.acquire_lock` in compute-marker step | another generate holds the provision lock past ttl | CLI `main()` — exit 1; user re-runs |

Exit codes intentionally collapsed to 1 across all Layer I error paths — matches the existing CLI policy. Distinguishing exit codes by error class is a future cross-cutting concern, not in scope here.

The compute-path marker reader treats corrupt JSON, missing keys, or absent file all as "not provisioned" — never raises. Self-healing on the next provision pass.

## 6. Testing

### 6.1 Offline (default `pixi run test`)

| File | Adds | Tests | Notes |
|---|---|---|---|
| `tests/core/test_provisioner.py` | extends | +2 | Diffusers + ComfyUI receive dict cfg via provisioner. |
| `tests/core/test_config.py` | extends | +6 | Hosted + Fal validator coverage. |
| `tests/core/test_orchestrator.py` | extends | +3 | UX A hosted preflight: provision called every call; AuthError raised before submit; health failure raised before submit. |
| `tests/core/test_provision_state.py` | NEW | +5 | `marker_path` / `read_marker` (corrupt JSON returns None) / `write_marker` / `is_marker_current` / staleness rule. |
| `tests/core/test_orchestrator_compute.py` | NEW | +4 | UX A compute path: first call writes marker; second call same key skips; stale key re-provisions; concurrent generates lock-serialize. |
| `tests/engines/test_fake.py` | extends | +2 | `declared_flags_map` default; fresh-discover has no WARNING. |
| `tests/engines/test_hosted.py` | extends | +2 | Runtime AuthError uses defense-in-depth message; validator rejects empty `api_key_env`. |
| `tests/core/test_profiles.py` | extends | +2 | Fresh-discovery + empty declared_flags logs DEBUG not WARNING; verify path still WARNs. |
| `tests/engines/test_fal_wire.py` | NEW | +8 | Pure helpers in isolation (no I/O). |
| `tests/engines/test_fal.py` | NEW | +12 | Engine + backend with injected HTTP spies. |
| `tests/test_examples.py` | extends | +2 | `fal.yaml` loads + validates; updated `hosted.yaml` loads + validates. |
| `tests/test_core_invariant.py` | extends | +0 | Allowlist regex extension only (`kinoforge.engines.fal` confined to `engines/fal/`). |

**Total**: ~48 new tests. **Target**: 596 → ~644 green.

### 6.2 Live (opt-in)

`tests/live/test_fal_live.py` — gated:

```python
import os
import pytest

if not (os.getenv("KINOFORGE_LIVE_TESTS") == "1" and os.getenv("FAL_KEY")):
    pytest.skip("live tests require KINOFORGE_LIVE_TESTS=1 + FAL_KEY",
                allow_module_level=True)
```

1. `test_fal_provision_real()` — run CLI `kinoforge provision -c examples/configs/fal.yaml`. Assert exit 0.
2. `test_fal_generate_short_t2v_real()` — run CLI `kinoforge generate -c examples/configs/fal.yaml --prompt "a cat sitting on a fence" --mode t2v`. Assert exit 0, artifact file present, MP4 magic bytes at offset 4.

Cost: ~$0.05–$0.20 per run.

### 6.3 Tooling

- `pyproject.toml`: `[tool.pytest.ini_options] markers = ["live: opt-in tests that hit real APIs"]`.
- `pixi.toml`: `test-live = "KINOFORGE_LIVE_TESTS=1 pytest tests/live/ -v"`.
- CI matrix unchanged; live tests skipped silently without env vars.

### 6.4 Manual smoke at end of layer

`kinoforge --env-file .env generate -c examples/configs/fal.yaml --prompt "..." --mode t2v` → real video file. Document the file path + capability_key hex + git SHA in PROGRESS Phase 19 as "first real artifact" milestone.

## 7. Task sequencing

12 tasks. Critical path: 1 → 4 → 5 → 6 → 8 → 9 → 10 → 11 → 12. Tasks 2, 3, 7 parallelizable.

| # | Task | Files | Deps | Rationale |
|---|---|---|---|---|
| 1 | Diffusers + ComfyUI provisioner-cfg regression tests | `tests/core/test_provisioner.py` | none | Lock down Bug 1's full blast radius first. |
| 2 | `declared_flags` WARNING → DEBUG on fresh-discovery path | `core/profiles.py`, `tests/core/test_profiles.py` | none | Independent. Quiets local-fake. |
| 3 | `FakeEngine` + `HostedAPIEngine` `declared_flags_map` defaults | `engines/fake/__init__.py`, `engines/hosted/__init__.py`, `tests/engines/test_fake.py`, `tests/engines/test_hosted.py` | 2 | Completes Bug 6. |
| 4 | Pydantic validators on `HostedEngineConfig` | `core/config.py`, `tests/core/test_config.py` | none | Closes Bug 7 primary fix; catches Bug 2 + 3 at load. |
| 5 | `HostedAPIEngine` runtime `AuthError` defense-in-depth | `engines/hosted/__init__.py`, `tests/engines/test_hosted.py` | 4 | Bug 7 defense-in-depth. |
| 6 | Rewrite `examples/configs/hosted.yaml` (absolute URL + new fields + shim contract docs) | `examples/configs/hosted.yaml`, `tests/test_examples.py` | 4 | Must come after validators. |
| 7 | `core/provision_state.py` + tests | `core/provision_state.py`, `tests/core/test_provision_state.py` | none | Foundation for UX A compute path. |
| 8 | UX A hosted path — orchestrator preflight | `core/orchestrator.py`, `tests/core/test_orchestrator.py` | 4, 5 | Needs validator-clean config + cleaned AuthError. |
| 9 | UX A compute path — marker + acquire_lock wrap | `core/orchestrator.py`, `tests/core/test_orchestrator_compute.py` | 7, 8 | Builds on provision_state + hosted UX A pattern. |
| 10 | `FalEngineConfig` pydantic block + validators | `core/config.py`, `tests/core/test_config.py` | 4 | Foundation for tasks 11–12. |
| 11 | `FalEngine` + `FalBackend` + wire helpers + tests | `engines/fal/__init__.py`, `engines/fal/wire.py`, `tests/engines/test_fal.py`, `tests/engines/test_fal_wire.py` | 8, 10 | Needs UX A hosted pattern in place. |
| 12 | `_adapters.py` + `examples/configs/fal.yaml` + invariant allowlist + live test + README + PROGRESS + pixi/pyproject markers | `_adapters.py`, `examples/configs/fal.yaml`, `tests/test_core_invariant.py`, `tests/live/test_fal_live.py`, `README.md`, `PROGRESS.md`, `pyproject.toml`, `pixi.toml` | 11 | Final wiring + docs. Manual smoke after. |

## 8. Pre-merge gates

1. `pixi run pre-commit run --all-files` clean.
2. `pixi run test` ~644 green.
3. `pixi run typecheck` clean.
4. Manual smoke produces a real fal.ai artifact; details recorded in PROGRESS.
5. Two-stage review (spec compliance, then code quality).

## 9. Merge commit template

```
Merge branch 'build/layer-i': fal.ai adapter + UX A + hosted hardening (Layer I)

- FalEngine: first real public-provider adapter (queue API)
- UX A: orchestrator.generate() preflights engine.provision() (cred + health)
- HostedAPIEngine: pydantic validators, declared_flags noise removed, AuthError clarity
- examples/configs/hosted.yaml rewritten; examples/configs/fal.yaml added

Tests: 596 -> ~644 offline + 2 opt-in live tests gated by
KINOFORGE_LIVE_TESTS=1 + FAL_KEY.

First real artifact produced: <filename>, capability_key <hex>.

Closes <GitHub issue # for "first real public-provider adapter"; open a new
issue if none exists when this layer ships, with a one-paragraph description
linking to this design doc>.
```
