# Hygiene re-audit 2026-10-21 — tools / docs / config area

Auditor scope: `tools/` (42 modules; `_vendored/` hands-off), `docs/*.md` (excl. the three hygiene
files; `docs/superpowers/` skimmed for pointers only), `README.md`, `AGENTS.md`, `CLAUDE.md`
(factual claims only), `successful-generations.md` (preamble), `examples/`, `pixi.toml`,
`pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/*.yml`, `docker/`, `.claude/`,
`.gitignore`, `LICENSE`/`LICENSES/`.

Verified against `f89eff2b`; re-checked that the two commits that landed meanwhile
(`0661d558` orchestrator import, `58d27ef0` noqa cleanup in six `tools/*.py`) touch nothing any
finding below depends on. Line numbers are at `f89eff2b`.

Method notes: real CLI help captured via `pixi run kinoforge <sub> --help` for all 22 subcommands
(saved under the scratchpad `help/` dir); every intra-repo markdown link + anchor checked by
script (143 links); every `--flag` in a `kinoforge` context in the docs checked against the 52
real flags; every repo path mentioned in the docs checked for existence (301 mentions); every
example YAML walked against the pydantic model tree for silently-dropped keys (`extra=ignore`);
every `pixi run <task>` mention checked against `[tasks]`.

---

## A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line) |
|---|---|---|---|
| Finding #0 / B1 | `smoke_leak_sweep.py:103-131` ages pods from `created_at`, which RunPod hard-codes to 0.0 → every pod reaps | CONFIRMED (code); cron state CANNOT-VERIFY-OFFLINE | `tools/smoke_leak_sweep.py:102-104` `tag = pod.tags.get("smoke_tier")` / `age_h = (now - pod.created_at) / 3600.0`; `providers/runpod/__init__.py:1971` `created_at=0.0,  # RunPod list API does not return creation time`; `rg smoke_tier src` → 0 writers. Whether the GitHub cron is still disabled is not checkable offline. |
| B20 | `pixi run test-live` sets `KINOFORGE_LIVE_TESTS=1` itself; README:147 points operators at it | CONFIRMED | `pixi.toml:132` `env = { KINOFORGE_LIVE_TESTS = "1" }`; README:147 says "Needs `KINOFORGE_LIVE_TESTS=1` + cloud creds"; `tests/live/*.py` = 85 modules |
| FIX NOW README:83 | clone URL is `emmykillett/kinoforge` | CONFIRMED | README:83; `git remote -v` → `https://github.com/killett/kinoforge.git` |
| FIX NOW README:110-133 | cheatsheet: no `image`/`text`; `gc`/`cost`/`sweeper *` omit required cfg; `metrics` needs `--prom`; `pod lora ls --id` is positional | CONFIRMED | help: `gc [-h] --config PATH` (long form only, no `-c`); `cost [-h] -c PATH`; `sweeper start/stop/status [-h] -c PATH`; `sweeper metrics [-h] -c PATH --prom`; `pod lora ls … POD_ID`; `image`/`text` exist in top-level help, absent from README:109-133 |
| FIX NOW README:160-161 | `smoke-21b-live` is Wan 2.1 1.3B; `smoke-wan22-live` caps at $2.00/module | PARTIAL | `tests/smoke/live_wan21/test_lora_swap_matrix.py:1` "Wan 2.1 1.3B", `_BUDGET_CAP = 0.30` ×3; `release_wan22` caps are **2.00 / 1.50 / 4.0** per module (`test_lora_swap_matrix.py:49`, `test_lora_strength_variation.py:44`, `test_dual_transformer_routing.py:68`) — not a uniform $2.00 |
| FIX NOW README:461 + configuration.md:34 | `hosted.yaml` is not a provider comparison / fal.ai | CONFIRMED | `examples/configs/hosted.yaml:1-12` "HostedAPIEngine (user-deployed shim) … No public provider … implements this contract" |
| FIX NOW README:560 / troubleshooting.md:41 / lifecycle.md:365-369 | ComfyUI poll timeout default is 1800 not 600; `poll_interval_s` not a field | CONFIRMED | `core/config.py:325` `poll_timeout_s: float = Field(default=1800.0, gt=0.0)`; `rg poll_interval_s src/kinoforge/core/config.py` → only the docstring word at :312; `ComfyUIEngineConfig` (:302) has no `extra="forbid"` → key silently dropped |
| FIX NOW README | no "Project structure" section | CONFIRMED | README H2s: Table of contents, Quickstart, Installation, Command cheatsheet, Grid examples, Upscaling…, Configuration at a glance, Credentials, Operator concepts, Troubleshooting, Contributing, Releasing, License |
| FIX NOW extending.md:15,87-95,131-140 | templates import never-existing `GpuOffer`, omit abstract methods, wrong `validate_spec` | PARTIAL | `GpuOffer` absent (`interfaces.py:93` is `Offer`) ✔; GenerationEngine template omits abstract `profile_for`/`declared_flags`/`model_identity` (`interfaces.py:1224-1234`) and `validate_spec(self, job: GenerationJob)` ✔; ArtifactStore template omits abstract `acquire_lock`/`delete_run`/`manual_cleanup_command`/`signed_url` (`stores/base.py:130-182`) ✔; **but** the ComputeProvider template (:24-31) names all 7 abstract methods — not deficient. New: `:71` `ModelSource.resolve(self, ref) -> Artifact` vs ABC `resolve(self, ref, creds) -> list[Artifact]` (`interfaces.py:655`) |
| FIX NOW credentials.md:104-117 | nested `auth:` block fails `extra=forbid`; `VeoEngine`/`NovaReelEngine` do not exist | CONFIRMED | `core/config.py:366` `HostedEngineConfig … ConfigDict(extra="forbid")`, no `auth` field; `rg VeoEngine|NovaReelEngine src` → 0 (shipped class is `BedrockVideoEngine`, `engines/bedrock_video/__init__.py:257`) |
| FIX NOW engines.md:192,223,336-340 | `--init-image` never existed; quickstart omits `--mode` | CONFIRMED | `rg 'init.image' src/kinoforge/cli/_main.py` → 0; `generate` usage: `-c PATH --prompt TEXT --mode MODE` (all required) |
| FIX NOW engines.md:13 | `engine.type` → `engine.kind` | CONFIRMED | `EngineConfig.kind: str` (`config.py:950`) |
| FIX NOW engines.md:375 | `boot_timeout_s` → `boot_timeout` | PARTIAL | key is `lifecycle.boot_timeout` (`config.py:116`) ✔, **and the default is 900 s, not 1800** (`config.py:116`, `interfaces.py:143 boot_timeout_s: float = 900.0`; 1800 is only the `or 1800` fallback for a 0 value at `providers/modal/__init__.py:324`) |
| FIX NOW engines.md:367 | "Eleven" Modal configs → 13 | CONFIRMED | `ls examples/configs/modal-*.yaml` → 13 |
| FIX NOW `../` path rewrites (engines, cost-and-spend:11-13, batch-and-grid:11-12, warm-reuse:127,135) | shell paths wrongly prefixed `../` | CONFIRMED | non-link `../` hits: engines.md ×25 (:34-683), cost-and-spend.md:11-13, batch-and-grid.md:11-12,21, warm-reuse.md:127,135,208-209,309,316,343,399-400,440, releasing.md:21, configuration.md:136; `pixi run` executes from repo root |
| FIX NOW warm-reuse.md:127,135 | cites never-existing `wan21-1_3b.yaml` / `wan22-14b.yaml` | CONFIRMED | `fd -e yaml . examples` has neither |
| FIX NOW configuration.md:180 | "first frame" contradicts :190-198 | CONFIRMED | :180 "(animated GIFs and multi-page TIFFs contribute their first frame)"; :190-192 "Animated inputs … are upscaled frame by frame" |
| FIX NOW output-layout.md:10 | schema is `{ts}_[{kind}_]{provider}_{model}_{slug}{ext}` | CONFIRMED | `outputs/local.py:63,153`; `slugify(prompt)` default `max_chars=20` (`outputs/base.py:105`) so the "first 20 chars" sentence still holds |
| FIX NOW broken anchors warm-reuse:360,432 / breaking-changes:312 / troubleshooting:11 | anchors do not resolve | CONFIRMED + 2 more | script: `#known-keys` absent from warm-reuse.md (lives in credentials.md); `#migration-from-a-local-ledger` lives in cloud-stores.md:68 not breaking-changes.md; CLAUDE.md has no `pre-commit-stages-pixilock` heading. **Also broken (missed):** troubleshooting.md:29 `lifecycle.md#kinoforge-forget---id-id----clear-…` and :44 `warm-reuse.md#kinoforge-generate---loras----cli-…` (the em dash slugs to nothing → `--` not `----`; headings at lifecycle.md:143, warm-reuse.md:113) |
| FIX NOW RELEASE-CHECKLIST.md:10-11,24-25 | requires a weekly run of a dispatch-only workflow; hand-tag contradicts `pixi run release` | CONFIRMED | `smoke-wan21-weekly.yml:8-9` `on: workflow_dispatch:` only ("Dispatch-only since 2026-09-21"); `releasing.md:12` / `tools/release.py:3` `pixi run release X.Y.Z` |
| FIX NOW AGENTS.md:45-57,76 | points at `_CREDENTIAL_PATTERNS` in conftest (now an alias); stale `PROGRESS:213` | CONFIRMED | `tests/providers/conftest_runpod.py:30-31` `from kinoforge.core.credential_patterns import (CREDENTIAL_PATTERNS as _CREDENTIAL_PATTERNS,`; PROGRESS.md:213 is a `time_buffer` tolerance sentence. Extra drift: table :26-34 lists 7 of the 16 pattern names (missing `github_token`, `replicate_token`, `runway_key`, `luma_key`, `jwt`, `google_api_key`, `gcp_private_key_id`, `url_credentials`, `credential_assignment`, `hf_token_strict`); :65 "All five" precedes 6 variables |
| FIX NOW pixi.toml:128-131 | `test-live` comment says tests/live "is created in Layer I Task 13" | CONFIRMED | pixi.toml:130-131 verbatim; `tests/live/` holds 85 `.py` modules |
| FIX NOW smoke-wan21-weekly.yml:13 | `issues: write # post issue on test failure` describes a step that never existed | PARTIAL | the **permission is needed**: the `if: always()` step (:31-33) runs `smoke_leak_sweep.py`, whose `_post_issue` (:39-65) calls `gh issue create --label …` with `GH_TOKEN` (:25); only the comment text is wrong. Also stale: :7 "The independent leak-sweep cron remains the standing money guard" |
| docstring rows `validate_scoped_policy.py:28,94,496`, `render_aws_policy.py:216` | drifted line pointers into `cloud_perms_probe.py` | CONFIRMED | targets today: bootstrap `cloud_perms_probe.py:48-50` (doc says 33-35); identity capture `:239 sts.get_caller_identity()` (doc says 209-215); KMS split `:85-95` + `:229-235` (doc says 201-241); ARN file `:83 _AWS_KMS_KEY_ARN_FILE` / `:92-95` (doc says :68) |
| dead `tools/_redact.py:31-35` | `_CREDENTIAL_PATTERNS` alias has zero importers | CONFIRMED | `rg _CREDENTIAL_PATTERNS tools tests src .claude` (excl. conftest_runpod) → only `tools/_redact.py:35` and a comment at `tests/tools/test_redact.py:49`; fence `a8876b44 refactor(credentials): point the existing redactors at the shared list` (added as back-compat at that refactor; nothing adopted it) |
| ND #21 purge list | quota_burn*, c33_* ×4, uptime family, build_bsa_wheel, local_hooks dead; diagnose_pod_boot / debug_ledger_watcher new; cloud_perms_probe alive | CONFIRMED (refined) | liveness sweep over src/tests/tools/docs/pixi.toml/.github/examples/PROGRESS/README/CLAUDE/AGENTS/successful-generations: `c33_j_probe`, `c33_k_probe`, `debug_ledger_watcher` → **0 refs anywhere** (not even PROGRESS); `c33_l_metricwatch`, `c33_denial_branch`, `diagnose_pod_boot`, `diagnose_u13_exit_holder`, `uptime_sweep_status.sh` → PROGRESS / `docs/superpowers` plans only; `uptime_field_hourly_sweep` / `uptime_sweep_summary` → only `uptime_sweep_status.sh`; `quota_burn` → README:525,563, troubleshooting.md:52, quota-justification-*.md + 6 tests (1,432 LOC); `build_bsa_wheel.py` → `runpod-diffusers-flashvsr-x4-upscale.yaml`, successful-generations, `diagnose_flashvsr_writer_modal.py` + `tests/tools/test_build_bsa_wheel.py` (60); `build_bsa_wheel_modal.py` → 5 modal yamls (provenance, alive); `local_hooks` → `tests/hooks/test_tasks_live_query.py` (268) + `docs/hook-differential-test-results-2026-06-18.md`; `cloud_perms_probe` alive via `validate_scoped_policy.py:100` import + `pixi.toml:162` |
| ND #21 deps | `google-cloud-billing` no importer; `lumaai`/`fal-client` imported nowhere; `httpx` declared twice | CONFIRMED | `tools/quota_burn.py:82` imports `google.cloud.billing.budgets_v1` (that is the `-billing-budgets` dist) — no importer of the base `google-cloud-billing`; `rg 'import lumaai|fal_client' src tests tools` → 0 (pixi.toml:239-241 "Lazy-imported inside engine modules" is false); `httpx` at pixi.toml:78 (conda) and :96 (PyPI); `httpx/typer/rich/stamina/vermin` have 0 importers |
| ND #22 | CI pin drift across workflows | CONFIRMED | `leak-sweep.yml:20-21`, `smoke-wan21-weekly.yml:27-28`, `build-wan-comfyui-image.yml:35` → `checkout@v4` / `setup-pixi@v0.8.1`; `ci.yml:29,31,33` → `checkout@v6` / `setup-pixi@v0.9.6` / `pixi-version: latest` |
| ND #23 | pixi.toml header says it is a generated template edited via `groups.toml` | CONFIRMED | pixi.toml:1-27; no `groups.toml` in repo |
| ND #5 (example-config part) | "BudgetTracker tears down mid-run" comments are false | CONFIRMED | `rg BudgetTracker src/kinoforge` → only `core/lifecycle.py` (no production caller); what `budget` does today: shortens the selfterm cap to `budget ÷ rate` at create (`validation/checks/capabilities.py:201-209`, `field_support.py:358-369`). Comments at `runpod-comfyui-wan-2_1-14b-t2v.yaml:20,89-90`, `runpod-comfyui-wan-2_1-1_3b-t2v.yaml:29,101-102`, `runpod-comfyui-wan-2_1-14b-i2v.yaml:21` |
| ND #6 (`docs/cloud-stores.md:100`) | "profile discovery uses the lock automatically" while `resolve_or_discover` is test-only | CONFIRMED | callers of `resolve_or_discover(`: only `tests/core/test_profiles.py:386,906-943`; orchestrator calls `profile_provider.resolve(key)` :2081 and `.discover(…)` :2128 |
| P3 `config.py:366` vs hosted `auth:` (docs side) | the documented block cannot reach any engine | CONFIRMED | credentials.md:107-117 documents it; `HostedEngineConfig` forbids extras |
| keep: `_t11_probe`/`_t11_tail` per PROGRESS | still referenced | CONFIRMED | `rg -c '_t11_probe|_t11_tail' PROGRESS.md` → 2; no other refs |
| keep: `cloud_perms_probe` alive (`75f37b74`) | | CONFIRMED | commit resolves; import at `validate_scoped_policy.py:100` |
| keep: `bootstrap_kms.py:430-440` broad excepts | per-cloud independence | CONFIRMED | two `except Exception as exc:` in that range, one per cloud |
| keep: `smoke:` block in the warm-reuse-smoke yaml | only key `Config` drops | CONFIRMED | dropped-key walk over all 60 example Configs → exactly one dropped key repo-wide: `.smoke` in that file |
| keep: `pyproject.toml:11 seedvr = []` | referenced by install hint | CONFIRMED | `core/errors.py:649` "``kinoforge[seedvr]``" |
| keep: pixi.toml:78-82 `typer/rich/stamina/vermin/httpx` unused (`6f33dc24`) | | CONFIRMED | commit `6f33dc24 chore(deps): add httpx, typer, rich, stamina, vermin`; 0 importers |
| keep: Dockerfile pins restated ×3 | low churn, Dockerfile canonical | CONFIRMED | `docker/wan-comfyui/Dockerfile:17-20` == `tools/build_image_wan_comfyui.sh:11-14` == `build-wan-comfyui-image.yml:16-28` defaults == `tests/live/cfg_c27_phase_b.yaml:21-25` |
| keep (07-16): `tools/_uptime_field_sweep_log.jsonl` location | PROGRESS references path | CONFIRMED | `rg -c _uptime_field_sweep_log PROGRESS.md` → 1 |
| keep (07-16): `tools/repro_runpod_uptime.py` | retained per PROGRESS | CONFIRMED | `rg -c repro_runpod_uptime PROGRESS.md` → 1; no other refs |
| keep (07-16): `tools/flashvsr_debug_matrix.py` | reusable warm-pod harness | CONFIRMED (reason unchanged; only PROGRESS references it) | liveness sweep → PROGRESS only |
| keep (07-16): `probe_civitai_throughput.py:43-47` provider-private imports | | CONFIRMED | `:43-47` imports `_CREATE_POD_MUTATION, _LIST_PODS_QUERY, _make_default_http_seams` |
| keep (07-16): `c28_provision_s3_diagnostics.py` bucket consumed by `orchestrator.py:248` | | PARTIAL | alive (`tests/tools/test_c28_provision_s3_diagnostics.py`, 159 LOC) but the pointer drifted: `KINOFORGE_DIAG_BUCKET` is read at `core/orchestrator.py:276-280` (hygiene-notes is outside my area — cross-area note) |
| keep (07-16): `comfyui_ui_to_api.py` + `_vendored/` (goldens wired) | | CONFIRMED | `tests/tools/test_comfyui_ui_to_api.py`, `test_kijai_workflow_golden.py` |
| keep (07-16): `probe_pod_watchdog.py` (`pixi run probe-watchdog`) | | CONFIRMED | pixi.toml:151 |
| keep (07-16): `bootstrap_kms.py` (`tests/stores/live/conftest.py` directs operators to it) | | CONFIRMED | conftest message "run pixi run cloud:bootstrap-kms"; pixi.toml:161 |
| prior roll-up: "successful-generations schema vs spec" still open | | CANNOT-VERIFY (out of preamble scope) | preamble cites `docs/superpowers/specs/2026-06-08-successful-generations-log-design.md`, which exists; schema conformance of 4.4k lines not re-audited |

---

## B. New bugs (missed by the prior audit)

**None found** in this area. Specifically checked:

- `tools/smoke_leak_sweep.py` both paths (age test — Finding #0 stands; `_post_issue` `gh` invocation + label).
- `tools/release.py` contract vs `docs/releasing.md` (consistent; `tests/tools/test_release.py` exists).
- `tools/preflight.py` docstring vs behaviour (`--check-hosted` exists at :271); `tools/probe_hosted.py` `--check-bedrock-model-access` exists (:281).
- `tools/scan_secrets.py --all-tracked` (:386) and `tests/test_source_audit.py::test_no_tracked_file_contains_a_credential` (:53) — the two pointers in `.pre-commit-config.yaml:80-81` resolve.
- Every example YAML against the pydantic tree: zero silently-dropped keys other than the documented `smoke:` keep (so no "key the user thinks is active but isn't" in shipped configs).
- All 143 doc links / anchors (6 broken, all docs-only), all 301 doc path mentions (3 real misses, docs-only), all `--flag` mentions in `kinoforge` contexts (only `--init-image` is phantom), all `pixi run <task>` mentions (all defined).
- Workflows: secrets wiring and `permissions:` are consistent with what the steps do (see A row on `issues: write`).
- `.claude/settings.json` registers exactly the two hooks CLAUDE.md names; both files tracked.
- Two *documented commands that fail as written* are recorded under D, not here, because they are docs: `CLAUDE.md:375 pixi run kinoforge sweeper start &` (exit 2: `-c PATH` required) and `docs/lifecycle.md:234-237` (same four commands without `-c`, `metrics` without the required `--prom`).

---

## C. New hygiene findings

| # | location | category | behaviour-preserving? | risk | call | rationale |
|---|---|---|---|---|---|---|
| C1 | `docs/troubleshooting.md:29,44` | documentation | yes (doc) | low | FIX NOW | two further broken anchors (em-dash slug miscount), see A |
| C2 | `docs/extending.md:64,71-72` | documentation | yes | low | FIX NOW | `ModelSource.resolve` template has the wrong signature/return (`interfaces.py:655`) |
| C3 | `docs/extending.md:84,91-93`, `:127-138` | documentation | yes | low | FIX NOW | precise missing abstract methods for GenerationEngine (3) and ArtifactStore (4); prior row was generic |
| C4 | `docs/engines.md:375` | documentation | yes | low | FIX NOW | default is 900 s, not 1800 s (see A) |
| C5 | `docs/warm-reuse.md:399-400,440` | documentation | yes | low | FIX NOW | `CapabilityKey.derive()` lives in `core/interfaces.py:744` not `core/profiles.py`; `../src/engines/diffusers/` is not a path (`src/kinoforge/engines/diffusers/`) |
| C6 | `docs/lifecycle.md:234-237` | documentation | yes | low | FIX NOW | sweeper command listing omits required `-c PATH` on all four and `--prom` on `metrics` (help confirms both required; `docs/modal-command-matrix.md:253` already documents "exit 2 without it") |
| C7 | `docs/engines.md:359` | documentation | yes | low | FIX NOW | describes `runpod-comfyui-wan-2_2-14b-t2v.yaml` as "Wan 2.1 i2v engine config"; the i2v live test at :311-313 uses `runpod-comfyui-wan-2_1-14b-i2v.yaml` |
| C8 | `AGENTS.md:24-34,65` | documentation | yes | low | FIX NOW | pattern table is 7 of 16 names; "All five" precedes six variables |
| C9 | `CLAUDE.md:169-170` | documentation | yes (factual claim, not a rule) | low | FIX NOW | claim "`pixi run -- gcloud …` just prints the task list" is WRONG at pixi 0.69: `pixi run -- echo hi` prints `hi`; `pixi run -- gcloud --version` → `gcloud: command not found`. The conclusion (use `-e live-skypilot`) is right; the stated failure mode is not |
| C10 | `CLAUDE.md:416,459` | documentation | yes | low | FIX NOW | commit SHAs `5418c35` and `8438a8b` do not resolve (`git log --all`); the commits that did the work are `90404cc5` (gzip provision script) and `70cd3992` (downscale reads from a temp file) |
| C11 | `CLAUDE.md:361` | documentation | yes | low | FIX NOW | "the 'Known infra gotchas' section **above**" — that section starts at :405, below |
| C12 | `CLAUDE.md:375` | documentation | yes | low | FIX NOW | `pixi run kinoforge sweeper start &` exits 2 (`-c PATH` required) — the documented safety net starts nothing |
| C13 | `CLAUDE.md:438` "the guard covers sixteen configs" | documentation | yes | low | LEAVE | dated narrative (2026-09-23); the guard now parametrises 20 configs (`--collect-only`). Optional "(20 at 2026-10-21)" |
| C14 | `.github/workflows/smoke-wan21-weekly.yml:7`, `docs/RELEASE-CHECKLIST.md:17-18`, `docs/warm-reuse.md:306-308`, `README.md:162` | documentation | yes (comments/docs) | low | FIX NOW (:7, README:162, checklist) / NEEDS DISCUSSION (warm-reuse:306-308 wording depends on the Finding #0 decision) | all present the leak-sweep cron as the standing guard that "reaps tier-tagged pods"; no pod is ever tagged (A row), and the 10-18 audit records the cron as disabled |
| C15 | `README.md:147` | documentation | yes | low | FIX NOW | "Needs `KINOFORGE_LIVE_TESTS=1`" — the task sets it (B20); the note should warn, not reassure |
| C16 | `docs/RELEASE-CHECKLIST.md:14` | documentation | yes | low | FIX NOW | "bounded by `BudgetTracker(cap_usd=2.00)`" — per-module caps are 2.00/1.50/4.0 (`_BUDGET_CAP`); this is the harness tracker, not `core/lifecycle.py`'s dead one |
| C17 | `.claude/hooks/summarize-and-exit.sh` + `.claude/CLAUDE_SETTINGS_summarize_and_exit.json` | dead (scaffold) | n/a | med (operator tooling) | NEEDS DISCUSSION | not registered in `.claude/settings.json`; the only references are each other; writes `PROGRESS001.md`-style files the project does not use and shells `claude -p`. Fence: `c2043deb chore: scaffold project` — operator-owned scaffold, not project code |
| C18 | `pyproject.toml:55-58` (rasterio/netCDF4 override stub), `:123-125` (uv note) | documentation | yes | low | LEAVE | earth-science scaffold boilerplate; same family as ND #23 — operator template decision |
| C19 | `docs/windows-migration-cancelled.md` | documentation | yes | low | NEEDS DISCUSSION | body is titled "Plan: Fix windows-latest CI failure by adding real win-64 support" with no status/cancelled marker, while `ci.yml:18` cites it as the reason Windows was declined; needs a one-line status header the operator should word |
| C20 | `docs/cloud-stores.md:100-102` | documentation | yes | low | NEEDS DISCUSSION (ND #6) | interim wording offered in E |
| C21 | `pixi.toml:133-137` (`test-live-skypilot` comment: GCP SA with compute.admin, "smallest GCP CPU SKU") | documentation | yes | low | LEAVE | matches `tests/live/test_skypilot_live.py:4-13` (that test IS the GCP one); stale only relative to the Phase-53 Lambda pivot, which is a product decision, not a doc error |
| C22 | `tools/smoke_leak_sweep.py:1-11` docstring "Designed to run every 30 min via leak-sweep.yml" | documentation | — | — | LEAVE | accurate for the file; the cron's live state is not checkable offline |
| C23 | `examples/configs/runpod-comfyui-wan-2_1-{14b-t2v,1_3b-t2v,14b-i2v}.yaml` BudgetTracker comments | documentation | yes (YAML comments; parsed Config unchanged; goldens pin the rendered payload, not comments) | low | FIX NOW | see A (ND #5 row) |

---

## D. FIX NOW edit list

All items: `embedded: no` — none of these files appear in any `embed_files`/`embed_modules` list
(`rg -n 'smoke_leak_sweep|_redact|validate_scoped|render_aws|tools/' examples/configs
tests/providers/test_pod_embed_closure.py` → only YAML *comments* citing tools, no embeds).
Markdown links of the form `[text](../x)` are relative links and must be left alone; only shell
paths / backtick paths lose their `../`.

### D.1 Docs-only

Tests to run after the docs batch: `tests/test_redact_hook_parity.py` (reads CLAUDE.md +
AGENTS.md for a banned env-var name), `tests/test_source_audit.py`,
`tests/test_cloud_identifier_scrub.py` (tracked-tree scanners), `tests/tools/test_preflight.py`
(mentions README.md only in a fake git-status string). No test asserts on README cheatsheet or doc
anchor text (`rg -l 'README\.md|docs/[a-z-]+\.md' tests --glob '*.py'` → 6 files, none asserting
doc content).

1. `README.md:83` — ``git clone https://github.com/emmykillett/kinoforge.git`` → ``git clone https://github.com/killett/kinoforge.git``
2. `README.md:113` — after the `generate` row insert:
   ``| `image` | Generate a single image from a prompt (no compute) | `pixi run kinoforge image -c cfg.yaml --prompt "…"` |``
   ``| `text` | Generate text with an open-weight LLM on a reserved pod | `pixi run kinoforge text -c cfg.yaml --prompt "…"` |``
3. `README.md:119` — ``| `gc` | Garbage-collect stored artifacts | `pixi run kinoforge gc` |`` → ``…| `pixi run kinoforge gc --config cfg.yaml` |`` (note: `gc` has no `-c` short form)
4. `README.md:120` — ``| `cost` | Show cost report | `pixi run kinoforge cost` |`` → ``…| `pixi run kinoforge cost -c cfg.yaml` |``
5. `README.md:121` — ``| `pod lora ls` | List LoRA inventory on a live pod | `pixi run kinoforge pod lora ls --id <id>` |`` → ``…| `pixi run kinoforge pod lora ls <pod-id>` |``
6. `README.md:122-124` — the three ```pixi run kinoforge sweeper {start,stop,status}` `` cells → append `` -c cfg.yaml``
7. `README.md:125` — ``| `sweeper metrics` | Fetch sweeper metrics | `pixi run kinoforge sweeper metrics` |`` → ``…| `pixi run kinoforge sweeper metrics -c cfg.yaml --prom` |``
8. `README.md:147` — ``| `pixi run test-live` | Run live integration tests | Needs `KINOFORGE_LIVE_TESTS=1` + cloud creds |`` → ``| `pixi run test-live` | Run ALL live integration tests (85 modules; books real pods) | Sets `KINOFORGE_LIVE_TESTS=1` itself — run `pixi run preflight` first and prefer one module via `pixi run pytest tests/live/<file> -m live` |``
9. `README.md:160` — ``| `pixi run smoke-21b-live` | Live smoke: Wan 2.1 21B | Costs ~$0.10–0.30 |`` → ``| `pixi run smoke-21b-live` | Live smoke: Wan 2.1 1.3B (`tests/smoke/live_wan21/`) | $0.30 cap per module |``
10. `README.md:161` — ``| `pixi run smoke-wan22-live` | Live smoke: Wan 2.2 14B | Costs ~$0.10–0.30 |`` → ``| `pixi run smoke-wan22-live` | Live smoke: Wan 2.2 14B (`tests/smoke/release_wan22/`) | Per-module caps $1.50–$4.00; expect $1–2 |``
11. `README.md:162` — ``| `pixi run smoke-leak-sweep` | Sweep for cost/resource leaks | Runs leak-detection checks |`` → ``| `pixi run smoke-leak-sweep` | Destroy RunPod pods past the per-tier age ceiling | DANGER: ages pods from a `created_at` sentinel — reaps every pod (Finding #0, `docs/hygiene-audit-2026-10-18.md`); fix before use |``
12. `README.md:461` — ``| `hosted.yaml` | Multi-provider hosted comparison (Replicate / Runway / Luma) |`` → ``| `hosted.yaml` | `HostedAPIEngine` against a user-deployed inference shim (synthetic submit/poll contract — no public provider implements it) |``
13. `README.md:560` — ``| ComfyUI poll timeout (~10 min default) |`` → ``| ComfyUI poll timeout (30 min default) |``
14. `docs/configuration.md:34` — ``| [`hosted.yaml`](../examples/configs/hosted.yaml) | Hosted API | fal.ai | Zero-infra hosted |`` → ``| [`hosted.yaml`](../examples/configs/hosted.yaml) | Hosted API | user-deployed shim | Synthetic submit/poll contract (not a public provider) |``
15. `docs/configuration.md:180` — ``(animated GIFs and multi-page TIFFs contribute their first frame).`` → ``(animated inputs are handled frame by frame — see below).``
16. `docs/troubleshooting.md:11` — ``See [../CLAUDE.md](../CLAUDE.md#pre-commit-stages-pixilock).`` → ``See [../CLAUDE.md](../CLAUDE.md).``
17. `docs/troubleshooting.md:29` — ``lifecycle.md#kinoforge-forget---id-id----clear-a-stale-ledger-entry`` → ``lifecycle.md#kinoforge-forget---id-id--clear-a-stale-ledger-entry``
18. `docs/troubleshooting.md:41` — ``| ComfyUI poll times out (default 600s) on Wan 14B t2v | Wan 14B t2v can take ~6 min per step; default ceiling too low |`` → ``| ComfyUI poll times out (default 1800 s) on Wan 14B t2v | Wan 14B t2v can take ~6 min per step; a long run can exceed the ceiling |``
19. `docs/troubleshooting.md:44` — ``warm-reuse.md#kinoforge-generate---loras----cli-lora-stack-override`` → ``warm-reuse.md#kinoforge-generate---loras--cli-lora-stack-override``
20. `docs/lifecycle.md:234-237` — replace the four lines with:
    ```
    kinoforge sweeper start   -c cfg.yaml          # foreground; blocks until SIGTERM
    kinoforge sweeper stop    -c cfg.yaml          # SIGTERM the daemon owning sweeper:<host>
    kinoforge sweeper status  -c cfg.yaml          # human or --json output
    kinoforge sweeper metrics -c cfg.yaml --prom   # textfile-collector target (--prom is required)
    ```
21. `docs/lifecycle.md:365` — delete the line ``    poll_interval_s: 2.0`` (not a field; silently dropped)
22. `docs/lifecycle.md:366` — ``    poll_timeout_s: 600.0    # raise for known-slow models (Wan 14B t2v ~6 min)`` → ``    poll_timeout_s: 1800.0   # the default; raise for known-slow models (Wan 14B t2v ~6 min/step)``
23. `docs/lifecycle.md:369` — ``Default is `600.0` s (10 min).`` → ``Default is `1800.0` s (30 min).``
24. `docs/output-layout.md:10` — ``    YYYYMMDD-HHMMSS_<prompt-slug>.<ext>`` → ``    YYYYMMDD-HHMMSS_[<kind>_]<provider-slug>_<model-slug>_<prompt-slug>.<ext>`` and after the slug bullet (:13-16) add: ``* `<provider-slug>` (≤20 chars) and `<model-slug>` (≤24 chars) come from the run's provider/model, falling back to `unknown`; `<kind>` is present only for tagged artifacts (e.g. `keyframe-init`).``
25. `docs/warm-reuse.md:127` — ``kinoforge generate -c ../examples/configs/wan21-1_3b.yaml \`` → ``kinoforge generate -c examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml \``
26. `docs/warm-reuse.md:135` — ``kinoforge generate -c ../examples/configs/wan22-14b.yaml \`` → ``kinoforge generate -c examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml \``
27. `docs/warm-reuse.md:360` and `:432` — ``(#known-keys)`` → ``(credentials.md#known-keys)`` (2 occurrences; both intended — `replace_all`)
28. `docs/warm-reuse.md:399-400` — ``in `../src/kinoforge/core/vault.py` `` → ``in `src/kinoforge/core/vault.py` ``; ``and `CapabilityKey.derive()` in `../src/kinoforge/core/profiles.py`). Always`` → ``and `CapabilityKey.derive()` in `src/kinoforge/core/interfaces.py`). Always``
29. `docs/warm-reuse.md:440` — ``(`../src/engines/diffusers/`)`` → ``(`src/kinoforge/engines/diffusers/`)``
30. `docs/breaking-changes.md:312` — ``[Migration from a local ledger](#migration-from-a-local-ledger)`` → ``[Migration from a local ledger](cloud-stores.md#migration-from-a-local-ledger)``
31. `docs/engines.md:13` — ``| Generation engines | `engine.type` | `get_engine` |`` → ``| Generation engines | `engine.kind` | `get_engine` |``
32. `docs/engines.md:192` — ``| i2v (image-to-video) | Supply your own init image via `--init-image` |`` → ``| i2v (image-to-video) | Supply your own init image as the cfg's conditioning asset (there is no `--init-image` flag) |``
33. `docs/engines.md:223` — ``supply `--init-image` at the CLI.`` → ``supply an init image yourself (there is no `--init-image` flag).``
34. `docs/engines.md:336-339` — replace the block with
    ```
    pixi run kinoforge generate \
      --config examples/configs/runpod-comfyui-wan-2_1-14b-i2v.yaml \
      --prompt "a cat turns into a woman" --mode i2v
    ```
    (the init frame comes from the cfg; `--mode` is required). Residual: I did not verify how that cfg names its init asset — if the controller prefers, keep the block but delete only the `--init-image …` line and add `--mode i2v`.
35. `docs/engines.md:359` — ``- `../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml` — Wan 2.1 i2v engine config`` → ``- `examples/configs/runpod-comfyui-wan-2_1-14b-i2v.yaml` — Wan 2.1 i2v engine config``
36. `docs/engines.md:367` — ``Eleven example configs ship as `../examples/configs/modal-*.yaml`, covering`` → ``Thirteen example configs ship as `examples/configs/modal-*.yaml`, covering``
37. `docs/engines.md:375` — ``  `lifecycle.boot_timeout_s` (default 1800 s). A serialized function drops a`` → ``  `lifecycle.boot_timeout` (default 900 s; Modal falls back to 1800 s only when it is 0). A serialized function drops a``
38. `docs/engines.md` `../` shell/backtick paths at :34,38,87,88,135,148,286,289,297,311,313,331,337,345,347,360,404,405,469,566,567,569,682,683 — strip the leading `../` (`../examples/` → `examples/`, `../tests/` → `tests/`, `../.env.example` → `.env.example`). Verify: ``rg -n '\.\./' docs/engines.md | rg -v '\]\(\.\./'`` → 0 hits.
39. `docs/cost-and-spend.md:11-13` — ``-c ../examples/configs/cost.yaml`` ×3 → ``-c examples/configs/cost.yaml``
40. `docs/batch-and-grid.md:11-12` — ``-c ../examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml \`` / ``--manifest ../examples/configs/manifests/batch-prompts.yaml`` → drop `../`; `:21` comment likewise. **Leave `:26` `prompt_file: ../prompts/forest.txt`** — that is manifest-relative and correct.
41. `docs/releasing.md:21` — ``See `../tools/release.py` for the contract and `../tests/tools/test_release.py` `` → drop both `../`.
42. `docs/extending.md:15` — ``    ComputeProvider, GpuOffer, InstanceSpec, Instance, Lifecycle,`` → ``    ComputeProvider, InstanceSpec, Instance, Lifecycle,``
43. `docs/extending.md:64,71-72` — ``from kinoforge.core.interfaces import ModelSource, Artifact`` → ``from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource``; ``    def resolve(self, ref: str) -> Artifact:`` → ``    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:``; ``        # return an Artifact with url + headers`` → ``        # return one Artifact per file, each with url + headers``
44. `docs/extending.md:84,91-93` — ``from kinoforge.core.interfaces import GenerationEngine, GenerationBackend`` → ``from kinoforge.core.interfaces import (CapabilityKey, GenerationBackend, GenerationEngine, GenerationJob, ModelProfile)``; replace the three method stubs with
    ```
        def provision(self, instance, cfg) -> None: ...
        def backend(self, instance, cfg) -> GenerationBackend: ...
        def profile_for(self, key: CapabilityKey) -> ModelProfile: ...
        def declared_flags(self, key: CapabilityKey) -> dict[str, bool]: ...
        def validate_spec(self, job: GenerationJob) -> None: ...
        def model_identity(self, cfg: dict[str, object]) -> str: ...
    ```
45. `docs/extending.md:131-138` — after ``    def uri_for(self, run_id: str, name: str) -> str: ...`` add the four remaining abstract methods, copying the signatures from `src/kinoforge/stores/base.py:131,149,166,182` (`acquire_lock(self, key, *, ttl_s) -> Lock`, `delete_run(self, run_id) -> None`, `manual_cleanup_command(self, run_id) -> str`, `signed_url(…)`), and add the `Lock` import the way `stores/base.py` does.
46. `docs/credentials.md:104-105` — ``| `gcp_service_account` | VeoEngine (Layer 2); future Vertex AI integrations |`` → ``| `gcp_service_account` | no shipped engine today; future Vertex AI integrations |``; ``| `aws_sigv4` | NovaReelEngine (Layer 3); future Bedrock integrations |`` → ``| `aws_sigv4` | the `bedrock_video` engine (`BedrockVideoEngine`) |`` — *the sigv4↔bedrock_video wiring itself was not re-verified by me; the class-name part is certain.*
47. `docs/credentials.md:107-117` — replace from ``Each engine config carries a nested `auth:` block`` through the end of the YAML block with: ``Hosted configs do **not** carry an `auth:` block — `HostedEngineConfig` is `extra="forbid"` (`src/kinoforge/core/config.py:366`) and rejects one at load. The strategy is derived by the engine:`` and keep the existing "Backward-compat" sentence reworded to ```provision()` builds `Bearer(env_var=cfg.api_key_env)` from the hosted config's `api_key_env`.``
48. `docs/RELEASE-CHECKLIST.md:10-11` — ``- [ ] Tier 3 last weekly run (check the most recent\n  `smoke-wan21-weekly` GH Actions run) — green within the last 7 days.`` → ``- [ ] Tier 3: dispatch `smoke-wan21-weekly` (workflow_dispatch only since 2026-09-21) or run `pixi run smoke-21b-live` locally — green within the last 7 days.``
49. `docs/RELEASE-CHECKLIST.md:14` — ``min; expected spend $1-2; bounded by `BudgetTracker(cap_usd=2.00)`.`` → ``min; expected spend $1-2; each module is bounded by its `_BUDGET_CAP` ($1.50–$4.00, harness `tests/_smoke_harness/budget.py`).``
50. `docs/RELEASE-CHECKLIST.md:17-18` — ``  `destroy-pods-when-work-is-done` memory, the smoke's finally + the\n  leak-sweep cron together cap any leak at 90 min.`` → ``  `destroy-pods-when-work-is-done` memory, the smoke's finally + a post-run `kinoforge list` are the leak guard (the leak-sweep cron is disabled — Finding #0 in `docs/hygiene-audit-2026-10-18.md`).``
51. `docs/RELEASE-CHECKLIST.md:24-25` — ``- [ ] Bump version in `pyproject.toml`.\n- [ ] `git tag v<version>` + `git push --tags`.`` → ``- [ ] `pixi run release X.Y.Z [--note "…"]` — bumps `pyproject.toml`, commits, and creates the annotated tag (see `docs/releasing.md`).\n- [ ] `git push origin main --follow-tags`.``
52. `AGENTS.md:45-46` — ``- New credential format → add a regex to `_CREDENTIAL_PATTERNS` in\n  `tests/providers/conftest_runpod.py`, then add a parametrised unit test in`` → ``- New credential format → add a `CredentialPattern` to `CREDENTIAL_PATTERNS` in\n  `src/kinoforge/core/credential_patterns.py` (the conftest imports it), then add a parametrised unit test in``
53. `AGENTS.md:55-56` — ``1. Add the regex to `_CREDENTIAL_PATTERNS` in `tests/providers/conftest_runpod.py` (canonical\n   snake_case name + compiled `re.Pattern`).`` → ``1. Add the `CredentialPattern` to `CREDENTIAL_PATTERNS` in `src/kinoforge/core/credential_patterns.py` (canonical snake_case name, compiled `re.Pattern`, strict flag) and mirror it in `.claude/hooks/redact_secrets.py` — `tests/test_redact_hook_parity.py` enforces the two stay identical.``
54. `AGENTS.md:24` — ``3. **Value-pattern matcher** (`_redact_credential_patterns`) — recursive sweep that catches:`` → ``3. **Value-pattern matcher** (`_redact_credential_patterns`) — recursive sweep over `CREDENTIAL_PATTERNS` (16 patterns today; the table shows the original seven):``
55. `AGENTS.md:65` — ``All five MUST live in `.env` (gitignored).`` → ``All of these MUST live in `.env` (gitignored).``
56. `AGENTS.md:76` — ``- Git history scrub — the project has never committed a real credential per PROGRESS:213.`` → ``- Git history scrub — the project has never committed a real credential (standing guard: `tests/test_source_audit.py`).``
57. `CLAUDE.md:169-170` — ``# wrong — pixi run is the task runner; without a matching task name\n# it just prints the task list. No error, just confusing output.`` → ``# wrong — `pixi run -- <cmd>` runs <cmd> in the DEFAULT env, where gcloud\n# is not installed: "gcloud: command not found".``
58. `CLAUDE.md:361` — ``{ id } }` probe named in the "Known infra gotchas" section above.`` → ``{ id } }` probe named in the "Known infra gotchas" section below.``
59. `CLAUDE.md:375` — ``pixi run kinoforge sweeper start &`` → ``pixi run kinoforge sweeper start -c <cfg> &``
60. `CLAUDE.md:416` — ``(`_create_pod`, commit `5418c35`)`` → ``(`_create_pod`, commit `90404cc5`)``
61. `CLAUDE.md:458-459` — ``(commit\n`8438a8b`)`` → ``(commit\n`70cd3992`)``
62. `pixi.toml:128-131` — replace the four comment lines with ``# Opt-in live tests that hit real provider APIs. This task sets KINOFORGE_LIVE_TESTS=1\n# ITSELF, so one command books RunPod/Modal/SkyPilot/hosted jobs across the ~85 modules\n# under tests/live/ with no preflight — run `pixi run preflight` first and prefer a single\n# module (`pixi run pytest tests/live/<file> -m live`).``
63. `.github/workflows/smoke-wan21-weekly.yml:7` — ``# The independent leak-sweep cron remains the standing money guard.`` → ``# The leak-sweep cron was disabled 2026-10-18 (Finding #0, docs/hygiene-audit-2026-10-18.md);\n# the always() sweep step below runs the same tool — fix its age source before dispatching.``
64. `.github/workflows/smoke-wan21-weekly.yml:13` — ``  issues: write   # post issue on test failure`` → ``  issues: write   # the always() sweep step files one GitHub issue per reaped pod``

### D.2 Code (comments/docstrings and one dead alias)

Tests to run: `tests/tools/test_redact.py`, `tests/core/test_credential_patterns.py`,
`tests/test_redact_hook_parity.py` (item 65); `tests/tools/test_validate_scoped_policy.py`,
`tests/tools/test_render_aws_policy.py` (66-69); `tests/providers/test_launch_payload_goldens.py`,
`tests/providers/test_env_payload_ceiling.py`, `tests/integration/test_no_unknown_slug_for_example_configs.py`,
`tests/validation/test_shipped_configs_capabilities.py` (70-72 — comment-only YAML edits; parsed
`Config` is unchanged so goldens must not move; run them to prove it).

65. `tools/_redact.py:31-35` — delete
    ```
    # Backward-compatible alias. The list itself now lives in
    # kinoforge.core.credential_patterns — see that module's docstring for
    # the loose/strict tier split. Callers that imported this name keep
    # working; new code should import from the shared module directly.
    _CREDENTIAL_PATTERNS = CREDENTIAL_PATTERNS
    ```
    and (since `CREDENTIAL_PATTERNS` then has no other use in the module — verify with `rg -n CREDENTIAL_PATTERNS tools/_redact.py`) drop `CREDENTIAL_PATTERNS` from the import at :28 if it becomes unused (ruff F401 will say). Fence: `a8876b44` (alias added as back-compat during the refactor; `git log -S'_CREDENTIAL_PATTERNS' -- tools/_redact.py` → a8876b44, 5167719c, 560c48c7 — the two older commits are the pre-refactor list itself). Proof of no consumer: `rg -n '_CREDENTIAL_PATTERNS' tools tests src .claude --glob '!*.pyc' | rg -v conftest_runpod` → 2 hits, both non-importing (`tools/_redact.py:35` itself; comment `tests/tools/test_redact.py:49`). Optionally reword that comment to "declaration order in `CREDENTIAL_PATTERNS`".
66. `tools/validate_scoped_policy.py:28` — ``documented at `tools/cloud_perms_probe.py:201-241`).`` → ``documented at `tools/cloud_perms_probe.py` (`_AWS_KMS_ACTIONS` + the two-pass simulation in `probe_aws`)).``
67. `tools/validate_scoped_policy.py:94` — ``# in `tools/cloud_perms_probe.py:33-35`.`` → ``# in `tools/cloud_perms_probe.py` (its `_REPO_ROOT` sys.path insert).``
68. `tools/validate_scoped_policy.py:496` — ``    `tools/cloud_perms_probe.py:209-215`.`` → ``    `tools/cloud_perms_probe.py` (`probe_aws`'s `sts.get_caller_identity()`).``
69. `tools/render_aws_policy.py:215-216` — ``            to `.aws/kms-test-key.arn`, the same file\n            `tools/cloud_perms_probe.py:68` reads.`` → ``            to `.aws/kms-test-key.arn`, the same file\n            `tools/cloud_perms_probe.py` (`_AWS_KMS_KEY_ARN_FILE`) reads.``
    `rg -n 'cloud_perms_probe.py:' tests` → 0 (no test pins these strings).
70. `examples/configs/runpod-comfyui-wan-2_1-14b-t2v.yaml:20` — ``#   - budget: 4.00                    (BudgetTracker tears down mid-run; 2× i2v for 2 generations)`` → ``#   - budget: 4.00                    (shortens the pod's selfterm cap to budget ÷ hourly rate at create; 2× i2v for 2 generations)``; `:89-90` — ``    # per generation; the cap is the BudgetTracker's mid-run circuit\n    # breaker, not an estimate.`` → ``    # per generation; the cap bounds the pod's selfterm lifetime\n    # (budget ÷ hourly rate), not an estimate.``
71. `examples/configs/runpod-comfyui-wan-2_1-1_3b-t2v.yaml:29` — ``#   - budget: 2.00                    (BudgetTracker tears down mid-run)`` → ``#   - budget: 2.00                    (shortens the pod's selfterm cap to budget ÷ hourly rate at create)``; `:101-102` — ``    # offer (~$0.16/hr × 30m = $0.08); the $2 cap is the\n    # BudgetTracker's mid-run circuit breaker, not an estimate.`` → ``    # offer (~$0.16/hr × 30m = $0.08); the $2 cap bounds the pod's\n    # selfterm lifetime (budget ÷ hourly rate), not an estimate.``
72. `examples/configs/runpod-comfyui-wan-2_1-14b-i2v.yaml:21` — ``#   - budget: 2.00                    (BudgetTracker tears down mid-run)`` → same replacement as 71.
    `rg -n 'BudgetTracker' tests/integration tests/validation tests/providers` → 0 asserting on these comments.

---

## E. Needs discussion

1. **Leak-sweep narrative across docs** (`docs/warm-reuse.md:306-308`, `README.md:162`,
   `RELEASE-CHECKLIST.md:17-20` incl. the `gh issue list -l leaked-smoke-pod` check for a label the
   10-18 audit says does not exist). Wording depends on the Finding #0 decision (fix the age
   source and re-enable vs retire the cron). D items 11/50/63 use the "disabled, fix before use"
   wording; the operator may prefer to wait for the decision.
2. **Closed-investigation tools purge** (ND #21), with precise collateral per tool:
   - `tools/c33_j_probe.py` (360), `c33_k_probe.py` (492), `debug_ledger_watcher.py` (82): **zero
     references anywhere** (not even PROGRESS). Fences: `8d308d07`, `e80e9774`, `11253c14` — all
     "diag/probe" commits for closed investigations. Takes nothing else with it.
   - `c33_l_metricwatch.py` (224), `c33_denial_branch.py` (153), `diagnose_pod_boot.py` (504),
     `diagnose_u13_exit_holder.py` (193): PROGRESS / `docs/superpowers` plan refs only (historical).
   - `uptime_field_hourly_sweep.py` (310) + `uptime_sweep_summary.py` (50) + `uptime_sweep_status.sh`
     (32): only reference each other; `repro_runpod_uptime.py` + the `.jsonl` log stay (keeps).
   - `quota_burn.py` + `quota_burn_lib.py` (1,157) → takes 6 test files (1,432 LOC) and the
     `google-cloud-bigquery/-billing/-billing-budgets/-quotas` PyPI deps; README:525,563,
     troubleshooting.md:52 and `docs/quota-justification-*.md` reference it (README row is the
     B14-era GCP billing note — would need a rewrite, or keep the tool).
   - `build_bsa_wheel.py` (400) + `tests/tools/test_build_bsa_wheel.py` (60): cited as wheel
     provenance by `runpod-diffusers-flashvsr-x4-upscale.yaml:40` and successful-generations —
     deleting it orphans the provenance pointer; the Modal sibling is alive (5 configs cite it).
   - `tools/local_hooks/` (1,362) + `tests/hooks/test_tasks_live_query.py` (268; runs in
     `pixi run test`) + `docs/hook-differential-test-results-2026-06-18.md`: user memory says
     "no transcript-reading hooks" — candidate for removal with its test.
3. **Dependency cleanup** (`pixi.toml:78-82,89-92,96,244-245`): `lumaai`/`fal-client` have no
   importer (the :239-241 "lazy-imported" comment is false); `httpx` twice; `typer/rich/stamina/
   vermin/httpx` are an operator toolkit keep. Any change regenerates `pixi.lock` (hands-off) and,
   per memory, needs `pixi lock` then `pixi install` under `PIXI_FROZEN`.
4. **CI pin drift** (ND #22): align `leak-sweep.yml:20-21`, `smoke-wan21-weekly.yml:27-28`,
   `build-wan-comfyui-image.yml:35` to `checkout@v6` / `setup-pixi@v0.9.6`; decide whether
   `ci.yml:33 pixi-version: latest` should pin. Behaviour-affecting for CI → operator call.
5. **`.claude/hooks/summarize-and-exit.sh` + `CLAUDE_SETTINGS_summarize_and_exit.json`** (C17):
   unregistered scaffold hook that would write `PROGRESS001.md`… files; delete, or keep as an
   operator opt-in and say so in a comment.
6. **README "Project structure" section** (user convention, carried since 07-16): content to add
   — `src/kinoforge/{cli,core,engines,providers,stores,sources,pipeline,outputs,validation,
   upscalers,image_engines,text_engines,diagnostics}`, `tests/`, `tools/`, `examples/configs/`
   (`grids/`, `manifests/`, `extras/`, `comparison/`, `prompts/`), `docs/`, `docker/`. Needs a
   one-line purpose per entry the operator may want to word.
7. **`docs/cloud-stores.md:100-102`** (ND #6): either wire `resolve_or_discover` into
   `deploy_session` or reword to "available (`JsonProfileCache.resolve_or_discover`) but the
   `generate` path calls `resolve`/`discover` directly today".
8. **`docs/windows-migration-cancelled.md`** (C19): add a status header ("Cancelled 2026-…; see
   `ci.yml:18`") — operator to word.
9. **`pixi.toml:1-27` template header + `pyproject.toml:55-58,123-125` scaffold comments**
   (ND #23 / C18): operator-owned boilerplate; delete or keep as a set.
10. **`docs/credentials.md:104-105` "Used by" column** (D46): I verified the class names, not the
    strategy wiring; confirm `BedrockVideoEngine` really goes through `AWSSigV4` before accepting
    the replacement text.

---

## F. Cross-area notes

- `tests/tools/test_smoke_leak_sweep.py:12-22` — confirmed: the fake sets `created_at` and a
  `smoke_tier` tag the real provider never produces (hides Finding #0). `tests/_smoke_harness/
  runpod_lifecycle.py:110` `tag_filter` on `smoke_tier` can never match for the same reason.
- `docs/hygiene-notes.md` (excluded from my scope) has two stale pointers: the c28 keep cites
  `core/orchestrator.py:248` (now :276-280), and it cites `e82b0d1` for the FlashVSR fix — that
  SHA does not resolve in this repo (`git log --all`). Several user-memory SHAs (`5fcfb9c`,
  `e82b0d1`, `7b820a1`) likewise do not resolve; memory is outside the repo.
- `src/kinoforge/core/profiles.py:445-523` `resolve_or_discover` / `_discover_single_flight`
  remain test-only (core area; ND #6).
- `core/lifecycle.py::BudgetTracker` still has zero production callers (ND #5); the shipped
  budget semantics live in `validation/checks/capabilities.py:201-209` — worth saying in the
  `LifecycleConfig.budget` docstring (`core/config.py:103`) when core is edited.
- `tools/snapshot_launch_payloads.py:217` `CONFIG_DIR.glob("*.yaml")` is still non-recursive
  (CLAUDE.md:434-436 describes this correctly; only the test helper recurses) — a regenerated
  golden set therefore does not cover `grids/` configs; no finding, just a reminder for whoever
  next runs the snapshot.
- `pyproject.toml:112-118` has no `testpaths` / `xfail_strict` / `--strict-markers` (prior ND
  #20, tests area).
- The controller's `58d27ef0` noqa cleanup touched six `tools/*.py`; none of the D.2 edits above
  overlap those lines (`_redact.py`, `validate_scoped_policy.py`, `render_aws_policy.py` were not in
  that commit).
