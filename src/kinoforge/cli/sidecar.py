"""Sidecar pointer recording which artifact store backs the ledger.

Written by cfg-bearing CLI subcommands on first run; read by no-config
subcommands (``list``, ``stop``, ``destroy``, ``forget``, ``reap``) so
they discover the configured store without needing ``--config`` on the
command line.

The sidecar is per-``state_dir``: every operator's local
``.kinoforge/store.json`` records which store their ledger lives in.
Cross-machine bootstrap is a Layer T+1 concern (``--store-uri`` /
``KINOFORGE_STORE_URI``).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from kinoforge.core.config import Config
from kinoforge.core.errors import SidecarMigrationBlocked, SidecarMismatch

SIDECAR_NAME = "store.json"
LEDGER_RUN_ID = "_lifecycle"
LEDGER_NAME = "ledger.json"


class SidecarRecord(BaseModel):
    """Frozen mirror of ``StoreConfig``'s identity fields.

    ``extra="forbid"`` catches forward-compat drift: a newer kinoforge
    that adds a ``StoreConfig`` field but forgets to mirror it here will
    fail the parametrized field-mirror test before the change ships.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    kind: str
    bucket: str | None = None
    prefix: str = ""
    root: str | None = None

    @classmethod
    def from_cfg(cls, cfg: Config) -> SidecarRecord:
        """Build a record from the store block of a loaded Config."""
        sc = cfg.store
        return cls(
            kind=sc.kind,
            bucket=sc.bucket,
            prefix=sc.prefix,
            root=str(sc.root) if sc.root is not None else None,
        )

    def differs_from(self, other: SidecarRecord) -> bool:
        """Return True when any mirrored identity field differs."""
        return self.model_dump() != other.model_dump()


def _path(state_dir: Path) -> Path:
    return state_dir / SIDECAR_NAME


def read_sidecar(state_dir: Path) -> SidecarRecord | None:
    """Load the sidecar from ``state_dir/store.json``.

    Raises:
        pydantic.ValidationError: corrupt JSON or unknown field.

    Returns:
        ``SidecarRecord`` if present, ``None`` if the file is absent.
    """
    p = _path(state_dir)
    if not p.exists():
        return None
    return SidecarRecord.model_validate_json(p.read_text())


def write_sidecar(state_dir: Path, cfg: Config) -> None:
    """Persist a fresh sidecar describing ``cfg.store``.

    Creates ``state_dir`` (and parents) if absent.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = SidecarRecord.from_cfg(cfg)
    _path(
        state_dir
    ).write_text(  # kinoforge:public-write — sidecar carries cfg.store identity only, no prompt-derived bytes
        json.dumps(rec.model_dump(), indent=2)
    )


def _local_ledger_nonempty(state_dir: Path) -> bool:
    """Return True iff ``state_dir/_lifecycle/ledger.json`` has one+ entries.

    Reads the raw file (no ``LocalArtifactStore`` construction). Corrupt
    JSON is treated as empty — safer to allow a fresh sidecar to be
    written than to brick the operator on a malformed local file.
    """
    p = state_dir / LEDGER_RUN_ID / LEDGER_NAME
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return isinstance(entries, list) and bool(entries)


def verify_or_write_sidecar(state_dir: Path, cfg: Config) -> None:
    """Verify cfg.store matches the sidecar, or write a fresh sidecar.

    Args:
        state_dir: Root of the operator's local state directory.
        cfg: Loaded Config whose ``store`` block describes the store.

    Raises:
        SidecarMismatch: cfg.store differs from the sidecar on disk.
        SidecarMigrationBlocked: first cloud-store cfg attempted while
            ``state_dir/_lifecycle/ledger.json`` has entries.
    """
    existing = read_sidecar(state_dir)
    new = SidecarRecord.from_cfg(cfg)
    if existing is not None:
        if existing.differs_from(new):
            raise SidecarMismatch(
                f"cfg.store ({new.model_dump()}) differs from sidecar "
                f"({existing.model_dump()}); remove {_path(state_dir)} "
                f"or revert cfg.store to switch"
            )
        return
    if new.kind != "local" and _local_ledger_nonempty(state_dir):
        raise SidecarMigrationBlocked(
            f"refusing to switch to cloud store ({new.kind}) while local "
            f"ledger has entries; run `kinoforge destroy` on each "
            f"local-tracked instance, then re-run"
        )
    write_sidecar(state_dir, cfg)
