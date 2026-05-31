# Layer F — engine `submit()` consumes seg-0 assets

**Date:** 2026-05-30
**Scope:** Wire `init_image` conditioning assets from `segments[0].assets` into
each engine's outgoing request, closing the loop opened by Layer E (which
persists tail PNGs into the store but no engine reads them).
**Roles:** `init_image` only.
**Engines:** ComfyUI + Diffusers + Hosted.

## 1. Problem

Layer E rewired `GenerateClipStage` to extract the previous segment's last
frame, persist it as a PNG via `store.put_bytes`, wrap it in a
`ConditioningAsset(kind="image", role="init_image", ref=<Artifact>)`, and call
`inject_tail_frame(next_job, tail_asset)` so `next_job.segments[0].assets`
carries that asset. But every engine's `submit()` reads only `job.spec`. The
tail PNG is dead weight at render time. The non-native multi-segment chain
produces orphan PNGs; no segment after the first conditions on the previous
output.

Layer F closes that gap. Each engine's `submit()` reads
`job.segments[0].assets`, finds the asset matching its accepted role, and
folds the content into the protocol-specific spec/request payload before
dispatch.

## 2. Resolved design questions

| Question | Decision |
|---|---|
| Role scope | `init_image` only. Other roles (`first_frame`, `last_frame`, `drive_audio`, `source_video`) deferred until a consuming engine ships. |
| Engine scope | All three engines (ComfyUI, Diffusers, Hosted) wired in one layer. |
| Asset transport | Engine fetches bytes from `asset.ref.uri` itself via a shared `core/assets.py` helper. User picks a store reachable by the engine's runtime (LocalArtifactStore for local engines; S3/GCS for remote engines). |
| Where assets land in the spec | **Data-driven.** ComfyUI: per-job `spec["asset_node_ids"]: dict[role, node_id]` declares which graph node receives each role. Diffusers/Hosted: per-engine `cfg.engine.<kind>.asset_paths: dict[role, dot_path]` declares the dot-path into the request body. Mirrors Layer E's `cfg.engine.hosted.url_path` dot-walker pattern. |
| Approach | **Approach A** — per-engine `submit()` owns its injection inline; shared pure helpers in `core/assets.py`; no new ABC methods. Matches Layer E pattern (`core/frames.py` + per-engine `extract_last_frame`). |

## 3. Module surface

### New module: `src/kinoforge/core/assets.py`

Two pure helpers + one dot-path setter. No I/O at module level. Mirrors
`core/frames.py`.

```python
def find_asset(
    job: GenerationJob, role: str
) -> ConditioningAsset | None:
    """Return segments[0]'s asset with matching role, or None.

    Raises ValidationError on duplicate role in segments[0].assets.
    Looks only at segments[0] — the chain mechanism puts injected
    tail-frames there, and user-supplied request assets land there
    via GenerateClipStage.run() (segments_override path or the
    happy-path single-segment build).
    """

def asset_bytes(
    uri: str,
    *,
    http_get_bytes: Callable[[str], bytes],
) -> bytes:
    """Resolve URI to raw bytes by scheme.

    http(s)  -> http_get_bytes(uri)
    file://  -> Path(urlparse(uri).path).read_bytes()
    other    -> raise AssetFetchError

    Wraps urllib.error.URLError, FileNotFoundError, OSError as
    AssetFetchError.
    """

def set_by_dot_path(body: dict, dot_path: str, value: Any) -> None:
    """In-place: write value at dotted path, creating intermediate dicts.

    Caller is responsible for passing a copy of the body it wants to mutate.

    Note: a sibling reader exists as the private `_walk_dot_path` in
    `engines/hosted/__init__.py` (Layer E, used to parse server response
    bodies). The two helpers serve different sides (write request vs read
    response) and stay separate at Layer F. A future cleanup may promote
    the reader to `core/assets.py` if a second engine needs response
    walking.
    """
```

### New error: `src/kinoforge/core/errors.py`

```python
class AssetFetchError(KinoforgeError):
    """Raised when fetching an asset's bytes fails (bad scheme,
    HTTP non-200, missing file, ComfyUI /upload/image failure)."""
```

Subclasses `KinoforgeError` alongside `FrameExtractionError` (Layer E).
Symmetric wrapping discipline.

### Modified: `src/kinoforge/engines/comfyui/__init__.py`

- New injectable seam: `_http_post_file(url, *, field_name, filename, content) -> str`.
  Default: stdlib `urllib.request` + `email.mime.multipart` multipart POST;
  parses `{"name": "..."}` from response JSON (per ComfyUI `/upload/image`
  contract). Tests inject a spy.
- `submit()` extended (see §4.1).
- `validate_spec()` extended (see §6).

### Modified: `src/kinoforge/engines/diffusers/__init__.py`

- `submit()` extended (see §4.2).
- `validate_spec()` extended (see §6).
- Reuses existing `_http_get_bytes` seam (Layer E) — though Diffusers does
  not currently fetch asset bytes in Layer F (URL passthrough); the seam is
  available if a future Diffusers server expects bytes.

### Modified: `src/kinoforge/engines/hosted/__init__.py`

- `submit()` extended (see §4.3).
- `validate_spec()` extended (see §6).
- Reuses existing `_http_get_bytes` seam (Layer E); same as Diffusers — URL
  passthrough at Layer F, seam ready if needed.

### Modified: `src/kinoforge/core/config.py`

- `DiffusersEngineCfg` gains optional `asset_paths: dict[str, str] = {}`.
- `HostedEngineCfg` gains optional `asset_paths: dict[str, str] = {}`.

No ComfyUI cfg change — ComfyUI's `asset_node_ids` lives on the per-job
`spec`, not on cfg, because graph node IDs are graph-specific.

### Untouched

- `GenerateClipStage` body — Layer F adds nothing here except an extra
  `engine.validate_spec(job)` call inside the chain loop (see §6).
- `ConditioningAsset`, `Segment`, `GenerationJob`, `GenerationEngine` ABC,
  `inject_tail_frame`, `core/continuity.py`, all stores.

### Invariants preserved

- Core never imports an engine — `core/assets.py` has no engine imports.
- Stage's only concern remains "store the tail, inject via
  `inject_tail_frame`" — Layer F adds zero stage logic except the second
  `validate_spec` call site documented in §6.
- `core/__init__.py` no-op — `core/assets.py` is pure helpers, no registry.
- ABC surface unchanged — no new methods on `GenerationEngine`.

## 4. Per-engine injection contracts

### 4.1 ComfyUI

**Spec shape change:** one optional key added to `job.spec`:

```python
job.spec = {
    "graph": {...},
    "node_overrides": {...},
    "asset_node_ids": {              # NEW, optional
        "init_image": "<node_id>",   # node_id of a LoadImage node in graph
    },
}
```

The template author declares which `LoadImage` node receives each role.
Engine never guesses graph topology (real graphs have multiple `LoadImage`
nodes — LoRA previews, reference frames, init image, etc.).

**`submit()` extension** (inserted before existing graph-merge loop):

```python
def submit(self, job: GenerationJob) -> str:
    graph = copy.deepcopy(job.spec.get("graph", {}))
    overrides: dict[str, Any] = dict(job.spec.get("node_overrides", {}))
    asset_node_ids: dict[str, str] = job.spec.get("asset_node_ids", {})

    for role, node_id in asset_node_ids.items():
        asset = find_asset(job, role)
        if asset is None:
            continue
        payload = asset_bytes(
            asset.ref.uri,
            http_get_bytes=self._http_get_bytes,
        )
        upload_url = f"{self._base_url}/upload/image"
        try:
            uploaded_name = self._http_post_file(
                upload_url,
                field_name="image",
                filename=asset.ref.filename or f"{role}.png",
                content=payload,
            )
        except (urllib.error.URLError, OSError) as e:
            raise AssetFetchError(
                f"ComfyUI /upload/image failed for role {role!r}: {e}"
            ) from e
        node_patch = overrides.setdefault(node_id, {})
        inputs = node_patch.setdefault("inputs", {})
        inputs["image"] = uploaded_name

    # existing deep-merge loop unchanged
    for node_id, node_patch in overrides.items():
        if node_id in graph:
            _deep_merge(graph[node_id], node_patch)
        else:
            graph[node_id] = copy.deepcopy(node_patch)
    url = f"{self._base_url}/prompt"
    response = self._http_post(url, {"prompt": graph})
    return str(response["prompt_id"])
```

**ComfyUI `/upload/image` server contract** (documented in ComfyUI
`server.py`): response JSON shape `{"name": "<server-side-filename>",
"subfolder": "", "type": "input"}`. Engine reads `name`. `subfolder` and
`type` pinned to defaults; later layer can expose if needed.

### 4.2 Diffusers

**Config change:** `DiffusersEngineCfg.asset_paths: dict[str, str] = {}`.

```yaml
engine:
  kind: diffusers
  base_url: http://localhost:8000
  asset_paths:
    init_image: "init_image_url"
```

**Server contract** (in-house Diffusers server, kinoforge-controlled): server
fetches `body["init_image_url"]` via stdlib `urllib`, loads as `PIL.Image`,
passes to pipeline as `image=...`. Documented in `README.md`'s Diffusers
section (where Layer E's server-side `url` field is already documented);
Layer F appends a paragraph describing `init_image_url` and the configured
`asset_paths` mapping.

**`submit()` extension:**

```python
def submit(self, job: GenerationJob) -> str:
    body = dict(job.spec)
    asset_paths = self._cfg.engine.diffusers.asset_paths
    for role, dot_path in asset_paths.items():
        asset = find_asset(job, role)
        if asset is None:
            continue
        set_by_dot_path(body, dot_path, asset.ref.uri)
    url = f"{self._base_url}/generate"
    response = self._http_post(url, body)
    return str(response["job_id"])
```

No bytes fetched at the engine — URL passthrough. Server fetches. Trust
model is identical to the existing Diffusers server contract: server has
network reach to whatever store the URI points to. If the user runs the
Diffusers server in a container on a different host than the orchestrator,
the store must be reachable from the server (S3/GCS, or shared volume for
`file://`).

### 4.3 Hosted

**Config change:** `HostedEngineCfg.asset_paths: dict[str, str] = {}`.

```yaml
engine:
  kind: hosted
  endpoint: https://api.example.com/v1/predictions
  asset_paths:
    init_image: "input.image_url"
```

**`submit()` extension:** structurally identical to Diffusers. Reads
`cfg.engine.hosted.asset_paths`. Writes URL directly into provider request
body via `set_by_dot_path`. No bytes fetch — provider fetches from URL
(Replicate, Stability AI, etc. all accept URL inputs for image
conditioning).

```python
def submit(self, job: GenerationJob) -> str:
    body = dict(job.spec)
    asset_paths = self._cfg.engine.hosted.asset_paths
    for role, dot_path in asset_paths.items():
        asset = find_asset(job, role)
        if asset is None:
            continue
        set_by_dot_path(body, dot_path, asset.ref.uri)
    response = self._http_post(self._endpoint, body)
    return str(response["job_id"])
```

### 4.4 Symmetry note

Diffusers + Hosted are structurally identical at Layer F (both URL
passthrough via dot-walker). The duplication is acceptable per the
established sibling-adapter pattern (PROGRESS.md §"S3/GCS shipped as two
independent siblings, no shared cloud-base"). If a third URL-passthrough
engine ships later, factor an `_inject_url_assets(body, cfg_asset_paths,
job)` helper into `core/assets.py`.

## 5. Data flow

```
seg-i finishes (i >= 1)
  └─> stage: extract_last_frame  (Layer E)
      └─> store.put_bytes(run_id, "seg-{i-1}-tail.png", bytes)
          └─> ConditioningAsset(role="init_image", ref=stored_artifact)
              └─> inject_tail_frame(next_job, asset)  (Layer E)
                  └─> next_job.segments[0].assets = [asset]
                      └─> stage.validate_spec(next_job)            ← NEW in §6
                          └─> pool.submit(next_job)
                              └─> backend.submit(next_job)         ← LAYER F
                                  ├─ ComfyUI: find_asset → fetch bytes → upload → patch node
                                  ├─ Diffusers: find_asset → set URL at dot-path
                                  └─ Hosted: find_asset → set URL at dot-path
```

## 6. Failure modes + error contract

Four failure axes. One exception per axis. Loud and early.

| # | Axis | Detected in | Exception | Behaviour |
|---|------|-------------|-----------|-----------|
| 1 | Asset role present, injection path undeclared | `engine.validate_spec(job)` | `ValidationError` | Hard fail before submit; no HTTP, no compute spend |
| 2 | Duplicate role in `segments[0].assets` | `find_asset(job, role)` inside `submit()` | `ValidationError` | Programmer error — splitter/orchestrator broke the invariant |
| 3 | URI scheme unsupported / unreachable | `asset_bytes()` (ComfyUI only — Diffusers/Hosted defer to their server) | `AssetFetchError` | Wraps `urllib.error.URLError`, `FileNotFoundError`, `OSError`. Submit aborts before any HTTP POST to engine — no half-state |
| 4 | ComfyUI `/upload/image` non-200 or transport failure | `_http_post_file()` | `AssetFetchError` | Wraps underlying HTTP error; engine never tries `/prompt` |

**Not a failure mode** (deliberate):

- **Asset role absent + path declared** → silent skip. Same template serves
  t2v + i2v + flf2v variants; declaring `asset_paths.init_image` doesn't
  force every job to carry an init_image. Mode-vs-role enforcement is
  `validate_request`'s job (already done by orchestrator upstream).
- **Server-side asset fetch failure** (Diffusers/Hosted server can't reach
  URL) → engine's `submit()` succeeds; failure surfaces in `result()`
  polling per the server's status protocol. Already covered by existing
  `TimeoutError` / engine-specific error paths.
- **ComfyUI `/upload/image` succeeds but rendered frame is corrupt** → not
  Layer F's concern; engine's existing `result()` poll handles via the
  history endpoint.

### `validate_spec` call sites

Today `validate_spec(job)` runs once: orchestrator-level, before dispatch.
Layer F's chain mechanism injects the tail-frame asset INSIDE
`GenerateClipStage.run`'s loop, AFTER orchestrator-level validation. A
misconfigured `asset_node_ids` / `asset_paths` wouldn't trip until inside
`submit()`, by which point we've already burned compute on segment 0.

**Decision:** `GenerateClipStage` calls `self.engine.validate_spec(job)` on
each post-`inject_tail_frame` job (segments ≥ 1). One extra call per chained
segment; pure function; cheap. Catches misconfig before the engine HTTP
round-trip.

Updated stage loop:

```python
for i, job in enumerate(jobs):
    if i > 0 and should_chain:
        tail_bytes = self.engine.extract_last_frame(results[-1])
        tail_name = f"seg-{i - 1}-tail.png"
        stored = self.store.put_bytes(self.run_id, tail_name, tail_bytes)
        tail_artifact = replace(stored, filename=tail_name)
        tail_asset = ConditioningAsset(
            kind="image",
            role="init_image",
            ref=tail_artifact,
        )
        job = inject_tail_frame(job, tail_asset)
        self.engine.validate_spec(job)                ← NEW
    art = self.pool.submit(job).result()
    results.append(art)
```

## 7. Testing strategy (offline, red-first)

Discipline: no real network, no real subprocess, no real GPU/weights.
Existing injectable seams cover all HTTP; one new seam
(`_http_post_file` on ComfyUI) covers multipart upload.

### 7.1 New file: `tests/core/test_assets.py`

| Test | Behaviour under test | Concrete bug it catches |
|---|---|---|
| `test_find_asset_returns_match` | role match returns first asset in `segments[0].assets` | substring/prefix role matching that accepts partial-role |
| `test_find_asset_returns_none_for_missing` | role absent returns `None` | implicit KeyError on missing role |
| `test_find_asset_raises_on_duplicate_role` | two assets with same role → `ValidationError` | engine silently picks first when splitter bug puts same role twice |
| `test_asset_bytes_http_dispatches_to_fetcher` | `http://` URI → injected `http_get_bytes(uri)` | someone wires filesystem path for http URI |
| `test_asset_bytes_file_reads_filesystem` | `file:///path` → `Path.read_bytes()` | urlparse drops the leading slash on `file://` URIs |
| `test_asset_bytes_unsupported_scheme_raises` | `s3://` or `gs://` → `AssetFetchError` | silent fallback to http_get_bytes that 404s 30s later |
| `test_asset_bytes_wraps_http_error` | fetcher raises `URLError` → `AssetFetchError` wraps it | engine error path leaks `URLError` to caller (Layer E symmetry) |
| `test_asset_bytes_wraps_file_not_found` | missing file → `AssetFetchError` | leaks `FileNotFoundError` instead of typed error |
| `test_set_by_dot_path_simple` | `set(body, "key", val)` writes at top level | dot in single-key path treated as nested |
| `test_set_by_dot_path_nested_creates_intermediates` | `set(body, "a.b.c", val)` creates intermediate dicts | overwrites existing sibling branch; or fails when intermediate missing |

### 7.2 Extended: `tests/engines/test_comfyui.py` (~8 new tests)

- `test_submit_uploads_bytes_for_declared_asset_role` — spy on
  `_http_post_file` records exact call args (URL, field_name, filename,
  payload bytes).
- `test_submit_patches_node_with_uploaded_filename` — captured `_http_post`
  body for `/prompt` contains
  `node_overrides[<node_id>]["inputs"]["image"] == <returned_name>`.
- `test_submit_with_no_asset_node_ids_unchanged` — old test path still
  works (regression for Layer F not breaking pre-Layer-F templates).
- `test_submit_skips_role_when_asset_absent` — `asset_node_ids:
  {init_image: "5"}` but `segments[0].assets == []` → no upload, no patch
  (silent skip per §6).
- `test_validate_spec_rejects_role_without_node_id_mapping` — asset
  present, mapping absent → `ValidationError`.
- `test_submit_raises_AssetFetchError_on_upload_failure` —
  `_http_post_file` raises `URLError` → `AssetFetchError`.
- `test_submit_raises_AssetFetchError_on_fetch_failure` — `_http_get_bytes`
  raises → `AssetFetchError`.
- `test_submit_file_uri_reads_local_bytes` — fake store URI
  `file:///tmp/...` round-trips through `asset_bytes` to upload.

### 7.3 Extended: `tests/engines/test_diffusers.py` (~4 new tests)

- `test_submit_writes_asset_uri_at_configured_dot_path` — body sent to
  `/generate` contains the URI at the configured dot-path.
- `test_submit_no_asset_paths_unchanged` — regression.
- `test_validate_spec_rejects_asset_without_path_mapping`.
- `test_submit_does_not_fetch_asset_bytes` — `_http_get_bytes` spy never
  called (URL passthrough at Layer F).

### 7.4 Extended: `tests/engines/test_hosted.py` (~4 new tests)

Mirror of Diffusers tests against `cfg.engine.hosted.asset_paths`, using a
nested dot-path destination (`input.image_url`) to exercise
`set_by_dot_path` nesting in an engine context.

### 7.5 Extended: `tests/pipeline/test_generate_clip.py` (~3 new tests)

- `test_stage_calls_validate_spec_on_chained_jobs` — counting fake engine
  records call count; N chained segments → N post-chain `validate_spec`
  calls (one per `i > 0 and should_chain` iteration).
- `test_chain_delivers_asset_to_engine_submit` — fake engine records
  `job.segments[0].assets` it observed; asserts
  `assets[0].role == "init_image"` and
  `assets[0].ref.uri == stored_tail.uri`.
- `test_stage_aborts_when_engine_validate_spec_raises_post_chain` — engine
  raises `ValidationError` on segment 1's `validate_spec`; segment 0
  succeeded; no segment-2 dispatch; raises out.

### 7.6 Extended: `tests/core/test_config.py` (~2 new tests)

- `test_diffusers_cfg_asset_paths_defaults_empty` — optional, defaults
  to `{}`.
- `test_hosted_cfg_asset_paths_defaults_empty`.

### 7.7 Total

~30 new tests across 6 files (1 new, 5 extended). Current suite 478 →
projected ~508. No new test infrastructure beyond a local `CountingEngine`
in `tests/pipeline/test_generate_clip.py` that records observed segments.

## 8. Acceptance criteria

1. **AC1** — ComfyUI: with `spec["asset_node_ids"] = {"init_image": "5"}`
   and `segments[0].assets = [<init_image asset>]`, `submit()` uploads the
   asset's bytes via `/upload/image`, then sends a `/prompt` whose merged
   graph has node `5`'s `inputs.image` set to the upload's returned name.
2. **AC2** — Diffusers: with `cfg.engine.diffusers.asset_paths =
   {"init_image": "init_image_url"}` and `segments[0].assets =
   [<init_image asset>]`, `submit()` POSTs a body containing
   `init_image_url == asset.ref.uri` (no bytes fetch).
3. **AC3** — Hosted: with `cfg.engine.hosted.asset_paths = {"init_image":
   "input.image_url"}`, `submit()` POSTs a body containing
   `input.image_url == asset.ref.uri`.
4. **AC4** — Validation: each engine's `validate_spec(job)` raises
   `ValidationError` when `segments[0].assets` contains a role for which
   no injection path is declared.
5. **AC5** — `GenerateClipStage` calls `engine.validate_spec(job)` once
   per post-chain job (i > 0 and `should_chain` True).
6. **AC6** — `find_asset` returns `None` for missing role and raises
   `ValidationError` for duplicate role.
7. **AC7** — `asset_bytes` resolves `http(s)` via the injected fetcher,
   `file://` via filesystem read, and raises `AssetFetchError` for
   unsupported schemes / underlying failures.
8. **AC8** — `set_by_dot_path` writes at nested paths, creating
   intermediate dicts.
9. **AC9** — Core-import invariant unbroken: `test_core_invariant.py`
   passes; `core/assets.py` imports no engine module.
10. **AC10** — Pre-Layer-F templates (no `asset_node_ids`, no
    `asset_paths`) submit and validate exactly as before — full
    regression-free.

## 9. Out of scope (deferred)

- Roles other than `init_image` (`first_frame`, `last_frame`, `drive_audio`,
  `source_video`). No engine declares flf2v or audio-mode support yet.
- `flf2v + N > 1 + non-native` chain semantics — pre-existing gap; Layer F
  does not address.
- Base64-embedded asset bytes for hosted providers that don't accept URL
  inputs. Future layer if a real provider needs it.
- Engine-side caching of uploaded assets across jobs (re-upload per submit
  is fine at current scale).
- ComfyUI multi-`subfolder` / `type` upload (`/upload/image` defaults
  cover the init-image case).
- A central `consume_assets()` ABC method (Approach B). Re-evaluate when 5+
  engines exist or a CDN-style upload service ships.
- Asset transport ABC + per-engine `AssetTransport` impls (Approach C). Same
  trigger as above.

## 10. Build order

1. `AssetFetchError` in `core/errors.py`.
2. `core/assets.py` (`find_asset`, `asset_bytes`, `set_by_dot_path`) +
   `tests/core/test_assets.py` (red-first).
3. `DiffusersEngineCfg.asset_paths` + `HostedEngineCfg.asset_paths` +
   `tests/core/test_config.py` extensions.
4. Diffusers `submit` + `validate_spec` extensions + tests.
5. Hosted `submit` + `validate_spec` extensions + tests.
6. ComfyUI `_http_post_file` injectable + `submit` + `validate_spec`
   extensions + tests.
7. `GenerateClipStage` post-chain `validate_spec` call + tests.
8. Append Layer F paragraph to `README.md` Diffusers section (declares
   `init_image_url` is server-fetched; documents the `asset_paths` cfg
   mapping). Mirror with a Hosted paragraph documenting `asset_paths` on
   `cfg.engine.hosted`.
9. Final run: `pixi run test`, `pixi run typecheck`, `pixi run lint`,
   `pixi run pre-commit run --all-files`. Verify `~508` tests pass.
10. Update `PROGRESS.md` (Layer F entry under Phase 16) and `README.md`
    (replace Layer F limitation callout with the new contract).
