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

import contextlib
import dataclasses
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal

from kinoforge.core import registry
from kinoforge.core.cancel import CancelToken
from kinoforge.core.config import Config
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import (
    AuthError,
    Cancelled,
    CapabilityMismatch,
    CapacityError,
    ProfileNotCached,
    ProvisionFailed,
    ProvisionTimeout,
    TeardownError,
    ValidationError,
)
from kinoforge.core.heartbeat_loop import HeartbeatLoop, HeartbeatLoopProtocol
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ComputeProvider,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationRequest,
    ImageBackend,
    ImageEngine,
    ImageProfileProvider,
    Instance,
    InstanceSpec,
    ModelProfile,
    ModelProfileProvider,
    Offer,
    PipelineState,
    Stage,
)
from kinoforge.core.lifecycle import Ledger, destroy_confirmed
from kinoforge.core.logging import get_logger
from kinoforge.core.pool import ConcurrentPool
from kinoforge.core.profiles import JsonImageProfileCache, JsonProfileCache
from kinoforge.core.provision_state import (
    is_marker_current,
    marker_key_for,
    marker_path,
    read_marker,
    write_marker,
)
from kinoforge.core.provisioner import provision as provisioner_provision
from kinoforge.core.session_claim import hold_until_first_tick
from kinoforge.core.validation import validate_request
from kinoforge.outputs.base import OutputSink
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.pipeline.keyframe import KeyframeStage
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
        A ready ``ComputeProvider`` instance, with the B5a heartbeat
        endpoint installed when ``cfg.compute.heartbeat_mode != "none"``.

    Raises:
        ValueError: ``cfg.compute`` is ``None`` (called on a hosted config).
        AuthError: heartbeat mode requires a credential that is not set.
        ValidationError: provider does not support the configured
            heartbeat_mode.
    """
    if provider is not None:
        return provider
    if cfg.compute is None:
        raise ValueError(
            "cannot resolve provider: cfg.compute is None (hosted engine path)"
        )
    p = registry.get_provider(cfg.compute.provider)()
    # B5a: install the heartbeat substrate endpoint when the operator
    # opted in via compute.heartbeat_mode. Lives here (not in the registry
    # factory) because the factory is zero-arg by ABC and the dispatch
    # needs cfg + creds. The dispatch lives in _adapters because importing
    # the concrete satisfier module from core would violate core-import-ban.
    if cfg.compute.heartbeat_mode != "none":
        from kinoforge._adapters import build_heartbeat_endpoint_for

        endpoint = build_heartbeat_endpoint_for(cfg, EnvCredentialProvider())
        p.set_heartbeat_endpoint(endpoint)
    return p


def _cfg_dict(cfg: Config) -> dict[str, object]:
    """Serialise *cfg* to a plain dict for engine/provisioner calls.

    Args:
        cfg: The pydantic Config to dump.

    Returns:
        A plain ``dict`` (pydantic model_dump output).
    """
    return cfg.model_dump()


_DIAG_BUCKET_DEFAULT = "kinoforge-pod-diagnostics"
_DIAG_REGION_DEFAULT = "us-west-2"


def _build_diagnostic_env(run_id: str) -> dict[str, str]:
    """Build the C28 diagnostic env overlay for an InstanceSpec.

    Reads ``KINOFORGE_DIAG_BUCKET`` (default ``kinoforge-pod-diagnostics``),
    derives ``KINOFORGE_DIAG_PREFIX`` from ``run_id``, and resolves AWS
    credentials via the boto3 default chain so the in-pod ``aws s3 cp`` call
    in the EXIT trap can authenticate.

    AWS keys are looked up through ``boto3.Session().get_credentials()``
    rather than ``os.environ`` directly so the project's
    ``AWS_SHARED_CREDENTIALS_FILE`` activation (per ``cloud_creds_workspace_local``)
    is honoured. If the chain returns no credentials, the AWS keys are
    omitted from the overlay; the in-pod ``aws s3 cp || true`` will then
    fail silently and the trap reports rc + last_line without an upload.

    Args:
        run_id: Per-run identifier. Used as the ``boot-logs/<run_id>`` prefix
            so each run's diagnostic snapshots land under a distinct path.

    Returns:
        Mapping of env-var name to value, ready to splat into
        ``InstanceSpec.diagnostic_env``.
    """
    overlay: dict[str, str] = {
        "KINOFORGE_DIAG_BUCKET": os.environ.get(
            "KINOFORGE_DIAG_BUCKET",
            _DIAG_BUCKET_DEFAULT,
        ),
        "KINOFORGE_DIAG_PREFIX": os.environ.get(
            "KINOFORGE_DIAG_PREFIX",
            f"boot-logs/{run_id}",
        ),
        "AWS_DEFAULT_REGION": os.environ.get(
            "AWS_DEFAULT_REGION",
            _DIAG_REGION_DEFAULT,
        ),
    }
    try:
        import boto3

        creds = boto3.Session().get_credentials()
    except (ImportError, Exception):  # pragma: no cover - boto3 always present
        creds = None
    if creds is not None:
        frozen = creds.get_frozen_credentials()
        overlay["AWS_ACCESS_KEY_ID"] = frozen.access_key
        overlay["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        if frozen.token:
            overlay["AWS_SESSION_TOKEN"] = frozen.token
    return overlay


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
    cfg_dict_override: dict[str, object] | None = None,
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
        cfg_dict_override: When provided, this dict is used as the cfg
            argument passed to ``engine.provision`` (via provisioner) instead
            of ``cfg.model_dump()``.  Callers that enrich ``cfg_dict`` (e.g.
            with a top-level ``"lifecycle"`` key) should pass it here so
            engines receive the augmented form.
    """
    effective_creds: CredentialProvider = (
        creds if creds is not None else EnvCredentialProvider()
    )
    marker = marker_path(state_dir, instance.id)

    # B7: provision:<id> lock is held by the outer hold_until_first_tick in
    # deploy_session.__enter__. The marker check remains idempotent for warm-
    # supplied paths where the caller also pre-provisioned. Concurrent
    # _provision_compute_once for the same instance.id is impossible by
    # construction — deploy_session is the only call site.
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
    if cfg_dict_override is not None:

        class _EnrichedCfgWrapper:
            """Thin shim: delegates .models to pydantic cfg; overrides model_dump."""

            models = cfg.models

            def model_dump(self) -> dict[str, object]:  # noqa: D102
                return cfg_dict_override  # type: ignore[return-value]

        effective_cfg: object = _EnrichedCfgWrapper()
    else:
        effective_cfg = cfg
    provisioner_provision(
        engine,
        effective_cfg,  # type: ignore[arg-type]
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


class _LazyClaim:
    """B7 lazy-acquire wrapper around :func:`hold_until_first_tick`.

    The cooperative session-claim lock keys on ``instance.id``, but
    ``instance.id`` is only known AFTER ``create_instance`` returns —
    which is itself nested inside
    :func:`_provision_instance_and_build_backend`. ``_LazyClaim`` lets
    :func:`deploy_session` wrap the entire region from cache-resolve
    through HeartbeatLoop's first tick in a single context manager whose
    actual lock acquisition is deferred to the moment ``instance.id``
    becomes available.

    Usage::

        holder = _LazyClaim(store=..., ledger=..., hb_interval=..., claim_ttl=...)
        with holder:
            # Some code that may create an instance
            holder.install(instance)
            # Subsequent code runs inside hold_until_first_tick
            # Lock releases on holder __exit__ via first-tick polling.

    ``install`` is a no-op when:
      * ``hb_interval`` is ``None`` or non-positive (HB disabled → no race), or
      * Already installed (idempotent — subsequent calls return immediately).

    On the no-op branch the holder stays inert; ``__exit__`` becomes a
    no-op as well. Hosted-engine paths never call ``install``.
    """

    def __init__(
        self,
        *,
        store: ArtifactStore,
        ledger: Ledger,
        hb_interval: float | None,
        claim_ttl: float,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._hb_interval = hb_interval
        self._claim_ttl = claim_ttl
        self._cm: contextlib.AbstractContextManager[None] | None = None

    def install(self, instance: Instance) -> None:
        """Enter ``hold_until_first_tick`` for ``instance.id`` once known."""
        if self._cm is not None:
            return
        if self._hb_interval is None or self._hb_interval <= 0:
            return
        cm = hold_until_first_tick(
            store=self._store,
            instance_id=instance.id,
            ledger=self._ledger,
            ttl_s=self._claim_ttl,
            timeout_s=self._claim_ttl,
        )
        cm.__enter__()
        self._cm = cm

    def __enter__(self) -> _LazyClaim:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        if self._cm is None:
            return None
        try:
            return self._cm.__exit__(exc_type, exc, tb)
        finally:
            self._cm = None


def _create_with_offer_retry(
    provider: ComputeProvider,
    build_spec: Callable[[Offer], InstanceSpec],
    offers: list[Offer],
) -> tuple[Instance, Offer]:
    """Iterate offers until create_instance succeeds.

    The first offer is tried first (the list is already sorted by
    filter_offers' gpu_preference). On CapacityError, continue to the
    next offer. Any other exception propagates immediately — non-
    capacity errors fail every offer identically.

    Args:
        provider: The resolved compute provider.
        build_spec: Closure that builds an InstanceSpec for one offer.
            Called once per offer attempted.
        offers: Non-empty list of offers in attempt order.

    Returns:
        ``(instance, offer)`` — the first offer for which create_instance
        succeeded, paired with the live instance.

    Raises:
        CapacityError: Every offer raised CapacityError. The last
            per-offer CapacityError is chained as ``__cause__``.
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
    on_instance_created: Callable[[Instance], None] | None = None,
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
        tags: Optional caller-supplied tags merged onto the orchestrator's
            built-in ``{kinoforge_engine, kinoforge_key}``. Caller wins on
            key collision.
        on_instance_created: Optional callback fired exactly once,
            immediately after ``create_instance`` returns, with the
            freshly-created ``Instance``. B7 uses this seam to enter
            ``hold_until_first_tick`` before ``engine.provision`` runs.

    Returns:
        ``(instance, backend)`` — instance polled to ``ready``, backend
        constructed via ``engine.backend(instance, cfg_dict)``.

    Raises:
        CapacityError: ``find_offers`` returned an empty list.
        AuthError: A var in ``rendered.env_required`` is absent from *creds*.
            Raised before ``create_instance`` is called.
        ProvisionFailed: Engine boot script crashed; instance already destroyed.
        ProvisionTimeout: Ready check timed out; instance already destroyed.
        CapabilityMismatch: Engine rejected its own capability key; instance destroyed.
        ValidationError: Spec validation failed; instance destroyed.
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
    cfg_dict = _cfg_dict(cfg)

    # Lift the resolved Lifecycle dataclass onto cfg_dict["lifecycle"] so that
    # engine.provision() can read canonical _s-suffixed interface keys
    # (boot_timeout_s, idle_timeout_s, etc.) regardless of pydantic schema
    # shape. cfg.model_dump() produces "lifecycle_cfg" at the top level and
    # "boot_timeout" (no _s) under compute.lifecycle — neither satisfies the
    # engine's lookup. This lift is the single authoritative source for the
    # engine-facing lifecycle dict.
    cfg_dict["lifecycle"] = dataclasses.asdict(lifecycle)

    # NEW — Layer Q: render provision payload + validate creds before create_instance
    rendered = resolved_engine.render_provision(cfg_dict)
    rendered_env: dict[str, str] = {}
    for var in rendered.env_required:
        value = creds.get(var) if creds is not None else None
        if value is None:
            raise AuthError(f"missing required env var: {var}")
        rendered_env[var] = value

    def _build_spec(offer: Offer) -> InstanceSpec:
        merged_tags: dict[str, str] = {
            "kinoforge_engine": resolved_engine.name,
            "kinoforge_key": key_hash,
        }
        if tags:
            merged_tags.update(tags)
        diagnostic_env: dict[str, str] = (
            _build_diagnostic_env(run_id) if cfg.diagnostic_mode else {}
        )
        # C28 A3: diagnostic-mode runs request restart_policy=never so a
        # crashed boot leaves the container in a STOPPED state instead of
        # being auto-restarted by RunPod (which would obliterate the
        # diagnostic snapshot the A2 trap is trying to upload). Effective only
        # if the provider's input schema accepts the field; otherwise the
        # RunPod provider warns + skips with no behaviour change.
        restart_policy: Literal["always", "never"] = (
            "never" if cfg.diagnostic_mode else "always"
        )
        return InstanceSpec(
            image=rendered.image or image,
            offer=offer,
            ports=tuple(rendered.ports),
            lifecycle=lifecycle,
            tags=merged_tags,
            env=dict(rendered_env),
            run_id=run_id,
            provision_script=rendered.script,
            run_cmd=rendered.run_cmd,
            diagnostic_env=diagnostic_env,
            restart_policy=restart_policy,
        )

    instance, _chosen_offer = _create_with_offer_retry(
        resolved_provider, _build_spec, offers
    )
    # B7 — acquire the cooperative session-claim lock now that instance.id is
    # known, BEFORE engine.provision runs. The callback enters the outer
    # hold_until_first_tick context; release happens on the _LazyClaim
    # holder's __exit__ in deploy_session.
    if on_instance_created is not None:
        on_instance_created(instance)
    # Status-only polling: preserve endpoints + tags from create_instance.
    # provider.get_instance(id) re-queries the API but the GraphQL `pod` query
    # only returns id/desiredStatus/imageName — endpoints + ports tag are
    # stripped. Without the replace, instance.endpoints goes from
    # populated-by-_create_pod to empty-by-_pod_to_instance, and the
    # downstream wait_for_ready raises ProvisionFailed immediately.
    while instance.status != "ready":
        time.sleep(2.0)
        refreshed = resolved_provider.get_instance(instance.id)
        instance = dataclasses.replace(instance, status=refreshed.status)

    # NEW — Layer Q: wire provider.get_instance onto engine before engine.provision
    resolved_engine.attach_get_instance(resolved_provider.get_instance)

    try:
        _provision_compute_once(
            engine=resolved_engine,
            cfg=cfg,
            instance=instance,
            creds=creds,
            store=store,
            state_dir=state_dir,
            # Alias-key the .provisioned marker under STRICT + vault.
            # default=key.derive() preserves the pre-existing contract for
            # call sites + tests that mock ``key`` directly; the alias
            # path fires only when an EphemeralSession + vault are active.
            # See docs/superpowers/specs/2026-06-10-provision-marker-alias-keying-design.md.
            capability_key_hex=marker_key_for(cfg, default=key.derive()),
            cfg_dict_override=cfg_dict,
        )
    except (ProvisionFailed, ProvisionTimeout, CapabilityMismatch, ValidationError):
        resolved_provider.destroy_instance(instance.id)
        raise

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
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
    heartbeat_loop_factory: Callable[..., HeartbeatLoopProtocol] | None = None,
    cancel_token: CancelToken | None = None,
    single: bool = False,
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
        instance: Optional pre-created ``Instance`` to reuse. When
            supplied, the orchestrator skips ``find_offers`` +
            ``create_instance`` and uses the caller's instance directly.
            ``engine.provision`` still runs (idempotent via Layer I
            marker). Caller owns the lifecycle — teardown is suppressed
            on ``CapabilityMismatch`` so the warm pod survives drift
            re-raises. Caller must pre-poll the instance to
            ``status == 'ready'``; ``deploy_session`` does not re-poll a
            supplied instance.
        tags: Optional caller-supplied tags merged onto the orchestrator's
            built-in ``{kinoforge_engine, kinoforge_key}`` when the
            orchestrator creates the pod on the cold path. Caller wins on
            key collision. Ignored when ``instance=`` is supplied (caller
            already owns the instance's tags).
        heartbeat_loop_factory: Layer U seam — optional callable that
            builds a :class:`HeartbeatLoopProtocol` given the kwargs
            ``ledger``/``provider``/``instance_id``/``interval_s``.
            Defaults to :class:`HeartbeatLoop`. Tests substitute a
            non-threaded spy. Only called when
            ``cfg.lifecycle().heartbeat_interval_s`` is set AND a
            compute instance was created (hosted-engine sessions skip
            the loop entirely).
        cancel_token: Phase 50 cooperative-cancellation token. When set
            (typically by the CLI SIGINT handler) the ``__exit__``
            ``finally`` calls ``pool.close(cancel_pending=True,
            timeout=30.0)`` so a wedged worker no longer blocks
            shutdown forever. ``None`` (the library default) preserves
            today's unbounded-wait behavior.
        single: B3 ``--no-reuse`` knob. When ``True`` and a compute
            instance was created (or supplied), ``__exit__`` runs
            ``destroy_confirmed`` + ``Ledger.forget`` under the
            ``reaper:<id>`` lock so the pod tears down immediately
            after the yielded body returns. Hosted-engine paths and
            ``instance is None`` paths skip the destroy. Default
            ``False`` preserves warm-reuse-friendly behavior.

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
    # Lift the resolved Lifecycle dataclass so engine.provision() sees the
    # canonical _s-suffixed interface keys (boot_timeout_s etc.) regardless
    # of pydantic schema shape (model_dump emits "lifecycle_cfg" + nested
    # "boot_timeout" without the _s suffix).  Only lift when compute is
    # present — hosted engines don't have a lifecycle block.
    if cfg.compute is not None:
        cfg_dict["lifecycle"] = dataclasses.asdict(cfg.lifecycle())
    _caller_supplied_instance = instance is not None

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
    # B7 — Build the cooperative session-claim lock holder.
    # Acquisition is DEFERRED until instance.id becomes available (inside
    # the cache-miss / cache-hit branches below, or the caller-supplied
    # path). Release happens when the holder's ``with`` block exits, which
    # also runs the first-tick polling phase. Hosted-engine paths never
    # call install — holder stays inert. HB-disabled compute paths call
    # install but install short-circuits — holder also stays inert.
    # ------------------------------------------------------------------
    _ledger_for_claim = Ledger(store=store)
    _hb_interval = cfg.lifecycle().heartbeat_interval_s
    _claim_ttl: float = (
        cfg.lifecycle().boot_timeout_s + 2.0 * _hb_interval
        if (_hb_interval is not None and _hb_interval > 0)
        else 0.0
    )
    claim_holder = _LazyClaim(
        store=store,
        ledger=_ledger_for_claim,
        hb_interval=_hb_interval,
        claim_ttl=_claim_ttl,
    )

    def _record_then_install(inst: Instance) -> None:
        """Record + claim — chain on_instance_created callbacks.

        B3 + B7 — record instance to ledger BEFORE entering
        ``hold_until_first_tick``. Without an existing ledger entry,
        :class:`HeartbeatLoop`'s ``ledger.touch`` no-ops (strict
        update) and ``hold_until_first_tick`` polls forever waiting
        for a sentinel that never lands.
        """
        try:
            _ledger_for_claim.record(
                inst,
                idle_timeout_s=int(cfg.lifecycle().idle_timeout_s),
                max_age_s=int(cfg.lifecycle().max_lifetime_s),
            )
        except Exception as record_exc:  # noqa: BLE001
            _log.warning(
                "B3/B7: ledger.record failed for %s: %s "
                "(hold_until_first_tick may FirstTickTimeout)",
                inst.id,
                record_exc,
            )
        claim_holder.install(inst)

    try:
        with claim_holder:
            # --------------------------------------------------------------
            # Step 4 — resolve profile; discover on miss
            # --------------------------------------------------------------
            backend: GenerationBackend | None = None
            _just_discovered: bool = False

            try:
                profile = profile_provider.resolve(key)
                _log.debug("profile cache hit for key %s", key.derive()[:12])
            except ProfileNotCached:
                _log.debug(
                    "profile cache miss for key %s — running discover",
                    key.derive()[:12],
                )
                if resolved_engine.requires_compute:
                    if resolved_provider is None:
                        raise CapacityError(
                            "requires_compute is True but no provider was resolved"
                        ) from None
                    if _caller_supplied_instance:
                        # Caller pre-created the pod; marker-idempotent provision.
                        claim_holder.install(instance)  # type: ignore[arg-type]
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
                            on_instance_created=_record_then_install,
                        )
                else:
                    backend = resolved_engine.backend(None, cfg_dict)

                profile = profile_provider.discover(key, resolved_engine, backend)
                _just_discovered = True

            # --------------------------------------------------------------
            # Step 7 — ensure we have a backend (cache-hit branch)
            # --------------------------------------------------------------
            if backend is None:
                if resolved_engine.requires_compute:
                    if resolved_provider is None:
                        raise CapacityError(
                            "requires_compute is True but no provider was resolved"
                        ) from None
                    if _caller_supplied_instance:
                        claim_holder.install(instance)  # type: ignore[arg-type]
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
                            on_instance_created=_record_then_install,
                        )
                else:
                    backend = resolved_engine.backend(None, cfg_dict)

            # --------------------------------------------------------------
            # Step 8 — verify (skip when just-discovered).  Fail-hard teardown
            # --------------------------------------------------------------
            if not _just_discovered:
                try:
                    profile_provider.verify(
                        profile, backend, engine=resolved_engine, key=key
                    )
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

            # --------------------------------------------------------------
            # Step 8.5 — build the shared pool + yield
            # --------------------------------------------------------------
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

            # Layer U — spawn a background HeartbeatLoop when configured.
            # Gated on both (a) a positive interval AND (b) a compute instance
            # to track. Hosted-engine sessions have no instance + no
            # provider.heartbeat to call, so the loop would log exceptions
            # every tick. The factory seam lets tests substitute a non-
            # threaded spy.
            hb_loop: HeartbeatLoopProtocol | None = None
            interval = cfg.lifecycle().heartbeat_interval_s
            if (
                interval is not None
                and interval > 0
                and instance is not None
                and resolved_provider is not None
            ):
                factory: Callable[..., HeartbeatLoopProtocol] = (
                    heartbeat_loop_factory or HeartbeatLoop
                )
                # C26: build util-snapshot endpoint + thread stall thresholds.
                # When build_util_endpoint_for returns None (kill switch or
                # unsupported provider), HeartbeatLoop falls back to the B5a
                # heartbeat-only path. Reading thresholds direct from cfg.compute
                # avoids extending InterfaceLifecycle for a path that's only
                # consumed here.
                from kinoforge._adapters import build_util_endpoint_for

                util_endpoint = (
                    build_util_endpoint_for(cfg, creds) if creds is not None else None
                )
                stall_window_s: float | None = None
                stall_gpu_threshold = 5.0
                stall_cpu_threshold = 20.0
                # C27: restart-loop predicate cfg knobs — None window = kill switch.
                restart_loop_window_s: float | None = None
                restart_loop_uptime_threshold_s = 90.0
                provider_kind: str | None = None
                if cfg.compute is not None:
                    provider_kind = cfg.compute.provider
                    lc = cfg.compute.lifecycle
                    if lc is not None and lc.stall_reap_enabled:
                        stall_window_s = lc.stall_window_s
                        stall_gpu_threshold = lc.stall_gpu_threshold
                        stall_cpu_threshold = lc.stall_cpu_threshold
                    if lc is not None and lc.restart_loop_reap_enabled:
                        restart_loop_window_s = lc.restart_loop_window_s
                        restart_loop_uptime_threshold_s = (
                            lc.restart_loop_uptime_threshold_s
                        )
                hb_loop = factory(
                    ledger=Ledger(store=store),
                    provider=resolved_provider,
                    instance_id=instance.id,
                    interval_s=interval,
                    util_endpoint=util_endpoint,
                    cancel_token=cancel_token,
                    provider_kind=provider_kind,
                    stall_window_s=stall_window_s,
                    stall_gpu_threshold=stall_gpu_threshold,
                    stall_cpu_threshold=stall_cpu_threshold,
                    restart_loop_window_s=restart_loop_window_s,
                    restart_loop_uptime_threshold_s=restart_loop_uptime_threshold_s,
                )
                hb_loop.start()
                # B3 — record session_start so concurrent scanners see this CLI's claim.
                # Write AFTER hb_loop.start() so the heartbeat freshness gate trusts
                # the marker. Touch failure is non-fatal — log + continue.
                try:
                    Ledger(store=store).touch(instance.id, session_start=time.time())
                except Exception as touch_exc:  # noqa: BLE001
                    _log.warning(
                        "B3: ledger.touch(session_start) failed for %s: %s",
                        instance.id,
                        touch_exc,
                    )
            try:
                yield session
            finally:
                if hb_loop is not None:
                    hb_loop.stop()
                # Phase 50: when the caller requested cancellation, drain the
                # pool with a bounded watchdog so a wedged worker no longer
                # blocks shutdown forever. The token-unset path preserves
                # today's unbounded-wait behavior so library callers without a
                # token see no change. ``pool.close`` failures are logged at
                # ERROR but do not swallow ``BaseException`` — a fresh
                # KeyboardInterrupt during shutdown must still propagate so
                # the operator can force-exit.
                if cancel_token is not None and cancel_token.is_set():
                    try:
                        pool.close(cancel_pending=True, timeout=30.0)
                    except Exception as close_exc:
                        _log.error(
                            "pool.close failed during interrupt cleanup: %s", close_exc
                        )
                else:
                    pool.close()
                # B3 — record session_end so future scanners auto-clear busy
                # state. Write BEFORE any --no-reuse destroy (Task d) so the
                # causal chain session_end-then-destroy is correct: a concurrent
                # classify never sees STALE_LEDGER for an entry still flagged
                # busy.
                if instance is not None and resolved_provider is not None:
                    try:
                        Ledger(store=store).touch(instance.id, session_end=time.time())
                    except Exception as touch_exc:  # noqa: BLE001
                        _log.warning(
                            "B3: ledger.touch(session_end) failed for %s: %s",
                            instance.id,
                            touch_exc,
                        )
    finally:
        # B3 — --no-reuse destroy under reaper:<id> lock. Composes
        # with --instance-id per D7 (operator wants attach + destroy).
        # Reaper lock prevents concurrent B3 scanners from attaching
        # mid-destroy. Runs AFTER the claim_holder exits so
        # ``hold_until_first_tick`` sees the existing
        # ``heartbeat_thread_tick`` (forgetting the ledger entry inside
        # the holder would hang the first-tick poll until timeout).
        if single and instance is not None and resolved_provider is not None:
            try:
                with store.acquire_lock(f"reaper/{instance.id}", ttl_s=30.0):
                    destroy_confirmed(resolved_provider, instance.id, sleep=time.sleep)
                    Ledger(store=store).forget(instance.id)
                    _log.info("--no-reuse: destroyed + forgot pod %s", instance.id)
            except TeardownError as destroy_exc:
                _log.error(
                    "--no-reuse destroy failed for %s: %s "
                    "(use `kinoforge reap --apply` to recover)",
                    instance.id,
                    destroy_exc,
                )
            except Exception as destroy_exc:  # noqa: BLE001
                _log.error(
                    "--no-reuse destroy raised unexpected for %s: %s",
                    instance.id,
                    destroy_exc,
                )


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
    tags: dict[str, str] | None = None,
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
        tags: Optional caller-supplied tags merged onto the orchestrator's
            built-in ``{kinoforge_engine, kinoforge_key}``. Caller wins on
            key collision.

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

    instance, _chosen_offer = _create_with_offer_retry(
        resolved_provider, _build_spec, offers
    )

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
    image_engine: ImageEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
    run_id: str = "run",
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
    cancel_token: CancelToken | None = None,
    single: bool = False,
) -> tuple[Artifact, Instance | None]:
    """Run the full generation pipeline for a single clip.

    When ``cfg.keyframe`` is set, a :class:`~kinoforge.pipeline.keyframe.KeyframeStage`
    is prepended to the pipeline to fill any missing image-kind conditioning roles
    before the main clip generation step.

    **Guaranteed ordering (explicit, in this order):**

    1. If ``cfg.keyframe`` is set, pre-resolve the image engine + backend + profile
       BEFORE ``deploy_session`` so that unknown image-engine names fail fast without
       incurring any compute spend.
    2. Enter ``deploy_session`` — resolves the video engine, profile, backend, and
       compute instance (or uses the hosted path).
    3. Validate the request against the video profile.
    4. Split the validated prompt into ordered segments.
    5. Build ``stages: list[Stage]`` — ``[KeyframeStage, GenerateClipStage]`` when
       ``cfg.keyframe`` is set, else ``[GenerateClipStage]``.
    6. Walk the stage list with a shared ``PipelineState``; extract
       ``state.artifacts["clip"]`` as the return artifact.
    7. On ``ValidationError`` from any stage: tear down the compute instance (if the
       orchestrator created it) then re-raise.

    Args:
        cfg: The loaded kinoforge configuration.
        request: The generation request (prompt, mode, assets).
        store: The artifact store for persisting results and profiles.
        provider: Optional ``ComputeProvider`` (test injection).
        engine: Optional ``GenerationEngine`` (test injection).
        image_engine: Optional ``ImageEngine`` (test injection for the keyframe path).
            When ``None`` and ``cfg.keyframe`` is set, resolved from the registry via
            ``cfg.keyframe.engine``.
        creds: Optional credential provider (forwarded to provisioner).
        profile_provider: Optional ``ModelProfileProvider`` (test injection).
            Defaults to ``JsonProfileCache(store)``.
        image_profile_provider: Optional ``ImageProfileProvider`` (test injection for
            the image-engine profile cache).  Defaults to ``JsonImageProfileCache(store)``
            when ``cfg.keyframe`` is set.
        run_id: Namespace for output artifacts in the store.
        state_dir: Root directory for kinoforge state (provision markers,
            weights, locks).  Defaults to ``Path(".kinoforge")`` for test
            scaffolding that doesn't pass it; the CLI always forwards
            ``--state-dir``.
        sink: Optional user-facing output sink.  When provided, the stage
            calls ``sink.publish(...)`` after persisting to the store.
            ``None`` (default) preserves pre-Layer-O behavior.
        instance: Optional pre-created ``Instance`` to reuse. Threaded
            through to ``deploy_session`` — when supplied, ``create_instance``
            is skipped and the ``ValidationError`` teardown is suppressed
            so the caller-owned warm pod survives spec-validation failures.
            Caller must pre-poll the instance to ``status == 'ready'``;
            ``generate`` does not re-poll a supplied instance.
        tags: Optional caller-supplied tags merged onto the orchestrator's
            built-in ``{kinoforge_engine, kinoforge_key}`` on the cold path
            (no ``instance=``). Ignored when ``instance=`` is supplied.
        cancel_token: Phase 50 cooperative-cancellation token. When the
            CLI SIGINT handler flips this on operator Ctrl-C, the
            backend poll loops, the pool shutdown, and the stage loop
            unwind cooperatively. On a ``Cancelled`` raise the pod is
            NOT destroyed (warm-reuse intent per commit ``3bc6473``);
            a single WARN names the surviving pod id + ``kinoforge
            reap`` recovery command. ``None`` (the library default)
            preserves the pre-Phase-50 uncancellable path.
        single: B3 ``--no-reuse`` knob; threaded through to
            :func:`deploy_session`. When ``True`` the pod is destroyed
            + forgotten under the ``reaper:<id>`` lock at the end of
            this call. Default ``False`` preserves warm-reuse.

    Returns:
        A ``(Artifact, Instance | None)`` tuple. The ``Artifact`` is the
        persisted output (with ``uri``) from the pipeline stage. The second
        element is the compute ``Instance`` the orchestrator used or created
        during this call — ``None`` for hosted engines
        (``requires_compute=False``) or when the caller supplied a warm
        instance via ``instance=``; otherwise the live pod the orchestrator
        owns for the duration of the call. Returning the instance lets
        callers run post-generate teardown by pod id (e.g. live tests)
        without resorting to provider-specific tag-discovery scans.

    Raises:
        CapabilityMismatch: The live backend's capabilities differ from the
            cached profile; instance has already been destroyed before this
            propagates.
        CapacityError: No compute offer satisfies ``cfg.hardware_requirements()``.
        UnknownAdapter: ``cfg.keyframe.engine`` is not registered in the image-engine
            registry.
        ValidationError: The ``request`` fails mode/role/kind validation, or a
            stage raises ``ValidationError`` (e.g. missing keyframe prompt).
    """
    # Default-shim: a None creds reaches the provisioner as None and trips
    # AuthError on the first env_required var even when os.environ holds
    # the value. CLI callers and ad-hoc harnesses routinely forget the
    # kwarg; default it here so the public API matches operator
    # expectations. Mirrors the precedent at _provision_compute_once
    # (line 209). Drift-locked by
    # tests/core/test_orchestrator_creds_default.py.
    if creds is None:
        creds = EnvCredentialProvider()
    _caller_supplied_instance = instance is not None

    # ------------------------------------------------------------------
    # Pre-resolve image engine + backend + profile if keyframe block present.
    # Image engine resolved BEFORE deploy_session so unknown names fail fast
    # without incurring any compute spend.
    # ------------------------------------------------------------------
    image_backend: ImageBackend | None = None
    image_prof = None
    resolved_image_engine: ImageEngine | None = None
    if cfg.keyframe is not None:
        resolved_image_engine = (
            image_engine
            if image_engine is not None
            else registry.get_image_engine(cfg.keyframe.engine)()
        )
        kf_cfg_dict = cfg.keyframe.model_dump()
        resolved_image_engine.provision(None, kf_cfg_dict)
        image_backend = resolved_image_engine.backend(None, kf_cfg_dict)
        image_key = cfg.keyframe.capability_key()
        ipp: ImageProfileProvider = (
            image_profile_provider
            if image_profile_provider is not None
            else JsonImageProfileCache(store)  # type: ignore[assignment]
        )
        try:
            image_prof = ipp.resolve(image_key)
        except ProfileNotCached:
            image_prof = ipp.discover(image_key, resolved_image_engine, image_backend)

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
        cancel_token=cancel_token,
        single=single,
    ) as session:
        _eph = EphemeralSession.current()
        if _eph is not None:
            _eph.register_store(store, run_id)
        accepted_kinds: set[str] = getattr(session.engine, "accepted_kinds", {"image"})

        # ------------------------------------------------------------------
        # When a keyframe block is present, run KeyframeStage FIRST so that
        # any missing image-kind conditioning roles (e.g. init_image for i2v)
        # are filled BEFORE validate_request checks for required roles.
        # validate_request is then called on the enriched request so the role
        # contract is satisfied by the keyframe-generated assets.
        # ------------------------------------------------------------------
        state = PipelineState(request=request, artifacts={})
        if cfg.keyframe is not None:
            # Layer 4 — keyframes publish to the user-facing sink with the
            # IMAGE engine's name as `provider` and the keyframe spec.model as
            # `model`, so they land next to the final clip but tagged with
            # the image-generation provider that produced them.
            # Layer 8 — keyframe stage mirrors clip stage: image_engine.model_identity
            # owns the slug so non-spec-model image engines (e.g. fal image, future
            # LumaAgentsImageEngine) get a real slug instead of "unknown".
            # resolved_image_engine is guaranteed non-None here: it was assigned
            # in the matching `if cfg.keyframe is not None:` block above (line ~1008).
            # Narrow to ImageEngine so mypy can resolve attribute access below.
            _kf_eng: ImageEngine = resolved_image_engine  # type: ignore[assignment]
            _kf_provider = getattr(_kf_eng, "name", None) or None
            _raw_kf_model = _kf_eng.model_identity(kf_cfg_dict)
            if not _raw_kf_model:
                _log.warning(
                    "image engine %s returned empty model identity; "
                    "sink will render keyframe filename slug as 'unknown'",
                    _kf_eng.name,
                )
            _kf_model = _raw_kf_model or None
            try:
                state = KeyframeStage(
                    keyframe_cfg=cfg.keyframe,
                    image_engine=_kf_eng,
                    image_backend=image_backend,  # type: ignore[arg-type]
                    image_profile=image_prof,  # type: ignore[arg-type]
                    store=store,
                    run_id=run_id,
                    sink=sink,
                    provider=_kf_provider,
                    model=_kf_model,
                ).run(state)
            except ValidationError:
                _log.warning(
                    "spec validation failed; tearing down instance before re-raising"
                )
                if (
                    session.instance is not None
                    and session.provider is not None
                    and not _caller_supplied_instance
                ):
                    session.provider.destroy_instance(session.instance.id)
                raise
            except (KeyboardInterrupt, Cancelled) as exc:
                # Phase 50: operator-initiated cancellation. Warm-reuse
                # intent preserved (per commit 3bc6473) — the pod stays
                # alive for ledger-driven reap or the next session. We
                # log a single WARN naming the pod id so the operator
                # knows exactly which pod to destroy with `kinoforge
                # reap`. Hosted-engine sessions render ``<hosted>``.
                _log.warning(
                    "%s during keyframe stage; pod %s kept alive "
                    "(selfterm/reap path). Run `kinoforge reap` to "
                    "destroy now.",
                    type(exc).__name__,
                    session.instance.id if session.instance is not None else "<hosted>",
                )
                raise

        # ------------------------------------------------------------------
        # Validate the (possibly keyframe-enriched) request against the
        # video profile.
        # ------------------------------------------------------------------
        validated = validate_request(
            session.profile, state.request, accepted_kinds=accepted_kinds
        )
        state = dataclasses.replace(state, request=validated)

        # ------------------------------------------------------------------
        # Split the validated prompt into ordered segments.
        # Attach assets to segment 0 only.  Continuity (#02) fills 1..N-1.
        # ------------------------------------------------------------------
        splitter = registry.get_splitter(cfg.splitter.kind)()
        prompt_segments = splitter.split(validated.prompt, session.profile, {})
        if prompt_segments and validated.assets:
            prompt_segments[0] = dataclasses.replace(
                prompt_segments[0], assets=list(validated.assets)
            )

        # ------------------------------------------------------------------
        # Build stage list from cfg-block presence (GenerateClipStage only
        # here — KeyframeStage already ran above when keyframe was set).
        # ------------------------------------------------------------------
        # Layer 8: provider + model for the OutputSink filename schema.
        # Provider = registered engine name; model = engine.model_identity(cfg)
        # so non-hosted engines (fal, comfyui, bedrock) get a real slug instead
        # of "unknown". Empty return -> WARNING + None -> sink renders "unknown".
        _provider = getattr(session.engine, "name", None) or None
        cfg_dict = _cfg_dict(cfg)
        _raw_model = session.engine.model_identity(cfg_dict)
        if not _raw_model:
            _log.warning(
                "engine %s returned empty model identity; "
                "sink will render filename slug as 'unknown'",
                session.engine.name,
            )
        _model = _raw_model or None
        stages: list[Stage] = [
            GenerateClipStage(
                profile=session.profile,
                pool=session.pool,
                store=store,
                run_id=run_id,
                accepted_kinds=accepted_kinds,
                base_params=dict(cfg.params),
                base_spec=dict(cfg.spec),
                engine=session.engine,
                segments=prompt_segments,
                sink=sink,
                provider=_provider,
                model=_model,
                cancel_token=cancel_token,
            )
        ]

        # ------------------------------------------------------------------
        # Walk the remaining stages with shared PipelineState.
        # ValidationError from any stage → tear down compute before re-raise
        # so a config typo cannot leave a billing pod alive.
        # ------------------------------------------------------------------
        try:
            for stage in stages:
                state = stage.run(state)
        except ValidationError:
            _log.warning(
                "spec validation failed; tearing down instance before re-raising"
            )
            if (
                session.instance is not None
                and session.provider is not None
                and not _caller_supplied_instance
            ):
                session.provider.destroy_instance(session.instance.id)
            raise
        except (KeyboardInterrupt, Cancelled) as exc:
            # Phase 50: operator-initiated cancellation. Pod stays alive
            # (warm-reuse intent per commit 3bc6473) — log a single WARN
            # naming the surviving pod id so the operator knows exactly
            # what to destroy with ``kinoforge reap``. Hosted-engine
            # sessions render ``<hosted>``. Catch order matters: this
            # arm runs AFTER ValidationError so config-typo failures
            # still tear down the pod; KeyboardInterrupt is a
            # BaseException and would otherwise propagate silently.
            _log.warning(
                "%s during stages; pod %s kept alive "
                "(selfterm/reap path). Run `kinoforge reap` to destroy "
                "now.",
                type(exc).__name__,
                session.instance.id if session.instance is not None else "<hosted>",
            )
            raise

        artifact = state.artifacts["clip"]
        _log.info("generate completed — artifact uri=%r", artifact.uri)
        owned_instance = None if _caller_supplied_instance else session.instance
        return artifact, owned_instance
