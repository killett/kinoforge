# Warm-attach teardown hang — design

**Date:** 2026-06-27
**Author:** Dr. Twinklebrane (operator) + Claude Opus 4.7 (1M context)
**Predecessor:** `docs/superpowers/specs/2026-06-27-ephemeral-warm-reuse-discovery-design.md` (now SHIPPED, all 7 tasks GREEN; live evidence at `tests/live/_ephemeral_warm_reuse_smoke_evidence.json`).

## Problem

Live smoke captured this session ran two back-to-back `kinoforge --ephemeral generate` invocations against RunPod pod `d3q7ejf6e910jv`. The discovery channel from the predecessor workstream works end-to-end:

- Run #1 — cold-boot, `running provisioner.provision for instance d3q7ejf6e910jv`, generate completed, subprocess exits cleanly with `generated: uri=...` line.
- Run #2 — `warm-reuse: attached to d3q7ejf6e910jv` emitted 9 s after run #1 finished, generate completed at 17:45:56,285 …

… then the run #2 subprocess **never exited**. The python process (pid 201720 in the captured run) stayed alive ≥8 minutes with zero new log output and 0 % CPU. The pod stayed alive on RunPod, accumulating cost, until manually destroyed via `pixi run kinoforge destroy --id`.

Run #1 (cold-boot path) does not exhibit this hang. Only the warm-attach path (`instance is not None` in `_cmd_generate`) hangs under `EphemeralSession`.

This blocks the live smoke test from going GREEN end-to-end and is a $/min liability for any operator who runs two back-to-back ephemeral generations and doesn't have the sweeper daemon active.

## Goal

Make `kinoforge --ephemeral generate` exit cleanly on the warm-attach path so the existing live smoke scaffold (`tests/live/test_runpod_ephemeral_warm_reuse_smoke.py`, committed `9ecd902` + rewritten `65aeefc`) passes verbatim, including its destroy + post-cleanup assertions.

## Non-goals

- No rewrite of the discovery channel — the predecessor workstream is done.
- No change to cold-boot or non-ephemeral code paths. Fix must be branch-gated to `(EphemeralSession.current() is not None) and (instance is not None) and (single is False)`.
- No new `py-spy` integration in the live smoke. Future work.
- No new live-spend tooling beyond one confirmation run.

## Approach

**Differential debug.** Run #1 (cold-boot, `instance=None`) does not hang; run #2 (warm-attach, `instance != None`) does. Same orchestrator entry, same `EphemeralSession`. The bug sits in code reachable only when `instance is not None` during the orchestrator's `deploy_session` exit.

Investigation is **offline-first**. A pytest test reproduces the hang with mocked provider + engine but a real `EphemeralSession` + real ledger/store + real `_cmd_generate` entry. Pytest faulthandler dumps the stack at 30 s, pinpointing the hung frame. After the fix lands, one live smoke run confirms end-to-end GREEN.

## Components

### C1 — Offline regression test

**File:** `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py`

**Behavior:**

- Uses `tmp_path`-backed `LocalArtifactStore`.
- Pre-seeds an `EphemeralIndex` row for a fake pod id.
- Stubs `kinoforge.core.registry.get_provider("runpod")` to return a fake provider:
  - `get_instance(pid)` returns a fake `Instance(id=pid, endpoints={"8188": "https://fake-...-8188.proxy.runpod.net"})`.
  - `list_instances()` reports the same instance.
  - `destroy_instance(pid)` no-op.
- Stubs the engine: `generate(...)` writes a tiny stub artifact to `tmp_path` and returns. `wait_for_ready` returns immediately.
- Stubs the heartbeat substrate to satisfy any util query with `gpu=0,cpu=0`.
- Invokes `_cmd_generate(args, ctx)` inside `with EphemeralSession(enabled=True):`.
- Asserts `_cmd_generate` returns `0` within 30 s, gated by `pytest --faulthandler-timeout=30`.

**Pre-fix expectation:** hangs ≥ 30 s, faulthandler dumps the stack, test FAILS with `Timeout (0:00:30)` and a traceback pointing at the hung frame.

**Post-fix expectation:** passes in < 5 s.

### C2 — Differential read + fix

**Files:** `src/kinoforge/core/orchestrator.py` (likely; could extend to `deploy_session`-adjacent modules).

**Process:**

1. Open `orchestrator.py`, read `deploy_session` + `generate` end-to-end.
2. Grep every conditional on `instance is None` / `instance is not None` / `single` in the exit path.
3. Match each conditional against the suspected hang frame surfaced by C1's faulthandler dump.
4. Identify the smallest possible fix in the offending frame.
5. Branch-gate the fix to `(EphemeralSession.current() is not None) and (instance is not None) and (single is False)` so cold-boot + non-ephemeral paths are byte-identical.

**Candidate suspects to verify against the dump:**
- `HeartbeatLoop.stop()` `Thread.join()` blocking forever because the worker is stuck on a network call to a pod whose proxy already 404'd.
- Pod-lock release missing on the warm-attach success path.
- In-memory ledger drain at session exit re-entering `Ledger.read` and stalling on the session lock.
- `EphemeralSession.__exit__` cleanup of `cli_loras` or `vault`.

**Hard rule:** the fix must NOT widen the surface area beyond the offending frame. No opportunistic refactors. If the fix needs a new field on a frozen dataclass, that's fine; if it needs a new module, push back through the brainstorming cycle.

### C3 — Live confirmation

**Action:** re-run `tests/live/test_runpod_ephemeral_warm_reuse_smoke.py` with `KINOFORGE_LIVE_TESTS=1` after `pixi run preflight` PASS. Cap: ~$0.10. The existing scaffold passes verbatim. Capture logs to `tests/live/_warm_attach_teardown_fix_evidence_run{1,2}.log` + a summary JSON.

## Error handling + risks

**R1: Offline test doesn't reproduce the hang.**
Means the mocks are too clean. Fallback: replace the mock heartbeat with a real `HeartbeatLoop` thread backed by a faked-out util endpoint that blocks indefinitely; this is the most likely culprit. If still no repro, escalate to live capture (out of scope for this spec — surface to operator + ask).

**R2: Fix regresses cold-boot or non-ephemeral.**
Branch-gate every change. Full `pixi run pytest tests/core tests/cli tests/integration -x` must stay green before commit. The existing `tests/core/test_ephemeral_*` + `tests/core/warm_reuse/` + `tests/core/test_lifecycle*` suites are the regression sentinel.

**R3: Live confirmation hangs again post-fix.**
Means the offline test did not cover the real path. Capture `py-spy dump --pid <runtime>` from the hung subprocess; treat as a new bug; loop back to C2 with the new stack. Pod gets destroyed via `pixi run kinoforge destroy --id <id>` after stack capture. Spend cap $0.10 per try, ≤ 3 tries before stopping to reassess with operator.

**R4: Spend overrun.**
`pixi run kinoforge sweeper start &` before any live invocation as safety net.

## Testing

- **Offline regression:** C1 hangs ≥30 s pre-fix (RED), passes < 5 s post-fix (GREEN). Faulthandler is the diagnostic.
- **Regression:** full `pixi run pytest tests/core tests/cli tests/integration -x` green.
- **Live:** existing scaffold `tests/live/test_runpod_ephemeral_warm_reuse_smoke.py` passes verbatim under `KINOFORGE_LIVE_TESTS=1` with `pixi run preflight` PASS. Acceptance includes destroy + post-cleanup assertions reaching GREEN.
- **Evidence:** live run log captured to `tests/live/_warm_attach_teardown_fix_evidence_run{1,2}.log` + summary JSON mirroring the prior gate's evidence file shape.

## Out of scope (filed for later)

- `py-spy` integration in the live smoke harness so future hangs surface immediately.
- Sweeper-daemon-as-a-test-fixture for live smokes.
- General audit of all `with deploy_session(...)` callers under `EphemeralSession`.
