# MiniMax-H3 text-to-audio-video on Modal — design

**Date:** 2026-09-17
**Status:** approved in brainstorm; not yet planned
**Budget:** $20 operator-authorised for the whole effort

## Goal

Add MiniMax-H3 (`MiniMaxAI/MiniMax-H3`, a.k.a. Hailuo 3.0) as a kinoforge video
model on Modal, generating **native 2K / 24 fps video with its joint stereo
audio intact**, driven by the same `kinoforge generate` command shape already
used for Wan 2.2.

## What this is NOT

**The Wan 2.2 post-chain is deliberately out of scope for H3.** That chain
(480² → FlashVSR 1080p → RIFE 60 fps) exists to compensate for Wan's low native
output. H3 emits up to 2K at 24 fps already, so upscaling it is at best a no-op,
and RIFE would desynchronise a soundtrack that was generated in lockstep with
the frames. Operator decision 2026-09-17: **t2va only, keep the audio.**

Also out of scope: `fl2va` (first/last-frame) and `ref2va` (reference) modes,
INT8/NVFP4 quantised weights, and multi-GPU SGLang serving. Each is a later
increment; none is needed for a first working clip.

## Grounding — where these facts come from

**This model postdates the assistant's training data.** Every claim below was
read from the live model card, the live repo tree, or Modal's live docs during
the 2026-09-17 brainstorm. Nothing here is recalled. Where a number could not be
sourced from the vendor, that is stated rather than guessed.

| Fact | Value | Source |
|---|---|---|
| `t2va` is a supported task on FL2VA | yes, no image inputs required | model card task list |
| FL2VA folder is self-contained | own `model_index.json`, `transformer`, `text_encoder`, `tokenizer`, `video_vae`, `audio_vae`, `processor` | repo tree |
| FL2VA folder size | **144 GB** | repo tree |
| Whole repo size | 498 GB | repo tree |
| bf16 component sizes | transformer 61.7 GB, text encoder 48.0 GB, video VAE 4.9 GB, audio VAE 0.6 GB (**≈115 GB**) | comfyui-wiki listing |
| Native output | up to 2K, 24 fps, 15 s, 32 kHz stereo audio | model card |
| Inference entry point | `DiffusionPipeline.from_pretrained(..., dtype=torch.bfloat16, device_map="cuda")` | model card example |
| Modal H200 VRAM | **141 GB** (verbatim) | Modal GPU docs |
| Modal accepts gpu strings | `"H200"`, `"B200"`, `"B200+"`, `"B300"` | Modal GPU docs |
| Modal H200 price | $0.001261/sec ≈ **$4.54/hr** | Modal pricing |

**Stated VRAM requirement: none.** The model card gives no hardware floor. The
~115 GB figure is derived from published weight sizes, and the claim that it
does not fit 80 GB is an inference from that arithmetic — not a vendor number.
Treat it as the design's single largest assumption.

## Decomposition

Three sub-projects. **A and B ship before C** (operator decision 2026-09-17);
both are cheap and both de-risk C.

| | Sub-project | Live spend | Gate |
|---|---|---|---|
| A | Modal catalog: add H200 | $0 | offline tests green |
| B | HF Volume prefetch path | ~$0.51 | 144 GB resident on the Volume |
| C | H3 t2va server + config | ~$5 | one frame-QA'd clip with audio |

---

## Sub-project A — Modal catalog: add H200

### Problem

`src/kinoforge/providers/modal/_catalog.py:16` holds `_MODAL_GPUS`, a 7-entry
tuple snapshotted **2026-07-08**, whose largest card is H100 at 80 GB. Modal now
sells H200/B200/B300. Because `Offer.gpu_type` is passed straight through to
Modal's `gpu=` parameter (`providers/modal/__init__.py:310` →
`providers/modal/_app.py:135`), the catalog is the *only* thing deciding what we
can book — and it currently cannot express any card large enough for H3 in bf16.

### Change

Add exactly one row:

```python
("H200", 141, 4.54),
```

Re-date the snapshot comment to 2026-09-17.

### Why H200 only, and not B200/B300

**Modal's own documentation does not state B200 or B300 VRAM.** Only NVIDIA
datasheets do. Writing a VRAM constant the provider never supplied is precisely
the **U48** defect — SkyPilot reported a CUDA version from a lookup that could
not succeed, every offer carried the same fabricated constant, and every cfg
leaving `min_cuda` at its default had its entire catalog filtered away. A wrong
`vram_gb` here fails the same way: silently, through `filter_offers`, with no
error to read.

H200's 141 GB is quoted verbatim by Modal and is sufficient for this design.
B200/B300 are deferred until their VRAM can be sourced from Modal itself.

### Tests

- A `Placement(min_vram_gb=120)` returns H200 and nothing smaller.
- A `Placement(min_vram_gb=120, accelerators=["H200"])` ranks H200 first.
- Existing catalog tests stay green (H200 must not displace A100-80GB for
  workloads that already fit, or every Wan config gets more expensive).
- The `"H200"` id string matches Modal's accepted spelling exactly — asserted as
  a literal, because a typo here surfaces only as a live create failure.

### Done when

Offline tests green, committed. No live spend.

---

## Sub-project B — HF Volume prefetch

### Problem

`kinoforge-hf-cache` (`providers/modal/_app.py:54`) is mounted at `HF_HOME` on
every Modal pod, so weights persist across runs. But **there is no way to
populate it except by running the job that needs it** — grep for
`prefetch`/`warm_cache` returns nothing. The first H3 run would therefore pull
144 GB while an H200 bills at $4.54/hr (~$3.94), and an interrupted fetch would
burn that again.

### Change

A path that mounts the same Volume on a **T4** ($0.59/hr — the cheapest card in
the catalog) and runs:

```python
snapshot_download("MiniMaxAI/MiniMax-H3", allow_patterns=["FL2VA/*"])
```

followed by an explicit `volume.commit()`.

`allow_patterns` is load-bearing: without it this pulls 498 GB instead of 144 GB,
tripling both time and cost.

### Properties

- **Resumable.** HF's cache dedupes by blob hash, so a retry after a failure
  re-fetches only what is missing. A failed prefetch costs cents.
- **Idempotent.** Running it against an already-warm Volume is a no-op.
- **Reusable.** Nothing about it is H3-specific beyond its arguments; it serves
  every future large model.

A CPU-only container would be cheaper still, but whether the Modal provider can
compose a GPU-less request is unverified — T4 is the decided default, and
CPU-only is a later optimisation to be measured, not assumed.

### Tests

- Offline: the composed Modal request carries the expected `allow_patterns`, the
  Volume name, and a cheap GPU — asserted without touching the network.
- Offline: a missing/empty `allow_patterns` is rejected loudly rather than
  silently fetching the whole 498 GB repo.
- Live: one real prefetch; verify by listing the Volume and confirming the
  FL2VA tree is resident and ~144 GB.

### Done when

144 GB of `FL2VA/*` is resident on `kinoforge-hf-cache` and a second invocation
is a no-op. Spend recorded.

---

## Sub-project C — H3 t2va server and config

### Server

**New module** `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py`,
a sibling of `wan_t2v_server.py` — **not** an extension of it. That file is 2610
lines with 65 Wan-specific references; threading a second architecture through it
would put the working Wan path at risk for no benefit.

The new server reuses:
- `servers/_util_stats.py` — serves the `/util` route. **Load-bearing**: the
  CLAUDE.md live-smoke rule requires polling `gpuUtilPercent`, and a server
  without this route cannot be monitored.
- a **new** `servers/_av_io.py` (below).

The diffusers engine is already config-driven — `server_cmd`, `embed_modules`,
`embed_files` are all read from YAML (`engines/diffusers/__init__.py:1022-1081`)
— so **no engine fork is required**. The config points `server_cmd` at the new
module.

### Audio output

`servers/_video_io.py:write_mp4` takes `(T, H, W, 3)` uint8 and nothing else.
**This is the exact line at which H3's audio would be silently discarded.**

Add `servers/_av_io.py`:

```python
def write_mp4_with_audio(frames, audio, fps, sample_rate, path) -> None
```

muxing H.264/yuv420p video with AAC stereo. **`_video_io.write_mp4` is not
touched**, so the Wan/FlashVSR/RIFE paths cannot regress.

### Mode

Add to `MODE_ROLE_REQUIREMENTS` (`core/interfaces.py:799`):

```python
"t2va": {},
```

Empty dict — t2va takes no image roles, same shape as `t2v`.

### Capability profile

The H3 profile sets `supports_joint_audio=True` — **the first `True` in the
repo**. Verified 2026-09-17: `supports_joint_audio=False` appears at **11 sites
across 11 files** (video engines, image engines and the fake backbone alike) and
`supports_joint_audio=True` appears **nowhere**.

**Do not assume this does anything.** `core/strategy.py:55` reads it to write an
`_audio_mode` marker into the job spec "so a downstream stage can branch", and a
grep confirms **nothing reads `_audio_mode` anywhere**. It is a dormant
placeholder, not a working seam — the same shape as **U40** (correct logic,
unreachable) and **U24** (the fix that was one field short).

**Operator decision 2026-09-17: leave the marker inert, and say so in the code.**

The flag is set to `True` because it is *factually true* — H3 does generate joint
audio, and a capability profile that lied about that would be its own defect. But
nothing may be built on it in this project. Concretely:

- Add a comment at `core/strategy.py:55` recording that `_audio_mode` is written
  and read nowhere, that this is deliberate as of 2026-09-17, and that H3 is the
  first model for which the value is not a constant.
- Add a comment at the H3 profile site recording that `supports_joint_audio=True`
  is a capability *declaration*, and that the audio actually reaches the output
  through `_av_io.write_mp4_with_audio` — **not** through the strategy seam.
- **No test may assert behaviour through `_audio_mode`.** Audio behaviour is
  tested at `_av_io` and at the frame-QA audio arm, where it is real.

The comments are the deliverable here. An inert seam that is *documented* as
inert is fine; an inert seam that reads as working is how U40 cost a live run.
A future agent finding `supports_joint_audio=True` must be able to see, without
grepping, that the strategy marker is not the mechanism.

### Config

`examples/configs/modal-diffusers-minimax-h3-t2va.yaml`:

- `compute.provider: modal`, `placement.min_vram_gb: 120`,
  `accelerators: ["H200"]`
- `models: [{ref: "hf:MiniMaxAI/MiniMax-H3", ...}]` restricted to `FL2VA/*`
- `spec`: 24 fps, resolution and frame count chosen so the first run is the
  *cheapest* clip that proves the path, not the largest
- lifecycle `boot_timeout` generous enough for a warm-Volume load of ~115 GB

**The config must enter the golden ratchet.** `EXCLUDED_CONFIGS` is empty for
the first time since the Modal matrix opened (U35); it must stay empty.

### Frame QA — with an audio arm

The CLAUDE.md visual-QA rule applies unchanged: extract frames, eyeball a contact
sheet, record the verdict. **Plus an audio assertion**, because this is the first
model where a silent "success" is possible:

- an audio stream exists in the container
- its duration matches the video's within tolerance
- it is stereo at the expected sample rate
- it is not digital silence

The FlashVSR lesson is the precedent: 24+ runs reported green on exit code and
dimensions while every output was garbage, because nothing looked at the actual
payload. Dimensions cannot see pixels; `ffprobe` alone cannot hear silence.

### Done when

One `kinoforge generate --mode t2va` produces a 2K/24 fps clip with verified
stereo audio, frame-QA recorded, pod torn down and verified from a fresh process,
and an entry written to `successful-generations.md` (new capability axis: new
model, new mode, new provider-GPU, first joint-audio output).

---

## Spend plan

| Step | Est. | Basis |
|---|---|---|
| A | $0 | offline only |
| B | ~$0.51, ~$1 with one retry | 144 GB on a T4 at $0.59/hr |
| C | ~$0.76 per attempt, budget 6–8 | H200 $4.54/hr, ~10 min per warm-Volume attempt |
| **Total** | **~$6–7** | leaves ~$13 headroom |

Without B, C costs ~$4–5 *per attempt* and $20 buys three or four shots total.
This repo's history — FlashVSR 24+ attempts, RIFE 6 boots — says that is not
enough. **The prefetch is what makes the budget viable**, which is why it is a
prerequisite rather than an optimisation.

All live spend follows the standing rules: `pixi run preflight` first,
`--no-reuse` on every one-shot, utilisation polled during the run (not
`est_spend`), teardown verified from a fresh process after the orchestrator
exits.

## Risks

1. **The 141 GB fit is unverified.** ~115 GB of weights leaves ~26 GB for 2K
   activations. If it OOMs, the first mitigation is a smaller resolution or frame
   count — far cheaper than changing hardware, and it isolates whether the
   problem is capacity or configuration.
2. **`from_pretrained` may need an explicit subfolder or variant** to select
   FL2VA. The model card's example is abbreviated and does not show it. Resolve
   offline by reading `model_index.json` and `modular_model_index.json` before
   spending.
3. **diffusers version conflict.** ModularPipeline likely needs newer than the
   `diffusers>=0.32` the Wan configs pin. The H3 config pins its own versions, so
   this must not perturb the Wan image — verify the two images stay independent.
4. **Image bake cost.** The first deploy of any new Modal config pays a full
   image build (356 s observed for FlashVSR on 2026-09-17). Budget it once.
5. **License.** `minimax-h3-community-license-agreement`. No gating notice was
   visible on the repo, but acceptance may still be required for download — a
   prefetch that 401s is the cheapest possible place to discover that, which is
   another argument for B preceding C.

## Decisions log

| Date | Decision | Rationale |
|---|---|---|
| 2026-09-17 | t2va only; no FlashVSR/RIFE for H3 | H3 is natively 2K/24 fps; upscaling is a no-op and interpolation would desync joint audio |
| 2026-09-17 | H200 bf16, not A100 offload or INT8 | Fits outright with headroom; official weights; fewest unknowns |
| 2026-09-17 | Prefetch to the Volume from a T4 | Turns a ~$3.94 fetch into ~$0.51 and makes the budget viable |
| 2026-09-17 | A and B ship before C | Both cheap, both de-risk C |
| 2026-09-17 | H200 only; defer B200/B300 | Modal does not state their VRAM; inventing the constant is the U48 defect |
| 2026-09-17 | Leave `_audio_mode` inert, document it | Flag is factually true; the seam is not the mechanism, and must not read as one |

## Open questions for plan time

None blocking. Two to resolve offline, for free, before any spend:

- the exact `from_pretrained` invocation that selects FL2VA (risk 2)
- the minimum diffusers version, and whether it can coexist with the Wan pin
  (risk 3)
