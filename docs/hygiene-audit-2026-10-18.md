# Hygiene audit — 2026-10-18 (whole repo, AUDIT-ONLY)

Scope: whole repo. Mode: audit-only — **no code changed**. Seven parallel read-only audit
agents (cli+pipeline+outputs / core part 1 / core part 2 / engines+upscalers / providers+stores+
validation / tests / tools+docs+config), each briefed with `docs/hygiene-notes.md` and the
2026-07-16 audit so prior keeps were not re-litigated. Every P1 claim below, and the highest-stakes
P2s, were re-verified by the controller against HEAD before inclusion (marked ✔).

Baseline at `5f8a52a0`, working tree clean:

- `pixi run pre-commit run --all-files` — **green** (ruff, ruff-format, mypy, merge-conflict,
  large-files, check-toml, scan-secrets).
- `pixi run test` — **6838 passed, 182 skipped, 25 deselected, 6 xfailed, 0 failed** (10 m 48 s;
  317 warnings, dominated by FastAPI's `@app.on_event("startup")` deprecation in the pod servers).
- `pixi run smoke-local` — **58 passed**.

788 commits landed since the 2026-07-16 audit. Size today: `src/` ≈ 68k LOC (core 27k, engines
12.6k, cli 8.8k, providers 6.8k), `tests/` ≈ 174k, `tools/` ≈ 11.7k. 16 functions in `src/` exceed
200 lines; the largest are `cli/_main.py::_build_parser` (726), `core/orchestrator.py::deploy_session`
(598) and `::generate` (572).

---

## Finding #0 — the leak-sweep cron destroys EVERY RunPod pod it sees (P1, live) ✔

`tools/smoke_leak_sweep.py:103-131`, run by `.github/workflows/leak-sweep.yml` (`*/30 * * * *`,
**active** on GitHub, RunPod secrets wired) and by the always-run step of `smoke-wan21-weekly.yml`.

- The age test is `now - pod.created_at` against `RunPodProvider.list_instances()`. That converter
  (`providers/runpod/__init__.py:1971`, `_pod_to_instance`) hard-codes `created_at=0.0` ("RunPod
  list API does not return creation time") and never sets a `smoke_tier` tag. So every pod in the
  account reads as ~497,000 h old and untagged → past the 4 h untagged ceiling → `REAP` →
  `provider.destroy_instance(pod.id)`.
- Scope of the blast radius: ANY pod in the account at sweep time — in-flight generations,
  deliberately warm-reuse pods, `kinoforge provision`ed pods, non-kinoforge pods. Each reap also
  tries to file a GitHub issue titled "reaped after 497882.8h".
- `tests/tools/test_smoke_leak_sweep.py:12-22` hides it: its fakes carry a real `created_at` and
  a `smoke_tier` tag, which the real provider never produces.
- Both halves predate the cron: the `created_at=0.0` converter is from the first RunPod commit
  `9f51d74e` (2026-05-29); the sweep landed `4c76c785`/`1ebd52dc` (2026-06-21).
- **Observed history (GitHub API, read-only) — it HAS destroyed real work.** Scanning the job
  logs of every retained run from 2026-09-11 to now (1000 runs) found exactly two reaps:

  | sweep run (UTC) | log line |
  |---|---|
  | 2026-09-12 21:32 (`34720235233`) | `REAP pod=i5mvj3oxljkyaa tag=None age=497013.54h` |
  | 2026-09-13 04:06 (`34737107594`) | `REAP pod=ag0ntjnovabxqt tag=None age=497020.11h` |

  **These are the two pods U42 (PROGRESS STATUS INDEX, FIXED 2026-09-12 `29c87291`) attributes
  to "RunPod's COMMUNITY pool reclaims pods minutes after create".** The "indirect symptom" U42
  records — the subsequent terminate failing `POD_NOT_FOUND` — is exactly what happens when our
  own cron destroyed the pod first. Both reaps then failed to file their issue (`gh issue create
  exited 1 … could not add label: 'leaked-smoke-pod' not found`), which is why no one saw them.
  U65's pod `178qwug7mlvifd` (2026-09-25) is NOT in the sweep logs — that reclamation has a
  different cause. The 2026-07-03 / 2026-07-09 "host-reclaimed" incidents are older than GitHub's
  90-day log retention and cannot be checked; see the addendum for the 07-19..09-11 window.
  Consequence beyond the money: U42's root cause is misattributed, and conclusions built on it
  (the community-pool reclamation model behind U42's fix; U36's "advertised-vs-realized gap looks
  substantially like a community-pool artifact") should be re-examined. U65's 17-config secure
  pinning is independently evidenced and costs nothing (secure billed cheaper), so it stays.
- All runs since 2026-09-28 listed **zero** pods; GitHub throttles the `*/30` cron to roughly
  every 2-4 h, so a short live run usually slips between sweeps — a long boot or a warm-reuse pod
  does not.
- Same root cause PROGRESS.md records for the reconcile path ("the reaper destroyed every adopted
  instance unconditionally"); fixed there, never here. `tests/_smoke_harness/runpod_lifecycle.py:110`'s
  `tag_filter` can never match for the same reason.

**Immediate action TAKEN 2026-10-18 (operator-approved):** `gh workflow disable smoke-leak-sweep`
→ state `disabled_manually` (reversible: `gh workflow enable smoke-leak-sweep`). The always-run
sweep step inside `smoke-wan21-weekly.yml` is still present but that workflow is dispatch-only, so
it runs only if someone dispatches it — fix the tool before dispatching it. Remaining fix (not
done — audit-only): fix the age source — e.g. derive age from the ledger row's `created_at`, from
RunPod's `runtime.uptimeInSeconds` (already read by the util probe), or skip pods whose
`created_at` is the `0.0` sentinel — and make the test fake use the real `_pod_to_instance`.

---

## Bugs (separate from hygiene; audit-only — none fixed)

### P1 — cost money, destroy work, or corrupt output

| # | Location | Bug |
|---|----------|-----|
| B1 | `tools/smoke_leak_sweep.py:104-127` | Finding #0 above. ✔ |
| B2 | `core/orchestrator.py:2338-2611` (`deploy`) | **`deploy()` never calls `_enforce_rate_cap`** — its only call site is `:1577` on the generate/batch path. Modal's `realized_rate` docstring (`providers/modal/__init__.py:349-358`) says it is "the ONLY thing able to enforce a cap on Modal" because `filter_offers` skips serverless offers. A Modal cfg with `accelerators: [H100]` ($3.95/hr) under the default `max_usd_per_hr=2.20`: `generate` destroys + raises `RateCapExceeded`, **`deploy` books the H100 and returns success**. `cli/_commands._cmd_provision` (a hand copy of `deploy`) has the same gap; SkyPilot is exposed when its pre-launch estimate is unreadable (`skypilot/__init__.py:1229-1233`). ✔ |
| B3 | `engines/diffusers/__init__.py:776-782` | **A rolled-back LoRA swap is reported as success.** The pod catches a `ValueError` from `set_adapters`/`load_lora_weights` (e.g. PEFT "Target modules … not found"), rolls back to the previous stack and returns `done` with `swap_rejected={"reason": "set_adapters_value_error"}` (`wan_t2v_server.py:2185-2228`). The client raises only for `reason == "vram_oom"`; `set_adapters_value_error` has zero consumers in src/tests. `core/lora_apply.py` logs "accepted the N-entry stack" and the render runs on the previous (or empty) stack — the silent-inert-LoRA class frame-QA exists to catch. ✔ |
| B4 | `engines/diffusers/servers/wan_t2v_server.py:1140` (+ `:1241`, `:2205`) | **Wan stores every LoRA flat at `LORAS_DIR/<vendor leaf filename>`** (`core/lora_apply.py:135` passes `pick.filename`; HF uses the path leaf). Two refs sharing a basename (`pytorch_lora_weights.safetensors` is common — `_lora.py:349-353` says so) in one stack: the second download replaces the first, both inventory rows point at one file, the first LoRA silently never applies. Evicting either unlinks the shared file (the sibling check compares refs, not paths). H3's `_lora.py` fixed this with per-ref dirs in `9a3428b7`; Wan never did. ✔ |
| B5 | `engines/comfyui/__init__.py:882-933` | **`status_str == "error"` is not terminal.** `ComfyUIBackend.result` logs the error once and keeps polling until `poll_timeout_s` (default 1800 s) or the stall reaper, billing the pod for a dead job and ending in a misleading `TimeoutError` (the `afe31584` incident: error at 4.3 s, pod ran to the 10-min stall mark). `_logged_error_once` lives on the backend instance, so only the first failing job per backend ever logs its messages. ✔ |

### P2 — correctness / money-adjacent / UX

| # | Location | Bug |
|---|----------|-----|
| B6 | `core/cost.py:29-38` | `_BURNING_VERDICTS` omits `STALL_REAP` and `RESTART_LOOP_REAP` — both are carved out of LIVE (pod alive, billing). `kinoforge cost` reports $0.00/hr burn for a stalled heartbeating pod. The frozenset (`0be90202`) predates both verdicts by a day. ✔ |
| B7 | `core/reaper_actor.py:291-361` | `RESTART_LOOP_REAP` and `DEGRADED_REAP` are in `DEFAULT_APPLY_POLICY` (`reaper.py:68-79`) but `act_on_verdict`'s destroy set is only {IDLE, OVERAGE, ORPHAN, STALL} → both fall to `else: no_op`. A restart-looping pod whose controller has died is classified every sweep and never reaped (in-process HeartbeatLoop self-reap needs the controller alive). DEGRADED is latent today — its only writer, `warm_reuse/integration.py::try_warm_attach_with_swap`, has no production caller. Degraded also short-circuits before the overage check. ✔ |
| B8 | `providers/runpod/__init__.py:131`, `:144`, `:250` | The main RunPod GraphQL transport (`_request`, `_urllib_post_json`, `_urllib_get_json`) calls `urlopen` with **no timeout** (no `setdefaulttimeout` anywhere in src); `_transport.py:51` uses `timeout=10`. A stalled RunPod edge during `podTerminate` / the destroy poll blocks `--no-reuse` teardown forever while the pod bills until selfterm's cap. ✔ |
| B9 | `pipeline/artifact_bytes.py:34`, `:70-74` | Default artifact GET has **no timeout** (diffusers/comfyui results carry `url=`) — a stalled proxy hangs the run while the pod bills. Separately, a uri that is neither a readable local file nor http(s) (an `s3://` `bedrock_video` result; a `file://` whose file is gone) falls through to **fabricated bytes** `b"<filename>\|[]"`, stored and published as the clip with exit 0 (prior NEEDS DISCUSSION #10, now with a reachable path). ✔ |
| B10 | `providers/skypilot/__init__.py:1119,1196` | SkyPilot never applies `EphemeralSession.resource_name` although `--ephemeral` is allowed for comfyui/diffusers on skypilot (`core/ephemeral.py:95-103`). The cluster launches as the raw `spec.run_id` (alias + local timestamp, which STRICT_POLICY forbids reaching the provider) while the CLI's pre-create index row names `kinoforge-<hex>` (`cli/_commands.py:3006`) — for the whole multi-minute `sky.launch` the only durable handle names a cluster that does not exist. ✔ |
| B11 | `providers/runpod/__init__.py:1525` (+ `:1650`, `util.py:134`, `heartbeat.py:145`) | `"errors" in resp` treats a present-but-empty `errors: []` as failure, contradicting `_unwrap_graphql_response`'s own documented RunPod behaviour (`:170-171`). On create: pod booked, then `ValueError("…returned errors:\n")` → run fails, pod bills until reconcile/selfterm. Conditional on that RunPod response shape. ✔ |
| B12 | `cli/_commands.py:1213` + `core/orchestrator.py:2955` | (Backlog **a**, still open) `upscale --scale` ignored on the single `--video`/`--image` path — and worse than filed: the megapixel guard (`:1201`) and `--dry-run` (`:1186`) use the override, the run uses `cfg.upscale.scale`, so a run can exceed `upscale.max_output_megapixels`, the cap that guard exists to enforce. ✔ |
| B13 | `core/orchestrator.py:2490-2501` | `kinoforge deploy --diagnostic-mode` (a deploy-only flag) never passes `diagnostic_env=` to `_build_spec`, so the pod gets `restart_policy=never` + the EXIT trap but no bucket/prefix/creds — a failed boot uploads nothing. The CLI comment at `cli/_commands.py:259-262` claims the overlay is merged. |
| B14 | `core/grid/executor.py:1118-1155` | `_run_swap_group` is dispatched without the semaphore (only `_run_group` acquires it at `:929`) → `--max-parallel-groups 1` with three swap groups cold-boots three pods at once. |
| B15 | `core/grid/executor.py:758-795` | A cell-1 cold-boot failure with action CONTINUE returns `(failed, aborted=False)`; cell 2 then hits `pod.pod_id is None` → `RuntimeError("swap-group invariant…")` escapes `gather`/`run_grid`: no `.cost.json`, no residual probe, traceback (CLI catches only `ValueError`, `:5764`). |
| B16 | `core/grid/executor.py:56` | `_GridStatus` literal `"budget"` is never produced — a tripped budget cap yields `partial` → exit 2, where spec AC8 and the CLI map (`cli/_commands.py:5773`) say exit 3. |
| B17 | `core/sweeper.py:316-329` vs `core/reaper.py:441-449` | Ephemeral STALL_REAP needs `ceil(stall_window_s / heartbeat_interval_s)` samples but samples are banked per **sweeper** tick and the deque is capped by `sweeper.interval_s`. Heartbeat 30 / sweeper 60 / window 300 → needs 10, holds 5 → can never fire; heartbeat 120 / sweeper 60 → fires at half the window. Tests use 30/30. |
| B18 | `engines/diffusers/__init__.py:970` + `core/warm_reuse/integration.py:137-152` | The pod's `rollback_failed` 500 (whose own comment says it exists to make the orchestrator destroy + cold-boot) and `lora_swap_failed` 500 reach the client's unknown-body branch as a bare `RuntimeError`; warm-reuse releases the pod without marking it degraded, so the next attach can reuse a pod whose adapter state the server declared unknown. |
| B19 | `wan_t2v_server.py:254-272` | Documented cfg knobs silently ignored on the pod: `upscale.spandrel.tile_size`/`batch_size` (runtime hardcodes 512/4; `params` discarded), `upscale.flashvsr.tile_size`/`long_video_mode`/`window_size` (hardcoded 0/False/24; `_long_video_mode` assigned, never read — though `long_video_mode: true` does fetch the 4-file bundle). |
| B20 | `pixi.toml:132` | `pixi run test-live` sets `KINOFORGE_LIVE_TESTS=1` itself and `tests/live/conftest.py` auto-loads `.env` → one command books RunPod/Modal/SkyPilot/hosted jobs across ~48 modules with no preflight, no `--no-reuse` discipline, no utilisation polling. README:147 points operators at it. |
| B21 | `sources/{civitai:46,civarchive:55,huggingface:96}` | The three ref-resolve fetches set no timeout and their docstrings claim network failure maps to `KinoforgeError` (raw `URLError` propagates). `LoraRefsResolvableCheck` runs them in the generate preflight → a stalled host hangs `kinoforge generate`. |

### P3 — narrow / latent / misleading

- `diagnostics/c30_probe.py:437-444` — `destroy_with_retry` discards the terminate response and reads an errors-only listing as "pod gone" (the B3-2026-07 class), used by 10 teardown sites in `tests/live/conftest.py`. Live-harness only.
- `providers/runpod/__init__.py:2082` — `RunPodCapacityHintCheck` reads GraphQL data without an errors check → warns "no capacity" instead of "inconclusive".
- `validation/checks/field_support.py:283` — disk message infers CPU from `min_vram_gb == 0`, but `_select_accelerator` returns a GPU whenever one is named → "30 GB for this CPU cfg" where sky pins 60 GB.
- `validation/checks/ledger.py:31-37,129` — `LedgerStaleRowsCheck` registered with an inert loader; `kinoforge doctor` always prints "ledger empty". (Wiring it as written would recommend `forget` for RunPod launching rows — the documented wrong door.)
- `cli/_commands.py:603-604,636-655` — `kinoforge provision` writes the real row before readiness and never forgets it on a post-create failure; on Modal (not reconcilable) the row is permanent and blocks every later `provision`.
- `cli/_commands.py:4574` — `kinoforge destroy` catches `(UnknownAdapter, KeyError)` but not `TeardownError` → traceback; production passes the test-only `sleep=lambda _: None` to `destroy_confirmed`, so the 3 confirm polls run back-to-back against RunPod's eventually-consistent listing.
- `cli/_commands.py:5161` — `kinoforge cost` builds the balance endpoint from `cfg.compute.provider`, not the row's provider → RunPod rows show "balance N/A" under a Modal cfg.
- `core/orchestrator.py:2177-2192` — HeartbeatLoop thread leaks when verify raises `CapabilityMismatch` on the cache-hit cold path (before the `try` whose `finally` stops it). Harmless for the one-shot CLI.
- `core/orchestrator.py:2879-2885`, `:3049-3055` — Ctrl-C WARN says "pod X kept alive … run `kinoforge reap`" even under `--no-reuse`, where the outer `finally` destroys it.
- `core/remote_backend.py:52,58-71` — `_urllib_get_bytes` / `_urllib_delete` have no timeout (and the docstring claims "timeout/UA policy is one decision here").
- `engines/hosted/__init__.py:68,348-366` — 120 × 1 s poll budget hardcoded, no failure-state check (synthetic shim only). `engines/_pod_http.py:139` — `submit_and_poll` has no wall-clock deadline.
- `core/batch.py:609-632` vs `:893-916` — a 0-outcome `_batch_summary.json` IS written when `deploy_session.__enter__` raises, contrary to the docstring.
- `core/grid/executor.py:898` — blocking `_check_no_residual_pods()` (6 × `subprocess.run`, 60 s timeout each, + 5 s sleep) called synchronously inside the async `_run_swap_group` `finally`; `:685-686` hardcodes `gen_wall_time_s=0.0, swap_wall_time_s=0.0` in `.cost.json`.
- `tests/conftest.py:588-603` — the 15 s `faulthandler.dump_traceback_later` is armed at `pytest_configure`, so it fires 15 s into every healthy run (full stack dump on `-s` smokes) and is then spent; the comment claims it fires only if `sessionfinish` is never reached. ✔
- `core/config.py:366` vs `engines/hosted/__init__.py:513-516` — the documented `engine.<x>.auth:` block cannot reach any engine (`HostedEngineConfig` is `extra=forbid` → ValidationError; others `extra=ignore` → dropped), so `_resolve_auth` branch 2 is unreachable and `probe-hosted` can PASS a strategy `generate` never uses.

---

## FIX NOW hygiene (behaviour-preserving, low risk — pending mutation opt-in)

Docs (all behaviour-preserving; most are "the doc is simply wrong"):

- `README.md:83` clones `github.com/emmykillett/kinoforge.git`; the remote is `killett/kinoforge` — the first command a new user runs 404s. ✔
- `README.md:110-133` CLI cheatsheet: missing `image`/`text`; `gc`/`cost`/`sweeper *` omit required `-c`; `sweeper metrics` needs `--prom`; `pod lora ls --id` → positional.
- `README.md:160-161` `smoke-21b-live` is Wan 2.1 **1.3B** not 21B; `smoke-wan22-live` caps at $2.00/module, not "$0.10-0.30".
- `README.md:461` + `docs/configuration.md:34` describe `hosted.yaml` as a provider comparison / fal.ai; it is the user-deployed-shim example.
- `README.md:560`, `docs/troubleshooting.md:41`, `docs/lifecycle.md:365-369` — ComfyUI poll timeout is 1800 s (not 600); `engine.comfyui.poll_interval_s` is not a field (silently dropped).
- `README.md` — still no "Project structure" section (user convention; carried from 07-16).
- `docs/extending.md:15,87-95,131-140` — adapter templates import a never-existing `GpuOffer`, omit abstract methods, wrong `validate_spec` signature.
- `docs/credentials.md:104-117` — documents a nested `auth:` block that fails `extra=forbid`; names non-existent `VeoEngine`/`NovaReelEngine`.
- `docs/engines.md:192,223,336-340` — `generate --init-image` has never existed; quickstart omits required `--mode`. `:13` `engine.type` → `engine.kind`; `:375` `boot_timeout_s` → `boot_timeout`; `:367` "Eleven" Modal configs → 13.
- `docs/engines.md`, `docs/cost-and-spend.md:11-13`, `docs/batch-and-grid.md:11-12`, `docs/warm-reuse.md:127,135` — the README→docs split rewrote shell-command paths to `../` (wrong: `pixi run` runs from root); `warm-reuse.md` cites never-existing `wan21-1_3b.yaml`/`wan22-14b.yaml`.
- `docs/configuration.md:180` — "animated GIFs … contribute their first frame" contradicts `:190-198` (frame-wise since 10-11).
- `docs/output-layout.md:10` — filename schema is `{ts}_[{kind}_]{provider}_{model}_{slug}{ext}`, not `<ts>_<slug>`.
- `docs/warm-reuse.md:360,432`, `docs/breaking-changes.md:312`, `docs/troubleshooting.md:11` — broken anchors.
- `docs/RELEASE-CHECKLIST.md:10-11,24-25` — requires a green "weekly" run of a dispatch-only workflow; hand-tag steps contradict `docs/releasing.md` / `pixi run release`.
- `AGENTS.md:45-57,76` — points contributors at `_CREDENTIAL_PATTERNS` in `tests/providers/conftest_runpod.py` (now an alias of `core/credential_patterns.py`); stale "PROGRESS:213" pointer.
- `pixi.toml:128-131` — `test-live` comment says tests/live "is created in Layer I Task 13"; it holds 77 modules.
- `.github/workflows/smoke-wan21-weekly.yml:13` — `issues: write # post issue on test failure` describes a step that never existed.

Source docstrings/comments (not embedded → no golden moves):

- `core/orchestrator.py:1166-1172,1334-1342,1999-2002,2208-2214` — heartbeat "ticks throughout engine.provision" describes the reverted C29 order; it starts AFTER provision since `a9936bf5`.
- `core/orchestrator.py:176,589,636-640,1331-1332,1708-1710,1778-1784,2575,2647-2661,2752-2753,2835` — ten stale references (callers, `lifecycle.LAUNCH_PHASE_TAG`, "Task 6/5 will…", BudgetTracker teardown, `generate` stage order, "(line 209)"/"(line ~1008)").
- `core/orchestrator.py:1288,1311-1313` — `for_discovery` docstring claims it changes the CapacityError message; unread since `4fe46014`. `:348-446` `_provision_compute_once(store=)` docstring describes a lock that moved in `d6932465`.
- `core/remote_backend.py:58-71,~247` — "timeout/UA policy is one decision here" (sets neither); "Fal subclasses this backend" (it does not).
- `core/grid/executor.py:3-11,1066-1069,1136-1138`, `grid/grouping.py:4-6` — lora-swap "REFUSED when ephemeral" (refusal lifted 2026-09-11, no raise exists), "EVERY cell passes --no-reuse" (swap cells never do).
- `core/reaper.py:660-667`, `reaper_actor.py:79-90` — "one of the seven" verdicts (14 today); `ActionResult.action` list incomplete.
- `core/scale_target.py:3-6,27` — height targets "refused by every v1 consumer" (UpscaleStage resolves them).
- `core/lora_apply.py:402` — "The ONLY except in this module" (a second at `:158`).
- `core/heartbeat_loop.py:252,254`, `core/reaper.py:738,786`, `grid/executor.py:281` — stale `file:line` pointers.
- `cli/_commands.py:1097-1104,1208-1210,2398-2401`, `cli/context.py:50`, `cli/_reconcile.py:3-5`, `outputs/local.py:3`, `pipeline/generate_clip.py:6,75` — stale ("v1 owns dry-run only", "mirrors `_cmd_generate`'s chain", "`_cmd_deploy` writes the mapping", "two callers", old filename schema).
- `providers/runpod/__init__.py:12-20,351-353,457,517-521,714-716`, `selfterm.py:19-21` — cites non-existent sha `67627cd0` (B4 fix is `cfcd552c`); SkyPilot budget arithmetic is controller-side; heartbeat uses a `dockerArgs` marker not tags; serverless presented as working (refused at preflight, U46).
- `providers/modal/__init__.py:85,227,235,241`, `_catalog.py:50`, `providers/local/__init__.py:226` — `reqs` → `placement`; `offer` on spec / `ValueError` → `CapacityError`; scaledown "default 300 s".
- `validation/checks/models.py:5-7`, `capabilities.py:540-541`, `lifecycle.py:16`, `ledger.py:12-15,34-35`, `validation/__init__.py:3`, `field_support.py:121-129,268-272` — stale (`/resolve/main/`, orchestrator line refs, "two entry points", garbled sentence).
- `stores/s3/lock.py:155`, `stores/gcs/lock.py:145` — "best-effort: log in production" — nothing logs.
- `engines/diffusers/__init__.py:142-144,190,1159-1163` — `render_provision` "exec the server as PID 1" (`exec_pid1=False`); `base64 -d` (gzip+python3); stale payload sizes.
- `engines/comfyui/__init__.py:1537`, `engines/fake/__init__.py:249,251` — `profile_for` "deferred to Task 12" (permanent delegation). `image_engines/luma_agents/__init__.py:170-173` — "the live smoke settles which one is real" (settled 2026-07-04). `image_engines/replicate/__init__.py:104,111` — "lands in Task 17".
- `sources/{civitai,civarchive,huggingface}` docstrings claiming network failure → `KinoforgeError` (see B21).
- `tools/validate_scoped_policy.py:28,94,496`, `tools/render_aws_policy.py:216` — drifted line pointers into `cloud_perms_probe.py` (cite symbols instead).
- Tests: `tests/providers/test_skypilot.py:1160-1168,1180` stale T4 skip reason + nonexistent path; `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py:16-19` "marked XFAIL" (removed `4ceb4fdf`); `tests/engines/diffusers/servers/test_set_stack_async_job.py:19-22` cites pytest-randomly (not installed); `tests/core/test_heartbeat_endpoints.py:4-5`, `tests/providers/test_heartbeat_parity.py:31-32` say fakes live in `tests/providers/conftest.py`.

Dead code (Chesterton fences checked by the reporting agent):

- `core/profiles.py:676-749` `image_discover` / `image_resolve_or_discover` — zero references repo-wide (`92b6b16a`).
- `core/config.py:66` `VALID_KIND_TARGETS["lora"]` — unreachable since `dc3c4eba`.
- `core/grid/executor.py:57` `_NO_RESIDUAL_RE` — zero refs since `6b6feb8c`.
- `core/session_claim.py:107-110` — `try: yield / except BaseException: raise` no-op.
- `providers/skypilot/__init__.py:1122` — `if resources:` always true (+ misplaced "Status conversion" header at `:449-456`).
- `validation/checks/{models.py:29-30,image.py:26,46,custom_nodes.py:25}` — `_default_http_head` / `_PASS_CODES` "seam" aliases, zero test refs.
- `tools/_redact.py:31-35` — "backward-compatible" `_CREDENTIAL_PATTERNS` alias, zero importers.
- `cli/_commands.py:5099-5119` — `_cmd_cost`'s `cfg is None` fallback (unreachable: `--config` required) hardcodes thresholds that disagree with `Lifecycle()`; replace with `cfg.lifecycle() if cfg else Lifecycle()` like `:2590`/`:4755`.
- `cli/_commands.py` local re-imports shadowing module imports (`:522,536,619,662,1879,2160,3474,3630,3674,4258-4259,729-732`), `cli/_main.py:192,238`; dead `getattr(lc, "lora_swap_re_probe_after_s", 300.0) if lc else 300.0`; `pipeline/generate_clip.py:177` recomputes `ext`. `core/orchestrator.py:2947,2969,2993` triple local `registry` import.
- Three `noqa` codes that never fire (`orchestrator.py:426` D102, `config.py:2200` F401, `interfaces.py:531` B027 — `ruff --extend-select RUF100`).
- Tests: `tests/providers/conftest.py` (re-export nothing imports); `tests/integration/conftest.py` + `tests/smoke/conftest.py:36-38` (redaction-registry clears subsumed by the root autouse); `tests/providers/conftest_runpod.py:52-58` `_OPERATION_TABLE`; ten unreferenced test helpers (`SequencedEngine`, `HttpGetSpy`, `_CountingSleep`, `_FakeS3`, `_PROBE_*_SCRIPT`, `_swap_argv_assertions`, `_make_arg_capture`, `_record_and_set_tick`, `_spy_downloader`, `_make_vault_no_alias`); `tests/smoke/conftest.py:31` `config.option.log_cli = True` (ini-only in pytest 9).

Redundant (same-reason-to-change copies):

- `cli/_commands.py:2116-2125` — `_cmd_text` holds an exact copy of `_finish_launch_row` (`:1517-1526`); `_cmd_generate:1071-1090` a near-copy (needs the key back). (Backlog **f**, now with a third copy.)
- `cli/_commands.py:1220-1237,1325-1342,1745-1756,2064-2077` — simple attach ladder ×4; `--no-reuse`/`--attach-pod` refusal ×3.
- `cli/_commands.py:270,2776,3013,3296,3709` + `core/orchestrator.py:2082,2086` — `kinoforge_key` width `derive()[:12]` inline ×7 while `orchestrator._key_hash` exists (a matching contract).
- `cli/_commands.py:2380` vs `:4063` — `_reconcile._is_launching` vs `core.launch_phase.is_launching` in sibling commands; `:3179-3181` an undocumented inline fifth copy of the predicate.
- `cli/_commands.py:5402-5694` — `f"sweeper:{host}"` ×7 (+ `core/sweeper.py:289`), `cfg.sweeper.host or socket.gethostname()` ×4.
- `cli/_commands.py:4479` — hand-spelled RunPod proxy host instead of `_PROXY_URL_PATTERN`.
- `pipeline/decimate.py:39-87` ≡ `pipeline/downscale.py:18-76` (seekable-temp ffmpeg runner; the exit-183 fix must change both).
- `pipeline/upscale.py:291-299,340-347` + `pipeline/interpolate.py:96-102` — "is this uri local" ×3.
- `core/orchestrator.py:1447-1456,1872-1879,2468-2471` — engine-facing cfg-dict lifecycle lift ×3, each claiming to be "the single authoritative source".
- `core/orchestrator.py:2312` — `ttl_s=30.0` literal for the `reaper/{id}` lease vs `reaper_actor._LOCK_TTL_S`.
- `core/batch.py:461-923` — `entry_finish` + `BatchOutcome` pair hand-written ×5, `entry.run_id or str(idx)` ×13 (fn is 463 lines, depth 7).
- `core/upscale_dir.py:201-207` vs `:389-397` — seed-state→stage.run→fetch sequence ×2 (fn depth 8).
- `core/reaper_actor.py:322-351` vs `:608-644` — WARN-once SKIP_NO_PROBE/PROBE_FAILED blocks ×2; provider-kind fallback ×7 (and `:280` reads a key the ledger never writes).
- `core/lora_apply.py:364-410` ≡ `core/warm_reuse/integration.py:126-135` — ledger LoRA-inventory write (docstring demands they stay field-for-field identical).
- `core/image_dir.py:390` `_PASSTHROUGH_SUFFIXES` restates `media.IMAGE_SUFFIXES`; `register_openers()` before each of 4 `Image.open` sites.
- `core/grid/executor.py:386-394` ≡ `:524-531` stderr-persist block (both swallow `OSError` with bare `pass`); 5 `GridCellResult(...)` bypass `_cell_result`.
- `core/grid/executor.py:348`, `image_dir.py:394`, `media.py:121-124` — private sha256 helpers duplicating `downloader.sha256_file`. `core/frames.py:133-170` inlines `_default_probe_run`.
- `stores/local.py:218-219`, `stores/{s3,gcs}/lock.py:77-82` (+ `reaper_actor.py:188-189`) — `<prefix>/_locks/<key>.lock` layout ×4; S3/GCS steal-after-TTL check ×2.
- `providers/runpod/__init__.py:821-824,847-851,956-960` — list-pods fetch+parse ×3. `:73`, `util.py:28`, `heartbeat.py:42`, `balance.py:26` — GraphQL URL ×4. `:1801` `grace_s=90.0` restates `_BOOT_GRACE_MIN_S`.
- `providers/modal/_app.py:127` + `modal/__init__.py:332,413,434,554,662` — `"kinoforge-"` app prefix ×6; `:532-534` ≡ `:596-606` live-first partition.
- `sources/huggingface/__init__.py:225,258` + `validation/checks/models.py:49` — HF resolve-URL shape ×3.
- `engines/diffusers/__init__.py:127-133` ≡ `:199-205` — embed gzip+b64 encoding; `:1360-1414` sub-engine composition blocks ×3; `comfyui:1315-1319` ≡ `diffusers:1240-1244` selfterm snippet (goldens pin the bytes, so refactor is safe and verifiable).
- Tests: S4 catalog fakes `_S4_GPU_TYPES` ×8 / `_S4_ACCELERATORS` ×4 / `_create_body` ×5 (one commit, one reason to change); wan-server snapshot/restore autouse fixture ×4 (none covers `_swap_jobs`/`_LOADED`/`_interpolate_jobs`); `_fake_session` ×4 and `_probe_profile` ×4 in orchestrator tests; c29/c33 heartbeat harness clones.

Test strength (behaviour-preserving):

- Six discovery guards with no plausible-count floor (project rule "guard the guard"); three root at CWD-relative paths and pass with zero inputs from another directory; `tests/test_live_gating_lockdown.py:36` scans `tests/live/*.py` non-recursively (misses `tests/stores/live/`, `tests/smoke/live_wan21/`, `tests/smoke/release_wan22/` — all gated today).
- `tests/cli/test_cmd_generate_loras.py:40-51` — `try: _cmd_generate … except Exception: pass` then asserts `call_count == 0` (passes for the wrong reason on any crash).
- `tests/providers/test_runpod_name_resolution.py:216-232` — fake records `<unmatched>` for any id, so a wrong-id destroy passes.
- `tests/integration/test_no_unknown_slug_for_example_configs.py:80-85,100-103` — `except Exception: pytest.skip` converts the failure the guard exists for into a skip.
- `tests/tools/test_smoke_leak_sweep.py:12-22` — fakes a `created_at`/`smoke_tier` the real provider never returns (Finding #0's hiding place).

---

## NEEDS DISCUSSION (operator decisions)

1. **Leak sweep** (Finding #0) — cron disabled 2026-10-18; choose the age source; decide whether the cron should exist at all given `sweeper`/`reap` now cover leaks with ledger context; re-examine U42/U36 conclusions built on the community-pool attribution; create the `leaked-smoke-pod` label (or drop `--label`) so a reap can never again be silent.
2. **Guarded-launch sequence ×3** — `core/orchestrator.py:1446-1548` (generate path), `:2468-2553` (`deploy`), `cli/_commands.py:535-604` (`_cmd_provision`, "reuses deploy's mechanism verbatim") have drifted: B2 (rate cap) and B13 (diagnostic env) and the provision ledger-row ordering all come from the drift. One shared helper would close three bugs at once. High risk (launch/teardown path).
3. **`Lifecycle` → `classify()` kwargs mapping ×5** (`cli/_commands.py:2590-2604,3750-3764,4755-4777,5099-5142`, `core/config.py:2264-2302`) — already drifted (status/warm-resolve omit the ephemeral keys reap passes; `sweeper_thresholds_from_cfg` float-coerces and maps heartbeat 0→None, the CLI copies do not). U20 was this exact defect. Ephemeral keys are only read for index rows, so today's drift is inert for status, but the next knob will not be.
4. **`Lifecycle` defaults defined twice and disagreeing** — `core/interfaces.py:145-160` (`Lifecycle`: stall/restart/orphan windows `None` = disabled) vs `core/config.py:120-138` (`LifecycleConfig`: 600/180/3600 s = enabled). A compute cfg with no `lifecycle:` block gets the disabled set; one with only `budget:` gets the enabled set. Placement has a parity test; Lifecycle has none. The dead-man `3.0 × heartbeat_interval_s` literal also recurs in ≥5 modules with no named constant.
5. **Dead lifecycle layer** (carried, 07-16 #2) — `core/lifecycle.py` `_InstanceState:111`, `LifecycleManager:135-346`, `warm_reuse_or_create:354-391`, `reap:970-1037`, `BudgetTracker:1045-1154`, plus `effective_deadline:36-60` — still zero production callers; ~470 LOC + 8 tests. Collateral: `BudgetExceeded` becomes never-raised (its catch arms in `batch.py:811`, `cli/_commands.py:2303`, `upscale_dir.py:69` are already dead in production), and example-config comments claiming "BudgetTracker tears down mid-run" are false today (`runpod-comfyui-wan-2_1-*.yaml`, `tests/live/cfg_b3_warm_attach.yaml:13`).
6. **Other dead production surface** — `warm_reuse/integration.py:43-152 try_warm_attach_with_swap` (never adopted; sole writer of `status="degraded"` → matcher degraded-skip and DEGRADED_REAP unreachable; the deferral is recorded in `a1892981`); `GenerationEngine.profile_for` abstract with 7 raise-only implementers and no caller; `errors.py` `SetStackRequestRejected:508`, `StageMismatch:614`, `BSACompileFailed:664`, `UnsupportedGpuArch:700` never raised (errors.py is embedded → goldens move); `profiles.py:445-523 resolve_or_discover`/`_discover_single_flight` test-only while `docs/cloud-stores.md:100` says production discovery "uses the lock automatically" — wire it or delete it and fix the doc; `grid/spec.py:70 PathCell`, `grid/errors.py:37 GridBudgetExceeded` (tied to B16), `credential_patterns.py:394 PLACEHOLDER_MARKERS`; RunPod `kinoforge-ephemeral=true` tag written, never read; `orchestrator.py:1288 for_discovery` param (no-op → the hygiene-notes "duplicated provision branch" keep's only distinguishing argument is gone; the two branches are now semantically identical — re-evaluate that keep).
7. **`deploy_session` (598) / `generate` (572) / `_provision_instance_and_build_backend` (412) / `deploy` (274)** (carried, 07-16 #7, grown ~60-100 lines each) — seams listed by the core-1 auditor (ledger/claim closures, heartbeat assembly, backend acquisition, verify, pool + late heartbeat, `--no-reuse` destroy; post-clip stage builders, stage walk + teardown, two materialize helpers). Includes the duplicated `ValidationError`/`(KeyboardInterrupt, Cancelled)` teardown pairs (`:2861-2886` ≡ `:3029-3056`) and the interpolate materialize block hand-inlining `fetch_artifact_bytes` (`:3165-3173`). Also `cli/_commands.py` long commands (`_cmd_provision` 283, `_resolve_warm_instance` 207, `_cmd_batch` 188, `_cmd_text` 185, `_cmd_status` 176, `_cmd_upscale` 165) and `providers/skypilot/__init__.py:1062-1354 create_instance` (293, depth 5, includes a teardown path).
8. **`wan_t2v_server.py` decomposition** (carried, 07-16 #1) — now 2787 lines, ~10 concerns. **New constraint:** RunPod configs embed server modules one by one (`embed_files`, U53), so a sibling-module split edits every RunPod diffusers config's embed list and moves `test_pod_embed_closure` + payload-ceiling baselines on top of the goldens. Batch here the embedded-file docstring fixes (`:1-11,126-133,1129-1130,1541-1547,2462-2463`), the unreachable in-job LRU branch (`:2019-2039`, carried), producer-less `SwapRejectedDetails:908-912` + `VRAMRollbackFailure:642-649` (so `grid/swap_failures.py:47,58` patterns can never match), the dormant cold-boot LoRA path (`:1310-1424`, `KINOFORGE_INITIAL_LORA_STACK_JSON` has no producer), the `disk_full`/`lora_download_failed` detail dicts ×3/×2 and the `except OSError` arm that skips the bootstrap-log warning for HTTP/URL errors, `_torch_build` ×3 across servers (contracts drifted), U50 shutdown retirement applied to wan only (not `minimax_h3_server`/`text_server`), and bare `create_task` results (`:1829,2486,2653`).
9. **Hosted engines bypass `RemoteSubmitPollBackend`** — `fal:368-430`, `hosted:313-367`, `bedrock_video:205-246` hand-roll submit/poll and `del cancel_token` ("deferred to a future task"); `FalBackend._delete`/`manual_cleanup_url` are hooks on a class that does not inherit them, so the "cannot quietly leak" docstring is false.
10. **Upscaler registration `except UnknownAdapter: pass`** (carried, 07-16 #9) — spandrel/flashvsr/rife/seedvr2; the outer `except ImportError` also hides genuine controller-side import bugs; `UnknownAdapter` is the wrong name for "already registered".
11. **`image_engines/{replicate:103-112,luma_agents:184-188}._delete`** raise `NotImplementedError` (ABC says `EphemeralDeleteUnsupportedError`) — reachable only via programmatic `EphemeralSession` (CLI preflight blocks it). Carried 07-16 #13.
12. **RunPod GraphQL failure predicate ×3 spellings** (helper non-empty-list / `"errors" in resp` ×4 / c30 `or []`) — `_unwrap_graphql_response` could move into `_transport.py` to break the util/heartbeat import cycle. Ties to B11.
13. **Ephemeral resource naming** copied into each provider (`runpod:1278-1283,1627-1632`, `modal:277-282`) — belongs where `spec.run_id` is built (`core/spec_builder.py:81`); SkyPilot's missing copy is B10.
14. **Validation re-types provider numbers** (`field_support.py:124-135`, `capabilities.py:197-199`, `lifecycle.py:45`: RunPod 250 GB disk, SkyPilot 60/30 GB, Modal 1800 s, selfterm `2×idle`, dead-man 3×) with no test pinning them to source; doctor text still describes pre-S4 Modal/SkyPilot rate behaviour (`field_support.py:362-437`, `modal/__init__.py:134-138`, `skypilot/__init__.py:750-756` — operator-facing text, so not FIX NOW).
15. **`core/locks.py:96-229 InMemoryLock` vs `stores/_lease.py:87-140`** (carried) — the recorded reason ("core→stores import would invert layering") is inaccurate: `test_core_invariant.py:13-15` allows core→stores and 13 core modules import `stores.base`. The real blocker is a cycle (`stores/_lease.py:22` imports `core.locks.LockToken`). Non-inverting fix: move `_LeaseLockBase` down into `core/locks.py`, subclass it, re-export from `stores/_lease`.
16. **`balance_endpoints.py:35`** — a second `TransportError(Exception)` beside `errors.py:93 TransportError(KinoforgeError)`; `except KinoforgeError` misses balance transport failures.
17. **CLI error boundary** — text/image/`--image-dir`/batch map `KinoforgeError` (incl. `Cancelled`) to a clean line + exit 1; single-file upscale/interpolate catch nothing and generate only `UnknownAdapter` → raw tracebacks. `main()` has no top-level handler. Operator-facing text drift in `_main.py` help (`--loras` "resolver pending", `reap --force-forget` "implies --apply" but exits 4, `--emit-provision-record` lists 5 of 6 keys, `cost` always prints "replicate spend tracking pending B10"). `_rc_to_reason` labels rc=1 `cap-key-drift` (it is ledger-absent).
18. **Pod-side `"http"` endpoint key** — `cli/_commands.py:2869-2871,3632-3634` look up a key no provider emits and work by port insertion order; "the pod server port" is spelled `"8000"` / `_DEFAULT_SERVER_PORT` / a separate `_base_url` in `text_engines/transformers`.
19. **`_cmd_deploy:268-278`** has its own duplicate-key refusal lacking `_cmd_provision`'s dead-row reconcile and recovery advice (the "refuses FOREVER" failure provision's comment describes).
20. **Tests: env + config** — `tests/live/conftest.py:34-42` loads `.env` into every local `pixi run test` (collection imports it before `-m 'not live'` deselects), so the local unit suite runs with real credentials in `os.environ` while CI does not; `pyproject.toml:112-118` has no `testpaths`, `xfail_strict` or `--strict-markers`. Reload-pattern hazard (carried 07-16 #5) still unguarded — 10 `importlib.reload(srv)` sites, 5 files top-level-import from `wan_t2v_server` (safe today: pydantic models/constants only); the ×4 snapshot fixture is the natural home for a shared guard.
21. **Closed-investigation tools purge** (carried 07-16 #3, re-verified):
    - still dead: `quota_burn*.py` (1,157 LOC + 6 CI test files, 1,432 LOC — but B14's README fix now points at it: `README.md:525,563`, `docs/troubleshooting.md:52`, `docs/quota-justification-*.md:4`), `c33_*.py` ×4 (1,229), uptime-sweep family (392; `repro_runpod_uptime.py` stays kept), `build_bsa_wheel.py` (400 + test; still cited as RunPod wheel provenance in `runpod-diffusers-flashvsr-x4-upscale.yaml:40`), `tools/local_hooks/` (1,362 + `tests/hooks/` 522, run by `pixi run test`).
    - **new candidates:** `tools/diagnose_pod_boot.py` (504 LOC, closed June investigation, edited by 5 refactors since July), `tools/debug_ledger_watcher.py` (82 LOC, zero references anywhere).
    - **dropped from the list:** `cloud_perms_probe.py` is alive again (`75f37b74`; `validate_scoped_policy.py:100` imports `_REQUIRED_AWS_ACTIONS`).
    - deps: only `google-cloud-bigquery/-billing/-billing-budgets/-quotas` (`pixi.toml:89-92`) remain dead-tool-only (`-billing` has no importer at all); `-compute`/`-kms`/`-resource-manager` are alive. `lumaai` and `fal-client` (`pixi.toml:244-245`) are imported nowhere (comment "lazy-imported inside engine modules" is false); `httpx` declared twice (conda `:78`, PyPI `:96`). Any dep change forces a `pixi.lock` regen.
22. **CI pin drift** — `leak-sweep.yml`, `smoke-wan21-weekly.yml`, `build-wan-comfyui-image.yml` pin `checkout@v4` / `setup-pixi@v0.8.1` (Node 20, now force-run on Node 24 with a deprecation annotation), reversing `5c6d334b`'s move of `ci.yml` to v6/v0.9.6; `ci.yml` uses `pixi-version: latest`. `smoke-wan21-weekly` name vs dispatch-only.
23. **`pixi.toml:1-27` header** says it is a generated TEMPLATE edited via `groups.toml` (not in repo); deps are edited here. Operator-owned scaffold boilerplate.
24. **`tests/smoke` / live teardown check ×8** — the two literal `kinoforge list` lines CLAUDE.md names are re-encoded in 8 live files (+ `_destroy_ledger_pods` ×2). Live-only, cannot be verified offline.
25. **FastAPI `on_event` deprecation** (317 suite warnings) — pod servers; migrating to lifespan moves goldens. Fold into #8.

---

## Prior-audit (2026-07-16) status roll-up

RESOLVED since 07-16: Finding #0 (suite red), stale set_stack test files (`bda1488f`, `36e875a4`),
AC8 scanner Load/Store precision (`1e907642`), B4 selfterm (`cfcd552c`, operator option 2), B6
`_INTERRUPTIBLE_CMDS` (now covers all 7 `cancel_token` commands incl. text and `--image-dir`), B7,
`outputs/base.py` luma docstring (`c94520dc`), `outputs/local.py` filename dedup (`b83af260`),
README Modal + license + BigQuery (`20d4ef22`, `2a8f1156`), `gc --older-than`, `preflight.py`
docstring, `smoke_leak_sweep` gh returncode (`2ca5253c`), every 07-16 FIX NOW dedup (no
regressions: `_pod_http`, `_LeaseLockBase`, `_head`/`_run_gated`, `_urllib_delete`, `read_util`,
`_create_pod` now 66 lines, all B3 call sites on `_unwrap_graphql_response`).

STILL OPEN: NEEDS DISCUSSION #1 (wan server, grown), #2 (dead lifecycle layer), #3 (tools purge,
partly moved), #5 (reload hazard), #6 (RunPod disk/memory literals — now in
`_build_create_pod_body:1433-1455`; `HardwareRequirements` folded into `Placement`, `disk_gb`
declared UNSUPPORTED and doctor-warned; wiring it is a behaviour change: default 100 < 250), #7
(orchestrator sizes, grown), #8 (render_provision twins — narrowed to the selfterm snippet + boot
tail), #9, #10 (now B9), #11 (`TARGET_TO_SUBDIR`), #12 (spandrel `_fetch_weights`; the config claim
moved to `config.py:679`), #13, #14 (S3 `hasattr(set_retry_config)` test branch), #15 (c30 residue);
unreachable LRU branch; producer-less `SwapRejectedDetails`; `core/locks.py` (see #15 above);
README "Project structure"; successful-generations schema vs spec.

**Provenance correction.** Several SHAs cited in `docs/hygiene-audit-2026-07-16.md` and its
status notes do not resolve in this repo (`95f10eb`, `b314f67`, `366b95b`, `f007391`, `6a32535`,
`bd8817d..66a65ea`, `43ff1ffd`, `bf2a18f1`). The commits that actually did the work, as found by
this pass: pollution `20f32a02`, AC8 `1e907642`, fixture teardown `b4937875`, set_stack port
`bda1488f`, matrix OOM migration `36e875a4`, B6 `e9560bd4`, README license/BQ `2a8f1156`, B4
`cfcd552c`. The 07-16 file is a historical record and is left as written.

---

## Health summary

Still unusually well-documented — most surviving smells carry dated incident rationale, the
07-16 consolidation batch has held with zero regressions, and the suite is green and fast to
trust. The debt has moved, not grown uniformly:

1. **Teardown/cost guards are where the real defects now live.** Five of the seven P1/P2-money
   items are guards that quietly do nothing for one input shape: a sweep whose age source is a
   sentinel, a rate cap on one of three launch paths, a verdict policy whose actor ignores two
   verdicts, a cost aggregator missing two burning verdicts, transports with no timeout on the
   teardown path. Each was invisible to the suite because its test fakes supply what production
   never does (`created_at`, `smoke_tier`) or because only one of the copies is tested.
2. **Copy-drift is the dominant hygiene smell.** The guarded-launch sequence ×3, the
   `Lifecycle`→`classify` mapping ×5, the launch-row stamp ×3, the attach ladder ×4 — each copy
   says "same as X" in a comment, and each has already diverged in a way that produced a bug.
3. **Pod-side LoRA fidelity** — two new silent-wrong-LoRA paths (B3, B4) in the Wan server, the
   same class as 07-16's B2; H3's `_lora.py` already solved B4 and the "temporary" duplication
   between them is drifting.
4. **Docs drifted after the README→docs split** — wrong clone URL, a nonexistent flag, broken
   templates, wrong defaults. All cheap and safe to fix.

Highest-value order: ~~disable the leak-sweep cron~~ (done 2026-10-18) → fix the sweep's age
source before re-enabling (Finding #0) → B2 rate cap / unify the launch
sequence → B3 + B4 (silent LoRA) → B5 (ComfyUI error-polling) → B8/B9 timeouts on teardown and
artifact paths → B6/B7 cost + reaper verdicts → the docs FIX NOW batch → the rest of FIX NOW.

---

## Addendum — leak-sweep log scan, full retained window

Method (read-only GitHub API): for every `smoke-leak-sweep` run, fetch the sweep job's log and
grep `REAP pod=|OK pod=|failed to list|destroy failed|gh issue create`. GitHub retains Actions
logs for 90 days, so the earliest checkable run is 2026-07-19; the workflow has 3849 runs in
total since 2026-06-21.

| window | runs with a log | pods listed | reaps |
|---|---|---|---|
| 2026-07-19 → 2026-09-11 | 2112 | 0 | 0 |
| 2026-09-11 → 2026-09-28 | 800 | 2 | **2** — `i5mvj3oxljkyaa`, `ag0ntjnovabxqt` (the U42 pair) |
| 2026-09-28 → 2026-10-18 | 200 | 0 | 0 |

A spot-check of an August log confirms the sweep step ran (`Pixi task (smoke-leak-sweep)` line
present) and printed nothing, i.e. the listing was empty at that sweep. One run in the window
concluded `failure` (no `failed to list` line, so not the tool). So within the retained window the
cron destroyed exactly two pods, both mid-boot, both misattributed to RunPod. Everything before
2026-07-19 — including the 2026-07-03 and 2026-07-09 "host-reclaimed" incidents — is unverifiable
either way.
