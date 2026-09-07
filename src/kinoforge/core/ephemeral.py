"""EphemeralSession + EphemeralPolicy + pre-flight capability table.

EphemeralSession is a context manager whose active instance is held in
a process-wide class attribute. Stdlib ``ThreadPoolExecutor.map`` does
NOT auto-propagate ``contextvars.ContextVar`` across worker threads, so
storing the session in a class attribute is what guarantees that every
worker thread spawned inside the with-block (kinoforge's
``ConcurrentPool``) sees the same active session via
``EphemeralSession.current()``. Nesting is handled by stashing the
previous active value on each instance and restoring it on ``__exit__``.

Single-session-per-process is the intended use — the CLI wraps one
ephemeral session around a whole ``generate``/``batch`` invocation, so
two with-blocks racing in different threads is not a supported pattern.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from kinoforge.core.vault import Vault
    from kinoforge.stores.base import ArtifactStore


@dataclass(frozen=True)
class EphemeralPolicy:
    """Per-session toggle of every persistent-write gate + provider-side actions.

    Frozen so a misbehaving engine cannot flip a gate mid-run. Fields
    cover the three write gates (ledger, profile cache, batch summary),
    two optional sidecar/heartbeat gates, the provider-side
    delete-on-completion contract, identifier handling, and the logging
    safety override.
    """

    # Persistent-write gates
    ledger_record: bool
    profile_cache_persist: bool
    batch_summary_write: bool
    cost_sidecar_write: bool
    heartbeat_ledger_touch: bool
    # Provider-side
    delete_on_completion: bool
    delete_retries: int
    # Identifiers
    memory_only_run_id: bool
    pod_name_includes_alias: bool
    # Logging
    force_debug_show_secrets_off: bool


DEFAULT_POLICY = EphemeralPolicy(
    ledger_record=True,
    profile_cache_persist=True,
    batch_summary_write=True,
    cost_sidecar_write=True,
    heartbeat_ledger_touch=True,
    delete_on_completion=False,
    delete_retries=0,
    memory_only_run_id=False,
    pod_name_includes_alias=True,
    force_debug_show_secrets_off=False,
)

STRICT_POLICY = EphemeralPolicy(
    ledger_record=False,
    profile_cache_persist=False,
    batch_summary_write=False,
    cost_sidecar_write=False,
    heartbeat_ledger_touch=False,
    delete_on_completion=True,
    delete_retries=3,
    memory_only_run_id=True,
    pod_name_includes_alias=False,
    force_debug_show_secrets_off=True,
)


#: Provider-side name prefixes for the opaque ephemeral resource name. The
#: shapes are the ones each provider minted for itself before spec A2 moved the
#: mint controller-side, and they are load-bearing beyond cosmetics: Modal's
#: opaque name IS the ``Instance.id``, and ``providers/modal`` derives the app
#: name ``kinoforge-<name>`` from it, which ``probe_runtime`` matches on.
_RESOURCE_NAME_PREFIXES: dict[str, str] = {
    "modal": "eph-",
    "runpod": "kinoforge-",
}
_RESOURCE_NAME_PREFIX_DEFAULT = "kinoforge-"


EPHEMERAL_CAPABILITIES: dict[tuple[str, str | None], bool] = {
    ("comfyui", "runpod"): True,
    ("comfyui", "local"): True,
    ("comfyui", "skypilot"): True,
    ("comfyui", "modal"): True,
    ("diffusers", "runpod"): True,
    ("diffusers", "local"): True,
    ("diffusers", "skypilot"): True,
    ("diffusers", "modal"): True,
    # Fake engine: offline test backbone, no provider-side state ever
    # exists to scrub, so ephemeral is trivially supported under any
    # in-process provider.
    ("fake", "local"): True,
    ("hosted", None): False,
    ("replicate", None): True,
    ("runway", None): True,
    ("fal", None): False,
    ("luma", None): False,
}


class EphemeralSession:
    """Context manager activating the ephemeral policy.

    Use ``with EphemeralSession(enabled=...) as s:``. Inside the block,
    ``EphemeralSession.current()`` returns this session; outside, ``None``.
    The active session is stored in a process-wide class attribute so it
    is visible to every worker thread spawned inside the with-block
    (kinoforge's ``ConcurrentPool``). Nesting is handled by stashing the
    previous active value on each instance and restoring it on ``__exit__``.
    """

    _active: ClassVar[EphemeralSession | None] = None

    def __init__(self, *, enabled: bool, vault: Vault | None = None) -> None:
        """Construct a session bound to STRICT or DEFAULT policy.

        Args:
            enabled: When True, binds to ``STRICT_POLICY``; otherwise
                ``DEFAULT_POLICY``.
            vault: Optional loaded :class:`Vault`. Threaded in by the
                CLI's ``_load_vault_or_none`` result so downstream
                ephemeral-aware sites (notably the provision marker
                alias-key swap; see
                ``docs/superpowers/specs/2026-06-10-provision-marker-alias-keying-design.md``)
                can derive a deterministic alias instead of writing the
                raw ``CapabilityKey.derive()`` hash to disk. ``None`` is
                valid — vault-less ``--ephemeral`` runs fall back to the
                raw hash (no alias source available, no sensitive
                material in scope).
        """
        self.policy = STRICT_POLICY if enabled else DEFAULT_POLICY
        self.vault = vault
        # P3 — set by `_cmd_generate` when --loras is passed; downstream
        # resolver call sites (warm-reuse set_stack swap) read it so the
        # CLI override flows end-to-end without threading a kwarg through
        # every orchestrator/backend hop.
        self.cli_loras: list[Any] | None = None
        self.in_memory_ledger: dict[str, dict[str, Any]] = {}
        self.in_memory_profiles: dict[str, Any] = {}
        self._registered_stores: list[tuple[ArtifactStore, str]] = []
        self._prev: EphemeralSession | None = None
        self._entered = False
        # Spec A2 — one opaque token per launch, minted on first ask and
        # shared by the controller (which writes the pre-create index row)
        # and the provider (which names the resource). ConcurrentPool workers
        # see the same session, so the memo is locked.
        self._resource_tokens: dict[str, str] = {}
        self._resource_lock = threading.Lock()
        # Spec A2 — pods whose ``--no-reuse`` teardown did NOT confirm. The
        # orchestrator swallows TeardownError by design; this is the only
        # channel telling the CLI the pod may still be billing.
        self._destroy_unconfirmed: set[str] = set()

    @classmethod
    def current(cls) -> EphemeralSession | None:
        """Return the active session for this process, or ``None``."""
        return cls._active

    def resource_name(self, run_id: str, provider: str) -> str:
        """Return the provider-side NAME this launch's resource will carry.

        Spec A2. Under ``policy.pod_name_includes_alias`` (the default) the
        name is simply ``run_id`` — every log line, teardown message, warm
        attach key and ``cli/_reconcile._adopt_launching_row`` match already
        names a resource by it.

        Under ``--ephemeral``'s STRICT_POLICY the run id must never reach the
        provider (it carries the subcommand and a local timestamp, and a
        stopped Modal app lingers in ``modal app list`` forever), so the name
        is an opaque random token. Both providers used to mint that token
        themselves INSIDE ``create_instance``, which meant the name did not
        exist until the create was already in flight — and the pre-create
        index row, whose whole purpose is to be a handle on a resource that is
        already billing, could not name it. Minting here instead lets the
        controller reserve the row under the name the provider will use, while
        preserving exactly what STRICT_POLICY guarantees: a random token with
        no alias in it.

        Minted once per ``run_id`` and memoised, so the controller and the
        provider always agree; the prefix is applied per provider, so asking
        on behalf of a second provider does not re-roll the token.

        Args:
            run_id: The client-side run id for this launch. An EMPTY run id is
                never memoised — a shared name is not a handle, so each ask
                gets a fresh token.
            provider: Provider kind (``"modal"``, ``"runpod"``, ...). An
                unknown provider gets the default prefix rather than raising:
                adding a provider is a naming-cosmetics decision, not a crash
                on the ephemeral path.

        Returns:
            The provider-side resource name.
        """
        if self.policy.pod_name_includes_alias:
            return run_id
        prefix = _RESOURCE_NAME_PREFIXES.get(provider, _RESOURCE_NAME_PREFIX_DEFAULT)
        if not run_id:
            return f"{prefix}{secrets.token_hex(4)}"
        with self._resource_lock:
            token = self._resource_tokens.get(run_id)
            if token is None:
                token = secrets.token_hex(4)
                self._resource_tokens[run_id] = token
        return f"{prefix}{token}"

    def mark_destroy_unconfirmed(self, pod_id: str) -> None:
        """Record that *pod_id*'s teardown did not confirm the resource is gone.

        Spec A2. ``deploy_session``'s ``--no-reuse`` teardown catches
        ``TeardownError``, logs "use ``kinoforge reap --apply`` to recover" and
        returns normally — the return tuple is already fixed by then — so the
        CLI cannot otherwise tell a clean teardown from a failed one and would
        release the last durable trace of a live pod.

        Args:
            pod_id: The instance id whose destroy did not confirm.
        """
        self._destroy_unconfirmed.add(pod_id)

    def destroy_was_confirmed(self, pod_id: str) -> bool:
        """Return whether *pod_id* is known to be gone.

        Optimistic by design: only a teardown that actually failed marks a pod,
        so a pod nothing ever tried to destroy reads as confirmed. Callers use
        this to decide whether it is safe to drop a durable record, and the
        only unsafe direction is claiming a live pod is gone.

        Args:
            pod_id: The instance id to check.

        Returns:
            ``False`` only when :meth:`mark_destroy_unconfirmed` was called for
            this id.
        """
        return pod_id not in self._destroy_unconfirmed

    def register_store(self, store: ArtifactStore, run_id: str) -> None:
        """Queue a (store, run_id) pair for cleanup on ``__exit__`` (Task 15)."""
        self._registered_stores.append((store, run_id))

    def __enter__(self) -> EphemeralSession:
        """Activate this session in the current process."""
        self._prev = EphemeralSession._active
        EphemeralSession._active = self
        self._entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Scrub every registered run and restore the previous active session.

        When ``policy.delete_on_completion`` is ``True``, calls
        ``store.delete_run(run_id)`` for every registered ``(store, run_id)``
        pair AFTER the with-block body — so an ``OutputSink.publish`` that
        ran inside the block has already copied the user-facing artifact
        out to its publish destination before the store-side bytes are
        scrubbed. Runs regardless of whether the with-block raised, so a
        partial failure does not leave a footprint.

        On a ``delete_run`` failure, raises ``EphemeralStoreCleanupFailedError``
        carrying the store's ``manual_cleanup_command(run_id)`` so the
        operator can finish the scrub by hand. The active-session pointer
        is still restored (in the ``finally``) before the error propagates.
        """
        try:
            if self.policy.delete_on_completion:
                from kinoforge.core.errors import EphemeralStoreCleanupFailedError

                for store, run_id in self._registered_stores:
                    try:
                        store.delete_run(run_id)
                    except Exception as e:
                        raise EphemeralStoreCleanupFailedError(store, run_id, e) from e
        finally:
            if self._entered:
                EphemeralSession._active = self._prev
                self._prev = None
                self._entered = False
