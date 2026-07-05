# Height-target upscaling (`1080p` / `720p`) — design

**Date:** 2026-07-05
**Status:** validated (brainstorm approved)
**Spec (what):** extends `SPEC.md` upscale surface — height-target arithmetic.
**Related plan:** `docs/superpowers/plans/2026-07-05-height-target-upscale.md` (to be written)

## 1. Problem

The upscale surface today accepts only factor targets (`2x`, `4x`) via
`--scale` / `cfg.upscale.scale`. Factor targets multiply the source resolution,
which can produce very large deliverables (e.g. 480² → 1920² at 4×). Users want
to cap the deliverable by **vertical resolution** instead: `--scale 1080p`,
`--scale 720p`.

The grammar for this already exists and was shipped intentionally on day one:
`src/kinoforge/core/scale_target.py` — `ScaleTarget.parse` recognises both
`Nx` (`kind="factor"`) and `Np` (`kind="height"`). The height branch parses but
every v1 consumer (`UpscaleStage`, all three engine runtimes, both engine
`validate_spec`s) hard-raises `NotYetImplementedError`. This design implements
the deferred height branch.

## 2. Confirmed pre-conditions

- **Grammar present, arithmetic deferred.** `ScaleTarget(kind="height")` parses;
  consumers refuse it. Confirmed in `core/scale_target.py`.
- **FlashVSR is 4×-only.** Native scale is hard-pinned to 4× by the
  `Causal_LQ4x_Proj` weight shape. Non-4× is refused. This is the live
  Wan↔FlashVSR pipeline.
- **spandrel / seedvr2 are multi-factor.** seedvr2 `_SUPPORTED_FACTORS = (2.0,
  4.0)`; spandrel exposes a per-model `supported_scales`. So "pick a multiplier
  2×/4×" is only meaningful for these two; on FlashVSR the multiplier is always
  4×.
- **`supported_scales` is already on the `UpscalerEngine` interface** and
  populated by all three engines. The resolver reads this existing capability —
  no new per-engine declaration needed.
- **An ffmpeg-resize pattern already exists** in `core/grid/compose.py`
  (`scale=…:force_original_aspect_ratio`) to mirror for the downscale.

## 3. Scope decision

Cover **all three engines** via an **engine-agnostic resolver**, not FlashVSR
alone. Rationale: the multiplier-selection is a pure function over
`(source_height, engine.supported_scales, requested_height)`; that capability
already exists on the interface, so covering all three is barely more work than
one, and scoping to FlashVSR would tempt a hardcoded `factor=4` shortcut — which
would itself be the tech debt. All three get offline coverage cheaply; only
FlashVSR is live-validated this pass (spandrel/seedvr2 not live-proven — flagged,
not validated).

## 4. Locked policy decisions

| Axis | Decision |
|---|---|
| Scope | All three engines, engine-agnostic resolver |
| Downscale location | Local `DownscaleStage` post-upscale (in-container ffmpeg-lanczos); zero server/engine changes |
| Intermediate large file | Discarded; delivered artifact = downscaled only |
| Source already ≥ target | Downscale-only: skip the GPU upscaler entirely |
| Largest factor still short | Raise a clear error (`ScaleUnsatisfiableError`); no under-target delivery |
| Multiplier selection | **Smallest sufficient** factor (least overshoot → smallest intermediate + least downscale loss; exact hit when a factor lands dead-on) |
| Wiring | Two-stage + pure resolver (approach A) |

## 5. Architecture

> **Planning-time correction (2026-07-05).** The first draft placed the downscale
> in a mid-walk `DownscaleStage`. Code review during planning found the upscaled
> artifact's bytes are pod-side (its `.uri` is a RunPod proxy URL) and only become
> local in the orchestrator's post-stage *materialize* block; a mid-walk stage
> would have no local file and re-fetching would double-download the large
> intermediate. The downscale therefore runs at the **materialize boundary**,
> driven by the engine's already-returned `output_resolution`. All six locked
> policy decisions in §4 are unchanged — only the internal seam moved.

Runtime resolution — the source height is unknown until the render stage
completes, so height→factor resolution happens at **stage-run time**, never at
config load.

### 5.1 Resolver — pure function

`src/kinoforge/core/scale_resolver.py`:

```python
@dataclass(frozen=True)
class HeightPlan:
    upscale_factor: float | None   # None → skip GPU upscale (downscale-only)
    downscale_to: int | None       # None → no downscale (exact hit)

def resolve_height_target(
    source_h: int,
    supported_factors: tuple[float, ...],
    requested_h: int,
) -> HeightPlan: ...
```

Rules:

1. `source_h >= requested_h` → `HeightPlan(upscale_factor=None,
   downscale_to=requested_h)` (skip GPU, pure downscale).
2. else candidates = `[f for f in supported_factors if source_h * f >= requested_h]`:
   - empty → **raise `ScaleUnsatisfiableError`** naming source height, largest
     factor, resulting height, and requested height.
   - else `factor = min(candidates)` (smallest sufficient):
     - `source_h * factor == requested_h` → `downscale_to=None`.
     - else (`> requested_h`) → `downscale_to=requested_h`.

Pure, no I/O, no torch → exhaustive unit table, zero spend. FlashVSR `(4.0,)`
degenerates correctly; seedvr2 `(2.0, 4.0)` and spandrel per-model menus all ride
the same function.

### 5.2 Probe helper

`core/frames.py` — add `ffprobe_dims(path) -> tuple[int, int]` (width, height),
sibling to the existing duration probe, reusing its ffprobe subprocess pattern
and `FrameExtractionError` handling.

### 5.3 `UpscaleStage` height-awareness

`pipeline/upscale.py` — delete the `kind=="height"` raise. On a height target the
stage picks a factor, runs the engine, and records the downscale decision for the
materialize boundary (§5.5). Runtime reality (source bytes/dims are pod-side in
render+upscale mode; the engine already returns `input_resolution` +
`output_resolution`) drives two cases:

- **Single-factor engine** (FlashVSR `supported_scales=(4x,)`): no pre-run source
  probe needed — run the sole factor, then read `result.output_resolution[1]`.
  `output_h < requested` → raise `ScaleUnsatisfiableError`; `output_h > requested`
  → `downscale_to=requested`; `==` → no downscale.
- **Multi-factor engine** (seedvr2 `(2x,4x)`, spandrel per-model): pre-select the
  smallest sufficient factor via `resolve_height_target`. `source_h` comes from
  `ffprobe_dims` when the source artifact is local (`file://`, the upscale-only
  path); the downscale decision is confirmed post-run against `output_resolution`.
  Not live-validated this pass.

The stage stashes the resolved `downscale_to` (int, or absent) onto
`artifacts["upscaled"].meta["downscale_to"]` so the materialize step acts without
re-deciding. Downscale-only (`upscale_factor is None`, source ≥ target) skips the
engine, passes the clip through as `"upscaled"`, stashes `downscale_to=requested`.
Always sets `artifacts["upscaled"]`. `kind="factor"` path unchanged.

### 5.4 Downscale executor — `pipeline/downscale.py`

A pure helper (NOT a mid-walk Stage — bytes aren't local until the orchestrator
materializes them, §5.5):

```python
def downscale_video_bytes(
    video_bytes: bytes, target_h: int,
    *, run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes: ...
```

Runs ffmpeg `scale=-2:{target_h}:flags=lanczos` over stdin→stdout (`-2`
auto-computes an even width, aspect preserved → h264-safe), reusing the injectable
`run` seam from `core/frames.py`. Fully offline-testable. The large intermediate is
the transient input bytes — never a delivered artifact.

### 5.5 Orchestrator materialize-boundary wiring

The delivered upscaled file is fetched to local bytes in the orchestrator's
post-stage materialize block (`core/orchestrator.py` ~1926–1958) — the first point
bytes are guaranteed local. Downscale slots here:

1. Obtain local `body` bytes for `state.artifacts["upscaled"]` (fetch when the uri
   is `http(s)`, read when `file://`).
2. If `upscaled.meta.get("downscale_to")` is set →
   `body = downscale_video_bytes(body, downscale_to)` before `sink.publish`.
3. Publish + replace the artifact uri with the local `file://` path, as today.

Reuses the single existing download (no double-fetch), stays engine-agnostic, keeps
the delivered file under `"upscaled"`. The block's current `http`-only guard widens
to "ensure local bytes, then optionally downscale" so a future local-uri upscaler
also downscales.

### 5.6 Lift the two height refusals; keep engine guards as invariants

Because `UpscaleStage` resolves height → a concrete factor *before* calling the
engine, the engine/runtime never receive `kind="height"`. Only two refusals must
be lifted:

1. `pipeline/upscale.py` — replace `UpscaleStage`'s `kind=="height"`
   `NotYetImplementedError` with the resolution logic of §5.3.
2. `core/config.py` ~683 — relax `UpscaleConfig._validate_flashvsr_wiring` to
   accept a height-target `scale`. Guard the existing `parsed.value != 4.0` check
   behind `parsed.kind == "factor"` (height has no factor value; the native run
   stays 4×, height is reached by 4× + downscale).

The `kind=="height"` guards in `seedvr2/_runtime.py`, `spandrel/_runtime.py`,
`flashvsr/_runtime.py`, and the two engine `validate_spec`s are **kept** — they are
now unreachable-in-normal-flow invariants ("height must be resolved upstream before
an engine sees it"), i.e. defense in depth. Optionally re-message them from
"deferred" to "must be resolved upstream", but no functional removal is required.
This keeps the change surface minimal and the live FlashVSR path low-risk.

### 5.7 Errors

Add `ScaleUnsatisfiableError(KinoforgeError)` to `core/errors.py` (undershoot —
no supported factor reaches the requested height). Message names source height,
largest available factor, resulting height, requested height, and suggests a
larger-factor engine.

## 6. Data flow

```
cfg.upscale.scale = "1080p"   (engine=flashvsr)
  → orchestrator: ScaleTarget.parse → kind="height", value=1080
  → stages = [ …render…, UpscaleStage(scale=height/1080) ]
  → UpscaleStage.run:
       single-factor engine (4x) → engine.upscale(factor=4.0)   # runs on pod
       result.output_resolution = (1920,1920)
       1920 > 1080 → artifacts["upscaled"].meta["downscale_to"] = 1080
  → orchestrator materialize block:
       fetch pod bytes → body
       meta.downscale_to == 1080 → body = downscale_video_bytes(body, 1080)
       sink.publish(body) → local file://…1080p.mp4
  → delivered artifact = 1080p (intermediate 1920² bytes discarded)
```

Downscale-only example (`source_h=1080, requested=720`, source local):
resolver → `HeightPlan(upscale_factor=None, downscale_to=720)` → engine skipped,
`downscale_to=720` stashed → materialize block lanczos to 720p.

## 7. Testing (red/green, per `test-design` skill)

- **Resolver** — exhaustive offline table; each case states the behavior under
  test and the concrete bug it catches:
  - downscale-only (`source ≥ target`) → factor None, downscale set.
  - exact hit (`540, (2,4), 1080`) → factor 2, downscale None.
  - overshoot → smallest sufficient (`480, (2,4), 1080`) → factor 4, downscale
    1080 (asserts it does **not** pick 2).
  - undershoot (`240, (4,), 1080`) → raises `ScaleUnsatisfiableError`.
  - single-factor menu (FlashVSR `(4,)`) vs multi (`(2,4)`).
- **`ffprobe_dims`** — offline against a tiny fixture mp4; asserts exact w,h.
- **`downscale_video_bytes`** — offline, injectable `run` seam: asserts the ffmpeg
  argv contains `scale=-2:{target}:flags=lanczos`; a real-ffmpeg fixture test
  asserts output height == target, width even, aspect within ±1px.
- **`UpscaleStage` height branch** — mocked engine: single-factor path runs the
  sole factor and stashes `downscale_to` from `output_resolution` (overshoot),
  omits it on exact, raises `ScaleUnsatisfiableError` on undershoot; downscale-only
  path does **not** call the engine and stashes `downscale_to=requested`.
- **Orchestrator materialize** — fakes (fake sink + injected fetch/downscale
  seams): asserts `downscale_video_bytes` is invoked with the stashed
  `downscale_to`, and NOT invoked when meta is absent (factor-target path).
- **Live** — FlashVSR only, one `--no-reuse` smoke (480² → `1080p`: 4×=1920 →
  lanczos 1080), frame-QA'd per CLAUDE.md (5-frame contact sheet, judged for
  artifacts / coherence / fidelity vs the 1920² sibling) before reporting green.
  spandrel/seedvr2 stay offline-only this pass.

## 8. Gotchas / non-obvious constraints

- Resolution is **runtime**, not config-time — source height is unknown until
  the render stage finishes.
- seedvr2/spandrel gain height-target offline-free but are **not** live-proven
  in this pass — flagged, not validated.
- `requested_h` values (1080, 720) are even; ffmpeg `-2` keeps width even →
  h264-safe.
- Smallest-sufficient (not largest) is deliberate: minimises the intermediate
  file and the downscale ratio, and yields an exact hit with no downscale when a
  factor lands on the target.

## 9. Out of scope

- Width-target grammar (`1920w`) — not requested.
- Pod-side downscale — rejected to preserve the engine-agnostic win; the
  DownscaleStage is local. (Could slot a pod-side implementation later behind a
  downscale-executor interface if transfer cost ever bites — not now.)
- Retaining the large intermediate — discarded; no `--keep-intermediate` flag.
- Upscale-interpolation to force an unreachable target — rejected (quality lie).
