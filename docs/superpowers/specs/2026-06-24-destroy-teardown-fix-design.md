# Destroy-on-Teardown Fix — Design

**Status:** DRAFT 2026-06-24
**Author:** Claude (Opus 4.7 1M, caveman-mode `full`)
**Driver:** PROGRESS.md top-priority bug — Tier-4 teardown 2026-06-23 23:07 PT
on pod `2k0gonzmeqw7xj` left pod alive at $1.49/hr; both
`destroy_all_active_pods()` and `subprocess.run(["pixi","run","kinoforge","destroy",...])`
fallback returned without surfacing failure. Same intermittent failure
also surfaced on the same day's Tier-3 fires. Money-leak risk on every
Tier-3 / Tier-4 fire until root-caused.

## Why this is hard to debug retroactively

No structured artifact survives a "silent destroy". The smoke harness
prints to the pytest captured-output buffer, but the *failure* of the
sweep is hidden behind `_log.warning(...)` (no console fallback) and
`subprocess.run(..., check=False, capture_output=True)` (output never
read). The only forensic trace was a post-fact `kinoforge list` per
the CLAUDE.md teardown-verification rule. So fixing the silent layer
matters more than guessing the underlying provider error.

## Multi-layer silent-failure chain

Every layer below has a silent-failure path that can swallow a
real "pod still alive" outcome:

### Layer A — `_make_default_http_seams.authed_post` does not parse GraphQL errors

`src/kinoforge/providers/runpod/__init__.py:167-176` returns the raw
JSON dict from the RunPod GraphQL endpoint. A response like
`{"errors": [...], "data": null}` is a SUCCESS from urllib's
perspective (HTTP 200) but a FAILURE at the GraphQL semantic layer.
The provider never inspects the `errors` field. Every higher-layer
call inherits this ambiguity.

### Layer B — `destroy_instance` can't distinguish "pod gone" from "errors response"

`src/kinoforge/providers/runpod/__init__.py:439-471` polls
`resp.get("data", {}).get("pod")`. This returns `None` in two
materially different cases:

  1. Pod was confirmed terminated by RunPod (intended success).
  2. The GraphQL response had `errors` set and `data: null` —
     `resp.get("data", {})` returns `{}`, `.get("pod")` returns
     `None`, and we incorrectly conclude the pod is gone.

This is the *first-poll-after-errors* failure mode that produces a
false-positive "destroyed" verdict — the smoke harness then treats
the pod as reaped and never runs the fallback subprocess.

Compounding: the terminate mutation response itself
(`_terminate_pod_mutation`) is not checked at all. A failed terminate
that returns `{"errors": [...]}` is invisible until the poll loop
makes the same incorrect inference.

### Layer C — `destroy_all_active_pods` warns silently, returns truncated list

`tests/_smoke_harness/runpod_lifecycle.py:61-79` wraps
`provider.destroy_instance(inst.id)` in an inner `try/except
Exception` that logs at `_log.warning(...)` and continues. Pytest
default log capture suppresses anything below ERROR (or anything at
all, depending on `log_cli_level`). The caller receives a list of
"successfully destroyed" IDs with no `errors_by_id` channel, so
"pod X failed to destroy" is silently dropped.

### Layer D — smoke harness subprocess fallback uses `check=False` with no inspection

`tests/smoke/release_wan22/test_dual_transformer_routing.py:217-224`:

```python
subprocess.run(  # noqa: S603
    ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
    cwd=str(REPO),
    capture_output=True,
    text=True,
    timeout=120,
    check=False,
)
```

No `returncode` inspection. No `stderr` log. No `assert`. Every
failure mode of `kinoforge destroy` (and there are several — see
Layer E) is silently swallowed. The fact that the call ran at all
proves `destroy_all_active_pods()` did not include `pod_id` in its
result list, so Layer C *did* warn-only on the underlying
exception. But Layer D then ate the error.

### Layer E — `_cmd_destroy` refuses without a ledger entry

`src/kinoforge/cli/_commands.py:1583-1611` looks up the pod in the
local ledger via `entries = ledger.entries(); next((e for e in
entries if e.get("id") == args.id), None)`. If the pod is not in
the ledger (e.g. orchestrator already removed it on a prior
destroy attempt, OR the pod was created by a different process /
container that wrote to a different state dir, OR the ledger was
corrupted), the command prints `"instance ... not found in ledger"`
to stderr and returns exit 1 *without contacting the provider*.

This is by design — `kinoforge destroy` is the "destroy a pod we
remember" surface, and `kinoforge reap` / `--force-forget` is the
"forget about a pod we don't recognise" surface. But the smoke
harness's subprocess fallback assumes plain `destroy --id` is
sufficient. It isn't, and the gap is invisible due to Layer D.

### Cross-cutting: orchestrator's own destroy path

`src/kinoforge/core/orchestrator.py` and
`src/kinoforge/core/lifecycle.py:715-765` (`destroy_confirmed`) wrap
the provider call with retries + a final ledger forget. If
`destroy_confirmed` raises `TeardownError` after exhausting retries,
`_cmd_destroy` does NOT catch it (line 1609 only catches
`UnknownAdapter, KeyError`) — the CLI crashes with a traceback, exit
1, and the ledger entry is left intact. This is the "manual
re-destroy works later" path, because the operator's later attempt
hits the same code path but with sufficient time elapsed for any
transient RunPod-side issue to clear.

## Design — fix every silent layer, surface the real error

The principle: **a failed destroy MUST raise loudly at every layer
above the network call.** Money-leak bugs need defense-in-depth.
Each layer must (a) refuse to falsely report success and (b) surface
enough information that the next layer up can take corrective action.

### A. GraphQL errors parsing — RunPod provider layer

Add a small helper `_unwrap_graphql_response(resp, *, context)` that:

  - Returns `resp["data"]` if `resp.get("errors")` is empty/missing.
  - Raises `RunPodGraphQLError(context, errors_list)` otherwise.

Apply it in `destroy_instance` for BOTH the terminate mutation and
each poll. Apply it in `list_instances` and `_get_pod_query` paths
too (defense in depth — the bug class repeats elsewhere).

`RunPodGraphQLError` should extend `TransportError` so the
`HeartbeatLoop`'s existing broad catch keeps working without code
changes.

`destroy_instance`'s poll loop becomes:

  - terminate: unwrap response → if errors, raise immediately.
  - poll: unwrap response → if errors, raise (do NOT treat as "pod
    gone").
  - if `data.pod` is `None`: confirmed gone, return.
  - if `data.pod` exists: continue polling.
  - after `_MAX_DESTROY_POLLS` polls with pod still present: raise
    `TeardownError` (unchanged).

### B. `destroy_all_active_pods` — surface per-pod failures

Change the return contract from `list[str]` to a small dataclass:

```python
@dataclass(frozen=True)
class SweepResult:
    destroyed: list[str]           # IDs that left cleanly
    failures: dict[str, BaseException]  # ID -> exception
```

Callers can keep behaving as before (iterate `result.destroyed`),
but smoke harness fixtures MUST inspect `result.failures` and react:

  - log to console (not just `_log.warning`)
  - either raise (fail the test loudly) or attempt the fallback
    subprocess

The signature shift is small and constrained to test code; the
existing two callers (test_branch_routing, test_dual_transformer_routing
+ the two Wan-2.2 LoRA-swap matrix tests) all need updating in the
same change.

### C. Smoke harness subprocess fallback — inspect everything

Replace the silent `subprocess.run(...)` with a wrapper that:

  - logs `stdout` + `stderr` via `print(...)` so pytest captures them
  - asserts `returncode == 0` AND raises if the pod is still in
    `provider.list_instances()` after the call (post-condition
    check)

A *post-condition* probe is the most important addition: the
subprocess can claim success (exit 0) and still leave the pod
alive, e.g. if `destroy_confirmed` polls returned None due to
Layer A ambiguity. The probe closes that loop by asking the
provider one more time.

### D. `_cmd_destroy` — accept pod IDs known to the provider

Two-step lookup:

  1. Try ledger entry — current behaviour, used for `provider_name`
     resolution.
  2. If ledger lookup fails, try `provider = runpod_factory()` (or
     iterate registered providers if we can't infer) and call
     `provider.get_instance(args.id)`. If the pod exists, destroy
     it and report "destroyed (ledger had no entry — pod was
     orphaned)". If neither layer knows the pod, exit 1 as today.

Defer: a full `--force` flag that lets the operator name any
provider+id pair. The two-step lookup covers the smoke-harness
failure mode and keeps the CLI surface narrow.

Optionally tighten: catch `TeardownError` explicitly in
`_cmd_destroy` and print the underlying GraphQL errors (instead of
crashing with an uncaught traceback). The traceback is itself
diagnostic, but a one-line summary on stderr is more operator-friendly.

### E. Pytest log capture for the smoke harness

Add `log_cli = true` and `log_cli_level = WARNING` to the smoke-tier
pytest config (either in `pyproject.toml` `[tool.pytest.ini_options]`
or in a smoke-tier `conftest.py`). Cheap defensive layer — even if
Layers B–D regress in the future, a WARNING log will reach the
operator's terminal.

## Test surface

### Tier-1 (unit, free, every PR via `pixi run test`)

Mock `_http_post` to simulate the failure modes:

  - **T1a `test_destroy_instance_raises_on_terminate_graphql_errors`**:
    inject a response `{"errors": [{"message": "Unauthorized"}], "data": null}`
    for the terminate call; assert `RunPodGraphQLError` raised, NOT
    silent success.
  - **T1b `test_destroy_instance_raises_on_poll_graphql_errors`**:
    terminate OK, first poll returns `{"errors": [...], "data": null}`;
    assert raise (do NOT conclude pod gone).
  - **T1c `test_destroy_instance_succeeds_on_data_pod_none`**:
    terminate OK, first poll `{"data": {"pod": null}}`; assert
    returns cleanly (pre-existing happy path stays green).
  - **T1d `test_destroy_all_active_pods_reports_failures`**:
    list_instances returns [A, B], destroy_instance(A) raises,
    destroy_instance(B) succeeds; assert
    `result.destroyed == ["B"]` and `result.failures == {"A": exc}`.
  - **T1e `test_cmd_destroy_falls_back_to_provider_lookup_for_orphan`**:
    ledger empty, `provider.get_instance(args.id)` returns a real
    Instance; assert exit 0 + destroy_confirmed was called.
  - **T1f `test_cmd_destroy_exits_1_when_pod_truly_unknown`**:
    ledger empty AND `provider.get_instance(args.id)` raises
    KeyError; assert exit 1 (preserve the "we don't know what this
    is" contract).
  - **T1g `test_smoke_subprocess_fallback_post_condition`**: under
    a mocked provider where the destroy subprocess "succeeds" but
    list_instances still includes pod_id, assert the fallback
    helper raises so the test fails LOUDLY.

### Tier-1.5 (no-cost integration via pytest-httpserver or recorded VCR)

Skip for now. The Tier-1 unit tests pin every transition with the
same `_http_post` seam the provider already uses; adding a recorded
HTTP layer would duplicate coverage.

### Tier-2 (live, ~$0.05–$0.15)

Spin up the cheapest available RunPod GPU (RTX A4000 / A5000 if
available, A6000 if not) with the smallest possible image, run
`destroy_all_active_pods` against it, then independently confirm via
`provider.get_instance(pod_id)` raises `KeyError` (or returns a
Terminated status). Optional sanity follow-up: also exercise the
new `_cmd_destroy` orphan path by deliberately calling
`ledger.forget(pod_id)` between create and destroy.

Defer Tier-2 until Tier-1 is green AND the next Tier-3 / Tier-4
fire is scheduled (the live confirmation is cheap to fold into the
next planned smoke). Tier-1 is sufficient to claim CODE-COMPLETE.

## Out of scope

  - C26 util-aware stall classify (separately tracked).
  - Layer-3 watchdog `tools/smoke_leak_sweep.py` enhancements
    (parallel work).
  - A broader audit of every `_http_post` call site (defense in
    depth, but the destroy path is the load-bearing case for the
    money-leak symptom — generalising the helper to apply
    everywhere is the right *eventual* shape but blowing up scope
    here invites the kind of "one PR, many fixes" that this rule
    set is trying to prevent).
  - Heartbeat path (B5a no-op write currently disabled per C33
    resolution — unchanged here).

## Open questions

None at design time. The five layers each have a forced choice
(parse vs. ignore, raise vs. swallow, inspect vs. drop), and the
cumulative shape is the only one that closes the money-leak.

## References

  - `PROGRESS.md` lines 97-116 — bug report
  - Commit `270a45a` — bug elevated to top priority
  - `tests/_smoke_harness/runpod_lifecycle.py:41-79`
  - `src/kinoforge/providers/runpod/__init__.py:439-471`
  - `src/kinoforge/cli/_commands.py:1583-1611`
  - `tests/smoke/release_wan22/test_dual_transformer_routing.py:207-224`
  - `tests/smoke/live_wan21/test_branch_routing.py:118-122`
