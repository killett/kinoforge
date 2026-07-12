# Modal Milestone 5 — Warm-reuse + HF-cache live proof (Wan 2.1 1.3B / A10)

**Status:** validated 2026-07-12 (brainstorm).
**Depends on:** M1 (`successful-generations.md` §22, cfg `examples/configs/modal-wan-t2v-1_3b.yaml`),
the `HF_HOME=/cache/hf` + named-Volume wiring (commit `4f2376f`), and the provider-agnostic
`src/kinoforge/core/warm_reuse/` package.

## Goal

Prove, live, the two remaining open Modal threads on the cheap M1 config:

1. **Cross-CLI warm-reuse** — a second `kinoforge generate` process attaches to the still-alive Modal
   container (replaying the persisted `.modal.run` endpoint) instead of redeploying + cold-booting.
2. **HF Volume cache** — a fresh Modal container reuses the Volume-cached Wan weights (`HF_HOME=/cache/hf`)
   instead of re-downloading them.

Close both threads and log `successful-generations.md` §26.

## Background — what already exists (verified in code)

- **Warm window.** `ModalProvider` sets `scaledown_window = lifecycle.idle_timeout_s`
  (`providers/modal/__init__.py:122`), so a container stays warm for the idle window after a generation.
- **Endpoint recovery across processes.** `core/warm_reuse/ephemeral_index.py` (`EphemeralIndexRow`)
  persists `endpoints: dict[str, str]` per pod and **replays it onto the resolved `Instance`** after
  `provider.get_instance` returns a sparse Instance (docstring lines 45-49, 73-76). So the non-deterministic
  `.modal.run/…-build-<hash>` URL — unrecoverable from `modal app list` — is recovered from the index.
- **Provider-agnostic match.** `core/warm_reuse/matcher.py::find_warm_attach_candidate` matches on
  `cfg.capability_key()` + `capability_key_hex`, with no RunPod string-gating.
- **HF cache plumbing.** Named Volume `kinoforge-hf-cache` mounted at `/cache/hf`
  (`providers/modal/_app.py:118,137`) + `env.setdefault("HF_HOME", "/cache/hf")`
  (`providers/modal/__init__.py:105`).
- **M1 cfg fetches weights at RUNTIME** (it predates the image-bake; plain Wan t2v has no composed
  sub-engine install to bake), so the Volume cache is exactly the mechanism under test — not a moot,
  already-baked artifact.

**The one unknown this milestone must resolve:** whether a Modal `create_instance` Instance actually
*flows into* the `EphemeralIndex` write on run 1 and is *found* by the matcher on run 2. If the index
write is RunPod-only, run B has nothing to attach to. Hence Phase 0 below.

## Approach — verify-then-prove (config + a small possible patch + a live sequence)

No new provider abstraction. One offline phase that may add a small provider-agnostic patch, then a
three-generation live sequence on the M1 cfg.

### Phase 0 — offline write+match trace (before any spend)

Unit-level test that the Modal path populates and matches the warm-reuse index, mirroring the proven
RunPod flow:

- After a Modal `create_instance` returns an `Instance` with `id` + `endpoints["8000"]` + capability tags,
  the warm-reuse **write** records an `EphemeralIndexRow` carrying those endpoints + `capability_key_hex`.
- `find_warm_attach_candidate(cfg=…)` for the same `capability_key()` **returns that row** and its endpoints
  replay onto a sparse Instance.

If a gap exists (e.g. the index write is gated to RunPod, or Modal instances never reach the write
call-site), close it with the smallest provider-agnostic patch that puts Modal on the same path. Commit
RED→green **before** the live scaffold. This is the only place code may change; if Phase 0 shows the path
already covers Modal, Phase 0 is characterization-only (still committed).

### The live sequence — 3 generations, all separate CLI invocations

Standard "cold + warm + cold" pattern; prompts read verbatim from
`examples/configs/prompts/field-realistic.txt` and one sibling (per the standard-test-prompt rule).
All runs use the **default warm-reuse** (NO `--no-reuse`) except the final teardown.

| Run | Prompt | Expectation | Proves |
|---|---|---|---|
| **A — cold** | prompt 1 | Cold boot: deploy app, fetch 1.3B weights → Volume, write index entry. Stays warm at end. | baseline |
| **B — warm-attach** | prompt 2 | Within idle window: matcher hits the warm entry, replays endpoint, POSTs to the **live** container. NO new `Building image`, NO new deploy URL, same app, wall-clock ≪ A. | cross-CLI warm-reuse on Modal |
| **C — cold-cache** | prompt 3 | **Destroy the app after B** (Modal stop == destroy; the named Volume survives), then generate → **fresh deploy** whose boot **skips the weight download** because `/cache/hf` already holds the weights. Fresh boot present, HF re-download absent, boot < A. | HF Volume cache (== the preemption-recovery scenario) |

Run C uses **destroy-then-fresh-deploy** rather than waiting out the 20-min scaledown: deterministic, and
it *is* the real preemption/cold-recovery scenario the Volume cache exists for (the Volume is named and
survives app deletion).

### Teardown

The final run (or an explicit post-C destroy) leaves nothing warm. Verify per the house rules:
`kinoforge list` → `No running instances` + `No instances recorded in ledger`, AND
`modal app list` → no running kinoforge app (stopped-but-listed is fine).

## Testing

- **Offline (Phase 0):** `tests/test_modal_warm_reuse_index.py` (or the nearest existing warm-reuse test
  module) — the write+match trace above; green before spend. Any patch lands with its own RED→green.
- **RED live scaffold:** `tests/live/test_modal_warm_reuse_hf_cache.py`, marked `pytest.mark.live`
  (deselected under `-m 'not live'`), mirroring the M4 scaffold shape (`INTERPOLATE_CMD`-style constants
  documenting the 3-run sequence + an xfail contract). Committed **before** any live spend (durability rule).
- **Live proof (controller-driven):** the 3-run sequence, monitored via app-state + orchestrator log
  (Modal has no util probe; poll app liveness, not `est_spend`). Frame-QA the distinct-prompt outputs.

## Acceptance criteria

- Phase 0 offline test green (write→match trace); any gap-closing patch committed RED→green.
- Run A: valid 480×480 mp4; index entry written (endpoints + capability_key_hex).
- Run B: orchestrator log shows **warm-attach, not redeploy** — no new image build, no new `.modal.run`
  deploy line, attaches to Run A's app; wall-clock materially < Run A.
- Run C: after explicit destroy, a **fresh** boot whose log shows the **weight download step skipped /
  cache-hit** (weights already on `/cache/hf`); boot < Run A's cold boot. (At 1.3B the time saving is
  modest — the **binary presence/absence of the download** is the proof, not the seconds.)
- All three outputs frame-QA'd (at least the two distinct prompts); no ⚠️.
- Teardown verified clean (`kinoforge list` + `modal app list`).
- `successful-generations.md` §26 written; `PROGRESS.md` + the Modal-gotchas memory updated.

## Known risks

1. **Phase-0 write-side gap (primary).** If Modal instances aren't written to the `EphemeralIndex`, run B
   silently cold-boots instead of attaching. Mitigation: Phase 0 catches it offline; the fix is in-scope
   and small (provider-agnostic).
2. **Modal URL longevity.** The warm `.modal.run` URL must stay reachable from a fresh process for the
   idle window. If Modal rotates/expires it faster than expected, run B fails to attach — surfaced by the
   live log; fall back to documenting the observed warm-window.
3. **1.3B download is small**, so the cache time-delta is modest. Accepted per the model-tier decision;
   the proof is the download step's presence/absence, not wall-clock.
4. **No util probe on Modal** — monitor app-state + log, not `est_spend` (per the live-smoke rule).

## Non-goals

- No LoRA swap — plain t2v; the `/lora/set_stack` inventory-convergence step is a no-op here.
- No A14B tier (cost).
- No warm-reuse of the baked engines (FlashVSR/RIFE bake weights into the image → the Volume cache is
  moot for them).
- No same-process reuse work (already functional via `_deployments` URL caching).

## User decisions (already made, 2026-07-12)

- **Proof scope: both** warm-attach AND cold-cache (the two halves of the two open threads).
- **Model tier: Wan 2.1 1.3B / A10** throughout (cheap; cache time-delta modest but the download's
  presence/absence is the binary proof).
- **Run C via destroy-then-fresh-deploy** (deterministic; the real preemption-recovery scenario), not
  waiting out scaledown.
- **Phase-0 write-side gap fix is in-scope** for this milestone (not split out).
