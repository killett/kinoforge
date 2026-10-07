# Directory image upscaling: `kinoforge upscale --image-dir`

**Status:** design approved 2026-10-06, not yet planned.
**Scope:** upscale every image under a directory, in any format Pillow can
open, on ONE spandrel pod held for the whole run, writing lossless PNGs into a
sibling directory named `<dir>_upscaled` that mirrors the source tree.
**Non-scope:** a shared tiler between the video and image paths (§3.3),
concurrency on the controller, an overwrite flag, output formats other than
PNG, height-target scales for images, hosted (no-compute) image upscalers, and
upscaling a directory of videos. Each is a separate decision.

Closes §11.5 ("several images per invocation") of
`2026-10-03-standalone-image-upscaling-design.md`, which deferred it to a
shell loop with `--attach-pod`.

## 1. The question, and the answer

*`kinoforge upscale --image` upscales one PNG or JPEG per call and pays a
3-10 minute pod boot to do it. How does an operator upscale a folder of
photos in mixed formats without paying that boot per file, and without
writing a shell loop?*

**The answer is one new flag on the existing command, one pure planning
module, and one session runner in the shape of `core/batch.py`.** The pod is
created or attached once; `UpscaleStage` runs once per image inside that
session; `--no-reuse` destroys the pod once, at the end, because the destroy
already lives in `deploy_session`'s `finally`. Nothing pod-side changes: the
controller converts every input to the PNG/JPEG bytes the pod already
accepts.

### 1.1 Why a flag and not a subcommand

`_cmd_upscale` and `_cmd_interpolate` each carry one copy of the warm-reuse /
attach / launch-row / ledger-stamp chain. A subcommand would be a third copy.
The capability is still "make this bigger"; the input kind is a flag on it.
`--image-dir` joins `--video` and `--image` in the required mutually
exclusive source group.

### 1.2 Why one session and not a loop over `generate()`

`deploy_session`'s docstring states the contract: "per-request work lives at
the call site so the setup cost amortises across many entries."
`batch_generate` already uses it that way for prompts. A loop over
`generate()` would re-enter the session per image — provision probe, profile
verify, pool build, a fresh heartbeat loop with its claim lock and first-tick
wait, and a page of instance-overview logging — and `--no-reuse` would need
its own try/finally in the CLI because the session's one-shot destroy only
knows about one image. The runner (§4) gets the destroy for free.

### 1.3 Why controller-side conversion

The pod's `PUT /upload` accepts `video/mp4`, `image/png` and `image/jpeg`
and nothing else, and `SpandrelRuntime.upscale_image` reads with imageio's
Pillow plugin, which does not apply EXIF orientation and refuses non-8-bit
input. Widening the pod would move every spandrel launch golden and payload
baseline and need a live re-proof of a path proven on 2026-10-03. Converting
on the controller touches no pod code, and Pillow 12 with WebP and AVIF is
already in the default environment (transitively, through imageio — this
design declares it).

## 2. Surface

```
kinoforge upscale -c <cfg> --image-dir DIR
                  [--scale Nx] [--no-reuse | --attach-pod ID]
                  [--run-id ID] [--dry-run]
```

The output directory is the resolved input path with `_upscaled` appended to
its last component: `photos` and `photos/` both give the sibling
`photos_upscaled`. Every other flag keeps its meaning. The default leaves the
pod warm at the end, exactly as `--image` does; `--no-reuse` destroys it once
the whole directory is done.

`--output-dir` and `--no-output-dir` are ignored with a one-line stderr note
when combined with `--image-dir`: the destination is defined by the input
directory, and nothing is written to the timestamped `output/` tree.

### 2.1 Preflight refusals — all exit 2, all before any pod work

Ordered as they run. Each message names the offending thing.

1. `--no-reuse` with `--attach-pod` (existing).
2. `--scale` malformed or a height target (existing).
3. The engine lacks `supports_image_input`, or `cfg.upscale.chunk_frames` /
   `cfg.upscale.tile_grid` is set (existing `--image` check, reused).
4. `--image-dir` empty, missing, or not a directory.
5. The walk finds no file with a recognised suffix (§3.1). An empty
   directory is almost always the wrong path, and it should cost nothing.
   Unreadable and oversize files DO count as found: they are failures, not
   absence.

Two outcomes that are NOT refusals:

- Every image already has an output: print `nothing to do`, exit 0, no
  ledger row, no pod.
- Files the megapixel guard rejects, or that Pillow cannot open, are
  recorded as per-file failures at plan time and never reach a pod; the run
  proceeds with the rest and exits 1 at the end (§5.3).

### 2.2 One new config field

`upscale.max_output_megapixels: int = 256`, validated `> 0`. It bounds
`width × height × scale²` of the OUTPUT. It also applies to single `--image`
runs, where an oversize file is an exit-2 refusal naming the cap, so the two
paths agree.

Why output pixels, and why this is the real large-image risk: the pod-side
tiler (§3.3) already bounds GPU memory per inference at roughly
`(tile_size + 64)²` whatever the image size. What is NOT bounded is the
full-size output canvas in the pod's host RAM and the PNG encode. A 50 MP
photo at 4x is an 800 MP canvas — about 2.4 GB raw and a PNG over a
gigabyte. A host-RAM kill takes the server process, therefore the pod, and
with it the rest of the directory run. The guard refuses on the controller
and the pod is never asked.

### 2.3 Dry run

`--dry-run` prints the existing plan header plus `source_dir`, `output_dir`,
`media: image`, then one line per discovered image with its disposition
(`pending`, `exists`, `oversize`, `unreadable`, and `renamed` for a
collision), then the count line of §5.3, and exits 0. Planning reads image
headers only (§3.4), so a dry run over thousands of files is fast. The store,
the registry engine and every HTTP seam record zero constructions.

### 2.4 Exit codes

Unchanged convention: 2 config/precondition, 1 operational (any item failed
or aborted, pod dead, batch-fatal), 0 success, dry run, and nothing-to-do.

## 3. Planning: `core/image_dir.py`

A pure module owns the walk, the naming, the guard and the conversion so the
CLI and the runner both consume a finished plan and neither re-derives a
rule.

```python
def output_dir_for(source_dir: Path) -> Path
def plan_image_dir(
    source_dir: Path, *, scale: int, max_output_megapixels: int
) -> ImageDirPlan
def prepare_upload(item: ImageDirItem, scratch: Path) -> Path
```

```python
@dataclass(frozen=True)
class ImageDirItem:
    source: Path                 # absolute
    output: Path                 # absolute, always .png
    width: int                   # as viewed (orientation applied)
    height: int
    disposition: Literal["pending", "exists", "oversize", "unreadable"]
    reason: str | None = None    # set for oversize / unreadable
    renamed: bool = False        # collision rule applied (§3.2)

@dataclass(frozen=True)
class ImageDirPlan:
    source_dir: Path
    output_dir: Path
    items: tuple[ImageDirItem, ...]   # sorted walk order
    skipped_non_image: int
    # properties: pending, exists, failed_at_plan
```

### 3.1 The walk

Recursive, sorted for a deterministic order, symlinks not followed, names
beginning with a dot skipped at every level. Recognised suffixes are a
curated set, `DIR_IMAGE_SUFFIXES` in `core/media.py` beside the existing
pod-accepted trio `IMAGE_SUFFIXES`:

```
.png .jpg .jpeg .jfif .webp .avif .gif .bmp .dib .tif .tiff .tga
.heic .heif .jp2 .j2k .psd .ico .pcx .pbm .pgm .ppm .pnm .qoi .dds
```

Deliberately NOT "everything Pillow registers": that list includes `.mpg`,
`.pdf`, `.h5`, `.ps` and `.eps`. Any other suffix is counted in
`skipped_non_image`, reported once, and never refused. A recognised file
Pillow cannot open becomes an `unreadable` item with the exception text as
its reason.

### 3.2 Naming

The relative path is mirrored under the output directory with the suffix
replaced by `.png`. Within one folder, sources whose stems collide (exact
comparison) keep their full original name plus `.png`: `a.webp` and `a.png`
become `a.webp.png` and `a.png.png`, `renamed=True`, logged once per folder.
A same-stem pair in different folders is not a collision. An output that
already exists is `exists` and is never overwritten; an overwrite flag waits
for someone to need it. Because `exists` is decided per file, a run that died
mid-way is restarted by running the same command again and pays only for
what is missing.

### 3.3 Sizing and the guard — and why the tilers stay separate

Planning opens each file lazily with Pillow, reads `size` and the EXIF
orientation tag, and closes it; no pixels are decoded. Width and height are
swapped for the 90° orientations (5, 6, 7, 8), so the guard and the dry run
report the image as it is viewed. The item is `oversize` when
`width × height × scale²` exceeds `max_output_megapixels × 10⁶`.

Two tilers exist and both stay as they are:

| | video path | image path |
|---|---|---|
| where | controller, `pipeline/tile.py` | pod, `spandrel/_runtime.py::tile_upscale` |
| medium | ffmpeg crops of an mp4 | numpy slices of one array |
| geometry | operator `tile_grid: [cols, rows]`, min overlap 32 | `tile_size` squares, 32 px context |
| seam | feather-blend across the actual overlap | overlap discarded, interior copied |
| trigger | explicit config | automatic: any image larger than `tile_size` |

They blend differently because the models differ. FlashVSR is generative and
its tiles can disagree at the edges, so the video path feathers. RealESRGAN
is a deterministic convolutional model: with context wider than its
receptive field the tile interior is exactly what a whole-image pass would
produce, and `tests/upscalers/test_spandrel_runtime_image.py` asserts tiled
equals untiled pixel for pixel. Feathering there would fix nothing. The two
share about fifteen lines of axis arithmetic; a shared module would sit in
the spandrel pod embed set (moving launch goldens and payload baselines) and
rewrite a live-proven video path for identical maths. Decided 2026-10-06:
not worth it. The example spandrel config already sets `tile_size: 512`, so
the auto-detection the feature needs exists.

### 3.4 Conversion: `prepare_upload`

The pod accepts PNG and JPEG bytes, 8-bit, and reads them without applying
EXIF orientation — a phone portrait would come back lying on its side. The
rule:

- A PNG or JPEG whose Pillow mode is `RGB`, `RGBA` or `L` and whose
  orientation is the identity is uploaded byte for byte; the returned path
  is the source, so its sha256 is the original's.
- Everything else is opened, `ImageOps.exif_transpose`d, converted to 8-bit
  `RGB`, and written as `<sha8>.png` (the first 8 hex digits of the
  source's sha256) under the per-run scratch directory,
  which the runner removes at the end.
- Animated GIFs and multi-page TIFFs contribute frame 0, logged.
- Alpha is dropped on the controller for converted files and on the pod
  (its existing `_to_rgb`) for passthrough RGBA PNGs; both yield RGB.

### 3.5 Dependencies

`pillow` and `pillow-heif` are added to `pixi.toml` from conda-forge (both
present: pillow 12.3, pillow-heif 1.8). Today Pillow is reachable only
transitively through imageio. HEIF support is registered lazily inside
`core/image_dir.py` on first use; if the import fails the `.heic`/`.heif`
suffixes stay recognised and such files become `unreadable` with a reason
naming `pillow-heif`.

## 4. The session runner: `core/upscale_dir.py`

In the shape of `core/batch.py`: one `deploy_session`, per-item work inside.

```python
def upscale_image_dir(
    cfg: Config,
    plan: ImageDirPlan,
    *,
    store: ArtifactStore,
    run_id: str,
    state_dir: Path,
    instance: Instance | None = None,
    cancel_token: CancelToken | None = None,
    single: bool = False,
    on_instance_created: Callable[[Instance], None] | None = None,
    on_item: Callable[[ImageDirItem, ItemOutcome], None] | None = None,
    provider: ComputeProvider | None = None,   # test injection
    engine: UpscalerEngine | None = None,      # test injection
) -> tuple[ImageDirResult, Instance | None]
```

Inside the session, in order:

1. Register the store with `EphemeralSession` if one is active, as
   `generate` does.
2. Build ONE `UpscaleStage` with the registry upscaler (or the injected
   one), the parsed scale, `session.instance`, `cfg` as a dict, the cancel
   token, and no chunking or tiling — preflight (§2.1 item 3) guaranteed
   that.
3. Create a per-run scratch directory for converted inputs; removed when the
   session closes, success or not.
4. For each `pending` item: `prepare_upload`; build the input `Artifact`
   with `meta["media"] = "image"`; seed a `PipelineState` with it under
   `"clip"` and the placeholder `GenerationRequest(prompt="", mode="upscale")`
   `generate` uses on the upscale-only path; run the stage; fetch the
   upscaled bytes; write them to `item.output` atomically (parents created,
   temp file then `os.replace`); report through `on_item`.

### 4.1 Two helpers move so nothing is duplicated

- The local-file branch of the CLI's `_resolve_input_as_artifact` (sha256
  plus size stamping) becomes `local_artifact(path, media)` in
  `core/media.py`; the CLI delegates to it.
- The "read the upscaled artifact's bytes from the pod proxy URL or a
  `file://` path" block in `generate`'s materialize step becomes
  `fetch_artifact_bytes(artifact)` in `core/orchestrator.py`, called by both.
  This is the only edit to `generate`.

### 4.2 Failure handling

- A per-item exception is recorded as `failed` with the exception's message;
  the run continues.
- After ANY per-item failure the runner sends one `GET /health` to the pod
  through the existing `engines/_pod_http.http_json` seam. No answer →
  remaining items `aborted`, a pod-dead error naming the pod is raised. This
  is the difference between one failed file and paying an upload timeout per
  remaining file against a corpse.
- `BudgetExceeded`, `CapabilityMismatch` and `TeardownError` are batch-fatal
  as in `batch_generate`: remaining items `aborted`, exception re-raised.
- `KeyboardInterrupt` and `Cancelled` mark the rest `aborted` and re-raise.
  The warm pod survives, with the same single WARN naming it that `generate`
  logs. With `--no-reuse` the session's own `finally` destroys it.

`single=True` therefore means "destroy once, at session exit, whatever
happened" — the `--no-reuse` contract — and costs the runner no code.

### 4.3 The result

```python
ItemOutcome = Literal["written", "failed", "aborted"]

@dataclass(frozen=True)
class ImageDirResult:
    plan: ImageDirPlan
    outcomes: tuple[tuple[ImageDirItem, ItemOutcome, str | None], ...]
    # properties: written, failed, aborted (counts)
```

Items are processed sequentially. The pod serves `/upscale` through a single
job worker, so controller-side concurrency would only queue.

## 5. CLI wiring: `_cmd_upscale`

### 5.1 The branch

The `--image-dir` branch shares everything up to and including the engine
preflight with `--image`, then diverges after the dry-run block:

1. Build the plan with the resolved scale and
   `cfg.upscale.max_output_megapixels`.
2. Print the plan summary: source and output directories, found, pending,
   exists, oversize, unreadable, non-image skipped. Oversize and unreadable
   items are listed by name with their reason.
3. No pending items: print `nothing to do`, return 0, no ledger row, no pod.
4. Resolve the instance exactly as today: `--attach-pod`, else the warm scan
   unless `--no-reuse`, then the pre-create launch-row reservation.
5. Call `upscale_image_dir(...)` with `single=bool(no_reuse)` and the same
   `on_instance_created` hook, then the same ledger stamp / settle-unused-row
   logic the single-image path runs. That block is lifted into
   `_finish_launch_row(ctx, cfg, launch, instance, returned_instance,
   no_reuse)` so `--video`, `--image` and `--image-dir` share one copy.

### 5.2 Progress lines

One stdout line per image as it completes, via `on_item`:

```
[12/47] sub/b.webp -> sub/b.png (2672x1504 -> 5344x3008)
[13/47] sub/c.tif FAILED: <reason>
```

Logging keeps its current channel.

### 5.3 Closing summary

```
upscaled 44, skipped 3 existing, failed 2, aborted 0 -> photos_upscaled/
```

followed by one line per failed or aborted item naming it and the reason.
`failed` counts plan-time failures (`oversize`, `unreadable`) and run-time
failures together. Exit 0 when nothing failed or aborted, else 1. A pod-dead abort or a
batch-fatal error prints its message and exits 1 AFTER the summary, so the
operator still sees which files were written.

## 6. Testing

Offline tests ride the existing seams: stub `UpscalerEngine` and
`ComputeProvider` injected the way the upscale-only `generate` tests do, a
monkeypatched `_pod_http.http_json` for the health probe, and tiny fixture
images written by Pillow at test time. Per the `test-design` skill each test
names the behaviour under test and the concrete bug that would make it fail.

| behaviour | assertion | bug it catches |
|---|---|---|
| output dir naming | `photos`, `photos/`, and a resolved absolute path all map to the sibling `photos_upscaled` | a trailing slash yields `photos/_upscaled` or an empty name |
| recursive walk | nested files appear with mirrored relative output paths; dotfiles and a symlinked directory are absent; the order is stable across two calls | subdirectories dropped, loops followed, flaky ordering |
| suffix set | every suffix in `DIR_IMAGE_SUFFIXES` has a registered Pillow opener after HEIF registration, and `.mpg` / `.pdf` are not in the set; ≥ 20 suffixes were iterated | a typo'd suffix no file ever matches; the sweep passing on an empty set |
| non-image files | a `.txt` and a `.mp4` raise the skipped count and never become items | a refusal or an `unreadable` item for a text file |
| collision naming | `a.webp` + `a.png` in one folder give `a.webp.png` + `a.png.png`; `a.webp` alone gives `a.png`; a same-stem pair in different folders does not rename | renaming always, never, or across folders |
| exists | a pre-existing output marks the item `exists` and it is absent from `pending` | re-upscaling and overwriting on every run |
| orientation | a JPEG with EXIF orientation 6 reports swapped width/height, and `prepare_upload` returns a transposed PNG whose pixels match the fixture rotated | a portrait upscaled on its side |
| passthrough | an RGB PNG with identity orientation comes back as the same path, byte-identical; a 16-bit PNG and an RGBA WebP come back as new 8-bit RGB PNGs | re-encoding everything, or shipping 16-bit bytes the pod rejects |
| first frame | a two-frame GIF yields a PNG equal to frame 0 | the last frame, or a Pillow error |
| megapixel guard | `width × height × scale²` just over the cap is `oversize`, just under is `pending`; the config field rejects 0 and negatives | an off-by-one, or a guard on input pixels |
| single `--image` guard | an oversize `--image` exits 2 naming the cap | the two paths disagreeing |
| runner session count | the stub provider's `create_instance` is called once for three pending items | one pod per image |
| runner writes | three outputs exist with the stub's bytes, parents created, scratch directory gone afterwards | missing `mkdir`, leaked temp files |
| per-item failure | item 2 raises; items 1 and 3 are written; result has one `failed`; `/health` was probed exactly once | stop on first failure, or no probe |
| pod dead | the health probe raises after a failure; remaining items `aborted`, the raised error names the pod, no further `upscale` calls | paying a timeout per remaining file |
| no-reuse destroy | `single=True` destroys exactly once at exit, including when an item failed | destroy per item, or none after a failure |
| cancel | `Cancelled` from the stage marks the rest `aborted`, re-raises, and the stub pod is NOT destroyed when `single=False` | a cancel that kills a warm pod |
| CLI dry run | prints per-file dispositions and counts; store, registry engine and HTTP seams record zero constructions | a dry run that boots a pod |
| CLI nothing to do | all outputs present prints `nothing to do`, exit 0, no ledger row | a pod for zero work |
| CLI exit codes | one failed item → exit 1, summary line, file named; clean run → 0; missing directory → 2 | a failure reported as success |
| CLI launch row | cold create stamps the ledger once; attach stamps nothing; `_finish_launch_row` is the one helper both `--image` and `--image-dir` call | a fourth copy drifting |
| output-dir note | `--image-dir` with `--output-dir` prints the stderr note and writes nothing under it | files silently landing in `output/` |
| embed and payload guards | `tests/providers/test_pod_embed_closure.py` and `test_env_payload_ceiling.py` stay green | a controller-side import leaking onto the pod |

Nothing in this feature touches pod-side code, so NO launch golden or payload
baseline is expected to move. The plan asserts that with
`tools/snapshot_launch_payloads.py` before the live fire; a moved golden is a
defect, not a regeneration.

### 6.1 Live fire

Exactly one run, after the RED scaffold is committed and `pixi run preflight`
exits 0, on the spandrel x2 config already live-proven for `--image`:

```
pixi run kinoforge upscale \
  -c examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml \
  --image-dir /workspace/output/dir-smoke \
  --no-reuse
```

The fixture directory is built by a committed script under `tests/live/` so
the proof is reproducible, and exercises every branch in one pod's life:

- the §35 Luma UNI-1 PNG (2672×1504) — passthrough
- the same image saved as WebP and as AVIF — conversion
- the same image as a JPEG with EXIF orientation 6 — rotation
- a nested `sub/` with a small animated GIF and an RGBA PNG
- a `notes.txt` — counted as skipped
- a synthetic 12000×12000 WebP — the oversize guard refusing at plan time
  with zero pod effect
- one output pre-placed in `dir-smoke_upscaled/` — the `exists` skip

Expected: six written, one existing, one oversize, zero failed, exit 0, pod
destroyed. Budget ~$0.15-0.30 — a 512-tile x2 pass over a 4 MP still takes
seconds, so the cost is almost all boot. Utilisation is polled every 60-90 s
during boot and run per CLAUDE.md.

Visual QA is a contact sheet per output beside its input: detail preserved,
no tile seams, no colour shift, and the rotated JPEG upright at 5344×3008
rather than 3008×5344. The GIF and RGBA cases are checked for frame 0 and for
alpha dropped cleanly. The verdict goes into a NEW `successful-generations.md`
section (directory mode is a new axis) with a "See also" under §36.

Afterwards `kinoforge list` must show both `No running instances.` and
`No instances recorded in ledger.`

## 7. Docs

- `docs/configuration.md` `upscale:` section: `max_output_megapixels`, the
  `--image-dir` paragraph (naming, collisions, exists, the conversion rules,
  the ignored output flags).
- `docs/engines.md` §Upscalers / spandrel: the controller-side orientation
  and conversion rules; the tiler comparison of §3.3 in two sentences.
- `README.md` command table: `upscale` — "Upscale a video clip, a still
  image, or a directory of images".
- `PROGRESS.md`: pointer, plan path, checklist, next action.

## 8. Module-by-module summary

| file | change |
|---|---|
| `core/media.py` | `DIR_IMAGE_SUFFIXES`; `local_artifact(path, media)` (moved from the CLI) |
| `core/image_dir.py` | **new** — `output_dir_for`, `plan_image_dir`, `prepare_upload`, the two dataclasses |
| `core/upscale_dir.py` | **new** — `upscale_image_dir`, `ImageDirResult`, `ItemOutcome` |
| `core/config.py` | `UpscaleConfig.max_output_megapixels: int = 256`, validated `> 0` |
| `core/orchestrator.py` | `fetch_artifact_bytes(artifact)` extracted from the materialize block; `generate` calls it |
| `cli/_main.py` | `--image-dir` in the `upscale` source mutex group |
| `cli/_commands.py` | the `--image-dir` branch; `_finish_launch_row`; `_resolve_input_as_artifact` delegates to `local_artifact`; the single `--image` megapixel refusal |
| `pixi.toml` | `pillow`, `pillow-heif` (conda-forge) |
| `tests/live/` | fixture-building script for §6.1 |
| docs | `configuration.md`, `engines.md`, `README.md`, `PROGRESS.md` |

Zero pod-side changes, zero golden moves, one new config field with a
default.

## 9. Open questions the plan must verify, not assume

1. **Does the pod keep `/artifacts/<id>` for the pod's lifetime?** The runner
   fetches each result immediately after its stage returns, so even a
   per-job cleanup would be fine, but the plan reads
   `wan_t2v_server.py`'s artifact retention before relying on it.
2. **Does the `/upload` cleanup (`_maybe_cleanup_upload`) run per job?** If
   uploads accumulate on the pod's disk across hundreds of images, a large
   directory could fill `/tmp`. The plan checks, and if they accumulate,
   adds a per-item `DELETE` or documents the disk ceiling.
3. **imageio's Pillow plugin and palette (`P`) PNGs.** §3.4 re-encodes
   anything outside `RGB`/`RGBA`/`L`; the plan confirms with a fixture that
   the pod path would otherwise have mishandled `P`, so the rule is justified
   by a test and not by caution alone.

## 10. Non-scope, and why each is separate

**10.1 A shared tiler.** §3.3.

**10.2 Controller concurrency.** The pod has one job worker.

**10.3 `--force` / overwrite.** The `exists` skip is what makes a restart
cheap; an overwrite flag is a one-line addition when someone needs it.

**10.4 Output formats other than PNG.** Lossless PNG is the only honest SR
output; same ruling as the previous spec's §11.4.

**10.5 A directory of videos.** Every video is minutes of pod time; the
economics and the failure surface differ enough to be their own design.

**10.6 Hosted image upscalers.** Still the previous spec's §11.1.
