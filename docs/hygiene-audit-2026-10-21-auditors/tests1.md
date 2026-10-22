# Hygiene re-audit 2026-10-21 — tests-1 (root `tests/*.py`, `tests/{core,cli,pipeline,outputs,integration,hooks,_fixtures}`)

Scope: 347 test modules, ~62k LOC. Method: every prior row re-verified against HEAD `f89eff2b`
(test files unchanged by the controller's later src-only cleanups), then a fresh mechanical pass
(AST sweep for assert-less / shadowed tests; multi-line `rg` for swallow / except→skip / `or True`
shapes; discovery-site inventory with empirical from-another-CWD and synthetic-credential runs).
All runs were offline, `-p no:cacheprovider`, no live tests, no `KINOFORGE_LIVE_TESTS`.
`src/` line numbers below are pre-cleanup and may be off by a few lines; test lines are exact.

## A. Prior-claim verification

| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line, ≤2 lines) |
|---|---|---|---|
| P3 `tests/conftest.py:588-603` | 15 s `dump_traceback_later` armed at configure fires 15 s into every healthy run; comment claims it fires only if sessionfinish is never reached | CONFIRMED | Empirical: a 1-test module sleeping 17 s, run with the root conftest loaded (`-p tests.conftest -s`), prints `Timeout (0:00:15)!` + full thread stack, then `1 passed in 17.09s`. `cancel_dump_traceback_later()` is only reached at `tests/conftest.py:631` (sessionfinish), i.e. after the whole session; comment at `:582-584` ("so healthy runs do NOT also dump from the timer") is true only for sessions <15 s. Under default capture the dump lands in whichever test's "Captured stderr" is active at t=15 s (shown only if that test fails); under `-s` it hits the terminal. |
| FIX NOW `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py:16-19` | docstring says "marked XFAIL … remove `@pytest.mark.xfail` in the same commit" — stale | CONFIRMED | `rg -n 'mark\.xfail' <file>` → only the docstring lines 17-18; `git log -S'pytest.mark.xfail' -- <file>` → `a392deaf` (added) then `4ceb4fdf fix(orchestrator): warm-attach teardown exits cleanly under --ephemeral` (removed). |
| FIX NOW `tests/core/test_heartbeat_endpoints.py:4-5` | says fake doubles live in `tests/providers/conftest.py` | CONFIRMED | `tests/core/test_heartbeat_endpoints.py:22: from tests.conftest import FakeRunPodHeartbeatEndpoint`; fakes defined at `tests/conftest.py:26` and `:72`. |
| dead `tests/integration/conftest.py` | redaction-registry clear subsumed by the root autouse | CONFIRMED | Root `tests/conftest.py:117-128` `_clear_redaction_registry_between_tests` is function-scoped autouse and clears BEFORE and AFTER; integration `:16-19` `_clear_redaction_registry` clears before only. Different names → no override, both run (parent conftest autouse first). Ordering is therefore irrelevant: clearing an already-cleared singleton twice is a no-op. Fence: root fixture from `101dc3d9`, integration from `82c8f59f`; neither commit records a reason the extra clear is needed. |
| dead `SequencedEngine` | unreferenced helper | CONFIRMED (+1 missed) | `rg -n SequencedEngine tests src tools` → only its def `tests/core/test_orchestrator.py:204`. Its sole collaborator `SequencedBackend` (`:188-202`) is referenced only by `SequencedEngine` (`:205,:220,:224`) → also dead; prior audit missed it. `git log -S` → single commit `679a7cb3` (dead since birth). |
| dead `_CountingSleep` | unreferenced | CONFIRMED | `tests/core/test_session_claim.py:39-50`, only hit; `git log -S` → `6a42d0ec` only. |
| dead `_record_and_set_tick` | unreferenced | CONFIRMED | `tests/core/test_session_claim.py:33-36`, only hit; `6a42d0ec` only. |
| dead `_swap_argv_assertions` | unreferenced | CONFIRMED (keep-reason false) | `tests/core/test_grid_executor_swap_group.py:363-365`; docstring claims "Module marker so collector finds the file" — pytest collects by the `test_*.py` filename, the function does nothing. `c00b28e9` only. |
| dead `_make_arg_capture` | unreferenced | CONFIRMED | `tests/core/test_orchestrator_diagnostic_env.py:167-176` ("# for future orchestrator-level integration test"); `55e35f0f` only. |
| dead `_spy_downloader` | unreferenced | CONFIRMED | `tests/core/test_provisioner.py:108-115`; `bdfc73a1` only. |
| dead `_make_vault_no_alias` | unreferenced | CONFIRMED | `tests/core/test_provision_state_alias_keyed.py:101-108`; `dee647ba` only. |
| dead `HttpGetSpy`, `_FakeS3`, `_PROBE_*_SCRIPT` | unreferenced | (outside my area) | `tests/providers/test_runpod.py:190`, `tests/diagnostics/test_c33_orchestrator.py:28`, `tests/providers/test_field_consumption_parity.py:733-734` — single hits each; routed under F. |
| strength "six discovery guards with no plausible-count floor; three root at CWD-relative paths and pass with zero inputs" | | PARTIAL (undercount / overcount) | Floorless sweeps in my area = **9 sites / 8 files**: `tests/test_pod_models_dir_seam.py:109`, `tests/test_no_precedence_branches_outside_resolver.py:52`, `tests/test_no_unredacted_writes.py:31` (planted-code positive controls exist for the matcher, none for the tree), `tests/core/test_no_top_level_pod_uptime_reads.py:42`, `tests/core/test_compute_surface.py:305`, `tests/test_ephemeral_index_write_gated.py:39`, `tests/test_core_invariant.py:136,186,211,268`, `tests/test_live_gating_lockdown.py:36`, `tests/test_examples.py:749`. CWD-relative roots = 5 sites / 4 files (`test_no_precedence…:48`, `test_compute_surface:305`, `test_layer_r_backcompat:30`, `test_examples:179,610`); run from the scratchpad dir only **2** pass vacuously (`test_no_precedence_branches_outside_resolver` PASSED; `test_compute_surface::test_every_shipped_config_still_loads` PASSED); `test_layer_r_backcompat` FAILS (`assert yamls`), `test_examples:179` SKIPS ("got empty parameter set"), `test_examples:610` FAILS (name membership). |
| strength `tests/test_live_gating_lockdown.py:36` non-recursive | misses `tests/stores/live/`, `tests/smoke/live_wan21/`, `tests/smoke/release_wan22/` | CONFIRMED (+nuance) | `LIVE_DIR.glob("test_*.py")` at `:36`, no floor (77 modules today). `live_wan21` 3/3 and `release_wan22` 3/3 carry in-file signals; `tests/stores/live/` 2/2 modules have NO in-file signal — gated only by `tests/stores/live/conftest.py:21-22` fixture skip, so a naive recursive extension would false-positive unless it also scans the directory's conftest. |
| strength `tests/cli/test_cmd_generate_loras.py:40-51` | `try/except Exception: pass` then `call_count == 0` — passes for the wrong reason on any crash | PARTIAL (shape ×6, inert today) | Six identical swallow sites: `:46-49, :58-61, :109-112, :125-128, :143-146, :162-165`. Wrapping `_cmd_generate` under pytest (scratch plugin logging every raise): `9 passed`, zero raises — `_cmd_generate` returns normally after printing `error: unknown adapter — no image engine registered: <MagicMock …>` (visible in `-rA` captured stderr). So the assertions are real today; the wrappers are dead code that would hide a future crash. Removal is behaviour-preserving (D). |
| strength `tests/integration/test_no_unknown_slug_for_example_configs.py:80-85,100-103` | `except Exception: pytest.skip` converts the guarded failure into a skip | CONFIRMED (latent) | Arms at `:80-85` and `:100-103`. `pytest -rs` → `60 passed, 2 skipped`, both skips are the `:98` "fake engine" skip; the except→skip arms never fire at HEAD. |
| redundant `_fake_session` ×4, `_probe_profile` ×4 | copies in orchestrator tests | CONFIRMED (more copies) | AST body-hash (docstrings stripped): `_fake_session` identical ×4 (`test_orchestrator_{fullres:55,interpolate:98,skip_clip_stage:59,upscale_chunk:59}.py`) + 2 variants (`publish_media:48`, `text:67`); `_probe_profile` identical ×4 (`cancelled:73, heartbeat:72, interrupt:78, session_claim:65`) + identical ×2 of a `max_frames` shape (`test_orchestrator.py:90`, `test_orchestrator_compute.py:86`). |
| redundant c29/c33 heartbeat harness clones | | CONFIRMED | `diff` of `test_orchestrator_c29_start_heartbeat.py:37-150` vs `…c33_start_heartbeat_after_provision.py:34-134` → 15 differing lines, all comments/docstrings; `_FakeLoop/_fake_engine/_fake_provider/_fake_cfg/_fake_creds/_fake_key` code-identical. |
| ND #20 (premise) `tests/live/conftest.py` loads `.env` into every local run | | CONFIRMED | `tests/live/conftest.py:35-42` module-level `if load_env_file is not None: … if env_file.exists(): load_env_file(env_file)`; `.env` is present in this workspace; shell creds are unset, so the load is the only way creds reach `os.environ` during `pixi run test`. |
| ND #20 (effect on my area) does any test behave differently with real creds present? | | WRONG for my area (no effect) | Simulated the load by exporting every `.env.example` variable (30 names) with synthetic values and running my whole area: `1 failed, 3807 passed, 11 skipped`; the one failure is order pollution, not env (B1 — re-run alone under each cred var: passes; re-run `tests/core` + that test with NO env: fails identically). Production reads of credential names are only `upscalers/{flashvsr,spandrel}/_fetch_weights.py` (`HF_TOKEN`); my area's only direct cred-env reads are `tests/core/test_dotenv_loader.py`, which `monkeypatch.delenv`s first (7 sites). |
| ND #20 pyproject `testpaths` / `xfail_strict` / `--strict-markers` absent | | CONFIRMED absent; adding each is a no-op at HEAD | `pytest --collect-only -o addopts=""`: root = 7034; `tests` = 7034; `--strict-markers` = 7034, zero errors (`fd -g 'test_*.py' --exclude tests` → none). Markers in my area: `parametrize` 137, `usefixtures` 9, `skipif` 5 (ffmpeg/platform), `xfail` 0 → `xfail_strict` cannot change any outcome in my area (repo-wide xfails live in `tests/live`, `tests/providers/runpod/test_heartbeat.py` (already `strict=True`), `tests/stores/live` — F). |
| keep: mega test files | split opportunistically | CONFIRMED (stands) | `tests/core/test_orchestrator.py` 2830, `tests/core/test_config.py` 2200; also `tests/test_cli.py` 1594, `tests/pipeline/test_generate_clip.py` 1510, `tests/core/test_provisional_launch_row.py` 1468. No new evidence against the keep. |
| keep: `tests/_fixtures/test_fake_auth.py` collected inside the helper package | harmless | CONFIRMED | 6 assertion-bearing tests over `FakeAuthStrategy` (`:9-52`); collected via the default `test_*.py` pattern; no side effects. |
| keep: walrus in `cli/_commands.py` for the AST invariant in `tests/test_ephemeral_index_write_gated.py` | | CONFIRMED (reason valid; guard floorless) | Running the test's own `_is_add_call_on_ephemeral_index` over `_all_py_files()` matches exactly the 2 real sites (`_commands.py` ~`:3009`, ~`:3292`, shape `EphemeralIndex(store=…).add(`) via the `EphemeralIndex(...)` branch (`:55-60`). But `:87-119` never asserts it matched anything — rename the receiver to `idx` and the guard passes on zero sites. |
| keep: `tests/test_live_gating_lockdown.py:18-20` text scan "coarse but cheap" | | CONFIRMED (stands) | See the lockdown row above; the keep's reason holds, the audit's recursive gap is real. |
| ND #21 `tools/local_hooks/` dead + `tests/hooks/` 522 LOC run by `pixi run test` | | PARTIAL | `tests/hooks/test_tasks_live_query.py:2` targets `tools/local_hooks/lib/tasks_live_query.py` (268 LOC, would go with the purge); `tests/hooks/test_block_secret_reads.py` (407 LOC) targets the committed `.claude/hooks/block_secret_reads.py` — alive, must stay. `tests/hooks/differential_test.sh` is a manual script cited only by `docs/hook-differential-test-results-2026-06-18.md:4,94`. |

## B. New bugs (missed by the prior audit)

**B1 — P3 (test-suite correctness; order-dependent false green).**
`tests/core/test_image_interfaces.py:118` registers `"_stub_T1_test"` into the process-global
`registry._image_engines` with no cleanup (no `monkeypatch`, no `try/finally`; the sibling
`tests/core/test_text_interfaces.py:43-45` does it right with `monkeypatch.setattr(registry, "_text_engines", {})`).
`tests/cli/test_cmd_image.py:106-122 test_image_table_covers_every_registered_image_engine` asserts every
registered image engine has an `IMAGE_EPHEMERAL_CAPABILITIES` entry.
Failure scenario (reproduced, no env involved):
`pixi run python -m pytest tests/core "tests/cli/test_cmd_image.py::test_image_table_covers_every_registered_image_engine"`
→ `1 failed … AssertionError: image engines with no IMAGE_EPHEMERAL_CAPABILITIES entry: ['_stub_T1_test']`.
`pixi run test` is green only because pytest's default directory order runs `tests/cli` before
`tests/core`; any `-p randomly`, `--lf`, `-k` selection, or a future directory rename flips it.
Introduced in `fc37edb8 fix(core): T1 review fixups — docstrings + registry tests + bug guards`.
Fix shape (not applied): `monkeypatch.setattr(registry, "_image_engines", dict(registry._image_engines))`
at the top of `test_register_and_get_image_engine` (or `registry._image_engines.pop("_stub_T1_test", None)` in `finally`).

No other product bugs found. Specifically checked: every `except Exception` in my area (only the six
inert loras swallows and the two latent except→skip arms); every `assert … or …`/`assert True` shape
(one hit, C2); assert-less tests via AST (31; all but the four in C3 either raise `AssertionError`
explicitly or are deliberate no-exception/idempotency contracts); duplicate/shadowed test names (0 of
347 files); mocks whose shape production never produces — the `MagicMock()` cfg in
`test_cmd_generate_loras.py` is the one case and it only exercises the pre-engine CLI path.

## C. New hygiene findings

C1 · `tests/cli/test_cmd_generate_loras.py:46-49,58-61,109-112,125-128,143-146,162-165` · error-handling (dead
swallow around the call under test) · behaviour-preserving: **yes** (wrapper-plugin run proves `_cmd_generate`
raises in none of the 9 tests) · risk low · **FIX NOW** · Removing the `try/except Exception: pass` turns a
future crash into a failure instead of a vacuous `call_count == 0` pass.

C2 · `tests/integration/test_output_filename_redacted_in_logs.py:56` · dead (vacuous assertion
`assert … or True`) · yes (the expression can never fail) · low · **FIX NOW** · Dead since `101dc3d9`; the real
assertions follow at `:63-72`.

C3 · `tests/test_examples.py:244-247,291-294` (`test_provider_swap_runpod_to_local`, `test_engine_swap_comfyui_to_diffusers`:
two `load_config` calls, no assertion); `tests/core/test_orchestrator_no_reuse.py:238-248` (`pass  # no crash`,
name claims "skips destroy" but nothing checks destroy); `tests/core/test_session_claim.py:217-233`
(`test_clock_injection_used_for_start_time`, no assertion — fails only via `FirstTickTimeout` if the clock were
ignored) · test-strength (no-exception-only) · n/a · low · **LEAVE** (strengthen when next touched; the first two
are fully covered by the per-config load tests at `test_examples.py:53,179`).

C4 · nine floorless discovery sweeps (table row above) · test-strength ("guard the guard") · adding a floor is
test-only · low per site · **NEEDS DISCUSSION** (floor values) — today's counts to pin against:
`src/kinoforge/**/*.py` = 206, `core/**/*.py` = 83, `engines/diffusers/servers/*.py` = 9,
`examples/configs/**/*.yaml` = 73 (top-level 53), `tests/live/test_*.py` = 77,
`EphemeralIndex…add(` sites = 2. Suggested floors: half of today's count, rounded down.

C5 · `tests/test_no_precedence_branches_outside_resolver.py:48` `Path("src/kinoforge")` and
`tests/core/test_compute_surface.py:305` `Path("examples/configs")` · structural (CWD-relative root; vacuous
from any other cwd — proven) · yes when run from the repo root (identical file set) · low · **FIX NOW** (D) ·
Only failure-message text changes (absolute vs relative path in `offenders`), never product output.

C6 · `tests/integration/conftest.py` (whole file, 19 lines) · dead (subsumed autouse) · yes · low · **FIX NOW**.

C7 · seven dead helpers + `SequencedBackend` (A rows) · dead · yes · low · **FIX NOW** (D). Orphan-import
check: `time` (3 uses remain), `FakeBackend` (18), `Any` (5) stay used; ruff `--fix` at pre-commit will drop any
that do not.

C8 · `tests/core/test_image_interfaces.py:118` · error-handling / isolation · n/a (it is B1) · low · **FIX NOW
as a bug fix in a separate commit** — the controller said bugs are recorded, not fixed, in this pass; it is a
one-line `monkeypatch` and I recommend an exception, but it stays out of D.

C9 · `tests/conftest.py:570-603` faulthandler timer (P3 row) · documentation + error-handling · **no** (changes
stderr output of every run >15 s) · low-med · **NEEDS DISCUSSION** (E1).

C10 · `tests/integration/test_no_unknown_slug_for_example_configs.py:80-85,100-103` except→skip · error-handling ·
test-only but changes which failures surface · low · **NEEDS DISCUSSION** (E2).

C11 · `_fake_session` ×4 / `_probe_profile` ×4(+2) / c29-c33 harness · redundant (true DRY: one fixture shape,
six files) · yes · med (10 files, fixture import ordering) · **NEEDS DISCUSSION** (E3).

C12 · `tests/test_live_gating_lockdown.py:36` non-recursive + no floor · test-strength · adding `tests/stores/live`
needs a conftest-aware signal · low · **NEEDS DISCUSSION** (E4).

C13 · `tests/_fixtures/test_fake_auth.py` keep, mega-file keep, walrus keep — all stand (A).

## D. FIX NOW edit list

### Code items

**D1 — `tests/cli/test_cmd_generate_loras.py` (embedded: no).** Six edits, each replacing a 4-line block with
the bare call. Fence: n/a (not a deletion of a symbol). `rg -n 'except Exception' tests/cli/test_cmd_generate_loras.py`
→ 6 hits, all these. Exercised by: `tests/cli/test_cmd_generate_loras.py` (9 tests; verified 9 passed with the
wrappers neutralised).
- `:46-49` old:
  ```
              try:
                  _cmd_generate(args, _ctx_with_cfg())
              except Exception:
                  pass
  ```
  new: `            _cmd_generate(args, _ctx_with_cfg())` (12-space indent; inside two `with` blocks)
- `:58-61` old (8-space indent):
  ```
          try:
              _cmd_generate(args, _ctx_with_cfg())
          except Exception:
              pass
  ```
  new: `        _cmd_generate(args, _ctx_with_cfg())`
- `:109-112` same text as `:58-61` → same replacement.
- `:125-128` same text as `:58-61` → same replacement.
- `:143-146` old (8-space indent):
  ```
          try:
              _cmd_generate(args, ctx)
          except Exception:
              pass
  ```
  new: `        _cmd_generate(args, ctx)`
- `:162-165` same text as `:143-146` → same replacement.
  (Because `:58-61`/`:109-112`/`:125-128` are byte-identical, apply with `replace_all` on that 4-line block,
  then `replace_all` on the `ctx` variant; the `:46-49` block has a distinct 12-space indent.)

**D2 — `tests/integration/test_output_filename_redacted_in_logs.py:56` (embedded: no).** Delete the line
`        assert "CANARY-SUBSTRING-PROMPT"[:20].lower() in path_str.lower() or True`.
Fence: `git log -S'or True' -- <file>` → `101dc3d9 chore: state-management refactor + write-site invariants`
(introduced with the file; no reason given). `rg -n 'CANARY-SUBSTRING-PROMPT"\[:20\]' tests docs` → 1 hit (this
line). Exercised by: the same file (1 test).

**D3 — `tests/integration/conftest.py` (embedded: no).** Delete the whole file (19 lines). Fence:
`git log -S'_clear_redaction_registry' -- tests/integration/conftest.py tests/conftest.py` → `82c8f59f`
(integration copy) / `101dc3d9` (root). `rg -n '_clear_redaction_registry\b' tests` → only the def in this file.
Exercised by: `tests/integration/` (24 modules; the root autouse at `tests/conftest.py:117` keeps clearing).

**D4 — dead helpers (embedded: no).** Each deletion = the quoted line range, plus the blank lines ruff-format
expects. `rg -n '<name>' tests src tools` = 1 hit (the def) for every name below.
- `tests/core/test_orchestrator.py:183-226` — delete the `# ---…` header (3 lines, "Sequenced backend — returns
  different profiles on successive calls (AC5)"), `class SequencedBackend(FakeBackend):` (`:188-202`) and
  `class SequencedEngine(FakeEngine):` (`:204-226`). Fence `679a7cb3 feat: add orchestrator deploy/generate flows
  with fail-hard discovery ordering` (added unused). Exercised by: `tests/core/test_orchestrator.py`.
- `tests/core/test_session_claim.py:33-36` (`def _record_and_set_tick`) and `:39-50` (`class _CountingSleep`).
  Fence `6a42d0ec feat(b7): session_claim helper + FirstTickTimeout`. `import time` stays (3 other uses).
  Exercised by: `tests/core/test_session_claim.py`.
- `tests/core/test_grid_executor_swap_group.py:363-365` (`def _swap_argv_assertions` + its 2-line docstring; EOF).
  Fence `c00b28e9 feat(grid): _run_swap_group executor + run_grid dispatch`; the docstring's "module marker"
  rationale is false (pytest collects by filename). Exercised by: that file.
- `tests/core/test_orchestrator_diagnostic_env.py:167-176` (`def _make_arg_capture`). Fence `55e35f0f feat(c28)
  … diagnostic_env`. Exercised by: that file.
- `tests/core/test_provisioner.py:108-115` (`def _spy_downloader`). Fence `bdfc73a1 feat: add core provisioner`.
  Exercised by: `tests/core/test_provisioner.py`.
- `tests/core/test_provision_state_alias_keyed.py:101-108` (`def _make_vault_no_alias`). Fence `dee647ba
  fix(provision-marker): alias-key under STRICT + vault`. Exercised by: that file.

**D5 — CWD-relative roots (embedded: no).**
- `tests/test_no_precedence_branches_outside_resolver.py:48` old `    src_root = Path("src/kinoforge")` → new
  `    src_root = Path(__file__).resolve().parents[1] / "src" / "kinoforge"`. `:49` `resolver` and `_ALLOWED`
  stay relative: `rel = py_file.relative_to(src_root.parent.parent)` (`:53`) still yields `src/kinoforge/…`.
  `rg -n 'Path\("src/kinoforge"\)' tests` → 1 hit. Exercised by: that file (1 test; passes from root before and
  after; fails loudly instead of vacuously from elsewhere only if a floor is also added — see E).
- `tests/core/test_compute_surface.py:305` old `    for path in sorted(Path("examples/configs").rglob("*.y*ml")):`
  → new `    for path in sorted((Path(__file__).resolve().parents[2] / "examples" / "configs").rglob("*.y*ml")):`.
  `rg -n 'Path\("examples/configs"\)\.rglob' tests` → 1 hit. Exercised by: `tests/core/test_compute_surface.py`.

### Docs-only items (docstrings in test files; embedded: no)

**D6 — `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py:16-19`.** old:
```
Pre-fix: hangs ≥30s, faulthandler dumps the stack inside
``hold_until_first_tick``'s post-yield poll loop, marked XFAIL.
Post-fix (Task 2): exits in <5s; remove ``@pytest.mark.xfail`` in the
same commit.
```
new:
```
Pre-fix (``a392deaf``): hung ≥30s, faulthandler dumped the stack inside
``hold_until_first_tick``'s post-yield poll loop. Fixed in ``4ceb4fdf``;
the test now asserts a clean exit in <5s.
```
`rg -n 'marked XFAIL' tests docs` → 1 hit.

**D7 — `tests/core/test_heartbeat_endpoints.py:4-5`.** old:
```
- HeartbeatEndpoint Protocol is runtime_checkable and structurally satisfied
  by the fake doubles in tests/providers/conftest.py.
```
new:
```
- HeartbeatEndpoint Protocol is runtime_checkable and structurally satisfied
  by the fake doubles in tests/conftest.py.
```
`rg -n 'tests/providers/conftest.py' tests/core` → 1 hit (this). (The twin at
`tests/providers/test_heartbeat_parity.py:31-32` is tests-2's.)

## E. Needs discussion

E1 · **Faulthandler timer** (`tests/conftest.py:570-603`). Options: (a) cancel at `pytest_collection_finish`
(covers the collection-phase deadlock the comment targets, never fires on a healthy run), (b) switch to pytest's
built-in `faulthandler_timeout` ini (per-test, auto-cancelled), (c) keep and fix the comment. Changes stderr
output → not FIX NOW. Note `tests/integration/test_ephemeral_warm_attach_exits_cleanly.py:180,193` arms/cancels
its own 25 s timer, which also cancels the session timer early when that test runs before t=15 s.

E2 · **except→skip** in `test_no_unknown_slug_for_example_configs.py`: narrow to `ExtrasNotInstalled` (the
only legitimate offline reason) so an unregistered engine name fails. Decide whether `UnknownAdapter` for a
shipped config should be a failure (I think yes).

E3 · **Fixture consolidation**: move the byte-identical `_fake_session` (×4) and `_probe_profile` (×4, ×2) into
`tests/core/conftest.py` / `tests/core/_fakes.py`; merge the c29/c33 harness into one helper module. Medium risk
(fixture resolution order across 10 files); do it in one commit with `tests/core` green.

E4 · **Live-gating lockdown**: make `:36` recursive over `tests/**/live*/` and `tests/smoke/release_*/`, count a
directory-level `conftest.py` gate (`tests/stores/live/conftest.py:21-22`) as a signal, and add a floor
(`assert len(modules) >= 40`).

E5 · **Floors for the nine floorless sweeps** (C4) — choose values; I recommend half of today's counts.
Highest-value first: `tests/test_ephemeral_index_write_gated.py` (assert matched sites ≥ 1 — protects the walrus
keep), `tests/test_no_precedence_branches_outside_resolver.py` and `tests/core/test_compute_surface.py:305`
(the two proven-vacuous ones), `tests/test_live_gating_lockdown.py`.

E6 · **B1 fix** — one-line `monkeypatch` in `tests/core/test_image_interfaces.py`; recommend applying now
despite the "record, don't fix" rule because it is test-only and the suite's green depends on directory order.

E7 · **ND #21 purge** — if `tools/local_hooks/` goes, `tests/hooks/test_tasks_live_query.py` goes with it;
`tests/hooks/test_block_secret_reads.py` must stay (targets `.claude/hooks/`, alive). `differential_test.sh`
is a manual, doc-cited script — keep or move under `docs/` with its results page.

## F. Cross-area notes

- `tests/live/conftest.py:35-42` module-level `.env` load confirmed; its effect on my area is nil (A), so ND #20's
  risk is confined to `tests/live`/`tests/providers`/`tests/stores` (tests-2 to check the same way: export the
  `.env.example` names with synthetic values and diff the pass set).
- Unreferenced helpers outside my area, each a single `rg` hit: `tests/providers/test_runpod.py:190 HttpGetSpy`,
  `tests/diagnostics/test_c33_orchestrator.py:28 _FakeS3`, `tests/providers/test_field_consumption_parity.py:733-734
  _PROBE_{BUILD,RUNTIME}_SCRIPT`.
- `tests/providers/test_heartbeat_parity.py:31-32` carries the same stale "tests/providers/conftest.py" pointer as D7.
- `xfail_strict=true` repo-wide would touch only `tests/live` (12 contract xfails), `tests/providers/runpod/test_heartbeat.py`
  (already strict) and `tests/stores/live` (documented `strict=False`) — a tests-2 decision; `--strict-markers`
  and `testpaths=["tests"]` are provably no-ops at HEAD (7034/7034, zero errors).
- `tests/_post_session_dump.txt` (169 lines) exists in the working tree; it is gitignored (`.gitignore:103`) and
  is the residue of a past leaky run — not a repo item, but it means a non-daemon thread leak occurred locally once.
- `tests/core/test_compute_surface.py:318-335` and much of `tests/test_examples.py` load configs by CWD-relative
  `Path("examples/configs/…")`; they fail loudly from another cwd (not vacuous), so no action.
