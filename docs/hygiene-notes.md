# Hygiene notes — intentionally kept smells

This file records code smells that periodic hygiene passes have evaluated and
deliberately **left in place**, with the reasoning. Future passes should
consult this file before re-flagging the same items.

Entries here are NOT bugs and NOT TODOs — they are conscious "leave it"
decisions, captured so we do not re-litigate them on every pass.

---

## Duplicated provision branch in `core/orchestrator.py`

**Location:** `src/kinoforge/core/orchestrator.py` — the cache-miss
("discovery") branch and the post-cache-hit branch each contain an
`if resolved_engine.requires_compute: ... else: backend = resolved_engine.backend(None, cfg_dict)`
block that differs only in passing `for_discovery=True` vs
`for_discovery=False` to `_provision_instance_and_build_backend`.

**Why kept:** the two blocks live inside a non-trivial control flow that
already has clear step-numbered comments and distinct preconditions
(`ProfileNotCached` vs `backend is None`). Extracting a small helper is
behavior-preserving in principle but moves a load-bearing branch behind one
more layer of indirection. The duplication is local (single function) and
cheap to read. Reconsider if a third caller appears or if the branches start
diverging.

---

## 2026-07-16 whole-repo audit — intentional keeps

Full audit: `docs/hygiene-audit-2026-07-16.md`. Items below were evaluated
and LEFT; do not re-flag without new evidence.

- **`wan_t2v_server.py` `_run_upscale_job` vs `_run_interpolate_job` mirroring**
  (`src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:2106,2250`) —
  deliberate 1:1 parallelism, documented in-file (comment block near :2193);
  shared invariants (result-before-done, `to_thread`, finally-cleanup) are
  intentionally restated per block. `_run_swap_job` differs structurally
  (rollback/eviction) and is NOT part of the mirror.
- **`providers/runpod/heartbeat.py` write-path residue** (`:48`
  `_POD_EDIT_JOB_MUTATION`, `:118` `_merge_marker`) — C33-m write-disable is
  asymmetric on purpose; module docstring records B5b resumption criteria.
- **`providers/runpod/balance.py:30` third Bearer-auth closure variant** —
  in-file comment documents "no shared transport" intent; only the util.py ↔
  heartbeat.py pair is true duplication.
- **`tools/_uptime_field_sweep_log.jsonl` location** — PROGRESS.md references
  this exact path; moving it to `tests/live/` breaks the pointer.
- **`tools/repro_runpod_uptime.py`** — retained per PROGRESS for a deferred
  external RunPod bug report, even though the C33-Q3 sweep family around it
  is dead.
- **`tools/flashvsr_debug_matrix.py`** — investigation closed (`e82b0d1`) but
  the tool is a generic warm-pod `/upscale` variant harness; reusable for the
  next quality regression.
- **`tools/probe_civitai_throughput.py:43-47` provider-private imports** —
  acceptable coupling for a diagnostic; breaks loudly at runtime on rename.
- **`tests/smoke/local_cpu/test_lora_swap_matrix.py:72` thin outer assert**
  (`len(report.steps) == 4`) — real assertions live inside
  `tests/_smoke_harness/matrix.py::run_matrix`; by design.
- **Mega test files** (`tests/core/test_orchestrator.py` ~2.9k,
  `tests/core/test_config.py` ~2.0k, `tests/engines/test_comfyui.py` ~2.0k
  LOC) — split opportunistically when next touched, not as a dedicated pass.
- **`tests/_fixtures/test_fake_auth.py` collected inside the helper package**
  — convention nit; harmless.
- **Alive despite dead appearance** (Chesterton fences checked — do NOT
  delete): `tools/c28_provision_s3_diagnostics.py` (bucket consumed by
  `core/orchestrator.py:248`), `tools/comfyui_ui_to_api.py` + `_vendored/`
  (ComfyUI goldens wired), `tools/probe_pod_watchdog.py` (selfterm live,
  `pixi run probe-watchdog`), `tools/bootstrap_kms.py`
  (`tests/stores/live/conftest.py` directs operators to it).
