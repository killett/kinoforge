# SkyPilot vast.ai Video Generation — Design Spec

**Date:** 2026-07-07
**Status:** Validated (brainstorm complete, awaiting plan)
**Topic:** Generate video on vast.ai using SkyPilot

---

## Goal

Make kinoforge generate a real video on a **vast.ai** GPU provisioned through
**SkyPilot**, by closing the two gaps that block it today:

1. SkyPilot's vast adapter is broken against the installed vastai-sdk.
2. `SkyPilotProvider` exposes no HTTP endpoint, so the video engine's
   `wait_for_ready` fails with `ProvisionFailed: has no endpoints` — deploy
   smokes work (no readiness check) but generation does not.

The design is built as a **generic HTTP-over-SkyPilot foundation** (works for
every sky cloud) and **delivered incrementally**: this spec covers **slice 1**
— one cheap, live, frame-QA'd video on vast.ai proving the whole chain. Slice 2
(hardening, Lambda parity) is scoped but out of this spec.

---

## Non-Goals (scope boundaries — stated to prevent debt creep)

- **Warm-reuse for sky clusters.** Sky clusters are not RunPod pods; slice 1 is
  one-shot `--no-reuse` only. Tunnel lifetime = create → generate → destroy.
- **Tunnel-drop reconnect / resilience.** Slice 2.
- **Lambda parity + multi-cloud fallback** (`clouds=["vast","lambda"]`). Slice 2.
- **Public-port / server-auth path.** Rejected transport (see Approach 2 below).
- **New video model or engine.** Reuses the existing `DiffusersEngine` FlashVSR
  upscale-only path — the cheapest existing "video-in/video-out over HTTP" path.

---

## Root-Cause Findings (grounded 2026-07-07)

- **Vast break is one expression, ours to fix.** `sky/provision/vast/utils.py:204`
  reads `vast.vast().client.api_key`. vastai-sdk **0.2.5** exposes the key at
  `VastAI().api_key` — there is no `.client`. Latest skypilot on PyPI is still
  `0.12.3.post1` (no upstream fix), so we shim it ourselves. **Not** a
  wait-for-upstream blocker (corrects the Explore agent's conclusion).
- **The endpoint gap is the real capability.** `SkyPilotProvider.create_instance`
  returns an `Instance` with **empty** `endpoints`; `endpoints()` returns only
  `{"ssh": "ssh://<id>"}`. `DiffusersEngine.wait_for_ready` needs an HTTP URL in
  `instance.endpoints`, exactly as RunPod supplies
  `{"8000": "https://<id>-8000.proxy.runpod.net"}`.
- **Provider path is otherwise provider-agnostic.** `SkyPilotProvider` already
  implements the full `ComputeProvider` surface (`find_offers` via
  `sky.list_accelerators`, `create_instance` via `sky.launch`, `get_instance`,
  `destroy_instance`, `endpoints`). Config selects it via `compute.provider:
  skypilot` + `compute.cloud: [...]` (`_adapters.build_provider_for` injects the
  cloud list into `provider._clouds`).
- **sky transport surface:** `sky.endpoints(cluster, port)` exists but requires a
  declared open port (public exposure); sky registers each cluster in SSH config
  so `ssh <cluster>` works. No clean programmatic tunnel API → we drive `ssh -L`
  as a subprocess.

---

## Architecture

**Core principle:** transport lives **inside the provider**. The engine never
learns what SkyPilot or SSH is. `SkyPilotProvider` returns a plain
`http://127.0.0.1:<port>` in `endpoints` exactly where RunPod returns a proxy
URL. `DiffusersEngine.wait_for_ready` / `http_get` / `generate` are **untouched**
— this is the low-debt boundary that keeps every engine provider-agnostic.

### Component A — Vast adapter shim

- New small module under `providers/skypilot/` (e.g. `vast_compat.py`).
- On skypilot-provider import, monkeypatch `vastai_sdk.VastAI` to add a `client`
  property that returns `self`, so sky's `vast.vast().client.api_key` resolves to
  `VastAI().api_key`.
- **Idempotent + self-disabling:** no-op if `VastAI` already has a working
  `.client.api_key` (future upstream fix) or if `vastai_sdk` is absent.
- One tiny, reversible shim — no fork, no SDK downgrade.

### Component B — HTTP-over-SkyPilot tunnel

- After `sky.launch`, `create_instance`:
  1. Allocates a free local TCP port (injected allocator seam).
  2. Spawns `ssh -N -L <local>:localhost:8000 <cluster>` (injected spawn seam),
     relying on sky's generated SSH config to make `<cluster>` resolvable
     (this is where vast's mapped-SSH-port is inherited).
  3. Returns an `Instance` with `endpoints={"8000": "http://127.0.0.1:<local>"}`.
- Provider holds the tunnel subprocess in an internal `{instance_id → proc}` map
  (keeps the frozen `Instance` clean; no handle leaks into the dataclass).
- `destroy_instance` kills the tunnel in a `finally`, **then** tears down the
  cluster (`sky down`) — no orphaned ssh procs even if `sky down` fails.
- **Injectable seams** (mirrors the existing `sky_client` seam): `ssh_spawn` and
  `port_allocator` — tests never open a real socket or SSH connection.
- Boot-liveness probe stays RunPod-only → sky supplies none → `None` → existing
  poll-until-timeout path (already handled by the 2026-07-07 boot-stall work).

### Component C — Provisioning confirm

- Verify the `sky.launch` Task built from `InstanceSpec` carries the engine
  `provision_script` + starts the diffusers server on the node (Task `setup`/`run`
  wiring). Deploy smokes work today; this makes the *server* come up so the
  tunnel has something to reach.

---

## Rejected Approaches

- **Approach 2 — sky-native `ports` + `sky.endpoints`.** Declare `ports:[8000]`,
  sky opens the cloud firewall, return public `http://<ip>:8000`. Rejected:
  exposes an **unauthenticated** video server on the public internet (real
  security debt — would force adding server auth), and `ports` support is uneven
  across vast/lambda (per-cloud special-casing = the debt we're avoiding).
- **Approach 3 — drive the server over `sky exec`/ssh stdin-stdout, no HTTP.**
  Rejected: discards the entire HTTP engine seam, diverges hard from the RunPod
  path. Maximal debt.

---

## Data Flow (slice 1, end to end)

```
cfg(provider=skypilot, cloud=[vast], upscale_only)
  → build_provider_for → SkyPilotProvider(_clouds=["vast"])
  → find_offers (sky.list_accelerators)
  → create_instance (sky.launch Task: provision_script + server start)
      → open ssh -L <local>:localhost:8000 <cluster>
      → Instance.endpoints = {"8000": "http://127.0.0.1:<local>"}
  → engine.wait_for_ready polls http://127.0.0.1:<local>/health
  → /upscale over the tunnel
  → download mp4
  → destroy_instance (kill tunnel in finally, then sky down)
```

---

## Error Handling

- **sky "no resources" / launch failure** → map to existing `CapacityError`
  (composes with the 2026-07-07 capacity-wait retry loop).
- **Tunnel spawn fails** → `ProvisionFailed`; cluster destroyed.
- **Tunnel dies mid-generation** → `http_get` fails → generation errors out
  (reconnect is slice 2, deliberately not masked here).
- **`destroy_instance`** kills the tunnel in a `finally` even if `sky down`
  raises — no orphaned ssh procs or ledger ghosts.

---

## Testing (TDD — all offline/unit in the red/green loop)

- **Vast shim:** after patch, `VastAI(api_key=k).client.api_key == k`; no-op when
  `VastAI` already correct or `vastai_sdk` absent.
- **Tunnel:** injected `ssh_spawn` + `port_allocator` → `create_instance` returns
  a `{"8000": "http://127.0.0.1:<port>"}` endpoint and records the proc;
  `destroy_instance` kills the proc **then** the cluster; spawn-failure →
  `ProvisionFailed`.
- **Endpoints population:** `create_instance` yields a non-empty HTTP endpoint
  (regression guard for the exact bug found: empty `instance.endpoints`).
- **Provisioning:** the sky Task built from an `InstanceSpec` carries
  `provision_script` + the server run command.
- **Regression:** existing `tests/providers/test_skypilot.py` stays green.

## Live Proof (slice 1 — one gated live run)

- New cfg `examples/configs/skypilot-vast-flashvsr.yaml`: `provider: skypilot`,
  `compute.cloud: ["vast"]`, `upscale_only: true`, on-demand (not spot — short
  job, avoid preemption), cheapest vast GPU meeting FlashVSR upscale-only VRAM
  (confirm exact VRAM via a plan-time probe; ~24–48 GB expected). Reuses the
  existing 480² fixture clip.
- Procedure: preflight (creds incl. `VAST_API_KEY`, zero pods, clean tree) →
  `kinoforge upscale --config skypilot-vast-flashvsr.yaml --video <fixture>
  --no-reuse` → poll GPU utilisation during the run → **frame-QA** the output
  video → verify `kinoforge list` clean after → log to
  `successful-generations.md` (new capability axis: provider=skypilot/vast,
  engine=diffusers upscale). Est ~$0.08–0.15, within session budget.

### Known slice-1 risk to validate live

vast.ai SSH reaches the node through a mapped port; the tunnel relies on sky's
generated SSH config to encode that mapping. If sky's vast SSH config is itself
incomplete, the tunnel step is where it surfaces — caught immediately by the
localhost readiness poll, **not** a silent 900s hang (the boot-stall fast-fail
work already bounds that).

---

## Pre-Flight (operator, before slice-1 live run)

- `VAST_API_KEY` present in `.env` (→ `~/.config/vastai/vast_api_key`, written by
  `tools/setup_sky_creds.sh`). If absent, operator supplies it before the live
  run — flagged as a grouped pre-flight, not a mid-build interrupt.

---

## Slice 2 (deferred — not this spec)

Tunnel-drop reconnect, Lambda parity, `clouds=["vast","lambda"]` fallback,
warm-reuse evaluation for sky clusters. Each rides the same provider-internal
tunnel architecture — no rework of slice-1 seams.
