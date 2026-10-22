# Hygiene re-audit 2026-10-21 — tests area 2 (engines / providers / stores / validation / sources / tools / upscalers / live / smoke / harness / image+text engines / diagnostics / interpolators / examples)

HEAD `f89eff2b`, read-only. Installed pytest: **9.0.3**. Scanned 2,465 test functions across
~86k lines in the area (AST sweep, read-only script in the scratchpad). Every claim below was
verified against the file at HEAD; empirical runs were limited to `tests/smoke/local_cpu`,
`tests/_smoke_harness`, `tests/providers`, `tests/validation` (never `tests/live`, never
`KINOFORGE_LIVE_TESTS`).

---

## A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line) |
|---|---|---|---|
| Finding #0 / B1 hiding place | `tests/tools/test_smoke_leak_sweep.py:12-22` fakes carry `created_at`/`smoke_tier` the real provider never produces | **CONFIRMED** | `:20` `self.created_at = time.time() - created_h_ago * 3600`, `:21` `self.tags = {"smoke_tier": tag}`; production `providers/runpod/__init__.py:1929-1972` `_pod_to_instance` (post-`58d27ef0` numbering) → `tags={"mode":"pod"}` (+`name`), `created_at=0.0` at `:1970`. All four tests (`:33,:54,:73,:93-94`) use non-zero ages; none uses `0.0`. `smoke_tier` is written only in `examples/configs/*.yaml` `compute.tags` and RunPod never sends `spec.tags` (only reads `mode`, `:741`) |
| Finding #0 (tail) | `tests/_smoke_harness/runpod_lifecycle.py:110` `tag_filter` can never match | **CONFIRMED** | `:110` `inst.tags.get("smoke_tier") != tag_filter` over `provider.list_instances()` → same `_pod_to_instance`. Both live callers already avoid it (`tests/smoke/live_wan21/test_lora_swap_matrix.py:150` "No tag_filter: cfg.compute.tags is not currently propagated"). Its unit test `tests/_smoke_harness/test_runpod_lifecycle.py:52-70` fakes `smoke_tier` tags — a second copy of the #0 hiding place (prior audit did not name it) |
| B20 | `pixi run test-live` sets `KINOFORGE_LIVE_TESTS=1` itself and `tests/live/conftest.py` auto-loads `.env` | **CONFIRMED** | `pixi.toml:132` `env = { KINOFORGE_LIVE_TESTS = "1" }`; `tests/live/conftest.py:39-42` module-level `load_env_file(env_file)`. 88 modules under `tests/live` (prior said ~48) |
| P3 (c30) | `c30_probe.destroy_with_retry` used by 10 teardown sites in `tests/live/conftest.py` | **PARTIAL** | 16 occurrences in `tests/live/conftest.py` (count differs); behaviour is live-only — cannot verify offline |
| FIX NOW docs | `tests/providers/test_skypilot.py:1160-1168,1180` stale T4 skip reason + nonexistent path | **CONFIRMED** | `:1160-1163` `skipif(not (GPU_FIXTURE_DIR / "list_accelerators.json").exists(), reason="T4 GPU fixtures not captured yet — Layer W+β T4 will land them")` — the file exists (`tests/providers/fixtures/skypilot/gpu/list_accelerators.json`), so the skip never fires; `:1180` cites `tests/conftest_runpod.py` which does not exist (it is `tests/providers/conftest_runpod.py`) |
| FIX NOW docs | `tests/engines/diffusers/servers/test_set_stack_async_job.py:19-22` cites pytest-randomly (not installed) | **CONFIRMED** | `:20` "pytest-randomly is active"; `importlib.util.find_spec("pytest_randomly") is None` in the default env; the only `pixi.lock` mention (`:18588`) is another package's optional extra |
| FIX NOW docs | `tests/providers/test_heartbeat_parity.py:31-32` says fakes live in `tests/providers/conftest.py` | **CONFIRMED** | `:31-32` comment; `:33` imports `from tests.conftest import LocalHeartbeatEndpoint` |
| dead | `tests/providers/conftest.py` re-export nothing imports | **CONFIRMED** | `:15-24` re-exports three fakes + `__all__`; `rg 'tests\.providers\.conftest' tests/` → 0 importers (the `from .conftest import` hits are `tests/live/`). Fence `e85bd377` "feat(b5a): heartbeat substrate…" (the b5a plan `:2212` said "Append to tests/providers/conftest" but the fakes landed in the root) |
| dead | `tests/smoke/conftest.py:36-38` redaction clear subsumed by root autouse | **CONFIRMED** | root `tests/conftest.py:117-128` `@pytest.fixture(autouse=True) _clear_redaction_registry_between_tests` clears before AND after; smoke `:36-38` clears before only |
| dead | `tests/smoke/conftest.py:31` `config.option.log_cli = True` (ini-only in pytest 9) | **PARTIAL** | Line 31 IS dead: pytest 9.0.3 `_pytest/logging.py:759-763` `_log_cli_enabled()` reads `getoption("--log-cli-level") is not None or getini("log_cli")` — nothing reads `option.log_cli`. But lines 32-33 (`config.option.log_cli_level = "WARNING"`) are LIVE: `--log-cli-level` has `dest="log_cli_level"` (`:278`), and because logging's `pytest_configure` is `trylast=True` (`:636`) the conftest hook runs first when `tests/smoke/...` is an initial arg. **Empirically verified**: `pytest tests/smoke/local_cpu/test_stub_pipe.py tests/_smoke_harness/test_runpod_lifecycle.py -k failures` prints `live log call … WARNING … failed to destroy bad`; the same test without a `tests/smoke` path prints nothing. Delete line 31 only; the hook is not dead |
| dead | `tests/providers/conftest_runpod.py:52-58` `_OPERATION_TABLE` | **CONFIRMED** | sole reference is the definition; the same operation→fixture mapping is implemented as if/elif at `:446-454`. Fence `c38dde70` (Layer N plan) |
| dead | ten unreferenced test helpers (in-area: `HttpGetSpy`, `_FakeS3`, `_PROBE_*_SCRIPT`) | **CONFIRMED + extended** | `tests/providers/test_runpod.py:190-201` `HttpGetSpy` (1 ref = def; fence `9f51d74e`); `tests/diagnostics/test_c33_orchestrator.py:28-30` `_FakeS3` (1 ref; fence `a44fbdda`); `tests/providers/test_field_consumption_parity.py:732-734` — THREE dead constants `_PROBE_SCRIPT`, `_PROBE_BUILD_SCRIPT`, `_PROBE_RUNTIME_SCRIPT` (1 ref each; `_PROBE_TAGS` at `:735` is used ×5; fence `a72c5401`). The other seven helpers are in `tests/core` (not my area) |
| redundant | S4 catalog fakes `_S4_GPU_TYPES` ×8 / `_S4_ACCELERATORS` ×4 / `_create_body` ×5 | **CONFIRMED** | in-area: `_S4_GPU_TYPES` in `test_runpod_setup_steps.py:27`, `test_runpod_capacity_error.py:15`, `test_runpod_create_pod_restart_policy.py:33`, `test_runpod_create_pod_cloud_type.py:25`, `test_runpod_provision_script.py:15`, `test_runpod_create_pod_diagnostic_env.py:61` (+2 in `tests/core`); `_S4_ACCELERATORS` `test_skypilot_provision_script.py:13`, `test_skypilot_tunnel.py:21` (+2 core); `_create_body` ×5 all in `tests/providers` |
| redundant | wan-server snapshot/restore autouse ×4, none covers `_swap_jobs`/`_LOADED`/`_interpolate_jobs` | **CONFIRMED** | identical bodies snapshotting only `_pipe_arity` + `_inventory`: `tests/engines/test_cold_boot_branch_validation.py:36-44`, `test_lora_http_branch_surface.py:27-34`, `test_replace_adapter_stack_routing.py:21-30`, `test_vram_rollback_branch.py:29-36`. (`test_wan_coresident_swap.py:39-53` separately covers `_LOADED`/`_WAN_REGISTRY_NAME`; `test_lru_eviction.py:44-50` covers `_LOADED`.) Nothing restores `_swap_jobs`, `_interpolate_jobs`, `_upscale_jobs` |
| ND #20 | `tests/live/conftest.py:34-42` loads `.env` at collection, before `-m 'not live'` deselects | **CONFIRMED** | module-level, not a hook (`:39-42`); `pixi run test` = `python -m pytest -m 'not live'` with no path args (`pixi.toml:126`) so `tests/live/` is collected and the conftest imported; `-m` deselection runs after collection. Extra: `:30` imports `kinoforge.providers.runpod.util` UNGUARDED above the `try` at `:34-37`, so the docstring's "silent … [if] the kinoforge package isn't importable" (`:12-13`) is false and the `except` arm is unreachable |
| ND #20 | reload-pattern hazard: 10 `importlib.reload(srv)` sites; 5 files top-level-import from `wan_t2v_server` | **PARTIAL** | 10 sites confirmed (`tests/engines/diffusers/test_server_upscale_image.py:22`, `test_server_upscale.py:43`, `test_server_worker_shutdown.py:43`, `tests/engines/test_diffusers_wan_t2v_server.py:39,93,103,138,207,253`, `test_wan_t2v_server_torch_build_log.py:63`). Top-level importers are **15** repo-wide (11 in this area), not 5 — but only `tests/engines/diffusers/test_wan_t2v_server_strength.py:17-20` binds names (`LoraTarget`, `SetStackRequest`); the rest bind the module object, which `reload` mutates in place. "Safe today" stands |
| keep | `tests/validation/test_shipped_configs_capabilities.py:77-83` non-recursive glob (documented convention) | **CONFIRMED keep** | `:73-74` + docstring `:78-82`. New evidence (not against the keep): `:17` `CONFIG_DIR = Path("examples/configs")` is CWD-relative — from `/tmp` the parametrize yields "got empty parameter set" SKIP (empirical) |
| keep | 12 always-raise contract live tests under `xfail` | **CONFIRMED keep** | 12 files in `tests/live` carry `pytest.mark.xfail`; e.g. `test_modal_wan_t2v_1_3b.py:15-19` `raise AssertionError("run GENERATE_CMD live; …")` with the command at `:9-12` |
| keep | `tests/providers/runpod/test_heartbeat.py:54-63` `_C33_XFAIL` strict=True | **CONFIRMED keep** | `:54-62`; run: `6 passed, 6 xfailed` |
| keep | `tests/stores/live/test_{s3,gcs}_live.py` strict=False with documented causes | **CONFIRMED keep** | `test_s3_live.py:155-161` (SigV4 Host binding), `test_gcs_live.py:153-163` |
| keep | `test_skypilot_{vast_compat,sitecustomize}.py` `importorskip("vastai_sdk")` — no task runs them | **CONFIRMED keep** | `test_skypilot_vast_compat.py:7`, `test_skypilot_sitecustomize.py:17`; the only `live-skypilot` pytest task is `test-live-skypilot` → `tests/live/test_skypilot_live.py` (`pixi.toml:138`) |
| keep | `tests/providers/test_launch_payload_goldens.py:119` `pytest.raises(Exception)` over empty `EXCLUDED_CONFIGS` — vacuous today | **CONFIRMED keep** | `tools/snapshot_launch_payloads.py:80` `EXCLUDED_CONFIGS: dict[str, str] = {}`; the loop at `:114-120` never enters |
| keep | `tests/smoke/local_cpu/test_lora_swap_matrix.py:72` thin outer assert | **CONFIRMED keep** | `:72` `assert len(report.steps) == 4` |
| keep | mega `tests/engines/test_comfyui.py` ~2.0k | **CONFIRMED** | 1,974 lines |
| keep / ND #5 | `tests/live/cfg_b3_warm_attach.yaml:13` "BudgetTracker tears down mid-run" is false | **CONFIRMED** | `:13` present verbatim |
| keep | `smoke:` cfg block read by `tests/smoke/live_wan21/*` | **CONFIRMED keep** | `test_lora_swap_matrix.py:97`, `test_branch_routing.py:142` read `cfg["smoke"]["lora_a"]` |
| test strength | `tests/providers/test_runpod_name_resolution.py:216-232` fake records `<unmatched>` for any id, so a wrong-id destroy passes | **CONFIRMED** | `_Transport.__call__` `:77-85`: a `podTerminate` whose id is not in the listing appends the literal `"<unmatched>"`; `:225-226` asserts `terminated == ["<unmatched>"]`, so a provider that terminated `""`, the NAME, or any other wrong id passes identically |
| test strength | Six discovery guards with no plausible-count floor; three root at CWD-relative paths and pass with zero inputs from another directory | **CONFIRMED + extended** | Empirical run from `/tmp` (`--rootdir=/workspace -o addopts=""`): **pass vacuously** — `test_provider_selection.py:165 test_no_source_file_still_reads_spec_offer`, `test_skypilot.py:203-205 test_ac1_no_top_level_sky_import_anywhere_in_src`, `test_launch_payload_goldens.py` `test_every_compute_config_has_a_golden`/`test_no_orphan_goldens`/`test_excluded_configs…`/`test_goldens_carry_no_credential_shapes`; **silently become "empty parameter set" skips** — `test_shipped_configs_capabilities.py:73`, `modal/test_catalog_no_regression.py:31`, `test_field_support_check.py:673,688`, `test_launch_payload_goldens.py:63`. Guards WITH floors (good): goldens `:53 >=5`, `:146 ==1`, `:181 >=3`, `:214 >=3`; `test_catalog_no_regression.py:10 >=5`; `test_h200_rate_cap.py:68 assert caps`; `test_bakeable_steps_set_u_safe.py:197 >=20`; `test_env_payload_ceiling.py:309-330` (symmetric cross-check vs `_BASELINE_BYTES`). No floor and `__file__`-anchored: `test_fixtures_audit.py:36-48` (139 JSON fixtures today), `test_diffusers_optional_cfg_defaults.py:160-175` |
| ND #24 | `kinoforge list` teardown check re-encoded ×8 in live files | **CANNOT-VERIFY-OFFLINE** | the literal appears in 14 `tests/live/*.py` modules + 1 `tests/smoke/live_wan21` module (+ evidence `.txt`); exact copy count and behaviour are live-only |
| AGENTS.md pointer | `_CREDENTIAL_PATTERNS` in `tests/providers/conftest_runpod.py` is now an alias | **CONFIRMED** (cross-area doc) | `conftest_runpod.py:31` `CREDENTIAL_PATTERNS as _CREDENTIAL_PATTERNS` |

---

## B. New bugs (missed by the prior audit)

**B-T1 (P3, latent — live harness only).** `tests/_smoke_harness/runpod_lifecycle.py:106-110`
`destroy_all_active_pods(tag_filter=...)` is the inverse of Finding #0: with a filter set it
destroys NOTHING and reports clean. Scenario: an operator (or a future smoke) calls
`destroy_all_active_pods(tag_filter="kinoforge-smoke-tier-3")` while a tier-3 pod is running →
`list_instances()` yields `tags={"mode":"pod","name":…}` → every pod fails the `smoke_tier`
comparison → returns `SweepResult(destroyed=[], failures={})` → the caller logs a clean sweep
while the pod bills. No caller passes `tag_filter` today (both live smokes comment it out),
and `test_runpod_lifecycle.py:52-70` passes only because its fake supplies the tag.
Introduced `3eef7ed3` (harness) on top of `9f51d74e` (converter); same root as #0.

Nothing else rose to a bug with a concrete failure scenario. Specifically checked: every
provider fake's `Instance` shape vs the three list converters (RunPod `:1929-1972`, Modal
`:411-424`, SkyPilot `:557-562` — all `created_at=0.0`; only one fake in the area sets a
non-zero `created_at` on a RunPod `Instance`, `test_runpod_boot_grace_from_timeout.py:111`,
and that models the CREATE path where production does stamp `time.time()` at `:1608,1670`);
GraphQL `errors` shapes (fakes use `errors: [{"message"}]` ×18 and exactly one `errors: []`,
`test_runpod.py:936`, which only covers `_unwrap_graphql_response` — the B11 `"errors" in resp`
sites have no `[]` fake, consistent with B11 being open); RunPod `desiredStatus` (fakes:
RUNNING ×67, STARTING, CREATED; EXITED/DEAD appear only in the set literal at
`test_runpod.py:1676`); SkyPilot statuses (UP/INIT/TERMINATING/`ClusterStatus.*` — parser
accepts both spellings); hosted statuses (replicate `succeeded/failed`, runway
`SUCCEEDED/FAILED`, fal `COMPLETED/IN_PROGRESS/FAILED`, bedrock `Completed/Failed` all
mirrored by fakes); diffusers job statuses (`done`/`running`/`error`/`queued` mirrored);
`tests/stores/live` and `tests/live` swallowed-exception sites (all teardown best-effort,
documented); the 37 assertion-less test functions (all "must not raise" contracts; one is a
fake method `test_validate_scoped_policy.py:810`, not a test).

---

## C. New hygiene findings

| # | location | category | behaviour-preserving? | risk | call | rationale |
|---|---|---|---|---|---|---|
| C1 | `tests/providers/test_launch_payload_goldens.py:4` | documentation | yes (docstring) | low | FIX NOW | says "the 31 goldens"; `compute_configs()` returns 44 and `tests/providers/golden/launch_payloads/` holds 44 |
| C2 | `tests/providers/test_field_consumption_parity.py:732-734` | dead | yes | low | FIX NOW | `_PROBE_SCRIPT`, `_PROBE_BUILD_SCRIPT`, `_PROBE_RUNTIME_SCRIPT` each referenced once (the definition); prior audit listed only two |
| C3 | `tests/live/conftest.py:12-13` + `:30` vs `:34-37` | documentation / dead | docstring fix yes; moving `:30` under the `try` NO (changes collection failure mode) | low (doc) | FIX NOW (docs) / E6 (code) | "silent … [if] the kinoforge package isn't importable" is false — `:30` `from kinoforge.providers.runpod.util import _default_http_post` runs unguarded first; the `except` at `:36` is unreachable |
| C4 | `tests/_smoke_harness/test_runpod_lifecycle.py:52-70` | test strength (fake ≠ production shape) | no (fixing it turns the test red) | med | NEEDS DISCUSSION (E1) | fake `_FakeInstance("t3", {"smoke_tier": …})` supplies a tag `_pod_to_instance` cannot produce; see B-T1 |
| C5 | `tests/tools/test_smoke_leak_sweep.py:12-22` (+ all four tests) | test strength (fake ≠ production shape) | no (a production-shape case is RED today — correctly) | med | NEEDS DISCUSSION (E1) | prior FIX NOW item; the fix is a RED test, so it belongs with the Finding #0 code fix, not a hygiene pass |
| C6 | `tests/providers/modal/test_provider.py:100-106` | test strength (fake ≠ production shape) | n/a | med | NEEDS DISCUSSION (E2) | list fake records use `"name"`; real `modal app list --json` uses `"description"` (per `providers/modal/__init__.py:401-402`, and `test_modal_destroy_by_app_id.py:81,115,142,168,203,246` use the real key). The `name` fallback in `_rec_name` exists only for this fake |
| C7 | `tests/engines/test_comfyui_poll_log.py:81` | test strength (fake value) | yes if changed to `"success"` (code never branches on `"complete"`) | low | LEAVE | `status_str: "complete"` is not a ComfyUI value (`success`/`error`); harmless, note only |
| C8 | `tests/engines/` (ComfyUI backend) | coverage gap | n/a | — | NEEDS DISCUSSION (E8) | no test in the area fakes `status_str: "error"`; `engines/comfyui/__init__.py:887` (`last_status == "error"`, `_logged_error_once`) is untested — the exact seam B5 hid behind |
| C9 | `tests/validation/test_shipped_configs_capabilities.py:17` | structural (CWD-relative discovery root) | yes under `pixi run` (same directory); ids use `p.name` so node ids are unchanged | low | FIX NOW | from another CWD the whole parametrize silently skips |
| C10 | `tests/validation/test_field_support_check.py:37` | structural (CWD-relative) | NO — `:673,688` use `ids=str` over the path, so anchoring changes node ids | low | NEEDS DISCUSSION (E3) | same vacuity; needs relative ids kept |
| C11 | `tests/providers/test_provider_selection.py:165`, `tests/providers/test_skypilot.py:203` | test strength ("guard the guard") | adding a floor is a failure-direction-only change | low | NEEDS DISCUSSION (E3) | both `src/kinoforge` scans pass with zero files from another CWD (empirical) |
| C12 | `tests/providers/test_fixtures_audit.py:36-48`, `tests/engines/test_diffusers_optional_cfg_defaults.py:160-175`, `tests/providers/test_launch_payload_goldens.py:45 _CONFIGS` | test strength ("guard the guard") | floor = failure-direction only | low | NEEDS DISCUSSION (E3) | no plausible-count floor; 139 JSON / 44 configs today |
| C13 | `tests/providers/test_runpod.py:1670-1674` | test strength | n/a | — | LEAVE (documented) | lockdown test `pytest.skip`s when fixtures carry no `desiredStatus` (the failure the guard exists for); passes today (`1 passed`) because `get_pod.json` holds RUNNING |
| C14 | `tests/providers/test_runpod_name_resolution.py:77-85` | test strength | fix is behaviour-preserving for the suite only if the id parse matches today's mutation text | low | NEEDS DISCUSSION (E4) | prior row confirmed; fake should record the id parsed from `podTerminate(input: {podId: …})` |
| C15 | `tests/_smoke_harness/test_runpod_lifecycle.py:85,117` | documentation | yes | low | LEAVE | "the smoke-tier `log_cli` flip still prints this" is true only when a `tests/smoke/...` path is also an initial arg (true for `pixi run smoke-local`, false for a bare run of this file — verified) |
| C16 | `tests/smoke/conftest.py:6-7,31` | dead / documentation | yes (line 31 has no reader; 32-33 keep the behaviour) | low | FIX NOW | docstring says it flips `log_cli`; it sets `log_cli_level` |
| C17 | snapshot/restore autouse ×4 (A row) | redundant | n/a | med | NEEDS DISCUSSION (E5) | one shared fixture is also the natural home for the reload guard (ND #20) |
| C18 | S4 fakes ×6/×2/×5 in-area (A row) | redundant | n/a | low | NEEDS DISCUSSION (E7) | one commit, one reason to change |
| C19 | `tests/live/conftest.py:27` `import boto3`, `:30` kinoforge import | structural | n/a | — | LEAVE | makes boto3 + the RunPod util a hard dependency of unit-suite COLLECTION; present in the env today |
| C20 | 36 assertion-less "must not raise" tests (AST sweep) | test strength | n/a | low | LEAVE except three (E10) | cheap stronger asserts: `tests/engines/test_hosted.py:725` (assert the `http_get` spy saw zero calls), `tests/providers/test_runpod.py:853` (assert exactly 2 POSTs, 0 sleeps), `tests/engines/test_fake.py:280` (assert `is None`) |
| C21 | `tests/stores/test_file_lock.py:205` | structural (real 3 s sleep) | n/a | — | LEAVE | cross-process lock holder in a subprocess; needs wall-clock |
| C22 | `tests/_smoke_harness/{http,matrix,runpod_lifecycle,test_http,test_matrix,test_runpod_lifecycle}.py`, `tests/engines/diffusers/servers/test_set_stack_swap_gaps.py:65`, `tests/engines/diffusers/test_av_io.py:263` RUF100 | — | — | — | LEAVE (hygiene-notes keep) | "annotate intent" `noqa` codes not enabled; not stale suppressions |

---

## D. FIX NOW edit list

Ruff `--select F401,F841,F811,B018` over the whole area: **zero hits** — every deletion below
was confirmed by `rg` reference counts, not by ruff.

### Code items

**D1 — `tests/providers/conftest.py:12-24`** · embedded: **no**
Delete the dead re-export (keep the module docstring and `from __future__`):
```
old:
from __future__ import annotations

# Re-export the fake classes for use in provider-level tests that import them
# directly (e.g. type annotations in test helpers).
from tests.conftest import (
    FakeRunPodHeartbeatEndpoint,
    FakeSkyPilotHeartbeatEndpoint,
    LocalHeartbeatEndpoint,
)

__all__ = [
    "FakeRunPodHeartbeatEndpoint",
    "FakeSkyPilotHeartbeatEndpoint",
    "LocalHeartbeatEndpoint",
]
new:
from __future__ import annotations
```
Fence: `git log -S'FakeRunPodHeartbeatEndpoint,' -- tests/providers/conftest.py` → `e85bd377`
"feat(b5a): heartbeat substrate Protocol + TransportError + fakes" (plan intended the fakes
here; they landed in the root conftest). Proof: `rg -n 'tests\.providers\.conftest' tests/` →
0; `rg -n 'from \.conftest import' tests/providers/` → 0. Tests to run first:
`tests/providers/test_heartbeat_parity.py`, `tests/core/test_heartbeat_endpoints.py`.
Also update docstring `:3-7` ("defined in the root tests/conftest.py … This file exists as a
package-level conftest for any future provider-specific fixtures") — already accurate, no edit.

**D2 — `tests/smoke/conftest.py:31`** · embedded: **no**
```
old:
    config.option.log_cli = True
    if not getattr(config.option, "log_cli_level", None):
new:
    if not getattr(config.option, "log_cli_level", None):
```
Fence: `fdd926ef` "cfg(p2): pytest log_cli WARNING for smoke tiers". Proof:
`rg -n 'config\.option\.log_cli\b' tests/ docs/` → 1 (this line) + the prior audit doc.
Mechanism verified against `_pytest/logging.py:759-763` and empirically (see A). Tests:
`pixi run smoke-local` (the WARNING live-log line from `test_runpod_lifecycle.py::…reports_failures`
must still appear).

**D3 — `tests/providers/conftest_runpod.py:52-58`** · embedded: **no**
```
old:
_OPERATION_TABLE: list[tuple[str, str]] = [
    ("gpuTypes {", "gpu_types.json"),
    ("myself { pods", "list_pods.json"),
    ("podFindAndDeployOnDemand", "create_pod.json"),
    ("podTerminate", "terminate_pod.json"),
    ("pod(input:", "get_pod.json"),
]

_log = logging.getLogger(__name__)
new:
_log = logging.getLogger(__name__)
```
Fence: `c38dde70` "docs(plan): Layer N — RunPod cloud-fidelity hardening implementation plan".
Proof: `rg -n '_OPERATION_TABLE' tests/ src/ tools/ docs/` → 1 (definition). The live mapping
is `:446-454`. Tests: `tests/providers/test_runpod_conftest.py`, `tests/providers/test_fixtures_audit.py`.

**D4 — `tests/providers/test_runpod.py:190-203`** · embedded: **no**
Delete `class HttpGetSpy` (`:190-201`) and the two blank lines after it (keep `class SleepSpy:` at `:204`).
```
old (first/last lines, delete the whole block):
class HttpGetSpy:
    """Returns a different response for each successive call."""
    …
        return self._responses.pop(0)
```
Fence: `9f51d74e` "feat: add RunPod provider (pod + serverless) with self-terminating
instances". Proof: `rg -n 'HttpGetSpy' tests/ src/ tools/` → 1. Tests: `tests/providers/test_runpod.py`.

**D5 — `tests/diagnostics/test_c33_orchestrator.py:26-31`** · embedded: **no**
```
old:
class _FakeS3:
    def list_objects_v2(self, **_kw: Any) -> dict[str, Any]:
        return {"Contents": [], "IsTruncated": False}


def test_c33_hard_cap_is_five_dollars() -> None:
new:
def test_c33_hard_cap_is_five_dollars() -> None:
```
Fence: `a44fbdda` "docs(c33): implementation plan + native tasks snapshot". Proof:
`rg -n '_FakeS3\b' tests/diagnostics/` → 1; `Any` remains used ×4 elsewhere in the file
(no F401 after the edit). Tests: `tests/diagnostics/test_c33_orchestrator.py`.

**D6 — `tests/providers/test_field_consumption_parity.py:732-734`** · embedded: **no**
```
old:
_PROBE_SCRIPT = "#!/bin/bash\necho kf-probe-provision-marker\n"
_PROBE_BUILD_SCRIPT = "#!/bin/bash\necho kf-probe-build-marker\n"
_PROBE_RUNTIME_SCRIPT = "#!/bin/bash\necho kf-probe-runtime-marker\n"
_PROBE_TAGS = {"kf_probe": "sentinel"}
new:
_PROBE_TAGS = {"kf_probe": "sentinel"}
```
Fence: `a72c5401` "feat(providers): declare portable-field consumption and guard it with a
parity test" (constants shipped unused). Proof: `rg -c '_PROBE_SCRIPT\b|_PROBE_BUILD_SCRIPT|_PROBE_RUNTIME_SCRIPT' tests/providers/test_field_consumption_parity.py`
→ 1 each; 0 elsewhere. Tests: `tests/providers/test_field_consumption_parity.py`.

**D7 — `tests/validation/test_shipped_configs_capabilities.py:17`** · embedded: **no**
```
old:
CONFIG_DIR = Path("examples/configs")
new:
CONFIG_DIR = Path(__file__).resolve().parents[2] / "examples" / "configs"
```
Behaviour-preserving under `pixi run` (resolves to the same directory; parametrize ids are
`p.name`, `:74`, so node ids are unchanged). Proof: `rg -n 'CONFIG_DIR' tests/validation/test_shipped_configs_capabilities.py`
→ `:17,:74,:87` only, all path uses. Tests: `tests/validation/test_shipped_configs_capabilities.py`
(from `/workspace` AND from `/tmp` with `--rootdir=/workspace`: the latter should now collect
53 configs instead of skipping).

### Docs-only items (comments/docstrings/skip reasons)

**D8 — `tests/providers/test_launch_payload_goldens.py:3-5`**
```
old:
globs ``examples/configs/*.yaml`` NON-recursively, so the 31 goldens cover the
top-level configs that carry a ``compute:`` block.
new:
globs ``examples/configs/*.yaml`` NON-recursively, so the goldens cover only the
top-level configs that carry a ``compute:`` block.
```
Proof: `rg -n '31 goldens' tests/` → this line only (the other hits are historical plans under `docs/superpowers/`).

**D9 — `tests/providers/test_skypilot.py:1162,1167,1180`**
```
old (:1162):
    reason="T4 GPU fixtures not captured yet — Layer W+β T4 will land them",
new:
    reason="SkyPilot GPU fixtures absent — regenerate via the recording proxy",
old (:1167):
    Lands after Layer W+β T4 captures the fixtures. Catches sky SDK shape
new:
    Runs whenever the captured fixtures are present. Catches sky SDK shape
old (:1180):
    # fixtures committed to git (see tools/_redact.py + tests/conftest_runpod.py).
new:
    # fixtures committed to git (see tools/_redact.py + tests/providers/conftest_runpod.py).
```
The skip never fires today (fixture committed), so the reason text is unobservable. Proof:
`rg -n 'Layer W\+β T4|tests/conftest_runpod\.py' tests/` → these lines only.

**D10 — `tests/providers/test_heartbeat_parity.py:31-32`**
```
old:
    # Imported lazily so the conftest.py fixtures registered by Task a's
    # `tests/providers/conftest.py` are guaranteed available.
new:
    # Imported lazily so the fakes defined in the root `tests/conftest.py`
    # are guaranteed to be registered before this factory runs.
```
Proof: `rg -n 'fixtures registered by' tests/` → this file only. (Twin stale comment in
`tests/core/test_heartbeat_endpoints.py:4-5` — tests-1 area.)

**D11 — `tests/engines/diffusers/servers/test_set_stack_async_job.py:19-22`**
```
old:
    Restores ``ready`` and re-clears ``_inventory`` / ``_swap_jobs`` on
    teardown: pytest-randomly is active, so a test elsewhere that depends
    on the not-ready state (or on an empty inventory) must not inherit
    this fixture's leaked module state.
new:
    Restores ``ready`` and re-clears ``_inventory`` / ``_swap_jobs`` on
    teardown so a test elsewhere that depends on the not-ready state (or on
    an empty inventory) never inherits this fixture's leaked module state,
    whatever the collection order.
```
Proof: `rg -n 'pytest-randomly' tests/ pixi.toml pyproject.toml` → this file only.

**D12 — `tests/smoke/conftest.py:7`** (pair with D2)
```
old:
Also flips pytest's ``log_cli`` ON at ``WARNING`` for any test collected
new:
Also sets pytest's ``log_cli_level`` to ``WARNING`` for any test collected
```

**D13 — `tests/live/conftest.py:12-13`**
```
old:
Loading is silent if ``.env`` is absent or the kinoforge package isn't
importable for any reason — tests then fall back to whatever the
new:
Loading is silent if ``.env`` is absent — tests then fall back to whatever the
```
(The "not importable" clause is false: `:30` imports kinoforge unguarded. The code-side
option — moving `:30` under the `try` — changes the collection failure mode and is E6.)

---

## E. Needs discussion

**E1 — Finding #0 test fakes (`tests/tools/test_smoke_leak_sweep.py`, `tests/_smoke_harness/test_runpod_lifecycle.py:52-70`).**
Decision: when the age source for the sweep is chosen, add ONE test per file built from the
real `_pod_to_instance({"id":…,"desiredStatus":"RUNNING","imageName":…})` output (so
`created_at=0.0`, `tags={"mode":"pod"}`) asserting no destroy — it is RED today, which is the
correct scaffold for the #0 fix (CLAUDE.md "commit RED scaffolds"). Separately decide whether
`tag_filter`/`smoke_tier` should exist at all: RunPod never sends `compute.tags`, so either
plumb tags through `podFindAndDeployOnDemand` (payload change → goldens move) or delete
`tag_filter` + its test (B-T1).

**E2 — Modal list-fake key (`tests/providers/modal/test_provider.py:100-106` vs `providers/modal/__init__.py:398-404`).**
Decision: drop the fake-only `rec.get("name")` fallback in `_rec_name` and switch the fake to
`"description"` (the key `test_modal_destroy_by_app_id.py` already uses), or keep the dual
read and document it as deliberate. Source change is cross-area.

**E3 — Discovery-guard floors and CWD anchoring** (`test_field_support_check.py:37` with
relative ids preserved, e.g. `ids=lambda p: p.relative_to(CONFIG_DIR).as_posix()`;
`test_provider_selection.py:165` and `test_skypilot.py:203` `assert len(files) >= 50`;
`test_fixtures_audit.py` `assert scanned >= 100`; `test_diffusers_optional_cfg_defaults.py`
`assert len(found) >= 10`; `test_launch_payload_goldens.py` `assert len(_CONFIGS) >= 40`).
Decision: floor values. All failure-direction-only changes.

**E4 — `test_runpod_name_resolution._Transport` (`:77-85`).** Parse the pod id out of the
`podTerminate` mutation text (`podId: "<id>"`) and record THAT, so a wrong-id destroy fails;
the `<unmatched>` sentinel then only covers a malformed mutation. Decision: whether the test
at `:225-226` should assert `["pod-abc123"]` (the id passed in) — it should.

**E5 — Shared wan-server state fixture.** Replace the ×4 identical snapshot/restore fixtures
with one `tests/engines/conftest.py` fixture covering `_pipe_arity`, `_inventory`,
`_swap_jobs`, `_LOADED`, `_WAN_REGISTRY_NAME`, `_upscale_jobs`, `_interpolate_jobs`; it is also
the place to assert `sys.modules[...] is srv` after any `importlib.reload` (ND #20 guard).

**E6 — `tests/live/conftest.py` collection side effects (B20 / ND #20).** Options: gate the
`.env` load on `os.environ.get("KINOFORGE_LIVE_TESTS") == "1"` (then `pixi run test` no longer
loads credentials into `os.environ`), and move the `:27` boto3 / `:30` kinoforge imports under
the same gate or into the C30 helpers. Any of these changes what `pixi run test` does at
collection — not for the hygiene pass.

**E7 — S4 catalog fakes** (`_S4_GPU_TYPES` ×6, `_S4_ACCELERATORS` ×2, `_create_body` ×5 in
this area; +4 in `tests/core`). One `tests/providers/_s4_fakes.py` (or `conftest_runpod.py`)
owner. Pure test-side refactor; moderate churn across 10 files.

**E8 — ComfyUI `status_str: "error"` coverage.** No unit test exercises
`engines/comfyui/__init__.py:885-900`; add one with the B5 fix (and it will need to decide
whether "error" is terminal — B5's open question).

**E9 — `test_pod_status_mapping_covers_real_statuses` (`test_runpod.py:1640-1680`).** Convert
the skip-on-empty into a hard assertion the next time the RunPod fixtures are regenerated with
`KINOFORGE_SAVE_FIXTURES=1`; today the fixture carries RUNNING so it passes.

**E10 — Three assertion-less tests** worth a stronger assert (C20). Behaviour-preserving for
production; test-only edits; needs a quick RED check that the stronger assert actually fails
on the named bug.

**E11 — `test_launch_payload_goldens.py:114-120` vacuous exclusions loop** (keep stands). If
the operator wants it visible, `assert not EXCLUDED_CONFIGS, "tighten this test"` would turn
the first exclusion into a deliberate edit — a behaviour change, so recorded here only.

---

## F. Cross-area notes

- `src/kinoforge/providers/modal/__init__.py:418-421`: the comment calls `created_at=0.0`
  "the conservative over-age 0.0" — the exact assumption Finding #0 shows is destructive for
  any age-based consumer. Modal `:422` and SkyPilot `:561` list converters also return `0.0`;
  the core auditor should check `sweeper`/`reaper`/`kinoforge cost` for the same `now - created_at`
  arithmetic on listed (not ledgered) instances.
- `src/kinoforge/providers/modal/__init__.py:398-404` `_rec_name` carries a fake-only `name`
  branch (see E2).
- `tests/core/test_heartbeat_endpoints.py:4-5` — same stale "fakes live in
  tests/providers/conftest.py" comment as D10 (tests-1 area).
- `tests/core/test_capability_parity.py:48,71`, `tests/core/test_compute_surface.py:24,60` —
  the other four S4 catalog-fake copies (E7).
- `AGENTS.md:45-57` points at `_CREDENTIAL_PATTERNS` in `tests/providers/conftest_runpod.py`;
  confirmed it is now an alias of `core.credential_patterns.CREDENTIAL_PATTERNS` (`:31`).
- `pixi.toml:128-131` comment "tests/live directory is created in Layer I Task 13" — it holds
  88 modules.
- `tests/conftest.py:117-128` autouse redaction clear is what makes `tests/smoke/conftest.py:36-38`
  and `tests/integration/conftest.py` redundant (prior dead row confirmed from this side).
- `engines/comfyui/__init__.py:887` error-status seam has zero unit coverage anywhere in
  `tests/` (not only in this area) — relevant to the B5 fix owner.
