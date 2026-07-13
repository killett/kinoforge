# Design: job-based `/lora/set_stack` (async submit + poll)

**Date:** 2026-07-13
**Status:** validated (brainstorm complete, awaiting plan)
**Author:** Claude (Fable 5) + Dr. Twinklebrane

## Problem

`smoke-wan21-weekly` (Monday 12:00 UTC cron) has failed 3/3 consecutive runs
(Jun 22 / Jun 29 / Jul 6). Every failing step is a `POST /lora/set_stack`
whose pod-side download of the 350 MB `sttcrttn.safetensors` (test `lora_a`)
must finish inside a hard wall budget it cannot meet in the Monday-cron
window:

- **Branch-routing tests** (`test_branch_routing.py`): raw POSTs with no
  502-recovery. RunPod's edge proxy returns HTTP 502 ("Waiting for service to
  respond") once a held-open request exceeds its ~100 s response ceiling.
- **Matrix test** (`test_lora_swap_matrix.py`): tolerates the 502, then polls
  `/lora/inventory` for a fixed 600 s. Fails when download + load exceeds
  ~700 s total. Died at `step-2-load-a` (the 350 MB file) all three weeks.

The cold-boot `generate` (HF-hosted weights) passed every run, which is why
the smoke gets ~10 min in before dying.

### Root cause: architectural, not network

An off-peak probe (`tools/probe_civitai_throughput.py`, 2026-07-13) pulled the
full 350 MB in 33 s @ 10.6 MB/s — the pipe is fine off-peak. The failure is a
latent design inconsistency that a slow link merely exposes:

**Every long operation in `wan_t2v_server.py` is job-based (POST returns a
`job_id`, client polls status) EXCEPT `set_stack`, which is synchronous —
it holds the client's HTTP request open through the entire download + pipeline
reload, then returns inventory.** The one long operation that is not job-based
is exactly the one that 502s.

```
/generate      -> poll /status/{job_id}              job-based
/upscale       -> poll /upscale/status/{job_id}      job-based
/interpolate   -> poll /interpolate/status/{job_id}  job-based
/lora/set_stack -> (synchronous, held-open request)  THE OUTLIER
```

A prior fix wrapped `_download_one` in `asyncio.to_thread` so the event loop
stays responsive (`/health` keeps answering), but that does nothing for the
`set_stack` request itself, which remains open for the whole download and
trips the proxy ceiling.

### Secondary defect (diagnostic blindness)

`tests/_smoke_harness/matrix.py::_wait_for_inventory_convergence` catches
`except urllib.error.URLError`, which also catches its subclass
`urllib.error.HTTPError`. Every failure was therefore logged as
`last observed []` — indistinguishable from a dead pod. This is why 3 weeks of
failures were misread as pod deaths rather than slow downloads.

## Goal

Make LoRA swaps resilient to slow/variable networks at any LoRA size, on any
host pool, by applying the server's existing job-based pattern to the one
handler that lacks it — and fix the diagnostic defect so future failures are
legible. Fixes the production warm-reuse swap path, not just the test.

## Non-goals

- **LoRA prefetch at provision (option "1a")** — a separate follow-on spec.
  Substrate already exists (`KINOFORGE_INITIAL_LORA_STACK_JSON` loads an
  initial stack at startup); front-loading known LoRAs is a clean standalone
  increment later.
- No change to `/generate`, `/upscale`, `/interpolate`.
- No backward-compatibility shim for old synchronous clients — server and
  client ship atomically (pods are built from this repo).

## Decisions

- **Hybrid rejection semantics** (chosen over uniform-all-via-status). Cheap,
  instant rejections stay synchronous HTTP 4xx at POST; only the genuinely
  long path (download + load + rollback) becomes a pollable job. This keeps
  the branch-routing tests' synchronous-400 contract and the orchestrator's
  `400 -> typed-exception` translation untouched.
- **`create_task` idiom** (like `/upscale`), not the `/generate` queue+worker
  thread. `set_stack` is already `async def` under `_swap_lock`, so this is
  the smaller, pattern-consistent change.
- **Client hides async behind its existing synchronous-looking contract.** The
  `DiffusersEngine` LoRA-swap method submits + polls internally and returns /
  raises exactly as today, so its callers (warm-reuse matcher, `pod_lock`,
  grid executor) are untouched. Blast radius collapses to: server + client
  internals + smoke harness.

## Architecture

### 1. Server — submit endpoint `POST /lora/set_stack`

Returns fast; never holds the request through a download. At POST,
synchronously (no `_swap_lock`, no download):

- **Request shape** -> 422 (Pydantic, unchanged).
- **Branch-legality, hoisted before download** -> 400. Compute pipeline arity
  (read-only; a swap never changes it) and evaluate each target branch with
  the same logic `_replace_adapter_stack`'s pre-load gate uses today. Emit the
  identical 400 bodies verbatim (`branch_unsupported_single_transformer`,
  `branch_auto_disallowed_on_moe`, `branch_unknown`). A doomed branch request
  now rejects in ms with zero download.
- **Plan-time disk** -> 507 `phase:"plan"` (compute `target_dl_bytes` vs
  reclaimable disk, unchanged).

Then create the job record
`{state:"queued", inventory:None, free_bytes:None, swap_rejected:None,
error:None}`, `asyncio.create_task(_run_swap_job(...))`, and return
`{"job_id": ...}` (200).

Arity is stable between submit and job-run (it is a property of the loaded base
pipeline, not of any LoRA), so evaluating branch-legality at submit — outside
the swap lock — is safe.

### 2. Server — job runner `_run_swap_job`

`async with _swap_lock:` wraps the **entire existing `set_stack` body
verbatim** — pending-entry seeding, evict plan, evict loop, download loop,
`_replace_adapter_stack` load, VRAM-OOM rollback — minus the two hoisted
checks (branch-gate; plan-time-disk sync-raise). Download-phase ENOSPC remains
here.

Terminal outcomes write their payload **then flip `state` last** (the
race-free ordering `/upscale` already uses, so a poller that sees a terminal
state always sees a populated payload):

| Outcome | `state` | payload |
|---|---|---|
| success | `done` | `inventory`, `free_bytes`, `swap_rejected:None` |
| vram_oom / set_adapters_value_error | `done` | `swap_rejected:{reason, target_refs_dropped}` (today a **200**, stays a non-error) |
| disk_full (download phase, ENOSPC) | `error` | `{error:"disk_full", phase:"download", ...}` |
| lora_download_failed | `error` | `{error:"lora_download_failed", ...}` |
| rollback_failed | `error` | `{error:"rollback_failed", ...}` |
| any unexpected exception | `error` | `{error: "<type>: <msg>"}` (poller always terminates) |

Note the `swap_rejected` (vram_oom) case is **not** an error — today it is a
200 response with `swap_rejected` populated. It stays a non-error terminal
(`state:"done"` + `swap_rejected` set); the client raises
`LoraSwapVramOomError` from it exactly as it does today's 200-with-reject.

Each `error` payload also carries the **HTTP-equivalent `status`** (507 / 502
/ 500) alongside the existing `error` / `evict_completed` / `download_failed` /
`underlying` fields, so the client can feed it straight into the unchanged
`_raise_lora_swap_error(status, body, pod_id)` translator.

### 3. Server — status endpoint `GET /lora/set_stack/status/{job_id}`

Return the job record; 404 if unknown. `state in queued|running|done|error`.
Mirrors `/upscale/status/{job_id}` 1:1.

### 4. Client internals (`DiffusersEngine.set_lora_stack`)

Signature (`set_lora_stack(*, pod_id, active_stack, download_specs) ->
dict`), return shape (`{inventory, free_bytes, swap_rejected}`), and raised
exceptions (`LoraSwapVramOomError`, `LoraSwapDownloadError`,
`LoraSwapDegradedPodError`, `LoraSwapDiskFullError`,
`LoraSwapPodUnreachableError`, `RuntimeError` on unknown body) **identical to
today**. Only the internals change:

- POST `/lora/set_stack` (still wrapped in `retry_proxy_call` /
  `RUNPOD_PROXY_POLICY`).
  - 4xx at submit (422/400/507) -> the raised exception carries `.status` +
    `.body`; feed to the existing `_raise_lora_swap_error(status, body,
    pod_id)`, which maps 507/`disk_full` and the (hoisted) branch/plan bodies
    exactly as today. Synchronous-rejection contract preserved.
  - 200 `{job_id}` -> poll `GET /lora/set_stack/status/{job_id}` (also wrapped
    in `retry_proxy_call`) every ~3-5 s until terminal or a generous deadline
    (minutes; the proxy ceiling no longer applies — POST returns instantly and
    status GETs are cheap). This needs a GET seam on the engine (the class only
    has `_http_post` today; add `_http_get`, mirroring the CLI's
    `_http_get_json`).
    - `done` + `swap_rejected:None` -> return the `{inventory, free_bytes,
      swap_rejected}` dict as today.
    - `done` + `swap_rejected.reason=="vram_oom"` -> **raise**
      `LoraSwapVramOomError(pod_id, dropped_refs=...)` — matching today's
      behavior (today it raises, it does not return the reject).
    - `error` -> call `_raise_lora_swap_error(status, error_payload, pod_id)`.
      **The job's error payload therefore includes the HTTP-equivalent
      `status` (507 / 502 / 500) and the same `evict_completed` /
      `download_failed` / `underlying` fields**, so the translator is reused
      verbatim and still distinguishes `LoraSwapDownloadError` (empty
      `evict_completed`) from `LoraSwapDegradedPodError` (non-empty).
    - deadline exceeded -> raise `LoraSwapPodUnreachableError` (the existing
      transport-failure type).
- Keep the existing `/health` proxy-warmup before the first POST (the
  fresh-pod first-request 502 guard is still real; see
  `wan_server_set_stack_proxy_warmup` memory).

### 5. Harness (`matrix.py` + branch tests) — the logging fix

- `run_matrix`: replace the raw POST + `_wait_for_inventory_convergence` with
  submit + poll on the status endpoint. On `error`, assert with the **actual
  `status.error` payload** — no more blind `last observed []`.
- **Kill the `except urllib.error.URLError` swallow** that silently catches
  `HTTPError` (its subclass). Split so genuine HTTP errors surface distinctly
  from transient URL errors.
- `_wait_for_inventory_convergence` retires (its only reason to exist was the
  proxy-502 recovery hack). Keep the `/lora/inventory == expected` assertion as
  the post-swap correctness check, but drive **completion** off job status.
- Branch tests: `test_explicit_high_noise_branch_rejected_on_wan21` keeps
  `pytest.raises(HTTPError)` / `code == 400` **unchanged and now faster** (sync
  reject at submit, no download). `test_auto_branch_succeeds_on_wan21` becomes
  submit -> poll `done` -> assert inventory.

## Testing

### Offline / TDD (no spend)

- **Server** (FastAPI `TestClient`, mock `_download_one` + `_replace_adapter_stack`):
  submit returns `job_id`; hoisted branch-400 and plan-disk-507 stay
  synchronous; job runner each terminal transition (done / vram_oom /
  disk_full / download_failed / rollback_failed); status 404 on unknown id;
  result-before-state ordering (no done-without-payload).
- **Client** (mock HTTP seam): each terminal state -> correct return /
  exception; 4xx-at-submit path; deadline-exceeded path.
- **Harness**: a `state:"error"` payload surfaces the real error string; an
  `HTTPError` is no longer swallowed as `URLError`.
- **Golden regen**: the new routes live in `wan_t2v_server.py`, which is
  embedded (base64+gzip) in the provision script, so
  `tests/engines/diffusers/test_render_provision_split.py::test_script_is_byte_identical_to_golden`
  will drift — regenerate the golden (same mechanism as the 2026-07-13
  iterdir-sort fix) and verify the diff is embed-content-only.

### Live re-validation (gated spend, ~$0.20-0.30)

- Commit the RED scaffold **before** any spend (durability rule).
- Run `pixi run smoke-21b-live` on a real A5000; prove the 350 MB swap now
  completes via poll with **no 502**, the matrix's 4 steps pass, and the branch
  tests reject/accept correctly.
- Poll pod utilisation during the run (live-smoke monitoring rule).
- Frame-QA every output video (mandatory visual-QA rule).
- Teardown with `--no-reuse` semantics; verify `kinoforge list` shows no pod
  and an empty ledger after.

## Blast radius

- **Server:** `wan_t2v_server.py` — split `set_stack`, add `_run_swap_job` +
  status route.
- **Client:** `DiffusersEngine` LoRA-swap method internals only (signature +
  exceptions unchanged).
- **Harness:** `tests/_smoke_harness/matrix.py`, `tests/smoke/live_wan21/*`.
- **Untouched by design:** warm-reuse matcher, `pod_lock`, grid executor
  (client contract preserved); `/generate`, `/upscale`, `/interpolate`;
  ComfyUI (its LoRA path is graph-wiring, not `/lora/set_stack`).
- **Bonus, no extra work:** `wan_t2v_server.py` also runs on Modal pods
  (embedded module), whose proxy has the same response-window limit — Modal
  warm-swaps get this fix for free.

## Risks

- **Job-record volatility:** in-memory `_swap_jobs`, lost on pod restart —
  consistent with `/upscale` + `/interpolate`; a restart implies cold-boot
  anyway, so acceptable.
- **Branch-gate hoist correctness:** the submit-time gate must be byte-for-byte
  equivalent to the current in-`_replace_adapter_stack` gate, or a branch
  request could pass submit and reject at load. Mitigation: extract the gate
  into one shared helper called from both submit and the load path; unit-test
  parity.
- **Deadline tuning:** the client poll deadline must exceed the worst realistic
  download+load. Set generously (minutes) since the proxy ceiling no longer
  bounds it; surface a clear timeout error if hit.
