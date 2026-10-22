# Hygiene re-audit 2026-10-21 — providers / stores / validation / sources / diagnostics

HEAD `f89eff2b`, read-only. Every file in the area was read in full (48 files, 13,254 LOC);
every network call was enumerated (A.1). Line numbers are HEAD line numbers.

## A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line, ≤2 lines) |
|---|---|---|---|
| Finding #0 / B1 (provider half) | `_pod_to_instance` hard-codes `created_at=0.0`; no listing path carries an age | CONFIRMED (+answer) | `providers/runpod/__init__.py:1971` `created_at=0.0,  # RunPod list API does not return creation time`; `_LIST_PODS_QUERY:1757-1759` selects only `id name desiredStatus imageName costPerHr`. A real per-pod age DOES exist in the provider: `probe_runtime():1084-1153` → `RuntimeProbe.container_uptime_s` ← `util.py:155-157` `runtime.uptimeInSeconds` (one GraphQL read per pod; `None` while `runtime` is null in early boot; util cache ~15-30 s). `list_instances` cannot carry it without widening the query. Same 0.0 sentinel on Modal `:419` and SkyPilot `:552`, so the sweep's age test is broken on every provider (cross-area). |
| B8 | main RunPod transport `urlopen` with no timeout | CONFIRMED | `:131` `urlopen(req)`, `:144` `urlopen(url)`, `:250` `urlopen(req)` — no `timeout=`; `rg -n setdefaulttimeout src/` → 0. Full enumeration in A.1. |
| B10 | SkyPilot never applies `EphemeralSession.resource_name` | CONFIRMED | `providers/skypilot/__init__.py:1119` `"name": spec.run_id or "kinoforge-skypilot"`, `:1196` `cluster_name: str = spec.run_id or "skypilot-cluster"`; `rg -c 'EphemeralSession\|resource_name' skypilot/__init__.py` → 0. |
| B11 | `"errors" in resp` treats `errors: []` as failure (4 sites) | CONFIRMED | `:1526-1528` `if "errors" not in resp: return` then `raise ValueError(assembled)` on an empty list (message ends `returned errors:\n`); `:1650` (serverless arm, kept); `util.py:134`; `heartbeat.py:145`. Helper `:186-188` uses truthiness; its docstring `:170-171` records the empty-list shape as observed. |
| B21 | sources fetches set no timeout; docstrings claim `KinoforgeError` on network failure | CONFIRMED | `civitai:53`, `civarchive:62`, `huggingface:100` `urlopen(req)` with no timeout; each catches only `HTTPError` (`:55`/`:64`/`:102`) so `URLError` escapes raw, contradicting `:46`/`:55`/`:96` "or network failure". Reached from preflight via `validation/checks/loras.py:318-320`. |
| P3 c30 `destroy_with_retry` | discards terminate response; errors-only listing reads as gone | CONFIRMED | `diagnostics/c30_probe.py:439` `client.execute(_TERMINATE_MUTATION, …)` result unused; `:441-443` `(listing.get("data") or {}).get("myself", {}).get("pods") or []` → `[]` on `data: null` → `return n`. |
| P3 `RunPodCapacityHintCheck` | reads `data` without an errors check → "no capacity" instead of inconclusive | CONFIRMED | `:2082` `types = (resp.get("data") or {}).get("gpuTypes", [])`; an HTTP-200 errors-only body → `:2093` `passed=False` "no current capacity on any preferred GPU". Only the *raised* path `:2075-2080` says "inconclusive". |
| P3 `field_support.py:283` | disk message infers CPU from `min_vram_gb == 0` | CONFIRMED | `:282` `is_gpu = placement.min_vram_gb > 0`; `skypilot/__init__.py:1033-1034` returns `placement.accelerators[0]` before any VRAM test. `accelerators: [A100], min_vram_gb: 0` → "30 GB for this CPU cfg" while sky pins 60. |
| P3 `ledger.py:31-37,129` | `LedgerStaleRowsCheck` registered inert; doctor always "ledger empty" | CONFIRMED | `:129` `register(LedgerStaleRowsCheck())` → `_empty_ledger_loader:28-34`; `rg LedgerStaleRowsCheck src/` → ledger.py only; `:77` `message="ledger empty"`. |
| FIX NOW docs runpod `:12-20`, `:714-716` | serverless presented as working | CONFIRMED | `:12-16` "Serverless mode: … Reports status=ready immediately", `:714-716` same; refused at preflight by `validation/checks/unimplemented_mode.py:40-42` (U46). The code keep stands. |
| FIX NOW docs runpod `:351-353` | "SkyPilot … its watchdog does this arithmetic on the instance" | CONFIRMED wrong | `skypilot/__init__.py:1140-1151` computes `deadline_epoch = watchdog.compute_deadline(...)` CONTROLLER-side at create (stand-in rate = `placement.max_usd_per_hr`); the instance only enforces the precomputed epoch (`watchdog.py:93-99`). |
| FIX NOW docs runpod `:457` | cites non-existent sha `67627cd0` | CONFIRMED | `git cat-file -t 67627cd0` → "Not a valid object name"; `cfcd552c` exists: "fix(runpod): make the selfterm cap say what it does (audit B4)". |
| FIX NOW docs runpod `:517-521` | heartbeat "reads the pod's own tags off the GraphQL API" | CONFIRMED wrong | `heartbeat.py:1-9` appends `# _kinoforge_hb:<ISO>` to `dockerArgs`; `_read_dockerargs:160-179`; `write():181-227` is a no-op since C33-m. |
| FIX NOW docs `selfterm.py:19-21` | "the orchestrator's heartbeat writes to RunPod pod tags" | CONFIRMED wrong | same evidence; write path disabled (`heartbeat.py:13-18`). |
| FIX NOW docs modal `:85` | scaledown "default 300 s" | PARTIAL | `_app.py:34` dataclass default is 300, but `__init__.py:296` always passes `int(spec.lifecycle.idle_timeout_s)` (7200 by default) — the 300 never applies in production. |
| FIX NOW docs modal `:227,:235,:241` | `reqs`; `offer` on spec; `ValueError` for a missing offer | CONFIRMED | `:227` "meeting ``reqs``"; `:235` "(image, offer, setup_steps, launch, env)"; `:241` "ValueError: If … ``offer`` is missing" vs `:256-261` `raise CapacityError(...)`. |
| FIX NOW docs `_catalog.py:50`, `local/__init__.py:226` | `reqs` → `placement` | CONFIRMED | `_catalog.py:50` "filtered/ordered per ``reqs``"; `local:226` "satisfy ``reqs``". |
| FIX NOW docs `models.py:5-7` | `/resolve/main/` | CONFIRMED | `:7` vs `:49` `f"…/resolve/{revision}/{path}"`. |
| FIX NOW docs `capabilities.py:540-541` | stale orchestrator line refs | CONFIRMED | cites `core/orchestrator.py:2008-2022` / `:2031-2035`; `instance=session.instance` is now at `:2956`, `:2976`, `:3014`. |
| FIX NOW docs `lifecycle.py:16` | "interfaces.py:94" | CONFIRMED | `grace_after_session_s` default lives at `core/interfaces.py:158`. |
| FIX NOW docs `ledger.py:12-15,34-35` | "CLI wiring (Task 10/11)" | CONFIRMED | no wiring exists (P3 row above). |
| FIX NOW docs `validation/__init__.py:3` | "Two entry points" | CONFIRMED | three exported `:30-32`; `validate_for_load:155`. |
| FIX NOW docs `field_support.py:121-129,268-272` | garbled sentences | CONFIRMED | `:127-129` unclosed "(the S4 replacement … which it does exactly when"; `:268-270` "returns None (pre-S4: … offer)\n offer (no ``gpu_type``…". |
| FIX NOW docs `stores/s3/lock.py:155`, `gcs/lock.py:145` | "best-effort: log in production" — nothing logs | CONFIRMED | s3 `:153-155` comment only after `raise` guard; gcs `:143-145` `pass`; neither file imports `logging`. |
| dead: skypilot `:1122` `if resources:` | always true | CONFIRMED | `:1103-1113` always sets `cpus`+`memory` or `accelerators` first. Fence `afe4d956` ("synthesise CPU offer + build sky.Task from dict") — no stated reason to keep the guard. |
| dead: skypilot `:449-456` header | misplaced "Status conversion" | CONFIRMED | `:449-451` header sits above the SSH-tunnel section `:453-456`; the status code is `:514-556`. |
| dead: validation seam aliases | `_default_http_head` / `_PASS_CODES` aliases, zero test refs | CONFIRMED (fence noted) | `rg -n '_default_http_head\|_PASS_CODES' tests/` → 0 hits (the 8 doc hits are the two prior audits + a 2026-06-18 plan). Introduced `a2daaacd`: "module-level aliases preserve the existing injectable-seam names" — nothing references those names. `custom_nodes._PASS_CODES:23` is a distinct frozenset (keep), not an alias. |
| redundant: runpod list-pods ×3 | fetch+parse ×3 | CONFIRMED | `:821-824`, `:847-851`, `:956-960` identical two-liners. |
| redundant: GraphQL URL ×4 | | CONFIRMED | `__init__.py:73`, `util.py:28`, `heartbeat.py:42`, `balance.py:26`; `balance.py` is a keep; no test imports the constants (`rg _DEFAULT_GRAPHQL_URL\|_DEFAULT_BASE_URL tests/` → 0). |
| redundant: `:1801` `grace_s=90.0` | restates `_BOOT_GRACE_MIN_S` | CONFIRMED | `:1801` default vs `:309`; production passes it explicitly `:1175`; tests pass it as a kwarg (`test_runpod_boot_liveness.py` ×2). |
| redundant: modal prefix ×6 + live-first ×2 | | CONFIRMED | `_app.py:127`, `__init__.py:332,413,434,554,662`; partition `:532-534` vs `:596-606` (second also filters `isinstance(r, dict)`). |
| redundant: HF resolve-URL ×3 | | CONFIRMED | `huggingface:225`, `:258`, `models.py:49`. |
| redundant: stores lock layout ×4, steal ×2 | | CONFIRMED | `local.py:219`, `s3/lock.py:80`, `gcs/lock.py:81` (+ `core/reaper_actor.py:189`); expiry predicate `now <= expires_at` at `s3/lock.py:113` and `gcs/lock.py:116`. |
| ND #12 | GraphQL failure predicate ×3 spellings | CONFIRMED | helper truthiness `:186`; `"errors" in` ×4; c30 `or []` at `:340`, `:766`. |
| ND #13 | ephemeral naming per provider | CONFIRMED | `runpod:1278-1283` (`_create_pod`), `:1627-1632` (`_create_serverless`), `modal:277-282`; skypilot absent (= B10). |
| ND #14 | validation re-types provider numbers | CONFIRMED | `field_support.py:124` 250, `:131-132` 60/30, `:136` 1800; `capabilities.py:197-199` `2.0 * idle_timeout`; `lifecycle.py:45` `3 * hb`; `test_field_support_check.py:110` pins Placement defaults only. |
| ND #15 (stores side) | cycle via `stores/_lease.py:22` | CONFIRMED | `_lease.py:22` `from kinoforge.core.locks import LockToken`; `core/locks.py:96-229` `InMemoryLock` re-implements the acquire/`__enter__`/`__exit__` policy that `_LeaseLockBase:87-140` owns. |
| ND #16 | second `TransportError` | CONFIRMED | `balance.py:22` imports `TransportError` from `core.balance_endpoints`, not `core.errors`. |
| keep: `nothing_booked_errors() -> ()` ×3 | required by parity test | CONFIRMED | `tests/core/test_provider_abc.py:83`. |
| keep: `_create_serverless` | golden-covered, refused at preflight | CONFIRMED | golden `runpod-diffusers-serverless.json` exists; `unimplemented_mode.py:40-42`. |
| keep: selfterm `idle_timeout` | B4 option 2, deliberate | CONFIRMED | `selfterm.py:17-30`. |
| keep: gzip `:1365-1377` | | CONFIRMED | `:1362-1375` "Do NOT drop this gzip". |
| keep: `_pod_ids_matching` `:936-940` | | CONFIRMED | `:935-940`. |
| keep: `PreLaunchRateCapExceeded` `:259-263` | | CONFIRMED | `:263-267`. |
| keep: `_SKY_CUDA_UNKNOWN`; `_selection_tags` single try | | CONFIRMED | `:185-198`; `:1589-1595`. |
| keep: modal `max_containers=1` | | CONFIRMED | `_app.py:147-160`. |
| keep: custom_nodes pass codes | | CONFIRMED | `:21-23`. |
| keep: `local_lock` sidecar never unlinked; `_blocking_none_message` | | CONFIRMED | `local_lock.py:126-131`; `_lease.py:41-44`, `gcs/lock.py:45`. |
| keep: heartbeat.py write residue; balance.py third closure | | CONFIRMED | `heartbeat.py:48,118`; `balance.py:5-6`. |

### A.1 Network-call enumeration (timeout / retry / error-shape), whole area

| call site | timeout | retry | error shape handled |
|---|---|---|---|
| `runpod/__init__.py:131` `_urllib_post_json` (no-key seam) | none | none | none (raw `HTTPError`/`URLError`) |
| `:144` `_urllib_get_json` | none | none | none |
| `:250` `_request` (authed post+get: catalog, **create**, get, list, **stop**, **terminate**, destroy poll, capacity hint) | none | 3× on 502/503/504, backoff 2/5/10 s — applies to mutations too (B-1) | HTTPError body echoed to stderr; `URLError` not caught; GraphQL `errors` left to callers |
| `:1860` `_fetch_bootstrap_log_tail` | 10 | none | any → `None` |
| `_transport.py:51` (util + heartbeat) | 10 | none | HTTPError/URLError/JSON → `TransportError`; `errors: []` → `TransportError` (B11) |
| `balance.py:49` | 10.0 | none | URLError/JSON → balance `TransportError` |
| `selfterm.py:122` (in-pod DELETE; template is golden-pinned) | none | every 15 s tick | all swallowed |
| `modal/util.py:44` | 10 | none | HTTPError → status; `URLError` raw (caught by `probe_runtime`) |
| `modal/_app.py:198` `app.deploy()` + `get_web_url()` | none | none | raw |
| `_app.py:207` `modal app stop` | 120 s | none | `CalledProcessError` → `RuntimeError`; `TimeoutExpired` raw → `TeardownError` at `__init__.py:565` |
| `_app.py:221` `modal app list --json` | 60 s | none | raw (callers degrade) |
| `skypilot/__init__.py:1246` `sky.launch`; `:1370/:1395/:1549` `sky.status`; `:1623` `sky.down` | none (sky's own) | `retry_until_up` only | raw; `_launched_resources` swallows |
| `:495` ssh tunnel `Popen` | n/a | n/a | spawn fault → `ProvisionFailed` |
| `_estimate_hourly_rate:1483` | 20 s (daemon thread) | none | any → `None` |
| `watchdog.py` in-instance `run_command` | 120 s | 15 s tick | logged |
| `validation/checks/_head.py:32` | 5 | none | HTTPError → code; others raise → callers log "inconclusive" |
| `sources/{civitai:53, civarchive:62, huggingface:100}` | none | none | `HTTPError` only (B21) |
| `stores/s3` boto3 | SDK default | `max_attempts=3, standard` | `NoSuchKey` / `ClientError` codes |
| `stores/gcs` | `_GCS_RETRY` deadline 30 s on store ops; none passed on lock ops | SDK | `NotFound` / `PreconditionFailed` |
| `diagnostics/c30_probe.py` `client.execute` | client-owned (live harness) | `destroy_with_retry` 5× | `or []` ×2; terminate result discarded |

RunPod create/terminate/list body cross-check: create `input` keys `cloudType gpuCount volumeInGb containerDiskInGb minVcpuCount minMemoryInGb gpuTypeId name imageName dockerArgs ports volumeMountPath env` (`:1431-1477`); list selects `id name desiredStatus imageName costPerHr` (`:1757`); get selects `id desiredStatus imageName costPerHr` (`:1876-1878`); terminate `podTerminate(input:{podId})` (`:1905`); stop `podStop(input:{podId})` (`:1891`). `name` written at create is read back on list (S5 adoption) — consistent; on get, `name` is absent so `tags["name"]` is omitted (documented `:1947-1953`). No drifted key names found. c30's `_LIST_PODS_QUERY:412` selects `id` only, which is all it uses.

Lock-semantics cross-check (stores): S3 and GCS agree — payload `{nonce, expires_at = now + ttl}`, steal only when `now > expires_at` (`s3:113`, `gcs:116`), conditional delete on the captured ETag/generation, release conditional on the held token + ETag/generation, silent on stolen. `FileLock` records the same payload (`local_lock.py:86-90`) but never steals (fcntl owns exclusion, documented `:9-14`); `_LeaseLockBase:95-128` owns the blocking/timeout loop for all three. `tests/stores/test_acquire_lock_contract.py` claims only the ABC-level shape; the steal tests (`test_s3_lock.py:76`, `test_gcs_lock.py:76`) are cloud-only. No disagreement found between the implementations and what the tests claim.

Validation pass/fail/inconclusive mapping: `image.py:66-73` and `RunPodCapacityHintCheck:2075-2080` map a *raised* transport failure to `passed=True, WARN, "inconclusive"`; `models.py:103-109` and `custom_nodes.py:67-74` `continue` past a raised failure (pass); `loras.py:340-348` maps a transient to pass "inconclusive". The only "warn/fail instead of inconclusive" case found is the capacity hint's HTTP-200 `errors` body (prior P3, confirmed) plus B-5 below (misleading pass text).

## B. New bugs (missed by the prior audit)

**B-1 — P2 (money-adjacent; conditional on gateway behaviour).** `providers/runpod/__init__.py:240-262` + `:275-283`. `_request` re-sends the SAME request on HTTP 502/503/504, and `authed_post` is the closure `_create_pod:1289` sends `podFindAndDeployOnDemand` through. Scenario: the create reaches RunPod and books a pod; the edge returns 504 while the response is in flight (the 2026-07-05 incident shows the create path does emit raw 5xx); `_request` sleeps 2 s and re-sends the identical mutation → a second pod is booked under the same `name`; only the second id is returned and registered in `_created_instances`; the first bills until selfterm (≤ 4 h at defaults). Knock-on: the provisional ledger row (keyed by name) now matches two pods, so `_pod_ids_matching:920-968` returns two candidates and a by-name `destroy_instance` raises "ambiguous" (`:985-991`). The same closure retries `podTerminate` / `podStop`, which is harmless. Introduced `64c3fde0` ("retry GraphQL on 5xx"), whose message reasons only about the `get_instance` poll loop; no test or comment considers the mutation case (`rg -i 'idempot|504|duplicate' providers/runpod/__init__.py tests/providers/test_runpod*.py` → destroy-idempotency only).

**B-2 — P2 (correctness / money).** `providers/runpod/__init__.py:341-378` `_budget_bounded_lifetime` + `selfterm.py:81-85`. The budget-derived `max_lifetime` handed to `selfterm.RENDER` (`:1339-1343`) still has `time_buffer` (default 1800 s) subtracted on the pod: `effective_deadline = start + budget/rate·3600 − time_buffer`. Scenario: `compute.lifecycle.budget: 0.25` on an A40 secure at $0.40/hr → lifetime 2250 s → effective deadline start + 450 s → selfterm kills the pod ~7 min into a 25-min Wan cold boot, every attempt, having spent ≈ $0.05 and produced nothing; any budget below `rate × 0.5 h` terminates on the FIRST 15 s tick. The doctor line (`capabilities.py:200-209`) says "further shortened at create by budget … divided by the booked offer's rate" and never mentions the buffer; `docs/lifecycle.md:203-209` likewise. `tests/providers/test_runpod_budget_deadline.py:49,169,172` pin the behaviour with `time_buffer=0` only, so the suite cannot see it. Introduced `ddddea53` ("close U51 — lifecycle.budget now bounds a pod in dollars").

**B-3 — P3.** `providers/runpod/__init__.py:973-975` + `:1009-1013`. `destroy_instance` is documented "Idempotent: if the pod is already gone (empty response) the call succeeds immediately", but RunPod answers `podTerminate` on a gone pod with a GraphQL `errors` body (`POD_NOT_FOUND`, live-recorded in `PROGRESS.md:3260`, `:4752`), which `_unwrap_graphql_response` at `:1013` raises as `RunPodGraphQLError` before the poll loop. Scenario: host reclaims the pod mid-run; the `--no-reuse` teardown (or `kinoforge destroy --id`) raises instead of the documented clean exit. `tests/providers/test_runpod_name_resolution.py:203-213` and `tests/providers/test_runpod.py:853` fake a terminate that returns NO errors for an unknown id, i.e. the suite asserts the opposite of the live shape.

**B-4 — P3 (cannot verify offline).** `stores/s3/lock.py:100-103`. Only `PreconditionFailed` counts as contention; S3 documents `409 ConditionalRequestConflict` when two conditional writes on one key race. Scenario: two reapers take `reaper/<id>` in the same instant → the loser's `put_object(IfNoneMatch="*")` gets 409 → the `raise` escapes `acquire()` as a raw `ClientError`, breaking the `None` / `LockTimeout` contract `_lease.py:95-128` promises. No test, doc, or comment mentions 409 (`rg -i 'ConditionalRequestConflict|409' src/kinoforge/stores tests/stores` → 0).

**B-5 — P3 (misleading text).** `validation/checks/models.py:101-124`. When every HEAD raises (`:103-109` `continue`), the result is `passed=True` with "N ref(s) probed; all reachable", where N counts refs that were never probed. User-facing text, so recorded, not proposed as an edit.

Checked and found clean: Modal destroy "gone" semantics (`__init__.py:596-614`) and app-id resolution; SkyPilot destroy tunnel cleanup (`:1615-1631`) and create-failure teardown gating; `_classify_capacity_error` propagation; `_create_with_offer_retry` non-capacity short-circuit; S3/GCS steal + TTL arithmetic; `_LeaseLockBase` deadline check; `FileLock` fd/flock lifecycle and token matching; GCS retry on store ops; `LoraRefsResolvableCheck` uncertainty mapping; `watchdog.compute_deadline` zero-rate/zero-budget guards and the F3 backstop arithmetic; `RunPodBootLivenessProbe` error degradation; Modal `realized_rate`; RunPod `realized_rate` None contract; `_restart_policy_supported` fallback.

## C. New hygiene findings

| # | location | category | behaviour-preserving? | risk | call | rationale |
|---|---|---|---|---|---|---|
| C-1 | `runpod/__init__.py:1801` | magic | yes (same value) | low | FIX NOW | `grace_s: float = 90.0` restates `_BOOT_GRACE_MIN_S:309`; tests pass the value as a kwarg, not via the default. |
| C-2 | `skypilot/__init__.py:1122`; `:449-451` | dead; documentation | yes (dedent; comment move) | low | FIX NOW | guard always true (`:1103-1113`); header misplaced above the tunnel section. |
| C-3 | `validation/checks/{models.py:29-30, image.py:26,46, custom_nodes.py:25}` | dead (compat shim) | yes (module-internal names) | low | FIX NOW | 0 external refs; `a2daaacd`'s "preserve seam names" reason is moot. Keep `custom_nodes._PASS_CODES:23`. |
| C-4 | `validation/checks/loras.py:446` | redundant | yes | low | FIX NOW | `except (UnknownAdapter, Exception)` — the subclass adds nothing and reads as if it narrowed. |
| C-5 | `diagnostics/c30_probe.py:197-199,449-451,583,598,626,634` | documentation | yes | low | FIX NOW | six line pointers into `engines/comfyui/__init__.py` drifted (anchors now `:1201,:1281,:1337,:1354,:1400`); cite symbols. |
| C-6 | `skypilot/__init__.py:752-756` | documentation | yes | low | FIX NOW | "stays UNSUPPORTED until S4 wires the realized-rate check" — S4 landed (`realized_rate:1403`, `PreLaunchRateCapExceeded:1270`); the row stays `u` because the optimizer never sees the cap. |
| C-7 | `runpod/__init__.py:821-824, 847-851, 956-960` | redundant | yes | low | FIX NOW | one `_list_pods()` helper (prior redundant row). |
| C-8 | `modal/_app.py:127` + `modal/__init__.py:332,413,434,554,662` | redundant / magic | yes | low | FIX NOW | `"kinoforge-"` app-name prefix as one constant (prior redundant row). |
| C-9 | `stores/s3/lock.py:113` ≡ `gcs/lock.py:116` | redundant | yes | low | FIX NOW | expiry predicate into `_LeaseLockBase` (prior redundant row). |
| C-10 | `sources/huggingface/__init__.py:225,258` + `validation/checks/models.py:49` | redundant | yes | low | FIX NOW | `hf_resolve_url()` in sources; models.py already imports `_parse_hf_ref` from there. |
| C-11 | `modal/__init__.py:532-534` vs `:596-606` | redundant | yes | med | NEEDS DISCUSSION | the second site also drops non-dict records; a shared helper moves the type filter. |
| C-12 | `runpod/__init__.py:73`, `util.py:28`, `heartbeat.py:42` | redundant | yes | low | NEEDS DISCUSSION | `_transport.py` is the natural home, but `balance.py:26` is a documented keep — decide the import direction. |
| C-13 | stores lock layout ×4 | redundant | yes | med | NEEDS DISCUSSION | fourth site is `core/reaper_actor.py:189` (cross-area); local is a `Path`, cloud a key string. |
| C-14 | `runpod/__init__.py:91-93` `_RUNPOD_SCHEMA_SIDECAR = Path("tests/live/…")` | structural | no (which file is read) | med | LEAVE | CWD-relative read of a test fixture by production code; today a miss and a hit both yield "unsupported", so it is inert. Note only. |
| C-15 | `diagnostics/c30_probe.py:759` `_ = int(sidecar.get(...))` | dead | no (drops an accidental `int()` validation) | low | LEAVE | cosmetic. |
| C-16 | `modal/util.py:70-73` docstring | documentation | yes | low | LEAVE | omits that `URLError` escapes the default GET raw; the only caller catches `Exception`. |
| C-17 | `sources/civitai:51`, `civarchive:58` UA `kinoforge-smoke/0.1` | naming | no (wire header) | low | LEAVE | production sources identify as "smoke". |
| C-18 | `stores/base.py:131-134` "after expiry another acquirer may steal" | documentation | yes | low | LEAVE | true for S3/GCS, never for `FileLock` (`local_lock.py:9-14`); "may" is defensible. |
| C-19 | `runpod/__init__.py:973-975` destroy docstring "Idempotent…" | documentation | depends on B-3 | — | NEEDS DISCUSSION | the fix is a behaviour decision, not a doc edit. |

## D. FIX NOW edit list

Embedded status: **no file in this area is in any `embed_files`/`embed_modules` list** (`rg providers/|validation/|stores/|sources/|diagnostics/ examples/configs tests/providers/test_pod_embed_closure.py` → config comments only). Golden-pinned text in the area: the `_TEMPLATE` body of `selfterm.py:46-150` (23 runpod goldens + `sweeper.json`), the `watchdog.py` templates `:106-290`/`:330-380` (5 skypilot goldens), and `_app.py`'s boot payload (13 modal goldens). None of the edits below touches those; run `tests/providers/test_launch_payload_goldens.py` after D-26/D-30 anyway. For every item the rg command was `rg -c --fixed-strings '<old text>' tests docs README.md`; hit counts quoted are after excluding the two prior-audit docs and historical `docs/superpowers/*` plans/specs (which quote old text as history, never as an assertion).

### Docs-only items

- **D-1** `providers/runpod/__init__.py:12-16`; embedded: no. old
  ```
  Serverless mode:
    - Creates a serverless endpoint via the RunPod GraphQL API.
    - Sets concurrency caps (``max_workers``, ``max_in_flight``) and a
      per-request deadline from the ``Lifecycle``.
    - Reports ``status="ready"`` immediately (serverless is ready on creation).
  ```
  new
  ```
  Serverless mode (NOT runnable — refused at preflight, U46):
    - ``_create_serverless`` is retained only so ``runpod-diffusers-serverless.yaml``
      stays covered by the launch goldens. Its mutation is invalid GraphQL, and
      ``validation/checks/unimplemented_mode.py`` refuses ``compute.mode: serverless``
      before any create is attempted.
  ```
  rg `'serverless is ready on creation'` → 0. Tests: `tests/providers/test_runpod.py`, `tests/validation/test_serverless_unimplemented.py`.
- **D-2** `:714-716`; embedded: no. old `        - ``"serverless"``: Creates a serverless template/endpoint via GraphQL.\n          Sets concurrency caps from ``spec.lifecycle``.  No self-terminator\n          script is injected.  Returns ``status="ready"``.` → new `        - ``"serverless"``: retained for golden coverage only; refused at\n          preflight (U46, ``unimplemented_mode.py``) so ``kinoforge generate``\n          never reaches it.` rg `'No self-terminator'` → 0.
- **D-3** `:351-353`; embedded: no. old
  ```
      here, at create. SkyPilot is ``RATE_READBACK`` — its rate is knowable only
      after launch, which is why its watchdog does this arithmetic on the
      instance instead.
  ```
  new
  ```
      here, at create. SkyPilot is ``RATE_READBACK`` — its rate is knowable only
      after launch, so its provider computes the deadline controller-side at
      create with ``placement.max_usd_per_hr`` as the stand-in rate and arms the
      instance watchdog with the resulting epoch.
  ```
  rg `'does this arithmetic on the'` → 0. Tests: `tests/providers/test_runpod_budget_deadline.py`.
- **D-4** `:457`; embedded: no. old `        ``selfterm.py`` is a boot-relative money cap (audit B4, 67627cd0), not` → new `        ``selfterm.py`` is a boot-relative money cap (audit B4, cfcd552c), not`. rg `'67627cd0'` in tests → 0 (5 hits are all historical docs). Tests: `tests/providers/test_runpod.py`.
- **D-5** `:517-521`; embedded: no. old
  ```
          ``heartbeat_mode`` is CONSUMED and RunPod is the only provider where
          that is true: ``_adapters.build_heartbeat_endpoint_for`` maps
          ``'graphql-tag'`` onto :class:`RunPodGraphQLHeartbeatEndpoint`, which
          reads the pod's own tags off the GraphQL API. Every other value, and
          every other provider, raises or returns None there.
  ```
  new (lines 3-4 only) `        ``'graphql-tag'`` onto :class:`RunPodGraphQLHeartbeatEndpoint`, which\n        reads a ``# _kinoforge_hb:<ISO>`` marker off the pod's ``dockerArgs``\n        (its write path is a no-op since C33-m). Every other value, and`. rg `"reads the pod's own tags"` → 0. Tests: `tests/providers/test_field_consumption_parity.py`.
- **D-6** `providers/runpod/selfterm.py:19-21`; embedded: no (module docstring; the `_TEMPLATE` body `:46-150` IS golden-pinned — do not touch). old
  ```
  was never called, and the orchestrator's heartbeat writes to RunPod pod tags
  (:mod:`kinoforge.providers.runpod.heartbeat`) which this script never reads.
  ```
  new
  ```
  was never called, and the orchestrator's RunPod heartbeat endpoint
  (:mod:`kinoforge.providers.runpod.heartbeat`, a ``dockerArgs`` marker whose
  write path is disabled) is nothing this script ever reads.
  ```
  rg `'writes to RunPod pod tags'` over tests, docs, AND `tests/providers/golden` → 0. Tests: `tests/providers/runpod/test_selfterm_reap_conditions.py`, `tests/providers/test_launch_payload_goldens.py`.
- **D-7** `providers/modal/__init__.py:85`; embedded: no. old `        IDLE_AUTOSTOP is ``_app.py`` ``scaledown_window`` (default 300 s);` → new `        IDLE_AUTOSTOP is ``_app.py`` ``scaledown_window``, set from\n        ``lifecycle.idle_timeout_s`` at create (the dataclass default of 300 s\n        never applies in production);`. rg `'default 300 s'` in tests → 0. Tests: `tests/providers/modal/test_provider.py`.
- **D-8** `modal/__init__.py:227` old `        """Return Modal catalog offers meeting ``reqs``."""` → `        """Return Modal catalog offers meeting ``placement``."""`
- **D-9** `:235` old `            spec: The instance spec (image, offer, setup_steps, launch, env).` → `            spec: The instance spec (image, placement, setup_steps, launch, env).`
- **D-10** `:240-241` old `        Raises:\n            ValueError: If ``setup_steps``/``launch`` or ``offer`` is missing.` → `        Raises:\n            ValueError: If ``setup_steps``/``launch`` is missing.\n            CapacityError: No catalog GPU class satisfies ``spec.placement``.`
- **D-11** `modal/_catalog.py:50` old `    """Return catalog offers filtered/ordered per ``reqs``.` → `    """Return catalog offers filtered/ordered per ``placement``.`
- **D-12** `providers/local/__init__.py:226` old `        """Return synthetic local offers that satisfy ``reqs``.` → `        """Return synthetic local offers that satisfy ``placement``.`
  rg for D-8..D-12 `'``reqs``'` → `tests/core/test_capability_shape_launch.py:1` (an unrelated docstring; `tests/providers/test_local.py` matches the word `reqs` only in prose). Tests: `tests/providers/modal/test_provider.py`, `tests/providers/modal/test_catalog.py`, `tests/providers/test_local.py`.
- **D-13** `validation/checks/models.py:6-7`; old `  - ``hf:<repo>:<file>``  -> resolved to\n    ``https://huggingface.co/<repo>/resolve/main/<file>``` → `  - ``hf:<repo>[@<rev>]:<file>``  -> resolved to\n    ``https://huggingface.co/<repo>/resolve/<rev or main>/<file>```. rg `'resolve/main/<file>'` in tests → 0. Tests: `tests/validation/checks/test_models.py`.
- **D-14** `validation/checks/capabilities.py:540-541`; old `      (``core/orchestrator.py:2008-2022`` and ``:2031-2035``) — an` → `      (``core/orchestrator.py``, the ``instance=session.instance`` stage\n      constructors) — an`. rg `'2008-2022'` → `tests/validation/test_capability_check.py:134` is a docstring, not an assertion (cross-area: it carries the same stale pointer). Tests: `tests/validation/test_capability_check.py`.
- **D-15** `validation/checks/lifecycle.py:16`; old `  to 1800 in interfaces.py:94.` → `  to 1800 in ``core/interfaces.py`` (``Lifecycle.grace_after_session_s``).` rg → 0. Tests: `tests/validation/checks/test_lifecycle.py`.
- **D-16** `validation/checks/ledger.py:12-15`; old
  ```
  Both the ledger loader and provider factory are injectable so the
  production wiring lives in the CLI layer (Task 10/11) rather than
  imported here — keeps the check independent of SessionContext
  construction details.
  ```
  new
  ```
  Both the ledger loader and provider factory are injectable. NOTE: no
  production wiring exists — the instance registered at the bottom of this
  module uses the inert defaults, so ``kinoforge doctor`` always reports
  "ledger empty" (hygiene audit 2026-10-18, P3).
  ```
  and `:33-34` old `    Production callers (the CLI wiring in Task 10/11) inject a real\n    loader bound to the current SessionContext's ledger.` → `    Nothing injects a real loader today (see the module docstring).` rg `'Task 10/11'` in tests → 0. Tests: `tests/validation/checks/test_ledger.py`.
- **D-17** `validation/__init__.py:3`; old `Two entry points:` → `Three entry points:`; insert after line 8 (`    still fails after auto-fix.`): `\n  - validate_for_load(cfg) runs STATIC only (the load_config context),\n    with the same auto-fix + raise policy as validate_for_generate.` rg `'Two entry points'` in tests → `tests/smoke/release_wan22/test_lora_strength_variation.py:12` (unrelated harness docstring). Tests: `tests/validation/test_integration.py`.
- **D-18** `validation/checks/field_support.py:127-129`; old
  ```
  #: The CPU arm is reached when ``_select_accelerator`` returns None (the S4
  #: replacement for what used to be a short-circuit to the synthetic
  #: ``sky-cpu-auto`` offer, which it does exactly when ``min_vram_gb == 0``.
  ```
  new
  ```
  #: The CPU arm is reached when ``_select_accelerator`` returns None (the S4
  #: replacement for the old short-circuit to the synthetic ``sky-cpu-auto``
  #: offer), which happens when no accelerator is named and ``min_vram_gb == 0``.
  ```
- **D-19** `:268-271`; old
  ```
      knowable at load: ``_select_accelerator`` returns None (pre-S4: the
      synthetic ``sky-cpu-auto`` offer)
      offer (no ``gpu_type``, hence the 30 GB arm) exactly when
      ``min_vram_gb == 0``. Printing both numbers would leave the operator to
  ```
  new
  ```
      knowable at load: ``_select_accelerator`` returns None (pre-S4: the
      synthetic ``sky-cpu-auto`` offer, no ``gpu_type``, hence the 30 GB arm)
      when no accelerator is named and ``min_vram_gb == 0``. Printing both
      numbers would leave the operator to
  ```
  (The code at `:282` still ignores a named accelerator — prior P3; these edits only un-garble the prose.) rg `'which it does exactly when'` → 0. Tests: `tests/validation/test_field_support_check.py`.
- **D-20** `stores/s3/lock.py:155` old `            # Stolen after TTL — best-effort: log via stdlib in production.` → `            # Stolen after TTL — silent by contract (nothing left to release).`; `stores/gcs/lock.py:144` old `            # Stolen after TTL — best-effort: log in production.` → same new text. rg `'best-effort: log'` in tests → 0. Tests: `tests/stores/test_s3_lock.py`, `tests/stores/test_gcs_lock.py`.
- **D-21** `sources/civitai/__init__.py:46`, `sources/civarchive/__init__.py:55`, `sources/huggingface/__init__.py:96`; old (identical in all three) `        KinoforgeError: Any other non-2xx HTTP error or network failure.` → `        KinoforgeError: Any other non-2xx HTTP error. A transport failure\n            (``urllib.error.URLError``) propagates unwrapped — audit B21.` rg `'non-2xx HTTP error or network failure'` in tests → 0. Tests: `tests/sources/test_civitai.py`, `test_civarchive.py`, `test_huggingface.py`.
- **D-22** `skypilot/__init__.py:449-451`; delete the three-line block (`# ---…`, `# Status conversion`, `# ---…`) that precedes the tunnel header at `:453`, and insert the same three lines directly above `_SKY_STATUS_MAP` at `:514`. Comment-only; no rg needed.
- **D-23** `skypilot/__init__.py:755-756`; old `          overridden is not honoured; this is verification finding F4 and it\n          stays UNSUPPORTED until S4 wires the realized-rate check.` → `          overridden is not honoured (verification finding F4). S4's\n          post-launch ``realized_rate`` readback and the pre-launch estimate\n          enforce the cap controller-side; the row stays UNSUPPORTED because\n          the optimizer itself never sees it.` rg `'until S4 wires'` in tests → 0. Tests: `tests/providers/test_field_consumption_parity.py`.
- **D-24** `diagnostics/c30_probe.py`; `:197-198` old `# Inlined verbatim from src/kinoforge/engines/comfyui/__init__.py lines\n# 1285-1330 (the diagnostic_mode trap_preamble in ComfyUIEngine.render_provision).` → `# Inlined verbatim from src/kinoforge/engines/comfyui/__init__.py (the\n# ``_kinoforge_diag_capture`` trap preamble in ComfyUIEngine.render_provision).`; `:449` old `# Inlined verbatim from src/kinoforge/engines/comfyui/__init__.py:1226-1274` → `# Inlined verbatim from src/kinoforge/engines/comfyui/__init__.py`; `:583` old `    """Mirror engines/comfyui/__init__.py:1376-1389 for a single pinned node."""` → `    """Mirror the custom-node clone lines in engines/comfyui/__init__.py."""`; `:598` old `    """Mirror engines/comfyui/__init__.py:1421-1427 for a single model entry.` → `    """Mirror the ``_kinoforge_download`` model line in engines/comfyui/__init__.py.`; `:626` old `# A3: A2 + ComfyUI clone (mirrors engines/comfyui/__init__.py:1358-1361).` → `# A3: A2 + ComfyUI clone (mirrors the ComfyUI clone line in engines/comfyui/__init__.py).`; `:634` old `# A4: A3 + ComfyUI requirements pip install (line 1362).` → `# A4: A3 + ComfyUI requirements pip install (the requirements.txt line).` rg `'1285-1330'` → 0. Tests: `tests/diagnostics/test_c30_provision_walk_down.py`.

### Code items

- **D-25** `providers/runpod/__init__.py:1801`; embedded: no. old `        grace_s: float = 90.0,` → `        grace_s: float = _BOOT_GRACE_MIN_S,`. Constant defined at `:309`, class at `:1786`. rg `'grace_s=90'` → tests pass it as an explicit kwarg (`test_runpod_boot_liveness.py` ×2, `tests/core/test_boot_liveness*.py` ×2), none reads the default. Tests: `tests/providers/test_runpod_boot_liveness.py`, `tests/providers/test_runpod_boot_grace_from_timeout.py`.
- **D-26** `providers/skypilot/__init__.py:1122-1148`; embedded: no (5 skypilot goldens pin the rendered Task config — unchanged by this edit). Delete line `:1122` `        if resources:` and dedent `:1123-1148` (through `            task_config["resources"] = resources`) by four spaces. Fence: `git log -S'if resources:'` → `afe4d956` "fix(providers/skypilot): synthesise CPU offer + build sky.Task from dict" — at that commit `resources` could be empty; `:1103-1113` now always populates it. rg `'"resources" not in'` → 0; `tests/providers/test_skypilot.py:505,531,561` read `cfg["resources"]` unconditionally. Tests: `tests/providers/test_skypilot.py`, `test_skypilot_setup_run_split.py`, `test_launch_payload_goldens.py`.
- **D-27** validation aliases; embedded: no. `models.py:29-30` delete `_PASS_CODES = PASS_CODES_AUTH_OK` and `_default_http_head = default_http_head`; `:67` `        self._http_head = http_head or _default_http_head` → `… or default_http_head`; `:108` `            if code not in _PASS_CODES:` → `            if code not in PASS_CODES_AUTH_OK:`. `image.py:26` delete `_PASS_CODES = PASS_CODES_AUTH_OK`; `:46` delete `_default_http_head = default_http_head`; `:58` → `or default_http_head`; `:82` `        if code in _PASS_CODES:` → `        if code in PASS_CODES_AUTH_OK:`. `custom_nodes.py:25` delete `_default_http_head = default_http_head`; `:48` → `or default_http_head` (KEEP `_PASS_CODES` at `:23`, the documented stricter set). Fence: `a2daaacd` ("module-level aliases preserve the existing injectable-seam names"). rg `'_default_http_head|_PASS_CODES' tests/` → 0. Tests: `tests/validation/checks/test_{models,image,custom_nodes}.py`, `tests/validation/test_model_ref_reachable_applies.py`.
- **D-28** `validation/checks/loras.py:443,446`; embedded: no. old `        except (UnknownAdapter, Exception):  # noqa: BLE001 — unknown => not our call` → `        except Exception:  # noqa: BLE001 — unknown engine => not our call`; then `:443` `        from kinoforge.core.errors import UnknownAdapter` becomes unused — delete it (ruff F401 would otherwise fail pre-commit). rg `'UnknownAdapter, Exception'` → 0. Tests: `tests/validation/test_lora_engine_support.py`.
- **D-29** `providers/runpod/__init__.py` list-pods helper; embedded: no. Insert after `list_instances` (`:824`):
  ```
      def _list_pods(self) -> list[dict[str, Any]]:
          """Return the raw ``myself.pods`` rows from one GraphQL read."""
          resp = self._http_post(self._base_url, {"query": _LIST_PODS_QUERY})
          data = _unwrap_graphql_response(resp, context="list pods")
          return (data.get("myself") or {}).get("pods") or []
  ```
  then `:821-824` body → `        return [_pod_to_instance(p) for p in self._list_pods()]`; `:847-851` → keep the `try:` / `except Exception: return None` shape with `pods = self._list_pods()` inside the `try` and delete the standalone `pods = …` line `:851`; `:956-960` → same shape with `return [identifier]` in the except, deleting `:960`. rg `'_LIST_PODS_QUERY' tests/` → one live-test docstring only. Tests: `tests/providers/test_runpod.py`, `test_runpod_name_resolution.py`.
- **D-30** Modal prefix constant; embedded: no (13 modal goldens pin the boot payload, not the app-name literal). `_app.py:54` add `_APP_PREFIX = "kinoforge-"` beside `_VOLUME_NAME` and add it to the `__init__.py:35-41` import list; `_app.py:127` `name=f"kinoforge-{req.run_id}"` → `name=f"{_APP_PREFIX}{req.run_id}"`; `__init__.py:332,554,662` `f"kinoforge-{…}"` → `f"{_APP_PREFIX}{…}"`; `:413` `[len("kinoforge-") :]` → `[len(_APP_PREFIX) :]`; `:434` `.startswith("kinoforge-")` → `.startswith(_APP_PREFIX)`. rg `'len("kinoforge-")'` in tests → 0; test fakes use the literal in records (still match). Tests: `tests/providers/modal/test_provider.py`, `test_app.py`, `tests/providers/test_modal_destroy_by_app_id.py`, `test_launch_payload_goldens.py`.
- **D-31** lease expiry predicate; embedded: no. In `stores/_lease.py` (add `from typing import Any`) add to `_LeaseLockBase` after `release`:
  ```
      def _lease_expired(self, payload: dict[str, Any]) -> bool:
          """True once the lease recorded in *payload* has passed ``expires_at``."""
          return self._clock.now() > float(payload.get("expires_at", 0.0))
  ```
  `s3/lock.py:113-114` old `            if self._clock.now() <= float(existing.get("expires_at", 0.0)):\n                return None` → `            if not self._lease_expired(existing):\n                return None`; `gcs/lock.py:116-117` identical edit. rg `'expires_at", 0.0'` in tests → 0. Tests: `tests/stores/test_s3_lock.py`, `test_gcs_lock.py`, `test_file_lock.py`.
- **D-32** HF resolve URL; embedded: no. `sources/huggingface/__init__.py` add after `_HF_BASE` (`:46`):
  ```
  def hf_resolve_url(repo: str, revision: str, path: str) -> str:
      """Return the canonical HuggingFace ``resolve`` URL for one file."""
      return f"{_HF_BASE}/{repo}/resolve/{revision}/{path}"
  ```
  `:225` and `:258` `url = f"{_HF_BASE}/{repo}/resolve/{revision}/{path}"` → `url = hf_resolve_url(repo, revision, path)`; `validation/checks/models.py:20` `from kinoforge.sources.huggingface import _parse_hf_ref` → `… import _parse_hf_ref, hf_resolve_url`; `:49` `        return f"https://huggingface.co/{repo}/resolve/{revision}/{path}"` → `        return hf_resolve_url(repo, revision, path)`. Tests: `tests/sources/test_huggingface.py`, `tests/validation/checks/test_models.py`.

## E. Needs discussion

1. **B-1 retry on mutations** — options: (a) split `_make_default_http_seams` into a retrying read closure and a non-retrying mutation path; (b) before re-sending a create, list by `name` and adopt an existing pod; (c) accept. Money path; needs a live check.
2. **B-2 budget vs `time_buffer`** — should the buffer apply to the budget arm at all? Either hand selfterm `budget/rate·3600 + time_buffer` so the pod-side subtraction yields the budget, or document that the real ceiling is `budget − rate×buffer` and make `_runpod_deadline_phrase` say so. Add a test with the default buffer either way.
3. **B-3 destroy of an already-gone pod** — tolerate the `POD_NOT_FOUND` errors shape on terminate (then confirm absence by listing) vs keep raising and fix the docstring plus the two tests whose fakes contradict live.
4. **B-4 S3 409** — treat `ConditionalRequestConflict` as contention (return `None` / retry); verify against live S3 (`tests/stores/live`).
5. **GCS steal path** — confirm in the live GCS lock test that `existing_blob.generation` is populated after `download_as_bytes()` (`gcs/lock.py:110`); if not, `delete(if_generation_match=None)` is unconditional and a steal can delete a fresh lock.
6. **SkyPilot `destroy_instance:1612`** claims idempotence on an absent cluster; `sky.down` on an unknown cluster raises in SkyPilot — verify at the pinned version.
7. **B11 fix shape** — the four `"errors" in resp` sites → truthiness is behaviour-changing only on the `errors: []` shape; moving `_unwrap_graphql_response` into `_transport.py` (ND #12) would let util/heartbeat share it.
8. **B8 / B21 timeouts** — adding `timeout=` to `:131,:144,:250` and the three sources is a behaviour change (new exception on stall); choose values (10 s for reads; create may need longer).
9. **User-facing text** for the confirmed P3s (`RunPodCapacityHintCheck`, `_skypilot_disk`, B-5 "all reachable").
10. ND #14 / #15 / #16 as previously filed; C-11, C-12, C-13 refactors; C-14 `_RUNPOD_SCHEMA_SIDECAR` CWD-relative path.
11. `LedgerStaleRowsCheck` — wire or delete (wiring as written recommends `forget` for launching rows, the documented wrong door).

## F. Cross-area notes

- The leak-sweep age bug is provider-wide: Modal `_record_to_instance:419` and SkyPilot `_cluster_record_to_instance:552` also return `created_at=0.0` (tools/ area).
- `core/reaper_actor.py:189` re-derives the `_locks/<key>.lock` sidecar path (stores layout ×4).
- `core/locks.py:96-229 InMemoryLock` duplicates `_LeaseLockBase` policy (ND #15); the cycle is `stores/_lease.py:22`.
- `tests/providers/test_runpod_name_resolution.py:203-213` and `tests/providers/test_runpod.py:853` fake a no-errors terminate for an unknown pod, contradicting the live `POD_NOT_FOUND` shape (B-3).
- `tests/providers/test_runpod_budget_deadline.py:49,169,172` zero `time_buffer`, hiding B-2.
- `tests/validation/test_capability_check.py:134` carries the same stale `orchestrator.py:2008-2022` pointer as `capabilities.py:540`.
- `docs/lifecycle.md:203-209` documents the runpod deadline formula without the budget arm's buffer interaction (B-2).
- Prior B2 (`deploy` never calls `_enforce_rate_cap`) is unchanged; Modal's `realized_rate` docstring `:349-358` still says it is the only cap on Modal.
- `tests/live/_c28_runpod_input_schema_probe.json` is read by production code via a CWD-relative path (`runpod/__init__.py:91`).
- `engines/comfyui/__init__.py` line anchors cited by `c30_probe.py` moved; the "If the source diverges, sync this constant" contract has no test.
