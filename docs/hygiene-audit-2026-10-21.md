# Hygiene audit — 2026-10-21 (whole repo, WITH FIXES)

Scope: whole repo, mutation opted in. This pass re-ran the 2026-10-18 audit with a stronger
model: eight parallel read-only auditors (cli+pipeline+outputs / core ×2 / engines+upscalers /
providers+stores+validation+sources / tests ×2 / tools+docs+config), each briefed to treat every
prior row as a hypothesis, re-verify it against HEAD with `file:line` evidence, then do a fresh
file-by-file pass. The controller spot-verified every new P2 claim and the one prior ✔ claim that
turned out to be overstated (see "Corrections"). Everything rated FIX NOW, low risk and strictly
behaviour-preserving was then applied as 43 single-purpose commits (`b4366e32..d64eaacf`).

The eight auditors' full reports (prior-claim verification tables, every proposed edit including
the ones NOT applied, and the per-area NEEDS DISCUSSION detail) are preserved verbatim under
`docs/hygiene-audit-2026-10-21-auditors/` (`brief.md` is the shared instructions they were given).

Baseline at `f89eff2b` (code byte-identical to the 10-18 audit's `5f8a52a0`; tree clean):

- `pixi run pre-commit run --all-files` — green.
- `pixi run test` — 6838 passed, 182 skipped, 25 deselected, 6 xfailed, 0 failed (11 m 24 s).

Final gate at `d64eaacf`: see the bottom of this file.

---

## Corrections to the 2026-10-18 audit

Roughly 300 prior rows (bugs, FIX NOW, dead, redundant, NEEDS DISCUSSION, hygiene-notes keeps)
were re-verified. **No prior P1/P2 bug was refuted**; B2/B3/B4/B5/B6/B7/B8/B9/B10/B11/B12/B13/
B17/B18/B19/B20/B21 and Finding #0 all stand with exact lines. The rows below were wrong or
materially imprecise:

| prior row | verdict | what is actually true |
|---|---|---|
| dead re-import `cli/_main.py:238 import os` | WRONG | `_main.py` has no module-level `os`; a legitimate lazy import. Left alone. |
| "Source docstrings (not embedded → no golden moves)" lists `core/scale_target.py` | WRONG on the embed point | `scale_target.py` is embedded by 13 configs; its stale docstring moves goldens and is now in the embedded batch below. `core/frames.py` (the `_default_probe_run` dedup) is embedded too. |
| B18 cites `warm_reuse/integration.py:137-152` | PARTIAL | the bug is real but those lines are dead code (`try_warm_attach_with_swap` has no caller); the live path is `lora_apply.py` → `orchestrator.deploy_session`, and **no production code ever writes `status="degraded"`**, so the matcher's degraded-skip and `DEGRADED_REAP` are unreachable. |
| dead `tests/smoke/conftest.py:31-33` | PARTIAL | only `:31` (`config.option.log_cli`) was dead; `:32-33` (`log_cli_level`) are what enable the live WARNING log under `pixi run smoke-local` (verified in pytest 9.0.3 source and empirically). Fixed accordingly. |
| local re-imports `cli/_commands.py:522,536,619,662` | PARTIAL | `:522` was an alias of a module import; `:536/:619` were intra-function duplicates; `:662` is a different scope and legitimate. |
| `smoke-wan21-weekly.yml:13 issues: write` "describes a step that never existed" | PARTIAL | the permission IS needed — the `always()` sweep step files issues via `gh`; only the comment was wrong. |
| `engines.md:375 boot_timeout_s → boot_timeout` | PARTIAL | also the default: 900 s, not 1800 (1800 is Modal's fallback for 0). |
| README `smoke-wan22-live` "caps at $2.00/module" | PARTIAL | per-module caps are 2.00 / 1.50 / 4.00. |
| `docs/extending.md` templates "omit abstract methods" | PARTIAL | the ComputeProvider template was complete; GenerationEngine (3) and ArtifactStore (4) were not, and `ModelSource.resolve` had the wrong signature. |
| B6 `_BURNING_VERDICTS` omits STALL/RESTART_LOOP | understated | it also omits `DEGRADED_REAP`; its docstring says "all 8" of 16 verdicts. |
| B17 ephemeral STALL_REAP sample arithmetic | understated | **unreachable with the shipped defaults** (sweeper 60 s, heartbeat None→30, window 600: needs 20 samples, holds 10) and with `examples/configs/sweeper.yaml` itself. |
| ND #4 `Lifecycle` defaults disagree | understated | **reachable**: `Config.lifecycle()` returns the all-disabled `interfaces.Lifecycle()` when no `lifecycle:` block exists, nothing requires the block, and `extras/runpod-diffusers-seedvr2-3b-upscale.yaml` ships without one (stall/restart/orphan OFF, `budget_usd=0.0`); any block flips all three ON. |
| counts | imprecise | loras swallow ×6 (not ×2); floorless discovery guards 9 sites / 8 files (2 pass vacuously from another cwd, not 3); 15 modules top-level-import `wan_t2v_server` (not 5; still only one binds names); 16 c30 teardown sites (not 10); 85-88 `tests/live` modules (not ~48); `test_field_consumption_parity.py` had three dead probe constants (not two). |
| hygiene-notes keep "wan inline `/upload` beside `_upload.py` … for no behaviour change" | keep reason now false | the copies have diverged in behaviour (NB3 below). Reworded in `docs/hygiene-notes.md`. |
| hygiene-notes keep "Duplicated provision branch" | trigger inverted, keep retained | the two blocks are identical apart from one comment line and the unread `for_discovery`; documented as such (D-doc-8) and left for the operator (one 20-line helper + parameter removal). |

Several SHAs cited in prior notes do not resolve in this repo: `67627cd0` (runpod docstring; the B4
fix is `cfcd552c`), `acdc8e1` (cli comment and `PROGRESS.md:10418`), `5418c35` / `8438a8b`
(CLAUDE.md; the commits that did the work are `90404cc5` / `70cd3992`), and `e82b0d1`
(hygiene-notes 07-16 section, memory). The in-repo pointers were corrected; PROGRESS.md and the
07-16 notes are left as history.

---

## New bugs (not fixed — recorded for the operator; ✔ = controller re-verified)

### P2 — money / correctness

| # | location | defect |
|---|---|---|
| N1 ✔ | `providers/runpod/__init__.py` `_request` (`:240-262`) | retries the SAME request on HTTP 502/503/504 for **mutations** too — `podFindAndDeployOnDemand` goes through the same closure. A 504 after RunPod booked the pod re-sends the create → a second, untracked pod under the same name bills until selfterm, and a by-name destroy then raises "ambiguous". Introduced `64c3fde0`, which reasoned only about the poll loop. |
| N2 ✔ | `providers/runpod/__init__.py` `_budget_bounded_lifetime` + `selfterm.py` `effective_deadline` | U51's `budget / rate × 3600` lifetime still has `time_buffer` (default 1800 s) subtracted on the pod: `budget: 0.25` on a $0.40/hr A40 kills the pod 450 s into boot, and any budget below `rate × 0.5 h` dies on the first tick. The doctor text and `docs/lifecycle.md` never mention the buffer; the tests pin it only with `time_buffer=0`. |
| N3 ✔ | `core/animation.py` `assemble_animation` + `core/upscale_dir.py` | every upscaled frame is held in controller RAM for EVERY container — Pillow 12.3's GIF/APNG writers `im_frames.append` and WebP `list()`s the generator (verified in site-packages). The plan-time caps (300 frames, 256 MP) do not bound it: a 120-frame 1080p GIF at 4× needs ~12-16 GB after the GPU spend is paid, then fails. The module docstring and `docs/configuration.md:194-195` claim only APNG materialises. |
| N4 ✔ | `core/grid/executor.py` `_cold_boot_swap_cell` (`rc == 0 and record_path.exists()`) + `cli/_commands.py` | a swap-group cell-1 that boots a pod and then fails generation leaks the pod: the provision record is written only on a successful return, swap cells never pass `--no-reuse`, so the group's `finally` never learns the pod id. The "no pod leak" / "destroyed in a finally block regardless of outcome" docstrings are false. |
| N5 | `engines/diffusers/__init__.py:288,305,322`, `engines/comfyui/__init__.py` (×4), `engines/hosted` (×3), both `fal` helper pairs | every controller→pod HTTP seam calls `urlopen` with no timeout (no `setdefaulttimeout` anywhere). A proxy TCP black-hole hangs `/status` polling AND `wait_for_ready` inside `urlopen`, where the wall clock, cancel token and boot-liveness probe cannot fire; the pod bills to selfterm. `_pod_http.py` already carries 60 s / 600 s constants. Same class as prior B8/B9/B21. |

### P3 — narrow / latent

- `core/reaper_actor.py` `sweep()` ephemeral path: `registry_get_provider` never returns `None` (it raises `UnknownAdapter`), so an index row naming an unregistered provider aborts the whole sweep instead of being isolated as the docstring promises.
- `core/orchestrator.py` + `core/session_claim.py`: the `provision:<id>` lease TTL (`boot_timeout + 2·hb`) is shorter than the ready-poll + `engine.provision` hold; and if the heartbeat closure raises after the claim is installed, a SUCCESSFUL run ends with a ~16-minute `FirstTickTimeout` that nothing catches (the docstring says the orchestrator does).
- `core/reaper_actor.py:338-343` and `cli/_commands.py` `_cmd_destroy`: production passes the test-only `sleep=lambda _: None` to `destroy_confirmed` (3 confirm polls back-to-back; masked on RunPod by the provider's own loop).
- `providers/runpod/__init__.py` `destroy_instance` is documented idempotent on a gone pod, but live `podTerminate` returns a `POD_NOT_FOUND` errors body (PROGRESS.md) which `_unwrap_graphql_response` raises; two tests fake the opposite shape.
- `stores/s3/lock.py:100-103`: only `PreconditionFailed` counts as contention; S3's `409 ConditionalRequestConflict` on a racing conditional write escapes `acquire()` raw (cannot verify offline).
- `validation/checks/models.py:101-124`: when every HEAD raises, the check passes with "N ref(s) probed; all reachable" (user-facing text).
- `cli/_commands.py` `_stamp_cold_created_instance` → `_ephemeral_index_add`: the post-run index write is unguarded, so a store fault after a completed generation becomes a traceback + exit 1 without `generated: uri=`. (The CLI auditor's first scenario — the `on_instance_created` hook raising mid-boot — is **wrong**: the hook reaches the orchestrator through `_record_then_install`, which wraps it in try/except and logs "the launch continues".) Also the gate is `EphemeralSession.current()`, which is true for every CLI dispatch, so every non-ephemeral cold create writes an index row.
- `cli/_commands.py` `_render_cost_json`: `cached_age_s` is hardcoded `0` (spec defines it as the cache age); `_cmd_sweeper_start`: `--interval-s 0` is falsy and silently falls back to the cfg value instead of exiting 2.
- `core/downloader.py:290-304` stdlib branch: a complete-but-unpromoted `.part` can never self-heal (416 on every retry), and a server that ignores `Range` gets its full body APPENDED to the partial file (promoted when no sha256 is declared). aria2c path is immune.
- `engines/diffusers/servers/wan_t2v_server.py` (embedded): `_ensure_on_gpu` / `_enforce_headroom` load/evict multi-GB models synchronously on the event loop under the registry lock (the `/health`-hang → proxy-502 class; masked live by client retries); the inline `/upload` lacks the all-dots `X-Filename` guard and runs `os.replace` outside the `try` — the two defects `_upload.py` fixed in `e6041bdd`; `_LOADED` is mutated under two different locks (`_REGISTRY_LOCK` vs `_REGISTRY_TLOCK`).
- `engines/comfyui/__init__.py:1351-1355`: the rendered `[ ! -d … ] && git clone … && cd custom_nodes/X && git checkout REF && cd ../..` is not fail-fast under `set -e`; a stale ref leaves the cwd in `custom_nodes/X`, every model download lands there, and ComfyUI boots "healthy" with no models after the full download was paid for.
- `engines/hosted/__init__.py:624-628` pings `health_url` unconditionally while the schema defaults it to `""` → `ValueError("unknown url type: ''")` reported as "hosted endpoint unreachable".
- `tests/_smoke_harness/runpod_lifecycle.py:106-110` `tag_filter`: the inverse of Finding #0 — with a filter set it destroys nothing and reports clean (no caller passes it today).
- `tests/core/test_image_interfaces.py` leaked a stub into the global image-engine registry, so `tests/cli/test_cmd_image.py::test_image_table_covers_every_registered_image_engine` failed whenever `tests/core` ran first. **Fixed here** (`55bc043a`, test-only, RED→GREEN shown in the commit).

Specifically checked and found clean by the auditors (so the next pass need not re-check): every
`None`-vs-`0` lifecycle threshold and verdict-table row in `classify()`; `_enforce_rate_cap`'s
readback matrix; lease/steal/TTL arithmetic in S3/GCS/local locks; RunPod create/terminate/list body
keys; every module-level `kinoforge.*` import in the pod servers against every config's embed set;
all 28 swallowing `except`s in `servers/`; `image_dir` temp-file handling on every failure path;
`credential_patterns` lookaheads and `redaction` ordering; every `except Exception` and
assert-less test in `tests/`.

---

## Fixed in this pass (43 commits, 121 files, +640 / −904)

Every commit is one logical change, behaviour-preserving by the hygiene definition (no change to
return values, exit codes, side effects, log text or help text), with the touched tests run before
the commit and the full gate run at the end. No embedded pod file was edited, so no launch golden
or payload baseline moved.

**Dead code (13 commits).** No-op `try/except` around the session-claim yield; unreachable
`VALID_KIND_TARGETS["lora"]`; three local `registry` re-imports in the orchestrator and eleven
local re-imports in the CLI that shadowed module-level names; two unreachable CLI fallbacks; a
recomputed `ext` in `GenerateClipStage`; `_NO_RESIDUAL_RE`; the empty `TYPE_CHECKING` stub in
`lora.py`; `image_discover` / `image_resolve_or_discover` (unused since birth); the
`_PASS_CODES` / `_default_http_head` seam aliases; `except (UnknownAdapter, Exception)`; the
always-true SkyPilot `if resources:`; the `_CREDENTIAL_PATTERNS` alias in `tools/_redact.py`;
24 `noqa` directives ruff reports as unused (the `F401` side-effect-import idiom, the embedded
servers' `E402`s and the tools under purge discussion were deliberately left — see hygiene-notes).

**True DRY consolidations (9 commits).** `_PASSTHROUGH_SUFFIXES` → `media.IMAGE_SUFFIXES`;
`grace_s` default → `_BOOT_GRACE_MIN_S`; one `hf_resolve_url` for three spellings; one RunPod
`_list_pods()` for three identical reads; one Modal `_APP_PREFIX`; one `_lease_expired` for the S3
and GCS steal rule; `resolve_prompt` hoisted to module level in four engines (the "avoid circular"
comments were false); five hand-built `GridCellResult(...)` through `_cell_result`; one
`_persist_cell_stderr`; one `_upscale_one`; and the pod upload now stream-hashes instead of reading
up to 2 GB into RAM (same digest, same headers).

**Test suite (6 commits).** Twelve unreferenced helpers/constants and a re-export nothing imports;
six dead `try/except: pass` wrappers around the call under test, an `assert … or True`, and the
no-op `log_cli` option; three discovery roots anchored to the test file (two guards passed
vacuously from any other cwd — proven, and proven fixed); the registry-leak isolation fix; the
subsumed `tests/integration/conftest.py`; stale comments and skip reasons.

**Documentation (15 commits).** ~140 corrections: stale docstrings and comments across
orchestrator, reaper, cost, config, lifecycle, offers, pool, grid, auth, redaction,
remote_backend, cli, outputs, pipeline, `_adapters.py` (section headers had drifted off their
imports), providers (serverless presented as working, SkyPilot budget arithmetic, heartbeat
mechanism, unresolvable SHA), modal/local (`reqs`→`placement`), validation, stores, sources,
diagnostics, engines (gzip embed lines, payload sizes, `exec`/PID 1, "Task 12/17", hosted
"e.g. fal.ai"); README (wrong clone URL, cheatsheet invocations that exit 2 as written, task
costs, hosted.yaml description, a new **Project structure** section); docs/*.md (`../` shell
paths, phantom `--init-image`, `engine.type`, config counts, defaults, five broken anchors,
adapter templates, the rejected `auth:` block, the release checklist); AGENTS.md and CLAUDE.md
factual claims (where the patterns live, `pixi run --` behaviour, `sweeper start -c`, two
unresolvable SHAs); pixi.toml / workflow / tools / example-config comments (BudgetTracker has no
production caller; `budget` shortens the selfterm cap at create).

---

## Deliberately left (LEAVE)

Beyond the standing keeps in `docs/hygiene-notes.md` (all re-verified; three reworded), this pass
left: the `# noqa: F401` self-registration idiom and the pod servers' `# noqa: E402`;
`SequentialPool` (public reference impl, zero production callers); `secret.py` (public-looking
type, zero users); the three private sha256 helpers (1 MiB chunks vs `downloader`'s 8 KiB — a
perf-only consolidation the operator should choose); `batch.py`'s `run_id or str(idx)` fallback
(dead-by-contract for CLI manifests, kept for hand-built ones); `interpolate._run_local`'s
decimate-before-publish-check order; `pipeline/keyframe.py` `image_profile` (constructor field);
every item whose fix changes a log line, help string or exception type.

---

## NEEDS DISCUSSION (operator decisions), ordered by money at risk

1. **Finding #0 leak sweep** — choose the age source (`probe_runtime().container_uptime_s` exists per pod; `list_instances` cannot carry it without widening `_LIST_PODS_QUERY`; Modal/SkyPilot listings are 0.0 too), decide whether the cron should exist, add the RED test built from the real `_pod_to_instance` output, and decide `tag_filter`'s fate (RunPod never sends `compute.tags`).
2. **N1 retry-on-mutation** — split the RunPod transport into a retrying read path and a non-retrying mutation path, or list-by-name and adopt before re-sending a create. Needs a live check.
3. **N2 budget vs `time_buffer`** — hand selfterm `budget/rate·3600 + time_buffer`, or document the real ceiling; add a test with the default buffer either way.
4. **B2 / B13 / N4 / capacity-wait: one guarded-launch helper** — `deploy()` now differs from the generate path in four ways (rate cap, diagnostic env, capacity-wait, real-row timing) and `_cmd_provision` is a hand copy; N4's swap-group leak closes if the provision record is written from `on_instance_created`.
5. **B3 / B4 (silent LoRA)** — client must honour `swap_rejected.reason == "set_adapters_value_error"`; Wan needs per-ref LoRA dirs like `_lora.py`. Embedded → goldens.
6. **B5 ComfyUI `status=error` not terminal** and the NB4 non-fail-fast `git checkout` line — both change rendered/polled behaviour; `test_comfyui_render_provision.py:110` pins the string.
7. **Timeouts** — B8 (RunPod transport), B9 (artifact GET), B21 (sources), N5 (controller→pod seams): one policy, reuse `_pod_http`'s 60 s / 600 s; a stalled proxy then raises `TimeoutError` (already in the proxy retry's catch set) instead of hanging.
8. **B6 / B7 verdict tables** — add STALL/RESTART_LOOP/DEGRADED to `_BURNING_VERDICTS`; add RESTART_LOOP/DEGRADED to `act_on_verdict`'s destroy set or drop them from `DEFAULT_APPLY_POLICY`; also the three `act_on_verdict` arms no reachable policy can hit.
9. **B17 denominator** — pass the sweeper interval as the ephemeral denominator or bank samples on the heartbeat cadence; today ephemeral STALL_REAP cannot fire out of the box.
10. **ND #4 `Lifecycle` defaults** — make `interfaces.Lifecycle` match `LifecycleConfig`, or require `lifecycle:` on compute cfgs; add the missing parity test.
11. **Dead lifecycle layer** (`LifecycleManager`, `BudgetTracker`, `warm_reuse_or_create`, `reap`, `effective_deadline`; ~470 LOC + 8 tests) and `try_warm_attach_with_swap` (sole writer of `degraded`); `resolve_or_discover` (wire it as `docs/cloud-stores.md` promises, or delete and fix the doc); `PathCell`, `GridBudgetExceeded` (B16), `PLACEHOLDER_MARKERS`, `core/secret.py`.
12. **Embedded-file batch** (one golden regeneration): `scale_target.py` docstring, `frames.py` `_default_probe_run` dedup, `errors.py` never-raised classes, the wan server items (ND #8 + `_ensure_on_gpu` to_thread, `/upload` back-port or migration, `localhost:8000` literal ×2, `_migrate_legacy_target_refs` shim, route list, B18's missing rollback boundary), flashvsr `_fetch_weights` pointer, `_av_io`/`_video_io` encoder profile.
13. **CLI copy-drift helpers** — `Lifecycle→classify` kwargs ×5, launch-row stamp ×4, attach ladder ×4 + refusal ×3, `kinoforge_key` width ×7, `is_launching` spellings, sweeper row id ×7, proxy host, `_cmd_destroy` branches (already diverged on `TeardownError`), `_cmd_cost` cfg-None fallback.
14. **User-facing text drift** — `_main.py` help (`civarchive` "resolver pending", `--force-forget` "implies --apply", 5-of-6 record keys, "replicate spend tracking pending B10"), `_rc_to_reason` label, `RunPodCapacityHintCheck` / `_skypilot_disk` / models "all reachable" messages, `build_heartbeat_endpoint_for` "unknown provider", `prepare_upload` "frame 0 only" for PSD/ICO, `--diagnostic-mode` help on `deploy`.
15. **Hosted engines** — fal/hosted/bedrock bypass `RemoteSubmitPollBackend` (`del cancel_token` ×6; `_delete` hooks nothing calls); image `_delete` raises `NotImplementedError` and drops the paid artifact; hosted `health_url` default; `_HostedEngineConfig` `auth:` branch unreachable from YAML.
16. **`pipeline/decimate.py` ≡ `downscale.py`** (the exit-183 fix must change both) and the five `file://` strippers → one `local_path_of(artifact)` beside `media.local_artifact`; `interpolate._default_count` raw `CalledProcessError`.
17. **Tests** — floors for the nine floorless discovery sweeps (recommend half of today's counts), recursive live-gating lockdown with a conftest-aware signal, `test_live/conftest.py` `.env` load gated on `KINOFORGE_LIVE_TESTS`, the faulthandler timer (fires 15 s into every healthy run), except→skip in the example-config guard, `_Transport`'s `<unmatched>` sentinel, the Modal list fake's `name` key vs production's `description`, S4 fake ×10 and wan snapshot fixture ×4 consolidation, ComfyUI `status_str: "error"` coverage (B5's hiding place), `xfail_strict`.
18. **Tools / deps / CI** — purge list refined (`c33_j_probe`, `c33_k_probe`, `debug_ledger_watcher` have zero references anywhere; the rest with their collateral in the tools-docs report); `lumaai` / `fal-client` imported nowhere, `httpx` declared twice, `google-cloud-billing` has no importer; CI pin drift (`checkout@v4` / `setup-pixi@v0.8.1` vs `ci.yml`); `.claude/hooks/summarize-and-exit.sh` unregistered scaffold; `pixi.toml` "generated template" header; `docs/windows-migration-cancelled.md` needs a status line; `docs/cloud-stores.md:100-102` wording; leak-sweep narrative in `docs/warm-reuse.md:306-308`.
19. **Smaller code decisions** — `_provision_compute_once(store=)` unused parameter; interpolate materialize block vs `fetch_artifact_bytes` (only the INFO line differs); `ttl_s=30.0` vs `reaper_actor._LOCK_TTL_S`; `balance_endpoints.TransportError` vs `errors.TransportError`; `_LeaseLockBase` relocation into `core/locks.py` (the real blocker is a cycle, not layering); `fal_key` pattern shape (needs one online check); redaction of `exc_text`/non-str args; `batch.py` summary-on-`__enter__`-failure docstring vs code; animation memory remedy (N3) and its docs.

---

## Final gate

Recorded by the controller after the last commit; see the PROGRESS.md session entry for the
numbers.
