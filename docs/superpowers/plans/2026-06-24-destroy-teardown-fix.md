# Destroy-on-Teardown Fix — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-06-24-destroy-teardown-fix-design.md`
**TDD discipline:** every task lands a RED test, then the GREEN
implementation, then a fresh `pixi run test` confirming the test
flips. Commit after each RED/GREEN pair (atomic). No bundled tasks.

## Task list

- [ ] **T1** — `RunPodGraphQLError` + `_unwrap_graphql_response` helper
  - RED: `test_unwrap_graphql_response_raises_on_errors_field`,
    `test_unwrap_graphql_response_returns_data_on_success`.
  - GREEN: add helper + exception class in
    `src/kinoforge/providers/runpod/__init__.py`. `RunPodGraphQLError`
    extends `TransportError`. Helper raises with `context` string
    + `errors` list embedded in `args` so unit tests can assert
    on both.
  - Commit: `feat(p2): RunPodGraphQLError + helper for typed GraphQL failures`

- [ ] **T2** — `destroy_instance` uses the helper for terminate + every poll
  - RED: T1a (terminate errors) + T1b (poll errors) from spec.
  - GREEN: wrap both `_http_post` calls in
    `_unwrap_graphql_response`. Confirm T1c (existing happy path)
    stays green.
  - Commit: `fix(p2): destroy_instance refuses to claim success on GraphQL errors`

- [ ] **T3** — `SweepResult` dataclass + `destroy_all_active_pods` return contract
  - RED: T1d from spec.
  - GREEN: introduce `SweepResult` in
    `tests/_smoke_harness/runpod_lifecycle.py`; change function
    signature to return `SweepResult`. Update the two unit tests
    in `tests/_smoke_harness/test_runpod_lifecycle.py` if their
    assertions assume `list[str]`; keep the IDs-of-destroyed channel
    intact.
  - Commit: `feat(p2): SweepResult — destroy_all_active_pods surfaces failures`

- [ ] **T4** — All four smoke fixtures consume `SweepResult.failures`
  - RED: T1g from spec — a no-cost test that the fixture's
    teardown helper raises when post-condition probe still sees
    pod_id.
  - GREEN: extract a `_teardown_pod_or_raise(pod_id)` helper into
    `tests/_smoke_harness/runpod_lifecycle.py` that bundles
    `destroy_all_active_pods` + subprocess fallback + post-condition
    probe. Wire it into:
      - `tests/smoke/live_wan21/test_branch_routing.py`
      - `tests/smoke/live_wan21/test_lora_swap_matrix.py`
      - `tests/smoke/release_wan22/test_dual_transformer_routing.py`
      - `tests/smoke/release_wan22/test_lora_swap_matrix.py`
  - Commit: `fix(p2): smoke teardown raises on residual pod (post-condition)`

- [ ] **T5** — `_cmd_destroy` falls back to provider lookup for orphans
  - RED: T1e + T1f from spec.
  - GREEN: in `src/kinoforge/cli/_commands.py:_cmd_destroy`,
    when ledger entry is None, try every registered provider's
    `get_instance(args.id)` (or just `runpod` for the smoke
    failure mode — narrower scope, easier to defend). On success
    call `destroy_confirmed` + skip the `ledger.forget` step.
    Print `"destroyed orphan: {id} (no ledger entry)"`.
  - Commit: `fix(p2): kinoforge destroy reaps orphans not in local ledger`

- [ ] **T6** — Pytest log_cli for smoke tiers (defensive)
  - GREEN-only (no behavioural test; this is a defensive config
    knob). Add `log_cli = true` + `log_cli_level = "WARNING"` to
    `pyproject.toml` `[tool.pytest.ini_options]` OR a
    `tests/smoke/conftest.py`. Verify a smoke-style unit test
    surfaces the WARNING with the new config; we already have
    `test_destroy_all_active_pods` shape — extend one to assert
    via `caplog` that the warning fires when destroy_instance
    raises.
  - Commit: `cfg(p2): pytest log_cli WARNING for smoke tiers`

- [ ] **T7** — Update PROGRESS.md
  - Move the destroy-on-teardown bug from "highest priority" to
    "fixed 2026-06-24 (commits T1-T6, Tier-1 GREEN)". Link new
    spec + plan. Single-next-action becomes whichever deferred
    workstream is next (Layer-5 cost capture, C26 util-aware
    stall, or the parked thread-leak brainstorm — operator's
    choice; default to Layer-5).
  - Commit: `docs(progress): destroy-on-teardown bug FIXED (Tier-1 GREEN)`

- [ ] **T8 (deferred to next live smoke)** — Tier-2 live confirmation
  - Spin up the cheapest RunPod GPU, run `destroy_all_active_pods`,
    confirm `provider.get_instance(pod_id)` raises `KeyError`.
    Fold into the next planned Tier-3 or Tier-4 fire — do not
    spend on this independently.

## Acceptance criteria

  - Every `_http_post` call in `destroy_instance` parses GraphQL
    errors (no silent success).
  - `destroy_all_active_pods` returns `SweepResult` with explicit
    `failures` channel.
  - All four smoke fixtures' teardown path raises on residual pod.
  - `kinoforge destroy --id <orphan>` succeeds when the pod exists
    in RunPod but not in the ledger.
  - `pytest -k destroy` green; engine+core+provider+smoke-harness
    suites green at every commit (no regressions).
  - Cumulative live spend on Tier-1 GREEN = $0.00.

## Risks & gotchas

  - Changing `destroy_all_active_pods` signature breaks any
    out-of-tree callers. Search confirms only the smoke fixtures
    consume it — safe.
  - `_cmd_destroy` provider-lookup fallback must NOT raise on
    `KeyError` from `provider.get_instance` — that's the
    "truly unknown" path; preserve exit 1.
  - Adding `log_cli` may noisily change pytest output for all
    tests, not just smoke. Scope it to a smoke-only conftest if
    the global default produces churn in unrelated suites.
