# Animated inputs in `kinoforge upscale --image-dir`

**Status:** design approved 2026-10-11, not yet planned.
**Scope:** an animated input under `--image-dir` (animated WebP, GIF, APNG,
multi-page TIFF) is upscaled frame by frame on the pod the run already holds
and written back as an animation in the input's own container, with the
original per-frame durations, loop count and alpha. A plan-time frame cap
bounds spend. The latent APNG passthrough (a multi-frame `.png` uploaded byte
for byte) is closed by the same change.
**Non-scope:** the single-still `--image` path (keeps frame 0), a video
round trip through `--video` (see §1.1), temporal models, frame
de-duplication, a quality knob per container, and any pod-side change. Each
is a separate decision.

Addendum to `2026-10-06-directory-image-upscale-design.md` (the directory
spec); every rule not restated here is unchanged.

---

## 1. The question, and the answer

The directory runner flattens any multi-frame input to frame 0 and writes a
still `<stem>.png`, logged once at INFO and counted as a success (directory
spec §3.4). For an animated WebP that is a silent loss: the plan, the
summary and the mirrored tree all look like a finished upscale. Verified
2026-10-11 with a two-frame WebP — Pillow reports `n_frames=2`,
`is_animated=True`; the output is a one-frame PNG of frame 0.

Worse, an APNG whose first frame is 8-bit RGB/RGBA/L matches the passthrough
rule (`.png` + mode + identity orientation) and is uploaded **unchanged**, so
the pod receives a multi-frame file the still path was never meant to see.

The answer: treat an animated input as a sequence of stills on the path that
already exists. Decode frames on the controller, send each through the same
`UpscaleStage` the run holds, reassemble on the controller. One item stays
one item in the plan, the progress lines, the summary and the exit code.

### 1.1 Why not convert to video and use `--video`

Measured 2026-10-11 against the bundled ffmpeg 8.1.1: its `webp` decoder is
still-only (`image data not found` on an animated file), so the conversion
would be Pillow → PNG frames → mp4 anyway. Both video engines then write
H.264 4:2:0 — alpha is lost and hard-edged animation picks up chroma
smearing — and read a single constant fps, so per-frame durations (an
animated WebP commonly mixes 100/200/300 ms) are either resampled with
duplicate frames (paid for on the GPU) or lost. FlashVSR additionally pads to
`8n+1` frames by cloning the last one, so a three-frame sticker becomes nine
frames fed to a temporal model. The spandrel video path is per-frame, so for
short animations it computes exactly what the still path computes. The
detour costs two lossy encodes and a second container; it buys nothing.

Long, video-like WebPs (hundreds of frames, no alpha) that want FlashVSR's
temporal model remain an offline conversion plus `kinoforge upscale --video`,
unchanged.

### 1.2 Why the loop lives in the runner

Three placements were weighed:

- **Runner-level loop per item (chosen).** Planner marks the item animated;
  converter yields frames; runner loops; assembler writes. Four modules, no
  pod change, one item remains one item everywhere.
- **Expand to N frame items at plan time.** The loop stays untouched, but the
  plan, progress and summary count frames as files, `exists` and collision
  naming grow special cases, and a dead pod mid-animation leaves orphan frame
  files in the mirrored tree.
- **Loop on the pod.** Breaks the directory spec's "zero pod-side changes"
  decision, moves every embedded server and the launch goldens, and the pod's
  imageio reader does not decode animated WebP.

---

## 2. Surface

No new flag. `kinoforge upscale --image-dir` handles animated inputs on its
own. Exit codes are unchanged (directory spec §2.4).

### 2.1 One new config field

```yaml
upscale:
  max_animation_frames: 300   # default; plan-time refusal above it
```

`UpscaleConfig.max_animation_frames: int = 300`, validated `>= 1` in the same
validator shape as `max_output_megapixels`. An animated input above the cap
is refused **at plan time** as `oversize` (§3.2) — before any pod work, like
the megapixel guard. An animation of F frames costs F stage calls, the same
as F stills; the cap is the only spend guard, and the dry run is the spend
preview.

### 2.2 Dry run and summary

The plan line and the closing summary gain a parenthetical on the pending
count:

```
found 8: pending 6 (2 animated, 94 frames), exists 1, oversize 1, unreadable 0; non-image skipped 1
upscaled 6, skipped 1 existing, failed 1, aborted 0
```

Per-item progress lines (directory spec §5.2) gain `(94 frames)` for an
animated item so a long silence has a visible cause.

---

## 3. Planning: `core/image_dir.py`

Planning stays header-only. The planner never reads durations, loop counts
or alpha; those are the converter's business at run time, so a thousand-file
walk stays fast.

### 3.1 The header read

`read_image_header` gains `frames: int` from Pillow's `n_frames` (container
metadata, no pixel decode; `1` when the attribute is absent). `ImageHeader`
and `ImageDirItem` carry `frames`. `frames == 1` is a still and takes every
existing path unchanged; `frames > 1` is animated.

Because the planner now knows the frame count, the passthrough rule in
`prepare_upload` is reached only for stills. The APNG leak is closed by
construction, not by a new check.

### 3.2 The cap

After the header read and the `exists` check, before the megapixel guard:

```
elif hdr.frames > max_animation_frames:
    disposition = "oversize"
    reason = f"{hdr.frames} frames exceeds upscale.max_animation_frames={cap}"
```

Reusing `oversize` keeps the `Disposition` literal, `failed_at_plan`, the
exit code and the summary bucket unchanged. The megapixel guard then applies
per frame (all frames share one size); its reason text is unchanged.

### 3.3 Naming

A still keeps `<stem>.png`. An animated item keeps its **input container**:

| input | output |
|---|---|
| `anim.webp` (animated) | `anim.webp` |
| `anim.gif` (animated) | `anim.gif` |
| `anim.png` (APNG) | `anim.png` |
| `pages.tif` / `pages.tiff` (multi-page) | `pages.tif` / `pages.tiff` |

The same-folder collision rule (directory spec §3.2) is unchanged and still
compares **stems** only: `a.webp` (animated) and `a.png` (still) in one folder
become `a.webp.webp` and `a.png.png`, `renamed=True`, logged once per folder.
Different output suffixes do not exempt a pair from the rule; predictability
beats a special case. `exists` checks the container-specific output path, so
a rerun skips a finished animation.

Decision recorded for the GIF row: the user chose "same container as input"
knowing that Pillow re-quantises every upscaled frame to a 256-colour
palette on GIF write. That throws away much of what the upscaler added on
photographic content and is the accepted cost of a GIF staying a GIF.

---

## 4. Conversion and reassembly

Both controller-side. Frame decoding joins `core/image_dir.py` beside
`prepare_upload`; the assembler is a new `core/animation.py`.

### 4.1 `prepare_frames(item, scratch)` — a generator

Opens the file once, reads `loop` and the per-frame `duration` (ms) from
Pillow's frame `info`, applies the EXIF transpose per frame, and yields, in
order, one `Frame(path, duration_ms, alpha)`:

- `path` — an 8-bit RGB PNG under *scratch*, `<sha8>-f0001.png` and so on.
  The caller unlinks it as soon as the frame's upload finishes, so scratch
  holds at most one decoded frame at a time.
- `duration_ms` — the frame's own duration; TIFF pages have none and yield 0.
- `alpha` — the frame's alpha channel as an 8-bit `L` image kept in memory,
  or `None` when the source has no alpha.

`read_loop` returns the container's loop count, or `None` when it carries
none — a GIF without the Netscape extension plays once and must NOT be
rewritten as `0` (forever); the assembler omits the loop field for `None`
(GIF) or falls back to the writer default where the container always has
one (WebP, APNG). Amended 2026-10-11 after the final review: the original
text said `0` when absent, which conflated play-once with forever.
`prepare_upload` is unchanged for stills.

### 4.2 Alpha

The pod returns RGB only (`tile_upscale` → `(H*scale, W*scale, 3)`), so alpha
cannot go through the upscaler. The assembler resizes each frame's alpha
mask with Lanczos to the upscaled size on the controller and reattaches it.
Cheap, keeps stickers transparent, and the limit is stated plainly: mask
edges are interpolated, not model-upscaled.

### 4.3 `assemble_animation(frames, meta, output)` — `core/animation.py`

Inputs: the upscaled frame files in order (PNG bytes from the pod written to
scratch as `<sha8>-u0001.png` …), the duration list, the loop count, the
optional alpha masks, the output container (from the item's output suffix)
and the output path. One writer per container, all through Pillow's
`save_all`:

| container | writer | timing | notes |
|---|---|---|---|
| WebP | `save(..., save_all=True, duration=[…], loop=n, lossless=False, quality=90, method=4)` | preserved | alpha preserved |
| GIF | `save(..., save_all=True, duration=[…], loop=n, disposal=2)` | preserved | Pillow quantises to 256 colours per frame; alpha → 1-bit transparency |
| APNG | `save(..., format="PNG", save_all=True, duration=[…], loop=n)` | preserved | lossless, alpha preserved |
| TIFF | `save(..., save_all=True)` | none | multi-page, lossless |

The assembler opens the upscaled frames lazily (`Image.open` per frame,
handed to `append_images` as an iterator) so a 300-frame animation never has
300 decoded frames in memory. The encoded bytes go through the existing
`_write_atomic` (`.part` then rename), so a failed write leaves nothing in
the mirrored tree.

The lossy WebP quality is a constant in `core/animation.py`, not a config
field; a knob waits for someone to need it.

---

## 5. The session runner: `core/upscale_dir.py`

The per-item block branches on `item.frames`:

- **Still** (`frames == 1`): exactly today's path.
- **Animated**: iterate `prepare_frames`; per frame build the same
  `PipelineState` with `local_artifact(frame.path, "image")`, run the stage,
  `fetch_artifact_bytes`, write to the item's `-uNNNN.png` scratch file,
  unlink the decoded frame; after the last frame call `assemble_animation`
  and write the output atomically; drain the item's scratch files.

The per-item `try` already around the block makes a frame failure an item
failure: any exception on frame 37 of 100 records the item as `failed` with
reason `frame 37/100: <ExcType>: <msg>`, drains the item's scratch, runs the
health probe exactly as today (directory spec §4.2), and continues to the
next file. Nothing partial reaches the mirrored tree. `_FATAL` exceptions
abort the rest as today.

**Progress.** `_log.info("%s: frame %d/%d", name, i, n)` per frame.
Cancellation is checked per frame through the stage's existing cancel token,
so Ctrl-C stops within one frame.

**Scratch sizing.** Per animated item at most one decoded frame plus the
upscaled frames so far; the upscaled set is drained when the item finishes
or fails. The directory spec's disk-sizing note gains one line: an animated
item needs `F × (upscaled frame PNG)` of scratch while it runs.

---

## 6. CLI wiring: `_cmd_upscale`

The dry-run plan printer, the per-item progress line and
`_print_image_dir_summary` read `item.frames` to produce §2.2. Preflight
refusals are unchanged. `--image` on a single animated file keeps today's
frame-0 behaviour; the docs say so.

---

## 7. Testing

Every test names the bug it catches (test-design standard). The whole-tree
invariants (`tests/test_core_invariant.py`, `tests/test_no_unredacted_writes.py`,
`tests/test_source_audit.py`) and `tests/providers` run in **every** task's
verify step — the directory plan omitted them and paid for it in
`de669716`.

| area | test | bug it catches |
|---|---|---|
| header | `frames` for WebP, GIF, APNG, multi-page TIFF; `1` for a still | a two-frame APNG reports one and passes through byte for byte |
| cap | 301 frames → `oversize`, reason names the cap; 300 → pending | a 2,000-frame file enters the pod loop |
| naming | animated keeps its suffix, still keeps `.png`, `a.webp`+`a.png` → full names | two outputs overwrite each other |
| exists | an existing `anim.webp` output → `exists` | a finished animation is paid for twice |
| frames | order, durations (mixed 100/200/300), loop, alpha mask present/absent, EXIF transpose | durations off by one frame; loop dropped; portrait frames lying down |
| assembler | round trip per container: frame count, pixel of frame 0 and last, durations, loop, alpha | GIF written with one duration; alpha lost on WebP; TIFF single page |
| assembler | output written via `.part` + rename; a failing frame open leaves no output | half an animation in the tree |
| runner | fake stage: F frames → F calls, one output, zero scratch left | runner uploads the source once; scratch leaks per frame |
| runner | stage fails on frame 3 → item `failed`, reason names `frame 3/5`, no output, next item runs | partial animation lands; one bad file ends the run |
| runner | `_FATAL` on frame 2 → rest aborted, exception propagates | budget overrun continues upscaling |
| CLI | dry run and summary carry `(2 animated, 94 frames)` | spend preview silent on animations |
| config | `max_animation_frames: 0` rejected; omitted → 300 | cap silently disabled |

### 7.1 Live fire

`tests/live/build_image_dir_fixture.py` gains one animated WebP with alpha
(four frames, mixed durations, loop 0) and one animated GIF (three frames).
`tests/live/test_image_dir_upscale_smoke.py` fires once on RunPod spandrel
x2 with `--no-reuse`, utilisation polled by the harness thread (never
`est_spend`). Expected dry run: `pending 8 (2 animated, 7 frames)`.
Superseded: the live fixture already carried a two-frame GIF, so the real
counts are `found 9: pending 7 (2 animated, 6 frames), exists 1, oversize 1,
unreadable 0; non-image skipped 1` — see the plan header's planning-time
findings.
Frame QA on both outputs before reporting green: a contact sheet of every
output frame, durations and loop read back and compared to the fixture,
alpha checked on the WebP. Expected spend: a few cents; the RED scaffold is
committed before the pod is created.

---

## 8. Docs

- This spec (addendum; the directory spec is not rewritten — its §3.4 line
  "Animated GIFs and multi-page TIFFs contribute frame 0" gets a one-line
  pointer here).
- `docs/configuration.md`: `max_animation_frames` beside
  `max_output_megapixels`.
- README upscale row: "animations are upscaled frame by frame and written
  back in their own container".
- `PROGRESS.md` resume snapshot; `successful-generations.md` "See also" under
  §39 (same `(provider, engine, model, mode)` tuple).

---

## 9. Module-by-module summary

| module | change |
|---|---|
| `core/config.py` | `UpscaleConfig.max_animation_frames` + validator |
| `core/image_dir.py` | `ImageHeader.frames`, `ImageDirItem.frames`, cap in `plan_image_dir`, container-keeping output name, `prepare_frames`, `Frame` |
| `core/animation.py` (new) | `assemble_animation`, per-container writers, alpha resize |
| `core/upscale_dir.py` | animated branch, per-frame progress, per-item scratch drain |
| `cli/_commands.py` | plan line, progress line, summary parenthetical |
| `tests/live/build_image_dir_fixture.py` | two animated fixtures |
| docs | §8 |

No pod-side module changes; no launch golden should move. A task whose
snapshot diff is non-empty has gone wrong.

---

## 10. Open questions the plan must verify, not assume

1. Pillow's `n_frames` on WebP needs libwebp animation support in the
   conda-forge build — confirmed working 2026-10-11 in the default env
   (`n_frames=2` on a saved two-frame file); the plan's first task asserts
   it under CI's fresh lockfile install, since the AVIF gap of 2026-10-07
   came from exactly this kind of local/CI drift.
2. Whether `append_images` accepts a lazy iterator for every writer in §4.3
   or materialises the list for one of them (TIFF is the suspect). If it
   materialises, cap memory by opening frames lazily and letting Pillow hold
   only the encoded page.
3. The GIF writer's `disposal=2` interaction with a resized alpha mask on
   frames that were previously opaque; the fixture's GIF has no alpha, so a
   unit test with an alpha GIF decides it.
