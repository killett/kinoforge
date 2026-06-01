# Layer M — hosted-YAML collapse + Authorization-header passthrough

**Date:** 2026-05-31
**Branch:** `build/layer-m`
**Closes:** PROGRESS:155 follow-ups #1 (Authorization header) and #2 (hosted-YAML model duplication).

## 1. Motivation

Two known design defects survive on `main` after Layer L:

1. **Hosted-YAML model duplication.** `engine.hosted.model` (cache identity, fed to
   `HostedAPIEngine.key_base(cfg)`) and `spec.model` (wire body, read by
   `HostedAPIBackend.submit`) hold the same string in every shipped hosted YAML.
   `examples/configs/hosted.yaml` lines 41–46 explicitly tells users "keep these in
   sync" — a documented footgun. Silent drift between the two pollutes the
   ModelProfile cache: a stale `engine.hosted.model` paired with a corrected
   `spec.model` will discover a profile for one model and apply it to inference
   results from another.
2. **`Artifact.headers` silently dropped on download.** `Artifact` carries
   `headers: dict[str, str]` (`core/interfaces.py:103`) but
   `GenerateClipStage._artifact_bytes` calls `urllib.request.urlopen(url)` at
   `pipeline/generate_clip.py:187` with no custom headers. Any hosted/queue
   engine returning a media URL behind an `Authorization: Bearer …` gate
   (RunwayML, Pika, self-hosted shims) cannot be wired without first patching
   the pipeline. The shipped fal smoke succeeds today only because fal returns
   short-lived signed URLs that do not require auth on GET.

Both items were flagged as carry-forward in PROGRESS:155 but not scheduled.
Layer M closes them as a single small footgun-closure layer. Real-cloud
verification work (RunPod find_offers shape, SkyPilot SDK smoke, S3/GCS
medium-fidelity tests) is deferred to a separate Layer N.

## 2. Scope decisions (locked)

| # | Decision | Why |
|---|---|---|
| Q1 | Layer M scope = footgun closure only (no cloud-fidelity hardening). | Design defects compound; bugs don't. Fix design first so every hosted YAML written after Layer M encodes the right mental model. |
| Q2 | Collapse strategy: drop `engine.hosted.model`; `spec.model` wins. | Cache identity IS the wire-body model for hosted engines — they cannot diverge meaningfully. Single source of truth at the YAML layer AND the pydantic layer. |
| Q3 | Header fix shape: `Artifact.headers` passthrough + injectable `http_get_bytes` seam on `GenerateClipStage`. | Mirrors PROGRESS:87 "injected I/O seam on every adapter" pattern; tests get a spy without monkeypatching urllib. |
| Q4 | Migration: hard cut + docs. | Matches the `kinoforge gc --config` precedent (PROGRESS:135). Soft deprecation drags the smell through one more cycle for zero functional gain. |
| Q5 | Retrofit `HostedAPIEngine` to populate `Artifact.headers`; skip Fal. | Gives the new seam a real consumer in Layer M so it is exercised end-to-end, not just by unit-level spies. Fal stays as-is (signed URLs work). |

## 3. Architecture

Two-track surgical layer; both tracks land same branch `build/layer-m`. Order:
Track A first (config + cache identity), Track B second (pipeline seam +
engine retrofit). Tracks are independent at the file level except for a
shared edit to `src/kinoforge/engines/hosted/__init__.py`.

### Track A — hosted-YAML collapse

- `HostedEngineConfig` (in `src/kinoforge/core/config.py`) loses its required
  `model: str` field. A `model_validator(mode="before")` intercepts the raw
  dict and raises a `ValueError` with the guiding message
  `"engine.hosted.model is no longer supported; move the value to top-level spec.model"`
  when the stale key is present. `model_config = ConfigDict(extra="forbid")`
  is added as a backstop so any OTHER future stale field on this block
  surfaces at load instead of being silently dropped.
- `HostedAPIEngine.key_base(cfg)` (in `src/kinoforge/engines/hosted/__init__.py`)
  reads `cfg.get("spec", {}).get("model", "")` instead of
  `cfg["engine"]["hosted"]["model"]`. Empty result raises `ConfigError`
  guiding the user to set `spec.model` at the top level.
- `examples/configs/hosted.yaml` deletes the `engine.hosted.model` line and
  the "keep these in sync" comment block.

Spec/engine boundary preserved: `spec` remains the permissive
`dict[str, Any]` set by Layer K Q3=A. The pydantic layer never gains
hosted-specific knowledge about the `"model"` key — that knowledge lives in
the engine's `key_base` only.

### Track B — Authorization header passthrough

- `GenerateClipStage.__init__` (in `src/kinoforge/pipeline/generate_clip.py`)
  gains an optional `http_get_bytes: Callable[[str, dict[str, str]], bytes] | None`
  constructor param. Default is a small module-level helper that builds a
  `urllib.request.Request(url, headers=headers).read()`.
- `GenerateClipStage._artifact_bytes` replaces the inline `urlopen` call
  in the http(s) branch with `self._http_get_bytes(url, artifact.headers)`.
  The file/uri and synthesize-fallback branches are unchanged.
- `HostedAPIBackend.result()` populates
  `headers={"Authorization": f"Bearer {self._token}"}` when constructing the
  returned `Artifact`, but only when `self._token` is non-empty. Empty token
  yields an empty headers dict (preserves today's behavior for shims that
  serve unauthenticated artifact URLs).
- `core/orchestrator.py` does NOT pass `http_get_bytes` when constructing
  `GenerateClipStage` — production code keeps using the default seam.

No new files. No new dependencies. No ABC surface change. No
core-import-ban impact (the seam lives in `pipeline/`, not `core/`, and
takes no vendor imports).

## 4. Components

### Track A — files touched

| Path | Change |
|---|---|
| `src/kinoforge/core/config.py` | Delete `model: str` field on `HostedEngineConfig`. Add `model_validator(mode="before")` that raises with the migration guidance when the stale `"model"` key is present in the raw dict. Add `model_config = ConfigDict(extra="forbid")` as a backstop for future stale fields. Update class docstring (remove model attribute line; add migration note). |
| `src/kinoforge/engines/hosted/__init__.py` | `key_base(cfg)` body rewritten to read `cfg["spec"]["model"]`; raises `ConfigError` on empty/missing. Docstring updated. |
| `examples/configs/hosted.yaml` | Delete the `model:` line under `engine.hosted`. Delete the comment block at lines 41–46 about keeping the two in sync. `spec.model` line stays. |

### Track B — files touched

| Path | Change |
|---|---|
| `src/kinoforge/pipeline/generate_clip.py` | Add module-level `_default_http_get_bytes(url, headers) -> bytes` helper using `urllib.request.Request`. Add `http_get_bytes` constructor param + instance attr. Replace `urlopen` call in `_artifact_bytes`. |
| `src/kinoforge/engines/hosted/__init__.py` | `HostedAPIBackend.result()` Artifact construction populates `headers` from `self._token`. |
| `README.md` | New "Breaking changes — Layer M" subsection: hosted YAML must move `engine.hosted.model` value to top-level `spec.model`. |
| `PROGRESS.md` | New Phase 23 entry with task SHAs; PROGRESS:155 follow-ups #1 and #2 marked CLOSED. |

### Untouched (load-bearing)

- `core/interfaces.py` — `Artifact.headers` already exists.
- `core/orchestrator.py` — stage construction signature unchanged at the
  call site (default seam picked up automatically).
- `tests/test_core_invariant.py` — no new vendor imports introduced.
- Every fal/diffusers/comfyui engine — Track A is hosted-only; Track B's
  seam is opt-in via `Artifact.headers` and existing engines pass empty
  dicts, preserving today's wire behavior.

## 5. Data flow

### Track A — capability identity for hosted engines

```
YAML loaded
  Config (pydantic)
    cfg.spec["model"]                = "wan-ai/Wan2.2-T2V-A14B"
    cfg.engine.hosted.{provider, endpoint, api_key_env, ...}    # no model field
orchestrator.generate(cfg)
  cfg.capability_key()                                # unchanged; uses models[].ref base
  engine.key_base(cfg.model_dump())                   # now reads cfg["spec"]["model"]
CapabilityKey.derive()                                # hash includes the same string the wire body uses
ModelProfile cache lookup keyed by hash
```

Wire body path (unchanged):

```
HostedAPIBackend.submit(job)
  body["model"] = job.spec["model"]                   # same string, single YAML source
  POST {endpoint}  body
```

Two paths now read the same value from the same YAML location. Silent drift
becomes impossible.

### Track B — artifact download with auth

```
backend.result(job_id)
  Artifact(
    filename="x.mp4",
    url="https://shim.example.com/media/abc",
    headers={"Authorization": "Bearer …"},            # populated by HostedAPIBackend.result
  )
GenerateClipStage._artifact_bytes(artifact)
  if uri/file present:
    Path.read_bytes()                                 # unchanged
  elif url is http(s):
    self._http_get_bytes(url, artifact.headers)       # NEW: injected seam, headers passed through
      default impl: urllib.request.Request(url, headers=headers).read()
  else:
    synthesize_fallback(filename, meta)               # unchanged (FakeEngine path)
store.put_bytes(run_id, filename, bytes)
```

Shims that serve unauthenticated artifact URLs: `self._token` is empty
string → `Artifact.headers` is `{}` → seam sends a bare GET (today's
behavior). Fal: result path unchanged → `Artifact.headers` is `{}` →
seam sends a bare GET (today's behavior; fal's signed URLs work).

## 6. Error handling

### Track A — hard-cut migration

- pydantic `ValidationError` raised at config load if YAML contains
  `engine.hosted.model`. Message wrapped from a `ValueError` raised by a
  `model_validator(mode="before")` and reads
  `"engine.hosted.model is no longer supported; move the value to top-level spec.model"`.
  Implementation: the `model_validator` is the user-facing error path;
  `model_config = ConfigDict(extra="forbid")` is a backstop that catches
  any OTHER future stale key on this block (e.g. typos) with pydantic's
  default `extra_forbidden` message naming the loc tuple.
- If a YAML passes pydantic load but is missing `spec.model`, the failure
  surfaces at `key_base()` call time as `ConfigError("hosted engine requires
  spec.model at the top level of the YAML config")`. Single chokepoint, single
  error class, no orchestrator-side wiring required.

### Track B — Authorization-header passthrough

- `_default_http_get_bytes` lets `urllib` errors propagate raw (matches
  today's `urlopen` behavior on `pipeline/generate_clip.py:187`). Layer M
  adds no new error wrapping in the pipeline — wrapping is an engine's job
  (per the Layer E pattern).
- Empty `self._token` in `HostedAPIBackend.result()` skips the header
  entirely. The wire never carries `"Authorization: Bearer "` (trailing
  space, no value), which would silently 401 downstream.
- No `Optional` shape drift on the seam: when no override is passed, the
  module-level default is bound at `__init__` time, so
  `self._http_get_bytes` is always callable.

## 7. Testing

Each test names the bug it would catch (test-design skill, PROGRESS:93).
Red-first per track: write the failing test, confirm FAIL, implement,
confirm PASS.

### Track A

| # | File | Test | Bug caught |
|---|---|---|---|
| 1 | `tests/core/test_config.py` | YAML with `engine.hosted.model` → `ValidationError` whose message names both `"engine.hosted.model"` and `"spec.model"`. | Stale field silently dropped → cache identity collapses to `""` → cross-config cache poisoning. |
| 2 | `tests/core/test_config.py` | YAML with only `spec.model`, no `engine.hosted.model` → loads cleanly. | Field removal accidentally requires it elsewhere in the model. |
| 3 | `tests/core/test_config.py` | Round-trip: `cfg.model_dump()["spec"]["model"]` equals the value written in YAML. | pydantic field rename or default elision silently drops the value. |
| 4 | `tests/engines/test_hosted.py` | Replace existing AC1 + AC6: `_BASE_CFG` carries `spec.model` (no `engine.hosted.model`); `engine.key_base(_BASE_CFG) == _MODEL`. | `key_base` reads the wrong dict path → wrong hash. |
| 5 | `tests/engines/test_hosted.py` | `key_base(cfg)` raises `ConfigError` when `cfg["spec"]["model"]` is absent or empty. | Silent empty-string return → cache identity collision across hosted configs. |
| 6 | `tests/test_examples.py` | `examples/configs/hosted.yaml` loads + carries `spec.model`. | Future edit drops `spec.model` from the example. |

### Track B

| # | File | Test | Bug caught |
|---|---|---|---|
| 7 | `tests/pipeline/test_generate_clip.py` | Spy seam receives the exact `Artifact.headers` dict. | Pipeline filters/strips/wraps headers en route. |
| 8 | `tests/pipeline/test_generate_clip.py` | No populated headers → spy receives empty dict, NOT `None`. | Optional-vs-always-dict type drift. |
| 9 | `tests/pipeline/test_generate_clip.py` | `uri=file://…` branch bypasses the seam entirely. | Regression that downloads when a local file exists. |
| 10 | `tests/pipeline/test_generate_clip.py` | Default seam (no override) downloads via stdlib urllib with a `Request` whose `.headers` includes `Authorization` when headers are populated. | Production path skips headers because the seam is only consulted in tests. |
| 11 | `tests/engines/test_hosted.py` | `HostedAPIBackend.result()` Artifact has `headers["Authorization"] == f"Bearer {token}"`. | Token leaks into `meta` or never reaches `headers`. |
| 12 | `tests/engines/test_hosted.py` | Empty `self._token` → `Artifact.headers` is empty dict (no `"Bearer "` key). | `"Bearer "` (trailing space, no value) leaks to wire and silently 401s. |

### Integration / lockdown

| # | File | Test | Bug caught |
|---|---|---|---|
| 13 | `tests/pipeline/test_generate_clip.py` (or new `test_e2e_hosted_auth.py`) | E2E: hosted-style fake engine returns `Artifact(url=…, headers={…})`; orchestrator + stage drive it; spy seam captures the headers; `store.put_bytes` is called with the bytes returned by the spy. | Spy passthrough works in unit tests but orchestrator constructs `GenerateClipStage` without the seam wired. |
| 14 | `tests/test_core_invariant.py` (existing, unchanged) | Already covers the core-import-ban + vendor-SDK confinement scans. | Layer M accidentally drags a vendor import into `core/`. |

### Test count delta

- `+12` net new tests across Track A (6), Track B (5), and integration (1).
- `±2` retrofits to existing AC1 and AC6 in `tests/engines/test_hosted.py`.
- Pre-Layer-M baseline: `743 passed, 1 skipped`. Post-Layer-M expected:
  `~755 passed, 1 skipped`.

### Discipline

- Mocks only at I/O seams (`http_get_bytes` spy; stdlib urllib stub for
  the default-seam test). No mocking of internal logic.
- Tests assert on observable behavior (header contents, error messages,
  artifact roundtrip), never on internal state.
- Red-first per track; two red/green cycles, not one.

## 8. Migration / breaking changes

Hard cut. Users with hosted configs on `main` (post-Layer-L) must edit
their YAML once:

```diff
 engine:
   kind: hosted
   hosted:
     provider: my-shim
     endpoint: "https://shim/inference"
-    model: "wan-ai/Wan2.2-T2V-A14B"
     api_key_env: "MY_SHIM_KEY"
     health_url: "https://shim/health"
     url_path: video.url

 spec:
   model: "wan-ai/Wan2.2-T2V-A14B"
```

Failure mode at load: `ValidationError` from pydantic naming
`engine.hosted.model` as the offending field. README "Breaking changes —
Layer M" subsection carries the same diff.

This matches the migration shape of the Phase 13 `kinoforge gc --config`
breaking change (PROGRESS:135).

## 9. Out of scope

Carried forward to Layer N or later:

- RunPod `find_offers` REST shape validation against a real GraphQL
  response (golden fixture + opt-in live smoke).
- SkyPilot `_get_sky()` lazy-import path exercised against the real `sky`
  SDK.
- `S3ArtifactStore` + `GCSArtifactStore` medium-fidelity testing via
  `moto` and `fake-gcs-server`.
- Streaming per-entry log lines in `kinoforge batch` (PROGRESS:158–169,
  Layer L Task 4 deferral).
- Diffusers / Fal `Artifact.headers` retrofits (not needed; fal uses
  signed URLs; diffusers is local-server so no auth on artifact GET).
- New engines (RunwayML, Pika) — they will populate `Artifact.headers`
  themselves and inherit the Layer M passthrough for free.

## 10. Acceptance criteria

1. `engine.hosted.model` field removed from `HostedEngineConfig`. A
   `model_validator(mode="before")` raises with the migration message
   `"engine.hosted.model is no longer supported; move the value to top-level spec.model"`
   when the stale key is present. `extra="forbid"` is added as a backstop
   for other future stale keys.
2. `HostedAPIEngine.key_base(cfg)` reads `cfg["spec"]["model"]`; empty
   value raises `ConfigError` with a guiding message.
3. `examples/configs/hosted.yaml` no longer contains
   `engine.hosted.model` nor the "keep these in sync" comment.
4. `GenerateClipStage.__init__` accepts an optional
   `http_get_bytes` seam; default uses `urllib.request.Request` with
   headers; instance attribute always callable.
5. `_artifact_bytes` http(s) branch calls the seam with
   `(url, artifact.headers)`; file/uri/synthesize branches unchanged.
6. `HostedAPIBackend.result()` populates `Artifact.headers` with
   `Authorization: Bearer {self._token}` when the token is non-empty;
   otherwise an empty dict.
7. New tests (12) and retrofitted tests (2) all PASS; pre-Layer-M test
   suite (743 + 1 skipped) keeps passing unmodified.
8. `pixi run pre-commit run --all-files` clean.
9. `test_core_invariant.py` continues to pass — no new vendor imports
   in `core/`, no new core imports of `engines/`/`providers/`/`sources/`.
10. README + PROGRESS reflect the breaking change and Phase 23 entry.

## 11. Phase ordering

Layer M = Phase 23. Single feature branch `build/layer-m`. Tasks
(plan to be authored by writing-plans skill):

1. Track A — `HostedEngineConfig` field drop + `extra="forbid"` + tests 1–3.
2. Track A — `HostedAPIEngine.key_base` rewrite + `ConfigError` + tests 4–5.
3. Track A — `examples/configs/hosted.yaml` edit + test 6.
4. Track B — `GenerateClipStage` seam + `_artifact_bytes` rewire + tests 7–10.
5. Track B — `HostedAPIBackend.result()` header population + tests 11–12.
6. Integration test 13 + README + PROGRESS Phase 23 entry.
7. `--no-ff` merge to `main`; PROGRESS:155 follow-ups #1 and #2 marked CLOSED.

Each task lands as one commit with a Conventional Commits subject
(`feat`, `fix`, `test`, `docs`, `refactor`, `chore`). PROGRESS.md updated
+ committed after each task.
