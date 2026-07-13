# Modal ephemeral parity — design

**Date:** 2026-07-12
**Status:** validated (brainstorm approved)
**Spec type:** provider-parity feature — Modal joins the `--ephemeral` capability set
(naming, capability gate, ephemeral-index, sweeper reap)

## Problem

`--ephemeral` is refused for `(diffusers, modal)` and `(comfyui, modal)`:
`EPHEMERAL_CAPABILITIES` (`src/kinoforge/core/ephemeral.py:81-97`) has no modal
entries, so `_preflight_ephemeral` (`src/kinoforge/cli/_main.py:250`) errors out
before any spend. Modal shipped (M1–M5, 2026-07-08..12) after the ephemeral
workspaces feature (2026-06-08) and was never wired in.

Flipping the two table entries alone would ship a **dishonest** capability. The
investigation (this brainstorm, 2026-07-12) found three real gaps:

1. **Name leak.** RunPod honors `policy.pod_name_includes_alias=False` by naming
   ephemeral pods `kinoforge-{secrets.token_hex(4)}`
   (`src/kinoforge/providers/runpod/__init__.py:814-821`). Modal hardcodes
   `modal.App(name=f"kinoforge-{req.run_id}")`
   (`src/kinoforge/providers/modal/_app.py`), and CLI run_ids embed the
   subcommand + local timestamp (`upscale-20260712-200409`,
   `src/kinoforge/cli/_commands.py:738,853`). Because `modal app stop` only
   STOPS an app — stopped apps linger in `modal app list` indefinitely (no
   public app-delete) — an ephemeral Modal run would leave a permanently
   visible, timestamped, subcommand-named record. Exactly the footprint
   ephemeral exists to suppress.
2. **Ephemeral-index gap.** Under STRICT_POLICY the ledger is memory-only; the
   next CLI process discovers a surviving ephemeral pod via the disk
   `EphemeralIndex` (spec 2026-06-27). The `EphemeralIndex.add` call is inlined
   in `_cmd_generate` only (`src/kinoforge/cli/_commands.py:616-635`) —
   `_cmd_upscale` / `_cmd_interpolate` never index, on ANY provider. Bare
   `--ephemeral` on Modal (or upscale/interpolate on RunPod) leaves a live pod
   no later invocation can discover.
3. **Sweeper reap gap.** The sweeper's ephemeral reap (spec 2026-06-28) probes
   rows via `provider.probe_runtime` (`src/kinoforge/core/reaper_actor.py:385`).
   `ModalProvider` has no `probe_runtime` → every Modal row would classify
   `SKIP_NO_PROBE` (`src/kinoforge/core/reaper.py:295`) and never be reaped.
   A bare-`--ephemeral` Modal app would idle-burn until Modal's own
   scaledown, invisible to kinoforge (memory-only ledger + no reaper).

## Decisions (user-validated 2026-07-12)

- **Residue contract:** accept the opaque stopped-app residue. Ephemeral on
  Modal = opaque `kinoforge-{8hex}` name + no local files + memory-only ledger +
  store scrubbed + app stopped. The lingering stopped-app entry in
  `modal app list` is a documented provider-internal-log carve-out, same class
  as the ephemeral-workspaces spec's provider-internal-logs exclusion (§72).
  No prompt, output, alias, or capability key is recoverable from it. Fuller
  scrub (true app deletion) has no documented public SDK path — out of scope.
- **Scope:** full parity — capability + naming + ephemeral-index + sweeper reap
  — in ONE spec, implemented in three phases (EM1 → EM2 → EM3), each
  independently shippable and live-verifiable.
- **Index-add home:** shared CLI-layer helper (not orchestrator) — the add
  needs `cfg` cap-key/wak derivation, which is CLI-layer business.
- **Opaque naming:** reuse RunPod's exact mechanism (`secrets.token_hex(4)` +
  the `pod_name_includes_alias` gate). Symmetry over novelty.
- **probe_runtime:** build on the M5 Modal util probe (`GET /util`,
  `ModalUtilEndpoint.read_util`, spec 2026-07-12-modal-util-probe) — no new
  in-container surface.

## Background — how ephemeral works today (investigation summary)

- `EphemeralSession` (`src/kinoforge/core/ephemeral.py`) binds STRICT_POLICY:
  5 write gates off (ledger, profile cache, batch summary, cost sidecar,
  heartbeat touch), `delete_on_completion=True` (store scrub on `__exit__`
  via `store.delete_run(run_id)` — output/ publish is the sole exempt zone),
  `memory_only_run_id=True`, `pod_name_includes_alias=False`,
  `force_debug_show_secrets_off=True`.
- **Pod lifecycle is deliberately independent** of the `__exit__` scrub: pods
  are destroyed only by `--no-reuse` (orchestrator `single=True`,
  `src/kinoforge/core/orchestrator.py:1434-1439`) or by the sweeper. Bare
  `--ephemeral` intentionally leaves the pod warm for cross-CLI reuse via the
  EphemeralIndex.
- The hosted-engine DELETE path (`RemoteSubmitPollBackend._delete`,
  `src/kinoforge/core/remote_backend.py`) is irrelevant to pod providers:
  for pods, `destroy_instance` IS the provider-side scrub.
- Modal's `destroy_instance` (`src/kinoforge/providers/modal/__init__.py:212`)
  runs `modal app stop <name> --yes`, polls `modal app list` until the app
  leaves the active set (~120 s bound), pops the in-memory deployment record.
  Adequate as the ephemeral destroy leg given the residue decision above.
- The `EphemeralIndexRow` (`src/kinoforge/core/warm_reuse/ephemeral_index.py:32`)
  is provider-agnostic: `{id, warm_attach_key, kinoforge_key, endpoints,
  provider, created_at_local}`. Modal's non-rebuildable `.modal.run` URL rides
  in `endpoints` — the same property the M5 warm-reuse fix (`1cb4299`)
  persisted through the ledger.
- The shared HF-cache Volume (`kinoforge-hf-cache`) persists across runs by
  design (weight cache). It stores model weights only — never job inputs or
  outputs — so it is NOT ephemeral residue. Unchanged.

## Phase EM1 — correctness + honesty (unblocks `--ephemeral --no-reuse`)

### A. Opaque Modal app name under ephemeral

In `ModalProvider.create_instance`
(`src/kinoforge/providers/modal/__init__.py:80`), before building
`ModalAppRequest`:

```python
_eph = EphemeralSession.current()
if _eph is not None and not _eph.policy.pod_name_includes_alias:
    app_run_id = f"eph-{secrets.token_hex(4)}"
else:
    app_run_id = spec.run_id
```

`ModalAppRequest.run_id` gets `app_run_id`, so the app deploys as
`kinoforge-eph-{8hex}` and the `_deployments` record + returned
`Instance.id` carry the same opaque id — `destroy_instance`, the ledger
(memory-only), the ephemeral index, and `probe_runtime` all key off it
consistently. NOTE: the instance id visible to the orchestrator/ledger/index
IS the opaque id; the store `run_id` (used for artifact namespacing +
`delete_run`) is unchanged — those are already distinct concepts in the
orchestrator signature (`instance` vs `run_id`).

The `eph-` infix mirrors nothing secret (RunPod's ephemeral pods are plain
`kinoforge-{8hex}`; the infix is purely so a human scanning `modal app list`
can tell which stopped apps were ephemeral runs — useful given stopped apps
linger there forever, unlike RunPod pods which vanish). The name is LOCKED as
`kinoforge-eph-{8hex}` (the offline test pins the regex). Leak requirement:
**no subcommand, no timestamp, no alias, no capability hash in the name.**

Non-ephemeral naming is byte-identical to today (`kinoforge-{spec.run_id}`) —
golden-locked by existing tests.

### B. Capability entries + preflight message

- `src/kinoforge/core/ephemeral.py` — add to `EPHEMERAL_CAPABILITIES`:
  `("comfyui", "modal"): True`, `("diffusers", "modal"): True`.
- `src/kinoforge/cli/_main.py` `_preflight_error_block` — the "Use one of
  these instead" lines for comfyui/diffusers gain modal in the provider list.

EM1 exit criteria: `--ephemeral --no-reuse` upscale on Modal runs end-to-end —
opaque app name, no ledger row, no batch/profile/sidecar writes, store scrubbed
on exit, app stopped, output/ artifact present.

## Phase EM2 — ephemeral-index parity (bare `--ephemeral` discoverable)

### C. DRY-lift the EphemeralIndex.add into a shared CLI helper

Extract `src/kinoforge/cli/_commands.py:616-635` into:

```python
def _ephemeral_index_add(ctx: SessionContext, cfg: Config, instance: Instance) -> None:
    """Index a surviving ephemeral pod for cross-CLI discovery (no-op when
    no EphemeralSession is active)."""
```

Body = the existing block (session gate, `EphemeralIndexRow` construction from
`cfg` wak/cap-key + `instance.endpoints`/`instance.provider`,
`datetime.now().isoformat()` — local TZ per convention). Call it from:

- `_cmd_generate` (replacing the inline block),
- `_cmd_upscale` (after `_orchestrator.generate` returns `returned_instance`,
  when an instance came back and `--no-reuse` was NOT used),
- `_cmd_interpolate` (same shape).

The existing AST-scan invariant test (`60872f8` — every `EphemeralIndex.add`
is session-gated) must keep passing: the gate lives inside the helper, and the
scan is updated to accept the helper as the single gated add-site.

Row content is already provider-agnostic; a Modal row carries
`endpoints={"8000": "https://…modal.run"}` and `provider="modal"`.

### D. Warm-attach discovery for Modal rows

The matcher (`src/kinoforge/core/warm_reuse/matcher.py:156-268`) and the
`_scan_warm_candidates` path are provider-agnostic (endpoint replay from the
row, per fix `000c084` + `b28311a`). Expected: zero code, verify by test. If a
provider-specific assumption surfaces (e.g. a RunPod-proxy URL pattern in the
health preflight), fix is in-scope for EM2.

EM2 exit criteria: two consecutive bare-`--ephemeral` Modal invocations —
second warm-attaches the first's live app via the index (no redeploy), and the
index row is removed when the matcher consumes/invalidates it per existing
semantics.

## Phase EM3 — sweeper reap parity (orphans get reaped)

### E. `ModalProvider.probe_runtime`

New method mirroring RunPod's (`src/kinoforge/providers/runpod/__init__.py:633`):

```python
def probe_runtime(self, instance_id: str) -> RuntimeProbe | None:
```

- App absent from `list_instances()` (active set) → `RuntimeProbe(found=False,
  …all None)` → reaper classifies GC_404, drops the row, no destroy.
- App active, `/util` reachable (via `ModalUtilEndpoint.read_util` with the
  ledger/deployments/row-resolved `.modal.run` URL) → `found=True`,
  `gpu_util_pct=snapshot.gpu_util_percent`, `cpu_pct=snapshot.cpu_percent`,
  `container_uptime_s=snapshot.uptime_seconds`, `cost_per_hr` from the
  deployment record when present else None.
- App active but `/util` unreachable/5xx → `found=True`, util fields None,
  `error` set (partial probe — reaper stays conservative, no false reap).
- Never raises (reaper's `PROBE_FAILED` path is belt-and-suspenders).

Resolver note: in the sweeper process there is no live `_deployments` cache —
the probe resolves the URL from the EphemeralIndexRow `endpoints` (threaded by
the reaper via the entry) or the ledger, mirroring how
`build_util_endpoint_for`'s modal branch takes an injected ledger-backed
resolver (`src/kinoforge/_adapters.py:296-301`). The implementation plan picks
the exact seam; the contract is: id → base URL → `UtilSnapshot` → `RuntimeProbe`.

### F. Sweeper wiring verification

`provider_util_supported("modal")` is already True (util-probe workstream
`35c2068`), so `_classify_ephemeral`'s stall/idle gates engage once
`probe_runtime` exists. Expected: no classify/actor changes. In-scope
verification: a Modal ephemeral row flows sweep → probe → verdict →
`act_on_verdict` destroy/GC path, including that `destroy_instance` under the
reaper resolves the app name for an index-only (never-ledgered) instance —
`destroy_instance` already falls back to `f"kinoforge-{instance_id}"` when the
deployment record is absent, which matches the opaque naming from EM1 as long
as the Instance.id is the `eph-{8hex}` token (it is — see A).

EM3 exit criteria: an idle bare-`--ephemeral` Modal app is reaped by
`kinoforge sweeper` (verdict IDLE_REAP or STALL_REAP → app stopped → row
removed), and a row pointing at an already-gone app is GC'd (GC_404) without a
destroy call.

## Error handling

- `probe_runtime` never raises; partial probes report `error` and keep
  `found=True` so the reaper cannot false-reap on a flaky `/util`.
- Opaque-name collision (`token_hex(4)`): Modal rejects duplicate app names
  loudly at deploy — acceptable (1-in-4B), retriable, no silent overwrite.
- Store scrub failure: existing `EphemeralStoreCleanupFailedError` with
  `manual_cleanup_command` (unchanged).
- Preflight continues to refuse unsupported combos before any spend; modal
  combos now pass.
- `--ephemeral --no-reuse` destroy failure: existing WARN + surviving-pod
  naming path (orchestrator `1442-1449`) applies unchanged; the app name in
  the WARN is the opaque one.

## Testing

Offline (all phases, TDD per test-design skill — each test names the bug it
catches):

- **A:** ephemeral create → app name matches `^kinoforge-eph-[0-9a-f]{8}$`;
  non-ephemeral create → name unchanged `kinoforge-{run_id}` (bug: name leak /
  regression of normal naming). Injected fake modal module — no network.
- **B:** capability lookups return True for both modal combos; preflight error
  block for a still-unsupported combo names modal in the alternatives (bug:
  gate flip forgotten / message stale).
- **C:** all three handlers call the shared helper; helper no-ops without an
  active session; row for a Modal instance carries the `.modal.run` endpoint
  map (bug: upscale/interpolate pods invisible to discovery). AST-scan
  invariant updated + green.
- **E:** probe maps `UtilSnapshot` → `RuntimeProbe` field-for-field; absent
  app → `found=False`; `/util` 5xx → `found=True` + `error` (bug: false reap
  or unreapable rows). Injected HTTP + lister seams.
- **F:** reaper-actor integration test drives a synthetic Modal ephemeral row
  through sweep → GC_404 and → IDLE_REAP with a stubbed provider (bug: Modal
  rows stuck at SKIP_NO_PROBE forever).

Live (RED scaffolds committed pre-spend, per durability rules; util-poll +
teardown-verify + `kinoforge list` per CLAUDE.md):

- **EM1 smoke:** `--ephemeral --no-reuse` upscale (1080p cfg, fixture clip) —
  assert opaque app name in `modal app list` output, no ledger row, store
  scrubbed, artifact in output/, teardown clean.
- **EM2 smoke:** bare `--ephemeral` t2v (cheap 1.3B/A10 cfg) twice — second
  attaches, then explicit destroy + index cleanup.
- **EM3 smoke:** bare `--ephemeral`, wait idle threshold, one sweeper tick —
  reap observed, `modal app list` shows stopped, index row gone.

Live-smoke budget: ≤ $1 total (1.3B/A10 ~$0.05/run; 1080p upscale ~$0.10).

## Out of scope

- True Modal app deletion (no public SDK path; residue decision above).
- Volume scrubbing (`kinoforge-hf-cache` is a weight cache, not run residue).
- ComfyUI-on-Modal live proof (capability entry ships; comfyui has never run
  on Modal — the entry is justified because the scrub contract is engine-
  independent for pod providers; first comfyui-modal run will exercise it).
- Heartbeat-substrate parity for Modal (separate axis; sweeper's ephemeral
  path probes util, not heartbeat).

## Milestone ordering / durability

EM1 → EM2 → EM3, each: offline tests green → RED live scaffold committed →
live smoke → docs (PROGRESS; `successful-generations.md` only if a run
qualifies AND is not `--ephemeral` — note the EM smokes are ephemeral runs and
therefore MUST NOT be logged there; evidence lives in the live-test files +
PROGRESS).
