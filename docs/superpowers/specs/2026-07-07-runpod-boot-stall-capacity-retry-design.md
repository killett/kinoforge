# RunPod Boot-Stall Fast-Fail + Capacity-Retry — Design

**Date:** 2026-07-07
**Status:** validated (brainstorm approved)

## Problem

RunPod transiently fails pods across the lifecycle, and kinoforge handles two of
those failure modes badly:

1. **Boot-stall (live pod, dead server).** A pod boots, RunPod keeps reporting it
   `running`, but its server never serves `/health` — the provision script
   crashed under `set -euo pipefail` (its trap logs `[bootstrap-trap] rc=<N>`),
   or a weight download hung. `wait_for_ready` polls `/health` for the full
   `boot_timeout` (900s) before raising `ProvisionTimeout`. Observed 2026-07-06
   (pod `x1skfnuk5m4cwb`): a 15-minute dead-wait. `get_instance` never went
   terminal and never `KeyError`'d, so neither existing guard fired.

2. **Capacity miss at create.** `podFindAndDeployOnDemand` returns
   `"no longer any instances available with the requested specifications"` (or the
   `"…with enough disk space"` variant) when the offer listed by `find_offers`
   is gone by create time. Today this fails the whole run instantly. Observed
   2026-07-07: 6 consecutive misses over ~7 min for secure A100/H100 80GB.

Related but **out of scope** (deliberately, per brainstorm): auto-retry the whole
generation on a fresh pod (self-heal). A dead boot still ends the run — but in
~2–3 min with an honest error instead of 900s, and capacity droughts stop
failing on the first miss.

## Non-goals

- No auto-reprovision / self-heal orchestration.
- No change to the mid-job POD_GONE path (already shipped `91744b3`/`b1e88e1`/`ac1e3c3`).
- No new CLI flag (the cfg field covers the knob; YAGNI).
- Non-RunPod providers (local, comfyui-on-other, hosted-Bearer) unaffected — every
  new behavior sits behind a seam that defaults to today's behavior.

---

## Component 1 — Boot-stall fast-fail

### Seam: `BootLivenessProbe`

A new injectable probe, threaded into `wait_for_ready` the same way `get_instance`
already is. Default `None` → today's poll-until-timeout behavior preserved
(comfyui / local / any provider that supplies no probe is untouched).

```python
class BootVerdict(StrEnum):
    ALIVE = "ALIVE"        # progressing or indeterminate-but-present → keep waiting
    GONE = "GONE"          # pod reclaimed (get_instance KeyError) → abort
    STALLED = "STALLED"    # provision script died / util flatline → abort
    UNKNOWN = "UNKNOWN"    # probe error → treat as ALIVE (never a false abort)

class BootLivenessProbe(Protocol):
    def check(self, instance_id: str) -> BootVerdict: ...
```

The RunPod provider supplies the concrete probe (it owns the util endpoint and can
build the `:8001/bootstrap.log` URL from `instance.endpoints`). The probe is a
small stateful object (holds prior mem/disk readings + a consecutive-flatline
counter across calls).

### Verdict logic (RunPod impl)

Per `check(instance_id)`:

- **GONE** — the existence probe (`RunPodGraphQLUtilEndpoint.probe` → `(exists, snap)`,
  or `get_instance` KeyError) reports the pod absent.
- **STALLED** — either:
  - `bootstrap.log` tail (`GET https://<pod>-8001.proxy.runpod.net/bootstrap.log`)
    contains `[bootstrap-trap] rc=<nonzero>` — ground truth the provision script
    exited nonzero; **or**
  - util **flatline** for **K consecutive** checks: `cpu_percent == 0` AND
    `|mem_delta| ≈ 0` AND `|disk_delta| ≈ 0` (deltas vs the prior check's snapshot).
    GPU 0% alone is NOT sufficient (a CPU-bound download or model-load is legitimately
    GPU-idle) — CPU+mem+disk must ALL be flat.
- **ALIVE** — any signal of progress (nonzero CPU, or mem/disk growing).
- **UNKNOWN** — probe fetch/parse errored → caller treats as ALIVE (keep waiting).

### Guardrails against false positives

- **Grace window** (`_BOOT_STALL_GRACE_S`, default 90s): stall-checking does not begin
  until the pod has been booting this long — early boot has legitimate quiet moments.
  `GONE` is honored immediately (no grace — a vanished pod is unambiguous).
- **K consecutive** (`_BOOT_STALL_CONSECUTIVE`, default 3): one flat reading never trips;
  ~90s of provable inactivity (3 × 30s cadence) does.
- **Throttled cadence** (`_BOOT_PROBE_INTERVAL_S`, default 30s): the liveness probe runs
  on its own interval, NOT on every `/health` poll — bounds GraphQL + log-fetch cost.
- **Reset on progress**: any `ALIVE`/`UNKNOWN` verdict resets the flatline counter, so a
  stalled-then-resumed download does not accumulate toward a false STALLED.

### `wait_for_ready` integration

In the existing poll loop (`engines/diffusers/__init__.py` ~1096 and
`engines/comfyui/__init__.py` ~1402): after the `/health` GET fails and the existing
terminal-status check, when a probe is present and the throttle interval has elapsed,
call `probe.check(id)`:

- `GONE` → `raise ProvisionFailed("pod <id> vanished during boot")`
- `STALLED` → `raise ProvisionFailed("pod <id> boot stalled: <reason>")`
- else → continue (unchanged wall-clock `boot_timeout` still bounds the worst case).

The existing `KeyError` from `get_instance` is caught and mapped to the same
`ProvisionFailed("… vanished during boot")` (clean error, no unhandled crash).

---

## Component 2 — Capacity-retry + widened config

### Typed error — reuse existing `CapacityError`

`CapacityError` already exists (`core/errors.py`) and `_create_with_offer_retry`
already catches it per-offer. RunPod's `_create_pod`
(`providers/runpod/__init__.py` ~903) already raises `CapacityError` — but only
for the message substring `"resources to deploy"`. The two variants we actually
hit — `"no longer any instances available with the requested specifications"` and
`"… with enough disk space"` — miss that match and fall through to a raw
`ValueError`, so the offer-retry never sees them.

Fix = **extend the existing capacity-message match** to also catch
`"no longer any instances available"`. No new error type: reuse `CapacityError`.
The substring match stays confined to the one place it already lives (the
provider), and the retry loop keys on the `CapacityError` type.

### Retry loop

Wrap the **find_offers + create** block (both live in
`_provision_instance_and_build_backend`, `orchestrator.py` ~679–748): on
`CapacityError` (from either an empty `find_offers` or all offers exhausted),
sleep `_CAPACITY_RETRY_INTERVAL_S` (default 25s), **re-query `find_offers`**
(capacity is fluid — a fresh query may surface a newly-free host), and retry —
until the `capacity_wait` deadline elapses, then re-raise the last `CapacityError`
clean. Every other exception propagates immediately (unchanged). Clock + sleep are
injected seams for tests.

### The knob: `lifecycle.capacity_wait`

New field on the lifecycle config block (`core/config.py`), a **duration string**
parsed to seconds exactly like its siblings (`boot_timeout: 15m`,
`idle_timeout: 30m`) — NOT a bare int. Default `5m`. `0` (or `0s`) → fail on the
first miss (smokes); larger → ride a drought (batches). Reuse the existing
lifecycle duration-parsing path; do not invent a second parser.

### Config widening — `examples/configs/upscale-flashvsr-1080p.yaml` only

```yaml
requirements:
  max_usd_per_hr: 3.00            # was 2.50 — admits H100 HBM3 ($2.69) + H100 NVL ($2.59)
  gpu_preference:
    - "NVIDIA A100 80GB PCIe"     # $1.19 (kept)
    - "NVIDIA A100-SXM4-80GB"     # $1.39 (added)
    - "NVIDIA H100 80GB HBM3"     # $2.69 (kept; now under cap)
    - "NVIDIA H100 NVL"           # $2.59 (added)
lifecycle:
  capacity_wait: 5m               # ride a ~5min drought (duration string, like boot_timeout)
```

Blackwell RTX-6000 (96GB, $1.69) is deliberately **excluded**: the FlashVSR BSA
wheel is cu124/torch2.4 and lacks Blackwell (sm_120) kernels — it would boot then
fail at inference. `interpolate-rife-60fps.yaml` is untouched (RIFE runs on a small
cheap GPU, not capacity-constrained).

---

## Testing

All unit-level, fakes only, no network/pod:

- **BootLivenessProbe verdict logic** — `rc!=0` tail → STALLED; flatline × K → STALLED;
  grace window suppresses early flatline; disk-growing or mem-growing → ALIVE (counter
  resets); existence-gone → GONE; probe fetch error → UNKNOWN. Each test names the bug it
  catches (test-design skill).
- **wait_for_ready** — inject a fake probe emitting a scripted verdict sequence; assert
  STALLED and GONE abort promptly with `ProvisionFailed`; assert `probe=None` preserves
  the existing poll-until-`boot_timeout` behavior (regression guard); assert the throttle
  means the probe isn't consulted every `/health` poll.
- **Capacity retry** — inject a create fn raising `CapacityUnavailable` N times then
  succeeding: asserts it retries within deadline, re-queries offers each attempt, and
  returns the eventual instance; a non-capacity error raises immediately; `capacity_wait=0`
  fails on the first miss; deadline exceeded re-raises `CapacityUnavailable`.
- **Config** — `capacity_wait` parses the duration string (`5m` → 300.0, `0` → 0.0) and
  defaults to `5m` when absent; the widened FlashVSR cfg loads with the four prefs +
  `max_usd_per_hr=3.00`.
- **Provider classification** — `_create_pod` maps both "no longer any instances available"
  variants to `CapacityError` (existing type); the pre-existing "resources to deploy" match
  still works; a non-capacity GraphQL error stays a raw `ValueError`.

## File structure

- **New:** `src/kinoforge/core/boot_liveness.py` — `BootVerdict`, `BootLivenessProbe` protocol.
- **New:** RunPod `BootLivenessProbe` impl (in `providers/runpod/`, uses the util endpoint +
  bootstrap.log fetch).
- **Modify:** `providers/runpod/__init__.py` — extend the `_create_pod` capacity-message match
  to raise the existing `CapacityError` for the "no longer any instances available" variants;
  expose a boot-liveness-probe factory for `wait_for_ready` wiring. (No new error type.)
- **Modify:** `engines/diffusers/__init__.py` + `engines/comfyui/__init__.py` — accept +
  consult the `BootLivenessProbe` in `wait_for_ready`; map `get_instance` KeyError → GONE.
- **Modify:** `core/orchestrator.py` — capacity-retry loop around `_create_with_offer_retry`;
  thread the probe from provider → engine.
- **Modify:** `core/config.py` — `lifecycle.capacity_wait` field.
- **Modify:** `provisioner.py` — thread the probe seam through `provision`.
- **Modify:** `examples/configs/upscale-flashvsr-1080p.yaml` — widened prefs/cap + capacity_wait.

## Defaults (tunable constants, stated for the plan)

| Constant | Default | Meaning |
|---|---|---|
| `_BOOT_STALL_GRACE_S` | 90s | no stall-check before this |
| `_BOOT_STALL_CONSECUTIVE` | 3 | flatline checks to trip STALLED |
| `_BOOT_PROBE_INTERVAL_S` | 30s | liveness-probe throttle |
| `lifecycle.capacity_wait` | `5m` (300s) | capacity-retry deadline (cfg duration string, overridable) |
| `_CAPACITY_RETRY_INTERVAL_S` | 25s | sleep between capacity retries |
