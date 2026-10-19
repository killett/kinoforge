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

---

## 2026-10-18 whole-repo audit — intentional keeps

Full audit: `docs/hygiene-audit-2026-10-18.md`. Items below were evaluated
and LEFT because an in-code comment, test or design doc explains them; do not
re-flag without new evidence. Line numbers are at `5f8a52a0`.

**Re-evaluate, don't just keep:** the "Duplicated provision branch in
`core/orchestrator.py`" entry at the top of this file. Its only distinguishing
argument, `for_discovery=True/False`, has been unread since `4fe46014`, so the
cache-miss (`:2088-2126`) and cache-hit (`:2135-2172`) blocks are now
semantically identical. The original reason (distinct preconditions, local
duplication) still holds, but the "reconsider if the branches start
diverging" trigger has inverted — they converged.

Also, `cli/_commands.py:2869-2871`'s `endpoints.get("http")` is NOT a keep:
no provider emits that key (audit NEEDS DISCUSSION #18).

Core / CLI:
- `core/strategy.py:55-61` `_audio_mode` written, never read — "INERT SEAM —
  deliberate, 2026-09-17" (`core/config.py:454` points at it).
- `core/interfaces.py:83-90` vs `core/config.py:1010-1017` Placement defaults
  duplicated (portable dataclass vs YAML model) — guarded by
  `tests/validation/test_field_support_check.py:110`. (Lifecycle defaults have
  the same split WITHOUT a guard and disagree — that one is audit #4, not a keep.)
- `core/orchestrator.py` `ledger: Any  # noqa: ANN401` (×6) — duck typing so
  core never imports a concrete store; contract spelled out at `:1367-1400`.
- Repo-wide `# noqa: BLE001/SLF001/PLC0415/ARG001` — those codes are not
  enabled; the comments annotate intent, they are not stale suppressions.
- `cli/_reconcile.py:18-24` launch-phase literals copied from
  `core/launch_phase.py` on purpose (grace parity guarded by
  `tests/core/test_reaper_launching_rows.py:189`). The stated reason
  ("imported on every CLI command") is weaker now that `_commands.py` imports
  `core.launch_phase` anyway.
- `cli/_commands.py:778-784` upscale/interpolate/text use a simpler attach
  chain than generate/batch, with a different mutex exit code (2 vs 1).
- `cli/_commands.py:3583-3596` `--attach-pod` refuses an empty endpoint map
  ("DELIBERATE DIVERGENCE" from `_resolve_warm_endpoints`);
  `:4135-4141` status merges tags only; `:329-332` `_recorded_instance_for_key`
  never raises.
- `cli/_commands.py:2995-3000` walrus inside the `if` — kept for the AST
  invariant in `tests/test_ephemeral_index_write_gated.py`.
- `cli/_commands.py:137-139,1016-1019` read `_cli_clock`/`generate` through
  `sys.modules["kinoforge.cli"]` so test monkeypatches on the shim take effect.
- `cli/_main.py:425-432` `_propagate_session_globals` reaches into argparse
  internals (no public API; the matrix test catches breakage).
- `outputs/__init__.py:39-67`, `outputs/local.py:206-220` sink registry unused
  in production — the documented future-sink seam (`core/config.py:1416-1418`).
- `cli/batch_formatters.py:48` `[idx+1/run_id]` prefix — specified that way
  (`tests/cli/test_batch_formatters.py:73`).
- `core/image_dir.py:37-42` `_ANIMATED_SUFFIXES` vs `core/animation.py:44-50`
  `_WRITERS` — two encodings, parity test from `1f9460e7`.
- `core/{heartbeat,util,balance}_endpoints.py` — three distinct capabilities
  (Protocol + one-line gate each), no shared transport; not duplication.
- `core/reaper.py:49-53` `Verdict.LAUNCHING` absent from
  `DEFAULT_APPLY_POLICY` and falls to `no_op` BY DESIGN (U64) — unlike
  RESTART_LOOP_REAP/DEGRADED_REAP, which are audit bug B7.
- `core/reaper.py:476-482` `_EPHEMERAL_GC_404_GRACE_S` deliberately not
  configurable (its doc pointer to `_reconcile._LAUNCHING_GRACE_S` is stale —
  point it at `launch_phase.LAUNCHING_GRACE_S`).
- `core/pool.py:28-100` `_DaemonThreadPoolExecutor` overrides private stdlib
  `_adjust_thread_count` (U31/U13 exit hang) — pinned by
  `tests/core/test_pool_exit_does_not_hang.py`.
- `core/heartbeat_loop.py:296`, `core/sweeper.py:294` broad per-tick `except`
  — "a single bad tick must not kill the loop".
- `core/lora_apply.py:141-159` `_engine_lora_support` degrades to the noisy
  `SERVER_HTTP` answer on purpose (U56).
- `core/batch.py:10-12,88-95` re-exports `batch_models` dataclasses for six
  test-module import sites.
- `core/lifecycle.py:909` `destroy_confirmed(clock=…)` "reserved for future use".

Providers / stores / validation:
- `nothing_booked_errors() -> ()` re-declared by runpod/modal/local — the
  explicit declaration is required by
  `test_every_provider_declares_its_nothing_booked_errors`.
- `providers/runpod/__init__.py:1614-1673` `_create_serverless` — GraphQL is
  invalid (U46) but kept so `runpod-diffusers-serverless.yaml` stays covered
  by the launch goldens; refused at preflight
  (`validation/checks/unimplemented_mode.py:16-23`).
- `providers/runpod/selfterm.py:17-30,170-172` `idle_timeout` does not measure
  idleness — B4 option 2, deliberate.
- `providers/runpod/__init__.py:1365-1377` "Do NOT drop this gzip" (U53 measurement).
- `providers/runpod/__init__.py:936-940` `_pod_ids_matching` deliberately not
  built on `find_instance_by_tag`.
- `providers/skypilot/__init__.py:259-263` `PreLaunchRateCapExceeded` stays in
  the provider — editing `core/errors.py` rewrites 13 goldens.
- `providers/skypilot/__init__.py:185-198` `_SKY_CUDA_UNKNOWN = "9999"`;
  `:1589-1595` single outer try/except in `_selection_tags`.
- `providers/modal/_app.py:147-158` `max_containers=1` (stateful-pod split-brain).
- `validation/checks/custom_nodes.py:21-23` stricter pass codes (a GitHub 401
  means a bad ref).
- `stores/local_lock.py:129-133` lock sidecar never unlinked (inode race);
  `stores/_lease.py:41-44` + `stores/gcs/lock.py:45` per-backend
  `_blocking_none_message` keeps historical wording.

Engines (pod-side — duplication forced by the embed boundary):
- `wan_t2v_server.py:2702-2782` inline `/upload` beside `servers/_upload.py` —
  design `2026-10-04-text-command-design.md` §6.4 (migrating moves 16
  configs' `embed_files` + goldens for no behaviour change).
- `servers/_lora.py:307-310` `download_one` duplicating Wan's `_download_one`
  — "TEMPORARY and deliberate" until Wan migrates. NOTE: the copies have
  already drifted (H3 has per-ref dirs, Wan does not — audit bug B4).
- `LoraTarget` ×3 (core `LoraEntry`, `wan_t2v_server.py:915-987`,
  `_lora.py:429-488`) — forced by the pod boundary, locked by
  `tests/test_lora_schema_parity.py`; Wan's U60 reverse map deliberately
  absent from H3.
- `minimax_h3_server.py:12-17` sibling, not extension, of wan.
- `wan_t2v_server.py:166-182` `KINOFORGE_MODELS_DIR` literals instead of a
  `core.pod_paths` import (U66: a module-level `kinoforge.core` import killed
  RunPod pods; `test_pod_embed_closure` guards it).
- `upscalers/spandrel/_engine.py:83-98` inline `curl` instead of
  `_fetch_weights` — payload ceiling.
- `upscalers/seedvr2/` extras-gated stub until Phase 2 vendoring (its
  `_fetch_weights --dest` unused — fix with the vendoring).
- `engines/diffusers/__init__.py:722-737` `_wire_entry` omits `target` so
  older warm pods with `extra="forbid"` do not 422.
- `servers/text_server.py:36-40` 64 MB upload cap vs wan's 2048 MB, on purpose.
- `engines/diffusers/__init__.py:79` vs `engines/comfyui/__init__.py:89`
  `_DEFAULT_RUNPOD_IMAGE` — NOT the same decision (diffusers is coupled to
  the cu124 index).

Tests / tools / config:
- 12 always-raise "contract" live tests under `xfail` (e.g.
  `tests/live/test_modal_wan_t2v_1_3b.py:15-19`) — the file records the
  reproduction command and acceptance contract.
- `tests/providers/runpod/test_heartbeat.py:54-63` `_C33_XFAIL` strict=True
  pins the IDEAL write contract; `tests/stores/live/test_{s3,gcs}_live.py`
  strict=False xfails have documented structural causes.
- `tests/providers/test_skypilot_{vast_compat,sitecustomize}.py`
  `importorskip("vastai_sdk")` — only runs in `live-skypilot`; note no task
  or CI job runs them.
- `tests/validation/test_shipped_configs_capabilities.py:77-83` non-recursive
  glob (documented convention); `tests/test_live_gating_lockdown.py:18-20`
  text scan "coarse but cheap" (but see audit: no recursive coverage).
- `tests/providers/test_launch_payload_goldens.py:119`
  `pytest.raises(Exception)` over an empty `EXCLUDED_CONFIGS` — vacuous
  today; tighten when the first exclusion lands.
- `tools/_t11_probe.py`, `tools/_t11_tail.py` — kept per PROGRESS (Task-11 pair).
- `tools/cloud_perms_probe.py` — ALIVE again (`75f37b74`); its
  `_REQUIRED_AWS_ACTIONS` is the single source for `validate_scoped_policy.py`.
  Remove it from any purge list.
- `tools/bootstrap_kms.py:430-440` broad excepts — per-cloud independence.
- `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml:119`
  `smoke:` block ignored by `Config` (`extra=ignore`), read by
  `tests/smoke/live_wan21/*` (rationale `:110-118`).
- `pyproject.toml:11` `seedvr = []` placeholder extra — referenced by the
  `ExtrasNotInstalled` install hint.
- `pixi.toml:78-82` `typer`/`rich`/`stamina`/`vermin`/`httpx` unused in code —
  operator toolkit choice (`6f33dc24`).
- `docker/wan-comfyui/Dockerfile:17-20` ComfyUI/KJ/VHS/Kijai pins restated in
  `tools/build_image_wan_comfyui.sh` + the build workflow — low churn,
  Dockerfile ARGs canonical.
