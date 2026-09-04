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
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from kinoforge.core import registry
from kinoforge.core.cancel import CancelToken
from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.clock import Clock, RealClock
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
    RateCapExceeded,
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
    Launch,
    Lifecycle,
    ModelProfile,
    ModelProfileProvider,
    PipelineState,
    RenderedProvision,
    Stage,
)
from kinoforge.core.lifecycle import (
    LAUNCH_PHASE_LAUNCHING,
    LAUNCH_PHASE_TAG,
    Ledger,
    destroy_confirmed,
)
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
from kinoforge.core.spec_builder import build_instance_spec
from kinoforge.core.validation import validate_request
from kinoforge.outputs.base import OutputSink
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.pipeline.keyframe import KeyframeStage
from kinoforge.stores.base import ArtifactStore
from kinoforge.validation.protocol import Severity

if TYPE_CHECKING:
    # Type-only: the runtime import lives inside assert_launch_capabilities
    # to avoid a module-scope import cycle (validation.checks.capabilities
    # imports kinoforge.core.config).
    from kinoforge.validation.checks.capabilities import Gap

_log = get_logger("orchestrator")


# ---------------------------------------------------------------------------
# C29 — ProvisionResult NamedTuple
# ---------------------------------------------------------------------------


class ProvisionResult(NamedTuple):
    """C29 — return shape of :func:`_provision_instance_and_build_backend`.

    ``hb_loop`` is ``None`` when ``start_heartbeat`` was not supplied (hosted-
    engine paths, ``heartbeat_interval_s <= 0``, callers that explicitly
    opted out), or when the closure raised. NamedTuple supports field-access
    and positional unpacking so callers that prefer ``instance, backend,
    hb_loop = ...`` keep working.

    Attributes:
        instance: The polled-ready compute instance.
        backend: The engine-built backend wired to ``instance``.
        hb_loop: A running HeartbeatLoop (``start()`` already called), or
            ``None`` when no closure was supplied (or it failed).
    """

    instance: Instance
    backend: GenerationBackend
    hb_loop: HeartbeatLoopProtocol | None


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
    from kinoforge._adapters import build_provider_for

    p = build_provider_for(cfg)
    if p is None:
        # Unreachable: build_provider_for returns None only when cfg.compute
        # is None, which the guard above already rejects. Belt-and-suspenders
        # for the type narrowing.
        raise RuntimeError(
            "build_provider_for returned None despite cfg.compute being set"
        )
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


_DIAG_BUCKET_DEFAULT = "<DIAG_BUCKET>"
_DIAG_REGION_DEFAULT = "us-west-2"


def _build_diagnostic_env(run_id: str) -> dict[str, str]:
    """Build the C28 diagnostic env overlay for an InstanceSpec.

    Reads ``KINOFORGE_DIAG_BUCKET`` (default ``<DIAG_BUCKET>``),
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
        Mapping of env-var name to value, ready to pass as the
        ``diagnostic_env`` overlay to ``build_instance_spec`` (compute-seam
        S1 Task 7: merged into ``InstanceSpec.env`` there, not a distinct
        spec field).
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
    except Exception:  # pragma: no cover — best-effort overlay: a missing
        # boto3 OR any cred-chain fault must degrade to "no AWS creds in
        # the pod env" (diag upload disabled), never block provisioning.
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
    cancel_token: CancelToken | None = None,
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
        cancel_token: C29 cooperative cancellation. Forwarded into
            ``provisioner.provision`` → ``engine.provision`` →
            ``engine.wait_for_ready`` so a boot-phase reap raises ``Cancelled``
            cleanly. Default ``None`` preserves pre-C29 behaviour.
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
        cancel_token=cancel_token,
    )
    write_marker(
        marker,
        instance.id,
        capability_key_hex,
        engine.name,
        time.time(),
    )


def _warm_attach_install(
    *,
    claim_holder: _LazyClaim,
    ledger: Ledger,
    record_then_install: Callable[[Instance], None],
    instance: Instance,
) -> None:
    """Install the session-claim for a caller-supplied warm pod, recording first when needed.

    Cold-boot paths invoke ``_record_then_install`` via
    ``_provision_instance_and_build_backend``'s ``on_instance_created``
    callback, which records the entry to the ledger BEFORE entering
    ``hold_until_first_tick`` — so the C29 HeartbeatLoop's strict
    ``ledger.touch(heartbeat_thread_tick=...)`` finds an entry to mutate
    and the post-yield poll releases as soon as the first tick lands.

    Warm-attach paths used to skip that callback and call
    ``claim_holder.install`` directly. Under
    :class:`~kinoforge.core.ephemeral.EphemeralSession` STRICT_POLICY
    the in-memory ledger is fresh (the caller-supplied pod was
    provisioned by an earlier CLI process), so the absent entry made
    every HeartbeatLoop tick a silent no-op and the post-yield
    ``hold_until_first_tick`` polled until ``claim_ttl`` elapsed
    (``boot_timeout_s + 2*heartbeat_interval_s``). This helper closes
    the gap by routing through ``record_then_install`` when no entry is
    visible from this process's ledger view; the existing-entry path
    keeps the prior behaviour bit-identical for non-ephemeral runs and
    for ephemeral runs that already see the entry from disk bootstrap.
    """
    if ledger.read(instance.id) is None:
        record_then_install(instance)
    else:
        claim_holder.install(instance)


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


def _placement_summary(instance: Instance) -> str:
    """Return a one-line identity of what was actually booked.

    Kept to what an :class:`Instance` really holds — provider plus whatever
    selection tags the provider chose to record. An operator reading a cap
    violation needs to tell "my cap is too low" from "this went somewhere I
    did not intend", and only the identity distinguishes them.

    Args:
        instance: The launched instance.

    Returns:
        e.g. ``"sku=A100:1, cloud=lambda, provider=skypilot"``.
    """
    parts = [
        f"{key}={instance.tags[key]}"
        for key in ("sku", "cloud", "region", "accelerators")
        if instance.tags.get(key)
    ]
    parts.append(f"provider={instance.provider}")
    return ", ".join(parts)


def _strip_reserved_tags(
    tags: dict[str, str],
    logger: logging.Logger = _log,
) -> dict[str, str]:
    """Drop kinoforge-reserved keys from caller-supplied *tags*.

    compute-seam S5. Only :data:`~kinoforge.core.lifecycle.LAUNCH_PHASE_TAG` is
    reserved today, and it is reserved because the ledger's same-key collapse
    reads it as ground truth: a real row carrying ``kf_launch_phase=launching``
    is indistinguishable from a provisional one, and the collapse would refuse
    forever. Caller tags reach the real row through ``spec.tags``, so a config
    could otherwise set it.

    Dropping rather than raising: an unknown tag is not worth failing a launch
    over, and the WARNING names the key so it is diagnosable.

    Args:
        tags: Caller-supplied tags. Not mutated.
        logger: Injected for testability.

    Returns:
        A new dict with every reserved key removed.
    """
    if LAUNCH_PHASE_TAG not in tags:
        return dict(tags)
    logger.warning(
        "dropping reserved tag %r=%r from caller tags: kinoforge writes it on "
        "the pre-launch provisional row and the ledger's collapse depends on "
        "only that row carrying it",
        LAUNCH_PHASE_TAG,
        tags[LAUNCH_PHASE_TAG],
    )
    return {key: value for key, value in tags.items() if key != LAUNCH_PHASE_TAG}


def _record_provisional_row(
    *,
    ledger: Any,  # noqa: ANN401 — Ledger, or any object exposing record/forget
    run_id: str,
    provider_name: str,
    tags: dict[str, str],
    max_age_s: int,
    now: float,
    logger: logging.Logger = _log,
) -> str | None:
    """Write the durable pre-launch row and return its id.

    compute-seam S5 generalises Brief 1's SkyPilot-only fix (finding F12). A
    ``create_instance`` call is multi-minute on every cloud provider, and until
    it returns there is no durable record of the resource it may already have
    created: a SIGKILL inside it leaves a billing pod, cluster or app that no
    kinoforge command can see.

    The row is keyed by the CLIENT-side id (``run_id``), which is not the
    provider's id anywhere but SkyPilot — on RunPod it is the pod NAME, on
    Modal the app run id. Task 6 teaches ``cli/_reconcile`` to adopt a
    ``launching`` row by name before it is allowed to forget one; until then
    the row is durable but the reconciler cannot yet act on it.

    Never raises: bookkeeping must not be able to fail a launch that would
    otherwise succeed. A fault forfeits F12 protection for this launch only,
    and is logged rather than passed.

    Args:
        ledger: The ledger to write to.
        run_id: The client-side id for this launch.
        provider_name: The provider about to be called.
        tags: Orchestrator tags to carry onto the row.
        max_age_s: Lifecycle snapshot, so the reaper can age the row out.
        now: Current epoch seconds (injected for testability).
        logger: Injected for testability.

    Returns:
        The row id, or None when nothing was written.
    """
    if not run_id:
        logger.warning(
            "F12: no run_id for this launch; skipping the pre-launch "
            "provisional row (a row with no id cannot be found or forgotten)"
        )
        return None
    provisional = Instance(
        id=run_id,
        provider=provider_name,
        status="starting",
        created_at=now,
        endpoints={},
        tags={
            # Stripped, not merely overridden: direct callers (the golden
            # harness, the live smokes) reach this helper without going through
            # _provision_instance_and_build_backend's strip, and the WARNING is
            # the only signal a config is trying to set a reserved key.
            **_strip_reserved_tags(tags, logger),
            # Shared constants, not literals: this tag is the ONLY thing that
            # distinguishes this row from the real one when the two share an id
            # (SkyPilot), so a rename here that missed
            # ``Ledger.forget_provisional`` would silently stop collapsing and
            # ship two rows per cluster.
            LAUNCH_PHASE_TAG: LAUNCH_PHASE_LAUNCHING,
            "kf_run_id": run_id,
            "kf_launched_at": repr(now),
        },
        cost_rate_usd_per_hr=0.0,
    )
    try:
        ledger.record(provisional, max_age_s=max_age_s)
    except Exception:  # noqa: BLE001 — ledger fault must not block a launch
        logger.warning(
            "F12 provisional ledger record failed for %r; this launch has no "
            "pre-launch orphan protection until it completes",
            run_id,
            exc_info=True,
        )
        return None
    return run_id


def _mint_deploy_run_id(now: float | None = None) -> str:
    """Return a fresh client-side run id for a one-shot ``deploy()``.

    ``deploy`` takes no ``run_id`` from its caller the way ``deploy_session``
    does, and an EMPTY one is not an option once the provisional row exists:

    * :func:`_record_provisional_row` refuses to write a row it cannot key, so
      an empty run id silently forfeits F12 protection on exactly the path most
      likely to be interrupted;
    * providers fall back to a SHARED constant name when ``spec.run_id`` is
      empty (``"kinoforge-pod"`` on RunPod, ``"skypilot-cluster"`` on SkyPilot),
      and ``cli/_reconcile._adopt_or_age_out`` adopts a launching row by
      matching its id against that name — a constant would match every
      concurrent deploy's resource, not this one's.

    Local time, per project convention. The 6 hex characters are what make two
    deploys inside the same second distinguishable; without them the row key and
    the provider-side name would collide and the reconciler could adopt the
    wrong resource. Lowercase/dash-only so it is a legal SkyPilot cluster name
    and RunPod pod name.

    Args:
        now: Epoch seconds (injected for testability). Defaults to now.

    Returns:
        e.g. ``"kinoforge-deploy-20260904-141233-9f3ac1"``.
    """
    stamp = datetime.fromtimestamp(time.time() if now is None else now).strftime(
        "%Y%m%d-%H%M%S"
    )
    return f"kinoforge-deploy-{stamp}-{uuid.uuid4().hex[:6]}"


def _forget_provisional_row(
    ledger: Any,  # noqa: ANN401
    row_id: str | None,
    logger: logging.Logger = _log,
) -> None:
    """Remove the provisional row after a create that PROVABLY booked nothing.

    A create that never produced a resource must not leave a row whose
    ``est_spend`` inflates forever (``cli/_reconcile``'s "$210 phantom pod").

    Ruling C1 narrowed who reaches this. The caller no longer calls it for any
    raise — only for the exception types
    :func:`_nothing_booked_error_types` returns, because a raise on its own is
    not evidence that the provider created nothing, and deleting the row when
    it did is the F12 hole in reverse.

    Uses the phase-scoped delete rather than ``Ledger.forget``, which matches on
    id alone. On the same-key shape (SkyPilot: the cluster name IS the
    ``run_id``) a real row can already exist under this id from an EARLIER
    successful launch that reused the run id — a re-run with an explicit
    ``--run-id``, or a second ``create_instance`` against one cluster. Forgetting
    by id there would delete a LIVE cluster's only durable handle: the same
    zero-row hole the success path closes, in the mirror branch. Phase scoping
    removes only the ``launching`` row this launch wrote.

    No ``real_id`` precondition: the create raised, so requiring a real row
    would strand the very ghost this call exists to clear.

    Never raises — bookkeeping must not replace the exception that explains why
    the launch failed.

    Args:
        ledger: The ledger holding the row.
        row_id: The provisional row id, or None when none was written.
        logger: Injected for testability.
    """
    if not row_id:
        return
    try:
        ledger.forget_provisional(row_id)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a launch
        logger.warning(
            "F12 provisional ledger forget failed for %r; a stale 'launching' "
            "row may linger for a launch that never produced a resource",
            row_id,
            exc_info=True,
        )


def _record_real_row(
    ledger: Any,  # noqa: ANN401 — Ledger, or any object exposing record
    instance: Instance,
    *,
    lifecycle: Lifecycle,
    logger: logging.Logger = _log,
) -> None:
    """Write the durable row for a launched instance, best-effort.

    The ``deploy()`` counterpart of ``deploy_session``'s ``_record_then_install``,
    and it carries the same lifecycle snapshot so ``kinoforge status`` can
    surface the policy without re-loading the YAML. ``max_age_s`` mirrors the
    spec naming; the source attribute is ``Lifecycle.max_lifetime_s``.

    Never raises, for the same reason every other ledger call on this path does
    not: the instance is created and billing by the time this runs, and
    ``deploy``'s error path DESTROYS the instance — so letting a store fault
    escape here would tear down a healthy launch over bookkeeping. A fault
    leaves the provisional row in place, which the reconciler can still resolve.

    Args:
        ledger: The ledger to write to.
        instance: The launched instance.
        lifecycle: Effective lifecycle guardrails for this launch.
        logger: Injected for testability.
    """
    try:
        ledger.record(
            instance,
            idle_timeout_s=int(lifecycle.idle_timeout_s),
            max_age_s=int(lifecycle.max_lifetime_s),
        )
    except Exception:  # noqa: BLE001 — a ledger fault must not destroy a live pod
        logger.warning(
            "ledger.record failed for %r; the launch stands and its pre-launch "
            "'launching' row is left for cli/_reconcile to resolve",
            instance.id,
            exc_info=True,
        )


def _nothing_booked_error_types(
    provider: Any,  # noqa: ANN401 — duck-typed ComputeProvider
    logger: logging.Logger = _log,
) -> tuple[type[BaseException], ...]:
    """Return the create errors that PROVE *provider* booked nothing.

    compute-seam S5, ruling C1. The failure path may delete the pre-launch
    provisional row ONLY for these; every other exception leaves the row for
    ``cli/_reconcile`` to resolve. See
    :meth:`~kinoforge.core.interfaces.ComputeProvider.nothing_booked_errors`
    for why the default is the conservative one.

    Two sources, unioned:

    * :class:`~kinoforge.core.errors.CapacityError`, which core owns. It is
      what ``_create_with_capacity_wait`` re-raises once the window expires
      with nothing bookable, and "no capacity" is by construction "nothing was
      created". Naming it here rather than making every provider declare it
      keeps the portable guarantee portable.
    * Whatever the provider declares. This is the seam that lets SkyPilot's
      ``PreLaunchRateCapExceeded`` — raised before ``sky.launch`` runs, and
      living in ``kinoforge.providers.skypilot`` where ``kinoforge.core`` may
      not import it at module scope — reach this decision without an import.

    Defensive by design, because ``provider`` is typed ``Any``: a missing
    method, a raising one, or a declaration that is not a tuple of exception
    types all degrade to the portable pair. That direction is safe — an
    undeclared error keeps the row, and a kept row is reconcilable while a
    deleted one is not.

    Args:
        provider: The resolved compute provider whose ``create_instance`` is
            about to be called.
        logger: Injected for testability.

    Returns:
        The exception types the failure path may forget the row for.
    """
    portable: tuple[type[BaseException], ...] = (CapacityError,)
    declare = getattr(provider, "nothing_booked_errors", None)
    if not callable(declare):
        return portable
    try:
        declared = declare()
    except Exception:  # noqa: BLE001 — a broken declaration must not fail a launch
        logger.debug(
            "provider %r could not declare its nothing-booked errors; assuming none",
            getattr(provider, "name", provider),
            exc_info=True,
        )
        return portable
    if not isinstance(declared, tuple | list):
        # Includes the MagicMock case: a stand-in that answers every attribute
        # returns a Mock here, and treating that as "declares everything" would
        # silently restore the unconditional forget this ruling removed.
        return portable
    return portable + tuple(
        exc
        for exc in declared
        if isinstance(exc, type) and issubclass(exc, BaseException)
    )


def _collapse_provisional_row(
    ledger: Any,  # noqa: ANN401
    provisional_id: str | None,
    instance_id: str,
    logger: logging.Logger = _log,
) -> None:
    """Collapse the provisional row onto the real one, best-effort.

    Called on the success path only, AFTER ``on_instance_created`` has had its
    chance to write the real row. Delegates the decision to
    :meth:`~kinoforge.core.lifecycle.Ledger.forget_provisional` rather than
    probing and then forgetting here, because the two shapes this has to serve
    make a two-step version unsafe:

    * ``provisional_id != instance_id`` (RunPod, Modal — the provider assigns
      the id): two distinct keys, and a plain ``forget`` is correct.
    * ``provisional_id == instance_id`` (SkyPilot — the cluster name IS the
      run id): one key holding two rows. ``Ledger.forget`` matches on id alone
      and would delete BOTH, leaving a live billing cluster invisible.

    ``forget_provisional`` handles both under a single lock: it removes the row
    only when it is phase-tagged ``launching`` AND a real row already exists to
    survive it, so there is never a window with zero rows for a resource that
    has already been created.

    Never raises: the instance is live and billing by the time this runs, so an
    exception here would fail a launch that actually succeeded.

    Args:
        ledger: The ledger holding both rows.
        provisional_id: The provisional row id, or None when none was written.
        instance_id: The id the provider returned.
        logger: Injected for testability.
    """
    if not provisional_id:
        return
    try:
        collapsed = ledger.forget_provisional(provisional_id, real_id=instance_id)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a launch
        logger.warning(
            "F12 provisional ledger collapse failed for %r; the provisional "
            "'launching' row may linger alongside the real record",
            provisional_id,
            exc_info=True,
        )
        return
    if not collapsed:
        # A refusal is the DESIGNED outcome when the real row never landed, and
        # keeping the row is right — but it is indistinguishable from a healthy
        # launch unless it says so. Silence here is how a permanent duplicate
        # (or a permanently stale 'launching' row on a live instance) reaches
        # production with nothing to grep for.
        logger.warning(
            "F12: collapse refused for %r; no real row under %r — the "
            "'launching' row remains, so this instance may show up as "
            "provisional (or twice) in kinoforge list",
            provisional_id,
            instance_id,
        )


def _enforce_rate_cap(
    *,
    provider: ComputeProvider,
    instance: Instance,
    cap: float,
    logger: logging.Logger = _log,
) -> float | None:
    """Destroy *instance* and raise when it bills above *cap*.

    Args:
        provider: The provider that launched it.
        instance: The freshly created instance.
        cap: ``placement.max_usd_per_hr``.
        logger: Injected for testability.

    Returns:
        The realized rate, or None when it could not be read on a provider
        whose catalog price already bounded the booking.

    Raises:
        RateCapExceeded: The realized rate exceeds *cap*, or could not be read
            on a provider that chooses its own SKU. The instance is destroyed
            first; a teardown failure is folded into the same error rather
            than replacing it.
    """
    declared = provider.capabilities()
    realized = provider.realized_rate(instance)
    if realized is not None and realized <= cap:
        return realized
    if realized is None and Capability.RATE_READBACK not in declared:
        # The catalog already bounded this before booking; an unreadable
        # readback is a missing nicety, not a money risk.
        logger.warning(
            "[rate-cap] %s: realized rate unreadable; the catalog price "
            "bounded this launch before it was booked",
            instance.id,
        )
        return None

    summary = _placement_summary(instance)
    teardown_error: str | None = None
    try:
        provider.destroy_instance(instance.id)
    except Exception as exc:  # noqa: BLE001 — folded into the raised error
        teardown_error = repr(exc)
        logger.error(
            "[rate-cap] %s: teardown FAILED after a cap violation; the "
            "instance is still billing and its ledger row is the only "
            "handle on it",
            instance.id,
        )
    err = RateCapExceeded(
        realized=realized,
        cap=cap,
        instance_id=instance.id,
        placement_summary=summary,
    )
    if teardown_error is not None:
        err.args = (f"{err.args[0]}\n  TEARDOWN ALSO FAILED: {teardown_error}",)
    raise err


_CAPACITY_RETRY_INTERVAL_S: float = 25.0


def _create_with_capacity_wait[T](
    *,
    create: Callable[[], T],
    capacity_wait_s: float,
    retry_interval_s: float = _CAPACITY_RETRY_INTERVAL_S,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry ``create`` while it raises CapacityError.

    Capacity is fluid: a currently-empty pool can free up seconds later. Retry
    ``create`` on CapacityError until ``capacity_wait_s`` elapses, then re-raise
    the last one. Non-CapacityError propagates immediately.
    ``capacity_wait_s <= 0`` fails on the first miss, which makes this a
    pass-through on every provider that declares no wait window.

    compute-seam S4 dropped the ``find_offers`` parameter: re-querying the
    catalog between attempts is now the enumerating provider's own business,
    inside its ``create_instance``.

    Args:
        create: Creates the instance; may raise CapacityError.
        capacity_wait_s: Deadline; 0 disables retry.
        retry_interval_s: Sleep between attempts.
        clock: Injected clock (defaults to RealClock).
        sleep: Injected sleep seam.

    Returns:
        The created instance (whatever ``create`` returns).

    Raises:
        CapacityError: Deadline elapsed with sustained capacity exhaustion.
    """
    the_clock = clock if clock is not None else RealClock()
    start = the_clock.now()
    while True:
        try:
            return create()
        except CapacityError:
            if the_clock.now() - start >= capacity_wait_s:
                raise
            _log.warning(
                "[capacity-wait] no capacity yet; retry in %.0fs (waited %.0fs / %.0fs)",
                retry_interval_s,
                the_clock.now() - start,
                capacity_wait_s,
            )
            sleep(retry_interval_s)


_READY_POLL_INTERVAL_S: float = 2.0


def _wait_for_provider_ready(
    provider: ComputeProvider,
    instance: Instance,
    *,
    boot_timeout_s: float,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval_s: float = _READY_POLL_INTERVAL_S,
) -> str:
    """Poll ``provider.get_instance`` until the pod reports ``ready``.

    Only the status is returned: ``created_at``, ``tags``, and
    ``cost_rate_usd_per_hr`` are authoritative on the ``create_instance``
    result and would be clobbered by the impoverished Instances that
    RunPod/SkyPilot list APIs return (Stage E live smoke 2026-06-18).

    Args:
        provider: Provider to poll.
        instance: The just-created instance (its status seeds the loop).
        boot_timeout_s: Give-up deadline. A pod that never leaves
            "starting" (image-pull hang, wedged host) otherwise holds the
            CLI forever while billing (audit B5).
        clock: Injected clock (defaults to RealClock).
        sleep: Injected sleep seam.
        interval_s: Delay between polls. Pre-fix ``deploy()`` had none and
            hammered the provider API in a hot loop.

    Returns:
        The terminal status (always ``"ready"``).

    Raises:
        ProvisionTimeout: ``boot_timeout_s`` elapsed before ready.
    """
    the_clock = clock if clock is not None else RealClock()
    deadline = the_clock.now() + boot_timeout_s
    status = instance.status
    while status != "ready":
        if the_clock.now() >= deadline:
            raise ProvisionTimeout(
                f"instance {instance.id!r} never reached ready within "
                f"{boot_timeout_s:.0f}s (last status={status!r})"
            )
        sleep(interval_s)
        status = provider.get_instance(instance.id).status
    return status


def _build_start_heartbeat_closure(
    *,
    ledger: Ledger,
    provider: ComputeProvider,
    interval: float,
    util_endpoint: object,
    cancel_token: CancelToken | None,
    provider_kind: str | None,
    stall_window_s: float | None,
    stall_gpu_threshold: float,
    stall_cpu_threshold: float,
    restart_loop_window_s: float | None,
    restart_loop_uptime_threshold_s: float,
    factory: Callable[..., HeartbeatLoopProtocol],
) -> Callable[[Instance], HeartbeatLoopProtocol]:
    """C29 — closure that builds + starts a HeartbeatLoop given an instance.

    Captures every HeartbeatLoop kwarg except ``instance_id``. The closure is
    passed into :func:`_provision_instance_and_build_backend` and invoked
    right after the RunPod-status poll succeeds, so the loop ticks throughout
    ``engine.provision`` / ``wait_for_ready`` rather than waiting until
    ``deploy_session`` resumes after provision returns. Steady-state lifetime
    and the matching ``stop()`` remain owned by ``deploy_session``'s finally
    block.

    Args:
        ledger: Heartbeat ledger.
        provider: ComputeProvider whose ``heartbeat`` callback the loop drives.
        interval: Tick interval in seconds.
        util_endpoint: Optional :class:`UtilSnapshotEndpoint`; ``None`` falls
            back to the heartbeat-only path.
        cancel_token: Shared cancel token; the loop ``raise_if_set``s before
            each tick to support cooperative shutdown.
        provider_kind: Friendly name forwarded into the loop's util adapter.
        stall_window_s: Window over which the STALL_REAP predicate watches
            GPU/CPU util; ``None`` disables the predicate.
        stall_gpu_threshold: Below this GPU%, the window counts as low.
        stall_cpu_threshold: Below this CPU%, the window counts as low.
        restart_loop_window_s: Window over which RESTART_LOOP_REAP watches
            container uptime; ``None`` disables the predicate.
        restart_loop_uptime_threshold_s: Below this uptime, the window counts.
        factory: HeartbeatLoop constructor (or test spy).

    Returns:
        A 1-arg closure ``(Instance) -> HeartbeatLoopProtocol`` that builds
        and ``start()``-s a fresh loop bound to that instance's id.
    """

    def start_heartbeat(inst: Instance) -> HeartbeatLoopProtocol:
        loop = factory(
            ledger=ledger,
            provider=provider,
            instance_id=inst.id,
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
        loop.start()
        return loop

    return start_heartbeat


def assert_launch_capabilities(
    cfg: Config,
    *,
    launch: Launch | None,
    logger: logging.Logger = _log,
) -> list[Gap]:
    """Re-evaluate capability gaps against the authoritative workload shape.

    Load-time ``infer_shape`` reads nothing and returns SERVER
    unconditionally — it refuses to guess (see its docstring for why every
    heuristic it could use guesses in the dangerous direction). Here
    ``launch`` is the authoritative, rendered thing, so a spec that really
    is BATCH is caught rather than mis-reported, and the mismatch is logged.

    Args:
        cfg: The loaded Config.
        launch: The rendered provision's launch. None -> BATCH, because an
            engine that declares no launch starts nothing that outlives the
            provision.
        logger: Injected for testability.

    Returns:
        The gaps at the authoritative shape (WARN-severity ones only; ERROR
        gaps raise).

    Raises:
        ValidationError: A guardrail the cfg asserts has no declared
            capability and no substitute at the authoritative shape.
    """
    # Function-local import: kinoforge.validation.checks.capabilities imports
    # kinoforge.core.config, and core/orchestrator.py importing back into
    # validation at module scope risks a cycle. noqa: PLC0415 — avoids an
    # import cycle, matching the pattern used elsewhere in this plan.
    from kinoforge.validation.checks.capabilities import (  # noqa: PLC0415
        evaluate_capability_gaps,
        infer_shape,
    )

    shape = WorkloadShape.BATCH if launch is None else WorkloadShape.SERVER
    inferred = infer_shape(cfg)
    if inferred is not shape:
        logger.warning(
            "[capabilities] shape inference miss: load-time inferred %s, "
            "spec.launch says %s — the launch-time shape wins",
            inferred.value,
            shape.value,
        )
    gaps: list[Gap] = evaluate_capability_gaps(cfg, shape)
    fatal = [g for g in gaps if g.severity is Severity.ERROR]
    if fatal:
        detail = "; ".join(f"{g.field} needs {g.missing.value}" for g in fatal)
        raise ValidationError(
            f"{cfg.compute.provider if cfg.compute else '?'} cannot enforce "
            f"guardrails this cfg asserts at shape={shape.value}: {detail}"
        )
    for gap in gaps:
        logger.warning("[capabilities] %s: %s", gap.field, gap.detail)
    return gaps


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
    on_rate_verified: Callable[[Instance], None] | None = None,
    cancel_token: CancelToken | None = None,
    start_heartbeat: Callable[[Instance], HeartbeatLoopProtocol] | None = None,
    capacity_wait_s: float | None = None,
    provisional_ledger: Any | None = None,  # noqa: ANN401 — Ledger, duck-typed
) -> ProvisionResult:
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
            key collision, EXCEPT for kinoforge-reserved keys, which are
            dropped with a warning (see :func:`_strip_reserved_tags`).
        on_instance_created: Optional callback fired exactly once,
            immediately after ``create_instance`` returns, with the
            freshly-created ``Instance``. B7 uses this seam to enter
            ``hold_until_first_tick`` before ``engine.provision`` runs.
        on_rate_verified: compute-seam S4 — optional callback fired at most
            once, with the instance carrying the REALIZED hourly rate, after
            the cap check passes. Not fired when the rate was unreadable (the
            recorded catalog number is then the best available and must
            survive). The row was already written by ``on_instance_created``,
            so this corrects it in place rather than recording a second one:
            ``Ledger.record`` appends.
        cancel_token: C29 cooperative cancellation. Forwarded into
            ``_provision_compute_once`` so a boot-phase reap raises
            ``Cancelled`` from inside ``engine.wait_for_ready``. Task 5 adds
            the matching ``except Cancelled`` clause that destroys the pod.
            Default ``None`` preserves pre-C29 behaviour.
        start_heartbeat: C29 closure that constructs + starts a
            ``HeartbeatLoop`` given the just-readied ``Instance``. Invoked
            right after the RunPod status poll succeeds and BEFORE
            ``engine.provision`` runs, so STALL_REAP / RESTART_LOOP_REAP
            predicates tick throughout the boot phase. ``None`` skips the
            invocation and the returned ``hb_loop`` is also ``None``; a
            closure that raises also falls through to ``hb_loop=None``
            (logged) — the late-start path in ``deploy_session`` handles the
            caller-supplied warm-pod recovery.
        capacity_wait_s: Seconds to keep re-querying offers and retrying
            create on ``CapacityError`` before giving up. Provider-scoped
            (compute-seam S1) and non-zero for RunPod only.

            **This is the single place ``None`` is resolved.** ``None``
            (the default) derives the window from *cfg* via
            :func:`kinoforge._adapters.build_capacity_wait_for`, exactly
            as this function derived it from ``cfg.lifecycle()`` before
            the window moved into the provider namespace. Pass an
            explicit float only to override; ``0.0`` means "fail on the
            first miss" and must be stated, never inherited from a
            forgotten keyword — a caller that silently got ``0.0`` would
            lose RunPod's capacity retry with no signal at all.
        provisional_ledger: compute-seam S5 (finding F12) — the ledger that
            receives a durable ``kf_launch_phase=launching`` row keyed by
            ``run_id`` BEFORE ``create_instance`` is called, so a kill inside
            the multi-minute create still leaves a handle on whatever the
            provider may already have booked. The row is collapsed onto the
            real row on success, and forgotten on failure ONLY for the errors
            that prove nothing was booked (ruling C1 — see
            :func:`_nothing_booked_error_types`); any other exception leaves
            the row for ``cli/_reconcile`` to adopt or age out.
            ``None`` (the default) disables the row entirely.

            Duck-typed (hence ``Any``): ``kinoforge.core`` must not import a
            concrete store, and every caller passes a
            :class:`~kinoforge.core.lifecycle.Ledger`. TWO methods are used, and
            ``forget`` is NOT one of them — a fake built around ``forget`` loses
            BOTH cleanup paths, and does so silently, because every call below
            is wrapped:

            * ``record(instance, *, max_age_s=int)`` — the pre-launch write.
            * ``forget_provisional(provisional_id) -> bool`` — the FAILURE path
              (:func:`_forget_provisional_row`), with no ``real_id``: the create
              raised, so requiring a real row would strand the very ghost the
              call exists to clear. Still not ``forget``, because a real row can
              exist under this id from an earlier launch that reused the run id.
            * ``forget_provisional(provisional_id, *, real_id) -> bool`` — the
              SUCCESS path (:func:`_collapse_provisional_row`). On SkyPilot the
              provisional row and the real row share a key (the cluster name IS
              the run id), so a plain forget would delete both.

            Two further expectations that only bite a hand-rolled fake, since
            :class:`~kinoforge.core.lifecycle.Ledger` satisfies them for free:

            * The return value is READ, not discarded. ``False`` from the
              success-path call means the collapse was refused and is logged as
              a warning; a fake that returns ``None`` reports every healthy
              launch as a refusal.
            * The refusal check is a read over the rows the REAL row was written
              into — by ``on_instance_created``, through a *different* ledger
              object over the same store. A fake that keeps a private, unshared
              list therefore never sees the real row and refuses every collapse,
              leaving a permanently stale ``launching`` row on a live instance.

            Every call is best-effort and its exception is swallowed and logged,
            because bookkeeping must never fail a launch that would otherwise
            succeed.

    Returns:
        :class:`ProvisionResult` ``(instance, backend, hb_loop)`` —
        instance polled to ``ready``, backend constructed via
        ``engine.backend(instance, cfg_dict)``, hb_loop started or ``None``.

    Raises:
        CapacityError: The provider found nothing bookable for the cfg's
            placement, or every candidate it tried lacked capacity.
        AuthError: A var in ``rendered.env_required`` is absent from *creds*.
            Raised before ``create_instance`` is called.
        ProvisionFailed: Engine boot script crashed; instance already destroyed.
        ProvisionTimeout: Ready check timed out; instance already destroyed.
        CapabilityMismatch: Engine rejected its own capability key; instance destroyed.
        ValidationError: Two distinct sites. (1) ``assert_launch_capabilities``
            finds an ERROR-severity capability gap. It runs once, after
            ``render_provision`` (it needs the authoritative launch) and
            above the offer-retry / capacity-wait loops — so it raises before
            ``find_offers`` or ``create_instance`` are reached and nothing
            exists yet to destroy. (2) Spec validation fails inside ``_provision_compute_once``
            after the instance is already created — that instance is destroyed
            before the exception propagates.
    """
    # compute-seam S5 — ``kf_launch_phase`` is RESERVED. It is the only thing
    # distinguishing the provisional row from the real one when the two share an
    # id, and caller tags flow into ``spec.tags`` and from there onto the real
    # ``Instance.tags`` (SkyPilot spreads them verbatim). A config that set this
    # key would make the REAL row read as provisional: the collapse precondition
    # would never pass, leaving a permanent two-row state with nothing raising.
    # Stripping it here — above BOTH the provisional write and ``_build_spec`` —
    # is what makes the phase tag a fact about the launch rather than about the
    # config.
    tags = _strip_reserved_tags(dict(tags or {}))
    lifecycle = cfg.lifecycle()
    image = cfg.compute.image if cfg.compute is not None else ""
    # THE single resolution site for the capacity window. It sits beside the
    # other cfg-derived values on purpose: before compute-seam S1 this
    # function read the window off cfg.lifecycle() itself, so deriving it
    # here restores that shape with the namespace as the new source. Every
    # caller supplies cfg, so `None` is always resolvable — which is what
    # makes a forgotten keyword impossible to turn into a silent 0.0.
    if capacity_wait_s is None:
        from kinoforge._adapters import build_capacity_wait_for

        capacity_wait_s = build_capacity_wait_for(cfg)
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

    # Capability re-check against the AUTHORITATIVE rendered launch, hoisted
    # above the offer-retry / capacity-wait loops on purpose: it depends only
    # on (cfg, rendered.launch), neither of which varies per offer. Inside
    # _build_spec it re-raised the same ERROR and re-emitted every WARN line
    # once per offer AND again per capacity-wait retry, which reads to an
    # operator as several distinct guardrail problems instead of one.
    assert_launch_capabilities(cfg, launch=rendered.launch)

    def _build_spec() -> InstanceSpec:
        return build_instance_spec(
            cfg=cfg,
            rendered=rendered,
            engine_name=resolved_engine.name,
            key_hash=key_hash,
            image=image,
            lifecycle=lifecycle,
            env=rendered_env,
            run_id=run_id,
            tags=tags,
            diagnostic_env=_build_diagnostic_env(run_id)
            if cfg.diagnostic_mode
            else None,
        )

    # 2026-07-07 capacity-wait: re-query offers + retry create on CapacityError
    # (empty offers OR every offer exhausted at create time) until
    # capacity_wait_s elapses, then re-raise clean. Rides transient RunPod
    # capacity droughts instead of failing the run on the first miss. S1 made
    # the window provider-scoped — see build_capacity_wait_for.
    # compute-seam S4: the orchestrator hands over ONE spec and the provider
    # selects for itself — RunPod from its catalog, Modal from its GPU table,
    # SkyPilot by handing constraints to its optimizer. The capacity-WAIT
    # window survives because capacity is fluid: a provider that raises
    # CapacityError now may succeed in 25 s.
    #
    # F12 — the row goes in ABOVE the capacity-wait loop, not inside it:
    # ``Ledger.record`` appends, so one write per attempt would leave N rows
    # for one launch and every reader would pick whichever it found first.
    provisional_id = (
        _record_provisional_row(
            ledger=provisional_ledger,
            run_id=run_id,
            provider_name=getattr(resolved_provider, "name", "unknown"),
            tags=dict(tags or {}),
            max_age_s=int(lifecycle.max_lifetime_s),
            now=time.time(),
        )
        if provisional_ledger is not None
        else None
    )
    # Ruling C1 (2026-09-03), which OVERRIDES the S5 plan's Task 4 text. Read
    # before the create so a fault in the declaration cannot land inside the
    # failure path itself.
    nothing_was_booked = _nothing_booked_error_types(resolved_provider)
    try:
        instance = _create_with_capacity_wait(
            create=lambda: resolved_provider.create_instance(_build_spec()),
            capacity_wait_s=capacity_wait_s,
        )
    except nothing_was_booked:
        # These, and ONLY these, prove no resource exists: the capacity window
        # expiring with nothing bookable, plus whatever the provider declares
        # (SkyPilot's pre-launch cap refusal, raised before sky.launch runs).
        # A create that never produced a resource must not leave a row whose
        # est_spend inflates forever (cli/_reconcile's "$210 phantom pod").
        _forget_provisional_row(provisional_ledger, provisional_id)
        raise
    except BaseException:
        # A raise does not prove the provider booked nothing. Leave the row;
        # cli/_reconcile._adopt_or_age_out resolves it after the grace window
        # — adopting if the resource exists, ageing it out if it does not.
        #
        # Three shapes make this the only safe default, all of them live:
        # ``sky.launch`` raising out of a failed setup script leaves an UP
        # cluster (providers/skypilot/__init__.py has no handler for it); the
        # tunnel branch's best-effort ``sky.down`` swallows its own exception,
        # so "we tore it down" is a hope rather than a fact; and a Ctrl-C
        # lands here as a BaseException while SkyPilot's API server goes on
        # creating the cluster. Forgetting the row in any of those leaves a
        # live, billing resource with ZERO ledger rows — the exact F12 hole
        # this branch exists to close.
        raise
    # B7 — acquire the cooperative session-claim lock now that instance.id is
    # known, BEFORE engine.provision runs. The callback enters the outer
    # hold_until_first_tick context; release happens on the _LazyClaim
    # holder's __exit__ in deploy_session.
    if on_instance_created is not None:
        on_instance_created(instance)
    # Order is load-bearing: the REAL row is written first, so no window exists
    # in which a kill loses both rows. The collapse is therefore CONDITIONAL on
    # the real row actually being there — ``on_instance_created`` is optional,
    # and deploy_session's ``_record_then_install`` swallows its own
    # ``ledger.record`` failure and returns normally. Dropping the provisional
    # row in either case would delete the only durable handle on an instance
    # that is live and billing, which is precisely the state F12 exists to make
    # impossible. That condition and the delete live together inside
    # ``Ledger.forget_provisional``, under one lock — see
    # ``_collapse_provisional_row`` for why the same-key (SkyPilot) shape makes
    # a probe-then-forget version unsafe no matter how it is ordered here.
    if provisional_ledger is not None:
        _collapse_provisional_row(provisional_ledger, provisional_id, instance.id)
    # compute-seam S4: the cap is verified against what was LAUNCHED, not
    # filtered against a catalog the chooser may never have consulted. Runs
    # after on_instance_created (so a failed teardown still leaves a ledger row
    # pointing at the live instance) and before _wait_for_provider_ready and
    # engine.provision, so a violation is torn down before the expensive part
    # of a boot — on RunPod and Modal. On SkyPilot `sky.launch` already ran
    # Task.setup by the time it returned, so there the teardown discards work
    # that has been done; that is the accepted trade (design §14) and a
    # pre-launch estimate is an S5 follow-up.
    realized_rate = _enforce_rate_cap(
        provider=resolved_provider,
        instance=instance,
        cap=cfg.placement().max_usd_per_hr,
    )
    if realized_rate is not None:
        # S4 closes F4 here: every downstream surface — the ledger row,
        # est_spend, `kinoforge list`, every lifecycle.budget computation —
        # reads cost_rate_usd_per_hr, and until now that was the number
        # kinoforge ASKED for. An unreadable rate leaves the recorded catalog
        # number alone rather than zeroing a pod that is very much billing.
        instance = dataclasses.replace(instance, cost_rate_usd_per_hr=realized_rate)
        if on_rate_verified is not None:
            on_rate_verified(instance)
    # Status-only polling: preserve endpoints + tags from create_instance.
    # provider.get_instance(id) re-queries the API but the GraphQL `pod` query
    # only returns id/desiredStatus/imageName — endpoints + ports tag are
    # stripped. Without the replace, instance.endpoints goes from
    # populated-by-_create_pod to empty-by-_pod_to_instance, and the
    # downstream wait_for_ready raises ProvisionFailed immediately.
    instance = dataclasses.replace(
        instance,
        status=_wait_for_provider_ready(
            resolved_provider,
            instance,
            boot_timeout_s=lifecycle.boot_timeout_s,
        ),
    )

    # NEW — Layer Q: wire provider.get_instance onto engine before engine.provision
    resolved_engine.attach_get_instance(resolved_provider.get_instance)

    # 2026-07-07: give the engine a boot-liveness probe when the provider
    # supplies one (RunPod). Providers without one → None → no boot-stall check.
    _make_probe = getattr(resolved_provider, "make_boot_liveness_probe", None)
    resolved_engine.attach_boot_liveness_probe(
        _make_probe(instance) if _make_probe is not None else None
    )

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
            cancel_token=cancel_token,
        )
    except (ProvisionFailed, ProvisionTimeout, CapabilityMismatch, ValidationError):
        resolved_provider.destroy_instance(instance.id)
        raise
    except Cancelled:
        # C33-m moved heartbeat start to AFTER provision returns. Therefore
        # during the provision window, Cancelled can only originate from
        # operator Ctrl-C (no boot-phase heartbeat predicates can fire). The
        # destroy_instance call is the idempotent cleanup leg; provider
        # failures (RunPod 404 on an already-gone pod) are swallowed +
        # logged so Cancelled keeps propagating to the operator.
        try:
            resolved_provider.destroy_instance(instance.id)
        except Exception as destroy_exc:  # noqa: BLE001
            _log.warning(
                "C33-m: idempotent destroy after Cancelled raised %s for %s",
                destroy_exc,
                instance.id,
            )
        raise

    # C33-m: start the heartbeat loop AFTER provision completes. C29's
    # BEFORE-provision ordering was reverted on 2026-06-17 because the C25
    # B5a RunPod satisfier's ``podEditJob`` mutation (issued every 30 s)
    # triggers a container-level restart on the RunPod side, which makes
    # provisions infinite (Wan cold-boot cycled every ~31 s under Q4/Q(h)/
    # (l); succeeded under (m) with heartbeat_mode: none). See
    # tests/live/_c33_probe_m_evidence.json.
    #
    # Trade-off: STALL_REAP / RESTART_LOOP_REAP predicates can no longer
    # fire during provision (was C29's design intent). They still fire
    # post-boot. A heartbeat that prevents provision from completing cannot
    # help with stall detection during it. A closure failure falls through
    # to hb_loop=None so a bug in the heartbeat construction path never
    # blocks a fresh boot.
    hb_loop: HeartbeatLoopProtocol | None = None
    if start_heartbeat is not None:
        try:
            hb_loop = start_heartbeat(instance)
        except Exception:  # noqa: BLE001
            _log.exception(
                "C33-m: start_heartbeat closure failed for %s; falling through "
                "to late-start hb_loop construction in deploy_session",
                instance.id,
            )
            hb_loop = None

    backend = resolved_engine.backend(instance, cfg_dict)
    return ProvisionResult(instance=instance, backend=backend, hb_loop=hb_loop)


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
    capacity_wait_s: float | None = None,
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
        capacity_wait_s: Seconds to keep re-querying offers and retrying
            create on ``CapacityError`` before giving up (compute-seam
            S1). Forwarded verbatim — including ``None`` — to
            :func:`_provision_instance_and_build_backend`, which owns the
            sole ``None``-resolution site. ``None`` (the default) means
            "derive the window from *cfg*"; pass a float only to
            override.

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
    # compute-seam S5 (F12) — the ledger the ORCHESTRATOR writes the pre-launch
    # provisional row into, for EVERY provider. None only on hosted engines,
    # which have no create to protect. S5 Task 5 deleted SkyPilot's private
    # provider-side writer and the gate that kept the two apart, so this is now
    # the one and only writer of that row.
    _provisional_ledger: Ledger | None = None
    if resolved_engine.requires_compute:
        resolved_provider = _resolve_provider(cfg, provider)
        _provisional_ledger = Ledger(store=store)

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

    def _correct_recorded_rate(inst: Instance) -> None:
        """Rewrite the ledger row's rate with the one read off the instance.

        compute-seam S4. Deliberately NOT a second ``_record_then_install``:
        ``Ledger.record`` appends, so re-firing it would leave two rows for one
        instance and re-enter the claim. This corrects the one row in place.
        """
        try:
            _ledger_for_claim.set_cost_rate(inst.id, inst.cost_rate_usd_per_hr)
        except Exception as rate_exc:  # noqa: BLE001
            _log.warning(
                "S4: ledger.set_cost_rate failed for %s: %s "
                "(the row keeps the pre-launch rate)",
                inst.id,
                rate_exc,
            )

    # ------------------------------------------------------------------
    # C29 — build the start_heartbeat closure ONCE per deploy_session.
    #
    # The closure is invoked from inside _provision_instance_and_build_backend
    # right after the RunPod-status poll succeeds (cold-start branches), so
    # the HeartbeatLoop ticks throughout engine.provision / wait_for_ready
    # instead of waiting until deploy_session resumes after provision returns.
    # ``None`` for any of the following short-circuits the closure and the
    # post-Step-8.5 fallback also stays inert:
    #
    #   - hosted engines (``requires_compute`` is False)
    #   - heartbeat disabled (``heartbeat_interval_s`` is None or <= 0)
    #   - no provider resolved (the helper would not be called either)
    #
    # The caller-supplied warm-pod branch reuses the same closure after Step
    # 8.5 — it has no boot phase to protect but still wants steady-state ticks.
    # ------------------------------------------------------------------
    _start_heartbeat: Callable[[Instance], HeartbeatLoopProtocol] | None = None
    if (
        _hb_interval is not None
        and _hb_interval > 0
        and resolved_engine.requires_compute
        and resolved_provider is not None
    ):
        from kinoforge._adapters import build_util_endpoint_for

        def _resolve_modal_endpoint(iid: str) -> str | None:
            entry = _ledger_for_claim.read(iid)
            if not entry:
                return None
            return (entry.get("endpoints") or {}).get("8000")

        _util_endpoint = (
            build_util_endpoint_for(
                cfg, creds, resolve_modal_endpoint=_resolve_modal_endpoint
            )
            if creds is not None
            else None
        )
        # Single source of truth for stall/restart-loop knobs: Config.lifecycle()
        # already applies the enabled-gating (window is None when the reap is
        # disabled — thresholds are then inert) and the Lifecycle defaults.
        # Re-deriving from cfg.compute.lifecycle here had triplicated the
        # default values across interfaces.py / config.py / this block.
        _lc_eff = cfg.lifecycle()
        _stall_window_s = _lc_eff.stall_window_s
        _stall_gpu_threshold = _lc_eff.stall_gpu_threshold
        _stall_cpu_threshold = _lc_eff.stall_cpu_threshold
        _restart_loop_window_s = _lc_eff.restart_loop_window_s
        _restart_loop_uptime_threshold_s = _lc_eff.restart_loop_uptime_threshold_s
        _provider_kind: str | None = (
            cfg.compute.provider if cfg.compute is not None else None
        )
        _factory: Callable[..., HeartbeatLoopProtocol] = (
            heartbeat_loop_factory or HeartbeatLoop
        )
        _start_heartbeat = _build_start_heartbeat_closure(
            ledger=Ledger(store=store),
            provider=resolved_provider,
            interval=_hb_interval,
            util_endpoint=_util_endpoint,
            cancel_token=cancel_token,
            provider_kind=_provider_kind,
            stall_window_s=_stall_window_s,
            stall_gpu_threshold=_stall_gpu_threshold,
            stall_cpu_threshold=_stall_cpu_threshold,
            restart_loop_window_s=_restart_loop_window_s,
            restart_loop_uptime_threshold_s=_restart_loop_uptime_threshold_s,
            factory=_factory,
        )

    # C29 — hb_loop populated either by the cold-start closure invocation
    # inside _provision_instance_and_build_backend or by the late-start
    # fallback for caller-supplied warm pods after Step 8.5.
    hb_loop: HeartbeatLoopProtocol | None = None

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
                        _warm_attach_install(
                            claim_holder=claim_holder,
                            ledger=_ledger_for_claim,
                            record_then_install=_record_then_install,
                            instance=instance,  # type: ignore[arg-type]
                        )
                        resolved_engine.provision(
                            instance, cfg_dict, cancel_token=cancel_token
                        )
                        backend = resolved_engine.backend(instance, cfg_dict)
                    else:
                        _result = _provision_instance_and_build_backend(
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
                            on_rate_verified=_correct_recorded_rate,
                            cancel_token=cancel_token,
                            start_heartbeat=_start_heartbeat,
                            capacity_wait_s=capacity_wait_s,
                            provisional_ledger=_provisional_ledger,
                        )
                        instance, backend, hb_loop = _result
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
                        _warm_attach_install(
                            claim_holder=claim_holder,
                            ledger=_ledger_for_claim,
                            record_then_install=_record_then_install,
                            instance=instance,  # type: ignore[arg-type]
                        )
                        resolved_engine.provision(
                            instance, cfg_dict, cancel_token=cancel_token
                        )
                        backend = resolved_engine.backend(instance, cfg_dict)
                    else:
                        _result = _provision_instance_and_build_backend(
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
                            on_rate_verified=_correct_recorded_rate,
                            cancel_token=cancel_token,
                            start_heartbeat=_start_heartbeat,
                            capacity_wait_s=capacity_wait_s,
                            provisional_ledger=_provisional_ledger,
                        )
                        instance, backend, hb_loop = _result
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

            # C29 — late-start HeartbeatLoop for the caller-supplied warm-pod
            # branch. Cold-start branches above already populated ``hb_loop``
            # by invoking ``_start_heartbeat`` inside
            # ``_provision_instance_and_build_backend`` (right after the
            # RunPod-status poll succeeded). Caller-supplied pods skipped that
            # path — they have no boot phase to protect — so the loop is
            # constructed here at the original pre-C29 location.
            if (
                hb_loop is None
                and _start_heartbeat is not None
                and instance is not None
            ):
                try:
                    hb_loop = _start_heartbeat(instance)
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "C29: late-start hb_loop construction failed for "
                        "caller-supplied %s",
                        instance.id,
                    )
                    hb_loop = None

            if hb_loop is not None and instance is not None:
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
    store: ArtifactStore | None = None,
    run_id: str = "",
) -> DeployResult:
    """Provision compute (or confirm hosted endpoint) for a kinoforge config.

    Steps for the **compute path**:

    1. Derive a ``CapabilityKey`` from *cfg*.
    2. Resolve the engine (registry or injection).
    3. If ``engine.requires_compute == False`` (hosted): skip compute entirely,
       return a ``DeployResult`` with ``instance=None`` and the engine's endpoints.
    4. Resolve the provider.  Since compute-seam S4 there is no offer pre-check
       here: ``find_offers`` is off the ABC and selection happens inside
       ``create_instance``, so an enumerating provider raises ``CapacityError``
       from its own empty catalog, with its own message.
    5. **Dry-run:** print a vendor/engine-neutral plan and return a
       ``DeployResult(instance=None, plan_text=...)``.  ``create_instance`` is
       NEVER called in dry-run mode, and no ledger row is written.
    6. **Live run:** write the pre-launch provisional row (when *store* is
       given), create an instance, wait for ``status == "ready"``
       (``LocalProvider`` returns ready immediately), record the real row and
       collapse the provisional one onto it, and return a ``DeployResult`` with
       the instance and provider endpoints.

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
            key collision, EXCEPT for kinoforge-reserved keys, which are
            dropped with a warning (see :func:`_strip_reserved_tags`).
        store: The artifact store whose ledger receives this launch's rows —
            the pre-launch ``kf_launch_phase=launching`` row (finding F12) and,
            on success, the real one. Additive and defaulted so every existing
            caller keeps working; ``None`` means no ledger row of either kind,
            which is the pre-S5 behaviour. ``cli/_commands._cmd_deploy`` passes
            ``ctx.store()`` — the same store ``kinoforge list``, the reconciler
            and the sweeper read, which a store constructed in here from a
            default path would NOT be.
        run_id: Client-side id for this launch. It becomes ``spec.run_id``
            (hence the provider-side resource NAME) and the provisional row's
            key, which is what lets ``cli/_reconcile`` adopt the row by name.
            Empty (the default) mints one — see :func:`_mint_deploy_run_id` for
            why an empty id cannot simply be passed through.

    Returns:
        A ``DeployResult`` describing the outcome.

    Raises:
        CapacityError: The provider found nothing bookable for the cfg's
            placement, or every candidate it tried lacked capacity.
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

    # Compute path: resolve the provider. compute-seam S4 — selection happens
    # inside create_instance, so there is no catalog read here and no
    # "no offers" pre-check: an enumerating provider raises CapacityError from
    # its own empty catalog, with its own message.
    resolved_provider = _resolve_provider(cfg, provider)

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
            f"  placement:        {cfg.placement()}\n"
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
    # compute-seam S5 — the same strip ``_provision_instance_and_build_backend``
    # applies. ``deploy`` is a second, independent route from caller tags to
    # ``spec.tags`` and from there onto a real ``Instance.tags`` (SkyPilot
    # spreads them verbatim), so without this a config setting
    # ``kf_launch_phase`` produces a REAL row that reads as provisional and
    # every later collapse silently refuses.
    tags = _strip_reserved_tags(dict(tags or {}))
    launch_run_id = run_id or _mint_deploy_run_id()

    def _build_spec() -> InstanceSpec:
        return build_instance_spec(
            cfg=cfg,
            rendered=RenderedProvision(
                script="", image=image, ports=[], env_required=[]
            ),
            engine_name=resolved_engine.name,
            key_hash=key_hash,
            image=image,
            lifecycle=lifecycle,
            env={},
            run_id=launch_run_id,
            tags=tags,
        )

    # F12 on the one-shot path. ``kinoforge deploy`` is the launch most likely
    # to be interrupted mid-create, and until now it was the only launch path
    # with no durable pre-launch record at all: a Ctrl-C during a multi-minute
    # provision left a billing cluster with nothing in the ledger to find it.
    # Same contract as ``_provision_instance_and_build_backend`` — written
    # BEFORE create, keyed by the client-side run id, and kept on any raise the
    # provider has not declared as booking nothing (ruling C1).
    provisional_ledger = Ledger(store=store) if store is not None else None
    provisional_id = (
        _record_provisional_row(
            ledger=provisional_ledger,
            run_id=launch_run_id,
            provider_name=getattr(resolved_provider, "name", "unknown"),
            tags=dict(tags),
            max_age_s=int(lifecycle.max_lifetime_s),
            now=time.time(),
        )
        if provisional_ledger is not None
        else None
    )
    # Read BEFORE the create so a fault in the declaration cannot land inside
    # the failure path itself.
    nothing_was_booked = _nothing_booked_error_types(resolved_provider)

    # compute-seam S4: selection and its retry belong to the provider.
    try:
        instance = resolved_provider.create_instance(_build_spec())
    except nothing_was_booked:
        # These, and ONLY these, prove no resource exists. A create that never
        # produced one must not leave a row whose est_spend inflates forever
        # (cli/_reconcile's "$210 phantom pod").
        _forget_provisional_row(provisional_ledger, provisional_id)
        raise
    except BaseException:
        # Ruling C1: a raise does not prove the provider booked nothing, and a
        # Ctrl-C lands here as a BaseException while the provider's own API
        # server goes on creating the resource. Leave the row;
        # cli/_reconcile._adopt_or_age_out resolves it after the grace window.
        raise

    try:
        # Poll until ready (LocalProvider returns ready immediately; cloud providers
        # may require polling — this handles both).
        #
        # Only ``status`` is refreshed from the polled response. ``created_at``,
        # ``tags``, and ``cost_rate_usd_per_hr`` are authoritative on the
        # ``create_instance`` return because providers (notably SkyPilot and
        # RunPod) cannot reliably recover those fields from their list/status
        # APIs — see ``_cluster_record_to_instance`` and ``_pod_to_instance``,
        # both of which hard-code ``created_at=0.0`` and drop tags / cost rate.
        # Reassigning ``instance`` here would clobber the rich create-time
        # fields and surface as ``age=~56y``, ``est_spend=$0.00``, and
        # ``capability_key=<unknown>`` in the ledger (Stage E live smoke
        # 2026-06-18 regression).
        instance.status = _wait_for_provider_ready(
            resolved_provider,
            instance,
            boot_timeout_s=lifecycle.boot_timeout_s,
        )

        # compute-seam S5: deploy_session hands the instance to a caller
        # that will immediately make HTTP requests against it, so use the
        # door that repairs a dead tunnel rather than the pure read.
        endpoints = resolved_provider.ensure_endpoints(instance)
        # Order is load-bearing, and mirrors ``_provision_instance_and_build_backend``:
        # the REAL row is written first, so no window exists in which a kill
        # loses both rows, and ``_collapse_provisional_row`` removes the
        # provisional one only because the real one is there to survive it.
        # ``_cmd_deploy`` used to write this row after ``deploy`` returned;
        # doing it here is what makes the collapse expressible at all.
        if provisional_ledger is not None:
            _record_real_row(provisional_ledger, instance, lifecycle=lifecycle)
            _collapse_provisional_row(provisional_ledger, provisional_id, instance.id)
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
    request: GenerationRequest | None,
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
    skip_clip_stage: bool = False,
    initial_clip: Artifact | None = None,
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
        skip_clip_stage: T10 upscale-only flag. When ``True``,
            :class:`~kinoforge.pipeline.generate_clip.GenerateClipStage`
            is NOT constructed and the request-validation / splitter
            steps are skipped — the orchestrator threads ``initial_clip``
            straight into ``UpscaleStage``. ``request`` may be ``None``
            in this mode. Default ``False`` preserves the
            request-validated text-to-video path.
        initial_clip: Source clip Artifact used to seed
            ``state.artifacts["clip"]`` when ``skip_clip_stage=True``.
            Ignored when ``skip_clip_stage`` is ``False``. The artifact
            must reference a local mp4 the upscaler engine can fetch via
            its ``source_url`` payload — kinoforge's source-resolver
            chain handles ``file://`` / ``hf:`` / ``http(s)://`` URIs.

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
        CapacityError: No compute offer satisfies ``cfg.placement()``.
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
        seed_artifacts: dict[str, Artifact] = {}
        if skip_clip_stage and initial_clip is not None:
            # T10 — upscale-only entry: seed state.artifacts["clip"] so
            # UpscaleStage finds its input without GenerateClipStage running.
            seed_artifacts["clip"] = initial_clip
        # Synthesize a placeholder request only on the upscale-only path
        # so PipelineState's non-None contract holds. UpscaleStage does
        # not consume the request. Default path passes the
        # operator-supplied request through unchanged; if request is
        # None there, the cast preserves the original behaviour of
        # exploding on first use.
        if skip_clip_stage and request is None:
            effective_request: GenerationRequest = GenerationRequest(
                prompt="", mode="upscale"
            )
        else:
            effective_request = cast(GenerationRequest, request)
        state = PipelineState(request=effective_request, artifacts=seed_artifacts)
        if cfg.keyframe is not None and not skip_clip_stage:
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
        # video profile + split into prompt segments + assemble
        # GenerateClipStage. All three steps are skipped in upscale-only
        # mode (skip_clip_stage=True) — there is no clip to generate and
        # state.artifacts["clip"] is already seeded from initial_clip.
        # ------------------------------------------------------------------
        cfg_dict = _cfg_dict(cfg)
        stages: list[Stage] = []
        if not skip_clip_stage:
            validated = validate_request(
                session.profile, state.request, accepted_kinds=accepted_kinds
            )
            state = dataclasses.replace(state, request=validated)

            splitter = registry.get_splitter(cfg.splitter.kind)()
            prompt_segments = splitter.split(validated.prompt, session.profile, {})
            if prompt_segments and validated.assets:
                prompt_segments[0] = dataclasses.replace(
                    prompt_segments[0], assets=list(validated.assets)
                )

            # Layer 8: provider + model for the OutputSink filename schema.
            # Provider = registered engine name; model = engine.model_identity(cfg)
            # so non-hosted engines (fal, comfyui, bedrock) get a real slug
            # instead of "unknown". Empty return -> WARNING + None -> sink
            # renders "unknown".
            _provider = getattr(session.engine, "name", None) or None
            _raw_model = session.engine.model_identity(cfg_dict)
            if not _raw_model:
                _log.warning(
                    "engine %s returned empty model identity; "
                    "sink will render filename slug as 'unknown'",
                    session.engine.name,
                )
            _model = _raw_model or None
            stages.append(
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
            )

        # T16 — append UpscaleStage when cfg.upscale is set. The upscaler
        # engine routes through registry.get_upscaler so adding a future
        # backend (FlashVSR) needs only its own self-registration; no
        # change to this orchestrator branch.
        if cfg.upscale is not None:
            from kinoforge.core import registry as _registry
            from kinoforge.core.scale_target import ScaleTarget
            from kinoforge.pipeline.upscale import UpscaleStage

            upscaler_engine = _registry.get_upscaler(cfg.upscale.engine)()
            stages.append(
                UpscaleStage(
                    engine=upscaler_engine,
                    scale=ScaleTarget.parse(cfg.upscale.scale),
                    instance=session.instance,
                    cfg=cfg_dict,
                    cancel_token=cancel_token,
                )
            )

        # Append InterpolateStage when cfg.interpolate is set. Standalone path
        # only (see plan Planning-time correction): interp runs on its own pod
        # via a separate `kinoforge interpolate` invocation, so its input clip
        # is always the seeded initial_clip (local/http), never a mid-walk
        # pod-side upscaled artifact. The local-decimate branch publishes via
        # the sink seam below; the engine branch's pod-proxy artifact is
        # materialized post-walk (mirrors the upscale materialize block).
        if cfg.interpolate is not None:
            from kinoforge.core import registry as _registry
            from kinoforge.pipeline.interpolate import InterpolateStage

            interp_engine = _registry.get_interpolator(cfg.interpolate.engine)()
            _interp_provider = cfg.interpolate.engine

            def _interp_publish(body: bytes) -> str:
                assert sink is not None  # noqa: S101 — guarded by the seam below
                return "file://" + sink.publish(
                    body,
                    prompt="interpolate",
                    extension=".mp4",
                    provider=_interp_provider,
                    model="interp",
                    kind="interpolated",
                )

            stages.append(
                InterpolateStage(
                    engine=interp_engine,
                    target_fps=cfg.interpolate.fps,
                    instance=session.instance,
                    cfg=cfg_dict,
                    cancel_token=cancel_token,
                    publish=_interp_publish if sink is not None else None,
                )
            )

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

        # T10 — upscale-only entry returns the upscaled artifact, not the
        # input clip. Standalone interpolate returns the interpolated artifact.
        # Default path still returns the clip artifact.
        if skip_clip_stage and cfg.interpolate is not None:
            artifact_key = "interpolated"
        elif skip_clip_stage and cfg.upscale is not None:
            artifact_key = "upscaled"
        else:
            artifact_key = "clip"

        # T15/att6 + T16/att2 fix: ``UpscaleStage.run`` returns an Artifact
        # whose ``.uri`` is the pod's proxy URL (e.g.
        # https://<pod>-8000.proxy.runpod.net/artifacts/X). ``--no-reuse``
        # destroys the pod in deploy_session's finally after this function
        # returns, so we MUST materialize the upscaled bytes NOW while the
        # pod is still alive — otherwise the URI points at a dead pod.
        # Materialize regardless of which artifact ``artifact_key`` returns
        # so the sinked mp4 always survives (multi-stage `kinoforge generate`
        # returns "clip" but still needs the upscaled file on disk).
        upscaled = state.artifacts.get("upscaled")
        _downscale_to = upscaled.meta.get("downscale_to") if upscaled else None
        _needs_materialize = (
            upscaled is not None
            and sink is not None
            and (
                upscaled.uri.startswith(("http://", "https://"))
                or (_downscale_to is not None and upscaled.uri.startswith("file://"))
            )
        )
        if _needs_materialize and upscaled is not None and sink is not None:
            from kinoforge.pipeline.materialize import finalize_upscaled_bytes

            if upscaled.uri.startswith(("http://", "https://")):
                import urllib.request as _urequest  # orchestrator stays urllib-free

                _log.info("materializing upscaled artifact from %s", upscaled.uri)
                req = _urequest.Request(  # noqa: S310 — pod proxy URL only
                    upscaled.uri,
                    headers={"User-Agent": "kinoforge-orchestrator/0.1"},
                )
                with _urequest.urlopen(req, timeout=600) as resp:  # noqa: S310
                    body: bytes = resp.read()
            else:
                body = Path(upscaled.uri.removeprefix("file://")).read_bytes()

            if _downscale_to is not None:
                _log.info("downscaling upscaled artifact to %dp", _downscale_to)
                body = finalize_upscaled_bytes(body, _downscale_to)
            provider_tag = cfg.upscale.engine if cfg.upscale is not None else "unknown"
            spec_obj: Any = cfg.spec
            model_tag = (
                getattr(spec_obj, "model", None)
                or (spec_obj.get("model") if isinstance(spec_obj, dict) else None)
                or "unknown"
            )
            local_path = sink.publish(
                body,
                prompt="upscale",
                extension=".mp4",
                provider=provider_tag,
                model=model_tag,
                kind="upscaled",
            )
            state.artifacts["upscaled"] = dataclasses.replace(
                upscaled, uri=f"file://{local_path}"
            )

        # Materialize a pod-side interpolated artifact (engine branch) while the
        # pod is still alive — same reason as the upscale block above. The
        # local-decimate branch already published via the stage's publish seam,
        # so only an http(s) proxy uri needs fetching here. Interp has no
        # downscale, so this is the simpler fetch+publish half.
        interpolated = state.artifacts.get("interpolated")
        if (
            interpolated is not None
            and sink is not None
            and interpolated.uri.startswith(("http://", "https://"))
        ):
            import urllib.request as _urequest  # orchestrator stays urllib-free

            _log.info("materializing interpolated artifact from %s", interpolated.uri)
            req = _urequest.Request(  # noqa: S310 — pod proxy URL only
                interpolated.uri,
                headers={"User-Agent": "kinoforge-orchestrator/0.1"},
            )
            with _urequest.urlopen(req, timeout=600) as resp:  # noqa: S310
                interp_body: bytes = resp.read()
            provider_tag = cfg.interpolate.engine if cfg.interpolate else "unknown"
            local_path = sink.publish(
                interp_body,
                prompt="interpolate",
                extension=".mp4",
                provider=provider_tag,
                model="interp",
                kind="interpolated",
            )
            state.artifacts["interpolated"] = dataclasses.replace(
                interpolated, uri=f"file://{local_path}"
            )

        artifact = state.artifacts[artifact_key]
        _log.info("generate completed — artifact uri=%r", artifact.uri)
        owned_instance = None if _caller_supplied_instance else session.instance
        return artifact, owned_instance
