# Layer P — Task 7 item #1: orchestrator offer-retry on capacity errors

## Status

Approved 2026-06-01. Sub-design for Layer P, Task 7. Branch `build/layer-p`.
Parent spec: `docs/superpowers/specs/2026-06-01-layer-p-runpod-engine-integration-design.md`.
Closes `PROGRESS.md:182` pending Task 7 item #1.

## Problem

`orchestrator.deploy()` and `orchestrator.deploy_session.__enter__` both pick
`offers[0]` (`src/kinoforge/core/orchestrator.py:274`, `:626`) without retry on
capacity errors. RunPod intermittently returns
`"This machine does not have the resources to deploy your pod"` when the chosen
GPU type has no current host capacity. The live smoke
(`tests/live/test_comfyui_wan_live.py`) already iterates offers with a
string-match catch on `ValueError` (`4a673d7`); the same hardening is missing on
the production code paths used by `kinoforge deploy`, `kinoforge generate`, and
`kinoforge batch`.

Two consumers of `find_offers` therefore exhibit two bugs:

1. **`deploy()` (line 626):** any single-shot user of `kinoforge deploy` against
   RunPod blows up on the first capacity hiccup.
2. **`deploy_session.__enter__` (line 274):** consumed by both `generate()` and
   `batch_generate()`. Same blast radius, plus N silent batch entries fail in a
   row when capacity bounces.

The error signal today is a generic `ValueError` raised from
`RunPodProvider.create_instance` at
`src/kinoforge/providers/runpod/__init__.py:504`. The smoke caches around it
with a substring sniff (`"resources to deploy" in str(exc)`) — fragile against
any RunPod copy edit.

## Goal

Make `deploy()` and `deploy_session.__enter__` survive transient RunPod
"no host capacity" responses by iterating offers in their already-sorted-by-
`gpu_preference` order. Surface the signal as a typed `CapacityError` from the
provider so the orchestrator catches a contract, not a string.

## Non-goals

- SkyPilot / future-provider plumbing of `CapacityError` (no real-cloud path
  yet; PROGRESS:113 carry-forward #2).
- Serverless mode (`mode=serverless` at runpod line 545–566): different
  capacity model (concurrency caps, not host availability).
- Warm-pod reuse via `instance=` kwarg on `deploy_session` (Task 7 item #2;
  separate change).
- Workflow API-JSON conversion (Task 7 item #3).
- Cap or jitter on the offer-iteration retry; offers are already filter+sorted
  by `filter_offers` (Phase 2 Task 5), so the worst case is bounded.

## Decisions locked

| # | Decision | Choice | Reason |
|---|---|---|---|
| Q1 | Scope of fix | Both `deploy()` and `deploy_session.__enter__` via a shared private helper in `orchestrator.py`. | Same latent bug, same fix; PROGRESS:182 named only one site but the second is in the same module. |
| Q2 | Error signal | Provider raises typed `CapacityError`; orchestrator catches the type, not a string. | Layer N philosophy: typed shapes over string sniffs. |
| Q3 | Exhaustion behavior | Iterate all offers in input order; on exhaustion raise `CapacityError("all N offers exhausted; …")` with the last per-offer `CapacityError` chained as `__cause__`. | Preserves provider's last raw message for debugging. |
| Q4 | Helper placement | Private `_create_with_offer_retry` at top of `orchestrator.py`. | Caller-side orchestration concern; both call sites already live in this module; no new module surface; no ABC change. |

## Architecture

### Provider layer — `RunPodProvider.create_instance`

`src/kinoforge/providers/runpod/__init__.py`, around lines 502–507.

When the GraphQL mutation response contains an `errors` block, the existing
code assembles `error_msgs = [str(e.get("message", e)) for e in resp["errors"]]`
and raises a generic `ValueError`. After this change, the assembled message is
inspected (case-insensitive) for the substring `"resources to deploy"`:

- Match → `raise CapacityError(f"RunPod has no current capacity for {spec.offer.gpu_type!r}: {msg}") from value_error`.
- No match → unchanged `raise ValueError(...)`. (Back-compat for auth /
  malformed-body emitters, and the null-pod-id emitter at line 514.)

`from kinoforge.core.errors import CapacityError` added to the imports. The
serverless mutation (lines 545–566) is **untouched** — capacity model differs.

### Orchestrator layer — `_create_with_offer_retry`

`src/kinoforge/core/orchestrator.py`. New private helper near existing helpers
(above `deploy_session`):

```python
def _create_with_offer_retry(
    provider: ComputeProvider,
    build_spec: Callable[[Offer], InstanceSpec],
    offers: list[Offer],
) -> tuple[Instance, Offer]:
    """Iterate offers until create_instance succeeds.

    Catches CapacityError per offer (provider has no host capacity for that
    GPU type) and continues to the next. Any other exception propagates
    immediately — non-capacity errors fail every offer the same way.

    Raises:
        CapacityError: every offer exhausted; the last per-offer
            CapacityError is chained as __cause__.
    """
    last_capacity_exc: CapacityError | None = None
    for offer in offers:
        spec = build_spec(offer)
        try:
            instance = provider.create_instance(spec)
            return instance, offer
        except CapacityError as exc:
            last_capacity_exc = exc
            _log.warning(
                "[offer-retry] %s @ $%.4f/hr unavailable: %s",
                offer.gpu_type,
                offer.cost_rate_usd_per_hr,
                exc,
            )
            continue
    raise CapacityError(
        f"all {len(offers)} offers exhausted; provider "
        f"{getattr(provider, 'name', repr(provider))!r} "
        f"has no current capacity"
    ) from last_capacity_exc
```

### Call-site rewires

**`deploy_session.__enter__`** (current lines ~262–283):

```python
def _build_spec(offer: Offer) -> InstanceSpec:
    return InstanceSpec(
        image=image,
        offer=offer,
        lifecycle=lifecycle,
        tags={
            "kinoforge_engine": resolved_engine.name,
            "kinoforge_key": key_hash,
        },
        env={},
        run_id=run_id,
    )

instance, _chosen = _create_with_offer_retry(resolved_provider, _build_spec, offers)
```

**`deploy()`** (current lines ~622–636):

Identical pattern. `run_id=""` matches the existing literal at line 633. The
`try / except BaseException` block at lines 638–668 (the Layer N
destroy-on-error wrap) is unchanged — retry happens *before* the helper returns
the live instance.

### No ABC changes

`ComputeProvider.create_instance` signature unchanged. `LocalProvider`,
`SkyPilotProvider`, `FakeProvider` unchanged — they do not raise
`CapacityError` today, so `_create_with_offer_retry` returns on first
iteration for them.

## Smoke retrofit

`tests/live/test_comfyui_wan_live.py` lines 197–247: swap
`except ValueError as exc: if "resources to deploy" in str(exc):` to
`except CapacityError:`. Keep the surrounding `for candidate in offers:` loop
intact — the smoke calls `provider.create_instance` directly (not through
`deploy_session`), so orchestrator-side retry does not yet apply. The loop
deletes naturally once Task 7 item #2 (`instance=` kwarg) lands.

## Test plan

### Provider (`tests/providers/test_runpod.py`, +3)

- **`test_create_instance_raises_capacity_error_on_no_resources`** — fake
  `_http_post` returns
  `{"errors":[{"message":"This machine does not have the resources to deploy your pod"}]}`.
  Bug catch: any non-typed raise (e.g., re-raising `ValueError` instead of
  `CapacityError`) makes the orchestrator-side retry inert and reintroduces the
  original failure.
- **`test_create_instance_capacity_error_chains_cause`** — same fake input;
  assert `exc.__cause__` is `ValueError` and its `str()` carries the original
  RunPod message verbatim. Bug catch: dropping `from value_error` loses
  debugging signal across the orchestrator boundary.
- **`test_create_instance_non_capacity_error_still_raises_value_error`** —
  fake returns `{"errors":[{"message":"template not found"}]}`. Asserts
  `pytest.raises(ValueError)` and explicitly NOT `CapacityError`. Bug catch:
  over-eager regex on the errors block would silently turn auth / template
  errors into retry-eligible capacity errors.

### Orchestrator (`tests/core/test_orchestrator.py`, +5)

Pattern: subclassed `LocalProvider` named `_OfferRetryProvider` that records
each `create_instance` call and is configured with a list of per-call outcomes
(`"capacity"` → raise `CapacityError`; `"value"` → raise `ValueError("non-cap")`;
`"ok"` → return a real `Instance`).

- **`test_deploy_retries_next_offer_on_capacity_error`** — 3 offers; outcomes
  `["capacity", "ok", "ok"]`; assert `provider.calls[1].offer.id == offers[1].id`
  and the returned `DeployResult.instance.id` matches offer[1]'s instance.
  Verify `destroy_instance` NOT called (no instance was ever created on
  offer[0]).
- **`test_deploy_iterates_offers_in_input_order`** — 5 offers, all
  `"capacity"` except offer[3]; assert `provider.calls` records the exact
  ordered sequence `[0, 1, 2, 3]`. Discriminating order lockdown — catches a
  future change that reverses or randomizes offer iteration.
- **`test_deploy_raises_capacity_error_when_all_offers_exhausted`** — 3
  offers; outcomes `["capacity"]*3`. Assert `CapacityError`, message contains
  `"3 offers exhausted"`, `exc.__cause__` is the *last* per-offer
  `CapacityError` (identity check by spy-marked exc instances). Bug catch:
  raising the wrong type or the wrong `__cause__` masks RunPod's last
  diagnostic.
- **`test_deploy_does_not_retry_on_non_capacity_error`** — 3 offers; outcomes
  `["value", "ok", "ok"]`. Assert `pytest.raises(ValueError)` and
  `len(provider.calls) == 1`. Bug catch: catching `Exception` (not
  `CapacityError`) would silently retry auth failures across every offer.
- **`test_deploy_session_retries_next_offer_on_capacity_error`** — parity
  check for the second call site. Same outcomes as the first orchestrator
  test, but driven through `with deploy_session(...) as s:`. Bug catch:
  forgetting to rewire the second call site.

`test_deploy_destroys_pod_when_get_instance_raises` (line 1192) and other
existing deploy tests remain unchanged — retry runs before
`create_instance` returns; destroy-on-error is a post-create concern.

## Acceptance criteria

1. `RunPodProvider.create_instance` raises `CapacityError` (not `ValueError`)
   when the mutation error message contains `"resources to deploy"`
   (case-insensitive).
2. `RunPodProvider.create_instance` continues to raise `ValueError` for all
   other mutation errors.
3. The raised `CapacityError` chains the original `ValueError` as `__cause__`.
4. `deploy()` retries `create_instance` across `find_offers`-returned offers
   in input order on `CapacityError`.
5. `deploy()` does NOT retry on any other exception (immediate propagation).
6. `deploy()` raises `CapacityError("all N offers exhausted; …")` when every
   offer raises `CapacityError`; `__cause__` is the last per-offer
   `CapacityError`.
7. `deploy_session.__enter__` exhibits identical retry behavior (AC4–AC6
   mirror).
8. Smoke `tests/live/test_comfyui_wan_live.py` swaps the `ValueError +
   "resources to deploy"` catch to `except CapacityError`. Smoke control flow
   unchanged.
9. Full offline gate green: `pixi run pytest` (~836 → ~844 after +8 net new
   tests).
10. `pixi run typecheck`, `pixi run lint`, `pixi run pre-commit run
    --all-files` clean.

## Commit plan

Three atomic commits on `build/layer-p`:

1. **`feat(providers/runpod): typed CapacityError on no-resources mutation`**
   — provider-side typed raise + 3 regression tests.
2. **`feat(core/orchestrator): offer-retry across deploy + deploy_session`**
   — `_create_with_offer_retry` helper + 2 call-site rewires + 5 regression
   tests.
3. **`refactor(test/live): swap ValueError sniff to typed CapacityError`** —
   smoke retrofit.

## File-by-file scope

| File | Action | LOC |
|---|---|---|
| `src/kinoforge/providers/runpod/__init__.py` | Modify (capacity-error branch) | ~10 |
| `src/kinoforge/core/orchestrator.py` | Modify (helper + 2 rewires) | ~40 |
| `tests/providers/test_runpod.py` | Modify (+3 tests) | ~60 |
| `tests/core/test_orchestrator.py` | Modify (+5 tests + fake) | ~120 |
| `tests/live/test_comfyui_wan_live.py` | Modify (smoke retrofit) | ~3 |
| **Total** | 5 files, 0 new | **~230** |

## Out of scope (recorded follow-ups)

- Task 7 item #2: `instance=` kwarg on `deploy_session` for warm-pod reuse
  (`PROGRESS.md:183`).
- Task 7 item #3: workflow API-JSON conversion via warm pod (`PROGRESS.md:187`).
- SkyPilot capacity-error plumbing (PROGRESS:113 carry-forward #2).
- Serverless-mode capacity model (concurrency caps, not host availability).
