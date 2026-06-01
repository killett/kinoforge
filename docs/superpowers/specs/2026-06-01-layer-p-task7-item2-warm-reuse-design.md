# Layer P — Task 7 item #2: warm-pod reuse via `instance=` kwarg on `deploy_session`

## Status

Approved 2026-06-01. Sub-design for Layer P, Task 7. Branch `build/layer-p`.
Parent spec: `docs/superpowers/specs/2026-06-01-layer-p-runpod-engine-integration-design.md`.
Sibling sub-spec: `docs/superpowers/specs/2026-06-01-layer-p-task7-item1-offer-retry-design.md`.
Closes `PROGRESS.md:200-203` pending Task 7 item #2.

## Problem

`orchestrator.deploy_session` always provisions a fresh instance — it has no
hook to accept a pre-existing pod. Layer P's live smoke
(`tests/live/test_comfyui_wan_live.py`) therefore calls
`provider.create_instance` manually before `orchestrator.generate`, which
ALSO unconditionally creates a second instance via `deploy_session`'s
internal `_provision_instance_and_build_backend`. Two pods get billed per
smoke iteration; the `find_instance_by_tag` warm-reuse helper (Layer P
Task 3, commit `fefe413`) cannot actually be wired in.

Layer P's whole dev-iteration economics depend on amortising the ~28 GB
Wan 2.2 weights download across many smoke runs by reusing a warm pod
via `KINOFORGE_LIVE_KEEP_POD=1`. Without an `instance=` kwarg on the
orchestrator boundary, each iteration pays the cold-start cost in full.

The `provision_state` marker (Layer I, `src/kinoforge/core/provision_state.py`)
already makes `engine.provision` idempotent for re-attached pods, so the
only missing piece is a way for the caller to say "use THIS pod; don't
allocate a new one." That hook is this sub-spec.

## Goal

Thread an optional `instance: Instance | None = None` kwarg through
`deploy_session` → `generate` → `batch_generate`. When supplied:

- Skip `find_offers` + `create_instance` + the `_provision_instance_and_build_backend` helper.
- Call `engine.provision(instance, cfg_dict)` directly (the marker handles idempotence).
- Build the backend from the supplied instance via `engine.backend(instance, cfg_dict)`.
- Skip `provider.destroy_instance` on `CapabilityMismatch` and `ValidationError`
  teardown paths — the caller owns the lifecycle, the caller decides on destroy.

The kwarg is purely additive: every existing call site passes `instance=None`
and exhibits unchanged behavior.

## Non-goals

- New typed errors for caller-supplied invariants (e.g., dead-pod detection).
  The downstream backend HTTP call surfaces a connection error loudly enough.
- Instance status refresh (`provider.get_instance(instance.id)` on entry).
  Caller's `find_instance_by_tag` already filters by `status == "ready"`;
  any race window between discovery and orchestrator entry is bounded by the
  same provider-side timeout the cold-start path already trusts.
- CLI flag for `--instance-id` on `kinoforge generate` / `kinoforge batch`.
  Smoke is the only caller in this iteration; CLI exposure is a Layer Q candidate.
- Provider-side ABC change. `ComputeProvider.create_instance`, `get_instance`,
  `destroy_instance` signatures unchanged.
- SkyPilot / `LocalProvider` warm-reuse parity (no real-cloud SkyPilot path yet).

## Decisions locked

| # | Decision | Choice | Reason |
|---|---|---|---|
| Q1 | Cache-miss policy when `instance=` supplied | Use supplied instance for `discover` too (skip `create_instance`, call `engine.provision` for idempotent marker check, then `profile.discover`). | Maximum warm-reuse value: even cold-discover on a re-attached pod avoids the cold-start cost. |
| Q2 | `CapabilityMismatch` teardown when `instance=` supplied | Skip `destroy_instance`; re-raise mismatch directly. | Caller owns the pod; caller decides on destroy. Preserves `KEEP_POD` semantics. |
| Q3 | Kwarg propagation scope | `deploy_session` + `generate` + `batch_generate`. Full parity. | Smoke calls `generate()` (not `deploy_session` directly); batch is the obvious sibling and warm-reuse value is identical there. |
| Q4 | Freshness validation when `instance=` supplied | Trust caller as-is. No `get_instance` refresh. | Caller's tag-discovery already filtered by `status=="ready"`. Avoids one GraphQL round-trip per session. Downstream HTTP failures surface dead pods loudly. |
| Q5 | `ValidationError` teardown in `generate()` when `instance=` supplied | Skip `destroy_instance`; re-raise. | Symmetric with Q2. Same ownership rule for both teardown paths. |

## Architecture

### Orchestrator layer — `deploy_session`

`src/kinoforge/core/orchestrator.py`, lines 394–582.

Add a single kwarg to the signature:

```python
@contextmanager
def deploy_session(
    cfg: Config,
    *,
    store: ArtifactStore,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    run_id: str = "run",
    state_dir: Path = Path(".kinoforge"),
    instance: Instance | None = None,        # NEW
) -> Iterator[DeploySession]: ...
```

Internally, derive a private flag:

```python
_caller_supplied_instance = instance is not None
```

Three behavioral branches change:

**1. Cache-miss branch (step 4, currently line 507–522).**

When `requires_compute=True` and the profile is missing, the current code
calls `_provision_instance_and_build_backend(for_discovery=True)` which
internally calls `find_offers` → `_create_with_offer_retry` →
`engine.provision` → `engine.backend`. With `instance=` supplied, replace
that call with a direct provision+backend pair on the supplied instance:

```python
except ProfileNotCached:
    if resolved_engine.requires_compute:
        if resolved_provider is None:
            raise CapacityError(
                "requires_compute is True but no provider was resolved"
            ) from None
        if _caller_supplied_instance:
            # Caller pre-created the pod (warm-reuse path).
            # provision_state marker (Layer I) makes engine.provision idempotent.
            resolved_engine.provision(instance, cfg_dict)
            backend = resolved_engine.backend(instance, cfg_dict)
        else:
            instance, backend = _provision_instance_and_build_backend(
                resolved_engine=resolved_engine,
                resolved_provider=resolved_provider,
                cfg=cfg,
                run_id=run_id,
                key=key,
                creds=creds,
                store=store,
                state_dir=state_dir,
                for_discovery=True,
            )
    else:
        backend = resolved_engine.backend(None, cfg_dict)

    profile = profile_provider.discover(key, resolved_engine, backend)
    _just_discovered = True
```

Note `_provision_compute_once` (currently called inside the helper)
will get called via `engine.provision(...)`; the marker check inside
that engine call enforces idempotence. No double-provision risk.

**2. Cache-hit / steady-state branch (step 7, currently line 532–550).**

Same shape as branch 1 but `for_discovery=False`. Mirror the change:

```python
if backend is None:
    if resolved_engine.requires_compute:
        if resolved_provider is None:
            raise CapacityError(
                "requires_compute is True but no provider was resolved"
            ) from None
        if _caller_supplied_instance:
            resolved_engine.provision(instance, cfg_dict)
            backend = resolved_engine.backend(instance, cfg_dict)
        else:
            instance, backend = _provision_instance_and_build_backend(
                resolved_engine=resolved_engine,
                resolved_provider=resolved_provider,
                cfg=cfg,
                run_id=run_id,
                key=key,
                creds=creds,
                store=store,
                state_dir=state_dir,
                for_discovery=False,
            )
    else:
        backend = resolved_engine.backend(None, cfg_dict)
```

**3. `CapabilityMismatch` teardown (step 8, currently line 555–564).**

Guard the `destroy_instance` call on `not _caller_supplied_instance`:

```python
if not _just_discovered:
    try:
        profile_provider.verify(profile, backend, engine=resolved_engine, key=key)
    except CapabilityMismatch:
        _log.warning(
            "capability mismatch detected; tearing down instance before re-raising"
        )
        if (
            instance is not None
            and resolved_provider is not None
            and not _caller_supplied_instance
        ):
            resolved_provider.destroy_instance(instance.id)
        raise
```

The session yields the same `DeploySession` (its `instance` field will
be the caller-supplied object). No new field on `DeploySession`.

### Orchestrator layer — `generate`

`src/kinoforge/core/orchestrator.py`, lines 734–869.

Add the kwarg and thread it through:

```python
def generate(
    cfg: Config,
    request: GenerationRequest,
    *,
    store: ArtifactStore,
    ...
    sink: OutputSink | None = None,
    instance: Instance | None = None,        # NEW
) -> Artifact:
    ...
    with deploy_session(
        cfg,
        store=store,
        ...
        state_dir=state_dir,
        instance=instance,                    # NEW
    ) as session:
        ...
```

The `ValidationError` teardown at line 859–867 mirrors the
`CapabilityMismatch` skip. Derive `_caller_supplied_instance` locally in
`generate` from the kwarg (NOT from `session.instance`, which can equal
the caller's instance after threading — they happen to be the same
object, but reading the kwarg is the unambiguous source of truth):

```python
try:
    artifact = stage.run(request, segments_override=prompt_segments)
except ValidationError:
    _log.warning(
        "spec validation failed; tearing down instance before re-raising"
    )
    if (
        session.instance is not None
        and session.provider is not None
        and instance is None    # caller did NOT supply — orchestrator owns lifecycle
    ):
        session.provider.destroy_instance(session.instance.id)
    raise
```

### Batch layer — `batch_generate`

`src/kinoforge/core/batch.py`, line 428.

Add the kwarg, thread to `deploy_session`:

```python
def batch_generate(
    cfg: Config,
    manifest: BatchManifest,
    *,
    store: ArtifactStore,
    batch_id: str,
    ...
    sink: OutputSink | None = None,
    instance: Instance | None = None,        # NEW
) -> BatchResult:
    ...
    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        creds=creds,
        profile_provider=profile_provider,
        run_id=batch_id,
        state_dir=state_dir,
        instance=instance,                    # NEW
    ) as session:
```

`batch_generate` does not have its own teardown branches that mirror
`generate`'s `ValidationError` path — the per-entry stages catch their
own exceptions into `BatchOutcome` — so no additional skip-destroy
guard is needed in batch. Only `CapabilityMismatch` mid-batch tears down,
and that path is owned by `deploy_session`'s step 8, already guarded.

### No ABC changes

`ComputeProvider`, `GenerationEngine`, `BackendPool`, `ModelProfileProvider`
signatures unchanged. `Instance` dataclass unchanged. `DeploySession`
dataclass unchanged (its existing `instance: Instance | None` field
carries the caller-supplied object transparently).

## Smoke retrofit

`tests/live/test_comfyui_wan_live.py`. The current shape (post-item-#1):

```python
# [phase=find_offers]
offers = provider.find_offers(reqs)
...
# [phase=create_instance]  — manual loop with CapacityError catch
for candidate in offers:
    try:
        instance = provider.create_instance(ispec)
        break
    except CapacityError:
        continue
...
# [phase=poll_ready]
while instance.status != "ready":
    ...
# [phase=generate]
artifact = orchestrator.generate(cfg, request, provider=provider, ...)  # creates ANOTHER instance
```

New shape (post-item-#2):

```python
# [phase=reuse_check] — already in skeleton
existing = provider.find_instance_by_tag(_TAG_KEY, _TAG_VALUE)

# [phase=generate]
artifact = orchestrator.generate(
    cfg,
    request,
    store=store,
    provider=provider,
    engine_factory=lambda *_: engine,
    instance=existing,                # warm reuse, None on cold start
)
```

The manual `find_offers` + create loop + poll_ready loop all disappear —
they live in `_provision_instance_and_build_backend` and run only on the
cold-start path (`existing is None`). Phases 3–5 in the skeleton collapse
into `phase=generate`. KEEP_POD destroy-skip in `finally` unchanged.

## Test plan

### Orchestrator (`tests/core/test_orchestrator.py`, +6)

Helper: an `_InstanceSupplyProvider(LocalProvider)` subclass that
records every `create_instance` / `destroy_instance` / `find_offers`
call. Tests inspect `provider.create_calls` and
`provider.destroy_calls` lists.

- **`test_deploy_session_with_supplied_instance_skips_create_and_find_offers`**
  — `deploy_session(cfg, store=store, provider=spy, engine=fake_engine, instance=preMade)`;
  pre-seeded profile cache (warm-cache branch). Asserts `spy.find_offers_calls == 0`,
  `spy.create_calls == 0`, session yields with `session.instance is preMade`.
  Bug catch: forgetting to guard either the cache-hit or cache-miss branch
  on `_caller_supplied_instance`.

- **`test_deploy_session_with_supplied_instance_runs_discover_on_cache_miss`**
  — empty profile cache + `instance=preMade`. Asserts `fake_engine.provision_calls`
  contains one entry with `instance is preMade`, `profile_provider.discover_calls == 1`
  (use a counting fake `ModelProfileProvider`, mirroring the item #1 orchestrator
  test pattern), and `spy.create_calls == 0`. Bug catch: forgetting to wire the
  cache-miss branch and only handling cache-hit.

- **`test_deploy_session_with_supplied_instance_calls_engine_provision`**
  — discriminating: even on cache-hit, `engine.provision` must run
  (the marker handles idempotence; skipping provision means a fresh
  worker process on a re-attached pod has no custom nodes installed).
  Bug catch: optimising away `engine.provision` because "the pod's
  already up."

- **`test_deploy_session_supplied_instance_skips_destroy_on_capability_mismatch`**
  — pre-seeded profile cache with mismatching capabilities; fake
  `profile_provider.verify` raises `CapabilityMismatch`. Assert
  `spy.destroy_calls == 0`, `CapabilityMismatch` propagates. Bug catch:
  forgetting the destroy guard in step 8.

- **`test_generate_with_supplied_instance_skips_destroy_on_validation_error`**
  — engine's `validate_spec` raises `ValidationError`. Assert
  `spy.destroy_calls == 0`, `ValidationError` propagates. Bug catch:
  the second teardown branch in `generate` (line 859–867) drifts from
  the first.

- **`test_generate_threads_instance_kwarg_to_deploy_session`**
  — discriminating: spy on inner `deploy_session` resolution.
  Pass `instance=preMade` to `generate`; assert downstream
  `spy.create_calls == 0`. Bug catch: forgetting to forward the kwarg
  from `generate` to `deploy_session`.

### Batch (`tests/core/test_batch.py`, +1)

- **`test_batch_generate_with_supplied_instance_skips_create`**
  — 2-entry manifest + `instance=preMade`. Asserts `spy.create_calls == 0`,
  both entries land in `BatchResult` with `status="ok"`. Bug catch:
  batch_generate forgets the new kwarg.

### Live smoke (`tests/live/test_comfyui_wan_live.py`)

No new tests; the skeleton already covers the green path. Retrofit only:
delete the manual offer-iteration loop, pass `instance=existing` to
`orchestrator.generate`. Phase markers `find_offers` / `create_instance`
/ `poll_ready` collapse into a single `generate` phase on the warm path;
the cold path keeps them logged inside `_provision_instance_and_build_backend`.

## Acceptance criteria

1. `deploy_session(instance=existing)` does NOT call
   `provider.find_offers` or `provider.create_instance`.
2. `deploy_session(instance=existing)` still calls
   `engine.provision(existing, cfg_dict)` (idempotent via the Layer I
   `provision_state` marker).
3. `deploy_session(instance=existing)` builds the backend via
   `engine.backend(existing, cfg_dict)`.
4. With `instance=existing` AND an empty profile cache,
   `engine.provision` + `profile_provider.discover` BOTH run;
   `provider.create_instance` does NOT.
5. `CapabilityMismatch` with `instance=existing` re-raises WITHOUT
   calling `provider.destroy_instance`.
6. `ValidationError` from `engine.validate_spec` in `generate(instance=existing)`
   re-raises WITHOUT calling `provider.destroy_instance`.
7. `generate(instance=existing)` threads the kwarg through to
   `deploy_session` (no double-create on the discriminating spy).
8. `batch_generate(instance=existing)` threads the kwarg through to
   `deploy_session`.
9. `tests/live/test_comfyui_wan_live.py` drops the manual offer-iteration
   loop AND the explicit `provider.create_instance` call; warm path
   passes `instance=existing` to `orchestrator.generate`; cold path
   passes `instance=None`.
10. Full offline gate green: `pixi run pytest` (~846 → ~853 after +7 net
    new tests). `pixi run typecheck`, `pixi run lint`, `pixi run pre-commit
    run --all-files` clean.

## Commit plan

Three atomic commits on `build/layer-p`:

1. **`feat(core/orchestrator): instance= kwarg for warm-pod reuse`**
   — `deploy_session` + `generate` kwarg threading, skip-create on warm
   path, skip-destroy in both teardown branches, +6 regression tests.
2. **`feat(core/batch): instance= kwarg parity for batch_generate`**
   — single-line kwarg thread, +1 regression test.
3. **`refactor(test/live): warm-reuse via orchestrator instance kwarg`**
   — smoke drops manual offer-iteration loop + explicit `create_instance`;
   warm branch passes `instance=existing` to `orchestrator.generate`.

## File-by-file scope

| File | Action | LOC |
|---|---|---|
| `src/kinoforge/core/orchestrator.py` | Modify (`deploy_session` + `generate` kwarg + branches) | ~50 |
| `src/kinoforge/core/batch.py` | Modify (`batch_generate` kwarg thread) | ~3 |
| `tests/core/test_orchestrator.py` | Modify (+6 tests + provider spy helper) | ~160 |
| `tests/core/test_batch.py` | Modify (+1 test) | ~30 |
| `tests/live/test_comfyui_wan_live.py` | Modify (drop manual loop, add kwarg) | ~25 net delete |
| **Total** | 5 files, 0 new | **~220** |

## Out of scope (recorded follow-ups)

- `kinoforge generate --instance-id <id>` CLI flag. Smoke is the only
  caller in this iteration; CLI exposure is a Layer Q candidate.
- `kinoforge batch --instance-id <id>` CLI flag. Same rationale.
- Provider-side dead-instance probe. Caller-supplied dead pods surface
  via downstream backend HTTP failure; no preflight check today.
- `provider.get_instance` freshness refresh on entry. Layer P smoke
  already tag-discovers by `status=="ready"`; future production callers
  may want it.
- Task 7 item #3: workflow API-JSON conversion via warm pod
  (`PROGRESS.md:204`).
- Task 7 item #4: remaining live-iteration unknowns (custom-node
  requirements install path, model routing edge cases, ComfyUI
  multipart shape, history-output shape) — closes only after first
  green MP4.
