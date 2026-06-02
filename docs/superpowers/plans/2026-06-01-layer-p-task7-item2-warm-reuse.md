# Layer P Task 7 item #2 — warm-pod reuse via `instance=` + `tags=` kwargs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thread `instance: Instance | None = None` and `tags: dict[str, str] | None = None` kwargs through `deploy_session` → `generate` → `batch_generate`; orchestrator skips internal `create_instance` when `instance=` supplied and merges caller `tags=` onto orchestrator-built `InstanceSpec.tags` on the cold path; smoke drops its manual `find_offers` + `create_instance` + `poll_ready` block and unconditionally calls `orchestrator.generate(instance=existing, tags={...})`.

**Architecture:** Pure additive kwarg threading. Two private guards inside `deploy_session`: `_caller_supplied_instance = instance is not None` short-circuits create + skips destroy in `CapabilityMismatch` teardown; same flag in `generate` skips destroy in `ValidationError` teardown. `tags=` flows into `_provision_instance_and_build_backend` and `deploy()`'s `_build_spec` closures, where it merges over the built-in `{kinoforge_engine, kinoforge_key}` dict (caller wins on collision). When `instance=` supplied, `tags=` is ignored (caller owns instance lifecycle + tags). No ABC changes; `Instance`, `DeploySession`, `ComputeProvider`, `GenerationEngine` signatures all unchanged.

**Tech Stack:** Python 3.13, pytest, pydantic v2 (untouched), kinoforge stdlib-only orchestrator layer.

**Spec:** `docs/superpowers/specs/2026-06-01-layer-p-task7-item2-warm-reuse-design.md` (committed `e5a367a` + amendment `eb5caff`)

**Branch:** `build/layer-p` (off `main@7788f93`, currently at `eb5caff`)

---

## File structure

**New files:** None.

**Modified files:**

| Path | Owner task | Change |
|---|---|---|
| `src/kinoforge/core/orchestrator.py` | Task 1 | `deploy_session` + `generate` gain `instance=` + `tags=` kwargs; `_provision_instance_and_build_backend` gains `tags=` param + `merged_tags` block; cache-miss + cache-hit branches in `deploy_session` short-circuit on supplied instance; `CapabilityMismatch` teardown skips destroy when caller-supplied; `generate`'s `ValidationError` teardown mirrors the skip; `deploy()`'s `_build_spec` also gains the tags-merge so `kinoforge deploy --tag k=v` (future CLI) works the same way. |
| `src/kinoforge/core/batch.py` | Task 2 | `batch_generate` gains `instance=` + `tags=` kwargs, threaded into `deploy_session` call. |
| `tests/core/test_orchestrator.py` | Task 1 | +8 tests + `_InstanceSupplyProvider(LocalProvider)` spy + `_CountingProfileProvider` spy. |
| `tests/core/test_batch.py` | Task 2 | +1 test. |
| `tests/live/test_comfyui_wan_live.py` | Task 3 | Delete the `if not warm:` block (~lines 184–268); call `orchestrator.generate(instance=existing, tags={...})` unconditionally; collapse phases 3–5 markers into a single `phase=generate` block. |

---

## Task 1: Orchestrator `instance=` + `tags=` kwargs

**Goal:** `deploy_session` and `generate` accept new `instance: Instance | None = None` and `tags: dict[str, str] | None = None` kwargs. Supplied instance short-circuits internal `create_instance` (Q1); caller `tags=` merges over orchestrator built-ins on the cold path (Q6). `CapabilityMismatch` and `ValidationError` teardowns skip `destroy_instance` when `instance=` was caller-supplied (Q2, Q5). Trust caller as-is, no refresh (Q4).

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:225` (Callable import already present), `:273-345` (`_provision_instance_and_build_backend` adds `tags=` param + `merged_tags` block), `:394-466` (`deploy_session` signature + kwarg branches), `:507-548` (cache-miss / cache-hit branches), `:555-564` (`CapabilityMismatch` teardown guard), `:679-694` (`deploy()`'s `_build_spec` merge), `:734-869` (`generate` signature + kwarg threading + `ValidationError` teardown guard)
- Modify: `tests/core/test_orchestrator.py` (+8 tests + `_InstanceSupplyProvider(LocalProvider)` spy near the existing `_OfferRetryProvider` block at line ~1192)

**Acceptance Criteria:**
- [ ] `deploy_session(instance=existing)` does NOT call `provider.find_offers` or `provider.create_instance`.
- [ ] `deploy_session(instance=existing)` still calls `engine.provision(existing, cfg_dict)` (idempotent via Layer I marker).
- [ ] `deploy_session(instance=existing)` builds backend via `engine.backend(existing, cfg_dict)`.
- [ ] With `instance=existing` AND empty profile cache: `engine.provision` + `profile_provider.discover` BOTH run; `provider.create_instance` does NOT.
- [ ] `CapabilityMismatch` mid-session with `instance=existing` re-raises WITHOUT calling `provider.destroy_instance`.
- [ ] `ValidationError` in `generate(instance=existing)` re-raises WITHOUT calling `provider.destroy_instance`.
- [ ] `generate(instance=existing)` threads kwarg through to `deploy_session` (downstream `create_instance` not called).
- [ ] `deploy_session(tags={"k": "v"})` (no `instance=`) merges caller tags onto orchestrator-built `InstanceSpec.tags`; `tags["kinoforge_engine"]` + `tags["kinoforge_key"]` still present.
- [ ] `deploy_session(tags={"k": "v"}, instance=existing)` leaves supplied instance's tags untouched; tags kwarg ignored.

**Verify:**
```bash
pixi run pytest tests/core/test_orchestrator.py -v -k "supplied_instance or threads_instance or threads_tags or tags_ignored"
```
Expected: 8/8 pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_orchestrator.py` near the existing `_OfferRetryProvider` block (~line 1192). Mirror the item #1 fake-provider pattern.

```python
# ---- Layer P Task 7 item #2: instance= + tags= kwarg tests ---------------

class _InstanceSupplyProvider(LocalProvider):
    """LocalProvider spy that records create_instance + destroy_instance + find_offers.

    Used by Task 7 item #2 tests to assert orchestrator skips create when
    `instance=` is supplied and skips destroy on teardown when caller owns
    the instance lifecycle.
    """

    def __init__(self) -> None:
        super().__init__()
        self.create_calls: list[InstanceSpec] = []
        self.destroy_calls: list[str] = []
        self.find_offers_calls: int = 0

    def find_offers(self, requirements: HardwareRequirements) -> list[Offer]:
        self.find_offers_calls += 1
        return super().find_offers(requirements)

    def create_instance(self, spec: InstanceSpec) -> Instance:
        self.create_calls.append(spec)
        return super().create_instance(spec)

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)
        super().destroy_instance(instance_id)


def _make_premade_instance() -> Instance:
    """Build a fully-ready Instance dataclass for instance= kwarg tests."""
    return Instance(
        id="pod-premade-7b2",
        status="ready",
        endpoints={},
        tags={"kinoforge.layer": "layer-p-smoke", "mode": "pod"},
        offer=Offer(
            id="offer-A40",
            gpu_type="NVIDIA A40",
            cuda_capability="8.6",
            vram_gb=48,
            cost_rate_usd_per_hr=0.35,
        ),
        created_at=0.0,
        last_heartbeat=None,
    )


def test_deploy_session_with_supplied_instance_skips_create_and_find_offers(
    tmp_path: Path,
) -> None:
    """instance= supplied + warm cache → no find_offers + no create_instance."""
    cfg = _make_fake_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()

    # Pre-seed the profile cache so deploy_session takes the warm-cache branch.
    _seed_profile_cache(store, cfg.capability_key())

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        instance=premade,
    ) as session:
        assert session.instance is premade
        assert session.backend is not None

    assert spy.find_offers_calls == 0
    assert spy.create_calls == []
    assert engine.provision_calls == [premade]  # marker handles idempotence
    assert engine.backend_calls == [premade]


def test_deploy_session_with_supplied_instance_runs_discover_on_cache_miss(
    tmp_path: Path,
) -> None:
    """instance= + empty cache → engine.provision + discover both run; no create."""
    cfg = _make_fake_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    profile_provider = _CountingProfileProvider()
    premade = _make_premade_instance()

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        profile_provider=profile_provider,
        instance=premade,
    ) as session:
        assert session.instance is premade

    assert spy.find_offers_calls == 0
    assert spy.create_calls == []
    assert engine.provision_calls == [premade]
    assert profile_provider.discover_calls == 1
    assert profile_provider.verify_calls == 0  # skip verify on just-discovered


def test_deploy_session_supplied_instance_calls_engine_provision(
    tmp_path: Path,
) -> None:
    """Discriminating: even on cache-hit, engine.provision must run for re-attached pod."""
    cfg = _make_fake_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    _seed_profile_cache(store, cfg.capability_key())

    with deploy_session(
        cfg, store=store, provider=spy, engine=engine, instance=premade,
    ) as _:
        pass

    assert engine.provision_calls == [premade]


def test_deploy_session_supplied_instance_skips_destroy_on_capability_mismatch(
    tmp_path: Path,
) -> None:
    """CapabilityMismatch + instance= → destroy NOT called; mismatch propagates."""
    cfg = _make_fake_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    profile_provider = _MismatchingProfileProvider()
    _seed_profile_cache(store, cfg.capability_key())
    premade = _make_premade_instance()

    with pytest.raises(CapabilityMismatch):
        with deploy_session(
            cfg,
            store=store,
            provider=spy,
            engine=engine,
            profile_provider=profile_provider,
            instance=premade,
        ) as _:
            pass

    assert spy.destroy_calls == []


def test_generate_with_supplied_instance_skips_destroy_on_validation_error(
    tmp_path: Path,
) -> None:
    """ValidationError in generate + instance= → destroy NOT called."""
    cfg = _make_fake_compute_config_with_validating_spec()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _RaisingValidateSpecFakeEngine()    # raises ValidationError("bad spec")
    premade = _make_premade_instance()
    request = _make_fake_request(prompt="hi", mode="t2v")

    with pytest.raises(ValidationError):
        generate(
            cfg,
            request,
            store=store,
            provider=spy,
            engine=engine,
            instance=premade,
        )

    assert spy.destroy_calls == []


def test_generate_threads_instance_kwarg_to_deploy_session(
    tmp_path: Path,
) -> None:
    """Discriminating: generate(instance=) → downstream provider.create_instance NOT called."""
    cfg = _make_fake_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    request = _make_fake_request(prompt="hi", mode="t2v")

    artifact = generate(
        cfg, request, store=store, provider=spy, engine=engine, instance=premade,
    )

    assert spy.create_calls == []
    assert artifact is not None


def test_deploy_session_threads_tags_into_instance_spec(
    tmp_path: Path,
) -> None:
    """tags={"k":"v"} (no instance=) → orchestrator-built InstanceSpec.tags merged."""
    cfg = _make_fake_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        tags={"kinoforge.layer": "layer-p-smoke", "mode": "pod"},
    ) as _:
        pass

    assert len(spy.create_calls) == 1
    created_spec = spy.create_calls[0]
    assert created_spec.tags["kinoforge.layer"] == "layer-p-smoke"
    assert created_spec.tags["mode"] == "pod"
    # Engine + key tags must still be present — merge, not replace.
    assert "kinoforge_engine" in created_spec.tags
    assert "kinoforge_key" in created_spec.tags


def test_deploy_session_tags_ignored_when_instance_supplied(
    tmp_path: Path,
) -> None:
    """tags= + instance= → caller's instance tags untouched; tags= kwarg ignored."""
    cfg = _make_fake_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    original_tags = dict(premade.tags)
    _seed_profile_cache(store, cfg.capability_key())

    with deploy_session(
        cfg,
        store=store,
        provider=spy,
        engine=engine,
        instance=premade,
        tags={"override": "should-be-ignored"},
    ) as session:
        assert session.instance is premade
        assert dict(session.instance.tags) == original_tags
        assert "override" not in session.instance.tags

    assert spy.create_calls == []
```

Helpers referenced (`_make_fake_compute_config`, `_seed_profile_cache`, `_CountingFakeEngine`, `_CountingProfileProvider`, `_MismatchingProfileProvider`, `_RaisingValidateSpecFakeEngine`, `_make_fake_request`) — match the existing test file's pattern. Search the file for `def _make_fake` / `class _` test helpers and use the existing names. If a helper does not exist (`_CountingProfileProvider`, `_MismatchingProfileProvider`, `_RaisingValidateSpecFakeEngine`), add it inline to the test file near the other helpers.

- [ ] **Step 2: Run tests to verify they fail**

```bash
pixi run pytest tests/core/test_orchestrator.py -v -k "supplied_instance or threads_instance or threads_tags or tags_ignored"
```
Expected: 8 FAIL — `TypeError: deploy_session() got an unexpected keyword argument 'instance'` OR `'tags'`.

- [ ] **Step 3: Implement the orchestrator changes**

**a. `deploy_session` signature (`src/kinoforge/core/orchestrator.py:395-405`).**

Add two kwargs in alphabetical-ish order at the end of the signature:

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
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
) -> Iterator[DeploySession]:
```

Inside the function body, immediately after `cfg_dict = _cfg_dict(cfg)`:

```python
_caller_supplied_instance = instance is not None
```

**b. Cache-miss branch (`:507-522`).**

Replace the `if resolved_engine.requires_compute:` block:

```python
    except ProfileNotCached:
        _log.debug(
            "profile cache miss for key %s — running discover", key.derive()[:12]
        )
        if resolved_engine.requires_compute:
            if resolved_provider is None:
                raise CapacityError(
                    "requires_compute is True but no provider was resolved"
                ) from None
            if _caller_supplied_instance:
                # Caller pre-created the pod; marker-idempotent provision.
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
                    tags=tags,
                )
        else:
            backend = resolved_engine.backend(None, cfg_dict)

        profile = profile_provider.discover(key, resolved_engine, backend)
        _just_discovered = True
```

**c. Cache-hit branch (`:532-548`).**

Mirror the same shape (just `for_discovery=False`):

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
                    tags=tags,
                )
        else:
            backend = resolved_engine.backend(None, cfg_dict)
```

**d. `CapabilityMismatch` teardown guard (`:555-564`).**

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

**e. `_provision_instance_and_build_backend` signature + body (`:273-345`).**

Add `tags: dict[str, str] | None` to the signature (keyword-only). Inside `_build_spec`, replace the existing `tags=` literal with a merged dict:

```python
def _provision_instance_and_build_backend(
    *,
    resolved_engine: GenerationEngine,
    resolved_provider: ComputeProvider,
    cfg: Config,
    run_id: str,
    key: CapabilityKey,
    creds: CredentialProvider | None,
    store: ArtifactStore,
    state_dir: Path,
    for_discovery: bool,
    tags: dict[str, str] | None = None,
) -> tuple[Instance, GenerationBackend]:
    ...
    def _build_spec(offer: Offer) -> InstanceSpec:
        merged_tags: dict[str, str] = {
            "kinoforge_engine": resolved_engine.name,
            "kinoforge_key": key_hash,
        }
        if tags:
            merged_tags.update(tags)
        return InstanceSpec(
            image=image,
            offer=offer,
            lifecycle=lifecycle,
            tags=merged_tags,
            env={},
            run_id=run_id,
        )
```

**f. `deploy()`'s `_build_spec` (`:679-694`).**

Add the same merge so a future `kinoforge deploy --tag k=v` CLI flag uses the same code path. `deploy()` signature gains `tags: dict[str, str] | None = None`:

```python
def deploy(
    cfg: Config,
    *,
    dry_run: bool = False,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    creds: CredentialProvider | None = None,
    tags: dict[str, str] | None = None,
) -> DeployResult:
    ...
    def _build_spec(offer: Offer) -> InstanceSpec:
        merged_tags: dict[str, str] = {
            "kinoforge_engine": resolved_engine.name,
            "kinoforge_key": key_hash,
        }
        if tags:
            merged_tags.update(tags)
        return InstanceSpec(
            image=image,
            offer=offer,
            lifecycle=lifecycle,
            tags=merged_tags,
            env={},
            run_id="",
        )
```

**g. `generate` signature + ValidationError teardown guard (`:734-869`).**

Add `instance=` + `tags=` to the signature, thread both into `deploy_session`, and guard the `ValidationError` teardown:

```python
def generate(
    cfg: Config,
    request: GenerationRequest,
    *,
    store: ArtifactStore,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    run_id: str = "run",
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
) -> Artifact:
    ...
    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        creds=creds,
        profile_provider=profile_provider,
        run_id=run_id,
        state_dir=state_dir,
        instance=instance,
        tags=tags,
    ) as session:
        ...
        try:
            artifact = stage.run(request, segments_override=prompt_segments)
        except ValidationError:
            _log.warning(
                "spec validation failed; tearing down instance before re-raising"
            )
            if (
                session.instance is not None
                and session.provider is not None
                and instance is None    # caller did NOT supply → orchestrator owns lifecycle
            ):
                session.provider.destroy_instance(session.instance.id)
            raise
        _log.info("generate completed — artifact uri=%r", artifact.uri)
        return artifact
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pixi run pytest tests/core/test_orchestrator.py -v -k "supplied_instance or threads_instance or threads_tags or tags_ignored"
```
Expected: 8/8 PASS.

- [ ] **Step 5: Run the full orchestrator test suite — no regression**

```bash
pixi run pytest tests/core/test_orchestrator.py -v
```
Expected: all existing pass + 8 new.

- [ ] **Step 6: Run typecheck + lint + pre-commit on changed files**

```bash
pixi run typecheck
pixi run lint
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
```

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(core/orchestrator): instance= + tags= kwargs for warm-pod reuse (Layer P Task 7 item #2)

Adds two optional kwargs to deploy_session, deploy, and generate:

- instance: Instance | None — pre-created pod from caller. When supplied,
  orchestrator skips find_offers + create_instance and builds the backend
  directly from the caller's instance. engine.provision still runs
  (idempotent via Layer I provision_state marker).
- tags: dict[str, str] | None — operator-supplied tags merged onto the
  orchestrator's built-in {kinoforge_engine, kinoforge_key} when the
  orchestrator creates the pod on the cold path. Caller wins on collision.
  Ignored when instance= is supplied (caller already owns the instance's
  tags).

Teardown semantics: when instance= was caller-supplied, CapabilityMismatch
(deploy_session) and ValidationError (generate) re-raise WITHOUT calling
provider.destroy_instance. Caller owns the lifecycle.

Enables the Layer P live smoke's warm-pod iteration loop: tag-discovery
via find_instance_by_tag now sees pods that the orchestrator created on
the previous iteration's cold path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Batch parity — `batch_generate(instance=, tags=)`

**Goal:** `batch_generate` threads `instance=` and `tags=` through to `deploy_session`. Same warm-reuse semantics as `generate`, applied once per batch (one shared instance across all entries).

**Files:**
- Modify: `src/kinoforge/core/batch.py:428-547` (signature + `deploy_session` call)
- Modify: `tests/core/test_batch.py` (+1 test)

**Acceptance Criteria:**
- [ ] `batch_generate(instance=existing)` does NOT call `provider.create_instance`.
- [ ] `batch_generate` threads `tags=` kwarg through to `deploy_session`.
- [ ] All existing batch tests still pass.

**Verify:**
```bash
pixi run pytest tests/core/test_batch.py -v -k "supplied_instance"
```
Expected: 1/1 pass + all existing batch tests still pass.

**Steps:**

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_batch.py`:

```python
def test_batch_generate_with_supplied_instance_skips_create(tmp_path: Path) -> None:
    """instance= supplied → batch_generate does NOT call create_instance."""
    cfg = _make_batch_compute_config()
    store = LocalArtifactStore(root=tmp_path)
    spy = _InstanceSupplyProvider()
    engine = _CountingFakeEngine()
    premade = _make_premade_instance()
    manifest = _make_two_entry_manifest()

    result = batch_generate(
        cfg,
        manifest,
        store=store,
        batch_id="batch-itest-7b2",
        provider=spy,
        engine=engine,
        instance=premade,
    )

    assert spy.create_calls == []
    assert len(result.outcomes) == 2
    assert all(o.status == "ok" for o in result.outcomes)
```

Reuse `_InstanceSupplyProvider`, `_CountingFakeEngine`, `_make_premade_instance` — either import from `test_orchestrator.py` or copy the helpers into a shared `tests/conftest.py` fixture. Pick the lighter path (a 1-line import is fine if `test_orchestrator.py` exposes them at module level).

- [ ] **Step 2: Run test to verify it fails**

```bash
pixi run pytest tests/core/test_batch.py -v -k "supplied_instance"
```
Expected: FAIL — `TypeError: batch_generate() got an unexpected keyword argument 'instance'`.

- [ ] **Step 3: Implement the batch_generate change**

In `src/kinoforge/core/batch.py:428`, add the two kwargs and thread them:

```python
def batch_generate(
    cfg: Config,
    manifest: BatchManifest,
    *,
    store: ArtifactStore,
    batch_id: str,
    concurrent: int | None = None,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
) -> BatchResult:
    ...
    try:
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            creds=creds,
            profile_provider=profile_provider,
            run_id=batch_id,
            state_dir=state_dir,
            instance=instance,
            tags=tags,
        ) as session:
            ...
```

Add `from kinoforge.core.interfaces import Instance` at the top if not already imported.

- [ ] **Step 4: Run test to verify it passes**

```bash
pixi run pytest tests/core/test_batch.py -v -k "supplied_instance"
```
Expected: PASS.

- [ ] **Step 5: Run the full batch suite — no regression**

```bash
pixi run pytest tests/core/test_batch.py -v
```

- [ ] **Step 6: Typecheck + lint + pre-commit**

```bash
pixi run typecheck
pixi run lint
pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch.py
```

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/batch.py tests/core/test_batch.py
git commit -m "$(cat <<'EOF'
feat(core/batch): instance= + tags= kwarg parity for batch_generate (Layer P Task 7 item #2)

Threads the new instance= and tags= kwargs through batch_generate to its
internal deploy_session call. Semantics identical to generate(): one
shared pre-created instance amortises across every batch entry; caller
tags merge onto orchestrator-built InstanceSpec.tags on the cold path.

+1 regression test (test_batch_generate_with_supplied_instance_skips_create).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Smoke rewire — warm-reuse via orchestrator kwargs

**Goal:** Live smoke deletes the manual `if not warm:` block (manual `find_offers`/`create_instance`/`poll_ready`); calls `orchestrator.generate(instance=existing, tags={...})` unconditionally. Phases 3–5 (find_offers/create_instance/poll_ready) collapse into a single `phase=generate` marker on both cold and warm paths.

**Files:**
- Modify: `tests/live/test_comfyui_wan_live.py:184-268` (delete the `if not warm:` block) + `:296-305` (`generate(...)` call adds `instance=instance` + `tags={...}`)

**Acceptance Criteria:**
- [ ] Smoke deletes the `if not warm:` block (find_offers + create_instance loop + poll_ready).
- [ ] Smoke unconditionally passes `instance=existing` + `tags={...}` to `orchestrator.generate`.
- [ ] Tags dict includes `_TAG_KEY: _TAG_VALUE`, `"mode": "pod"`, `"kinoforge.git_sha": _git_sha()`.
- [ ] `pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v` reports collected-and-skipped without live env vars.
- [ ] `pixi run lint` + `pixi run typecheck` clean.

**Verify:**
```bash
pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v
pixi run typecheck
pixi run lint
```
Expected: collection green; lint/typecheck clean.

**Steps:**

- [ ] **Step 1: Read the current smoke**

```bash
sed -n '170,310p' tests/live/test_comfyui_wan_live.py
```

Confirm the structure of `if not warm:` block + the `generate(...)` call below it.

- [ ] **Step 2: Delete `if not warm:` block + add kwargs to `generate(...)` call**

Replace lines ~184–268 (the entire `if not warm:` block including `[phase=find_offers]`, `[phase=create_instance]`, `[phase=poll_ready]`) with a single comment line:

```python
        # Cold/warm both flow through orchestrator.generate(): warm path
        # passes the discovered instance; cold path passes instance=None
        # and the orchestrator's _provision_instance_and_build_backend
        # handles find_offers + create_instance (with item #1 offer-retry)
        # + poll_ready. tags= ensures the cold-path pod carries
        # _TAG_KEY=_TAG_VALUE so the NEXT iteration's
        # find_instance_by_tag(...) rediscovers it.
```

Then in the `generate(...)` call at ~line 296, add `instance=instance` + `tags={...}`:

```python
        artifact = generate(
            cfg,
            request,
            store=store,
            sink=sink,
            provider=provider,
            engine=engine,
            creds=creds,
            state_dir=state_dir,
            instance=instance,           # NEW — None on cold start, discovered pod when warm
            tags={                       # NEW — preserved across orchestrator-managed creates
                "mode": "pod",
                _TAG_KEY: _TAG_VALUE,
                "kinoforge.git_sha": _git_sha(),
            },
        )

        # pod_id may be unset before this point on cold start; pull it from
        # the artifact's source instance via session, OR rely on the
        # subsequent finally-block destroy guard (which already handles a
        # None pod_id).
```

The `instance` local at this point is either the discovered warm pod (set in `phase=reuse_check`, value `existing`) or `None` (cold start). `instance: Any = None` is already initialized at line ~166 of the smoke before the `try:` block, so `instance=instance` in the `generate()` call is valid Python whether we hit cold or warm.

**Cold-path pod handle recovery (3-line addition after `generate()` returns).** On the cold path, the orchestrator creates a pod with our supplied `_TAG_KEY=_TAG_VALUE` tag but we have no local handle to it (orchestrator does not surface the new instance to `generate()`'s caller). Without a `pod_id`, the `phase=destroy` + `phase=cleanup_finally` blocks no-op and the cold-start pod leaks (selfterm + small `idle_timeout_s` bound the cost, but it is real). Recover the handle via tag-discovery AFTER `generate()` returns:

```python
        if instance is None:
            # Cold path: orchestrator created an (untagged-by-it, but tagged-by-our-kwarg)
            # pod we have no handle to. Tag-discover it for the destroy block.
            instance = provider.find_instance_by_tag(_TAG_KEY, _TAG_VALUE)
            if instance is not None:
                pod_id = instance.id
                _log.info(
                    "[phase=generate] cold-path pod recovered via tag: %s", pod_id
                )
            else:
                _log.warning(
                    "[phase=generate] cold-path pod not found by tag — destroy "
                    "block will no-op; selfterm + idle_timeout_s will clean up"
                )
```

Place this block immediately after the `artifact = generate(...)` return and before the existing artifact-validation assertions.

Verify the `phase=destroy` block at ~lines 329–339 already handles `pod_id is None` correctly (`if pod_id is not None: provider.destroy_instance(pod_id)`).

- [ ] **Step 3: Verify collection + lint + typecheck**

```bash
pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v
pixi run lint
pixi run typecheck
```

Expected: live tests collected-and-skipped because env vars unset; lint/typecheck clean.

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/live/test_comfyui_wan_live.py
git add tests/live/test_comfyui_wan_live.py
git commit -m "$(cat <<'EOF'
refactor(test/live): warm-reuse via orchestrator instance + tags kwargs (Layer P Task 7 item #2)

Deletes the manual `if not warm:` block (find_offers + create_instance
loop + poll_ready) — orchestrator.generate now handles all three via
_provision_instance_and_build_backend on the cold path (with item #1
offer-retry). On warm path, passes the discovered instance via the new
instance= kwarg so generate() skips its own create.

tags={mode, _TAG_KEY: _TAG_VALUE, kinoforge.git_sha} now flows into the
orchestrator-built InstanceSpec on the cold path, so iteration #2's
find_instance_by_tag(...) call can rediscover the pod. Without this,
KEEP_POD=1 would leave an untagged pod alive on first iteration and the
warm-reuse loop would never engage.

Smoke control flow otherwise unchanged: KEEP_POD destroy-skip + finally
cleanup + fixture flush all preserved.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Closure (post-execution, not a TDD task)

After Tasks 1–3 commit clean and the full offline gate is green:

```bash
pixi run pytest           # all suites
pixi run typecheck
pixi run lint
pixi run pre-commit run --all-files
```

Expected: ~855 passed + 1 skipped (~846 → ~855: +9 net new tests).

Then:

1. Update `docs/superpowers/plans/2026-06-01-layer-p-task7-item2-warm-reuse.md.tasks.json` to mark all 3 tasks `completed`.
2. Append a closure snapshot to `PROGRESS.md` under the Layer P Single-next-action block (mirror the item #1 closure snapshot at line 156 of PROGRESS).
3. Commit the doc updates as `docs(progress): Layer P Task 7 item #2 — closure snapshot`.

Once committed, Task 7 item #3 (workflow API-JSON conversion via warm pod) becomes feasible because the smoke now has a usable warm-reuse loop.

---

## Test count projection

| Stage | Tests |
|---|---|
| Pre-item-#2 (post-item-#1 closure, HEAD `dfb6216`) | 846 |
| After Task 1 (orchestrator +8) | 854 |
| After Task 2 (batch +1) | 855 |
| After Task 3 (smoke rewire — 0 net offline) | 855 |
