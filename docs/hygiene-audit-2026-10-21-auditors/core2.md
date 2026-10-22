# Hygiene re-audit 2026-10-21 — core part 2 (read-only)

Area: `src/kinoforge/core/` — batch*, grid/, upscale_dir, image_dir, image_run, image_stack,
animation, media, frames, fps_resolver, scale_resolver, scale_target, splitter, lora*,
warm_reuse/, profiles, downloader, remote_backend, auth, vault, secret, credentials,
credential_patterns, redaction, dotenv_loader, assets, artifacts, text_request,
prompt_routing, pod_paths. HEAD `f89eff2b`, tree clean. Every file in the area was read in
full; every prior row was re-verified against HEAD line numbers.

**Embedded files in this area (any byte change moves goldens):** `scale_target.py`
(13 example configs: every `*-flashvsr-upscale.yaml` / `*-spandrel-upscale.yaml`, RunPod +
SkyPilot), `frames.py` + `fps_resolver.py` (3 RIFE configs: `modal-diffusers-rife-60fps-
interpolate*.yaml`, `runpod-diffusers-rife-60fps-interpolate.yaml`), `pod_paths.py`
(imported by the RunPod/Modal providers for pod env; the servers carry literals, see
`test_pod_embed_closure.py:497-504`). All other files in the area: `embedded: no`.

---

### A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line, ≤2 lines) |
|---|---|---|---|
| B14 | `_run_swap_group` dispatched without the semaphore | CONFIRMED | `grid/executor.py:1118` `sem = asyncio.Semaphore(max_parallel_groups)` is passed only to `_run_group` (`:1147-1152`, `async with sem:` `:929`); the `_run_swap_group(...)` call at `:1129-1142` takes no `sem`. |
| B15 | cell-1 cold-boot failure + CONTINUE → cell 2 `RuntimeError` escapes `run_grid`, no `.cost.json` | CONFIRMED | `:789 return _cell_result(cell, "failed", error=failure), False` then `:757-758 if pod.pod_id is None: raise RuntimeError("swap-group invariant…")`; `sidecar.write` `:1159` sits after `gather` `:1156`, no handler. CONTINUE is returned for `policy="continue"` on any non-137 rc (`swap_failures.py:98-99`) and for recoverable patterns under `classify` (`:103-104`). |
| B16 | `"budget"` status never produced → cap-trip exits 2 not 3 | CONFIRMED | `:56` literal; `rg 'status="budget"' src/` → only the exit map `cli/_commands.py:5773`; `budget_killed` cells fall into `any(r.status != "success")` → `status="partial"` `:1195-1203`. |
| B18 (warm_reuse half) | pod's `rollback_failed`/`lora_swap_failed` 500 → bare `RuntimeError`; warm-reuse releases without `degraded` | PARTIAL | Mechanism real: the ONLY writer of `status="degraded"` is `warm_reuse/integration.py:142`, inside `try_warm_attach_with_swap`, which has zero production callers. The live path is `lora_apply.py:338` (bare `set_lora_stack`) → `orchestrator.py:2256` inside `deploy_session`; on raise the pod is kept unless `single` (`:2310`) and nothing marks it; `matcher.py:286` skips only `status == "degraded"`. So the bug stands, but the cited lines are dead code. |
| P3 `remote_backend.py:52,58-71` | `_urllib_get_bytes`/`_urllib_delete` have no timeout | CONFIRMED | `:54 urllib.request.urlopen(url)` and `:68 urlopen(req)` — no `timeout=`; docstring `:62` claims "timeout/UA policy is one decision here". |
| P3 `batch.py:609-632` vs `:893-916` | 0-outcome `_batch_summary.json` IS written when `deploy_session.__enter__` raises | CONFIRMED | `with deploy_session(` `:664` is inside the `try:` whose `finally:` `:893` writes `store.put_json(batch_id, "_batch_summary.json", redacted)` `:909` unconditionally (only the strict-ephemeral gate `:898-902` skips). Placeholder `summary` `:628-633` has `outcomes=[]`. Module docstring `:7` agrees with the code; the function docstring `:609-616` + comment `:623-627` do not. |
| P3 `grid/executor.py:898`, `:685-686` | blocking residual probe in async `finally`; hardcoded wall-times | CONFIRMED | `:898 clean, raw = _check_no_residual_pods()` called directly (no `to_thread`) in `async def _run_swap_group`'s `finally`; the probe is 6×`subprocess.run` + `time.sleep` (`:979-1010`). `:685-686 gen_wall_time_s=0.0, swap_wall_time_s=0.0`. |
| FIX NOW `remote_backend.py:58-71,~247` | "timeout/UA policy is one decision here" (sets neither); "Fal subclasses this backend" (it does not) | CONFIRMED | `:62`; `:252 (Replicate / Runway / Luma / Fal all subclass this`. `rg 'class \w+\(RemoteSubmitPollBackend'` → `engines/runway`, `engines/replicate`, `image_engines/replicate`, `image_engines/luma_agents` — no fal. Also `:480` "(Replicate, Runway, Luma)" is stale for *engines* (only Runway/Replicate subclass `RemoteSubmitPollEngine`). |
| FIX NOW `grid/executor.py:3-11,1066-1069,1136-1138`, `grouping.py:4-6` | "REFUSED when ephemeral" (no raise exists); "EVERY cell passes --no-reuse" (swap cells never do) | CONFIRMED | Swap cells: `:475 Never --no-reuse`, `:482 no_reuse=False`. No raise after `swap_groups_present` (`:1106-1117`). `_run_swap_group` docstring `:820-822` records the 2026-09-11 lift that `:1066-1069` and `:1136-1138` still deny. `grouping.py:4-6` "rides the existing matcher's warm-reuse" contradicts `_run_group:916-926`. |
| FIX NOW `scale_target.py:3-6,27` | height targets "refused by every v1 consumer" (UpscaleStage resolves them) | CONFIRMED, **but misfiled** | `pipeline/upscale.py:5,61` "``kind="height"`` is resolved here"; only `upscalers/seedvr2/_runtime.py:62-63` still raises. The prior audit listed this under "Source docstrings (not embedded → no golden moves)" — **WRONG on the embed point**: `scale_target.py` is embedded by 13 configs (`rg -l 'kinoforge.core.scale_target' examples/configs` = 13). Any edit regenerates launch goldens → moved to §E. |
| FIX NOW `lora_apply.py:402` | "The ONLY except in this module" (a second at `:158`) | CONFIRMED | `:158 except Exception:  # noqa: BLE001 — a bookkeeping read must not fail a run`; `:401-402` comment. |
| FIX NOW `grid/executor.py:281` stale `file:line` pointer | `cli/_main.py:413` is not `--prompt` | CONFIRMED | `:281` "(``cli/_main.py:413``)"; `_main.py:413` is docstring prose about SUPPRESS. |
| dead `profiles.py:676-749` | `image_discover` / `image_resolve_or_discover` zero refs | CONFIRMED | `rg -n 'image_discover|image_resolve_or_discover' --type py --type md` → only the two `def`s. Fence: `92b6b16a feat(core): JsonImageProfileCache with .image.json namespace (Phase 32 T5)` — body gives no reason; `image_stack.py:77-79` and `orchestrator.py:2128` call `resolve`/`discover` directly. |
| dead `grid/executor.py:57` `_NO_RESIDUAL_RE` | zero refs since `6b6feb8c` | CONFIRMED | only the definition `:57-61`; `6b6feb8c fix(grid): residual-pod check ignores sweeper bookkeeping rows` replaced its use with `_is_residual_pod_clean` `:961-976`. `re` stays used by `_SLUG_RE` `:62`. |
| redundant `batch.py:461-923` | `entry_finish`+`BatchOutcome` pair ×5, `run_id or str(idx)` ×13, fn 463 lines | PARTIAL | Shape confirmed, counts off: `kind="entry_finish"` ×6 (`:374,401,774,823,855,874`), `BatchOutcome(` ×7, `entry.run_id or str(idx)` ×10. `load_manifest:142-144` guarantees `run_id` is set, so the `or str(idx)` fallback is dead-by-contract for CLI input (kept for hand-built manifests). |
| redundant `upscale_dir.py:201-207` vs `:389-397` | seed-state→stage.run→fetch ×2 | CONFIRMED | `:202-207` and `:390-397` — identical 6-line block, one with injected `fetch`, one with `_orch.fetch_artifact_bytes`. |
| redundant `lora_apply.py:364-410` ≡ `warm_reuse/integration.py:126-135` | ledger inventory write duplicated | CONFIRMED (textually) | `lora_apply.py:390-400` ≡ `integration.py:126-135`; but the integration copy lives in dead `try_warm_attach_with_swap` and lacks the try/except, and `lora_apply.py:48` imports `_entry_to_dict` FROM integration.py. Resolution belongs with ND #6 (delete the dead wrapper, move `_entry_to_dict`). |
| redundant `image_dir.py:390` `_PASSTHROUGH_SUFFIXES` restates `media.IMAGE_SUFFIXES`; `register_openers()` ×4 | | CONFIRMED / PARTIAL | `media.py:26` is the same three suffixes and `media.py:62-63` literally states the passthrough rule ("Anything outside IMAGE_SUFFIXES is re-encoded to PNG") → one decision, two places → §D. `register_openers()` at `:208,424,489,513` is one idempotent call per public entry point, not a decision duplicated → LEAVE. |
| redundant `grid/executor.py:386-394` ≡ `:524-531`; 5 `GridCellResult(` bypass `_cell_result` | | CONFIRMED | stderr-persist `:386-394` and `:525-531` (both `except OSError: pass`); bypasses at `:403,:422,:432,:935,:1167`, all on `_ResolvedCell` inputs. No test references `_cell_result` (`rg _cell_result tests/` = 0). |
| redundant private sha256 helpers ×3 vs `downloader.sha256_file` | | CONFIRMED, LEAVE | `executor.py:348`, `image_dir.py:394` (`_sha8`, 1 MiB chunks), `media.py:121-124` vs `downloader.py:67` (8 KiB `_CHUNK`). Same digest, different chunk size; `tests/core/test_image_dir_prepare.py:137` pins `_sha8` naming; replacing with 8 KiB reads is a perf regression on multi-GB mp4s → §E. |
| redundant `frames.py:133-170` inlines `_default_probe_run` | | CONFIRMED, but EMBEDDED | `:146-168` duplicates `:173-191` byte-for-byte plus the float parse. `frames.py` is embedded (3 RIFE configs) and `tests/core/test_frames.py:341-428` pins `_default_probe_duration`'s messages → §E (golden batch). `pipeline/chunk.py:25` imports the private `_default_probe_run`. |
| ND #6 `warm_reuse/integration.py:43-152` `try_warm_attach_with_swap` never adopted | | CONFIRMED | 0 src callers (`rg try_warm_attach_with_swap src/` → definition only); 7 test files use it; `a1892981` records the deferral ("deploy_session integration is a follow-up", `:14-19`). The matcher itself IS live via `cli/_commands.py:741` (`--dry-run-swap`). |
| ND #6 `profiles.py:445-523` `resolve_or_discover`/`_discover_single_flight` test-only vs `docs/cloud-stores.md:100` | | CONFIRMED | src callers of `.discover(`: `orchestrator.py:2128`, `image_stack.py:79`; `.resolve_or_discover(` only in `tests/core/test_profiles.py`. `docs/cloud-stores.md:98-102` says production profile discovery "use[s] the lock automatically" — false today. |
| ND #6 `grid/spec.py:70` `PathCell` | | CONFIRMED | `GridCell.path: Path \| None` `:119` never uses it; refs only `tests/core/test_grid_spec.py:15,54`. Fence `78e8ad18 feat(grid): GridSpec pydantic models (schema only)`. |
| ND #6 `grid/errors.py:37` `GridBudgetExceeded` | never raised | CONFIRMED | `rg GridBudgetExceeded src/` → definition only; `tests/core/test_grid_errors.py:12,25` import it. Tied to B16. |
| ND #6 `credential_patterns.py:394` `PLACEHOLDER_MARKERS` | | CONFIRMED | `rg --hidden PLACEHOLDER_MARKERS` (excl. docs/PROGRESS) → definition only; not mirrored in `.claude/hooks`. Comment `:393` "exported for callers/tests that want the full set" — none exist. |
| keep `lora_apply.py:141-159` `_engine_lora_support` degrades to SERVER_HTTP (U56) | | CONFIRMED | `:158` returns `LoraSupport.SERVER_HTTP`; reason intact (`:269-273`). |
| keep `batch.py:10-12,88-95` re-exports "for six test-module import sites" | | PARTIAL | 6 test files import from `core.batch`, but only 2 import a re-exported dataclass (`tests/cli/test_main_flow.py:607` `BatchResult`; `tests/core/test_batch_summary_skipped.py:23` `BatchEntry, BatchManifest`); src imports `batch_models` directly (`cli/_commands.py:64`, `cli/batch_formatters.py:25`). Keep stands; count is wrong. |
| keep `image_dir.py:37-42` `_ANIMATED_SUFFIXES` vs `animation.py:44-50` `_WRITERS` parity test from `1f9460e7` | | CONFIRMED | `tests/core/test_animation.py:317` `assert set(animation._WRITERS) == image_dir._ANIMATED_SUFFIXES` with a `>= 5` floor at `:320`. |

---

### B. New bugs (missed by the prior audit)

**B-1 (P2, borderline P1 — wastes an item's whole GPU spend) — `animation.py:132-179` + `upscale_dir.py:153-239`: every upscaled frame of an animation is held in controller RAM before encoding, for EVERY container, and the plan-time guards do not bound it.**
`assemble_animation` hands a generator to Pillow, but the installed Pillow 12.3.0 materialises it: GIF `GifImagePlugin.py:766 im_frames.append(...)`, APNG `PngImagePlugin.py:1249 im_frames.append(...)`, WebP `WebPImagePlugin.py:161 append_images = list(...)`. The module docstring (`animation.py:17-19`, "this is the one container whose frames are materialised in memory") and `docs/configuration.md:194-195` ("every other container streams one frame at a time") are wrong; the `peak[0] == 1` test (`tests/core/test_animation.py:305-308`) only counts open file handles via the `_open_frame` seam, not resident pixels.
Concrete scenario: `kinoforge upscale --image-dir` on a 120-frame 1920×1080 animated GIF with `scale: 4x` (defaults `max_animation_frames=300`, `max_output_megapixels=256`, both satisfied: 120 ≤ 300, 33 MP ≤ 256 MP). All 120 frames are upscaled on the pod (paid), written to scratch, then `assemble_animation` holds 120 × 7680×4320 RGB(A) frames ≈ 12-16 GB on the controller → `MemoryError` → caught by `upscale_dir.py:414` → item recorded `failed` after full spend. Worst case under defaults is 300 × 256 MP × 3-4 B ≈ 230-300 GB. Introduced with the feature (`92263378..1f9460e7`, merged `53523d1d`).

**B-2 (P2 — money leak until the sweeper) — `grid/executor.py:588-640` `_cold_boot_swap_cell` + `:884-903` `_run_swap_group` finally: a swap-group cold-boot cell whose generation fails AFTER the pod booted leaves the pod alive and the grid never learns its id.**
`_cold_boot_swap_cell` publishes `pod.pod_id` only when `rc == 0 and record_path.exists()` (`:626`), and the CLI writes `--emit-provision-record` only after a *successful* return (`cli/_commands.py:1070-1084`, inside `if returned_instance is not None and instance is None and not single`). Swap cells never pass `--no-reuse` (`:482`), and a non-`ValidationError` failure inside the subprocess keeps the pod (`orchestrator.py:2256-2310` — `deploy_session` destroys only `if single`; `generate()` destroys only on `ValidationError`, `:3028-3037`). So `pod.pod_id` stays `None`, the `finally` at `:884-886` skips `kinoforge destroy`, and the group docstring's "destroyed in a finally block regardless of outcome" (`:816-817`) and `_SwapPodHandle`'s "no pod leak" (`:576-581`) are false.
Concrete scenario: `lora_swap` group, cell-1 boots pod P, `ensure_lora_stack` raises `LoraSwapDownloadError` for a bad ref (`lora_apply.py:338` → `orchestrator.py:2256`), subprocess exits 1 with no record; under `classify` the stderr matches no pattern → ABORT; remaining cells `aborted`; `finally` finds `pod_id is None`; P idles at its hourly rate until selfterm/sweeper. `run_grid`'s residual probe (`:1178`) then reports `status="teardown"` (exit 5) but destroys nothing. Introducing commit: `c00b28e9 feat(grid): _run_swap_group executor + run_grid dispatch` (the `rc == 0` gate survived the `3c011326` decomposition).

**B-3 (P3, narrow) — `downloader.py:290-304` stdlib branch: a leftover `.part` can never self-heal.**
(a) A complete-but-unpromoted `.part` (process killed during `sha256_file(part_path)` `:310` on a multi-GB file) makes the next run send `Range: bytes=<size>-` → server answers 416 → `urlopen` raises `HTTPError` → `KinoforgeError("HTTP request failed…")` `:115-116`; nothing unlinks the `.part`, so every retry fails identically until the operator deletes it by hand. (b) A server that ignores `Range` and returns 200 with the full body is accepted (`:118`) and APPENDED to the partial file (`:299-301`); with `artifact.sha256 is None` the corrupt file is promoted `:321`. The aria2c path is immune (`:275` pre-unlinks). Only reachable with no `aria2c` on PATH and a prior interrupted stdlib download.

Checked and found clean (no claim): `image_dir.py` temp handling (`prepare_frames` writes one frame per `next()`, consumer unlinks; a partial write of frame *i* stays under the run's `TemporaryDirectory`), `upscale_dir._write_atomic` (`.part` + `os.replace` in the destination dir, stray `.part` drained in the animated `finally` `:237-239`), `gen.close()` on every exit, loop-count preservation (`loop=None` → no Netscape block for GIF; WebP/APNG fall back to 0 — matches Pillow 12.3's `encoderinfo.get("loop", …)` semantics), per-frame durations (lists accepted by all three writers), alpha (`_has_alpha` on the source frame, `L` mask resized Lanczos, GIF index-255 transparency), frame cap wiring (`config.py:859` default 300 == `plan_image_dir` default == docs), `_FATAL` propagation, `redaction.redact` longest-first, `credential_patterns` `_ASSIGNMENT_RE` lookaheads, `iter_findings` line/col arithmetic, `PodLockRegistry` zombie reap, `EphemeralIndex` locked RMW, `fps_resolver` / `scale_resolver` arithmetic, `AWSSigV4.apply` canonicalisation.

---

### C. New hygiene findings

| # | location | category | behaviour-preserving? | risk | call | rationale |
|---|---|---|---|---|---|---|
| C-1 | `animation.py:17-25` module docstring; `animation.py:105-109`; `docs/configuration.md:194-195` | documentation | yes (docs only) | low | NEEDS DISCUSSION | Claims only APNG materialises frames; false for GIF/WebP under Pillow 12.3 (B-1). Reword only once the B-1 remedy is chosen so the doc describes the final behaviour. |
| C-2 | `grid/executor.py:576-581` (`_SwapPodHandle`), `:816-819` (`_run_swap_group`) | documentation | yes | low | NEEDS DISCUSSION | "no pod leak" / "destroyed in a finally block regardless of outcome" are false in the B-2 scenario; fix text with the bug, not before. |
| C-3 | `grid/cost_sidecar.py:4-5` "written unconditionally at grid exit (success, partial, or abort)" | documentation | yes | low | LEAVE (tie to B15) | `sidecar.write` `:1159` is skipped when `gather` raises (B15). |
| C-4 | `redaction.py:3-6` "The vault loader is the only writer" | documentation | yes | low | FIX NOW | Writers today: vault loader, `grid/spec.py:250-269`, `lora.py:270-272` (`--loras`), `warm_reuse/redaction.py:58`, `downloader.py:249`. |
| C-5 | `grid/spec.py:3-5` "The loader (``GridSpec.load``) ships in Task 4; this initial cut is models-only" | documentation | yes | low | FIX NOW | `GridSpec.load` exists at `:151`. |
| C-6 | `auth.py:139-141` ("future Replicate / Runway / Luma integrations"), `:272-274` ("Used by VeoEngine (Layer 2)"), `:390` ("Used by NovaReelEngine (Layer 3)") | documentation | yes | low | FIX NOW | `Bearer(` is used by `engines/{runway,replicate}`, `image_engines/{replicate,luma_agents}`, `engines/hosted`; no `VeoEngine`/`NovaReelEngine` class exists (`rg '^class '` → `BedrockVideoEngine`); `GCPServiceAccount(` has no src constructor call. |
| C-7 | `upscale_dir.py:1` "Upscale every planned still" | documentation | yes | low | FIX NOW | The module now upscales animations too (`:384-387`). |
| C-8 | `lora.py:18,25-26` `if TYPE_CHECKING:\n    pass` | dead | yes | low | FIX NOW | Vestigial since `d86e9a5c`; the only `TYPE_CHECKING` use in the module. |
| C-9 | `core/secret.py` (whole module, 56 LOC) | dead | yes (delete) | med | NEEDS DISCUSSION | `rg 'core\.secret|Secret\(' src/` → no users; only `tests/core/test_secret.py`. Fence `101dc3d9 chore: state-management refactor + write-site invariants`; docstring says it is "carried inside narrow boundary code" — nothing carries it. Public-looking type → operator call. |
| C-10 | `credential_patterns.py:173` `fal_key` = `\bfal_key_[A-Za-z0-9_\-]{8,}\b` | documentation / dead pattern | n/a | — | NEEDS DISCUSSION (CANNOT-VERIFY-OFFLINE) | fal.ai keys are documented as `<key_id>:<key_secret>` (uuid:hex) with no `fal_key_` prefix; if so the strict pattern can never match a real key and only `credential_assignment` (`FAL_KEY=…`) protects. Needs one online check of fal's key format before changing anything. |
| C-11 | `redaction.py:1-6` + `cli/_main.py:175-230` | error-handling / documentation | n/a | — | NEEDS DISCUSSION | Neither the `RedactingLogFilter` nor the record-factory redactor touches `exc_info`/`exc_text` or non-`str` `args` (e.g. an `Exception` object passed to `%s`), so "substituted on every persistent surface (logs…)" overstates. No concrete leaking call site found in this area (the two `%s`-with-exception sites, `lora_apply.py:410-416` and `upscale_dir.py:420`, carry pod ids/paths, not registered tokens); flagged as a gap, not a bug. |
| C-12 | `image_dir.py:434-439` `prepare_upload` multi-frame branch | documentation (user-facing log text) | no (log line) | low | NEEDS DISCUSSION | Since animated suffixes route to `prepare_frames`, this branch now fires only for PSD (layers) / ICO (sizes); the INFO "N frames; upscaling frame 0 only" is misleading there. |
| C-13 | `docs/configuration.md:168-172` "writes lossless PNGs… keep their full name plus `.png` (`a.webp.png`…)" | documentation | yes | low | FIX NOW (docs, small) | True for stills only; an animated input keeps its own container (`image_dir.py:273-275`) and WebP is lossy q90 (`animation.py:38`). The paragraph at `:190-198` corrects it; one qualifying clause at `:169` closes the contradiction. |
| C-14 | `remote_backend.py:208-209` "1s/2s/4s exponential backoff" | documentation | yes | low | LEAVE | With `retries=3` only the 1 s and 2 s sleeps occur (`:218-219` sleeps only when another attempt follows); harmless imprecision. |
| C-15 | `batch_events.py:35-36`, `batch.py` `entry.run_id or str(idx)` ×10 | redundant / dead-by-contract | yes | low | LEAVE | `load_manifest:142-144` always sets `run_id`; the fallback only serves hand-built manifests. Not worth touching a 463-line function for. |
| C-16 | `grid/executor.py:1123-1127` `raise RuntimeError("internal: swap group detected but sidecar was not pre-allocated")` | dead (unreachable) | yes | low | LEAVE | Defensive; `sidecar` is always built when `swap_groups_present` (`:1110-1117`). |
| C-17 | `downloader.py:109-111` stdlib fetch buffers the entire body (`resp.read()`) before writing | structural | n/a | — | LEAVE | Design limitation (aria2c is the fast path); worth a docstring line, not a change. |
| C-18 | `profiles.py:11-15` module docstring "``resolve_or_discover`` implements a per-key single-flight guarantee" | documentation | yes | low | LEAVE (fold into ND #6) | True of the method; misleading because production never calls it (A table, ND #6 row). |
| C-19 | `warm_reuse/integration.py:1-19` + `lora_apply.py:48` | dead / structural | — | med | NEEDS DISCUSSION | Deleting the dead wrapper (ND #6) requires relocating `_entry_to_dict` into `lora_apply.py` first; `tests/core/test_warm_reuse_integration.py` + 6 other test files go with it. |

---

### D. FIX NOW edit list

All items: `embedded: no` unless stated. `pre-commit run --all-files` after each group.

#### D-docs — documentation / docstring fixes (code untouched)

**D1. `src/kinoforge/core/remote_backend.py:58-63`** — `_urllib_delete` docstring. Tests: `tests/core/test_remote_backend.py`. `rg -c 'timeout/UA policy' src tests docs` = 1 (this line).
```
old:
    Shared default for every ``RemoteSubmitPollBackend`` ``http_delete``
    seam (replicate, runway) — timeout/UA policy is one decision here.
new:
    Shared default for every ``RemoteSubmitPollBackend`` ``http_delete``
    seam (replicate, runway). Sets no timeout and no User-Agent today; a
    future timeout/UA policy belongs here so both seams change together.
```

**D2. `src/kinoforge/core/remote_backend.py:251-253` and `:480`** — subclass lists. `rg -c 'Fal all subclass' src tests docs` = 1.
```
old (:251-253):
        This closes the Ctrl-C path for every Bearer-API hosted provider
        in one shot (Replicate / Runway / Luma / Fal all subclass this
        backend).
new:
        This closes the Ctrl-C path for every Bearer-API hosted provider
        built on this base (the Replicate / Runway video backends and the
        Replicate / Luma image backends subclass it; fal and the hosted
        shim hand-roll their own poll loops).
old (:480):
        """Remote-submit-poll engines (Replicate, Runway, Luma) read ``spec.model``.
new:
        """Remote-submit-poll engines (Replicate, Runway) read ``spec.model``.
```

**D3. `src/kinoforge/core/grid/executor.py:3-7`** — module docstring. Tests: `tests/core/test_grid_executor.py`, `tests/core/test_grid_executor_swap_group.py`. `rg -c 'EVERY cell passes' src tests docs` = 1.
```
old:
Each cell launches ``pixi run kinoforge generate`` as a subprocess. Same-
group cells run sequentially; EVERY cell passes ``--no-reuse`` (cold-boot,
generate, destroy) — see ``_run_group``'s docstring for the 2026-06-25
matcher-race rationale that retired the reuse-cells-0..N-2 design this
docstring used to describe.
new:
Each cell launches ``pixi run kinoforge generate`` as a subprocess. Same-
group cells run sequentially; every ``generate:`` cell passes ``--no-reuse``
(cold-boot, generate, destroy) — see ``_run_group``'s docstring for the
2026-06-25 matcher-race rationale that retired the reuse-cells-0..N-2
design this docstring used to describe. ``lora_swap:`` cells are the
exception: one pod per group, cold-booted by cell-1, attached by cells
2..N and destroyed by ``_run_swap_group``'s ``finally`` (never
``--no-reuse``).
```

**D4. `src/kinoforge/core/grid/executor.py:1066-1069`** — `run_grid` docstring. `rg -c 'REFUSED when' src tests docs` = 1.
```
old:
            still wouldn't collide. Lora-swap cells are REFUSED when
            ``ephemeral`` is true (see the raise right after
            ``swap_groups_present`` is computed) rather than silently
            defaulted to non-ephemeral — filed as U24 in PROGRESS.md.
new:
            still wouldn't collide. Lora-swap cells accept ``ephemeral``
            too since 2026-09-11 (U24) — see ``_run_swap_group``'s
            docstring for why the earlier refusal was lifted.
```

**D5. `src/kinoforge/core/grid/executor.py:1133-1138`** — comment in the swap dispatch.
```
old:
                        # work_parent, not output_dir: per-cell dirs belong to
                        # the working tree, which is private + removed under an
                        # ephemeral policy and identical to output_dir
                        # otherwise. (Unreachable while ephemeral, per U24's
                        # refusal — kept aligned so a future lift of that
                        # refusal does not silently re-open U25.)
new:
                        # work_parent, not output_dir: per-cell dirs belong to
                        # the working tree, which is private + removed under an
                        # ephemeral policy and identical to output_dir
                        # otherwise (U25). Reachable under --ephemeral since
                        # the U24 refusal was lifted on 2026-09-11.
```

**D6. `src/kinoforge/core/grid/executor.py:281`** — stale pointer.
```
old:
    (``cli/_main.py:413``). Read the prompt from the effective cfg's
new:
    (``cli/_main.py``'s ``generate`` subparser). Read the prompt from the effective cfg's
```

**D7. `src/kinoforge/core/grid/grouping.py:3-6`** — module docstring. Tests: `tests/core/test_grid_grouping.py`, `tests/core/test_grid_grouping_lora_swap.py`. `rg -c "existing matcher's warm-reuse" src tests` = 1 (plus a historical plan doc).
```
old:
A group is a set of cells that can share one warm pod (same
``CapabilityKey``). Sequential execution within a group rides the
existing matcher's warm-reuse; groups run in parallel up to the
caller's concurrency cap.
new:
A group is a set of cells that share one grouping key — ``CapabilityKey``
for ``generate:`` cells, ``WarmAttachKey`` for ``lora_swap:`` cells.
``generate:`` groups run their cells sequentially, each under
``--no-reuse``; ``lora_swap:`` groups share one warm pod. Groups run in
parallel up to the caller's concurrency cap.
```

**D8. `src/kinoforge/core/lora_apply.py:402-403`** — comment. Tests: `tests/core/test_lora_apply.py`, `tests/engines/diffusers/servers/test_lora_apply_core.py`. `rg -c 'ONLY ``except``' src tests docs` = 1.
```
old:
        # The ONLY ``except`` in this module, and deliberately not in the
        # apply path: by the time it can fire the pod has already
new:
        # One of two ``except``s in this module (the other guards the
        # ``_engine_lora_support`` bookkeeping read) and deliberately not
        # in the apply path: by the time it can fire the pod has already
```

**D9. `src/kinoforge/core/redaction.py:3-6`** — module docstring (C-4). Tests: `tests/core/test_redaction.py`. `rg -c 'vault loader is the only writer' src tests` = 1.
```
old:
The registry is the single source of truth for tokens that must be
substituted on every persistent surface (logs, JSON files, stdout, error
blocks). The vault loader is the only writer; sinks (Ledger, profile cache,
batch summary, OutputSink.publish, _save_fixture, etc.) are readers.
new:
The registry is the single source of truth for tokens that must be
substituted on every persistent surface (logs, JSON files, stdout, error
blocks). Writers: the vault loader, the grid-spec loader (title, captions,
swap-stack refs), ``--loras`` parsing, observed pod inventories
(``warm_reuse/redaction.py``) and opaque LoRA downloads. Sinks (Ledger,
profile cache, batch summary, OutputSink.publish, _save_fixture, etc.) are
readers.
```

**D10. `src/kinoforge/core/grid/spec.py:3-5`** (C-5). Tests: `tests/core/test_grid_spec.py`. `rg -c 'ships in Task 4' src tests` = 1.
```
old:
This module owns the spec-file surface. The loader (``GridSpec.load``)
ships in Task 4; this initial cut is models-only so downstream tests
have a parseable schema target.
new:
This module owns the spec-file surface: the pydantic models and the
outside-repo loader (``GridSpec.load``).
```

**D11. `src/kinoforge/core/auth.py:139-141, 272-274, 390`** (C-6). Tests: `tests/core/test_auth*.py` (via `rg -l 'core.auth' tests/core`). `rg -c 'future Replicate / Runway' src tests docs` = 1.
```
old (:139-141):
    Used by the existing :class:`HostedAPIEngine` for fal.ai today; future
    Replicate / Runway / Luma integrations reuse this class with different
    ``env_var`` values.
new:
    Used by :class:`HostedAPIEngine`, the Replicate / Runway video engines
    and the Replicate / Luma image engines, each with its own ``env_var``.
old (:272-274):
    Used by VeoEngine (Layer 2) and any future Vertex AI integrations
    (Imagen, Lyria, Gemini-Vision). The ``google.auth`` SDK is lazy-imported
    inside method bodies to preserve the core-import-ban invariant.
new:
    Registered as ``gcp_service_account``; no shipped engine constructs it
    today (reserved for Vertex AI integrations). The ``google.auth`` SDK is
    lazy-imported inside method bodies to preserve the core-import-ban
    invariant.
old (:390):
    Used by NovaReelEngine (Layer 3) and any future Bedrock integrations.
new:
    Used by :class:`BedrockVideoEngine` and any future Bedrock integrations.
```

**D12. `src/kinoforge/core/upscale_dir.py:1`** (C-7). Tests: `tests/core/test_upscale_dir.py`.
```
old:
"""Upscale every planned still in a directory on ONE pod.
new:
"""Upscale every planned image (still or animated) in a directory on ONE pod.
```

**D13. `docs/configuration.md:178-180`** (prior audit row, confirmed). `rg -c 'contribute their first frame' docs README.md src tests` = 1 live hit (+1 in a historical plan under `docs/superpowers/plans/`, leave it).
```
old:
on the controller to an 8-bit RGB PNG with EXIF orientation applied
(animated GIFs and multi-page TIFFs contribute their first frame).
new:
on the controller to an 8-bit RGB PNG with EXIF orientation applied
(animated inputs take the frame-wise path described below).
```

**D14. `docs/configuration.md:168-169`** (C-13).
```
old:
under `photos/` (recursive) on ONE pod and writes lossless PNGs into a sibling
new:
under `photos/` (recursive) on ONE pod and writes lossless PNGs (stills; animations keep their own container, see below) into a sibling
```

#### D-code — dead code and TRUE-DRY consolidations

**D15. Delete `_NO_RESIDUAL_RE` — `src/kinoforge/core/grid/executor.py:57-61`.** Fence: `6b6feb8c fix(grid): residual-pod check ignores sweeper bookkeeping rows` (replaced by `_is_residual_pod_clean`). `rg -n '_NO_RESIDUAL_RE' --type py` = 1 hit (the definition). Tests: `tests/core/test_grid_executor.py`.
```
old:
_NO_RESIDUAL_RE = re.compile(
    r"\[instance overview\] No running instances\."
    r"|No instances recorded in ledger\.",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
new:
_SLUG_RE = re.compile(r"[^a-z0-9]+")
```

**D16. Delete `image_discover` + `image_resolve_or_discover` — `src/kinoforge/core/profiles.py:676-749` (file tail).** Fence: `92b6b16a feat(core): JsonImageProfileCache with .image.json namespace (Phase 32 T5)` — no reason stated; zero refs repo-wide (`rg -n 'image_discover|image_resolve_or_discover' --type py --type md -g '!docs/hygiene-audit*'` = 2 hits, both the defs). Tests: `tests/core/test_profiles.py`, `tests/core/test_profile_cache_redaction.py`. Edit: delete from the line `    def image_discover(` (`:676`) through end of file (`:749`); keep the trailing newline after `verify`'s closing `)` at `:674`. `threading`, `ImageEngine`, `ImageBackend` imports remain used.

**D17. Remove the vestigial `TYPE_CHECKING` stub — `src/kinoforge/core/lora.py:18,25-26`** (C-8). Fence: present since `d86e9a5c feat(p1): resolve_active_lora_stack + …`. Tests: `tests/core/test_lora*.py`.
```
old (:18):
from typing import TYPE_CHECKING, Any, Literal
new:
from typing import Any, Literal
old (:25-26, delete both lines and one of the surrounding blank lines):
if TYPE_CHECKING:
    pass
```

**D18. `_PASSTHROUGH_SUFFIXES` → alias of `media.IMAGE_SUFFIXES` — `src/kinoforge/core/image_dir.py:23,390`.** Identical frozenset; `media.py:62-63` already documents this as the same rule. `rg -n '_PASSTHROUGH_SUFFIXES' tests/` = 0. Tests: `tests/core/test_image_dir_prepare.py`, `tests/core/test_image_dir_plan.py`, `tests/cli/test_cmd_upscale_image_dir.py`.
```
old (:23):
from kinoforge.core.media import DIR_IMAGE_SUFFIXES
new:
from kinoforge.core.media import DIR_IMAGE_SUFFIXES, IMAGE_SUFFIXES
old (:390):
_PASSTHROUGH_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
new:
_PASSTHROUGH_SUFFIXES = IMAGE_SUFFIXES
"""The pod accepts PNG/JPEG byte-for-byte — the same rule as ``--image``."""
```

**D19. Route the 5 hand-built `GridCellResult(...)` through `_cell_result` — `src/kinoforge/core/grid/executor.py:403-410, 422-429, 432-438, 935-943, 1167-1174`.** Same dataclass, same field values (`error=None` is the factory default). `rg -n '_cell_result' tests/` = 0 (nothing monkeypatches it). Tests: `tests/core/test_grid_executor.py`, `tests/core/test_grid_executor_swap_group.py`, `tests/integration/test_grid_end_to_end.py`.
```
:403-410  old: return GridCellResult(idx=cell.idx, caption=cell.caption, status="failed", mp4_path=None, sha256=None, cost_usd=None, error=err,)
          new: return _cell_result(cell, "failed", error=err)
:422-429  old: return GridCellResult(idx=cell.idx, caption=cell.caption, status="failed", mp4_path=None, sha256=None, cost_usd=None, error=err,)
          new: return _cell_result(cell, "failed", error=err)
:432-438  old: return GridCellResult(idx=cell.idx, caption=cell.caption, status="success", mp4_path=mp4, sha256=_sha256_file(mp4), cost_usd=None,)
          new: return _cell_result(cell, "success", mp4_path=mp4, sha256=_sha256_file(mp4))
:935-943  old: results.append(GridCellResult(idx=cell.idx, caption=cell.caption, status="aborted", mp4_path=None, sha256=None, cost_usd=None,))
          new: results.append(_cell_result(cell, "aborted"))
:1167-1174 old: all_results.append(GridCellResult(idx=cell.idx, caption=cell.caption, status="success", mp4_path=cell.mp4_path, sha256=_sha256_file(cell.mp4_path), cost_usd=0.0,))
          new: all_results.append(_cell_result(cell, "success", mp4_path=cell.mp4_path, sha256=_sha256_file(cell.mp4_path), cost_usd=0.0))
```
(The multi-line originals are at the cited lines verbatim; `_cell_result` is defined at `:535`, below `_run_one_cell`, which is fine at call time.)

**D20. Share the stderr-persist block — `src/kinoforge/core/grid/executor.py:386-394` and `:525-531`.** Add once (above `_run_one_cell`, `:355`):
```python
def _persist_cell_stderr(cell: _ResolvedCell, proc: subprocess.CompletedProcess[str]) -> None:
    """Write the subprocess's FULL stderr next to the cell's tmp cfg (best effort)."""
    if cell.cfg_path is None:
        return
    stderr_log = cell.cfg_path.with_suffix(".stderr.txt")
    try:
        stderr_log.write_text(  # kinoforge:public-write
            proc.stderr or proc.stdout or "<no output>",
        )
    except OSError:
        pass
```
then replace `:386-394` (`if cell.cfg_path is not None:` … `except OSError:\n                pass`) with `        _persist_cell_stderr(cell, proc)` (keep the explanatory comment `:379-385`), and `:524-531` (`if proc.returncode != 0 and cell.cfg_path is not None:` … `pass`) with `    if proc.returncode != 0:\n        _persist_cell_stderr(cell, proc)`. The `# kinoforge:public-write` tag moves with the write (`tests/test_no_unredacted_writes.py` must stay green). Same tests as D19.

**D21. Share the seed→run→fetch block — `src/kinoforge/core/upscale_dir.py:202-207` and `:390-397`.** Add once (above `_upscale_animated`, `:152`):
```python
def _upscale_one(stage: UpscaleStage, path: Path, fetch: Callable[[Artifact], bytes]) -> bytes:
    """Run *stage* on the local image at *path* and return the upscaled bytes."""
    state = PipelineState(
        request=GenerationRequest(prompt="", mode="upscale"),
        artifacts={"clip": local_artifact(path, "image")},
    )
    state = stage.run(state)
    return fetch(state.artifacts["upscaled"])
```
replace `:202-207` with `                body = _upscale_one(stage, frame.path, fetch)` and `:390-397` with `                            body = _upscale_one(stage, upload, _orch.fetch_artifact_bytes)`. `UpscaleStage`/`Artifact` are already `TYPE_CHECKING` imports (`from __future__ import annotations` is on). Tests: `tests/core/test_upscale_dir.py`, `tests/cli/test_cmd_upscale_image_dir.py`.

---

### E. Needs discussion

1. **B-1 animation memory.** Options: (a) encode frame-by-frame through ffmpeg (`-f image2pipe` → gif/webp/apng muxers) instead of Pillow `save_all`; (b) bound `frames × output_megapixels` at plan time (a second `oversize` rule) and document the ceiling; (c) accept and document. Any of these changes behaviour; the docs (C-1) should follow the choice.
2. **B-2 swap-group pod leak.** Fix sits across two areas: either `cli/_commands.py` writes `--emit-provision-record` from `on_instance_created` (so the record exists the moment the pod does, success or not), or `_cold_boot_swap_cell` reads the record whenever it exists regardless of `rc`. Both change the record's meaning ("pod exists" vs "cell succeeded"); the grid's RETRY path then also needs to decide between re-cold-boot and attach.
3. **`batch.py` summary on `__enter__` failure.** Code writes a 0-outcome `_batch_summary.json`; the function docstring `:609-616` and comment `:623-627` say it does not; the module docstring `:7` says "every exit path". Pick one: make the docstring match (behaviour-preserving, trivial) or add `if outcomes_by_idx or summary.outcomes:` before the write (behaviour change; a test would be needed — `tests/core/test_batch_summary_skipped.py` tests redaction only, despite its name).
4. **`scale_target.py:3-6,27` docstring (embedded).** Confirmed stale, but every byte moves 13 configs' launch goldens + payload-ceiling baselines. Batch it with the other embedded-file docstring fixes (ND #8's `wan_t2v_server.py` list, `errors.py` never-raised classes) and the `frames.py:133-170` dedup (also embedded) in ONE golden regeneration.
5. **ND #6 deletions, now with the dependency graph:** `try_warm_attach_with_swap` (move `_entry_to_dict` to `lora_apply.py` first; 7 test files); `resolve_or_discover` + `_discover_single_flight` (either wire into `orchestrator.py:2128` / `image_stack.py:79` — which is what `docs/cloud-stores.md:98-102` promises — or delete and fix that doc); `PathCell`; `GridBudgetExceeded` (keep if B16 is fixed by raising it); `PLACEHOLDER_MARKERS` (zero refs incl. hooks — safe to delete, but it is a declared export of a stdlib-only module); `core/secret.py` (C-9, zero users).
6. **sha256 helper consolidation** (`executor.py:348`, `image_dir.py:394`, `media.py:121-124` → `downloader.sha256_file`): same digest, but `downloader._CHUNK` is 8 KiB vs 1 MiB; either bump `_CHUNK` (perf-only change) or leave three private copies. Recommend bumping `_CHUNK` to `1 << 20` and consolidating in one step.
7. **Redaction coverage of tracebacks** (C-11): decide whether `exc_text`/non-str args should be redacted (a `Formatter.formatException` override or `str()`-ing args in the factory) or whether the docstring should state the limit.
8. **`fal_key` pattern** (C-10): one online check of fal's current key format; if it is `uuid:hex`, add a `fal_key`-named pattern for that shape (per the `luma_key` precedent comment) rather than loosening.
9. **`image_dir.prepare_upload` log text for PSD/ICO** (C-12): "N frames; upscaling frame 0 only" → operator-facing wording change.
10. **B18 live path**: whether `deploy_session` should `Ledger.touch(status="degraded")` (or destroy) when `ensure_lora_stack` raises a pod-state-unknown error; today no production code ever writes `degraded`, so the matcher's degraded-skip and `DEGRADED_REAP` are unreachable.

---

### F. Cross-area notes

- `cli/_commands.py:1070-1084` writes `--emit-provision-record` only after a successful `_generate` return — the root of B-2; a hook on `on_instance_created` would close it.
- `orchestrator.py:2256-2310`: an `ensure_lora_stack` raise before `yield` keeps the pod warm (non-single) with `session_end` stamped and no `degraded` mark — B18's live path.
- `cli/_commands.py:5773` `"budget": 3` is an unreachable exit-map entry (B16).
- `docs/cloud-stores.md:98-102` claims production profile discovery goes through `resolve_or_discover`'s cross-process lock; production calls `.discover()` directly (`orchestrator.py:2128`, `image_stack.py:79`).
- `pipeline/chunk.py:25` imports the private `frames._default_probe_run` — any frames.py refactor must keep that name.
- `tests/core/test_batch_summary_skipped.py` never tests a skipped summary (all four tests assert the file is written/redacted); misleading name.
- `tests/core/test_animation.py:305-308` `peak[0] == 1` measures open file handles, not resident frames — it cannot catch B-1; a guard would need to count live `Image` objects or bound `len(materialised)`.
- Prior audit's "Source docstrings (not embedded → no golden moves)" list contains an embedded file (`scale_target.py`); the controller should re-check the other areas' entries in that list against `rg -n '<basename>' examples/configs` before applying any of them.
- `.env.example:447` documents the `luma-api-` key prefix, so `credential_patterns.py:246-262`'s "corroborated by .env.example" is accurate (checked, no finding).
