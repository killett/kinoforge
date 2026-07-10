# Design — Modal fast-boot via image bake (build/runtime provision split)

> Unblocks **Modal Milestone 3 Task 5** (FlashVSR 4x live proof), which failed
> 2026-07-09: Modal preempted the pooled A100 repeatedly during the ~15 min
> runtime boot (pip torch + 526 MB BSA wheel + FlashVSR weights, no caching), so
> `/health` never bound and the run piled up 10 containers before teardown. Root
> cause: the kinoforge Modal transport provisions everything at **runtime**, but
> Modal is **image-centric** — a long runtime boot is a wide preemption window.

## Goal

Make Modal container boot near-instant by baking the slow, deterministic
provision steps (pip deps, BSA wheel, model weights) into the **Modal image** at
build time, leaving only fast steps (embed kinoforge code, exec the server) at
container start. A pre-baked image is pulled+cached by Modal, so a preemption
re-pull is fast and the boot no longer offers a ~15 min preemption window. Then
re-run M3 Task 5 to green (full 480²→1920², frame-QA, log §24).

Success = the M3 FlashVSR upscale completes on Modal, output is 1920×1920,
frame-QA passes, teardown clean. Secondary win: M1/M2 boots also shrink.

## The split (core idea)

`DiffusersEngine.render_provision` today emits ONE bash script that interleaves:

| Current line (order) | Phase | Bakeable? |
|---|---|---|
| `exec > /tmp/bootstrap.log` redirect | runtime | no (runtime log surface) |
| keep-alive `trap … sleep infinity` EXIT | runtime | no |
| sidecar `http.server 8001` | runtime | no (RunPod log proxy; unused on Modal) |
| selfterm watchdog launch | runtime | no |
| embed_modules / embed_files decode + `PYTHONPATH` | runtime | no (tiny/fast; ~KB base64) |
| **`pip install … <deps>`** | **build** | **yes (torch ~2 GB + deps)** |
| `export KINOFORGE_SKIP_WAN_LOAD` (upscale_only) | runtime | no (env for the server) |
| **composed upscaler install (BSA wheel curl+install, FlashVSR weights fetch)** | **build** | **yes (526 MB + ~2-5 GB)** |
| `exec <server_cmd>` | runtime | no |

**Split into two scripts:**
- **`build_script`** — the two bold rows: `pip install` + the upscaler install
  (BSA + weights). Idempotent, deterministic, no runtime-only side effects
  (no `sleep infinity`, no server exec). Safe to run at image-build time.
- **`runtime_script`** — everything else, in the same order minus the build rows:
  log redirect, trap, sidecar, selfterm, embed, exports, `exec server`.

RunPod behaviour is UNCHANGED: it keeps running the **combined** script
(`build_script` + `runtime_script` concatenated, == today's script byte-for-byte)
at runtime. Only Modal consumes the split.

### Why embed stays runtime, not baked

Embed decodes ~KB of base64 kinoforge modules to `/tmp/kfsrv` + sets `PYTHONPATH`
via a runtime `export`. Baking it would need a persistent path + image-level
`PYTHONPATH`, and it is far too fast to matter for preemption. Keep it runtime —
smaller blast radius, the embed mechanism is untouched.

## Data flow / threading

1. **`RenderedProvision`** (returned by `render_provision`) gains two fields:
   `build_script: str` (may be empty) and `runtime_script: str`. The existing
   `script` field stays and equals `build_script + "\n" + runtime_script` (so
   RunPod + every existing caller is byte-identical). `render_provision` builds
   the two buckets and joins them for `script`.
2. **`InstanceSpec`** gains `image_build_script: str | None = None`. The
   orchestrator populates it from `rendered.build_script` (None/empty → omit).
   `provision_script` continues to carry the **combined** script (RunPod path).
3. **`ModalProvider.create_instance`**: when `spec.image_build_script` is set,
   pass it to `ModalAppRequest.image_build_script`; also pass a Modal-only
   `runtime_provision_script` (derived so Modal execs ONLY the runtime phase, not
   the combined script — otherwise it would re-pip/re-download at runtime).
   Simplest wiring: the orchestrator also sets a new
   `InstanceSpec.runtime_provision_script` from `rendered.runtime_script`, and
   ModalProvider uses `runtime_provision_script or provision_script` for the boot
   payload while RunPod ignores it. (One field pair, RunPod-inert.)
4. **`build_modal_app`**: `Image.from_registry(req.image, add_python=…)` gains
   `.run_commands(req.image_build_script)` when present. The web_server boot
   payload uses the runtime script only. Volume + secrets unchanged.

## Modal image build specifics

- `python:3.13-slim` base already ships pip; `pip install` + the BSA-wheel URL
  install + the weights fetch all run in `run_commands` at build (network is
  available during Modal image builds; FlashVSR weights are public — no token;
  add a Modal build Secret later if a gated repo ever needs one).
- The image is content-hashed by Modal → built once, cached; a preemption
  re-pull of a cached image is fast (no re-download of torch/BSA/weights).
- Weights land at the same path the runtime server reads
  (`/workspace/models/flashvsr…` per the upscaler provision) — baked into the
  image layer (read-only at runtime, which is fine for reads).
- **Bootstrap-error visibility during build:** unlike the opaque runtime boot
  (which `exec > /tmp/bootstrap.log`, invisible on Modal), `run_commands` streams
  to the `modal run` / deploy build log — so a bad wheel/weights fetch surfaces
  at build time, not as a silent 15 min hang. (Directly fixes the blindness that
  cost the 2026-07-09 session.)

## Backward-compatibility guarantees

- RunPod + all existing cfgs: `render_provision(...).script` is byte-identical to
  today (a characterization test locks this). No RunPod behaviour change.
- M1/M2 Modal cfgs: they have a non-empty `pip` list → their pip now bakes into
  the image, shrinking their boot too. Their `models[]` (HF weights) still load
  at server startup via `from_pretrained` (that is the diffusers server's own
  eager load, NOT part of `render_provision`) — so M1/M2 large-model download is
  a SEPARATE concern, out of scope here; this milestone bakes only the
  `render_provision` steps. (M2's 63 GB HF load remains a runtime cost; noted as
  a follow-up, not regressed.)

## Testing

- **Characterization (RunPod unchanged):** `render_provision(cfg).script` for an
  existing FlashVSR + a Wan cfg equals the pre-split output (golden compare).
- **Split correctness:** for the Modal FlashVSR cfg, `build_script` contains the
  `pip install` line AND the BSA-wheel install AND the weights fetch, and
  contains NEITHER the `exec <server>` line NOR `sleep infinity`; `runtime_script`
  contains the `exec <server>` line and the embed lines and NONE of the pip/BSA
  lines.
- **Spec threading:** the orchestrator populates `InstanceSpec.image_build_script`
  + `runtime_provision_script` from the rendered split (unit test with a fake
  engine/provider seam).
- **Modal image build wiring:** `build_modal_app` calls `Image…run_commands` with
  the build script (offline, fake `modal_mod` capturing the calls) and the boot
  payload contains the runtime script, not the pip/BSA lines.
- **Live re-run (M3 Task 5, USER-GATE):** the FlashVSR upscale on Modal 80GB
  produces a 1920×1920 clip; frame-QA vs the 480² source; teardown clean; log §24.

## Non-goals / deferred

- Baking the M1/M2 HF `models[]` snapshot into the image (the diffusers server's
  eager `from_pretrained`, separate from `render_provision`) — a later
  optimization if M2 preemption ever recurs.
- Warm-reuse / i2v / flf2v on Modal; RIFE (M4).
- Any RunPod change — this milestone is Modal-only by construction.
