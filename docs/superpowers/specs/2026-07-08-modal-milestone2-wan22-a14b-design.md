# Design — Modal Milestone 2: Wan 2.2 T2V-A14B on Modal 80GB GPU

> **Milestone 2** of the Modal provider roadmap
> (`docs/superpowers/briefs/2026-07-08-modal-provider-roadmap.md`). Milestone 1
> (Wan 2.1 T2V-1.3B on Modal A10) is LIVE-GREEN — `successful-generations.md` §22.
> This milestone proves **big-model gen on Modal's 80GB GPUs** on the SAME transport.

## Goal

Generate a Wan 2.2 T2V-A14B (dual-14B MoE) video on a Modal **80GB** GPU
(A100-80GB / H100), frame-QA it, and log it. Proves the Modal transport carries a
model that needs a large card — the axis Milestone 1 (24GB A10) could not reach.

## Scope — a new config, nothing else

The Milestone-1 `ModalProvider` transport is **reused verbatim, zero code change**:
- deploy a Modal App → `@modal.web_server(8000)` running the existing diffusers
  `wan_t2v_server` via the same `provision_script; exec run_cmd` bundle as RunPod;
- public `https://<app>--<fn>.modal.run` URL returned as `endpoints["8000"]`;
- boot payload gzip-chunked across Secret keys (Modal 32768-byte per-value cap);
- `python:3.13-slim` image (serialized web-server fn requires image-Python ==
  controller-Python 3.13); provision pip-installs torch 2.6 cu124 + diffusers.

All four M1 gotchas ([[reference_modal_provider_gotchas]]) already handled in code.
**This milestone adds one YAML config and its tests. No `src/` change is expected.**

## The config — `examples/configs/modal-wan-t2v-14b-2_2.yaml`

Merge of the M1 Modal cfg (transport) and the RunPod A14B cfg
(`runpod-diffusers-wan-t2v-14b-2_2.yaml`, model + hardware):

| Field | M1 (1.3B) | **M2 (A14B)** | Why |
|---|---|---|---|
| `engine.precision` | fp16 | **bf16** | A14B validated bf16 on RunPod |
| `engine.diffusers.image` | python:3.13-slim | python:3.13-slim | Modal py-match invariant |
| pip list | torch2.6 cu124 stack | same (drop `peft` — no LoRA) | RunPod A14B pip list |
| `models[0].ref` | Wan2.1-T2V-1.3B-Diffusers | **Wan2.2-T2V-A14B-Diffusers** | `-Diffusers` variant: sharded `from_pretrained` (bare repo 404s) |
| `compute.requirements.min_vram_gb` | 24 | **80** | MoE = two 14B transformers (~56GB bf16) + T5 + VAE; 48GB OOMs |
| `gpu_preference` | A10, L4, A100-40GB | **A100-80GB, H100** | Modal catalog 80GB strings (`_catalog.py`) |
| `disk_gb` | 40 | **150** | ~63GB weights + HF cache |
| `spec.num_frames` | 33 | **81** | matches RunPod A14B cfg |
| `lifecycle.boot_timeout` | 30m | **45m** | ~63GB HF download dominates cold boot |
| `lifecycle.budget` | 2.0 | **4.0** | headroom under the $30 ceiling |

`spec`: model `Wan2.2-T2V-A14B-Diffusers`, pipeline `WanPipeline`, scheduler
`UniPCMultistepScheduler`, 480×480, fps 16. No `cloud:` key (Modal is non-sky).

## Cost & the free-tier 80GB probe (roadmap flag)

- A100-80GB = **$2.50/hr**, H100 = $3.95/hr (Modal catalog snapshot).
- Est smoke: ~35–45 min, download-dominated → **~$1.50–1.90**. Well under $30.
- **The smoke IS the probe.** Modal allocates the GPU at container start —
  *before* the long weight download — so a free-tier 80GB denial surfaces early and
  cheap (fail fast, no wasted download). Unlike AWS/GCP there is **no quota-approval
  gate** on Modal GPUs (that reliability win is the whole reason for this provider).
  Monitor: if no 80GB container starts within a couple minutes, abort.

## Testing

- **Offline characterization (green offline, no spend):** the M2 config resolves to
  a `ModalProvider`, `cloud is None`, `min_vram_gb == 80`, model ref contains
  `Wan2.2-T2V-A14B`, and `modal_offers(reqs)` returns an 80GB card first
  (A100-80GB ahead of H100 per `gpu_preference`). Mirrors `tests/test_modal_config.py`.
- **Live-smoke scaffold (RED, committed BEFORE any spend):** mirrors
  `tests/live/test_modal_wan_t2v_1_3b.py`; drives the real `kinoforge generate`.

## Live-run protocol (autonomous, per brief)

1. Commit offline config + tests + RED live scaffold **before** spend (durability rule).
2. `pixi run preflight` (clean tree, creds). `pixi run -e live-modal kinoforge generate
   --config … --mode t2v --prompt "$(cat …/field-realistic.txt)" --no-reuse`.
3. Monitor: Modal has **no util probe** → app-state + orchestrator-log only
   (`modal app list`, bootstrap not proxied). Abort if GPU never allocates or the
   HF download stalls past `boot_timeout`.
4. **Frame-QA** the output (mandatory) — extract ~5 frames, eyeball for
   corruption/coherence/adherence. ⚠️-flag anything not clearly high quality.
5. Verify teardown: `kinoforge list` → no instances + `modal app list` clean.
6. Log to `successful-generations.md` §23 (new axis: A14B on Modal 80GB). Update PROGRESS.

## Non-goals (deferred)

- Modal **Volume** weight-caching (repeat-run optimization; one-shot smoke pays one
  download — YAGNI here).
- Warm-reuse on Modal, i2v/flf2v modes (Milestones 3–4 cover upscale/interp).
