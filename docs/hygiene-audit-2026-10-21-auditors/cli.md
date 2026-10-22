# Hygiene re-audit 2026-10-21 — area: `cli/`, `pipeline/`, `outputs/`, `interpolators/`, package top level

HEAD `f89eff2b`, working tree clean, read-only. Every file in the area was read in full
(`cli/_commands.py` 5779 lines, `cli/_main.py` 1410, `cli/_reconcile.py` 755, all helpers,
all of `pipeline/`, `outputs/`, `interpolators/`, `__init__.py`, `__main__.py`, `_adapters.py`).
Line numbers below are at HEAD (byte-identical to the prior audit's `5f8a52a0`).

Embed status for this area (from `rg -n 'kinoforge\.(cli|pipeline|outputs)' examples/configs/
tests/providers/test_pod_embed_closure.py` → 0 hits): nothing under `cli/`, `pipeline/`,
`outputs/` or the package top level is embedded. **`interpolators/rife/` IS embedded whole**
(`embed_modules: - "kinoforge.interpolators.rife"` in `runpod-diffusers-rife-60fps-interpolate.yaml:33`,
`modal-diffusers-rife-60fps-interpolate.yaml:37`, `modal-diffusers-rife-60fps-interpolate-long.yaml:46`),
so any byte change there moves launch goldens — no FIX NOW item below touches it.

---

## A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD |
|---|---|---|---|
| B9 | `artifact_bytes` GET has no timeout; non-local/non-http uri → fabricated bytes | CONFIRMED | `pipeline/artifact_bytes.py:81` `with urllib.request.urlopen(req) as resp:` (no `timeout=`); `:117-121` returns `filename.encode() + b"\|" + repr(sorted(meta))` whenever the uri is not an existing local path and `url` is not http(s). |
| B12 | `upscale --scale` ignored on the single `--video`/`--image` path while the guard/dry-run use it | CONFIRMED | `cli/_commands.py:1213` `del scale  # ScaleTarget recomputed inside UpscaleStage via cfg.upscale.scale`; `:1201` `_image_megapixel_error(source, cfg, scale)` and `:1186` dry-run print use the override; `--image-dir` passes `scale=scale` (`:1808`) so it is single-path only. `_cmd_interpolate:1305-1308` DOES apply its `--fps` via `cfg.model_copy(...)` — the asymmetry is real. |
| B13 (CLI comment half) | `:259-262` claims `deploy` merges the diagnostic overlay | CONFIRMED | `rg -n 'diagnostic_env\|restart_policy' src/kinoforge/core/orchestrator.py` → only `:273-299` (helper) and `:1486` (generate path); `restart_policy` has zero hits in orchestrator.py. |
| B10 (CLI line `:3006`) | pre-create index row keyed `kinoforge-<hex>` | CONFIRMED | `:3006` `row_id = session.resource_name(run_id, cfg.compute.provider)`. |
| B15/B16 (CLI halves) | `_cmd_grid` catches only `ValueError` (`:5764`); budget→exit 3 (`:5773`) | CONFIRMED | `:5764` `except ValueError as exc:`; `:5768-5774` `status_to_exit = {"full": 0, "partial": 2, "budget": 3, ...}`. |
| P3 `cli/_commands.py:603-604,636-655` | provision writes the real row before readiness; never forgets it on post-create failure; Modal row permanent | CONFIRMED | `:603-604` `_record_real_row` + `_collapse_provisional_row` precede `_wait_for_provider_ready` (`:624`); `:636-655` `provider.destroy_instance` with no `ledger.forget`; `_reconcile.py:79` `_RECONCILABLE_PROVIDERS = {"runpod", "skypilot"}`. |
| P3 `cli/_commands.py:4574` | destroy catches `(UnknownAdapter, KeyError)` not `TeardownError`; passes test-only `sleep=lambda _: None` | CONFIRMED (+ copy drift) | `:4574` `except (UnknownAdapter, KeyError) as exc:` vs the orphan copy `:4625` `except (UnknownAdapter, KeyError, TeardownError)` — the two branches of one function have already diverged. `core/lifecycle.py:924` "Pass ``sleep=lambda _: None`` in tests to skip real sleeps"; production passes it at `:4567` and `:4618` (retries default 3, `:929`). |
| P3 `cli/_commands.py:5161` | cost builds the balance endpoint from `cfg.compute.provider`, not the row's provider | CONFIRMED | `:5161` `endpoint = build_balance_endpoint_for(cfg, creds)` inside `for provider_kind in providers_in_ledger`; `_adapters.py:302` `provider = cfg.compute.provider`. |
| FIX NOW docstring `:1097-1104` | "v1 owns dry-run only" stale | CONFIRMED | `:1101-1103` "for v1 the handler only owns the dry-run path + early refusal gates" vs `:1216-1259` (attach ladder, launch row, orchestrator call). |
| FIX NOW `:1208-1210` | "mirrors `_cmd_generate`'s chain" stale | CONFIRMED | `:1210` vs `_resolve_warm_attach_chain` docstring `:778-784` "deliberately run a different, simpler chain ... do NOT route through this helper". |
| FIX NOW `:2398-2401` | "`_cmd_deploy` writes the mapping" stale | CONFIRMED (worse) | `:2400` cites sha `acdc8e1`; `git cat-file -t acdc8e1` → "Not a valid object name". `_cmd_deploy` (`:229-309`) no longer calls `ledger.record`. |
| FIX NOW `cli/context.py:50` | "(installed for generate / batch)" | CONFIRMED | `context.py:50` vs `_main.py:88-90` `_INTERRUPTIBLE_CMDS` = {generate, batch, upscale, interpolate, image, text}. |
| FIX NOW `cli/_reconcile.py:3-5` | "two callers" | CONFIRMED | `_reconcile.py:3-5`; third caller `_commands.py:480` (`_cmd_provision`). |
| FIX NOW `outputs/local.py:3` | old filename schema | CONFIRMED | `local.py:3` "``<dir>/<namespace>/<ts>_<slug><ext>``" vs `base.py:format_filename` "``{ts}_[{kind}_]{provider}_{model}_{slug}{extension}``". |
| FIX NOW `pipeline/generate_clip.py:6,75` | stale "now"/"NEW" | CONFIRMED | `:6` "``segments`` is now a constructor field"; `:75` `segments: list[Segment]  # NEW — always populated by orchestrator`. |
| dead `_cmd_cost:5099-5119` | `cfg is None` fallback unreachable; thresholds disagree with `Lifecycle()` | CONFIRMED | `_main.py:1138` `p_cost.add_argument("-c", "--config", required=True, ...)`; fallback `:5110-5119` idle 600 / max_lifetime 3600 / grace 300 vs `core/interfaces.py:136,139,152` 7200 / 18000 / 1800. `tests/cli/test_cmd_cost.py:24-34` always sets `ctx.cfg` (a MagicMock whose lifecycle happens to carry the fallback's numbers). |
| dead re-imports `:2160,3474,3630,3674,4258-4259`, `_main.py:192` | shadow module-level imports | CONFIRMED | same name bound at module level: `datetime` (`_commands.py:22`), `UnknownAdapter` (`:38`), `json` (`:13`), `urllib.request` (`:19`), `_main.py` `logging` (`:10`). |
| dead re-imports `:522,536,619,662` | shadow module-level imports | PARTIAL | `:522` `import dataclasses as _dc` duplicates module-level `import dataclasses` (`:12`) under an alias — redundant, not a shadow. `:536` (`EnvCredentialProvider as _ECP`), `:619` (`EnvCredentialProvider`, `_wait_for_provider_ready`, `provision`), `:662` (`EnvCredentialProvider`, `provision`) are NOT module-level imports (`rg -n '^from kinoforge.core.(credentials\|provisioner)' cli/_commands.py` → 0); `:536` and `:619` are intra-function duplicates of each other, `:662` is a different branch. |
| dead re-import `:1879` | | PARTIAL | `:1876-1881` imports `Cancelled, KinoforgeError, UnknownAdapter, ValidationError` — only `UnknownAdapter` is module-level; the other three are legitimately local. |
| dead re-import `_main.py:238` | | WRONG | `_main.py:238` `import os` — `_main.py` has no module-level `os` import (`rg -n '^import os' cli/_main.py` → 0). Legitimate lazy import. |
| dead getattr (`:729-732`) | `getattr(lc, "lora_swap_re_probe_after_s", 300.0)` default dead | CONFIRMED | `:731`; the field exists on `Lifecycle` (`core/interfaces.py:155`) and `Config.lifecycle()` populates it (`core/config.py:1996`). |
| dead `pipeline/generate_clip.py:177` | recomputes `ext` | CONFIRMED | `:166` and `:177` both `ext = Path(last.filename).suffix or ".bin"`; `last` unchanged between. |
| redundant launch-row stamp ×3 (`:2116-2125` exact, `:1071-1090` near) | | CONFIRMED (×4) | `_finish_launch_row` `:1491-1527`; `_cmd_text` `:2116-2125` byte-identical body; `_cmd_generate` `:1071-1090` same shape plus the `cfg_wak` return; `_cmd_interpolate` `:1355-1365` a fourth variant (index-add only — documented gap). |
| attach ladder ×4; `--no-reuse`/`--attach-pod` refusal ×3 | | CONFIRMED | identical `if attach_pod_id: ... elif not args.no_reuse:` blocks at `:1220-1237`, `:1325-1342`, `:1745-1756`, `:2064-2077`; identical refusal text + exit 2 at `:1108-1115`, `:1273-1280`, `:1987-1994` (a 4th wording with exit 1 in `_resolve_warm_attach_chain:803-809` is the documented keep). |
| `kinoforge_key` width ×7 (`:270,2776,3013,3296,3709`) | | CONFIRMED | all five `cfg.capability_key().derive()[:12]`; `orchestrator._key_hash` (`:336-345`) is `key.derive()[:12]`; `_cmd_provision:467` already uses `_key_hash`. |
| `is_launching` spellings `:2380` vs `:4063`; inline `:3179-3181` | | CONFIRMED | `:2380` `_is_launching(entry)` (`cli._reconcile`), `:4063` `is_launching(entry)` (`core.launch_phase`), `:3179-3181` `tags.get(LAUNCH_PHASE_TAG) == (LAUNCH_PHASE_LAUNCHING)`. |
| sweeper `f"sweeper:{host}"` ×7; `cfg.sweeper.host or socket.gethostname()` ×4 | | CONFIRMED | `:5452,5498,5525,5546,5571,5616,5695`; `:5402,5523,5614,5693`. |
| `:4479` hand-spelled RunPod proxy host | | CONFIRMED | `:4479` `f"https://{args.id}-{_LOG_SIDECAR_PORT}.proxy.runpod.net/{filename}"`; `_PROXY_URL_PATTERN` is provider-private (`providers/runpod/__init__.py:82`). |
| `pipeline/decimate.py:39-87` ≡ `downscale.py:18-76` | | CONFIRMED | `decimate.py:80-87` and `downscale.py:69-76` identical temp-file/`run`/`unlink` block; argv builders differ only in `-vf`. |
| "is this uri local" ×3 (`upscale.py:291-299,340-347`, `interpolate.py:96-102`) | | CONFIRMED (×5) | plus `pipeline/text.py:41-46` (`_local_path`, URL-refusing variant) and `interpolators/rife/_engine.py:141` (embedded). |
| ND #17 CLI error boundary + help/text drift | | CONFIRMED (all sub-claims) | upscale/interpolate: no `try` around `_orchestrator.generate` (`:1234-1249`, `:1328-1343`); generate catches only `UnknownAdapter` (`:1057`); `main()` dispatch `:1407` unguarded. Help: `_main.py:649` "civarchive … resolver is pending" while `sources/civarchive/__init__.py:257` registers `CivArchiveSource`; `:1101` `--force-forget` "(implies --apply)" vs `_commands.py:4697-4702` exit 4; `:701` lists 5 record keys vs `:3636-3643` writes 6; `_render_cost_human:5246-5247` prints "replicate spend tracking pending B10" whenever `threshold > 0` (default 4.50). `_rc_to_reason:2733-2745` docstring "rc=1 → ledger-absent" vs code `"cap-key-drift"` (pinned by `tests/cli/test_scan_warm_candidates.py:402`). |
| ND #18 `"http"` endpoint key `:2869-2871,3632-3634` | | CONFIRMED (+1 spelling) | both `instance.endpoints.get("http") or next(iter(instance.endpoints.values()), "")`; `_cmd_pod_lora_ls:4331` spells it `endpoints_map.get("8000") or next(...)` — a third spelling of "the pod server port" inside cli. |
| ND #19 `_cmd_deploy:268-278` | duplicate-key refusal lacks provision's reconcile + advice | CONFIRMED | `:266-276` raw ledger scan, no `_reconcile_dead_ledger_entries`; `_cmd_provision:468-487` reconciles first. |
| ND #3 Lifecycle→classify ×5 (cli: `:2590-2604,3750-3764,4755-4777,5099-5142`) | already drifted | CONFIRMED | `_classify_for_status` and `_resolve_warm_instance` pass 9 kwargs; `_cmd_reap` thresholds add `ephemeral_orphan_age_s`/`ephemeral_orphan_samples` (`:4770,4777`); `_cmd_cost` float-coerces four and omits the ephemeral pair; `classify` accepts all (`core/reaper.py:590-607`). |
| ND #5 (cli) `:2303` `except BudgetExceeded` dead in production | | CANNOT-VERIFY-OFFLINE (core claim) | cli half holds: `:2303` catches it; sole raiser is `core/lifecycle.py:1151` (`BudgetTracker`) — whether that class has production callers is core's row. |
| ND #7 long cli commands | | CONFIRMED | `_cmd_provision` 283 (`:391-673`), `_resolve_warm_instance` 207, `_cmd_batch` 209, `_cmd_text` 185, `_cmd_status` 176, `_cmd_upscale` 165. |
| keep `cli/_reconcile.py:18-24` launch-phase literals | parity guarded | CONFIRMED (keep stands; pointer stale) | `tests/core/test_reaper_launching_rows.py:189` `assert _reconcile._LAUNCHING_GRACE_S == LAUNCHING_GRACE_S`; but the comment says "imported from ``kinoforge.core.lifecycle``" while the constants live in `core/launch_phase.py:31-47` (`core/lifecycle.py` only mentions the tag in a docstring, `:685`). |
| keep `:778-784` simpler attach chain, exit 2 vs 1 | | CONFIRMED | `:772-784`; mutex exits `:1115` (2) vs `:803` (1). |
| keep `:3583-3596`; `:4135-4141`; `:329-332` | | CONFIRMED | `:3570-3604` "DELIBERATE DIVERGENCE"; `:4128-4141` `_merge_recorded_tags` only; `:335-343` broad `except` → `None`. |
| keep `:2995-3000` walrus | | CONFIRMED | `:3001-3005`. |
| keep `:137-139,1016-1019` `sys.modules["kinoforge.cli"]` reads | | CONFIRMED | `:136-138`, `:1016-1019`. |
| keep `_main.py:425-432` `_propagate_session_globals` | | CONFIRMED | `:462-485`; docstring names the matrix test. |
| keep `outputs/__init__.py:39-67`, `local.py:206-220` sink registry unused | | CONFIRMED | `rg -n 'get_sink\(' src/` → only `outputs/__init__.py` + a docstring mention `local.py:211`. |
| keep `cli/batch_formatters.py:48` `[idx+1/run_id]` | | CONFIRMED | `:48` `f"[{event.batch_id}] [{event.idx + 1}/{event.run_id}]"`. |
| hygiene-notes: `endpoints.get("http")` is NOT a keep | | CONFIRMED | see ND #18 row. |
| 07-16 roll-up: B6 `_INTERRUPTIBLE_CMDS` "covers all 7 cancel_token commands incl. text and `--image-dir`" | | PARTIAL | `_main.py:88-90` has 6 entries; `--image-dir` is a flag of `upscale`, so "7" double-counts — the set is complete. |
| 07-16 roll-up: `outputs/local.py` filename dedup (`b83af260`) | | CONFIRMED | `_resolve_collision:170-178` derives `base` from `format_filename`. |

---

## B. New bugs (missed by the prior audit)

**B-new-1 — P2 (money-adjacent).** `cli/_commands.py:3306-3358 _ephemeral_row_upgrade_hook` → `:3247-3303 _ephemeral_index_add`.
The `on_instance_created` hook the CLI hands the orchestrator can raise (`EphemeralIndex(store=ctx.store()).add(...)` at `:3290-3299` is unguarded), and the orchestrator calls it **unguarded** at `core/orchestrator.py:1554` — *outside* the `create_instance` try at `:1521-1549` — while its twin at `:1967-1976` wraps the same callback in `try/except` and logs "the launch continues". The pre-create write (`_ephemeral_launch_row_reserve:3015-3031`) is explicitly "Never raises: bookkeeping must not fail a run"; the post-create one is not.
*Scenario:* `kinoforge --ephemeral generate -c <cfg with store.kind: s3>` → `create_instance` returns a booked pod → the hook's S3 `put` hits a transient fault → the exception escapes `_provision_instance_and_build_backend` with the pod already billing. Whether core then destroys the boot (money burned, nothing produced) or leaks it is decided by the orchestrator's teardown path (core-2's area, not verified here) — the row that was supposed to name the pod is exactly the thing that failed.
*Second scenario (P3, non-ephemeral):* `_stamp_cold_created_instance:926-932` calls `_ephemeral_index_add` after every successful cold create because its gate is `EphemeralSession.current() is not None` (`:3289`), which `_ephemeral_strict_session`'s own docstring (`:2927-2945`) says is true for *every* CLI dispatch; an index fault there turns a completed run into a traceback + exit 1 without ever printing `generated: uri=`.
Introduced: index add + `current()` gate `71ecbe8c` ("ephemeral cold-create writes index row"); hook U28.

**B-new-2 — P3.** `cli/_commands.py:5276 _render_cost_json` hardcodes `"cached_age_s": 0`. The spec (`docs/superpowers/specs/2026-06-12-b2-cost-dashboard-design.md:535`) defines the field as the cache age on fresh-success *or stale-fallback*; the plan (`docs/superpowers/plans/2026-06-12-b2-cost-dashboard.md:1823`) says "Task 6 will populate from cache" — never done.
*Scenario:* `kinoforge cost --json` after a RunPod transport failure with a 10-minute-old cache → `balance.runpod.cached_age_s` is `0` (the `balance_errors` map still carries "transport (using cache)", so a consumer *can* tell — hence P3). Introduced `b1a1196d`.

**B-new-3 — P3 (narrow).** `cli/_commands.py:5403 _cmd_sweeper_start`: `interval_s = float(args.interval_s) if args.interval_s else cfg.sweeper.interval_s` — `--interval-s 0` is falsy and is silently replaced by the cfg value instead of reaching the `interval_s <= 0` refusal at `:5404-5406`.
*Scenario:* `kinoforge sweeper start -c cfg.yaml --interval-s 0` → expected `invalid interval_s=0.0` + exit 2; actual: daemon starts at `cfg.sweeper.interval_s`.

(The `_cmd_destroy` branch drift — `TeardownError` caught in the orphan copy but not the ledger copy — is recorded under the prior P3 row in A rather than as a new bug.)

**Specifically checked and found clean:** every `_cmd_*` exit-code table against its docstring (deploy/provision/generate/upscale/interpolate/image/text/batch/list/status/stop/destroy/logs/forget/reap/gc/cost/doctor/sweeper */pod lora ls/grid); the three mutex checks; `_resolve_run_id`'s clock seam; the `file://` round trip (`core/media.local_artifact:125` builds `f"file://{p}"` *unencoded*, so every `removeprefix("file://")` site is the exact inverse — no percent-encoding bug); `--image-dir` honours `--scale` (`:1808`); sidecar/context lazy-store precedence; `batch_formatters`; `loras_arg` tokenizer and duplicate detection; `_reconcile` adoption ordering (record-then-collapse, same-key SkyPilot guard); `tile`/`chunk` arithmetic (contract-tested); `outputs/local` collision sequence (98 numeric + 16 hash, error message matches); `_main.py` session-global propagation.

---

## C. New hygiene findings

Format: location · category · strictly behaviour-preserving? · risk · call · rationale.

1. `cli/_commands.py:5205,5251,5291` · dead · yes (module-level `from kinoforge.core.reaper import Verdict` at `:55`) · low · **FIX NOW** · three more local re-imports the prior audit missed.
2. `cli/_commands.py:2171` · dead · yes (`TeardownError` is module-level, `:38`) · low · **FIX NOW** · `_cmd_batch`'s local errors tuple re-imports it.
3. `cli/_commands.py:1879` · dead · yes (`UnknownAdapter` module-level, `:38`) · low · **FIX NOW** · `_cmd_image`'s local errors tuple.
4. `cli/_commands.py:522,533` · dead/naming · yes · low · **FIX NOW** · `import dataclasses as _dc` aliases the module already imported at `:12`.
5. `cli/_commands.py:536-538,619` · redundant · yes · low · **FIX NOW** · `EnvCredentialProvider` imported twice in the same `if provider is not None:` branch of `_cmd_provision` (as `_ECP`, then bare); `rg -n '_ECP' tests/` → 0.
6. `cli/_commands.py:3750` + `:3675` · dead · yes · low · **FIX NOW** · `cfg.lifecycle() if cfg is not None else Lifecycle()` on a non-Optional `cfg: Config` (`:3651`); the runtime `Lifecycle` import exists only for that dead arm. No test passes `cfg=None` (`rg -n 'cfg=None' tests/cli/test_resolve_warm_instance*.py tests/cli/test_scan_warm_candidates.py tests/cli/test_cmd_generate_scan.py` → 0).
7. `cli/_commands.py:5703-5724, 5760-5763` · documentation · yes · low · **FIX NOW (docs)** · `_cmd_grid` docstring + `except` comment describe a `run_grid` `ValueError` for `--ephemeral` + `lora_swap:` that does not exist — the executor's only `ValueError`s are `cfg_path is None` (`core/grid/executor.py:305`) and a missing `prompt:` (`:312`). The `except ValueError` itself is still live for those.
8. `cli/_commands.py:4664-4667` · documentation · yes · low · **FIX NOW (docs)** · `_cmd_reap` docstring lists `DEFAULT_APPLY_POLICY` as three verdicts; it is seven (`core/reaper.py:69-81`).
9. `cli/_commands.py:2398-2401` · documentation · yes · low · **FIX NOW (docs)** · non-existent sha `acdc8e1` + a `_cmd_deploy` record site that no longer exists.
10. `cli/_commands.py:2733-2745 _rc_to_reason` · documentation/naming · **no** (the label reaches an INFO line and `tests/cli/test_scan_warm_candidates.py:402` asserts it) · low · NEEDS DISCUSSION · docstring says rc=1 is "ledger-absent", code returns `"cap-key-drift"`.
11. `cli/_main.py:1391` · documentation · yes · low · **FIX NOW (docs)** · comment lists 7 read-only commands; `_READ_ONLY_CMDS` (`:93-106`) has 10, and the thing that actually decides is membership in `_INTERRUPTIBLE_CMDS`.
12. `cli/_reconcile.py:18-22` · documentation · yes · low · **FIX NOW (docs)** · "imported from ``kinoforge.core.lifecycle``" → the constants live in `core/launch_phase.py:31-47`. (The duplication itself stays a keep: parity-tested.)
13. `_adapters.py:68-113` · documentation · yes (comment moves only; import order unchanged) · low-med (ruff `I` must keep the layout — the existing blank-line+comment pattern survives pre-commit today, so it should) · **FIX NOW (docs)** · `# Providers` heads three `engines.*` imports, `# Engines` heads `engines.fake`, `# Output sinks` heads `outputs.local` *and* four `providers.*` *and* two `sources.*`, `# Sources` heads only `sources.http`/`huggingface`.
14. `_adapters.py:60-63` · documentation · yes · low · fold into 13 · "a small set of cross-provider dispatch helpers (:func:`build_heartbeat_endpoint_for`)" — there are five.
15. `_adapters.py:272 build_heartbeat_endpoint_for` · error-handling/naming · **no** (user-facing message) · low · NEEDS DISCUSSION · a registered provider (`modal`) is reported as "unknown provider for heartbeat dispatch"; reachable only with an explicit non-`none` `compute.heartbeat_mode` (default `"none"`, `core/config.py:1140`).
16. `cli/_commands.py:3289 _ephemeral_index_add` gate · error-handling · **no** · med · NEEDS DISCUSSION · gated on `EphemeralSession.current()`, which `_ephemeral_strict_session`'s docstring (`:2927-2945`) calls the wrong gate for an index write; consequence: every non-ephemeral cold create also writes an index row, and the write is unguarded (B-new-1). Fence: `71ecbe8c` titles it "ephemeral cold-create writes index row" — the non-ephemeral write reads as accidental, but the later `_scan_warm_candidates` union ("ledger wins on overlap", `:2786`) tolerates it, so intent is ambiguous.
17. `cli/_commands.py:4562-4575` vs `:4610-4633 _cmd_destroy` · redundant · yes · low-med · NEEDS DISCUSSION (fold into the prior P3) · both branches build `EphemeralIndex(store=ctx.store())` + `destroy_confirmed(..., sleep=lambda _: None, ...)`; the except-tuples already drifted.
18. `cli/_commands.py:4331` · magic · n/a · · fold into ND #18 · `endpoints_map.get("8000")` — third spelling of "the pod server port" in cli.
19. `cli/_commands.py:4932-4935 _cmd_gc` · error-handling · yes (comment) · low · LEAVE · `except FileNotFoundError: pass` with no intent comment (intent is obvious: a vanished artifact is already collected). Add the comment when next touched.
20. `cli/_commands.py:2502-2505 _human_bytes` · dead · yes · low · LEAVE · `if n is None: return "?"` under `n: int | float`; callers pass `int(... or 0)`.
21. `cli/_commands.py:4477 _cmd_logs` · redundant/magic · yes · low · LEAVE · `getattr(args, "file", None) or "bootstrap.log"` re-defaults what argparse already defaults (`_main.py:1065`).
22. `cli/_commands.py:5742 _cmd_grid` · magic · n/a · · LEAVE · `output_dir = Path("output")` hardcoded (grid has no cfg; reading anything else is a behaviour change).
23. `cli/_commands.py:4040-4044 _cmd_status` · magic · · · cross-area (ND #4) · dead-man `3 * interval` with a `30.0` literal default.
24. `cli/_commands.py:5169-5172 _cmd_cost` · dead · yes · low · LEAVE · `throttle_warnings: tuple[str, ...] = ()` is always empty; `threshold` feeds only `threshold_set` (tied to the "pending B10" text in ND #17 — resolve together).
25. `pipeline/interpolate.py:32-48 _default_count` · error-handling/redundant · **no** (exception type) · low · NEEDS DISCUSSION · raw `subprocess.run(check=True)` raises `CalledProcessError`/`FileNotFoundError`, not `FrameExtractionError` like `ffprobe_fps` (`core/frames.py:235-278`) and `chunk.ffprobe_frames` (`:943-982`) — a missing `ffprobe` on `kinoforge interpolate --video <local>` is a raw traceback. Same argv as `rife/_runtime._probe_frame_count` (pod side; embed boundary forces that copy).
26. `pipeline/interpolate.py:116-119 _run_local` · structural · **no** (process-exec order) · low · LEAVE · decimates (spawns ffmpeg) before checking `self.publish is None` and raising.
27. `pipeline/upscale.py:292-300` ≡ `pipeline/interpolate.py:96-102` `_local_path`; `upscale._source_h:341-348` restates it inline · redundant · yes · low · NEEDS DISCUSSION · with `text.py:41-46` and the embedded `rife/_engine.py:141` that is five decoders of one URI shape; `core.media.local_artifact` owns the builder, so the inverse belongs beside it.
28. `pipeline/artifact_bytes.py:101-106` vs every `removeprefix("file://")` site · naming/redundant · · · LEAVE (note) · `artifact_bytes` decodes with `url2pathname(urlparse(uri).path)` while the builder (`media.py:125`) never encodes — a `%`/`#` in a path would be mis-decoded here, but only engine-produced artifacts reach this function today.
29. `pipeline/keyframe.py:48` · dead · yes · low · LEAVE · `image_profile: ImageProfile  # reserved for future spec validation` is unused; it is a constructor field, so removal touches callers.
30. `interpolators/rife/_runtime.py:86` · magic/redundant · yes · med (**embedded** → goldens) · LEAVE (batch with the next embedded-file change) · `/workspace/Practical-RIFE` re-hardcoded; `_engine.py:37 _RIFE_REPO_DIR` names it and its comment says `_runtime` adds it to `sys.path`.
31. `interpolators/rife/_runtime.py:239-245 _sha256_file` · redundant · · embedded · LEAVE · fifth private sha256 helper (prior audit lists three + `core/downloader.sha256_file`); pod side cannot import core's.
32. `interpolators/rife/_runtime.py:179-180` · dead (documented) · · embedded · LEAVE (note) · lazily imports `kinoforge.pipeline.decimate`, which is NOT in the rife embed list (`runpod-diffusers-rife-60fps-interpolate.yaml:33-37`: `interpolators.rife`, `core.errors`, `core.frames`, `core.fps_resolver`); the module docstring (`:9-12`) says the branch is "RIFE-never-hit" and `test_pod_embed_closure` tolerates it.
33. `cli/sidecar.py:3-6`, `cli/__init__.py:1-11` · documentation · yes · low · LEAVE · minor stale lists ("no-config subcommands (list, stop, destroy, forget, reap)" omits status/logs/pod).
34. `cli/_commands.py:1355-1365 _cmd_interpolate` · redundant · · · fold into A "launch-row stamp" row · a fourth settle variant that cannot reuse `_finish_launch_row` until the documented never-ledger-stamps gap is closed.

---

## D. FIX NOW edit list

All items below: `embedded: no` (`rg -n 'kinoforge\.(cli|pipeline|outputs)' examples/configs tests/providers/test_pod_embed_closure.py` → 0 hits). Run `pixi run pre-commit run --files <file>` after each group; ruff-format may re-wrap a shortened import tuple — that is expected.

### D.1 Code (dead code / dead re-imports) — `src/kinoforge/cli/_commands.py` unless noted

Monkeypatch safety: `rg -n '_commands\.(datetime|json|urllib|Verdict|UnknownAdapter|TeardownError|dataclasses|Lifecycle)' tests/` → only `tests/cli/test_cmd_logs.py` patches `kinoforge.cli._commands.urllib.request.urlopen`, which targets the *module-level* `urllib` (kept) — unaffected by removing the local re-import in `_http_get_json`.

**D1** `:2160-2162` (`_cmd_batch`) — fence `2daf7266 feat(cli): kinoforge batch subcommand` (local import landed with the command; module-level `datetime` at `:22` predates it; no reason recorded).
```
old:
    from datetime import datetime

    from pydantic import ValidationError as PydanticValidationError
new:
    from pydantic import ValidationError as PydanticValidationError
```
Tests: `tests/test_batch_cli.py`, `tests/cli/test_cmd_batch_scan.py`, `tests/cli/test_cmd_batch_ephemeral.py`.

**D2** `:3473-3476` (`_resolve_attach_pod`) — fence `57f92447 feat(cli): --attach-pod + --emit-provision-record`.
```
old:
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter

    ledger = ctx.ledger()
    entry = ledger.read(pod_id)
new:
    from kinoforge.core import registry

    ledger = ctx.ledger()
    entry = ledger.read(pod_id)
```
Tests: `tests/cli/test_resolve_attach_pod.py`, `tests/cli/test_generate_attach_pod.py`, `tests/cli/test_ledger_endpoint_seeding.py`.

**D3** `:3630-3632` (`_write_provision_record`) — fence `57f92447`.
```
old:
    from datetime import datetime

    endpoint_url = instance.endpoints.get("http") or next(
new:
    endpoint_url = instance.endpoints.get("http") or next(
```
Tests: `tests/cli/test_generate_attach_pod.py`.

**D4 + D12** `:3673-3678` and `:3749-3750` (`_resolve_warm_instance`) — fence `476b3940 feat(b4): add _resolve_warm_instance helper`. `cfg: Config` is non-Optional (`:3651`); `Lifecycle` as an annotation on `_refuse_reason_for_verdict:3906` is covered by the `TYPE_CHECKING` import (`:68`) + `from __future__ import annotations`.
```
old:
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.reaper import classify

    now = time.time()
new:
    from kinoforge.core import registry
    from kinoforge.core.reaper import classify

    now = time.time()
```
```
old:
    # 6. classify verdict gate.
    lifecycle = cfg.lifecycle() if cfg is not None else Lifecycle()
new:
    # 6. classify verdict gate.
    lifecycle = cfg.lifecycle()
```
Tests: `tests/cli/test_resolve_warm_instance.py`, `tests/cli/test_resolve_warm_instance_endpoints.py`, `tests/cli/test_scan_warm_candidates.py`, `tests/cli/test_cmd_generate_scan.py`.

**D5** `:4258-4261` (`_http_get_json`) — fence `587f80de refactor(cli): split package into _main + _commands` (carried from `cli.py`).
```
old:
    import json
    import urllib.request

    req = urllib.request.Request(  # noqa: S310 — operator-supplied pod URL
new:
    req = urllib.request.Request(  # noqa: S310 — operator-supplied pod URL
```
Tests: `tests/cli/test_pod_lora_ls.py`, `tests/cli/test_scan_warm_candidates.py`.

**D6** `:5203-5207`, `:5249-5253`, `:5289-5293` (`_render_cost_human/_json/_prom`) — fence `b1a1196d feat(b2): kinoforge cost subcommand` (the module-level `Verdict` import at `:55` arrived in the same commit; the three local ones are style residue). Three edits, same shape:
```
old:
    """Human-readable cost table per spec §5."""
    from kinoforge.core.reaper import Verdict

    lines: list[str] = []
new:
    """Human-readable cost table per spec §5."""
    lines: list[str] = []
```
```
old:
    """Render the stable §10 JSON schema."""
    from kinoforge.core.reaper import Verdict

    out: dict[str, Any] = {
new:
    """Render the stable §10 JSON schema."""
    out: dict[str, Any] = {
```
```
old:
    """Render Prometheus text exposition per spec §9. LF-only."""
    from kinoforge.core.reaper import Verdict

    lines: list[str] = []
new:
    """Render Prometheus text exposition per spec §9. LF-only."""
    lines: list[str] = []
```
Tests: `tests/cli/test_cmd_cost.py`.

**D7** `:2166-2172` (`_cmd_batch`) — fence `2daf7266`; module-level `TeardownError` at `:38` (`eaa4e74d`).
```
old:
    from kinoforge.core.errors import (
        BudgetExceeded,
        CapabilityMismatch,
        ConfigError,
        KinoforgeError,
        TeardownError,
    )
new:
    from kinoforge.core.errors import (
        BudgetExceeded,
        CapabilityMismatch,
        ConfigError,
        KinoforgeError,
    )
```
Tests: as D1.

**D8** `:1876-1881` (`_cmd_image`) — fence `3cbf1bf0 feat(cli): add the image subcommand`. `except UnknownAdapter` at `:1934` resolves to the module-level name.
```
old:
    from kinoforge.core.errors import (
        Cancelled,
        KinoforgeError,
        UnknownAdapter,
        ValidationError,
    )
new:
    from kinoforge.core.errors import (
        Cancelled,
        KinoforgeError,
        ValidationError,
    )
```
Tests: `tests/cli/test_cmd_image.py`.

**D9** `:522-525` and `:533` (`_cmd_provision`) — fence `5b05a2b8 fix(cli): kinoforge provision threads the rendered payload into its spec` (alias introduced with the block; no reason given). `rg -n '_dc\.' tests/` → 0.
```
old:
        import dataclasses as _dc

        from kinoforge.core.orchestrator import _cfg_dict
        from kinoforge.core.spec_builder import build_instance_spec
new:
        from kinoforge.core.orchestrator import _cfg_dict
        from kinoforge.core.spec_builder import build_instance_spec
```
```
old:
        cfg_dict["lifecycle"] = _dc.asdict(lifecycle)
new:
        cfg_dict["lifecycle"] = dataclasses.asdict(lifecycle)
```
Tests: `tests/cli/test_cmd_provision.py`.

**D10** `:536-538` and `:619-621` (`_cmd_provision`, same `if provider is not None:` branch) — fence `5b05a2b8` / U21. `rg -n '_ECP' src/ tests/ | rg -v _commands.py` → 0.
```
old:
        from kinoforge.core.credentials import EnvCredentialProvider as _ECP

        _creds = _ECP()
new:
        from kinoforge.core.credentials import EnvCredentialProvider

        _creds = EnvCredentialProvider()
```
```
old:
        from kinoforge.core.credentials import EnvCredentialProvider
        from kinoforge.core.orchestrator import _wait_for_provider_ready
        from kinoforge.core.provisioner import provision
new:
        from kinoforge.core.orchestrator import _wait_for_provider_ready
        from kinoforge.core.provisioner import provision
```
(`:662` in the hosted branch is a different scope — leave it.) Tests: `tests/cli/test_cmd_provision.py`.

**D11** `:730-732` (`_dry_run_swap_preview`) — introduced with `--dry-run-swap` (`71ecbe8c` era); the `getattr` default was never reachable (`core/interfaces.py:155`). `tests/cli/test_dry_run_swap.py:50` fakes a lifecycle *with* the attribute; other dry-run-swap tests use real YAML cfgs.
```
old:
    threshold = float(
        getattr(lc, "lora_swap_re_probe_after_s", 300.0) if lc is not None else 300.0
    )
new:
    threshold = float(lc.lora_swap_re_probe_after_s if lc is not None else 300.0)
```
Tests: `tests/cli/test_dry_run_swap.py`, `tests/cli/test_cmd_generate_loras.py`, `tests/test_batch_cli.py`, `tests/cli/test_generate_attach_pod.py`, `tests/cli/test_ephemeral_index_timing.py`.

**D13** `src/kinoforge/cli/_main.py:190-194` (`_install_redacting_filter`) — fence `101dc3d9 chore: state-management refactor + write-site invariants`; module-level `logging` at `:10`. `rg -ln '_install_redacting_filter' tests/` → 0 files.
```
old:
    per logger and one factory override.
    """
    import logging

    flt = RedactingLogFilter(RedactionRegistry.instance(), bypass=bypass)
new:
    per logger and one factory override.
    """
    flt = RedactingLogFilter(RedactionRegistry.instance(), bypass=bypass)
```
Tests: `tests/test_cli.py`, `tests/cli/test_session_global_flag_positions.py` (any `main()` caller).

**D14** `src/kinoforge/pipeline/generate_clip.py:176-178` — fence `a8b2df42 feat(pipeline): GenerateClipStage publishes to OutputSink (Layer O Task 4)` (the publish block arrived with its own copy of a line computed 11 lines earlier; `last` is unchanged between).
```
old:
        if self.sink is not None:
            ext = Path(last.filename).suffix or ".bin"
            self.sink.publish(
new:
        if self.sink is not None:
            self.sink.publish(
```
Tests: `tests/pipeline/test_generate_clip.py`, `tests/pipeline/test_opaque_store_name.py`, `tests/test_layer_r_backcompat.py`, `tests/core/test_orchestrator.py`.

### D.2 Docs-only (comments / docstrings; no runtime text)

Proof no test asserts on the changed phrases (`rg -n --fixed-strings '<phrase>' tests/ docs/ src/`, hygiene docs excluded): "for v1 the handler only owns" 0; "acdc8e1" 1 (the comment itself; PROGRESS.md:10418 also cites it — append-only, leave); "IDLE_REAP, OVERAGE_REAP, STALE_LEDGER)" only in `docs/superpowers/plans/2026-06-06-*` (historical); "installed for ``generate``" 1 (itself); "One implementation, two" 2 (itself + a plan doc); "is now a constructor field" 1; "NEW — always populated" 2; "lora_swap:`` refusal" 1; "list / status / stop / destroy / forget / reap / gc" 1; "kinoforge.core.lifecycle``: this module" 1; "small set of cross-provider dispatch helpers" 2 (itself + a doc). "Mirrors _cmd_generate's warm-reuse" has 3 hits — controller: confirm none is a test assertion (`rg -n --fixed-strings "Mirrors _cmd_generate's" tests/`). "<ts>_<slug><ext>" has 7 hits, all in docs/src prose (the old schema also lives in `docs/output-layout.md:10` — prior audit's docs row, tools+docs auditor).

**Dd1** `_commands.py:1097-1104`
```
old:
    """Handle ``upscale`` subcommand — standalone video upscale.

    The CLI surface validates scale + flag conflicts BEFORE any cfg
    load or pod provisioning so a malformed invocation costs zero
    cold-boot time. Full warm-reuse / attach / cold-create wiring
    routes through the orchestrator (see T16); for v1 the handler
    only owns the dry-run path + early refusal gates.
    """
new:
    """Handle ``upscale`` subcommand — standalone video / still-image upscale.

    The CLI surface validates scale + flag conflicts BEFORE any cfg
    load or pod provisioning so a malformed invocation costs zero
    cold-boot time. After the refusal gates and ``--dry-run`` it resolves
    ``--attach-pod`` / the warm scan, reserves the ephemeral launch row and
    drives ``orchestrator.generate(skip_clip_stage=True, initial_clip=...)``;
    ``--image-dir`` hands off to :func:`_cmd_upscale_image_dir`.
    """
```

**Dd2** `_commands.py:1208-1210`
```
old:
    # T11 — non-dry-run wiring. Reuses generate()'s machinery via the
    # skip_clip_stage flag (T10). Mirrors _cmd_generate's warm-reuse /
    # attach / cold-create precedence chain.
new:
    # T11 — non-dry-run wiring. Reuses generate()'s machinery via the
    # skip_clip_stage flag (T10). Runs the simpler attach chain shared with
    # _cmd_interpolate / _cmd_text — deliberately NOT _cmd_generate's (see
    # _resolve_warm_attach_chain's docstring for the differences).
```

**Dd3** `_commands.py:2398-2401`
```
old:
# Map ledger key -> Lifecycle attribute. The ledger persists the generic
# name `max_age_s`; the Lifecycle dataclass attribute is `max_lifetime_s`.
# T1 commit acdc8e1 introduced the same mapping at the _cmd_deploy call
# site; this map mirrors it on read.
new:
# Map ledger key -> Lifecycle attribute. The ledger persists the generic
# name `max_age_s`; the Lifecycle dataclass attribute is `max_lifetime_s`.
# The write side of the same mapping sits beside each `ledger.record` call
# (e.g. _stamp_cold_created_instance: `max_age_s=int(lc.max_lifetime_s)`);
# this map mirrors it on read.
```

**Dd4** `_commands.py:4664-4667`
```
old:
    Dry-run by default. ``--apply`` activates DEFAULT_APPLY_POLICY
    (IDLE_REAP, OVERAGE_REAP, STALE_LEDGER). Opt-in flags
new:
    Dry-run by default. ``--apply`` activates DEFAULT_APPLY_POLICY
    (IDLE_REAP, OVERAGE_REAP, STALE_LEDGER, STALL_REAP, RESTART_LOOP_REAP,
    DEGRADED_REAP, GC_404 — see ``core.reaper.DEFAULT_APPLY_POLICY``). Opt-in flags
```

**Dd5** `_commands.py:5704-5710` and `:5717-5724` (docstring) and `:5760-5763` (comment)
```
old:
    spec error → 1. Exit 2 is also emitted (review round 2, U11) for an
    ``--ephemeral`` + ``lora_swap:`` refusal — see below; that refusal never
    produces a :class:`GridResult` so it cannot collide with a real
    ``"partial"`` run in practice, but a caller branching on exit codes
    cannot distinguish the two from the code alone.
new:
    spec error → 1. Exit 2 is also emitted for a ``ValueError`` raised by
    :func:`run_grid` before any cell runs (a ``generate:`` cell with no
    ``cfg_path`` or no top-level ``prompt:``); that never produces a
    :class:`GridResult`, but a caller branching on exit codes cannot
    distinguish it from a real ``"partial"`` run.
```
```
old:
    :func:`run_grid` raises ``ValueError`` when ``--ephemeral`` is combined
    with a ``lora_swap:`` cell — that shape is refused, not silently run
    non-ephemerally (U24). Caught here (review round 2: the controller
    ruling's fail-closed precedent, ``_preflight_ephemeral``'s stderr-block
    + ``return 2``, applies to the presentation too, not only to the
    refuse-vs-warn decision) and reported as a clean stderr message + exit
    2, never a raw traceback.
new:
    The ``--ephemeral`` + ``lora_swap:`` refusal U24 once placed here is no
    longer enforced by :func:`run_grid` (no such raise exists; swap cells run
    under the session like any other). The ``except ValueError`` below stays
    for ``run_grid``'s own pre-run spec faults and keeps the fail-closed
    presentation (``_preflight_ephemeral``'s precedent: clean stderr line +
    exit 2, never a raw traceback).
```
```
old:
        # U24 (review round 2): run_grid's ephemeral + lora_swap refusal
        # must surface as a clean fail-closed message, not an unhandled
        # Python traceback — matching _preflight_ephemeral's precedent.
new:
        # run_grid's pre-run spec faults (a generate cell with no cfg_path or
        # no `prompt:`) surface as a clean fail-closed message, not an
        # unhandled Python traceback — matching _preflight_ephemeral's precedent.
```

**Dd6** `cli/_main.py:1389-1392`
```
old:
    # the long-running orchestration subcommands. Read-only operations
    # (list / status / stop / destroy / forget / reap / gc) keep the
new:
    # the long-running orchestration subcommands. Every other subcommand
    # (anything outside _INTERRUPTIBLE_CMDS) keeps the
```

**Dd7** `cli/context.py:49-51`
```
old:
    # SIGINT handler (installed for ``generate`` / ``batch``) sets this
new:
    # SIGINT handler (installed for ``_main._INTERRUPTIBLE_CMDS``) sets this
```

**Dd8** `cli/_reconcile.py:3-5`
```
old:
Used by both ``kinoforge list`` (`_cmd_list`) and the top-of-command
instance overview (`_print_instance_overview`). One implementation, two
callers — a dead pod's ``est_spend`` (age×rate) must not inflate forever.
new:
Used by ``kinoforge list`` (`_cmd_list`), the top-of-command instance
overview (`_print_instance_overview`) and ``kinoforge provision``'s
duplicate-key check (`_cmd_provision`). One implementation, three
callers — a dead pod's ``est_spend`` (age×rate) must not inflate forever.
```

**Dd9** `cli/_reconcile.py:19-20`
```
old:
# provisional row, duplicated as a literal rather than imported from
# ``kinoforge.core.lifecycle``: this module is imported on every CLI command,
new:
# provisional row, duplicated as a literal rather than imported from
# ``kinoforge.core.launch_phase``: this module is imported on every CLI command,
```

**Dd10** `outputs/local.py:3-4`
```
old:
Writes bytes to ``<dir>/<namespace>/<ts>_<slug><ext>`` with atomic
rename semantics and ``_2.._99`` then sha256-hash collision suffixes.
new:
Writes bytes to ``<dir>/<namespace?>/<ts>_[<kind>_]<provider>_<model>_<slug><ext>``
with atomic rename semantics and ``_2.._99`` then sha256-hash collision suffixes.
```

**Dd11** `pipeline/generate_clip.py:6` and `:75`
```
old:
``segments`` is now a constructor field — the orchestrator (and batch) populate
new:
``segments`` is a constructor field — the orchestrator (and batch) populate
```
```
old:
    segments: list[Segment]  # NEW — always populated by orchestrator
new:
    segments: list[Segment]  # always populated by the orchestrator
```

**Dd12** `_adapters.py:60-63` and `:68-99` (comment moves only; keep import order). Verify afterwards with `pixi run ruff check src/kinoforge/_adapters.py` (rule `I`) — the file already relies on blank-line+comment groups inside one import block surviving isort, so re-seated comments should too.
```
old:
module also exposes a small set of cross-provider dispatch helpers
(:func:`build_heartbeat_endpoint_for`) that need to import concrete
providers and therefore cannot live in core.
new:
module also exposes the cross-provider dispatch helpers
(:func:`build_provider_for`, :func:`build_capacity_wait_for`,
:func:`build_heartbeat_endpoint_for`, :func:`build_balance_endpoint_for`,
:func:`build_util_endpoint_for`) that need to import concrete providers and
therefore cannot live in core.
```
Section headers: `:68 # Providers` → `# Engines`; delete `:72-73` (blank + `# Engines` above `engines.fake`); insert a blank line + `# Providers` above `:91 import kinoforge.providers.local`; insert a blank line + `# Sources` above `:95 import kinoforge.sources.civarchive`; delete `:97-98` (blank + `# Sources` above `sources.http`). No test references these comments (`rg -n '# Providers|# Output sinks' tests/` → 0).

---

## E. Needs discussion

1. **`_cmd_cost` cfg-None fallback (`:5099-5119`).** CLI-unreachable (`-c` required) and untested, but a programmatic caller with `ctx.cfg = None` would see thresholds move from (600, 3600, 300) to `Lifecycle()`'s (7200, 18000, 1800) — so not strictly behaviour-preserving. Ready edit if accepted: replace the `if cfg is not None: ... else: ...` ladder with `lc = cfg.lifecycle() if cfg is not None else Lifecycle()` (runtime import `from kinoforge.core.interfaces import Lifecycle` beside `:5067`) and keep the existing `float(...)` coercions on the four fields. Decision: accept the unreachable-path change, or keep the dead arm.
2. **`_rc_to_reason` (`:2733-2745`).** Either rename the rc=1 label to `ledger-absent` (INFO-line text + `tests/cli/test_scan_warm_candidates.py:402` must change) or fix the docstring to say the label is deliberately defensive. The code path cannot fire in the scan (`entry` always comes from the ledger).
3. **Help-text drift in `_main.py` (ND #17).** Four operator-facing strings: `:649` civarchive "resolver is pending" (false — `CivArchiveSource` is registered); `:1101` `--force-forget` "(implies --apply)" vs the handler's exit 4; `:701` 5-of-6 record keys; `_render_cost_human:5246-5247` unconditional "replicate spend tracking pending B10" (+ the inert `throttle_warnings`/`threshold` plumbing, C-24). Decide wording, then fix handler-vs-help together.
4. **`--diagnostic-mode` help (`_main.py:584-591`)** promises "S3 boot-log capture" on `deploy`; per B13 the overlay never reaches `deploy`'s spec. Fix the orchestrator (core-2) or the help — not both halves silently.
5. **`_cmd_destroy` (`:4544-4633`).** Unify the two branches behind one helper, catch `TeardownError` on the ledger branch too (today a raw traceback), and decide whether production should keep passing the test-only `sleep=lambda _: None` (three back-to-back confirm polls against RunPod's eventually-consistent listing) — ties to the prior P3.
6. **`_ephemeral_index_add` (B-new-1 / C-16).** (a) Guard the post-create index writes the way the pre-create one is guarded ("never raises; log and forfeit"); (b) decide whether the gate should be `_ephemeral_strict_session()` (ephemeral-only, as `71ecbe8c`'s title implies) or stay `current()` (every CLI cold create). (a) is safe on its own; (b) changes which pods get index rows. Core half: `orchestrator.py:1554` should wrap the callback like `:1969` does.
7. **Lifecycle→classify kwargs ×4 in cli (ND #3).** `_classify_for_status`, `_resolve_warm_instance`, `_cmd_reap`, `_cmd_cost` each hand-map `Lifecycle` → `classify` kwargs and have drifted (ephemeral keys, float coercion). One helper (`classify_kwargs(lifecycle) -> dict`) next to `sweeper_thresholds_from_cfg` would close it; today's drift is inert for status (ephemeral keys only matter for index rows), so this is prevention.
8. **Launch-row stamp ×4 / attach ladder ×4 / refusal ×3.** `_cmd_generate:1071-1090` and `_cmd_text:2116-2125` can call `_finish_launch_row` today (generate needs the `cfg_wak` return added to the helper); `_cmd_interpolate` cannot until its documented never-ledger-stamps gap (plan 2026-07-12 Task 3) is closed — decide whether to close it in the same change. The four identical attach ladders + three identical refusals are one small helper (`_resolve_simple_attach(args, ctx, cfg)`), distinct from `_resolve_warm_attach_chain` by design (keep `:778-784`).
9. **`kinoforge_key` width.** Five cli sites spell `derive()[:12]`; `_cmd_provision` already imports `orchestrator._key_hash`. Moving the width into one public helper (`core.capability_key.key_hash`?) is the real fix; importing a private orchestrator name into five more places is not.
10. **`is_launching` spellings.** Make `_reconcile._is_launching` delegate to `core.launch_phase.is_launching` (semantically identical: both test `tags[kf_launch_phase] == "launching"`) and replace the inline copy at `:3179-3181`; the `_reconcile` literal keep stays (parity-tested) but its stated "not imported on every command" reason is moot since `_commands.py:45-50` imports `core.launch_phase` at module level.
11. **Sweeper key/host (`:5402-5695`).** `_sweeper_row_id(cfg) -> str` (one `socket.gethostname()` fallback, one `f"sweeper:{host}"`) — note `core/sweeper.py:289` spells the key too (cross-area).
12. **Proxy host at `:4479`.** `_PROXY_URL_PATTERN` is provider-private; options: expose a public `runpod.proxy_url(pod_id, port)` and import it in cli (cli already imports engine privates in `pod_health.py`), or accept the literal with a comment pointing at the pattern.
13. **`pipeline/decimate.py` ≡ `downscale.py`** — one `run_ffmpeg_on_bytes(video_bytes, argv_builder, run)` helper closes the "exit-183 fix must change both" risk; and the five `file://` strippers (C-27) belong beside `core.media.local_artifact` as `local_path_of(artifact)`. Both behaviour-preserving but touch stage seams that tests inject.
14. **`_adapters.build_heartbeat_endpoint_for:272`** — wording for an explicitly-configured but unsupported provider ("modal has no heartbeat substrate; set heartbeat_mode: none") vs today's "unknown provider".
15. **`cached_age_s` (B-new-2)** — populate from `cached_at` (the data is already in `cached_balance_read`) or drop the field from the §10 schema; either changes JSON output.
16. **`interpolate._default_count` (C-25)** — wrap in `FrameExtractionError` like its two siblings (changes the exception type on a missing/failing `ffprobe`).
17. **`--interval-s 0` (B-new-3)** — `is not None` instead of truthiness; one-token change but it alters which input reaches the refusal.

---

## F. Cross-area notes

- **core-2 / orchestrator:** `on_instance_created` is called unguarded at `orchestrator.py:1554` (outside the create try, `:1521-1549`) but guarded with a "launch continues" warning at `:1967-1976` — B-new-1's core half; also `rg restart_policy src/kinoforge/core/orchestrator.py` → 0 hits, so where `--diagnostic-mode`'s `restart_policy=never` is applied (B13) needs locating (spec_builder?).
- **core-2:** `_cmd_provision` is a hand copy of `deploy()`; the post-create-failure "never forgets the real row" P3 almost certainly exists in `deploy()` too (ND #2).
- **core-1:** `core/media.local_artifact:125` builds `file://{p}` unencoded; `pipeline/artifact_bytes.py:104` decodes with `url2pathname` — only engine-produced artifacts reach the latter today, so no bug, but the two inverses disagree on `%`/`#`.
- **core-1 (ND #4):** the dead-man `3 * interval` literal with a `30.0` default recurs at `cli/_commands.py:4040-4044`.
- **engines:** `interpolators/rife/_runtime.py:179-180` lazily imports `kinoforge.pipeline.decimate`, which is absent from the rife embed list (`runpod-diffusers-rife-60fps-interpolate.yaml:33-37`); documented as never hit on RIFE, but it is an ImportError waiting for any engine that sets `decimate_to`.
- **tests:** `tests/cli/test_cmd_cost.py:28-31` fakes `cfg.lifecycle()` with exactly the dead fallback's numbers (600/3600/None/300), so neither branch of `_cmd_cost`'s threshold ladder is distinguishable there; `tests/cli/test_scan_warm_candidates.py:402` pins the misleading `cap-key-drift` label; `tests/cli/test_cmd_logs.py` patches `kinoforge.cli._commands.urllib.request.urlopen` (module-level — safe for D5).
- **tools+docs:** the old `<ts>_<slug><ext>` schema has 7 textual hits in docs (prior audit's `docs/output-layout.md:10` row); `docs/superpowers/plans/2026-06-06-layer-v-*` still states the three-verdict `DEFAULT_APPLY_POLICY` (historical plan — leave).
- **controller (PROGRESS.md):** `PROGRESS.md:10418` cites the unresolvable sha `acdc8e1` for Layer S T1 — append-only file, record only.
- **providers:** `_PROXY_URL_PATTERN` (`providers/runpod/__init__.py:82`) is the only spelling of the proxy host outside cli `:4479`; `_cmd_pod_lora_ls:4331` hardcodes port `"8000"` as the pod server port (ND #18's "`_DEFAULT_SERVER_PORT`" twin lives in engines).
