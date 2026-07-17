# Hygiene audit — 2026-07-16 (whole repo, AUDIT-ONLY)

> **Un-red pass executed same day (operator-approved):** Finding #0 fully resolved —
> suite green (4070 passed, 0 failed). Commits: `95f10eb` (pollution), `b314f67`
> (AC8 scanner Load-ctx), `366b95b` (client ref registration), `f007391` (fixture
> teardown), port+delete of stale set_stack files, `6a32535` (matrix submit+poll
> migration). Note: the AC8 wan_t2v_server resolution took the SCANNER-PRECISION
> route, not the exempt tag — the tag budget (max 1 src file,
> `test_ac8_exempt_tag_count_is_audit_friendly`) was already spent by
> `core/grid/executor.py`.
>
> **New finding from the port (add to NEEDS DISCUSSION):** the in-job LRU-evict
> branch (`wan_t2v_server.py:1738-1740`) is unreachable by set algebra —
> `mandatory_evict = current − target` removes every non-target key first, so the
> LRU candidate pool `set(_inventory) − target_keys` is always empty and
> `_pick_lru_evict` can only return None (→ the 507 backstop). Either the LRU
> sub-branch is dead code to delete, or the intended design was to LRU-evict
> BEFORE mandatory eviction ordering constraints — decide, then delete or fix.
> The ported tight-disk test pins the reachable behavior (mandatory evict funds
> the download).
>
> **FIX NOW batch executed same day (operator-approved, 26 commits
> `bd8817d`..`66a65ea`, suite 4071 passed / 0 failed after):** every item in the
> "FIX NOW hygiene" section below is DONE — docstring corrections, dead-code
> drops (SetStackResponse + parity-test retarget, gc --older-than, skypilot
> vestigials, unused server imports), dedups (_branch_error_to_http,
> _resolve_transformer_attr, runpod _transport + read_util→probe, _urllib_delete,
> fal resolve_prompt, outputs format_filename base, validation _head +
> _run_gated, cli warm-attach ladder + cold-create stamp, wait_for_ready
> poll_until_ready, lock _LeaseLockBase, pod-HTTP _pod_http), structural
> decompositions (_run_swap_job 281→67, _create_pod 222→39, _run_swap_group
> nesting 5→3), last_heartbeat on the ABC, cfg.lifecycle() knob unification,
> modal 0.0 created_at sentinel, leak-sweep gh returncode logging. Two audit
> corrections surfaced during execution: (a) the AC8 exempt-tag route was
> blocked by the 1-file tag budget — resolved via scanner Load-ctx precision
> instead; (b) the warm-attach duplication was generate↔batch, NOT
> generate↔upscale (upscale/interpolate run a deliberately different chain).
> New follow-ups recorded: SwapRejectedDetails now producer-less in the server;
> core/locks.py in-memory lock shares the lease shape but a core→stores import
> would invert layering.

Scope: whole repo. Mode: audit-only — **no code changed**. Five parallel read-only audit
agents (engines / providers+core / cli+pipeline+rest / tests / tools+docs); every file:line
verified by the reporting agent; the two highest-stakes claims (graphifyy dep, capability-prefix
mismatch) re-verified by the controller.

Baseline: `pixi run pre-commit run --all-files` **green**; working tree clean at `0ac6a8e`.

---

## Finding #0 — test suite RED on HEAD (pre-existing; PROGRESS "main green" is stale)

`pixi run test`: **14 failed**, 4057 passed. Three independent causes:

1. **Stale shadow contract (9–10 tests, NONDETERMINISTIC).**
   `tests/engines/test_wan_t2v_server_set_stack.py` + `..._failures.py` +
   `tests/smoke/local_cpu/test_lora_swap_matrix.py::test_vram_oom_rollback_restores_previous_stack`
   still call the synchronous `set_stack` contract retired by the job migration (`8d88e0b`,
   2026-07-13). Files last touched at `7e0ef8d` (pre-migration). Worse: `asyncio.run(s.set_stack(req))`
   orphans the `create_task(_run_swap_job)`, which partially executes during loop teardown —
   two suite runs fail *different subsets*. Flaky red poisons all future triage.
   New job-based tests exist (`tests/engines/diffusers/servers/test_set_stack_async_job.py`,
   `test_set_stack_swap_gaps.py`) but do NOT cover everything — see coverage table below.
   **Old files are NOT wholesale deletable; port first.**

2. **AC8 redaction invariant (1 test) — scanner FALSE POSITIVE, but real gap in scanner + one belt-and-braces fix.**
   `test_ac8_inventory_readers_register_observed_refs` trips on:
   - `wan_t2v_server.py` — `_swap_jobs[job_id]["inventory"] = ...` (:1906/:1917) is a *Store*-context
     subscript; `_reads_lora_inventory` (test_no_unredacted_writes.py:440-445) matches `ast.Subscript`
     without checking Load/Store ctx. Pod-side file cannot import the redaction helper (stdlib+fastapi
     only). Fix: `# kinoforge:lora-redact-exempt` tag with pod-side justification (ref-logging lines
     pre-date the migration).
   - `engines/diffusers/__init__.py` — `data.get("inventory")` in new `_poll_set_stack` (:772,
     `589cd21`). Refs returned opaquely, registered at render boundary (integration.py:127,
     cli/_commands.py:1295). No leak. Cleanest fix: call `_register_observed_lora_refs` inside
     `_poll_set_stack` (client-side, CAN import).

3. **Cross-test pollution (3 tests, order-dependent).** Root cause verified with a 2-file repro:
   `tests/engines/test_resolve_transformer.py:200-205` (added `14fa285`) does a mid-file top-level
   `from ...wan_t2v_server import BranchAutoNotAllowedOnMoE, ..., _check_branch_legal` — violating
   the file's OWN header rule (lines 15-21) forbidding exactly this. Any `importlib.reload(srv)`
   elsewhere (`test_diffusers_wan_t2v_server.py` ×6, `diffusers/test_server_upscale.py:43`)
   re-executes the module in-namespace → captured function's `__globals__` hold NEW exception
   classes while `pytest.raises` holds OLD identities. Fix: live-attribute access
   (`wan_t2v_server.X`) like the rest of the file. Systemic hazard: the reload pattern will
   silently claim the next file that top-level-imports any name from that module.

---

## Bugs (separate from hygiene; audit-only — none fixed)

### P1 — cost money or corrupt output

| # | Location | Bug |
|---|----------|-----|
| B1 | `wan_t2v_server.py:439` vs `:1356` | **Warm-reuse silently defeated** (verified). `_register_eager_wan` names the pipe `wan-eager-{MODEL_ID}`; `_capability_for_model` maps only `wan-t2v-*` → `t2v` never advertised in `/health` capabilities → `_health_preflight_ok` (`cli/_commands.py:2186`) refuses every warm-attach → cold-boot cost every run. Tests mask it by seeding `_LOADED["wan-t2v-a14b-fp8"]` directly (`tests/engines/diffusers/test_server_health.py:68`). |
| B2 | `wan_t2v_server.py:485-492` | **Silent LoRA-less generation.** `_promote_wan_if_evicted` reloads a disk-dropped Wan via bare `_load_pipeline()` (no LoRA stack) while `_inventory` still lists adapters → post-evict generation runs inert-LoRA while `/lora/inventory` + warm-attach matcher claim the stack is active. Same failure class frame-QA rules exist to catch. |
| B3 | `providers/runpod/__init__.py:398,473,486,535` | **GraphQL read paths bypass `_unwrap_graphql_response`** (:146, built after the 2026-06-23 destroy money-leak). On `{"errors":[...],"data":null}`: `find_offers`/`get_instance` AttributeError; `list_instances` returns `[]` → feeds `destroy_confirmed` (lifecycle.py:792) a false "confirmed gone" — the exact leak class the helper kills. `stop_instance` (:535) discards the response → failed pause-billing is silent. |
| B4 | `providers/runpod/selfterm.py:61,76,121` | **In-pod watchdog dead-man fires mid-job.** Rendered watchdog: `heartbeat()` never called, `_job_start` never set → condition-2 fires unconditionally at `2×idle_timeout`; `job_timeout` branch unreachable. Effective pod lifetime `min(2×idle_timeout, max_lifetime−buffer)` (~4 h at defaults) — a legit >4 h render dies mid-job. Dates to `1be572d`, pre-C33-wire-disable; possibly now an intended orphan backstop — NEEDS DISCUSSION on intent, docstring (:13-15) wrong either way. |

### P2 — correctness / UX

| # | Location | Bug |
|---|----------|-----|
| B5 | `core/orchestrator.py:1587-1589` | `deploy()` ready-poll: no sleep, no timeout — busy-spins provider API, hangs forever on stuck-"starting". Sibling :832-835 sleeps 2.0 s but also unbounded (no `boot_timeout_s` enforcement at either). |
| B6 | `cli/_main.py:86` | `_INTERRUPTIBLE_CMDS = {"generate","batch"}` omits `upscale`/`interpolate` (both thread `ctx.cancel_token`) → Ctrl-C bypasses Phase-50 cooperative drain → teardown/leak exposure on the exact `--no-reuse` one-shot paths. Line-84 comment stale. |
| B7 | `cli/_commands.py:2149` | `provider.endpoints(args.id)` passes a **string**; every provider signature takes `Instance` → AttributeError swallowed by broad except at :2150 → `kinoforge status` renders `endpoints=unknown (AttributeError)` unconditionally. |
| B8 | `validation/checks/models.py:41-51` | `_resolve_ref_to_url` ignores `@rev` grammar, hardcodes `/resolve/main/` → `hf:repo@v1:path` HEADs a 404 → false ERROR blocks `generate` for a ref shape `HuggingFaceSource._parse_hf_ref` supports. |
| B9 | `wan_t2v_server.py:1712` (+ `:857`) | Rollback snapshot captured AFTER the swap-gap seeding loop (:1664-1680) → VRAM-OOM rollback replays never-activated seeded targets; `target_refs_dropped` under-reports. Falsy trap: `v.get("last_strength") or 1.0` coerces legit strength `0.0` → `1.0`. May interact with 2026-06-23 file-anchoring spec — discuss. |
| B10 | `engines/diffusers/__init__.py:762-768` | `_poll_set_stack` docstring claims it mirrors `result()`'s transient absorption, but no surrounding catch: exhausted transient burst (known proxy 502s during long downloads) escapes as raw `HTTPError`/`URLError`, bypassing the documented `LoraSwapPodUnreachableError` mapping. |
| B11 | `engines/diffusers/__init__.py:781-824` | (Known, PROGRESS follow-up (a)) `_raise_lora_swap_error` has no `branch_routing` case → generic `RuntimeError("unknown /lora/set_stack error body")`. Confirmed still unhandled. |

### P3 — supply chain / legal / misleading docs

| # | Location | Bug |
|---|----------|-----|
| B12 | `pixi.toml:86` | **`graphifyy = "*"`** (verified): unpinned PyPI dep, imported NOWHERE in src/tools/tests, typosquat-shaped name, resolves 0.8.16 into every env via pixi.lock. Arrived in scaffold commit `fe67f82`. Supply-chain review, then removal. |
| B13 | `README.md:457` | "SPDX-License-Identifier: MIT" contradicts LICENSE (Apache-2.0) and `pyproject.toml:5`. Legally meaningful one-liner. |
| B14 | `README.md:406,439` | Claims `kinoforge cost` reads BigQuery billing export / emits `gcp_status: export-not-ready` — no such code path in src (string lives only in dead `tools/quota_burn.py`). Sends operators to "wait 24 h" for a status that cannot occur. |

---

## Test-coverage gaps opened by the 8d88e0b migration (port before deleting stale files)

| Behavior (old stale test) | New coverage | Verdict |
|---|---|---|
| from-empty download-all | async_job happy + swap_gaps T-B + local_cpu matrix step-2 | COVERED-BY-NEW |
| to-empty evict-all | matrix steps 1&4 + helpers unload test | COVERED-BY-NEW (composite) |
| idempotent same-stack (no redownload) | — | **NOT COVERED — port** |
| overlap downloads-only-new (download-count assert) | — | **NOT COVERED — port** |
| tight-disk LRU evict (job-path wiring, `evict_completed` accrual) | pure-fn helpers only | **NOT COVERED — port** |
| download-fail-no-evict → 502 | async_job + client mapping test | COVERED-BY-NEW |
| download-fail-after-evict → non-empty `evict_completed` from server | client-side synthetic body only | **NOT COVERED — port** |
| disk-full mid-download ENOSPC → 507 phase:download (server) | plan-time 507 only | **NOT COVERED — port** |
| VRAM-OOM rollback restores-previous-stack end-state | empty-prior-state only | **NOT COVERED — port** |
| partial-file cleanup (`_failures.py:195`, still passes) | nowhere else | **RELOCATE before file delete** |

`test_lora_swap_matrix.py::test_vram_oom_rollback_restores_previous_stack`: migrate its raw
`http.post_json` calls to the harness submit+poll driver (sibling already migrated in `42cb84a`).

---

## FIX NOW hygiene (behavior-preserving, low risk — pending mutation opt-in)

Engines:
- `wan_t2v_server.py:837` dead `SetStackResponse` (sole ref: `tests/engines/test_wan_t2v_server_inventory.py:76`, a vacuous parity assert on the dead model — retarget to job status payload). Regenerate golden after.
- `wan_t2v_server.py:312` unused `VRAMEvictionFailed` import + `noqa: F401`; `:1336` local `import json` shadow.
- `wan_t2v_server.py:1520-1550` vs `:1835-1866` — branch-exception→HTTP-detail mapping duplicated verbatim; extract `_branch_error_to_http`.
- `wan_t2v_server.py:1079-1084` vs `:1207-1211` — branch→`target_attr` derivation duplicated; `_resolve_transformer` (self-declared "single dispatch point") should return the attr name.
- `wan_t2v_server.py:1631-1934` `_run_swap_job` ~300 lines / 4-5 nesting — extract phase helpers along existing comment boundaries.
- `diffusers/__init__.py:1240-1324` `wait_for_ready` near-clone of comfyui's (`e3ad3d9` edited both identically) — extract shared helper.
- `replicate/__init__.py:50-59` `_urllib_delete` byte-identical to runway's — move next to `RemoteSubmitPollBackend`.
- `fal/__init__.py:628-634` `validate_spec` hand-rolls the prompt-location decision `resolve_prompt` owns.
- `bedrock_video/__init__.py:403-408` docstring claims validation the body doesn't do.
- `src/kinoforge/engines/luma/` — untracked `__pycache__` residue only; `rm -rf` (nothing in git).

Providers/core:
- `core/interfaces.py:228` add default `last_heartbeat` to `ComputeProvider` ABC (all 4 providers implement ad hoc; skypilot:912 documents the incident; kills `type: ignore` at lifecycle.py:340). Companion: stale comment `providers/local/__init__.py:186`.
- `core/orchestrator.py:1188-1191` stall-threshold defaults triplicated (interfaces.py:96-100, config.py:118-122) — use `cfg.lifecycle()`.
- `core/grid/executor.py:3-6` docstring says `--no-reuse` only on last cell; code passes it on EVERY cell (:741, 2026-06-25 matcher-race rationale) — docstring wrong on money-sensitive behavior.
- `providers/runpod/util.py:125` `read_util` duplicates `probe`'s query+parse ~35 lines; `read_util(id) ≡ probe(id)[1]`.
- `providers/runpod/util.py:56` Bearer-auth closure byte-identical to `heartbeat.py:83` (balance.py:30 variant documents no-shared-transport intent — leave that one).
- `providers/runpod/__init__.py:752` `_create_pod` ~220 lines — extract helpers.
- `core/grid/executor.py:464` `_run_swap_group` 233 lines, builds `GridCellResult` 6× — extract attempt loop + result factory.
- `core/orchestrator.py:259` `except (ImportError, Exception)` — subsumed tuple, silent cred-fault mapping.
- `providers/modal/__init__.py:203` `created_at=self._clock()` vs runpod/skypilot `0.0` — Modal instances look forever-new to age-based consumers; drift, no comment.
- `providers/skypilot/__init__.py:81-82` vestigial `if TYPE_CHECKING: pass`; `:334` unreachable third condition.

CLI/pipeline/rest:
- Pod-HTTP client triplicated (~150 LOC × 3): `upscalers/flashvsr/_engine.py:290-380` ≡ `spandrel/_engine.py:217-312` ≡ `interpolators/rife/_engine.py:206-295` (502-warmup retry, sha cross-check, UA gate, timeouts = one decision) — extract shared client.
- `validation/checks/models.py:32-38` `_default_http_head` ×3 (image.py, custom_nodes.py) + `_PASS_CODES` ×2 + `timeout=5` ×3.
- `validation/__init__.py:189-227` `validate_for_load` copies `validate_for_generate`'s autofix/report/log/raise block.
- Lock lease loop byte-identical ×3: `stores/gcs/lock.py:127-198`, `s3/lock.py:134-206`, `local_lock.py:105-190` — template-method base; only CAS primitive is backend-specific.
- `cli/_commands.py:753-770` cold-create ledger-stamp block copied from `_cmd_generate:595-626`; `:1031-1071` warm-attach precedence chain duplicated from `:509-570` — extract helpers (also shrinks `_cmd_generate`, finding below).
- `cli/_main.py:732` `gc --older-than` parsed, never read — operator-facing no-op flag: implement or delete.
- `outputs/local.py:167-168` re-encodes filename schema that `format_filename` (base.py:136-169) is declared single source of truth for.

Tests:
- `test_resolve_transformer.py:200` fix pollution (live-attribute access) — un-reds 3 tests.
- `test_set_stack_async_job.py:19-22` fixture mutates module state with no teardown (pytest-randomly active) — restore in teardown.

Tools:
- `tools/preflight.py:177` docstring "all four checks" — runs three; fourth lives in `main()` (:312).
- `tools/smoke_leak_sweep.py:66` `gh issue create` result unchecked — reaped-leak notification vanishes silently; check returncode + log.

## NEEDS DISCUSSION (user decisions)

1. **`wan_t2v_server.py` decomposition** — 2372 lines, six concerns (VRAM LRU, LoRA swap, generate/upscale/interpolate workers, upload). Embed mechanism ships whole `servers/` package, so sibling-module split is compatible — but byte-identity goldens make it a deliberate pass.
2. **Dead lifecycle layer** — `core/lifecycle.py`: `LifecycleManager`(:132), `warm_reuse_or_create`(:351), `reap`(:814), `BudgetTracker`(:889) ≈ 450 lines, zero production callers (superseded by sweeper/reaper_actor/warm_reuse pkg). `Ledger`/`destroy_confirmed` in same file are LIVE — surgical delete.
3. **Closed-investigation tools purge** (~3,400 of 7,300 LOC in tools/): `quota_burn*.py` (1,153 LOC + 6 CI-burning test files; Phase 52 CLOSED), `c33_*.py` ×4 (1,220 LOC; C33 CLOSED, evidence durable in tests/live/_c33_*.json), `cloud_perms_probe.py` (+ stale `pixi.toml:160` task), uptime-sweep family (EXCEPT `repro_runpod_uptime.py` — PROGRESS retains for external bug report), `build_bsa_wheel.py` (superseded by Modal builder), `tools/local_hooks/` (decommissioned per operator feedback). Plus `pixi.toml:76,87-90`: five `google-cloud-*` deps consumed ONLY by this dead tooling.
4. **Stale set_stack test files** — port the 5 uncovered behaviors + relocate partial-file-cleanup test, then delete both files (kills the flaky-red + 2 of 4 duplicate `_Stub` classes).
5. **Reload-pattern hazard** — snapshot/restore fixture or lint banning top-level imports from `wan_t2v_server` in tests.
6. `providers/runpod/__init__.py:843` `containerDiskInGb: 250` / `minMemoryInGb: 32` hardcoded; `HardwareRequirements.disk_gb` exists but never reaches create body (in-code TODOs). Config plumbing = behavior change.
7. `core/orchestrator.py` `deploy_session` (~500 LOC) + `generate` (~515 LOC, :1623) — `generate` embeds two near-identical materialize-before-destroy blocks (:2054-2100, :2107-2133).
8. `comfyui/__init__.py:1146-1406` + `diffusers/__init__.py:998-1238` `render_provision` twins (~260/240 LOC) — goldens make refactor safe but deliberate.
9. Upscaler registration `except UnknownAdapter: pass` (seedvr2/spandrel/flashvsr/rife `__init__`) silently defeats registry's documented loud duplicate-rejection.
10. `pipeline/artifact_bytes.py:70-74` fabricates deterministic synthetic bytes for uri-less artifacts (FakeEngine support) — mis-addressed real artifact ships garbage silently instead of raising.
11. `comfyui/__init__.py:63-69` `TARGET_TO_SUBDIR` functional no-op (every value = `models/{key}`, fallback identical) — closed-vocabulary doc or delete.
12. `upscalers/spandrel/_fetch_weights.py` production-dead since `0d990d5`; docstring + `config.py:531` claim dispatch it doesn't do.
13. `image_engines/replicate/__init__.py:102-111` "Task 17" scaffold comment stale; raising `_delete` reachable via ephemeral delete cascade.
14. `stores/s3/__init__.py:70-73` prod `__init__` branches on `hasattr(client, "set_retry_config")` purely for tests.
15. `diagnostics/c30_probe.py:777` computed-and-discarded spec-§4 value; `:128-130` unreachable `NoSuchKey` branch.
16. AC8 scanner precision — teach `_reads_lora_inventory` Load-vs-Store ctx (kills the wan_t2v_server false positive class at the root).

## Docs/config mismatches

- README.md: providers list omits **Modal** (6 logged generations §22-27); license line wrong (B13); BQ cost guidance bogus (B14); missing "Project structure" section (user convention).
- `successful-generations.md` entries 26/27 use an evolved de-facto schema (Exact command(s)/Artifacts/Frame-QA verdict/Reproduction recipe) vs the spec'd one (`2026-06-08-successful-generations-log-design.md:80-156`) — internally consistent; update the spec, not the entries.
- `outputs/base.py:74` docstring still lists `luma` among provider examples (video engine deleted Phase 44).
- pyproject/pixi coherent otherwise: markers declared+used, mypy strict with documented overrides, `__main__.py` present, `.gitignore` covers secrets/outputs.

## LEAVE (documented intent — do not re-litigate)

- `wan_t2v_server.py` `_run_upscale_job` vs `_run_interpolate_job` 1:1 mirroring — deliberate, documented (:2193-2198); `_run_swap_job` structurally different.
- `providers/runpod/heartbeat.py:48` `_POD_EDIT_JOB_MUTATION` + `_merge_marker` — C33-m write-disable asymmetry documented with B5b resumption criteria.
- `providers/runpod/balance.py:30` third Bearer-closure variant — "no shared transport" documented intent.
- `tools/_uptime_field_sweep_log.jsonl` under tools/ — PROGRESS references exact path.
- `tools/repro_runpod_uptime.py` — retained for deferred external bug report (PROGRESS).
- `tools/flashvsr_debug_matrix.py` — closed investigation but generic warm-pod variant harness, reusable.
- `tools/probe_civitai_throughput.py:43-47` — imports provider privates; acceptable for a diagnostic.
- `tests/smoke/local_cpu/test_lora_swap_matrix.py:72` weak-looking outer assert — real assertions in `run_matrix`.
- Mega test files (test_orchestrator 2868, test_config 1983, test_comfyui 1974 LOC) — split opportunistically when touched.
- `tests/_fixtures/test_fake_auth.py` collected inside helper pkg — convention nit only.
- Alive despite appearances (fences checked): `tools/c28_provision_s3_diagnostics.py` (bucket used by orchestrator.py:248), `comfyui_ui_to_api.py` + `_vendored/`, `probe_pod_watchdog.py`, `tools/bootstrap_kms.py`.

## Health summary

Codebase is unusually well-documented — most surviving smells carry dated incident rationale, and
hosted/Bearer adapters are small and clean. Debt concentrates in three places: `wan_t2v_server.py`
(six concerns, two state-fidelity bugs, capability mismatch), growth-by-mirroring triplicates
(pod-HTTP client, locks, validation heads) held together by "sync this" comments, and ~3,400 LOC
of closed-investigation tooling dragging five cloud SDK deps. The suite core is strong but the
job migration left a flaky-red shadow contract and five uncovered behaviors. Highest-value order:
B12 (supply chain), B1/B2/B3 (money/output), Finding #0 un-red (pollution one-liner + AC8
resolutions + stale-file port plan), then the FIX NOW consolidation batch.
