# Design — Modal serverless-GPU provider (spec 1: provider + Milestone 1)

- **Status:** validated 2026-07-08, ready for planning.
- **Brief (the WHAT):** `docs/superpowers/briefs/2026-07-08-modal-provider-roadmap.md`
- **Scope decision:** this spec ships the shared `ModalProvider` infrastructure plus the
  first live capability proof (Milestone 1, Wan 2.1 T2V-1.3B). Later capability axes are
  thin follow-up specs on the same provider.
- **Delivery-mechanism decision:** **Option A — generic, config-driven Modal app** that
  reuses `render_provision` and the existing FastAPI HTTP server verbatim. (Rejected: B —
  direct Modal function calls; C — static per-model modules. Both create a divergent
  Modal-only execution path = structural debt.)

## Why

Raw-GPU rental proved unreliable for kinoforge: AWS/GCP deny GPU quota; RunPod/Lambda hit
availability droughts; vast is software-blocked (sky/SDK `/api/v0/instances` 410). Modal is
serverless GPU — run your own code/weights on pooled A100/H100, no quota approval, per-second
billing, scale-to-zero. Adding Modal as a kinoforge `ComputeProvider` lets the custom-weights
pipelines (Wan-via-diffusers, FlashVSR, RIFE) stop fighting availability.

Credentials READY (operator confirmed 2026-07-08): `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` in
`/workspace/.env` (verified present, `modal` SDK default auth chain reads both). **$30 free
credit = HARD budget ceiling for the whole roadmap.**

## §1 Decomposition

This spec ships shared provider infra + the first live proof. Later capability axes = thin
follow-up specs, same provider, new config + smoke.

- **This spec:** `ModalProvider` (full `ComputeProvider` ABC) + registration + `live-modal`
  pixi env + Milestone 1 live-green (Wan 2.1 T2V-1.3B, cheapest GPU that fits).
- **Deferred (each its own follow-up spec, do NOT start until the prior is live-green +
  frame-QA'd + logged):**
  - M2 — Wan 2.2 T2V-A14B (needs 80GB GPU). Requires a free-tier big-GPU access probe first.
  - M3 — FlashVSR full 480²→1920² 4x (needs >40GB; a Modal 80GB card runs full-res, unlike
    the Lambda 40GB proof that had to downscale to 288²).
  - M4 — RIFE v4.26 interpolate (low VRAM, cheap).

## §2 Architecture — generic config-driven Modal app

No engine changes. Reuse the existing provision seam: `engine.render_provision(cfg)` →
`RenderedProvision(script, run_cmd, image, ports, env_required)`. RunPod runs
`bash script; exec run_cmd` on port 8000 to boot the FastAPI server
(`kinoforge.engines.diffusers.servers.wan_t2v_server`, routes `/health`, `/generate`,
`/status`, `/artifacts`, `/upscale`, `/interpolate`, `/upload`, `/lora/*`). Modal runs the
**same command** via `@modal.web_server`, which publishes whatever HTTP server the startup
command brings up on the given port at a `https://<ws>--<label>.modal.run` URL.

```
create_instance(spec):
  build a modal.App(name=run_id):
    image  = modal.Image.from_registry(spec.image)          # same CUDA base as RunPod
    volume = modal.Volume "kinoforge-hf-cache" mounted @ HF cache dir   # weight cache across cold starts
    gpu    = _MODAL_GPU[spec.offer.gpu_type]                 # A10G / L4 / L40S / A100-40GB / A100-80GB / H100
    @modal.web_server(port=8000, startup_timeout=...):
      run  bash -c '<spec.provision_script>; exec <spec.run_cmd>'
      env  = {**spec.env, **{k: ... for k in env_required}}
  app.deploy(name=run_id)  ->  https://<ws>--<run_id>-server.modal.run
  return Instance(id=run_id, provider="modal", status="starting",
                  endpoints={"8000": url}, cost_rate_usd_per_hr=...)
```

Downstream is unchanged — the HTTP client hits the `.modal.run` URL exactly as it hits a
RunPod proxy URL. A provisioning bug fixed once is fixed for all three providers.

**Cold-start:** first fetch of a given weight set is slow; the Modal Volume absorbs repeats.
Evolution path if latency bites (DEFERRED, out of scope here): push apt/pip/base-weights into
Modal image-build layers, leaving only per-run bits (LoRA stack, model selection) at startup —
reduces cold-start without forking per model.

## §3 `ComputeProvider` ABC mapping

Contract from `src/kinoforge/core/interfaces.py:208`.

| ABC method | Modal implementation |
|------------|----------------------|
| `find_offers(reqs)` | static Modal GPU catalog (fixed published pricing, per-sec → per-hr), filtered by the shared `filter_offers(reqs)`. No live "offers" API — Modal pricing is a fixed table. |
| `create_instance(spec)` | build + deploy the app (§2), return `Instance(status="starting", endpoints={"8000": url})`. |
| `get_instance(id)` | query Modal app state → map to kinoforge status. |
| `list_instances()` | Modal app list, filtered to kinoforge-tagged deployments. |
| `stop_instance(id)` / `destroy_instance(id)` | `app.stop`; destroy uses a **bounded** poll (mirror SkyPilot `_DESTROY_POLL_MAX_ITERS`, ~40×3s) — never an unbounded `while True`. |
| `heartbeat(id)` | no-op (Modal owns container liveness). |
| `last_heartbeat` (off-ABC) | return `None` — mirror the SkyPilot gen §21 fix so `HeartbeatLoop._tick_once` does not `AttributeError` once validation auto-sets `heartbeat_interval_s`. |
| `probe_runtime(id)` | `None` (optional default); Modal container stats can be wired later. |
| `endpoints(instance)` | return the stored `.modal.run` URL for `"8000"`. |

## §4 Seams + testing (offline, zero spend)

`ModalProvider.__init__(modal_client, app_factory, sleep, clock)` — all injectable, mirroring
the SkyPilot seam pattern (`_sky_client`, `_sleep`, `_alloc_port`). Unit tests drive a **fake
modal client** and assert:

- app built with the right image / gpu / volume from the spec + offer;
- the `web_server` startup command carries `spec.provision_script` AND `spec.run_cmd`
  (characterization guard, mirrors the SkyPilot provision guard `a7d772a`);
- `create_instance` returns `endpoints={"8000": "https://...modal.run"}`;
- `destroy_instance` is bounded (fake client reports never-gone → returns after the bound,
  idempotent);
- `last_heartbeat` returns `None`;
- `find_offers` filters the static catalog by `HardwareRequirements`.

No live Modal in the unit suite. Follow the `test-design` skill: each test states the behavior
under test + a concrete bug it would catch; no weak assertions; no over-mocking beyond the
one injected client seam.

## §5 Config shape (Milestone 1)

`examples/configs/modal-wan-t2v-1_3b.yaml`, mirroring `examples/configs/skypilot-lambda-flashvsr.yaml`:

```yaml
compute:
  provider: modal
  gpu: [A10G]              # ~24GB, cheapest Modal GPU that fits Wan 2.1 T2V-1.3B (ran on RTX A5000 24GB, gen §9)
lifecycle:
  idle_timeout: <short>    # -> modal container_idle_timeout (scale-to-zero)
engine: diffusers
model: Wan2.1-T2V-1.3B
mode: t2v
```

Exact field names reconciled against the live config schema during planning (do not invent —
grep the loader).

## §6 Lifecycle / billing

- Per-second, scale-to-zero. `lifecycle.idle_timeout` → Modal `container_idle_timeout`.
- `--no-reuse` → `app.stop` after generate; the stop path MUST be bounded (verify no teardown
  hang, mirroring the SkyPilot gen §21 fix). After the orchestrator exits, confirm
  `pixi run kinoforge list` shows no instances (per the `--no-reuse` durability rule).
- $30 credit is the hard ceiling. M1 smoke on the cheapest GPU is pennies.

## §7 Registration + pixi env

- `src/kinoforge/providers/modal/__init__.py` self-registers `"modal"` in the provider
  registry (mirror `providers/skypilot`); add `import kinoforge.providers.modal` to
  `src/kinoforge/_adapters.py:47`.
- `build_provider_for(cfg)` resolves `cfg.compute.provider == "modal"` → `ModalProvider`.
- `pixi.toml`: add `[feature.live-modal.pypi-dependencies] modal = "*"` and
  `live-modal = { features = ["live-modal"] }` under `[environments]` (mirror
  `[feature.live-skypilot]` / `[feature.live-hosted]`). Live commands run as
  `pixi run -e live-modal kinoforge ...`. `modal` is not on conda-forge → PyPI dependency.

## §8 Milestone 1 live proof (USER-GATE — executed during plan execution, pre-authorized)

- Standard prompt read verbatim from `examples/configs/prompts/field-realistic.txt`.
- Run with `--no-reuse`. Poll GPU/CPU/mem utilisation during the smoke (Modal stats or
  server `/health`) — do not wait on the per-test timeout to discover a dead container.
- **Frame-QA every output** (contact sheet via `kinoforge.core.frames`, judge artifacts /
  temporal coherence / prompt adherence). Anything not clearly high quality gets a ⚠️ flag.
- Log the qualifying gen to `successful-generations.md` (new provider axis = Modal).
- Verify ledger clean after (`kinoforge list` → no instances).
- **Commit the RED scaffold (failing/xfail live-proof test + config) BEFORE the live spend**
  (durability rule).

## §9 Plan-time research (NOT design blockers)

1. **Modal SDK verification:** confirm programmatic runtime-named `app.deploy`, the
   `@modal.web_server` startup-command semantics, and the exact `.modal.run` URL format
   against the current `modal` SDK. WebFetch the Modal docs during planning; adjust §2 if the
   SDK requires a static module + lookup instead of in-process deploy.
2. **`web_server` `startup_timeout`** vs weight-download time — may need a bump for big
   weights (matters M2+; cheap for the 1.3B M1 model).
3. **Big-GPU free-tier access** (A100-80GB / H100) — probe with a tiny function BEFORE
   committing M2 to a live spend. **M2 concern, not this spec.**

## Non-goals (this spec)

- No image-layer weight-baking optimization (cold-start evolution path, deferred).
- No Modal container-stats `probe_runtime` (default `None`).
- Milestones 2–4 (A14B / FlashVSR / RIFE) — own follow-up specs.

## References (patterns to mirror)

- `src/kinoforge/core/interfaces.py:208` — `ComputeProvider` ABC + `Offer`/`Instance`/
  `InstanceSpec`/`RenderedProvision` dataclasses.
- `src/kinoforge/providers/skypilot/__init__.py` — closest analog: injectable seams, HTTP
  endpoint return, bounded destroy poll, off-ABC `last_heartbeat`.
- `src/kinoforge/providers/runpod/__init__.py` — provision_script → startup command pattern.
- `src/kinoforge/_adapters.py:85` — `build_provider_for` factory + registry.
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — the FastAPI server Modal hosts.
- `examples/configs/skypilot-lambda-flashvsr.yaml` — provider-backed config shape.
