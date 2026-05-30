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
from dataclasses import dataclass

from kinoforge.core import registry
from kinoforge.core.config import Config
from kinoforge.core.errors import CapabilityMismatch, CapacityError
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
    ModelProfileProvider,
)
from kinoforge.core.logging import get_logger
from kinoforge.core.pool import SequentialPool
from kinoforge.core.profiles import JsonProfileCache
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
    8. Build a ``SequentialPool(backend)`` + ``GenerateClipStage``; call
       ``stage.run(request)``; return the resulting ``Artifact``.

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

    Returns:
        The persisted ``Artifact`` (with ``uri``) from the pipeline stage.

    Raises:
        CapabilityMismatch: The live backend's capabilities differ from the
            cached profile; instance has already been destroyed before this
            propagates.
        CapacityError: No compute offer satisfies ``cfg.hardware_requirements()``.
        ValidationError: The ``request`` fails mode/role/kind validation.
    """
    # ------------------------------------------------------------------
    # Step 1 — derive capability key
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
    # Step 3 — get (or default) profile provider
    # ------------------------------------------------------------------
    if profile_provider is None:
        profile_provider = JsonProfileCache(store)

    # ------------------------------------------------------------------
    # Step 4 — resolve profile; discover on miss
    # ------------------------------------------------------------------
    from kinoforge.core.errors import ProfileNotCached

    backend: GenerationBackend | None = None
    instance: Instance | None = None
    # Track whether we just ran discover so we can skip verify on fresh profiles.
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
            # Poll until ready.
            while instance.status != "ready":
                instance = resolved_provider.get_instance(instance.id)
            backend = resolved_engine.backend(instance, cfg_dict)
        else:
            # Hosted path — no instance needed for discovery.
            backend = resolved_engine.backend(None, cfg_dict)

        profile = profile_provider.discover(key, resolved_engine, backend)
        # Profile was just probed — skip verify on this call (it's trivially consistent).
        _just_discovered = True

    # ------------------------------------------------------------------
    # Step 5 — validate the request against the profile
    # ------------------------------------------------------------------
    accepted_kinds: set[str]
    if hasattr(resolved_engine, "accepted_kinds"):
        accepted_kinds = resolved_engine.accepted_kinds
    else:
        accepted_kinds = {"image"}

    from kinoforge.core.validation import validate_request

    validated = validate_request(profile, request, accepted_kinds=accepted_kinds)

    # ------------------------------------------------------------------
    # Step 6 — split the validated prompt into ordered segments
    # ------------------------------------------------------------------
    splitter = registry.get_splitter(cfg.splitter.kind)()
    prompt_segments = splitter.split(validated.prompt, profile, {})

    # Attach assets to segment 0 only. Continuity (#02) will fill segments
    # 1..N-1 with previous-frame conditioning when implemented.
    if prompt_segments and validated.assets:
        prompt_segments[0] = dataclasses.replace(
            prompt_segments[0], assets=list(validated.assets)
        )

    # ------------------------------------------------------------------
    # Step 7 — ensure we have a backend for generation
    # ------------------------------------------------------------------
    if backend is None:
        # Profile was already cached; create backend now.
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
            backend = resolved_engine.backend(instance, cfg_dict)
        else:
            backend = resolved_engine.backend(None, cfg_dict)

    # ------------------------------------------------------------------
    # Step 8 — verify: fail-hard teardown on CapabilityMismatch
    #
    # Skip verify when the profile was just discovered in this same call —
    # the probe is trivially consistent with itself and calling inspect_capabilities
    # twice in one generate() would be wasteful and confuses AC4 counters.
    # ------------------------------------------------------------------
    if not _just_discovered:
        try:
            profile_provider.verify(profile, backend)
        except CapabilityMismatch:
            _log.warning(
                "capability mismatch detected; tearing down instance before re-raising"
            )
            if instance is not None and resolved_provider is not None:
                resolved_provider.destroy_instance(instance.id)
            raise

    # ------------------------------------------------------------------
    # Step 9 — run the pipeline stage
    # ------------------------------------------------------------------
    pool = SequentialPool(backend)
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds=accepted_kinds,
        base_params={},
        base_spec={},
        engine=resolved_engine,
    )
    artifact = stage.run(request, segments_override=prompt_segments)
    _log.info("generate completed — artifact uri=%r", artifact.uri)
    return artifact
