# Layer L — `kinoforge batch` CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kinoforge batch -c CONFIG --manifest MANIFEST` as a sibling CLI subcommand that runs N entries on one shared deployed instance, with continue-on-error semantics and a machine-readable `_batch_summary.json`.

**Architecture:** Extract steps 1–4, 7, 8 of `orchestrator.generate()` into a new `deploy_session` context manager that yields `(backend, profile, pool, instance, engine, provider)`. Rewrite `generate()` on top of it (zero behavior change — all 708 existing tests must still pass). Add `core/batch.py` with pydantic manifest models + `batch_generate()` that wraps `deploy_session`, fans entries out via `ThreadPoolExecutor`, collects via `as_completed`, swallows per-entry exceptions, re-raises batch-fatal (`BudgetExceeded` / `CapabilityMismatch` / `TeardownError`). Write `_batch_summary.json` in a `finally` clause so every exit path leaves a parseable record. Wire it through `cli.py` as a new subcommand.

**Tech Stack:** Python 3.11+, pydantic v2 for manifest, pytest for tests, stdlib `concurrent.futures.ThreadPoolExecutor` + `as_completed` for entry fan-out, stdlib `datetime` for local-timezone batch_id, PyYAML for manifest load. No new runtime deps.

**Spec:** `docs/superpowers/specs/2026-05-31-layer-l-batch-cli-design.md`

---

## File map

**Create:**
- `src/kinoforge/core/batch.py` — `BatchEntry`, `BatchManifest`, `load_manifest`, `BatchOutcome`, `BatchResult`, `batch_generate` (Tasks 2 + 3).
- `tests/core/test_deploy_session.py` — 6 tests pinning `deploy_session` contract (Task 1).
- `tests/core/test_batch_manifest.py` — 8 tests pinning manifest validation + `load_manifest` (Task 2).
- `tests/core/test_batch_generate.py` — 10 tests pinning batch dispatch + summary write (Task 3).
- `tests/test_batch_cli.py` — 5 tests pinning CLI wiring (Task 4).
- `examples/configs/batch-prompts.yaml` — example manifest (Task 5).
- `examples/configs/prompts/forest.txt` — sample prompt file referenced by manifest (Task 5).
- `examples/configs/prompts/dawn-flight.md` — sample prompt file referenced by manifest (Task 5).

**Modify:**
- `src/kinoforge/core/orchestrator.py` — add `deploy_session` + `DeploySession`; rewrite `generate()` body on top of it (Task 1).
- `tests/core/test_orchestrator.py` — verify all existing tests still pass; no edits required (Task 1, see Verify section).
- `src/kinoforge/cli.py` — `_cmd_batch` handler + `kinoforge batch` subparser; `main()` dispatch entry (Task 4).
- `tests/test_examples.py` — 2 new tests for manifest example load (Task 5).
- `README.md` — new "Batch generation" section (Task 5).
- `PROGRESS.md` — Phase 22 entry; close Layer L candidate #3 (Task 5).

**Untouched (anchors the refactor — these tests guard `deploy_session`):**
- `tests/core/test_orchestrator.py` — every existing test must pass unmodified after Task 1.
- `tests/test_core_invariant.py` — `core/batch.py` MUST NOT trigger an adapter import (subprocess isolation test catches this).

---

### Task 1: Extract `deploy_session` context manager

**Goal:** Factor steps 1–4, 7, 8 of `orchestrator.generate()` into a reusable `deploy_session(cfg, ..., run_id=None)` context manager that yields a `DeploySession` dataclass. Rewrite `generate()` on top of it. **Zero behavioral change** — all 708 existing tests pass unmodified.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` — add `DeploySession` dataclass + `deploy_session` function near top; rewrite `generate()` body (lines 342–625) to use it.
- Test: `tests/core/test_deploy_session.py` — create with 6 tests.

**Acceptance Criteria:**
- [ ] `tests/core/test_orchestrator.py` — all existing tests pass without modification.
- [ ] `with deploy_session(cfg, store=store, run_id="r") as s` yields a `DeploySession` with `s.backend` non-None, `s.profile` a `ModelProfile`, `s.pool` an open `ConcurrentPool`, `s.engine` the resolved `GenerationEngine`.
- [ ] On compute path, `s.instance` is the created `Instance`; on hosted path (`requires_compute=False`), `s.instance is None` and `provider.create_instance` is never called.
- [ ] On clean `__exit__`, `pool.close()` is called exactly once; `provider.destroy_instance` is NOT called.
- [ ] On `CapabilityMismatch` raised by `verify` inside the `__enter__` body, instance is destroyed before the exception re-raises.
- [ ] On any exception raised inside the `with` block, `__exit__` still calls `pool.close()` (idempotent) but does NOT destroy the instance.
- [ ] Profile cache hit + verify path: `discover` not called; backend constructed exactly once.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_deploy_session.py` passes (ruff/format/mypy).

**Verify:** `pixi run pytest tests/core/ tests/test_orchestrator_iface.py tests/pipeline/ -v` → existing tests still pass + 6 new `test_deploy_session` tests pass. Total test count grows by 6.

**Steps:**

- [ ] **Step 1: Read current `orchestrator.generate()` end-to-end** to map every step to its new home.

```
Steps 1 (key) + 2 (resolve engine/provider) + 2.5 (hosted preflight) + 3 (profile_provider) + 4 (resolve profile, discover, instance create, backend build) + 7 (backend on cache hit) + 8 (verify with teardown) → MOVE TO deploy_session
Step 5 (validate_request) → STAYS IN generate()
Step 6 (splitter) → STAYS IN generate()
Step 9 (Pool wrap, stage.run, ValidationError teardown) → STAYS IN generate(), but the ConcurrentPool is now owned by deploy_session — generate uses session.pool
```

- [ ] **Step 2: Write failing test `tests/core/test_deploy_session.py`** with the 6 ACs. Use the same FakeEngine/FakeProvider/FakeProfileProvider patterns as existing `test_orchestrator.py`.

```python
"""Tests for orchestrator.deploy_session — the shared compute setup.

deploy_session is the refactored extraction of steps 1-4, 7, 8 of generate().
These tests pin its contract; the existing test_orchestrator.py suite anchors
the no-behavior-change guarantee for generate() itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.errors import CapabilityMismatch
from kinoforge.core.interfaces import (
    Artifact,
    GenerationJob,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.orchestrator import deploy_session, DeploySession
from kinoforge.core.pool import ConcurrentPool
from kinoforge.stores.local import LocalArtifactStore

# Reuse the test scaffolding patterns from test_orchestrator.py.
from tests.core.test_orchestrator import (
    _make_cfg,
    _make_hosted_cfg,
    _SpyFakeEngine,
    _SpyFakeProvider,
    _FakeProfileProvider,
    _stub_profile,
)


def test_deploy_session_yields_pool_backend_profile(tmp_path: Path) -> None:
    """A clean entry to deploy_session must yield a usable session.

    Bug catch: refactor that drops the `pool.add(backend, ...)` line — every
    batch invocation would fail to dispatch with "ConcurrentPool has no
    registered backend".
    """
    cfg = _make_cfg()
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
        run_id="r",
    ) as session:
        assert isinstance(session, DeploySession)
        assert isinstance(session.pool, ConcurrentPool)
        assert session.backend is not None
        assert session.profile is not None
        assert session.engine is engine


def test_deploy_session_does_not_destroy_instance_on_clean_exit(
    tmp_path: Path,
) -> None:
    """Clean exit must leave the instance running for warm reuse.

    Bug catch: refactor that calls provider.destroy_instance in __exit__ —
    every batch would teardown the pod after one run, defeating the whole
    point of batch's shared deploy.
    """
    cfg = _make_cfg()
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
        run_id="r",
    ) as session:
        assert session.instance is not None

    assert provider.destroy_instance_calls == []


def test_deploy_session_closes_pool_on_exit_even_when_body_raises(
    tmp_path: Path,
) -> None:
    """Pool must be closed even when the body raises.

    Bug catch: missing finally / __exit__ wiring leaks the ThreadPoolExecutor
    forever — every failing test eats a thread pool.
    """
    cfg = _make_cfg()
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())
    store = LocalArtifactStore(tmp_path)

    pool_ref: list[ConcurrentPool] = []
    with pytest.raises(RuntimeError, match="boom"):
        with deploy_session(
            cfg,
            store=store,
            engine=engine,
            provider=provider,
            profile_provider=profile_provider,
            run_id="r",
        ) as session:
            pool_ref.append(session.pool)
            raise RuntimeError("boom")

    # Re-using a closed pool raises; that's our observable.
    with pytest.raises(RuntimeError, match="pool closed"):
        pool_ref[0].submit(  # type: ignore[arg-type]
            GenerationJob(spec={}, params={}, segments=[])
        )


def test_deploy_session_teardown_on_capability_mismatch_during_enter(
    tmp_path: Path,
) -> None:
    """Verify failure during __enter__ must destroy compute before re-raise.

    Bug catch: refactor that loses the verify-fail teardown branch — a
    capability-mismatched pod stays alive and bills.
    """
    cfg = _make_cfg()
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()

    class _MismatchProfileProvider(_FakeProfileProvider):
        def verify(self, profile: Any, backend: Any, *, engine: Any, key: Any) -> None:
            raise CapabilityMismatch("forced for test")

    profile_provider = _MismatchProfileProvider(profile=_stub_profile())
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(CapabilityMismatch):
        with deploy_session(
            cfg,
            store=store,
            engine=engine,
            provider=provider,
            profile_provider=profile_provider,
            run_id="r",
        ):
            pytest.fail("body should not run — __enter__ raised")

    assert len(provider.destroy_instance_calls) == 1


def test_deploy_session_hosted_path_skips_instance(tmp_path: Path) -> None:
    """Hosted engine (requires_compute=False) yields session with instance=None.

    Bug catch: refactor that always creates an instance — hosted batches
    silently provision pods they don't need.
    """
    cfg = _make_hosted_cfg()
    engine = _SpyFakeEngine(requires_compute=False)
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
        run_id="r",
    ) as session:
        assert session.instance is None
        assert session.backend is not None

    assert provider.create_instance_calls == []


def test_deploy_session_profile_cache_hit_skips_discover(tmp_path: Path) -> None:
    """Profile cache hit must not call discover; backend built exactly once.

    Bug catch: refactor that always calls discover wastes a probe pass on
    every batch.
    """
    cfg = _make_cfg()
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
        run_id="r",
    ) as session:
        assert session.backend is not None

    assert profile_provider.discover_calls == 0
    assert engine.backend_calls == 1
```

- [ ] **Step 3: Run new tests; confirm RED.**

Run: `pixi run pytest tests/core/test_deploy_session.py -v`
Expected: `ImportError: cannot import name 'deploy_session' from 'kinoforge.core.orchestrator'` (function does not exist yet).

- [ ] **Step 4: Add `DeploySession` + `deploy_session` to orchestrator.py.** Insert after `_provision_compute_once` and before `def deploy`. The body is a near-verbatim copy of generate()'s steps 1–4, 7, 8 wrapped in `@contextmanager`.

In `src/kinoforge/core/orchestrator.py`, at the top of the imports section (after line 22 — `from pathlib import Path`), add:

```python
from contextlib import contextmanager
from collections.abc import Iterator
```

Then, just before `def deploy(...)` (around line 220), insert:

```python
@dataclass
class DeploySession:
    """Shared compute state yielded by :func:`deploy_session`.

    Holds every reference a generate-style call needs: the live backend
    that talks to the engine, the resolved ``ModelProfile``, an open
    ``ConcurrentPool`` already wired to the backend, the compute
    ``Instance`` (``None`` on hosted), and the resolved engine + provider.

    Lifetime: bounded by the ``with deploy_session(...) as s:`` block.
    On clean exit the pool is closed but the instance is left alive
    for warm reuse — destruction is the sweeper / budget tracker's job,
    matching the behavior of the pre-refactor ``generate()``.
    """

    backend: GenerationBackend
    profile: "ModelProfile"
    pool: ConcurrentPool
    instance: Instance | None
    engine: GenerationEngine
    provider: ComputeProvider | None


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
) -> Iterator[DeploySession]:
    """Yield a ready-to-dispatch ``DeploySession`` for one or more generation calls.

    Extracts steps 1 through 4 (resolve key, engine, provider, hosted
    preflight, profile cache + discover-or-instance), 7 (backend on
    cache hit), and 8 (verify with capability-mismatch teardown) from
    :func:`generate`.  ``generate`` and :func:`batch_generate` both
    consume the yielded session; per-request work (validate, split,
    stage.run) lives at the call site.

    Args:
        cfg: The loaded kinoforge configuration.
        store: ArtifactStore for the profile cache + any per-call outputs.
        provider: Optional ``ComputeProvider`` (test injection).
        engine: Optional ``GenerationEngine`` (test injection).
        creds: Optional credential provider, forwarded to the provisioner.
        profile_provider: Optional ``ModelProfileProvider`` (defaults to
            ``JsonProfileCache(store)``).
        run_id: Namespace passed to ``InstanceSpec`` (used in pod tags).
        state_dir: Root for kinoforge state (provision markers, locks).

    Yields:
        A live ``DeploySession``.  ``session.pool`` is open and contains
        one slot wrapping ``session.backend``.

    Raises:
        CapacityError: No compute offer satisfies hardware requirements.
        AuthError: Hosted preflight credential / health probe failed.
        CapabilityMismatch: Profile verify drift — instance is destroyed
            before this propagates.
    """
    # The body below is the verbatim setup logic that used to live inline
    # in generate().  Steps 5 (validate_request) and 6 (splitter) and 9
    # (stage.run) stay at the call site because they are per-request.
    key = cfg.capability_key()
    cfg_dict = _cfg_dict(cfg)

    resolved_engine = _resolve_engine(cfg, engine)
    resolved_provider: ComputeProvider | None = None
    if resolved_engine.requires_compute:
        resolved_provider = _resolve_provider(cfg, provider)

    # Hosted preflight (Layer I).
    if not resolved_engine.requires_compute:
        resolved_engine.provision(None, cfg_dict)

    if profile_provider is None:
        profile_provider = JsonProfileCache(store)

    from kinoforge.core.errors import ProfileNotCached

    backend: GenerationBackend | None = None
    instance: Instance | None = None
    _just_discovered: bool = False

    try:
        profile = profile_provider.resolve(key)
        _log.debug("profile cache hit for key %s", key.derive()[:12])
    except ProfileNotCached:
        _log.debug(
            "profile cache miss for key %s — running discover", key.derive()[:12]
        )
        if resolved_engine.requires_compute:
            if resolved_provider is None:
                raise CapacityError(
                    "requires_compute is True but no provider was resolved"
                ) from None
            hw_reqs = cfg.hardware_requirements()
            offers = resolved_provider.find_offers(hw_reqs)
            if not offers:
                raise CapacityError(
                    f"no offers available for discovery from provider "
                    f"{getattr(resolved_provider, 'name', repr(resolved_provider))!r}"
                ) from None
            lifecycle = cfg.lifecycle()
            image = cfg.compute.image if cfg.compute is not None else ""
            key_hash = _key_hash(key)
            spec = InstanceSpec(
                image=image,
                offer=offers[0],
                lifecycle=lifecycle,
                tags={
                    "kinoforge_engine": resolved_engine.name,
                    "kinoforge_key": key_hash,
                },
                env={},
                run_id=run_id,
            )
            instance = resolved_provider.create_instance(spec)
            while instance.status != "ready":
                instance = resolved_provider.get_instance(instance.id)
            _provision_compute_once(
                engine=resolved_engine,
                cfg=cfg,
                instance=instance,
                creds=creds,
                store=store,
                state_dir=state_dir,
                capability_key_hex=key.derive(),
            )
            backend = resolved_engine.backend(instance, cfg_dict)
        else:
            backend = resolved_engine.backend(None, cfg_dict)

        profile = profile_provider.discover(key, resolved_engine, backend)
        _just_discovered = True

    # Step 7 — ensure we have a backend (cache-hit branch).
    if backend is None:
        if resolved_engine.requires_compute:
            if resolved_provider is None:
                raise CapacityError(
                    "requires_compute is True but no provider was resolved"
                )
            hw_reqs = cfg.hardware_requirements()
            offers = resolved_provider.find_offers(hw_reqs)
            if not offers:
                raise CapacityError(
                    f"no offers available from provider "
                    f"{getattr(resolved_provider, 'name', repr(resolved_provider))!r}"
                )
            lifecycle = cfg.lifecycle()
            image = cfg.compute.image if cfg.compute is not None else ""
            key_hash = _key_hash(key)
            spec = InstanceSpec(
                image=image,
                offer=offers[0],
                lifecycle=lifecycle,
                tags={
                    "kinoforge_engine": resolved_engine.name,
                    "kinoforge_key": key_hash,
                },
                env={},
                run_id=run_id,
            )
            instance = resolved_provider.create_instance(spec)
            while instance.status != "ready":
                instance = resolved_provider.get_instance(instance.id)
            _provision_compute_once(
                engine=resolved_engine,
                cfg=cfg,
                instance=instance,
                creds=creds,
                store=store,
                state_dir=state_dir,
                capability_key_hex=key.derive(),
            )
            backend = resolved_engine.backend(instance, cfg_dict)
        else:
            backend = resolved_engine.backend(None, cfg_dict)

    # Step 8 — verify (skip when just-discovered).
    if not _just_discovered:
        try:
            profile_provider.verify(profile, backend, engine=resolved_engine, key=key)
        except CapabilityMismatch:
            _log.warning(
                "capability mismatch detected; tearing down instance before re-raising"
            )
            if instance is not None and resolved_provider is not None:
                resolved_provider.destroy_instance(instance.id)
            raise

    # Build the shared pool + yield.
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=cfg.lifecycle().max_in_flight)
    session = DeploySession(
        backend=backend,
        profile=profile,
        pool=pool,
        instance=instance,
        engine=resolved_engine,
        provider=resolved_provider,
    )
    try:
        yield session
    finally:
        pool.close()
```

- [ ] **Step 5: Rewrite `generate()` body to call `deploy_session`.** Replace the body from line 403 (after the docstring) through line 625 (return).

The new body:

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
) -> Artifact:
    """Run the full generation pipeline for a single clip.

    (docstring unchanged from the pre-refactor — every step still happens
    in the same order, just split across deploy_session.__enter__ and
    this function's body.)
    """
    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        creds=creds,
        profile_provider=profile_provider,
        run_id=run_id,
        state_dir=state_dir,
    ) as session:
        # Step 5 — validate the request against the profile.
        accepted_kinds: set[str]
        if hasattr(session.engine, "accepted_kinds"):
            accepted_kinds = session.engine.accepted_kinds
        else:
            accepted_kinds = {"image"}

        from kinoforge.core.validation import validate_request

        validated = validate_request(
            session.profile, request, accepted_kinds=accepted_kinds
        )

        # Step 6 — split the validated prompt into ordered segments.
        splitter = registry.get_splitter(cfg.splitter.kind)()
        prompt_segments = splitter.split(validated.prompt, session.profile, {})

        # Attach assets to segment 0 only.
        if prompt_segments and validated.assets:
            prompt_segments[0] = dataclasses.replace(
                prompt_segments[0], assets=list(validated.assets)
            )

        # Step 9 — run the pipeline stage.
        stage = GenerateClipStage(
            profile=session.profile,
            pool=session.pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params=dict(cfg.params),
            base_spec=dict(cfg.spec),
            engine=session.engine,
        )
        try:
            artifact = stage.run(request, segments_override=prompt_segments)
        except ValidationError:
            _log.warning(
                "spec validation failed; tearing down instance before re-raising"
            )
            if session.instance is not None and session.provider is not None:
                session.provider.destroy_instance(session.instance.id)
            raise
        _log.info("generate completed — artifact uri=%r", artifact.uri)
        return artifact
```

- [ ] **Step 6: Run existing orchestrator + pipeline tests; confirm GREEN.**

Run: `pixi run pytest tests/core/test_orchestrator.py tests/pipeline/ -v`
Expected: every existing test passes (no test modifications). If any fail, the refactor diverged from generate()'s original behavior — debug before continuing.

- [ ] **Step 7: Run new `test_deploy_session` tests; confirm GREEN.**

Run: `pixi run pytest tests/core/test_deploy_session.py -v`
Expected: 6 tests pass.

- [ ] **Step 8: Full suite gate.**

Run: `pixi run pytest -q`
Expected: 708 → 714 tests pass (708 + 6 new), 1 skipped. Anything regressed = refactor bug.

- [ ] **Step 9: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_deploy_session.py
git add src/kinoforge/core/orchestrator.py tests/core/test_deploy_session.py
git commit -m "$(cat <<'EOF'
refactor(core): extract deploy_session context from generate() (Layer L Task 1)

Steps 1-4, 7, 8 of orchestrator.generate() now live in a reusable
deploy_session(cfg, ...) context manager that yields a DeploySession
dataclass holding backend, profile, pool, instance, engine, provider.

generate() is rewritten on top of deploy_session with zero behavioral
change — all 708 existing tests pass unmodified. The new context is the
shared seam batch_generate() (next task) will consume.
EOF
)"
```

---

### Task 2: Manifest data layer (`core/batch.py`)

**Goal:** Define `BatchEntry`, `BatchManifest` pydantic models + `load_manifest` function + `BatchOutcome`, `BatchResult` dataclasses. Pure data layer, no compute.

**Files:**
- Create: `src/kinoforge/core/batch.py`
- Test: `tests/core/test_batch_manifest.py`

**Acceptance Criteria:**
- [ ] `load_manifest(yaml_path)` parses a 3-entry YAML list into a `BatchManifest` with 3 `BatchEntry`s.
- [ ] An entry with both `prompt` AND `prompt_file` set → `pydantic.ValidationError` from the `model_validator` with message containing `"exactly one of `prompt` / `prompt_file`"`.
- [ ] An entry with neither set → same `ValidationError`.
- [ ] `prompt_file` paths are resolved relative to the manifest file's parent directory, not the CWD.
- [ ] Missing `prompt_file` → `ConfigError` (not `FileNotFoundError`), with the resolved path in the message.
- [ ] `prompt_file` content is `.strip()`-ed of trailing whitespace; internal newlines preserved.
- [ ] Explicit duplicate `run_id` in the manifest → `pydantic.ValidationError` listing the duplicates.
- [ ] `extra="forbid"`: an unknown per-entry key (e.g. `engine: foo`) → `ValidationError`.
- [ ] After `load_manifest`, every entry has `prompt is not None`, `prompt_file is None`, and `run_id is not None` (auto-indexed when absent).
- [ ] `pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch_manifest.py` passes.

**Verify:** `pixi run pytest tests/core/test_batch_manifest.py -v` → 8 tests pass.

**Steps:**

- [ ] **Step 1: Write the failing test file.**

```python
"""Tests for kinoforge.core.batch — manifest schema + loader (Layer L Task 2)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.batch import (
    BatchEntry,
    BatchManifest,
    load_manifest,
)
from kinoforge.core.errors import ConfigError


def _write_yaml(path: Path, data: list[dict]) -> Path:
    """Dump *data* to *path* as YAML; return *path* for chaining."""
    path.write_text(yaml.safe_dump(data))
    return path


def test_load_manifest_round_trip_three_entries(tmp_path: Path) -> None:
    """A 3-entry YAML list must produce a 3-entry BatchManifest.

    Bug catch: a parser that silently drops entries (e.g. a generator
    that exhausts early) ships an under-counted batch with no error.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [
            {"prompt": "a", "mode": "t2v", "run_id": "x"},
            {"prompt": "b", "mode": "t2v", "run_id": "y"},
            {"prompt": "c", "mode": "t2v", "run_id": "z"},
        ],
    )
    m = load_manifest(path)
    assert len(m.entries) == 3
    assert [e.run_id for e in m.entries] == ["x", "y", "z"]
    assert [e.prompt for e in m.entries] == ["a", "b", "c"]


def test_entry_with_both_prompt_and_prompt_file_raises(tmp_path: Path) -> None:
    """An entry with both prompt and prompt_file is ambiguous — must reject.

    Bug catch: ambiguity in the input shape silently picks one (whichever
    pydantic sees first) and discards the other — user thinks they wrote
    one prompt, the engine sees a different one.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt": "a", "prompt_file": "x.txt", "mode": "t2v"}],
    )
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "exactly one of `prompt` / `prompt_file`" in str(exc_info.value)


def test_entry_with_neither_prompt_nor_prompt_file_raises(tmp_path: Path) -> None:
    """An entry with neither source — must reject.

    Bug catch: empty entry silently produces an empty prompt that fails
    downstream in the engine with a confusing message.
    """
    path = _write_yaml(tmp_path / "m.yaml", [{"mode": "t2v"}])
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "exactly one of `prompt` / `prompt_file`" in str(exc_info.value)


def test_prompt_file_resolves_relative_to_manifest_dir(tmp_path: Path) -> None:
    """prompt_file paths are resolved against the manifest's parent dir.

    Bug catch: resolving against CWD breaks any invocation where the user
    runs `kinoforge batch` from a directory other than the one containing
    the manifest — the silent footgun is wide.
    """
    sub = tmp_path / "configs"
    sub.mkdir()
    prompt_path = sub / "forest.txt"
    prompt_path.write_text("forest at dawn")
    manifest_path = _write_yaml(
        sub / "m.yaml",
        [{"prompt_file": "forest.txt", "mode": "t2v", "run_id": "f"}],
    )
    m = load_manifest(manifest_path)
    assert m.entries[0].prompt == "forest at dawn"
    assert m.entries[0].prompt_file is None  # collapsed to inline


def test_missing_prompt_file_raises_config_error_with_path(tmp_path: Path) -> None:
    """A missing prompt_file must produce ConfigError naming the path.

    Bug catch: a bare FileNotFoundError leaves the user grepping for which
    entry was bad. We need both the resolved path and the entry's mode in
    the message.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt_file": "nope.txt", "mode": "t2v", "run_id": "f"}],
    )
    with pytest.raises(ConfigError) as exc_info:
        load_manifest(path)
    assert "nope.txt" in str(exc_info.value)
    assert "t2v" in str(exc_info.value)


def test_prompt_file_strips_trailing_whitespace(tmp_path: Path) -> None:
    """Trailing newlines on prompt_file content must be stripped.

    Bug catch: a literal trailing newline poisons engines that validate
    prompt length or hash the prompt for caching — silent retries.
    """
    (tmp_path / "p.txt").write_text("hello world\n\n")
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt_file": "p.txt", "mode": "t2v", "run_id": "p"}],
    )
    m = load_manifest(path)
    assert m.entries[0].prompt == "hello world"


def test_duplicate_explicit_run_ids_raise(tmp_path: Path) -> None:
    """Two entries with the same explicit run_id — must reject.

    Bug catch: silent overlap in the store namespace; second artifact
    overwrites the first.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [
            {"prompt": "a", "mode": "t2v", "run_id": "same"},
            {"prompt": "b", "mode": "t2v", "run_id": "same"},
        ],
    )
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "same" in str(exc_info.value)
    assert "duplicate run_id" in str(exc_info.value)


def test_unknown_per_entry_key_raises_via_extra_forbid(tmp_path: Path) -> None:
    """Per-entry `engine: foo` (unsupported override) — must reject.

    Bug catch: silently accepted per-entry engine override breaks the
    shared-deploy assumption — batch ships wrong-engine artifacts.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt": "a", "mode": "t2v", "engine": "foo"}],
    )
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "engine" in str(exc_info.value).lower()
```

- [ ] **Step 2: Run new tests; confirm RED.**

Run: `pixi run pytest tests/core/test_batch_manifest.py -v`
Expected: `ImportError: cannot import name 'BatchEntry' from 'kinoforge.core.batch'`.

- [ ] **Step 3: Create `src/kinoforge/core/batch.py`** with the data layer:

```python
"""Batch generation: manifest schema + dispatch (Layer L).

This module owns:
  * BatchEntry / BatchManifest pydantic models with strict validation.
  * load_manifest() — reads YAML, resolves prompt_file paths, auto-indexes
    run_ids, returns a fully validated BatchManifest.
  * BatchOutcome / BatchResult dataclasses.
  * batch_generate() — the orchestration entry point (added in Task 3).

Core-import-ban: this module imports ONLY from kinoforge.core.* + stdlib
+ pydantic + PyYAML.  No kinoforge.providers / engines / sources.  The
invariant test in tests/test_core_invariant.py enforces this via
subprocess isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kinoforge.core.errors import ConfigError


class BatchEntry(BaseModel):
    """One entry in a batch manifest.

    Attributes:
        prompt: Inline prompt text. Mutually exclusive with prompt_file.
        prompt_file: Path to a text file (resolved relative to the
            manifest's parent dir). Mutually exclusive with prompt.
            After load_manifest runs, this is always None — the loader
            collapses prompt_file into prompt.
        mode: Generation mode (t2v / i2v / flf2v). Required per entry —
            no inherited default. An explicit per-entry choice avoids
            silent mode mixups.
        run_id: Sub-namespace under the batch_id for this entry's
            artifacts. None means "let the loader auto-index by position".
            After load_manifest runs, this is always set.
        params: Engine-neutral param overrides shallow-merged onto
            cfg.params (entry wins per key).
        spec: Engine-interpreted spec overrides shallow-merged onto
            cfg.spec (entry wins per key).
        assets: List of asset dicts forwarded into GenerationRequest.assets.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    prompt_file: str | None = None
    mode: str
    run_id: str | None = None
    params: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    assets: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> "BatchEntry":
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError(
                "entry must set exactly one of `prompt` / `prompt_file`"
            )
        return self


class BatchManifest(BaseModel):
    """A validated batch manifest.

    Attributes:
        entries: One or more BatchEntry objects, in submission order.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[BatchEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_run_ids(self) -> "BatchManifest":
        # When ANY entry sets run_id explicitly, ALL run_ids (including
        # the auto-derived ones added by load_manifest later) must be
        # unique.  When NONE set run_id, the loader auto-indexes "0", "1",
        # ... — collision-free by construction.
        ids = [e.run_id for e in self.entries if e.run_id is not None]
        if ids and len(set(ids)) != len(ids):
            dupes = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"duplicate run_id in manifest: {dupes}")
        return self


def load_manifest(path: Path) -> BatchManifest:
    """Load and fully validate a batch manifest YAML.

    Performs:
      1. YAML parse (top-level must be a list).
      2. pydantic validation (extra="forbid", per-entry exactly-one
         prompt source, manifest-level run_id uniqueness).
      3. prompt_file resolution against ``path.parent`` + content read
         + ``.strip()`` of trailing whitespace.
      4. Auto-indexing of any entry that didn't set run_id.

    After this returns, every entry has ``prompt is not None``,
    ``prompt_file is None``, ``run_id is not None``.

    Args:
        path: Filesystem path to the manifest YAML.

    Returns:
        A fully validated BatchManifest ready for batch_generate().

    Raises:
        ConfigError: Manifest isn't a YAML list, or a prompt_file is missing.
        pydantic.ValidationError: Schema / per-entry / manifest-level rules.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ConfigError("manifest top-level must be a YAML list of entries")
    manifest = BatchManifest(entries=raw)

    base = path.parent
    for entry in manifest.entries:
        if entry.prompt_file is not None:
            resolved = (base / entry.prompt_file).resolve()
            if not resolved.is_file():
                raise ConfigError(
                    f"prompt_file not found: {resolved} (entry mode={entry.mode!r})"
                )
            entry.prompt = resolved.read_text().strip()
            entry.prompt_file = None

    for idx, entry in enumerate(manifest.entries):
        if entry.run_id is None:
            entry.run_id = str(idx)

    return manifest


@dataclass
class BatchOutcome:
    """The result of one entry in a batch run.

    Attributes:
        run_id: The entry's run_id (always set after load_manifest).
        status: One of "ok" / "fail" / "aborted" / "interrupted".
        duration_s: Seconds the entry was in-flight (None for "aborted").
        uri: Persisted artifact URI on "ok"; None otherwise.
        error: Stringified exception on "fail" / "interrupted"; None otherwise.
    """

    run_id: str
    status: str
    duration_s: float | None = None
    uri: str | None = None
    error: str | None = None


@dataclass
class BatchResult:
    """Summary of one batch_generate() call.

    Attributes:
        batch_id: The batch namespace ID (e.g. "batch-20260531-093052").
        started_at: ISO local-tz timestamp string.
        finished_at: ISO local-tz timestamp string.
        outcomes: Ordered by entry submission order (NOT completion order).
    """

    batch_id: str
    started_at: str
    finished_at: str
    outcomes: list[BatchOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly shape written to ``_batch_summary.json``."""
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "entries": [
                {k: v for k, v in vars(o).items() if v is not None}
                for o in self.outcomes
            ],
        }
```

- [ ] **Step 4: Run new tests; confirm GREEN.**

Run: `pixi run pytest tests/core/test_batch_manifest.py -v`
Expected: 8 tests pass.

- [ ] **Step 5: Confirm core-import-ban invariant still holds.**

Run: `pixi run pytest tests/test_core_invariant.py -v`
Expected: all 3 invariant tests pass — `core/batch.py` does not trigger any adapter import.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch_manifest.py
git add src/kinoforge/core/batch.py tests/core/test_batch_manifest.py
git commit -m "$(cat <<'EOF'
feat(core): batch manifest models + load_manifest (Layer L Task 2)

BatchEntry + BatchManifest pydantic v2 models with extra="forbid",
exactly-one-of prompt/prompt_file validator, manifest-level run_id
uniqueness validator. load_manifest resolves prompt_file paths
relative to the manifest dir, strips trailing whitespace, and
auto-indexes missing run_ids.  BatchOutcome + BatchResult dataclasses
own the summary JSON shape consumed by Task 3.

8 new tests; core-import-ban invariant preserved.
EOF
)"
```

---

### Task 3: `batch_generate()` core function

**Goal:** Implement `batch_generate(cfg, manifest, *, store, batch_id, concurrent=None, ...)` that wraps `deploy_session`, fans entries out via `ThreadPoolExecutor`, collects with `as_completed`, handles per-entry exceptions, re-raises batch-fatal, writes `_batch_summary.json` in `finally`.

**Files:**
- Modify: `src/kinoforge/core/batch.py` — append `batch_generate` function + helpers.
- Test: `tests/core/test_batch_generate.py` — 10 tests.

**Acceptance Criteria:**
- [ ] 3-entry manifest on a FakeEngine+LocalProvider cfg → returns `BatchResult` with 3 `BatchOutcome`s in submission order, all `status="ok"`, each `uri` resolves to a real file under `<root>/<batch_id>/<run_id>/`.
- [ ] Entry 2 raising `AssetFetchError` → outcomes 1 and 3 are `"ok"`, outcome 2 is `"fail"`; `batch_generate` returns normally (does NOT raise).
- [ ] Entry 2 raising `BudgetExceeded` → `batch_generate` re-raises after writing partial `_batch_summary.json` to the store.
- [ ] `cfg.params={"seed": 1}` + entry override `params={"seed": 42}` → the overriding entry's stage sees `base_params == {"seed": 42}`; other entries see `base_params == {"seed": 1}`.
- [ ] No entry's `params` / `spec` mutation leaks into `cfg.params` / `cfg.spec` or into a sibling entry's stage (verified via deep equality of cfg after the call).
- [ ] `concurrent=2` with 3 entries → max 2 outstanding stage runs observed via a spy.
- [ ] Cold profile cache: `engine.inspect_capabilities` called exactly once for the whole batch (via `discover`).
- [ ] Warm profile cache (`profile_provider.resolve` succeeds): `discover` never called; `verify` called exactly once for the batch.
- [ ] Stage's `validate_request` called exactly once per entry (count == `len(manifest.entries)`).
- [ ] `_batch_summary.json` written under `<batch_id>/_batch_summary.json` on success AND on `BudgetExceeded` re-raise.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch_generate.py` passes.

**Verify:** `pixi run pytest tests/core/test_batch_generate.py -v` → 10 tests pass.

**Steps:**

- [ ] **Step 1: Write the failing test file.**

```python
"""Tests for kinoforge.core.batch.batch_generate (Layer L Task 3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.batch import (
    BatchEntry,
    BatchManifest,
    BatchResult,
    batch_generate,
)
from kinoforge.core.errors import BudgetExceeded
from kinoforge.stores.local import LocalArtifactStore

# Reuse FakeEngine + LocalProvider fixtures already shared by orchestrator tests.
from tests.core.test_orchestrator import (
    _make_cfg,
    _SpyFakeEngine,
    _SpyFakeProvider,
    _FakeProfileProvider,
    _stub_profile,
)


def _three_entry_manifest() -> BatchManifest:
    return BatchManifest(
        entries=[
            BatchEntry(prompt="a", mode="t2v", run_id="x"),
            BatchEntry(prompt="b", mode="t2v", run_id="y"),
            BatchEntry(prompt="c", mode="t2v", run_id="z"),
        ]
    )


def test_three_entries_all_ok_round_trip(tmp_path: Path) -> None:
    """3-entry batch on local-fake config → 3 ok outcomes, 3 distinct URIs.

    Bug catch: the as_completed loop swaps the outcome-to-entry mapping
    when futures finish out of order — a user-facing data corruption.
    """
    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    result: BatchResult = batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )

    assert [o.status for o in result.outcomes] == ["ok", "ok", "ok"]
    assert [o.run_id for o in result.outcomes] == ["x", "y", "z"]
    uris = [o.uri for o in result.outcomes]
    assert len(set(uris)) == 3
    for uri in uris:
        assert uri is not None and uri.startswith("file://")


def test_per_entry_failure_continues_batch(tmp_path: Path) -> None:
    """One entry raising a per-entry exception must not abort the others.

    Bug catch: a stage-level error that aborts the whole batch defeats the
    continue-on-error contract — overnight runs die on the first bad prompt.
    """
    from kinoforge.core.errors import AssetFetchError

    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    # Wire engine to fail on the "b" prompt only.
    engine.fail_on_prompt = "b"
    engine.fail_with = AssetFetchError("forced for test")
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    result = batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )

    statuses = {o.run_id: o.status for o in result.outcomes}
    assert statuses == {"x": "ok", "y": "fail", "z": "ok"}
    fail_outcome = next(o for o in result.outcomes if o.run_id == "y")
    assert fail_outcome.error is not None
    assert "AssetFetchError" in fail_outcome.error or "forced" in fail_outcome.error


def test_budget_exceeded_re_raises_after_writing_summary(tmp_path: Path) -> None:
    """BudgetExceeded mid-batch must re-raise and write partial summary.

    Bug catch: a fatal exception that aborts without persisting the
    summary leaves users with no record of what completed before the
    crash.
    """
    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    engine.fail_on_prompt = "b"
    engine.fail_with = BudgetExceeded("forced for test")
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    with pytest.raises(BudgetExceeded):
        batch_generate(
            cfg,
            _three_entry_manifest(),
            store=store,
            batch_id="b",
            engine=engine,
            provider=provider,
            profile_provider=profile_provider,
        )

    summary_path = tmp_path / "b" / "_batch_summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text())
    assert summary["batch_id"] == "b"
    entries = summary["entries"]
    statuses = {e["run_id"]: e["status"] for e in entries}
    assert "y" in statuses
    assert statuses["y"] == "interrupted"


def test_entry_param_override_isolated_to_that_entry(tmp_path: Path) -> None:
    """params override on one entry must not leak to sibling entries.

    Bug catch: a shared dict reference between entries means one user's
    seed silently propagates to every other clip in the batch.
    """
    cfg = _make_cfg()
    cfg.params = {"seed": 1}
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    manifest = BatchManifest(
        entries=[
            BatchEntry(prompt="a", mode="t2v", run_id="x"),
            BatchEntry(prompt="b", mode="t2v", run_id="y", params={"seed": 42}),
            BatchEntry(prompt="c", mode="t2v", run_id="z"),
        ]
    )

    batch_generate(
        cfg,
        manifest,
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )

    seeds_per_entry = engine.observed_base_params_per_run_id
    assert seeds_per_entry["x"] == {"seed": 1}
    assert seeds_per_entry["y"] == {"seed": 42}
    assert seeds_per_entry["z"] == {"seed": 1}
    # cfg untouched.
    assert cfg.params == {"seed": 1}


def test_entry_override_does_not_mutate_cfg_or_siblings(tmp_path: Path) -> None:
    """Stage-side mutation of base_params must not leak to cfg or siblings.

    Bug catch: shallow-copy bug where dict(cfg.params) shares nested-dict
    references — engine mutates inner dict, breaks next entry.
    """
    cfg = _make_cfg()
    cfg.params = {"nested": {"a": 1}}
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    engine.mutate_base_params = True  # spy that does base_params["nested"]["a"] = 99
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    manifest = BatchManifest(
        entries=[
            BatchEntry(prompt="a", mode="t2v", run_id="x"),
            BatchEntry(prompt="b", mode="t2v", run_id="y"),
        ]
    )
    batch_generate(
        cfg,
        manifest,
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )

    # cfg.params untouched at the outer level...
    assert cfg.params == {"nested": {"a": 1}}, (
        "batch_generate must not allow stage-side mutation to leak into cfg"
    )


def test_concurrent_caps_in_flight_stages(tmp_path: Path) -> None:
    """concurrent=2 limits in-flight stage runs to 2 at a time.

    Bug catch: an unbounded executor floods the backend with concurrent
    requests, blowing past the engine's documented cap.
    """
    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    engine.observe_in_flight = True  # tracks peak concurrency via a barrier
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    manifest = BatchManifest(
        entries=[
            BatchEntry(prompt=str(i), mode="t2v", run_id=str(i)) for i in range(3)
        ]
    )
    batch_generate(
        cfg,
        manifest,
        store=store,
        batch_id="b",
        concurrent=2,
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )

    assert engine.peak_in_flight <= 2


def test_cold_cache_discover_runs_once(tmp_path: Path) -> None:
    """Cold profile cache → one inspect_capabilities call for the whole batch.

    Bug catch: per-entry rediscovery would burn cost on every entry instead
    of amortizing the probe across the batch.
    """
    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=None)  # cold

    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )
    assert profile_provider.discover_calls == 1


def test_warm_cache_verify_runs_once(tmp_path: Path) -> None:
    """Warm profile cache → one verify for the whole batch.

    Bug catch: per-entry verify wastes probe traffic on a warm batch.
    """
    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )
    assert profile_provider.discover_calls == 0
    assert profile_provider.verify_calls == 1


def test_validate_request_runs_once_per_entry(tmp_path: Path) -> None:
    """Stage.run for each entry → exactly N validate_request calls.

    Bug catch: skipping per-entry validation lets bad mode/role/asset
    combinations dispatch to the engine, where the failure mode is
    cryptic.
    """
    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    from unittest.mock import patch

    with patch(
        "kinoforge.pipeline.generate_clip.validate_request",
        wraps=__import__("kinoforge.core.validation", fromlist=["validate_request"]).validate_request,
    ) as spy:
        batch_generate(
            cfg,
            _three_entry_manifest(),
            store=store,
            batch_id="b",
            engine=engine,
            provider=provider,
            profile_provider=profile_provider,
        )
    assert spy.call_count == 3


def test_artifacts_land_under_batch_id_slash_run_id(tmp_path: Path) -> None:
    """Each entry's artifact lives at <root>/<batch_id>/<run_id>/<name>.

    Bug catch: any flattening of the namespace lets two batches collide
    on `run_id`-only directories or makes `kinoforge gc --run <batch_id>`
    miss entries.
    """
    cfg = _make_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _SpyFakeEngine()
    provider = _SpyFakeProvider()
    profile_provider = _FakeProfileProvider(profile=_stub_profile())

    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=profile_provider,
    )

    for sub in ("x", "y", "z"):
        assert (tmp_path / "b" / sub).is_dir(), f"missing namespace {sub}"
        assert list((tmp_path / "b" / sub).iterdir()), (
            f"no artifacts in {sub}"
        )
```

> **Note on test scaffolding:** Several tests reference `_SpyFakeEngine`
> attributes (`fail_on_prompt`, `mutate_base_params`, `observe_in_flight`,
> `peak_in_flight`, `observed_base_params_per_run_id`) and
> `_FakeProfileProvider.discover_calls` / `verify_calls`. These are minor
> additions to the existing test_orchestrator.py scaffolding — extend the
> existing classes inline in `tests/core/test_orchestrator.py` (or add a
> shared `tests/core/_fakes.py` if the scaffolding feels heavy enough to
> deserve its own module). Keep the additions minimal — track only what
> the tests above assert.

- [ ] **Step 2: Extend `_SpyFakeEngine` + `_FakeProfileProvider`** in `tests/core/test_orchestrator.py` (or factor into a new `tests/core/_fakes.py` module) to support the new spy attributes. Keep existing test_orchestrator behavior unchanged — only add fields/methods.

The spy attributes that tests above need:

```python
# On _SpyFakeEngine:
#   .fail_on_prompt: str | None         — when set, submit raises fail_with on matching prompt
#   .fail_with: Exception | None
#   .observe_in_flight: bool             — track peak concurrent submits
#   .peak_in_flight: int                 — observable
#   .mutate_base_params: bool            — spy that touches base_params on submit
#   .observed_base_params_per_run_id: dict[str, dict]  — recorded per-entry base_params
#
# On _FakeProfileProvider:
#   .discover_calls: int                 — increment in discover()
#   .verify_calls: int                   — increment in verify()
```

- [ ] **Step 3: Run new tests; confirm RED.**

Run: `pixi run pytest tests/core/test_batch_generate.py -v`
Expected: `ImportError: cannot import name 'batch_generate'`.

- [ ] **Step 4: Append `batch_generate` to `src/kinoforge/core/batch.py`**:

```python
# Append below the BatchResult dataclass.

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING

from kinoforge.core.errors import (
    BudgetExceeded,
    CapabilityMismatch,
    TeardownError,
)
from kinoforge.core.logging import get_logger
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import (
        ComputeProvider,
        CredentialProvider,
        GenerationEngine,
        ModelProfileProvider,
    )

_log = get_logger(__name__)


def batch_generate(
    cfg: "Config",
    manifest: BatchManifest,
    *,
    store: ArtifactStore,
    batch_id: str,
    concurrent: int | None = None,
    provider: "ComputeProvider | None" = None,
    engine: "GenerationEngine | None" = None,
    creds: "CredentialProvider | None" = None,
    profile_provider: "ModelProfileProvider | None" = None,
    state_dir: Path = Path(".kinoforge"),
) -> BatchResult:
    """Run every entry in *manifest* on one shared deployed instance.

    Lifecycle:
      1. Open ``deploy_session(cfg, ...)`` — sets up backend, profile,
         pool, optional instance. Discover runs once on cold cache;
         verify runs once on warm cache.
      2. For each entry, build a per-entry ``GenerateClipStage`` with
         shallow-merged params/spec and submit ``stage.run`` to an outer
         ``ThreadPoolExecutor`` (sized by ``concurrent`` or
         ``cfg.lifecycle.max_in_flight``).
      3. Collect via ``as_completed``. Per-entry exceptions go into
         ``BatchOutcome(status="fail")``. Batch-fatal exceptions
         (``BudgetExceeded`` / ``CapabilityMismatch`` / ``TeardownError``)
         cancel queued futures, mark in-flight as ``"interrupted"``,
         and re-raise.
      4. In ``finally``, write ``_batch_summary.json`` to the store
         (every exit path — success, per-entry fail, batch-fatal).

    Args:
        cfg: Loaded kinoforge configuration.
        manifest: A fully validated BatchManifest (use load_manifest).
        store: Destination ArtifactStore for outputs + summary JSON.
        batch_id: Top-level namespace for this batch's artifacts.
        concurrent: Outer-executor size override. Defaults to
            cfg.lifecycle.max_in_flight.
        provider: Optional ComputeProvider (test injection).
        engine: Optional GenerationEngine (test injection).
        creds: Optional CredentialProvider (forwarded to provisioner).
        profile_provider: Optional ModelProfileProvider (test injection).
        state_dir: Operator state root.

    Returns:
        BatchResult with outcomes in submission order.

    Raises:
        BudgetExceeded, CapabilityMismatch, TeardownError:
            Batch-fatal — instance gone or budget breached. Summary JSON
            written before re-raise.
        AuthError, CapacityError, UnknownAdapter:
            Setup failure inside deploy_session.__enter__. Raised
            before any per-entry work; no summary written.
    """
    # Late imports keep core/batch.py import-light + dodge the runtime
    # cycle through orchestrator.
    from kinoforge.core.interfaces import Asset, GenerationRequest
    from kinoforge.core.orchestrator import deploy_session
    from kinoforge.pipeline.generate_clip import GenerateClipStage

    cap = concurrent if concurrent is not None else cfg.lifecycle().max_in_flight

    started_at = datetime.now().isoformat(timespec="seconds")
    outcomes_by_idx: dict[int, BatchOutcome] = {}

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        creds=creds,
        profile_provider=profile_provider,
        run_id=batch_id,
        state_dir=state_dir,
    ) as session:
        accepted_kinds: set[str]
        if hasattr(session.engine, "accepted_kinds"):
            accepted_kinds = session.engine.accepted_kinds
        else:
            accepted_kinds = {"image"}

        executor = ThreadPoolExecutor(
            max_workers=cap, thread_name_prefix=f"kinoforge-batch-{batch_id}"
        )
        future_to_idx: dict[Future[Any], int] = {}
        start_times: dict[int, float] = {}

        try:
            for idx, entry in enumerate(manifest.entries):
                merged_params = {**dict(cfg.params), **(entry.params or {})}
                merged_spec = {**dict(cfg.spec), **(entry.spec or {})}
                req_assets = [
                    Asset(**a) for a in (entry.assets or [])
                ]
                request = GenerationRequest(
                    prompt=entry.prompt,
                    mode=entry.mode,
                    assets=req_assets,
                )
                entry_run_id = f"{batch_id}/{entry.run_id}"
                stage = GenerateClipStage(
                    profile=session.profile,
                    pool=session.pool,
                    store=store,
                    run_id=entry_run_id,
                    accepted_kinds=accepted_kinds,
                    base_params=merged_params,
                    base_spec=merged_spec,
                    engine=session.engine,
                )
                start_times[idx] = monotonic()
                fut = executor.submit(stage.run, request)
                future_to_idx[fut] = idx

            try:
                for fut in as_completed(future_to_idx.keys()):
                    idx = future_to_idx[fut]
                    entry = manifest.entries[idx]
                    duration = monotonic() - start_times[idx]
                    try:
                        artifact = fut.result()
                        outcomes_by_idx[idx] = BatchOutcome(
                            run_id=entry.run_id or str(idx),
                            status="ok",
                            duration_s=duration,
                            uri=artifact.uri,
                        )
                    except (
                        BudgetExceeded,
                        CapabilityMismatch,
                        TeardownError,
                    ) as exc:
                        outcomes_by_idx[idx] = BatchOutcome(
                            run_id=entry.run_id or str(idx),
                            status="interrupted",
                            duration_s=duration,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        # Cancel everything else; mark queued as aborted,
                        # in-flight as interrupted.
                        for other_fut, other_idx in future_to_idx.items():
                            if other_idx in outcomes_by_idx:
                                continue
                            other_entry = manifest.entries[other_idx]
                            if other_fut.cancel():
                                outcomes_by_idx[other_idx] = BatchOutcome(
                                    run_id=other_entry.run_id or str(other_idx),
                                    status="aborted",
                                )
                            else:
                                other_duration = (
                                    monotonic() - start_times[other_idx]
                                )
                                outcomes_by_idx[other_idx] = BatchOutcome(
                                    run_id=other_entry.run_id or str(other_idx),
                                    status="interrupted",
                                    duration_s=other_duration,
                                )
                        raise
                    except Exception as exc:  # noqa: BLE001 — per-entry catch
                        outcomes_by_idx[idx] = BatchOutcome(
                            run_id=entry.run_id or str(idx),
                            status="fail",
                            duration_s=duration,
                            error=f"{type(exc).__name__}: {exc}",
                        )
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        finally:
            # Order outcomes by submission index; fill any unsubmitted as aborted.
            ordered = [
                outcomes_by_idx.get(i, BatchOutcome(
                    run_id=manifest.entries[i].run_id or str(i),
                    status="aborted",
                ))
                for i in range(len(manifest.entries))
            ]
            finished_at = datetime.now().isoformat(timespec="seconds")
            summary = BatchResult(
                batch_id=batch_id,
                started_at=started_at,
                finished_at=finished_at,
                outcomes=ordered,
            )
            try:
                store.put_json(batch_id, "_batch_summary.json", summary.to_dict())
            except Exception:  # noqa: BLE001
                _log.exception("failed to write _batch_summary.json")

    return summary
```

- [ ] **Step 5: Run new tests; confirm GREEN.**

Run: `pixi run pytest tests/core/test_batch_generate.py -v`
Expected: 10 tests pass.

- [ ] **Step 6: Full suite gate.**

Run: `pixi run pytest -q`
Expected: 708 + 6 (Task 1) + 8 (Task 2) + 10 (Task 3) = 732 tests pass.

- [ ] **Step 7: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch_generate.py tests/core/test_orchestrator.py
git add src/kinoforge/core/batch.py tests/core/test_batch_generate.py tests/core/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(core): batch_generate fans entries across shared deploy (Layer L Task 3)

batch_generate(cfg, manifest, ...) wraps deploy_session, builds one
GenerateClipStage per entry with shallow-merged params/spec, submits
to an outer ThreadPoolExecutor capped at cfg.lifecycle.max_in_flight,
collects via as_completed. Per-entry exceptions become BatchOutcome
status="fail" (continue-on-error). Batch-fatal exceptions (Budget,
CapabilityMismatch, Teardown) mark queued as "aborted", in-flight as
"interrupted", re-raise after writing _batch_summary.json in finally.

10 new tests; extends _SpyFakeEngine spies for per-entry isolation
+ peak-in-flight tracking + per-prompt failure injection.
EOF
)"
```

---

### Task 4: CLI subcommand `kinoforge batch`

**Goal:** Add `kinoforge batch -c CONFIG --manifest MANIFEST [--batch-id ID] [--concurrent N] [--env-file ENV]` to `cli.py`. Stream per-entry status; print final summary; exit code reflects batch outcome.

**Files:**
- Modify: `src/kinoforge/cli.py` — `kinoforge batch` subparser + `_cmd_batch` + dispatch.
- Test: `tests/test_batch_cli.py` — 5 tests.

**Acceptance Criteria:**
- [ ] `kinoforge batch -c cfg.yaml --manifest m.yaml` on local-fake cfg with 3 entries → exit 0, summary table on stdout, 3 artifacts on disk.
- [ ] Missing `--manifest` → exit 2 (argparse) with required-arg error to stderr.
- [ ] `--batch-id existing` (a non-empty namespace already exists under the store) → exit 1 with `"batch_id collision: existing"` in stderr, no compute touched.
- [ ] `--concurrent 0` → exit 1 with `"--concurrent must be a positive integer"` in stderr.
- [ ] One entry with a bogus `mode: "nope"` → exit 1, other 2 entries succeed, summary table shows the failed entry with `FAIL` and the error string.
- [ ] `pixi run pre-commit run --files src/kinoforge/cli.py tests/test_batch_cli.py` passes.

**Verify:** `pixi run pytest tests/test_batch_cli.py -v` → 5 tests pass.

**Steps:**

- [ ] **Step 1: Write the failing test file.**

```python
"""End-to-end CLI tests for `kinoforge batch` (Layer L Task 4)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from kinoforge.cli import main


def _write_local_fake_cfg(tmp_path: Path) -> Path:
    cfg = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {
                "ref": "https://example.com/fake.safetensors",
                "kind": "base",
                "target": "checkpoints",
            }
        ],
        "compute": {"provider": "local", "image": ""},
        "budget": {"max_usd": 10},
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_kinoforge_batch_happy_path(tmp_path: Path, capsys) -> None:
    """3-entry batch on local-fake cfg → exit 0, summary printed.

    Bug catch: CLI wiring drops the new subcommand from the dispatcher.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest = [
        {"prompt": "a", "mode": "t2v", "run_id": "x"},
        {"prompt": "b", "mode": "t2v", "run_id": "y"},
        {"prompt": "c", "mode": "t2v", "run_id": "z"},
    ]
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))

    state_dir = tmp_path / "state"
    rc = main([
        "--state-dir", str(state_dir),
        "batch",
        "-c", str(cfg_path),
        "--manifest", str(manifest_path),
        "--batch-id", "b",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "summary" in out.lower()
    for rid in ("x", "y", "z"):
        assert rid in out
    # Summary JSON exists.
    summary = json.loads((state_dir / "b" / "_batch_summary.json").read_text())
    assert summary["batch_id"] == "b"
    assert len(summary["entries"]) == 3


def test_missing_manifest_arg_exits_with_argparse_error(
    tmp_path: Path, capsys
) -> None:
    """argparse must demand --manifest.

    Bug catch: a default of '' silently accepts no manifest and dispatches
    against an empty list.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["batch", "-c", str(cfg_path)])
    assert exc_info.value.code == 2  # argparse standard exit code


def test_batch_id_collision_exits_one(tmp_path: Path, capsys) -> None:
    """An existing batch_id namespace must short-circuit with exit 1.

    Bug catch: silent overwrite destroys prior batch artifacts.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(yaml.safe_dump(
        [{"prompt": "a", "mode": "t2v", "run_id": "x"}]
    ))
    state_dir = tmp_path / "state"
    # Pre-create a colliding namespace with at least one file.
    (state_dir / "existing").mkdir(parents=True)
    (state_dir / "existing" / "leftover.bin").write_bytes(b"hi")

    rc = main([
        "--state-dir", str(state_dir),
        "batch",
        "-c", str(cfg_path),
        "--manifest", str(manifest_path),
        "--batch-id", "existing",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "batch_id collision" in err
    assert "existing" in err


def test_zero_concurrent_exits_one(tmp_path: Path, capsys) -> None:
    """--concurrent 0 must be rejected before any work starts.

    Bug catch: 0 passed to ThreadPoolExecutor(max_workers=0) raises a
    confusing ValueError mid-batch; we want the CLI to fail fast.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(yaml.safe_dump(
        [{"prompt": "a", "mode": "t2v", "run_id": "x"}]
    ))
    rc = main([
        "batch",
        "-c", str(cfg_path),
        "--manifest", str(manifest_path),
        "--concurrent", "0",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--concurrent" in err
    assert "positive" in err


def test_one_bad_entry_continues_others(tmp_path: Path, capsys) -> None:
    """Bogus mode on one entry must not abort the batch.

    Bug catch: any per-entry exception aborting the whole batch defeats
    the continue-on-error contract at the CLI surface.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(yaml.safe_dump([
        {"prompt": "a", "mode": "t2v", "run_id": "x"},
        {"prompt": "b", "mode": "nope", "run_id": "y"},
        {"prompt": "c", "mode": "t2v", "run_id": "z"},
    ]))
    state_dir = tmp_path / "state"
    rc = main([
        "--state-dir", str(state_dir),
        "batch",
        "-c", str(cfg_path),
        "--manifest", str(manifest_path),
        "--batch-id", "b",
    ])
    assert rc == 1
    out = capsys.readouterr().out
    assert "x" in out and "z" in out
    assert "y" in out
    # Summary JSON shows 1 fail.
    summary = json.loads((state_dir / "b" / "_batch_summary.json").read_text())
    statuses = {e["run_id"]: e["status"] for e in summary["entries"]}
    assert statuses == {"x": "ok", "y": "fail", "z": "ok"}
```

- [ ] **Step 2: Run new tests; confirm RED.**

Run: `pixi run pytest tests/test_batch_cli.py -v`
Expected: every test fails because `batch` is not a recognized subcommand.

- [ ] **Step 3: Add the `batch` subparser + `_cmd_batch` handler to `cli.py`.**

In `src/kinoforge/cli.py`, after the `p_gc` block (around line 185) inside the `_build_parser` (or wherever the subparsers are defined — look at the existing `p_generate` block as the template), add:

```python
    # batch
    p_batch = sub.add_parser("batch", help="run a batch of generation jobs")
    p_batch.add_argument("-c", "--config", required=True, metavar="PATH")
    p_batch.add_argument("--manifest", required=True, metavar="PATH")
    p_batch.add_argument("--batch-id", default=None, metavar="ID")
    p_batch.add_argument("--concurrent", type=int, default=None, metavar="N")
    p_batch.add_argument("--env-file", default=None, metavar="PATH")
```

Add a `_cmd_batch` function (place it near `_cmd_generate` for proximity):

```python
def _cmd_batch(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``batch`` subcommand.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code: 0 (all ok), 1 (one+ per-entry fail / setup error /
        collision / bad flag), 2 (batch-fatal mid-run).
    """
    from datetime import datetime

    from kinoforge.core.batch import batch_generate, load_manifest
    from kinoforge.core.config import load_config
    from kinoforge.core.errors import (
        BudgetExceeded,
        CapabilityMismatch,
        ConfigError,
        TeardownError,
    )
    from pydantic import ValidationError as PydanticValidationError

    if args.env_file is not None:
        load_env_file(Path(args.env_file))

    # Early flag validation — fail before touching compute.
    if args.concurrent is not None and args.concurrent < 1:
        print(
            f"error: --concurrent must be a positive integer (got {args.concurrent})",
            file=sys.stderr,
        )
        return 1

    try:
        cfg = load_config(Path(args.config).read_text())
    except (ConfigError, PydanticValidationError) as exc:
        print(f"error: config: {exc}", file=sys.stderr)
        return 1

    try:
        manifest = load_manifest(Path(args.manifest))
    except (ConfigError, PydanticValidationError) as exc:
        print(f"error: manifest: {exc}", file=sys.stderr)
        return 1

    store = _build_store(cfg, state_dir)

    batch_id: str = (
        args.batch_id
        if args.batch_id is not None
        else datetime.now().strftime("batch-%Y%m%d-%H%M%S")
    )

    # Same-second collision check via existing store API.
    try:
        existing = store.list(batch_id)
    except Exception:  # noqa: BLE001 — first list on empty namespace returns []
        existing = []
    if existing:
        print(
            f"error: batch_id collision: {batch_id} already has artifacts "
            f"(pass --batch-id to override)",
            file=sys.stderr,
        )
        return 1

    print(
        f"[{batch_id}] manifest loaded: {len(manifest.entries)} entries, "
        f"concurrency={args.concurrent or cfg.lifecycle().max_in_flight}"
    )

    try:
        result = batch_generate(
            cfg,
            manifest,
            store=store,
            batch_id=batch_id,
            concurrent=args.concurrent,
            state_dir=state_dir,
        )
    except (BudgetExceeded, CapabilityMismatch, TeardownError) as exc:
        print(f"[{batch_id}] batch-fatal: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    # Final summary table.
    print("\nsummary:")
    for o in result.outcomes:
        status_label = o.status.upper()
        duration = f"{o.duration_s:.1f}s" if o.duration_s is not None else "—"
        detail = o.uri if o.uri else (o.error or "")
        print(f"  {o.run_id:<20s} {status_label:<8s} {duration:<8s} {detail}")
    print(f"batch-id: {batch_id}")
    n_ok = sum(1 for o in result.outcomes if o.status == "ok")
    n_fail = len(result.outcomes) - n_ok
    print(f"results:  {n_ok}/{len(result.outcomes)} ok, {n_fail} failed")
    return 0 if n_fail == 0 else 1
```

Wire `_cmd_batch` into the `main()` dispatch table:

```python
# In main(), where other elif args.command == "..." branches live, add:
    elif args.command == "batch":
        return _cmd_batch(args, state_dir)
```

- [ ] **Step 4: Run new tests; confirm GREEN.**

Run: `pixi run pytest tests/test_batch_cli.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Full suite gate.**

Run: `pixi run pytest -q`
Expected: 732 + 5 = 737 tests pass.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/cli.py tests/test_batch_cli.py
git add src/kinoforge/cli.py tests/test_batch_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): kinoforge batch subcommand (Layer L Task 4)

`kinoforge batch -c CONFIG --manifest PATH [--batch-id ID]
[--concurrent N] [--env-file PATH]` runs every entry in the
manifest on one shared deployed instance with continue-on-error
semantics.  Default batch_id derives from datetime.now()
(local timezone) as `batch-YYYYMMDD-HHMMSS`.

Collision check via store.list(batch_id); --concurrent < 1 short-
circuits. Streaming log + final summary table on stdout; exit 0
(all ok), 1 (one+ fail / setup / collision), 2 (batch-fatal).

5 new tests.
EOF
)"
```

---

### Task 5: Examples + README + PROGRESS + full gate

**Goal:** Ship an example manifest + the two referenced prompt files, document `kinoforge batch` in the README, log Layer L into PROGRESS, gate the full test suite.

**Files:**
- Create: `examples/configs/batch-prompts.yaml`
- Create: `examples/configs/prompts/forest.txt`
- Create: `examples/configs/prompts/dawn-flight.md`
- Modify: `tests/test_examples.py` — 2 new tests pinning manifest example load.
- Modify: `README.md` — new "Batch generation" section.
- Modify: `PROGRESS.md` — Phase 22 entry; close Layer L candidate #3; update test count.

**Acceptance Criteria:**
- [ ] `examples/configs/batch-prompts.yaml` loads via `load_manifest` without error.
- [ ] Every entry's `mode` is in `{"t2v", "i2v", "flf2v"}`.
- [ ] `examples/configs/prompts/forest.txt` and `dawn-flight.md` exist and are non-empty.
- [ ] README has a new H2 or H3 "Batch generation" section with a quickstart that shows `kinoforge batch -c CONFIG --manifest MANIFEST` and links to the example.
- [ ] PROGRESS.md has a new `### Phase 22 — Layer L (kinoforge batch CLI)` section with the task SHAs, key decisions, and an updated test count.
- [ ] `pixi run pytest -q` — full suite passes; Layer L candidate #3 from PROGRESS:155 is no longer Pending.
- [ ] `pixi run pre-commit run --all-files` passes.

**Verify:** `pixi run pytest tests/test_examples.py -v && pixi run pytest -q` → 2 new example tests pass; full suite green at 737 + 2 = 739 tests.

**Steps:**

- [ ] **Step 1: Create `examples/configs/prompts/forest.txt`:**

```
A dense old-growth forest at first light. Mist coils between the trunks,
backlit by a low golden sun. Camera drifts slowly forward through
the underbrush; ferns brush the lens; a single shaft of light pierces
the canopy.
```

- [ ] **Step 2: Create `examples/configs/prompts/dawn-flight.md`:**

```
Aerial drone shot at dawn. The camera lifts off the surface of a still
lake, water beading on the lens, then climbs above a ridge line as
the first sun strikes the peaks. The horizon glows orange-pink.
Slow forward motion. Cinematic, photoreal.
```

- [ ] **Step 3: Create `examples/configs/batch-prompts.yaml`:**

```yaml
# Example batch manifest. Run with:
#   kinoforge batch -c examples/configs/wan.yaml --manifest examples/configs/batch-prompts.yaml
#
# All three entries share one deploy. Each entry's outputs land under
# .kinoforge/<batch_id>/<run_id>/. The default batch_id is
# `batch-YYYYMMDD-HHMMSS` in local timezone; pass --batch-id ID to override.

- prompt: "Waves crashing on basalt cliffs at dusk, long-exposure foam trails."
  mode: t2v
  run_id: waves

- prompt_file: prompts/forest.txt        # resolved relative to this manifest's dir
  mode: t2v
  run_id: forest
  params:
    seed: 42                              # per-entry param overrides

- prompt_file: prompts/dawn-flight.md
  mode: i2v
  run_id: dawn
  assets:
    - kind: image
      role: init_image
      ref: "file:///workspace/seed/dawn.png"
```

- [ ] **Step 4: Add 2 new tests to `tests/test_examples.py`** (append at end):

```python
from kinoforge.core.batch import load_manifest


def test_batch_prompts_example_loads() -> None:
    """examples/configs/batch-prompts.yaml must parse cleanly.

    Bug catch: an example that rots silently (missing prompt file,
    pydantic schema drift) breaks the documented quickstart.
    """
    path = Path("examples/configs/batch-prompts.yaml")
    m = load_manifest(path)
    assert len(m.entries) == 3
    for entry in m.entries:
        assert entry.prompt is not None and len(entry.prompt) > 0
        assert entry.run_id is not None
        assert entry.prompt_file is None  # collapsed to inline


def test_batch_prompts_example_uses_valid_modes() -> None:
    """Every example entry must declare a supported mode.

    Bug catch: a typo'd mode (e.g. "t2vv") would silently fail at
    request validation, not at load time.
    """
    path = Path("examples/configs/batch-prompts.yaml")
    m = load_manifest(path)
    for entry in m.entries:
        assert entry.mode in {"t2v", "i2v", "flf2v"}, (
            f"unexpected mode in example: {entry.mode!r} (run_id={entry.run_id})"
        )
```

- [ ] **Step 5: Add a "Batch generation" section to `README.md`.** Place it after the existing "Quickstart" / single-clip section. Use this text verbatim:

```markdown
## Batch generation

Render N clips on one shared deployed instance with continue-on-error
semantics:

```bash
kinoforge batch -c examples/configs/wan.yaml \
                --manifest examples/configs/batch-prompts.yaml
```

The manifest is a YAML list. Each entry sets exactly one of `prompt`
(inline text) or `prompt_file` (path resolved relative to the manifest's
parent directory). Optional per-entry overrides: `params`, `spec`,
`assets`, `run_id`.

```yaml
# examples/configs/batch-prompts.yaml
- prompt: "waves crashing on basalt cliffs at dusk"
  mode: t2v
  run_id: waves

- prompt_file: prompts/forest.txt
  mode: t2v
  run_id: forest
  params: { seed: 42 }
```

**Outputs.** Each entry's artifact lands at
`<store>/<batch_id>/<run_id>/<name>`. Default `batch_id` is
`batch-YYYYMMDD-HHMMSS` in **local timezone**; override with
`--batch-id ID` for a memorable name. A machine-readable summary
is written to `<batch_id>/_batch_summary.json` on every exit path.

**Concurrency.** `--concurrent N` overrides
`cfg.lifecycle.max_in_flight`. Both layers (outer entry executor and
ConcurrentPool slot cap) share the same value.

**Failure semantics.** Per-entry exceptions become `FAIL` outcomes;
the batch keeps going. Batch-fatal exceptions (`BudgetExceeded`,
`CapabilityMismatch`, `TeardownError`) cancel queued entries and exit
with code 2. The summary JSON is written before the exit in every case.

**Cleanup.** `kinoforge gc --run <batch_id> -c <config>` walks the
entire batch namespace at once.
```

- [ ] **Step 6: Add Phase 22 entry to `PROGRESS.md`.** Append after the existing Phase 21 entry, before "Post-MVP" or before the "GitHub issues" table. Use this template (fill in real SHAs at commit time):

```markdown
### Phase 22 — Layer L (`kinoforge batch` CLI)

- [x] Task 1: deploy_session context manager extraction — commit `<sha1>`
- [x] Task 2: core/batch.py manifest models + load_manifest — commit `<sha2>`
- [x] Task 3: batch_generate() core function — commit `<sha3>`
- [x] Task 4: kinoforge batch CLI subcommand — commit `<sha4>`
- [x] Task 5: examples + README + PROGRESS + full gate — commit `<sha5>`
- [x] Merge to main via `--no-ff` — merge commit `<merge-sha>` (closes PROGRESS:155 follow-up #3)

**Key design decisions:**
- Shared deploy across N entries (Q1=A): one create_instance, ConcurrentPool fans entries.
- YAML manifest with per-entry `prompt`/`prompt_file` (Q2=A): exactly-one-of validator.
- batch_id default `batch-YYYYMMDD-HHMMSS` in LOCAL timezone (Q3 clarification + new feedback memory).
- Continue-on-error per entry; batch-fatal on Budget/Capability/Teardown (Q4=A).
- `deploy_session` extraction (Q5=B refactor): both generate() and batch_generate() consume it; zero behavior change to generate().
- `_batch_summary.json` written in finally clause regardless of exit path.

**Test count:** 708 → 739 tests passed + 1 skipped (+31 net).
```

Also update the "Single next action" line and the Layer L candidates block to remove follow-up #3.

- [ ] **Step 7: Full suite gate.**

Run: `pixi run pytest -q`
Expected: 739 tests pass + 1 skipped (no regressions).

- [ ] **Step 8: Pre-commit all-files.**

Run: `pixi run pre-commit run --all-files`
Expected: all hooks pass on every file.

- [ ] **Step 9: Commit.**

```bash
git add examples/configs/batch-prompts.yaml examples/configs/prompts/ \
        tests/test_examples.py README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs: Layer L examples + README + PROGRESS Phase 22 entry (Task 5)

examples/configs/batch-prompts.yaml + 2 prompt files demonstrate
the inline + prompt_file shapes side-by-side.  README "Batch
generation" section covers quickstart, outputs, concurrency, failure
semantics, cleanup.  PROGRESS.md Phase 22 logs all 5 tasks, key
decisions, +31 test delta.

Closes PROGRESS:155 follow-up #3 (Layer L candidate #3).
EOF
)"
```

- [ ] **Step 10: (Optional, gated by user) Merge to main via `--no-ff`** with a substantive merge body. Match the Layer K / Layer J pattern:

```bash
git checkout main
git merge --no-ff <layer-l-branch> -m "$(cat <<'EOF'
Merge branch 'build/layer-l': kinoforge batch CLI (Layer L)

Tasks 1-5 (commits <sha1>..<sha5>) ship the batch subcommand.
Shared deploy across N entries via the new deploy_session context;
continue-on-error per entry; machine-readable _batch_summary.json on
every exit path.

Closes PROGRESS:155 follow-up #3.
EOF
)"
```

Skip this step if the user prefers the in-place merge / fast-forward / PR flow.

---

## Self-Review

**1. Spec coverage:**
- §1 Purpose/scope → Task 4 (`kinoforge batch` subcommand) + Task 3 (`batch_generate` orchestration).
- §2 Resolved decisions → embedded in tasks 2 (manifest), 3 (batch_generate), 4 (CLI).
- §3 Architecture → Task 1 (deploy_session) + Task 3 (batch_generate) + Task 4 (CLI).
- §4 Manifest schema → Task 2.
- §5 CLI surface → Task 4.
- §6 Data flow → Task 3.
- §7 Error handling / cost-safety → Task 3 (per-entry + fatal handling) + Task 4 (exit codes + collision check).
- §8 Testing → Tasks 1-5 each ship the new test file(s) the spec specifies.
- §9 Acceptance criteria → distributed across all tasks.
- §10 Out-of-scope → respected (no retry, no per-entry budget, no daemon).
- §11 Open follow-ups → preserved in PROGRESS for post-merge.
- §12 Dependencies → no new deps required; verified in Task 3 (uses stdlib `concurrent.futures`).

**2. Placeholder scan:** Searched for TBD/TODO/FIXME — none. Every step has either a code block or an exact command + expected output.

**3. Type / signature consistency:**
- `DeploySession` fields used identically across Task 1 (definition) and Task 3 (consumer).
- `BatchEntry` / `BatchManifest` / `BatchOutcome` / `BatchResult` shapes used identically across Task 2 (definition) and Tasks 3, 4 (consumers).
- `batch_generate` signature in Task 3 matches the call site in Task 4's `_cmd_batch`.
- `store.put_json(run_id, name, dict)` signature confirmed against `src/kinoforge/stores/base.py:59`.
- `cfg.lifecycle().max_in_flight` accessor confirmed at `tests/core/test_config.py` and `orchestrator.py:604` usage.

**4. Behavior parity for `generate()`:** Task 1 explicitly mandates "all 708 existing tests pass without modification" as an acceptance criterion. The refactor is a pure extraction.

**5. Known minor deviation from spec:** the spec's §6 data-flow trace shows `cfg.params` / `cfg.spec` merged into `merged_params` / `merged_spec` and handed to a constructor named `GenerateClipStage(... params=merged_params, spec=merged_spec, ...)`. The actual constructor parameter names today are `base_params` / `base_spec`. Task 3's code uses the real names. Spec wording is loose enough that this is not a contract divergence — `base_params` is the long-standing field name and there is no test or doc that requires a renamed parameter.

**6. Splitter omission for batch entries:** orchestrator.generate() calls the splitter (step 6) before stage.run; batch_generate does NOT. Each batch entry is therefore treated as exactly one segment. This is an implicit simplification embedded in the spec's §6 data flow (`fut = executor.submit(stage.run, request)` — no `segments_override`, so Stage builds 1 segment from the request prompt). Documented here so the executor doesn't get surprised. If users want per-entry multi-segment behavior, they can invoke `generate` directly. Track in PROGRESS as a possible Layer M follow-up if asked.

---

## Heads-up (suppressed)

No user-gate tasks tagged — all gates are routine engineering verification, not user-thrown gates. No banner needed.
