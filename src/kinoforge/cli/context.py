"""SessionContext — the per-invocation bundle of state_dir + cfg + lazy store.

Built once in ``cli._main.main()`` and threaded through every subcommand
handler. Lazy ``store()`` and ``ledger()`` accessors mean ``kinoforge --help``
never touches cloud SDKs, and ``ledger_safe()`` lets the always-on
instance overview degrade gracefully when cloud credentials are
unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from kinoforge.cli.sidecar import (
    LEDGER_RUN_ID,
    SidecarRecord,
    read_sidecar,
    verify_or_write_sidecar,
)
from kinoforge.core.cancel import CancelToken
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore

log = logging.getLogger(__name__)


@dataclass
class SessionContext:
    """Per-invocation state for the CLI.

    Attributes:
        state_dir: Operator's local state directory (``--state-dir`` arg).
        cfg: Loaded Config, or None when no ``--config`` was passed.
        sidecar: Snapshot of the sidecar from ``state_dir/store.json``,
            or None if absent.
        clock: Injected clock seam — defaults to RealClock.
    """

    state_dir: Path
    cfg: Config | None
    sidecar: SidecarRecord | None
    clock: Clock = field(default_factory=RealClock)
    # Phase 50 — per-invocation cooperative-cancellation token. The CLI's
    # SIGINT handler (installed for ``generate`` / ``batch``) sets this
    # token on first Ctrl-C; the orchestrator + every backend poll loop
    # observe it and unwind cooperatively. ``default_factory=CancelToken``
    # so each SessionContext gets a *fresh* token — sharing one across
    # invocations would let a previously-set token instant-cancel the
    # next run.
    cancel_token: CancelToken = field(default_factory=CancelToken)
    _store: ArtifactStore | None = None
    _ledger: Ledger | None = None

    @classmethod
    def from_args(
        cls,
        *,
        state_dir: Path,
        cfg_path: Path | None,
        clock: Clock | None = None,
    ) -> SessionContext:
        """Build a SessionContext from parsed CLI arguments.

        - Loads ``cfg_path`` when present (None for no-config commands).
        - Verifies / writes the sidecar when cfg is loaded.
        - Snapshots the sidecar for later lookup.

        Raises:
            SidecarMismatch: when cfg.store differs from on-disk sidecar.
            SidecarMigrationBlocked: on first cloud-cfg with non-empty
                local ledger.
            pydantic.ValidationError: when the on-disk sidecar is corrupt.
        """
        cfg = load_config(cfg_path) if cfg_path is not None else None
        if cfg is not None:
            verify_or_write_sidecar(state_dir, cfg)
        sidecar = read_sidecar(state_dir)
        return cls(
            state_dir=state_dir,
            cfg=cfg,
            sidecar=sidecar,
            clock=clock or RealClock(),
        )

    def store(self) -> ArtifactStore:
        """Lazily build and cache the configured ArtifactStore.

        Precedence: cfg.store > sidecar > LocalArtifactStore(state_dir).
        """
        if self._store is not None:
            return self._store
        if self.cfg is not None:
            from kinoforge.cli._commands import _build_store  # noqa: PLC0415

            self._store = _build_store(self.cfg, self.state_dir)
        elif self.sidecar is not None:
            self._store = _build_store_from_sidecar(self.sidecar, self.state_dir)
        else:
            self._store = LocalArtifactStore(self.state_dir)
        return self._store

    def ledger(self) -> Ledger:
        """Lazily build and cache the lifecycle Ledger backed by ``store()``."""
        if self._ledger is None:
            self._ledger = Ledger(store=self.store(), run_id=LEDGER_RUN_ID)
        return self._ledger

    def ledger_safe(self) -> tuple[Ledger | None, str | None]:
        """Best-effort ledger accessor — never raises.

        Used by ``_print_instance_overview`` which runs at the top of
        every invocation. When store construction fails (expired creds,
        unreachable bucket), returns ``(None, "<type>: <msg>")`` for the
        overview to print as a warning header.

        Returns:
            ``(ledger, None)`` on success, ``(None, reason)`` on failure.
        """
        try:
            return self.ledger(), None
        except Exception as exc:  # noqa: BLE001 — best-effort surface
            return None, f"{type(exc).__name__}: {exc}"


def _build_store_from_sidecar(sc: SidecarRecord, state_dir: Path) -> ArtifactStore:
    """Reconstruct the ArtifactStore named by a sidecar record.

    Cloud SDK imports are lazy so no-config commands (``kinoforge --help``,
    ``kinoforge list`` with a local sidecar) never load boto3 / google-cloud.

    Raises:
        UnknownAdapter: sidecar.kind is not one of ``local | s3 | gcs``
            (e.g. a sidecar written by a newer kinoforge with cloud
            backends this binary does not understand).
    """
    if sc.kind == "local":
        root = Path(sc.root) if sc.root else state_dir
        return LocalArtifactStore(root)
    if sc.kind == "s3":
        from kinoforge.stores.s3 import S3ArtifactStore  # noqa: PLC0415

        if sc.bucket is None:
            raise ValueError("s3 sidecar missing bucket")  # invariant from StoreConfig
        return S3ArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    if sc.kind == "gcs":
        from kinoforge.stores.gcs import GCSArtifactStore  # noqa: PLC0415

        if sc.bucket is None:
            raise ValueError("gcs sidecar missing bucket")
        return GCSArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    raise UnknownAdapter(f"unknown sidecar kind: {sc.kind!r}")
