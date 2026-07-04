# LumaImageEngine — UNI-1 / Photon image keyframes (Layer R plugin)

**Date:** 2026-07-03 (PDT)
**Status:** validated (autonomous session — user-gate waived per standing
autonomy memory; Dr. Twinklebrane reviews post-hoc)
**Closes:** memory `project_luma_video_retirement_2026` carry-forward
(the "LumaAgentsImageEngine to add" half; the deletion half closed in
Phase 44). $20 Luma platform credit funds the live smoke.

## 1. What

A hosted `ImageEngine` implementation for Luma's Dream Machine image
API, registered as `"luma"` in the image-engine registry, usable from
the existing `keyframe:` YAML block (Layer R / Phase 32 subsystem). No
video surface — Luma retired the direct video API (Phase 44).

Naming note: project memory calls this "LumaAgentsImageEngine". The
live API surface (verified 2026-07-03 via docs.lumalabs.ai) is
`POST https://api.lumalabs.ai/dream-machine/v1/generations/image` —
there is no "Agents" API for image generation. Class is
**`LumaImageEngine`**; the memory label was a pre-research guess.

## 2. Verified API contract (docs.lumalabs.ai, 2026-07-03)

- Submit: `POST /dream-machine/v1/generations/image`, header
  `Authorization: Bearer $LUMAAI_API_KEY`, JSON body:
  `{"prompt": str, "model": str, "aspect_ratio": str?}` (+ ref fields
  we do NOT ship in v1).
- Poll: `GET /dream-machine/v1/generations/{id}` → `state` ∈
  `dreaming | completed | failed`, `failure_reason`, `assets.image`
  (URL on completion).
- Delete: `DELETE /dream-machine/v1/generations/{id}`.
- Models: docs list `photon-1` (default) + `photon-flash-1`; the
  UNI-1 / `uni-1.1` ids are announced but not yet in the SDK docs page.
  Therefore the engine hardcodes NO model allowlist — `spec.model` is
  required cfg input and Luma validates server-side. The live smoke
  probes `uni-1.1` first and falls back to `photon-1` if the API
  rejects it; whichever works is what the example cfg pins.

## 3. Approaches considered

- **A (chosen): raw REST inner backend on `RemoteSubmitPollBackend`**,
  mirroring `image_engines/replicate/` structurally but with urllib
  instead of an SDK client. Zero new dependencies; Bearer via the
  existing `core.auth.Bearer` strategy; submit/poll/delete are 3 tiny
  HTTP calls.
- B: `lumaai` SDK — new pixi dep in `live-hosted` for three REST
  calls, and the published SDK lags the model list. Rejected.
- C: reuse the hosted *video* engine machinery — wrong shape; the
  image sibling subsystem exists precisely for this. Rejected.

## 4. Components

All in `src/kinoforge/image_engines/luma/__init__.py` (~300 lines,
mirroring the replicate sibling):

- `_LumaHttp` — tiny client the factory returns: holds base URL +
  token; `post_json(path, body)`, `get_json(path)`, `delete(path)`
  via `urllib.request` with `Authorization: Bearer` header. Injectable
  in tests (the `client_factory` seam `RemoteSubmitPollBackend`
  already takes).
- `_LumaImageInnerBackend(RemoteSubmitPollBackend)` —
  - `_submit`: POST body `{"prompt", "model": job.spec["model"],
    **job.spec.get("params") or {}}` (so `aspect_ratio`, `format`
  flow from YAML without code changes); returns `id`.
  - `_poll_one`: GET generation; returns dict with `id/state/assets/
    failure_reason`.
  - `_is_done`: `state == "completed"`.
  - `_is_failed`: `state == "failed"` + `failure_reason`.
  - `_extract_output_url`: `assets.image` (empty-safe).
  - `_delete`: DELETE generation (implemented, not scaffolded — the
    endpoint is documented).
  - `manual_cleanup_url`: `https://lumalabs.ai/dream-machine/creations`.
- `LumaImageBackend(ImageBackend)` — ImageJob→GenerationJob adapter,
  same as the replicate sibling.
- `LumaImageEngine(ImageEngine)` — `name="luma"`,
  `requires_compute=False`; `provision` validates Bearer presence and
  rejects non-None instance; `backend()` builds `_LumaHttp` factory;
  `validate_spec` requires `spec.model` + non-empty prompt;
  `model_identity` returns `spec.model` (or `""`).
- Self-registration `registry.register_image_engine("luma", ...)` +
  import in `kinoforge/_adapters.py`.

## 5. Config surface

No schema changes — the existing `keyframe:` block already carries
`engine` / `prompt` / `spec` / `params`. New example cfg
`examples/configs/keyframe-luma.yaml`:

```yaml
keyframe:
  engine: luma
  prompt: "<from examples/configs/prompts/field-realistic.txt at runtime>"
  spec:
    model: uni-1.1        # falls back to photon-1 if API rejects — see §2
    params:
      aspect_ratio: "16:9"
```

(Exact composition with a video cfg for i2v is E21/E22 territory —
out of scope here; the live smoke drives the engine directly.)

## 6. Error handling

- Missing/empty `LUMAAI_API_KEY` → `AuthError` at `provision`/`backend`.
- HTTP non-2xx on submit/poll → `KinoforgeError` with status + body
  tail (Luma returns JSON error details; include them).
- `state == "failed"` → failed-with-reason via the base class path.
- Poll timeout → base class TimeoutError semantics (max_poll × 2 s;
  generation time ~31 s per Luma, so default 60 × 2 s is ample).

## 7. Testing

- `tests/image_engines/test_luma.py`, mirroring `test_replicate.py`:
  fake `_LumaHttp` recording requests; submit body shape (model +
  params passthrough, NO ref fields), poll state machine
  (dreaming→completed, failed w/ reason), `_extract_output_url`
  empty-safety, `validate_spec` rejections, auth errors, registry
  registration, `model_identity`.
- Live smoke (`tests/live/test_luma_keyframe_live.py`,
  `KINOFORGE_LIVE_SPEND` gated): generate one image from the standard
  prompt, assert PNG/JPEG bytes land + non-zero size, DELETE the
  generation, log entry in `successful-generations.md` (new capability
  axis: first `keyframe`-mode entry + new provider tuple
  `(luma, LumaImageEngine, <model>, t2i)`). Cost: ~cents from the $20
  credit.

## 8. Out of scope (YAGNI)

- `image_ref` / `style_ref` / `character_ref` / `modify_image_ref`.
- Video (retired), Ray/Dream-Machine video models.
- E21 (upload keyframe → hosted i2v end-to-end) and E22 (role wiring).
- Rate limiting / provisioned throughput.


## 9. Correction (same day, pre-smoke)

The first implementation pass targeted
`api.lumalabs.ai/dream-machine/v1/generations/image` per
docs.lumalabs.ai — that surface is RETIRED for platform keys and
returns `403 Not authenticated` (verified live). The Layer 5a
brainstorm (memory `project_luma_video_retirement_2026`) had already
recorded the correct surface and locked decisions this design failed
to consult first:

- Base URL `agents.lumalabs.ai` (docs at docs.agents.lumalabs.ai);
  `POST /v1/generations` with `{"prompt","model","type":"image",
  "aspect_ratio"}`; poll `GET /v1/generations/{id}`; completed
  payload carries `output: [{"url": ...}]` (with `assets.image` kept
  as a defensive fallback in `_extract_output_url`).
- Module `image_engines/luma_agents/`, registry slug `luma_agents`
  (NOT `luma`), class `LumaAgentsImageEngine`.
- Model id `uni-1` (default; `uni-1-max` also documented). No
  DELETE endpoint — `_delete` raises NotImplementedError and
  `manual_cleanup_url` points at the dashboard.
- The stored `LUMAAI_API_KEY` is VALID for the agents API (GET on a
  bogus id returns 404 Generation-not-found, i.e. authenticated).

Process lesson recorded for future sessions: recall project memories
for the target subsystem BEFORE external doc research — the memory
explicitly said not to relitigate these decisions, and the stale
public docs cost one wasted implementation pass + a wrongly-diagnosed
operator blocker (retracted before commit).
