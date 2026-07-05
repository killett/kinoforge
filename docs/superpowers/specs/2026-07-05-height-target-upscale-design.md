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

`pipeline/upscale.py` — delete the `kind=="height"` raise. On a height target:

1. Probe source height (`ffprobe_dims` on `state.artifacts["clip"]`).
2. `plan = resolve_height_target(h, tuple(s.value for s in engine.supported_scales), requested)`.
3. `plan.upscale_factor` set → run `engine.upscale` at that factor
   (`ScaleTarget(kind="factor", value=plan.upscale_factor)`).
4. `plan.upscale_factor is None` (downscale-only) → skip the engine, pass the
   clip through.
5. Always set `artifacts["upscaled"]` (engine output, or the clip when skipped)
   so downstream keying holds.

`kind="factor"` path unchanged.

### 5.4 `DownscaleStage` (new)

`pipeline/downscale.py` — appended only for `kind="height"`. Carries the target
height. On run: probe `artifacts["upscaled"]` height; if `> requested_h` →
ffmpeg `scale=-2:{requested_h}:flags=lanczos` (`-2` auto-computes an even width
with aspect preserved → h264-safe). Idempotent passthrough if already exact.
Rewrites `artifacts["upscaled"]` with the shrunk file; the large intermediate is
a temp, discarded.

### 5.5 Orchestrator wiring

`core/orchestrator.py:1858` (`if cfg.upscale is not None`) — after appending
`UpscaleStage`, if `ScaleTarget.parse(cfg.upscale.scale).kind == "height"`, also
append `DownscaleStage(requested_h=…)`. The existing artifact materialization
(1908–1926) is unchanged — the delivered file stays under `"upscaled"`.

### 5.6 Remove stale raises

Drop the `kind=="height"` `NotYetImplementedError` in
`seedvr2/_runtime.py`, `spandrel/_runtime.py`, `flashvsr/_runtime.py`, and both
engine `validate_spec`s. Resolution now happens upstream in the stage; runtimes
only ever receive a concrete factor.

### 5.7 Errors

Add `ScaleUnsatisfiableError(KinoforgeError)` to `core/errors.py` (undershoot —
no supported factor reaches the requested height). Message names source height,
largest available factor, resulting height, requested height, and suggests a
larger-factor engine.

## 6. Data flow

```
cfg.upscale.scale = "1080p"
  → orchestrator: ScaleTarget.parse → kind="height", value=1080
  → stages = [ …render…, UpscaleStage(scale=height/1080), DownscaleStage(1080) ]
  → UpscaleStage.run:
       source_h = ffprobe_dims(clip).h            # e.g. 480
       plan = resolve_height_target(480, (4.0,), 1080)
            → factor=4.0 (1920 ≥ 1080), downscale_to=1080
       upscaled = engine.upscale(factor=4.0)      # 1920²
  → DownscaleStage.run:
       1920 > 1080 → ffmpeg scale=-2:1080:lanczos → 1080p artifact
  → delivered artifact = 1080p (intermediate 1920² discarded)
```

Downscale-only example: `source_h=1080, requested=720` →
`HeightPlan(upscale_factor=None, downscale_to=720)` → GPU skipped, lanczos to
720p.

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
- **`DownscaleStage`** — offline ffmpeg on a fixture: output height == target,
  width even, aspect within ±1px of source, passthrough (byte-identical or
  dims-identical) when already exact.
- **`UpscaleStage` height branch** — mocked engine: asserts the factor passed to
  the engine, and that the engine is **not** called on the downscale-only path.
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
