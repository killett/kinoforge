# Hygiene re-audit 2026-10-21 — core part 1 (orchestrator / config / reaper / lifecycle / sweeper / heartbeat / locks / small modules)

Scope: `src/kinoforge/core/{orchestrator,config,interfaces,lifecycle,reaper,reaper_actor,sweeper,sweeper_metrics,heartbeat_loop,heartbeat_endpoints,util_endpoints,balance_endpoints,cost,pool,locks,launch_phase,session_claim,ephemeral,spec_builder,provisioner,provision_state,boot_liveness,errors,strategy,offers,capabilities,registry,clock,cancel,util_counter,runtime_probe,validation,logging,continuity,__init__}.py`.
HEAD `f89eff2b`, tree clean, read-only. Every file in the area was read in full except the long tail of `interfaces.py` (engine ABCs, read at the cited sites only) and `config.py` (read at the cited sites + all lifecycle/placement/sweeper/load paths).

**Embedded-module fact (for every `embedded:` line below):** `rg -o 'kinoforge\.core\.[a-z_]+' examples/configs | sort | uniq -c` → only `core.errors` (21), `core.scale_target` (13), `core.fps_resolver` (3), `core.frames` (3). No other file in this area is embedded, so `errors.py` is the only golden-moving file here.

---

## A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line, ≤2 lines) |
|---|---|---|---|
| B2 | `deploy()` never calls `_enforce_rate_cap` | **CONFIRMED** | `rg _enforce_rate_cap src/` → def `orchestrator.py:984`, sole call `:1577` (inside `_provision_instance_and_build_backend`). `deploy()` `:2338-2612` goes `create_instance(_build_spec())` `:2544` → `_wait_for_provider_ready` `:2581` → `ensure_endpoints` `:2588` with no cap read. Extra drift: `deploy` also skips `_create_with_capacity_wait` (generate path `:1536-1539`). |
| B6 | `_BURNING_VERDICTS` omits STALL_REAP / RESTART_LOOP_REAP | **CONFIRMED, wider** | `cost.py:29-38` = {LIVE, IDLE_REAP, OVERAGE_REAP, ORPHAN_REAP, HEARTBEAT_UNKNOWN, HEARTBEAT_SUBSTRATE_MISSING}; `DEGRADED_REAP` (pod alive, self-marked) is also omitted. `:51` docstring says "All 8 Verdict keys" — `Verdict` has 16 members (`reaper.py:35-53`). |
| B7 | `act_on_verdict` destroy set lacks RESTART_LOOP/DEGRADED although both are in `DEFAULT_APPLY_POLICY`; degraded short-circuits before overage | **CONFIRMED** | `reaper_actor.py:291-296` set = {IDLE_REAP, OVERAGE_REAP, ORPHAN_REAP, STALL_REAP}; `:352-357` `else: action = "no_op"`. `reaper.py:68-79` policy has RESTART_LOOP_REAP + DEGRADED_REAP. `reaper.py:717-724` `status == "degraded"` returns before row 2 (`:731-733`). |
| B13 | `deploy --diagnostic-mode` never passes `diagnostic_env=` | **CONFIRMED** | `deploy._build_spec` `:2490-2501` has no `diagnostic_env`; generate path `:1475-1489` passes `_build_diagnostic_env(run_id) if cfg.diagnostic_mode else None`. CLI sets `cfg.diagnostic_mode=True` (`cli/_commands.py:264-265`); `spec_builder.py:55-57` merges env only when `diagnostic_env` is given, `:85-92` still sets `restart_policy=never`. |
| B17 | ephemeral STALL_REAP needs `ceil(window/heartbeat)` samples, sweeper banks per tick capped at `ceil(window/sweeper_interval)` | **CONFIRMED — and unreachable under the shipped defaults** | `reaper.py:441-449` `required = ceil(stall_window_s / (heartbeat_interval_s or 30.0))`; `sweeper.py:316-329` `maxlen = max(ceil(stall_window_s / self._interval_s), orphan_samples-1)`. Defaults: `SweeperConfig.interval_s=60` (`config.py:1444`), `LifecycleConfig.stall_window_s=600` + `stall_reap_enabled=True` (`config.py:~121-122`), `Lifecycle.heartbeat_interval_s=None` → required 20, deque holds 10 → `len(history) < required` forever. The shipped `examples/configs/sweeper.yaml` (`heartbeat_interval_s: 30`, `interval_s: 60`, no `stall_window_s`) has exactly this 20-vs-10 shape. |
| P3 `orchestrator.py:2177-2192` | HeartbeatLoop thread leaks when verify raises `CapabilityMismatch` on the cache-hit path | **CONFIRMED** | hb_loop is started inside `_provision_instance_and_build_backend` (`:2135-2172`); verify at `:2182-2195` destroys + re-raises; the `finally: hb_loop.stop()` is at `:2271-2272`, after the raise point. Daemon thread → harmless for the CLI. |
| P3 `orchestrator.py:2879-2885`, `:3049-3055` | Ctrl-C WARN says "pod kept alive … run `kinoforge reap`" even under `--no-reuse` | **CONFIRMED** | Both WARNs are unconditional on `single`; the outer `finally` at `:2305-2312` destroys under `single=True`. |
| P3 `config.py:366` vs `engines/hosted` | documented `engine.<x>.auth:` block cannot reach any engine (HostedEngineConfig `extra=forbid`) | **PARTIAL (core side only)** | `HostedEngineConfig` `model_config = ConfigDict(extra="forbid")` `config.py:371`, fields `:373-379`, no `auth`; `rg -n auth config.py` → 0 hits. The engine-side `_resolve_auth` claim is cross-area (not verified here). |
| FIX NOW `orchestrator.py:1166-1172,1334-1342,1999-2002,2208-2214` | heartbeat "ticks throughout engine.provision" describes the reverted C29 order | **CONFIRMED** | `:1666-1690` starts the loop AFTER `_provision_compute_once` with the C33-m comment ("reverted on 2026-06-17"); pinned by `tests/core/test_orchestrator_c33_start_heartbeat_after_provision.py`. All four texts quoted verbatim in D. |
| FIX NOW `orchestrator.py` ten stale refs (`:176,589,636-640,1331-1332,1708-1710,1778-1784,2575,2647-2661,2752-2753,2835`) | stale callers / pointers / "Task N will" / budget tracker | **CONFIRMED 9, PARTIAL 1** | `:176` "Both call sites" — `deploy()` is a third (`:2419-2420`). `:589` cites `lifecycle.LAUNCH_PHASE_TAG` — canonical home `launch_phase.py:31` (lifecycle re-exports, so it resolves). `:638-640` "Task 6 … until then the reconciler cannot yet act" — `_reconcile._adopt_or_age_out` exists (`:2555-2556` cites it). `:1331-1332` "Task 5 adds the matching except Cancelled" — it is at `:1650`. `:1708-1710`/`:1782-1784` "sweeper / budget tracker" — `BudgetTracker` has zero production callers; `single=True` destroys at `:2305-2312`. `:2647-2661` lists 2 stages; Upscale/Text/Interpolate stages are appended `:2941-3017`. `:2752-2753` "(line 209)" — `_provision_compute_once` is `:348`. `:2835` "(line ~1008)" — keyframe block is `:2771-2778`. `:2575` PARTIAL: historical note is accurate; only the "mirrors" claim is inexact (deploy writes the real row after `ensure_endpoints` `:2592-2594`, generate path right after create `:1550-1554`). |
| FIX NOW `orchestrator.py:1288,1311-1313` `for_discovery`; `:348-446` `_provision_compute_once(store=)` docstring | `for_discovery` unread; docstring describes a lock that moved | **CONFIRMED both** | `rg for_discovery orchestrator.py` → `:1288` def, `:1311` doc, `:2115`/`:2161` call sites — never read in the body. `_provision_compute_once` docstring `:361-364` "Uses an artifact-store lock keyed on the instance id"; body `:398-403` says the lock is the outer `hold_until_first_tick`, never calls `store.acquire_lock`; the `store` parameter (`:353`, doc `:381-382`) is unused in the body. |
| FIX NOW `reaper.py:660-667`, `reaper_actor.py:79-90` | "one of the seven" verdicts; `ActionResult.action` list incomplete | **CONFIRMED** | `reaper.py:662-664` lists 7; `classify` returns 14 distinct values (11 ledger-branch incl. LAUNCHING/DEGRADED/STALL/RESTART + GC_404/SKIP_NO_PROBE/PROBE_FAILED on the ephemeral branch). `reaper_actor.py:81-82` "destroyed_and_forgot — IDLE_REAP / OVERAGE_REAP / ORPHAN_REAP" omits STALL_REAP (`:295`); `"gc_404_removed"` (`:316`) and `"probe_failed"` (`:350`) are missing from the list. |
| FIX NOW `heartbeat_loop.py:252,254`, `reaper.py:738,786` | stale `file:line` pointers | **CONFIRMED** | `lifecycle.py:651` → the `None`-skip is `lifecycle.py:877`; `reaper.py:334` → `return Verdict.HEARTBEAT_UNKNOWN` is `reaper.py:753`; `lifecycle.py:504` → `"provider": instance.provider` is `lifecycle.py:626`; `interfaces.py:65` → `grace_after_session_s` doc is `interfaces.py:122`. |
| dead `config.py:66` `VALID_KIND_TARGETS["lora"]` | unreachable since `dc3c4eba` | **CONFIRMED** | `ModelEntry.kind: Literal["base","vae","text_encoder","clip_vision"]` `config.py:973`; sole reader `:1735` `VALID_KIND_TARGETS.get(entry.kind, set())`; legacy `kind: lora` is promoted out of `models` by `_promote_legacy_kind_lora_to_loras_block` `:1615-1635` before field validation. Fence: dict introduced `6bf6b940`; removal of the kind documented at `:965`. |
| dead `session_claim.py:107-110` | `try: yield / except BaseException: raise` no-op | **CONFIRMED** | `session_claim.py:106-109` verbatim. Fence `6a42d0ec feat(b7): session_claim helper + FirstTickTimeout` (introducing commit, no reason given; docstring item 5 "propagate unchanged" is what a bare `yield` already does under `@contextmanager`). |
| dead `orchestrator.py:2947,2969,2993` triple local `registry` import | redundant re-import | **CONFIRMED** | module-level `from kinoforge.core import registry` `:32`; `from kinoforge.core import registry as _registry` at `:2947`, `:2969`, `:2993` binds the same module object. Fences `acbe0ed2` (upscale stage), `05a21b8e` (interpolate) — feature commits, no stated reason. No test touches `orchestrator._registry` (the 9 `_registry` hits in tests/core are an unrelated fixture in `test_reaper_sweep.py` and an attribute in `test_ledger_touch.py`). |
| dead three never-firing `noqa` (`orchestrator.py:426` D102, `config.py:2200` F401, `interfaces.py:531` B027) | RUF100 | **CONFIRMED + 1 missed** | `ruff check --extend-select RUF100` reports all three as "unused". Also `pool.py:67` `# noqa: D401, D102` — D102 unused (D401 is a non-enabled intent annotation, keep). Scratch copies with the four directives removed pass `ruff --select D102,F401,B027` ("All checks passed!"). The other ~44 RUF100 hits in the area are the documented non-enabled BLE001/SLF001/PLC0415/ARG001/TC001 annotations (hygiene-notes keep). |
| redundant `orchestrator.py:1447-1456,1872-1879,2468-2471` cfg-dict lifecycle lift ×3 | three copies, each "authoritative" | **CONFIRMED** | `:1446-1456` ("This lift is the single authoritative source"), `:1872-1879` (deploy_session; feeds only the warm-attach `engine.provision` and hosted `backend()` calls — the cold path re-lifts inside the helper), `:2468-2471` ("Same lift as the generate path"). |
| redundant `orchestrator.py:2312` `ttl_s=30.0` vs `reaper_actor._LOCK_TTL_S` | one lease length, two spellings | **CONFIRMED** | `orchestrator.py:2312` `store.acquire_lock(f"reaper/{instance.id}", ttl_s=30.0)`; `reaper_actor.py:40` `_LOCK_TTL_S: float = 30.0`, used `:239`, `:653`. Same key, both around `destroy_confirmed`. |
| redundant `reaper_actor.py:322-351` vs `:608-644` WARN-once ×2; provider-kind fallback ×7; `:280` reads a key the ledger never writes | copies | **CONFIRMED, but the act_on_verdict copies are DEAD** | Blocks `:321-350` (act_on_verdict) ≡ `:594-623` (sweep) modulo `entry`/`snap_entry`, `instance_id`/`eid`. Fallback `entry.get("provider_kind") or entry.get("provider", …)` at `reaper.py:185,245,741` + `reaper_actor.py:321,337,596,610` (=7). `:280` `str(entry.get("provider_kind", ""))` — `Ledger.record` writes `"provider"` (`lifecycle.py:626`). **However** `act_on_verdict`'s only caller is `sweep()` `:670` after `partition()` by a Policy that is always `DEFAULT_APPLY_POLICY ∪ ⊆{ORPHAN_REAP, UNROUTABLE}` (`reaper.py:109-117`, `config.py:2256-2261`), and a drifted re-classify returns `"skipped"` before dispatch (`:258-265`) — so the HEARTBEAT_SUBSTRATE_MISSING (`:277-291`), SKIP_NO_PROBE (`:321-334`) and PROBE_FAILED (`:335-350`) arms are unreachable in production. The live WARN-once copies are the ones in `sweep()`. |
| ND #2 guarded-launch ×3 | generate path / `deploy` / `_cmd_provision` drifted | **CONFIRMED (+1 drift)** | Beyond B2/B13: `deploy` has no capacity-wait (`:2544` direct `create_instance` vs `:1536-1539`) and writes the real row after readiness rather than right after create (see B2 row). `_cmd_provision` is cross-area. |
| ND #3 `Lifecycle→classify` kwargs ×5 | `sweeper_thresholds_from_cfg` float-coerces + maps heartbeat 0→None, CLI copies do not | **PARTIAL (core side)** | `config.py:2264-2302` does exactly that (`float(...)`, `if lc.heartbeat_interval_s else None`). The four CLI copies are cross-area. |
| ND #4 `Lifecycle` defaults twice and disagreeing | interfaces.py `None`=disabled vs config.py 600/180/3600 enabled | **CONFIRMED, precise numbers, REACHABLE** | `interfaces.py:145-160`: `stall_window_s=None`, `restart_loop_window_s=None`, `ephemeral_orphan_age_s=None`, `heartbeat_interval_s=None`, `budget_usd=0.0`. `config.py:~119-140`: `stall_window_s=600.0`+`stall_reap_enabled=True`, `restart_loop_window_s=180.0`+enabled, `ephemeral_orphan_age_s=3600.0`+enabled, `budget: float` REQUIRED. `Config.lifecycle()` `:1964-1977` returns bare `InterfaceLifecycle()` when `_effective_lifecycle_config()` (`:1838-1846`) is None. `ComputeConfig.lifecycle: LifecycleConfig \| None = None` (`:1139`); no validator requires it (the checks at `validation/checks/lifecycle.py:35,85`, `heartbeat.py:27` all gate "iff a lifecycle block exists"). `examples/configs/extras/runpod-diffusers-seedvr2-3b-upscale.yaml` ships `compute:` (`:36`) with no `lifecycle:` → `kinoforge upscale -c` that file runs with stall/restart/orphan reaping OFF and `budget_usd=0.0`; adding `lifecycle: {budget: 1}` turns all three ON. The `3.0 *` dead-man literal: `reaper.py:755`, `heartbeat_loop.py:359`, `lifecycle.py:101`, `sweeper_metrics.py:81`, `cli/_commands.py:4038`, `validation/checks/lifecycle.py:27-53` (≥5 modules, confirmed). |
| ND #5 dead lifecycle layer | zero production callers | **CONFIRMED** | `rg 'LifecycleManager\|warm_reuse_or_create\|BudgetTracker\|effective_deadline\|_InstanceState' src/` outside `lifecycle.py` → only the `BudgetExceeded` catch arms (`batch.py:811`, `cli/_commands.py:2303`, `upscale_dir.py:69`) and RunPod selfterm's own unrelated `effective_deadline()` (`providers/runpod/selfterm.py:90`). `lifecycle.reap` (`:970`) is imported nowhere (`rg 'from kinoforge.core.lifecycle import'` → Ledger / destroy_confirmed / is_session_busy only). |
| ND #6 `errors.py` never-raised classes; `for_discovery` no-op | dead production surface | **CONFIRMED** | `SetStackRequestRejected:508`, `StageMismatch:614`, `BSACompileFailed:664`, `UnsupportedGpuArch:700` — each 1 src hit (the def), `raise X` → 0 in src; tests reference them (2/2/5/4). `errors.py` embedded: **yes** (21 config refs). `for_discovery`: see row above. |
| ND #7 function sizes + duplicated teardown pairs + inlined fetch | sizes, `:2861-2886 ≡ :3029-3056`, `:3165-3173` | **CONFIRMED** | `deploy_session` `:1733-2330` (~598), `generate` `:2619-3190` (~572), `_provision_instance_and_build_backend` `:1278-1690` (~412), `deploy` `:2338-2612` (~274). The two `except ValidationError / except (KeyboardInterrupt, Cancelled)` pairs are textually identical bar "during keyframe stage"/"during stages". `:3165-3173` re-implements `fetch_artifact_bytes` (`:239-262`: same UA, same 600 s timeout) differing only in the INFO log text. |
| ND #15 `core/locks.py` vs `stores/_lease.py` | real blocker is a cycle, not layering | **CONFIRMED** | `stores/_lease.py:22` `from kinoforge.core.locks import LockToken`; `tests/test_core_invariant.py:13-15` "Imports from kinoforge.stores are explicitly allowed"; `core/locks.py:21-24` imports only `clock` and `errors`. |
| ND #16 `balance_endpoints.py:35` second `TransportError` | `except KinoforgeError` misses it | **CONFIRMED** | `balance_endpoints.py:35` `class TransportError(Exception)` (docstring explains "NOT ValueError", not why not `KinoforgeError`); `errors.py:93` `class TransportError(KinoforgeError)`; `cli/_commands.py:4980,5002` imports and catches the balance one by name. |
| keep: "Duplicated provision branch" (re-evaluate) | blocks now identical apart from `for_discovery` | **CONFIRMED (one comment line aside)** | `diff <(sed -n 2088,2126p) <(sed -n 2135,2172p)` → two hunks only: the comment `# Caller pre-created the pod; marker-idempotent provision.` (`:2094`, cache-miss only) and `for_discovery=True`/`False`. With `for_discovery` unread the branches are semantically identical; the keep's "reconsider if … diverging" trigger has inverted. → E-6. |
| keep: `strategy.py:55-61` `_audio_mode` | inert seam, deliberate | **CONFIRMED** | `strategy.py:55-63` comment block; `config.py:454` pointer still accurate (`"core/strategy.py:55"`). |
| keep: `interfaces.py:83-90` vs `config.py:1010-1017` Placement defaults | duplicated but guarded | **CONFIRMED identical** | `interfaces.py:84-91` and `config.py:1010-1017`: `()`/`[]`, 1, 48, "12.8", 100, None, False, 2.20. |
| keep: `orchestrator.py` `ledger: Any  # noqa: ANN401` ×6 | duck typing | **CONFIRMED** | `:613`, `:753`, `:790`, `:836`, `:1294` + one more; contract spelled out at `:1367-1400`. |
| keep: `reaper.py:49-53` `Verdict.LAUNCHING` absent from policy | by design (U64) | **CONFIRMED** | `:49-53` comment; `act_on_verdict` has no LAUNCHING arm → `no_op`. |
| keep: `reaper.py:476-482` `_EPHEMERAL_GC_404_GRACE_S` pointer stale | points at `cli/_reconcile._LAUNCHING_GRACE_S` | **PARTIAL** | `cli/_reconcile.py:36` `_LAUNCHING_GRACE_S: float = 1800.0` DOES exist, so the pointer resolves; it is merely non-canonical — the owner is `launch_phase.LAUNCHING_GRACE_S` (`launch_phase.py:51`). Pointer swap is in D (docs). |
| keep: `pool.py:28-100` `_DaemonThreadPoolExecutor` | pinned by test | **CONFIRMED** | `pool.py:28-100` rationale; `tests/core/test_pool_exit_does_not_hang.py`, `test_pool_workers_daemon.py` exist. |
| keep: `heartbeat_loop.py:296`, `sweeper.py:294` broad per-tick `except` | "single bad tick must not kill the loop" | **CONFIRMED** | `heartbeat_loop.py:304` (`# noqa: BLE001 — single bad tick must not kill the loop`), `sweeper.py:286-296`. |
| keep: `lifecycle.py:909` `destroy_confirmed(clock=…)` reserved | unused param | **CONFIRMED** | `:909` `clock: Clock \| None = None,  # noqa: ARG001  (reserved for future use)`; body `:955-973` never reads it. |
| keep: `PreLaunchRateCapExceeded` stays in the provider | editing `errors.py` moves goldens | **CONFIRMED** | `orchestrator.py:862-866` `_nothing_booked_error_types` docstring explains the seam; `errors.py` embedded 21×. |

---

## B. New bugs (missed by the prior audit)

**B-1 (P3) `reaper_actor.py:552-554` — `sweep()` does not isolate an unregistered provider name on an EphemeralIndex row; the whole sweep aborts.**
`factory = registry_get_provider(row.provider); if factory is None: continue` — but the production callable is `registry.get_provider` (`cli/_commands.py:4792,5458`), which never returns `None`: it raises `UnknownAdapter` (`registry.py:90-105`). The `None` guard is dead and the real failure mode is unhandled, contradicting the docstring's "a single … exception demotes that provider's entries … but does not abort the sweep" (`:571-575`). The ledger-row path is isolated (`provider_for` `:131-141` catches); only the ephemeral path is not.
Scenario: an index row whose `provider` names no registered provider (hand-edited index, or a row written by a build whose provider was later renamed; all four shipped providers register unconditionally via `_adapters.py:47-50`, so this needs a foreign name). `kinoforge reap` then tracebacks before classifying ANY pod; `kinoforge sweeper` logs "sweep tick failed" every tick (`sweeper.py:294-296`, `errors_total++`) and sweeps nothing until the row is removed by hand.

**B-2 (P3, latent) `orchestrator.py:1917-1921` + `session_claim.py:105-134` — the `provision:<id>` lease TTL is shorter than the maximum hold, and a failed heartbeat closure turns a successful run into a 16-minute hang + failure.**
`_claim_ttl = boot_timeout_s + 2·heartbeat_interval_s` is used as BOTH the lease TTL and the post-yield poll timeout. The lease is taken right after `create_instance` (`_record_then_install` → `claim_holder.install`, `:1931-1950`) and held through `_wait_for_provider_ready` (≤ `boot_timeout_s`, `:1596-1604`) AND `engine.provision` (`:1626-1640`, whose own ready poll is bounded by `boot_timeout_s` again). On a Wan A14B cold boot (~30 min against the 900 s default) the lease expires mid-provision; the reaper's probe (`reaper_actor.py:174-178`, non-blocking acquire on an expired lease succeeds and releases) then reads "lock free". Benign TODAY: a row with no heartbeat keys classifies HEARTBEAT_UNKNOWN / HEARTBEAT_SUBSTRATE_MISSING (`reaper.py:745-753`), which no reachable policy acts on. It becomes a real hole the day a destructive verdict can reach a heartbeat-less row.
Second half: if the `start_heartbeat` closure raises (swallowed at `:1680-1690` → `hb_loop=None`) after the claim was installed, no tick ever lands, so after a SUCCESSFUL generation `claim_holder.__exit__` polls for `_claim_ttl` (960 s default) and raises `FirstTickTimeout` out of `deploy_session`; the artifact is already published, the command fails ~16 min later, and nothing in `orchestrator.py` catches `FirstTickTimeout` (rg → 0 handlers) although `session_claim.py:30-36` says the cold-path teardown "should catch this and destroy the orphaned instance". Narrow: needs `HeartbeatLoop(...)`/`Thread.start()` to raise (e.g. thread exhaustion).

**B-3 (P3) `reaper_actor.py:338-343` — production reaper passes the test-only `sleep=lambda _: None` to `destroy_confirmed`.**
`destroy_confirmed`'s own docstring (`lifecycle.py:923-924`) says "Pass `sleep=lambda _: None` in tests to skip real sleeps"; the reaper does it in production, so the 3 confirm polls run back-to-back. Masked on RunPod because `RunPodProvider.destroy_instance` has its own confirm loop (`providers/runpod/__init__.py:982-1011`); on a provider with an eventually-consistent listing it yields a spurious `action="failed"` + ERROR log + `errors_total++`, self-healing next sweep via STALE_LEDGER. Same shape the prior audit filed for `cli/_commands.py:4574`. (Also in the dead `reap()` `:1020` and `BudgetTracker.enforce` `:1146`.)

Specifically checked and found clean: `classify()` row ordering and every `None`-vs-`0` threshold (`stall_window_s`/`restart_loop_window_s` `None` = off, `0` → `_ephemeral_stall_predicate` returns False, `ephemeral_orphan_age_s <= 0` → off, `ephemeral_orphan_samples` validated ≥ 1); `_update_stall_history`'s `is None` (not falsiness) guard; `_enforce_rate_cap`'s `None`/`RATE_READBACK` matrix; `_create_with_capacity_wait` `<= 0` pass-through; `_wait_for_provider_ready` deadline; provisional-row forget/collapse ordering on both launch paths; `Ledger.touch` strict-update + protected keys; `destroy_confirmed` retry arithmetic; `InMemoryLock` steal-after-TTL; `is_session_busy`; `boot_liveness` precedence; `util_counter` reset rules; `ConcurrentPool` slot accounting and bounded shutdown.

---

## C. New hygiene findings

| # | location | category | behaviour-preserving? | risk | call | rationale |
|---|---|---|---|---|---|---|
| C-1 | `cost.py:51` | documentation | yes (docstring) | low | FIX NOW | "All 8 Verdict keys" — enum has 16. |
| C-2 | `config.py:2239-2241` `sweeper_policy_from_cfg` | documentation | yes | low | FIX NOW | Says policy starts as "(IDLE_REAP, OVERAGE_REAP, STALE_LEDGER)"; `DEFAULT_APPLY_POLICY` has 7 members. |
| C-3 | `lifecycle.py:865-866` `Ledger.touch` | documentation | yes | low | FIX NOW | "forward-compat — no reaper consumes it today": `classify` consumes `heartbeat_thread_tick` at `reaper.py:745-760`. |
| C-4 | `offers.py:1` | documentation | yes | low | FIX NOW | "applied by ComputeProvider.find_offers" — `find_offers` left the ABC in S4 (`interfaces.py:104-108`); only runpod/modal/local define it on themselves. |
| C-5 | `pool.py:4-7`, `:124-125` | documentation + dead | doc yes; class removal no (public) | low / med | FIX NOW (doc) / ND (class) | Module docstring presents `SequentialPool` as the impl and says concurrent variants "are DEFERRED"; `ConcurrentPool` is in the same file and is what `deploy_session` builds (`orchestrator.py:2199`). `SequentialPool` has zero production callers (only two docstring mentions). |
| C-6 | `reaper_actor.py:277-291, 321-350` | dead | yes (unreachable arms) | med | NEEDS DISCUSSION | Three `act_on_verdict` arms unreachable with any Policy the CLI/daemon can build (see A row "WARN-once ×2"); `tests/core/test_reaper_actor.py` exercises them directly, so deletion takes tests with it. Public function → operator call. |
| C-7 | `orchestrator.py:353,381-382` `_provision_compute_once(store=)` | dead parameter | signature change (private fn, sole caller `:1626-1640`; the only test fake takes `**kwargs`, `test_rate_cap_enforcement.py:177`) | low | docstring FIX NOW; param removal ND | Parameter never read; docstring describes a lock the body no longer takes. |
| C-8 | `session_claim.py:30-36` `FirstTickTimeout` | documentation | yes | low | NEEDS DISCUSSION | Docstring describes an orchestrator handler that does not exist (B-2); fix is either the doc or the handler. |
| C-9 | `interfaces.py:118-160` `Lifecycle` docstring | documentation | yes | low | LEAVE | Attributes block documents 2 of 18 fields; the rest carry inline comments. |
| C-10 | `reaper.py` `_EPHEMERAL_GC_404_GRACE_S` comment + `_ephemeral_pre_create_grace` docstring | documentation | yes | low | FIX NOW | Cites `cli/_reconcile._LAUNCHING_GRACE_S` (exists, non-canonical); owner is `launch_phase.LAUNCHING_GRACE_S`. From hygiene-notes. |
| C-11 | `orchestrator.py:2947,2969,2993` | dead | yes (same module object) | low | FIX NOW | See A. |
| C-12 | `config.py:66` | dead | yes | low | FIX NOW | See A. |
| C-13 | `session_claim.py:106-109` | dead | yes | low | FIX NOW | See A. |
| C-14 | `orchestrator.py:426`, `config.py:2200`, `interfaces.py:531`, `pool.py:67` | dead (noqa) | yes | low | FIX NOW | Proven on scratch copies. |
| C-15 | `orchestrator.py:2544` `deploy()` | redundant / drift | n/a | high | NEEDS DISCUSSION | `deploy` lacks the capacity-wait the generate path has (`:1536-1539`) — fourth drift of ND #2 besides rate cap, diagnostic env and real-row timing. |
| C-16 | `orchestrator.py:3165-3173` | redundant | **no** — log text differs ("materializing interpolated artifact from" vs `fetch_artifact_bytes`' "materializing artifact from") | low | NEEDS DISCUSSION | Hand-inlined copy of `fetch_artifact_bytes` (`:239-262`). |
| C-17 | `orchestrator.py:2312` vs `reaper_actor.py:40` | redundant (magic) | yes if a shared constant is introduced | low | NEEDS DISCUSSION | `_LOCK_TTL_S` is private to another module; importing it or promoting it to a public name is a small design choice. |
| C-18 | `reaper.py:~470-497`, `launch_phase.py:51`, `cli/_reconcile.py:36` | magic | n/a | low | LEAVE | `1800.0` ×3 — the reaper comment documents the deliberate non-configurability; `tests/core/test_reaper_launching_rows.py:189` guards `_reconcile` parity. |
| C-19 | `provisioner.py:1-15, 99-106` | documentation | yes | low | LEAVE | Docstring step 2/3 mentions only `requires_local_weights`; the `refs_to_stage` precedence is explained in the inline comment `:143-152`. |
| C-20 | `lifecycle.py:135-391, 970-1154` | dead | yes (zero callers) | med | NEEDS DISCUSSION | ND #5 — ~470 LOC + 8 tests; deleting also orphans `BudgetExceeded` and the example-config comments that claim "BudgetTracker tears down mid-run". |
| C-21 | `errors.py:508,614,664,700` | dead | yes, but **embedded: yes** → moves 20+ goldens | high (goldens) | NEEDS DISCUSSION | Batch with the next deliberate golden move. |

---

## D. FIX NOW edit list

All items below: **embedded: no** (none of these files appears in `examples/configs`' `embed_*` lists — see the fact at the top).

### D.1 Code items

**D-code-1 — `src/kinoforge/core/session_claim.py:105-110` — delete the no-op `try/except`.**
old (verbatim):
```
    with store.acquire_lock(f"provision:{instance_id}", ttl_s=ttl_s):
        start = _clock.now()
        try:
            yield
        except BaseException:
            raise
        deadline = start + timeout_s
```
new:
```
    with store.acquire_lock(f"provision:{instance_id}", ttl_s=ttl_s):
        start = _clock.now()
        yield
        deadline = start + timeout_s
```
- Fence: `git log -S'except BaseException:' -- src/kinoforge/core/session_claim.py` → `6a42d0ec feat(b7): session_claim helper + FirstTickTimeout` (introducing commit; no reason beyond docstring item 5, which a bare `yield` already satisfies under `@contextmanager` — `gen.throw` re-raises at the yield either way, including `GeneratorExit`).
- rg proof: `rg -c 'except BaseException' tests/core/test_session_claim.py` → 0.
- Tests: `tests/core/test_session_claim.py`, `tests/core/test_orchestrator_compute.py` (B7 claim tests).

**D-code-2 — `src/kinoforge/core/config.py:66` — delete the unreachable dict entry.**
old (verbatim, one line): `    "lora": {"loras"},`
new: (line removed; dict keeps base/vae/text_encoder/clip_vision)
- Fence: `git log -S'"lora": {"loras"}' -- src/kinoforge/core/config.py` → `6bf6b940 feat: add validated pydantic config model …` (original dict). `ModelEntry.kind` is `Literal["base","vae","text_encoder","clip_vision"]` (`config.py:973`) since P1 (`:965`), and legacy `kind: lora` entries are promoted out of `models` by the `mode="before"` validator `:1615-1635`, so `VALID_KIND_TARGETS.get("lora")` at `:1735` can never be evaluated.
- rg proof: `rg -n 'VALID_KIND_TARGETS' tests/` → 0; `rg -n 'VALID_KIND_TARGETS' src/ tools/` → `config.py:64`, `:1735` only.
- Tests: `tests/core/test_config.py`, `tests/core/test_config_loras_migration.py`.

**D-code-3 — `src/kinoforge/core/orchestrator.py:2947, 2951, 2969, 2972, 2993, 2996` — drop the three local `registry` re-imports.**
Three edits, each: delete the line `            from kinoforge.core import registry as _registry` and change the following use:
- `:2951` `            upscaler_engine = _registry.get_upscaler(cfg.upscale.engine)()` → `            upscaler_engine = registry.get_upscaler(cfg.upscale.engine)()`
- `:2972` `            text_engine = _registry.get_text_engine(cfg.text.engine)()` → `            text_engine = registry.get_text_engine(cfg.text.engine)()`
- `:2996` `            interp_engine = _registry.get_interpolator(cfg.interpolate.engine)()` → `            interp_engine = registry.get_interpolator(cfg.interpolate.engine)()`
Leave the neighbouring lazy pipeline imports (`ScaleTarget`, `UpscaleStage`, `TextStage`, `InterpolateStage`) in place — those are deliberate deferred imports.
- Fence: `git log -S'registry as _registry' -- src/kinoforge/core/orchestrator.py` → `acbe0ed2 feat(upscale): … orchestrator wires UpscaleStage …`, `05a21b8e feat(orchestrator): append InterpolateStage …` (feature commits, no stated reason; the module-level `from kinoforge.core import registry` at `:32` predates both).
- rg proof: `rg -n 'orchestrator\._registry|_registry\.get_' tests/` → 0 (the 9 `\b_registry\b` hits in `tests/core` are `test_reaper_sweep.py`'s `_registry()` fixture and `test_ledger_touch.py`'s `self._registry` — unrelated). Monkeypatches of `kinoforge.core.registry.get_upscaler` etc. keep working (attribute lookup on the same module object at call time).
- Tests: `tests/core/test_orchestrator_upscale_chunk.py`, `tests/core/test_orchestrator_text.py`, `tests/core/test_orchestrator_interpolate.py`.

**D-code-4 — four never-firing `noqa` directives (RUF100 "unused").**
1. `orchestrator.py:426` old `            def model_dump(self) -> dict[str, object]:  # noqa: D102` → new `            def model_dump(self) -> dict[str, object]:`
2. `config.py:2200` old `    import kinoforge._adapters  # noqa: F401 — self-register every adapter (incl. provider checks)` → new `    import kinoforge._adapters  # self-register every adapter (incl. provider checks)` (keep the intent text; `:2201`'s F401 noqa IS used — leave it).
3. `interfaces.py:531` old `    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:  # noqa: B027` → new `    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:`
4. `pool.py:67` old `    def _adjust_thread_count(self) -> None:  # noqa: D401, D102` → new `    def _adjust_thread_count(self) -> None:  # noqa: D401`
- Proof: `pixi run python -m ruff check --extend-select RUF100 --output-format concise src/kinoforge/core/` lists exactly these as `unused:`; scratch copies with the four edits pass `ruff check --config pyproject.toml --select D102,F401,B027` → "All checks passed!". Do NOT touch the ~44 `non-enabled:` hits (BLE001/SLF001/PLC0415/ARG001/TC001) — hygiene-notes keep.
- rg proof: nothing in `tests/` asserts on noqa text.
- Tests: `tests/core/test_pool*.py`, `tests/core/test_config.py` (sanity only); pre-commit's ruff pass is the real check.

### D.2 Docs-only items (docstrings/comments; no runtime text)

**D-doc-1 — `orchestrator.py:1168-1172`** (`_build_start_heartbeat_closure`), old:
```
    passed into :func:`_provision_instance_and_build_backend` and invoked
    right after the RunPod-status poll succeeds, so the loop ticks throughout
    ``engine.provision`` / ``wait_for_ready`` rather than waiting until
    ``deploy_session`` resumes after provision returns. Steady-state lifetime
```
new:
```
    passed into :func:`_provision_instance_and_build_backend` and invoked
    AFTER ``engine.provision`` returns (C33-m, 2026-06-17: the C29
    before-provision order was reverted because the RunPod heartbeat write
    restarted the container mid-boot), so the loop covers the steady state
    only. Steady-state lifetime
```

**D-doc-2 — `orchestrator.py:1336-1338`** (`start_heartbeat` param doc), old:
```
            ``HeartbeatLoop`` given the just-readied ``Instance``. Invoked
            right after the RunPod status poll succeeds and BEFORE
            ``engine.provision`` runs, so STALL_REAP / RESTART_LOOP_REAP
            predicates tick throughout the boot phase. ``None`` skips the
```
new:
```
            ``HeartbeatLoop`` given the just-readied ``Instance``. Invoked
            AFTER ``engine.provision`` returns (C33-m), so STALL_REAP /
            RESTART_LOOP_REAP predicates tick post-boot only. ``None`` skips the
```

**D-doc-3 — `orchestrator.py:1999-2002`**, old:
```
    # The closure is invoked from inside _provision_instance_and_build_backend
    # right after the RunPod-status poll succeeds (cold-start branches), so
    # the HeartbeatLoop ticks throughout engine.provision / wait_for_ready
    # instead of waiting until deploy_session resumes after provision returns.
```
new:
```
    # The closure is invoked from inside _provision_instance_and_build_backend
    # AFTER engine.provision returns (cold-start branches; C33-m reverted the
    # C29 before-provision order), so the HeartbeatLoop covers the steady
    # state only.
```

**D-doc-4 — `orchestrator.py:2209-2214`**, old:
```
            # branch. Cold-start branches above already populated ``hb_loop``
            # by invoking ``_start_heartbeat`` inside
            # ``_provision_instance_and_build_backend`` (right after the
            # RunPod-status poll succeeded). Caller-supplied pods skipped that
            # path — they have no boot phase to protect — so the loop is
            # constructed here at the original pre-C29 location.
```
new:
```
            # branch. Cold-start branches above already populated ``hb_loop``
            # by invoking ``_start_heartbeat`` inside
            # ``_provision_instance_and_build_backend`` (after provision
            # returned, C33-m). Caller-supplied pods skipped that path, so the
            # loop is constructed here at the original pre-C29 location.
```

**D-doc-5 — `orchestrator.py:176`**, old `    # Both call sites (`deploy_session`, `generate`) call `cfg.capability_key()`` → new `    # All three call sites (`deploy`, `deploy_session`, `generate`) call `cfg.capability_key()``.

**D-doc-6 — `orchestrator.py:589`**, old `    compute-seam S5. Only :data:`~kinoforge.core.lifecycle.LAUNCH_PHASE_TAG` is` → new `    compute-seam S5. Only :data:`~kinoforge.core.launch_phase.LAUNCH_PHASE_TAG` is`.

**D-doc-7 — `orchestrator.py:638-640`**, old:
```
    Modal the app run id. Task 6 teaches ``cli/_reconcile`` to adopt a
    ``launching`` row by name before it is allowed to forget one; until then
    the row is durable but the reconciler cannot yet act on it.
```
new:
```
    Modal the app run id. ``cli/_reconcile._adopt_or_age_out`` adopts a
    ``launching`` row by name before it is allowed to forget one.
```

**D-doc-8 — `orchestrator.py:1311-1313`** (`for_discovery`), old:
```
        for_discovery: When True, the CapacityError message reads
            'no offers available for discovery from provider ...' to
            distinguish the cold-start failure from a steady-state one.
```
new:
```
        for_discovery: Currently unread — the CapacityError message it used
            to select was removed with the S4 provider-side selection
            (``4fe46014``). Kept so both ``deploy_session`` call sites keep
            their keyword; see hygiene-notes "Duplicated provision branch".
```

**D-doc-9 — `orchestrator.py:1331-1332`**, old:
```
            ``Cancelled`` from inside ``engine.wait_for_ready``. Task 5 adds
            the matching ``except Cancelled`` clause that destroys the pod.
```
new:
```
            ``Cancelled`` from inside ``engine.wait_for_ready``; the matching
            ``except Cancelled`` clause below destroys the pod.
```

**D-doc-10 — `orchestrator.py:361-364` and `:381-382`** (`_provision_compute_once`), old:
```
    Layer I UX A compute-path preflight.  Uses an artifact-store lock keyed on
    the instance id to serialise concurrent ``generate()`` callers, and reads
    a per-instance marker to skip the (potentially expensive) provision when
    the same instance was already provisioned for the same capability key.
```
new:
```
    Layer I UX A compute-path preflight.  Serialisation is owned by the
    ``provision:<id>`` lock that ``deploy_session`` holds around this call
    (B7 ``hold_until_first_tick``); this helper only reads a per-instance
    marker to skip the (potentially expensive) provision when the same
    instance was already provisioned for the same capability key.
```
and old:
```
        store: Artifact store providing ``acquire_lock`` for cross-process
            mutual exclusion.
```
new:
```
        store: Unused since the lock moved to ``deploy_session``
            (``d6932465``); retained for call-site compatibility.
```

**D-doc-11 — `orchestrator.py:1708-1710`**, old:
```
    On clean exit the pool is closed but the instance is left alive for
    warm reuse — destruction is the sweeper / budget tracker's job,
    matching the behaviour of the pre-refactor :func:`generate`.
```
new:
```
    On clean exit the pool is closed and, unless ``single=True`` destroyed
    it in the ``finally``, the instance is left alive for warm reuse —
    destruction is then the sweeper's / ``kinoforge reap``'s job.
```
and `:1782-1784`, old:
```
    * Does NOT call ``provider.destroy_instance`` — the instance is left
      alive for warm reuse by the next session or for the
      sweeper / budget tracker to reap.
```
new:
```
    * Does NOT call ``provider.destroy_instance`` unless ``single=True``
      (``--no-reuse``) — otherwise the instance is left alive for warm
      reuse by the next session or for the sweeper / ``kinoforge reap``.
```

**D-doc-12 — `orchestrator.py:2752-2753`**, old:
```
    # expectations. Mirrors the precedent at _provision_compute_once
    # (line 209). Drift-locked by
```
new:
```
    # expectations. Mirrors the precedent in _provision_compute_once.
    # Drift-locked by
```
and `:2835`, old `            # block above (line ~1008). Narrow so mypy can resolve attribute` → new `            # block above. Narrow so mypy can resolve attribute`.

**D-doc-13 — `orchestrator.py:2655-2656`** (generate docstring step 5), old:
```
    5. Build ``stages: list[Stage]`` — ``[KeyframeStage, GenerateClipStage]`` when
       ``cfg.keyframe`` is set, else ``[GenerateClipStage]``.
```
new:
```
    5. Build ``stages: list[Stage]`` — ``[KeyframeStage, GenerateClipStage]`` when
       ``cfg.keyframe`` is set, else ``[GenerateClipStage]``; ``UpscaleStage`` /
       ``TextStage`` / ``InterpolateStage`` are appended when their cfg blocks
       are set (``skip_clip_stage=True`` drops the clip stage).
```
(Controller: confirm the exact two lines with `sed -n '2655,2656p'` — the step list sits at `:2647-2661`.)

**D-doc-14 — `reaper.py:662-664`**, old:
```
        One of the seven non-UNROUTABLE Verdict values:
        LIVE, IDLE_REAP, ORPHAN_REAP, OVERAGE_REAP, STALE_LEDGER,
        HEARTBEAT_UNKNOWN, or HEARTBEAT_SUBSTRATE_MISSING. UNROUTABLE
```
new:
```
        Any :class:`Verdict` except UNROUTABLE and POD_GONE (the latter is
        only ever produced by ``HeartbeatLoop``); the ephemeral branch adds
        GC_404 / SKIP_NO_PROBE / PROBE_FAILED. UNROUTABLE
```

**D-doc-15 — `reaper_actor.py:81-83`** (`ActionResult.action`), old:
```
            * ``"destroyed_and_forgot"`` — IDLE_REAP / OVERAGE_REAP /
              ORPHAN_REAP destroyed + ledger entry forgotten.
            * ``"forgot"`` — STALE_LEDGER: entry forgotten, no destroy.
```
new:
```
            * ``"destroyed_and_forgot"`` — IDLE_REAP / OVERAGE_REAP /
              ORPHAN_REAP / STALL_REAP destroyed + ledger entry forgotten.
            * ``"forgot"`` — STALE_LEDGER: entry forgotten, no destroy.
            * ``"gc_404_removed"`` — GC_404: ephemeral index row removed,
              no destroy, no ledger write.
            * ``"probe_failed"`` — PROBE_FAILED: WARN-once, retried next tick.
```

**D-doc-16 — `heartbeat_loop.py:252,254`**, old:
```
            # SKIPS writing the field (lifecycle.py:651), the row stays
            # without ``last_heartbeat``, and ``reaper.classify``
            # (reaper.py:334) returns ``HEARTBEAT_UNKNOWN`` — defeating
```
new:
```
            # SKIPS writing the field (``Ledger.touch`` drops ``None``
            # values), the row stays without ``last_heartbeat``, and
            # ``reaper.classify`` (row 7) returns ``HEARTBEAT_UNKNOWN`` — defeating
```

**D-doc-17 — `reaper.py:738`**, old `    # provider kind under the key ``"provider"`` (lifecycle.py:504);` → new `    # provider kind under the key ``"provider"`` (``Ledger.record``);`.
`reaper.py:786`, old `    # and documented (interfaces.py:65) as the POST-SESSION warm-reuse window.` → new `    # and documented (``Lifecycle.grace_after_session_s``) as the POST-SESSION warm-reuse window.`

**D-doc-18 — `reaper.py` `_EPHEMERAL_GC_404_GRACE_S` block (~`:470-497`)** — two substrings: `` ``cli/_reconcile._LAUNCHING_GRACE_S`` `` (in the `#:` comment above the constant and in the last sentence of `_ephemeral_pre_create_grace`'s docstring) → `` :data:`kinoforge.core.launch_phase.LAUNCHING_GRACE_S` `` (the canonical owner; `cli/_reconcile` keeps a guarded copy). Controller: `rg -n '_reconcile._LAUNCHING_GRACE_S' src/kinoforge/core/reaper.py` → exactly 2 hits.

**D-doc-19 — `cost.py:51`**, old `        pod_counts_by_verdict: All 8 Verdict keys; zeros included for` → new `        pod_counts_by_verdict: Every Verdict member; zeros included for`.

**D-doc-20 — `config.py:2239-2241`**, old:
```
    Starts with Layer V :data:`DEFAULT_APPLY_POLICY` (IDLE_REAP,
    OVERAGE_REAP, STALE_LEDGER) and unions the two opt-in verdicts based
    on YAML flags.
```
new:
```
    Starts with Layer V :data:`DEFAULT_APPLY_POLICY` and unions the two
    opt-in verdicts (ORPHAN_REAP, UNROUTABLE) based on YAML flags.
```

**D-doc-21 — `lifecycle.py:865-866`**, old:
```
        Sentinel-gate contract (forward-compat — no reaper consumes it
        today): code that consults ``last_heartbeat`` for a reaping or
```
new:
```
        Sentinel-gate contract (consumed by ``reaper.classify`` rows 3-6):
        code that consults ``last_heartbeat`` for a reaping or
```

**D-doc-22 — `offers.py:1`**, old `"""Pure offer-filtering helper applied by ComputeProvider.find_offers."""` → new `"""Pure offer-filtering helper the enumerating providers apply inside their own catalog reads (compute-seam S4: ``find_offers`` is no longer on the ABC)."""`

**D-doc-23 — `pool.py:4-7`**, old:
```
never on a concrete engine. SequentialPool is the trivial impl: it runs jobs
through the single registered backend inline and wraps the result in an
already-resolved Future so the contract is identical to what a future concurrent
pool exposes.
```
new:
```
never on a concrete engine. ConcurrentPool is what deploy_session builds;
SequentialPool is the trivial reference impl: it runs jobs through the single
registered backend inline and wraps the result in an already-resolved Future
so the contract is identical to ConcurrentPool's.
```
and `:124-125`, old:
```
    This is the reference implementation of :class:`BackendPool`; concurrent
    and multi-backend variants are DEFERRED.
```
new:
```
    This is the reference implementation of :class:`BackendPool`;
    :class:`ConcurrentPool` below is the production pool.
```

rg proofs for the docs batch: `rg -n 'seven non-UNROUTABLE|All 8 Verdict|ticks throughout|no reaper consumes|applied by ComputeProvider.find_offers' src tests docs --glob '!docs/hygiene-audit-*.md'` → outside the edited lines only `docs/superpowers/plans/*.md` (historical plans) and one comment in `tests/live/test_c29_phase_c_boot_status_live.py:193` — no assertion. Tests to run after the docs batch (import-level sanity): `tests/core/test_reaper.py`, `test_reaper_actor.py`, `test_cost.py`, `test_heartbeat_loop.py`, `test_lifecycle.py`, `test_pool.py`, `test_config.py`, `tests/core/test_orchestrator_c33_start_heartbeat_after_provision.py`.

---

## E. Needs discussion

1. **B17 denominator (money/teardown).** Ephemeral STALL_REAP is unreachable with the shipped defaults and with `examples/configs/sweeper.yaml`. The sweeper banks one sample per `sweeper.interval_s`; `classify` demands `ceil(window / heartbeat_interval_s)` where ephemeral pods have no heartbeat at all. Decide: pass the sweeper interval as the ephemeral denominator (sweeper-side, no classify API change) or bank samples on the heartbeat cadence. Also decide whether `_update_stall_history`'s maxlen should be derived from the same expression.
2. **ND #4 — which `Lifecycle` default is intended.** A compute cfg without `lifecycle:` (reachable: `extras/runpod-diffusers-seedvr2-3b-upscale.yaml`) runs with stall/restart/orphan reaping OFF and `budget_usd=0.0`; adding any `lifecycle:` block flips the three ON (600/180/3600). Options: make `interfaces.Lifecycle` defaults match `LifecycleConfig`; or add a STATIC check requiring `lifecycle:` on compute cfgs; or document the asymmetry. Add a parity test either way (Placement has one, Lifecycle does not).
3. **B6/B7 verdict tables.** Add STALL_REAP / RESTART_LOOP_REAP / DEGRADED_REAP to `_BURNING_VERDICTS` (changes `kinoforge cost` output) and RESTART_LOOP_REAP / DEGRADED_REAP to `act_on_verdict`'s destroy set (or drop them from `DEFAULT_APPLY_POLICY`). Both are behaviour changes on the money path.
4. **ND #2 — one guarded-launch helper.** `deploy` now differs from the generate path in four ways (rate cap, diagnostic env, capacity-wait, real-row timing). High risk; pairs with B2/B13.
5. **Duplicated provision branch keep.** Branches are identical bar one comment and the unread `for_discovery`. Either delete the parameter and extract a 20-line helper, or keep and update the note (D-doc-8 keeps the doc honest meanwhile).
6. **`act_on_verdict` dead arms (C-6).** Delete `:277-291, 321-350` (+ their direct tests) or keep as defensive dispatch on a public function. If kept, fix `:280` to the `or entry.get("provider", "")` shape used everywhere else (log text; not FIX NOW).
7. **`_provision_compute_once(store=)` removal (C-7)** — private signature, one caller, one `**kwargs` fake.
8. **Interpolate materialize → `fetch_artifact_bytes` (C-16)** — identical bytes/timeout/UA; only the INFO line changes.
9. **`ttl_s=30.0` → shared constant (C-17)** — promote `_LOCK_TTL_S` to a public name in `reaper_actor` or `locks`.
10. **ND #16 `TransportError` twins** — fold `balance_endpoints.TransportError` into `errors.TransportError` (embedded file → goldens) or alias.
11. **ND #15 `_LeaseLockBase` relocation** into `core/locks.py` (non-inverting; stores re-export).
12. **ND #5 dead lifecycle layer** — ~470 LOC + 8 tests; also decide the fate of `BudgetExceeded` and the config comments claiming BudgetTracker.
13. **`errors.py` never-raised classes (C-21)** — embedded; batch with the next intentional golden regeneration.
14. **B-1 `sweep()` isolation** — wrap the ephemeral `registry_get_provider(row.provider)` in the same try/except `provider_for` uses (drop the dead `is None` guard).
15. **B-2 claim-lease arithmetic + `FirstTickTimeout`** — size `_claim_ttl` from the full hold (ready poll + provision) or re-acquire after provision; decide whether the orchestrator should catch `FirstTickTimeout` as `session_claim.py:30-36` promises (then C-8 resolves itself).
16. **B-3 zero-sleep `destroy_confirmed` in the reaper** — pass `time.sleep` (adds ≤1 s per failed confirm) or document the RunPod-side loop as the reason.
17. **`SequentialPool` (C-5)** — public class with zero production callers; keep as reference impl or delete with its tests.

---

## F. Cross-area notes

- `cli/_commands.py:535-604` `_cmd_provision` ("reuses deploy's mechanism verbatim") — B2/B13/capacity-wait drift presumably mirrored there; not verified in this pass.
- `cli/_commands.py:2590-2604, 3750-3764, 4755-4777, 5099-5142` — the four CLI `Lifecycle→classify` copies (ND #3) not verified here; `core/config.py:2264-2302` is the only one I checked.
- `cli/_commands.py:4038` and `validation/checks/lifecycle.py:27-53` carry the `3 *` dead-man literal too (ND #4 sub-claim: ≥5 modules confirmed from core alone).
- `cli/_reconcile.py:36` `_LAUNCHING_GRACE_S = 1800.0` still exists — the hygiene-notes keep's parity reason is intact; only the reaper's pointer is non-canonical (D-doc-18).
- `providers/runpod/__init__.py:982-1011` `destroy_instance` has its own 3-poll confirm loop — relevant to the prior P3 on `cli/_commands.py:4574` (zero-sleep `destroy_confirmed`): on RunPod the CLI/reaper zero-sleep is masked by the provider's loop.
- `engines/hosted/__init__.py:513-516` `_resolve_auth` branch 2 — core side confirmed (no `auth` field, `extra="forbid"`); engine side for the engines auditor.
- `docs/superpowers/plans/2026-06-12-b5a-heartbeat-substrate.md:1909`, `…b2-cost-dashboard.md:823,1132,1265,1436`, `…c29-heartbeat-earlier.md:849`, `…2026-05-29-kinoforge.md:206,872`, `…layer-u-heartbeat-persistence.md:136` repeat the same stale "seven verdicts" / "All 8 Verdict" / "ticks throughout" / `find_offers` text — historical plans, leave.
- `tests/live/test_c29_phase_c_boot_status_live.py:193` comment "the heartbeat ticks throughout" is stale after C33-m (live test; cannot run offline).
- `tests/core/test_reaper_actor.py` exercises `act_on_verdict` arms that production cannot reach (C-6); if E-6 deletes them, those tests go too.
- `tests/live/_c33_probe_m_evidence.json:56` cites `orchestrator.py:719-736` for the old heartbeat-start site — evidence file, leave.
