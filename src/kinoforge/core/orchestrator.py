"""Top-level orchestration flows: deploy() and generate().

Ties together config, registry, provisioner, profiles, validation, strategy,
pool, and pipeline stage into two public entry points.

Conventions
-----------
* This module imports ONLY from ``kinoforge.core.*``, ``kinoforge.pipeline.*``,
  and ``kinoforge.stores.base``.  It MUST NOT import from
  ``kinoforge.providers.*``, ``kinoforge.engines.*``, or
  ``kinoforge.sources.*`` — those are resolved via the registry at runtime.
* ``model_dump()`` is called on the pydantic ``Config`` before passing to
  engine methods so callers receive a plain ``dict``.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kinoforge.core import registry
from kinoforge.core.config import Config
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import (
    CapabilityMismatch,
    CapacityError,
    ProfileNotCached,
    ValidationError,
)
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ComputeProvider,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationRequest,
    Instance,
    InstanceSpec,
    ModelProfile,
    ModelProfileProvider,
)
from kinoforge.core.logging import get_logger
from kinoforge.core.pool import ConcurrentPool
from kinoforge.core.profiles import JsonProfileCache
from kinoforge.core.provision_state import (
    is_marker_current,
    marker_path,
    read_marker,
    write_marker,
)
from kinoforge.core.provisioner import provision as provisioner_provision
from kinoforge.outputs.base import OutputSink
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.stores.base import ArtifactStore

_log = get_logger("orchestrator")


# ---------------------------------------------------------------------------
# Public data structure
# ---------------------------------------------------------------------------


@dataclass
class DeployResult:
    """Result of a deploy() call.

    Attributes:
        instance: The created compute instance, or ``None`` for hosted
            engines or dry-runs.
        endpoints: A mapping of endpoint name → URL for the deployed backend.
        plan_text: Populated only on a dry-run; contains a vendor/engine-neutral
            textual plan describing what *would* have been created.
    """

    instance: Instance | None
    endpoints: dict[str, str]
    plan_text: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_engine(cfg: Config, engine: GenerationEngine | None) -> GenerationEngine:
    """Return the injected engine or resolve from the registry.

    Args:
        cfg: The loaded kinoforge config.
        engine: Optional pre-constructed engine (test injection).

    Returns:
        A ready ``GenerationEngine`` instance.
    """
    if engine is not None:
        return engine
    return registry.get_engine(cfg.engine.kind)()


def _resolve_provider(cfg: Config, provider: ComputeProvider | None) -> ComputeProvider:
    """Return the injected provider or resolve from the registry.

    Args:
        cfg: The loaded kinoforge config.  Must have a ``compute`` block.
        provider: Optional pre-constructed provider (test injection).

    Returns:
        A ready ``ComputeProvider`` instance.

    Raises:
        ValueError: ``cfg.compute`` is ``None`` (called on a hosted config).
    """
    if provider is not None:
        return provider
    if cfg.compute is None:
        raise ValueError(
            "cannot resolve provider: cfg.compute is None (hosted engine path)"
        )
    return registry.get_provider(cfg.compute.provider)()


def _cfg_dict(cfg: Config) -> dict[str, object]:
    """Serialise *cfg* to a plain dict for engine/provisioner calls.

    Args:
        cfg: The pydantic Config to dump.

    Returns:
        A plain ``dict`` (pydantic model_dump output).
    """
    return cfg.model_dump()


def _key_hash(key: CapabilityKey) -> str:
    """Return the first 12 hex chars of the derived hash for plan display.

    Args:
        key: The ``CapabilityKey`` to abbreviate.

    Returns:
        A 12-character hex string suitable for human-readable output.
    """
    return key.derive()[:12]


def _provision_compute_once(
    *,
    engine: GenerationEngine,
    cfg: Config,
    instance: Instance,
    creds: CredentialProvider | None,
    store: ArtifactStore,
    state_dir: Path,
    capability_key_hex: str,
) -> None:
    """Run ``provisioner.provision`` exactly once per ``(instance, capability_key)``.

    Layer I UX A compute-path preflight.  Uses an artifact-store lock keyed on
    the instance id to serialise concurrent ``generate()`` callers, and reads
    a per-instance marker to skip the (potentially expensive) provision when
    the same instance was already provisioned for the same capability key.

    Stale-key rule: when the user edits cfg (e.g. precision / model set) the
    derived capability_key changes and the marker becomes stale, forcing a
    re-provision that overwrites the marker.

    Args:
        engine: The resolved ``GenerationEngine`` that owns the final
            provision step.
        cfg: The loaded kinoforge ``Config``.  Forwarded to
            :func:`kinoforge.core.provisioner.provision`.
        instance: The ready compute instance to provision against.
        creds: Optional credential provider.  Defaults to
            ``EnvCredentialProvider()`` when ``None``.
        store: Artifact store providing ``acquire_lock`` for cross-process
            mutual exclusion.
        state_dir: Root state directory under which the marker is written
            (``<state_dir>/instances/<instance.id>/.provisioned``) and into
            whose ``weights/`` subdirectory downloads are placed.
        capability_key_hex: Current ``cfg.capability_key().derive()`` hex.
    """
    effective_creds: CredentialProvider = (
        creds if creds is not None else EnvCredentialProvider()
    )
    marker = marker_path(state_dir, instance.id)

    with store.acquire_lock(f"provision:{instance.id}", ttl_s=300):
        record = read_marker(marker)
        if record is not None and is_marker_current(record, capability_key_hex):
            _log.debug(
                "provision marker current for instance %s key %s — skipping",
                instance.id,
                capability_key_hex[:12],
            )
            return
        _log.info(
            "running provisioner.provision for instance %s (engine=%s key=%s)",
            instance.id,
            engine.name,
            capability_key_hex[:12],
        )
        provisioner_provision(
            engine,
            cfg,  # type: ignore[arg-type]
            instance,
            creds=effective_creds,
            download_dir=state_dir / "weights",
        )
        write_marker(
            marker,
            instance.id,
            capability_key_hex,
            engine.name,
            time.time(),
        )


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
) -> tuple[Instance, GenerationBackend]:
    """Provision a compute instance and build a backend for it.

    Shared by the cache-miss (discovery) and cache-hit (steady-state)
    branches of deploy_session.

    Args:
        resolved_engine: The resolved generation engine.
        resolved_provider: The resolved compute provider (must be non-None).
        cfg: Loaded configuration.
        run_id: Run-id tag for the instance.
        key: Capability key (used for the kinoforge_key tag).
        creds: Optional credential provider, forwarded to the provisioner.
        store: Artifact store (forwarded to the provisioner for marker reads).
        state_dir: Operator state root.
        for_discovery: When True, the CapacityError message reads
            'no offers available for discovery from provider ...' to
            distinguish the cold-start failure from a steady-state one.

    Returns:
        ``(instance, backend)`` — instance polled to ``ready``, backend
        constructed via ``engine.backend(instance, cfg_dict)``.

    Raises:
        CapacityError: ``find_offers`` returned an empty list.
    """
    hw_reqs = cfg.hardware_requirements()
    offers = resolved_provider.find_offers(hw_reqs)
    if not offers:
        prefix = "for discovery " if for_discovery else ""
        raise CapacityError(
            f"no offers available {prefix}from provider "
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
    cfg_dict = _cfg_dict(cfg)
    backend = resolved_engine.backend(instance, cfg_dict)
    return instance, backend


# ---------------------------------------------------------------------------
# deploy_session — shared compute setup yielded to generate() and batch_generate()
# ---------------------------------------------------------------------------


@dataclass
class DeploySession:
    """Shared compute state yielded by :func:`deploy_session`.

    Holds every reference a generate-style call needs: the live backend
    that talks to the engine, the resolved :class:`ModelProfile`, an
    open :class:`ConcurrentPool` already wired to the backend, the
    compute :class:`Instance` (``None`` on hosted), and the resolved
    engine + provider.

    Lifetime is bounded by the ``with deploy_session(...) as s:`` block.
    On clean exit the pool is closed but the instance is left alive for
    warm reuse — destruction is the sweeper / budget tracker's job,
    matching the behaviour of the pre-refactor :func:`generate`.

    Attributes:
        backend: The live backend wired through ``session.pool``.
        profile: The resolved ``ModelProfile`` for ``cfg.capability_key()``.
        pool: An open ``ConcurrentPool`` with ``backend`` registered at
            ``cfg.lifecycle().max_in_flight`` concurrency.
        instance: The provisioned compute ``Instance``, or ``None`` on a
            hosted engine path.
        engine: The resolved ``GenerationEngine`` (registry or injection).
        provider: The resolved ``ComputeProvider`` (registry or
            injection), or ``None`` on a hosted engine path.
    """

    backend: GenerationBackend
    profile: ModelProfile
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
    """Yield a ready-to-dispatch :class:`DeploySession` for one or more calls.

    This is the verbatim extraction of steps 1-4, 7, and 8 of the
    pre-Layer-L :func:`generate` body.  ``generate`` and
    ``batch_generate`` both consume the yielded session; per-request
    work (validate, split, stage.run) lives at the call site so the
    setup cost amortises across many entries.

    On entry the function:

    1. Derives ``cfg.capability_key()``.
    2. Resolves the engine (and the provider when ``requires_compute``).
    3. Runs the hosted preflight (``engine.provision(None, cfg_dict)``)
       on the hosted path.
    4. Defaults ``profile_provider`` to ``JsonProfileCache(store)``.
    5. Tries ``profile_provider.resolve(key)`` — on
       ``ProfileNotCached`` provisions an instance, builds the backend,
       and calls ``discover``; on cache hit defers backend construction
       to step 7.
    6. (Cache-hit only) Step 7 — creates the instance (compute path) /
       builds the backend (hosted path).
    7. (Cache-hit only) Step 8 — calls ``profile_provider.verify``; on
       ``CapabilityMismatch`` destroys the instance and re-raises.
    8. Constructs a :class:`ConcurrentPool`, registers the backend at
       ``cfg.lifecycle().max_in_flight``, and yields the assembled
       :class:`DeploySession`.

    On exit the function:

    * Always closes the pool (in a ``finally`` block — propagates any
      body exception unchanged).
    * Does NOT call ``provider.destroy_instance`` — the instance is left
      alive for warm reuse by the next session or for the
      sweeper / budget tracker to reap.

    Args:
        cfg: The loaded kinoforge configuration.
        store: ArtifactStore for the profile cache and any per-call
            outputs.
        provider: Optional pre-constructed ``ComputeProvider`` (test
            injection).
        engine: Optional pre-constructed ``GenerationEngine`` (test
            injection).
        creds: Optional credential provider, forwarded to the
            provisioner.
        profile_provider: Optional ``ModelProfileProvider`` (defaults to
            ``JsonProfileCache(store)``).
        run_id: Namespace tag forwarded to ``InstanceSpec.run_id``
            (used in pod tags).
        state_dir: Root for kinoforge state (provision markers, weights,
            locks).

    Yields:
        A live :class:`DeploySession`.  ``session.pool`` is open with
        one slot wrapping ``session.backend``.

    Raises:
        CapacityError: No compute offer satisfies hardware requirements.
        CapabilityMismatch: Profile verify drift — instance is
            destroyed before this propagates.
    """
    # ------------------------------------------------------------------
    # Step 1 — derive capability key + serialised cfg dict
    # ------------------------------------------------------------------
    key = cfg.capability_key()
    cfg_dict = _cfg_dict(cfg)

    # ------------------------------------------------------------------
    # Step 2 — resolve engine (and provider when compute is required)
    # ------------------------------------------------------------------
    resolved_engine = _resolve_engine(cfg, engine)
    resolved_provider: ComputeProvider | None = None
    if resolved_engine.requires_compute:
        resolved_provider = _resolve_provider(cfg, provider)

    # ------------------------------------------------------------------
    # Step 2.5 — UX A hosted preflight (Layer I)
    # ------------------------------------------------------------------
    if not resolved_engine.requires_compute:
        resolved_engine.provision(None, cfg_dict)

    # ------------------------------------------------------------------
    # Step 3 — default profile_provider when not injected
    # ------------------------------------------------------------------
    if profile_provider is None:
        profile_provider = JsonProfileCache(store)

    # ------------------------------------------------------------------
    # Step 4 — resolve profile; discover on miss
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Step 7 — ensure we have a backend (cache-hit branch)
    # ------------------------------------------------------------------
    if backend is None:
        if resolved_engine.requires_compute:
            if resolved_provider is None:
                raise CapacityError(
                    "requires_compute is True but no provider was resolved"
                ) from None
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

    # ------------------------------------------------------------------
    # Step 8 — verify (skip when just-discovered).  Fail-hard teardown
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Step 8.5 — build the shared pool + yield
    # ------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# deploy()
# ---------------------------------------------------------------------------


def deploy(
    cfg: Config,
    *,
    dry_run: bool = False,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    creds: CredentialProvider | None = None,
) -> DeployResult:
    """Provision compute (or confirm hosted endpoint) for a kinoforge config.

    Steps for the **compute path**:

    1. Derive a ``CapabilityKey`` from *cfg*.
    2. Resolve the engine (registry or injection).
    3. If ``engine.requires_compute == False`` (hosted): skip compute entirely,
       return a ``DeployResult`` with ``instance=None`` and the engine's endpoints.
    4. Resolve the provider.  Call ``provider.find_offers(cfg.hardware_requirements())``;
       raise ``CapacityError`` if the list is empty.
    5. **Dry-run:** print a vendor/engine-neutral plan and return a
       ``DeployResult(instance=None, plan_text=...)``.  ``create_instance`` is
       NEVER called in dry-run mode.
    6. **Live run:** create an instance, wait for ``status == "ready"``
       (``LocalProvider`` returns ready immediately), and return a
       ``DeployResult`` with the instance and provider endpoints.

    Args:
        cfg: The loaded kinoforge configuration.
        dry_run: When ``True``, print the plan and return without creating
            any cloud resource.
        provider: Optional pre-constructed ``ComputeProvider`` (test injection).
            When ``None``, resolved from the registry using ``cfg.compute.provider``.
        engine: Optional pre-constructed ``GenerationEngine`` (test injection).
            When ``None``, resolved from the registry using ``cfg.engine.kind``.
        creds: Optional credential provider.  Defaults to ``EnvCredentialProvider()``.

    Returns:
        A ``DeployResult`` describing the outcome.

    Raises:
        CapacityError: No compute offer satisfies ``cfg.hardware_requirements()``.
    """
    key = cfg.capability_key()
    resolved_engine = _resolve_engine(cfg, engine)

    # Hosted path: skip compute entirely.
    if not resolved_engine.requires_compute:
        _log.info(
            "hosted engine %r — skipping compute provisioning", resolved_engine.name
        )
        backend = resolved_engine.backend(None, _cfg_dict(cfg))
        return DeployResult(instance=None, endpoints=backend.endpoints())

    # Compute path: resolve provider and find offers.
    resolved_provider = _resolve_provider(cfg, provider)
    hw_reqs = cfg.hardware_requirements()
    offers = resolved_provider.find_offers(hw_reqs)

    if not offers:
        raise CapacityError(
            f"no compute offers available from provider "
            f"{getattr(resolved_provider, 'name', repr(resolved_provider))!r} "
            f"for hardware requirements {hw_reqs!r}"
        )

    lifecycle = cfg.lifecycle()

    # Build a short capability-key hash for the plan/tags.
    key_hash = _key_hash(key)

    if dry_run:
        # Vendor/engine-neutral plan — DO NOT call create_instance.
        plan_text = (
            f"[kinoforge dry-run plan]\n"
            f"  engine:           {resolved_engine.name}\n"
            f"  provider:         {getattr(resolved_provider, 'name', repr(resolved_provider))}\n"
            f"  model count:      {len(cfg.models)}\n"
            f"  offers available: {len(offers)}\n"
            f"  lifecycle ceilings:\n"
            f"    idle_timeout_s:  {lifecycle.idle_timeout_s}\n"
            f"    max_lifetime_s:  {lifecycle.max_lifetime_s}\n"
            f"    budget_usd:      {lifecycle.budget_usd}\n"
            f"  capability key:   {key_hash}...\n"
        )
        _log.info("dry-run plan:\n%s", plan_text)
        return DeployResult(instance=None, endpoints={}, plan_text=plan_text)

    # Live run: create the instance.
    image = cfg.compute.image if cfg.compute is not None else ""
    spec = InstanceSpec(
        image=image,
        offer=offers[0],
        lifecycle=lifecycle,
        tags={
            "kinoforge_engine": resolved_engine.name,
            "kinoforge_key": key_hash,
        },
        env={},
        run_id="",
    )

    instance = resolved_provider.create_instance(spec)

    try:
        # Poll until ready (LocalProvider returns ready immediately; cloud providers
        # may require polling — this handles both).
        while instance.status != "ready":
            instance = resolved_provider.get_instance(instance.id)

        endpoints = resolved_provider.endpoints(instance)
        _log.info(
            "deployed instance %r via %r (status=%s)",
            instance.id,
            getattr(resolved_provider, "name", repr(resolved_provider)),
            instance.status,
        )
        return DeployResult(instance=instance, endpoints=endpoints)
    except BaseException as exc:
        # Pod is paid-for and not tracked — destroy it before re-raising.
        _log.error(
            "deploy failed after create_instance(%r); attempting destroy: %s",
            instance.id,
            exc,
        )
        try:
            resolved_provider.destroy_instance(instance.id)
        except Exception as destroy_exc:
            _log.error(
                "destroy_instance(%r) failed during deploy-error cleanup: %s",
                instance.id,
                destroy_exc,
            )
            # Re-raise the ORIGINAL error; surface destroy failure via the log only.
        raise


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


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
) -> Artifact:
    """Run the full generation pipeline for a single clip.

    **Guaranteed ordering (explicit, in this order):**

    1. Derive ``CapabilityKey`` from *cfg*.
    2. Resolve engine (and provider when ``requires_compute``).
    3. Get ``profile_provider`` (defaults to ``JsonProfileCache(store)``).
    4. Try ``profile_provider.resolve(key)``.  On ``ProfileNotCached``:

       * **Compute path:** create a minimal instance via ``provider.create_instance``;
         build ``backend = engine.backend(instance, cfg_dict)``.
       * **Hosted path:** ``backend = engine.backend(None, cfg_dict)`` — no instance.
       * Call ``profile_provider.discover(key, engine, backend)`` to populate the cache.

    5. Validate the request via ``validate_request``.
    6. If ``backend`` was not yet created (profile was already cached), create it now:
       compute path calls ``provider.create_instance`` + ``engine.backend``; hosted calls
       ``engine.backend(None, ...)``.
    7. ``profile_provider.verify(profile, backend)`` — on ``CapabilityMismatch``, tear
       down the compute instance (if one exists) then **re-raise**.  This is fail-hard.
    8. Build a ``ConcurrentPool(...).add(backend, max_in_flight=cfg.lifecycle().max_in_flight)``
       + ``GenerateClipStage``; call ``stage.run(request)`` inside the ``with`` block;
       return the resulting ``Artifact``.

    Args:
        cfg: The loaded kinoforge configuration.
        request: The generation request (prompt, mode, assets).
        store: The artifact store for persisting results and profiles.
        provider: Optional ``ComputeProvider`` (test injection).
        engine: Optional ``GenerationEngine`` (test injection).
        creds: Optional credential provider (forwarded to provisioner).
        profile_provider: Optional ``ModelProfileProvider`` (test injection).
            Defaults to ``JsonProfileCache(store)``.
        run_id: Namespace for output artifacts in the store.
        state_dir: Root directory for kinoforge state (provision markers,
            weights, locks).  Defaults to ``Path(".kinoforge")`` for test
            scaffolding that doesn't pass it; the CLI always forwards
            ``--state-dir``.
        sink: Optional user-facing output sink.  When provided, the stage
            calls ``sink.publish(...)`` after persisting to the store.
            ``None`` (default) preserves pre-Layer-O behavior.

    Returns:
        The persisted ``Artifact`` (with ``uri``) from the pipeline stage.

    Raises:
        CapabilityMismatch: The live backend's capabilities differ from the
            cached profile; instance has already been destroyed before this
            propagates.
        CapacityError: No compute offer satisfies ``cfg.hardware_requirements()``.
        ValidationError: The ``request`` fails mode/role/kind validation.
    """
    # Steps 1-4, 7, 8 now live in deploy_session.__enter__.  This body
    # owns only the per-request work: validate (5), split (6), stage.run
    # (9).  ``dict(cfg.spec)`` / ``dict(cfg.params)`` still defensively
    # copies the pydantic-owned dicts so stage-side mutation cannot leak
    # back into ``cfg``.
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
        # ------------------------------------------------------------------
        # Step 5 — validate the request against the profile
        # ------------------------------------------------------------------
        accepted_kinds: set[str]
        if hasattr(session.engine, "accepted_kinds"):
            accepted_kinds = session.engine.accepted_kinds
        else:
            accepted_kinds = {"image"}

        from kinoforge.core.validation import validate_request

        validated = validate_request(
            session.profile, request, accepted_kinds=accepted_kinds
        )

        # ------------------------------------------------------------------
        # Step 6 — split the validated prompt into ordered segments
        # ------------------------------------------------------------------
        splitter = registry.get_splitter(cfg.splitter.kind)()
        prompt_segments = splitter.split(validated.prompt, session.profile, {})

        # Attach assets to segment 0 only.  Continuity (#02) fills 1..N-1.
        if prompt_segments and validated.assets:
            prompt_segments[0] = dataclasses.replace(
                prompt_segments[0], assets=list(validated.assets)
            )

        # ------------------------------------------------------------------
        # Step 9 — run the pipeline stage
        #
        # ValidationError from engine.validate_spec is treated like
        # CapabilityMismatch in deploy_session: tear down compute before
        # re-raising so a config typo cannot leave a billing pod alive.
        # ------------------------------------------------------------------
        stage = GenerateClipStage(
            profile=session.profile,
            pool=session.pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params=dict(cfg.params),
            base_spec=dict(cfg.spec),
            engine=session.engine,
            sink=sink,
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
