# Frame interpolation stage (RIFE) — design

**Date:** 2026-07-05
**Status:** validated (brainstorm approved)
**Spec (what):** extends `SPEC.md` post-processing surface — a new frame-interpolation
stage sibling to upscale.
**Related plan:** `docs/superpowers/plans/2026-07-05-frame-interpolation.md` (to be written)

## 1. Problem

Generated clips are low-fps (Wan 2.2 T2V default ≈ 16 fps). Users want smooth
deliverables at conventional rates — 24, 25, 29.97, 30, 60 fps. Add an
**interpolation stage** that synthesizes intermediate frames, mirroring the
upscale stage in every structural respect:

- Runs on a freshly-generated clip, **or** standalone on an already-existing
  video uploaded to the compute instance (`--video`, exactly like `upscale`).
- Takes a floating-point `--fps` target (`24`, `25`, `29.97`, `30`, `60`, …).

## 2. Confirmed decisions (brainstorm)

| Axis | Decision |
|---|---|
| First engine | **RIFE v4** (arbitrary-timestep), shipped + live-validated alone |
| Engine surface | **Engine-agnostic interface + resolver from day one**; only RIFE wired this pass. FILM / GMFSS-Fortuna / GIMM-VFI become pure plug-ins later with zero stage/resolver/config/CLI changes |
| Ordering (both stages) | **Upscale → interpolate** — upscale the fewest (original) frames through the expensive diffusion upscaler, then RIFE cheaply multiplies the sharp high-res frames. Minimizes frames through the costly upscaler and pod-death exposure; RIFE sees sharp frames so motion is cleaner |
| `target_fps ≤ source_fps` | **Decimate via ffmpeg, skip GPU** — symmetric to upscale's downscale-only path (`fps` filter in-container, no pod) |
| Compute | **Independent stage/pod for v1** — own engine + own RunPod server, mirrors FlashVSR. Co-residency (one pod does upscale+interp) is a deferred cost optimization |

## 3. Why engine-agnostic-interface-but-RIFE-only is the low-debt foundation

The foundation is the **interface + resolver abstraction**, not the count of
engines wired. Wiring three engines now is *surface*; untested surface is itself
debt, and only one engine can be live-validated inside smoke budget anyway. The
low-debt move is getting the abstraction right and shipping one engine proven
through it.

The abstraction mirrors upscale exactly:

- Upscale's foundation: `supported_scales` capability + a resolver doing *"pick
  smallest sufficient FACTOR, then downscale to exact height."*
- Interpolation's parallel: an engine **capability** (`ARBITRARY_TIMESTEP` vs
  `RECURSIVE_2X`) + a resolver doing *"reach the target frame-rate, then decimate
  to exact fps."* Same overshoot-then-trim shape; the `--fps` contract is computed
  by the **stage** from `(source_fps, target_fps)`, never by the engine.

Interp engines genuinely differ, which is why the capability split is the right
seam:

- **RIFE v4 / GIMM-VFI** — native *arbitrary-timestep*: hand them `t∈(0,1)`, hit
  any target fps in one pass.
- **FILM / GMFSS-classic** — *recursive 2×* only: overshoot to the nearest
  power-of-two ≥ target, then ffmpeg-decimate to the exact target.

With the interface built around the fps-contract + capability declaration, adding
FILM/GMFSS/GIMM later is a pure engine plug-in — zero stage/config/CLI/resolver
changes. (The upscale spec explicitly warned that a hardcoded shortcut "would
itself be the tech debt.")

## 4. Engine — RIFE v4

- RIFE v4.x (e.g. `rife49`): arbitrary-timestep. Given two adjacent frames and a
  timestep `t∈(0,1)`, synthesizes the intermediate frame at that time. Hits any
  target fps directly.
- Runs **pod-side** on its own RunPod server, mirroring `FlashVSREngine`
  (`upscalers/flashvsr/_engine.py`). GPU + torch. Weights are tiny (~50 MB) →
  fast cold-boot, low cost, short pod-death window.
- Engine attributes: `name = "rife"`, `requires_compute = True`,
  `requires_local_weights = True`, `capability = ARBITRARY_TIMESTEP`.
- Content note (known limitation, §11): RIFE interpolates across hard scene cuts →
  ghosting. Harmless for generated clips (no cuts); flagged for uploaded
  multi-cut videos.

## 5. Architecture

### 5.1 The fps resolver — pure function

`src/kinoforge/core/fps_resolver.py`:

```python
class InterpCapability(Enum):
    ARBITRARY_TIMESTEP = "arbitrary_timestep"
    RECURSIVE_2X = "recursive_2x"

@dataclass(frozen=True)
class FpsPlan:
    schedule: tuple[tuple[int, float], ...] | None  # (src_idx, timestep) per output frame (arbitrary-t)
    recursion_depth: int | None                     # log2 insertion factor (recursive-2x)
    decimate_to: float | None                       # ffmpeg fps trim to exact target; None → exact hit
    skip_gpu: bool                                  # target<=source or exact noop → no pod

def resolve_fps_target(
    source_fps: float, target_fps: float, cap: InterpCapability,
    *, source_frame_count: int | None = None,
) -> FpsPlan: ...
```

Rules:

1. `target_fps == source_fps` → `FpsPlan(skip_gpu=True, decimate_to=None, …)` —
   pure passthrough, no work.
2. `target_fps < source_fps` → `FpsPlan(skip_gpu=True, decimate_to=target_fps, …)`
   — ffmpeg `fps` filter decimation in-container, no GPU pod.
3. `target_fps > source_fps`:
   - **`ARBITRARY_TIMESTEP`** — exact placement. For each output frame index `j`
     (`0 … round(duration*target)-1`), output time `t_out = j / target`; source
     position `p = t_out * source = i + f`; emit `(i, f)`. `f == 0` copies source
     frame `i`; else synthesize between frames `i` and `i+1` at timestep `f`.
     Result is **exactly** `target_fps`, minimal frames, smooth; `decimate_to =
     None`. Non-integer targets (29.97 = 30000/1001) fall out exactly from the
     arithmetic.
   - **`RECURSIVE_2X`** (future engines) — `k = next_pow2(ceil(target/source))`;
     `recursion_depth = log2(k)`; the engine inserts to `source*k` fps, then
     `decimate_to = target_fps` unless `source*k == target` exactly.

Pure, no I/O, no torch → exhaustive offline unit table, zero spend. RIFE rides the
arbitrary-timestep branch; the recursive branch is validated offline against a
capability fixture so the abstraction is proven before a second engine exists.

### 5.2 Probe + decimation helpers — `core/frames.py`

- `ffprobe_fps(path) -> float` — parses ffprobe `r_frame_rate` (rational form
  `"16/1"`, `"30000/1001"`) into a float. Sibling to the existing `ffprobe_dims`
  and duration probes; reuses their subprocess + `FrameExtractionError` pattern.
- `decimate_video_fps(video_bytes, target_fps, *, run=_default_run) -> bytes` —
  ffmpeg `fps={target}` re-timing for the `target ≤ source` path. **Reads input
  from a seekable temp file, NOT `pipe:0`** — the known large-mp4 moov-atom
  seek failure (exit 183, commit `8438a8b`); reuses `downscale_video_bytes`'s
  temp-file pattern and injectable `run` seam. Fully offline-testable.

### 5.3 RIFE engine — new `src/kinoforge/interpolators/` package

A new `interpolators/` package parallel to `upscalers/` (its `rife/` subpackage
holds `_engine.py`, `_input_prep.py`, `__init__.py` — same shape as
`upscalers/flashvsr/`). New `InterpolatorEngine` interface (protocol paralleling `UpscalerEngine`) with
`interpolate(instance, job, cfg) -> InterpolateResult`, `render_provision(cfg)`,
`validate_spec(job)`, `model_identity(cfg)`, `capability`, and `requires_*`
flags. `RifeEngine` implements it: `render_provision` emits the bootstrap
(RIFE clone/weights fetch), `interpolate` POSTs `/interpolate`, polls
`/interpolate/status/{id}`, returns the result. Registered via
`registry.register_interpolator("rife", RifeEngine)`, discovered like the
upscaler registry.

### 5.4 `InterpolateStage` — `pipeline/interpolate.py`

Implements the `Stage` protocol (`run(state) -> state`, `core/interfaces.py`).
Reads its input artifact (`"upscaled"` if upscale ran, else `"clip"`), probes
`source_fps` via `ffprobe_fps` on the local materialized input, calls
`resolve_fps_target(source_fps, target_fps, engine.capability)`:

- `skip_gpu` + `decimate_to` set → `decimate_video_fps` locally, no engine call.
- `skip_gpu` + no decimate (exact noop) → passthrough.
- else → upload source to the RIFE pod, call `engine.interpolate` with the plan,
  fetch/record the result.

Always writes `state.artifacts["interpolated"]`. `interpolate_only` standalone
mode consumes the `--video` artifact seeded via `skip_clip_stage=True` +
`initial_clip` (the same seam `upscale` uses, `orchestrator.py:1724-1735`).

### 5.5 Orchestrator wiring — inter-stage materialization

Append `InterpolateStage` conditionally when `cfg.interpolate is not None`
(pattern: `orchestrator.py:1858-1872`), **after** `UpscaleStage`.

Non-obvious consequence: upscale currently stashes `downscale_to` in
`artifact.meta` and the single **post-walk** materialize block
(`orchestrator.py:1911-1969`) fetches bytes + applies the downscale. With interp
appended after upscale on an **independent pod**, the upscaled bytes live on the
FlashVSR pod (proxy URL) and that pod may be `--no-reuse`-destroyed before the
RIFE pod exists. Therefore the orchestrator must **materialize each compute
stage's output before the next compute stage consumes it**:

1. Generalize the post-walk materialize into a reusable
   `materialize_artifact(artifact) -> local bytes` that fetches (http proxy) or
   reads (file://) and applies any stashed `downscale_to`.
2. `InterpolateStage` consumes the materialized (already-downscaled) local input.
   Bonus: RIFE then runs on the smaller downscaled frames — cheaper.
3. The final materialize just publishes `artifacts["interpolated"]` to the sink.

Flow when both stages requested:

```
render → clip (pod A / local)
  → UpscaleStage: 4x on pod, stash downscale_to=1080
  → materialize upscaled: fetch → downscale to 1080 → local file
  → InterpolateStage: upload 1080 clip to RIFE pod, 16→60 fps, fetch result
  → materialize interpolated: publish → local file://…60fps.mp4
delivered = 1080p @ 60fps
```

### 5.6 Config surface — `core/config.py`

Top-level `interpolate:` block, sibling to `upscale:` (pattern:
`config.py:651-694`):

```yaml
interpolate:
  engine: rife
  fps: 60.0
  interpolate_only: false      # standalone mode, like upscale_only
  rife:
    weights_bundle: <HF ref>
    model: rife49
    precision: fp16
```

- `engine: str`, `fps: float` (validate `> 0`), `interpolate_only: bool`,
  engine-specific `rife: RifeEngineConfig | None`.
- `Config.capability_key()` appends `"interpolate"` to the `stages` tuple and adds
  `interpolator` + `interpolator_fps` factors (pattern: `config.py:1266-1285`).

### 5.7 CLI — `cli/_main.py` + `cli/_commands.py`

New `kinoforge interpolate` subcommand (pattern: `_main.py:565-598`,
`_commands.py:646-782`):

```
kinoforge interpolate --video <path|url> --fps 60 \
  [--config c.yaml] [--no-reuse] [--attach-pod POD] [--dry-run]
```

- `--video` required (local path or http(s) URL) → `_resolve_input_video_as_artifact`.
- `--fps` float, overrides `cfg.interpolate.fps`.
- Reuses `skip_clip_stage=True` + `initial_clip` seeding.
- `kinoforge generate` runs render→upscale→interp automatically when both config
  blocks are present.

### 5.8 Pod server endpoints — `engines/diffusers/servers/…`

Mirror the FlashVSR endpoints (`wan_t2v_server.py:1881-2005`):

- `POST /interpolate` — body: `source_url`, `source_fps`, `target_fps`, `plan`
  (the resolved schedule / recursion depth), `engine`, engine-specific `rife`
  params. Enqueues, returns `{"job_id": …}`.
- `GET /interpolate/status/{job_id}` — poll-friendly status.
- `PUT /upload` — shared streaming upload with sha256 verify.
- Result block: `filename`, `sha256`, `size`, `input_fps`, `output_fps`,
  `input_frame_count`, `output_frame_count`, `engine_meta`.

The **stage** computes the plan (it probes `source_fps` locally anyway); the
server just executes it — keeps engine cleverness testable offline.

### 5.9 Errors — `core/errors.py`

No "unsatisfiable" error is needed (any target is reachable). `fps <= 0` is a
config-validation error. A minimal `InterpolationError(KinoforgeError)` base
covers pod/engine failures for parity with the upscale error surface.

## 6. Testing (red/green, per `test-design` skill)

- **`resolve_fps_target`** — exhaustive offline table; each case names the behavior
  and the concrete bug it catches:
  - passthrough (`target == source`) → `skip_gpu`, no decimate, no schedule.
  - decimate-only (`target < source`, e.g. 30→24) → `skip_gpu`, `decimate_to=24`.
  - arbitrary-t exact multiple (`16→32`) → schedule of length ≈2×, `decimate_to=None`.
  - arbitrary-t non-multiple (`16→60`) → schedule hits exactly 60 fps count,
    `decimate_to=None`; asserts fractional timesteps present.
  - arbitrary-t rational (`16→29.97`) → exact 30000/1001 handling.
  - recursive-2x (`16→60`, capability fixture) → `recursion_depth=2` (k=4),
    `decimate_to=60` (asserts it does **not** deliver 64).
- **`ffprobe_fps`** — offline fixtures asserting `16/1 → 16.0` and
  `30000/1001 → 29.97…`.
- **`decimate_video_fps`** — offline injectable-`run`: asserts argv contains
  `fps=24` and temp-file input (NOT `pipe:0`); a real-ffmpeg fixture test asserts
  output fps == target.
- **`InterpolateStage`** — mocked engine: arbitrary-t path builds the correct
  schedule and calls the engine; decimate-only path does **not** call the engine
  and invokes `decimate_video_fps`; passthrough path does neither;
  `interpolate_only` consumes the seeded `--video` artifact.
- **Orchestrator** — interp appended after upscale; `materialize_artifact` applies
  the upscale `downscale_to` before interp consumes the input; final delivered
  artifact is `"interpolated"`; interp-absent config leaves the pipeline unchanged.
- **Live** — RIFE only, one `--no-reuse` smoke on the **interpolate-only fixture
  path** (per CLAUDE.md "prefer the upscale-only path" gotcha — no 70 GB Wan
  download): the 480² 81-frame fixture, `source_fps → 60`, ~$0.08, RIFE pod
  auto-destroyed. Frame-QA per CLAUDE.md (5-frame contact sheet judged for motion
  smoothness, warping/ghosting artifacts, temporal coherence) before reporting
  green. Verify ledger clean via `kinoforge list` after the orchestrator exits.

## 7. Gotchas / non-obvious constraints

- **Source fps is runtime**, not config-time — unknown until the render (or the
  uploaded clip) is materialized. Resolution happens at stage-run time.
- **Ordering forces inter-stage materialization** (§5.5) — the upscale downscale
  must apply before interp; with independent pods the bytes must round-trip
  through the orchestrator between pods regardless.
- **mp4 pipe-seek trap** — `decimate_video_fps` must read from a temp file, not
  stdin (exit 183, commit `8438a8b`). Test with realistic file sizes; a tiny
  fixture hides it.
- **RIFE scene-cut ghosting** — interpolating across hard cuts smears. Fine for
  single-shot generated clips; a real limitation for uploaded multi-cut footage.
- **Exact vs overshoot** — arbitrary-t exact placement yields exactly `target_fps`
  with the fewest synthesized frames and no decimation judder; the recursive
  branch's overshoot-then-decimate is the fallback that keeps future 2×-only
  engines correct.

## 8. Out of scope

- **Co-resident interp + upscale on one pod** — a cost optimization reusing the
  existing Wan↔FlashVSR co-resident pattern; independent pods for v1.
- **FILM / GMFSS-Fortuna / GIMM-VFI engines** — the interface + resolver are built
  to accept them as pure plug-ins; none wired this pass.
- **Native exact-placement fast-paths beyond RIFE**, scene-cut detection,
  slow-motion / retiming UX, motion-blur synthesis.
- **Width-target or resolution changes** in the interp stage — interp changes only
  the time axis; resolution is upscale's job.
