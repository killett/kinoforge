# Hygiene re-audit 2026-10-21 — engines / upscalers / image_engines / text_engines

Scope: `src/kinoforge/engines/**` (incl. `diffusers/servers/**`), `src/kinoforge/upscalers/**`,
`src/kinoforge/image_engines/**`, `src/kinoforge/text_engines/**`. HEAD `f89eff2b` (nothing in this
area changed in the controller's later cleanups). Every file in the area was read in full; line numbers
below are at HEAD.

Embedded-file reminder (controller): `servers/wan_t2v_server.py`, `servers/_video_io.py`,
`servers/_util_stats.py`, `servers/text_server.py`, `servers/_upload.py` are embedded by name on RunPod
configs; the whole `servers/` package (incl. `minimax_h3_server.py`, `_lora.py`, `_av_io.py`) on 9
Modal/SkyPilot configs; `upscalers/flashvsr/*` (11 configs), `upscalers/spandrel/*` (2 configs) via
`embed_modules`. Any byte change in those files moves launch goldens + payload baselines. Everything
under `engines/*/__init__.py`, `engines/_*.py`, `image_engines/`, `text_engines/`, `upscalers/*/_engine.py`
and `upscalers/seedvr2/` is controller-side (NOT embedded).

---

## A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line) |
|---|---|---|---|
| B3 | rolled-back LoRA swap reported as success | CONFIRMED | server emits exactly two reasons, `wan_t2v_server.py:2225` `"reason": "vram_oom" if is_oom else "set_adapters_value_error"`; client checks only `sr.get("reason") == "vram_oom"` (`engines/diffusers/__init__.py:777`); `core/lora_apply.py:344` logs "accepted the %d-entry stack" without reading `swap_rejected` (`rg swap_rejected src/` → no other consumer). |
| B4 | Wan stores LoRAs flat by vendor filename; two refs sharing a basename collide | CONFIRMED | `wan_t2v_server.py:1390` + `:2083` call `_download_one(spec, LORAS_DIR)` → `:1140 target = dest_dir / spec.filename`; evict unlinks by path when no sibling with the same *ref* survives (`:1241-1244`); H3 uses per-ref dirs `_lora.py:347-365,390`. |
| B5 | ComfyUI `status=="error"` is not terminal; `_logged_error_once` per backend | CONFIRMED | `comfyui/__init__.py:887-905` logs once then falls through to `poll_idx += 1` (`:929`); only exits are `_MAX_POLL`/`poll_timeout_s` at `:792-804`; `getattr(self, "_logged_error_once", False)` at `:887-889`. |
| B18 (client half) | `rollback_failed`/`lora_swap_failed` reach client as bare `RuntimeError` | CONFIRMED (+server detail) | `_raise_lora_swap_error` handles only `branch_routing`/`lora_format_unsupported`/`lora_load_failed`/507/502 then `raise RuntimeError(f"unknown /lora/set_stack error body…")` (`diffusers/__init__.py:970`). Server side: `_replace_adapter_stack` (`wan_t2v_server.py:1275-1307`) has no rollback boundary — a non-OOM `RuntimeError` at entry *k* is re-raised (`:2191`) leaving adapters 0..k-1 attached with no `set_adapters` while `_inventory` already holds rows for every target (seeded at `:2138`), then recorded as `lora_swap_failed` (`:2313-2320`). `_lora.py:251-294` is the fixed shape. |
| B19 | cfg knobs silently ignored on the pod | CONFIRMED | spandrel: server pins `tile_size=512, batch_size=4` (`wan_t2v_server.py:257-258`), runtime `del params` (`spandrel/_runtime.py:160`, `:210`). flashvsr: server pins `window_size=24, tile_size=0, long_video_mode=False` (`:269-271`); runtime reads `self._window` only for a warning (`flashvsr/_runtime.py:314`), `self._tile` only as `tiled=bool(...)` (`:342`), `self._long_video_mode` assigned (`:292`) and never read; its own docstring admits "informational only" (`:163-165`) while `core/config.py:731-751` documents them as live knobs. `long_video_mode` does still select the 4-file bundle (`flashvsr/_engine.py:81`). |
| P3 `engines/hosted:68,348-366` | 120×1 s poll budget, no failure-state check | CONFIRMED | `_MAX_POLL = 120` (`:68`); loop `:348-365` only tests `status == "done"`, sleeps 1.0, raises `TimeoutError`. |
| P3 `_pod_http.py:139` | `submit_and_poll` has no wall-clock deadline | CONFIRMED | `while True:` at `:139`, exits only on `done`/`error`. (Per-request timeouts DO exist: `:33` 60 s, `:36` 600 s.) |
| P3 `config.py:366` vs `hosted:513-516` | `auth:` dict branch unreachable from YAML | CONFIRMED | `HostedEngineConfig` fields: `provider, endpoint, api_key_env, health_url, url_path` under `extra="forbid"` (`core/config.py:366-372`); `hosted/__init__.py:514-516` branch reachable only from a programmatic raw dict. |
| FIX NOW `diffusers:142-144` | `_render_embed_lines` docstring says `base64 -d` | CONFIRMED | `:142-143` "base64-encodes each .py file, and emits an ``echo '<b64>' \| base64 -d``"; code is gzip+`python3 -c "…gzip.decompress(base64.b64decode…"` (`:186-205`). |
| FIX NOW `diffusers:190` | stale payload sizes | CONFIRMED | `:190` "(~67KB raw → ~16KB gz → ~22KB b64)"; measured `wan_t2v_server.py` = 111,757 B raw / 33,874 gz / 45,376 b64. |
| FIX NOW `diffusers:1159-1163` | "exec the server … PID 1" | CONFIRMED | `:1159-1160`; launch is `Launch(argv=…, exec_pid1=False)` with "NOTE: NO `exec` prefix" (`:1424-1437`). |
| FIX NOW `comfyui:1537`, `fake:249,251` | "deferred to Task 12" | CONFIRMED | `comfyui:1537`, `fake:14`, `:249`, `:251`, `:257`; `profile_for` has no production caller (`rg profile_for src/` → raise-only implementers + unrelated `client_profile_for_server_module`). |
| FIX NOW `luma_agents:170-173` | "the live smoke settles which one is real" | CONFIRMED | `:170-173`; settled by `successful-generations.md` entry #15 (2026-07-04, 4/4 green) — `output[]` shape is real; `assets.image` fallback at `:181-182` is now a dead branch (keep it: edge-case behaviour). |
| FIX NOW `image_engines/replicate:104,111` | "lands in Task 17" | CONFIRMED | `:104` "concrete impl lands in Task 17", `:111` same; `:106` exception TEXT also says "filled in Task 17" (user-facing — leave). |
| redundant `diffusers:127-133` ≡ `:199-205` | gzip+b64 embed encoding ×2 | CONFIRMED | `:128-135` and `:200-207` are the same 8-line `encoded = …; lines.append(f"echo '{encoded}' \| python3 -c …")` block (one encoding decision; `mtime=0` comment duplicated too). |
| redundant `diffusers:1360-1414` | sub-engine composition ×3 | PARTIAL | upscaler (`:1361-1372`) and interpolator (`:1374-1392`) blocks are twins (phase `"build"`); the text block (`:1394-1416`) differs on purpose — phase `"runtime"` and captures `ports` (documented `:1394-1400`). A helper would carry two parameters; low value. |
| redundant `comfyui:1315-1319` ≡ `diffusers:1240-1244` | selfterm snippet | CONFIRMED | byte-identical 5-line `'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then …'` in both; goldens pin the rendered bytes. |
| ND #8 `SwapRejectedDetails:908`, `VRAMRollbackFailure:642` | producer-less | CONFIRMED | only definitions + `core/grid/swap_failures.py:47,58` regexes + tests constructing them directly (`tests/engines/test_vram_rollback_branch.py:156-157` asserts the class exists — fence for any deletion). |
| ND #8 dormant cold-boot LoRA path `:1310-1424` | `KINOFORGE_INITIAL_LORA_STACK_JSON` has no producer | CONFIRMED | `rg` across `src tests examples tools`: readers only (`wan_t2v_server.py:1570`), tests + plans; `docs/superpowers/specs/2026-09-22-h3-lora-shared-seam-design.md:353` records it as deliberately dormant. |
| ND #8 `_torch_build` ×3 | contracts drifted | CONFIRMED | `wan:1518-1534` → `{"version": None, "cuda": None}` on any `Exception`; `minimax:382-397` → `{}` on any `Exception`; `text_server:168-174` → `{}` on `ImportError` only. |
| ND #8 U50 shutdown retirement wan-only | | CONFIRMED | `@app.on_event("shutdown")` only in `wan:1590`; `text_server:318-319` and `minimax:860-861` start daemon workers with no shutdown hook. |
| ND #8 bare `create_task` `:1829,2486,2653` | | CONFIRMED | all three discard the task; `_lora.py:913-917` keeps strong refs in `_BACKGROUND_TASKS` and explains why (`:576-578`). |
| ND #8 unreachable in-job LRU branch `:2019-2039` | | PARTIAL | `_plan_disk_infeasible` (`:1847-1893`) pre-rejects with the same inequality, but free disk is re-read in-job (`:1973`) and can change between submit and run, and the per-sibling accounting (`:1984-1988` vs `:1879-1883`) is not identical → defensive, not dead. |
| ND #9 | hosted engines bypass `RemoteSubmitPollBackend`; `FalBackend._delete` hooks on a class that doesn't inherit them | CONFIRMED | `class FalBackend(GenerationBackend)` (`fal:200`), `HostedAPIBackend(GenerationBackend)` (`hosted:187`), `BedrockVideoBackend(GenerationBackend)` (`bedrock:101`); `del cancel_token` ×6 (`fal:329,395`, `hosted:295,346`, `bedrock:187,228`); the only `_delete` caller is `RemoteSubmitPollBackend._delete_with_retries` (`core/remote_backend.py:225`), so `fal:285-289` "cannot quietly leak" is false. |
| ND #10 | upscaler registration `except UnknownAdapter: pass` | CONFIRMED | `spandrel/__init__.py:21-24`, `flashvsr:19-22`, `seedvr2:105-108`, `interpolators/rife:18-21`; `core/registry.py:278-280` says duplicates are rejected "so that adapter import-order accidents surface loudly" — every registrant mutes it. Fence `3b95cca3`. Outer `except ImportError` is documented pod-embed intent (`spandrel/__init__.py:3-8`). |
| ND #11 | image `_delete` raises `NotImplementedError` | CONFIRMED, sharper | `luma_agents:184-188`, `image replicate:103-107`; `_delete_with_retries` catches only `EphemeralDeleteHTTPError` (`core/remote_backend.py:221-226`) and is called *after* completion and *before* `return artifact` (`:320-324`) → the paid-for artifact is dropped. Reachable only programmatically: `core/ephemeral.py:132-133` maps `luma_agents`/`replicate` image engines to unsupported. |
| ND #25 | FastAPI `on_event` in pod servers | CONFIRMED | `wan:1537,1590`, `text_server:302`, `minimax:815`. |
| keep: wan inline `/upload` beside `_upload.py` ("migrating … for no behaviour change") | | **REASON NOW FALSE** | the copies have diverged in behaviour: `_upload.py` closes two defects (all-dots `X-Filename` → `:47-57`; `os.replace` inside the guarded block → `:116-121,125-133`, commit `e6041bdd`) that the Wan copy still has (`wan:2702-2714` no all-dots guard; `:2769-2770` `os.replace` outside the `try`). See new bug NB3. The payload-ceiling reason for not migrating still stands; the "no behaviour change" half does not. |
| keep: `_lora.py:307-310` `download_one` duplicating Wan's | | CONFIRMED | `_lora.py:300-344` vs `wan:1125-1165`; the keep itself already notes the per-ref-dir drift (B4). |
| keep: `LoraTarget` ×3 | | CONFIRMED | `wan:915-987`, `_lora.py:429-488`, parity test named in both. |
| keep: minimax sibling | | CONFIRMED | `minimax_h3_server.py:12-17`. |
| keep: `KINOFORGE_MODELS_DIR` literals | | CONFIRMED (lines moved) | now `wan:183-188` (keep cites `:166-182`). |
| keep: spandrel inline `curl` | | CONFIRMED | `spandrel/_engine.py:83-98,129-137`. |
| keep: seedvr2 extras stub, `--dest` unused | | CONFIRMED | `seedvr2/__init__.py:55-102` raise `ExtrasNotInstalled`; `_fetch_weights.py:29` parses `--dest`, never uses it. |
| keep: `_wire_entry` omits `target` | | CONFIRMED | `diffusers/__init__.py:722-737`. |
| keep: `text_server` 64 MB cap | | CONFIRMED | `text_server.py:76`, rationale `:35-39`. |
| keep: `_DEFAULT_RUNPOD_IMAGE` ×2 | | CONFIRMED | `diffusers:79` / `comfyui:89` identical strings; diffusers' coupling to cu124 documented `:71-86`. |
| 07-16 ND #12 spandrel `_fetch_weights` production-dead | | CONFIRMED | no `src/` caller (`rg 'spandrel\._fetch_weights' src` → only itself); `docs/engines.md:606` still documents the CLI (cross-area). |

---

## B. New bugs (missed by the prior audit)

**NB1 — P2 · no socket timeout on the controller→pod poll path (diffusers + ComfyUI + hosted + fal).**
`engines/diffusers/__init__.py:288` (`_urllib_post_json`), `:305` (`_urllib_get_json`), `:322` (`_urllib_get_bytes`);
`engines/comfyui/__init__.py:207,239,279,372`; `engines/hosted/__init__.py:90,103,109`;
`engines/fal/__init__.py:96,115`; `image_engines/fal/__init__.py:53,63` — all `urlopen(req)` with no
`timeout=`, and `rg setdefaulttimeout src/` is empty. Contrast `_pod_http.py:33-37` (60 s / 600 s),
`luma_agents:96` (60 s), every server-side download (600 s).
Scenario: `kinoforge generate` on RunPod; `DiffusersBackend.result` polls `/status/{job}` through
`retry_proxy_call(... self._http_get ...)` (`:595-602`). The RunPod proxy accepts the TCP connection and
never answers (black-hole, distinct from the 502 "waiting for service" case). `urlopen` blocks forever;
the wall-clock check at `:587` and the cancel-token check at `:585` are never reached because the thread is
inside `urlopen`; the heartbeat thread's `POD_GONE` cannot interrupt it (`retry_proxy_call` honours the token
only between attempts, `_proxy_retry.py:110-114`). The run hangs until Ctrl-C while the pod bills to
selfterm's cap. Worse at boot: `_wait_ready.poll_until_ready:102` calls the same `http_get`, so a hung
readiness GET also silences the 2026-07-07 boot-stall probe (`:121-133`) that exists to fail fast.
Introduced with the engines: `7a461afc` (diffusers), `9317d684` (comfyui). Same class as prior B8/B9/B21.
Not FIX NOW (adding a timeout is a behaviour change).

**NB2 — P3 · `_ensure_on_gpu` does model load/eviction synchronously on the event loop.**
`wan_t2v_server.py:375-420`: `async def _ensure_on_gpu` holds `_REGISTRY_LOCK` and calls
`_free_headroom_for` → `_evict_to_cpu` → `victim["pipe"].to("cpu")` / `_drop_to_disk` (`gc.collect()`,
`torch.cuda.empty_cache()`), then `_load_model_to_gpu(name)` (`:383`, `:406`) and `new_pipe.to("cuda")`
(`:391`, `:408`); `_enforce_headroom` (`:463-468`) likewise — none wrapped in `asyncio.to_thread`, unlike
every other heavy call in the same file (`:2083`, `:2550`, `:2561`, `:2675`) and unlike `_lora.py:830-832`.
Scenario: `kinoforge upscale` against a warm Wan pod → `/upscale` → `_run_upscale_job` →
`_ensure_on_gpu("flashvsr-wan21-bfloat16")` → FlashVSR load (9 GiB expected, tens of seconds) runs on the
loop → `/health`, `/util`, `/upscale/status` do not answer → RunPod proxy 502s. The client survives because
`retry_proxy_call` absorbs 502s, which is also why this never showed as a failure; on Modal, `/util` IS the
util probe (`_util_stats.py:5`), so the pod reads as dead for the load window. Exactly the class memory
`feedback_wan_server_async_blocking.md` describes. Introduced `a5481591`, reshaped `704f4dae`. Embedded
file → fix moves goldens.

**NB3 — P3 · Wan inline `/upload` has the two defects `_upload.py` fixed (and the keep says they are equivalent).**
`wan_t2v_server.py:2702-2714` `_sanitize_upload_filename` has no all-dots guard; `:2769-2770` runs
`os.replace(tmp_path, final)` *outside* the `try`. Scenario: `PUT /upload` with `X-Filename: ..` (or `...`)
→ `cleaned == ".."` → `os.replace(tmp, /tmp/kf-uploads/..)` raises `OSError` → escapes as a raw 500 and the
`.part` tempfile stays on the pod disk — the leak `_upload.py:47-57` and `:116-121` describe closing
(commit `e6041bdd`). Also any ENOSPC at publish time leaks the same way. The kinoforge client always sends
`<sha8>.mp4`/`.png` (`_pod_http.py:241`), so this needs a hand-crafted request. This is the new evidence
that refutes the hygiene-notes keep's "no behaviour change" clause (section A).

**NB4 — P3 · ComfyUI remote bootstrap: a failed custom-node `git checkout <ref>` is not fail-fast and relocates every model download.**
`engines/comfyui/__init__.py:1351-1355` renders
`[ ! -d custom_nodes/X ] && git clone … custom_nodes/X && cd custom_nodes/X && git checkout REF && cd ../..`
under `set -euo pipefail`. Bash exits on a failure only for the command *after the final* `&&`; if
`git checkout REF` fails (stale/rewritten SHA; every shipped ComfyUI config pins refs, e.g.
`runpod-comfyui-wan-2_1-14b-i2v.yaml:32`) the list returns non-zero without exiting and the cwd is left in
`custom_nodes/X`. Every later `mkdir -p models/<subdir>` / `_kinoforge_download … 'models/<subdir>/<file>'`
(`:1396-1402`) then lands under `custom_nodes/X/models/…`; the final `cd /workspace/ComfyUI && exec …`
is absolute, so ComfyUI boots "healthy" with no models and `/prompt` fails validation after the full
multi-GB download has been paid for. A clone failure on that same line behaves the same (the clone is no
longer the final `&&` command). The local path (`nodes.py:54`, `run_cmd` with `check=True`) fails fast.
Introduced `2e213167`. Goldens pin the rendered line (`tests/engines/test_comfyui_render_provision.py:110`).

**NB5 — P3 · hosted `provision` pings `health_url` unconditionally while the schema defaults it to `""`.**
`engines/hosted/__init__.py:624-628` `self._http_get(health_url)` with `health_url = ""` →
`urlopen("")` raises `ValueError("unknown url type: ''")` → wrapped as `KinoforgeError("hosted endpoint
unreachable: unknown url type: ''")`. `HostedEngineConfig.health_url: str = ""` (`core/config.py:370`,
documented at `:334` as "URL pinged by provision()" with no "optional"); fal guards the same step with
`if health_url:` (`fal:553-556`) and documents it optional (`config.py:550`). A cfg that omits `health_url`
gets a misleading "unreachable" instead of a config error. Synthetic-shim engine; `examples/configs/hosted.yaml:21`
sets it.

**NB6 — P3 (latent) · the LRU registry `_LOADED` is mutated under two different locks.**
`_ensure_on_gpu`/`_enforce_headroom` use `_REGISTRY_LOCK` (asyncio, `wan:375`), while
`_promote_wan_if_evicted` — run on the sync generate worker — uses `_REGISTRY_TLOCK` (threading, `:522`)
and `_drop_to_disk`s any CUDA-resident upscaler (`:526-537`); the docstring acknowledges the split
(`:510-511`). Scenario on a co-resident pod: `/generate` queued while `/upscale` is running (the upscale
holds no registry lock during `method(local, scale, params)` at `:2561`) → the worker drops the FlashVSR
entry (`victim["pipe"] = None`, `empty_cache`) under the running upscale, then reloads ~70 GiB of Wan next to
it → OOM in one or both jobs. The orchestrator serialises stages per run today, so this needs concurrent
clients (grid/warm-reuse overlap).

Checked and found clean (so the controller need not re-check): U66 module-level `kinoforge.*` imports
under `servers/` (all target `servers/` siblings — AST sweep); every lazy `kinoforge.core.*` import in the
RunPod-embedded servers is embedded by every config that can reach it (`_enforce_headroom:431`,
`_run_upscale_job:2514` vs `embed_files` per config — script cross-check); all 28 swallowing `except`s in
`servers/` are intent-commented diagnostics/probes or terminal job records — none can silently substitute
weights; `_lora.py` apply/rollback is sound; `text_server.py`, `minimax_h3_server.py` (incl.
`LORAS_DIR.mkdir` at `:831`), `_upload.py`, `_av_io.py`, `_util_stats.py`, `_video_io.py`,
`_proxy_retry.py`, `_wait_ready.py`, `replicate`, `runway`, `bedrock_video`, both image `RemoteSubmitPoll`
engines, `transformers` text engine; flashvsr's two `if exists` fallbacks (`_runtime.py:194`, `:281`) are
covered by `curl -L -f` staging in `_engine.py:189,200` plus the loud `LQ_proj_in.ckpt` check (`:246-258`).

---

## C. New hygiene findings

| # | location | category | behaviour-preserving? | risk | call | rationale |
|---|---|---|---|---|---|---|
| C1 | `fal/__init__.py:330-332,639-641`; `hosted/__init__.py:296-297`; `diffusers/__init__.py:505-507` | dead + documentation | yes — `core/prompt_routing.py` imports only `core.interfaces` (`:15`); `replicate:32` and `bedrock_video:42` already import it at module level; no test patches it by path | low | FIX NOW | The "local — avoid circular at module load" comment is false in all four places; hoist to one module-level import. (Un-commented local imports of the same name at `comfyui:707,1601`, `diffusers:1630`, `hosted:742` can go with it.) |
| C2 | `engines/_pod_http.py:235-242,272` | structural (memory) | yes — identical sha256 and `Content-Length`; only the hashing path changes | low | FIX NOW | `local_path.read_bytes()` loads up to 2 GB (wan's upload cap) into controller RAM solely to hash it, then a second handle streams the body. `core/downloader.sha256_file` (`:67`) already streams; size from `stat()`. |
| C3 | `engines/_wait_ready.py:104-105` | error-handling (intent comment) | yes | low | FIX NOW | bare `except Exception: pass` with `noqa` but no stated intent (readiness poll — any failure means "not ready yet"). |
| C4 | `engines/comfyui/nodes.py:22-31` | documentation | yes | low | FIX NOW | 3-step docstring omits the `ref` checkout step at `:52-54`. |
| C5 | `engines/replicate/__init__.py:63-65` | documentation | yes | low | FIX NOW | "default 10 s" vs `_REPLICATE_SUBMIT_MIN_INTERVAL_S = 12.0` (`:46`). |
| C6 | `image_engines/replicate/__init__.py:47-50` | documentation | yes | low | FIX NOW | claims `_extract_output_url` differs from the video backend; `:96-101` ≡ `engines/replicate:157-162`. |
| C7 | `engines/hosted/__init__.py:3-4,17-25` | documentation | yes | low | FIX NOW | module docstring presents the user-deployed shim as "e.g. fal.ai" with a `provider: fal` / `fal.run` example — the same misdescription the prior audit flagged in README/configuration.md, in source. |
| C8 | `engines/fal/__init__.py:282-293` | documentation | yes | low | FIX NOW | `_delete` docstring: "a buggy caller bypassing pre-flight cannot quietly leak" — nothing calls `_delete` on a `GenerationBackend` (ND #9). |
| C9 | `engines/diffusers/__init__.py:59-66` | documentation | yes | low | FIX NOW (minor) | `_MAX_POLL` docstring says it "is the total seconds the backend will wait"; `poll_timeout_s` (wall clock, `:423`, `:587`) governs since the cancel-token rework; `_MAX_POLL` is the belt-and-braces cap. |
| C10 | `upscalers/spandrel/_fetch_weights.py:3-9,124` | documentation | docstring yes; `:124` `--help` text is user-facing → no | low (docs) / med | NEEDS DISCUSSION | docstring says it is "invoked by `SpandrelEngine.render_provision`" and dispatches "via `registry.source_for_ref`"; `_engine.py:83-98` inlines `curl` instead and `_resolve_source` is standalone. Help advertises `civitai:`/`civarchive:` that `_resolve_source:63-89` rejects. Embedded (2 configs). |
| C11 | `upscalers/flashvsr/_fetch_weights.py:58-59` | documentation | yes | low but embedded (11 configs) | NEEDS DISCUSSION (batch with ND #8) | "line 175 of engines/diffusers/__init__.py" → the `.endswith(".py")` filter is at `:183`; cite the symbol instead. |
| C12 | `image_engines/luma_agents/__init__.py:196-275` ≡ `image_engines/replicate/__init__.py:115-194`; engine classes `:278-337` ≡ `:197-262` | redundant | yes (shared base) | med | NEEDS DISCUSSION | the ImageBackend→`RemoteSubmitPollBackend` adapter incl. the `ModelProfile` shim (`:220-234` / `:139-152`) is one decision in two files; a third image engine would copy it again. |
| C13 | `image_engines/replicate/__init__.py:71-101` vs `engines/replicate/__init__.py:134-162` | redundant | yes | med | NEEDS DISCUSSION | `_poll_one`/`_is_done`/`_is_failed`/`_extract_output_url` are verbatim twins of one Replicate status shape. |
| C14 | `image_engines/fal/__init__.py:42-64,191-203` vs `engines/fal/__init__.py:76-118,402-428` | redundant + inconsistent | n/a | med | NEEDS DISCUSSION | two fal poll loops and two default HTTP helper pairs; the image helpers inject `User-Agent: kinoforge/0.1` citing a fal-edge 403 (`:45-46`), the video helpers send the stdlib UA. One of the two policies is wrong or stale; decide which and share. |
| C15 | `upscalers/spandrel/_engine.py:160-220` ≡ `upscalers/flashvsr/_engine.py:224-275` (+ rife) | redundant | yes if tests keep patching per-module `_http_json` | med | NEEDS DISCUSSION | upload-local-source → POST `/upscale` → poll → `UpscaleResult` is one decision ×3 (07-16 extracted `_pod_http`; the engine-level twins remain). |
| C16 | `engines/diffusers/__init__.py:1140-1154` ≡ `engines/comfyui/__init__.py:1137-1151`; `_default_get_instance` ×2 | redundant | yes | low | NEEDS DISCUSSION (low value) | remote-provision branch (`lifecycle.boot_timeout_s` default 900 → `wait_for_ready`) duplicated; natural home is `_wait_ready.py`. |
| C17 | `servers/_av_io.py:154-162` vs `servers/_video_io.py:38-46` | redundant | yes | embedded (Modal) | NEEDS DISCUSSION | H.264/yuv420p/crf 19 encoder profile restated "so the video track is bit-comparable" — one decision in two embedded files; `write_mp4_with_audio` could call `write_mp4` (both ride on every Modal pod). |
| C18 | `image_engines/fal/__init__.py:307` | dead | no — removing the `spec.endpoint` alternative changes which `ValidationError` text fires | low | NEEDS DISCUSSION | `validate_spec` accepts `spec.endpoint`; `submit:112-116` reads only `spec.model`/`cfg.model`; `spec.endpoint` is read nowhere else (`rg`). |
| C19 | `engines/comfyui/__init__.py:1346-1348` ≡ `nodes.py:47-49` | redundant | yes | low | NEEDS DISCUSSION (low) | repo-dir-name derivation duplicated between the bash renderer and the local installer. |
| C20 | `servers/wan_t2v_server.py:1741`, `servers/minimax_h3_server.py:948` | magic / dead-ish | — | embedded | NEEDS DISCUSSION (ND #8) | `"http://localhost:8000/artifacts/…"` literal ×2; the client rebuilds the URL from its own base (`diffusers/__init__.py:635`, "server-supplied localhost:8000 URL is ignored" `:557-559`). |
| C21 | `servers/wan_t2v_server.py:1013-1033` | dead (vestigial compat shim) | no (wire compat) | embedded | NEEDS DISCUSSION (ND #8) | `_migrate_legacy_target_refs` documents itself as a "one-window transition … removed in the release after every in-flight pod has rolled"; client docstring `:716-719` says no caller ships the legacy shape. Three test files still exercise it. |
| C22 | `engines/bedrock_video/__init__.py:235` | magic | — | low | LEAVE | `(self._poll_backoff_s[-1],) * 50` — ~7 min unnamed poll budget; changing it is behaviour. |
| C23 | `servers/wan_t2v_server.py:1-8` | documentation | yes | embedded | NEEDS DISCUSSION (ND #8 already lists `:1-11`) | module docstring lists 4 of the 13 routes (`/util`, `/lora/*`, `/upscale*`, `/interpolate*`, `/upload` missing). |

---

## D. FIX NOW edit list

All items below: non-embedded files (`embedded: no`) unless stated; pre-commit `ruff format` may re-wrap
long docstring lines — the controller should let it. Test files to run first are named per item; the
whole-tree invariants (`tests/test_core_invariant.py`, `tests/test_no_unredacted_writes.py`,
`tests/test_source_audit.py`, `tests/providers`) apply to every batch per project rule.

### D-docs (docstrings / comments only)

**D1** `src/kinoforge/engines/diffusers/__init__.py:142-143` · embedded: no
old:
```
    For each dotted package name, walks the package's directory files,
    base64-encodes each .py file, and emits an ``echo '<b64>' | base64 -d
    > /tmp/kfsrv/<rel>`` line. Also ensures every ancestor namespace
```
new:
```
    For each dotted package name, walks the package's directory files,
    gzip+base64-encodes each .py file, and emits an ``echo '<b64>' | python3 -c
    "…gzip.decompress(base64.b64decode(…))" > /tmp/kfsrv/<rel>`` line (see the
    body comment on the ~101 KB env ceiling). Also ensures every ancestor namespace
```
proof: `rg -c -F 'base64-encodes each .py file' src tests docs` → src=1 tests=0 docs=0.
tests: `tests/engines/test_diffusers*.py`, `tests/providers/test_launch_payload_goldens.py` (docstring only — goldens unaffected).

**D2** `src/kinoforge/engines/diffusers/__init__.py:190` · embedded: no
old: `            # wan_t2v_server.py (~67KB raw → ~16KB gz → ~22KB b64). Pre-gzip`
new: `            # wan_t2v_server.py (~110KB raw → ~34KB gz → ~45KB b64 as of 2026-10). Pre-gzip`
(measured 111,757 / 33,874 / 45,376 B at HEAD). proof: `rg -c -F '~67KB raw' src tests docs` → 1/0/0.

**D3** `src/kinoforge/engines/diffusers/__init__.py:1159-1160` · embedded: no
old:
```
        The script is minimal: pip-install declared deps then ``exec``
        the server command so it becomes PID 1.  Credentials are not
```
new:
```
        The script is minimal: pip-install declared deps then launch the
        server command WITHOUT ``exec`` — bash stays PID 1 so the EXIT trap
        can fire (``Launch(exec_pid1=False)`` below).  Credentials are not
```
proof: `rg -n -F 'so it becomes PID 1' src tests docs` → `src/kinoforge/engines/diffusers/__init__.py:1160` plus `core/interfaces.py:213` (the `exec_pid1` field doc — correct there) and `tests/engines/test_comfyui_render_provision.py:150` (ComfyUI, which does `exec` — unrelated). tests: `tests/engines/test_diffusers_render_provision*.py`.

**D4** `src/kinoforge/engines/comfyui/__init__.py:1537` · embedded: no
old: `        """Raise ``NotImplementedError`` — deferred to Task 12.`
new: `        """Raise ``NotImplementedError`` — profiles come from ``ModelProfileProvider``, never the engine.`
Leave the exception message string at `:1546` unchanged (user-facing). proof: `rg -c -F 'deferred to Task 12' src tests docs` → src=2 (this + fake) / 0 / docs=1 (prior audit). tests: `tests/engines/test_comfyui.py`.

**D5** `src/kinoforge/engines/fake/__init__.py:14`, `:249-257` · embedded: no
old `:14`: `* ``profile_for`` is deferred until Task 12 wires ``ModelProfileProvider``.`
new: `* ``profile_for`` always raises — profiles come from ``ModelProfileProvider``, never the engine.`
old `:249-251`:
```
        """Raise ``NotImplementedError`` — deferred to Task 12.

        # DEFERRED: profile_for is supplied by ModelProfileProvider.resolve in Task 12.
```
new:
```
        """Raise ``NotImplementedError`` — profiles come from ``ModelProfileProvider``.
```
old `:257`: `            NotImplementedError: Always. Task 12 wires the real cache.` → new: `            NotImplementedError: Always.`
Leave `:260` message text. proof: `rg -n -F 'Task 12' src/kinoforge/engines/fake/__init__.py` → 5 lines (14, 249, 251, 257, 260 — 260 is the exception text to keep; edit the other four). tests: `tests/engines/test_fake*.py`.

**D6** `src/kinoforge/image_engines/luma_agents/__init__.py:170-173` · embedded: no
old:
```
        Documented shape (2026-07-03): ``output`` is an array of
        ``{"url": ...}``. The earlier dream-machine shape
        (``assets.image``) is kept as a fallback in case the docs and
        the wire disagree — the live smoke settles which one is real.
```
new:
```
        Live-confirmed shape (2026-07-04, successful-generations #15):
        ``output`` is an array of ``{"url": ...}``. The earlier
        dream-machine shape (``assets.image``) is kept as a fallback only.
```
proof: `rg -c -F 'the live smoke settles which one is real' src tests docs` → 1/0/1 (prior audit). tests: `tests/image_engines/test_luma_agents*.py`.

**D7** `src/kinoforge/image_engines/replicate/__init__.py:104`, `:111` · embedded: no
old `:104`: `        """Scaffold for the ``_delete`` ABC; concrete impl lands in Task 17."""`
new: `        """Not implemented: no delete path on this adapter; ephemeral is refused at preflight (``core/ephemeral.py``)."""`
old `:111`: `        """Scaffold for the ABC; concrete impl lands in Task 17."""`
new: `        """Browser URL for the prediction; the only cleanup path this adapter offers."""`
Leave `:106` exception text. proof: `rg -c -F 'lands in Task 17' src tests docs` → 2/0/1. tests: `tests/image_engines/test_replicate*.py`.

**D8** `src/kinoforge/engines/comfyui/nodes.py:28-31` · embedded: no
old:
```
    2. Issues ``git clone <url> <dest_dir>`` via *run_cmd*.
    3. Checks whether ``<dest_dir>/requirements.txt`` exists using
       *file_exists*; if it does, runs ``pip install -r <path>`` via
       *run_cmd*.
```
new:
```
    2. Issues ``git clone <url> <dest_dir>`` via *run_cmd*.
    3. If the entry carries ``"ref"``, runs ``git checkout <ref>`` in
       ``<dest_dir>`` via *run_cmd*.
    4. Checks whether ``<dest_dir>/requirements.txt`` exists using
       *file_exists*; if it does, runs ``pip install -r <path>`` via
       *run_cmd*.
```
proof: `rg -c -F 'Checks whether ``<dest_dir>/requirements.txt`` exists using' src tests docs` → src=1 (`nodes.py`) / tests=0 / docs=0. tests: `tests/engines/test_comfyui.py` (`:1572`, `:1605`).

**D9** `src/kinoforge/engines/replicate/__init__.py:64` · embedded: no
old: `    default 10 s) to ride out the Replicate throttle-when-credit-< $5`
new: `    default 12 s) to ride out the Replicate throttle-when-credit-< $5`
proof: `rg -c -F 'default 10 s) to ride out' src tests docs` → 1/0/0. tests: `tests/engines/test_replicate*.py`.

**D10** `src/kinoforge/image_engines/replicate/__init__.py:47-50` · embedded: no
old:
```
    Differs from the video backend in two places:
    - ``_extract_output_url`` unwraps the single-image flux-schnell shape
      (``output`` is either a list with one URL or a bare URL string).
    - Asset injection is a no-op — image-from-text only.
```
new:
```
    Differs from the video backend in one place: asset injection is a
    no-op (image-from-text only). ``_poll_one`` / ``_is_done`` /
    ``_is_failed`` / ``_extract_output_url`` mirror ``ReplicateBackend``.
```
proof: `rg -c -F 'unwraps the single-image flux-schnell shape' src tests docs` → 1/0/0.

**D11** `src/kinoforge/engines/hosted/__init__.py:3-4`, `:17-25` · embedded: no
old `:3-4`:
```
Validates credentials and endpoint availability, then delegates generation
to a remote hosted inference API (e.g. fal.ai).  No GPU instance is ever
```
new:
```
Validates credentials and endpoint availability, then delegates generation
to a user-deployed inference shim speaking the ``/generate`` +
``/status/{job_id}`` contract below (NOT a vendor API — fal.ai has its own
engine under ``engines/fal``).  No GPU instance is ever
```
old `:17-21` example block: replace `provider: fal`, `https://fal.run/fal-ai/ltx-video`, `FAL_KEY`, `https://fal.run/health` with `provider: my-shim`, `https://your-shim.example.com/generate`, `MY_SHIM_API_KEY`, `https://your-shim.example.com/health` (mirrors `examples/configs/hosted.yaml:21`).
proof: `rg -c -F 'e.g. fal.ai).  No GPU instance' src tests docs` → 1/0/0; `rg -n 'fal.run' tests/engines/test_hosted*.py` → confirm 0 before editing the example. tests: `tests/engines/test_hosted.py`.

**D12** `src/kinoforge/engines/fal/__init__.py:285-289` · embedded: no
old:
```
        Pre-flight (Task 18) refuses ephemeral for the fal engine before
        any submit fires, so this branch is belt-and-suspenders. Surface
        ``EphemeralDeleteUnsupportedError`` rather than silently no-op so
        a buggy caller bypassing pre-flight cannot quietly leak the
        prompt-laden record.
```
new:
```
        Pre-flight refuses ephemeral for the fal engine before any submit
        fires. Nothing calls this today — ``FalBackend`` does not inherit
        ``RemoteSubmitPollBackend``, whose ``_delete_with_retries`` is the
        only ``_delete`` caller — so it documents intent, not a live path.
```
proof: `rg -c -F 'cannot quietly leak' src tests docs` → 1/0/1 (prior audit). tests: `tests/engines/test_fal*.py`.

**D13** `src/kinoforge/engines/_wait_ready.py:104-105` · embedded: no
old:
```
        except Exception:  # noqa: BLE001, S110
            pass
```
new:
```
        except Exception:  # noqa: BLE001, S110 — any failure here means "not ready yet"; the pod-status + liveness checks below decide whether to keep waiting
            pass
```
(let `ruff format` wrap). proof: `rg -c -F 'except Exception:  # noqa: BLE001, S110' src/` → 4 hits across src (`_wait_ready.py:1`, `src/sitecustomize.py:1`, `providers/skypilot/__init__.py:2`) — unique WITHIN `_wait_ready.py`, so scope the `Edit` to that file; tests=0. tests: `tests/engines/test_wait_ready*.py`, `tests/engines/test_diffusers*ready*.py`, `tests/engines/test_comfyui.py`.

**D14 (minor)** `src/kinoforge/engines/diffusers/__init__.py:59-61` · embedded: no
old:
```
#: Maximum poll iterations in :meth:`DiffusersBackend.result`. Each
#: iteration sleeps 1 s, so this is the total seconds the backend
#: will wait for a single job to complete. Bumped from 60 (which fit
```
new:
```
#: Belt-and-braces iteration cap in :meth:`DiffusersBackend.result`;
#: the wall-clock bound is ``poll_timeout_s`` (constructor). At the
#: default 1 s interval the two coincide. Bumped from 60 (which fit
```
proof: `rg -c -F 'so this is the total seconds the backend' src tests docs` → 1/0/0. tests: `tests/engines/test_diffusers_result_loop.py` (patches `_MAX_POLL`; comment only).

### D-code (behaviour-preserving refactors)

**D15** hoist `resolve_prompt` imports · embedded: no · fence: `3bfb2e0e` (fal refactor that introduced the local import + comment); `core/prompt_routing.py:15` imports only `core.interfaces`, so no cycle; `engines/replicate/__init__.py:32` and `engines/bedrock_video/__init__.py:42` already do the module-level import.
- `src/kinoforge/engines/fal/__init__.py`: add `from kinoforge.core.prompt_routing import resolve_prompt` after `:44`; delete `:330-332` and `:639-641` (the two `from kinoforge.core.prompt_routing import (\n    resolve_prompt,  # local — avoid circular at module load\n)` blocks).
- `src/kinoforge/engines/hosted/__init__.py`: add the import after `:50`; delete `:296-298` and `:742`.
- `src/kinoforge/engines/diffusers/__init__.py`: add the import after `:50`; delete `:505-507` and `:1630`.
- (optional, same reason) `src/kinoforge/engines/comfyui/__init__.py:707`, `:1601`.
proof: `rg -c -F 'avoid circular at module load' src tests docs` → 4/0/1 (docs hit is a plan file); `rg -n 'prompt_routing|resolve_prompt' tests/ | rg -i 'patch|setattr'` → 0 (no test patches the name by path). tests: `tests/engines/test_fal*.py`, `tests/engines/test_hosted.py`, `tests/engines/test_diffusers*.py`, `tests/engines/test_comfyui.py`, `tests/core/test_prompt_routing.py`, plus `tests/test_core_invariant.py`.

**D16** `src/kinoforge/engines/_pod_http.py:235-242`, `:272` · embedded: no (the pod-side `upscalers/*/__init__.py` import of `_engine` → `_pod_http` is already swallowed by their `except ImportError`).
old:
```
        body = local_path.read_bytes()
        local_sha = hashlib.sha256(body).hexdigest()
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": content_type,
            "X-Filename": f"{short}{suffix}",
            "Content-Length": str(len(body)),
```
new:
```
        size = local_path.stat().st_size
        local_sha = sha256_file(local_path)
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": content_type,
            "X-Filename": f"{short}{suffix}",
            "Content-Length": str(size),
```
and `:272` `                bytes_sent=len(body),` → `                bytes_sent=size,`; add `from kinoforge.core.downloader import sha256_file` to the imports (`:26-29`); drop `import hashlib` if it becomes unused (ruff F401 will say). `sha256_file(path: Path) -> str` streams (`core/downloader.py:67`).
proof: `rg -n 'read_bytes' tests/upscalers tests/interpolators tests/text_engines tests/engines/diffusers/test_upload_module.py` → check no test patches `Path.read_bytes` on the upload path (none found by name; `_put_upload` is what they patch). tests: `tests/upscalers/test_spandrel_upload.py`, `tests/upscalers/test_spandrel_image_upload.py`, `tests/upscalers/flashvsr/test_engine.py`, `tests/interpolators/test_rife_engine.py`, `tests/text_engines/test_transformers_engine.py`, `tests/engines/diffusers/test_upload_module.py`.

---

## E. Needs discussion

1. **NB1 timeouts** — decide one timeout policy for the controller→pod seams (`diffusers`/`comfyui`
   `_urllib_*`, `hosted`, both `fal` helper pairs). `_pod_http.py:33-37` already encodes 60 s (JSON) /
   600 s (upload); reusing those constants is the obvious choice. Behaviour change: a stalled proxy now
   raises `TimeoutError` (an `OSError` → already in `RUNPOD_PROXY_POLICY.catch_classes`, so the retry
   ladder absorbs it) instead of hanging. Pairs with prior B8/B9/B21.
2. **NB2 `_ensure_on_gpu`** — wrap `_free_headroom_for`, `_load_model_to_gpu`, `.to("cuda")` in
   `asyncio.to_thread` inside the lock (same shape as `_lora.py:830`). Embedded → goldens move; fold into
   ND #8's batch. Also resolves half of NB6 if the registry mutation is moved onto one lock.
3. **NB3 / the `/upload` keep** — either migrate wan onto `_upload.receive_upload` (moves 16 configs'
   `embed_files` + goldens, per the keep) or back-port the two `e6041bdd` fixes into the inline copy (also
   moves goldens, smaller). Either way the hygiene-notes keep text should drop "for no behaviour change".
4. **NB4 ComfyUI ref line** — render `git checkout` as its own statement (or `(cd … && git checkout …)`
   in a subshell) so `set -e` applies and the cwd cannot leak. Changes rendered bytes → goldens; a
   `test_comfyui_render_provision.py:110` string pin moves.
5. **NB5 hosted `health_url`** — either guard with `if health_url:` like fal (`fal:554`) or make the
   schema field required; the current default + unconditional ping cannot both be right (schema is
   cross-area).
6. **ND #9 `del cancel_token` ×6** — `FalBackend`/`HostedAPIBackend`/`BedrockVideoBackend` could
   inherit `RemoteSubmitPollBackend` and get cancellation + ephemeral delete for free; the "deferred to a
   future task" comments are 4+ months old.
7. **ND #10 registration swallow** — replace `except UnknownAdapter: pass` with a reload-aware check
   (`if name not in registry…`) or let it raise; `core/registry.py:278-280` promises loudness the
   registrants defeat. Also decide whether `UnknownAdapter` is the right type for "already registered".
8. **ND #11 image `_delete`** — raise `EphemeralDeleteUnsupportedError` (the ABC's documented type) so
   `_delete_with_retries` can surface a manual-cleanup URL instead of dropping the artifact; or catch
   `NotImplementedError` there. Only reachable programmatically today.
9. **C12–C15 adapter twins** — one `_SubmitPollImageBackend` base for luma/replicate images; one fal
   HTTP helper pair + poll loop; one pod-upscale `upscale()` body. Each is medium risk because tests patch
   per-module seams (`_http_json`, `http_post`/`http_get`).
10. **C17 encoder profile** — `_av_io.write_mp4_with_audio` calling `_video_io.write_mp4` for the video
    track (both files ride every Modal pod) removes the duplicated libx264/yuv420p/crf-19 decision;
    embedded → goldens.
11. **ND #8 batch additions** from this pass: C11 (flashvsr line pointer), C20 (localhost URL ×2), C21
    (`target_refs` shim — tests to delete alongside), C23 (route list), NB2, and the B18 server-side
    rollback boundary (port `_lora.py:251-294`'s single `try/except BaseException: unload; raise`
    around `_replace_adapter_stack`'s load loop).
12. **Keep re-evaluation** — hygiene-notes entry "`wan_t2v_server.py:2702-2782` inline `/upload` beside
    `servers/_upload.py` … for no behaviour change": the second clause is now false (NB3). Recommend
    rewording the keep to "migration moves goldens; the inline copy has since drifted (see audit
    2026-10-21 NB3)".

---

## F. Cross-area notes

- `core/config.py:334,370` — `HostedEngineConfig.health_url` documented as pinged, defaults `""` (NB5).
- `core/config.py:731-751` — `FlashVSREngineConfig` documents `window_size`/`tile_size`/`long_video_mode`
  as live knobs; the pod ignores them (B19). `:687-697` same for spandrel `tile_size`/`batch_size`.
- `core/remote_backend.py:221-226` — `_delete_with_retries` catches only `EphemeralDeleteHTTPError`;
  a `NotImplementedError` from an image `_delete` escapes after the job completed (ND #11 sharpening).
- `core/orchestrator.py:1451-1453,1874-1876` — the `boot_timeout` → `boot_timeout_s` lift is what makes
  both engines' `lifecycle_block.get("boot_timeout_s", 900.0)` correct; keep them in step.
- `docs/engines.md:606` still documents `python -m kinoforge.upscalers.spandrel._fetch_weights`, which is
  production-dead (07-16 ND #12).
- `docs/hygiene-notes.md` keep for the wan inline `/upload` needs its "no behaviour change" clause
  revised (NB3).
- `engines/bedrock_video/__init__.py:244-247` is the producer of the `s3://…/output.mp4` artifact URI
  that prior B9 says `pipeline/artifact_bytes.py:70-74` turns into fabricated bytes — the reachable path
  for that bug is a Bedrock run.
- `tests/engines/test_comfyui_render_provision.py:110` pins the exact `git checkout … && cd ../..`
  string (NB4 fix moves it).
- `core/ephemeral.py:132-133` is what keeps ND #11 narrow (image engines refused at preflight).
