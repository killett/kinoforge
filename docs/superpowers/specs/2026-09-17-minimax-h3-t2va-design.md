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

Also out of scope: `fl2va` (first/last-frame), image-to-video, `ref2va`
(reference) modes, INT8/NVFP4 quantised weights, and multi-GPU SGLang serving.
Each is a later increment; none is needed for a first working clip.

**Two of those deferrals are much cheaper to reverse than the third**, and the
plan order should reflect it. The **FL2VA checkpoint already covers
text-to-video *and* image-to-video with optional first/last-frame inputs** — so
once Sub-project B has cached it, adding `i2v` and `fl2va` costs no new download
and no new hardware, only mode wiring. **`ref2va` is the expensive one**: it is a
separate checkpoint of comparable size (multi-reference — up to 9 images, 3 video
clips and 3 audio clips at once), so enabling it means a second ~144 GB fetch.
Sequence accordingly: i2v/fl2va are natural next increments after C; ref2va is
its own budgeted project.

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
| Inference entry point | `ModularPipeline.from_pretrained("MiniMaxAI/MiniMax-H3")` | operator, 2026-09-17 |
| Minimal fetch patterns | `--include "model_index.json" "FL2VA/*"` — **the ROOT `model_index.json` is required alongside the subfolder** | operator, 2026-09-17 |
| FL2VA covers | text-to-video **and** image-to-video, with optional first/last frame inputs | operator, 2026-09-17 |
| Ref2VA covers | multi-reference: up to 9 images, 3 video clips, 3 audio clips at once | operator, 2026-09-17 |
| Modal H200 VRAM | **141 GB** (verbatim) | Modal GPU docs |
| Modal accepts gpu strings | `"H200"`, `"B200"`, `"B200+"`, `"B300"` | Modal GPU docs |
| Modal H200 price | $0.001261/sec ≈ **$4.54/hr** | Modal pricing |

**Stated VRAM requirement: none.** The model card gives no hardware floor. The
~115 GB figure is derived from published weight sizes, and the claim that it
does not fit 80 GB is an inference from that arithmetic — not a vendor number.
Treat it as the design's single largest assumption.

## OPEN — engine route, must be settled before Sub-project B

**Raised 2026-09-17 by operator, after the hardware decision was made.** The
Comfy-Org reference t2v workflow
(`Comfy-Org/workflow_templates/templates/video_minimax_h3_t2v.json`) does **not**
run bf16. It loads:

| Role | File | Precision |
|---|---|---|
| diffusion model | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | **pruned INT8** |
| text encoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | **NVFP4 AWQ** |
| video VAE | `minimax_h3_video_vae_fp16.safetensors` | fp16 |
| audio VAE | `minimax_h3_audio_vae_fp32.safetensors` | fp32 |
| optional LoRA | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | bf16 |

Sampler `res_multistep`, scheduler `simple`, **20 steps** (or **8** with the
turbo LoRA), **1344×768 @ 24 fps**. Audio is decoded by a separate
`VAEDecodeAudio` node and muxed with the frames by `CreateVideo` — which
independently confirms the `_av_io.write_mp4_with_audio` shape below is right.

**Three assumptions this undermines:**

1. **"bf16, therefore >80 GB, therefore H200."** The ecosystem's own reference
   configuration is quantised and totals **~38 GB** (19.5 + 14.6 + VAEs, per the
   comfyui-wiki listing — exact per-file sizes still to be confirmed). That fits
   an A100-80GB with room to spare, and possibly an L40S at 48 GB.
2. **"INT8 is an unvetted third-party repackage."** That reasoning was sound when
   the quantised set looked like a community side-product. It is weaker now that
   it is what Comfy-Org ships as the reference path.
3. **"~2K output."** The reference runs 1344×768 — well under 2K, and far
   cheaper.

**Also newly known and directly budget-relevant:** a **turbo 8-step LoRA** exists.
20 steps → 8 is a ~2.5× cut in generation time, i.e. in dollars per attempt.

**What is NOT undermined:** Sub-project A stands as committed. The catalog was
genuinely stale, H200 is genuinely the only card above 80 GB, and the row is
correct and harmless whichever route wins. Do not revert it.

**The fork, stated plainly.** kinoforge has BOTH a `diffusers` engine and a
`comfyui` engine, and already ships `.graph.json` workflows for Wan 2.2 on the
ComfyUI path. So:

- **Diffusers route** (as specced): official `MiniMaxAI` bf16 layout, 144 GB,
  H200, `ModularPipeline`. Matches how Wan 2.2 runs on Modal today.
- **ComfyUI route**: `Comfy-Org` quantised layout, ~38 GB, A100-80GB, driven by
  a `.graph.json` adapted from the reference template. Matches an
  already-validated reference, 4× smaller fetch, cheaper card, and a known
  8-step turbo option.

**This changes what Sub-project B downloads**, so it must be decided before B is
planned. The numeric parameters above (1344×768, 24 fps, 73 frames, 20/8 steps,
`res_multistep`/`simple`) are useful ground truth for the `spec:` block on
*either* route.

**Unverified:** the fetched summary reported "73 frames ≈ 5 seconds", but
73 / 24 = 3.04 s. Frame count and duration must be read from the template
directly before being copied into a config.

### Operator ruling 2026-09-17: decide later, probe both offline first

**Do not pick a route from the numbers above.** Two of them are unconfirmed and
both are load-bearing:

1. **The ~38 GB figure is third-party and per-file-unverified.** It comes from a
   comfyui-wiki listing, not from the repo. `Comfy-Org/MiniMax-H3` is itself
   480 GB (it holds every precision), so the saving depends entirely on hitting
   the right individual files. Confirm the actual sizes of
   `minimax_h3_fl2va_pruned_int8_convrot.safetensors`,
   `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` and the two VAEs from the repo
   tree itself.
2. **Whether kinoforge's `comfyui` engine can drive H3's node types at all is
   unknown.** The reference template uses `CLIPLoader` with an H3-specific
   `"minimax"` CLIP type, plus `VAEDecodeAudio` and `CreateVideo`. If our engine
   or the pinned ComfyUI build does not carry those nodes, the ComfyUI route is
   not cheaper — it does not exist. Check before choosing, not after.

Both probes are **$0 and offline**. The cost of guessing wrong is a 144 GB
download or a dead-end engine path, so the probes are strictly cheaper than the
decision they inform.

**Sequencing:** run these probes at the start of Sub-project B's planning, and
record the answer in this section before B is planned. Sub-project A is
unaffected and proceeds now.

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
snapshot_download(
    "MiniMaxAI/MiniMax-H3",
    allow_patterns=["model_index.json", "FL2VA/*"],
)
```

followed by an explicit `volume.commit()`.

**Both patterns are required.** `FL2VA/*` alone omits the repo-root
`model_index.json`, producing a 144 GB download that looks complete and does not
load. Operator-supplied 2026-09-17, matching the documented
`huggingface-cli download ... --include "model_index.json" "FL2VA/*"`. A test
must assert **both** patterns are present, not merely that `allow_patterns` is
non-empty — the omission is invisible until load time, and load time is on the
expensive card.

`allow_patterns` is otherwise load-bearing for cost: without it this pulls 498 GB
instead of 144 GB, tripling both time and money.

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

- Offline: the composed Modal request carries the Volume name, a cheap GPU, and
  `allow_patterns` containing **both** `"model_index.json"` and `"FL2VA/*"` —
  asserted without touching the network.
- Offline: a missing/empty `allow_patterns` is rejected loudly rather than
  silently fetching the whole 498 GB repo.
- Offline: `allow_patterns` carrying only `"FL2VA/*"` is rejected — the
  root-manifest omission is the failure that would otherwise be discovered on
  the H200.
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
2. ~~**`from_pretrained` may need an explicit subfolder or variant.**~~
   **Largely resolved 2026-09-17** by operator: the entry point is
   `ModularPipeline.from_pretrained("MiniMaxAI/MiniMax-H3")` against the repo
   root, which is why the root `model_index.json` must be in the fetch. Residual:
   confirm the root manifest resolves to FL2VA when only FL2VA is cached — verify
   offline by reading the fetched `model_index.json` after Sub-project B lands,
   before spending on C.
3. **diffusers version conflict.** ModularPipeline likely needs newer than the
   `diffusers>=0.32` the Wan configs pin. The H3 config pins its own versions, so
   this must not perturb the Wan image — verify the two images stay independent.
4. **Image bake cost.** The first deploy of any new Modal config pays a full
   image build (356 s observed for FlashVSR on 2026-09-17). Budget it once.
5. **License.** `minimax-h3-community-license-agreement`. No gating notice was
   visible on the repo, but acceptance may still be required for download — a
   prefetch that 401s is the cheapest possible place to discover that, which is
   another argument for B preceding C.
6. **H200 now makes an under-capped config launch-then-die instead of fail-free.**
   Every Modal offer is `mode="serverless"`, so `filter_offers` skips the
   `max_usd_per_hr` ceiling at selection time — but `_enforce_rate_cap`
   (`src/kinoforge/orchestrator.py`, around line 973) still destroys the
   instance *after launch* if the realized rate exceeds the cap. Before H200
   existed, a config asking for >80 GB VRAM simply got a `CapacityError` for
   free. Now it can book H200 at $4.54/hr and then get torn down mid-run if its
   `max_usd_per_hr` is below 4.54. All five shipped Modal configs sit between
   1.00 and 4.00. **The MiniMax-H3 config (Sub-project C) must set
   `max_usd_per_hr >= 4.54`**, or it will pay for a launch it cannot keep.

## Decisions log

| Date | Decision | Rationale |
|---|---|---|
| 2026-09-17 | t2va only; no FlashVSR/RIFE for H3 | H3 is natively 2K/24 fps; upscaling is a no-op and interpolation would desync joint audio |
| 2026-09-17 | H200 bf16, not A100 offload or INT8 | Fits outright with headroom; official weights; fewest unknowns |
| 2026-09-17 | Prefetch to the Volume from a T4 | Turns a ~$3.94 fetch into ~$0.51 and makes the budget viable |
| 2026-09-17 | A and B ship before C | Both cheap, both de-risk C |
| 2026-09-17 | H200 only; defer B200/B300 | Modal does not state their VRAM; inventing the constant is the U48 defect |
| 2026-09-17 | Leave `_audio_mode` inert, document it | Flag is factually true; the seam is not the mechanism, and must not read as one |
| 2026-09-17 | Fetch `model_index.json` **and** `FL2VA/*` | Operator correction: `FL2VA/*` alone omits the root manifest and the download will not load |
| 2026-09-17 | Entry point is `ModularPipeline`, not `DiffusionPipeline` | Operator correction; loads the repo root, which is why the root manifest is required |
| 2026-09-17 | Engine route (diffusers vs ComfyUI) deferred; probe both offline first | The ~38 GB saving is unverified per-file, and whether our comfyui engine carries H3's node types is unknown. Both probes are $0; guessing wrong costs a 144 GB fetch or a dead-end path |

## Open questions for plan time

None blocking. Two to resolve offline, for free, before any spend:

- the exact `from_pretrained` invocation that selects FL2VA (risk 2)
- the minimum diffusers version, and whether it can coexist with the Wan pin
  (risk 3)
