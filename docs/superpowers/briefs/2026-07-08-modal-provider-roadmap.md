# Build brief — Modal serverless-GPU provider + progressive capability roadmap

> **For the resuming session:** this is the *requirements* (the WHAT). Start the
> Superpowers **brainstorming** skill on THIS brief, then write-plan, then
> execute — **autonomously** (see "Process" below). PROGRESS.md's SINGLE NEXT
> ACTION points here.

## Why (the reliability pivot)

Raw-GPU rental proved unreliable for this project: AWS/GCP deny GPU quota;
RunPod/Lambda hit availability droughts; vast is software-blocked (sky/SDK
`/api/v0/instances` 410 — see `PROGRESS.md` slice-1 block + the 2026-07-07
skypilot-vast spec). **Modal** is the fix: serverless GPU where you run your own
code/weights on pooled A100/H100 — no quota approval, per-second billing, scales
to zero. Goal: add Modal as a kinoforge `ComputeProvider` so the custom-weights
pipelines (Wan-via-diffusers, FlashVSR, RIFE) stop fighting availability.

## Credentials — READY (operator confirmed 2026-07-08)

- `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` in `/workspace/.env` (template section
  already in `.env.example`, committed `89eb81f`). The `modal` SDK reads both as
  its default auth chain.
- **$30 free credit** — this is the HARD budget ceiling for the whole roadmap.
  Smokes must be cheap; start with the smallest model.

## Progressive capability roadmap (cheapest first — each its own milestone + live smoke)

Deliver in this order; do NOT start a later one until the earlier is live-green,
frame-QA'd, and logged. Each proves one more axis on the same Modal transport.

1. **Wan 2.1 T2V-1.3B** — smallest model, fits ~24GB, cheapest smoke. FIRST
   end-to-end proof of the Modal transport + the diffusers server on Modal.
   Validates the whole path for pennies. (Model ran on RTX A5000 24GB — gen §9.)
2. **Wan 2.2 T2V-A14B** — dual-14B MoE, needs an **80GB** GPU (A100-80GB / H100).
   Proves big-model gen on Modal's larger GPUs. (RunPod pinned A100 80GB — gen §8.)
3. **Upscaling — FlashVSR** — full 480²→1920² 4x. Needs >40GB; a Modal 80GB card
   runs it at FULL res (unlike the Lambda 40GB proof that had to downscale to
   288²). Reuses the diffusers server upscale path. (gen §13/§19/§21.)
4. **Interpolation — RIFE v4.26** — low VRAM (ran on 16GB A4000 — gen §20), cheap.
   Reuses the RIFE server/engine.

## Core design questions for brainstorming to resolve

- **Transport (the big one):** Modal exposes HTTPS web endpoints natively
  (`@modal.fastapi_endpoint` / `modal.Function` / `App.deploy`). Unlike SkyPilot
  (which needed the provider-internal `ssh -L` tunnel), Modal likely gives a
  direct `https://<app>--<fn>.modal.run` URL — so the provider returns
  `endpoints={"8000": "https://...modal.run"}` directly, **no tunnel**. Decide:
  (a) **run the existing kinoforge HTTP server** (`wan_t2v_server`) inside a Modal
  App as a web endpoint — reuses all engine/server code, RECOMMENDED; vs
  (b) restructure generation as direct Modal function calls — bigger rewrite,
  drops the HTTP-server abstraction. Strongly prefer (a).
- **Mapping serverless onto the `ComputeProvider` ABC:** Modal is "deploy an app,
  invoke a function," not "create a pod." Map: `create_instance` ≈ deploy/warm a
  Modal App + return its URL; `get_instance`/`list_instances` ≈ query app state;
  `destroy_instance` ≈ stop the app (or rely on scale-to-zero); `find_offers` ≈
  Modal GPU catalog (T4, L4, A10G, L40S, A100-40GB, A100-80GB, H100). `heartbeat`
  + `last_heartbeat` — likely no-ops (Modal owns liveness), mirror the SkyPilot
  fix (`last_heartbeat -> None`, gen §21 follow-up).
- **Image + weights:** Modal builds images from an image spec in code; weights via
  runtime fetch + a Modal Volume for caching. Map to the existing
  `render_provision` / `provision_script` / `embed_modules` machinery.
- **Lifecycle/billing:** per-second, scale-to-zero. `idle_timeout` → Modal
  `container_idle_timeout`. `--no-reuse` → stop the app after generate (the
  teardown-hang fix from gen §21 already bounds sky; confirm Modal's stop path is
  bounded too).
- **Big-GPU access on the free tier:** confirm A100-80GB / H100 are available to a
  new $30-credit account BEFORE committing milestone 2 to a live spend (probe
  `modal` catalog / a tiny function first).

## Constraints / gotchas to carry into the plan

- $30 credit ceiling; each milestone must budget its smoke and start cheap.
- Frame-QA EVERY output video before reporting green (CLAUDE.md rule).
- Commit RED scaffolds BEFORE any live spend (durability rule); verify teardown
  after each smoke (scale-to-zero or explicit stop) — no silent billing.
- Log every qualifying gen to `successful-generations.md` (new provider axis).
- Standard test prompt: `/workspace/examples/configs/prompts/field-realistic.txt`.

## References (existing patterns to mirror)

- `src/kinoforge/core/interfaces.py` — `ComputeProvider` ABC (`find_offers`,
  `create_instance`, `get_instance`, `list_instances`, `stop_instance`,
  `destroy_instance`, `heartbeat`, `endpoints`; + off-ABC `last_heartbeat`).
- `src/kinoforge/providers/runpod/__init__.py` — native pod provider, closest analog.
- `src/kinoforge/providers/skypilot/__init__.py` — the just-built HTTP-endpoint
  provider + `_adapters.build_provider_for` cloud-plumbing pattern.
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — the HTTP server
  Modal would host (already does t2v / i2v / flf2v / upscale).
- `src/kinoforge/interpolators/rife/` + `src/kinoforge/upscalers/flashvsr/` —
  the interpolate + upscale runtimes.
- `examples/configs/skypilot-lambda-flashvsr.yaml` — cfg shape for a
  provider-backed FlashVSR run (Modal cfg mirrors this with `provider: modal`).

## Process (autonomous)

Superpowers **brainstorm → write-plan → execute**, red/green TDD, two-stage
review. Run **autonomously** — skip reply-when-done handshakes and pre-spend
confirmations; live smokes pre-authorized within the $30 Modal credit (per the
`feedback_autonomous_no_gates` standing rule). Persist the design doc to
`docs/superpowers/specs/` and the plan to `docs/superpowers/plans/` as they form;
keep `PROGRESS.md` current after each milestone. New Modal deps via
`pixi add --pypi modal` into a dedicated feature env (mirror `live-skypilot`).
