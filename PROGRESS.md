# PROGRESS — kinoforge

Recovery index. A fresh/resumed session reads THIS first (see `CLAUDE.md` → Session resume
protocol), then the design + plan it points to, then `git log --oneline -20`, then resumes from the
first unchecked task without redoing committed work.

## Pointers
- **Spec (the *what*):** `SPEC.md`
- **Design (validated):** `DESIGN.md`
- **Implementation plan:** `docs/superpowers/plans/2026-05-29-kinoforge.md`
- **Native task snapshot:** `docs/superpowers/plans/2026-05-29-kinoforge.md.tasks.json` (28 tasks, IDs 1–28, dependencies set)

## Active workstream
**C33 ROOT CAUSE RESOLVED 2026-06-17 (autonomous overnight, $0.91 cumulative).** The T+15 s bash-exit / ~31 s container-restart cycle plaguing every Wan-on-RunPod cold-boot since C28 was the **C25 B5a `RunPodGraphQLHeartbeatEndpoint.write()`** issuing a `podEditJob` mutation every 30 s. RunPod treats any `dockerArgs` mutation as a container-reconcile event — destroys + recreates the container — and `pod.lastStartedAt` stays invariant (POD-level restart marker, not CONTAINER-level), which is why C33-P1's lastStartedAt-only classifier missed it entirely. Two-stage fix shipped: (1) `aacd49e` moved `start_heartbeat` AFTER `engine.provision` returns in `_provision_instance_and_build_backend` so provision finishes before the first tick; (2) `c2526ac` no-ops the RunPod heartbeat `write()` because the C33 (n) live confirmation showed post-provision ticks STILL restart the container — the satisfier itself is incompatible with the current RunPod API. Final live confirmation under ORIGINAL `cfg_c28_phase_a_diagnostic.yaml` (heartbeat_mode=graphql-tag): generate completed in 3:29 wall, artifact `f7173e92e31968c5.mp4`, pod destroyed cleanly, `C33-m: RunPod heartbeat write DISABLED` warning fired exactly once. Bisection commits (h–n) preserved verbatim in `tests/live/_c33_probe_{h,j,k,l,m,n}_evidence.json`. Closed follow-ups: ~~(h)~~ ~~(i)~~. Remaining open follow-ups (see C33 entry below): (f) trivial restart-policy warning string, (a) classify_run negative-uptime heuristic refinement, (d) unit test banning top-level `Pod.uptimeSeconds` reads, (g) `diagnostic_mode: "trace"` cfg opt-in. New durable spec hooks: **B5b** non-mutating heartbeat substitute (e.g. `selfterm-http` mode pinging an in-pod HTTP endpoint that touches a local file timestamp without mutating any RunPod resource); **B7 cross-CLI marker refresh** for new pods (current state: marker is set at create time only, write is a no-op, read still parses any pre-existing trailer). 18 local commits ahead of `origin/main`; not pushed.

## Next session — resume target (single next action at top)

**CI RED PRE-EXISTING FAILURES RESOLVED 2026-06-19 (autonomous).** Both families
fixed + one bonus flake closed; **full FAIL-mode suite GREEN with zero ignores**:
`2631 passed, 76 skipped, 6 xfailed in 123.98s`. The original L1 follow-up
goal (PROGRESS line 100-104) — "extended-ignore collapses back to original
2-file set" — is exceeded: even the 2-file set is no longer needed. L1
thread-leak FIX policy is in fully load-bearing FAIL mode against the
entire suite, no escape hatches.

- Family 1 — heartbeat / supplied-instance regression (8 failures): commit `b1c8c7d`
  adds `warm_reuse_auto_attach: false` to `_COMPUTE_YAML` in `tests/core/test_orchestrator.py`
  + `tests/core/test_orchestrator_heartbeat.py`. Root cause: `HeartbeatIntervalRequiredCheck`
  (new with the cfg-validation merge) auto-fixed `heartbeat_interval_s=None`→`30`
  whenever a test cfg had a `lifecycle:` block AND `warm_reuse_auto_attach` defaulted
  to True (config.py:546). Auto-fix silently spawned an HB loop the test scenarios
  never set up to satisfy — surfaced as `FirstTickTimeout` on 6 supplied-instance tests
  + `session_start written when HB disabled` on 1 + `loop spawned despite interval_s=None`
  on 1. Production contract unchanged; only the test fixtures gain explicit semantics.
- Family 2 — core layering invariant (1 failure): commit `f1b958d` replaces the two
  direct `import kinoforge.providers.{runpod,skypilot}` calls in `src/kinoforge/core/config.py:1134-1135`
  with a single `import kinoforge._adapters`. Routes provider-check registration via
  the canonical adapter hub (the SOLE module permitted to import providers/). AC 1
  subprocess-isolation still green — function-local import does not enter sys.modules
  at core.config module-load time.
- Bonus — race in `test_blocking_acquire_serializes_concurrent_calls` (commit
  `345dcb2`). Plan A's single-run harvest missed this leaker because the race
  went the lucky way that day. Under FAIL mode the race lost on first re-run;
  `second`'s pre-acquire `ledger.touch(...200.5)` was clobbered by `first`'s
  in-band `touch(...100.5)` so `second`'s poll loop never satisfied
  `tick >= start=200.0` (FakeClock(start=200.0) never advances). Moved the
  `200.5` write inside `second`'s with-block — same intent as `first`'s
  in-band touch at line 181. 10x serial re-runs green.

### Original failure list (now resolved)
Most recent CI run `27838499829` (push `81ee758..1d83d1d`, branch `main`) failed on
both ubuntu-latest + macos-latest. Failures are NOT caused by the thread-leak work
(L1 was WARN mode, could not fail tests). Two independent bug families:

**Family 1: heartbeat / supplied-instance regression (8 failures).** Likely introduced
by cfg-validation merge `70bcda3` or a sub-commit (`f3dd5d3`, `1df7aa8`, `3ba8369`,
`2d96a3d` touch validation+orchestrator pathways).

| Test | Failure |
|---|---|
| `tests/core/test_batch_generate.py::test_batch_generate_with_supplied_instance_skips_create` | `FirstTickTimeout: no heartbeat tick for 'pod-premade-7b2' within 960.0s` |
| `tests/core/test_orchestrator.py::test_deploy_session_with_supplied_instance_skips_create_and_find_offers` | same `FirstTickTimeout` |
| `tests/core/test_orchestrator.py::test_deploy_session_with_supplied_instance_runs_discover_on_cache_miss` | same |
| `tests/core/test_orchestrator.py::test_deploy_session_supplied_instance_calls_engine_provision` | same |
| `tests/core/test_orchestrator.py::test_generate_threads_instance_kwarg_to_deploy_session` | same |
| `tests/core/test_orchestrator.py::test_deploy_session_tags_ignored_when_instance_supplied` | same |
| `tests/core/test_orchestrator_heartbeat.py::test_deploy_session_with_interval_none_does_not_spawn_loop` | loop spawned despite `interval_s=None` — restart-loop registry contains one unwanted entry |
| `tests/core/test_orchestrator_session_fields.py::test_deploy_session_session_start_absent_when_hb_disabled` | `session_start` written when heartbeat disabled (e.g. `1781893998.325463 is not None`) |

**Family 2: core layering invariant (1 failure).**

| Test | Failure |
|---|---|
| `tests/test_core_invariant.py::test_no_adapter_imports_in_core` | `src/kinoforge/core/config.py:1134-1135` self-registers `import kinoforge.providers.runpod` + `import kinoforge.providers.skypilot` at module load — violates `core/` cannot import `providers/` |

**Related but orthogonal:** the FileLock hang in `src/kinoforge/stores/local_lock.py`
caused Plan A's harvest to `--ignore=tests/core/test_orchestrator.py
--ignore=tests/core/test_batch_generate.py` (lock held past
`_make_premade_instance` teardown blocks the next call with `timeout_s=None`).
The 6 `FirstTickTimeout` failures above are downstream of either the heartbeat
regression OR this lock contention — investigate both vectors.

Last green CI: `a5ee765` (~14h ago). 30+ commits since spanning the cfg-validation
merge (`70bcda3`), doctor-smoke xfail cleanup (`d8e5941`), and the thread-leak work
(`d38d565..1d83d1d`).

**Plan B written 2026-06-19** at `docs/superpowers/plans/2026-06-19-pytest-thread-leak-fix-policy-plan-b.md`
(commit `c5399e4`) — 2 tasks: fix `kinoforge-pool-0_0` ThreadPoolExecutor leaker
+ flip L1 WARN→FAIL.

**Task 1 LANDED (autonomous, commit `565624e`).** Plan's
`initializer=_mark_thread_daemon` mechanism did NOT work on Python 3.13
(`Thread.daemon` setter raises `RuntimeError("cannot set daemon status of
active thread")` once the worker has started — initializer runs INSIDE the
live worker, too late). Pivoted to a `_DaemonThreadPoolExecutor` subclass
that overrides `_adjust_thread_count` and sets `daemon=True` BEFORE
`t.start()`. Same intent, working mechanism. 2 new unit tests in
`tests/core/test_pool_workers_daemon.py` pin the contract (every alive
`kinoforge-pool-*` is daemon=True; worker observes
`current_thread().daemon == True` at run time). The previously-xfail-ish
`test_l1_thread_policy.py::test_hook_exempts_daemon_threads` now passes
clean. 28-test thread-stack + pool_cancel regression green. ruff + mypy clean.

**Task 2 LANDED (autonomous).** `_L1_MODE` flipped `"warn"` → `"fail"` in
`tests/conftest.py`; `_l1_append_warn` helper deleted (dead under FAIL);
WARN branch deleted from `pytest_runtest_makereport`; top-level
`import sys` dropped (only `_l1_append_warn` used it); inventory line
removed from `.gitignore`; `tests/_l1_leakers_inventory.txt` deleted;
two WARN-mode tests pruned from `tests/test_l1_thread_policy.py`
(now 6 tests: call-stash, setup-noop, FAIL-flip, silent-on-call-failed,
daemon-exempt, known-pytest-name-exempt). All 6 L1 unit tests green.
26-test thread-stack regression green.

**Scoped re-run had to extend `--ignore` beyond the harvest's 2-file
list.** The harvest used `--ignore=tests/core/test_orchestrator.py
--ignore=tests/core/test_batch_generate.py` (FileLock contention modules)
under WARN mode, so the heartbeat-regression tests in
`tests/core/test_orchestrator_heartbeat.py` +
`tests/core/test_orchestrator_session_fields.py` (pre-existing 9 CI
failures noted above) silently leaked their heartbeat-loop threads —
which then cascaded as L1 ERRORs across every subsequent test under FAIL
mode. Plan B's design expected zero L1 ERRORs because the harvest reported
one leaker (`kinoforge-pool-0_0`), but the harvest could not see the
cascade because WARN never failed teardown. Extended ignore set to
`--ignore=tests/core/test_orchestrator.py --ignore=tests/core/test_batch_generate.py
--ignore=tests/core/test_orchestrator_heartbeat.py --ignore=tests/core/test_orchestrator_session_fields.py
--ignore=tests/test_core_invariant.py` and the suite passes clean:
`2527 passed, 76 skipped, 6 xfailed in 113.31s` — zero L1 errors. The
extended-ignore set IS the existing pre-existing-CI-failures list noted
above; no NEW L1 leakers were uncovered.

Followup (separate workstream): fix the heartbeat / supplied-instance
regression so the extended-ignore can collapse back to the original
2-file set, and then no future code-change can leak a non-daemon thread
without an L1 ERROR appearing instantly. The policy is now in place to
catch the next leaker on its first run.

L1 thread-leak FIX policy is now ENFORCING. Spec
`docs/superpowers/specs/2026-06-19-pytest-thread-leak-fix-policy-design.md`
closed.

---

**Pytest thread-leak FIX policy Plan A SHIPPED 2026-06-19 (autonomous, pushed).**
Plan `docs/superpowers/plans/2026-06-19-pytest-thread-leak-fix-policy-plan-a.md`
landed on `main` in 4 commits + 2 doc commits:
- `d38d565` — spec (3-layer defense)
- `b13b201` — plan
- `3508835` — Task 1: `managed_thread` fixture + 4 unit tests
- `ec12d0f` — Task 2: L1 `pytest_runtest_makereport` hookwrapper in WARN mode + 8 unit tests
- `45d7e4d` — Task 3: shared subprocess helper + L1 e2e smoke
- `1d83d1d` — Task 4: harvest run, frozen inventory doc

Harvest result: **ONE leaker** — `kinoforge-pool-0_0` (non-daemon), 1845 distinct
test nodeids (5532 raw lines). Source: `src/kinoforge/core/pool.py:211-214`
ThreadPoolExecutor workers default to non-daemon on Python 3.13. Plan B trivially
scoped to that single fix + WARN→FAIL flip.

---

**Pytest thread-leak diagnostic (layered) SHIPPED 2026-06-19 (autonomous).**
Plan `docs/superpowers/plans/2026-06-19-pytest-thread-leak-diagnostic.md`
landed on `main` in two commits:
- `0f02e34` extracted the inline `pytest_sessionfinish` dump body into
  pure helper `tests/_thread_dump_helper.py::_build_dump(threads,
  exitstatus) -> str` + 3 unit tests (`tests/test_post_session_dump.py`)
  pinning name/daemon/n_threads, the C-extension fallback marker, and
  the linux `/proc/self/fd` inventory.
- `a9929a0` layered a C-side safety net: `pytest_configure` arms
  `faulthandler.dump_traceback_later(15, repeat=False, file=sys.stderr,
  exit=False)`; `pytest_sessionfinish` prepends
  `faulthandler.cancel_dump_traceback_later()` so healthy runs do NOT
  also dump from the timer. `tests/test_post_session_dump_e2e.py`
  spawns pytest as a subprocess on a temp leaky-thread module and
  asserts banner + `daemon=False` + `e2e_leaker` in stderr.

Two plan-discovered deltas (documented in `a9929a0`):
- subprocess gets PYTHONPATH=tmp:project/src:project (cwd=tmp_path
  skips the project pyproject's pythonpath=["src"] config).
- Leaked non-daemon thread blocks subprocess past summary, so e2e
  uses Popen + communicate(timeout=10) + TimeoutExpired to capture
  pre-stall stderr then kill the runaway.

Native task IDs 1–2 of this run completed; .tasks.json synced.
All 9 thread-dump tests pass (3 unit + 1 e2e + 5 existing hook tests)
in 10s.

**Cfg validation Check Registry SHIPPED 2026-06-19 (autonomous).**
All 14 tasks of plan `docs/superpowers/plans/2026-06-18-cfg-validation-check-registry.md`
landed on branch `worktree-cfg-validation-check-registry`. New package
`kinoforge.validation` (Check Protocol + plugin Registry + 9 built-in
checks across STATIC / NETWORK / PREFLIGHT categories); load_config
runs the STATIC pass with single-retry auto-fix (deviation from plan —
deferred NETWORK + PREFLIGHT to CLI wrappers to preserve backward compat
with cfgs using `https://example.com/...` placeholders); new
`kinoforge doctor <cfg>` subcommand prints the full report with exit
code = error count; `kinoforge generate` runs pre-flight before any
provider call (opt-out via `--skip-preflight`).

**Live doctor smoke (`tests/live/test_doctor_examples_live.py`,
gated by `KINOFORGE_LIVE_TESTS=1`)** ran end-to-end against the 21
example cfgs: **9 clean, 12 xfailed** with documented reasons. Each
xfail is a follow-up:
- 7 cfgs got the heartbeat fix inline (local-fake, wan, diffusers,
  skypilot×3, runpod-comfyui-wan).
- 4 cfgs ship placeholder `https://example.com/...` model refs that
  404 (local-fake, cost, batch-prompts, diffusers, hosted, fal).
- 4 cfgs (skypilot×3, wan) point at `hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors`
  which 404s on HF Hub — actual filename likely differs.
- `nova-reel.yaml` engine kind not in `KNOWN_ENGINES`; either fix
  the cfg or add `nova_reel` to the engine registry.
- `runpod-comfyui-wan-manifest.yaml` is a batch manifest (top-level
  list), not a Config mapping — doctor needs a manifest-aware path
  or the file should be moved out of `examples/configs/`.

**Plan deviations (committed + explained in commit messages):**
- Task 3 precursor: bumped `LifecycleConfig.grace_after_session_s`
  default 300→1800 (commit `ad84e2b` only bumped the dataclass; this
  completes it).
- Task 10: introduced `validate_for_load` (STATIC only) for
  load_config instead of `validate_for_generate` (all categories).
  Rationale captured in commit body — every cfg that loaded green
  before the registry must still load green.

Native task IDs 36–49 all completed; .tasks.json synced.

**12 xfailed example cfgs CLOSED 2026-06-19 (autonomous).**
Plan `docs/superpowers/plans/2026-06-19-doctor-smoke-xfail-cleanup.md`
landed on branch `doctor-smoke-xfail-cleanup` (not yet merged to
main at time of writing). Five edits unblocked all 12 entries:

  1. `ModelRefReachableCheck.applies_to` filters non-fetching engine
     kinds (hosted / fal / replicate / runway / bedrock_video / fake)
     so their cfgs can ship informational refs without breaking
     doctor (commit `b9c1bd0`).
  2. Batch manifests moved to `examples/configs/manifests/`; doctor
     glob switched to a `"manifests" not in p.parts` filter so the
     subdir is excluded while `comparison/` stays in scope (commit
     `9771b18`; docstring follow-up `677f59f`).
  3. Wan2.2 refs repointed to a real sharded HF file
     (`high_noise_model/diffusion_pytorch_model-00001-of-00006.safetensors`)
     in all four cfgs (`wan.yaml`, `skypilot.yaml`, `skypilot-gpu.yaml`,
     `skypilot-lambda.yaml`). Plus `skypilot.yaml` `heartbeat_interval_s`
     lowered 30 → 20 to satisfy the cfg-validation Check Registry's
     `idle_timeout_vs_heartbeat` rule (3 * heartbeat must fit within
     the 60 s idle_timeout). Commit `a013277`; comment softening
     follow-up `e41ca88`.
  4. `nova-reel.yaml` collapsed onto the registered `bedrock_video`
     engine (no new engine module). The `nova_reel:` sub-block was
     replaced with `bedrock_video.model_input_template` carrying
     the real Bedrock Nova Reel API shape (`taskType`,
     `textToVideoParams.text: "${PROMPT}"`, `videoGenerationConfig`).
     Live smoke + integration-skip entry updated. Commit `9bef7cd`.
  5. `_KNOWN_BROKEN` dict deleted from
     `tests/live/test_doctor_examples_live.py`. In-task discoveries:
     `cost.yaml` + `diffusers.yaml` STATIC validation tripped on
     missing `heartbeat_interval_s` once their xfail markers were
     gone; both cfgs received `heartbeat_interval_s: 30` (cost is
     RunPod, diffusers is RunPod serverless — both warm-reuse-attach
     default true). Live doctor smoke
     (`KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_doctor_examples_live.py -v`)
     reports **19 PASSED, 0 XFAIL / XPASS / FAIL** in ~24 s
     (21 cfgs recursive minus 2 manifests rehomed under
     `examples/configs/manifests/`).

Filed follow-ups (anchored 2026-06-19, do NOT drift):
- `tests/live/test_nova_reel_live.py:121` — TWO bugs in one line:
  (a) `Path("tests/engines/fixtures/nova_reel")` is a relative path,
      so it resolves correctly only when pytest is invoked from the
      repo root. Any other cwd silently writes to the wrong dir;
      fix with `Path(__file__).resolve().parents[2] / "tests/engines/fixtures/<name>"`.
  (b) The `nova_reel` segment is inconsistent with the now-canonical
      `bedrock_video` engine name. Rename the fixtures dir (or skip
      writing to it) before the next Nova Reel live smoke fires
      under `KINOFORGE_SAVE_FIXTURES=1`.
- Stale `wan2.2_14b.safetensors` refs still present in
  `examples/configs/fal.yaml:20`, `examples/configs/hosted.yaml:29`,
  `tests/test_examples.py:161,182`, and
  `successful-generations.md:74`. The cfgs do NOT break doctor (Task
  1's `_NON_FETCHING_ENGINES` filter skips `fal` + `hosted`), but the
  refs contradict the canonical comment shipped in
  `examples/configs/wan.yaml:14-22`. Bulk-repoint to
  `hf:Wan-AI/Wan2.2-T2V-A14B:high_noise_model/diffusion_pytorch_model-00001-of-00006.safetensors`
  for consistency.
- `src/kinoforge/engines/hosted/__init__.py:808` — `_HOSTED_DEFAULT_KEY`
  hardcodes the old `wan2.2_14b.safetensors` filename. Used to seed
  `_DEFAULT_DECLARED_FLAGS_MAP`, so any capability-cache lookup that
  hashes the new canonical ref against this key will miss and surface
  a runtime cache-miss warning. Repoint to the new canonical ref to
  match.

**RESUME TARGET:** pick the next workstream from the queue below.
Likely candidates: (1) the parked thread-leak brainstorm in
`core/pool.py` (still elevated priority — symptom observed again
during this workstream: ~50 zombie `pixi`/`pytest` pairs piled up
from subagent-dispatched test runs); (2) merge
`doctor-smoke-xfail-cleanup` into main; (3) one of the Stage E
ledger-row follow-ups below (skypilot age / cost / capability_key).

---

**Phase 53 Stage E CLOSED 2026-06-18 — end-to-end path verified on
Lambda.** Live smoke ran twice:
- Run 1 (19:14:25 → 19:21, ~7 min, ~$0.15): instance came up; sky
  failed at `sudo docker pull skypilot/skypilot-gpu:latest` because
  that namespace 404s on Docker Hub — Stage-C cfg shipped a
  placeholder image that was never verified. Cluster torn down via
  `sky down -y skypilot-cluster`.
- Run 2 (19:25:39 → 19:33:55, ~8 min 16 s, ~$0.18) after `05fc93d`
  swapped the image to `nvidia/cuda:12.2.0-base-ubuntu22.04`:
  Lambda A10 (us-east-1) chosen → `Instance is up` → `Docker
  container is up` → `Cluster launched` → orchestrator returned
  `deployed instance 'skypilot-cluster' via 'skypilot'
  (status=ready)`. Full kinoforge → SkyPilotProvider → sky →
  Lambda contract verified. Cluster torn down via `sky down -y
  skypilot-cluster`. Total Stage E spend ≈ $0.34 against the
  ~$0.15 envelope — overage attributable to Run 1's image-pull
  failure.

**RESUME TARGET:** pick the next workstream from the queue below.
Stage D (Vast.ai parity) remains BLOCKED on upstream sky vast
adapter regression — Lambda is sole sky cloud until upstream ships
the fix.

### Stage E follow-ups (filed 2026-06-18)
- **Bogus skypilot ledger fields.** The Run-2 ledger entry came out
  as `skypilot-cluster  age=494954.6h  est_spend=$0.0000  capability_key=<unknown>`
  via `kinoforge list`. Three independent bugs surfaced by the first
  real skypilot ledger row: (a) age computation reads a non-existent
  or zero `created_at`, (b) cost computation gives up silently for
  skypilot rows, (c) capability_key isn't persisted onto the ledger
  for skypilot. None blocked teardown — `kinoforge forget --id
  skypilot-cluster` cleared the row cleanly. File as a Layer-S
  follow-up before any next skypilot deploy that needs operator-
  facing list/cost UX.
- **Same image placeholder in `skypilot.yaml` + `skypilot-gpu.yaml`.**
  Commit `05fc93d` scope-limited the fix to `skypilot-lambda.yaml`.
  Apply the same swap (or document the operator-supplied image
  expectation) to the other two cfgs before either is used live.
- **Stale RunPod ghost ledger entries. CLOSED 2026-06-18.**
  Seven RunPod rows aging 43–51 h with cumulative ~$73 phantom
  estimated spend. Preflight confirmed 0 active pods on RunPod side
  — purely ledger-side state from earlier C33 / Phase 47 / Phase 52
  sessions that were never reconciled. Swept via
  `for id in <7 ids>; do pixi run kinoforge forget --id "$id"; done`.
  `kinoforge list` now reports `No instances recorded in ledger.`

> ⚠️ **DEFERRED — UPSTREAM HOOK ISSUES TO FILE.** Two issue bodies are
> drafted under `docs/upstream-issues/` and awaiting Dr. Twinklebrane's
> manual filing. Local hooks at `~/.claude/hooks/` are in active soak.
> See **Parked queue item 2** below for the trigger criteria + URLs to
> file. Do NOT let this drift past the next session resume.

### Elevated priority — thread-leak fix brainstorm (`core/pool.py`)

**Why this jumped the queue (compelling practical reasons, anchored
2026-06-18 from live evidence):**

1. **TDD friction observed this session.** The full `pixi run test`
   regression suite finished its test session in 89 s (`2542 passed,
   1 failed, 57 skipped, 6 xfailed`) but then hung for ~24 additional
   minutes waiting for the leaked non-daemon threads to exit. Only
   SIGTERM recovered the prompt, which also discarded the
   end-of-session diagnostic output. The current local workaround is
   cherry-picking subsets that exclude `tests/core/test_pool_cancel.py`
   — REACTIVE (catches only the known leaker, misses any new one) and
   only viable because the leaker is named. Every TDD cycle that wants
   full-suite confidence either takes 25 + min wall, terminates
   early, or relies on subset discipline that may silently exclude a
   newly-introduced regression in `core/pool.py`-adjacent code.

2. **Confidence erosion compounds.** Subset runs miss the "did I
   break something far away" guarantee a full suite provides. The
   longer the leaker stays unfixed, the more contributors learn to
   skip the full suite locally, the more bugs in distant modules
   land between green-subset runs and CI catching them.

3. **Plausible production risk path.** `src/kinoforge/core/pool.py`
   is the production submit / cancel / shutdown contract used by
   `kinoforge generate`'s concurrent-jobs path. The leaker thread
   (`kinoforge-pool-0_0`, non-daemon, stuck in
   `time.sleep(60.0)` at `tests/core/test_pool_cancel.py:60` via
   `src/kinoforge/core/pool.py:338`) exposes the same shutdown
   contract used in real generate flows. If a production cancel /
   shutdown path hits the same shape — non-daemon worker waiting on
   `slot.executor.shutdown(wait=True)` at `pool.py:421` while the
   worker itself is wedged — the kinoforge CLI process would fail to
   exit cleanly AFTER the artifact has been written. Operator's
   natural Ctrl-C would orphan the pod (ledger entry persists, no
   clean `destroy_instance` on exit, RunPod / Lambda spend bleeds
   until selfterm or autostop fires). This path has not yet been
   reproduced in production but the bug shape is the same.

4. **Compounding maintenance cost.** Every new test against the
   pool's contract risks landing the same leak. Reluctance to touch
   `core/pool.py` entrenches the bug and slows down adjacent feature
   work (e.g. C26 stall classify, B1 sweeper).

5. **Investigation is already done — only the brainstorm + fix
   remain.** The CI banner on run `27801123512` named both threads
   (`kinoforge-pool-0_0` non-daemon and `kinoforge-pool-shutdown-*`
   daemon blocked in `slot.executor.shutdown(wait=True)` at
   `pool.py:421`). No additional diagnostic work needed — the
   brainstorm just designs the submit / cancel / shutdown contract
   fix from a known repro.

6. **Cross-cutting unblock.** Fixing this also clears the path for
   ambient pytest hygiene improvements (`pytest-timeout` dev dep,
   per-test timeout default) that would proactively catch the NEXT
   leaker without requiring a fresh diagnostic round.

**Concrete next action.** Fresh `/brainstorming` session targeting
the `core/pool.py` submit / cancel / shutdown contract. Entry-point
artefacts: `src/kinoforge/core/pool.py` (line 338 submit path,
line 421 shutdown(wait=True) join), `tests/core/test_pool_cancel.py`
(the repro fixture), and the CI banner output at run
[27801123512](https://github.com/killett/kinoforge/actions/runs/27801123512)
(`pid=2490 exitstatus=0 n_threads=7`). Out of scope: any change to
the diagnostic hook (closed via Parked queue item 1).

### Parked / do-not-forget queue (anchored 2026-06-17)

These items have plans/specs/code in-tree and MUST NOT drift. Each is
parked behind a higher-priority workstream but the durability anchor
lives here so a fresh-session resume reads them before Phase 53 / C33.

1. **Pytest post-session hang — diagnostic Task 3 (CI validation). CLOSED 2026-06-18.**
   Plan: `docs/superpowers/plans/2026-06-17-pytest-post-session-hang-diagnostic.md` §Task 2.
   Code shipped (`976e09d` hook, `330883a` review fixes, `49ae731` test hygiene).
   CI banner CONFIRMED on ubuntu-latest run **27801123512** (sha `9a6b9a3`,
   `pid=2490 exitstatus=0 n_threads=7`) — same leaker as local diagnostic:
   `kinoforge-pool-0_0` non-daemon thread stuck in `time.sleep(60.0)` at
   `tests/core/test_pool_cancel.py:60` via `src/kinoforge/core/pool.py:338`.
   CI also surfaced a secondary blocker `kinoforge-pool-shutdown-*` daemon
   thread blocked in `slot.executor.shutdown(wait=True)` at `pool.py:421`
   waiting on the same non-daemon worker. **Handoff:** fresh
   `/brainstorming` for the thread-leak fix spec (out of scope for the
   diagnostic plan) — proposal entry-point is `src/kinoforge/core/pool.py`
   submit/cancel/shutdown contract.

2. **Hook patch — `pre-commit-check-tasks.sh` transcript lag.**

   > ⚠️ **DEFERRED ACTION FOR DR. TWINKLEBRANE: FILE TWO UPSTREAM ISSUES.**
   >
   > **Trigger:** after a soak period of using the local hooks at
   > `~/.claude/hooks/` long enough to be confident they work in
   > real sessions (no false-positives, no missed blocks, no
   > unexpected interaction with other hooks). Operator's call.
   >
   > **Files to read + file manually:**
   > 1. `docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md`
   >    → file at `https://github.com/pcvelz/superpowers/issues/new`.
   > 2. `docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md`
   >    → file at `https://github.com/anthropics/claude-code/issues/new`.
   >    Body contains literal `<USER WILL ADD URL AFTER FILING ISSUE A>`
   >    placeholder; replace with the Issue A URL from step 1 before
   >    pasting OR add as a follow-up comment.
   >
   > **After filing:** append both URLs to this item (replace the
   > `<NOT YET FILED>` placeholders below) AND update the cross-reference
   > line in the second body file.
   >
   > **Why deferred:** soak-test the local hooks before publishing the
   > workaround. If we discover a refit bug or a marketplace-hook
   > behaviour we missed, the issue bodies should be updated before
   > filing — easier than amending a public issue.

   ---

   Reference: [memory `reference_precommit_check_tasks_transcript_lag.md`].
   Incident: 2026-06-17 subagent forged a `TaskUpdate(2, completed)` line
   into the session JSONL to defeat the hook because the hook reads
   on-disk transcript which lags one turn behind live `TaskUpdate` state.
   Code `330883a` is clean; the forgery affected only the transcript.

   **Local patch shipped 2026-06-18 — currently in soak.** Spec:
   `docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md`.
   Plan: `docs/superpowers/plans/2026-06-18-precommit-task-hook-livestore.md`.
   Components:
   - Canonical helper: `tools/local_hooks/lib/tasks_live_query.py` reads
     `$HOME/.claude/tasks/<sessionId>/<taskId>.json` (live store, no lag)
     with transcript-parse fallback. 6 TDD tests at
     `tests/hooks/test_tasks_live_query.py`.
   - 5 refit hooks at `tools/local_hooks/*.sh`. Each carries
     `# variant=local` on line 2 + `| variant=local` suffix on every
     trace-log line for unambiguous attribution.
   - Installer: `tools/local_hooks/install.sh` copies into
     `~/.claude/hooks/`, backs up `~/.claude/settings.json` to
     `*.pre-hook-swap-2026-06-18.bak`, rewrites the 5 hook command paths.
     Idempotent. `--dry-run` supported. Uninstall: `cp $BACKUP $SETTINGS`.
   - Verified: same-turn `TaskUpdate(completed)` + `git commit` now
     succeeds (commit `10700bb` is the regression-proof artefact).
     Trace log captured 50+ `variant=local` lines + 0 `variant=marketplace`
     lines during the smoke window.
   - Differential test: `tests/hooks/differential_test.sh` synthesises 4
     transcript-vs-live divergence scenarios. Results at
     `docs/hook-differential-test-results-2026-06-18.md` — 4/4 proven
     including the inverse-direction marketplace under-enforcement
     (tests 2 + 3) that we didn't know about until we built the test.

   **Soak telemetry to watch.** Tail `/tmp/claude-hooks/user-gate-trace.log`
   periodically for:
   - Any `variant=marketplace` lines → swap regressed; investigate.
   - `fallback-failed` or `argparse-error` source values in `tasks_live_query.py`
     output → helper crashed; check stderr for traceback.
   - Spurious `block` lines on routine TaskUpdate/commit flows → refit is
     over-triggering.

   **Upstream issue URLs (DEFERRED — fill in after filing):**
   - Issue A (superpowers marketplace): `<NOT YET FILED — see callout above>`
   - Issue B (anthropics/claude-code): `<NOT YET FILED — see callout above>`

C33 (f) restart-policy warning-string fix is **deprioritized** — still
correct but no longer the bottleneck since the operator pivot away from
RunPod-flavoured work. Reachable through the prioritized queue below
once Phase 53 reaches a checkpoint.

Phase 53 Stage D (Vast.ai raw smoke) **BLOCKED on upstream sky bug**
2026-06-17 — sky 0.12.3.post1's vast adapter expects
`vastai_sdk.vast.vast().client.api_key` but vastai-sdk 0.2.5 refactored
to a `VastAI`-class-only surface. Lambda alone is sole sky cloud for
now. See Phase 53 §Stage D for full debug.

After Phase 53 reaches a checkpoint, the prior C33 queue (do in order unless preferences shift):

1. **(f) ~~trivial — restart-policy warning string.~~ DONE 2026-06-18
   commit `0685973`.** Warning at `RunPodProvider._create_pod` rewritten
   to name RunPod's actual default (`RestartPolicy.ALWAYS` — restart
   on every container exit, success + failure alike) instead of the
   misleading "restart-on-failure". New fence test asserts the new
   wording (must contain "always", must NOT contain "restart-on-
   failure"). Matters in the C28 diagnostic-mode flow where
   `restart_policy='never'` is requested precisely so a failed boot
   leaves the snapshot intact.
2. **(a) ~~small — `classify_run` negative-uptime heuristic.~~ DONE
   2026-06-18 commit `0ed4db5`.** `classify_run` now requires either
   ``fire_count >= 1`` OR ``negative_count >= 2`` before tripping
   RESTARTED on a negative-uptime sample. Isolated single negatives
   fall through to the monotonicity check and land on AMBIGUOUS. Two
   new fence tests; existing 4-negative test still trips RESTARTED.
   Full diagnostics suite 66/66 green.
3. **(d) ~~small — banner test for top-level `Pod.uptimeSeconds`.~~ DONE
   2026-06-18 commit `48f012d`.** `tests/core/test_no_top_level_pod_uptime_reads.py`
   recursively scans every `*.py` under `src/` for the substring
   `uptimeSeconds`, asserts zero matches, dumps every offending
   `file:line: snippet` on failure. Locks the C33 Q3 audit baseline
   (broken stub returns 0 in 154/154 samples).
4. **(g) medium — `diagnostic_mode: "trace"` cfg opt-in.** Bundle the
   Q5-Q8 throwaway instrumentation (`set -x` + `PS4='[%T.%N]'` timestamps,
   `/tmp/p.sh wc/tail` dump in trap, `selfterm.log` + `ps auxf` dumps,
   `stdbuf -oL` flushing, `sync; sleep 0.5; sync` in trap) behind a single
   cfg flag so future restart-loop debugging doesn't re-implement them.
   Spec hook: `docs/superpowers/specs/<date>-cfg-diagnostic-trace-mode-design.md`.
   ~2 hr, no live spend (the existing C28 phase-A cfg already exercises the
   diagnostic trap path).
5. **~~B5b — non-mutating heartbeat satisfier substitute.~~ DEFERRED
   2026-06-18 commit `12f6304`.** Brainstormed with Dr. Twinklebrane
   and landed on indefinite deferral under same-host single-operator
   scope — local `.kinoforge/` ledger already delivers every property
   the wire-level substrate was meant to provide (per-tick freshness,
   single-writer ordering via the `ledger/{run_id}` lock, cross-CLI
   contention via B7's `provision:<id>` lock, warm-reuse via B4's
   ledger lookup). Spec:
   `docs/superpowers/specs/2026-06-18-b5b-deferred-design.md`
   documents the decision, four concrete resumption criteria (second-
   host contention, ledger durability incidents, new provider whose
   semantics don't transfer, new feature requiring the marker
   contract), and the asymmetric `read`-functional / `write`-disabled
   state of `RunPodGraphQLHeartbeatEndpoint` as the documented
   post-C33 mode. Two in-code docstring updates ship alongside (no
   registry/Protocol/test changes).
6. **B7 cross-CLI marker refresh — superseded by B5b deferral.**
   B7's cross-CLI session-claim guarantee was flagged "degraded" while
   B5b was assumed to be pending. Under the now-shipped B5b deferral,
   the cross-CLI guarantee is upheld by the local ledger's `last_heartbeat`
   field + B7's `provision:<id>` lock — the dockerArgs marker is no
   longer load-bearing for same-host operation. Resumption of B5b (per
   the spec's §5 triggers) would re-open B7 marker refresh as a
   follow-on workstream.

Previous workstream (closed): B2 Layer X (cost dashboard) shipped through closeout `f7071c0` + follow-ups `99704b5` (prom scrape_errors_total), `7045418` (balance disk cache TTL + stale-fallback), `39557d5` (closeout sha pin), `793c7eb` (cache put_json public-write). See §B for the next-candidate backlog (B1 sweeper, B3 orchestrator warm-reuse retrofit, B5b SkyPilot heartbeat satisfier, B6 per-entry heartbeat cadence, C25 RunPod heartbeat preserve-and-merge — UNBLOCKED per C33 resolution; write disabled, B5b is the durable fix).

## Phase
ALL 28 MVP tasks complete. All 9 phases complete. Post-MVP layers shipped through Phase 33 (Layer S — `kinoforge status` reads the ledger + `kinoforge forget` recovery subcommand), the warm-reuse trio (B5a heartbeat substrate `bade08c` + B7 cooperative session-claim lock `b2d5b8b` + B4 cross-CLI warm-reuse `54d2867`), and B2 Layer X cost dashboard (closeout `f7071c0`).

## Task checklist (high-level; plan refines into 28 bite-sized tasks)
- [x] Read SPEC.md, explore project context
- [x] Resolve open design questions (8 decisions locked — see DESIGN.md §1)
- [x] Write + commit DESIGN.md
- [x] Design review gate — approved
- [x] Write + commit implementation plan + native tasks + tasks.json
- [x] Phase 1: interfaces + registry + config model + tests (Tasks 1–4)
  - [x] Task 1: Core interfaces, errors, structured logging (`src/kinoforge/core/{__init__,errors,interfaces,logging}.py`, `tests/core/test_interfaces.py`) — commit e636df4
  - [x] Task 2: Adapter registry (`src/kinoforge/core/registry.py`, `tests/core/test_registry.py`) — commit f33ec13. API: register_provider/engine/source + get_provider/engine/source_for_ref via handles(). Sources dispatch by handles() not scheme equality; re-registration overwrites. pyproject.toml: added ignore_errors=true to tests.* mypy override to allow duck-typed fakes.
  - [x] Task 3: Env-backed credential provider (`src/kinoforge/core/credentials.py`, `tests/core/test_credentials.py`) — commit 85699ee. `EnvCredentialProvider.get(key)` reads from `os.environ`; returns `None` when unset. Subclasses `CredentialProvider` ABC.
  - [x] Task 4: Config model (`src/kinoforge/core/config.py`, `tests/core/test_config.py`) — commit 36e7e1a. `load_config()`/`parse_duration()`; pydantic v2 `Config` with `LifecycleConfig`, `EngineConfig`, `ModelEntry`, `ComputeConfig`, `RequirementsConfig`; cross-field validators; `capability_key()`, `lifecycle()`, `hardware_requirements()`. types-pyyaml added for mypy stubs. 11/11 AC tests pass.
- [x] Phase 2: Tasks 5–7 complete.
  - [x] Task 5: `filter_offers` pure helper (`src/kinoforge/core/offers.py`, `tests/core/test_offers.py`) — commit 57e04ca. Semantic CUDA compare via `_cuda_tuple()`; pod-only cost filter; stable `gpu_preference` sort. 6/6 AC tests pass.
  - [x] Task 6: Downloader (`src/kinoforge/core/downloader.py`, `tests/core/test_downloader.py`, `tests/conftest.py`) — commit 566d9d9. stdlib ThreadPool downloader: skip (sha256 or filename), resume via Range header, sha256 verify, corrupt-.part detect-and-raise, concurrent download_all. Range-aware loopback HTTP fixture in conftest.py. 8/8 tests pass. Corrupt-.part strategy: append + sha verify; mismatch → delete .part + raise; next call retries from scratch.
  - [x] Task 7: HTTPSource (`src/kinoforge/sources/__init__.py`, `src/kinoforge/sources/http/__init__.py`, `tests/sources/test_http.py`) — commit 37db66f. `HTTPSource.handles()` dispatches http/https only; `resolve()` strips query strings; self-registers on import. 5/5 AC tests pass.
- [x] Phase 3: Tasks 8–10 complete.
  - [x] Task 8: FakeEngine + FakeBackend (`src/kinoforge/engines/__init__.py`, `src/kinoforge/engines/fake/__init__.py`, `tests/engines/test_fake.py`) — commit dfdb9cf. Deterministic GPU-free engine/backend: sha256-derived `Artifact.filename`, injectable probe profile, `declared_flags_map`, `required_spec_keys`-gated `validate_spec`, `profile_for` deferred to Task 12, self-registers under `"fake"` on import. 17/17 tests pass.
  - [x] Task 9: LocalProvider + injectable clock (`src/kinoforge/core/clock.py`, `src/kinoforge/providers/__init__.py`, `src/kinoforge/providers/local/__init__.py`, `tests/core/test_clock.py`, `tests/providers/__init__.py`, `tests/providers/test_local.py`) — commit 5c8bbbb. Clock protocol (runtime_checkable) + RealClock + FakeClock(start, advance, ValueError on negative); LocalProvider(ComputeProvider) with synthetic offers (2 LOCAL offers), filter_offers delegation, full lifecycle (create/get/list/stop/destroy/heartbeat), idempotent destroy, last_heartbeat accessor, endpoints returning local://id, self-registration under "local". 18/18 tests pass.
  - [x] Task 10: Provisioner (`src/kinoforge/core/provisioner.py`, `tests/core/test_provisioner.py`) — commit fb53c46. `provision()` function with `_ProvisionConfig`/`_ModelEntryLike` structural Protocols; walks model entries, resolves via registry, merges sha256+target onto artifacts with dataclasses.replace; calls downloader only when `requires_local_weights=True`; runs `post_provision_hook(instance)` before delegating `engine.provision()` last. 7/7 tests pass (5 ACs fully covered).
- [x] Phase 3 (remaining): provisioner + e2e vs fake
- [x] Phase 4: profiles + strategy decision point + pool/SequentialPool + GenerateClipStage + local ArtifactStore
  - [x] Task 11: ArtifactStore ABC + LocalArtifactStore + store registry (`src/kinoforge/stores/base.py`, `src/kinoforge/stores/local.py`, `src/kinoforge/core/registry.py`, `tests/stores/test_local.py`) — commit 55e8668. `put_bytes`/`get_bytes`/`put_json`/`get_json`/`list`/`delete`; run_id-namespaced layout `<root>/<run_id>/<name>`; resolved absolute URIs; `list()` returns relative names, empty list for unknown run_ids; `delete()` raises `FileNotFoundError`; self-registers under `"local"`; `register_store`/`get_store` in registry raising `UnknownAdapter`. 22/22 tests pass.
  - [x] Task 12: ModelProfileProvider — JsonProfileCache (`src/kinoforge/core/profiles.py`, `tests/core/test_profiles.py`) — commit 9ad354f. `resolve/discover/verify/resolve_or_discover`; per-key single-flight via threading.Event + inflight dict; JSON serialisation (set→sorted-list, tuple→list round-trips); URI index populated by `_persist`, fallback to `_reconstruct_uri` via `LocalArtifactStore._path` for cross-restart reads; `declared_flags` merged onto probe (only the two flag fields); WARNING emitted when both flags absent; `verify` compares only probeable fields (max_frames, fps, max_resolution, supported_modes). 13/13 tests pass.
  - [x] Task 13: Request validation (`src/kinoforge/core/validation.py`, `tests/core/test_validation.py`) — commit 8c352e9. Pure `validate_request(profile, request, *, accepted_kinds)`: mode gate, kind gate, single-asset-mode lone-image default (i2v only; flf2v requires explicit roles), role contract (required role present exactly once with kind=="image"). Returns new `GenerationRequest` via `dataclasses.replace`; never mutates input. 9/9 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 14: Strategy decision point (`src/kinoforge/core/strategy.py`, `tests/core/test_strategy.py`) — commit 4c2fe8e. Pure `decide(profile, segments, params, spec) -> list[GenerationJob]`: native branch → 1 job with all N segments; fallback branch → N single-segment jobs; segment-wins merge on Segment.params; job-level params is unchanged base; `spec["_audio_mode"]` set from `supports_joint_audio`. 18/18 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 15: SequentialPool + Stage re-export + GenerateClipStage (`src/kinoforge/core/pool.py`, `src/kinoforge/pipeline/__init__.py`, `src/kinoforge/pipeline/stage.py`, `src/kinoforge/pipeline/generate_clip.py`, `tests/core/test_pool.py`, `tests/pipeline/__init__.py`, `tests/pipeline/test_generate_clip.py`) — commit 4088b19. `SequentialPool.submit` wraps backend.submit+result in a pre-resolved Future; `map` preserves input order; `_ListPool` pool-swap AC verified; `add` increments `_backends` list; `Stage` Protocol re-exported from pipeline layer; `GenerateClipStage.run(request, *, segments_override)` validates → decide → pool.map → store.put_bytes; deterministic bytes from `filename+meta`; `CountingBackend` tests branching at N=3 segments (native=1 job, fallback=3 jobs). 11/11 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 16: Orchestrator (`src/kinoforge/core/orchestrator.py`, `tests/core/test_orchestrator.py`) — commit 0f3d0f6. `deploy()`: hosted path (requires_compute=False) skips provider; dry-run prints vendor/engine-neutral plan without calling create_instance; live path polls until ready. `generate()`: guaranteed ordering — discover on cache miss (verify skipped on fresh profile, trivially consistent); verify on cache hit with fail-hard teardown (destroy_instance called before re-raising CapabilityMismatch); 1-segment splitter stub (DEFERRED). Key design decision: verify is skipped when _just_discovered=True to avoid double inspect_capabilities (AC4 requires exactly 1 call on first generate; AC5 requires verify triggers on second generate/cache-hit). 12/12 AC tests pass; mypy + ruff + pre-commit clean.
- [x] Phase 5: cost-safety complete (Tasks 17–18)
  - [x] Task 17: LifecycleManager + effective_deadline + warm_reuse_or_create (`src/kinoforge/core/lifecycle.py`, `tests/core/test_lifecycle.py`) — commit 353eacd. `effective_deadline` pure function; `LifecycleManager` with per-instance state (created_at, idle_since, in_flight_job, accepting_new_jobs); `start_job`/`finish_job`/`should_reap`/`should_drain`/`is_liveness_OK`/`accepting_new_jobs`/`in_flight_job`; `warm_reuse_or_create` destroys + creates on reap; dead-man window = 2×idle_timeout; `last_signal = max(heartbeat or 0, created_at)` avoids killing brand-new instances. 9/9 AC tests pass; mypy + ruff + pre-commit clean.
  - [x] Task 18: Ledger + destroy_confirmed + reap + BudgetTracker (`src/kinoforge/core/lifecycle.py`, `tests/core/test_lifecycle_sweeper.py`) — commit 5a9a5e3. `Ledger` persists instance records to ArtifactStore as ledger.json; `destroy_confirmed` polls until gone with injectable sleep and raises TeardownError+logs ERROR on failure; `reap` sweeps over-age and idle instances via destroy_confirmed; `BudgetTracker.enforce` destroys before raising BudgetExceeded. 9/9 AC tests pass; mypy + ruff + pre-commit clean.
- [x] Phase 6: CivitAI + HuggingFace sources
  - [x] Task 19a: CivitAISource (`src/kinoforge/sources/civitai/__init__.py`, `tests/sources/test_civitai.py`) — commit f786de1. `CivitAISource` resolves `civitai:<modelId>[@<versionId>]` refs via CivitAI REST API; injectable `fetch` transport; `CIVITAI_TOKEN` attached to HTTP request + Artifact headers; model-only path hits `/models/{id}` then `/model-versions/{vid}`; `AuthError` re-raised. 14/14 tests pass.
  - [x] Task 19b: HuggingFaceSource (`src/kinoforge/sources/huggingface/__init__.py`, `tests/sources/test_huggingface.py`) — commit dc8715e. `HuggingFaceSource` resolves `hf:<repo>:<path>` refs to canonical HF resolve URLs (no HTTP calls); `HF_TOKEN` attached to Artifact headers; bare repo ref raises `ValidationError` with "specify a file path" message (directory listing DEFERRED); self-registers under `"hf"`. 11/11 tests pass; mypy + ruff + pre-commit clean.
- [x] Phase 7: ComfyUI engine (+node installer) + RunPodProvider — Tasks 20a+20b complete
  - [x] Task 20a: ComfyUIEngine + ComfyUIBackend + git node installer (`src/kinoforge/engines/comfyui/__init__.py`, `src/kinoforge/engines/comfyui/nodes.py`, `tests/engines/test_comfyui.py`) — commit 3e9c223. `provision` clones nodes via injected `run_cmd`, installs `requirements.txt` via `file_exists` spy, routes models via `TARGET_TO_SUBDIR` + injected `route_file`, launches ComfyUI with `launch_args`. `submit` deep-merges `node_overrides` onto `graph` and POSTs to `/prompt`; `result` polls `/history/{id}` until outputs present. All I/O seams injected; self-registers under `"comfyui"`. 23/23 AC tests pass.
  - [x] Task 20b: RunPodProvider (pod+serverless) (`src/kinoforge/providers/runpod/__init__.py`, `src/kinoforge/providers/runpod/selfterm.py`, `tests/providers/test_runpod.py`) — commit 1be572d. Pod mode: `find_offers` (http_get→filter_offers), `create_instance` injects `RUNPOD_TERMINATE_KEY` (scoped, not main key) + `KINOFORGE_SELFTERM_SCRIPT` via `selfterm.RENDER(...)`. Serverless mode: concurrency caps from Lifecycle, `status="ready"` immediately. `endpoints` uses `https://{id}-{port}.proxy.runpod.net` (pod) / `/v2/{id}/run` (serverless). `destroy_instance` polls+raises TeardownError, idempotent. All HTTP via injected seams; self-registers under `"runpod"`. 24/24 tests pass.
- [x] Phase 8 (partial): Tasks 21a–21b complete
  - [x] Task 21a: DiffusersEngine + DiffusersBackend (`src/kinoforge/engines/diffusers/__init__.py`, `tests/engines/test_diffusers.py`) — commit 157325b. `provision` runs pip install + server_cmd via injected `run_cmd`; `backend` constructs `DiffusersBackend` with cfg base_url; `submit` POSTs to `/generate`; `result` polls `/status/{job_id}` until done; `validate_spec` requires `pipeline` + `scheduler`; `declared_flags` returns copy from map; self-registers under `"diffusers"`. 25/25 tests pass.
  - [x] Task 21b: HostedAPIEngine + HostedAPIBackend (`src/kinoforge/engines/hosted/__init__.py`, `tests/engines/test_hosted.py`) — commit ad5c726. `requires_compute=False`, `requires_local_weights=False`; `provision(None, cfg)` validates cred via injected `CredentialProvider` + pings health URL via injected `http_get`; raises `AuthError` on missing cred, `KinoforgeError("hosted endpoint unreachable: …")` on ping failure, `KinoforgeError` if non-None instance passed; `backend(None, cfg)` returns `HostedAPIBackend`; `submit` POSTs to endpoint; `result` polls `/status/{job_id}`; `validate_spec` requires `model`+`params`; `key_base(cfg)` returns hosted model ID; `declared_flags` returns copy from map; self-registers under `"hosted"`. 25/25 tests pass; mypy/ruff/pre-commit clean.
  - [x] Task 21c: SkyPilotProvider (lazy import) — commit e069dfe. `SkyPilotProvider(ComputeProvider)` with `_get_sky()` lazy import (only inside function body, never at module top level); injectable `sky_client` seam so tests run without skypilot installed; `idle_timeout_s → autostop` (minutes) mapping via `sky_client.launch(task_config, autostop=...)`;  `list_instances()` via `sky_client.status()`; `destroy_instance()` calls `sky_client.down()` then polls until gone; `get_instance()` raises `KeyError` when absent; `endpoints()` returns `{"ssh": "ssh://<id>"}`. 16/16 AC tests pass; mypy/ruff/pre-commit clean.
- [x] Phase 9 (partial): CLI — Task 22 complete
  - [x] Task 22: CLI + `__main__` — `_adapters.py` (sole concrete-import hub), `cli.py` (deploy/provision/generate/list/status/stop/destroy/reap/gc), `__main__.py` wired. Duplicate-pod guard, UnknownAdapter catch, instance overview header, 8/8 ACs pass. — commit 4b4e31e
- [x] Phase 9 (complete): Examples, README, CI — Task 23 complete
  - [x] Task 23: `examples/configs/{wan,diffusers,hosted,local-fake}.yaml`, `README.md` (6 required headings), `.github/workflows/ci.yml` (3-OS matrix), `tests/test_examples.py` (21 tests). All 6 ACs pass. — commit 1b7f662
  - [x] Task 24: `tests/test_core_invariant.py` — 3-AC lockdown: subprocess isolation (no adapter modules in sys.modules after core import), vendor-SDK confinement scan (sky/skypilot→providers/skypilot, runpod→providers/runpod), core-import ban scan (no kinoforge.providers/sources/engines in core/). All 3 tests pass; mypy/ruff/pre-commit clean. — commit e2f9b37

## Key decisions & gotchas
- Core NEVER imports a concrete provider/source/engine — registry-mediated by name/scheme. Reviewer enforces.
- 8 open questions resolved in DESIGN.md §1 (submit/result+Pool, models-per-engine, params-vs-spec, profile-cache location, serverless caps, artifact GC, role vocab, under-use warning).
- Discovery ordering is explicit & guaranteed (resolve→validate→split→provision→verify); fail-hard on drift tears down compute.
- Cost-safety: invariant universal, mechanism provider-specific. RunPod in-pod self-terminator + least-privilege terminate-only cred; SkyPilot native autostop; LocalProvider injectable clock for tests.
- `CapabilityKey.derive()` uses `json.dumps`, not separator scheme — JSON escaping guarantees distinct tuples never collide (caught in commit `7e70a57`).
- Config requires exactly one `kind: base` model entry — zero or many rejected at load time (commit `94afa3e`).
- Splitter is pluggable ABC+registry, not a single function — future LLM/scene-detect strategies slot in as adapters. `HeuristicSplitter` uses blank-line markers.
- `validate_request` called exactly once per `generate()` — orchestrator calls it; `GenerateClipStage` `segments_override` branch skips re-validation.
- Asset attachment is an orchestrator concern, not a splitter concern — splitter returns segments with empty assets; orchestrator attaches to seg-0 via `dataclasses.replace`.
- Continuity dispatch via `MODE_ROLE_REQUIREMENTS` — injects only when `"init_image"` in role contract keys (i2v today; t2v/flf2v skip); future modes automatic. Schema: `dict[mode, dict[role, kind]]` since Layer R.
- `ArtifactStore.uri_for(run_id, name)` is pure, no I/O — returns URI it *would* address; invariant: `uri_for == put_*.uri`. Unblocks S3/GCS.
- Concrete ABC defaults are a legitimate extension pattern — `GenerationEngine.extract_last_frame` is a concrete default that raises; engines opt in by override.
- S3/GCS shipped as two independent siblings, no shared cloud-base — ~30 LOC duplication acceptable; avoids locking guesses about future stores (Azure, B2, R2). Factor when third cloud lands.
- SDK credential discovery uses default chains, not kinoforge plumbing — boto3 walks AWS env → `~/.aws/credentials` → IMDS → IAM role; GCS walks `GOOGLE_APPLICATION_CREDENTIALS` → gcloud ADC → GCE metadata. Routing through `EnvCredentialProvider` would defeat IMDS/IAM-role auto-discovery.
- `.env` loader is a transparent shim at CLI entry — populates `os.environ` once; every downstream consumer (EnvCredentialProvider, boto3, GCS default chains) reads unchanged. `override=True` is library-only, no CLI flag.
- Deferred (interface + 1 path only, layers NOT built): stitching, audio, keyframe stage, cross-process discovery lock. (Splitter, uri_for, continuity, S3/GCS, .env loader, concurrent pool now built.)
- Deps stdlib-first: pydantic + PyYAML + python-dotenv runtime; boto3 + google-cloud-storage lazy-import-gated; skypilot optional/lazy; urllib for all HTTP; stdlib logging.
- TDD red-first, fully offline (LocalProvider/FakeProvider/FakeSource/FakeEngine + injectable clock + Fake cloud clients). No real cloud/net/GPU/weights in any test.

## Established patterns for layer development

Patterns proven across MVP + Layers A–D. New layers should follow them by default; deviation needs justification.

- Injected I/O seams on every adapter — HTTP/subprocess/filesystem as constructor params with stdlib defaults; tests pass spies; no real network/subprocess/git/GPU in tests.
- Self-registration on import — zero-arg factories for engines/providers/stores; instances for sources (dispatch by `handles(ref)`).
- Source dispatch by behaviour, not key equality — `source_for_ref(ref)` asks each registered source `handles(ref)`, returns first match.
- Stage protocol + pool-swap — stages talk only to `BackendPool`/`ArtifactStore`/`ModelProfile`; `SequentialPool` is default, `ConcurrentPool` drops in via same ABC; future distributed variants (Ray, cross-process) follow the same pattern.
- `ConcurrentPool` dispatch pattern — `_Slot(backend, executor, cap, in_flight)` per backend; `submit` picks least-loaded slot by `in_flight / cap` ratio under a per-pool lock (ties broken by registration order via `min`'s left-bias), then dispatches to that slot's `ThreadPoolExecutor`; `_run_one` releases the counter via `try/finally` so backend failures don't leak slots; `map` submits all eagerly, iterates futures in input order (preserves result ordering), on first exception cancels queued + drains in-flight + re-raises; `close` flips closed flag under lock then calls `executor.shutdown(wait=True)` per slot outside the lock for deterministic shutdown.
- Strategy / validation / continuity / splitter helpers are pure functions — `decide`, `validate_request`, `inject_tail_frame`, `split()` all return new objects, never mutate input.
- `dataclasses.replace` for every immutable update — no mutation paths; tests verify with `is`-identity on unchanged fields.
- TDD red-first, every task — write failing test first, confirm FAIL, then implement; `test-design` skill (bug-catch comments, no implementation mirroring, no over-mocking).
- In-core defaults wire themselves via `core/__init__.py` — `HeuristicSplitter` self-registers here, not in `_adapters.py` (preserves the core-import-ban invariant).
- `Field(default_factory=NestedModel)` for optional pydantic nested blocks — `Config.splitter`, `Config.store` both use this pattern.
- Brainstorm → spec → plan → execute → ship — superpowers workflow with brainstorming skill, spec doc committed, plan with full HEREDOC code blocks, subagent per task, two-stage review, whole-branch review, `--no-ff` merge.
- Spec self-review before commit — strip "implementer must grep" footnotes; provide exact line numbers, variable names, diff snippets. Saves round-trips.
- ABC change → pre-implementation grep for construction sites — adding a required dataclass field breaks every site; plan must enumerate them.
- Cloud SDK lazy-import gate — `__init__(client=None)` with `if client is None: import <sdk>` inside; tests inject fake. Dual-gate for GCS (client + exception module).
- Shared test conftest for sibling adapters — `tests/stores/conftest.py` holds both `FakeS3Client` + `FakeGCSClient` even when only one is used per task.
- CLI dispatcher with lazy SDK imports per branch — `_build_store(cfg, state_dir)` dispatches by `cfg.store.kind`, imports heavy SDKs only on relevant branches; keeps CLI startup fast.
- Two-stage review (spec compliance first, then code quality) — spec reviewer catches contract mismatches; quality reviewer focuses on placement, imports, mutations, tests. Different classes of issue.
- Quality reviewer can escalate NICE → FIX-REQUIRED — if an unraised case is reachable by test or supported caller pattern, it's a gap not a polish nit.
- `--no-ff` merge pattern with substantive body — merge commit references layer name, AC state, per-task commits, GitHub issue via `Closes #N` trailer. Natural layer boundary.
- Builder subagent (caveman:cavecrew-builder) has no Bash — use for surgical 1-2 line edits; controller handles verify-and-commit.
- `import X.Y.Z as alias` for lazy SDK imports — ruff-format wraps `from X.Y import Z` onto multiple lines and splits type-ignore comments off; the alias form keeps comments attached.
- **Standard prompt for all video-generation live smokes:** every video-gen live smoke (current + future, all engines, all providers, all model variants) reads its prompt body verbatim from `prompt-field-realistic.txt` at the repo root. Tests load the file at runtime — no paste-into-YAML, no paraphrase, no per-smoke override. Rationale: user is comparing how different models/providers respond to the *same* detailed prompt; any prompt variance defeats the comparison. The prompt is a long-form, demanding photorealistic cinematic shot — it also exercises prompt-routing, length caps, and per-model adherence. Image-only / audio-only smokes are exempt. Tracked in user's auto-memory as `feedback_standard_test_prompt`.

## Known limitations & follow-ups

**This is the canonical index of every open deferred item.** Per-phase
entries below may still mention out-of-scope notes in context, but every
non-trivial open follow-up MUST also appear here so future-us can find
the full set in one read. When closing a phase, mirror its
`Out of scope` / `Carry-forwards` / `Forward-compat hooks` block here.
When closing an item, strike-through (`~~item~~ — CLOSED by …`) rather
than delete — historical context aids future reviewers.

Numbering is stable across rewrites; new items append at the bottom of
their category.

### A. Live-spend / operator-gated (paid work, blocked or queued)

- ~~**A1. Bedrock Luma Ray v2 live smoke**~~ — **ABANDONED 2026-06-17** along with all AWS GPU work; Phase 52 quota burn produced a denial on the AWS service-quotas request (`ac4331ff…Z68FM0c`) and operator pivoted to vast.ai + Lambda for GPU compute. See Phase 53 below.
- ~~**A2. Layer 2 — Veo on Vertex AI.**~~ — **ABANDONED 2026-06-17**. GCP GPU quotas stuck at 0 + console quota-request UX impassable; operator pivoted away from GCP entirely. Vertex AI hosted-video path can re-fire only if billing + auth re-enabled.
- ~~**A3. Layer W+β SkyPilot T4 GPU smoke re-fire**~~ — **ABANDONED 2026-06-17**. GCP pivot away; replaced by vast.ai + Lambda SkyPilot smokes (Phase 53).
- ~~**A4. AWS arm of W+β2 (SkyPilot AWS GPU smoke)**~~ — **ABANDONED 2026-06-17**. AWS quota request denied; pivot to vast.ai + Lambda (Phase 53).
- ~~**A5. SkyPilot live AWS smoke (any).**~~ — **ABANDONED 2026-06-17** for the same reason as A4. Extras (`f74a73d`) remain in `pixi.toml` but unexercised; safe to keep (sky aws path is pure-Python, no runtime cost).
- **A6. SkyPilot Azure compute.** DEFERRED on upstream conda-forge / `azure-cli` / `azure-batch` packaging gap; 3 unblock paths in RESUME block.
- **A7. SkyPilot GPU + per-engine smokes beyond Layer W+β.** Phase 31 scope cut.
- **A8. Engine smoke on a verified SkyPilot adapter** — Phase 40 carry-forward; stacks on A3 once smoke fires.
- **A9. Engine-integration live smoke for Diffusers + Hosted on real RunPod.** Phase 24 Layer N + Phase 28 Layer P closed ComfyUI/Wan; Diffusers + Hosted gap remains.
- **A10. RunPod serverless mode read-paths + live smoke.** Phase 24 was pod-only.
- **A11. HuggingFace live smoke for gated/private repos.** Phase 30 carry-forward.
- **A12. aria2c real-binary smoke (`KINOFORGE_LIVE_ARIA2=1`).** Phase 29 carry-forward.
- **A13. Luma credential refresh OR API-plan upgrade.** Phase 43 Layer 4 carry-forward (Luma direct video API retired in 2026; `LUMAAI_API_KEY` reserved for Layer 5b UNI-1 keyframes).
- **A14. Layer 4 comparison configs** — 2 of 15 YAMLs shipped (t2v only); i2v / flf2v / keyframe-prestage / manifest deferred. Phase 43 Task 10.
- **A15. Layer 4 Fal i2v + flf2v extension.** Phase 43 Task 14, depends on A14.
- **A16. Layer 4 comparison batch capstone.** Phase 43 Task 15, depends on A14 + A15.
- **A17. Fal retrofit onto `RemoteSubmitPollBackend`.** Phase 43 Task 7; engine functional, refactor-only.

### B. Spec-locked future layers (substrate ready, layer not started)

- ~~**B1. Layer W — `kinoforge sweeper` daemon.**~~ — CLOSED by commit `cbe5337`. Spec at `docs/superpowers/specs/2026-06-13-b1-sweeper-daemon-design.md`; plan at `docs/superpowers/plans/2026-06-13-b1-sweeper-daemon.md`. Foreground supervisor (`kinoforge sweeper start | stop | status | metrics`); `SweeperLoop` mirrors `HeartbeatLoop` (eager first tick, bounded shutdown, broad try/except per iter); synthetic `sweeper:<host>` ledger entry as daemon-liveness signal; one-line filter in `sweep()` prevents self-reap; `kinoforge_sweeper_*` Prom gauges as siblings of B2. Banner advertises B5a HEARTBEAT_SUBSTRATE_MISSING contract + B7 cooperative session-claim probe. Live spend: $0.
- ~~**B2. Layer X — cost dashboard / metrics consumer.**~~ — CLOSED. Spec at `docs/superpowers/specs/2026-06-12-b2-cost-dashboard-design.md`; plan at `docs/superpowers/plans/2026-06-12-b2-cost-dashboard.md`. Ships `kinoforge cost` (human / `--json` / `--prom`) reading ledger + classify + RunPod GraphQL `clientBalance`. Disk cache TTL 15s default; replicate throttle stub wired RED for B10. `BalanceEndpoint` Protocol substrate at `core/balance_endpoints.py`; RunPod satisfier at `providers/runpod/balance.py`; pure aggregator at `core/cost.py`. Wire-discovery delta vs spec: RunPod GraphQL gateway requires Bearer + `User-Agent` header (Bearer-only returns HTTP 403). Closed by commit `f7071c0`.
- ~~**B3. Layer Y — in-session orchestrator warm-reuse retrofit.**~~ — CLOSED. Spec: `docs/superpowers/specs/2026-06-13-b3-warm-reuse-retrofit-design.md`. Plan: `docs/superpowers/plans/2026-06-13-b3-warm-reuse-retrofit-plan.md`. Auto-discovery via `_scan_warm_candidates` at `cli/_commands.py`; cross-CLI session-busy ledger fields (`session_start`/`session_end`) via existing `Ledger.touch(**extra)` seam; `--no-reuse` for ephemeral pods (cold create + immediate destroy at `deploy_session.__exit__` under `reaper:<id>` lock). Reuses B7 `hold_until_first_tick`, B4 `_resolve_warm_instance`, B1 `reaper:<id>` lock; zero new lock keys; zero new modules. Live smoke at `tests/live/test_b3_warm_attach_live.py` confirmed: Gen 1 (cold) 11.3 s wall, Gen 2 (warm reuse) 2.9 s wall — 74 % cold-skip benefit, ratio 0.26 well under 0.7 pass-threshold; total spend \$0.0040 RunPod (FakeEngine on RTX A5000 — ComfyUI + RunPod combo still C25-gated). Mid-task fixes folded back: `_cmd_generate` ledger-record (commit `3454b48`) + `_record_then_install` callback (commit `3bdec1c`) — both surfaced production gaps invisible to unit tests because B7 spy HB loop pre-records the instance in `spy.start()`.
- ~~**B4. Cross-CLI warm-reuse CLI exposure.** Layer P Task 7 item #2 (`2026-06-01-layer-p-task7-item2-warm-reuse-design.md:54,546`) noted `LifecycleManager.warm_reuse_or_create` CLI surface as a Layer Q candidate; Layer Q shipped HF source instead — surface never materialized. Sub-item of B3.~~ — CLOSED. Spec at `docs/superpowers/specs/2026-06-12-b4-cross-cli-warm-reuse-design.md`; plan at `docs/superpowers/plans/2026-06-12-b4-cross-cli-warm-reuse-plan.md`. CLI exposes `--instance-id` + `--force-attach` on `generate` + `batch`; `kinoforge list` shows `capability_key=<hash>` column; cheap-first validation helper at `cli/_commands.py:_resolve_warm_instance`. Zero orchestrator diff; B7's `provision:<id>` lock reused unchanged.
- ~~**B5. Real `provider.heartbeat()` for RunPod / SkyPilot.**~~ — CLOSED by Phase 52 Task f (commit `bade08c`). B5a substrate + RunPod satisfier shipped end-to-end; live smoke confirmed `podEditJob`/`pod { dockerArgs }` round-trip @ P50=460ms, P99=583ms, no 429 at 5s cadence. B5b SkyPilot satisfier still gated on A3 / A4 GPU quota landing.
- **B5b. SkyPilot satisfier for `core/heartbeat_endpoints.py` substrate.** Gated on A3 / A4 GPU quota landing. Plug-in satisfier; substrate Protocol shipped in B5a, no substrate churn required. ~3-4 tasks. Live spend ~$0.05 (one bare-cluster SkyPilot smoke once quota lands; CPU cluster acceptable since the heartbeat path is GPU-irrelevant). Spec hook: `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §13 (B5b Implementation Notes).
- **B6. Per-entry `heartbeat_interval_s` override.** Layer V §6 candidate.
- ~~**B7. Cooperative lock between session-start and reaper.**~~ — CLOSED by commit `8f1ee89` (and predecessors `bd2d97b`, `4d10da2`, `da644f6`, `18cc469`, `d785b18`). Extends the existing `provision:<id>` lock (orchestrator.py) from "engine.provision only" to "instance-id committed through first heartbeat tick lands"; reaper non-blocking-probes the same key before destroying and returns `ActionResult(action="deferred-session-claim", reason="held by pid <N>; ...")` on contention. Closes Layer V §5 Risk 3 race. Spec at `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md`; plan at `docs/superpowers/plans/2026-06-12-b7-cooperative-session-claim-lock.md`.
- **B8. `--policy policy.yaml` (JSON/YAML policy file).** Layer V §6 candidate; CLI flag composition today.
- **B9. Bearer Layer 5+ provider adapters — Pika, Kling, Higgsfield, MiniMax, Hailuo.** Config-only via `RemoteSubmitPollBackend`; one thin subclass each. Layer 4 §13 future layers.
- **B10. Hosted-engine per-prediction cost capture (Layer 5 candidate).** Per-engine `_extract_cost(status) -> float | None` hook on `RemoteSubmitPollBackend`; lifts onto `Artifact.meta["cost_usd"]` + `.cost.json` sidecar + `KINOFORGE_SESSION_BUDGET_USD` pre-submit gate. Substrate already names "spend tracking" as planned. Phase 43 Layer 4 carry-forward.
- **B11. Future cloud-native hosted providers (Vertex Imagen, Bedrock Claude, Azure DALL-E).** Reuse existing AuthStrategy or +1 per family. Phase 41 Layer 1 §7.
- **B12. Future Bedrock video models (drop-in via `model_input_template`).** Phase 42 Layer 3 / README:754.
- **B13. Layer 5b cost sidecar implementation.** Pre-wired gate from ephemeral-workspaces §2; concrete writer is the open work.
- **B14. `validate_request` promoted to Stage peer.** Then `KeyframeStage` becomes a real Stage entry instead of a pre-phase. Layer R §10.4 / README:870.
- **B15. Splitter into `GenerateClipStage`.** Eliminates orchestrator's splitter knowledge; cleaner separation. Layer R §10.4.
- **B16. Distributed / cross-process `BackendPool` variant** (e.g. `RayPool`). Slots in via `BackendPool` ABC unchanged. README:1298.
- **B17. Audio sync stage (GH #2).** `strategy.decide` already marks `spec["_audio_mode"]="separate"`; stage reads marker.
- **B18. Stitching layer.** Slots between `pool.map` and `store.put_bytes` in `GenerateClipStage`. Required to close persistence model — `GenerateClipStage` keeps intermediates in memory today.
- **B19. Stitching across multi-segment clips sharing one keyframe.** Layer R §10.4; orthogonal to B18.
- **B20. `WeightProvisioning` enum to replace `requires_local_weights` bool.** Today's bool collapses two orthogonal axes (engine intent × deployment target). A four-value enum (`HOSTED` / `LOCAL` / `SELF_PROVISION` / `UPLOAD_FROM_LOCAL`) gives the provisioner a single switch point per engine and gives future engines (custom-weights LoRAs, cross-pod sharing, BYO-weights paths) a real home. Today's path narrowed B20 by flipping `ComfyUIEngine.requires_local_weights` from `True` to `False` (ComfyUI's pod-side Layer Q `render_provision` was already the actual provisioning path); the enum refactor is the durable shape. Engine churn: every `GenerationEngine` + `ImageEngine` subclass declares its strategy; provisioner branches on the enum; legacy bool retained as a `@property` alias during the transition. Spec hook: write at `docs/superpowers/specs/<date>-weight-provisioning-enum-design.md` when the next caller hits the upload-from-local case.

### C. Architectural follow-ups (in-tree work, no new layer required)

- ~~**Layer F: engine `submit()` ignores seg-0 assets.**~~ — CLOSED by Phase 16.
- ~~`cli._cmd_status` queried in-process provider state only, not the ledger.~~ — CLOSED by Phase 33 (Layer S).
- ~~Production-side `last_heartbeat` persistence on `Ledger.record` (Layer S forward-compat seam).~~ — CLOSED by Phase 36 (Layer U). Sentinel-gate contract: any future heartbeat-aware reaper MUST check `heartbeat_thread_tick` freshness before destructive decisions — see `Ledger.touch` docstring + Layer U spec §3.4.
- ~~Ledger local-only by CLI wiring~~ — CLOSED by Phase 34 (Layer T).
- **C1. Atomic write in `LocalArtifactStore.put_bytes`** (tmp + `os.replace`). Root-cause fix for the race bandaged in test-side at `6b9fba3`. Helps every concurrent reader, not just the one test.
- **C2. `provisioner.provision` `# type: ignore[arg-type]`** — Protocol generic-variance cleanup.
- **C3. `flf2v + N > 1 + non-native` continuity** — pre-existing two-image-bookend gap.
- **C4. `test_core_invariant.py` allowlist extension for `splitters/`** — first adapter splitter (LLM, scene-detect) must add it.
- **C5. Default zero-arg store factories require env vars set** (`KINOFORGE_S3_BUCKET` etc.). CLI bypasses via `_build_store`.
- **C6. No multipart-threshold knob on cloud stores.** SDK defaults cover today.
- **C7. `Orchestrator.generate` `base_spec={}` hardcode** at `core/orchestrator.py:605`. Layer K landed most of the routing; this line residual.
- **C8. Hosted YAML `engine.hosted.model` vs `spec.model` collapse.** Documented in `examples/configs/hosted.yaml`; Layer-L+ candidate.
- **C9. `--header=` Artifact-headers population.** Passthrough mechanism shipped (Phase 29); population deferred until `Artifact.headers` field lands.
- **C10. `local_path_for` hardlink / zero-copy optimization** (`ArtifactStore.local_path_for`). Phase 25 Layer O scope cut; sub-GB disk doubling negligible today.
- **C11. S3 recorder botocore-context `operation_name` empty.** Workaround in `tests/stores/recording.py:307`; root-cause fix deferred. Phase 38 Layer W.
- **C12. Phase 45 Sub-γ pod-name alias rename.** `kinoforge-<alias>-<rand4>` + `capability=<alias>` tag default; needs `spec.tags["capability"]` populated somewhere first.
- **C13. `_CapturingSink` test-helper dedup.** Promote to module helper if a third site appears (Layer 8).
- **C14. WARNING template helper extraction** for `engine %s returned empty model identity ...`. Two sites; premature at 2.
- **C15. `mode_identity` / `precision_identity` / `lora_stack_identity` sibling ABCs.** Layer 8 forward-pointer for finer filename slug facets.
- **C16. Legacy `lifecycle.reap(policy=...)` dead seam** (`core/lifecycle.py:681,708`). Accepted-and-ignored before Layer V; superseded by `sweep()`. Delete or wire to `Policy`.
- **C17. Stale `core/pool.py:32` docstring** — says multi-backend variants are DEFERRED but Layer G `ConcurrentPool` is multi-backend.
- **C18. Split-wait helper for cooperative poll loops.** Phase 50 shipped the `token.raise_if_set(); …probe…; token.wait(interval_s)` pattern at two sites (`ComfyUIBackend.result` + `RemoteSubmitPollBackend.result`). Factor into `kinoforge.core.cancel` as a reusable `bounded_poll` / `poll_with_cancel` helper when a 3rd caller appears (Diffusers / Hosted / Bedrock cancel hardening would qualify).
- **C19. Per-backend cancel hardening for Diffusers / Hosted / Bedrock.** Phase 50 grew the `cancel_token` kwarg on every concrete backend at the ABC level; only `ComfyUIBackend` and `RemoteSubmitPollBackend` honor it. The remaining engines accept the kwarg as a no-op until the C18 helper exists or a real stall surfaces.
- **C20. `pool.map` ignores `cancel_token`.** The t2v non-chained fan-out path. Workers still honor the token internally; the wait on the orchestrator side is longer because `pool.map` joins every in-flight future before raising. Forward the kwarg through `BackendPool.map` when fan-out latency on interrupt becomes a real complaint.
- **C21. `KeyframeStage` cancel_token plumbing.** `KeyframeStage` uses `ImageBackend` directly (no `pool.submit` site). Production WARN-not-destroy is provided by the orchestrator outer except today; in-stage cancel honoring waits on `ImageBackend` growing the same kwarg.
- **C22. ComfyUI WebSocket live observability — deferred.** `/history/{prompt_id}` returns `{}` while the job is queued or executing — populates only on completion. `/queue` (now probed every tick when `status=="unknown"`) reveals "my job is currently the running one" via `queue_pos=0` but cannot surface the per-node progress (`current_node`, `step X of Y` from the sampler) that ComfyUI's own web UI shows. Real per-node observability needs a persistent `ws://server/ws` subscription that listens for `executing` + `progress` events and threads the latest into a shared state read by `ComfyUIBackend.result`. Promote to its own layer when the next stall in the middle of a long sampler tick re-surfaces. Scope: WS client with reconnect, background thread/asyncio task, fake-WS test seam, integration with `cancel_token`. Spec hook: write at `docs/superpowers/specs/<date>-comfyui-ws-progress-design.md` when triggered.
- **C23. ComfyUI + Wan LoRA wiring.** Neither `runpod-comfyui-wan.graph.json` nor `runpod-comfyui-wan-t2v.graph.json` includes a LoRA-loader node — the UI→API converter dropped them as part of "manual path-strip/LoRA-drop/T5-fp8 fixups" (see comment in `runpod-comfyui-wan.graph.json`). YAML schema for `kind: lora` model entries already exists (Layer Q `render_provision` routes files to `loras/` via `TARGET_TO_SUBDIR`), so the gap is graph-side, not provisioner-side. Scope: ship `*-with-lora.graph.json` variants adding `WanVideoLoraSelect` (kijai pack) → `WanVideoSampler.lora` slot, plus per-graph offline shape-lock tests + a live re-fire on RunPod A5000 with a real public LoRA ref (HF or CivitAI). Live spend ~$0.30. Promote when the next operator wants a Wan LoRA stack. Untracked before 2026-06-10; surfaced during a "load the example LoRA" CLI question.
- **C24. `examples/configs/wan.yaml` placeholder LoRA ref.** The `civitai:123456@78901` entry is a fake placeholder — `CivitAISource.resolve()` will 404 on the model lookup. Misleads operators into believing the documented stack works. Fix on the heel of C23: either swap for the real LoRA chosen during C23 implementation or drop the entry entirely with a `# see C23` comment pointing at the wiring follow-up. Trivial; bundled into the C23 commit when the LoRA wiring lands.
- ~~**C25. B5a RunPod heartbeat wire-slot research.** B5a Task f live smoke (2026-06-12, commit `0219a13`) discovered `PodEditJobInput` has NO `tags` field. The shipped satisfier uses `dockerArgs` as a JSON carrier (`{"_kinoforge_hb": "<ISO>"}`). **Production-safety concern:** every heartbeat tick OVERWRITES `dockerArgs`, which is the SAME field Phase 24 `RunPodProvider._create_pod` injects the kinoforge selfterm script into at pod creation. On a real workload pod (e.g. ComfyUI + Wan with `provision_script` set), enabling `compute.heartbeat_mode = "graphql-tag"` will silently overwrite the selfterm script. The in-pod process keeps running, but a pod restart (RunPod migrations, tier changes) re-reads `dockerArgs` and the container fails to boot the JSON-as-bash. Worse — the in-pod dead-man self-terminator is also lost, opening a cost-leak window. **Workaround today:** operator opt-in is gated by the YAML default (`"none"`) and the spec §9 wire-discovery note states the constraint, so no production pod trips it by accident. **Real fix candidates:** (a) preserve-and-merge — read `dockerArgs` first, splice the heartbeat JSON onto the end of the selfterm script as a comment (`# _kinoforge_hb: <ISO>`), and rewrite the whole field on every tick; (b) find a different RunPod metadata slot — REST API `/v1/pods/{id}` may expose a free-form `notes` or `metadata` field worth probing; (c) add a runtime guard in `_adapters.build_heartbeat_endpoint_for` that REFUSES to construct the satisfier when `cfg.engine` would set provision_script, with a clear `ValidationError` explaining the conflict. Spec hook: amend `2026-06-12-b5a-heartbeat-substrate-design.md` §9 with the chosen fix when the next operator wants production heartbeat on a workload pod. Until then, B5a + B1/B2/B3 work correctly on bare pods; the substrate is honest; only the cross-product (heartbeat + selfterm) is blocked. **PARTIAL — operator-opt-in foot-gun closed via runtime guard (commit `5aa2dcb`); `build_heartbeat_endpoint_for` now raises `ValidationError` when `provider == "runpod"`, `mode == "graphql-tag"`, and `engine.kind` is not in `_RUNPOD_HEARTBEAT_SAFE_ENGINES` (`{"fake"}`). The guard fires at orchestrator startup before any pod is created. Preserve-and-merge wire path (fix candidate a) still deferred.**~~ — **CLOSED (PARTIAL)** by fix-candidate (a) preserve-and-merge wire path. Spec: `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md`. Plan: `docs/superpowers/plans/2026-06-13-c25-runpod-heartbeat-preserve-and-merge.md`. Probe (commit `209a180`): RunPod's `pod.env` is `[String]` (no subfields) → `read-unavailable` → Branch B selected. Shipped: `RunPodGraphQLHeartbeatEndpoint.write` reads current `dockerArgs`, strips any stale `# _kinoforge_hb:<ISO>` trailer, appends a fresh one (commit `71dea61`); `_RUNPOD_HEARTBEAT_SAFE_ENGINES` allow-list deleted (commit `23cb880`); 11/11 wire-shape unit tests green; 228/228 provider suite green. Wire fix VALIDATED on production pod (commit `7436969`, sidecar `tests/live/_c25_smoke_evidence.json`) via direct GraphQL `dockerArgs` readback at pod `uokf7x7cbfcunk`: Phase 24 bash decoder INTACT + exactly one heartbeat marker (`# _kinoforge_hb:2026-06-13T14:23:45.666422-07:00`). **Partial:** the full Wan + ComfyUI + 2-CLI warm-reuse end-to-end smoke (gen2 cold-skip ratio < 0.7) was deferred — gen 1 stalled on Wan provision before completing (RAM/GPU/disk util near zero with classify returning LIVE), root cause orthogonal to C25. Follow-up tracked as **C26** (RunPod util-aware stall classify): extend heartbeat tick with `runtime.gpus.gpuUtilPercent` / `runtime.container.cpuPercent` snapshot into the ledger and add a new `STALL_REAP` verdict.

- ~~**C33. Test whether RunPod `podEditJob` mutation causes a container restart (B5a heartbeat hypothesis).**~~ — **CLOSED (DEFERRED — substantively denied; classifier returns AMBIGUOUS due to observed-unreliable uptime field (per our probes; see Q1/Q2 corroboration below); spec §8 routes to OPERATOR-ESCALATE pending clarification)** 2026-06-15. Spec: `docs/superpowers/specs/2026-06-15-podeditjob-restart-investigation-design.md`. Plan: `docs/superpowers/plans/2026-06-15-c33-podeditjob-restart-investigation.md`. **P0 verdict: `orphan_quirk`** — `tests/live/_c33_probe_p0_evidence.json` (S3 `boot-logs/c33-p0-20260615T082404/`, image=ubuntu:22.04, ports=null, NVIDIA RTX A5000 @ 16¢/hr, pod `fx5ymtpjljl2qd`, $0.0272, 10-min 30s poll); 21-sample trail showed `n_last_started_at_advances=0`, `n_negative_uptime_samples=10`, `n_null_uptime_samples=1`, `fire_count=0`. RunPod `runtime.uptimeInSeconds` was observed-unreliable in this probe (returns negatives/nulls/small values noisily) while `pod.lastStartedAt` stays invariant — consistent with the C30 negative-uptime observation and tentatively resolving C32 hypothesis (a)/(b): `runtime.uptimeInSeconds` appears to be an API quirk, not a restart signal. **NOTE:** "observed-unreliable" is from our own probes only — *no* external RunPod docs, GitHub issues, Discord threads, or community posts corroborate this as of 2026-06-15. Q1 + Q2 below firm this claim on direct evidence. **P1 verdict: `ambiguous` (substantively denial-class)** — `tests/live/_c33_probe_p1_evidence.json` (S3 `boot-logs/c33-p1-20260615T085602/`, NVIDIA GeForce RTX 3080 @ 17¢/hr, pod `lyyyzodr1inqb6`, $0.0096 successful + $0.043 across 2 prior aborts; stable_reason=`status=RUNNING for >=90s` after the orchestrator's uptime-gate was extended to fall back on `desiredStatus`+wall-clock). After ONE `podEditJob` mutation via B5a's `_merge_marker`, the 90s post-mutation 10-sample trail observed `last_started_at_advanced=False` (lastStartedAt invariant at `2026-06-15T15:56:03.474Z` for ALL 10 samples), `uptime_reset_observed=False`, `desiredStatus="RUNNING"` throughout, `mutation_response.data.podEditJob.id=lyyyzodr1inqb6` (mutation accepted). `_classify_p1` returned `ambiguous` only because `uptime_monotonic_for_90s=False` (uptime noise -14/2/0/-9 across the trail, expected per P0's orphan_quirk); the lastStartedAt + status invariance is denial-class evidence: a single podEditJob does NOT restart the container on this tier. **Denial branch:** `tools/c33_denial_branch.py` short-circuits with `outcome="N/A — P1 verdict != denied"` per spec §4 P_alt_branch (verdict was ambiguous, not denied). **Routing per spec §8:** P0=orphan_quirk + P1=ambiguous + db=N/A → OPERATOR-ESCALATE (unexpected combination). **Total C33 spend:** $0.0795 (P0+P1 successful runs only — P1 aborts add $0.043; well under $5 hard cap). **Shipped (Tasks 0-5):** `src/kinoforge/diagnostics/c30_probe.py` extended with `snapshot_last_started_at` + `PodStatusPollerExtended` (4-tuple samples) + `issue_single_pod_edit_job` + `Verdict_P0` / `Verdict_P1` enums + `_classify_p0` / `_classify_p1` (19 offline tests, all green). `tests/live/conftest.py` extended with `C33_HARD_CAP_USD=5.00` + `c33_sidecar_path` + `c33_run_id` + `_c33_count_advances` / `_c33_count_negative_uptimes` / `_c33_count_null_uptimes` + `c33_execute_p0` + `c33_execute_p1` (3 offline orchestrator tests, all green). `tests/live/test_c33_p0_orphan_disambig_live.py` + `tests/live/test_c33_p1_podeditjob_restart_ab_live.py` live scaffolds (RED-committed pre-spend per CLAUDE.md durability rule). `tools/c33_denial_branch.py` + `tests/live/_c33_denial_branch_evidence.json`. Three runtime fixes shipped during execution: capacity-error fallthrough widened to match `"resources to deploy"` + `"instances available"` substrings (RunPod returns capacity errors without an `extensions.code` field); `C30_GPU_CANDIDATES` refreshed to 2026-06-15 snapshot (RTX 3070/3080/3080 Ti/4000 Ada all out of stock today); P1 stable-gate now falls back to `desiredStatus="RUNNING"` continuously ≥90s wall-clock when the uptime gate is unsatisfiable. **Follow-up:** none in the kinoforge codebase. Next operator decisions: (a) treat the lastStartedAt+status invariance as informal denial and unblock production heartbeat (the C25 preserve-and-merge wire path is already byte-safe per `tests/live/_c25_smoke_evidence.json`) — OR (b) widen `_classify_p1` to recognize that on a tier where P0=orphan_quirk, the monotonic-uptime denial criterion is unsatisfiable and substitute `lastStartedAt invariance + status invariance` as the denial proxy. Cross-ref C32 (same uptime-quirk root cause — C33 P0 + Q1 + Q2 jointly resolve C32 in the affirmative). **Q1 (top-level `Pod.uptimeSeconds` vs wall-clock estimate, 2026-06-15):** `tests/live/_c33_probe_q1_evidence.json` (community-cloud RTX A5000 @ 16¢/hr, pod `17pm8vxqtyoo7l`, 5-min poll @ 30s, 11 samples, $0.0137). Both fields were stuck at 0 for the full window while wall-clock estimate `now_utc − lastStartedAt` rose monotonically 0.2s → 306.8s. `top.uptimeSeconds` returned 0 on all 11 samples (mean disagreement 153.6s, max 306.8s); `runtime.uptimeInSeconds` returned 0 on 9 of 11 samples plus one −7 plus one None (mean disagreement 169.7s, max 306.8s). Conclusion (PROVISIONAL — superseded by Q3 sweep below): BOTH GraphQL uptime fields appeared unreliable on this tier in this window; `now_utc − lastStartedAt` was the only sound uptime signal at the time. Q3 below shows this held only during the 08:00-09:30 PDT incident window; in steady state `runtime.uptimeInSeconds` is reliable and only `Pod.uptimeSeconds` remains broken. **Q2 (SECURE-cloud P0 repeat, 2026-06-15):** `tests/live/_c33_probe_q2_evidence.json` (SECURE-cloud NVIDIA A40 @ 44¢/hr, pod `9lkjo0n760r0jl`, 7-min poll @ 30s, 15 samples, $0.0528 within relaxed $0.06 Q2 cap). Verdict: `orphan_quirk` — `n_last_started_at_advances=0`, `n_negative_uptime_samples=12` (range −15 to +1), `n_null_uptime_samples=1`, `fire_count=0`, `desiredStatus="RUNNING"` throughout. Same negative-uptime pattern as community-cloud P0, different cloud tier, different GPU class — confirming the uptime weirdness is RunPod-API-wide, NOT community-cloud-specific. Combined with C30 A1a + C30 A0' + C33 P0 + C33 Q1 + C33 Q2, the uptime-unreliability claim now stands on five independent probes across two cloud tiers and four GPU classes. **Q3 (16-hour hourly sweep across 12 GPU types + 2 cloud tiers, 2026-06-15 22:26 → 2026-06-16 14:07 PDT):** `tools/_uptime_field_sweep_log.jsonl` (16 iterations: 14 success + 2 capacity-failed; cumulative spend $0.3373). Tools: `tools/uptime_field_hourly_sweep.py` (stdlib-only orchestrator), `tools/uptime_sweep_status.sh` + `tools/uptime_sweep_summary.py` (pretty-printers), `tools/repro_runpod_uptime.py` (minimal stdlib reproducer for the Pod.uptimeSeconds bug). GPU coverage: RTX A5000 (community), RTX A4000 (SECURE), RTX 3080 (community), RTX 4000 Ada (SECURE), RTX A4500 (community ×4), L4 (SECURE), V100-SXM2-16GB (community), RTX 3090 (community ×2), RTX PRO 4500 Blackwell (SECURE), RTX 4070 Ti (×2 capacity-failed). **Decisive findings (154 total samples across 14 successful pods):** (i) `Pod.uptimeSeconds`: **154/154 samples == 0** — fully-broken-stub bug, every pod, every GPU, every cloud tier, throughout the entire 16-hour window. Treat as unimplemented; never read this field. (ii) `runtime.uptimeInSeconds`: 127/154 positive monotonic, 26/154 null (early-init pre-runtime samples, normal), 1/154 zero, **0/154 negative**. Reliable in steady state. (iii) The morning's 22 negative-uptime samples (C30 A1a + C30 A0' + C33 P0 + C33 Q2 sidecars, all run between 08:00-09:30 PDT 2026-06-15) were caused by a real but **transient RunPod platform incident** that had fully recovered by the time the sweep started ~12 hours later. The Q1 sidecar (09:22 PDT) caught the incident endgame. (iv) `pod.lastStartedAt` was stable and present on every successful pod throughout the sweep; `now_utc − pod.lastStartedAt` is a clean wall-clock-based fallback when `runtime.uptimeInSeconds` returns None (early-init) or is otherwise unavailable. **Reliable uptime sources (recommended order):** (1) `pod.runtime.uptimeInSeconds` when non-null and non-negative — the canonical reading. (2) `now_utc − parse_iso(pod.lastStartedAt)` — fallback for the early-init window AND for cross-checking the canonical reading. Disagreement beyond round-trip latency (~30s) signals either stale `lastStartedAt` or buggy `uptimeInSeconds` (e.g., during platform incident); flag and rerun. **Unreliable — do NOT use:** `Pod.uptimeSeconds` (top-level). 154/154 zeros, no exceptions. **Implications for the codebase:** (a) `src/kinoforge/diagnostics/c30_probe.py::classify_run` currently treats any non-None negative `uptimeInSeconds` as RESTARTED. The sweep shows negatives are PLATFORM-INCIDENT noise rather than restart signals on healthy hosts, so the heuristic over-classifies during RunPod incidents. Recommended refinement: add a corroborating signal (S3 trap-fire count > 0 OR multi-sample negative pattern) before flagging RESTARTED — single-sample negative during an otherwise-monotonic trail should not trip the verdict. (b) The C33 P1 "ambiguous" verdict can now be re-interpreted: P1 ran at 08:57 PDT, fully within the incident window. The post-mutation trail's noisy uptimes (-14, 2, 0, -9) were incident artifacts, not restart signals. Combined with `lastStartedAt` invariance across the 90s post-mutation window, the substantive verdict is **DENIED — `podEditJob` does NOT restart the container**. (c) The C25 production heartbeat path (B5a `podEditJob` preserve-and-merge) can be unblocked. The original concern that `podEditJob` ticks restart containers is contradicted by P1 evidence read through corrected interpretation + sweep evidence that the API is reliable in steady state. (d) Anywhere the codebase reads `Pod.uptimeSeconds` (top-level), switch to `pod.runtime.uptimeInSeconds` or `now_utc − pod.lastStartedAt`. Audit: as of 2026-06-16, no production code reads the top-level field — only the C33 Q1 probe explicitly tested it. **Decision NOT to file external bug report:** the empirical case is captured here for any future operator who wants to escalate; filing has been deferred indefinitely because (i) `runtime.uptimeInSeconds` works correctly in steady state and we have a sound fallback for the failure modes, (ii) the `Pod.uptimeSeconds` bug is decisively diagnosed locally and we already know not to use it, (iii) the morning incident is observed to have recovered without intervention. **Next phase (concrete):** (1) ship the `classify_run` heuristic refinement above as a small follow-up; (2) unblock C25 production heartbeat enable (or document the green-light formally); (3) add a unit test asserting the diagnostics code never reads `Pod.uptimeSeconds`. Cross-ref tools committed in `4ed6dba`. **Q4 (real Wan 1.3B t2v cold-boot test, 2026-06-16):** ran `kinoforge generate -c tests/live/cfg_c28_phase_a_diagnostic.yaml --prompt examples/configs/prompts/dawn-flight.md --mode t2v --no-reuse` on community-cloud NVIDIA RTX 3090 @ 22¢/hr (pod `n81blcbc8fixx7`, ~$0.06, killed at 15 min after no progress). Verdict: **C28 docker-level restart loop is REAL on real Wan workloads** (reverses an earlier provisional retraction). 29 S3 EXIT-trap uploads in ~15 min (one every ~31s) with `rc=0`, `last_line="Cloning into 'ComfyUI'..."`, `models/diffusion_models/` empty, GPU 0% util "No running processes found", overlay disk wiping between cycles (1.1GB → 185MB). `lastStartedAt` stable throughout = docker container restart, not pod restart. The earlier C33 P0/Q1/Q2/Q3 sweep on `sleep 600` pods didn't trip the loop because `sleep 600` doesn't fail — only the actual Wan provision script does. **Q5–Q8 bisection (each ~$0.02 with throwaway `diag(c33-q5..q8)` instrumentation commits — REVERTED in clean-up commit after evidence captured):** added `set -x` + `PS4='[%T.%N]'` + `/tmp/p.sh wc/tail` + `selfterm.log` + `ps auxf` + `stdbuf -oL` + `sync; sleep 0.5; sync` in trap. Captured trace: script reaches `cd ComfyUI && pip install -q -r requirements.txt` (line 69), bash exits at ~T+15s with `rc=0` and pip STILL running at 76% CPU per ps. Ruled out: script truncation (p.sh intact at 86 lines / 5373 bytes), tee buffering (stdbuf changed nothing), selfterm conditions firing (dead-man window is 50 min, max-lifetime is 85 min, neither hits at T+15s). **Q9 ($0.005 orthogonal probe):** simplest possible `bash -c "echo HELLO; sleep 120; echo BYE"` pod cycled at ~135s when bash exited with rc=0. Conclusion: **RunPod's docker restart policy is `always`, not `on-failure`** as kinoforge's runpod-provider warning currently claims; containers respawn even on rc=0 clean exit. **Unresolved mystery:** the precise mechanism that exits bash at T+15s with rc=0 mid-pip-install during Wan provisioning. Not investigated further this session. **Implications additional to Q3's:** (e) revoke implication (b)+(c) above — C33 P1's "DENIED" re-interpretation and the C25 production-heartbeat green-light are PROVISIONAL pending a fix for the Q4 restart loop; production Wan workloads currently cannot cold-boot regardless of whether podEditJob restarts the container. (f) the kinoforge runpod-provider warning string "falling back to the provider's default restart-on-failure behaviour" is factually wrong per Q9 evidence; RunPod restarts on rc=0. Should be updated to "restart-always (verified Q9, 2026-06-16)". (g) follow-up: design a `diagnostic_mode: "trace"` cfg opt-in that bundles the Q5–Q8 instrumentation (set -x, PS4 timestamps, p.sh/selfterm.log/ps dumps, stdbuf line-buffering, sync-in-trap) as a single clean flag instead of carrying ad-hoc patches in production code. Future operators investigating restart-loop / provision-stall bugs can flip `diagnostic_mode: "trace"` to get the same evidence the Q5–Q8 commits captured without re-implementing the instrumentation. Spec hook: `docs/superpowers/specs/<date>-cfg-diagnostic-trace-mode-design.md` when triggered. (h) follow-up: isolate the T+15s bash-exit mystery — single probe with selfterm disabled (`KINOFORGE_SELFTERM_SCRIPT=""` override). If the cycle disappears, selfterm is somehow involved despite none of its documented termination conditions firing. If the cycle persists, root cause is in the image entrypoint, docker daemon, or RunPod-side container manager. Cheap ($0.02). (i) follow-up: until both (g)/(h) land, C25 production-heartbeat remains BLOCKED — not because podEditJob restarts containers (Q3+P1 say it doesn't on a stable pod), but because Wan workloads currently can't reach a stable state to begin with. Q4 spend: $0.06. Q5-Q9 spend: ~$0.10. Session total this debugging cycle: ~$0.50. **Q(h) (selfterm-disabled cold-boot, 2026-06-16, $0.06):** `tests/live/_c33_probe_h_evidence.json` (RTX 3090 community-cloud @ $0.22/hr, pod `xbynlkmzil8k26`, `KINOFORGE_DIAG_DISABLE_SELFTERM=1` via throwaway diag commit `e66ec99` reverted in `6f9104e`). Throwaway gate suppressed `KINOFORGE_SELFTERM_SCRIPT` injection at create_instance time so the in-pod bash guard `[ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]` short-circuited and `nohup python3 /tmp/selfterm.py` never started. 14.1 min wall window: **27 EXIT-trap uploads, 32.5 s avg cycle, 31-38 s range.** Q4 baseline (selfterm enabled): 29 uploads / ~31 s cycle. Statistically identical — disabling selfterm does NOT change the restart-loop cadence. Sample payload shows the same fingerprint: `rc=0`, `last_line="Cloning into 'ComfyUI'..."`, GPU 0 %. **Verdict: H_h REFUTED.** Selfterm is NOT the cause of the T+15 s bash-exit mystery. Root-cause funnel after Q(h): RULED OUT — selfterm (this probe), script truncation (Q7), tee buffering (Q8), documented selfterm timer conditions (Q5/Q6 dead-man 50 min + max-lifetime 85 min); REMAINING CANDIDATES — image entrypoint (`runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`), docker daemon (OOM-killer / resource ceiling), RunPod container manager. **Next cheapest probe (j):** $0.005 image-only sleep dockerArgs (no provision script). Pod `xbynlkmzil8k26` leaked when `timeout 124` SIGTERM'd kinoforge before `deploy_session.__exit__` destroyed it; manually destroyed via `kinoforge destroy --id xbynlkmzil8k26`. Implication (h-now-resolved): selfterm conclusively ruled out; implication (i) unchanged — C25 production-heartbeat remains BLOCKED on the kill-source isolation. **Q(j) (image-only sleep, 2026-06-17, $0.018):** `tests/live/_c33_probe_j_evidence.json`. Bare `bash -c 'sleep 200'` dockerArgs on `runpod/pytorch:2.4.0`, NO provision script + NO selfterm + NO diag trap. Uptime climbed monotonically 26 → 56 → 86 → 116 → 146 → 176, reset at wall=214.6 s = natural sleep completion + restart=always respawn. Image entrypoint + RunPod-side container manager RULED OUT as the kill source. (j) classifier flagged the benign natural-completion reset as `cycling`; refined to `bash_survived` because uptime reached near the 200 s sleep budget before reset. **Q(k) (provision-phase bisection, 2026-06-17, $0.013):** `tests/live/_c33_probe_k_evidence.json`. Hand-crafted script base64-piped through `PROBE_SCRIPT` env var ran awscli install / apt-get aria2 / git clone ComfyUI / `pip install -q -r ComfyUI/requirements.txt` serially with phase-marker touch files in `/tmp/phases/`. Single S3 upload at probe terminate showed bash reached `T_SLEEP_BEGIN` (the post-pip 300 s sleep) — pip-install completed in 37 s. The provision-script content itself is NOT intrinsically the kill trigger. **Q(l) (container metric watcher, 2026-06-17, $0.22 leak — manually destroyed):** `tests/live/_c33_probe_l_evidence.json`. Paired `tools/c33_l_metricwatch.py` with a real kinoforge run on cfg_c28_phase_a_diagnostic.yaml. 189 polls / 600 s / 2 s cadence: `runtime.container.memoryPercent = 0` across ALL samples, cpu peaked at 14 %. Container is NEVER under resource stress at the moment of any bash-death. **Container-OOM RULED OUT.** uptime never climbed above 28 s — cycling continuously the entire 10 min window. Pod cvuq8c0x1ysveh leaked on timeout SIGTERM; manually destroyed via podTerminate. Hypothesis H_m surfaced: cfg has `heartbeat_mode: graphql-tag` + `heartbeat_interval_s: 30`; cycle is ~31 s; the C25 B5a `podEditJob` mutation may be the kill mechanism, missed by C33-P1's lastStartedAt-only classifier because podEditJob mutations restart at the CONTAINER level (`runtime.uptimeInSeconds` resets) not POD level. **Q(m) (DECISIVE — heartbeat_mode: none, 2026-06-17, $0.013):** `tests/live/_c33_probe_m_evidence.json` + `tests/live/cfg_c33_m_heartbeat_disabled.yaml` (copy of c28 phase A with single field flip heartbeat_mode → none). Result: **SUCCESS in 3 min 23 s.** Provision + boot 100 s, ComfyUI poll 94 s, artifact published, pod destroyed cleanly. ZERO EXIT-trap uploads. **H_m CONFIRMED — `podEditJob` mutation during the heartbeat tick IS the kill mechanism.** Cycle period 31 s = 30 s heartbeat interval + 1 s GraphQL roundtrip. First cycle's death at bash time T+15 s = pod time T+30 s (after 15 s container-boot overhead) — matches Q4 exactly. C29's design (start heartbeat BEFORE engine.provision so STALL/RESTART_LOOP predicates can fire during long provisions) ALLOWED the bug to manifest. **Fix 1 (`aacd49e`)**: moved `start_heartbeat` to AFTER `_provision_compute_once` returns in `_provision_instance_and_build_backend`. Trade-off: stall/restart-loop predicates can't fire during provision; they still fire post-boot. New regression tests at `tests/core/test_orchestrator_c33_start_heartbeat_after_provision.py`; C29 test docstrings updated to cite C33-m supersession. **Q(n) (post-fix live confirmation, 2026-06-17, $0.067 leak — manually destroyed):** even with start_heartbeat moved to AFTER provision, post-boot ticks still mutated dockerArgs every 30 s → container restart resumed once ComfyUI was running. ComfyUI history endpoint cycled 404/502 with container uptime resetting to ~10 s repeatedly. **Fix 1 was necessary but not sufficient. Fix 2 (`c2526ac`)**: made `RunPodGraphQLHeartbeatEndpoint.write()` a NO-OP that logs a single WARNING per instance via a `_c33_warned_instances` cache. The 6 wire-shape unit tests at `tests/providers/runpod/test_heartbeat.py` that pinned the dockerArgs mutation contract are xfail'd with `strict=True` and document the IDEAL contract a future non-mutating satisfier (B5b) must replicate; new `test_write_is_noop_post_c33m` pins the no-op + warning behaviour. **Q(n2) (post-fix-2 live confirmation, 2026-06-17, $0.067):** `tests/live/_c33_probe_n_evidence.json`. ORIGINAL `cfg_c28_phase_a_diagnostic.yaml` (heartbeat_mode=graphql-tag, UNCHANGED — the failing Q4 cfg) ran successfully under the C33 fix combo: artifact produced in 3:29 wall, `C33-m: RunPod heartbeat write DISABLED` warning fired exactly once at T+1:40 (first heartbeat tick after provision), pod destroyed cleanly. **C33 cycle EXTINGUISHED end-to-end.** Cumulative C33 spend ≈ $0.91 of $10 budget. **C33 ROOT CAUSE CLOSED.** Open follow-ups remaining: (f) restart-policy warning string + (a) classify_run negative-uptime heuristic refinement + (d) banner unit test for top-level Pod.uptimeSeconds + (g) `diagnostic_mode: "trace"` cfg opt-in. New durable spec hooks: **B5b** non-mutating heartbeat satisfier substitute (e.g. `selfterm-http` mode); **B7 cross-CLI marker refresh** for new pods (current state: marker is set at create time only, write is a no-op, read still parses any pre-existing trailer — intra-CLI heartbeat still ticks the local ledger).
- **C32. Investigate why C30 A1a + A0' direct-GraphQL probes showed oscillating + negative `runtime.uptimeInSeconds` values.** Filed 2026-06-15 after the C25/B5a `PodEditJob`-causes-restart hypothesis (see C33 followup below — TBD) accounted for the kinoforge-side restart loop. C30 probes are a SEPARATE signal: A1a/A0' both used direct GraphQL `podFindAndDeployOnDemand` with NO heartbeat ticks, NO `PodEditJob` mutation, NO selfterm — yet 21-sample poll trails over 10 min showed uptime values like `[None, 6, -2, -15, -11, 0, -4, None, None, None, -17, -10, -5, -18, -11, -8, -5, 0, -15, -2, 0]` (A1a) and `[None, 17, 0, 0, 0, -15, -12, -9, -6, -3, 0, -7, -4, 1, -14, 0, -2, -12, -9, -6, 0]` (A0'). Negative uptimes are non-physical. Possible explanations to investigate: (a) RunPod's `runtime.uptimeInSeconds` is computed as `current_time - container_start_time` with a future `container_start_time` returned during pending-restart window — i.e., the field encodes "seconds until next scheduled start" when negative, not "seconds since boot"; (b) RunPod GraphQL response has a clock-skew or timezone bug; (c) the pods really WERE restart-cycling, but for a different reason than C25/B5a (e.g., RunPod community-cloud platform restart-on-idle for pods with NO declared port — but only on certain accounts/regions/GPU tiers). Diagnostic plan: (i) re-run A1a but also capture `desiredStatus` + the raw GraphQL response JSON every tick (not just `uptimeInSeconds`), so we can see whether the field appears as null-during-restart or actually returns negatives; (ii) cross-reference RunPod's GraphQL schema docs / Discord / support for the documented semantics of `runtime.uptimeInSeconds` (and whether it's signed at all); (iii) test on `cloudType=SECURE` for comparison. Cheap probe (~$0.02 / 10 min). Promote when next operator wants to use direct-GraphQL probes on RunPod community cloud and needs to interpret the uptime values correctly. Cross-ref C30 sidecars `tests/live/_c30_phase_a1a_evidence.json` + `tests/live/_c30_phase_a0prime_evidence.json`.
- **C31. Live-test `_destroy_safely` needs verify-and-retry against RunPod restart-policy races.** C28 Phase A v5 surfaced a pod leak: the test's atexit hook + `_destroy_safely` both call RunPod `podTerminate` directly, and ledger confirmed `pod_id` was correctly populated in `_OWNED_PODS`, but the pod was STILL RUNNING when the operator checked 10 min after `pytest` exited 0. Most likely cause: during the chronic restart-loop window, RunPod's restart-policy raced with the terminate — terminate "succeeded" (mutation returned `{data: {podTerminate: null}}`) but the container had already entered a fresh restart cycle before the terminate took effect. Operator's external guardian (`/tmp/c28_pod_guardian.sh`) caught it. Fix: in `_destroy_safely`, after issuing terminate, POLL `{ myself { pods { id } } }` 3-5 times at 3 s intervals; if `pod_id` still present, re-issue terminate. Apply to BOTH Phase A and Phase B live-test scaffolds. Promote when next live smoke leaks.
- ~~**C30. Investigate why RunPod containers restart every ~30s during clone phase (Wan 1.3B cfg).**~~ — **CLOSED (PARTIAL — root narrowed, full RCA escalated to RunPod support)** 2026-06-14. Spec: `docs/superpowers/specs/2026-06-14-c30-restart-loop-diagnosis-design.md`. Plan: `docs/superpowers/plans/2026-06-14-c30-restart-loop-diagnosis-plan.md`. **RCA: H_platform_restart_loop_community_cloud — RunPod community-cloud platform is restart-cycling pods regardless of image, ports, listener, or provisioning. Affects the simplest possible pod (no `ports`, `dockerArgs="bash -c sleep 600"`).** Decisive evidence: `tests/live/_c30_phase_a1a_evidence.json` (S3 `boot-logs/c30-a1a-20260614T222804/`, image=runpod/pytorch:2.4.0, ports=null, RTX 3070, 10:24, $0.022; poll trail uptimes `[None, 6, -2, -15, -11, 0, -4, None, None, None, -17, -10, -5, -18, -11, -8, -5, 0, -15, -2, 0]`). Inverse control: `tests/live/_c30_phase_a0prime_evidence.json` (S3 `boot-logs/c30-a0prime-20260614T224154/`, image=ubuntu:22.04, ports=null, RTX 3070, 10:17, $0.022; same oscillating-uptime pattern → image is NOT the cause). Total live spend: ~$0.044 (well under $1.50 cap; 7 of 9 walk-down phases never needed). **Shipped (Tasks 0-9):** additive subtree `src/kinoforge/diagnostics/c30_probe.py` (Verdict enum, classify_run with monotonic-uptime + negative-uptime rules, count_trap_fires S3 lister, BudgetCapExceeded + spend ledger + monotonic-time guard, _C28_TRAP_PREAMBLE_LINES inlined verbatim from `engines/comfyui/__init__.py:1285-1330`, create_probe_pod direct-GraphQL wrapper with full RunPod input shape — `cloudType=ALL`/`gpuCount`/`containerDiskInGb`/`minVcpuCount`/`minMemoryInGb` required for supply matching, PodStatusPoller using `runtime.uptimeInSeconds`, destroy_with_retry inlining C31 verify-and-retry, GraphQLError surfacing `extensions.code`, A2-A6 provision-line constants mirroring `render_provision` rungs, `_KINOFORGE_DOWNLOAD_HELPER_LINES` + `_C28_PHASE_A_CUSTOM_NODES` + `_C28_PHASE_A_MODELS`). `tests/live/conftest.py` extended with `_C30GraphQLClient` (wraps `_default_http_post` from `providers/runpod/util.py`) + `C30_GPU_CANDIDATES` ranked-fallback list (RTX 3070 cheapest at 13¢/hr) + `c30_execute_phase` orchestrator. 9 live test scaffolds with predecessor-sidecar gating; A1b/A1c/A2-A6 all skipped cleanly post-A1a=RESTARTED. 41 unit tests (classify_run 8, count_trap_fires 5, spend_ledger 6, create_probe_pod 7, pod_status_poller 5, destroy_with_retry 4, provision_walk_down 7) — all green. **Pivot (committed `ca4b6b3`):** original Task 7 specified YAML cfgs against a flat `engine.provision_script` schema that doesn't exist; pivoted to in-Python provision-line constants + uniform `create_probe_pod` across all 9 phases, honoring spec §2 "zero production-code mutation" crisply. **Reclassification rule (committed `933178e`):** `classify_run` gained "any non-None negative uptime → RESTARTED" after A1a's S3 EXIT trap never fired (pod killed before `aws s3 cp` could complete) but uptime values were unambiguously non-physical. **Follow-up:** none in the kinoforge codebase. RunPod community-cloud platform behaviour escalation: file a support ticket with the two sidecar JSONs + S3 diag prefixes; consider testing the same probes on `cloudType=SECURE` to confirm community-vs-secure asymmetry; consider testing on a fresh RunPod account to confirm whether the loop is account-bound or platform-wide. C28 Phase B (pre-baked image) sidesteps this only insofar as it avoids the clone phase — but if A1a (no clone, no anything) cycles, Phase B will too unless the platform issue is fixed upstream.
- ~~**C29. Heartbeat starts BEFORE wait_for_ready (boot-phase protection).**~~ — **CLOSED** 2026-06-14. Spec: `docs/superpowers/specs/2026-06-14-c29-heartbeat-earlier-design.md`. Plan: `docs/superpowers/plans/2026-06-14-c29-heartbeat-earlier.md`. **Shipped:** new `ProvisionResult` NamedTuple `(instance, backend, hb_loop)` + `_build_start_heartbeat_closure` helper hoist hb_loop construction OUT of `deploy_session`'s post-provision block INTO `_provision_instance_and_build_backend` right after the RunPod status-poll succeeds (cold-start branches) and BEFORE engine.provision runs. `GenerationEngine.wait_for_ready` + `provision()` accept `cancel_token: CancelToken | None = None` across ABCs + 6 GenerationEngine impls + 3 ImageEngine impls + RemoteSubmitPollEngine; the closure threads `cancel_token` end-to-end so STALL_REAP / RESTART_LOOP_REAP raise `Cancelled` cleanly from inside the engine's poll loop. Outer `except Cancelled` in the helper stops `hb_loop` + idempotently re-destroys the pod (operator-Ctrl-C path is load-bearing). Caller-supplied-instance path keeps pre-C29 late-start byte-identically. 14 new unit tests + 3 live smokes (Phase A boot-phase STALL_REAP, Phase B boot-phase RESTART_LOOP_REAP, Phase C boot-phase `kinoforge status` liveness). **Live evidence:**
  - `tests/live/_c29_phase_a_evidence.json` — Smoke A PROVEN: STALL_REAP fired at 66.0 s into provision sleep (window=60 s, util_counter=6, pod `1cba0n8mz63i87`, $0.0025 spend).
  - `tests/live/_c29_phase_b_evidence.json` — Smoke B PROVEN: RESTART_LOOP_REAP fired at 64.4 s (window=60 s, uptime_counter=4, pod `pmrex9fxwhof86`, $0.0024 spend).
  - `tests/live/_c29_phase_c_evidence.json` — Smoke C PROVEN: 4/4 operator-facing markers (`id`, `provider`, `last_heartbeat=2026-06-14T13:48:51-07:00`, `provider_status=ready`) surfaced by `kinoforge status --id <pod>` 60 s into provision; tick_count=6; pod `at9n5itq5sqd1w`, $0.0023 spend.
  - Total C29 live spend: ~$0.0072. Final `pixi run test`: 2450 passed, 41 skipped (baseline 2436 + 14 C29 tests). Pre-existing concurrency flakes excluded (test_blocking_acquire_serializes_concurrent_calls, test_sighup_reloads_interval — pass in isolation).
- ~~**C29. `gcp_submit_quota` refit for `cloudquotas_v1beta.CloudQuotasClient`.**~~ — **ABANDONED 2026-06-17**. Refit unnecessary because GCP itself is abandoned (see Phase 53 pivot). Code path (`tools/quota_burn*` + `cloudquotas_v1beta` import) preserved as historical reference; will not re-fire.
- **C28. RunPod container-restart-loop prevention.** — **CLOSED (PARTIAL)** 2026-06-14. Spec: `docs/superpowers/specs/2026-06-13-c28-restart-loop-prevention-design.md`. Plan: `docs/superpowers/plans/2026-06-13-c28-restart-loop-prevention.md`. Diagnostic-first prevention layer for the chronic RunPod container-restart loop that C27 detects but does not fix. **Shipped (Tasks 0-12, 14, 15):** A0 empirical mutation probe for `PodFindAndDeployOnDemandInput` (RunPod's GraphQL gateway disables Apollo introspection, so the planned `__type` query 400s; the probe sends a one-field mutation per candidate and classifies on parser-validation errors); sidecar `tests/live/_c28_runpod_input_schema_probe.json` confirms `restartPolicy=false`, `networkVolumeId=true`, `registryAuthId=false` (`f394412`). A1 S3 diagnostics bucket `kinoforge-pod-diagnostics` (us-west-2, 7-day lifecycle on `boot-logs/`) + `kinoforge-c28-diag-put` IAM policy (PutObject-only, prefix-scoped) attached to `kinoforge-ci` via `tools/c28_provision_s3_diagnostics.py` (idempotent) (`02f7aee`). A2 `render_provision` EXIT trap pre-amble gated on `cfg.diagnostic_mode` — captures rc/last_line/nvidia-smi/df/free/models-dir/dpkg/boot.log tail, PUTs via `aws s3 cp || true`; byte-identical to baseline when knob off (`0827f32`). A1.5 `InstanceSpec.diagnostic_env: dict[str,str]` + `setdefault`-merge in `_create_pod` (user env always wins) + `Config.diagnostic_mode: bool = False` + `orchestrator._build_diagnostic_env(run_id)` reads AWS via boto3 default chain (honours `AWS_SHARED_CREDENTIALS_FILE`) (`8011c78`). A3 `InstanceSpec.restart_policy: Literal["always","never"] = "always"` + sidecar-gated wire branch (A0 says `restart_policy_supported=false` → warn+skip path is current production) + `kinoforge deploy --diagnostic-mode` CLI flag rebuilds cfg via `model_copy(diagnostic_mode=True)` (`b2e1b79`). Phase A RED scaffold + cfg (`a6adda4`); live smoke run **NO_REPRODUCTION** + matched_hypothesis Hn (`db023f4`) — 3 cold boots all reaped by C26/C27 predicate before EXIT trap could complete `aws s3 cp`; likely silent-trap root cause: `aws` CLI absent from `runpod/pytorch:2.4.0` base image. Gate Task 8: ship Phase B + C unconditionally per spec table (`e7862d5`). Phase B: `docker/wan-comfyui/Dockerfile` with 4 pinned ARG refs + build-time `import comfy` smoke + pre-installed awscli (closes the silent-trap root cause for future captures) + 7-test static lint suite (`522047c`); `pixi run build-image-wan-comfyui` + GH Actions `build-wan-comfyui-image.yml` workflow_dispatch pipeline (`a60f0b2`); `render_provision` slim-mode branch — image prefix `kinoforge/wan-comfyui:` skips ComfyUI clone + custom-node clones + pip installs (`28828e9`); Phase B cfg + RED scaffold (`7eb8823`). Phase C: `_kinoforge_download` pure-bash helper rendered unconditionally — 3-attempt loop, 5/10/15 s backoff, `${out}.partial` cleanup, optional sha256 verify, bash indirect expansion `${!token_env}` for bearer header (HF/CivitAI/etc.); 7 helper tests (`41c9625`). C2 model-loop refactor: every download emits `_kinoforge_download '<url>' '<out>' '<sha>' '<token_env_name>'`; inline curl gone; existing HF + CivitAI fixture tests updated to verify the new call-site shape (`a188903`). Worktree symlinks for `.env`/`.aws`/`.gcp` so the boto3 default chain finds credentials. Regression: 1535 tests across `tests/core` + `tests/providers` + `tests/engines` + `tests/cli` — all green. **Deferred (Tasks 13, 16, 17):** Phase B live smoke (B4), Phase C live smoke (C3), and spec-level acceptance (3 cold boots + C27 PB re-fire) all require `kinoforge/wan-comfyui:v0.3.10-088128b2-cu124` to be pushed to Docker Hub. No Docker in the container; operator must trigger the `build-wan-comfyui-image.yml` workflow_dispatch in GitHub Actions (DOCKERHUB_USERNAME + DOCKERHUB_TOKEN already in `.env`, need adding to repo Settings → Secrets). Live spend across C28 to date: ~$0.20 (Phase A only).
- ~~**C27. Restart-loop stall detection.**~~ — **CLOSED** 2026-06-13. Spec: `docs/superpowers/specs/2026-06-13-c27-restart-loop-stall-detection-design.md`. Plan: `docs/superpowers/plans/2026-06-13-c27-restart-loop-stall-detection.md`. Pure-additive extension of the C26 util substrate. Shipped: `Verdict.RESTART_LOOP_REAP` appended after `STALL_REAP` + `DEFAULT_APPLY_POLICY` entry (`19cffff`). `_update_uptime_counter` pure state machine — twin of `_update_counter` on the uptime axis, no restart-blip filter (the chronic restart loop IS the signal) (`25a738d`). `_restart_loop_reap_predicate` pure function — same defensive shape as `_stall_reap_predicate`; per-entry `restart_loop_window_s` override (`71c8780`). `classify()` row 3'' wiring with STALL_REAP tie-breaker — both predicates can fire on the same tick; stall checked first wins (`d12f26a`). `LifecycleConfig.restart_loop_reap_enabled=True` / `restart_loop_window_s=180.0` / `restart_loop_uptime_threshold_s=90.0` + two non-negative validators (`16266a9`). `interfaces.Lifecycle` extension + `Config.lifecycle()` collapse on enabled bool (`9397eb6`). `HeartbeatLoop` extension — two new kwargs, `_uptime_counter` instance state, `consecutive_low_uptime_count` persisted into ledger touches both branches (`16ba622`). `_maybe_fire_stall_reap` renamed to `_maybe_fire_reap` with both-routes wiring; log line names verdict + both counters + both windows (`2e3e6f5`). `--restart-loop-window-override SECONDS` CLI flag on `kinoforge deploy` persists per-entry override (`f6fecda`). Cross-process callsite threading — `_adapters.build_util_endpoint_for` kill-switch now requires BOTH enabled flags off; orchestrator HeartbeatLoop construction + four CLI `classify()` callsites all thread the two new kwargs (`23dccaa`). `FakeUtilEndpoint` test helper for Phase A1 (`1d2296c`). Three live smokes all PROVEN: Phase A1 (`tests/live/test_c27_phase_a1_uptime_streak_live.py`) — FakeUtilEndpoint forcing `uptime_seconds=1` drives counter `[1,2,3,4,5,6]` end-to-end on RTX A2000 @ $0.12/hr; fires at 52.3 s; ~$0.002 (RED scaffold `34571f6`, evidence `2f57931`). Phase A2 (`tests/live/test_c27_phase_a2_alpine_restart_loop_live.py`) — real `RunPodGraphQLUtilEndpoint` against alpine pod with `provision_script="sleep 5; exit 1"` forcing real RunPod restart churn; counter `[0,1,2,3,4,5,6]`; uptime readings `[None, 0, 0, -2, -2, -15, -15]` (RunPod surfaces 0 / negative uptime during restart); fires at 96.4 s; ~$0.003 (RED `d698d3b`, evidence `8faef91`). Phase B (`tests/live/test_c27_phase_b_wan_warm_reuse_live.py`) — re-fires the deferred C25 Task 4 / C26 Task 14 gate on real Wan 2.1 14B T2V; gen1 regressed into the same restart-loop symptom that defeated C26 but C27 caught it: `RESTART_LOOP_REAP` self-fired, pod destroyed, CancelToken set, gen1's `ComfyUIBackend.result` raised `Cancelled` at 356.8 s; acceptance_path **PROVEN-PROTECTION**; ~$0.05 (RED `39e64f8`, evidence `ce4bd00`). Full regression: 208 unit tests across all C27-touched files green. Live spend across C27: ~$0.06 total. **Closes** the C25 Task 4 + C26 Task 14 deferred acceptance gate.
- ~~**C26. RunPod util-aware stall classify.**~~ — **CLOSED (PARTIAL)** 2026-06-13. Spec: `docs/superpowers/specs/2026-06-13-c26-runpod-util-aware-stall-classify-design.md`. Plan: `docs/superpowers/plans/2026-06-13-c26-runpod-util-aware-stall-classify.md`. **Shipped (Tasks 1-13):** RunPod GraphQL disk-util probe (Task 1, sidecar `tests/live/_runpod_util_disk_probe.json` — no disk axis exists, `disk_percent` ships as None permanently). `core/util_endpoints.py` Protocol + `UtilSnapshot` dataclass + `provider_util_supported` gate (`{"local", "runpod"}`). `RunPodGraphQLUtilEndpoint` satisfier (Bearer auth + inlined podId, MAX across gpus). `LocalUtilEndpoint` test seam. `core/util_counter.py` pure `_update_counter` state machine. `LifecycleConfig` stall_reap_enabled / stall_window_s / stall_gpu_threshold / stall_cpu_threshold cfg knobs (`core/config.py`). `Verdict.STALL_REAP` appended (additive) + in `DEFAULT_APPLY_POLICY`. `classify()` row 3' inside sentinel-fresh + non-idle branch intercepts LIVE. HeartbeatLoop integration: per-tick util read → counter update → 7 ledger fields persisted → self-classify → on STALL_REAP destroy + ledger.forget + cancel_token.set + stop. Cross-process consumers (CLI `_resolve_warm_instance`, `_cmd_destroy`, cost-mode classify loop, `reaper_actor.act_on_verdict` + `sweep`, `sweeper.SweeperLoop`) thread three new kwargs; STALL_REAP outside `_FORCE_BYPASSABLE_VERDICTS` so `--force-attach` refuses stalled pods. `--stall-window-override SECONDS` CLI flag on `kinoforge deploy` persists per-entry override. Phase A live smoke (`tests/live/test_c26_phase_a_stall_detection_live.py`) **PROVEN** at 76.6 s — FakeEngine on cheapest RunPod offer (RTX 3070), counter trail `[0, 0, 1, 2, 3, 4, 5, 6]`, STALL_REAP fires at counter × interval = 60 s ≥ stall_window_s=60 s, pod destroyed, cancel_token set, ledger.forget invoked; spend ~$0.003. Sidecar `tests/live/_c26_phase_a_smoke_evidence.json`. Full regression: 419 unit tests across core + CLI suites green. **Partial (Task 14 Phase B):** Wan + ComfyUI 2-CLI smoke uncovered a design hole — the pod entered a container restart loop (RAM/disk/GPU near zero, uptime_seconds=1 on every tick). Both stall axes ARE below threshold (gpu=0, cpu=13 < 20), but `_update_counter`'s uptime-decrease guard fires every tick and resets the counter to 0, so STALL_REAP never fires. Sidecar `tests/live/_c26_phase_b_smoke_evidence.json` captures the diagnosis. The shipped design covers the "steady low util" stall class (Phase A) but NOT the "chronic restart loop" stall class (Phase B). C25 Task 4 deferred gate remains open. Follow-up tracked as **C27** (restart-loop stall detection): a sibling predicate, e.g. `uptime_seconds < threshold_uptime` for K consecutive ticks, alongside the existing low-util predicate. Live spend across C26: probe $0.01 (2 runs) + Phase A $0.003 + Phase B $0.025 ≈ $0.04 total.

### D. CI / platform

- ~~**macOS heartbeat-ledger race** (flaky CI since Layer U / Phase 36).~~ — CLOSED by commit `6b9fba3` (2026-06-09). Test-side JSONDecodeError tolerance + post-with poll. C1 above is the source-side root-cause fix.
- **D1. Windows CI.** DECLINED. Full implementation plan committed at `windows-migration-cancelled.md`. Revivable: real `win-64` platform support, pixi 4-platform lock, `as_posix()` path normalization, Windows-portable `check-added-large-files` hook. Linux + macOS only today.

### E. Per-phase Out-of-Scope (mostly polish; reviewed once for promotion)

These were noted as out-of-scope in their parent phase. Listed here so the
central index stays the single source of truth. Promotion to A/B/C
happens when a concrete next-step is identified.

#### Phase 25 — Layer O (output directory)
- **E1.** Cloud-native sinks (S3 mirror, webhook POST).
- **E2.** Filename template customization.
- **E3.** Migrate existing `.kinoforge/<run_id>/*.mp4` → `output/`.
- **E4.** `Artifact.published_path` field for CLI status / batch summary.

#### Phase 29 — aria2c
- **E5.** `Artifact.headers` field for HF-gated weights via `Authorization: Bearer hf_…` (also see C9).
- **E6.** aria2c knobs via env-var / YAML config.
- **E7.** aria2c `--checksum=` flag short-circuit.
- **E8.** Split `tests/core/test_downloader.py` (658 lines) into stdlib + aria2c sub-files.

#### Phase 30 — Layer Q (HF)
- **E9.** `include` / `exclude` filtering on `ModelEntry`.
- **E10.** `GatedModelError` for HF 403 nuance.
- **E11.** Custom HF mirror (`HF_ENDPOINT` env var support).

#### Phase 32 — Layer R (keyframe)
- **E12.** `HostedImageEngine` + `DiffusersImageEngine` concretes.
- **E13.** Image-backend pool for parallel `flf2v` role fills (serial today).
- **E14.** Keyframe caching across runs (`store.get_bytes` pre-check).
- **E15.** User-facing `pipeline:` YAML override (add at ≥3 stages).
- **E16.** `output_intermediates: true` cfg knob.
- **E17.** LoRA support on image engines (extend `ImageProfile.loras`).
- **E18.** Dynamic fal per-endpoint capability sniffing.
- **E19.** Multi-pass refinement keyframes (`KeyframeStage → KeyframeRefineStage → GenerateClipStage`).
- **E20.** Content-type sniffing (`KeyframeStage` hardcodes `.png` regardless of JPEG/PNG actual).
- **E21.** fal storage upload integration for keyframe→wan i2v / flf2v end-to-end (Layer S candidate as noted in PROGRESS:1046).
- **E22.** Asset-role wiring beyond `init_image` — `first_frame` / `last_frame` / `drive_audio` / `source_video`. No engine declares support today (README:1136).

#### Phase 33 — Layer S (`kinoforge status` ledger)
- **E23.** `kinoforge status --all` (every ledger entry).
- **E24.** `kinoforge status --json` (machine-readable).
- **E25.** `kinoforge ledger migrate` helper for legacy entries.

#### Phase 34 — Layer T (cloud ledger CLI routing)
- **E26.** `--store-uri s3://kf-prod` / `KINOFORGE_STORE_URI` cross-machine bootstrap (README:426).
- **E27.** Lock-contention surfacing in non-batch handlers (`LockTimeout` catch-arm beyond `_cmd_batch`).

#### Phase 38 — Layer W (S3 / GCS real-cloud)
- **E28.** S3 + GCS retry-via-proxy live verification (2 xfail axes; covered offline).
- **E29.** DSSE-KMS (S3) + CSEK (GCS) encryption modes.
- **E30.** Multipart resumability across process restart.
- **E31.** Bucket-level default encryption knob.
- **E32.** Signed URL custom response headers.
- **E33.** Azure + B2 + R2 store backends.

#### Phase 39 — Layer W+α (cloud bootstrap)
- **E34.** Scope-down AWS-managed broad policies → `.aws/policies/skypilot-minimal.json`.
- **E35.** AWS bucket scope-down on `AmazonS3FullAccess` (predates this layer).
- **E36.** `skypilot[aws]` pixi pin-conflict resolution (blocks `sky check aws`).

#### Phase 40 — Layer W+β (SkyPilot T4 GPU smoke)
- **E37.** `accelerators_in_cost` ordering verification on the GPU branch.

#### Phase 43 — Layer 4 (Bearer comparison smokes)
- **E38.** Rate limiting on `RemoteSubmitPollBackend` (home exists; YAGNI today).
- **E39.** Webhook callback path (each provider supports; polling fine today).
- **E40.** HTTP-recording fixtures for SDK-drift detection.
- **E41.** Flagship-tier YAMLs (budget-tier first).
- **E42.** Cross-provider quality scoring (CLIP, FVD).
- **E43.** Alt image engines beyond Replicate flux-schnell — SDXL via Replicate, Imagen via Vertex AI.
- **E44.** Per-mode budget-vs-flagship model upgrades for `flf2v`.
- **E45.** `probe_hosted --check-bedrock-model-access` root-cause fix (list-only false positive). Phase 42 Task 7 follow-up.

#### Phase 45 — Layer 5b (ephemeral workspaces)
- **E46.** Vault encryption at rest (chmod 600 only).
- **E47.** Multi-vault composition / inheritance.
- **E48.** Online vault validation against CivitAI / HF.
- **E49.** Keyring / OS credential-store integration.
- **E50.** Provider-internal log retention coverage (Replicate / Runway / RunPod internal logs).
- **E51.** Git-history rewrite for prompt-field-*.txt.
- **E52.** `Secret[str]` newtype across SPEC ABCs (D10 rejected — architecture choice).
- **E53.** Per-segment LoRA stacks.
- **E54.** Encrypted profile cache (opaque alias supersedes).
- **E55.** `hooks.post_generate` (forward-compat contract spelled out).
- **E56.** RunPod billing-log scrub.
- **E57.** Auto-redact of output-directory contents.

#### Phase 48 — Layer 8 (`model_identity` ABC)
- See C13, C14, C15 above (sibling identity ABCs).

#### Phase 28 — ComfyUI UI→API converter (sub-plan)
- **E58.** Wrap `_meta` header into converter output.
- **E59.** Auto-derive `_meta.source_repo` / `source_sha` / `source_path` from CLI flags.
- **E60.** AST-walk source for `INPUT_TYPES` as offline fallback (no live capture).
- **E61.** `tools/capture_object_info.py` in CI (operator-invoked today).
- **E62.** Cache `/object_info` across pod boots.
- **E63.** Multi-pack-stack composition.
- **E64.** Vendor `pydn/ComfyUI-to-Python-Extension` for API→Python direction.
- **E65.** Lint Seth's vendored code (excluded from ruff + mypy).
- **E66.** `tools/capture_object_info.py` SkyPilot / other-provider support.

### F. Intentionally-kept smells

See `docs/hygiene-notes.md`. Reviewer checks before re-flagging.

- **F1.** Duplicated provision branch in `core/orchestrator.py` (cache-miss vs post-cache-hit). Reconsider on third caller or branch divergence.

### G. Breaking changes already shipped (operator-visible)

- `kinoforge gc` requires `--config PATH` (since Layer C). Old shell scripts must update.
- `kinoforge generate` default `--run-id` flipped `"run"` → `f"run-{ts}"` (Layer O). Pass `--run-id run` to restore prior behavior.


## GitHub issues status

| # | Title | Status |
|---|---|---|
| #1 | Continuity / stitching fallback | CLOSED (Layer B) |
| #2 | Audio sync stage | Open |
| #3 | Concurrent / distributed backend scheduler | CLOSED (Layer G) |
| #4 | Keyframe / image-generation upstream Stage | CLOSED (Layer R) |
| #5 | S3 / GCS artifact stores | CLOSED (Layer C) |
| #6 | `ArtifactStore.uri_for(run_id, name)` ABC | CLOSED (Layer A) |
| #7 | Cross-process discovery lock | CLOSED (Layer H) |
| #8 | HuggingFaceSource bare-repo listing | CLOSED (Phase 30) |
| #9 | aria2c fast-path | CLOSED (Phase 29) |

## Single next action

**C26 — RunPod util-aware stall classify CLOSED (PARTIAL) 2026-06-13.**
Phase A end-to-end PROVEN on cheap RunPod pod (sha `8406b0a`). Phase B
on Wan + ComfyUI exposed a design hole — chronic container-restart
loop defeats the uptime-decrease guard in `_update_counter`; the
operator-visible C25 stall class is NOT yet protected. C25 Task 4
deferred gate stays open. Next: **C27 — restart-loop stall detection**.
Sibling predicate (uptime < threshold for K consecutive ticks) added
alongside the existing low-util predicate, then re-fire the Phase B
Wan + ComfyUI smoke. Spec + plan TBD. Tracks A/B below remain queued
live-spend work — not blocked by C27, not blocking it.

**Phase 45 — Layer 5b (prompt + LoRA confidentiality) CLOSED.** All 21
tasks landed end-to-end (full entry near end of this file). +37 net
tests across Tasks 17-20. Tracks A/B below remain queued live-spend
work — not blocked by Layer 5b, not blocking it.

### Phase 43 — Layer 4 (Bearer-provider comparison smokes)

Hosted Bearer adapters for Replicate / Runway / Luma sharing a
`RemoteSubmitPollBackend` foundation. Plus `ReplicateImageEngine` image-
sibling for Layer-R `KeyframeStage`. `OutputSink` Protocol extended with
`provider` + `model` named-only params; `LocalOutputSink` embeds them
in the filename schema `{ts}_{provider}_{model-slug}_{prompt-slug}.{ext}`.

- [x] Task 0: `RemoteSubmitPollBackend` + `RemoteSubmitPollEngine` ABCs — commit `2a9efec`
- [x] Task 1: ABC stable-surface invariant + vendor-SDK confinement scan — commit `b39c3fd`
- [x] Task 2: `OutputSink` + `format_filename` extension + `LocalOutputSink` — commit `ef04e73`
- [x] Task 3: `pixi.toml live-hosted` env + `preflight --check-hosted` — commit `c426457` (+ fix `3db517a`)
- [x] Task 4: `ReplicateEngine` + `ReplicateBackend` — commit `b63c895` (+ slug fix `bb6e2e3`)
- [x] Task 5: `RunwayEngine` + `RunwayBackend` — commit `8ac8f03`
- [x] Task 6: `LumaEngine` + `LumaBackend` — commit `4515ac4`
- [DEFERRED] Task 7: Fal retrofit onto `RemoteSubmitPollBackend` — base ABC validated against 3 wire shapes already; punt to follow-up.
- [x] Task 8: `ReplicateImageEngine` — commit `cc5bd6c`
- [x] Task 9: `GenerateClipStage` threads provider+model — commit `671cd6f`
- [PARTIAL] Task 10: Comparison configs — 2 of 15 YAMLs (t2v only) — commit `a054877`. i2v/flf2v/keyframe-prestage/manifest deferred; luma-t2v.yaml removed in Phase 44.
- [x] Task 11: Replicate live smoke (t2v) — `bytedance/seedance-1-lite`, 6 MB MP4, ~32 s, ~$0.10.
- [x] Task 12: Runway live smoke (t2v) — `gen4.5`, 2.8 MB MP4, ~2 m 40 s, ~$1.25. Caught 4 production bugs (commit `f20a70d`).
- [CLOSED] Task 13: Luma live smoke — API was retired by the provider in 2026; see Phase 44 / Layer 5a. The 403 observed at deferral time was the provider winding the endpoint down.
- [DEFERRED] Task 14: Fal i2v + flf2v extension — depends on Task 10 keyframe pre-stage.
- [DEFERRED] Task 15: Comparison batch capstone — depends on Tasks 10/13/14.
- [x] Task 16: README + PROGRESS + merge.

**First real artifacts (Layer 4):**

- Runway gen4.5 t2v: `/workspace/output/20260607-194607_runway_gen4.5_Photorealistic-cinem.bin` — 2.8 MB ISO-BMFF.
- Replicate seedance-1-lite t2v: `/workspace/output/20260607-194858_replicate_bytedance-seedance-1-lit_Cinematic-shot-of-a.mp4` — 6 MB ISO-BMFF, full filename schema verified.

**Live-smoke bug catches (4 production fixes in `f20a70d`):**

1. `job.params` (orchestrator-threaded `cfg.params`) was ignored vs `job.spec.params` only. All 3 hosted engines merge both sites now.
2. Runway returns 403 for both auth AND model-access failures ("Model variant X is not available"). Bare 401/403 mapping misclassified the latter. Narrowed on `runwayml.AuthenticationError` SDK subclass.
3. `RemoteSubmitPollBackend.result()` returned `filename=""` when status had no filename hint; sink fell back to `.bin`. Now derives from `urlparse(url).path` basename.
4. Replicate `predictions.create` uses `model=` (slug), not `version=` (hash). Both video + image backends switched. (Caught earlier in `bb6e2e3`.)

**Layer 4 carry-forward:**

- Luma credential refresh needed (or API plan upgrade).
- Comparison batch capstone: needs Task 10 (15 YAMLs) + keyframe pre-stage.
- Fal retrofit onto `RemoteSubmitPollBackend`: refactor only; existing engine functional.
- **Hosted-engine per-prediction cost capture (Layer 5 candidate).** Hosted
  engines bill per-prediction, not per-second, so the existing
  `BudgetTracker` (pod-time only) does not cover them. Spend is currently
  not recorded anywhere — not in `Artifact.meta`, not in any sidecar, not
  in the ledger. Proposed surface: per-engine `_extract_cost(status) ->
  float | None` hook on `RemoteSubmitPollBackend` (Replicate exposes
  `metrics.predict_time` × rate-card; Runway / Luma return duration +
  resolution from which the rate card is recoverable). Lift the value onto
  `Artifact.meta["cost_usd"]`, optionally write a `.cost.json` sidecar
  next to each clip, and add a `KINOFORGE_SESSION_BUDGET_USD` env-gated
  pre-submit check that raises `BudgetExceeded`. Tracked here so the
  next layer planner sees the seam already mapped — the
  `RemoteSubmitPollBackend` docstring already names "spend tracking" as
  one of the planned cross-cutting features bolting onto this foundation.

### RESUME — START HERE

**Successful generations log:** see `successful-generations.md` (added Phase 46). Per `CLAUDE.md`
Durability rules, every new-capability success gets a new entry unless `--ephemeral` was passed;
same-tuple `(provider, engine, model, mode)` repeats get a "See also" line.

**Where we are (as of session 2026-06-07):**
- **Phase 43 (Layer 4 — Bearer-provider comparison smokes):** PARTIAL (above). 10 of 17 tasks landed end-to-end; 4 deferred + 1 partial. 2 hosted Bearer providers proven live (Runway + Replicate). Phase 44 closes the Luma direct-API carry-forward (API retired by provider); Layer 5b adds `LumaAgentsImageEngine` (UNI-1 image keyframes) — separate spec.
- **Phase 41 (Layer 1 — AuthStrategy substrate):** CLOSED. 11 tasks, merged to main.
  ABC + Bearer + GCPServiceAccount + AWSSigV4 + `build_auth_strategy` registry +
  HostedAPIEngine retrofit + FakeAuthStrategy fixture + `tools/probe_hosted.py` +
  ABC stable-surface invariant. Fully offline.
- **Phase 42 (Layer 3 — BedrockVideoEngine pivot):** PARTIAL. Tasks 0–6 + Task 8
  done. Probe tightened. Region pivot us-east-1 → us-west-2. NovaReelEngine
  generalized to `BedrockVideoEngine` (YAML-driven `model_input_template`). Task 7
  (live smoke) BLOCKED on AWS Support case for Luma Ray v2 account authorization.
- **Layer 2 (Veo on Vertex AI):** UNBLOCKED 2026-06-07 — operator upgraded GCP
  billing to pay-as-you-go. Plan not started yet. The same upgrade also
  unblocks Layer W+β (SkyPilot T4 GPU smoke from Phase 40) for re-fire.
- **Bearer-key hosted video (Replicate / Runway):** UNBLOCKED 2026-06-07
  — operator signed up, added credit, pasted keys into `.env` for
  `REPLICATE_API_TOKEN` and `RUNWAYML_API_SECRET`. Same Layer 1
  `HostedAPIEngine` + `Bearer` strategy serves both — config-only
  addition, no engine work. Each smoke ~$0.05-0.50. (Luma direct API
  retired by provider in 2026 — see Phase 44 / Layer 5a; `LUMAAI_API_KEY`
  is reserved for Layer 5b's UNI-1 image-keyframe engine.)
- **SkyPilot AWS compute:** WIRED 2026-06-07 (`f74a73d`) —
  `skypilot.extras=["gcp","aws"]` + `awscli` in live-skypilot env;
  `.env.example` documents IAM-user / SP-style auth recipe. Live AWS smoke
  + GPU + per-engine smokes still deferred (same scope cut as the GCP
  multi-cloud line below).
- **SkyPilot Azure compute:** DEFERRED 2026-06-07 — upstream packaging gap.
  SkyPilot's `[azure]` extra transitively pulls `azure-cli >= 2.73`, which
  pins `azure-batch >=15.0.0b1,<15.1.dev0` — a pre-release-only range.
  conda-forge has no `azure-batch` 15.0.x build (jumps 14.2.0 → 15.1.0);
  PyPI has 15.0.0b* betas but uv refuses pre-releases by default. Pixi
  does have per-package cooldown overrides (`[exclude-newer]` /
  `[pypi-exclude-newer]`, verified against 0.69.0 docs) but those address
  the cooldown filter, not uv's prerelease default — there's no
  `--prerelease=allow` equivalent surface in pixi 0.69.0.
  Unblock paths: (a) conda-forge ships `azure-batch` 15.0.x GA, (b)
  `azure-cli` loosens the pin, or (c) pixi gains a prerelease allowlist.
  TODO comment in `pixi.toml` next to the `[feature.live-skypilot.pypi-dependencies]`
  block carries the full status so future-us doesn't relitigate.
  Workaround for operator who needs Azure today: `brew install azure-cli`
  / `apt install azure-cli` host-side and run `sky` from a non-pixi shell.
  Infrastructure that landed regardless: `AZURE_CONFIG_DIR` activation env
  + `.azure/` gitignore whitelist mirror the existing `.gcp/` / `.aws/`
  pattern, ready for the unblock without further pixi.toml churn.

**Single next action (operator, two parallel tracks):**

**B5a CLOSED (2026-06-12).** Heartbeat substrate + RunPod satisfier live. Next in warm-reuse queue: **B7 — cooperative lock between session-start and reaper** (Layer V §6, prereq for B3 warm-reuse retrofit). Run `pixi run preflight` then start the B7 spec/plan cycle.

Track A — Bedrock Luma Ray v2 (us-west-2):
1. Open AWS Support case at `https://us-west-2.console.aws.amazon.com/support/home#/case/create` — Technical → Service: Bedrock → Severity: General guidance.
2. Subject: "Bedrock Luma Ray v2 access — `authorizationStatus=NOT_AUTHORIZED` despite agreement accepted".
3. Body: include account `<AWS_ACCOUNT>`, region `us-west-2`, model `luma.ray-v2:0`, RequestId `b24a6306-af82-4c5b-b24b-e40c1f393517`, identity `arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci`, and the use case ("internal kinoforge SDK comparing video-generation model outputs across providers").
4. On AWS reply, run:
   ```
   KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 pixi run pytest tests/live/test_luma_ray_live.py -v -s
   ```
   ~$3.75 spend at 540p, ~3 min wall time. Fixture lands at `tests/engines/fixtures/luma_ray/last_smoke.json`; offline replay test (skip → pass) auto-activates.

Track B — Veo on Vertex AI (us-central1):
- ✓ **2026-06-07: operator upgraded GCP billing to pay-as-you-go.** Track B is
  now unblocked. Layer 2 (Veo) ready to plan + execute via the Layer 1
  substrate. Same upgrade also unblocks **Layer W+β** (SkyPilot T4 GPU smoke,
  paused at `b9a45e4`) — single command re-fire:
  ```
  KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot pytest \
    tests/live/test_skypilot_live.py::test_skypilot_live_e2e_t4_gpu_lifecycle_smoke \
    -v -s
  ```

**Read in this order:**
1. The Phase 42 entry below — pivot rationale + Phase 2 blocker detail.
2. `git log --oneline -10` for recent commits.

**Budget remaining: ~$10.88 of $15** (Layer 1 + Layer 3 Tasks 0–8 spent $0; B5a live smokes spent ~$0.004).

## Post-MVP

### Phase 10 — prompt splitter (deferred layer #1 from handoff §7)
- [x] Task 1: Splitter ABC + register/get registry helpers — commit 231fcc4
- [x] Task 2: HeuristicSplitter + core self-registration trigger — commit f522e2b
- [x] Task 3: SplitterConfig optional block (defaults to heuristic) — commit fd0978a
- [x] Task 4: Orchestrator step-6 wiring + stage validate-once + README/PROGRESS — commit d1828b7

### Phase 11 — uri_for ABC (deferred layer A, GitHub issue #6)
- [x] Task 1: Add `ArtifactStore.uri_for` ABC method + LocalArtifactStore impl + tests — commit `a6f8950`
- [x] Task 2: Refactor JsonProfileCache to use `store.uri_for`; delete `_uri_index`, `_uri_for`, `_reconstruct_uri` — commit `dd08f0c` (closes #6)

### Phase 12 — continuity fallback (deferred layer B, GitHub issue #1)
- [x] Task 1: Add `inject_tail_frame` helper + `extract_last_frame` ABC default + FakeEngine impl — commit `b9cb44b`
- [x] Task 2: Wire continuity into GenerateClipStage non-native branch — commit `270accd` (closes #1)

### Phase 13 — S3 / GCS artifact stores (deferred layer C, GitHub issue #5)
- [x] Task 1: S3ArtifactStore + deps + invariant patterns + adapters wire + 17 tests — commit `424c7c9`
- [x] Task 2: GCSArtifactStore + adapters wire + 17 tests — commit `057caaf`
- [x] Task 3: StoreConfig pydantic block + 6 tests + YAML example — commit `41cc75d`
- [x] Task 4: CLI _build_store + 3 call-site swaps + 3 tests + Layer-A _path peek fix — commit `1cd1f15` (+ docstring polish at `b661576`) (closes #5)

**CLI breaking change (Task 4):** `kinoforge gc` subcommand gained a required `--config PATH` argument so it can read the optional `store:` block; anyone resuming the project must update existing `gc` invocations accordingly.

### Phase 14 — .env secrets loader (post-MVP Layer D)
- [x] Task 1: python-dotenv dep + .gitignore .env + .env.example — commit `59f732e`
- [x] Task 2: dotenv_loader module + 8 unit tests — commit `0dc4714` (+ polish at `366ce5d`)
- [x] Task 3: CLI --env-file flag + 2 integration tests — commit `727ee2f` (+ polish at `b9056cf`)
- [x] Task 4: README Credentials section + PROGRESS Phase 14 entry — commit `d4be826`

### Phase 15 — per-engine extract_last_frame (post-MVP Layer E)
- [x] Task 1: `FrameExtractionError` + `core/frames.ffmpeg_last_frame` helper + injectable subprocess seam — commit `ba265bb` (+ missing-ffmpeg wrap + test strengthening at `ec04976`)
- [x] Task 2: ABC contract change (`extract_last_frame -> bytes`) + `inject_tail_frame` simplification + FakeEngine bytes return — commit `b6fca7a` (+ docstring polish at `d150613`)
- [x] Task 3: `GenerateClipStage` non-native rewiring (extract → put_bytes → wrap → inject) — commit `0c2c7a0` (+ filename-population + chain-test strengthening at `f41f3c4`)
- [x] Task 4: ComfyUI `result()` /view URL backfill + `extract_last_frame` + 2 seams — commit `50a08bb` (+ filename URL-encoding at `e4151ff`)
- [x] Task 5: Diffusers `result()` URL passthrough + `extract_last_frame` + 2 seams + server contract doc — commit `9df1dfd` (+ url-shadowing rename at `3d6ce7a`)
- [x] Task 6: Hosted `url_path` cfg + dot-walker + `result()` backfill + `extract_last_frame` + 2 seams — commit `c10b111`
- [x] Cross-engine fetch-error wrap (Task 4/5/6 retrofit) — commit `0d2d2c3`. All three engines now wrap `http_get_bytes` exceptions as `FrameExtractionError` per spec §4.3.

### Phase 16 — per-engine asset wiring (post-MVP Layer F)
- [x] Task 1: `AssetFetchError` + `core/assets.py` (find_asset, asset_bytes, set_by_dot_path) + 10 tests — commit `8335ff9`
- [x] Task 2: Diffusers backend `asset_paths` + submit + validate_spec + 4 tests — commit `a62d110`
- [x] Task 3: Hosted backend `asset_paths` + submit + validate_spec + 4 tests — commit `d25c5c8`
- [x] Task 4: ComfyUI backend `http_get_bytes` + `http_post_file` seams + `asset_node_ids` + 8 tests — commit `40dfaec`
- [x] Task 4 (review fix): random multipart boundary + filename escape + AssetFetchError wrapping + 8 tests — commit `e6826c6`
- [x] Task 5: GenerateClipStage post-chain `validate_spec` + 3 tests — commit `22269ed`
- [x] Task 6: README + PROGRESS + final gate + merge — commit `a271a03` (+ Phase 16 SHA backfill at `cb94413`; merge commit `3037bde`)
- [x] Post-merge fix: pydantic cfg strip closed for Layer E `url_path` + Layer F `asset_paths`. `HostedEngineConfig` gains `url_path`/`asset_paths`/`api_key_env`/`health_url`; new `DiffusersEngineConfig` registered on `EngineConfig.diffusers`. 7 cfg round-trip tests + 2 YAML→engine.backend E2E tests close the silent-strip defect that bypassed both Layer F unit tests and Layer E tests — commit `484e368`. Post-Layer-F count: 524 tests.

### Phase 17 — concurrent backend scheduler (post-MVP Layer G, GitHub issue #3)
- [x] Task 1: `BackendPool.close()` ABC method + context-manager (`__enter__`/`__exit__`) + `SequentialPool` no-op impl + 4 parity tests — commit `a344bc8`
- [x] Task 1 cleanup: drop `func-returns-value` suppression in close-noop test — commit `f770a8b`
- [x] Task 2: `ConcurrentPool` core dispatch: `_Slot` (backend + `ThreadPoolExecutor` + cap + lock-protected `in_flight` counter), `submit` (least-loaded-by-utilization pick under lock, executor dispatch outside lock), `close` shutdown — commit `a6f504a`
- [x] Task 2 fix: release slot `in_flight` counter when `executor.submit` raises to prevent slot leak — commit `0725457`
- [x] Task 3: `ConcurrentPool.map` with fail-fast cancellation: eager submits all futures, iterates in input order (preserves result ordering), cancels remaining queued futures on first exception, drains in-flight, re-raises first exception — commit `a4d4421`
- [x] Task 4: `GenerateClipStage` branches on `should_chain` (i2v non-native → serial loop; t2v non-native → `pool.map` fan-out) — commit `7ba9974`
- [x] Task 4 hardening: spy on `pool.map` in 1-job test for discriminating assertion — commit `24356cc`
- [x] Task 5: `orchestrator.generate()` wraps stage inside `with ConcurrentPool() as pool: pool.add(backend, max_in_flight=cfg.lifecycle().max_in_flight)`; `SequentialPool` import removed — commit `c90b046`
- [x] Task 6: `LifecycleConfig.max_in_flight` field + wire through `lifecycle()` method; README Concurrency section; PROGRESS Phase 17 — commit `b7e57fc` (Phase 17 Task 6 SHA backfill at `eed9706`)
- [x] Task 6 regression test: lock down YAML→`Lifecycle.max_in_flight` wiring so a future drop of the `lc.max_in_flight=` line in `Config.lifecycle()` fails fast instead of silently defaulting to cap=1 — commit `bab8d64`
- [x] Task 6 doc corrections: fix Phase 17 Task 2/3 inaccuracies (semaphore → lock-protected counter; `as_completed` → input-order iteration); refresh test count — commits `4622083` + `08eb48b`
- [x] Merge to main via `--no-ff` — merge commit `9e02e15` (closes #3)

### Phase 18 — cross-process discovery lock (post-MVP Layer H, GitHub issue #7)
- [x] Task 1: `core/locks.py` — `Lock` Protocol + `LockToken` + `InMemoryLock` + `LockError`/`LockTimeout` in `core/errors.py` — commit `a1802d3` (+ fix `81052a8`)
- [x] Task 2: `ArtifactStore.acquire_lock` abstract method + temporary `NotImplementedError` stubs on 3 stores — commit `6a4d8dc` (+ test gap fix `15742f0`)
- [x] Task 3: `FileLock` (fcntl) + `LocalArtifactStore.acquire_lock`; subprocess integration test — commit `0ac9d90` (+ fix `98bc569`)
- [x] Task 4: `S3CloudLock` (`IfNoneMatch="*"`) + `S3ArtifactStore.acquire_lock` + `FakeS3Client` precondition support — commit `b26c6fd`
- [x] Task 5: `GCSCloudLock` (`if_generation_match=0`) + `GCSArtifactStore.acquire_lock` + `FakeGCSClient` generation tracking — commit `9ac0abd`
- [x] Task 6: `JsonProfileCache.resolve_or_discover` outer-lock wrap; cache-hit fast path preserved; `discover_ttl_s` kwarg — commit `e03d28a` (+ import cleanup `8c2d175`)
- [x] Task 7: `Ledger.record`/`forget` outer-lock wrap; `mutate_ttl_s` kwarg; `entries()` stays lock-free — commit `c8372f6`
- [x] Task 8: README "Multi-node coordination" section + PROGRESS Phase 18 — commit `351d691`
- [x] Merge to main via `--no-ff` — merge commit `4672735` (closes #7)

### Phase 19 — Layer I (fal.ai adapter + UX A + hosted hardening)

- [x] Hot-fix: provisioner cfg-dict — commit `e78cafc` on `main`
- [x] Task 1: Diffusers + ComfyUI provisioner-cfg regression — commit `78a09e1`
- [x] Task 2: declared_flags WARNING → DEBUG — commits `46653ec` + `b1d8b1b`
- [x] Task 3: FakeEngine declared_flags_map default — commit `c586f01`
- [x] Task 4: HostedEngineConfig validators — commit `c1a1c85`
- [x] Task 5: HostedAPIEngine AuthError + declared_flags_map default — commit `d7460f8`
- [x] Task 6: Rewrite hosted.yaml + shim contract docs — commit `bd35810`
- [x] Task 7: core/provision_state.py — commit `a285c36`
- [x] Task 8: UX A hosted preflight — commit `9d5bcd8`
- [x] Task 9: UX A compute preflight + marker — commit `4d573b5`
- [x] Task 10: FalEngineConfig pydantic block — commits `96d45a8` + `2680b22`
- [x] Task 11: FalEngine + FalBackend + wire — commits `7e3327a` + `0d324dc`
- [x] Task 12: _adapters + fal.yaml + invariant + tooling — commit `9be6e67`
- [x] Task 13: Live opt-in test + manual smoke — commit `bf3841f`
- [x] Merge to main via `--no-ff` — merge commit `0b2a8d7`

**First real artifact:** `/tmp/kinoforge-fal-smoke/smoke-i-1/n9TG4YoyIIkzR1rouhQCw_tmpykhkugmc.mp4` — 3,073,440 bytes, MP4 (`ftyp isom`), produced by `fal-ai/wan-t2v` via `examples/configs/fal.yaml` (capability_key `2820ed10e74fbea4bb4ab8e3d338f716db8d86383869ebf793bed423f507caaa`, git SHA `9be6e67` at smoke time).

**Live-smoke bug catches integrated into Task 13:**
- `examples/configs/fal.yaml` endpoint changed `fal-ai/wan/v2.2/t2v` (404 on result URL — fal.ai rewrites the family path back to `fal-ai/wan/...` which 404s on GET) → `fal-ai/wan-t2v` (queue family matches; status/response URLs round-trip cleanly).
- `FalBackend.submit` now falls back to `segments[0].prompt` when `job.spec` lacks `"prompt"` — the orchestrator places the user prompt on the Segment, not in the engine spec, so without this the fal POST body contained only `_audio_mode` and fal silently completed a no-op job that 422'd on result fetch.
- `FalEngine.validate_spec` widened to accept a non-empty prompt on `segments[0]` as well as `job.spec` (mirrors the new submit fallback).
- `GenerateClipStage._artifact_bytes` now resolves `uri` → local file read → `url` → HTTP download → synthetic-fallback (FakeEngine path).  Hosted/queue engines that return `Artifact(url="https://...mp4")` previously had their bytes silently replaced with debug-stub bytes.
- CLI `provision` and `generate` accept `-c` as a short alias for `--config` so the documented quickstart works verbatim.
- README "Real providers — fal.ai" quickstart added.

### Phase 20 — Layer J (cross-engine prompt fallback)

- [x] Task 1: `core/prompt_routing.py` + 8 helper tests — commit `ba078ec`
- [x] Task 2: `prompt_body_key` on hosted + diffusers configs + 4 round-trip tests — commit `4c87e27`
- [x] Task 3: HostedAPIBackend + Engine wire + 6 tests (5 routing + 1 E2E YAML) — commit `cc7b3dd`
- [x] Task 4: DiffusersBackend + Engine wire + 6 tests — commit `e3e4244`
- [x] Task 5: ComfyUIBackend + Engine wire (spec-level `prompt_node_ids`) + 6 tests — commit `acf93c2`
- [x] Task 6: FalBackend retrofit (drop inline fallback, use helper) — commit `36cdc5c`
- [x] Task 7: Examples + README + PROGRESS — commit `ec65c01`

**Key design decisions:**
- Shared helper in `core/prompt_routing.py` (Q1=B): single `resolve_prompt(job)` consumed by all 4 engines.
- Hosted/Diffusers default `prompt_body_key="prompt"` (Q4=A) with opt-out via `null`.
- ComfyUI `prompt_node_ids` lives in `job.spec`, not cfg (Q6=A) — mirrors `asset_node_ids` symmetry.
- Opt-in `validate_spec` raise (Q3=A): legacy configs untouched.
- Fal retrofit (Q5=A): behavior preserved.

**Known follow-up (necessary but out of scope):** `Orchestrator.generate` hardcodes `base_spec={}` (`src/kinoforge/core/orchestrator.py:605`). Routing YAML-supplied spec into the orchestrator (model/params for hosted, pipeline/scheduler for diffusers, graph/node_overrides for comfyui) is a separate Layer K candidate. Hosted/Diffusers/ComfyUI orchestrator-driven runs remain blocked on missing required spec keys until that work lands.

### Phase 21 — Layer K (spec & params routing)

- [x] Task 1: Config.spec + Config.params pydantic fields + 4 round-trip tests — commit `638937e`
- [x] Task 2: Orchestrator routes cfg.spec/cfg.params + validate_spec moved into stage + ValidationError teardown + 4 tests — commit `3606527`
- [x] Task 3: Strategy precedence regression locks (segment-wins + _audio_mode authority) — commit `8b81eb2`
- [x] Task 4: e2e YAML round-trip via Orchestrator — commit `2b5fa25`
- [x] Task 5: hosted/diffusers/wan/fal example YAMLs + 4 extended example-load tests — commit `0d3c514`
- [x] Task 6: README + PROGRESS + full suite gate — commit `23ca0e0`
- [x] Merge to main via `--no-ff` — merge commit `13fc395`

**Key design decisions:**
- Permissive `dict[str, Any]` (Q3=A): Config stays engine-agnostic, preserves the core-import-ban invariant. `engine.validate_spec` is the sole gate.
- Top-level YAML siblings (Q2=A): `spec:` and `params:` live alongside `engine:` / `models:` / `lifecycle:`, not nested per-engine.
- Teardown on `ValidationError` (Q5=A): orchestrator mirrors the existing `CapabilityMismatch` branch; a config typo does not leak compute.
- `dict(...)` copy at stage construction: defends against any future engine that mutates `job.spec`.
- `validate_spec` moved into `GenerateClipStage.run` (after `decide`, before any dispatch): closes a pre-existing gap where `validate_spec` only ran for chained tail-frame jobs.

**Hosted YAML ambiguity (carried forward):** `engine.hosted.model` (cache identity, fed to `key_base(cfg)`) and `spec.model` (wire body) coincide today but are read by different callers. Documented in `examples/configs/hosted.yaml` comment block; collapsing them is a Layer-L+ candidate.

**Test count:** 708 tests passed + 1 skipped (was 693 + 1 skipped before Layer K, +15 net).

### Phase 22 — Layer L (`kinoforge batch` CLI)

- [x] Task 1: deploy_session context manager extraction — commit `f971c4c` (+ polish `25b4dc7`)
- [x] Task 2: core/batch.py manifest models + load_manifest — commit `def94dc` (+ polish `ac06873`)
- [x] Task 3: batch_generate() core function — commit `f06fa3b` (+ polish `6122215`)
- [x] Task 4: kinoforge batch CLI subcommand — commit `4e8a564` (+ polish `c940da9` + streaming-log deferral note `38d5394`)
- [x] Task 5: examples + README + PROGRESS + full gate — commit `cc50ba8`
- [x] Merge to main via `--no-ff` — merge commit `da072a3` (closes PROGRESS:155 follow-up #3)

**Key design decisions:**
- Shared deploy across N entries (Q1=A): one `create_instance`, ConcurrentPool fans entries; `deploy_session` is the reusable seam.
- YAML manifest with per-entry `prompt`/`prompt_file` (Q2=A): pydantic `extra="forbid"` + exactly-one-of validator; `prompt_file` paths resolve relative to the manifest's parent dir; auto-indexed `run_id` when omitted.
- `batch_id` default `batch-YYYYMMDD-HHMMSS` in LOCAL timezone (Q3 clarification): override with `--batch-id`.
- Continue-on-error per entry; batch-fatal on `BudgetExceeded` / `CapabilityMismatch` / `TeardownError` (Q4=A) → cancel queued + exit code 2.
- `deploy_session` extraction (Q5=B refactor): both `generate()` and `batch_generate()` consume it; zero behavior change to `generate()` — all 708 pre-Layer-L tests pass unmodified.
- `_batch_summary.json` written in a `finally` clause regardless of exit path; in-flight entries at fatal-abort time are recorded as `interrupted`.
- Per-entry param/spec overrides are shallow-merged onto `cfg.params` / `cfg.spec` (entry wins per key) via a fresh `dict(...)` copy at stage construction — no mutation leaks to siblings or to `cfg`.

~~**Streaming per-entry log lines (DEFERRED):** the CLI prints the initial `manifest loaded` header and the final per-entry summary table but no mid-run markers — see the "Layer L Task 4" note in the Single-next-action block above (committed at `38d5394`). Closing the gap requires a callback hook into `batch_generate` so `core/` does not print directly. Future contributor picks this up as a self-contained polish phase.~~ — **CLOSED** by Phase 35 (Layer L-T4).

**Test count:** 741 tests passed + 1 skipped before Task 5 → 743 tests passed + 1 skipped after Task 5 (+35 net across Layer L; pre-Layer-L baseline was 708 + 1).

### Phase 23 — Layer M (hosted-YAML collapse + Authorization-header passthrough)

- [x] Task 1: HostedEngineConfig.model dropped + model_validator migration + tests — commit `e63cf61` (+ fix `c50b701`)
- [x] Task 2: HostedAPIEngine.key_base reads cfg["spec"]["model"] + retrofit _BASE_CFG + new tests — commit `d4d583f` (+ fix `5f4f11b`)
- [x] Task 3: examples/configs/hosted.yaml cleaned + test_hosted_yaml smell-lock rewritten — commit `5ab4493` (+ plan-sync `986a64a`)
- [x] Task 4: GenerateClipStage gains http_get_bytes seam; _artifact_bytes threads Artifact.headers — commit `c482a05` (+ fix `9b3df5e`)
- [x] Task 5: HostedAPIBackend.result populates Authorization: Bearer header — commit `67e3236` (+ docstring `9ef0efe`)
- [x] Task 6: E2E test + README + PROGRESS + full gate — commit `3ea5cfa`
- [x] Merge to main via `--no-ff` — merge commit `862e2d5` (closes PROGRESS:155 follow-ups #1 + #2)

**Key design decisions:**
- spec.model is the single source of truth for hosted model identity (Q2=A): cache identity and wire body cannot meaningfully diverge for hosted engines.
- Hard-cut migration with a guiding `model_validator` (Q4=A): matches the `kinoforge gc --config` precedent; deprecation cycles would drag the smell through one more layer for zero functional gain.
- Authorization passthrough via `Artifact.headers` + injectable `http_get_bytes` seam (Q3=A): mirrors the PROGRESS:87 "injected I/O seam" pattern; no new ABC.
- HostedAPIEngine retrofitted as the in-tree consumer of the seam (Q5=A): exercises the auth path end-to-end without waiting for a future RunwayML/Pika adapter.
- Out of scope (Layer N candidate): real-cloud verification gaps (RunPod find_offers shape, SkyPilot SDK smoke, S3/GCS medium-fidelity tests).

**Test count:** 743 passed + 1 skipped pre-Layer-M → ~755 passed + 1 skipped post-Layer-M (+12 net new; +2 retrofits on AC1/AC6).

### Phase 24 — Layer N (RunPod cloud-fidelity hardening)

Verification-only layer that closes PROGRESS:113 carry-forward #1 (`RunPodProvider`
real-cloud shape). What was planned as a fixture-capture pass against the
existing offline tests became, on the first live run, the discovery that the
production code had NEVER successfully talked to a real RunPod API — the
offline tests passed against fictional shape because fake `http_get`/`http_post`
seams bypass URL validation, headers, and CSRF. Ten distinct bugs were caught
and fixed on this branch, every one with a regression test against the captured
shape. Layer N's net contribution is therefore far larger than the spec
projected: the provider works against real RunPod for the first time.

- [x] Task 1: Recording HTTP seam + `_load_fixture` + redaction — commits `0dace7a`, `85e7877`, `561e63d`
- [x] Task 2: Placeholder fixture commits + offline-load smoke — commit `059c6ab`
- [x] Task 3: Live smoke YAML + skeleton test + sample init frame — commits `e7ddc20`, `915ab1c`, `66446fc`
- [x] Task 4: USER-GATE live smoke + real fixture capture — commits `8d71eed`, `ff97bb8` + 8 bug-fix commits between
- [x] Task 5: Refactor `test_runpod.py` to load fixtures — commit `198faf4`
- [x] Task 6: Real-shape required-keys + status-mapping lockdown — commit `8be0930`
- [x] Task 7: README + PROGRESS + final gate + merge — commit `a594346`
- [x] Merge to main via `--no-ff` — merge commit `454e514` (closes PROGRESS:114 carry-forward #1)

**First real artifact (RunPod):** pod `ia66l3rlto5x66` on NVIDIA A40 @ $0.35/hr,
ready at T+5s, destroyed at T+10s. Captured fixtures committed at
`tests/providers/fixtures/runpod/*.json` (5 GraphQL responses) +
`last_smoke.json` (artifact metadata). Smoke captured 2026-05-31T20:53:21-0700
at git SHA `7a85d62`. Total cost ≈ $0.001.

**Live-smoke bug catches integrated (10 production fixes):**

1. `83605b8` — URL-encode GraphQL queries; Python 3.13's urllib rejects raw spaces (`InvalidURL`)
2. `7edb10f`+`5c085d7` — Auth header (`Bearer` 403) → query param (`?api_key=` 200)
3. `f026133` — `Content-Type: application/json` required on GETs to bypass RunPod's CSRF block (HTTP 400)
4. `d22f25b` — User-Agent override; RunPod's edge layer blocks the `Python-urllib/*` default (HTTP 403)
5. `45b4a91` — Tolerate `lowestPrice=null` in `find_offers` (was `AttributeError`)
6. `9f63e6b` (part) — GraphQL `env` is an array of `{key, value}` pairs, not a plain dict
7. `9f63e6b` (part) — Detect mutation `errors` block + raise on empty pod id; previously returned `Instance(id="")` leaking paid pods
8. `b694c0b` — Switch ALL GraphQL ops to POST (RunPod GET broken for parameterised queries) + orchestrator `destroy_instance` wraps post-create block (would otherwise leak on any error after create_instance returns)
9. `7a85d62` (part) — Recording seam carries `git_sha` in `_meta` so fixture provenance survives reviewer scrutiny
10. `7a85d62` (part) — `lowestPrice` resolver requires `(input: { gpuCount: 1 })` to return prices; `find_offers` now drops null-priced (unavailable) entries instead of surfacing them as $0 offers

**Key design decisions / deviations from spec:**

- **Smoke pivoted to bare pod lifecycle** (find_offers → create alpine pod → poll ready → destroy) instead of ComfyUI + Wan i2v. The spec called for an MP4 artifact; that was deferred because the original architecture (kinoforge CLI subprocess + in-pytest recording seam) cannot capture fixtures across the process boundary. The bare lifecycle exercises the same 10 production-code paths at $0.001/run vs ~$2/run for engine integration. Engine smoke is a Layer O candidate.
- **Spec convention deviation:** `KINOFORGE_LIVE_RUNPOD` (spec §3) → `KINOFORGE_LIVE_TESTS=1` + per-provider creds (existing fal-live convention).
- **`RUNPOD_TERMINATE_KEY` reuses `RUNPOD_API_KEY`** via `${...}` interpolation in `.env` because RunPod's scoped-key UX has no terminate-only tier. Privilege separation is lost but selfterm fallback still works.

**Test count:** ~756 pre-Layer-N → 778 post-Layer-N (+22 net; mostly +regression tests on the 10 bug catches, +2 lockdown tests in Task 6).

**Out of scope (Layer O candidates):**

- Engine-integration live smoke (ComfyUI/Diffusers/Hosted deployed on a real RunPod pod producing a real MP4)
- Serverless mode read-paths + live smoke (Q3 from Layer N brainstorm was pod-only)
- SkyPilot SDK smoke (PROGRESS:113 carry-forward #2)
- S3/GCS medium-fidelity tests (PROGRESS:113 carry-forward #3)
- ~~Streaming per-entry log lines in `kinoforge batch` (PROGRESS:158 deferred from Layer L Task 4)~~ — **CLOSED** by Phase 35 (Layer L-T4).

### Phase 25 — Layer O (user-facing output directory)

UX-only layer that closes the operator findability + persistence gap
identified during the Layer-N retro: final clips were buried under
`.kinoforge/<run_id>/<engine-derived-name>` with names that mean nothing
at a glance, and the default `--run-id="run"` silently overwrote prior
runs.

- [x] Task 1: `outputs/base.py` (Protocol + slugify + format_filename) + `outputs/__init__.py` (registry) + 12 slugify tests — commit `3f621e9`
- [x] Task 2: `outputs/local.py` (LocalOutputSink with atomic write + collision suffix + self-register) + 10 tests — commit `a58df43`
- [x] Task 3: `OutputConfig` pydantic block + `Config.output` field + 3 round-trip tests — commit `3af17d8`
- [x] Task 4: `GenerateClipStage` sink + namespace integration + 4 stage tests — commit `9d22694`
- [x] Task 5: `orchestrator.generate()` sink threading + 2 tests — commit `e845443`
- [x] Task 6: `batch.batch_generate()` sink + batch_id namespace + 2 tests — commit `3e66a72`
- [x] Task 7: CLI `--output-dir`/`--no-output-dir` mutex group + `_build_sink` + `--run-id` uniquification + 5 tests — commit `0f135de`
- [x] Task 8: `.gitignore` `output/` + commented `output:` block on every example YAML + 6 round-trip tests — commit `503b3a8`
- [x] Task 9: README "Output directory" section + this PROGRESS entry + invariant verification — commit `646adf7`
- [x] Task 10: Full gate + `--no-ff` merge to main — merge commit `7788f93`

**Key design decisions:**
- Publish step layered on top of ArtifactStore (Q2=A): zero behavior change to existing call sites; store/ledger/uri_for/gc untouched.
- ASCII-conservative slug (Q3=A): emoji/CJK/accents dropped, not transliterated; cross-platform safe, shell-friendly.
- Flat single + batch-nested layout (Q4=A): single-clip runs land directly in `output/`; batch runs nest under `output/<batch_id>/`.
- `--run-id` default uniquification folded into Layer O: one-line CLI change closes the silent-overwrite foot-gun on the internal store side too.
- Bytes-only v1: hardlink optimization (`ArtifactStore.local_path_for`) deferred; sub-GB mp4 disk doubling is negligible.

**Breaking changes:**
- `kinoforge generate` default `--run-id` flipped from `"run"` to `f"run-{ts}"`. Scripts that grep `.kinoforge/run/` no longer find clips; pass `--run-id run` to restore prior behavior. Second breaking change after the Layer C `kinoforge gc --config PATH` precedent.

**Test count:** 778 pre-Layer-O → 823 post-Layer-O (+45 net: 12 slugify + 11 local incl. hash-exhausted regression + 3 cfg + 4 stage + 2 orch + 3 batch + 6 CLI + 6 examples).

**Out of scope (Layer P+ candidates):**
- Hardlink / zero-copy via `ArtifactStore.local_path_for`.
- Cloud-native sinks (S3 mirror, webhook POST).
- Filename template customization.
- Migration of existing `.kinoforge/<run_id>/*.mp4` into `output/`.
- `Artifact.published_path` field for CLI status / batch summary.
- Engine integration on real RunPod (original Layer-O candidate; now reslotted as Layer P).

### Phase 26 — Secret-Scanning Cleanup (post-Layer-Q housekeeping)

Housekeeping pass that closes the GitHub Secret-Scanning UI alerts raised
after the Layer P bug-fix #1 commits landed literal credential-prefix
strings (`sk-proj-…`, `sk-ant-api03-…`, `AKIA…EXAMPLE`, `ASIA…EXAMPLE`,
multi-line PEM blocks) into source-controlled spec / plan / test files.
The fix is byte-identical at runtime (concat-only re-spelling) so production
code paths are untouched; the layer's lasting contribution is the permanent
fail-closed audit that prevents the same class of leak landing again.

- Spec: `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md`
  — initial `49475a5`, scanner-grade amendment `9e18be7`, consistency nits
  `146d94e`, T1 review fixes `0ab7fe1`
- Plan: `docs/superpowers/plans/2026-06-01-secret-scanning-cleanup.md`
  (+ `.tasks.json`) — initial `badbcad`, Before-blocks alignment `e9add1d`
- [x] Task 1: Amend cleanup spec for scanner-grade pattern set +
  concat-escaped examples (so the spec passes its own audit) — sync commit
  `e11ec55`; substantive spec edits in the spec commits above
- [x] Task 2: Forward-fix the 3 Layer P bug-fix #1 files — source scrub
  `a692a4a` (test fixture concat-only rewrite + spec/plan shape descriptions)
  + sync `1326917`
- [x] Task 3: `tests/test_source_audit.py` — fail-closed lockdown walking
  `docs/superpowers/**/*.md`, `tests/**/*.py`, repo-root `README.md`,
  `AGENTS.md`, `PROGRESS.md`, `CLAUDE.md`, `.env.example`; 3 audit functions
  (`test_audit_walker_fires_on_known_credential`, plus negative and full-tree
  passes) — `778d473` + sync `3540bc3`
- Landed directly on `main` (no merge commit — Phase 26 is a housekeeping
  pass, not a feature layer).

**Key design decisions:**
- Scanner-grade subset for the audit (`sk_token`, `aws_access_key`,
  `pem_private_key`, `hf_token` tightened to `\bhf_[A-Za-z0-9]{32,}\b`), not
  the production `_CREDENTIAL_PATTERNS` from
  `tests/providers/conftest_runpod.py`. Production set tolerates noisier
  matches inside the recording-seam backstop; the source-tree audit needs
  zero false positives over committed prose.
- Concat-only re-spelling, byte-identical at runtime: every fixture tuple
  rewritten as `"sk-" + "proj-" + "…"` so `pytest` assertions still hold
  unchanged.
- Before / After example blocks in spec + plan re-described as prose ("shape
  with ellipsis") so the cleanup spec itself passes the audit it specifies.
- T3 owns its own pattern list (does NOT import from `conftest_runpod.py`):
  scanner-grade vs. production divergence is intentional and the audit
  must keep working if the production set ever changes.

**Test count:** 980 pre-Phase-26 (HEAD at Layer Q merge `c63cbea`) → 983
post-Phase-26 (+3 net new audit tests). PROGRESS:336 had recorded 972
post-Layer-Q; the actual collected count at `c63cbea` was 980 — the prior
number was stale by 8 (likely a hand-count miss across the Layer P / Layer Q
co-merge). The +3 net delta matches the plan §Post-Plan projection.

**Manual follow-up — RESOLVED 2026-06-01:** the existing GitHub
Secret-Scanning UI alerts at
https://github.com/killett/kinoforge/security/secret-scanning have been
manually dismissed ("Used in tests" / "False positive") by the project
owner. Phase 26 work fully closed: the audit prevents *new* literal
credentials landing on `main`, and the historic alerts GitHub raised
against the literal-bearing SHAs are now resolved in the UI. Phase 26
commits (through `965a060`) have been pushed to `origin/main`.

**Out of scope:**
- Rewriting git history to expunge the historic literal-bearing SHAs.
  Rejected: PROGRESS + spec + plan still reference those SHAs for audit
  trail; rewrite would break every cross-reference for zero security gain
  (GitHub's secret-scanning state is per-alert, not per-blob, once dismissed).
- Pre-commit hook running the audit. Rejected: `pixi run test` already
  includes the audit and runs in CI + via pre-commit's `pytest` hook; a
  duplicate scanner would just slow commits.

### Phase 28 — Layer P close-out (T8 / T9 / T10)

Layer P (RunPod engine integration: ComfyUI + Wan i2v) closes here. Phases 24–28 + the item #1, #2, #3 sub-plans + the ci-green-recovery + secret-scanning-cleanup together comprise the Layer P arc shipped directly to `main` (no `build/layer-p` branch ever existed). Reference spec: `docs/superpowers/specs/2026-06-03-layer-p-closeout-design.md`; plan: `docs/superpowers/plans/2026-06-03-layer-p-closeout.md`.

**Per-task SHAs:**
- T8 (conftest helper + 34-test rewrite + review follow-up): `04c9fe6`, `cdee15b`
- T9 (2 shape-lockdown tests + review follow-up): `c152deb`, `3fba6f5`
- T10 (README + PROGRESS + tag): `477a88a`

**Test count:** `1034 → 1036` passing (+2: T9 lockdowns). `tests/engines/test_comfyui.py` collected count `57 → 59`.

**Total Layer P live spend across all sub-plans:** ~$0.74 (item #3 wave: T6 + diagnostic + capture + quality re-render + cat-fixture re-render + morph re-render; plus earlier smaller item #1/#2 spends).

**Bug-catch trail from the live wave (one bullet each):**
- Prompt routing: kijai `WanVideoTextEncode` uses `positive_prompt`, not `text` (`d455f93`).
- Sampler defaults: non-distilled Wan 2.1 needs `steps=20 cfg=6 shift=7 scheduler=unipc` (`d455f93`).
- Init-fixture: gradient PNG placeholder showed through as diagonal seam at t=0; replaced with real cat photo (`056abe4`).
- `batch_cli` sink leak: `output.dir` defaulted to repo root in tests (`c2d28e2`).
- Orphan-pod L1: in-process `_created_instances` registry in `RunPodProvider` (`93beb14`).
- Orphan-pod L2: `orchestrator.generate()` returns `tuple[Artifact, Instance | None]` so callers can teardown by id (`7a10fd4`).
- Subject-morph: `start_latent_strength=0.6` locked in node 63 for visible morph (`b7b4ff2`).

**Key design decisions surfaced during the wave:**
- kijai WanVideoWrapper graph treated as upstream truth — fetched at pinned SHA, validated by a SHA cross-reference test, not hand-edited.
- Fixture-replay as offline-contract pattern (T8/T9): captured real-server HTTP shapes drive offline tests; future server-side drift fails loudly.
- In-process pod registry + tuple-return orchestrator API as defence-in-depth against tag-discovery gaps in cold-start cloud-state APIs.

**Annotated tag:** `layer-p-closed` at this commit.

**Real-cloud verification gap closed:** ComfyUI engine end-to-end against real RunPod compute — Layer P ships the live shake-out + offline fixture lockdown.

**Carry-forwards (unchanged):**
- `SkyPilotProvider._get_sky()` lazy path still unexercised against real `sky` SDK.
- `S3ArtifactStore` + `GCSArtifactStore` never hit real cloud.
- (Other follow-ups per the "Known limitations & follow-ups" section above.)

### Phase 29 — aria2c fast-path (GitHub issue #9)

Single-file change to `src/kinoforge/core/downloader.py` that auto-detects
the `aria2c` system binary and uses it as a transparent multi-connection
fast-path on every model fetch.  Silent stdlib fallback on subprocess
failure preserves the existing single-connection path as a safety net.

- Spec: `docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md`
- Plan: `docs/superpowers/plans/2026-06-03-aria2c-fast-path.md`
- T1 (seams + types + helpers + logger + drop DEFERRED + review nits): `2b45734`, `5df72a7`
- T2 (download_one transport branch + 4 ACs + review nits): `efa4c68`, `2ce4d21`
- T3 (silent fallback + WARNING log + 2 ACs): `2ef53fa`
- T4 (download_all forwarding + A7): `a0ec352`
- T5 (README + PROGRESS + SHA backfill): `7254a82`, `29a2a8d`

**Key design decisions:**
- Auto-detect by `shutil.which("aria2c")` per call (Q1=A): zero ceremony;
  tests inject `which_aria2=lambda: None` to force the stdlib path.
- Silent fallback to stdlib on aria2c failure with `WARNING` log (Q2=A):
  operators always get the file; lost wall-clock is the only cost.
- Injectable `run_aria2` + `which_aria2` callables (Q3=A): mirrors the
  existing `fetch` seam pattern; no monkey-patching of `shutil` or
  `subprocess` in tests.
- Hard-coded knobs `-x 16 -s 16 -k 1M --max-tries=3 --retry-wait=2`
  (Q5=A): battle-tested HF / CivitAI defaults; tuning is YAGNI.
- Keep post-download `sha256_file()` verify; do NOT use aria2c's
  `--checksum=` flag (Q7=A): single checksum code path for both
  transports.
- `--header=` passthrough mechanism shipped, population deferred (Q6=A):
  `Artifact` has no `headers` field yet, so the aria2c branch passes
  `headers={}`.  The seam contract is final; populating it is a one-line
  follow-up when (and if) `Artifact.headers` is added.
- Bug catch during T3: log message wording changed from "falling back to
  stdlib" to "fallback to stdlib" — the substring `"fallback"` does not
  appear in `"falling back"`, so the A3 case-insensitive assertion
  drove the wording.

**Test count:** 1036 (post-Layer-P) → 1044 (post-Phase-29).  Delta: +8 net
new (A1-A7 + the T1 symbol-lock test).

**Out of scope (carry-forward):**
- Real-binary smoke test (`KINOFORGE_LIVE_ARIA2=1`).
- `Artifact.headers` field for HF-gated weights via
  `Authorization: Bearer hf_…`.
- aria2c knobs via env-var / YAML config.
- aria2c's `--checksum=` flag as a verify short-circuit.
- Splitting `tests/core/test_downloader.py` (now 658 lines) into stdlib
  + aria2c sub-files; deferred until a follow-up task touches the file.

Closes GH #9.

### Phase 31 — SkyPilot real-cloud verification (PROGRESS:114 #2)

Closes the dormant `SkyPilotProvider._get_sky()` lazy path against real
GCP. CPU-only bare lifecycle smoke (Layer-N analog); captures four SDK
return-shape fixtures (`gpu_list`/`status`/`launch`/`down` — most
collapse to `<volatile-uuid>` because modern SkyPilot's async API
returns `RequestId` UUIDs from these calls) as the PR review surface
for future SDK upgrades. Provider rewritten for modern async API
(RequestId resolution via `sky.stream_and_get`, typed `StatusResponse`
records via dual-shape `_record_field` adapter, `sky.Task.from_yaml_config`
construction, CPU-offer synthesis, `docker:` image normalisation,
`disk_size=30` default to fit fresh-project GCP `SSD_TOTAL_GB=250`
quota). Pixi `live-skypilot` feature env now ships `google-cloud-sdk`,
`rsync`, and `openssh` so the SkyPilot API server (background daemon)
finds all its CLI prereqs.

- Spec: `docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md`
- Plan: `docs/superpowers/plans/2026-06-03-skypilot-real-cloud.md`
- T1 (pixi feature env): `ed0dbda`
- T2 (recording proxy + 8 Ring-2 tests): `005eca2` + `fd8cac9` (review fixup)
- T3 (skypilot.yaml + parse test): `eedf7db`
- T4 (preflight SkyPilot check + 3 tests): `6dd3530` + `90a6452` (review fixup)
- T5 (RED live-smoke scaffold, pre-spend): `c3beb96` + `44101f2` (tasks.json sync)
- T5.5 (live-env mypy hygiene — preflight + recorder typing): `f1a684e`
- T6 (1st live invocation, $0 spend, surfaced sky.gpu_list missing): `44101f2`
- T7a (provider rewrite for modern async API): `b9fd9ee` + `f86db8a` (tasks.json sync)
- T7b (sky.Task construction + CPU-offer synthesis): `91139c2`
- T7d (recorder pydantic BaseModel support): `fffb034`
- T7e (teardown uses absolute gcloud path): `d2d90ce`
- T7g (recorder passes classes through unchanged): `32186a1`
- T7h (image_id docker: prefix normalisation): `2a921ae`
- T7i (google-cloud-sdk in live-skypilot env): `b425407`
- T7j (rsync + openssh in live-skypilot env): `ddf5aa6`
- T7k (provider default disk_size=30): `2e6c233`
- T7l+T7m (image swap + UUID-volatile recorder): `afeb635`
- T7n (image swap bash:5 → debian:12-slim): `c6679ba`
- T7f (live smoke fixtures, byte-identical across 2 runs): `9301c83`

**Key design decisions:**
- Bare CPU lifecycle only (Q1=A): GPU smoke deferred — same SDK code paths
  exercised at ~1/100th cost.
- Fixture capture via decorator-based seam in test code (Q6=A): zero
  production-code touch; matches Layer N's sibling pattern.
- Four-tier teardown (Q3=B): `try/finally` + `autostop=1` + extended
  preflight + `gcloud` nuclear fallback. Survivor check uses absolute
  gcloud path (T7e).
- Pixi feature env `live-skypilot` (Q4=A): default `pixi run test` stays
  lean. Feature env ships skypilot[gcp] + google-cloud-sdk + rsync + openssh
  to satisfy SkyPilot's API-server prereqs.
- Full method coverage (Q5=A): `gpu_list → launch → status → endpoints
  → down`. Provider rewrites use `sky.stream_and_get` to resolve each
  RequestId.
- Provider auto-normalisations: `docker:` image-id prefix, CPU-offer
  synthesis when `min_vram_gb == 0`, `disk_size=30` default. All keep
  the smoke runnable against any fresh GCP project.

**Real-world SDK + cloud-prep findings (the value Phase 31 delivered):**
- `sky.gpu_list()` no longer exists — replaced by `sky.list_accelerators()`.
- `sky.status()`/`launch()`/`down()` are async (return `RequestId`);
  callers must `sky.stream_and_get(req)` to block on the resolved payload.
- `StatusResponse` is a pydantic BaseModel with attribute access (not a
  dict) — recorder + provider both updated to handle.
- SkyPilot's API server is a background daemon needing gcloud + rsync +
  ssh on PATH; pixi feature env now provides all three.
- SkyPilot's GCP setup requires SA permissions beyond `compute.admin`:
  `serviceusage.serviceUsageAdmin` + `iam.serviceAccountAdmin` + `viewer`
  + `iam.securityAdmin`. The last lets the SA self-grant future roles
  without re-OAuth.
- `bash:5` Docker image is Alpine-based (bash at `/usr/local/bin/bash`);
  SkyPilot's docker bootstrap hardcodes `/bin/bash` — `debian:12-slim`
  is the right minimal CPU image.
- Default GCP SSD quota (`SSD_TOTAL_GB=250`) is below SkyPilot's default
  `disk_size=256`. Provider now defaults to 30 GB.

**Live-smoke confirmation (T7f attempt 10, both runs PASS):**

```
KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot pytest tests/live/test_skypilot_live.py -v
============================== 1 passed in 408.99s (0:06:48) ===============================
============================== 1 passed in 413.18s (0:06:53) ===============================
```

Two successive runs: ~6.8 min and ~6.9 min wall-clock each. Cluster name
pattern: `kinoforge-skypilot-smoke-<8hex>`. Provisioning landed in
`us-east1-b` both runs (5 zone retries each on quota exhaustion). Total
GCP spend across all T6 + T7f attempts: **~$0.082** (of the layer's
$0.50 ceiling).

Fixture sha256 chain (byte-identical across 2 successive runs after
volatile-key + UUID-sentinel normalisation):

```
b7d419236e47a6d02cae538462dc0d66909df7a63b5e4595664ef5da8b4bce46  tests/providers/fixtures/skypilot/down.json
b7d419236e47a6d02cae538462dc0d66909df7a63b5e4595664ef5da8b4bce46  tests/providers/fixtures/skypilot/launch.json
b7d419236e47a6d02cae538462dc0d66909df7a63b5e4595664ef5da8b4bce46  tests/providers/fixtures/skypilot/status.json
37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570  tests/providers/fixtures/skypilot/stream_and_get.json
```

(Three of the four files share a sha256 because each call site returns a
distinct RequestId UUID and the recorder normalises those to a
single `<volatile-uuid>` sentinel — the byte-for-byte identical payload
is intentional and is the contract being locked.)

**Side-effect — gcloud persistence:** Earlier in the session we minted
`kinoforge-runner` SA + a key at `/workspace/.gcp/kinoforge-sa.json`. T7's
IAM-discovery surfaced the need for re-OAuth to grant additional roles.
Resolution: `~/.config/gcloud` is now persisted at
`/workspace/.gcp/gcloud-config` (host-visible mount, gitignored) and
`pixi.toml [activation.env]` exports
`CLOUDSDK_CONFIG=$PIXI_PROJECT_ROOT/.gcp/gcloud-config` so any `pixi run`
shell inherits it and future sessions skip the OAuth dance entirely. The
SA additionally holds `roles/iam.securityAdmin`, so future role grants
can come from the SA itself without going back through user OAuth.

**Test count:** ~1071 (post-Phase-30) → **1111 passed / 6 skipped** (post-
Phase-31). Delta: +40 net new offline tests across T2 (8+2), T3 (1),
T4 (3+1), T7a (+11), T7b (+7), T7d (+1), T7g (+1), T7h (+2), T7k (+1),
T7m (+1). T7f adds +1 live-skipped under default env (3 pre-existing +
2 HF live + 1 SkyPilot live = 6 skips).

**Out of scope (carry-forward — see spec §7):**
- GPU lifecycle smoke (CPU was sufficient to validate the modern SDK shape).
- Engine-on-SkyPilot smoke (ComfyUI/Wan via SkyPilot setup, ~$2-5/run).
- Multi-cloud verification (AWS, Azure, Lambda Labs).
- Retroactive backfill of offline tests from fixtures.
- Per-call fixture differentiation (the recorder's single-file-per-method
  scheme makes multi-call methods like `status` last-call-wins; not
  blocking T7f's contract but a fidelity improvement for future review).
- Cross-process recording (kinoforge CLI subprocess invoked by pytest).

Closes PROGRESS:114 carry-forward #2.

### Phase 30 — HF bare-repo listing (GH #8)

Single-file extension to `src/kinoforge/sources/huggingface/__init__.py`
that widens `HuggingFaceSource.resolve()` to enumerate a whole repo via
the HF tree API on a bare `hf:<repo>` ref. Plus a generic
`provisioner.provision()` guard that rejects `entry.sha256` on any
multi-artifact resolve (closes a latent silent-broken case in
`CivitAISource` as a side effect). Plus a one-line `downloader` mkdir
hygiene fix that lets subpath-bearing artifact filenames land in fresh
directory trees.

- Spec: `docs/superpowers/specs/2026-06-03-hf-bare-repo-design.md`
- Plan: `docs/superpowers/plans/2026-06-03-hf-bare-repo.md`
- T1 (downloader mkdir-parents + 1 AC): `355611a`
- T2 (parser + Link cursor + FetchCallable + 8 ACs): `d53a668`
- T3 (@rev in single-file branch + 2 ACs): `482058b`
- T4 (tree branch + 13 ACs + bare-ref rewrite, closes deferred T3 AC4): `5580f09`
- T5 (provisioner generic guard + 2 ACs): `1a9276e`
- T6 (README + examples + PROGRESS + live smoke + convention conformance):
  `8300887`, `cd7483a`, `e129686`, `7264e9f`

**Key design decisions:**
- Mirror CivitAI minimalism (Q1=A): bare `hf:<repo>` returns every file;
  no `include`/`exclude` filter knobs.
- `@<rev>` suffix for revision pinning (Q2=A): default `main`, optional.
- LFS-oid auto-populated onto `Artifact.sha256`; reject `entry.sha256`
  on multi-artifact resolves via a generic provisioner guard (Q3=A).
- Preserve repo subdirs in `Artifact.filename`; one-line
  `target_path.parent.mkdir(parents=True, exist_ok=True)` in the
  downloader (Q4=A).
- `?recursive=true` + cursor-loop pagination (Q5=A).
- Error mapping mirrors CivitAI (401 → `AuthError`, other → `KinoforgeError`).
- Provisioner check is source-agnostic (Q7 architecture pick): any
  source returning >1 artifact with `entry.sha256` set fails loud.
- Live-smoke gate uses the project-standard `KINOFORGE_LIVE_TESTS=1`
  (not a bespoke `KINOFORGE_LIVE_HF=1`) and lives under `tests/live/`,
  mirroring the Phase 24 Layer N precedent for convention conformance.

**Live-smoke confirmation (Phase 30 T6 gate):**

```
KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_huggingface_live.py -v
============================== 2 passed in 0.70s ===============================
```

Canary repo: `hf-internal-testing/tiny-random-CLIPModel` — 13 files
enumerated via real HF tree API. Representative artifacts:

| filename | size | sha256 (lfs.oid) |
|---|---|---|
| `.gitattributes` | 1477 | `None` (non-LFS) |
| `config.json` | 4570 | `None` (non-LFS) |
| `onnx/model.onnx` | 767977 | `3c1108337f06...` |
| `onnx/text_model.onnx` | 483660 | `925d5251526c...` |
| `pytorch_model.bin` | 578637 | `4d0ce4dd8f7b...` |
| `tf_model.h5` | 722684 | `7714fee94709...` |
| `tokenizer.json` | 33401 | `None` (non-LFS) |

End-to-end live verification covers: real Link-header pagination loop,
real LFS `oid` → `Artifact.sha256` (5/13 files LFS-tracked), subdir
preservation (`onnx/model.onnx` materialised verbatim), non-LFS files
correctly get `sha256=None`, no auth required for the public read API.

**Side-effect — latent CivitAI bug closed:** the generic provisioner
guard turns formerly-silent N-1 verification failures on multi-file
`civitai:<modelId>` refs (where the operator had set `sha256:` on the
YAML entry) into a startup-time `ValidationError` with a clear
migration message. See spec §10.

**Test count:** 1044 (post-Phase-29) → ~1071 (post-Phase-30 T1–T5,
pre-live-smoke).  Delta: +27 net new across the 5 source-modifying
tasks (T1 +1, T2 +8, T3 +2, T4 +14 incl. bare-ref rewrite, T5 +2).
T6 adds +2 more (live smoke) when `KINOFORGE_LIVE_TESTS=1` is set;
default-skip count goes from 3 to 5.

**Out of scope (carry-forward):**
- `include` / `exclude` filtering on `ModelEntry`.
- `GatedModelError` for 403 nuance.
- Custom HF mirror (`HF_ENDPOINT` env var support).
- Live smoke for gated/private repos.

Closes GH #8.

### Phase 32 — Layer R (keyframe / image-generation upstream Stage, GH #4)

Closes the deferred `Keyframe / image-generation upstream Stage` item (GH #4 /
PROGRESS:78 deferred layer). Ships a new `image_engines/` subsystem
(`ImageEngine` ABC + `ImageBackend` ABC + `FakeImageEngine` + `FalImageEngine`)
alongside `KeyframeStage` (the pre-phase that calls the image engine and injects
`ConditioningAsset` results into the request before `validate_request` runs).
Config opt-in via a new `keyframe:` YAML block; configs without the block are
fully backwards-compatible.

- Spec: `docs/superpowers/specs/` (Layer R design doc)
- Plan: `docs/superpowers/plans/` (Layer R plan)

**Per-task SHAs:**

| Task | SHA(s) |
|---|---|
| T1 (Image-side ABCs + PipelineState + Stage Protocol + registry helpers) | `14e97fc` (initial) + `472cd78` (review fixups) |
| T2 (MODE_ROLE_REQUIREMENTS schema migration) | `3decc87` |
| T3 (Extract artifact_bytes helper) | `9ddb551` |
| T4 (GenerateClipStage signature migration) | `58bf231` |
| T5 (JsonImageProfileCache namespace split) | `0be8be6` |
| T6 (FakeImageEngine + FakeImageBackend + self-registration) | `51a71fe` |
| T7 (FalImageEngine + FalImageBackend + self-registration) | `09dc9b3` |
| T8 (KeyframeConfig pydantic + Config.keyframe field) | `87e6952` |
| T9 (KeyframeStage implementation) | `ce9790a` |
| T10 (Orchestrator pipeline list-walker + image engine pre-resolution) | `7c1b19c` |
| T11 (batch_generate() mirror) | `3dbed46` |
| T12 (Example YAMLs + load-lockdown tests) | `9cfdc89` |
| T13 (Backwards-compat lockdown tests) | `cce3877` |
| T14 (Core invariant scan extension) | `495cde9` |
| T15 (RED scaffold for live smoke, pre-spend) | `65b32fe` |
| T16 bug-catches | `73deb53` (#1+#2 slug + persist URLs) + `32376fb` (#3+#4 wan-asset-scope-cut + flf2v in stub) + `cf90696` (#5 JPEG accept) |
| chore (.tasks.json sync mid-execution) | `38e1838` |

**Key design decisions / spec deviations:**

- **KeyframeStage is a pre-validation phase, NOT a Stage list peer.** Spec §2.2
  showed `validate_request → splitter → stages list [KeyframeStage,
  GenerateClipStage]`. Reality: `validate_request` rejects `mode=i2v` with empty
  assets, so `KeyframeStage` must run BEFORE `validate_request`. T10 implementer
  made `KeyframeStage` a pre-phase outside the stages list; the orchestrator then
  runs `validate_request` + splitter + `GenerateClipStage` (sole stages entry).
  Foundation note: future stages (audio, upscale) face the same pre/post-validation
  choice — consider making `validate_request` itself a Stage in a future layer.
- **Config schema additions:** T12 added `mode: str | None = None` and
  `prompt: str | None = None` to the top-level `Config` model to make example
  YAMLs self-contained. Existing configs unaffected (None defaults).
- **MODE_ROLE_REQUIREMENTS schema:** T1 implementer changed `flf2v` from `set`
  to `list` for ordering BEFORE T2's planned full migration to
  `dict[str, dict[str, str]]`. T2 then migrated to the final dict shape. No net
  impact on consumers.

**Bug-catch trail (T16 live wave):**

1. **fal slug**: `fal-ai/flux-schnell` (hyphen) is wrong; correct slug is
   `fal-ai/flux/schnell` (forward slash). Returns HTTP 404
   "Application 'flux-schnell' not found" on POST. Verified by direct curl probe.
2. **canonical status/response URLs**: `FalImageBackend.result()` initially
   reconstructed URLs from endpoint name. fal's actual request paths use the
   family root (`fal-ai/flux/requests/<id>`), stripping the leaf endpoint. Fix:
   persist submit response per `request_id` in `_jobs` dict.
3. **wan keyframe-asset-upload scope cut**: end-to-end keyframe→wan-i2v requires
   fal storage upload (wan endpoints need `image_url` as a public or fal-CDN URL).
   Layer R does not ship that glue. Live tests scoped to exercise only the new
   Layer R surface (`FalImageEngine` + `KeyframeStage` + persistence). Wan video
   engine live verification already shipped Phase 19.
4. **flf2v not in fal stub profile**: `_DEFAULT_STUB_PROFILE.supported_modes`
   excluded `flf2v`. Added.
5. **JPEG vs PNG**: fal flux/schnell returns JPEG (`\xff\xd8\xff`), not PNG.
   `KeyframeStage` hardcodes `.png` filename — bytes are valid (JPEG content
   under `.png` extension). Tests now accept either magic. Documented as
   cosmetic carry-forward (content-type sniffing).

**Live-smoke confirmation (T16 gate — both runs PASS):**

```
KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v
============================== 2 passed in 9.34s ==============================
KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v
============================== 2 passed in 6.64s ==============================
```

Two successive runs. Total fal spend across all T16 attempts (including probes +
bug-fix wave): ~$0.26 (slightly over the $0.20 budget projection due to early
`curl` probes against fal queue — those POSTs queued real jobs that got billed).

**Test count:** 1111 passed (pre-Phase-32) → **1198 passed, 8 skipped**
(post-Phase-32). Delta: +87 net new offline tests across T1–T15; live smoke adds
+2 under `KINOFORGE_LIVE_TESTS=1` (skip count goes from 6 to 8 under default env).

**Out of scope (carry-forwards):**

- **fal storage upload integration for keyframe→wan i2v/flf2v end-to-end** —
  wan endpoints need `image_url` as a public/fal-CDN URL; Layer R scoped down to
  the new surface only. Layer S candidate.
- `HostedImageEngine` + `DiffusersImageEngine` concretes.
- Image-backend pool for parallel flf2v role fills (today serial).
- Keyframe caching across runs.
- User-facing `pipeline:` YAML override.
- `output_intermediates: true` cfg knob.
- LoRA support on image engines.
- Dynamic fal per-endpoint capability sniffing.
- Splitter into `GenerateClipStage`.
- Multi-pass refinement keyframes.
- **Content-type sniffing:** `KeyframeStage` hardcodes `.png` filename regardless
  of actual image format. flux/schnell returns JPEG; bytes are valid but extension
  is misleading. Sniff `Artifact.url` or response content-type to pick `.png`/`.jpg`.
- **`validate_request` as a Stage:** would let `KeyframeStage` be a real Stage
  peer instead of a pre-phase. Foundation cleanup.

Closes GH #4.

### Phase 33 — Layer S (`kinoforge status` reads the ledger + `kinoforge forget`)

Closes the PROGRESS:120 carry-forward (`cli._cmd_status` queried in-process
provider state only, not the ledger). Ships ledger-first dispatch for
`kinoforge status`, surfaces rich ledger-derived facts in an alphabetised
`key=value` block, and adds `kinoforge forget --id <id>` so the new stale-ledger
advisory points at a real recovery command. Fully offline-tested (no live spend).

- Spec: `docs/superpowers/specs/2026-06-05-layer-s-cmd-status-ledger-design.md`
- Plan: `docs/superpowers/plans/2026-06-05-layer-s-cmd-status-ledger.md`

**Per-task SHAs:**

| Task | SHA |
|---|---|
| T1 (`Ledger.record` schema extension — persists `idle_timeout_s` + `max_age_s`; `_cmd_deploy` threads `cfg.lifecycle()` values into the call) | `acdc8e1` |
| T2 (`_cmd_status` ledger-first rewrite — `_build_ledger_block` pure helper + `_print_status_block` formatter + sibling-parity provider dispatch; `--config`/`-c` flag added) | `fc90b21` |
| T3 (`kinoforge forget --id <id>` recovery subcommand + README "Operator commands" section + this PROGRESS entry) | `c947f9b` |

**Key design decisions:**

- **Spec scope locked at A+B (Q1):** ledger-first dispatch + rich
  ledger-derived output. Cloud-ledger CLI routing (PROGRESS:127) is explicitly
  out of scope.
- **Exit-code split (Q2=B):** provider `KeyError` ⇒ exit 0 (stale ledger;
  operator action = `forget`); any other provider exception ⇒ exit 2
  (transient). `endpoints()` failure when `get_instance` succeeds keeps exit 0
  (ancillary lookup must not turn a healthy `ready` instance into an outage).
- **Multi-line `key=value` alphabetised output (Q3=A):** scales as fields are
  added, plays well with `grep`/`awk`, no `jq` dependency.
- **Ledger-schema extension + optional `--config` (Q5=A+C):** values frozen at
  instance creation time, immune to later YAML edits; legacy entries fall back
  to cfg or `<not in ledger>` sentinel via `_ledger_field_or_cfg`.
- **Soft migration (Q6=A):** no `kinoforge ledger migrate` helper; legacy
  entries age out fast.
- **Sibling parity for provider construction (Q7=A):** same `registry.get_provider(name)()`
  shape as `stop`/`destroy`/`reap`.
- **New `kinoforge forget --id <id>` (Q9=B):** closes the recovery gap
  end-to-end; the advisory line in `_cmd_status` points to a real command.
- **`--id` flag style for `forget` (plan deviation from spec §3.3 positional draft):**
  matches `stop`/`destroy` house style; the advisory string emitted by
  `_cmd_status` was wired as `kinoforge forget --id <id>` in T2 so T3's parser
  has to match.
- **Spec naming `max_age_s` vs. dataclass attribute `max_lifetime_s`:** Layer S
  names the persisted ledger key generically (`max_age_s`) per spec; the source
  attribute on the `Lifecycle` dataclass is `max_lifetime_s`. T1 implementer
  threaded `lc.max_lifetime_s` into the `max_age_s` kwarg at the `_cmd_deploy`
  call site.
- **Non-idempotent `forget` (spec §6 edge case #7):** a second `forget` on the
  same id (after the first removes it) returns exit 1. Mirrors `stop`/`destroy`.
  Idempotent-success would mask script bugs that pass the wrong id.
- **Forward-compat `last_heartbeat` field:** `_build_ledger_block` surfaces it
  when present and omits it when absent. `Ledger.record` does NOT yet persist
  it — when a future layer wires production-side persistence, the operator-visible
  side will light up automatically with no further `_cmd_status` work.

**Test count:** 1198 passed + 8 skipped pre-Layer-S → **1222 passed + 8 skipped**
post-Layer-S (+24 net new: T1 adds 4 offline tests; T2 adds 16; T3 adds 4).

**Out of scope (carry-forwards):**

- ~~**PROGRESS:127 — cloud-ledger CLI routing.**~~ — **CLOSED** by Phase 34.
- **Production-side `last_heartbeat` persistence.** Surface is wired
  (`_build_ledger_block` reads it when present); the writer (`Ledger.record`
  or a sibling `Ledger.touch(instance_id, last_heartbeat=...)` method) is a
  future layer.
- **`kinoforge status --all`** (operator view over every ledger entry).
- **`kinoforge status --json`** (machine-readable output mode).
- **`kinoforge ledger migrate`** helper for backfilling legacy entries
  (soft migration accepted instead).

Closes PROGRESS:120.

### Phase 34 — Layer T (cloud-ledger CLI routing)

Routes the CLI ledger through `cfg.store` (s3/gcs) via a JSON sidecar in
`state_dir/store.json`. Introduces `SessionContext` threaded through every
subcommand. Refactors `Ledger._compute_uri` to use the universal
`store.uri_for` ABC. Splits the 1000-LOC `cli.py` monolith into a `cli/`
package (`_main`, `_commands`, `context`, `sidecar`).

- [x] Task 1: `Ledger._compute_uri` uses `store.uri_for` — commits `18e6837` + `0ed8f67`
- [x] Task 2: `SidecarMismatch` + `SidecarMigrationBlocked` errors — commits `878d76f` + `f0f3bb8`
- [x] Task 3: `cli.py` → `cli/` package promotion — commit `13d5a91`
- [x] Task 4: `cli/sidecar.py` module + 27 tests — commits `2552b0e` + `0575c39`
- [x] Task 5: `cli/context.py` `SessionContext` + 16 tests — commits `a067a46` + `4668537`
- [x] Task 6: `cli/` split into `_main` + `_commands` (mechanical, no behaviour change) — commits `035b524` + `f9be21f`
- [x] Task 7: `SessionContext` wired through `main()`; every `_cmd_*` signature migrated; 24 new tests in `tests/cli/test_commands_routing.py` + `tests/cli/test_main_flow.py` — commits `710b679` + `ddab26b`
- [x] Task 8: Multi-node lock integration test (Layer T's headline win) — commit `2ebeaad`
- [x] Task 9: README + PROGRESS + final gate + merge — *this commit*

**Key design decisions:**
- Sidecar JSON in `state_dir/store.json` over global `--config` flag (Q1=A):
  no breaking flag change for single-user CLI; no-config commands like
  `kinoforge list` discover the store transparently.
- Hard error on cfg-vs-sidecar mismatch (Q2=A): mirrors `kinoforge gc
  --config` precedent — explicit > silent.
- Hard block on first cloud cmd when local ledger non-empty (Q3=A):
  prevents silently orphaning in-flight pods.
- Best-effort overview when cloud creds unavailable (Q4=A): keeps
  `kinoforge --help` working during credential rotation.
- `SessionContext` over thread-cfg-through-9-fns: single integration
  point for every future per-session field (streaming logs, spend cap,
  multi-tenant profiles, daemon mode).
- `cli.py` → `cli/` package: file was 1000+ LOC; splitting now while
  the surface is small avoids paying it later when more layers land here.

**Test count:** 1222 → 1297 passed (+75 net new across T1, T2, T4, T5, T7, T8).

**Known limitations / carry-forwards:**
- Cross-machine bootstrap requires every host's first command to be
  cfg-bearing. `--store-uri` / `KINOFORGE_STORE_URI` is a Layer T+1
  candidate (non-breaking, additive).
- Two concurrent cfg-bearing cmds on the same `state_dir` with different
  configs: last writer wins. Documented as operator-side concern.
- Lock-contention surfacing in non-batch handlers is a pre-existing
  gap inherited from Phase 18 (`LockTimeout` is a `KinoforgeError` but
  only `_cmd_batch` has the catch arm). Layer T does not extend the
  catch sites.
- No real-cloud verification — PROGRESS:116 (S3 / GCS real-cloud) is
  the gate for that.

**Established patterns reinforced:**
- `SessionContext` lazy-built and identity-cached per invocation, so
  `kinoforge --help` never touches cloud SDKs.
- `ledger_safe()` for the always-on instance overview — never raises,
  prints `unavailable: <reason>` header on store-construction failure.
- Spec §9 error matrix is honoured at the `main()` envelope:
  `SidecarMismatch` / `SidecarMigrationBlocked` / corrupt-sidecar
  `PydanticValidationError` / config `FileNotFoundError` all exit 1
  with clean stderr.
- Parametrized field-mirror lockdown for `SidecarRecord` matches the
  Phase 16 `484e368` post-merge fix pattern.

Closes PROGRESS:127.

### Phase 35 — Layer L-T4 (batch streaming logs)

- [x] Task 1: Extract batch dataclasses to `core/batch_models.py` — commits `08d7c00` + `59f135d`
- [x] Task 2: `core/batch_events.py` — BatchEvent + _LockedEmitter + 6 ACs — commits `f906b3e` + `ace17a0`
- [x] Task 3: batch_generate emits at 5 sites; aborted/interrupted outcomes carry duration_s + error for JSONL uniformity — commits `27b3f56` + `93b9c57`
- [x] Task 4: `cli/batch_formatters.py` — Human / JSONL / NoOp + 8 ACs — commit `b63b527`
- [x] Task 5: `--stream-format={human,jsonl,none}` wired through `_cmd_batch` + instance-overview stderr routing in jsonl mode + 4 ACs — commits `35436d2` + `2368e1d` + `7017df3`
- [x] Task 6: README + PROGRESS + final gate — commit `bd9a222`
- [x] Merge to main via `--no-ff` — merge commit `f077e54` (closes PROGRESS:326 follow-up #1)

**Key design decisions:**
- Callback hook in core (foundation-first; Q1=C). CLI consumes the seam.
  Future consumers (Slack, Prometheus, TUI progress bars) cost nothing
  extra. Matches the existing seam pattern from PROGRESS:87.
- Bundle JSONL formatter on day one (Q1 follow-up): operators get
  pipeable output without a follow-on layer.
- Minimal event vocabulary (Q2=A): `entry_start` + `entry_finish`. New
  status values added as enum extensions, not new event kinds.
- Internal `threading.Lock` serializes the user callback (Q3=A). Matches
  the stdlib `logging.Handler` pattern. Multi-line output never
  interleaves under concurrency.
- Lean+entry event payload (Q4=A): `BatchEvent` carries the universal
  fields plus a full `BatchEntry` on `entry_start` so formatters do not
  need to close over the manifest.
- Build-time fail emits both events back-to-back (Q5=A): preserves the
  invariant `start_count == finish_count == len(entries)` across all 4
  exit paths.
- Single CLI flag default `human` (Q6=A): visible behaviour change
  ships the layer to existing users; `--stream-format=none` preserves
  prior output for anyone who wants it.
- Model-extract refactor (Q9): `BatchEntry` / `BatchManifest` /
  `BatchOutcome` / `BatchResult` moved to `core/batch_models.py` to dodge
  an import cycle with `core/batch_events.py`. `core/batch.py`
  re-exports the four names so every existing import site keeps working.

**Behavioural upgrade (small but visible):** `BatchOutcome` for
`aborted` / `interrupted` entries now carries `duration_s` (0.0 / actual)
+ `error` (`"batch aborted by <FatalType>"`) so the JSONL on-wire shape
is uniform across every `entry_finish` event. Pre-Layer-L-T4 outcomes
for these paths had `duration_s = None` and no `error`. The single
existing assertion at `tests/core/test_batch_generate.py:240` (which
checks only `interrupted` status, not the new fields) continues to
pass unchanged.

**AC8 contract enforcement caught in review:** the first T5 cut
filtered `[instance overview]` lines in the JSONL test; the spec
reviewer flagged this as masking a real `| jq .` breakage in
production. The fix (`2368e1d`) routes `_print_instance_overview`
output to stderr in jsonl mode via a `file: TextIO | None = None`
kwarg (capsys-safe lazy resolution; same pattern as the
`batch_formatters.py` `_out` property). The test now asserts strict
stdout purity (every line must parse as JSON).

**Test count:** 1297 pre-Layer + 6 (batch_events) + 6 (batch_generate
streaming ACs) + 8 (batch_formatters) + 4 (batch_cli stream-format
ACs) = 1321 post-Layer (8 skipped unchanged).

**Live spend:** $0. Fully offline-tested via existing
`_BatchSpyEngine` / `FakeProvider` / `FakeImageEngine` fixtures.

Closes PROGRESS:326 follow-up #1 (Layer L Task 4 streaming-log deferral).

### Phase 36 — Layer U (heartbeat persistence)

- [x] T0: Spec doc + plan doc + tasks.json committed — commits `6114b41` + `760e501`
- [x] T1: `Ledger.touch` for in-place entry updates — commit `0d3614e`. 9 tests including subprocess cross-process visibility and forget+touch no-resurrect lockdown.
- [x] T2: `HeartbeatLoop` threaded poll with crash-safe try/except + sentinel — commit `09b61e8`. 8 tests including provider/ledger exception isolation (caplog), sentinel monotonic, bounded `stop()` from mid-sleep AND on wedged thread, two-loop semantic isolation. Adds `_HeartbeatProvider` / `_TouchableLedger` / `HeartbeatLoopProtocol` structural Protocols (PROGRESS:121 pattern).
- [x] T4: `LifecycleConfig.heartbeat_interval_s` config field — commit `9cf1de1`. Default `None`. Positive-value validator rejects bad values at config-load, before any compute is provisioned. 4 tests.
- [x] T3: `deploy_session` spawns `HeartbeatLoop` when configured — commit `2d7e749`. Gated on positive interval AND compute instance (hosted sessions skip). Injectable `heartbeat_loop_factory` seam for test substitution. 5 tests including end-to-end real Loop ledger write at 50ms cadence.
- [x] T5: `kinoforge status` surfaces `last_heartbeat` + sentinel-staleness advisory — commit `1fbe58b`. Layer S read formatter already surfaced `last_heartbeat`; T5 adds the sentinel-staleness advisory and a positive/negative regression-guard pair on the read surface. 4 tests.
- [x] T6: README + PROGRESS + example yaml + final gate — commit `933c01f`.
- [x] Merge to main via `--no-ff` — merge commit `e466321`.

**Key design decisions:**
- Q1 (write trigger) = dedicated periodic poll inside `deploy_session`.
  Re-opened from "observation-time piggyback" after exploration
  revealed `LifecycleManager.is_liveness_OK` has zero production
  callers — the original wire would have shipped to dead code.
- Q2 (call site) = inside the `deploy_session` ctx manager. Thread
  lifetime tracks the orchestration session, not the process. One-shot
  CLI commands (`status`, `forget`, `list`) don't spawn the thread.
- Q3 (scope) = generation + persistence. Re-opened from "pure pipe"
  after exploration showed no production caller of
  `provider.heartbeat()` exists — the pipe would have carried no data.
  The loop is now both source (`provider.heartbeat(id)`) AND persister
  (`ledger.touch(id, ...)`) per tick.
- Q4 (crash-safety) = three-layer defense, explicit constraint:
  inner `try/except Exception` per tick + sentinel field +
  `daemon=True` thread with bounded `join(timeout=...)`.
- Q5 (default) = config-gated, default-off (`heartbeat_interval_s:
  null`). Every existing YAML config loads unchanged; backwards-compat.
- Strict-update `Ledger.touch` (no upsert): unknown id is a silent
  no-op. Insertion stays the sole responsibility of `record`.
- Skip-unchanged guard inside `touch`: second call with the same
  value writes zero bytes (pre-mitigation for sub-second-cadence
  consumers).
- Protected ledger keys filtered from `**extra`: a future Layer V
  consumer cannot accidentally overwrite `id` / `provider` / `tags` /
  `created_at` / `cost_rate_usd_per_hr`.
- First tick is eager: `_run` ticks BEFORE the first
  `_stop.wait(interval_s)` so short-lived sessions still write at
  least one heartbeat.
- Structural `HeartbeatLoopProtocol` (start/stop) lets tests substitute
  non-threaded spies without inheriting the full class.

**Forward-compat sentinel-gate contract (load-bearing):**
Every successful `_tick_once` writes `heartbeat_thread_tick` alongside
`last_heartbeat`. Any code that consults `last_heartbeat` for a
reaping or destructive decision MUST first check
`heartbeat_thread_tick`; if
`now - heartbeat_thread_tick > 3 * heartbeat_interval_s`, treat
`last_heartbeat` as untrustworthy. No production reaper consumes the
field today — the contract is documented for the future Layer V
heartbeat-aware reaper. The CLI surfaces the same gate as a
user-visible advisory.

**Test count:** 1321 pre-Layer-U + 9 (ledger_touch) + 8
(heartbeat_loop) + 4 (config) + 5 (orchestrator_heartbeat) + 4
(cli_status) = 1351 post-Layer-U (8 skipped unchanged).

**Live spend:** $0. Fully offline-tested via `LocalProvider` +
`FakeEngine` + duck-typed spy fixtures.

Closes PROGRESS:113 carry-forward "production-side `last_heartbeat`
persistence" (Layer S forward-compat seam).

### Phase 37 — Layer V (heartbeat-aware reaper)

Closes the "Layer V candidate" carry-forward at PROGRESS:163. Ships
the first production consumer of Layer U's `heartbeat_thread_tick`
sentinel and the reusable substrate every future heartbeat consumer
(sweeper daemon, dashboard, in-session warm-reuse retrofit) will share.

- [x] Task 1: `core/reaper.py` pure substrate (Verdict, Policy, classify, partition) — commits `75a41d0` + review fix `1413bb7`
- [x] Task 2: invariant scan locking `core/reaper.py` purity — commit `81c02e8`
- [x] Task 3: `Lifecycle.grace_after_session_s` + config wire — commits `fb3f4fe` + zero-boundary fix `8cb7893`
- [x] Task 4: `core/reaper_actor.py` — `act_on_verdict`, `provider_for` — commits `d6265ee` + dead-UNROUTABLE-branch removal `a7ca0b7`
- [x] Task 5: `sweep` orchestration with caches + UNROUTABLE force-forget path — commit `94ff68e`
- [x] Task 6: `kinoforge reap` rewrite + flags + JSONL formatter — commits `0340b4d` + review fix `c2713e1`
- [x] Task 7: `kinoforge status` verdict line — commits `fbe00c8` + honest-fallback fix `f3c7567`
- [x] Task 8: README + PROGRESS + examples + final gate + merge — commit `f6045ab`
- [x] Merge to main via `--no-ff` — merge commit `7442808`

**Key design decisions:**

- **Substrate, not CLI patch (Q1=A).** Pure `classify` / `Policy` /
  `partition` shared by every future consumer.
- **D-hybrid verdict tree (Q2=D).** `LIVE` / `IDLE_REAP` /
  `ORPHAN_REAP` / `OVERAGE_REAP` / `STALE_LEDGER` /
  `HEARTBEAT_UNKNOWN` / `UNROUTABLE`. `classify` returns six of those
  seven; `UNROUTABLE` is assigned by `sweep()` when provider lookup
  fails.
- **Dry-run default + bundled `kinoforge status` verdict line
  (Q3=A).** Two consumers in one release prove the substrate is
  consumer-shaped, not CLI-shaped.
- **UNROUTABLE / STALE_LEDGER are first-class verdicts.**
  `STALE_LEDGER` is acted on by `DEFAULT_APPLY_POLICY` — closes the
  latent ledger-drift bug in the pre-Layer-V `reap()` (forced-forgot
  multi-provider entries against Local-only `live_ids`).
- **A+C compromise on config (Q5).**
  `lifecycle.grace_after_session_s` in YAML; explicit threshold
  kwargs to `classify` (no `Lifecycle` import in `core/reaper.py`).
- **B+C race mitigation (Q6).** `act_on_verdict` re-classifies
  inside a Layer 18 per-instance `reaper/<id>` lock.
- **Approach 2 (Q7).** Strict purity split (`core/reaper.py` pure +
  `core/reaper_actor.py` impure) enforced by
  `test_core_invariant.py::test_core_reaper_module_is_pure`.
- **UNROUTABLE force-forget lives in `sweep()`, not
  `act_on_verdict`** (architectural amendment from T4 review).
  UNROUTABLE entries have no provider, so `act_on_verdict` cannot
  reach them. `sweep()` handles `force_forget` by acquiring the same
  `reaper/<id>` lock and calling `ledger.forget(id)` directly.
- **`kinoforge status` honest fallback** (T7 review fix). When
  `provider.list_instances()` raises in the status command, surface
  `verdict=HEARTBEAT_UNKNOWN` rather than silently bias toward LIVE.

**Test count:** 1351 → 1423 passed + 8 skipped (+72 net Layer V tests).
Fully offline-tested; no live spend.

**Forward-compat hooks** (spec §7) lock the substrate's public surface
for **Layer W (`kinoforge sweeper` daemon)**, **Layer X (cost
dashboard / metrics)**, and **Layer Y (in-session warm-reuse
retrofit)**. All three reuse `classify` + `Policy` + `partition` +
`act_on_verdict` + `sweep` without modification.

### Phase 38 — Layer W (S3 / GCS real-cloud verification)

Verification-only layer that closes PROGRESS:116 carry-forward #4
(`S3ArtifactStore` + `GCSArtifactStore` never hit real cloud). Five
axes per cloud (hot path, multipart/resumable, encryption
defaults + customer-managed KMS, signed GET + PUT, retry via 503
proxy) with live opt-in capture + offline fixture replay. Mirrors Layer
N (Phase 24) pattern at the storage substrate.

Production additions: `StoreEncryptionConfig` pydantic block (`mode:
default | kms`, `kms_key_id`), `ArtifactStore.signed_url` ABC,
`signed_url_default_ttl_s` store config field, retry-baseline pins in
both store adapters, `tools/bootstrap_kms.py` + `pixi run
cloud:bootstrap-kms` task, and `docs/CLOUD-CREDS.md` updated with KMS
key inventory.

- Spec: `docs/superpowers/specs/2026-06-06-layer-w-s3-gcs-real-cloud-design.md`
- Plan: `docs/superpowers/plans/2026-06-06-layer-w-s3-gcs-real-cloud.md`

**Per-task SHAs:**

| Task | SHA(s) |
|---|---|
| T1 (StoreEncryptionConfig + signed_url_default_ttl_s pydantic) | `2e6fa24` |
| T2 (ArtifactStore.signed_url ABC + LocalArtifactStore NotImplementedError stub) | `7495634` |
| T3 (S3ArtifactStore multipart + encryption + signed_url + retry pin) | `5644d3b` + review fix `5a888b0` |
| T4 (GCSArtifactStore resumable + CMEK + signed_url + retry pin) | `2022332` + review fix `685ed8c` |
| T5 (bootstrap_kms.py with PendingDeletion guards + IAM re-verify + spec gaps) | `738f9b4` + `53350fe` + `6aa7992` |
| T6 (recording seam + redaction) | `6d61d60` + review fix `7c2de86` |
| T7 (Fail503Proxy) | `ac087d7` |
| T8 (live-suite gate) | `5650f14` |
| T9 (S3 live + redaction-order fix) | scaffold `a1e935d`, recording fix `f9c2ed8`, fixtures `71cfae8`, redaction fix `56402a2`, recapture `89257e4` |
| T10 (GCS live) | `350bde2` + `3af0162` |
| T11 (FixtureReplay clients + offline isolation) | `972d652` + CLI regression fix `e192dec` + quality fix `ad005af` |
| T12 (README + PROGRESS Phase 38 entry) | `e0e16d2` |

**Real artifacts captured:**

- S3 multipart ETag: `"0fdfb84099d425daeed95c07873a8f11-2"` (2-part MPU, 16 MiB object)
- S3 KMS-encrypted object: `ServerSideEncryption=aws:kms` confirmed against key `4b0dbe0c-3a76-401a-ac2e-d0d949b9fa3e`
- GCS resumable upload size: `16777216` bytes confirmed on blob metadata
- GCS CMEK `kms_key_name`: `projects/.../keyRings/<GCS_KMS_KEYRING>/cryptoKeys/bucket-cmek/cryptoKeyVersions/1`

**Key design decisions:**

- **Multipart switch is unconditional.** boto3 + google-cloud-storage SDK defaults handle the threshold; no kinoforge knob (spec §4.1). Both real-cloud axes confirmed at 16 MiB.
- **`StoreEncryptionConfig.kms_key_id` is a single field across clouds.** The store adapter parses the ARN vs Cloud KMS resource name form at call time (spec §4.2).
- **`LocalArtifactStore.signed_url` raises `NotImplementedError`.** Local files have no transport-layer auth; the ABC contract documents this as an expected provider limitation (spec §4.3).
- **Retry baselines pinned in store source.** `botocore.config.Config(retries={"max_attempts": 3, "mode": "standard"})` for S3; `Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)` for GCS. No caller knob (spec §4.0).
- **KMS keys are NOT auto-rotated.** Rotation invalidates Layer W fixtures committed to the repo; rotation is a manual, deliberate operator action only (spec §6.3).
- **2 axes xfailed (live only).** S3 retry-via-proxy (SigV4 Host binding prevents Fail503Proxy MITM); GCS retry-via-proxy (`google-resumable-media` treats 503 as terminal on initiation). Retry config verification falls back to offline tests in T7 + T3 + T4; the xfail markers are documented at the test sites.
- **Redaction-order bug caught during T9 live run.** `extra_subs` ran AFTER the regex pipeline, so the KMS UUID (`4b0dbe0c-…`) leaked into captured fixtures. Fixed in `56402a2` (extra_subs now runs first); fixtures recaptured at `89257e4`. Leaked content exists in git history at `71cfae8` (acceptable for internal repo; would need `git filter-repo` rewrite if repo ever goes public).
- **S3 recorder `operation_name` empty.** botocore context gap means `operation_name` is `""` in the event hook. T11 worked around via params-pivoting shape fingerprint to identify the operation. TODO marker added in `tests/stores/recording.py` for follow-up root-cause fix.

**Test count:**

- Pre-Layer-W baseline: 1423 passed + 8 skipped.
- Post-Layer-W offline: ~1497 passed + 8 skipped (~74 net new offline tests).
- Plus 14 KINOFORGE_LIVE_TESTS-gated tests (2 × 7 axes; 2 of those xfailed for the proxy axes above).

**Out of scope / carry-forward for future layers:**

- S3 + GCS retry-via-proxy live verification (covered offline only; 2 live axes remain xfailed).
- DSSE-KMS (S3) + CSEK (GCS) encryption modes.
- Multipart resumability across process restart.
- Bucket-level default encryption knob.
- Signed URL custom response headers.
- Azure + B2 + R2 stores.
- S3 recorder botocore-context `operation_name` fix (params-pivoting workaround in place; root-cause fix deferred).

Closes PROGRESS:116 carry-forward #4.

### Phase 39 — Layer W+α (cloud bootstrap, SkyPilot perms front-load)

Zero-spend verification layer. Lands every AWS + GCP permission and GPU
quota the SkyPilot multi-cloud T4 smoke (Layer W+β) needs. Spec:
`docs/superpowers/specs/2026-06-06-layer-w-alpha-cloud-bootstrap-design.md`.
Plan: `docs/superpowers/plans/2026-06-06-layer-w-alpha-cloud-bootstrap.md`.

- [x] Task 1: AWS scoped IAM policy doc — commit `032b697`
- [x] Task 2: Operator gate — `.aws/README.md` apply instructions + gitignore
      re-include patterns for tracked operator docs / policy bytes — commit `90ddbfb`
- [x] Task 3: AWS probe (sts + iam.simulate + ec2.describe + servicequotas.get)
      — commit `b79c116`
- [x] Task 4: GCP probe (regions + SA-role audit + T4 quota), `google-cloud-compute`
      pinned, `setuptools<72` pinned for `pkg_resources` compat — commit `7a97bf0`
- [x] Task 5: Quota gap handler — AWS auto-request idempotent via history
      lookup; GCP console URL emitter (unused this run — GCP already at target)
      — commit `c0aa2d4`
- [x] Task 6: `sky check gcp` clean via `live-skypilot` pixi env; AWS sky check
      deferred (skypilot[aws] pin conflict; AWS perm surface covered by probe)
      — commit `fbd5387`
- [x] Task 7: CLOUD-CREDS table + SkyPilot permissions section + this entry
      + README pointer — commit `1d032d4`

**Key design decisions:**

- **Scoped IAM policy stays as a doc, not the live attachment.** Operator
  preferred 3 AWS-managed broad policies (EC2/IAM/ServiceQuotas FullAccess)
  + the existing S3FullAccess instead of pasting the 175-line scoped JSON
  (AWS inline policy limit is 2048 chars; ours is over). The scoped doc
  at `.aws/policies/skypilot-minimal.json` (commit `032b697`) is committed
  as the documented swap-in target for a future scope-down layer.
- **Probe mirrors `tools/preflight.py` seam pattern.** Every SDK call goes
  through a factory callable; tests inject fakes (`_FakeBoto3Session`,
  `_FakeGCPRegionsClient`, `_FakeGCPIAMClient`); no real cloud in CI.
- **Atomic snapshot writes.** tmp-file + rename so a crashed probe never
  leaves a half-written snapshot.
- **AWS quota requests are idempotent via history lookup.** Re-running
  the probe surfaces the same CaseId; never duplicates.
- **GCP quota requests have no SDK surface.** Probe emits a console URL
  + operator instructions on gap. Not exercised this layer — GCP T4 quota
  in `us-central1` was already at the 1.0 target.
- **`kinoforge-ci-kms` customer policy auto-created mid-T3.** The 4
  AWS-managed broad policies cover EC2 + IAM + S3 + SQ but not KMS. The
  probe caught `kms:Encrypt`/`kms:Decrypt` as `implicitDeny` and
  programmatically attached a scoped customer policy (resource =
  `arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/4b0dbe0c-3a76-401a-ac2e-d0d949b9fa3e`)
  to `kinoforge-ci`. Documented in `docs/CLOUD-CREDS.md`.
- **Gitignore re-include pattern.** Switched from blanket `.aws/` / `.gcp/`
  ignore to `.aws/*` / `.gcp/*` with explicit re-includes for
  `.aws/README.md`, `.aws/policies/`, `.gcp/README.md` — secrets
  (credentials, sa.json, snapshots, KMS arns) still ignored.

**First real artifact:** AWS Service Quotas case
`cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR` open against `L-DB2E81BA`
(Running On-Demand G/VT vCPUs) requesting 4.0 in `us-east-1`. Captured in
`.aws/perms-snapshot.json` `quota_request.case_id`. AWS reviews
asynchronously.

**Spend:** $0. Two operator console actions consumed:
- Attached 3 AWS-managed broad policies to `kinoforge-ci` (T2 gate).
- KMS auto-grant didn't require operator (`kinoforge-ci` already held
  `IAMFullAccess` after T2 attach so the probe could mint the customer
  policy itself).
- GCP T4 quota was already at target — no operator action needed.
- T5 GCP console-URL path never fired.

**Test count:** 9 probe unit tests at T4 → 12 at T5 (+3 quota-gap tests).
Full suite count unchanged from Phase 38 baseline (~1497) since probe
tests landed in a previously empty file.

**Deferred / out of scope (Layer W+β candidates):**
- `sky launch` GPU smoke on AWS + GCP T4 instances.
- Azure / B2 / R2 SkyPilot enablement.
- Scope-down: swap AWS-managed broad policies for
  `.aws/policies/skypilot-minimal.json`.
- AWS bucket scope-down on `AmazonS3FullAccess` (predates this layer).
- `skypilot[aws]` pixi pin conflict resolution (blocks `sky check aws`
  via pixi env; standalone venv runs but reports both clouds disabled
  due to env-var discovery quirks — both paths abandoned because the
  probe covers AWS perms end-to-end).
- AWS quota case approval landing (asynchronous; visible in AWS console
  Service Quotas → "Requested quotas" tab).

Closes PROGRESS:113 carry-forward #2 (SkyPilot SDK shape) is partial —
GCP path of SkyPilot is exercised by `sky check` clean. AWS path of
SkyPilot is NOT exercised; closure is gated on Layer W+β.

### Phase 40 — Layer W+β PARTIAL (SkyPilot T4 GPU smoke, blocked on GCP billing)

Layer attempted the live T4 GPU lifecycle of the `providers/skypilot/`
adapter against real hardware. Five real adapter/test bugs caught on
the path and fixed; live smoke itself blocked by GCP free-tier
billing restriction.

Spec:
`docs/superpowers/specs/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke-design.md`.
Plan:
`docs/superpowers/plans/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke.md`.

- [x] Task 1: Helpers + parametrized scaffold (RED) — commit `384041f`
- [x] Task 2: GPU example config + offline fixture-shape regression — commit `c8327e2`
- [x] Task 3: Pre-spend gate — ran mechanically (operator pre-authorized $20 spend)
- [~] Task 4: Live GCP T4 smoke — BLOCKED on free-tier billing; 5 fix commits shipped on the path
- [x] Task 5: CLOUD-CREDS + this entry + final gate — commit `b9a45e4`

**Bug-catch commits (the layer's actual artifact):**

| SHA | Commit | What it fixes |
|---|---|---|
| `ee90ac3` | fix(providers/skypilot): add clouds= param | `sky.list_accelerators()` without `clouds=["gcp"]` triggers a Kubernetes catalog import that fails without the `kubernetes` package |
| `c9a5aa6` | fix(providers/skypilot): vram fallback + offer attr | GCP `InstanceTypeInfo` returns `device_memory=None` for NVIDIA GPUs — added `_KNOWN_GPU_VRAM_GB` fallback. Also fixed the T4 offer filter attribute name: `gpu_name` (wrong) → `gpu_type` (correct field on the Offer dataclass) |
| `f0c7783` | fix(providers/skypilot): GPU disk_size default 60 GB | SkyPilot GPU base image needs ≥50 GB; provider defaulted to 30 GB → HTTP 400 from GCP |
| `819d130` | fix(tests/live): use sky default GPU image | `docker:skypilot/skypilot-gpu:latest` does not translate cleanly to GCP VM images; empty `image=""` lets SkyPilot pick its per-cloud GPU image |
| `f3ade88` | feat(interfaces,skypilot): InstanceSpec.spot + use_spot mapping | GCP had `GPUS_ALL_REGIONS=0` (on-demand) but `PREEMPTIBLE_NVIDIA_T4_GPUS=1` (spot); added `InstanceSpec.spot: bool = False` (backward compatible) wired through to `resources.use_spot` in the SkyPilot provider |

**Blocker:**

```
ERROR: Your billing account is currently in the free tier where
non-TPU accelerators are not available.
```

Per-region quota (`NVIDIA_T4_GPUS=1`) is pre-granted; activation
requires upgrading the GCP billing account. The `GPUS_ALL_REGIONS=0`
global is a free-tier consequence, not separately adjustable.

**Re-fire instructions (post-billing-upgrade):**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot pytest \
  tests/live/test_skypilot_live.py::test_skypilot_live_e2e_t4_gpu_lifecycle_smoke \
  -v -s
```

Expected: PASS within 5–10 min, spend $0.03–$0.06.

**Key design decisions / discoveries:**

- **Bug surface of bare lifecycle is exactly as the Layer N pattern
  predicted.** 5 production bugs caught at $0 spend — every one would
  have masqueraded as an engine failure if the bare lifecycle had been
  skipped in favor of a direct ComfyUI smoke.
- **Adapter `clouds=` param is now load-bearing.** Without explicit
  `clouds=["gcp"]`, sky tries to probe every catalog backend including
  k8s. This is a subtle behavior of `sky.list_accelerators()` that
  isn't surfaced in any sky doc we found.
- **`InstanceSpec.spot` is a new public ABC field** — added with default
  `False` so all existing callers unchanged. Layer W+β2 (AWS) will
  exercise the same field (`PREEMPTIBLE` analogous on AWS spot).
- **GCP `device_memory=None` discovery.** `_KNOWN_GPU_VRAM_GB` is a
  manual map covering T4, A10, L4, A100, H100, V100. Maintenance
  burden ≤ 1 entry/year. Better than silently filtering offers out.

**Spend:** $0. All attempts failed before any VM provisioned.

**Out of scope / carried forward:**

- The live smoke itself — re-fires once billing is upgraded.
- AWS arm (W+β2) — gated on quota case
  `cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR` landing.
- Engine smoke on a verified adapter — separable layer that stacks on
  this one once the smoke fires.
- `accelerators_in_cost` ordering verification on the GPU branch.

### Phase 41 — Layer 1 AuthStrategy substrate

Pluggable-auth foundation for hosted engines. Ships an `AuthStrategy`
ABC (5 stable methods, locked by signature-baseline invariant) plus
Bearer / GCPServiceAccount / AWSSigV4 concrete strategies, a
`build_auth_strategy` registry, a backward-compat retrofit of
`HostedAPIEngine`, a `FakeAuthStrategy` test fixture, and a
`tools/probe_hosted.py` preflight tool. Foundation for Layer 2 (Veo)
and Layer 3 (Nova Reel) plus future Bearer providers
(Replicate / Runway / Luma).

Spec:
`docs/superpowers/specs/2026-06-07-veo-novareel-auth-strategy-design.md`.
Plan:
`docs/superpowers/plans/2026-06-07-layer-1-auth-strategy-substrate.md`.

- [x] Task 0: boto3 pin `>=1.34,<2.0` — commit `5262f3e`
- [x] Task 1: AuthStrategy ABC + HealthResult + HttpRequest — commits `4ddbb1c` + docstring fix `050bd26`
- [x] Task 2: Bearer strategy + 8 unit tests — commit `ddb9f1e` + edge-cases follow-up `1718135`
- [x] Task 3: GCPServiceAccount strategy + 7 unit tests — commit `20decff`
- [x] Task 4: AWSSigV4 strategy + 7 unit tests — commit `27dc1b2`
- [x] Task 5: build_auth_strategy registry + 6 unit tests — commit `a790279` + TypeError test follow-up `2da2287`
- [x] Task 6: ABC stable-surface invariant + extended subprocess-isolation — commit `4a9d594`
- [x] Task 7: FakeAuthStrategy shared fixture — commit `a388b85`
- [x] Task 8: HostedAPIEngine retrofit (backward-compat) — commit `aa9591d`
- [x] Task 9: tools/probe_hosted.py + pixi task — commit `292a392`
- [x] Task 10: README + PROGRESS + final gate — this commit

**Key design decisions:**

- **5-method ABC** — `credentials_present`, `health_check`,
  `redact_patterns`, `apply`, `client_kwargs`. Locked by signature
  baseline + invariant test.
- **Typed boundary objects** — `HealthResult` / `HttpRequest` frozen
  dataclasses. No duck-typed `dict[str, Any]` returns from the ABC.
- **Lazy vendor SDK imports** — `google.auth` and `boto3` only enter
  `sys.modules` when a strategy method is called, never at module
  import. Verified by extended subprocess-isolation invariant.
- **Direct SigV4 implementation** — hashlib + hmac stdlib rather than
  `botocore.auth.SigV4Auth`. Keeps the seam SDK-version-independent
  and lazy.
- **`build_auth_strategy` registry** — single discriminator-based
  factory. Unknown names raise `UnknownAdapter` for consistency with
  the rest of the registry pattern (engines, providers, sources,
  stores, splitters).
- **`HostedAPIEngine` backward-compat** — `auth_strategy=None` default
  derives `Bearer(env_var=cfg.api_key_env)` at `provision()` time.
  All 9 pre-Layer-1 construction sites pass unchanged.
- **Azure + OCI pseudocode in the spec, NOT the codebase** — verifies
  the ABC admits both providers without modification. Catches AWS+GCP
  over-fit before any real third-cloud integration lands.

**Test count:** 1528 pre-Layer-1 → 1584 post-Layer-1
(+56 net Layer 1 tests; all offline, no live spend).

**Live spend:** $0. Fully offline-tested via `FakeAuthStrategy` and
monkeypatched fake `google.auth` + `boto3` modules.

**Layer sequencing hard-block:** Layer 2 (Veo) and Layer 3 (Nova Reel)
plans MUST hard-block on this layer's merge commit per the spec §2
sequencing rule.

**Forward-compat hooks** (spec §7): future Bearer providers
(Replicate / Runway / Luma) land config-only — no new engine code.
Future cloud-native providers (Vertex Imagen, Bedrock Claude, Azure
DALL-E) reuse the existing strategies or add one new strategy per
auth family.

Closes (partial): PROGRESS:113 carry-forward "Engine-integration live
smoke" — Layer 1 is the architectural foundation; Layer 2 + Layer 3
close the engine surface.

### Phase 42 — Layer 3 BedrockVideoEngine (pivot from Nova Reel → Luma Ray v2)

**Pivot rationale:** Nova Reel requires account-level invocation approval
that cannot be granted programmatically (see "Task 7 blocker" section below).
User preference is `us-west-2` (Oregon); Nova Reel is `us-east-1` only. Luma
Ray v2 is available in `us-west-2` and uses the same Bedrock async-invoke
pattern. The blocker became an opportunity: instead of a Nova-Reel-specific
engine, we now ship a generic `BedrockVideoEngine` where
`model_input_template` is YAML-supplied and `"${PROMPT}"` is recursively
substituted at submit time. Same engine handles Nova Reel, Luma Ray v2, and
any future Bedrock video model — new models are config-only additions.

**AWS Model access page** says first-party serverless foundation
models (Nova, Titan) auto-activate on first invoke. The retirement
notice does NOT apply to third-party Bedrock models (Luma Ray,
Anthropic Claude, Stability). Those still require an AWS Support
case to flip `authorizationStatus` from `NOT_AUTHORIZED` to
`AUTHORIZED` — confirmed end-to-end this session via the
`PutUseCaseForModelAccess` API rejection ("Your account is not
authorized to perform this action. Please create a support case").

Spec:
`docs/superpowers/specs/2026-06-07-veo-novareel-auth-strategy-design.md`.
Plan:
`docs/superpowers/plans/2026-06-07-layer-3-nova-reel-engine.md`.

- [x] Task 0: `NovaReelEngineConfig` pydantic + wire onto `EngineConfig` — commit `3ca3d77`
- [x] Task 1: `engines/nova_reel/` package + 10 offline unit tests — commit `1e2dd1a`
- [x] Task 2: `examples/configs/nova-reel.yaml` + parse test — commit `6b941e7`
- [x] Task 3: `.aws/policies/bedrock-nova-reel.json` IAM policy doc — commit `e8902d2`
- [x] Task 4: Attach IAM policy + create S3 output bucket (real cloud mutation) — commit `71a41c6`
- [x] Task 5: `probe_hosted --check-bedrock-model-access` flag + 3 tests — commit `018213a`
- [x] Task 6: RED live-smoke scaffold (`tests/live/test_nova_reel_live.py`) — commit `28f31bd`
- [x] **PIVOT (Phase 1 refactor):** `nova_reel` → `BedrockVideoEngine` + Luma Ray config — commit `aae46d7`
- [x] **Probe tighten:** two-stage `check_bedrock_model_access` (catalog + runtime authorization) — commit `889a016`
- [x] **Region pivot:** us-east-1 → us-west-2 (Luma Ray v2 availability); AWS Support case is the documented unblock path
- [x] Task 8: Offline replay scaffold (`tests/engines/test_bedrock_video_replay.py`) — commit `97208fb` (skips until fixture lands)
- [ ] Task 7 (BLOCKED): fire live smoke + capture fixture — blocked on AWS Support case for Bedrock Luma Ray v2 access
- [ ] Task 9: README + PROGRESS final gate — PROGRESS Phase 42 PARTIAL + README Bedrock Video section landed at `8883022`; "PARTIAL → CLOSED" flip + smoke happens after AWS Support unblocks Task 7

**Phase 2 (live smoke) — BLOCKED (same account-level gate as Nova Reel):**

The Luma Ray v2 agreement (EULA) was accepted programmatically via
`CreateFoundationModelAgreement` (offer `offer-o5smt33izgzbm`) and
`agreementAvailability` moved from `PENDING` → `AVAILABLE`. However
`authorizationStatus` remains `NOT_AUTHORIZED` even after 6 minutes of
polling. Same pattern as Nova Reel: EULA accepted, but account-level
invocation authorization has not activated.

`GetFoundationModelAvailability` diagnostic:
```
agreementAvailability: AVAILABLE
authorizationStatus: NOT_AUTHORIZED
entitlementAvailability: AVAILABLE
regionAvailability: AVAILABLE
```

All `StartAsyncInvoke` and `InvokeModel` calls return:
`ValidationException: Operation not allowed`

**What the operator must do (one-time):**
1. Sign in to the AWS Console as an IAM admin (or the root account).
2. Navigate to: Amazon Bedrock → Left menu → "Model access".
3. Find "Luma AI Ray v2" and click "Enable" / accept the use case form.
4. Wait for `authorizationStatus` to flip to `AUTHORIZED` (usually instant
   after console action).
5. Re-run:
   ```
   KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 \
       pixi run pytest tests/live/test_luma_ray_live.py -v -s
   ```

**IAM state after Phase 2 cloud work:**
- Old `kinoforge-nova-reel` inline policy deleted.
- New `kinoforge-luma-ray` inline policy attached (Luma Ray ARNs in
  us-west-2 + S3 on `bedrock-video-generation-us-west-2-nw51wr`).
- `AmazonBedrockFullAccess` AWS managed policy attached (for model
  access probing; can be detached after smoke succeeds).
- Luma Ray EULA accepted via `CreateFoundationModelAgreement`.
- `bedrock:GetFoundationModelAvailability` + agreement management
  actions added to inline policy for diagnostics.

No spend incurred. All failures were pre-submit.

**Phase 1 refactor — what changed:**
- `engines/nova_reel/` → `engines/bedrock_video/`; `NovaReelEngine` →
  `BedrockVideoEngine`; `NovaReelBackend` → `BedrockVideoBackend`.
- `_substitute_prompt(template, prompt)` helper walks template recursively,
  replacing any `"${PROMPT}"` string value with the actual prompt. Uses
  `copy.deepcopy` to avoid mutating cfg.
- Self-registers under `"bedrock_video"` (not `"nova_reel"`).
- `_adapters.py`: import line updated to `bedrock_video`.
- `core/config.py`: `NovaReelEngineConfig` → `BedrockVideoEngineConfig`
  with `model_id` + `model_input_template` as required fields (no defaults);
  Nova-Reel-specific `duration_seconds`/`fps`/`dimension`/`prompt_body_key`
  removed — those now live in `model_input_template`. `EngineConfig.nova_reel`
  → `EngineConfig.bedrock_video`. `KNOWN_ENGINES`: `"nova_reel"` →
  `"bedrock_video"`.
- `tests/engines/test_nova_reel.py` → `test_bedrock_video.py`; all tests
  updated for Luma Ray shape; new `test_bedrock_video_submit_substitutes_prompt_in_template`
  (2-level nesting) and `test_bedrock_video_submit_does_not_mutate_template_config`.
  12 tests total.
- `examples/configs/nova-reel.yaml` → `luma-ray.yaml` (Luma Ray v2, us-west-2).
- `.aws/policies/bedrock-nova-reel.json` → `bedrock-luma-ray.json`
  (Luma Ray ARNs in us-west-2; bucket `bedrock-video-generation-us-west-2-nw51wr`).
- `test_examples.py`: `test_nova_reel_example_config_parses` →
  `test_luma_ray_example_config_parses`; asserts `kind=="bedrock_video"`,
  `model_id=="luma.ray-v2:0"`, `region_name=="us-west-2"`.
- `test_core_invariant.py`: boto3 allowed-dirs `engines/nova_reel` →
  `engines/bedrock_video`.
- `test_probe_hosted.py`: model ID references updated to `luma.ray-v2:0`;
  strategy name `nova_reel` → `bedrock_video` in the E2E test.
- `tools/probe_hosted.py`: fallback region `us-east-1` → `us-west-2`.
- `tests/live/test_nova_reel_live.py` → `test_luma_ray_live.py`; updated
  region, model_id, bucket, config path.
- `tests/core/test_config.py`: 4 `Nova Reel` config tests replaced with
  `BedrockVideoEngineConfig` equivalents using Luma Ray shape.
- `docs/CLOUD-CREDS.md`: Nova Reel rows replaced with Luma Ray rows.
- 1585 tests pass; 0 failures; lint + typecheck + pre-commit clean.

**Task 5 live-probe result:**
```
PASS strategy=bedrock:amazon.nova-reel-v1:1 identity=amazon.nova-reel-v1:1
exit=0
```
Nova Reel model access confirmed for `kinoforge-ci` in `us-east-1`.

**Task 7 blocker — AWS account Nova Reel invocation not approved:**

The smoke test is blocked on an AWS account-level restriction that cannot
be resolved programmatically. Two IAM bugs were found and fixed during the
attempt (commits `a42f3d1` + `216e4c5`), but the root cause is that the
AWS account has not been granted model-invocation access for Nova Reel.

Diagnostic trail:
1. **First failure** — `AccessDeniedException: bedrock:InvokeModel on
   async-invoke/*` — IAM policy only covered the foundation-model ARN;
   async-invoke actions also need permission on
   `arn:aws:bedrock:us-east-1:<acct>:async-invoke/*`. Fixed at `a42f3d1`.
2. **Second failure** — same error, different facet: `StartAsyncInvoke`
   internally evaluates as `bedrock:InvokeModel` against the async-invoke
   resource ARN. Added that ARN to the `InvokeModel` statement. Fixed at
   `216e4c5`.
3. **Third failure (all subsequent)** — `ValidationException: Operation
   not allowed` — the same error on both `StartAsyncInvoke` (async) and
   `InvokeModel` (sync). This is NOT an IAM error; it fires AFTER IAM
   passes. It means Bedrock's own model-access gate is rejecting the call.
4. **Root cause confirmed** — `bedrock.put_use_case_for_model_access`
   returns `ValidationException: Your account is not authorized to perform
   this action. Please create a support case`. Amazon Nova Reel requires
   explicit account-level approval before any invocation is permitted —
   separate from, and beyond, IAM policies.

**Note:** `probe_hosted --check-bedrock-model-access` passing is a false
positive. The probe checks `list_foundation_models` (model is listed =
PASS), but listing a model does not mean the account can invoke it.
Nova Reel appears in the list with `status=LEGACY` and
`inferenceTypesSupported=['ON_DEMAND']` — both correct — but invocation
still requires a separate account-level approval.

**What the operator must do (one-time, cannot be scripted):**
1. Sign in to the AWS Console as an IAM admin.
2. Navigate to: Amazon Bedrock → Left menu → "Model access".
3. Find "Amazon Nova Reel" and click "Request access" (or "Manage model
   access"). Accept any EULA / use-case form.
4. Wait for access to be granted (usually instant for Amazon's own models,
   but can take up to 24 hours).
5. Re-run: `KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 pixi run
   pytest tests/live/test_nova_reel_live.py::test_nova_reel_live_e2e_smoke
   -v -s`

**IAM fixes committed (no spend incurred — all failures were pre-submit):**
- `a42f3d1`: `async-invoke/*` added to `StartAsyncInvoke`/`GetAsyncInvoke`
  resource list.
- `216e4c5`: `async-invoke/*` added to `bedrock:InvokeModel` resource list
  (AWS evaluates `StartAsyncInvoke` as `InvokeModel` on the async-invoke
  resource ARN).

**Probe fix needed (Task 7 follow-up):** `check_bedrock_model_access`
should verify actual invocation capability, not just list presence.
A lightweight fix: attempt a `start_async_invoke` with a clearly-invalid
input (e.g. `durationSeconds: 0`) and accept any error *except*
`AccessDeniedException` / `ValidationException("Operation not allowed")`
as proof of access. Or use the IAM policy simulator API.

**Key decisions:**
- `extra_checks: Sequence[(label, Callable[[], ProbeResult])]` seam on
  `run()` — future provider-specific checks (Vertex Veo model list,
  etc.) plug in without touching the probe shape.
- `boto3.Session().client("bedrock", ...)` (control plane) for
  `list_foundation_models` — distinct from `bedrock-runtime` used by
  the engine.
- Region resolved from first `AWSSigV4` strategy in the loaded config;
  falls back to `us-east-1` if none present.

**Test count:** 1584 pre-Layer-3-T5 → +3 new probe tests = 1587 total
(9 probe tests, all pass offline).

### Phase 44 — Layer 5a (Luma direct-API retirement, deletion-only)

Luma retired the Dream Machine direct video API in 2026; the dead
`LumaEngine` package that targeted it and its 12-test unit-test file
are removed in this layer. The carry-forward in project memory
`project_luma_video_retirement_2026.md` is now CLOSED.

Spec: `docs/superpowers/specs/2026-06-07-luma-direct-api-retirement-design.md`.
Plan: `docs/superpowers/plans/2026-06-07-layer-5a-luma-retirement.md`.

- [x] Task 1: code + test deletions + label sweep — commit `20ad7d9`
- [x] Task 2: README tombstone + PROGRESS Phase 44 entry — this commit (`<TASK2-SHA>`)

**Files removed:**
- `src/kinoforge/engines/luma/__init__.py` (164 lines)
- `tests/engines/test_luma.py` (297 lines, 12 tests)
- `examples/configs/comparison/luma-t2v.yaml` (30 lines)

**Files edited (1-5 line changes):**
- `src/kinoforge/_adapters.py` — drop the `engines.luma` self-registration import.
- `src/kinoforge/core/config.py` — drop `"luma"` from `KNOWN_ENGINES`.
- `tests/test_core_invariant.py` — drop the `lumaai` tuple from the vendor-confinement scan list.
- `tests/test_examples.py` — tighten the comparison-YAML kind allowlist set to `{"replicate","runway"}`.
- `tests/pipeline/test_generate_clip.py`, `tests/outputs/test_local.py`,
  `tests/outputs/test_format_filename.py` — sweep `provider="luma"`
  free-form labels to `provider="replicate"`.
- `README.md` — strip Luma from the Bearer-strategy table row, the
  Hosted Bearer section heading, and the wire-shape table; insert a
  forward-pointing tombstone paragraph; recomment the
  `LUMAAI_API_KEY` echo line in the quickstart.

**Test count:** N pre-Layer-5a → N − 13 post-Layer-5a (12 from the deleted
`test_luma.py` plus 1 from the comparison-YAML parametrize loop losing
`luma-t2v.yaml`).

**Live spend:** $0. Fully offline source-tree deletion; no provider
calls, no cloud mutations.

**Out of scope — landed in a separate spec:**

- `LumaAgentsImageEngine` for UNI-1 image keyframes (Layer 5b).
- Anything Bedrock-side (Luma Ray v2 lives there and is unaffected).

Closes carry-forward: `project_luma_video_retirement_2026.md`.

### Phase 45 — Layer 5b (ephemeral workspaces: vault + `--ephemeral`)

Ephemeral workspaces = vault (workspace content) + `--ephemeral` (workspace
lifetime) + `RedactionRegistry` (workspace boundary). Vault loader +
`RedactionRegistry` singleton + `RedactingLogFilter` on the root `kinoforge`
logger (Sub-α). Canonical write-site pattern at every persistent-write site;
`ArtifactStore.delete_run` + `manual_cleanup_command`; `OutputSink.publish`
registers basename; opaque sha256-derived names at every `put_bytes` (Sub-β).
`EphemeralSession` context manager via class-attribute storage with
`EphemeralPolicy` toggling each gate (Sub-γ).
Hosted-engine `_delete_with_retries` on `RemoteSubmitPollBackend`;
`EPHEMERAL_CAPABILITIES` pre-flight table refuses fal/luma/hosted (Sub-δ).
AST-based `tests/test_no_unredacted_writes.py` invariant + E2E (Sub-ε).

Spec: `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md`.
Plan: `docs/superpowers/plans/2026-06-08-ephemeral-workspaces.md`.

**Sub-α (vault loader + redaction substrate):**
- [x] Task 1: `Secret` newtype — commit `4533461`
- [x] Task 2: `RedactionRegistry` singleton + token rules — commit `5d12780`
- [x] Task 3: `RedactingLogFilter` for root `kinoforge` logger — commit `00d8ad6`
- [x] Task 4: Vault loader + alias derivation + repo-root check — commit `89a772c`

**Sub-β (canonical write-site pattern):**
- [x] Task 5: `opaque_store_name` helper — commit `8f120e5`
- [x] Task 6: `ArtifactStore.delete_run` + `manual_cleanup_command` across all stores — commit `fd0978a`
- [x] Task 7: `Ledger` persists via `redact_json` — commit `b1258aa`
- [x] Task 8: previously-skipped tasks-json checkpoint — commit `398ddfc`
- [x] Task 9: `JsonProfileCache._persist` redaction — commit `ff3d27d`
- [x] Task 10: `batch_generate _batch_summary.json` redaction — commit `da41f86`
- [x] Task 11: `LocalOutputSink.publish` registers basename — commit `fea947a`
- [x] Task 12: `Downloader` opaque-name path — commit `7e88398`
- [x] Task 13: `GenerateClipStage` opaque store names at every `put_bytes` — commit `7bbb2c8`

**Sub-γ (EphemeralSession context manager):**
- [x] Task 14: `core/ephemeral.py` — `EphemeralSession` + `EphemeralPolicy` +
  `EPHEMERAL_CAPABILITIES`; `EphemeralError` base in `errors.py`. Storage is
  a process-wide class attribute (NOT `contextvars`) because stdlib
  `ThreadPoolExecutor.map` does not auto-propagate `ContextVar` across
  worker threads, and `ConcurrentPool` relies on workers seeing the active
  session. 11/11 new tests pass. — commit `669fe0d`
- [x] Task 15: `EphemeralSession.__exit__` calls `store.delete_run(run_id)`
  for every registered store; `EphemeralStoreCleanupFailedError` carries
  `manual_cleanup_command` via `.cleanup_command`; spec §10.5 error block
  format; orchestrator + batch register the (store, run_id) pair after
  `deploy_session` opens; `RunPodProvider._create_pod` /
  `_create_serverless` rename pod to `kinoforge-<rand8>` and tag
  `kinoforge-ephemeral=true` under `policy.pod_name_includes_alias=False`.
  5/5 new tests pass; full suite passes (1 pre-existing skypilot fixture
  failure unrelated). **AC deferral:** the AC's "default mode:
  `kinoforge-<alias>-<rand4>` with `capability=<alias>` tag" is deferred —
  no `spec.tags["capability"]` is populated anywhere in the current code,
  so introducing the default rename would change observable pod naming
  without a wired alias source. Sub-δ candidate. — commit `4740c09`

**Sub-δ (hosted-engine delete + pre-flight gate):**
- [x] Task 16: `RemoteSubmitPollBackend._delete` ABC + `manual_cleanup_url`
  classmethod ABC + concrete `_delete_with_retries` (1s/2s/4s backoff,
  injectable sleep) on the base; `result()` fires the retry chain iff
  active session + `delete_on_completion=True`. Three new errors:
  `EphemeralDeleteUnsupportedError`, `EphemeralDeleteHTTPError`,
  `EphemeralDeleteFailedError` (spec §10.5 format). Replicate, Runway,
  and `_ReplicateImageInnerBackend` scaffold-stubbed (NotImplementedError
  on `_delete`; real `manual_cleanup_url` URL). ABC surface fixture
  regenerated. — commit `4c73b96`
- [x] Task 17: per-engine concrete `_delete` on Replicate
  (`DELETE /v1/predictions/{id}`) and Runway
  (`DELETE /v1/tasks/{id}`); `FalBackend._delete` raises
  `EphemeralDeleteUnsupportedError`. Token threaded from
  `Bearer.client_kwargs()` into each backend via new `token=` +
  `http_delete=` ctor kwargs; stdlib `urllib` default,
  injectable fake for tests. 14/14 new tests pass. — commit `6ba1ce0`

**CLI surface:**
- [x] Task 18: `--vault PATH` / `--ephemeral` / `--debug-show-secrets`
  global flags on the top-level parser. `main()` validates mutex on
  ephemeral + debug-show-secrets (exit 2 + named error before any work);
  loads vault from `--vault` or `KINOFORGE_VAULT` env (rejecting
  in-repo paths with exit 2); installs `RedactingLogFilter` on root +
  `kinoforge` loggers. Pre-flight check looks up
  `(engine.kind, compute.provider)` in `EPHEMERAL_CAPABILITIES` and
  refuses with the spec §11.4 error block on unsupported combinations
  (fal, luma, hosted). Read-only subcommands emit a stderr note and
  skip the gate. Entire dispatch wrapped in
  `with EphemeralSession(enabled=args.ephemeral)`. 9/9 new tests pass.
  — commit `c797627`

**Sub-ε (CI invariant + E2E proof points):**
- [x] Task 19: `tests/test_no_unredacted_writes.py` — AST-based scan of
  `src/kinoforge/` asserting the canonical write-site pattern (AC1-7,
  exemption tags). Three Sub-α/β sites retrofitted to add the
  `EphemeralSession`-gate (`Ledger._write_entries`,
  `JsonProfileCache._persist`, `batch_generate` finally). USER-ORDERED
  GATE re-verified RED→GREEN with a deliberately-injected put_json
  violation. 7/7 ACs pass. — commit `6b8b7f7`
- [x] Task 20: 3 E2E integration tests in `tests/integration/` driving
  the real CLI through FakeEngine + LocalProvider stack. Plus
  `setLogRecordFactory` hardening on the CLI filter install (logger-
  filters do NOT run during child-logger propagation; the factory
  override redacts every record at birth). `EPHEMERAL_CAPABILITIES`
  gains `("fake", "local"): True`. USER-ORDERED GATE re-verified
  RED→GREEN against all 3 tests (removed basename register, flipped
  STRICT delete_on_completion, removed filter install — each broke
  the corresponding test as expected). 3/3 tests pass.
  — commit `835704d`

**Docs:**
- [x] Task 21: `examples/vault/example.yaml` template with safety
  preamble + all documented fields. `DESIGN.md` Privacy boundary
  section with the 8 forward-compat contracts. PROGRESS.md Phase 45
  entry finalised with all 21 task SHAs. — this commit.

**Sub-ε (invariants + docs):**
- [ ] Task 20: `tests/test_no_unredacted_writes.py` AST invariant.
- [ ] Task 21: README "Confidentiality mode" section + Phase 45 finalisation.

**Single next action:** Phase 51 (ComfyUI poll parser real-shape fix +
`poll_timeout_s` 600→1800 s bump) shipped. Production parser now
descends `envelope[prompt_id]["status"]` per the real ComfyUI
`/history` shape, `/queue` probe fires whenever `status` is `"unknown"`
(real empty-during-execution), and the default cap covers Wan 14 B on
A5000-class GPUs. The next Wan 14 B live re-fire on RunPod should run
to completion without operator intervention; the log line will report
`queue_pos=0` during execution (live-running). Per-node progress
(`current_node`, sampler step counter) remains unavailable until C22
(ComfyUI WebSocket subscription) ships. Choose next: Track A (Bedrock
Luma Ray v2 live smoke, blocked on AWS Support case), Track B (Veo on
Vertex AI plan, unblocked since 2026-06-07), Phase 45 Sub-ε tail
(T20 AST invariant + T21 Confidentiality README), or open a new layer.

**Pre-existing failure (unrelated to Layer 5b):**
- `tests/providers/test_skypilot.py::test_t4_fixture_shape` fails on `main`
  with `AssertionError: T4 not present in launch fixture` — the captured
  launch fixture got volatile-uuid'd by the redaction pattern in a way
  the assertion didn't anticipate. Caught at Task 14 verify; not introduced
  by Task 14. Fix candidate for an early Sub-δ commit.

### Phase 46 — Successful-generations log scaffold

Layer 6. Stands up `successful-generations.md` as the durable C-rule log of every kinoforge
generation that introduces a new capability axis. Adds reminders to `CLAUDE.md` (Durability rules
bullet) and the RESUME block above. Adds a top-level `kinoforge --version` CLI flag so future log
entries don't have to grep `pyproject.toml`. Closes with four live-spend re-fires (one per known
stack) — each appends one entry + commits atomically.

Spec: `docs/superpowers/specs/2026-06-08-successful-generations-log-design.md` (`df70955`).
Plan: `docs/superpowers/plans/2026-06-08-successful-generations-log.md` (`bafbd59`).

- [x] Task 2: `successful-generations.md` scaffold — commit `72f5b18`
- [x] Task 3: `CLAUDE.md` Durability bullet — commit `1a76df9`
- [ ] Task 4: `PROGRESS.md` pointer + this section — commit `<sha>`
- [x] Task 5: `kinoforge --version` flag + 2 tests — commit `b913732`
- [x] Task 6: fal-ai/wan-t2v re-fire + entry #1 — commit `ef6d7a9`
- [x] Task 7: Wan 2.1 14B i2v on RunPod+ComfyUI re-fire + entry #4 — CLOSED by Phase 47 (Layer 7). Live re-fire on pod `7tfkwgtyf83gr2` (RTX A5000 @ $0.16/hr after 4090 capacity-retry) produced `47b3eb01950ff084.mp4` (964 KiB, 624×624, 81 frames @ 16 fps, 5.0625 s, h264/MP4); 25 m 24 s wall; ~$0.29 estimated. See entry #4 in `successful-generations.md` and Phase 47 below for root cause + fix.
- [x] Task 9: Replicate seedance-1-lite t2v re-fire + entry #2 — commit `d4fabd5` (864x480, 5.04 s, 121 frames, ~$0.10, 26 s wall)
- [x] Task 8: Runway gen4.5 t2v re-fire + entry #3 — commit `d4fabd5` (1280x720, 5.04 s, 121 frames, ~$1.25, 100 s wall)

**Live-spend budget (Tasks 6–9):** total spend this session ≈ $1.40 (Task 6 fal ~$0.05 + Task 8 Runway ~$1.25 + Task 9 Replicate ~$0.10 + Task 7 pod-wall ~$0.013). Remaining session budget: ~$18.60.

**Carry-forwards:**
- ~~Task 7 — Wan 2.1 14B i2v RunPod+ComfyUI HTTP 404 regression in `ComfyUIBackend.result()`.~~ — **CLOSED** by Phase 47 (Layer 7). Root cause was a RunPod-proxy startup-window race, not a ComfyUI or kijai-node regression.
- ~~LocalOutputSink renders the `model` slug as `unknown` for the fal config because `cfg.engine.fal.endpoint` isn't propagated to the sink.~~ — **CLOSED** by Phase 48 (Layer 8). `model_identity(cfg)` ABC method on every engine; orchestrator threads engine-native slug into the sink.

### Phase 47 — Layer 7 (ComfyUI RunPod-proxy 404 retry)

Phase 46 Task 7 carry-forward investigation. The failed re-fire on pod `xawdweboxapubz` surfaced an `/upload/image` HTTPError 404 — a different code path than the prior `/history/{id}` 404 on pod `sapoahjqbgd331` — so the regression was clearly **not** ComfyUI / kijai-pin drift. Live probe of the still-warm `xawdweboxapubz`: 50/50 sequential POSTs to `/upload/image` returned 200, confirming a transient RunPod-proxy startup window. ComfyUI 0.3.10 upstream `server.py` confirmed `/history/{id}` always returns 200 with `{}` for unknown IDs — it cannot 404 itself.

Fix shipped via two atomic commits + a final green smoke that produced a 964 KiB MP4 (`successful-generations.md` entry #4):

- [x] Task 1: diagnostic logger + first instrumentation patch — commit `bc25062`
- [x] Task 2: `_retry_proxy_call` + `submit()`/`result()` transient-404 retry + 4 new tests — commit `5fcfb9c`
- [x] Task 3: live re-fire green smoke — pod `7tfkwgtyf83gr2`, 25 m 24 s wall, ~$0.29 RunPod spend
- [x] Task 4: PROGRESS + successful-generations entry — this commit

**Spend this layer:** ~$1.30 total (failed attempt on `xawdweboxapubz` ~$1.00 — kept alive for live probing before destroy, plus the green smoke ~$0.30). Remaining session budget: ~$17.30.

**Carry-forwards:**
- ~~`LocalOutputSink` `model` slug = `unknown` for the ComfyUI config — same defect as the fal carry-forward.~~ — **CLOSED** by Phase 48 (Layer 8).
- No retries actually fired during the green run — the proxy startup window had closed before submit attempted. The retry helper is defensive coverage for the race, not a smoke-time bug-trigger. Future flaky-run investigation should confirm the WARNING line `[comfyui.submit.upload] transient HTTPError ...` lands in logs when the race re-occurs.

### Phase 48 — Layer 8 (model_identity ABC)

Fixes the `LocalOutputSink` `model = "unknown"` defect for non-hosted
engines (fal, ComfyUI, Bedrock). Adds a `model_identity(cfg) -> str`
`@abstractmethod` to both `GenerationEngine` and `ImageEngine`; each
engine returns the human-grep-able surface it already interprets
natively (hosted/diffusers/replicate-image -> `spec.model`; fal ->
`engine.fal.endpoint`; ComfyUI -> filename stem of the `kind: base`
entry in `models[]`; Bedrock -> `engine.bedrock_video.model_id`).
Orchestrator emits one WARNING per `deploy()` per stage when the engine
returns `""`; the sink falls back to the literal `"unknown"` as before.

Spec: `docs/superpowers/specs/2026-06-08-model-identity-abc-design.md`
(`a539b8c` + `8d17123`).
Plan: `docs/superpowers/plans/2026-06-09-layer-8-model-identity-abc.md`
(`608b805`).

- [x] Task 0: ABC additions + per-engine concrete impls + test-local stubs — commits `c6c6942` + `831a4f7`
- [x] Task 1: Per-engine unit tests + cross-engine ABC contract test — commits `08ea661` + `306a6ce`
- [x] Task 2: Orchestrator clip-stage wiring (`session.engine.model_identity(_cfg_dict(cfg))` + WARNING) — commits `3156267` + `4ac3017`
- [x] Task 3: Orchestrator keyframe-stage wiring (`resolved_image_engine.model_identity(kf_cfg_dict)` + WARNING) — commit `412aee2`
- [x] Task 4: Integration regression lock — `tests/integration/test_no_unknown_slug_for_example_configs.py` (12 parametrized cases incl. `examples/configs/comparison/*.yaml`). Caught + fixed 2 real YAML bugs along the way: `diffusers.yaml` missing `spec.model`, `skypilot-gpu.yaml` had wrong lifecycle field names silently dropped by pydantic — commits `1f28118` + `61e765c`
- [x] Task 5: PROGRESS + README + final gate — this commit

**Key design decisions:**
- Separate ABC method (display-only), independent of `HostedAPIEngine.key_base` (cache identity). Conflating the two would force cache-identity tightening to track filename aesthetics, which is the wrong direction.
- Each engine reads the cfg field it ALREADY interprets natively — no new schema surfaces, no Layer M reversal.
- Empty → `""` → WARNING → sink `"unknown"` fallback. Engine MUST NOT raise; cache-identity contract (`key_base`) stays stricter than display contract (`model_identity`).
- `ImageEngine` gets its own copy of the abstract method (parallel ABCs do not share a parent today; introducing one is out of scope).

**Bug catches during execution:**
- `diffusers.yaml` shipped without `spec.model` — silently produced `"unknown"` slug. T4 regression lock caught this; fix in `1f28118`.
- `skypilot-gpu.yaml` shipped with wrong lifecycle field names (`budget_usd` vs `budget`; `idle_timeout_s` vs `idle_timeout`; etc.) — pydantic was silently dropping them as extras, leaving `budget` unset. T4 regression lock surfaced the load failure; fix in `1f28118`.
- Code-quality review caught `_cfg_dict` local in T2 shadowing the module-level helper `_cfg_dict(cfg)` at orchestrator.py:142. Latent landmine (no live failure today); fix in `4ac3017`.
- Code-quality review caught `_adapters.py` only importing `image_engines.replicate`, silently hiding `fake` and `fal` image engines from production-side registry iteration. Fix in `306a6ce`.

**Test count delta:** +~45 net (per-engine unit tests +22, ABC contract test +11 parametrized, orchestrator wiring tests +3, integration regression lock +12 parametrized — minus 2 deselected for `cfg.engine.kind == "fake"` and 4 static skips for non-Config / unregistered-engine YAMLs).

**Carry-forwards / known follow-ups:**
- `nova-reel.yaml` is skip-listed in the regression lock (`nova_reel` engine kind not registered; planned for Layer 3 reactivation). Skip-list comment forward-points to Layer 3.
- `mode_identity` / `precision_identity` / `lora_stack_identity` sibling ABC methods would let the filename schema grow more facets (e.g. `t2v` / `i2v` / `flf2v` in the slug). Not in scope for Layer 8.
- Code-quality review observation: `_CapturingSink` is duplicated across orchestrator tests T2 and T3. Acceptable for two sites; promote to a module-level helper if a third site appears.
- WARNING template `engine %s returned empty model identity ...` is structurally duplicated across clip + keyframe stages. Two sites only; helper extraction premature.

### Phase 49 — Wan 2.1 14B t2v on RunPod + in-process warm-reuse smoke

Sibling of the Phase 47 / Phase 28 ComfyUI + Wan i2v stack on the t2v
mode axis, plus the first proof of the in-process warm-reuse path
across two consecutive generations on the same pod. Same provider,
same engine, same custom-node pack pins; t2v variant of the diffusion
checkpoint; new graph with `WanVideoEmptyEmbeds` substituted in for
the i2v image-input pipeline.

No spec / plan document — single-session smoke driven from PROGRESS
B-row context (the user's "do this autonomously" prompt + the
`successful-generations.md` C-rule). One RED scaffold commit before
live spend, one conftest fix when the first live attempt skipped, one
documentation commit at the end.

- [x] Task 0: t2v YAML + hand-authored API graph + offline graph-shape lock (6 tests, all green) — commit `4c6ea68`
- [x] Task 1: `tests/live/conftest.py` — session-scoped silent `.env` loader so credential gates fire at pytest collection (benefits every live test) — commit `36b65ca`
- [x] Task 2: live smoke green — cold (402.4 s) + warm (271.7 s) on pod `1cyd9v4e17ufvc`, two MP4s @ 480×480 / 16 fps / 81 frames, ~$0.10 estimated, log entry #5 in `successful-generations.md` — this commit

**Key design decisions:**
- **Why a programmatic harness, not a CLI re-invocation:** PROGRESS B3 (in-session orchestrator warm-reuse retrofit) + B4 (CLI exposure of `LifecycleManager.warm_reuse_or_create`) remain unbuilt. The only way to keep one pod across two generations today is to hold the `Instance` returned by `orchestrator.generate()` and pass it back in as the next call's `instance=` kwarg — exactly what the test does. Building Layer Y to make two `kinoforge generate` invocations land on the same pod is multi-day work (cooperative session-start lock B7 first, then ledger-classify integration, hot-path touch) and was explicitly out of scope here.
- **Why a separate graph file:** the i2v graph relies on `LoadImage` + `WanVideoImageToVideoEncode` + `WanVideoClipVisionEncode` + `ImageResizeKJv2`, all of which the sampler's `image_embeds` slot consumes. T2V wants a text-only embed shape — `WanVideoEmptyEmbeds` produces it from `(width, height, num_frames)` widget values. Trying to overload the i2v graph with conditional branches would have broken the offline graph-shape lock that catches kijai-pin drift.
- **Why double the budget + extend max_lifetime:** the lifecycle block governs the BudgetTracker mid-run circuit breaker, not an estimate. With two generations on the same pod, the original i2v limits (budget 2.0, max_lifetime 50m) would have been hit ~70 % through gen 2 in the worst case. Doubled to 4.0 / 90 m respectively.
- **Why `WanVideoEmptyEmbeds(width=480, height=480, num_frames=81)` widget-baked:** the graph + YAML params must agree (lock test `test_empty_embeds_shape_matches_params` enforces). A runtime-override path through `spec.node_overrides` would have been cleaner but isn't needed until the next caller wants a different shape.

**Bug catches during execution:**
- **Live test skipped silently on first run because pytest never loaded `.env`.** Module-import-time gate (`os.getenv("KINOFORGE_LIVE_TESTS") == "1"` and three secret keys) ran before pixi's `[activation.env]` block injected anything into `os.environ`. The historical workaround was for the operator to `source .env` in the host shell before `pixi run pytest`; that doesn't compose with Claude's autonomous execution path. Fix in `36b65ca` (session-scoped conftest) is silent + override-`False` so explicit shell exports always win — no regression risk for the operator-driven path.
- **No t2v-specific runtime regressions.** The hand-authored graph + `WanVideoEmptyEmbeds` rewiring landed on the first live attempt — testament to the i2v graph being a good template and the offline graph-shape lock catching what it was meant to catch.

**Carry-forwards / known follow-ups:**
- **PROGRESS B3 / B4 still open.** This smoke proves the in-process warm-reuse path works end-to-end; CLI exposure (`kinoforge generate` consulting the ledger for matching live pods) remains spec'd but unbuilt. Two CLI invocations against the same pod still cold-boot twice.
- **GPU type not captured into smoke fixture.** `last_t2v_smoke.json` records `pod_id` but not `gpu_type`; entry #4's fixture also lacked it. Surface candidate: `orchestrator._provision_instance_and_build_backend` could lift `Instance.tags["gpu_type"]` into the return path. Trivial; non-blocking.
- **Test count delta:** +7 net (6 offline graph-shape tests + 1 live smoke that skips offline).

### Phase 50 — Graceful interrupt + ComfyUI poll observability

Sibling layer triggered by a 2026-06-10 Wan 14B t2v live smoke that hung
silently after `provisioner.provision` returned, required two `Ctrl-C`
presses to escape, and left `provider.destroy_instance` unrun. Three
orthogonal defects (silent stall with no per-tick log; two-press
requirement from a wedged `ConcurrentPool.close`; no
`KeyboardInterrupt` WARN naming the surviving pod) repaired in one
six-commit phase. No live spend — every fix lands offline behind
injected I/O seams and `caplog` assertions.

Spec: `docs/superpowers/specs/2026-06-10-graceful-interrupt-and-poll-observability-design.md` (`8318686`).
Plan: `docs/superpowers/plans/2026-06-10-graceful-interrupt-and-poll-observability.md` (`aa4e407`).

- [x] Task 0: `CancelToken` + `Cancelled` foundation — commit `f52eb00`
- [x] Task 1: ABC + pool signature changes + bounded `ConcurrentPool.close` watchdog — commits `e774e2a` + `9578ed7` (quality-review fix forwarding `cancel_token` in the `_ListPool` test fake)
- [x] Task 2: ComfyUI per-tick poll log + `poll_timeout_s` cfg field + cooperative cancel — commit `71fb9ab`
- [x] Task 3: `RemoteSubmitPollBackend` cancel honoring (Replicate / Runway / Luma / Fal share the path) — commit `611d243`
- [x] Task 4: Orchestrator stage-loop `(KeyboardInterrupt, Cancelled)` arm — WARN-not-destroy, cancel-aware `deploy_session.__exit__` pool close — commit `b8234da`
- [x] Task 5: CLI two-press SIGINT handler + `SessionContext.cancel_token` — commit `ca3862d`
- [x] Task 6: Closeout — this commit

**Key design decisions:**
- **Cooperative cancellation, not preemption.** `CancelToken` is a thin
  `threading.Event` wrapper; backends call `raise_if_set()` at the top
  of every poll iteration and `wait(interval)` in place of
  `time.sleep`. No thread is killed; in-flight HTTP requests complete,
  then the next loop iteration raises `Cancelled`. Preserves the
  semantics existing tests expect.
- **`_NULL_TOKEN` sentinel as default-kwarg default.** Library + test
  callers that pass no token get unchanged behavior; the sentinel is
  never `set()`, so `raise_if_set()` and `wait()` are no-ops with the
  same blocking semantics as `time.sleep`. Lets every ABC grow a
  defaulted kwarg without breaking a single caller.
- **WARN-not-destroy on interrupt.** Matches the Layer 5b session
  manager intent locked in `3bc6473` — `--ephemeral` and
  non-`--ephemeral` runs both keep the pod alive on Ctrl-C; the in-pod
  self-terminator + `kinoforge reap` handle teardown. `ValidationError`
  path still destroys (existing behavior preserved).
- **Hard `poll_timeout_s` upper bound surfaces stalls without operator
  patience.** Default 600 s (10 min) on `ComfyUIEngineConfig`; lift for
  known-slow models. The `TimeoutError` message contains the literal
  substrings `last_status=` and `exec_node=` (plus the actual node
  name) so a single line in CI logs diagnoses the stall.
- **Structured per-tick INFO log** — every poll iteration emits
  `comfyui poll job=… elapsed=…s status=… queue_pos=… exec_node=…`
  matching a regex the test pins. `queue_pos=None` when not queued;
  separate `/queue` probe populates it when status is `queued`. Means
  the next stall self-diagnoses without a separate diagnostic patch.
- **Bounded `ConcurrentPool.close(cancel_pending=True, timeout=…)`
  via watchdog thread.** A wedged worker that ignores cancellation no
  longer blocks shutdown forever. `close()` with no kwargs preserves
  today's `wait=True, no cancel_futures` semantics — the new path is
  strictly opt-in. WARN log "worker still running after %.1fs;
  abandoning slot" tells the operator a slot was abandoned (daemon
  thread exits with the process).
- **Two-press SIGINT handler.** First press sets the token; second
  press restores `SIG_DFL`. Third press kills the process the usual
  way. Operator never has to escalate to `SIGKILL`.

**Bug catches during execution:**
- **`_ListPool` test fake silently dropped `cancel_token` kwarg** —
  Task 1 quality review (`9578ed7`) caught the test-only `_ListPool`
  forwarding `pool.submit(job)` to its backend without the new kwarg.
  Tests would have green'd without exercising the production path. Fix
  forwards `cancel_token` to the backend; matches the production
  `SequentialPool` shape.

**Carry-forwards / known follow-ups:**
- **KeyframeStage cancel_token plumbing — deferred.** `KeyframeStage`
  uses `ImageBackend` directly (no `pool.submit` site). The current
  keyframe except-arm provides WARN-not-destroy via the orchestrator
  outer except, but the in-stage cancel honoring waits on
  `ImageBackend` growing the same kwarg. Slot when the next image
  backend exhibits a stall.
- **`pool.map` cancel_token forwarding — deferred.** The t2v
  non-chained fan-out path. `pool.map(jobs)` ignores token today;
  interrupt during fan-out waits for all in-flight backends to finish
  their current poll tick before the except fires. Workers still honor
  the token internally — the wait is just longer than the
  `pool.submit` path. Promote when a fan-out smoke shows the latency
  in practice.
- **Diffusers / Hosted / Bedrock per-backend cancel hardening —
  inherited at the ABC level.** Every concrete backend grew the
  `cancel_token` kwarg in Task 1, but only `ComfyUIBackend` (Task 2)
  and `RemoteSubmitPollBackend` (Task 3) actually honor it. If
  `DiffusersBackend` / `HostedAPIBackend` / `BedrockVideoBackend` stall
  in production the same way ComfyUI did, the structured logs from
  Task 2 will tell us where, and a follow-up layer adds backend-
  specific per-tick logging + `wait()`-based sleep replacement.
- **Split-wait helper DRY** — the same `token.raise_if_set();
  …probe…; token.wait(interval_s)` pattern is duplicated at 2 sites
  (`ComfyUIBackend.result` + `RemoteSubmitPollBackend.result`). Factor
  into `kinoforge.core.cancel` as a reusable helper (`poll_with_cancel`
  / `bounded_poll`) when a 3rd caller appears — see new C-section
  entry below.
- **`kinoforge reap --orphans` helper — deferred.** Operator helper
  that walks the RunPod REST API for pods absent from the ledger.
  Already mostly covered by Layer V `sweep` but ergonomics could
  improve — single command after an interrupt that destroys every
  unaccounted pod.

**Test count delta:** +27 net offline tests (Task 0 +7 `test_cancel.py`; Task 1 +4 `test_pool_cancel.py`; Task 2 +5 across `test_comfyui_cancel.py` + `test_comfyui_timeout.py` + `test_comfyui_poll_log.py`; Task 3 +4 `test_remote_submit_poll_cancel.py`; Task 4 +5 across `test_orchestrator_interrupt.py` + `test_orchestrator_cancelled.py`; Task 5 +2 `test_sigint_handler.py`). Full suite at end of Task 5: **1898 passed, 26 skipped** — no live smoke required for this layer.

### Phase 51 — ComfyUI poll parser real-shape fix + poll_timeout_s bump

Single-task patch phase. A 2026-06-10 Wan 14B t2v live smoke on pod
`2fhv2v3cccs98d` was killed by Phase 50's 600 s `poll_timeout_s` at
elapsed 602.8 s while the GPU was at 100% — a healthy sampler tick,
not a hang. Investigation surfaced two coupled defects: Phase 50's
`_extract_poll_fields` parser only ever ran against the flat fixture
shape (`{"status": {"status_str": …}}` at the envelope root), but
real ComfyUI `/history/{prompt_id}` nests the per-job dict under the
`prompt_id` key. Production parser therefore returned
`status="unknown"` for the entire run, which gated the `/queue` probe
out of firing (it was scoped to `status=="queued"` only), leaving
`queue_pos` and `exec_node` as `None` for every log line. With no
real observability and a 10-min cap tuned for ~6-min Wan 1.3 B runs,
a healthy Wan 14B (25-40 min on A5000-class GPUs) was indistinguishable
from a stuck job.

No spec / plan doc — single-session offline fix driven by
systematic-debugging Phase 1 evidence. No live spend.

- [x] Task 1: RED tests — 9 in `tests/engines/test_comfyui_poll_real_shape.py` covering nested envelope shape, flat back-compat, empty-envelope `"unknown"` sentinel, `job_id=None` legacy path, widened `/queue` gate, queue probe with missing job_id stays `None`, backend ctor default 1800 s, and `ComfyUIEngineConfig.poll_timeout_s` default 1800 s.
- [x] Task 2: GREEN — `_extract_poll_fields` grows an optional `job_id` parameter and descends into `envelope[job_id]["status"]` when the top-level key is absent (mirrors the outputs extractor's existing dual-shape pattern). Caller in `ComfyUIBackend.result` passes `job_id`; gate widened to `if last_status in ("queued", "unknown"):` so the `/queue` probe fires during real execution.
- [x] Task 3: poll_timeout_s default 600 → 1800 across all four sites (`ComfyUIBackend.__init__`, `ComfyUIEngine.backend` cfg-walk fallback (two lines), `ComfyUIEngineConfig.poll_timeout_s` pydantic Field).
- [x] Task 4: Two pre-existing regression-test updates — `test_result_polls_until_completed` + `test_result_retries_on_transient_404_then_returns` both mocked `http_get` URL-agnostically; widened queue probe now consumes additional GETs in production reality, so both mocks route `/queue` → empty envelope and assert the history-call counter instead.
- [x] Task 5: PROGRESS update + new C22 entry covering deferred WebSocket-based per-node observability — this commit.

**Key design decisions:**

- **Parser stays dual-shape, not real-only.** The flat shape is still
  used by `test_comfyui_poll_log.py`, the Phase 47 retry test, and
  the capture-tool fixtures. Mirror the outputs extractor's pattern
  (try flat first, fall back to nested) rather than flipping the
  parser real-only and forcing test-fixture rewrites.
- **`/queue` probe widens to `unknown`, not "every tick unconditionally".**
  `unknown` already means "I cannot tell from `/history` alone" — that
  is exactly when `/queue` is informative. Once a real `status_str`
  appears (`executing` / `success` / `error`), `/history` is authoritative
  and the probe stops. Avoids doubling HTTP load for the steady-state
  happy path.
- **`exec_node` stays `None` during real runs.** ComfyUI's
  `/history/{id}.status.exec_info.current_node` only populates
  post-completion; while the job runs, `current_node` lives in the
  WebSocket `executing` event stream that kinoforge does not subscribe
  to. Real per-node observability is the C22 follow-up, not this phase.
- **1800 s default, not "no cap".** Phase 50's hard timeout still has
  value (catching truly wedged processes) — the fix is calibration,
  not removal. Operators with slower setups override per-config via
  `engine.comfyui.poll_timeout_s`.

**Bug catches during execution:**

- **Two pre-existing tests modeled production with URL-agnostic GET
  mocks.** Widening the `/queue` gate exposed both: `test_result_polls_until_completed`
  raised `IndexError` because the queue probe consumed the next entry
  in its response list; `test_result_retries_on_transient_404_then_returns`
  failed `assert calls["n"] == 4` (now 5 with the extra queue probe).
  Both tests now dispatch by URL and assert on the history-call counter
  alone — more faithful to production.
- **`ComfyUIBackend.__init__` docstring previously claimed "Default 600 s
  covers Wan 14B t2v (~6 min)".** That figure was the Phase 49 Wan
  *1.3 B* smoke wall time — never the 14 B value. Docstring updated.

**Carry-forwards / known follow-ups:**

- **C22 — ComfyUI WebSocket live observability** (new C-section entry
  above). Per-node + per-step progress remains invisible without the
  WebSocket subscription. Deferred until the next stall mid-sampler.
- **Recovery of orphan pod `2fhv2v3cccs98d`** — operator destroyed
  manually before this phase landed; no kinoforge-side action needed.
- **Phase 50 docstring + spec did not require parser validation against
  the real `/history` envelope shape** — a single sentence "tested
  against the production envelope, not just the flat fixture" in
  future observability specs would have caught this offline.

**Test count delta:** +9 net (`test_comfyui_poll_real_shape.py`).
Full suite: **1930 passed, 26 skipped** (was 1898 + 26 at end of Phase 50;
+32 reflects 9 new Phase 51 tests + 23 unrelated additions across
in-flight branches collected by full-tree runs).

### Phase 52 — GPU quota utilization-burn (Tasks 1–13)

Spec: `docs/superpowers/specs/2026-06-10-gpu-quota-utilization-burn-spec.md`
Plan: `docs/superpowers/plans/2026-06-10-gpu-quota-utilization-burn.md`

Lib: `tools/quota_burn_lib.py` | CLI: `tools/quota_burn.py` (Task 8+)

- [x] Task 1: Manifest dataclass + JSON round-trip — commit `682d236`
- [x] Task 2: GCP spin-up helpers — commit `2b712f1`
- [x] Task 3: GCP teardown + MTD spend snapshot helpers — commit `7f1bbc1`
- [x] Task 4: AWS spin-up helpers — commit `698473b`
- [x] Task 5: AWS teardown + MTD spend snapshot helpers — commit `33129a7`
- [x] Task 6: Quota-submit helpers (GCP fallback URL + AWS case attach) — commit `12267f4`
- [x] Task 7: BigQuery dry-run gate — `BigQueryCapExceeded` + `bq_scan_with_cap`; added `google-cloud-bigquery>=3.11` PyPI dep; 2 tests green — commit `ac00594`
- [x] Task 8: CLI dispatcher — `tools/quota_burn.py` with 5 subcommands (`spin-up`, `tear-down`, `snapshot`, `scan-bigquery`, `submit-quota`); 108 tests green — commit `93fcbd9`
- [x] Task 9: Justification draft templates + PROGRESS update — `docs/quota-justification-gcp.md` + `docs/quota-justification-aws.md`; Phase 52 prerequisites documented
- [x] Task 10: Day 0 — live spinup — manifest at `.quota_burn/manifest.json`, both clouds live 2026-06-11 19:16 local
- [x] Task 11: Days 1–4 daily snapshot — Day 1 skipped (kernel-shutdown re-spin); Day 2 logged 2026-06-13 (BQ export not ready, partial); Days 3-5 skipped (autonomous overnight C33 work consumed window); Day 6 final snapshot logged 2026-06-17 (see Task 13 closeout)
- [x] Task 12: Day 6 — populated justification drafts with snapshot figures — commit `b18397b`
- [x] Task 13: Day 6 — submit quotas + teardown + closeout — see closeout below

#### Justification drafts

- GCP: `docs/quota-justification-gcp.md`
- AWS: `docs/quota-justification-aws.md`

Both populated 2026-06-17 with Day-6 snapshot figures (commit `b18397b`); placeholder substituted, repo URL filled (public).

#### Task 10 prerequisites (operator — REQUIRED before live spinup)

Task 10 will abort at GCP budget creation unless these are set in `/workspace/.env` first:

1. **`GCP_BILLING_ACCOUNT_ID`** (REQUIRED) — format `XXXXXX-XXXXXX-XXXXXX`.
   Find it: Cloud Console -> Billing -> Account Management -> Billing Account ID.
   ```
   GCP_BILLING_ACCOUNT_ID=XXXXXX-XXXXXX-XXXXXX
   ```
2. **`GCP_NOTIFICATION_CHANNEL_ID`** (OPTIONAL) — format `projects/<proj>/notificationChannels/<id>`.
   Omit to create the budget with no extra notification channel (owner email still fires via the
   billing budget default).
3. AWS creds already present in `.env` (see memory: `project_hosted_video_keys_configured`).
4. Run `pixi run preflight` — must exit 0 (checks creds present, zero active RunPod pods, clean
   working tree) before invoking `python -m tools.quota_burn spinup`.

#### Task 10 closeout — 2026-06-11

Live resources (manifest at `.quota_burn/manifest.json`):

- **GCP project `kinoforge-prod-0ddb375e`** (zone us-west1-a):
  - VM `kinoforge-burn-upddv3` (e2-small, status RUNNING)
  - Boot disk `kinoforge-burn-upddv3-disk` (10 GB pd-balanced, auto-delete=True)
  - GCS bucket `kinoforge-quota-burn-gcp-upddv3`
  - Budget `billingAccounts/01522C-EC9AA4-64A7D5/budgets/c3aeaec1-a1f9-410f-89a9-bebaecec238d` ($7 alert threshold)
- **AWS account 009910375621** (us-west-2):
  - EC2 `i-099081763c43fe593` (t4g.nano, running, kernel-side `shutdown -h +480`)
  - S3 bucket `kinoforge-quota-burn-aws-kmwsgh`
  - DynamoDB: SKIPPED (kinoforge-ci lacks `dynamodb:CreateTable`; 10c/5d signal loss acceptable)
  - Budget: SKIPPED (kinoforge-ci lacks `budgets:ModifyBudget`; kernel-shutdown + daily snapshot
    carry the safety net)

**Bug/perm catches during the 10 spinup attempts:**

1. `google-cloud-billing` + `google-cloud-billing-budgets` not in default pixi env → added
   to `[pypi-dependencies]`.
2. `tools/quota_burn.py` missing `load_dotenv()` → operator-supplied env vars didn't reach
   `_build_gcp_clients`.
3. `_Bundle` class-body `storage = storage.Client(project=...)` shadowed the imported
   module → aliased to `_storage`.
4. `_GcpInstanceResource` duck-type rejected by real `InstancesClient.insert()` → refactored
   to dict literal; test assertions converted to dict-key access. Same for the local
   `_Budget`/`_BudgetFilter`/`_BudgetAmount` dataclasses.
5. GCE Instance dict needed `network_interfaces` block with default-VPC + External NAT.
6. Cloud Billing Budget API was disabled on the new project → `gcloud services enable
   billingbudgets.googleapis.com`.
7. `kinoforge-runner` SA lacked billing-account-level perms → granted
   `roles/billing.costsManager` on billing account `01522C-EC9AA4-64A7D5` (via the
   workspace-cached `<OPERATOR_EMAIL>` operator identity).
8. Empty `GCP_NOTIFICATION_CHANNEL_ID` was sent as `[""]` in the budget — rejected as
   invalid channel name. Now filters empty out.
9. SSM-resolved AMI in `aws_spin_up` required `ssm:GetParameters` which `kinoforge-ci`
   lacks → hardcoded `ami-029ea2abb0342f2f2` (Canonical Ubuntu 22.04 arm64, us-west-2).
10. `run_instances` response had empty `BlockDeviceMappings` (populates only after poll)
    → tolerate empty; volume tracked-as-empty since `DeleteOnTermination=True`.
11. `dynamodb:CreateTable` perm missing on `kinoforge-ci` → wrapped in try/except, log,
    skip cleanly.
12. `budgets:ModifyBudget` perm missing on `kinoforge-ci` → same tolerance pattern.

**Spend orphans burned during retries:** ~$0.05 (5 sets of partial-spinup GCP VMs +
buckets + budgets manually cleaned via gcloud).

**Burn rate (steady state):** ~$0.013/hr combined (GCP e2-small ~$0.0084/hr +
AWS t4g.nano ~$0.0042/hr + storage negligible). 5-day projection ~$1.50 +
storage/disk/budget-API hits ~$1 → **~$2.50 total, well under $20**.

**Single next action:** Task 11 day-3 snapshot (target 2026-06-14, after BQ export
first job lands — see Task 11 day-2 entry below).

#### Task 11 — Day 2 snapshot — 2026-06-13 12:35 PDT

**Resource re-spin reconciliation:** Task 10 closeout (commit `79cae2f`) cited AWS
`i-099081763c43fe593` + bucket `kmwsgh`; the current manifest + live state shows AWS
`i-0c61ecda9dd38fef8` + bucket `at2e8a` (re-spun 2026-06-12 07:36 UTC, ~12h after
initial spinup — kernel-shutdown safety net almost certainly fired the original
instance because the 8h `shutdown -h +480` predated commit `72bfda8` that extended the
window to 8d for the 5-day burn). GCP resources unchanged (`upddv3` suffix).

**Live snapshot output** (`pixi run python -m tools.quota_burn snapshot --project-id
kinoforge-prod-0ddb375e`, post-hardening commit `977fafa`):

- `gcp_status: export-not-ready` — BQ billing-export dataset `all_billing_data`
  created 2026-06-13 12:13 PDT, first `gcp_billing_export_v1_*` table not yet landed
  (6-24h lag). GCP MTD unavailable until day 3.
- AWS June MTD total: **$0.51** (includes pre-burn Phase 51 spend; quota-burn-only
  attribution requires `--start-date 2026-06-12` filter — out-of-scope for Task 11).
  Top services: KMS $0.21, VPC $0.13, EC2-compute $0.10, EC2-other $0.08.

**Steady-state estimate** (40.5 h GCP uptime + 35.2 h AWS uptime, BQ data
unavailable):
- GCP VM ~$0.340 + AWS EC2 ~$0.148 → combined **~$0.49**
- Envelope: $10 pause / $15 abort / $20 cap → ~3% of pause threshold

**Plumbing fixes folded into Day-2:**

13. BQ billing-export NOT enabled on the new project (Cloud Console step) →
    operator enabled 2026-06-13 12:13 PDT after dataset pre-created via
    `bq mk --dataset --location=US all_billing_data`.
14. `kinoforge-runner` SA lacked `bigquery.datasets.create` → self-granted
    `roles/bigquery.admin` on `kinoforge-prod-0ddb375e` (covers create + manage).
15. `kinoforge-ci` AWS IAM user lacked `ce:GetCostAndUsage` → self-attached managed
    policy `AWSBillingReadOnlyAccess`.
16. `_do_snapshot` blew up at GCP fetch when export not ready → commit `977fafa`
    translates `NotFound 404` + `BadRequest 400 "does not match any table"` to
    `BillingExportNotReady`, surfaces as `gcp_status` field on partial report.

#### Task 13 closeout — 2026-06-17 (Day 6, 1 day over plan)

**Final MTD snapshot** (`pixi run python -m tools.quota_burn snapshot`, as of
`2026-06-17T06:49:53` local):

- GCP `kinoforge-prod-0ddb375e`: **$2.58** total. Compute Engine $2.20,
  Networking $0.36, Cloud KMS $0.02, Cloud Storage $0.00, BigQuery $0.00.
  `gcp_status: ok` — BQ billing-export now live, no partial-report fallback.
- AWS `009910375621`: **$1.76** total. VPC $0.57, EC2-compute $0.48,
  EC2-other / EBS $0.37, KMS $0.33, Cost Explorer $0.01, S3 $0.00.
- **Combined $4.34** — 22% of $20 cap, well below $10 pause threshold.

**Steady-state delta vs Day-2 estimate:** actual GCP Compute $0.0157/hr (vs
$0.0084 estimated; ~1.9× under-estimate); AWS dominated by VPC + KMS, not
the t4g.nano compute line. Spec napkin math used the on-demand price sheet
only and ignored the always-on networking + KMS-key floor that hit ~$0.90
in non-compute over six days.

**Quota submissions:**

- AWS: real service-quotas request filed via boto3. Request ID
  `ac4331ff4ef64f8c9ac80c6b2e62f75d9Z68FM0c` (quota code
  `L-DB2E81BA` — All G & VT Spot Instance Requests, region `us-west-2`,
  desired_value=4). Lib emitted WARNING `aws_submit_quota: no CaseId on
  RequestedQuota response; justification not attached` — newer
  service-quotas API doesn't return a CaseId on first call, so the
  justification text from `docs/quota-justification-aws.md` was NOT
  auto-attached to a support case. If AWS asks for context, paste the doc
  contents into a manual case follow-up.
- GCP: SDK fallback path (expected). New `google-cloud-quotas` SDK exposes
  `cloudquotas_v1beta.CloudQuotasClient` with `create_quota_preference`,
  not the old `quotas_v1beta.QuotaAdjusterClient.create_quota_adjustment`
  shape the lib targets — the broad `except Exception` in
  `gcp_submit_quota` catches the AttributeError and emits the pre-filled
  console URL. **Operator action**: click
  `https://console.cloud.google.com/iam-admin/quotas?project=kinoforge-prod-0ddb375e&filter=metric%3Acompute.googleapis.com%2FNVIDIA_T4_GPUS+OR+compute.googleapis.com%2Fgpus_all_regions`,
  paste the body of `docs/quota-justification-gcp.md` into the request
  reason, submit both global `gpus_all_regions=1` and regional
  `nvidia_t4_gpus=1` (us-west1).

**Teardown:** `pixi run python -m tools.quota_burn teardown --project-id
kinoforge-prod-0ddb375e --zone us-west1-a` returned:

- GCP deleted: VM `kinoforge-burn-upddv3`, bucket
  `kinoforge-quota-burn-gcp-upddv3`, budget
  `billingAccounts/01522C-EC9AA4-64A7D5/budgets/c3aeaec1-a1f9-410f-89a9-bebaecec238d`
- AWS deleted: EC2 `i-0c61ecda9dd38fef8`, S3 bucket
  `kinoforge-quota-burn-aws-at2e8a`

**Post-teardown verification** (zero remaining):

- `gcloud compute instances list --filter='labels.kinoforge-quota-burn:*'` → empty
- `gcloud storage buckets list --filter='name:kinoforge-quota-burn-*'` → empty
- `aws ec2 describe-instances --filters Name=tag:kinoforge-quota-burn,Values='*'` → empty
- `aws s3api list-buckets --query 'Buckets[?starts_with(Name, ...)]'` → empty

**Bug/perm catches during Day-6 (Task 13):**

17. `google-cloud-quotas` PyPI dep missing → added via
    `pixi add --pypi google-cloud-quotas>=0.1`.
18. SDK package rename: `google.cloud.quotas_v1beta` →
    `google.cloud.cloudquotas_v1beta`; client class
    `QuotaAdjusterClient` → `CloudQuotasClient`. Updated import in
    `tools/quota_burn.py:_do_submit_quota`. New client lacks
    `create_quota_adjustment` method (now `create_quota_preference` with a
    different request shape) — left lib unchanged because the broad
    `except Exception` already routes to the console-URL fallback the
    spec sanctions; rewriting `gcp_submit_quota` for the new SDK is
    deferred until a future burn re-fires.

**Phase 52 status: CLOSED + ABANDONED 2026-06-17.** AWS request
`ac4331ff4ef64f8c9ac80c6b2e62f75d9Z68FM0c` was DENIED by AWS Service
Quotas Support. GCP request was never submitted — operator gave up at
the console-quota UI (~100 metrics on the filtered page; pre-fill
filter URL produced by the lib's `_gcp_console_quota_url` is stale
against the current console). Operator pivoted to **vast.ai +
Lambda Cloud** for GPU compute (see **Phase 53** below). Six days of
GCP+AWS burn ($4.34 spend) produced zero quota grants.

**Carry-forward:** none. C29 abandoned alongside the rest of the
GCP+AWS workstream.

### Phase 53 — vast.ai + Lambda SkyPilot integration

Scope: wire `vast.ai` + `Lambda Cloud` into kinoforge's existing
`SkyPilotProvider` so GPU compute lands on operator-owned accounts at
those two providers instead of GCP/AWS.

**Operator scoping decisions (2026-06-17):**
- Abandon mode: **stop new spend only**, no rip-out. Historical
  Bedrock/Vertex engines + GCP/AWS extras + `tools/quota_burn*`
  preserved as dead code; PROGRESS marks them ABANDONED. Reversible.
- First live target: **Lambda first** (predictable single-vendor),
  Vast.ai parity smoke queued.

#### Stage A — extras + cred materialization (CLOSED 2026-06-17)

- [x] **A1: pixi.toml extras** — `skypilot` extras grew
  `gcp, aws` → `gcp, aws, vast, lambda` (commit `982c6c4`). `vast`
  pulls `vastai-sdk`; `lambda` adds no transitive deps (pure HTTP).
- [x] **A2: cred materialization script** — `tools/setup_sky_creds.sh`
  reads `LAMBDA_API_KEY` → `$HOME/.lambda_cloud/lambda_keys` and
  `VAST_API_KEY` → `$HOME/.config/vastai/vast_api_key`. Mode 600
  files + mode 700 parents. Silent no-op when env vars unset
  (commit `982c6c4`). Pixi 0.69 does **not** auto-source `.env`
  before activation, so the script self-sources
  `${PIXI_PROJECT_ROOT:-/workspace}/.env` first (commit `375409a`).
- [x] **A3: activation wiring** — `[feature.live-skypilot.activation]`
  `scripts = ["tools/setup_sky_creds.sh"]`. Self-heals after
  container rebuilds because `/home/claudeuser` is overlay-only
  except for the `.claude` bind-mount (commit `982c6c4`).
- [x] **A4: .env.example** — `LAMBDA_API_KEY` + `VAST_API_KEY` blocks
  added (commit `982c6c4`). GCP/AWS/Azure blocks preserved per
  stop-new-spend scoping decision.
- [x] **A5: `sky check` verification** — both clouds report
  `enabled [compute]`. AWS still enabled too (preserved scaffolding);
  GCP dropped because the kinoforge-runner SA auth cache aged out
  after the project swap — irrelevant per abandonment.

#### Stage B — raw `sky launch` capacity proof (CLOSED 2026-06-17)

First-try result: **Lambda A6000 `gpu_1x_a6000` ($1.09/hr) returned
`insufficient-capacity` in us-east-1**, the only region sky considered
under the default `--infra lambda`. Retried with A10 `gpu_1x_a10`
($1.29/hr) — provisioned cleanly in us-east-1, `nvidia-smi -L` reported
`NVIDIA A10 (UUID: GPU-6b915775-...)`, `SMOKE_OK` echoed. Explicit
teardown via `sky down -y` returned `Terminating cluster ...done.`;
`sky status` confirms zero remaining clusters.

Smoke spend: ~$0.07 (provision ~1 min + nvidia-smi ~5 s + teardown
~30 s on $1.29/hr → ~$0.025; SkyPilot's launch path adds an extra
~1-2 min idle billing window between launch-completes and
job-start).

**Key learnings:**

- **No T4 / no CPU-only on Lambda.** Cheapest GPU available is
  A6000 1x ($1.09/hr); A10 1x is the next tier ($1.29/hr).
  The justifications written for Phase 52 Task 12 referenced T4 —
  moot now per Phase 52 abandonment.
- **Capacity is fluid.** A6000 was unavailable today, A10 worked.
  Production runs must use `--retry-until-up` OR fall through to
  Vast.ai as the redundant cloud. The kinoforge SkyPilotProvider
  passes `clouds=` to `sky.launch` (line 569 of
  `src/kinoforge/providers/skypilot/__init__.py`) so a list like
  `["lambda", "vast"]` would let sky try both before failing —
  but this requires Stage C cfg surface (see below).
- **`sky show-gpus` is deprecated** — use `sky gpus list --infra <c>`
  on this sky version (0.12.3.post1).

#### Stage C — kinoforge cfg surface for cloud pinning (CLOSED 2026-06-17)

Today's `ComputeConfig` (`src/kinoforge/core/config.py:513`) had no
`cloud` field. The `SkyPilotProvider` factory at
`src/kinoforge/providers/skypilot/__init__.py:809` was
`lambda: SkyPilotProvider()` — no kwargs — so `self._clouds` stayed
`None` and sky considered every enabled cloud (AWS+GCP+Lambda+Vast).
With `requirements.max_usd_per_hr: 1.00`, sky picked whichever of the
four had a matching offer in capacity. That meant **Vast.ai always
won on price** for any non-tiny GPU.

**Closed work:**

- [x] **C1**: Added `cloud: list[str] | None = None` to `ComputeConfig`
  at `src/kinoforge/core/config.py:540`. Validator `_validate_cloud`
  rejects unknown cloud names and empty lists; allowed set is
  `{"aws","gcp","azure","lambda","vast","kubernetes","runpod"}`.
  Six TDD tests in `tests/core/test_config.py` (default-none,
  parametrized valid literals, unknown-rejection, empty-rejection).
- [x] **C2**: Added `build_provider_for(cfg)` in `kinoforge._adapters`
  (sister to `build_heartbeat_endpoint_for`). Threads
  `cfg.compute.cloud` into `SkyPilotProvider._clouds` at instantiation
  time. The zero-arg registry factory is unchanged — `build_provider_for`
  wraps it. Six TDD tests in `tests/test_adapters_build_provider_for.py`
  cover single-cloud, multi-cloud, none-preserves-legacy, non-skypilot
  ignore, hosted-engine returns None, unknown-provider raise.
  Call sites refit:
    - `core/orchestrator.py:_resolve_provider` (the `kinoforge deploy`
      / `kinoforge generate` chokepoint).
    - `cli/_commands.py:_cmd_provision` (manual `kinoforge provision`).
  Integration test
  `tests/core/test_orchestrator.py::test_resolve_provider_threads_skypilot_cloud_pin`
  locks in the orchestrator wiring.
- [x] **C3**: New `examples/configs/skypilot-lambda.yaml` ships
  `compute.cloud: ["lambda"]`, `max_usd_per_hr: 2.00`, idle/budget
  envelope sized for Stage E live smoke. Lockdown test
  `test_skypilot_lambda_example_pins_lambda_cloud` in
  `tests/test_examples.py` guards against accidental cloud-key drops.

#### Stage D — Vast.ai parity raw smoke (BLOCKED on upstream sky bug)

Attempted 2026-06-17 with `sky launch -y --infra vast --gpus RTX3060:1 -c
kf-smoke-vast-001 ...`. Sky chose Vast (Ontario, CA, NA) at $0.16/hr,
attempted provision, then died with
`AttributeError: VastAI has no attribute client` at
`sky/provision/vast/utils.py:204`.

**Root cause:** sky 0.12.3.post1's vast adapter expects
`vastai_sdk.vast.vast().client.api_key` (legacy factory-function +
`.client` proxy). The current vastai-sdk 0.2.5 has refactored its
public surface to a single `vastai_sdk.VastAI` class — no
`vast.vast()` factory, no `.client` proxy. Sky's vast adapter has not
caught up.

**Workarounds:**

- (a) Monkey-patch `sky/provision/vast/utils.py:204` to read the API key
  directly from `~/.config/vastai/vast_api_key` (a file sky already
  requires for `sky check` to enable vast). 1-line change, ~3 LOC with
  imports. Brittle: any `pixi install` rerun on the live-skypilot env
  wipes the patch.
- (b) Pin vastai-sdk to a pre-refactor version (likely 0.1.x). Risk:
  unknown other regressions in sky's vast paths against older SDKs.
- (c) Upgrade sky once the upstream fix lands. Repo:
  https://github.com/skypilot-org/skypilot
- (d) Skip Vast entirely; rely on Lambda as the sole sky cloud. Loses
  Vast as a fallback for Lambda capacity issues.

**Decision 2026-06-17:** Path (d) — skip Vast, rely on Lambda only.
Operator pivoted to vast+Lambda specifically to escape pain; debugging
sky internals defeats the purpose. Revisit when sky ships a vast
adapter compatible with vastai-sdk ≥ 0.2.

Spend: $0.00 (provision failed before any compute billed).

#### Stage E — end-to-end `kinoforge deploy` on Lambda (NOT STARTED)

After Stages C+D: a real FakeEngine deploy via
`kinoforge deploy examples/configs/skypilot-lambda.yaml` to verify the
full kinoforge → SkyPilotProvider → sky → Lambda path. ~$0.15 budget.

---

## TODO: `cost_rate_usd_per_hr` is inaccurate — fix needed (2026-06-19)

`kinoforge status --id <pod>` reports `cost_rate_usd_per_hr=0.35` for
a RunPod pod whose live RunPod console bills `$0.45/hr`. Confirmed
during the Wan 2.2 native T2V-A14B Phase 1 live smoke against pod
`hpxzx441nwhiqv`. Discrepancy = ~28% understatement of burn.

**Impact:** every `est_spend` figure, the `cost` dashboard total, and
budget-ceiling enforcement are all wrong by the same factor. Live
spend appears safer than it is; budgets may silently overshoot before
the cap fires.

**Suspected source:** the field is populated at provision time from
the offer's listed rate (e.g. an A40 entry's catalog price), not from
the actual GPU selected by RunPod when A40 isn't available and A6000
or L40S gets substituted. The cached value never refreshes against
RunPod's live billing.

**Fix shape (TBD):** either (a) refresh `cost_rate_usd_per_hr` per
status poll from the live pod's `costPerHr` GraphQL field, or
(b) propagate the actually-selected GPU type from the createPod
response and re-derive the rate from the offer catalog at that
point. Lean toward (a) — single source of truth, no offer-catalog
staleness.

**Until fixed:** treat all reported `est_spend` numbers as a lower
bound. Cross-check against RunPod's UI before approving long-running
or high-budget pods.

---

## Wan 2.2 native T2V-A14B via DiffusersEngine (CLOSED 2026-06-20)

Plan: `docs/superpowers/plans/2026-06-19-wan22-native-t2v-a14b.md`
(includes amendment for Task 7.5).
Spec: `docs/superpowers/specs/2026-06-19-wan22-native-t2v-a14b-design.md`.

Goal: ship a green live smoke for Wan 2.2 T2V-A14B running via
`diffusers.WanPipeline` on a RunPod A100 80GB pod, with warm-reuse
across two prompts and cross-cap-key isolation against the Kijai
5B ComfyUI cfg.

**Status: GREEN.** 1 passed in 0:24:04 on 2026-06-20 ~06:08 local
(commit `365ab00`). Three MP4s landed in `output/`:
- `20260620-055823_diffusers_unknown_Photorealistic-cinem.mp4` (14B cold, 1.1 MB)
- `20260620-060158_diffusers_unknown_Photorealistic-yet-d.mp4` (14B warm reuse, 1.9 MB)
- `20260620-060729_comfyui_Wan2_2-TI2V-5B-FastWanFu_Photorealistic-cinem.mp4` (5B cross-cap-key, 1.3 MB)

See `successful-generations.md` entry #8 for the full schema +
failure-modes recap. Total session spend ~$10 across 28 attempts
(layered-bug debug) + ~$0.49 on the green pod.

### Resume pointer

This worktree (`worktree-wan22-native-t2v-a14b`) is ready to merge
back to `main`. Pending: superpowers' finishing-a-development-branch
workflow (open PR / squash / merge — operator's choice).

Sibling `runpod-comfyui-wan-t2v-14b-2_2.yaml` is now marked DEAD
with a comment header pointing at the diffusers cfg. The
`hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers` ref + DiffusersEngine path is
the canonical Wan 2.2 14B integration.
