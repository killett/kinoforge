# Standalone image upscaling: `kinoforge upscale --image`

**Status:** design approved 2026-10-03, not yet planned.
**Scope:** upscale one still image (PNG or JPEG) on a pod with the spandrel
engine, through the existing `upscale` command and the existing `upscale:` config
block. One image in, one PNG out.
**Non-scope:** hosted (no-compute) image upscalers, height-target scales for
images, tiled inference on the video path, JPEG or WebP output, more than one
image per invocation, and chaining `image` into `upscale` inside one command
(§11). Each is a separate decision.

Closes the item deferred by
`2026-10-03-standalone-image-generation-design.md` §13.1 and recorded in the
`PROGRESS.md` pointer as "still-image UPSCALING (spandrel already holds the
model; only its I/O is video-shaped)".

## 1. The question, and the answer

*kinoforge can upscale a video with a still-image super-resolution model
(RealESRGAN via spandrel). Why can it not upscale a still image?*

Because every layer between the operator and the model infers "video" by
convention rather than carrying it as data:

| layer | the mp4 assumption |
|---|---|
| `cli/_commands.py` | `upscale` takes `--video` only; `_resolve_input_video_as_artifact` |
| `core/interfaces.py:989` | `UpscaleJob` docstring: "upscaling is video-in / video-out" |
| `engines/_pod_http.py:191` | `_upload_source` sends `Content-Type: video/mp4`, `X-Filename: <sha8>.mp4` |
| `servers/wan_t2v_server.py` `PUT /upload` | rejects any content type but `video/mp4` with 415 |
| `servers/wan_t2v_server.py` `UpscaleRequest` | no media field; `_run_upscale_job` calls `pipe.upscale` unconditionally |
| `upscalers/spandrel/_runtime.py` | `iio.imread(video_path, plugin="FFMPEG")`, batches frames, writes `<stem>.upscaled.mp4` |
| `core/orchestrator.py` materialize block | four `sink.publish(..., extension=".mp4")` calls |

The model itself is image-shaped: spandrel loads genuine still-image SR
architectures, and the live-proven weights (`RealESRGAN_x2.pth`, log entries
12 and 34) are a still-image model applied frame by frame.

**The answer is one explicit media value, threaded through the three seams
that already exist, plus one new runtime method.** Nothing new is provisioned,
no launch golden moves, and the spandrel example config runs unmodified for
both inputs.

### 1.1 Why pod-side spandrel and not a hosted endpoint

A hosted image upscaler (fal `esrgan` / `clarity-upscaler`, Replicate
`real-esrgan`) would be seconds and a cent per image with no compute, and it
would match the `image` command's no-compute contract. It was considered and
deferred, because it is a different feature: a new provider endpoint needing
its own live proof, an input-image shipping seam that nothing in the tree has
yet (the §13.2 gap), and no analogue for FlashVSR or SeedVR2. This design
finishes the item the previous spec actually named, reusing a model, a
server and a compute seam that are all live-proven. The pod cost — roughly
3-10 min of boot and $0.08-0.40 cold — is the accepted price; warm-reuse
amortises it across a shell loop of images exactly as it does for clips.

### 1.2 Why `upscale --image` and not a new subcommand

`_cmd_upscale` and `_cmd_interpolate` already carry one copy each of the
warm-reuse / attach / launch-row / ledger-stamp chain. An `upscale-image`
subcommand would be a third. The capability is "make this bigger"; the input
kind is a flag on it. Sniffing the kind from the filename (`--input PATH`) was
rejected because a mis-suffixed file would cost a 10-minute boot before the
error.

## 2. Surface

```
kinoforge upscale -c <cfg> (--video PATH_OR_URL | --image PATH)
                  [--scale Nx] [--no-reuse | --attach-pod ID]
                  [--run-id ID] [--dry-run]
```

`--video` and `--image` form a **required mutually exclusive group**; every
other flag keeps its meaning. `--image` is a local path only in v1 — the
`http(s)://` passthrough that `--video` allows is not offered, because the pod
would then infer the media kind from a URL it has not fetched yet. Accepted
suffixes: `.png`, `.jpg`, `.jpeg`.

### 2.1 Preflight refusals — all exit 2, all before any pod work

Ordered as they run. Each message names the offending thing.

1. `--no-reuse` with `--attach-pod` (existing).
2. `--scale` malformed or a height target (existing; the height refusal now
   also covers a height target in `cfg.upscale.scale` when `--image` is set,
   since spandrel refuses those anyway and an image never reaches the
   downscaler — §6).
3. `--image` with an engine whose `supports_image_input` is false (§3.2).
4. `--image` with `cfg.upscale.chunk_frames` or `cfg.upscale.tile_grid` set.
   Both are temporal/spatial *video* splits in `UpscaleStage`; a still is
   tiled on the pod instead (§5.3).
5. `--image` path empty, missing, a directory, or an unaccepted suffix.

`--dry-run` prints the existing plan plus `media: image|video` and returns 0
before any of the store, the registry engine or an HTTP seam is constructed.

### 2.2 Exit codes

Unchanged convention: 2 config/precondition, 1 operational, 0 success and
dry-run.

## 3. The media kind travels as data

### 3.1 `UpscaleJob.media`

```python
@dataclass(frozen=True)
class UpscaleJob:
    source: Artifact
    scale: ScaleTarget
    params: dict = field(default_factory=dict)
    media: Literal["video", "image"] = "video"
```

Defaulted so every existing construction in `src/` and `tests/` is untouched.
The docstring's "video-in / video-out" line is corrected.

### 3.2 `UpscalerEngine.supports_image_input`

A class attribute on the ABC with a default of `False`:

```python
class UpscalerEngine(ABC):
    ...
    supports_image_input: bool = False
```

`SpandrelEngine` sets it `True`. FlashVSR and SeedVR2 are not edited and
refuse by default — FlashVSR's causal temporal model has no meaning for one
frame, and SeedVR2 is extras-gated. The CLI preflight (§2.1 item 3) consults
it; so does the pod server (§5.2) as defence in depth.

### 3.3 The artifact carries its media

`_resolve_input_video_as_artifact` becomes
`_resolve_input_as_artifact(path_or_url, media)` and stamps
`meta["media"] = media` on the returned `Artifact`. `Artifact.meta` is already
the cross-layer channel `UpscaleStage` and the orchestrator use for
`downscale_to` and `materialize`, so this follows precedent rather than adding
a parameter to `orchestrator.generate`.

A small pure helper owns the convention so no layer re-implements the
default:

```python
# core/media.py
Media = Literal["video", "image"]
def media_of(artifact: Artifact) -> Media:
    """Return the artifact's media kind; absent means video."""
```

### 3.4 `UpscaleStage`

Reads `media_of(clip)` in `_engine_call` and builds
`UpscaleJob(source=clip, scale=scale, media=...)`. Nothing else in the stage
changes: with `--image` the preflight has already refused chunking, tiling and
height targets, so an image runs exactly the factor / no-chunk / no-tile
branch, which calls the engine directly and probes nothing on the controller.
`UpscaleResult.artifact` from the engine carries the same `meta["media"]`
forward so the orchestrator can read it.

## 4. Client side: `SpandrelEngine` and the upload seam

### 4.1 `_pod_http._upload_source`

Gains a `media` parameter. For `"video"` the header pair is unchanged
(`video/mp4`, `<sha8>.mp4`). For `"image"` the content type and suffix follow
the local file's suffix: `.png` → `image/png`, `.jpg`/`.jpeg` → `image/jpeg`,
filename `<sha8><suffix>`. The sha256 cross-check and the one-shot 502
recovery are unchanged.

### 4.2 `SpandrelEngine.upscale`

Passes `job.media` into `_upload_source` and adds `"media": job.media` to the
`/upscale` payload. `validate_spec`, `model_identity`, `render_provision` and
the embed set are untouched: **however, the spandrel configs' launch goldens
and `_BASELINE_BYTES` entries do move, deliberately, in Task 8 — the
`_engine.py`/`_runtime.py` growth from this and the sibling tasks rides onto
the pod because those two files are embedded whole.** The returned
`UpscaleResult.artifact` gets `meta={"media": job.media}`.

## 5. Pod side

### 5.1 `PUT /upload`

Accepts `image/png` and `image/jpeg` in addition to `video/mp4`. The
sanitised-basename rule, the size cap, the atomic `os.replace` and the
`_maybe_cleanup_upload` unlink are unchanged. Any other content type is still
415.

### 5.2 `POST /upscale`

`UpscaleRequest` gains `media: Literal["video", "image"] = "video"`. The
handler refuses `media="image"` with any engine other than `spandrel` with
HTTP 400 **at submit**, mirroring the existing unknown-engine refusal, so the
controller sees the error on its first poll rather than after a model load.
`_run_upscale_job` dispatches:

```python
method = entry["pipe"].upscale_image if req.media == "image" else entry["pipe"].upscale
out_path = await asyncio.to_thread(method, local, scale, params)
```

`_download_to_local_temp` is keyed on the uploaded filename and is
suffix-agnostic. `_probe_resolution` runs ffprobe, which reads a PNG or JPEG
as a one-frame video stream, so `input_resolution` / `output_resolution` stay
honest for images.

### 5.3 `SpandrelRuntime.upscale_image`

```python
def upscale_image(self, image_path: Path, scale: ScaleTarget, params: dict[str, Any]) -> Path:
    """Upscale one still; returns ``<stem>.upscaled.png``."""
```

- Same two refusals as `upscale`: height target → `NotYetImplementedError`,
  `scale.value != self._scale` → `UnsupportedScaleError`.
- Reads with imageio's default (Pillow) plugin. An alpha channel is dropped
  with one log line; greyscale is broadcast to RGB. Output is always RGB PNG,
  lossless, regardless of input format.
- **Honours `tile_size` for the first time.** The still is cut into
  `tile_size`-square tiles with a fixed 32 px overlap on every interior edge;
  each tile is inferred alone; the output canvas is assembled from tile
  *interiors* (overlap discarded) at `scale` times the tile geometry. Right
  and bottom remainder tiles are handled by clamping the window to the image
  edge, not by padding. A still no larger than `tile_size` in both dimensions
  goes through in one call.
- The device / dtype / tensor conversion block currently inline in `upscale`
  is extracted into one private `_infer(batch_np) -> np.ndarray` helper that
  both methods call, so the two paths cannot drift. **The video path's
  behaviour is unchanged**: it stays untiled, because every live spandrel
  entry was proven on it at 480² frames, and tiling it is not this feature.
- The class docstring's "spandrel handles tiling internally" sentence and the
  `SpandrelEngineConfig.tile_size` docstring are corrected to say the image
  path tiles and the video path does not.

## 6. Orchestrator publish

In the materialize block (`orchestrator.py` ~3072-3100) the hardcoded
`extension=".mp4"` on the `fullres` and `upscaled` publishes becomes
`extension=extension_for(media_of(upscaled))` — `.png` for image, `.mp4` for
video. The `interpolated` publish stays `.mp4` (interpolation is video by
construction). The published name is therefore

```
output/20261003-181200_upscaled_spandrel_spandrel-realesrgan-fp16_upscale.png
```

The `downscale_to` branch cannot be reached with an image: spandrel refuses
height targets at `validate_spec`, and §2.1 refuses them at preflight. It gets
a defensive `raise` naming the contradiction rather than feeding PNG bytes
through `finalize_upscaled_bytes`, which is an ffmpeg mp4 pipeline.

## 7. Testing

Offline tests ride the existing seams: the monkeypatched
`spandrel._engine._http_json`, the server's FastAPI test client, and a stub
spandrel model. Per the `test-design` skill each test states the behaviour
under test and the concrete bug that would make it fail.

| behaviour | assertion | bug it catches |
|---|---|---|
| `UpscaleJob.media` default | every existing `UpscaleJob(...)` test stays green untouched; a bare construction reports `"video"` | a required field breaks the whole video suite |
| `supports_image_input` | iterate `registry` upscaler names: only `spandrel` is true (guard-the-guard: assert ≥3 names were iterated) | a future engine inherits "yes" by accident; a sweep over nothing passes |
| preflight refusals | one case per §2.1 item 3-5, each asserting stderr names *that* thing and exit 2 | refusing everything for the wrong reason |
| dry run | `--image --dry-run` prints `media: image`; store / registry / HTTP seams record zero constructions | a dry run that boots a pod |
| stage threading | the engine's received `UpscaleJob.media == "image"` when the seeded clip carries the meta | the flag parses and never reaches the pod |
| client upload + payload | image artifact → upload headers `image/png` + `.png` suffix; submit payload has `"media": "image"` | the pod still receives a "video" |
| server submit refusal | `media=image` + `engine=flashvsr` → 400 and the `_ensure_on_gpu` counter is **zero** | refusal after a model load |
| server dispatch | `media=image` calls `pipe.upscale_image`, not `pipe.upscale` | image bytes fed to the FFMPEG reader |
| `/upload` content types | `image/png` body stored under a `.png` name; `text/plain` still 415 | the gate widened to everything |
| runtime shape | stub 2× model: 100×70 RGBA → 200×140 RGB `.upscaled.png` | alpha reaches the model; wrong suffix |
| runtime tiling | 1100×900 input, `tile_size=512`: output equals the untiled stub result pixel-for-pixel | seams, off-by-one at right/bottom remainders |
| runtime video path | existing `tests/upscalers/test_spandrel_runtime.py` green | the `_infer` extraction changed the video path |
| publish extension | image artifact → `.png`; video → `.mp4`; image + `downscale_to` → raises | PNG named `.mp4`; PNG through the mp4 downscaler |

### 7.1 Live fire

Exactly one run, after the RED scaffold is committed and `pixi run preflight`
exits 0:

```
pixi run kinoforge upscale \
  -c examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml \
  --image output/20261003-174427_image_luma_agents_uni-1_Photorealistic-cinem.png \
  --no-reuse
```

Input is the §35 `kinoforge image` PNG (2672×1504); expected output 5344×3008.
Budget ~$0.10-0.20. Utilisation is polled every 60-90 s per CLAUDE.md.
Visual QA is a side-by-side read of input and output PNGs — detail
preserved, no tile seams, no colour shift — recorded in a **new**
`successful-generations.md` section, because "image upscale" is a new mode
axis. Afterwards `kinoforge list` must show both `No running instances.` and
`No instances recorded in ledger.`

This run is also the first `image` → `upscale` chain, which §5 of the image
design predicted would be "file hand-off between two commands".

## 8. Docs

- `docs/engines.md` §Upscalers: an "Image input" paragraph under `spandrel`;
  the corrected `tile_size` story.
- `docs/configuration.md` `upscale:` section: `--image`, the accepted suffixes,
  the §2.1 refusals.
- `README.md` command table: `upscale` — "Upscale a video clip or a still image".
- `PROGRESS.md`: pointer, plan path, checklist, next action.

## 9. Module-by-module summary

| file | change |
|---|---|
| `core/interfaces.py` | `UpscaleJob.media` (default `"video"`); `UpscalerEngine.supports_image_input = False`; docstring fix |
| `core/media.py` | **new** — `Media`, `media_of()`, `extension_for()` |
| `cli/_main.py` | `--image` in a required mutex group with `--video` on `upscale` |
| `cli/_commands.py` | `_resolve_input_as_artifact(path, media)`; §2.1 refusals; dry-run `media:` line |
| `pipeline/upscale.py` | `_engine_call` passes `media_of(clip)` |
| `engines/_pod_http.py` | `_upload_source(..., media)` content type + suffix |
| `upscalers/spandrel/_engine.py` | `supports_image_input = True`; media into upload + payload; result meta |
| `upscalers/spandrel/_runtime.py` | `upscale_image()`; `_infer()` extraction; tiling on the image path; docstrings |
| `servers/wan_t2v_server.py` | `/upload` content types; `UpscaleRequest.media`; submit refusal; dispatch |
| `core/orchestrator.py` | extension by media; defensive raise on image + `downscale_to` |
| docs | `engines.md`, `configuration.md`, `README.md`, `PROGRESS.md` |

Two new fields with defaults, one new method, one new 20-line module, zero
config changes — but the spandrel configs' launch goldens and `_BASELINE_BYTES`
do move, deliberately, in Task 8, because the spandrel package they embed
whole grew.

## 10. Open question the plan must verify, not assume

**Does `tests/providers/test_pod_embed_closure.py` accept the server importing
nothing new?** §5 adds no import to `wan_t2v_server.py` (the dispatch is an
attribute lookup on the loaded pipe), and `_runtime.py` adds no import outside
`imageio`/`numpy`/`torch`, which are already there. The plan runs that guard
and `test_env_payload_ceiling.py` first, before any live spend, because the
spandrel example config's payload sits under the ceiling and the server
module is embedded by it.

## 11. Non-scope, and why each is separate

**11.1 Hosted image upscalers.** §1.1. Needs an input-image shipping seam and
a per-provider live proof; the first real instance of the previous spec's
§13.2.

**11.2 Height targets for images.** `--scale 1080p` on a still would need a
still-image downscaler beside `finalize_upscaled_bytes`; spandrel refuses
height targets for video too, so this waits for both at once.

**11.3 Tiling on the video path.** The §5.3 tiler is image-only by decision.
Applying it per frame would change a live-proven path for no current need.

**11.4 JPEG/WebP output.** Lossless PNG is the only honest default for an SR
output; a format knob is a config change this design does not make.

**11.5 Several images per invocation.** As with `image` §2.3: a shell loop
with `--attach-pod` costs what a batched call would, and the command's
contract stays one input, one output, one exit code.

**11.6 `image` then `upscale` in one command.** Two pods' worth of lifecycle
in one invocation; file hand-off between the two commands is the proven
shape (§7.1 does exactly this).
